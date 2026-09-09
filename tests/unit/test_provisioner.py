import base64
import copy
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from plane_demo.management import provisioner
from plane_demo.management.providers.azure import AzureProvider
from plane_demo.management.providers.commands import Commands
from plane_demo.management.providers.credentials import Credentials, database_dsn
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


def database_properties():
    return {
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


def test_shared_reuse_runs_no_radius_or_provider_commands(provider):
    observe = MagicMock()
    existing = pair(provider.config, available=True)
    result = provision_pair(operation(), provider, existing, observe)
    assert asdict(result) == {key: existing[key] for key in asdict(result)}
    observe.assert_called_once_with("reuse-pair")
    provider.commands.run.assert_not_called()
    provider.commands.json.assert_not_called()


def test_reuse_rejects_wrong_cluster_inventory(provider):
    existing = pair(provider.config, available=True)
    existing["control_cluster_id"] = "unowned"
    with pytest.raises(ProvisioningError, match="pair_inventory_mismatch"):
        provision_pair(operation(), provider, existing, MagicMock())


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
    result = pair(provider.config)
    store.complete.assert_called_once_with(
        pending.operation_id, **{key: result[key] for key in PairResult.__dataclass_fields__}
    )
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
        intent = provider.state / "management-database-intent.json"
        assert json.loads(intent.read_text())["resourceName"] == "postgres"
        assert intent.stat().st_mode & 0o777 == 0o600
        deployed = True

    monkeypatch.setattr(provider, "deploy", deploy)
    monkeypatch.setattr(provider, "database_resource_exists", MagicMock(return_value=False))
    monkeypatch.setattr(provider, "resource", MagicMock(return_value=properties))
    monkeypatch.setattr(provider, "apply", lambda slot, payload, **kwargs: applied.append(payload))
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
    secret, job, marker = applied
    assert secret["kind"] == "Secret"
    assert secret["stringData"]["BOOTSTRAP_KIND"] == "management"
    assert SECRET in conninfo_to_dict(secret["stringData"]["BOOTSTRAP_DSN"])["password"]
    assert job["spec"]["backoffLimit"] == 0
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python", "-m", "plane_demo.setup.bootstrap"]
    assert container["envFrom"] == [{"secretRef": {"name": "database-init"}}]
    assert marker["metadata"]["name"] == "database-initialized"
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert "job/database-init" in calls[-1] and "secret/postgres-setup" in calls[-1]
    assert all(SECRET not in arg for command in calls for arg in command)


def test_database_marker_skips_reinitialization_and_partial_job_is_not_replayed(
    provider, monkeypatch
):
    marker = {
        "data": {
            "serverId": "postgres-resource",
            "database": "management",
        }
    }
    monkeypatch.setattr(provider, "kube_get", lambda *args: marker)
    (provider.state / "management-database-intent.json").write_text("{}")
    deploy = MagicMock()
    monkeypatch.setattr(provider, "deploy", deploy)
    provider.initialize_database("management")
    deploy.assert_not_called()
    monkeypatch.setattr(
        provider,
        "kube_get",
        lambda slot, namespace, kind, name: {"exists": True} if name == "database-init" else None,
    )
    with pytest.raises(ProvisioningError, match="database_initialization_incomplete"):
        provider.initialize_database("management")
    deploy.assert_not_called()


def test_missing_marker_never_replays_a_retained_database_attempt(provider, monkeypatch):
    monkeypatch.setattr(provider, "kube_get", lambda *_: None)
    monkeypatch.setattr(provider, "deploy", MagicMock())
    with pytest.raises(ProvisioningError, match="database_initialization_incomplete"):
        provider.initialize_database("management")
    provider.deploy.assert_not_called()


@pytest.mark.parametrize("existing", ["radius-resource", "setup-secret"])
def test_unmarked_gate_database_is_rejected_without_promotion(provider, monkeypatch, existing):
    del provider.credentials.plane("management")["database"]
    provider.credentials.save()
    saved = provider.credentials.path.read_bytes()
    monkeypatch.setattr(provider, "deploy", MagicMock())
    monkeypatch.setattr(
        provider,
        "kube_get",
        lambda slot, namespace, kind, name: (
            {"metadata": {"name": name}}
            if existing == "setup-secret" and name == "postgres-setup"
            else None
        ),
    )
    provider.commands.run.return_value = json.dumps([{"name": "postgres"}])
    with pytest.raises(ProvisioningError, match="database_initialization_incomplete"):
        provider.initialize_database("management")
    provider.deploy.assert_not_called()
    assert provider.credentials.path.read_bytes() == saved
    assert not (provider.state / "management-database-intent.json").exists()
    assert not any("delete" in call.args[0] for call in provider.commands.run.call_args_list)
    if existing == "radius-resource":
        args = provider.commands.run.call_args.args[0]
        assert "list" in args and "Demo.Platform/postgreSqlDatabases" in args
        assert "--group" in args and "radplanes" in args


def test_interruption_after_recipe_before_metadata_cannot_replay(provider, monkeypatch):
    del provider.credentials.plane("management")["database"]
    provider.credentials.save()
    passwords = copy.deepcopy(provider.credentials.plane("management")["passwords"])
    monkeypatch.setattr(provider, "kube_get", lambda *_: None)
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
    intent = provider.state / "management-database-intent.json"
    assert json.loads(intent.read_text())["slot"] == "management"
    restarted = AzureProvider(
        provider.config,
        provider.root,
        Credentials(provider.credentials.path),
        provider.commands,
    )
    monkeypatch.setattr(restarted, "kube_get", lambda *_: None)
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
            {"kind": "ServiceAccount", "name": account, "namespace": "radplanes-shared-data-data"}
        ]
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
        str(provider.root / "operations/run-certificate-job.py"),
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
        "/app/operations/run-certificate-job.py",
    ]


def test_certificate_command_override_is_an_immutable_argument_vector(provider, raw_config):
    raw_config["certificateCommand"] = ["custom-python", "/app/operations/run-certificate-job.py"]
    provider.config = OperatorConfig.from_dict(raw_config)
    issuer = certificate_wrapper(
        "https://demo-vault.vault.azure.net/secrets/gateway-shared-control"
    )
    provider.commands.run.side_effect = issuer.run
    provider.certificate("shared-control", "control.centralus.cloudapp.azure.com")
    assert provider.commands.run.call_args.args[0][:2] == raw_config["certificateCommand"]
    raw_config["certificateCommand"] = "python operations/run-certificate-job.py"
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


def test_full_data_deploy_calls_certificate_then_https_and_preserves_it_on_reapply(provider):
    provider._verified = True
    provider.credentials.ensure("shared-control", {"cp_api", "cp_reconciler", "dp_reconciler"})
    provider.credentials.set_database("shared-control", database_properties())
    host = "data.centralus.cloudapp.azure.com"
    uri = "https://demo-vault.vault.azure.net/secrets/gateway-shared-data"
    issuer = certificate_wrapper(uri)
    gateway_calls = 0

    def run_command(args, **kwargs):
        nonlocal gateway_calls
        if "show" in args and "Demo.Platform/gateways" in args:
            gateway_calls += 1
            https = gateway_calls > 1
            return json.dumps(
                {
                    "properties": {
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
    assert stages == ["data-credentials", "data-application", "data-certificate"]
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
    first_values = []
    original_deploy = provider.deploy

    def observe_deploy(slot, template, application, values):
        first_values.append(dict(values))
        return original_deploy(slot, template, application, values)

    provider.deploy = observe_deploy
    provider.deploy_plane("shared-data")
    assert first_values[0]["gatewayPhase"] == "https"
    assert first_values[0]["certificateSecretUri"] == uri
    assert provider.credentials.path.read_text() == retained_passwords


def test_management_deploy_preserves_coordinator_identity_and_certificate_command(
    provider,
    raw_config,
):
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
        if "get" in args and "database-initialized" in args:
            return json.dumps(
                {
                    "data": {"serverId": "postgres-resource", "database": "management"},
                }
            )
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
        "/app/operations/run-certificate-job.py",
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


def test_main_acquires_one_session_and_runs_the_actual_polling_loop(tmp_path, config, monkeypatch):
    source = credentials(tmp_path / "operator-credentials.json", config)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_file = runtime / "provisioning.json"
    config_file.write_text(json.dumps(config.to_dict()))
    monkeypatch.setenv("PROVIDER", "azure")
    monkeypatch.setenv("PROJECT_ROOT", str(runtime))
    monkeypatch.setenv("PROVISIONING_CONFIG", str(config_file))
    monkeypatch.setenv("PROVISIONING_CREDENTIALS_JSON", json.dumps(source.runtime_seed(config)))
    monkeypatch.setenv("MANAGEMENT_DSN", source.dsn("management", "mgmt_provisioner"))
    store = MagicMock()
    store.claim_pending.return_value = None
    sessions = []

    @contextmanager
    def session(dsn):
        sessions.append(dsn)
        yield store

    monkeypatch.setattr(provisioner, "provisioner_session", session)
    monkeypatch.setattr(provisioner.signal, "signal", lambda *_: None)
    authenticate = MagicMock()
    monkeypatch.setattr(AzureProvider, "authenticate", authenticate)
    monkeypatch.setattr(AzureProvider, "connect_management", MagicMock())
    monkeypatch.setattr(AzureProvider, "verify_recipes", MagicMock())
    real_loop = provisioner.run_loop
    stopped = False

    def stop(_seconds):
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(
        provisioner,
        "run_loop",
        lambda operations, driver: real_loop(
            operations, driver, sleep=stop, stopped=lambda: stopped
        ),
    )
    assert provisioner.main() == 0
    assert len(sessions) == 1
    store.interrupt_running.assert_called_once_with()
    store.claim_pending.assert_called_once_with()
    authenticate.assert_called_once_with(workload_required=True)
    runtime_credentials = runtime / ".state/azure/credentials.json"
    assert runtime_credentials.is_file()
    assert "mgmt_api" not in runtime_credentials.read_text()


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


def test_local_startup_fails_without_a_fake_provider(monkeypatch):
    monkeypatch.setenv("PROVIDER", "local")
    assert provisioner.main() == 1


def test_entrypoints_are_cloud_free_for_help():
    root = Path(__file__).resolve().parents[2]
    for script in ("deploy-plane.py", "register-radius.py", "run-certificate-job.py"):
        result = subprocess.run(
            [sys.executable, str(root / "operations" / script), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0 and "--slot" in result.stdout
