#!/usr/bin/env python3
"""Deploy an existing plane; this command never creates an AKS cluster."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plane_demo.management.providers.azure import AzureProvider  # noqa: E402
from plane_demo.management.providers.credentials import Credentials  # noqa: E402
from plane_demo.management.provisioning import OperatorConfig, ProvisioningError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", default="management")
    parser.add_argument("--config", type=Path, default=ROOT / ".state/azure/provisioning.json")
    parser.add_argument("--credentials", type=Path, default=ROOT / ".state/azure/credentials.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        config = OperatorConfig.load(args.config)
        config.allocation(args.slot)
        state = (ROOT / ".state/azure").resolve()
        if not args.credentials.resolve().is_relative_to(state):
            raise ValueError("credentials must remain in project .state/azure")
        credentials = Credentials(args.credentials)
        provider = AzureProvider(config, ROOT, credentials)
        provider.authenticate()
        provider.get_access(args.slot)
        provider.register(args.slot)
        url = provider.deploy_plane(args.slot)
        print(json.dumps({"slot": args.slot, "url": url}))
        return 0
    except (ProvisioningError, ValueError, KeyError, OSError) as error:
        code = error.code if isinstance(error, ProvisioningError) else type(error).__name__
        print(f"Deployment failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
