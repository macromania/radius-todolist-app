"""Human-facing operator status; never writes to machine-readable stdout."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from contextlib import contextmanager

COLORS = {
    "title": "1",
    "detail": "",
    "phase": "1;34",
    "next": "",
    "section": "1",
    "progress": "",
    "success": "32",
    "warning": "33",
    "error": "31",
}


def _rule() -> str:
    try:
        width = os.get_terminal_size(sys.stderr.fileno()).columns
    except (OSError, ValueError):
        columns = os.environ.get("COLUMNS", "80")
        width = int(columns) if re.fullmatch(r"[1-9][0-9]{0,3}", columns) else 80
    return "-" * min(44, width or 80)


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
    prefix, suffix = (f"\033[{code}m", "\033[0m") if colored and code else ("", "")
    if kind == "title":
        print(f"\n{prefix}{text}{suffix}", file=sys.stderr, flush=True)
        return
    if kind == "detail":
        print(text, file=sys.stderr, flush=True)
        return
    if kind == "phase":
        print(f"\n{prefix}{text}{suffix}\n{_rule()}", file=sys.stderr, flush=True)
        return
    if kind == "next":
        print(f"  Next: {text}", file=sys.stderr, flush=True)
        return
    if kind == "section":
        _, separator, detail = text.partition(": ")
        heading = detail[:1].upper() + detail[1:] if separator else text
        print(f"\n  {prefix}{heading}{suffix}", file=sys.stderr, flush=True)
        return
    marker = {
        "progress": "",
        "success": "\u2713 " if colored else "OK  ",
        "warning": "WARNING: ",
        "error": "ERROR: ",
    }[kind]
    print(f"  {prefix}{marker}{suffix}{text}", file=sys.stderr, flush=True)


def phase(number: int, steps: Sequence[str]) -> None:
    if not 1 <= number <= len(steps):
        raise ValueError("Runbook phase is outside the workflow")
    status("phase", f"{number} / {len(steps)}  {steps[number - 1]}")
    if number < len(steps):
        status("next", steps[number])


@contextmanager
def progress(label: str):
    """Announce a phase once; native diagnostics remain visible while it runs."""
    status("progress", label)
    yield


def run_main(main, label: str, *, announce: bool = True) -> int:
    try:
        if announce:
            status("progress", label)
        result = main()
        status(
            "success" if result in (None, 0) else "error",
            f"{label} {'completed' if result in (None, 0) else 'incomplete'}",
        )
        return result
    except KeyboardInterrupt:
        status("warning", f"{label} interrupted; an external operation may still be running")
        return 130
