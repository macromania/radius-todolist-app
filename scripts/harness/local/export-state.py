#!/usr/bin/env python3
"""Read-only, progressive local acceptance export from owned Radius access Secrets."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import yaml

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / ".state/local"
sys.path.insert(0, str(ROOT / "src"))

from plane_demo.management.providers.local_config import LocalConfig, same_radius_id  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


shared = load("plane_demo_local_export_shared", ROOT / "scripts/harness/export-state.py")
acceptance = load("plane_demo_local_acceptance_sources", ROOT / "scripts/harness/test-e2e.py")
network = sys.modules.get("plane_demo_local_fault_" + acceptance.faults.__name__)
if network is None:
    acceptance.faults.fault_class(type("Local", (), {"environment": "local"})())
    network = sys.modules["plane_demo_local_fault_" + acceptance.faults.__name__]
Error, Pending, require = shared.ExportError, shared.Pending, shared.require
SLOTS = network.SLOTS
RESOURCE_PREFIX = "/planes/radius/local/resourceGroups/radplanes-local/providers"
RESOURCE_TYPE = "Demo.Platform/clusters"
ACCESS_NAMESPACE = "radplanes-local-access"


def private_file(path):
    path = ROOT / path
    require(path.is_relative_to(STATE), "local_state_path_refused")
    require(
        all(not part.is_symlink() for part in [path, *path.parents]),
        "local_state_symlink_refused",
    )
    require(
        path.is_file()
        and stat.S_IMODE(path.stat().st_mode) == 0o600
        and path.stat().st_size <= 2_000_000,
        "local_state_file_not_private",
    )
    return path


def read_private(path):
    return shared.read_json(private_file(path))


def immutable(path, text):
    require(path.is_relative_to(STATE), "local_state_path_refused")
    if path.exists() or path.is_symlink():
        existing = private_file(path).read_text()
        require(hmac.compare_digest(existing, text), "local_export_immutable_file_changed")
    else:
        shared.private_write(path, text)


def kube_profile(value, context, server, *, child):
    require(
        isinstance(value, dict)
        and value.get("apiVersion") == "v1"
        and value.get("kind") == "Config"
        and value.get("current-context") == context
        and len(value.get("contexts", [])) == 1
        and len(value.get("clusters", [])) == 1
        and len(value.get("users", [])) == 1,
        "local_kubeconfig_shape_mismatch",
    )
    selected, cluster, user = value["contexts"][0], value["clusters"][0], value["users"][0]
    require(
        selected.get("name") == context
        and selected.get("context", {}).get("cluster") == cluster.get("name")
        and selected["context"].get("user") == user.get("name"),
        "local_kubeconfig_context_mismatch",
    )
    fields, credential = cluster["cluster"], user["user"]
    require(
        set(fields) <= {"server", "certificate-authority-data", "tls-server-name"}
        and fields.get("server") == server
        and (not child or fields.get("tls-server-name") == context)
        and fields.get("tls-server-name") in (None, context)
        and set(credential) == {"client-certificate-data", "client-key-data"},
        "local_kubeconfig_transport_mismatch",
    )
    try:
        ca = base64.b64decode(fields["certificate-authority-data"], validate=True)
        certificate = base64.b64decode(credential["client-certificate-data"], validate=True)
        key = base64.b64decode(credential["client-key-data"], validate=True)
    except (KeyError, ValueError):
        raise Error("local_kubeconfig_certificate_invalid") from None
    require(
        ca.startswith(b"-----BEGIN CERTIFICATE-----")
        and certificate.startswith(b"-----BEGIN CERTIFICATE-----")
        and b"PRIVATE KEY-----" in key[:64],
        "local_kubeconfig_certificate_invalid",
    )
    return hashlib.sha256(ca).hexdigest()


class Exporter(shared.Exporter):
    def __init__(
        self,
        config=STATE / "provisioning.json",
        *,
        execute=None,
        health=shared.healthy_gateway,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.config_path = private_file(config)
        require(self.config_path == STATE / "provisioning.json", "local_provisioning_path_refused")
        self.state = STATE
        require(stat.S_IMODE(STATE.stat().st_mode) == 0o700, "local_state_not_private")
        self.config = read_private(self.config_path)
        self.local_config = LocalConfig.from_dict(self.config)
        self.allocations = self.local_config.allocations
        self.images = dict(self.local_config.images)
        self.execute = execute or subprocess.run
        self.health, self.clock, self.sleep = health, clock, sleep
        self.deadline = None
        self.accesses, self.identities, self.resources = {}, {}, {}
        self.work = self.state / ".export-state-work" / uuid4().hex
        self.previous = (
            read_private(STATE / "acceptance.json")
            if (STATE / "acceptance.json").exists()
            else None
        )
        if self.previous:
            require(
                self.previous.get("version") == 1
                and self.previous.get("project") == "radplanes"
                and self.previous.get("environment") == "local",
                "existing_acceptance_identity_mismatch",
            )
        self.tenants = dict(shared.SHOWCASE)
        self.targets = dict(self.previous.get("targets", {})) if self.previous else {}
        require(set(self.targets) <= set(SLOTS), "existing_target_outside_allocation")
        self.endpoints = {"pairs": {}}
        if self.previous:
            relative = self.previous.get("endpoints_file", "")
            require(
                isinstance(relative, str)
                and acceptance.re.fullmatch(
                    r"exported-state/[a-f0-9]{32}/endpoints\.json", relative
                ),
                "local_export_generation_invalid",
            )
            self.endpoints = read_private(STATE / relative)
            require(self.previous.get("tenants") == self.tenants, "local_showcase_names_changed")
            self.validate_previous()
        elif (STATE / "endpoints.json").exists():
            raise Error("local_endpoints_without_owned_generation")
        self.review = None
        self.work.mkdir(parents=True, mode=0o700)

    def run_command(self, argv, *, timeout=30, missing=False, binary=False):
        require(not missing, "local_ambiguous_missing_detection_refused")
        if self.deadline is not None:
            remaining = self.deadline - self.clock()
            require(remaining > 0, "export_timeout")
            timeout = min(timeout, remaining)
        # Explicit transports and an isolated HOME; never inherit proxy/cloud CLI overrides.
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(STATE / "home"),
            "LC_ALL": "C",
            "DOCKER_HOST": network.docker_host(),
        }
        try:
            result = self.execute(
                argv,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=not binary,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise Error("operator_command_unavailable_or_timeout") from None
        require(result.returncode == 0, "operator_command_failed")
        return result.stdout

    def validate_previous(self):
        expected_pairs = {slot.rsplit("-", 1)[0] for slot in self.targets if slot != "management"}
        require(
            set(self.endpoints.get("pairs", {})) <= expected_pairs, "local_endpoint_pair_invalid"
        )
        for slot, target in self.targets.items():
            allocation = self.allocations[slot]
            role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
            require(
                target.get("context") == allocation["context"]
                and target.get("namespace") == f"radplanes-local-{slot}-{role}"
                and target.get("kubeconfig") == slot + ".kubeconfig"
                and target.get("cluster_id") == self.cluster_id(slot),
                "local_published_target_invalid",
            )
            private_file(STATE / target["kubeconfig"])
            entry = (
                self.endpoints.get("management")
                if slot == "management"
                else self.endpoints.get("pairs", {}).get(slot.rsplit("-", 1)[0], {}).get(role)
            )
            require(
                isinstance(entry, dict)
                and entry.get("key_file") == slot + ".key"
                and entry.get("url") == f"http://127.0.0.1:{allocation['gatewayPort']}",
                "local_published_endpoint_invalid",
            )
            require(
                bool(
                    shared.KEY.fullmatch(
                        private_file(STATE / entry["key_file"]).read_text().strip()
                    )
                ),
                "invalid_existing_api_key",
            )

    def verify_images(self):
        review = read_private(STATE / "runtime-images.json")
        source = acceptance.faults.source_metadata()
        require(
            source["worktree_dirty"] is False
            and not self.run_command(
                [
                    "git",
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    "scripts",
                    "infra",
                    "images",
                    ".dockerignore",
                    "scripts/harness/local",
                ]
            ).strip()
            and review.get("version") == 1
            and review.get("content_verified") is True
            and review.get("source_revision") == source["commit"]
            and acceptance.report_time(source["committed_at"])
            <= acceptance.report_time(review.get("inspected_at"))
            <= acceptance.report_time(shared.now()),
            "local_image_review_not_current",
        )
        for role in ("api", "provisioner"):
            image = review.get(role, {})
            require(
                image.get("reference") == self.images[role]
                and image.get("image_id") == self.local_config.image_ids[role]
                and image.get("source_hashes") == acceptance.local_source_hashes(role),
                "local_review_source_or_image_mismatch",
            )
            inspected = json.loads(
                self.run_command(network.docker("image", "inspect", self.images[role]))
            )
            require(
                len(inspected) == 1
                and inspected[0].get("Id") == image["image_id"]
                and inspected[0].get("Architecture") == review.get("architecture")
                and inspected[0].get("Os") == "linux",
                "local_reviewed_image_changed",
            )
        require(
            self.review is None or review == self.review, "local_image_review_changed_during_export"
        )
        self.review = review

    def cluster_id(self, slot):
        return self.local_config.expected_cluster_id(slot)

    def node(self, slot):
        expected = self.targets.get(slot, {}).get("local", {}).get("node")
        return network.node_identity(self.run_command, slot, expected)

    def resource_inventory(self, management):
        path = (
            "/apis/api.ucp.dev/v1alpha3"
            + RESOURCE_PREFIX
            + "/"
            + RESOURCE_TYPE
            + "?api-version=2025-08-01-preview"
        )
        value = json.loads(self.kube(management, "get", "--raw", path))
        require(
            isinstance(value, dict) and isinstance(value.get("value"), list),
            "local_radius_inventory_invalid",
        )
        result = {}
        for item in value["value"]:
            slot = item.get("name")
            require(slot in SLOTS[1:] and slot not in result, "local_radius_allocation_mismatch")
            properties = item.get("properties", {})
            require(
                same_radius_id(item.get("id"), RESOURCE_PREFIX + "/" + RESOURCE_TYPE + "/" + slot)
                and item.get("type") == RESOURCE_TYPE
                and properties.get("slot") == slot
                and same_radius_id(
                    properties.get("environment"),
                    RESOURCE_PREFIX + "/Applications.Core/environments/provision-" + slot,
                )
                and same_radius_id(
                    properties.get("application"),
                    RESOURCE_PREFIX + "/Applications.Core/applications/cluster-" + slot,
                ),
                "local_radius_resource_identity_mismatch",
            )
            require(
                properties.get("provisioningState")
                in {"Accepted", "Creating", "Updating", "Succeeded"},
                "local_radius_cluster_failed_or_invalid",
            )
            result[slot] = item
        return result

    def child_config(self, management, slot, node):
        resource = self.resources.get(slot)
        if resource is None:
            raise Pending("radius_cluster_not_created")
        properties = resource["properties"]
        status = properties.get("provisioningState")
        require(status not in ("Failed", "Canceled", "Deleting"), "local_radius_cluster_failed")
        if status != "Succeeded":
            raise Pending("radius_cluster_not_ready")
        name = "radplanes-local-" + slot + "-access"
        require(
            properties.get("clusterId") == self.cluster_id(slot)
            and properties.get("clusterName") == "radplanes-local-" + slot
            and properties.get("bootstrapAccessRef")
            == f"kubernetes://{ACCESS_NAMESPACE}/{name}#kubeconfig",
            "local_radius_bootstrap_access_mismatch",
        )
        output = self.kube(
            management,
            "-n",
            ACCESS_NAMESPACE,
            "get",
            "secret",
            name,
            "--ignore-not-found",
            "-o",
            'jsonpath={.metadata.uid}{"\\n"}{.metadata.labels}{"\\n"}'
            '{.metadata.annotations}{"\\n"}{.data.kubeconfig}',
        )
        if not output.strip():
            raise Pending("radius_access_secret_not_created")
        lines = output.splitlines()
        require(len(lines) == 4, "local_access_secret_fields_missing")
        uid = str(UUID(lines[0]))
        require(
            json.loads(lines[1]).get("radplanes.local/slot") == slot
            and same_radius_id(
                json.loads(lines[2]).get("radplanes.local/radius-resource"), resource["id"]
            ),
            "local_access_secret_ownership_mismatch",
        )
        try:
            value = yaml.safe_load(base64.b64decode(lines[3], validate=True))
        except (ValueError, yaml.YAMLError):
            raise Error("local_access_kubeconfig_invalid") from None
        ca = kube_profile(
            value, "radplanes-local-" + slot, f"https://{node['address']}:6443", child=True
        )
        return value, {
            "access_secret": name,
            "access_secret_uid": uid,
            "ca_sha256": ca,
            "radius_resource_id": resource["id"],
        }

    def access(self, slot):
        allocation = self.allocations[slot]
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        node = self.node(slot)
        if slot == "management":
            owned = read_private(STATE / "management-created.json")
            require(
                owned.get("secretEncryptionVerified") is True
                and owned.get("name") == owned.get("context") == allocation["clusterName"]
                and owned.get("nodeId") == node["id"]
                and owned.get("nodeAddress")
                == node["address"]
                == self.local_config.management_cluster["nodeAddress"],
                "local_management_bootstrap_mismatch",
            )
            value = yaml.safe_load(private_file(STATE / "home/.kube/config").read_text())
            ca = kube_profile(value, allocation["context"], "https://127.0.0.1:35495", child=False)
            require(
                ca == self.local_config.management_cluster["caSHA256"],
                "local_management_ca_mismatch",
            )
            identity = {"ca_sha256": ca}
        else:
            value, identity = self.child_config(self.accesses["management"], slot, node)
        value["clusters"][0]["cluster"]["server"] = f"https://127.0.0.1:{allocation['apiPort']}"
        profile = json.dumps(value, sort_keys=True, indent=2) + "\n"
        identity["kubeconfig_sha256"] = hashlib.sha256(profile.encode()).hexdigest()
        path = STATE / (slot + ".kubeconfig")
        immutable(path, profile)
        access = shared.Access(
            slot,
            allocation["context"],
            f"radplanes-local-{slot}-{role}",
            path,
            self.cluster_id(slot),
            "",
        )
        access.cluster_uid = str(
            UUID(self.kube_json(access, "get", "namespace", "kube-system")["metadata"]["uid"])
        )
        if slot == "management":
            require(
                access.cluster_uid == self.local_config.management_cluster["uid"],
                "local_management_cluster_uid_mismatch",
            )
        identity.update(
            node=node, management_node=self.identities.get("management", {}).get("node", node)
        )
        previous = self.targets.get(slot)
        if previous:
            require(
                previous["cluster_uid"] == access.cluster_uid
                and all(previous["local"].get(key) == val for key, val in identity.items()),
                "local_published_cluster_identity_changed",
            )
        self.identities[slot], self.accesses[slot] = identity, access
        return access

    def component(self, access, component, deployments):
        try:
            names, pod = super().component(access, component, deployments)
        except Pending as error:
            if str(error) == "component_image_not_deployed":
                raise Error("local_component_image_mismatch") from None
            raise
        require(
            pod["metadata"].get("namespace") == access.namespace
            and pod["spec"].get("serviceAccountName")
            == ("data-api-runtime" if component == "data-api" else component)
            and pod["spec"].get("nodeName") == self.identities[access.slot]["node"]["name"],
            "local_workload_identity_mismatch",
        )
        return names, pod

    def containerd_content(self, slot, digest):
        require(
            isinstance(digest, str) and acceptance.re.fullmatch(r"sha256:[a-f0-9]{64}", digest),
            "local_containerd_content_id_invalid",
        )
        raw = self.run_command(
            network.docker(
                "exec",
                self.identities[slot]["node"]["id"],
                "ctr",
                "--namespace",
                "k8s.io",
                "content",
                "get",
                digest,
            ),
            binary=True,
        )
        require(
            isinstance(raw, bytes)
            and len(raw) <= 2_000_000
            and "sha256:" + hashlib.sha256(raw).hexdigest() == digest,
            "local_containerd_content_hash_mismatch",
        )
        try:
            value = json.loads(raw)
        except ValueError:
            raise Error("local_containerd_content_not_json") from None
        require(isinstance(value, dict), "local_containerd_content_not_object")
        return value

    def verify_image_mapping(self, slot, running, expected):
        inspected = json.loads(
            self.run_command(
                network.docker(
                    "exec",
                    self.identities[slot]["node"]["id"],
                    "crictl",
                    "inspecti",
                    running.removeprefix("docker-pullable://").removeprefix("containerd://"),
                )
            )
        )
        reported = inspected.get("status", {}).get("id")
        require(
            isinstance(reported, str) and acceptance.re.fullmatch(r"sha256:[a-f0-9]{64}", reported),
            "local_running_image_mapping_mismatch",
        )
        if reported != expected:
            manifest = self.containerd_content(slot, reported)
            if manifest.get("mediaType") in {
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
            }:
                require(
                    manifest.get("schemaVersion") == 2
                    and isinstance(manifest.get("manifests"), list),
                    "local_running_image_mapping_mismatch",
                )
                native = [
                    item
                    for item in manifest["manifests"]
                    if item.get("platform", {}).get("os") == "linux"
                    and item.get("platform", {}).get("architecture") == self.review["architecture"]
                ]
                require(len(native) == 1, "local_native_image_manifest_ambiguous")
                manifest = self.containerd_content(slot, native[0].get("digest"))
            require(
                manifest.get("schemaVersion") == 2
                and manifest.get("mediaType")
                in {
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                }
                and manifest.get("config", {}).get("digest") == expected,
                "local_running_image_mapping_mismatch",
            )
            self.containerd_content(slot, expected)
        return {"running_image_id": running, "image_id": expected}

    def collect_plane(self, slot):
        access = self.access(slot)
        namespace = self.kube_json(access, "get", "namespace", access.namespace, optional=True)
        namespace_uid = str(UUID(namespace["metadata"]["uid"]))
        role = "management" if slot == "management" else slot.rsplit("-", 1)[1]
        deployments = self.kube_json(access, "get", "deployments")
        require(isinstance(deployments.get("items"), list), "local_deployment_inventory_invalid")
        components, pods, image_ids = {}, {}, {}
        for name in shared.component_names(role):
            components[name], pods[name] = self.component(access, name, deployments["items"])
            image_role = "provisioner" if name == "provisioner" else "api"
            pod = pods[name]
            statuses = [
                item
                for item in pod["status"].get("containerStatuses", [])
                if item["name"] == components[name]["container"]
            ]
            require(
                len(statuses) == 1 and statuses[0].get("ready") is True,
                "local_running_image_status_missing",
            )
            running = statuses[0].get("imageID", "")
            require(
                isinstance(running, str) and acceptance.re.search(r"sha256:[a-f0-9]{64}$", running),
                "local_running_image_id_invalid",
            )
            image_id = self.review[image_role]["image_id"]
            image_ids[name] = self.verify_image_mapping(slot, running, image_id)
            actual = self.pod_json(
                access,
                components[name],
                pod,
                acceptance.SOURCE_PROBE,
                json.dumps(list(self.review[image_role]["source_hashes"])),
            )
            require(
                actual.get("files") == self.review[image_role]["source_hashes"],
                "local_running_source_hash_mismatch",
            )
            if name == "data-api":
                require(actual.get("parent_dsn_present") is False, "data_api_has_parent_dsn")
            confirmed = self.kube_json(access, "get", "pod", pod["metadata"]["name"])
            require(
                confirmed["metadata"]["uid"] == pod["metadata"]["uid"],
                "local_workload_changed_during_export",
            )
        local = {**self.identities[slot], "image_ids": image_ids}
        target = {
            "context": access.context,
            "kubeconfig": slot + ".kubeconfig",
            "namespace": access.namespace,
            "cluster_id": access.cluster_id,
            "cluster_uid": access.cluster_uid,
            "namespace_uid": namespace_uid,
            "components": components,
            "local": local,
        }
        if role != "management":
            parent_slot = (
                "management" if role == "control" else slot.removesuffix("-data") + "-control"
            )
            parent_node = self.node(parent_slot)
            component = role + "-reconciler"
            parent = self.pod_json(
                access,
                components[component],
                pods[component],
                shared.PARENT_PROBE,
                "MANAGEMENT_DSN" if role == "control" else "CONTROL_DSN",
            )
            require(
                parent == {"host": parent_node["address"], "port": 31543},
                "local_parent_endpoint_mismatch",
            )
            target["parent"] = {**parent, "allowed_cidrs": [parent_node["address"] + "/32"]}
            local["parent_node"] = parent_node
        url = f"http://127.0.0.1:{self.allocations[slot]['gatewayPort']}"
        self.health(url)
        key = self.demo_key(access, role)
        require(
            self.kube_json(access, "get", "namespace", access.namespace)["metadata"]["uid"]
            == namespace_uid,
            "namespace_changed_during_export",
        )
        require(
            slot not in self.targets or self.targets[slot] == target,
            "published_target_identity_changed",
        )
        return shared.Plane(
            target,
            url,
            key,
            access,
            pods[role + "-api"]["metadata"]["name"],
            components[role + "-api"]["container"],
        )

    def publish(self, planes):
        endpoints, targets = json.loads(json.dumps(self.endpoints)), dict(self.targets)
        for slot, plane in planes.items():
            key_name = slot + ".key"
            immutable(STATE / key_name, plane.demo_key + "\n")
            entry = {"url": plane.url, "key_file": key_name}
            if slot == "management":
                endpoints["management"] = entry
            else:
                pair, role = slot.rsplit("-", 1)
                endpoints.setdefault("pairs", {}).setdefault(pair, {})[role] = entry
            targets[slot] = plane.target
        require("management" in targets, "management_must_be_exported_first")
        if (
            self.previous
            and endpoints == self.endpoints
            and targets == self.targets
            and self.previous.get("local_images") == self.review
        ):
            shared.write_json(STATE / "endpoints.json", endpoints)
            return
        generation = uuid4().hex
        relative = f"exported-state/{generation}/endpoints.json"
        immutable(STATE / relative, json.dumps(endpoints, sort_keys=True, indent=2) + "\n")
        value = {
            "version": 1,
            "environment": "local",
            "project": "radplanes",
            "synthetic_data": True,
            "endpoints_file": relative,
            "onboarding_timeout_seconds": 3600,
            "tenants": self.tenants,
            "targets": targets,
            "images": self.images,
            "local_images": self.review,
            "export": {"generation": generation, "observed_at": shared.now()},
        }
        shared.write_json(STATE / "acceptance.json", value)
        shared.write_json(STATE / "endpoints.json", endpoints)
        self.previous, self.targets, self.endpoints = value, targets, endpoints

    def sample(self):
        self.verify_images()
        self.accesses, self.identities = {}, {}
        ready, pending = {}, {}
        try:
            management = self.collect_plane("management")
        except Pending as error:
            return self.progress({}, {"management": str(error)}, [], complete=False)
        self.resources = self.resource_inventory(management.access)
        require(
            all(slot in self.resources for slot in self.targets if slot != "management"),
            "local_published_radius_cluster_missing",
        )
        inventory = self.inventory(management)
        slots = self.planned_slots(inventory)
        require(set(slots) == set(SLOTS), "local_planned_topology_mismatch")
        ready["management"] = management
        self.publish(ready)
        pairs = {item["pair_id"]: item for item in inventory["pairs"]}
        for slot in SLOTS[1:]:
            pair, role = slot.rsplit("-", 1)
            if pair not in pairs or not pairs[pair].get(role + "_cluster_id"):
                require(slot not in self.targets, "local_published_pair_cluster_missing")
                pending[slot] = "pair_cluster_not_recorded"
                continue
            if slot not in self.resources:
                pending[slot] = "radius_cluster_not_created"
                continue
            if self.resources[slot]["properties"].get("provisioningState") not in (
                "Succeeded",
                "Failed",
                "Canceled",
            ):
                pending[slot] = "radius_cluster_not_ready"
                continue
            try:
                plane = self.collect_plane(slot)
                expected_url = pairs[pair].get(role + "_url")
                require(
                    not expected_url
                    or self.local_config.validate_endpoint(slot, expected_url) == plane.url,
                    "inventory_gateway_mismatch",
                )
                ready[slot] = plane
                self.publish({slot: plane})
            except Pending as error:
                pending[slot] = str(error)
        tenants = {item["tenant_id"]: item for item in inventory["tenants"]}
        completed = [
            name for name in self.tenants.values() if tenants.get(name, {}).get("ready") is True
        ]
        return self.progress(
            ready, pending, completed, complete=not pending and len(completed) == 3
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=STATE / "provisioning.json")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--watch", action="store_true")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args(argv)
    exporter = None

    def interrupted(_signal, _frame):
        raise Error("export_interrupted")

    previous = {
        number: signal.signal(number, interrupted) for number in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        exporter = Exporter(args.config)
        return exporter.run(watch=args.watch, timeout=args.timeout)
    except Exception as error:
        print(
            json.dumps(
                {
                    "outcome": "failed",
                    "error": str(error)
                    if isinstance(error, Error)
                    else "local_state_export_failed",
                }
            )
        )
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if exporter is not None and exporter.work.exists():
            shutil.rmtree(exporter.work)


if __name__ == "__main__":
    raise SystemExit(main())
