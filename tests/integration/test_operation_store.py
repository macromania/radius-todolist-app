from uuid import uuid4

import psycopg
import pytest

from plane_demo.shared.db import ProvisionerAlreadyRunning, provisioner_session

pytestmark = pytest.mark.integration


def test_provisioner_singleton_lock_is_session_scoped(databases):
    with provisioner_session(databases.dsn("mgmt_provisioner")):
        with pytest.raises(ProvisionerAlreadyRunning):
            with provisioner_session(databases.dsn("mgmt_provisioner")):
                pytest.fail("second provisioner acquired the singleton lock")
    with provisioner_session(databases.dsn("mgmt_provisioner")) as store:
        assert store.claim_pending() is None


def test_claim_stage_and_complete_are_transactional(databases, management_client):
    created = databases.create()
    with provisioner_session(databases.dsn("mgmt_provisioner")) as store:
        assert store.interrupt_running() == 0
        operation = store.claim_pending()
        assert str(operation.operation_id) == created["operation_id"]
        assert operation.initial_message == "alpha-initial"
        assert operation.isolation == "shared"
        assert operation.pair_id == "shared"
        assert store.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        assert store.claim_pending() is None
        store.observe(operation.operation_id, "control-cluster")
        store.observe(operation.operation_id, "control-cluster")
        store.complete(
            operation.operation_id,
            control_cluster_id="control-cluster-id",
            data_cluster_id="data-cluster-id",
            control_url="https://control.example.test",
            data_url="https://data.example.test",
        )
    status = management_client.get("/tenants/alpha").json()
    assert status["provisioning_status"] == "succeeded"
    assert status["onboarding_status"] == "pending"
    assert status["control_url"] == "https://control.example.test"
    assert len(status["timeline"]) == 4
    assert [event["stage"] for event in status["timeline"]] == [
        None,
        "starting",
        "control-cluster",
        "available",
    ]


def test_restart_marks_running_interrupted_but_never_replays(databases, management_client):
    databases.create()
    with provisioner_session(databases.dsn("mgmt_provisioner")) as store:
        operation = store.claim_pending()
        store.observe(operation.operation_id, "data-cluster")
    with provisioner_session(databases.dsn("mgmt_provisioner")) as restarted:
        assert restarted.interrupt_running() == 1
        assert restarted.interrupt_running() == 0
        assert restarted.claim_pending() is None
    status = management_client.get("/tenants/alpha").json()
    assert status["provisioning_status"] == "interrupted"
    assert status["provisioning_stage"] == "data-cluster"
    assert status["error_code"] == "provisioner_restarted"
    assert status["onboarding_status"] == "pending"
    databases.create("beta")
    with provisioner_session(databases.dsn("mgmt_provisioner")) as restarted:
        assert restarted.interrupt_running() == 0
        assert restarted.claim_pending().tenant_id == "beta"


def test_failed_operation_cannot_be_resumed(databases, management_client):
    databases.create()
    with provisioner_session(databases.dsn("mgmt_provisioner")) as store:
        operation = store.claim_pending()
        store.observe(
            operation.operation_id, "control-cluster", status="failed", error_code="cluster_failed"
        )
        with pytest.raises(ValueError, match="not_running"):
            store.observe(operation.operation_id, "starting")
    assert management_client.get("/tenants/alpha").json()["provisioning_status"] == "failed"


def test_invalid_observation_rolls_back_without_an_event(databases, management_client):
    databases.create()
    with provisioner_session(databases.dsn("mgmt_provisioner")) as store:
        operation = store.claim_pending()
        with pytest.raises(psycopg.errors.CheckViolation):
            store.observe(operation.operation_id, "not a stable identifier")
        assert store.connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with pytest.raises(ValueError, match="operation_not_found"):
            store.observe(uuid4(), "starting")
    status = management_client.get("/tenants/alpha").json()
    assert status["provisioning_stage"] == "starting"
    assert len(status["timeline"]) == 2


def test_completion_rejects_credential_bearing_endpoints(databases):
    databases.create()
    with provisioner_session(databases.dsn("mgmt_provisioner")) as store:
        operation = store.claim_pending()
        with pytest.raises(ValueError, match="without credentials"):
            store.complete(
                operation.operation_id,
                control_cluster_id="control",
                data_cluster_id="data",
                control_url="https://user:secret@control.example.test",
                data_url="https://data.example.test",
            )
