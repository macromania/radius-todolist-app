import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from plane_demo.control.api import create_app as control_app
from plane_demo.management.api import create_app as management_app
from plane_demo.setup.bootstrap import initialize
from plane_demo.shared.settings import Settings

KEY = "integration-test-key-not-a-deployed-credential"
HEADERS = {"X-Demo-Key": KEY}
SLOTS = [
    {"pair_id": "shared", "reporting_role": "cp_shared"},
    {"pair_id": "isolated-1", "reporting_role": "cp_isolated_1"},
    {"pair_id": "isolated-2", "reporting_role": "cp_isolated_2"},
]


@dataclass
class Databases:
    management_admin: str
    control_admin: str
    passwords: dict

    def dsn(self, role):
        admin = (
            self.management_admin
            if role.startswith(("mgmt_", "cp_shared", "cp_isolated"))
            else self.control_admin
        )
        return make_conninfo(admin, user=role, password=self.passwords[role])

    def settings(self, role):
        return Settings(
            demo_key=KEY,
            management_dsn=self.dsn("mgmt_api" if role == "management" else "cp_shared"),
            control_dsn=self.dsn(
                "cp_api"
                if role == "control"
                else "cp_reconciler"
                if role == "control_reconciler"
                else "dp_reconciler"
            ),
            pair_id="shared",
            project_id="integration",
            namespace="plane-demo-test",
        )

    def finish(self, tenant):
        with psycopg.connect(self.management_admin) as connection:
            connection.execute(
                "UPDATE management.operations SET status='succeeded',stage='available' "
                "WHERE tenant_id=%s",
                (tenant,),
            )
            connection.execute(
                "UPDATE management.pairs SET stage='available' WHERE pair_id="
                "(SELECT pair_id FROM management.tenants WHERE tenant_id=%s)",
                (tenant,),
            )

    def create(self, tenant="alpha", isolation="shared"):
        with TestClient(management_app(self.settings("management"))) as client:
            response = client.post(
                "/tenants",
                headers=HEADERS,
                json={
                    "tenant_id": tenant,
                    "isolation": isolation,
                    "initial_message": tenant + "-initial",
                },
            )
        assert response.status_code == 202, response.text
        return response.json()


@pytest.fixture(scope="session")
def database_server():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("TEST_ALLOW_DATABASE_CREATE") != "yes":
        pytest.skip("requires disposable TEST_POSTGRES_DSN and TEST_ALLOW_DATABASE_CREATE=yes")
    names = [f"plane_test_{kind}_{uuid4().hex[:12]}" for kind in ("management", "control")]
    passwords = {
        name: secrets.token_urlsafe(32)
        for name in (
            "mgmt_api",
            "mgmt_provisioner",
            "cp_shared",
            "cp_isolated_1",
            "cp_isolated_2",
            "cp_api",
            "cp_reconciler",
            "dp_reconciler",
        )
    }
    with psycopg.connect(dsn, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    management = make_conninfo(dsn, dbname=names[0])
    control = make_conninfo(dsn, dbname=names[1])
    try:
        initialize(
            management,
            "management",
            {
                key: value
                for key, value in passwords.items()
                if key.startswith(("mgmt_", "cp_shared", "cp_isolated"))
            },
            SLOTS,
            Path("sql"),
        )
        initialize(
            control,
            "control",
            {
                key: value
                for key, value in passwords.items()
                if key in {"cp_api", "cp_reconciler", "dp_reconciler"}
            },
            [],
            Path("sql"),
            "shared",
        )
        yield Databases(management, control, passwords)
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            for name in names:
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                )


@pytest.fixture
def databases(database_server):
    with psycopg.connect(database_server.management_admin) as connection:
        connection.execute("TRUNCATE management.events,management.operations,management.tenants")
        connection.execute("UPDATE management.pairs SET stage='allocated'")
    with psycopg.connect(database_server.control_admin) as connection:
        connection.execute("TRUNCATE control.events,control.tenant_config")
    return database_server


@pytest.fixture
def management_client(databases):
    with TestClient(management_app(databases.settings("management")), headers=HEADERS) as client:
        yield client


@pytest.fixture
def control_client(databases):
    with TestClient(control_app(databases.settings("control")), headers=HEADERS) as client:
        yield client
