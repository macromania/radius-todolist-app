#!/usr/bin/env python3
"""Preview or execute .env-selected Azure teardown through verified live Radius owners."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from plane_demo.management.providers.commands import Commands
from plane_demo.management.provisioning import ProvisioningError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.operations.config import ConfigError, load_config  # noqa: E402
from scripts.operations.output import progress, run_main, status  # noqa: E402

PROJECT = "radplanes"
SUBSCRIPTION = "a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc"
TAGS = {"project": PROJECT, "managedBy": "radius-todolist-app", "SecurityControl": "Ignore"}
SLUG = re.compile(r"[a-z][a-z0-9-]{0,62}")
GUID_NAMESPACE = UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830")
ROLE_NAMES = {
    "certificateImporter": ("certificate-importer", "radplanes certificate importer"),
    "acmeStateWriter": ("acme-state-writer", "radplanes ACME state writer"),
    "childClusterRecipe": ("child-cluster-recipe", "radplanes child cluster recipe"),
    "childIdentityFederation": ("child-identity-federation", "radplanes child identity federation"),
}
UNTAGGABLE = {
    "microsoft.authorization/roleassignments",
    "microsoft.resources/deployments",
    "microsoft.network/virtualnetworks/subnets",
    "microsoft.network/privateendpoints/privatednszonegroups",
    "microsoft.managedidentity/userassignedidentities/federatedidentitycredentials",
    "microsoft.dbforpostgresql/flexibleservers/databases",
    "microsoft.dbforpostgresql/flexibleservers/configurations",
    "microsoft.cache/redisenterprise/databases",
}


class CleanupError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CleanupError(message)


def role_id(key: str) -> str:
    name = uuid5(GUID_NAMESPACE, f"/subscriptions/{SUBSCRIPTION}-{PROJECT}-{ROLE_NAMES[key][0]}")
    return f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{name}"


def group_id(name: str) -> str:
    return f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{name}"


def same_id(left: object, right: str) -> bool:
    return isinstance(left, str) and left.rstrip("/").lower() == right.rstrip("/").lower()


def state_file(root: Path, value: str | Path, *, exists: bool = True) -> Path:
    state = root / ".state" / "azure"
    path = Path(value)
    path = path if path.is_absolute() else state / path
    require(path.resolve().is_relative_to(state.resolve()), "File must remain in .state/azure")
    for part in (state, *path.parents, path):
        if part.is_relative_to(root):
            require(not part.is_symlink(), "State/configuration paths cannot be symlinks")
    require(not exists or path.is_file(), "Required project state file is missing")
    return path.resolve()


def owned_tags(value: object) -> bool:
    return isinstance(value, dict) and all(
        {str(k).lower(): v for k, v in value.items()}.get(key.lower()) == expected
        for key, expected in TAGS.items()
    )


def array(value: object) -> list:
    if isinstance(value, dict):
        value = value.get("value", value.get("items"))
    require(isinstance(value, list), "Command returned an unexpected list shape")
    return value


class Manifest:
    def __init__(self, data: dict):
        self.data = {
            key: value["value"]
            if isinstance(value, dict) and "value" in value and "type" in value
            else value
            for key, value in data.items()
        }
        self.foundation = self.data["foundation"]
        foundation = self.foundation
        require(foundation["projectName"] == PROJECT, "Foreign project in ownership manifest")
        require(foundation["subscriptionId"] == SUBSCRIPTION, "Foreign subscription in manifest")
        require(owned_tags(foundation["tags"]), "Manifest ownership tags do not match")
        require(foundation["platformResourceGroup"] == f"rg-{PROJECT}-platform", "Foreign platform")
        self.platform = foundation["platformResourceGroup"]
        entries = self.data["allocations"]
        require(isinstance(entries, list) and entries, "Bootstrap allocations must be an array")
        self.allocations = {}
        for allocation in entries:
            slot = allocation["slot"]
            require(
                isinstance(slot, str)
                and SLUG.fullmatch(slot) is not None
                and (slot == "management" or slot.endswith(("-control", "-data"))),
                "Invalid allocation slot",
            )
            require(slot not in self.allocations, "Duplicate allocation slot")
            require(allocation["clusterName"] == f"aks-{PROJECT}-{slot}", "Foreign AKS name")
            for kind in ("cluster", "app"):
                name = f"rg-{PROJECT}-{slot}-{kind}"
                require(allocation[f"{kind}ResourceGroup"] == name, "Foreign resource group name")
                require(
                    same_id(allocation[f"{kind}ResourceGroupId"], group_id(name)), "Foreign RG ID"
                )
            require(
                allocation["nodeResourceGroup"] == f"rg-{PROJECT}-{slot}-nodes",
                "Foreign managed node resource group",
            )
            self.allocations[slot] = allocation
        require("management" in self.allocations, "Management allocation is required")
        for slot in self.children:
            pair, role = slot.rsplit("-", 1)
            require(
                f"{pair}-{'data' if role == 'control' else 'control'}" in self.allocations,
                "Incomplete child pair allocation",
            )
        self.groups = [self.platform] + [
            allocation[field]
            for allocation in self.allocations.values()
            for field in ("appResourceGroup", "clusterResourceGroup", "nodeResourceGroup")
        ]
        require(len(set(self.groups)) == len(self.groups), "Duplicate resource group ownership")
        self.roles = foundation["roleDefinitionIds"]
        require(set(self.roles) == set(ROLE_NAMES), "Expected exactly four bootstrap custom roles")
        for key, value in self.roles.items():
            require(same_id(value, role_id(key)), "Foreign or tampered custom role definition")
        for field, resource_type, name_field in (
            ("vaultId", "Microsoft.KeyVault/vaults", "vaultName"),
            ("registryId", "Microsoft.ContainerRegistry/registries", "registryName"),
            ("virtualNetworkId", "Microsoft.Network/virtualNetworks", "virtualNetworkName"),
        ):
            name = foundation[name_field]
            require(isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9-]+", name), "Invalid name")
            require(
                same_id(
                    foundation[field], f"{group_id(self.platform)}/providers/{resource_type}/{name}"
                ),
                "Foreign foundation resource ID",
            )
        management = self.data["managementCluster"]
        require(same_id(management["id"], self.cluster_id("management")), "Foreign management AKS")
        require(
            management["resourceGroup"] == self.allocations["management"]["clusterResourceGroup"],
            "Foreign management group",
        )

    @property
    def children(self) -> list[str]:
        return sorted(
            (slot for slot in self.allocations if slot != "management"),
            key=lambda slot: (not slot.endswith("-data"), slot),
        )

    def cluster_id(self, slot: str) -> str:
        allocation = self.allocations[slot]
        return (
            f"{allocation['clusterResourceGroupId']}/providers/"
            f"Microsoft.ContainerService/managedClusters/{allocation['clusterName']}"
        )

    def owns_scope(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        match = re.fullmatch(
            rf"/subscriptions/{re.escape(SUBSCRIPTION)}/resourceGroups/([a-z0-9-]+)"
            r"(?:/providers/[A-Za-z0-9._/-]+)?/?",
            value,
            re.IGNORECASE,
        )
        return bool(match and match[1].lower() in self.groups)

    def check_azure_references(self, value: object) -> None:
        if isinstance(value, str) and value.startswith("/subscriptions/"):
            require(
                self.owns_scope(value)
                or same_id(value, f"/subscriptions/{SUBSCRIPTION}")
                or any(same_id(value, identifier) for identifier in self.roles.values()),
                "Radius metadata references an unowned Azure scope",
            )
        elif isinstance(value, dict):
            for child in value.values():
                self.check_azure_references(child)
        elif isinstance(value, list):
            for child in value:
                self.check_azure_references(child)


class Cleanup:
    def __init__(
        self,
        manifest: Manifest,
        *,
        root: Path,
        commands=None,
        targets: dict | None = None,
        radius_config: Path | None = None,
        execute: bool = False,
        provider_only: bool = False,
        radius_only: bool = False,
    ):
        require(
            not execute or os.environ.get("CONFIRM_AZURE") == "yes",
            "Execution requires CONFIRM_AZURE=yes",
        )
        self.manifest = manifest
        self.root = root
        self.commands = commands or Commands(root)
        self.targets = targets or {}
        require(set(self.targets).issubset(manifest.allocations), "Foreign cleanup target slot")
        self.radius_config = radius_config
        self.execute = execute
        self.provider_only = provider_only
        require(not (provider_only and radius_only), "Cleanup modes cannot be combined")
        self.radius_only = radius_only
        self.steps: list[dict] = []

    def call(self, args: list[str], *, env=None, mutation: bool = False) -> str:
        if mutation:
            require(
                self.execute and os.environ.get("CONFIRM_AZURE") == "yes",
                "Mutation requires --execute and CONFIRM_AZURE=yes",
            )
        try:
            return self.commands.run(args, env=env, timeout=7200 if mutation else 180)
        except ProvisioningError:
            if args[0] in {"rad", "kubectl"}:
                raise CleanupError(
                    "Radius/Kubernetes access failed. No automatic provider fallback. "
                    "Use a reachable approved network and exported project credentials, or "
                    "review the explicit --provider-only emergency path."
                ) from None
            target = next(
                (
                    args[args.index(flag) + 1]
                    for flag in ("--resource-group", "--scope", "--name")
                    if flag in args
                ),
                None,
            )
            operation = " ".join(args[:3]) + (f" ({target})" if target else "")
            raise CleanupError(f"{operation} command failed; cleanup is incomplete") from None

    def az(self, *args: str, mutation: bool = False):
        text = self.call(
            [
                "az",
                *args,
                "--subscription",
                SUBSCRIPTION,
                "--only-show-errors",
                "--output",
                "none" if mutation else "json",
            ],
            mutation=mutation,
        )
        return None if mutation else json.loads(text)

    def group(self, name: str) -> list[dict] | None:
        require(name in self.manifest.groups, "Resource group is not in the ownership manifest")
        exists = self.az("group", "exists", "--name", name)
        require(type(exists) is bool, "Invalid Azure group-existence response")
        if not exists:
            return None
        group = self.az("group", "show", "--name", name)
        require(same_id(group.get("id"), group_id(name)), "Azure group scope mismatch")
        require(group.get("name") == name and owned_tags(group.get("tags")), "Unowned Azure group")
        resources = array(self.az("resource", "list", "--resource-group", name))
        for resource in resources:
            require(self.manifest.owns_scope(resource.get("id")), "Foreign provider resource scope")
            require(
                resource["id"].lower().startswith(group_id(name).lower() + "/providers/"),
                "Resource listed in the wrong group",
            )
            require(
                owned_tags(resource.get("tags"))
                or (not resource.get("tags") and resource.get("type", "").lower() in UNTAGGABLE),
                "Resource lacks verified project tags; inspect ownership before cleanup",
            )
            if name == self.manifest.platform:
                foundation_ids = {
                    "microsoft.keyvault/vaults": "vaultId",
                    "microsoft.containerregistry/registries": "registryId",
                    "microsoft.network/virtualnetworks": "virtualNetworkId",
                }
                field = foundation_ids.get(resource.get("type", "").lower())
                require(
                    field is None or same_id(resource["id"], self.manifest.foundation[field]),
                    "Foundation contains a different resource than the saved manifest",
                )
        return resources

    def role_state(self) -> tuple[dict, list]:
        definitions = array(self.az("role", "definition", "list", "--custom-role-only", "true"))
        for identifier in self.manifest.roles.values():
            exact = array(
                self.az("role", "definition", "list", "--name", identifier.rsplit("/", 1)[-1])
            )
            require(
                all(same_id(definition.get("id"), identifier) for definition in exact),
                "Exact custom role lookup returned a different role ID",
            )
            definitions.extend(exact)
        found = {}
        for definition in definitions:
            key = next(
                (
                    key
                    for key, value in self.manifest.roles.items()
                    if same_id(definition.get("id"), value)
                ),
                None,
            )
            if key is None:
                require(
                    not definition.get("roleName", "").startswith(PROJECT + " "),
                    "Unmanifested project custom role; inspect it explicitly",
                )
                continue
            require(definition.get("roleType") == "CustomRole", "Refusing a built-in role")
            require(definition.get("roleName") == ROLE_NAMES[key][1], "Custom role name mismatch")
            expected = (
                {group_id(self.manifest.platform).lower()}
                if key in {"certificateImporter", "acmeStateWriter"}
                else {
                    self.manifest.allocations[slot]["clusterResourceGroupId"].lower()
                    for slot in self.manifest.children
                }
            )
            require(
                {scope.lower() for scope in definition["assignableScopes"]} == expected,
                "Custom role has foreign assignable scopes",
            )
            found[key] = definition
        assignments = []
        for assignment in array(
            self.az(
                "role",
                "assignment",
                "list",
                "--all",
                "--fill-principal-name",
                "false",
            )
        ):
            scope = assignment.get("scope")
            custom = any(
                same_id(assignment.get("roleDefinitionId"), value)
                for value in self.manifest.roles.values()
            )
            if not custom and not self.manifest.owns_scope(scope):
                continue
            require(self.manifest.owns_scope(scope), "Project role assignment has a foreign scope")
            identifier = assignment.get("id", "")
            prefix = scope.rstrip("/") + "/providers/Microsoft.Authorization/roleAssignments/"
            require(identifier.lower().startswith(prefix.lower()), "Assignment ID mismatch")
            require(
                re.fullmatch(
                    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                    identifier[len(prefix) :],
                )
                is not None,
                "Invalid assignment ID",
            )
            assignments.append(assignment)
        return found, assignments

    def unexpected_resources(self) -> tuple[list, list]:
        groups = array(self.az("group", "list", "--tag", f"project={PROJECT}"))
        require(
            all(group.get("name") in self.manifest.groups for group in groups),
            "Unmanifested project-tagged group; it will not be deleted",
        )
        resources = array(self.az("resource", "list", "--tag", f"project={PROJECT}"))
        require(
            all(self.manifest.owns_scope(resource.get("id")) for resource in resources),
            "Unmanifested project-tagged resource; it will not be deleted",
        )
        return groups, resources

    def clusters(self, groups: dict) -> dict:
        result = {}
        for slot, allocation in self.manifest.allocations.items():
            group = allocation["clusterResourceGroup"]
            if groups[group] is None:
                continue
            entries = array(
                self.az(
                    "aks",
                    "list",
                    "--resource-group",
                    group,
                    "--query",
                    "[].{id:id,name:name,fqdn:fqdn,nodeResourceGroup:nodeResourceGroup,"
                    "tags:tags,provisioningState:provisioningState}",
                )
            )
            require(len(entries) <= 1, "Unexpected additional AKS in allocated group")
            if entries:
                cluster = entries[0]
                require(same_id(cluster.get("id"), self.manifest.cluster_id(slot)), "Foreign AKS")
                require(cluster.get("name") == allocation["clusterName"], "AKS name mismatch")
                require(
                    cluster.get("nodeResourceGroup") == allocation["nodeResourceGroup"],
                    "AKS node group differs from ownership manifest",
                )
                require(owned_tags(cluster.get("tags")), "AKS ownership tags are missing")
                if not self.provider_only:
                    require(
                        cluster.get("provisioningState") in {"Succeeded", "Failed", "Canceled"},
                        "AKS operation is still running; do not race cleanup with provisioning",
                    )
                result[slot] = cluster
        return result

    def target(self, slot: str) -> dict:
        require(slot in self.targets, f"Exported cleanup target is missing for {slot}")
        target = dict(self.targets[slot])
        require(target.get("context") == f"{PROJECT}-{slot}", "Unexpected project kubecontext")
        target["workspace"] = target.get("workspace", target["context"])
        target["group"] = target.get("group", PROJECT)
        for field in ("workspace", "group"):
            require(
                isinstance(target[field], str) and SLUG.fullmatch(target[field]), "Invalid target"
            )
        require(
            same_id(target.get("clusterId"), self.manifest.cluster_id(slot)), "Foreign target AKS"
        )
        try:
            UUID(target["clusterUid"])
        except (KeyError, ValueError, TypeError):
            raise CleanupError("Target requires a recorded kube-system namespace UID") from None
        target["kubeconfig"] = state_file(self.root, target["kubeconfig"])
        target["config"] = state_file(
            self.root,
            target.get("radiusConfig") or self.radius_config or "radius.yaml",
        )
        return target

    def kube(self, target: dict, *args: str, mutation: bool = False) -> str:
        return self.call(
            [
                "kubectl",
                "--kubeconfig",
                str(target["kubeconfig"]),
                "--context",
                target["context"],
                *args,
            ],
            mutation=mutation,
        )

    def rad(self, target: dict, *args: str, mutation: bool = False, group: bool = True):
        command = [
            "rad",
            "--config",
            str(target["config"]),
            *args,
            "--workspace",
            target["workspace"],
        ]
        if group:
            command += ["--group", target["group"]]
        if not mutation:
            command += ["--output", "json"]
        result = self.call(
            command,
            env=self.commands.radius_environment(target["kubeconfig"], target["context"]),
            mutation=mutation,
        )
        return None if mutation else json.loads(result)

    def radius_inventory(self, clusters: dict) -> tuple[dict, dict]:
        apps, records = {}, {}
        for slot in [*self.manifest.children, "management"]:
            if slot not in clusters:
                continue
            target = self.target(slot)
            server = self.kube(
                target,
                "config",
                "view",
                "--minify",
                "-o",
                "jsonpath={.clusters[0].cluster.server}",
            )
            parsed = urlsplit(server)
            require(
                parsed.scheme == "https"
                and parsed.hostname == clusters[slot]["fqdn"]
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.query,
                "Kubeconfig server does not match the allocated AKS",
            )
            insecure = self.kube(
                target,
                "config",
                "view",
                "--minify",
                "-o",
                "jsonpath={.clusters[0].cluster.insecure-skip-tls-verify}",
            )
            require(insecure in ("", "false"), "TLS verification must remain enabled")
            uid = self.kube(
                target, "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"
            )
            require(uid == target["clusterUid"], "Cluster UID mismatch")
            workspace = self.rad(target, "workspace", "show", group=False)
            scope = f"/planes/radius/local/resourceGroups/{target['group']}"
            require(
                workspace.get("connection") == {"kind": "kubernetes", "context": target["context"]},
                "Radius workspace targets a different cluster",
            )
            require(same_id(workspace.get("scope"), scope), "Radius workspace group mismatch")
            apps[slot] = []
            for app in array(self.rad(target, "app", "list")):
                name = app.get("name")
                require(isinstance(name, str) and SLUG.fullmatch(name), "Invalid Radius app name")
                app_id = f"{scope}/providers/Applications.Core/applications/{name}"
                require(same_id(app.get("id"), app_id), "Foreign Radius application")
                env_id = app["properties"]["environment"]
                prefix = f"{scope}/providers/Applications.Core/environments/"
                require(env_id.lower().startswith(prefix.lower()), "Foreign Radius environment")
                env_name = env_id[len(prefix) :]
                require(SLUG.fullmatch(env_name) is not None, "Invalid Radius environment name")
                environment = self.rad(target, "environment", "show", env_name)["properties"]
                azure_scope = environment["providers"]["azure"]["scope"]
                allowed = {self.manifest.allocations[slot]["appResourceGroupId"].lower()}
                if slot == "management":
                    allowed.update(
                        self.manifest.allocations[child]["clusterResourceGroupId"].lower()
                        for child in self.manifest.children
                    )
                require(azure_scope.lower() in allowed, "Application targets a foreign Azure scope")
                namespace = f"{environment['compute']['namespace']}-{name}"
                require(len(namespace) <= 63 and SLUG.fullmatch(namespace), "Invalid app namespace")
                app_resources = array(self.rad(target, "resource", "list", "--application", name))
                for resource in app_resources:
                    self.manifest.check_azure_references(resource)
                    resource_name, kind = resource["name"], resource["type"]
                    require(
                        isinstance(resource_name, str) and SLUG.fullmatch(resource_name),
                        "Invalid Radius resource name",
                    )
                    require(
                        same_id(resource["id"], f"{scope}/providers/{kind}/{resource_name}"),
                        "Foreign Radius resource",
                    )
                    properties = resource["properties"]
                    require(
                        same_id(properties.get("application"), app_id),
                        "Radius resource belongs to another application",
                    )
                    state = properties.get("provisioningState", "Succeeded")
                    require(
                        state in {"Succeeded", "Failed", "Canceled"},
                        "Radius operation is still running; stop/wait before cleanup",
                    )
                    if state != "Succeeded":
                        print(
                            f"partial Radius resource: {slot}/{name}/{resource_name} {state}",
                            file=sys.stderr,
                        )
                    if kind.lower() == "demo.platform/clusters":
                        child = properties["slot"]
                        require(
                            slot == "management" and child in self.manifest.children,
                            "Child cluster has the wrong Radius owner",
                        )
                        require(child not in records, "Duplicate cluster ownership records")
                        require(
                            same_id(
                                azure_scope,
                                self.manifest.allocations[child]["clusterResourceGroupId"],
                            ),
                            "Cluster environment has the wrong provider scope",
                        )
                        if properties.get("clusterId"):
                            require(
                                same_id(properties["clusterId"], self.manifest.cluster_id(child)),
                                "Radius cluster output targets a foreign AKS",
                            )
                        records[child] = (target, name, resource_name)
                apps[slot].append((target, name, namespace))
        require(
            all(slot == "management" or slot in records for slot in clusters),
            "AKS lacks its management-Radius ownership record; inspect --provider-only",
        )
        return apps, records

    def step(self, kind: str, name: str) -> None:
        self.steps.append({"action": kind, "name": name})
        print(f"{'execute' if self.execute else 'plan'} {kind}: {name}", file=sys.stderr)

    def delete_apps(self, slot: str, apps: list) -> None:
        for target, name, _ in apps:
            self.step("Radius application", f"{slot}/{name}")
            if self.execute:
                self.rad(target, "app", "delete", name, "--yes", mutation=True)
                remaining = array(self.rad(target, "app", "list"))
                require(
                    all(item["name"] != name for item in remaining),
                    "Radius app deletion incomplete",
                )
        if self.execute:
            remaining = self.group(self.manifest.allocations[slot]["appResourceGroup"])
            require(
                not remaining, "Radius left app-group resources; inspect explicit --provider-only"
            )

    def delete_aks(self, slot: str) -> None:
        allocation = self.manifest.allocations[slot]
        groups = {
            allocation["clusterResourceGroup"]: self.group(allocation["clusterResourceGroup"])
        }
        if groups[allocation["clusterResourceGroup"]] is None:
            return
        entries = array(
            self.az("aks", "list", "--resource-group", allocation["clusterResourceGroup"])
        )
        if not entries:
            return
        if slot != "management" and not self.provider_only:
            require(not self.execute, "Child AKS remains after Radius deletion")
            return
        require(
            len(entries) == 1
            and same_id(entries[0].get("id"), self.manifest.cluster_id(slot))
            and owned_tags(entries[0].get("tags")),
            "AKS ownership changed during cleanup",
        )
        self.step("bootstrap/emergency AKS", slot)
        if self.execute:
            require(
                slot == "management" or self.provider_only, "Normal child deletion must use Radius"
            )
            self.az(
                "aks",
                "delete",
                "--name",
                allocation["clusterName"],
                "--resource-group",
                allocation["clusterResourceGroup"],
                "--yes",
                mutation=True,
            )
            require(
                not array(
                    self.az("aks", "list", "--resource-group", allocation["clusterResourceGroup"])
                ),
                "AKS deletion is not complete",
            )

    def delete_group(self, name: str) -> None:
        resources = self.group(name)
        if resources is None:
            return
        for resource in resources:
            print(f"final provider resource: {resource['id']}", file=sys.stderr)
        self.step("owned Azure group", name)
        if self.execute:
            self.az("group", "delete", "--name", name, "--yes", mutation=True)
            require(
                self.az("group", "exists", "--name", name) is False,
                "Resource group deletion has not completed",
            )

    def clean(self) -> dict:
        self.unexpected_resources()
        node_groups = {item["nodeResourceGroup"] for item in self.manifest.allocations.values()}
        groups = {
            name: self.group(name)
            for name in self.manifest.groups
            if not self.radius_only or name not in node_groups
        }
        if not self.radius_only:
            self.role_state()  # Validate every custom scope before any mutation.
        clusters = self.clusters(groups)
        uninspected_node_groups = set()
        if self.radius_only:
            require("management" in clusters, "Radius-only cleanup requires management Radius")
            live_node_groups = {
                self.manifest.allocations[slot]["nodeResourceGroup"] for slot in clusters
            }
            for name in sorted(live_node_groups):
                require(self.group(name) is not None, "Live AKS managed node group is missing")
            uninspected_node_groups = node_groups - live_node_groups
        if any(value is not None for value in groups.values()) and not self.provider_only:
            require(
                "management" in clusters, "Management Radius is unavailable; review --provider-only"
            )
            apps, records = self.radius_inventory(clusters)
            for slot in self.manifest.children:
                require(
                    slot in clusters
                    or not groups[self.manifest.allocations[slot]["appResourceGroup"]],
                    "App resources have no reachable cluster owner; review --provider-only",
                )
            for target, name, namespace in apps.get("management", []):
                deployments = array(
                    json.loads(
                        self.kube(
                            target,
                            "-n",
                            namespace,
                            "get",
                            "deployments",
                            "-l",
                            f"radapp.io/application={name}",
                            "-o",
                            "json",
                        )
                    )
                )
                for deployment in deployments:
                    component = deployment["metadata"].get("labels", {}).get("radapp.io/resource")
                    if component not in {"management-api", "provisioner"}:
                        continue
                    deployment_name = deployment["metadata"]["name"]
                    require(SLUG.fullmatch(deployment_name) is not None, "Invalid deployment name")
                    self.step("quiesce", f"{namespace}/{deployment_name}")
                    if self.execute:
                        self.kube(
                            target,
                            "-n",
                            namespace,
                            "scale",
                            f"deployment/{deployment_name}",
                            "--replicas=0",
                            mutation=True,
                        )
                        deadline = time.monotonic() + 180
                        while array(
                            json.loads(
                                self.kube(
                                    target,
                                    "-n",
                                    namespace,
                                    "get",
                                    "pods",
                                    "-l",
                                    f"radapp.io/application={name},radapp.io/resource={component}",
                                    "-o",
                                    "json",
                                )
                            )
                        ):
                            require(
                                time.monotonic() < deadline, "Management workloads did not stop"
                            )
                            time.sleep(2)
            for slot in self.manifest.children:
                self.delete_apps(slot, apps.get(slot, []))
            for slot in self.manifest.children:
                if slot not in records:
                    continue
                target, app, name = records[slot]
                self.step("management-Radius cluster", slot)
                if self.execute:
                    self.rad(
                        target,
                        "resource",
                        "delete",
                        "Demo.Platform/clusters",
                        name,
                        "--application",
                        app,
                        "--yes",
                        mutation=True,
                    )
                    remaining = array(self.rad(target, "resource", "list", "--application", app))
                    require(
                        all(item["name"] != name for item in remaining),
                        "Radius cluster still exists",
                    )
                    require(
                        not array(
                            self.az(
                                "aks",
                                "list",
                                "--resource-group",
                                self.manifest.allocations[slot]["clusterResourceGroup"],
                            )
                        ),
                        "Child AKS still exists; refusing management teardown",
                    )
            self.delete_apps("management", apps.get("management", []))
        elif self.provider_only:
            print(
                "EMERGENCY provider-only teardown: Radius ownership path is explicitly bypassed",
                file=sys.stderr,
            )
            # Remove the public entrypoint and provisioning database before child infrastructure.
            self.delete_group(self.manifest.allocations["management"]["appResourceGroup"])
        if self.radius_only:
            return {
                "status": "radius_resources_removed" if self.execute else "planned",
                "foundationRetained": True,
                "uninspectedManagedNodeGroups": sorted(uninspected_node_groups),
                "steps": self.steps,
            }
        for slot in self.manifest.children:
            self.delete_group(self.manifest.allocations[slot]["appResourceGroup"])
            self.delete_aks(slot)
            self.delete_group(self.manifest.allocations[slot]["clusterResourceGroup"])
            self.delete_group(self.manifest.allocations[slot]["nodeResourceGroup"])
        self.delete_group(self.manifest.allocations["management"]["appResourceGroup"])
        self.delete_aks("management")
        self.delete_group(self.manifest.allocations["management"]["clusterResourceGroup"])
        self.delete_group(self.manifest.allocations["management"]["nodeResourceGroup"])
        self.delete_group(self.manifest.platform)
        definitions, assignments = self.role_state()
        for assignment in assignments:
            self.step("scoped role assignment", assignment["id"])
            if self.execute:
                self.az("role", "assignment", "delete", "--ids", assignment["id"], mutation=True)
        if self.execute:
            definitions, assignments = self.role_state()
            require(not assignments, "Project-scope role assignments remain")
        for key in definitions:
            self.step("custom role definition", key)
            if self.execute:
                self.az(
                    "role",
                    "definition",
                    "delete",
                    "--name",
                    self.manifest.roles[key].rsplit("/", 1)[-1],
                    "--custom-role-only",
                    "true",
                    mutation=True,
                )
        return (
            {**self.verify(), "steps": self.steps}
            if self.execute
            else {"status": "planned", "steps": self.steps}
        )

    def verify(self) -> dict:
        remaining = [
            name
            for name in self.manifest.groups
            if self.az("group", "exists", "--name", name) is not False
        ]
        require(not remaining, "Owned resource groups remain: " + ", ".join(remaining))
        definitions, assignments = self.role_state()
        require(not definitions and not assignments, "Project custom roles or assignments remain")
        tagged_groups, tagged_resources = self.unexpected_resources()
        require(not tagged_groups and not tagged_resources, "Active project resources remain")
        tombstones = []
        for vault in array(self.az("keyvault", "list-deleted")):
            if vault.get("name") == self.manifest.foundation["vaultName"]:
                require(
                    same_id(
                        vault["properties"].get("vaultId"), self.manifest.foundation["vaultId"]
                    ),
                    "Soft-deleted vault does not match the ownership manifest",
                )
                tombstones.append(
                    {
                        "name": vault["name"],
                        "scheduledPurgeDate": vault["properties"].get("scheduledPurgeDate"),
                    }
                )
        return {"status": "clean", "subscriptionId": SUBSCRIPTION, "softDeletedVaults": tombstones}


SLOTS = ("management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data")
CHILDREN = ("shared-data", "isolated-1-data", "shared-control", "isolated-1-control")
TERMINAL = {"Succeeded", "Failed", "Canceled"}
DEPENDENCIES = {
    "Demo.Platform/postgreSqlDatabases",
    "Demo.Platform/gateways",
    "Applications.Datastores/redisCaches",
}
OPEN_CLUSTER = r"""
set -euo pipefail
set +x
umask 077
source "$1/scripts/lib/env.sh"
source "$1/scripts/lib/discovery.sh"
demo_load_env "$1/.env"
[[ "$DEMO_ENV" == "$CLEANUP_ENV" && "$DEMO_PROJECT" == "$CLEANUP_PROJECT" &&
   "$DEMO_DEPLOYMENT" == "$CLEANUP_DEPLOYMENT" ]] || {
  demo_error 'Cleanup configuration changed'; exit 1;
}
unset DEMO_KEY_MANAGEMENT DEMO_KEY_SHARED_CONTROL DEMO_KEY_SHARED_DATA \
  DEMO_KEY_ISOLATED_1_CONTROL DEMO_KEY_ISOLATED_1_DATA
DEMO_WORKSPACE=$2
demo_open_cluster "$3"
cluster=$(demo_kube get namespace kube-system --output json)
jq -n --arg context "$DEMO_CONTEXT" --arg kubeconfig "$DEMO_KUBECONFIG" \
  --argjson cluster "$cluster" '{context:$context,kubeconfig:$kubeconfig,cluster:$cluster}'
"""


def fault_helpers():
    name = "plane_demo_cleanup_journal_helpers"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).parents[1] / "harness/fault-parent-link.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class LiveClusterCleanup:
    """Cluster-only access and Radius owner deletion shared by the normal cleanup paths."""

    def __init__(
        self, *, environment, execute=False, runner=None, clock=time.monotonic, sleep=time.sleep
    ):
        self.config = load_config(ROOT / ".env")
        require(self.config.environment == environment, "Cleanup environment differs from .env")
        self.environment, self.execute = environment, execute
        self.runner = runner or subprocess.run
        self.clock, self.sleep = clock, sleep
        self.confirmation = "CONFIRM_AZURE" if environment == "azure" else "CONFIRM_LOCAL"
        require(
            not execute or os.environ.get(self.confirmation) == "yes",
            f"Execution requires {self.confirmation}=yes",
        )
        self.workspace = tempfile.TemporaryDirectory(prefix=".cleanup-", dir=ROOT)
        self.work = Path(self.workspace.name)
        self.scope = f"/planes/radius/local/resourceGroups/{self.config.stem}"
        self.targets, self.inventories, self.steps = {}, {}, []
        self.removed = {}
        self.env = dict(os.environ)
        for key in ("DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_HOST", "KUBECONFIG"):
            self.env.pop(key, None)
        self.env["AZURE_CONFIG_DIR"] = os.environ.get(
            "AZURE_CONFIG_DIR", str(Path.home() / ".azure")
        )

    def close(self):
        self.workspace.cleanup()

    def current(self):
        require(load_config(ROOT / ".env") == self.config, "Cleanup configuration changed")

    def call(self, argv, *, mutation=False, payload=None, env=None, timeout=180):
        self.current()
        if mutation:
            require(
                self.execute and os.environ.get(self.confirmation) == "yes",
                f"Mutation requires --execute and {self.confirmation}=yes",
            )
        try:
            with progress(f"Cleanup: {Path(argv[0]).name}"):
                result = self.runner(
                    argv,
                    input=payload,
                    env=env or self.env,
                    cwd=ROOT,
                    timeout=timeout,
                    capture_output=True,
                    text=True,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired):
            raise CleanupError(
                f"{argv[0]} unavailable or timed out; cleanup is incomplete"
            ) from None
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        require(result.returncode == 0, f"{argv[0]} command failed; no provider fallback")
        require(len(result.stdout) <= 8_000_000, "Cleanup response exceeds inventory limit")
        return result.stdout

    def json(self, argv, **kwargs):
        try:
            return json.loads(self.call(argv, **kwargs))
        except ValueError:
            raise CleanupError("Cleanup command returned invalid JSON") from None

    def rows(self, value):
        require(
            not isinstance(value, dict)
            or not (value.get("nextLink") or value.get("@odata.nextLink")),
            "Incomplete inventory pagination",
        )
        return array(value)

    def open_cluster(self, slot):
        require(slot in SLOTS, "Unknown cleanup slot")
        if slot in self.targets:
            return self.targets[slot]
        work = Path(tempfile.mkdtemp(prefix="access-", dir=self.work))
        value = self.json(
            ["bash", "-c", OPEN_CLUSTER, "cleanup-access", str(ROOT), str(work), slot],
            env={
                **os.environ,
                "TMPDIR": str(self.work),
                "CLEANUP_ENV": self.environment,
                "CLEANUP_PROJECT": self.config.project,
                "CLEANUP_DEPLOYMENT": self.config.deployment,
            },
        )
        context = self.config.slot_name(slot)
        profile = work / slot / "kubeconfig"
        require(
            value.get("context") == context
            and value.get("kubeconfig") == str(profile)
            and profile.is_file()
            and not profile.is_symlink()
            and profile.stat().st_mode & 0o777 == 0o600,
            "Cluster access is not the selected private profile",
        )
        metadata = value["cluster"]["metadata"]
        require(
            metadata["name"] == "kube-system" and str(UUID(metadata["uid"])) == metadata["uid"],
            "Live cluster UID is invalid",
        )
        home = work / "home"
        home.mkdir(mode=0o700)
        (home / ".kube").mkdir(mode=0o700)
        (home / ".kube/config").symlink_to(profile)
        radius = work / "radius.json"
        radius.write_text(
            json.dumps(
                {
                    "workspaces": {
                        "default": context,
                        "items": {
                            context: {
                                "connection": {"kind": "kubernetes", "context": context},
                                "scope": self.scope,
                            }
                        },
                    }
                }
            )
        )
        radius.chmod(0o600)
        target = {
            "slot": slot,
            "context": context,
            "kubeconfig": profile,
            "home": home,
            "radius": radius,
            "cluster_uid": metadata["uid"],
        }
        self.targets[slot] = target
        return target

    def kube(self, slot, *args, namespace=None, mutation=False, payload=None):
        target = self.open_cluster(slot)
        return self.call(
            [
                "kubectl",
                "--kubeconfig",
                str(target["kubeconfig"]),
                "--context",
                target["context"],
                "--request-timeout=30s",
                *(["-n", namespace] if namespace else []),
                *args,
            ],
            mutation=mutation,
            payload=payload,
            env={**self.env, "HOME": str(target["home"])},
        )

    def kube_json(self, slot, *args, **kwargs):
        return json.loads(self.kube(slot, *args, "-o", "json", **kwargs))

    def verify_cluster(self, slot):
        current = self.kube_json(slot, "get", "namespace", "kube-system")
        require(
            current["metadata"]["uid"] == self.targets[slot]["cluster_uid"],
            "Cluster UID changed since cleanup preflight",
        )

    def namespace(self, slot):
        name = self.config.namespace(slot)
        raw = self.kube(slot, "get", "namespace", name, "--ignore-not-found", "-o", "json")
        if not raw.strip():
            return None
        value = json.loads(raw)
        metadata = value["metadata"]
        require(
            metadata["name"] == name and str(UUID(metadata["uid"])) == metadata["uid"],
            "Application namespace identity differs",
        )
        require(
            all(
                metadata.get("labels", {}).get(key) == expected
                for key, expected in {
                    "plane-demo/project": self.config.project,
                    "plane-demo/deployment": self.config.deployment,
                    "plane-demo/environment": self.environment,
                }.items()
            ),
            "Application namespace ownership differs",
        )
        return value

    def rad(self, slot, *args, mutation=False):
        target = self.open_cluster(slot)
        raw = self.call(
            [
                "rad",
                "--config",
                str(target["radius"]),
                *args,
                "--workspace",
                target["context"],
                "--group",
                self.config.stem,
                *([] if mutation else ["--output", "json"]),
            ],
            mutation=mutation,
            timeout=900 if mutation else 180,
            env={**self.env, "HOME": str(target["home"]), "KUBECONFIG": str(target["kubeconfig"])},
        )
        return None if mutation else json.loads(raw)

    def native_resources(self, slot):
        value = json.loads(
            self.kube(
                slot,
                "get",
                "--raw",
                f"/apis/api.ucp.dev/v1alpha3{self.scope}/resources?api-version=2023-10-01-preview",
            )
        )
        result, seen = [], set()
        for item in self.rows(value):
            name, kind, identifier = item["name"], item["type"], item["id"]
            require(
                SLUG.fullmatch(name)
                and re.fullmatch(r"[A-Za-z0-9.]+/[A-Za-z0-9.]+", kind)
                and same_id(identifier, f"{self.scope}/providers/{kind}/{name}")
                and identifier.lower() not in seen,
                "Foreign or duplicate Radius resource",
            )
            seen.add(identifier.lower())
            if kind.lower() not in {
                "applications.core/applications",
                "applications.core/environments",
                "microsoft.resources/deployments",
            }:
                result.append(item)
        return result

    def inventory(self, slot):
        self.verify_cluster(slot)
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        apps = self.rows(self.rad(slot, "app", "list"))
        resources = self.native_resources(slot)
        allowed = {role} | (
            {f"cluster-{child}" for child in CHILDREN} if slot == "management" else set()
        )
        require(len({item["name"] for item in apps}) == len(apps), "Duplicate Radius applications")
        bindings, owners = {}, {}
        for app in apps:
            name = app["name"]
            require(
                name in allowed and same_id(app["id"], self.app_id(name)),
                "Unexpected Radius application owner",
            )
            environment = slot if name == role else "provision-" + name.removeprefix("cluster-")
            env_id = f"{self.scope}/providers/Applications.Core/environments/{environment}"
            require(
                same_id(app["properties"]["environment"], env_id), "Foreign application environment"
            )
            value = self.rad(slot, "env", "show", environment)
            require(same_id(value["id"], env_id), "Radius environment identity differs")
            compute = value["properties"]["compute"]
            namespaces = (
                {self.config.slot_name(slot), self.config.namespace(slot)}
                if name == role
                else {f"{self.config.stem}-p-{name.removeprefix('cluster-')}"}
            )
            require(
                compute["kind"] == "kubernetes" and compute["namespace"] in namespaces,
                "Radius compute scope differs",
            )
            self.environment_scope(slot, name, value["properties"])
            bindings[name] = env_id
        for item in resources:
            kind, properties = item["type"], item["properties"]
            require(
                properties.get("provisioningState") in TERMINAL,
                "Radius operation is incomplete; cleanup will not cancel it",
            )
            app = next(
                (
                    name
                    for name in bindings
                    if same_id(properties.get("application"), self.app_id(name))
                ),
                None,
            )
            require(app is not None, "Resource has no validated Radius application")
            require(
                same_id(properties.get("environment"), bindings[app])
                or (
                    kind == "Applications.Core/containers" and properties.get("environment") is None
                ),
                "Radius resource environment differs",
            )
            if kind == "Demo.Platform/clusters":
                child = properties.get("slot")
                require(
                    slot == "management"
                    and child in CHILDREN
                    and item["name"] == child
                    and app == "cluster-" + child
                    and child not in owners,
                    "Foreign or duplicate management-owned child cluster",
                )
                self.cluster_record(child, properties)
                owners[child] = item
            else:
                require(
                    kind in DEPENDENCIES | {"Applications.Core/containers"} and app == role,
                    "Unknown Radius workload owner",
                )
            self.resource_scope(item)
        return {"apps": apps, "resources": resources, "children": owners}

    def app_id(self, name):
        return f"{self.scope}/providers/Applications.Core/applications/{name}"

    def environment_scope(self, slot, app, properties):
        raise NotImplementedError

    def resource_scope(self, item):
        raise NotImplementedError

    def cluster_record(self, slot, properties):
        raise NotImplementedError

    def note(self, action, identity):
        self.steps.append({"action": action, "identity": identity})
        status("progress", f"{'execute' if self.execute else 'plan'} {action}: {identity}")

    def quiesce(self):
        if self.namespace("management") is None:
            return
        namespace = self.config.namespace("management")
        jobs = self.rows(self.kube_json("management", "get", "jobs", namespace=namespace))
        require(
            all(
                any(
                    c.get("type") in {"Complete", "Failed"} and c.get("status") == "True"
                    for c in job.get("status", {}).get("conditions", [])
                )
                for job in jobs
            ),
            "Management Job is active; finish it before cleanup",
        )
        deployments = self.rows(
            self.kube_json("management", "get", "deployments", namespace=namespace)
        )
        for value in deployments:
            metadata = value["metadata"]
            component = metadata.get("labels", {}).get("plane-demo/component")
            if component not in {"management-api", "provisioner"}:
                continue
            require(
                metadata["name"] == component
                and metadata["namespace"] == namespace
                and metadata["labels"].get("plane-demo/project") == self.config.project,
                "Management Deployment ownership differs",
            )
            self.note("quiesce", namespace + "/" + component)
            if self.execute:
                patch = [
                    {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
                    {"op": "test", "path": "/metadata/labels", "value": metadata["labels"]},
                    {
                        "op": "test",
                        "path": "/spec/replicas",
                        "value": value["spec"].get("replicas", 1),
                    },
                    {"op": "replace", "path": "/spec/replicas", "value": 0},
                ]
                self.kube(
                    "management",
                    "patch",
                    "deployment",
                    component,
                    "--type=json",
                    "-p",
                    json.dumps(patch),
                    namespace=namespace,
                    mutation=True,
                )
                deadline = self.clock() + 180
                while self.rows(
                    self.kube_json(
                        "management",
                        "get",
                        "pods",
                        "-l",
                        "plane-demo/component=" + component,
                        namespace=namespace,
                    )
                ):
                    require(self.clock() < deadline, "Management Pods did not terminate")
                    self.sleep(2)

    def delete_app(self, slot, app):
        self.verify_cluster(slot)
        expected = next(value for value in self.inventories[slot]["apps"] if value["name"] == app)
        current = [
            value for value in self.rows(self.rad(slot, "app", "list")) if value["name"] == app
        ]
        require(current == [expected], "Radius application changed since preflight")
        self.note("radius-app", f"{slot}/{app}")
        if not self.execute:
            return
        self.rad(slot, "app", "delete", app, "--yes", mutation=True)
        require(
            not any(value["name"] == app for value in self.rows(self.rad(slot, "app", "list"))),
            "Radius application deletion is incomplete",
        )
        removed = self.removed.setdefault(slot, set())
        removed.update(
            item["id"].lower()
            for item in self.inventories[slot]["resources"]
            if same_id(item["properties"]["application"], self.app_id(app))
        )
        self.verify_remaining(slot)

    def verify_remaining(self, slot):
        expected = {item["id"].lower() for item in self.inventories[slot]["resources"]}
        remaining = {item["id"].lower() for item in self.native_resources(slot)}
        require(
            remaining == expected - self.removed.get(slot, set()),
            "Radius resources remain, changed, or appeared during cleanup",
        )

    def delete_cluster_owner(self, slot, record):
        from kubernetes import config as kube_config
        from kubernetes.client.exceptions import ApiException

        self.verify_cluster("management")
        current = [
            item
            for item in self.native_resources("management")
            if same_id(item["id"], record["id"])
        ]
        require(current == [record], "Management Radius child owner changed")
        self.note("radius-child", slot)
        if not self.execute:
            return
        target = self.targets["management"]
        self.current()
        require(os.environ.get(self.confirmation) == "yes", "Cleanup confirmation was withdrawn")
        try:
            with kube_config.new_client_from_config(
                config_file=str(target["kubeconfig"]), context=target["context"]
            ) as client:
                require(client.configuration.verify_ssl, "Radius DELETE requires verified TLS")
                client.configuration.proxy = None
                self.before_child_delete(slot, record)
                _, status, _ = client.call_api(
                    "/apis/api.ucp.dev/v1alpha3" + record["id"],
                    "DELETE",
                    query_params=[("api-version", "2025-08-01-preview")],
                    header_params={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    auth_settings=["BearerToken"],
                    response_type="object",
                    _request_timeout=(5, 60),
                    _return_http_data_only=False,
                )
                require(status in {200, 202, 204}, "Radius child DELETE was not accepted")
        except ApiException:
            raise CleanupError(
                "Radius child deletion failed; direct provider deletion is forbidden"
            ) from None
        deadline = self.clock() + 900
        while any(
            same_id(item["id"], record["id"]) for item in self.native_resources("management")
        ):
            require(self.clock() < deadline, "Radius child deletion did not complete")
            self.sleep(3)
        self.removed.setdefault("management", set()).add(record["id"].lower())
        self.verify_remaining("management")

    def before_child_delete(self, slot, record):
        self.verify_cluster("management")

    def fault_target(self, slot, namespace):
        helpers = fault_helpers()
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        target = self.targets[slot]
        return helpers.Target(
            self.config.project,
            slot,
            target["context"],
            target["kubeconfig"],
            self.config.namespace(slot),
            target["cluster_uid"],
            namespace["metadata"]["uid"],
            {
                name: {"deployment": name, "container": name}
                for name in (role + "-api", role + "-reconciler")
            },
            {},
            ownership={
                "plane-demo/project": self.config.project,
                "plane-demo/deployment": self.config.deployment,
                "plane-demo/environment": self.environment,
            },
        )

    def check_journals(self, slot, namespace):
        if namespace is None:
            return None
        helpers = fault_helpers()
        target = self.fault_target(slot, namespace)
        kube = helpers.Kubectl(
            target,
            runner=lambda argv, payload=None, timeout=30: self.call(
                argv,
                payload=payload,
                timeout=timeout,
                env={**self.env, "HOME": str(self.targets[slot]["home"])},
            ),
        )
        for value in self.rows(kube.json("get", "configmaps")):
            metadata = value["metadata"]
            if (
                not metadata["name"].startswith("plane-demo-fault-")
                and metadata.get("labels", {}).get("plane-demo/journal-kind") != "fault"
            ):
                continue
            journal = helpers.ConfigMapJournal(kube, metadata["name"], "fault")
            record = journal.load()
            require(
                record.get("restored") is True and record.get("physical_restored") is True,
                "Restore the owned fault journal before cleanup",
            )
            journal.check()
        return kube

    def clean_radius(self, clusters):
        require(
            "management" in clusters or not clusters,
            "Management Radius is unavailable; direct child cleanup is forbidden",
        )
        if not clusters:
            return
        for slot in [*CHILDREN, "management"]:
            if slot in clusters:
                self.open_cluster(slot)
                self.inventories[slot] = self.inventory(slot)
                self.check_faults(slot)
        owners = self.inventories["management"]["children"]
        require(
            set(clusters) - {"management"} <= set(owners),
            "A child cluster lacks its management Radius owner",
        )
        self.preflight_dependencies(clusters, owners)
        self.quiesce()
        for slot in CHILDREN:
            if slot in clusters:
                for app in self.inventories[slot]["apps"]:
                    self.delete_app(slot, app["name"])
                if self.execute:
                    self.child_apps_absent(slot)
        for slot in CHILDREN:
            if slot in owners:
                if self.execute and slot in clusters:
                    self.child_apps_absent(slot)
                self.delete_cluster_owner(slot, owners[slot])
                if self.execute:
                    self.child_absent(slot, owners[slot])
        for app in sorted(
            self.inventories["management"]["apps"], key=lambda value: value["name"] == "management"
        ):
            self.delete_app("management", app["name"])
        if self.execute:
            require(
                not self.native_resources("management")
                and not self.rows(self.rad("management", "app", "list")),
                "Management Radius owners remain",
            )


class LiveAzureCleanup(LiveClusterCleanup):
    def __init__(self, **kwargs):
        super().__init__(environment="azure", **kwargs)
        self.platform = f"rg-{self.config.stem}-platform"
        self.groups = [self.platform] + [
            self.group_name(slot, kind) for slot in SLOTS for kind in ("app", "cluster", "nodes")
        ]
        require(
            not any(name.startswith("rg-todolist-") for name in self.groups),
            "Legacy rg-todolist resources are protected",
        )
        self.roles = {
            key: f"/subscriptions/{self.config.subscription}/providers/Microsoft.Authorization/"
            "roleDefinitions/"
            + str(
                uuid5(
                    GUID_NAMESPACE,
                    f"/subscriptions/{self.config.subscription}-{self.config.stem}-{value[0]}",
                )
            )
            for key, value in ROLE_NAMES.items()
        }
        self.vault_id = (
            self.gid(self.platform)
            + "/providers/Microsoft.KeyVault/vaults/"
            + self.config.vault_name
            if self.config.key_vault is None
            else None
        )
        self.external = None
        self.group_resources = {}
        self.bootstrap_record = None
        self.partial_foundation = False

    def gid(self, name):
        return f"/subscriptions/{self.config.subscription}/resourceGroups/{name}"

    def group_name(self, slot, kind):
        return f"rg-{self.config.slot_name(slot)}-{kind}"

    def cluster_id(self, slot):
        return self.gid(self.group_name(slot, "cluster")) + (
            "/providers/Microsoft.ContainerService/managedClusters/aks-"
            + self.config.slot_name(slot)
        )

    def tags(self, value):
        return isinstance(value, dict) and all(
            value.get(key) == expected
            for key, expected in {
                "project": self.config.project,
                "deployment": self.config.deployment,
                "environment": "azure",
                "managedBy": "radius-todolist-app",
                "SecurityControl": "Ignore",
            }.items()
        )

    def owns(self, identifier):
        return isinstance(identifier, str) and any(
            same_id(identifier, self.gid(name))
            or identifier.lower().startswith(self.gid(name).lower() + "/")
            for name in self.groups
        )

    def az(self, *args, mutation=False):
        if mutation and self.bootstrap_record is not None:
            self.bootstrap_properties()
        raw = self.call(
            [
                "az",
                *args,
                "--subscription",
                self.config.subscription,
                "--only-show-errors",
                "--output",
                "none" if mutation else "json",
            ],
            mutation=mutation,
            timeout=7200 if mutation else 180,
        )
        return None if mutation else json.loads(raw)

    def external_vault(self):
        if self.config.key_vault is None:
            return []
        candidates = [
            value
            for value in self.rows(self.az("keyvault", "list"))
            if str(value.get("name", "")).lower() == self.config.vault_name
        ]
        require(len(candidates) == 1, "Selected external vault is missing or ambiguous")
        value = candidates[0]
        match = re.fullmatch(
            rf"/subscriptions/{re.escape(self.config.subscription)}/resourceGroups/([^/]+)"
            rf"/providers/Microsoft.KeyVault/vaults/{re.escape(self.config.vault_name)}",
            value["id"],
            re.IGNORECASE,
        )
        require(
            match is not None and not match[1].lower().startswith(f"rg-{self.config.stem}-"),
            "External vault overlaps deployment-owned groups or subscription",
        )
        self.vault_id = value["id"]
        self.external = {"id": self.vault_id, "resourceGroup": match[1], "kind": "external-vault"}
        return [self.external]

    def group(self, name):
        require(name in self.groups, "Unknown Azure group")
        present = self.az("group", "exists", "--name", name)
        require(type(present) is bool, "Invalid Azure existence response")
        if not present:
            return None
        value = self.az("group", "show", "--name", name)
        require(
            value.get("name") == name
            and same_id(value.get("id"), self.gid(name))
            and self.tags(value.get("tags")),
            "Azure group ownership differs",
        )
        resources = self.rows(self.az("resource", "list", "--resource-group", name))
        for item in resources:
            require(
                isinstance(item.get("id"), str)
                and item["id"].lower().startswith(self.gid(name).lower() + "/providers/")
                and (
                    self.tags(item.get("tags"))
                    or (not item.get("tags") and item["type"].lower() in UNTAGGABLE)
                ),
                "Azure resource ownership differs; no group deletion is authorized",
            )
            if item["type"].lower() == "microsoft.keyvault/vaults":
                require(
                    self.config.key_vault is None and same_id(item["id"], self.vault_id),
                    "Refusing deletion of an external or unexpected vault",
                )
            if item["type"].lower() == "microsoft.containerregistry/registries":
                require(
                    same_id(
                        item["id"],
                        self.gid(self.platform)
                        + "/providers/Microsoft.ContainerRegistry/registries/"
                        + self.config.registry_name,
                    ),
                    "Unexpected registry identity",
                )
        return resources

    def unexpected(self):
        groups = self.rows(self.az("group", "list", "--tag", "project=" + self.config.project))
        resources = self.rows(
            self.az("resource", "list", "--tag", "project=" + self.config.project)
        )
        selected_groups, selected_resources = [], []
        for value in groups:
            selected = (
                value.get("tags", {}).get("deployment") == self.config.deployment
                and value.get("tags", {}).get("environment") == "azure"
            )
            named = value.get("name", "").startswith("rg-" + self.config.stem + "-")
            if selected or named:
                require(
                    value.get("name") in self.groups, "Unexpected deployment-owned group; retained"
                )
                require(self.tags(value.get("tags")), "Ambiguous deployment group ownership")
                selected_groups.append(value)
        for value in resources:
            tags = value.get("tags") or {}
            if (
                tags.get("deployment") == self.config.deployment
                and tags.get("environment") == "azure"
            ):
                require(self.owns(value.get("id")), "Unexpected deployment resource; retained")
                selected_resources.append(value)
        return selected_groups, selected_resources

    def bootstrap_properties(self):
        value = self.az("deployment", "sub", "show", "--name", self.config.stem + "-bootstrap")
        require(isinstance(value, dict), "Bootstrap deployment response is missing or malformed")
        properties = value.get("properties")
        require(
            isinstance(properties, dict), "Bootstrap deployment properties are missing or malformed"
        )
        state = properties.get("provisioningState")
        require(
            isinstance(state, str) and state in TERMINAL,
            "Bootstrap deployment is nonterminal or has an invalid state",
        )
        record = json.dumps(
            [
                value.get("id"),
                properties.get("timestamp"),
                properties.get("correlationId"),
                properties.get("provisioningState"),
                properties.get("parameters"),
                properties.get("outputs"),
            ],
            sort_keys=True,
        )
        require(
            self.bootstrap_record is None or self.bootstrap_record == record,
            "Bootstrap deployment changed during cleanup; resources retained",
        )
        return value, properties, record

    def foundation(self):
        value, properties, record = self.bootstrap_properties()
        raw = properties.get("outputs")
        require(raw is None or isinstance(raw, dict), "Bootstrap outputs are malformed")
        outputs = {}
        for key, item in (raw or {}).items():
            require(
                isinstance(item, dict) and "value" in item, "Bootstrap output entry is malformed"
            )
            outputs[key] = item["value"]
        if outputs.get("foundation") is not None:
            require(
                isinstance(outputs["foundation"], dict), "Bootstrap foundation output is malformed"
            )
        if outputs.get("allocations") is not None:
            allocations = outputs["allocations"]
            require(
                isinstance(allocations, list)
                and all(
                    isinstance(item, dict)
                    and isinstance(item.get("slot"), str)
                    and item["slot"] in SLOTS
                    for item in allocations
                ),
                "Bootstrap allocations output is malformed",
            )
        if outputs.get("foundation") is None or outputs.get("allocations") is None:
            require(
                properties["provisioningState"] in {"Failed", "Canceled"},
                "Succeeded bootstrap has missing outputs; ownership must be investigated",
            )
            expected = {
                "projectName": self.config.project,
                "deploymentName": self.config.deployment,
                "environment": "azure",
                "location": self.config.location,
                "registryName": self.config.registry_name,
                "vaultName": self.config.vault_name,
                "deploymentHash": self.config.identity_hash,
                "externalVaultResourceGroup": self.external["resourceGroup"]
                if self.external
                else "",
            }
            parameters = properties.get("parameters")
            require(
                same_id(
                    value.get("id"),
                    f"/subscriptions/{self.config.subscription}"
                    f"/providers/Microsoft.Resources/deployments/{self.config.stem}-bootstrap",
                )
                and isinstance(parameters, dict)
                and all(
                    isinstance(parameters.get(key), dict) and parameters[key].get("value") == wanted
                    for key, wanted in expected.items()
                ),
                "Incomplete bootstrap identity or parameters differ; resources retained",
            )
            partial = outputs.get("foundation") or {}
            for key, wanted in {
                "projectName": self.config.project,
                "deploymentName": self.config.deployment,
                "environment": "azure",
                "resourcePrefix": self.config.stem,
                "subscriptionId": self.config.subscription,
                "vaultId": self.vault_id,
                "vaultOwned": self.config.key_vault is None,
                "roleDefinitionIds": self.roles,
            }.items():
                require(
                    key not in partial or partial[key] == wanted,
                    "Incomplete bootstrap outputs contradict the selected identity",
                )
            for item in outputs.get("allocations") or []:
                slot = item["slot"]
                require(
                    item.get("clusterName") == "aks-" + self.config.slot_name(slot)
                    and item.get("appResourceGroup") == self.group_name(slot, "app")
                    and item.get("clusterResourceGroup") == self.group_name(slot, "cluster")
                    and item.get("nodeResourceGroup") == self.group_name(slot, "nodes"),
                    "Incomplete bootstrap allocations contradict the selected identity",
                )
            self.partial_foundation = True
            self.bootstrap_record = record
            status(
                "warning",
                "Bootstrap has no complete outputs; validating live owners before partial cleanup",
            )
            return
        foundation = outputs["foundation"]
        require(isinstance(foundation, dict), "Bootstrap foundation output is malformed")
        require(
            foundation.get("projectName") == self.config.project
            and foundation.get("deploymentName") == self.config.deployment
            and foundation.get("environment") == "azure"
            and foundation.get("resourcePrefix") == self.config.stem
            and foundation.get("radiusResourceGroup") == self.config.stem
            and foundation.get("subscriptionId") == self.config.subscription
            and foundation.get("vaultOwned") is (self.config.key_vault is None)
            and same_id(foundation.get("vaultId"), self.vault_id)
            and foundation.get("vaultResourceGroup")
            == (self.external["resourceGroup"] if self.external else self.platform)
            and same_id(
                foundation.get("vaultPrivateEndpointId"),
                self.gid(self.platform)
                + f"/providers/Microsoft.Network/privateEndpoints/pe-{self.config.stem}-vault",
            )
            and foundation.get("roleDefinitionIds") == self.roles,
            "Live bootstrap outputs differ from the selected identity",
        )
        allocations = outputs["allocations"]
        require(
            isinstance(allocations, list)
            and len(allocations) == len(SLOTS)
            and all(
                isinstance(item, dict) and isinstance(item.get("slot"), str) for item in allocations
            )
            and {item["slot"] for item in allocations} == set(SLOTS),
            "Bootstrap allocations differ",
        )
        for item in allocations:
            slot = item["slot"]
            require(
                item["clusterName"] == "aks-" + self.config.slot_name(slot)
                and item["appResourceGroup"] == self.group_name(slot, "app")
                and item["clusterResourceGroup"] == self.group_name(slot, "cluster")
                and item["nodeResourceGroup"] == self.group_name(slot, "nodes"),
                "Bootstrap group or cluster ownership differs",
            )
        self.bootstrap_record = record

    def partial_without_clusters(self):
        """No Radius bypass: accept only foundation resource kinds, never orphaned apps/nodes."""
        platform_types = {
            "microsoft.network/virtualnetworks",
            "microsoft.network/publicipaddresses",
            "microsoft.network/natgateways",
            "microsoft.network/networksecuritygroups",
            "microsoft.network/privatednszones",
            "microsoft.network/privatednszones/virtualnetworklinks",
            "microsoft.network/privateendpoints",
            "microsoft.network/networkinterfaces",
            "microsoft.containerregistry/registries",
            "microsoft.keyvault/vaults",
            "microsoft.managedidentity/userassignedidentities",
        }
        prefix = self.config.stem
        platform_names = {
            "microsoft.network/virtualnetworks": {f"vnet-{prefix}"},
            "microsoft.network/publicipaddresses": {f"pip-{prefix}-egress"},
            "microsoft.network/natgateways": {f"nat-{prefix}"},
            "microsoft.network/networksecuritygroups": {f"nsg-{prefix}-gateways"},
            "microsoft.network/privatednszones": {
                f"{prefix}.postgres.database.azure.com",
                "privatelink.redis.azure.net",
                "privatelink.vaultcore.azure.net",
            },
            "microsoft.network/privateendpoints": {f"pe-{prefix}-vault"},
            "microsoft.network/networkinterfaces": {f"nic-{prefix}-vault"},
            "microsoft.containerregistry/registries": {self.config.registry_name},
            "microsoft.keyvault/vaults": {self.config.vault_name},
            "microsoft.managedidentity/userassignedidentities": {
                f"id-{prefix}-coordinator",
                f"id-{prefix}-harness",
            },
        }
        for group in self.groups:
            resources = self.group(group) or []
            if group.endswith(("-app", "-nodes")):
                require(
                    not resources,
                    f"Resources remain without their application/AKS owner: {group}; retained",
                )
                continue
            allowed = (
                platform_types
                if group == self.platform
                else {
                    "microsoft.managedidentity/userassignedidentities",
                }
            )
            for item in resources:
                kind = item["type"].lower()
                require(
                    kind
                    in allowed
                    | {
                        "microsoft.authorization/roleassignments",
                        "microsoft.resources/deployments",
                        "microsoft.managedidentity/userassignedidentities/federatedidentitycredentials",
                    },
                    f"Unexpected partial-bootstrap resource: {item['id']}; retained",
                )
                require(
                    self.tags(item.get("tags")) or kind in UNTAGGABLE,
                    "Partial-bootstrap resource ownership differs",
                )
                if group == self.platform and kind in platform_names:
                    require(
                        item["id"].rsplit("/", 1)[-1] in platform_names[kind],
                        f"Unexpected partial-bootstrap resource identity: {item['id']}; retained",
                    )
                elif kind == "microsoft.managedidentity/userassignedidentities":
                    slot = group.removeprefix(f"rg-{prefix}-").removesuffix("-cluster")
                    require(
                        item["id"].rsplit("/", 1)[-1]
                        in {
                            f"id-{prefix}-{slot}-{purpose}"
                            for purpose in (
                                "control-plane",
                                "kubelet",
                                "radius",
                                "gateway",
                                "certificate-issuer",
                            )
                        },
                        "Unexpected partial-bootstrap cluster identity; retained",
                    )

    def role_state(self):
        found, retained, assignments = {}, [], []
        definitions = self.rows(self.az("role", "definition", "list", "--custom-role-only", "true"))
        for identifier in self.roles.values():
            exact = self.rows(
                self.az("role", "definition", "list", "--name", identifier.rsplit("/", 1)[1])
            )
            require(
                all(same_id(value.get("id"), identifier) for value in exact),
                "Exact role lookup returned a foreign role",
            )
            definitions.extend(exact)
        for value in definitions:
            key = next(
                (
                    key
                    for key, identifier in self.roles.items()
                    if same_id(value.get("id"), identifier)
                ),
                None,
            )
            if key is None:
                require(
                    not value.get("roleName", "").startswith(self.config.stem + " "),
                    "Unknown deployment custom role; retained",
                )
                continue
            expected_scopes = (
                {self.gid(self.platform).lower(), self.vault_id.lower()}
                if key in {"certificateImporter", "acmeStateWriter"}
                else {self.gid(self.group_name(slot, "cluster")).lower() for slot in CHILDREN}
            )
            require(
                value.get("roleType") == "CustomRole"
                and value.get("roleName") == self.config.stem + ROLE_NAMES[key][1][len(PROJECT) :]
                and {scope.lower() for scope in value["assignableScopes"]} == expected_scopes,
                "Custom role ownership or scopes differ",
            )
            found[key] = value
        for value in self.rows(
            self.az("role", "assignment", "list", "--all", "--fill-principal-name", "false")
        ):
            scope = value.get("scope", "")
            external = self.external and (
                same_id(scope, self.vault_id)
                or scope.lower().startswith(self.vault_id.lower() + "/")
            )
            own_role = next(
                (
                    key
                    for key, identifier in self.roles.items()
                    if same_id(value.get("roleDefinitionId"), identifier)
                ),
                None,
            )
            if external:
                retained.append({"id": value["id"], "kind": "external-role-assignment"})
                if own_role in found:
                    retained.append(
                        {"id": found[own_role]["id"], "kind": "retained-role-definition"}
                    )
                continue
            if not self.owns(scope):
                require(own_role is None, "Deployment custom role has an unowned assignment scope")
                continue
            prefix = scope.rstrip("/") + "/providers/Microsoft.Authorization/roleAssignments/"
            require(
                value.get("id", "").lower().startswith(prefix.lower())
                and str(UUID(value["id"][len(prefix) :])) == value["id"][len(prefix) :].lower(),
                "Role assignment ID differs from its owned scope",
            )
            assignments.append(value)
        return found, assignments, list({item["id"]: item for item in retained}.values())

    def clusters(self):
        result = {}
        for slot in SLOTS:
            group = self.group_name(slot, "cluster")
            if self.group_resources[group] is None:
                continue
            entries = self.rows(self.az("aks", "list", "--resource-group", group))
            require(len(entries) <= 1, "Unexpected AKS in selected group")
            listed_ids = {
                item["id"].lower()
                for item in self.group_resources[group]
                if item["type"].lower() == "microsoft.containerservice/managedclusters"
            }
            require(
                listed_ids == {item["id"].lower() for item in entries},
                "Azure resource and AKS inventories disagree",
            )
            if entries:
                value = entries[0]
                require(
                    same_id(value.get("id"), self.cluster_id(slot))
                    and value.get("name") == "aks-" + self.config.slot_name(slot)
                    and value.get("nodeResourceGroup") == self.group_name(slot, "nodes")
                    and self.tags(value.get("tags"))
                    and value.get("provisioningState") == "Succeeded",
                    "AKS identity, ownership, or readiness differs",
                )
                result[slot] = value
        return result

    def environment_scope(self, slot, app, properties):
        child = app.removeprefix("cluster-") if app.startswith("cluster-") else None
        expected = self.gid(self.group_name(child or slot, "cluster" if child else "app"))
        require(
            same_id(properties["providers"]["azure"]["scope"], expected),
            "Radius environment targets an unowned Azure group",
        )

    def resource_scope(self, value):
        if isinstance(value, str) and value.lower().startswith("/subscriptions/"):
            require(
                self.owns(value)
                or same_id(value, self.vault_id)
                or same_id(value, "/subscriptions/" + self.config.subscription),
                "Radius references an unowned Azure resource",
            )
        elif isinstance(value, dict):
            for child in value.values():
                self.resource_scope(child)
        elif isinstance(value, list):
            for child in value:
                self.resource_scope(child)

    def cluster_record(self, slot, properties):
        require(
            not properties.get("clusterId")
            or same_id(properties["clusterId"], self.cluster_id(slot)),
            "Radius child points at a different AKS",
        )

    def check_faults(self, slot):
        namespace = self.namespace(slot)
        kube = self.check_journals(slot, namespace)
        if kube is not None:
            kube.policies(reject_faults=True)

    def preflight_dependencies(self, clusters, owners):
        for slot in SLOTS:
            require(
                slot in clusters or not self.group_resources[self.group_name(slot, "app")],
                "App resources lack a reachable Radius owner",
            )
            if self.group_resources[self.group_name(slot, "app")]:
                role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
                require(
                    any(app["name"] == role for app in self.inventories[slot]["apps"]),
                    f"{slot}: app resources have no Radius application owner",
                )

    def child_apps_absent(self, slot):
        require(
            not self.rows(self.rad(slot, "app", "list")) and not self.native_resources(slot),
            "Child Radius workloads remain",
        )
        remaining = self.group(self.group_name(slot, "app"))
        require(
            not remaining,
            "Radius left app resources; direct deletion is forbidden: "
            + ", ".join(item["id"] for item in remaining or []),
        )

    def child_absent(self, slot, record):
        deadline = self.clock() + 900
        while self.rows(
            self.az("aks", "list", "--resource-group", self.group_name(slot, "cluster"))
        ):
            require(self.clock() < deadline, "Child AKS remains after Radius deletion")
            self.sleep(3)

    def delete_group(self, name):
        if self.partial_foundation and not self.targets:
            self.partial_without_clusters()
        resources = self.group(name)
        if resources is None:
            return
        if self.execute:
            require(
                not any(
                    value["type"].lower() == "microsoft.containerservice/managedclusters"
                    for value in resources
                ),
                "A cluster remains; group deletion would bypass its owner",
            )
        self.note("bootstrap-group", name)
        if self.execute:
            self.az("group", "delete", "--name", name, "--yes", mutation=True)
            require(
                self.az("group", "exists", "--name", name) is False,
                "Azure group deletion is incomplete",
            )

    def clean(self, *, radius_only=False):
        status("section", "Cleanup: discover and validate live ownership")
        self.external_vault()
        self.unexpected()
        self.group_resources = {name: self.group(name) for name in self.groups}
        definitions, assignments, retained = self.role_state()
        if any(value is not None for value in self.group_resources.values()):
            self.foundation()
        clusters = self.clusters()
        if not clusters:
            require(
                not any(self.group_resources[self.group_name(slot, "app")] for slot in SLOTS),
                "App resources remain without Radius; normal cleanup cannot bypass it",
            )
            if self.partial_foundation:
                self.partial_without_clusters()
        status("section", "Cleanup: remove applications and children through Radius")
        self.clean_radius(clusters)
        if self.execute:
            remaining = self.group(self.group_name("management", "app"))
            require(
                not remaining,
                "Radius left management app resources: "
                + ", ".join(item["id"] for item in remaining or []),
            )
        if radius_only:
            return {
                "status": "radius_resources_removed" if self.execute else "planned",
                "foundationRetained": True,
                "steps": self.steps,
            }
        status("section", "Cleanup: remove owned foundation resources")
        for slot in CHILDREN:
            self.delete_group(self.group_name(slot, "app"))
            self.delete_group(self.group_name(slot, "cluster"))
            self.delete_group(self.group_name(slot, "nodes"))
        self.delete_group(self.group_name("management", "app"))
        if "management" in clusters:
            self.note("bootstrap-management-aks", self.cluster_id("management"))
            if self.execute:
                self.verify_cluster("management")
                current = self.rows(
                    self.az(
                        "aks", "list", "--resource-group", self.group_name("management", "cluster")
                    )
                )
                require(
                    len(current) == 1 and current[0] == clusters["management"],
                    "Management AKS changed",
                )
                self.az(
                    "aks",
                    "delete",
                    "--name",
                    "aks-" + self.config.slot_name("management"),
                    "--resource-group",
                    self.group_name("management", "cluster"),
                    "--yes",
                    mutation=True,
                )
                require(
                    not self.rows(
                        self.az(
                            "aks",
                            "list",
                            "--resource-group",
                            self.group_name("management", "cluster"),
                        )
                    ),
                    "Management AKS deletion is incomplete",
                )
        if self.execute or "management" not in clusters:
            self.delete_group(self.group_name("management", "cluster"))
            self.delete_group(self.group_name("management", "nodes"))
        else:
            self.note("bootstrap-group", self.group_name("management", "cluster"))
            self.note("bootstrap-group", self.group_name("management", "nodes"))
        self.delete_group(self.platform)
        if self.execute:
            definitions, assignments, retained = self.role_state()
        for assignment in assignments:
            self.note("owned-role-assignment", assignment["id"])
            if self.execute:
                self.az("role", "assignment", "delete", "--ids", assignment["id"], mutation=True)
        retained_ids = {value["id"].lower() for value in retained}
        for value in definitions.values():
            if value["id"].lower() in retained_ids:
                continue
            self.note("bootstrap-role-definition", value["id"])
            if self.execute:
                self.az(
                    "role",
                    "definition",
                    "delete",
                    "--name",
                    value["id"].rsplit("/", 1)[1],
                    "--custom-role-only",
                    "true",
                    mutation=True,
                )
        return (
            self.verify()
            if self.execute
            else {
                "status": "planned",
                "steps": self.steps,
                "retainedExternalObjects": ([self.external] if self.external else []) + retained,
            }
        )

    def verify(self):
        status("section", "Cleanup: verify owned active resources are absent")
        retained = self.external_vault()
        remaining = []
        for name in self.groups:
            exists = self.az("group", "exists", "--name", name)
            require(type(exists) is bool, "Invalid Azure existence response")
            if exists:
                remaining.append(name)
        require(not remaining, "Owned Azure groups remain: " + ", ".join(remaining))
        definitions, assignments, external_roles = self.role_state()
        retained.extend(external_roles)
        allowed = {item["id"].lower() for item in retained}
        require(
            not assignments
            and all(value["id"].lower() in allowed for value in definitions.values()),
            "Owned custom roles or role assignments remain",
        )
        groups, resources = self.unexpected()
        require(not groups and not resources, "Owned Azure resources remain")
        tombstones = []
        if self.config.key_vault is None:
            for value in self.rows(self.az("keyvault", "list-deleted")):
                if value.get("name") == self.config.vault_name:
                    require(
                        same_id(value["properties"].get("vaultId"), self.vault_id),
                        "Soft-deleted vault identity differs",
                    )
                    tombstones.append(
                        {
                            "id": self.vault_id,
                            "scheduledPurgeDate": value["properties"].get("scheduledPurgeDate"),
                        }
                    )
        return {
            "status": "clean",
            "scope": "owned-active-resources",
            "environment": "azure",
            "subscriptionId": self.config.subscription,
            "deployment": self.config.stem,
            "retainedExternalObjects": retained,
            "softDeletedVaults": tombstones,
            "purged": False,
        }


def legacy_main(*, verify_only: bool = False) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only verification of the exact Azure ownership manifest."
        if verify_only
        else __doc__
    )
    parser.add_argument("--environment", choices=["azure"], default="azure")
    parser.add_argument("--manifest", default="bootstrap.outputs.json")
    if not verify_only:
        parser.add_argument("--targets", default="cleanup-targets.json")
        parser.add_argument("--radius-config", default="radius.yaml")
        parser.add_argument("--execute", action="store_true")
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--provider-only", action="store_true")
        modes.add_argument(
            "--radius-only",
            action="store_true",
            help="Remove Radius-owned apps and child AKS; retain management AKS and foundation",
        )
        parser.add_argument("--credential-file", action="append", default=[])
    args = parser.parse_args()
    try:
        require(
            verify_only or not args.radius_only or not args.credential_file,
            "Radius-only cleanup retains local credentials and evidence",
        )
        manifest = Manifest(json.loads(state_file(ROOT, args.manifest).read_text()))
        targets = {}
        if not verify_only and not args.provider_only:
            target_file = state_file(ROOT, args.targets, exists=False)
            if target_file.exists():
                targets = json.loads(target_file.read_text())
                require(targets.get("version") == 1, "Unsupported cleanup target version")
                targets = targets["targets"]
        engine = Cleanup(
            manifest,
            root=ROOT,
            targets=targets,
            radius_config=None if verify_only else Path(args.radius_config),
            execute=not verify_only and args.execute,
            provider_only=not verify_only and args.provider_only,
            radius_only=not verify_only and args.radius_only,
        )
        credentials = []
        if not verify_only:
            allowed = {"credentials.json", "kubeconfig", "radius.yaml"} | {
                f"{slot}{suffix}"
                for slot in manifest.allocations
                for suffix in (".key", ".kubeconfig")
            }
            for value in args.credential_file:
                path = state_file(ROOT, value, exists=False)
                require(
                    path.parent == ROOT / ".state/azure" and path.name in allowed,
                    "Only named top-level credential files may be removed",
                )
                credentials.append(path)
        result = engine.verify() if verify_only else engine.clean()
        if not verify_only and args.execute and not args.radius_only:
            require(
                result.get("status") == "clean", "Credential cleanup requires verified deletion"
            )
            for path in credentials:
                if path.exists():
                    require(not path.is_symlink() and path.is_file(), "Credential path changed")
                    path.unlink()
        if credentials:
            result["credentialFiles"] = [str(path.relative_to(ROOT)) for path in credentials]
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        print(
            "Cleanup incomplete: interrupted. An Azure operation may still be running and "
            "resources may remain. Local credential removal may be partial; inspect state "
            "and verify cleanup before retrying.",
            file=sys.stderr,
        )
        return 130
    except (
        CleanupError,
        ProvisioningError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OSError,
    ) as exc:
        print(f"Cleanup incomplete: {exc}", file=sys.stderr)
        return 1


def main(argv=None, *, verify_only=False, engine_factory=LiveAzureCleanup):
    parser = argparse.ArgumentParser(
        description="Cleanup and verify the selected .env deployment using live owners."
    )
    parser.add_argument("--environment", choices=("azure",), default="azure")
    if not verify_only:
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--radius-only", action="store_true")
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = engine_factory(execute=not verify_only and args.execute)
        result = engine.verify() if verify_only else engine.clean(radius_only=args.radius_only)
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        print(
            "Cleanup incomplete: interrupted; verify live ownership before retrying.",
            file=sys.stderr,
        )
        return 130
    except (
        CleanupError,
        ConfigError,
        ProvisioningError,
        fault_helpers().AcceptanceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        status("error", f"Cleanup incomplete: {error}")
        return 1
    finally:
        if engine is not None:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(run_main(main, "Azure cleanup"))
