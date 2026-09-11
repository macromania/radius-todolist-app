"""Local administrative provider. Management Radius alone creates child clusters."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path

import yaml

from plane_demo.management.providers import workloads
from plane_demo.management.providers.commands import (
    Commands,
    create_json,
    write_json,
    write_private,
)
from plane_demo.management.providers.credentials import Credentials
from plane_demo.management.providers.local_config import (
    ACCESS_NAMESPACE,
    GROUP,
    SCOPE,
    SLOTS,
    LocalConfig,
    private_ipv4,
    same_radius_id,
)
from plane_demo.management.provisioning import Cluster, ProvisioningError

TYPES = {
    "cluster": "Demo.Platform/clusters",
    "postgresql": "Demo.Platform/postgreSqlDatabases",
    "gateway": "Demo.Platform/gateways",
    "redis": "Applications.Datastores/redisCaches",
}
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
PYTHON_IMAGE = (
    "docker.io/library/python:3.13.12-alpine3.23@sha256:"
    "bb1f2fdb1065c85468775c9d680dcd344f6442a2d1181ef7916b60a623f11d40"
)
TERRAFORM_INIT = """
import hashlib,io,os,platform,urllib.request,zipfile
from pathlib import Path
arch={"aarch64":"arm64","x86_64":"amd64"}[platform.machine()]
expected={"arm64":"8891e9dcedc9e3b8950bc6af9d4d8af1f4cfade3062f53b9dc403a89f6ce8c9c",
          "amd64":"d25ce7b6902013ad905db3d2eab0be4cd905887fe88b81a6171b8d5503c31f3d"}[arch]
url=f"https://releases.hashicorp.com/terraform/1.15.8/terraform_1.15.8_linux_{arch}.zip"
with urllib.request.urlopen(url,timeout=90) as response:
    archive=response.read(40000001)
if len(archive)>40000000 or hashlib.sha256(archive).hexdigest()!=expected:
    raise RuntimeError("terraform_archive_mismatch")
binary=zipfile.ZipFile(io.BytesIO(archive)).read("terraform")
for name in ("/terraform","/terraform/.terraform-global"):
    Path(name).mkdir(exist_ok=True,mode=0o700)
    os.chown(name,0,0)
    os.chmod(name,0o700)
for name in ("/terraform/terraform","/terraform/.terraform-global/terraform"):
    if Path(name).exists():
        os.chown(name,0,0)
    Path(name).write_bytes(binary)
    os.chmod(name,0o700)
    os.chown(name,65532,65532)
marker=Path("/terraform/.terraform-global/.terraform-ready")
if marker.exists():
    os.chown(marker,0,0)
marker.touch(mode=0o600)
os.chmod(marker,0o600)
os.chown(marker,65532,65532)
for name in ("/terraform/.terraform-global","/terraform"):
    os.chown(name,65532,65532)
"""


def decode_access(value: str, context: str, *, child: bool) -> tuple[dict, bytes, str]:
    """Admit only one static, CA-verified context, without exec plugins or proxy overrides."""
    try:
        access = yaml.safe_load(value)
        if (
            access["apiVersion"] != "v1"
            or access["kind"] != "Config"
            or access["current-context"] != context
            or any(len(access[key]) != 1 for key in ("clusters", "users", "contexts"))
            or access["contexts"][0]["name"] != context
        ):
            raise ValueError
        cluster = access["clusters"][0]
        user = access["users"][0]
        binding = access["contexts"][0]["context"]
        if binding["cluster"] != cluster["name"] or binding["user"] != user["name"]:
            raise ValueError
        details = cluster["cluster"]
        allowed = {"server", "certificate-authority-data", "tls-server-name"}
        if set(details) - allowed:
            raise ValueError
        if set(user["user"]) != {"client-certificate-data", "client-key-data"}:
            raise ValueError
        ca = base64.b64decode(details["certificate-authority-data"], validate=True)
        if not ca or any(
            not base64.b64decode(user["user"][key], validate=True) for key in user["user"]
        ):
            raise ValueError
        server = details["server"]
        if child:
            match = re.fullmatch(r"https://([0-9.]+):6443", server)
            if not match or details.get("tls-server-name") != context:
                raise ValueError
            private_ipv4(match[1])
        elif server != "https://127.0.0.1:35495":
            raise ValueError
        return access, ca, server
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        raise ProvisioningError("invalid_local_kubeconfig") from None


class LocalProvider:
    def __init__(
        self,
        config: LocalConfig,
        root: Path,
        credentials: Credentials,
        commands: Commands | None = None,
    ):
        if credentials.environment != "local":
            raise ProvisioningError("credentials_environment_mismatch")
        self.root = root.resolve()
        self.state = self.root / ".state/local"
        if self.state.is_symlink():
            raise ProvisioningError("invalid_local_state")
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state.chmod(0o700)
        self.config, self.credentials = config, credentials
        if credentials.has_database("management"):
            database = credentials.plane("management")["database"]
            if (
                database.get("host") != config.management_cluster["nodeAddress"]
                or database.get("database") != "management"
                or database.get("serverId")
                != "kubernetes://radplanes-local-management-management/statefulsets/postgres"
            ):
                raise ProvisioningError("local_management_database_mismatch")
        self.commands = commands or Commands(self.root, state_root=self.state, local=True)
        self.commands.protect(credentials._data)
        self.radius_config = self.state / "radius.yaml"
        self.config_path = self.state / "provisioning.json"
        write_json(self.config_path, config.to_dict())
        self._verified = False
        self._workload = False
        self._modules: dict[str, dict] = {}
        self._node_addresses = {"management": config.management_cluster["nodeAddress"]}

    def expected_cluster_id(self, slot: str) -> str:
        return self.config.expected_cluster_id(slot)

    def validate_endpoint(self, slot: str, value: str) -> str:
        return self.config.validate_endpoint(slot, value)

    def paths(self, slot: str) -> tuple[str, Path]:
        return self.config.allocation(slot)["context"], self.state / f"{slot}.kubeconfig"

    @staticmethod
    def names(slot: str) -> tuple[str, str]:
        if slot not in SLOTS:
            raise ProvisioningError("allocation_unavailable")
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        return role, f"radplanes-local-{slot}-{role}"

    def rad(self, slot: str, *args: str, workspace: bool = True, timeout: int = 900) -> str:
        context, kubeconfig = self.paths(slot)
        return self.commands.run(
            [
                "rad",
                "--config",
                str(self.radius_config),
                *args,
                *(["--workspace", context] if workspace else []),
            ],
            env=self.commands.radius_environment(kubeconfig, context),
            timeout=timeout,
        )

    def kubectl(self, slot: str, *args: str, stdin: str | None = None, timeout: int = 660) -> str:
        context, kubeconfig = self.paths(slot)
        return self.commands.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "--context",
                context,
                "--request-timeout=30s",
                *args,
            ],
            stdin=stdin,
            timeout=timeout,
        )

    def kube_get(self, slot: str, namespace: str, kind: str, name: str) -> dict | None:
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

    def authenticate(self, *, workload_required: bool = False) -> None:
        self.commands.guard()
        self._workload = workload_required
        context, target = self.paths("management")
        if workload_required:
            ca, token = SERVICE_ACCOUNT / "ca.crt", SERVICE_ACCOUNT / "token"
            expected_host = self.config.management_cluster["serviceAddress"]
            if (
                os.environ.get("KUBERNETES_SERVICE_HOST") != expected_host
                or os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443") != "443"
                or not ca.is_file()
                or not token.is_file()
                or (SERVICE_ACCOUNT / "namespace").read_text().strip()
                != self.names("management")[1]
                or hashlib.sha256(ca.read_bytes()).hexdigest()
                != self.config.management_cluster["caSHA256"]
            ):
                raise ProvisioningError("local_management_identity_mismatch")
            write_json(
                target,
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "current-context": context,
                    "clusters": [
                        {
                            "name": context,
                            "cluster": {
                                "server": f"https://{expected_host}:443",
                                "certificate-authority": str(ca),
                            },
                        }
                    ],
                    "users": [{"name": "provisioner", "user": {"tokenFile": str(token)}}],
                    "contexts": [
                        {
                            "name": context,
                            "context": {
                                "cluster": context,
                                "user": "provisioner",
                                "namespace": "radius-system",
                            },
                        }
                    ],
                },
            )
            who = json.loads(self.kubectl("management", "auth", "whoami", "-o", "json"))
            if who["status"]["userInfo"]["username"] != (
                f"system:serviceaccount:{self.names('management')[1]}:provisioner"
            ):
                raise ProvisioningError("local_management_identity_mismatch")
        else:
            source = self.state / "home/.kube/config"
            if source.is_symlink() or not source.is_file() or source.stat().st_mode & 0o077:
                raise ProvisioningError("invalid_local_kubeconfig")
            access, ca_bytes, _ = decode_access(source.read_text(), context, child=False)
            if hashlib.sha256(ca_bytes).hexdigest() != self.config.management_cluster["caSHA256"]:
                raise ProvisioningError("local_management_identity_mismatch")
            self.commands.protect(access["users"][0]["user"])
            write_json(target, access)
        self.verify_management_identity()

    def verify_management_identity(self) -> None:
        namespace = json.loads(
            self.kubectl("management", "get", "namespace", "kube-system", "-o", "json")
        )
        if namespace["metadata"]["uid"] != self.config.management_cluster["uid"]:
            raise ProvisioningError("local_management_identity_mismatch")
        if self.node_address("management") != self.config.management_cluster["nodeAddress"]:
            raise ProvisioningError("local_management_identity_mismatch")

    def seed_workspace(self, slot: str, *, environment: str | None = None) -> None:
        context, _ = self.paths(slot)
        if self.radius_config.is_symlink():
            raise ProvisioningError("invalid_radius_config")
        try:
            config = (
                yaml.safe_load(self.radius_config.read_text())
                if self.radius_config.exists()
                else {}
            ) or {}
            items = config.setdefault("workspaces", {}).setdefault("items", {})
            items[context] = {
                "connection": {"context": context, "kind": "kubernetes"},
                "scope": SCOPE,
                "environment": (
                    f"{SCOPE}/providers/Applications.Core/environments/{environment or slot}"
                ),
            }
            config["workspaces"]["default"] = self.paths("management")[0]
        except (AttributeError, TypeError, yaml.YAMLError):
            raise ProvisioningError("invalid_radius_config") from None
        write_json(self.radius_config, config)

    def connect_management(self) -> None:
        self.verify_management_identity()
        self.seed_workspace("management")
        for arguments, expected in (
            (("group", "show", GROUP), SCOPE),
            (
                ("environment", "show", "management", "--group", GROUP),
                f"{SCOPE}/providers/Applications.Core/environments/management",
            ),
        ):
            actual = json.loads(self.rad("management", *arguments, "--output", "json"))["id"]
            if not same_radius_id(actual, expected):
                raise ProvisioningError("management_radius_mismatch")

    def verify_recipes(self) -> None:
        for kind, recipe in self.config.recipes.items():
            module = self.kube_get(
                "management", "radius-system", "configmap", recipe["moduleServer"]
            )
            if not module or module.get("immutable") is not True:
                raise ProvisioningError("local_recipe_not_immutable")
            try:
                archive = base64.b64decode(module["binaryData"]["archive.tar.gz"], validate=True)
                server = module["data"]["server.py"].encode()
                digest = hashlib.sha256(archive).hexdigest()
                name = "local-module-" + hashlib.sha256(archive + server).hexdigest()[:20]
                if (
                    digest != recipe["digest"].removeprefix("sha256:")
                    or name != recipe["moduleServer"]
                    or module["metadata"]["name"] != name
                    or module["metadata"]["namespace"] != "radius-system"
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise ProvisioningError("local_recipe_digest_mismatch") from None
            self._modules[kind] = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": "radius-system"},
                "immutable": True,
                "data": {"server.py": server.decode()},
                "binaryData": {"archive.tar.gz": base64.b64encode(archive).decode()},
            }
        self._verified = True

    def parameters(self, slot: str, name: str, values: dict) -> Path:
        self.config.allocation(slot)
        path = self.state / f"{slot}-{name}.parameters.json"
        write_json(
            path,
            {
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
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
            GROUP,
            "--environment",
            environment or slot,
            "--application",
            application,
            "--parameters",
            f"@{path}",
        )

    def resource(self, slot: str, kind: str, name: str, application: str) -> dict:
        value = json.loads(
            self.rad(
                slot,
                "resource",
                "show",
                TYPES[kind],
                name,
                "--group",
                GROUP,
                "--application",
                application,
                "--output",
                "json",
            )
        )
        properties = value["properties"]
        if properties.get("provisioningState") != "Succeeded":
            raise ProvisioningError("radius_resource_not_ready")
        if kind == "postgresql":
            role, namespace = self.names(slot)
            if (
                properties.get("host") != self.node_address(slot)
                or properties.get("port") != 31543
                or properties.get("database") != role
                or properties.get("username") != "plane_setup"
                or properties.get("tlsRequired") is not False
                or properties.get("serverId") != f"kubernetes://{namespace}/statefulsets/postgres"
                or properties.get("setupSecretName") != "postgres-setup"
            ):
                raise ProvisioningError("local_database_output_mismatch")
        return properties

    def resource_exists(self, slot: str, kind: str, name: str) -> bool:
        values = json.loads(
            self.rad(slot, "resource", "list", TYPES[kind], "--group", GROUP, "--output", "json")
        )
        if isinstance(values, dict):
            values = values["value"]
        if not isinstance(values, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in values
        ):
            raise ProvisioningError("invalid_radius_output")
        return any(item["name"] == name for item in values)

    def register_environment(self, slot: str, *, cluster: bool = False) -> str:
        if not self._verified:
            self.verify_recipes()
        self.config.allocation(slot)
        target = "management" if cluster else slot
        environment = f"provision-{slot}" if cluster else slot
        recipe_names = (
            ["cluster"]
            if cluster
            else ["gateway", "redis" if slot.endswith("-data") else "postgresql"]
        )
        node = self.node_address(slot) if "postgresql" in recipe_names else None
        parameters = {
            "cluster": {"images": [self.config.images[role] for role in ("api", "provisioner")]},
            "postgresql": {"node_address": node},
            "redis": {},
            "gateway": {
                "gateway_host_port": self.config.allocation(slot)["gatewayPort"],
            },
        }
        recipes = {
            TYPES[kind]: {
                "default": {
                    "templateKind": "terraform",
                    "templatePath": self.config.recipes[kind]["reference"],
                    "parameters": parameters[kind],
                }
            }
            for kind in recipe_names
        }
        values = {
            "location": "global",
            "properties": {
                "compute": {
                    "kind": "kubernetes",
                    "resourceId": "self",
                    "namespace": (
                        f"radplanes-local-p-{slot}" if cluster else f"radplanes-local-{slot}"
                    ),
                },
                "recipes": recipes,
                "recipeConfig": {
                    "env": {
                        "DOCKER_HOST": "unix:///run/radplanes/docker.sock",
                        "KIND_EXPERIMENTAL_PROVIDER": "docker",
                        "KIND_EXPERIMENTAL_DOCKER_NETWORK": "kind",
                    }
                    if cluster
                    else {},
                },
            },
        }
        suffix = "cluster-environment" if cluster else "environment"
        path = self.state / f"{slot}-{suffix}.json"
        write_json(path, values)
        self.rad(
            target,
            "resource",
            "create",
            "Applications.Core/environments",
            environment,
            "--from-file",
            str(path),
        )
        return environment

    def register(self, slot: str) -> None:
        self.seed_workspace(slot)
        self.rad(slot, "group", "create", GROUP)
        for alias in ("gateways", "postgresql", *(("clusters",) if slot == "management" else ())):
            self.rad(
                slot,
                "resource-type",
                "create",
                "--from-file",
                str(self.root / "infra/radius/types" / f"{alias}.yaml"),
            )
        self.register_environment(slot)

    def ensure_child_cluster(self, slot: str) -> Cluster:
        self.config.allocation(slot)
        if slot == "management":
            raise ProvisioningError("management_is_bootstrap_owned")
        intent = self.state / f"{slot}-cluster-intent.json"
        secret_name = f"radplanes-local-{slot}-access"
        if (
            intent.exists()
            or intent.is_symlink()
            or self.resource_exists("management", "cluster", slot)
            or self.kube_get("management", ACCESS_NAMESPACE, "secret", secret_name)
        ):
            raise ProvisioningError("local_cluster_creation_incomplete")
        create_json(
            intent, {"version": 1, "slot": slot, "clusterId": self.expected_cluster_id(slot)}
        )
        environment = self.register_environment(slot, cluster=True)
        # The shared module selects the registered 2025 API; generic resource create does not.
        self.deploy(
            "management",
            "child-cluster",
            f"cluster-{slot}",
            {"slot": slot},
            environment=environment,
        )
        properties = self.resource("management", "cluster", slot, f"cluster-{slot}")
        expected_ref = f"kubernetes://{ACCESS_NAMESPACE}/{secret_name}#kubeconfig"
        if (
            properties["clusterId"] != self.expected_cluster_id(slot)
            or properties["clusterName"] != self.config.allocation(slot)["clusterName"]
            or properties["bootstrapAccessRef"] != expected_ref
        ):
            raise ProvisioningError("cluster_output_mismatch")
        return self.get_access(slot)

    def get_access(self, slot: str) -> Cluster:
        if slot == "management":
            raise ProvisioningError("management_is_bootstrap_owned")
        context, path = self.paths(slot)
        name = f"radplanes-local-{slot}-access"
        secret = self.kube_get("management", ACCESS_NAMESPACE, "secret", name)
        resource_id = f"{SCOPE}/providers/Demo.Platform/clusters/{slot}"
        if (
            not secret
            or secret["metadata"]["name"] != name
            or secret["metadata"]["namespace"] != ACCESS_NAMESPACE
            or secret["metadata"].get("labels", {}).get("radplanes.local/slot") != slot
            or not same_radius_id(
                secret["metadata"].get("annotations", {}).get("radplanes.local/radius-resource"),
                resource_id,
            )
            or not secret["metadata"].get("uid")
        ):
            raise ProvisioningError("local_access_secret_mismatch")
        encoded = secret["data"]["kubeconfig"]
        self.commands.protect(encoded)
        access, _, _ = decode_access(
            base64.b64decode(encoded, validate=True).decode(), context, child=True
        )
        self.commands.protect(access["users"][0]["user"])
        write_json(path, access)
        if self.kubectl(slot, "get", "--raw=/readyz") != "ok":
            raise ProvisioningError("local_child_not_ready")
        address = self.node_address(slot)
        if access["clusters"][0]["cluster"]["server"] != f"https://{address}:6443":
            raise ProvisioningError("local_child_address_mismatch")
        uid = json.loads(self.kubectl(slot, "get", "namespace", "kube-system", "-o", "json"))[
            "metadata"
        ]["uid"]
        record = {
            "version": 1,
            "slot": slot,
            "clusterId": self.expected_cluster_id(slot),
            "resourceId": resource_id,
            "accessSecretName": name,
            "accessSecretUID": secret["metadata"]["uid"],
            "clusterUID": uid,
            "nodeAddress": address,
        }
        record_path = self.state / f"{slot}-cluster.json"
        if record_path.exists() and json.loads(record_path.read_text()) != record:
            raise ProvisioningError("local_child_identity_changed")
        write_json(record_path, record)
        return Cluster(slot, self.expected_cluster_id(slot), context, path)

    def node_address(self, slot: str) -> str:
        allocation = self.config.allocation(slot)
        node = json.loads(
            self.kubectl(
                slot, "get", "node", f"{allocation['clusterName']}-control-plane", "-o", "json"
            )
        )
        addresses = [
            item["address"] for item in node["status"]["addresses"] if item["type"] == "InternalIP"
        ]
        if (
            node["metadata"]["name"] != f"{allocation['clusterName']}-control-plane"
            or len(addresses) != 1
        ):
            raise ProvisioningError("local_node_identity_mismatch")
        try:
            address = private_ipv4(addresses[0])
        except ValueError:
            raise ProvisioningError("local_node_identity_mismatch") from None
        previous = self._node_addresses.get(slot)
        if previous and previous != address:
            raise ProvisioningError("local_node_identity_mismatch")
        self._node_addresses[slot] = address
        return address

    def publish_modules(self, slot: str) -> None:
        role, _ = self.names(slot)
        for kind in ("gateway", "redis" if role == "data" else "postgresql"):
            recipe = self.config.recipes[kind]
            name = recipe["moduleServer"]
            sha = recipe["digest"].removeprefix("sha256:")
            labels = {"app": name}
            self.apply(
                slot,
                [
                    self._modules[kind],
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {"name": name, "namespace": "radius-system"},
                        "spec": {
                            "replicas": 1,
                            "selector": {"matchLabels": labels},
                            "template": {
                                "metadata": {"labels": labels},
                                "spec": {
                                    "automountServiceAccountToken": False,
                                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532},
                                    "containers": [
                                        {
                                            "name": "module",
                                            "image": PYTHON_IMAGE,
                                            "command": ["python3", "/module/server.py"],
                                            "env": [
                                                {
                                                    "name": "MODULE_SHA256",
                                                    "value": recipe["digest"].removeprefix(
                                                        "sha256:"
                                                    ),
                                                }
                                            ],
                                            "ports": [{"containerPort": 18080}],
                                            "readinessProbe": {
                                                "httpGet": {
                                                    "path": f"/{sha}.tar.gz",
                                                    "port": 18080,
                                                }
                                            },
                                            "securityContext": {
                                                "readOnlyRootFilesystem": True,
                                                "allowPrivilegeEscalation": False,
                                                "capabilities": {"drop": ["ALL"]},
                                            },
                                            "resources": {
                                                "requests": {"cpu": "25m", "memory": "32Mi"},
                                                "limits": {"cpu": "200m", "memory": "64Mi"},
                                            },
                                            "volumeMounts": [
                                                {
                                                    "name": "module",
                                                    "mountPath": "/module",
                                                    "readOnly": True,
                                                }
                                            ],
                                        }
                                    ],
                                    "volumes": [{"name": "module", "configMap": {"name": name}}],
                                },
                            },
                        },
                    },
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "metadata": {"name": name, "namespace": "radius-system"},
                        "spec": {
                            "selector": labels,
                            "ports": [{"port": 18080, "targetPort": 18080}],
                        },
                    },
                ],
            )
            self.kubectl(
                slot,
                "-n",
                "radius-system",
                "rollout",
                "status",
                f"deployment/{name}",
                "--timeout=180s",
            )

    def configure_child_terraform(
        self, slot: str, names: tuple[str, ...] = ("dynamic-rp", "applications-rp")
    ) -> None:
        for name in names:
            deployment = self.kube_get(slot, "radius-system", "deployment", name)
            if not deployment:
                raise ProvisioningError("child_radius_deployment_missing")
            pod = deployment["spec"]["template"]["spec"]
            if (
                len(pod["containers"]) != 1
                or pod["containers"][0]["name"] != name
                or any("hostPath" in volume for volume in pod.get("volumes", []))
            ):
                raise ProvisioningError("child_radius_deployment_mismatch")
            configured = False
            for volume in pod.get("volumes", []):
                if "configMap" not in volume:
                    continue
                cm_name = volume["configMap"]["name"]
                configmap = self.kube_get(slot, "radius-system", "configmap", cm_name)
                if not configmap:
                    continue
                for key, value in configmap.get("data", {}).items():
                    settings = yaml.safe_load(value)
                    if not isinstance(settings, dict) or "terraform" not in settings:
                        continue
                    if settings["terraform"].get("path") != "/terraform":
                        raise ProvisioningError("child_terraform_layout_mismatch")
                    settings["terraform"]["logLevel"] = "OFF"
                    self.kubectl(
                        slot,
                        "-n",
                        "radius-system",
                        "patch",
                        "configmap",
                        cm_name,
                        "--type=merge",
                        "-p",
                        json.dumps({"data": {key: yaml.safe_dump(settings)}}),
                    )
                    configured = True
            if not configured:
                raise ProvisioningError("child_terraform_configuration_missing")
            patch = {
                "spec": {
                    "strategy": {"type": "Recreate", "rollingUpdate": None},
                    "template": {
                        "spec": {
                            "automountServiceAccountToken": False,
                            "securityContext": {
                                "fsGroup": 65532,
                                "fsGroupChangePolicy": "OnRootMismatch",
                            },
                            "initContainers": [
                                {
                                    "name": "local-terraform-layout",
                                    "image": PYTHON_IMAGE,
                                    "command": ["python3", "-c", TERRAFORM_INIT],
                                    "securityContext": {
                                        "runAsUser": 0,
                                        "runAsGroup": 0,
                                        "runAsNonRoot": False,
                                        "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"], "add": ["CHOWN"]},
                                    },
                                    "volumeMounts": [
                                        {"name": "terraform", "mountPath": "/terraform"}
                                    ],
                                }
                            ],
                            "containers": [
                                {
                                    "name": name,
                                    "env": [{"name": "RADIUS_LOGGING_LEVEL", "value": "error"}],
                                    "volumeMounts": [
                                        {
                                            "name": "local-radius-service-account",
                                            "mountPath": str(SERVICE_ACCOUNT),
                                            "readOnly": True,
                                        }
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "local-radius-service-account",
                                    "projected": {
                                        "defaultMode": 0o440,
                                        "sources": [
                                            {
                                                "serviceAccountToken": {
                                                    "path": "token",
                                                    "expirationSeconds": 3600,
                                                }
                                            },
                                            {
                                                "configMap": {
                                                    "name": "kube-root-ca.crt",
                                                    "items": [{"key": "ca.crt", "path": "ca.crt"}],
                                                }
                                            },
                                            {
                                                "downwardAPI": {
                                                    "items": [
                                                        {
                                                            "path": "namespace",
                                                            "fieldRef": {
                                                                "apiVersion": "v1",
                                                                "fieldPath": "metadata.namespace",
                                                            },
                                                        }
                                                    ]
                                                }
                                            },
                                        ],
                                    },
                                }
                            ],
                        }
                    },
                },
            }
            self.kubectl(
                slot,
                "-n",
                "radius-system",
                "patch",
                "deployment",
                name,
                "--type=strategic",
                "-p",
                json.dumps(patch),
            )
            self.kubectl(
                slot,
                "-n",
                "radius-system",
                "rollout",
                "status",
                f"deployment/{name}",
                "--timeout=300s",
            )

    def bootstrap_child(self, cluster: Cluster) -> None:
        if cluster.slot == "management" or cluster.cluster_id != self.expected_cluster_id(
            cluster.slot
        ):
            raise ProvisioningError("cluster_output_mismatch")
        intent = self.state / f"{cluster.slot}-radius-intent.json"
        try:
            create_json(intent, {"version": 1, "clusterId": cluster.cluster_id})
        except FileExistsError:
            raise ProvisioningError("local_child_bootstrap_incomplete") from None
        self.rad(
            cluster.slot,
            "install",
            "kubernetes",
            "--kubecontext",
            cluster.context,
            "--skip-contour-install",
            "--set",
            "dashboard.enabled=false",
            "--set",
            "global.terraform.enabled=false",
            "--set",
            "dynamicrp.buildkit.enabled=false",
            workspace=False,
            timeout=660,
        )
        self.configure_child_terraform(cluster.slot)
        self.publish_modules(cluster.slot)
        self.register(cluster.slot)

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

    @staticmethod
    def management_permissions(namespace: str, module_names: list[str]) -> list[dict]:
        resources = workloads.role_binding(
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
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": module_names,
                    "verbs": ["get"],
                },
            ],
        )
        resources += workloads.role_binding(
            ACCESS_NAMESPACE,
            "plane-provisioner",
            namespace,
            "provisioner",
            [
                {
                    "apiGroups": [""],
                    "resources": ["secrets"],
                    "verbs": ["get"],
                    "resourceNames": [f"radplanes-local-{slot}-access" for slot in SLOTS[1:]],
                }
            ],
        )
        name = "radplanes-local-provisioner-radius-api"
        return resources + [
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": name, "labels": {"project": "radplanes"}},
                "rules": [
                    {
                        "apiGroups": ["api.ucp.dev"],
                        "resources": ["planes/local"],
                        "resourceNames": ["radius"],
                        "verbs": ["get", "list", "create", "update", "delete"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["namespaces"],
                        "resourceNames": ["kube-system"],
                        "verbs": ["get"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["nodes"],
                        "resourceNames": ["radplanes-local-management-control-plane"],
                        "verbs": ["get"],
                    },
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": name, "labels": {"project": "radplanes"}},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": name,
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": "provisioner", "namespace": namespace}
                ],
            },
        ]

    def prerequisites(self, slot: str) -> None:
        role, namespace = self.names(slot)
        self.apply(
            slot,
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace, "labels": {"plane-demo/project": "radplanes"}},
            },
        )
        accounts = [
            f"{role}-api",
            "challenge",
            "database-init",
            "provisioner" if role == "management" else f"{role}-reconciler",
        ]
        resources = [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": account, "namespace": namespace},
                "automountServiceAccountToken": account
                in {"provisioner", "data-api", "data-reconciler"},
            }
            for account in accounts
        ]
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
                resources += workloads.role_binding(
                    namespace,
                    account + "-configmaps",
                    namespace,
                    account,
                    [{"apiGroups": [""], "resources": ["configmaps"], "verbs": verbs}],
                )
        if role == "management":
            resources += self.management_permissions(
                namespace, [recipe["moduleServer"] for recipe in self.config.recipes.values()]
            )
            resources += [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "provisioning-settings", "namespace": namespace},
                    "immutable": True,
                    "data": {"provisioning.json": json.dumps(self.config.to_dict())},
                },
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "provisioner-state", "namespace": namespace},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": "standard",
                        "resources": {"requests": {"storage": "2Gi"}},
                    },
                },
            ]
        self.apply(slot, resources)

    def initialize_database(self, slot: str) -> None:
        workloads.initialize_database(self, slot)

    def database_resource_exists(self, slot: str) -> bool:
        return self.resource_exists(slot, "postgresql", "postgres")

    def cleanup_initialization(self, slot: str, namespace: str, setup_name: str | None) -> None:
        if setup_name not in (None, "postgres-setup"):
            raise ProvisioningError("invalid_setup_secret_name")
        self.kubectl(
            slot,
            "-n",
            namespace,
            "delete",
            "job/database-init",
            "secret/database-init",
            *(["secret/postgres-setup"] if setup_name else []),
            "--wait=true",
            "--ignore-not-found",
        )

    @staticmethod
    def job(namespace: str, name: str, image: str, command: list[str], account: str) -> dict:
        return workloads.job(namespace, name, image, command, account)

    def runtime_secrets(self, slot: str) -> None:
        workloads.runtime_secrets(self, slot)

    def deploy_plane(self, slot: str, observe: Callable[[str], None] = lambda _: None) -> str:
        role, namespace = self.names(slot)
        observe(f"{role}-database" if role != "data" else "data-credentials")
        self.prerequisites(slot)
        if role != "data":
            self.initialize_database(slot)
        self.runtime_secrets(slot)
        values = {"image": self.config.images["api"]}
        if role == "management":
            values.update(provisionerImage=self.config.images["provisioner"])
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
        url = self.validate_endpoint(slot, properties["url"])
        if properties["host"] != "127.0.0.1":
            raise ProvisioningError("invalid_gateway_output")
        # The host URL is operator-facing. A worker Pod must use the node's private address.
        health = f"http://{self.node_address(slot)}:31480" if self._workload else url
        self.commands.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--noproxy",
                "*",
                "--max-time",
                "30",
                f"{health}/livez",
            ],
            timeout=40,
        )
        self.record_endpoint(slot, url)
        return url

    def record_endpoint(self, slot: str, url: str) -> None:
        url = self.validate_endpoint(slot, url)
        write_json(self.state / f"{slot}-endpoint.json", {"url": url})
        key_file = f"{slot}.key"
        write_private(self.state / key_file, self.credentials.plane(slot)["demoKey"] + "\n")
