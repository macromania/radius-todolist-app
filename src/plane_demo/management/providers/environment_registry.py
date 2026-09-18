"""Operator-only changes to prepared environment capacity and reporting logins."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql

from plane_demo.management.providers.identity import isolated_pair
from plane_demo.management.provisioning import OperatorConfig, ProvisioningError
from plane_demo.setup.bootstrap import (
    catalog_digest,
    create_pair_records,
    grant_setup_memberships,
    initialized,
    restore_setup_memberships,
    schema_contract,
)


class GuardedConnection:
    def __init__(self, connection, guard: Callable[[], None]):
        self.connection, self.guard = connection, guard

    def execute(self, *args, **kwargs):
        self.guard()
        result = self.connection.execute(*args, **kwargs)
        self.guard()
        return result


class EnvironmentRegistry:
    def __init__(
        self,
        config: OperatorConfig,
        dsn: str,
        guard: Callable[[], None],
        *,
        schema_directory: Path = Path("/app/sql"),
        connector=psycopg.connect,
    ):
        if not config.prepared_environments:
            raise ProvisioningError("prepared_environments_required")
        self.config, self._dsn, self.guard = config, dsn, guard
        self.schema_directory, self.connector = schema_directory, connector

    @contextmanager
    def transaction(self):
        self.guard()
        with self.connector(self._dsn, connect_timeout=10) as connection:
            guarded = GuardedConnection(connection, self.guard)
            guarded.execute("SELECT pg_advisory_xact_lock(35510,1)")
            yield guarded
            self.guard()

    def manifest(self, connection):
        rows = connection.execute(
            "SELECT configuration FROM demo_metadata.schema_version"
        ).fetchall()
        if len(rows) != 1 or not isinstance(rows[0][0], dict):
            raise ProvisioningError("environment_registry_manifest_invalid")
        configuration = rows[0][0]
        slots = configuration.get("slots")
        known = {item["pair_id"] for item in self.config.pair_slots}
        known.update(self.config.foundation.get("environmentFoundations", {}))
        if (
            configuration.get("admission_mode") != "prepared"
            or not isinstance(slots, list)
            or not slots
        ):
            raise ProvisioningError("environment_registry_manifest_invalid")
        seen = set()
        for item in slots:
            if not isinstance(item, dict) or set(item) != {"pair_id", "reporting_role"}:
                raise ProvisioningError("environment_registry_manifest_invalid")
            pair = item["pair_id"]
            if (
                not isinstance(pair, str)
                or pair not in known
                or pair in seen
                or (pair != "shared" and isolated_pair(pair) != pair)
                or item["reporting_role"] != "cp_" + pair.replace("-", "_")
            ):
                raise ProvisioningError("environment_registry_manifest_invalid")
            seen.add(pair)
        if "shared" not in seen:
            raise ProvisioningError("environment_registry_manifest_invalid")
        roles, _, expected = schema_contract(
            "management", slots, self.schema_directory, "", "prepared"
        )
        if not initialized(connection, "management", roles, expected):
            raise ProvisioningError("environment_registry_uninitialized")
        return slots, roles

    def observe(self):
        with self.transaction() as connection:
            slots, _ = self.manifest(connection)
            setup, memberships = grant_setup_memberships(connection)
            connection.execute("SET ROLE plane_owner")
            mode = connection.execute(
                "SELECT mode FROM management.admission_settings WHERE singleton"
            ).fetchone()
            rows = connection.execute(
                "SELECT p.pair_id,p.reporting_role,p.stage,l.login_role "
                "FROM management.pairs p LEFT JOIN management.login_pairs l USING(pair_id) "
                "ORDER BY p.pair_id"
            ).fetchall()
            expected = {(item["pair_id"], item["reporting_role"]) for item in slots}
            if (
                mode != ("prepared",)
                or {(row[0], row[1]) for row in rows} != expected
                or any(row[1] != row[3] for row in rows)
            ):
                raise ProvisioningError("environment_registry_binding_mismatch")
            restore_setup_memberships(connection, setup, memberships)
            return [
                {"pair_id": pair, "reporting_role": role, "stage": stage}
                for pair, role, stage, _ in rows
            ]

    def register(self, pair: str, password: str) -> None:
        if pair not in {item["pair_id"] for item in self.config.pair_slots}:
            raise ProvisioningError("invalid_pair_assignment")
        if not isinstance(password, str) or len(password) < 32:
            raise ProvisioningError("invalid_plane_credentials")
        role = "cp_" + pair.replace("-", "_")
        with self.transaction() as connection:
            slots, _ = self.manifest(connection)
            if any(item["pair_id"] == pair for item in slots):
                return
            if connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                raise ProvisioningError("unregistered_reporting_role_exists")
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOBYPASSRLS NOREPLICATION PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            setup, memberships = grant_setup_memberships(connection)
            create_pair_records(connection, pair, role)
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA demo_metadata TO {}").format(sql.Identifier(role))
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON demo_metadata.schema_version TO {}").format(
                    sql.Identifier(role)
                )
            )
            expanded = [*slots, {"pair_id": pair, "reporting_role": role}]
            roles, _, expected = schema_contract(
                "management", expanded, self.schema_directory, "", "prepared"
            )
            digest = catalog_digest(connection, "management", roles)
            connection.execute(
                "UPDATE demo_metadata.schema_version "
                "SET configuration=%s::jsonb,catalog_sha256=%s WHERE singleton",
                (json.dumps(expected["configuration"]), digest),
            )
            restore_setup_memberships(connection, setup, memberships)
            if not initialized(connection, "management", roles, expected):
                raise ProvisioningError("environment_registry_update_unverified")

    def available(self, pair: str) -> None:
        with self.transaction() as connection:
            slots, _ = self.manifest(connection)
            if pair not in {item["pair_id"] for item in slots}:
                raise ProvisioningError("environment_not_registered")
            setup, memberships = grant_setup_memberships(connection)
            connection.execute("SET ROLE plane_owner")
            changed = connection.execute(
                "UPDATE management.pairs SET stage='available' "
                "WHERE pair_id=%s AND stage IN ('allocated','available') RETURNING pair_id",
                (pair,),
            ).fetchone()
            if changed != (pair,):
                raise ProvisioningError("environment_registry_state_changed")
            restore_setup_memberships(connection, setup, memberships)

    def retire(self, pair: str) -> None:
        if isolated_pair(pair) != pair:
            raise ProvisioningError("isolated_environment_required")
        with self.transaction() as connection:
            slots, _ = self.manifest(connection)
            if pair not in {item["pair_id"] for item in slots}:
                raise ProvisioningError("environment_not_registered")
            setup, memberships = grant_setup_memberships(connection)
            connection.execute("SET ROLE plane_owner")
            if connection.execute(
                "SELECT 1 FROM management.tenants WHERE pair_id=%s LIMIT 1", (pair,)
            ).fetchone():
                raise ProvisioningError("environment_has_tenants")
            changed = connection.execute(
                "UPDATE management.pairs SET stage='allocated' WHERE pair_id=%s RETURNING pair_id",
                (pair,),
            ).fetchone()
            if changed != (pair,):
                raise ProvisioningError("environment_registry_state_changed")
            restore_setup_memberships(connection, setup, memberships)
