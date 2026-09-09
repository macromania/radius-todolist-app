#!/usr/bin/env python3
"""Install and configure Radius in one explicitly named project cluster."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from project import ROOT, SUBSCRIPTION, CommandError, run, uuid


def install(context: str, kubeconfig: Path, config: Path, client_id: str, tenant_id: str) -> None:
    if not re.fullmatch(r"radplanes-[a-z0-9-]+", context):
        raise ValueError("Context must be an explicitly named radplanes cluster")
    state = (ROOT / ".state" / "azure").resolve()
    for path in (kubeconfig, config):
        if not path.resolve().is_relative_to(state):
            raise ValueError("Azure kubeconfig and Radius config must be in .state/azure")
    if not kubeconfig.is_file():
        raise ValueError("Project kubeconfig does not exist")
    uuid(client_id, "Radius client ID")
    uuid(tenant_id, "Tenant ID")
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {**os.environ, "KUBECONFIG": str(kubeconfig.resolve())}
    rad = ["rad", "--config", str(config.resolve())]
    kubectl = ["kubectl", "--context", context]

    def execute(args: list[str], *, capture: bool = False) -> str:
        return run(args, env=environment, capture=capture)

    execute([*kubectl, "get", "nodes"])
    execute(
        [
            *rad,
            "install",
            "kubernetes",
            "--kubecontext",
            context,
            "--skip-contour-install",
            "--set",
            "dashboard.enabled=false",
            "--set",
            "global.azureWorkloadIdentity.enabled=true",
        ]
    )
    for account in ("applications-rp", "bicep-de", "ucp", "dynamic-rp"):
        execute(
            [
                *kubectl,
                "-n",
                "radius-system",
                "annotate",
                "serviceaccount",
                account,
                "--overwrite",
                f"azure.workload.identity/client-id={client_id}",
                f"azure.workload.identity/tenant-id={tenant_id}",
            ]
        )
        execute(
            [
                *kubectl,
                "-n",
                "radius-system",
                "patch",
                "deployment",
                account,
                "--type=merge",
                "-p",
                json.dumps(
                    {
                        "spec": {
                            "template": {
                                "metadata": {
                                    "labels": {"azure.workload.identity/use": "true"},
                                }
                            }
                        },
                    }
                ),
            ]
        )
        execute(
            [
                *kubectl,
                "-n",
                "radius-system",
                "rollout",
                "restart",
                f"deployment/{account}",
            ]
        )
        execute(
            [
                *kubectl,
                "-n",
                "radius-system",
                "rollout",
                "status",
                f"deployment/{account}",
                "--timeout=300s",
            ]
        )
    execute(
        [
            *rad,
            "workspace",
            "create",
            "kubernetes",
            context,
            "--context",
            context,
            "--force",
        ]
    )
    execute(
        [
            *rad,
            "credential",
            "register",
            "azure",
            "wi",
            "--workspace",
            context,
            "--client-id",
            client_id,
            "--tenant-id",
            tenant_id,
        ]
    )
    deployments = json.loads(
        execute(
            [*kubectl, "-n", "radius-system", "get", "pods", "-o", "json"],
            capture=True,
        )
    )
    accounts_seen = set()
    for pod in deployments["items"]:
        account = pod["spec"].get("serviceAccountName")
        if account not in {"applications-rp", "bicep-de", "ucp", "dynamic-rp"}:
            continue
        if pod["metadata"].get("deletionTimestamp"):
            continue
        for container in pod["spec"]["containers"]:
            variables = {item["name"]: item.get("value") for item in container.get("env", [])}
            if variables.get("AZURE_CLIENT_ID") == client_id and variables.get(
                "AZURE_FEDERATED_TOKEN_FILE"
            ):
                accounts_seen.add(account)
    expected = {"applications-rp", "bicep-de", "ucp", "dynamic-rp"}
    if accounts_seen != expected:
        raise CommandError(
            f"Workload identity was not injected for: {sorted(expected - accounts_seen)}"
        )
    config.chmod(0o600)
    kubeconfig.chmod(0o600)
    print(
        f"Radius installed: context={context} subscription={SUBSCRIPTION}; "
        "identity projection verified."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    args = parser.parse_args()
    try:
        install(args.context, args.kubeconfig, args.config, args.client_id, args.tenant_id)
    except (CommandError, ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
