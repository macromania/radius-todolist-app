import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("target", "script", "arguments"),
    [
        ("local-export", "scripts/harness/local/export-state.py", ["--once"]),
        (
            "local-test",
            "scripts/harness/test-e2e.py",
            ["--environment", "local", "--mode", "all", "--execute"],
        ),
        ("export-state", "scripts/harness/export-state.py", ["--environment", "azure", "--once"]),
        (
            "test-e2e",
            "scripts/harness/test-e2e.py",
            ["--environment", "azure", "--mode", "scenario", "--execute"],
        ),
        (
            "test-outages",
            "scripts/harness/test-e2e.py",
            ["--environment", "azure", "--mode", "outages", "--execute"],
        ),
    ],
)
def test_harness_make_targets_use_live_configuration_without_check_state(
    tmp_path, target, script, arguments
):
    recorder = tmp_path / "record.py"
    recorder.write_text("import json,sys; print(json.dumps(sys.argv[1:]))\n")
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-f",
            str(ROOT / "Makefile"),
            target,
            f"RUN={sys.executable} {recorder}",
            "ENV=azure",
            "CONFIRM_AZURE=yes",
            "CONFIRM_LOCAL=yes",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["python", script, *arguments]
    assert not (tmp_path / ".state").exists()
