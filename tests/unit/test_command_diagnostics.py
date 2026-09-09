import logging
from unittest.mock import MagicMock, patch

import pytest

from plane_demo.management.providers.commands import Commands
from plane_demo.management.provisioning import ProvisioningError


def test_failed_radius_stdout_is_reported_and_redacted(tmp_path, caplog):
    process = MagicMock()
    process.returncode = 1
    process.communicate.return_value = (
        '{"code":"DeploymentFailed","message":"bad probe","password":"private-test-value"}',
        "",
    )
    commands = Commands(tmp_path)
    commands.protect("private-test-value")
    with (
        patch("subprocess.Popen", return_value=process),
        patch("plane_demo.management.providers.commands.os.killpg"),
        caplog.at_level(logging.ERROR),
    ):
        with pytest.raises(ProvisioningError):
            commands.run(["rad", "deploy", "management.bicep"])
    assert "DeploymentFailed" in caplog.text
    assert "bad probe" in caplog.text
    assert "private-test-value" not in caplog.text
