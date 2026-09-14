#!/usr/bin/env python3
"""Build digest-pinned images in the project's ACR with complete build output."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys

from project import ROOT, SUBSCRIPTION, CommandError, az, run, state_dir, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prototype", action="store_true")
    args = parser.parse_args()
    state = state_dir("azure")
    foundation = json.loads((state / "bootstrap.outputs.json").read_text())["foundation"]
    registry, host = foundation["registryName"], foundation["registryLoginServer"]
    if not re.fullmatch(r"[a-z0-9]{5,50}", registry) or host != f"{registry}.azurecr.io":
        raise ValueError("Invalid project registry")
    revision = run(["git", "rev-parse", "HEAD"], capture=True)
    dirty = run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "src",
            "sql",
            "scripts",
            "infra",
            "images",
            "pyproject.toml",
            "uv.lock",
            ".dockerignore",
        ],
        capture=True,
    )
    if dirty and not args.prototype:
        raise CommandError("Commit verified runtime sources before a release build")
    sources = {}
    for directory in ("src/plane_demo", "sql", "scripts"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".sql", ".sh"}:
                sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    source_hash = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    tag = revision[:12] if not args.prototype else f"gate-{source_hash[:16]}"
    images = {}
    for component in ("api", "provisioner"):
        image = f"plane-{component}:{tag}"
        command = [
            "az",
            "acr",
            "build",
            "--subscription",
            SUBSCRIPTION,
            "--registry",
            registry,
            "--platform",
            "linux/amd64",
            "--file",
            f"images/{component}/Dockerfile",
            "--image",
            image,
            "--build-arg",
            f"SOURCE_REVISION={revision}",
        ]
        if component == "provisioner":
            command.extend(
                ["--build-arg", "TARGETARCH=amd64", "--build-arg", f"API_IMAGE={images['api']}"]
            )
        run([*command, "."])
        digest = az(
            "acr",
            "repository",
            "show",
            "--name",
            registry,
            "--image",
            image,
        )["digest"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("Registry returned an invalid image digest")
        images[component] = f"{host}/plane-{component}@{digest}"
    write_json(
        state / "images.json",
        {
            **images,
            "source_revision": revision,
            "source_hashes": sources,
            "prototype": args.prototype,
            "content_verified": False,
        },
    )
    print(
        "Images built. Inspect their actual contents before deployment; "
        "verification is not inferred."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CommandError, ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
