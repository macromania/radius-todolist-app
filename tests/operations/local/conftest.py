import importlib

import pytest
from local_support import bootstrap, common, gate, images, prepare


@pytest.fixture(autouse=True)
def isolated_docker_endpoint(monkeypatch):
    monkeypatch.setattr(
        importlib.import_module("docker_desktop"),
        "docker_host",
        lambda: "unix:///test/docker-desktop.sock",
    )
    monkeypatch.setattr(common, "docker_host", lambda: "unix:///test/docker-desktop.sock")


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    state = tmp_path / "local"
    for module in (common, bootstrap, images, prepare, gate):
        monkeypatch.setattr(module, "STATE", state)
    return state
