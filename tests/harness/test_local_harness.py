import base64
import copy
import hashlib
import importlib.util
import io
import json
import shutil
import socket
import stat
import subprocess
import sys
import unittest
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import NAMESPACE_DNS, uuid4, uuid5

import redis
import test_acceptance as existing
from redis.connection import Connection

from plane_demo.shared import settings

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "local_harness_export_under_test", ROOT / "harness/local/export-state.py"
)
export = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = export
SPEC.loader.exec_module(export)
runner, shared, network = export.acceptance, export.shared, export.network
faults = runner.faults
SLOTS = network.SLOTS
REVISION = "9" * 40
SOURCE = {
    "commit": REVISION,
    "committed_at": "2026-09-09T09:00:00+00:00",
    "worktree_dirty": False,
}
SOURCES = {"src/plane_demo/probe.py": "a" * 64}


def uid(value):
    return str(uuid5(NAMESPACE_DNS, value))


def encoded(text):
    return base64.b64encode(text.encode()).decode()


def profile(slot, address):
    context = "radplanes-local-" + slot
    cluster = {
        "server": address,
        "certificate-authority-data": encoded("-----BEGIN CERTIFICATE-----\n" + slot),
    }
    if slot != "management":
        cluster["tls-server-name"] = context
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": context,
        "contexts": [{"name": context, "context": {"cluster": context, "user": context}}],
        "clusters": [{"name": context, "cluster": cluster}],
        "users": [
            {
                "name": context,
                "user": {
                    "client-certificate-data": encoded("-----BEGIN CERTIFICATE-----\nclient"),
                    "client-key-data": encoded("-----BEGIN PRIVATE KEY-----\nsynthetic"),
                },
            }
        ],
    }


class Operator:
    """Bounded native-command simulator; unknown commands fail, never run subprocesses."""

    def __init__(self):
        self.calls = []
        self.all_ready = False
        self.error_on = None
        self.foreign_secret = False
        self.changed_ca = False
        self.changed_uid = False
        self.changed_key = False
        self.rules = False
        self.extra_rules = ""
        self.add_fails_after_mutation = False
        self.delete_fails = False
        self.sandbox_changed = False
        self.parent_blocked = True
        self.existing_survives = False
        self.local_ok = True
        self.image_mapping_wrong = False
        self.running_source_wrong = False
        self.running_ids = {}
        self.blobs = {}
        self.restore_cost = 0
        self.clock = existing.Clock()
        self.node_ids = {slot: hashlib.sha256(slot.encode()).hexdigest() for slot in SLOTS}
        self.addresses = {slot: f"172.18.0.{index + 2}" for index, slot in enumerate(SLOTS)}
        self.image_ids = {"api": "sha256:" + "a" * 64, "provisioner": "sha256:" + "b" * 64}

    def node(self, slot):
        return {
            "Id": self.node_ids[slot],
            "Name": f"/radplanes-local-{slot}-control-plane",
            "Config": {
                "Labels": {"io.x-k8s.kind.cluster": f"radplanes-local-{slot}"},
                "Image": network.NODE_IMAGE,
            },
            "HostConfig": {
                "PortBindings": {
                    "6443/tcp": [
                        {"HostIp": "127.0.0.1", "HostPort": str(35495 + SLOTS.index(slot))}
                    ],
                    "31480/tcp": [
                        {"HostIp": "127.0.0.1", "HostPort": str(35490 + SLOTS.index(slot))}
                    ],
                }
            },
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {"kind": {"IPAddress": self.addresses[slot]}}},
        }

    def components(self, slot):
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        return shared.component_names(role)

    def pod(self, slot, component):
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        namespace = f"radplanes-local-{slot}-{role}"
        image = "provisioner" if component == "provisioner" else "api"
        return {
            "metadata": {
                "name": component + "-pod",
                "uid": uid(slot + component + ("changed" if self.changed_uid else "")),
                "namespace": namespace,
                "labels": {"plane-demo/project": "radplanes", "plane-demo/component": component},
                "ownerReferences": [
                    {
                        "kind": "ReplicaSet",
                        "name": component + "-rs",
                        "uid": uid(slot + component + "rs"),
                    }
                ],
            },
            "spec": {
                "nodeName": self.node(slot)["Name"][1:],
                "serviceAccountName": component,
                "containers": [
                    {
                        "name": component,
                        "command": [
                            "python",
                            "-m",
                            "plane_demo." + component.replace("-", ".")
                            if component != "provisioner"
                            else "plane_demo.management.provisioner",
                        ],
                        "image": f"localhost/radplanes-plane-{image}:{REVISION}",
                        "envFrom": [{"secretRef": {"name": component + "-runtime"}}],
                    }
                ],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": component,
                        "ready": True,
                        "imageID": self.running_ids.get(image, self.image_ids[image]),
                        "containerID": "containerd://" + "c" * 64,
                    }
                ],
            },
        }

    def deployment(self, slot, component):
        pod = self.pod(slot, component)
        return {
            "metadata": {
                "name": component,
                "uid": uid(slot + component + "deployment"),
                "namespace": pod["metadata"]["namespace"],
            },
            "spec": {
                "replicas": 1,
                "template": {"metadata": {"labels": pod["metadata"]["labels"]}},
            },
        }

    def inventory(self):
        if not self.all_ready:
            return {"pairs": [], "tenants": []}
        return {
            "pairs": [
                {
                    "pair_id": pair,
                    "stage": "ready",
                    **{
                        role + "_cluster_id": f"kind://radplanes-local-{pair}-{role}"
                        for role in ("control", "data")
                    },
                    **{
                        role + "_url": f"http://127.0.0.1:{35490 + SLOTS.index(pair + '-' + role)}"
                        for role in ("control", "data")
                    },
                }
                for pair in ("shared", "isolated-1")
            ],
            "tenants": [
                {
                    "tenant_id": name,
                    "pair_id": "isolated-1" if index == 2 else "shared",
                    "isolation": "isolated" if index == 2 else "shared",
                    "ready": True,
                }
                for index, name in enumerate(shared.SHOWCASE.values())
            ],
        }

    def resources(self):
        return {
            "value": [
                {
                    "id": export.RESOURCE_PREFIX + "/" + export.RESOURCE_TYPE + "/" + slot,
                    "name": slot,
                    "type": export.RESOURCE_TYPE,
                    "properties": {
                        "slot": slot,
                        "provisioningState": "Succeeded",
                        "environment": export.RESOURCE_PREFIX
                        + "/Applications.Core/environments/provision-"
                        + slot,
                        "application": export.RESOURCE_PREFIX
                        + "/Applications.Core/applications/cluster-"
                        + slot,
                        "clusterId": "kind://radplanes-local-" + slot,
                        "clusterName": "radplanes-local-" + slot,
                        "bootstrapAccessRef": f"kubernetes://radplanes-local-access/radplanes-local-{slot}-access#kubeconfig",
                    },
                }
                for slot in SLOTS[1:]
            ]
            if self.all_ready
            else []
        }

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error_on and self.error_on(argv):
            raise faults.AcceptanceError("synthetic_operator_refusal")
        if argv[0] == "git":
            return ""
        if argv[0] == "docker":
            if argv[:3] != ["docker", "--host", network.DOCKER_HOST]:
                raise AssertionError("unscoped Docker transport")
            args = argv[3:]
            if args[0] == "ps":
                slot = args[-1].removeprefix("label=io.x-k8s.kind.cluster=radplanes-local-")
                return self.node_ids[slot] + "\n"
            if args[0] == "inspect":
                slot = next(
                    slot for slot, identity in self.node_ids.items() if identity == args[-1]
                )
                return json.dumps([self.node(slot)])
            if args[:2] == ["network", "inspect"]:
                return json.dumps(
                    [
                        {
                            "Containers": {
                                self.node_ids[slot]: {"IPv4Address": address + "/16"}
                                for slot, address in self.addresses.items()
                            }
                        }
                    ]
                )
            if args[:2] == ["image", "inspect"]:
                role = "provisioner" if "provisioner:" in args[-1] else "api"
                return json.dumps(
                    [{"Id": self.image_ids[role], "Os": "linux", "Architecture": "arm64"}]
                )
            if args[0] == "exec":
                slot = next(slot for slot, identity in self.node_ids.items() if identity == args[1])
                cmd = args[2:]
                if cmd[:2] == ["crictl", "inspecti"]:
                    return json.dumps(
                        {"status": {"id": "invalid" if self.image_mapping_wrong else cmd[-1]}}
                    )
                if cmd[:5] == ["ctr", "--namespace", "k8s.io", "content", "get"]:
                    return self.blobs[cmd[-1]]
                component = "control-reconciler" if slot.endswith("-control") else "data-reconciler"
                pod = self.pod(slot, component)
                sandbox_id = "d" * 64
                if cmd[:2] == ["crictl", "pods"]:
                    return json.dumps(
                        {"items": [{"id": sandbox_id, "metadata": {"uid": pod["metadata"]["uid"]}}]}
                    )
                if cmd[:2] == ["crictl", "inspectp"]:
                    return json.dumps(
                        {
                            "status": {
                                "id": sandbox_id,
                                "state": "SANDBOX_READY",
                                "metadata": {
                                    key: pod["metadata"][key]
                                    for key in ("uid", "name", "namespace")
                                },
                                "labels": {"io.kubernetes.pod.uid": pod["metadata"]["uid"]},
                            },
                            "info": {"pid": 245},
                        }
                    )
                if cmd[:2] == ["crictl", "inspect"]:
                    return json.dumps(
                        {
                            "status": {
                                "id": "c" * 64,
                                "metadata": {"name": component},
                                "labels": {"io.kubernetes.pod.uid": pod["metadata"]["uid"]},
                            },
                            "info": {"sandboxID": sandbox_id},
                        }
                    )
                if cmd[0] == "stat":
                    return "4026532000\n" if not self.sandbox_changed else "4026532001\n"
                if cmd[:2] == ["bash", "-ceu"]:
                    if cmd[2] != network.NETWORK_COMMAND:
                        raise AssertionError("unguarded network command")
                    native = cmd[6:]
                    if native == ["iptables", "-w", "2", "-S", "OUTPUT"]:
                        return (
                            "-P OUTPUT ACCEPT\n"
                            + ("synthetic-rule\n" if self.rules else "")
                            + self.extra_rules
                        )
                    if native[:2] == ["bash", "-ceu"] and native[2] == network.RULE_COMMAND:
                        action = native[4]
                        if action == "add":
                            self.rules = True
                            if self.add_fails_after_mutation:
                                raise faults.AcceptanceError("ambiguous_rule_create")
                        elif action == "delete":
                            if self.delete_fails:
                                raise faults.AcceptanceError("rule_delete_failed")
                            self.rules = False
                            self.clock.sleep(self.restore_cost)
                        elif action == "check":
                            faults.require(self.rules, "rule_not_present")
                        else:
                            raise AssertionError(action)
                        return ""
        if argv[0] == "kubectl":
            if "--kubeconfig" not in argv or "--context" not in argv:
                raise AssertionError("unscoped Kubernetes transport")
            slot = argv[argv.index("--context") + 1].removeprefix("radplanes-local-")
            args = argv[
                next(
                    index for index, arg in enumerate(argv) if arg.startswith("--request-timeout=")
                )
                + 1 :
            ]
            if args[:2] == ["-n", "radplanes-local-access"]:
                args = args[2:]
            if args[:2] == ["get", "--raw"]:
                return json.dumps(self.resources())
            if args[:2] == ["get", "namespace"]:
                name = args[2]
                return json.dumps({"metadata": {"name": name, "uid": uid(slot + name)}})
            if args[:2] == ["get", "deployments"]:
                return json.dumps(
                    {
                        "items": [
                            self.deployment(slot, component) for component in self.components(slot)
                        ]
                    }
                )
            if args[:2] == ["get", "deployment"]:
                return json.dumps(self.deployment(slot, args[2]))
            if args[:2] == ["get", "pods"]:
                component = args[args.index("-l") + 1].split("plane-demo/component=")[1]
                return json.dumps({"items": [self.pod(slot, component)]})
            if args[:2] == ["get", "pod"]:
                return json.dumps(self.pod(slot, args[2].removesuffix("-pod")))
            if args[:2] == ["get", "replicaset"]:
                component = args[2].removesuffix("-rs")
                return json.dumps(
                    {
                        "metadata": {
                            "uid": uid(slot + component + "rs"),
                            "ownerReferences": [
                                {"kind": "Deployment", "uid": uid(slot + component + "deployment")}
                            ],
                        }
                    }
                )
            if args[:2] == ["get", "secret"]:
                if args[2].endswith("-access"):
                    child = args[2].removeprefix("radplanes-local-").removesuffix("-access")
                    access = profile(child, f"https://{self.addresses[child]}:6443")
                    if self.changed_ca:
                        access["clusters"][0]["cluster"]["certificate-authority-data"] = encoded(
                            "-----BEGIN CERTIFICATE-----\nchanged"
                        )
                    return "\n".join(
                        [
                            uid(child + "-secret"),
                            json.dumps({"radplanes.local/slot": child}),
                            json.dumps(
                                {
                                    "radplanes.local/radius-resource": "foreign"
                                    if self.foreign_secret
                                    else export.RESOURCE_PREFIX
                                    + "/"
                                    + export.RESOURCE_TYPE
                                    + "/"
                                    + child
                                }
                            ),
                            encoded(json.dumps(access)),
                        ]
                    )
                key = ("changed" if self.changed_key else slot) + "-" + "k" * 40
                return uid(slot + "-api-key") + "\n" + encoded(key)
            if args[0] == "exec":
                code = args[args.index("-c", args.index("--")) + 1]
                if code == shared.INVENTORY_PROBE:
                    return json.dumps(self.inventory())
                if code == runner.SOURCE_PROBE:
                    return json.dumps(
                        {
                            "files": {} if self.running_source_wrong else SOURCES,
                            "parent_dsn_present": not args[1].startswith("data-api"),
                        }
                    )
                if code == shared.PARENT_PROBE:
                    parent_slot = (
                        "management"
                        if slot.endswith("-control")
                        else slot.removesuffix("-data") + "-control"
                    )
                    return json.dumps({"host": self.addresses[parent_slot], "port": 31543})
                if code == runner.IDENTITY_PROBE:
                    if slot.endswith("-control"):
                        return json.dumps(
                            {
                                "postgresql": {
                                    "host": getattr(self, "postgres_host", self.addresses[slot]),
                                    "port": getattr(self, "postgres_port", 31543),
                                    "tls": False,
                                }
                            }
                        )
                    return json.dumps(
                        {
                            "redis": {
                                "host": f"redis.radplanes-local-{slot}-data.svc.cluster.local",
                                "port": 6379,
                                "tls": False,
                                "authenticated": True,
                                "wrong_password_rejected": True,
                            }
                        }
                    )
        raise AssertionError("unrecognized native command: " + repr(argv))

    def execute(self, argv, **kwargs):
        output = self.run(argv, **kwargs)
        if kwargs.get("text") is False and isinstance(output, str):
            output = output.encode()
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


class LocalStateCase(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ".state" / ("harness-local-unit-" + uuid4().hex)
        self.state = self.root / ".state/local"
        self.state.mkdir(parents=True, mode=0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (export, shared, runner, faults):
            self.stack.enter_context(patch.object(module, "ROOT", self.root))
        self.stack.enter_context(patch.object(export, "STATE", self.state))
        self.stack.enter_context(patch.object(faults, "source_metadata", return_value=SOURCE))
        self.stack.enter_context(patch.object(runner, "local_source_hashes", return_value=SOURCES))
        self.operator = Operator()
        ca = hashlib.sha256(b"-----BEGIN CERTIFICATE-----\nmanagement").hexdigest()
        self.config = {
            "version": 1,
            "provider": "local",
            "projectName": "radplanes",
            "allocations": {
                slot: {
                    "slot": slot,
                    "clusterName": "radplanes-local-" + slot,
                    "context": "radplanes-local-" + slot,
                    "gatewayPort": 35490 + index,
                    "apiPort": 35495 + index,
                }
                for index, slot in enumerate(SLOTS)
            },
            "recipes": {
                role: {
                    "digest": "sha256:" + "e" * 64,
                    "moduleServer": "local-module-" + "e" * 20,
                    "reference": "http://local-module-"
                    + "e" * 20
                    + ".radius-system.svc.cluster.local:18080/"
                    + "e" * 64
                    + ".tar.gz",
                }
                for role in ("cluster", "postgresql", "redis", "gateway")
            },
            "images": {
                role: {
                    "reference": f"localhost/radplanes-plane-{role}:{REVISION}",
                    "imageId": self.operator.image_ids[role],
                }
                for role in ("api", "provisioner")
            },
            "managementCluster": {
                "clusterId": "kind://radplanes-local-management",
                "uid": uid("managementkube-system"),
                "nodeAddress": self.operator.addresses["management"],
                "serviceAddress": "10.96.0.1",
                "caSHA256": ca,
            },
        }
        self.review = {
            "version": 1,
            "source_revision": REVISION,
            "architecture": "arm64",
            "content_verified": True,
            "inspected_at": "2026-09-09T10:00:00+00:00",
            **{
                role: {
                    "reference": value["reference"],
                    "image_id": value["imageId"],
                    "source_hashes": SOURCES,
                }
                for role, value in self.config["images"].items()
            },
        }
        self.write("provisioning.json", self.config)
        self.write("runtime-images.json", self.review)
        self.write("home/.kube/config", profile("management", "https://127.0.0.1:35495"))
        self.write(
            "management-created.json",
            {
                "name": "radplanes-local-management",
                "context": "radplanes-local-management",
                "nodeId": self.operator.node_ids["management"],
                "nodeAddress": self.operator.addresses["management"],
                "secretEncryptionVerified": True,
            },
        )

    def write(self, name, value):
        path = self.state / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def exporter(self):
        subject = export.Exporter(
            self.state / "provisioning.json", execute=self.operator.execute, health=Mock()
        )
        self.addCleanup(lambda: shutil.rmtree(subject.work, ignore_errors=True))
        return subject

    def publish(self, ready=True):
        self.operator.all_ready = ready
        subject = self.exporter()
        result = subject.run(watch=False, emit=lambda _value: None)
        return result, subject

    def manifest_image(self, *, foreign_config=False):
        config = b'{"architecture":"arm64","os":"linux"}\r\n'
        digest = "sha256:" + hashlib.sha256(config).hexdigest()
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": "sha256:" + "c" * 64 if foreign_config else digest},
                "layers": [],
            }
        ).encode()
        running = "sha256:" + hashlib.sha256(manifest).hexdigest()
        self.operator.image_ids["api"] = digest
        self.operator.running_ids["api"] = running
        self.operator.blobs = {running: manifest, digest: config}
        self.config["images"]["api"]["imageId"] = digest
        self.review["api"]["image_id"] = digest
        self.write("provisioning.json", self.config)
        self.write("runtime-images.json", self.review)
        return running, digest


class ExportTests(LocalStateCase):
    def test_child_inventory_requires_the_actual_per_slot_provisioning_owners(self):
        self.operator.all_ready = True
        subject = self.exporter()
        records = self.operator.resources()
        for spelling in ("resourceGroups", "resourcegroups"):
            body = json.dumps(records).replace("resourceGroups", spelling)
            with patch.object(subject, "kube", return_value=body):
                self.assertEqual(set(subject.resource_inventory({})), set(SLOTS[1:]))
        for field, suffix in (
            ("environment", "/Applications.Core/environments/management"),
            ("application", "/Applications.Core/applications/management"),
            ("environment", "/Applications.Core/environments/provision-shared-data"),
            ("application", "/Applications.Core/applications/cluster-shared-data"),
        ):
            changed = copy.deepcopy(records)
            changed["value"][0]["properties"][field] = export.RESOURCE_PREFIX + suffix
            with (
                patch.object(subject, "kube", return_value=json.dumps(changed)),
                self.assertRaisesRegex(shared.ExportError, "resource_identity_mismatch"),
            ):
                subject.resource_inventory({})

    def test_aggregate_endpoints_without_export_generation_are_not_adopted(self):
        path = self.write("endpoints.json", {"pairs": {}})
        original = path.read_bytes()
        with self.assertRaisesRegex(export.Error, "local_endpoints_without_owned_generation"):
            self.exporter()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.operator.calls, [])

    def test_containerd_manifest_digest_is_mapped_using_actual_raw_config_bytes(self):
        running, digest = self.manifest_image()
        _, subject = self.publish()
        mapping = subject.targets["management"]["local"]["image_ids"]["management-api"]
        self.assertNotEqual(running, digest)
        self.assertEqual(mapping, {"running_image_id": running, "image_id": digest})
        content_calls = [(argv, options) for argv, options in self.operator.calls if "ctr" in argv]
        self.assertTrue(content_calls)
        self.assertTrue(all(options["text"] is False for _, options in content_calls))
        self.assertTrue(
            all(
                argv[-5:-1] == ["--namespace", "k8s.io", "content", "get"]
                for argv, _ in content_calls
            )
        )
        self.assertTrue(any(runner.SOURCE_PROBE in argv for argv, _ in self.operator.calls))
        self.assertFalse(any(argv[0] in {"az", "kubelogin"} for argv, _ in self.operator.calls))

    def test_native_index_selects_one_architecture_and_hashes_every_link(self):
        manifest, digest = self.manifest_image()
        index = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {"digest": manifest, "platform": {"os": "linux", "architecture": "arm64"}},
                    {
                        "digest": "sha256:" + "d" * 64,
                        "platform": {"os": "unknown", "architecture": "unknown"},
                    },
                ],
            }
        ).encode()
        running = "sha256:" + hashlib.sha256(index).hexdigest()
        self.operator.blobs[running] = index
        self.operator.running_ids["api"] = running
        _, subject = self.publish()
        mapping = subject.targets["management"]["local"]["image_ids"]["management-api"]
        self.assertEqual(mapping, {"running_image_id": running, "image_id": digest})
        reads = {argv[-1] for argv, _ in self.operator.calls if "ctr" in argv}
        self.assertEqual(reads, {running, manifest, digest})

    def test_manifest_and_config_byte_hashes_cannot_be_metadata_only(self):
        for kind in ("manifest", "configuration"):
            with self.subTest(kind=kind):
                running, digest = self.manifest_image()
                self.operator.blobs[running if kind == "manifest" else digest] += b"changed"
                with self.assertRaisesRegex(export.Error, "content_hash_mismatch"):
                    self.publish()

    def test_manifest_referencing_another_config_is_refused(self):
        self.manifest_image(foreign_config=True)
        with self.assertRaisesRegex(export.Error, "image_mapping_mismatch"):
            self.publish()

    def test_verified_manifest_mapping_never_skips_running_source_hashes(self):
        self.manifest_image()
        self.operator.running_source_wrong = True
        with self.assertRaisesRegex(export.Error, "running_source_hash_mismatch"):
            self.publish()

    def test_real_export_path_publishes_management_then_all_five_atomically(self):
        first_code, first = self.publish(ready=False)
        self.assertEqual(first_code, 3)
        self.assertEqual(set(first.targets), {"management"})
        initial = json.loads((self.state / "acceptance.json").read_text())
        original = (self.state / initial["endpoints_file"]).read_bytes()
        self.assertTrue(
            json.loads((self.state / "export-status.json").read_text())["ready_for_onboarding"]
        )
        second_code, second = self.publish()
        self.assertEqual(second_code, 0)
        self.assertEqual(set(second.targets), set(SLOTS))
        self.assertNotEqual(second.previous["endpoints_file"], initial["endpoints_file"])
        self.assertEqual((self.state / initial["endpoints_file"]).read_bytes(), original)
        for slot in SLOTS:
            for suffix in (".key", ".kubeconfig"):
                self.assertEqual(stat.S_IMODE((self.state / (slot + suffix)).stat().st_mode), 0o600)
            target = second.targets[slot]
            self.assertEqual(target["context"], "radplanes-local-" + slot)
            self.assertEqual(target["cluster_id"], "kind://radplanes-local-" + slot)
            config = faults.Configuration(self.state / "acceptance.json")
            self.assertEqual(config.target(slot).namespace, target["namespace"])
        calls = [argv for argv, _ in self.operator.calls]
        secret_reads = [argv for argv in calls if "secret" in argv]
        self.assertTrue(secret_reads)
        self.assertTrue(
            all(any(arg.startswith("jsonpath=") for arg in argv) for argv in secret_reads)
        )
        self.assertFalse(any("secrets" in argv for argv in calls))
        self.assertFalse(
            any(arg in {"create", "apply", "delete"} for argv in calls for arg in argv)
        )
        source_reads = [argv for argv in calls if runner.SOURCE_PROBE in argv]
        self.assertGreaterEqual(len(source_reads), 10)

    def test_authentication_failure_is_not_pending_and_retains_snapshot(self):
        self.publish(False)
        before = (self.state / "acceptance.json").read_bytes()
        self.operator.error_on = lambda argv: "--raw" in argv
        with self.assertRaisesRegex(faults.AcceptanceError, "operator_refusal"):
            self.publish()
        self.assertEqual((self.state / "acceptance.json").read_bytes(), before)
        status = json.loads((self.state / "export-status.json").read_text())
        self.assertEqual(status["outcome"], "failed")
        self.assertFalse(status["ready_for_onboarding"])

    def test_named_access_secret_requires_radius_ownership(self):
        self.operator.foreign_secret = True
        with self.assertRaisesRegex(export.Error, "access_secret_ownership"):
            self.publish()
        self.assertFalse((self.state / "shared-control.kubeconfig").exists())

    def test_changed_child_ca_is_rejected_before_using_cached_credentials(self):
        self.publish()
        before = (self.state / "shared-control.kubeconfig").read_bytes()
        self.operator.calls.clear()
        self.operator.changed_ca = True
        with self.assertRaisesRegex(export.Error, "immutable_file_changed"):
            self.publish()
        self.assertEqual((self.state / "shared-control.kubeconfig").read_bytes(), before)
        child_reads = [
            argv for argv, _ in self.operator.calls if "radplanes-local-shared-control" in argv
        ]
        self.assertEqual(child_reads, [])

    def test_changed_key_and_image_mapping_and_source_fail(self):
        self.publish()
        before = (self.state / "management.key").read_bytes()
        for attribute, code in (
            ("changed_key", "immutable_file_changed"),
            ("image_mapping_wrong", "image_mapping_mismatch"),
            ("running_source_wrong", "source_hash_mismatch"),
        ):
            with self.subTest(attribute=attribute):
                setattr(self.operator, attribute, True)
                with self.assertRaisesRegex(export.Error, code):
                    self.publish()
                setattr(self.operator, attribute, False)
                self.assertEqual((self.state / "management.key").read_bytes(), before)

    def test_bootstrap_docker_id_and_encryption_are_not_metadata_only(self):
        record = json.loads((self.state / "management-created.json").read_text())
        for field, value in (("nodeId", "f" * 64), ("secretEncryptionVerified", False)):
            with self.subTest(field=field):
                self.write("management-created.json", {**record, field: value})
                with self.assertRaisesRegex(export.Error, "management_bootstrap"):
                    self.publish()

    def test_external_and_nonprivate_paths_are_refused_before_commands(self):
        (self.state / "provisioning.json").chmod(0o644)
        with self.assertRaisesRegex(export.Error, "not_private"):
            self.exporter()
        self.assertEqual(self.operator.calls, [])

    def test_previously_published_child_disappearance_is_not_pending(self):
        self.publish()
        with patch.object(self.operator, "resources", return_value={"value": []}):
            with self.assertRaisesRegex(export.Error, "published_radius_cluster_missing"):
                self.publish()

    def test_export_uses_isolated_home_and_no_inherited_cloud_or_proxy_credentials(self):
        with patch.dict(
            "os.environ", {"HTTPS_PROXY": "http://foreign", "AZURE_CLIENT_SECRET": "synthetic"}
        ):
            self.publish()
        for _argv, kwargs in self.operator.calls:
            environment = kwargs.get("env")
            if environment is not None:
                self.assertEqual(environment["HOME"], str(self.state / "home"))
                self.assertNotIn("HTTPS_PROXY", environment)
                self.assertNotIn("AZURE_CLIENT_SECRET", environment)

    def test_transport_refuses_tls_bypass_exec_and_wrong_san(self):
        valid = profile("shared-control", "https://172.18.0.3:6443")
        cases = []
        for field, value in (
            ("insecure-skip-tls-verify", True),
            ("tls-server-name", "foreign"),
            ("proxy-url", "http://foreign"),
        ):
            changed = copy.deepcopy(valid)
            changed["clusters"][0]["cluster"][field] = value
            cases.append(changed)
        changed = copy.deepcopy(valid)
        changed["users"][0]["user"]["exec"] = {"command": "unsafe"}
        cases.append(changed)
        for changed in cases:
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(export.Error, "transport_mismatch"),
            ):
                export.kube_profile(
                    changed, "radplanes-local-shared-control", "https://172.18.0.3:6443", child=True
                )


class Probe:
    def __init__(self, operator):
        self.operator = operator
        self.closed = False
        self.actions = []

    def request(self, action):
        self.actions.append(action)
        blocked = self.operator.rules and self.operator.parent_blocked
        ok = self.operator.local_ok if action == "local" else not blocked
        if action == "existing" and self.operator.existing_survives:
            ok = True
        return {"ok": ok, "network_failure": not ok, "ips": [self.operator.addresses["management"]]}

    def close(self):
        self.closed = True


class LocalFaultTests(LocalStateCase):
    def make_fault(self):
        self.publish()
        self.configuration = faults.Configuration(self.state / "acceptance.json")
        self.probes = []

        def probe_factory(*_args):
            probe = Probe(self.operator)
            self.probes.append(probe)
            return probe

        subject = network.LocalParentFault(
            self.configuration,
            "shared-control",
            "control-reconciler",
            self.state / "evidence/fault.json",
            operator=self.operator.run,
            kube_factory=lambda target: faults.Kubectl(target, runner=self.operator.run),
            probe_factory=probe_factory,
            clock=self.operator.clock,
            sleep=self.operator.clock.sleep,
        )
        self.operator.calls.clear()
        return subject

    def test_real_fault_path_guards_pod_cri_netns_and_restores_exact_rule(self):
        subject = self.make_fault()
        with subject:
            self.assertTrue(self.operator.rules)
            journal = json.loads(subject.evidence_path.read_text())
            self.assertTrue(journal["creation_attempted"])
            self.assertEqual(journal["strategy"], "pod-network-namespace-iptables")
            subject.assert_blocked()
        self.assertFalse(self.operator.rules)
        self.assertTrue(subject.record["restored"])
        self.assertEqual(
            subject.record["original_rules_sha256"], subject.record["restored_rules_sha256"]
        )
        self.assertNotIn("policy", subject.record)
        calls = [argv for argv, _ in self.operator.calls]
        self.assertTrue(any("inspectp" in argv for argv in calls))
        self.assertTrue(any("inspect" in argv and "crictl" in argv for argv in calls))
        mutations = [argv for argv in calls if network.RULE_COMMAND in argv]
        self.assertEqual(
            [argv[argv.index(network.RULE_COMMAND) + 2] for argv in mutations],
            ["add", "check", "delete"],
        )
        for argv in mutations:
            self.assertIn(network.NETWORK_COMMAND, argv)
            self.assertEqual(argv[-len(subject.rule) :], subject.rule)
            self.assertNotIn("--privileged", argv)
        actions = [action for probe in self.probes for action in probe.actions]
        self.assertIn("existing", actions)
        self.assertIn("fresh", actions)
        self.assertIn("local", actions)
        self.assertTrue(all(probe.closed for probe in self.probes))

    def test_ambiguous_mutation_failure_still_restores(self):
        subject = self.make_fault()
        self.operator.add_fails_after_mutation = True
        with self.assertRaisesRegex(faults.AcceptanceError, "ambiguous_rule"):
            with subject:
                self.fail("activation must fail")
        self.assertFalse(self.operator.rules)
        self.assertTrue(subject.record["restored"])

    def test_existing_connection_must_really_be_blocked(self):
        subject = self.make_fault()
        self.operator.existing_survives = True
        with self.assertRaisesRegex(faults.AcceptanceError, "existing_connection_not_blocked"):
            with subject:
                self.fail("a policy-only proof must not pass")
        self.assertFalse(self.operator.rules)
        self.assertTrue(subject.record["restored"])

    def test_foreign_kind_label_refuses_before_insertion(self):
        subject = self.make_fault()
        original = self.operator.node

        def foreign(slot):
            node = original(slot)
            node["Config"]["Labels"]["io.x-k8s.kind.cluster"] = "unrelated-project"
            return node

        with patch.object(self.operator, "node", side_effect=foreign):
            with self.assertRaisesRegex(faults.AcceptanceError, "node_ownership_mismatch"):
                with subject:
                    self.fail("foreign node must not be entered")
        self.assertFalse(self.operator.rules)
        self.assertFalse(any(network.RULE_COMMAND in argv for argv, _ in self.operator.calls))

    def test_wrong_service_account_refuses_before_insertion(self):
        subject = self.make_fault()
        original = self.operator.pod

        def foreign(slot, component):
            pod = original(slot, component)
            pod["spec"]["serviceAccountName"] = "default"
            return pod

        with patch.object(self.operator, "pod", side_effect=foreign):
            with self.assertRaisesRegex(faults.AcceptanceError, "pod_identity_mismatch"):
                with subject:
                    self.fail("a default service account cannot be targeted")
        self.assertFalse(self.operator.rules)

    def test_body_exception_and_cleanup_failure_are_explicit(self):
        subject = self.make_fault()
        with self.assertRaisesRegex(ValueError, "synthetic"):
            with subject:
                raise ValueError("synthetic")
        self.assertFalse(self.operator.rules)
        subject = self.make_fault()
        with self.assertRaisesRegex(faults.AcceptanceError, "fault_restoration_failed"):
            with subject:
                self.operator.delete_fails = True
        self.assertTrue(self.operator.rules)
        self.assertFalse(subject.record["restored"])

    def test_changed_pod_or_namespace_refuses_mutating_replacement(self):
        subject = self.make_fault()
        with self.assertRaisesRegex(faults.AcceptanceError, "fault_restoration_failed"):
            with subject:
                self.operator.changed_uid = True
        self.assertTrue(self.operator.rules)
        self.assertIn("reconciler_replaced", subject.record["restoration_error"])
        self.operator.changed_uid = False
        subject.restore()
        self.assertFalse(self.operator.rules)

    def test_network_namespace_inode_change_is_not_adopted(self):
        subject = self.make_fault()
        with self.assertRaisesRegex(faults.AcceptanceError, "fault_restoration_failed"):
            with subject:
                self.operator.sandbox_changed = True
        self.assertTrue(self.operator.rules)
        self.assertEqual(subject.record["restoration_error"], "local_network_namespace_changed")

    def test_restore_and_cleanup_share_the_existing_absolute_30_second_budget(self):
        subject = self.make_fault()
        self.operator.restore_cost = 31
        with self.assertRaisesRegex(faults.AcceptanceError, "fault_restoration_failed"):
            with subject:
                pass
        self.assertFalse(self.operator.rules)
        self.assertEqual(subject.recovery_deadline - subject.recovery_started, 30)
        self.assertFalse(subject.record["restored"])

    def test_restore_only_validates_saved_rule_then_runs_native_cleanup(self):
        subject = self.make_fault()
        subject.activate()
        subject.probe.close()
        restored = network.LocalParentFault.from_evidence(
            self.configuration,
            subject.evidence_path,
            operator=self.operator.run,
            kube_factory=lambda target: faults.Kubectl(target, runner=self.operator.run),
            probe_factory=lambda *_args: Probe(self.operator),
            clock=self.operator.clock,
            sleep=self.operator.clock.sleep,
        )
        restored.restore()
        self.assertFalse(self.operator.rules)
        self.assertTrue(restored.record["restored"])
        value = json.loads(subject.evidence_path.read_text())
        value["rule"][1] = "172.18.0.0/16"
        self.write("evidence/fault.json", value)
        with self.assertRaisesRegex(faults.AcceptanceError, "restore_rule_mismatch"):
            network.LocalParentFault.from_evidence(self.configuration, subject.evidence_path)

    def test_factory_selects_local_without_weakening_azure(self):
        subject = self.make_fault()
        self.assertIs(faults.fault_class(self.configuration), network.LocalParentFault)
        self.assertIs(faults.fault_class(SimpleNamespace(environment="azure")), faults.ParentFault)
        self.assertEqual(subject.target.parent["port"], 31543)


class CleanupFaultTests(LocalStateCase):
    make_fault = LocalFaultTests.make_fault

    def check(self):
        return network.assert_restored_for_cleanup(
            self.operator.run, faults.Configuration(self.state / "acceptance.json")
        )

    def test_cleanup_proof_is_read_only_and_reuses_live_ownership_guards(self):
        subject = self.make_fault()
        with subject:
            pass
        before = {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        self.operator.calls.clear()
        with patch.object(
            network.base, "protected_write", side_effect=AssertionError("write refused")
        ):
            proof = self.check()
        self.assertEqual(set(proof["targets"]), set(SLOTS[1:]))
        self.assertEqual(proof["journals"][0]["run_id"], subject.run_id)
        self.assertEqual(proof["targets"]["shared-control"]["sandbox"], subject.sandbox)
        self.assertEqual(
            before, {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        )
        self.assertTrue(any("inspectp" in argv for argv, _ in self.operator.calls))
        self.assertFalse(any(network.RULE_COMMAND in argv for argv, _ in self.operator.calls))
        self.assertNotIn("PRIVATE KEY", json.dumps(proof))

    def test_unrestored_attempt_is_refused_before_live_commands(self):
        subject = self.make_fault()
        subject.activate()
        self.operator.calls.clear()
        try:
            with self.assertRaisesRegex(faults.AcceptanceError, "cleanup_fault_not_restored"):
                self.check()
            self.assertEqual(self.operator.calls, [])
            self.assertTrue(self.operator.rules)
        finally:
            subject.restore()

    def test_malformed_recognized_journal_cannot_be_skipped(self):
        subject = self.make_fault()
        with subject:
            pass
        record = json.loads(subject.evidence_path.read_text())
        record.pop("strategy")
        self.write("evidence/fault-" + "a" * 32 + ".json", record)
        self.operator.calls.clear()
        with self.assertRaisesRegex(faults.AcceptanceError, "journal_identity_mismatch"):
            self.check()
        self.assertEqual(self.operator.calls, [])

    def test_successful_restore_may_retain_historical_failure_fields(self):
        subject = self.make_fault()
        with subject:
            pass
        record = json.loads(subject.evidence_path.read_text())
        record.update(outcome="restoration_failed", restoration_error="earlier_failure")
        self.write("evidence/fault.json", record)
        proof = self.check()
        self.assertEqual(len(proof["journals"]), 1)
        self.assertEqual(
            json.loads(subject.evidence_path.read_text())["restoration_error"], "earlier_failure"
        )

    def test_recorded_pod_and_sandbox_cannot_be_replaced(self):
        subject = self.make_fault()
        with subject:
            pass
        for attribute in ("changed_uid", "sandbox_changed"):
            with self.subTest(attribute=attribute):
                setattr(self.operator, attribute, True)
                with self.assertRaisesRegex(
                    faults.AcceptanceError, "restored_identity_or_rules_changed"
                ):
                    self.check()
                setattr(self.operator, attribute, False)

    def test_unjournaled_live_fault_rule_is_refused_without_creating_evidence(self):
        self.publish()
        self.operator.extra_rules = (
            "-A OUTPUT -m comment --comment plane-demo-fault-" + "a" * 12 + " -j DROP\n"
        )
        with self.assertRaisesRegex(faults.AcceptanceError, "live_parent_fault_present"):
            self.check()
        self.assertFalse((self.state / "evidence").exists())
        self.assertFalse(any(network.RULE_COMMAND in argv for argv, _ in self.operator.calls))

    def test_changed_unrelated_output_rule_is_not_removed_or_adopted(self):
        subject = self.make_fault()
        with subject:
            pass
        self.operator.extra_rules = "-A OUTPUT -d 172.18.0.19/32 -j ACCEPT\n"
        self.operator.calls.clear()
        with self.assertRaisesRegex(faults.AcceptanceError, "restored_identity_or_rules_changed"):
            self.check()
        self.assertFalse(any(network.RULE_COMMAND in argv for argv, _ in self.operator.calls))
        self.assertIn("ACCEPT", self.operator.extra_rules)

    def test_partial_topology_is_refused_before_commands(self):
        self.publish(False)
        self.operator.calls.clear()
        with self.assertRaisesRegex(faults.AcceptanceError, "requires_complete_local_export"):
            self.check()
        self.assertEqual(self.operator.calls, [])

    def test_concurrent_journal_change_invalidates_proof(self):
        subject = self.make_fault()
        with subject:
            pass
        original = self.operator.run
        modified = False

        def concurrent(argv, **kwargs):
            nonlocal modified
            if not modified and network.NETWORK_COMMAND in argv:
                modified = True
                record = json.loads(subject.evidence_path.read_text())
                record["concurrent_write"] = True
                self.write("evidence/fault.json", record)
            return original(argv, **kwargs)

        with self.assertRaisesRegex(faults.AcceptanceError, "journals_changed"):
            network.assert_restored_for_cleanup(
                concurrent, faults.Configuration(self.state / "acceptance.json")
            )


class LocalRunnerTests(LocalStateCase):
    def make_runner(self, mode="all"):
        self.publish()
        configuration = faults.Configuration(self.state / "acceptance.json")
        subject = runner.Runner(
            configuration,
            mode,
            apis=Mock(),
            kube_factory=lambda target: faults.Kubectl(target, runner=self.operator.run),
        )
        subject.record["source"] = dict(SOURCE)
        return subject

    def test_local_workload_proof_executes_running_source_and_refuses_mapping_changes(self):
        subject = self.make_runner()
        subject.management_image()
        evidence = subject.record["events"]
        self.assertEqual(
            [item["component"] for item in evidence], ["management-api", "provisioner"]
        )
        self.assertEqual(evidence[0]["source_hashes"], SOURCES)
        value = json.loads((self.state / "acceptance.json").read_text())
        value["targets"]["management"]["local"]["image_ids"]["management-api"]["image_id"] = (
            "sha256:" + "f" * 64
        )
        self.write("acceptance.json", value)
        with self.assertRaisesRegex(faults.AcceptanceError, "running_image_identity"):
            subject.management_image()

    def test_local_datastore_identity_matches_real_nodeport_and_redis_contracts(self):
        subject = self.make_runner()
        observed = subject.workload_evidence("shared")
        self.assertEqual(
            observed["control"]["postgresql"],
            {"host": self.operator.addresses["shared-control"], "port": 31543, "tls": False},
        )
        self.assertTrue(observed["data"]["redis"]["wrong_password_rejected"])
        for host, port in (
            (self.operator.addresses["shared-control"], 5432),
            (self.operator.addresses["management"], 31543),
            (self.operator.addresses["isolated-1-control"], 31543),
            ("postgresql.radplanes-local-shared-control-control.svc.cluster.local", 31543),
            ("8.8.8.8", 31543),
        ):
            with self.subTest(host=host, port=port):
                self.operator.postgres_host, self.operator.postgres_port = host, port
                with self.assertRaisesRegex(faults.AcceptanceError, "datastore_endpoint_mismatch"):
                    subject.workload_evidence("shared")

    def test_mapped_manifest_id_acceptance_still_executes_running_source_probe(self):
        running, digest = self.manifest_image()
        subject = self.make_runner()
        subject.management_image()
        event = subject.record["events"][0]
        self.assertEqual(event["running_image_id"], running)
        self.assertNotEqual(event["running_image_id"], digest)
        self.assertEqual(event["source_hashes"], SOURCES)

    def test_local_worker_dockerfile_dirty_check_runs_before_workload_reads(self):
        subject = self.make_runner()
        with (
            patch.object(
                faults, "command", return_value=" M images/local-provisioner/Dockerfile\n"
            ) as command,
            patch.object(subject, "management_image") as workload,
        ):
            with self.assertRaisesRegex(faults.AcceptanceError, "source_worktree_dirty"):
                subject.run()
        self.assertIn("images/local-provisioner", command.call_args.args[0])
        workload.assert_not_called()

    def test_local_all_uses_full_scenario_and_both_original_outage_methods(self):
        subject = self.make_runner()
        methods = [
            "management_image",
            "scenario",
            "management_outage",
            "control_outage",
            "collect_timelines",
        ]
        calls = []
        for name in methods:
            setattr(subject, name, Mock(side_effect=lambda name=name: calls.append(name)))
        with patch.object(faults, "command", return_value=""):
            subject.run()
        self.assertEqual(calls, methods)
        self.assertEqual(subject.record["outcome"], "passed")
        self.assertIs(subject.fault_factory, network.LocalParentFault)
        self.assertIs(runner.Runner.scenario, type(subject).scenario)
        self.assertIs(runner.Runner.control_outage, type(subject).control_outage)

    def test_image_inspection_must_follow_commit_and_match_source(self):
        subject = self.make_runner()
        for change in (
            {"inspected_at": "2026-09-08T10:00:00+00:00"},
            {"source_revision": "8" * 40},
            {"content_verified": False},
        ):
            value = json.loads((self.state / "acceptance.json").read_text())
            original = copy.deepcopy(value)
            value["local_images"].update(change)
            self.write("acceptance.json", value)
            with self.assertRaisesRegex(faults.AcceptanceError, "image_review_mismatch"):
                subject.management_image()
            self.write("acceptance.json", original)

    def test_execute_is_required_before_acceptance_or_fault_entrypoints(self):
        with patch.object(runner, "Runner") as execute, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                runner.main(["--config", str(self.state / "acceptance.json")])
            execute.assert_not_called()
        with patch.object(faults, "fault_class") as select, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                faults.main(["--config", str(self.state / "acceptance.json")])
            select.assert_not_called()


class NativeScriptTests(unittest.TestCase):
    def test_local_operator_command_clears_proxy_and_global_home(self):
        result = SimpleNamespace(returncode=0, stdout="safe")
        with (
            patch.object(network.subprocess, "run", return_value=result) as execute,
            patch.dict("os.environ", {"HTTPS_PROXY": "http://foreign"}),
        ):
            self.assertEqual(network.operator_command(network.docker("version")), "safe")
        options = execute.call_args.kwargs
        self.assertEqual(options["env"]["HOME"], str(faults.ROOT / ".state/local/home"))
        self.assertNotIn("HTTPS_PROXY", options["env"])
        self.assertEqual(options["cwd"], faults.ROOT)

    def test_shell_syntax_and_network_fd_guards_without_docker(self):
        for code in (network.NETWORK_COMMAND, network.RULE_COMMAND):
            result = subprocess.run(
                ["bash", "-n"], input=code, text=True, capture_output=True, timeout=5, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('exec 3<"/proc/$pid/ns/net"', network.NETWORK_COMMAND)
        self.assertIn('test "$(stat -Lc %i /proc/1/ns/net)" != "$inode"', network.NETWORK_COMMAND)
        self.assertNotIn("iptables -F", network.RULE_COMMAND)
        self.assertNotIn("iptables-restore", network.RULE_COMMAND)

    def test_fault_main_local_restore_uses_routed_class(self):
        configuration = SimpleNamespace(environment="local")
        local = Mock()
        with (
            patch.object(faults, "Configuration", return_value=configuration),
            patch.object(faults, "fault_class", return_value=local),
            patch.object(faults, "interruption_is_failure", nullcontext),
            redirect_stdout(io.StringIO()),
        ):
            result = faults.main(
                [
                    "--config",
                    ".state/local/acceptance.json",
                    "--restore",
                    ".state/local/evidence/fault.json",
                    "--execute",
                ]
            )
        self.assertEqual(result, 0)
        local.from_evidence.return_value.restore.assert_called_once()


class LocalPostgresProbeTests(unittest.TestCase):
    def test_actual_probe_uses_the_supported_pgconn_ssl_property(self):
        import psycopg

        self.assertTrue(hasattr(psycopg.pq.PGconn, "ssl_in_use"))
        connection = SimpleNamespace(
            info=SimpleNamespace(host="172.18.0.3", port=31543),
            pgconn=SimpleNamespace(ssl_in_use=False),
            execute=Mock(
                return_value=SimpleNamespace(
                    fetchone=lambda: {"database": "control", "server_address": "10.244.0.5/32"}
                )
            ),
        )
        output = io.StringIO()
        with (
            patch("psycopg.connect", return_value=nullcontext(connection)),
            patch.dict(
                "os.environ",
                {
                    "CONTROL_DSN": (
                        "host=172.18.0.3 port=31543 dbname=control user=cp_api "
                        "password=synthetic-local-password sslmode=disable"
                    )
                },
            ),
            patch.object(sys, "argv", ["probe", "control-api", "local"]),
            redirect_stdout(output),
        ):
            exec(compile(runner.IDENTITY_PROBE, "<identity-probe>", "exec"), {})
            self.assertFalse(json.loads(output.getvalue())["postgresql"]["tls"])
            connection.pgconn.ssl_in_use = True
            with self.assertRaisesRegex(RuntimeError, "local_postgresql_transport_contract"):
                exec(compile(runner.IDENTITY_PROBE, "<identity-probe>", "exec"), {})


class LocalRedisProbeTests(unittest.TestCase):
    def test_run_path_performs_authenticated_ping_and_negative_password_check(self):
        connection = Connection(
            host="redis.radplanes-local-shared-data-data.svc.cluster.local",
            port=6379,
            password="synthetic-test-password",
        )
        connected = Mock(spec=socket.socket)
        connected.getpeername.return_value = ("10.96.0.8", 6379)
        connection._sock = connected
        self.addCleanup(setattr, connection, "_sock", None)
        store = Mock()
        store.ping.return_value = True
        store.connection_pool.get_connection.return_value = connection
        wrong = Mock()
        wrong.ping.side_effect = redis.AuthenticationError("synthetic")
        context = Mock()
        context.__enter__ = Mock(return_value=wrong)
        context.__exit__ = Mock(return_value=False)
        output = io.StringIO()
        with (
            patch.object(settings.Settings, "from_env"),
            patch.object(settings, "redis_client", return_value=store),
            patch.object(redis, "Redis", return_value=context) as rejected,
            patch.object(sys, "argv", ["identity-probe", "data-api", "local"]),
            redirect_stdout(output),
        ):
            exec(compile(runner.IDENTITY_PROBE, "<local-identity-probe>", "exec"), {})
        value = json.loads(output.getvalue())["redis"]
        self.assertIs(value["tls"], False)
        self.assertIs(value["authenticated"], True)
        self.assertIs(value["wrong_password_rejected"], True)
        store.ping.assert_called_once_with()
        wrong.ping.assert_called_once_with()
        self.assertNotEqual(rejected.call_args.kwargs["password"], connection.password)
        self.assertNotIn(connection.password, output.getvalue())
        store.close.assert_called_once_with()
