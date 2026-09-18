"""Check the configured grants of verified Radius identities, without changing access."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from plane_demo.management.providers.identity import (  # noqa: E402
    AZURE_DEFAULT_SLOTS,
    AZURE_ENVIRONMENT_MODE,
    AZURE_GROUP_LAYOUT,
    IDENTITY_PURPOSES,
    SLOTS,
    DemoConfig,
    azure_slot,
)

POLICY = json.loads(Path(__file__).with_name("plane-policy.json").read_text())
GUID_NAMESPACE = UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830")
BUILTINS = {
    "reader": "acdd72a7-3385-48ef-bd42-f606fba81ae7",
    "network": "4d97b98b-1d4f-4787-a291-c67834d212e7",
    "dns": "b12aa53e-6015-4669-85d0-8515ebb3ae7f",
    "identityOperator": "f1a07417-d97a-45cb-824c-7a7467783830",
    "repositoryReader": "b93aa761-3e63-49ed-ac28-beffa264f7ac",
}
ROLE_NAMES = {
    "certificateImporter": ("certificate-importer", "radplanes certificate importer"),
    "acmeStateWriter": ("acme-state-writer", "radplanes ACME state writer"),
    "childClusterRecipe": ("child-cluster-recipe", "radplanes child cluster recipe"),
    "childIdentityFederation": ("child-identity-federation", "radplanes child identity federation"),
    **{
        key: (value["purpose"], "radplanes " + value["name"])
        for key, value in POLICY["roles"].items()
    },
}


class PolicyError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise PolicyError(message)


def role_id(subscription, stem, purpose):
    return (
        f"/subscriptions/{subscription}/providers/Microsoft.Authorization/roleDefinitions/"
        + str(uuid5(GUID_NAMESPACE, f"/subscriptions/{subscription}-{stem}-{purpose}"))
    )


def application_actions(key):
    return POLICY["deploymentActions"] + POLICY["gatewayActions"] + POLICY["roles"][key]["actions"]


def role_slots(key, slots=SLOTS):
    if key in POLICY["roles"]:
        return tuple(
            slot for slot in slots if slot.endswith("-data") == (key == "redisApplication")
        )
    return tuple(slot for slot in slots if slot != "management")


def expected_grants(config, document):
    """Return exact Radius principal/role/scope tuples and custom role contracts."""
    foundation = document["foundation"]
    require(foundation.get("resourceGroupLayout") == AZURE_GROUP_LAYOUT, "Unsupported group layout")
    require(
        all(
            foundation.get(key) == value
            for key, value in {
                "projectName": config.project,
                "deploymentName": config.deployment,
                "environment": "azure",
                "subscriptionId": config.subscription,
                "resourcePrefix": config.stem,
            }.items()
        ),
        "Foundation selection differs",
    )
    allocations = document["allocations"]
    require(
        isinstance(allocations, list)
        and 3 <= len(allocations) <= 15
        and all(isinstance(item, dict) and azure_slot(item.get("slot")) for item in allocations)
        and len({item["slot"] for item in allocations}) == len(allocations)
        and set(AZURE_DEFAULT_SLOTS) <= {item["slot"] for item in allocations},
        "Invalid plane allocations",
    )
    allocations = {item["slot"]: item for item in allocations}
    slots = tuple(allocations)
    pairs = {slot.removesuffix("-control") for slot in slots if slot.endswith("-control")}
    require(
        set(slots)
        == {"management", *(f"{pair}-{role}" for pair in pairs for role in ("control", "data"))},
        "Incomplete plane pair",
    )
    principals, expected, definitions = {}, set(), {}
    prefix = (
        f"/subscriptions/{config.subscription}/providers/Microsoft.Authorization/roleDefinitions/"
    )
    for slot, allocation in allocations.items():
        require(
            allocation["clusterResourceGroup"] == config.plane_group(slot)
            and allocation["appResourceGroup"] == config.plane_group(slot),
            "Allocated group differs",
        )
        require(
            set(allocation["identities"]) == set(IDENTITY_PURPOSES), "Invalid identity purposes"
        )
        for key, purpose in IDENTITY_PURPOSES.items():
            identity = allocation["identities"][key]
            require(
                identity["id"] == config.managed_identity_id(slot, purpose),
                "Identity scope differs",
            )
            UUID(identity["principalId"])
        principals[slot] = allocation["identities"]["radius"]["principalId"].lower()
    require(len(set(principals.values())) == len(slots), "Radius identities must be distinct")
    platform = f"/subscriptions/{config.subscription}/resourceGroups/rg-{config.stem}-platform"
    vnet = platform + f"/providers/Microsoft.Network/virtualNetworks/vnet-{config.stem}"
    registry = (
        platform + f"/providers/Microsoft.ContainerRegistry/registries/{config.registry_name}"
    )

    def grant(slot, role, scope):
        expected.add(
            (
                principals[slot],
                (prefix + BUILTINS[role] if role in BUILTINS else role).lower(),
                scope.lower(),
            )
        )

    domains = {}
    prepared = foundation.get("environmentMode") == AZURE_ENVIRONMENT_MODE
    for slot, allocation in allocations.items():
        domain = (
            config.stem
            if slot in AZURE_DEFAULT_SLOTS or not prepared
            else f"{config.stem}-{slot.rsplit('-', 1)[0]}"
        )
        require(
            allocation.get("roleDefinitionPrefix", config.stem) == domain,
            "Custom role scope does not match its environment",
        )
        domains.setdefault(domain, []).append(slot)
    selected_roles = {}
    for domain, members in domains.items():
        selected_roles[domain] = {}
        for key in (*POLICY["roles"], "childClusterRecipe", "childIdentityFederation"):
            identifier = role_id(config.subscription, domain, ROLE_NAMES[key][0])
            selected_roles[domain][key] = identifier
            if domain == config.stem:
                require(
                    foundation["roleDefinitionIds"].get(key) == identifier,
                    "Custom role identity differs",
                )
            actions = (
                application_actions(key)
                if key in POLICY["roles"]
                else POLICY["deploymentActions"] + POLICY["clusterActions"]
                if key == "childClusterRecipe"
                else [
                    "Microsoft.ManagedIdentity/userAssignedIdentities/read",
                    *[
                        "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/"
                        + action
                        for action in ("read", "write", "delete")
                    ],
                ]
            )
            definitions[identifier.lower()] = {
                "roleName": domain + ROLE_NAMES[key][1][len("radplanes") :],
                "assignableScopes": [
                    config.plane_group_id(slot) for slot in role_slots(key, members)
                ],
                "permissions": [
                    {"actions": actions, "notActions": [], "dataActions": [], "notDataActions": []}
                ],
            }
    for slot in slots:
        domain = allocations[slot].get("roleDefinitionPrefix", config.stem)
        role_ids = selected_roles[domain]
        role = "redisApplication" if slot.endswith("-data") else "postgresApplication"
        grant(slot, role_ids[role], config.plane_group_id(slot))
        grant(slot, "reader", vnet)
        grant(slot, "repositoryReader", registry)
        grant(slot, "network", vnet + f"/subnets/snet-{slot}-gateway")
        grant(slot, "identityOperator", config.managed_identity_id(slot, "gateway"))
        if slot.endswith("-data"):
            grant(slot, "network", vnet + f"/subnets/snet-{slot}-endpoints")
            zone = "privatelink.redis.azure.net"
        else:
            grant(slot, "network", vnet + f"/subnets/snet-{slot}-postgresql")
            zone = f"{config.stem}.postgres.database.azure.com"
        grant(slot, "dns", platform + "/providers/Microsoft.Network/privateDnsZones/" + zone)
        grant("management", "network", vnet + f"/subnets/snet-{slot}-nodes")
        if slot != "management":
            grant(
                "management",
                role_ids["childClusterRecipe"],
                config.plane_group_id(slot),
            )
            for purpose in ("control-plane", "kubelet"):
                grant("management", "identityOperator", config.managed_identity_id(slot, purpose))
            for purpose in ("radius", "certificate-issuer"):
                grant(
                    "management",
                    role_ids["childIdentityFederation"],
                    config.managed_identity_id(slot, purpose),
                )
    return set(principals.values()), expected, definitions


class Verifier:
    def __init__(self, config, *, runner=subprocess.run):
        self.config, self.runner = config, runner

    def get(self, url):
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.hostname in {"management.azure.com", "graph.microsoft.com"}
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.fragment,
            "Invalid authorization discovery URL",
        )
        result = self.runner(
            [
                "az",
                "rest",
                "--method",
                "get",
                "--url",
                url,
                "--subscription",
                self.config.subscription,
                "--output",
                "json",
                "--only-show-errors",
            ],
            stdout=subprocess.PIPE,
            text=True,
            check=False,
            timeout=180,
        )
        require(result.returncode == 0, "Grant verification incomplete; Azure/Graph read failed")
        value = json.loads(result.stdout)
        require(isinstance(value, dict), "Grant verification returned an invalid object")
        return value

    def pages(self, url):
        result, seen = [], set()
        origin = urlsplit(url)
        while url:
            require(url not in seen and len(seen) < 1000, "Invalid authorization pagination")
            require(
                urlsplit(url).hostname == origin.hostname, "Authorization pagination changed host"
            )
            seen.add(url)
            page = self.get(url)
            require(isinstance(page.get("value"), list), "Authorization page is incomplete")
            require(
                all(isinstance(item, dict) for item in page["value"]), "Invalid authorization entry"
            )
            result.extend(page["value"])
            url = page.get("nextLink") or page.get("@odata.nextLink")
            require(url is None or isinstance(url, str), "Invalid next page")
        return result

    def verify(self, document, *, allow_missing=False):
        principals, expected, contracts = expected_grants(self.config, document)
        members = set(principals)
        for principal in principals:
            groups = self.pages(
                f"https://graph.microsoft.com/v1.0/servicePrincipals/{principal}/transitiveMemberOf"
            )
            for group in groups:
                require(
                    group.get("@odata.type") == "#microsoft.graph.group",
                    "Unexpected or incomplete Radius directory membership",
                )
                members.add(str(UUID(group["id"])))
        scope = f"https://management.azure.com/subscriptions/{self.config.subscription}"
        path = "/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01"
        assignments = self.pages(scope + path)
        assignments += self.pages(scope + path + "&$filter=atScope()")
        found, definitions, seen = set(), {}, {}
        for assignment in assignments:
            props = assignment["properties"]
            if str(props.get("principalId", "")).lower() not in members:
                continue
            identifier = assignment["id"].lower()
            if identifier in seen:
                require(seen[identifier] == props, "Role assignment changed during verification")
                continue
            seen[identifier] = props
            selected = tuple(
                str(props[key]).lower() for key in ("principalId", "roleDefinitionId", "scope")
            )
            require(
                selected in expected
                and not props.get("condition")
                and not props.get("delegatedManagedIdentityResourceId"),
                f"Unexpected Radius grant at {selected[2]} for {selected[0]} ({selected[1]})",
            )
            role = selected[1]
            if role not in definitions:
                definition = self.get(
                    "https://management.azure.com" + role + "?api-version=2022-04-01"
                )
                require(
                    str(definition.get("id", "")).lower() == role,
                    "Role definition identity differs",
                )
                definition = definition["properties"]
                if role in contracts:
                    contract = contracts[role]
                    require(
                        definition.get("type") == "CustomRole"
                        and definition.get("roleName") == contract["roleName"]
                        and {v.lower() for v in definition["assignableScopes"]}
                        == {v.lower() for v in contract["assignableScopes"]}
                        and definition["permissions"] == contract["permissions"],
                        f"Radius custom role permissions differ: {role}",
                    )
                else:
                    require(
                        role.rsplit("/", 1)[-1] in BUILTINS.values()
                        and definition.get("type") == "BuiltInRole",
                        "Unexpected built-in role definition",
                    )
                definitions[role] = definition
            found.add(selected)
        require(
            allow_missing or found == expected,
            "Required Radius grants are missing; wait for propagation and retry",
        )
        return {
            "status": "conformant",
            "layout": AZURE_GROUP_LAYOUT,
            "checkedAt": datetime.now(UTC).isoformat(),
            "radiusIdentities": len(principals),
            "assignments": len(found),
            "missingAssignments": len(expected - found),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--foundation", type=Path, required=True)
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args()
    try:
        document = json.loads(args.foundation.read_text())
        foundation = document["foundation"]
        config = DemoConfig(
            "azure",
            foundation["projectName"],
            foundation["deploymentName"],
            foundation["subscriptionId"],
            foundation["location"],
        )
        print(json.dumps(Verifier(config).verify(document, allow_missing=args.allow_missing)))
        return 0
    except (
        PolicyError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        subprocess.TimeoutExpired,
    ) as error:
        print(
            f"ERROR: Radius grant verification incomplete or nonconformant: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
