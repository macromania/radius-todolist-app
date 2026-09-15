"""Reconstruct worker configuration from its selected identity and current resource APIs."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import UUID

from kubernetes import client
from kubernetes import config as kube_config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError

from plane_demo.management.providers.azure import TYPES, login_workload_identity
from plane_demo.management.providers.commands import Commands
from plane_demo.management.providers.identity import SLOTS, DemoConfig
from plane_demo.management.providers.local_artifacts import binding_inputs
from plane_demo.management.providers.local_config import LocalConfig
from plane_demo.management.provisioning import OperatorConfig, ProvisioningError

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def read_runtime_configuration(identity: DemoConfig, root: Path) -> LocalConfig | OperatorConfig:
    namespace = identity.namespace("management")
    if (SERVICE_ACCOUNT / "namespace").read_text().strip() != namespace:
        raise ProvisioningError("management_namespace_mismatch")
    settings = client.Configuration()
    try:
        kube_config.load_incluster_config(client_configuration=settings)
    except ConfigException:
        raise ProvisioningError("in_cluster_management_access_required") from None
    try:
        with client.ApiClient(settings) as connection:
            core, applications = client.CoreV1Api(connection), client.AppsV1Api(connection)
            owner = core.read_namespace(namespace, _request_timeout=(5, 15))
            expected_labels = {
                "plane-demo/project": identity.project,
                "plane-demo/deployment": identity.deployment,
                "plane-demo/environment": identity.environment,
            }
            if owner.metadata.name != namespace or any(
                (owner.metadata.labels or {}).get(key) != value
                for key, value in expected_labels.items()
            ):
                raise ProvisioningError("management_namespace_mismatch")
            resource_id = (
                f"/planes/radius/local/resourceGroups/{identity.stem}"
                "/providers/Applications.Core/environments/management"
            )
            environment = connection.call_api(
                "/apis/api.ucp.dev/v1alpha3" + resource_id,
                "GET",
                query_params=[("api-version", "2023-10-01-preview")],
                response_type="object",
                auth_settings=["BearerToken"],
                _return_http_data_only=True,
                _request_timeout=(5, 15),
            )
            if environment["id"].casefold() != resource_id.casefold():
                raise ProvisioningError("management_radius_mismatch")
            compute = environment["properties"]["compute"]
            if compute != {
                "kind": "kubernetes",
                "resourceId": "self",
                "namespace": f"{identity.stem}-management",
            }:
                raise ProvisioningError("management_radius_mismatch")
            images = {}
            for role, name, account in (
                ("api", "management-api", "management-api"),
                ("provisioner", "provisioner", "provisioner"),
            ):
                deployment = applications.read_namespaced_deployment(
                    name, namespace, _request_timeout=(5, 15)
                )
                pod = deployment.spec.template.spec
                if (
                    deployment.metadata.name != name
                    or deployment.metadata.namespace != namespace
                    or pod.service_account_name != account
                    or len(pod.containers) != 1
                    or pod.containers[0].name != name
                ):
                    raise ProvisioningError("management_workload_mismatch")
                images[role] = pod.containers[0].image
            if identity.environment == "local":
                return local_configuration(identity, environment, images, core)
            return azure_configuration(identity, environment, images, root)
    except (ApiException, HTTPError):
        raise ProvisioningError("runtime_discovery_failed") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ProvisioningError("runtime_discovery_contract_invalid") from None


def local_configuration(identity: DemoConfig, environment: dict, images: dict, core) -> LocalConfig:
    inputs = binding_inputs(environment, identity.stem, identity.stem)
    if any(images[role] != inputs["images"][role]["reference"] for role in images) or (
        identity.revision is not None and inputs["revision"] != identity.revision
    ):
        raise ProvisioningError("management_workload_mismatch")
    system = core.read_namespace("kube-system", _request_timeout=(5, 15))
    node_name = identity.slot_name("management") + "-control-plane"
    node = core.read_node(node_name, _request_timeout=(5, 15))
    service = core.read_namespaced_service("kubernetes", "default", _request_timeout=(5, 15))
    addresses = [value.address for value in node.status.addresses if value.type == "InternalIP"]
    if (
        node.metadata.name != node_name
        or len(addresses) != 1
        or service.metadata.name != "kubernetes"
        or service.metadata.namespace != "default"
    ):
        raise ProvisioningError("local_management_identity_mismatch")
    recipes = {}
    for kind, (resource_type, _) in TYPES.items():
        reference = environment["properties"]["recipes"][resource_type]["default"]["templatePath"]
        url = urlsplit(reference)
        recipes[kind] = {
            "reference": reference,
            "moduleServer": url.hostname.split(".", 1)[0],
            "digest": "sha256:" + url.path.removeprefix("/").removesuffix(".tar.gz"),
        }
    return LocalConfig.from_dict(
        {
            "version": 1,
            "provider": "local",
            "projectName": identity.project,
            "allocations": {
                slot: {
                    "slot": slot,
                    "clusterName": identity.slot_name(slot),
                    "context": identity.slot_name(slot),
                    "apiPort": 35495 + index,
                    "gatewayPort": 35490 + index,
                }
                for index, slot in enumerate(SLOTS)
            },
            "recipes": recipes,
            "images": {
                role: {
                    "reference": inputs["images"][role]["reference"],
                    "imageId": inputs["images"][role]["id"],
                }
                for role in ("api", "provisioner")
            },
            "managementCluster": {
                "clusterId": f"kind://{identity.slot_name('management')}",
                "uid": system.metadata.uid,
                "nodeAddress": addresses[0],
                "serviceAddress": service.spec.cluster_ip,
                "caSHA256": hashlib.sha256((SERVICE_ACCOUNT / "ca.crt").read_bytes()).hexdigest(),
            },
        },
        identity=identity,
    )


def azure_configuration(
    identity: DemoConfig, environment: dict, images: dict, root: Path
) -> OperatorConfig:
    if identity.subscription is None:
        raise ProvisioningError("subscription_required")
    client_id, tenant_id = (
        os.environ.get("AZURE_CLIENT_ID", ""),
        os.environ.get("AZURE_TENANT_ID", ""),
    )
    UUID(client_id)
    UUID(tenant_id)
    with TemporaryDirectory(prefix="plane-runtime-discovery-") as temporary:
        workspace = Path(temporary)
        commands = Commands(root, state_root=workspace)
        login_workload_identity(commands, workspace, client_id, tenant_id)

        def az(*arguments):
            return commands.json(
                [
                    "az",
                    *arguments,
                    "--subscription",
                    identity.subscription,
                    "--output",
                    "json",
                    "--only-show-errors",
                ]
            )

        deployment = az("deployment", "sub", "show", "--name", f"{identity.stem}-bootstrap")
        if deployment["properties"].get("provisioningState") != "Succeeded":
            raise ProvisioningError("foundation_not_ready")
        outputs = {
            key: value["value"] for key, value in deployment["properties"]["outputs"].items()
        }
        if (
            outputs["coordinatorIdentity"]["clientId"] != client_id
            or outputs["foundation"]["tenantId"] != tenant_id
        ):
            raise ProvisioningError("workload_identity_mismatch")
        host = f"{identity.registry_name}.azurecr.io"
        recipes = {}
        for kind, (resource_type, _) in TYPES.items():
            binding = environment["properties"]["recipes"][resource_type]["default"]
            reference = binding["templatePath"]
            if binding["templateKind"] != "bicep" or not re.fullmatch(
                re.escape(host + "/") + r"[a-z0-9/_-]+:[a-zA-Z0-9_.-]+", reference
            ):
                raise ProvisioningError("management_recipe_binding_mismatch")
            record = az(
                "acr",
                "repository",
                "show",
                "--name",
                identity.registry_name,
                "--image",
                reference.removeprefix(host + "/"),
            )
            recipes[kind] = {
                "reference": reference,
                "digest": record["digest"],
                "immutability": "acr-abac-arm-import-v1",
            }
        return OperatorConfig.from_dict(
            {
                "version": 1,
                **outputs,
                "allocations": {value["slot"]: value for value in outputs["allocations"]},
                "recipes": recipes,
                "images": images,
            },
            identity=identity,
        )
