import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException
from psycopg.conninfo import conninfo_to_dict

from plane_demo.management.providers.azure import AzureProvider
from plane_demo.management.providers.credentials import StoredCredentials
from plane_demo.management.providers.identity import DemoConfig
from plane_demo.management.providers.local import LocalProvider
from plane_demo.management.providers.secret_store import (
    AzureKeyVaultCredentialStore,
    CredentialScope,
    CredentialValue,
    KubernetesCredentialStore,
    StoreError,
)
from plane_demo.management.provisioning import ProvisioningError


class Configuration:
    def __init__(self, environment):
        self.identity = DemoConfig(
            environment,
            "sample",
            "demo",
            "11111111-1111-1111-1111-111111111111" if environment == "azure" else None,
            "centralus" if environment == "azure" else None,
        )
        self.pair_slots = [
            {"pair_id": "shared", "reporting_role": "cp_shared"},
            {"pair_id": "isolated-1", "reporting_role": "cp_isolated_1"},
        ]

    def allocation(self, slot):
        self.identity.slot_name(slot)
        return {"slot": slot}

    @property
    def project_name(self):
        return self.identity.project


class Store:
    def __init__(self, environment="local"):
        self.scope = CredentialScope("sample", "demo", environment)
        self.values = {}
        self.created = []

    def get(self, slot, role):
        if (slot, role) not in self.values:
            raise StoreError("credential_missing")
        return CredentialValue(self.values[slot, role])

    def get_or_create(self, slot, role, *, provided_value=None, require_existing=False):
        if (slot, role) not in self.values:
            if require_existing:
                raise StoreError("credential_missing")
            self.values[slot, role] = provided_value or f"synthetic-{slot}-{role}-" + "x" * 48
            self.created.append((slot, role))
        if provided_value is not None and self.values[slot, role] != provided_value:
            raise StoreError("credential_conflict")
        return self.get(slot, role)


def database(environment, host=None):
    return {
        "host": host
        or ("pg-demo.postgres.database.azure.com" if environment == "azure" else "172.18.0.2"),
        "port": 5432 if environment == "azure" else 31543,
        "database": "control",
        "tlsRequired": environment == "azure",
    }


@pytest.mark.parametrize("environment", ["azure", "local"])
def test_recreated_credentials_keep_service_values_and_rediscover_connections(
    environment, tmp_path
):
    config, store = Configuration(environment), Store(environment)
    reads, protected = [], []
    properties = database(environment)

    def reader(slot):
        reads.append(slot)
        return properties

    first = StoredCredentials(config, store)
    first.bind(reader, protected.append, lambda: None)
    roles = {"cp_api", "cp_reconciler", "dp_reconciler"}
    original = first.ensure("shared-control", roles)
    created = list(store.created)
    restarted = StoredCredentials(config, store)
    restarted.bind(reader, protected.append, lambda: None)
    assert restarted.ensure("shared-control", roles, require_existing=True) == original
    assert store.created == created
    before = conninfo_to_dict(restarted.dsn("shared-control", "cp_api"))
    properties = database(
        environment,
        "pg-moved.postgres.database.azure.com" if environment == "azure" else "172.18.0.9",
    )
    after = conninfo_to_dict(restarted.dsn("shared-control", "cp_api"))
    assert before["host"] != after["host"]
    assert before["password"] == after["password"] == original["passwords"]["cp_api"]
    assert reads == ["shared-control", "shared-control"]
    assert original["demoKey"] in protected
    assert not list(tmp_path.iterdir())


def test_missing_existing_value_is_not_regenerated():
    config, store = Configuration("local"), Store()
    credentials = StoredCredentials(config, store)
    credentials.bind(lambda _: database("local"), lambda _: None, lambda: None)
    with pytest.raises(ProvisioningError, match="credential_missing"):
        credentials.ensure("shared-data", set(), require_existing=True)
    assert not store.created


def test_credential_source_rejects_another_service_scope_before_access():
    store = Store()
    store.scope = CredentialScope("other", "demo", "local")
    with pytest.raises(ProvisioningError, match="credential_owner_mismatch"):
        StoredCredentials(Configuration("local"), store)
    assert not store.created


def test_store_errors_and_lost_session_stop_before_credential_mutation():
    config, store = Configuration("local"), Store()
    credentials = StoredCredentials(config, store)
    guard = MagicMock(side_effect=ProvisioningError("session_stopped"))
    credentials.bind(lambda _: database("local"), lambda _: None, guard)
    with pytest.raises(ProvisioningError, match="session_stopped"):
        credentials.ensure("shared-data", set())
    assert not store.created
    guard.side_effect = None
    with pytest.raises(ProvisioningError, match="credential_missing"):
        credentials.demo_key("shared-data")


def test_key_vault_rechecks_singleton_after_read_before_set():
    class Missing(Exception):
        status_code = 404
        error = SimpleNamespace(code="SecretNotFound")

    client = MagicMock()
    client.get_secret.side_effect = Missing
    guard = MagicMock(side_effect=ProvisioningError("session_stopped"))
    store = AzureKeyVaultCredentialStore(
        CredentialScope("sample", "demo", "azure"),
        client,
        request_errors=(Missing,),
        singleton_writer=True,
        singleton_guard=guard,
    )
    with pytest.raises(ProvisioningError, match="session_stopped"):
        store.get_or_create("management", "mgmt_provisioner")
    guard.assert_called_once_with()
    client.set_secret.assert_not_called()


def test_kubernetes_rechecks_singleton_after_reads_before_create():
    scope = CredentialScope("sample", "demo", "local")
    client = MagicMock()
    client.read_namespace.return_value = SimpleNamespace(
        metadata=SimpleNamespace(
            name="sample-demo-local-management-management", labels=scope.owner_labels()
        )
    )
    client.read_namespaced_secret.side_effect = ApiException(status=404)
    guard = MagicMock(side_effect=ProvisioningError("session_stopped"))
    store = KubernetesCredentialStore(
        scope, "sample-demo-local-management-management", client, singleton_guard=guard
    )
    with pytest.raises(ProvisioningError, match="session_stopped"):
        store.get_or_create("shared-control", "cp_api")
    guard.assert_called_once_with()
    client.create_namespaced_secret.assert_not_called()


@pytest.mark.parametrize(
    ("environment", "provider_class"),
    [
        ("azure", AzureProvider),
        ("local", LocalProvider),
    ],
)
def test_runtime_secret_run_path_uses_service_values_and_never_exports_seed(
    environment, provider_class
):
    config, store = Configuration(environment), Store(environment)
    credentials = StoredCredentials(config, store)
    credentials.bind(
        lambda slot: {
            **database(environment),
            "database": "management" if slot == "management" else "control",
        },
        lambda _: None,
        lambda: None,
    )
    credentials.ensure("management", {"mgmt_api", "mgmt_provisioner", "cp_shared", "cp_isolated_1"})
    credentials.ensure("shared-control", {"cp_api", "cp_reconciler", "dp_reconciler"})
    credentials.ensure("shared-data", set())
    emitted = {}
    stored = {"PROVISIONING_CREDENTIALS_JSON": "synthetic-previous-bundle"}
    uid, version = "11111111-1111-1111-1111-111111111111", "1"
    patches = []

    def secret(slot, namespace, name, values):
        emitted[name] = values
        if name == "provisioner-runtime":
            stored.update(values)

    def kubectl(slot, *args):
        namespace = config.identity.namespace("management")
        if args[:2] == ("get", "namespace"):
            return json.dumps(
                {"metadata": {"name": namespace, "labels": store.scope.owner_labels()}}
            )
        if "patch" in args:
            patch = json.loads(args[args.index("-p") + 1])
            assert patch[:2] == [
                {"op": "test", "path": "/metadata/uid", "value": uid},
                {"op": "test", "path": "/metadata/resourceVersion", "value": version},
            ]
            for operation in patch[2:]:
                assert operation["op"] == "remove"
                del stored[operation["path"].removeprefix("/data/")]
            patches.append(patch)
            return ""
        assert "go-template=" in args[-1] and "{{$value}}" not in args[-1]
        keys = set(stored) & {"PROVISIONING_CREDENTIALS_JSON", "PROVISIONING_CREDENTIALS"}
        return "\n".join([uid, version, "provisioner-runtime", namespace, "Opaque", *sorted(keys)])

    provider = SimpleNamespace(
        config=config,
        credentials=credentials,
        names=lambda slot: (
            "management" if slot == "management" else slot.rsplit("-", 1)[1],
            config.identity.namespace(slot),
        ),
        secret=secret,
        kubectl=kubectl,
        kube_get=lambda *args: None,
    )
    for slot in ("management", "shared-control", "shared-data"):
        provider_class.runtime_secrets(provider, slot)
    assert "PROVISIONING_CREDENTIALS_JSON" not in emitted["provisioner-runtime"]
    assert "PROVISIONING_CREDENTIALS_JSON" not in stored and len(patches) == 1
    assert "DEMO_KEY" not in emitted["provisioner-runtime"]
    assert emitted["data-api-runtime"]["DEMO_KEY"] == store.values["shared-data", "demoKey"]
    assert not any("DSN" in key for key in emitted["data-api-runtime"])
    assert (
        conninfo_to_dict(emitted["data-reconciler-runtime"]["CONTROL_DSN"])["user"]
        == "dp_reconciler"
    )
    created = list(store.created)
    del store.values["shared-data", "demoKey"]
    emitted.clear()
    provider.kube_get = lambda *args: {"metadata": {"name": "data-api-runtime"}}
    with pytest.raises(ProvisioningError, match="credential_missing"):
        provider_class.runtime_secrets(provider, "shared-data")
    assert store.created == created and not emitted
