#!/usr/bin/env python3
"""Deploy management inside its owned operator Job; children remain provisioner-owned."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from management_job import bootstrap_guards  # noqa: E402

from plane_demo.management.providers.credentials import StoredCredentials  # noqa: E402
from plane_demo.management.providers.identity import SECRET_KEYS, DemoConfig  # noqa: E402
from plane_demo.management.provisioner import service_provider  # noqa: E402
from plane_demo.management.provisioning import OperatorConfig, ProvisioningError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", default="management")
    parser.add_argument(
        "--config", type=Path, required=True, help="The operator Job's owned inputs"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    try:
        config = OperatorConfig.load(args.config)
        config.allocation(args.slot)
        if config.identity is None:
            raise ProvisioningError("bootstrap_identity_required")
        if args.slot != "management":
            raise ProvisioningError("children_are_provisioner_owned")
        supplied = {key: os.environ[key] for key in SECRET_KEYS if key in os.environ}
        identity = DemoConfig.from_values({**config.bootstrap_settings, **supplied})
        config = OperatorConfig.from_dict(config.to_dict(), identity=identity)
        with bootstrap_guards(config) as (active, writer):
            with service_provider(config, ROOT, guard=active, writer_guard=writer) as provider:
                provider.authenticate(workload_required=True)
                provider.get_access("management")
                provider.register("management")
                if not isinstance(provider.credentials, StoredCredentials):
                    raise ProvisioningError("service_credentials_required")
                if config.prepared_environments:
                    provider.credentials.seed_provided_keys(set(config.allocations))
                else:
                    provider.credentials.seed_provided_keys()
                url = provider.deploy_plane("management")
        print(json.dumps({"slot": "management", "url": url}))
        return 0
    except (ProvisioningError, ValueError, KeyError, OSError) as error:
        code = error.code if isinstance(error, ProvisioningError) else type(error).__name__
        print(f"Deployment failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
