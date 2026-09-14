"""Provider-independent SQL bootstrap Jobs and plane-specific runtime Secrets."""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from plane_demo.management.providers.commands import Commands
from plane_demo.management.providers.credentials import Credentials, database_dsn
from plane_demo.management.providers.local_config import same_radius_id
from plane_demo.management.provisioning import (
    Cluster,
    PairResult,
    ProvisioningConfig,
    ProvisioningError,
)

logger = logging.getLogger(__name__)


class PlaneRuntime(Protocol):
    @property
    def config(self) -> ProvisioningConfig: ...

    state: Path
    radius_scope: str
    credentials: Credentials
    commands: Commands

    def names(self, slot: str) -> tuple[str, str]: ...
    def kube_get(self, slot: str, namespace: str, kind: str, name: str) -> dict | None: ...
    def apply(self, slot: str, resources: dict | list, *, create: bool = False) -> None: ...
    def deploy(self, slot: str, template: str, application: str, values: dict) -> None: ...
    def resource(self, slot: str, kind: str, name: str, application: str) -> dict: ...
    def kubectl(self, slot: str, *args: str) -> str: ...
    def secret(
        self, slot: str, namespace: str, name: str, values: dict, *, create: bool = False
    ) -> None: ...
    def database_resource_exists(self, slot: str) -> bool: ...
    def get_access(self, slot: str) -> Cluster: ...
    def expected_cluster_id(self, slot: str) -> str: ...
    def validate_endpoint(self, slot: str, value: str) -> str: ...
    def cleanup_initialization(self, slot: str, namespace: str, setup_name: str | None) -> None: ...
    def job(
        self, namespace: str, name: str, image: str, command: list[str], account: str
    ) -> dict: ...


def inspect_pair(provider: PlaneRuntime, pair_id: str, radius_scope: str) -> PairResult:
    identifiers, urls = [], []
    owners = f"{radius_scope}/providers/Applications.Core"
    for role in ("control", "data"):
        slot = f"{pair_id}-{role}"
        expected = provider.expected_cluster_id(slot)
        cluster = provider.resource("management", "cluster", slot, f"cluster-{slot}")
        if cluster.get("clusterId") != expected or cluster.get("provisioningState") != "Succeeded":
            raise ProvisioningError("pair_inventory_mismatch")
        if not same_radius_id(
            cluster.get("application"), f"{owners}/applications/cluster-{slot}"
        ) or not same_radius_id(
            cluster.get("environment"), f"{owners}/environments/provision-{slot}"
        ):
            raise ProvisioningError("pair_owner_mismatch")
        access = provider.get_access(slot)
        if access.cluster_id != expected:
            raise ProvisioningError("pair_inventory_mismatch")
        gateway = provider.resource(slot, "gateway", "gateway", role)
        if gateway.get("provisioningState") != "Succeeded":
            raise ProvisioningError("gateway_not_ready")
        if not same_radius_id(
            gateway.get("application"), f"{owners}/applications/{role}"
        ) or not same_radius_id(gateway.get("environment"), f"{owners}/environments/{slot}"):
            raise ProvisioningError("pair_owner_mismatch")
        urls.append(provider.validate_endpoint(slot, gateway["url"]))
        identifiers.append(expected)
    return PairResult(*identifiers, *urls)


def initialize_database(provider: PlaneRuntime, slot: str) -> None:
    provider.config.allocation(slot)
    role, namespace = provider.names(slot)
    roles = (
        {
            "mgmt_api",
            "mgmt_provisioner",
            *(item["reporting_role"] for item in provider.config.pair_slots),
        }
        if role == "management"
        else {"cp_api", "cp_reconciler", "dp_reconciler"}
    )
    variables = {"BOOTSTRAP_KIND": role}
    if role == "management":
        variables["PAIR_SLOTS_JSON"] = json.dumps(provider.config.pair_slots)
    else:
        variables["PAIR_ID"] = slot.removesuffix("-control")
    if provider.database_resource_exists(slot):
        properties = read_database(provider, slot)
        provider.credentials.set_database(slot, properties)
        variables.update(
            BOOTSTRAP_MODE="observe",
            BOOTSTRAP_DSN=provider.credentials.dsn(
                slot, "mgmt_provisioner" if role == "management" else "cp_api"
            ),
        )
        run_database_job(
            provider, slot, f"database-observe-{uuid4().hex[:12]}", variables, observe=True
        )
        provider.cleanup_initialization(slot, namespace, properties.get("setupSecretName"))
        return
    if (
        provider.kube_get(slot, namespace, "secret", "database-init")
        or provider.kube_get(slot, namespace, "job", "database-init")
        or provider.kube_get(slot, namespace, "secret", "postgres-setup")
        or any(
            provider.kube_get(slot, namespace, "secret", name)
            for name in (
                f"{role}-api-runtime",
                "provisioner-runtime" if role == "management" else "control-reconciler-runtime",
            )
        )
    ):
        raise ProvisioningError("database_initialization_incomplete")
    plane = provider.credentials.ensure(slot, roles)
    provider.commands.protect(plane)
    variables["ROLE_PASSWORDS_JSON"] = json.dumps(plane["passwords"])
    provider.secret(slot, namespace, "database-init", variables, create=True)
    provider.deploy(slot, "database", role, {"databaseName": role})
    properties = read_database(provider, slot)
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
    variables["BOOTSTRAP_DSN"] = dsn
    run_database_job(provider, slot, "database-init", variables)
    provider.cleanup_initialization(slot, namespace, setup_name)


def read_database(provider: PlaneRuntime, slot: str) -> dict:
    role, _ = provider.names(slot)
    properties = provider.resource(slot, "postgresql", "postgres", role)
    owners = f"{provider.radius_scope}/providers/Applications.Core"
    if (
        properties.get("provisioningState") != "Succeeded"
        or not same_radius_id(properties.get("application"), f"{owners}/applications/{role}")
        or not same_radius_id(properties.get("environment"), f"{owners}/environments/{slot}")
        or properties.get("database") != role
        or properties.get("username") != "plane_setup"
    ):
        raise ProvisioningError("database_owner_mismatch")
    return properties


def run_database_job(
    provider: PlaneRuntime, slot: str, name: str, variables: dict, *, observe: bool = False
) -> None:
    _, namespace = provider.names(slot)
    provider.secret(slot, namespace, name, variables, create=observe)
    job = provider.job(
        namespace,
        name,
        provider.config.images["api"],
        ["python", "-m", "plane_demo.setup.bootstrap"],
        "database-init",
    )
    job["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [{"secretRef": {"name": name}}]
    created = False
    try:
        provider.apply(slot, job, create=True)
        created = True
        try:
            provider.kubectl(
                slot,
                "-n",
                namespace,
                "wait",
                "--for=condition=complete",
                f"job/{name}",
                "--timeout=600s",
            )
        except ProvisioningError:
            logs = provider.kubectl(slot, "-n", namespace, "logs", f"job/{name}", "--tail=100")
            logger.error("database_initialization_failed %s", provider.commands.redact(logs))
            raise
    finally:
        if observe:
            provider.kubectl(
                slot,
                "-n",
                namespace,
                "delete",
                *([f"job/{name}"] if created else []),
                f"secret/{name}",
                "--wait=true",
                "--ignore-not-found",
            )


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
