#!/usr/bin/env python3
"""Source/tar/YAML transformations for native local scripts. Never run a platform command."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path

import yaml

from plane_demo.management.providers import local_artifacts as recipe_assets

TERRAFORM_HASHES = {
    "arm64": "8891e9dcedc9e3b8950bc6af9d4d8af1f4cfade3062f53b9dc403a89f6ce8c9c",
    "amd64": "d25ce7b6902013ad905db3d2eab0be4cd905887fe88b81a6171b8d5503c31f3d",
}


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def runtime_helpers(root: Path):
    # Reuse only the existing byte-level proof functions, never Commands or its stateful CLI.
    directory = root / "scripts/operations/local"
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location(
        "runtime_image_proof", directory / "runtime-images.py"
    )
    require(spec is not None and spec.loader is not None, "Runtime proof helper is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = root
    return module


def provider_locks(root: Path) -> dict[str, tuple[str, set[str]]]:
    providers = {}
    for name in ("cluster", "postgresql", "redis", "gateway"):
        text = (root / "infra/radius/recipes/local" / name / ".terraform.lock.hcl").read_text()
        for address, body in re.findall(r'provider "([^"]+)" \{(.*?)\n\}', text, re.S):
            match = re.search(r'version\s*=\s*"([^"]+)"', body)
            require(match is not None, "A provider lock has no version")
            value = (match[1], set(re.findall(r'"zh:([a-f0-9]{64})"', body)))
            require(value[1], "A provider lock has no package hashes")
            require(
                address not in providers or providers[address] == value, "Provider locks conflict"
            )
            providers[address] = value
    require(providers, "Local provider locks are missing")
    return providers


def file_bytes(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.getmember(name)
    require(member.isfile(), "An expected image file is not a regular file")
    stream = archive.extractfile(member)
    require(stream is not None, "An expected image file is unreadable")
    return stream.read()


def runtime_permissions(archive: tarfile.TarFile, role: str) -> dict[str, int]:
    """Check exported ownership/modes for source and non-secret runtime artifacts."""
    uid = 10001 if role in {"api", "provisioner"} else 65532
    members = {item.name.removeprefix("./").rstrip("/"): item for item in archive.getmembers()}
    roots = ("app/src/plane_demo", "app/sql", "app/scripts", "app/infra/radius", "opt/radplanes")
    files = {"app/pyproject.toml", "app/uv.lock", "app/LICENSE"}
    executables = {
        "opt/radplanes/terraform",
        "usr/local/bin/rad",
        "usr/local/bin/kubectl",
        "home/plane/.rad/bin/bicep",
        "dynamic-rp",
    }
    checked = {}
    for name, member in members.items():
        if name not in files | executables and not any(
            name == root or name.startswith(root + "/") for root in roots
        ):
            continue
        required = 5 if member.isdir() or name in executables else 4
        require(
            member.isdir() or member.isfile(), "Runtime artifact is not a regular file/directory"
        )
        shift = 6 if member.uid == uid else 3 if member.gid == uid else 0
        require(
            (member.mode >> shift) & required == required,
            f"Runtime UID {uid} cannot access image path: {name}",
        )
        checked[name] = member.mode & 0o7777
        for parent in Path(name).parents:
            if str(parent) == ".":
                break
            directory = members.get(parent.as_posix())
            require(
                directory is not None and directory.isdir(),
                f"Runtime artifact parent is missing: {parent}",
            )
            shift = 6 if directory.uid == uid else 3 if directory.gid == uid else 0
            require(
                (directory.mode >> shift) & 1 == 1,
                f"Runtime UID {uid} cannot traverse image path: {parent}",
            )
            checked[parent.as_posix()] = directory.mode & 0o7777
    require(checked, "Image has no runtime files to inspect")
    return checked


def bundle_proof(archive: tarfile.TarFile, root: Path, arch: str) -> dict:
    locks = provider_locks(root)
    packages = {}
    prefix = "opt/radplanes/providers/"
    for address, (version, hashes) in locks.items():
        provider = address.rsplit("/", 1)[1]
        path = f"{prefix}{address}/terraform-provider-{provider}_{version}_linux_{arch}.zip"
        digest = hashlib.sha256(file_bytes(archive, path)).hexdigest()
        require(digest in hashes, "A packaged provider differs from the committed lock")
        packages[address] = digest
    require(
        file_bytes(archive, "opt/radplanes/terraform.tfrc")
        == (root / "scripts/operations/local/terraform.tfrc").read_bytes(),
        "Packaged Terraform configuration differs from source",
    )
    package = file_bytes(archive, "opt/radplanes/terraform.zip")
    require(
        hashlib.sha256(package).hexdigest() == TERRAFORM_HASHES[arch],
        "Packaged Terraform archive differs from its upstream pin",
    )
    with zipfile.ZipFile(io.BytesIO(package)) as terraform:
        require(
            file_bytes(archive, "opt/radplanes/terraform") == terraform.read("terraform"),
            "Packaged Terraform binary differs from the verified archive",
        )
    return {"providers": packages}


def inspect_image(root: Path, role: str, arch: str, path: Path, upstream: str | None) -> dict:
    helpers = runtime_helpers(root)
    if role in {"api", "provisioner"}:
        result = helpers.rootfs_proof(path, role, arch)
    else:
        result = {}
    with tarfile.open(path, "r:") as archive:
        result["runtimeModes"] = runtime_permissions(archive, role)
        if role != "api":
            result.update(bundle_proof(archive, root, arch))
        if role == "executor":
            require(
                upstream and re.fullmatch(r"[a-f0-9]{64}", upstream),
                "The inspected upstream Radius binary hash is required",
            )
            digest = hashlib.sha256(file_bytes(archive, "dynamic-rp")).hexdigest()
            require(digest == upstream, "The executor changed the upstream Radius binary")
            result["radiusBinarySHA256"] = digest
        if role == "operator":
            for name in ("rad", "kubectl"):
                path_in_image = f"usr/local/bin/{name}"
                require(
                    hashlib.sha256(file_bytes(archive, path_in_image)).hexdigest()
                    == helpers.TOOL_HASHES[arch][path_in_image],
                    "Packaged operator tool differs from its pin",
                )
        if role in {"operator", "provisioner"}:
            archives, server = recipe_assets.source_archives(root)
            require(
                file_bytes(archive, "opt/radplanes/bootstrap/module-server.py") == server
                and file_bytes(archive, "opt/radplanes/bootstrap/terraform-init.py")
                == (root / "scripts/operations/local/terraform-init.py").read_bytes(),
                "Prepared bootstrap code differs from source",
            )
            for kind, expected in archives.items():
                require(
                    file_bytes(archive, f"opt/radplanes/bootstrap/modules/{kind}/archive.tar.gz")
                    == expected,
                    "Prepared Recipe differs from source",
                )
            chart = file_bytes(archive, "opt/radplanes/bootstrap/radius.tgz")
            with tarfile.open(fileobj=io.BytesIO(chart), mode="r:gz") as package:
                metadata = yaml.safe_load(file_bytes(package, "radius/Chart.yaml"))
            require(
                metadata.get("name") == "radius" and metadata.get("version") == "0.60.2",
                "The packaged Radius chart is not 0.60.2",
            )
            images = json.loads(file_bytes(archive, "opt/radplanes/bootstrap/images.json"))
            require(isinstance(images, list) and images, "Packaged dependency images are missing")
            for image in images:
                require(
                    isinstance(image, dict)
                    and re.fullmatch(r"sha256:[a-f0-9]{64}", image.get("id", ""))
                    and re.fullmatch(
                        r"(?:ghcr.io/radius-project|docker.io/library|docker.io/envoyproxy|"
                        r"kindest)/[a-zA-Z0-9._/-]+(?::[a-zA-Z0-9._-]+)?"
                        r"(?:@sha256:[a-f0-9]{64})?",
                        image.get("reference", ""),
                    ),
                    "A packaged dependency identity is invalid",
                )
            result["chartSHA256"] = hashlib.sha256(chart).hexdigest()
            result["dependencies"] = images
    return result


def radius_images(text: str, executor: str) -> list[str]:
    images = set()
    for document in yaml.safe_load_all(text):
        if document is None:
            continue
        # Include dynamically launched Bicep images embedded in Radius ConfigMap YAML.
        for image in re.findall(
            r"ghcr\.io/radius-project/[a-z0-9-]+:[a-zA-Z0-9._-]+",
            json.dumps(document),
        ):
            images.add(image)

        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "image" and isinstance(child, str) and child != executor:
                        require(
                            child.startswith("ghcr.io/radius-project/"),
                            "Unexpected chart dependency registry",
                        )
                        images.add(child)
                    else:
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(document)
    require(images, "No Radius images were rendered")
    return sorted(images)


def executor_overlay(root: Path, executor: str, gid: int) -> dict:
    value = yaml.safe_load((root / "scripts/operations/local/dynamic-rp-overlay.yaml").read_text())
    spec = value["spec"]["template"]["spec"]
    spec["initContainers"][0]["image"] = executor
    spec["securityContext"]["supplementalGroups"] = [gid]
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    images = sub.add_parser("images")
    images.add_argument("executor")
    overlay = sub.add_parser("overlay")
    overlay.add_argument("root", type=Path)
    overlay.add_argument("executor")
    overlay.add_argument("gid", type=int)
    proof = sub.add_parser("inspect")
    proof.add_argument("root", type=Path)
    proof.add_argument("role", choices=("api", "provisioner", "executor", "operator"))
    proof.add_argument("arch", choices=("amd64", "arm64"))
    proof.add_argument("archive", type=Path)
    proof.add_argument("--upstream")
    package = sub.add_parser("package-recipes")
    package.add_argument("root", type=Path)
    package.add_argument("destination", type=Path)
    package.add_argument("revision")
    setup = sub.add_parser("setup-plan")
    setup.add_argument("directory", type=Path)
    setup.add_argument("inputs", type=Path)
    setup.add_argument("prefix")
    setup.add_argument("group")
    setup.add_argument("access_namespace")
    setup.add_argument("node_address")
    owned = sub.add_parser("owned-object")
    owned.add_argument("expected", type=Path)
    owned.add_argument("actual", type=Path)
    owned.add_argument("--patch", action="store_true")
    settings = sub.add_parser("terraform-settings")
    settings.add_argument("configmap", type=Path)
    live = sub.add_parser("live-config")
    live.add_argument("project")
    live.add_argument("deployment")
    live.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "images":
            value = radius_images(sys.stdin.read(), args.executor)
        elif args.action == "overlay":
            require(0 <= args.gid <= 2**31 - 1, "Invalid socket group")
            value = executor_overlay(args.root, args.executor, args.gid)
        elif args.action == "package-recipes":
            value = recipe_assets.package(args.root, args.destination, args.revision)
        elif args.action == "setup-plan":
            value = recipe_assets.setup_plan(
                args.directory,
                json.loads(args.inputs.read_text()),
                args.prefix,
                args.group,
                args.access_namespace,
                args.node_address,
            )
        elif args.action == "owned-object":
            expected = json.loads(args.expected.read_text())
            actual = json.loads(args.actual.read_text())
            require(
                recipe_assets.matches(expected, actual, patch=args.patch),
                "Existing object has another owner or spec",
            )
            value = {"matchedFields": sorted(expected)}
        elif args.action == "terraform-settings":
            configmap = json.loads(args.configmap.read_text())
            settings = yaml.safe_load(configmap["data"]["radius-self-host.yaml"])
            require(settings["terraform"]["path"] == "/terraform", "Terraform path differs")
            settings["terraform"]["logLevel"] = "OFF"
            value = {"data": {"radius-self-host.yaml": yaml.safe_dump(settings)}}
        elif args.action == "live-config":
            value = recipe_assets.live_config(
                args.project,
                args.deployment,
                json.loads((args.directory / "images.json").read_text()),
                json.loads((args.directory / "plan.json").read_text()),
                json.loads((args.directory / "node.json").read_text()),
                json.loads((args.directory / "namespace.json").read_text()),
                json.loads((args.directory / "service.json").read_text()),
                (args.directory / "ca").read_text().strip(),
            )
        else:
            value = inspect_image(args.root, args.role, args.arch, args.archive, args.upstream)
        print(json.dumps(value))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        tarfile.TarError,
        zipfile.BadZipFile,
        yaml.YAMLError,
    ) as error:
        print(f"Local artifact inspection failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
