#!/usr/bin/env python3
"""Create operator-owned management kind, or install its Radius-only executor overlay."""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
from datetime import UTC, datetime

import yaml
from common import (
    CONTEXT,
    ENVIRONMENT,
    GROUP,
    MANAGEMENT,
    NAMESPACE,
    NODE_IMAGE,
    ROOT,
    STATE,
    Commands,
    LocalError,
    containers,
    docker,
    image_names,
    kube,
    node_address,
    rad,
    write_private,
)
from prepare import prepare

from images import require_inspection


def reserve_ports() -> list[socket.socket]:
    expected = {
        "PORT_BLOCK_START=35490",
        "PORT_BLOCK_END=35499",
    }
    if not expected.issubset(set((ROOT / "ports.env").read_text().splitlines())):
        raise LocalError("ports.env does not reserve this gate's full 35490-35499 block")
    held = []
    try:
        for port in range(35490, 35500):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            held.append(sock)
            sock.bind(("127.0.0.1", port))
    except OSError:
        for sock in held:
            sock.close()
        raise LocalError("A port in 35490-35499 is occupied; no cluster was created") from None
    return held


def management_config(vm_socket: str) -> dict:
    if vm_socket != "/var/run/docker.sock":
        raise LocalError("Only the Docker Desktop VM /var/run/docker.sock candidate is supported")
    return {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "name": MANAGEMENT,
        "networking": {"apiServerAddress": "127.0.0.1", "apiServerPort": 35495},
        "nodes": [
            {
                "role": "control-plane",
                "image": NODE_IMAGE,
                "labels": {"radplanes.local/slot": "management"},
                "extraMounts": [
                    {
                        "hostPath": vm_socket,
                        "containerPath": "/run/radplanes/docker.sock",
                        "readOnly": False,
                    },
                ],
                "extraPortMappings": [
                    {
                        "containerPort": 31480,
                        "hostPort": 35490,
                        "listenAddress": "127.0.0.1",
                        "protocol": "TCP",
                    }
                ],
            }
        ],
    }


def create(commands: Commands, vm_socket: str) -> None:
    require_inspection(commands)
    if containers(commands, MANAGEMENT) or (STATE / "bootstrap-started.json").exists():
        raise LocalError("Management creation already started; no automatic retry or adoption")
    if "kind v0.31.0" not in commands.run(["kind", "version"]):
        raise LocalError("Host kind must be 0.31.0")
    if "0.60.2" not in commands.run(rad("version", "--cli", workspace=False)):
        raise LocalError("Host Radius must be 0.60.2")
    capacity = commands.json(docker("info", "--format", "{{json .}}"))
    held = reserve_ports()
    try:
        config = management_config(vm_socket)
        write_private(STATE / "management/kind.yaml", yaml.safe_dump(config))
        write_private(
            STATE / "bootstrap-started.json",
            {
                "startedAt": datetime.now(UTC).isoformat(),
                "name": MANAGEMENT,
                "containersBefore": containers(commands),
                "reservedPorts": list(range(35490, 35500)),
                "dockerCapacity": {
                    key: capacity[key] for key in ("NCPU", "MemTotal", "Architecture")
                },
            },
        )
    finally:
        for sock in held:
            sock.close()
    # Reservation cannot span Docker's bind. The unavoidable release/create race
    # is fail-closed by Docker; no alternate ports or takeover are attempted.
    commands.run(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            MANAGEMENT,
            "--config",
            str(STATE / "management/kind.yaml"),
            "--kubeconfig",
            str(STATE / "home/.kube/config"),
            "--image",
            NODE_IMAGE,
            "--wait",
            "300s",
        ],
        timeout=600,
    )
    access = STATE / "home/.kube/config"
    access.chmod(0o600)
    commands.run(
        [
            "kubectl",
            "--kubeconfig",
            str(access),
            "--context",
            f"kind-{MANAGEMENT}",
            "config",
            "rename-context",
            f"kind-{MANAGEMENT}",
            CONTEXT,
        ]
    )
    if commands.run(kube("config", "current-context")).strip() != CONTEXT:
        raise LocalError("Project kubeconfig did not select the management context")
    access.chmod(0o600)
    node = commands.json(docker("inspect", "--type", "container", f"{MANAGEMENT}-control-plane"))[0]
    address = node_address(node, MANAGEMENT)
    commands.run(
        docker(
            "exec",
            f"{MANAGEMENT}-control-plane",
            "test",
            "-S",
            "/run/radplanes/docker.sock",
        )
    )
    commands.run(kube("wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s"))
    encryption = commands.json(
        [
            "bash",
            str(ROOT / "scripts/operations/local/encryption.sh"),
            "--cluster",
            MANAGEMENT,
            "--node",
            f"{MANAGEMENT}-control-plane",
            "--kubeconfig",
            str(access),
            "--context",
            CONTEXT,
            "--docker-host",
            commands.env["DOCKER_HOST"],
        ],
        timeout=300,
    )
    if (
        encryption.get("cluster") != MANAGEMENT
        or encryption.get("nodeId") != node["Id"]
        or encryption.get("keyStatus") != "created"
        or encryption.get("syntheticCiphertextVerified") is not True
    ):
        raise LocalError("Management Secret encryption was not verified")
    write_private(
        STATE / "management-created.json",
        {
            "name": MANAGEMENT,
            "context": CONTEXT,
            "nodeId": node["Id"],
            "nodeAddress": address,
            "secretEncryptionVerified": True,
            "createdAt": datetime.now(UTC).isoformat(),
        },
    )


def verify_management(commands: Commands) -> dict:
    owned = json.loads((STATE / "management-created.json").read_text())
    if owned.get("secretEncryptionVerified") is not True:
        raise LocalError("Management bootstrap has no successful Secret-encryption proof")
    node = commands.json(docker("inspect", "--type", "container", f"{MANAGEMENT}-control-plane"))[0]
    node_address(node, MANAGEMENT)
    if node["Id"] != owned["nodeId"]:
        raise LocalError("Management node differs from the operator-owned bootstrap record")
    return node


def verify_encryption(commands: Commands, namespace: str, name: str) -> None:
    response = commands.json(
        kube(
            "-n",
            "kube-system",
            "exec",
            f"etcd-{MANAGEMENT}-control-plane",
            "--",
            "etcdctl",
            "--endpoints=https://127.0.0.1:2379",
            "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
            "--cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt",
            "--key=/etc/kubernetes/pki/etcd/healthcheck-client.key",
            "get",
            f"/registry/secrets/{namespace}/{name}",
            "--write-out=json",
        )
    )
    values = response.get("kvs", [])
    if len(values) != 1 or not base64.b64decode(values[0]["value"]).startswith(
        b"k8s:enc:aescbc:v1:local-key:",
    ):
        raise LocalError("The actual Secret in etcd is missing or not encrypted at rest")


def install(commands: Commands) -> None:
    review = require_inspection(commands)
    verify_management(commands)
    if (STATE / "install-started.json").exists():
        raise LocalError("Radius installation already started; inspect explicitly, do not replay")
    write_private(STATE / "install-started.json", {"startedAt": datetime.now(UTC).isoformat()})
    inputs = prepare()
    for name in image_names().values():
        commands.run(["kind", "load", "docker-image", "--name", MANAGEMENT, name], timeout=300)
    executor = image_names()["executor"]
    commands.run(
        rad(
            "install",
            "kubernetes",
            "--kubecontext",
            CONTEXT,
            "--skip-contour-install",
            "--set",
            f"dynamicrp.image={executor}",
            "--set",
            "dashboard.enabled=false",
            "--set",
            "global.terraform.enabled=false",
            "--set",
            "dynamicrp.buildkit.enabled=false",
            workspace=False,
        ),
        timeout=600,
    )
    deployment = commands.json(
        kube("-n", "radius-system", "get", "deploy", "dynamic-rp", "-o", "json")
    )
    pod_spec = deployment["spec"]["template"]["spec"]
    main = next(c for c in pod_spec["containers"] if c["name"] == "dynamic-rp")
    if (
        len(pod_spec["containers"]) != 1
        or main["image"] != executor
        or main.get("command")
        or pod_spec.get("hostNetwork")
    ):
        raise LocalError("Unexpected dynamic-rp image, entrypoint override, or host network")
    configmap = commands.json(
        kube(
            "-n",
            "radius-system",
            "get",
            "configmap",
            "dynamic-rp-config",
            "-o",
            "json",
        )
    )
    config = yaml.safe_load(configmap["data"]["radius-self-host.yaml"])
    if config["terraform"]["path"] != "/terraform":
        raise LocalError("Unexpected upstream Terraform layout")
    config["terraform"]["logLevel"] = "OFF"
    commands.run(
        kube(
            "-n",
            "radius-system",
            "patch",
            "configmap",
            "dynamic-rp-config",
            "--type=merge",
            "-p",
            json.dumps({"data": {"radius-self-host.yaml": yaml.safe_dump(config)}}),
        )
    )
    patch = yaml.safe_load((ROOT / "scripts/operations/local/dynamic-rp-overlay.yaml").read_text())
    overlay = patch["spec"]["template"]["spec"]
    overlay["initContainers"][0]["image"] = executor
    stat = (
        commands.run(
            docker(
                "exec",
                f"{MANAGEMENT}-control-plane",
                "stat",
                "-c",
                "%u %g %a",
                "/run/radplanes/docker.sock",
            )
        )
        .strip()
        .split()
    )
    if len(stat) != 3:
        raise LocalError("Cannot determine the actual VM socket ownership")
    uid, gid, mode = int(stat[0]), int(stat[1]), int(stat[2], 8)
    if not (uid == 65532 and mode & 0o200) and not (mode & 0o002):
        if not mode & 0o020:
            raise LocalError(
                "Socket is not group-writable; stop for a reviewed permission decision"
            )
        overlay["securityContext"]["supplementalGroups"] = [gid]
    commands.run(
        kube(
            "-n",
            "radius-system",
            "patch",
            "deployment",
            "dynamic-rp",
            "--type=strategic",
            "-p",
            json.dumps(patch),
        )
    )
    commands.run(
        kube(
            "-n",
            "radius-system",
            "rollout",
            "status",
            "deployment/dynamic-rp",
            "--timeout=300s",
        ),
        timeout=330,
    )
    daemon_id = commands.run(
        kube(
            "-n",
            "radius-system",
            "exec",
            "deployment/dynamic-rp",
            "-c",
            "dynamic-rp",
            "--",
            "sh",
            "-ec",
            'test "$(id -u)" = 65532; test "$(id -g)" = 65532; '
            "test -w /terraform; test -S /run/radplanes/docker.sock; "
            "docker --host unix:///run/radplanes/docker.sock info --format '{{.ID}}'",
        )
    ).strip()
    host_daemon_id = commands.run(docker("info", "--format", "{{.ID}}")).strip()
    if not daemon_id or daemon_id != host_daemon_id:
        raise LocalError("The management Pod socket does not address the operator's Docker daemon")
    binary_hash = commands.run(
        kube(
            "-n",
            "radius-system",
            "exec",
            "deployment/dynamic-rp",
            "-c",
            "dynamic-rp",
            "--",
            "sha256sum",
            "/dynamic-rp",
        )
    ).split()[0]
    if binary_hash != review["images"]["executor"]["contents"].split()[0]:
        raise LocalError("The running executor does not contain the inspected Radius binary")
    verify_encryption(commands, "radius-system", "radius-encryption-key")
    commands.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
    commands.run(kube("apply", "-f", str(STATE / "prepared/module-server.json")))
    commands.run(
        kube(
            "-n",
            "radius-system",
            "rollout",
            "status",
            f"deployment/{inputs['moduleServer']}",
            "--timeout=180s",
        ),
        timeout=210,
    )
    commands.run(
        rad(
            "workspace",
            "create",
            "kubernetes",
            MANAGEMENT,
            "--context",
            CONTEXT,
            workspace=False,
        )
    )
    commands.run(rad("group", "create", GROUP))
    commands.run(
        rad(
            "environment",
            "create",
            ENVIRONMENT,
            "--group",
            GROUP,
            "--kubernetes-namespace",
            "radplanes-local-gate",
        )
    )
    commands.run(
        rad(
            "workspace",
            "create",
            "kubernetes",
            MANAGEMENT,
            "--context",
            CONTEXT,
            "--group",
            GROUP,
            "--environment",
            ENVIRONMENT,
            "--force",
            workspace=False,
        )
    )
    commands.run(
        rad(
            "resource-type",
            "create",
            "--from-file",
            str(ROOT / "infra/radius/types/clusters.yaml"),
        )
    )
    commands.run(
        rad(
            "resource",
            "create",
            "Applications.Core/environments",
            ENVIRONMENT,
            "--from-file",
            str(STATE / "prepared/environment.json"),
        ),
        timeout=180,
    )
    write_private(
        STATE / "installed.json",
        {
            **inputs,
            "installedAt": datetime.now(UTC).isoformat(),
            "socket": {"uid": uid, "gid": gid, "mode": oct(mode)},
            "socketDaemonId": daemon_id,
            "supplementalGroups": overlay.get("securityContext", {}).get("supplementalGroups", []),
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["create", "install"])
    parser.add_argument("--vm-docker-socket", default="/var/run/docker.sock")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    "stage": args.stage,
                    "management": MANAGEMENT,
                    "context": CONTEXT,
                    "hostPorts": list(range(35490, 35500)),
                    "execute": False,
                }
            )
        )
        return 0
    os.umask(0o077)
    try:
        commands = Commands()
        if args.stage == "create":
            create(commands, args.vm_docker_socket)
        else:
            install(commands)
        print(f"Management {args.stage} completed; no child gate success is implied.")
        return 0
    except (LocalError, OSError, ValueError, KeyError, StopIteration) as error:
        print(f"Management {args.stage} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
