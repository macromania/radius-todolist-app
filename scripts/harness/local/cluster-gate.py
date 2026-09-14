#!/usr/bin/env python3
"""One-child Radius/kind feasibility gate. No full tenant API or recovery workflow."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import gzip
import json
import os
import re
import secrets
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/operations/local"))

from bootstrap import verify_encryption, verify_management  # noqa: E402
from common import (  # noqa: E402
    ACCESS_SECRET,
    CHILD,
    ENVOY_IMAGE,
    GROUP,
    MANAGEMENT,
    NAMESPACE,
    NODE_IMAGE,
    POSTGRES_IMAGE,
    RESOURCE_ID,
    STATE,
    Commands,
    LocalError,
    containers,
    digest,
    docker,
    image_names,
    kube,
    node_address,
    rad,
    state_secret_name,
    write_private,
)
from prepare import module_archive  # noqa: E402

from images import require_inspection  # noqa: E402

PROCESS_PROBE = """
test "$(readlink /proc/1/exe)" = /dynamic-rp
for exe in /proc/[0-9]*/exe; do
    target=$(readlink "$exe") || continue
    case "$target" in
        /terraform/*terraform-provider-kind*)
            printf '%s %s\\n' "$exe" "$target"
            ;;
    esac
done
"""


def secret(commands: Commands, namespace: str, name: str) -> dict | None:
    value = commands.run(
        kube(
            "-n",
            namespace,
            "get",
            "secret",
            name,
            "--ignore-not-found",
            "-o",
            "json",
        )
    )
    return json.loads(value) if value.strip() else None


def state_summary(stored: dict) -> dict:
    if stored["metadata"]["name"] != state_secret_name():
        raise LocalError("Unexpected Radius Terraform state Secret")
    labels = stored["metadata"].get("labels", {})
    if labels.get("tfstate") != "true" or labels.get("app.kubernetes.io/managed-by") != "terraform":
        raise LocalError("State Secret was not written by the Terraform Kubernetes backend")
    state = json.loads(gzip.decompress(base64.b64decode(stored["data"]["tfstate"], validate=True)))
    resources = state["resources"]
    kinds = [r for r in resources if r["type"] == "kind_cluster" and r["mode"] == "managed"]
    if len(kinds) != 1 or len(kinds[0]["instances"]) != 1:
        raise LocalError("State does not contain exactly one managed kind cluster")
    attrs = kinds[0]["instances"][0]["attributes"]
    if (
        attrs["name"] != CHILD
        or attrs["node_image"] != NODE_IMAGE
        or attrs["id"] != f"{CHILD}-{NODE_IMAGE}"
        or not attrs["completed"]
        or not attrs["kubeconfig"]
        or not attrs["client_key"]
    ):
        raise LocalError("Terraform state does not describe the completed gate child")
    accesses = [
        r for r in resources if r["type"] == "kubernetes_secret_v1" and r["mode"] == "managed"
    ]
    if len(accesses) != 1:
        raise LocalError("Terraform did not track the expected protected access Secret")
    metadata = accesses[0]["instances"][0]["attributes"]["metadata"][0]
    if metadata["name"] != ACCESS_SECRET or metadata["namespace"] != NAMESPACE:
        raise LocalError("Terraform tracked a different access Secret")
    if state["terraform_version"] != "1.15.8" or state["serial"] < 1 or not state["lineage"]:
        raise LocalError("Unexpected Terraform version or unpersisted state")
    return {
        "secret": state_secret_name(),
        "uid": stored["metadata"]["uid"],
        "serial": state["serial"],
        "terraformVersion": state["terraform_version"],
        "kindResourceId": attrs["id"],
        "managedAccessSecret": ACCESS_SECRET,
    }


def resource(commands: Commands) -> dict:
    return commands.json(
        rad(
            "resource",
            "show",
            "Demo.Platform/clusters",
            "shared-control",
            "--group",
            GROUP,
            "-o",
            "json",
        )
    )


def check_absent(commands: Commands) -> None:
    if containers(commands, CHILD):
        raise LocalError("Child Docker containers already exist; no interrupted-create adoption")
    items = commands.json(
        rad(
            "resource",
            "list",
            "Demo.Platform/clusters",
            "--group",
            GROUP,
            "-o",
            "json",
        )
    )
    if not isinstance(items, list):
        raise LocalError("Unexpected Radius resource-list response")
    if any(item["name"] == "shared-control" for item in items):
        raise LocalError("Child Radius resource already exists; creation will not be retried")
    if secret(commands, "radius-system", state_secret_name()) or secret(
        commands,
        NAMESPACE,
        ACCESS_SECRET,
    ):
        raise LocalError("Gate state/access Secret already exists; do not adopt or edit state")


def executor_pod(commands: Commands) -> dict:
    pods = commands.json(
        kube(
            "-n",
            "radius-system",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=dynamic-rp",
            "-o",
            "json",
        )
    )["items"]
    pods = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    if len(pods) != 1:
        raise LocalError("Exactly one live management dynamic-rp Pod is required")
    pod = pods[0]
    spec = pod["spec"]
    container = next(c for c in spec["containers"] if c["name"] == "dynamic-rp")
    variables = {e["name"]: e.get("value") for e in container.get("env", [])}
    if (
        spec.get("hostPID")
        or spec.get("hostNetwork")
        or container.get("command")
        or container["image"] != image_names()["executor"]
        or variables.get("RADIUS_LOGGING_LEVEL") != "error"
        or variables.get("DOCKER_HOST") != "unix:///run/radplanes/docker.sock"
        or container["securityContext"].get("runAsUser") != 65532
    ):
        raise LocalError("Management dynamic-rp does not match the protected executor overlay")
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
    if yaml.safe_load(configmap["data"]["radius-self-host.yaml"])["terraform"] != {
        "path": "/terraform",
        "logLevel": "OFF",
    }:
        raise LocalError("Terraform logging/layout differs from the reviewed local installation")
    return pod


def create_child(commands: Commands, pod: dict) -> str:
    def submit_and_wait() -> None:
        deadline = time.monotonic() + 900
        commands.create_cluster_resource()
        while time.monotonic() < deadline:
            current = resource(commands)
            if time.monotonic() >= deadline:
                break
            if current.get("id", "").lower() != RESOURCE_ID.lower():
                raise LocalError("Radius creation returned a different resource identity")
            state = current.get("properties", {}).get("provisioningState")
            if state == "Succeeded":
                return
            if state not in {"Accepted", "Creating", "Updating"}:
                raise LocalError(f"Radius child creation did not succeed: {state}")
            time.sleep(2)
        raise LocalError("Child creation deadline exceeded; Radius execution may continue")

    observed = ""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        creation = pool.submit(submit_and_wait)
        while not creation.done():
            output = commands.run(
                kube(
                    "-n",
                    "radius-system",
                    "exec",
                    pod["metadata"]["name"],
                    "-c",
                    "dynamic-rp",
                    "--",
                    "sh",
                    "-ec",
                    PROCESS_PROBE,
                ),
                timeout=40,
            )
            if "terraform-provider-kind" in output:
                observed = output.strip()
            time.sleep(2)
        creation.result()
    return observed


def parent_postgres(run_id: str, password: str) -> list[dict]:
    name = f"gate-{run_id}"
    metadata = {
        "name": name,
        "namespace": NAMESPACE,
        "labels": {"radplanes.local/gate-run": run_id},
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": metadata,
            "type": "Opaque",
            "data": {"password": base64.b64encode(password.encode()).decode()},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata,
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {
                        "labels": {
                            "app": name,
                            "radplanes.local/gate-run": run_id,
                        }
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "postgres",
                                "image": POSTGRES_IMAGE,
                                "args": ["-c", "ssl=off"],
                                "env": [
                                    {"name": "POSTGRES_DB", "value": "gate"},
                                    {"name": "POSTGRES_USER", "value": "gate"},
                                    {
                                        "name": "POSTGRES_PASSWORD",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": name,
                                                "key": "password",
                                            }
                                        },
                                    },
                                    {"name": "POSTGRES_HOST_AUTH_METHOD", "value": "scram-sha-256"},
                                ],
                                "readinessProbe": {
                                    "exec": {"command": ["pg_isready", "-U", "gate", "-d", "gate"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "1", "memory": "256Mi"},
                                },
                                "volumeMounts": [
                                    {"name": "data", "mountPath": "/var/lib/postgresql/data"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "data", "emptyDir": {}},
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata,
            "spec": {
                "type": "NodePort",
                "selector": {"app": name},
                "ports": [{"port": 5432, "targetPort": 5432, "nodePort": 31543}],
            },
        },
    ]


def bootstrap_job(run_id: str, parent_address: str, child_address: str) -> list[dict]:
    name = f"gate-{run_id}"
    metadata = {
        "name": name,
        "namespace": NAMESPACE,
        "labels": {"radplanes.local/gate-run": run_id},
    }
    inputs = {
        "runId": run_id,
        "parentAddress": parent_address,
        "childAddress": child_address,
        "postgresImage": POSTGRES_IMAGE,
        "envoyImage": ENVOY_IMAGE,
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "immutable": True,
            "data": {
                "bootstrap-child.py": (
                    ROOT / "scripts/operations/local/bootstrap-child.py"
                ).read_text(),
                "inputs.json": json.dumps(inputs),
            },
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": metadata,
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 1200,
                "template": {
                    "metadata": {"labels": {"radplanes.local/gate-run": run_id}},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsUser": 65532,
                            "runAsGroup": 65532,
                            "runAsNonRoot": True,
                            "fsGroup": 65532,
                            "fsGroupChangePolicy": "OnRootMismatch",
                        },
                        "containers": [
                            {
                                "name": "bootstrap",
                                "image": image_names()["operator"],
                                "imagePullPolicy": "Never",
                                "command": ["python3", "/scripts/bootstrap-child.py", "--execute"],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "1", "memory": "512Mi"},
                                },
                                "env": [
                                    {"name": "HOME", "value": "/work/home"},
                                    {"name": "TMPDIR", "value": "/work"},
                                ],
                                "volumeMounts": [
                                    {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
                                    {"name": "access", "mountPath": "/access", "readOnly": True},
                                    {
                                        "name": "password",
                                        "mountPath": "/password",
                                        "readOnly": True,
                                    },
                                    {"name": "work", "mountPath": "/work"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "scripts", "configMap": {"name": name}},
                            {
                                "name": "access",
                                "secret": {
                                    "secretName": ACCESS_SECRET,
                                    "defaultMode": 0o440,
                                },
                            },
                            {
                                "name": "password",
                                "secret": {
                                    "secretName": name,
                                    "defaultMode": 0o440,
                                },
                            },
                            {
                                "name": "work",
                                "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"},
                            },
                        ],
                    },
                },
            },
        },
    ]


def create_objects(commands: Commands, objects: list[dict]) -> None:
    commands.run(
        kube("create", "-f", "-"),
        data=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": objects,
            }
        ),
    )


def verify_ports(node: dict) -> None:
    bindings = node["HostConfig"]["PortBindings"]
    expected = {"6443/tcp": "35496", "31480/tcp": "35491"}
    if set(bindings) != set(expected):
        raise LocalError("Child Docker node has unexpected published ports")
    for port, host_port in expected.items():
        if bindings[port] != [{"HostIp": "127.0.0.1", "HostPort": host_port}]:
            raise LocalError("Child port is not bound exclusively to its reserved loopback port")


def child_checks(commands: Commands, record: dict) -> None:
    name = f"gate-{record['runId']}"
    all_services = commands.json(kube("get", "services", "-A", "-o", "json"))["items"]
    if any(p.get("nodePort") == 31543 for s in all_services for p in s["spec"].get("ports", [])):
        raise LocalError("Parent PostgreSQL NodePort 31543 is already allocated")
    create_objects(commands, parent_postgres(record["runId"], secrets.token_urlsafe(32)))
    commands.run(
        kube(
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deployment/{name}",
            "--timeout=180s",
        ),
        timeout=210,
    )
    create_objects(
        commands,
        bootstrap_job(
            record["runId"],
            record["parentAddress"],
            record["childAddress"],
        ),
    )
    commands.run(
        kube(
            "-n",
            NAMESPACE,
            "wait",
            "--for=condition=complete",
            f"job/{name}",
            "--timeout=1210s",
        ),
        timeout=1240,
    )
    result = json.loads(commands.run(kube("-n", NAMESPACE, "logs", f"job/{name}")))
    required = (
        "childTLS",
        "wrongServerNameRejected",
        "childRadius",
        "radiusWorkload",
        "envoyReady",
    )
    if result.get("runId") != record["runId"] or not all(result.get(k) is True for k in required):
        raise LocalError("Management Pod did not prove every child bootstrap step")
    if result.get("parentPostgreSQL") != {
        "authenticated": True,
        "tlsRequired": False,
        "nodePort": 31543,
    }:
        raise LocalError("Child-to-parent password-authenticated PostgreSQL proof is missing")
    record["childProof"] = result
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:35491/", timeout=15) as response:
        if response.status != 200 or response.read(256).decode() != (
            f"local-cluster-gate:{record['runId']}"
        ):
            raise LocalError("Reserved loopback Envoy response does not match this gate run")
    record["envoyHostPort"] = 35491


def delete_child(commands: Commands, record: dict) -> None:
    verify_management(commands)
    actual = resource(commands)
    if actual["id"].lower() != RESOURCE_ID.lower() or actual["properties"].get(
        "provisioningState",
    ) not in {"Succeeded", "Failed"}:
        raise LocalError("Refusing deletion of an unexpected or still-running Radius resource")
    summary = state_summary(secret(commands, "radius-system", state_secret_name()) or {})
    if summary["uid"] != record["terraformState"]["uid"]:
        raise LocalError("State Secret changed ownership since this gate observed it")
    node = commands.json(docker("inspect", "--type", "container", f"{CHILD}-control-plane"))[0]
    node_address(node, CHILD)
    if node["Id"] != record["childNodeId"]:
        raise LocalError("Child Docker node changed since the gate; deletion refused")
    executor_pod(commands)
    commands.run(
        rad(
            "resource",
            "delete",
            "Demo.Platform/clusters",
            "shared-control",
            "--group",
            GROUP,
            "--yes",
        ),
        timeout=600,
    )
    check_absent(commands)
    remaining = set(containers(commands))
    if not set(record["containersBefore"]).issubset(remaining):
        raise LocalError("A pre-existing Docker container disappeared during the gate")
    if remaining - set(record["containersBefore"]):
        raise LocalError("Unexpected extra Docker containers remain; inspect without deleting them")
    verify_management(commands)
    record["childDeletedThroughRadius"] = True
    record["childDockerAndKubernetesAbsent"] = True
    check_logs(commands, record)


def check_logs(commands: Commands, record: dict) -> None:
    pod = executor_pod(commands)
    if pod["metadata"]["uid"] != record["executorPod"]["uid"]:
        raise LocalError("Executor Pod restarted; invocation attribution is incomplete")
    logs = commands.run(
        kube(
            "-n",
            "radius-system",
            "logs",
            pod["metadata"]["name"],
            "-c",
            "dynamic-rp",
            "--since-time",
            record["startedAt"],
        )
    )
    if any(
        marker in logs
        for marker in (
            "PRIVATE KEY",
            "client-key-data",
            '"client_key"',
            "client_key =",
        )
    ):
        raise LocalError("Possible credential content in Radius logs; do not export the logs")
    record["credentialLogCheck"] = "no-key-markers-observed"


def cleanup_fixtures(commands: Commands, record: dict) -> None:
    name = f"gate-{record['runId']}"
    for kind in ("job", "deployment", "service", "configmap", "secret"):
        value = commands.run(
            kube(
                "-n",
                NAMESPACE,
                "get",
                kind,
                name,
                "--ignore-not-found",
                "-o",
                "json",
            )
        )
        if not value.strip():
            continue
        obj = json.loads(value)
        if obj["metadata"].get("labels", {}).get("radplanes.local/gate-run") != record["runId"]:
            raise LocalError("Fixture ownership mismatch; cleanup refused")
        commands.run(
            kube(
                "-n",
                NAMESPACE,
                "delete",
                kind,
                name,
                "--wait=true",
                "--timeout=90s",
                "--cascade=foreground",
            )
        )
    remaining = commands.json(
        kube(
            "-n",
            NAMESPACE,
            "get",
            "pods",
            "-l",
            f"radplanes.local/gate-run={record['runId']}",
            "-o",
            "json",
        )
    )["items"]
    if remaining:
        raise LocalError("Run-labelled management fixture Pods remain after foreground deletion")


def run_gate(commands: Commands, record: dict, path: Path) -> None:
    require_inspection(commands)
    management = verify_management(commands)
    installed = json.loads((STATE / "installed.json").read_text())
    if installed["moduleSHA256"] != digest(module_archive()):
        raise LocalError("Recipe source differs from the installed immutable module")
    check_absent(commands)
    verify_encryption(commands, "radius-system", "radius-encryption-key")
    protected_namespaces = ("radius-system", NAMESPACE)
    for namespace in protected_namespaces:
        for account_namespace in ("default", *protected_namespaces):
            for verb in ("get", "list", "watch"):
                denial = commands.run(
                    kube(
                        "auth",
                        "can-i",
                        verb,
                        "secrets",
                        "-n",
                        namespace,
                        f"--as=system:serviceaccount:{account_namespace}:default",
                    ),
                    expected_codes=(0, 1),
                ).strip()
                if denial != "no":
                    raise LocalError(
                        f"Default service account {account_namespace}:default can {verb} "
                        f"protected Secrets in {namespace}"
                    )
    pod = executor_pod(commands)
    record.update(
        {
            "containersBefore": containers(commands),
            "parentAddress": node_address(management, MANAGEMENT),
            "executorPod": {"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"]},
            "moduleSHA256": installed["moduleSHA256"],
        }
    )
    write_private(path, record)
    record["observedKindProvider"] = create_child(commands, pod)
    actual = resource(commands)
    properties = actual["properties"]
    if (
        properties.get("provisioningState") != "Succeeded"
        or properties.get("clusterId") != f"kind://{CHILD}"
        or properties.get("clusterName") != CHILD
        or properties.get("bootstrapAccessRef")
        != f"kubernetes://{NAMESPACE}/{ACCESS_SECRET}#kubeconfig"
        or any(
            properties.get(k)
            for k in (
                "resourceGroup",
                "fqdn",
                "oidcIssuer",
                "radiusIdentityId",
                "radiusClientId",
            )
        )
    ):
        raise LocalError("Custom cluster outputs do not match the local contract")
    stored = secret(commands, "radius-system", state_secret_name())
    record["terraformState"] = state_summary(stored or {})
    verify_encryption(commands, "radius-system", state_secret_name())
    access = secret(commands, NAMESPACE, ACCESS_SECRET)
    if (
        not access
        or access["metadata"]
        .get("annotations", {})
        .get(
            "radplanes.local/radius-resource",
            "",
        )
        .lower()
        != RESOURCE_ID.lower()
    ):
        raise LocalError("Protected access Secret is not linked to this Radius resource")
    verify_encryption(commands, NAMESPACE, ACCESS_SECRET)
    node = commands.json(docker("inspect", "--type", "container", f"{CHILD}-control-plane"))[0]
    verify_ports(node)
    record.update({"childNodeId": node["Id"], "childAddress": node_address(node, CHILD)})
    write_private(path, record)
    if not record["observedKindProvider"]:
        raise LocalError("No actual kind-provider process was observed in management dynamic-rp")
    check_logs(commands, record)
    child_checks(commands, record)
    write_private(path, record)
    delete_child(commands, record)
    cleanup_fixtures(commands, record)
    record["status"] = "passed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["run", "delete"])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--run", type=Path, help="Existing private gate record for explicit deletion"
    )
    args = parser.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    "stage": args.stage,
                    "execute": False,
                    "child": CHILD,
                    "scope": "one-child feasibility only",
                    "noLiveProof": True,
                }
            )
        )
        return 0
    os.umask(0o077)
    run_id = secrets.token_hex(6)
    path = STATE / "runs" / f"{run_id}.json"
    record = {
        "runId": run_id,
        "status": "running",
        "startedAt": datetime.now(UTC).isoformat(),
        "scope": "local-milestone-5-one-child",
        "resourceId": RESOURCE_ID,
    }
    deleting_existing = False
    try:
        if args.stage == "delete":
            if (
                args.run is None
                or args.run.is_symlink()
                or not args.run.resolve().is_relative_to((STATE / "runs").resolve())
            ):
                raise LocalError("Deletion requires an existing private local gate run record")
            existing = json.loads(args.run.read_text())
            if (
                existing["resourceId"] != RESOURCE_ID
                or not re.fullmatch(r"[a-f0-9]{12}", existing["runId"])
                or args.run.name != f"{existing['runId']}.json"
            ):
                raise LocalError("Gate run belongs to an unexpected resource")
            path, record = args.run, existing
            deleting_existing = True
        elif args.run:
            raise LocalError("Create does not resume an existing gate record")
        write_private(path, record)
        commands = Commands()
        commands.deadline = time.monotonic() + 2700
        if args.stage == "run":
            run_gate(commands, record, path)
        else:
            delete_child(commands, record)
            cleanup_fixtures(commands, record)
            record["cleanupStatus"] = "passed"
        record["finishedAt"] = datetime.now(UTC).isoformat()
        write_private(path, record)
        print(
            json.dumps(
                {
                    "record": str(path),
                    "status": record["status"],
                    "cleanupStatus": record.get("cleanupStatus"),
                }
            )
        )
        return 0
    except (
        LocalError,
        OSError,
        ValueError,
        KeyError,
        StopIteration,
        urllib.error.URLError,
    ) as error:
        if deleting_existing:
            record.update(
                {
                    "cleanupStatus": "failed",
                    "cleanupFailedAt": datetime.now(UTC).isoformat(),
                    "cleanupError": str(error),
                }
            )
        else:
            record.update(
                {
                    "status": "failed",
                    "failedAt": datetime.now(UTC).isoformat(),
                    "error": str(error),
                    "operatorAction": (
                        "Inspect exact resources; do not retry/adopt an interrupted create. "
                        "A reporting timeout does not cancel Radius. "
                        "No automatic repair is performed."
                    ),
                }
            )
        write_private(path, record)
        print(f"Local gate failed; private record: {path}; {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
