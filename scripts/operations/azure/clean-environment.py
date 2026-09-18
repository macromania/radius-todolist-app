#!/usr/bin/env python3
"""Remove one unassigned isolated environment, retaining the default deployment."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from plane_demo.management.providers.azure_environments import (  # noqa: E402
    deployment_outputs,
    environment_deployment,
    require,
)
from plane_demo.management.providers.identity import isolated_pair  # noqa: E402
from plane_demo.management.providers.secret_store import CredentialScope  # noqa: E402
from scripts.operations.azure.bootstrap import artifacts, check_source  # noqa: E402
from scripts.operations.azure.environment_operator import (  # noqa: E402
    EnvironmentOperator,
    azure,
    base_deployment,
    catalog,
    operator_config,
)
from scripts.operations.config import load_config  # noqa: E402
from scripts.operations.output import run_main, status  # noqa: E402


def cleanup_engine(execute):
    spec = importlib.util.spec_from_file_location(
        "isolated_cleanup_engine", ROOT / "scripts/operations/clean-azure.py"
    )
    cleanup = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cleanup
    spec.loader.exec_module(cleanup)
    return cleanup.LiveAzureCleanup(execute=execute)


@contextmanager
def cleanup_exclusion(engine, identity, base, pair):
    with tempfile.TemporaryDirectory(prefix="plane-environment-clean-") as directory:
        operator = EnvironmentOperator(identity, Path(directory), base)
        operator.acquire(cleanup_target=pair)
        original_current = engine.current

        def guarded_current():
            original_current()
            operator.guard()

        engine.current = guarded_current
        try:
            yield operator
        finally:
            if operator.release_safe:
                operator.release()
            engine.current = original_current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    identity = load_config(ROOT / ".env")
    require(identity.environment == "azure", "Isolated cleanup requires Azure")
    require(not args.execute or os.environ.get("CONFIRM_AZURE") == "yes", "Set CONFIRM_AZURE=yes")
    pair = isolated_pair(args.isolated)
    check_source(identity)
    base_record = base_deployment(identity)
    require(base_record is not None, "The default environment is missing")
    base = deployment_outputs(base_record)
    document = catalog(identity, base)
    members = (f"{pair}-control", f"{pair}-data")
    require(
        all(slot in {item["slot"] for item in document["allocations"]} for slot in members),
        "The selected isolated environment is not active",
    )
    engine = cleanup_engine(args.execute)
    try:
        engine.discover_topology()
        engine.external_vault()
        engine.unexpected()
        engine.group_resources = {name: engine.group(name) for name in engine.groups}
        engine.foundation()
        definitions, assignments, _ = engine.role_state()
        clusters = engine.clusters()
        require(
            all(slot in clusters for slot in ("management", *members)),
            "Each selected environment cluster must be reachable for owner-ordered cleanup",
        )
        for slot in ("management", *members):
            engine.inventories[slot] = engine.inventory(slot)
            engine.check_faults(slot)
        owners = engine.inventories["management"]["children"]
        require(all(slot in owners for slot in members), "Isolated cluster lacks a Radius owner")
        binding = json.loads(
            engine.kube(
                "management",
                "exec",
                "deployment/management-api",
                "--",
                "python",
                "-B",
                "-c",
                "import json,os,sys,psycopg\n"
                "with psycopg.connect(os.environ['MANAGEMENT_DSN'],connect_timeout=5,"
                "options='-c default_transaction_read_only=on -c statement_timeout=5000') as c:\n"
                " print(json.dumps({'assigned': c.execute("
                "'SELECT EXISTS(SELECT 1 FROM management.tenants WHERE pair_id=%s)',"
                "(sys.argv[1],)).fetchone()[0]}))",
                pair,
                namespace=identity.namespace("management"),
            )
        )
        require(binding == {"assigned": False}, "The isolated environment still has tenants")
        allocations = {item["slot"]: item for item in document["allocations"]}
        principals = {
            selected["principalId"].lower()
            for slot in members
            for selected in allocations[slot]["identities"].values()
        }
        vnet = base["foundation"]["virtualNetworkId"]
        subnets = [
            f"{vnet}/subnets/snet-{slot}-{suffix}"
            for slot in members
            for suffix in ("nodes", "gateway", "endpoints", "postgresql")
        ]
        groups = [
            identity.plane_group(slot) + suffix for slot in members for suffix in ("", "-nodes")
        ]
        scope = CredentialScope(identity.project, identity.deployment, "azure")
        names = [scope.secret_name("management", "cp_" + pair.replace("-", "_"))]
        names += [
            scope.secret_name(f"{pair}-{role}", credential)
            for role, roles in (
                ("control", ("demoKey", "cp_api", "cp_reconciler", "dp_reconciler")),
                ("data", ("demoKey",)),
            )
            for credential in roles
        ]
        vault = base["foundation"]["vaultId"]
        credential_scopes = {f"{vault}/secrets/{name}".lower() for name in names}
        selected_assignments = [
            row
            for row in assignments
            if row.get("principalId", "").lower() in principals
            or row.get("scope", "").lower() in credential_scopes
            or row.get("scope", "").lower() in {item.lower() for item in subnets}
            or any(
                row.get("scope", "").lower() == engine.gid(group).lower()
                or row.get("scope", "").lower().startswith(engine.gid(group).lower() + "/")
                for group in groups
            )
        ]
        selected_definitions = {
            key: value for key, value in definitions.items() if key.startswith(pair + "/")
        }
        summary = {
            "status": "planned",
            "pair_id": pair,
            "groups": groups,
            "subnets": subnets,
            "retained": [
                "management and shared environments",
                "tenant and operation history",
                "environment allocation tombstone",
                "Key Vault credential and certificate objects",
            ],
        }
        if not args.execute:
            print(json.dumps(summary))
            return 0
        proof = artifacts(inspect_only=True)
        with cleanup_exclusion(engine, identity, base, pair) as operator:
            configuration = operator_config(identity, document, proof)
            operator.run_job(configuration, f"retire-{pair}")
            for slot in reversed(members):
                for app in engine.inventories[slot]["apps"]:
                    engine.delete_app(slot, app["name"])
                engine.child_apps_absent(slot)
            for slot in reversed(members):
                engine.delete_cluster_owner(slot, owners[slot])
                engine.child_absent(slot, owners[slot])
                engine.delete_app("management", f"cluster-{slot}")
            live_assignments = engine.az(
                "role", "assignment", "list", "--all", "--fill-principal-name", "false"
            )
            require(
                isinstance(live_assignments, list)
                and all(
                    isinstance(item, dict) and isinstance(item.get("id"), str)
                    for item in live_assignments
                ),
                "Role assignment observation is incomplete",
            )
            live_by_id = {item["id"].lower(): item for item in live_assignments}
            for assignment in selected_assignments:
                current = live_by_id.get(assignment["id"].lower())
                if current is None:
                    continue
                require(
                    all(
                        current.get(key) == assignment.get(key)
                        for key in (
                            "principalId",
                            "roleDefinitionId",
                            "scope",
                            "condition",
                            "conditionVersion",
                        )
                    ),
                    "An isolated role assignment changed during cleanup",
                )
                engine.az("role", "assignment", "delete", "--ids", assignment["id"], mutation=True)
            for group in groups:
                engine.delete_group(group)
            for subnet in subnets:
                engine.az("network", "vnet", "subnet", "delete", "--ids", subnet, mutation=True)
            for value in selected_definitions.values():
                engine.az(
                    "role",
                    "definition",
                    "delete",
                    "--name",
                    value["id"].rsplit("/", 1)[1],
                    "--custom-role-only",
                    "true",
                    mutation=True,
                )
            for group in groups:
                require(
                    engine.az("group", "exists", "--name", group) is False, "Isolated group remains"
                )
            observed_subnets = engine.az(
                "network",
                "vnet",
                "subnet",
                "list",
                "--resource-group",
                base["foundation"]["platformResourceGroup"],
                "--vnet-name",
                base["foundation"]["virtualNetworkName"],
            )
            require(
                not {row["id"].lower() for row in observed_subnets}
                & {value.lower() for value in subnets},
                "Isolated subnets remain",
            )
            operator.mark(pair, "retired")
            deployment_id = (
                f"/subscriptions/{identity.subscription}/providers/Microsoft.Resources/deployments/"
                + environment_deployment(identity, pair)
            )
            operator.guard()
            azure(
                identity,
                "tag",
                "update",
                "--resource-id",
                deployment_id,
                "--operation",
                "Merge",
                "--tags",
                "plane-demo/environment-state=retired",
            )
        summary["status"] = "isolated_environment_removed"
        print(json.dumps(summary))
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    try:
        raise SystemExit(run_main(main, "Isolated environment cleanup"))
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        status("error", str(error))
        raise SystemExit(1) from None
