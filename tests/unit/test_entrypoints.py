from unittest.mock import Mock

import pytest

from plane_demo.control import api as control_api
from plane_demo.control import reconciler as control_reconciler
from plane_demo.data import api as data_api
from plane_demo.data import reconciler as data_reconciler
from plane_demo.management import api as management_api
from plane_demo.setup import acme_responder
from plane_demo.shared.models import ReconcileResult
from plane_demo.shared.settings import Settings


class StopLoop(Exception):
    pass


@pytest.mark.parametrize("module", [control_reconciler, data_reconciler])
def test_real_reconciler_main_invokes_run_once(module, monkeypatch):
    settings = Settings()
    run = Mock(return_value=ReconcileResult())
    monkeypatch.setattr(module.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(module, "run_once", run)
    monkeypatch.setattr(module.time, "sleep", Mock(side_effect=StopLoop))
    if module is data_reconciler:
        monkeypatch.setattr(module, "ConfigMaps", Mock())
    with pytest.raises(StopLoop):
        module.main()
    assert run.call_args.args == (settings,)


@pytest.mark.parametrize("module", [management_api, control_api, data_api, acme_responder])
def test_real_api_main_uses_app_factory(module, monkeypatch):
    import uvicorn

    settings = Settings(demo_key="test-key-that-is-long-enough-12345", listen_port=35519)
    factory = Mock()
    runner = Mock()
    monkeypatch.setattr(module.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(module, "create_app", factory)
    monkeypatch.setattr(uvicorn, "run", runner)
    module.main()
    factory.assert_called_once_with(settings)
    assert runner.call_args.args == (factory.return_value,)
    assert runner.call_args.kwargs["port"] == 35519
