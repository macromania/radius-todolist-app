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
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
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

    def labels(self, component: str) -> dict[str, str]:
        return {"plane-demo/project": self.target.project, "plane-demo/component": component}

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

    def policies(self, exclude: str | None = None) -> list[dict]:
        policies = []
        for resource in ("networkpolicies.networking.k8s.io", POLICY_RESOURCE):
            for item in self.json("get", resource).get("items", []):
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
            "labels": {"plane-demo/project": target.project, "plane-demo/fault-run": run_id},
        },
        "spec": {
            "endpointSelector": {
                "matchLabels": {
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
        evidence: Path,
        *,
        kube_factory=Kubectl,
        probe_factory=Probe,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        require(configuration.environment == self.environment, "fault_environment_mismatch")
        require(component in COMPONENT_DSN, "unsupported_fault_component")
        self.target = configuration.target(slot)
        self.component = component
        self.evidence_path = evidence
        self.kube = kube_factory(self.target)
        self.probe_factory = probe_factory
        self.clock, self.sleep = clock, sleep
        self.run_id = uuid4().hex[:12]
        self.record = {
            "version": 1,
            "run_id": self.run_id,
            "project": self.target.project,
            "slot": slot,
            "component": component,
            "started_at": utc_now(),
            "outcome": "not_activated",
            "restored": False,
            "probes": [],
            "cluster_uid": self.target.cluster_uid,
            "namespace_uid": self.target.namespace_uid,
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
        protected_write(self.evidence_path, self.record)

    def _check(self, action: str) -> dict:
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
        self.record["original_policies"] = self.original

    def plan_fault(self, cidrs):
        self.policy = deny_policy(self.target, self.component, cidrs, self.run_id)
        self.record["policy"] = self.policy

    def create_fault(self):
        # create, never apply: an existing object must never be overwritten.
        self.kube.run("create", "-f", "-", payload=json.dumps(self.policy))
        self.created = True
        created = self.kube.json("get", POLICY_RESOURCE, self.policy["metadata"]["name"])
        self.record["policy_uid"] = created["metadata"]["uid"]

    def remove_fault(self):
        self.kube.verify_scope()
        name = self.policy["metadata"]["name"]
        current = self.kube.optional(POLICY_RESOURCE, name)
        if current:
            require(
                current["metadata"].get("labels", {}).get("plane-demo/fault-run") == self.run_id
                and current.get("spec") == self.policy["spec"],
                "fault_policy_ownership_changed",
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
        self.plan_fault(cidrs)
        self.record["baseline"] = baseline
        self.record["outcome"] = "activating"
        self.creation_attempted = True
        self.record["creation_attempted"] = True
        self._save()
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
        restoration_error = None
        self.record["restored"] = False
        self.record["physical_restored"] = False
        self.record.pop("restored_at", None)
        self.record.pop("physical_restored_at", None)
        try:
            if self.creation_attempted:
                self.recovery_started = self.clock()
                self.recovery_deadline = self.recovery_started + RECOVERY_SECONDS
                self.record["restoration_started_at"] = utc_now()
                self.record["recovery_started_monotonic"] = self.recovery_started
                self.record["recovery_deadline_monotonic"] = self.recovery_deadline
                self.remove_fault()
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
            self._save()
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
        path = state_path(path)
        require(path.is_relative_to(configuration.root / "evidence"), "restore_evidence_scope")
        record = read_json(path)
        require(record.get("version") == 1, "unsupported_fault_evidence_version")
        fault = cls(configuration, record["slot"], record["component"], path, **kwargs)
        require(
            record.get("project") == fault.target.project
            and record.get("cluster_uid") == fault.target.cluster_uid
            and record.get("namespace_uid") == fault.target.namespace_uid,
            "restore_target_identity_changed",
        )
        expected = deny_policy(
            fault.target,
            fault.component,
            parent_cidrs(fault.target, record["baseline"]["ips"]),
            record["run_id"],
        )
        require(
            record.get("policy") == expected and record.get("creation_attempted") is True,
            "restore_policy_contract_mismatch",
        )
        fault.record = record
        fault.run_id = record["run_id"]
        fault.policy = expected
        fault.original = record["original_policies"]
        fault.creation_attempted = True
        return fault


def fault_class(configuration):
    if configuration.environment == "azure":
        return ParentFault
    name = "plane_demo_local_fault_" + __name__
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "scripts/harness/local/fault-parent-link.py"
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--slot")
    parser.add_argument("--component", choices=tuple(COMPONENT_DSN))
    parser.add_argument("--restore", type=Path, help="Restore only this recorded, owned fault")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        require(60 <= args.duration <= 600, "fault_duration_must_be_60_to_600_seconds")
        configuration = Configuration(args.config)
        selected_fault = fault_class(configuration)
        if args.restore:
            with interruption_is_failure():
                fault = selected_fault.from_evidence(configuration, args.restore)
                fault.restore()
            print(
                json.dumps(
                    {"outcome": "restored_only_not_acceptance", "evidence": str(args.restore)}
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
        print(json.dumps({"outcome": "fault_verified_and_restored", "evidence": str(evidence)}))
        return 0
    except Exception:
        print(json.dumps({"outcome": "failed", "error": "parent_fault_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
