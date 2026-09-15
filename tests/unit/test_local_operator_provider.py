import base64
import copy
import hashlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from plane_demo.management.providers import discovery
from plane_demo.management.providers.identity import DemoConfig
from plane_demo.management.providers.local import LocalProvider
from plane_demo.management.providers.local_artifacts import environment
from plane_demo.management.providers.local_config import SLOTS, LocalConfig
from plane_demo.management.provisioning import ProvisioningError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def operator_context(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "local_operator_context", ROOT / "scripts/operations/local/operator_provider.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = DemoConfig(
        "local",
        "sample",
        "demo",
        revision="a" * 40,
        demo_keys={"shared-data": "provided-local-data-key-" + "x" * 40},
    )
    images = {
        role: {
            "reference": f"localhost/{identity.stem}-{role}:" + "a" * 40,
            "id": "sha256:" + ("b" if role == "api" else "c") * 64,
        }
        for role in ("api", "provisioner", "operator")
    }
    recipes = {
        kind: {
            "reference": "http://local-module-"
            + "a" * 20
            + ".radius-system.svc.cluster.local:18080/"
            + "d" * 64
            + ".tar.gz",
            "digest": "sha256:" + "d" * 64,
            "moduleServer": "local-module-" + "a" * 20,
        }
        for kind in ("cluster", "postgresql", "redis", "gateway")
    }
    ca = b"synthetic-local-ca"
    selected = LocalConfig.from_dict(
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
                role: {"reference": images[role]["reference"], "imageId": images[role]["id"]}
                for role in ("api", "provisioner")
            },
            "managementCluster": {
                "clusterId": f"kind://{identity.slot_name('management')}",
                "uid": "11111111-1111-1111-1111-111111111111",
                "nodeAddress": "172.18.0.2",
                "serviceAddress": "10.96.0.1",
                "caSHA256": hashlib.sha256(ca).hexdigest(),
            },
        },
        identity=identity,
    )
    profile = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": identity.slot_name("management"),
        "clusters": [
            {
                "name": "cluster",
                "cluster": {
                    "server": "https://127.0.0.1:35495",
                    "certificate-authority-data": base64.b64encode(ca).decode(),
                },
            }
        ],
        "users": [
            {
                "name": "user",
                "user": {
                    "client-certificate-data": "Y2VydA==",
                    "client-key-data": "a2V5",
                },
            }
        ],
        "contexts": [
            {
                "name": identity.slot_name("management"),
                "context": {"cluster": "cluster", "user": "user"},
            }
        ],
    }
    path = tmp_path / "discovered.kubeconfig"
    path.write_text(json.dumps(profile))
    path.chmod(0o600)
    inputs = {
        "revision": "a" * 40,
        "stem": identity.stem,
        "images": images,
        "dependencies": [{"reference": "docker.io/library/redis:8", "id": "sha256:" + "e" * 64}],
    }
    binding = environment(
        identity.stem,
        identity.stem,
        identity.stem + "-access",
        "management",
        recipes,
        inputs,
        "172.18.0.2",
        all_recipes=True,
    )
    binding["id"] = (
        f"/planes/radius/local/resourceGroups/{identity.stem}/providers/Applications.Core/environments/management"
    )
    connection = MagicMock()
    connection.call_api.return_value = binding
    manager = MagicMock()
    manager.__enter__.return_value = connection
    state = {
        "real_api_client": module.client.ApiClient,
        "extract_prepared": module.extract_prepared,
        "binding": binding,
        "connection": connection,
        "configurations": [],
    }

    def make_client(settings):
        state["configurations"].append(settings)
        return manager

    monkeypatch.setattr(module.client, "ApiClient", make_client)
    core, apps, leases = MagicMock(), MagicMock(), MagicMock()
    namespace = identity.namespace("management")
    core.read_namespace.return_value = SimpleNamespace(
        metadata=SimpleNamespace(
            name=namespace,
            labels={
                "plane-demo/project": identity.project,
                "plane-demo/deployment": identity.deployment,
                "plane-demo/environment": "local",
            },
        )
    )
    secrets = {}

    def read_secret(name, namespace, **kwargs):
        if name not in secrets:
            raise ApiException(status=404)
        return secrets[name]

    def create_secret(namespace, body, **kwargs):
        if body.metadata.name in secrets:
            raise ApiException(status=409)
        secrets[body.metadata.name] = body
        return body

    core.read_namespaced_secret.side_effect = read_secret
    core.create_namespaced_secret.side_effect = create_secret
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)

    def create_lease(namespace, body, **kwargs):
        lease = SimpleNamespace(
            metadata=SimpleNamespace(
                name=body["metadata"]["name"],
                namespace=namespace,
                labels=body["metadata"]["labels"],
                uid="22222222-2222-2222-2222-222222222222",
                resource_version="1",
                deletion_timestamp=None,
            ),
            spec=SimpleNamespace(holder_identity=body["spec"]["holderIdentity"]),
        )
        state["lease"] = lease
        return lease

    leases.create_namespaced_lease.side_effect = create_lease
    leases.read_namespaced_lease.side_effect = lambda *args, **kwargs: state["lease"]
    monkeypatch.setattr(module.client, "CoreV1Api", lambda _: core)
    monkeypatch.setattr(module.client, "AppsV1Api", lambda _: apps)
    monkeypatch.setattr(module.client, "CoordinationV1Api", lambda _: leases)
    monkeypatch.setattr(
        module, "extract_prepared", lambda commands, workspace, inputs: workspace / "prepared"
    )

    def authenticate(provider):
        assert (provider.state / "home/.kube/config").read_text() == path.read_text()

    monkeypatch.setattr(LocalProvider, "authenticate", authenticate)
    monkeypatch.setattr(LocalProvider, "connect_management", lambda _: None)
    monkeypatch.setattr(LocalProvider, "verify_recipes", lambda _: None)
    return module, selected, path, core, apps, leases, state


def test_public_host_factory_owns_lease_and_never_creates_file_credentials(
    operator_context, tmp_path
):
    module, config, path, core, _, leases, _ = operator_context
    with module.local_operator_provider(
        config, tmp_path, kubeconfig=path, context=config.allocation("management")["context"]
    ) as provider:
        workspace = provider.state
        assert provider.prepared_assets == workspace / "prepared"
        assert not hasattr(provider.credentials, "path")
        provider.commands.guard()
        provider.credentials.seed_provided_keys()
        assert (
            provider.credentials.demo_key("shared-data") == config.identity.demo_keys["shared-data"]
        )
    assert not workspace.exists()
    assert not (tmp_path / ".state").exists()
    options = leases.delete_namespaced_lease.call_args.kwargs["body"].preconditions
    assert options.uid == "22222222-2222-2222-2222-222222222222"
    assert options.resource_version == "1"
    core.create_namespaced_secret.assert_called_once()


def test_host_factory_never_takes_over_existing_lease(operator_context, tmp_path):
    module, config, path, core, _, leases, _ = operator_context
    leases.create_namespaced_lease.side_effect = ApiException(status=409)
    with pytest.raises(ProvisioningError, match="management_bootstrap_active_or_interrupted"):
        with module.local_operator_provider(
            config, tmp_path, kubeconfig=path, context=config.allocation("management")["context"]
        ):
            pytest.fail("existing bootstrap lock admitted")
    leases.delete_namespaced_lease.assert_not_called()
    core.create_namespaced_secret.assert_not_called()


def test_host_factory_does_not_delete_a_replaced_holder(operator_context, tmp_path):
    module, config, path, _, _, leases, state = operator_context
    with pytest.raises(ProvisioningError, match="management_bootstrap_owner_mismatch"):
        with module.local_operator_provider(
            config, tmp_path, kubeconfig=path, context=config.allocation("management")["context"]
        ):
            state["lease"] = copy.deepcopy(state["lease"])
            state["lease"].spec.holder_identity = "another-owner"
    leases.delete_namespaced_lease.assert_not_called()


def test_host_factory_refuses_credential_creation_while_worker_pod_remains(
    operator_context, tmp_path
):
    module, config, path, core, _, _, _ = operator_context
    core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[
            SimpleNamespace(metadata=SimpleNamespace(owner_references=[])),
        ]
    )
    with module.local_operator_provider(
        config, tmp_path, kubeconfig=path, context=config.allocation("management")["context"]
    ) as provider:
        with pytest.raises(ProvisioningError, match="provisioner_must_be_stopped"):
            provider.credentials.seed_provided_keys()
    core.create_namespaced_secret.assert_not_called()


def test_real_sdk_client_uses_only_scoped_certificate_files_on_repeated_entry(
    operator_context, tmp_path, monkeypatch
):
    module, config, path, _, _, _, state = operator_context
    real_client = state["real_api_client"]
    seen = []

    def request(connection, *args, **kwargs):
        settings = connection.configuration
        files = [Path(settings.ssl_ca_cert), Path(settings.cert_file), Path(settings.key_file)]
        assert settings.verify_ssl is True and settings.host == "https://127.0.0.1:35495"
        assert all(file.is_file() and file.stat().st_mode & 0o777 == 0o600 for file in files)
        seen.extend(files)
        return state["binding"]

    monkeypatch.setattr(real_client, "call_api", request)
    monkeypatch.setattr(module.client, "ApiClient", real_client)
    for _ in range(2):
        with module.local_operator_provider(
            config, tmp_path, kubeconfig=path, context=config.allocation("management")["context"]
        ) as provider:
            assert all(file.is_relative_to(provider.state) for file in seen[-3:])
        assert all(not file.exists() for file in seen)
    assert len(seen) == 6


@pytest.mark.parametrize("malformed", [None, {}, {"id": "owned", "properties": None}])
def test_malformed_live_binding_fails_with_provisioning_code(operator_context, tmp_path, malformed):
    module, config, path, _, _, leases, state = operator_context
    state["connection"].call_api.return_value = malformed
    with pytest.raises(ProvisioningError, match="local_recipe_binding_invalid"):
        with module.local_operator_provider(
            config, tmp_path, kubeconfig=path, context=config.allocation("management")["context"]
        ):
            pytest.fail("malformed binding admitted")
    leases.create_namespaced_lease.assert_not_called()


def test_missing_prepared_package_is_a_provisioning_error(operator_context, tmp_path, monkeypatch):
    module, config, _, _, _, _, state = operator_context
    inputs = module.binding_inputs(state["binding"], config.resource_prefix, config.radius_group)
    commands = MagicMock()
    commands.json.return_value = [{"Id": inputs["images"]["operator"]["id"]}]
    commands.run.side_effect = ["a" * 64, "", ""]
    monkeypatch.setattr(module, "docker_host", lambda: "unix:///synthetic/docker.sock")
    with pytest.raises(ProvisioningError, match="local_prepared_assets_invalid"):
        state["extract_prepared"](commands, tmp_path, inputs)
    assert commands.run.call_args.args[0][-2:] == ["rm", "a" * 64]


@pytest.mark.parametrize(
    "malformed",
    [None, [], {"version": 1, "modules": ["cluster", "postgresql", "redis", "gateway"]}],
)
def test_non_object_prepared_manifest_is_normalized(
    operator_context, tmp_path, monkeypatch, malformed
):
    module, config, _, _, _, _, state = operator_context
    inputs = module.binding_inputs(state["binding"], config.resource_prefix, config.radius_group)
    commands = MagicMock()
    commands.json.return_value = [{"Id": inputs["images"]["operator"]["id"]}]

    def run(arguments):
        if "create" in arguments:
            return "a" * 64
        if "cp" in arguments:
            (tmp_path / "prepared/modules.json").write_text(json.dumps(malformed))
        return ""

    commands.run.side_effect = run
    monkeypatch.setattr(module, "docker_host", lambda: "unix:///synthetic/docker.sock")
    with pytest.raises(ProvisioningError, match="local_prepared_assets_invalid"):
        state["extract_prepared"](commands, tmp_path, inputs)


def test_list_shaped_runtime_images_is_normalized(operator_context, tmp_path):
    module, config, path, _, _, leases, state = operator_context
    binding = copy.deepcopy(state["binding"])
    binding["properties"]["recipes"]["Demo.Platform/clusters"]["default"]["parameters"][
        "runtime_images"
    ] = [
        "api",
        "provisioner",
        "operator",
    ]
    state["connection"].call_api.return_value = binding
    with pytest.raises(ProvisioningError, match="local_recipe_binding_invalid"):
        with module.local_operator_provider(
            config, tmp_path, kubeconfig=path, context=config.allocation("management")["context"]
        ):
            pytest.fail("malformed image map admitted")
    leases.create_namespaced_lease.assert_not_called()


@pytest.mark.parametrize("drift", [None, "namespace", "radius-namespace", "image", "revision"])
def test_local_worker_reconstructs_configuration_from_current_apis(
    operator_context, tmp_path, monkeypatch, drift
):
    _, config, _, core, apps, _, state = operator_context
    account = tmp_path / "service-account"
    account.mkdir()
    namespace = config.namespace("management")
    (account / "namespace").write_text(namespace)
    (account / "ca.crt").write_bytes(b"synthetic-local-ca")
    monkeypatch.setattr(discovery, "SERVICE_ACCOUNT", account)
    monkeypatch.setattr(discovery.kube_config, "load_incluster_config", lambda **kwargs: None)
    azure = MagicMock(side_effect=AssertionError("local discovery must not call Azure"))
    monkeypatch.setattr(discovery, "azure_configuration", azure)
    manager = MagicMock()
    manager.__enter__.return_value = state["connection"]
    monkeypatch.setattr(discovery.client, "ApiClient", lambda settings: manager)
    labels = {
        "plane-demo/project": config.identity.project,
        "plane-demo/deployment": config.identity.deployment,
        "plane-demo/environment": "local",
    }
    core.read_namespace.side_effect = lambda name, **kwargs: SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=labels,
            uid=config.management_cluster["uid"],
        )
    )
    core.read_node.return_value = SimpleNamespace(
        metadata=SimpleNamespace(
            name=config.allocation("management")["clusterName"] + "-control-plane"
        ),
        status=SimpleNamespace(
            addresses=[SimpleNamespace(type="InternalIP", address="172.18.0.2")]
        ),
    )
    core.read_namespaced_service.return_value = SimpleNamespace(
        metadata=SimpleNamespace(name="kubernetes", namespace="default"),
        spec=SimpleNamespace(cluster_ip="10.96.0.1"),
    )

    def deployment(name, requested_namespace, **kwargs):
        role = "api" if name == "management-api" else "provisioner"
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name, namespace=requested_namespace),
            spec=SimpleNamespace(
                template=SimpleNamespace(
                    spec=SimpleNamespace(
                        service_account_name=name,
                        containers=[
                            SimpleNamespace(
                                name=name,
                                image="localhost/other-api:latest"
                                if drift == "image"
                                else config.images[role],
                            )
                        ],
                    )
                )
            ),
        )

    apps.read_namespaced_deployment.side_effect = deployment
    if drift == "namespace":
        labels["plane-demo/deployment"] = "other"
    elif drift == "radius-namespace":
        state["binding"]["properties"]["compute"]["namespace"] = namespace
    if drift:
        code = {
            "namespace": "management_namespace_mismatch",
            "radius-namespace": "management_radius_mismatch",
            "image": "management_workload_mismatch",
            "revision": "management_workload_mismatch",
        }[drift]
        identity = (
            replace(config.identity, revision="f" * 40) if drift == "revision" else config.identity
        )
        with pytest.raises(ProvisioningError, match=code):
            discovery.read_runtime_configuration(identity, tmp_path)
        azure.assert_not_called()
        return
    result = discovery.read_runtime_configuration(config.identity, tmp_path)
    assert result.identity == config.identity
    assert result.images == config.images and result.image_ids == config.image_ids
    assert result.management_cluster == config.management_cluster
    azure.assert_not_called()
    assert not (tmp_path / ".state").exists()
