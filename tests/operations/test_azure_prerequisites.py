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
        self.providers = {}
        self.calls = []
        self.now = 0
        self.failed = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        group, action = argv[1:3]
        output = "none" if action == "register" else "json"
        assert argv[-4:] == ["--subscription", SUBSCRIPTION, "--output", output]
        assert kwargs == {
            "stdout": subprocess.PIPE,
            "text": True,
            "check": False,
            "timeout": 180,
        }
        if self.failed:
            return subprocess.CompletedProcess(argv, 17, "")
        namespace = argv[argv.index("--namespace") + 1]
        if action == "register":
            if group == "provider":
                self.providers[namespace] = "Registering"
                print(
                    "WARNING: Registering is still on-going. You can monitor using "
                    f"'az provider show -n {namespace}'",
                    file=sys.stderr,
                )
            elif self.feature == "NotRegistered":
                self.feature = "Registered"
            return subprocess.CompletedProcess(argv, 0, "")
        value = (
            {"registrationState": self.providers.get(namespace, self.provider)}
            if group == "provider"
            else {"properties": {"state": self.feature}}
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(value))

    def sleep(self, seconds):
        self.now += seconds

    def client(self):
        return subject.Registrations(
            SUBSCRIPTION, runner=self, clock=lambda: self.now, sleep=self.sleep
        )


def test_prepare_registers_only_required_providers_and_never_byoip(capsys):
    fake = Azure(provider="NotRegistered")
    result = fake.client().prepare()
    assert result == {
        "subscriptionId": SUBSCRIPTION,
        "providers": dict.fromkeys(subject.PROVIDERS, "Registering"),
        "features": [],
    }
    assert [call[1:5] for call in fake.calls] == [
        ["provider", action, "--namespace", namespace]
        for namespace in subject.PROVIDERS
        for action in ("show", "register", "show")
    ]
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.count("WARNING: Registering is still on-going.") == len(subject.PROVIDERS)
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
    assert actions == [
        ["feature", "show"],
        ["feature", "register"],
        ["feature", "show"],
        ["provider", "show"],
        ["provider", "register"],
        ["provider", "show"],
    ]
    assert fake.now == 15


@pytest.mark.parametrize("group", ["provider", "feature"])
def test_direct_registration_requires_confirmation(monkeypatch, group):
    monkeypatch.delenv("CONFIRM_AZURE", raising=False)
    fake = Azure(provider="NotRegistered", feature="NotRegistered")
    client = fake.client()
    with pytest.raises(subject.PrerequisiteError, match="CONFIRM_AZURE"):
        if group == "provider":
            client.provider("Microsoft.Network")
        else:
            client.feature("Microsoft.Network", "SyntheticTestFeature")
    assert [call[1:3] for call in fake.calls] == [[group, "show"]]


@pytest.mark.parametrize("group", ["provider", "feature"])
def test_registration_failure_stops_before_followup_query(group):
    fake = Azure(provider="NotRegistered", feature="NotRegistered")

    def runner(argv, **kwargs):
        fake.failed = argv[2] == "register"
        return fake(argv, **kwargs)

    client = subject.Registrations(SUBSCRIPTION, runner=runner)
    with pytest.raises(subject.PrerequisiteError, match=f"{group} register failed \\(exit 17\\)"):
        if group == "provider":
            client.provider("Microsoft.Network")
        else:
            client.feature("Microsoft.Network", "SyntheticTestFeature")
    assert [call[1:3] for call in fake.calls] == [[group, "show"], [group, "register"]]


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


@pytest.mark.parametrize("payload", ["", " \n", "not json", "null", "[]", "{}"])
@pytest.mark.parametrize("group", ["provider", "feature"])
def test_malformed_response_is_not_success(payload, group):
    client = subject.Registrations(
        SUBSCRIPTION, runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, payload)
    )
    with pytest.raises(subject.PrerequisiteError):
        if group == "provider":
            client.provider("Microsoft.Network")
        else:
            client.feature("Microsoft.Network", "SyntheticTestFeature")
