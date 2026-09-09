"""Protected, once-generated runtime credentials; no setup login is retained."""

from __future__ import annotations

import json
import secrets
import stat
from pathlib import Path

from psycopg.conninfo import make_conninfo

from plane_demo.management.providers.commands import write_json
from plane_demo.management.provisioning import OperatorConfig, ProvisioningError


class Credentials:
    def __init__(self, path: Path, seed: dict | None = None):
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

    def assert_management(self, config: OperatorConfig) -> None:
        management = self.plane("management")
        for role in {"mgmt_provisioner", *(slot["reporting_role"] for slot in config.pair_slots)}:
            if len(management.get("passwords", {}).get(role, "")) < 32:
                raise ProvisioningError("management_credentials_missing")
        self.dsn("management", "mgmt_provisioner")

    def set_database(self, slot: str, properties: dict) -> None:
        if properties.get("tlsRequired") is not True:
            raise ProvisioningError("database_tls_required")
        self.plane(slot)["database"] = {
            key: properties[key] for key in ("host", "port", "database", "serverId")
        }
        self.save()

    def dsn(self, slot: str, role: str) -> str:
        plane = self.plane(slot)
        try:
            return database_dsn(plane["database"], role, plane["passwords"][role])
        except KeyError:
            raise ProvisioningError("database_credentials_missing") from None

    def runtime_seed(self, config: OperatorConfig) -> dict:
        management = self.plane("management")
        roles = {"mgmt_provisioner", *(slot["reporting_role"] for slot in config.pair_slots)}
        return {
            "version": 1,
            "planes": {
                "management": {
                    "database": management["database"],
                    "passwords": {role: management["passwords"][role] for role in roles},
                }
            },
        }


def database_dsn(properties: dict, role: str, password: str) -> str:
    host = properties["host"]
    if (
        not isinstance(host, str)
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
