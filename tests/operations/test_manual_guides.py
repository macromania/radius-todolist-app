import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("environment", ["AZURE", "LOCAL"])
def test_manual_guide_shell_blocks_parse_and_use_current_targets(environment):
    text = (ROOT / f"RUN_{environment}_SCENARIOS.md").read_text()
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", (ROOT / "Makefile").read_text(), re.MULTILINE))
    blocks = re.findall(r"```(?:bash|sh)\n(.*?)```", text, re.DOTALL)
    assert blocks
    for block in blocks:
        result = subprocess.run(
            ["bash", "-n"], input=block, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        for target in re.findall(r"^\s*make ([a-z][a-z0-9-]*)", block, re.MULTILINE):
            assert target in targets, target
        assert "$STATE" not in block
        assert "acceptance.json" not in block
        assert "LOCAL_CLEANUP_RECORD" not in block
        assert "--config " not in block
    assert "plane-demo-fault-$2" in text and '.data["record.json"] | fromjson' in text
    assert "shared-clusters-before.txt" in text and "shared-clusters-after.txt" in text
    assert "make verify-clean" in text
