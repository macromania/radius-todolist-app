"""Immutable local placement. It has no Azure identity or resource-group fields."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from plane_demo.management.provisioning import DIGEST, ProvisioningError, freeze, plain

SLOTS = (
    "management",
    "shared-control",
    "shared-data",
    "isolated-1-control",
    "isolated-1-data",
)
GROUP = "radplanes-local"
SCOPE = f"/planes/radius/local/resourceGroups/{GROUP}"
ACCESS_NAMESPACE = "radplanes-local-access"
API_VERSION = "2025-08-01-preview"


def private_ipv4(value: str) -> str:
    try:
        address = IPv4Address(value)
    except (TypeError, ValueError):
        raise ValueError("local node address must be a private IPv4 address") from None
    if (
        not any(
            address in IPv4Network(network)
            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        or address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
    ):
        raise ValueError("local node address must be a private IPv4 address")
    return str(address)


@dataclass(frozen=True)
class LocalConfig:
    allocations: Mapping
    recipes: Mapping
    images: Mapping
    image_ids: Mapping
    management_cluster: Mapping

    @classmethod
    def load(cls, path: Path) -> LocalConfig:
        return cls.from_dict(json.loads(path.read_text()))

    @classmethod
    def from_dict(cls, data: dict) -> LocalConfig:
        if (
            data.get("version") != 1
            or data.get("provider") != "local"
            or data.get("projectName") != "radplanes"
        ):
            raise ValueError("local configuration requires version 1, local, radplanes")
        allocations = data["allocations"]
        if not isinstance(allocations, dict) or set(allocations) != set(SLOTS):
            raise ValueError("local configuration requires exactly the five reserved slots")
        for index, slot in enumerate(SLOTS):
            allocation = allocations[slot]
            expected = {
                "slot": slot,
                "clusterName": f"radplanes-local-{slot}",
                "context": f"radplanes-local-{slot}",
                "gatewayPort": 35490 + index,
                "apiPort": 35495 + index,
            }
            if allocation != expected:
                raise ValueError("local allocation does not match the reserved topology")
        recipes = data["recipes"]
        if set(recipes) != {"cluster", "postgresql", "redis", "gateway"}:
            raise ValueError("local configuration requires all four reviewed Recipes")
        for recipe in recipes.values():
            digest = recipe["digest"]
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise ValueError("local Recipe digest must be SHA-256")
            sha = digest.removeprefix("sha256:")
            name = recipe["moduleServer"]
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"local-module-[a-f0-9]{20}", name)
                or recipe["reference"]
                != f"http://{name}.radius-system.svc.cluster.local:18080/{sha}.tar.gz"
            ):
                raise ValueError("local Recipes must use content-addressed in-cluster archives")
        images, ids = {}, {}
        revisions = set()
        for role in ("api", "provisioner"):
            image = data["images"][role]
            reference, image_id = image["reference"], image["imageId"]
            match = re.fullmatch(rf"localhost/radplanes-plane-{role}:([a-f0-9]{{40}})", reference)
            if not match or not DIGEST.fullmatch(image_id):
                raise ValueError("local images require committed tags and inspected image IDs")
            revisions.add(match[1])
            images[role], ids[role] = reference, image_id
        if len(revisions) != 1:
            raise ValueError("both local images must use the same source commit")
        management = data["managementCluster"]
        if management["clusterId"] != "kind://radplanes-local-management":
            raise ValueError("local management identity mismatch")
        UUID(management["uid"])
        private_ipv4(management["nodeAddress"])
        private_ipv4(management["serviceAddress"])
        if not re.fullmatch(r"[a-f0-9]{64}", management["caSHA256"]):
            raise ValueError("management CA identity must be SHA-256")
        return cls(
            freeze(allocations),
            freeze(recipes),
            freeze(images),
            freeze(ids),
            freeze(management),
        )

    def allocation(self, slot: str) -> Mapping:
        if slot not in SLOTS:
            raise ProvisioningError("allocation_unavailable")
        return self.allocations[slot]

    @property
    def pair_slots(self) -> list[dict[str, str]]:
        return [
            {"pair_id": "isolated-1", "reporting_role": "cp_isolated_1"},
            {"pair_id": "shared", "reporting_role": "cp_shared"},
        ]

    def expected_cluster_id(self, slot: str) -> str:
        return f"kind://{self.allocation(slot)['clusterName']}"

    def validate_endpoint(self, slot: str, value: str) -> str:
        port = self.allocation(slot)["gatewayPort"]
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "http"
                and parsed.netloc == f"127.0.0.1:{port}"
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ProvisioningError("invalid_gateway_output")
        return value.rstrip("/")

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "provider": "local",
            "projectName": "radplanes",
            "allocations": plain(self.allocations),
            "recipes": plain(self.recipes),
            "images": {
                role: {"reference": self.images[role], "imageId": self.image_ids[role]}
                for role in ("api", "provisioner")
            },
            "managementCluster": plain(self.management_cluster),
        }
