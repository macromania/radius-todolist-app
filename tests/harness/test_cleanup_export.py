import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_export_state as exports

SPEC = importlib.util.spec_from_file_location(
    "export_cleanup_contract", Path(__file__).resolve().parents[2] / "operations/clean-azure.py"
)
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)
export_module = exports.export_module
PROJECT_SPEC = importlib.util.spec_from_file_location(
    "export_bootstrap_contract", Path(__file__).resolve().parents[2] / "operations/project.py"
)
project = importlib.util.module_from_spec(PROJECT_SPEC)
PROJECT_SPEC.loader.exec_module(project)


class CleanupExportTests(unittest.TestCase):
    setUp = exports.ExportTests.setUp
    exporter = exports.ExportTests.exporter

    def cleanup_targets(self):
        return json.loads((self.root / "cleanup-targets.json").read_text())["targets"]

    def missing_management_app(self, slot, args):
        if slot == "management" and args[:2] == ["get", "namespace"] and args[2] != "kube-system":
            return ""
        return exports.Platform.kubernetes(self.platform, slot, args)

    def test_generated_handoff_roundtrips_through_cleanup_target_and_workspace_contract(self):
        self.roundtrip_handoff("")

    def test_nested_handoff_roundtrips_through_cleanup_target_without_rebasing_acceptance(self):
        self.roundtrip_handoff("operator-export")

    def roundtrip_handoff(self, directory):
        azure_state = self.root / ".state/azure"
        azure_state.mkdir(parents=True, mode=0o700)
        state = azure_state / directory
        state.mkdir(exist_ok=True, mode=0o700)
        values = json.loads(
            json.dumps(self.values).replace(exports.SUBSCRIPTION, cleanup.SUBSCRIPTION)
        )
        config = state / "provisioning.json"
        config.write_text(json.dumps(values))
        platform = exports.Platform(values)
        active_config = azure_state / "radius.yaml"
        active_config.write_text("active operator workspace: must not be changed\n")
        with patch.object(export_module, "ROOT", self.root.resolve()):
            exporter = export_module.Exporter(
                config.resolve(), execute=platform.execute, health=Mock()
            )
            self.assertEqual(exporter.run(watch=False, emit=lambda _: None), 0)
            acceptance = (state / "acceptance.json").read_bytes()
            if directory:
                metadata = state / "cleanup-targets.json"
                previous = json.loads(metadata.read_text())
                for target in previous["targets"].values():
                    for field in ("kubeconfig", "radiusConfig"):
                        target[field] = Path(target[field]).name
                metadata.write_text(json.dumps(previous))
                self.assertEqual(
                    export_module.Exporter(
                        config.resolve(), execute=platform.execute, health=Mock()
                    ).run(watch=False, emit=lambda _: None),
                    0,
                )
                self.assertEqual((state / "acceptance.json").read_bytes(), acceptance)

        foundation = {
            **values["foundation"],
            "tags": dict(cleanup.TAGS),
            "platformResourceGroup": "rg-radplanes-platform",
            "virtualNetworkName": "project",
            "roleDefinitionIds": {key: cleanup.role_id(key) for key in cleanup.ROLE_NAMES},
        }
        for kind, resource_type in (
            ("vault", "Microsoft.KeyVault/vaults"),
            ("registry", "Microsoft.ContainerRegistry/registries"),
        ):
            foundation[kind + "Name"] = "project"
            foundation[kind + "Id"] = (
                cleanup.group_id("rg-radplanes-platform")
                + "/providers/"
                + resource_type
                + "/project"
            )
        manifest = cleanup.Manifest(
            {
                "foundation": foundation,
                "allocations": [
                    {**allocation, "nodeResourceGroup": f"rg-radplanes-{slot}-nodes"}
                    for slot, allocation in values["allocations"].items()
                ],
                "managementCluster": {
                    **values["managementCluster"],
                    "resourceGroup": "rg-radplanes-management-cluster",
                },
            }
        )
        document = json.loads((state / "cleanup-targets.json").read_text())
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["generatedBy"], "harness/export-state.py")
        engine = cleanup.Cleanup(
            manifest, root=self.root.resolve(), commands=Mock(), targets=document["targets"]
        )
        self.assertEqual(set(engine.targets), set(exports.SLOTS))
        public = json.dumps(document) + (state / "cleanup-radius.yaml").read_text()
        for slot in exports.SLOTS:
            with self.subTest(slot=slot):
                exported = document["targets"][slot]
                self.assertEqual(
                    exported["kubeconfig"], str(Path(directory) / (slot + ".kubeconfig"))
                )
                self.assertEqual(
                    exported["radiusConfig"], str(Path(directory) / "cleanup-radius.yaml")
                )
                self.assertEqual(
                    json.loads(acceptance)["targets"][slot]["kubeconfig"], slot + ".kubeconfig"
                )
                target = engine.target(slot)
                self.assertEqual(target["clusterId"], manifest.cluster_id(slot))
                self.assertEqual(target["clusterUid"], exports.uid("cluster-" + slot))
                self.assertEqual(target["kubeconfig"], (state / (slot + ".kubeconfig")).resolve())
                self.assertEqual(target["config"], (state / "cleanup-radius.yaml").resolve())
                radius = export_module.yaml.safe_load(target["config"].read_text())
                self.assertEqual(
                    radius["workspaces"]["items"][target["workspace"]],
                    {
                        "connection": {"kind": "kubernetes", "context": target["context"]},
                        "scope": "/planes/radius/local/resourceGroups/radplanes",
                    },
                )
                self.assertEqual(target["group"], "radplanes")
                self.assertNotIn(platform.key(slot), public)
                self.assertEqual(target["kubeconfig"].stat().st_mode & 0o777, 0o600)
        for name in ("cleanup-targets.json", "cleanup-radius.yaml"):
            self.assertEqual((state / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            active_config.read_text(), "active operator workspace: must not be changed\n"
        )

    def seed_bootstrap_kubeconfig(self):
        allocation = self.values["allocations"]["management"]
        outputs = {
            "managementCluster": {
                **self.values["managementCluster"],
                "name": allocation["clusterName"],
                "resourceGroup": allocation["clusterResourceGroup"],
            },
            "foundation": {"tenantId": exports.uid("tenant")},
            "allocations": [
                {**allocation, "identities": {"radius": {"clientId": exports.uid("radius")}}}
            ],
        }
        (self.root / "bootstrap.outputs.json").write_text(json.dumps(outputs))

        def run(args, **_kwargs):
            if args[0] in {"az", "kubelogin"}:
                result = self.platform.execute(args)
                self.assertEqual(result.returncode, 0)
                return result.stdout
            self.assertEqual(args[1], "operations/install-radius.py")
            return ""

        with (
            patch.object(project, "state_dir", return_value=self.root.resolve()),
            patch.object(project, "require_confirmation"),
            patch.object(project, "SUBSCRIPTION", exports.SUBSCRIPTION),
            patch.object(project, "run", side_effect=run),
        ):
            project.install_management_radius()
        return self.root / "management.kubeconfig"

    def test_standard_bootstrap_to_first_export_reuses_verified_canonical_kubeconfig(self):
        canonical = self.seed_bootstrap_kubeconfig()
        before, inode = canonical.read_bytes(), canonical.stat().st_ino
        self.assertNotIn(b"Generated by", before)
        self.assertFalse((self.root / "acceptance.json").exists())
        self.assertFalse((self.root / "cleanup-targets.json").exists())
        self.platform.calls.clear()
        exporter = self.exporter()
        self.assertEqual(exporter.run(watch=False, emit=lambda _: None), 0)
        self.assertEqual(canonical.read_bytes(), before)
        self.assertEqual(canonical.stat().st_ino, inode)
        self.assertEqual(exporter.accesses["management"].kubeconfig, canonical.resolve())
        management_reads = [
            args
            for args in self.platform.calls
            if args[0] == "kubectl" and "radplanes-management" in args
        ]
        self.assertTrue(management_reads)
        for args in management_reads:
            self.assertEqual(Path(args[args.index("--kubeconfig") + 1]), canonical.resolve())

    def test_bootstrap_adoption_refuses_changed_context_ca_tls_or_exec_before_using_file(self):
        changes = {
            "context": lambda value: value.update({"current-context": "foreign"}),
            "server": lambda value: value["clusters"][0]["cluster"].update(
                server="https://foreign.example.test"
            ),
            "ca": lambda value: value["clusters"][0]["cluster"].update(
                {"certificate-authority-data": "Zm9yZWlnbi1jYQ=="}
            ),
            "tls_name": lambda value: value["clusters"][0]["cluster"].update(
                {"tls-server-name": "foreign.example.test"}
            ),
            "exec": lambda value: value["users"][0]["user"]["exec"].update(
                command="/untrusted/kubelogin"
            ),
            "exec_environment": lambda value: value["users"][0]["user"]["exec"].update(
                env=[{"name": "AZURE_CONFIG_DIR", "value": "/untrusted"}]
            ),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                canonical = self.seed_bootstrap_kubeconfig()
                value = json.loads(canonical.read_text())
                change(value)
                canonical.write_text(json.dumps(value))
                before = canonical.read_bytes()
                self.platform.calls.clear()
                with self.assertRaises(export_module.ExportError):
                    self.exporter().run(watch=False, emit=lambda _: None)
                self.assertEqual(canonical.read_bytes(), before)
                self.assertFalse(any(args[0] == "kubectl" for args in self.platform.calls))
                self.assertFalse((self.root / "cleanup-targets.json").exists())

    def test_cleanup_export_configuration_must_stay_within_azure_state(self):
        state = self.root / ".state/other"
        state.mkdir(parents=True, mode=0o700)
        config = state / "provisioning.json"
        config.write_text(json.dumps(self.values))
        with patch.object(export_module, "ROOT", self.root.resolve()):
            with self.assertRaisesRegex(
                export_module.ExportError, "cleanup_config_must_be_in_azure_state"
            ):
                export_module.Exporter(config.resolve(), execute=self.platform.execute)
        self.assertEqual(self.platform.calls, [])

    def test_unchanged_complete_export_repairs_each_missing_cleanup_artifact(self):
        self.exporter().run(watch=False, emit=lambda _: None)
        acceptance = (self.root / "acceptance.json").read_bytes()
        for missing in (
            ("cleanup-targets.json",),
            ("cleanup-radius.yaml",),
            ("cleanup-targets.json", "cleanup-radius.yaml"),
        ):
            with self.subTest(missing=missing):
                for name in missing:
                    (self.root / name).unlink()
                self.assertEqual(self.exporter().run(watch=False, emit=lambda _: None), 0)
                self.assertEqual((self.root / "acceptance.json").read_bytes(), acceptance)
                self.assertEqual(set(self.cleanup_targets()), set(exports.SLOTS))
                self.assertTrue((self.root / "cleanup-radius.yaml").is_file())

    def test_verified_management_access_is_exported_before_its_application_exists(self):
        with patch.object(self.platform, "kubernetes", side_effect=self.missing_management_app):
            exporter = self.exporter()
            self.assertEqual(exporter.run(watch=False, emit=lambda _: None), 3)
        self.assertEqual(set(self.cleanup_targets()), {"management"})
        self.assertFalse((self.root / "acceptance.json").exists())
        self.assertFalse((self.root / "management.key").exists())
        self.assertTrue((self.root / "management.kubeconfig").is_file())
        self.assertFalse(exporter.work.exists())
        status = json.loads((self.root / "export-status.json").read_text())
        self.assertEqual(status["outcome"], "waiting")
        self.assertFalse(status["ready_for_onboarding"])
        self.assertEqual(status["published_slots"], [])

    def test_partial_export_repairs_missing_metadata_without_acceptance_snapshot(self):
        with patch.object(self.platform, "kubernetes", side_effect=self.missing_management_app):
            self.assertEqual(self.exporter().run(watch=False, emit=lambda _: None), 3)
            for name in ("cleanup-targets.json", "cleanup-radius.yaml"):
                (self.root / name).unlink()
            self.assertEqual(self.exporter().run(watch=False, emit=lambda _: None), 3)
        self.assertEqual(set(self.cleanup_targets()), {"management"})
        self.assertFalse((self.root / "acceptance.json").exists())

    def test_child_without_ready_gateway_still_has_cleanup_access(self):
        self.platform.no_https.add("shared-data")
        self.assertEqual(self.exporter().run(watch=False, emit=lambda _: None), 3)
        self.assertEqual(set(self.cleanup_targets()), set(exports.SLOTS))
        targets = json.loads((self.root / "acceptance.json").read_text())["targets"]
        self.assertNotIn("shared-data", targets)
        self.assertFalse((self.root / "shared-data.key").exists())

    def test_inventory_clusters_without_showcase_tenants_still_get_cleanup_metadata(self):
        self.platform.requested = []
        self.assertEqual(self.exporter().run(watch=False, emit=lambda _: None), 3)
        self.assertEqual(set(self.cleanup_targets()), set(exports.SLOTS))
        targets = json.loads((self.root / "acceptance.json").read_text())["targets"]
        self.assertEqual(set(targets), {"management"})
        for slot in exports.SLOTS[1:]:
            self.assertFalse((self.root / (slot + ".key")).exists())

    def test_partial_metadata_pins_cluster_uid_across_exporter_runs(self):
        with patch.object(self.platform, "kubernetes", side_effect=self.missing_management_app):
            self.exporter().run(watch=False, emit=lambda _: None)
        original = (self.root / "cleanup-targets.json").read_bytes()

        def changed(slot, args):
            if args[:3] == ["get", "namespace", "kube-system"]:
                return json.dumps({"metadata": {"uid": exports.uid("replacement-cluster")}})
            return self.missing_management_app(slot, args)

        with patch.object(self.platform, "kubernetes", side_effect=changed):
            with self.assertRaisesRegex(
                export_module.ExportError, "published_cleanup_identity_changed"
            ):
                self.exporter().run(watch=False, emit=lambda _: None)
        self.assertEqual((self.root / "cleanup-targets.json").read_bytes(), original)
        self.assertFalse((self.root / "acceptance.json").exists())

    def test_foreign_or_manual_cleanup_files_are_not_overwritten(self):
        for name, content in (
            ("cleanup-targets.json", '{"version":1,"targets":{}}\n'),
            ("cleanup-radius.yaml", "workspaces: {}\n"),
        ):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text(content)
                with self.assertRaisesRegex(export_module.ExportError, "not_owned"):
                    self.exporter().run(watch=False, emit=lambda _: None)
                self.assertEqual(path.read_text(), content)
                self.assertFalse((self.root / "acceptance.json").exists())
                path.unlink()

    def test_generated_marker_does_not_allow_foreign_target_or_workspace_scope(self):
        self.exporter().run(watch=False, emit=lambda _: None)
        for name in ("cleanup-targets.json", "cleanup-radius.yaml"):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_text()
                changed = (
                    original.replace('"project": "radplanes"', '"project": "foreign"')
                    if name.endswith(".json")
                    else original.replace("/resourceGroups/radplanes", "/resourceGroups/foreign")
                )
                path.write_text(changed)
                with self.assertRaises(export_module.ExportError):
                    self.exporter().run(watch=False, emit=lambda _: None)
                self.assertEqual(path.read_text(), changed)
                path.write_text(original)

    def test_symlinked_cleanup_files_are_rejected_without_changing_the_destination(self):
        foreign = self.root / "manual-file"
        foreign.write_text("not an exporter file\n")
        for name in ("cleanup-targets.json", "cleanup-radius.yaml"):
            with self.subTest(name=name):
                path = self.root / name
                path.symlink_to(foreign.resolve())
                with self.assertRaisesRegex(export_module.ExportError, "symlink_refused"):
                    self.exporter().run(watch=False, emit=lambda _: None)
                self.assertTrue(path.is_symlink())
                self.assertEqual(foreign.read_text(), "not an exporter file\n")
                path.unlink()

    def test_unowned_kubeconfig_is_not_overwritten_by_partial_export(self):
        path = self.root / "management.kubeconfig"
        path.write_text("manual configuration\n")
        path.chmod(0o600)
        with self.assertRaisesRegex(export_module.ExportError, "cleanup_kubeconfig_not_owned"):
            self.exporter().run(watch=False, emit=lambda _: None)
        self.assertEqual(path.read_text(), "manual configuration\n")
        self.assertFalse((self.root / "cleanup-targets.json").exists())
        self.assertFalse((self.root / "cleanup-radius.yaml").exists())

    def test_interrupted_handoff_preserves_atomic_targets_and_repairable_dependencies(self):
        original_replace = export_module.os.replace

        def interrupted(source, destination):
            if (
                Path(destination).name == "cleanup-targets.json"
                and len(json.loads(Path(source).read_text())["targets"]) == 2
            ):
                raise OSError("injected handoff failure")
            return original_replace(source, destination)

        with patch.object(export_module.os, "replace", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "injected handoff failure"):
                self.exporter().run(watch=False, emit=lambda _: None)
        targets = self.cleanup_targets()
        self.assertEqual(set(targets), {"management"})
        azure_state = export_module.ROOT / ".state/azure"
        self.assertTrue((azure_state / targets["management"]["kubeconfig"]).is_file())
        self.assertTrue((azure_state / targets["management"]["radiusConfig"]).is_file())
        self.assertEqual(list(self.root.rglob("*.writing")), [])
        self.assertEqual(self.exporter().run(watch=False, emit=lambda _: None), 0)
        self.assertEqual(set(self.cleanup_targets()), set(exports.SLOTS))


if __name__ == "__main__":
    unittest.main()
