"""Resolve Docker Desktop before subprocesses switch to the project-scoped HOME."""

from __future__ import annotations

import json
import os
import subprocess
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit


@cache
def docker_host() -> str:
    try:
        result = subprocess.run(
            [
                "docker",
                "context",
                "inspect",
                "desktop-linux",
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            env={
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(Path.home()),
                "LC_ALL": "C",
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise OSError("Docker Desktop context is unavailable; start Docker Desktop") from None
    if result.returncode:
        raise OSError("Docker Desktop context is unavailable; start Docker Desktop")
    try:
        endpoint = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise OSError("Docker Desktop returned an invalid endpoint") from None
    if not isinstance(endpoint, str):
        raise OSError("Docker Desktop returned an invalid endpoint")
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        raise OSError("Docker Desktop returned an invalid endpoint") from None
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or ".." in Path(parsed.path).parts
        or any(character in endpoint for character in "\x00\r\n")
    ):
        raise OSError("Local deployment requires Docker Desktop's local Unix socket")
    return endpoint
