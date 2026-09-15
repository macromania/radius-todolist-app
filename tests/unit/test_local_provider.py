import base64
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from test_local_deploy_caller import caller as caller
from test_prepared_local_provider import prepared_provider as prepared_provider

from plane_demo.management import provisioner
from plane_demo.management.providers import local
from plane_demo.management.providers.commands import Commands
from plane_demo.management.providers.credentials import (
    Credentials,
    StoredCredentials,
    credential_roles,
    database_dsn,
)
from plane_demo.management.providers.identity import DemoConfig
from plane_demo.management.providers.local import LocalProvider
from plane_demo.management.providers.local_config import ACCESS_NAMESPACE, SCOPE, SLOTS, LocalConfig
from plane_demo.management.providers.secret_store import CredentialScope, CredentialValue
from plane_demo.management.provisioning import (
    Cluster,
    PairResult,
    ProvisioningError,
    provision_pair,
)

ROOT = Path(__file__).resolve().parents[2]
CA = b"synthetic-ca"
UID = "11111111-1111-1111-1111-111111111111"
PASSWORD = "local/synthetic+password:" + "x" * 48


@pytest.fixture
def modules():
    result = {}
    for kind in ("cluster", "postgresql", "redis", "gateway"):
        archive = kind.encode()
        server = "source-only-static-server"
        name = "local-module-" + hashlib.sha256(archive + server.encode()).hexdigest()[:20]
        result[kind] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": "radius-system"},
            "immutable": True,
            "binaryData": {"archive.tar.gz": base64.b64encode(archive).decode()},
            "data": {"server.py": server},
        }
    return result


@pytest.fixture
def raw_local(modules):
    recipes = {}
    for kind, module in modules.items():
        sha = hashlib.sha256(kind.encode()).hexdigest()
        name = module["metadata"]["name"]
        recipes[kind] = {
            "reference": f"http://{name}.radius-system.svc.cluster.local:18080/{sha}.tar.gz",
            "digest": "sha256:" + sha,
            "moduleServer": name,
        }
    return {
        "version": 1,
        "provider": "local",
        "projectName": "radplanes",
        "allocations": {
            slot: {
                "slot": slot,
                "clusterName": f"radplanes-local-{slot}",
                "context": f"radplanes-local-{slot}",
                "gatewayPort": 35490 + index,
                "apiPort": 35495 + index,
            }
            for index, slot in enumerate(SLOTS)
        },
        "recipes": recipes,
        "images": {
            role: {
                "reference": f"localhost/radplanes-plane-{role}:" + "a" * 40,
                "imageId": "sha256:" + "b" * 64,
            }
            for role in ("api", "provisioner")
        },
        "managementCluster": {
            "clusterId": "kind://radplanes-local-management",
            "uid": UID,
            "nodeAddress": "172.18.0.2",
            "serviceAddress": "10.96.0.1",
            "caSHA256": hashlib.sha256(CA).hexdigest(),
        },
    }


@pytest.fixture
def config(raw_local):
    return LocalConfig.from_dict(raw_local)


def db_properties(slot="management", host="172.18.0.2"):
    role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
    namespace = f"radplanes-local-{slot}-{role}"
    return {
        "provisioningState": "Succeeded",
        "application": f"{SCOPE}/providers/Applications.Core/applications/{role}",
        "environment": f"{SCOPE}/providers/Applications.Core/environments/{slot}",
        "host": host,
        "port": 31543,
        "database": role,
        "username": "plane_setup",
        "tlsRequired": False,
        "serverId": f"kubernetes://{namespace}/statefulsets/postgres",
        "setupSecretName": "postgres-setup",
    }


def test_selected_local_identity_drives_names_access_and_temporary_commands(
    raw_local, tmp_path, monkeypatch
):
    identity = DemoConfig("local", "example", "learn")
    raw_local["projectName"] = identity.project
    for slot, allocation in raw_local["allocations"].items():
        allocation.update(clusterName=identity.slot_name(slot), context=identity.slot_name(slot))
    raw_local["managementCluster"]["clusterId"] = f"kind://{identity.slot_name('management')}"
    for role, image in raw_local["images"].items():
        image["reference"] = (
            f"localhost/{identity.stem}-{role}:" + image["reference"].rsplit(":", 1)[1]
        )
    selected = LocalConfig.from_dict(raw_local, identity=identity)
    root, workspace = tmp_path / "checkout", tmp_path / "work"
    root.mkdir()
    credentials = Credentials(tmp_path / "credentials.json", environment="local")
    credentials.ensure(
        "management",
        {
            "mgmt_api",
            "mgmt_provisioner",
            *(item["reporting_role"] for item in selected.pair_slots),
        },
    )
    properties = db_properties()
    properties["serverId"] = (
        f"kubernetes://{identity.namespace('management')}/statefulsets/postgres"
    )
    credentials.set_database("management", properties)
    provider = LocalProvider(selected, root, credentials, workspace=workspace)
    commands = provider.commands
    compiler = tmp_path / "bicep"
    compiler.write_text("#!/bin/sh\nexit 0\n")
    compiler.chmod(0o700)
    commands._bicep = compiler
    provider.paths("management")[1].write_text("{}")
    monkeypatch.setattr(
        commands, "run", MagicMock(return_value='{"properties":{"provisioningState":"Succeeded"}}')
    )
    provider.resource("management", "cluster", "shared-control", "cluster-shared-control")
    command = commands.run.call_args.args[0]
    assert command[command.index("--group") + 1] == identity.stem
    assert command[command.index("--workspace") + 1] == identity.slot_name("management")
    assert not (root / ".state").exists()
    emitted = []
    monkeypatch.setattr(provider, "apply", lambda slot, value, **kw: emitted.append(value))
    monkeypatch.setattr(provider, "kube_get", lambda *args: None)
    provider.prerequisites("shared-control")
    assert emitted[0]["metadata"] == {
        "name": identity.namespace("shared-control"),
        "labels": {
            "plane-demo/project": "example",
            "plane-demo/deployment": "learn",
            "plane-demo/environment": "local",
        },
    }
    permissions = provider.management_permissions(identity.namespace("management"), [])
    access = next(
        value
        for value in permissions
        if value["kind"] == "Role" and value["metadata"]["namespace"] == selected.access_namespace
    )
    assert access["rules"][0]["resourceNames"] == [
        f"{identity.slot_name(slot)}-access" for slot in SLOTS[1:]
    ]
    credential_role = next(
        value
        for value in permissions
        if value["kind"] == "Role" and value["metadata"]["name"] == "plane-credential-store"
    )
    assert credential_role["metadata"]["namespace"] == identity.namespace("management")
    assert credential_role["rules"][0]["verbs"] == ["get"]
    assert len(credential_role["rules"][0]["resourceNames"]) == 15
    assert credential_role["rules"][1] == {
        "apiGroups": [""],
        "resources": ["secrets"],
        "verbs": ["create"],
    }
    emitted.clear()
    provider.prerequisites("management")
    settings = next(
        item
        for batch in emitted
        if isinstance(batch, list)
        for item in batch
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "provisioning-settings"
    )
    assert settings["data"] == selected.bootstrap_settings
    assert settings["immutable"] is True
    binding = next(
        value
        for value in permissions
        if value["kind"] == "RoleBinding" and value["metadata"]["name"] == "plane-credential-store"
    )
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "provisioner",
            "namespace": identity.namespace("management"),
        }
    ]
    restored = LocalConfig.from_dict(selected.to_dict())
    assert restored.identity == identity


def credential_file(path, config, *, database=True):
    credentials = Credentials(path, environment="local")
    credentials.ensure(
        "management",
        {"mgmt_api", "mgmt_provisioner", *(x["reporting_role"] for x in config.pair_slots)},
    )
    if database:
        credentials.set_database("management", db_properties())
    return credentials


@pytest.fixture
def provider(tmp_path, config):
    commands = MagicMock(spec=Commands)
    commands.guard = MagicMock()
    commands.environment = {"PATH": os.environ["PATH"]}
    commands.run.return_value = ""
    commands.radius_environment.return_value = {"HOME": str(tmp_path / ".state/local/home")}
    return LocalProvider(
        config,
        tmp_path,
        credential_file(tmp_path / ".state/local/credentials.json", config),
        commands,
    )


def operation(pair_id="shared"):
    return SimpleNamespace(
        operation_id=uuid4(),
        tenant_id=pair_id + "-a",
        pair_id=pair_id,
        onboarding_id=uuid4(),
        initial_message="hello",
        isolation="shared" if pair_id == "shared" else "isolated",
    )


def inventory(config, pair_id="shared", *, available=False):
    return {
        "pair_id": pair_id,
        "isolation": "shared" if pair_id == "shared" else "isolated",
        "reporting_role": "cp_" + pair_id.replace("-", "_"),
        "stage": "available" if available else "allocated",
        **{
            f"{role}_cluster_id": config.expected_cluster_id(f"{pair_id}-{role}")
            for role in ("control", "data")
        },
        **{
            f"{role}_url": f"http://127.0.0.1:{config.allocation(f'{pair_id}-{role}')['gatewayPort']}"
            for role in ("control", "data")
        },
    }


def access(context, server, *, child=True):
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": context,
        "clusters": [
            {
                "name": "kind-" + context,
                "cluster": {
                    "server": server,
                    "certificate-authority-data": base64.b64encode(CA).decode(),
                    **({"tls-server-name": context} if child else {}),
                },
            }
        ],
        "contexts": [
            {"name": context, "context": {"cluster": "kind-" + context, "user": "kind-" + context}}
        ],
        "users": [
            {
                "name": "kind-" + context,
                "user": {
                    "client-certificate-data": base64.b64encode(b"synthetic-certificate").decode(),
                    "client-key-data": base64.b64encode(b"synthetic-key").decode(),
                },
            }
        ],
    }


def test_config_round_trip_is_frozen_and_has_no_azure_fields(config):
    assert LocalConfig.from_dict(config.to_dict()) == config
    assert "foundation" not in config.to_dict()
    with pytest.raises(TypeError):
        config.allocations["shared-control"]["gatewayPort"] = 8000
    assert config.expected_cluster_id("isolated-1-data") == "kind://radplanes-local-isolated-1-data"


@pytest.mark.parametrize(
    "mutation",
    [
        "provider",
        "project",
        "version",
        "missing-slot",
        "extra-slot",
        "gateway-port",
        "api-port",
        "context",
        "cluster",
        "recipe-host",
        "recipe-digest",
        "image-tag",
        "image-id",
        "different-revision",
        "management-id",
        "management-ca",
        "node-address",
    ],
)
def test_invalid_local_config_fails_before_commands(raw_local, mutation):
    if mutation in ("provider", "version"):
        raw_local[mutation] = "invalid"
    elif mutation == "project":
        raw_local["projectName"] = "other"
    elif mutation == "missing-slot":
        del raw_local["allocations"]["shared-data"]
    elif mutation == "extra-slot":
        raw_local["allocations"]["isolated-2-data"] = {}
    elif mutation in ("gateway-port", "api-port", "context", "cluster"):
        field = {
            "gateway-port": "gatewayPort",
            "api-port": "apiPort",
            "context": "context",
            "cluster": "clusterName",
        }[mutation]
        raw_local["allocations"]["shared-control"][field] = "invalid"
    elif mutation == "recipe-host":
        raw_local["recipes"]["cluster"]["reference"] = "http://127.0.0.1/module.tar.gz"
    elif mutation == "recipe-digest":
        raw_local["recipes"]["cluster"]["digest"] = "latest"
    elif mutation == "image-tag":
        raw_local["images"]["api"]["reference"] = "localhost/radplanes-plane-api:latest"
    elif mutation == "image-id":
        raw_local["images"]["api"]["imageId"] = "uninspected"
    elif mutation == "different-revision":
        raw_local["images"]["api"]["reference"] = "localhost/radplanes-plane-api:" + "c" * 40
    elif mutation == "management-id":
        raw_local["managementCluster"]["clusterId"] = "kind://other"
    elif mutation == "management-ca":
        raw_local["managementCluster"]["caSHA256"] = "unverified"
    else:
        raw_local["managementCluster"]["nodeAddress"] = "127.0.0.1"
    with pytest.raises((ValueError, KeyError, TypeError)):
        LocalConfig.from_dict(raw_local)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:35491",
        "http://127.0.0.1:35492",
        "http://localhost:35491",
        "http://host.docker.internal:35491",
        "http://user@127.0.0.1:35491",
        "http://127.0.0.1:35491/path",
        "http://127.0.0.1:35491/?x=1",
        "http://127.0.0.1:35491/#fragment",
    ],
)
def test_local_endpoints_are_exact_operator_loopback(config, url):
    with pytest.raises(ProvisioningError, match="invalid_gateway_output"):
        config.validate_endpoint("shared-control", url)


def test_explicit_local_database_mode_and_azure_default_are_separate(tmp_path, config):
    credentials = credential_file(tmp_path / "local.json", config)
    dsn = conninfo_to_dict(credentials.dsn("management", "mgmt_api"))
    assert dsn["sslmode"] == "disable" and dsn["port"] == "31543"
    assert "sslrootcert" not in dsn
    seed = credentials.runtime_seed(config)
    assert seed["provider"] == "local"
    assert seed["planes"]["management"]["database"]["tlsRequired"] is False
    assert "mgmt_api" not in seed["planes"]["management"]["passwords"]
    with pytest.raises(ProvisioningError, match="invalid_database_connection"):
        database_dsn(db_properties(), "mgmt_api", PASSWORD)
    with pytest.raises(ProvisioningError, match="credentials_environment_mismatch"):
        Credentials(tmp_path / "local.json")


@pytest.mark.parametrize("mutation", ["omitted-tls", "tls", "port", "loopback", "public"])
def test_local_database_never_disables_tls_by_omission(mutation):
    properties = db_properties()
    if mutation == "omitted-tls":
        del properties["tlsRequired"]
    elif mutation == "tls":
        properties["tlsRequired"] = True
    elif mutation == "port":
        properties["port"] = 5432
    else:
        properties["host"] = "127.0.0.1" if mutation == "loopback" else "8.8.8.8"
    with pytest.raises(ProvisioningError, match="invalid_database_connection"):
        database_dsn(properties, "mgmt_api", PASSWORD, environment="local")


def test_second_shared_tenant_reuses_live_pair_without_creating_resources(
    provider, config, monkeypatch
):
    first = inventory(config, available=True)
    live = PairResult(**{key: first.pop(key) for key in PairResult.__dataclass_fields__})
    inspect = MagicMock(return_value=live)
    monkeypatch.setattr(provider, "inspect_pair", inspect)
    result = provision_pair(operation(), provider, first, lambda _: None)
    second = provision_pair(operation(), provider, first, lambda _: None)
    assert live.control_url == result.control_url == second.control_url
    assert inspect.call_count == 2
    provider.commands.run.assert_not_called()


def test_available_pair_mismatch_does_not_try_infrastructure(provider, config, monkeypatch):
    pair = inventory(config, available=True)
    monkeypatch.setattr(
        provider,
        "inspect_pair",
        lambda _: PairResult(
            pair["control_cluster_id"],
            "kind://radplanes-local-isolated-1-data",
            pair["control_url"],
            pair["data_url"],
        ),
    )
    with pytest.raises(ProvisioningError, match="pair_inventory_mismatch"):
        provision_pair(operation(), provider, pair, lambda _: None)
    provider.commands.run.assert_not_called()


def test_local_available_pair_discovers_gateway_values_again(provider, config, monkeypatch):
    reads = []

    def resource(slot, kind, name, application):
        reads.append((slot, kind, name, application))
        if kind == "cluster":
            return {
                "clusterId": config.expected_cluster_id(name),
                "provisioningState": "Succeeded",
                "application": f"{SCOPE}/providers/Applications.Core/applications/{application}",
                "environment": f"{SCOPE}/providers/Applications.Core/environments/provision-{name}",
            }
        return {
            "url": f"http://127.0.0.1:{config.allocation(slot)['gatewayPort']}",
            "provisioningState": "Succeeded",
            "application": f"{SCOPE}/providers/Applications.Core/applications/{application}",
            "environment": f"{SCOPE}/providers/Applications.Core/environments/{slot}",
        }

    monkeypatch.setattr(provider, "resource", resource)
    monkeypatch.setattr(
        provider,
        "get_access",
        lambda slot: SimpleNamespace(cluster_id=config.expected_cluster_id(slot)),
    )
    pair = {
        "pair_id": "shared",
        "isolation": "shared",
        "reporting_role": "cp_shared",
        "stage": "available",
    }
    first = provision_pair(operation(), provider, pair, lambda _: None)
    second = provision_pair(operation(), provider, pair, lambda _: None)
    assert first == second
    assert len(reads) == 8
    assert first.data_url == "http://127.0.0.1:35492"
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
            "application": f"{SCOPE}/providers/Applications.Core/applications/{application}",
            "environment": f"{SCOPE}/providers/Applications.Core/environments/"
            + (f"provision-{name}" if resource_kind.endswith("/clusters") else slot),
        }
        if resource_kind.endswith("/clusters"):
            properties["clusterId"] = provider.expected_cluster_id(name)
        else:
            properties["url"] = (
                f"http://127.0.0.1:{provider.config.allocation(slot)['gatewayPort']}"
            )
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


def test_isolated_pair_uses_distinct_allocated_ids_and_ports(provider, config, monkeypatch):
    created = []

    def ensure(slot):
        created.append(slot)
        return Cluster(slot, config.expected_cluster_id(slot), *provider.paths(slot))

    monkeypatch.setattr(provider, "ensure_child_cluster", ensure)
    bootstrap = MagicMock()
    monkeypatch.setattr(provider, "bootstrap_child", bootstrap)
    monkeypatch.setattr(
        provider,
        "deploy_plane",
        lambda slot, _: f"http://127.0.0.1:{config.allocation(slot)['gatewayPort']}",
    )
    result = provision_pair(
        operation("isolated-1"), provider, inventory(config, "isolated-1"), lambda _: None
    )
    assert created == ["isolated-1-control", "isolated-1-data"]
    assert bootstrap.call_count == 2
    assert result.control_url.endswith(":35493") and result.data_url.endswith(":35494")
    assert result.data_cluster_id == "kind://radplanes-local-isolated-1-data"


def test_child_creation_actual_commands_use_management_radius_bicep_and_never_kind(
    prepared_provider, monkeypatch
):
    provider, _, inputs, _ = prepared_provider
    provider._verified = True
    provider.commands.run.side_effect = [
        "[]",
        "[]",
        "",
        "",
        json.dumps(
            {
                "properties": {
                    "provisioningState": "Succeeded",
                    "clusterId": provider.expected_cluster_id("shared-control"),
                    "clusterName": provider.config.allocation("shared-control")["clusterName"],
                    "bootstrapAccessRef": f"kubernetes://{provider.config.access_namespace}/{provider.config.resource_prefix}-shared-control-access#kubeconfig",
                }
            }
        ),
        json.dumps([{"name": "provision-shared-control"}]),
    ]
    get_access = MagicMock(return_value="protected-access")
    monkeypatch.setattr(provider, "get_access", get_access)
    assert provider.ensure_child_cluster("shared-control") == "protected-access"
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    submissions = [args for args in calls if "deploy" in args]
    assert len(submissions) == 1
    assert all(provider.config.allocation("management")["context"] in args for args in submissions)
    assert submissions[0][4].endswith("/modules/child-cluster.bicep")
    assert "provision-shared-control" in submissions[0]
    environment = next(
        args for args in calls if "Applications.Core/environments" in args and "create" in args
    )
    assert provider.config.allocation("management")["context"] in environment
    assert "--group" not in environment
    assert not any("create" in args and "Demo.Platform/clusters" in args for args in calls)
    assert not any(args[0] in {"kind", "docker", "terraform", "az"} for args in calls)
    parameters = json.loads(
        (provider.state / "shared-control-cluster-environment.json").read_text()
    )
    cluster_recipe = parameters["properties"]["recipes"]["Demo.Platform/clusters"]["default"]
    assert {
        item["reference"]
        for item in (
            *cluster_recipe["parameters"]["runtime_images"].values(),
            *cluster_recipe["parameters"]["dependency_images"],
        )
    } == {
        *(item["reference"] for item in inputs["images"].values()),
        *(item["reference"] for item in inputs["dependencies"]),
    }
    assert not (provider.state / "shared-control-cluster-intent.json").exists()
    with pytest.raises(ProvisioningError, match="local_cluster_creation_incomplete"):
        provider.ensure_child_cluster("shared-control")


def test_failed_cluster_submission_is_not_retried(provider, monkeypatch):
    monkeypatch.setattr(provider, "resource_exists", lambda *_: False)
    monkeypatch.setattr(provider, "kube_get", lambda *_: None)
    environments = []
    monkeypatch.setattr(provider, "rad", lambda *args, **kwargs: json.dumps(environments))

    def register(*args, **kwargs):
        environments.append({"name": "provision-shared-data"})
        return "provision-shared-data"

    monkeypatch.setattr(provider, "register_environment", register)
    submission = MagicMock(side_effect=ProvisioningError("command_timeout"))
    monkeypatch.setattr(provider, "deploy", submission)
    for expected in ("command_timeout", "local_cluster_creation_incomplete"):
        with pytest.raises(ProvisioningError, match=expected):
            provider.ensure_child_cluster("shared-data")
    assert submission.call_count == 1


def test_recipe_startup_verifies_actual_immutable_content(prepared_provider):
    provider, _, _, modules = prepared_provider
    provider.verify_recipes()
    assert provider._verified and set(provider._modules) == set(provider.config.recipes)
    modules[provider.config.recipes["cluster"]["moduleServer"]]["data"]["module.json"] = "{}"
    with pytest.raises(ProvisioningError, match="local_recipe_digest_mismatch"):
        provider.verify_recipes()


@pytest.mark.parametrize("bad", ["insecure", "exec", "proxy", "context", "server", "ca", "san"])
def test_child_kubeconfig_rejects_unsafe_or_wrong_access(bad):
    context = "radplanes-local-shared-control"
    value = access(context, "https://172.18.0.3:6443")
    cluster = value["clusters"][0]["cluster"]
    if bad == "insecure":
        cluster["insecure-skip-tls-verify"] = True
    elif bad == "exec":
        value["users"][0]["user"] = {"exec": {"command": "untrusted"}}
    elif bad == "proxy":
        cluster["proxy-url"] = "http://untrusted"
    elif bad == "context":
        value["current-context"] = "other"
    elif bad == "server":
        cluster["server"] = "http://172.18.0.3:6443"
    elif bad == "ca":
        del cluster["certificate-authority-data"]
    else:
        cluster["tls-server-name"] = "other"
    with pytest.raises(ProvisioningError, match="invalid_local_kubeconfig"):
        local.decode_access(json.dumps(value), context, child=True)


def test_runtime_auth_uses_rotating_token_and_proves_parent_uid(provider, monkeypatch):
    service = provider.state / "serviceaccount"
    service.mkdir()
    (service / "ca.crt").write_bytes(CA)
    (service / "token").write_text("synthetic-service-account-token")
    (service / "namespace").write_text(provider.names("management")[1])
    monkeypatch.setattr(local, "SERVICE_ACCOUNT", service)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    provider.commands.run.side_effect = [
        json.dumps(
            {
                "status": {
                    "userInfo": {
                        "username": (
                            f"system:serviceaccount:{provider.names('management')[1]}:provisioner"
                        )
                    }
                }
            }
        ),
        json.dumps({"metadata": {"uid": UID}}),
        json.dumps(
            {
                "metadata": {"name": "radplanes-local-management-control-plane"},
                "status": {"addresses": [{"type": "InternalIP", "address": "172.18.0.2"}]},
            }
        ),
    ]
    provider.authenticate(workload_required=True)
    value = json.loads(provider.paths("management")[1].read_text())
    assert value["users"][0]["user"] == {"tokenFile": str(service / "token")}
    assert "synthetic-service-account-token" not in json.dumps(value)
    assert value["clusters"][0]["cluster"] == {
        "server": "https://10.96.0.1:443",
        "certificate-authority": str(service / "ca.crt"),
    }
    assert provider.commands.guard.called
    assert provider._workload


@pytest.mark.parametrize("spelling", ["resourceGroups", "resourcegroups"])
def test_management_workspace_startup_is_api_read_not_helm_discovery(
    provider, monkeypatch, spelling
):
    monkeypatch.setattr(provider, "verify_management_identity", lambda: None)
    provider.commands.run.side_effect = [
        json.dumps({"id": SCOPE.replace("resourceGroups", spelling)}),
        json.dumps(
            {
                "id": (f"{SCOPE}/providers/Applications.Core/environments/management").replace(
                    "resourceGroups", spelling
                )
            }
        ),
    ]
    provider.connect_management()
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert not any("create" in args or "secrets" in args for args in calls)
    assert "radplanes-local-management" in provider.radius_config.read_text()


def test_management_workspace_still_refuses_a_different_group(provider, monkeypatch):
    monkeypatch.setattr(provider, "verify_management_identity", lambda: None)
    provider.commands.run.return_value = json.dumps({"id": SCOPE + "-foreign"})
    with pytest.raises(ProvisioningError, match="management_radius_mismatch"):
        provider.connect_management()


def test_secret_permissions_are_named_and_no_docker_privilege_is_granted(provider):
    resources = provider.management_permissions(
        provider.names("management")[1], ["local-module-" + "a" * 20]
    )
    access_roles = [
        item
        for item in resources
        if item["kind"] == "Role" and item["metadata"]["namespace"] == ACCESS_NAMESPACE
    ]
    assert access_roles[0]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "verbs": ["get"],
            "resourceNames": [f"radplanes-local-{slot}-access" for slot in SLOTS[1:]],
        }
    ]
    assert not any(
        word in json.dumps(resources) for word in ("hostPath", "cluster-admin", "pods/exec")
    )


@pytest.mark.parametrize("slot", SLOTS)
def test_storage_recipe_permissions_are_only_for_the_data_namespace(provider, monkeypatch, slot):
    emitted = []
    monkeypatch.setattr(
        provider,
        "apply",
        lambda _, value, **__: emitted.extend(value if isinstance(value, list) else [value]),
    )
    monkeypatch.setattr(provider, "kube_get", lambda *_: None)
    provider.prerequisites(slot)
    grants = [value for value in emitted if value["metadata"].get("name") == "redis-recipe-storage"]
    if not slot.endswith("-data"):
        assert grants == []
        return
    namespace = provider.names(slot)[1]
    assert [value["kind"] for value in grants] == ["Role", "RoleBinding"]
    assert all(value["metadata"]["namespace"] == namespace for value in grants)
    assert grants[0]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["persistentvolumeclaims"],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
    ]
    assert grants[1]["subjects"] == [
        {
            "kind": "ServiceAccount",
            "namespace": "radius-system",
            "name": "applications-rp",
        }
    ]


@pytest.mark.parametrize("slot", ["shared-data", "isolated-1-data"])
def test_data_api_runtime_account_has_only_configmap_get(provider, monkeypatch, slot):
    emitted = []
    monkeypatch.setattr(
        provider,
        "apply",
        lambda _, value, **__: emitted.extend(value if isinstance(value, list) else [value]),
    )
    monkeypatch.setattr(provider, "kube_get", lambda *_: None)
    provider.prerequisites(slot)
    accounts = {
        value["metadata"]["name"]: value for value in emitted if value["kind"] == "ServiceAccount"
    }
    assert accounts["data-api-runtime"]["automountServiceAccountToken"] is True
    assert accounts["data-api"]["automountServiceAccountToken"] is False
    binding = next(
        v
        for v in emitted
        if v["kind"] == "RoleBinding" and v["metadata"]["name"] == "data-api-configmaps"
    )
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "data-api-runtime",
            "namespace": provider.names(slot)[1],
        }
    ]
    role = next(
        v
        for v in emitted
        if v["kind"] == "Role" and v["metadata"]["name"] == binding["roleRef"]["name"]
    )
    assert role["rules"] == [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get"]}]


def test_local_runtime_secrets_keep_api_and_worker_credentials_separate(provider, monkeypatch):
    emitted = {}
    monkeypatch.setattr(provider, "secret", lambda _, __, name, data: emitted.update({name: data}))
    provider.runtime_secrets("management")
    assert set(emitted["management-api-runtime"]) == {"MANAGEMENT_DSN", "DEMO_KEY"}
    worker = emitted["provisioner-runtime"]
    assert worker["PROVIDER"] == "local" and "DEMO_KEY" not in worker
    assert set(worker) == {"MANAGEMENT_DSN", "PROVIDER"}
    assert conninfo_to_dict(worker["MANAGEMENT_DSN"])["sslmode"] == "disable"


def test_database_bootstrap_uses_real_job_and_only_deletes_temporary_setup_secret(
    provider, monkeypatch
):
    provider.credentials = credential_file(
        provider.state / "fresh-credentials.json", provider.config, database=False
    )
    properties = db_properties()
    monkeypatch.setattr(provider, "database_resource_exists", lambda _: False)
    monkeypatch.setattr(
        provider,
        "kube_get",
        lambda _, __, kind, name: (
            {"data": {"password": base64.b64encode(PASSWORD.encode()).decode()}}
            if kind == "secret"
            and name == "postgres-setup"
            and provider.credentials.has_database("management") is False
            and deployment.called
            else None
        ),
    )
    monkeypatch.setattr(provider, "resource", lambda *_: properties)
    deployment = MagicMock()
    monkeypatch.setattr(provider, "deploy", deployment)
    emitted = []
    monkeypatch.setattr(
        provider, "apply", lambda _, value, **__: emitted.append(copy.deepcopy(value))
    )
    provider.initialize_database("management")
    deployment.assert_called_once_with(
        "management", "database", "management", {"databaseName": "management"}
    )
    job = next(value for value in emitted if value["kind"] == "Job")
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "python",
        "-m",
        "plane_demo.setup.bootstrap",
    ]
    assert "BOOTSTRAP_DSN" not in emitted[0]["stringData"]
    secret = next(
        value
        for value in emitted
        if value["kind"] == "Secret" and "BOOTSTRAP_DSN" in value["stringData"]
    )
    assert conninfo_to_dict(secret["stringData"]["BOOTSTRAP_DSN"])["sslmode"] == "disable"
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert any("wait" in args and "job/database-init" in args for args in calls)
    assert calls[-1][-3:] == ["secret/postgres-setup", "--wait=true", "--ignore-not-found"]
    assert not any(
        retained in args
        for args in calls
        for retained in ("secret/postgres-credentials", "secret/postgres-server")
    )


def test_local_existing_database_runs_read_only_observer_job(provider, monkeypatch):
    monkeypatch.setattr(provider, "database_resource_exists", lambda _: True)
    monkeypatch.setattr(provider, "rad", lambda *_: json.dumps({"properties": db_properties()}))
    node = {
        "metadata": {
            "name": provider.config.allocation("management")["clusterName"] + "-control-plane"
        },
        "status": {"addresses": [{"type": "InternalIP", "address": "172.18.0.2"}]},
    }
    provider.commands.run.side_effect = lambda args, **kwargs: (
        json.dumps(node) if "get" in args and "node" in args else ""
    )
    deploy = MagicMock()
    monkeypatch.setattr(provider, "deploy", deploy)
    emitted = []
    monkeypatch.setattr(
        provider, "apply", lambda _, value, **__: emitted.append(copy.deepcopy(value))
    )
    provider.initialize_database("management")
    deploy.assert_not_called()
    secret, job = emitted
    values = secret["stringData"]
    assert values["BOOTSTRAP_MODE"] == "observe"
    assert "ROLE_PASSWORDS_JSON" not in values
    assert conninfo_to_dict(values["BOOTSTRAP_DSN"])["user"] == "mgmt_provisioner"
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "python",
        "-m",
        "plane_demo.setup.bootstrap",
    ]
    assert not (provider.state / "management-database-intent.json").exists()
    assert not any(value["kind"] == "ConfigMap" for value in emitted)


@pytest.mark.parametrize(
    "workload,health",
    [
        (False, "http://127.0.0.1:35490/livez"),
        (True, "http://172.18.0.2:31480/livez"),
    ],
)
def test_local_deploy_uses_shared_bicep_and_correct_health_address(
    provider, monkeypatch, workload, health
):
    provider._workload = workload
    for method in ("prerequisites", "initialize_database", "runtime_secrets"):
        monkeypatch.setattr(provider, method, MagicMock())
    deployment = MagicMock()
    monkeypatch.setattr(provider, "deploy", deployment)
    monkeypatch.setattr(
        provider, "resource", lambda *_: {"host": "127.0.0.1", "url": "http://127.0.0.1:35490"}
    )
    monkeypatch.setattr(provider, "node_address", lambda _: "172.18.0.2")
    url = provider.deploy_plane("management")
    assert url == "http://127.0.0.1:35490"
    deployment.assert_called_once_with(
        "management",
        "management",
        "management",
        {
            "image": provider.config.images["api"],
            "provisionerImage": provider.config.images["provisioner"],
            "ownershipLabels": {"plane-demo/project": "radplanes"},
        },
    )
    assert provider.commands.run.call_args.args[0][-1] == health
    assert json.loads((provider.state / "management-endpoint.json").read_text()) == {"url": url}
    assert not (provider.state / "endpoints.json").exists()


@pytest.mark.parametrize("slot", ["management", "shared-data"])
@pytest.mark.parametrize("existing", [None, b'{"publisher":"exporter","preserve":true}\n'])
def test_local_endpoint_records_do_not_create_or_touch_exporter_aggregate(provider, slot, existing):
    if slot != "management":
        provider.credentials.ensure(slot, set())
    aggregate = provider.state / "endpoints.json"
    if existing is not None:
        aggregate.write_bytes(existing)
    url = f"http://127.0.0.1:{provider.config.allocation(slot)['gatewayPort']}"
    provider.record_endpoint(slot, url)
    assert json.loads((provider.state / f"{slot}-endpoint.json").read_text()) == {"url": url}
    key = provider.state / f"{slot}.key"
    assert key.read_text().strip() == provider.credentials.plane(slot)["demoKey"]
    assert key.stat().st_mode & 0o777 == 0o600
    if existing is None:
        assert not aggregate.exists()
    else:
        assert aggregate.read_bytes() == existing


def test_real_local_command_run_guards_before_process_spawn(tmp_path, monkeypatch):
    guard = MagicMock(side_effect=psycopg.OperationalError("lost lock"))
    commands = Commands(tmp_path, guard, state_root=tmp_path / ".state/local", local=True)
    spawn = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(psycopg.OperationalError):
        commands.run(["rad", "deploy", "child-cluster.bicep"])
    spawn.assert_not_called()


def test_local_commands_do_not_inherit_cloud_or_daemon_credentials(tmp_path, monkeypatch):
    for key in (
        "AZURE_CONFIG_DIR",
        "AZURE_FEDERATED_TOKEN_FILE",
        "DOCKER_HOST",
        "HTTP_PROXY",
        "TF_LOG",
    ):
        monkeypatch.setenv(key, "never-inherit")
    commands = Commands(tmp_path, state_root=tmp_path / ".state/local", local=True)
    assert set(commands.environment) == {"HOME", "PATH", "LC_ALL"}


def test_selected_main_reads_service_credentials_and_discards_worker_workspace(
    tmp_path, raw_local, monkeypatch
):
    from kubernetes import config as kube_config

    identity = DemoConfig("local", "sample", "demo")
    raw_local["projectName"] = identity.project
    for slot, allocation in raw_local["allocations"].items():
        allocation.update(clusterName=identity.slot_name(slot), context=identity.slot_name(slot))
    raw_local["managementCluster"]["clusterId"] = f"kind://{identity.slot_name('management')}"
    for role, image in raw_local["images"].items():
        image["reference"] = (
            f"localhost/{identity.stem}-{role}:" + image["reference"].rsplit(":", 1)[1]
        )
    config = LocalConfig.from_dict(raw_local, identity=identity)
    discovery = MagicMock(return_value=config)
    monkeypatch.setattr(provisioner, "read_runtime_configuration", discovery)
    for name, value in identity.public_values().items():
        monkeypatch.setenv(name, value)
    properties = db_properties()
    scope = f"/planes/radius/local/resourceGroups/{identity.stem}/providers/Applications.Core"
    properties.update(
        application=f"{scope}/applications/management",
        environment=f"{scope}/environments/management",
        serverId=f"kubernetes://{identity.namespace('management')}/statefulsets/postgres",
    )
    passwords = {
        role: f"synthetic-{role}-" + "x" * 48 for role in credential_roles(config, "management")
    }
    backend = MagicMock()
    backend.scope = CredentialScope(identity.project, identity.deployment, "local")
    backend.get.side_effect = lambda slot, role: CredentialValue(passwords[role])
    discovered = []

    def credential_store(scope, namespace, api, **kwargs):
        assert callable(kwargs["singleton_guard"])
        discovered.append((scope, namespace))
        return backend

    monkeypatch.setattr(provisioner, "KubernetesCredentialStore", credential_store)
    monkeypatch.setattr(kube_config, "load_incluster_config", lambda **kwargs: None)
    monkeypatch.setattr(LocalProvider, "resource", lambda *args: properties)
    workspaces = []

    def authenticate(provider, **kwargs):
        assert isinstance(provider.credentials, StoredCredentials)
        workspaces.append(provider.state)

    monkeypatch.setattr(LocalProvider, "authenticate", authenticate)
    monkeypatch.setattr(LocalProvider, "connect_management", lambda _: None)
    monkeypatch.setattr(LocalProvider, "verify_recipes", lambda _: None)
    monkeypatch.setenv("PROVIDER", "local")
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("PROVISIONING_CONFIG", str(tmp_path / "missing-inventory.json"))
    monkeypatch.setenv("PROVISIONING_CREDENTIALS_JSON", "not a credential seed")
    monkeypatch.setenv(
        "MANAGEMENT_DSN",
        database_dsn(
            properties,
            "mgmt_provisioner",
            passwords["mgmt_provisioner"],
            environment="local",
        ),
    )
    operations = MagicMock()
    operations.claim_pending.return_value = None
    sessions = []

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
    assert discovered[0][1] == identity.namespace("management")
    assert discovered[0][0].project == identity.project
    assert workspaces and all(not workspace.exists() for workspace in workspaces)
    assert not (tmp_path / ".state").exists()
    backend.get_or_create.assert_not_called()
    assert not (tmp_path / ".state/azure").exists()


def load_operator(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / f"scripts/operations/local/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("entrypoint", ["setup-demo", "deploy-demo"])
def test_operator_preview_makes_no_commands(entrypoint, monkeypatch):
    script = load_operator(entrypoint)
    command = MagicMock(side_effect=AssertionError("preview must not execute"))
    monkeypatch.setattr(Commands, "run", command)
    assert script.main([]) == 0
    command.assert_not_called()


def test_operator_deploy_invokes_guarded_factory_flow_once(caller):
    assert caller.script.deploy(caller.root) == "http://127.0.0.1:35490"
    assert caller.order == [
        "lease-acquired",
        ("seed", "management"),
        ("seed", "shared-control"),
        ("deploy", "management"),
        "lease-released",
    ]
    assert not (caller.root / ".state").exists()


def test_setup_entrypoint_builds_config_from_real_read_path_and_registers_management(
    tmp_path, monkeypatch
):
    script = load_operator("setup-demo")
    native = MagicMock(return_value=17)
    monkeypatch.setattr(script.subprocess, "call", native)
    monkeypatch.setattr(script, "ROOT", tmp_path)
    assert script.main(["--execute"]) == 17
    native.assert_called_once_with(["bash", str(tmp_path / "scripts/operations/local/setup.sh")])
    assert not (tmp_path / ".state").exists()


def test_child_bootstrap_configures_both_stock_rps_without_socket_or_image_replacement(
    prepared_provider, monkeypatch
):
    provider, _, inputs, _ = prepared_provider
    slot = "shared-control"
    layouts = {}
    for name in ("dynamic-rp", "applications-rp"):
        layouts[("deployment", name)] = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": name, "image": f"ghcr.io/radius-project/{name}:0.60"}
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": name + "-config"}},
                            {"name": "terraform", "emptyDir": {}},
                        ],
                    }
                }
            }
        }
        layouts[("configmap", name + "-config")] = {
            "data": {"radius-self-host.yaml": "terraform:\n  path: /terraform\n  logLevel: TRACE\n"}
        }
    monkeypatch.setattr(provider, "kube_get", lambda _, __, kind, name: layouts.get((kind, name)))
    monkeypatch.setattr(
        provider,
        "apply",
        lambda _, value, **kwargs: layouts.update({("namespace", "radius-system"): value}),
    )
    published = MagicMock()
    registered = MagicMock()
    monkeypatch.setattr(provider, "publish_modules", published)
    monkeypatch.setattr(provider, "register", registered)
    cluster = Cluster(slot, provider.expected_cluster_id(slot), *provider.paths(slot))
    provider.bootstrap_child(cluster)
    published.assert_called_once_with(slot)
    registered.assert_called_once_with(slot)
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    install = calls[0]
    assert "install" in install and cluster.context in install
    assert "global.terraform.enabled=false" in install
    patches = [json.loads(args[-1]) for args in calls if "patch" in args]
    cm_patches = [patch for patch in patches if "data" in patch]
    assert len(cm_patches) == 2 and all("OFF" in str(patch) for patch in cm_patches)
    deployment_patches = [patch for patch in patches if "spec" in patch]
    assert len(deployment_patches) == 2
    for patch in deployment_patches:
        pod = patch["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert "image" not in pod["containers"][0]
        assert {"name": "TF_CLI_CONFIG_FILE", "value": "/terraform/terraform.tfrc"} in (
            pod["containers"][0]["env"]
        )
        assert pod["initContainers"][0]["volumeMounts"] == [
            {"name": "terraform", "mountPath": "/terraform"}
        ]
        assert pod["initContainers"][0]["command"] == ["python3", local.TERRAFORM_INIT]
        assert pod["initContainers"][0]["image"] == inputs["images"]["operator"]["reference"]
        assert pod["initContainers"][0]["imagePullPolicy"] == "Never"
        assert pod["initContainers"][0]["securityContext"]["capabilities"] == {
            "drop": ["ALL"],
            "add": ["CHOWN"],
        }
        assert pod["volumes"][0]["projected"]["defaultMode"] == 0o440
        assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == (
            "/var/run/secrets/kubernetes.io/serviceaccount"
        )
        assert "hostPath" not in json.dumps(patch) and "docker.sock" not in json.dumps(patch)
    with pytest.raises(ProvisioningError, match="local_child_bootstrap_incomplete"):
        provider.bootstrap_child(cluster)


def test_local_run_loop_failure_marks_only_claimed_operation_and_never_replays(
    provider, monkeypatch
):
    request = operation()
    operations = MagicMock()
    operations.claim_pending.side_effect = [request, None]
    operations.connection.execute.return_value.fetchone.return_value = inventory(provider.config)
    submission = MagicMock(side_effect=ProvisioningError("local_cluster_creation_incomplete"))
    monkeypatch.setattr(provider, "ensure_child_cluster", submission)
    for name in ("authenticate", "connect_management", "verify_recipes"):
        monkeypatch.setattr(provider, name, MagicMock())
    ticks = []
    provisioner.run_loop(
        operations, provider, sleep=lambda _: ticks.append(True), stopped=lambda: len(ticks) == 2
    )
    operations.interrupt_running.assert_called_once()
    assert operations.claim_pending.call_count == 2 and submission.call_count == 1
    operations.complete.assert_not_called()
    assert operations.observe.call_args.kwargs == {
        "status": "failed",
        "error_code": "local_cluster_creation_incomplete",
    }
    operations.connection.execute.side_effect = psycopg.OperationalError("lost singleton")
    with pytest.raises(psycopg.OperationalError):
        provider.commands.guard()


def test_local_radius_environment_uses_exact_state_and_does_not_touch_global_home(
    tmp_path, monkeypatch
):
    home = tmp_path / "global-home"
    compiler = home / ".rad/bin/bicep"
    compiler.parent.mkdir(parents=True)
    compiler.write_text("#!/bin/sh\nexit 0\n")
    compiler.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    state = tmp_path / ".state/local"
    state.mkdir(parents=True)
    kubeconfig = state / "shared-control.kubeconfig"
    kubeconfig.write_text("protected slot context")
    commands = Commands(tmp_path, state_root=state, local=True)
    env = commands.radius_environment(kubeconfig, "radplanes-local-shared-control")
    selected = Path(env["HOME"])
    assert selected == state / "homes/radplanes-local-shared-control"
    assert (selected / ".kube/config").resolve() == kubeconfig
    assert (selected / ".rad/bin/bicep").resolve() == compiler
    assert "AZURE_CONFIG_DIR" not in env
    assert not (home / ".kube/config").exists()
    assert not (tmp_path / ".state/azure").exists()


def test_child_access_run_path_proves_secret_ownership_tls_node_and_cluster_uid(provider):
    slot = "shared-control"
    context, path = provider.paths(slot)
    kubeconfig = access(context, "https://172.18.0.3:6443")
    secret = {
        "metadata": {
            "name": context + "-access",
            "namespace": ACCESS_NAMESPACE,
            "uid": "access-uid",
            "labels": {"radplanes.local/slot": slot},
            "annotations": {
                "radplanes.local/radius-resource": (
                    f"{SCOPE}/providers/Demo.Platform/clusters/{slot}"
                )
            },
        },
        "data": {"kubeconfig": base64.b64encode(json.dumps(kubeconfig).encode()).decode()},
    }
    provider.commands.run.side_effect = [
        json.dumps(secret),
        "ok",
        json.dumps(
            {
                "metadata": {"name": context + "-control-plane"},
                "status": {"addresses": [{"type": "InternalIP", "address": "172.18.0.3"}]},
            }
        ),
        json.dumps({"metadata": {"uid": UID}}),
    ]
    cluster = provider.get_access(slot)
    assert cluster == Cluster(slot, provider.expected_cluster_id(slot), context, path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == kubeconfig
    assert not (provider.state / f"{slot}-cluster.json").exists()
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert "--raw=/readyz" in calls[1]
    assert "kube-system" in calls[3]
    assert all(context in args for args in calls[1:])
    assert not any("--insecure-skip-tls-verify" in args for args in calls)


@pytest.mark.parametrize(
    "slot,datastore,port",
    [
        ("management", "Demo.Platform/postgreSqlDatabases", 35490),
        ("shared-control", "Demo.Platform/postgreSqlDatabases", 35491),
        ("shared-data", "Applications.Datastores/redisCaches", 35492),
    ],
)
def test_environment_passes_only_the_required_recipe_parameters(
    prepared_provider, monkeypatch, slot, datastore, port
):
    provider, _, _, _ = prepared_provider
    provider._verified = True
    address = MagicMock(return_value="172.18.0.4")
    monkeypatch.setattr(provider, "node_address", address)
    assert provider.register_environment(slot) == slot
    parameters = json.loads((provider.state / f"{slot}-environment.json").read_text())["properties"]
    values = parameters["recipes"]
    assert set(values) == (
        set(local.TYPES.values()) if slot == "management" else {datastore, "Demo.Platform/gateways"}
    )
    identity = {
        "resource_prefix": provider.config.resource_prefix,
        "radius_group": provider.config.radius_group,
    }
    assert values["Demo.Platform/gateways"]["default"]["parameters"] == {
        **identity,
        "gateway_host_port": port,
    }
    if slot.endswith("-data"):
        assert values[datastore]["default"]["parameters"] == identity
        address.assert_not_called()
    else:
        assert values[datastore]["default"]["parameters"] == {
            **identity,
            "node_address": "172.18.0.4",
        }
        address.assert_called_once_with(slot)
    assert parameters["recipeConfig"]["env"] == {}
    command = provider.commands.run.call_args.args[0]
    assert provider.config.allocation(slot)["context"] in command
    assert "--group" not in command


def test_source_only_recipe_bundle_matches_runtime_verification_and_child_publication(
    prepared_provider, monkeypatch
):
    provider, _, _, modules = prepared_provider
    provider.verify_recipes()
    published = []
    monkeypatch.setattr(provider, "kube_get", lambda *args: None)
    monkeypatch.setattr(
        provider, "apply", lambda _, resources, **kwargs: published.extend(resources)
    )
    monkeypatch.setattr(provider, "kubectl", MagicMock())
    provider.publish_modules("shared-data")
    names = {provider.config.recipes[kind]["moduleServer"] for kind in ("redis", "gateway")}
    assert {item["metadata"]["name"] for item in published} == names
    assert {item["kind"] for item in published} == {"ConfigMap", "Deployment", "Service"}
    assert len(published) == 6
    for item in published:
        if item["kind"] == "ConfigMap":
            assert item == modules[item["metadata"]["name"]]
