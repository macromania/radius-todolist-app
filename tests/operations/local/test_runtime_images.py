import hashlib
import io
import json
import tarfile
from unittest.mock import Mock

import pytest
from local_support import ROOT, common, load

runtime = load("local_runtime_images", ROOT / "operations/local/runtime-images.py")
REVISION = "a" * 40


@pytest.fixture
def images(monkeypatch, local_state):
    monkeypatch.setattr(runtime, "STATE", local_state)
    monkeypatch.setattr(runtime, "architecture", lambda _: "arm64")
    monkeypatch.setattr(runtime, "expected_hashes", lambda role: {role + ".py": "verified"})
    monkeypatch.setattr(runtime, "expected_extensions", lambda: {"clusters": {"types.json": "ok"}})
    commands = Mock()
    bad = {"dirty": False, "content": False, "uid": False, "existing": False}

    def execute(args, **_):
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return REVISION
        if args[:2] == ["git", "status"]:
            return "M src/changed.py" if bad["dirty"] else ""
        if "ls" in args:
            return "existing" if bad["existing"] else ""
        if "run" in args:
            role = args[-1]
            return json.dumps(
                {
                    "component": role,
                    "uid": 0 if bad["uid"] else 10001,
                    "source_hashes": {role + ".py": "wrong" if bad["content"] else "verified"},
                    "extension_members": {"clusters": {"types.json": "ok"}}
                    if role == "provisioner"
                    else {},
                }
            )
        return ""

    def inspect(args):
        role = "api" if "plane-api:" in args[-1] else "provisioner"
        return [
            {
                "Id": "sha256:" + ("1" if role == "api" else "2") * 64,
                "Architecture": "arm64",
                "Os": "linux",
                "Config": {
                    "User": "10001:10001",
                    "Labels": {"org.opencontainers.image.revision": REVISION},
                },
            }
        ]

    commands.run.side_effect = execute
    commands.json.side_effect = inspect

    def proof(_commands, _image_id, role, _arch):
        return {
            "source_hashes": {role + ".py": "verified"},
            "extension_members": {"clusters": {"types.json": "ok"}}
            if role == "provisioner"
            else {},
            "tool_hashes": {},
            "python_runtime_sha256": "runtime",
        }

    monkeypatch.setattr(runtime, "exported_proof", Mock(side_effect=proof))
    common.write_private(
        local_state / "runtime-images.json",
        {
            "version": 1,
            "source_revision": REVISION,
            "architecture": "arm64",
            "content_verified": False,
            **{
                role: {
                    "reference": runtime.references(REVISION)[role],
                    "image_id": "sha256:" + ("1" if role == "api" else "2") * 64,
                    **proof(commands, "", role, ""),
                }
                for role in ("api", "provisioner")
            },
        },
    )
    return commands, bad


@pytest.mark.parametrize("stage", ["build", "inspect", "load-management"])
def test_preview_does_not_construct_commands(stage, monkeypatch):
    constructor = Mock(side_effect=AssertionError("Preview cannot invoke Docker"))
    monkeypatch.setattr(runtime, "Commands", constructor)
    assert runtime.main([stage]) == 0
    constructor.assert_not_called()


def test_build_uses_native_platform_public_api_boundary_and_no_push(images, local_state):
    commands, _ = images
    result = runtime.build(commands)
    assert result["content_verified"] is False
    assert result["api"]["reference"] == f"localhost/radplanes-plane-api:{REVISION}"
    builds = [call for call in commands.run.call_args_list if "build" in call.args[0]]
    assert len(builds) == 2
    for call in builds:
        assert call.kwargs["visible"] is True
        assert "linux/arm64" in call.args[0]
        assert not any(value in call.args[0] for value in ("push", "--push", "--secret"))
    assert f"API_IMAGE={result['api']['reference']}" in builds[1].args[0]
    assert (
        json.loads((local_state / "runtime-images.json").read_text())["content_verified"] is False
    )


def test_inspection_checks_actual_contents_not_only_image_ids(images, local_state):
    commands, _ = images
    result = runtime.inspect_runtime(commands)
    assert result["content_verified"] is True
    assert result["provisioner"]["source_hashes"] == {"provisioner.py": "verified"}
    assert json.loads((local_state / "runtime-images.json").read_text()) == result
    runs = [call.args[0] for call in commands.run.call_args_list if "run" in call.args[0]]
    assert len(runs) == 2
    assert all("--network" in args and "none" in args and "--pull=never" in args for args in runs)
    assert all(not any(value in args for value in ("-v", "--volume", "--mount")) for args in runs)


@pytest.mark.parametrize("failure", ["dirty", "content", "uid"])
def test_failed_inspection_cannot_record_success(images, local_state, failure):
    commands, flags = images
    flags[failure] = True
    with pytest.raises(common.LocalError):
        runtime.inspect_runtime(commands)
    assert (
        json.loads((local_state / "runtime-images.json").read_text())["content_verified"] is False
    )


def test_build_refuses_replacing_an_existing_source_tag(images):
    commands, flags = images
    flags["existing"] = True
    with pytest.raises(common.LocalError, match="already exists"):
        runtime.build(commands)
    assert not any("build" in call.args[0] for call in commands.run.call_args_list)


def test_api_source_manifest_excludes_privileged_code():
    sources = runtime.expected_hashes("api")
    assert "src/plane_demo/data/api.py" in sources
    assert "sql/management.sql" in sources
    assert not any(
        "/providers/" in path or path.endswith("/provisioner.py") or path.startswith("operations/")
        for path in sources
    )


def test_copied_local_overlay_is_part_of_the_worker_source_manifest():
    assert "operations/local/dynamic-rp-overlay.yaml" in runtime.expected_hashes("provisioner")
    assert "operations/local/dynamic-rp-overlay.yaml" not in runtime.expected_hashes("api")


def test_inspection_requires_a_recorded_guarded_build(images, local_state):
    commands, _ = images
    (local_state / "runtime-images.json").unlink()
    with pytest.raises(FileNotFoundError):
        runtime.inspect_runtime(commands)
    assert not any("run" in call.args[0] for call in commands.run.call_args_list)


def test_inspection_refuses_a_retagged_image_before_executing_it(images, local_state):
    commands, _ = images
    manifest = json.loads((local_state / "runtime-images.json").read_text())
    manifest["api"]["image_id"] = "sha256:" + "9" * 64
    common.write_private(local_state / "runtime-images.json", manifest)
    with pytest.raises(common.LocalError, match="source identity differs"):
        runtime.inspect_runtime(commands)
    assert not any("run" in call.args[0] for call in commands.run.call_args_list)


def test_python_runtime_changes_cannot_pass_self_reported_source_hashes(images, local_state):
    commands, _ = images
    manifest = json.loads((local_state / "runtime-images.json").read_text())
    manifest["api"]["python_runtime_sha256"] = "tampered"
    common.write_private(local_state / "runtime-images.json", manifest)
    with pytest.raises(common.LocalError, match="filesystem differs"):
        runtime.inspect_runtime(commands)
    assert not any("run" in call.args[0] for call in commands.run.call_args_list)


def test_trusted_host_parser_hashes_the_image_without_running_its_interpreter(
    tmp_path, monkeypatch
):
    source = b"public source\n"
    name = "src/plane_demo/api.py"
    monkeypatch.setattr(
        runtime, "expected_hashes", lambda _: {name: hashlib.sha256(source).hexdigest()}
    )
    path = tmp_path / "rootfs.tar"
    with tarfile.open(path, "w") as archive:
        for filename, content in (
            ("app/" + name, source),
            ("usr/local/bin/python3.13", b"interpreter"),
            ("app/.venv/lib/python3.13/site-packages/package.py", b"dependency"),
        ):
            member = tarfile.TarInfo(filename)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    result = runtime.rootfs_proof(path, "api", "arm64")
    assert result["source_hashes"] == {name: hashlib.sha256(source).hexdigest()}
    assert len(result["python_runtime_sha256"]) == 64


def test_host_parser_rejects_privileged_files_even_with_an_api_label(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "expected_hashes", lambda _: {})
    path = tmp_path / "rootfs.tar"
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo("app/src/plane_demo/management/providers/trojan.py")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"code"))
    with pytest.raises(common.LocalError, match="privileged code"):
        runtime.rootfs_proof(path, "api", "arm64")


@pytest.mark.parametrize(
    "changed", ["usr/local/bin/rad", "usr/local/bin/kubectl", "home/plane/.rad/bin/bicep"]
)
def test_host_parser_rejects_replaced_administrative_tools(tmp_path, monkeypatch, changed):
    pins = {
        name: hashlib.sha256(name.encode()).hexdigest() for name in runtime.TOOL_HASHES["arm64"]
    }
    monkeypatch.setitem(runtime.TOOL_HASHES, "arm64", pins)
    monkeypatch.setattr(runtime, "expected_hashes", lambda _: {})
    monkeypatch.setattr(runtime, "expected_extensions", lambda: {})
    path = tmp_path / "rootfs.tar"
    with tarfile.open(path, "w") as archive:
        contents = {name: name.encode() for name in pins}
        contents.update({changed: b"replaced tool", "usr/local/bin/python3.13": b"interpreter"})
        for name, data in contents.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    with pytest.raises(common.LocalError, match="administrative tools"):
        runtime.rootfs_proof(path, "provisioner", "arm64")


def test_only_verified_management_receives_runtime_images(images, local_state, monkeypatch):
    from local_support import bootstrap

    commands, _ = images
    runtime.inspect_runtime(commands)
    verify = Mock()
    monkeypatch.setattr(bootstrap, "verify_management", verify)
    original = commands.run.side_effect

    def run(args, **kwargs):
        if "exec" in args:
            return "\n".join(runtime.references(REVISION).values())
        return original(args, **kwargs)

    commands.run.side_effect = run
    runtime.load_management(commands)
    loads = [call.args[0] for call in commands.run.call_args_list if call.args[0][0] == "kind"]
    assert len(loads) == 2
    assert all(
        args[:5] == ["kind", "load", "docker-image", "--name", common.MANAGEMENT] for args in loads
    )
    assert verify.call_count == 2
    assert (local_state / "runtime-images-loaded.json").exists()


def test_uninspected_images_cannot_be_loaded_into_management(images):
    commands, _ = images
    with pytest.raises(common.LocalError, match="Inspect both"):
        runtime.load_management(commands)
    assert not any(call.args[0][0] == "kind" for call in commands.run.call_args_list)
