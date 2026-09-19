import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from plane_demo.management.providers.identity import RESOURCE_SIZING  # noqa: E402
from scripts.operations import config as configuration  # noqa: E402
from scripts.operations.azure import resource_sizes as subject  # noqa: E402

CONFIG = configuration.DemoConfig(
    "azure", "sample", "demo", "11111111-1111-1111-1111-111111111111", "eastus2"
)


def storage():
    return [
        {
            "supportedServerEditions": [
                {
                    "name": "GeneralPurpose",
                    "supportedStorageEditions": [
                        {
                            "name": "ManagedDisk",
                            "supportedStorageMb": [
                                {"storageSizeMb": value * 1024} for value in (32, 64, 128, 256)
                            ],
                        }
                    ],
                }
            ],
        }
    ]


def offers():
    return [
        {
            "armSkuName": "Azure_Managed_Redis_" + name,
            "skuName": name.removeprefix("Balanced_"),
            "productName": "Azure Managed Redis - Balanced",
            "armRegionName": "eastus2",
            "type": "Consumption",
            "unitOfMeasure": "1 Hour",
            "currencyCode": "USD",
            "retailPrice": (index + 1) / 100,
        }
        for index, name in enumerate(subject.REDIS_MEMORY)
    ]


class Runner:
    def __init__(self):
        self.calls = []
        self.bad_region = False
        self.fail = False
        self.items = offers()
        self.next_page = None
        self.storage = storage()
        self.vault_sku = "standard"

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.fail:
            print("native discovery diagnostic", file=sys.stderr)
            return subprocess.CompletedProcess(argv, 7, "")
        if argv[0] == "az":
            assert argv[-4:] == ["--subscription", CONFIG.subscription, "--output", "json"]
        if argv[:3] == ["az", "provider", "show"]:
            namespace = argv[argv.index("--namespace") + 1]
            value = {
                "namespace": namespace,
                "registrationState": "Registered",
                "resourceTypes": [
                    {
                        "resourceType": kind,
                        "locations": ["West Europe"] if self.bad_region else ["East US 2"],
                    }
                    for kind in subject.REGIONAL_TYPES[namespace]
                ],
            }
        elif argv[:3] == ["az", "postgres", "flexible-server"]:
            value = self.storage
        elif argv[:3] == ["az", "keyvault", "show"]:
            value = {"properties": {"sku": {"name": self.vault_sku}}}
        else:
            assert argv[0] == "curl" and argv[-1].startswith("https://prices.azure.com/")
            value = {"Items": self.items, "NextPageLink": self.next_page}
        return subprocess.CompletedProcess(argv, 0, json.dumps(value))


def test_discovery_checks_each_regional_service_and_compatible_redis_offers():
    runner = Runner()
    choices, prices = subject.Discovery(CONFIG, runner=runner).available()
    namespaces = {
        call[call.index("--namespace") + 1]
        for call in runner.calls
        if call[:3] == ["az", "provider", "show"]
    }
    assert namespaces == set(subject.REGIONAL_TYPES)
    assert choices["postgres_storage_gb"] == [32, 64, 128, 256]
    assert set(choices["redis_sku_name"]) == set(subject.REDIS_MEMORY)
    assert prices["Balanced_B0"] > 0
    assert all(
        not any(word in call for word in ("create", "update", "delete")) for call in runner.calls
    )


@pytest.mark.parametrize(
    "bad", ["region", "no-offers", "wrong-family", "price", "pagination", "storage"]
)
def test_incomplete_or_incompatible_discovery_stops_before_selection(bad):
    runner = Runner()
    if bad == "region":
        runner.bad_region = True
    elif bad == "no-offers":
        runner.items = []
    elif bad == "wrong-family":
        for item in runner.items:
            item["productName"] = "Azure Redis Cache Standard"
    elif bad == "price":
        runner.items[0]["retailPrice"] = -1
    elif bad == "pagination":
        runner.next_page = "https://unrelated.invalid/prices"
    else:
        runner.storage = []
    with pytest.raises(subject.SelectionError):
        subject.Discovery(CONFIG, runner=runner).available()


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    selected = replace(CONFIG, demo_keys={"management": "synthetic-private-key-" + "x" * 32})
    path = tmp_path / ".env"
    configuration.initialize_config(selected, path)
    runner = Runner()
    discovery = subject.Discovery(selected, runner=runner)
    monkeypatch.setattr(subject, "Discovery", lambda _: discovery)
    monkeypatch.setenv("CONFIRM_AZURE", "yes")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "argv", ["resource_sizes.py", "--config", str(path)])
    return path, selected, runner


def test_main_saves_all_choices_atomically_and_labels_capacity_uncertainty(
    invocation, monkeypatch, capsys
):
    path, original, runner = invocation
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n" * len(RESOURCE_SIZING)))
    assert subject.main() == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    saved = configuration.load_config(path)
    assert set(result["parameters"]) == {field for _, field, _ in RESOURCE_SIZING.values()}
    assert saved.resource_sizes == result["parameters"]
    assert saved.demo_keys == original.demo_keys
    assert path.stat().st_mode & 0o777 == 0o600
    assert "not live capacity" in output.err
    assert "elapsed" not in output.err and "completed (" not in output.err
    assert original.demo_keys["management"] not in output.out + output.err
    before = path.stat(), path.read_bytes()
    assert subject.main() == 0
    assert (path.stat(), path.read_bytes()) == before
    assert "Select 1-" not in capsys.readouterr().err
    assert len(runner.calls) >= 2


def test_enter_accepts_a_labeled_recommendation_with_units_for_every_resource(
    invocation, monkeypatch, capsys
):
    path, original, _ = invocation
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" * len(RESOURCE_SIZING)))
    assert subject.main() == 0
    saved = configuration.load_config(path)
    for name, (value, _) in subject.RECOMMENDATIONS.items():
        assert getattr(saved, name) == value
    assert saved.demo_keys == original.demo_keys
    output = capsys.readouterr()
    assert output.err.count("(recommended)") == len(RESOURCE_SIZING)
    for row in (
        "1) 2 nodes (recommended)",
        "2) 3 nodes",
        "3) 4 nodes",
        "1) 64 GiB (recommended)",
        "2) 128 GiB",
        "1) 32 GiB (recommended)",
        "1) 1 instance (recommended)",
        "2) 2 instances",
        "3) 3 instances",
        "2) Balanced_B1  1 GB",
        "2) Standard tier (recommended)",
    ):
        assert row in output.err
    assert "Select 1-6 [Enter = 2, recommended; q = cancel]" in output.err
    assert json.loads(output.out)["parameters"] == saved.resource_sizes


def test_recommendation_is_always_an_available_choice(invocation, monkeypatch, capsys):
    path, _, runner = invocation
    runner.storage[0]["supportedServerEditions"][0]["supportedStorageEditions"][0][
        "supportedStorageMb"
    ] = [{"storageSizeMb": 65536}]
    runner.items = [item for item in runner.items if item["skuName"] == "B3"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" * len(RESOURCE_SIZING)))
    assert subject.main() == 0
    assert configuration.load_config(path).postgres_storage_gb == 64
    assert configuration.load_config(path).redis_sku_name == "Balanced_B3"
    assert capsys.readouterr().err.count("usual demo recommendation is unavailable") == 2


def test_recommendations_never_replace_saved_valid_operator_choices(invocation, capsys):
    path, original, _ = invocation
    selected = replace(
        original, **{name: choices[-1] for name, (_, _, choices) in RESOURCE_SIZING.items()}
    )
    configuration.initialize_config(selected, path)
    before = path.read_bytes()
    assert subject.main() == 0
    assert path.read_bytes() == before
    output = capsys.readouterr()
    assert "Select 1-" not in output.err
    assert "retain 4 nodes" in output.err
    assert "retain 256 GiB" in output.err
    assert "retain 3 instances" in output.err


@pytest.mark.parametrize("answer", ["q\n", "", "1\nq\n"])
def test_cancel_never_partially_replaces_configuration(invocation, monkeypatch, answer):
    path, _, _ = invocation
    before = path.read_bytes()
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert subject.main() == 130
    assert path.read_bytes() == before


def test_discovery_failure_preserves_native_error_and_existing_choices(invocation, capsys):
    path, _, runner = invocation
    runner.fail = True
    before = path.read_bytes()
    assert subject.main() == 1
    assert "native discovery diagnostic" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_existing_foundation_cannot_be_resized(invocation, monkeypatch, capsys):
    path, original, _ = invocation
    selected = replace(original, **{name: values[2][0] for name, values in RESOURCE_SIZING.items()})
    configuration.initialize_config(selected, path)
    foundation = {
        "projectName": CONFIG.project,
        "deploymentName": CONFIG.deployment,
        "subscriptionId": CONFIG.subscription,
        "location": CONFIG.location,
        "resourceSizingVersion": 1,
        **selected.resource_sizes,
        "redisSkuName": "Balanced_B1",
    }
    file = path.parent / "foundation.json"
    file.write_text(json.dumps({"foundation": foundation}))
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--existing-foundation", str(file)])
    before = path.read_bytes()
    assert subject.main() == 1
    assert "no automatic resize" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_external_vault_tier_is_observed_not_changed():
    selected = replace(CONFIG, key_vault="shared-vault", key_vault_sku="premium")
    runner = Runner()
    with pytest.raises(subject.SelectionError, match="resizing is not permitted"):
        subject.Discovery(selected, runner=runner).available()
    assert not any("update" in call for call in runner.calls)


def test_configuration_changed_during_resource_selection_is_not_overwritten(
    invocation, monkeypatch, capsys
):
    path, original, _ = invocation
    changed = replace(original, deployment="other")

    class Input:
        def readline(self):
            configuration.initialize_config(changed, path)
            return "1\n"

    monkeypatch.setattr(sys, "stdin", Input())
    assert subject.main() == 1
    assert configuration.load_config(path) == changed
    assert "changed while selecting" in capsys.readouterr().err


def test_resource_choices_round_trip_and_local_configuration_rejects_them(tmp_path):
    fields = {name: values[2][-1] for name, values in RESOURCE_SIZING.items()}
    selected = replace(CONFIG, **fields)
    path = tmp_path / ".env"
    configuration.initialize_config(selected, path)
    assert configuration.load_config(path) == selected
    assert selected.identity_hash == CONFIG.identity_hash
    with pytest.raises(configuration.ConfigError, match="Azure settings"):
        configuration.DemoConfig("local", "sample", "demo", **fields)


@pytest.mark.parametrize("name", RESOURCE_SIZING)
def test_invalid_resource_choices_are_never_silently_defaulted(name):
    with pytest.raises(configuration.ConfigError):
        replace(CONFIG, **{name: "invalid"})
