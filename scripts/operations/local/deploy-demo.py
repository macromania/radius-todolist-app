#!/usr/bin/env python3
"""Deploy management from canonical local setup and owned service credentials."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from plane_demo.management.providers.commands import Commands, write_private  # noqa: E402
from plane_demo.management.providers.credentials import StoredCredentials  # noqa: E402
from plane_demo.management.providers.identity import PUBLIC_KEYS, DemoConfig  # noqa: E402
from plane_demo.management.providers.local_config import LocalConfig  # noqa: E402
from plane_demo.management.provisioning import ProvisioningError  # noqa: E402
from scripts.operations.config import load_config  # noqa: E402
from scripts.operations.local.operator_provider import local_operator_provider  # noqa: E402


def command(
    commands: Commands,
    argv: list[str],
    environment: dict[str, str],
    code: str,
    *,
    timeout: int = 600,
) -> str:
    try:
        output = commands.run(argv, env=environment, timeout=timeout)
    except (ProvisioningError, UnicodeError):
        raise ProvisioningError(code) from None
    if len(output) > 1_048_576:
        raise ProvisioningError(code)
    return output


def prepare_extensions(commands: Commands, environment: dict[str, str]) -> None:
    radius = ["rad", "--config", str(commands.state_root / "extensions-radius.yaml")]
    version = command(commands, [*radius, "version", "--cli"], environment, "radius_unavailable")
    compiler = Path(environment["HOME"]) / ".rad/bin/bicep"
    bicep_version = command(
        commands, [str(compiler), "--version"], environment, "radius_compiler_missing"
    )
    if not re.search(r"\bv?0\.60\.2\b", version) or not re.search(r"\b0\.42\.1\b", bicep_version):
        raise ProvisioningError("local_tool_version_mismatch")
    directory = commands.root / "infra/radius/types"
    with TemporaryDirectory(dir=directory, prefix=".extensions-") as temporary:
        for name in ("clusters", "postgresql", "gateways"):
            target = Path(temporary) / f"{name}.tgz"
            command(
                commands,
                [
                    *radius,
                    "bicep",
                    "publish-extension",
                    "--from-file",
                    str(directory / f"{name}.yaml"),
                    "--target",
                    str(target),
                    "--force",
                ],
                environment,
                "local_extension_preparation_failed",
            )
            if not target.is_file() or target.stat().st_size == 0:
                raise ProvisioningError("local_extension_preparation_failed")
            target.chmod(0o644)
            target.replace(directory / f"{name}.tgz")


def selected_config(selected: DemoConfig, value: dict) -> LocalConfig:
    public = value.get("bootstrapIdentity")
    if not isinstance(public, dict) or set(public) - PUBLIC_KEYS:
        raise ProvisioningError("local_setup_identity_mismatch")
    discovered = DemoConfig.from_values(public)
    if (
        (discovered.environment, discovered.project, discovered.deployment)
        != (selected.environment, selected.project, selected.deployment)
        or discovered.revision is None
        or (selected.revision is not None and selected.revision != discovered.revision)
    ):
        raise ProvisioningError("local_setup_identity_mismatch")
    identity = DemoConfig(
        "local",
        selected.project,
        selected.deployment,
        revision=discovered.revision,
        demo_keys=selected.demo_keys,
    )
    return LocalConfig.from_dict(value, identity=identity)


def deploy(root: Path) -> str:
    root = root.resolve()
    selected = load_config(root / ".env")
    if selected.environment != "local":
        raise ProvisioningError("local_environment_required")
    operator_environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "LC_ALL": "C",
    }
    if "TMPDIR" in os.environ:
        operator_environment["TMPDIR"] = os.environ["TMPDIR"]
    with TemporaryDirectory(prefix="plane-local-deploy-") as temporary:
        workspace = Path(temporary)
        commands = Commands(root, state_root=workspace, local=True)
        commands.protect(dict(selected.demo_keys))
        observed = command(
            commands,
            ["bash", str(root / "scripts/operations/local/setup.sh"), "inspect"],
            operator_environment,
            "local_setup_observation_failed",
        )
        try:
            value = json.loads(observed)
            if not isinstance(value, dict):
                raise ValueError
            config = selected_config(selected, value)
        except (ValueError, KeyError, TypeError):
            raise ProvisioningError("local_setup_observation_invalid") from None
        prepare_extensions(commands, operator_environment)
        host_text = command(
            commands,
            [
                "docker",
                "context",
                "inspect",
                "desktop-linux",
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            operator_environment,
            "local_docker_context_unavailable",
        )
        try:
            host = json.loads(host_text)
        except ValueError:
            raise ProvisioningError("local_docker_context_unavailable") from None
        if (
            not isinstance(host, str)
            or not re.fullmatch(r"unix:///[-a-zA-Z0-9_./]+", host)
            or ".." in Path(host.removeprefix("unix://")).parts
        ):
            raise ProvisioningError("local_docker_context_unavailable")
        allocation = config.allocation("management")
        home = workspace / "home"
        home.mkdir(mode=0o700)
        kubeconfig = workspace / "management.kubeconfig"
        environment = {
            "PATH": operator_environment["PATH"],
            "HOME": str(home),
            "LC_ALL": "C",
            "TMPDIR": str(workspace),
            "KUBECONFIG": str(kubeconfig),
            "DOCKER_HOST": host,
            "KIND_EXPERIMENTAL_PROVIDER": "docker",
        }
        profile = command(
            commands,
            ["kind", "get", "kubeconfig", "--name", allocation["clusterName"]],
            environment,
            "local_management_access_unavailable",
        )
        write_private(kubeconfig, profile)
        command(
            commands,
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "config",
                "rename-context",
                "kind-" + allocation["clusterName"],
                allocation["context"],
            ],
            environment,
            "local_management_context_invalid",
        )
        with local_operator_provider(
            config,
            root,
            kubeconfig=kubeconfig,
            context=allocation["context"],
        ) as provider:
            if not isinstance(provider.credentials, StoredCredentials):
                raise ProvisioningError("service_credentials_required")
            provider.credentials.seed_provided_keys()
            return provider.deploy_plane("management")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    "execute": False,
                    "stage": "deploy-local-management",
                    "createsClusters": False,
                }
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
    from scripts.operations.output import run_main

    raise SystemExit(run_main(main, "Local management deployment"))
