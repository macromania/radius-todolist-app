#!/usr/bin/env python3
"""Read-only operator export of API keys, acceptance metadata, and cleanup access."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hmac
import ipaddress
import json
import os
import re
import shutil
import signal
import ssl
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "radplanes"
MANAGED_BY = "radius-todolist-app"
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
TENANT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
KEY = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
HOST = re.compile(r"[a-z0-9.-]+\.postgres\.database\.azure\.com\Z")
SHOWCASE = {"shared_a": "shared-a", "shared_b": "shared-b", "isolated": "isolated-c"}
CLEANUP_GENERATOR = "harness/export-state.py"


def component_names(role):
    return (
        ["management-api", "provisioner"]
        if role == "management"
        else [
            role + "-api",
            role + "-reconciler",
        ]
    )


INVENTORY_PROBE = r"""
import json,os,sys
import psycopg
from psycopg.rows import dict_row
try:
    with psycopg.connect(os.environ["MANAGEMENT_DSN"],connect_timeout=5,
                         options="-c statement_timeout=5000",row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        pairs=connection.execute(
            "SELECT pair_id,stage,control_cluster_id,data_cluster_id,control_url,data_url "
            "FROM management.pairs ORDER BY pair_id").fetchall()
        tenants=connection.execute(
            "SELECT t.tenant_id,t.pair_id,t.isolation,EXISTS("
            "SELECT 1 FROM management.events e WHERE e.tenant_id=t.tenant_id "
            "AND e.onboarding_id=t.onboarding_id AND e.version=t.desired_revision "
            "AND e.type='control_record_created' AND e.source='control') AS ready "
            "FROM management.tenants t WHERE tenant_id=ANY(%s)",
            (json.loads(sys.argv[1]),)).fetchall()
    print(json.dumps({"pairs":pairs,"tenants":tenants}))
except Exception:
    print(json.dumps({"error":"management_inventory_probe_failed"}))
    sys.exit(1)
"""

PARENT_PROBE = r"""
import json,os,sys
from psycopg.conninfo import conninfo_to_dict
try:
    values=conninfo_to_dict(os.environ[sys.argv[1]])
    if values.get("hostaddr") or values.get("service"):
        raise ValueError()
    print(json.dumps({"host":values["host"],"port":int(values.get("port","5432"))}))
except Exception:
    print(json.dumps({"error":"parent_connection_metadata_failed"}))
    sys.exit(1)
"""


class ExportError(RuntimeError):
    pass


class Pending(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise ExportError(code)


def now():
    return datetime.now(UTC).isoformat()


def read_json(path: Path):
    path = (ROOT / path).resolve()
    require(path.is_relative_to(ROOT / ".state"), "state_path_escape")
    require(path.is_file() and path.stat().st_size <= 2_000_000, "invalid_state_json_file")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ExportError("state_json_unreadable") from None
    require(isinstance(value, dict), "state_json_must_be_object")
    return value


def private_write(path: Path, content: str):
    path = ROOT / path
    require((ROOT / ".state").resolve().is_relative_to(ROOT), "project_state_symlink_escape")
    require(path.resolve().is_relative_to(ROOT / ".state"), "state_path_escape")
    require(not path.is_symlink(), "state_symlink_refused")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_name(path.name + "." + uuid4().hex + ".writing")
    try:
        descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
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


def write_json(path: Path, value):
    private_write(path, json.dumps(value, sort_keys=True, indent=2) + "\n")


def scoped_file(root: Path, value: str, *, private=False):
    root = (ROOT / root).resolve()
    require(root.is_relative_to(ROOT / ".state"), "state_path_escape")
    require(isinstance(value, str) and bool(value), "missing_state_filename")
    require(not Path(value).is_absolute(), "state_filename_must_be_relative")
    path = (root / value).resolve()
    require(path.is_relative_to(root), "state_path_escape")
    require(path.is_file(), "state_file_missing")
    if private:
        require(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "state_file_not_private")
    return path


def gateway_url(value: str):
    require(isinstance(value, str), "gateway_url_missing")
    parsed = urlsplit(value)
    require(
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.hostname.endswith(".cloudapp.azure.com")
        and parsed.port in (None, 443)
        and parsed.path in ("", "/")
        and not (parsed.username or parsed.password or parsed.query or parsed.fragment),
        "invalid_gateway_https_url",
    )
    return value.rstrip("/")


def healthy_gateway(url: str):
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url + "/healthz") as response:
                if response.status_code in {429, 502, 503, 504}:
                    raise Pending("gateway_backend_not_ready")
                require(response.status_code == 200, "gateway_health_contract_failed")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    require(len(body) <= 4096, "gateway_health_response_too_large")
                require(json.loads(body) == {"status": "ok"}, "gateway_health_contract_failed")
    except httpx.HTTPError as error:
        cause = error
        while cause:
            if isinstance(cause, ssl.SSLError):
                raise ExportError("gateway_tls_verification_failed") from None
            cause = cause.__cause__
        raise Pending("gateway_not_reachable") from None


@dataclass
class Access:
    slot: str
    context: str
    namespace: str
    kubeconfig: Path
    cluster_id: str
    cluster_uid: str


@dataclass
class Plane:
    target: dict
    url: str
    demo_key: str = field(repr=False)
    access: Access
    api_pod: str
    api_container: str


class Exporter:
    def __init__(
        self,
        config: Path,
        *,
        execute=None,
        health=healthy_gateway,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.config_path = (ROOT / config).resolve()
        self.state = self.config_path.parent
        require(self.state.is_relative_to(ROOT / ".state"), "config_must_be_in_project_state")
        self.cleanup_state = ROOT / ".state/azure"
        require(
            self.state.is_relative_to(self.cleanup_state), "cleanup_config_must_be_in_azure_state"
        )
        self.config = read_json(self.config_path)
        self.foundation = self.config.get("foundation", {})
        require(
            self.config.get("version") == 1
            and self.foundation.get("projectName") == PROJECT
            and self.foundation.get("location") == "centralus",
            "invalid_project_config",
        )
        self.subscription = self.foundation.get("subscriptionId", "")
        UUID(self.subscription)
        self.allocations = self.config.get("allocations", {})
        require(
            isinstance(self.allocations, dict) and "management" in self.allocations,
            "missing_allocations",
        )
        self.validate_allocations()
        registry = self.foundation.get("registryLoginServer", "")
        require(
            isinstance(registry, str) and registry.endswith(".azurecr.io"),
            "project_registry_missing",
        )
        self.images = {}
        for role in ("api", "provisioner"):
            image = self.config.get("images", {}).get(role)
            if isinstance(image, dict):
                image = image.get("reference")
            require(
                isinstance(image, str)
                and re.fullmatch(re.escape(registry) + r"/[a-z0-9/_-]+@sha256:[a-f0-9]{64}", image),
                "project_image_digest_missing",
            )
            self.images[role] = image
        self.execute = execute or subprocess.run
        self.health, self.clock, self.sleep = health, clock, sleep
        self.deadline = None
        self.accesses = {}
        self.work = self.state / ".export-state-work" / uuid4().hex
        self.previous = (
            read_json(self.state / "acceptance.json")
            if (self.state / "acceptance.json").exists()
            else None
        )
        if self.previous is not None:
            require(
                self.previous.get("version") == 1
                and self.previous.get("project") == PROJECT
                and self.previous.get("environment") == "azure",
                "existing_acceptance_identity_mismatch",
            )
        self.tenants = (
            self.previous.get("tenants", SHOWCASE) if self.previous is not None else SHOWCASE
        )
        require(
            set(self.tenants) == set(SHOWCASE)
            and len(set(self.tenants.values())) == 3
            and all(TENANT.fullmatch(value) for value in self.tenants.values()),
            "invalid_showcase_tenants",
        )
        endpoint_file = self.state / "endpoints.json"
        if self.previous is not None:
            endpoint_file = scoped_file(self.state, self.previous["endpoints_file"])
        self.endpoints = read_json(endpoint_file) if endpoint_file.exists() else {"pairs": {}}
        self.validate_existing_endpoints()
        self.targets = dict(self.previous.get("targets", {})) if self.previous else {}
        require(set(self.targets) <= set(self.allocations), "existing_target_outside_allocation")
        for slot, target in self.targets.items():
            role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
            require(
                target.get("context") == PROJECT + "-" + slot
                and target.get("namespace") == f"{PROJECT}-{slot}-{role}"
                and target.get("kubeconfig") == slot + ".kubeconfig",
                "existing_target_scope_mismatch",
            )
            UUID(target["cluster_uid"])
            UUID(target["namespace_uid"])
            scoped_file(self.state, target["kubeconfig"], private=True)
            names = set(component_names(role))
            existing = set(target.get("components", {}))
            require(
                existing == names or role == "management" and existing == {"management-api"},
                "existing_components_mismatch",
            )
            for value in target["components"].values():
                require(
                    set(value) == {"deployment", "container"}
                    and all(NAME.fullmatch(item) for item in value.values()),
                    "existing_component_identity_invalid",
                )
            if role != "management":
                parent = target.get("parent", {})
                parent_slot = "management" if role == "control" else slot[:-5] + "-control"
                require(
                    HOST.fullmatch(parent.get("host", ""))
                    and parent.get("port") == 5432
                    and parent.get("allowed_cidrs") == [self.expected_subnet(parent_slot)],
                    "existing_parent_scope_mismatch",
                )
        self.work.mkdir(parents=True, mode=0o700)

    def validate_allocations(self):
        prefix = f"/subscriptions/{self.subscription}/resourceGroups/"
        vnet = self.foundation.get("virtualNetworkId", "")
        require(
            vnet.startswith(prefix) and "/providers/Microsoft.Network/virtualNetworks/" in vnet,
            "vnet_outside_project_subscription",
        )
        for slot, item in self.allocations.items():
            require(
                NAME.fullmatch(slot)
                and item.get("slot") == slot
                and (slot == "management" or slot.endswith(("-control", "-data"))),
                "invalid_allocated_slot",
            )
            for kind in ("cluster", "app"):
                group = item.get(kind + "ResourceGroup", "")
                require(
                    NAME.fullmatch(group) and item.get(kind + "ResourceGroupId") == prefix + group,
                    "invalid_allocated_resource_group",
                )
            require(NAME.fullmatch(item.get("clusterName", "")), "invalid_allocated_cluster_name")
            require(
                item.get("postgresqlSubnetId") == vnet + "/subnets/snet-" + slot + "-postgresql",
                "postgres_subnet_outside_allocation",
            )
            self.expected_subnet(slot)
        pairs = {
            slot.removesuffix("-control") for slot in self.allocations if slot.endswith("-control")
        }
        require("shared" in pairs and len(pairs - {"shared"}) >= 1, "showcase_allocation_missing")
        require(
            set(self.allocations)
            == {"management"}
            | {pair + "-" + role for pair in pairs for role in ("control", "data")},
            "incomplete_pair_allocation",
        )
        management = self.config.get("managementCluster", {})
        require(
            management.get("id", "").lower() == self.cluster_id("management").lower(),
            "management_cluster_outside_allocation",
        )

    def expected_subnet(self, slot):
        item = self.allocations[slot]
        address = ipaddress.IPv4Address(item["apiPrivateIp"])
        octets = list(address.packed)
        require(
            octets[:2] == [10, 64] and octets[3] == 240 and octets[2] < 16,
            "invalid_allocated_role_index",
        )
        index = octets[2]
        require(
            item.get("challengePrivateIp") == f"10.64.{index}.241"
            and item.get("gatewaySubnetCidr") == f"10.64.{16 + index}.0/24",
            "inconsistent_role_network_allocation",
        )
        return f"10.64.{48 + index}.0/27"

    def cluster_id(self, slot):
        item = self.allocations[slot]
        return (
            item["clusterResourceGroupId"] + "/providers/Microsoft.ContainerService/"
            "managedClusters/" + item["clusterName"]
        )

    def validate_existing_endpoints(self):
        require(
            set(self.endpoints) <= {"management", "pairs"}
            and isinstance(self.endpoints.get("pairs", {}), dict),
            "invalid_existing_endpoints",
        )
        values = []
        if "management" in self.endpoints:
            values.append(("management", self.endpoints["management"]))
        for pair, roles in self.endpoints.get("pairs", {}).items():
            require(
                isinstance(roles, dict) and set(roles) <= {"control", "data"},
                "invalid_existing_pair_endpoints",
            )
            values.extend((pair + "-" + role, item) for role, item in roles.items())
        for slot, item in values:
            require(
                slot in self.allocations and set(item) == {"url", "key_file"},
                "existing_endpoint_outside_allocation",
            )
            gateway_url(item["url"])
            key = scoped_file(self.state, item["key_file"], private=True).read_text().strip()
            require(bool(KEY.fullmatch(key)), "invalid_existing_api_key")

    def run_command(self, argv, *, timeout=30, missing=False):
        if self.deadline is not None:
            remaining = self.deadline - self.clock()
            require(remaining > 0, "export_timeout")
            timeout = min(timeout, remaining)
        try:
            result = self.execute(
                argv,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ExportError("operator_command_unavailable_or_timeout") from None
        if result.returncode:
            if missing and re.match(
                r"^\s*ERROR:\s*\((?:ResourceNotFound|NotFound)\)", result.stderr or ""
            ):
                raise Pending("cluster_not_created")
            raise ExportError("operator_command_failed")
        return result.stdout

    def az(self, *args, missing=False):
        output = self.run_command(
            [
                "az",
                *args,
                "--subscription",
                self.subscription,
                "--only-show-errors",
                "--output",
                "json",
            ],
            timeout=60,
            missing=missing,
        )
        try:
            return json.loads(output)
        except ValueError:
            raise ExportError("invalid_azure_response") from None

    def kube(self, access: Access, *args):
        return self.run_command(
            [
                "kubectl",
                "--kubeconfig",
                str(access.kubeconfig),
                "--context",
                access.context,
                "--namespace",
                access.namespace,
                "--request-timeout=15s",
                *args,
            ],
            timeout=30,
        )

    def kube_json(self, access, *args, optional=False):
        suffix = ["--ignore-not-found"] if optional else []
        output = self.kube(access, *args, *suffix, "-o", "json")
        if optional and not output.strip():
            raise Pending("kubernetes_object_not_created")
        try:
            return json.loads(output)
        except ValueError:
            raise ExportError("invalid_kubernetes_response") from None

    def owned_azure(self, value, expected_id):
        require(value.get("id", "").lower() == expected_id.lower(), "azure_resource_id_mismatch")
        tags = value.get("tags") or {}
        require(
            tags.get("project") == PROJECT and tags.get("managedBy") == MANAGED_BY,
            "azure_resource_ownership_mismatch",
        )

    def access(self, slot):
        item = self.allocations[slot]
        value = self.az(
            "aks",
            "show",
            "--resource-group",
            item["clusterResourceGroup"],
            "--name",
            item["clusterName"],
            "--query",
            "{id:id,tags:tags,provisioningState:provisioningState,fqdn:fqdn,"
            "privateFqdn:privateFqdn,disableLocalAccounts:disableLocalAccounts,aadProfile:aadProfile}",
            missing=slot != "management",
        )
        self.owned_azure(value, self.cluster_id(slot))
        if value.get("provisioningState") != "Succeeded":
            raise Pending("cluster_not_ready")
        require(
            value.get("disableLocalAccounts") is True
            and value.get("aadProfile", {}).get("managed") is True
            and value.get("aadProfile", {}).get("enableAzureRbac") is True,
            "cluster_requires_operator_entra_access",
        )
        if slot in self.accesses:
            access = self.accesses[slot]
            self.verify_kubeconfig(access.kubeconfig, access.context, value)
            identity = self.kube_json(access, "get", "namespace", "kube-system")
            require(
                identity["metadata"]["uid"] == access.cluster_uid,
                "published_cluster_identity_changed",
            )
            return access
        context = PROJECT + "-" + slot
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        namespace = f"{PROJECT}-{slot}-{role}"
        require(NAME.fullmatch(namespace), "application_namespace_too_long")
        path = self.work / (slot + ".kubeconfig")
        private_write(path, "")
        self.run_command(
            [
                "az",
                "aks",
                "get-credentials",
                "--subscription",
                self.subscription,
                "--resource-group",
                item["clusterResourceGroup"],
                "--name",
                item["clusterName"],
                "--context",
                context,
                "--file",
                str(path),
                "--overwrite-existing",
                "--only-show-errors",
            ],
            timeout=120,
        )
        path.chmod(0o600)
        self.run_command(
            [
                "kubelogin",
                "convert-kubeconfig",
                "--kubeconfig",
                str(path),
                "--context",
                context,
                "--login",
                "azurecli",
            ]
        )
        trusted = self.verify_kubeconfig(path, context, value)
        canonical = self.state / (slot + ".kubeconfig")
        require(not canonical.is_symlink(), "published_kubeconfig_symlink_refused")
        if canonical.exists():
            try:
                require(
                    canonical.is_file()
                    and stat.S_IMODE(canonical.stat().st_mode) == 0o600
                    and self.verify_kubeconfig(canonical, context, value) == trusted,
                    "cleanup_kubeconfig_not_owned",
                )
            except (AttributeError, KeyError, TypeError, ValueError, yaml.YAMLError):
                raise ExportError("cleanup_kubeconfig_not_owned") from None
            path = canonical
        access = Access(slot, context, namespace, path, self.cluster_id(slot), "")
        identity = self.kube_json(access, "get", "namespace", "kube-system")
        access.cluster_uid = str(UUID(identity["metadata"]["uid"]))
        previous = self.targets.get(slot)
        require(
            not previous or previous["cluster_uid"] == access.cluster_uid,
            "published_cluster_identity_changed",
        )
        self.accesses[slot] = access
        return access

    @staticmethod
    def verify_kubeconfig(path, context, aks):
        values = yaml.safe_load(path.read_text())
        require(values.get("current-context") == context, "downloaded_context_mismatch")
        contexts = [item["context"] for item in values["contexts"] if item["name"] == context]
        require(len(contexts) == 1, "downloaded_context_mismatch")
        clusters = [
            item["cluster"] for item in values["clusters"] if item["name"] == contexts[0]["cluster"]
        ]
        users = [item["user"] for item in values["users"] if item["name"] == contexts[0]["user"]]
        require(len(clusters) == len(users) == 1, "downloaded_kubeconfig_ambiguous")
        server = urlsplit(clusters[0]["server"])
        require(
            server.scheme == "https"
            and server.hostname in {aks.get("fqdn"), aks.get("privateFqdn")}
            and not clusters[0].get("insecure-skip-tls-verify")
            and bool(clusters[0].get("certificate-authority-data")),
            "downloaded_kubernetes_tls_mismatch",
        )
        user = users[0]
        require(
            set(user) == {"exec"}
            and Path(user["exec"]["command"]).name == "kubelogin"
            and "azurecli" in user["exec"].get("args", []),
            "downloaded_credentials_are_not_operator_entra",
        )
        return clusters[0], users[0]

    def component(self, access, component, deployments):
        candidates = [
            item
            for item in deployments
            if item["spec"]["template"]["metadata"].get("labels", {}).get("plane-demo/component")
            == component
        ]
        if not candidates:
            raise Pending("component_not_deployed")
        require(len(candidates) == 1, "component_deployment_ambiguous")
        deployment = candidates[0]
        require(
            deployment["spec"]["template"]["metadata"]["labels"].get("plane-demo/project")
            == PROJECT,
            "workload_ownership_mismatch",
        )
        if deployment["spec"].get("replicas", 1) != 1:
            raise Pending("component_not_single_running_replica")
        selector = f"plane-demo/project={PROJECT},plane-demo/component={component}"
        pods = self.kube_json(access, "get", "pods", "-l", selector).get("items", [])
        pods = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
        if len(pods) != 1 or pods[0].get("status", {}).get("phase") != "Running":
            raise Pending("component_pod_not_ready")
        pod = pods[0]
        require(not pod["spec"].get("hostNetwork", False), "host_network_workload_refused")
        owner = pod["metadata"].get("ownerReferences", [])
        require(len(owner) == 1 and owner[0].get("kind") == "ReplicaSet", "pod_controller_mismatch")
        replicaset = self.kube_json(access, "get", "replicaset", owner[0]["name"])
        require(
            replicaset["metadata"]["uid"] == owner[0]["uid"]
            and any(
                item.get("uid") == deployment["metadata"]["uid"]
                and item.get("kind") == "Deployment"
                for item in replicaset["metadata"].get("ownerReferences", [])
            ),
            "pod_deployment_identity_mismatch",
        )
        entrypoint = {
            "management-api": "plane_demo.management.api",
            "provisioner": "plane_demo.management.provisioner",
            "control-api": "plane_demo.control.api",
            "control-reconciler": "plane_demo.control.reconciler",
            "data-api": "plane_demo.data.api",
            "data-reconciler": "plane_demo.data.reconciler",
        }[component]
        containers = [
            item
            for item in pod["spec"]["containers"]
            if entrypoint in item.get("command", []) + item.get("args", [])
        ]
        require(len(containers) == 1, "component_entrypoint_mismatch")
        container = containers[0]
        image_role = "provisioner" if component == "provisioner" else "api"
        if container.get("image") != self.images[image_role]:
            raise Pending("component_image_not_deployed")
        if component.endswith("-api"):
            require(
                any(
                    item.get("secretRef", {}).get("name") == component + "-runtime"
                    for item in container.get("envFrom", [])
                ),
                "api_runtime_secret_reference_mismatch",
            )
        return {"deployment": deployment["metadata"]["name"], "container": container["name"]}, pod

    def pod_json(self, access, component, pod, code, *args):
        output = self.kube(
            access,
            "exec",
            pod["metadata"]["name"],
            "-c",
            component["container"],
            "--",
            "python",
            "-c",
            code,
            *args,
        )
        try:
            value = json.loads(output)
        except ValueError:
            raise ExportError("invalid_pod_metadata_response") from None
        require(isinstance(value, dict) and "error" not in value, "pod_metadata_probe_failed")
        return value

    def demo_key(self, access, role):
        output = self.kube(
            access,
            "get",
            "secret",
            role + "-api-runtime",
            "--ignore-not-found",
            "-o",
            'jsonpath={.metadata.uid}{"\\n"}{.data.DEMO_KEY}',
        )
        if not output.strip():
            raise Pending("api_runtime_secret_not_created")
        lines = output.splitlines()
        require(len(lines) == 2, "api_runtime_demo_key_missing")
        UUID(lines[0])
        try:
            key = base64.b64decode(lines[1], validate=True).decode()
        except (ValueError, UnicodeError):
            raise ExportError("api_runtime_demo_key_invalid") from None
        require(bool(KEY.fullmatch(key)), "api_runtime_demo_key_invalid")
        return key

    def parent_subnet(self, slot):
        subnet_id = self.allocations[slot]["postgresqlSubnetId"]
        actual = self.az(
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
        require(
            actual.get("id", "").lower() == subnet_id.lower(), "parent_subnet_identity_mismatch"
        )
        prefixes = actual.get("addressPrefixes") or [actual.get("addressPrefix")]
        require(
            prefixes == [self.expected_subnet(slot)]
            and any(
                item.get("serviceName") == "Microsoft.DBforPostgreSQL/flexibleServers"
                for item in actual.get("delegations", [])
            ),
            "parent_postgresql_subnet_mismatch",
        )
        return prefixes[0]

    def actual_gateway(self, slot):
        item = self.allocations[slot]
        gateways = self.az(
            "network",
            "application-gateway",
            "list",
            "--resource-group",
            item["appResourceGroup"],
            "--query",
            "[].{id:id,name:name,tags:tags,provisioningState:provisioningState,"
            "httpListeners:httpListeners,frontendIPConfigurations:frontendIPConfigurations}",
        )
        if not gateways:
            raise Pending("gateway_not_created")
        require(len(gateways) == 1, "gateway_inventory_ambiguous")
        gateway = gateways[0]
        expected = (
            item["appResourceGroupId"] + "/providers/Microsoft.Network/"
            "applicationGateways/" + gateway["name"]
        )
        self.owned_azure(gateway, expected)
        if gateway.get("provisioningState") != "Succeeded":
            raise Pending("gateway_not_ready")
        listeners = [
            value
            for value in gateway.get("httpListeners", [])
            if value.get("protocol", "").lower() == "https"
        ]
        if not listeners:
            raise Pending("gateway_https_not_configured")
        ips = {
            entry["publicIPAddress"]["id"]
            for entry in gateway.get("frontendIPConfigurations", [])
            if entry.get("publicIPAddress")
            and any(
                listener.get("frontendIPConfiguration", {}).get("id") == entry["id"]
                for listener in listeners
            )
        }
        require(len(ips) == 1, "gateway_public_frontend_ambiguous")
        public_id = ips.pop()
        require(
            public_id.lower().startswith(
                (
                    item["appResourceGroupId"] + "/providers/Microsoft.Network/publicIPAddresses/"
                ).lower()
            ),
            "gateway_public_ip_outside_allocation",
        )
        public = self.az(
            "network",
            "public-ip",
            "show",
            "--ids",
            public_id,
            "--query",
            "{id:id,tags:tags,dnsSettings:dnsSettings}",
        )
        self.owned_azure(public, public_id)
        fqdn = public.get("dnsSettings", {}).get("fqdn", "")
        url = gateway_url("https://" + fqdn)
        for listener in listeners:
            hosts = listener.get("hostNames") or (
                [listener["hostName"]] if listener.get("hostName") else []
            )
            require(not hosts or hosts == [fqdn], "gateway_listener_hostname_mismatch")
        self.health(url)
        return url

    def collect_plane(self, slot):
        access = self.access(slot)
        self.publish_cleanup(access)
        namespace = self.kube_json(access, "get", "namespace", access.namespace, optional=True)
        namespace_uid = str(UUID(namespace["metadata"]["uid"]))
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        components, pods = {}, {}
        deployments = self.kube_json(access, "get", "deployments").get("items", [])
        names = component_names(role)
        for name in names:
            components[name], pods[name] = self.component(access, name, deployments)
        target = {
            "context": access.context,
            "kubeconfig": slot + ".kubeconfig",
            "namespace": access.namespace,
            "cluster_uid": access.cluster_uid,
            "namespace_uid": namespace_uid,
            "components": components,
        }
        if role != "management":
            component = role + "-reconciler"
            parent_slot = (
                "management" if role == "control" else slot.removesuffix("-data") + "-control"
            )
            environment = "MANAGEMENT_DSN" if role == "control" else "CONTROL_DSN"
            parent = self.pod_json(
                access, components[component], pods[component], PARENT_PROBE, environment
            )
            require(
                HOST.fullmatch(parent.get("host", "")) and parent.get("port") == 5432,
                "invalid_parent_postgresql_endpoint",
            )
            target["parent"] = {**parent, "allowed_cidrs": [self.parent_subnet(parent_slot)]}
        url = self.actual_gateway(slot)
        key = self.demo_key(access, role)
        confirmed = self.kube_json(access, "get", "namespace", access.namespace)
        require(confirmed["metadata"]["uid"] == namespace_uid, "namespace_changed_during_export")
        old = self.targets.get(slot)
        comparable = target
        if old and slot == "management" and "provisioner" not in old["components"]:
            comparable = {
                **target,
                "components": {
                    name: value for name, value in components.items() if name != "provisioner"
                },
            }
        require(not old or old == comparable, "published_target_identity_changed")
        return Plane(
            target,
            url,
            key,
            access,
            pods[role + "-api"]["metadata"]["name"],
            components[role + "-api"]["container"],
        )

    def inventory(self, management: Plane):
        return self.pod_json(
            management.access,
            {"container": management.api_container},
            {"metadata": {"name": management.api_pod}},
            INVENTORY_PROBE,
            json.dumps(list(self.tenants.values())),
        )

    def planned_slots(self, inventory):
        require(
            set(inventory) == {"pairs", "tenants"}
            and isinstance(inventory["pairs"], list)
            and isinstance(inventory["tenants"], list),
            "invalid_management_inventory",
        )
        tenants = {item["tenant_id"]: item for item in inventory["tenants"]}
        require(set(tenants) <= set(self.tenants.values()), "unexpected_inventory_tenant")
        for key in ("shared_a", "shared_b"):
            item = tenants.get(self.tenants[key])
            require(
                not item or item["pair_id"] == "shared" and item["isolation"] == "shared",
                "showcase_shared_assignment_mismatch",
            )
        item = tenants.get(self.tenants["isolated"])
        require(
            not item or item["pair_id"] != "shared" and item["isolation"] == "isolated",
            "showcase_isolated_assignment_mismatch",
        )
        pairs = {"shared"}
        for value in inventory.get("tenants", []):
            pair = value["pair_id"]
            require(
                pair + "-control" in self.allocations and pair + "-data" in self.allocations,
                "tenant_pair_outside_allocation",
            )
            pairs.add(pair)
        isolated = next(
            (
                value["pair_id"]
                for value in inventory.get("tenants", [])
                if value["tenant_id"] == self.tenants["isolated"]
            ),
            None,
        )
        if isolated is None:
            isolated = sorted(
                slot.removesuffix("-control")
                for slot in self.allocations
                if slot.endswith("-control") and slot != "shared-control"
            )[0]
        pairs.add(isolated)
        for pair in inventory.get("pairs", []):
            require(
                pair["pair_id"] + "-control" in self.allocations,
                "inventory_pair_outside_allocation",
            )
            for role in ("control", "data"):
                cluster_id = pair.get(role + "_cluster_id")
                require(
                    not cluster_id
                    or cluster_id.lower() == self.cluster_id(pair["pair_id"] + "-" + role).lower(),
                    "inventory_cluster_identity_mismatch",
                )
        return ["management"] + sorted(
            pair + "-" + role for pair in pairs for role in ("control", "data")
        )

    def publish_cleanup(self, access: Access):
        require(stat.S_IMODE(self.state.stat().st_mode) == 0o700, "cleanup_state_not_private")
        marker = {
            "version": 1,
            "generatedBy": CLEANUP_GENERATOR,
            "project": PROJECT,
            "subscriptionId": self.subscription,
        }
        header = (
            f"# Generated by {CLEANUP_GENERATOR} for {PROJECT} cleanup in {self.subscription}.\n"
        )
        target_path, radius_path = (
            self.state / "cleanup-targets.json",
            self.state / "cleanup-radius.yaml",
        )
        radius_reference = str(radius_path.relative_to(self.cleanup_state))
        require(
            not target_path.is_symlink() and not radius_path.is_symlink(),
            "cleanup_export_symlink_refused",
        )
        targets = {}
        if target_path.exists():
            document = read_json(target_path)
            require(
                set(document) == {*marker, "targets"}
                and all(document.get(key) == value for key, value in marker.items())
                and isinstance(document["targets"], dict),
                "cleanup_targets_not_owned",
            )
            targets = document["targets"]
            require(set(targets) <= set(self.allocations), "cleanup_target_outside_allocation")
            for slot, target in targets.items():
                expected = {
                    "clusterId": self.cluster_id(slot),
                    "clusterUid": str(UUID(target["clusterUid"])),
                    "context": PROJECT + "-" + slot,
                    "workspace": PROJECT + "-" + slot,
                    "group": PROJECT,
                    "kubeconfig": str(
                        (self.state / (slot + ".kubeconfig")).relative_to(self.cleanup_state)
                    ),
                    "radiusConfig": radius_reference,
                }
                require(
                    target == expected
                    or target
                    == {
                        **expected,
                        "kubeconfig": slot + ".kubeconfig",
                        "radiusConfig": radius_path.name,
                    },
                    "cleanup_target_identity_mismatch",
                )
                targets[slot] = expected
        scope = f"/planes/radius/local/resourceGroups/{PROJECT}"
        workspaces = {
            PROJECT + "-" + slot: {
                "connection": {"kind": "kubernetes", "context": PROJECT + "-" + slot},
                "scope": scope,
            }
            for slot in self.allocations
        }
        if radius_path.exists():
            require(
                radius_path.is_file() and radius_path.stat().st_size <= 2_000_000,
                "invalid_cleanup_radius_file",
            )
            content = radius_path.read_text()
            require(content.startswith(header), "cleanup_radius_not_owned")
            radius = yaml.safe_load(content)
            items = radius["workspaces"]["items"]
            require(
                isinstance(items, dict)
                and set(items) <= set(workspaces)
                and radius
                == {
                    "workspaces": {
                        "default": PROJECT + "-management",
                        "items": {name: workspaces[name] for name in items},
                    }
                },
                "cleanup_radius_scope_mismatch",
            )
        final_kubeconfig = self.state / (access.slot + ".kubeconfig")
        target = {
            "clusterId": access.cluster_id,
            "clusterUid": access.cluster_uid,
            "context": access.context,
            "workspace": access.context,
            "group": PROJECT,
            "kubeconfig": str(final_kubeconfig.relative_to(self.cleanup_state)),
            "radiusConfig": radius_reference,
        }
        previous = targets.get(access.slot)
        require(not previous or previous == target, "published_cleanup_identity_changed")
        require(not final_kubeconfig.is_symlink(), "published_kubeconfig_symlink_refused")
        if access.kubeconfig != final_kubeconfig:
            require(not final_kubeconfig.exists(), "cleanup_kubeconfig_not_owned")
            private_write(final_kubeconfig, header + access.kubeconfig.read_text())
            access.kubeconfig = final_kubeconfig
        targets[access.slot] = target
        radius = {
            "workspaces": {
                "default": PROJECT + "-management",
                "items": {
                    targets[slot]["workspace"]: workspaces[targets[slot]["workspace"]]
                    for slot in sorted(targets)
                },
            }
        }
        # Publish dependencies first; an interrupted handoff never references an unwritten file.
        private_write(radius_path, header + json.dumps(radius, sort_keys=True, indent=2) + "\n")
        write_json(target_path, {**marker, "targets": targets})

    def publish(self, planes):
        endpoints = json.loads(json.dumps(self.endpoints))
        targets = dict(self.targets)
        changed = self.previous is None or self.previous.get("images") != self.images
        for slot, plane in planes.items():
            if slot == "management":
                old = endpoints.get("management")
            else:
                pair, role = slot.rsplit("-", 1)
                old = endpoints.get("pairs", {}).get(pair, {}).get(role)
            require(not old or old["url"] == plane.url, "published_gateway_url_changed")
            key_name = old["key_file"] if old else slot + ".key"
            key_path = self.state / key_name
            if key_path.exists():
                existing = scoped_file(self.state, key_name, private=True).read_text().strip()
                require(hmac.compare_digest(existing, plane.demo_key), "published_api_key_changed")
            else:
                private_write(key_path, plane.demo_key + "\n")
            entry = {"url": plane.url, "key_file": key_name}
            changed = changed or old != entry or targets.get(slot) != plane.target
            targets[slot] = plane.target
            if slot == "management":
                endpoints["management"] = entry
            else:
                pair, role = slot.rsplit("-", 1)
                endpoints.setdefault("pairs", {}).setdefault(pair, {})[role] = entry
        if not changed:
            if self.previous is not None:
                compatibility = self.state / "endpoints.json"
                if not compatibility.exists() or read_json(compatibility) != endpoints:
                    write_json(compatibility, endpoints)
            return
        require("management" in targets, "management_must_be_exported_first")
        generation = uuid4().hex
        relative = f"exported-state/{generation}/endpoints.json"
        write_json(self.state / relative, endpoints)
        acceptance = {
            "version": 1,
            "environment": "azure",
            "project": PROJECT,
            "synthetic_data": True,
            "endpoints_file": relative,
            "onboarding_timeout_seconds": 3600,
            "tenants": dict(self.tenants),
            "targets": targets,
            "images": self.images,
            "export": {"generation": generation, "observed_at": now()},
        }
        # A reader of acceptance.json always gets its matching immutable endpoint generation.
        write_json(self.state / "acceptance.json", acceptance)
        write_json(self.state / "endpoints.json", endpoints)
        self.previous, self.targets, self.endpoints = acceptance, targets, endpoints

    def sample(self):
        ready, pending = {}, {}
        try:
            management = self.collect_plane("management")
        except Pending as error:
            return self.progress({}, {"management": str(error)}, [], complete=False)
        ready["management"] = management
        inventory = self.inventory(management)
        slots = self.planned_slots(inventory)
        self.publish({"management": management})
        assigned_pairs = {item["pair_id"] for item in inventory["tenants"]}
        known_slots = {
            pair["pair_id"] + "-" + role
            for pair in inventory["pairs"]
            for role in ("control", "data")
            if pair.get(role + "_cluster_id")
        }
        for slot in sorted(known_slots):
            if slot in slots and slot.rsplit("-", 1)[0] in assigned_pairs:
                continue
            try:
                self.publish_cleanup(self.access(slot))
            except Pending as error:
                pending[slot] = str(error)
        for slot in sorted(slots[1:], key=lambda item: (item in self.targets, item)):
            pair, role = slot.rsplit("-", 1)
            if pair not in assigned_pairs:
                pending[slot] = "pair_not_requested"
                continue
            try:
                plane = self.collect_plane(slot)
                record = next(
                    (item for item in inventory["pairs"] if item["pair_id"] == pair), None
                )
                require(record is not None, "assigned_pair_inventory_missing")
                expected_url = record.get(role + "_url")
                require(
                    not expected_url or gateway_url(expected_url) == plane.url,
                    "inventory_gateway_mismatch",
                )
                ready[slot] = plane
                self.publish({slot: plane})
            except Pending as error:
                pending[slot] = str(error)
        tenants = {item["tenant_id"]: item for item in inventory.get("tenants", [])}
        completed = [
            name
            for name in self.tenants.values()
            if name in tenants and tenants[name].get("ready") is True
        ]
        return self.progress(
            ready, pending, completed, complete=not pending and len(completed) == 3
        )

    def progress(self, ready, pending, completed, *, complete):
        value = {
            "version": 1,
            "outcome": "export_complete" if complete else "waiting",
            "observed_at": now(),
            "pid": os.getpid(),
            "ready_for_onboarding": "management" in ready and "management" in self.targets,
            "ready_slots": sorted(ready),
            "published_slots": sorted(self.targets),
            "pending_slots": pending,
            "showcase_ready": sorted(completed),
            "acceptance_file": "acceptance.json" if self.previous else None,
        }
        write_json(self.state / "export-status.json", value)
        return value

    def take_lock(self):
        path = self.state / "export-state.lock"
        require(not path.is_symlink(), "export_lock_symlink_refused")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        handle = os.fdopen(descriptor, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ExportError("exporter_already_running") from None
        return handle

    def run(self, *, watch: bool, timeout=7200, emit=print):
        require(1 <= timeout <= 10800, "invalid_export_timeout")
        self.deadline = self.clock() + timeout
        lock = self.take_lock()
        try:
            current = (
                read_json(self.state / "acceptance.json")
                if (self.state / "acceptance.json").exists()
                else None
            )
            require(current == self.previous, "snapshot_changed_restart_exporter")
            write_json(
                self.state / "export-status.json",
                {
                    "version": 1,
                    "outcome": "starting",
                    "observed_at": now(),
                    "pid": os.getpid(),
                    "ready_for_onboarding": False,
                    "published_slots": sorted(self.targets),
                },
            )
            while True:
                value = self.sample()
                require(self.clock() <= self.deadline, "export_timeout")
                emit(json.dumps(value, sort_keys=True))
                if value["outcome"] == "export_complete":
                    return 0
                if not watch:
                    return 3
                require(self.clock() < self.deadline, "export_timeout")
                self.sleep(min(5, self.deadline - self.clock()))
        except Exception as error:
            value = {
                "version": 1,
                "outcome": "failed",
                "observed_at": now(),
                "pid": os.getpid(),
                "error": str(error) if isinstance(error, ExportError) else "state_export_failed",
                "published_slots": sorted(self.targets),
                "ready_for_onboarding": False,
            }
            write_json(self.state / "export-status.json", value)
            raise
        finally:
            lock.close()
            shutil.rmtree(self.work)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".state/azure/provisioning.json")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--watch", action="store_true")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args(argv)
    exporter = None

    def interrupted(_signal, _frame):
        raise ExportError("export_interrupted")

    previous = {
        number: signal.signal(number, interrupted) for number in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        exporter = Exporter(args.config)
        return exporter.run(watch=args.watch, timeout=args.timeout)
    except Exception as error:
        print(
            json.dumps(
                {
                    "outcome": "failed",
                    "error": str(error)
                    if isinstance(error, ExportError)
                    else "state_export_failed",
                }
            )
        )
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if exporter is not None and exporter.work.exists():
            shutil.rmtree(exporter.work)


if __name__ == "__main__":
    raise SystemExit(main())
