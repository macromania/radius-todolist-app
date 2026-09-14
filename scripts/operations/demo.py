#!/usr/bin/env python3
"""One operator entrypoint with explicit, non-executable deployment configuration."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.operations.config import (  # noqa: E402
    SECRET_KEYS,
    ConfigError,
    DemoConfig,
    initialize_config,
    load_config,
)


def current_subscription() -> str:
    try:
        result = subprocess.run(
            ["az", "account", "show", "--query", "id", "--output", "json", "--only-show-errors"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError(
            "Azure account lookup failed; provide --subscription explicitly"
        ) from None
    if result.returncode:
        raise ConfigError("Azure account lookup failed; provide --subscription explicitly")
    try:
        identifier = json.loads(result.stdout)
    except ValueError:
        raise ConfigError("Azure account lookup returned invalid JSON") from None
    if not isinstance(identifier, str):
        raise ConfigError("Azure account lookup returned no subscription")
    return identifier


def answer(label: str, value: str | None, default: str | None = None) -> str:
    if value is not None:
        return value
    if sys.stdin.isatty():
        selected = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
        if selected:
            return selected
    if default is not None:
        return default
    raise ConfigError(f"{label} is required in noninteractive mode")


def initialize(args: argparse.Namespace) -> DemoConfig:
    environment = answer("Environment (azure/local)", args.environment)
    values = {
        "DEMO_ENV": environment,
        "DEMO_PROJECT": answer("Project", args.project, "radplanes"),
        "DEMO_DEPLOYMENT": answer("Deployment", args.deployment, "learning"),
    }
    if environment == "azure":
        subscription = args.subscription
        if subscription is None:
            subscription = answer("Subscription", None, current_subscription())
        values["AZURE_SUBSCRIPTION_ID"] = subscription
        values["AZURE_LOCATION"] = answer("Azure location", args.location, "centralus")
        if args.key_vault:
            values["DEMO_KEY_VAULT"] = args.key_vault
    elif any((args.subscription, args.location, args.key_vault)):
        raise ConfigError("Local initialization does not accept Azure settings")
    if args.revision:
        values["DEMO_REVISION"] = args.revision
    for entry in args.demo_key_from_env:
        slot, separator, variable = entry.partition("=")
        key = next((key for key, name in SECRET_KEYS.items() if name == slot), None)
        if not separator or key is None or not variable.isidentifier():
            raise ConfigError("Use --demo-key-from-env SLOT=VARIABLE")
        if key in values:
            raise ConfigError("Duplicate demo key input")
        value = os.environ.get(variable)
        if value is None:
            raise ConfigError("The supplied demo-key environment variable is missing")
        values[key] = value
    for slot in args.prompt_demo_key:
        key = next((key for key, name in SECRET_KEYS.items() if name == slot), None)
        if key is None or key in values:
            raise ConfigError("Unknown or duplicate demo-key slot")
        if not sys.stdin.isatty():
            raise ConfigError("A terminal is required for a credential prompt")
        values[key] = getpass.getpass(f"Demo key for {slot}: ")
    config = DemoConfig.from_values(values)
    initialize_config(config, ROOT / ".env")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create or replace the checkout's .env")
    init.add_argument("--environment", choices=("azure", "local"))
    init.add_argument("--project")
    init.add_argument("--deployment")
    init.add_argument("--subscription")
    init.add_argument("--location")
    init.add_argument("--key-vault")
    init.add_argument("--revision")
    init.add_argument("--demo-key-from-env", action="append", default=[], metavar="SLOT=VARIABLE")
    init.add_argument("--prompt-demo-key", action="append", default=[], metavar="SLOT")
    commands.add_parser("config", help="Show selected configuration with secret values redacted")
    args = parser.parse_args(argv)
    try:
        config = initialize(args) if args.command == "init" else load_config(ROOT / ".env")
        print(json.dumps(config.values(), indent=2))
        return 0
    except (ConfigError, OSError, EOFError, KeyboardInterrupt) as error:
        message = str(error) if isinstance(error, ConfigError) else "Configuration operation failed"
        print(f"ERROR: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
