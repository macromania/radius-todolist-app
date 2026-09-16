import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.operations.azure import prerequisites as subject  # noqa: E402

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def confirm(monkeypatch):
    monkeypatch.setenv("CONFIRM_AZURE", "yes")


class Azure:
    def __init__(self, *, provider="Registered", feature="Registered"):
        self.provider, self.feature = provider, feature
        self.calls = []
        self.now = 0
        self.failed = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert argv[-4:] == ["--subscription", SUBSCRIPTION, "--output", "json"]
        assert kwargs["timeout"] == 180
        group, action = argv[1:3]
        if action == "register":
            if group == "provider":
                self.provider = "Registering"
            elif self.feature == "NotRegistered":
                self.feature = "Registered"
        value = (
            {"registrationState": self.provider}
            if group == "provider"
            else {"properties": {"state": self.feature}}
        )
        return subprocess.CompletedProcess(argv, 17 if self.failed else 0, json.dumps(value))

    def sleep(self, seconds):
        self.now += seconds

    def client(self):
        return subject.Registrations(
            SUBSCRIPTION, runner=self, clock=lambda: self.now, sleep=self.sleep
        )


def test_prepare_registers_only_required_providers_and_never_byoip(monkeypatch):
    monkeypatch.setenv("CONFIRM_AZURE", "yes")
    fake = Azure(provider="NotRegistered")
    result = fake.client().prepare()
    assert set(result["providers"]) == set(subject.PROVIDERS)
    assert result["features"] == []
    assert not any(call[1] == "feature" for call in fake.calls)
    assert ["provider", "register"] == fake.calls[1][1:3]
    before = len(fake.calls)
    fake.client().prepare()
    assert all(call[2] == "show" for call in fake.calls[before:])


def test_confirmation_precedes_all_azure_calls(monkeypatch):
    monkeypatch.delenv("CONFIRM_AZURE", raising=False)
    fake = Azure()
    with pytest.raises(subject.PrerequisiteError, match="CONFIRM_AZURE"):
        fake.client().prepare()
    assert fake.calls == []


def test_feature_registration_waits_then_refreshes_provider():
    fake = Azure(feature="NotRegistered")
    fake.client().feature("Microsoft.Network", "SyntheticTestFeature")
    actions = [call[1:3] for call in fake.calls]
    assert actions.index(["feature", "register"]) < actions.index(["provider", "register"])
    assert fake.now == 15


@pytest.mark.parametrize("state", ["Pending", None, "Unknown", "Unregistering"])
def test_unavailable_or_approval_pending_feature_stops_without_mutation(state):
    fake = Azure(feature=state)
    with pytest.raises(subject.PrerequisiteError, match="Pending|unexpected"):
        fake.client().feature("Microsoft.Network", "SyntheticTestFeature")
    assert all(call[2] == "show" for call in fake.calls)


def test_feature_timeout_is_bounded():
    fake = Azure(feature="Registering")
    with pytest.raises(subject.PrerequisiteError, match="timed out"):
        fake.client().feature("Microsoft.Network", "SyntheticTestFeature")
    assert fake.now == 900


def test_denied_provider_query_is_not_treated_as_unregistered():
    fake = Azure()
    fake.failed = True
    with pytest.raises(subject.PrerequisiteError, match="exit 17"):
        fake.client().provider("Microsoft.Network")
    assert len(fake.calls) == 1


@pytest.mark.parametrize("payload", ["not json", "null", "[]", "{}"])
def test_malformed_response_is_not_success(payload):
    client = subject.Registrations(
        SUBSCRIPTION, runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, payload)
    )
    with pytest.raises(subject.PrerequisiteError):
        client.provider("Microsoft.Network")
