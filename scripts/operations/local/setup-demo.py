#!/usr/bin/env python3
"""Compatibility entrypoint for canonical native local setup; no separate setup implementation."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    "execute": False,
                    "stage": "setup-local-management",
                    "entrypoint": "scripts/operations/local/setup.sh",
                }
            )
        )
        return 0
    return subprocess.call(["bash", str(ROOT / "scripts/operations/local/setup.sh")])


if __name__ == "__main__":
    raise SystemExit(main())
