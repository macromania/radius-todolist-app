"""Argument-vector execution that stops work when the singleton session is lost."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from plane_demo.provisioning import ProvisioningError

logger = logging.getLogger(__name__)


class Commands:
    def __init__(self, root: Path, guard: Callable[[], None] = lambda: None):
        self.root = root.resolve()
        self.guard = guard
        self.environment = dict(os.environ)
        self._original_home = Path.home().resolve()
        self._bicep = (self._original_home / ".rad/bin/bicep").resolve()
        self.environment.setdefault("AZURE_CONFIG_DIR", str(self._original_home / ".azure"))
        self._secrets: set[str] = set()

    def radius_environment(self, kubeconfig: Path, context: str) -> dict[str, str]:
        self.guard()
        if not re.fullmatch(r"radplanes-[a-z0-9-]+", context):
            raise ProvisioningError("invalid_radius_context")
        state = (self.root / ".state/azure").resolve()
        kubeconfig = kubeconfig.resolve()
        if not kubeconfig.is_relative_to(state) or not kubeconfig.is_file():
            raise ProvisioningError("invalid_kubeconfig_path")
        if not self._bicep.is_file() or not os.access(self._bicep, os.X_OK):
            raise ProvisioningError("radius_compiler_missing")
        home = state / "homes" / context
        for directory in (home.parent, home, home / ".kube", home / ".rad", home / ".rad/bin"):
            if directory.is_symlink() or not directory.resolve().is_relative_to(state):
                raise ProvisioningError("invalid_radius_home")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        for relative, target in (
            (".kube/config", kubeconfig),
            (".rad/bin/bicep", self._bicep),
        ):
            link = home / relative
            if not link.exists() and not link.is_symlink():
                try:
                    link.symlink_to(target)
                except FileExistsError:
                    pass
            if not link.is_symlink() or link.resolve() != target:
                raise ProvisioningError("radius_home_link_mismatch")
        return {
            **self.environment,
            "HOME": str(home),
            "KUBECONFIG": str(kubeconfig),
            "AZURE_CONFIG_DIR": self.environment["AZURE_CONFIG_DIR"],
        }

    def protect(self, value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                self.protect(item)
        elif isinstance(value, str) and len(value) >= 8:
            self._secrets.update(
                (value, quote(value, safe=""), base64.b64encode(value.encode()).decode())
            )

    def redact(self, text: str) -> str:
        for value in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        text = re.sub(r"(?is)-----BEGIN .*?-----.*?-----END .*?-----", "[redacted]", text)
        text = re.sub(r"(?i)(?:postgres(?:ql)?|rediss?)://\S+", "[redacted-dsn]", text)
        text = re.sub(
            r"(?i)((?:password|access_token|accessToken|client_secret|authorization)"
            r"""["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|\S+)""",
            r"\1[redacted]",
            text,
        )
        text = re.sub(
            r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted-token]", text
        )
        return text

    def run(
        self,
        args: list[str],
        *,
        env: dict | None = None,
        stdin: str | None = None,
        timeout: int = 3600,
    ) -> str:
        self.guard()
        if not args or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise ProvisioningError("invalid_command")
        try:
            process = subprocess.Popen(
                args,
                cwd=self.root,
                env={**self.environment, **(env or {})},
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            logger.error("command_unavailable executable=%s errno=%s", args[0], exc.errno)
            raise ProvisioningError("command_unavailable") from None
        start = time.monotonic()
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(input=stdin, timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    stdin = None
                    self.guard()
                    if time.monotonic() - start >= timeout:
                        raise ProvisioningError("command_timeout") from None
            self.guard()
            if process.returncode:
                logger.error(
                    "command_failed executable=%s exit=%d stderr=%s",
                    args[0],
                    process.returncode,
                    self.redact(stderr)[-16384:],
                )
                raise ProvisioningError("command_failed")
            if stderr.strip():
                logger.info(
                    "command_stderr executable=%s %s", args[0], self.redact(stderr)[-16384:]
                )
            return stdout.strip()
        except BaseException:
            # Helpers spawn their own tools: stop the entire owned process group.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise

    def json(self, args: list[str], **kwargs):
        try:
            return json.loads(self.run(args, **kwargs))
        except json.JSONDecodeError:
            raise ProvisioningError("invalid_command_output") from None


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    pending = path.with_name(path.name + ".pending")
    descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    write_private(path, json.dumps(value, indent=2) + "\n")


def create_json(path: Path, value) -> None:
    """Durably create an intent without overwriting a previous attempt."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
