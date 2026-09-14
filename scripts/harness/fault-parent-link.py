#!/usr/bin/env python3
"""Opt-in, namespaced Cilium parent-PostgreSQL faults; never a policy-only success."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.operations.config import ConfigError, load_config  # noqa: E402

PROJECT = "radplanes"
RECOVERY_SECONDS = 30
COMPONENT_DSN = {
    "control-reconciler": "MANAGEMENT_DSN",
    "data-reconciler": "CONTROL_DSN",
}
SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
UID = re.compile(r"[a-f0-9-]{16,64}\Z")
POLICY_RESOURCE = "ciliumnetworkpolicies.cilium.io"
LOCAL_SLOTS = (
    "management",
    "shared-control",
    "shared-data",
    "isolated-1-control",
    "isolated-1-data",
)


class AcceptanceError(RuntimeError):
    """Only stable, non-secret error identifiers may enter public evidence."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise AcceptanceError(code)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def state_path(path: Path) -> Path:
    state = (ROOT / ".state").resolve()
    require(state == ROOT / ".state", "project_state_symlink_escape")
    resolved = (ROOT / path).resolve()
    require(resolved.is_relative_to(state), "state_path_escape")
    return resolved


def source_metadata() -> dict:
    commit = command(["git", "rev-parse", "HEAD"]).strip()
    require(bool(re.fullmatch(r"[a-f0-9]{40,64}", commit)), "invalid_source_commit")
    committed_at = command(["git", "show", "-s", "--format=%cI", "HEAD"]).strip()
    require(datetime.fromisoformat(committed_at) <= datetime.now(UTC), "source_commit_in_future")
    dirty = command(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "scripts/harness/test-e2e.py",
            "scripts/harness/fault-parent-link.py",
            "scripts/harness/local",
            "scripts/lib",
            "scripts/operations/api.sh",
            "scripts/operations/endpoints.sh",
            "src/plane_demo",
            "sql",
            "infra/radius/apps",
            "infra/radius/modules",
            "images/api",
            "images/provisioner",
            "pyproject.toml",
            "uv.lock",
        ]
    ).strip()
    return {"commit": commit, "committed_at": committed_at, "worktree_dirty": bool(dirty)}


def read_json(path: Path) -> dict:
    path = state_path(path)
    try:
        require(path.stat().st_size <= 2_000_000, "configuration_too_large")
        value = json.loads(path.read_text())
        require(isinstance(value, dict), "configuration_must_be_object")
        return value
    except (OSError, ValueError):
        raise AcceptanceError("configuration_unreadable") from None


def protected_write(path: Path, value: dict) -> None:
    path = ROOT / path
    state_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not path.is_symlink(), "evidence_symlink_refused")
    pending = path.with_name(path.name + "." + uuid4().hex + ".writing")
    try:
        descriptor = os.open(
            pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        pending.unlink(missing_ok=True)


@dataclass(frozen=True)
class Target:
    project: str
    slot: str
    context: str
    kubeconfig: Path
    namespace: str
    cluster_uid: str
    namespace_uid: str
    components: dict
    parent: dict
    local: dict = field(default_factory=dict)
    cluster_id: str = ""
    ownership: dict = field(default_factory=dict)

    def component(self, name: str) -> dict:
        value = self.components.get(name)
        require(isinstance(value, dict), "component_not_configured")
        require(
            all(
                isinstance(value.get(key), str) and SLUG.fullmatch(value[key])
                for key in ("deployment", "container")
            ),
            "invalid_component_names",
        )
        return value


class Configuration:
    """Legacy snapshot adapter for offline fault and continuation regression coverage."""

    live = False

    def __init__(self, path: Path):
        self.path = state_path(path)
        self.root = self.path.parent
        value = read_json(self.path)
        require(value.get("version") == 1, "unsupported_configuration_version")
        self.project = value.get("project", "")
        self.environment = value.get("environment", "")
        require(self.project == PROJECT, "invalid_project")
        require(self.environment in {"azure", "local"}, "invalid_environment")
        require(
            self.environment != "local" or self.root == ROOT / ".state/local",
            "local_configuration_scope",
        )

    def file(self, value: str, *, secret: bool = False) -> Path:
        require(isinstance(value, str) and bool(value), "missing_state_file")
        if self.environment == "local":
            raw = self.root / value
            require(
                all(not part.is_symlink() for part in [raw, *raw.parents]),
                "local_state_symlink_refused",
            )
        path = (self.root / value).resolve()
        require(path.is_relative_to(self.root), "state_path_escape")
        require(path.is_file(), "state_file_missing")
        if secret:
            require(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "credential_file_permissions")
            if self.environment == "local":
                require(stat.S_IMODE(path.stat().st_mode) == 0o600, "credential_file_permissions")
        return path

    def current(self) -> dict:
        value = read_json(self.path)
        require(
            value.get("project") == self.project
            and value.get("environment") == self.environment
            and value.get("version") == 1,
            "configuration_identity_changed",
        )
        return value

    def target(self, slot: str) -> Target:
        require(bool(SLUG.fullmatch(slot)), "invalid_slot")
        value = self.current().get("targets", {}).get(slot)
        require(isinstance(value, dict), "target_not_exported")
        namespace = value.get("namespace", "")
        require(
            bool(SLUG.fullmatch(namespace)) and namespace.startswith(self.project + "-"),
            "invalid_target_namespace",
        )
        if self.environment == "local":
            role = "management" if slot == "management" else slot.rsplit("-", 1)[-1]
            require(
                slot in LOCAL_SLOTS and namespace == f"radplanes-local-{slot}-{role}",
                "local_target_namespace_mismatch",
            )
        context = value.get("context", "")
        require(
            isinstance(context, str)
            and context
            == self.project + ("-local-" if self.environment == "local" else "-") + slot,
            "unexpected_target_context",
        )
        require(
            all(
                isinstance(value.get(key), str) and UID.fullmatch(value[key])
                for key in ("cluster_uid", "namespace_uid")
            ),
            "missing_target_uids",
        )
        kubeconfig = self.file(value.get("kubeconfig"), secret=True)
        self.verify_transport(kubeconfig, context)
        if self.environment == "local":
            self.verify_local_transport(kubeconfig, context, slot, value.get("local", {}))
        return Target(
            self.project,
            slot,
            context,
            kubeconfig,
            namespace,
            value["cluster_uid"],
            value["namespace_uid"],
            value.get("components", {}),
            value.get("parent", {}),
            value.get("local", {}),
        )

    @staticmethod
    def verify_local_transport(path, context, slot, local):
        import yaml

        try:
            value = yaml.safe_load(path.read_text())
            require(
                value.get("current-context") == context
                and len(value["contexts"]) == len(value["clusters"]) == len(value["users"]) == 1,
                "local_kubeconfig_shape_mismatch",
            )
            selected, cluster, user = (
                value["contexts"][0],
                value["clusters"][0],
                value["users"][0],
            )
            fields = cluster["cluster"]
            require(
                selected["context"]["cluster"] == cluster["name"]
                and selected["context"]["user"] == user["name"]
                and set(fields) <= {"server", "certificate-authority-data", "tls-server-name"}
                and fields.get("server") == f"https://127.0.0.1:{35495 + LOCAL_SLOTS.index(slot)}"
                and (slot == "management" or fields.get("tls-server-name") == context)
                and fields.get("tls-server-name") in (None, context)
                and set(user["user"]) == {"client-certificate-data", "client-key-data"}
                and hashlib.sha256(path.read_bytes()).hexdigest() == local.get("kubeconfig_sha256")
                and hashlib.sha256(
                    base64.b64decode(fields["certificate-authority-data"], validate=True)
                ).hexdigest()
                == local.get("ca_sha256"),
                "local_kubeconfig_transport_mismatch",
            )
        except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
            raise AcceptanceError("local_kubeconfig_invalid") from None

    @staticmethod
    def verify_transport(path: Path, context: str):
        import yaml

        try:
            values = yaml.safe_load(path.read_text())
            selected = [item["context"] for item in values["contexts"] if item["name"] == context]
            require(len(selected) == 1, "kubeconfig_context_missing_or_ambiguous")
            clusters = [
                item["cluster"]
                for item in values["clusters"]
                if item["name"] == selected[0]["cluster"]
            ]
            require(len(clusters) == 1, "kubeconfig_cluster_missing_or_ambiguous")
            require(
                clusters[0].get("insecure-skip-tls-verify") is not True
                and clusters[0].get("server", "").startswith("https://"),
                "insecure_kubernetes_transport_refused",
            )
        except (OSError, TypeError, KeyError, yaml.YAMLError):
            raise AcceptanceError("invalid_project_kubeconfig") from None


DISCOVER_SLOT = r"""
set -euo pipefail
set +x
umask 077
source "$1/scripts/lib/env.sh"
source "$1/scripts/lib/discovery.sh"
demo_load_env "$1/.env"
[[ "$DEMO_ENV" == "$PLANE_DEMO_EXPECT_ENV" ]] || {
  demo_error 'Environment changed after configuration was loaded'; exit 1;
}
DEMO_WORKSPACE=$2
demo_open_slot "$3"
url=$(demo_endpoint)
cluster=$(demo_kube get namespace kube-system --output json)
namespace=$(demo_kube get namespace "$DEMO_NAMESPACE" --output json)
node=null
host=
if [[ "$DEMO_ENV" == local ]]; then
  host=$(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_CONFIG \
    docker context inspect desktop-linux --format '{{json .Endpoints.docker.Host}}' | jq -er .)
  node=$(docker --host "$host" inspect --type container "$DEMO_CONTEXT-control-plane")
fi
jq -n --arg slot "$DEMO_SLOT" --arg context "$DEMO_CONTEXT" --arg url "$url" \
  --arg project "$DEMO_PROJECT" --arg deployment "$DEMO_DEPLOYMENT" \
  --arg environment "$DEMO_ENV" --arg kubeconfig "$DEMO_KUBECONFIG" --arg host "$host" \
  --argjson cluster "$cluster" --argjson namespace "$namespace" --argjson node "$node" \
  '{slot:$slot,context:$context,url:$url,project:$project,deployment:$deployment,
    environment:$environment,kubeconfig:$kubeconfig,cluster:$cluster,
    namespace:$namespace,node:$node,docker_host:$host}'
"""

PARENT_BINDING_PROBE = r"""
import json,os,sys
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
try:
    name=sys.argv[1]
    if name not in ("MANAGEMENT_DSN","CONTROL_DSN"): raise ValueError()
    values=conninfo_to_dict(os.environ[name])
    if values.get("hostaddr") or values.get("service"): raise ValueError()
    print(json.dumps({"host":values["host"],"port":int(values.get("port","5432")),
                      "sslmode":values.get("sslmode")}))
except (KeyError,ValueError,ProgrammingError):
    print(json.dumps({"error":"parent_binding_invalid"}))
    sys.exit(1)
"""


class LiveConfiguration:
    """One run's native discovery and private, disposable Kubernetes access."""

    live = True

    def __init__(self, path: Path = ROOT / ".env", *, execute=None):
        self.path = ROOT / path
        require(self.path == ROOT / ".env", "configuration_must_be_checkout_dotenv")
        try:
            self.config = load_config(self.path)
        except ConfigError:
            raise AcceptanceError("invalid_dotenv_configuration") from None
        self.project = self.config.project
        self.environment = self.config.environment
        self.execute = execute or subprocess.run
        self.workspace = tempfile.TemporaryDirectory(prefix=".harness-", dir=ROOT)
        self.root = Path(self.workspace.name)
        self.targets = {}
        self.urls = {}
        self.keys = {}
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.env = {**os.environ, "HOME": str(self.home)}
        for name in ("DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_HOST", "KUBECONFIG"):
            self.env.pop(name, None)
        if self.environment == "azure":
            self.env["AZURE_CONFIG_DIR"] = os.environ.get(
                "AZURE_CONFIG_DIR", str(Path.home() / ".azure")
            )

    def close(self):
        self.keys.clear()
        self.workspace.cleanup()

    def current(self):
        try:
            require(load_config(self.path) == self.config, "configuration_identity_changed")
        except ConfigError:
            raise AcceptanceError("invalid_dotenv_configuration") from None
        return {
            "version": 1,
            "project": self.project,
            "environment": self.environment,
            "synthetic_data": True,
        }

    def command(self, argv, *, payload=None, timeout=30, binary=False, discovery=False):
        try:
            result = self.execute(
                argv,
                input=payload,
                capture_output=True,
                text=not binary,
                timeout=timeout,
                check=False,
                cwd=ROOT,
                env={
                    **os.environ,
                    "TMPDIR": str(self.root),
                    "PLANE_DEMO_EXPECT_ENV": self.environment,
                }
                if discovery
                else self.env,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AcceptanceError("live_discovery_command_unavailable_or_timeout") from None
        require(result.returncode == 0, "live_discovery_command_failed")
        require(len(result.stdout) <= 2_000_000, "live_discovery_response_too_large")
        return result.stdout

    def target(self, slot):
        self.current()
        require(slot in LOCAL_SLOTS, "invalid_slot")
        if slot in self.targets:
            return self.targets[slot]
        work = Path(tempfile.mkdtemp(prefix="access-", dir=self.root))
        raw = self.command(
            ["bash", "-c", DISCOVER_SLOT, "harness-discovery", str(ROOT), str(work), slot],
            timeout=120,
            discovery=True,
        )
        try:
            value = json.loads(raw)
            namespace = value["namespace"]["metadata"]
            cluster = value["cluster"]["metadata"]
            context = self.config.slot_name(slot)
            require(
                value["slot"] == slot
                and value["project"] == self.project
                and value["deployment"] == self.config.deployment
                and value["environment"] == self.environment
                and value["context"] == context
                and cluster["name"] == "kube-system"
                and namespace["name"] == self.config.namespace(slot)
                and UID.fullmatch(cluster["uid"])
                and UID.fullmatch(namespace["uid"]),
                "live_discovery_identity_mismatch",
            )
            ownership = {
                "plane-demo/project": self.project,
                "plane-demo/deployment": self.config.deployment,
                "plane-demo/environment": self.environment,
            }
            require(
                all(namespace.get("labels", {}).get(key) == val for key, val in ownership.items()),
                "live_namespace_ownership_mismatch",
            )
            profile = work / slot / "kubeconfig"
            require(
                value["kubeconfig"] == str(profile)
                and profile.is_file()
                and not profile.is_symlink()
                and stat.S_IMODE(profile.stat().st_mode) == 0o600,
                "live_kubeconfig_not_private",
            )
            Configuration.verify_transport(profile, context)
            role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
            components = (
                ("management-api", "provisioner")
                if role == "management"
                else (role + "-api", role + "-reconciler")
            )
            local = {}
            if self.environment == "local":
                require(len(value["node"]) == 1, "local_node_inventory_mismatch")
                node = value["node"][0]
                address = ipaddress.IPv4Address(
                    node["NetworkSettings"]["Networks"]["kind"]["IPAddress"]
                )
                host = value["docker_host"]
                require(
                    isinstance(host, str)
                    and re.fullmatch(r"unix:///[^\s?#]+", host)
                    and ".." not in Path(host.removeprefix("unix://")).parts
                    and re.fullmatch(r"[a-f0-9]{64}", node["Id"])
                    and node["Name"] == f"/{context}-control-plane"
                    and node["Config"]["Labels"]["io.x-k8s.kind.cluster"] == context
                    and node["Config"]["Labels"]["io.x-k8s.kind.role"] == "control-plane"
                    and node["State"]["Running"] is True
                    and address.is_private
                    and not (
                        address.is_loopback
                        or address.is_link_local
                        or address.is_multicast
                        or address.is_unspecified
                        or address.is_reserved
                    ),
                    "local_node_ownership_mismatch",
                )
                local = {
                    "docker_host": host,
                    "node": {"id": node["Id"], "name": node["Name"][1:], "address": str(address)},
                }
                cluster_id = f"kind://{context}"
            else:
                cluster_id = (
                    f"/subscriptions/{self.config.subscription}/resourceGroups/"
                    f"rg-{context}-cluster/providers/Microsoft.ContainerService/"
                    f"managedClusters/aks-{context}"
                )
            target = Target(
                self.project,
                slot,
                context,
                profile,
                namespace["name"],
                cluster["uid"],
                namespace["uid"],
                {name: {"deployment": name, "container": name} for name in components},
                {},
                local,
                cluster_id,
                ownership,
            )
            self.validate_endpoint(slot, value["url"])
        except (KeyError, TypeError, ValueError):
            raise AcceptanceError("invalid_live_discovery_response") from None
        self.targets[slot], self.urls[slot] = target, value["url"]
        return target

    def validate_endpoint(self, slot, url):
        if self.environment == "local":
            require(
                url == f"http://127.0.0.1:{35490 + LOCAL_SLOTS.index(slot)}",
                "local_endpoint_not_reserved_loopback",
            )
        else:
            require(
                isinstance(url, str)
                and re.fullmatch(r"https://[a-z0-9][a-z0-9.-]*\.cloudapp\.azure\.com", url),
                "azure_endpoint_requires_trusted_https",
            )

    def endpoint(self, name):
        slot = name if name == "management" else "-".join(reversed(name.split(":", 1)))
        target = self.target(slot)
        if slot not in self.keys:
            key = self.config.demo_keys.get(slot)
            if key is None:
                role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
                secret_name = role + "-api-runtime"
                fields = (
                    Kubectl(target, self.command)
                    .run(
                        "get",
                        "secret",
                        secret_name,
                        "-o",
                        'jsonpath={.metadata.name}{"\\n"}{.metadata.namespace}{"\\n"}{.data.DEMO_KEY}',
                    )
                    .splitlines()
                )
                require(
                    len(fields) == 3 and fields[0] == secret_name and fields[1] == target.namespace,
                    "api_credential_scope_mismatch",
                )
                try:
                    encoded = fields[2]
                    key = base64.b64decode(encoded, validate=True).decode("ascii")
                except (KeyError, ValueError, UnicodeError):
                    raise AcceptanceError("invalid_demo_key") from None
            require(
                isinstance(key, str) and re.fullmatch(r"[!-~]{32,512}", key), "invalid_demo_key"
            )
            self.keys[slot] = key
        return self.urls[slot], self.keys[slot]

    def kube(self, target):
        kube = Kubectl(target, self.command)
        kube.environment = self.env
        return kube

    def fault_target(self, slot, component):
        require(component in COMPONENT_DSN, "unsupported_fault_component")
        suffix = "-control" if component == "control-reconciler" else "-data"
        require(slot in LOCAL_SLOTS[1:] and slot.endswith(suffix), "fault_component_slot_mismatch")
        target = self.target(slot)
        parent_slot = (
            "management"
            if component == "control-reconciler"
            else slot.removesuffix("-data") + "-control"
        )
        parent_target = self.target(parent_slot)
        parent_kube = self.kube(parent_target)
        parent_kube.verify_scope()
        prefix = f"/planes/radius/local/resourceGroups/{self.config.stem}/providers"
        resource_id = prefix + "/Demo.Platform/postgreSqlDatabases/postgres"
        parent_role = "management" if parent_slot == "management" else "control"
        try:
            resource = json.loads(
                parent_kube.run(
                    "get",
                    "--raw",
                    f"/apis/api.ucp.dev/v1alpha3{resource_id}?api-version=2025-08-01-preview",
                )
            )
            properties = resource["properties"]
            require(
                resource["id"].lower() == resource_id.lower()
                and properties["application"].lower()
                == (prefix + "/Applications.Core/applications/" + parent_role).lower()
                and properties["environment"].lower()
                == (prefix + "/Applications.Core/environments/" + parent_slot).lower()
                and properties["provisioningState"] == "Succeeded"
                and properties["database"] == parent_role,
                "parent_radius_owner_mismatch",
            )
            host, port, server_id = properties["host"], properties["port"], properties["serverId"]
            owner = {
                "slot": parent_slot,
                "cluster_uid": parent_target.cluster_uid,
                "namespace_uid": parent_target.namespace_uid,
                "resource_id": resource_id,
                "server_id": server_id,
            }
            local = dict(target.local)
            if self.environment == "local":
                parent_node = parent_target.local["node"]
                require(
                    host == parent_node["address"]
                    and type(port) is int
                    and port == 31543
                    and properties["tlsRequired"] is False
                    and server_id == f"kubernetes://{parent_target.namespace}/statefulsets/postgres"
                    and target.local["docker_host"] == parent_target.local["docker_host"],
                    "local_parent_radius_binding_mismatch",
                )
                server = parent_kube.json("get", "statefulset", "postgres")["metadata"]
                require(
                    server["name"] == "postgres"
                    and server["namespace"] == parent_target.namespace
                    and UID.fullmatch(server["uid"]),
                    "local_parent_server_identity_mismatch",
                )
                owner["server_uid"] = server["uid"]
                allowed = [host + "/32"]
                management = self.target("management")
                require(
                    management.local["docker_host"] == target.local["docker_host"],
                    "local_docker_host_changed",
                )
                local.update(parent_node=parent_node, management_node=management.local["node"])
            else:
                expected_server = (
                    f"/subscriptions/{self.config.subscription}/resourceGroups/"
                    f"rg-{self.config.slot_name(parent_slot)}-app/providers/"
                    "Microsoft.DBforPostgreSQL/flexibleServers/"
                )
                require(
                    isinstance(host, str)
                    and re.fullmatch(r"[a-z0-9-]+\.postgres\.database\.azure\.com", host)
                    and type(port) is int
                    and port == 5432
                    and properties["tlsRequired"] is True
                    and isinstance(server_id, str)
                    and server_id.lower().startswith(expected_server.lower())
                    and SLUG.fullmatch(server_id[len(expected_server) :]),
                    "azure_parent_radius_binding_mismatch",
                )
                server = self.azure_json(
                    "postgres",
                    "flexible-server",
                    "show",
                    "--ids",
                    server_id,
                    "--query",
                    "{id:id,host:fullyQualifiedDomainName,network:network,tags:tags,state:state}",
                )
                vnet_id = (
                    f"/subscriptions/{self.config.subscription}/resourceGroups/"
                    f"rg-{self.config.stem}-platform/providers/Microsoft.Network/"
                    f"virtualNetworks/vnet-{self.config.stem}"
                )
                subnet_id = vnet_id + "/subnets/snet-" + parent_slot + "-postgresql"
                require(
                    server["id"].lower() == server_id.lower()
                    and server["host"] == host
                    and server["state"] == "Ready"
                    and server["network"]["publicNetworkAccess"] == "Disabled"
                    and server["network"]["delegatedSubnetResourceId"].lower() == subnet_id.lower()
                    and server["tags"]["radapp.io-resource"].lower() == resource_id.lower()
                    and server["tags"]["radapp.io-environment"].lower()
                    == properties["environment"].lower()
                    and server["tags"]["radapp.io-application"].lower()
                    == properties["application"].lower(),
                    "azure_parent_server_owner_mismatch",
                )
                vnet = self.azure_json(
                    "network", "vnet", "show", "--ids", vnet_id, "--query", "{id:id,tags:tags}"
                )
                require(
                    vnet["id"].lower() == vnet_id.lower()
                    and vnet["tags"].get("project") == self.project
                    and vnet["tags"].get("deployment") == self.config.deployment
                    and vnet["tags"].get("environment") == "azure",
                    "azure_parent_network_owner_mismatch",
                )
                subnet = self.azure_json(
                    "network",
                    "vnet",
                    "subnet",
                    "show",
                    "--ids",
                    subnet_id,
                    "--query",
                    "{id:id,addressPrefix:addressPrefix,addressPrefixes:addressPrefixes,"
                    "delegations:delegations}",
                )
                allowed = subnet.get("addressPrefixes") or [subnet["addressPrefix"]]
                require(
                    subnet["id"].lower() == subnet_id.lower()
                    and len(allowed) == 1
                    and any(
                        item.get("serviceName") == "Microsoft.DBforPostgreSQL/flexibleServers"
                        for item in subnet["delegations"]
                    ),
                    "azure_parent_subnet_mismatch",
                )
                network = ipaddress.ip_network(allowed[0], strict=True)
                require(
                    network.version == 4
                    and network.prefixlen == 27
                    and network.network_address.is_private
                    and not (
                        network.network_address.is_loopback or network.network_address.is_link_local
                    ),
                    "azure_parent_subnet_scope_mismatch",
                )
                owner["subnet_id"] = subnet_id
            child_kube = self.kube(target)
            child_kube.verify_scope()
            binding = child_kube.exec_json(
                component, PARENT_BINDING_PROBE, COMPONENT_DSN[component]
            )
            require(
                binding
                == {
                    "host": host,
                    "port": port,
                    "sslmode": "disable" if self.environment == "local" else "verify-full",
                },
                "runtime_parent_binding_mismatch",
            )
            updated = replace(
                target,
                parent={"host": host, "port": port, "allowed_cidrs": allowed, "owner": owner},
                local=local,
            )
        except (KeyError, TypeError, ValueError):
            raise AcceptanceError("parent_discovery_response_invalid") from None
        if target.parent:
            require(updated == target, "parent_owner_or_binding_changed")
        self.targets[slot] = updated
        return updated

    def azure_json(self, *args):
        raw = self.command(
            [
                "az",
                *args,
                "--subscription",
                self.config.subscription,
                "--output",
                "json",
                "--only-show-errors",
            ]
        )
        try:
            result = json.loads(raw)
        except ValueError:
            raise AcceptanceError("azure_parent_response_invalid") from None
        require(isinstance(result, dict), "azure_parent_response_invalid")
        return result


def command(argv: list[str], *, payload: str | None = None, timeout: float = 30) -> str:
    try:
        result = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=ROOT,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise AcceptanceError("operator_command_unavailable_or_timeout") from None
    require(result.returncode == 0, "operator_command_failed")
    return result.stdout


class Kubectl:
    def __init__(self, target: Target, runner=command):
        self.target = target
        self.runner = runner

    def argv(self, *args: str, streaming: bool = False) -> list[str]:
        return [
            "kubectl",
            "--kubeconfig",
            str(self.target.kubeconfig),
            "--context",
            self.target.context,
            "--namespace",
            self.target.namespace,
            "--request-timeout=0" if streaming else "--request-timeout=15s",
            *args,
        ]

    def run(self, *args: str, payload: str | None = None, timeout: float = 30) -> str:
        return self.runner(self.argv(*args), payload=payload, timeout=timeout)

    def json(self, *args: str) -> dict:
        try:
            return json.loads(self.run(*args, "-o", "json"))
        except ValueError:
            raise AcceptanceError("invalid_kubernetes_response") from None

    def optional(self, resource: str, name: str) -> dict | None:
        result = self.run("get", resource, name, "--ignore-not-found", "-o", "json")
        try:
            return json.loads(result) if result.strip() else None
        except ValueError:
            raise AcceptanceError("invalid_kubernetes_response") from None

    def delete_uid(self, plural: str, name: str, uid: str, *, group: str = "") -> None:
        from kubernetes import config
        from kubernetes.client.exceptions import ApiException

        require(bool(SLUG.fullmatch(name)) and bool(UID.fullmatch(uid)), "invalid_delete_identity")
        require(
            (plural, group) in {("pods", ""), ("ciliumnetworkpolicies", "cilium.io/v2")},
            "unsupported_delete_resource",
        )
        prefix = f"/apis/{group}" if group else "/api/v1"
        path = f"{prefix}/namespaces/{self.target.namespace}/{plural}/{name}"
        try:
            with config.new_client_from_config(
                config_file=str(self.target.kubeconfig), context=self.target.context
            ) as api:
                require(api.configuration.verify_ssl, "insecure_kubernetes_tls_refused")
                api.call_api(
                    path,
                    "DELETE",
                    body={
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": {"uid": uid},
                        "gracePeriodSeconds": 5,
                    },
                    auth_settings=["BearerToken"],
                    response_type="object",
                    _request_timeout=(5, 15),
                )
        except ApiException:
            raise AcceptanceError("uid_guarded_delete_failed") from None

    def verify_scope(self) -> None:
        cluster = self.json("get", "namespace", "kube-system")
        namespace = self.json("get", "namespace", self.target.namespace)
        require(cluster["metadata"]["uid"] == self.target.cluster_uid, "cluster_uid_mismatch")
        require(namespace["metadata"]["uid"] == self.target.namespace_uid, "namespace_uid_mismatch")
        require(
            all(
                namespace["metadata"].get("labels", {}).get(key) == value
                for key, value in getattr(self.target, "ownership", {}).items()
            ),
            "namespace_ownership_mismatch",
        )

    def labels(self, component: str) -> dict[str, str]:
        return {
            "plane-demo/project": self.target.project,
            **getattr(self.target, "ownership", {}),
            "plane-demo/component": component,
        }

    def deployment(self, component: str) -> dict:
        configured = self.target.component(component)
        deployment = self.json("get", "deployment", configured["deployment"])
        labels = deployment["spec"]["template"]["metadata"].get("labels", {})
        require(
            all(labels.get(key) == value for key, value in self.labels(component).items()),
            "deployment_ownership_mismatch",
        )
        require(
            deployment["metadata"]["namespace"] == self.target.namespace,
            "deployment_namespace_mismatch",
        )
        return deployment

    def pod(self, component: str) -> dict:
        deployment = self.deployment(component)
        require(deployment["spec"].get("replicas", 1) == 1, "expected_single_replica")
        selector = ",".join(f"{key}={value}" for key, value in self.labels(component).items())
        items = self.json("get", "pods", "-l", selector).get("items", [])
        items = [pod for pod in items if not pod["metadata"].get("deletionTimestamp")]
        require(len(items) == 1, "expected_one_scoped_pod")
        pod = items[0]
        require(pod.get("status", {}).get("phase") == "Running", "pod_not_running")
        require(not pod.get("spec", {}).get("hostNetwork", False), "host_network_not_supported")
        owner = pod["metadata"].get("ownerReferences", [])
        require(
            len(owner) == 1 and owner[0].get("kind") == "ReplicaSet", "unexpected_pod_controller"
        )
        replicaset = self.json("get", "replicaset", owner[0]["name"])
        require(replicaset["metadata"]["uid"] == owner[0]["uid"], "replicaset_uid_mismatch")
        owners = replicaset["metadata"].get("ownerReferences", [])
        require(
            any(
                item.get("kind") == "Deployment"
                and item.get("uid") == deployment["metadata"]["uid"]
                for item in owners
            ),
            "pod_deployment_uid_mismatch",
        )
        require(
            any(
                container["name"] == self.target.component(component)["container"]
                for container in pod["spec"].get("containers", [])
            ),
            "configured_container_missing",
        )
        return pod

    def exec_json(self, component: str, code: str, *arguments: str) -> dict:
        pod = self.pod(component)
        output = self.run(
            "exec",
            pod["metadata"]["name"],
            "-c",
            self.target.component(component)["container"],
            "--",
            "python",
            "-c",
            code,
            *arguments,
            timeout=40,
        )
        try:
            return json.loads(output)
        except ValueError:
            raise AcceptanceError("invalid_pod_probe_response") from None

    def policies(self, exclude: str | None = None, *, reject_faults=False) -> list[dict]:
        policies = []
        for resource in ("networkpolicies.networking.k8s.io", POLICY_RESOURCE):
            for item in self.json("get", resource).get("items", []):
                if reject_faults:
                    metadata = item["metadata"]
                    labels = metadata.get("labels", {})
                    annotations = metadata.get("annotations", {})
                    require(
                        not metadata["name"].startswith("plane-demo-fault-")
                        and "plane-demo/fault-run" not in labels
                        and not labels.get("plane-demo/journal", "").startswith("plane-demo-fault-")
                        and "plane-demo/journal-uid" not in annotations
                        and "plane-demo/rule-sha256" not in annotations,
                        "unattempted_fault_artifact_present",
                    )
                if resource == POLICY_RESOURCE and item["metadata"]["name"] == exclude:
                    continue
                policies.append(
                    {
                        "kind": item["kind"],
                        "name": item["metadata"]["name"],
                        "uid": item["metadata"]["uid"],
                        "spec_sha256": hashlib.sha256(
                            json.dumps(item["spec"], sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest(),
                    }
                )
        return sorted(policies, key=lambda value: (value["kind"], value["name"]))


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def intent_fingerprint(record, kind):
    fields = (
        ("run_id", "project", "environment", "mode", "source")
        if kind == "acceptance"
        else (
            "run_id",
            "project",
            "environment",
            "slot",
            "component",
            "cluster_uid",
            "namespace_uid",
            "parent",
            "pod_uid",
            "node",
            "sandbox",
            "rule",
            "original_rules_sha256",
            "original_policies",
            "baseline",
        )
    )
    intent = {key: record.get(key) for key in fields}
    if kind == "fault":
        intent["policy_spec"] = record.get("policy", {}).get("spec")
    return hashlib.sha256(canonical_json(intent).encode()).hexdigest()


class ConfigMapJournal:
    """Namespace-owned, UID-bound records with optimistic concurrency and immutable intent."""

    def __init__(self, kube, name, kind):
        require(kind in {"fault", "acceptance"}, "journal_kind_invalid")
        pattern = (
            r"plane-demo-fault-(?:control|data)-reconciler"
            if kind == "fault"
            else r"plane-demo-acceptance-[a-f0-9]{32}"
        )
        require(isinstance(name, str) and re.fullmatch(pattern, name), "journal_name_invalid")
        self.kube, self.name, self.kind = kube, name, kind
        self.uid = self.version = self.snapshot = self.intent = None
        self.record = None
        self.claimed_by = None
        self.updated_at = None
        self.sealed = False

    @property
    def reference(self):
        require(self.uid is not None and self.record is not None, "journal_not_committed")
        return f"{self.name}@{self.uid}@{self.record['run_id']}"

    @property
    def owner(self):
        target = self.kube.target
        return [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "name": target.namespace,
                "uid": target.namespace_uid,
                "controller": False,
                "blockOwnerDeletion": False,
            }
        ]

    @property
    def labels(self):
        return {
            **self.kube.target.ownership,
            "plane-demo/project": self.kube.target.project,
            "plane-demo/journal-kind": self.kind,
        }

    def _decode(self, value, *, unsealed=False):
        try:
            metadata, data = value["metadata"], value["data"]
            annotations = metadata["annotations"]
            require(
                value["apiVersion"] == "v1"
                and value["kind"] == "ConfigMap"
                and metadata["name"] == self.name
                and metadata["namespace"] == self.kube.target.namespace
                and metadata["labels"] == self.labels
                and metadata["ownerReferences"] == self.owner
                and annotations["plane-demo/cluster-uid"] == self.kube.target.cluster_uid
                and annotations["plane-demo/namespace-uid"] == self.kube.target.namespace_uid
                and UID.fullmatch(metadata["uid"])
                and isinstance(metadata["resourceVersion"], str)
                and 0 < len(metadata["resourceVersion"]) <= 128
                and not metadata.get("deletionTimestamp")
                and set(data) == {"record.json"}
                and not value.get("binaryData"),
                "journal_ownership_mismatch",
            )
            require(
                annotations.get("plane-demo/journal-uid") == metadata["uid"]
                or (unsealed and "plane-demo/journal-uid" not in annotations),
                "journal_uid_changed_or_unsealed",
            )
            updated = datetime.fromisoformat(annotations["plane-demo/updated-at"])
            require(updated.tzinfo is not None, "journal_timestamp_invalid")
            raw = data["record.json"]
            require(
                isinstance(raw, str) and len(raw.encode()) <= 750_000, "journal_record_too_large"
            )
            record = json.loads(raw)
            require(
                isinstance(record, dict)
                and canonical_json(record) == raw
                and record.get("version") == 1
                and record.get("project") == self.kube.target.project
                and record.get("environment")
                == self.kube.target.ownership["plane-demo/environment"]
                and isinstance(record.get("run_id"), str)
                and re.fullmatch(
                    r"[a-f0-9]{12}" if self.kind == "fault" else r"[a-f0-9]{32}", record["run_id"]
                )
                and hashlib.sha256(raw.encode()).hexdigest()
                == annotations["plane-demo/record-sha256"]
                and intent_fingerprint(record, self.kind)
                == annotations["plane-demo/intent-sha256"],
                "journal_record_fingerprint_mismatch",
            )
            if self.kind == "fault":
                require(
                    record.get("slot") == self.kube.target.slot
                    and record.get("cluster_uid") == self.kube.target.cluster_uid
                    and record.get("namespace_uid") == self.kube.target.namespace_uid
                    and self.name == "plane-demo-fault-" + record.get("component", ""),
                    "journal_fault_target_mismatch",
                )
            require(self.uid is None or metadata["uid"] == self.uid, "journal_uid_changed")
            return record
        except (KeyError, TypeError, ValueError):
            raise AcceptanceError("journal_record_invalid") from None

    def _remember(self, value, *, unsealed=False):
        self.record = self._decode(value, unsealed=unsealed)
        metadata = value["metadata"]
        self.uid, self.version = metadata["uid"], metadata["resourceVersion"]
        self.snapshot = canonical_json(value)
        self.intent = metadata["annotations"]["plane-demo/intent-sha256"]
        self.claimed_by = metadata["annotations"].get("plane-demo/continued-by")
        self.updated_at = metadata["annotations"]["plane-demo/updated-at"]
        self.sealed = metadata["annotations"].get("plane-demo/journal-uid") == self.uid

    def load(self, reference=None, *, allow_unsealed_pre_mutation=False):
        self.kube.verify_scope()
        value = self.kube.optional("configmap", self.name)
        require(value is not None, "journal_not_found")
        self._remember(value, unsealed=allow_unsealed_pre_mutation)
        if not self.sealed:
            self.require_unsealed_pre_mutation()
        if reference is not None:
            pieces = str(reference).split("@")
            require(
                1 <= len(pieces) <= 3
                and pieces[0] == self.name
                and (len(pieces) < 2 or pieces[1] == self.uid)
                and (len(pieces) < 3 or pieces[2] == self.record["run_id"]),
                "journal_reference_changed",
            )
        return self.record

    def require_unsealed_pre_mutation(self):
        require(
            self.kind == "fault"
            and not self.sealed
            and self.record.get("outcome") == "preparing"
            and self.record.get("creation_attempted") is False
            and self.record.get("restored") is False
            and not self.record.get("physical_restored")
            and not any(
                key in self.record
                for key in (
                    "policy",
                    "rule",
                    "policy_uid",
                    "blocked_at",
                    "restored_at",
                    "restoration_started_at",
                    "physical_restored_at",
                )
            ),
            "unsealed_journal_not_pre_mutation",
        )

    def check(self, *, allow_unsealed_pre_mutation=False):
        require(self.snapshot is not None, "journal_not_committed")
        self.kube.verify_scope()
        current = self.kube.optional("configmap", self.name)
        require(current is not None, "journal_disappeared")
        self._decode(current, unsealed=allow_unsealed_pre_mutation)
        if not self.sealed:
            require(allow_unsealed_pre_mutation, "journal_not_committed")
            self.require_unsealed_pre_mutation()
        require(canonical_json(current) == self.snapshot, "journal_changed")
        require(self.claimed_by is None, "journal_already_continued")

    def _object(self, record):
        raw = canonical_json(record)
        require(len(raw.encode()) <= 750_000, "journal_record_too_large")
        target = self.kube.target
        annotations = {
            "plane-demo/cluster-uid": target.cluster_uid,
            "plane-demo/namespace-uid": target.namespace_uid,
            "plane-demo/record-sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "plane-demo/intent-sha256": intent_fingerprint(record, self.kind),
            "plane-demo/updated-at": utc_now(),
        }
        metadata = {
            "name": self.name,
            "namespace": target.namespace,
            "labels": self.labels,
            "ownerReferences": self.owner,
            "annotations": annotations,
        }
        if self.uid is not None:
            metadata.update(uid=self.uid, resourceVersion=self.version)
            annotations["plane-demo/journal-uid"] = self.uid
        if self.claimed_by:
            annotations["plane-demo/continued-by"] = self.claimed_by
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "data": {"record.json": raw},
        }

    def _submit(self, record, verb, *, unsealed=False):
        intended = self._object(record)
        try:
            actual = json.loads(
                self.kube.run(verb, "-f", "-", "-o", "json", payload=canonical_json(intended))
            )
        except ValueError:
            raise AcceptanceError("journal_write_response_invalid") from None
        self._decode(actual, unsealed=unsealed)
        require(
            actual["data"] == intended["data"]
            and actual["metadata"]["annotations"] == intended["metadata"]["annotations"],
            "journal_write_content_mismatch",
        )
        self._remember(actual, unsealed=unsealed)

    def start(self, record, *, reuse_restored=False):
        self.kube.verify_scope()
        existing = self.kube.optional("configmap", self.name)
        if existing is not None:
            self._remember(existing)
            require(
                self.kind == "fault"
                and reuse_restored
                and self.record.get("restored") is True
                and self.record.get("physical_restored") is True,
                "journal_exists_restore_required",
            )
            self.check()
            self._submit(record, "replace")
        else:
            # No fault mutation is permitted until this UID seal has been acknowledged.
            self._submit(record, "create", unsealed=True)
            self._submit(record, "replace")

    def save(self, record):
        self.check()
        require(intent_fingerprint(record, self.kind) == self.intent, "journal_intent_changed")
        self._submit(record, "replace")

    def cancel_unsealed(self, record):
        self.require_unsealed_pre_mutation()
        self.check(allow_unsealed_pre_mutation=True)
        require(
            record.get("outcome") == "cancelled_before_mutation"
            and record.get("creation_attempted") is False
            and record.get("restored") is True
            and record.get("physical_restored") is True
            and intent_fingerprint(record, self.kind) == self.intent,
            "unsealed_journal_cancellation_invalid",
        )
        self._submit(record, "replace")

    def commit_fault(self, record):
        self.check()
        require(
            self.kind == "fault"
            and self.record.get("creation_attempted") is False
            and not self.record.get("restored")
            and record.get("creation_attempted") is True
            and intent_fingerprint({**record, "policy": {}, "rule": None}, self.kind)
            == self.intent,
            "journal_fault_prepare_changed",
        )
        self._submit(record, "replace")

    def claim_continuation(self, run_id):
        require(
            self.kind == "acceptance" and re.fullmatch(r"[a-f0-9]{32}", run_id),
            "journal_claim_invalid",
        )
        self.check()
        self.claimed_by = run_id
        self._submit(self.record, "replace")


def verify_image_mapping(running, expected, architecture, *, inspect, content, check=require):
    reported = inspect(running).get("status", {}).get("id")
    check(
        isinstance(reported, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", reported),
        "local_running_image_mapping_mismatch",
    )
    if reported != expected:
        manifest = content(reported)
        if manifest.get("mediaType") in {
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        }:
            check(
                manifest.get("schemaVersion") == 2 and isinstance(manifest.get("manifests"), list),
                "local_running_image_mapping_mismatch",
            )
            native = [
                item
                for item in manifest["manifests"]
                if item.get("platform", {}).get("os") == "linux"
                and item.get("platform", {}).get("architecture") == architecture
            ]
            check(len(native) == 1, "local_native_image_manifest_ambiguous")
            manifest = content(native[0].get("digest"))
        check(
            manifest.get("schemaVersion") == 2
            and manifest.get("mediaType")
            in {
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            }
            and manifest.get("config", {}).get("digest") == expected,
            "local_running_image_mapping_mismatch",
        )
        content(expected)
    return {"running_image_id": running, "image_id": expected}


PROBE_CODE = r"""
import ipaddress,json,os,socket,sys
import psycopg
from psycopg.conninfo import conninfo_to_dict
name,expected_host,expected_port=sys.argv[1:]
dsn=os.environ.get(name,"")
parameters=conninfo_to_dict(dsn)
host=parameters.get("host","")
port=int(parameters.get("port","5432"))
if host.rstrip(".").lower()!=expected_host.rstrip(".").lower() or port!=int(expected_port):
    print(json.dumps({"error":"parent_dsn_mismatch"}),flush=True); sys.exit(2)
if parameters.get("hostaddr") or parameters.get("service") or "," in host:
    print(json.dumps({"error":"indirect_parent_dsn_refused"}),flush=True); sys.exit(2)
connection=None
def addresses():
    return sorted({str(ipaddress.ip_address(item[4][0]))
                   for item in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)})
def opened(value):
    return psycopg.connect(value,autocommit=True,connect_timeout=3,tcp_user_timeout="3000",
                          keepalives_idle="1",keepalives_interval="1",keepalives_count="2",
                          options="-c statement_timeout=3000 -c lock_timeout=3000")
def checked(existing=False):
    global connection
    try:
        if existing:
            if connection is None:
                return {"ok":False,"network_failure":False,"error":"baseline_required"}
            connection.execute("SELECT 1").fetchone()
        else:
            with opened(dsn) as fresh: fresh.execute("SELECT 1").fetchone()
        return {"ok":True}
    except psycopg.Error as error:
        state=error.sqlstate
        network=state is None or state.startswith("08") or state=="57014"
        return {"ok":False,"network_failure":network,"sqlstate":state}
try:
    for line in sys.stdin:
        action=json.loads(line)["action"]
        ips=addresses()
        if action=="baseline":
            connection=opened(dsn)
            connection.execute("SELECT 1").fetchone()
            result={"ok":True}
        elif action=="fresh": result=checked()
        elif action=="existing": result=checked(existing=True)
        elif action=="local":
            if name=="MANAGEMENT_DSN":
                with opened(os.environ["CONTROL_DSN"]) as local:
                    local.execute("SELECT 1").fetchone()
            else:
                from kubernetes import client,config
                config.load_incluster_config()
                client.VersionApi().get_code(_request_timeout=(3,3))
            result={"ok":True}
        else: result={"error":"unsupported_probe_action"}
        print(json.dumps({**result,"ips":ips}),flush=True)
except Exception:
    print(json.dumps({"error":"pod_probe_failed"}),flush=True)
finally:
    if connection is not None: connection.close()
"""


class Probe:
    def __init__(self, kube: Kubectl, component: str, pod: dict):
        parent = kube.target.parent
        args = kube.argv(
            "exec",
            "-i",
            pod["metadata"]["name"],
            "-c",
            kube.target.component(component)["container"],
            "--",
            "python",
            "-u",
            "-c",
            PROBE_CODE,
            COMPONENT_DSN[component],
            parent["host"],
            str(parent["port"]),
            streaming=True,
        )
        try:
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=getattr(kube, "environment", None),
            )
        except OSError:
            raise AcceptanceError("pod_probe_start_failed") from None
        self.buffer = b""

    def request(self, action: str) -> dict:
        try:
            self.process.stdin.write(json.dumps({"action": action}).encode() + b"\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + 20
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                while b"\n" not in self.buffer:
                    remaining = deadline - time.monotonic()
                    require(remaining > 0 and bool(selector.select(remaining)), "pod_probe_timeout")
                    data = os.read(self.process.stdout.fileno(), 4096)
                    require(bool(data), "pod_probe_ended")
                    self.buffer += data
                    require(len(self.buffer) <= 8192, "pod_probe_response_too_large")
            line, self.buffer = self.buffer.split(b"\n", 1)
            value = json.loads(line)
            require(isinstance(value, dict) and "error" not in value, "pod_probe_failed")
            return value
        except (OSError, ValueError):
            raise AcceptanceError("pod_probe_io_failed") from None

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()


def parent_cidrs(target: Target, addresses: list[str]) -> list[str]:
    parent = target.parent
    require(
        isinstance(parent.get("host"), str) and bool(parent["host"]),
        "expected_parent_host_required",
    )
    require(
        isinstance(parent.get("port"), int) and 1 <= parent["port"] <= 65535, "invalid_parent_port"
    )
    try:
        allowed = [
            ipaddress.ip_network(value, strict=True) for value in parent.get("allowed_cidrs", [])
        ]
        resolved = sorted({ipaddress.ip_address(value) for value in addresses}, key=str)
    except ValueError:
        raise AcceptanceError("invalid_parent_network") from None
    require(bool(allowed) and 0 < len(resolved) <= 8, "parent_address_count_or_scope")
    for address in resolved:
        require(
            address.is_private
            and not (
                address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
            ),
            "parent_must_be_private_unicast",
        )
        require(any(address in network for network in allowed), "parent_address_outside_allocation")
    return [f"{address}/{address.max_prefixlen}" for address in resolved]


def deny_policy(target: Target, component: str, cidrs: list[str], run_id: str) -> dict:
    require(component in COMPONENT_DSN, "unsupported_fault_component")
    require(
        target.slot.endswith("-control" if component == "control-reconciler" else "-data"),
        "fault_component_slot_mismatch",
    )
    require(bool(re.fullmatch(r"[a-f0-9]{12}", run_id)), "invalid_fault_run_id")
    try:
        networks = [ipaddress.ip_network(value, strict=True) for value in cidrs]
    except ValueError:
        raise AcceptanceError("invalid_fault_cidr") from None
    require(
        bool(networks)
        and all(
            network.prefixlen == network.max_prefixlen
            and network.network_address.is_private
            and not network.network_address.is_loopback
            and not network.network_address.is_link_local
            for network in networks
        ),
        "fault_must_target_exact_private_addresses",
    )
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {
            "name": f"plane-demo-fault-{run_id}",
            "namespace": target.namespace,
            "labels": {
                **target.ownership,
                "plane-demo/project": target.project,
                "plane-demo/fault-run": run_id,
            },
        },
        "spec": {
            "endpointSelector": {
                "matchLabels": {
                    **target.ownership,
                    "plane-demo/project": target.project,
                    "plane-demo/component": component,
                }
            },
            "enableDefaultDeny": {"ingress": False, "egress": False},
            "egressDeny": [
                {
                    "toCIDR": cidrs,
                    "toPorts": [
                        {"ports": [{"port": str(target.parent["port"]), "protocol": "TCP"}]}
                    ],
                }
            ],
        },
    }


class ParentFault:
    environment = "azure"

    def __init__(
        self,
        configuration: Configuration,
        slot: str,
        component: str,
        evidence: Path | None = None,
        *,
        kube_factory=Kubectl,
        probe_factory=Probe,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        require(configuration.environment == self.environment, "fault_environment_mismatch")
        require(component in COMPONENT_DSN, "unsupported_fault_component")
        self.configuration = configuration
        self.target = (
            configuration.fault_target(slot, component)
            if configuration.live
            else configuration.target(slot)
        )
        self.component = component
        self.evidence_path = evidence
        self.kube = (
            configuration.kube(self.target)
            if configuration.live and kube_factory is Kubectl
            else kube_factory(self.target)
        )
        self.journal = (
            ConfigMapJournal(self.kube, "plane-demo-fault-" + component, "fault")
            if configuration.live
            else None
        )
        self.probe_factory = probe_factory
        self.clock, self.sleep = clock, sleep
        self.run_id = uuid4().hex[:12]
        self.record = {
            "version": 1,
            "run_id": self.run_id,
            "project": self.target.project,
            "environment": self.environment,
            "slot": slot,
            "component": component,
            "started_at": utc_now(),
            "outcome": "not_activated",
            "restored": False,
            "probes": [],
            "cluster_uid": self.target.cluster_uid,
            "namespace_uid": self.target.namespace_uid,
            "parent": self.target.parent,
        }
        self.probe = None
        self.created = False
        self.creation_attempted = False
        self.original = None
        self.policy = None
        self.pod_uid = None
        self.addresses = None
        self.recovery_started = None
        self.recovery_deadline = None

    def _save(self):
        if self.journal is None:
            protected_write(self.evidence_path, self.record)
        elif self.journal.uid is not None:
            self.journal.save(self.record)
        elif self.record.get("creation_attempted"):
            self.journal.start(self.record, reuse_restored=True)

    def check_journal(self):
        if self.journal is not None and self.creation_attempted:
            self.journal.check()

    def _check(self, action: str) -> dict:
        self.check_journal()
        value = self.probe.request(action)
        current = sorted(value.get("ips", []))
        require(current == self.addresses, "parent_dns_changed_during_fault")
        self.record["probes"].append({"action": action, "received_at": utc_now(), **value})
        self._save()
        return value

    def prepare_fault(self, pod):
        crd = self.kube.json("get", "customresourcedefinition", POLICY_RESOURCE)
        require(
            any(
                item.get("name") == "v2" and item.get("served") is True
                for item in crd.get("spec", {}).get("versions", [])
            )
            and any(
                item.get("type") == "Established" and item.get("status") == "True"
                for item in crd.get("status", {}).get("conditions", [])
            ),
            "cilium_policy_crd_not_ready",
        )
        self.original = self.kube.policies()
        if self.configuration.live:
            require(
                not any(item["name"].startswith("plane-demo-fault-") for item in self.original),
                "unrestored_parent_fault_present",
            )
        self.record["original_policies"] = self.original

    def plan_fault(self, cidrs):
        self.policy = deny_policy(self.target, self.component, cidrs, self.run_id)
        if self.configuration.live:
            self.policy["metadata"]["labels"]["plane-demo/journal"] = self.journal.name
            self.policy["metadata"]["annotations"] = {
                "plane-demo/journal-uid": self.journal.uid,
                "plane-demo/rule-sha256": hashlib.sha256(
                    canonical_json(self.policy["spec"]).encode()
                ).hexdigest(),
            }
        self.record["policy"] = self.policy

    def create_fault(self):
        self.check_journal()
        # create, never apply: an existing object must never be overwritten.
        self.kube.run("create", "-f", "-", payload=json.dumps(self.policy))
        self.created = True
        created = self.kube.json("get", POLICY_RESOURCE, self.policy["metadata"]["name"])
        self.record["policy_uid"] = created["metadata"]["uid"]

    def remove_fault(self):
        self.check_journal()
        self.kube.verify_scope()
        name = self.policy["metadata"]["name"]
        current = self.kube.optional(POLICY_RESOURCE, name)
        require(self.kube.policies(exclude=name) == self.original, "original_policies_changed")
        if current:
            require(
                current["metadata"].get("labels", {}).get("plane-demo/fault-run") == self.run_id
                and current.get("spec") == self.policy["spec"],
                "fault_policy_ownership_changed",
            )
            if self.configuration.live:
                require(
                    current["metadata"].get("namespace") == self.target.namespace
                    and all(
                        current["metadata"].get("labels", {}).get(key) == value
                        for key, value in self.policy["metadata"]["labels"].items()
                    )
                    and current["metadata"].get("annotations", {}).get("plane-demo/rule-sha256")
                    == self.policy["metadata"]["annotations"]["plane-demo/rule-sha256"]
                    and current["metadata"].get("annotations", {}).get("plane-demo/journal-uid")
                    == self.journal.uid,
                    "fault_policy_journal_binding_changed",
                )
            if self.record.get("policy_uid"):
                require(
                    current["metadata"]["uid"] == self.record["policy_uid"],
                    "fault_policy_uid_changed",
                )
            self.kube.delete_uid(
                "ciliumnetworkpolicies",
                name,
                current["metadata"]["uid"],
                group="cilium.io/v2",
            )
            while self.kube.optional(POLICY_RESOURCE, name) is not None:
                require(self.clock() < self.recovery_deadline, "fault_policy_not_deleted")
                self.sleep(1)
        require(self.kube.policies() == self.original, "original_policies_changed")
        self.record["restored_policies"] = self.original

    def activate(self):
        self.kube.verify_scope()
        pod = self.kube.pod(self.component)
        self.pod_uid = pod["metadata"]["uid"]
        self.record["pod_uid"] = self.pod_uid
        self.record["pod_images"] = [
            {"name": item["name"], "image_id": item.get("imageID")}
            for item in pod.get("status", {}).get("containerStatuses", [])
        ]
        self.prepare_fault(pod)
        self.probe = self.probe_factory(self.kube, self.component, pod)
        baseline = self.probe.request("baseline")
        require(baseline.get("ok") is True, "parent_baseline_not_healthy")
        self.addresses = sorted(baseline.get("ips", []))
        cidrs = parent_cidrs(self.target, self.addresses)
        require(self.probe.request("local").get("ok") is True, "local_baseline_not_healthy")
        self.record["baseline"] = baseline
        if self.journal is not None:
            self.record["outcome"] = "preparing"
            self.record["creation_attempted"] = False
            self.journal.start(self.record, reuse_restored=True)
        self.plan_fault(cidrs)
        self.record["outcome"] = "activating"
        self.record["creation_attempted"] = True
        if self.journal is not None:
            self.journal.commit_fault(self.record)
        else:
            self._save()
        self.creation_attempted = True
        self.create_fault()
        self._save()
        deadline = self.clock() + 30
        while True:
            fresh = self._check("fresh")
            if fresh.get("ok") is False and fresh.get("network_failure") is True:
                break
            require(self.clock() < deadline, "parent_fault_not_enforced")
            self.sleep(1)
        existing = self._check("existing")
        require(
            existing.get("ok") is False and existing.get("network_failure") is True,
            "existing_connection_not_blocked",
        )
        require(self._check("local").get("ok") is True, "fault_blocked_local_prerequisite")
        self.record["blocked_at"] = utc_now()
        self.record["outcome"] = "blocked_verified"
        self._save()
        return self

    def assert_blocked(self):
        self.check_journal()
        require(
            self.kube.pod(self.component)["metadata"]["uid"] == self.pod_uid,
            "reconciler_replaced_during_fault",
        )
        fresh = self._check("fresh")
        require(
            fresh.get("ok") is False and fresh.get("network_failure") is True,
            "parent_link_no_longer_blocked",
        )

    def restore(self):
        if self.journal is not None and self.journal.uid is not None and not self.journal.sealed:
            self.cancel_unsealed()
            return
        restoration_error = None
        self.record["restored"] = False
        self.record["physical_restored"] = False
        self.record.pop("restored_at", None)
        self.record.pop("physical_restored_at", None)
        self.record.pop("restoration_error", None)
        try:
            if self.creation_attempted or (
                self.journal is not None and self.journal.uid is not None
            ):
                self.recovery_started = self.clock()
                self.recovery_deadline = self.recovery_started + RECOVERY_SECONDS
                self.record["restoration_started_at"] = utc_now()
                self.record["recovery_started_monotonic"] = self.recovery_started
                self.record["recovery_deadline_monotonic"] = self.recovery_deadline
                if self.creation_attempted:
                    self.remove_fault()
                else:
                    self.journal.check()
                    self.verify_unattempted()
                if self.probe:
                    self.probe.close()
                self.probe = self.probe_factory(
                    self.kube, self.component, self.kube.pod(self.component)
                )
                while True:
                    require(self.clock() <= self.recovery_deadline, "parent_link_not_restored")
                    result = self.probe.request("fresh")
                    require(self.clock() <= self.recovery_deadline, "parent_link_not_restored")
                    if result.get("ok") is True:
                        parent_cidrs(self.target, result.get("ips", []))
                        break
                    require(self.clock() < self.recovery_deadline, "parent_link_not_restored")
                    self.sleep(1)
                self.record["restoration_probe"] = result
                self.record["physical_restored"] = True
                self.record["physical_restored_at"] = utc_now()
        except Exception as error:
            restoration_error = error
        finally:
            try:
                if self.probe:
                    self.probe.close()
            except Exception as error:
                restoration_error = restoration_error or error
            finally:
                self.probe = None
            if (
                restoration_error is None
                and self.recovery_deadline is not None
                and self.clock() > self.recovery_deadline
            ):
                restoration_error = AcceptanceError("restoration_cleanup_deadline_exceeded")
            if restoration_error is None and self.record["physical_restored"]:
                self.record["restored"] = True
                self.record["restored_at"] = utc_now()
            if restoration_error:
                self.record["outcome"] = "restoration_failed"
                self.record["restoration_error"] = (
                    str(restoration_error)
                    if isinstance(restoration_error, AcceptanceError)
                    else "restoration_failed"
                )
            try:
                self._save()
            except AcceptanceError as error:
                restoration_error = restoration_error or error
                self.record["restored"] = False
                self.record.pop("restored_at", None)
                self.record["outcome"] = "restoration_failed"
                self.record["restoration_error"] = str(error)
            if (
                restoration_error is None
                and self.recovery_deadline is not None
                and self.clock() > self.recovery_deadline
            ):
                restoration_error = AcceptanceError("restoration_cleanup_deadline_exceeded")
                self.record["restored"] = False
                self.record.pop("restored_at", None)
                self.record["outcome"] = "restoration_failed"
                self.record["restoration_error"] = str(restoration_error)
                self._save()
        if restoration_error:
            raise AcceptanceError("fault_restoration_failed") from restoration_error

    def verify_unattempted(self):
        self.kube.verify_scope()
        require(
            self.kube.policies(reject_faults=True) == self.original, "original_policies_changed"
        )
        self.record["restored_policies"] = self.original

    def cancel_unsealed(self):
        self.journal.require_unsealed_pre_mutation()
        self.journal.check(allow_unsealed_pre_mutation=True)
        require(self.creation_attempted is False, "unsealed_journal_not_pre_mutation")
        self.verify_unattempted()
        cancelled_at = utc_now()
        record = {
            **self.record,
            "outcome": "cancelled_before_mutation",
            "restored": True,
            "physical_restored": True,
            "restored_at": cancelled_at,
            "physical_restored_at": cancelled_at,
        }
        self.journal.cancel_unsealed(record)
        self.record = record

    def __enter__(self):
        try:
            return self.activate()
        except BaseException as error:
            self.record["outcome"] = "activation_failed"
            self.record["error"] = (
                str(error) if isinstance(error, AcceptanceError) else "fault_activation_failed"
            )
            self.restore()
            raise

    def __exit__(self, exception_type, _exception, _traceback):
        self.record["outcome"] = "failed" if exception_type else "verified_and_restored"
        self.restore()

    @classmethod
    def from_evidence(cls, configuration, path: Path, **kwargs):
        require(not configuration.live, "live_restore_requires_journal_reference")
        path = state_path(path)
        require(path.is_relative_to(configuration.root / "evidence"), "restore_evidence_scope")
        record = read_json(path)
        require(record.get("version") == 1, "unsupported_fault_evidence_version")
        fault = cls(configuration, record["slot"], record["component"], path, **kwargs)
        fault.restore_record(record)
        return fault

    @classmethod
    def from_journal(cls, configuration, slot, component, reference=None, **kwargs):
        require(configuration.live, "live_configuration_required")
        fault = cls(configuration, slot, component, **kwargs)
        record = fault.journal.load(reference, allow_unsealed_pre_mutation=True)
        fault.restore_record(record)
        return fault

    def restore_record(self, record):
        require(
            record.get("project") == self.target.project
            and record.get("cluster_uid") == self.target.cluster_uid
            and record.get("namespace_uid") == self.target.namespace_uid,
            "restore_target_identity_changed",
        )
        if self.configuration.live:
            require(
                record.get("parent") == self.target.parent
                and record.get("environment") == self.environment,
                "restore_parent_binding_changed",
            )
        self.run_id = record["run_id"]
        self.plan_fault(parent_cidrs(self.target, record["baseline"]["ips"]))
        attempted = record.get("creation_attempted")
        require(
            type(attempted) is bool
            and (record.get("policy") == self.policy if attempted else "policy" not in record),
            "restore_policy_contract_mismatch",
        )
        self.record = record
        self.original = record["original_policies"]
        self.pod_uid = record["pod_uid"]
        self.creation_attempted = attempted


def fault_class(configuration):
    if configuration.environment == "azure":
        return ParentFault
    name = "plane_demo_local_fault_" + __name__
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).parent / "local/fault-parent-link.py"
        )
        module = importlib.util.module_from_spec(spec)
        module.base = sys.modules[__name__]
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].LocalParentFault


@contextmanager
def interruption_is_failure():
    def interrupted(_signal, _frame):
        raise AcceptanceError("operator_interrupted")

    previous = {
        value: signal.signal(value, interrupted) for value in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for value, handler in previous.items():
            signal.signal(value, handler)


def main(argv=None, *, configuration_factory=LiveConfiguration) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".env", help="Checkout .env only")
    parser.add_argument("--slot")
    parser.add_argument("--component", choices=tuple(COMPONENT_DSN))
    parser.add_argument(
        "--restore",
        type=Path,
        nargs="?",
        const=Path("active"),
        help="Restore the slot/component's owned journal, optionally NAME@UID@RUN_ID",
    )
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    configuration = None
    try:
        require(60 <= args.duration <= 600, "fault_duration_must_be_60_to_600_seconds")
        configuration = configuration_factory(args.config)
        selected_fault = fault_class(configuration)
        if args.restore:
            with interruption_is_failure():
                if configuration.live:
                    require(bool(args.slot) and bool(args.component), "slot_and_component_required")
                    fault = selected_fault.from_journal(
                        configuration,
                        args.slot,
                        args.component,
                        None if str(args.restore) == "active" else str(args.restore),
                    )
                    if fault.journal.sealed:
                        fault.record["outcome"] = "restored_only"
                else:
                    fault = selected_fault.from_evidence(configuration, args.restore)
                fault.restore()
            print(
                json.dumps(
                    {
                        "outcome": (
                            "cancelled_before_mutation"
                            if configuration.live
                            and fault.record.get("outcome") == "cancelled_before_mutation"
                            else "restored_only_not_acceptance"
                        ),
                        **(
                            {"journal": fault.journal.reference}
                            if configuration.live
                            else {"evidence": str(args.restore)}
                        ),
                    }
                )
            )
            return 0
        require(bool(args.slot) and bool(args.component), "slot_and_component_required")
        evidence = configuration.root / "evidence" / f"fault-{uuid4().hex}.json"
        with interruption_is_failure():
            fault = selected_fault(configuration, args.slot, args.component, evidence)
            fault.record["source"] = source_metadata()
            if configuration.environment == "local":
                require(
                    fault.record["source"]["worktree_dirty"] is False,
                    "local_fault_source_worktree_dirty",
                )
            with fault:
                deadline = time.monotonic() + args.duration
                while time.monotonic() < deadline:
                    fault.assert_blocked()
                    time.sleep(min(5, max(0, deadline - time.monotonic())))
        print(
            json.dumps(
                {
                    "outcome": "fault_verified_and_restored",
                    **(
                        {"journal": fault.journal.reference}
                        if configuration.live
                        else {"evidence": str(evidence)}
                    ),
                }
            )
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "outcome": "failed",
                    "error": str(error)
                    if isinstance(error, AcceptanceError)
                    else "parent_fault_failed",
                }
            )
        )
        return 1
    finally:
        if configuration is not None and configuration.live:
            configuration.close()


if __name__ == "__main__":
    raise SystemExit(main())
