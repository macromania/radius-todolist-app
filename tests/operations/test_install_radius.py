import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[2] / "operations"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("install_radius", SCRIPTS / "install-radius.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

CLIENT = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"


class RadiusInstallTests(unittest.TestCase):
    def test_every_cluster_command_uses_project_config_and_context(self):
        commands = []
        pods = {
            "items": [
                {
                    "metadata": {},
                    "spec": {
                        "serviceAccountName": account,
                        "containers": [
                            {
                                "env": [
                                    {"name": "AZURE_CLIENT_ID", "value": CLIENT},
                                    {
                                        "name": "AZURE_FEDERATED_TOKEN_FILE",
                                        "value": "/projected/token",
                                    },
                                ],
                            }
                        ],
                    },
                }
                for account in ("applications-rp", "bicep-de", "ucp", "dynamic-rp")
            ]
        }

        def run(args, **kwargs):
            commands.append((args, kwargs["env"]))
            return json.dumps(pods) if kwargs.get("capture") else ""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".state/azure"
            state.mkdir(parents=True)
            kubeconfig, config = state / "kubeconfig", state / "radius.yaml"
            kubeconfig.touch()
            config.touch()
            with (
                patch.object(installer, "ROOT", root),
                patch("project.ROOT", root),
                patch.object(installer, "run", side_effect=run),
            ):
                installer.install("radplanes-management", kubeconfig, config, CLIENT, TENANT)
            for args, env in commands:
                self.assertEqual(env["KUBECONFIG"], str(kubeconfig.resolve()))
                self.assertEqual(env["HOME"], str((state / "homes/radplanes-management").resolve()))
                if args[0] == "kubectl":
                    self.assertEqual(args[1:3], ["--context", "radplanes-management"])
                else:
                    self.assertEqual(args[:3], ["rad", "--config", str(config.resolve())])
            for account in ("applications-rp", "bicep-de", "ucp", "dynamic-rp"):
                restart = next(
                    i
                    for i, (args, _) in enumerate(commands)
                    if "restart" in args and f"deployment/{account}" in args
                )
                status = next(
                    i
                    for i, (args, _) in enumerate(commands)
                    if "status" in args and f"deployment/{account}" in args
                )
                self.assertLess(restart, status)

    def test_unrelated_context_fails_before_any_command(self):
        with patch.object(installer, "run") as run:
            with self.assertRaises(ValueError):
                installer.install("kind-unrelated", Path("/x"), Path("/y"), CLIENT, TENANT)
            run.assert_not_called()

    def test_global_kubeconfig_fails_before_any_command(self):
        with patch.object(installer, "run") as run:
            with self.assertRaises(ValueError):
                installer.install(
                    "radplanes-management",
                    Path.home() / ".kube/config",
                    Path("/y"),
                    CLIENT,
                    TENANT,
                )
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
