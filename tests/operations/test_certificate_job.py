import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "certificate_job", Path(__file__).parents[2] / "scripts/operations/run-certificate-job.py"
)
issuer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(issuer)


class CertificateJobTests(unittest.TestCase):
    def settings(self):
        return {
            "allocations": {
                "shared-control": {
                    "identities": {"certificateIssuer": {"clientId": "issuer-identity"}},
                },
            },
            "foundation": {"vaultName": "project-vault", "tenantId": "tenant"},
            "images": {"provisioner": "project.azurecr.io/provisioner@sha256:" + "a" * 64},
        }

    def test_job_uses_scoped_identity_and_only_challenge_configmap_permission(self):
        resources = issuer.job_resources(
            self.settings(),
            "shared-control",
            "radplanes-shared-control-control",
            "test.centralus.cloudapp.azure.com",
        )
        role = next(item for item in resources if item["kind"] == "Role")
        self.assertEqual(
            role["rules"],
            [
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": ["acme-challenges"],
                    "verbs": ["get", "patch"],
                }
            ],
        )
        job = resources[-1]
        self.assertEqual(job["metadata"]["namespace"], "radplanes-system")
        pod = job["spec"]["template"]
        self.assertEqual(pod["metadata"]["labels"]["azure.workload.identity/use"], "true")
        self.assertEqual(pod["spec"]["serviceAccountName"], "certificate-issuer")
        command = pod["spec"]["containers"][0]["command"]
        self.assertIn("gateway-shared-control", command)
        self.assertIn("acme-shared-control", command)
        self.assertEqual(job["spec"]["backoffLimit"], 0)

    def test_mutable_certificate_image_is_rejected(self):
        settings = self.settings()
        settings["images"]["provisioner"] = "project.azurecr.io/provisioner:latest"
        with self.assertRaises(ValueError):
            issuer.job_resources(
                settings,
                "shared-control",
                "radplanes-shared-control-control",
                "test.centralus.cloudapp.azure.com",
            )


def test_selected_certificate_entrypoint_uses_exact_context_namespace_and_reference(tmp_path):
    settings = CertificateJobTests().settings()
    prefix, slot = "sample-demo-azure", "shared-control"
    settings["foundation"].update(projectName="sample", resourcePrefix=prefix)
    certificate = f"gateway-{prefix}-{slot}"
    settings["allocations"][slot].update(
        certificateName=certificate, acmeStateSecretName=f"acme-{prefix}-{slot}"
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps(settings))
    namespace = f"{prefix}-{slot}-control"
    uri = f"https://project-vault.vault.azure.net/secrets/{certificate}"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "jobs" in command:
            body = {"items": []}
        elif "pods" in command:
            body = {
                "items": [
                    {
                        "status": {
                            "containerStatuses": [
                                {
                                    "name": "issuer",
                                    "state": {
                                        "terminated": {
                                            "exitCode": 0,
                                            "message": json.dumps({"certificateSecretUri": uri}),
                                        }
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        elif "get" in command and "job" in command:
            body = {"status": {"succeeded": 1}}
        else:
            body = {}
        return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

    argv = [
        "run-certificate-job",
        "--slot",
        slot,
        "--context",
        f"{prefix}-{slot}",
        "--namespace",
        namespace,
        "--kubeconfig",
        str(tmp_path / "kubeconfig"),
        "--domain",
        "test.centralus.cloudapp.azure.com",
        "--config",
        str(config),
    ]
    with patch.object(sys, "argv", argv), patch.object(issuer.subprocess, "run", side_effect=run):
        assert issuer.main() == 0
    assert calls
    assert all(command[command.index("--context") + 1] == f"{prefix}-{slot}" for command in calls)
    assert all(
        command[command.index("-n") + 1] == f"{prefix}-system"
        for command in calls
        if "-n" in command
    )
    argv[argv.index("--context") + 1] = "radplanes-shared-control"
    with patch.object(sys, "argv", argv), patch.object(issuer.subprocess, "run") as rejected:
        with pytest.raises(ValueError, match="Context does not match"):
            issuer.main()
        rejected.assert_not_called()


if __name__ == "__main__":
    unittest.main()
