import copy
import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.operations import config as configuration  # noqa: E402
from scripts.operations.azure import postgres_sizes as subject  # noqa: E402

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
CONFIG = configuration.DemoConfig("azure", "sample", "learn", SUBSCRIPTION, "eastus2")
NAMES = ("Standard_D2ads_v5", "Standard_D2ds_v4", "Standard_D2ds_v5")


def capabilities():
    return [
        {
            "name": "FlexibleServerCapabilities",
            "status": None,
            "reason": None,
            "restricted": None,
            "supportedFeatures": [{"name": "OfferRestricted", "status": "Disabled"}],
            "supportedServerVersions": [{"name": "16", "status": None, "reason": None}],
            "supportedServerEditions": [
                {
                    "name": "GeneralPurpose",
                    "status": None,
                    "reason": None,
                    "supportedServerSkus": [
                        {
                            "name": name,
                            "vCores": 2,
                            "supportedMemoryPerVcoreMb": 4096,
                            "supportedZones": ["1", "2", "3"],
                            "status": None,
                            "reason": None,
                        }
                        for name in NAMES
                    ],
                }
            ],
        }
    ]


class Runner:
    def __init__(self):
        self.value = capabilities()
        self.calls = []
        self.fail = False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert argv == [
            "az",
            "postgres",
            "flexible-server",
            "list-skus",
            "--location",
            "eastus2",
            "--subscription",
            SUBSCRIPTION,
            "--output",
            "json",
        ]
        assert kwargs == {
            "stdout": subprocess.PIPE,
            "text": True,
            "check": False,
            "timeout": 180,
        }
        if self.fail:
            print("native PostgreSQL discovery failure", file=sys.stderr)
            return subprocess.CompletedProcess(argv, 7, "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.value))


def test_postgres_discovery_uses_database_capabilities_not_vm_inventory():
    runner = Runner()
    eligible, problems = subject.Discovery(CONFIG, runner=runner).available()
    assert len(runner.calls) == 1
    assert set(eligible) == {name.lower() for name in NAMES}
    assert not any(problems.values())
    assert all(size.cpus == 2 and size.memory == 8 for size in eligible.values())


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"status": "Disabled"}, "status"),
        ({"reason": "NotAvailableForSubscription"}, "NotAvailableForSubscription"),
        ({"vCores": True}, "invalid"),
        ({"vCores": 16}, "2-8"),
        ({"supportedMemoryPerVcoreMb": 1}, "8-64"),
        ({"supportedMemoryPerVcoreMb": "NaN"}, "invalid"),
        ({"supportedZones": None}, "zones"),
    ],
)
def test_restricted_or_incomplete_skus_are_not_offered(change, reason):
    records = capabilities()
    records[0]["supportedServerEditions"][0]["supportedServerSkus"][0].update(change)
    eligible, problems = subject.available_sizes(records)
    assert NAMES[0].lower() not in eligible
    assert reason in problems[NAMES[0].lower()]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"restricted": True}, "restricted"),
        ({"status": "Disabled"}, "unavailable"),
        ({"supportedServerVersions": [{"name": "15"}]}, "PostgreSQL 16"),
        ({"supportedServerVersions": None}, "version capabilities"),
        ({"supportedServerEditions": []}, "GeneralPurpose"),
        ({"supportedFeatures": [{"name": "OfferRestricted", "status": "Enabled"}]}, "offer"),
    ],
)
def test_regional_restrictions_stop_selection(change, reason):
    records = capabilities()
    records[0].update(change)
    with pytest.raises(subject.PostgresSizeError, match=reason):
        subject.available_sizes(records)


def test_duplicate_sku_metadata_is_not_treated_as_a_choice():
    records = capabilities()
    skus = records[0]["supportedServerEditions"][0]["supportedServerSkus"]
    skus.append(copy.deepcopy(skus[0]))
    with pytest.raises(subject.PostgresSizeError, match="duplicate"):
        subject.available_sizes(records)


def test_non_zonal_region_does_not_require_an_explicit_zone():
    records = capabilities()
    records[0]["supportedServerEditions"][0]["supportedServerSkus"][0]["supportedZones"] = []
    eligible, _ = subject.available_sizes(records)
    assert eligible[NAMES[0].lower()].zones == ()


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    monkeypatch.delenv("PLANE_DEMO_PROGRESS_DIR", raising=False)
    selected = replace(CONFIG, demo_keys={"management": "synthetic-private-key-" + "x" * 32})
    path = tmp_path / ".env"
    configuration.initialize_config(selected, path)
    monkeypatch.setenv("CONFIRM_AZURE", "yes")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(sys, "argv", ["postgres_sizes.py", "--config", str(path)])
    runner = Runner()
    discovery = subject.Discovery(selected, runner=runner)
    monkeypatch.setattr(subject, "Discovery", lambda _: discovery)
    return path, selected, runner


def test_main_prompts_and_atomically_persists_both_selection_fields(
    invocation, monkeypatch, capsys
):
    path, original, runner = invocation
    monkeypatch.setattr(sys, "stdin", io.StringIO("invalid\n2\n"))
    assert subject.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "postgresSkuName": NAMES[1],
        "postgresSkuTier": "GeneralPurpose",
    }
    assert all(name in output.err for name in NAMES)
    assert "Enter a number" in output.err
    assert "does not reserve capacity or quota" in output.err
    assert "not price" in output.err
    assert original.demo_keys["management"] not in output.out + output.err
    assert configuration.load_config(path) == replace(
        original, postgres_sku_name=NAMES[1], postgres_sku_tier="GeneralPurpose"
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert subject.main() == 0
    assert len(runner.calls) == 2
    assert "Select 1-" not in capsys.readouterr().err


@pytest.mark.parametrize("answer", ["q\n", ""])
def test_cancel_and_eof_preserve_configuration(invocation, monkeypatch, answer):
    path, _, _ = invocation
    before = path.read_bytes()
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert subject.main() == 130
    assert path.read_bytes() == before


def test_prompt_pauses_progress_and_releases_it_on_interrupt(invocation, monkeypatch):
    path, _, _ = invocation
    directory = path.parent / "plane-progress.postgres"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("PLANE_DEMO_PROGRESS_DIR", str(directory))

    class Input:
        def readline(self):
            assert (directory / "heartbeat").is_dir()
            raise KeyboardInterrupt

    before = path.read_bytes()
    monkeypatch.setattr(sys, "stdin", Input())
    assert subject.main() == 130
    assert not (directory / "heartbeat").exists()
    assert path.read_bytes() == before


def test_native_discovery_error_never_saves_a_default(invocation, capsys):
    path, _, runner = invocation
    runner.fail = True
    before = path.read_bytes()
    assert subject.main() == 1
    output = capsys.readouterr()
    assert "native PostgreSQL discovery failure" in output.err
    assert "exit 7" in output.err and not output.out
    assert path.read_bytes() == before


def test_no_eligible_compute_stops_without_prompt(invocation, capsys):
    path, _, runner = invocation
    runner.value[0]["supportedServerEditions"][0]["supportedServerSkus"] = []
    before = path.read_bytes()
    assert subject.main() == 1
    output = capsys.readouterr()
    assert "No eligible PostgreSQL" in output.err and "Select 1-" not in output.err
    assert path.read_bytes() == before


def test_invalid_saved_choice_requires_another_explicit_selection(invocation, monkeypatch, capsys):
    path, original, _ = invocation
    configuration.initialize_config(
        replace(original, postgres_sku_name="Standard_D2s_v3", postgres_sku_tier="GeneralPurpose"),
        path,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("3\n"))
    assert subject.main() == 0
    assert "Saved PostgreSQL SKU" in capsys.readouterr().err
    assert configuration.load_config(path).postgres_sku_name == NAMES[2]


def test_concurrent_configuration_changes_are_not_overwritten(invocation, monkeypatch):
    path, original, _ = invocation
    newer = replace(original, deployment="newer")

    class Input:
        def readline(self):
            configuration.initialize_config(newer, path)
            return "1\n"

    monkeypatch.setattr(sys, "stdin", Input())
    assert subject.main() == 1
    assert configuration.load_config(path) == newer


@pytest.mark.parametrize("changed", ["missing", "different", "unavailable"])
def test_existing_foundation_cannot_be_adopted_or_resized(invocation, monkeypatch, capsys, changed):
    path, original, runner = invocation
    selected = replace(original, postgres_sku_name=NAMES[0], postgres_sku_tier="GeneralPurpose")
    configuration.initialize_config(selected, path)
    foundation = {
        "projectName": selected.project,
        "deploymentName": selected.deployment,
        "subscriptionId": selected.subscription,
        "environment": "azure",
        "location": "eastus2",
    }
    if changed != "missing":
        foundation.update(
            postgresSkuName=NAMES[1] if changed == "different" else NAMES[0],
            postgresSkuTier="GeneralPurpose",
        )
    if changed == "unavailable":
        runner.value[0]["supportedServerEditions"][0]["supportedServerSkus"][0]["status"] = (
            "Disabled"
        )
    document = path.parent / "foundation.json"
    document.write_text(json.dumps({"foundation": foundation}))
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--existing-foundation", str(document)])
    before = path.read_bytes()
    assert subject.main() == 1
    assert path.read_bytes() == before
    message = "fresh deployment name" if changed == "missing" else "no automatic resize"
    assert message in capsys.readouterr().err


def test_confirmation_is_required_before_azure_reads(invocation, monkeypatch):
    path, _, runner = invocation
    before = path.read_bytes()
    monkeypatch.delenv("CONFIRM_AZURE")
    assert subject.main() == 1
    assert not runner.calls and path.read_bytes() == before
