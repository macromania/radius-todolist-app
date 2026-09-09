#!/usr/bin/env python3
"""Publish immutable Recipe artifacts with isolated registry credentials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from project import ROOT, SUBSCRIPTION, CommandError, az, run, state_dir, write_json


def main() -> int:
    state = state_dir("azure")
    foundation = json.loads((state / "bootstrap.outputs.json").read_text())["foundation"]
    registry = foundation["registryName"]
    host = foundation["registryLoginServer"]
    if not re.fullmatch(r"[a-z0-9]{5,50}", registry) or host != f"{registry}.azurecr.io":
        raise ValueError("Invalid project registry output")
    docker_config = state / "docker"
    docker_config.mkdir(mode=0o700, exist_ok=True)
    credential = az("acr", "login", "--name", registry, "--expose-token")
    environment = {**os.environ, "DOCKER_CONFIG": str(docker_config)}
    login = subprocess.run(
        [
            "docker",
            "--config",
            str(docker_config),
            "login",
            host,
            "--username",
            "00000000-0000-0000-0000-000000000000",
            "--password-stdin",
        ],
        input=credential["accessToken"],
        text=True,
        cwd=ROOT,
        env=environment,
        check=False,
    )
    if login.returncode:
        raise CommandError("Project registry login failed")
    del credential
    config_file = docker_config / "config.json"
    if config_file.exists():
        config_file.chmod(0o600)
    manifest_file = state / "recipes.json"
    manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
    for name in ("cluster", "postgresql", "gateway", "redis"):
        source = ROOT / "infra/radius/recipes/azure" / f"{name}.bicep"
        # Cluster Recipes contain modules; include all bootstrap module sources in the version.
        inputs = [source, source.parent / "bicepconfig.json"]
        if name == "cluster":
            inputs.extend(sorted((ROOT / "infra/bootstrap").glob("*.bicep")))
        compiler = run([str(Path.home() / ".rad/bin/bicep"), "--version"], capture=True)
        source_hash = hashlib.sha256(
            compiler.encode() + b"".join(path.read_bytes() for path in inputs)
        ).hexdigest()
        tag = f"src-{source_hash[:20]}"
        repository = f"radius-recipes/{name}"
        image = f"{repository}:{tag}"
        existing_repositories = az("acr", "repository", "list", "--name", registry)
        existing_tags = (
            az("acr", "repository", "show-tags", "--name", registry, "--repository", repository)
            if repository in existing_repositories
            else []
        )
        if tag in existing_tags:
            trusted = manifest.get(name)
            if not trusted or trusted.get("reference") != f"{host}/{image}":
                raise CommandError("Existing Recipe tag has no trusted local digest record")
            if trusted.get("source_sha256") != source_hash:
                raise CommandError(
                    "Existing Recipe source identity does not match the local source"
                )
        else:
            run(
                [
                    "rad",
                    "--config",
                    str(state / "radius.yaml"),
                    "bicep",
                    "publish",
                    "--file",
                    str(source),
                    "--target",
                    f"br:{host}/{image}",
                ],
                env=environment,
            )
        details = az("acr", "repository", "show", "--name", registry, "--image", image)
        digest = details["digest"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("Registry returned an invalid Recipe digest")
        if tag in existing_tags and trusted.get("digest") != digest:
            raise CommandError("Existing Recipe digest differs from the trusted record")
        lock = az(
            "acr",
            "repository",
            "update",
            "--name",
            registry,
            "--image",
            image,
            "--write-enabled",
            "false",
            "--delete-enabled",
            "false",
        )
        if (
            lock["digest"] != digest
            or lock["changeableAttributes"]["writeEnabled"]
            or lock["changeableAttributes"]["deleteEnabled"]
        ):
            raise CommandError("Could not verify immutable Recipe tag")
        manifest[name] = {
            "reference": f"{host}/{image}",
            "digest": digest,
            "source_sha256": source_hash,
        }
        write_json(manifest_file, manifest)
        print(f"{name}: {host}/{image}@{digest} (tag locked)")
    print(f"Recipe manifest saved; subscription={SUBSCRIPTION}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CommandError, ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
