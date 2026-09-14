import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from plane_demo.setup.bootstrap import OWNERS, SCHEMA_VERSION, initialize, observe

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        ("management", "mgmt_provisioner"),
        ("control", "cp_api"),
    ],
)
def test_database_observation_uses_existing_runtime_login(database_server, kind, role):
    with psycopg.connect(
        database_server.management_admin if kind == "management" else database_server.control_admin
    ) as connection:
        slots = (
            [
                {"pair_id": pair, "reporting_role": reporting_role}
                for pair, reporting_role in connection.execute(
                    "SELECT pair_id,reporting_role FROM management.pairs ORDER BY pair_id"
                ).fetchall()
            ]
            if kind == "management"
            else []
        )
        before = connection.execute("SELECT * FROM demo_metadata.schema_version").fetchone()
    observe(
        database_server.dsn(role),
        kind,
        slots,
        Path("sql"),
        "" if kind == "management" else "shared",
    )
    with psycopg.connect(database_server.dsn(role)) as connection:
        assert connection.execute("SELECT * FROM demo_metadata.schema_version").fetchone() == before


def test_bootstrap_runs_with_nonsuperuser_setup(database_server):
    admin = os.environ["TEST_POSTGRES_DSN"]
    setup = "plane_setup_" + uuid4().hex[:12]
    database = "plane_setup_db_" + uuid4().hex[:12]
    password = "disposable-setup-password-not-a-deployed-secret"
    roles = {"cp_api", "cp_reconciler", "dp_reconciler"}
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN CREATEDB CREATEROLE INHERIT PASSWORD {}").format(
                sql.Identifier(setup), sql.Literal(password)
            )
        )
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(setup)
            )
        )
        # These already-created roles need the same administration authority that
        # the creator receives on a fresh managed PostgreSQL instance.
        for role in OWNERS | roles:
            connection.execute(
                sql.SQL("GRANT {} TO {} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE").format(
                    sql.Identifier(role), sql.Identifier(setup)
                )
            )
    dsn = make_conninfo(admin, dbname=database, user=setup, password=password)
    try:
        initialize(
            dsn,
            "control",
            {role: database_server.passwords[role] for role in roles},
            [],
            Path("sql"),
            "shared",
        )
        with psycopg.connect(admin) as connection:
            for owner in OWNERS:
                assert connection.execute(
                    "SELECT m.inherit_option,m.set_option FROM pg_auth_members m "
                    "JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member "
                    "WHERE r.rolname=%s AND u.rolname=%s",
                    (owner, setup),
                ).fetchone() == (False, False)
        runtime = make_conninfo(
            dsn, user="cp_reconciler", password=database_server.passwords["cp_reconciler"]
        )
        with psycopg.connect(runtime) as connection:
            assert (
                connection.execute(
                    "SELECT control.ensure_tenant('alpha',%s,'shared','hello')", (uuid4(),)
                ).fetchone()[0]
                == 1
            )
        initialize(
            dsn,
            "control",
            {role: database_server.passwords[role] for role in roles},
            [],
            Path("sql"),
            "shared",
        )
        with psycopg.connect(runtime) as connection:
            assert connection.execute(
                "SELECT schema_kind,version FROM demo_metadata.schema_version"
            ).fetchone() == ("control", SCHEMA_VERSION)
            assert (
                connection.execute(
                    "SELECT count(*) FROM control.tenant_config WHERE tenant_id='alpha'"
                ).fetchone()[0]
                == 1
            )
        with psycopg.connect(make_conninfo(admin, dbname=database)) as connection:
            connection.execute("ALTER TABLE control.tenant_config DISABLE TRIGGER immutable_tenant")
        with pytest.raises(ValueError, match="database_schema_contract_changed"):
            initialize(
                dsn,
                "control",
                {role: database_server.passwords[role] for role in roles},
                [],
                Path("sql"),
                "shared",
            )
        with psycopg.connect(make_conninfo(admin, dbname=database)) as connection:
            connection.execute("ALTER TABLE control.tenant_config ENABLE TRIGGER immutable_tenant")
            connection.execute("DROP INDEX control.unique_data_report")
            connection.execute(
                "CREATE INDEX unique_data_report ON control.events(onboarding_id,version,type) "
                "WHERE source='data'"
            )
        with pytest.raises(ValueError, match="database_schema_contract_changed"):
            initialize(
                dsn,
                "control",
                {role: database_server.passwords[role] for role in roles},
                [],
                Path("sql"),
                "shared",
            )
        with psycopg.connect(make_conninfo(admin, dbname=database), autocommit=True) as connection:
            connection.execute("DROP INDEX control.unique_data_report")
            connection.execute(
                "INSERT INTO control.events(tenant_id,onboarding_id,pair_id,source,type,version)"
                "SELECT tenant_id,onboarding_id,pair_id,'data','config_applied',1 "
                "FROM control.tenant_config CROSS JOIN generate_series(1,2)"
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                connection.execute(
                    "CREATE UNIQUE INDEX CONCURRENTLY unique_data_report "
                    "ON control.events(onboarding_id,version,type) WHERE source='data'"
                )
            assert connection.execute(
                "SELECT indisvalid FROM pg_index "
                "WHERE indexrelid='control.unique_data_report'::regclass"
            ).fetchone() == (False,)
        with pytest.raises(ValueError, match="database_schema_contract_changed"):
            initialize(
                dsn,
                "control",
                {role: database_server.passwords[role] for role in roles},
                [],
                Path("sql"),
                "shared",
            )
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(setup)))
