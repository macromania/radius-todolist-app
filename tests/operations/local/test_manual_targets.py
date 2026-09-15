import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "target,stage,environment",
    [
        ("local-build", "build", "local"),
        ("local-inspect-build", "inspect-build", "local"),
        ("local-bootstrap", "bootstrap", "local"),
        ("local-setup", "setup", "local"),
        ("local-deploy-management", "deploy-management", "local"),
        ("deploy-management-preview", "preview-management", None),
    ],
)
def test_manual_target_runs_one_explicit_stage(tmp_path, target, stage, environment):
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-n",
            "-f",
            str(ROOT / "Makefile"),
            target,
            "CONFIRM_LOCAL=yes",
            "ENV=azure",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"bash scripts/operations/stage.sh {stage}" in result.stdout
    assert result.stdout.count("bash scripts/operations/stage.sh") == 1
    if environment:
        assert f"PLANE_DEMO_EXPECT_ENV={environment}" in result.stdout
    assert ".state/" not in result.stdout


@pytest.mark.parametrize("checker_exit", [0, 7])
def test_shell_target_covers_nested_libraries_and_propagates_failures(tmp_path, checker_exit):
    paths = {
        "scripts/lib/env.sh",
        "scripts/operations/local/encryption.sh",
        "scripts/harness/nested/with space.sh",
        "scripts/recipes/local/cluster/load-images.sh",
    }
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
    (tmp_path / "scripts/lib/ignored.txt").write_text("not a shell input")
    checker = tmp_path / "shellcheck"
    checker.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "Path('calls.json').write_text(json.dumps(sys.argv[1:]))\n"
        f"raise SystemExit({checker_exit})\n"
    )
    checker.chmod(0o700)
    result = subprocess.run(
        ["make", "--no-print-directory", "-f", str(ROOT / "Makefile"), "check-shell"],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    arguments = json.loads((tmp_path / "calls.json").read_text())
    assert arguments[:2] == ["--external-sources", "--source-path=SCRIPTDIR"]
    assert set(arguments[2:]) == paths
    assert (result.returncode == 0) == (checker_exit == 0)
