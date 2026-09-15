#!/usr/bin/env python3
"""Validate the published Recipe bytes using only Terraform mock providers."""

import argparse
import io
import logging
import os
import shutil
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

from common import ROOT, LocalError
from prepare import RECIPE_FILES, module_archive

from plane_demo.management.providers.commands import Commands
from plane_demo.management.provisioning import ProvisioningError


def validate(
    *,
    recipes: tuple[str, ...] | None = None,
    offline: bool = False,
    provider_directory: Path | None = None,
) -> None:
    with TemporaryDirectory(prefix="plane-recipe-check-") as temporary:
        workspace = Path(temporary)
        (workspace / "home").mkdir(mode=0o700)
        commands = Commands(ROOT, state_root=workspace, local=True)
        commands.environment.update({"TMPDIR": ".", "CHECKPOINT_DISABLE": "1"})
        guard = workspace / "no-live-tools"
        guard.mkdir(mode=0o700)
        for tool in ("docker", "kind", "kubectl", "az"):
            executable = guard / tool
            executable.write_text(
                "#!/bin/sh\n"
                "echo 'Live platform commands are forbidden in Recipe mock tests' >&2\n"
                "exit 99\n"
            )
            executable.chmod(0o700)
        commands.environment["PATH"] = str(guard) + os.pathsep + commands.environment["PATH"]

        def run(arguments, *, timeout=300):
            output = commands.run(arguments, timeout=timeout)
            if output:
                print(output)

        for recipe in recipes or tuple(RECIPE_FILES):
            source = ROOT / "infra/radius/recipes/local" / recipe
            cache = provider_directory or source / ".terraform/providers"
            if offline and not cache.is_dir():
                raise LocalError(f"Prepared native provider cache is missing for {recipe}")
            run(["terraform", f"-chdir={source}", "fmt", "-check"])
            root = workspace / recipe
            root.mkdir(mode=0o700)
            with tarfile.open(fileobj=io.BytesIO(module_archive(recipe)), mode="r:gz") as archive:
                archive.extractall(root, filter="data")
            (root / "tests").mkdir(mode=0o700)
            shutil.copyfile(
                ROOT / f"tests/operations/local/terraform/{recipe}.tftest.hcl",
                root / f"tests/{recipe}.tftest.hcl",
            )
            terraform = ["terraform", f"-chdir={root}"]
            run(
                [
                    *terraform,
                    "init",
                    "-backend=false",
                    "-input=false",
                    "-lockfile=readonly",
                    *([f"-plugin-dir={cache.resolve()}"] if offline else []),
                ],
            )
            run([*terraform, "validate"])
            run([*terraform, "test"])
    print("Offline Recipe checks passed; no cluster creation, state lifecycle, or live TLS proof.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--recipe", choices=tuple(RECIPE_FILES), action="append")
    parser.add_argument("--provider-directory", type=Path)
    args = parser.parse_args()
    if args.provider_directory and not args.offline:
        parser.error("--provider-directory requires --offline")
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    try:
        validate(
            recipes=tuple(args.recipe) if args.recipe else None,
            offline=args.offline,
            provider_directory=args.provider_directory,
        )
    except (LocalError, ProvisioningError, OSError, ValueError) as error:
        print(f"Offline validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
