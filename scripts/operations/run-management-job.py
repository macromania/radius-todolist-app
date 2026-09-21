#!/usr/bin/env python3
"""Start one management deployment from an in-cluster operator Job."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from config import ROOT, load_config
from output import run_main, status

sys.path.insert(0, str(ROOT / "src"))
from plane_demo.management.providers.identity import SECRET_KEYS, DemoConfig  # noqa: E402
from plane_demo.management.provisioning import OperatorConfig  # noqa: E402


class CommandError(RuntimeError):
    pass


def require_confirmation(environment: str) -> None:
    if environment != "azure" or os.environ.get("CONFIRM_AZURE") != "yes":
        raise CommandError("Azure mutation requires CONFIRM_AZURE=yes")


def resources(
    config: dict,
    name: str,
    provided_keys: dict[str, str] | None = None,
    *,
    lease_uid: str = "",
) -> list[dict]:
    selected = OperatorConfig.from_dict(config) if "bootstrapIdentity" in config else None
    namespace = selected.namespace("management") if selected else "radplanes-management-management"
    identity = config["coordinatorIdentity"]
    labels = {"project": "radplanes", "plane-demo/operator": "management-deploy"}
    if selected:
        allowed = {"deploy-management"}
        if selected.prepared_environments:
            allowed.update(f"prepare-{item['pair_id']}" for item in selected.pair_slots)
            allowed.update(
                f"retire-{item['pair_id']}"
                for item in selected.pair_slots
                if item["pair_id"] != "shared"
            )
        if name not in allowed:
            raise ValueError("Selected management deployment uses one fixed operator Job")
        if selected.identity is None:
            raise ValueError("Selected deployment identity is missing")
        labels.update(
            {
                "project": selected.identity.project,
                "plane-demo/project": selected.identity.project,
                "plane-demo/deployment": selected.identity.deployment,
                "plane-demo/environment": selected.identity.environment,
            }
        )
    result = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": labels},
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": "provisioner",
                "namespace": namespace,
                "labels": labels,
                "annotations": {
                    "azure.workload.identity/client-id": identity["clientId"],
                    "azure.workload.identity/tenant-id": config["foundation"]["tenantId"],
                },
            },
        },
        {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": "radplanes-provisioner"},
            "provisioner": "disk.csi.azure.com",
            "reclaimPolicy": "Delete",
            "volumeBindingMode": "WaitForFirstConsumer",
            "allowVolumeExpansion": True,
            "parameters": {
                "skuName": "StandardSSD_LRS",
                "tags": "SecurityControl=Ignore,project=radplanes,managedBy=radius-todolist-app",
            },
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "operator-state", "namespace": namespace, "labels": labels},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "radplanes-provisioner",
                "resources": {"requests": {"storage": "8Gi"}},
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{name}-config", "namespace": namespace, "labels": labels},
            "immutable": True,
            "data": {"provisioning.json": json.dumps(config)},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 3600,
                "template": {
                    "metadata": {"labels": {**labels, "azure.workload.identity/use": "true"}},
                    "spec": {
                        "serviceAccountName": "provisioner",
                        "restartPolicy": "Never",
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "fsGroup": 10001,
                            "fsGroupChangePolicy": "OnRootMismatch",
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "operator",
                                "image": config["images"]["provisioner"],
                                "command": [
                                    "python",
                                    "scripts/operations/deploy-plane.py",
                                    "--slot",
                                    "management",
                                    "--config",
                                    "/gate/provisioning.json",
                                ],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {"name": "config", "mountPath": "/gate", "readOnly": True},
                                    {"name": "state", "mountPath": "/app/.state"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": f"{name}-config"}},
                            {
                                "name": "state",
                                "persistentVolumeClaim": {"claimName": "operator-state"},
                            },
                        ],
                    },
                },
            },
        },
    ]
    if selected:
        keys = provided_keys or {}
        if set(keys) - SECRET_KEYS.keys():
            raise ValueError("Unsupported bootstrap credential input")
        DemoConfig.from_values({**selected.bootstrap_settings, **keys})
        result = [
            value
            for value in result
            if value["kind"] not in {"StorageClass", "PersistentVolumeClaim"}
        ]
        job = result[-1]
        job["metadata"]["annotations"] = {
            "plane-demo/config-sha256": hashlib.sha256(
                json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        job["spec"].update(suspend=True, parallelism=1, completions=1)
        pod = job["spec"]["template"]["spec"]
        pod["automountServiceAccountToken"] = True
        pod["volumes"] = [value for value in pod["volumes"] if value["name"] != "state"]
        container = pod["containers"][0]
        container["volumeMounts"] = [
            value for value in container["volumeMounts"] if value["name"] != "state"
        ]
        container["env"] = [
            {
                "name": "OPERATOR_JOB_UID",
                "valueFrom": {
                    "fieldRef": {
                        "apiVersion": "v1",
                        "fieldPath": "metadata.labels['batch.kubernetes.io/controller-uid']",
                    }
                },
            }
        ]
        if selected.prepared_environments:
            container["env"] += [
                {"name": "OPERATOR_JOB_NAME", "value": name},
                {"name": "OPERATOR_LEASE_UID", "value": lease_uid},
                {
                    "name": "OPERATOR_POD_UID",
                    "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}},
                },
            ]
            if name != "deploy-management":
                action, pair = name.split("-", 1)
                container["command"] = [
                    "python",
                    "scripts/operations/prepare-environment.py",
                    "--pair",
                    pair,
                    "--config",
                    "/gate/provisioning.json",
                    *(["--retire"] if action == "retire" else []),
                ]
        if keys:
            result.insert(
                -1,
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "type": "Opaque",
                    "immutable": True,
                    "metadata": {"name": f"{name}-keys", "namespace": namespace, "labels": labels},
                    "stringData": keys,
                },
            )
            container["envFrom"] = [{"secretRef": {"name": f"{name}-keys"}}]
        result[1:1] = [
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {
                    "name": "management-bootstrap-observer",
                    "namespace": namespace,
                    "labels": labels,
                },
                "rules": [
                    {
                        "apiGroups": ["batch"],
                        "resources": ["jobs"],
                        "resourceNames": [name],
                        "verbs": ["get"],
                    },
                    {
                        "apiGroups": ["apps"],
                        "resources": ["deployments"],
                        "resourceNames": ["provisioner"],
                        "verbs": ["get"],
                    },
                    {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {
                    "name": "management-bootstrap-observer",
                    "namespace": namespace,
                    "labels": labels,
                },
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": "management-bootstrap-observer",
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": "provisioner", "namespace": namespace}
                ],
            },
        ]
        if selected.prepared_environments:
            role, binding = result[1:3]
            role["metadata"]["name"] = f"{name}-observer"
            binding["metadata"]["name"] = f"{name}-observer"
            binding["roleRef"]["name"] = f"{name}-observer"
            role["rules"] += [
                {
                    "apiGroups": ["coordination.k8s.io"],
                    "resources": ["leases"],
                    "resourceNames": ["environment-operator"],
                    "verbs": ["get"],
                },
                {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["create"]},
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": [f"{name}-attempt"],
                    "verbs": ["get"],
                },
                {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get"]},
            ]
    return result


def unfinished_operator(job: dict) -> bool:
    if job["metadata"]["namespace"] != "radplanes-management-management":
        return False
    return not any(
        condition.get("type") in {"Complete", "Failed"} and condition.get("status") == "True"
        for condition in job.get("status", {}).get("conditions", [])
    )


def job_progress(job: dict) -> tuple[str, str]:
    conditions = job.get("status", {}).get("conditions", [])
    for condition in conditions:
        if (
            condition.get("type") in {"Failed", "FailureTarget"}
            and condition.get("status") == "True"
        ):
            return "failed", condition.get("reason") or "unspecified reason"
    if any(
        condition.get("type") == "Complete" and condition.get("status") == "True"
        for condition in conditions
    ):
        return "complete", ""
    return ("active" if job.get("status", {}).get("active", 0) else "waiting"), ""


def job_failure(name: str, reason: str) -> str:
    return (
        f"The canonical operator Job failed: {name} ({reason}). "
        f"Read logs with: make kube ARGS='management logs job/{name} "
        "--all-containers=true --tail=80'"
    )


@contextmanager
def job_logs(base, namespace, name, *, enabled):
    if not enabled:
        yield
        return
    try:
        process = subprocess.Popen(
            [
                *base,
                "--request-timeout=0",
                "-n",
                namespace,
                "logs",
                f"job/{name}",
                "--all-containers=true",
                "--follow",
                "--pod-running-timeout=300s",
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            start_new_session=True,
        )
    except OSError as error:
        status(
            "warning",
            f"Job {name}: cannot stream logs ({type(error).__name__}); use make kube logs",
        )
        yield
        return
    try:
        yield
    finally:
        try:
            result = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        else:
            if result != 0:
                status("warning", f"Job {name}: log stream exited {result}; inspect scoped logs")


def execute(
    arguments: list[str],
    *,
    value: dict | None = None,
    timeout: int = 120,
    capture: bool = True,
) -> str:
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        text=True,
        input=json.dumps(value) if value is not None else None,
        stdout=subprocess.PIPE if capture else sys.stderr,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise CommandError(
            f"Management operation failed: {arguments[0]} (exit {result.returncode})"
        )
    return result.stdout.strip() if capture else ""


def live_configuration(identity: DemoConfig) -> OperatorConfig:
    if identity.environment != "azure" or identity.subscription is None:
        raise ValueError("This entrypoint requires the Azure .env selection")
    dirty = execute(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "src",
            "sql",
            "images",
            "scripts",
            "infra",
            "pyproject.toml",
            "uv.lock",
            ".dockerignore",
        ]
    )
    if dirty:
        raise ValueError("Commit verified deployment inputs before management deployment")
    checkout_revision = execute(["git", "rev-parse", "HEAD"])
    revision = execute(["git", "rev-parse", identity.revision or "HEAD"])
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or revision != checkout_revision:
        raise ValueError("Use a checkout matching the selected deployment revision")
    artifacts = json.loads(
        execute(
            ["bash", str(ROOT / "scripts/operations/azure/build.sh"), "--inspect"],
            timeout=3600,
        )
    )
    if (
        artifacts.get("source_revision") != revision
        or artifacts.get("status") != "artifacts_verified"
        or artifacts.get("content_verified") is not True
    ):
        raise ValueError("Canonical artifact inspection did not verify the selected source")
    deployment = json.loads(
        execute(
            [
                "az",
                "deployment",
                "sub",
                "show",
                "--subscription",
                identity.subscription,
                "--name",
                f"{identity.stem}-bootstrap",
                "--output",
                "json",
                "--only-show-errors",
            ]
        )
    )
    if deployment["properties"].get("provisioningState") != "Succeeded":
        raise ValueError("The selected foundation is not complete")
    outputs = {key: item["value"] for key, item in deployment["properties"]["outputs"].items()}
    selected = DemoConfig.from_values(
        {
            **identity.values(include_secrets=True),
            "DEMO_REVISION": revision,
        }
    )
    value = {
        "version": 1,
        **outputs,
        "allocations": {item["slot"]: item for item in outputs["allocations"]},
        "recipes": artifacts["recipes"],
        "images": artifacts["images"],
        "bootstrapIdentity": selected.public_values(),
    }
    config = OperatorConfig.from_dict(value, identity=selected)
    allocation = config.allocation("management")
    expected_id = (
        allocation["clusterResourceGroupId"]
        + "/providers/Microsoft.ContainerService/managedClusters/"
        + allocation["clusterName"]
    )
    if any(
        config.management_cluster.get(key) != expected
        for key, expected in {
            "id": expected_id,
            "name": allocation["clusterName"],
            "resourceGroup": allocation["clusterResourceGroup"],
        }.items()
    ):
        raise ValueError("Foundation management target does not match the selected allocation")
    return config


def management_access(identity: DemoConfig, workspace: Path) -> tuple[str, str]:
    if identity.environment != "azure" or identity.subscription is None:
        raise ValueError("Azure management access requires a selected subscription")
    workspace = workspace.resolve()
    script = (
        'set -euo pipefail; source "$1/scripts/lib/env.sh"; '
        'source "$1/scripts/lib/discovery.sh"; demo_load_env "$1/.env"; '
        '[[ "$DEMO_ENV" == azure && "$DEMO_PROJECT" == "$3" && '
        '"$DEMO_DEPLOYMENT" == "$4" && "$AZURE_SUBSCRIPTION_ID" == "$5" ]] || '
        '{ demo_error "Configuration changed during deployment"; exit 1; }; '
        'DEMO_WORKSPACE="$2"; demo_open_cluster management; '
        'jq -n --arg context "$DEMO_CONTEXT" --arg kubeconfig "$DEMO_KUBECONFIG" '
        "'{context:$context,kubeconfig:$kubeconfig}'"
    )
    access = json.loads(
        execute(
            [
                "bash",
                "-c",
                script,
                "management-access",
                str(ROOT),
                str(workspace),
                identity.project,
                identity.deployment,
                identity.subscription,
            ]
        )
    )
    path = Path(access["kubeconfig"]).resolve()
    if access["context"] != identity.slot_name("management") or not path.is_relative_to(workspace):
        raise ValueError("Management access does not match the selected workspace")
    return access["context"], str(path)


def deploy_selected(
    config: OperatorConfig,
    context: str,
    kubeconfig: str,
    *,
    name: str = "deploy-management",
    on_job: Callable[[dict], None] | None = None,
    lease_uid: str = "",
) -> dict:
    namespace = config.namespace("management")
    if config.identity is None:
        raise ValueError("Selected deployment identity is required")
    keys = {
        key: config.identity.demo_keys[slot]
        for key, slot in SECRET_KEYS.items()
        if slot in config.identity.demo_keys
    }
    if config.prepared_environments and (not lease_uid or on_job is None):
        raise ValueError("Prepared deployments require an owned operator Lease")
    desired = resources(config.to_dict(), name, keys, lease_uid=lease_uid)
    namespace_resource, job_resource = desired[0], desired[-1]
    labels = job_resource["metadata"]["labels"]
    base = ["kubectl", "--kubeconfig", kubeconfig, "--context", context, "--request-timeout=30s"]

    def read(kind: str, resource_name: str, *, namespaced: bool = True) -> dict | None:
        output = execute(
            [
                *base,
                *(["-n", namespace] if namespaced else []),
                "get",
                kind,
                resource_name,
                "--ignore-not-found",
                "-o",
                "json",
            ]
        )
        return json.loads(output) if output else None

    existing_namespace = read("namespace", namespace, namespaced=False)
    if existing_namespace is None:
        execute([*base, "create", "-f", "-"], value=namespace_resource)
    elif existing_namespace["metadata"]["name"] != namespace or any(
        existing_namespace["metadata"].get("labels", {}).get(key) != expected
        for key, expected in labels.items()
        if key in {"plane-demo/project", "plane-demo/deployment", "plane-demo/environment"}
    ):
        raise ValueError("Management namespace ownership mismatch")
    job = read("job", name)
    if job is None:
        job = json.loads(execute([*base, "create", "-f", "-", "-o", "json"], value=job_resource))
    metadata = job["metadata"]
    actual_containers = job["spec"]["template"]["spec"]["containers"]
    expected_container = job_resource["spec"]["template"]["spec"]["containers"][0]
    if (
        metadata["name"] != name
        or metadata["namespace"] != namespace
        or any(metadata.get("labels", {}).get(key) != expected for key, expected in labels.items())
        or metadata.get("annotations", {}).get("plane-demo/config-sha256")
        != job_resource["metadata"]["annotations"]["plane-demo/config-sha256"]
        or not metadata.get("uid")
        or metadata.get("deletionTimestamp")
        or len(actual_containers) != 1
        or any(
            actual_containers[0].get(field, [] if field in {"env", "envFrom"} else None)
            != expected_container.get(field, [] if field in {"env", "envFrom"} else None)
            for field in ("name", "image", "command", "env", "envFrom")
        )
    ):
        raise ValueError("Existing operator Job does not match the selected deployment")
    owner = {"apiVersion": "batch/v1", "kind": "Job", "name": name, "uid": metadata["uid"]}

    def verify_input(resource: dict, current: dict) -> None:
        if (
            current["metadata"].get("ownerReferences") != [owner]
            or current.get("immutable") is not True
        ):
            raise ValueError("Bootstrap input ownership mismatch")
        if resource["kind"] == "ConfigMap":
            if json.loads(current["data"]["provisioning.json"]) != config.to_dict():
                raise ValueError("Bootstrap configuration changed")
        else:
            existing = {
                key: base64.b64decode(value, validate=True).decode()
                for key, value in current.get("data", {}).items()
            }
            if existing != keys:
                raise ValueError("Supplied bootstrap keys changed")

    for resource in desired[1:-1]:
        if resource["kind"] in {"ConfigMap", "Secret"}:
            current = read(resource["kind"], resource["metadata"]["name"])
            if current is None:
                if not job["spec"].get("suspend"):
                    raise ValueError("The existing operator Job lost its immutable inputs")
            else:
                verify_input(resource, current)
    phase, reason = job_progress(job)
    if phase == "failed":
        raise ValueError(job_failure(name, reason))
    if on_job is not None:
        on_job(job)
    if job["spec"].get("suspend"):
        for resource in desired[1:-1]:
            if resource["kind"] not in {"ServiceAccount"}:
                resource["metadata"]["ownerReferences"] = [owner]
            current = read(resource["kind"], resource["metadata"]["name"])
            if current is None:
                execute([*base, "create", "-f", "-"], value=resource)
            elif resource["kind"] in {"ConfigMap", "Secret"}:
                verify_input(resource, current)
            else:
                if resource["kind"] != "ServiceAccount" and (
                    current["metadata"].get("ownerReferences") != [owner]
                ):
                    raise ValueError("Bootstrap resource ownership mismatch")
                if any(
                    current["metadata"].get("annotations", {}).get(key) != expected
                    for key, expected in resource["metadata"].get("annotations", {}).items()
                ):
                    raise ValueError("Bootstrap identity annotation mismatch")
                for field in ("rules", "roleRef", "subjects"):
                    if field in resource and current.get(field) != resource[field]:
                        raise ValueError("Bootstrap permission mismatch")
                if any(
                    current["metadata"].get("labels", {}).get(key) != expected
                    for key, expected in labels.items()
                ):
                    raise ValueError("Bootstrap resource ownership mismatch")
        execute(
            [
                *base,
                "-n",
                namespace,
                "patch",
                "job",
                name,
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
                        {"op": "replace", "path": "/spec/suspend", "value": False},
                    ]
                ),
            ]
        )
    with job_logs(base, namespace, name, enabled=phase != "complete"):
        deadline = time.monotonic() + 3600
        previous_phase = None
        while True:
            current = read("job", name)
            if current is None or current["metadata"].get("uid") != metadata["uid"]:
                raise ValueError("Operator Job ownership changed while waiting")
            phase, reason = job_progress(current)
            if phase != previous_phase:
                status("progress", f"Job {name}: {phase}")
                previous_phase = phase
            if phase == "failed":
                raise ValueError(job_failure(name, reason))
            if phase == "complete":
                break
            if time.monotonic() >= deadline:
                raise ValueError("The canonical operator Job exceeded its completion deadline")
            time.sleep(3)
    deployments = (
        (("management-api",) if config.prepared_environments else ("management-api", "provisioner"))
        if name == "deploy-management"
        else ()
    )
    for deployment in deployments:
        execute(
            [
                *base,
                "-n",
                namespace,
                "rollout",
                "status",
                f"deployment/{deployment}",
                "--timeout=300s",
            ],
            timeout=360,
            capture=False,
        )
    return {
        "stage": "management-deployed",
        "namespace": namespace,
        "job": name,
        "job_uid": metadata["uid"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.config is not None:
        raise ValueError("Configuration now comes from the checkout .env and live APIs")
    identity = load_config(ROOT / ".env")
    if identity.environment != "azure":
        raise ValueError("This command requires the Azure .env selection")
    if args.execute:
        require_confirmation("azure")
    status("section", "Management: discover deployment inputs and cluster access")
    config = live_configuration(identity)
    if args.execute and config.prepared_environments:
        raise ValueError("Use make bootstrap for prepared Azure environments")
    with tempfile.TemporaryDirectory(prefix="plane-management-") as directory:
        context, kubeconfig = management_access(identity, Path(directory))
        if not args.execute:
            print(
                json.dumps(
                    {
                        "stage": "management-preview",
                        "context": context,
                        "namespace": config.namespace("management"),
                        "source_revision": config.identity.revision,
                        "provided_key_slots": sorted(config.identity.demo_keys),
                    }
                )
            )
            return 0
        status("section", "Management: submit deployment Job and wait for completion")
        print(json.dumps(deploy_selected(config, context, kubeconfig)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_main(main, "Management deployment"))
    except (CommandError, ValueError, KeyError, OSError) as exc:
        status("error", f"ERROR: {exc}")
        sys.exit(1)
