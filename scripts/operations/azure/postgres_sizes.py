#!/usr/bin/env python3
"""Select PostgreSQL compute before creating the Azure foundation."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from scripts.operations.azure.compute_selection import (  # noqa: E402
    Discovery as ComputeDiscovery,
)
from scripts.operations.azure.compute_selection import (  # noqa: E402
    SelectionCancelled,
    integer,
    read_selection,
)
from scripts.operations.azure.compute_selection import (  # noqa: E402
    SelectionError as PostgresSizeError,
)
from scripts.operations.config import (  # noqa: E402
    ConfigError,
    initialize_config,
    load_config,
)
from scripts.operations.output import status  # noqa: E402


@dataclass(frozen=True)
class Size:
    name: str
    tier: str
    cpus: int
    memory: float
    zones: tuple[str, ...]


def restriction(record):
    restricted = record.get("restricted")
    if restricted is not None and restricted is not False and restricted != "Disabled":
        return "restricted by Azure"
    if record.get("status") not in (None, "Available", "Enabled"):
        return f"Azure status: {record['status']}"
    if record.get("reason"):
        return str(record["reason"])
    return None


def available_sizes(records):
    if len(records) != 1:
        raise PostgresSizeError("Expected one regional PostgreSQL capability record")
    capabilities = records[0]
    if problem := restriction(capabilities):
        raise PostgresSizeError(f"PostgreSQL provisioning is unavailable: {problem}")
    features = capabilities.get("supportedFeatures", [])
    if not isinstance(features, list) or any(not isinstance(item, dict) for item in features):
        raise PostgresSizeError("PostgreSQL capability features are invalid")
    if any(
        item.get("name") == "OfferRestricted" and item.get("status") != "Disabled"
        for item in features
    ):
        raise PostgresSizeError("The subscription offer is restricted for PostgreSQL")
    versions = capabilities.get("supportedServerVersions")
    if not isinstance(versions, list) or any(not isinstance(item, dict) for item in versions):
        raise PostgresSizeError("PostgreSQL version capabilities are missing or invalid")
    supported = [item for item in versions if item.get("name") == "16"]
    if len(supported) != 1 or restriction(supported[0]):
        raise PostgresSizeError("PostgreSQL 16 is not advertised as available in this region")
    editions = capabilities.get("supportedServerEditions")
    if not isinstance(editions, list) or any(not isinstance(item, dict) for item in editions):
        raise PostgresSizeError("PostgreSQL edition capabilities are missing or invalid")
    matching = [edition for edition in editions if edition.get("name") == "GeneralPurpose"]
    if len(matching) != 1:
        raise PostgresSizeError("Expected one PostgreSQL GeneralPurpose capability record")
    edition = matching[0]
    if problem := restriction(edition):
        raise PostgresSizeError(f"PostgreSQL GeneralPurpose is unavailable: {problem}")
    skus = edition.get("supportedServerSkus")
    if not isinstance(skus, list) or any(not isinstance(item, dict) for item in skus):
        raise PostgresSizeError("PostgreSQL compute capabilities are missing or invalid")
    eligible, problems = {}, {}
    for record in skus:
        name = record.get("name")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"Standard_[A-Za-z0-9_]{1,64}", name)
            or name.lower() in problems
        ):
            raise PostgresSizeError("Invalid or duplicate PostgreSQL SKU name")
        problem = restriction(record)
        if not problem:
            try:
                cpus = integer(record.get("vCores"), "PostgreSQL vCores")
                memory_mb = integer(
                    record.get("supportedMemoryPerVcoreMb"), "PostgreSQL memory per vCore"
                )
                memory = cpus * memory_mb / 1024
            except PostgresSizeError:
                problem = "CPU or memory capabilities are invalid"
            else:
                if not 2 <= cpus <= 8 or not math.isfinite(memory) or not 8 <= memory <= 64:
                    problem = "demo choices require 2-8 vCores and 8-64 GiB RAM"
        zones = record.get("supportedZones")
        if not isinstance(zones, list) or any(
            not isinstance(zone, str) or not re.fullmatch(r"[1-9][0-9]*", zone) for zone in zones
        ):
            problem = problem or "supported availability zones are missing or invalid"
        problems[name.lower()] = problem
        if not problem:
            eligible[name.lower()] = Size(
                name, "GeneralPurpose", cpus, memory, tuple(sorted(set(zones)))
            )
    return eligible, problems


class Discovery(ComputeDiscovery):
    def available(self):
        return available_sizes(
            self.az("postgres", "flexible-server", "list-skus", "--location", self.config.location)
        )


def existing_selection(path, config):
    document = json.loads(path.read_text())
    foundation = document.get("foundation") if isinstance(document, dict) else None
    if not isinstance(foundation, dict) or any(
        foundation.get(key) != value
        for key, value in {
            "projectName": config.project,
            "deploymentName": config.deployment,
            "subscriptionId": config.subscription,
            "location": config.location,
            "environment": "azure",
        }.items()
    ):
        raise PostgresSizeError("Existing foundation does not match the selected deployment")
    name, tier = foundation.get("postgresSkuName"), foundation.get("postgresSkuTier")
    if not isinstance(name, str) or not isinstance(tier, str):
        raise PostgresSizeError(
            "Existing foundation has no PostgreSQL compute selection; use a fresh deployment name"
        )
    return name, tier


def select_size(eligible, problems, config, *, existing=None):
    saved = (config.postgres_sku_name, config.postgres_sku_tier)
    if existing and saved[0] and existing != saved:
        raise PostgresSizeError(
            "Saved PostgreSQL compute differs from the existing foundation; no automatic resize"
        )
    name, tier = existing or saved
    if name:
        selected = eligible.get(name.lower())
        if selected and selected.tier == tier:
            status("success", f"PostgreSQL compute: {selected.name}, {selected.tier}")
            return selected
        reason = problems.get(name.lower()) or "not an eligible regional PostgreSQL SKU"
        if existing:
            raise PostgresSizeError(
                f"Existing PostgreSQL SKU {name}: {reason}; no automatic resize"
            )
        status("warning", f"Saved PostgreSQL SKU {name}: {reason}")
    if not eligible:
        raise PostgresSizeError(
            f"No eligible PostgreSQL 16 GeneralPurpose SKUs are advertised in {config.location}"
        )
    choices = sorted(eligible.values(), key=lambda size: (size.cpus, size.memory, size.name))[:3]
    status("section", "PostgreSQL compute: choose an available option")
    print(
        f"  Region: {config.location}\n"
        "  Used by management and prepared control databases; PostgreSQL 16.\n"
        "  Storage follows the separate bootstrap resource sizing choice.\n\n"
        "  #  SKU                          Tier             vCores  RAM GiB  Zones",
        file=sys.stderr,
    )
    for number, size in enumerate(choices, 1):
        print(
            f"  {number}  {size.name:<28} {size.tier:<16} {size.cpus:>6} "
            f"{size.memory:>8g}  {','.join(size.zones) or 'regional'}"
            f"{'  (recommended)' if number == 1 else ''}",
            file=sys.stderr,
        )
    print(
        "\n  Recommended: the smallest eligible compute choice by vCores and RAM.\n"
        "  Ties are sorted by name, not price.\n"
        "  Compute and storage are billed separately; Azure chooses placement.\n"
        "  Advertised availability does not reserve capacity or quota.\n"
        "  SkuNotAvailable can still occur; no automatic SKU substitution is performed.\n",
        file=sys.stderr,
    )
    return choices[read_selection(len(choices))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--existing-foundation", type=Path)
    args = parser.parse_args()
    try:
        if os.environ.get("CONFIRM_AZURE") != "yes":
            raise PostgresSizeError("PostgreSQL compute selection requires CONFIRM_AZURE=yes")
        config = load_config(args.config)
        if config.environment != "azure":
            raise PostgresSizeError("PostgreSQL compute selection requires an Azure configuration")
        existing = (
            existing_selection(args.existing_foundation, config)
            if args.existing_foundation
            else None
        )
        eligible, problems = Discovery(config).available()
        selected = select_size(eligible, problems, config, existing=existing)
        initialize_config(
            replace(config, postgres_sku_name=selected.name, postgres_sku_tier=selected.tier),
            args.config,
            expected=config,
        )
        status("success", f"PostgreSQL compute: saved {selected.name}, {selected.tier} in .env")
        print(json.dumps({"postgresSkuName": selected.name, "postgresSkuTier": selected.tier}))
        return 0
    except (SelectionCancelled, KeyboardInterrupt):
        status("warning", "PostgreSQL selection cancelled; no foundation deployment was submitted")
        return 130
    except (PostgresSizeError, ConfigError, OSError, ValueError) as error:
        status("error", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
