import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.output import progress, run_main, status  # noqa: E402

WRAPPER = ROOT / "scripts/lib/progress.sh"
SHELLS = sorted({"/bin/bash", shutil.which("bash")})


@pytest.mark.parametrize(
    "color,no_color,colored",
    [
        ("auto", "", False),
        ("always", "", True),
        ("never", "", False),
        ("always", "1", False),
    ],
)
def test_shell_and_python_status_share_colors_without_changing_stdout(
    monkeypatch, capsys, color, no_color, colored
):
    monkeypatch.setenv("COLOR", color)
    monkeypatch.setenv("NO_COLOR", no_color)
    for kind, code in {
        "section": "1;34",
        "progress": "36",
        "success": "32",
        "warning": "33",
        "error": "31",
    }.items():
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; demo_status "$2" "message"',
                "test",
                str(ROOT / "scripts/lib/output.sh"),
                kind,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        status(kind, "message")
        output = capsys.readouterr()
        assert result.returncode == 0 and result.stdout == output.out == ""
        assert result.stderr == output.err
        assert (f"\033[{code}m" in output.err) is colored


def test_wrapper_preserves_stdin_stdout_native_diagnostics_and_exit_code():
    result = subprocess.run(
        [
            "bash",
            str(WRAPPER),
            "Synthetic command",
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read()); "
            "print('native diagnostic',file=sys.stderr); sys.exit(7)",
        ],
        input='{"synthetic":"input"}',
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
        timeout=5,
    )
    assert result.returncode == 7
    assert json.loads(result.stdout) == {"synthetic": "input"}
    assert "native diagnostic" in result.stderr and "ERROR:" in result.stderr
    assert "OK  Synthetic command" not in result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_quick_commands_do_not_leave_monitors_holding_capture_pipes(shell):
    result = subprocess.run(
        [
            shell,
            "-c",
            'set -e; source "$1"; '
            "trap 'printf \"caller cleanup\\n\" >&2' EXIT; "
            "for ((i=0; i<100; i++)); do value=$(demo_run quick printf ok); "
            '[[ "$value" == ok ]]; done',
            "test",
            str(ROOT / "scripts/lib/output.sh"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("caller cleanup") == 1


@pytest.mark.parametrize("shell", SHELLS)
def test_progress_cleanup_runs_inside_an_existing_exit_handler(tmp_path, shell):
    result = subprocess.run(
        [
            shell,
            "-c",
            'source "$1"; cleanup() { local result=$?; '
            'demo_run cleanup true; exit "$result"; }; trap cleanup EXIT; exit 8',
            "test",
            str(ROOT / "scripts/lib/output.sh"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env={**os.environ, "NO_COLOR": "1", "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 8, result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("shell", SHELLS)
def test_progress_is_visible_before_completion_and_every_fifteen_seconds(shell):
    started = time.monotonic()
    with subprocess.Popen(
        [
            shell,
            str(WRAPPER),
            "Waiting for input",
            sys.executable,
            "-c",
            "import sys; sys.stdin.read(); print('{}')",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "NO_COLOR": "1"},
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stderr, selectors.EVENT_READ)
                assert selector.select(timeout=3), "No early status before the command blocks"
                assert b"      Waiting for input\n" == process.stderr.readline()
                assert process.poll() is None
                assert selector.select(timeout=20), "No heartbeat during a quiet command"
                assert b"elapsed" in process.stderr.readline()
                assert 14 <= time.monotonic() - started < 25
            stdout, stderr = process.communicate(input=b"done", timeout=5)
            assert process.returncode == 0 and json.loads(stdout) == {}
            assert b"OK  Waiting for input completed" in stderr
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


def test_interruption_reaps_the_owned_monitor_and_keeps_failure():
    with subprocess.Popen(
        [
            "bash",
            str(WRAPPER),
            "Interrupted command",
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={**os.environ, "NO_COLOR": "1"},
    ) as process:
        assert process.stderr.readline() == b"      Interrupted command\n"
        assert process.stdout.readline() == b"ready\n"
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode != 0 and stdout == b""
        assert b"OK  Interrupted command" not in stderr


def test_python_progress_stops_on_failure_and_sanitizes_controls(capsys):
    with pytest.raises(RuntimeError):
        with progress("Synthetic failure", interval=0.01):
            time.sleep(0.02)
            raise RuntimeError("failure")
    before = capsys.readouterr()
    assert "      Synthetic failure\n" in before.err
    time.sleep(0.03)
    assert capsys.readouterr().err == ""
    status("error", "unsafe\033[2J\nvalue")
    assert "\033" not in capsys.readouterr().err
    assert run_main(lambda: 7, "failed operation") == 7
    assert "OK  failed operation" not in capsys.readouterr().err


@pytest.mark.parametrize("color", ["never", "always"])
def test_entities_sections_and_outcomes_are_visually_separate(monkeypatch, capsys, color):
    monkeypatch.setenv("COLOR", color)
    monkeypatch.setenv("NO_COLOR", "")
    for kind, message in (
        ("section", "Bootstrap: validate the foundation"),
        ("progress", "Azure: deployment sub validate"),
        ("success", "Azure: deployment sub validate completed (2s)"),
    ):
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; demo_status "$2" "$3"',
                "test",
                str(ROOT / "scripts/lib/output.sh"),
                kind,
                message,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        status(kind, message)
        assert result.stderr == capsys.readouterr().err
        assert not result.stdout
        assert f"[{kind}]" not in result.stderr
        if kind == "section":
            assert "BOOTSTRAP\nValidate the foundation\n" + "-" * 78 in result.stderr
        elif kind == "progress":
            assert f"      {'Azure':<26}  deployment sub validate" in result.stderr
            assert "\u2705" not in result.stderr
        else:
            assert ("\u2705" in result.stderr) == (color == "always")
