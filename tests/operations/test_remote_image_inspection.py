import copy
import gzip
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/operations/azure"))
import remote_inspection as remote  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "remote_inspection_test_data", Path(__file__).with_name("test_azure_image_inspection.py")
)
shared = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shared)
source = shared.source


def request_for(source, directory, component="api"):
    values = shared.inputs(source, component)
    return remote.prepare(
        source,
        component,
        values["revision"],
        values["reference"],
        values["api_base"],
        values["run_info"],
        directory,
    )


def test_task_uses_pinned_trusted_tools_and_never_starts_the_candidate(source, tmp_path):
    context = tmp_path / "context"
    request = request_for(source, context)
    task = json.loads((context / "verify.yaml").read_text())
    assert remote.validate_request(request) == request
    assert task["steps"][0]["cmd"].startswith(remote.DOCKER_IMAGE)
    assert task["steps"][1]["cmd"].startswith(remote.PYTHON_IMAGE)
    assert request["reference"] not in json.dumps(task)
    assert task["steps"][1]["env"][-1] == "ACR_RUN_ID={{.Run.ID}}"
    assert not any(step.get("privileged") for step in task["steps"])
    assert not any(step.get("ignoreErrors") for step in task["steps"])
    assert task["steps"][-1]["push"] == [f"{shared.HOST}/plane-api:verify-{request['context_id']}"]
    assert set(path.name for path in context.iterdir()) == {
        "source.tar",
        "request.json",
        "verifier",
        "verify.yaml",
        "report.Dockerfile",
    }
    assert "registry-token.json" not in json.dumps(request)


def test_context_identity_is_stable_when_only_archive_timestamps_change(source, tmp_path):
    one = request_for(source, tmp_path / "one")
    os.utime(source / "images/api/Dockerfile", (1700000000, 1700000000))
    for extension in (source / "infra/radius/types").glob("*.tgz"):
        extension.write_bytes(shared.extension_bytes(mtime=1700000000))
    two = request_for(source, tmp_path / "two")
    assert one["context_id"] == two["context_id"]
    assert one["source_archive_sha256"] != two["source_archive_sha256"]


class Docker:
    def __init__(self, request, archive):
        self.request, self.archive = request, archive
        self.commands = []
        self.container = None
        self.foreign = False

    def __call__(self, argv, **kwargs):
        self.commands.append(argv)
        assert argv[0] == "/trusted/docker"
        assert argv[1:4] == ["--host", "unix:///var/run/docker.sock", "--config"]
        args = argv[5:]
        image = "sha256:" + "d" * 64
        container = "e" * 64
        value = ""
        if args[0] == "pull":
            assert args == ["pull", "--platform", "linux/amd64", self.request["reference"]]
        elif args[:2] == ["image", "inspect"]:
            module = "api" if self.request["component"] == "api" else "provisioner"
            value = json.dumps(
                [
                    {
                        "Id": image,
                        "Architecture": "amd64",
                        "Os": "linux",
                        "RepoDigests": [self.request["reference"]],
                        "Config": {
                            "User": "10001:10001",
                            "WorkingDir": "/app",
                            "Entrypoint": None,
                            "Volumes": None,
                            "OnBuild": None,
                            "Env": ["PYTHONPATH=/app/src"],
                            "Cmd": ["python", "-m", "plane_demo.management." + module],
                            "Labels": {
                                "org.opencontainers.image.revision": self.request["revision"]
                            },
                        },
                    }
                ]
            )
        elif args[:2] == ["container", "create"]:
            assert "--read-only" in args
            assert args[args.index("--network") + 1] == "none"
            assert args[args.index("--entrypoint") + 1] == "/bin/true"
            self.container = {
                "Id": container,
                "Name": "/" + args[args.index("--name") + 1],
                "Image": image,
                "State": {"Running": False},
                "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True},
                "Mounts": [],
                "Config": {"Labels": {"plane-demo/inspection-run": self.request["context_id"]}},
            }
            value = container + "\n"
        elif args[:2] == ["container", "inspect"]:
            value = json.dumps(
                [{**self.container, **({"Image": "foreign"} if self.foreign else {})}]
            )
        elif args[:2] == ["container", "export"]:
            shutil.copyfile(self.archive, args[args.index("--output") + 1])
        elif args[:2] == ["container", "rm"]:
            self.container = None
        else:
            pytest.fail(f"Unexpected Docker operation: {args}")
        return subprocess.CompletedProcess(argv, 0, value)


@pytest.mark.parametrize("operator_state", [False, True])
@pytest.mark.parametrize("group_writable", [False, True])
def test_remote_runner_exports_without_execution_and_reports_the_rejected_path(
    source, tmp_path, monkeypatch, operator_state, group_writable
):
    if group_writable:
        (source / "images/api/Dockerfile").chmod(0o664)
        (source / "scripts/operations/init.sh").chmod(0o775)
    context = tmp_path / "context"
    request = request_for(source, context)
    path = "app/.state/operator.json"
    archive = shared.exported(
        source, "api", changes={path: b"do-not-disclose"} if operator_state else None
    )
    docker = Docker(request, archive)
    monkeypatch.setattr(remote.subprocess, "run", docker)
    monkeypatch.setattr(remote.platform, "system", lambda: "Linux")
    monkeypatch.setattr(remote.platform, "machine", lambda: "x86_64")
    monkeypatch.setenv("ACR_RUN_ID", "vr1")
    output = tmp_path / "report.json"
    if operator_state:
        with pytest.raises(remote.InspectionError, match="image_contains_operator_state") as caught:
            remote.execute(request, context / "source.tar", Path("/trusted/docker"), output)
        assert path in str(caught.value) and "do-not-disclose" not in str(caught.value)
        assert not output.exists()
    else:
        remote.execute(request, context / "source.tar", Path("/trusted/docker"), output)
        result = json.loads(output.read_text())
        assert result["content_verified"] is True
        assert result["remote_verification"]["run_id"] == "vr1"
    assert docker.container is None
    assert not any(
        word in command for command in docker.commands for word in ("run", "start", "exec")
    )


def report_artifact(source, tmp_path):
    request = request_for(source, tmp_path / "context")
    report = shared.inspect(source, shared.exported(source, "api"), "api")
    report["remote_verification"] = {
        "policy": remote.POLICY,
        "context_id": request["context_id"],
        "run_id": "vr1",
    }
    payload = json.dumps(report).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("inspection.json")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    layer = tmp_path / "layer"
    layer.write_bytes(gzip.compress(output.getvalue()))
    manifest = {
        "schemaVersion": 2,
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "digest": "sha256:" + remote.file_hash(layer),
                "size": layer.stat().st_size,
            }
        ],
    }
    run = {
        "runId": "vr1",
        "status": "Succeeded",
        "runType": "QuickRun",
        "platform": {"os": "linux", "architecture": "amd64"},
        "outputImages": [
            {
                "registry": shared.HOST,
                "repository": "plane-api",
                "tag": "verify-" + request["context_id"],
                "digest": "sha256:" + "f" * 64,
            }
        ],
    }
    return request, report, layer, manifest, run


def test_report_is_bound_to_authenticated_run_and_current_verifier(source, tmp_path):
    request, expected, layer, manifest, run = report_artifact(source, tmp_path)
    report, receipt = remote.report_from_layer(layer, manifest, run, request)
    assert report == expected
    state = remote.receipt_state({"tags": {receipt["key"]: receipt["value"]}}, request)
    assert state["state"] == "complete" and state["runId"] == "vr1"
    newer = copy.deepcopy(run)
    newer["runId"] = "vr2"
    newer["outputImages"][0]["tag"] = "verify-" + "0" * 64
    assert remote.resolve_run([newer, run], request) == run
    with pytest.raises(remote.RemoteInspectionError, match="missing_or_ambiguous"):
        remote.resolve_run([run, copy.deepcopy(run)], request)


@pytest.mark.parametrize("change", ["digest", "run-tag", "run-status", "run-id", "source"])
def test_untrusted_or_mismatched_verification_evidence_is_rejected(source, tmp_path, change):
    request, _, layer, manifest, run = report_artifact(source, tmp_path)
    if change == "digest":
        layer.write_bytes(b"changed report")
    elif change == "run-tag":
        run["outputImages"][0]["tag"] = "verify-" + "0" * 64
    elif change == "run-status":
        run["status"] = "Failed"
    elif change == "run-id":
        run["runId"] = "other"
    else:
        request["source_fingerprint"] = "0" * 64
    with pytest.raises(remote.RemoteInspectionError):
        remote.report_from_layer(layer, manifest, run, request)
