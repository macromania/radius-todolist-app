"""Provider-independent SQL bootstrap Jobs and plane-specific runtime Secrets."""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Protocol

from plane_demo.management.providers.commands import Commands, create_json
from plane_demo.management.providers.credentials import Credentials, database_dsn
from plane_demo.management.provisioning import ProvisioningConfig, ProvisioningError

logger = logging.getLogger(__name__)


class PlaneRuntime(Protocol):
    @property
    def config(self) -> ProvisioningConfig: ...

    state: Path
    credentials: Credentials
    commands: Commands

    def names(self, slot: str) -> tuple[str, str]: ...
    def kube_get(self, slot: str, namespace: str, kind: str, name: str) -> dict | None: ...
    def apply(self, slot: str, resources: dict | list, *, create: bool = False) -> None: ...
    def deploy(self, slot: str, template: str, application: str, values: dict) -> None: ...
    def resource(self, slot: str, kind: str, name: str, application: str) -> dict: ...
    def kubectl(self, slot: str, *args: str) -> str: ...
    def secret(self, slot: str, namespace: str, name: str, values: dict) -> None: ...
    def database_resource_exists(self, slot: str) -> bool: ...
    def cleanup_initialization(self, slot: str, namespace: str, setup_name: str | None) -> None: ...
    def job(
        self, namespace: str, name: str, image: str, command: list[str], account: str
    ) -> dict: ...


def initialize_database(provider: PlaneRuntime, slot: str) -> None:
    provider.config.allocation(slot)
    role, namespace = provider.names(slot)
    intent = provider.state / f"{slot}-database-intent.json"
    roles = (
        {
            "mgmt_api",
            "mgmt_provisioner",
            *(item["reporting_role"] for item in provider.config.pair_slots),
        }
        if role == "management"
        else {"cp_api", "cp_reconciler", "dp_reconciler"}
    )
    marker = provider.kube_get(slot, namespace, "configmap", "database-initialized")
    if marker:
        plane = provider.credentials.plane(slot)
        if {key: marker["data"][key] for key in ("serverId", "database")} != {
            "serverId": plane["database"]["serverId"],
            "database": plane["database"]["database"],
        }:
            raise ProvisioningError("database_marker_mismatch")
        provider.cleanup_initialization(slot, namespace, marker["data"].get("setupSecretName"))
        return
    if (
        intent.exists()
        or intent.is_symlink()
        or provider.kube_get(slot, namespace, "secret", "database-init")
        or provider.kube_get(slot, namespace, "job", "database-init")
        or provider.kube_get(slot, namespace, "secret", "postgres-setup")
        or provider.credentials.has_database(slot)
        or provider.database_resource_exists(slot)
    ):
        raise ProvisioningError("database_initialization_incomplete")
    plane = provider.credentials.ensure(slot, roles)
    provider.commands.protect(plane)
    try:
        create_json(
            intent,
            {
                "version": 1,
                "slot": slot,
                "application": role,
                "resourceType": "Demo.Platform/postgreSqlDatabases",
                "resourceName": "postgres",
            },
        )
    except FileExistsError:
        raise ProvisioningError("database_initialization_incomplete") from None
    provider.deploy(slot, "database", role, {"databaseName": role})
    properties = provider.resource(slot, "postgresql", "postgres", role)
    setup_name = properties.get("setupSecretName")
    if not isinstance(setup_name, str) or not re.fullmatch(r"[a-z0-9-]+-setup", setup_name):
        raise ProvisioningError("postgres_setup_contract_missing")
    setup = provider.kube_get(slot, namespace, "secret", setup_name)
    if not setup:
        raise ProvisioningError("postgres_setup_secret_missing")
    password = base64.b64decode(setup["data"]["password"], validate=True).decode()
    provider.commands.protect(password)
    dsn = database_dsn(
        properties, properties["username"], password, environment=provider.credentials.environment
    )
    provider.commands.protect(dsn)
    provider.credentials.set_database(slot, properties)
    variables = {
        "BOOTSTRAP_DSN": dsn,
        "BOOTSTRAP_KIND": role,
        "ROLE_PASSWORDS_JSON": json.dumps(plane["passwords"]),
    }
    if role == "management":
        variables["PAIR_SLOTS_JSON"] = json.dumps(provider.config.pair_slots)
    else:
        variables["PAIR_ID"] = slot.removesuffix("-control")
    provider.secret(slot, namespace, "database-init", variables)
    job = provider.job(
        namespace,
        "database-init",
        provider.config.images["api"],
        ["python", "-m", "plane_demo.setup.bootstrap"],
        "database-init",
    )
    job["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"secretRef": {"name": "database-init"}}
    ]
    provider.apply(slot, job, create=True)
    try:
        provider.kubectl(
            slot,
            "-n",
            namespace,
            "wait",
            "--for=condition=complete",
            "job/database-init",
            "--timeout=600s",
        )
    except ProvisioningError:
        logs = provider.kubectl(slot, "-n", namespace, "logs", "job/database-init", "--tail=100")
        logger.error("database_initialization_failed %s", provider.commands.redact(logs))
        raise
    provider.apply(
        slot,
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "database-initialized", "namespace": namespace},
            "immutable": True,
            "data": {
                "serverId": properties["serverId"],
                "database": properties["database"],
                "setupSecretName": setup_name,
            },
        },
        create=True,
    )
    provider.cleanup_initialization(slot, namespace, setup_name)


def runtime_secrets(provider: PlaneRuntime, slot: str) -> None:
    role, namespace = provider.names(slot)
    if role == "management":
        provider.secret(
            slot,
            namespace,
            "management-api-runtime",
            {
                "MANAGEMENT_DSN": provider.credentials.dsn(slot, "mgmt_api"),
                "DEMO_KEY": provider.credentials.plane(slot)["demoKey"],
            },
        )
        provider.secret(
            slot,
            namespace,
            "provisioner-runtime",
            {
                "MANAGEMENT_DSN": provider.credentials.dsn(slot, "mgmt_provisioner"),
                "PROVIDER": provider.credentials.environment,
                "PROVISIONING_CONFIG": "/etc/plane-demo/provisioning.json",
                "PROVISIONING_CREDENTIALS_JSON": json.dumps(
                    provider.credentials.runtime_seed(provider.config)
                ),
            },
        )
    elif role == "control":
        pair = slot.removesuffix("-control")
        reporting_role = next(
            item["reporting_role"] for item in provider.config.pair_slots if item["pair_id"] == pair
        )
        provider.secret(
            slot,
            namespace,
            "control-api-runtime",
            {
                "CONTROL_DSN": provider.credentials.dsn(slot, "cp_api"),
                "DEMO_KEY": provider.credentials.plane(slot)["demoKey"],
            },
        )
        provider.secret(
            slot,
            namespace,
            "control-reconciler-runtime",
            {
                "CONTROL_DSN": provider.credentials.dsn(slot, "cp_reconciler"),
                "MANAGEMENT_DSN": provider.credentials.dsn("management", reporting_role),
                "PAIR_ID": pair,
            },
        )
    else:
        pair = slot.removesuffix("-data")
        plane = provider.credentials.ensure(slot, set())
        common = {"PAIR_ID": pair, "PROJECT_ID": "radplanes", "KUBE_NAMESPACE": namespace}
        provider.secret(
            slot, namespace, "data-api-runtime", {**common, "DEMO_KEY": plane["demoKey"]}
        )
        provider.secret(
            slot,
            namespace,
            "data-reconciler-runtime",
            {**common, "CONTROL_DSN": provider.credentials.dsn(f"{pair}-control", "dp_reconciler")},
        )


def role_binding(namespace, name, subject_namespace, subject_name, rules) -> list[dict]:
    return [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": name, "namespace": namespace},
            "rules": rules,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": namespace},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
            "subjects": [
                {"kind": "ServiceAccount", "name": subject_name, "namespace": subject_namespace}
            ],
        },
    ]


def job(namespace: str, name: str, image: str, command: list[str], account: str) -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "template": {
                "metadata": {"labels": {"plane-demo/project": "radplanes"}},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": account,
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": name,
                            "image": image,
                            "command": command,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }
