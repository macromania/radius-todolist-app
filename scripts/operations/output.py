"""Human-facing operator status; never writes to machine-readable stdout."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

COLORS = {"section": "1;34", "progress": "36", "success": "32", "warning": "33", "error": "31"}
RULE = "-" * 78


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
def progress(label: str):
    """Announce a phase once; native diagnostics remain visible while it runs."""
    status("progress", label)
    yield


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
