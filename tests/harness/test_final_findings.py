import io
import json
import subprocess
import unittest
from contextlib import nullcontext, redirect_stdout
from unittest.mock import Mock, patch

import test_acceptance as base
import test_export_state as exports

faults = base.faults


class FinalRecoveryDeadlineTests(base.StateCase):
    def test_f026_standalone_cli_fails_when_29_second_restore_has_2_second_close(self):
        kube = base.FakeKube(self.config.target("shared-control"))
        clock = self.clock

        class FinalCloseProbe(base.FakeProbe):
            def request(self, action):
                if action == "fresh" and self.kube.policy is None:
                    clock.sleep(29)
                    self.after_restore_query = True
                return super().request(action)

            def close(self):
                if not self.closed and getattr(self, "after_restore_query", False):
                    clock.sleep(2)
                super().close()

        fault = faults.ParentFault(
            self.config,
            "shared-control",
            "control-reconciler",
            self.root / "evidence" / "final-close.json",
            kube_factory=lambda _: kube,
            probe_factory=FinalCloseProbe,
            clock=clock,
            sleep=clock.sleep,
        )
        output = io.StringIO()
        with (
            patch.object(faults, "ParentFault", return_value=fault),
            patch.object(faults, "source_metadata", return_value={"commit": "a" * 40}),
            patch.object(faults, "interruption_is_failure", return_value=nullcontext()),
            patch.object(faults.time, "monotonic", clock),
            patch.object(faults.time, "sleep", clock.sleep),
            redirect_stdout(output),
        ):
            result = faults.main(
                [
                    "--config",
                    str(self.config_path),
                    "--slot",
                    "shared-control",
                    "--component",
                    "control-reconciler",
                    "--execute",
                ],
                configuration_factory=faults.Configuration,
            )
        self.assertEqual(clock() - fault.recovery_started, 31)
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "failed")
        self.assertIsNone(kube.policy)
        self.assertTrue(fault.record["physical_restored"])
        self.assertFalse(fault.record["restored"])
        self.assertEqual(fault.record["outcome"], "restoration_failed")


class FreshExportTrustTests(unittest.TestCase):
    setUp = exports.ExportTests.setUp
    exporter = exports.ExportTests.exporter

    def test_f034_watch_rechecks_project_tags_for_cluster_gateway_and_public_ip(self):
        families = {
            "cluster": ["az", "aks", "show"],
            "gateway": ["az", "network", "application-gateway", "list"],
            "public-ip": ["az", "network", "public-ip", "show"],
        }
        for name, family in families.items():
            with self.subTest(resource=name):
                root = self.root / name
                root.mkdir(mode=0o700)
                config = root / "provisioning.json"
                values = exports.configuration()
                config.write_text(json.dumps(values))
                platform = exports.Platform(values)
                platform.requested[-1]["ready"] = False
                clock = exports.Clock()
                drift = {"changed": False}
                messages = []

                def execute(args, _platform=platform, _family=family, _drift=drift, **kwargs):
                    result = _platform.execute(args, **kwargs)
                    if _drift["changed"] and args[: len(_family)] == _family:
                        value = json.loads(result.stdout)
                        resource = value[0] if isinstance(value, list) else value
                        resource["tags"]["project"] = "not-radplanes"
                        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
                    return result

                def sleep(seconds, _clock=clock, _platform=platform, _drift=drift):
                    _clock.sleep(seconds)
                    _platform.requested[-1]["ready"] = True
                    _drift["changed"] = True

                exporter = exports.export_module.Exporter(
                    config,
                    execute=execute,
                    health=Mock(),
                    clock=clock,
                    sleep=sleep,
                )
                with self.assertRaisesRegex(
                    exports.export_module.ExportError, "azure_resource_ownership_mismatch"
                ):
                    exporter.run(watch=True, timeout=30, emit=messages.append)
                self.assertTrue(json.loads(messages[0])["ready_for_onboarding"])
                status = json.loads((root / "export-status.json").read_text())
                self.assertEqual(status["outcome"], "failed")
                self.assertFalse(status["ready_for_onboarding"])

    def test_f034_cached_credentials_do_not_skip_live_cluster_identity_validation(self):
        exporter = self.exporter()
        exporter.sample()
        original = self.platform.execute
        credentials_before = sum(
            args[:3] == ["az", "aks", "get-credentials"] for args in self.platform.calls
        )

        def changed(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "kubectl" and args[-4:] == ["namespace", "kube-system", "-o", "json"]:
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"metadata": {"uid": exports.uid("different-cluster")}}), ""
                )
            return result

        exporter.execute = changed
        with self.assertRaisesRegex(
            exports.export_module.ExportError, "published_cluster_identity_changed"
        ):
            exporter.sample()
        self.assertEqual(
            sum(args[:3] == ["az", "aks", "get-credentials"] for args in self.platform.calls),
            credentials_before,
        )


if __name__ == "__main__":
    unittest.main()
