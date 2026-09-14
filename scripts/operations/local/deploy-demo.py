#!/usr/bin/env python3
"""Initialize and deploy management through its Radius; the worker creates children later."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from plane_demo.management.providers.commands import create_json, write_json  # noqa: E402
from plane_demo.management.providers.credentials import Credentials  # noqa: E402
from plane_demo.management.providers.local import LocalProvider  # noqa: E402
from plane_demo.management.providers.local_config import LocalConfig  # noqa: E402
from plane_demo.management.provisioning import ProvisioningError  # noqa: E402


def deploy(root: Path) -> str:
    state = root / ".state/local"
    intent = state / "deploy-demo-intent.json"
    if intent.exists() or intent.is_symlink():
        raise ProvisioningError("local_deployment_already_attempted")
    config = LocalConfig.load(state / "provisioning.json")
    setup = json.loads((state / "setup-demo-complete.json").read_text())
    expected_hash = hashlib.sha256(
        json.dumps(config.to_dict(), sort_keys=True).encode()
    ).hexdigest()
    if (
        setup.get("configurationSHA256") != expected_hash
        or setup.get("managementUID") != config.management_cluster["uid"]
    ):
        raise ProvisioningError("local_setup_configuration_mismatch")
    credentials = Credentials(state / "credentials.json", environment="local")
    provider = LocalProvider(config, root, credentials)
    provider.authenticate()
    provider.connect_management()
    provider.verify_recipes()
    create_json(intent, {"version": 1, "managementUID": config.management_cluster["uid"]})
    url = provider.deploy_plane("management")
    write_json(
        state / "deploy-demo-complete.json",
        {
            "version": 1,
            "managementUID": config.management_cluster["uid"],
            "url": url,
        },
    )
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {"execute": False, "stage": "deploy-local-management", "createsClusters": False}
            )
        )
        return 0
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    try:
        print(json.dumps({"slot": "management", "url": deploy(ROOT)}))
        return 0
    except (ProvisioningError, ValueError, KeyError, OSError) as error:
        code = error.code if isinstance(error, ProvisioningError) else type(error).__name__
        print(f"Local deployment failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
