import base64
import gzip
import hashlib
import json
from uuid import UUID

import pytest
import yaml
from local_support import LOCAL, common, load

cleanup = load("local_full_cleanup", LOCAL / "cleanup.py")
LOAD_INPUTS = cleanup.load_inputs
CHECK_FAULTS = cleanup.Cleanup.check_faults
NATIVE_DELETE = cleanup.Cleanup.native_delete
REVISION = "a" * 40
IMAGES = {
    role: {
        "reference": f"localhost/radplanes-plane-{role}:{REVISION}", "imageId": "sha256:" + "b"*64,
    }
    for role in ("api", "provisioner")
}
CLUSTER_STATE_NAMES = {
    "shared-control": "tfstate-default-991db2202b21b52c0a09b1b342573648d3506a6b",
    "shared-data": "tfstate-default-b5cc6138c3b3fd3b08d079b268786494f89f95fd",
    "isolated-1-control": "tfstate-default-18d033b9f3488b1bfc01a93455d23a7488e8d455",
    "isolated-1-data": "tfstate-default-a20b34bc893f2883e258a306d116d784409fd94a",
}
APP_STATE_CASES = (
    ("management", "Demo.Platform/postgreSqlDatabases", "postgres",
     "tfstate-default-ff4b436814cd6fd55181cba746d639c5da9568c9"),
    ("shared-control", "Demo.Platform/postgreSqlDatabases", "postgres",
     "tfstate-default-09b37ea8a6822e91ee597fb03596daf173a65cdd"),
    ("shared-data", "Applications.Datastores/redisCaches", "redis",
     "tfstate-default-64c4c7d176a81d814a1a42958327bb9316e6ca49"),
    ("shared-data", "Demo.Platform/gateways", "gateway",
     "tfstate-default-0069531a116edf70a6dc14ae47c7df94e7b367dc"),
)


def encoded(value):
    return base64.b64encode(value.encode()).decode()


def access(slot, server):
    name = "radplanes-local-" + slot
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": name,
        "contexts": [{"name": name, "context": {"cluster": name, "user": name}}],
        "clusters": [{
            "name": name, "cluster": {
                "server": server, "certificate-authority-data": encoded("synthetic-ca"),
                **({"tls-server-name": name} if slot != "management" else {}),
            },
        }],
        "users": [{
            "name": name, "user": {
                "client-certificate-data": encoded("synthetic-cert"),
                "client-key-data": encoded("synthetic-key-not-real"),
            },
        }],
    }


class Offline:
    def __init__(self, state, targets):
        self.env = {}
        self.calls = []
        self.targets = targets
        self.nodes = {}
        self.apps = {}
        self.resources = {}
        self.objects = {}
        self.app_sticks = False
        self.delete_fails = False
        self.faults = []
        self.mutations = []
        for index, slot in enumerate(cleanup.SLOTS):
            target = targets[slot]
            name = "radplanes-local-" + slot
            self.nodes[target["nodeId"]] = {
                "Id": target["nodeId"], "Name": f"/{name}-control-plane",
                "Config": {"Labels": {"io.x-k8s.kind.cluster": name}},
                "NetworkSettings": {"Networks": {"kind": {"IPAddress": f"172.18.0.{index+2}"}}},
            }
            role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
            app_id = f"{cleanup.SCOPE}/Applications.Core/applications/{role}"
            env_id = f"{cleanup.SCOPE}/Applications.Core/environments/{slot}"
            self.apps[slot] = [{
                "name": role, "id": app_id, "properties": {"environment": env_id},
            }]
            self.resources[slot] = [{
                "type": "Applications.Core/containers", "name": "workload",
                "id": f"{cleanup.SCOPE}/Applications.Core/containers/workload",
                "properties": {
                    "environment": env_id, "application": app_id, "provisioningState": "Succeeded",
                },
            }]
            for namespace, uid in (
                ("kube-system", target["clusterUid"]),
                (target["namespace"], target["namespaceUid"]),
            ):
                self.objects[slot, "", "namespace", namespace] = {
                    "metadata": {"name": namespace, "uid": uid},
                }
            if slot != "management":
                self.child_state(slot, index)
        self.nodes["f" * 64] = {
            "Id": "f" * 64, "Name": "/other-project",
            "Config": {"Labels": None},
        }
        for slot in cleanup.CHILDREN:
            self.apps["management"].append({
                "name": f"cluster-{slot}",
                "id": f"{cleanup.SCOPE}/Applications.Core/applications/cluster-{slot}",
                "properties": {
                    "environment": (
                        f"{cleanup.SCOPE}/Applications.Core/environments/provision-{slot}"
                    ),
                },
            })
            self.resources["management"].append({
                "type": "Demo.Platform/clusters", "name": slot,
                "id": cleanup.cluster_resource(slot),
                "properties": {
                    "slot": slot,
                    "environment": (
                        f"{cleanup.SCOPE}/Applications.Core/environments/provision-{slot}"
                    ),
                    "application": f"{cleanup.SCOPE}/Applications.Core/applications/cluster-{slot}",
                    "provisioningState": "Succeeded",
                    "clusterId": f"kind://radplanes-local-{slot}",
                    "clusterName": f"radplanes-local-{slot}",
                    "bootstrapAccessRef": (
                        f"kubernetes://{cleanup.NAMESPACE}/radplanes-local-{slot}-access#kubeconfig"
                    ),
                },
            })
        self.deployments = []
        for name in ("management-api", "provisioner"):
            deployment = {
                "metadata": {
                    "name": name, "namespace": targets["management"]["namespace"],
                    "uid": name+"-uid",
                    "labels": {
                        "radapp.io/application": "management", "radapp.io/resource": name,
                        "plane-demo/project": "radplanes", "plane-demo/component": name,
                    },
                },
                "spec": {
                    "replicas": 1,
                    "template": {"spec": {"containers": [{
                        "name": name,
                        "image": IMAGES["api" if name == "management-api" else name]["reference"],
                    }]}},
                },
            }
            self.deployments.append(deployment)
            self.objects[
                "management", targets["management"]["namespace"], "deployment", name,
            ] = deployment

    def child_state(self, slot, index):
        target = self.targets[slot]
        name = "radplanes-local-" + slot
        kubeconfig = yaml.safe_dump(access(slot, f"https://172.18.0.{index+2}:6443"))
        state = {
            "lineage": target["terraformState"]["lineage"],
            "serial": target["terraformState"]["serial"], "terraform_version": "1.15.8",
            "resources": [
                {
                    "module": "module.default", "name": "child_address", "mode": "data",
                    "type": "external", "instances": [{"attributes": {
                        "result": {"address": f"172.18.0.{index+2}"},
                    }}],
                },
                {
                    "module": "module.default", "name": "child",
                    "mode": "managed", "type": "kind_cluster", "instances": [{"attributes": {
                        "name": name, "id": f"{name}-{cleanup.NODE_IMAGE}",
                        "node_image": cleanup.NODE_IMAGE, "completed": True,
                        "kubeconfig": kubeconfig, "client_key": "synthetic-key-not-real",
                    }}],
                },
                {
                    "module": "module.default", "name": "access",
                    "mode": "managed", "type": "kubernetes_secret_v1",
                    "instances": [{"attributes": {"metadata": [{
                        "name": target["accessSecret"]["name"], "namespace": cleanup.NAMESPACE,
                    }]}}],
                },
                {
                    "module": "module.default", "mode": "managed", "type": "terraform_data",
                    "name": "images", "provider": 'provider["terraform.io/builtin/terraform"]',
                    "instances": [{
                        "index_key": 0, "schema_version": 0,
                        "attributes": {
                            "id": str(UUID(int=100+index)), "input": None, "output": None,
                            "triggers_replace": {
                                "value": {
                                    "cluster_id": f"{name}-{cleanup.NODE_IMAGE}",
                                    "images": [IMAGES[role]["reference"]
                                               for role in ("api", "provisioner")],
                                },
                                "type": [
                                    "object",
                                    {"cluster_id": "string", "images": ["list", "string"]},
                                ],
                            },
                        },
                        "sensitive_attributes": [], "identity_schema_version": 0,
                    }],
                },
            ],
        }
        secret = {
            "metadata": {
                "name": target["terraformState"]["secret"], "uid": target["terraformState"]["uid"],
                "namespace": "radius-system",
                "labels": {"tfstate": "true", "app.kubernetes.io/managed-by": "terraform"},
            },
            "data": {
                "tfstate": base64.b64encode(gzip.compress(json.dumps(state).encode())).decode(),
            },
        }
        self.objects[
            "management", "radius-system", "secret", target["terraformState"]["secret"],
        ] = secret
        self.objects["management", cleanup.NAMESPACE, "secret", target["accessSecret"]["name"]] = {
            "metadata": {
                **target["accessSecret"],
                "labels": {"radplanes.local/slot": slot},
                "annotations": {"radplanes.local/radius-resource": cleanup.cluster_resource(slot)},
            },
            "data": {"kubeconfig": encoded(kubeconfig)},
        }

    def json(self, args, **kwargs):
        return json.loads(self.run(args, **kwargs))

    def native_delete(self, instance, slot):
        self.mutations.append(("radius-cluster", slot))
        if self.delete_fails:
            raise cleanup.LocalError("Synthetic Radius failure")
        self.resources["management"] = [
            value for value in self.resources["management"]
            if value["id"] != cleanup.cluster_resource(slot)
        ]
        target = self.targets[slot]
        del self.nodes[target["nodeId"]]
        state_name = target["terraformState"]["secret"]
        del self.objects["management", "radius-system", "secret", state_name]
        del self.objects["management", cleanup.NAMESPACE, "secret", target["accessSecret"]["name"]]

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "docker":
            command = args[3:]
            if command[:2] == ["ps", "-aq"]:
                return "\n".join(self.nodes)
            if command[:3] == ["inspect", "--type", "container"]:
                return json.dumps([self.nodes[key] for key in command[3:]])
            if command[:2] == ["image", "inspect"]:
                return json.dumps([{"Id": "sha256:" + "b"*64}])
        if args[0] == "rad":
            slot = args[args.index("--workspace")+1].removeprefix("radplanes-local-")
            command = args[3:]
            if command[:2] == ["workspace", "show"]:
                return json.dumps({
                    "connection": {"kind": "kubernetes", "context": f"radplanes-local-{slot}"},
                    "scope": cleanup.SCOPE.rsplit("/providers", 1)[0],
                })
            if command[:2] == ["app", "list"]:
                return json.dumps(self.apps[slot])
            if command[:2] == ["resource", "list"]:
                values = self.resources[slot]
                if "--application" in command:
                    app = command[command.index("--application")+1]
                    values = [
                        value for value in values if value["properties"].get("application")
                        == f"{cleanup.SCOPE}/Applications.Core/applications/{app}"
                    ]
                return json.dumps(values)
            if command[:2] == ["env", "show"]:
                return json.dumps({
                    "id": f"{cleanup.SCOPE}/Applications.Core/environments/{slot}",
                    "properties": {"compute": {"namespace": f"radplanes-local-{slot}"}},
                })
            if command[:2] == ["app", "delete"]:
                name = command[2]
                self.mutations.append((
                    "radius-app", slot if not name.startswith("cluster-") else name,
                ))
                if not self.app_sticks:
                    self.apps[slot] = [app for app in self.apps[slot] if app["name"] != name]
                    removed = [
                        value for value in self.resources[slot]
                        if value["properties"]["application"]
                        == f"{cleanup.SCOPE}/Applications.Core/applications/{name}"
                    ]
                    for resource in removed:
                        for owner_slot, _, owner_name, state_name in APP_STATE_CASES:
                            if slot == owner_slot and resource["name"] == owner_name:
                                self.objects.pop((slot, "radius-system", "secret", state_name))
                    self.resources[slot] = [
                        value for value in self.resources[slot]
                        if value["properties"]["application"]
                        != f"{cleanup.SCOPE}/Applications.Core/applications/{name}"
                    ]
                return ""
        if args[0] == "kubectl":
            slot = args[args.index("--context")+1].removeprefix("radplanes-local-")
            command = args[6:]
            namespace = ""
            if command[0] == "-n":
                namespace, command = command[1], command[2:]
            if command[:2] == ["get", "deployments"]:
                return json.dumps({"items": self.deployments})
            if command[:2] == ["get", "pods"]:
                return '{"items":[]}'
            if command[:2] == ["get", "secrets"]:
                return json.dumps({"items": [
                    obj for (s, ns, kind, _), obj in self.objects.items()
                    if s == slot and ns == namespace and kind == "secret"
                ]})
            if command[0] == "get":
                obj = self.objects.get((slot, namespace, command[1], command[2]))
                return json.dumps(obj) if obj else ""
            if command[0] == "patch":
                patch = json.loads(command[command.index("-p")+1])
                assert patch[0]["path"] == "/metadata/uid"
                self.mutations.append(("quiesce", command[2]))
                return ""
        if args[:3] == ["kind", "delete", "cluster"]:
            assert args[args.index("--name")+1] == cleanup.MANAGEMENT
            self.mutations.append(("kind-management", "management"))
            del self.nodes[self.targets["management"]["nodeId"]]
            return ""
        raise AssertionError(f"Unmocked command: {args}")


@pytest.fixture
def scenario(local_state, monkeypatch):
    monkeypatch.setattr(cleanup, "STATE", local_state)
    common.private_dir(local_state)
    targets = {}
    for index, slot in enumerate(cleanup.SLOTS):
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        path = "home/.kube/config" if slot == "management" else f"exports/{slot}.kubeconfig"
        common.write_private(
            local_state / path,
            yaml.safe_dump(access(slot, f"https://127.0.0.1:{35495+index}")),
        )
        targets[slot] = {
            "clusterId": f"kind://radplanes-local-{slot}", "clusterUid": str(UUID(int=index+1)),
            "context": f"radplanes-local-{slot}", "workspace": f"radplanes-local-{slot}",
            "group": cleanup.GROUP, "kubeconfig": path, "radiusConfig": "cleanup-radius.yaml",
            "namespace": f"radplanes-local-{slot}-{role}", "namespaceUid": f"namespace-{slot}-uid",
            "nodeId": str(index+1)*64,
            "nodeAddress": f"172.18.0.{index+2}",
        }
        if slot != "management":
            targets[slot].update(
                terraformState={
                    "secret": CLUSTER_STATE_NAMES[slot], "uid": "state-" + slot,
                    "lineage": "lineage-" + slot, "serial": 1,
                },
                accessSecret={
                    "name": f"radplanes-local-{slot}-access",
                    "namespace": cleanup.NAMESPACE, "uid": "access-" + slot,
                },
            )
    common.write_private(local_state / "cleanup-radius.yaml", "{}")
    fake = Offline(local_state, targets)
    monkeypatch.setattr(cleanup, "Commands", lambda: fake)
    monkeypatch.setattr(
        cleanup, "load_inputs", lambda: (targets, {"source_revision": REVISION, "images": IMAGES}),
    )
    monkeypatch.setattr(
        cleanup.Cleanup, "native_delete", lambda instance, slot: fake.native_delete(instance, slot),
    )
    monkeypatch.setattr(cleanup.Cleanup, "check_faults", lambda self: None)
    return local_state, targets, fake


def test_preview_real_entrypoint_never_mutates(scenario, capsys):
    state, _, fake = scenario
    assert cleanup.main([]) == 0
    assert not fake.mutations
    assert not list(state.glob("evidence/cleanup-*.json"))
    assert json.loads(capsys.readouterr().out)["result"] == "preview"
    for slot in cleanup.SLOTS:
        assert any(
            "--context" in call and call[call.index("--context")+1] == f"radplanes-local-{slot}"
            for call in fake.calls
        )


def test_full_cleanup_uses_owners_and_independent_read_only_verify(scenario, capsys):
    state, _, fake = scenario
    assert cleanup.main(["--execute"]) == 0
    expected = (
        [("quiesce", name) for name in ("management-api", "provisioner")]
        + [("radius-app", slot) for slot in cleanup.CHILDREN]
        + [
            action for slot in cleanup.CHILDREN
            for action in (("radius-cluster", slot), ("radius-app", f"cluster-{slot}"))
        ]
        + [("radius-app", "management"), ("kind-management", "management")]
    )
    assert fake.mutations == expected
    record_path, = state.glob("evidence/cleanup-*.json")
    record = json.loads(record_path.read_text())
    assert record["scope"] == "cleanup"
    assert record["result"] == "resources_removed"
    assert record["unrelatedContainerIds"] == ["f"*64]
    assert "synthetic-key" not in record_path.read_text()
    fake.mutations.clear()
    fake.calls.clear()
    fake.nodes["e"*64] = {"Id": "e"*64, "Name": "/new-other", "Config": {"Labels": {}}}
    assert cleanup.main(["--verify", str(record_path)]) == 0
    assert not fake.mutations
    assert all(call[0] == "docker" and call[3] in ("ps", "inspect") for call in fake.calls)
    assert "synthetic-key" not in capsys.readouterr().out


@pytest.mark.parametrize("failure", [
    "cluster-uid", "namespace-uid", "node-id", "state-uid", "state-lineage", "access-uid",
    "partial-radius", "extra-container", "missing-child", "access-private", "exec-plugin",
    "foreign-state", "foreign-access", "changed-ca", "changed-internal-ip",
])
def test_all_ownership_checked_before_any_mutation(scenario, failure):
    state, targets, fake = scenario
    slot = "isolated-1-data"
    target = targets[slot]
    if failure == "cluster-uid":
        fake.objects[slot, "", "namespace", "kube-system"]["metadata"]["uid"] = "foreign"
    elif failure == "namespace-uid":
        fake.objects[slot, "", "namespace", target["namespace"]]["metadata"]["uid"] = "foreign"
    elif failure == "node-id":
        target["nodeId"] = "a"*64
    elif failure in ("state-uid", "state-lineage"):
        target["terraformState"]["uid" if failure == "state-uid" else "lineage"] = "foreign"
    elif failure == "access-uid":
        target["accessSecret"]["uid"] = "foreign"
    elif failure == "partial-radius":
        fake.resources[slot][0]["properties"]["provisioningState"] = "Updating"
    elif failure == "extra-container":
        fake.nodes["e"*64] = {
            "Id": "e"*64, "Name": "/radplanes-local-foreign-control-plane",
            "Config": {"Labels": {"io.x-k8s.kind.cluster": "radplanes-local-foreign"}},
        }
    elif failure == "missing-child":
        del fake.nodes[target["nodeId"]]
    elif failure == "access-private":
        (state / target["kubeconfig"]).chmod(0o644)
    elif failure == "exec-plugin":
        path = state / target["kubeconfig"]
        value = yaml.safe_load(path.read_text())
        value["users"][0]["user"]["exec"] = {"command": "unexpected"}
        common.write_private(path, yaml.safe_dump(value))
    elif failure == "foreign-state":
        fake.objects["management", "radius-system", "secret", "foreign"] = {
            "metadata": {"name": "foreign", "uid": "foreign-uid"},
        }
    elif failure == "foreign-access":
        fake.objects["management", cleanup.NAMESPACE, "secret", "foreign"] = {
            "metadata": {"name": "foreign", "uid": "foreign-uid"},
        }
    elif failure in ("changed-ca", "changed-internal-ip"):
        stored = fake.objects[
            "management", cleanup.NAMESPACE, "secret", target["accessSecret"]["name"],
        ]
        value = yaml.safe_load(base64.b64decode(stored["data"]["kubeconfig"]))
        cluster = value["clusters"][0]["cluster"]
        if failure == "changed-ca":
            cluster["certificate-authority-data"] = encoded("different-ca")
        else:
            cluster["server"] = "https://172.18.0.99:6443"
        stored["data"]["kubeconfig"] = encoded(yaml.safe_dump(value))
    assert cleanup.main(["--execute"]) == 1
    assert not fake.mutations
    evidence, = state.glob("evidence/cleanup-*.json")
    assert json.loads(evidence.read_text())["result"] == "failed"


def test_zero_cli_exit_with_remaining_app_stops_before_cluster_delete(scenario):
    state, _, fake = scenario
    fake.app_sticks = True
    assert cleanup.main(["--execute"]) == 1
    assert fake.mutations == [
        ("quiesce", "management-api"), ("quiesce", "provisioner"),
        ("radius-app", "shared-data"),
    ]
    evidence, = state.glob("evidence/cleanup-*.json")
    assert "Radius application remains" in json.loads(evidence.read_text())["error"]


def test_radius_delete_failure_retains_evidence_and_never_falls_through(scenario):
    state, _, fake = scenario
    fake.delete_fails = True
    common.write_private(state / "evidence/old-failed.json", {"result": "failed"})
    assert cleanup.main(["--execute"]) == 1
    assert not any(action == "kind-management" for action, _ in fake.mutations)
    assert (state / "evidence/old-failed.json").read_text() == '{\n  "result": "failed"\n}\n'
    evidence, = state.glob("evidence/cleanup-*.json")
    assert json.loads(evidence.read_text())["result"] == "failed"


def test_lost_unrelated_container_fails_final_proof_without_deleting_others(scenario, monkeypatch):
    state, _, fake = scenario

    def delete(instance, slot):
        fake.native_delete(instance, slot)
        if slot == cleanup.CHILDREN[-1]:
            del fake.nodes["f"*64]

    monkeypatch.setattr(cleanup.Cleanup, "native_delete", delete)
    assert cleanup.main(["--execute"]) == 1
    evidence, = state.glob("evidence/cleanup-*.json")
    assert "unrelated Docker container disappeared" in json.loads(evidence.read_text())["error"]
    assert not any("rm" in call or "rmi" in call for call in fake.calls)


def test_renamed_child_id_is_not_mistaken_for_deletion(scenario, monkeypatch):
    _, targets, fake = scenario
    ticks = iter(range(0, 100_000, 1000))
    monkeypatch.setattr(cleanup, "time", type("Clock", (), {
        "monotonic": lambda: next(ticks), "sleep": lambda _: None,
    }))

    def delete(instance, slot):
        fake.native_delete(instance, slot)
        node_id = targets[slot]["nodeId"]
        fake.nodes[node_id] = {"Id": node_id, "Name": "/renamed", "Config": {"Labels": None}}

    monkeypatch.setattr(cleanup.Cleanup, "native_delete", delete)
    assert cleanup.main(["--execute"]) == 1
    assert ("radius-cluster", "shared-data") in fake.mutations
    assert ("radius-app", "cluster-shared-data") not in fake.mutations
    assert not any(action == "kind-management" for action, _ in fake.mutations)


def test_missing_export_blocks_without_a_command(local_state, monkeypatch):
    monkeypatch.setattr(cleanup, "STATE", local_state)
    fake = type("Unused", (), {"calls": []})()
    monkeypatch.setattr(cleanup, "Commands", lambda: fake)
    assert cleanup.main(["--execute"]) == 1
    assert not fake.calls


def test_no_direct_child_or_unrelated_container_delete_in_operator():
    source = (LOCAL / "cleanup.py").read_text()
    assert '"kind", "delete", "cluster", "--name", MANAGEMENT' in source
    assert '"docker", "rm"' not in source
    assert '"--force"' not in source
    assert "bstore" not in source


def real_inputs(state, targets):
    config = {
        "version": 1, "provider": "local", "projectName": "radplanes",
        "allocations": {
            slot: {
                "slot": slot, "clusterName": f"radplanes-local-{slot}",
                "context": f"radplanes-local-{slot}", "gatewayPort": 35490+index,
                "apiPort": 35495+index,
            }
            for index, slot in enumerate(cleanup.SLOTS)
        },
        "recipes": {
            role: {
                "digest": "sha256:"+"c"*64, "moduleServer": "local-module-"+"c"*20,
                "reference": (
                    "http://local-module-"+"c"*20+".radius-system.svc.cluster.local:18080/"
                    +"c"*64+".tar.gz"
                ),
            }
            for role in ("cluster", "postgresql", "redis", "gateway")
        },
        "images": IMAGES,
        "managementCluster": {
            "clusterId": targets["management"]["clusterId"],
            "uid": targets["management"]["clusterUid"],
            "nodeAddress": "172.18.0.2", "serviceAddress": "10.96.0.1",
            "caSHA256": cleanup.digest(b"synthetic-ca"),
        },
    }
    export = {
        "version": 1, "environment": "local", "project": "radplanes",
        "targets": {
            slot: {
                "context": value["context"], "namespace": value["namespace"],
                "namespace_uid": value["namespaceUid"], "cluster_uid": value["clusterUid"],
                "cluster_id": value["clusterId"], "kubeconfig": value["kubeconfig"],
                "local": {
                    "node": {"id": value["nodeId"], "address": value["nodeAddress"]},
                    **({
                        "access_secret": value["accessSecret"]["name"],
                        "access_secret_uid": value["accessSecret"]["uid"],
                    } if slot != "management" else {}),
                },
            }
            for slot, value in targets.items()
        },
    }
    export["targets"]["management"]["kubeconfig"] = "management.kubeconfig"
    common.write_private(
        state / "management.kubeconfig", (state / "home/.kube/config").read_text(),
    )
    bootstrap = {
        "name": cleanup.MANAGEMENT, "context": cleanup.MANAGEMENT,
        "secretEncryptionVerified": True, "nodeId": targets["management"]["nodeId"],
        "nodeAddress": "172.18.0.2",
    }
    source_file = "sql/management.sql"
    images = {
        "version": 1, "content_verified": True, "source_revision": REVISION,
        **{
            role: {
                "reference": IMAGES[role]["reference"], "image_id": IMAGES[role]["imageId"],
                "source_hashes": {
                    source_file: cleanup.digest((cleanup.ROOT / source_file).read_bytes()),
                },
            }
            for role in ("api", "provisioner")
        },
    }
    export["local_images"] = images
    radius = {"workspaces": {"default": cleanup.MANAGEMENT, "items": {
        f"radplanes-local-{slot}": {
            "connection": {"kind": "kubernetes", "context": f"radplanes-local-{slot}"},
            "scope": cleanup.SCOPE.rsplit("/providers", 1)[0],
        }
        for slot in cleanup.SLOTS
    }}}
    common.write_private(state / "cleanup-radius.yaml", yaml.safe_dump(radius))
    for name, value in (
        ("provisioning.json", config), ("acceptance.json", export),
        ("management-created.json", bootstrap), ("runtime-images.json", images),
    ):
        common.write_private(state / name, value)


def test_real_entrypoint_validates_protected_runtime_export_and_bootstrap(scenario, monkeypatch):
    state, targets, _ = scenario
    real_inputs(state, targets)
    monkeypatch.setattr(cleanup, "load_inputs", LOAD_INPUTS)
    assert cleanup.main(["--execute"]) == 0
    record, = state.glob("evidence/cleanup-*.json")
    value = json.loads(record.read_text())
    assert value["configSHA256"] == cleanup.digest((state / "provisioning.json").read_bytes())
    assert value["source_revision"] == REVISION


@pytest.mark.parametrize("name,field,value", [
    ("provisioning.json", "provider", "azure"),
    ("acceptance.json", "project", "other"),
    ("acceptance.json", "local_images", {}),
    ("management-created.json", "nodeId", "e"*64),
    ("management-created.json", "secretEncryptionVerified", False),
    ("runtime-images.json", "content_verified", False),
])
def test_input_provenance_failures_precede_all_live_commands(
    scenario, monkeypatch, name, field, value,
):
    state, targets, fake = scenario
    real_inputs(state, targets)
    data = json.loads((state / name).read_text())
    data[field] = value
    common.write_private(state / name, data)
    monkeypatch.setattr(cleanup, "load_inputs", LOAD_INPUTS)
    assert cleanup.main(["--execute"]) == 1
    assert not fake.calls


def test_native_cluster_delete_uses_exact_api_and_authenticated_json(scenario, monkeypatch):
    _, targets, fake = scenario
    observed = {}
    context = type("TLS", (), {"load_cert_chain": lambda *_: None})()
    monkeypatch.setattr(cleanup.ssl, "create_default_context", lambda **kw: context)

    class Client:
        def __init__(self, **kwargs):
            observed["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def delete(self, url, **kwargs):
            observed.update(url=url, request=kwargs)
            return type("Response", (), {"status_code": 202})()

    monkeypatch.setattr(cleanup.httpx, "Client", Client)
    instance = cleanup.Cleanup(fake, targets, True)
    NATIVE_DELETE(instance, "isolated-1-data")
    assert observed["client"]["verify"] is context
    assert observed["client"]["trust_env"] is False
    assert observed["client"]["follow_redirects"] is False
    assert observed["url"] == (
        "https://127.0.0.1:35495/apis/api.ucp.dev/v1alpha3"
        + cleanup.cluster_resource("isolated-1-data")
    )
    assert observed["request"] == {
        "params": {"api-version": "2025-08-01-preview"},
        "headers": {"Content-Type": "application/json", "Accept": "application/json"},
    }


@pytest.mark.parametrize("case,live_rule,should_fail", [
    ("none", False, False), ("not-restored", False, True), ("none", True, True),
    ("restored", False, False), ("old-error", False, False),
    ("pre-mutation-failure", False, False), ("missing-strategy", False, True),
    ("wrong-strategy", False, True), ("missing-identity", False, True),
    ("missing-probe", False, True), ("changed-pod", False, True),
    ("changed-node", False, True), ("changed-sandbox", False, True),
    ("invalid-attempt", False, True),
])
def test_fault_restoration_is_checked_from_real_runpath(
    scenario, monkeypatch, case, live_rule, should_fail,
):
    state, targets, fake = scenario
    common.write_private(state / "acceptance.json", {})

    def node(slot):
        return {
            "id": targets[slot]["nodeId"], "address": targets[slot]["nodeAddress"],
            "name": f"radplanes-local-{slot}-control-plane",
        }

    def sandbox():
        return {"id": "a"*64, "pid": 10, "inode": "20", "container_id": "e"*64}

    if case != "none":
        record = {
            "version": 1, "run_id": "b"*12,
            "strategy": "pod-network-namespace-iptables",
            "environment": "local", "project": "radplanes", "slot": "shared-data",
            "component": "data-reconciler", "pod_uid": str(UUID(int=201)),
            "cluster_uid": targets["shared-data"]["clusterUid"],
            "namespace_uid": targets["shared-data"]["namespaceUid"],
            "creation_attempted": True, "restored": True, "physical_restored": True,
            "original_rules_sha256": "c"*64, "restored_rules_sha256": "c"*64,
            "restoration_probe": {"ok": True}, "node": node("shared-data"),
            "sandbox": sandbox(),
        }
        if case == "not-restored":
            record["restored"] = False
        elif case == "old-error":
            record.update(outcome="restoration_failed", restoration_error="historical error")
        elif case == "pre-mutation-failure":
            for key in ("creation_attempted", "node", "sandbox", "pod_uid", "restoration_probe"):
                del record[key]
            record.update(outcome="activation_failed", restored=False, physical_restored=False)
        elif case == "missing-strategy":
            del record["strategy"]
        elif case == "wrong-strategy":
            record["strategy"] = "unknown"
        elif case == "missing-identity":
            del record["run_id"]
        elif case == "missing-probe":
            del record["restoration_probe"]
        elif case == "changed-pod":
            record["pod_uid"] = str(UUID(int=202))
        elif case == "changed-node":
            record["node"]["id"] = "f"*64
        elif case == "changed-sandbox":
            record["sandbox"]["inode"] = "21"
        elif case == "invalid-attempt":
            record["creation_attempted"] = "false"
        common.write_private(state / "evidence" / ("1"*32 + "-control-link.json"), record)
    checked = []

    class Fault:
        def __init__(self, configuration, slot, component, path, **kwargs):
            self.target = type("Target", (), {
                "cluster_uid": targets[slot]["clusterUid"],
                "namespace_uid": targets[slot]["namespaceUid"],
            })()
            self.kube = type("Kube", (), {
                "pod": lambda *args: {"metadata": {"uid": str(UUID(int=201))}},
            })()
            self.node = node(slot)
            self.slot = slot

        def verify_nodes(self):
            checked.append(self.slot)

        def sandbox_identity(self, pod):
            return sandbox()

        def network(self, *args):
            assert args == ("iptables", "-w", "2", "-S", "OUTPUT")
            return "plane-demo-fault-example" if live_rule else "-P OUTPUT ACCEPT"

    support = type("Support", (), {
        "base": type("Base", (), {"Configuration": lambda path: object()}),
        "LocalParentFault": Fault,
    })
    monkeypatch.setattr(cleanup, "fault_support", lambda: support)
    monkeypatch.setattr(cleanup.Cleanup, "check_faults", CHECK_FAULTS)
    assert cleanup.main(["--execute"]) == (1 if should_fail else 0)
    if should_fail:
        assert not fake.mutations
    else:
        assert set(checked) == set(cleanup.CHILDREN)


@pytest.mark.parametrize("change", [
    "gate-two-resources", "extra-owner", "foreign-module", "foreign-name", "foreign-type",
    "foreign-provider", "wrong-index", "string-index", "changed-input", "changed-output",
    "changed-cluster", "changed-images", "reordered-images", "wrong-type", "extra-trigger",
    "deposed", "tainted",
])
def test_full_recipe_image_owner_must_match_before_any_mutation(scenario, change):
    state_path, targets, fake = scenario
    slot = "isolated-1-data"
    secret = fake.objects[
        "management", "radius-system", "secret", targets[slot]["terraformState"]["secret"],
    ]
    state = json.loads(gzip.decompress(base64.b64decode(secret["data"]["tfstate"])))
    image_load = next(entry for entry in state["resources"] if entry["type"] == "terraform_data")
    instance = image_load["instances"][0]
    attributes = instance["attributes"]
    trigger = attributes["triggers_replace"]
    if change == "gate-two-resources":
        state["resources"].remove(image_load)
    elif change == "extra-owner":
        extra = json.loads(json.dumps(image_load))
        extra["name"] = "unrelated"
        state["resources"].append(extra)
    elif change in ("foreign-module", "foreign-name", "foreign-type", "foreign-provider"):
        field = change.removeprefix("foreign-")
        image_load[field] = "unrelated"
    elif change in ("wrong-index", "string-index"):
        instance["index_key"] = 1 if change == "wrong-index" else "0"
    elif change in ("changed-input", "changed-output"):
        attributes[change.removeprefix("changed-")] = {"value": "unexpected", "type": "string"}
    elif change == "changed-cluster":
        trigger["value"]["cluster_id"] = "foreign-cluster"
    elif change == "changed-images":
        trigger["value"]["images"][1] = "localhost/radplanes-plane-provisioner:" + "f"*40
    elif change == "reordered-images":
        trigger["value"]["images"].reverse()
    elif change == "wrong-type":
        trigger["type"][1]["images"] = ["set", "string"]
    elif change == "extra-trigger":
        trigger["value"]["other"] = "foreign"
    elif change == "deposed":
        instance["deposed"] = "superseded"
    elif change == "tainted":
        instance["status"] = "tainted"
    secret["data"]["tfstate"] = base64.b64encode(gzip.compress(json.dumps(state).encode())).decode()
    assert cleanup.main(["--execute"]) == 1
    assert fake.mutations == []
    evidence, = state_path.glob("evidence/cleanup-*.json")
    assert json.loads(evidence.read_text())["result"] == "failed"


def test_application_bound_backend_names_cover_all_cleanup_runpaths(scenario):
    state, targets, fake = scenario
    for slot, kind, name, state_name in APP_STATE_CASES:
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        fake.resources[slot].append({
            "name": name, "type": kind, "id": f"{cleanup.SCOPE}/{kind}/{name}",
            "properties": {
                "environment": f"{cleanup.SCOPE}/Applications.Core/environments/{slot}",
                "application": f"{cleanup.SCOPE}/Applications.Core/applications/{role}",
                "provisioningState": "Succeeded",
            },
        })
        fake.objects[slot, "radius-system", "secret", state_name] = {
            "metadata": {
                "name": state_name, "namespace": "radius-system", "uid": f"state-{slot}-{name}",
                "labels": {"tfstate": "true", "app.kubernetes.io/managed-by": "terraform"},
            },
        }
    assert cleanup.main(["--execute"]) == 0
    path, = state.glob("evidence/cleanup-*.json")
    record = json.loads(path.read_text())
    for slot, _, _, state_name in APP_STATE_CASES:
        assert state_name in {owner["name"] for owner in record["stateOwners"][slot]}
    for slot in cleanup.CHILDREN:
        expected = CLUSTER_STATE_NAMES[slot]
        assert record["targets"][slot]["terraformState"]["secret"] == expected
        assert any(
            call[0] == "kubectl" and ["get", "secret", expected] == call[8:11]
            for call in fake.calls
        )
        assert targets[slot]["terraformState"]["secret"] == expected


@pytest.mark.parametrize("variant", ["application-omitted", "legacy-sha1", "legacy-alongside"])
def test_old_or_legacy_backend_names_are_refused_without_adoption(scenario, variant):
    _, targets, fake = scenario
    slot = "isolated-1-data"
    resource_id = cleanup.cluster_resource(slot)
    if variant == "application-omitted":
        source = f"provision-{slot}-{resource_id}".lower().encode()
        old_name = "tfstate-default-" + hashlib.sha256(source).hexdigest()[:40]
    else:
        source = f"provision-{slot}-cluster-{slot}-{resource_id}".lower().encode()
        old_name = "tfstate-default-" + hashlib.sha1(source, usedforsecurity=False).hexdigest()
    current = ("management", "radius-system", "secret", targets[slot]["terraformState"]["secret"])
    old = json.loads(json.dumps(fake.objects[current]))
    old["metadata"]["name"] = old_name
    if variant != "legacy-alongside":
        del fake.objects[current]
    fake.objects["management", "radius-system", "secret", old_name] = old
    assert cleanup.main(["--execute"]) == 1
    assert fake.mutations == []
