"""Protected, once-generated runtime credentials; no setup login is retained."""

from __future__ import annotations

import json
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from psycopg.conninfo import make_conninfo

from plane_demo.management.providers.commands import write_json
from plane_demo.management.providers.local_config import private_ipv4
from plane_demo.management.providers.secret_store import (
    CredentialScope,
    CredentialStore,
    StoreError,
)
from plane_demo.management.provisioning import ProvisioningConfig, ProvisioningError

Environment = Literal["azure", "local"]


def credential_roles(config: ProvisioningConfig, slot: str) -> set[str]:
    config.allocation(slot)
    if slot == "management":
        return {
            "mgmt_api",
            "mgmt_provisioner",
            *(item["reporting_role"] for item in config.pair_slots),
        }
    if slot.endswith("-control"):
        return {"cp_api", "cp_reconciler", "dp_reconciler"}
    return set()


class Credentials:
    def __init__(self, path: Path, seed: dict | None = None, *, environment: Environment = "azure"):
        if environment not in ("azure", "local"):
            raise ProvisioningError("invalid_credentials_environment")
        self.environment = environment
        self.path = path
        if path.exists():
            if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ProvisioningError("credentials_permissions")
            self._data = json.loads(path.read_text())
            if seed and self._data.get("planes", {}).get("management") != seed.get(
                "planes", {}
            ).get("management"):
                raise ProvisioningError("credentials_seed_mismatch")
        else:
            self._data = seed or {"version": 1, "planes": {}}
            self.save()
        if self._data.get("version") != 1 or not isinstance(self._data.get("planes"), dict):
            raise ProvisioningError("invalid_credentials")
        if self._data.get("provider", "azure") != environment:
            if environment != "local" or self._data["planes"]:
                raise ProvisioningError("credentials_environment_mismatch")
            self._data["provider"] = "local"
            self.save()

    def save(self) -> None:
        write_json(self.path, self._data)

    def plane(self, slot: str) -> dict:
        if slot not in self._data["planes"]:
            raise ProvisioningError("plane_credentials_missing")
        return self._data["planes"][slot]

    def has_database(self, slot: str) -> bool:
        return bool(self._data["planes"].get(slot, {}).get("database"))

    def bind(
        self,
        _reader: Callable[[str], dict],
        protect: Callable[[object], None],
        _guard: Callable[[], None],
    ) -> None:
        protect(self._data)

    def demo_key(self, slot: str) -> str:
        return self.plane(slot)["demoKey"]

    def ensure(self, slot: str, roles: set[str], *, require_existing: bool = False) -> dict:
        existing = self._data["planes"].get(slot)
        if existing is None:
            if require_existing:
                raise ProvisioningError("plane_credentials_missing")
            existing = {
                "demoKey": secrets.token_urlsafe(48),
                "passwords": {role: secrets.token_urlsafe(48) for role in sorted(roles)},
            }
            self._data["planes"][slot] = existing
            self.save()
        if (
            len(existing.get("demoKey", "")) < 32
            or set(existing.get("passwords", {})) != roles
            or any(len(value) < 32 for value in existing["passwords"].values())
        ):
            raise ProvisioningError("invalid_plane_credentials")
        return existing

    def assert_management(self, config: ProvisioningConfig) -> None:
        management = self.plane("management")
        for role in {"mgmt_provisioner", *(slot["reporting_role"] for slot in config.pair_slots)}:
            if len(management.get("passwords", {}).get(role, "")) < 32:
                raise ProvisioningError("management_credentials_missing")
        self.dsn("management", "mgmt_provisioner")

    def set_database(self, slot: str, properties: dict) -> None:
        if properties.get("tlsRequired") is not (self.environment == "azure"):
            raise ProvisioningError("database_tls_required")
        database_dsn(properties, "plane_setup", "x" * 48, environment=self.environment)
        self.plane(slot)["database"] = {
            key: properties[key] for key in ("host", "port", "database", "serverId", "tlsRequired")
        }
        self.save()

    def dsn(self, slot: str, role: str) -> str:
        plane = self.plane(slot)
        try:
            return database_dsn(
                plane["database"], role, plane["passwords"][role], environment=self.environment
            )
        except KeyError:
            raise ProvisioningError("database_credentials_missing") from None

    def runtime_seed(self, config: ProvisioningConfig) -> dict:
        management = self.plane("management")
        roles = {"mgmt_provisioner", *(slot["reporting_role"] for slot in config.pair_slots)}
        return {
            "version": 1,
            **({"provider": "local"} if self.environment == "local" else {}),
            "planes": {
                "management": {
                    "database": management["database"],
                    "passwords": {role: management["passwords"][role] for role in roles},
                }
            },
        }


class StoredCredentials:
    """Read credentials from their service owner and connection properties from APIs."""

    def __init__(self, config: ProvisioningConfig, store: CredentialStore):
        identity = config.identity
        if identity is None:
            raise ProvisioningError("bootstrap_identity_required")
        if store.scope != CredentialScope(
            identity.project, identity.deployment, identity.environment
        ):
            raise ProvisioningError("credential_owner_mismatch")
        self.environment = identity.environment
        self.config = config
        self.store = store
        self._provided = identity.demo_keys
        self._reader: Callable[[str], dict] | None = None
        self._protect: Callable[[object], None] | None = None
        self._guard: Callable[[], None] | None = None

    def bind(
        self,
        reader: Callable[[str], dict],
        protect: Callable[[object], None],
        guard: Callable[[], None],
    ) -> None:
        self._reader, self._protect, self._guard = reader, protect, guard

    def _value(
        self, slot: str, role: str, *, create: bool = False, require_existing: bool = False
    ) -> str:
        self.config.allocation(slot)
        if self._guard is None or self._protect is None:
            raise ProvisioningError("credential_source_not_bound")
        self._guard()
        try:
            value = (
                self.store.get_or_create(
                    slot,
                    role,
                    require_existing=require_existing,
                    provided_value=self._provided.get(slot) if role == "demoKey" else None,
                )
                if create
                else self.store.get(slot, role)
            )
        except StoreError as error:
            raise ProvisioningError(error.code) from None
        self._protect(value.value)
        self._guard()
        return value.value

    def demo_key(self, slot: str) -> str:
        return self._value(slot, "demoKey")

    def seed_provided_keys(self, slots: set[str] | None = None) -> None:
        for slot in self._provided:
            if slots is None or slot in slots:
                self._value(slot, "demoKey", create=True)

    def retain_administrator(self, password: str) -> None:
        if not self.config.prepared_environments or self.environment != "azure":
            raise ProvisioningError("administrative_credentials_not_enabled")
        if self._guard is None or self._protect is None:
            raise ProvisioningError("credential_source_not_bound")
        self._guard()
        self._protect(password)
        try:
            self.store.get_or_create("management", "management_admin", provided_value=password)
        except StoreError as error:
            raise ProvisioningError(error.code) from None
        self._guard()

    def administrator_dsn(self) -> str:
        if (
            not self.config.prepared_environments
            or self.environment != "azure"
            or self._reader is None
            or self._protect is None
        ):
            raise ProvisioningError("administrative_credentials_not_enabled")
        value = database_dsn(
            self._reader("management"),
            "plane_setup",
            self._value("management", "management_admin"),
            environment="azure",
        )
        self._protect(value)
        return value

    def reporting_password(self, pair: str) -> str:
        if not self.config.prepared_environments:
            raise ProvisioningError("administrative_credentials_not_enabled")
        matches = [item for item in self.config.pair_slots if item["pair_id"] == pair]
        if len(matches) != 1:
            raise ProvisioningError("invalid_pair_assignment")
        return self._value("management", matches[0]["reporting_role"], create=True)

    def ensure(self, slot: str, roles: set[str], *, require_existing: bool = False) -> dict:
        if roles != credential_roles(self.config, slot):
            raise ProvisioningError("invalid_plane_credentials")
        return {
            "demoKey": self._value(slot, "demoKey", create=True, require_existing=require_existing),
            "passwords": {
                role: self._value(slot, role, create=True, require_existing=require_existing)
                for role in sorted(roles)
            },
        }

    def set_database(self, slot: str, properties: dict) -> None:
        """Validate a live result without saving a connection inventory."""
        self.config.allocation(slot)
        database_dsn(properties, "plane_setup", "x" * 48, environment=self.environment)

    def dsn(self, slot: str, role: str) -> str:
        if role not in credential_roles(self.config, slot):
            raise ProvisioningError("database_credentials_missing")
        if self._reader is None or self._protect is None:
            raise ProvisioningError("credential_source_not_bound")
        value = database_dsn(
            self._reader(slot), role, self._value(slot, role), environment=self.environment
        )
        self._protect(value)
        return value

    def assert_management(self, config: ProvisioningConfig) -> None:
        for role in {"mgmt_provisioner", *(item["reporting_role"] for item in config.pair_slots)}:
            self._value("management", role)
        self.dsn("management", "mgmt_provisioner")


type CredentialSource = Credentials | StoredCredentials


def database_dsn(
    properties: dict, role: str, password: str, *, environment: Environment = "azure"
) -> str:
    host = properties["host"]
    if environment == "local":
        try:
            private_ipv4(host)
        except (ValueError, TypeError):
            raise ProvisioningError("invalid_database_connection") from None
        if (
            properties.get("tlsRequired") is not False
            or properties["port"] != 31543
            or not role
            or len(password) < 32
        ):
            raise ProvisioningError("invalid_database_connection")
        return make_conninfo(
            host=host,
            port=31543,
            dbname=properties["database"],
            user=role,
            password=password,
            sslmode="disable",
            connect_timeout=5,
        )
    if (
        environment != "azure"
        or properties.get("tlsRequired", True) is not True
        or not isinstance(host, str)
        or not host.endswith(".postgres.database.azure.com")
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for character in host)
        or int(properties["port"]) != 5432
        or not role
        or len(password) < 32
    ):
        raise ProvisioningError("invalid_database_connection")
    return make_conninfo(
        host=host,
        port=5432,
        dbname=properties["database"],
        user=role,
        password=password,
        sslmode="verify-full",
        sslrootcert="/etc/ssl/certs/ca-certificates.crt",
        connect_timeout=5,
    )
