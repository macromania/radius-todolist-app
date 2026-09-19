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
AZURE_GROUP_LAYOUT = "plane-v2"
IDENTITY_PURPOSES = {
    "controlPlane": "control-plane",
    "kubelet": "kubelet",
    "radius": "radius",
    "gateway": "gateway",
    "certificateIssuer": "certificate-issuer",
}
SLOTS = ("management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data")
AZURE_DEFAULT_SLOTS = ("management", "shared-control", "shared-data")
AZURE_ENVIRONMENT_MODE = "prepared-v1"
ISOLATED_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,10}[a-z0-9])?")
SECRET_KEYS = {f"DEMO_KEY_{slot.upper().replace('-', '_')}": slot for slot in SLOTS}
PUBLIC_KEYS = {
    "DEMO_ENV",
    "DEMO_PROJECT",
    "DEMO_DEPLOYMENT",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_LOCATION",
    "AZURE_NODE_VM_SIZE",
    "AZURE_POSTGRES_SKU",
    "AZURE_POSTGRES_TIER",
    "DEMO_KEY_VAULT",
    "DEMO_REVISION",
}
RESOURCE_SIZING = {
    "aks_tier": ("AZURE_AKS_TIER", "aksTier", ("Free", "Standard")),
    "node_count": ("AZURE_NODE_COUNT", "nodeCount", (2, 3, 4)),
    "node_os_disk_gb": ("AZURE_NODE_OS_DISK_GB", "nodeOsDiskSizeGb", (64, 128, 256)),
    "postgres_storage_gb": (
        "AZURE_POSTGRES_STORAGE_GB",
        "postgresStorageSizeGb",
        (32, 64, 128, 256),
    ),
    "redis_sku_name": (
        "AZURE_REDIS_SKU",
        "redisSkuName",
        (
            "Balanced_B0",
            "Balanced_B1",
            "Balanced_B3",
            "Balanced_B5",
            "Balanced_B10",
            "Balanced_B20",
        ),
    ),
    "gateway_capacity": ("AZURE_GATEWAY_CAPACITY", "gatewayCapacity", (1, 2, 3)),
    "registry_sku": ("AZURE_REGISTRY_SKU", "registrySkuName", ("Basic", "Standard", "Premium")),
    "key_vault_sku": ("AZURE_KEY_VAULT_SKU", "vaultSkuName", ("standard", "premium")),
}
PUBLIC_KEYS.update(value[0] for value in RESOURCE_SIZING.values())
COMPUTE_SELECTION_FIELDS = (
    "node_vm_size",
    "postgres_sku_name",
    "postgres_sku_tier",
    *RESOURCE_SIZING,
)


class ConfigError(ValueError):
    """Configuration errors never include supplied values."""


def isolated_pair(name: str) -> str:
    if not isinstance(name, str):
        raise ConfigError("Invalid isolated environment name")
    short = name.removeprefix("isolated-")
    if not ISOLATED_NAME.fullmatch(short) or short in {"shared", "management"}:
        raise ConfigError("Isolated environment names require 1-12 lowercase letters or digits")
    return f"isolated-{short}"


def azure_slot(slot: str) -> bool:
    if slot in AZURE_DEFAULT_SLOTS:
        return True
    if not isinstance(slot, str):
        return False
    pair, separator, role = slot.rpartition("-")
    if not separator or role not in {"control", "data"} or not pair.startswith("isolated-"):
        return False
    try:
        return isolated_pair(pair) == pair
    except ConfigError:
        return False


def provisioning_namespace(prefix: str, slot: str, *, index: int | None = None) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,24}", prefix) or (
        slot == "management" or not azure_slot(slot)
    ):
        raise ConfigError("Invalid child provisioning namespace selection")
    if index is None:
        if slot not in SLOTS[1:]:
            raise ConfigError("Named environments require an explicit allocation index")
        index = SLOTS.index(slot)
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 14:
        raise ConfigError("Invalid child allocation index")
    return f"{prefix}-p-{index}"


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
    node_vm_size: str | None = None
    postgres_sku_name: str | None = None
    postgres_sku_tier: str | None = None
    aks_tier: str | None = None
    node_count: int | None = None
    node_os_disk_gb: int | None = None
    postgres_storage_gb: int | None = None
    redis_sku_name: str | None = None
    gateway_capacity: int | None = None
    registry_sku: str | None = None
    key_vault_sku: str | None = None

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
        elif any(
            value is not None
            for value in (
                self.subscription,
                self.location,
                self.key_vault,
                self.node_vm_size,
                self.postgres_sku_name,
                self.postgres_sku_tier,
                *(getattr(self, name) for name in RESOURCE_SIZING),
            )
        ):
            raise ConfigError("Local configuration must not contain Azure settings")
        if self.node_vm_size is not None and (
            not isinstance(self.node_vm_size, str)
            or not re.fullmatch(r"Standard_[A-Za-z0-9_]{1,64}", self.node_vm_size)
        ):
            raise ConfigError("AZURE_NODE_VM_SIZE must be an Azure VM size")
        if (self.postgres_sku_name is None) != (self.postgres_sku_tier is None):
            raise ConfigError("AZURE_POSTGRES_SKU and AZURE_POSTGRES_TIER must be set together")
        if self.postgres_sku_name is not None and (
            not isinstance(self.postgres_sku_name, str)
            or not re.fullmatch(r"Standard_[A-Za-z0-9_]{1,64}", self.postgres_sku_name)
            or not isinstance(self.postgres_sku_tier, str)
            or self.postgres_sku_tier not in {"Burstable", "GeneralPurpose", "MemoryOptimized"}
        ):
            raise ConfigError("Invalid PostgreSQL SKU or tier")
        for name, (key, _, choices) in RESOURCE_SIZING.items():
            value = getattr(self, name)
            if value is not None and (type(value) is not type(choices[0]) or value not in choices):
                raise ConfigError(f"Invalid {key} sizing choice")
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
        if not (azure_slot(slot) if self.environment == "azure" else slot in SLOTS):
            raise ConfigError("Unknown plane slot")
        return f"{self.stem}-{slot}"

    def namespace(self, slot: str) -> str:
        name = self.slot_name(slot)
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        return f"{name}-{role}"

    def plane_group(self, slot: str) -> str:
        if self.environment != "azure":
            raise ConfigError("Azure resource groups require an Azure configuration")
        return f"rg-{self.slot_name(slot)}"

    def plane_group_id(self, slot: str) -> str:
        return f"/subscriptions/{self.subscription}/resourceGroups/{self.plane_group(slot)}"

    def managed_identity_id(self, slot: str, purpose: str) -> str:
        if purpose not in IDENTITY_PURPOSES.values() and not (
            slot == "management" and purpose in {"coordinator", "harness"}
        ):
            raise ConfigError("Unknown managed identity purpose")
        name = self.stem if purpose in {"coordinator", "harness"} else self.slot_name(slot)
        return (
            self.plane_group_id(slot)
            + f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-{name}-{purpose}"
        )

    def values(self, *, include_secrets: bool = False) -> dict[str, str]:
        values = {
            "DEMO_ENV": self.environment,
            "DEMO_PROJECT": self.project,
            "DEMO_DEPLOYMENT": self.deployment,
        }
        for key, value in (
            ("AZURE_SUBSCRIPTION_ID", self.subscription),
            ("AZURE_LOCATION", self.location),
            ("AZURE_NODE_VM_SIZE", self.node_vm_size),
            ("AZURE_POSTGRES_SKU", self.postgres_sku_name),
            ("AZURE_POSTGRES_TIER", self.postgres_sku_tier),
            ("DEMO_KEY_VAULT", self.key_vault),
            ("DEMO_REVISION", self.revision),
        ):
            if value is not None:
                values[key] = value
        for key, slot in SECRET_KEYS.items():
            if slot in self.demo_keys:
                values[key] = self.demo_keys[slot] if include_secrets else "[redacted]"
        for name, (key, _, _) in RESOURCE_SIZING.items():
            value = getattr(self, name)
            if value is not None:
                values[key] = str(value)
        return values

    @property
    def resource_sizes(self) -> dict[str, str | int]:
        return {
            field: getattr(self, name)
            for name, (_, field, _) in RESOURCE_SIZING.items()
            if getattr(self, name) is not None
        }

    def public_values(self) -> dict[str, str]:
        return {key: value for key, value in self.values().items() if key in PUBLIC_KEYS}

    @classmethod
    def from_values(cls, values: Mapping[str, str]) -> DemoConfig:
        if set(values) - PUBLIC_KEYS - SECRET_KEYS.keys():
            raise ConfigError("Unknown configuration key")
        environment = values.get("DEMO_ENV")
        if environment not in {"azure", "local"}:
            raise ConfigError("DEMO_ENV must be azure or local")
        sizing = {}
        for name, (key, _, choices) in RESOURCE_SIZING.items():
            if key in values:
                value = values[key]
                if not isinstance(value, str):
                    raise ConfigError(f"Invalid {key} sizing choice")
                if isinstance(choices[0], int):
                    if not value.isascii() or not value.isdecimal():
                        raise ConfigError(f"Invalid {key} sizing choice")
                    value = int(value)
                sizing[name] = value
        return cls(
            environment="azure" if environment == "azure" else "local",
            project=values.get("DEMO_PROJECT", ""),
            deployment=values.get("DEMO_DEPLOYMENT", ""),
            subscription=values.get("AZURE_SUBSCRIPTION_ID"),
            location=values.get("AZURE_LOCATION"),
            node_vm_size=values.get("AZURE_NODE_VM_SIZE"),
            postgres_sku_name=values.get("AZURE_POSTGRES_SKU"),
            postgres_sku_tier=values.get("AZURE_POSTGRES_TIER"),
            key_vault=values.get("DEMO_KEY_VAULT"),
            revision=values.get("DEMO_REVISION"),
            demo_keys={slot: values[key] for key, slot in SECRET_KEYS.items() if key in values},
            **sizing,
        )
