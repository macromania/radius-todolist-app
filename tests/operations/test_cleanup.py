import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid5

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "azure_cleanup_tests", ROOT / "scripts/operations/clean-azure.py"
)
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)


def bootstrap():
    platform = cleanup.group_id("rg-radplanes-platform")
    allocations = []
    for slot in ("management", "shared-control", "shared-data"):
        allocations.append(
            {
                "slot": slot,
                "clusterName": f"aks-radplanes-{slot}",
                "clusterResourceGroup": f"rg-radplanes-{slot}-cluster",
                "clusterResourceGroupId": cleanup.group_id(f"rg-radplanes-{slot}-cluster"),
                "appResourceGroup": f"rg-radplanes-{slot}-app",
                "appResourceGroupId": cleanup.group_id(f"rg-radplanes-{slot}-app"),
                "nodeResourceGroup": f"rg-radplanes-{slot}-nodes",
            }
        )
    return {
        "foundation": {
            "projectName": "radplanes",
            "subscriptionId": cleanup.SUBSCRIPTION,
            "location": "centralus",
            "tags": dict(cleanup.TAGS),
            "platformResourceGroup": "rg-radplanes-platform",
            "vaultName": "kv-radplan-abcdefghijklm",
            "vaultId": f"{platform}/providers/Microsoft.KeyVault/vaults/kv-radplan-abcdefghijklm",
            "registryName": "acrradplanesabcdefghijklm",
            "registryId": f"{platform}/providers/Microsoft.ContainerRegistry/registries/"
            "acrradplanesabcdefghijklm",
            "virtualNetworkName": "vnet-radplanes",
            "virtualNetworkId": (
                f"{platform}/providers/Microsoft.Network/virtualNetworks/vnet-radplanes"
            ),
            "roleDefinitionIds": {key: cleanup.role_id(key) for key in cleanup.ROLE_NAMES},
        },
        "allocations": allocations,
        "managementCluster": {
            "id": f"{allocations[0]['clusterResourceGroupId']}/providers/"
            "Microsoft.ContainerService/managedClusters/aks-radplanes-management",
            "name": "aks-radplanes-management",
            "resourceGroup": allocations[0]["clusterResourceGroup"],
        },
    }


class FakeCommands(cleanup.Commands):
    """All command responses and mutations are in-memory; no subprocess is run."""

    def __init__(self, root, manifest):
        super().__init__(root)
        self._bicep = root / "compiler"
        self._bicep.write_text("fixture, never executed")
        self._bicep.chmod(0o700)
        self.manifest = manifest
        self.calls = []
        self.fail = lambda args: False
        self.leave_cluster = False
        self.leave_radius_app = False
        self.leave_app_resource = False
        self.leave_group = False
        self.hidden_roles = set()
        self.leave_role = False
        self.foreign_workspace = False
        self.foreign_server = False
        self.foreign_uid = False
        self.insecure = False
        self.foreign_env = False
        self.groups = {
            name: {"name": name, "id": cleanup.group_id(name), "tags": dict(cleanup.TAGS)}
            for name in manifest.groups
        }
        self.resources = {name: [] for name in manifest.groups}
        self.clusters = {}
        self.targets = {}
        self.apps = {}
        self.app_resources = {}
        self.environments = {}
        self.deleted_vaults = []
        state = root / ".state/azure"
        state.mkdir(parents=True)
        (state / "radius.yaml").write_text("fixture, never parsed by a real CLI")
        for slot, allocation in manifest.allocations.items():
            path = state / f"{slot}.kubeconfig"
            path.write_text("fixture, never passed to a real CLI")
            self.targets[slot] = {
                "kubeconfig": path.name,
                "context": f"radplanes-{slot}",
                "clusterId": manifest.cluster_id(slot),
                "clusterUid": str(uuid5(cleanup.GUID_NAMESPACE, slot)),
            }
            self.clusters[slot] = {
                "id": manifest.cluster_id(slot),
                "name": allocation["clusterName"],
                "resourceGroup": allocation["clusterResourceGroup"],
                "nodeResourceGroup": allocation["nodeResourceGroup"],
                "fqdn": f"{slot}.hcp.centralus.azmk8s.io",
                "provisioningState": "Succeeded",
                "tags": dict(cleanup.TAGS),
            }
            self.resources[allocation["clusterResourceGroup"]].append(
                self.resource(
                    manifest.cluster_id(slot), "Microsoft.ContainerService/managedClusters"
                )
            )
            self.resources[allocation["nodeResourceGroup"]].append(
                self.resource(
                    f"{cleanup.group_id(allocation['nodeResourceGroup'])}/providers/"
                    "Microsoft.Compute/virtualMachineScaleSets/system",
                    "Microsoft.Compute/virtualMachineScaleSets",
                )
            )
            self.resources[allocation["appResourceGroup"]].append(
                self.resource(
                    f"{allocation['appResourceGroupId']}/providers/Microsoft.Network/applicationGateways/gateway",
                    "Microsoft.Network/applicationGateways",
                )
            )
            name = "management" if slot == "management" else slot.rsplit("-", 1)[1]
            self.apps[slot] = {}
            self.add_app(slot, name, slot, allocation["appResourceGroupId"])
            self.app_resources[(slot, name)] = [
                self.radius_resource(
                    name,
                    "Applications.Core/containers",
                    "management-api" if slot == "management" else "api",
                    {},
                )
            ]
        for slot in manifest.children:
            app = f"cluster-{slot}"
            self.add_app(
                "management",
                app,
                f"provision-{slot}",
                manifest.allocations[slot]["clusterResourceGroupId"],
            )
            self.app_resources[("management", app)] = [
                self.radius_resource(
                    app,
                    "Demo.Platform/clusters",
                    slot,
                    {"slot": slot, "clusterId": manifest.cluster_id(slot)},
                )
            ]
        for key, kind in (
            ("vaultId", "Microsoft.KeyVault/vaults"),
            ("registryId", "Microsoft.ContainerRegistry/registries"),
            ("virtualNetworkId", "Microsoft.Network/virtualNetworks"),
        ):
            self.resources[manifest.platform].append(self.resource(manifest.foundation[key], kind))
        self.roles = {}
        self.assignments = []
        for key, identifier in manifest.roles.items():
            scopes = (
                [cleanup.group_id(manifest.platform)]
                if key in {"certificateImporter", "acmeStateWriter"}
                else [
                    manifest.allocations[slot]["clusterResourceGroupId"]
                    for slot in manifest.children
                ]
            )
            self.roles[key] = {
                "id": identifier,
                "roleType": "CustomRole",
                "roleName": cleanup.ROLE_NAMES[key][1],
                "assignableScopes": scopes,
            }
            for index in range(2):
                scope = scopes[index % len(scopes)]
                name = uuid5(cleanup.GUID_NAMESPACE, key + str(index))
                self.assignments.append(
                    {
                        "id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}",
                        "scope": scope,
                        "roleDefinitionId": identifier,
                    }
                )
        self.assignments.append(
            {
                "id": f"/subscriptions/{cleanup.SUBSCRIPTION}/providers/"
                "Microsoft.Authorization/roleAssignments/ffffffff-ffff-ffff-ffff-ffffffffffff",
                "scope": f"/subscriptions/{cleanup.SUBSCRIPTION}",
                "roleDefinitionId": "unrelated-baseline-role",
            }
        )

    @staticmethod
    def resource(identifier, kind):
        return {"id": identifier, "type": kind, "tags": dict(cleanup.TAGS)}

    @staticmethod
    def radius_resource(app, kind, name, properties):
        scope = "/planes/radius/local/resourceGroups/radplanes"
        return {
            "id": f"{scope}/providers/{kind}/{name}",
            "name": name,
            "type": kind,
            "properties": {
                "application": f"{scope}/providers/Applications.Core/applications/{app}",
                "provisioningState": "Succeeded",
                **properties,
            },
        }

    def add_app(self, slot, name, environment, azure_scope):
        scope = "/planes/radius/local/resourceGroups/radplanes"
        self.apps[slot][name] = {
            "name": name,
            "id": f"{scope}/providers/Applications.Core/applications/{name}",
            "properties": {
                "environment": f"{scope}/providers/Applications.Core/environments/{environment}"
            },
        }
        self.environments[(slot, environment)] = {
            "properties": {
                "providers": {"azure": {"scope": azure_scope}},
                "compute": {"namespace": "radplanes-" + (slot if environment == slot else "p")},
            },
        }

    @staticmethod
    def value(args, flag):
        return args[args.index(flag) + 1]

    @staticmethod
    def mutates(args):
        return "delete" in args or "scale" in args

    def drop_cluster(self, slot):
        cluster = self.clusters.pop(slot)
        self.resources[cluster["resourceGroup"]] = []
        self.groups.pop(cluster["nodeResourceGroup"], None)
        self.resources.pop(cluster["nodeResourceGroup"], None)

    def run(self, args, *, env=None, **kwargs):
        self.calls.append((list(args), env))
        if self.fail(args):
            raise cleanup.ProvisioningError("mock_command_failed")
        if args[0] == "az":
            result = self.azure(args)
        elif args[0] == "rad":
            result = self.radius(args, env)
        elif args[0] == "kubectl":
            return self.kubernetes(args)
        else:
            raise AssertionError(f"Unexpected executable: {args[0]}")
        return "" if result is None else json.dumps(copy.deepcopy(result))

    def azure(self, args):
        def require(flag):
            return self.value(args, flag)

        action = args[1:3]
        if action == ["group", "exists"]:
            return require("--name") in self.groups
        if action == ["group", "show"]:
            return self.groups[require("--name")]
        if action == ["group", "list"]:
            return [g for g in self.groups.values() if g["tags"].get("project") == "radplanes"]
        if action == ["resource", "list"]:
            if "--resource-group" in args:
                return self.resources.get(require("--resource-group"), [])
            return [
                r
                for values in self.resources.values()
                for r in values
                if r.get("tags", {}).get("project") == "radplanes"
            ]
        if action == ["aks", "list"]:
            return [
                c
                for c in self.clusters.values()
                if c["resourceGroup"] == require("--resource-group")
            ]
        if action == ["aks", "delete"]:
            slot = next(
                slot for slot, value in self.clusters.items() if value["name"] == require("--name")
            )
            self.drop_cluster(slot)
            return None
        if action == ["group", "delete"]:
            group = require("--name")
            if self.leave_group:
                return None
            self.groups.pop(group)
            self.resources.pop(group, None)
            if group == self.manifest.platform:
                self.deleted_vaults.append(
                    {
                        "name": self.manifest.foundation["vaultName"],
                        "properties": {
                            "vaultId": self.manifest.foundation["vaultId"],
                            "scheduledPurgeDate": "2030-01-01T00:00:00Z",
                        },
                    }
                )
            return None
        if args[1:4] == ["role", "definition", "list"]:
            if "--name" in args:
                return [
                    role
                    for role in self.roles.values()
                    if role["id"].rsplit("/", 1)[-1] == require("--name")
                ]
            return [role for key, role in self.roles.items() if key not in self.hidden_roles]
        if args[1:4] == ["role", "assignment", "list"]:
            return self.assignments
        if args[1:4] == ["role", "assignment", "delete"]:
            identifier = require("--ids")
            self.assignments = [a for a in self.assignments if a["id"] != identifier]
            return None
        if args[1:4] == ["role", "definition", "delete"]:
            key = next(k for k, r in self.roles.items() if r["id"].endswith(require("--name")))
            assert not any(a["roleDefinitionId"] == self.roles[key]["id"] for a in self.assignments)
            if not self.leave_role:
                del self.roles[key]
            return None
        if action == ["keyvault", "list-deleted"]:
            return self.deleted_vaults
        raise AssertionError(f"Unexpected mocked Azure command: {args[:4]}")

    def radius(self, args, env):
        slot = Path(env["KUBECONFIG"]).stem
        action = args[3:5]
        if action == ["workspace", "show"]:
            return {
                "connection": {
                    "kind": "kubernetes",
                    "context": "radplanes-foreign"
                    if self.foreign_workspace
                    else f"radplanes-{slot}",
                },
                "scope": "/planes/radius/local/resourceGroups/radplanes",
            }
        if action == ["app", "list"]:
            return list(self.apps[slot].values())
        if action == ["environment", "show"]:
            result = copy.deepcopy(self.environments[(slot, args[5])])
            if self.foreign_env:
                result["properties"]["providers"]["azure"]["scope"] = cleanup.group_id("rg-foreign")
            return result
        if action == ["resource", "list"]:
            return self.app_resources[(slot, self.value(args, "--application"))]
        if action == ["app", "delete"]:
            app = args[5]
            if not self.leave_radius_app:
                del self.apps[slot][app]
                self.app_resources.pop((slot, app), None)
            if not self.leave_app_resource:
                self.resources[self.manifest.allocations[slot]["appResourceGroup"]] = []
            return None
        if action == ["resource", "delete"]:
            child = args[6]
            app = self.value(args, "--application")
            self.app_resources[(slot, app)] = []
            if child in self.clusters and not self.leave_cluster:
                self.drop_cluster(child)
            return None
        raise AssertionError(f"Unexpected mocked Radius command: {action}")

    def kubernetes(self, args):
        slot = Path(self.value(args, "--kubeconfig")).stem
        if any("cluster.server" in arg for arg in args):
            return (
                "https://foreign.invalid"
                if self.foreign_server
                else f"https://{self.clusters[slot]['fqdn']}"
            )
        if any("insecure-skip" in arg for arg in args):
            return "true" if self.insecure else ""
        if "namespace" in args:
            return (
                str(uuid5(cleanup.GUID_NAMESPACE, "foreign"))
                if self.foreign_uid
                else self.targets[slot]["clusterUid"]
            )
        if "deployments" in args:
            if self.value(args, "-n") != "radplanes-management-management":
                return '{"items":[]}'
            return json.dumps(
                {
                    "items": [
                        {"metadata": {"name": name, "labels": {"radapp.io/resource": name}}}
                        for name in ("management-api", "provisioner")
                    ]
                }
            )
        if "scale" in args:
            return ""
        if "pods" in args:
            return '{"items":[]}'
        raise AssertionError("Unexpected mocked Kubernetes command")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".state/test-cleanup"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = cleanup.Manifest(bootstrap())
        self.commands = FakeCommands(self.root, self.manifest)
        environment = patch.dict(os.environ, {"CONFIRM_AZURE": "yes"})
        environment.start()
        self.addCleanup(environment.stop)
        guard = patch.object(
            subprocess, "Popen", side_effect=AssertionError("Live commands forbidden")
        )
        guard.start()
        self.addCleanup(guard.stop)
        output = redirect_stderr(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def engine(self, *, execute=True, provider_only=False, radius_only=False):
        return cleanup.Cleanup(
            self.manifest,
            root=self.root,
            commands=self.commands,
            targets=self.commands.targets,
            execute=execute,
            provider_only=provider_only,
            radius_only=radius_only,
        )

    def mutations(self):
        return [args for args, _ in self.commands.calls if FakeCommands.mutates(args)]

    def test_preview_runs_no_mutations_and_no_direct_child_delete(self):
        result = self.engine(execute=False).clean()
        self.assertEqual(result["status"], "planned")
        self.assertEqual(self.mutations(), [])
        self.assertFalse(
            any(
                s["action"] == "bootstrap/emergency AKS" and s["name"] != "management"
                for s in result["steps"]
            )
        )

    def test_normal_cleanup_preserves_reverse_ownership_and_verifies_absence(self):
        result = self.engine().clean()
        self.assertEqual(result["status"], "clean")
        self.assertEqual(len(result["softDeletedVaults"]), 1)
        calls = self.mutations()
        child_apps = [
            i
            for i, a in enumerate(calls)
            if a[0] == "rad" and a[3:5] == ["app", "delete"] and a[5] in {"control", "data"}
        ]
        clusters = [
            i for i, a in enumerate(calls) if a[0] == "rad" and a[3:5] == ["resource", "delete"]
        ]
        management = next(
            i
            for i, a in enumerate(calls)
            if a[0] == "rad" and a[3:6] == ["app", "delete", "management"]
        )
        groups = [i for i, a in enumerate(calls) if a[:3] == ["az", "group", "delete"]]
        roles = [i for i, a in enumerate(calls) if a[:4] == ["az", "role", "definition", "delete"]]
        self.assertLess(max(child_apps), min(clusters))
        self.assertLess(max(clusters), management)
        self.assertLess(management, min(groups))
        self.assertLess(max(groups), min(roles))
        self.assertEqual(len(roles), 4)
        self.assertEqual(
            [a for a in calls if a[:3] == ["az", "aks", "delete"]][0][4], "aks-radplanes-management"
        )
        self.assertEqual(len(self.commands.assignments), 1)  # Unrelated subscription role retained.
        self.assertFalse(any("purge" in a for a in calls))

    def test_radius_only_removes_owners_in_order_but_retains_bootstrap_and_credentials(self):
        roles, assignments = copy.deepcopy((self.commands.roles, self.commands.assignments))
        result = self.engine(radius_only=True).clean()
        self.assertEqual(result["status"], "radius_resources_removed")
        self.assertTrue(result["foundationRetained"])
        self.assertEqual(set(self.commands.clusters), {"management"})
        self.assertIn(self.manifest.platform, self.commands.groups)
        self.assertTrue(self.commands.resources[self.manifest.platform])
        self.assertEqual((self.commands.roles, self.commands.assignments), (roles, assignments))
        self.assertFalse(any(args[0] == "az" for args in self.mutations()))
        self.assertFalse(any(args[:2] == ["az", "role"] for args, _ in self.commands.calls))
        for slot, allocation in self.manifest.allocations.items():
            self.assertEqual(self.commands.resources[allocation["appResourceGroup"]], [])
            self.assertFalse(self.commands.apps[slot])
            self.assertTrue((self.root / ".state/azure" / f"{slot}.kubeconfig").exists())
        calls = self.mutations()
        cluster_deletes = [
            i
            for i, args in enumerate(calls)
            if args[0] == "rad" and args[3:5] == ["resource", "delete"]
        ]
        management = next(
            i
            for i, args in enumerate(calls)
            if args[0] == "rad" and args[3:6] == ["app", "delete", "management"]
        )
        self.assertLess(max(cluster_deletes), management)

    def test_radius_only_retains_managed_node_resource_ownership_checks(self):
        node_group = self.manifest.allocations["shared-data"]["nodeResourceGroup"]
        self.commands.resources[node_group][0]["tags"]["project"] = "foreign"
        with self.assertRaisesRegex(cleanup.CleanupError, "lacks verified project tags"):
            self.engine(radius_only=True).clean()
        self.assertFalse(self.mutations())

    def test_radius_only_reports_but_does_not_inspect_node_groups_without_live_aks(self):
        slot = "shared-data"
        allocation = self.manifest.allocations[slot]
        self.commands.clusters.pop(slot)
        self.commands.resources[allocation["appResourceGroup"]] = []
        self.commands.fail = lambda args: allocation["nodeResourceGroup"] in args
        result = self.engine(radius_only=True, execute=False).clean()
        self.assertEqual(result["uninspectedManagedNodeGroups"], [allocation["nodeResourceGroup"]])
        self.assertFalse(self.mutations())
        self.assertIn(allocation["nodeResourceGroup"], self.commands.groups)

    def test_radius_only_is_not_a_provider_fallback_or_a_full_clean_claim(self):
        with self.assertRaisesRegex(cleanup.CleanupError, "modes cannot be combined"):
            self.engine(radius_only=True, provider_only=True)
        self.commands.leave_cluster = True
        with self.assertRaisesRegex(cleanup.CleanupError, "Child AKS still exists"):
            self.engine(radius_only=True).clean()
        self.assertFalse(any(args[0] == "az" for args in self.mutations()))

    def test_radius_only_preview_and_missing_management_fail_before_mutations(self):
        self.assertEqual(self.engine(radius_only=True, execute=False).clean()["status"], "planned")
        self.assertFalse(self.mutations())
        self.commands.clusters.pop("management")
        with self.assertRaisesRegex(cleanup.CleanupError, "requires management Radius"):
            self.engine(radius_only=True).clean()
        self.assertFalse(self.mutations())

    def test_radius_only_cli_rejects_credential_removal_before_commands(self):
        with (
            patch.object(
                sys,
                "argv",
                [
                    "clean-azure.py",
                    "--radius-only",
                    "--execute",
                    "--credential-file",
                    "credentials.json",
                ],
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cleanup.main(), 1)
        self.assertFalse(self.commands.calls)

    def test_azure_failure_names_the_operation_and_owned_target(self):
        self.commands.fail = lambda args: args[:3] == ["az", "group", "exists"]
        with self.assertRaisesRegex(
            cleanup.CleanupError, r"az group exists \(rg-radplanes-platform\) command failed"
        ):
            self.engine(radius_only=True).clean()
        self.assertFalse(self.mutations())

    def test_every_command_is_explicitly_scoped(self):
        self.engine().clean()
        for args, env in self.commands.calls:
            if args[0] == "az":
                self.assertEqual(FakeCommands.value(args, "--subscription"), cleanup.SUBSCRIPTION)
            elif args[0] == "rad":
                self.assertIn("--config", args)
                self.assertIn("--workspace", args)
                self.assertTrue(Path(env["HOME"]).is_relative_to(self.root / ".state/azure/homes"))
                self.assertTrue(Path(env["KUBECONFIG"]).is_relative_to(self.root / ".state/azure"))
            else:
                self.assertIn("--kubeconfig", args)
                self.assertIn("--context", args)

    def test_provider_only_requires_explicit_flag_and_still_checks_ownership(self):
        self.commands.fail = lambda args: args[0] in {"rad", "kubectl"}
        self.assertEqual(self.engine(provider_only=True).clean()["status"], "clean")
        self.assertFalse(any(a[0] in {"rad", "kubectl"} for a, _ in self.commands.calls))
        self.assertEqual(sum(a[:3] == ["az", "aks", "delete"] for a in self.mutations()), 3)

    def test_execute_without_confirmation_fails_before_commands(self):
        with patch.dict(os.environ, {"CONFIRM_AZURE": "no"}):
            with self.assertRaises(cleanup.CleanupError):
                self.engine()
        self.assertEqual(self.commands.calls, [])

    def test_optimized_python_keeps_confirmation_and_mutation_guards(self):
        path = ROOT / "scripts/operations/clean-azure.py"
        namespace = {"__name__": "optimized_cleanup", "__file__": str(path)}
        exec(compile(path.read_text(), str(path), "exec", optimize=2), namespace)
        error, engine_type = namespace["CleanupError"], namespace["Cleanup"]
        with patch.dict(os.environ, {"CONFIRM_AZURE": "no"}):
            with self.assertRaisesRegex(error, "Execution requires CONFIRM_AZURE=yes"):
                engine_type(self.manifest, root=self.root, commands=self.commands, execute=True)
        engine = engine_type(self.manifest, root=self.root, commands=self.commands, execute=True)
        with patch.dict(os.environ, {"CONFIRM_AZURE": "no"}):
            with self.assertRaisesRegex(error, "Mutation requires"):
                engine.az("group", "delete", "--name", self.manifest.platform, mutation=True)
        self.assertFalse(self.commands.calls)

    def test_tampered_manifest_project_subscription_groups_and_roles_are_rejected(self):
        mutations = [
            lambda d: d["foundation"].update(projectName="foreign"),
            lambda d: d["foundation"].update(subscriptionId="11111111-1111-1111-1111-111111111111"),
            lambda d: d["foundation"]["tags"].update(managedBy="foreign"),
            lambda d: d["allocations"][1].update(clusterResourceGroup="rg-foreign"),
            lambda d: d["allocations"][1].update(nodeResourceGroup="rg-foreign"),
            lambda d: d["allocations"][1].update(appResourceGroupId=cleanup.group_id("rg-foreign")),
            lambda d: d["foundation"]["roleDefinitionIds"].update(certificateImporter="foreign"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = bootstrap()
                mutate(value)
                with self.assertRaises(cleanup.CleanupError):
                    cleanup.Manifest(value)
        self.assertEqual(self.commands.calls, [])

    def test_foreign_group_tags_block_all_mutations_even_provider_only(self):
        self.commands.groups[self.manifest.platform]["tags"]["managedBy"] = "foreign"
        with self.assertRaises(cleanup.CleanupError):
            self.engine(provider_only=True).clean()
        self.assertEqual(self.mutations(), [])

    def test_foreign_resource_tags_and_untaggable_type_do_not_bypass_ownership(self):
        resource = self.commands.resources[self.manifest.platform][0]
        resource["type"] = "Microsoft.Authorization/roleAssignments"
        resource["tags"]["project"] = "foreign"
        with self.assertRaises(cleanup.CleanupError):
            self.engine(provider_only=True).clean()
        self.assertEqual(self.mutations(), [])

    def test_unmanifested_project_group_is_reported_not_prefix_deleted(self):
        self.commands.groups["rg-radplanes-foreign"] = {
            "name": "rg-radplanes-foreign",
            "tags": dict(cleanup.TAGS),
        }
        with self.assertRaises(cleanup.CleanupError):
            self.engine(provider_only=True).clean()
        self.assertEqual(self.mutations(), [])

    def test_group_helper_cannot_delete_a_name_absent_from_the_manifest(self):
        with self.assertRaisesRegex(cleanup.CleanupError, "not in the ownership manifest"):
            self.engine(provider_only=True).delete_group("rg-radplanes-foreign")
        self.assertEqual(self.commands.calls, [])

    def test_radius_output_reference_to_foreign_azure_resource_blocks_mutations(self):
        resource = self.commands.app_resources[("shared-control", "control")][0]
        resource["properties"]["outputResources"] = [
            {
                "id": (
                    f"{cleanup.group_id('rg-foreign')}/providers/"
                    "Microsoft.Storage/storageAccounts/foreign"
                ),
            }
        ]
        with self.assertRaisesRegex(cleanup.CleanupError, "unowned Azure scope"):
            self.engine().clean()
        self.assertEqual(self.mutations(), [])

    def test_foreign_custom_role_assignment_blocks_cleanup(self):
        assignment = self.commands.assignments[0]
        assignment["scope"] = cleanup.group_id("rg-foreign")
        with self.assertRaises(cleanup.CleanupError):
            self.engine(provider_only=True).clean()
        self.assertEqual(self.mutations(), [])

    def test_foreign_custom_role_definition_scope_blocks_cleanup(self):
        self.commands.roles["certificateImporter"]["assignableScopes"] = ["/"]
        with self.assertRaises(cleanup.CleanupError):
            self.engine(provider_only=True).clean()
        self.assertEqual(self.mutations(), [])

    def test_exact_role_lookup_rejects_foreign_id_before_mutations(self):
        original = self.commands.azure

        def changed(args):
            if args[:4] == ["az", "role", "definition", "list"] and "--name" in args:
                return [self.commands.roles["childClusterRecipe"]]
            return original(args)

        with patch.object(self.commands, "azure", side_effect=changed):
            with self.assertRaisesRegex(cleanup.CleanupError, "different role ID"):
                self.engine(provider_only=True).clean()
        self.assertEqual(self.mutations(), [])

    def test_exact_role_lookup_errors_are_not_absence(self):
        self.commands.hidden_roles = set(self.manifest.roles)
        self.commands.fail = lambda args: (
            args[:4] == ["az", "role", "definition", "list"] and "--name" in args
        )
        with self.assertRaisesRegex(cleanup.CleanupError, "command failed"):
            self.engine(provider_only=True).clean()
        self.assertEqual(self.mutations(), [])

    def test_exact_only_roles_still_require_verified_name_type_and_scopes(self):
        self.commands.hidden_roles.add("certificateImporter")
        original = copy.deepcopy(self.commands.roles["certificateImporter"])
        for change in (
            {"roleName": "foreign"},
            {"roleType": "BuiltInRole"},
            {"assignableScopes": ["/"]},
        ):
            with self.subTest(change=change):
                self.commands.roles["certificateImporter"] = {**original, **change}
                with self.assertRaises(cleanup.CleanupError):
                    self.engine(provider_only=True).clean()
                self.assertEqual(self.mutations(), [])

    def test_broad_role_inventory_still_rejects_unmanifested_project_role(self):
        self.commands.roles["unexpected"] = {
            "id": cleanup.role_id("certificateImporter") + "-foreign",
            "roleName": "radplanes unexpected",
        }
        with self.assertRaisesRegex(cleanup.CleanupError, "Unmanifested project custom role"):
            self.engine(provider_only=True).clean()
        self.assertEqual(self.mutations(), [])

    def test_orphaned_builtin_assignment_is_removed_only_at_owned_scope(self):
        scope = cleanup.group_id(self.manifest.platform)
        identifier = (
            f"{scope}/providers/Microsoft.Authorization/roleAssignments/"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        self.commands.assignments.append(
            {
                "id": identifier,
                "scope": scope,
                "roleDefinitionId": "builtin-project-grant",
            }
        )
        self.assertEqual(self.engine().clean()["status"], "clean")
        deleted = [a for a in self.mutations() if a[:4] == ["az", "role", "assignment", "delete"]]
        self.assertTrue(any(FakeCommands.value(a, "--ids") == identifier for a in deleted))
        self.assertEqual(len(self.commands.assignments), 1)

    def test_missing_radius_access_has_no_automatic_provider_fallback(self):
        self.commands.fail = lambda args: args[0] == "rad"
        with self.assertRaisesRegex(cleanup.CleanupError, "No automatic provider fallback"):
            self.engine().clean()
        self.assertEqual(self.mutations(), [])

    def test_radius_delete_failure_stops_before_any_provider_delete(self):
        self.commands.fail = lambda args: args[0] == "rad" and "delete" in args
        with self.assertRaises(cleanup.CleanupError):
            self.engine().clean()
        self.assertFalse(any(a[0] == "az" and "delete" in a for a in self.mutations()))

    def test_failed_partial_radius_resource_is_attempted_not_ignored(self):
        self.commands.app_resources[("shared-control", "control")][0]["properties"][
            "provisioningState"
        ] = "Failed"
        self.assertEqual(self.engine().clean()["status"], "clean")

    def test_nonterminal_radius_operation_blocks_mutations(self):
        self.commands.app_resources[("shared-control", "control")][0]["properties"][
            "provisioningState"
        ] = "Creating"
        with self.assertRaises(cleanup.CleanupError):
            self.engine().clean()
        self.assertEqual(self.mutations(), [])

    def test_remaining_app_resource_stops_before_cluster_deletion(self):
        self.commands.leave_app_resource = True
        with self.assertRaisesRegex(cleanup.CleanupError, "left app-group resources"):
            self.engine().clean()
        self.assertFalse(
            any(a[0] == "rad" and a[3:5] == ["resource", "delete"] for a in self.mutations())
        )

    def test_zero_exit_with_remaining_radius_app_stops_before_owner_or_provider_deletion(self):
        self.commands.leave_radius_app = True
        with self.assertRaisesRegex(cleanup.CleanupError, "Radius app deletion incomplete"):
            self.engine(radius_only=True).clean()
        self.assertIn("data", self.commands.apps["shared-data"])
        self.assertEqual(
            self.commands.resources[self.manifest.allocations["shared-data"]["appResourceGroup"]],
            [],
        )
        self.assertTrue(
            any(
                args[0] == "rad" and args[3:6] == ["app", "delete", "data"]
                for args in self.mutations()
            )
        )
        self.assertFalse(
            any(
                (args[0] == "rad" and args[3:5] == ["resource", "delete"])
                or (args[0] == "az" and "delete" in args)
                for args in self.mutations()
            )
        )

    def test_remaining_child_aks_prevents_management_teardown(self):
        self.commands.leave_cluster = True
        with self.assertRaisesRegex(cleanup.CleanupError, "Child AKS still exists"):
            self.engine().clean()
        self.assertFalse(
            any(
                a[0] == "rad" and a[3:6] == ["app", "delete", "management"]
                for a in self.mutations()
            )
        )

    def test_provider_failure_propagates_and_roles_are_retained(self):
        self.commands.fail = lambda args: args[:3] == ["az", "group", "delete"]
        with self.assertRaises(cleanup.CleanupError):
            self.engine(provider_only=True).clean()
        self.assertEqual(len(self.commands.roles), 4)

    def test_group_delete_acceptance_is_not_deletion_proof(self):
        self.commands.leave_group = True
        with self.assertRaisesRegex(cleanup.CleanupError, "has not completed"):
            self.engine(provider_only=True).clean()
        self.assertEqual(len(self.commands.roles), 4)

    def test_target_server_uid_workspace_scope_and_tls_are_checked(self):
        for field in (
            "foreign_server",
            "foreign_uid",
            "foreign_workspace",
            "foreign_env",
            "insecure",
        ):
            with self.subTest(field=field):
                setattr(self.commands, field, True)
                with self.assertRaises(cleanup.CleanupError):
                    self.engine().clean()
                self.assertEqual(self.mutations(), [])
                setattr(self.commands, field, False)

    def test_missing_child_export_is_not_silently_skipped(self):
        del self.commands.targets["shared-control"]
        with self.assertRaisesRegex(cleanup.CleanupError, "target is missing"):
            self.engine().clean()
        self.assertEqual(self.mutations(), [])

    def test_foreign_target_arm_id_is_rejected(self):
        self.commands.targets["shared-control"]["clusterId"] = self.manifest.cluster_id(
            "management"
        )
        with self.assertRaises(cleanup.CleanupError):
            self.engine().clean()
        self.assertEqual(self.mutations(), [])

    def test_global_and_symlinked_configuration_are_rejected(self):
        with self.assertRaises(cleanup.CleanupError):
            cleanup.state_file(self.root, Path.home() / ".kube/config")
        target = self.root / ".state/azure/link.kubeconfig"
        target.symlink_to(self.root / ".state/azure/management.kubeconfig")
        with self.assertRaises(cleanup.CleanupError):
            cleanup.state_file(self.root, target)

    def test_redirected_radius_home_fails_before_any_mutation(self):
        outside = self.root / "not-radius-home"
        outside.mkdir()
        (self.root / ".state/azure/homes").symlink_to(outside)
        with self.assertRaises(cleanup.ProvisioningError):
            self.engine().clean()
        self.assertEqual(self.mutations(), [])

    def test_changed_foundation_resource_name_is_not_deleted_with_old_manifest(self):
        resource = self.commands.resources[self.manifest.platform][0]
        resource["id"] += "-different-run"
        with self.assertRaisesRegex(cleanup.CleanupError, "different resource"):
            self.engine(provider_only=True).clean()
        self.assertEqual(self.mutations(), [])

    def test_verifier_never_mutates_and_refuses_running_resources(self):
        with self.assertRaises(cleanup.CleanupError):
            self.engine(execute=False).verify()
        self.assertEqual(self.mutations(), [])

    def test_verifier_detects_role_and_assignment_leftovers_after_groups_are_gone(self):
        self.commands.groups.clear()
        self.commands.resources.clear()
        self.commands.clusters.clear()
        with self.assertRaisesRegex(cleanup.CleanupError, "roles or assignments remain"):
            self.engine(execute=False).verify()
        self.assertEqual(self.mutations(), [])

    def test_verifier_finds_role_missing_from_broad_inventory(self):
        self.commands.groups.clear()
        self.commands.resources.clear()
        self.commands.clusters.clear()
        self.commands.assignments.clear()
        self.commands.hidden_roles = set(self.manifest.roles)
        with self.assertRaisesRegex(cleanup.CleanupError, "roles or assignments remain"):
            self.engine(execute=False).verify()
        names = {
            FakeCommands.value(args, "--name")
            for args, _ in self.commands.calls
            if args[:4] == ["az", "role", "definition", "list"] and "--name" in args
        }
        self.assertEqual(
            names, {value.rsplit("/", 1)[-1] for value in self.manifest.roles.values()}
        )
        self.assertEqual(self.mutations(), [])

    def test_cleanup_cannot_report_clean_when_exact_role_survives_delete(self):
        self.commands.hidden_roles = set(self.manifest.roles)
        self.commands.leave_role = True
        with self.assertRaisesRegex(cleanup.CleanupError, "roles or assignments remain"):
            self.engine(provider_only=True).clean()
        self.assertFalse(self.commands.groups)
        self.assertEqual(
            sum(args[:4] == ["az", "role", "definition", "delete"] for args in self.mutations()), 4
        )
        self.assertEqual(len(self.commands.roles), 4)

    def test_credentials_are_removed_only_after_verified_cleanup(self):
        state = self.root / ".state/azure"
        (state / "bootstrap.outputs.json").write_text(json.dumps(bootstrap()))
        credential = state / "credentials.json"
        credential.write_text("sensitive fixture")
        evidence = state / "endpoints.json"
        evidence.write_text("retained fixture")
        argv = [
            "clean-azure.py",
            "--provider-only",
            "--execute",
            "--credential-file",
            "credentials.json",
        ]
        with (
            patch.object(cleanup, "ROOT", self.root),
            patch.object(cleanup, "Commands", return_value=self.commands),
            patch.object(sys, "argv", argv),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cleanup.main(), 0)
        self.assertFalse(credential.exists())
        self.assertTrue(evidence.exists())
        self.assertTrue((state / "bootstrap.outputs.json").exists())

    def test_failed_cleanup_never_removes_credentials(self):
        state = self.root / ".state/azure"
        (state / "bootstrap.outputs.json").write_text(json.dumps(bootstrap()))
        credential = state / "credentials.json"
        credential.write_text("sensitive fixture")
        self.commands.fail = lambda args: args[:3] == ["az", "group", "delete"]
        with (
            patch.object(cleanup, "ROOT", self.root),
            patch.object(cleanup, "Commands", return_value=self.commands),
            patch.object(
                sys,
                "argv",
                [
                    "clean-azure.py",
                    "--provider-only",
                    "--execute",
                    "--credential-file",
                    "credentials.json",
                ],
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cleanup.main(), 1)
        self.assertTrue(credential.exists())

    def test_interrupt_during_provider_delete_reports_incomplete_and_retains_credentials(self):
        state = self.root / ".state/azure"
        (state / "bootstrap.outputs.json").write_text(json.dumps(bootstrap()))
        credential = state / "credentials.json"
        credential.write_text("sensitive fixture")

        def interrupted(args):
            if args[:3] == ["az", "group", "delete"]:
                raise KeyboardInterrupt
            return False

        self.commands.fail = interrupted
        error, output = io.StringIO(), io.StringIO()
        with (
            patch.object(cleanup, "ROOT", self.root),
            patch.object(cleanup, "Commands", return_value=self.commands),
            patch.object(
                sys,
                "argv",
                [
                    "clean-azure.py",
                    "--provider-only",
                    "--execute",
                    "--credential-file",
                    "credentials.json",
                ],
            ),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            self.assertEqual(cleanup.main(), 130)
        self.assertIn("Cleanup incomplete: interrupted", error.getvalue())
        self.assertIn("Azure operation may still be running", error.getvalue())
        self.assertIn("resources may remain", error.getvalue())
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(credential.exists())
        self.assertTrue(self.commands.groups)

    def test_late_interrupt_does_not_claim_credentials_were_retained(self):
        state = self.root / ".state/azure"
        (state / "bootstrap.outputs.json").write_text(json.dumps(bootstrap()))
        first, second = state / "credentials.json", state / "management.kubeconfig"
        first.write_text("sensitive fixture")
        events = []
        verify, unlink = cleanup.Cleanup.verify, Path.unlink

        def verified(engine):
            result = verify(engine)
            events.append("verified")
            return result

        def interrupted(path, *args, **kwargs):
            self.assertEqual(events[0], "verified")
            if path == second:
                raise KeyboardInterrupt
            events.append("removed")
            return unlink(path, *args, **kwargs)

        error, output = io.StringIO(), io.StringIO()
        with (
            patch.object(cleanup, "ROOT", self.root),
            patch.object(cleanup, "Commands", return_value=self.commands),
            patch.object(cleanup.Cleanup, "verify", verified),
            patch.object(Path, "unlink", interrupted),
            patch.object(
                sys,
                "argv",
                [
                    "clean-azure.py",
                    "--provider-only",
                    "--execute",
                    "--credential-file",
                    first.name,
                    "--credential-file",
                    second.name,
                ],
            ),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            self.assertEqual(cleanup.main(), 130)
        self.assertEqual(events, ["verified", "removed"])
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertIn("credential removal may be partial", error.getvalue())
        self.assertNotIn("no credentials removed", error.getvalue().lower())
        self.assertEqual(output.getvalue(), "")

    def test_evidence_cannot_be_requested_as_a_credential_file(self):
        state = self.root / ".state/azure"
        (state / "bootstrap.outputs.json").write_text(json.dumps(bootstrap()))
        with (
            patch.object(cleanup, "ROOT", self.root),
            patch.object(cleanup, "Commands", return_value=self.commands),
            patch.object(
                sys,
                "argv",
                [
                    "clean-azure.py",
                    "--provider-only",
                    "--execute",
                    "--credential-file",
                    "bootstrap.outputs.json",
                ],
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cleanup.main(), 1)
        self.assertEqual(self.mutations(), [])

    def test_verify_entrypoint_delegates_only_read_only_mode(self):
        spec = importlib.util.spec_from_file_location(
            "verify_cleanup_tests", ROOT / "scripts/operations/verify-clean.py"
        )
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        delegate = unittest.mock.Mock()
        delegate.main.return_value = 0
        with patch.object(verifier.importlib.util, "module_from_spec", return_value=delegate):
            with patch.object(verifier.importlib.util, "spec_from_file_location") as factory:
                factory.return_value.name = "verify_cleanup_fake"
                self.assertEqual(verifier.main(), 0)
        delegate.main.assert_called_once_with(verify_only=True)


if __name__ == "__main__":
    unittest.main()
