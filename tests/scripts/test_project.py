import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location(
    "project", Path(__file__).parents[2] / "scripts/project.py"
)
project = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(project)


class PreflightTests(unittest.TestCase):
    def test_operator_lookup_pins_subscription_without_changing_defaults(self):
        commands = []

        def run(args, **kwargs):
            commands.append(args)
            if args[:3] == ["az", "account", "show"]:
                return json.dumps({
                    "id": project.SUBSCRIPTION,
                    "tenantId": "11111111-1111-1111-1111-111111111111",
                })
            if args[:3] == ["az", "account", "get-access-token"]:
                return '{"accessToken":"project-tenant-token"}'
            if args[0] == "curl":
                return "8.8.8.8"
            if args[:3] == ["az", "vm", "list-usage"]:
                return json.dumps([
                    {"name": {"value": name}, "currentValue": 0, "limit": 100}
                    for name in ("cores", "standardDSv5Family")
                ])
            if args[:3] == ["az", "group", "list"]:
                return "[]"
            if args[:3] == ["az", "postgres", "flexible-server"]:
                return '[{"supportedServerEditions":[{"name":"GeneralPurpose"}]}]'
            return ""

        connection = MagicMock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = (
            b'{"id":"22222222-2222-2222-2222-222222222222"}'
        )
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(project, "run", side_effect=run),
                patch.object(project.http.client, "HTTPSConnection", return_value=connection),
                patch.object(project.shutil, "which", return_value="/tool"),
                patch.object(project.Path, "is_file", return_value=True),
                patch.object(project, "state_dir", return_value=Path(temp)),
            ):
                project.preflight("azure")
                result = json.loads((Path(temp) / "context.json").read_text())
        self.assertEqual(result["operator_object_id"], "22222222-2222-2222-2222-222222222222")
        connection.request.assert_called_once_with(
            "GET", "/v1.0/me?$select=id",
            headers={"Authorization": "Bearer project-tenant-token"},
        )
        connection.close.assert_called_once()
        for command in commands:
            if command[0] == "az":
                self.assertIn("--subscription", command)
                self.assertEqual(command[command.index("--subscription") + 1], project.SUBSCRIPTION)
        self.assertFalse(any(command[:3] == ["az", "account", "set"] for command in commands))

    def test_graph_redirect_does_not_forward_token(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 302
        with (
            patch.object(project, "az", return_value={"accessToken": "scoped-token"}),
            patch.object(project.http.client, "HTTPSConnection", return_value=connection),
        ):
            with self.assertRaises(project.CommandError):
                project.operator_identity()
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()

    def test_restricted_postgres_fails_before_persisting_deployment_context(self):
        replies = [
            {"id": project.SUBSCRIPTION, "tenantId": "11111111-1111-1111-1111-111111111111"},
            [
                {"name": {"value": name}, "currentValue": 0, "limit": 100}
                for name in ("cores", "standardDSv5Family")
            ],
            [{"supportedServerEditions": [], "reason": "Subscriptions are restricted"}],
        ]
        with (
            patch.object(project, "az", side_effect=replies),
            patch.object(project, "operator_identity",
                         return_value={"id": "22222222-2222-2222-2222-222222222222"}),
            patch.object(project, "run", return_value="8.8.8.8"),
            patch.object(project.shutil, "which", return_value="/tool"),
            patch.object(project.Path, "is_file", return_value=True),
            patch.object(project, "write_json") as write,
        ):
            with self.assertRaisesRegex(project.CommandError, "PostgreSQL provisioning is unavailable"):
                project.preflight("azure")
        write.assert_not_called()

    def test_azure_mutation_requires_explicit_confirmation(self):
        with patch.dict(project.os.environ, {}, clear=True):
            with self.assertRaises(project.CommandError):
                project.require_confirmation("azure")

    def test_invalid_capture_is_rejected(self):
        for invalid in ("", "ERROR: key not found", "/subscriptions/other"):
            with self.assertRaises(ValueError):
                project.uuid(invalid, "test")


if __name__ == "__main__":
    unittest.main()
