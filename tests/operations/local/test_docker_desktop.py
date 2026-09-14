import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import docker_desktop
import pytest

RESOLVE = docker_desktop.docker_host


@pytest.fixture(autouse=True)
def clear_endpoint_cache():
    RESOLVE.cache_clear()
    yield
    RESOLVE.cache_clear()


@pytest.mark.parametrize(
    "endpoint",
    [
        "unix:///Users/operator/.docker/run/docker.sock",
        "unix:///home/operator/.docker/desktop/docker.sock",
    ],
)
def test_desktop_context_resolves_operator_socket_once(monkeypatch, endpoint):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(endpoint), ""))
    monkeypatch.setattr(docker_desktop.subprocess, "run", run)
    monkeypatch.setattr(Path, "home", lambda: Path("/operator-home"))
    monkeypatch.setenv("DOCKER_HOST", "tcp://unrelated:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "colima")
    monkeypatch.setenv("DOCKER_CONFIG", "/unrelated/config")
    assert RESOLVE() == RESOLVE() == endpoint
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0] == [
        "docker",
        "context",
        "inspect",
        "desktop-linux",
        "--format",
        "{{json .Endpoints.docker.Host}}",
    ]
    assert kwargs["env"]["HOME"] == "/operator-home"
    assert not {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"} & kwargs["env"].keys()
    assert kwargs["timeout"] == 15


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp://127.0.0.1:2375",
        "ssh://host",
        "npipe:////./pipe/docker_engine",
        "unix://remote/docker.sock",
        "unix:///tmp/../docker.sock",
        "unix:///tmp/docker.sock?query=1",
        "unix:///tmp/docker.sock#fragment",
        "unix:///tmp/docker.sock\n",
        "unix://[",
        None,
        {},
    ],
)
def test_remote_or_malformed_context_has_no_fallback(monkeypatch, endpoint):
    monkeypatch.setattr(
        docker_desktop.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess([], 0, json.dumps(endpoint), ""),
        ),
    )
    with pytest.raises(OSError, match="invalid endpoint|local Unix socket"):
        RESOLVE()


@pytest.mark.parametrize("failure", ["missing", "timeout", "invalid-json", "nonzero"])
def test_lookup_errors_are_explicit_and_do_not_disclose_output(monkeypatch, failure):
    result = subprocess.CompletedProcess(
        [],
        1 if failure == "nonzero" else 0,
        "untrusted-secret",
        "untrusted-secret",
    )
    run = Mock(return_value=result)
    if failure == "missing":
        run.side_effect = FileNotFoundError("untrusted-secret")
    elif failure == "timeout":
        run.side_effect = subprocess.TimeoutExpired("docker", 15, stderr="untrusted-secret")
    monkeypatch.setattr(docker_desktop.subprocess, "run", run)
    with pytest.raises(OSError) as error:
        RESOLVE()
    assert "untrusted-secret" not in str(error.value)
