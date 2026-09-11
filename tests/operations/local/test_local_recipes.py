import base64
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from local_support import ROOT, common, load, prepare, server

RECIPES = ROOT / "infra/radius/recipes/local"
SLOTS = ("shared-control", "shared-data", "isolated-1-control", "isolated-1-data")
API_IMAGE = "localhost/radplanes-plane-api:" + "a" * 40
WORKER_IMAGE = "localhost/radplanes-plane-provisioner:" + "b" * 40


@pytest.mark.parametrize("recipe", prepare.RECIPE_FILES)
def test_every_recipe_archive_is_deterministic_and_source_only(recipe):
    content = prepare.module_archive(recipe)
    assert content == prepare.module_archive(recipe)
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        assert archive.getnames() == list(prepare.RECIPE_FILES[recipe])
        assert all(member.isfile() and member.mode == 0o644 for member in archive)
        for member in archive:
            expected = (RECIPES / recipe / member.name).read_bytes()
            assert archive.extractfile(member).read() == expected
    assert ".terraform.lock.hcl" in prepare.RECIPE_FILES[recipe]
    assert all(not name.startswith(".terraform/") for name in prepare.RECIPE_FILES[recipe])


@pytest.mark.parametrize("recipe", ["../azure", "unknown", "/etc", "redis/../../cluster"])
def test_archive_rejects_unlisted_recipe(recipe):
    with pytest.raises(common.LocalError, match="Unknown local Recipe"):
        prepare.module_archive(recipe)


def test_archive_rejects_symlinked_source_without_reading_target(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "ROOT", tmp_path)
    directory = tmp_path / "infra/radius/recipes/local/cluster"
    directory.mkdir(parents=True)
    (directory / ".terraform.lock.hcl").symlink_to(tmp_path / "private-credential")
    with pytest.raises(common.LocalError, match="symlinked"):
        prepare.module_archive()


def test_archive_rejects_symlinked_parent_without_reading_target(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "ROOT", tmp_path)
    directory = tmp_path / "infra/radius/recipes"
    directory.mkdir(parents=True)
    elsewhere = tmp_path / "not-recipe-source"
    elsewhere.mkdir()
    (directory / "local").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(common.LocalError, match="symlinked"):
        prepare.module_archive()


def test_publication_refuses_symlinked_server_code(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "ROOT", tmp_path)
    directory = tmp_path / "operations/local"
    directory.mkdir(parents=True)
    (directory / "module-server.py").symlink_to(tmp_path / "private-credential")
    with pytest.raises(common.LocalError, match="symlinked"):
        prepare.manifests(b"allowlisted-source")


def test_bundle_keeps_one_exact_immutable_archive_per_server():
    bundle = prepare.recipe_bundle()
    assert bundle["liveStatus"] == "not-run"
    assert set(bundle["modules"]) == set(prepare.RECIPE_FILES)
    assert set(bundle["recipes"]) == set(prepare.RECIPE_TYPES.values())
    assert len(bundle["objects"]) == 12
    for recipe, module in bundle["modules"].items():
        template = bundle["recipes"][module["resourceType"]]["default"]
        assert template == {"templateKind": "terraform", "templatePath": module["url"]}
        content = prepare.module_archive(recipe)
        assert module["sha256"] == common.digest(content)
        objects = [
            item for item in bundle["objects"] if item["metadata"]["name"] == module["moduleServer"]
        ]
        configmap, deployment, service = objects
        assert configmap["immutable"] is True
        assert set(configmap["binaryData"]) == {"archive.tar.gz"}
        assert set(configmap["data"]) == {"server.py"}
        assert base64.b64decode(configmap["binaryData"]["archive.tar.gz"]) == content
        assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
        spec = deployment["spec"]["template"]["spec"]
        assert spec["automountServiceAccountToken"] is False
        assert set(spec["volumes"][0]) == {"name", "configMap"}
        assert spec["containers"][0]["env"] == [
            {"name": "MODULE_SHA256", "value": module["sha256"]}
        ]
        assert module["url"].endswith("/" + module["sha256"] + ".tar.gz")
    assert len(bundle["sharedSourceHashes"]) == 15
    for name, sha in bundle["sharedSourceHashes"].items():
        assert sha == common.digest((ROOT / name).read_bytes())
    assert {
        name for name in bundle["sharedSourceHashes"] if name.startswith("infra/radius/apps/")
    } == {
        f"infra/radius/apps/{role}.bicep" for role in ("management", "control", "data")
    }


def test_recipe_bundle_cli_returns_same_source_inputs():
    result = subprocess.run(
        [sys.executable, str(ROOT / "operations/local/recipe-bundle.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == prepare.recipe_bundle()


@pytest.mark.parametrize("args", [["--execute"], ["--module", "cluster"]])
def test_recipe_bundle_cli_rejects_unsupported_execution_or_selection(args):
    result = subprocess.run(
        [sys.executable, str(ROOT / "operations/local/recipe-bundle.py"), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "unrecognized arguments" in result.stderr


@pytest.fixture
def projected_module(tmp_path):
    content = prepare.module_archive()
    objects, _, _ = prepare.manifests(content)
    configmap = objects[0]
    root = tmp_path / "module"
    payload = root / "..2026_09_11_11_04_58.123456789"
    payload.mkdir(parents=True)
    (root / "..data").symlink_to(payload.name, target_is_directory=True)
    for name, value in configmap["data"].items():
        (payload / name).write_text(value)
        (root / name).symlink_to("..data/" + name)
    for name, value in configmap["binaryData"].items():
        (payload / name).write_bytes(base64.b64decode(value))
        (root / name).symlink_to("..data/" + name)
    return root, payload, content


def test_published_server_starts_and_serves_real_configmap_projection(
    projected_module, monkeypatch
):
    root, _, content = projected_module
    published = load("projected_module_server", root / "server.py")
    sha = common.digest(content)
    monkeypatch.setenv("MODULE_SHA256", sha)
    http_server = Mock()
    monkeypatch.setattr(published.http.server, "HTTPServer", http_server)
    published.serve(root)
    http_server.return_value.serve_forever.assert_called_once_with()
    address, handler = http_server.call_args.args
    assert address == ("0.0.0.0", 18080)
    instance = object.__new__(handler)
    instance.path, instance.wfile = f"/{sha}.tar.gz", io.BytesIO()
    instance.send_response, instance.send_header, instance.end_headers = Mock(), Mock(), Mock()
    instance.do_GET()
    instance.send_response.assert_called_once_with(200)
    assert instance.wfile.getvalue() == content


@pytest.mark.parametrize("link", ["archive.tar.gz", "..data"])
def test_module_server_rejects_projection_escape(projected_module, tmp_path, link):
    root, _, content = projected_module
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "archive.tar.gz").write_bytes(content)
    (root / link).unlink()
    target = outside if link == "..data" else outside / "archive.tar.gz"
    (root / link).symlink_to(target, target_is_directory=link == "..data")
    with pytest.raises(ValueError, match="outside the module directory"):
        server.handler(root / "archive.tar.gz", common.digest(content), module_root=root)


def test_module_server_checks_projected_payload_integrity(projected_module):
    root, payload, content = projected_module
    (payload / "archive.tar.gz").write_bytes(b"changed-payload")
    with pytest.raises(ValueError, match="digest mismatch"):
        server.handler(root / "archive.tar.gz", common.digest(content), module_root=root)


def test_module_server_rejects_symlinked_module_root(projected_module, tmp_path):
    root, _, content = projected_module
    alias = tmp_path / "aliased-mount"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="directory is invalid"):
        server.handler(alias / "archive.tar.gz", common.digest(content), module_root=alias)


def test_module_server_rejects_non_file_projection(projected_module):
    root, payload, content = projected_module
    (root / "archive.tar.gz").unlink()
    (root / "archive.tar.gz").symlink_to(payload, target_is_directory=True)
    with pytest.raises(ValueError, match="not a file"):
        server.handler(root / "archive.tar.gz", common.digest(content), module_root=root)


@pytest.mark.parametrize(
    "request_path",
    ["/", "/.terraform/terraform.tfstate", "/%2e%2e/credentials", "/archive.tar.gz", "/?listing=1"],
)
def test_module_server_does_not_serve_other_files(tmp_path, request_path):
    archive = tmp_path / "archive"
    archive.write_bytes(b"source")
    handler = server.handler(archive, common.digest(b"source"), module_root=tmp_path)
    instance = object.__new__(handler)
    instance.path = request_path
    instance.send_error = Mock()
    instance.do_GET()
    instance.send_error.assert_called_once_with(404)


@pytest.fixture
def docker_double(tmp_path):
    executable = tmp_path / "docker"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['CALLS']).open('a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "mode = os.environ.get('FAIL_MODE', '')\n"
        "if mode == 'hang':\n"
        "    import time\n"
        "    time.sleep(30)\n"
        "if args[0] == 'inspect':\n"
        "    if 'kind.cluster' in args[-2]:\n"
        "        print('foreign' if mode == 'label' else os.environ['LOCAL_CLUSTER'])\n"
        "    elif 'kind.role' in args[-2]:\n"
        "        print('worker' if mode == 'role' else 'control-plane')\n"
        "    else:\n"
        "        print('172.18.0.4')\n"
        "elif args[:2] == ['image', 'save']:\n"
        "    sys.stdout.buffer.write(b'offline-image-stream')\n"
        "    sys.exit(17 if mode == 'save' else 0)\n"
        "elif 'import' in args:\n"
        "    assert sys.stdin.buffer.read() == b'offline-image-stream'\n"
        "    sys.exit(19 if mode == 'import' else 0)\n"
        "elif 'list' in args:\n"
        "    print('not-the-image' if mode == 'absent' else os.environ['LOCAL_IMAGES'])\n"
        "else:\n"
        "    raise SystemExit(99)\n"
    )
    executable.chmod(0o700)
    calls = tmp_path / "calls.jsonl"
    env = {
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "CALLS": str(calls),
        "LOCAL_CLUSTER": "radplanes-local-shared-control",
        "LOCAL_IMAGES": API_IMAGE + "\n" + WORKER_IMAGE,
    }
    return tmp_path, env, calls


def run_import(fixture):
    directory, environment, calls = fixture
    result = subprocess.run(
        ["sh", str(RECIPES / "cluster/load-images.sh")],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    commands = [json.loads(line) for line in lines]
    assert not list(directory.glob(".radplanes-image-import-*"))
    return result, commands


@pytest.mark.parametrize("slot", SLOTS)
def test_image_copy_runpath_targets_only_created_owned_child(docker_double, slot):
    _, env, _ = docker_double
    env["LOCAL_CLUSTER"] = f"radplanes-local-{slot}"
    result, commands = run_import(docker_double)
    assert result.returncode == 0, result.stderr
    node = f"radplanes-local-{slot}-control-plane"
    assert all(command[-1] == node for command in commands[:2])
    assert commands[2:4] == [
        ["image", "save", API_IMAGE],
        ["exec", "-i", node, "ctr", "--namespace", "k8s.io", "images", "import", "-"],
    ] or commands[2:4] == [
        ["exec", "-i", node, "ctr", "--namespace", "k8s.io", "images", "import", "-"],
        ["image", "save", API_IMAGE],
    ]
    assert sum(command[:2] == ["image", "save"] for command in commands) == 2
    assert sum(command[-3:] == ["images", "list", "--quiet"] for command in commands) == 2
    assert all("create" not in command and "run" not in command for command in commands)


@pytest.mark.parametrize(
    "mode,expected", [("save", "save=17"), ("import", "import=19"), ("absent", "absent")]
)
def test_image_copy_checks_both_stream_statuses_and_actual_loaded_reference(
    docker_double, mode, expected
):
    _, env, _ = docker_double
    env["FAIL_MODE"] = mode
    result, commands = run_import(docker_double)
    assert result.returncode != 0
    assert expected in result.stderr
    assert sum(command[:2] == ["image", "save"] for command in commands) == 1


@pytest.mark.parametrize("mode", ["label", "role"])
def test_image_copy_refuses_changed_child_ownership(docker_double, mode):
    _, env, _ = docker_double
    env["FAIL_MODE"] = mode
    result, commands = run_import(docker_double)
    assert result.returncode != 0
    assert "ownership" in result.stderr
    assert len(commands) == 2


def test_image_copy_deadline_bounds_hung_docker_without_timeout_or_pipefail(docker_double):
    directory, env, _ = docker_double
    sleep = directory / "sleep"
    sleep.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        "from pathlib import Path\n"
        "calls = Path(os.environ['CALLS'])\n"
        "deadline = time.monotonic() + 5\n"
        "while (not calls.exists() or not calls.stat().st_size) and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
    )
    sleep.chmod(0o700)
    env["FAIL_MODE"] = "hang"
    result, commands = run_import(docker_double)
    assert result.returncode != 0
    assert len(commands) == 1


@pytest.mark.parametrize(
    "images",
    [
        "",
        "docker.io/library/redis:latest",
        API_IMAGE + ";id",
        API_IMAGE + "\n$(id)",
        "localhost/radplanes-plane-api:latest",
        "localhost/radplanes-plane-api:" + "a" * 65,
        API_IMAGE + "\n" + WORKER_IMAGE + "\n" + API_IMAGE,
    ],
)
def test_image_copy_refuses_unapproved_images_before_docker(docker_double, images):
    _, env, _ = docker_double
    env["LOCAL_IMAGES"] = images
    result, commands = run_import(docker_double)
    assert result.returncode != 0
    assert commands == []


@pytest.mark.parametrize("cluster", ["radplanes-local-management", "foreign", "x;id"])
def test_image_copy_refuses_unowned_node_before_docker(docker_double, cluster):
    _, env, _ = docker_double
    env["LOCAL_CLUSTER"] = cluster
    result, commands = run_import(docker_double)
    assert result.returncode != 0
    assert commands == []


@pytest.mark.parametrize("slot", SLOTS)
def test_node_address_helper_accepts_all_reserved_children(docker_double, slot):
    directory, env, calls = docker_double
    env["LOCAL_CLUSTER"] = f"radplanes-local-{slot}"
    result = subprocess.run(
        ["sh", str(RECIPES / "cluster/node-address.sh"), env["LOCAL_CLUSTER"]],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"address": "172.18.0.4"}
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all(command[-1] == env["LOCAL_CLUSTER"] + "-control-plane" for command in commands)


def test_validator_checks_the_actual_archive_of_every_recipe(local_state, monkeypatch):
    validator = load("local_recipe_validator", ROOT / "operations/local/validate.py")
    monkeypatch.setattr(validator, "STATE", local_state)
    commands = Mock()
    monkeypatch.setattr(validator, "Commands", Mock(return_value=commands))
    seen = []

    def check_archive(argv, **kwargs):
        if argv[2] != "init":
            return
        directory = Path(argv[1].removeprefix("-chdir="))
        recipe = directory.name.rsplit("-", 1)[0]
        for name in prepare.RECIPE_FILES[recipe]:
            assert (directory / name).read_bytes() == (RECIPES / recipe / name).read_bytes()
        assert "-backend=false" in argv and "-lockfile=readonly" in argv
        assert (directory / "tests" / f"{recipe}.tftest.hcl").is_file()
        seen.append(recipe)

    commands.run.side_effect = check_archive
    validator.validate()
    assert seen == list(prepare.RECIPE_FILES)
    assert [call.args[0][2] for call in commands.run.call_args_list] == [
        stage for _ in prepare.RECIPE_FILES for stage in ("fmt", "init", "validate", "test")
    ]
    assert list((local_state / "validation").iterdir()) == []
