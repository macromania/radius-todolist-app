"""Tag one proven Redis private-endpoint NIC, outside Radius Recipe tracking."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx

from plane_demo.management.provisioning import ProvisioningError

ARM = "https://management.azure.com"
TOKEN_FILE = "/var/run/secrets/azure/tokens/azure-identity-token"
BASE_TAGS = {
    "SecurityControl": "Ignore",
    "project": "radplanes",
    "managedBy": "radius-todolist-app",
}
ERRORS = frozenset(
    {
        "redis_nic_input_invalid",
        "redis_nic_identity_mismatch",
        "redis_nic_identity_unavailable",
        "redis_nic_arm_failed",
        "redis_nic_ownership_mismatch",
        "redis_nic_tag_conflict",
        "redis_nic_verification_failed",
        "redis_nic_timeout",
    }
)


def require(condition: bool, code: str = "redis_nic_ownership_mismatch") -> None:
    if not condition:
        raise ProvisioningError(code)


def same_id(actual, expected: str) -> bool:
    return isinstance(actual, str) and actual.casefold() == expected.casefold()


def tag_values(value) -> dict[str, str]:
    if value is None:
        return {}
    require(isinstance(value, dict))
    require(
        all(isinstance(key, str) and key and isinstance(item, str) for key, item in value.items())
    )
    folded = {key.casefold(): item for key, item in value.items()}
    require(len(folded) == len(value))
    return folded


@dataclass(frozen=True)
class Target:
    slot: str
    subscription_id: str
    tenant_id: str
    client_id: str
    resource_group: str
    subnet_id: str
    location: str
    resource_id: str
    environment_id: str
    application_id: str
    tags: dict[str, str]
    project_name: str = "radplanes"
    resource_prefix: str = "radplanes"

    @classmethod
    def parse(cls, value: dict) -> Target:
        try:
            require(isinstance(value, dict), "redis_nic_input_invalid")
            target = cls(**value)
            for identifier in (target.subscription_id, target.tenant_id, target.client_id):
                require(
                    isinstance(identifier, str) and str(UUID(identifier)) == identifier.lower(),
                    "redis_nic_input_invalid",
                )
            require(
                bool(re.fullmatch(r"[a-z][a-z0-9-]{0,47}", target.slot)), "redis_nic_input_invalid"
            )
            require(
                bool(re.fullmatch(r"[a-z][a-z0-9-]{0,15}", target.project_name))
                and bool(re.fullmatch(r"[a-z][a-z0-9-]{0,24}", target.resource_prefix)),
                "redis_nic_input_invalid",
            )
            require(
                (
                    target.project_name == "radplanes" and target.location == "centralus"
                    if target.resource_prefix == "radplanes"
                    else target.resource_prefix.startswith(target.project_name + "-")
                    and target.resource_prefix.endswith("-azure")
                    and bool(re.fullmatch(r"[a-z][a-z0-9]{1,31}", target.location))
                ),
                "redis_nic_input_invalid",
            )
            require(
                target.resource_group
                == f"rg-{target.resource_prefix}-{target.slot}"
                + ("-app" if target.resource_prefix == "radplanes" else ""),
                "redis_nic_input_invalid",
            )
            prefix = f"/planes/radius/local/resourceGroups/{target.resource_prefix}/providers/"
            for identifier, kind in (
                (target.resource_id, "Applications.Datastores/redisCaches"),
                (target.environment_id, "Applications.Core/environments"),
                (target.application_id, "Applications.Core/applications"),
            ):
                require(
                    isinstance(identifier, str)
                    and bool(
                        re.fullmatch(
                            re.escape(prefix + kind + "/") + r"[a-z][a-z0-9-]{0,62}",
                            identifier,
                            re.I,
                        )
                    ),
                    "redis_nic_input_invalid",
                )
            subnet_prefix = (
                f"/subscriptions/{target.subscription_id}"
                f"/resourceGroups/rg-{target.resource_prefix}-platform"
                "/providers/Microsoft.Network/virtualNetworks/"
            )
            require(
                isinstance(target.subnet_id, str)
                and bool(
                    re.fullmatch(
                        re.escape(subnet_prefix) + r"[a-zA-Z0-9_-]+/subnets/[a-zA-Z0-9_-]+",
                        target.subnet_id,
                        re.I,
                    )
                ),
                "redis_nic_input_invalid",
            )
            tags = tag_values(target.tags)
            require(
                all(
                    tags.get(key.casefold()) == item
                    for key, item in {**BASE_TAGS, "project": target.project_name}.items()
                ),
                "redis_nic_input_invalid",
            )
            require(
                not any(key.startswith("radapp.io-") for key in tags), "redis_nic_input_invalid"
            )
            return target
        except (TypeError, ValueError, AttributeError):
            raise ProvisioningError("redis_nic_input_invalid") from None

    @property
    def group_id(self) -> str:
        return f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"

    @property
    def required_tags(self) -> dict[str, str]:
        return {
            **self.tags,
            "radapp.io-resource": self.resource_id,
            "radapp.io-environment": self.environment_id,
            "radapp.io-application": self.application_id,
        }

    def owned_id(self, value, resource_type: str) -> str:
        require(
            isinstance(value, str)
            and bool(
                re.fullmatch(
                    re.escape(f"{self.group_id}/providers/{resource_type}/") + r"[a-zA-Z0-9_-]+",
                    value,
                    re.I,
                )
            )
        )
        return value


def validate_identity(target: Target, environment: Mapping[str, str]) -> None:
    expected = {
        "AZURE_CLIENT_ID": target.client_id,
        "AZURE_TENANT_ID": target.tenant_id,
        "AZURE_FEDERATED_TOKEN_FILE": TOKEN_FILE,
        "POD_NAMESPACE": "radius-system",
        "POD_SERVICE_ACCOUNT": "applications-rp",
    }
    require(
        all(environment.get(key) == value for key, value in expected.items()),
        "redis_nic_identity_mismatch",
    )
    require(
        environment.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com/")
        == "https://login.microsoftonline.com/",
        "redis_nic_identity_mismatch",
    )
    try:
        require(Path(TOKEN_FILE).is_file(), "redis_nic_identity_mismatch")
    except OSError:
        raise ProvisioningError("redis_nic_identity_mismatch") from None


class Arm:
    def __init__(
        self,
        client: httpx.Client,
        token: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.token = token
        self.clock = clock
        self.deadline = clock() + 60

    def request(self, method: str, path: str, version: str, body: dict | None = None) -> dict:
        remaining = self.deadline - self.clock()
        require(remaining > 0, "redis_nic_timeout")
        try:
            response = self.client.request(
                method,
                ARM + path,
                params={"api-version": version},
                headers={"Authorization": f"Bearer {self.token}"},
                json=body,
                timeout=httpx.Timeout(min(10, remaining), connect=min(5, remaining)),
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise ProvisioningError("redis_nic_timeout") from None
        except httpx.RequestError:
            raise ProvisioningError("redis_nic_arm_failed") from None
        require(self.clock() < self.deadline, "redis_nic_timeout")
        require(response.status_code in (200, 201), "redis_nic_arm_failed")
        try:
            data = response.json()
        except ValueError:
            raise ProvisioningError("redis_nic_arm_failed") from None
        require(isinstance(data, dict), "redis_nic_arm_failed")
        return data

    def get(self, path: str, version: str) -> dict:
        return self.request("GET", path, version)


def check_tags(resource: dict, expected: Mapping[str, str]) -> dict[str, str]:
    actual = tag_values(resource.get("tags"))
    for key, value in expected.items():
        found = actual.get(key.casefold())
        require(same_id(found, value) if key.startswith("radapp.io-") else found == value)
    return actual


def check_resource(resource: dict, identifier: str, kind: str, target: Target) -> dict:
    require(same_id(resource.get("id"), identifier) and same_id(resource.get("type"), kind))
    require(resource.get("name") == identifier.rsplit("/", 1)[1])
    # Managed Redis returns the display name; network resources return the region code.
    location = resource.get("location")
    require(isinstance(location, str) and location.replace(" ", "").casefold() == target.location)
    properties = resource.get("properties")
    require(isinstance(properties, dict) and properties.get("provisioningState") == "Succeeded")
    return properties


def network_properties(value):
    if isinstance(value, dict):
        return {key: network_properties(item) for key, item in value.items() if key != "etag"}
    if isinstance(value, list):
        return [network_properties(item) for item in value]
    return value


def tag_nic(target: Target, arm: Arm, *, pause: Callable[[float], None] = time.sleep) -> dict:
    group = arm.get(target.group_id, "2021-04-01")
    require(
        same_id(group.get("id"), target.group_id)
        and same_id(group.get("type"), "Microsoft.Resources/resourceGroups")
        and group.get("name") == target.resource_group
    )
    check_tags(group, target.tags)
    listing = arm.get(target.group_id + "/providers/Microsoft.Cache/redisEnterprise", "2025-07-01")
    require(not listing.get("nextLink"), "redis_nic_arm_failed")
    values = listing.get("value")
    require(isinstance(values, list) and all(isinstance(value, dict) for value in values))
    candidates = [
        value
        for value in values
        if same_id(tag_values(value.get("tags")).get("radapp.io-resource"), target.resource_id)
    ]
    require(len(candidates) == 1)
    cache_id = target.owned_id(candidates[0].get("id"), "Microsoft.Cache/redisEnterprise")
    cache = arm.get(cache_id, "2025-07-01")
    properties = check_resource(cache, cache_id, "Microsoft.Cache/redisEnterprise", target)
    require(properties.get("publicNetworkAccess") == "Disabled")
    cache_tags = check_tags(cache, target.required_tags)
    required = {key.casefold(): cache_tags[key.casefold()] for key in target.required_tags}
    cache_name = cache_id.rsplit("/", 1)[1]
    require(bool(re.fullmatch(r"amr-[a-z0-9]{13}", cache_name)))
    pe_id = f"{target.group_id}/providers/Microsoft.Network/privateEndpoints/pe-{cache_name}"
    pe = arm.get(pe_id, "2024-07-01")
    properties = check_resource(pe, pe_id, "Microsoft.Network/privateEndpoints", target)
    pe_tags = check_tags(pe, target.required_tags)
    require(all(pe_tags.get(key) == value for key, value in required.items()))
    require(
        isinstance(properties.get("subnet"), dict)
        and same_id(properties["subnet"].get("id"), target.subnet_id)
    )
    connections = properties.get("privateLinkServiceConnections")
    require(isinstance(connections, list) and len(connections) == 1)
    require(isinstance(connections[0], dict) and isinstance(connections[0].get("properties"), dict))
    connection = connections[0]["properties"]
    manual = properties.get("manualPrivateLinkServiceConnections")
    require(
        same_id(connection.get("privateLinkServiceId"), cache_id)
        and connection.get("groupIds") == ["redisEnterprise"]
        and (manual is None or manual == [])
    )
    state = connection.get("privateLinkServiceConnectionState")
    require(isinstance(state, dict) and state.get("status") == "Approved")
    nic_name = f"nic-{cache_name}"
    nic_id = f"{target.group_id}/providers/Microsoft.Network/networkInterfaces/{nic_name}"
    require(properties.get("customNetworkInterfaceName") == nic_name)
    interfaces = properties.get("networkInterfaces")
    require(
        isinstance(interfaces, list)
        and len(interfaces) == 1
        and isinstance(interfaces[0], dict)
        and same_id(interfaces[0].get("id"), nic_id)
    )

    def read_nic() -> tuple[dict, dict, dict]:
        nic = arm.get(nic_id, "2024-07-01")
        props = check_resource(nic, nic_id, "Microsoft.Network/networkInterfaces", target)
        require(
            isinstance(props.get("privateEndpoint"), dict)
            and same_id(props["privateEndpoint"].get("id"), pe_id)
        )
        return props, tag_values(nic.get("tags")), nic.get("tags") or {}

    original_properties, existing, original_tags = read_nic()
    require(
        all(key not in existing or existing[key] == value for key, value in required.items()),
        "redis_nic_tag_conflict",
    )
    result = {"cacheId": cache_id, "privateEndpointId": pe_id, "nicId": nic_id}
    if all(existing.get(key) == value for key, value in required.items()):
        return result
    properties, current_tags, _ = read_nic()
    require(
        current_tags == existing
        and network_properties(properties) == network_properties(original_properties),
        "redis_nic_tag_conflict",
    )
    merged = {key: value for key, value in original_tags.items() if key.casefold() not in required}
    merged.update(
        {key: value for key, value in cache["tags"].items() if key.casefold() in required}
    )
    arm.request(
        "PATCH",
        nic_id,
        "2024-07-01",
        {"tags": merged},
    )
    expected = {**existing, **required}
    for attempt in range(3):
        properties, verified, _ = read_nic()
        require(
            network_properties(properties) == network_properties(original_properties),
            "redis_nic_verification_failed",
        )
        if all(verified.get(key) == value for key, value in expected.items()):
            return result
        if attempt < 2:
            pause(2)
    raise ProvisioningError("redis_nic_verification_failed")


def workload_token(target: Target) -> str:
    from azure.core.exceptions import AzureError
    from azure.identity import WorkloadIdentityCredential

    try:
        with WorkloadIdentityCredential(
            tenant_id=target.tenant_id,
            client_id=target.client_id,
            token_file_path=TOKEN_FILE,
            connection_timeout=5,
            read_timeout=10,
            retry_total=0,
        ) as credential:
            return credential.get_token(ARM + "/.default").token
    except AzureError:
        raise ProvisioningError("redis_nic_identity_unavailable") from None


def main() -> int:
    try:
        try:
            raw = json.loads(os.environ.get("REDIS_NIC_TAG_TARGET", ""))
        except ValueError:
            raise ProvisioningError("redis_nic_input_invalid") from None
        target = Target.parse(raw)
        validate_identity(target, os.environ)
        token = workload_token(target)
        require(bool(token), "redis_nic_identity_unavailable")
        with httpx.Client(trust_env=False) as client:
            result = tag_nic(target, Arm(client, token))
    except ProvisioningError as error:
        logging.error("Redis NIC metadata failed: %s", error.code)
        Path("/dev/termination-log").write_text(json.dumps({"error_code": error.code}))
        return 1
    Path("/dev/termination-log").write_text(json.dumps(result))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
