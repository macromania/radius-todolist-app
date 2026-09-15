import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUTES = {
    "azure": {
        "build": [("scripts/operations/azure/build.sh", [])],
        "inspect-build": [("scripts/operations/azure/build.sh", ["--inspect"])],
        "bootstrap": [("scripts/operations/azure/bootstrap.sh", [])],
        "deploy-management": [
            (
                "uv",
                [
                    "run",
                    "--no-sync",
                    "python",
                    "scripts/operations/run-management-job.py",
                    "--execute",
                ],
            )
        ],
        "preview-management": [
            ("uv", ["run", "--no-sync", "python", "scripts/operations/run-management-job.py"])
        ],
        "clean-plan": [("uv", ["run", "--no-sync", "python", "scripts/operations/clean-azure.py"])],
        "clean": [
            ("uv", ["run", "--no-sync", "python", "scripts/operations/clean-azure.py", "--execute"])
        ],
        "verify-clean": [
            ("uv", ["run", "--no-sync", "python", "scripts/operations/verify-clean.py"])
        ],
    },
    "local": {
        "build": [("scripts/operations/local/build.sh", ["build"])],
        "inspect-build": [("scripts/operations/local/build.sh", ["inspect"])],
        "bootstrap": [("scripts/operations/local/bootstrap.sh", [])],
        "setup": [("scripts/operations/local/setup.sh", ["apply"])],
        "deploy-management": [
            ("scripts/operations/local/setup.sh", ["apply"]),
            (
                "uv",
                [
                    "run",
                    "--no-sync",
                    "python",
                    "scripts/operations/local/deploy-demo.py",
                    "--execute",
                ],
            ),
        ],
        "preview-management": [
            ("uv", ["run", "--no-sync", "python", "scripts/operations/local/deploy-demo.py"])
        ],
        "clean-plan": [
            ("uv", ["run", "--no-sync", "python", "scripts/operations/local/cleanup.py"])
        ],
        "clean": [
            (
                "uv",
                ["run", "--no-sync", "python", "scripts/operations/local/cleanup.py", "--execute"],
            )
        ],
        "verify-clean": [
            (
                "uv",
                ["run", "--no-sync", "python", "scripts/operations/local/cleanup.py", "--verify"],
            )
        ],
    },
}


@pytest.fixture
def stages(tmp_path):
    for relative in ("scripts/lib/env.sh", "scripts/operations/stage.sh"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    binary = tmp_path / "bin"
    binary.mkdir()
    recorder = binary / "record.py"
    recorder.write_text(
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['STAGE_ROOT'])\n"
        "name = sys.argv[1]\n"
        "if name != 'uv': name = str(Path(name).resolve().relative_to(root.resolve()))\n"
        "value = {'name': name, 'args': sys.argv[2:], 'environment': os.environ['DEMO_ENV']}\n"
        "with (root / 'calls.jsonl').open('a') as stream: stream.write(json.dumps(value)+'\\n')\n"
        "print(json.dumps(value))\n"
        "print('visible tool diagnostic',file=sys.stderr)\n"
        "raise SystemExit(17 if os.environ.get('FAIL_STAGE') == name else 0)\n"
    )
    for environment in ROUTES.values():
        for calls in environment.values():
            for name, _ in calls:
                path = binary / name if name == "uv" else tmp_path / name
                if path.exists():
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "#!/bin/bash\n"
                    f"exec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "
                    + ('uv "$@"\n' if name == "uv" else '"$0" "$@"\n')
                )
                path.chmod(0o700)
    return tmp_path


def run_stage(root, environment, stage, *arguments, **overrides):
    values = {"DEMO_ENV": environment, "DEMO_PROJECT": "demo", "DEMO_DEPLOYMENT": "team"}
    if environment == "azure":
        values.update(
            AZURE_SUBSCRIPTION_ID="11111111-1111-1111-1111-111111111111",
            AZURE_LOCATION="centralus",
        )
    (root / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    (root / ".env").chmod(0o600)
    return subprocess.run(
        ["bash", str(root / "scripts/operations/stage.sh"), stage, *arguments],
        cwd=root,
        env={
            **os.environ,
            "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
            "STAGE_ROOT": str(root),
            "DEMO_ENV": "wrong-inherited-value",
            "CONFIRM_AZURE": "yes",
            "CONFIRM_LOCAL": "yes",
            "PLANE_DEMO_EXPECT_ENV": "",
            **overrides,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("environment", "stage"),
    [(environment, stage) for environment, routes in ROUTES.items() for stage in routes],
)
def test_stages_use_selected_environment_and_only_canonical_entrypoints(stages, environment, stage):
    result = run_stage(stages, environment, stage)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in (stages / "calls.jsonl").read_text().splitlines()]
    assert [(value["name"], value["args"]) for value in records] == ROUTES[environment][stage]
    assert all(value["environment"] == environment for value in records)
    assert result.stderr.count("visible tool diagnostic") == len(records)
    assert json.loads(result.stdout) == records[-1]
    assert not (stages / ".state").exists()


@pytest.mark.parametrize("environment", ["azure", "local"])
@pytest.mark.parametrize("stage", ["build", "bootstrap", "deploy-management", "clean", "fault"])
def test_mutating_stages_require_confirmation_for_the_selected_environment(
    stages, environment, stage
):
    result = run_stage(stages, environment, stage, **{f"CONFIRM_{environment.upper()}": ""})
    assert result.returncode != 0
    assert f"CONFIRM_{environment.upper()}=yes" in result.stderr
    assert not (stages / "calls.jsonl").exists()


@pytest.mark.parametrize("environment", ["azure", "local"])
def test_environment_alias_mismatch_stops_before_tools(stages, environment):
    other = "local" if environment == "azure" else "azure"
    result = run_stage(stages, environment, "inspect-build", PLANE_DEMO_EXPECT_ENV=other)
    assert result.returncode != 0
    assert not (stages / "calls.jsonl").exists()


def test_local_setup_failure_never_starts_deployment(stages):
    result = run_stage(
        stages, "local", "deploy-management", FAIL_STAGE="scripts/operations/local/setup.sh"
    )
    assert result.returncode == 17
    records = (stages / "calls.jsonl").read_text().splitlines()
    assert len(records) == 1
    assert json.loads(records[0])["name"] == "scripts/operations/local/setup.sh"


@pytest.mark.parametrize("environment", ["azure", "local"])
def test_fault_arguments_are_forwarded_without_saved_inventory(stages, environment):
    arguments = ["--slot", "shared-data", "--component", "data-reconciler", "--restore"]
    result = run_stage(stages, environment, "fault", *arguments)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["args"] == [
        "run",
        "--no-sync",
        "python",
        "scripts/harness/fault-parent-link.py",
        "--execute",
        *arguments,
    ]
