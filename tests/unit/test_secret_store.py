import base64
import builtins
import copy
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import h11
import pytest
from fastapi.testclient import TestClient
from kubernetes.client import V1Namespace, V1ObjectMeta, V1Secret
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from plane_demo.management import api as management_api
from plane_demo.management.providers import secret_store
from plane_demo.management.providers.secret_store import (
    AzureKeyVaultCredentialStore,
    CredentialScope,
    CredentialStore,
    KubernetesCredentialStore,
    StoreError,
    azure_key_vault_store,
)
from plane_demo.shared.settings import Settings

SECRET = "literal $value /+%:= ' \" \\ # ü " + "a" * 32
OTHER = "other-literal-secret-" + "b" * 32
SLOT = "shared-control"
ROLE = "cp_reconciler"
NAMESPACE = "radplanes-learning-local-shared-control"
BAD_DEMO_KEYS = [
    pytest.param("x" * 32 + "ü", id="unicode"),
    pytest.param("x" * 32 + "\r", id="carriage-return"),
    pytest.param("x" * 32 + "\n", id="line-feed"),
    pytest.param("x" * 32 + "\t", id="tab"),
    pytest.param("x" * 16 + " " + "y" * 16, id="space"),
    pytest.param("x" * 32 + "\0", id="nul"),
    pytest.param("x" * 32 + "\x1f", id="control"),
    pytest.param("x" * 32 + "\x7f", id="delete"),
    pytest.param("x" * 32 + "\xa0", id="nonbreaking-space"),
    pytest.param("x" * 31, id="short"),
    pytest.param("x" * 513, id="long"),
]


class AzureRequestError(Exception):
    def __init__(self, status=403, code="Forbidden"):
        self.status_code = status
        self.error = SimpleNamespace(code=code)
        super().__init__(SECRET)


class FakeVault:
    def __init__(self):
        self.records = {}
        self.reads = []
        self.writes = []
        self.read_error = None
        self.write_error = None
        self.on_write = None

    def record(self, name, labels, value):
        return SimpleNamespace(
            name=name,
            value=value,
            properties=SimpleNamespace(tags=labels, enabled=True, version="original"),
        )

    def get_secret(self, name, **kwargs):
        self.reads.append((name, kwargs))
        if self.read_error:
            raise self.read_error
        if name not in self.records:
            raise AzureRequestError(404, "SecretNotFound")
        return self.records[name]

    def set_secret(self, name, value, **kwargs):
        self.writes.append((name, value, kwargs))
        if self.write_error:
            raise self.write_error
        record = self.record(name, kwargs["tags"], value)
        record.properties.version = str(len(self.writes))
        if self.on_write:
            self.on_write(name, record)
        else:
            self.records[name] = record
        return record


class FakeKubernetes:
    def __init__(self, scope):
        self.records = {}
        self.reads = []
        self.writes = []
        self.namespace_reads = []
        self.read_error = None
        self.write_error = None
        self.namespace_error = None
        self.on_write = None
        self.namespace = V1Namespace(
            metadata=V1ObjectMeta(name=NAMESPACE, labels=scope.owner_labels())
        )

    def record(self, name, labels, value):
        return V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=V1ObjectMeta(name=name, namespace=NAMESPACE, labels=labels),
            type="Opaque",
            immutable=True,
            data={"value": base64.b64encode(value.encode()).decode()},
        )

    def read_namespace(self, name, **kwargs):
        self.namespace_reads.append((name, kwargs))
        if self.namespace_error:
            raise self.namespace_error
        return self.namespace

    def read_namespaced_secret(self, name, namespace, **kwargs):
        self.reads.append((name, namespace, kwargs))
        if self.read_error:
            raise self.read_error
        if name not in self.records:
            raise ApiException(status=404, reason=SECRET)
        return self.records[name]

    def create_namespaced_secret(self, namespace, body, **kwargs):
        self.writes.append((namespace, body, kwargs))
        if self.write_error:
            raise self.write_error
        if self.on_write:
            self.on_write(body.metadata.name, body)
        else:
            self.records[body.metadata.name] = body
        return body


@dataclass
class StoreFixture:
    kind: str
    scope: CredentialScope
    client: Any
    store: CredentialStore

    @property
    def name(self):
        return self.scope.secret_name(SLOT, ROLE)

    def seed(self, value=SECRET, *, slot=SLOT, role=ROLE):
        name = self.scope.secret_name(slot, role)
        record = self.client.record(name, self.scope.labels(slot, role), value)
        self.client.records[name] = record
        return record

    def error(self, status):
        if self.kind == "azure":
            return AzureRequestError(status)
        return ApiException(status=status, reason=SECRET)

    def labels(self, record):
        return record.properties.tags if self.kind == "azure" else record.metadata.labels


@pytest.fixture(params=["azure", "local"])
def backend(request):
    scope = CredentialScope("radplanes", "learning", request.param)
    if request.param == "azure":
        client = FakeVault()
        store = AzureKeyVaultCredentialStore(
            scope, client, request_errors=(AzureRequestError,), singleton_writer=True
        )
    else:
        client = FakeKubernetes(scope)
        store = KubernetesCredentialStore(scope, NAMESPACE, client)
    return StoreFixture(request.param, scope, client, store)


@pytest.fixture
def no_generation(monkeypatch):
    generator = Mock(side_effect=AssertionError("unexpected credential generation"))
    monkeypatch.setattr(secret_store.secrets, "token_urlsafe", generator)
    return generator


def test_get_existing_is_read_only_and_redacted(backend, no_generation):
    original = copy.deepcopy(backend.seed())
    result = backend.store.get(SLOT, ROLE)
    assert result.value == SECRET
    assert SECRET not in repr(result)
    assert SECRET not in str(result)
    assert SECRET not in repr(backend.store)
    assert SECRET not in repr(StoreError("credential_conflict"))
    assert not backend.client.writes
    assert backend.client.records[backend.name] == original


@pytest.mark.parametrize("provided", [None, SECRET])
def test_existing_is_reused_without_rotation_or_generation(backend, no_generation, provided):
    original = copy.deepcopy(backend.seed())
    for _ in range(2):
        result = backend.store.get_or_create(SLOT, ROLE, provided_value=provided)
        assert result.value == SECRET
    assert backend.client.records[backend.name] == original
    assert not backend.client.writes


def test_changed_supplied_value_conflicts_without_rotation(backend, no_generation):
    original = copy.deepcopy(backend.seed())
    with pytest.raises(StoreError, match="^credential_conflict$"):
        backend.store.get_or_create(SLOT, ROLE, provided_value=OTHER)
    assert backend.client.records[backend.name] == original
    assert not backend.client.writes


@pytest.mark.parametrize("method", ["get", "required", "required-provided"])
def test_missing_existing_credential_never_regenerates(backend, no_generation, method):
    with pytest.raises(StoreError, match="^credential_missing$"):
        if method == "get":
            backend.store.get(SLOT, ROLE)
        else:
            backend.store.get_or_create(
                SLOT,
                ROLE,
                require_existing=True,
                provided_value=SECRET if method == "required-provided" else None,
            )
    assert not backend.client.writes


@pytest.mark.parametrize("provided", [None, SECRET, "x" * 32])
def test_create_persists_once_then_reads_exact_owned_value(backend, monkeypatch, provided):
    generator = Mock(return_value=SECRET)
    monkeypatch.setattr(secret_store.secrets, "token_urlsafe", generator)
    first = backend.store.get_or_create(SLOT, ROLE, provided_value=provided)
    second = backend.store.get_or_create(SLOT, ROLE, provided_value=provided)
    assert first.value == second.value == (provided or SECRET)
    assert len(backend.client.writes) == 1
    assert len(backend.client.reads) == 3
    if provided is None:
        generator.assert_called_once_with(48)
    else:
        generator.assert_not_called()
    record = backend.client.records[backend.name]
    assert backend.labels(record) == backend.scope.labels(SLOT, ROLE)
    if backend.kind == "azure":
        assert record.properties.version == "1"
        kwargs = backend.client.writes[0][2]
        assert kwargs["logging_enable"] is False
        assert kwargs["retry_total"] == 0
        assert kwargs["enabled"] is True
        assert all(kwargs["logging_enable"] is False for _, kwargs in backend.client.reads)
    else:
        assert record.metadata.namespace == NAMESPACE
        assert record.immutable is True
        assert record.type == "Opaque"
        assert record.data == {"value": base64.b64encode(first.value.encode()).decode()}
        assert all(namespace == NAMESPACE for _, namespace, _ in backend.client.reads)
        assert backend.client.writes[0][0] == NAMESPACE
        assert backend.client.writes[0][2] == {"_request_timeout": (5, 15)}
        assert all(name == NAMESPACE for name, _ in backend.client.namespace_reads)


@pytest.mark.parametrize("provided", [None, SECRET, OTHER])
def test_create_conflict_reads_winner_and_checks_supplied_value(backend, monkeypatch, provided):
    monkeypatch.setattr(secret_store.secrets, "token_urlsafe", lambda _: OTHER)

    def competing_write(name, record):
        backend.seed()
        raise backend.error(409)

    backend.client.on_write = competing_write
    if provided == OTHER:
        with pytest.raises(StoreError, match="^credential_conflict$"):
            backend.store.get_or_create(SLOT, ROLE, provided_value=provided)
    else:
        assert backend.store.get_or_create(SLOT, ROLE, provided_value=provided).value == SECRET
    assert backend.store.get(SLOT, ROLE).value == SECRET
    assert len(backend.client.writes) == 1


@pytest.mark.parametrize("winner", ["foreign", "invalid", "missing"])
def test_create_conflict_never_adopts_unverified_winner(backend, winner):
    def competing_write(name, record):
        if winner != "missing":
            existing = backend.seed(value="short" if winner == "invalid" else SECRET)
            if winner == "foreign":
                backend.labels(existing)["plane-demo/deployment"] = "foreign"
        raise backend.error(409)

    backend.client.on_write = competing_write
    expected = {
        "foreign": "credential_owner_mismatch",
        "invalid": "invalid_credential_value",
        "missing": "credential_conflict",
    }[winner]
    with pytest.raises(StoreError, match=f"^{expected}$"):
        backend.store.get_or_create(SLOT, ROLE, provided_value=SECRET)
    assert len(backend.client.writes) == 1


@pytest.mark.parametrize("after_write", ["changed", "foreign", "missing"])
def test_successful_write_is_read_back_and_conflicts_are_not_repaired(backend, after_write):
    def overwrite(name, record):
        if after_write != "missing":
            existing = backend.seed(OTHER if after_write == "changed" else SECRET)
            if after_write == "foreign":
                backend.labels(existing)["plane-demo/project"] = "foreign"

    backend.client.on_write = overwrite
    code = "credential_owner_mismatch" if after_write == "foreign" else "credential_conflict"
    with pytest.raises(StoreError, match=f"^{code}$"):
        backend.store.get_or_create(SLOT, ROLE, provided_value=SECRET)
    assert len(backend.client.writes) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "credential_store_access_denied"),
        (403, "credential_store_access_denied"),
        (408, "credential_store_unavailable"),
        (429, "credential_store_unavailable"),
        (500, "credential_store_unavailable"),
        (503, "credential_store_unavailable"),
        (400, "credential_store_failed"),
    ],
)
@pytest.mark.parametrize("operation", ["read", "write"])
def test_service_errors_are_explicit_redacted_and_never_retried(backend, status, code, operation):
    setattr(backend.client, f"{operation}_error", backend.error(status))
    with pytest.raises(StoreError, match=f"^{code}$") as caught:
        backend.store.get_or_create(SLOT, ROLE, provided_value=SECRET)
    assert caught.value.code == code
    assert SECRET not in repr(caught.value)
    assert SECRET not in "".join(traceback.format_exception(caught.value))
    assert len(backend.client.writes) == (operation == "write")
    assert len(backend.client.reads) == 1


@pytest.mark.parametrize(
    "label",
    [
        "plane-demo/project",
        "plane-demo/deployment",
        "plane-demo/environment",
        "plane-demo/source",
        "plane-demo/credential-schema",
        "plane-demo/slot",
        "plane-demo/credential-role",
    ],
)
@pytest.mark.parametrize("method", ["get", "get_or_create"])
def test_each_ownership_field_is_required(backend, no_generation, label, method):
    record = backend.seed()
    backend.labels(record).pop(label)
    with pytest.raises(StoreError, match="^credential_owner_mismatch$"):
        getattr(backend.store, method)(SLOT, ROLE)
    assert not backend.client.writes


@pytest.mark.parametrize(
    ("slot", "role"),
    [
        ("management", "demoKey"),
        ("management", "mgmt_api"),
        ("management", "mgmt_provisioner"),
        ("management", "cp_shared"),
        ("management", "cp_isolated_1"),
        ("isolated-1-control", "cp_api"),
        ("isolated-1-control", "cp_reconciler"),
        ("isolated-1-control", "dp_reconciler"),
        ("isolated-1-control", "demoKey"),
        ("isolated-1-data", "demoKey"),
    ],
)
def test_existing_logical_slot_and_role_values_are_preserved(backend, slot, role):
    value = OTHER if role == "demoKey" else SECRET
    assert backend.store.get_or_create(slot, role, provided_value=value).value == value
    record = backend.client.records[backend.scope.secret_name(slot, role)]
    labels = backend.labels(record)
    assert labels["plane-demo/slot"] == slot
    assert labels["plane-demo/credential-role"] == role


@pytest.mark.parametrize(
    ("slot", "role"),
    [
        ("", "demoKey"),
        ("Management", "mgmt_api"),
        ("shared", "demoKey"),
        ("-control", "cp_api"),
        ("shared-control/other", "cp_api"),
        ("shared-control", "cp_API"),
        ("shared-control", "mgmt_api"),
        ("shared-data", "dp_reconciler"),
        ("management", "plane_setup"),
        ("management", "cp_hyphen-name"),
        ("management", "cp_secret;select"),
        ("management", "demo_key"),
        (None, "demoKey"),
        ("management", None),
        ("a" * 33 + "-control", "cp_api"),
    ],
)
@pytest.mark.parametrize("method", ["get", "get_or_create"])
def test_invalid_references_fail_before_service_access(backend, slot, role, method):
    with pytest.raises(StoreError, match="^invalid_credential_reference$"):
        getattr(backend.store, method)(slot, role)
    assert not backend.client.reads
    assert not backend.client.writes


@pytest.mark.parametrize(
    "value", ["", "x" * 31, "x" * 32 + "\0", 123, b"x" * 32, SECRET + "\ud800"]
)
def test_invalid_supplied_values_fail_without_service_access(backend, value):
    with pytest.raises(StoreError, match="^invalid_credential_value$") as caught:
        backend.store.get_or_create(SLOT, ROLE, provided_value=value)
    assert SECRET not in "".join(traceback.format_exception(caught.value))
    assert not backend.client.reads
    assert not backend.client.writes


def test_invalid_existing_value_is_not_replaced(backend, no_generation):
    backend.seed("short")
    with pytest.raises(StoreError, match="^invalid_credential_value$"):
        backend.store.get_or_create(SLOT, ROLE)
    assert not backend.client.writes


@pytest.mark.parametrize("value", BAD_DEMO_KEYS)
@pytest.mark.parametrize("slot", ["management", "shared-control", "shared-data"])
def test_rejects_invalid_supplied_demo_key_before_service_access(
    backend, no_generation, value, slot
):
    with pytest.raises(StoreError, match="^invalid_credential_value$") as caught:
        backend.store.get_or_create(slot, "demoKey", provided_value=value)
    assert value not in str(caught.value)
    assert not backend.client.reads
    assert not backend.client.writes


@pytest.mark.parametrize("value", BAD_DEMO_KEYS)
@pytest.mark.parametrize("method", ["get", "get_or_create"])
def test_invalid_stored_demo_key_is_not_returned_or_repaired(backend, no_generation, value, method):
    original = copy.deepcopy(backend.seed(value, role="demoKey"))
    with pytest.raises(StoreError, match="^invalid_credential_value$") as caught:
        getattr(backend.store, method)(SLOT, "demoKey")
    assert value not in str(caught.value)
    assert backend.client.records[backend.scope.secret_name(SLOT, "demoKey")] == original
    assert not backend.client.writes


def test_valid_supplied_demo_key_does_not_replace_an_invalid_stored_key(backend, no_generation):
    original = copy.deepcopy(backend.seed(SECRET, role="demoKey"))
    with pytest.raises(StoreError, match="^invalid_credential_value$"):
        backend.store.get_or_create(SLOT, "demoKey", provided_value=OTHER)
    assert backend.client.records[backend.scope.secret_name(SLOT, "demoKey")] == original
    assert not backend.client.writes


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("value", [SECRET, "x" * 32 + "\r\n"])
def test_readback_rejects_invalid_demo_key_even_after_create_conflict(backend, conflict, value):
    def competing_write(name, record):
        backend.seed(value, role="demoKey")
        if conflict:
            raise backend.error(409)

    backend.client.on_write = competing_write
    with pytest.raises(StoreError, match="^invalid_credential_value$"):
        backend.store.get_or_create(SLOT, "demoKey", provided_value=OTHER)
    assert len(backend.client.writes) == 1


def test_generated_demo_key_is_validated_before_writing(backend, monkeypatch):
    monkeypatch.setattr(secret_store.secrets, "token_urlsafe", lambda _: SECRET)
    with pytest.raises(StoreError, match="^invalid_credential_value$"):
        backend.store.get_or_create(SLOT, "demoKey")
    assert not backend.client.writes


@pytest.mark.parametrize("value", [None, "!" * 32, "~" * 512, "".join(map(chr, range(33, 127)))])
def test_stored_demo_key_survives_httpx_wire_encoding_and_existing_api_auth(
    backend, monkeypatch, value
):
    created = backend.store.get_or_create("management", "demoKey", provided_value=value)
    stored = backend.store.get("management", "demoKey")
    assert created.value == stored.value
    if value is not None:
        assert stored.value == value
    assert 32 <= len(stored.value) <= 512
    assert all("!" <= character <= "~" for character in stored.value)
    assert backend.store.get_or_create("management", "demoKey").value == stored.value
    assert len(backend.client.writes) == 1

    connect = Mock(side_effect=AssertionError("authentication test accessed a database"))
    monkeypatch.setattr(management_api, "connect", connect)
    with TestClient(management_api.create_app(Settings(demo_key=stored.value))) as client:
        request = client.build_request(
            "POST", "/tenants", headers={"X-Demo-Key": stored.value}, json={}
        )
        assert (b"X-Demo-Key", stored.value.encode("ascii")) in request.headers.raw
        wire = h11.Connection(h11.CLIENT).send(
            h11.Request(
                method=request.method, target=request.url.raw_path, headers=request.headers.raw
            )
        )
        assert b"X-Demo-Key: " + stored.value.encode("ascii") + b"\r\n" in wire
        assert client.send(request).status_code == 422
        assert client.post("/tenants", headers={"X-Demo-Key": OTHER}, json={}).status_code == 401
        assert client.post("/tenants", json={}).status_code == 401
    connect.assert_not_called()


@pytest.mark.parametrize("value", [SECRET + "😀", SECRET + "\r\n\t", "x" * 513])
def test_password_roles_retain_unicode_whitespace_and_long_literal_values(backend, value):
    assert backend.store.get_or_create(SLOT, ROLE, provided_value=value).value == value
    assert backend.store.get(SLOT, ROLE).value == value
    assert backend.store.get_or_create(SLOT, ROLE, provided_value=value).value == value
    assert len(backend.client.writes) == 1


def test_store_operations_do_not_open_files(backend, monkeypatch):
    with monkeypatch.context() as guarded:
        guarded.setattr(
            builtins, "open", Mock(side_effect=AssertionError("credential store opened a file"))
        )
        assert backend.store.get_or_create(SLOT, ROLE, provided_value=SECRET).value == SECRET
        assert backend.store.get(SLOT, ROLE).value == SECRET


def test_store_disappearance_during_create_is_not_retried(backend):
    backend.client.write_error = backend.error(404)
    with pytest.raises(StoreError, match="^credential_store_missing$"):
        backend.store.get_or_create(SLOT, ROLE, provided_value=SECRET)
    assert len(backend.client.writes) == 1


@pytest.mark.parametrize(
    ("project", "deployment", "environment"),
    [
        ("", "learning", "local"),
        ("bad/project", "learning", "local"),
        ("MixedCase", "learning", "azure"),
        ("project", "trailing-", "azure"),
        ("project", "a" * 49, "azure"),
        ("project", None, "local"),
        ("project", "learning", "other"),
    ],
)
def test_invalid_scope_is_rejected(project, deployment, environment):
    with pytest.raises(StoreError, match="^invalid_credential_scope$"):
        CredentialScope(project, deployment, environment)


def test_names_include_complete_scope_and_reference_without_join_collisions():
    scopes = [
        CredentialScope("project", "deployment", "local"),
        CredentialScope("different", "deployment", "local"),
        CredentialScope("project", "different", "local"),
        CredentialScope("project", "deployment", "azure"),
        CredentialScope("a-b", "c", "local"),
        CredentialScope("a", "b-c", "local"),
    ]
    names = {scope.secret_name(SLOT, ROLE) for scope in scopes}
    names.add(scopes[0].secret_name("other-control", ROLE))
    names.add(scopes[0].secret_name(SLOT, "cp_api"))
    assert len(names) == 8
    assert scopes[0].secret_name(SLOT, ROLE) == scopes[0].secret_name(SLOT, ROLE)
    assert all(
        len(name) <= 127 and set(name) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
        for name in names
    )


@pytest.fixture
def vault():
    scope = CredentialScope("radplanes", "learning", "azure")
    client = FakeVault()
    store = AzureKeyVaultCredentialStore(scope, client, request_errors=(AzureRequestError,))
    return StoreFixture("azure", scope, client, store)


def test_vault_reads_do_not_require_writer_acknowledgment(vault, no_generation):
    vault.seed()
    assert vault.store.get_or_create(SLOT, ROLE).value == SECRET
    assert not vault.client.writes


def test_vault_creation_requires_explicit_singleton_acknowledgment(vault, no_generation):
    with pytest.raises(StoreError, match="^credential_store_singleton_required$"):
        vault.store.get_or_create(SLOT, ROLE)
    assert not vault.client.writes


@pytest.mark.parametrize("code", ["VaultNotFound", "NotFound", None])
def test_only_specific_secret_not_found_allows_creation(vault, no_generation, code):
    vault.client.read_error = AzureRequestError(404, code)
    with pytest.raises(StoreError, match="^credential_store_missing$"):
        vault.store.get_or_create(SLOT, ROLE)
    assert not vault.client.writes


def test_vault_transport_failure_is_not_treated_as_missing(vault, no_generation):
    vault.client.read_error = AzureRequestError(None, "ConnectionFailed")
    with pytest.raises(StoreError, match="^credential_store_unavailable$"):
        vault.store.get_or_create(SLOT, ROLE)
    assert not vault.client.writes


@pytest.mark.parametrize("mutation", ["name", "disabled", "tags", "properties"])
def test_invalid_vault_record_refuses_adoption(vault, no_generation, mutation):
    record = vault.seed()
    if mutation == "name":
        record.name = "foreign"
    elif mutation == "disabled":
        record.properties.enabled = False
    elif mutation == "tags":
        record.properties.tags = None
    else:
        record.properties = None
    with pytest.raises(StoreError):
        vault.store.get_or_create(SLOT, ROLE)
    assert not vault.client.writes


def test_uncertain_vault_write_is_not_retried_or_rotated_on_reentry(vault):
    store = AzureKeyVaultCredentialStore(
        vault.scope, vault.client, request_errors=(AzureRequestError,), singleton_writer=True
    )

    def write_then_disconnect(name, record):
        vault.client.records[name] = record
        raise AzureRequestError(None, "ConnectionLost")

    vault.client.on_write = write_then_disconnect
    with pytest.raises(StoreError, match="^credential_store_unavailable$"):
        store.get_or_create(SLOT, ROLE, provided_value=SECRET)
    assert store.get_or_create(SLOT, ROLE, provided_value=SECRET).value == SECRET
    assert len(vault.client.writes) == 1


@pytest.fixture
def kube():
    scope = CredentialScope("radplanes", "learning", "local")
    client = FakeKubernetes(scope)
    store = KubernetesCredentialStore(scope, NAMESPACE, client)
    return StoreFixture("local", scope, client, store)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "credential_store_missing"),
        (403, "credential_store_access_denied"),
        (503, "credential_store_unavailable"),
    ],
)
def test_namespace_must_exist_and_be_readable_before_generation(kube, no_generation, status, code):
    kube.client.namespace_error = ApiException(status=status, reason=SECRET)
    with pytest.raises(StoreError, match=f"^{code}$"):
        kube.store.get_or_create(SLOT, ROLE)
    assert not kube.client.reads
    assert not kube.client.writes


@pytest.mark.parametrize("mutation", ["name", "project", "deployment", "environment", "labels"])
def test_namespace_ownership_is_verified(kube, no_generation, mutation):
    metadata = kube.client.namespace.metadata
    if mutation == "name":
        metadata.name = "foreign"
    elif mutation == "labels":
        metadata.labels = None
    else:
        metadata.labels[f"plane-demo/{mutation}"] = "foreign"
    with pytest.raises(StoreError, match="^credential_owner_mismatch$"):
        kube.store.get_or_create(SLOT, ROLE)
    assert not kube.client.reads
    assert not kube.client.writes


@pytest.mark.parametrize(
    "mutation",
    ["name", "namespace", "type", "mutable", "missing", "extra", "base64", "utf8", "none"],
)
def test_kubernetes_secret_contract_is_exact(kube, no_generation, mutation):
    record = kube.seed()
    if mutation in ("name", "namespace"):
        setattr(record.metadata, mutation, "foreign")
    elif mutation == "type":
        record.type = "kubernetes.io/tls"
    elif mutation == "mutable":
        record.immutable = False
    elif mutation == "missing":
        record.data = {}
    elif mutation == "extra":
        record.data["extra"] = record.data["value"]
    elif mutation == "base64":
        record.data["value"] = "!not-base64"
    elif mutation == "utf8":
        record.data["value"] = base64.b64encode(b"\xff" * 48).decode()
    else:
        record.data = None
    with pytest.raises(StoreError):
        kube.store.get_or_create(SLOT, ROLE)
    assert not kube.client.writes


@pytest.mark.parametrize("operation", ["namespace", "read", "write"])
def test_kubernetes_transport_errors_are_sanitized(kube, operation):
    setattr(kube.client, f"{operation}_error", HTTPError(SECRET))
    with pytest.raises(StoreError, match="^credential_store_unavailable$") as caught:
        kube.store.get_or_create(SLOT, ROLE, provided_value=SECRET)
    assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("namespace", ["", "../foreign", "a" * 64, "UPPER", "trailing-", None])
def test_invalid_namespace_cannot_reach_client(kube, namespace):
    with pytest.raises(StoreError, match="^invalid_credential_scope$"):
        KubernetesCredentialStore(kube.scope, namespace, kube.client)
    assert not kube.client.reads


def test_adapters_refuse_wrong_environment_before_client_access(kube, vault):
    with pytest.raises(StoreError, match="^invalid_credential_scope$"):
        KubernetesCredentialStore(vault.scope, NAMESPACE, kube.client)
    with pytest.raises(StoreError, match="^invalid_credential_scope$"):
        AzureKeyVaultCredentialStore(kube.scope, vault.client, request_errors=(AzureRequestError,))


def test_local_import_and_store_work_with_all_azure_imports_blocked():
    script = """
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'azure' or name.startswith('azure.'):
        raise AssertionError('local attempted an Azure import')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from types import SimpleNamespace
from plane_demo.management.providers.secret_store import CredentialScope, KubernetesCredentialStore
scope = CredentialScope('project', 'deployment', 'local')
name = scope.secret_name('management', 'demoKey')
class Client:
    def read_namespace(self, namespace, **kwargs):
        return SimpleNamespace(
            metadata=SimpleNamespace(name=namespace, labels=scope.owner_labels()))
    def read_namespaced_secret(self, name, namespace, **kwargs):
        import base64
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name, namespace=namespace,
                labels=scope.labels('management', 'demoKey')),
            type='Opaque', immutable=True,
            data={'value': base64.b64encode(b'x' * 32).decode()})
store = KubernetesCredentialStore(scope, 'owned-namespace', Client())
assert store.get_or_create('management', 'demoKey').value == 'x' * 32
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_missing_azure_dependency_is_explicit_and_not_a_cloud_call(vault, monkeypatch):
    real_import = builtins.__import__

    def block_azure(name, *args, **kwargs):
        if name.startswith("azure"):
            raise ModuleNotFoundError(SECRET)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_azure)
    with pytest.raises(StoreError, match="^credential_store_dependency_missing$") as caught:
        azure_key_vault_store(vault.scope, "https://owned-vault.vault.azure.net")
    assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "url",
    [
        "http://owned-vault.vault.azure.net",
        "https://foreign.example",
        "https://user:password@owned-vault.vault.azure.net",
        "https://owned-vault.vault.azure.net/secret",
        "https://owned-vault.vault.azure.net?query=secret",
        "https://owned-vault.vault.azure.net:8443",
        None,
    ],
)
def test_factory_rejects_unsafe_endpoint_before_import(vault, url):
    with pytest.raises(StoreError, match="^invalid_credential_scope$"):
        azure_key_vault_store(vault.scope, url)


def test_factory_rejects_local_scope_before_import(kube):
    with pytest.raises(StoreError, match="^invalid_credential_scope$"):
        azure_key_vault_store(kube.scope, "https://owned-vault.vault.azure.net")


def test_azure_factory_wires_injected_identity_and_safe_sdk_options(vault, monkeypatch):
    constructor = Mock(return_value=vault.client)
    default_identity = Mock()
    modules = {
        "azure": ModuleType("azure"),
        "azure.core": ModuleType("azure.core"),
        "azure.core.exceptions": ModuleType("azure.core.exceptions"),
        "azure.identity": ModuleType("azure.identity"),
        "azure.keyvault": ModuleType("azure.keyvault"),
        "azure.keyvault.secrets": ModuleType("azure.keyvault.secrets"),
    }
    modules["azure.core.exceptions"].AzureError = AzureRequestError
    modules["azure.identity"].DefaultAzureCredential = default_identity
    modules["azure.keyvault.secrets"].SecretClient = constructor
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    identity = object()
    store = azure_key_vault_store(
        vault.scope,
        "https://owned-vault.vault.azure.net",
        credential=identity,
        singleton_writer=True,
    )
    assert store.get_or_create(SLOT, ROLE, provided_value=SECRET).value == SECRET
    default_identity.assert_not_called()
    constructor.assert_called_once_with(
        vault_url="https://owned-vault.vault.azure.net",
        credential=identity,
        logging_enable=False,
        retry_total=0,
        connection_timeout=5,
        read_timeout=15,
    )
    constructor.side_effect = AzureRequestError(403)
    with pytest.raises(StoreError, match="^credential_store_access_denied$") as caught:
        azure_key_vault_store(
            vault.scope, "https://owned-vault.vault.azure.net", credential=identity
        )
    assert SECRET not in "".join(traceback.format_exception(caught.value))


def test_error_constructor_cannot_expose_arbitrary_backend_text():
    with pytest.raises(ValueError, match="^invalid credential store error code$") as caught:
        StoreError(SECRET)
    assert SECRET not in str(caught.value)
