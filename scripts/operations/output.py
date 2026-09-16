"""Human-facing operator status; never writes to machine-readable stdout."""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

COLORS = {"section": "1;34", "progress": "36", "success": "32", "warning": "33", "error": "31"}
RULE = "-" * 78
_HEARTBEAT_LOCK = threading.RLock()
_prompt_depth = 0


def _progress_gate() -> Path | None:
    value = os.environ.get("PLANE_DEMO_PROGRESS_DIR")
    if not value:
        return None
    directory = Path(value)
    metadata = directory.lstat()
    if (
        not directory.is_absolute()
        or not directory.name.startswith("plane-progress.")
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise ValueError("Invalid progress coordination directory")
    return directory / "heartbeat"


def _check_busy_gate(gate: Path) -> None:
    try:
        metadata = gate.lstat()
    except FileNotFoundError:
        # Another heartbeat can release the gate after mkdir reports contention.
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Invalid progress coordination gate")


@contextmanager
def _heartbeat_slot(gate: Path | None):
    if not _HEARTBEAT_LOCK.acquire(blocking=False):
        yield False
        return
    acquired = False
    try:
        if gate is not None:
            try:
                gate.mkdir(mode=0o700)
            except FileExistsError:
                _check_busy_gate(gate)
                yield False
                return
            acquired = True
        yield True
    finally:
        try:
            if acquired and gate is not None:
                gate.rmdir()
        finally:
            _HEARTBEAT_LOCK.release()


@contextmanager
def pause_progress():
    """Keep the prompt and its enclosing shell/Python timers from competing for stderr."""
    global _prompt_depth
    with _HEARTBEAT_LOCK:
        gate = _progress_gate() if _prompt_depth == 0 else None
        if gate is not None:
            deadline = time.monotonic() + 5
            while True:
                try:
                    gate.mkdir(mode=0o700)
                    break
                except FileExistsError:
                    _check_busy_gate(gate)
                    if time.monotonic() >= deadline:
                        raise ValueError("Timed out pausing progress updates") from None
                    time.sleep(0.01)
        _prompt_depth += 1
        try:
            yield
        finally:
            _prompt_depth -= 1
            if gate is not None:
                gate.rmdir()


def status(kind: str, message: str) -> None:
    mode = os.environ.get("COLOR", "auto")
    if mode not in {"auto", "always", "never"}:
        raise ValueError("Invalid COLOR. Use COLOR=auto, always, or never.")
    code = COLORS[kind]
    colored = not os.environ.get("NO_COLOR") and (
        mode == "always"
        or (mode == "auto" and sys.stderr.isatty() and os.environ.get("TERM", "dumb") != "dumb")
    )
    # Resource names and diagnostics must not inject terminal control sequences.
    text = "".join(char if char.isprintable() else " " for char in message)
    prefix, suffix = (f"\033[{code}m", "\033[0m") if colored else ("", "")
    entity, separator, detail = text.partition(": ")
    if kind == "section":
        heading = f"{entity.upper()}\n{detail[:1].upper()}{detail[1:]}" if separator else text
        print(f"\n\n{prefix}{heading}\n{RULE}{suffix}\n", file=sys.stderr, flush=True)
        return
    if separator:
        text = f"{entity:<26}  {detail}"
    marker = {
        "progress": "    ",
        "success": "\u2705  " if colored else "OK  ",
        "warning": "WARNING: ",
        "error": "ERROR: ",
    }[kind]
    ending = "\n\n" if kind in {"success", "error"} else "\n"
    print(f"{prefix}  {marker}{text}{suffix}", end=ending, file=sys.stderr, flush=True)


@contextmanager
def progress(label: str, *, interval: float = 15):
    start = time.monotonic()
    stopped = threading.Event()
    gate = _progress_gate()
    failures = []

    def heartbeat():
        while not stopped.wait(interval):
            try:
                with _heartbeat_slot(gate) as available:
                    if available:
                        status("progress", f"{label}: {int(time.monotonic() - start)}s elapsed")
            except (OSError, ValueError) as error:
                failures.append(error)
                status("error", f"{label}: progress monitor failed: {error}")
                stopped.set()

    status("progress", label)
    monitor = threading.Thread(target=heartbeat, daemon=True)
    monitor.start()
    try:
        yield
    finally:
        stopped.set()
        monitor.join()
    if failures:
        raise ValueError(f"{label}: progress monitor failed") from failures[0]


def run_main(main, label: str) -> int:
    try:
        with progress(label):
            result = main()
        status(
            "success" if result in (None, 0) else "error",
            f"{label} {'completed' if result in (None, 0) else 'incomplete'}",
        )
        return result
    except KeyboardInterrupt:
        status("warning", f"{label} interrupted; an external operation may still be running")
        return 130
