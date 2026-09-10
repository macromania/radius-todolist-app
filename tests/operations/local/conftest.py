import pytest
from local_support import bootstrap, common, gate, images, prepare


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    state = tmp_path / "local"
    for module in (common, bootstrap, images, prepare, gate):
        monkeypatch.setattr(module, "STATE", state)
    return state
