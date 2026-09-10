#!/usr/bin/env python3
"""Validate the published Recipe bytes using only Terraform mock providers."""

import io
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from common import ROOT, STATE, Commands, LocalError, private_dir
from prepare import module_archive


def validate() -> None:
    commands = Commands()
    commands.run(
        ["terraform", f"-chdir={ROOT / 'infra/radius/recipes/local/cluster'}", "fmt", "-check"],
        visible=True,
    )
    with tempfile.TemporaryDirectory(
        prefix="terraform-",
        dir=private_dir(STATE / "validation"),
    ) as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(module_archive()), mode="r:gz") as archive:
            archive.extractall(root, filter="data")
        (root / "tests").mkdir(mode=0o700)
        shutil.copyfile(
            ROOT / "tests/operations/local/terraform/cluster.tftest.hcl",
            root / "tests/cluster.tftest.hcl",
        )
        terraform = ["terraform", f"-chdir={root}"]
        commands.run(
            [*terraform, "init", "-backend=false", "-input=false", "-lockfile=readonly"],
            visible=True,
            timeout=300,
        )
        commands.run([*terraform, "validate"], visible=True)
        commands.run([*terraform, "test"], visible=True)
    print("Offline Recipe checks passed; no cluster creation, state lifecycle, or live TLS proof.")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        validate()
    except (LocalError, OSError, ValueError) as error:
        print(f"Offline validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
