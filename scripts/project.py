#!/usr/bin/env python3
"""Project-scoped infrastructure commands; never changes global CLI defaults."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "radplanes"
SUBSCRIPTION = "a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc"
LOCATION = "eastus2"
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
        args, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE if capture else None,
        check=False,
    )
    if result.returncode:
        raise CommandError(f"{args[0]} {args[1] if len(args) > 1 else ''} exited {result.returncode}")
    return result.stdout.strip() if capture else ""


def az(*args: str) -> Any:
    output = run(
        ["az", *args, "--subscription", SUBSCRIPTION, "--output", "json"], capture=True
    )
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
            "GET", "/v1.0/me?$select=id",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise CommandError(f"Subscription-scoped Graph identity lookup failed: HTTP {response.status}")
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
        raise CommandError("The local deployment gate follows Azure acceptance; not implemented yet")
    account = az("account", "show")
    if account["id"] != SUBSCRIPTION:
        raise CommandError("Azure account does not match the project subscription")
    identity = operator_identity()
    operator = uuid(identity["id"], "Operator object ID")
    public_ip = run(
        ["curl", "--fail", "--silent", "--show-error", "--max-time", "20",
         "https://api.ipify.org"], capture=True,
    )
    parsed_ip = ipaddress.ip_address(public_ip)
    if parsed_ip.version != 4 or not parsed_ip.is_global:
        raise ValueError("Operator IP must be a public IPv4 address")
    usage = az("vm", "list-usage", "--location", LOCATION)
    capacity = {
        item["name"]["value"]: {
            "current": int(item["currentValue"]), "limit": int(item["limit"]),
        }
        for item in usage
        if item["name"]["value"] in {"cores", "standardDSv5Family"}
    }
    for name in ("cores", "standardDSv5Family"):
        if name not in capacity or capacity[name]["limit"] - capacity[name]["current"] < 20:
            raise CommandError(f"Insufficient or unknown compute capacity for five D4s_v5 nodes: {name}")
    groups = az("group", "list", "--query",
                "[?starts_with(name, 'rg-radplanes-')].{name:name,tags:tags}")
    for group in groups:
        if (group.get("tags") or {}).get("project") != PROJECT:
            raise CommandError(f"Project name collides with an unowned resource group: {group['name']}")
    context = {
        "project": PROJECT, "subscription": SUBSCRIPTION, "location": LOCATION,
        "tenant": uuid(account["tenantId"], "Tenant ID"),
        "operator_object_id": operator, "operator_ip": public_ip,
        "kubernetes_version": "1.35.7", "node_vm_size": "Standard_D4s_v5",
        "tags": TAGS, "capacity": capacity,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight"])
    parser.add_argument("--environment", choices=["azure", "local"], default="azure")
    args = parser.parse_args()
    try:
        preflight(args.environment)
    except (CommandError, ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
