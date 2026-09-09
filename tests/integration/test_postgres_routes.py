from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest

from plane_demo.control.reconciler import run_once

pytestmark = pytest.mark.integration


def test_duplicate_tenant_does_not_provision(databases, management_client):
    accepted = databases.create()
    duplicate = management_client.post(
        "/tenants",
        json={
            "tenant_id": "alpha",
            "isolation": "isolated",
            "initial_message": "different",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.headers["Location"] == accepted["status_url"]
    with psycopg.connect(databases.management_admin) as connection:
        assert connection.execute("SELECT count(*) FROM management.operations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM management.events").fetchone()[0] == 1
        assert connection.execute(
            "SELECT isolation,initial_message FROM management.tenants"
        ).fetchone() == ("shared", "alpha-initial")


def test_busy_returns_retry_without_creating_tenant(databases, management_client):
    databases.create()
    response = management_client.post(
        "/tenants",
        json={
            "tenant_id": "beta",
            "isolation": "shared",
            "initial_message": "",
        },
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert management_client.get("/tenants/beta").status_code == 404


def test_concurrent_acceptance_is_serialized(databases):
    def submit(tenant):
        try:
            with psycopg.connect(databases.dsn("mgmt_api")) as connection:
                connection.execute(
                    "SELECT management.accept_tenant(%s,'isolated','initial')", (tenant,)
                )
            return "accepted"
        except psycopg.Error as error:
            return error.sqlstate

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["alpha", "beta"]))
    assert sorted(results) == ["PT503", "accepted"]


def test_atomic_isolated_slot_assignment_is_extensible(databases, management_client):
    for tenant in ["alpha", "beta"]:
        databases.create(tenant, "isolated")
        databases.finish(tenant)
    first = management_client.get("/tenants/alpha").json()
    second = management_client.get("/tenants/beta").json()
    assert first["pair_id"] == "isolated-1"
    assert second["pair_id"] == "isolated-2"
    failed = management_client.post(
        "/tenants",
        json={
            "tenant_id": "gamma",
            "isolation": "isolated",
            "initial_message": "x",
        },
    )
    assert failed.status_code == 503
    assert failed.json()["detail"] == "allocation_unavailable"
    assert management_client.get("/tenants/gamma").status_code == 404


def test_management_ready_does_not_wait_for_data(databases, management_client, control_client):
    databases.create()
    databases.finish("alpha")
    assert management_client.get("/tenants/alpha").json()["onboarding_status"] == "pending"
    result = run_once(databases.settings("control_reconciler"))
    assert result.succeeded == 1
    management = management_client.get("/tenants/alpha").json()
    assert management["onboarding_status"] == "ready"
    assert management["control_record"]["status"] == "created"
    assert "data_config" not in management
    assert control_client.get("/tenants/alpha").json()["data_config"]["status"] == "pending"


def test_control_poll_preserves_updated_configuration(databases, control_client):
    databases.create()
    databases.finish("alpha")
    settings = databases.settings("control_reconciler")
    assert run_once(settings).succeeded == 1
    response = control_client.put("/tenants/alpha/configuration", json={"message": "new"})
    assert response.json()["desired"]["version"] == 2
    for _ in range(3):
        assert run_once(settings).succeeded == 1
    status = control_client.get("/tenants/alpha").json()
    assert status["desired"] == {"message": "new", "version": 2}
    assert [event["type"] for event in status["timeline"]] == [
        "configuration_created",
        "configuration_updated",
    ]
    with psycopg.connect(databases.management_admin) as connection:
        assert connection.execute("SELECT count(*) FROM management.events").fetchone()[0] == 2


def test_onboarding_mismatch_is_rejected_without_overwrite(databases, control_client):
    databases.create()
    databases.finish("alpha")
    run_once(databases.settings("control_reconciler"))
    with pytest.raises(psycopg.Error) as error:
        with psycopg.connect(databases.dsn("cp_reconciler")) as connection:
            connection.execute(
                "SELECT control.ensure_tenant('alpha',%s,'shared','overwritten')", (uuid4(),)
            )
    assert error.value.sqlstate == "PT409"
    assert control_client.get("/tenants/alpha").json()["desired"]["message"] == "alpha-initial"


def test_unknown_tenant_configuration_update_is_not_onboarding(control_client):
    response = control_client.put("/tenants/missing/configuration", json={"message": "no"})
    assert response.status_code == 404


def test_concurrent_control_updates_increment_atomically(databases):
    databases.create()
    databases.finish("alpha")
    run_once(databases.settings("control_reconciler"))

    def update(index):
        with psycopg.connect(databases.dsn("cp_api")) as connection:
            return connection.execute(
                "SELECT control.update_configuration('alpha',%s)", (str(index),)
            ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sorted(executor.map(update, range(8))) == list(range(2, 10))
