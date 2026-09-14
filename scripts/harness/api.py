#!/usr/bin/env python3
"""Call a live plane API through the native .env-backed operator command."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.operations.config import ConfigError, load_config  # noqa: E402


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    legacy_environment = None
    if arguments and arguments[0] in {"azure", "local"}:
        legacy_environment = arguments.pop(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("azure", "local"))
    parser.add_argument("target")
    parser.add_argument("method", choices=("GET", "POST", "PUT"))
    parser.add_argument("path")
    parser.add_argument("body", nargs="?")
    args = parser.parse_args(arguments)
    try:
        config = load_config(ROOT / ".env")
        if any(
            value is not None and value != config.environment
            for value in (legacy_environment, args.environment)
        ):
            raise ConfigError("Requested environment differs from .env")
        command = [
            "bash",
            str(ROOT / "scripts/operations/api.sh"),
            args.target,
            args.method,
            args.path,
        ]
        if args.body is not None:
            command.append(args.body)
        return subprocess.run(command, cwd=ROOT, check=False).returncode
    except (ConfigError, OSError) as error:
        message = str(error) if isinstance(error, ConfigError) else "Native API command unavailable"
        print(f"ERROR: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
