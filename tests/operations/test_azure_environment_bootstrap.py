import copy
import json
import sys
from unittest.mock import MagicMock

import pytest

from plane_demo.management.providers.azure_environments import EnvironmentError
from plane_demo.management.providers.identity import (
    AZURE_DEFAULT_SLOTS,
    RESOURCE_SIZING,
    DemoConfig,
)
from scripts.operations.azure import bootstrap as subject
from scripts.operations.azure.environment_operator import EnvironmentOperator

CONFIG = DemoConfig("azure", "sample", "demo", "11111111-1111-1111-1111-111111111111", "eastus2")
REVISION = "a" * 40


@pytest.fixture
def bootstrap(monkeypatch):
    document = {
        "foundation": {
            "nodeCount": 2,
            "nodeVmSize": "Standard_D4as_v7",
            "environmentMode": "prepared-v1",
            "postgresSkuName": "Standard_D2ads_v5",
            "postgresSkuTier": "GeneralPurpose",
            "resourceSizingVersion": 1,
            **{field: choices[0] for _, field, choices in RESOURCE_SIZING.values()},
        },
        "allocations": [{"slot": slot} for slot in AZURE_DEFAULT_SLOTS],
    }
    state = {"base": True, "calls": [], "document": document}
    monkeypatch.setenv("CONFIRM_AZURE", "yes")
    monkeypatch.setattr(subject, "load_config", lambda _: CONFIG)
    monkeypatch.setattr(subject, "check_source", lambda _: REVISION)
    monkeypatch.setattr(
        subject,
        "base_deployment",
        lambda _: (
            {
                "properties": {
                    "provisioningState": "Succeeded",
                    "outputs": {key: {"value": value} for key, value in document.items()},
                }
            }
            if state["base"]
            else None
        ),
    )
    monkeypatch.setattr(subject, "catalog", lambda *_: state["document"])
    monkeypatch.setattr(subject, "artifacts", lambda **kwargs: {"source_revision": REVISION})
    monkeypatch.setattr(subject, "operator_config", lambda _, doc, __: doc)

    def execute(argv, **kwargs):
        state["calls"].append(tuple(argv))
        if argv[0] == "bash" and argv[1].endswith("foundation.sh"):
            state["base"] = True
        return ""

    monkeypatch.setattr(subject, "execute", execute)

    class Operator:
        def __init__(self, *_):
            state["operator"] = self
            self.jobs = []
            self.release_safe = True

        def ensure_management_radius(self):
            state["calls"].append(("verify-radius",))

        def acquire(self):
            state["calls"].append(("acquire",))

        def run_job(self, config, name):
            self.jobs.append(name)

        def get(self, *_):
            return {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}

        def require_default_ready(self, configuration):
            state["calls"].append(("verify-default-inputs",))

        def owned(self, value):
            return value

        def reserve(self, pair, environments):
            return {"pairId": pair, "allocationStart": 3}, True

        def isolated_foundation(self, pair, record, *, fresh):
            state["calls"].append(("isolated-foundation", pair, record["allocationStart"]))
            state["document"] = copy.deepcopy(document)
            state["document"]["allocations"] += [
                {"slot": f"{pair}-control"},
                {"slot": f"{pair}-data"},
            ]

        def verify_ready(self, config, pair):
            state["calls"].append(("verify-ready", pair))

        def mark(self, pair, state_name):
            state["calls"].append(("mark", pair, state_name))

        def release(self):
            state["calls"].append(("release",))

    monkeypatch.setattr(subject, "EnvironmentOperator", Operator)
    discovery = MagicMock()
    discovery.available.return_value = ({}, {})
    monkeypatch.setattr(subject.node_sizes, "Discovery", lambda _: discovery)
    monkeypatch.setattr(subject.node_sizes, "select_size", MagicMock())
    monkeypatch.setattr(subject.postgres_sizes, "Discovery", lambda _: discovery)
    monkeypatch.setattr(subject.postgres_sizes, "select_size", MagicMock())
    monkeypatch.setattr(subject.resource_sizes, "Discovery", lambda _: discovery)
    monkeypatch.setattr(subject.resource_sizes, "select", MagicMock())
    return state


def test_default_entrypoint_builds_three_cluster_capacity_before_reporting_success(
    bootstrap, monkeypatch, capsys
):
    bootstrap["base"] = False
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    assert subject.main() == 0
    assert bootstrap["operator"].jobs == ["deploy-management", "prepare-shared"]
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err.count("\nAzure bootstrap\n") == 1
    assert "demo | eastus2 | shared" in output.err
    for number, title in enumerate(
        (
            "Prepare foundation",
            "Build images and Recipes",
            "Deploy management",
            "Prepare shared environment",
        ),
        1,
    ):
        assert output.err.count(f"\n{number} / 4  {title}\n") == 1
    assert output.err.count("Next:") == 3
    assert "\n\n\n" not in output.err
    assert result["slots"] == list(AZURE_DEFAULT_SLOTS)
    assert result["status"] == "environment_prepared"
    assert any(
        call[0] == "bash" and call[1].endswith("foundation.sh") for call in bootstrap["calls"]
    )
    assert bootstrap["calls"][-2:] == [("verify-ready", "shared"), ("release",)]


def test_existing_default_foundation_is_observed_without_redeployment(bootstrap, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    assert subject.main() == 0
    assert not any(call[0] == "bash" for call in bootstrap["calls"])


@pytest.mark.parametrize("profile", [None, True, 2, 1])
def test_old_or_malformed_sizing_profiles_stop_before_build_or_operator_job(
    bootstrap, monkeypatch, profile
):
    bootstrap["document"]["foundation"]["resourceSizingVersion"] = profile
    if profile == 1 and type(profile) is int:
        del bootstrap["document"]["foundation"]["redisSkuName"]
    artifacts = MagicMock()
    monkeypatch.setattr(subject, "artifacts", artifacts)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    with pytest.raises(EnvironmentError, match="sizing profile"):
        subject.main()
    artifacts.assert_not_called()
    assert bootstrap["calls"] == []


def test_readiness_failure_after_completed_jobs_releases_the_workstation_lease(
    bootstrap, monkeypatch
):
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    original = subject.EnvironmentOperator

    class Unhealthy(original):
        def verify_ready(self, config, pair):
            raise EnvironmentError("temporary health failure")

    monkeypatch.setattr(subject, "EnvironmentOperator", Unhealthy)
    with pytest.raises(EnvironmentError, match="health"):
        subject.main()
    assert bootstrap["calls"][-1] == ("release",)


def test_failed_artifact_phase_never_announces_future_deployment_phases(
    bootstrap, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    monkeypatch.setattr(
        subject, "artifacts", MagicMock(side_effect=EnvironmentError("synthetic artifact failure"))
    )
    with pytest.raises(EnvironmentError, match="artifact failure"):
        subject.main()
    output = capsys.readouterr()
    assert "\n2 / 4  Build images and Recipes\n" in output.err
    assert "\n3 / 4 " not in output.err and "\n4 / 4 " not in output.err
    assert "is prepared for tenant admission" not in output.err
    assert "operator" not in bootstrap


def test_management_radius_phase_is_independent_of_arm_submission(monkeypatch, tmp_path):
    operator = object.__new__(EnvironmentOperator)
    operator.guard = MagicMock()
    operator.management_radius_ready = MagicMock(side_effect=[False, True])
    operator.get = MagicMock(return_value=None)
    operator.record_command = MagicMock()
    operator.token, operator.lease_uid = "owned", "lease"
    operator.labels = {}
    operator.operator_namespace = "sample-demo-azure-operations"
    operator.workspace, operator.context = tmp_path, "sample-demo-azure-management"
    operator.kubeconfig = str(tmp_path / "kubeconfig")
    operator.base = {
        "foundation": {"tenantId": "tenant"},
        "allocations": [{"slot": "management", "identities": {"radius": {"clientId": "radius"}}}],
    }
    run = MagicMock(
        return_value=json.dumps(
            {
                "context": operator.context,
                "workload_identity_verified": True,
            }
        )
    )
    monkeypatch.setattr("scripts.operations.azure.environment_operator.execute", run)
    operator.ensure_management_radius()
    assert operator.release_safe is True
    assert run.call_args.args[0][0] == "bash"
    assert run.call_args.args[0][1].endswith("install-radius.sh")
    assert not any(value == "az" for value in run.call_args.args[0])
    operator.management_radius_ready = MagicMock(return_value=True)
    operator.ensure_management_radius()
    assert run.call_count == 1


def test_interrupted_radius_installation_is_not_automatically_replayed():
    operator = object.__new__(EnvironmentOperator)
    operator.guard = lambda: None
    operator.management_radius_ready = lambda: False
    operator.get = lambda *a, **kw: {"metadata": {"name": "management-radius-installation"}}
    with pytest.raises(EnvironmentError, match="interrupted"):
        operator.ensure_management_radius()


def test_isolated_entrypoint_never_redeploys_default_foundation_or_applications(
    bootstrap, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--isolated", "blue"])
    assert subject.main() == 0
    assert bootstrap["operator"].jobs == ["prepare-isolated-blue"]
    assert ("isolated-foundation", "isolated-blue", 3) in bootstrap["calls"]
    assert not any(call[0] == "bash" for call in bootstrap["calls"])
    output = capsys.readouterr()
    assert json.loads(output.out)["slots"] == [
        *AZURE_DEFAULT_SLOTS,
        "isolated-blue-control",
        "isolated-blue-data",
    ]
    assert "demo | eastus2 | isolated-blue" in output.err
    assert "\n1 / 4  Verify default foundation\n" in output.err
    assert "\n3 / 4  Prepare isolated foundation\n" in output.err
    assert "\n4 / 4  Deploy isolated environment\n" in output.err
    assert "\n3 / 4  Deploy management\n" not in output.err
    subject.node_sizes.select_size.assert_called_once()
    assert subject.node_sizes.select_size.call_args.args[3].clusters == 5
    subject.resource_sizes.select.assert_called_once()
    assert (
        subject.resource_sizes.select.call_args.kwargs["existing"]
        == bootstrap["document"]["foundation"]
    )


def test_isolated_bootstrap_refuses_a_missing_default_instead_of_creating_one(
    bootstrap, monkeypatch
):
    bootstrap["base"] = False
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--isolated", "blue"])
    with pytest.raises(EnvironmentError, match="default environment"):
        subject.main()
    assert bootstrap["calls"] == []


def test_missing_confirmation_prevents_reads_and_writes(bootstrap, monkeypatch):
    monkeypatch.delenv("CONFIRM_AZURE")
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    with pytest.raises(EnvironmentError, match="CONFIRM_AZURE"):
        subject.main()
    assert bootstrap["calls"] == []


def test_lease_changes_use_uid_and_resource_version_preconditions():
    operator = object.__new__(EnvironmentOperator)
    operator.lease_uid = "owned-uid"
    operator.owned = lambda value, **kw: value
    operator.record_command = MagicMock(return_value="{}")
    lease = {
        "metadata": {"uid": "owned-uid", "resourceVersion": "42"},
        "spec": {"holderIdentity": "before"},
    }
    operator.change_holder(lease, "after")
    arguments = operator.record_command.call_args.args
    patch = json.loads(arguments[arguments.index("-p") + 1])
    assert [item["path"] for item in patch[:3]] == [
        "/metadata/uid",
        "/metadata/resourceVersion",
        "/spec/holderIdentity",
    ]
    assert patch[-1] == {"op": "replace", "path": "/spec/holderIdentity", "value": "after"}


def test_interrupted_isolated_reservation_cannot_resubmit_arm(monkeypatch, tmp_path):
    operator = object.__new__(EnvironmentOperator)
    operator.identity = CONFIG
    operator.guard = lambda: None
    operator.workspace = tmp_path
    monkeypatch.setattr("scripts.operations.azure.environment_operator.azure", lambda *args: [])
    with pytest.raises(EnvironmentError, match="explicit recovery"):
        operator.isolated_foundation("isolated-blue", {"allocationStart": 3}, fresh=False)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "collision", ["group", "role", "subnet", "address", "foreign-network", None]
)
def test_new_foundation_rejects_existing_or_overlapping_resources_before_submission(
    monkeypatch, collision
):
    operator = object.__new__(EnvironmentOperator)
    operator.identity = CONFIG
    operator.guard = lambda: None
    vnet = (
        f"/subscriptions/{CONFIG.subscription}/resourceGroups/rg-{CONFIG.stem}-platform/"
        f"providers/Microsoft.Network/virtualNetworks/vnet-{CONFIG.stem}"
    )
    operator.base = {"foundation": {"virtualNetworkId": vnet}}
    calls = []

    def read(config, *args):
        calls.append(args)
        if args[:2] == ("group", "list"):
            return (
                [{"name": CONFIG.plane_group("isolated-blue-control")}]
                if collision == "group"
                else []
            )
        if args[:3] == ("role", "definition", "list"):
            return [{"id": "existing"}] if collision == "role" else []
        assert args[:3] == ("network", "vnet", "show")
        subnet = {"name": "snet-management-nodes", "addressPrefix": "10.64.0.0/24"}
        if collision == "subnet":
            subnet["name"] = "snet-isolated-blue-control-nodes"
        if collision == "address":
            subnet["addressPrefix"] = "10.64.3.0/24"
        return {
            "id": vnet,
            "provisioningState": "Succeeded",
            "addressSpace": {"addressPrefixes": ["10.64.0.0/16"]},
            "tags": {
                "project": "foreign" if collision == "foreign-network" else CONFIG.project,
                "deployment": CONFIG.deployment,
                "environment": "azure",
                "managedBy": "radius-todolist-app",
            },
            "subnets": [subnet],
        }

    monkeypatch.setattr("scripts.operations.azure.environment_operator.azure", read)
    if collision:
        with pytest.raises(EnvironmentError):
            operator.check_new_foundation("isolated-blue", 3)
    else:
        operator.check_new_foundation("isolated-blue", 3)
    assert all("create" not in call and "update" not in call for call in calls)
