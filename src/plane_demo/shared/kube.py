import re

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from plane_demo.shared.models import AppliedConfiguration
from plane_demo.shared.settings import Settings


class ConfigurationMissing(Exception):
    pass


class ConfigurationInvalid(Exception):
    pass


def core_client() -> client.CoreV1Api:
    config.load_incluster_config()
    return client.CoreV1Api()


class ConfigMaps:
    def __init__(self, settings: Settings, api=None):
        self.settings = settings
        self.api = api if api is not None else core_client()

    def _read(self, tenant_id: str):
        try:
            return self.api.read_namespaced_config_map(
                f"tenant-{tenant_id}",
                self.settings.namespace,
                _request_timeout=(self.settings.timeout_seconds, self.settings.timeout_seconds),
            )
        except ApiException as error:
            if error.status == 404:
                raise ConfigurationMissing from None
            raise

    def _parse(self, tenant_id: str, record) -> AppliedConfiguration:
        try:
            labels = record.metadata.labels or {}
            data = record.data or {}
            if not re.fullmatch(r"[1-9][0-9]{0,18}", data["version"]):
                raise ValueError
            applied = AppliedConfiguration(
                tenant_id=tenant_id,
                onboarding_id=data["onboarding_id"],
                message=data["message"],
                version=int(data["version"]),
            )
            expected_labels = {
                "plane-demo/project": self.settings.project_id,
                "plane-demo/pair": self.settings.pair_id,
                "plane-demo/onboarding": str(applied.onboarding_id),
            }
            if any(labels.get(key) != value for key, value in expected_labels.items()):
                raise ValueError
            return applied
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ConfigurationInvalid from None

    def read(self, tenant_id: str) -> AppliedConfiguration:
        return self._parse(tenant_id, self._read(tenant_id))

    def apply(self, desired: AppliedConfiguration) -> AppliedConfiguration:
        existing = None
        try:
            existing = self._read(desired.tenant_id)
            current = self._parse(desired.tenant_id, existing)
            if current.onboarding_id != desired.onboarding_id:
                raise ConfigurationInvalid
            if current.version > desired.version:
                return current
            if current.version == desired.version:
                if current != desired:
                    raise ConfigurationInvalid
                return current
        except ConfigurationMissing:
            pass
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=f"tenant-{desired.tenant_id}",
                namespace=self.settings.namespace,
                resource_version=existing.metadata.resource_version if existing else None,
                labels={
                    "plane-demo/project": self.settings.project_id,
                    "plane-demo/pair": self.settings.pair_id,
                    "plane-demo/onboarding": str(desired.onboarding_id),
                },
            ),
            data={
                "message": desired.message,
                "version": str(desired.version),
                "onboarding_id": str(desired.onboarding_id),
            },
        )
        kwargs = {
            "namespace": self.settings.namespace,
            "body": body,
            "_request_timeout": (self.settings.timeout_seconds, self.settings.timeout_seconds),
        }
        if existing:
            response = self.api.patch_namespaced_config_map(name=body.metadata.name, **kwargs)
        else:
            response = self.api.create_namespaced_config_map(**kwargs)
        actual = self._parse(desired.tenant_id, response)
        if actual != desired:
            raise ConfigurationInvalid
        return actual
