#!/usr/bin/env python3
"""Preview .env-selected local teardown; --execute destroys data through verified live owners."""

from __future__ import annotations

import argparse
import base64
import gzip
import importlib.util
import io
import json
import re
import ssl
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

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

from plane_demo.management.providers.local_config import same_radius_id

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


def terraform_envelope(state):
    require(
        isinstance(state, dict)
        and isinstance(state.get("lineage"), str)
        and state["lineage"]
        and type(state.get("serial")) is int
        and state["serial"] > 0
        and state.get("terraform_version") == "1.15.8"
        and isinstance(state.get("resources"), list)
        and all(
            isinstance(item, dict) and item.get("mode") in {"managed", "data"}
            for item in state["resources"]
        ),
        "Terraform state is incomplete or uses an unexpected version",
    )
    return {"lineage": state["lineage"], "serial": state["serial"]}


def decode_terraform_payload(encoded):
    try:
        compressed = base64.b64decode(encoded, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(4_000_001)
        require(len(raw) <= 4_000_000, "Terraform payload exceeds the cleanup limit")
        state = json.loads(raw)
    except (OSError, EOFError, UnicodeError, ValueError):
        raise LocalError("Terraform state payload is invalid") from None
    return state, {**terraform_envelope(state), "sha256": digest(raw)}


def managed_owners(state, expected, *, strict=True):
    terraform_envelope(state)
    managed = [entry for entry in state["resources"] if entry["mode"] == "managed"]
    require(
        len(managed) == len(expected)
        and {(entry.get("type"), entry.get("name")) for entry in managed} == set(expected),
        "Terraform contains missing, extra, or foreign managed resources",
    )
    result = {}
    for entry in managed:
        require(entry.get("module") == "module.default", "Terraform resource module differs")
        instances = entry.get("instances")
        require(isinstance(instances, list) and len(instances) == 1, "Partial Terraform ownership")
        instance = instances[0]
        require(
            not instance.get("deposed") and not instance.get("status"),
            "Terraform instance is tainted or deposed",
        )
        if strict:
            provider = (
                "terraform.io/builtin/terraform"
                if entry["type"] == "terraform_data"
                else "registry.terraform.io/tehcyx/kind"
                if entry["type"] == "kind_cluster"
                else "registry.terraform.io/hashicorp/random"
                if entry["type"] == "random_password"
                else "registry.terraform.io/hashicorp/kubernetes"
            )
            reference = f'provider["{provider}"]'
            require(
                entry.get("provider") in {reference, "module.default." + reference},
                "Terraform resource provider differs",
            )
            require(instance.get("index_key") is None, "Unexpected counted Terraform owner")
        require(
            isinstance(instance.get("attributes"), dict), "Terraform resource attributes missing"
        )
        result[entry["type"], entry["name"]] = (entry, instance)
    return result


def validate_cluster_payload(
    state, cluster_name, access_namespace, images, *, counted=False, strict=True
):
    owners = managed_owners(
        state,
        {
            ("kind_cluster", "child"),
            ("kubernetes_secret_v1", "access"),
            ("terraform_data", "images"),
        },
        strict=strict,
    )
    attrs = owners["kind_cluster", "child"][1]["attributes"]
    require(
        attrs.get("name") == cluster_name
        and attrs.get("id") == f"{cluster_name}-{NODE_IMAGE}"
        and attrs.get("node_image") == NODE_IMAGE
        and attrs.get("completed") is True
        and isinstance(attrs.get("kubeconfig"), str)
        and attrs["kubeconfig"]
        and isinstance(attrs.get("client_key"), str)
        and attrs["client_key"],
        "Terraform does not describe the completed, owned child",
    )
    image_load, instance = owners["terraform_data", "images"]
    image_attrs = instance["attributes"]
    image_type = "string" if counted else ["object", {"reference": "string", "image_id": "string"}]
    types = [
        ["object", {"cluster_id": "string", "images": ["list", image_type]}],
        ["object", {"cluster_id": "string", "images": ["tuple", [image_type] * len(images)]}],
    ]
    trigger = image_attrs.get("triggers_replace", {})
    require(
        image_load.get("provider") == 'provider["terraform.io/builtin/terraform"]'
        and instance.get("schema_version") == 0
        and (
            (type(instance.get("index_key")) is int and instance["index_key"] == 0)
            if counted
            else instance.get("index_key") is None
        )
        and image_attrs.get("input") is None
        and image_attrs.get("output") is None
        and set(trigger) == {"type", "value"}
        and trigger["type"] in types
        and trigger["value"] == {"cluster_id": attrs["id"], "images": images},
        "Terraform image-load owner or its inspected image references changed",
    )
    access_attrs = owners["kubernetes_secret_v1", "access"][1]["attributes"]
    metadata = access_attrs.get("metadata", [])
    require(
        len(metadata) == 1
        and metadata[0].get("name") == cluster_name + "-access"
        and metadata[0].get("namespace") == access_namespace
        and (
            not strict
            or access_attrs.get("id") == access_namespace + "/" + cluster_name + "-access"
        ),
        "Terraform access reference differs",
    )
    return attrs


APPLICATION_STATE_OWNERS = {
    "Demo.Platform/postgreSqlDatabases": {
        ("random_password", "server"): None,
        ("kubernetes_secret_v1", "server"): "postgres-credentials",
        ("kubernetes_secret_v1", "setup"): "postgres-setup",
        ("kubernetes_persistent_volume_claim_v1", "data"): "postgres-data",
        ("kubernetes_service_v1", "postgres"): "postgres",
        ("kubernetes_stateful_set_v1", "postgres"): "postgres",
    },
    "Applications.Datastores/redisCaches": {
        ("random_password", "server"): None,
        ("kubernetes_secret_v1", "server"): "redis-credentials",
        ("kubernetes_persistent_volume_claim_v1", "data"): "redis-data",
        ("kubernetes_service_v1", "redis"): "redis",
        ("kubernetes_stateful_set_v1", "redis"): "redis",
    },
    "Demo.Platform/gateways": {
        ("kubernetes_service_v1", "backend"): "gateway-api",
        ("kubernetes_config_map_v1", "envoy"): "gateway-envoy",
        ("kubernetes_deployment_v1", "envoy"): "gateway",
        ("kubernetes_service_v1", "gateway"): "gateway",
    },
}


def validate_application_payload(state, resource_type, namespace):
    expected = APPLICATION_STATE_OWNERS[resource_type]
    owners = managed_owners(state, expected)
    for key, name in expected.items():
        if name is None:
            continue
        attributes = owners[key][1]["attributes"]
        metadata = attributes.get("metadata", [])
        require(
            len(metadata) == 1
            and metadata[0].get("name") == name
            and metadata[0].get("namespace") == namespace
            and attributes.get("id") == namespace + "/" + name,
            "Terraform application resource targets an unowned Kubernetes object",
        )


def fault_support():
    spec = importlib.util.spec_from_file_location(
        "plane_demo_cleanup_faults",
        ROOT / "scripts/harness/local/fault-parent-link.py",
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
        value.get("apiVersion") == "v1"
        and value.get("kind") == "Config"
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
        self.removed_resource_ids: dict[str, set[str]] = {}

    def step(self, action: str, identity: str, **proof) -> None:
        self.record["steps"].append(
            {
                "at": stamp(),
                "action": action,
                "identity": identity,
                **proof,
            }
        )
        if self.execute:
            write_private(self.path, self.record)

    def kube(self, slot: str, *args: str) -> list[str]:
        target = self.targets[slot]
        return [
            "kubectl",
            "--kubeconfig",
            str(protected(target["kubeconfig"])),
            "--context",
            target["context"],
            "--request-timeout=30s",
            *args,
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

    @contextmanager
    def native_client(self, slot: str):
        require(slot in self.targets, "Unknown native Radius target")
        _, cluster, user = selected_access(self.targets[slot], slot)
        directory = private_dir(STATE / "client" / f"cleanup-{self.record['runId']}-{slot}")
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
            verify=context,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(60, connect=5),
        ) as client:
            yield client, cluster["server"]

    def native_delete(self, slot: str) -> None:
        with self.native_client("management") as (client, server):
            response = client.delete(
                server + "/apis/api.ucp.dev/v1alpha3" + cluster_resource(slot),
                params={"api-version": API_VERSION},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        require(response.status_code in {200, 202, 204}, "Radius cluster DELETE was not accepted")

    def native_resources(self, slot: str) -> list[dict]:
        with self.native_client(slot) as (client, server):
            response = client.get(
                server
                + "/apis/api.ucp.dev/v1alpha3"
                + SCOPE.rsplit("/providers", 1)[0]
                + "/resources",
                params={"api-version": "2023-10-01-preview"},
                headers={"Accept": "application/json"},
                extensions=(
                    {} if slot == "management" else {"sni_hostname": f"radplanes-local-{slot}"}
                ),
            )
        require(response.status_code == 200, "Native Radius inventory failed")
        body = response.json()
        require(
            isinstance(body, dict) and not body.get("nextLink") and not body.get("@odata.nextLink"),
            "Incomplete native Radius inventory",
        )
        result, identifiers = [], set()
        for value in items(body):
            require(
                isinstance(value, dict)
                and isinstance(value.get("id"), str)
                and isinstance(value.get("type"), str)
                and isinstance(value.get("name"), str)
                and same_radius_id(value["id"], f"{SCOPE}/{value['type']}/{value['name']}")
                and value["id"].casefold() not in identifiers,
                "Foreign or duplicate native Radius resource",
            )
            identifiers.add(value["id"].casefold())
            # These are definitions or retained ARM deployment history, not workload owners.
            if value["type"].casefold() not in {
                "applications.core/applications",
                "applications.core/environments",
                "microsoft.resources/deployments",
            }:
                result.append(value)
        return result

    def verify_complete_inventory(self, slot: str) -> None:
        require(
            {r["id"].casefold() for r in self.native_resources(slot)}
            == {r["id"].casefold() for r in self.inventory[slot]["resources"]},
            f"{slot}: unreviewed Radius resources exist outside application inventory",
        )

    def app_resources_absent(self, slot: str, name: str) -> None:
        application = f"{SCOPE}/Applications.Core/applications/{name}"
        resources = [
            resource
            for resource in self.inventory[slot]["resources"]
            if same_radius_id(resource["properties"].get("application"), application)
        ]
        identifiers = self.removed_resource_ids.get(slot, set()) | {
            resource["id"].casefold() for resource in resources
        }
        allowed = {r["id"].casefold() for r in self.inventory[slot]["resources"]} - identifiers
        require(
            {r["id"].casefold() for r in self.native_resources(slot)} <= allowed,
            f"{slot}: Radius resources remain after application deletion",
        )
        self.removed_resource_ids[slot] = identifiers

    def child_empty(self, slot: str) -> None:
        require(slot in CHILDREN, "Only child clusters use this absence guard")
        self.namespace(slot, "kube-system", self.targets[slot]["clusterUid"])
        require(
            not self.radius_list(slot, "app", "list")
            and not self.native_resources(slot)
            and not items(
                self.commands.json(
                    self.kube(
                        slot,
                        "-n",
                        "radius-system",
                        "get",
                        "secrets",
                        "-l",
                        "tfstate=true",
                        "-o",
                        "json",
                    )
                )
            ),
            f"{slot}: child Radius resources or Terraform state remain",
        )

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
            metadata["name"] == name
            and metadata.get("uid")
            and metadata["namespace"] == "radius-system"
            and labels.get("tfstate") == "true"
            and labels.get("app.kubernetes.io/managed-by") == "terraform",
            "Foreign Terraform state owner",
        )
        state, _ = decode_terraform_payload(stored["data"]["tfstate"])
        summary = {
            "secret": name,
            "uid": metadata["uid"],
            "lineage": state["lineage"],
            "serial": state["serial"],
        }
        require(
            "terraformState" not in target or target["terraformState"] == summary,
            f"{slot}: Terraform owner changed since preflight",
        )
        name = f"radplanes-local-{slot}"
        attrs = validate_cluster_payload(
            state,
            name,
            NAMESPACE,
            [self.record["images"][role]["reference"] for role in ("api", "provisioner")],
            counted=True,
            strict=False,
        )
        access = target["accessSecret"]
        require(
            access["name"] == f"{name}-access" and access["namespace"] == NAMESPACE,
            "Foreign access Secret",
        )
        live = self.get("management", "secret", access["name"], NAMESPACE)
        require(
            live
            and live["metadata"]["uid"] == access["uid"]
            and live["metadata"].get("labels", {}).get("radplanes.local/slot") == slot
            and same_radius_id(
                live["metadata"].get("annotations", {}).get("radplanes.local/radius-resource"),
                resource_id,
            ),
            f"{slot}: access Secret ownership changed",
        )
        stored_access = yaml.safe_load(base64.b64decode(live["data"]["kubeconfig"], validate=True))
        require(
            len(stored_access["contexts"])
            == len(stored_access["clusters"])
            == len(stored_access["users"])
            == 1
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
            and same_radius_id(workspace.get("scope"), scope),
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
                same_radius_id(app["id"], f"{SCOPE}/Applications.Core/applications/{name}")
                and same_radius_id(
                    app["properties"]["environment"],
                    f"{SCOPE}/Applications.Core/environments/{environment}",
                ),
                f"{slot}: foreign application/environment",
            )
        app_id = f"{SCOPE}/Applications.Core/applications/{role}"
        env_id = f"{SCOPE}/Applications.Core/environments/{slot}"
        environment = json.loads(
            self.rad(
                slot,
                "env",
                "show",
                slot,
                "--group",
                GROUP,
                "-o",
                "json",
            )
        )
        require(
            same_radius_id(environment["id"], env_id)
            and environment["properties"]["compute"]["namespace"] == f"radplanes-local-{slot}",
            f"{slot}: foreign Radius compute namespace",
        )
        resources = [
            resource
            for app in apps
            for resource in self.radius_list(slot, "resource", "list", "--application", app["name"])
        ]
        require(
            all(isinstance(resource.get("id"), str) for resource in resources)
            and len({resource["id"].casefold() for resource in resources}) == len(resources),
            f"{slot}: duplicate or malformed Radius resource inventory",
        )
        cluster_records = {}
        for resource in resources:
            kind, name, properties = resource["type"], resource["name"], resource["properties"]
            require(
                re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name)
                and same_radius_id(resource["id"], f"{SCOPE}/{kind}/{name}")
                and properties.get("provisioningState") in TERMINAL,
                f"{slot}: partial or foreign Radius resource",
            )
            if kind == "Demo.Platform/clusters":
                child = properties.get("slot")
                require(
                    slot == "management"
                    and child in CHILDREN
                    and name == child
                    and child not in cluster_records
                    and same_radius_id(
                        properties.get("environment"),
                        f"{SCOPE}/Applications.Core/environments/provision-{child}",
                    )
                    and same_radius_id(
                        properties.get("application"),
                        f"{SCOPE}/Applications.Core/applications/cluster-{child}",
                    )
                    and properties.get("clusterId") == f"kind://radplanes-local-{child}"
                    and properties.get("clusterName") == f"radplanes-local-{child}"
                    and properties.get("bootstrapAccessRef")
                    == f"kubernetes://{NAMESPACE}/radplanes-local-{child}-access#kubeconfig",
                    "Foreign or incomplete management Radius child owner",
                )
                cluster_records[child] = resource
            else:
                require(
                    kind
                    in {
                        "Applications.Core/containers",
                        "Applications.Datastores/redisCaches",
                        "Demo.Platform/postgreSqlDatabases",
                        "Demo.Platform/gateways",
                    }
                    and same_radius_id(properties.get("application"), app_id)
                    and (
                        same_radius_id(properties.get("environment"), env_id)
                        or (
                            kind == "Applications.Core/containers"
                            and properties.get("environment") is None
                        )
                    ),
                    f"{slot}: unknown or foreign Radius application resource",
                )
        if slot == "management":
            require(set(cluster_records) == set(CHILDREN), "Every child requires its Radius owner")
        return {"app": role, "apps": apps, "resources": resources, "clusters": cluster_records}

    def deployments(self) -> list[dict]:
        namespace = self.targets["management"]["namespace"]
        deployments = items(
            self.commands.json(
                self.kube(
                    "management",
                    "-n",
                    namespace,
                    "get",
                    "deployments",
                    "-l",
                    "radapp.io/application=management",
                    "-o",
                    "json",
                )
            )
        )
        result = []
        for component in ("management-api", "provisioner"):
            matches = [
                item
                for item in deployments
                if item["metadata"].get("labels", {}).get("radapp.io/resource") == component
            ]
            require(len(matches) == 1, f"Expected one management {component} Deployment")
            deployment = matches[0]
            metadata = deployment["metadata"]
            labels = metadata.get("labels", {})
            require(
                metadata["name"] == component
                and metadata["namespace"] == namespace
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
            self.verify_complete_inventory(slot)
            self.verify_state_inventory(slot)
            self.record["targets"][slot] = {
                key: target[key]
                for key in (
                    "clusterId",
                    "clusterUid",
                    "context",
                    "namespace",
                    "namespaceUid",
                    "nodeId",
                )
            }
            if slot != "management":
                self.state_owner(slot)
                self.record["targets"][slot].update(
                    terraformState=target["terraformState"],
                    accessSecret=target["accessSecret"],
                )
        accesses = items(
            self.commands.json(
                self.kube(
                    "management",
                    "-n",
                    NAMESPACE,
                    "get",
                    "secrets",
                    "-o",
                    "json",
                )
            )
        )
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
                len(actual) == 1
                and actual[0]["Id"] == image["imageId"]
                and [item["image"] for item in deployment["spec"]["template"]["spec"]["containers"]]
                == [image["reference"]],
                "Management runtime image differs from inspected/exported provenance",
            )
        self.record["deployments"] = [
            {
                "name": item["metadata"]["name"],
                "uid": item["metadata"]["uid"],
                "namespace": item["metadata"]["namespace"],
                "labels": item["metadata"]["labels"],
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
        states = items(
            self.commands.json(
                self.kube(
                    slot,
                    "-n",
                    "radius-system",
                    "get",
                    "secrets",
                    "-l",
                    "tfstate=true",
                    "-o",
                    "json",
                )
            )
        )
        require(
            len(states) == len(expected)
            and {value["metadata"]["name"] for value in states} == set(expected),
            f"{slot}: missing, extra, or chunked Terraform owner state",
        )
        self.record.setdefault("stateOwners", {})[slot] = [
            {"name": value["metadata"]["name"], "uid": value["metadata"]["uid"]} for value in states
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
                record.get("environment") == "local"
                and record.get("project") == "radplanes"
                and record.get("component") == slot.rsplit("-", 1)[1] + "-reconciler"
                and record.get("cluster_uid") == self.targets[slot]["clusterUid"]
                and record.get("namespace_uid") == self.targets[slot]["namespaceUid"],
                "Fault journal targets a different live cluster",
            )
            if record.get("creation_attempted"):
                require(
                    record.get("restored") is True
                    and record.get("physical_restored") is True
                    and record.get("original_rules_sha256") == record.get("restored_rules_sha256")
                    and DOCKER_ID.fullmatch(record.get("original_rules_sha256", ""))
                    and record.get("restoration_probe", {}).get("ok") is True,
                    "Restore the recorded parent fault before cleanup",
                )
                attempted.append(record)
        for slot in CHILDREN:
            component = slot.rsplit("-", 1)[1] + "-reconciler"
            fault = support.LocalParentFault(
                configuration,
                slot,
                component,
                self.path,
                operator=run,
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
                current
                and current["metadata"]["uid"] == metadata["uid"]
                and current["metadata"]["labels"] == metadata["labels"],
                "Management Deployment changed since preflight",
            )
            patch = [
                {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
                {"op": "test", "path": "/metadata/labels", "value": metadata["labels"]},
                {"op": "replace", "path": "/spec/replicas", "value": 0},
            ]
            self.step("quiesce_requested", f"{namespace}/{name}", uid=metadata["uid"])
            self.commands.run(
                self.kube(
                    "management",
                    "-n",
                    namespace,
                    "patch",
                    "deployment",
                    name,
                    "--type=json",
                    "-p",
                    json.dumps(patch),
                )
            )
            deadline = time.monotonic() + 180
            while items(
                self.commands.json(
                    self.kube(
                        "management",
                        "-n",
                        namespace,
                        "get",
                        "pods",
                        "-l",
                        f"radapp.io/application=management,radapp.io/resource={name}",
                        "-o",
                        "json",
                    )
                )
            ):
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
        self.app_resources_absent(slot, name)
        self.step("radius_application_absent", f"{slot}/{name}")

    def cluster_absent(self, slot: str) -> bool:
        resources = self.radius_list(
            "management", "resource", "list", "--application", f"cluster-{slot}"
        )
        if any(same_radius_id(item["id"], cluster_resource(slot)) for item in resources):
            return False
        target = self.targets[slot]
        if slot in self.docker_nodes() or target["nodeId"] in self.current_ids:
            return False
        return not self.get(
            "management",
            "secret",
            target["terraformState"]["secret"],
            "radius-system",
        ) and not self.get("management", "secret", target["accessSecret"]["name"], NAMESPACE)

    def destroy(self) -> None:
        self.quiesce()
        for slot in CHILDREN:
            self.delete_app(slot)
        for slot in CHILDREN:
            self.child_empty(slot)
            require(
                self.docker_nodes()[slot]["Id"] == self.targets[slot]["nodeId"],
                "Child Docker identity changed before Radius deletion",
            )
            self.namespace(slot, "kube-system", self.targets[slot]["clusterUid"])
            self.state_owner(slot)
            current = self.radius_list(
                "management", "resource", "list", "--application", f"cluster-{slot}"
            )
            expected = self.inventory["management"]["clusters"][slot]
            require(
                [item for item in current if same_radius_id(item["id"], cluster_resource(slot))]
                == [expected],
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
            not self.radius_list("management", "app", "list")
            and not self.native_resources("management"),
            "Management Radius resources remain; bootstrap deletion refused",
        )
        require(
            not items(
                self.commands.json(
                    self.kube(
                        "management",
                        "-n",
                        "radius-system",
                        "get",
                        "secrets",
                        "-l",
                        "tfstate=true",
                        "-o",
                        "json",
                    )
                )
            )
            and not items(
                self.commands.json(
                    self.kube(
                        "management",
                        "-n",
                        NAMESPACE,
                        "get",
                        "secrets",
                        "-o",
                        "json",
                    )
                )
            ),
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
        self.commands.run(
            [
                "kind",
                "delete",
                "cluster",
                "--name",
                MANAGEMENT,
                "--kubeconfig",
                str(protected(self.targets["management"]["kubeconfig"])),
            ],
            timeout=300,
        )
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
        export.get("version") == 1
        and export.get("environment") == "local"
        and export.get("project") == "radplanes"
        and set(export["targets"]) == set(SLOTS),
        "A complete local acceptance export is required",
    )
    targets = {}
    for slot, value in export["targets"].items():
        identity = value["local"]
        targets[slot] = {
            "clusterId": value["cluster_id"],
            "clusterUid": value["cluster_uid"],
            "namespaceUid": value["namespace_uid"],
            "namespace": value["namespace"],
            "nodeId": identity["node"]["id"],
            "nodeAddress": identity["node"]["address"],
            "context": value["context"],
            "workspace": value["context"],
            "group": GROUP,
            "kubeconfig": "home/.kube/config" if slot == "management" else value["kubeconfig"],
            "radiusConfig": "cleanup-radius.yaml",
        }
        if slot != "management":
            targets[slot]["accessSecret"] = {
                "name": identity["access_secret"],
                "uid": identity["access_secret_uid"],
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
        bootstrap.get("name") == MANAGEMENT
        and bootstrap.get("context") == MANAGEMENT
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
            if relative == "scripts/operations/local/cleanup.py":
                continue
            path = ROOT / relative
            require(
                not Path(relative).is_absolute()
                and ".." not in path.parts
                and path.is_file()
                and not path.is_symlink()
                and digest(path.read_bytes()) == expected,
                "Inspected runtime source differs from current project files",
            )
    radius = {
        "workspaces": {
            "default": MANAGEMENT,
            "items": {
                f"radplanes-local-{slot}": {
                    "connection": {"kind": "kubernetes", "context": f"radplanes-local-{slot}"},
                    "scope": SCOPE.rsplit("/providers", 1)[0],
                }
                for slot in SLOTS
            },
        }
    }
    path = STATE / "cleanup-radius.yaml"
    if path.exists() or path.is_symlink():
        require(
            yaml.safe_load(protected(path).read_text()) == radius,
            "Foreign cleanup Radius config",
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


def live_support():
    name = "plane_demo_live_cleanup"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).parents[1] / "clean-azure.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class LiveLocalCleanup(live_support().LiveClusterCleanup):
    def __init__(self, **kwargs):
        super().__init__(environment="local", **kwargs)
        self.host = None
        self.nodes, self.states, self.access = {}, {}, {}
        self.state_proofs = {}
        self.initial_unrelated = None

    def check_bootstrap_lease(self):
        namespace = self.config.namespace("management")
        raw = self.kube(
            "management",
            "get",
            "leases.coordination.k8s.io",
            "management-bootstrap",
            "--ignore-not-found",
            "-o",
            "json",
            namespace=namespace,
        )
        if not raw.strip():
            return
        metadata = json.loads(raw).get("metadata", {})
        require(
            metadata.get("name") == "management-bootstrap"
            and metadata.get("namespace") == namespace,
            "Unexpected management bootstrap Lease response; cleanup refused",
        )
        raise LocalError(
            "Active or interrupted management bootstrap Lease exists; "
            "release it through the deployment owner before cleanup"
        )

    def call(self, argv, *, mutation=False, **kwargs):
        if mutation:
            self.check_bootstrap_lease()
        return super().call(argv, mutation=mutation, **kwargs)

    def docker(self, *args):
        if self.host is None:
            self.host = self.json(
                [
                    "env",
                    "-u",
                    "DOCKER_HOST",
                    "-u",
                    "DOCKER_CONTEXT",
                    "-u",
                    "DOCKER_CONFIG",
                    "docker",
                    "context",
                    "inspect",
                    "desktop-linux",
                    "--format",
                    "{{json .Endpoints.docker.Host}}",
                ]
            )
            require(
                isinstance(self.host, str)
                and re.fullmatch(r"unix:///[^\s?#]+", self.host)
                and ".." not in Path(self.host.removeprefix("unix://")).parts,
                "Docker Desktop must use a local Unix socket",
            )
        return ["docker", "--host", self.host, *args]

    def containers(self):
        ids = self.call(self.docker("ps", "-aq", "--no-trunc")).split()
        require(
            all(DOCKER_ID.fullmatch(value) for value in ids) and len(set(ids)) == len(ids),
            "Docker returned invalid container IDs",
        )
        values = self.json(self.docker("inspect", "--type", "container", *ids)) if ids else []
        require(
            isinstance(values, list) and {value["Id"] for value in values} == set(ids),
            "Incomplete Docker inventory",
        )
        nodes = {}
        for value in values:
            name = value["Name"].removeprefix("/")
            owner = (value["Config"].get("Labels") or {}).get("io.x-k8s.kind.cluster", "")
            if not (
                name.startswith(self.config.stem + "-") or owner.startswith(self.config.stem + "-")
            ):
                continue
            slot = next((slot for slot in SLOTS if owner == self.config.slot_name(slot)), None)
            require(slot is not None and slot not in nodes, "Unknown or duplicate deployment node")
            index = SLOTS.index(slot)
            require(
                name == self.config.slot_name(slot) + "-control-plane"
                and value["Config"].get("Image") == NODE_IMAGE
                and value["HostConfig"]["PortBindings"]
                == {
                    "6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(35495 + index)}],
                    "31480/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(35490 + index)}],
                },
                "Local node identity, image, or reserved ports differ",
            )
            nodes[slot] = value
        return nodes, set(ids) - {value["Id"] for value in nodes.values()}

    def environment_scope(self, slot, app, properties):
        require(not properties.get("providers", {}).get("azure"), "Local Radius references Azure")

    def resource_scope(self, item):
        if isinstance(item, str):
            require(
                not item.lower().startswith("/subscriptions/"), "Local resource references Azure"
            )
        elif isinstance(item, dict):
            for value in item.values():
                self.resource_scope(value)
        elif isinstance(item, list):
            for value in item:
                self.resource_scope(value)

    def cluster_record(self, slot, properties):
        name = self.config.slot_name(slot)
        require(
            (not properties.get("clusterId") or properties["clusterId"] == "kind://" + name)
            and (not properties.get("clusterName") or properties["clusterName"] == name)
            and (
                not properties.get("bootstrapAccessRef")
                or properties["bootstrapAccessRef"]
                == (f"kubernetes://{self.config.stem}-access/{name}-access#kubeconfig")
            ),
            "Radius child points at another kind owner",
        )
        if slot in self.nodes:
            require(
                properties.get("clusterId") == "kind://" + name
                and bool(properties.get("bootstrapAccessRef")),
                "Live child lacks Radius outputs",
            )

    def state_inventory(self, slot):
        output = self.kube(
            slot,
            "get",
            "secrets",
            "-l",
            "tfstate=true",
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.metadata.uid}{"\\t"}'
            '{.metadata.labels.tfstate}{"\\t"}{.metadata.labels.app\\.kubernetes\\.io/managed-by}'
            '{"\\n"}{end}',
            namespace="radius-system",
        )
        result = {}
        for line in output.splitlines():
            fields = line.split("\t")
            require(
                len(fields) == 4
                and re.fullmatch(r"tfstate-default-[a-f0-9]{40}", fields[0])
                and str(UUID(fields[1])) == fields[1]
                and fields[2:] == ["true", "terraform"]
                and fields[0] not in result,
                "Unowned or malformed Terraform backend metadata",
            )
            result[fields[0]] = fields[1]
        return result

    def access_secret(self, slot):
        name = self.config.slot_name(slot) + "-access"
        namespace = self.config.stem + "-access"
        raw = self.kube(
            "management",
            "get",
            "secret",
            name,
            "--ignore-not-found",
            "-o",
            'jsonpath={.metadata.name}{"\\n"}{.metadata.namespace}{"\\n"}{.metadata.uid}{"\\n"}'
            '{.metadata.labels.radplanes\\.local/slot}{"\\n"}'
            "{.metadata.annotations.radplanes\\.local/radius-resource}",
            namespace=namespace,
        )
        if not raw.strip():
            return None
        fields = raw.splitlines()
        require(
            len(fields) == 5
            and fields[:2] == [name, namespace]
            and str(UUID(fields[2])) == fields[2]
            and fields[3] == slot
            and same_radius_id(fields[4], f"{self.scope}/providers/Demo.Platform/clusters/{slot}"),
            "Child access Secret ownership differs",
        )
        return fields[2]

    def access_inventory(self):
        raw = self.kube(
            "management",
            "get",
            "secrets",
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.metadata.uid}{"\\t"}'
            '{.metadata.labels.radplanes\\.local/slot}{"\\t"}'
            '{.metadata.annotations.radplanes\\.local/radius-resource}{"\\n"}{end}',
            namespace=self.config.stem + "-access",
        )
        result = {}
        for line in raw.splitlines():
            fields = line.split("\t")
            require(
                len(fields) == 4
                and fields[2] in CHILDREN
                and fields[0] == self.config.slot_name(fields[2]) + "-access"
                and str(UUID(fields[1])) == fields[1]
                and same_radius_id(
                    fields[3], f"{self.scope}/providers/Demo.Platform/clusters/{fields[2]}"
                )
                and fields[2] not in result,
                "Unrecognized or foreign child access Secret",
            )
            result[fields[2]] = fields[1]
        return result

    def preflight_dependencies(self, clusters, owners):
        for slot, value in self.inventories.items():
            expected = {
                backend_secret_name(item)
                for item in value["resources"]
                if item["type"] in live_support().DEPENDENCIES | {"Demo.Platform/clusters"}
            }
            self.states[slot] = self.state_inventory(slot)
            require(
                set(self.states[slot]) == expected, "Radius and Terraform backend owners differ"
            )
        for slot in owners:
            self.access[slot] = self.access_secret(slot)
            require(
                slot not in clusters or self.access[slot] is not None,
                "Live child has no Radius-owned access Secret",
            )
        require(
            self.access_inventory()
            == {
                slot: identifier
                for slot, identifier in self.access.items()
                if identifier is not None
            },
            "Unrecognized child access Secret inventory",
        )
        for slot in self.inventories:
            self.validate_state_payloads(slot, remember=True)

    def read_state_payload(self, slot, name):
        fields = self.kube(
            slot,
            "get",
            "secret",
            name,
            "-o",
            'jsonpath={.metadata.name}{"\\n"}{.metadata.namespace}{"\\n"}'
            '{.metadata.uid}{"\\n"}{.data.tfstate}',
            namespace="radius-system",
        ).splitlines()
        require(
            len(fields) == 4
            and fields[:2] == [name, "radius-system"]
            and fields[2] == self.states[slot][name],
            "Terraform payload owner changed",
        )
        state, proof = decode_terraform_payload(fields[3])
        return state, {"uid": fields[2], **proof}

    def cluster_images(self, resource):
        properties = resource["properties"]
        name = properties["environment"].rsplit("/", 1)[1]
        environment = self.rad("management", "env", "show", name)
        require(
            same_radius_id(environment["id"], properties["environment"]),
            "Cluster Recipe environment changed",
        )
        binding = environment["properties"]["recipes"]["Demo.Platform/clusters"]["default"]
        parameters = binding["parameters"]
        require(
            binding["templateKind"] == "terraform"
            and parameters["resource_prefix"] == self.config.stem
            and parameters["radius_group"] == self.config.stem
            and parameters["access_namespace"] == self.config.stem + "-access",
            "Cluster Recipe ownership parameters differ",
        )
        runtime, dependencies = parameters["runtime_images"], parameters["dependency_images"]
        require(
            set(runtime) == {"api", "provisioner", "operator"}
            and isinstance(dependencies, list)
            and 1 <= len(dependencies) <= 61,
            "Cluster Recipe image inputs are incomplete",
        )
        images = [runtime[role] for role in ("api", "provisioner", "operator")] + dependencies
        require(
            all(
                isinstance(item, dict)
                and set(item) == {"reference", "image_id"}
                and isinstance(item["reference"], str)
                and item["reference"]
                and re.fullmatch(r"sha256:[a-f0-9]{64}", item["image_id"])
                for item in images
            )
            and len({item["reference"] for item in images}) == len(images)
            and any(item["reference"] == NODE_IMAGE for item in dependencies),
            "Cluster Recipe image identity differs",
        )
        for role, image in runtime.items():
            require(
                re.fullmatch(
                    rf"localhost/{re.escape(self.config.stem)}-{role}:[a-f0-9]{{40}}",
                    image["reference"],
                ),
                "Foreign cluster runtime image",
            )
        return images

    def validate_child_credentials(self, child, attributes):
        if child not in self.targets:
            return
        try:
            selected = yaml.safe_load(self.targets[child]["kubeconfig"].read_text())
            original = yaml.safe_load(attributes["kubeconfig"])
            fields = self.kube(
                "management",
                "get",
                "secret",
                self.config.slot_name(child) + "-access",
                "-o",
                'jsonpath={.metadata.uid}{"\\n"}{.data.kubeconfig}',
                namespace=self.config.stem + "-access",
            ).splitlines()
            require(
                len(fields) == 2 and fields[0] == self.access[child],
                "Child access Secret changed during payload verification",
            )
            tracked = yaml.safe_load(base64.b64decode(fields[1], validate=True))
            for value in (selected, original, tracked):
                require(
                    isinstance(value, dict)
                    and len(value["contexts"])
                    == len(value["clusters"])
                    == len(value["users"])
                    == 1,
                    "Ambiguous Terraform child credentials",
                )
            ca = selected["clusters"][0]["cluster"]["certificate-authority-data"]
            user = selected["users"][0]["user"]
            require(
                original["clusters"][0]["cluster"]["certificate-authority-data"] == ca
                and original["users"][0]["user"] == user
                and attributes["client_key"].encode()
                == base64.b64decode(user["client-key-data"], validate=True)
                and tracked["clusters"][0]["cluster"]["certificate-authority-data"] == ca
                and tracked["users"][0]["user"] == user
                and tracked["current-context"] == self.config.slot_name(child)
                and tracked["clusters"][0]["cluster"].get("tls-server-name")
                == self.config.slot_name(child)
                and tracked["clusters"][0]["cluster"]["server"]
                == "https://"
                + self.nodes[child]["NetworkSettings"]["Networks"]["kind"]["IPAddress"]
                + ":6443",
                "Terraform kind credentials do not match the owned child access",
            )
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, yaml.YAMLError):
            raise LocalError("Terraform child credential proof is invalid") from None

    def validate_state_payloads(self, slot, *, remember=False):
        self.verify_state_uids(slot)
        for resource in self.inventories[slot]["resources"]:
            if resource["type"] not in live_support().DEPENDENCIES | {
                "Demo.Platform/clusters"
            } or resource["id"].lower() in self.removed.get(slot, set()):
                continue
            name = backend_secret_name(resource)
            state, proof = self.read_state_payload(slot, name)
            if resource["type"] == "Demo.Platform/clusters":
                child = resource["properties"]["slot"]
                attrs = validate_cluster_payload(
                    state,
                    self.config.slot_name(child),
                    self.config.stem + "-access",
                    self.cluster_images(resource),
                )
                self.validate_child_credentials(child, attrs)
            else:
                validate_application_payload(state, resource["type"], self.config.namespace(slot))
            key = (slot, name)
            if remember:
                self.state_proofs[key] = proof
            else:
                require(
                    self.state_proofs.get(key) == proof, "Terraform payload changed since preflight"
                )

    def rad(self, slot, *args, mutation=False):
        if mutation:
            require(args[:2] == ("app", "delete"), "Unexpected Radius mutation")
            self.validate_state_payloads(slot)
        return super().rad(slot, *args, mutation=mutation)

    def child_apps_absent(self, slot):
        require(
            not self.rows(self.rad(slot, "app", "list"))
            and not self.native_resources(slot)
            and not self.state_inventory(slot),
            "Child Radius or Terraform owners remain",
        )
        nodes, _ = self.containers()
        require(nodes.get(slot, {}).get("Id") == self.nodes[slot]["Id"], "Child node was replaced")
        self.verify_cluster(slot)

    def verify_state_uids(self, slot):
        expected = {
            backend_secret_name(item): self.states[slot][backend_secret_name(item)]
            for item in self.inventories[slot]["resources"]
            if item["type"] in live_support().DEPENDENCIES | {"Demo.Platform/clusters"}
            and item["id"].lower() not in self.removed.get(slot, set())
        }
        require(self.state_inventory(slot) == expected, "Terraform backend ownership changed")

    def delete_app(self, slot, app):
        self.verify_state_uids(slot)
        super().delete_app(slot, app)
        if self.execute:
            self.verify_state_uids(slot)

    def before_child_delete(self, slot, record):
        super().before_child_delete(slot, record)
        self.validate_state_payloads("management")
        require(self.access_secret(slot) == self.access[slot], "Child access Secret UID changed")
        self.check_bootstrap_lease()

    def child_absent(self, slot, record):
        deadline = self.clock() + 900
        state = backend_secret_name(record)
        while True:
            nodes, _ = self.containers()
            if (
                slot not in nodes
                and state not in self.state_inventory("management")
                and self.access_secret(slot) is None
            ):
                return
            require(self.clock() < deadline, "Radius child, backend, or access Secret remains")
            self.sleep(3)

    def check_faults(self, slot):
        namespace = self.namespace(slot)
        kube = self.check_journals(slot, namespace)
        node = self.nodes[slot]
        values = self.json(self.docker("exec", node["Id"], "crictl", "pods", "-o", "json"))
        sandboxes = self.rows(values)
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        component = role + "-reconciler"
        relevant = []
        for value in sandboxes:
            metadata = value.get("metadata", {})
            require(
                all(isinstance(metadata.get(key), str) for key in ("name", "namespace", "uid")),
                "CRI sandbox scope is incomplete",
            )
            if metadata["namespace"] == self.config.namespace(slot) and metadata["name"].startswith(
                component + "-"
            ):
                relevant.append(value)
        require(kube is not None or not relevant, "Orphaned fault namespace requires inspection")
        if kube is None or not relevant:
            return
        helpers = live_support().fault_helpers()
        local = helpers.fault_class(type("Local", (), {"environment": "local", "live": False})())
        module = sys.modules[local.__module__]
        pods = self.rows(kube.json("get", "pods", "-l", "plane-demo/component=" + component))
        deployment = kube.deployment(component)
        for item in relevant:
            metadata = item["metadata"]
            matching = [pod for pod in pods if pod["metadata"]["uid"] == metadata["uid"]]
            require(len(matching) == 1, "Orphaned reconciler sandbox is not safe to ignore")
            pod = matching[0]
            require(
                pod["metadata"]["name"] == metadata["name"]
                and pod["metadata"]["namespace"] == metadata["namespace"]
                and pod["spec"].get("nodeName") == node["Name"][1:]
                and not pod["spec"].get("hostNetwork")
                and not pod["spec"].get("hostPID"),
                "Reconciler sandbox owner differs",
            )
            owners = pod["metadata"].get("ownerReferences", [])
            require(len(owners) == 1 and owners[0]["kind"] == "ReplicaSet", "Unexpected Pod owner")
            rs = kube.json("get", "replicaset", owners[0]["name"])
            require(
                rs["metadata"]["uid"] == owners[0]["uid"]
                and any(
                    owner.get("kind") == "Deployment"
                    and owner.get("uid") == deployment["metadata"]["uid"]
                    for owner in rs["metadata"].get("ownerReferences", [])
                ),
                "Reconciler controller UID differs",
            )
            sandbox = self.json(self.docker("exec", node["Id"], "crictl", "inspectp", item["id"]))
            status, info = sandbox["status"], sandbox["info"]
            require(
                status["id"] == item["id"]
                and status["metadata"] == metadata
                and status["state"] == "SANDBOX_READY"
                and status["labels"].get("io.kubernetes.pod.uid") == metadata["uid"]
                and type(info.get("pid")) is int
                and info["pid"] > 1,
                "Reconciler network namespace is not verified",
            )
            inode = self.call(
                self.docker("exec", node["Id"], "stat", "-Lc", "%i", f"/proc/{info['pid']}/ns/net")
            ).strip()
            require(re.fullmatch(r"[1-9][0-9]{0,19}", inode), "Invalid network namespace inode")
            rules = self.call(
                self.docker(
                    "exec",
                    node["Id"],
                    "bash",
                    "-ceu",
                    module.NETWORK_COMMAND,
                    "cleanup-netns",
                    str(info["pid"]),
                    inode,
                    "iptables",
                    "-w",
                    "2",
                    "-S",
                    "OUTPUT",
                )
            )
            require("plane-demo-fault-" not in rules, "Active parent fault must be restored first")

    def clean(self):
        self.nodes, self.initial_unrelated = self.containers()
        clusters = dict(self.nodes)
        if "management" in clusters:
            self.check_bootstrap_lease()
        self.clean_radius(clusters)
        if "management" in clusters:
            if self.execute:
                require(
                    not self.state_inventory("management"), "Management Terraform owners remain"
                )
                require(not self.access_inventory(), "Child access Secret owners remain")
                for slot in CHILDREN:
                    require(self.access_secret(slot) is None, "Child access Secret remains")
                nodes, _ = self.containers()
                require(
                    set(nodes) == {"management"}
                    and nodes["management"]["Id"] == self.nodes["management"]["Id"],
                    "Management node changed or children remain",
                )
                self.verify_cluster("management")
            self.note("bootstrap-management-kind", self.config.slot_name("management"))
            if self.execute:
                target = self.targets["management"]
                self.call(
                    [
                        "kind",
                        "delete",
                        "cluster",
                        "--name",
                        self.config.slot_name("management"),
                        "--kubeconfig",
                        str(target["kubeconfig"]),
                    ],
                    mutation=True,
                    timeout=600,
                    env={
                        **self.env,
                        "HOME": str(target["home"]),
                        "DOCKER_HOST": self.host,
                        "KIND_EXPERIMENTAL_PROVIDER": "docker",
                    },
                )
        if not self.execute:
            return {"status": "planned", "environment": "local", "steps": self.steps}
        result = self.verify()
        require(
            self.initial_unrelated <= set(result["unrelatedContainerIds"]),
            "An unrelated container disappeared during cleanup",
        )
        result["unrelatedPreservationVerified"] = True
        result["steps"] = self.steps
        return result

    def verify(self):
        nodes, unrelated = self.containers()
        require(not nodes, "Owned local containers remain: " + ", ".join(sorted(nodes)))
        return {
            "status": "clean",
            "scope": "owned-active-resources",
            "environment": "local",
            "deployment": self.config.stem,
            "unrelatedContainerIds": sorted(unrelated),
            "retained": ["kind network", "images and build cache", "local files"],
        }


def legacy_main(argv: list[str] | None = None) -> int:
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
            path = Path(args.verify)
            if not path.is_absolute() and path.parts[:2] == (".state", "local"):
                path = ROOT / path
            record = read_json(path)
            require(
                record.get("version") == 1
                and record.get("scope") == "cleanup"
                and set(record["targets"]) == set(SLOTS)
                and set(
                    step["identity"]
                    for step in record["steps"]
                    if step["action"] == "radius_cluster_owners_absent"
                )
                == set(CHILDREN)
                and any(
                    step["action"] == "radius_application_absent"
                    and step["identity"] == "management/management"
                    for step in record["steps"]
                ),
                "Verification requires retained proof of each child Radius/state/access deletion",
            )
            cleanup = Cleanup(commands, {}, False)
            cleanup.verify_absent(record)
            print(
                json.dumps(
                    {
                        "scope": "cleanup",
                        "result": "resources_removed",
                        "verifiedAt": stamp(),
                    }
                )
            )
            return 0
        targets, provenance = load_inputs()
        cleanup = Cleanup(commands, targets, args.execute)
        cleanup.record.update(provenance)
        cleanup.preflight()
        if not args.execute:
            print(
                json.dumps(
                    {
                        "scope": "cleanup",
                        "result": "preview",
                        "destroysDemoData": True,
                        "order": ["quiesce-management", *CHILDREN, "management"],
                    }
                )
            )
            return 0
        cleanup.destroy()
        cleanup.record.update(result="resources_removed", completedAt=stamp())
        write_private(cleanup.path, cleanup.record)
        print(
            json.dumps(
                {
                    "scope": "cleanup",
                    "result": "resources_removed",
                    "record": str(cleanup.path),
                }
            )
        )
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


def main(argv=None, *, engine_factory=LiveLocalCleanup):
    parser = argparse.ArgumentParser(
        description="Normal local cleanup from .env and live Radius owners."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument(
        "--verify", action="store_true", help="Independent Docker verification; no record required"
    )
    parser.add_argument("--environment", choices=("local",), default="local")
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = engine_factory(execute=args.execute)
        print(json.dumps(engine.verify() if args.verify else engine.clean(), indent=2))
        return 0
    except KeyboardInterrupt:
        print(
            "Cleanup incomplete: interrupted; inspect live owners before retrying.", file=sys.stderr
        )
        return 130
    except (
        LocalError,
        live_support().CleanupError,
        live_support().ConfigError,
        live_support().fault_helpers().AcceptanceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        print(f"Cleanup incomplete: {error}; no direct child deletion", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
