#!/usr/bin/env python3
"""Preview or execute manifest-checked Azure teardown, Radius owners first."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from plane_demo.management.providers.commands import Commands
from plane_demo.management.provisioning import ProvisioningError

ROOT = Path(__file__).resolve().parents[1]
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
        return self.verify() if self.execute else {"status": "planned", "steps": self.steps}

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


def main(*, verify_only: bool = False) -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
