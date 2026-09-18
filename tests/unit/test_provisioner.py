import base64
import copy
import importlib.util
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from plane_demo.management import provisioner
from plane_demo.management.providers import discovery
from plane_demo.management.providers.azure import TYPES, AzureProvider
from plane_demo.management.providers.commands import Commands
from plane_demo.management.providers.credentials import (
    Credentials,
    StoredCredentials,
    credential_roles,
    database_dsn,
)
from plane_demo.management.providers.identity import (
    PUBLIC_KEYS,
    SLOTS,
    DemoConfig,
    provisioning_namespace,
)
from plane_demo.management.providers.secret_store import CredentialScope, CredentialValue
from plane_demo.management.provisioning import (
    OperatorConfig,
    PairResult,
    ProvisioningError,
    provision_pair,
)

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"
PREFIX = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/"
RADIUS_SCOPE = "/planes/radius/local/resourceGroups/radplanes"
MANAGEMENT_ENVIRONMENT = f"{RADIUS_SCOPE}/providers/Applications.Core/environments/management"
SECRET = "a/+: % password ' escaped " + "x" * 40
URLS = {
    "control": "https://control.centralus.cloudapp.azure.com",
    "data": "https://data.centralus.cloudapp.azure.com",
}


@pytest.fixture
def raw_config():
    slots = ["management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data"]
    allocations = {}
    for index, slot in enumerate(slots):
        subnet = PREFIX + "rg-radplanes-platform/providers/Microsoft.Network/virtualNetworks/vnet"
        allocations[slot] = {
            "slot": slot,
            "certificateName": f"gateway-{slot}",
            "acmeStateSecretName": f"acme-{slot}",
            "certificateIssuerSubject": "system:serviceaccount:radplanes-system:certificate-issuer",
            "clusterName": f"radplanes-{slot}",
            "clusterResourceGroup": f"rg-radplanes-{slot}-cluster",
            "clusterResourceGroupId": PREFIX + f"rg-radplanes-{slot}-cluster",
            "appResourceGroup": f"rg-radplanes-{slot}-app",
            "appResourceGroupId": PREFIX + f"rg-radplanes-{slot}-app",
            "nodeSubnetId": subnet + "/subnets/nodes",
            "nodeSubnetName": "nodes",
            "gatewaySubnetId": subnet + "/subnets/gateway",
            "gatewaySubnetCidr": f"10.64.{16 + index}.0/24",
            "privateEndpointSubnetId": subnet + "/subnets/endpoints",
            "postgresqlSubnetId": subnet + "/subnets/postgres",
            "apiPrivateIp": f"10.64.{index}.240",
            "challengePrivateIp": f"10.64.{index}.241",
            "identities": {
                name: {
                    "id": PREFIX + f"rg-radplanes-{slot}-cluster/providers/"
                    f"Microsoft.ManagedIdentity/userAssignedIdentities/{name}",
                    "clientId": CLIENT,
                }
                for name in ("radius", "gateway", "controlPlane", "kubelet", "certificateIssuer")
            },
        }
    return {
        "version": 1,
        "foundation": {
            "subscriptionId": SUBSCRIPTION,
            "tenantId": TENANT,
            "projectName": "radplanes",
            "location": "centralus",
            "registryName": "demoregistry",
            "registryLoginServer": "demoregistry.azurecr.io",
            "vaultName": "demo-vault",
            "postgresqlDnsZoneId": PREFIX + "rg-radplanes-platform/dns/postgres",
            "redisDnsZoneId": PREFIX + "rg-radplanes-platform/dns/redis",
            "tags": {"SecurityControl": "Ignore", "project": "radplanes"},
            "nodeVmSize": "Standard_D4s_v5",
            "nodeCount": 2,
            "postgresSkuName": "Standard_D2ads_v5",
            "postgresSkuTier": "GeneralPurpose",
            "kubernetesVersion": "1.35.7",
            "egressIp": "5.6.7.8",
            "authorizedIpRanges": ["1.2.3.4/32", "5.6.7.8/32"],
        },
        "allocations": allocations,
        "recipes": {
            name: {
                "reference": f"demoregistry.azurecr.io/recipes/{name}:v1",
                "digest": "sha256:" + "a" * 64,
            }
            for name in ("cluster", "postgresql", "gateway", "redis")
        },
        "images": {
            name: f"demoregistry.azurecr.io/{name}@sha256:" + "b" * 64
            for name in ("api", "provisioner")
        },
        "coordinatorIdentity": {"clientId": CLIENT},
        "managementCluster": {"id": "management-id"},
    }


@pytest.fixture
def config(raw_config):
    return OperatorConfig.from_dict(raw_config)


def credentials(path, config):
    result = Credentials(path)
    result.ensure(
        "management",
        {
            "mgmt_api",
            "mgmt_provisioner",
            *(item["reporting_role"] for item in config.pair_slots),
        },
    )
    result.set_database("management", database_properties())
    return result


@pytest.fixture
def selected_config(raw_config):
    identity = DemoConfig(
        "azure",
        "sample",
        "demo",
        SUBSCRIPTION,
        "northeurope",
        demo_keys={"management": "synthetic-selected-api-key-" + "x" * 32},
    )
    foundation = raw_config["foundation"]
    old_registry = foundation["registryLoginServer"]
    foundation.update(
        {
            "projectName": identity.project,
            "deploymentName": identity.deployment,
            "environment": "azure",
            "resourceGroupLayout": "plane-v2",
            "resourcePrefix": identity.stem,
            "radiusResourceGroup": identity.stem,
            "location": identity.location,
            "registryName": identity.registry_name,
            "registryLoginServer": f"{identity.registry_name}.azurecr.io",
            "vaultName": identity.vault_name,
        }
    )
    for slot, allocation in raw_config["allocations"].items():
        name = identity.slot_name(slot)
        allocation.update(
            {
                "clusterName": f"aks-{name}",
                "clusterResourceGroup": f"rg-{name}",
                "appResourceGroup": f"rg-{name}",
                "clusterResourceGroupId": PREFIX + f"rg-{name}",
                "appResourceGroupId": PREFIX + f"rg-{name}",
                "nodeResourceGroup": f"rg-{name}-nodes",
                "namespace": identity.namespace(slot),
                "certificateName": f"gateway-{name}",
                "acmeStateSecretName": f"acme-{name}",
                "certificateIssuerSubject": (
                    f"system:serviceaccount:{identity.stem}-system:certificate-issuer"
                ),
            }
        )
        for key, purpose in {
            "radius": "radius",
            "gateway": "gateway",
            "controlPlane": "control-plane",
            "kubelet": "kubelet",
            "certificateIssuer": "certificate-issuer",
        }.items():
            allocation["identities"][key].update(
                id=identity.managed_identity_id(slot, purpose), principalId=CLIENT
            )
    for recipe in raw_config["recipes"].values():
        recipe["reference"] = recipe["reference"].replace(
            old_registry, foundation["registryLoginServer"]
        )
    raw_config["images"] = {
        role: reference.replace(old_registry, foundation["registryLoginServer"])
        for role, reference in raw_config["images"].items()
    }
    management = raw_config["allocations"]["management"]
    raw_config["managementCluster"] = {
        "id": management["clusterResourceGroupId"]
        + "/providers/Microsoft.ContainerService/managedClusters/"
        + management["clusterName"],
        "name": management["clusterName"],
        "resourceGroup": management["clusterResourceGroup"],
    }
    return OperatorConfig.from_dict(raw_config, identity=identity)


def test_selected_identity_drives_provider_commands_and_temporary_workspace(
    selected_config, tmp_path, monkeypatch
):
    root, workspace = tmp_path / "checkout", tmp_path / "work"
    root.mkdir()
    provider = AzureProvider(
        selected_config,
        root,
        credentials(tmp_path / "credentials.json", selected_config),
        workspace=workspace,
    )
    commands = provider.commands
    compiler = tmp_path / "bicep"
    compiler.write_text("#!/bin/sh\nexit 0\n")
    compiler.chmod(0o700)
    commands._bicep = compiler
    provider.paths("shared-control")[1].write_text("{}")
    monkeypatch.setattr(
        commands, "run", MagicMock(return_value='{"properties":{"provisioningState":"Succeeded"}}')
    )
    provider.resource("shared-control", "gateway", "gateway", "control")
    command = commands.run.call_args.args[0]
    assert command[command.index("--group") + 1] == "sample-demo-azure"
    assert command[command.index("--workspace") + 1] == "sample-demo-azure-shared-control"
    assert provider.expected_cluster_id("shared-control").endswith(
        "/managedClusters/aks-sample-demo-azure-shared-control"
    )
    assert (workspace / "provisioning.json").is_file()
    assert not (root / ".state").exists()
    emitted = []
    monkeypatch.setattr(provider, "apply", lambda slot, value, **kw: emitted.append(value))
    monkeypatch.setattr(provider, "kube_get", lambda *args: None)
    provider.prerequisites("shared-control")
    assert emitted[0]["metadata"] == {
        "name": "sample-demo-azure-shared-control-control",
        "labels": {
            "plane-demo/project": "sample",
            "plane-demo/deployment": "demo",
            "plane-demo/environment": "azure",
        },
    }
    provider.credentials.ensure("shared-control", {"cp_api", "cp_reconciler", "dp_reconciler"})
    control_database = database_properties()
    control_database["database"] = "control"
    provider.credentials.set_database("shared-control", control_database)
    secrets = {}
    monkeypatch.setattr(
        provider, "secret", lambda _, __, name, values: secrets.update({name: values})
    )
    provider.runtime_secrets("shared-data")
    assert secrets["data-api-runtime"]["PROJECT_ID"] == "sample"
    assert secrets["data-reconciler-runtime"]["PROJECT_ID"] == "sample"
    emitted.clear()
    provider.prerequisites("management")
    settings = next(
        item
        for batch in emitted
        if isinstance(batch, list)
        for item in batch
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "provisioning-settings"
    )
    assert settings["data"] == selected_config.bootstrap_settings
    assert settings["immutable"] is True
    assert selected_config.identity.demo_keys["management"] not in json.dumps(settings)


def test_selected_identity_round_trip_excludes_provided_credentials(selected_config):
    values = selected_config.to_dict()
    key = selected_config.identity.demo_keys["management"]
    assert key not in json.dumps(values)
    assert "DEMO_KEY_MANAGEMENT" not in values["bootstrapIdentity"]
    restored = OperatorConfig.from_dict(values, identity=selected_config.identity)
    assert restored.identity is selected_config.identity
    assert restored.namespace("management") == "sample-demo-azure-management-management"
    values["bootstrapIdentity"]["DEMO_KEY_MANAGEMENT"] = key
    with pytest.raises(ValueError, match="public settings only"):
        OperatorConfig.from_dict(values)


def test_selected_child_provisioning_namespace_accounts_for_radius_application_suffix(
    selected_config, tmp_path, monkeypatch
):
    provider = AzureProvider(
        selected_config,
        tmp_path,
        credentials(tmp_path / "credentials.json", selected_config),
    )
    provider._verified = True
    monkeypatch.setattr(provider, "rad", MagicMock())
    provider.register_cluster_environment("isolated-1-control")
    parameters = json.loads(
        (provider.state / "isolated-1-control-cluster-environment.parameters.json").read_text()
    )["parameters"]
    namespace = parameters["namespace"]["value"]
    assert namespace == "sample-demo-azure-p-3"
    assert len(namespace + "-cluster-isolated-1-control") <= 63


@pytest.mark.parametrize("size", ["Standard_D4as_v7", "Standard_D4s_v7"])
def test_selected_node_size_reaches_management_and_child_recipes(
    selected_config, tmp_path, monkeypatch, size
):
    raw = selected_config.to_dict()
    raw["foundation"]["nodeVmSize"] = size
    selected = OperatorConfig.from_dict(raw, identity=selected_config.identity)
    provider = AzureProvider(
        selected, tmp_path, credentials(tmp_path / "credentials.json", selected)
    )
    provider._verified = True
    monkeypatch.setattr(provider, "rad", MagicMock())
    parameters = provider.recipe_map("management")["Demo.Platform/clusters"]["default"][
        "parameters"
    ]
    assert parameters["nodeVmSize"] == size and parameters["nodeCount"] == 2
    for slot in ("shared-control", "isolated-1-data"):
        provider.register_cluster_environment(slot)
        document = json.loads(
            (provider.state / f"{slot}-cluster-environment.parameters.json").read_text()
        )
        parameters = document["parameters"]["recipes"]["value"]["Demo.Platform/clusters"]
        assert parameters["default"]["parameters"]["nodeVmSize"] == size
        assert parameters["default"]["parameters"]["nodeCount"] == 2


def test_maximum_selected_prefix_stays_within_kubernetes_namespace_limit():
    identity = DemoConfig("azure", "abcdefghijklmnop", "ab", SUBSCRIPTION, "centralus")
    assert len(identity.stem) == 25
    for slot in SLOTS[1:]:
        assert len(provisioning_namespace(identity.stem, slot) + f"-cluster-{slot}") <= 63
        assert len(identity.namespace(slot)) <= 63


@pytest.mark.parametrize("sku", ["Standard_D2ads_v5", "Standard_D2ds_v5"])
def test_selected_postgres_compute_reaches_each_database_recipe(
    selected_config, tmp_path, monkeypatch, sku
):
    raw = selected_config.to_dict()
    raw["foundation"].update(postgresSkuName=sku, postgresSkuTier="GeneralPurpose")
    selected = OperatorConfig.from_dict(raw, identity=selected_config.identity)
    provider = AzureProvider(
        selected, tmp_path, credentials(tmp_path / "credentials.json", selected)
    )
    provider._verified = True
    monkeypatch.setattr(provider, "rad", MagicMock())
    monkeypatch.setattr(provider, "verify_recipes", MagicMock())
    for slot in ("management", "shared-control", "isolated-1-control"):
        provider.register(slot)
        document = json.loads((provider.state / f"{slot}-environment.parameters.json").read_text())
        parameters = document["parameters"]["recipes"]["value"][
            "Demo.Platform/postgreSqlDatabases"
        ]["default"]["parameters"]
        assert parameters["skuName"] == sku
        assert parameters["skuTier"] == "GeneralPurpose"


@pytest.mark.parametrize(
    "selection",
    [
        {"postgresSkuName": "Standard_D2ads_v5", "postgresSkuTier": "unknown"},
        {"postgresSkuName": "Standard_D2ads_v5", "postgresSkuTier": None},
        {"postgresSkuName": None, "postgresSkuTier": "GeneralPurpose"},
        {"postgresSkuName": "invalid", "postgresSkuTier": "GeneralPurpose"},
    ],
)
def test_foundation_postgres_selection_must_be_valid_and_complete(raw_config, selection):
    raw_config["foundation"].update(selection)
    with pytest.raises(ValueError, match="PostgreSQL compute"):
        OperatorConfig.from_dict(raw_config)


def test_old_foundation_cannot_silently_use_a_hardcoded_postgres_size(selected_config, tmp_path):
    raw = selected_config.to_dict()
    raw["foundation"].pop("postgresSkuName")
    raw["foundation"].pop("postgresSkuTier")
    selected = OperatorConfig.from_dict(raw, identity=selected_config.identity)
    provider = AzureProvider(
        selected, tmp_path, credentials(tmp_path / "credentials.json", selected)
    )
    with pytest.raises(ProvisioningError, match="postgresql_selection_missing"):
        provider.recipe_map("shared-control")


def test_canonical_management_configuration_and_suspended_job_use_live_inputs(
    selected_config, tmp_path, monkeypatch
):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts/operations"))
    spec = importlib.util.spec_from_file_location(
        "canonical_management_operator", root / "scripts/operations/run-management-job.py"
    )
    operator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operator)
    selected = selected_config.to_dict()
    outputs = {
        key: selected[key] for key in ("foundation", "coordinatorIdentity", "managementCluster")
    }
    outputs["allocations"] = list(selected["allocations"].values())
    artifacts = {
        "source_revision": "a" * 40,
        "status": "artifacts_verified",
        "content_verified": True,
        "recipes": selected["recipes"],
        "images": selected["images"],
    }
    observed, objects = [], {}
    resumed = []

    def execute(arguments, *, value=None, **kwargs):
        observed.append(arguments)
        if arguments[:2] == ["git", "status"]:
            return ""
        if arguments[:2] == ["git", "rev-parse"]:
            return "a" * 40
        if arguments[0] == "bash":
            assert arguments[-1] == "--inspect"
            return json.dumps(artifacts)
        if arguments[:4] == ["az", "deployment", "sub", "show"]:
            assert arguments[arguments.index("--subscription") + 1] == SUBSCRIPTION
            return json.dumps(
                {
                    "properties": {
                        "provisioningState": "Succeeded",
                        "outputs": {key: {"value": item} for key, item in outputs.items()},
                    }
                }
            )
        assert arguments[0] == "kubectl"
        if "get" in arguments:
            position = arguments.index("get")
            record = objects.get((arguments[position + 1].lower(), arguments[position + 2]))
            return json.dumps(record) if record else ""
        if "create" in arguments:
            record = copy.deepcopy(value)
            record["metadata"].update(uid=str(uuid4()), resourceVersion="1")
            if record["kind"] == "Secret":
                record["data"] = {
                    key: base64.b64encode(item.encode()).decode()
                    for key, item in record.pop("stringData").items()
                }
            key = (record["kind"].lower(), record["metadata"]["name"])
            assert key not in objects
            objects[key] = record
            return json.dumps(record) if "-o" in arguments else ""
        if "patch" in arguments:
            job = objects["job", "deploy-management"]
            patch = json.loads(arguments[arguments.index("-p") + 1])
            assert patch[0]["value"] == job["metadata"]["uid"]
            assert ("configmap", "deploy-management-config") in objects
            assert ("secret", "deploy-management-keys") in objects
            job["spec"]["suspend"] = False
            job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
            resumed.append(job["metadata"]["uid"])
            return ""
        assert "wait" in arguments or "rollout" in arguments
        assert resumed
        return ""

    monkeypatch.setattr(operator, "execute", execute)
    config = operator.live_configuration(selected_config.identity)
    assert config.identity.revision == "a" * 40
    assert config.identity.demo_keys == selected_config.identity.demo_keys
    result = operator.deploy_selected(
        config, config.workspace("management"), str(tmp_path / "kubeconfig")
    )
    assert result["stage"] == "management-deployed"
    job = objects["job", "deploy-management"]
    secret = objects["secret", "deploy-management-keys"]
    assert secret["metadata"]["ownerReferences"][0]["uid"] == job["metadata"]["uid"]
    assert (
        base64.b64decode(secret["data"]["DEMO_KEY_MANAGEMENT"]).decode()
        == config.identity.demo_keys["management"]
    )
    assert not any(kind == "persistentvolumeclaim" for kind, _ in objects)
    assert not any(".state" in argument for arguments in observed for argument in arguments)
    changed_identity = DemoConfig.from_values(
        {
            **config.identity.public_values(),
            "DEMO_KEY_MANAGEMENT": "changed-key-" + "x" * 48,
        }
    )
    changed = OperatorConfig.from_dict(config.to_dict(), identity=changed_identity)
    with pytest.raises(ValueError, match="Supplied bootstrap keys changed"):
        operator.deploy_selected(
            changed, config.workspace("management"), str(tmp_path / "kubeconfig")
        )
    job["spec"]["suspend"] = True
    job["status"] = {}
    job["spec"]["template"]["spec"]["containers"][0].pop("envFrom")
    del objects["secret", "deploy-management-keys"]
    before = copy.deepcopy(objects)
    with pytest.raises(ValueError, match="Existing operator Job does not match"):
        operator.deploy_selected(
            config, config.workspace("management"), str(tmp_path / "kubeconfig")
        )
    assert objects == before


def test_radius_commands_only_accept_selected_contexts_in_temporary_home(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    kubeconfig = workspace / "management.kubeconfig"
    kubeconfig.write_text("synthetic-kubeconfig")
    compiler = tmp_path / "bicep"
    compiler.write_text("#!/bin/sh\nexit 0\n")
    compiler.chmod(0o700)
    context = "sample-demo-azure-management"
    commands = Commands(tmp_path, state_root=workspace, contexts={context})
    commands._bicep = compiler
    env = commands.radius_environment(kubeconfig, context)
    assert Path(env["HOME"]).is_relative_to(workspace)
    assert Path(env["KUBECONFIG"]) == kubeconfig
    for foreign in ("radplanes-management", "foreign", "../../outside"):
        with pytest.raises(ProvisioningError, match="invalid_radius_context"):
            commands.radius_environment(kubeconfig, foreign)
    assert not (tmp_path / ".state").exists()


@pytest.mark.parametrize(
    "field",
    [
        "projectName",
        "deploymentName",
        "environment",
        "resourcePrefix",
        "radiusResourceGroup",
        "registryName",
        "vaultName",
    ],
)
def test_selected_identity_rejects_foreign_foundation(selected_config, field):
    values = selected_config.to_dict()
    values["foundation"][field] = "foreign"
    with pytest.raises(ValueError):
        OperatorConfig.from_dict(values, identity=selected_config.identity)


def database_properties():
    return {
        "provisioningState": "Succeeded",
        "application": f"{RADIUS_SCOPE}/providers/Applications.Core/applications/management",
        "environment": MANAGEMENT_ENVIRONMENT,
        "host": "pg-demo.postgres.database.azure.com",
        "port": 5432,
        "database": "management",
        "username": "plane_setup",
        "tlsRequired": True,
        "serverId": "postgres-resource",
        "setupSecretName": "postgres-setup",
    }


@pytest.fixture
def provider(tmp_path, config):
    commands = MagicMock(spec=Commands)
    commands.environment = {}
    commands.run.return_value = ""
    commands.radius_environment.side_effect = lambda kubeconfig, context: {
        "KUBECONFIG": str(kubeconfig),
        "HOME": str(tmp_path / ".state/azure/homes" / context),
        "AZURE_CONFIG_DIR": str(tmp_path / ".state/azure/az"),
    }
    return AzureProvider(
        config, tmp_path, credentials(tmp_path / ".state/azure/credentials.json", config), commands
    )


@pytest.mark.parametrize(
    ("template", "directory"),
    [
        ("management", "apps"),
        ("control", "apps"),
        ("data", "apps"),
        ("child-cluster", "modules"),
        ("database", "modules"),
        ("gateway", "modules"),
        ("challenge", "modules"),
        ("workload", "modules"),
    ],
)
def test_deploy_run_path_selects_the_moved_template_group(
    provider, monkeypatch, template, directory
):
    provider._verified = True
    command = MagicMock()
    monkeypatch.setattr(provider, "rad", command)
    provider.deploy("management", template, "layout-test", {})
    assert command.call_args.args[:3] == (
        "management",
        "deploy",
        str(provider.root / "infra/radius" / directory / f"{template}.bicep"),
    )
    assert (provider.state / f"management-{template}.parameters.json").is_file()


def operation(pair_id="shared"):
    return SimpleNamespace(
        operation_id=uuid4(),
        tenant_id="shared-a",
        pair_id=pair_id,
        onboarding_id=uuid4(),
        initial_message="hello",
        isolation="shared" if pair_id == "shared" else "isolated",
    )


def pair(config, *, available=False):
    ids = [
        f"{config.allocations[f'shared-{role}']['clusterResourceGroupId']}"
        f"/providers/Microsoft.ContainerService/managedClusters/radplanes-shared-{role}"
        for role in ("control", "data")
    ]
    return {
        "pair_id": "shared",
        "isolation": "shared",
        "reporting_role": "cp_shared",
        "stage": "available" if available else "allocated",
        "control_cluster_id": ids[0],
        "data_cluster_id": ids[1],
        "control_url": URLS["control"],
        "data_url": URLS["data"],
    }


def test_configuration_is_deeply_immutable(config):
    with pytest.raises(TypeError):
        config.allocations["shared-control"]["clusterName"] = "other"
    assert config.to_dict()["version"] == 1
    assert {slot["reporting_role"] for slot in config.pair_slots} == {"cp_shared", "cp_isolated_1"}


def test_image_manifest_reference_objects_are_normalized(raw_config):
    reference = raw_config["images"]["provisioner"]
    raw_config["images"]["provisioner"] = {"reference": reference, "source_sha256": "recorded"}
    config = OperatorConfig.from_dict(raw_config)
    assert config.images["provisioner"] == reference
    assert config.to_dict()["images"]["provisioner"] == reference


@pytest.mark.parametrize("slot", ["../../management", "shared-control;echo bad", "--help", "x\nx"])
def test_invalid_slots_never_reach_commands(provider, slot):
    with pytest.raises(ProvisioningError, match="allocation_unavailable"):
        provider.ensure_child_cluster(slot)
    provider.commands.run.assert_not_called()


@pytest.mark.parametrize("mutation", ["slot", "subscription", "image", "digest", "missing-pair"])
def test_invalid_operator_config_is_rejected(raw_config, mutation):
    if mutation == "slot":
        raw_config["allocations"]["shared-control"]["slot"] = "../management"
    elif mutation == "subscription":
        raw_config["allocations"]["shared-control"]["nodeSubnetId"] = "/subscriptions/other/x"
    elif mutation == "image":
        raw_config["images"]["api"] = "demoregistry.azurecr.io/api:latest"
    elif mutation == "digest":
        raw_config["recipes"]["cluster"]["digest"] = "wrong"
    else:
        del raw_config["allocations"]["shared-data"]
    with pytest.raises(ValueError):
        OperatorConfig.from_dict(raw_config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("gatewaySubnetCidr", "bad-cidr"),
        ("gatewaySubnetCidr", "10.64.17.1/24"),
        ("gatewaySubnetCidr", "fd00::/64"),
        ("gatewaySubnetCidr", "0.0.0.0/0"),
        ("gatewaySubnetCidr", "10.64.17.0/25"),
        ("gatewaySubnetCidr", "10.64.1.0/24"),
        ("apiPrivateIp", "1.2.3.4"),
        ("apiPrivateIp", "10.64.2.240"),
        ("apiPrivateIp", "10.64.1.241"),
        ("apiPrivateIp", "10.64.1.999"),
        ("apiPrivateIp", "::1"),
        ("apiPrivateIp", 123),
        ("challengePrivateIp", "10.64.1.240"),
        ("challengePrivateIp", "10.64.1.255"),
        ("challengePrivateIp", "10.64.2.241"),
        ("nodeSubnetCidr", "10.64.2.0/24"),
        ("nodeSubnetCidr", "10.64.1.0/25"),
    ],
)
def test_allocation_network_fields_follow_the_bootstrap_contract(raw_config, field, value):
    raw_config["allocations"]["shared-control"][field] = value
    with pytest.raises(ValueError):
        OperatorConfig.from_dict(raw_config)


@pytest.mark.parametrize(
    "ranges",
    [
        [],
        None,
        "1.2.3.4/32",
        ["bad-cidr"],
        ["::/0"],
        ["0.0.0.0/0"],
        ["1.2.3.0/24"],
        ["10.0.0.1/32"],
        ["127.0.0.1/32"],
        ["169.254.1.1/32"],
        ["224.0.0.1/32"],
        ["1.2.3.4"],
        ["1.2.3.4/255.255.255.255"],
        ["1.2.3.4/32"],
    ],
)
def test_authorized_ranges_require_explicit_public_hosts_and_nat(raw_config, ranges):
    raw_config["foundation"]["authorizedIpRanges"] = ranges
    with pytest.raises(ValueError, match="authorizedIpRanges"):
        OperatorConfig.from_dict(raw_config)


def test_multiple_operator_hosts_and_nat_are_preserved_without_broadening(raw_config):
    ranges = ["1.2.3.5/32", "1.2.3.133/32", "5.6.7.8/32"]
    raw_config["foundation"]["authorizedIpRanges"] = ranges
    raw_config["allocations"]["shared-control"]["nodeSubnetCidr"] = "10.64.1.0/24"
    raw_config["allocations"] = dict(reversed(list(raw_config["allocations"].items())))
    config = OperatorConfig.from_dict(raw_config)
    assert config.to_dict()["foundation"]["authorizedIpRanges"] == ranges


def test_duplicate_subnet_allocations_are_rejected(raw_config):
    source = raw_config["allocations"]["shared-control"]
    target = raw_config["allocations"]["shared-data"]
    for field in ("gatewaySubnetCidr", "apiPrivateIp", "challengePrivateIp"):
        target[field] = source[field]
    with pytest.raises(ValueError, match="subnet allocations"):
        OperatorConfig.from_dict(raw_config)


@pytest.mark.parametrize("value", ["5.6.7.8/32", "::1", "bad-ip", 123])
def test_nat_egress_requires_a_bare_ipv4_address(raw_config, value):
    raw_config["foundation"]["egressIp"] = value
    with pytest.raises(ValueError, match="egressIp"):
        OperatorConfig.from_dict(raw_config)


def test_shared_reuse_uses_live_pair_metadata_not_database_inventory(provider, monkeypatch):
    observe = MagicMock()
    existing = pair(provider.config, available=True)
    live = PairResult(**{key: existing.pop(key) for key in PairResult.__dataclass_fields__})
    inspect = MagicMock(return_value=live)
    monkeypatch.setattr(provider, "inspect_pair", inspect)
    result = provision_pair(operation(), provider, existing, observe)
    assert result == live
    inspect.assert_called_once_with("shared")
    observe.assert_called_once_with("reuse-pair")
    provider.commands.run.assert_not_called()
    provider.commands.json.assert_not_called()


def test_reuse_rejects_wrong_live_cluster_inventory(provider, monkeypatch):
    existing = pair(provider.config, available=True)
    monkeypatch.setattr(
        provider,
        "inspect_pair",
        lambda _: PairResult(
            "unowned",
            existing["data_cluster_id"],
            existing["control_url"],
            existing["data_url"],
        ),
    )
    with pytest.raises(ProvisioningError, match="pair_inventory_mismatch"):
        provision_pair(operation(), provider, existing, MagicMock())


def test_available_pair_reads_current_radius_owners_and_gateways(provider, monkeypatch):
    reads = []
    accesses = []

    def resource(slot, kind, name, application):
        reads.append((slot, kind, name, application))
        if kind == "cluster":
            return {
                "clusterId": provider.expected_cluster_id(name),
                "provisioningState": "Succeeded",
                "application": (
                    f"{RADIUS_SCOPE}/providers/Applications.Core/applications/{application}"
                ),
                "environment": (
                    f"{RADIUS_SCOPE}/providers/Applications.Core/environments/provision-{name}"
                ),
            }
        return {
            "url": URLS[application],
            "provisioningState": "Succeeded",
            "application": f"{RADIUS_SCOPE}/providers/Applications.Core/applications/{application}",
            "environment": f"{RADIUS_SCOPE}/providers/Applications.Core/environments/{slot}",
        }

    def access(slot):
        accesses.append(slot)
        return SimpleNamespace(cluster_id=provider.expected_cluster_id(slot))

    monkeypatch.setattr(provider, "resource", resource)
    monkeypatch.setattr(provider, "get_access", access)
    result = provision_pair(
        operation(),
        provider,
        {
            "pair_id": "shared",
            "isolation": "shared",
            "reporting_role": "cp_shared",
            "stage": "available",
        },
        MagicMock(),
    )
    assert result.control_url == URLS["control"]
    assert accesses == ["shared-control", "shared-data"]
    assert reads == [
        ("management", "cluster", "shared-control", "cluster-shared-control"),
        ("shared-control", "gateway", "gateway", "control"),
        ("management", "cluster", "shared-data", "cluster-shared-data"),
        ("shared-data", "gateway", "gateway", "data"),
    ]
    provider.commands.run.assert_not_called()


@pytest.mark.parametrize("kind", ["Demo.Platform/clusters", "Demo.Platform/gateways"])
@pytest.mark.parametrize("field", ["application", "environment"])
@pytest.mark.parametrize("foreign", [None, "/planes/radius/local/resourceGroups/foreign"])
def test_worker_refuses_wrong_live_radius_owners(provider, monkeypatch, kind, field, foreign):
    pending = operation()
    store = MagicMock()
    store.claim_pending.return_value = pending
    store.connection.execute.return_value.fetchone.return_value = {
        "pair_id": "shared",
        "isolation": "shared",
        "reporting_role": "cp_shared",
        "stage": "available",
    }

    def rad(slot, *args):
        resource_kind, name = args[2:4]
        application = args[args.index("--application") + 1]
        properties = {
            "provisioningState": "Succeeded",
            "application": f"{RADIUS_SCOPE}/providers/Applications.Core/applications/{application}",
            "environment": f"{RADIUS_SCOPE}/providers/Applications.Core/environments/"
            + (f"provision-{name}" if resource_kind.endswith("/clusters") else slot),
        }
        if resource_kind.endswith("/clusters"):
            properties["clusterId"] = provider.expected_cluster_id(name)
        else:
            properties["url"] = URLS[application]
        if resource_kind == kind:
            properties[field] = foreign
        return json.dumps({"properties": properties})

    monkeypatch.setattr(provider, "rad", rad)
    monkeypatch.setattr(
        provider,
        "get_access",
        lambda slot: SimpleNamespace(cluster_id=provider.expected_cluster_id(slot)),
    )
    assert provisioner.run_once(store, provider)
    store.complete.assert_not_called()
    assert store.observe.call_args.kwargs == {
        "status": "failed",
        "error_code": "pair_owner_mismatch",
    }
    provider.commands.run.assert_not_called()


def test_child_creation_uses_radius_and_exact_allocation(provider, monkeypatch):
    provider._verified = True
    allocation = provider.config.allocation("shared-control")
    cluster_id = pair(provider.config)["control_cluster_id"]
    provider.commands.run.return_value = json.dumps(
        {
            "properties": {
                "clusterId": cluster_id,
                "clusterName": allocation["clusterName"],
                "resourceGroup": allocation["clusterResourceGroup"],
                "radiusClientId": allocation["identities"]["radius"]["clientId"],
                "provisioningState": "Succeeded",
            }
        }
    )
    access = MagicMock(return_value="access")
    monkeypatch.setattr(provider, "get_access", access)
    assert provider.ensure_child_cluster("shared-control") == "access"
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert calls[0][0] == "rad" and "environments/azure.bicep" in " ".join(calls[0])
    assert calls[1][0] == "rad" and "child-cluster.bicep" in " ".join(calls[1])
    assert "--workspace" in calls[1] and "radplanes-management" in calls[1]
    assert calls[1][calls[1].index("--environment") + 1] == "provision-shared-control"
    assert calls[1][calls[1].index("--application") + 1] == "cluster-shared-control"
    parameters = json.loads(
        (provider.state / "management-child-cluster.parameters.json").read_text()
    )
    assert parameters["parameters"] == {"slot": {"value": "shared-control"}}
    assert "Demo.Platform/clusters" in calls[2]
    assert calls[2][calls[2].index("--application") + 1] == "cluster-shared-control"
    assert not any(command[:3] == ["az", "aks", "create"] for command in calls)
    access.assert_called_once_with("shared-control")


def test_get_credentials_is_explicit_and_non_admin(provider):
    _, path = provider.paths("shared-control")
    path.write_text("kubeconfig")
    cluster = provider.get_access("shared-control")
    commands = [call.args[0] for call in provider.commands.run.call_args_list]
    assert commands[0][:3] == ["az", "aks", "get-credentials"]
    assert "--subscription" in commands[0] and "--admin" not in commands[0]
    assert "--file" in commands[0] and str(path) in commands[0]
    assert "--context" in commands[1] and "azurecli" in commands[1]
    assert "--kubeconfig" in commands[2] and cluster.context in commands[2]
    assert path.stat().st_mode & 0o777 == 0o600


def test_recipe_registration_uses_two_workspaces_and_slot_parameters(provider, monkeypatch):
    monkeypatch.setattr(provider, "verify_recipes", MagicMock())
    provider.register("shared-control")
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    workspaces = [call for call in calls if "workspace" in call]
    assert len(workspaces) == 2
    assert "--environment" not in workspaces[0]
    assert workspaces[1][workspaces[1].index("--environment") + 1] == "shared-control"
    parameters = json.loads(
        (provider.state / "shared-control-environment.parameters.json").read_text()
    )
    recipe_map = parameters["parameters"]["recipes"]["value"]
    assert "Demo.Platform/clusters" not in recipe_map
    postgres = recipe_map["Demo.Platform/postgreSqlDatabases"]["default"]
    assert (
        postgres["parameters"]["delegatedSubnetId"]
        == provider.config.allocations["shared-control"]["postgresqlSubnetId"]
    )
    assert "administratorPassword" not in postgres["parameters"]
    assert all(
        call.kwargs["env"]["KUBECONFIG"].endswith("shared-control.kubeconfig")
        for call in provider.commands.run.call_args_list
    )
    assert all(
        call.kwargs["env"]["HOME"].endswith("/homes/radplanes-shared-control")
        for call in provider.commands.run.call_args_list
    )
    assert provider.commands.radius_environment.call_count == len(calls)
    management_map = provider.recipe_map("management")
    assert "Demo.Platform/clusters" not in management_map


@pytest.mark.parametrize("slot", ["shared-control", "isolated-1-data"])
def test_cluster_environment_uses_child_group_and_only_management_radius_identity(
    provider,
    raw_config,
    slot,
):
    management_radius = "44444444-4444-4444-4444-444444444444"
    child_radius = "55555555-5555-5555-5555-555555555555"
    raw_config["allocations"]["management"]["identities"]["radius"]["clientId"] = management_radius
    raw_config["allocations"][slot]["identities"]["radius"]["clientId"] = child_radius
    provider.config = OperatorConfig.from_dict(raw_config)
    provider._verified = True
    assert provider.register_cluster_environment(slot) == f"provision-{slot}"
    command = provider.commands.run.call_args.args[0]
    assert command[command.index("--workspace") + 1] == "radplanes-management"
    argument = command[command.index("--parameters") + 1]
    assert argument.startswith("@")
    parameters = json.loads(Path(argument[1:]).read_text())["parameters"]
    allocation = provider.config.allocations[slot]
    assert parameters["azureResourceGroup"]["value"] == allocation["clusterResourceGroup"]
    assert parameters["azureResourceGroup"]["value"] != allocation["appResourceGroup"]
    assert parameters["radiusClientId"]["value"] == management_radius
    assert parameters["radiusClientId"]["value"] not in (
        child_radius,
        provider.config.coordinator_identity["clientId"],
    )
    assert parameters["environmentName"]["value"] == f"provision-{slot}"
    assert parameters["namespace"]["value"] == f"radplanes-p-{slot}"
    recipes = parameters["recipes"]["value"]
    assert set(recipes) == {"Demo.Platform/clusters"}
    cluster_parameters = recipes["Demo.Platform/clusters"]["default"]["parameters"]
    assert set(cluster_parameters["allocations"]) == {slot}
    assert (
        cluster_parameters["allocations"][slot]["identities"]["radius"]["clientId"] == child_radius
    )
    assert cluster_parameters["tenantId"] == TENANT
    assert cluster_parameters["authorizedIpRanges"] == ["1.2.3.4/32", "5.6.7.8/32"]


def test_recipe_digest_and_both_immutable_locks_are_checked(provider):
    provider.commands.json.return_value = {
        "digest": "sha256:" + "a" * 64,
        "changeableAttributes": {"writeEnabled": False, "deleteEnabled": False},
    }
    provider.verify_recipes()
    assert provider.commands.json.call_count == 4
    provider.commands.json.return_value["changeableAttributes"]["deleteEnabled"] = True
    with pytest.raises(ProvisioningError, match="recipe_digest_or_lock_mismatch"):
        provider.verify_recipes()


@pytest.mark.parametrize("slot", ["management", "shared-control", "isolated-1-data"])
def test_registration_passes_slot_radius_identity_for_private_recipe_registry(
    provider,
    raw_config,
    monkeypatch,
    slot,
):
    radius_id = "44444444-4444-4444-4444-444444444444"
    raw_config["allocations"][slot]["identities"]["radius"]["clientId"] = radius_id
    provider.config = OperatorConfig.from_dict(raw_config)
    monkeypatch.setattr(provider, "verify_recipes", MagicMock())
    provider.register(slot)
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    deployment = next(command for command in calls if command[0] == "rad" and "deploy" in command)
    parameter_argument = deployment[deployment.index("--parameters") + 1]
    assert parameter_argument.startswith("@")
    parameters = json.loads(Path(parameter_argument[1:]).read_text())["parameters"]
    assert parameters["registryHost"]["value"] == "demoregistry.azurecr.io"
    assert parameters["radiusClientId"]["value"] == radius_id
    assert parameters["radiusClientId"]["value"] != provider.config.coordinator_identity["clientId"]
    assert parameters["azureTenantId"]["value"] == TENANT
    assert parameters["environmentName"]["value"] == slot
    assert not any(command[0] == "docker" for command in calls)


def test_run_loop_invokes_real_pair_driver_and_store(provider, monkeypatch):
    store = MagicMock()
    pending = operation()
    store.claim_pending.return_value = pending
    store.connection.execute.return_value.fetchone.return_value = pair(provider.config)
    created = []
    bootstrapped = []
    original = provider.ensure_child_cluster

    def create(slot):
        created.append(slot)
        return original(slot)

    def run_command(args, **_kwargs):
        if "show" in args and "Demo.Platform/clusters" in args:
            slot = args[args.index("Demo.Platform/clusters") + 1]
            allocation = provider.config.allocation(slot)
            return json.dumps(
                {
                    "properties": {
                        "clusterId": pair(provider.config)[
                            "control_cluster_id" if slot.endswith("control") else "data_cluster_id"
                        ],
                        "clusterName": allocation["clusterName"],
                        "resourceGroup": allocation["clusterResourceGroup"],
                        "radiusClientId": CLIENT,
                    }
                }
            )
        return ""

    provider._verified = True
    provider.commands.run.side_effect = run_command
    monkeypatch.setattr(provider, "ensure_child_cluster", create)
    monkeypatch.setattr(
        provider,
        "get_access",
        lambda slot: SimpleNamespace(
            slot=slot,
            cluster_id=pair(provider.config)[
                "control_cluster_id" if slot.endswith("control") else "data_cluster_id"
            ],
        ),
    )
    monkeypatch.setattr(
        provider, "bootstrap_child", lambda cluster: bootstrapped.append(cluster.slot)
    )
    monkeypatch.setattr(
        provider, "deploy_plane", lambda slot, observe: URLS[slot.rsplit("-", 1)[1]]
    )
    finished = False

    def sleep(seconds):
        nonlocal finished
        assert seconds == 5
        finished = True

    provisioner.run_loop(store, provider, sleep=sleep, stopped=lambda: finished, prepare=False)
    store.interrupt_running.assert_called_once_with()
    assert created == bootstrapped == ["shared-control", "shared-data"]
    store.complete.assert_called_once_with(pending.operation_id)
    assert [call.args[1] for call in store.observe.call_args_list] == [
        "control-cluster",
        "data-cluster",
        "control-radius",
        "data-radius",
    ]


def test_provider_failure_is_observed_without_retry(provider, monkeypatch):
    store = MagicMock()
    pending = operation()
    store.claim_pending.return_value = pending
    store.connection.execute.return_value.fetchone.return_value = pair(provider.config)
    monkeypatch.setattr(
        provider, "ensure_child_cluster", MagicMock(side_effect=ProvisioningError("command_failed"))
    )
    assert provisioner.run_once(store, provider)
    store.observe.assert_called_with(
        pending.operation_id, "control-cluster", status="failed", error_code="command_failed"
    )
    store.complete.assert_not_called()
    assert provider.ensure_child_cluster.call_count == 1


def test_database_connection_loss_is_fatal_and_does_not_report_success(provider, monkeypatch):
    store = MagicMock()
    store.claim_pending.return_value = operation()
    store.connection.execute.return_value.fetchone.return_value = pair(provider.config)
    monkeypatch.setattr(
        provider,
        "ensure_child_cluster",
        MagicMock(side_effect=psycopg.OperationalError("connection lost")),
    )
    with pytest.raises(psycopg.OperationalError):
        provisioner.run_once(store, provider)
    assert len(store.observe.call_args_list) == 1
    store.complete.assert_not_called()


def test_commands_stop_current_process_when_singleton_check_fails(tmp_path):
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise psycopg.OperationalError("connection lost")

    commands = Commands(tmp_path, guard)
    pidfile = tmp_path / "owned-process.pid"
    with pytest.raises(psycopg.OperationalError):
        commands.run(
            [
                sys.executable,
                "-c",
                "import os,time,pathlib;"
                "pathlib.Path('owned-process.pid').write_text(str(os.getpid()));"
                "time.sleep(30)",
            ]
        )
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_command_errors_preserve_safe_diagnostics_but_not_secrets(tmp_path, caplog):
    commands = Commands(tmp_path)
    commands.protect(SECRET)
    with caplog.at_level(logging.ERROR), pytest.raises(ProvisioningError, match="command_failed"):
        commands.run(
            [
                sys.executable,
                "-c",
                "import sys;"
                "sys.stderr.write('Azure DeploymentFailed: subnet rejected. '+sys.argv[1]);"
                "sys.exit(7)",
                SECRET,
            ]
        )
    assert "subnet rejected" in caplog.text and "exit=7" in caplog.text
    assert SECRET not in caplog.text and "[redacted]" in caplog.text


@pytest.fixture
def radius_command_setup(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    home = tmp_path / "operator-home"
    (home / ".kube").mkdir(parents=True)
    global_config = home / ".kube/config"
    global_config.write_text(json.dumps({"contexts": [{"name": "unrelated-user-context"}]}))
    bicep = home / ".rad/bin/bicep"
    bicep.parent.mkdir(parents=True)
    bicep.write_text("verified bundled compiler")
    bicep.chmod(0o700)
    state = root / ".state/azure"
    state.mkdir(parents=True)
    for slot in ("management", "shared-control"):
        (state / f"{slot}.kubeconfig").write_text(
            json.dumps(
                {
                    "contexts": [{"name": f"radplanes-{slot}"}],
                }
            )
        )
    tools = tmp_path / "tools"
    tools.mkdir()
    rad = tools / "rad"
    rad.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "home = pathlib.Path.home()\n"
        "kubeconfig = home / '.kube/config'\n"
        "context = sys.argv[sys.argv.index('--context') + 1]\n"
        "names = [item['name'] for item in json.loads(kubeconfig.read_text())['contexts']]\n"
        "if context not in names:\n"
        "    print(f'the kubeconfig does not contain a context called {context}',"
        " file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "compiler = home / '.rad/bin/bicep'\n"
        "assert compiler.read_text() == 'verified bundled compiler'\n"
        "print(json.dumps({'home': str(home), 'context': context,\n"
        "    'kubeconfig': str(kubeconfig.resolve()), 'compiler': str(compiler.resolve()),\n"
        "    'azureCache': os.environ['AZURE_CONFIG_DIR']}))\n"
    )
    rad.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)
    return SimpleNamespace(
        root=root, home=home, state=state, bicep=bicep, global_config=global_config
    )


@pytest.mark.parametrize("cache", ["default", "custom", "runtime"])
def test_rad_run_path_uses_scoped_home_even_when_kubeconfig_environment_is_ignored(
    radius_command_setup,
    config,
    monkeypatch,
    caplog,
    cache,
):
    setup = radius_command_setup
    original_config = setup.global_config.read_bytes()
    original_compiler = setup.bicep.read_bytes()
    expected_cache = setup.home / ".azure"
    if cache == "custom":
        expected_cache = setup.home / "custom-azure-cache"
        monkeypatch.setenv("AZURE_CONFIG_DIR", str(expected_cache))
    commands = Commands(setup.root)
    if cache == "runtime":
        expected_cache = setup.state / "az"
        commands.environment["AZURE_CONFIG_DIR"] = str(expected_cache)
    with caplog.at_level(logging.ERROR), pytest.raises(ProvisioningError, match="command_failed"):
        commands.run(
            [
                "rad",
                "workspace",
                "create",
                "kubernetes",
                "radplanes-management",
                "--context",
                "radplanes-management",
            ],
            env={"KUBECONFIG": str(setup.state / "management.kubeconfig")},
        )
    assert "the kubeconfig does not contain a context called radplanes-management" in caplog.text
    driver = AzureProvider(
        config,
        setup.root,
        credentials(setup.state / "credentials.json", config),
        commands,
    )
    for slot in ("management", "shared-control", "management"):
        context = f"radplanes-{slot}"
        output = json.loads(
            driver.rad(
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
        )
        scoped_home = setup.state / "homes" / context
        assert output == {
            "home": str(scoped_home),
            "context": context,
            "kubeconfig": str(setup.state / f"{slot}.kubeconfig"),
            "compiler": str(setup.bicep),
            "azureCache": str(expected_cache),
        }
        assert (scoped_home / ".kube/config").is_symlink()
        assert (scoped_home / ".rad/bin/bicep").is_symlink()
        assert scoped_home.stat().st_mode & 0o777 == 0o700
    assert setup.global_config.read_bytes() == original_config
    assert setup.bicep.read_bytes() == original_compiler
    assert os.environ["HOME"] == commands.environment["HOME"] == str(setup.home)


def test_radius_scoped_environment_preserves_inherited_pod_and_workload_identity_environment(
    radius_command_setup,
    config,
    monkeypatch,
):
    setup = radius_command_setup
    inherited = {
        "KUBERNETES_SERVICE_HOST": "172.20.0.1",
        "KUBERNETES_SERVICE_PORT": "443",
        "AZURE_CLIENT_ID": CLIENT,
        "AZURE_TENANT_ID": TENANT,
        "AZURE_FEDERATED_TOKEN_FILE": str(setup.state / "projected-azure-token"),
        "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com/",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    commands = Commands(setup.root)
    driver = AzureProvider(
        config,
        setup.root,
        credentials(setup.state / "credentials.json", config),
        commands,
    )
    kubeconfig = setup.state / "shared-control.kubeconfig"
    selected_home = setup.state / "homes/radplanes-shared-control"
    copied = commands.radius_environment(kubeconfig, "radplanes-shared-control")
    assert copied["KUBERNETES_SERVICE_HOST"] == inherited["KUBERNETES_SERVICE_HOST"]
    assert copied["KUBERNETES_SERVICE_PORT"] == inherited["KUBERNETES_SERVICE_PORT"]
    actual_popen = subprocess.Popen
    environments = []

    def capture(*args, **kwargs):
        environments.append(dict(kwargs["env"]))
        return actual_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", capture)
    output = json.loads(
        driver.rad(
            "shared-control",
            "workspace",
            "create",
            "kubernetes",
            "radplanes-shared-control",
            "--context",
            "radplanes-shared-control",
            "--force",
            workspace=False,
        )
    )
    assert output["context"] == "radplanes-shared-control"
    assert output["home"] == str(selected_home)
    assert output["kubeconfig"] == str(kubeconfig)
    assert len(environments) == 1
    child = environments[0]
    assert child["HOME"] == str(selected_home)
    assert child["KUBECONFIG"] == str(kubeconfig)
    for key, value in inherited.items():
        assert child[key] == copied[key] == value
        assert os.environ[key] == commands.environment[key] == value


@pytest.mark.parametrize(
    "target",
    [
        "environment",
        "argument",
        "joined-argument",
        "credential-file",
        "untargeted",
    ],
)
def test_kubeconfig_subprocesses_preserve_inherited_environment_without_speculative_filtering(
    radius_command_setup,
    monkeypatch,
    target,
):
    setup = radius_command_setup
    inherited = {
        "KUBERNETES_SERVICE_HOST": "172.20.0.1",
        "KUBERNETES_SERVICE_PORT": "443",
        "AZURE_CLIENT_ID": CLIENT,
        "AZURE_TENANT_ID": TENANT,
        "AZURE_FEDERATED_TOKEN_FILE": str(setup.state / "projected-azure-token"),
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    keys = [*inherited, "HOME", "KUBECONFIG"]
    program = (
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"print(json.dumps({{key: os.environ.get(key) for key in {keys!r}}}))\n"
    )
    script = setup.root / "echo-command-environment.py"
    script.write_text(program)
    kubeconfig = setup.state / "shared-control.kubeconfig"
    selected_home = setup.state / "homes/radplanes-shared-control"
    overrides = {"HOME": str(selected_home)}
    args = [sys.executable, str(script)]
    if target == "environment":
        overrides["KUBECONFIG"] = str(kubeconfig)
    elif target == "argument":
        args += ["--kubeconfig", str(kubeconfig)]
    elif target == "joined-argument":
        args.append(f"--kubeconfig={kubeconfig}")
    elif target == "credential-file":
        executable = Path(os.environ["PATH"].split(os.pathsep)[0]) / "az"
        executable.write_text(program)
        executable.chmod(0o700)
        args = ["az", "aks", "get-credentials", "--file", str(kubeconfig)]
    commands = Commands(setup.root)
    result = commands.json(args, env=overrides)
    assert result["HOME"] == str(selected_home)
    for key, value in inherited.items():
        assert result[key] == value
        assert os.environ[key] == commands.environment[key] == value
    if target == "environment":
        assert result["KUBECONFIG"] == str(kubeconfig)


@pytest.mark.parametrize("context", ["../management", "radplanes-management/other", "other"])
def test_radius_home_rejects_invalid_context_before_writing(radius_command_setup, context):
    setup = radius_command_setup
    with pytest.raises(ProvisioningError, match="invalid_radius_context"):
        Commands(setup.root).radius_environment(setup.state / "management.kubeconfig", context)
    assert not (setup.state / "homes").exists()


def test_radius_home_rejects_kubeconfig_outside_project_state(radius_command_setup):
    setup = radius_command_setup
    with pytest.raises(ProvisioningError, match="invalid_kubeconfig_path"):
        Commands(setup.root).radius_environment(setup.global_config, "radplanes-management")
    assert not (setup.state / "homes").exists()


def test_radius_home_requires_original_bundled_compiler(radius_command_setup):
    setup = radius_command_setup
    commands = Commands(setup.root)
    setup.bicep.unlink()
    with pytest.raises(ProvisioningError, match="radius_compiler_missing"):
        commands.radius_environment(setup.state / "management.kubeconfig", "radplanes-management")
    assert not (setup.state / "homes").exists()


def test_radius_home_refuses_to_retarget_existing_links(radius_command_setup):
    setup = radius_command_setup
    home = setup.state / "homes/radplanes-management"
    (home / ".kube").mkdir(parents=True)
    link = home / ".kube/config"
    link.symlink_to(setup.state / "shared-control.kubeconfig")
    with pytest.raises(ProvisioningError, match="radius_home_link_mismatch"):
        Commands(setup.root).radius_environment(
            setup.state / "management.kubeconfig",
            "radplanes-management",
        )
    assert link.resolve() == setup.state / "shared-control.kubeconfig"


def test_radius_home_never_writes_through_global_kube_directory_symlink(radius_command_setup):
    setup = radius_command_setup
    original = setup.global_config.read_bytes()
    home = setup.state / "homes/radplanes-management"
    home.mkdir(parents=True)
    (home / ".kube").symlink_to(setup.home / ".kube", target_is_directory=True)
    with pytest.raises(ProvisioningError, match="invalid_radius_home"):
        Commands(setup.root).radius_environment(
            setup.state / "management.kubeconfig",
            "radplanes-management",
        )
    assert setup.global_config.read_bytes() == original


def test_runtime_passwords_are_retained_and_dsn_has_verified_tls(tmp_path, config):
    path = tmp_path / "credentials.json"
    first = credentials(path, config)
    generated = copy.deepcopy(first.plane("management"))
    second = Credentials(path)
    assert second.plane("management") == generated
    assert path.stat().st_mode & 0o777 == 0o600
    parsed = conninfo_to_dict(database_dsn(database_properties(), "cp_api", SECRET))
    assert parsed["password"] == SECRET
    assert parsed["sslmode"] == "verify-full"
    assert parsed["sslrootcert"] == "/etc/ssl/certs/ca-certificates.crt"
    seed = first.runtime_seed(config)
    assert "mgmt_api" not in seed["planes"]["management"]["passwords"]
    assert "demoKey" not in seed["planes"]["management"]


def test_database_initialization_uses_secret_stdin_and_a_short_lived_job(provider, monkeypatch):
    applied = []
    properties = database_properties()
    del provider.credentials.plane("management")["database"]
    deployed = False

    def deploy(*_args):
        nonlocal deployed
        assert applied[0]["metadata"]["name"] == "database-init"
        assert "ROLE_PASSWORDS_JSON" in applied[0]["stringData"]
        assert "BOOTSTRAP_DSN" not in applied[0]["stringData"]
        assert not (provider.state / "management-database-intent.json").exists()
        deployed = True

    monkeypatch.setattr(provider, "deploy", deploy)
    monkeypatch.setattr(provider, "database_resource_exists", MagicMock(return_value=False))
    monkeypatch.setattr(provider, "resource", MagicMock(return_value=properties))
    monkeypatch.setattr(
        provider, "apply", lambda slot, payload, **kwargs: applied.append(copy.deepcopy(payload))
    )
    monkeypatch.setattr(
        provider,
        "kube_get",
        lambda slot, namespace, kind, name: (
            {"data": {"password": base64.b64encode(SECRET.encode()).decode()}}
            if name == "postgres-setup" and deployed
            else None
        ),
    )
    provider.initialize_database("management")
    intent, secret, job = applied
    assert intent["kind"] == "Secret"
    assert secret["kind"] == "Secret"
    assert secret["stringData"]["BOOTSTRAP_KIND"] == "management"
    assert SECRET in conninfo_to_dict(secret["stringData"]["BOOTSTRAP_DSN"])["password"]
    assert job["spec"]["backoffLimit"] == 0
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python", "-m", "plane_demo.setup.bootstrap"]
    assert container["envFrom"] == [{"secretRef": {"name": "database-init"}}]
    assert not any(value["kind"] == "ConfigMap" for value in applied)
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert "job/database-init" in calls[-1] and "secret/postgres-setup" in calls[-1]
    assert all(SECRET not in arg for command in calls for arg in command)


def test_committed_database_is_observed_instead_of_trusting_file_or_configmap_markers(
    provider, monkeypatch
):
    marker = {
        "data": {
            "serverId": "postgres-resource",
            "database": "management",
        }
    }
    monkeypatch.setattr(provider, "kube_get", lambda *args: marker)
    monkeypatch.setattr(provider, "database_resource_exists", lambda _: True)
    monkeypatch.setattr(provider, "resource", lambda *_: database_properties())
    (provider.state / "management-database-intent.json").write_text("{}")
    deploy = MagicMock()
    monkeypatch.setattr(provider, "deploy", deploy)
    provider.initialize_database("management")
    deploy.assert_not_called()
    inputs = [
        json.loads(call.kwargs["stdin"])
        for call in provider.commands.run.call_args_list
        if call.kwargs.get("stdin")
    ]
    secret = next(value for value in inputs if value.get("kind") == "Secret")
    assert secret["stringData"]["BOOTSTRAP_MODE"] == "observe"
    assert "ROLE_PASSWORDS_JSON" not in secret["stringData"]
    assert conninfo_to_dict(secret["stringData"]["BOOTSTRAP_DSN"])["user"] == "mgmt_provisioner"
    monkeypatch.setattr(provider, "database_resource_exists", lambda _: False)
    monkeypatch.setattr(
        provider,
        "kube_get",
        lambda slot, namespace, kind, name: {"exists": True} if name == "database-init" else None,
    )
    with pytest.raises(ProvisioningError, match="database_initialization_incomplete"):
        provider.initialize_database("management")
    deploy.assert_not_called()


def test_failed_database_observation_never_replays_initialization(provider, monkeypatch):
    monkeypatch.setattr(provider, "kube_get", lambda *_: None)
    monkeypatch.setattr(provider, "deploy", MagicMock())
    monkeypatch.setattr(provider, "database_resource_exists", lambda _: True)
    monkeypatch.setattr(provider, "resource", lambda *_: database_properties())

    def kubectl(*args, **kwargs):
        if "wait" in args:
            raise ProvisioningError("command_failed")
        return ""

    monkeypatch.setattr(provider, "kubectl", kubectl)
    cleanup = MagicMock()
    monkeypatch.setattr(provider, "cleanup_initialization", cleanup)
    with pytest.raises(ProvisioningError, match="command_failed"):
        provider.initialize_database("management")
    provider.deploy.assert_not_called()
    cleanup.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [
        "provisioningState",
        "application",
        "environment",
        "database",
        "username",
    ],
)
def test_database_owner_mismatch_stops_before_credentials_are_bound(provider, monkeypatch, field):
    properties = database_properties()
    properties[field] = "foreign"
    saved = provider.credentials.path.read_bytes()
    monkeypatch.setattr(provider, "database_resource_exists", lambda _: True)
    monkeypatch.setattr(provider, "resource", lambda *_: properties)
    with pytest.raises(ProvisioningError, match="database_owner_mismatch"):
        provider.initialize_database("management")
    assert provider.credentials.path.read_bytes() == saved
    provider.commands.run.assert_not_called()


def test_observer_job_collision_only_cleans_its_created_secret(provider, monkeypatch):
    monkeypatch.setattr(provider, "database_resource_exists", lambda _: True)
    monkeypatch.setattr(provider, "resource", lambda *_: database_properties())
    created = []

    def apply(slot, value, **kwargs):
        if value["kind"] == "Job":
            raise ProvisioningError("job_creation_failed")
        created.append(copy.deepcopy(value))

    monkeypatch.setattr(provider, "apply", apply)
    with pytest.raises(ProvisioningError, match="job_creation_failed"):
        provider.initialize_database("management")
    name = created[0]["metadata"]["name"]
    assert name.startswith("database-observe-")
    command = provider.commands.run.call_args.args[0]
    assert f"secret/{name}" in command
    assert not any(argument.startswith("job/") for argument in command)


@pytest.mark.parametrize(
    ("slot", "existing_kind", "existing_name"),
    [
        ("management", "secret", "postgres-setup"),
        ("management", "secret", "database-init"),
        ("management", "job", "database-init"),
        ("management", "secret", "management-api-runtime"),
        ("management", "secret", "provisioner-runtime"),
        ("shared-control", "secret", "control-api-runtime"),
        ("shared-control", "secret", "control-reconciler-runtime"),
    ],
)
def test_orphan_bootstrap_resources_prevent_new_database(
    provider, monkeypatch, slot, existing_kind, existing_name
):
    del provider.credentials.plane("management")["database"]
    provider.credentials.save()
    saved = provider.credentials.path.read_bytes()
    monkeypatch.setattr(provider, "deploy", MagicMock())
    monkeypatch.setattr(
        provider,
        "kube_get",
        lambda slot, namespace, kind, name: (
            {"metadata": {"name": name}} if (kind, name) == (existing_kind, existing_name) else None
        ),
    )
    provider.commands.run.return_value = "[]"
    with pytest.raises(ProvisioningError, match="database_initialization_incomplete"):
        provider.initialize_database(slot)
    provider.deploy.assert_not_called()
    assert provider.credentials.path.read_bytes() == saved
    assert not (provider.state / "management-database-intent.json").exists()
    assert not any("delete" in call.args[0] for call in provider.commands.run.call_args_list)
    args = provider.commands.run.call_args.args[0]
    assert "list" in args and "Demo.Platform/postgreSqlDatabases" in args
    assert "--group" in args and "radplanes" in args


def test_interruption_after_recipe_before_metadata_cannot_replay(provider, monkeypatch):
    del provider.credentials.plane("management")["database"]
    provider.credentials.save()
    passwords = copy.deepcopy(provider.credentials.plane("management")["passwords"])
    objects = {}

    def lookup(slot, namespace, kind, name):
        return objects.get((kind, name))

    def apply(slot, value, **kwargs):
        objects[(value["kind"].lower(), value["metadata"]["name"])] = copy.deepcopy(value)

    monkeypatch.setattr(provider, "kube_get", lookup)
    monkeypatch.setattr(provider, "apply", apply)
    provider.commands.run.return_value = "[]"
    deploy = MagicMock()
    monkeypatch.setattr(provider, "deploy", deploy)
    monkeypatch.setattr(
        provider,
        "resource",
        MagicMock(side_effect=ProvisioningError("command_failed")),
    )
    with pytest.raises(ProvisioningError, match="command_failed"):
        provider.initialize_database("management")
    deploy.assert_called_once_with(
        "management", "database", "management", {"databaseName": "management"}
    )
    assert not provider.credentials.has_database("management")
    assert ("secret", "database-init") in objects
    assert not (provider.state / "management-database-intent.json").exists()
    restarted = AzureProvider(
        provider.config,
        provider.root,
        Credentials(provider.credentials.path),
        provider.commands,
    )
    monkeypatch.setattr(restarted, "kube_get", lookup)
    monkeypatch.setattr(restarted, "deploy", MagicMock())
    with pytest.raises(ProvisioningError, match="database_initialization_incomplete"):
        restarted.initialize_database("management")
    restarted.deploy.assert_not_called()
    assert restarted.credentials.plane("management")["passwords"] == passwords


@pytest.mark.parametrize("output", ["", "{}", '[{"id":"missing-name"}]', '"not-a-list"'])
def test_database_inventory_errors_fail_closed_before_persisting_intent(
    provider,
    monkeypatch,
    output,
):
    del provider.credentials.plane("management")["database"]
    monkeypatch.setattr(provider, "kube_get", lambda *_: None)
    monkeypatch.setattr(provider, "deploy", MagicMock())
    provider.commands.run.return_value = output
    with pytest.raises(ProvisioningError, match="invalid_radius_output"):
        provider.initialize_database("management")
    provider.deploy.assert_not_called()
    assert not (provider.state / "management-database-intent.json").exists()


def test_runtime_secret_roles_are_separate(provider, monkeypatch):
    emitted = {}
    monkeypatch.setattr(
        provider, "secret", lambda slot, ns, name, values: emitted.update({name: values})
    )
    provider.credentials.ensure("shared-control", {"cp_api", "cp_reconciler", "dp_reconciler"})
    provider.credentials.set_database("shared-control", database_properties())
    for slot in ("management", "shared-control", "shared-data"):
        provider.runtime_secrets(slot)
    expected = {
        ("management-api-runtime", "MANAGEMENT_DSN"): "mgmt_api",
        ("provisioner-runtime", "MANAGEMENT_DSN"): "mgmt_provisioner",
        ("control-api-runtime", "CONTROL_DSN"): "cp_api",
        ("control-reconciler-runtime", "CONTROL_DSN"): "cp_reconciler",
        ("control-reconciler-runtime", "MANAGEMENT_DSN"): "cp_shared",
        ("data-reconciler-runtime", "CONTROL_DSN"): "dp_reconciler",
    }
    for (name, variable), role in expected.items():
        assert conninfo_to_dict(emitted[name][variable])["user"] == role
    assert "CONTROL_DSN" not in emitted["data-api-runtime"]
    assert "MANAGEMENT_DSN" not in emitted["data-api-runtime"]


def test_scoped_data_rbac_and_tokenless_public_accounts(provider, monkeypatch):
    resources = []
    monkeypatch.setattr(
        provider,
        "apply",
        lambda slot, values: resources.extend(values if isinstance(values, list) else [values]),
    )
    monkeypatch.setattr(provider, "kube_get", lambda *args: None)
    provider.prerequisites("shared-data")
    roles = {
        resource["metadata"]["name"]: resource
        for resource in resources
        if resource["kind"] == "Role"
    }
    assert set(roles) == {"data-api-configmaps", "data-reconciler-configmaps"}
    assert roles["data-api-configmaps"]["rules"][0]["verbs"] == ["get"]
    assert roles["data-reconciler-configmaps"]["rules"][0]["verbs"] == ["get", "create", "patch"]
    bindings = {
        resource["metadata"]["name"]: resource
        for resource in resources
        if resource["kind"] == "RoleBinding"
    }
    for account in ("data-api", "data-reconciler"):
        binding = bindings[account + "-configmaps"]
        assert binding["roleRef"]["name"] == account + "-configmaps"
        assert binding["subjects"] == [
            {
                "kind": "ServiceAccount",
                "name": "data-api-runtime" if account == "data-api" else account,
                "namespace": "radplanes-shared-data-data",
            }
        ]
    accounts = {
        resource["metadata"]["name"]: resource
        for resource in resources
        if resource["kind"] == "ServiceAccount"
    }
    assert accounts["data-api-runtime"]["automountServiceAccountToken"] is True
    assert accounts["data-api"]["automountServiceAccountToken"] is False
    challenge = next(
        resource
        for resource in resources
        if resource["kind"] == "ServiceAccount" and resource["metadata"]["name"] == "challenge"
    )
    assert challenge["automountServiceAccountToken"] is False
    resources.clear()
    provider.prerequisites("management")
    public = next(
        resource
        for resource in resources
        if resource["kind"] == "ServiceAccount" and resource["metadata"]["name"] == "management-api"
    )
    assert public["automountServiceAccountToken"] is False
    assert not public["metadata"].get("annotations")
    bindings = [resource for resource in resources if resource["kind"] == "ClusterRoleBinding"]
    assert len(bindings) == 1
    assert bindings[0]["roleRef"]["name"] == "radplanes-provisioner-radius-api"
    assert bindings[0]["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "provisioner",
            "namespace": "radplanes-management-management",
        }
    ]


def certificate_wrapper(uri):
    state = SimpleNamespace(
        existing=False,
        message=json.dumps({"certificateSecretUri": uri}),
    )

    def run_command(args, **kwargs):
        if len(args) > 1 and args[1].endswith("/operations/run-certificate-job.py"):
            return state.message
        if args[0] == "kubectl" and "get" in args and "job" in args:
            return '{"metadata":{"uid":"existing-job"}}' if state.existing else ""
        return ""

    state.run = run_command
    return state


def test_certificate_uses_parent_wrapper_with_exact_operator_arguments(provider):
    uri = "https://demo-vault.vault.azure.net/secrets/gateway-shared-control"
    issuer = certificate_wrapper(uri)
    provider.commands.run.side_effect = issuer.run
    assert provider.certificate("shared-control", "control.centralus.cloudapp.azure.com") == uri
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert calls[1] == [
        sys.executable,
        str(provider.root / "scripts/operations/run-certificate-job.py"),
        "--slot",
        "shared-control",
        "--context",
        "radplanes-shared-control",
        "--namespace",
        "radplanes-shared-control-control",
        "--kubeconfig",
        str(provider.state / "shared-control.kubeconfig"),
        "--domain",
        "control.centralus.cloudapp.azure.com",
        "--config",
        str(provider.config_path),
    ]
    assert len(calls) == 2
    assert "get" in calls[0] and "radplanes-system" in calls[0]
    assert not any("logs" in args or "create" in args or "apply" in args for args in calls)


def test_certificate_uses_portable_container_default(provider):
    provider.root = Path("/app")
    issuer = certificate_wrapper(
        "https://demo-vault.vault.azure.net/secrets/gateway-shared-control"
    )
    provider.commands.run.side_effect = issuer.run
    provider.certificate("shared-control", "control.centralus.cloudapp.azure.com")
    assert provider.commands.run.call_args.args[0][:2] == [
        "python",
        "/app/scripts/operations/run-certificate-job.py",
    ]


def test_certificate_command_override_is_an_immutable_argument_vector(provider, raw_config):
    raw_config["certificateCommand"] = [
        "custom-python",
        "/app/scripts/operations/run-certificate-job.py",
    ]
    provider.config = OperatorConfig.from_dict(raw_config)
    issuer = certificate_wrapper(
        "https://demo-vault.vault.azure.net/secrets/gateway-shared-control"
    )
    provider.commands.run.side_effect = issuer.run
    provider.certificate("shared-control", "control.centralus.cloudapp.azure.com")
    assert provider.commands.run.call_args.args[0][:2] == raw_config["certificateCommand"]
    raw_config["certificateCommand"] = "python scripts/operations/run-certificate-job.py"
    with pytest.raises(ValueError, match="argument vector"):
        OperatorConfig.from_dict(raw_config)


@pytest.mark.parametrize(
    "case,code",
    [
        ("wrong-vault", "invalid_certificate_uri"),
        ("versioned-uri", "invalid_certificate_uri"),
        ("malformed-json", "invalid_certificate_result"),
        ("extra-fields", "invalid_certificate_result"),
        ("missing-result", "invalid_certificate_result"),
        ("oversized-result", "invalid_certificate_result"),
    ],
)
def test_certificate_accepts_only_valid_parent_wrapper_output(provider, case, code):
    issuer = certificate_wrapper(
        "https://demo-vault.vault.azure.net/secrets/gateway-shared-control"
    )
    if case == "wrong-vault":
        issuer.message = json.dumps(
            {"certificateSecretUri": "https://wrong.vault.azure.net/secrets/x"}
        )
    elif case == "versioned-uri":
        issuer.message = json.dumps(
            {
                "certificateSecretUri": "https://demo-vault.vault.azure.net/secrets/gateway-shared-control/version"
            }
        )
    elif case == "malformed-json":
        issuer.message = "not JSON"
    elif case == "extra-fields":
        issuer.message = '{"certificateSecretUri":"x","privateMaterial":"never-read"}'
    elif case == "missing-result":
        issuer.message = ""
    else:
        issuer.message = "x" * 4097
    provider.commands.run.side_effect = issuer.run
    with pytest.raises(ProvisioningError, match=code):
        provider.certificate("shared-control", "control.centralus.cloudapp.azure.com")
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert not any("logs" in args or "delete" in args for args in calls)


def test_existing_certificate_job_is_not_replayed(provider):
    issuer = certificate_wrapper("unused")
    issuer.existing = True
    provider.commands.run.side_effect = issuer.run
    with pytest.raises(ProvisioningError, match="certificate_job_incomplete"):
        provider.certificate("shared-control", "control.centralus.cloudapp.azure.com")
    assert provider.commands.run.call_count == 1


def test_certificate_wrapper_error_propagates_without_retry(provider):
    provider.commands.run.side_effect = ["", ProvisioningError("command_failed")]
    with pytest.raises(ProvisioningError, match="command_failed"):
        provider.certificate("shared-control", "control.centralus.cloudapp.azure.com")
    assert provider.commands.run.call_count == 2


@pytest.mark.parametrize(
    "field",
    [
        "certificateName",
        "acmeStateSecretName",
        "certificateIssuerSubject",
    ],
)
def test_certificate_allocation_names_and_subject_must_match_prebound_scope(raw_config, field):
    raw_config["allocations"]["shared-control"][field] = "different-scope"
    with pytest.raises(ValueError, match="certificate allocation"):
        OperatorConfig.from_dict(raw_config)


def test_full_data_deploy_calls_certificate_then_https_and_preserves_it_on_reapply(
    provider,
    monkeypatch,
):
    provider._verified = True
    metadata_action = MagicMock()
    monkeypatch.setattr(provider, "tag_redis_nic", metadata_action)
    provider.credentials.ensure("shared-control", {"cp_api", "cp_reconciler", "dp_reconciler"})
    provider.credentials.set_database("shared-control", database_properties())
    host = "data.centralus.cloudapp.azure.com"
    uri = "https://demo-vault.vault.azure.net/secrets/gateway-shared-data"
    issuer = certificate_wrapper(uri)
    gateway_calls = 0

    def run_command(args, **kwargs):
        nonlocal gateway_calls
        if "list" in args and "Demo.Platform/gateways" in args:
            return json.dumps([{"name": "gateway"}] if gateway_calls else [])
        if "show" in args and "Demo.Platform/gateways" in args:
            gateway_calls += 1
            https = gateway_calls > 1
            return json.dumps(
                {
                    "properties": {
                        "provisioningState": "Succeeded",
                        "application": (
                            f"{RADIUS_SCOPE}/providers/Applications.Core/applications/data"
                        ),
                        "environment": (
                            f"{RADIUS_SCOPE}/providers/Applications.Core/environments/shared-data"
                        ),
                        "host": host,
                        "url": ("https://" if https else "http://") + host,
                        "certificateSecretUri": uri if https else "",
                    }
                }
            )
        return issuer.run(args, **kwargs)

    provider.commands.run.side_effect = run_command
    stages = []
    assert provider.deploy_plane("shared-data", stages.append) == "https://" + host
    assert stages == [
        "data-credentials",
        "data-application",
        "data-redis-metadata",
        "data-certificate",
        "data-redis-metadata",
    ]
    assert [call.args for call in metadata_action.call_args_list] == [
        ("shared-data",),
        ("shared-data",),
    ]
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    deploys = [index for index, args in enumerate(calls) if "deploy" in args and args[0] == "rad"]
    certificate_call = next(
        index
        for index, args in enumerate(calls)
        if len(args) > 1 and args[1].endswith("/operations/run-certificate-job.py")
    )
    assert deploys[0] < certificate_call < deploys[1]
    assert calls[-1] == [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "30",
        "https://" + host + "/livez",
    ]
    assert not any("tenants" in argument for args in calls for argument in args)
    output = json.loads((provider.state / "endpoints.json").read_text())
    assert output["pairs"]["shared"]["data"]["key_file"] == "shared-data.key"
    assert (provider.state / "shared-data.key").stat().st_mode & 0o777 == 0o600
    retained_passwords = provider.credentials.path.read_text()
    restarted = AzureProvider(
        provider.config,
        provider.root,
        Credentials(provider.credentials.path),
        provider.commands,
        workspace=provider.root / "fresh-worker",
    )
    restarted._verified = True
    monkeypatch.setattr(restarted, "tag_redis_nic", metadata_action)
    first_values = []
    original_deploy = restarted.deploy

    def observe_deploy(slot, template, application, values):
        first_values.append(dict(values))
        return original_deploy(slot, template, application, values)

    restarted.deploy = observe_deploy
    restarted.deploy_plane("shared-data")
    assert first_values[0]["gatewayPhase"] == "https"
    assert first_values[0]["certificateSecretUri"] == uri
    assert provider.credentials.path.read_text() == retained_passwords
    assert metadata_action.call_count == 4
    assert not (restarted.state / "shared-data-certificate.json").exists()
    monkeypatch.setattr(
        restarted, "certificate", MagicMock(side_effect=ProvisioningError("certificate_failed"))
    )
    before = len(first_values)
    with pytest.raises(ProvisioningError, match="certificate_failed"):
        restarted.deploy_plane("shared-data")
    assert first_values[before]["gatewayPhase"] == "https"


def test_management_deploy_preserves_coordinator_identity_and_certificate_command(
    provider,
    raw_config,
    monkeypatch,
):
    metadata_action = MagicMock()
    monkeypatch.setattr(provider, "tag_redis_nic", metadata_action)
    coordinator_id = "44444444-4444-4444-4444-444444444444"
    raw_config["coordinatorIdentity"]["clientId"] = coordinator_id
    provider.config = OperatorConfig.from_dict(raw_config)
    provider._verified = True
    host = "management.centralus.cloudapp.azure.com"
    uri = "https://demo-vault.vault.azure.net/secrets/gateway-management"
    wrapper = certificate_wrapper(uri)
    deployments = []
    gateway_reads = 0

    def run_command(args, **kwargs):
        nonlocal gateway_reads
        if "list" in args and "Demo.Platform/gateways" in args:
            return "[]"
        if "list" in args and "Demo.Platform/postgreSqlDatabases" in args:
            return json.dumps([{"name": "postgres"}])
        if "show" in args and "Demo.Platform/postgreSqlDatabases" in args:
            return json.dumps({"properties": database_properties()})
        if args[0] == "rad" and "deploy" in args:
            path = args[args.index("--parameters") + 1].removeprefix("@")
            deployments.append(json.loads(Path(path).read_text())["parameters"])
        if "show" in args and "Demo.Platform/gateways" in args:
            gateway_reads += 1
            return json.dumps(
                {
                    "properties": {
                        "host": host,
                        "url": ("http://" if gateway_reads == 1 else "https://") + host,
                        "certificateSecretUri": "" if gateway_reads == 1 else uri,
                    }
                }
            )
        return wrapper.run(args, **kwargs)

    provider.commands.run.side_effect = run_command
    assert provider.deploy_plane("management") == "https://" + host
    metadata_action.assert_not_called()
    assert len(deployments) == 2
    for parameters in deployments:
        assert parameters["provisionerClientId"]["value"] == coordinator_id
        assert parameters["provisionerWorkloadIdentity"]["value"] is True
    resources = []
    for call in provider.commands.run.call_args_list:
        if call.kwargs.get("stdin"):
            payload = json.loads(call.kwargs["stdin"])
            resources.extend(payload["items"] if payload["kind"] == "List" else [payload])
    settings = next(
        resource
        for resource in resources
        if resource["kind"] == "ConfigMap"
        and resource["metadata"]["name"] == "provisioning-settings"
    )
    runtime_config = json.loads(settings["data"]["provisioning.json"])
    assert runtime_config["certificateCommand"] == [
        "python",
        "/app/scripts/operations/run-certificate-job.py",
    ]
    assert provider.config.certificate_command == ()
    provisioner_account = next(
        resource
        for resource in resources
        if resource["kind"] == "ServiceAccount" and resource["metadata"]["name"] == "provisioner"
    )
    assert (
        provisioner_account["metadata"]["annotations"]["azure.workload.identity/client-id"]
        == coordinator_id
    )


def metadata_job_fixture(provider, monkeypatch, *, application="data", resource_name="redis"):
    scope = "/planes/radius/local/resourceGroups/radplanes/providers/"
    monkeypatch.setattr(
        provider,
        "resource",
        MagicMock(
            return_value={
                "provisioningState": "Succeeded",
                "environment": scope + "Applications.Core/environments/shared-data",
                "application": scope + f"Applications.Core/applications/{application}",
            }
        ),
    )
    group = provider.config.allocations["shared-data"]["appResourceGroupId"]
    result = {
        "cacheId": group + "/providers/Microsoft.Cache/redisEnterprise/amr-abcdefghijklm",
        "privateEndpointId": group
        + "/providers/Microsoft.Network/privateEndpoints/pe-amr-abcdefghijklm",
        "nicId": group + "/providers/Microsoft.Network/networkInterfaces/nic-amr-abcdefghijklm",
    }
    state = SimpleNamespace(
        created=[],
        result=result,
        job_status={"succeeded": 1},
        pod_phase="Succeeded",
        exit_code=0,
        account={
            "metadata": {
                "name": "applications-rp",
                "namespace": "radius-system",
                "annotations": {
                    "azure.workload.identity/client-id": CLIENT,
                    "azure.workload.identity/tenant-id": TENANT,
                },
            }
        },
    )

    def get(slot, namespace, kind, name, **kwargs):
        assert slot == "shared-data" and namespace == "radius-system"
        assert kwargs["timeout"] == 15
        if kind == "serviceaccount":
            return state.account
        return (
            {"metadata": {"uid": "metadata-job-uid"}, "status": state.job_status}
            if state.created
            else None
        )

    def create(slot, payload, **kwargs):
        assert kwargs == {"create": True, "timeout": 15}
        state.created.append(payload)

    def execute(args, **kwargs):
        if "pods" in args:
            return json.dumps(
                {
                    "items": [
                        {
                            "metadata": {
                                "ownerReferences": [
                                    {
                                        "uid": "metadata-job-uid",
                                        "kind": "Job",
                                        "name": "redis-nic-tags",
                                        "controller": True,
                                    }
                                ]
                            },
                            "status": {
                                "phase": state.pod_phase,
                                "containerStatuses": [
                                    {
                                        "name": "redis-nic-tags",
                                        "state": {
                                            "terminated": {
                                                "exitCode": state.exit_code,
                                                "message": json.dumps(state.result),
                                            }
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                }
            )
        return ""

    monkeypatch.setattr(provider, "kube_get", get)
    monkeypatch.setattr(provider, "apply", create)
    provider.commands.run.side_effect = execute
    return state


def test_metadata_job_uses_existing_radius_identity_and_privileged_image(provider, monkeypatch):
    state = metadata_job_fixture(provider, monkeypatch)
    assert provider.tag_redis_nic("shared-data") == state.result
    provider.resource.assert_called_once_with("shared-data", "redis", "redis", "data", timeout=30)
    assert len(state.created) == 1
    job = state.created[0]
    assert job["metadata"]["namespace"] == "radius-system"
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 180
    pod = job["spec"]["template"]
    assert pod["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    assert pod["spec"]["serviceAccountName"] == "applications-rp"
    assert pod["spec"]["automountServiceAccountToken"] is False
    container = pod["spec"]["containers"][0]
    assert container["image"] == provider.config.images["provisioner"]
    assert container["command"] == [
        "python",
        "-m",
        "plane_demo.management.providers.redis_nic_tags",
    ]
    assert container["terminationMessagePolicy"] == "File"
    target = json.loads(container["env"][0]["value"])
    assert target["client_id"] == CLIENT and target["tenant_id"] == TENANT
    assert target["resource_id"].endswith("/redisCaches/redis")
    assert target["tags"]["managedBy"] == "radius-todolist-app"
    assert not any(key.endswith("DSN") or "password" in key for key in target)
    assert "delete" in provider.commands.run.call_args.args[0]
    assert all(call.kwargs["timeout"] == 15 for call in provider.commands.run.call_args_list)


def test_metadata_job_uses_radius_identity_not_coordinator_identity(
    provider, raw_config, monkeypatch
):
    radius_client = "44444444-4444-4444-4444-444444444444"
    raw_config["allocations"]["shared-data"]["identities"]["radius"]["clientId"] = radius_client
    provider.config = OperatorConfig.from_dict(raw_config)
    state = metadata_job_fixture(provider, monkeypatch)
    state.account["metadata"]["annotations"]["azure.workload.identity/client-id"] = radius_client
    provider.tag_redis_nic("shared-data")
    target = json.loads(
        state.created[0]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"]
    )
    assert target["client_id"] == radius_client
    assert target["client_id"] != provider.config.coordinator_identity["clientId"]


def test_control_plane_deployment_does_not_run_redis_metadata(provider, monkeypatch):
    slot = "shared-control"
    uri = "https://demo-vault.vault.azure.net/secrets/gateway-shared-control"
    monkeypatch.setattr(provider, "resource_exists", lambda *args: False)
    for name in (
        "prerequisites",
        "initialize_database",
        "runtime_secrets",
        "deploy",
        "record_endpoint",
    ):
        monkeypatch.setattr(provider, name, MagicMock())
    monkeypatch.setattr(provider, "certificate", MagicMock(return_value=uri))
    monkeypatch.setattr(
        provider,
        "resource",
        MagicMock(
            side_effect=[
                {
                    "host": "control.centralus.cloudapp.azure.com",
                    "url": "http://control.centralus.cloudapp.azure.com",
                },
                {
                    "host": "control.centralus.cloudapp.azure.com",
                    "url": "https://control.centralus.cloudapp.azure.com",
                    "certificateSecretUri": uri,
                },
            ]
        ),
    )
    metadata_action = MagicMock()
    monkeypatch.setattr(provider, "tag_redis_nic", metadata_action)
    assert provider.deploy_plane(slot) == "https://control.centralus.cloudapp.azure.com"
    metadata_action.assert_not_called()


def test_data_deployment_stops_at_metadata_failure_without_publishing_an_endpoint(
    provider, monkeypatch
):
    monkeypatch.setattr(provider, "resource_exists", lambda *args: False)
    for name in ("prerequisites", "runtime_secrets", "deploy", "certificate", "record_endpoint"):
        monkeypatch.setattr(provider, name, MagicMock())
    monkeypatch.setattr(
        provider,
        "tag_redis_nic",
        MagicMock(side_effect=ProvisioningError("redis_nic_tag_conflict")),
    )
    observed = []
    with pytest.raises(ProvisioningError, match="redis_nic_tag_conflict"):
        provider.deploy_plane("shared-data", observed.append)
    assert observed[-1] == "data-redis-metadata"
    assert provider.deploy.call_count == 1
    provider.certificate.assert_not_called()
    provider.record_endpoint.assert_not_called()


@pytest.mark.parametrize("wrong", ["client", "tenant", "name", "namespace", "annotations"])
def test_metadata_job_refuses_service_account_identity_mismatch(provider, monkeypatch, wrong):
    state = metadata_job_fixture(provider, monkeypatch)
    record = state.account["metadata"]
    if wrong == "client":
        record["annotations"]["azure.workload.identity/client-id"] = "different"
    elif wrong == "tenant":
        record["annotations"]["azure.workload.identity/tenant-id"] = "different"
    elif wrong == "annotations":
        record["annotations"] = []
    else:
        record[wrong] = "different"
    with pytest.raises(ProvisioningError, match="redis_nic_identity_mismatch"):
        provider.tag_redis_nic("shared-data")
    assert not state.created


@pytest.mark.parametrize(
    "field,value",
    [
        ("provisioningState", "Failed"),
        ("application", "/other/application"),
        ("environment", "/other/environment"),
    ],
)
def test_metadata_job_requires_the_actual_succeeded_radius_linkage(
    provider,
    monkeypatch,
    field,
    value,
):
    state = metadata_job_fixture(provider, monkeypatch)
    provider.resource.return_value[field] = value
    with pytest.raises(ProvisioningError, match="redis_nic_radius_not_ready"):
        provider.tag_redis_nic("shared-data")
    assert not state.created


def test_metadata_job_surfaces_safe_helper_failure_without_deleting_evidence(provider, monkeypatch):
    state = metadata_job_fixture(provider, monkeypatch)
    state.job_status, state.pod_phase, state.exit_code = {"failed": 1}, "Failed", 1
    state.result = {"error_code": "redis_nic_ownership_mismatch"}
    with pytest.raises(ProvisioningError, match="redis_nic_ownership_mismatch"):
        provider.tag_redis_nic("shared-data")
    assert not any("delete" in call.args[0] for call in provider.commands.run.call_args_list)


def test_metadata_job_wait_is_bounded(provider, monkeypatch):
    from plane_demo.management.providers import azure

    state = metadata_job_fixture(provider, monkeypatch)
    state.job_status = {}
    ticks = iter([0, 1, 201])
    monkeypatch.setattr(azure.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(azure.time, "sleep", lambda _: None)
    with pytest.raises(ProvisioningError, match="redis_nic_timeout"):
        provider.tag_redis_nic("shared-data")


def test_same_metadata_helper_supports_a_separately_named_fresh_lifecycle_gate(
    provider, monkeypatch
):
    state = metadata_job_fixture(
        provider, monkeypatch, application="redis-life", resource_name="fresh"
    )
    provider.tag_redis_nic("shared-data", resource_name="fresh", application="redis-life")
    target = json.loads(
        state.created[0]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"]
    )
    assert target["resource_id"].endswith("/redisCaches/fresh")
    assert target["application_id"].endswith("/applications/redis-life")


@pytest.mark.parametrize("environment", ["azure", "local"])
def test_worker_rejects_legacy_file_startup_before_discovery(tmp_path, monkeypatch, environment):
    for name in PUBLIC_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROVIDER", environment)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("PROVISIONING_CONFIG", str(tmp_path / "missing-provisioning.json"))
    monkeypatch.setenv("PROVISIONING_CREDENTIALS_JSON", "legacy seed is not an authority")
    discovered, session = MagicMock(), MagicMock()
    monkeypatch.setattr(provisioner, "read_runtime_configuration", discovered)
    monkeypatch.setattr(provisioner, "provisioner_session", session)
    assert provisioner.main() == 1
    discovered.assert_not_called()
    session.assert_not_called()
    assert not (tmp_path / ".state").exists()


@pytest.fixture
def management_service_account(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "172.20.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: (
            True
            if str(path).startswith("/var/run/secrets/kubernetes.io/serviceaccount/")
            else original_is_file(path)
        ),
    )


def management_radius_read(args, **_kwargs):
    if "group" in args and "show" in args:
        return json.dumps({"id": RADIUS_SCOPE})
    if "environment" in args and "show" in args:
        return json.dumps({"id": MANAGEMENT_ENVIRONMENT})
    raise AssertionError("runtime startup must only read the Radius group/environment")


def test_management_radius_uses_scoped_rotating_service_account_credentials(
    provider,
    management_service_account,
):
    provider.commands.run.side_effect = management_radius_read
    provider.connect_management()
    kubeconfig = json.loads((provider.state / "management.kubeconfig").read_text())
    user = kubeconfig["users"][0]["user"]
    assert set(user) == {"tokenFile"}
    assert user["tokenFile"].endswith("/serviceaccount/token")
    assert kubeconfig["clusters"][0]["cluster"]["certificate-authority"].endswith("/ca.crt")
    assert not any(call.args[0][0] == "az" for call in provider.commands.run.call_args_list)
    assert "insecure-skip-tls-verify" not in json.dumps(kubeconfig)
    assert not any("workspace" in call.args[0] for call in provider.commands.run.call_args_list)
    config = json.loads(provider.radius_config.read_text())
    assert config["workspaces"]["items"]["radplanes-management"] == {
        "connection": {"context": "radplanes-management", "kind": "kubernetes"},
        "scope": RADIUS_SCOPE,
        "environment": MANAGEMENT_ENVIRONMENT,
    }
    assert config["workspaces"]["default"] == "radplanes-management"
    assert provider.radius_config.stat().st_mode & 0o777 == 0o600
    assert os.environ["KUBERNETES_SERVICE_HOST"] == "172.20.0.1"
    assert os.environ["KUBERNETES_SERVICE_PORT"] == "443"


def test_runtime_loop_seeds_workspace_without_helm_and_preserves_child_entries(
    provider,
    monkeypatch,
    management_service_account,
):
    provider.radius_config.write_text(
        "workspaces:\n"
        "  default: radplanes-shared-control\n"
        "  items:\n"
        "    radplanes-management:\n"
        "      connection:\n"
        "        context: stale-context\n"
        "        kind: kubernetes\n"
        "    radplanes-shared-control:\n"
        "      connection:\n"
        "        context: radplanes-shared-control\n"
        "        kind: kubernetes\n"
        f"      scope: {RADIUS_SCOPE}\n"
        f"      environment: {RADIUS_SCOPE}"
        "/providers/Applications.Core/environments/shared-control\n"
    )
    store = MagicMock()
    store.claim_pending.return_value = None
    monkeypatch.setattr(provider, "authenticate", MagicMock())
    monkeypatch.setattr(provider, "verify_recipes", MagicMock())
    stopped = False

    def sleep(seconds):
        nonlocal stopped
        assert seconds == 5
        stopped = True

    def read(args, **kwargs):
        config = json.loads(provider.radius_config.read_text())
        assert config["workspaces"]["default"] == "radplanes-management"
        return management_radius_read(args, **kwargs)

    provider.commands.run.side_effect = read
    provisioner.run_loop(store, provider, sleep=sleep, stopped=lambda: stopped)
    config = json.loads(provider.radius_config.read_text())
    child = config["workspaces"]["items"]["radplanes-shared-control"]
    assert child["connection"]["context"] == "radplanes-shared-control"
    assert child["scope"] == RADIUS_SCOPE
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert len(calls) == 2
    assert all("show" in command and "workspace" not in command for command in calls)
    store.interrupt_running.assert_called_once_with()
    store.claim_pending.assert_called_once_with()
    rules = provider.management_permissions("radplanes-management-management")[0]["rules"]
    assert all(
        "secrets" not in rule["resources"] and "*" not in rule["resources"] for rule in rules
    )


@pytest.mark.parametrize(
    "document",
    [
        "workspaces: [",
        "[]",
        "workspaces: []",
        "workspaces:\n  items: []\n",
    ],
)
def test_runtime_workspace_rejects_malformed_local_configuration(provider, document):
    provider.radius_config.write_text(document)
    with pytest.raises(ProvisioningError, match="invalid_radius_config"):
        provider.seed_management_workspace()
    assert provider.radius_config.read_text() == document
    provider.commands.run.assert_not_called()


def test_management_worker_gets_only_its_radius_api_plane(provider):
    resources = provider.management_permissions("radplanes-management-management")
    role = next(item for item in resources if item["kind"] == "ClusterRole")
    assert role["rules"] == [
        {
            "apiGroups": ["api.ucp.dev"],
            "resources": ["planes/local"],
            "resourceNames": ["radius"],
            "verbs": ["get", "list", "create", "update", "delete"],
        }
    ]
    binding = next(item for item in resources if item["kind"] == "ClusterRoleBinding")
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "provisioner",
            "namespace": "radplanes-management-management",
        }
    ]
    assert binding["roleRef"]["name"] == role["metadata"]["name"]


def test_runtime_workspace_rejects_a_redirected_config_file(provider):
    foreign = provider.state / "foreign-radius.yaml"
    foreign.write_text("workspaces: {}\n")
    provider.radius_config.symlink_to(foreign)
    with pytest.raises(ProvisioningError, match="invalid_radius_config"):
        provider.seed_management_workspace()
    assert foreign.read_text() == "workspaces: {}\n"


@pytest.mark.parametrize(
    "output,code",
    [
        ("not-json", "invalid_radius_output"),
        ('{"id":null}', "invalid_radius_output"),
        ('{"id":"/planes/radius/local/resourceGroups/other"}', "management_radius_mismatch"),
    ],
)
def test_runtime_workspace_does_not_claim_work_when_radius_verification_fails(
    provider,
    monkeypatch,
    management_service_account,
    output,
    code,
):
    store = MagicMock()
    monkeypatch.setattr(provider, "authenticate", MagicMock())
    provider.commands.run.return_value = output
    with pytest.raises(ProvisioningError, match=code):
        provisioner.run_loop(store, provider)
    store.claim_pending.assert_not_called()
    assert not any("workspace" in call.args[0] for call in provider.commands.run.call_args_list)


def test_runtime_requires_exact_preassigned_workload_identity(provider, tmp_path, monkeypatch):
    monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE", raising=False)
    with pytest.raises(ProvisioningError, match="workload_identity_required"):
        provider.authenticate(workload_required=True)
    token = tmp_path / "federated-token"
    token.write_text("eyJfake.payload.signature")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token))
    monkeypatch.setenv("AZURE_CLIENT_ID", CLIENT)
    monkeypatch.setenv("AZURE_TENANT_ID", TENANT)
    provider.commands.json.return_value = {"id": SUBSCRIPTION, "tenantId": TENANT}
    provider.authenticate(workload_required=True)
    assert provider.commands.run.call_args.args[0][:2] == ["az", "login"]
    monkeypatch.setenv("AZURE_CLIENT_ID", "wrong-identity")
    with pytest.raises(ProvisioningError, match="workload_identity_mismatch"):
        provider.authenticate(workload_required=True)


@pytest.fixture
def azure_runtime_apis(selected_config, tmp_path, monkeypatch):
    identity = DemoConfig.from_values(selected_config.bootstrap_settings)
    account = tmp_path / "service-account"
    account.mkdir()
    (account / "namespace").write_text(identity.namespace("management"))
    monkeypatch.setattr(discovery, "SERVICE_ACCOUNT", account)
    monkeypatch.setattr(discovery.kube_config, "load_incluster_config", lambda **kwargs: None)
    connection, core, apps = MagicMock(), MagicMock(), MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = connection
    monkeypatch.setattr(discovery.client, "ApiClient", lambda settings: manager)
    monkeypatch.setattr(discovery.client, "CoreV1Api", lambda _: core)
    monkeypatch.setattr(discovery.client, "AppsV1Api", lambda _: apps)
    core.read_namespace.return_value = SimpleNamespace(
        metadata=SimpleNamespace(
            name=identity.namespace("management"),
            labels={
                "plane-demo/project": identity.project,
                "plane-demo/deployment": identity.deployment,
                "plane-demo/environment": identity.environment,
            },
        )
    )
    resource_id = (
        f"/planes/radius/local/resourceGroups/{identity.stem}"
        "/providers/Applications.Core/environments/management"
    )
    recipes = {
        kind: {
            "reference": f"{identity.registry_name}.azurecr.io/radius-recipes/{kind}:src-"
            + "c" * 64,
            "digest": "sha256:" + "d" * 64,
            "immutability": "acr-abac-arm-import-v1",
        }
        for kind in TYPES
    }
    environment = {
        "id": resource_id,
        "properties": {
            "compute": {
                "kind": "kubernetes",
                "resourceId": "self",
                "namespace": f"{identity.stem}-management",
            },
            "recipes": {
                resource_type: {
                    "default": {
                        "templateKind": "bicep",
                        "templatePath": recipes[kind]["reference"],
                    }
                }
                for kind, (resource_type, _) in TYPES.items()
            },
        },
    }
    connection.call_api.return_value = environment

    def deployment(name, namespace, **kwargs):
        role = "api" if name == "management-api" else "provisioner"
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name, namespace=namespace),
            spec=SimpleNamespace(
                template=SimpleNamespace(
                    spec=SimpleNamespace(
                        service_account_name=name,
                        containers=[SimpleNamespace(name=name, image=selected_config.images[role])],
                    )
                )
            ),
        )

    apps.read_namespaced_deployment.side_effect = deployment
    values = selected_config.to_dict()
    outputs = {
        key: values[key] for key in ("foundation", "coordinatorIdentity", "managementCluster")
    }
    outputs["allocations"] = list(values["allocations"].values())
    bootstrap = {
        "properties": {
            "provisioningState": "Succeeded",
            "outputs": {key: {"value": value} for key, value in outputs.items()},
        }
    }
    commands = []
    workspaces = []
    login = MagicMock(side_effect=lambda command, workspace, *_: workspaces.append(workspace))
    monkeypatch.setattr(discovery, "login_workload_identity", login)

    def az(command, arguments):
        commands.append(arguments)
        assert arguments[arguments.index("--subscription") + 1] == identity.subscription
        if arguments[1:4] == ["deployment", "sub", "show"]:
            assert arguments[arguments.index("--name") + 1] == identity.stem + "-bootstrap"
            return bootstrap
        assert arguments[1:4] == ["acr", "repository", "show"]
        assert arguments[arguments.index("--name") + 1] == identity.registry_name
        image = arguments[arguments.index("--image") + 1]
        recipe = next(value for value in recipes.values() if value["reference"].endswith(image))
        return {"digest": recipe["digest"]}

    monkeypatch.setattr(Commands, "json", az)
    monkeypatch.setenv("AZURE_CLIENT_ID", selected_config.coordinator_identity["clientId"])
    monkeypatch.setenv("AZURE_TENANT_ID", selected_config.foundation["tenantId"])
    return SimpleNamespace(
        identity=identity,
        expected=selected_config,
        core=core,
        apps=apps,
        connection=connection,
        environment=environment,
        recipes=recipes,
        bootstrap=bootstrap,
        commands=commands,
        workspaces=workspaces,
        login=login,
    )


def test_azure_worker_reconstructs_configuration_from_current_apis(azure_runtime_apis, tmp_path):
    api = azure_runtime_apis
    result = discovery.read_runtime_configuration(api.identity, tmp_path)
    assert result.identity == api.identity
    assert result.foundation == api.expected.foundation
    assert result.allocations == api.expected.allocations
    assert result.images == api.expected.images
    assert result.recipes == api.recipes
    assert len(api.commands) == 5
    assert api.workspaces and all(not path.exists() for path in api.workspaces)
    api.login.assert_called_once()
    api.connection.call_api.assert_called_once_with(
        "/apis/api.ucp.dev/v1alpha3" + api.environment["id"],
        "GET",
        query_params=[("api-version", "2023-10-01-preview")],
        response_type="object",
        auth_settings=["BearerToken"],
        _return_http_data_only=True,
        _request_timeout=(5, 15),
    )
    assert not (tmp_path / ".state").exists()


@pytest.mark.parametrize(
    ("drift", "code"),
    [
        ("namespace", "management_namespace_mismatch"),
        ("radius-owner", "management_radius_mismatch"),
        ("radius-namespace", "management_radius_mismatch"),
        ("foundation", "foundation_not_ready"),
        ("workload-identity", "workload_identity_mismatch"),
        ("recipe-kind", "management_recipe_binding_mismatch"),
        ("recipe-registry", "management_recipe_binding_mismatch"),
        ("malformed-environment", "runtime_discovery_contract_invalid"),
    ],
)
def test_azure_runtime_discovery_refuses_wrong_owners_and_bindings(
    azure_runtime_apis, tmp_path, drift, code
):
    api = azure_runtime_apis
    binding = api.environment["properties"]["recipes"]["Demo.Platform/clusters"]["default"]
    if drift == "namespace":
        api.core.read_namespace.return_value.metadata.labels["plane-demo/deployment"] = "other"
    elif drift == "radius-owner":
        api.environment["id"] = "/planes/radius/local/resourceGroups/other"
    elif drift == "radius-namespace":
        api.environment["properties"]["compute"]["namespace"] = api.identity.namespace("management")
    elif drift == "foundation":
        api.bootstrap["properties"]["provisioningState"] = "Running"
    elif drift == "workload-identity":
        api.bootstrap["properties"]["outputs"]["coordinatorIdentity"]["value"]["clientId"] = (
            "44444444-4444-4444-4444-444444444444"
        )
    elif drift == "recipe-kind":
        binding["templateKind"] = "terraform"
    elif drift == "recipe-registry":
        binding["templatePath"] = "other.azurecr.io/recipes/cluster:v1"
    else:
        api.connection.call_api.return_value = []
    with pytest.raises(ProvisioningError, match=code):
        discovery.read_runtime_configuration(api.identity, tmp_path)
    assert all(not path.exists() for path in api.workspaces)
    assert not (tmp_path / ".state").exists()


@pytest.mark.parametrize("unsafe", [False, True])
def test_selected_recipe_consumer_checks_effective_policy_before_tags(
    selected_config, tmp_path, monkeypatch, unsafe
):
    raw = selected_config.to_dict()
    identity = selected_config.identity
    for kind, recipe in raw["recipes"].items():
        recipe.update(
            reference=f"{identity.registry_name}.azurecr.io/radius-recipes/{kind}:src-" + "c" * 64,
            immutability="acr-abac-arm-import-v1",
        )
    config = OperatorConfig.from_dict(raw, identity=identity)
    root = Path(__file__).resolve().parents[2]
    provider = AzureProvider(
        config,
        root,
        credentials(tmp_path / "credentials.json", config),
        workspace=tmp_path / "work",
    )
    registry_id = (
        PREFIX
        + f"rg-{identity.stem}-platform/providers/Microsoft.ContainerRegistry/registries/"
        + identity.registry_name
    )
    policy = json.loads((root / "scripts/operations/azure/registry-policy.json").read_text())
    role_ids = {
        policy[key]
        for key in ("repositoryReaderRoleId", "repositoryWriterRoleId", "dataImporterRoleId")
    }
    calls = []

    def az(*arguments):
        calls.append(arguments)
        if arguments[:2] == ("acr", "show"):
            return {
                "id": registry_id,
                "roleAssignmentMode": "AbacRepositoryPermissions",
                "adminUserEnabled": False,
                "anonymousPullEnabled": False,
                "loginServer": config.foundation["registryLoginServer"],
                "tags": {"project": identity.project, "deployment": identity.deployment},
            }
        if arguments[:3] == ("role", "assignment", "list"):
            assert arguments[arguments.index("--scope") + 1] == registry_id
            assert "--include-inherited" in arguments
            assert "--all" not in arguments
            return [
                {
                    "roleDefinitionId": (
                        f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/"
                        f"roleDefinitions/{role_id}"
                    ),
                    **(
                        {
                            "conditionVersion": policy["conditionVersion"],
                            "condition": policy["writerCondition"],
                        }
                        if role_id == policy["repositoryWriterRoleId"] and not unsafe
                        else {}
                    ),
                }
                for role_id in role_ids
            ]
        if arguments[:3] == ("role", "definition", "list"):
            assert arguments[arguments.index("--scope") + 1] == registry_id
            role_id = arguments[arguments.index("--name") + 1]
            assert role_id in role_ids
            return [
                {
                    "name": role_id,
                    "permissions": [
                        {
                            "actions": ["*/read"],
                            "dataActions": (
                                [
                                    "Microsoft.ContainerRegistry/registries/repositories/content/write"
                                ]
                                if role_id == policy["repositoryWriterRoleId"]
                                else []
                            ),
                        }
                    ],
                }
            ]
        assert arguments[:3] == ("acr", "repository", "show")
        return {"digest": "sha256:" + "a" * 64}

    monkeypatch.setattr(provider, "az", az)
    if unsafe:
        with pytest.raises(ProvisioningError):
            provider.verify_recipes()
        assert provider._verified is False
        assert not any(call[:3] == ("acr", "repository", "show") for call in calls)
    else:
        provider.verify_recipes()
        assert provider._verified is True
        assert sum(call[:3] == ("acr", "repository", "show") for call in calls) == 4
        assert calls[0] == ("acr", "show", "--name", identity.registry_name)


def test_selected_azure_main_uses_key_vault_without_seed_or_persistent_workspace(
    tmp_path, selected_config, monkeypatch
):
    identity = DemoConfig.from_values(selected_config.bootstrap_settings)
    discovery = MagicMock(return_value=selected_config)
    monkeypatch.setattr(provisioner, "read_runtime_configuration", discovery)
    for name, value in identity.public_values().items():
        monkeypatch.setenv(name, value)
    identity_module, azure_module = ModuleType("azure.identity"), ModuleType("azure")
    azure_module.__path__ = []

    @contextmanager
    def credential():
        yield object()

    identity_module.WorkloadIdentityCredential = credential
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.identity", identity_module)
    properties = database_properties()
    scope = (
        f"/planes/radius/local/resourceGroups/{selected_config.radius_group}"
        "/providers/Applications.Core"
    )
    properties.update(
        application=f"{scope}/applications/management",
        environment=f"{scope}/environments/management",
    )
    values = {
        role: f"synthetic-{role}-" + "x" * 48
        for role in credential_roles(selected_config, "management")
    }
    backend = MagicMock()
    backend.scope = CredentialScope(
        selected_config.identity.project, selected_config.identity.deployment, "azure"
    )
    backend.get.side_effect = lambda slot, role: CredentialValue(values[role])
    factory = MagicMock(return_value=backend)
    monkeypatch.setattr(provisioner, "azure_key_vault_store", factory)
    monkeypatch.setattr(AzureProvider, "resource", lambda *args: properties)
    workspaces = []

    def authenticate(provider, **kwargs):
        assert isinstance(provider.credentials, StoredCredentials)
        workspaces.append(provider.state)

    monkeypatch.setattr(AzureProvider, "authenticate", authenticate)
    monkeypatch.setattr(AzureProvider, "connect_management", lambda _: None)
    monkeypatch.setattr(AzureProvider, "verify_recipes", lambda _: None)
    for name, value in {
        "PROVIDER": "azure",
        "PROJECT_ROOT": str(tmp_path),
        "PROVISIONING_CONFIG": str(tmp_path / "missing-inventory.json"),
        "PROVISIONING_CREDENTIALS_JSON": "not a credential seed",
        "AZURE_CLIENT_ID": selected_config.coordinator_identity["clientId"],
        "AZURE_TENANT_ID": selected_config.foundation["tenantId"],
        "MANAGEMENT_DSN": database_dsn(properties, "mgmt_provisioner", values["mgmt_provisioner"]),
    }.items():
        monkeypatch.setenv(name, value)
    operations, sessions = MagicMock(), []
    operations.claim_pending.return_value = None

    @contextmanager
    def session(dsn):
        sessions.append(dsn)
        yield operations

    monkeypatch.setattr(provisioner, "provisioner_session", session)
    original, stopped = provisioner.run_loop, []
    monkeypatch.setattr(
        provisioner,
        "run_loop",
        lambda operations, provider: original(
            operations,
            provider,
            sleep=lambda _: stopped.append(True),
            stopped=lambda: bool(stopped),
        ),
    )
    assert provisioner.main() == 0
    discovery.assert_called_once_with(identity, tmp_path)
    assert len(sessions) == 1
    operations.interrupt_running.assert_called_once()
    operations.claim_pending.assert_called_once()
    factory.assert_called_once()
    assert (
        factory.call_args.args[1]
        == f"https://{selected_config.foundation['vaultName']}.vault.azure.net"
    )
    assert factory.call_args.kwargs["singleton_writer"] is True
    assert callable(factory.call_args.kwargs["singleton_guard"])
    assert workspaces and all(not workspace.exists() for workspace in workspaces)
    assert not (tmp_path / ".state").exists()
    backend.close.assert_called_once()
    backend.get_or_create.assert_not_called()


def test_local_startup_fails_without_a_fake_provider(monkeypatch):
    monkeypatch.setenv("PROVIDER", "local")
    assert provisioner.main() == 1


@pytest.mark.parametrize("slot", ["management", "shared-control"])
def test_incluster_management_command_uses_only_the_guarded_service_provider(
    selected_config, tmp_path, monkeypatch, capsys, slot
):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts/operations"))
    spec = importlib.util.spec_from_file_location(
        "incluster_management_entrypoint", root / "scripts/operations/deploy-plane.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    path = tmp_path / "job-inputs.json"
    path.write_text(json.dumps(selected_config.to_dict()))
    provided = "synthetic-provided-shared-key-" + "z" * 48
    monkeypatch.setenv("DEMO_KEY_SHARED_DATA", provided)
    monkeypatch.setattr(sys, "argv", ["deploy-plane.py", "--config", str(path), "--slot", slot])
    active, writer = MagicMock(), MagicMock()
    provider = MagicMock()
    provider.credentials = MagicMock(spec=StoredCredentials)
    provider.deploy_plane.return_value = "https://management.centralus.cloudapp.azure.com"
    events = []

    @contextmanager
    def guards(config):
        assert config.identity.demo_keys["shared-data"] == provided
        events.append("lock")
        yield active, writer
        events.append("unlock")

    @contextmanager
    def factory(config, root, **kwargs):
        assert kwargs == {"guard": active, "writer_guard": writer}
        assert config.identity.demo_keys["shared-data"] == provided
        events.append("provider")
        yield provider
        events.append("closed")

    monkeypatch.setattr(script, "bootstrap_guards", guards)
    monkeypatch.setattr(script, "service_provider", factory)
    result = script.main()
    output = capsys.readouterr()
    if slot != "management":
        assert result == 1 and "children_are_provisioner_owned" in output.err
        assert not events
        return
    assert result == 0
    assert events == ["lock", "provider", "closed", "unlock"]
    assert json.loads(output.out) == {
        "slot": "management",
        "url": provider.deploy_plane.return_value,
    }
    provider.authenticate.assert_called_once_with(workload_required=True)
    provider.get_access.assert_called_once_with("management")
    provider.register.assert_called_once_with("management")
    provider.credentials.seed_provided_keys.assert_called_once_with()
    provider.deploy_plane.assert_called_once_with("management")
    assert not (tmp_path / ".state").exists()


def test_entrypoints_are_cloud_free_for_help():
    root = Path(__file__).resolve().parents[2]
    for script in ("deploy-plane.py", "register-radius.py", "run-certificate-job.py"):
        result = subprocess.run(
            [sys.executable, str(root / "scripts/operations" / script), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0 and "--slot" in result.stdout
