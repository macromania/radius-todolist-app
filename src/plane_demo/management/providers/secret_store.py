"""Privileged, service-owned credentials. This module never persists credentials to files.

Public APIs consume injected runtime values, not these stores. Callers must check
database initialization before choosing get_or_create rather than get (or
require_existing=True). Key Vault writers must hold the existing singleton lock:
its set-secret API has no atomic create-if-absent operation. A read after writing
detects some conflicts, but does not provide distributed compare-and-swap.

demoKey values must be 32 to 512 printable ASCII characters without whitespace
so HTTP clients can serialize them. PostgreSQL password roles retain literal UTF-8.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, get_args

from kubernetes.client import V1ObjectMeta, V1Secret
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

StoreErrorCode = Literal[
    "invalid_credential_scope",
    "invalid_credential_reference",
    "invalid_credential_value",
    "invalid_credential_record",
    "credential_missing",
    "credential_conflict",
    "credential_owner_mismatch",
    "credential_store_missing",
    "credential_store_access_denied",
    "credential_store_unavailable",
    "credential_store_failed",
    "credential_store_singleton_required",
    "credential_store_dependency_missing",
]
IDENTIFIER = re.compile(r"[a-z](?:[a-z0-9-]{0,46}[a-z0-9])?")
PAIR = re.compile(r"[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?")
NAMESPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
DEMO_KEY = re.compile(r"[!-~]{32,512}")
SECRET_FIELD = "value"


class StoreError(RuntimeError):
    """Stable error codes only; never include backend diagnostics or secret values."""

    def __init__(self, code: StoreErrorCode):
        if code not in get_args(StoreErrorCode):
            raise ValueError("invalid credential store error code")
        self.code = code
        super().__init__(code)


def _validate_reference(slot: str, role: str) -> None:
    if not isinstance(slot, str) or not isinstance(role, str):
        raise StoreError("invalid_credential_reference")
    if slot == "management":
        valid = role in {"demoKey", "mgmt_api", "mgmt_provisioner", "management_admin"} or bool(
            re.fullmatch(r"cp_[a-z](?:[a-z0-9_]{0,30}[a-z0-9])?", role)
        )
    else:
        pair, separator, plane = slot.rpartition("-")
        valid = bool(separator and PAIR.fullmatch(pair)) and (
            (plane == "data" and role == "demoKey")
            or (
                plane == "control"
                and role in {"demoKey", "cp_api", "cp_reconciler", "dp_reconciler"}
            )
        )
    if not valid:
        raise StoreError("invalid_credential_reference")


@dataclass(frozen=True)
class CredentialScope:
    project: str
    deployment: str
    environment: Literal["azure", "local"]

    def __post_init__(self) -> None:
        if self.environment not in ("azure", "local") or any(
            not isinstance(value, str) or not IDENTIFIER.fullmatch(value)
            for value in (self.project, self.deployment)
        ):
            raise StoreError("invalid_credential_scope")

    def owner_labels(self) -> dict[str, str]:
        """Labels required on the discovered local namespace as well as its Secrets."""
        return {
            "plane-demo/project": self.project,
            "plane-demo/deployment": self.deployment,
            "plane-demo/environment": self.environment,
        }

    def labels(self, slot: str, role: str) -> dict[str, str]:
        _validate_reference(slot, role)
        return {
            **self.owner_labels(),
            "plane-demo/source": "credential-store",
            "plane-demo/credential-schema": "v1",
            "plane-demo/slot": slot,
            "plane-demo/credential-role": role,
        }

    def secret_name(self, slot: str, role: str) -> str:
        _validate_reference(slot, role)
        identity = json.dumps(
            [self.project, self.deployment, self.environment, slot, role],
            separators=(",", ":"),
        )
        # Hash the complete tuple, not a truncated or ambiguous joined identifier.
        return "credential-" + hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True)
class CredentialValue:
    """Access plaintext explicitly through .value; string/repr output is redacted."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or len(self.value) < 32 or "\0" in self.value:
            raise StoreError("invalid_credential_value")
        try:
            self.value.encode("utf-8")
        except UnicodeError:
            raise StoreError("invalid_credential_value") from None

    def __repr__(self) -> str:
        return "CredentialValue(<redacted>)"


def _credential_value(value: str, role: str) -> CredentialValue:
    credential = CredentialValue(value)
    if role == "demoKey" and not DEMO_KEY.fullmatch(credential.value):
        raise StoreError("invalid_credential_value")
    return credential


class CredentialStore(Protocol):
    scope: CredentialScope

    def get(self, slot: str, role: str) -> CredentialValue: ...

    def get_or_create(
        self,
        slot: str,
        role: str,
        *,
        provided_value: str | None = None,
        require_existing: bool = False,
    ) -> CredentialValue: ...


class KeyVaultClient(Protocol):
    def get_secret(self, name: str, **kwargs: Any) -> Any: ...

    def set_secret(self, name: str, value: str, **kwargs: Any) -> Any: ...
    def close(self) -> None: ...


class KubernetesClient(Protocol):
    def read_namespace(self, name: str, **kwargs: Any) -> Any: ...

    def read_namespaced_secret(self, name: str, namespace: str, **kwargs: Any) -> Any: ...

    def create_namespaced_secret(self, namespace: str, body: V1Secret, **kwargs: Any) -> Any: ...


def _request_error(status: int | None) -> StoreError:
    if status in (401, 403):
        return StoreError("credential_store_access_denied")
    if status == 404:
        return StoreError("credential_store_missing")
    if status == 409:
        return StoreError("credential_conflict")
    if status is None or status in (408, 429) or status >= 500:
        return StoreError("credential_store_unavailable")
    return StoreError("credential_store_failed")


def _verify_labels(actual: Any, expected: dict[str, str]) -> None:
    if not isinstance(actual, dict) or any(
        actual.get(key) != value for key, value in expected.items()
    ):
        raise StoreError("credential_owner_mismatch")


class _OwnedStore:
    def get(self, slot: str, role: str) -> CredentialValue:
        _validate_reference(slot, role)
        value = self._read(slot, role)
        if value is None:
            raise StoreError("credential_missing")
        return value

    def get_or_create(
        self,
        slot: str,
        role: str,
        *,
        provided_value: str | None = None,
        require_existing: bool = False,
    ) -> CredentialValue:
        _validate_reference(slot, role)
        provided = _credential_value(provided_value, role) if provided_value is not None else None
        existing = self._read(slot, role)
        if existing is not None:
            return self._match(existing, provided)
        if require_existing:
            raise StoreError("credential_missing")
        self._allow_create()
        candidate = provided or _credential_value(secrets.token_urlsafe(48), role)
        created = self._create(slot, role, candidate)
        observed = self._read(slot, role)
        if observed is None:
            raise StoreError("credential_conflict")
        # A successful write must remain visible unchanged. A rejected create may
        # reuse another writer's owned value, but an explicit input must still match.
        return self._match(observed, candidate if created else provided)

    @staticmethod
    def _match(existing: CredentialValue, expected: CredentialValue | None) -> CredentialValue:
        if expected is not None and existing.value != expected.value:
            raise StoreError("credential_conflict")
        return existing

    def _allow_create(self) -> None:
        pass

    def _read(self, slot: str, role: str) -> CredentialValue | None:
        raise NotImplementedError

    def _create(self, slot: str, role: str, value: CredentialValue) -> bool:
        raise NotImplementedError


class AzureKeyVaultCredentialStore(_OwnedStore):
    """Inject a SecretClient and its specific SDK exception classes.

    singleton_writer=True acknowledges an external singleton lock; this class
    cannot acquire that lock or make Key Vault's versioned set-secret atomic.
    Failed/uncertain writes are not retried. Re-entry must read the stored value.
    """

    def __init__(
        self,
        scope: CredentialScope,
        client: KeyVaultClient,
        *,
        request_errors: tuple[type[Exception], ...],
        singleton_writer: bool = False,
        singleton_guard: Callable[[], None] | None = None,
    ):
        if scope.environment != "azure":
            raise StoreError("invalid_credential_scope")
        self.scope = scope
        self._client = client
        self._request_errors = request_errors
        self._singleton_writer = singleton_writer
        self._singleton_guard = singleton_guard

    def _allow_create(self) -> None:
        if self._singleton_writer is not True:
            raise StoreError("credential_store_singleton_required")
        if self._singleton_guard is not None:
            self._singleton_guard()

    def close(self) -> None:
        self._client.close()

    def _read(self, slot: str, role: str) -> CredentialValue | None:
        name = self.scope.secret_name(slot, role)
        try:
            record = self._client.get_secret(name, logging_enable=False)
        except self._request_errors as error:
            status = getattr(error, "status_code", None)
            if status == 404 and getattr(getattr(error, "error", None), "code", None) == (
                "SecretNotFound"
            ):
                return None
            raise _request_error(status) from None
        if getattr(record, "name", None) != name:
            raise StoreError("credential_owner_mismatch")
        properties = getattr(record, "properties", None)
        _verify_labels(getattr(properties, "tags", None), self.scope.labels(slot, role))
        if getattr(properties, "enabled", None) is not True:
            raise StoreError("invalid_credential_record")
        return _credential_value(getattr(record, "value", None), role)

    def _create(self, slot: str, role: str, value: CredentialValue) -> bool:
        try:
            self._client.set_secret(
                self.scope.secret_name(slot, role),
                value.value,
                tags=self.scope.labels(slot, role),
                enabled=True,
                logging_enable=False,
                retry_total=0,
            )
        except self._request_errors as error:
            if getattr(error, "status_code", None) == 409:
                return False
            raise _request_error(getattr(error, "status_code", None)) from None
        return True


class KubernetesCredentialStore(_OwnedStore):
    """Use an explicitly discovered namespace and a privileged Kubernetes client.

    The namespace must carry scope.owner_labels(). Each immutable Opaque Secret
    has exactly one base64-encoded 'value' field. Only named reads and creates are
    used, never Secret listing, patching, or apply. Public API identities must not
    receive this store's namespace/Secret permissions.
    """

    def __init__(
        self,
        scope: CredentialScope,
        namespace: str,
        client: KubernetesClient,
        *,
        singleton_guard: Callable[[], None] | None = None,
    ):
        if (
            scope.environment != "local"
            or not isinstance(namespace, str)
            or not NAMESPACE.fullmatch(namespace)
        ):
            raise StoreError("invalid_credential_scope")
        self.scope = scope
        self.namespace = namespace
        self._client = client
        self._singleton_guard = singleton_guard

    def _verify_namespace(self) -> None:
        try:
            record = self._client.read_namespace(self.namespace, _request_timeout=(5, 15))
        except ApiException as error:
            raise _request_error(error.status) from None
        except HTTPError:
            raise StoreError("credential_store_unavailable") from None
        metadata = getattr(record, "metadata", None)
        if getattr(metadata, "name", None) != self.namespace:
            raise StoreError("credential_owner_mismatch")
        _verify_labels(getattr(metadata, "labels", None), self.scope.owner_labels())

    def _read(self, slot: str, role: str) -> CredentialValue | None:
        self._verify_namespace()
        name = self.scope.secret_name(slot, role)
        try:
            record = self._client.read_namespaced_secret(
                name, self.namespace, _request_timeout=(5, 15)
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise _request_error(error.status) from None
        except HTTPError:
            raise StoreError("credential_store_unavailable") from None
        metadata = getattr(record, "metadata", None)
        if (
            getattr(metadata, "name", None) != name
            or getattr(metadata, "namespace", None) != self.namespace
        ):
            raise StoreError("credential_owner_mismatch")
        _verify_labels(getattr(metadata, "labels", None), self.scope.labels(slot, role))
        data = getattr(record, "data", None)
        if (
            getattr(record, "type", None) != "Opaque"
            or getattr(record, "immutable", None) is not True
            or not isinstance(data, dict)
            or set(data) != {SECRET_FIELD}
            or not isinstance(data[SECRET_FIELD], str)
        ):
            raise StoreError("invalid_credential_record")
        try:
            value = base64.b64decode(data[SECRET_FIELD], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError, ValueError):
            raise StoreError("invalid_credential_record") from None
        return _credential_value(value, role)

    def _create(self, slot: str, role: str, value: CredentialValue) -> bool:
        body = V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=V1ObjectMeta(
                name=self.scope.secret_name(slot, role),
                namespace=self.namespace,
                labels=self.scope.labels(slot, role),
            ),
            type="Opaque",
            immutable=True,
            data={SECRET_FIELD: base64.b64encode(value.value.encode("utf-8")).decode("ascii")},
        )
        if self._singleton_guard is not None:
            self._singleton_guard()
        try:
            self._client.create_namespaced_secret(self.namespace, body, _request_timeout=(5, 15))
        except ApiException as error:
            if error.status == 409:
                return False
            raise _request_error(error.status) from None
        except HTTPError:
            raise StoreError("credential_store_unavailable") from None
        return True


def azure_key_vault_store(
    scope: CredentialScope,
    vault_url: str,
    *,
    credential: Any = None,
    singleton_writer: bool = False,
    singleton_guard: Callable[[], None] | None = None,
) -> AzureKeyVaultCredentialStore:
    """Azure-only factory. The caller supplies the live-discovered vault URL.

    SDK packages must be installed in the invoking privileged interpreter, not
    merely in a separate Azure CLI environment. Local use never calls this factory.
    """
    if (
        scope.environment != "azure"
        or not isinstance(vault_url, str)
        or not re.fullmatch(r"https://[a-z][a-z0-9-]{1,22}[a-z0-9]\.vault\.azure\.net/?", vault_url)
    ):
        raise StoreError("invalid_credential_scope")
    try:
        from azure.core.exceptions import AzureError
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError:
        raise StoreError("credential_store_dependency_missing") from None
    try:
        client = SecretClient(
            vault_url=vault_url,
            credential=credential if credential is not None else DefaultAzureCredential(),
            logging_enable=False,
            retry_total=0,
            connection_timeout=5,
            read_timeout=15,
        )
    except AzureError as error:
        raise _request_error(getattr(error, "status_code", None)) from None
    return AzureKeyVaultCredentialStore(
        scope,
        client,
        request_errors=(AzureError,),
        singleton_writer=singleton_writer,
        singleton_guard=singleton_guard,
    )
