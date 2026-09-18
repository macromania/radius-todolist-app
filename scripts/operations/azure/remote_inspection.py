"""Prepare, run, and verify trusted ACR-hosted image inspections."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from build_provenance import (
    ProvenanceError,
    proof_key,
    run_digest,
    run_properties,
    source_fingerprint,
)
from image_inspection import InspectionError, content_hash, inspect_export, kubelogin_spec

POLICY = "acr-remote-inspection-v1"
DOCKER_IMAGE = (
    "docker.io/library/docker:28.5.2-cli@sha256:"
    "cd58b396e427d1ee8cddcc3f3b7e8d8c2ba45755c5dd5b821b6e615e1ccf4586"
)
PYTHON_IMAGE = (
    "docker.io/library/python:3.13-slim-bookworm@sha256:"
    "ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e"
)
VERIFIER_FILES = ("remote_inspection.py", "image_inspection.py", "build_provenance.py")
REPORT_LIMIT = 2 * 1024 * 1024


class RemoteInspectionError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RemoteInspectionError(message)


def json_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extensions(root):
    result = {}
    for path in sorted((root / "infra/radius/types").glob("*.tgz")):
        name = path.relative_to(root).as_posix()
        with path.open("rb") as stream:
            result[name] = content_hash("app/" + name, stream)
    return result


def definition(request):
    return {
        key: value
        for key, value in request.items()
        if key
        not in {
            "context_id",
            "source_archive_sha256",
        }
    }


def validate_request(request):
    require(
        isinstance(request, dict) and request.get("policy") == POLICY,
        "invalid_remote_inspection_request",
    )
    component, revision = request.get("component"), request.get("revision")
    proof_key(component, revision)
    require(
        isinstance(request.get("reference"), str)
        and re.fullmatch(
            rf"[a-z0-9]+\.azurecr\.io/plane-{component}@sha256:[a-f0-9]{{64}}",
            request["reference"],
        ),
        "remote_inspection_requires_digest_reference",
    )
    require(
        request.get("context_id") == json_hash(definition(request)),
        "remote_inspection_context_mismatch",
    )
    require(
        request.get("docker_image") == DOCKER_IMAGE and request.get("python_image") == PYTHON_IMAGE,
        "remote_inspection_toolchain_mismatch",
    )
    require(
        isinstance(request.get("verifier_files"), dict)
        and set(request["verifier_files"]) == set(VERIFIER_FILES)
        and all(
            isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
            for value in request["verifier_files"].values()
        )
        and isinstance(request.get("extensions"), dict)
        and isinstance(request.get("source_archive_sha256"), str)
        and re.fullmatch(r"[a-f0-9]{64}", request["source_archive_sha256"]),
        "invalid_remote_inspection_sources",
    )
    data = run_properties(request.get("build_run"))
    digest = run_digest(
        data, request["reference"].split("/", 1)[0], component, revision, data.get("runId")
    )
    require(request["reference"].endswith("@" + digest), "remote_candidate_run_mismatch")
    return request


def prepare(source, component, revision, reference, api_base, run_info, directory):
    proof_key(component, revision)
    host = reference.split("/", 1)[0]
    data = run_properties(run_info)
    digest = run_digest(run_info, host, component, revision, data.get("runId"))
    require(reference == f"{host}/plane-{component}@{digest}", "remote_candidate_run_mismatch")
    verifier = Path(__file__).resolve().parent
    request = {
        "policy": POLICY,
        "component": component,
        "revision": revision,
        "reference": reference,
        "api_base": api_base,
        "source_fingerprint": source_fingerprint(source, component, revision, api_base),
        "extensions": extensions(source),
        "build_run": {
            "runId": data["runId"],
            "status": data["status"],
            "runType": data["runType"],
            "platform": {"os": "linux", "architecture": "amd64"},
            "outputImages": [
                {
                    key: data["outputImages"][0][key]
                    for key in ("registry", "repository", "tag", "digest")
                }
            ],
        },
        "verifier_files": {name: file_hash(verifier / name) for name in VERIFIER_FILES},
        "docker_image": DOCKER_IMAGE,
        "python_image": PYTHON_IMAGE,
    }
    request["context_id"] = json_hash(request)
    require(not directory.exists(), "remote_inspection_context_already_exists")
    directory.mkdir(mode=0o700)
    (directory / "verifier").mkdir(mode=0o700)
    for name in VERIFIER_FILES:
        shutil.copyfile(verifier / name, directory / "verifier" / name)
        (directory / "verifier" / name).chmod(0o644)
    archive = directory / "source.tar"
    with tarfile.open(archive, "w") as stream:
        for path in sorted(source.rglob("*")):
            require(not path.is_symlink(), "remote_source_symlink")
            require(path.is_file() or path.is_dir(), "invalid_remote_source_member")
            stream.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
    request["source_archive_sha256"] = file_hash(archive)
    (directory / "request.json").write_text(json.dumps(request, sort_keys=True))
    tag = "verify-" + request["context_id"]
    report = f"{host}/plane-{component}:{tag}"
    task = {
        "version": "v1.1.0",
        "stepTimeout": 1800,
        "steps": [
            {
                "id": "tools",
                "cmd": (
                    f"{DOCKER_IMAGE} sh -c 'mkdir -p /workspace/tools && "
                    "cp /usr/local/bin/docker /workspace/tools/docker'"
                ),
            },
            {
                "id": "inspect",
                "cmd": (
                    f"{PYTHON_IMAGE} python /workspace/verifier/remote_inspection.py run "
                    "--request /workspace/request.json --source-archive /workspace/source.tar "
                    "--docker /workspace/tools/docker --output /workspace/result/inspection.json"
                ),
                "env": [
                    "PYTHONPATH=/workspace/verifier",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "DOCKER_HOST=unix:///var/run/docker.sock",
                    "ACR_RUN_ID={{.Run.ID}}",
                ],
                "when": ["tools"],
            },
            {
                "id": "report",
                "build": f"-t {report} -f report.Dockerfile result",
                "when": ["inspect"],
            },
            {"id": "publish-report", "push": [report], "when": ["report"]},
        ],
    }
    (directory / "verify.yaml").write_text(json.dumps(task, indent=2))
    (directory / "report.Dockerfile").write_text(
        "FROM scratch\nCOPY inspection.json /inspection.json\n"
    )
    return request


def receipt_key(request):
    return f"plane-demo-check-{request['component']}-{request['revision']}"


def receipt_state(registry, request):
    key = receipt_key(request)
    require(
        isinstance(registry, dict) and isinstance(registry.get("tags"), dict),
        "invalid_remote_inspection_registry",
    )
    value = registry["tags"].get(key)
    if value is None:
        return {"state": "missing", "key": key, "value": None}
    require(isinstance(value, str), "invalid_remote_inspection_receipt")
    parts = value.split(":")
    require(
        len(parts) in (2, 4)
        and parts[0] in {"pending-v1", "v1"}
        and re.fullmatch(r"[a-f0-9]{64}", parts[1]),
        "invalid_remote_inspection_receipt",
    )
    if parts[0] == "pending-v1":
        require(len(parts) == 2, "invalid_remote_inspection_receipt")
        return {
            "state": "pending" if parts[1] == request["context_id"] else "stale",
            "key": key,
            "value": value,
        }
    require(
        len(parts) == 4
        and re.fullmatch(r"[a-zA-Z0-9]{1,32}", parts[2])
        and re.fullmatch(r"[a-f0-9]{64}", parts[3]),
        "invalid_remote_inspection_receipt",
    )
    if parts[1] != request["context_id"]:
        return {"state": "stale", "key": key, "value": value}
    return {
        "state": "complete",
        "key": key,
        "value": value,
        "runId": parts[2],
        "digest": "sha256:" + parts[3],
    }


def verification_run(run, request):
    data = run_properties(run)
    require(
        isinstance(data.get("runId"), str)
        and re.fullmatch(r"[a-zA-Z0-9]{1,32}", data["runId"])
        and data.get("status") == "Succeeded"
        and data.get("runType") == "QuickRun"
        and isinstance(data.get("platform"), dict)
        and str(data["platform"].get("os", "")).lower() == "linux"
        and str(data["platform"].get("architecture", "")).lower() == "amd64",
        "untrusted_remote_inspection_run",
    )
    images = data.get("outputImages")
    require(
        isinstance(images, list) and len(images) == 1 and isinstance(images[0], dict),
        "unexpected_remote_inspection_outputs",
    )
    image = images[0]
    require(
        image.get("registry") == request["reference"].split("/", 1)[0]
        and image.get("repository") == f"plane-{request['component']}"
        and image.get("tag") == "verify-" + request["context_id"]
        and isinstance(image.get("digest"), str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", image["digest"]),
        "remote_inspection_output_mismatch",
    )
    return image["digest"]


def resolve_run(runs, request):
    require(isinstance(runs, list), "invalid_remote_inspection_runs")
    matches = []
    for run in runs:
        data = run_properties(run)
        images = data.get("outputImages")
        if images is None:
            continue
        require(
            isinstance(images, list) and all(isinstance(item, dict) for item in images),
            "invalid_remote_inspection_outputs",
        )
        if any(
            item.get("registry") == request["reference"].split("/", 1)[0]
            and item.get("repository") == f"plane-{request['component']}"
            and item.get("tag") == "verify-" + request["context_id"]
            for item in images
        ):
            matches.append(run)
    require(len(matches) == 1, "remote_inspection_run_missing_or_ambiguous")
    verification_run(matches[0], request)
    return matches[0]


def report_from_layer(path, manifest, run, request):
    validate_request(request)
    digest = verification_run(run, request)
    require(isinstance(manifest, dict), "invalid_remote_report_manifest")
    layers = manifest.get("layers")
    require(
        manifest.get("schemaVersion") == 2 and isinstance(layers, list) and len(layers) == 1,
        "invalid_remote_report_manifest",
    )
    layer = layers[0]
    require(
        isinstance(layer, dict)
        and isinstance(layer.get("size"), int)
        and 0 < layer["size"] <= REPORT_LIMIT
        and path.stat().st_size == layer["size"]
        and layer.get("digest") == "sha256:" + file_hash(path),
        "remote_report_layer_mismatch",
    )
    with path.open("rb") as stream:
        if layer.get("mediaType") in {
            "application/vnd.docker.image.rootfs.diff.tar.gzip",
            "application/vnd.oci.image.layer.v1.tar+gzip",
        }:
            with gzip.GzipFile(fileobj=stream) as unpacked:
                payload = unpacked.read(REPORT_LIMIT + 1)
        else:
            require(
                layer.get("mediaType") == "application/vnd.oci.image.layer.v1.tar",
                "unsupported_remote_report_layer",
            )
            payload = stream.read(REPORT_LIMIT + 1)
    require(len(payload) <= REPORT_LIMIT, "remote_report_too_large")
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        members = archive.getmembers()
        require(
            len(members) == 1
            and members[0].name.removeprefix("./") == "inspection.json"
            and members[0].isfile()
            and 0 < members[0].size <= REPORT_LIMIT,
            "invalid_remote_report_archive",
        )
        stream = archive.extractfile(members[0])
        require(stream is not None, "missing_remote_report")
        report = json.load(stream)
    provenance = report.get("provenance") if isinstance(report, dict) else None
    require(
        isinstance(report, dict)
        and report.get("component") == request["component"]
        and report.get("content_verified") is True
        and report.get("remote_verification")
        == {
            "policy": POLICY,
            "context_id": request["context_id"],
            "run_id": run_properties(run)["runId"],
        }
        and isinstance(report.get("filesystem_sha256"), str)
        and re.fullmatch(r"[a-f0-9]{64}", report["filesystem_sha256"])
        and isinstance(provenance, dict)
        and provenance.get("digest") == request["reference"].rsplit("@", 1)[1]
        and provenance.get("run_id") == request["build_run"]["runId"]
        and provenance.get("filesystem_sha256") == report["filesystem_sha256"]
        and provenance.get("source_fingerprint") == request["source_fingerprint"],
        "remote_report_context_mismatch",
    )
    return report, {
        "key": receipt_key(request),
        "value": f"v1:{request['context_id']}:{run_properties(run)['runId']}:{digest[7:]}",
    }


def docker_command(docker, *args):
    return [
        str(docker),
        "--host",
        "unix:///var/run/docker.sock",
        "--config",
        os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")),
        *args,
    ]


def docker_json(docker, *args):
    result = subprocess.run(
        docker_command(docker, *args), stdout=subprocess.PIPE, text=True, check=True
    )
    return json.loads(result.stdout)


def validate_image(value, request):
    module = "plane_demo.management." + ("api" if request["component"] == "api" else "provisioner")
    require(
        isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict),
        "invalid_remote_image_metadata",
    )
    image = value[0]
    config = image.get("Config")
    require(
        image.get("Architecture") == "amd64"
        and image.get("Os") == "linux"
        and request["reference"] in (image.get("RepoDigests") or [])
        and isinstance(image.get("Id"), str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", image["Id"])
        and isinstance(config, dict)
        and config.get("User") == "10001:10001"
        and config.get("WorkingDir") == "/app"
        and config.get("Entrypoint") in (None, [])
        and config.get("Volumes") in (None, {})
        and config.get("OnBuild") in (None, [])
        and config.get("Cmd") == ["python", "-m", module]
        and [entry for entry in config.get("Env", []) if entry.startswith("PYTHONPATH=")]
        == ["PYTHONPATH=/app/src"]
        and config.get("Labels", {}).get("org.opencontainers.image.revision")
        == request["revision"],
        "remote_image_identity_or_configuration_mismatch",
    )
    return image["Id"]


def execute(request, archive, docker, output):
    validate_request(request)
    require(
        sys.version_info[:2] == (3, 13)
        and platform.system() == "Linux"
        and platform.machine() in {"x86_64", "amd64"},
        "invalid_remote_verifier_platform",
    )
    run_id = os.environ.get("ACR_RUN_ID", "")
    require(bool(re.fullmatch(r"[a-zA-Z0-9]{1,32}", run_id)), "missing_remote_verification_run")
    for name, expected in request["verifier_files"].items():
        require(
            name in VERIFIER_FILES and file_hash(Path(__file__).parent / name) == expected,
            "remote_verifier_source_mismatch",
        )
    require(
        file_hash(archive) == request["source_archive_sha256"], "remote_source_archive_mismatch"
    )
    with tempfile.TemporaryDirectory(prefix="plane-image-check-") as temporary:
        work = Path(temporary)
        source = work / "source"
        source.mkdir()

        def source_member(member, destination):
            require(
                (member.isfile() or member.isdir()) and member.mode & 0o7000 == 0,
                "invalid_remote_source_archive",
            )
            filtered = tarfile.data_filter(member, destination)
            # Git archives can contain 0664/0775 files. Preserve their fingerprinted modes.
            return filtered.replace(mode=member.mode)

        with tarfile.open(archive) as contents:
            require(
                all(item.isfile() or item.isdir() for item in contents.getmembers()),
                "invalid_remote_source_archive",
            )
            contents.extractall(source, filter=source_member)
        require(
            source_fingerprint(
                source, request["component"], request["revision"], request["api_base"]
            )
            == request["source_fingerprint"],
            "remote_source_fingerprint_mismatch",
        )
        require(extensions(source) == request["extensions"], "remote_extension_content_mismatch")
        kubelogin = None
        if request["component"] == "provisioner":
            spec = kubelogin_spec(source)
            kubelogin = work / "kubelogin.zip"
            with (
                urllib.request.urlopen(spec["url"], timeout=120) as response,
                kubelogin.open("wb") as stream,
            ):
                remaining = 104_857_600
                while chunk := response.read(min(1024 * 1024, remaining + 1)):
                    require(len(chunk) <= remaining, "kubelogin_archive_too_large")
                    stream.write(chunk)
                    remaining -= len(chunk)
        subprocess.run(
            docker_command(docker, "pull", "--platform", "linux/amd64", request["reference"]),
            check=True,
        )
        image_id = validate_image(
            docker_json(docker, "image", "inspect", request["reference"]), request
        )
        name = f"plane-inspect-{request['context_id'][:24]}-{run_id}"
        result = subprocess.run(
            docker_command(
                docker,
                "container",
                "create",
                "--pull",
                "never",
                "--platform",
                "linux/amd64",
                "--network",
                "none",
                "--read-only",
                "--name",
                name,
                "--label",
                "plane-demo/inspection-run=" + request["context_id"],
                "--entrypoint",
                "/bin/true",
                image_id,
            ),
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        container_id = result.stdout.strip()
        require(
            bool(re.fullmatch(r"[a-f0-9]{64}", container_id)), "invalid_remote_inspection_container"
        )

        def owned_container():
            value = docker_json(docker, "container", "inspect", container_id)
            require(
                isinstance(value, list)
                and len(value) == 1
                and value[0].get("Id") == container_id
                and value[0].get("Name") == "/" + name
                and value[0].get("Image") == image_id
                and value[0].get("State", {}).get("Running") is False
                and value[0].get("HostConfig", {}).get("NetworkMode") == "none"
                and value[0].get("HostConfig", {}).get("ReadonlyRootfs") is True
                and value[0].get("Mounts") == []
                and value[0].get("Config", {}).get("Labels", {}).get("plane-demo/inspection-run")
                == request["context_id"],
                "remote_inspection_container_owner_mismatch",
            )

        try:
            owned_container()
            exported = work / "image.tar"
            subprocess.run(
                docker_command(
                    docker, "container", "export", "--output", str(exported), container_id
                ),
                check=True,
            )
            result = inspect_export(
                source,
                exported,
                request["component"],
                reference=request["reference"],
                revision=request["revision"],
                run_info=request["build_run"],
                queued_info={"runId": request["build_run"]["runId"]},
                staging="plane-"
                + request["component"]
                + ":"
                + request["build_run"]["outputImages"][0]["tag"],
                api_base=request["api_base"],
                kubelogin_archive=kubelogin,
            )
            result["remote_verification"] = {
                "policy": POLICY,
                "context_id": request["context_id"],
                "run_id": run_id,
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, sort_keys=True))
        finally:
            owned_container()
            subprocess.run(docker_command(docker, "container", "rm", container_id), check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "state", "resolve", "report"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--component")
    parser.add_argument("--revision")
    parser.add_argument("--reference")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--run-info", type=Path)
    parser.add_argument("--registry-info", type=Path)
    parser.add_argument("--runs-info", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--docker", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--layer", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        required = {
            "prepare": ("source", "component", "revision", "reference", "run_info", "directory"),
            "run": ("request", "source_archive", "docker", "output"),
            "state": ("request", "registry_info"),
            "resolve": ("request", "runs_info"),
            "report": ("request", "run_info", "manifest", "layer", "receipt"),
        }
        require(
            all(getattr(args, name) is not None for name in required[args.action]),
            "missing_remote_inspection_input",
        )
        if args.action == "prepare":
            request = prepare(
                args.source,
                args.component,
                args.revision,
                args.reference,
                args.api_base,
                json.loads(args.run_info.read_text()),
                args.directory,
            )
            print(
                json.dumps(
                    {
                        "context_id": request["context_id"],
                        "key": receipt_key(request),
                        "pending_value": "pending-v1:" + request["context_id"],
                    }
                )
            )
            return 0
        request = validate_request(json.loads(args.request.read_text()))
        if args.action == "run":
            execute(request, args.source_archive, args.docker, args.output)
        elif args.action == "state":
            print(json.dumps(receipt_state(json.loads(args.registry_info.read_text()), request)))
        elif args.action == "resolve":
            print(json.dumps(resolve_run(json.loads(args.runs_info.read_text()), request)))
        else:
            report, receipt = report_from_layer(
                args.layer,
                json.loads(args.manifest.read_text()),
                json.loads(args.run_info.read_text()),
                request,
            )
            args.receipt.write_text(json.dumps(receipt))
            print(json.dumps(report, sort_keys=True))
        return 0
    except (
        RemoteInspectionError,
        InspectionError,
        ProvenanceError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.CalledProcessError,
        tarfile.TarError,
    ) as error:
        print(f"ERROR: remote image verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
