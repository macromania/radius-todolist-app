import errno
import os
import pty
import selectors
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts/lib/progress.sh"
CLEAR = b"\r\x1b[2K"


class Terminal:
    def __init__(self, descriptor, process):
        self.descriptor, self.process = descriptor, process
        self.output = b""

    def read(self, duration=0.1):
        chunk = b""
        with selectors.DefaultSelector() as selector:
            selector.register(self.descriptor, selectors.EVENT_READ)
            if selector.select(timeout=duration):
                try:
                    chunk = os.read(self.descriptor, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
        self.output += chunk
        return chunk

    def until(self, text):
        deadline = time.monotonic() + 8
        while text not in self.output:
            if time.monotonic() >= deadline:
                pytest.fail(f"Missing {text!r}; terminal output: {self.output!r}")
            self.read()

    def quiet(self, duration=0.65):
        result = b""
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            result += self.read()
        return result

    def finish(self):
        deadline = time.monotonic() + 8
        while self.process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail(f"Command did not stop; terminal output: {self.output!r}")
            self.read()
        while self.read():
            pass
        return self.process.returncode


@contextmanager
def terminal(code="", *, environment=None, mode="--delegate", command=None):
    master, slave = pty.openpty()
    process = subprocess.Popen(
        command
        or [
            "bash",
            str(WRAPPER),
            *([mode] if mode else []),
            "synthetic",
            sys.executable,
            "-u",
            "-c",
            code,
        ],
        cwd=ROOT,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={
            **os.environ,
            "TERM": "xterm-256color",
            "COLOR": "auto",
            "NO_COLOR": "",
            "PLANE_DEMO_ACTIVITY_WRAPPED": "",
            **(environment or {}),
        },
    )
    os.close(slave)
    try:
        yield Terminal(master, process)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        os.close(master)


def test_real_make_entrypoint_displays_feedback_without_changing_native_json(tmp_path):
    tool = tmp_path / "tool.py"
    tool.write_text(
        "import sys\n"
        "print('native command ready', file=sys.stderr, flush=True)\n"
        "sys.stdin.readline()\n"
        'print(\'{"result":"unchanged"}\', flush=True)\n'
    )
    with terminal(
        command=[
            "make",
            "--no-print-directory",
            "-f",
            str(ROOT / "Makefile"),
            "show-config",
            f"RUN={sys.executable} {tool}",
        ]
    ) as screen:
        screen.until(b"Working")
        os.write(screen.descriptor, b"\n")
        assert screen.finish() == 0
        assert b'{"result":"unchanged"}' in screen.output
        assert screen.output.count(b"show-config completed") == 1
        assert b"Working" not in screen.output.split(b'{"result":"unchanged"}', 1)[1]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_indicator_is_transient_and_native_output_and_exit_codes_survive(exit_code):
    with terminal(
        "import sys\n"
        "print('native start', flush=True)\n"
        "sys.stdin.readline()\n"
        "print('native stdout', flush=True)\n"
        "print('native stderr', file=sys.stderr, flush=True)\n"
        f"sys.exit({exit_code})\n"
    ) as screen:
        screen.until(b"Working")
        screen.quiet(0.3)
        assert screen.output.count(b"Working") >= 2
        os.write(screen.descriptor, b"\n")
        assert screen.finish() == exit_code
        assert screen.output.count(b"native stdout") == 1
        assert screen.output.count(b"native stderr") == 1
        assert CLEAR in screen.output.split(b"native stdout", 1)[0][-len(CLEAR) :]
        assert b"Working" not in screen.output.split(b"native stdout", 1)[1]
        assert b"elapsed" not in screen.output


@pytest.mark.parametrize("prompt", ["Select an option: ", "partial native diagnostic"])
def test_no_animation_can_overwrite_a_prompt_or_partial_native_line(prompt):
    with terminal(
        "import sys\n"
        f"print({prompt!r}, end='', file=sys.stderr, flush=True)\n"
        "choice = sys.stdin.readline()\n"
        "print('\\nreceived=' + choice.strip(), file=sys.stderr, flush=True)\n"
    ) as screen:
        screen.until(prompt.encode())
        assert screen.quiet() == b""
        os.write(screen.descriptor, b"2\n")
        assert screen.finish() == 0
        assert b"received=2" in screen.output
        assert b"Working" not in screen.output


def test_parallel_discovery_has_one_owner_and_stops_before_the_real_selection_prompt():
    with terminal(
        "import os, subprocess, sys\n"
        "from concurrent.futures import ThreadPoolExecutor\n"
        "from scripts.operations.azure.compute_selection import Discovery, read_selection\n"
        "assert os.environ['PLANE_DEMO_ACTIVITY_WRAPPED'] == '1'\n"
        "def runner(argv, **kwargs):\n"
        "    return subprocess.run([sys.executable, '-u', '-c',\n"
        "        'import time; time.sleep(1); print(\"{}\")'], **kwargs)\n"
        "discovery = Discovery(None, runner=runner)\n"
        "with ThreadPoolExecutor(max_workers=3) as pool:\n"
        "    list(pool.map(lambda n: discovery.command(['synthetic'], f'lookup-{n}'), range(3)))\n"
        "print('ready to choose', file=sys.stderr, flush=True)\n"
        "choice = read_selection(3, recommended=1)\n"
        "print('selected=' + str(choice), file=sys.stderr, flush=True)\n"
    ) as screen:
        screen.until(b"Working")
        screen.until(b"q = cancel]: ")
        before = screen.output.count(b"Working")
        assert screen.quiet() == b""
        os.write(screen.descriptor, b"\n")
        assert screen.finish() == 0
        assert b"selected=1" in screen.output
        assert screen.output.count(b"Working") == before
        for number in range(3):
            assert screen.output.count(f"lookup-{number} completed".encode()) == 1


@pytest.mark.parametrize("value", [signal.SIGINT, signal.SIGTERM])
def test_interruption_clears_feedback_and_reaps_the_owned_command_group(tmp_path, value):
    pidfile = tmp_path / "pids"
    with terminal(
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()) + ' ' + str(child.pid))\n"
        "def stop(value, _):\n"
        "    child.wait(timeout=3)\n"
        "    sys.exit(128 + value)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "time.sleep(60)\n"
    ) as screen:
        screen.until(b"Working")
        screen.process.send_signal(value)
        assert screen.finish() == 128 + value
        assert CLEAR in screen.output
        for pid in map(int, pidfile.read_text().split()):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)


def test_first_interrupt_allows_existing_administrative_cleanup_to_finish():
    with terminal(
        "import signal, sys, time\n"
        "def stop(value, _):\n"
        "    print('cleanup started', file=sys.stderr, flush=True)\n"
        "    time.sleep(2.3)\n"
        "    print('cleanup complete', file=sys.stderr, flush=True)\n"
        "    sys.exit(130)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "time.sleep(60)\n"
    ) as screen:
        screen.until(b"Working")
        screen.process.send_signal(signal.SIGINT)
        assert screen.finish() == 130
        assert b"cleanup complete" in screen.output
        assert b"Working" not in screen.output.split(b"cleanup started", 1)[1]


def test_second_interrupt_stops_an_unresponsive_owned_command(tmp_path):
    pidfile = tmp_path / "pid"
    with terminal(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    ) as screen:
        screen.until(b"Working")
        screen.process.send_signal(signal.SIGINT)
        screen.quiet(0.3)
        assert screen.process.poll() is None
        screen.process.send_signal(signal.SIGINT)
        assert screen.finish() == 130
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


@pytest.mark.parametrize(
    "environment",
    [
        {"NO_COLOR": "1"},
        {"COLOR": "never"},
        {"TERM": "dumb"},
        {"TERM": ""},
        {"MAKEFLAGS": "-j2 --jobserver-auth=3,4"},
        {"MAKEFLAGS": "--jobs=2"},
    ],
)
def test_plain_terminal_modes_never_animate(environment):
    with terminal(
        "import sys; print('ready', flush=True); sys.stdin.readline(); print('finished')",
        environment=environment,
    ) as screen:
        screen.until(b"ready")
        assert screen.quiet() == b""
        os.write(screen.descriptor, b"\n")
        assert screen.finish() == 0
        assert b"\x1b" not in screen.output and b"Working" not in screen.output


def test_redirected_output_preserves_json_and_native_errors_without_control_sequences():
    result = subprocess.run(
        [
            "bash",
            str(WRAPPER),
            "--delegate",
            "synthetic",
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read()); print('native error', file=sys.stderr); "
            "sys.exit(7)",
        ],
        input='{"selected":2}',
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "COLOR": "always", "NO_COLOR": "", "TERM": "xterm-256color"},
    )
    assert result.returncode == 7
    assert result.stdout == '{"selected":2}\n'
    assert result.stderr == "native error\n"


def test_invalid_color_fails_without_restarting_the_activity_wrapper():
    with terminal(
        "raise RuntimeError('must not run')", environment={"COLOR": "invalid"}, mode=None
    ) as screen:
        assert screen.finish() != 0
        assert b"Use COLOR=auto, always, or never" in screen.output
        assert b"must not run" not in screen.output
        assert b"Working" not in screen.output
