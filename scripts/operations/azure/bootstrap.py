#!/usr/bin/env python3
"""Prepare default Azure capacity or add a named isolated environment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from plane_demo.management.providers.azure_environments import (  # noqa: E402
    EnvironmentError,
    deployment_outputs,
    require,
)
from plane_demo.management.providers.identity import isolated_pair  # noqa: E402
from scripts.operations.azure import node_sizes, postgres_sizes  # noqa: E402
from scripts.operations.azure.environment_operator import (  # noqa: E402
    EnvironmentOperator,
    base_deployment,
    catalog,
    execute,
    operator_config,
    selection_environment,
)
from scripts.operations.config import ConfigError, load_config  # noqa: E402
from scripts.operations.output import progress, run_main, status  # noqa: E402


def check_source(identity):
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
    require(not dirty, "Commit verified deployment inputs before bootstrap")
    revision = execute(["git", "rev-parse", "HEAD"])
    require(
        identity.revision is None or identity.revision == revision,
        "Use the checkout matching the selected deployment revision",
    )
    return revision


def artifacts(inspect_only=False, *, identity=None):
    identity = identity or load_config(ROOT / ".env")
    with progress("Artifacts: inspect" if inspect_only else "Artifacts: build and inspect"):
        return json.loads(
            execute(
                [
                    "bash",
                    str(ROOT / "scripts/operations/azure/build.sh"),
                    *(["--inspect"] if inspect_only else []),
                ],
                timeout=14400,
                env=selection_environment(identity),
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated", help="Add this named isolated control/data environment")
    args = parser.parse_args()
    require(os.environ.get("CONFIRM_AZURE") == "yes", "Set CONFIRM_AZURE=yes")
    identity = load_config(ROOT / ".env")
    require(identity.environment == "azure", "Select Azure in .env")
    revision = check_source(identity)
    pair = isolated_pair(args.isolated) if args.isolated else "shared"
    base = base_deployment(identity)
    if base is None:
        require(pair == "shared", "Prepare the default environment before adding an isolated pair")
        status("section", "Bootstrap: create the default foundation")
        with progress("Default foundation"):
            execute(
                ["bash", str(ROOT / "scripts/operations/azure/foundation.sh")],
                timeout=7200,
                env=selection_environment(identity),
            )
        observed = load_config(ROOT / ".env")
        require(
            observed
            == replace(
                identity,
                node_vm_size=identity.node_vm_size or observed.node_vm_size,
                postgres_sku_name=identity.postgres_sku_name or observed.postgres_sku_name,
                postgres_sku_tier=identity.postgres_sku_tier or observed.postgres_sku_tier,
            ),
            "Operator configuration changed during foundation selection",
        )
        identity = observed
        base = base_deployment(identity)
        require(base is not None, "Default foundation was not recorded")
    else:
        status("success", "Default foundation: retained without redeployment")
    base = deployment_outputs(base)
    proof = artifacts(identity=identity)
    require(proof.get("source_revision") == revision, "Artifact revision changed during bootstrap")
    with tempfile.TemporaryDirectory(prefix="plane-environments-") as directory:
        operator = EnvironmentOperator(identity, Path(directory), base)
        operator.acquire()
        completed = False
        try:
            operator.ensure_management_radius()
            document = catalog(identity, base)
            selected = operator_config(identity, document, proof)
            if pair == "shared":
                operator.run_job(operator_config(identity, base, proof), "deploy-management")
            else:
                operator.require_default_ready(selected)
                record, fresh = operator.reserve(
                    pair, document["foundation"].get("environmentFoundations", {})
                )
                if fresh or record.get("state") == "reserved":
                    status("section", "Bootstrap: recheck capacity for the added environment")
                    desired_slots = {
                        *(item["slot"] for item in document["allocations"]),
                        f"{pair}-control",
                        f"{pair}-data",
                    }
                    budget = node_sizes.Budget(len(desired_slots), base["foundation"]["nodeCount"])
                    discovery = node_sizes.Discovery(identity)
                    eligible, problems = discovery.available(budget, slots=desired_slots)
                    node_sizes.select_size(
                        eligible,
                        problems,
                        identity,
                        budget,
                        discovery,
                        existing=base["foundation"]["nodeVmSize"],
                    )
                    postgres, excluded = postgres_sizes.Discovery(identity).available()
                    postgres_sizes.select_size(
                        postgres,
                        excluded,
                        identity,
                        existing=(
                            base["foundation"]["postgresSkuName"],
                            base["foundation"]["postgresSkuTier"],
                        ),
                    )
                operator.isolated_foundation(pair, record, fresh=fresh)
                document = catalog(identity, base)
                selected = operator_config(identity, document, proof)
            inputs = Path(directory) / "foundation.json"
            from plane_demo.management.providers.commands import write_json

            write_json(inputs, document)
            with progress("Radius plane grant verification"):
                execute(
                    [
                        sys.executable,
                        str(ROOT / "scripts/operations/azure/plane_policy.py"),
                        "--foundation",
                        str(inputs),
                    ],
                    timeout=600,
                )
            operator.run_job(selected, f"prepare-{pair}")
            operator.verify_ready(selected, pair)
            if pair != "shared":
                operator.mark(pair, "available")
            completed = True
        finally:
            if completed or operator.release_safe:
                operator.release()
            if not completed:
                status(
                    "warning",
                    "Environment setup stopped. Its owned records are retained; "
                    "do not reset tenant state.",
                )
        status("success", f"Environment: {pair} is prepared for tenant admission")
        print(
            json.dumps(
                {
                    "status": "environment_prepared",
                    "pair_id": pair,
                    "source_revision": revision,
                    "slots": [item["slot"] for item in document["allocations"]],
                }
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run_main(main, "Azure environment bootstrap"))
    except (
        EnvironmentError,
        ConfigError,
        ValueError,
        KeyError,
        OSError,
        subprocess.TimeoutExpired,
    ) as error:
        status("error", str(error))
        raise SystemExit(1) from None
