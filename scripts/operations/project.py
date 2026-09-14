#!/usr/bin/env python3
"""Project-scoped infrastructure commands; never changes global CLI defaults."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "radplanes"
SUBSCRIPTION = "a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc"
LOCATION = "centralus"
BICEP = Path.home() / ".rad/bin/bicep"
TAGS = {
    "project": PROJECT,
    "managedBy": "radius-todolist-app",
    "SecurityControl": "Ignore",
}


class CommandError(RuntimeError):
    pass


def run(args: list[str], *, capture: bool = False, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        check=False,
    )
    if result.returncode:
        raise CommandError(
            f"{args[0]} {args[1] if len(args) > 1 else ''} exited {result.returncode}"
        )
    return result.stdout.strip() if capture else ""


def az(*args: str) -> Any:
    output = run(["az", *args, "--subscription", SUBSCRIPTION, "--output", "json"], capture=True)
    return json.loads(output) if output else None


def state_dir(environment: str) -> Path:
    path = ROOT / ".state" / environment
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2)
        handle.write("\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def radius_environment(kubeconfig: Path, context: str) -> dict[str, str]:
    if not re.fullmatch(r"radplanes-[a-z0-9-]+", context):
        raise ValueError("Invalid project Radius context")
    state = (ROOT / ".state/azure").resolve()
    if not kubeconfig.resolve().is_relative_to(state):
        raise ValueError("Radius kubeconfig must remain in project state")
    home = state / "homes" / context
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative, target in ((".kube/config", kubeconfig.resolve()), (".rad/bin/bicep", BICEP)):
        link = home / relative
        link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if link.exists() or link.is_symlink():
            if link.resolve() != target.resolve():
                raise ValueError("Project Radius home contains an unexpected configuration link")
        else:
            link.symlink_to(target)
    environment = {
        **os.environ,
        "HOME": str(home),
        "KUBECONFIG": str(kubeconfig.resolve()),
        "AZURE_CONFIG_DIR": os.environ.get("AZURE_CONFIG_DIR", str(Path.home() / ".azure")),
    }
    return environment


def uuid(value: str, label: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise ValueError(f"{label} is not a UUID")
    return value


def operator_identity() -> dict[str, Any]:
    # az rest does not consistently forward --subscription to Graph token lookup.
    token = az("account", "get-access-token", "--resource-type", "ms-graph")
    access_token = token.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise CommandError("Azure CLI returned no Microsoft Graph access token")
    connection = http.client.HTTPSConnection("graph.microsoft.com", timeout=20)
    try:
        connection.request(
            "GET",
            "/v1.0/me?$select=id",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise CommandError(
                f"Subscription-scoped Graph identity lookup failed: HTTP {response.status}"
            )
        return json.loads(response.read())
    finally:
        connection.close()


def preflight(environment: str) -> None:
    for name in ("az", "rad", "kubectl", "helm", "docker", "uv", "jq"):
        if shutil.which(name) is None:
            raise CommandError(f"Required tool is missing: {name}")
    if not BICEP.is_file():
        raise CommandError(f"Radius Bicep compiler is missing: {BICEP}")
    run(["docker", "info", "--format", "Docker CPUs={{.NCPU}} memory={{.MemTotal}}"])
    run(["rad", "version", "--cli"])
    if environment != "azure":
        raise CommandError(
            "The local deployment gate follows Azure acceptance; not implemented yet"
        )
    account = az("account", "show")
    if account["id"] != SUBSCRIPTION:
        raise CommandError("Azure account does not match the project subscription")
    identity = operator_identity()
    operator = uuid(identity["id"], "Operator object ID")
    public_ip = run(
        ["curl", "--fail", "--silent", "--show-error", "--max-time", "20", "https://api.ipify.org"],
        capture=True,
    )
    parsed_ip = ipaddress.ip_address(public_ip)
    if parsed_ip.version != 4 or not parsed_ip.is_global:
        raise ValueError("Operator IP must be a public IPv4 address")
    postgres = az("postgres", "flexible-server", "list-skus", "--location", LOCATION)
    if not any(item.get("supportedServerEditions") for item in postgres):
        raise CommandError(
            f"PostgreSQL provisioning is unavailable in {LOCATION}: "
            + "; ".join(item.get("reason") or "No editions returned" for item in postgres)
        )
    groups = az(
        "group", "list", "--query", "[?starts_with(name, 'rg-radplanes-')].{name:name,tags:tags}"
    )
    for group in groups:
        if (group.get("tags") or {}).get("project") != PROJECT:
            raise CommandError(
                f"Project name collides with an unowned resource group: {group['name']}"
            )
    observed_file = state_dir(environment) / "operator-ips.json"
    observed = json.loads(observed_file.read_text())["observed"] if observed_file.exists() else []
    if not isinstance(observed, list) or len(observed) > 8:
        raise ValueError("At most eight explicitly observed operator addresses are allowed")
    for address in observed:
        if not isinstance(address, str):
            raise ValueError("Observed operator addresses must be IPv4 strings")
        candidate = ipaddress.ip_address(address)
        if candidate.version != 4 or not candidate.is_global:
            raise ValueError("Additional operator addresses must be public IPv4 addresses")
    context = {
        "project": PROJECT,
        "subscription": SUBSCRIPTION,
        "location": LOCATION,
        "tenant": uuid(account["tenantId"], "Tenant ID"),
        "operator_object_id": operator,
        "operator_ip": public_ip,
        "additional_operator_ips": observed,
        "kubernetes_version": "1.35.7",
        "node_vm_size": "Standard_D4s_v5",
        "node_count": 2,
        "tags": TAGS,
        "postgres_available": True,
    }
    write_json(state_dir(environment) / "context.json", context)
    print(json.dumps(context, indent=2))
    print("Target scenario: 5 AKS clusters, 5 Application Gateways, 3 PostgreSQL, 2 Redis.")
    print("Preflight passed. No resources were created. Azure creation requires CONFIRM_AZURE=yes.")


def require_confirmation(environment: str) -> None:
    if environment == "azure" and os.environ.get("CONFIRM_AZURE") != "yes":
        raise CommandError("Azure mutation requires CONFIRM_AZURE=yes")


def load_context(environment: str) -> dict[str, Any]:
    context = json.loads((state_dir(environment) / "context.json").read_text())
    if context.get("subscription") != SUBSCRIPTION or context.get("project") != PROJECT:
        raise CommandError("Stored context does not match this project")
    return context


def bootstrap(preview: bool) -> None:
    context = load_context("azure")
    state = state_dir("azure")
    salt_file = state / "name-salt.json"
    if not salt_file.exists():
        write_json(salt_file, {"salt": uuid4().hex[:12]})
    salt = json.loads(salt_file.read_text())["salt"]
    if not re.fullmatch(r"[a-f0-9]{12}", salt):
        raise ValueError("Stored resource naming salt is malformed")
    parameters = {
        "projectName": PROJECT,
        "location": LOCATION,
        "nameSalt": salt,
        "operatorIp": context["operator_ip"],
        "additionalOperatorIps": context.get("additional_operator_ips", []),
        "operatorObjectId": context["operator_object_id"],
        "kubernetesVersion": context["kubernetes_version"],
        "nodeVmSize": context["node_vm_size"],
        "nodeCount": context["node_count"],
    }
    parameter_file = state / "bootstrap.parameters.json"
    write_json(
        parameter_file,
        {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {name: {"value": value} for name, value in parameters.items()},
        },
    )
    template = state / "bootstrap.json"
    run([str(BICEP), "build", "infra/bootstrap/azure.bicep", "--outfile", str(template)])
    args = [
        "az",
        "deployment",
        "sub",
        "what-if" if preview else "create",
        "--subscription",
        SUBSCRIPTION,
        "--location",
        LOCATION,
        "--name",
        f"{PROJECT}-bootstrap",
        "--template-file",
        str(template),
        "--parameters",
        f"@{parameter_file}",
    ]
    if preview:
        run([*args, "--result-format", "ResourceIdOnly"])
        return
    require_confirmation("azure")
    deployment = json.loads(run([*args, "--output", "json"], capture=True))
    if deployment.get("properties", {}).get("provisioningState") != "Succeeded":
        raise CommandError("Bootstrap deployment did not succeed")
    outputs = {name: value["value"] for name, value in deployment["properties"]["outputs"].items()}
    write_json(state / "bootstrap.outputs.json", outputs)
    print(f"Bootstrap succeeded. Outputs saved to {state / 'bootstrap.outputs.json'}")


def install_management_radius() -> None:
    require_confirmation("azure")
    state = state_dir("azure")
    outputs = json.loads((state / "bootstrap.outputs.json").read_text())
    management = outputs["managementCluster"]
    allocation = next(item for item in outputs["allocations"] if item["slot"] == "management")
    if management["name"] != "aks-radplanes-management":
        raise ValueError("Unexpected management cluster name in deployment outputs")
    kubeconfig = state / "management.kubeconfig"
    context = "radplanes-management"
    run(
        [
            "az",
            "aks",
            "get-credentials",
            "--subscription",
            SUBSCRIPTION,
            "--resource-group",
            management["resourceGroup"],
            "--name",
            management["name"],
            "--context",
            context,
            "--file",
            str(kubeconfig),
            "--overwrite-existing",
        ]
    )
    run(["kubelogin", "convert-kubeconfig", "--kubeconfig", str(kubeconfig), "-l", "azurecli"])
    run(
        [
            sys.executable,
            "scripts/operations/install-radius.py",
            "--context",
            context,
            "--kubeconfig",
            str(kubeconfig),
            "--config",
            str(state / "radius.yaml"),
            "--client-id",
            allocation["identities"]["radius"]["clientId"],
            "--tenant-id",
            outputs["foundation"]["tenantId"],
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["preflight", "bootstrap-preview", "bootstrap", "install-radius"]
    )
    parser.add_argument("--environment", choices=["azure", "local"], default="azure")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            preflight(args.environment)
        elif args.environment != "azure":
            raise CommandError("Local provider follows the Azure acceptance gate")
        elif args.command == "install-radius":
            install_management_radius()
        else:
            bootstrap(preview=args.command == "bootstrap-preview")
    except (CommandError, ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
