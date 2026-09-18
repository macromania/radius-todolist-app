"""Validate and merge Azure-owned environment foundations without local inventory."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network

from plane_demo.management.providers.identity import (
    AZURE_DEFAULT_SLOTS,
    AZURE_ENVIRONMENT_MODE,
    AZURE_GROUP_LAYOUT,
    DemoConfig,
    azure_slot,
    isolated_pair,
)

MAX_ISOLATED_ENVIRONMENTS = 6


class EnvironmentError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EnvironmentError(message)


def pair_slots(pair: str) -> tuple[str, str]:
    require(pair == "shared" or isolated_pair(pair) == pair, "Invalid environment pair")
    return f"{pair}-control", f"{pair}-data"


def allocation_index(allocation: Mapping) -> int:
    value = allocation.get("gatewaySubnetCidr")
    require(isinstance(value, str), "Allocation has no gateway subnet")
    try:
        network = IPv4Network(value, strict=True)
    except ValueError as error:
        raise EnvironmentError("Invalid allocation subnet") from error
    index = network.network_address.packed[2] - 16
    require(
        network.prefixlen == 24
        and network.subnet_of(IPv4Network("10.64.16.0/20"))
        and 0 <= index <= 14,
        "Allocation subnet is outside the environment address contract",
    )
    require(
        allocation.get("slotIndex", index) == index,
        "Allocation index differs from its subnet",
    )
    return index


def ordered_allocations(allocations: Sequence[dict]) -> list[dict]:
    require(
        isinstance(allocations, (list, tuple))
        and all(isinstance(item, Mapping) and azure_slot(item.get("slot")) for item in allocations),
        "Invalid Azure allocation list",
    )
    names = [item["slot"] for item in allocations]
    indices = [allocation_index(item) for item in allocations]
    require(len(set(names)) == len(names), "Duplicate Azure slot")
    require(len(set(indices)) == len(indices), "Overlapping Azure allocation indices")
    by_name = {item["slot"]: item for item in allocations}
    for index, name in enumerate(AZURE_DEFAULT_SLOTS):
        require(
            name in by_name and allocation_index(by_name[name]) == index, "Default slots differ"
        )
    pairs = {name.removesuffix("-control") for name in names if name.endswith("-control")}
    require(
        set(names) == {"management", *(slot for pair in pairs for slot in pair_slots(pair))},
        "Every environment requires exactly one control/data pair",
    )
    for pair in pairs - {"shared"}:
        control, data = (allocation_index(by_name[slot]) for slot in pair_slots(pair))
        require(
            3 <= control <= 13 and control % 2 == 1 and data == control + 1,
            "Isolated environment indices are not a stable adjacent pair",
        )
    return sorted(allocations, key=allocation_index)


def environment_deployment(config: DemoConfig, pair: str) -> str:
    require(pair != "shared" and isolated_pair(pair) == pair, "Expected an isolated environment")
    return f"{config.stem}-environment-{pair.removeprefix('isolated-')}"


def deployment_outputs(record: dict) -> dict:
    require(isinstance(record, dict), "Invalid foundation deployment")
    properties = record.get("properties")
    require(isinstance(properties, dict), "Foundation deployment has no properties")
    require(properties.get("provisioningState") == "Succeeded", "Foundation is not complete")
    outputs = properties.get("outputs")
    require(
        isinstance(outputs, dict)
        and all(isinstance(item, dict) and "value" in item for item in outputs.values()),
        "Foundation deployment outputs are incomplete",
    )
    return {key: item["value"] for key, item in outputs.items()}


def merge_foundations(config: DemoConfig, base: dict, environments: Sequence[dict]) -> dict:
    require(config.environment == "azure", "Azure environment configuration is required")
    require(isinstance(base, dict), "Invalid base foundation")
    foundation = base.get("foundation")
    require(isinstance(foundation, dict), "Base foundation is missing")
    require(
        all(
            foundation.get(key) == value
            for key, value in {
                "projectName": config.project,
                "deploymentName": config.deployment,
                "environment": "azure",
                "subscriptionId": config.subscription,
                "location": config.location,
                "resourceGroupLayout": AZURE_GROUP_LAYOUT,
                "resourcePrefix": config.stem,
            }.items()
        ),
        "Base foundation does not match the selected deployment",
    )
    allocations = copy.deepcopy(ordered_allocations(base.get("allocations")))
    require(
        foundation.get("environmentMode") in (None, AZURE_ENVIRONMENT_MODE),
        "Unsupported environment preparation mode",
    )
    if foundation.get("environmentMode") != AZURE_ENVIRONMENT_MODE:
        require(not environments, "Legacy deployments cannot adopt isolated environments")
        return copy.deepcopy(base)
    require(
        tuple(item["slot"] for item in allocations) == AZURE_DEFAULT_SLOTS,
        "The prepared base must contain only the default three slots",
    )
    require(
        isinstance(environments, (list, tuple)) and len(environments) <= MAX_ISOLATED_ENVIRONMENTS,
        "Too many isolated environment foundations",
    )
    result = copy.deepcopy(base)
    records = {}
    base_id = (
        f"/subscriptions/{config.subscription}/providers/Microsoft.Resources/"
        f"deployments/{config.stem}-bootstrap"
    )
    for record in environments:
        outputs = deployment_outputs(record)
        environment = outputs.get("environment")
        require(isinstance(environment, dict), "Isolated environment output is missing")
        pair = environment.get("pairId")
        require(isinstance(pair, str) and isolated_pair(pair) == pair, "Invalid isolated pair")
        name = environment_deployment(config, pair)
        expected_id = (
            f"/subscriptions/{config.subscription}/providers/Microsoft.Resources/deployments/{name}"
        )
        require(
            str(record.get("id", "")).casefold() == expected_id.casefold()
            and record.get("name") == name,
            "Isolated foundation deployment identity differs",
        )
        require(
            all(
                environment.get(key) == value
                for key, value in {
                    "projectName": config.project,
                    "deploymentName": config.deployment,
                    "subscriptionId": config.subscription,
                    "location": config.location,
                    "environmentMode": AZURE_ENVIRONMENT_MODE,
                    "baseDeploymentId": base_id,
                    "virtualNetworkId": foundation["virtualNetworkId"],
                    "registryId": foundation["registryId"],
                    "vaultId": foundation["vaultId"],
                }.items()
            ),
            "Isolated foundation belongs to another deployment or platform",
        )
        require(pair not in records, "Duplicate isolated environment")
        addition = outputs.get("allocations")
        require(
            isinstance(addition, list)
            and len(addition) == 2
            and {item.get("slot") for item in addition if isinstance(item, dict)}
            == set(pair_slots(pair)),
            "Isolated foundation must contain only its own control/data pair",
        )
        role_prefix = f"{config.stem}-{pair}"
        for allocation in addition:
            require(
                allocation.get("roleDefinitionPrefix") == role_prefix,
                "Isolated role definitions are not environment-scoped",
            )
        require(
            environment.get("allocationStart") == allocation_index(addition[0])
            and addition[0]["slot"] == f"{pair}-control",
            "Isolated allocation start differs",
        )
        state = (record.get("tags") or {}).get("plane-demo/environment-state", "active")
        require(state in {"active", "retired"}, "Unknown isolated foundation lifecycle state")
        records[pair] = {**environment, "deploymentId": expected_id, "state": state}
        if state != "retired":
            allocations.extend(copy.deepcopy(addition))
    result["allocations"] = ordered_allocations(allocations)
    result["foundation"]["environmentFoundations"] = records
    return result


def next_allocation_start(records: Sequence[Mapping]) -> int:
    occupied = set()
    for record in records:
        pair = record.get("pairId")
        require(
            isinstance(pair, str) and isolated_pair(pair) == pair, "Invalid environment journal"
        )
        start = record.get("allocationStart")
        require(
            isinstance(start, int) and not isinstance(start, bool) and start in range(3, 14, 2),
            "Invalid environment journal allocation",
        )
        require(start not in occupied, "Duplicate environment journal allocation")
        occupied.add(start)
    available = [index for index in range(3, 14, 2) if index not in occupied]
    require(bool(available), "No isolated environment address capacity remains")
    return available[0]
