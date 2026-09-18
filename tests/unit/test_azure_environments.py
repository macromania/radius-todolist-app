import copy

import pytest

from plane_demo.management.providers.azure_environments import (
    EnvironmentError,
    allocation_index,
    environment_deployment,
    merge_foundations,
    next_allocation_start,
    ordered_allocations,
)
from plane_demo.management.providers.identity import (
    AZURE_DEFAULT_SLOTS,
    ConfigError,
    DemoConfig,
    isolated_pair,
    provisioning_namespace,
)

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
CONFIG = DemoConfig("azure", "sample", "demo", SUBSCRIPTION, "eastus2")


def allocation(slot, index):
    return {
        "slot": slot,
        "slotIndex": index,
        "gatewaySubnetCidr": f"10.64.{16 + index}.0/24",
        "roleDefinitionPrefix": CONFIG.stem
        if slot in AZURE_DEFAULT_SLOTS
        else f"{CONFIG.stem}-{slot.rsplit('-', 1)[0]}",
    }


@pytest.fixture
def base():
    return {
        "foundation": {
            "projectName": CONFIG.project,
            "deploymentName": CONFIG.deployment,
            "subscriptionId": SUBSCRIPTION,
            "location": CONFIG.location,
            "environment": "azure",
            "environmentMode": "prepared-v1",
            "resourceGroupLayout": "plane-v2",
            "resourcePrefix": CONFIG.stem,
            "virtualNetworkId": "vnet-owned",
            "registryId": "registry-owned",
            "vaultId": "vault-owned",
        },
        "allocations": [allocation(slot, index) for index, slot in enumerate(AZURE_DEFAULT_SLOTS)],
    }


def environment(pair="isolated-blue", start=3):
    name = environment_deployment(CONFIG, pair)
    return {
        "id": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Resources/deployments/{name}",
        "name": name,
        "properties": {
            "provisioningState": "Succeeded",
            "outputs": {
                "environment": {
                    "value": {
                        "pairId": pair,
                        "allocationStart": start,
                        "projectName": CONFIG.project,
                        "deploymentName": CONFIG.deployment,
                        "subscriptionId": SUBSCRIPTION,
                        "location": CONFIG.location,
                        "environmentMode": "prepared-v1",
                        "baseDeploymentId": (
                            f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Resources/"
                            f"deployments/{CONFIG.stem}-bootstrap"
                        ),
                        "virtualNetworkId": "vnet-owned",
                        "registryId": "registry-owned",
                        "vaultId": "vault-owned",
                    }
                },
                "allocations": {
                    "value": [
                        allocation(f"{pair}-control", start),
                        allocation(f"{pair}-data", start + 1),
                    ]
                },
            },
        },
    }


def test_default_catalog_has_three_slots_and_no_isolated_resources(base):
    observed = merge_foundations(CONFIG, base, [])
    assert [item["slot"] for item in observed["allocations"]] == list(AZURE_DEFAULT_SLOTS)
    assert observed["foundation"]["environmentFoundations"] == {}
    assert base.get("foundation", {}).get("environmentFoundations") is None


def test_additive_catalog_preserves_default_allocations_and_named_indices(base):
    original = copy.deepcopy(base)
    result = merge_foundations(CONFIG, base, [environment(), environment("isolated-green", 5)])
    assert result["allocations"][:3] == base["allocations"]
    assert base == original
    assert [allocation_index(item) for item in result["allocations"]] == list(range(7))
    assert set(result["foundation"]["environmentFoundations"]) == {
        "isolated-blue",
        "isolated-green",
    }
    assert provisioning_namespace(CONFIG.stem, "isolated-green-data", index=6).endswith("-p-6")


@pytest.mark.parametrize(
    "change", ["owner", "platform", "overlap", "incomplete", "failed", "role-scope"]
)
def test_foreign_or_ambiguous_additions_are_rejected(base, change):
    record = environment()
    if change == "owner":
        record["id"] = record["id"].replace(SUBSCRIPTION, "foreign")
    elif change == "platform":
        record["properties"]["outputs"]["environment"]["value"]["virtualNetworkId"] = "foreign"
    elif change == "overlap":
        record = environment(start=1)
    elif change == "incomplete":
        record["properties"]["outputs"]["allocations"]["value"].pop()
    elif change == "failed":
        record["properties"]["provisioningState"] = "Failed"
    else:
        record["properties"]["outputs"]["allocations"]["value"][0]["roleDefinitionPrefix"] = (
            CONFIG.stem
        )
    with pytest.raises(EnvironmentError):
        merge_foundations(CONFIG, base, [record])


def test_retired_environment_keeps_registration_authority_without_active_slots(base):
    record = environment()
    record["tags"] = {"plane-demo/environment-state": "retired"}
    result = merge_foundations(CONFIG, base, [record])
    assert len(result["allocations"]) == 3
    assert result["foundation"]["environmentFoundations"]["isolated-blue"]["state"] == "retired"


def test_tombstones_consume_capacity_and_duplicate_indices_are_not_reassigned():
    records = [{"pairId": "isolated-blue", "allocationStart": 3, "state": "retired"}]
    assert next_allocation_start(records) == 5
    with pytest.raises(EnvironmentError, match="Duplicate"):
        next_allocation_start([*records, {"pairId": "isolated-green", "allocationStart": 3}])
    with pytest.raises(EnvironmentError, match="capacity"):
        next_allocation_start(
            [{"pairId": f"isolated-{index}", "allocationStart": index} for index in range(3, 14, 2)]
        )


@pytest.mark.parametrize("name", ["../other", "shared", "management", "UPPER", "", "a" * 13])
def test_invalid_isolated_names_do_not_become_resource_names(name):
    with pytest.raises(ConfigError):
        isolated_pair(name)


def test_named_slots_are_azure_only_and_namespace_lengths_remain_valid():
    assert CONFIG.slot_name("isolated-blue-control") == "sample-demo-azure-isolated-blue-control"
    local = DemoConfig("local", "sample", "demo")
    with pytest.raises(ConfigError):
        local.slot_name("isolated-blue-control")
    assert local.slot_name("isolated-1-control")
    longest = DemoConfig("azure", "abcdefghijklmnop", "ab", SUBSCRIPTION, "eastus2")
    assert len(longest.namespace("isolated-abcdefghijkl-control")) <= 63


def test_incomplete_default_is_not_an_environment_catalog(base):
    with pytest.raises(EnvironmentError):
        ordered_allocations(base["allocations"][:-1])
