#!/usr/bin/env python3
"""Discover regional service support and select the remaining Azure resource sizes."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from plane_demo.management.providers.identity import RESOURCE_SIZING  # noqa: E402
from scripts.operations.azure.compute_selection import (  # noqa: E402
    Discovery as AzureDiscovery,
)
from scripts.operations.azure.compute_selection import (  # noqa: E402
    SelectionCancelled,
    SelectionError,
    read_selection,
)
from scripts.operations.azure.postgres_sizes import restriction  # noqa: E402
from scripts.operations.config import ConfigError, initialize_config, load_config  # noqa: E402
from scripts.operations.output import status  # noqa: E402

REDIS_MEMORY = {
    "Balanced_B0": 0.5,
    "Balanced_B1": 1,
    "Balanced_B3": 3,
    "Balanced_B5": 6,
    "Balanced_B10": 12,
    "Balanced_B20": 24,
}
LABELS = {
    "node_count": "AKS nodes per cluster",
    "aks_tier": "AKS control-plane tier",
    "node_os_disk_gb": "AKS managed OS disk GiB",
    "postgres_storage_gb": "PostgreSQL storage GiB",
    "redis_sku_name": "Managed Redis size",
    "gateway_capacity": "Application Gateway instances per plane",
    "registry_sku": "Container Registry tier",
    "key_vault_sku": "Key Vault tier",
}
REGIONAL_TYPES = {
    "Microsoft.ContainerService": ("managedClusters",),
    "Microsoft.DBforPostgreSQL": ("flexibleServers",),
    "Microsoft.Cache": ("redisEnterprise",),
    "Microsoft.Network": ("applicationGateways", "natGateways", "publicIPAddresses"),
    "Microsoft.ContainerRegistry": ("registries",),
    "Microsoft.KeyVault": ("vaults",),
}


def require(condition, message):
    if not condition:
        raise SelectionError(message)


def normalized_location(value):
    require(isinstance(value, str), "Resource provider returned an invalid location")
    return "".join(character for character in value.casefold() if character.isalnum())


def redis_offers(items, location):
    result = {}
    for item in items:
        if (
            item.get("productName") != "Azure Managed Redis - Balanced"
            or item.get("armRegionName") != location
            or item.get("type") != "Consumption"
            or item.get("unitOfMeasure") != "1 Hour"
            or item.get("currencyCode") != "USD"
            or item.get("isPrimaryMeterRegion") is False
        ):
            continue
        name = "Balanced_" + str(item.get("skuName", ""))
        if name not in REDIS_MEMORY:
            continue
        require(
            item.get("armSkuName") == "Azure_Managed_Redis_" + name,
            "Managed Redis catalog SKU identity differs",
        )
        price = item.get("retailPrice")
        require(
            type(price) in (int, float) and math.isfinite(price) and price > 0,
            "Managed Redis catalog price is invalid",
        )
        require(
            name not in result or result[name] == price, "Ambiguous Managed Redis catalog offer"
        )
        result[name] = price
    require(result, f"No compatible Managed Redis offers are published for {location}")
    return result


def storage_sizes(capabilities):
    require(
        len(capabilities) == 1 and not restriction(capabilities[0]),
        "PostgreSQL storage capabilities are unavailable",
    )
    editions = capabilities[0].get("supportedServerEditions")
    require(isinstance(editions, list), "PostgreSQL storage editions are missing")
    selected = [
        edition
        for edition in editions
        if isinstance(edition, dict)
        and edition.get("name") == "GeneralPurpose"
        and not restriction(edition)
    ]
    require(len(selected) == 1, "PostgreSQL GeneralPurpose storage is unavailable")
    storage = selected[0].get("supportedStorageEditions")
    require(isinstance(storage, list), "PostgreSQL storage capabilities are missing")
    sizes = set()
    for edition in storage:
        require(isinstance(edition, dict), "Invalid PostgreSQL storage edition")
        if edition.get("name") != "ManagedDisk" or restriction(edition):
            continue
        entries = edition.get("supportedStorageMb")
        require(isinstance(entries, list), "PostgreSQL managed storage sizes are missing")
        for item in entries:
            require(isinstance(item, dict), "Invalid PostgreSQL storage size")
            if not restriction(item):
                size = item.get("storageSizeMb")
                require(type(size) is int and size > 0, "Invalid PostgreSQL storage size")
                if size % 1024 == 0 and size // 1024 in RESOURCE_SIZING["postgres_storage_gb"][2]:
                    sizes.add(size // 1024)
    require(sizes, "No compatible PostgreSQL managed storage sizes are advertised")
    return sorted(sizes)


class Discovery(AzureDiscovery):
    def available(self):
        for namespace, kinds in REGIONAL_TYPES.items():
            provider = self.command(
                [
                    "az",
                    "provider",
                    "show",
                    "--namespace",
                    namespace,
                    "--subscription",
                    self.config.subscription,
                    "--output",
                    "json",
                ],
                f"Service availability: {namespace}",
            )
            require(
                isinstance(provider, dict)
                and provider.get("namespace") == namespace
                and provider.get("registrationState") in {"Registered", "Registering"}
                and isinstance(provider.get("resourceTypes"), list),
                f"{namespace}: provider capability metadata is missing",
            )
            for kind in kinds:
                records = [
                    item
                    for item in provider["resourceTypes"]
                    if isinstance(item, dict)
                    and str(item.get("resourceType", "")).casefold() == kind.casefold()
                ]
                require(len(records) == 1, f"{namespace}/{kind}: capability metadata is missing")
                locations = records[0].get("locations")
                require(isinstance(locations, list), f"{namespace}/{kind}: locations are missing")
                require(
                    self.config.location in {normalized_location(item) for item in locations},
                    f"{namespace}/{kind} is not advertised in {self.config.location}",
                )
        capabilities = self.az(
            "postgres",
            "flexible-server",
            "list-skus",
            "--location",
            self.config.location,
        )
        offers = redis_offers(
            self.retail_items(
                "serviceName eq 'Redis Cache' and priceType eq 'Consumption' and "
                f"armRegionName eq '{self.config.location}'",
                "Redis: published regional offers",
            ),
            self.config.location,
        )
        choices = {name: list(values[2]) for name, values in RESOURCE_SIZING.items()}
        choices["postgres_storage_gb"] = storage_sizes(capabilities)
        choices["redis_sku_name"] = sorted(offers, key=lambda name: (offers[name], name))
        if self.config.key_vault:
            vault = self.command(
                [
                    "az",
                    "keyvault",
                    "show",
                    "--name",
                    self.config.key_vault,
                    "--subscription",
                    self.config.subscription,
                    "--output",
                    "json",
                ],
                "Key Vault: retain existing tier",
            )
            tier = (
                vault.get("properties", {}).get("sku", {}).get("name")
                if isinstance(vault, dict)
                else None
            )
            require(tier in choices["key_vault_sku"], "Existing vault tier is unsupported")
            require(
                self.config.key_vault_sku in (None, tier),
                "The selected tier differs from the external vault; resizing is not permitted",
            )
            choices["key_vault_sku"] = [tier]
        return choices, offers


def select(config, choices, offers, *, existing=None):
    selected = {}
    for name in LABELS:
        _, field, _ = RESOURCE_SIZING[name]
        saved = getattr(config, name)
        requested = existing[field] if existing is not None else saved
        if existing is not None and saved is not None:
            require(
                saved == requested,
                f"{LABELS[name]} differs from the foundation; no automatic resize",
            )
        options = choices[name]
        if requested is not None:
            if requested in options:
                selected[name] = requested
                status("success", f"{LABELS[name]}: retain {requested}")
                continue
            require(
                existing is None, f"{LABELS[name]} is no longer advertised; no automatic resize"
            )
            status("warning", f"{LABELS[name]}: saved choice is not currently advertised")
        if name == "key_vault_sku" and config.key_vault:
            selected[name] = options[0]
            status("success", f"External Key Vault: retain {options[0]}")
            continue
        status("section", f"Resource sizing: {LABELS[name]}")
        if name == "redis_sku_name":
            print(
                "  Regional retail offers, not live capacity or subscription quota.\n"
                "  NoCluster-compatible Balanced sizes only; HA remains disabled.\n",
                file=sys.stderr,
            )
        else:
            print(
                "  Supported demo choices; regional service support was checked.\n", file=sys.stderr
            )
        for index, value in enumerate(options, 1):
            description = (
                f"{value}  {REDIS_MEMORY[value]:g} GB  catalog USD {offers[value]:.3f}/unit-hour"
                if name == "redis_sku_name"
                else str(value)
            )
            print(f"  {index}  {description}", file=sys.stderr)
        print(
            "\n  Select a size explicitly; q cancels without saving these choices.\n",
            file=sys.stderr,
        )
        selected[name] = options[read_selection(len(options))]
    status("section", "Resource sizing: fixed compatibility requirements")
    print(
        "  Application Gateway tier       Standard_v2 (no WAF policy configured)\n"
        "  Public IP, NAT and load balancer Standard\n"
        "  AKS OS disks                    Managed\n"
        "  Redis                           NoCluster, TLS, HA disabled\n"
        "  DNS, identities, RBAC, endpoints No selectable compute size\n",
        file=sys.stderr,
    )
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--existing-foundation", type=Path)
    args = parser.parse_args()
    try:
        require(
            os.environ.get("CONFIRM_AZURE") == "yes", "Resource sizing requires CONFIRM_AZURE=yes"
        )
        config = load_config(args.config)
        require(config.environment == "azure", "Resource sizing requires Azure")
        existing = None
        if args.existing_foundation:
            document = json.loads(args.existing_foundation.read_text())
            existing = document.get("foundation") if isinstance(document, dict) else None
            require(
                isinstance(existing, dict)
                and all(
                    existing.get(key) == value
                    for key, value in {
                        "projectName": config.project,
                        "deploymentName": config.deployment,
                        "subscriptionId": config.subscription,
                        "location": config.location,
                    }.items()
                ),
                "Existing foundation differs from the selected deployment",
            )
            require(
                type(existing.get("resourceSizingVersion")) is int
                and existing["resourceSizingVersion"] == 1
                and all(
                    type(existing.get(field)) is type(values[0]) and existing[field] in values
                    for _, field, values in RESOURCE_SIZING.values()
                ),
                "Existing foundation has no complete sizing profile; use a fresh deployment",
            )
        choices, offers = Discovery(config).available()
        values = select(config, choices, offers, existing=existing)
        selected = replace(config, **values)
        if selected != config:
            initialize_config(selected, args.config, expected=config)
        else:
            require(
                load_config(args.config) == config,
                ".env changed while selecting configuration; rerun bootstrap",
            )
        print(
            json.dumps(
                {
                    "parameters": selected.resource_sizes,
                    "environment": {
                        key: str(getattr(selected, name))
                        for name, (key, _, _) in RESOURCE_SIZING.items()
                    },
                }
            )
        )
        return 0
    except (SelectionCancelled, KeyboardInterrupt):
        status("warning", "Resource sizing cancelled; no foundation was submitted")
        return 130
    except (SelectionError, ConfigError, ValueError, KeyError, OSError) as error:
        status("error", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
