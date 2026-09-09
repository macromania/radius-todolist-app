"""Azure administrative orchestration. Only Radius submits child cluster creation."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

from plane_demo.management.providers.commands import (
    Commands,
    create_json,
    write_json,
    write_private,
)
from plane_demo.management.providers.credentials import Credentials, database_dsn
from plane_demo.management.provisioning import (
    Cluster,
    OperatorConfig,
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
CONTAINER_CERTIFICATE_COMMAND = ("python", "/app/operations/run-certificate-job.py")


class AzureProvider:
    def __init__(
        self,
        config: OperatorConfig,
        root: Path,
        credentials: Credentials,
        commands: Commands | None = None,
    ):
        self.config = config
        self.root = root.resolve()
        self.state = self.root / ".state" / "azure"
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.chmod(0o700)
        self.credentials = credentials
        self.commands = commands or Commands(self.root)
        self.radius_config = self.state / "radius.yaml"
        self.config_path = self.state / "provisioning.json"
        write_json(self.config_path, config.to_dict())
        self.commands.protect(credentials._data)
        self._verified = False

    def paths(self, slot: str) -> tuple[str, Path]:
        self.config.allocation(slot)
        return f"radplanes-{slot}", self.state / f"{slot}.kubeconfig"

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
        token_path = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
        if token_path:
            identity = self.config.coordinator_identity["clientId"]
            tenant = self.config.foundation["tenantId"]
            if (
                os.environ.get("AZURE_CLIENT_ID") != identity
                or os.environ.get("AZURE_TENANT_ID") != tenant
            ):
                raise ProvisioningError("workload_identity_mismatch")
            token = Path(token_path).read_text().strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
                raise ProvisioningError("invalid_federated_token")
            self.commands.protect(token)
            az_config = self.state / "az"
            az_config.mkdir(mode=0o700, exist_ok=True)
            self.commands.environment["AZURE_CONFIG_DIR"] = str(az_config)
            self.commands.run(
                [
                    "az",
                    "login",
                    "--service-principal",
                    "--username",
                    identity,
                    "--tenant",
                    tenant,
                    "--federated-token",
                    token,
                    "--allow-no-subscriptions",
                    "--output",
                    "none",
                    "--only-show-errors",
                ],
                timeout=120,
            )

    def rad(self, slot: str, *args: str, workspace: bool = True):
        context, kubeconfig = self.paths(slot)
        command = ["rad", "--config", str(self.radius_config), *args]
        if workspace:
            command += ["--workspace", context]
        return self.commands.run(
            command,
            env=self.commands.radius_environment(kubeconfig, context),
        )

    def kubectl(self, slot: str, *args: str, stdin: str | None = None):
        context, kubeconfig = self.paths(slot)
        return self.commands.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "--context",
                context,
                *args,
            ],
            stdin=stdin,
        )

    def kube_get(self, slot: str, namespace: str, kind: str, name: str):
        output = self.kubectl(
            slot, "-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json"
        )
        return json.loads(output) if output else None

    def apply(self, slot: str, resources: dict | list, *, create: bool = False) -> None:
        payload = (
            {"apiVersion": "v1", "kind": "List", "items": resources}
            if isinstance(resources, list)
            else resources
        )
        self.kubectl(slot, "create" if create else "apply", "-f", "-", stdin=json.dumps(payload))

    def verify_recipes(self) -> None:
        for recipe in self.config.recipes.values():
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
            attributes = actual["changeableAttributes"]
            if (
                actual["digest"] != recipe["digest"]
                or attributes["writeEnabled"] is not False
                or attributes["deleteEnabled"] is not False
            ):
                raise ProvisioningError("recipe_digest_or_lock_mismatch")
        self._verified = True

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
        _, kubeconfig = self.paths("management")
        if ":" in host:
            host = f"[{host}]"
        write_json(
            kubeconfig,
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "radplanes-management",
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
                        "name": "radplanes-management",
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
            (("group", "show", "radplanes"), scope),
            (("environment", "show", "management", "--group", "radplanes"), environment),
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
        scope = "/planes/radius/local/resourceGroups/radplanes"
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
        items["radplanes-management"] = {
            "connection": {"context": "radplanes-management", "kind": "kubernetes"},
            "scope": scope,
            "environment": environment,
        }
        workspaces["default"] = "radplanes-management"
        write_json(self.radius_config, config)
        return scope, environment

    def recipe_map(self, slot: str) -> dict:
        allocation = self.config.allocation(slot)
        foundation = self.config.foundation
        common = {"location": foundation["location"], "tags": plain(foundation["tags"])}
        parameters = {
            "postgresql": {
                **common,
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
            "namespace": f"radplanes-p-{slot}",
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
            "radplanes",
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
        self.rad(slot, "group", "create", "radplanes")
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
            "namespace": f"radplanes-{slot}",
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
            "radplanes",
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
            "radplanes",
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
            "radplanes",
            "--environment",
            environment or slot,
            "--application",
            application,
            "--parameters",
            f"@{path}",
        )

    def resource(self, slot: str, kind: str, name: str, application: str) -> dict:
        output = self.rad(
            slot,
            "resource",
            "show",
            TYPES[kind][0],
            name,
            "--group",
            "radplanes",
            "--application",
            application,
            "--output",
            "json",
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
                sys.executable,
                str(self.root / "operations/install-radius.py"),
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
            ]
        )
        self.register(cluster.slot)

    @staticmethod
    def names(slot: str) -> tuple[str, str]:
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        return role, f"radplanes-{slot}-{role}"

    def secret(self, slot: str, namespace: str, name: str, values: dict) -> None:
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
                    "labels": {"plane-demo/project": "radplanes"},
                },
            },
        )
        accounts = [f"{role}-api", "challenge", "database-init"]
        if role == "management":
            accounts.append("provisioner")
        else:
            accounts.append(f"{role}-reconciler")
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
                    in {"provisioner", "data-api", "data-reconciler"},
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
                    account,
                    namespace,
                    account,
                    [{"apiGroups": [""], "resources": ["configmaps"], "verbs": verbs}],
                )
        if role == "management":
            resources += self.management_permissions(namespace)
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
                    "data": {"provisioning.json": json.dumps(runtime_config)},
                },
                {
                    "apiVersion": "storage.k8s.io/v1",
                    "kind": "StorageClass",
                    "metadata": {"name": "radplanes-provisioner"},
                    "provisioner": "disk.csi.azure.com",
                    "reclaimPolicy": "Delete",
                    "volumeBindingMode": "WaitForFirstConsumer",
                    "allowVolumeExpansion": True,
                    "parameters": {
                        "skuName": "StandardSSD_LRS",
                        "tags": "SecurityControl=Ignore,project=radplanes,"
                        "managedBy=radius-todolist-app",
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "provisioner-state", "namespace": namespace},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": "radplanes-provisioner",
                        "resources": {"requests": {"storage": "8Gi"}},
                    },
                },
            ]
        self.apply(slot, resources)

    @staticmethod
    def role_binding(namespace, name, subject_namespace, subject_name, rules):
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
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": name,
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": subject_name,
                        "namespace": subject_namespace,
                    }
                ],
            },
        ]

    def management_permissions(self, namespace: str) -> list:
        return self.role_binding(
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

    def initialize_database(self, slot: str) -> None:
        self.config.allocation(slot)
        role, namespace = self.names(slot)
        intent = self.state / f"{slot}-database-intent.json"
        roles = (
            {
                "mgmt_api",
                "mgmt_provisioner",
                *(item["reporting_role"] for item in self.config.pair_slots),
            }
            if role == "management"
            else {"cp_api", "cp_reconciler", "dp_reconciler"}
        )
        marker = self.kube_get(slot, namespace, "configmap", "database-initialized")
        if marker:
            plane = self.credentials.plane(slot)
            if {key: marker["data"][key] for key in ("serverId", "database")} != {
                "serverId": plane["database"]["serverId"],
                "database": plane["database"]["database"],
            }:
                raise ProvisioningError("database_marker_mismatch")
            self.cleanup_initialization(slot, namespace, marker["data"].get("setupSecretName"))
            return
        if (
            intent.exists()
            or intent.is_symlink()
            or self.kube_get(slot, namespace, "secret", "database-init")
            or self.kube_get(slot, namespace, "job", "database-init")
            or self.kube_get(slot, namespace, "secret", "postgres-setup")
            or self.credentials.has_database(slot)
            or self.database_resource_exists(slot)
        ):
            raise ProvisioningError("database_initialization_incomplete")
        plane = self.credentials.ensure(slot, roles)
        self.commands.protect(plane)
        try:
            create_json(
                intent,
                {
                    "version": 1,
                    "slot": slot,
                    "application": role,
                    "resourceType": TYPES["postgresql"][0],
                    "resourceName": "postgres",
                },
            )
        except FileExistsError:
            raise ProvisioningError("database_initialization_incomplete") from None
        self.deploy(slot, "database", role, {"databaseName": role})
        properties = self.resource(slot, "postgresql", "postgres", role)
        setup_name = properties.get("setupSecretName")
        if not isinstance(setup_name, str) or not re.fullmatch(r"[a-z0-9-]+-setup", setup_name):
            raise ProvisioningError("postgres_setup_contract_missing")
        setup = self.kube_get(slot, namespace, "secret", setup_name)
        if not setup:
            raise ProvisioningError("postgres_setup_secret_missing")
        password = base64.b64decode(setup["data"]["password"], validate=True).decode()
        self.commands.protect(password)
        dsn = database_dsn(properties, properties["username"], password)
        self.commands.protect(dsn)
        self.credentials.set_database(slot, properties)
        variables = {
            "BOOTSTRAP_DSN": dsn,
            "BOOTSTRAP_KIND": role,
            "ROLE_PASSWORDS_JSON": json.dumps(plane["passwords"]),
        }
        if role == "management":
            variables["PAIR_SLOTS_JSON"] = json.dumps(self.config.pair_slots)
        else:
            variables["PAIR_ID"] = slot.removesuffix("-control")
        self.secret(slot, namespace, "database-init", variables)
        job = self.job(
            namespace,
            "database-init",
            self.config.images["api"],
            ["python", "-m", "plane_demo.setup.bootstrap"],
            "database-init",
        )
        job["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
            {"secretRef": {"name": "database-init"}}
        ]
        self.apply(slot, job, create=True)
        try:
            self.kubectl(
                slot,
                "-n",
                namespace,
                "wait",
                "--for=condition=complete",
                "job/database-init",
                "--timeout=600s",
            )
        except ProvisioningError:
            logs = self.kubectl(slot, "-n", namespace, "logs", "job/database-init", "--tail=100")
            logger.error("database_initialization_failed %s", self.commands.redact(logs))
            raise
        self.apply(
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
        self.cleanup_initialization(slot, namespace, setup_name)

    def database_resource_exists(self, slot: str) -> bool:
        output = self.rad(
            slot,
            "resource",
            "list",
            TYPES["postgresql"][0],
            "--group",
            "radplanes",
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
        return any(resource["name"].casefold() == "postgres" for resource in resources)

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

    @staticmethod
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

    def runtime_secrets(self, slot: str) -> None:
        role, namespace = self.names(slot)
        if role == "management":
            self.secret(
                slot,
                namespace,
                "management-api-runtime",
                {
                    "MANAGEMENT_DSN": self.credentials.dsn(slot, "mgmt_api"),
                    "DEMO_KEY": self.credentials.plane(slot)["demoKey"],
                },
            )
            self.secret(
                slot,
                namespace,
                "provisioner-runtime",
                {
                    "MANAGEMENT_DSN": self.credentials.dsn(slot, "mgmt_provisioner"),
                    "PROVIDER": "azure",
                    "PROVISIONING_CONFIG": "/etc/plane-demo/provisioning.json",
                    "PROVISIONING_CREDENTIALS_JSON": json.dumps(
                        self.credentials.runtime_seed(self.config)
                    ),
                },
            )
        elif role == "control":
            pair = slot.removesuffix("-control")
            reporting_role = next(
                item["reporting_role"] for item in self.config.pair_slots if item["pair_id"] == pair
            )
            self.secret(
                slot,
                namespace,
                "control-api-runtime",
                {
                    "CONTROL_DSN": self.credentials.dsn(slot, "cp_api"),
                    "DEMO_KEY": self.credentials.plane(slot)["demoKey"],
                },
            )
            self.secret(
                slot,
                namespace,
                "control-reconciler-runtime",
                {
                    "CONTROL_DSN": self.credentials.dsn(slot, "cp_reconciler"),
                    "MANAGEMENT_DSN": self.credentials.dsn("management", reporting_role),
                    "PAIR_ID": pair,
                },
            )
        else:
            pair = slot.removesuffix("-data")
            plane = self.credentials.ensure(slot, set())
            common = {"PAIR_ID": pair, "PROJECT_ID": "radplanes", "KUBE_NAMESPACE": namespace}
            self.secret(
                slot,
                namespace,
                "data-api-runtime",
                {
                    **common,
                    "DEMO_KEY": plane["demoKey"],
                },
            )
            self.secret(
                slot,
                namespace,
                "data-reconciler-runtime",
                {
                    **common,
                    "CONTROL_DSN": self.credentials.dsn(f"{pair}-control", "dp_reconciler"),
                },
            )

    def certificate(self, slot: str, domain: str) -> str:
        allocation = self.config.allocation(slot)
        endpoint(f"https://{domain}")
        _, application_namespace = self.names(slot)
        context, kubeconfig = self.paths(slot)
        if self.kube_get(slot, "radplanes-system", "job", f"certificate-{slot}"):
            raise ProvisioningError("certificate_job_incomplete")
        default = (
            CONTAINER_CERTIFICATE_COMMAND
            if self.root == Path("/app")
            else (sys.executable, str(self.root / "operations/run-certificate-job.py"))
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

    def deploy_plane(self, slot: str, observe: Callable[[str], None] = lambda _: None) -> str:
        self.config.allocation(slot)
        role, namespace = self.names(slot)
        observe(f"{role}-database" if role != "data" else "data-credentials")
        self.prerequisites(slot)
        if role != "data":
            self.initialize_database(slot)
        self.runtime_secrets(slot)
        values = {"image": self.config.images["api"]}
        if role == "management":
            values.update(
                provisionerImage=self.config.images["provisioner"],
                provisionerWorkloadIdentity=True,
                provisionerClientId=self.config.coordinator_identity["clientId"],
            )
        existing = self.state / f"{slot}-certificate.json"
        # A healthy reapply keeps HTTPS; never downgrade a live gateway to challenge mode.
        previous = json.loads(existing.read_text()) if existing.exists() else None
        if previous:
            values.update(
                gatewayPhase="https", certificateSecretUri=previous["certificateSecretUri"]
            )
        observe(f"{role}-application")
        self.deploy(slot, role, role, values)
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
        write_json(existing, {"certificateSecretUri": uri})
        values.update(gatewayPhase="https", certificateSecretUri=uri)
        self.deploy(slot, role, role, values)
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
        write_private(self.state / key_file, self.credentials.plane(slot)["demoKey"] + "\n")
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
