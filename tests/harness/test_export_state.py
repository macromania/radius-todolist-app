import base64
import copy
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock
from uuid import NAMESPACE_DNS, uuid4, uuid5

SPEC = importlib.util.spec_from_file_location(
    "operator_state_exporter", Path(__file__).resolve().parents[2] / "harness/export-state.py"
)
export_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = export_module
SPEC.loader.exec_module(export_module)

CONTRACT = importlib.util.spec_from_file_location(
    "export_acceptance_contract",
    Path(__file__).resolve().parents[2] / "harness/fault-parent-link.py",
)
contract = importlib.util.module_from_spec(CONTRACT)
sys.modules[CONTRACT.name] = contract
CONTRACT.loader.exec_module(contract)

SLOTS = ["management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data"]
SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"


def uid(value):
    return str(uuid5(NAMESPACE_DNS, value))


def configuration():
    prefix = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/"
    vnet = prefix + "rg-radplanes-platform/providers/Microsoft.Network/virtualNetworks/project"
    allocations = {}
    for index, slot in enumerate(SLOTS):
        cluster_group, app_group = f"rg-radplanes-{slot}-cluster", f"rg-radplanes-{slot}-app"
        allocations[slot] = {
            "slot": slot,
            "clusterName": "aks-radplanes-" + slot,
            "clusterResourceGroup": cluster_group,
            "appResourceGroup": app_group,
            "clusterResourceGroupId": prefix + cluster_group,
            "appResourceGroupId": prefix + app_group,
            "postgresqlSubnetId": vnet + "/subnets/snet-" + slot + "-postgresql",
            "apiPrivateIp": f"10.64.{index}.240",
            "challengePrivateIp": f"10.64.{index}.241",
            "gatewaySubnetCidr": f"10.64.{16 + index}.0/24",
        }
    management = allocations["management"]
    return {
        "version": 1,
        "foundation": {
            "projectName": "radplanes",
            "location": "centralus",
            "subscriptionId": SUBSCRIPTION,
            "virtualNetworkId": vnet,
            "registryLoginServer": "project.azurecr.io",
        },
        # Deliberately reverse keys: role index must never depend on JSON dictionary order.
        "allocations": dict(reversed(list(allocations.items()))),
        "images": {
            "api": "project.azurecr.io/api@sha256:" + "a" * 64,
            "provisioner": "project.azurecr.io/provisioner@sha256:" + "b" * 64,
        },
        "managementCluster": {
            "id": management["clusterResourceGroupId"]
            + "/providers/Microsoft.ContainerService/managedClusters/"
            + management["clusterName"]
        },
    }


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def sleep(self, value):
        self.value += value


class Platform:
    def __init__(self, config):
        self.config = config
        self.allocations = config["allocations"]
        self.calls = []
        self.present = set(SLOTS)
        self.forbidden = set()
        self.missing_key = set()
        self.wrong_subnet = False
        self.wrong_owner = False
        self.no_https = set()
        self.namespaces = {slot: uid("namespace-" + slot) for slot in SLOTS}
        self.requested = [
            {"tenant_id": "shared-a", "pair_id": "shared", "isolation": "shared", "ready": True},
            {"tenant_id": "shared-b", "pair_id": "shared", "isolation": "shared", "ready": True},
            {
                "tenant_id": "isolated-c",
                "pair_id": "isolated-1",
                "isolation": "isolated",
                "ready": True,
            },
        ]

    def key(self, slot):
        return "synthetic-" + slot + "-" + "x" * 40

    def role(self, slot):
        return "management" if slot == "management" else slot.rsplit("-", 1)[1]

    def cluster_id(self, slot):
        value = self.allocations[slot]
        return (
            value["clusterResourceGroupId"]
            + "/providers/Microsoft.ContainerService/managedClusters/"
            + value["clusterName"]
        )

    def url(self, slot):
        return f"https://actual-{slot}.centralus.cloudapp.azure.com"

    def tags(self):
        return {
            "project": "other" if self.wrong_owner else "radplanes",
            "managedBy": "radius-todolist-app",
        }

    def azure_slot(self, args, kind):
        field = {"aks": "clusterName", "gateway": "appResourceGroup"}[kind]
        flag = "--name" if kind == "aks" else "--resource-group"
        return next(
            slot
            for slot, value in self.allocations.items()
            if value[field] == args[args.index(flag) + 1]
        )

    def execute(self, args, **_kwargs):
        self.calls.append(args)
        try:
            output = self.response(args)
            return subprocess.CompletedProcess(args, 0, output, "")
        except PermissionError:
            return subprocess.CompletedProcess(
                args, 1, "", "ERROR: (AuthorizationFailed) sensitive"
            )
        except FileNotFoundError:
            return subprocess.CompletedProcess(args, 3, "", "ERROR: (ResourceNotFound) not created")

    def response(self, args):
        if args[0] == "kubelogin":
            return ""
        if args[:3] == ["az", "aks", "show"]:
            slot = self.azure_slot(args, "aks")
            if slot in self.forbidden:
                raise PermissionError
            if slot not in self.present:
                raise FileNotFoundError
            return json.dumps(
                {
                    "id": self.cluster_id(slot),
                    "tags": self.tags(),
                    "provisioningState": "Succeeded",
                    "fqdn": slot + ".aks.example.test",
                    "disableLocalAccounts": True,
                    "aadProfile": {"managed": True, "enableAzureRbac": True},
                }
            )
        if args[:3] == ["az", "aks", "get-credentials"]:
            slot = self.azure_slot(args, "aks")
            context = args[args.index("--context") + 1]
            path = Path(args[args.index("--file") + 1])
            path.write_text(
                json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Config",
                        "current-context": context,
                        "contexts": [
                            {"name": context, "context": {"cluster": "scoped", "user": "operator"}}
                        ],
                        "clusters": [
                            {
                                "name": "scoped",
                                "cluster": {
                                    "server": "https://" + slot + ".aks.example.test",
                                    "certificate-authority-data": "cHVibGljLWNh",
                                },
                            }
                        ],
                        "users": [
                            {
                                "name": "operator",
                                "user": {
                                    "exec": {
                                        "command": "kubelogin",
                                        "args": ["get-token", "--login", "azurecli"],
                                    }
                                },
                            }
                        ],
                    }
                )
            )
            path.chmod(0o600)
            return ""
        if args[:5] == ["az", "network", "vnet", "subnet", "show"]:
            resource_id = args[args.index("--ids") + 1]
            slot = next(
                slot
                for slot, value in self.allocations.items()
                if value["postgresqlSubnetId"] == resource_id
            )
            prefix = f"10.64.{48 + SLOTS.index(slot)}.0/27"
            return json.dumps(
                {
                    "id": resource_id,
                    "addressPrefix": "10.99.0.0/24" if self.wrong_subnet else prefix,
                    "delegations": [{"serviceName": "Microsoft.DBforPostgreSQL/flexibleServers"}],
                }
            )
        if args[:4] == ["az", "network", "application-gateway", "list"]:
            slot = self.azure_slot(args, "gateway")
            group = self.allocations[slot]["appResourceGroupId"]
            gateway = group + "/providers/Microsoft.Network/applicationGateways/gateway"
            frontend = gateway + "/frontendIPConfigurations/public"
            return json.dumps(
                [
                    {
                        "id": gateway,
                        "name": "gateway",
                        "tags": self.tags(),
                        "provisioningState": "Succeeded",
                        "httpListeners": [
                            {
                                "protocol": "Http" if slot in self.no_https else "Https",
                                "hostName": self.url(slot).removeprefix("https://"),
                                "frontendIPConfiguration": {"id": frontend},
                            }
                        ],
                        "frontendIPConfigurations": [
                            {
                                "id": frontend,
                                "publicIPAddress": {
                                    "id": group
                                    + "/providers/Microsoft.Network/publicIPAddresses/public"
                                },
                            }
                        ],
                    }
                ]
            )
        if args[:4] == ["az", "network", "public-ip", "show"]:
            resource_id = args[args.index("--ids") + 1]
            slot = next(
                slot
                for slot, value in self.allocations.items()
                if resource_id.startswith(value["appResourceGroupId"] + "/")
            )
            return json.dumps(
                {
                    "id": resource_id,
                    "tags": self.tags(),
                    "dnsSettings": {"fqdn": self.url(slot).removeprefix("https://")},
                }
            )
        if args[0] == "kubectl":
            slot = args[args.index("--context") + 1].removeprefix("radplanes-")
            if slot in self.forbidden:
                raise PermissionError
            arguments = args[args.index("--request-timeout=15s") + 1 :]
            return self.kubernetes(slot, arguments)
        raise AssertionError("unexpected operator command")

    def kubernetes(self, slot, args):
        if args[:3] == ["get", "namespace", "kube-system"]:
            return json.dumps({"metadata": {"uid": uid("cluster-" + slot)}})
        if args[:2] == ["get", "namespace"]:
            return (
                json.dumps({"metadata": {"uid": self.namespaces[slot]}})
                if slot in self.present
                else ""
            )
        role = self.role(slot)
        if args[:2] == ["get", "deployments"]:
            components = export_module.component_names(role)
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {
                                "name": "deployment-" + name,
                                "uid": uid(slot + "-dep-" + name),
                            },
                            "spec": {
                                "replicas": 1,
                                "template": {
                                    "metadata": {
                                        "labels": {
                                            "plane-demo/project": "radplanes",
                                            "plane-demo/component": name,
                                        }
                                    }
                                },
                            },
                        }
                        for name in components
                    ]
                }
            )
        if args[:2] == ["get", "pods"]:
            component = args[args.index("-l") + 1].split("plane-demo/component=")[1]
            container = {
                "name": "container-" + component,
                "command": [
                    "python",
                    "-m",
                    {
                        "management-api": "plane_demo.management.api",
                        "provisioner": "plane_demo.management.provisioner",
                        "control-api": "plane_demo.control.api",
                        "control-reconciler": "plane_demo.control.reconciler",
                        "data-api": "plane_demo.data.api",
                        "data-reconciler": "plane_demo.data.reconciler",
                    }[component],
                ],
                "image": self.config["images"][
                    "provisioner" if component == "provisioner" else "api"
                ],
            }
            if component.endswith("-api"):
                container["envFrom"] = [{"secretRef": {"name": component + "-runtime"}}]
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {
                                "name": "pod-" + component,
                                "uid": uid(slot + "-pod-" + component),
                                "ownerReferences": [
                                    {
                                        "kind": "ReplicaSet",
                                        "name": "rs-" + component,
                                        "uid": uid(slot + "-rs-" + component),
                                    }
                                ],
                            },
                            "status": {"phase": "Running"},
                            "spec": {"containers": [container]},
                        }
                    ]
                }
            )
        if args[:2] == ["get", "replicaset"]:
            component = args[2].removeprefix("rs-")
            return json.dumps(
                {
                    "metadata": {
                        "uid": uid(slot + "-rs-" + component),
                        "ownerReferences": [
                            {"kind": "Deployment", "uid": uid(slot + "-dep-" + component)}
                        ],
                    }
                }
            )
        if args[:2] == ["get", "secret"]:
            assert args[2] == role + "-api-runtime"
            assert args[-1] == 'jsonpath={.metadata.uid}{"\\n"}{.data.DEMO_KEY}'
            return (
                uid("secret-" + slot)
                + "\n"
                + (
                    ""
                    if slot in self.missing_key
                    else base64.b64encode(self.key(slot).encode()).decode()
                )
            )
        if args[0] == "exec":
            code = args[args.index("-c", args.index("--") + 1) + 1]
            if code == export_module.PARENT_PROBE:
                parent = (
                    "management" if role == "control" else slot.removesuffix("-data") + "-control"
                )
                return json.dumps({"host": parent + ".postgres.database.azure.com", "port": 5432})
            assert code == export_module.INVENTORY_PROBE
            pairs = []
            for pair in ("shared", "isolated-1"):
                available = all(pair + "-" + role in self.present for role in ("control", "data"))
                pairs.append(
                    {
                        "pair_id": pair,
                        "stage": "available" if available else "allocated",
                        **{
                            role + "_cluster_id": self.cluster_id(pair + "-" + role)
                            if available
                            else None
                            for role in ("control", "data")
                        },
                        **{
                            role + "_url": self.url(pair + "-" + role) if available else None
                            for role in ("control", "data")
                        },
                    }
                )
            return json.dumps({"pairs": pairs, "tenants": self.requested})
        raise AssertionError("unexpected Kubernetes operation")


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(".state/azure") / ("export-unit-" + uuid4().hex)
        self.root.mkdir(parents=True, mode=0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root))
        self.values = configuration()
        self.config = self.root / "provisioning.json"
        self.config.write_text(json.dumps(self.values))
        self.platform = Platform(self.values)
        self.clock = Clock()

    def exporter(self):
        return export_module.Exporter(
            self.config,
            execute=self.platform.execute,
            health=Mock(),
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def test_complete_output_loads_with_existing_acceptance_configuration(self):
        exporter = self.exporter()
        messages = []
        self.assertEqual(exporter.run(watch=False, emit=messages.append), 0)
        status = json.loads(messages[-1])
        self.assertEqual(status["outcome"], "export_complete")
        self.assertEqual(set(status["ready_slots"]), set(SLOTS))
        config = contract.Configuration(self.root / "acceptance.json")
        for slot in SLOTS:
            target = config.target(slot)
            role = self.platform.role(slot)
            self.assertEqual(
                target.component(role + "-api")["container"], "container-" + role + "-api"
            )
        endpoints = json.loads(config.file(config.current()["endpoints_file"]).read_text())
        self.assertEqual(
            endpoints["pairs"]["shared"]["control"]["url"], self.platform.url("shared-control")
        )
        public = json.dumps(config.current()) + json.dumps(endpoints) + "".join(messages)
        for slot in SLOTS:
            self.assertEqual((self.root / (slot + ".key")).stat().st_mode & 0o777, 0o600)
            self.assertNotIn(self.platform.key(slot), public)
        self.assertFalse(exporter.work.exists())

    def test_subnet_comes_from_matching_parent_role_not_dictionary_order(self):
        exporter = self.exporter()
        exporter.run(watch=False, emit=lambda _: None)
        targets = json.loads((self.root / "acceptance.json").read_text())["targets"]
        self.assertEqual(targets["shared-control"]["parent"]["allowed_cidrs"], ["10.64.48.0/27"])
        self.assertEqual(targets["shared-data"]["parent"]["allowed_cidrs"], ["10.64.49.0/27"])
        self.assertEqual(targets["isolated-1-data"]["parent"]["allowed_cidrs"], ["10.64.51.0/27"])

    def test_cloud_harness_three_hour_timeout_is_supported(self):
        self.assertEqual(self.exporter().run(watch=False, timeout=10800, emit=lambda _: None), 0)

    def test_timeout_above_cloud_harness_limit_is_rejected(self):
        with self.assertRaisesRegex(export_module.ExportError, "invalid_export_timeout"):
            self.exporter().run(watch=False, timeout=10801, emit=lambda _: None)
        self.assertEqual(self.platform.calls, [])

    def test_commands_are_scoped_operator_reads_not_resource_creation(self):
        self.exporter().run(watch=False, emit=lambda _: None)
        for args in self.platform.calls:
            self.assertNotIn("--admin", args)
            self.assertNotIn("credentials.json", " ".join(args))
            self.assertNotEqual(args[0], "rad")
            if args[0] == "az":
                self.assertEqual(args[args.index("--subscription") + 1], SUBSCRIPTION)
                self.assertNotIn("create", args)
                self.assertNotIn("delete", args)
            if args[0] == "kubectl":
                self.assertIn("--kubeconfig", args)
                self.assertIn("--context", args)
                self.assertIn("--namespace", args)
                self.assertIn(args[args.index("--request-timeout=15s") + 1], {"get", "exec"})
            if args[:3] == ["az", "aks", "get-credentials"]:
                self.assertTrue(
                    Path(args[args.index("--file") + 1]).is_relative_to(self.root.resolve())
                )

    def test_once_missing_children_is_incomplete_but_management_is_usable(self):
        self.platform.present = {"management"}
        self.platform.requested = [dict(self.platform.requested[0], ready=False)]
        messages = []
        self.assertEqual(self.exporter().run(watch=False, emit=messages.append), 3)
        value = json.loads(messages[-1])
        self.assertEqual(value["outcome"], "waiting")
        self.assertTrue(value["ready_for_onboarding"])
        self.assertEqual(value["published_slots"], ["management"])
        self.assertEqual(
            set(json.loads((self.root / "acceptance.json").read_text())["targets"]), {"management"}
        )

    def test_starting_status_clears_stale_readiness_before_cloud_reads(self):
        exporter = self.exporter()
        original = self.platform.execute

        def checked(args, **kwargs):
            if not self.platform.calls:
                status = json.loads((self.root / "export-status.json").read_text())
                self.assertEqual(status["outcome"], "starting")
                self.assertFalse(status["ready_for_onboarding"])
            return original(args, **kwargs)

        exporter.execute = checked
        exporter.run(watch=False, emit=lambda _: None)

    def test_unrequested_children_do_not_trigger_cloud_lookups(self):
        self.platform.present = {"management"}
        self.platform.requested = []
        self.assertEqual(self.exporter().run(watch=False, emit=lambda _: None), 3)
        shows = [args for args in self.platform.calls if args[:3] == ["az", "aks", "show"]]
        self.assertEqual(len(shows), 1)

    def test_auth_failure_is_fatal_not_pending_and_does_not_leak_stderr(self):
        self.platform.forbidden.add("shared-control")
        exporter = self.exporter()
        with self.assertRaisesRegex(export_module.ExportError, "operator_command_failed"):
            exporter.run(watch=True, emit=lambda _: None)
        value = (self.root / "export-status.json").read_text()
        self.assertNotIn("sensitive", value)
        self.assertEqual(json.loads(value)["outcome"], "failed")

    def test_resource_not_found_does_not_hide_authorization_failure_containing_that_word(self):
        exporter = self.exporter()
        exporter.execute = Mock(
            return_value=subprocess.CompletedProcess(
                ["az"],
                1,
                "",
                "ERROR: (AuthorizationFailed) ResourceNotFound in unrelated diagnostic",
            )
        )
        with self.assertRaises(export_module.ExportError):
            exporter.run_command(["az", "aks", "show"], missing=True)

    def test_aks_not_found_during_creation_is_pending_only_for_optional_child_lookup(self):
        exporter = self.exporter()
        original = exporter.execute

        def not_found(args, **kwargs):
            if args[:3] == ["az", "aks", "show"] and "aks-radplanes-shared-data" in args:
                return subprocess.CompletedProcess(
                    args,
                    3,
                    "",
                    "ERROR: (NotFound) Could not find managed cluster resource: "
                    "aks-radplanes-shared-data in subscription: example.\n"
                    "Exception Details: (Unspecified) rpc error: code = NotFound",
                )
            return original(args, **kwargs)

        exporter.execute = not_found
        self.assertEqual(exporter.run(watch=False, emit=lambda _: None), 3)
        status = json.loads((self.root / "export-status.json").read_text())
        self.assertTrue(status["ready_for_onboarding"])
        self.assertEqual(status["pending_slots"]["shared-data"], "cluster_not_created")
        with self.assertRaisesRegex(export_module.ExportError, "operator_command_failed"):
            exporter.run_command(
                ["az", "aks", "show", "--name", "aks-radplanes-shared-data"], missing=False
            )

    def test_missing_child_preserves_previous_published_generation(self):
        exporter = self.exporter()
        exporter.sample()
        original = (self.root / "acceptance.json").read_bytes()
        endpoints = (self.root / "endpoints.json").read_bytes()
        self.platform.present.remove("shared-data")
        value = exporter.sample()
        self.assertEqual(value["outcome"], "waiting")
        self.assertIn("shared-data", value["pending_slots"])
        self.assertEqual((self.root / "acceptance.json").read_bytes(), original)
        self.assertEqual((self.root / "endpoints.json").read_bytes(), endpoints)

    def test_rerun_preserves_generation_and_repairs_missing_exports(self):
        self.exporter().run(watch=False, emit=lambda _: None)
        original = (self.root / "acceptance.json").read_bytes()
        for name in ("endpoints.json", "cleanup-targets.json", "cleanup-radius.yaml"):
            (self.root / name).unlink()
        for slot in SLOTS:
            path = self.root / (slot + ".kubeconfig")
            path.write_text(json.dumps(export_module.yaml.safe_load(path.read_text())))
        self.exporter().run(watch=False, emit=lambda _: None)
        self.assertEqual((self.root / "acceptance.json").read_bytes(), original)
        acceptance = json.loads(original)
        self.assertEqual(
            json.loads((self.root / "endpoints.json").read_text()),
            json.loads((self.root / acceptance["endpoints_file"]).read_text()),
        )
        self.assertEqual(
            set(json.loads((self.root / "cleanup-targets.json").read_text())["targets"]), set(SLOTS)
        )
        self.assertTrue((self.root / "cleanup-radius.yaml").is_file())

    def test_single_writer_lock_prevents_parallel_snapshot_regression(self):
        first = self.exporter()
        with first.take_lock():
            second = self.exporter()
            with self.assertRaisesRegex(export_module.ExportError, "exporter_already_running"):
                second.run(watch=True, emit=lambda _: None)
        self.assertEqual(self.platform.calls, [])

    def test_cross_plane_public_ip_is_rejected_before_it_is_read(self):
        original = self.platform.execute

        def changed(args, **kwargs):
            result = original(args, **kwargs)
            if args[:4] == ["az", "network", "application-gateway", "list"]:
                value = json.loads(result.stdout)
                value[0]["frontendIPConfigurations"][0]["publicIPAddress"]["id"] = (
                    self.values["allocations"]["shared-data"]["appResourceGroupId"]
                    + "/providers/Microsoft.Network/publicIPAddresses/other"
                )
                return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
            return result

        exporter = self.exporter()
        exporter.execute = changed
        with self.assertRaisesRegex(export_module.ExportError, "public_ip_outside_allocation"):
            exporter.run(watch=False, emit=lambda _: None)
        self.assertFalse(
            any(args[:4] == ["az", "network", "public-ip", "show"] for args in self.platform.calls)
        )

    def test_watch_retries_at_five_seconds_and_stops_only_when_complete(self):
        self.platform.present = {"management"}
        self.platform.requested = []
        exporter = self.exporter()
        messages = []

        def sleep(value):
            self.clock.sleep(value)
            self.platform.present = set(SLOTS)
            self.platform.requested = Platform(self.values).requested

        exporter.sleep = sleep
        self.assertEqual(exporter.run(watch=True, timeout=30, emit=messages.append), 0)
        self.assertEqual(self.clock.value, 5)
        self.assertEqual(
            [json.loads(value)["outcome"] for value in messages], ["waiting", "export_complete"]
        )

    def test_watch_timeout_is_failure_not_completed_shape(self):
        self.platform.present = {"management"}
        self.platform.requested = []
        exporter = self.exporter()
        with self.assertRaisesRegex(export_module.ExportError, "export_timeout"):
            exporter.run(watch=True, timeout=10, emit=lambda _: None)
        self.assertEqual(
            json.loads((self.root / "export-status.json").read_text())["outcome"], "failed"
        )
        self.assertFalse(exporter.work.exists())

    def test_actual_subnet_disagreement_fails(self):
        self.platform.wrong_subnet = True
        with self.assertRaisesRegex(export_module.ExportError, "parent_postgresql_subnet_mismatch"):
            self.exporter().run(watch=False, emit=lambda _: None)

    def test_foreign_azure_resource_tags_fail_before_kubeconfig_download(self):
        self.platform.wrong_owner = True
        with self.assertRaisesRegex(export_module.ExportError, "ownership_mismatch"):
            self.exporter().run(watch=False, emit=lambda _: None)
        self.assertFalse(
            any(args[:3] == ["az", "aks", "get-credentials"] for args in self.platform.calls)
        )

    def test_existing_namespace_identity_change_is_rejected(self):
        exporter = self.exporter()
        exporter.sample()
        original = (self.root / "acceptance.json").read_bytes()
        self.platform.namespaces["shared-data"] = str(uuid4())
        with self.assertRaisesRegex(export_module.ExportError, "published_target_identity_changed"):
            exporter.sample()
        self.assertEqual((self.root / "acceptance.json").read_bytes(), original)

    def test_present_secret_without_demo_key_fails_instead_of_waiting(self):
        self.platform.missing_key.add("management")
        with self.assertRaisesRegex(export_module.ExportError, "api_runtime_demo_key_missing"):
            self.exporter().run(watch=False, emit=lambda _: None)

    def test_plaintext_gateway_is_not_exported(self):
        self.platform.no_https.add("shared-data")
        self.assertEqual(self.exporter().run(watch=False, emit=lambda _: None), 3)
        endpoints = json.loads((self.root / "endpoints.json").read_text())
        self.assertNotIn("data", endpoints["pairs"]["shared"])

    def test_foreign_allocation_fails_before_any_external_command(self):
        values = copy.deepcopy(self.values)
        values["allocations"]["shared-data"]["appResourceGroupId"] = (
            "/subscriptions/other/groups/wrong"
        )
        self.config.write_text(json.dumps(values))
        with self.assertRaises(export_module.ExportError):
            self.exporter()
        self.assertEqual(self.platform.calls, [])

    def test_probe_programs_are_read_only_and_parse(self):
        for name, code in (
            ("inventory", export_module.INVENTORY_PROBE),
            ("parent", export_module.PARENT_PROBE),
        ):
            compile(code, name, "exec")
            self.assertNotIn("INSERT ", code)
            self.assertNotIn("UPDATE ", code)
            self.assertNotIn("DELETE ", code)
        self.assertIn("SET TRANSACTION READ ONLY", export_module.INVENTORY_PROBE)

    def test_cli_help_has_no_external_calls(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                export_module.main(["--help"])
        self.assertEqual(error.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
