import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("local-clean-plan", ["clean-plan"]),
        ("local-clean", ["clean"]),
        ("local-verify", ["verify-clean"]),
        ("clean-plan", ["clean-plan"]),
        ("clean-azure", ["clean"]),
        ("verify-clean", ["verify-clean"]),
    ],
)
def test_normal_cleanup_make_targets_need_no_saved_record(tmp_path, target, expected):
    recorder = tmp_path / "record.py"
    recorder.write_text("import json,sys; print(json.dumps(sys.argv[1:]))\n")
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-f",
            str(ROOT / "Makefile"),
            target,
            f"STAGE={sys.executable} {recorder}",
            "ENV=azure",
            "CONFIRM_AZURE=yes",
            "CONFIRM_LOCAL=yes",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
    assert not (tmp_path / ".state").exists()
