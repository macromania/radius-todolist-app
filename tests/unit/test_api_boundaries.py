from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException
from redis.exceptions import ConnectionError

from plane_demo import control_api, data_api, management_api
from plane_demo.kube import ConfigurationInvalid, ConfigurationMissing
from plane_demo.models import AppliedConfiguration
from plane_demo.settings import Settings

KEY = "unit-test-demo-key-with-32-characters"
HEADERS = {"X-Demo-Key": KEY}


def data_client(*, maps=None, counters=None):
    maps = maps if maps is not None else Mock()
    counters = counters if counters is not None else Mock()
    return TestClient(
        data_api.create_app(Settings(demo_key=KEY), config_maps=maps, counter_store=counters)
    )


@pytest.mark.parametrize("factory", [management_api.create_app, control_api.create_app])
def test_parent_api_authentication_and_minimal_health(factory):
    client = TestClient(factory(Settings(demo_key=KEY)))
    assert client.get("/livez").json() == {"status": "ok"}
    assert client.get("/healthz").status_code == 200
    assert client.get("/tenants/alpha").status_code == 401
    assert client.get("/tenants/alpha", headers={"X-Demo-Key": "wrong"}).status_code == 401
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/api/container-info").status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"tenant_id": "../oops", "isolation": "shared", "initial_message": ""},
        {"tenant_id": "a" * 33, "isolation": "shared", "initial_message": ""},
        {"tenant_id": "alpha", "isolation": "unknown", "initial_message": ""},
        {"tenant_id": "alpha", "isolation": "shared", "initial_message": "x" * 1025},
        {"tenant_id": "alpha", "isolation": "shared", "initial_message": "\x00"},
        {"tenant_id": "alpha", "isolation": "shared", "initial_message": "", "command": "bad"},
    ],
)
def test_invalid_request_never_connects_to_database(payload, monkeypatch):
    connection = Mock(side_effect=AssertionError("database must not be accessed"))
    monkeypatch.setattr(management_api, "connect", connection)
    client = TestClient(management_api.create_app(Settings(demo_key=KEY)))
    assert client.post("/tenants", json=payload, headers=HEADERS).status_code == 422
    connection.assert_not_called()


def test_actual_body_is_bounded_without_content_length():
    client = TestClient(management_api.create_app(Settings(demo_key=KEY)))
    response = client.post("/tenants", content=iter([b"a" * 5000, b"b" * 5000]), headers=HEADERS)
    assert response.status_code == 413


def test_invalid_unicode_is_rejected_without_reflecting_unserializable_input():
    client = TestClient(management_api.create_app(Settings(demo_key=KEY)))
    response = client.post(
        "/tenants",
        content=b'{"tenant_id":"alpha","isolation":"shared","initial_message":"\\ud800"}',
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}


def test_data_request_has_no_parent_dependency(monkeypatch):
    monkeypatch.delenv("MANAGEMENT_DSN", raising=False)
    monkeypatch.delenv("CONTROL_DSN", raising=False)
    applied = AppliedConfiguration(
        tenant_id="alpha", onboarding_id=uuid4(), message="last applied", version=7
    )
    maps, counters = Mock(), Mock()
    maps.read.return_value = applied
    counters.get.return_value = "12"
    counters.incr.return_value = 13
    client = data_client(maps=maps, counters=counters)
    assert client.get("/healthz").json() == {"status": "ok"}
    maps.read.assert_not_called()
    assert client.get("/tenants/alpha").status_code == 401
    assert client.get("/tenants/alpha", headers=HEADERS).json() == {
        "tenant_id": "alpha",
        "onboarding_id": str(applied.onboarding_id),
        "message": "last applied",
        "applied_version": 7,
        "counter": 12,
    }
    assert client.post("/tenants/alpha/counter", headers=HEADERS).json()["counter"] == 13
    counters.incr.assert_called_once_with(f"plane-demo:{applied.onboarding_id}:alpha:counter")


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (ConfigurationMissing(), 404, "tenant_config_not_applied"),
        (ConfigurationInvalid(), 503, "tenant_config_invalid"),
        (ApiException(status=500), 503, "local_configuration_unavailable"),
    ],
)
def test_data_config_errors_do_not_increment(error, status, detail):
    maps, counters = Mock(), Mock()
    maps.read.side_effect = error
    response = data_client(maps=maps, counters=counters).post(
        "/tenants/alpha/counter", headers=HEADERS
    )
    assert response.status_code == status
    assert response.json()["detail"] == detail
    counters.incr.assert_not_called()


def test_redis_failure_does_not_fall_back_or_leak(caplog):
    maps, counters = Mock(), Mock()
    maps.read.return_value = AppliedConfiguration(
        tenant_id="alpha", onboarding_id=uuid4(), message="m", version=1
    )
    counters.get.side_effect = ConnectionError("redis://password-secret@host")
    response = data_client(maps=maps, counters=counters).get("/tenants/alpha", headers=HEADERS)
    assert response.status_code == 503
    assert "password-secret" not in response.text + caplog.text
