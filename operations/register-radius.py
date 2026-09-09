#!/usr/bin/env python3
"""Register one preallocated Azure plane's types and immutable Recipe mapping."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plane_demo.management.providers.azure import AzureProvider  # noqa: E402
from plane_demo.management.providers.credentials import Credentials  # noqa: E402
from plane_demo.management.provisioning import OperatorConfig, ProvisioningError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / ".state/azure/provisioning.json")
    args = parser.parse_args()
    try:
        config = OperatorConfig.load(args.config)
        config.allocation(args.slot)
        credentials = Credentials(ROOT / ".state/azure/credentials.json")
        provider = AzureProvider(config, ROOT, credentials)
        provider.authenticate()
        provider.get_access(args.slot)
        provider.register(args.slot)
        print(json.dumps({"slot": args.slot, "workspace": f"radplanes-{args.slot}"}))
        return 0
    except (ProvisioningError, ValueError, KeyError, OSError) as error:
        print(f"Registration failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
