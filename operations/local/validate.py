#!/usr/bin/env python3
"""Validate the published Recipe bytes using only Terraform mock providers."""

import io
import os
import shutil
import sys
import tarfile
from pathlib import Path

from common import ROOT, STATE, Commands, LocalError, private_dir
from prepare import RECIPE_FILES, module_archive


def validate() -> None:
    commands = Commands()
    # Keep scratch and provider sockets inside Terraform's project working directory.
    commands.env.update({"TMPDIR": "."})
    for recipe in RECIPE_FILES:
        source = ROOT / "infra/radius/recipes/local" / recipe
        commands.run(
            ["terraform", f"-chdir={source}", "fmt", "-check"],
            visible=True,
        )
        root = Path(STATE / "validation" / f"{recipe}-{os.getpid()}")
        if root.exists():
            raise LocalError(f"Refusing to reuse validation work: {root}")
        private_dir(root)
        try:
            with tarfile.open(fileobj=io.BytesIO(module_archive(recipe)), mode="r:gz") as archive:
                archive.extractall(root, filter="data")
            (root / "tests").mkdir(mode=0o700)
            shutil.copyfile(
                ROOT / f"tests/operations/local/terraform/{recipe}.tftest.hcl",
                root / f"tests/{recipe}.tftest.hcl",
            )
            terraform = ["terraform", f"-chdir={root}"]
            commands.run(
                [*terraform, "init", "-backend=false", "-input=false", "-lockfile=readonly"],
                visible=True,
                timeout=300,
            )
            commands.run([*terraform, "validate"], visible=True)
            commands.run([*terraform, "test"], visible=True)
        finally:
            shutil.rmtree(root)
    print("Offline Recipe checks passed; no cluster creation, state lifecycle, or live TLS proof.")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        validate()
    except (LocalError, OSError, ValueError) as error:
        print(f"Offline validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
