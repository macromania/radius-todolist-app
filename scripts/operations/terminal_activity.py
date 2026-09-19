#!/usr/bin/env python3
"""Relay a Make workflow's output with one transient, quiet-wait indicator."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time

CLEAR = b"\r\x1b[2K"
FRAMES = (b"|", b"/", b"-", b"\\")


def enabled() -> bool:
    return (
        sys.stdout.isatty()
        and sys.stderr.isatty()
        and os.environ.get("TERM", "dumb") not in ("", "dumb")
        and not os.environ.get("NO_COLOR")
        and os.environ.get("COLOR", "auto") in ("auto", "always")
    )


def signal_group(process: subprocess.Popen, value: int) -> None:
    try:
        os.killpg(process.pid, value)
    except ProcessLookupError:
        pass


def run(argv: list[str]) -> int:
    environment = dict(os.environ)
    width = os.get_terminal_size(sys.stderr.fileno()).columns or 80
    # The relay owns the terminal. Child output is piped, but its status styling remains enabled.
    environment.update(COLOR="always", COLUMNS=str(width), PLANE_DEMO_ACTIVITY_WRAPPED="1")
    process = None
    interrupted = 0
    forced = False
    visible = False
    finished = False

    def clear():
        nonlocal visible
        if visible:
            visible = False
            sys.stderr.buffer.write(CLEAR)
            sys.stderr.buffer.flush()

    def interrupt(value, _):
        nonlocal interrupted, forced
        if not interrupted:
            interrupted = value
        else:
            forced = True
        if process is not None:
            signal_group(process, signal.SIGKILL if forced else value)

    previous = {value: signal.signal(value, interrupt) for value in (signal.SIGINT, signal.SIGTERM)}
    try:
        process = subprocess.Popen(
            argv,
            env=environment,
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        if interrupted:
            signal_group(process, signal.SIGKILL if forced else interrupted)
        last_output = time.monotonic()
        frame = 0
        line_start = True
        exited_at = None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, sys.stdout.buffer)
            selector.register(process.stderr, selectors.EVENT_READ, sys.stderr.buffer)
            while selector.get_map() or process.poll() is None:
                events = selector.select(timeout=0.12)
                now = time.monotonic()
                if interrupted:
                    clear()
                for key, _ in events:
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    clear()
                    key.data.write(data)
                    key.data.flush()
                    last_output = now
                    # Never draw over a partial native line, including a selection prompt.
                    line_start = data.endswith(b"\n")
                if process.poll() is not None:
                    exited_at = exited_at or now
                    if selector.get_map() and now - exited_at > 2:
                        raise RuntimeError("Command exited but its helpers kept output open")
                elif not interrupted and line_start and now - last_output >= 0.4:
                    message = b"  " + FRAMES[frame % len(FRAMES)] + b" Working"
                    sys.stderr.buffer.write(CLEAR + message[: max(1, width - 1)])
                    sys.stderr.buffer.flush()
                    visible = True
                    frame += 1
        result = process.wait()
        finished = True
        return 128 + interrupted if interrupted else (128 - result if result < 0 else result)
    finally:
        try:
            clear()
        finally:
            if process is not None:
                if not finished or interrupted:
                    signal_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    signal_group(process, signal.SIGKILL)
                    process.wait()
                finally:
                    if not finished or interrupted:
                        signal_group(process, signal.SIGKILL)
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
            for value, handler in previous.items():
                signal.signal(value, handler)


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print("  ERROR: terminal_activity.py requires a command", file=sys.stderr)
        return 2
    if not enabled():
        os.execvpe(argv[0], argv, os.environ)
    return run(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"  ERROR: Terminal command feedback failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
