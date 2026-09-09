from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest

from plane_demo.control_reconciler import run_once

pytestmark = pytest.mark.integration


def onboard(databases):
    databases.create()
    databases.finish("alpha")
    assert run_once(databases.settings("control_reconciler")).succeeded == 1
    with psycopg.connect(databases.control_admin) as connection:
        return connection.execute(
            "SELECT onboarding_id FROM control.tenant_config WHERE tenant_id='alpha'"
        ).fetchone()[0]


def report(databases, onboarding, version=1, transition="config_applied", error=None):
    with psycopg.connect(databases.dsn("dp_reconciler")) as connection:
        return connection.execute(
            "SELECT control.report_data('alpha',%s,%s,%s,%s)",
            (onboarding, version, transition, error),
        ).fetchone()[0]


def test_repeated_report_does_not_duplicate_event(databases):
    onboarding = onboard(databases)
    ids = [report(databases, onboarding) for _ in range(4)]
    assert len(set(ids)) == 1
    with psycopg.connect(databases.control_admin) as connection:
        assert connection.execute("SELECT count(*) FROM control.events").fetchone()[0] == 2


def test_same_version_failure_cannot_downgrade_success(databases, control_client):
    onboarding = onboard(databases)
    event_id = report(databases, onboarding)
    assert (
        report(databases, onboarding, transition="config_apply_failed", error="write_failed")
        == event_id
    )
    status = control_client.get("/tenants/alpha").json()
    assert status["data_config"]["status"] == "applied"
    assert len(status["timeline"]) == 2


def test_failure_then_success_is_allowed(databases, control_client):
    onboarding = onboard(databases)
    failed = report(databases, onboarding, transition="config_apply_failed", error="write_failed")
    assert control_client.get("/tenants/alpha").json()["data_config"]["status"] == "failed"
    success = report(databases, onboarding)
    assert failed < success
    assert control_client.get("/tenants/alpha").json()["data_config"]["status"] == "applied"


def test_changed_report_replay_is_rejected(databases):
    onboarding = onboard(databases)
    report(databases, onboarding, transition="config_apply_failed", error="write_failed")
    with pytest.raises(psycopg.Error) as error:
        report(databases, onboarding, transition="config_apply_failed", error="different")
    assert error.value.sqlstate == "PT409"


def test_out_of_order_report_does_not_regress_version(databases, control_client):
    onboarding = onboard(databases)
    control_client.put("/tenants/alpha/configuration", json={"message": "v2"})
    report(databases, onboarding, version=2)
    before = control_client.get("/tenants/alpha").json()["data_config"]
    report(databases, onboarding, version=1)
    after = control_client.get("/tenants/alpha").json()["data_config"]
    assert after["last_applied_version"] == 2
    assert after["reported_at"] == before["reported_at"]
    control_client.put("/tenants/alpha/configuration", json={"message": "v3"})
    assert control_client.get("/tenants/alpha").json()["data_config"]["status"] == "pending"


def test_late_old_success_does_not_hide_current_version_failure(databases, control_client):
    onboarding = onboard(databases)
    updated = control_client.put("/tenants/alpha/configuration", json={"message": "v2"})
    assert updated.json()["desired"]["version"] == 2
    failed = report(
        databases, onboarding, version=2, transition="config_apply_failed", error="write_failed"
    )
    assert control_client.get("/tenants/alpha").json()["data_config"]["status"] == "failed"
    assert report(databases, onboarding, version=1) > failed
    current = control_client.get("/tenants/alpha").json()["data_config"]
    assert current["status"] == "failed"
    assert current["last_applied_version"] == 1
    assert current["last_report"]["type"] == "config_applied"
    assert current["last_report"]["version"] == 1
    assert current["reported_at"] == current["last_report"]["received_at"]
    report(databases, onboarding, version=2)
    assert control_client.get("/tenants/alpha").json()["data_config"]["status"] == "applied"


@pytest.mark.parametrize("variant", ["future", "onboarding", "null", "invalid_error"])
def test_report_rejects_invalid_target_or_content(databases, variant):
    onboarding = onboard(databases)
    with pytest.raises(psycopg.Error) as error:
        report(
            databases,
            uuid4() if variant == "onboarding" else onboarding,
            version=2 if variant == "future" else None if variant == "null" else 1,
            error="unexpected" if variant == "invalid_error" else None,
        )
    assert error.value.sqlstate == "PT422"


def test_pair_role_cannot_report_for_other_pair(databases):
    databases.create("alpha", "shared")
    databases.finish("alpha")
    databases.create("beta", "isolated")
    with psycopg.connect(databases.management_admin) as connection:
        onboarding = connection.execute(
            "SELECT onboarding_id FROM management.tenants WHERE tenant_id='beta'"
        ).fetchone()[0]
    with psycopg.connect(databases.dsn("cp_shared")) as connection:
        connection.execute("SELECT set_config('plane.pair_id','isolated-1',false)")
        assert connection.execute("SELECT tenant_id FROM management.tenants").fetchall() == [
            ("alpha",)
        ]
        with pytest.raises(psycopg.Error) as error:
            connection.execute(
                "SELECT management.report_control('beta',%s,1,'control_record_created')",
                (onboarding,),
            )
    assert error.value.sqlstate == "PT422"


@pytest.mark.parametrize(
    ("role", "statement"),
    [
        ("cp_shared", "UPDATE management.tenants SET initial_message='bad'"),
        ("cp_shared", "UPDATE management.login_pairs SET pair_id='isolated-1'"),
        ("cp_shared", "SET ROLE cp_isolated_1"),
        ("cp_shared", "SET ROLE plane_owner"),
        ("cp_shared", "ALTER TABLE management.tenants DISABLE ROW LEVEL SECURITY"),
        ("cp_shared", "INSERT INTO management.events DEFAULT VALUES"),
        ("mgmt_api", "UPDATE management.operations SET status='succeeded'"),
        ("dp_reconciler", "UPDATE control.tenant_config SET message='bad'"),
        ("dp_reconciler", "UPDATE control.login_pairs SET pair_id='isolated-1'"),
        ("dp_reconciler", "INSERT INTO control.events DEFAULT VALUES"),
        ("dp_reconciler", "SELECT control.update_configuration('alpha','bad')"),
        ("cp_api", "SELECT control.report_data('alpha',gen_random_uuid(),1,'config_applied')"),
    ],
)
def test_runtime_role_cannot_bypass_rls(databases, role, statement):
    with pytest.raises(psycopg.Error) as error:
        with psycopg.connect(databases.dsn(role)) as connection:
            connection.execute(statement)
    assert error.value.sqlstate == "42501"


def test_row_security_off_does_not_bypass_pair_filter(databases):
    databases.create()
    with pytest.raises(psycopg.Error) as error:
        with psycopg.connect(databases.dsn("cp_shared")) as connection:
            connection.execute("SET row_security=off")
            connection.execute("SELECT * FROM management.tenants")
    assert error.value.sqlstate == "42501"


def test_provisioner_row_lock_privilege_cannot_change_onboarding(databases):
    databases.create()
    with psycopg.connect(databases.dsn("mgmt_provisioner")) as connection:
        assert connection.execute(
            "SELECT tenant_id FROM management.tenants WHERE tenant_id='alpha' FOR UPDATE"
        ).fetchone() == ("alpha",)
    with pytest.raises(psycopg.Error) as error:
        with psycopg.connect(databases.dsn("mgmt_provisioner")) as connection:
            connection.execute(
                "UPDATE management.tenants SET onboarding_id=%s WHERE tenant_id='alpha'", (uuid4(),)
            )
    assert error.value.sqlstate == "42501"


def test_management_report_replay_failure_and_success_rules(databases, management_client):
    databases.create()
    with psycopg.connect(databases.management_admin) as connection:
        onboarding = connection.execute(
            "SELECT onboarding_id FROM management.tenants WHERE tenant_id='alpha'"
        ).fetchone()[0]

    def submit(transition, error=None):
        with psycopg.connect(databases.dsn("cp_shared")) as connection:
            return connection.execute(
                "SELECT management.report_control('alpha',%s,1,%s,%s)",
                (onboarding, transition, error),
            ).fetchone()[0]

    failed = submit("control_record_failed", "write_failed")
    assert submit("control_record_failed", "write_failed") == failed
    with pytest.raises(psycopg.Error) as error:
        submit("control_record_failed", "changed_error")
    assert error.value.sqlstate == "PT409"
    succeeded = submit("control_record_created")
    assert succeeded > failed
    assert management_client.get("/tenants/alpha").json()["onboarding_status"] == "ready"
    # A replay of the historical failure is idempotent, not a new downgrade.
    assert submit("control_record_failed", "write_failed") == failed
    assert management_client.get("/tenants/alpha").json()["onboarding_status"] == "ready"


def test_runtime_roles_are_nonowner_nonsuperuser_without_owner_membership(databases):
    with psycopg.connect(databases.management_admin) as connection:
        for role in databases.passwords:
            row = connection.execute(
                "SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,"
                "pg_has_role(rolname,'plane_owner','MEMBER') FROM pg_roles WHERE rolname=%s",
                (role,),
            ).fetchone()
            assert row == (False, False, False, False, False)
        assert connection.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='management.tenants'::regclass"
        ).fetchone() == (True, True)
        assert connection.execute(
            "SELECT has_function_privilege('public',"
            "'management.report_control(text,uuid,bigint,text,text)','EXECUTE')"
        ).fetchone() == (False,)


def test_report_racing_update_preserves_paginated_committed_order(databases, control_client):
    onboarding = onboard(databases)
    first = control_client.get("/tenants/alpha", params={"limit": 1}).json()["timeline"][0][
        "event_id"
    ]
    with psycopg.connect(databases.dsn("cp_api")) as writer:
        writer.execute("SELECT control.update_configuration('alpha','v2')")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(report, databases, onboarding)
            # The desired event is allocated but not committed. A reader must not skip it.
            assert (
                control_client.get("/tenants/alpha", params={"after_event_id": first}).json()[
                    "timeline"
                ]
                == []
            )
            writer.commit()
            report_id = future.result(timeout=10)
    timeline = []
    cursor = first
    while True:
        response = control_client.get(
            "/tenants/alpha", params={"after_event_id": cursor, "limit": 1}
        ).json()
        timeline.extend(response["timeline"])
        if response["next_after_event_id"] is None:
            break
        cursor = response["next_after_event_id"]
    assert [event["type"] for event in timeline] == ["configuration_updated", "config_applied"]
    assert timeline[-1]["event_id"] == report_id
