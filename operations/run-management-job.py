#!/usr/bin/env python3
"""Start one management deployment from an in-cluster operator Job."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from project import ROOT, SUBSCRIPTION, CommandError, az, require_confirmation, write_json

sys.path.insert(0, str(ROOT / "src"))
from plane_demo.management.provisioning import OperatorConfig  # noqa: E402


def resources(config: dict, name: str) -> list[dict]:
    namespace = "radplanes-management-management"
    identity = config["coordinatorIdentity"]
    labels = {"project": "radplanes", "plane-demo/operator": "management-deploy"}
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": {"project": "radplanes"}},
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
                                    "operations/deploy-plane.py",
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


def unfinished_operator(job: dict) -> bool:
    if job["metadata"]["namespace"] != "radplanes-management-management":
        return False
    return not any(
        condition.get("type") in {"Complete", "Failed"} and condition.get("status") == "True"
        for condition in job.get("status", {}).get("conditions", [])
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="deploy-management")
    parser.add_argument("--config", type=Path, default=ROOT / ".state/azure/provisioning.json")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"deploy-management(?:-[a-z0-9]{1,16})?", args.name):
        raise ValueError("Operator Job name must be deploy-management or its bounded run suffix")
    state = (ROOT / ".state/azure").resolve()
    if not args.config.resolve().is_relative_to(state):
        raise ValueError("Configuration must remain in project Azure state")
    config = OperatorConfig.load(args.config).to_dict()
    if config["foundation"]["subscriptionId"] != SUBSCRIPTION:
        raise ValueError("Operator subscription does not match the approved project")
    target = config["managementCluster"]
    if (
        target.get("name") != "aks-radplanes-management"
        or target.get("resourceGroup") != "rg-radplanes-management-cluster"
    ):
        raise ValueError("Operator target is not this project's management cluster")
    allocation = config["allocations"]["management"]
    if (
        allocation.get("clusterName") != target["name"]
        or allocation.get("clusterResourceGroup") != target["resourceGroup"]
        or allocation.get("appResourceGroup") != "rg-radplanes-management-app"
    ):
        raise ValueError("Management deployment allocation does not match the fixed project target")
    manifest = state / f"{args.name}.json"
    write_json(
        manifest, {"apiVersion": "v1", "kind": "List", "items": resources(config, args.name)}
    )
    if not args.execute:
        print(f"Prepared {manifest}; no cluster changes made.")
        return 0
    require_confirmation("azure")
    status = az(
        "aks",
        "command",
        "invoke",
        "--name",
        target["name"],
        "--resource-group",
        target["resourceGroup"],
        "--command",
        "kubectl get jobs -A -l project=radplanes,plane-demo/operator=management-deploy -o json",
    )
    if status.get("exitCode") != 0:
        raise CommandError("Cannot verify whether a management operator is already running")
    jobs = json.loads(status["logs"])
    if any(unfinished_operator(job) for job in jobs["items"]):
        raise CommandError("A management operator Job has not reached a terminal state")
    result = az(
        "aks",
        "command",
        "invoke",
        "--name",
        target["name"],
        "--resource-group",
        target["resourceGroup"],
        "--command",
        f"kubectl apply -f {manifest.name}",
        "--file",
        str(manifest),
    )
    if result.get("exitCode") != 0:
        raise CommandError(f"Operator Job submission failed: {result.get('logs', '')[-2000:]}")
    print(result.get("logs", ""))
    print(f"Submitted {args.name}; Job completion and endpoint verification are still required.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CommandError, ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
