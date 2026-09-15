import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def image_sources(component):
    sources = set()
    dockerfile = (ROOT / "images" / component / "Dockerfile").read_text()
    for line in dockerfile.replace("\\\n", " ").splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        for pattern in shlex.split(line)[1:-1]:
            matches = list(ROOT.glob(pattern))
            assert matches, f"Missing image input: {pattern}"
            for path in matches:
                sources.update(path.rglob("*") if path.is_dir() else [path])
    return {
        str(path.relative_to(ROOT))
        for path in sources
        if path.is_file()
        and (
            path.suffix in {".py", ".sql", ".bicep", ".yaml", ".tf", ".hcl", ".sh"}
            or path.name in {"bicepconfig.json", "registry-policy.json"}
        )
    }


def acceptance_runner():
    spec = importlib.util.spec_from_file_location(
        "layout_acceptance_runner", ROOT / "scripts/harness/test-e2e.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_image_copy_allowlists_match_the_actual_acceptance_provenance():
    runner = acceptance_runner()
    api = image_sources("api")
    assert api == set(runner.source_files("management-api"))
    assert api | image_sources("provisioner") == set(runner.source_files("provisioner"))
    assert not any("providers/" in path or "provisioner.py" in path for path in api)
    assert not any(path.startswith("scripts/") for path in api)
    assert "src/plane_demo/management/provisioning.py" not in api


def test_local_image_copy_allowlist_matches_acceptance_provenance():
    runner = acceptance_runner()
    expected = image_sources("api") | image_sources("local-provisioner")
    assert expected | {"pyproject.toml", "uv.lock"} == set(
        runner.local_source_hashes("provisioner")
    )
    assert {
        "scripts/__init__.py",
        "scripts/operations/config.py",
        "scripts/operations/demo.py",
    } <= expected
    assert {
        "scripts/recipes/local/cluster/node-address.sh",
        "scripts/recipes/local/cluster/load-images.sh",
    } <= expected


def test_operator_groups_exist_only_under_scripts():
    assert not (ROOT / "operations").exists()
    assert not (ROOT / "harness").exists()
    assert not (ROOT / "demo").exists()
    assert (ROOT / "scripts/__init__.py").is_file()
    assert {path.name for path in (ROOT / "scripts").iterdir() if path.is_dir()} - {
        "__pycache__"
    } == {"operations", "harness", "recipes", "lib"}
    assert not list((ROOT / "infra/radius/recipes").rglob("*.sh"))


def test_public_image_subset_imports_new_entrypoints_without_administrative_code(tmp_path):
    for relative in image_sources("api"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    code = """
import importlib
import importlib.util
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
for name in (
    "management.api", "control.api", "control.reconciler", "data.api",
    "data.reconciler", "setup.bootstrap", "setup.acme_responder",
):
    module = importlib.import_module("plane_demo." + name)
    assert Path(module.__file__).is_relative_to(root)
assert importlib.util.find_spec("plane_demo.management.provisioner") is None
assert importlib.util.find_spec("plane_demo.management.provisioning") is None
assert importlib.util.find_spec("plane_demo.management.providers") is None
"""
    subprocess.run(
        [sys.executable, "-I", "-c", code, str(tmp_path)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_radius_application_group_contains_only_the_three_planes():
    assert {path.name for path in (ROOT / "infra/radius/apps").iterdir()} == {
        "management.bicep",
        "control.bicep",
        "data.bicep",
    }
    assert {path.name for path in (ROOT / "infra/radius/modules").glob("*.bicep")} == {
        "challenge.bicep",
        "child-cluster.bicep",
        "database.bicep",
        "gateway.bicep",
        "workload.bicep",
    }


def test_compiled_management_worker_consumes_public_configmap_settings():
    result = subprocess.run(
        [
            str(Path.home() / ".rad/bin/bicep"),
            "build",
            str(ROOT / "infra/radius/apps/management.bicep"),
            "--stdout",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    modules = {item["name"]: item["properties"] for item in json.loads(result.stdout)["resources"]}
    worker = modules["management-provisioner"]
    assert worker["parameters"]["runtimeConfigMapName"] == {"value": "provisioning-settings"}
    assert worker["parameters"]["entrypoint"] == {"value": "plane_demo.management.provisioner"}
    assert "volumes" not in worker["parameters"] and "mounts" not in worker["parameters"]
    assert worker["template"]["parameters"]["volumes"]["defaultValue"] == []
    assert worker["template"]["parameters"]["mounts"]["defaultValue"] == []
    pod = worker["template"]["resources"]["workload"]["properties"]["properties"]["runtimes"][
        "kubernetes"
    ]["pod"]
    assert pod["containers"][0]["envFrom"] == (
        "[concat(if(empty(parameters('runtimeSecretName')), createArray(), "
        "createArray(createObject('secretRef', createObject('name', "
        "parameters('runtimeSecretName'))))), "
        "if(empty(parameters('runtimeConfigMapName')), createArray(), "
        "createArray(createObject('configMapRef', createObject('name', "
        "parameters('runtimeConfigMapName'))))))]"
    )
    assert pod["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert "runtimeConfigMapName" not in modules["management-api"]["parameters"]
    assert modules["management-api"]["template"]["parameters"]["runtimeConfigMapName"][
        "defaultValue"
    ] == ""


def test_azure_redis_recipe_keeps_the_existing_ci_security_and_connection_guards():
    source = (ROOT / "infra/radius/recipes/azure/redis.bicep").read_text()
    assert re.search(r"\btls:\s*true\b", source)
    assert "uriComponent(" in source
    assert "publicNetworkAccess: 'Disabled'" in source


def test_data_application_keeps_the_lowercase_redis_connection():
    source = (ROOT / "infra/radius/apps/data.bicep").read_text()
    assert re.search(r"^\s+redis:\s*\{", source, re.MULTILINE)


def test_old_runtime_modules_are_removed_without_compatibility_entrypoints():
    assert {path.name for path in (ROOT / "src/plane_demo").glob("*.py")} == {"__init__.py"}
    assert not (ROOT / "src/plane_demo/providers").exists()


def test_harness_api_import_and_wrapper_resolve_from_another_directory(tmp_path):
    for command in (
        [sys.executable, str(ROOT / "scripts/harness/api.py"), "--help"],
        ["bash", str(ROOT / "scripts/harness/api.sh"), "--help"],
    ):
        result = subprocess.run(
            command,
            cwd=tmp_path,
            env={**os.environ, "UV_NO_SYNC": "1"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert "{azure,local}" in result.stdout
