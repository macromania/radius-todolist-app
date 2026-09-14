import importlib.util
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
