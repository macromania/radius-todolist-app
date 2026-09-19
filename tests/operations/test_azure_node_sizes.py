import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.operations import config as configuration  # noqa: E402
from scripts.operations.azure import node_sizes as subject  # noqa: E402

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
CONFIG = configuration.DemoConfig("azure", "sample", "learn", SUBSCRIPTION, "centralus")
NAMES = ("Standard_D4as_v7", "Standard_D4s_v7", "Standard_E4as_v7")
FAMILIES = ("StandardDasv7Family", "StandardDsv7Family", "StandardEasv7Family")
BUDGET = subject.Budget(5, 2)


def sku(name=NAMES[0], family=FAMILIES[0], *, cpus=4, memory=16, restrictions=None, **caps):
    return {
        "name": name,
        "family": family,
        "locations": ["centralus"],
        "restrictions": restrictions or [],
        "capabilities": [
            {"name": key, "value": value}
            for key, value in {
                "vCPUs": str(cpus),
                "MemoryGB": str(memory),
                "CpuArchitectureType": "x64",
                "HyperVGenerations": "V2",
                "PremiumIO": "True",
                "DiskControllerTypes": "NVMe",
                "EphemeralOSDiskSupported": "False",
                **caps,
            }.items()
        ],
    }


def quotas(*, current=0, limit=100):
    return [
        {"name": {"value": name}, "currentValue": str(current), "limit": str(limit)}
        for name in ("cores", *FAMILIES)
    ]


class Runner:
    def __init__(self):
        self.skus = [
            sku(name, family, memory=32 if name.startswith("Standard_E") else 16)
            for name, family in zip(NAMES, FAMILIES, strict=True)
        ]
        self.usage = quotas()
        self.clusters = []
        self.calls = []
        self.fail = None
        self.next_page = None

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert kwargs == {
            "stdout": subprocess.PIPE,
            "text": True,
            "check": False,
            "timeout": 180,
        }
        if self.fail and argv[: len(self.fail)] == list(self.fail):
            print("native discovery failure", file=sys.stderr)
            return subprocess.CompletedProcess(argv, 7, "")
        if argv[0] == "az":
            assert argv[-4:] == ["--subscription", SUBSCRIPTION, "--output", "json"]
            values = {
                ("vm", "list-skus"): self.skus,
                ("vm", "list-usage"): self.usage,
                ("aks", "list"): self.clusters,
            }
            value = values[tuple(argv[1:3])]
        else:
            assert argv[0] == "curl" and argv[-1].startswith(subject.PRICES)
            value = {
                "NextPageLink": self.next_page,
                "Items": [
                    {
                        "armSkuName": name,
                        "armRegionName": "centralus",
                        "type": "Consumption",
                        "currencyCode": "USD",
                        "unitOfMeasure": "1 Hour",
                        "retailPrice": price,
                        "productName": "Virtual Machines Linux",
                        "meterName": name,
                    }
                    for name, price in zip(NAMES, (0.182, 0.254, 0.3), strict=True)
                ],
            }
        return subprocess.CompletedProcess(argv, 0, json.dumps(value))


def test_discovery_uses_scoped_current_reads_and_reserves_the_full_fleet():
    runner = Runner()
    eligible, problems = subject.Discovery(CONFIG, runner=runner).available(BUDGET)
    assert set(eligible) == {name.lower() for name in NAMES}
    assert all(value is None for value in problems.values())
    assert BUDGET.fleet_nodes == 10
    assert BUDGET.required_cores(eligible[NAMES[0].lower()]) == 60
    assert {tuple(call[1:3]) for call in runner.calls} == {
        ("vm", "list-skus"),
        ("vm", "list-usage"),
        ("aks", "list"),
    }
    assert all("--all" in call for call in runner.calls if call[1:3] == ["vm", "list-skus"])
    cluster_query = next(call for call in runner.calls if call[1:3] == ["aks", "list"])
    assert CONFIG.stem in cluster_query[cluster_query.index("--query") + 1]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"CpuArchitectureType": "Arm64"}, "x64"),
        ({"HyperVGenerations": "V1"}, "Generation 2"),
        ({"PremiumIO": "False"}, "managed disk"),
        ({"vCPUs": "2"}, "4-16 vCPUs"),
        ({"MemoryGB": "8"}, "16-64 GiB"),
        ({"MemoryGB": "NaN"}, "16-64 GiB"),
        ({"vCPUs": "invalid"}, "invalid"),
    ],
)
def test_incompatible_hardware_is_not_an_option(change, reason):
    size, problem = subject.describe_size(sku(**change), "centralus")
    assert size is None and reason in problem


def test_location_restriction_is_not_confused_with_zone_only_restrictions():
    restriction = {
        "type": "Zone",
        "reasonCode": "NotAvailableForSubscription",
        "restrictionInfo": {"locations": ["centralus"], "zones": ["1", "2", "3"]},
        "values": ["centralus"],
    }
    assert subject.describe_size(sku(restrictions=[restriction]), "centralus")[0] is not None
    restriction["type"] = "Location"
    size, problem = subject.describe_size(sku(restrictions=[restriction]), "centralus")
    assert size is None and "NotAvailableForSubscription" in problem
    restriction["restrictionInfo"]["locations"] = ["westus3"]
    assert subject.describe_size(sku(restrictions=[restriction]), "centralus")[0] is not None


@pytest.mark.parametrize("missing", ["restrictions", "capabilities", "family", "locations"])
def test_incomplete_sku_metadata_never_means_available(missing):
    value = sku()
    value.pop(missing)
    assert subject.describe_size(value, "centralus")[0] is None


def test_large_or_family_quota_limited_sizes_are_excluded():
    runner = Runner()
    runner.skus[0] = sku(cpus=8, memory=32)
    runner.usage[-1]["limit"] = 0
    eligible, problems = subject.Discovery(CONFIG, runner=runner).available(BUDGET)
    assert set(eligible) == {NAMES[1].lower()}
    assert "120" in problems[NAMES[0].lower()]
    assert "0 free" in problems[NAMES[2].lower()]


def test_owned_running_nodes_are_not_counted_twice_against_quota():
    runner = Runner()
    runner.usage = quotas(current=8, limit=60)
    name = f"aks-{CONFIG.slot_name('management')}"
    runner.clusters = [
        {
            "name": name,
            "location": "centralus",
            "id": (
                f"{CONFIG.plane_group_id('management')}/providers/"
                f"Microsoft.ContainerService/managedClusters/{name}"
            ),
            "tags": {
                "project": "sample",
                "deployment": "learn",
                "environment": "azure",
                "managedBy": "radius-todolist-app",
            },
            "provisioningState": "Succeeded",
            "powerState": {"code": "Running"},
            "agentPoolProfiles": [{"vmSize": NAMES[0], "count": 2}],
        }
    ]
    eligible, _ = subject.Discovery(CONFIG, runner=runner).available(BUDGET)
    assert set(eligible) == {NAMES[0].lower()}
    runner.clusters[0]["tags"]["deployment"] = "foreign"
    with pytest.raises(subject.NodeSizeError, match="different resource owner"):
        subject.Discovery(CONFIG, runner=runner).available(BUDGET)


def test_discovery_failure_preserves_the_native_diagnostic(capsys):
    runner = Runner()
    runner.fail = ("az", "vm", "list-skus")
    with pytest.raises(subject.NodeSizeError, match="exit 7"):
        subject.Discovery(CONFIG, runner=runner).available(BUDGET)
    assert "native discovery failure" in capsys.readouterr().err


def test_malformed_cluster_identity_is_reported_as_a_discovery_error():
    runner = Runner()
    runner.clusters = [{"name": {}}]
    with pytest.raises(subject.NodeSizeError, match="Unexpected or duplicate AKS cluster"):
        subject.Discovery(CONFIG, runner=runner).available(BUDGET)


@pytest.mark.parametrize(
    "next_page",
    [
        "https://unrelated.invalid/prices",
        "http://prices.azure.com/x",
        "https://prices.azure.com:invalid/prices",
    ],
)
def test_retail_pagination_cannot_send_requests_to_another_host(next_page):
    runner = Runner()
    runner.next_page = next_page
    sizes = [subject.describe_size(row, "centralus")[0] for row in runner.skus]
    with pytest.raises(subject.NodeSizeError, match="pagination"):
        subject.Discovery(CONFIG, runner=runner).prices(sizes)
    assert len(runner.calls) == 1


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    monkeypatch.delenv("PLANE_DEMO_PROGRESS_DIR", raising=False)
    config = replace(CONFIG, demo_keys={"management": "synthetic-private-key-" + "x" * 32})
    path = tmp_path / ".env"
    configuration.initialize_config(config, path)
    template = tmp_path / "foundation.json"
    template.write_text(
        json.dumps(
            {
                "parameters": {
                    "nodeCount": {"defaultValue": 2},
                    "childSlots": {"defaultValue": list(configuration.SLOTS[1:])},
                }
            }
        )
    )
    monkeypatch.setenv("CONFIRM_AZURE", "yes")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "node_sizes.py",
            "--config",
            str(path),
            "--template",
            str(template),
        ],
    )
    runner = Runner()
    discovery = subject.Discovery(config, runner=runner)
    monkeypatch.setattr(subject, "Discovery", lambda selected: discovery)
    return path, config, runner


def test_real_main_prompts_then_persists_only_the_selected_setting(invocation, monkeypatch, capsys):
    path, original, runner = invocation
    monkeypatch.setattr(sys, "stdin", io.StringIO("invalid\n2\n"))
    assert subject.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"nodeVmSize": NAMES[1], "nodeCount": 2}
    assert all(name in output.err for name in NAMES)
    assert "one surge node per cluster" in output.err
    assert "1.820" in output.err and "2.540" in output.err
    assert "Enter a number" in output.err
    saved = configuration.load_config(path)
    assert saved == replace(original, node_vm_size=NAMES[1])
    assert original.demo_keys["management"] not in output.out + output.err
    assert path.stat().st_mode & 0o777 == 0o600
    before = len(runner.calls)
    assert subject.main() == 0
    assert not any(call[0] == "curl" for call in runner.calls[before:])
    assert "Select 1-" not in capsys.readouterr().err


def test_enter_selects_the_recommended_eligible_node_size(invocation, monkeypatch, capsys):
    path, _, _ = invocation
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert subject.main() == 0
    assert configuration.load_config(path).node_vm_size == NAMES[0]
    output = capsys.readouterr()
    assert output.err.count("(recommended)") == 1
    assert "Enter = 1, recommended" in output.err
    assert "ranked by available retail cost" in output.err


@pytest.mark.parametrize("answer", ["q\n", ""])
def test_cancel_or_eof_does_not_change_configuration(invocation, monkeypatch, answer):
    path, _, _ = invocation
    before = path.read_bytes()
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert subject.main() == 130
    assert path.read_bytes() == before


@pytest.mark.parametrize("answer,expected", [("2\n", 0), ("q\n", 130), ("", 130), (None, 130)])
def test_selector_preserves_input_and_cancellation_without_progress_files(
    invocation, monkeypatch, answer, expected
):
    path, _, _ = invocation

    class Input:
        def readline(self):
            assert not list(path.parent.glob("plane-progress.*"))
            if answer is None:
                raise KeyboardInterrupt
            return answer

    monkeypatch.setattr(sys, "stdin", Input())
    before = path.read_bytes()
    assert subject.main() == expected
    assert not list(path.parent.glob("plane-progress.*"))
    if expected != 0:
        assert path.read_bytes() == before


def test_config_changed_during_selection_is_not_overwritten(invocation, monkeypatch, capsys):
    path, original, _ = invocation
    newer = replace(original, deployment="newer")

    class Input:
        def readline(self):
            configuration.initialize_config(newer, path)
            return "1\n"

    monkeypatch.setattr(sys, "stdin", Input())
    assert subject.main() == 1
    assert configuration.load_config(path) == newer
    assert "changed while selecting" in capsys.readouterr().err


def test_no_candidates_stops_without_prompt_or_config_write(invocation, monkeypatch, capsys):
    path, _, runner = invocation
    before = path.read_bytes()
    runner.usage = quotas(limit=0)
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
    assert subject.main() == 1
    output = capsys.readouterr()
    assert not output.out and "No eligible x64 node sizes" in output.err
    assert "Select 1-" not in output.err
    assert path.read_bytes() == before


def test_unavailable_saved_size_prompts_for_a_replacement(invocation, monkeypatch, capsys):
    path, original, runner = invocation
    configuration.initialize_config(replace(original, node_vm_size="Standard_D4s_v5"), path)
    runner.skus.append(
        sku(
            "Standard_D4s_v5",
            "standardDSv5Family",
            restrictions=[
                {
                    "type": "Location",
                    "values": ["centralus"],
                    "reasonCode": "NotAvailableForSubscription",
                }
            ],
        )
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
    assert subject.main() == 0
    assert "NotAvailableForSubscription" in capsys.readouterr().err
    assert configuration.load_config(path) == replace(original, node_vm_size=NAMES[0])


def test_confirmation_precedes_discovery_and_config_writes(invocation, monkeypatch, capsys):
    path, _, runner = invocation
    before = path.read_bytes()
    monkeypatch.delenv("CONFIRM_AZURE")
    assert subject.main() == 1
    assert "CONFIRM_AZURE" in capsys.readouterr().err
    assert not runner.calls and path.read_bytes() == before


def test_selected_node_count_changes_real_main_quota_budget(invocation, monkeypatch, capsys):
    path, original, runner = invocation
    configuration.initialize_config(replace(original, node_count=4), path)
    runner.usage = quotas(limit=99)
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
    assert subject.main() == 1
    assert "No eligible x64 node sizes" in capsys.readouterr().err
    assert configuration.load_config(path).node_vm_size is None
    runner.usage = quotas(limit=200)
    assert subject.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["nodeCount"] == 4
    assert "5 clusters x 4 nodes" in output.err


def test_existing_foundation_cannot_be_resized_by_the_selector(invocation, monkeypatch, capsys):
    path, original, _ = invocation
    configuration.initialize_config(replace(original, node_vm_size=NAMES[0]), path)
    foundation = path.parent / "existing.json"
    foundation.write_text(
        json.dumps(
            {
                "foundation": {
                    "projectName": original.project,
                    "deploymentName": original.deployment,
                    "subscriptionId": original.subscription,
                    "environment": "azure",
                    "nodeVmSize": NAMES[1],
                    "nodeCount": 2,
                }
            }
        )
    )
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--existing-foundation", str(foundation)])
    before = path.read_bytes()
    assert subject.main() == 1
    assert path.read_bytes() == before
    assert "no automatic resize" in capsys.readouterr().err


def test_malformed_existing_foundation_is_reported_without_a_config_write(
    invocation, monkeypatch, capsys
):
    path, _, runner = invocation
    foundation = path.parent / "existing.json"
    foundation.write_text('{"foundation":[]}')
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--existing-foundation", str(foundation)])
    before = path.read_bytes()
    assert subject.main() == 1
    assert "Existing foundation is not an object" in capsys.readouterr().err
    assert not runner.calls and path.read_bytes() == before


def test_price_failure_is_explicit_and_never_displays_a_zero_quote(invocation, monkeypatch, capsys):
    _, _, runner = invocation
    runner.fail = ("curl",)
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
    assert subject.main() == 0
    output = capsys.readouterr()
    assert "Retail prices unavailable" in output.err and "unavailable" in output.err
    assert "0.000" not in output.err
