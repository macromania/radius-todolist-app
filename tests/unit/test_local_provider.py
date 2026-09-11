import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from plane_demo.management import provisioner
from plane_demo.management.providers import local
from plane_demo.management.providers.commands import Commands
from plane_demo.management.providers.credentials import Credentials, database_dsn
from plane_demo.management.providers.local import LocalProvider
from plane_demo.management.providers.local_config import ACCESS_NAMESPACE, SCOPE, SLOTS, LocalConfig
from plane_demo.management.provisioning import Cluster, ProvisioningError, provision_pair

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
    role, namespace = LocalProvider.names(slot)
    return {
        "host": host,
        "port": 31543,
        "database": role,
        "username": "plane_setup",
        "tlsRequired": False,
        "serverId": f"kubernetes://{namespace}/statefulsets/postgres",
        "setupSecretName": "postgres-setup",
    }


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


def test_second_shared_tenant_reuses_pair_without_provider_commands(provider, config):
    first = inventory(config, available=True)
    result = provision_pair(operation(), provider, first, lambda _: None)
    second = provision_pair(operation(), provider, first, lambda _: None)
    assert first["control_url"] == result.control_url == second.control_url
    provider.commands.run.assert_not_called()


def test_available_pair_mismatch_does_not_try_infrastructure(provider, config):
    pair = inventory(config, available=True)
    pair["data_cluster_id"] = "kind://radplanes-local-isolated-1-data"
    with pytest.raises(ProvisioningError, match="pair_inventory_mismatch"):
        provision_pair(operation(), provider, pair, lambda _: None)
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
    provider, monkeypatch
):
    provider._verified = True
    provider.commands.run.side_effect = [
        "[]",
        "",
        "",
        "",
        json.dumps(
            {
                "properties": {
                    "provisioningState": "Succeeded",
                    "clusterId": "kind://radplanes-local-shared-control",
                    "clusterName": "radplanes-local-shared-control",
                    "bootstrapAccessRef": f"kubernetes://{ACCESS_NAMESPACE}/radplanes-local-shared-control-access#kubeconfig",
                }
            }
        ),
    ]
    get_access = MagicMock(return_value="protected-access")
    monkeypatch.setattr(provider, "get_access", get_access)
    assert provider.ensure_child_cluster("shared-control") == "protected-access"
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    submissions = [args for args in calls if "deploy" in args]
    assert len(submissions) == 1
    assert all("radplanes-local-management" in args for args in submissions)
    assert submissions[0][4].endswith("/modules/child-cluster.bicep")
    assert "provision-shared-control" in submissions[0]
    environment = next(args for args in calls if "Applications.Core/environments" in args)
    assert "create" in environment and "radplanes-local-management" in environment
    assert "--group" not in environment
    assert not any("create" in args and "Demo.Platform/clusters" in args for args in calls)
    assert not any(args[0] in {"kind", "docker", "terraform", "az"} for args in calls)
    parameters = json.loads(
        (provider.state / "shared-control-cluster-environment.json").read_text()
    )
    cluster_recipe = parameters["properties"]["recipes"]["Demo.Platform/clusters"]["default"]
    assert cluster_recipe["parameters"]["images"] == list(provider.config.images.values())
    assert (provider.state / "shared-control-cluster-intent.json").is_file()
    with pytest.raises(ProvisioningError, match="local_cluster_creation_incomplete"):
        provider.ensure_child_cluster("shared-control")


def test_failed_cluster_submission_is_not_retried(provider, monkeypatch):
    monkeypatch.setattr(provider, "resource_exists", lambda *_: False)
    monkeypatch.setattr(provider, "kube_get", lambda *_: None)
    monkeypatch.setattr(provider, "register_environment", lambda *_, **__: "provision-shared-data")
    submission = MagicMock(side_effect=ProvisioningError("command_timeout"))
    monkeypatch.setattr(provider, "deploy", submission)
    for expected in ("command_timeout", "local_cluster_creation_incomplete"):
        with pytest.raises(ProvisioningError, match=expected):
            provider.ensure_child_cluster("shared-data")
    assert submission.call_count == 1


def test_recipe_startup_verifies_actual_immutable_content(provider, modules, monkeypatch):
    by_name = {module["metadata"]["name"]: module for module in modules.values()}
    monkeypatch.setattr(provider, "kube_get", lambda _, __, ___, name: by_name[name])
    provider.verify_recipes()
    assert provider._verified and set(provider._modules) == set(modules)
    modules["cluster"]["data"]["server.py"] = "changed"
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


def test_management_workspace_startup_is_api_read_not_helm_discovery(provider, monkeypatch):
    monkeypatch.setattr(provider, "verify_management_identity", lambda: None)
    provider.commands.run.side_effect = [
        json.dumps({"id": SCOPE}),
        json.dumps({"id": f"{SCOPE}/providers/Applications.Core/environments/management"}),
    ]
    provider.connect_management()
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert not any("create" in args or "secrets" in args for args in calls)
    assert "radplanes-local-management" in provider.radius_config.read_text()


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


def test_local_runtime_secrets_keep_api_and_worker_credentials_separate(provider, monkeypatch):
    emitted = {}
    monkeypatch.setattr(provider, "secret", lambda _, __, name, data: emitted.update({name: data}))
    provider.runtime_secrets("management")
    assert set(emitted["management-api-runtime"]) == {"MANAGEMENT_DSN", "DEMO_KEY"}
    worker = emitted["provisioner-runtime"]
    assert worker["PROVIDER"] == "local" and "DEMO_KEY" not in worker
    assert "mgmt_api" not in worker["PROVISIONING_CREDENTIALS_JSON"]
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
            and (provider.state / "management-database-intent.json").exists()
            else None
        ),
    )
    monkeypatch.setattr(provider, "resource", lambda *_: properties)
    deployment = MagicMock()
    monkeypatch.setattr(provider, "deploy", deployment)
    emitted = []
    monkeypatch.setattr(provider, "apply", lambda _, value, **__: emitted.append(value))
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
    secret = next(value for value in emitted if value["kind"] == "Secret")
    assert conninfo_to_dict(secret["stringData"]["BOOTSTRAP_DSN"])["sslmode"] == "disable"
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert any("wait" in args and "job/database-init" in args for args in calls)
    assert calls[-1][-3:] == ["secret/postgres-setup", "--wait=true", "--ignore-not-found"]
    assert not any(
        retained in args
        for args in calls
        for retained in ("secret/postgres-credentials", "secret/postgres-server")
    )


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


def test_local_main_enters_existing_singleton_loop_and_marks_interrupted_once(
    tmp_path, config, monkeypatch
):
    source = credential_file(tmp_path / "operator.json", config)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config.to_dict()))
    monkeypatch.setenv("PROVIDER", "local")
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("PROVISIONING_CONFIG", str(config_path))
    monkeypatch.setenv("PROVISIONING_CREDENTIALS_JSON", json.dumps(source.runtime_seed(config)))
    monkeypatch.setenv("MANAGEMENT_DSN", source.dsn("management", "mgmt_provisioner"))
    store = MagicMock()
    store.claim_pending.return_value = None
    entered = []

    @contextmanager
    def session(dsn):
        entered.append(dsn)
        yield store

    monkeypatch.setattr(provisioner, "provisioner_session", session)
    monkeypatch.setattr(provisioner.signal, "signal", lambda *_: None)
    auth = MagicMock()
    monkeypatch.setattr(LocalProvider, "authenticate", auth)
    monkeypatch.setattr(LocalProvider, "connect_management", MagicMock())
    monkeypatch.setattr(LocalProvider, "verify_recipes", MagicMock())
    original = provisioner.run_loop
    stopped = []
    monkeypatch.setattr(
        provisioner,
        "run_loop",
        lambda operations, driver: original(
            operations, driver, sleep=lambda _: stopped.append(True), stopped=lambda: bool(stopped)
        ),
    )
    assert provisioner.main() == 0
    assert len(entered) == 1 and conninfo_to_dict(entered[0])["sslmode"] == "disable"
    auth.assert_called_once_with(workload_required=True)
    store.interrupt_running.assert_called_once()
    store.claim_pending.assert_called_once()
    assert (tmp_path / ".state/local/credentials.json").exists()
    assert not (tmp_path / ".state/azure").exists()


def load_operator(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / f"operations/local/{name}.py"
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


def test_operator_deploy_calls_real_provider_flow_once(tmp_path, config, monkeypatch):
    script = load_operator("deploy-demo")
    state = tmp_path / ".state/local"
    credentials = credential_file(state / "credentials.json", config, database=False)
    (state / "provisioning.json").write_text(json.dumps(config.to_dict()))
    (state / "setup-demo-complete.json").write_text(
        json.dumps(
            {
                "managementUID": UID,
                "configurationSHA256": hashlib.sha256(
                    json.dumps(config.to_dict(), sort_keys=True).encode()
                ).hexdigest(),
            }
        )
    )
    calls = []
    for name in ("authenticate", "connect_management", "verify_recipes"):
        monkeypatch.setattr(LocalProvider, name, lambda self, method=name: calls.append(method))
    monkeypatch.setattr(
        LocalProvider,
        "deploy_plane",
        lambda self, slot: calls.append(("deploy_plane", slot)) or "http://127.0.0.1:35490",
    )
    assert script.deploy(tmp_path) == "http://127.0.0.1:35490"
    assert calls == [
        "authenticate",
        "connect_management",
        "verify_recipes",
        ("deploy_plane", "management"),
    ]
    assert (state / "deploy-demo-complete.json").exists()
    assert credentials.plane("management")["demoKey"]
    with pytest.raises(ProvisioningError, match="local_deployment_already_attempted"):
        script.deploy(tmp_path)


def test_setup_entrypoint_builds_config_from_real_read_path_and_registers_management(
    tmp_path, config, modules, monkeypatch
):
    script = load_operator("setup-demo")
    state = tmp_path / ".state/local"
    home = state / "home/.kube"
    home.mkdir(parents=True)
    path = home / "config"
    path.write_text(
        json.dumps(access("radplanes-local-management", "https://127.0.0.1:35495", child=False))
    )
    path.chmod(0o600)
    (state / "management-created.json").write_text(
        json.dumps(
            {
                "name": "radplanes-local-management",
                "context": "radplanes-local-management",
                "nodeAddress": "172.18.0.2",
                "secretEncryptionVerified": True,
            }
        )
    )
    (state / "installed.json").write_text("{}")
    (state / "runtime-images.json").write_text(
        json.dumps(
            {
                "content_verified": True,
                "source_revision": "a" * 40,
                **{
                    role: {"reference": config.images[role], "image_id": config.image_ids[role]}
                    for role in ("api", "provisioner")
                },
            }
        )
    )
    bundle = {
        "objects": list(modules.values()),
        "modules": {
            kind: {
                "url": recipe["reference"],
                "sha256": recipe["digest"].removeprefix("sha256:"),
                "moduleServer": recipe["moduleServer"],
            }
            for kind, recipe in config.recipes.items()
        },
    }
    commands = MagicMock(spec=Commands)
    commands.environment = {}
    commands.guard = MagicMock()
    commands.run.side_effect = lambda args, **kwargs: "" if "status" in args else "a" * 40
    commands.json.side_effect = [
        bundle,
        {"metadata": {"uid": UID}},
        {"status": {"addresses": [{"type": "InternalIP", "address": "172.18.0.2"}]}},
        {"spec": {"clusterIP": "10.96.0.1"}},
    ]
    monkeypatch.setattr(script, "Commands", lambda *_, **__: commands)
    order = []
    for name in (
        "authenticate",
        "apply",
        "kubectl",
        "verify_recipes",
        "configure_child_terraform",
        "register",
        "connect_management",
    ):
        monkeypatch.setattr(
            LocalProvider,
            name,
            lambda self, *args, method=name, **kwargs: order.append((method, args)),
        )
    result = script.setup(tmp_path)
    assert result == config
    assert order[0][0] == "authenticate" and order[-1][0] == "connect_management"
    assert ("configure_child_terraform", ("management", ("applications-rp",))) in order
    assert ("register", ("management",)) in order
    assert (state / "setup-demo-complete.json").exists()
    assert LocalConfig.load(state / "provisioning.json") == config
    read_commands = [call.args[0] for call in commands.json.call_args_list]
    assert all(
        "--context" in args and "radplanes-local-management" in args for args in read_commands[1:]
    )
    with pytest.raises(ProvisioningError, match="local_setup_already_attempted"):
        script.setup(tmp_path)


def test_child_bootstrap_configures_both_stock_rps_without_socket_or_image_replacement(
    provider, monkeypatch
):
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
    monkeypatch.setattr(provider, "kube_get", lambda _, __, kind, name: layouts[(kind, name)])
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
        assert pod["containers"][0]["env"] == [{"name": "RADIUS_LOGGING_LEVEL", "value": "error"}]
        assert pod["initContainers"][0]["volumeMounts"] == [
            {"name": "terraform", "mountPath": "/terraform"}
        ]
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
    record = json.loads((provider.state / f"{slot}-cluster.json").read_text())
    assert record["clusterUID"] == UID and record["accessSecretUID"] == "access-uid"
    assert record["nodeAddress"] == "172.18.0.3"
    calls = [call.args[0] for call in provider.commands.run.call_args_list]
    assert "--raw=/readyz" in calls[1]
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
    provider, monkeypatch, slot, datastore, port
):
    provider._verified = True
    address = MagicMock(return_value="172.18.0.4")
    monkeypatch.setattr(provider, "node_address", address)
    assert provider.register_environment(slot) == slot
    parameters = json.loads((provider.state / f"{slot}-environment.json").read_text())["properties"]
    values = parameters["recipes"]
    assert set(values) == {datastore, "Demo.Platform/gateways"}
    assert values["Demo.Platform/gateways"]["default"]["parameters"] == {"gateway_host_port": port}
    if slot.endswith("-data"):
        assert values[datastore]["default"]["parameters"] == {}
        address.assert_not_called()
    else:
        assert values[datastore]["default"]["parameters"] == {"node_address": "172.18.0.4"}
        address.assert_called_once_with(slot)
    assert parameters["recipeConfig"]["env"] == {}
    command = provider.commands.run.call_args.args[0]
    assert f"radplanes-local-{slot}" in command
    assert "--group" not in command


def test_source_only_recipe_bundle_matches_runtime_verification_and_child_publication(
    provider, raw_local, monkeypatch
):
    result = subprocess.run(
        [sys.executable, str(ROOT / "operations/local/recipe-bundle.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    bundle = json.loads(result.stdout)
    assert bundle["liveStatus"] == "not-run"
    raw_local["recipes"] = {
        kind: {
            "reference": module["url"],
            "digest": "sha256:" + module["sha256"],
            "moduleServer": module["moduleServer"],
        }
        for kind, module in bundle["modules"].items()
    }
    provider.config = LocalConfig.from_dict(raw_local)
    modules = {
        item["metadata"]["name"]: item for item in bundle["objects"] if item["kind"] == "ConfigMap"
    }
    monkeypatch.setattr(provider, "kube_get", lambda _, __, ___, name: modules[name])
    provider.verify_recipes()
    published = []
    monkeypatch.setattr(provider, "apply", lambda _, resources: published.extend(resources))
    monkeypatch.setattr(provider, "kubectl", MagicMock())
    provider.publish_modules("shared-data")
    names = {provider.config.recipes[kind]["moduleServer"] for kind in ("redis", "gateway")}
    assert {item["metadata"]["name"] for item in published} == names
    assert {item["kind"] for item in published} == {"ConfigMap", "Deployment", "Service"}
    assert len(published) == 6
    for item in published:
        if item["kind"] == "ConfigMap":
            assert item == modules[item["metadata"]["name"]]
