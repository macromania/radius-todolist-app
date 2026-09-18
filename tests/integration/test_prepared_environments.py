import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from plane_demo.management.api import create_app
from plane_demo.management.providers.environment_registry import EnvironmentRegistry
from plane_demo.management.provisioning import ProvisioningError
from plane_demo.setup.bootstrap import OWNERS, initialize
from plane_demo.shared.settings import Settings

pytestmark = pytest.mark.integration
KEY = "synthetic-api-key-not-a-deployed-credential"


@pytest.fixture
def prepared(database_server):
    admin = os.environ["TEST_POSTGRES_DSN"]
    suffix = uuid4().hex[:10]
    setup, database, pair = f"setup_{suffix}", f"prepared_{suffix}", f"isolated-{suffix}"
    role = "cp_" + pair.replace("-", "_")
    password = "synthetic-administrative-password-" + suffix
    roles = {"mgmt_api", "mgmt_provisioner", "cp_shared"}
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN CREATEDB CREATEROLE INHERIT PASSWORD {}").format(
                sql.Identifier(setup),
                sql.Literal(password),
            )
        )
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database),
                sql.Identifier(setup),
            )
        )
        for name in OWNERS | roles:
            connection.execute(
                sql.SQL("GRANT {} TO {} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE").format(
                    sql.Identifier(name),
                    sql.Identifier(setup),
                )
            )
    dsn = make_conninfo(admin, dbname=database, user=setup, password=password)
    try:
        initialize(
            dsn,
            "management",
            {name: database_server.passwords[name] for name in roles},
            [{"pair_id": "shared", "reporting_role": "cp_shared"}],
            Path("sql"),
            admission_mode="prepared",
        )
        config = SimpleNamespace(
            prepared_environments=True,
            pair_slots=[
                {"pair_id": "shared", "reporting_role": "cp_shared"},
                {"pair_id": pair, "reporting_role": role},
            ],
            foundation={},
        )
        registry = EnvironmentRegistry(config, dsn, lambda: None, schema_directory=Path("sql"))
        api_dsn = make_conninfo(
            dsn, user="mgmt_api", password=database_server.passwords["mgmt_api"]
        )
        with TestClient(
            create_app(Settings(demo_key=KEY, management_dsn=api_dsn)), headers={"X-Demo-Key": KEY}
        ) as client:
            yield registry, client, pair, role, dsn, api_dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(setup)))


def test_prepared_admission_only_assigns_ready_capacity_and_never_queues_work(prepared):
    registry, client, pair, _, _, _ = prepared
    shared = {"tenant_id": "shared-a", "isolation": "shared", "initial_message": "alpha"}
    assert client.post("/tenants", json=shared).status_code == 503
    assert client.get("/tenants/shared-a").status_code == 404
    registry.available("shared")
    accepted = client.post("/tenants", json=shared)
    assert accepted.status_code == 202
    operation = client.get(accepted.json()["operation_url"]).json()
    assert (operation["status"], operation["stage"]) == ("succeeded", "available")
    assert client.get("/tenants/shared-a").json()["onboarding_status"] == "pending"
    assert client.post("/tenants", json=shared).status_code == 409
    isolated = {**shared, "tenant_id": "private-a", "isolation": "isolated"}
    assert client.post("/tenants", json=isolated).status_code == 503
    registry.register(pair, "synthetic-reporting-password-" + "x" * 32)
    assert client.post("/tenants", json=isolated).status_code == 503
    registry.available(pair)
    assert client.post("/tenants", json=isolated).status_code == 202
    assert client.post("/tenants", json={**isolated, "tenant_id": "private-b"}).status_code == 503
    with pytest.raises(ProvisioningError, match="environment_has_tenants"):
        registry.retire(pair)


def test_registration_preserves_existing_metadata_and_enforces_reporting_isolation(prepared):
    registry, client, pair, role, admin_dsn, api_dsn = prepared
    registry.available("shared")
    assert (
        client.post(
            "/tenants",
            json={
                "tenant_id": "shared-a",
                "isolation": "shared",
                "initial_message": "alpha",
            },
        ).status_code
        == 202
    )
    password = "synthetic-reporting-password-" + "x" * 32
    registry.register(pair, password)
    assert {row["pair_id"] for row in registry.observe()} == {"shared", pair}
    registry.register(pair, password)
    with psycopg.connect(make_conninfo(admin_dsn, user=role, password=password)) as connection:
        assert connection.execute("SELECT tenant_id FROM management.tenants").fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE management.pairs SET stage='available'")
    with psycopg.connect(api_dsn) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE management.admission_settings SET mode='on_demand'")
    registry.retire(pair)
    assert (
        client.post(
            "/tenants",
            json={
                "tenant_id": "isolated-a",
                "isolation": "isolated",
                "initial_message": "alpha",
            },
        ).status_code
        == 503
    )
    registry.observe()


def test_concurrent_isolated_admissions_cannot_assign_one_pair_twice(prepared):
    registry, _, pair, _, _, api_dsn = prepared
    registry.register(pair, "synthetic-concurrent-password-" + "x" * 32)
    registry.available(pair)

    def accept(name):
        try:
            with psycopg.connect(api_dsn) as connection:
                connection.execute(
                    "SELECT management.accept_tenant(%s,'isolated','alpha')", (name,)
                )
            return "accepted"
        except psycopg.Error as error:
            return error.sqlstate

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, ["private-one", "private-two"]))
    assert sorted(results) == ["PT507", "accepted"]
