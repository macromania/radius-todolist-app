#!/usr/bin/env python3
"""Read verified Azure environment allocations without changing resources."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from plane_demo.management.providers.azure_environments import (  # noqa: E402
    EnvironmentError,
    deployment_outputs,
    merge_foundations,
    require,
)
from scripts.operations.config import load_config  # noqa: E402


def discover(config, *, base=None, runner=subprocess.run):
    def az(*args):
        result = runner(
            [
                "az",
                *args,
                "--subscription",
                config.subscription,
                "--output",
                "json",
                "--only-show-errors",
            ],
            stdout=subprocess.PIPE,
            text=True,
            check=False,
            timeout=180,
        )
        require(
            result.returncode == 0, "Environment catalog discovery failed; see Azure diagnostics"
        )
        return json.loads(result.stdout)

    if base is None:
        base = deployment_outputs(
            az("deployment", "sub", "show", "--name", f"{config.stem}-bootstrap")
        )
    if base.get("foundation", {}).get("environmentMode") != "prepared-v1":
        return merge_foundations(config, base, [])
    records = az(
        "deployment",
        "sub",
        "list",
        "--query",
        f"[?starts_with(name, '{config.stem}-environment-')]",
    )
    return merge_foundations(config, base, records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--slots", action="store_true")
    args = parser.parse_args()
    config = load_config(ROOT / ".env")
    require(config.environment == "azure", "Environment catalog requires Azure")
    base = json.loads(args.base.read_text()) if args.base else None
    value = discover(config, base=base)
    if args.slots:
        print("\n".join(item["slot"] for item in value["allocations"]))
    else:
        print(json.dumps(value))


if __name__ == "__main__":
    try:
        main()
    except (EnvironmentError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Environment catalog failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
