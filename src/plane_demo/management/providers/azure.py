"""Azure administrative orchestration. Only Radius submits child cluster creation."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import yaml

from plane_demo.management.providers import workloads
from plane_demo.management.providers.commands import (
    Commands,
    write_json,
    write_private,
)
from plane_demo.management.providers.credentials import CredentialSource
from plane_demo.management.providers.identity import provisioning_namespace
from plane_demo.management.providers.redis_nic_tags import BASE_TAGS, ERRORS, Target, same_id
from plane_demo.management.provisioning import (
    Cluster,
    OperatorConfig,
    PairResult,
    ProvisioningError,
    endpoint,
    plain,
)

TYPES = {
    "cluster": ("Demo.Platform/clusters", "clusters"),
    "postgresql": ("Demo.Platform/postgreSqlDatabases", "postgresql"),
    "gateway": ("Demo.Platform/gateways", "gateways"),
    "redis": ("Applications.Datastores/redisCaches", None),
}
logger = logging.getLogger(__name__)
CONTAINER_CERTIFICATE_COMMAND = ("python", "/app/scripts/operations/run-certificate-job.py")


def login_workload_identity(
    commands: Commands, workspace: Path, client_id: str, tenant_id: str
) -> None:
    if (
        os.environ.get("AZURE_CLIENT_ID") != client_id
        or os.environ.get("AZURE_TENANT_ID") != tenant_id
    ):
        raise ProvisioningError("workload_identity_mismatch")
    token_path = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
    if not token_path:
        raise ProvisioningError("workload_identity_required")
    token = Path(token_path).read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
        raise ProvisioningError("invalid_federated_token")
    commands.protect(token)
    directory = workspace / "az"
    directory.mkdir(mode=0o700, exist_ok=True)
    commands.environment["AZURE_CONFIG_DIR"] = str(directory)
    commands.run(
        [
            "az",
            "login",
            "--service-principal",
            "--username",
            client_id,
            "--tenant",
            tenant_id,
            "--federated-token",
            token,
            "--allow-no-subscriptions",
            "--output",
            "none",
            "--only-show-errors",
        ],
        timeout=120,
    )


class AzureProvider:
    radius_scope = "/planes/radius/local/resourceGroups/radplanes"

    def __init__(
        self,
        config: OperatorConfig,
        root: Path,
        credentials: CredentialSource,
        commands: Commands | None = None,
        *,
        workspace: Path | None = None,
    ):
        self.config = config
        self.root = root.resolve()
        self.state = workspace if workspace is not None else self.root / ".state" / "azure"
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.chmod(0o700)
        self.credentials = credentials
        self.commands = commands or Commands(
            self.root,
            state_root=self.state,
            contexts={config.workspace(slot) for slot in config.allocations}
            if config.identity
            else None,
        )
        self.radius_scope = f"/planes/radius/local/resourceGroups/{config.radius_group}"
        self.radius_config = self.state / "radius.yaml"
        self.config_path = self.state / "provisioning.json"
        write_json(self.config_path, config.to_dict())
        credentials.bind(
            lambda slot: workloads.read_database(self, slot),
            self.commands.protect,
            lambda: self.commands.guard(),
        )
        self._verified = False

    def expected_cluster_id(self, slot: str) -> str:
        allocation = self.config.allocation(slot)
        return (
            f"{allocation['clusterResourceGroupId']}/providers/Microsoft.ContainerService/"
            f"managedClusters/{allocation['clusterName']}"
        )

    def validate_endpoint(self, slot: str, value: str) -> str:
        self.config.allocation(slot)
        return endpoint(value)

    def paths(self, slot: str) -> tuple[str, Path]:
        self.config.allocation(slot)
        return self.config.workspace(slot), self.state / f"{slot}.kubeconfig"

    def az(self, *args: str):
        self.login_workload()
        return self.commands.json(
            [
                "az",
                *args,
                "--subscription",
                self.config.foundation["subscriptionId"],
                "--output",
                "json",
                "--only-show-errors",
            ]
        )

    def authenticate(self, *, workload_required: bool = False) -> None:
        token_path = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
        if workload_required and not token_path:
            raise ProvisioningError("workload_identity_required")
        account = self.az("account", "show")
        if (
            account["id"] != self.config.foundation["subscriptionId"]
            or account["tenantId"] != self.config.foundation["tenantId"]
        ):
            raise ProvisioningError("azure_account_mismatch")

    def login_workload(self) -> None:
        if os.environ.get("AZURE_FEDERATED_TOKEN_FILE"):
            login_workload_identity(
                self.commands,
                self.state,
                self.config.coordinator_identity["clientId"],
                self.config.foundation["tenantId"],
            )

    def rad(self, slot: str, *args: str, workspace: bool = True, timeout: int | None = None):
        context, kubeconfig = self.paths(slot)
        command = ["rad", "--config", str(self.radius_config), *args]
        if workspace:
            command += ["--workspace", context]
        return self.commands.run(
            command,
            env=self.commands.radius_environment(kubeconfig, context),
            **({"timeout": timeout} if timeout is not None else {}),
        )

    def kubectl(
        self,
        slot: str,
        *args: str,
        stdin: str | None = None,
        timeout: int | None = None,
    ):
        context, kubeconfig = self.paths(slot)
        return self.commands.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "--context",
                context,
                *([f"--request-timeout={timeout}s"] if timeout is not None else []),
                *args,
            ],
            stdin=stdin,
            **({"timeout": timeout} if timeout is not None else {}),
        )

    def kube_get(
        self,
        slot: str,
        namespace: str,
        kind: str,
        name: str,
        *,
        timeout: int | None = None,
    ):
        output = self.kubectl(
            slot,
            "-n",
            namespace,
            "get",
            kind,
            name,
            "--ignore-not-found",
            "-o",
            "json",
            **({"timeout": timeout} if timeout is not None else {}),
        )
        return json.loads(output) if output else None

    def apply(
        self,
        slot: str,
        resources: dict | list,
        *,
        create: bool = False,
        timeout: int | None = None,
    ) -> None:
        payload = (
            {"apiVersion": "v1", "kind": "List", "items": resources}
            if isinstance(resources, list)
            else resources
        )
        self.kubectl(
            slot,
            "create" if create else "apply",
            "-f",
            "-",
            stdin=json.dumps(payload),
            **({"timeout": timeout} if timeout is not None else {}),
        )

    def verify_recipes(self) -> None:
        if self.config.identity:
            self.verify_registry_policy()
        for kind, recipe in self.config.recipes.items():
            if self.config.identity and (
                recipe.get("immutability") != "acr-abac-arm-import-v1"
                or not re.fullmatch(
                    re.escape(
                        f"{self.config.foundation['registryLoginServer']}/radius-recipes/{kind}:src-"
                    )
                    + r"[a-f0-9]{64}",
                    recipe["reference"],
                )
            ):
                raise ProvisioningError("recipe_publication_policy_mismatch")
            image = recipe["reference"].split("/", 1)[1]
            actual = self.az(
                "acr",
                "repository",
                "show",
                "--name",
                self.config.foundation["registryName"],
                "--image",
                image,
            )
            attributes = actual.get("changeableAttributes", {})
            if actual["digest"] != recipe["digest"] or (
                not self.config.identity
                and (
                    attributes.get("writeEnabled") is not False
                    or attributes.get("deleteEnabled") is not False
                )
            ):
                raise ProvisioningError("recipe_digest_or_lock_mismatch")
        self._verified = True

    def verify_registry_policy(self) -> None:
        identity = self.config.identity
        if identity is None:
            raise ProvisioningError("bootstrap_identity_required")
        registry = self.az("acr", "show", "--name", identity.registry_name)
        expected = (
            f"/subscriptions/{identity.subscription}/resourceGroups/rg-{identity.stem}-platform"
            f"/providers/Microsoft.ContainerRegistry/registries/{identity.registry_name}"
        )
        if (
            not same_id(registry.get("id"), expected)
            or registry.get("roleAssignmentMode") != "AbacRepositoryPermissions"
            or registry.get("adminUserEnabled") is not False
            or registry.get("anonymousPullEnabled") is True
            or registry.get("loginServer") != f"{identity.registry_name}.azurecr.io"
            or registry.get("tags", {}).get("project") != identity.project
            or registry.get("tags", {}).get("deployment") != identity.deployment
        ):
            raise ProvisioningError("registry_publication_policy_mismatch")
        assignments = self.az(
            "role",
            "assignment",
            "list",
            "--scope",
            expected,
            "--include-inherited",
            "--fill-principal-name",
            "false",
            "--fill-role-definition-name",
            "false",
        )
        try:
            role_ids = sorted({item["roleDefinitionId"].rsplit("/", 1)[-1] for item in assignments})
            definitions = []
            for role_id in role_ids:
                UUID(role_id)
                values = self.az(
                    "role", "definition", "list", "--scope", expected, "--name", role_id
                )
                if len(values) != 1 or values[0]["name"].casefold() != role_id.casefold():
                    raise ValueError
                definitions += values
        except (ValueError, KeyError, TypeError):
            raise ProvisioningError("registry_permission_records_invalid") from None
        assignment_path = self.state / "registry-assignments.json"
        definition_path = self.state / "registry-definitions.json"
        write_json(assignment_path, assignments)
        write_json(definition_path, definitions)
        self.commands.run(
            [
                sys.executable,
                str(self.root / "scripts/operations/azure/registry_policy.py"),
                "--assignments",
                str(assignment_path),
                "--definitions",
                str(definition_path),
            ]
        )

    def get_access(self, slot: str) -> Cluster:
        allocation = self.config.allocation(slot)
        context, kubeconfig = self.paths(slot)
        self.login_workload()
        if kubeconfig.is_symlink():
            raise ProvisioningError("invalid_kubeconfig_path")
        self.commands.run(
            [
                "az",
                "aks",
                "get-credentials",
                "--subscription",
                self.config.foundation["subscriptionId"],
                "--resource-group",
                allocation["clusterResourceGroup"],
                "--name",
                allocation["clusterName"],
                "--context",
                context,
                "--file",
                str(kubeconfig),
                "--overwrite-existing",
                "--only-show-errors",
            ]
        )
        kubeconfig.chmod(0o600)
        self.commands.run(
            [
                "kubelogin",
                "convert-kubeconfig",
                "--kubeconfig",
                str(kubeconfig),
                "--context",
                context,
                "--login",
                "azurecli",
            ]
        )
        self.kubectl(slot, "get", "nodes", "-o", "name")
        return Cluster(
            slot,
            f"{allocation['clusterResourceGroupId']}/providers/Microsoft.ContainerService/"
            f"managedClusters/{allocation['clusterName']}",
            context,
            kubeconfig,
        )

    def inspect_pair(self, pair_id: str) -> PairResult:
        return workloads.inspect_pair(self, pair_id, self.radius_scope)

    def connect_management(self) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not re.fullmatch(r"[a-zA-Z0-9.:_-]+", host) or not port.isdecimal():
            raise ProvisioningError("in_cluster_management_access_required")
        service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        ca = service_account / "ca.crt"
        token = service_account / "token"
        if not ca.is_file() or not token.is_file():
            raise ProvisioningError("management_service_account_missing")
        context, kubeconfig = self.paths("management")
        if ":" in host:
            host = f"[{host}]"
        write_json(
            kubeconfig,
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": context,
                "clusters": [
                    {
                        "name": "management",
                        "cluster": {
                            "server": f"https://{host}:{port}",
                            "certificate-authority": str(ca),
                        },
                    }
                ],
                "users": [{"name": "provisioner", "user": {"tokenFile": str(token)}}],
                "contexts": [
                    {
                        "name": context,
                        "context": {
                            "cluster": "management",
                            "user": "provisioner",
                            "namespace": "radius-system",
                        },
                    }
                ],
            },
        )
        scope, environment = self.seed_management_workspace()
        for arguments, expected in (
            (("group", "show", self.config.radius_group), scope),
            (
                ("environment", "show", "management", "--group", self.config.radius_group),
                environment,
            ),
        ):
            output = self.rad("management", *arguments, "--output", "json")
            try:
                actual = json.loads(output)["id"]
                if not isinstance(actual, str):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise ProvisioningError("invalid_radius_output") from None
            if actual.rstrip("/").casefold() != expected.casefold():
                raise ProvisioningError("management_radius_mismatch")

    def seed_management_workspace(self) -> tuple[str, str]:
        scope = self.radius_scope
        context, _ = self.paths("management")
        environment = f"{scope}/providers/Applications.Core/environments/management"
        if self.radius_config.is_symlink():
            raise ProvisioningError("invalid_radius_config")
        try:
            config = (
                yaml.safe_load(self.radius_config.read_text())
                if self.radius_config.exists()
                else {}
            )
            if config is None:
                config = {}
            if not isinstance(config, dict):
                raise ValueError
            workspaces = config.setdefault("workspaces", {})
            if not isinstance(workspaces, dict):
                raise ValueError
            items = workspaces.setdefault("items", {})
            if not isinstance(items, dict):
                raise ValueError
        except (ValueError, yaml.YAMLError):
            raise ProvisioningError("invalid_radius_config") from None
        items[context] = {
            "connection": {"context": context, "kind": "kubernetes"},
            "scope": scope,
            "environment": environment,
        }
        workspaces["default"] = context
        write_json(self.radius_config, config)
        return scope, environment

    def recipe_map(self, slot: str) -> dict:
        allocation = self.config.allocation(slot)
        foundation = self.config.foundation
        if not slot.endswith("-data") and (
            not foundation.get("postgresSkuName") or not foundation.get("postgresSkuTier")
        ):
            raise ProvisioningError("postgresql_selection_missing")
        common = {"location": foundation["location"], "tags": plain(foundation["tags"])}
        parameters = {
            "postgresql": {
                **common,
                "skuName": foundation.get("postgresSkuName"),
                "skuTier": foundation.get("postgresSkuTier"),
                "delegatedSubnetId": allocation["postgresqlSubnetId"],
                "privateDnsZoneId": foundation["postgresqlDnsZoneId"],
            },
            "redis": {
                **common,
                "privateEndpointSubnetId": allocation["privateEndpointSubnetId"],
                "privateDnsZoneId": foundation["redisDnsZoneId"],
            },
            "gateway": {
                **common,
                **{
                    key: allocation[key]
                    for key in (
                        "gatewaySubnetId",
                        "gatewaySubnetCidr",
                        "nodeSubnetName",
                        "apiPrivateIp",
                        "challengePrivateIp",
                    )
                },
                "gatewayIdentityId": allocation["identities"]["gateway"]["id"],
            },
        }
        names = ["gateway", "redis" if slot.endswith("-data") else "postgresql"]
        if slot == "management" and self.config.identity:
            names = ["gateway", "postgresql", "redis", "cluster"]
            parameters["cluster"] = {
                "allocations": {
                    name: plain(value)
                    for name, value in self.config.allocations.items()
                    if name != "management"
                },
                "location": foundation["location"],
                "tenantId": foundation["tenantId"],
                "tags": plain(foundation["tags"]),
                **{
                    key: plain(foundation[key])
                    for key in (
                        "kubernetesVersion",
                        "nodeVmSize",
                        "nodeCount",
                        "authorizedIpRanges",
                    )
                },
            }
        return {
            TYPES[name][0]: {
                "default": {
                    "templateKind": "bicep",
                    "templatePath": self.config.recipes[name]["reference"],
                    "parameters": parameters[name],
                }
            }
            for name in names
        }

    def register_cluster_environment(self, slot: str) -> str:
        allocation = self.config.allocation(slot)
        if slot == "management":
            raise ProvisioningError("management_is_bootstrap_owned")
        if not self._verified:
            self.verify_recipes()
        foundation = self.config.foundation
        name = f"provision-{slot}"
        parameters = {
            "environmentName": name,
            "namespace": (
                provisioning_namespace(self.config.resource_prefix, slot)
                if self.config.identity
                else f"radplanes-p-{slot}"
            ),
            "azureSubscriptionId": foundation["subscriptionId"],
            "azureResourceGroup": allocation["clusterResourceGroup"],
            "registryHost": foundation["registryLoginServer"],
            "radiusClientId": self.config.allocations["management"]["identities"]["radius"][
                "clientId"
            ],
            "azureTenantId": foundation["tenantId"],
            "recipes": {
                TYPES["cluster"][0]: {
                    "default": {
                        "templateKind": "bicep",
                        "templatePath": self.config.recipes["cluster"]["reference"],
                        "parameters": {
                            "allocations": {slot: plain(allocation)},
                            "location": foundation["location"],
                            "tenantId": foundation["tenantId"],
                            "tags": plain(foundation["tags"]),
                            **{
                                key: plain(foundation[key])
                                for key in (
                                    "kubernetesVersion",
                                    "nodeVmSize",
                                    "nodeCount",
                                    "authorizedIpRanges",
                                )
                            },
                        },
                    }
                },
            },
        }
        parameter_file = self.parameters(slot, "cluster-environment", parameters)
        self.rad(
            "management",
            "deploy",
            str(self.root / "infra/radius/environments/azure.bicep"),
            "--group",
            self.config.radius_group,
            "--parameters",
            f"@{parameter_file}",
        )
        return name

    def register(self, slot: str) -> None:
        allocation = self.config.allocation(slot)
        self.verify_recipes()
        context, _ = self.paths(slot)
        self.rad(
            slot,
            "workspace",
            "create",
            "kubernetes",
            context,
            "--context",
            context,
            "--force",
            workspace=False,
        )
        self.rad(slot, "group", "create", self.config.radius_group)
        aliases = ["gateways", "postgresql"]
        if slot == "management":
            aliases.append("clusters")
        for alias in aliases:
            self.rad(
                slot,
                "resource-type",
                "create",
                "--from-file",
                str(self.root / "infra/radius/types" / f"{alias}.yaml"),
            )
        identity = allocation["identities"]["radius"]
        self.rad(
            slot,
            "credential",
            "register",
            "azure",
            "wi",
            "--client-id",
            identity["clientId"],
            "--tenant-id",
            self.config.foundation["tenantId"],
        )
        parameters = {
            "environmentName": slot,
            "namespace": f"{self.config.resource_prefix}-{slot}",
            "azureSubscriptionId": self.config.foundation["subscriptionId"],
            "azureResourceGroup": allocation["appResourceGroup"],
            "registryHost": self.config.foundation["registryLoginServer"],
            "radiusClientId": identity["clientId"],
            "azureTenantId": self.config.foundation["tenantId"],
            "recipes": self.recipe_map(slot),
        }
        parameter_file = self.parameters(slot, "environment", parameters)
        self.rad(
            slot,
            "deploy",
            str(self.root / "infra/radius/environments/azure.bicep"),
            "--group",
            self.config.radius_group,
            "--parameters",
            f"@{parameter_file}",
        )
        self.rad(
            slot,
            "workspace",
            "create",
            "kubernetes",
            context,
            "--context",
            context,
            "--group",
            self.config.radius_group,
            "--environment",
            slot,
            "--force",
            workspace=False,
        )

    def parameters(self, slot: str, name: str, values: dict) -> Path:
        path = self.state / f"{slot}-{name}.parameters.json"
        write_json(
            path,
            {
                "$schema": "https://schema.management.azure.com/schemas/"
                "2019-04-01/deploymentParameters.json#",
                "contentVersion": "1.0.0.0",
                "parameters": {key: {"value": value} for key, value in values.items()},
            },
        )
        return path

    def deploy(
        self,
        slot: str,
        template: str,
        application: str,
        values: dict,
        *,
        environment: str | None = None,
    ) -> None:
        if not self._verified:
            self.verify_recipes()
        path = self.parameters(slot, template, values)
        directory = "apps" if template in {"management", "control", "data"} else "modules"
        self.rad(
            slot,
            "deploy",
            str(self.root / "infra/radius" / directory / f"{template}.bicep"),
            "--group",
            self.config.radius_group,
            "--environment",
            environment or slot,
            "--application",
            application,
            "--parameters",
            f"@{path}",
        )

    def resource(
        self,
        slot: str,
        kind: str,
        name: str,
        application: str,
        *,
        timeout: int | None = None,
    ) -> dict:
        output = self.rad(
            slot,
            "resource",
            "show",
            TYPES[kind][0],
            name,
            "--group",
            self.config.radius_group,
            "--application",
            application,
            "--output",
            "json",
            **({"timeout": timeout} if timeout is not None else {}),
        )
        try:
            properties = json.loads(output)["properties"]
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ProvisioningError("invalid_radius_output") from None
        if properties.get("provisioningState", "Succeeded").lower() != "succeeded":
            raise ProvisioningError("radius_resource_not_ready")
        return properties

    def ensure_child_cluster(self, slot: str) -> Cluster:
        allocation = self.config.allocation(slot)
        if slot == "management":
            raise ProvisioningError("management_is_bootstrap_owned")
        environment = self.register_cluster_environment(slot)
        application = f"cluster-{slot}"
        self.deploy(
            "management",
            "child-cluster",
            application,
            {"slot": slot},
            environment=environment,
        )
        properties = self.resource("management", "cluster", slot, application)
        expected = (
            f"{allocation['clusterResourceGroupId']}/providers/Microsoft.ContainerService/"
            f"managedClusters/{allocation['clusterName']}"
        )
        if (
            properties["clusterId"] != expected
            or properties["clusterName"] != allocation["clusterName"]
            or properties["resourceGroup"] != allocation["clusterResourceGroup"]
            or properties["radiusClientId"] != allocation["identities"]["radius"]["clientId"]
        ):
            raise ProvisioningError("cluster_output_mismatch")
        return self.get_access(slot)

    def bootstrap_child(self, cluster: Cluster) -> None:
        allocation = self.config.allocation(cluster.slot)
        self.commands.run(
            [
                "bash",
                str(self.root / "scripts/operations/install-radius.sh"),
                "--context",
                cluster.context,
                "--kubeconfig",
                str(cluster.kubeconfig),
                "--config",
                str(self.radius_config),
                "--client-id",
                allocation["identities"]["radius"]["clientId"],
                "--tenant-id",
                self.config.foundation["tenantId"],
                "--workspace-root",
                str(self.state),
            ]
        )
        self.register(cluster.slot)

    def names(self, slot: str) -> tuple[str, str]:
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        return role, self.config.namespace(slot)

    def secret(
        self, slot: str, namespace: str, name: str, values: dict, *, create: bool = False
    ) -> None:
        self.commands.protect(values)
        self.apply(
            slot,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace},
                "type": "Opaque",
                "stringData": values,
            },
            create=create,
        )

    def prerequisites(self, slot: str) -> None:
        role, namespace = self.names(slot)
        self.apply(
            slot,
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "labels": workloads.ownership_labels(self.config),
                },
            },
        )
        accounts = [f"{role}-api", "challenge", "database-init"]
        if role == "management":
            accounts.append("provisioner")
        else:
            accounts.append(f"{role}-reconciler")
        if role == "data":
            accounts.append("data-api-runtime")
        resources = []
        for account in accounts:
            metadata = {"name": account, "namespace": namespace}
            if account == "provisioner":
                metadata["annotations"] = {
                    "azure.workload.identity/client-id": self.config.coordinator_identity[
                        "clientId"
                    ],
                    "azure.workload.identity/tenant-id": self.config.foundation["tenantId"],
                }
            resources.append(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": metadata,
                    "automountServiceAccountToken": account
                    in {"provisioner", "data-api-runtime", "data-reconciler"},
                }
            )
        if not self.kube_get(slot, namespace, "configmap", "acme-challenges"):
            resources.append(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "acme-challenges", "namespace": namespace},
                    "data": {},
                }
            )
        if role == "data":
            for account, verbs in (
                ("data-api", ["get"]),
                ("data-reconciler", ["get", "create", "patch"]),
            ):
                resources += self.role_binding(
                    namespace,
                    account + "-configmaps",
                    namespace,
                    "data-api-runtime" if account == "data-api" else account,
                    [{"apiGroups": [""], "resources": ["configmaps"], "verbs": verbs}],
                )
        if role == "management":
            resources += self.management_permissions(namespace)
            resources += workloads.management_discovery_permissions(namespace)
            runtime_config = self.config.to_dict()
            runtime_config["certificateCommand"] = list(
                self.config.certificate_command or CONTAINER_CERTIFICATE_COMMAND
            )
            resources += [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "provisioning-settings", "namespace": namespace},
                    "immutable": True,
                    "data": (
                        self.config.bootstrap_settings
                        if self.config.identity
                        else {"provisioning.json": json.dumps(runtime_config)}
                    ),
                },
            ]
        self.apply(slot, resources)

    @staticmethod
    def role_binding(namespace, name, subject_namespace, subject_name, rules):
        return workloads.role_binding(namespace, name, subject_namespace, subject_name, rules)

    def management_permissions(self, namespace: str) -> list:
        namespaced = AzureProvider.role_binding(
            "radius-system",
            "plane-provisioner",
            namespace,
            "provisioner",
            [
                {
                    "apiGroups": [""],
                    "resources": ["pods", "services", "endpoints"],
                    "verbs": ["get", "list"],
                },
                {"apiGroups": [""], "resources": ["pods/portforward"], "verbs": ["create"]},
                {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list"]},
            ],
        )
        return namespaced + [
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {
                    "name": "radplanes-provisioner-radius-api",
                    "labels": {"project": self.config.project_name},
                },
                "rules": [
                    {
                        "apiGroups": ["api.ucp.dev"],
                        "resources": ["planes/local"],
                        "resourceNames": ["radius"],
                        "verbs": ["get", "list", "create", "update", "delete"],
                    },
                    *(
                        [
                            {
                                "apiGroups": [""],
                                "resources": ["namespaces"],
                                "resourceNames": [namespace],
                                "verbs": ["get"],
                            }
                        ]
                        if self.config.identity
                        else []
                    ),
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {
                    "name": "radplanes-provisioner-radius-api",
                    "labels": {"project": self.config.project_name},
                },
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": "radplanes-provisioner-radius-api",
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": "provisioner", "namespace": namespace}
                ],
            },
        ]

    def initialize_database(self, slot: str) -> None:
        workloads.initialize_database(self, slot)

    def database_resource_exists(self, slot: str) -> bool:
        return self.resource_exists(slot, "postgresql", "postgres")

    def resource_exists(self, slot: str, kind: str, name: str) -> bool:
        output = self.rad(
            slot,
            "resource",
            "list",
            TYPES[kind][0],
            "--group",
            self.config.radius_group,
            "--output",
            "json",
        )
        try:
            resources = json.loads(output)
            if isinstance(resources, dict):
                resources = resources["value"]
            if not isinstance(resources, list) or any(
                not isinstance(resource, dict) or not isinstance(resource.get("name"), str)
                for resource in resources
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ProvisioningError("invalid_radius_output") from None
        return any(resource["name"].casefold() == name.casefold() for resource in resources)

    def cleanup_initialization(self, slot: str, namespace: str, setup_name: str | None) -> None:
        resources = ["job/database-init", "secret/database-init"]
        if setup_name:
            if not re.fullmatch(r"[a-z0-9-]+-setup", setup_name):
                raise ProvisioningError("invalid_setup_secret_name")
            resources.append(f"secret/{setup_name}")
        self.kubectl(
            slot,
            "-n",
            namespace,
            "delete",
            *resources,
            "--wait=true",
            "--ignore-not-found",
        )

    def job(self, namespace: str, name: str, image: str, command: list[str], account: str) -> dict:
        return workloads.job(
            namespace, name, image, command, account, project_name=self.config.project_name
        )

    def runtime_secrets(self, slot: str) -> None:
        workloads.runtime_secrets(self, slot)

    def certificate(self, slot: str, domain: str) -> str:
        allocation = self.config.allocation(slot)
        endpoint(f"https://{domain}")
        _, application_namespace = self.names(slot)
        context, kubeconfig = self.paths(slot)
        if self.kube_get(
            slot, f"{self.config.resource_prefix}-system", "job", f"certificate-{slot}"
        ):
            raise ProvisioningError("certificate_job_incomplete")
        default = (
            CONTAINER_CERTIFICATE_COMMAND
            if self.root == Path("/app")
            else (sys.executable, str(self.root / "scripts/operations/run-certificate-job.py"))
        )
        output = self.commands.run(
            [
                *(self.config.certificate_command or default),
                "--slot",
                slot,
                "--context",
                context,
                "--namespace",
                application_namespace,
                "--kubeconfig",
                str(kubeconfig),
                "--domain",
                domain,
                "--config",
                str(self.config_path),
            ]
        )
        if not output or len(output) > 4096:
            raise ProvisioningError("invalid_certificate_result")
        try:
            result = json.loads(output)
            if set(result) != {"certificateSecretUri"}:
                raise ValueError
            uri = result["certificateSecretUri"]
        except (TypeError, ValueError):
            raise ProvisioningError("invalid_certificate_result") from None
        expected = (
            f"https://{self.config.foundation['vaultName']}.vault.azure.net/secrets/"
            f"{allocation['certificateName']}"
        )
        if not isinstance(uri, str) or uri not in (expected, expected + "/"):
            raise ProvisioningError("invalid_certificate_uri")
        return expected

    def tag_redis_nic(
        self,
        slot: str,
        *,
        resource_name: str = "redis",
        application: str = "data",
        environment: str | None = None,
    ) -> dict:
        allocation = self.config.allocation(slot)
        foundation = self.config.foundation
        scope = f"{self.radius_scope}/providers/"
        target_data = {
            "slot": slot,
            "subscription_id": foundation["subscriptionId"],
            "tenant_id": foundation["tenantId"],
            "client_id": allocation["identities"]["radius"]["clientId"],
            "resource_group": allocation["appResourceGroup"],
            "subnet_id": allocation["privateEndpointSubnetId"],
            "location": foundation["location"],
            "resource_id": scope + f"Applications.Datastores/redisCaches/{resource_name}",
            "environment_id": scope + f"Applications.Core/environments/{environment or slot}",
            "application_id": scope + f"Applications.Core/applications/{application}",
            "tags": {
                **plain(foundation["tags"]),
                **BASE_TAGS,
                "project": self.config.project_name,
            },
            **(
                {
                    "project_name": self.config.project_name,
                    "resource_prefix": self.config.resource_prefix,
                }
                if self.config.identity
                else {}
            ),
        }
        target = Target.parse(target_data)
        properties = self.resource(slot, "redis", resource_name, application, timeout=30)
        if (
            properties.get("provisioningState") != "Succeeded"
            or not same_id(properties.get("environment"), target.environment_id)
            or not same_id(properties.get("application"), target.application_id)
        ):
            raise ProvisioningError("redis_nic_radius_not_ready")
        namespace, account, job_name = "radius-system", "applications-rp", "redis-nic-tags"
        service_account = self.kube_get(slot, namespace, "serviceaccount", account, timeout=15)
        if not isinstance(service_account, dict) or not isinstance(
            service_account.get("metadata"), dict
        ):
            raise ProvisioningError("redis_nic_identity_mismatch")
        metadata = service_account["metadata"]
        annotations = metadata.get("annotations", {})
        if (
            not isinstance(annotations, dict)
            or metadata.get("name") != account
            or metadata.get("namespace") != namespace
            or annotations.get("azure.workload.identity/client-id") != target.client_id
            or annotations.get("azure.workload.identity/tenant-id") != target.tenant_id
        ):
            raise ProvisioningError("redis_nic_identity_mismatch")
        if self.kube_get(slot, namespace, "job", job_name, timeout=15):
            raise ProvisioningError("redis_nic_job_exists")
        job = self.job(
            namespace,
            job_name,
            self.config.images["provisioner"],
            ["python", "-m", "plane_demo.management.providers.redis_nic_tags"],
            account,
        )
        job["spec"]["activeDeadlineSeconds"] = 180
        template = job["spec"]["template"]
        template["metadata"]["labels"].update(
            {
                "azure.workload.identity/use": "true",
                "plane-demo/slot": slot,
            }
        )
        template["spec"]["containers"][0].update(
            {
                "terminationMessagePolicy": "File",
                "env": [
                    {"name": "REDIS_NIC_TAG_TARGET", "value": json.dumps(target_data)},
                    {
                        "name": "POD_NAMESPACE",
                        "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                    },
                    {
                        "name": "POD_SERVICE_ACCOUNT",
                        "valueFrom": {"fieldRef": {"fieldPath": "spec.serviceAccountName"}},
                    },
                ],
            }
        )
        self.apply(slot, job, create=True, timeout=15)
        deadline, uid = time.monotonic() + 200, None
        while time.monotonic() < deadline:
            current = self.kube_get(slot, namespace, "job", job_name, timeout=15)
            if time.monotonic() >= deadline:
                raise ProvisioningError("redis_nic_timeout")
            if not current or not current.get("metadata", {}).get("uid"):
                raise ProvisioningError("redis_nic_job_missing")
            current_uid = current["metadata"]["uid"]
            if uid is not None and current_uid != uid:
                raise ProvisioningError("redis_nic_job_replaced")
            uid = current_uid
            status = current.get("status", {})
            if status.get("succeeded") == 1 or status.get("failed"):
                break
            time.sleep(2)
        else:
            raise ProvisioningError("redis_nic_timeout")
        pods = json.loads(
            self.kubectl(
                slot,
                "-n",
                namespace,
                "get",
                "pods",
                "-l",
                f"job-name={job_name}",
                "-o",
                "json",
                timeout=15,
            )
        )
        results = []
        for pod in pods["items"]:
            if not any(
                owner.get("uid") == uid
                and owner.get("kind") == "Job"
                and owner.get("name") == job_name
                and owner.get("controller") is True
                for owner in pod["metadata"].get("ownerReferences", [])
            ):
                continue
            for container in pod.get("status", {}).get("containerStatuses", []):
                terminated = container.get("state", {}).get("terminated", {})
                if container.get("name") == job_name and terminated:
                    if status.get("succeeded") == 1 and (
                        pod.get("status", {}).get("phase") != "Succeeded"
                        or terminated.get("exitCode") != 0
                    ):
                        raise ProvisioningError("redis_nic_job_output_invalid")
                    try:
                        results.append(json.loads(terminated.get("message", "")))
                    except ValueError:
                        raise ProvisioningError("redis_nic_job_output_invalid") from None
        if len(results) != 1 or not isinstance(results[0], dict):
            raise ProvisioningError(
                "redis_nic_job_failed"
                if status.get("succeeded") != 1
                else "redis_nic_job_output_invalid"
            )
        result = results[0]
        if status.get("succeeded") != 1:
            code = result.get("error_code")
            raise ProvisioningError(
                code if isinstance(code, str) and code in ERRORS else "redis_nic_job_failed"
            )
        if set(result) != {"cacheId", "privateEndpointId", "nicId"}:
            raise ProvisioningError("redis_nic_job_output_invalid")
        target.owned_id(result["cacheId"], "Microsoft.Cache/redisEnterprise")
        target.owned_id(result["privateEndpointId"], "Microsoft.Network/privateEndpoints")
        target.owned_id(result["nicId"], "Microsoft.Network/networkInterfaces")
        cache_name = result["cacheId"].rsplit("/", 1)[1]
        if (
            not re.fullmatch(r"amr-[a-z0-9]{13}", cache_name)
            or not same_id(
                result["privateEndpointId"],
                f"{target.group_id}/providers/Microsoft.Network/privateEndpoints/pe-{cache_name}",
            )
            or not same_id(
                result["nicId"],
                f"{target.group_id}/providers/Microsoft.Network/networkInterfaces/nic-{cache_name}",
            )
        ):
            raise ProvisioningError("redis_nic_job_output_invalid")
        self.kubectl(
            slot,
            "-n",
            namespace,
            "delete",
            f"job/{job_name}",
            "--cascade=foreground",
            "--wait=true",
            timeout=15,
        )
        return result

    def current_certificate(self, slot: str) -> str | None:
        if not self.resource_exists(slot, "gateway", "gateway"):
            return None
        role, _ = self.names(slot)
        properties = self.resource(slot, "gateway", "gateway", role)
        owners = f"{self.radius_scope}/providers/Applications.Core"
        if (
            properties.get("provisioningState") != "Succeeded"
            or not same_id(properties.get("application"), f"{owners}/applications/{role}")
            or not same_id(properties.get("environment"), f"{owners}/environments/{slot}")
        ):
            raise ProvisioningError("gateway_owner_mismatch")
        uri = properties.get("certificateSecretUri")
        url = endpoint(properties["url"], https=bool(uri))
        if url != f"{'https' if uri else 'http'}://{properties.get('host')}":
            raise ProvisioningError("invalid_gateway_output")
        if not uri:
            return None
        expected = (
            f"https://{self.config.foundation['vaultName']}.vault.azure.net/secrets/"
            f"{self.config.allocation(slot)['certificateName']}"
        )
        if uri != expected:
            raise ProvisioningError("gateway_certificate_mismatch")
        return uri

    def deploy_plane(self, slot: str, observe: Callable[[str], None] = lambda _: None) -> str:
        self.config.allocation(slot)
        role, namespace = self.names(slot)
        previous = self.current_certificate(slot)
        observe(f"{role}-database" if role != "data" else "data-credentials")
        self.prerequisites(slot)
        if role != "data":
            self.initialize_database(slot)
        self.runtime_secrets(slot)
        values = {
            "image": self.config.images["api"],
            "ownershipLabels": workloads.ownership_labels(self.config),
        }
        if role == "management":
            values.update(
                provisionerImage=self.config.images["provisioner"],
                provisionerWorkloadIdentity=True,
                provisionerClientId=self.config.coordinator_identity["clientId"],
            )
        if previous:
            values.update(gatewayPhase="https", certificateSecretUri=previous)
        observe(f"{role}-application")
        self.deploy(slot, role, role, values)
        if role == "data":
            observe("data-redis-metadata")
            self.tag_redis_nic(slot)
        self.kubectl(
            slot,
            "-n",
            namespace,
            "rollout",
            "status",
            "deployment",
            "--selector",
            f"radapp.io/application={role}",
            "--timeout=600s",
        )
        properties = self.resource(slot, "gateway", "gateway", role)
        host = properties["host"]
        endpoint(properties["url"], https=bool(previous))
        observe(f"{role}-certificate")
        uri = self.certificate(slot, host)
        values.update(gatewayPhase="https", certificateSecretUri=uri)
        self.deploy(slot, role, role, values)
        if role == "data":
            observe("data-redis-metadata")
            self.tag_redis_nic(slot)
        properties = self.resource(slot, "gateway", "gateway", role)
        url = endpoint(properties["url"])
        if properties["host"] != host or properties.get("certificateSecretUri") != uri:
            raise ProvisioningError("gateway_certificate_mismatch")
        # Administrative liveness only; control reports tenant creation independently.
        self.commands.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                f"{url}/livez",
            ],
            timeout=40,
        )
        self.record_endpoint(slot, url, uri)
        return url

    def record_endpoint(self, slot: str, url: str, certificate_uri: str) -> None:
        write_json(
            self.state / f"{slot}-endpoint.json",
            {
                "url": url,
                "certificateSecretUri": certificate_uri,
            },
        )
        key_file = f"{slot}.key"
        write_private(self.state / key_file, self.credentials.demo_key(slot) + "\n")
        path = self.state / "endpoints.json"
        inventory = json.loads(path.read_text()) if path.exists() else {"pairs": {}}
        selected = {"url": url, "key_file": key_file}
        role, _ = self.names(slot)
        if role == "management":
            inventory["management"] = selected
        else:
            pair = slot.removesuffix(f"-{role}")
            inventory["pairs"].setdefault(pair, {})[role] = selected
        write_json(path, inventory)
