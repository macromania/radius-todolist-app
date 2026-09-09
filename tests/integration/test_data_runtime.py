import os
from dataclasses import replace
from unittest.mock import Mock
from urllib.parse import unquote, urlparse
from uuid import uuid4

import psycopg
import pytest
import redis
from fastapi.testclient import TestClient

from plane_demo.control.reconciler import run_once as control_poll
from plane_demo.data import reconciler as data_reconciler
from plane_demo.data.api import create_app
from plane_demo.shared.kube import ConfigMaps
from plane_demo.shared.models import AppliedConfiguration
from plane_demo.shared.settings import Settings, redis_client

pytestmark = pytest.mark.integration
HEADERS = {"X-Demo-Key": "integration-test-key-not-a-deployed-credential"}


@pytest.fixture
def real_redis():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("requires TEST_REDIS_URL for a disposable Redis")
    client = redis.Redis.from_url(
        url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5
    )
    assert client.ping()
    yield client
    client.close()


def test_real_redis_special_character_password(real_redis, monkeypatch):
    parts = urlparse(os.environ["TEST_REDIS_URL"])
    password = unquote(parts.password or "")
    assert any(character in password for character in "/+:%"), (
        "test Redis must have a password containing URL-special characters"
    )
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("CONNECTION_REDIS_URL", raising=False)
    monkeypatch.setenv("CONNECTION_REDIS_HOST", parts.hostname)
    monkeypatch.setenv("CONNECTION_REDIS_PORT", str(parts.port or 6379))
    monkeypatch.setenv("CONNECTION_REDIS_TLS", str(parts.scheme == "rediss").lower())
    monkeypatch.setenv("CONNECTION_REDIS_PASSWORD", parts.password)
    standalone = redis_client(Settings())
    try:
        assert standalone.ping()
        url_client = redis_client(Settings(redis_url=os.environ["TEST_REDIS_URL"]))
        try:
            assert url_client.ping()
        finally:
            url_client.close()
    finally:
        standalone.close()


def test_repeated_poll_does_not_duplicate_event(databases, control_client):
    databases.create()
    databases.finish("alpha")
    control_poll(databases.settings("control_reconciler"))
    maps = Mock()
    maps.apply.side_effect = lambda desired: desired
    settings = databases.settings("data_reconciler")
    for _ in range(3):
        assert data_reconciler.run_once(settings, config_maps=maps).succeeded == 1
    status = control_client.get("/tenants/alpha").json()
    assert status["data_config"]["status"] == "applied"
    assert [event["type"] for event in status["timeline"]] == [
        "configuration_created",
        "config_applied",
    ]
    control_client.put("/tenants/alpha/configuration", json={"message": "v2"})
    control_client.put("/tenants/alpha/configuration", json={"message": "v3"})
    assert data_reconciler.run_once(settings, config_maps=maps).succeeded == 1
    assert maps.apply.call_args.args[0].version == 3
    with psycopg.connect(databases.control_admin) as connection:
        assert connection.execute(
            "SELECT version FROM control.events WHERE type='config_applied' ORDER BY version"
        ).fetchall() == [(1,), (3,)]


def test_configmap_result_must_match_before_success_report(databases, control_client):
    databases.create()
    databases.finish("alpha")
    control_poll(databases.settings("control_reconciler"))
    maps = Mock()
    maps.apply.side_effect = lambda desired: desired.model_copy(update={"message": "not applied"})
    result = data_reconciler.run_once(databases.settings("data_reconciler"), config_maps=maps)
    assert result.failed == 1
    status = control_client.get("/tenants/alpha").json()
    assert status["data_config"]["status"] == "failed"
    assert status["data_config"]["last_applied_version"] is None


def test_report_outage_keeps_applied_configuration(databases, monkeypatch):
    databases.create()
    databases.finish("alpha")
    control_poll(databases.settings("control_reconciler"))
    maps = Mock()
    applied = []

    def apply(desired):
        applied.append(desired)
        return desired

    maps.apply.side_effect = apply
    monkeypatch.setattr(
        data_reconciler,
        "report",
        Mock(side_effect=psycopg.OperationalError("secret-must-not-be-logged")),
    )
    result = data_reconciler.run_once(databases.settings("data_reconciler"), config_maps=maps)
    assert result.failed == 1
    assert applied[0].message == "alpha-initial"


def test_real_redis_api_counter_is_atomic_and_scoped(real_redis):
    from concurrent.futures import ThreadPoolExecutor

    configurations = {
        tenant: AppliedConfiguration(
            tenant_id=tenant, onboarding_id=uuid4(), message=tenant, version=1
        )
        for tenant in ("alpha", "beta")
    }
    maps = Mock()
    maps.read.side_effect = configurations.__getitem__
    settings = Settings(demo_key=HEADERS["X-Demo-Key"])
    keys = [
        f"plane-demo:{value.onboarding_id}:{value.tenant_id}:counter"
        for value in configurations.values()
    ]
    try:
        with TestClient(
            create_app(settings, config_maps=maps, counter_store=real_redis), headers=HEADERS
        ) as client:

            def increment(_index):
                response = client.post("/tenants/alpha/counter")
                assert response.status_code == 200
                return response.json()["counter"]

            with ThreadPoolExecutor(max_workers=8) as executor:
                values = list(executor.map(increment, range(24)))
            assert sorted(values) == list(range(1, 25))
            assert client.get("/tenants/beta").json()["counter"] == 0
        # Reconstructing the real API does not reconstruct counter state in memory.
        with TestClient(
            create_app(settings, config_maps=maps, counter_store=real_redis), headers=HEADERS
        ) as restarted:
            assert restarted.get("/tenants/alpha").json()["counter"] == 24
    finally:
        real_redis.delete(*keys)


def test_real_kubernetes_configmap_reconciliation(databases, real_redis):
    from kubernetes import client, config

    kubeconfig = os.environ.get("TEST_KUBECONFIG")
    context = os.environ.get("TEST_KUBE_CONTEXT")
    namespace = os.environ.get("TEST_KUBE_NAMESPACE")
    if not all((kubeconfig, context, namespace)):
        pytest.skip("requires explicit TEST_KUBECONFIG, TEST_KUBE_CONTEXT, TEST_KUBE_NAMESPACE")
    api_client = config.new_client_from_config(config_file=kubeconfig, context=context)
    core = client.CoreV1Api(api_client)
    settings = replace(databases.settings("data_reconciler"), namespace=namespace)
    tenant = "test-" + uuid4().hex[:12]
    databases.create(tenant)
    databases.finish(tenant)
    control_poll(databases.settings("control_reconciler"))
    maps = ConfigMaps(settings, core)
    key = None
    try:
        assert data_reconciler.run_once(settings, config_maps=maps).succeeded == 1
        actual = maps.read(tenant)
        assert actual.version == 1
        assert actual.message == tenant + "-initial"
        key = f"plane-demo:{actual.onboarding_id}:{tenant}:counter"
        with TestClient(
            create_app(settings, config_maps=ConfigMaps(settings, core), counter_store=real_redis),
            headers=HEADERS,
        ) as data:
            assert data.post(f"/tenants/{tenant}/counter").json()["counter"] == 1
    finally:
        core.delete_namespaced_config_map(f"tenant-{tenant}", namespace, _request_timeout=(5, 5))
        if key:
            real_redis.delete(key)
        api_client.close()
