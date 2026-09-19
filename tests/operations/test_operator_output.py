import importlib.util
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.output import progress, run_main, status  # noqa: E402

WRAPPER = ROOT / "scripts/lib/progress.sh"
SHELLS = sorted({"/bin/bash", shutil.which("bash")})


@pytest.fixture
def job_operator(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/operations"))
    spec = importlib.util.spec_from_file_location(
        "job_output_operator", ROOT / "scripts/operations/run-management-job.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("outcome", ["complete", "failed-stream", "stuck", "wait-failure"])
def test_job_log_follower_uses_scoped_access_and_is_reaped(
    job_operator, monkeypatch, tmp_path, capsys, outcome
):
    process = MagicMock()
    if outcome == "stuck":
        process.wait.side_effect = [
            subprocess.TimeoutExpired("kubectl", 5),
            subprocess.TimeoutExpired("kubectl", 5),
            -9,
        ]
    else:
        process.wait.return_value = 7 if outcome == "failed-stream" else 0
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(job_operator.subprocess, "Popen", spawn)
    signal_group = MagicMock()
    monkeypatch.setattr(job_operator.os, "killpg", signal_group)
    base = ["kubectl", "--kubeconfig", str(tmp_path / "kubeconfig"), "--context", "owned-context"]

    def wait():
        with job_operator.job_logs(base, "owned-namespace", "prepare-shared", enabled=True):
            assert spawn.call_count == 1
            if outcome == "wait-failure":
                raise ValueError("owned job failed")

    if outcome == "wait-failure":
        with pytest.raises(ValueError, match="owned job failed"):
            wait()
    else:
        wait()
    arguments = spawn.call_args.args[0]
    assert arguments[: len(base)] == base
    assert arguments[len(base) :] == [
        "--request-timeout=0",
        "-n",
        "owned-namespace",
        "logs",
        "job/prepare-shared",
        "--all-containers=true",
        "--follow",
        "--pod-running-timeout=300s",
    ]
    assert spawn.call_args.kwargs["stdout"] is sys.stderr
    assert spawn.call_args.kwargs["stderr"] is sys.stderr
    assert spawn.call_args.kwargs["start_new_session"] is True
    process.wait.assert_called()
    if outcome == "stuck":
        assert signal_group.call_args_list == [
            call(process.pid, signal.SIGTERM),
            call(process.pid, signal.SIGKILL),
        ]
    else:
        signal_group.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    if outcome == "failed-stream":
        assert "log stream exited 7" in capsys.readouterr().err


def test_completed_jobs_do_not_start_a_log_follower(job_operator, monkeypatch):
    spawn = MagicMock()
    monkeypatch.setattr(job_operator.subprocess, "Popen", spawn)
    with job_operator.job_logs([], "namespace", "deploy-management", enabled=False):
        pass
    spawn.assert_not_called()


def test_log_start_failure_is_explicit_and_does_not_hide_job_status(
    job_operator, monkeypatch, capsys
):
    monkeypatch.setattr(
        job_operator.subprocess, "Popen", MagicMock(side_effect=OSError("synthetic"))
    )
    with job_operator.job_logs([], "namespace", "prepare-shared", enabled=True):
        pass
    assert "cannot stream logs" in capsys.readouterr().err


def test_job_rollout_native_output_does_not_pollute_json_stdout(job_operator, capfd):
    assert (
        job_operator.execute(
            [sys.executable, "-c", 'print("deployment successfully rolled out")'], capture=False
        )
        == ""
    )
    output = capfd.readouterr()
    assert output.out == ""
    assert output.err == "deployment successfully rolled out\n"


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
def test_progress_is_visible_before_completion_without_polling_lines(shell):
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
                assert not selector.select(timeout=0.1), "Unexpected repeated progress"
            stdout, stderr = process.communicate(input=b"done", timeout=5)
            assert process.returncode == 0 and json.loads(stdout) == {}
            assert b"OK  Waiting for input completed" in stderr
            assert b"elapsed" not in stderr
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


def test_interruption_keeps_failure_without_reporting_completion():
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
        with progress("Synthetic failure"):
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
        ("success", "Azure: deployment sub validate completed"),
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


def test_python_progress_emits_only_phase_boundaries(capsys):
    def operation():
        with progress("Environment Job: prepare-shared"):
            print('{"status":"complete"}')
        return 0

    assert run_main(operation, "Bootstrap") == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "complete"}
    assert len(output.err.strip().splitlines()) == 3
    assert "Bootstrap completed\n" in output.err
    assert "completed (" not in output.err
    assert "elapsed" not in output.err


@pytest.mark.parametrize("shell", SHELLS)
def test_nested_shell_and_python_progress_stays_quiet_past_the_old_timer_interval(tmp_path, shell):
    program = (
        "import sys\n"
        "from scripts.operations.output import progress\n"
        "with progress('Python phase'):\n"
        "    print('prompt-ready', file=sys.stderr, flush=True)\n"
        "    sys.stdin.readline()\n"
        "print('prompt-finished', file=sys.stderr, flush=True)\n"
        "sys.stdin.readline()\n"
        "print('{}')\n"
    )
    with subprocess.Popen(
        [
            shell,
            str(WRAPPER),
            "outer",
            shell,
            str(WRAPPER),
            "inner",
            sys.executable,
            "-c",
            program,
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
        env={**os.environ, "NO_COLOR": "1", "TMPDIR": str(tmp_path)},
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stderr, selectors.EVENT_READ)
                deadline = time.monotonic() + 5
                while True:
                    assert selector.select(timeout=max(0, deadline - time.monotonic()))
                    line = process.stderr.readline()
                    assert line, "The command stopped before showing the prompt"
                    if line == b"prompt-ready\n":
                        break
                assert process.poll() is None
                assert not selector.select(timeout=16), "A timer printed while waiting for input"
                process.stdin.write(b"selected\n")
                process.stdin.flush()
                assert selector.select(timeout=3)
                assert process.stderr.readline() == b"prompt-finished\n"
            stdout, stderr = process.communicate(input=b"finish\n", timeout=5)
            assert process.returncode == 0, stderr
            assert json.loads(stdout) == {}
            assert b"elapsed" not in stderr
            assert b"outer completed" in stderr and b"inner completed" in stderr
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=5)
    assert list(tmp_path.iterdir()) == []


def test_killed_command_leaves_no_progress_files_or_false_success(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(WRAPPER),
            "Killed prompt",
            sys.executable,
            "-c",
            "import os,signal\n"
            "from scripts.operations.output import progress\n"
            "with progress('Python phase'):\n"
            "    os.kill(os.getpid(), signal.SIGKILL)\n",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env={**os.environ, "NO_COLOR": "1", "TMPDIR": str(tmp_path)},
    )
    assert result.returncode != 0 and not result.stdout
    assert list(tmp_path.iterdir()) == []
    assert "OK  Killed prompt" not in result.stderr
