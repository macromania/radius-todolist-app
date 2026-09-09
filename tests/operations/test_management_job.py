import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[2] / "operations"
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

    def test_foreign_management_target_is_rejected_before_any_mutation(self):
        config = {
            "foundation": {"subscriptionId": operator.SUBSCRIPTION},
            "managementCluster": {"name": "other-project", "resourceGroup": "other-group"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".state/azure/provisioning.json"
            with (
                patch.object(operator, "ROOT", root),
                patch.object(
                    operator.OperatorConfig,
                    "load",
                    return_value=SimpleNamespace(to_dict=lambda: config),
                ),
                patch.object(operator, "az") as az,
                patch.object(operator, "write_json") as write,
                patch.object(sys, "argv", ["run-management-job.py", "--config", str(path)]),
            ):
                with self.assertRaisesRegex(ValueError, "not this project's management cluster"):
                    operator.main()
            az.assert_not_called()
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
