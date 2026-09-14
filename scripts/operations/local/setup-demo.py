#!/usr/bin/env python3
"""Prepare the existing management Radius for the full local demo; never create a cluster."""

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

from plane_demo.management.providers.commands import Commands, create_json, write_json  # noqa: E402
from plane_demo.management.providers.credentials import Credentials  # noqa: E402
from plane_demo.management.providers.local import LocalProvider, decode_access  # noqa: E402
from plane_demo.management.providers.local_config import SLOTS, LocalConfig  # noqa: E402
from plane_demo.management.provisioning import ProvisioningError  # noqa: E402


def build_config(root: Path, commands: Commands, bundle: dict) -> LocalConfig:
    state = root / ".state/local"
    reviewed = json.loads((state / "runtime-images.json").read_text())
    created = json.loads((state / "management-created.json").read_text())
    dirty = commands.run(
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
    if (
        reviewed.get("content_verified") is not True
        or dirty
        or reviewed["source_revision"] != commands.run(["git", "rev-parse", "HEAD"])
        or created.get("secretEncryptionVerified") is not True
        or created["name"] != "radplanes-local-management"
        or created["context"] != "radplanes-local-management"
        or not (state / "installed.json").is_file()
    ):
        raise ProvisioningError("local_bootstrap_or_images_unverified")
    path = state / "home/.kube/config"
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ProvisioningError("invalid_local_kubeconfig")
    _, ca, _ = decode_access(path.read_text(), "radplanes-local-management", child=False)

    def read(*args: str) -> dict:
        return commands.json(
            [
                "kubectl",
                "--kubeconfig",
                str(path),
                "--context",
                "radplanes-local-management",
                "--request-timeout=30s",
                *args,
                "-o",
                "json",
            ]
        )

    namespace = read("get", "namespace", "kube-system")
    node = read("get", "node", "radplanes-local-management-control-plane")
    service = read("-n", "default", "get", "service", "kubernetes")
    addresses = [
        item["address"] for item in node["status"]["addresses"] if item["type"] == "InternalIP"
    ]
    if addresses != [created["nodeAddress"]]:
        raise ProvisioningError("local_management_identity_mismatch")
    return LocalConfig.from_dict(
        {
            "version": 1,
            "provider": "local",
            "projectName": "radplanes",
            "allocations": {
                slot: {
                    "slot": slot,
                    "clusterName": f"radplanes-local-{slot}",
                    "context": f"radplanes-local-{slot}",
                    "gatewayPort": 35490 + index,
                    "apiPort": 35495 + index,
                }
                for index, slot in enumerate(SLOTS)
            },
            "recipes": {
                kind: {
                    "reference": module["url"],
                    "digest": "sha256:" + module["sha256"],
                    "moduleServer": module["moduleServer"],
                }
                for kind, module in bundle["modules"].items()
            },
            "images": {
                role: {
                    "reference": reviewed[role]["reference"],
                    "imageId": reviewed[role]["image_id"],
                }
                for role in ("api", "provisioner")
            },
            "managementCluster": {
                "clusterId": "kind://radplanes-local-management",
                "uid": namespace["metadata"]["uid"],
                "nodeAddress": created["nodeAddress"],
                "serviceAddress": service["spec"]["clusterIP"],
                "caSHA256": hashlib.sha256(ca).hexdigest(),
            },
        }
    )


def setup(root: Path) -> LocalConfig:
    state = root / ".state/local"
    intent = state / "setup-demo-intent.json"
    if intent.exists() or intent.is_symlink() or (state / "provisioning.json").exists():
        raise ProvisioningError("local_setup_already_attempted")
    commands = Commands(root, state_root=state, local=True)
    bundle = commands.json(
        [sys.executable, str(root / "scripts/operations/local/recipe-bundle.py")]
    )
    config = build_config(root, commands, bundle)
    create_json(intent, {"version": 1, "managementUID": config.management_cluster["uid"]})
    credentials = Credentials(state / "credentials.json", environment="local")
    provider = LocalProvider(config, root, credentials, commands)
    provider.authenticate()
    provider.apply("management", bundle["objects"])
    for module in config.recipes.values():
        provider.kubectl(
            "management",
            "-n",
            "radius-system",
            "rollout",
            "status",
            f"deployment/{module['moduleServer']}",
            "--timeout=180s",
        )
    provider.verify_recipes()
    # Preserve the proven management dynamic-rp executor and its sole Docker mount.
    provider.configure_child_terraform("management", ("applications-rp",))
    provider.register("management")
    provider.connect_management()
    write_json(
        state / "setup-demo-complete.json",
        {
            "version": 1,
            "managementUID": config.management_cluster["uid"],
            "configurationSHA256": hashlib.sha256(
                json.dumps(config.to_dict(), sort_keys=True).encode()
            ).hexdigest(),
        },
    )
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {"execute": False, "stage": "setup-local-management", "createsClusters": False}
            )
        )
        return 0
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    try:
        config = setup(ROOT)
        print(
            json.dumps(
                {"configured": "management", "clusterId": config.expected_cluster_id("management")}
            )
        )
        return 0
    except (ProvisioningError, ValueError, KeyError, OSError) as error:
        code = error.code if isinstance(error, ProvisioningError) else type(error).__name__
        print(f"Local setup failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
