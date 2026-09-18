import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from plane_demo.management.providers.azure_environments import EnvironmentError
from plane_demo.management.providers.identity import DemoConfig

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "scoped_environment_cleanup", ROOT / "scripts/operations/azure/clean-environment.py"
)
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)
CONFIG = DemoConfig("azure", "sample", "demo", "11111111-1111-1111-1111-111111111111", "eastus2")


@pytest.fixture
def cleanup(monkeypatch):
    slots = [
        "management",
        "shared-control",
        "shared-data",
        "isolated-blue-control",
        "isolated-blue-data",
    ]
    group = f"/subscriptions/{CONFIG.subscription}/resourceGroups/rg-{CONFIG.stem}-platform"
    document = {
        "foundation": {
            "virtualNetworkId": group + "/providers/Microsoft.Network/virtualNetworks/vnet",
            "virtualNetworkName": "vnet",
            "vaultId": group + "/providers/Microsoft.KeyVault/vaults/vault",
            "platformResourceGroup": f"rg-{CONFIG.stem}-platform",
        },
        "allocations": [
            {
                "slot": slot,
                "identities": {"radius": {"principalId": f"principal-{slot}"}},
            }
            for slot in slots
        ],
    }
    actions = []

    class Engine:
        groups = []
        inventories = {}
        assigned = False

        def discover_topology(self):
            pass

        def external_vault(self):
            pass

        def unexpected(self):
            pass

        def foundation(self):
            pass

        def role_state(self):
            return {}, [], []

        def clusters(self):
            return {slot: {} for slot in slots}

        def inventory(self, slot):
            return {
                "apps": [{"name": slot.rsplit("-", 1)[-1]}],
                "children": {name: {"id": name} for name in slots if name.startswith("isolated-")},
            }

        def check_faults(self, slot):
            pass

        def kube(self, slot, *args, namespace=None):
            assert slot == "management" and namespace == CONFIG.namespace("management")
            actions.append(("admission-probe",))
            return json.dumps({"assigned": self.assigned})

        def gid(self, group):
            return f"/subscriptions/{CONFIG.subscription}/resourceGroups/{group}"

        def current(self):
            pass

        def delete_app(self, slot, name):
            actions.append(("delete-app", slot, name))

        def child_apps_absent(self, slot):
            pass

        def delete_cluster_owner(self, slot, record):
            actions.append(("delete-cluster", slot))

        def child_absent(self, slot, record):
            pass

        def delete_group(self, name):
            actions.append(("delete-group", name))

        def az(self, *args, mutation=False):
            if args[:3] == ("role", "assignment", "list"):
                return []
            if args[:2] == ("group", "exists"):
                return False
            if args[:4] == ("network", "vnet", "subnet", "list"):
                return []
            actions.append(("azure", *args))

        def close(self):
            pass

    class Operator:
        release_safe = True

        def __init__(self, *args):
            pass

        def acquire(self, **kwargs):
            actions.append(("acquire",))

        def guard(self):
            pass

        def run_job(self, config, name):
            actions.append(("job", name))

        def mark(self, pair, state):
            actions.append(("mark", pair, state))

        def release(self):
            actions.append(("release",))

    engine = Engine()
    monkeypatch.setenv("CONFIRM_AZURE", "yes")
    monkeypatch.setattr(subject, "load_config", lambda _: CONFIG)
    monkeypatch.setattr(subject, "check_source", lambda _: "a" * 40)
    monkeypatch.setattr(
        subject,
        "base_deployment",
        lambda _: {
            "properties": {
                "provisioningState": "Succeeded",
                "outputs": {key: {"value": value} for key, value in document.items()},
            }
        },
    )
    monkeypatch.setattr(subject, "catalog", lambda *_: document)
    monkeypatch.setattr(subject, "cleanup_engine", lambda _: engine)
    monkeypatch.setattr(subject, "artifacts", lambda **kw: {})
    monkeypatch.setattr(subject, "operator_config", lambda *args: SimpleNamespace())
    monkeypatch.setattr(subject, "EnvironmentOperator", Operator)
    monkeypatch.setattr(subject, "azure", lambda *args: actions.append(("retired-marker",)))
    monkeypatch.setattr(sys, "argv", ["clean-environment.py", "--isolated", "blue"])
    return engine, actions


def test_preview_stores_verified_inventory_and_uses_explicit_management_namespace(cleanup, capsys):
    engine, actions = cleanup
    assert subject.main() == 0
    assert set(engine.inventories) == {"management", "isolated-blue-control", "isolated-blue-data"}
    assert actions == [("admission-probe",)]
    assert json.loads(capsys.readouterr().out)["status"] == "planned"


def test_bound_environment_stops_before_operator_jobs_or_deletion(cleanup):
    engine, actions = cleanup
    engine.assigned = True
    with pytest.raises(EnvironmentError, match="still has tenants"):
        subject.main()
    assert actions == [("admission-probe",)]


def test_execution_retires_before_deleting_only_the_selected_pair(cleanup, monkeypatch):
    _, actions = cleanup
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--execute"])
    assert subject.main() == 0
    retired = actions.index(("job", "retire-isolated-blue"))
    deletions = [action for action in actions if action[0].startswith("delete-")]
    assert deletions and all(actions.index(action) > retired for action in deletions)
    assert all("shared" not in str(action) for action in deletions)
    assert ("delete-app", "management", "management") not in deletions
    assert ("delete-app", "management", "cluster-isolated-blue-control") in deletions
    assert ("delete-app", "management", "cluster-isolated-blue-data") in deletions
    assert actions[-1] == ("release",)


def test_post_retirement_observation_failure_releases_lease_before_workspace_cleanup(
    cleanup, monkeypatch
):
    engine, actions = cleanup
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--execute"])

    def unavailable(*args):
        raise RuntimeError("Temporary observation failure")

    engine.child_apps_absent = unavailable
    with pytest.raises(RuntimeError, match="observation"):
        subject.main()
    assert ("job", "retire-isolated-blue") in actions
    assert actions[-1] == ("release",)


@pytest.fixture
def full_cleanup_module():
    spec = importlib.util.spec_from_file_location(
        "prepared_full_cleanup_tests", ROOT / "scripts/operations/clean-azure.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("state", ["missing-group", "missing-cluster", "present", "inconsistent"])
def test_full_cleanup_requires_positive_management_absence_before_skipping_lease(
    full_cleanup_module, state
):
    engine = object.__new__(full_cleanup_module.LiveAzureCleanup)
    engine.config = CONFIG
    engine.management_removed = False
    identifier = engine.cluster_id("management")
    cluster = {"id": identifier}
    native = {"id": identifier, "type": "Microsoft.ContainerService/managedClusters"}
    calls = []

    def azure(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("group", "exists"):
            return state != "missing-group"
        assert args[:2] == ("aks", "list")
        return [cluster] if state in {"present", "inconsistent"} else []

    engine.az = azure
    engine.group = lambda _: [native] if state == "present" else []
    if state == "inconsistent":
        with pytest.raises(full_cleanup_module.CleanupError, match="cannot be verified"):
            engine.management_cluster_present()
        assert not engine.management_removed
    else:
        assert engine.management_cluster_present() is (state == "present")
        assert engine.management_removed is (state != "present")
    if state == "missing-group":
        assert len(calls) == 1


def test_cleanup_run_path_does_not_open_kubernetes_after_management_absence(
    full_cleanup_module, monkeypatch
):
    engine = object.__new__(full_cleanup_module.LiveAzureCleanup)
    engine.execute = True
    engine.allocation_catalog = {}
    engine.discover_topology = lambda: None
    engine.management_cluster_present = lambda: False

    class ObservedWithoutCluster(Exception):
        pass

    def continue_inventory():
        raise ObservedWithoutCluster

    engine.external_vault = continue_inventory

    def forbidden(*args, **kwargs):
        raise AssertionError("Tried to open removed management Kubernetes")

    monkeypatch.setattr(
        "scripts.operations.azure.environment_operator.EnvironmentOperator", forbidden
    )
    with pytest.raises(ObservedWithoutCluster):
        engine.clean()
