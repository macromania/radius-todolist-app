import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location(
    "issuance", Path(__file__).parents[2] / "scripts/operations/issue-certificate.py"
)
issuance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(issuance)


class NotFound(Exception):
    pass


class CertificateIssuanceTests(unittest.TestCase):
    def test_staging_never_overwrites_live_certificate(self):
        self.exercise(staging=True, failure=False)

    def test_failed_first_issuance_preserves_registered_account(self):
        self.exercise(staging=False, failure=True)

    def test_selected_deployment_uses_its_qualified_vault_objects(self):
        self.exercise(staging=True, failure=False, selected=True)

    def exercise(self, staging, failure, selected=False):
        certificates, secrets, credential = MagicMock(), MagicMock(), MagicMock()
        certificates.get_certificate.side_effect = NotFound
        secrets.get_secret.side_effect = NotFound
        modules = {}
        for name in (
            "azure",
            "azure.core",
            "azure.core.exceptions",
            "azure.identity",
            "azure.keyvault",
            "azure.keyvault.certificates",
            "azure.keyvault.secrets",
        ):
            modules[name] = ModuleType(name)
        modules["azure.core.exceptions"].ResourceNotFoundError = NotFound
        modules["azure.identity"].WorkloadIdentityCredential = lambda: credential
        modules["azure.keyvault.certificates"].CertificateClient = lambda *args: certificates
        modules["azure.keyvault.secrets"].SecretClient = lambda *args: secrets

        def run(command, **kwargs):
            self.assertEqual(command[0], "certbot")
            self.assertEqual(command[command.index("--cert-name") + 1], args.certificate_name)
            directory = Path(command[command.index("--config-dir") + 1])
            account = directory / "accounts/acme.example/account/regr.json"
            account.parent.mkdir(parents=True)
            account.write_text('{"account":"registered"}')
            if failure:
                raise subprocess.CalledProcessError(1, ["certbot"])

        args = SimpleNamespace(
            domain="demo.centralus.cloudapp.azure.com",
            slot="management",
            certificate_name="gateway-management",
            account_secret="acme-management",
            vault_name="projectvault",
            namespace="radplanes-management-management",
            staging=staging,
            force=False,
        )
        if selected:
            args.project_name = "sample"
            args.resource_prefix = "sample-demo-azure"
            args.certificate_name = "gateway-sample-demo-azure-management"
            args.account_secret = "acme-sample-demo-azure-management"
            args.namespace = "sample-demo-azure-management-management"
        with (
            patch.dict(sys.modules, modules),
            patch.object(issuance.subprocess, "run", side_effect=run),
        ):
            if failure:
                with self.assertRaises(subprocess.CalledProcessError):
                    issuance.issue_in_cluster(args)
            else:
                self.assertIsNone(issuance.issue_in_cluster(args))
        certificates.import_certificate.assert_not_called()
        secrets.set_secret.assert_called_once()
        name, value = secrets.set_secret.call_args.args
        self.assertEqual(name, args.account_secret)
        self.assertIn("accounts/acme.example/account/regr.json", json.loads(value))
        credential.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
