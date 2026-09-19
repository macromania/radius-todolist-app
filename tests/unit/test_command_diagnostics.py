import io
import logging
import subprocess
import sys
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


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_native_output_is_redacted_before_completion_without_duplicate_lines(
    tmp_path, monkeypatch, channel
):
    output = io.StringIO()
    monkeypatch.setattr(sys, "stderr", output)
    released = tmp_path / "release"
    completed = tmp_path / "complete"
    secret = "synthetic-multiline\nsecret-fragment"

    def guard():
        if "native phase started" in output.getvalue() and not released.exists():
            assert not completed.exists()
            assert "synthetic-private-value" not in output.getvalue()
            assert "synthetic-multiline" not in output.getvalue()
            assert "secret-fragment" not in output.getvalue()
            assert "PRIVATE KEY" not in output.getvalue()
            released.touch()

    commands = Commands(tmp_path, guard)
    commands.protect("synthetic-private-value")
    commands.protect(secret)
    code = """
import sys, time
from pathlib import Path
stream = getattr(sys, sys.argv[1])
print("native phase started\\r", file=stream, flush=True)
print("synthetic-private-value", file=stream, flush=True)
print("synthetic-multiline\\nsecret-fragment", file=stream, flush=True)
print("-----BEGIN PRIVATE KEY-----", file=stream, flush=True)
while not Path("release").exists():
    time.sleep(.01)
print("synthetic-pem-body\\n-----END PRIVATE KEY-----", file=stream, flush=True)
print("native phase finished", end="", file=stream, flush=True)
Path("complete").touch()
"""
    result = commands.run(
        [sys.executable, "-u", "-c", code, channel], stream_output=True, timeout=5
    )
    assert released.exists() and completed.exists()
    text = output.getvalue()
    assert text.count("native phase started") == 1
    assert text.count("native phase finished") == 1
    assert text.count("[redacted key material]") == 1
    assert all(
        value not in text
        for value in (
            "synthetic-private-value",
            "synthetic-multiline",
            "secret-fragment",
            "synthetic-pem-body",
            "PRIVATE KEY",
        )
    )
    if channel == "stdout":
        assert "synthetic-private-value" in result
    else:
        assert result == ""


def test_structured_stdout_is_captured_without_being_streamed(tmp_path, capsys):
    commands = Commands(tmp_path)
    result = commands.json(
        [sys.executable, "-c", 'print(\'{"password": "synthetic-private-value"}\')']
    )
    assert result == {"password": "synthetic-private-value"}
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_streamed_failure_preserves_unterminated_native_diagnostic_once(tmp_path, capsys, caplog):
    commands = Commands(tmp_path)
    with pytest.raises(ProvisioningError, match="command_failed"):
        commands.run(
            [sys.executable, "-c", 'import sys; sys.stderr.write("native failure"); sys.exit(7)'],
            stream_output=True,
        )
    assert capsys.readouterr().err.count("native failure") == 1
    assert "exit=7" in caplog.text
    assert "native failure" not in caplog.text


@pytest.mark.parametrize("reason", ["command_timeout", "singleton_lost"])
def test_streaming_failure_reaps_its_child_and_preserves_last_diagnostic(
    tmp_path, monkeypatch, capsys, reason
):
    processes = []
    popen = subprocess.Popen
    guards = 0

    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    def guard():
        nonlocal guards
        guards += 1
        if guards > 1 and reason == "singleton_lost":
            raise ProvisioningError(reason)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    commands = Commands(tmp_path, guard)
    with pytest.raises(ProvisioningError, match=reason):
        commands.run(
            [
                sys.executable,
                "-u",
                "-c",
                'import sys, time; sys.stderr.write("last native diagnostic"); '
                "sys.stderr.flush(); time.sleep(60)",
            ],
            timeout=0 if reason == "command_timeout" else 5,
            stream_output=True,
        )
    assert len(processes) == 1 and processes[0].poll() is not None
    assert capsys.readouterr().err.count("last native diagnostic") == 1
