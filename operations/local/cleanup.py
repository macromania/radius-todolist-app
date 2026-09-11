#!/usr/bin/env python3
"""Preview full local teardown; --execute destroys demo data through its owners."""

from __future__ import annotations

import argparse
import base64
import gzip
import importlib.util
import json
import re
import ssl
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import yaml
from common import (
    GROUP,
    MANAGEMENT,
    NAMESPACE,
    NODE_IMAGE,
    ROOT,
    SCOPE,
    STATE,
    Commands,
    LocalError,
    containers,
    digest,
    docker,
    node_address,
    private_dir,
    write_private,
)

SLOTS = ("management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data")
CHILDREN = ("shared-data", "isolated-1-data", "shared-control", "isolated-1-control")
API_VERSION = "2025-08-01-preview"
TERMINAL = {"Succeeded", "Failed", "Canceled"}
DOCKER_ID = re.compile(r"[a-f0-9]{64}")


def require(condition: object, message: str) -> None:
    if not condition:
        raise LocalError(message)


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def protected(value: str | Path) -> Path:
    path = Path(value)
    path = path if path.is_absolute() else STATE / path
    require(".." not in path.parts and path.is_relative_to(STATE), "Path must stay in .state/local")
    require(not any(part.is_symlink() for part in path.parents), "Symlinked state parent refused")
    for part in [STATE, *path.relative_to(STATE).parents]:
        if not part.is_absolute():
            part = STATE / part
        require(not part.is_symlink(), "Symlinked local state is refused")
        require(part.is_dir() and not part.stat().st_mode & 0o077, "Local state must be private")
    require(path.is_file() and not path.is_symlink(), "Protected local state file is missing")
    require(not path.stat().st_mode & 0o077, "Local state file must be private")
    return path


def read_json(value: str | Path) -> dict:
    result = json.loads(protected(value).read_text())
    require(isinstance(result, dict), "Expected a local state object")
    return result


def items(value: object) -> list:
    if isinstance(value, dict):
        value = value.get("items", value.get("value"))
    require(isinstance(value, list), "Unexpected inventory response")
    return value


def cluster_resource(slot: str) -> str:
    require(slot in CHILDREN, "Unknown child allocation")
    return f"{SCOPE}/Demo.Platform/clusters/{slot}"


def backend_secret_name(resource: dict) -> str:
    properties = resource["properties"]
    environment = properties["environment"].rsplit("/", 1)[-1]
    application = properties["application"].rsplit("/", 1)[-1]
    require(environment and application, "Full cleanup requires application-bound Terraform state")
    key = f"{environment}-{application}-{resource['id']}".lower()
    return "tfstate-default-" + digest(key.encode())[:40]


def fault_support():
    spec = importlib.util.spec_from_file_location(
        "plane_demo_cleanup_faults", ROOT / "harness/local/fault-parent-link.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def selected_access(target: dict, slot: str) -> tuple[Path, dict, dict]:
    path = protected(target["kubeconfig"])
    if slot == "management":
        require(path == STATE / "home/.kube/config", "Management must use its bootstrap kubeconfig")
    value = yaml.safe_load(path.read_text())
    context = f"radplanes-local-{slot}"
    require(
        value.get("apiVersion") == "v1" and value.get("kind") == "Config"
        and all(len(value[key]) == 1 for key in ("contexts", "clusters", "users")),
        "Only one explicit kubeconfig identity is admitted",
    )
    require(target["context"] == context, "Foreign kubecontext")
    require(value.get("current-context") == context, "Kubeconfig selects another context")
    contexts = [item["context"] for item in value["contexts"] if item["name"] == context]
    require(len(contexts) == 1, "Kubecontext is not unique")
    clusters = [
        item["cluster"] for item in value["clusters"] if item["name"] == contexts[0]["cluster"]
    ]
    users = [item["user"] for item in value["users"] if item["name"] == contexts[0]["user"]]
    require(len(clusters) == len(users) == 1, "Kubeconfig references are ambiguous")
    cluster, user = clusters[0], users[0]
    require(
        cluster.get("server") == f"https://127.0.0.1:{35495 + SLOTS.index(slot)}"
        and cluster.get("certificate-authority-data")
        and set(cluster) <= {"server", "certificate-authority-data", "tls-server-name"}
        and (slot == "management" or cluster.get("tls-server-name") == context),
        "Only the verified loopback cluster endpoint is admitted",
    )
    require(
        set(user) == {"client-certificate-data", "client-key-data"} and all(user.values()),
        "Only embedded certificate authentication is admitted; no exec/token plugins",
    )
    return path, cluster, user


class Cleanup:
    def __init__(self, commands: Commands, targets: dict, execute: bool):
        self.commands = commands
        self.targets = targets
        self.execute = execute
        self.record = {
            "version": 1,
            "scope": "cleanup",
            "runId": uuid4().hex[:12],
            "startedAt": stamp(),
            "result": "incomplete",
            "steps": [],
        }
        self.path = STATE / "evidence" / f"cleanup-{self.record['runId']}.json"
        self.inventory: dict = {}

    def step(self, action: str, identity: str, **proof) -> None:
        self.record["steps"].append({
            "at": stamp(), "action": action, "identity": identity, **proof,
        })
        if self.execute:
            write_private(self.path, self.record)

    def kube(self, slot: str, *args: str) -> list[str]:
        target = self.targets[slot]
        return [
            "kubectl", "--kubeconfig", str(protected(target["kubeconfig"])),
            "--context", target["context"], "--request-timeout=30s", *args,
        ]

    def get(self, slot: str, kind: str, name: str, namespace: str = "") -> dict | None:
        args = ["-n", namespace] if namespace else []
        output = self.commands.run(
            self.kube(slot, *args, "get", kind, name, "--ignore-not-found", "-o", "json")
        )
        return json.loads(output) if output.strip() else None

    def rad(self, slot: str, *args: str) -> str:
        target = self.targets[slot]
        config = protected(target["radiusConfig"])
        home = private_dir(STATE / "cleanup-homes" / slot)
        private_dir(home / ".kube")
        link = home / ".kube/config"
        kubeconfig = protected(target["kubeconfig"])
        if not link.exists() and not link.is_symlink():
            link.symlink_to(kubeconfig)
        require(link.is_symlink() and link.resolve() == kubeconfig, "Unexpected cleanup HOME link")
        previous = self.commands.env
        self.commands.env = {**previous, "HOME": str(home), "KUBECONFIG": str(kubeconfig)}
        try:
            return self.commands.run(
                ["rad", "--config", str(config), *args, "--workspace", target["workspace"]],
                timeout=600,
            )
        finally:
            self.commands.env = previous

    def radius_list(self, slot: str, *args: str) -> list:
        return items(json.loads(self.rad(slot, *args, "--group", GROUP, "-o", "json")))

    def native_delete(self, slot: str) -> None:
        _, cluster, user = selected_access(self.targets["management"], "management")
        directory = private_dir(STATE / "client" / f"cleanup-{self.record['runId']}")
        files = {}
        for key, encoded in (
            ("ca", cluster["certificate-authority-data"]),
            ("cert", user["client-certificate-data"]),
            ("key", user["client-key-data"]),
        ):
            files[key] = directory / f"{key}.pem"
            write_private(files[key], base64.b64decode(encoded, validate=True))
        context = ssl.create_default_context(cafile=str(files["ca"]))
        context.load_cert_chain(str(files["cert"]), str(files["key"]))
        with httpx.Client(
            verify=context, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(60, connect=5),
        ) as client:
            response = client.delete(
                cluster["server"] + "/apis/api.ucp.dev/v1alpha3" + cluster_resource(slot),
                params={"api-version": API_VERSION},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        require(response.status_code in {200, 202, 204}, "Radius cluster DELETE was not accepted")

    def docker_nodes(self) -> dict[str, dict]:
        ids = containers(self.commands)
        require(all(DOCKER_ID.fullmatch(value) for value in ids), "Docker returned invalid IDs")
        nodes = self.commands.json(docker("inspect", "--type", "container", *ids)) if ids else []
        result = {}
        for node in nodes:
            name = node["Name"].removeprefix("/")
            owner = (node["Config"].get("Labels") or {}).get("io.x-k8s.kind.cluster", "")
            if name.startswith("radplanes-local-") or owner.startswith("radplanes-local-"):
                slot = owner.removeprefix("radplanes-local-")
                require(slot in SLOTS and slot not in result, "Extra/foreign project Docker node")
                node_address(node, f"radplanes-local-{slot}")
                result[slot] = node
        self.current_ids = set(ids)
        return result

    def namespace(self, slot: str, name: str, uid: str) -> None:
        value = self.get(slot, "namespace", name)
        require(
            value and value["metadata"]["name"] == name and value["metadata"]["uid"] == uid,
            f"{slot}: namespace identity changed or missing",
        )

    def state_owner(self, slot: str) -> None:
        target = self.targets[slot]
        resource_id = cluster_resource(slot)
        name = backend_secret_name(self.inventory["management"]["clusters"][slot])
        stored = self.get("management", "secret", name, "radius-system")
        require(stored, f"{slot}: Terraform owner is missing; no direct child deletion")
        metadata = stored["metadata"]
        labels = metadata.get("labels", {})
        require(
            metadata["name"] == name and metadata.get("uid")
            and metadata["namespace"] == "radius-system"
            and labels.get("tfstate") == "true"
            and labels.get("app.kubernetes.io/managed-by") == "terraform",
            "Foreign Terraform state owner",
        )
        state = json.loads(
            gzip.decompress(base64.b64decode(stored["data"]["tfstate"], validate=True))
        )
        require(
            isinstance(state["lineage"], str) and state["lineage"]
            and type(state["serial"]) is int and state["serial"] > 0
            and state["terraform_version"] == "1.15.8",
            f"{slot}: Terraform state is incomplete or uses an unexpected version",
        )
        summary = {
            "secret": name, "uid": metadata["uid"],
            "lineage": state["lineage"], "serial": state["serial"],
        }
        require(
            "terraformState" not in target or target["terraformState"] == summary,
            f"{slot}: Terraform owner changed since preflight",
        )
        managed = [entry for entry in state["resources"] if entry["mode"] == "managed"]
        kinds = [entry for entry in managed if entry["type"] == "kind_cluster"]
        accesses = [entry for entry in managed if entry["type"] == "kubernetes_secret_v1"]
        image_loads = [entry for entry in managed if entry["type"] == "terraform_data"]
        require(
            len(managed) == 3 and len(kinds) == len(accesses) == len(image_loads) == 1,
            "Full cleanup requires exactly the cluster, image-load, and access Terraform owners",
        )
        require(
            len(kinds[0]["instances"]) == len(accesses[0]["instances"])
            == len(image_loads[0]["instances"]) == 1,
            "Partial Terraform ownership",
        )
        attrs = kinds[0]["instances"][0]["attributes"]
        name = f"radplanes-local-{slot}"
        require(
            attrs["name"] == name and attrs["id"] == f"{name}-{NODE_IMAGE}"
            and attrs["node_image"] == NODE_IMAGE and attrs["completed"] is True
            and attrs["kubeconfig"] and attrs["client_key"],
            "Terraform does not describe the completed, owned child",
        )
        image_load = image_loads[0]
        instance = image_load["instances"][0]
        image_attrs = instance["attributes"]
        require(
            image_load.get("module") == "module.default"
            and image_load.get("name") == "images"
            and image_load.get("provider") == 'provider["terraform.io/builtin/terraform"]'
            and type(instance.get("index_key")) is int and instance["index_key"] == 0
            and instance.get("schema_version") == 0 and not instance.get("deposed")
            and not instance.get("status")
            and image_attrs["input"] is None and image_attrs["output"] is None
            and image_attrs.get("triggers_replace") == {
                "type": ["object", {"cluster_id": "string", "images": ["list", "string"]}],
                "value": {
                    "cluster_id": attrs["id"],
                    "images": [
                        self.record["images"][role]["reference"] for role in ("api", "provisioner")
                    ],
                },
            },
            "Terraform image-load owner or its inspected image references changed",
        )
        access = target["accessSecret"]
        require(
            access["name"] == f"{name}-access" and access["namespace"] == NAMESPACE,
            "Foreign access Secret",
        )
        tracked = accesses[0]["instances"][0]["attributes"]["metadata"][0]
        require(
            tracked["name"] == access["name"] and tracked["namespace"] == NAMESPACE,
            "Terraform access reference differs",
        )
        live = self.get("management", "secret", access["name"], NAMESPACE)
        require(
            live and live["metadata"]["uid"] == access["uid"]
            and live["metadata"].get("labels", {}).get("radplanes.local/slot") == slot
            and live["metadata"].get("annotations", {}).get(
                "radplanes.local/radius-resource"
            ) == resource_id,
            f"{slot}: access Secret ownership changed",
        )
        stored_access = yaml.safe_load(base64.b64decode(live["data"]["kubeconfig"], validate=True))
        require(
            len(stored_access["contexts"]) == len(stored_access["clusters"])
            == len(stored_access["users"]) == 1
            and stored_access["current-context"] == f"radplanes-local-{slot}",
            "Ambiguous protected child access",
        )
        _, exported_cluster, exported_user = selected_access(target, slot)
        original = yaml.safe_load(attrs["kubeconfig"])
        require(
            original["clusters"][0]["cluster"]["certificate-authority-data"]
            == exported_cluster["certificate-authority-data"]
            and original["users"][0]["user"] == exported_user
            and attrs["client_key"].encode()
            == base64.b64decode(exported_user["client-key-data"], validate=True),
            "Terraform kind credentials do not match the owned child access",
        )
        internal = stored_access["clusters"][0]["cluster"]
        require(
            internal.get("server") == f"https://{self.nodes[slot]['address']}:6443"
            and set(internal) <= {"server", "certificate-authority-data", "tls-server-name"}
            and internal.get("tls-server-name") == f"radplanes-local-{slot}"
            and internal.get("certificate-authority-data")
            == exported_cluster["certificate-authority-data"]
            and stored_access["users"][0]["user"] == exported_user,
            "Child access keys, CA, or internal node endpoint changed",
        )
        target["terraformState"] = summary

    def radius_inventory(self, slot: str) -> dict:
        target = self.targets[slot]
        scope = SCOPE.rsplit("/providers", 1)[0]
        workspace = json.loads(self.rad(slot, "workspace", "show", "-o", "json"))
        require(
            workspace.get("connection") == {"kind": "kubernetes", "context": target["context"]}
            and workspace.get("scope") == scope,
            f"{slot}: Radius workspace changed",
        )
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        apps = self.radius_list(slot, "app", "list")
        expected_apps = {role}
        if slot == "management":
            expected_apps.update(f"cluster-{child}" for child in CHILDREN)
        require(
            len(apps) == len(expected_apps) and {app["name"] for app in apps} == expected_apps,
            f"{slot}: unexpected Radius apps",
        )
        for app in apps:
            name = app["name"]
            environment = slot if name == role else "provision-" + name.removeprefix("cluster-")
            require(
                app["id"] == f"{SCOPE}/Applications.Core/applications/{name}"
                and app["properties"]["environment"]
                == f"{SCOPE}/Applications.Core/environments/{environment}",
                f"{slot}: foreign application/environment",
            )
        app_id = f"{SCOPE}/Applications.Core/applications/{role}"
        env_id = f"{SCOPE}/Applications.Core/environments/{slot}"
        environment = json.loads(self.rad(
            slot, "env", "show", slot, "--group", GROUP, "-o", "json",
        ))
        require(
            environment["id"] == env_id
            and environment["properties"]["compute"]["namespace"] == f"radplanes-local-{slot}",
            f"{slot}: foreign Radius compute namespace",
        )
        resources = self.radius_list(slot, "resource", "list")
        cluster_records = {}
        for resource in resources:
            kind, name, properties = resource["type"], resource["name"], resource["properties"]
            require(
                re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name)
                and resource["id"] == f"{SCOPE}/{kind}/{name}"
                and properties.get("provisioningState") in TERMINAL,
                f"{slot}: partial or foreign Radius resource",
            )
            if kind == "Demo.Platform/clusters":
                child = properties.get("slot")
                require(
                    slot == "management" and child in CHILDREN and name == child
                    and child not in cluster_records
                    and properties.get("environment")
                    == f"{SCOPE}/Applications.Core/environments/provision-{child}"
                    and properties.get("application")
                    == f"{SCOPE}/Applications.Core/applications/cluster-{child}"
                    and properties.get("clusterId") == f"kind://radplanes-local-{child}"
                    and properties.get("clusterName") == f"radplanes-local-{child}"
                    and properties.get("bootstrapAccessRef")
                    == f"kubernetes://{NAMESPACE}/radplanes-local-{child}-access#kubeconfig",
                    "Foreign or incomplete management Radius child owner",
                )
                cluster_records[child] = resource
            else:
                require(
                    kind in {
                        "Applications.Core/containers", "Applications.Datastores/redisCaches",
                        "Demo.Platform/postgreSqlDatabases", "Demo.Platform/gateways",
                    }
                    and properties.get("application") == app_id
                    and properties.get("environment") == env_id,
                    f"{slot}: unknown or foreign Radius application resource",
                )
        if slot == "management":
            require(set(cluster_records) == set(CHILDREN), "Every child requires its Radius owner")
        return {"app": role, "apps": apps, "resources": resources, "clusters": cluster_records}

    def deployments(self) -> list[dict]:
        namespace = self.targets["management"]["namespace"]
        deployments = items(self.commands.json(self.kube(
            "management", "-n", namespace, "get", "deployments",
            "-l", "radapp.io/application=management", "-o", "json",
        )))
        result = []
        for component in ("management-api", "provisioner"):
            matches = [
                item for item in deployments
                if item["metadata"].get("labels", {}).get("radapp.io/resource") == component
            ]
            require(len(matches) == 1, f"Expected one management {component} Deployment")
            deployment = matches[0]
            metadata = deployment["metadata"]
            labels = metadata.get("labels", {})
            require(
                metadata["name"] == component and metadata["namespace"] == namespace
                and metadata.get("uid")
                and labels.get("radapp.io/application") == "management"
                and labels.get("plane-demo/project") == "radplanes"
                and labels.get("plane-demo/component") == component,
                "Management Deployment name/labels do not prove ownership",
            )
            result.append(deployment)
        return result

    def preflight(self) -> None:
        nodes = self.docker_nodes()
        require(set(nodes) == set(SLOTS), "Full cleanup requires all five completed owned clusters")
        self.nodes = {
            slot: {"address": node_address(node, f"radplanes-local-{slot}")}
            for slot, node in nodes.items()
        }
        self.record["unrelatedContainerIds"] = sorted(
            self.current_ids - {node["Id"] for node in nodes.values()}
        )
        self.record["targets"] = {}
        for slot in SLOTS:
            target = self.targets[slot]
            selected_access(target, slot)
            require(
                target["clusterId"] == f"kind://radplanes-local-{slot}"
                and target["workspace"] == target["context"] == f"radplanes-local-{slot}"
                and target["group"] == GROUP,
                f"{slot}: foreign exported target",
            )
            require(nodes[slot]["Id"] == target["nodeId"], f"{slot}: Docker identity changed")
            require(
                self.nodes[slot]["address"] == target["nodeAddress"],
                f"{slot}: Docker address changed since export",
            )
            self.namespace(slot, "kube-system", target["clusterUid"])
            role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
            require(
                target["namespace"] == f"radplanes-local-{slot}-{role}",
                "Foreign application namespace",
            )
            self.namespace(slot, target["namespace"], target["namespaceUid"])
            self.inventory[slot] = self.radius_inventory(slot)
            self.verify_state_inventory(slot)
            self.record["targets"][slot] = {
                key: target[key] for key in
                ("clusterId", "clusterUid", "context", "namespace", "namespaceUid", "nodeId")
            }
            if slot != "management":
                self.state_owner(slot)
                self.record["targets"][slot].update(
                    terraformState=target["terraformState"], accessSecret=target["accessSecret"],
                )
        accesses = items(self.commands.json(self.kube(
            "management", "-n", NAMESPACE, "get", "secrets", "-o", "json",
        )))
        require(
            len(accesses) == len(CHILDREN)
            and {value["metadata"]["name"] for value in accesses}
            == {f"radplanes-local-{slot}-access" for slot in CHILDREN},
            "Unexpected management access Secret owners",
        )
        self.owned_deployments = self.deployments()
        for deployment in self.owned_deployments:
            role = "provisioner" if deployment["metadata"]["name"] == "provisioner" else "api"
            image = self.record["images"][role]
            actual = self.commands.json(docker("image", "inspect", image["reference"]))
            require(
                len(actual) == 1 and actual[0]["Id"] == image["imageId"]
                and [item["image"] for item in deployment["spec"]["template"]["spec"]["containers"]]
                == [image["reference"]],
                "Management runtime image differs from inspected/exported provenance",
            )
        self.record["deployments"] = [
            {
                "name": item["metadata"]["name"], "uid": item["metadata"]["uid"],
                "namespace": item["metadata"]["namespace"], "labels": item["metadata"]["labels"],
            }
            for item in self.owned_deployments
        ]
        self.check_faults()
        self.step("preflight_verified", "all-five-clusters")

    def verify_state_inventory(self, slot: str) -> None:
        expected = {}
        for resource in self.inventory[slot]["resources"]:
            if resource["type"] == "Applications.Core/containers":
                continue
            name = backend_secret_name(resource)
            expected[name] = resource
        states = items(self.commands.json(self.kube(
            slot, "-n", "radius-system", "get", "secrets", "-l", "tfstate=true", "-o", "json",
        )))
        require(
            len(states) == len(expected)
            and {value["metadata"]["name"] for value in states} == set(expected),
            f"{slot}: missing, extra, or chunked Terraform owner state",
        )
        self.record.setdefault("stateOwners", {})[slot] = [
            {"name": value["metadata"]["name"], "uid": value["metadata"]["uid"]}
            for value in states
        ]

    def check_faults(self) -> None:
        support = fault_support()
        configuration = support.base.Configuration(protected("acceptance.json"))

        def run(args, *, payload=None, timeout=30):
            return self.commands.run(args, data=payload, timeout=timeout)

        attempted = []
        for path in sorted((STATE / "evidence").glob("*.json")):
            record = read_json(path)
            recognized = path.name.startswith("fault-") or path.name.endswith(
                ("-management-link.json", "-control-link.json"),
            )
            if not recognized and record.get("strategy") != "pod-network-namespace-iptables":
                continue
            require(
                record.get("strategy") == "pod-network-namespace-iptables"
                and record.get("version") == 1
                and re.fullmatch(r"[a-f0-9]{12}", record.get("run_id", ""))
                and isinstance(record.get("creation_attempted", False), bool),
                "Malformed local fault journal identity or strategy",
            )
            slot = record.get("slot")
            require(slot in CHILDREN, "Unknown fault journal allocation")
            require(
                record.get("environment") == "local" and record.get("project") == "radplanes"
                and record.get("component") == slot.rsplit("-", 1)[1] + "-reconciler"
                and record.get("cluster_uid") == self.targets[slot]["clusterUid"]
                and record.get("namespace_uid") == self.targets[slot]["namespaceUid"],
                "Fault journal targets a different live cluster",
            )
            if record.get("creation_attempted"):
                require(
                    record.get("restored") is True and record.get("physical_restored") is True
                    and record.get("original_rules_sha256")
                    == record.get("restored_rules_sha256")
                    and DOCKER_ID.fullmatch(record.get("original_rules_sha256", ""))
                    and record.get("restoration_probe", {}).get("ok") is True,
                    "Restore the recorded parent fault before cleanup",
                )
                attempted.append(record)
        for slot in CHILDREN:
            component = slot.rsplit("-", 1)[1] + "-reconciler"
            fault = support.LocalParentFault(
                configuration, slot, component, self.path, operator=run,
                kube_factory=lambda target: support.base.Kubectl(target, runner=run),
            )
            fault.verify_nodes()
            require(
                fault.target.cluster_uid == self.targets[slot]["clusterUid"]
                and fault.target.namespace_uid == self.targets[slot]["namespaceUid"],
                "Acceptance fault target differs from cleanup export",
            )
            pod = fault.kube.pod(component)
            fault.sandbox = fault.sandbox_identity(pod)
            for record in attempted:
                if record["slot"] == slot:
                    require(
                        record.get("pod_uid") == pod["metadata"]["uid"]
                        and record.get("node") == fault.node
                        and record.get("sandbox") == fault.sandbox,
                        "Fault journal Pod or network namespace changed; inspect before cleanup",
                    )
            rules = fault.network("iptables", "-w", "2", "-S", "OUTPUT")
            require("plane-demo-fault-" not in rules, "Owned parent fault remains in live iptables")
        self.step("faults_restored_verified", "journal-and-live-pod-network-namespaces")

    def quiesce(self) -> None:
        for expected in self.owned_deployments:
            metadata = expected["metadata"]
            namespace, name = metadata["namespace"], metadata["name"]
            current = self.get("management", "deployment", name, namespace)
            require(
                current and current["metadata"]["uid"] == metadata["uid"]
                and current["metadata"]["labels"] == metadata["labels"],
                "Management Deployment changed since preflight",
            )
            patch = [
                {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
                {"op": "test", "path": "/metadata/labels", "value": metadata["labels"]},
                {"op": "replace", "path": "/spec/replicas", "value": 0},
            ]
            self.step("quiesce_requested", f"{namespace}/{name}", uid=metadata["uid"])
            self.commands.run(self.kube(
                "management", "-n", namespace, "patch", "deployment", name,
                "--type=json", "-p", json.dumps(patch),
            ))
            deadline = time.monotonic() + 180
            while items(self.commands.json(self.kube(
                "management", "-n", namespace, "get", "pods", "-l",
                f"radapp.io/application=management,radapp.io/resource={name}", "-o", "json",
            ))):
                require(time.monotonic() < deadline, "Management workloads did not terminate")
                time.sleep(2)
            self.step("quiesced", f"{namespace}/{name}")

    def delete_app(self, slot: str, name: str | None = None) -> None:
        target = self.targets[slot]
        self.namespace(slot, "kube-system", target["clusterUid"])
        self.namespace(slot, target["namespace"], target["namespaceUid"])
        name = name or self.inventory[slot]["app"]
        require(
            [app for app in self.radius_list(slot, "app", "list") if app["name"] == name]
            == [app for app in self.inventory[slot]["apps"] if app["name"] == name],
            "Radius application owner changed since preflight",
        )
        self.step("radius_application_delete_requested", f"{slot}/{name}")
        self.rad(slot, "app", "delete", name, "--group", GROUP, "--yes")
        require(
            not any(app["name"] == name for app in self.radius_list(slot, "app", "list")),
            f"{slot}: Radius application remains",
        )
        remaining = self.radius_list(slot, "resource", "list", "--application", name)
        require(not remaining, f"{slot}: Radius resources remain after application deletion")
        self.step("radius_application_absent", f"{slot}/{name}")

    def cluster_absent(self, slot: str) -> bool:
        resources = self.radius_list("management", "resource", "list")
        if any(item["id"] == cluster_resource(slot) for item in resources):
            return False
        target = self.targets[slot]
        if slot in self.docker_nodes() or target["nodeId"] in self.current_ids:
            return False
        return not self.get(
            "management", "secret", target["terraformState"]["secret"], "radius-system",
        ) and not self.get("management", "secret", target["accessSecret"]["name"], NAMESPACE)

    def destroy(self) -> None:
        self.quiesce()
        for slot in CHILDREN:
            self.delete_app(slot)
        for slot in CHILDREN:
            require(
                self.docker_nodes()[slot]["Id"] == self.targets[slot]["nodeId"],
                "Child Docker identity changed before Radius deletion",
            )
            self.namespace(slot, "kube-system", self.targets[slot]["clusterUid"])
            self.state_owner(slot)
            current = self.radius_list("management", "resource", "list")
            expected = self.inventory["management"]["clusters"][slot]
            require(
                [item for item in current if item["id"] == cluster_resource(slot)] == [expected],
                "Management Radius cluster owner changed before deletion",
            )
            self.step("radius_cluster_delete_requested", cluster_resource(slot))
            self.native_delete(slot)
            deadline = time.monotonic() + 600
            while not self.cluster_absent(slot):
                require(time.monotonic() < deadline, f"{slot}: Radius deletion is incomplete")
                time.sleep(3)
            self.step("radius_cluster_owners_absent", slot)
            self.delete_app("management", f"cluster-{slot}")
        self.delete_app("management")
        require(
            not self.radius_list("management", "resource", "list"),
            "Management Radius resources remain; bootstrap deletion refused",
        )
        require(
            not items(self.commands.json(self.kube(
                "management", "-n", "radius-system", "get", "secrets",
                "-l", "tfstate=true", "-o", "json",
            ))) and not items(self.commands.json(self.kube(
                "management", "-n", NAMESPACE, "get", "secrets", "-o", "json",
            ))),
            "Terraform or access Secret owners remain; management deletion refused",
        )
        nodes = self.docker_nodes()
        require(
            set(nodes) == {"management"}
            and nodes["management"]["Id"] == self.targets["management"]["nodeId"],
            "Management bootstrap identity changed or children remain",
        )
        self.namespace("management", "kube-system", self.targets["management"]["clusterUid"])
        self.step("management_kind_delete_requested", nodes["management"]["Id"])
        self.commands.run([
            "kind", "delete", "cluster", "--name", MANAGEMENT,
            "--kubeconfig", str(protected(self.targets["management"]["kubeconfig"])),
        ], timeout=300)
        self.verify_absent(self.record)
        self.step("all_project_containers_absent", "five-clusters")

    def verify_absent(self, record: dict) -> None:
        require(
            set(record["targets"]) == set(SLOTS)
            and all(
                target["clusterId"] == f"kind://radplanes-local-{slot}"
                and DOCKER_ID.fullmatch(target["nodeId"])
                for slot, target in record["targets"].items()
            )
            and all(DOCKER_ID.fullmatch(value) for value in record["unrelatedContainerIds"]),
            "Cleanup record has invalid Docker or allocation identities",
        )
        require(not self.docker_nodes(), "Owned project Docker containers remain")
        require(
            set(record["unrelatedContainerIds"]).issubset(self.current_ids),
            "An original unrelated Docker container disappeared; inspect, do not repair",
        )
        expected = {target["nodeId"] for target in record["targets"].values()}
        require(not expected & self.current_ids, "An original project Docker ID remains")
        self.record["additionalUnrelatedContainerIds"] = sorted(
            self.current_ids - set(record["unrelatedContainerIds"])
        )


def load_inputs() -> tuple[dict, dict]:
    from plane_demo.management.providers.local_config import LocalConfig

    config = LocalConfig.from_dict(read_json("provisioning.json"))
    export = read_json("acceptance.json")
    require(
        export.get("version") == 1 and export.get("environment") == "local"
        and export.get("project") == "radplanes" and set(export["targets"]) == set(SLOTS),
        "A complete local acceptance export is required",
    )
    targets = {}
    for slot, value in export["targets"].items():
        identity = value["local"]
        targets[slot] = {
            "clusterId": value["cluster_id"], "clusterUid": value["cluster_uid"],
            "namespaceUid": value["namespace_uid"], "namespace": value["namespace"],
            "nodeId": identity["node"]["id"], "nodeAddress": identity["node"]["address"],
            "context": value["context"], "workspace": value["context"], "group": GROUP,
            "kubeconfig": "home/.kube/config" if slot == "management" else value["kubeconfig"],
            "radiusConfig": "cleanup-radius.yaml",
        }
        if slot != "management":
            targets[slot]["accessSecret"] = {
                "name": identity["access_secret"], "uid": identity["access_secret_uid"],
                "namespace": NAMESPACE,
            }
    bootstrap = read_json("management-created.json")
    _, management_access, _ = selected_access(targets["management"], "management")
    archive = protected(export["targets"]["management"]["kubeconfig"])
    require(
        archive == STATE / "management.kubeconfig"
        and yaml.safe_load(archive.read_text())
        == yaml.safe_load(protected("home/.kube/config").read_text()),
        "Retain the matching exported management kubeconfig before cleanup",
    )
    require(
        bootstrap.get("name") == MANAGEMENT and bootstrap.get("context") == MANAGEMENT
        and bootstrap.get("secretEncryptionVerified") is True
        and bootstrap["nodeId"] == targets["management"]["nodeId"]
        and bootstrap["nodeAddress"] == config.management_cluster["nodeAddress"]
        and config.management_cluster["uid"] == targets["management"]["clusterUid"],
        "Export/configuration do not match the encrypted management bootstrap",
    )
    require(
        digest(base64.b64decode(management_access["certificate-authority-data"], validate=True))
        == config.management_cluster["caSHA256"],
        "Management CA differs from the protected provisioning configuration",
    )
    images = read_json("runtime-images.json")
    require(
        images.get("content_verified") is True
        and images == export["local_images"]
        and re.fullmatch(r"[a-f0-9]{40}", images["source_revision"]),
        "Export must identify the content-inspected runtime source",
    )
    for role in ("api", "provisioner"):
        require(
            images[role]["reference"] == config.images[role]
            and images[role]["image_id"] == config.image_ids[role]
            and config.images[role].endswith(":" + images["source_revision"])
            and images[role].get("source_hashes"),
            "Runtime image/configuration provenance differs",
        )
        for relative, expected in images[role]["source_hashes"].items():
            if relative == "operations/local/cleanup.py":
                continue
            path = ROOT / relative
            require(
                not Path(relative).is_absolute() and ".." not in path.parts
                and path.is_file() and not path.is_symlink()
                and digest(path.read_bytes()) == expected,
                "Inspected runtime source differs from current project files",
            )
    radius = {"workspaces": {"default": MANAGEMENT, "items": {
        f"radplanes-local-{slot}": {
            "connection": {"kind": "kubernetes", "context": f"radplanes-local-{slot}"},
            "scope": SCOPE.rsplit("/providers", 1)[0],
        }
        for slot in SLOTS
    }}}
    path = STATE / "cleanup-radius.yaml"
    if path.exists() or path.is_symlink():
        require(
            yaml.safe_load(protected(path).read_text()) == radius, "Foreign cleanup Radius config",
        )
    else:
        write_private(path, yaml.safe_dump(radius))
    return targets, {
        "source_revision": images["source_revision"],
        "cleanupSourceSHA256": digest(Path(__file__).read_bytes()),
        "configSHA256": digest(protected("provisioning.json").read_bytes()),
        "exportSHA256": digest(protected("acceptance.json").read_bytes()),
        "bootstrapNodeId": bootstrap["nodeId"],
        "images": {
            role: {"reference": config.images[role], "imageId": config.image_ids[role]}
            for role in ("api", "provisioner")
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Destroy all local demo data")
    mode.add_argument("--verify", metavar="RECORD", help="Read-only cleanup record verification")
    args = parser.parse_args(argv)
    cleanup = None
    try:
        commands = Commands()
        commands.deadline = time.monotonic() + 2700
        if args.verify:
            record = read_json(args.verify)
            require(
                record.get("version") == 1 and record.get("scope") == "cleanup"
                and set(record["targets"]) == set(SLOTS)
                and set(
                    step["identity"] for step in record["steps"]
                    if step["action"] == "radius_cluster_owners_absent"
                ) == set(CHILDREN)
                and any(
                    step["action"] == "radius_application_absent"
                    and step["identity"] == "management/management" for step in record["steps"]
                ),
                "Verification requires retained proof of each child Radius/state/access deletion",
            )
            cleanup = Cleanup(commands, {}, False)
            cleanup.verify_absent(record)
            print(json.dumps({
                "scope": "cleanup", "result": "resources_removed", "verifiedAt": stamp(),
            }))
            return 0
        targets, provenance = load_inputs()
        cleanup = Cleanup(commands, targets, args.execute)
        cleanup.record.update(provenance)
        cleanup.preflight()
        if not args.execute:
            print(json.dumps({
                "scope": "cleanup", "result": "preview", "destroysDemoData": True,
                "order": ["quiesce-management", *CHILDREN, "management"],
            }))
            return 0
        cleanup.destroy()
        cleanup.record.update(result="resources_removed", completedAt=stamp())
        write_private(cleanup.path, cleanup.record)
        print(json.dumps({
            "scope": "cleanup", "result": "resources_removed", "record": str(cleanup.path),
        }))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        message = (
            str(error) if isinstance(error, LocalError) else "Cleanup failed; inspect private state"
        )
        if cleanup and args.execute:
            cleanup.record.update(result="failed", error=message, completedAt=stamp())
            write_private(cleanup.path, cleanup.record)
        print(message + "; no automatic recovery or direct child deletion", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
