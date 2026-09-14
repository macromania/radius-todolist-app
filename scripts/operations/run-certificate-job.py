#!/usr/bin/env python3
"""Run certificate issuance inside its plane cluster and return its public reference."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def project_scope(settings: dict) -> tuple[str, str]:
    foundation = settings["foundation"]
    project = foundation.get("projectName", "radplanes")
    prefix = foundation.get("resourcePrefix", "radplanes")
    if (
        not re.fullmatch(r"[a-z][a-z0-9-]{0,15}", project)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,24}", prefix)
        or (
            prefix != "radplanes"
            and (not prefix.startswith(project + "-") or not prefix.endswith("-azure"))
        )
        or (prefix == "radplanes" and project != "radplanes")
    ):
        raise ValueError("Certificate deployment identity mismatch")
    return project, prefix


def allocation_for(settings: dict, slot: str) -> dict:
    allocations = settings["allocations"]
    if isinstance(allocations, dict):
        return allocations[slot]
    for allocation in allocations:
        if allocation["slot"] == slot:
            return allocation
    raise ValueError("Certificate allocation is missing")


def job_resources(settings: dict, slot: str, namespace: str, domain: str) -> list[dict]:
    allocation = allocation_for(settings, slot)
    identity = allocation["identities"]["certificateIssuer"]
    foundation = settings["foundation"]
    project, prefix = project_scope(settings)
    common = {"project": project, "plane-demo/slot": slot}
    issuer_namespace = f"{prefix}-system"
    service_account = "certificate-issuer"
    certificate_name = allocation.get("certificateName", f"gateway-{slot}")
    account_secret = allocation.get("acmeStateSecretName", f"acme-{slot}")
    image = settings["images"]["provisioner"]
    if isinstance(image, dict):
        image = image["reference"]
    if not re.search(r"@sha256:[a-f0-9]{64}$", image):
        raise ValueError("Certificate image must be digest-pinned")
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"certificate-{slot}",
            "namespace": issuer_namespace,
            "labels": common,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "template": {
                "metadata": {"labels": {**common, "azure.workload.identity/use": "true"}},
                "spec": {
                    "serviceAccountName": service_account,
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "issuer",
                            "image": image,
                            "command": [
                                "python",
                                "/app/scripts/operations/issue-certificate.py",
                                "--slot",
                                slot,
                                "--domain",
                                domain,
                                "--namespace",
                                namespace,
                                "--vault-name",
                                foundation["vaultName"],
                                "--certificate-name",
                                certificate_name,
                                "--account-secret",
                                account_secret,
                                "--project-name",
                                project,
                                *(["--resource-prefix", prefix] if prefix != "radplanes" else []),
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                        }
                    ],
                },
            },
        },
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": issuer_namespace, "labels": common},
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": service_account,
                "namespace": issuer_namespace,
                "labels": common,
                "annotations": {
                    "azure.workload.identity/client-id": identity["clientId"],
                    "azure.workload.identity/tenant-id": foundation["tenantId"],
                },
            },
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "acme-token-writer", "namespace": namespace, "labels": common},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": ["acme-challenges"],
                    "verbs": ["get", "patch"],
                }
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "acme-token-writer", "namespace": namespace, "labels": common},
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": service_account,
                    "namespace": issuer_namespace,
                }
            ],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "acme-token-writer",
            },
        },
        job,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("slot", "context", "namespace", "kubeconfig", "domain", "config"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--staging", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", args.slot):
        raise ValueError("Invalid allocation slot")
    settings = json.loads(Path(args.config).read_text())
    project, prefix = project_scope(settings)
    issuer_namespace = f"{prefix}-system"
    if args.context != f"{prefix}-{args.slot}":
        raise ValueError("Context does not match the target allocation")
    role = "management" if args.slot == "management" else args.slot.rsplit("-", 1)[1]
    if args.namespace != f"{prefix}-{args.slot}-{role}":
        raise ValueError("Application namespace does not match the allocation")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.cloudapp\.azure\.com", args.domain):
        raise ValueError("Certificate domain must be an Azure gateway hostname")
    kubeconfig = Path(args.kubeconfig).resolve()
    environment = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    base = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", args.context]

    def execute(command: list[str], value: dict | None = None) -> dict | str:
        result = subprocess.run(
            [*base, *command],
            input=json.dumps(value) if value is not None else None,
            text=True,
            capture_output=True,
            env=environment,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Certificate Kubernetes operation failed: {command[0]}: {result.stderr[-2000:]}"
            )
        return json.loads(result.stdout) if "-o" in command else result.stdout

    resources = job_resources(settings, args.slot, args.namespace, args.domain)
    if args.staging:
        resources[-1]["spec"]["template"]["spec"]["containers"][0]["command"].append("--staging")
    execute(["apply", "-f", "-"], {"apiVersion": "v1", "kind": "List", "items": resources[:-1]})
    existing = execute(
        [
            "-n",
            issuer_namespace,
            "get",
            "jobs",
            "-l",
            f"plane-demo/slot={args.slot}",
            "-o",
            "json",
        ]
    )
    expected_name = f"certificate-{args.slot}"
    for job in existing["items"]:
        if job["metadata"]["name"] == expected_name:
            if job["metadata"].get("labels", {}).get("project") != project:
                raise ValueError("Certificate Job ownership mismatch")
            execute(["-n", issuer_namespace, "delete", "job", expected_name, "--wait=true"])
    execute(["apply", "-f", "-"], resources[-1])
    deadline = time.monotonic() + 960
    while time.monotonic() < deadline:
        job = execute(["-n", issuer_namespace, "get", "job", expected_name, "-o", "json"])
        if job.get("status", {}).get("failed"):
            raise RuntimeError("Certificate Job failed; inspect the scoped issuer pod logs")
        if job.get("status", {}).get("succeeded") == 1:
            pods = execute(
                [
                    "-n",
                    issuer_namespace,
                    "get",
                    "pods",
                    "-l",
                    f"job-name={expected_name}",
                    "-o",
                    "json",
                ]
            )
            for pod in pods["items"]:
                for container in pod.get("status", {}).get("containerStatuses", []):
                    terminated = container.get("state", {}).get("terminated", {})
                    if container["name"] == "issuer" and terminated.get("exitCode") == 0:
                        result = json.loads(terminated["message"])
                        if args.staging:
                            if result != {"stagingValidation": "passed"}:
                                raise ValueError("Staging Job returned an unexpected result")
                            execute(["-n", issuer_namespace, "delete", "job", expected_name])
                            print(json.dumps(result))
                            return 0
                        certificate_name = allocation_for(settings, args.slot).get(
                            "certificateName", f"gateway-{args.slot}"
                        )
                        expected = (
                            f"https://{settings['foundation']['vaultName']}.vault.azure.net"
                            f"/secrets/{certificate_name}"
                        )
                        if result.get("certificateSecretUri") != expected:
                            raise ValueError("Certificate Job returned another plane's reference")
                        execute(["-n", issuer_namespace, "delete", "job", expected_name])
                        print(json.dumps(result))
                        return 0
            raise RuntimeError("Completed issuer Job has no valid certificate reference")
        time.sleep(5)
    raise TimeoutError("Certificate Job exceeded the bounded wait")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
