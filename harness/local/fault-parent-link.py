#!/usr/bin/env python3
"""Operator-only parent faults inside an owned kind reconciler Pod network namespace."""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

if "base" not in globals():
    spec = importlib.util.spec_from_file_location(
        "plane_demo_standalone_fault", Path(__file__).resolve().parents[1] / "fault-parent-link.py"
    )
    base = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = base
    spec.loader.exec_module(base)

require = base.require
Error = base.AcceptanceError
DOCKER_HOST = "unix:///Users/mahmutcanga/.docker/run/docker.sock"
NODE_IMAGE = (
    "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
)
HEX_ID = re.compile(r"[a-f0-9]{64}\Z")
SLOTS = ("management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data")

# The open descriptor pins the namespace even if the sandbox exits between checks.
# Never enter the node namespace, and never invoke a shell with interpolated input.
NETWORK_COMMAND = r"""
set -eu
pid=$1
inode=$2
shift 2
test "$pid" -gt 1
exec 3<"/proc/$pid/ns/net"
test "$(stat -Lc %i /proc/self/fd/3)" = "$inode"
test "$(stat -Lc %i /proc/1/ns/net)" != "$inode"
exec nsenter --net=/proc/self/fd/3 -- "$@"
"""

RULE_COMMAND = r"""
set -eu
action=$1
shift
case "$action" in
  add)
    if iptables -w 2 -C OUTPUT "$@" 2>/dev/null; then exit 41; fi
    iptables -w 2 -I OUTPUT 1 "$@"
    iptables -w 2 -C OUTPUT "$@"
    ;;
  check) iptables -w 2 -C OUTPUT "$@" ;;
  delete)
    if iptables -w 2 -C OUTPUT "$@" 2>/dev/null; then
      iptables -w 2 -D OUTPUT "$@"
    fi
    if iptables -w 2 -C OUTPUT "$@" 2>/dev/null; then exit 42; fi
    ;;
  *) exit 43 ;;
esac
"""


def docker(*args):
    return ["docker", "--host", DOCKER_HOST, *args]


def environment():
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(base.ROOT / ".state/local/home"),
        "LC_ALL": "C",
        "DOCKER_HOST": DOCKER_HOST,
    }


def operator_command(argv, *, payload=None, timeout=30):
    try:
        result = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=base.ROOT,
            env=environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Error("local_operator_command_unavailable_or_timeout") from None
    require(result.returncode == 0, "local_operator_command_failed")
    return result.stdout


def node_identity(run, slot, expected=None):
    require(slot in SLOTS, "local_node_slot_refused")
    cluster = "radplanes-local-" + slot
    name = cluster + "-control-plane"
    ids = run(
        docker("ps", "-aq", "--no-trunc", "--filter", "label=io.x-k8s.kind.cluster=" + cluster)
    ).split()
    require(len(ids) == 1 and HEX_ID.fullmatch(ids[0]), "local_node_inventory_mismatch")
    values = json.loads(run(docker("inspect", "--type", "container", ids[0])))
    require(isinstance(values, list) and len(values) == 1, "local_node_inspection_ambiguous")
    node = values[0]
    address = node.get("NetworkSettings", {}).get("Networks", {}).get("kind", {}).get("IPAddress")
    try:
        ip = ipaddress.IPv4Address(address)
    except (ValueError, ipaddress.AddressValueError):
        raise Error("local_node_address_invalid") from None
    require(
        node.get("Id") == ids[0]
        and node.get("Name") == "/" + name
        and node.get("Config", {}).get("Labels", {}).get("io.x-k8s.kind.cluster") == cluster
        and node.get("Config", {}).get("Image") == NODE_IMAGE
        and node.get("State", {}).get("Running") is True
        and ip.is_private
        and not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified),
        "local_node_ownership_mismatch",
    )
    index = SLOTS.index(slot)
    require(
        node.get("HostConfig", {}).get("PortBindings")
        == {
            "6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(35495 + index)}],
            "31480/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(35490 + index)}],
        },
        "local_node_port_bindings_mismatch",
    )
    network = json.loads(run(docker("network", "inspect", "kind")))
    require(len(network) == 1, "local_kind_network_ambiguous")
    owners = [
        key
        for key, value in network[0].get("Containers", {}).items()
        if value.get("IPv4Address", "").split("/")[0] == str(ip)
    ]
    require(owners == [ids[0]], "local_node_address_not_unique")
    identity = {"id": ids[0], "name": name, "address": str(ip)}
    require(expected is None or identity == expected, "local_node_identity_changed")
    return identity


class LocalParentFault(base.ParentFault):
    environment = "local"

    def __init__(
        self, configuration, slot, component, evidence, *, operator=operator_command, **kwargs
    ):
        def kube_factory(target):
            kube = base.Kubectl(target, runner=operator)
            kube.environment = environment()
            return kube

        kwargs.setdefault("kube_factory", kube_factory)
        super().__init__(configuration, slot, component, evidence, **kwargs)
        require(
            configuration.root == base.ROOT / ".state/local"
            and self.target.slot in SLOTS[1:]
            and self.target.namespace
            == f"radplanes-local-{slot}-" + ("control" if slot.endswith("-control") else "data")
            and self.target.context == "radplanes-local-" + slot,
            "local_fault_scope_refused",
        )
        require(
            slot.endswith("-control" if component == "control-reconciler" else "-data"),
            "fault_component_slot_mismatch",
        )
        self.configuration = configuration
        self.operator = operator
        self.node = self.target.local.get("node")
        self.sandbox = None
        self.rule = None
        self.record["environment"] = "local"
        self.record["strategy"] = "pod-network-namespace-iptables"

    def verify_nodes(self):
        require(
            self.configuration.target(self.target.slot) == self.target,
            "local_fault_configuration_changed",
        )
        self.kube.verify_scope()
        owned = base.read_json(self.configuration.file("management-created.json", secret=True))
        management = self.target.local.get("management_node", {})
        require(
            owned.get("name") == "radplanes-local-management"
            and owned.get("context") == "radplanes-local-management"
            and owned.get("secretEncryptionVerified") is True
            and owned.get("nodeId") == management.get("id")
            and owned.get("nodeAddress") == management.get("address"),
            "local_management_bootstrap_mismatch",
        )
        node_identity(self.operator, "management", management)
        node_identity(self.operator, self.target.slot, self.node)
        parent_slot = (
            "management"
            if self.component == "control-reconciler"
            else self.target.slot.removesuffix("-data") + "-control"
        )
        parent_node = self.target.local.get("parent_node", {})
        node_identity(self.operator, parent_slot, parent_node)
        require(
            self.target.parent
            == {
                "host": parent_node["address"],
                "port": 31543,
                "allowed_cidrs": [parent_node["address"] + "/32"],
            },
            "local_parent_allocation_mismatch",
        )

    def exec_node(self, *args):
        require(
            isinstance(self.node, dict) and HEX_ID.fullmatch(self.node.get("id", "")),
            "local_node_identity_missing",
        )
        return self.operator(docker("exec", self.node["id"], *args), timeout=10)

    def sandbox_identity(self, pod):
        metadata, spec = pod["metadata"], pod["spec"]
        require(
            metadata.get("namespace") == self.target.namespace
            and spec.get("nodeName") == self.node["name"]
            and spec.get("serviceAccountName") == self.component
            and not spec.get("hostNetwork", False)
            and not spec.get("hostPID", False)
            and metadata.get("labels", {}).get("plane-demo/project") == "radplanes"
            and metadata.get("labels", {}).get("plane-demo/component") == self.component,
            "local_fault_pod_identity_mismatch",
        )
        pods = json.loads(self.exec_node("crictl", "pods", "-o", "json"))
        require(isinstance(pods.get("items"), list), "local_cri_sandbox_inventory_invalid")
        matches = [
            item for item in pods["items"] if item.get("metadata", {}).get("uid") == metadata["uid"]
        ]
        require(len(matches) == 1, "local_cri_sandbox_ambiguous")
        sandbox_id = matches[0].get("id", "")
        require(bool(HEX_ID.fullmatch(sandbox_id)), "local_cri_sandbox_id_invalid")
        sandbox = json.loads(self.exec_node("crictl", "inspectp", sandbox_id))
        status, info = sandbox.get("status", {}), sandbox.get("info", {})
        require(
            status.get("id") == sandbox_id
            and status.get("state") == "SANDBOX_READY"
            and all(
                status.get("metadata", {}).get(key) == metadata[key]
                for key in ("uid", "name", "namespace")
            )
            and status.get("labels", {}).get("io.kubernetes.pod.uid") == metadata["uid"],
            "local_cri_sandbox_identity_mismatch",
        )
        pid = info.get("pid")
        require(type(pid) is int and pid > 1, "local_cri_sandbox_pid_invalid")
        container = self.target.component(self.component)["container"]
        statuses = [
            item for item in pod["status"].get("containerStatuses", []) if item["name"] == container
        ]
        require(len(statuses) == 1, "local_fault_container_ambiguous")
        container_id = statuses[0].get("containerID", "").removeprefix("containerd://")
        require(bool(HEX_ID.fullmatch(container_id)), "local_fault_container_id_invalid")
        actual = json.loads(self.exec_node("crictl", "inspect", container_id))
        require(
            actual.get("info", {}).get("sandboxID") == sandbox_id
            and actual.get("status", {}).get("id") == container_id
            and actual["status"].get("labels", {}).get("io.kubernetes.pod.uid") == metadata["uid"]
            and actual["status"].get("metadata", {}).get("name") == container,
            "local_cri_container_identity_mismatch",
        )
        inode = self.exec_node("stat", "-Lc", "%i", f"/proc/{pid}/ns/net").strip()
        require(bool(re.fullmatch(r"[1-9][0-9]{0,19}", inode)), "local_network_inode_invalid")
        return {"id": sandbox_id, "pid": pid, "inode": inode, "container_id": container_id}

    def verify_sandbox(self):
        self.verify_nodes()
        pod = self.kube.pod(self.component)
        require(pod["metadata"]["uid"] == self.pod_uid, "reconciler_replaced_during_fault")
        require(self.sandbox_identity(pod) == self.sandbox, "local_network_namespace_changed")

    def network(self, *args):
        require(isinstance(self.sandbox, dict), "local_fault_sandbox_missing")
        return self.exec_node(
            "bash",
            "-ceu",
            NETWORK_COMMAND,
            "plane-demo-netns",
            str(self.sandbox["pid"]),
            self.sandbox["inode"],
            *args,
        )

    def rules_hash(self):
        return hashlib.sha256(
            self.network("iptables", "-w", "2", "-S", "OUTPUT").encode()
        ).hexdigest()

    def prepare_fault(self, pod):
        self.verify_nodes()
        self.sandbox = self.sandbox_identity(pod)
        original = self.network("iptables", "-w", "2", "-S", "OUTPUT")
        require(
            "plane-demo-fault-" + self.run_id not in original, "local_fault_rule_already_present"
        )
        self.original = hashlib.sha256(original.encode()).hexdigest()
        self.record.update(
            node=self.node, sandbox=self.sandbox, original_rules_sha256=self.original
        )

    def plan_fault(self, cidrs):
        require(cidrs == [self.target.parent["host"] + "/32"], "local_fault_destination_mismatch")
        self.rule = [
            "-d",
            cidrs[0],
            "-p",
            "tcp",
            "--dport",
            "31543",
            "-m",
            "comment",
            "--comment",
            "plane-demo-fault-" + self.run_id,
            "-j",
            "DROP",
        ]
        self.record["rule"] = self.rule

    def mutate_rule(self, action):
        self.network("bash", "-ceu", RULE_COMMAND, "plane-demo-rule", action, *self.rule)

    def create_fault(self):
        self.verify_sandbox()
        self.mutate_rule("add")
        self.created = True

    def assert_blocked(self):
        self.verify_sandbox()
        self.mutate_rule("check")
        super().assert_blocked()

    def remove_fault(self):
        self.verify_sandbox()
        self.mutate_rule("delete")
        restored = self.rules_hash()
        require(restored == self.original, "local_original_rules_changed")
        self.record["restored_rules_sha256"] = restored

    @classmethod
    def from_evidence(cls, configuration, path, **kwargs):
        path = base.state_path(path)
        require(path.is_relative_to(configuration.root / "evidence"), "restore_evidence_scope")
        require(
            path == configuration.file(str(path.relative_to(configuration.root)), secret=True),
            "restore_evidence_scope",
        )
        record = base.read_json(path)
        fault = cls(configuration, record["slot"], record["component"], path, **kwargs)
        require(
            record.get("version") == 1
            and record.get("project") == fault.target.project
            and record.get("environment") == "local"
            and record.get("strategy") == "pod-network-namespace-iptables"
            and record.get("creation_attempted") is True
            and record.get("cluster_uid") == fault.target.cluster_uid
            and record.get("namespace_uid") == fault.target.namespace_uid
            and record.get("node") == fault.node
            and bool(re.fullmatch(r"[a-f0-9]{12}", record.get("run_id", "")))
            and bool(base.UID.fullmatch(record.get("pod_uid", "")))
            and bool(HEX_ID.fullmatch(record.get("original_rules_sha256", ""))),
            "local_restore_identity_mismatch",
        )
        fault.run_id = record["run_id"]
        fault.plan_fault(base.parent_cidrs(fault.target, record["baseline"]["ips"]))
        require(fault.rule == record.get("rule"), "local_restore_rule_mismatch")
        fault.record = record
        fault.pod_uid = record["pod_uid"]
        fault.sandbox = record["sandbox"]
        fault.original = record["original_rules_sha256"]
        fault.creation_attempted = True
        return fault


def assert_restored_for_cleanup(run, configuration):
    """Read-only fault preflight; return non-secret proof for the caller's cleanup journal."""
    require(
        configuration.environment == "local"
        and configuration.root == base.ROOT / ".state/local"
        and stat.S_IMODE(configuration.root.stat().st_mode) == 0o700
        and set(configuration.current().get("targets", {})) == set(SLOTS),
        "cleanup_fault_requires_complete_local_export",
    )

    def operator(argv, *, payload=None, timeout=30):
        require(payload is None, "cleanup_fault_payload_refused")
        return run(argv, timeout=timeout)

    directory = configuration.root / "evidence"
    require(not directory.is_symlink(), "cleanup_fault_evidence_symlink")
    if directory.exists():
        require(
            directory.is_dir() and stat.S_IMODE(directory.stat().st_mode) == 0o700,
            "cleanup_fault_evidence_not_private",
        )

    def paths():
        result = sorted(directory.glob("*.json")) if directory.exists() else []
        require(all(not path.is_symlink() for path in result), "cleanup_fault_journal_symlink")
        return result

    initial_paths = paths()
    snapshots, journals, attempted = {}, [], []
    for path in initial_paths:
        relative = str(path.relative_to(configuration.root))
        path = configuration.file(relative, secret=True)
        require(path.stat().st_size <= 2_000_000, "cleanup_fault_journal_too_large")
        raw = path.read_bytes()
        snapshots[path] = hashlib.sha256(raw).hexdigest()
        try:
            record = json.loads(raw)
        except ValueError:
            raise Error("cleanup_fault_journal_invalid") from None
        require(isinstance(record, dict), "cleanup_fault_journal_invalid")
        named_fault = re.fullmatch(
            r"(?:fault-[a-f0-9]{32}|[a-f0-9]{32}-(?:management|control)-link)\.json", path.name
        )
        if (
            not named_fault
            and "strategy" not in record
            and "creation_attempted" not in record
            and record.get("component") not in base.COMPONENT_DSN
        ):
            continue
        require(
            record.get("version") == 1
            and record.get("environment") == "local"
            and record.get("project") == "radplanes"
            and record.get("strategy") == "pod-network-namespace-iptables"
            and record.get("slot") in SLOTS[1:]
            and isinstance(record.get("run_id"), str)
            and re.fullmatch(r"[a-f0-9]{12}", record["run_id"]),
            "cleanup_fault_journal_identity_mismatch",
        )
        slot = record["slot"]
        target = configuration.target(slot)
        require(
            record.get("cluster_uid") == target.cluster_uid
            and record.get("namespace_uid") == target.namespace_uid
            and record.get("component") == slot.rsplit("-", 1)[1] + "-reconciler",
            "cleanup_fault_journal_target_changed",
        )
        started = record.get("creation_attempted")
        require(started is None or type(started) is bool, "cleanup_fault_attempt_marker_invalid")
        if started:
            require(
                record.get("restored") is True
                and record.get("physical_restored") is True
                and isinstance(record.get("original_rules_sha256"), str)
                and HEX_ID.fullmatch(record["original_rules_sha256"])
                and record["original_rules_sha256"] == record.get("restored_rules_sha256")
                and isinstance(record.get("restoration_probe"), dict)
                and record.get("restoration_probe", {}).get("ok") is True,
                "cleanup_fault_not_restored",
            )
            try:
                times = [
                    datetime.fromisoformat(record[key])
                    for key in ("restoration_started_at", "physical_restored_at", "restored_at")
                ]
            except (KeyError, TypeError, ValueError):
                raise Error("cleanup_fault_restoration_timestamps_invalid") from None
            require(
                all(value.tzinfo is not None for value in times)
                and times[0] <= times[1] <= times[2] <= datetime.now(UTC),
                "cleanup_fault_restoration_timestamps_invalid",
            )
            try:
                fault = LocalParentFault.from_evidence(configuration, path, operator=operator)
            except (KeyError, TypeError, ValueError):
                raise Error("cleanup_fault_journal_invalid") from None
            base.parent_cidrs(target, record["restoration_probe"].get("ips", []))
            attempted.append(fault)
        else:
            require(
                record.get("outcome")
                in {"not_activated", "activation_failed", "restoration_failed"}
                and not record.get("restored")
                and not record.get("physical_restored")
                and "blocked_at" not in record
                and "restoration_started_at" not in record,
                "cleanup_fault_attempt_marker_missing",
            )
        journals.append(
            {
                "file": relative,
                "sha256": snapshots[path],
                "run_id": record["run_id"],
                "slot": slot,
                "creation_attempted": started is True,
            }
        )
    live = {}
    for slot in SLOTS[1:]:
        component = slot.rsplit("-", 1)[1] + "-reconciler"
        fault = LocalParentFault(
            configuration, slot, component, directory / "unused-read-only.json", operator=operator
        )
        fault.verify_nodes()
        pod = fault.kube.pod(component)
        fault.pod_uid = pod["metadata"]["uid"]
        fault.sandbox = fault.sandbox_identity(pod)
        rules = fault.network("iptables", "-w", "2", "-S", "OUTPUT")
        require("plane-demo-fault-" not in rules, "cleanup_live_parent_fault_present")
        rule_hash = hashlib.sha256(rules.encode()).hexdigest()
        for prior in attempted:
            if prior.target.slot == slot:
                require(
                    prior.pod_uid == fault.pod_uid
                    and prior.sandbox == fault.sandbox
                    and prior.original == rule_hash,
                    "cleanup_fault_restored_identity_or_rules_changed",
                )
        fault.verify_sandbox()
        live[slot] = {
            "cluster_uid": fault.target.cluster_uid,
            "namespace_uid": fault.target.namespace_uid,
            "pod_uid": fault.pod_uid,
            "node": fault.node,
            "sandbox": fault.sandbox,
            "output_rules_sha256": rule_hash,
        }
    require(
        paths() == initial_paths
        and all(
            hashlib.sha256(
                configuration.file(
                    str(path.relative_to(configuration.root)), secret=True
                ).read_bytes()
            ).hexdigest()
            == digest
            for path, digest in snapshots.items()
        ),
        "cleanup_fault_journals_changed",
    )
    return {
        "version": 1,
        "environment": "local",
        "project": "radplanes",
        "observed_at": base.utc_now(),
        "journals": journals,
        "targets": live,
    }


if __name__ == "__main__":
    raise SystemExit(base.main())
