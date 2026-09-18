"""Shared command handling and numeric prompts for bootstrap compute choices."""

from __future__ import annotations

import json
import re
import subprocess
import sys

from scripts.operations.output import progress, status


class SelectionError(RuntimeError):
    pass


class SelectionCancelled(Exception):
    pass


def integer(value, label):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise SelectionError(f"{label}: expected a nonnegative integer")
    return int(value)


def read_selection(count):
    # Separate selector processes must not read ahead into the next piped answer.
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    stream = getattr(stream, "raw", stream)
    while True:
        print(f"  Select 1-{count}, or q to cancel: ", end="", file=sys.stderr, flush=True)
        answer = stream.readline()
        if isinstance(answer, bytes):
            answer = answer.decode("utf-8", errors="replace")
        if not answer or answer.strip().lower() == "q":
            raise SelectionCancelled
        answer = answer.strip()
        if answer in {str(number) for number in range(1, count + 1)}:
            return int(answer) - 1
        status("warning", f"Enter a number from 1 to {count}, or q")


class Discovery:
    def __init__(self, config, *, runner=subprocess.run):
        self.config, self.runner = config, runner

    def command(self, argv, label):
        with progress(label):
            try:
                result = self.runner(
                    argv, stdout=subprocess.PIPE, text=True, check=False, timeout=180
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise SelectionError(f"{label}: command unavailable or timed out") from error
        if result.returncode:
            raise SelectionError(
                f"{label}: failed (exit {result.returncode}); see diagnostic above"
            )
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise SelectionError(f"{label}: returned invalid JSON") from error
        status("success", f"{label} completed")
        return value

    def az(self, *args):
        value = self.command(
            ["az", *args, "--subscription", self.config.subscription, "--output", "json"],
            f"Azure: {' '.join(args[:2])}",
        )
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise SelectionError("Azure compute discovery returned an invalid list")
        return value
