from unittest.mock import Mock
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from plane_demo.acme_responder import create_app
from plane_demo.kube import ConfigMaps, ConfigurationInvalid
from plane_demo.models import AppliedConfiguration
from plane_demo.settings import Settings, redis_client


def record(desired):
    return client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name="tenant-alpha",
            resource_version="10",
            labels={
                "plane-demo/project": "test",
                "plane-demo/pair": "shared",
                "plane-demo/onboarding": str(desired.onboarding_id),
            },
        ),
        data={
            "version": str(desired.version),
            "message": desired.message,
            "onboarding_id": str(desired.onboarding_id),
        },
    )


def test_configmap_real_client_calls_are_scoped_and_verified():
    settings = Settings(project_id="test", pair_id="shared", namespace="data")
    api = Mock()
    desired = AppliedConfiguration(
        tenant_id="alpha", onboarding_id=uuid4(), message="v1", version=1
    )
    api.read_namespaced_config_map.side_effect = ApiException(status=404)
    api.create_namespaced_config_map.return_value = record(desired)
    assert ConfigMaps(settings, api).apply(desired) == desired
    kwargs = api.create_namespaced_config_map.call_args.kwargs
    assert kwargs["namespace"] == "data"
    assert kwargs["_request_timeout"] == (5, 5)
    assert kwargs["body"].metadata.name == "tenant-alpha"
    api.create_namespaced_config_map.return_value.data["version"] = "2"
    with pytest.raises(ConfigurationInvalid):
        ConfigMaps(settings, api).apply(desired)


def test_configmap_update_uses_resource_version_and_cannot_regress():
    settings = Settings(project_id="test", pair_id="shared", namespace="data")
    api = Mock()
    old = AppliedConfiguration(tenant_id="alpha", onboarding_id=uuid4(), message="old", version=1)
    new = old.model_copy(update={"message": "new", "version": 2})
    api.read_namespaced_config_map.return_value = record(old)
    api.patch_namespaced_config_map.return_value = record(new)
    maps = ConfigMaps(settings, api)
    assert maps.apply(new) == new
    assert (
        api.patch_namespaced_config_map.call_args.kwargs["body"].metadata.resource_version == "10"
    )
    api.read_namespaced_config_map.return_value = record(new)
    assert maps.apply(old) == new
    assert api.patch_namespaced_config_map.call_count == 1


def test_same_version_changed_content_is_rejected():
    desired = AppliedConfiguration(
        tenant_id="alpha", onboarding_id=uuid4(), message="valid", version=1
    )
    api = Mock()
    api.read_namespaced_config_map.return_value = record(desired)
    maps = ConfigMaps(Settings(project_id="test", pair_id="shared", namespace="data"), api)
    with pytest.raises(ConfigurationInvalid):
        maps.apply(desired.model_copy(update={"message": "different"}))


def test_standalone_radius_password_is_decoded_exactly_once(monkeypatch):
    password = "a/b+c=:%25@?#"
    for name, value in {
        "CONNECTION_REDIS_HOST": "redis.example",
        "CONNECTION_REDIS_PORT": "10000",
        "CONNECTION_REDIS_TLS": "true",
        "CONNECTION_REDIS_PASSWORD": quote(password, safe=""),
    }.items():
        monkeypatch.setenv(name, value)
    store = redis_client(Settings())
    options = store.connection_pool.connection_kwargs
    assert options["password"] == password
    assert store.connection_pool.connection_class.__name__ == "SSLConnection"
    assert "password" not in repr(Settings(redis_url=f"redis://:{password}@redis"))


def test_settings_fail_closed_and_data_does_not_read_parent_env(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "a" * 32)
    monkeypatch.setenv("PAIR_ID", "shared")
    monkeypatch.setenv("PROJECT_ID", "test")
    monkeypatch.setenv("KUBE_NAMESPACE", "data")
    monkeypatch.delenv("MANAGEMENT_DSN", raising=False)
    monkeypatch.delenv("CONTROL_DSN", raising=False)
    assert not Settings.from_env("data_api").control_dsn
    with pytest.raises(ValueError, match="MANAGEMENT_DSN"):
        Settings.from_env("management_api")


def test_challenge_responder_serves_only_exact_token_paths(tmp_path):
    token = "public_token"
    (tmp_path / token).write_text("public_token.thumbprint")
    client_api = TestClient(create_app(Settings(challenge_directory=str(tmp_path))))
    response = client_api.get(f"/.well-known/acme-challenge/{token}")
    assert response.status_code == 200
    assert response.text == "public_token.thumbprint"
    for path in [
        "/",
        "/healthz",
        "/tenants/alpha",
        "/docs",
        "/.well-known/acme-challenge/missing",
        "/.well-known/acme-challenge/a.b",
        f"/.well-known/acme-challenge/{token}/other",
    ]:
        assert client_api.get(path).status_code == 404
