import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_acceptance
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

PROBE = test_acceptance.runner_module.DATA_API_PERMISSIONS_PROBE
EXPECTED = test_acceptance.runner_module.data_api_permissions(
    ["shared-a", "shared-b", "isolated-c"]
)


@pytest.mark.parametrize(
    "unexpected",
    [
        None,
        ("secrets", "get"),
        ("secrets", "list"),
        ("pods", "create"),
        ("serviceaccounts/token", "create"),
        ("configmaps", "patch"),
        ("configmaps", "watch"),
        ("serviceaccounts/token", "create", "data-api"),
        ("configmaps", "patch", "tenant-shared-a"),
        ("secrets", "watch", "data-reconciler-runtime"),
    ],
)
def test_actual_probe_rejects_permissions_outside_reading_configmaps(
    monkeypatch, capsys, unexpected
):
    namespace = "radplanes-local-shared-data-data"
    requests = []

    def review(*, body):
        attributes = body["spec"]["resourceAttributes"]
        assert attributes["namespace"] == namespace
        resource = attributes["resource"]
        if attributes.get("subresource"):
            resource += "/" + attributes["subresource"]
        key = (resource, attributes["verb"])
        if attributes.get("name"):
            key += (attributes["name"],)
        requests.append(key)
        return SimpleNamespace(
            status=SimpleNamespace(
                allowed=key == ("configmaps", "get") or key == unexpected,
            )
        )

    authorization = SimpleNamespace(create_self_subject_access_review=review)
    core = SimpleNamespace(
        read_namespaced_secret=Mock(side_effect=ApiException(status=403)),
        list_namespaced_secret=Mock(side_effect=ApiException(status=403)),
    )
    monkeypatch.setattr(config, "load_incluster_config", Mock())
    monkeypatch.setattr(client, "AuthorizationV1Api", lambda: authorization)
    monkeypatch.setattr(client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(sys, "argv", ["probe", namespace, json.dumps(EXPECTED)])
    if unexpected:
        with pytest.raises(RuntimeError, match="permissions_exceed_contract"):
            exec(compile(PROBE, "<permissions-probe>", "exec"), {})
        assert capsys.readouterr().out == ""
    else:
        exec(compile(PROBE, "<permissions-probe>", "exec"), {})
        result = json.loads(capsys.readouterr().out)
        assert len(requests) == len(EXPECTED)
        assert result["permissions"]["configmaps:get"] is True
        assert result["parent_secret_get_status"] == result["secret_list_status"] == 403
        core.read_namespaced_secret.assert_called_once_with("data-reconciler-runtime", namespace)
        core.list_namespaced_secret.assert_called_once_with(namespace, limit=1)


@pytest.mark.parametrize("status", [200, 401, 404])
def test_actual_secret_request_must_be_authenticated_forbidden(monkeypatch, status):
    def review(*, body):
        attrs = body["spec"]["resourceAttributes"]
        return SimpleNamespace(
            status=SimpleNamespace(
                allowed=attrs["resource"] == "configmaps" and attrs["verb"] == "get",
            )
        )

    core = Mock()
    if status != 200:
        core.read_namespaced_secret.side_effect = ApiException(status=status)
    monkeypatch.setattr(config, "load_incluster_config", Mock())
    monkeypatch.setattr(
        client,
        "AuthorizationV1Api",
        lambda: SimpleNamespace(
            create_self_subject_access_review=review,
        ),
    )
    monkeypatch.setattr(client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "radplanes-local-shared-data-data", json.dumps(EXPECTED)],
    )
    with pytest.raises(RuntimeError, match="data_api_"):
        exec(compile(PROBE, "<permissions-probe>", "exec"), {})
