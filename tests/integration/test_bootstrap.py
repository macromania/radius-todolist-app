import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from plane_demo.setup.bootstrap import OWNERS, initialize

pytestmark = pytest.mark.integration


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
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(setup)))
