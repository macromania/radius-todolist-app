"""Selected deployment identity, without discovered infrastructure inventory."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal
from uuid import UUID

Environment = Literal["azure", "local"]
SLOTS = ("management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data")
SECRET_KEYS = {f"DEMO_KEY_{slot.upper().replace('-', '_')}": slot for slot in SLOTS}
PUBLIC_KEYS = {
    "DEMO_ENV",
    "DEMO_PROJECT",
    "DEMO_DEPLOYMENT",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_LOCATION",
    "DEMO_KEY_VAULT",
    "DEMO_REVISION",
}


class ConfigError(ValueError):
    """Configuration errors never include supplied values."""


@dataclass(frozen=True)
class DemoConfig:
    environment: Environment
    project: str
    deployment: str
    subscription: str | None = None
    location: str | None = None
    key_vault: str | None = None
    revision: str | None = None
    demo_keys: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.environment not in {"azure", "local"}:
            raise ConfigError("DEMO_ENV must be azure or local")
        for key, value in (("DEMO_PROJECT", self.project), ("DEMO_DEPLOYMENT", self.deployment)):
            if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,15}", value):
                raise ConfigError(f"{key} must be a lowercase name of 1-16 characters")
            if value.endswith("-"):
                raise ConfigError(f"{key} must not end with a hyphen")
        if len(self.stem) > 25:
            raise ConfigError("Combined project, deployment and environment name is too long")
        if self.environment == "azure":
            if not isinstance(self.subscription, str):
                raise ConfigError("AZURE_SUBSCRIPTION_ID is required")
            try:
                identifier = str(UUID(self.subscription))
            except ValueError:
                raise ConfigError("AZURE_SUBSCRIPTION_ID must be a UUID") from None
            if identifier != self.subscription.lower():
                raise ConfigError("AZURE_SUBSCRIPTION_ID must use the canonical UUID format")
            object.__setattr__(self, "subscription", identifier)
            if not isinstance(self.location, str) or not re.fullmatch(
                r"[a-z][a-z0-9]{1,31}", self.location
            ):
                raise ConfigError("AZURE_LOCATION must be an Azure location identifier")
        elif any(value is not None for value in (self.subscription, self.location, self.key_vault)):
            raise ConfigError("Local configuration must not contain Azure settings")
        if self.key_vault is not None and (
            not isinstance(self.key_vault, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{1,22}[a-z0-9]", self.key_vault)
            or "--" in self.key_vault
        ):
            raise ConfigError("DEMO_KEY_VAULT must be a vault name")
        if self.revision is not None and (
            not isinstance(self.revision, str) or not re.fullmatch(r"[a-f0-9]{40}", self.revision)
        ):
            raise ConfigError("DEMO_REVISION must be a full Git commit")
        if not isinstance(self.demo_keys, Mapping) or set(self.demo_keys) - set(SLOTS):
            raise ConfigError("Unknown demo-key slot")
        for value in self.demo_keys.values():
            if not isinstance(value, str) or not re.fullmatch(r"[!-~]{32,512}", value):
                raise ConfigError(
                    "Demo keys must contain 32-512 printable ASCII characters without whitespace"
                )
        object.__setattr__(self, "demo_keys", MappingProxyType(dict(self.demo_keys)))

    @property
    def stem(self) -> str:
        return f"{self.project}-{self.deployment}-{self.environment}"

    @property
    def identity_hash(self) -> str:
        value = f"{self.subscription or ''}/{self.project}/{self.deployment}/{self.environment}"
        return hashlib.sha256(value.encode()).hexdigest()[:20]

    @property
    def vault_name(self) -> str:
        return self.key_vault or f"kv-{self.identity_hash}"

    @property
    def registry_name(self) -> str:
        return f"acr{self.identity_hash}"

    def slot_name(self, slot: str) -> str:
        if slot not in SLOTS:
            raise ConfigError("Unknown plane slot")
        return f"{self.stem}-{slot}"

    def namespace(self, slot: str) -> str:
        name = self.slot_name(slot)
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        return f"{name}-{role}"

    def values(self, *, include_secrets: bool = False) -> dict[str, str]:
        values = {
            "DEMO_ENV": self.environment,
            "DEMO_PROJECT": self.project,
            "DEMO_DEPLOYMENT": self.deployment,
        }
        for key, value in (
            ("AZURE_SUBSCRIPTION_ID", self.subscription),
            ("AZURE_LOCATION", self.location),
            ("DEMO_KEY_VAULT", self.key_vault),
            ("DEMO_REVISION", self.revision),
        ):
            if value is not None:
                values[key] = value
        for key, slot in SECRET_KEYS.items():
            if slot in self.demo_keys:
                values[key] = self.demo_keys[slot] if include_secrets else "[redacted]"
        return values

    def public_values(self) -> dict[str, str]:
        return {key: value for key, value in self.values().items() if key in PUBLIC_KEYS}

    @classmethod
    def from_values(cls, values: Mapping[str, str]) -> DemoConfig:
        if set(values) - PUBLIC_KEYS - SECRET_KEYS.keys():
            raise ConfigError("Unknown configuration key")
        environment = values.get("DEMO_ENV")
        if environment not in {"azure", "local"}:
            raise ConfigError("DEMO_ENV must be azure or local")
        return cls(
            environment="azure" if environment == "azure" else "local",
            project=values.get("DEMO_PROJECT", ""),
            deployment=values.get("DEMO_DEPLOYMENT", ""),
            subscription=values.get("AZURE_SUBSCRIPTION_ID"),
            location=values.get("AZURE_LOCATION"),
            key_vault=values.get("DEMO_KEY_VAULT"),
            revision=values.get("DEMO_REVISION"),
            demo_keys={slot: values[key] for key, slot in SECRET_KEYS.items() if key in values},
        )
