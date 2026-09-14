"""Immutable placement and the short, non-resumable infrastructure sequence."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from plane_demo.management.providers.identity import PUBLIC_KEYS, DemoConfig
from plane_demo.shared.db import PendingOperation

SLUG = re.compile(r"[a-z][a-z0-9-]{0,47}")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")


class ProvisioningError(RuntimeError):
    """A stable public error code; detailed diagnostics belong in operator logs."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("invalid provisioning error code")
        self.code = code
        super().__init__(code)


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain(item) for item in value]
    return value


def endpoint(value: str, *, https: bool = True) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != ("https" if https else "http")
        or not re.fullmatch(r"[a-z0-9.-]+\.cloudapp\.azure\.com", parsed.hostname or "")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443 if https else 80)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisioningError("invalid_gateway_output")
    return value.rstrip("/")


def ipv4(value, label: str) -> IPv4Address:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an IPv4 address")
    try:
        return IPv4Address(value)
    except ValueError:
        raise ValueError(f"{label} must be an IPv4 address") from None


def ipv4_cidr(value, label: str) -> IPv4Network:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9.]+/[0-9]{1,2}", value):
        raise ValueError(f"{label} must be an IPv4 CIDR")
    try:
        return IPv4Network(value, strict=True)
    except ValueError:
        raise ValueError(f"{label} must be a canonical IPv4 CIDR") from None


def validate_network(foundation: dict, allocations: dict) -> None:
    ranges = foundation.get("authorizedIpRanges")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("authorizedIpRanges must contain explicit public IPv4 /32 entries")
    authorized = [ipv4_cidr(value, "authorizedIpRanges") for value in ranges]
    for network in authorized:
        address = network.network_address
        if network.prefixlen != 32 or not address.is_global or address.is_multicast:
            raise ValueError("authorizedIpRanges must contain explicit public IPv4 /32 entries")
    egress = ipv4(foundation.get("egressIp"), "egressIp")
    if IPv4Network(f"{egress}/32") not in authorized:
        raise ValueError("authorizedIpRanges must include the NAT egress IPv4 /32")
    used = set()
    for slot, allocation in allocations.items():
        gateway = ipv4_cidr(allocation.get("gatewaySubnetCidr"), "gatewaySubnetCidr")
        if gateway.prefixlen != 24 or not gateway.subnet_of(IPv4Network("10.64.16.0/20")):
            raise ValueError("gatewaySubnetCidr does not match the bootstrap address contract")
        index = gateway.network_address.packed[2] - 16
        if (slot == "management") != (index == 0) or index in used:
            raise ValueError(
                "slot subnet allocations must be distinct with management at index zero"
            )
        used.add(index)
        node = IPv4Network(f"10.64.{index}.0/24")
        if (
            "nodeSubnetCidr" in allocation
            and ipv4_cidr(allocation["nodeSubnetCidr"], "nodeSubnetCidr") != node
        ):
            raise ValueError("nodeSubnetCidr does not match the bootstrap address contract")
        for field, offset in (("apiPrivateIp", 240), ("challengePrivateIp", 241)):
            if ipv4(allocation.get(field), field) != node.network_address + offset:
                raise ValueError(f"{field} does not match the allocated node subnet address")


@dataclass(frozen=True)
class OperatorConfig:
    foundation: Mapping
    allocations: Mapping
    recipes: Mapping
    images: Mapping
    coordinator_identity: Mapping
    management_cluster: Mapping
    certificate_command: tuple[str, ...] = ()
    identity: DemoConfig | None = None

    @classmethod
    def load(cls, path: Path) -> OperatorConfig:
        return cls.from_dict(json.loads(path.read_text()))

    @classmethod
    def from_dict(cls, data: dict, *, identity: DemoConfig | None = None) -> OperatorConfig:
        if data.get("version") != 1:
            raise ValueError("provisioning config version must be 1")
        if "bootstrapIdentity" in data:
            if not isinstance(data["bootstrapIdentity"], dict) or (
                set(data["bootstrapIdentity"]) - PUBLIC_KEYS
            ):
                raise ValueError("bootstrap identity accepts public settings only")
            saved_identity = DemoConfig.from_values(data["bootstrapIdentity"])
            if identity is not None and saved_identity.public_values() != identity.public_values():
                raise ValueError("bootstrap identity mismatch")
            identity = identity or saved_identity
        foundation = data["foundation"]
        if identity is not None:
            if identity.environment != "azure" or any(
                foundation.get(key) != value
                for key, value in {
                    "projectName": identity.project,
                    "deploymentName": identity.deployment,
                    "environment": "azure",
                    "subscriptionId": identity.subscription,
                    "location": identity.location,
                    "resourcePrefix": identity.stem,
                    "radiusResourceGroup": identity.stem,
                }.items()
            ):
                raise ValueError("foundation does not match the selected deployment")
        elif foundation["projectName"] != "radplanes" or foundation["location"] != "centralus":
            raise ValueError("this deployment requires radplanes in centralus")
        for field in ("subscriptionId", "tenantId"):
            UUID(foundation[field])
        registry = foundation["registryName"]
        if (
            not re.fullmatch(r"[a-z0-9]{5,50}", registry)
            or foundation["registryLoginServer"] != f"{registry}.azurecr.io"
        ):
            raise ValueError("invalid registry")
        if identity is not None and (
            registry != identity.registry_name or foundation.get("vaultName") != identity.vault_name
        ):
            raise ValueError("foundation stores do not match the selected deployment")
        allocations = data["allocations"]
        if not isinstance(allocations, dict) or "management" not in allocations:
            raise ValueError("allocations must be a slot-keyed dictionary")
        prefix = f"/subscriptions/{foundation['subscriptionId']}/resourceGroups/"
        for slot, allocation in allocations.items():
            if (
                not SLUG.fullmatch(slot)
                or allocation["slot"] != slot
                or (slot != "management" and not slot.endswith(("-control", "-data")))
            ):
                raise ValueError("invalid allocation slot")
            for field in ("clusterName", "clusterResourceGroup", "appResourceGroup"):
                pattern = r"[a-z][a-z0-9-]{0,89}" if identity else SLUG.pattern
                if not re.fullmatch(pattern, allocation[field]):
                    raise ValueError("invalid allocated resource name")
            if identity is not None:
                name = identity.slot_name(slot)
                expected_names = {
                    "clusterName": f"aks-{name}",
                    "clusterResourceGroup": f"rg-{name}-cluster",
                    "appResourceGroup": f"rg-{name}-app",
                    "namespace": identity.namespace(slot),
                    "clusterResourceGroupId": prefix + f"rg-{name}-cluster",
                    "appResourceGroupId": prefix + f"rg-{name}-app",
                }
                if any(allocation.get(key) != value for key, value in expected_names.items()):
                    raise ValueError("allocation does not match the selected deployment")
            for field in (
                "nodeSubnetId",
                "gatewaySubnetId",
                "privateEndpointSubnetId",
                "postgresqlSubnetId",
                "clusterResourceGroupId",
                "appResourceGroupId",
            ):
                if not allocation[field].startswith(prefix):
                    raise ValueError("allocation is outside the configured subscription")
            for managed_identity in allocation["identities"].values():
                UUID(managed_identity["clientId"])
                if not managed_identity["id"].startswith(prefix):
                    raise ValueError("identity is outside the configured subscription")
            certificate_slot = f"{identity.stem}-{slot}" if identity else slot
            if (
                allocation.get("certificateName") != f"gateway-{certificate_slot}"
                or allocation.get("acmeStateSecretName") != f"acme-{certificate_slot}"
                or allocation.get("certificateIssuerSubject")
                != f"system:serviceaccount:{identity.stem if identity else 'radplanes'}-system:"
                "certificate-issuer"
                or "certificateIssuer" not in allocation["identities"]
            ):
                raise ValueError("certificate allocation does not match its prebound identity")
        validate_network(foundation, allocations)
        pairs = {slot.removesuffix("-control") for slot in allocations if slot.endswith("-control")}
        if "shared" not in pairs or set(allocations) != {
            "management",
            *(f"{pair}-{role}" for pair in pairs for role in ("control", "data")),
        }:
            raise ValueError("every pair requires exactly one control and data allocation")
        for pair in pairs:
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", pair):
                raise ValueError("invalid pair allocation")
        for name in ("cluster", "postgresql", "gateway", "redis"):
            recipe = data["recipes"][name]
            if not re.fullmatch(
                re.escape(f"{registry}.azurecr.io/") + r"[a-z0-9/_-]+:[a-zA-Z0-9_.-]+",
                recipe["reference"],
            ) or not DIGEST.fullmatch(recipe["digest"]):
                raise ValueError("recipes require a registry tag and expected digest")
        images = dict(data["images"])
        for name in ("api", "provisioner"):
            image = images[name]
            if isinstance(image, dict):
                image = image.get("reference")
            if not isinstance(image, str) or not re.fullmatch(
                re.escape(f"{registry}.azurecr.io/") + r"[a-z0-9/_-]+@sha256:[a-f0-9]{64}",
                image,
            ):
                raise ValueError("application images must use project-registry digests")
            images[name] = image
        UUID(data["coordinatorIdentity"]["clientId"])
        command = data.get("certificateCommand", [])
        if not isinstance(command, list) or any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in command
        ):
            raise ValueError("certificateCommand must be an argument vector")
        return cls(
            freeze(foundation),
            freeze(allocations),
            freeze(data["recipes"]),
            freeze(images),
            freeze(data["coordinatorIdentity"]),
            freeze(data["managementCluster"]),
            tuple(command),
            identity,
        )

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "foundation": plain(self.foundation),
            "allocations": plain(self.allocations),
            "recipes": plain(self.recipes),
            "images": plain(self.images),
            "coordinatorIdentity": plain(self.coordinator_identity),
            "managementCluster": plain(self.management_cluster),
            "certificateCommand": list(self.certificate_command),
            **({"bootstrapIdentity": self.bootstrap_settings} if self.identity else {}),
        }

    @property
    def bootstrap_settings(self) -> dict[str, str]:
        if self.identity is None:
            raise ProvisioningError("bootstrap_identity_required")
        return self.identity.public_values()

    @property
    def project_name(self) -> str:
        return self.foundation["projectName"]

    @property
    def resource_prefix(self) -> str:
        return self.identity.stem if self.identity else "radplanes"

    @property
    def radius_group(self) -> str:
        return self.resource_prefix

    def namespace(self, slot: str) -> str:
        self.allocation(slot)
        if self.identity:
            return self.identity.namespace(slot)
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        return f"radplanes-{slot}-{role}"

    def workspace(self, slot: str) -> str:
        self.allocation(slot)
        return f"{self.resource_prefix}-{slot}"

    def allocation(self, slot: str) -> Mapping:
        if not SLUG.fullmatch(slot) or slot not in self.allocations:
            raise ProvisioningError("allocation_unavailable")
        return self.allocations[slot]

    @property
    def pair_slots(self) -> list[dict[str, str]]:
        return [
            {"pair_id": pair, "reporting_role": "cp_" + pair.replace("-", "_")}
            for pair in sorted(
                slot.removesuffix("-control")
                for slot in self.allocations
                if slot.endswith("-control")
            )
        ]


@dataclass(frozen=True)
class Cluster:
    slot: str
    cluster_id: str
    context: str
    kubeconfig: Path


@dataclass(frozen=True)
class PairResult:
    control_cluster_id: str
    data_cluster_id: str
    control_url: str
    data_url: str


class ProvisioningConfig(Protocol):
    @property
    def project_name(self) -> str: ...

    @property
    def images(self) -> Mapping: ...

    @property
    def pair_slots(self) -> list[dict[str, str]]: ...

    def allocation(self, slot: str) -> Mapping: ...
    def to_dict(self) -> dict: ...


class Provider(Protocol):
    @property
    def config(self) -> ProvisioningConfig: ...

    def expected_cluster_id(self, slot: str) -> str: ...
    def validate_endpoint(self, slot: str, value: str) -> str: ...
    def ensure_child_cluster(self, slot: str) -> Cluster: ...
    def bootstrap_child(self, cluster: Cluster) -> None: ...
    def deploy_plane(self, slot: str, observe: Callable[[str], None]) -> str: ...
    def inspect_pair(self, pair_id: str) -> PairResult: ...


def provision_pair(
    request: PendingOperation,
    provider: Provider,
    pair: Mapping,
    observe: Callable[[str], None],
) -> PairResult:
    """Infrastructure only: never send tenant state to a child API."""
    if (
        request.pair_id != pair["pair_id"]
        or request.isolation != pair["isolation"]
        or request.isolation != ("shared" if request.pair_id == "shared" else "isolated")
        or pair["reporting_role"] != "cp_" + request.pair_id.replace("-", "_")
    ):
        raise ProvisioningError("invalid_pair_assignment")
    slots = [f"{request.pair_id}-{role}" for role in ("control", "data")]
    expected_ids = [provider.expected_cluster_id(slot) for slot in slots]
    if pair["stage"] == "available":
        result = provider.inspect_pair(request.pair_id)
        if [result.control_cluster_id, result.data_cluster_id] != expected_ids:
            raise ProvisioningError("pair_inventory_mismatch")
        observe("reuse-pair")
        urls = [
            provider.validate_endpoint(slot, url)
            for slot, url in zip(slots, (result.control_url, result.data_url), strict=True)
        ]
        return PairResult(*expected_ids, *urls)
    clusters = []
    for role, slot in zip(("control", "data"), slots, strict=True):
        observe(f"{role}-cluster")
        clusters.append(provider.ensure_child_cluster(slot))
    for role, cluster in zip(("control", "data"), clusters, strict=True):
        observe(f"{role}-radius")
        provider.bootstrap_child(cluster)
    urls = [provider.deploy_plane(slot, observe) for slot in slots]
    return PairResult(*(cluster.cluster_id for cluster in clusters), *urls)
