"""Inspect an exported, never-started image against a selected source checkout."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import marshal
import posixpath
import re
import shlex
import struct
import sys
import tarfile
import types
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from build_provenance import (
    ProvenanceError,
    fresh_record,
    source_fingerprint,
    verify_record,
)

PUBLIC_MODULES = {
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
}
PUBLIC_FILES = {f"app/src/plane_demo/{name}.py" for name in PUBLIC_MODULES}
TRANSIENT_COPIES = {
    "images/provisioner/requirements.txt": "/tmp/provisioner-requirements.txt",
    "images/provisioner/azure-cli.txt": "/tmp/azure-cli-requirements.txt",
}


class InspectionError(ValueError):
    """Fixed diagnostic codes, never file contents or credentials."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise InspectionError(code)


def kubelogin_spec(root: Path) -> dict[str, str]:
    text = (root / "images/provisioner/Dockerfile").read_text().replace("\\\n", "")
    digest = re.search(r"amd64\).*?LOGIN_SHA=([a-f0-9]{64})\s*;;", text)
    version = re.search(
        r"https://github.com/Azure/kubelogin/releases/download/(v[0-9.]+)/kubelogin-linux-\$TARGETARCH.zip",
        text,
    )
    if digest is None or version is None:
        raise InspectionError("kubelogin_pin_missing")
    return {
        "url": f"https://github.com/Azure/kubelogin/releases/download/{version[1]}/kubelogin-linux-amd64.zip",
        "sha256": digest[1],
    }


def kubelogin_hash(root: Path, archive: Path) -> str:
    with archive.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == kubelogin_spec(root)["sha256"],
            "kubelogin_archive_digest_mismatch",
        )
    with zipfile.ZipFile(archive) as zipped:
        member = zipped.getinfo("bin/linux_amd64/kubelogin")
        require(
            member.file_size <= 104_857_600 and not member.is_dir(), "invalid_kubelogin_archive"
        )
        with zipped.open(member) as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()


def expected_tools(root: Path, kubelogin_archive: Path | None = None) -> dict[str, str]:
    text = (root / "images/provisioner/Dockerfile").read_text().replace("\\\n", "")
    tools = re.search(r"amd64\) RAD_SHA=([a-f0-9]{64});\s+KUBE_SHA=([a-f0-9]{64});", text)
    bicep = re.search(r"amd64\) BICEP_ARCH=x64; BICEP_SHA=([a-f0-9]{64})\s*;;", text)
    if tools is None or bicep is None:
        raise InspectionError("image_tool_pins_missing")
    result = {
        "usr/local/bin/rad": tools[1],
        "usr/local/bin/kubectl": tools[2],
        "home/plane/.rad/bin/bicep": bicep[1],
    }
    if kubelogin_archive is not None:
        result["usr/local/bin/kubelogin"] = kubelogin_hash(root, kubelogin_archive)
    return result


def expected_files(root: Path, component: str) -> dict[str, Path]:
    require(component in {"api", "provisioner"}, "invalid_image_component")
    result: dict[str, Path] = {}
    for image in ("api", "provisioner") if component == "provisioner" else ("api",):
        text = (root / f"images/{image}/Dockerfile").read_text()
        for line in text.replace("\\\n", "").splitlines():
            if not line.startswith("COPY "):
                continue
            arguments = shlex.split(line)[1:]
            if arguments[:1] == ["--from=uv"]:
                require(
                    arguments == ["--from=uv", "/uv", "/usr/local/bin/uv"],
                    "unsupported_image_copy",
                )
                continue
            require(
                len(arguments) >= 2 and not any(value.startswith("--") for value in arguments),
                "unsupported_image_copy",
            )
            sources, target = arguments[:-1], arguments[-1]
            if len(sources) == 1 and TRANSIENT_COPIES.get(sources[0]) == target:
                require(image == "provisioner", "unexpected_public_build_input")
                continue
            destination = PurePosixPath("/app") / target
            require(
                ".." not in destination.parts and destination.is_relative_to("/app"),
                "unsupported_image_destination",
            )
            for source in sources:
                require(
                    not source.startswith("/") and ".." not in PurePosixPath(source).parts,
                    "invalid_source_path",
                )
                matches = sorted(root.glob(source))
                require(bool(matches), "image_copy_source_missing")
                for match in matches:
                    require(not match.is_symlink(), "image_source_symlink")
                    if match.is_dir():
                        paths = sorted(path for path in match.rglob("*") if not path.is_dir())
                        for path in paths:
                            require(not path.is_symlink(), "image_source_symlink")
                            name = destination / path.relative_to(match).as_posix()
                            result[str(name).lstrip("/")] = path
                    else:
                        name = destination
                        if target.endswith("/") or len(sources) > 1 or len(matches) > 1:
                            name /= match.name
                        result[str(name).lstrip("/")] = match
    python_sources = {name for name in result if name.startswith("app/src/")}
    if component == "api":
        require(python_sources == PUBLIC_FILES, "public_source_allowlist_changed")
        require(
            all(
                name in PUBLIC_FILES
                or name in {"app/pyproject.toml", "app/uv.lock"}
                or name.startswith("app/sql/")
                for name in result
            ),
            "privileged_public_image_copy",
        )
    else:
        complete = {
            "app/" + path.relative_to(root).as_posix()
            for path in (root / "src/plane_demo").rglob("*")
            if path.is_file()
        }
        require(python_sources == complete, "private_runtime_source_missing")
    return result


def extension_hash(stream: BinaryIO) -> str:
    """Compare generated extensions by member content, not archive timestamps."""
    with tarfile.open(fileobj=stream, mode="r:gz") as outer:
        members = outer.getmembers()
        require(
            len(members) == 1 and members[0].name == "types.tgz" and members[0].isfile(),
            "invalid_image_extension",
        )
        require(members[0].size <= 20_971_520, "image_extension_too_large")
        nested = outer.extractfile(members[0])
        if nested is None:
            raise InspectionError("invalid_image_extension")
        with tarfile.open(fileobj=io.BytesIO(nested.read()), mode="r:gz") as inner:
            files = inner.getmembers()
            require(
                sorted(member.name for member in files) == ["index.json", "types.json"]
                and all(member.isfile() for member in files),
                "invalid_image_extension",
            )
            hashes = {}
            for member in files:
                content = inner.extractfile(member)
                if content is None:
                    raise InspectionError("invalid_image_extension")
                hashes[member.name] = hashlib.file_digest(content, "sha256").hexdigest()
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def content_hash(name: str, stream: BinaryIO) -> str:
    if name.startswith("app/infra/radius/types/") and name.endswith(".tgz"):
        return extension_hash(stream)
    return hashlib.file_digest(stream, "sha256").hexdigest()


def code_signature(value, depth: int = 0):
    require(depth <= 100, "bytecode_nesting_too_deep")
    if isinstance(value, types.CodeType):
        fields = (
            "co_argcount",
            "co_posonlyargcount",
            "co_kwonlyargcount",
            "co_nlocals",
            "co_stacksize",
            "co_flags",
            "co_code",
            "co_names",
            "co_varnames",
            "co_filename",
            "co_name",
            "co_qualname",
            "co_firstlineno",
            "co_linetable",
            "co_exceptiontable",
            "co_freevars",
            "co_cellvars",
        )
        return (
            "code",
            tuple(getattr(value, name) for name in fields),
            tuple(code_signature(item, depth + 1) for item in value.co_consts),
        )
    if type(value) in (tuple, frozenset):
        items = [code_signature(item, depth + 1) for item in value]
        return (
            type(value).__name__,
            tuple(sorted(items, key=repr) if type(value) is frozenset else items),
        )
    if type(value) is float:
        return ("float", struct.pack("!d", value))
    if type(value) is complex:
        return ("complex", struct.pack("!dd", value.real, value.imag))
    require(
        type(value) in (type(None), type(Ellipsis), bool, int, str, bytes),
        "invalid_bytecode_constant",
    )
    return (type(value).__name__, value)


def verify_bytecode(name: str, data: bytes, source_name: str, source: bytes) -> None:
    require(
        len(data) >= 16 and len(data) <= 20_971_520 and data[:4] == importlib.util.MAGIC_NUMBER,
        "invalid_application_bytecode",
    )
    flags = int.from_bytes(data[4:8], "little")
    require(flags in (0, 1, 3), "invalid_application_bytecode")
    if flags & 1:
        require(data[8:16] == importlib.util.source_hash(source), "bytecode_source_hash_mismatch")
    else:
        require(
            int.from_bytes(data[12:16], "little") == len(source), "bytecode_source_size_mismatch"
        )
    payload = io.BytesIO(data[16:])
    try:
        code = marshal.load(payload)
        require(
            isinstance(code, types.CodeType) and payload.tell() == len(data) - 16,
            "invalid_application_bytecode",
        )
        require(
            code.co_filename in ("/" + source_name, source_name.removeprefix("app/")),
            "unexpected_bytecode_filename",
        )
        match = re.fullmatch(r".+\.cpython-313(?:\.opt-([12]))?\.pyc", name)
        require(match is not None, "unexpected_bytecode_cache_name")
        expected = compile(
            source, code.co_filename, "exec", dont_inherit=True, optimize=int(match[1] or 0)
        )
        require(code_signature(code) == code_signature(expected), "application_bytecode_mismatch")
    except (EOFError, TypeError, SystemError, OverflowError, RecursionError, SyntaxError):
        raise InspectionError("invalid_application_bytecode") from None


def runtime_permissions(entry: list) -> int:
    mode, uid, gid = entry[1:4]
    return (mode >> (6 if uid == 10001 else 3 if gid == 10001 else 0)) & 7


def require_runtime_access(
    filesystem: dict[str, list], name: str, needed: int, *, executable: bool = False
) -> None:
    """Resolve image paths without extracting them; apply UID/GID 10001 access rules."""
    remaining = PurePosixPath(name).parts
    resolved: list[str] = []
    links = 0
    while remaining:
        part, *tail = remaining
        resolved.append(part)
        current = "/".join(resolved)
        entry = filesystem.get(current)
        require(entry is not None, "image_runtime_path_missing")
        kind = entry[0]
        if kind in {"1", "2"}:
            links += 1
            require(links <= 40, "image_runtime_link_cycle")
            target = entry[4]
            base = "/" if kind == "1" else "/" + "/".join(resolved[:-1])
            target = posixpath.normpath(posixpath.join(base, target))
            remaining = (*PurePosixPath(target).parts[1:], *tail)
            require(bool(remaining), "image_runtime_link_target_invalid")
            resolved = []
            continue
        if tail:
            require(kind == "5", "image_runtime_parent_not_directory")
            require(runtime_permissions(entry) & 1 == 1, "image_runtime_directory_not_traversable")
        else:
            require(not executable or kind in {"0", "\0", "7"}, "image_runtime_not_executable_file")
            required = needed | (1 if kind == "5" else 0)
            require(
                runtime_permissions(entry) & required == required,
                "image_runtime_path_not_accessible",
            )
        remaining = tuple(tail)


def verify_runtime_access(
    filesystem: dict[str, list], expected: dict[str, Path], tools: dict
) -> None:
    for name in expected:
        require_runtime_access(filesystem, name, 4)
    roots = (
        "app/src/",
        "app/.venv/",
        "opt/azure-cli/",
        "usr/local/lib/python3.13/",
    )
    require_runtime_access(filesystem, "app", 5)
    for name, entry in filesystem.items():
        if name.startswith(roots) or name in {root.rstrip("/") for root in roots}:
            require_runtime_access(filesystem, name, 5 if entry[0] == "5" else 4)
    for name in (*tools, "usr/local/bin/python3.13", "app/.venv/bin/python"):
        require_runtime_access(filesystem, name, 5, executable=True)


def inspect_export(
    root: Path,
    archive: Path,
    component: str,
    *,
    reference: str,
    revision: str,
    run_info: dict,
    registry_info: dict | None = None,
    queued_info: dict | None = None,
    staging: str = "",
    api_base: str = "",
    kubelogin_archive: Path | None = None,
) -> dict:
    require("@" in reference, "image_reference_must_be_digest_pinned")
    host = reference.split("/", 1)[0]
    digest = reference.rsplit("@", 1)[1]
    require(reference == f"{host}/plane-{component}@{digest}", "invalid_inspection_reference")
    fingerprint = source_fingerprint(root, component, revision, api_base)
    if queued_info is not None:
        require(registry_info is None, "ambiguous_image_provenance")
        provenance = fresh_record(
            queued_info, run_info, host, component, revision, fingerprint, staging
        )["provenance"]
        require(provenance["digest"] == digest, "fresh_run_digest_mismatch")
    else:
        require(registry_info is not None, "image_executable_provenance_required")
        provenance = verify_record(
            registry_info, run_info, host, component, revision, fingerprint, digest
        )
    expected = expected_files(root, component)
    require(
        component != "provisioner" or kubelogin_archive is not None, "kubelogin_baseline_required"
    )
    tool_hashes = expected_tools(root, kubelogin_archive) if component == "provisioner" else {}
    hashes = {}
    for name, path in expected.items():
        with path.open("rb") as stream:
            hashes[name] = content_hash(name, stream)
    observed, tools, seen, filesystem = {}, {}, set(), {}
    interpreter = False
    with tarfile.open(archive, mode="r|") as exported:
        for member in exported:
            name = PurePosixPath(member.name).as_posix()
            payload = None
            file_sha = None
            require(
                not name.startswith("/") and ".." not in PurePosixPath(name).parts,
                "unsafe_image_archive_path",
            )
            require(name not in seen, "duplicate_image_archive_path")
            seen.add(name)
            if name not in {"etc/hosts", "etc/hostname", "etc/resolv.conf"}:
                item = [
                    member.type.decode("ascii"),
                    member.mode,
                    member.uid,
                    member.gid,
                    member.linkname,
                ]
                if member.isfile():
                    stream = exported.extractfile(member)
                    if stream is None:
                        raise InspectionError("invalid_image_archive_member")
                    if (name.startswith("app/infra/radius/types/") and name.endswith(".tgz")) or (
                        name.startswith("app/")
                        and not name.startswith("app/.venv/")
                        and name.endswith(".pyc")
                    ):
                        require(member.size <= 20_971_520, "image_source_payload_too_large")
                        payload = stream.read()
                        file_sha = hashlib.sha256(payload).hexdigest()
                    else:
                        file_sha = hashlib.file_digest(stream, "sha256").hexdigest()
                    # Generated extension timestamps are not executable content.
                    item.append(
                        content_hash(name, io.BytesIO(payload))
                        if payload is not None and name.endswith(".tgz")
                        else file_sha
                    )
                filesystem[name] = item
            require(
                "/" + name not in TRANSIENT_COPIES.values(),
                "unexpected_build_input_in_image",
            )
            if component == "api":
                require(
                    not name.startswith(
                        ("app/scripts", "app/infra", "app/src/plane_demo/management/providers")
                    )
                    and name != "app/src/plane_demo/management/provisioner.py",
                    "privileged_public_image_content",
                )
            if member.isdir():
                continue
            require(
                not name.startswith(("app/.state/", "app/.azure/"))
                and not (
                    "/.azure/" in name
                    and PurePosixPath(name).name
                    in {
                        "accessTokens.json",
                        "msal_token_cache.json",
                        "msal_token_cache.bin",
                        "azureProfile.json",
                        "service_principal_entries.json",
                    }
                )
                and not any(
                    part == ".env" or part.startswith(".env.") for part in PurePosixPath(name).parts
                ),
                "image_contains_operator_state",
            )
            if name == "usr/local/bin/python3.13":
                require(
                    member.isfile()
                    and member.size > 0
                    and member.mode & 0o111 != 0
                    and member.mode & 0o6000 == 0,
                    "invalid_image_interpreter",
                )
                interpreter = True
            # Installed dependencies and the interpreter are covered by the
            # authenticated whole-image provenance and recorded filesystem hash.
            scoped = name.startswith("app/") and not name.startswith("app/.venv/")
            if scoped and "/__pycache__/" in name and name.endswith(".pyc"):
                parent, cached = name.split("/__pycache__/", 1)
                source = parent + "/" + cached.split(".", 1)[0] + ".py"
                require(source in expected and member.isfile(), "unexpected_image_bytecode")
                if payload is None:
                    raise InspectionError("invalid_application_bytecode")
                verify_bytecode(name, payload, source, expected[source].read_bytes())
                continue
            if scoped:
                require(name in expected and member.isfile(), "unexpected_image_source")
                if file_sha is None:
                    raise InspectionError("invalid_image_source")
                observed[name] = (
                    content_hash(name, io.BytesIO(payload))
                    if payload is not None and name.endswith(".tgz")
                    else file_sha
                )
            if name in tool_hashes:
                require(
                    member.isfile() and member.mode & 0o111 != 0 and member.mode & 0o6000 == 0,
                    "invalid_image_tool",
                )
                if file_sha is None:
                    raise InspectionError("invalid_image_tool")
                tools[name] = file_sha
    require(observed == hashes, "image_source_content_mismatch")
    require(interpreter and "app/.venv/bin/python" in seen, "image_python_runtime_missing")
    verify_runtime_access(filesystem, expected, tool_hashes)
    filesystem_sha = hashlib.sha256(
        json.dumps(filesystem, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if "filesystem_sha256" in provenance:
        require(
            provenance["filesystem_sha256"] == filesystem_sha, "trusted_image_filesystem_mismatch"
        )
    if component == "provisioner":
        require(tools == tool_hashes, "image_tool_content_mismatch")
    return {
        "component": component,
        "content_verified": True,
        "source_hashes": observed,
        "tool_hashes": tools,
        "filesystem_sha256": filesystem_sha,
        "provenance": {**provenance, "filesystem_sha256": filesystem_sha},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--component", choices=["api", "provisioner"], required=True)
    parser.add_argument("--kubelogin-spec", action="store_true")
    parser.add_argument("--kubelogin-archive", type=Path)
    parser.add_argument("--reference")
    parser.add_argument("--revision")
    parser.add_argument("--run-info", type=Path)
    parser.add_argument("--registry-info", type=Path)
    parser.add_argument("--queued-info", type=Path)
    parser.add_argument("--staging", default="")
    parser.add_argument("--api-base", default="")
    args = parser.parse_args()
    try:
        if args.kubelogin_spec:
            print(json.dumps(kubelogin_spec(args.source)))
            return 0
        require(
            args.archive is not None
            and args.run_info is not None
            and args.reference is not None
            and args.revision is not None,
            "image_executable_provenance_required",
        )
        print(
            json.dumps(
                inspect_export(
                    args.source,
                    args.archive,
                    args.component,
                    reference=args.reference,
                    revision=args.revision,
                    run_info=json.loads(args.run_info.read_text()),
                    registry_info=json.loads(args.registry_info.read_text())
                    if args.registry_info
                    else None,
                    queued_info=json.loads(args.queued_info.read_text())
                    if args.queued_info
                    else None,
                    staging=args.staging,
                    api_base=args.api_base,
                    kubelogin_archive=args.kubelogin_archive,
                ),
                sort_keys=True,
            )
        )
    except (InspectionError, ProvenanceError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (OSError, tarfile.TarError, zipfile.BadZipFile, UnicodeError, ValueError, KeyError):
        print("ERROR: image_inspection_io_or_archive_error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
