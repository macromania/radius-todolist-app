#!/usr/bin/env python3
"""Build or inspect the local executor/operator images, only with --execute."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from common import (
    RADIUS_IMAGE,
    ROOT,
    STATE,
    Commands,
    LocalError,
    docker,
    image_names,
    write_private,
)


def architecture(commands: Commands) -> str:
    info = commands.json(docker("info", "--format", "{{json .}}"))
    if info["OperatingSystem"] != "Docker Desktop" or info["OSType"] != "linux":
        raise LocalError("This gate supports only the explicitly addressed Docker Desktop daemon")
    names = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
    if info["Architecture"] not in names:
        raise LocalError("Docker architecture must be Linux arm64 or amd64")
    return names[info["Architecture"]]


def build(commands: Commands) -> None:
    arch = architecture(commands)
    for target, name in image_names().items():
        commands.run(
            docker(
                "build",
                "--platform",
                f"linux/{arch}",
                "--target",
                target,
                "--tag",
                name,
                str(ROOT / "images/radius-kind"),
            ),
            visible=True,
            timeout=1200,
        )


def inspect_images(commands: Commands) -> dict:
    arch = architecture(commands)
    commands.run(docker("pull", "--platform", f"linux/{arch}", RADIUS_IMAGE), visible=True)
    base_hash = commands.run(
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--pull=never",
            "--name",
            "radplanes-local-inspect-upstream",
            "--entrypoint",
            "/bin/sh",
            RADIUS_IMAGE,
            "-ec",
            "sha256sum /dynamic-rp",
        )
    ).split()[0]
    record = {"architecture": arch, "inspectedAt": datetime.now(UTC).isoformat(), "images": {}}
    for target, name in image_names().items():
        info = commands.json(docker("image", "inspect", name))[0]
        if info["Architecture"] != arch or info["Os"] != "linux":
            raise LocalError(f"{target} does not match Docker Desktop's native architecture")
        if info["Config"]["User"] != "65532:65532":
            raise LocalError(f"{target} is not the expected non-root image")
        if target == "executor":
            if info["Config"]["Entrypoint"] != ["/dynamic-rp"]:
                raise LocalError("The executor must retain the upstream Radius entrypoint")
            script = (
                "sha256sum /dynamic-rp; docker --version; /opt/radplanes/terraform version; "
                "git --version; test -s /etc/ssl/certs/ca-certificates.crt"
            )
        else:
            script = "rad version --cli; kubectl version --client; python3 --version"
        content = commands.run(
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--pull=never",
                "--name",
                f"radplanes-local-inspect-{target}",
                "--entrypoint",
                "/bin/sh",
                name,
                "-ec",
                script,
            )
        )
        if target == "executor" and (
            content.split()[0] != base_hash
            or "29.2.1" not in content
            or "Terraform v1.15.8" not in content
        ):
            raise LocalError("Executor contents differ from the pinned Radius/Docker/Terraform")
        if target == "operator" and ("0.60.2" not in content or "v1.35.7" not in content):
            raise LocalError("Operator CLI versions do not match the pins")
        record["images"][target] = {"name": name, "id": info["Id"], "contents": content}
    write_private(STATE / "image-review.json", record)
    return record


def require_inspection(commands: Commands) -> dict:
    record = json.loads((STATE / "image-review.json").read_text())
    if record["architecture"] != architecture(commands):
        raise LocalError("Image inspection belongs to a different Docker architecture")
    for target, name in image_names().items():
        info = commands.json(docker("image", "inspect", name))[0]
        if record["images"][target]["name"] != name or record["images"][target]["id"] != info["Id"]:
            raise LocalError("Images changed after inspection; inspect the actual contents again")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["build", "inspect"])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"stage": args.stage, "images": image_names(), "execute": False}))
        return 0
    os.umask(0o077)
    try:
        commands = Commands()
        if args.stage == "build":
            build(commands)
        else:
            print(json.dumps(inspect_images(commands), indent=2))
        return 0
    except (LocalError, OSError, ValueError, KeyError) as error:
        print(f"Image {args.stage} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
