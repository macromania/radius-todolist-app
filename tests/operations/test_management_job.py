import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plane_demo.management.providers.identity import DemoConfig

SCRIPTS = Path(__file__).parents[2] / "scripts/operations"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("management_job", SCRIPTS / "run-management-job.py")
operator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)


class ManagementJobTests(unittest.TestCase):
    def test_pending_operator_without_active_count_still_blocks(self):
        job = {"metadata": {"namespace": "radplanes-management-management"}, "status": {}}
        self.assertTrue(operator.unfinished_operator(job))
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        self.assertFalse(operator.unfinished_operator(job))
        job["status"] = {"conditions": [{"type": "FailureTarget", "status": "True"}]}
        self.assertTrue(operator.unfinished_operator(job))

    def test_operator_state_is_separate_and_private_modes_survive_remount(self):
        config = {
            "coordinatorIdentity": {"clientId": "coordinator"},
            "foundation": {"tenantId": "tenant"},
            "images": {"provisioner": "registry/provisioner@sha256:" + "a" * 64},
        }
        resources = operator.resources(config, "deploy-management")
        job = resources[-1]
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"]["fsGroupChangePolicy"], "OnRootMismatch")
        self.assertEqual(pod["securityContext"]["runAsUser"], 10001)
        self.assertEqual(pod["volumes"][1]["persistentVolumeClaim"]["claimName"], "operator-state")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertTrue(next(r for r in resources if r["kind"] == "ConfigMap")["immutable"])

    def test_saved_configuration_is_not_a_deployment_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".state/azure/provisioning.json"
            with (
                patch.object(operator, "ROOT", root),
                patch.object(operator, "execute") as execute,
                patch.object(sys, "argv", ["run-management-job.py", "--config", str(path)]),
            ):
                with self.assertRaisesRegex(ValueError, "checkout .env and live APIs"):
                    operator.main()
            execute.assert_not_called()


def test_selected_job_is_serialized_without_pvc_and_keeps_keys_out_of_configuration(monkeypatch):
    identity = DemoConfig(
        "azure", "sample", "demo", "11111111-1111-1111-1111-111111111111", "centralus"
    )
    selected = SimpleNamespace(
        identity=identity,
        namespace=identity.namespace,
        bootstrap_settings=identity.public_values(),
    )
    config = {
        "bootstrapIdentity": identity.public_values(),
        "coordinatorIdentity": {"clientId": "coordinator"},
        "foundation": {"tenantId": "tenant"},
        "images": {"provisioner": "registry/provisioner@sha256:" + "a" * 64},
    }
    monkeypatch.setattr(operator.OperatorConfig, "from_dict", lambda value: selected)
    key = "synthetic-child-key-" + "x" * 48
    resources = operator.resources(config, "deploy-management", {"DEMO_KEY_SHARED_DATA": key})
    assert not any(item["kind"] in {"StorageClass", "PersistentVolumeClaim"} for item in resources)
    job = resources[-1]
    assert job["metadata"]["namespace"] == identity.namespace("management")
    assert job["spec"]["suspend"] is True
    assert job["spec"]["parallelism"] == job["spec"]["completions"] == 1
    pod = job["spec"]["template"]["spec"]
    assert pod["volumes"] == [{"name": "config", "configMap": {"name": "deploy-management-config"}}]
    assert pod["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert pod["containers"][0]["env"][0]["name"] == "OPERATOR_JOB_UID"
    configmap = next(item for item in resources if item["kind"] == "ConfigMap")
    assert key not in json.dumps(configmap) and "DEMO_KEY_SHARED_DATA" not in json.dumps(configmap)
    secret = next(item for item in resources if item["kind"] == "Secret")
    assert secret["immutable"] is True
    assert secret["stringData"] == {"DEMO_KEY_SHARED_DATA": key}
    role = next(item for item in resources if item["kind"] == "Role")
    assert all(set(rule["verbs"]) <= {"get", "list"} for rule in role["rules"])
    assert role["rules"][-1] == {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}
    with pytest.raises(ValueError, match="fixed operator Job"):
        operator.resources(config, "deploy-management-other")


def test_normal_deployment_stops_when_canonical_image_inspection_fails(monkeypatch):
    identity = DemoConfig(
        "azure", "sample", "demo", "11111111-1111-1111-1111-111111111111", "centralus"
    )
    monkeypatch.setattr(operator, "load_config", lambda path: identity)
    monkeypatch.setattr(operator, "require_confirmation", lambda environment: None)
    calls = []

    def execute(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:2] == ["git", "status"]:
            return ""
        if arguments[:2] == ["git", "rev-parse"]:
            return "a" * 40
        raise operator.CommandError("canonical image inspection failed")

    monkeypatch.setattr(operator, "execute", execute)
    monkeypatch.setattr(sys, "argv", ["run-management-job.py", "--execute"])
    with pytest.raises(operator.CommandError, match="canonical image inspection failed"):
        operator.main()
    assert len(calls) == 4
    assert calls[-1][0] == "bash" and calls[-1][-1] == "--inspect"
    assert calls[-1][1].endswith("/scripts/operations/azure/build.sh")


if __name__ == "__main__":
    unittest.main()
