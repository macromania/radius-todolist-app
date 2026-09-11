"""Protected, once-generated runtime credentials; no setup login is retained."""

from __future__ import annotations

import json
import secrets
import stat
from pathlib import Path
from typing import Literal

from psycopg.conninfo import make_conninfo

from plane_demo.management.providers.commands import write_json
from plane_demo.management.providers.local_config import private_ipv4
from plane_demo.management.provisioning import ProvisioningConfig, ProvisioningError

Environment = Literal["azure", "local"]


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

    def ensure(self, slot: str, roles: set[str]) -> dict:
        existing = self._data["planes"].get(slot)
        if existing is None:
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
