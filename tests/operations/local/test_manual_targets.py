import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "target,operation,mutates",
    [
        ("local-prepare", "operations/local/prepare.py", False),
        ("local-executor-build", "operations/local/images.py build --execute", True),
        ("local-executor-inspect", "operations/local/images.py inspect --execute", True),
        ("local-bootstrap", "operations/local/bootstrap.py create --execute", True),
        ("local-install-radius", "operations/local/bootstrap.py install --execute", True),
        ("deploy-management-preview", "operations/run-management-job.py", False),
    ],
)
def test_manual_target_runs_one_explicit_stage(tmp_path, target, operation, mutates):
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
    assert operation in result.stdout
    assert result.stdout.count("uv run --no-sync python") == 1
    if mutates:
        assert "Set CONFIRM_LOCAL=yes" in result.stdout
        denied = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-f",
                str(ROOT / "Makefile"),
                target,
                "CONFIRM_LOCAL=no",
                "RUN=false",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert denied.returncode != 0
        assert "Set CONFIRM_LOCAL=yes" in denied.stderr
        assert operation not in denied.stdout
    else:
        assert "--execute" not in result.stdout
