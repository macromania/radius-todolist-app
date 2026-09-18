import importlib.util
import io
import json
import marshal
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/operations/azure/image_inspection.py"
sys.path.insert(0, str(HELPER.parent))
import build_provenance as provenance  # noqa: E402

SPEC = importlib.util.spec_from_file_location("azure_image_inspection", HELPER)
inspection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspection)
FAKE_TOOLS = {
    "usr/local/bin/rad": b"synthetic rad",
    "usr/local/bin/kubectl": b"synthetic kubectl",
    "home/plane/.rad/bin/bicep": b"synthetic bicep",
    "usr/local/bin/kubelogin": b"synthetic kubelogin",
}
REVISION = "c" * 40
HOST = "acrsynthetic.azurecr.io"


def extension_bytes(mtime=0):
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz") as archive:
        for name in ("index.json", "types.json"):
            member = tarfile.TarInfo(name)
            member.size, member.mtime = 2, mtime
            archive.addfile(member, io.BytesIO(b"{}"))
    outer = io.BytesIO()
    with tarfile.open(fileobj=outer, mode="w:gz") as archive:
        member = tarfile.TarInfo("types.tgz")
        member.size, member.mtime = len(inner.getvalue()), mtime
        archive.addfile(member, io.BytesIO(inner.getvalue()))
    return outer.getvalue()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "selected"
    root.mkdir()
    for directory in ("src", "sql", "scripts", "images", "infra"):
        shutil.copytree(
            ROOT / directory,
            root / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.tgz", ".terraform", ".build", ".env*"),
        )
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / name, root / name)
    dockerfile = root / "images/provisioner/Dockerfile"
    text = dockerfile.read_text()
    for name, digest in inspection.expected_tools(root).items():
        text = text.replace(digest, inspection.hashlib.sha256(FAKE_TOOLS[name]).hexdigest())
    archive = root.parent / "kubelogin.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("bin/linux_amd64/kubelogin", FAKE_TOOLS["usr/local/bin/kubelogin"])
    text = text.replace(
        inspection.kubelogin_spec(root)["sha256"],
        inspection.hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    dockerfile.write_text(text)
    for path in (root / "infra/radius/types").glob("*.yaml"):
        path.with_suffix(".tgz").write_bytes(extension_bytes())
        path.with_suffix(".tgz").chmod(0o644)
    return root


def exported(
    source, component, *, changes=None, links=None, duplicate=False, modes=None, owners=None
):
    files = inspection.expected_files(source, component)
    content = {name: path.read_bytes() for name, path in files.items()}
    if component == "provisioner":
        content.update(FAKE_TOOLS)
    content.update(
        {
            "usr/local/bin/python3.13": b"synthetic interpreter",
            "app/.venv/bin/python": b"synthetic venv launcher",
        }
    )
    content.update(changes or {})
    archive = source.parent / "image.tar"
    with tarfile.open(archive, mode="w") as result:
        directories = {
            parent.as_posix()
            for name in {*content, *(links or {})}
            for parent in Path(name).parents
            if parent.as_posix() != "."
        }
        for name in sorted(directories, key=lambda value: (value.count("/"), value)):
            if name in (links or {}):
                continue
            original = source / name.removeprefix("app/")
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = (modes or {}).get(
                name,
                stat.S_IMODE(original.stat().st_mode)
                if (
                    name in {"app/src", "app/scripts", "app/infra", "app/sql"}
                    or name.startswith(("app/src/", "app/scripts/", "app/infra/", "app/sql/"))
                )
                and original.is_dir()
                else 0o755,
            )
            member.uid, member.gid = (owners or {}).get(name, (0, 0))
            result.addfile(member)
        for name, value in content.items():
            if value is None or name in (links or {}):
                continue
            member = tarfile.TarInfo(name)
            member.size = len(value)
            member.mode = (modes or {}).get(
                name,
                stat.S_IMODE(files[name].stat().st_mode)
                if name in files
                else 0o755
                if name in FAKE_TOOLS or name.endswith(("/python", "/python3.13"))
                else 0o644,
            )
            member.uid, member.gid = (owners or {}).get(name, (0, 0))
            result.addfile(member, io.BytesIO(value))
        for name, target in (links or {}).items():
            member = tarfile.TarInfo(name)
            member.type, member.linkname = tarfile.SYMTYPE, target
            result.addfile(member)
        if duplicate:
            member = tarfile.TarInfo("app/pyproject.toml")
            member.size = 1
            result.addfile(member, io.BytesIO(b"x"))
    return archive


def inputs(source, component):
    digest = "sha256:" + ("a" if component == "api" else "b") * 64
    staging = f"plane-{component}:build-{REVISION}-" + "1" * 32
    return {
        "reference": f"{HOST}/plane-{component}@{digest}",
        "revision": REVISION,
        "queued_info": {"runId": "ca1"},
        "run_info": {
            "runId": "ca1",
            "status": "Succeeded",
            "runType": "QuickBuild",
            "platform": {"os": "linux", "architecture": "amd64"},
            "outputImages": [
                {
                    "registry": HOST,
                    "repository": f"plane-{component}",
                    "tag": staging.split(":")[1],
                    "digest": digest,
                }
            ],
        },
        "staging": staging,
        "api_base": f"{HOST}/plane-api@sha256:" + "a" * 64 if component == "provisioner" else "",
        "kubelogin_archive": source.parent / "kubelogin.zip",
    }


def inspect(source, archive, component, **overrides):
    return inspection.inspect_export(
        source, archive, component, **{**inputs(source, component), **overrides}
    )


@pytest.mark.parametrize("component", ["api", "provisioner"])
def test_actual_cli_verifies_all_selected_runtime_sources(source, component):
    archive = exported(source, component)
    evidence = inputs(source, component)
    run_file, queued_file = source.parent / "run.json", source.parent / "queued.json"
    run_file.write_text(json.dumps(evidence["run_info"]))
    queued_file.write_text(json.dumps(evidence["queued_info"]))
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--source",
            str(source),
            "--archive",
            str(archive),
            "--component",
            component,
            "--reference",
            evidence["reference"],
            "--revision",
            REVISION,
            "--run-info",
            str(run_file),
            "--queued-info",
            str(queued_file),
            "--staging",
            evidence["staging"],
            "--api-base",
            evidence["api_base"],
            "--kubelogin-archive",
            str(source.parent / "kubelogin.zip"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["content_verified"] is True
    assert proof["component"] == component
    assert set(proof["source_hashes"]) == set(inspection.expected_files(source, component))
    assert set(proof["tool_hashes"]) == (set(FAKE_TOOLS) if component == "provisioner" else set())


@pytest.mark.parametrize("component", ["api", "provisioner"])
@pytest.mark.parametrize("replacement", [None, b"changed source with a synthetic secret"])
def test_matching_labels_cannot_replace_missing_or_changed_source(source, component, replacement):
    archive = exported(
        source, component, changes={"app/src/plane_demo/management/api.py": replacement}
    )
    with pytest.raises(inspection.InspectionError, match="image_source_content_mismatch") as caught:
        inspect(source, archive, component)
    assert "synthetic secret" not in str(caught.value)


@pytest.mark.parametrize(
    "name",
    [
        "app/src/plane_demo/management/providers/secret_store.py",
        "app/scripts/hidden.sh",
        "app/infra/hidden.json",
        "app/src/plane_demo/management/provisioner.py",
        "app/src/plane_demo/data/unapproved.so",
        "app/plane_demo/__init__.py",
    ],
)
def test_public_image_rejects_every_unapproved_source_surface(source, name):
    archive = exported(source, "api", changes={name: b"unexpected"})
    with pytest.raises(inspection.InspectionError):
        inspect(source, archive, "api")


@pytest.mark.parametrize(
    "name",
    [
        "app/.env",
        "app/.state/access.json",
        "home/plane/.azure/msal_token_cache.json",
        "home/plane/.azure/azureProfile.json",
    ],
)
def test_images_reject_operator_state_and_credentials(source, name):
    archive = exported(source, "provisioner", changes={name: b"synthetic secret"})
    with pytest.raises(inspection.InspectionError, match="image_contains_operator_state") as caught:
        inspect(source, archive, "provisioner")
    assert name in str(caught.value) and "synthetic secret" not in str(caught.value)


def test_provisioner_version_probe_uses_disposable_azure_configuration():
    text = (ROOT / "images/provisioner/Dockerfile").read_text().replace("\\\n", "")
    assert "AZURE_CONFIG_DIR=/tmp/plane-tool-checks/azure az version" in text
    assert "rm -rf /tmp/plane-tool-checks" in text
    assert "ENV AZURE_CONFIG_DIR" not in text


@pytest.mark.parametrize("component", ["api", "provisioner"])
def test_links_cannot_satisfy_source_content_proof(source, component):
    archive = exported(
        source, component, links={"app/src/plane_demo/management/api.py": "/untrusted/api.py"}
    )
    with pytest.raises(inspection.InspectionError, match="unexpected_image_source"):
        inspect(source, archive, component)


def test_duplicate_and_traversal_archive_members_are_rejected(source):
    with pytest.raises(inspection.InspectionError, match="duplicate_image_archive_path"):
        inspect(source, exported(source, "api", duplicate=True), "api")
    with pytest.raises(inspection.InspectionError, match="unsafe_image_archive_path"):
        inspect(source, exported(source, "api", changes={"../escaped": b"never extracted"}), "api")
    assert not (source.parent.parent / "escaped").exists()


def test_private_runtime_files_cannot_be_omitted_from_the_dockerfile(source):
    path = source / "images/provisioner/Dockerfile"
    path.write_text(
        "\n".join(
            line
            for line in path.read_text().splitlines()
            if not line.startswith("COPY src/plane_demo/management/providers")
        )
    )
    with pytest.raises(inspection.InspectionError, match="private_runtime_source_missing"):
        inspection.expected_files(source, "provisioner")


def test_public_dockerfile_cannot_expand_its_approved_source_allowlist(source):
    path = source / "images/api/Dockerfile"
    path.write_text(
        path.read_text() + "\nCOPY src/plane_demo/management/providers /app/providers\n"
    )
    with pytest.raises(inspection.InspectionError, match="privileged_public_image_copy"):
        inspection.expected_files(source, "api")


def test_generated_extension_metadata_is_not_mistaken_for_source_change(source):
    archive = exported(
        source,
        "provisioner",
        changes={
            "app/infra/radius/types/clusters.tgz": extension_bytes(mtime=1234),
        },
    )
    assert inspect(source, archive, "provisioner")["content_verified"] is True


def test_actual_pinned_administrative_tool_bytes_are_checked(source):
    archive = exported(source, "provisioner", changes={"usr/local/bin/rad": b"wrong executable"})
    with pytest.raises(inspection.InspectionError, match="image_tool_content_mismatch"):
        inspect(source, archive, "provisioner")


def test_inspection_neither_extracts_tar_members_nor_executes_image_code(source, monkeypatch):
    archive = exported(source, "api")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("executed code")))
    monkeypatch.setattr(
        tarfile.TarFile, "extractall", Mock(side_effect=AssertionError("extracted tar"))
    )
    assert inspect(source, archive, "api")["content_verified"] is True


def pyc(source_bytes, code_bytes=None):
    code = compile(
        source_bytes if code_bytes is None else code_bytes,
        "src/plane_demo/management/api.py",
        "exec",
        dont_inherit=True,
    )
    return (
        importlib.util.MAGIC_NUMBER
        + (3).to_bytes(4, "little")
        + importlib.util.source_hash(source_bytes)
        + marshal.dumps(code)
    )


@pytest.mark.parametrize("poisoned", [False, True])
def test_application_pyc_is_compared_to_source_even_with_correct_hash_header(source, poisoned):
    code = (source / "src/plane_demo/management/api.py").read_bytes()
    archive = exported(
        source,
        "api",
        changes={
            "app/src/plane_demo/management/__pycache__/api.cpython-313.pyc": pyc(
                code, b"raise RuntimeError('poison')" if poisoned else None
            ),
        },
    )
    if poisoned:
        with pytest.raises(inspection.InspectionError, match="application_bytecode_mismatch"):
            inspect(source, archive, "api")
    else:
        assert inspect(source, archive, "api")["content_verified"]


@pytest.mark.parametrize("name", ["usr/local/bin/python3.13", "app/.venv/bin/python"])
def test_reused_interpreter_and_venv_bytes_must_match_arm_owned_baseline(source, name):
    initial = inspect(source, exported(source, "api"), "api")
    data = inputs(source, "api")
    fingerprint = provenance.source_fingerprint(source, "api", REVISION)
    record = provenance.fresh_record(
        data["queued_info"], data["run_info"], HOST, "api", REVISION, fingerprint, data["staging"]
    )
    registry = {"tags": {record["key"]: record["value"] + ":" + initial["filesystem_sha256"]}}
    archive = exported(source, "api", changes={name: b"poisoned interpreter"})
    with pytest.raises(inspection.InspectionError, match="trusted_image_filesystem_mismatch"):
        inspect(source, archive, "api", queued_info=None, registry_info=registry)


@pytest.mark.parametrize("name", [*FAKE_TOOLS, "usr/local/bin/python3.13"])
def test_executables_must_exist_with_executable_modes(source, name):
    with pytest.raises(inspection.InspectionError):
        inspect(source, exported(source, "provisioner", changes={name: None}), "provisioner")
    with pytest.raises(inspection.InspectionError):
        inspect(source, exported(source, "provisioner", modes={name: 0o644}), "provisioner")


def test_kubelogin_binary_and_pinned_archive_are_both_verified(source):
    with pytest.raises(inspection.InspectionError, match="image_tool_content_mismatch"):
        inspect(
            source,
            exported(source, "provisioner", changes={"usr/local/bin/kubelogin": b"wrong login"}),
            "provisioner",
        )
    (source.parent / "kubelogin.zip").write_bytes(b"wrong archive")
    with pytest.raises(inspection.InspectionError, match="kubelogin_archive_digest_mismatch"):
        inspect(source, exported(source, "provisioner"), "provisioner")


@pytest.mark.parametrize("component", ["api", "provisioner"])
def test_image_access_uses_real_copied_source_modes_not_synthetic_world_readability(
    source, component
):
    path = source / "src/plane_demo/management/api.py"
    path.chmod(0o600)
    with pytest.raises(inspection.InspectionError, match="image_runtime_path_not_accessible"):
        inspect(source, exported(source, component), component)
    path.chmod(0o644)
    path.parent.chmod(0o700)
    with pytest.raises(inspection.InspectionError, match="image_runtime_directory_not_traversable"):
        inspect(source, exported(source, component), component)


@pytest.mark.parametrize(
    "name", ["app/.venv/bin/python", "usr/local/bin/python3.13", "usr/local/bin/kubelogin"]
)
def test_root_only_executable_modes_are_not_usable_by_runtime_identity(source, name):
    with pytest.raises(inspection.InspectionError, match="image_runtime_path_not_accessible"):
        inspect(source, exported(source, "provisioner", modes={name: 0o700}), "provisioner")


@pytest.mark.parametrize("uid,gid,mode", [(10001, 0, 0o600), (0, 10001, 0o640), (0, 0, 0o644)])
def test_runtime_access_honors_owner_group_and_other_permissions(source, uid, gid, mode):
    name = "app/src/plane_demo/management/api.py"
    archive = exported(source, "api", modes={name: mode}, owners={name: (uid, gid)})
    assert inspect(source, archive, "api")["content_verified"]


def test_runtime_symlink_resolution_checks_target_and_ancestor_access(source):
    link = {"app/.venv/bin/python": "/usr/local/bin/python3.13"}
    assert inspect(source, exported(source, "api", links=link), "api")["content_verified"]
    with pytest.raises(inspection.InspectionError, match="image_runtime_directory_not_traversable"):
        inspect(source, exported(source, "api", links=link, modes={"usr/local/bin": 0o700}), "api")
    with pytest.raises(inspection.InspectionError, match="image_runtime_link_cycle"):
        inspect(source, exported(source, "api", links={"app/.venv/bin/python": "python"}), "api")


def test_runtime_import_directories_must_be_readable_as_well_as_traversable(source):
    with pytest.raises(inspection.InspectionError, match="image_runtime_path_not_accessible"):
        inspect(source, exported(source, "api", modes={"app/src/plane_demo": 0o111}), "api")
