#!/usr/bin/env python3
"""Build and inspect native API/provisioner images without publishing application source."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from common import ROOT, STATE, Commands, LocalError, docker, private_dir, write_private

from images import architecture

COMPONENTS = ("api", "provisioner")
API_MODULES = (
    "__init__",
    "management/__init__",
    "management/api",
    "control/__init__",
    "control/api",
    "control/reconciler",
    "data/__init__",
    "data/api",
    "data/reconciler",
    "shared/__init__",
    "shared/auth",
    "shared/db",
    "shared/http",
    "shared/kube",
    "shared/models",
    "shared/settings",
    "setup/__init__",
    "setup/bootstrap",
    "setup/acme_responder",
)
OPERATOR_FILES = (
    "project.py",
    "install-radius.py",
    "deploy-plane.py",
    "register-radius.py",
    "issue-certificate.py",
    "acme-hook.py",
    "run-certificate-job.py",
)
INPUTS = (
    "src",
    "sql",
    "images",
    "operations",
    "infra",
    "pyproject.toml",
    "uv.lock",
    ".dockerignore",
)
TOOL_HASHES = {
    "arm64": {
        "usr/local/bin/rad": "7de188b908157d19d6f0ab7a5736a2b3c083cb78416f98652c26b02f3a95a29e",
        "usr/local/bin/kubectl": "5103d45b8881434d417694057b8ccb5ae79fd310a5c5c13e403c5e62e15909be",
        "home/plane/.rad/bin/bicep": (
            "b01ac3bb5259096dfbe548138a538d1c4e4a55e6f87f3827e2299fbc2d4e6796"
        ),
    },
    "amd64": {
        "usr/local/bin/rad": "941daf7f646e93351a690f7327875e86adba761cf72d6e8238f1b6741798a745",
        "usr/local/bin/kubectl": "12e97f9d23a9f6cbb87b89becd6bd291e1a858a3379a4e11e2c822c4c1530052",
        "home/plane/.rad/bin/bicep": (
            "aed90eb2c69a6ee2bd70dc0d4354408ac4d04fd9911d3ec8e0cd74ad173e7139"
        ),
    },
}

INSPECT = r"""
import hashlib,importlib,io,json,os,sys,tarfile
from pathlib import Path
component=sys.argv[1]
root=Path("/app")
if os.getuid()!=10001:
    raise RuntimeError("unexpected_runtime_user")
for module in ("management.api","control.api","control.reconciler","data.api","data.reconciler"):
    importlib.import_module("plane_demo."+module)
if component=="api":
    if any((root/path).exists() for path in (
        "src/plane_demo/management/provisioner.py","src/plane_demo/management/providers","operations"
    )):
        raise RuntimeError("privileged_api_image")
else:
    importlib.import_module("plane_demo.management.provisioner")
    importlib.import_module("plane_demo.management.providers.local")
files=["pyproject.toml","uv.lock"]
for directory in ("src/plane_demo","sql","operations","infra/radius"):
    files.extend(str(p.relative_to(root)) for p in (root/directory).rglob("*")
                 if p.is_file()
                 and p.suffix in (".py",".sql",".bicep",".yaml",".json",".tf",".hcl",".sh")
                 and ".terraform" not in p.parts)
hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sorted(files)}
extensions={}
if component=="provisioner":
    for name in ("clusters","postgresql","gateways"):
        with tarfile.open(root/("infra/radius/types/"+name+".tgz"),"r:gz") as outer:
            if outer.getnames()!=["types.tgz"]: raise RuntimeError("invalid_extension")
            nested=outer.extractfile("types.tgz").read()
        with tarfile.open(fileobj=io.BytesIO(nested),mode="r:gz") as inner:
            if sorted(inner.getnames())!=["index.json","types.json"]:
                raise RuntimeError("invalid_extension_members")
            extensions[name]={m:hashlib.sha256(inner.extractfile(m).read()).hexdigest()
                              for m in inner.getnames()}
print(json.dumps({"uid":os.getuid(),"component":component,"source_hashes":hashes,
                  "extension_members":extensions}))
"""


def source_revision(commands: Commands) -> str:
    revision = commands.run(["git", "rev-parse", "HEAD"]).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise LocalError("Invalid committed source identity")
    if commands.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *INPUTS]
    ).strip():
        raise LocalError("Commit all image inputs before building or inspecting runtime images")
    return revision


def references(revision: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise LocalError("Runtime image tags require a full source commit")
    return {role: f"localhost/radplanes-plane-{role}:{revision}" for role in COMPONENTS}


def expected_hashes(role: str) -> dict[str, str]:
    paths = [ROOT / "pyproject.toml", ROOT / "uv.lock", *ROOT.glob("sql/*.sql")]
    if role == "api":
        paths += [ROOT / f"src/plane_demo/{name}.py" for name in API_MODULES]
    elif role == "provisioner":
        paths += [
            *ROOT.glob("src/plane_demo/**/*.py"),
            *(ROOT / "operations" / name for name in OPERATOR_FILES),
            *ROOT.glob("operations/local/*.py"),
        ]
        paths += [
            path
            for path in (ROOT / "infra/radius").rglob("*")
            if path.is_file()
            and path.suffix in {".bicep", ".yaml", ".json", ".tf", ".hcl", ".sh"}
            and ".terraform" not in path.parts
            and ".build" not in path.parts
        ]
    else:
        raise LocalError("Unknown runtime component")
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def expected_extensions() -> dict[str, dict[str, str]]:
    result = {}
    for name in ("clusters", "postgresql", "gateways"):
        with tarfile.open(ROOT / f"infra/radius/types/{name}.tgz", "r:gz") as outer:
            if outer.getnames() != ["types.tgz"]:
                raise LocalError("Unexpected extension envelope")
            nested = outer.extractfile("types.tgz").read()
        with tarfile.open(fileobj=io.BytesIO(nested), mode="r:gz") as inner:
            if sorted(inner.getnames()) != ["index.json", "types.json"]:
                raise LocalError("Unexpected extension contents")
            result[name] = {
                member: hashlib.sha256(inner.extractfile(member).read()).hexdigest()
                for member in inner.getnames()
            }
    return result


def rootfs_proof(path: Path, role: str, arch: str) -> dict:
    expected = expected_hashes(role)
    sources, tools, runtime = {}, {}, {}
    extensions = {}
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            relative = name.removeprefix("app/")
            if role == "api" and (
                name.startswith(("app/operations/", "app/src/plane_demo/management/providers/"))
                or name == "app/src/plane_demo/management/provisioner.py"
            ):
                raise LocalError("Exported API filesystem includes privileged code")
            source = name.startswith("app/") and (
                relative in ("pyproject.toml", "uv.lock")
                or (
                    relative.startswith(("src/plane_demo/", "sql/", "operations/", "infra/radius/"))
                    and Path(name).suffix
                    in {".py", ".sql", ".bicep", ".yaml", ".json", ".tf", ".hcl", ".sh"}
                )
            )
            runtime_file = name.startswith(("app/.venv/", "usr/local/lib/python3.13/")) or (
                name == "usr/local/bin/python3.13"
            )
            extension = role == "provisioner" and name in {
                f"app/infra/radius/types/{kind}.tgz"
                for kind in ("clusters", "postgresql", "gateways")
            }
            if not (source or runtime_file or extension or name in TOOL_HASHES[arch]):
                continue
            if not member.isfile():
                if source or extension or name in TOOL_HASHES[arch]:
                    raise LocalError("Required image content is not a regular file")
                if member.issym() or member.islnk():
                    runtime[name] = "link:" + member.linkname
                continue
            stream = archive.extractfile(member)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if source:
                sources[relative] = digest
            if runtime_file:
                runtime[name] = digest
            if name in TOOL_HASHES[arch]:
                tools[name] = digest
            if extension:
                payload = archive.extractfile(member).read()
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as outer:
                    if outer.getnames() != ["types.tgz"]:
                        raise LocalError("Unexpected exported extension envelope")
                    nested = outer.extractfile("types.tgz").read()
                with tarfile.open(fileobj=io.BytesIO(nested), mode="r:gz") as inner:
                    if sorted(inner.getnames()) != ["index.json", "types.json"]:
                        raise LocalError("Unexpected exported extension members")
                    extensions[Path(name).stem] = {
                        key: hashlib.file_digest(inner.extractfile(key), "sha256").hexdigest()
                        for key in inner.getnames()
                    }
    if sources != expected or "usr/local/bin/python3.13" not in runtime:
        raise LocalError("Exported runtime source or Python interpreter is missing or changed")
    if role == "provisioner" and (
        tools != TOOL_HASHES[arch] or extensions != expected_extensions()
    ):
        raise LocalError(
            "Exported administrative tools or extension payloads differ from their pins"
        )
    return {
        "source_hashes": sources,
        "extension_members": extensions,
        "tool_hashes": tools,
        "python_runtime_sha256": hashlib.sha256(
            json.dumps(runtime, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def exported_proof(commands: Commands, image_id: str, role: str, arch: str) -> dict:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise LocalError("Invalid immutable image ID")
    name = f"radplanes-local-image-export-{uuid4().hex[:12]}"
    container = commands.run(
        docker("create", "--network", "none", "--name", name, "--entrypoint", "/bin/true", image_id)
    ).strip()
    if not re.fullmatch(r"[a-f0-9]{64}", container):
        raise LocalError("Invalid inspection container identity")
    try:
        with tempfile.TemporaryDirectory(dir=private_dir(STATE / "image-inspection")) as directory:
            path = Path(directory) / "rootfs.tar"
            commands.run(docker("export", "--output", str(path), container), timeout=180)
            return rootfs_proof(path, role, arch)
    finally:
        commands.run(docker("rm", container))


def build(commands: Commands) -> dict:
    revision = source_revision(commands)
    arch = architecture(commands)
    names = references(revision)
    manifest = {
        "version": 1,
        "source_revision": revision,
        "architecture": arch,
        "content_verified": False,
    }
    for role in COMPONENTS:
        existing = commands.run(docker("image", "ls", "--quiet", names[role])).strip()
        if existing:
            raise LocalError(
                f"Image {names[role]} already exists; inspect it, do not replace its tag"
            )
    for role in COMPONENTS:
        command = docker(
            "build",
            "--platform",
            f"linux/{arch}",
            "--tag",
            names[role],
            "--file",
            "images/api/Dockerfile" if role == "api" else "images/local-provisioner/Dockerfile",
            "--build-arg",
            f"SOURCE_REVISION={revision}",
        )
        if role == "provisioner":
            command += [
                "--build-arg",
                f"API_IMAGE={names['api']}",
                "--build-arg",
                f"TARGETARCH={arch}",
            ]
        commands.run([*command, "."], visible=True, timeout=1800)
        info = commands.json(docker("image", "inspect", names[role]))[0]
        manifest[role] = {
            "reference": names[role],
            "image_id": info["Id"],
            **exported_proof(commands, info["Id"], role, arch),
        }
    if source_revision(commands) != revision:
        raise LocalError("Sources changed during the image build")
    write_private(STATE / "runtime-images.json", manifest)
    return manifest


def inspect_runtime(commands: Commands) -> dict:
    revision = source_revision(commands)
    arch = architecture(commands)
    names = references(revision)
    built = json.loads((STATE / "runtime-images.json").read_text())
    if built.get("source_revision") != revision or built.get("architecture") != arch:
        raise LocalError("A successful build manifest for this source and architecture is required")
    result = {
        "version": 1,
        "source_revision": revision,
        "architecture": arch,
        "content_verified": False,
    }
    for role in COMPONENTS:
        info = commands.json(docker("image", "inspect", names[role]))[0]
        if (
            built[role]["reference"] != names[role]
            or built[role]["image_id"] != info["Id"]
            or info["Architecture"] != arch
            or info["Os"] != "linux"
            or info["Config"]["User"] != "10001:10001"
            or info["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != revision
        ):
            raise LocalError("Runtime image platform, user, or source identity differs")
        proof = exported_proof(commands, info["Id"], role, arch)
        if any(built[role].get(key) != value for key, value in proof.items()):
            raise LocalError("The filesystem differs from the recorded guarded build")
        raw = commands.run(
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--pull=never",
                "--name",
                f"radplanes-local-inspect-runtime-{role}",
                "--entrypoint",
                "python",
                info["Id"],
                "-c",
                INSPECT,
                role,
            ),
            timeout=120,
        )
        observed = json.loads(raw)
        expected = expected_hashes(role)
        extensions = expected_extensions() if role == "provisioner" else {}
        if (
            observed.get("uid") != 10001
            or observed.get("component") != role
            or observed.get("source_hashes") != expected
            or observed.get("extension_members") != extensions
        ):
            raise LocalError(f"The actual {role} image contents differ from committed inputs")
        result[role] = {
            "reference": names[role],
            "image_id": info["Id"],
            **proof,
        }
    result.update(content_verified=True, inspected_at=datetime.now(UTC).isoformat())
    write_private(STATE / "runtime-images.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("build", "inspect"))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"stage": args.stage, "execute": False, "registry_push": False}))
        return 0
    os.umask(0o077)
    try:
        commands = Commands()
        value = build(commands) if args.stage == "build" else inspect_runtime(commands)
        print(
            json.dumps(
                {
                    "source_revision": value["source_revision"],
                    "architecture": value["architecture"],
                    "content_verified": value["content_verified"],
                    "images": {role: value[role]["reference"] for role in COMPONENTS},
                }
            )
        )
        return 0
    except (LocalError, OSError, ValueError, KeyError, tarfile.TarError) as error:
        print(f"Local runtime image {args.stage} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
