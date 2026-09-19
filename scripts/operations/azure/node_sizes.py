#!/usr/bin/env python3
"""Select an eligible AKS node size before creating the Azure foundation."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from scripts.operations.azure.compute_selection import (  # noqa: E402
    PRICES as PRICES,
)
from scripts.operations.azure.compute_selection import (  # noqa: E402
    Discovery as ComputeDiscovery,
)
from scripts.operations.azure.compute_selection import (  # noqa: E402
    SelectionCancelled,
    integer,
    read_selection,
)
from scripts.operations.azure.compute_selection import (  # noqa: E402
    SelectionError as NodeSizeError,
)
from scripts.operations.config import (  # noqa: E402
    SLOTS,
    ConfigError,
    DemoConfig,
    initialize_config,
    load_config,
)
from scripts.operations.output import status  # noqa: E402


@dataclass(frozen=True)
class Budget:
    clusters: int
    nodes: int

    @classmethod
    def from_template(cls, template):
        parameters = template.get("parameters", {})
        if not isinstance(parameters, dict) or not all(
            isinstance(parameters.get(name), dict) for name in ("nodeCount", "childSlots")
        ):
            raise NodeSizeError("The compiled template has no node capacity parameters")
        nodes = integer(parameters.get("nodeCount", {}).get("defaultValue"), "Node count")
        slots = parameters.get("childSlots", {}).get("defaultValue")
        if (
            nodes < 2
            or not isinstance(slots, list)
            or not all(isinstance(slot, str) for slot in slots)
            or len(set(slots)) != len(slots)
            or "management" in slots
        ):
            raise NodeSizeError("The compiled template has an invalid cluster capacity plan")
        return cls(len(slots) + 1, nodes)

    @property
    def fleet_nodes(self):
        return self.clusters * self.nodes

    def required_cores(self, size):
        # Each cluster explicitly reserves one surge node in both AKS declarations.
        return self.clusters * (self.nodes + 1) * size.cpus


@dataclass(frozen=True)
class Size:
    name: str
    family: str
    cpus: int
    memory: float


def describe_size(record, location):
    name, family = record.get("name"), record.get("family")
    if not isinstance(name, str) or not re.fullmatch(r"Standard_[DE]\d+[A-Za-z]*_v\d+", name):
        return None, "not a general-purpose or memory-optimized D/E size"
    if not isinstance(family, str) or not family:
        return None, "VM family is missing"
    locations = record.get("locations")
    if not isinstance(locations, list) or location not in locations:
        return None, f"not offered in {location}"
    restrictions = record.get("restrictions")
    if not isinstance(restrictions, list):
        return None, "availability restrictions are missing"
    for restriction in restrictions:
        if not isinstance(restriction, dict):
            return None, "invalid availability restriction"
        kind = restriction.get("type")
        if kind == "Location":
            info = restriction.get("restrictionInfo")
            locations = info.get("locations") if isinstance(info, dict) else None
            locations = locations if locations is not None else restriction.get("values")
            if not isinstance(locations, list):
                return None, "location restriction has no scope"
            if location in locations:
                return (
                    None,
                    f"unavailable in {location}: {restriction.get('reasonCode', 'restricted')}",
                )
        elif kind != "Zone":
            return None, "unknown availability restriction"
        # Both node pools are regional; a zone-only restriction does not exclude them.
    capabilities = record.get("capabilities")
    if not isinstance(capabilities, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) for item in capabilities
    ):
        return None, "VM capabilities are missing"
    caps = {item["name"]: item.get("value") for item in capabilities}
    if len(caps) != len(capabilities):
        return None, "duplicate VM capabilities"
    if caps.get("CpuArchitectureType") != "x64":
        return None, "requires x64 for the demo's amd64 images"
    generations = caps.get("HyperVGenerations")
    if not isinstance(generations, str) or "V2" not in generations.replace(" ", "").split(","):
        return None, "requires a Generation 2 Azure Linux image"
    if caps.get("PremiumIO") != "True":
        return None, "premium managed disk support is unavailable"
    try:
        cpus = integer(caps.get("vCPUs"), "VM vCPUs")
        memory = float(caps.get("MemoryGB", "invalid"))
    except (NodeSizeError, TypeError, ValueError):
        return None, "CPU or memory capabilities are invalid"
    if not 4 <= cpus <= 16 or not math.isfinite(memory) or not 16 <= memory <= 64:
        return None, "demo choices require 4-16 vCPUs and 16-64 GiB RAM"
    return Size(name, family.lower(), cpus, memory), None


def quota_rows(rows):
    quotas = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), dict):
            raise NodeSizeError("Azure returned an invalid quota entry")
        name = row["name"].get("value")
        if not isinstance(name, str) or name.lower() in quotas:
            raise NodeSizeError("Azure returned an invalid or duplicate quota name")
        quotas[name.lower()] = (
            integer(row.get("currentValue"), f"{name} usage"),
            integer(row.get("limit"), f"{name} limit"),
        )
    if "cores" not in quotas:
        raise NodeSizeError("Azure did not return the regional vCPU quota")
    return quotas


def owned_cores(clusters, config: DemoConfig, sizes, slots=SLOTS):
    credits = {"cores": 0}
    expected = {f"aks-{config.slot_name(slot)}": slot for slot in slots}
    seen = set()
    for cluster in clusters:
        if not isinstance(cluster, dict):
            raise NodeSizeError("Azure returned an invalid AKS entry")
        name = cluster.get("name")
        if not isinstance(name, str) or name not in expected or name in seen:
            raise NodeSizeError("Unexpected or duplicate AKS cluster in the selected deployment")
        seen.add(name)
        slot = expected[name]
        identifier = (
            f"{config.plane_group_id(slot)}/providers/"
            f"Microsoft.ContainerService/managedClusters/{name}"
        )
        tags = cluster.get("tags") or {}
        if (
            not isinstance(tags, dict)
            or str(cluster.get("id", "")).lower() != identifier.lower()
            or cluster.get("location") != config.location
            or any(
                tags.get(key) != value
                for key, value in {
                    "project": config.project,
                    "deployment": config.deployment,
                    "environment": "azure",
                    "managedBy": "radius-todolist-app",
                }.items()
            )
        ):
            raise NodeSizeError("AKS capacity discovery found a different resource owner")
        power_state = cluster.get("powerState")
        power = power_state.get("code") if isinstance(power_state, dict) else None
        if cluster.get("provisioningState") != "Succeeded" or power not in {"Running", "Stopped"}:
            raise NodeSizeError(f"{name}: wait for a stable AKS state before checking capacity")
        if power == "Stopped":
            continue
        pools = cluster.get("agentPoolProfiles")
        if not isinstance(pools, list):
            raise NodeSizeError(f"{name}: node pool capacity is missing")
        for pool in pools:
            if not isinstance(pool, dict):
                raise NodeSizeError(f"{name}: invalid node pool")
            size = sizes.get(str(pool.get("vmSize", "")).lower())
            if size is None:
                raise NodeSizeError(f"{name}: cannot verify the existing node size")
            count = integer(pool.get("count"), f"{name} node count")
            credits["cores"] += count * size.cpus
            credits[size.family] = credits.get(size.family, 0) + count * size.cpus
    return credits


def quota_problem(size, budget, quotas, credits):
    required = budget.required_cores(size)
    for name in ("cores", size.family):
        if name not in quotas:
            return f"{name}: quota was not returned"
        current, limit = quotas[name]
        owned = credits.get(name, 0)
        if owned > current:
            return f"{name}: quota usage has not caught up with existing AKS capacity; retry later"
        needed = max(0, required - owned)
        if current + needed > limit:
            return (
                f"{name}: needs {needed} additional vCPUs including surge; {limit - current} free"
            )
    return None


class Discovery(ComputeDiscovery):
    def available(self, budget, *, slots=SLOTS):
        with ThreadPoolExecutor(max_workers=3) as pool:
            sku_request = pool.submit(
                self.az,
                "vm",
                "list-skus",
                "--location",
                self.config.location,
                "--resource-type",
                "virtualMachines",
                "--all",
                "--query",
                "[?starts_with(name, 'Standard_D') || starts_with(name, 'Standard_E')]",
            )
            quota_request = pool.submit(
                self.az, "vm", "list-usage", "--location", self.config.location
            )
            cluster_request = pool.submit(
                self.az,
                "aks",
                "list",
                "--query",
                f"[?starts_with(name, 'aks-{self.config.stem}-')]",
            )
            records, quotas, clusters = (
                sku_request.result(),
                quota_request.result(),
                cluster_request.result(),
            )
        sizes, problems = {}, {}
        for record in records:
            name = record.get("name")
            if not isinstance(name, str) or name.lower() in problems:
                raise NodeSizeError("Azure returned an invalid or duplicate VM size name")
            size, problem = describe_size(record, self.config.location)
            problems[name.lower()] = problem
            if size is not None:
                sizes[name.lower()] = size
        quotas = quota_rows(quotas)
        credits = owned_cores(clusters, self.config, sizes, slots)
        eligible = {}
        for name, size in sizes.items():
            problem = quota_problem(size, budget, quotas, credits)
            if problem:
                problems[name] = problem
            else:
                eligible[name] = size
        return eligible, problems

    def prices(self, sizes):
        prices = {}
        names = [size.name for size in sizes]
        for offset in range(0, len(names), 15):
            selected = names[offset : offset + 15]
            filter_text = (
                "serviceName eq 'Virtual Machines' and priceType eq 'Consumption' and "
                f"armRegionName eq '{self.config.location}' and ("
                + " or ".join(f"armSkuName eq '{name}'" for name in selected)
                + ")"
            )
            for item in self.retail_items(filter_text, "Prices: Linux pay-as-you-go compute"):
                name = item.get("armSkuName")
                if (
                    name not in selected
                    or item.get("armRegionName") != self.config.location
                    or item.get("type") != "Consumption"
                    or item.get("currencyCode") != "USD"
                    or item.get("unitOfMeasure") != "1 Hour"
                    or item.get("isPrimaryMeterRegion") is False
                    or re.search(
                        r"Windows|Spot|Low Priority|RHEL|SUSE|Red Hat",
                        f"{item.get('productName', '')} {item.get('meterName', '')}",
                        re.IGNORECASE,
                    )
                ):
                    continue
                price = item.get("retailPrice")
                if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
                    raise NodeSizeError(f"{name}: invalid retail price")
                if name in prices and prices[name] != price:
                    raise NodeSizeError(f"{name}: ambiguous retail price")
                prices[name] = price
        return prices


def select_size(eligible, problems, config, budget, discovery, *, existing=None):
    requested = existing or config.node_vm_size
    if existing and config.node_vm_size and existing.lower() != config.node_vm_size.lower():
        raise NodeSizeError(
            "The saved node size differs from the existing foundation; no automatic resize"
        )
    if requested and requested.lower() in eligible:
        size = eligible[requested.lower()]
        status("success", f"AKS node size: {size.name}, {size.cpus} vCPUs, {size.memory:g} GiB")
        return size
    if requested:
        reason = problems.get(requested.lower()) or "not present in the regional SKU catalogue"
        if existing:
            raise NodeSizeError(f"Existing node size {requested}: {reason}; no automatic resize")
        status("warning", f"Saved node size {requested}: {reason}")
    if not eligible:
        raise NodeSizeError(
            f"No eligible x64 node sizes in {config.location} have quota for "
            f"{budget.fleet_nodes} demo nodes and {budget.clusters} surge nodes"
        )
    try:
        prices = discovery.prices(list(eligible.values()))
    except NodeSizeError as error:
        status("warning", f"Retail prices unavailable: {error}. Compare costs before selecting.")
        prices = {}
    if any(size.name not in prices for size in eligible.values()):
        status(
            "warning", "Some prices are unavailable; unpriced choices are ranked by CPU and RAM."
        )
    choices = sorted(
        eligible.values(),
        key=lambda size: (prices.get(size.name, math.inf), size.cpus, size.memory, size.name),
    )[:3]
    status("section", "AKS node size: choose an available option")
    print(
        f"  Region: {config.location}\n"
        f"  Full demo: {budget.clusters} clusters x {budget.nodes} nodes; "
        "one surge node per cluster reserved.\n\n"
        "  #  Size                         vCPUs  RAM GiB   USD/VM-hour  USD/demo-hour",
        file=sys.stderr,
    )
    for number, size in enumerate(choices, 1):
        price = prices.get(size.name)
        hourly = f"{price:.3f}" if price is not None else "unavailable"
        fleet = f"{price * budget.fleet_nodes:.3f}" if price is not None else "unavailable"
        print(
            f"  {number}  {size.name:<28} {size.cpus:>5} {size.memory:>8g} "
            f"{hourly:>13} {fleet:>14}",
            file=sys.stderr,
        )
    print(
        "\n  Prices are Linux retail compute estimates; disks and other services are extra.\n"
        "  Choices use regional node pools and managed OS disks.\n"
        "  Availability is not a capacity reservation.\n",
        file=sys.stderr,
    )
    return choices[read_selection(len(choices))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--existing-foundation", type=Path)
    args = parser.parse_args()
    try:
        if os.environ.get("CONFIRM_AZURE") != "yes":
            raise NodeSizeError("Node-size selection requires CONFIRM_AZURE=yes")
        config = load_config(args.config)
        if config.environment != "azure":
            raise NodeSizeError("Node-size selection requires an Azure configuration")
        template = json.loads(args.template.read_text())
        if not isinstance(template, dict):
            raise NodeSizeError("The compiled foundation is not an object")
        budget = Budget.from_template(template)
        if config.node_count is not None:
            budget = Budget(budget.clusters, config.node_count)
        existing = None
        if args.existing_foundation:
            document = json.loads(args.existing_foundation.read_text())
            foundation = document.get("foundation", {}) if isinstance(document, dict) else {}
            if not isinstance(foundation, dict):
                raise NodeSizeError("Existing foundation is not an object")
            if any(
                foundation.get(key) != value
                for key, value in {
                    "projectName": config.project,
                    "deploymentName": config.deployment,
                    "subscriptionId": config.subscription,
                    "environment": "azure",
                }.items()
            ):
                raise NodeSizeError("Existing foundation does not match the selected deployment")
            existing = foundation.get("nodeVmSize")
            if not isinstance(existing, str) or foundation.get("nodeCount") != budget.nodes:
                raise NodeSizeError("Existing foundation has missing or different node capacity")
        discovery = Discovery(config)
        slots = ("management", *template["parameters"]["childSlots"]["defaultValue"])
        eligible, problems = discovery.available(budget, slots=slots)
        size = select_size(eligible, problems, config, budget, discovery, existing=existing)
        initialize_config(replace(config, node_vm_size=size.name), args.config, expected=config)
        status("success", f"AKS node size: saved {size.name} in .env")
        print(json.dumps({"nodeVmSize": size.name, "nodeCount": budget.nodes}))
        return 0
    except SelectionCancelled:
        status("warning", "Node-size selection cancelled; no foundation deployment was submitted")
        return 130
    except KeyboardInterrupt:
        status("warning", "Node-size selection interrupted; no foundation deployment was submitted")
        return 130
    except (NodeSizeError, ConfigError, OSError, ValueError) as error:
        status("error", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
