"""Small operator-selected configuration, separate from discovered resources."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path

from plane_demo.management.providers.identity import (
    PUBLIC_KEYS,
    SECRET_KEYS,
    ConfigError,
    DemoConfig,
)
from plane_demo.management.providers.identity import SLOTS as SLOTS
from plane_demo.management.providers.identity import Environment as Environment

ROOT = Path(__file__).resolve().parents[2]


def parse_env(text: str) -> DemoConfig:
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if not match:
            raise ConfigError(f"Invalid configuration syntax on line {number}")
        key, value = match.groups()
        if key not in PUBLIC_KEYS and key not in SECRET_KEYS:
            raise ConfigError(f"Unknown configuration key on line {number}")
        if key in values:
            raise ConfigError(f"Duplicate configuration key on line {number}")
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                raise ConfigError(f"Invalid quoted value on line {number}") from None
        elif value.startswith("'"):
            if len(value) < 2 or not value.endswith("'") or "'" in value[1:-1]:
                raise ConfigError(f"Invalid quoted value on line {number}")
            value = value[1:-1]
        if not isinstance(value, str) or "\x00" in value:
            raise ConfigError(f"Invalid configuration value on line {number}")
        values[key] = value
    return DemoConfig.from_values(values)


def load_config(path: Path = ROOT / ".env") -> DemoConfig:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ConfigError(".env must be a private regular file")
            text = stream.read(16_385)
            if len(text) > 16_384:
                raise ConfigError(".env is too large")
    except FileNotFoundError:
        raise ConfigError("Run make init ENV=azure or make init ENV=local to create .env") from None
    except OSError:
        raise ConfigError("Cannot read .env; symlinks are not supported") from None
    return parse_env(text)


def initialize_config(
    config: DemoConfig, path: Path = ROOT / ".env", *, expected: DemoConfig | None = None
) -> None:
    if path.name != ".env" or not path.parent.is_dir() or path.parent.is_symlink():
        raise ConfigError("Configuration must be written to a checkout's .env")
    previous = path.lstat() if path.exists() or path.is_symlink() else None
    if previous and (not stat.S_ISREG(previous.st_mode) or previous.st_mode & 0o077):
        raise ConfigError("Existing .env must be a private regular file")
    if expected is not None and load_config(path) != expected:
        raise ConfigError(".env changed while selecting configuration; rerun bootstrap")
    text = "".join(
        f"{key}={json.dumps(value, ensure_ascii=True)}\n"
        for key, value in config.values(include_secrets=True).items()
    )
    descriptor, name = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        current = path.lstat() if path.exists() or path.is_symlink() else None
        if current != previous:
            raise ConfigError(".env changed during initialization")
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)
