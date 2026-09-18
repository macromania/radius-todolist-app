"""Workstation-side ownership, serialization and discovery for Azure environments."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from ipaddress import IPv4Network
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts/operations"), str(ROOT)]

from plane_demo.management.providers.azure_environments import (  # noqa: E402
    deployment_outputs,
    environment_deployment,
    merge_foundations,
    next_allocation_start,
    require,
)
from plane_demo.management.providers.commands import write_json  # noqa: E402
from plane_demo.management.providers.identity import (  # noqa: E402
    AZURE_ENVIRONMENT_MODE,
    DemoConfig,
    isolated_pair,
)
from plane_demo.management.provisioning import OperatorConfig  # noqa: E402
from scripts.operations.config import load_config  # noqa: E402
from scripts.operations.output import progress, status  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "management_deployment_operator", ROOT / "scripts/operations/run-management-job.py"
)
management = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(management)
LEASE_NAME = "environment-operator"


def execute(argv, *, value=None, timeout=180, env=None):
    result = subprocess.run(
        argv,
        cwd=ROOT,
        text=True,
        input=json.dumps(value) if value is not None else None,
        stdout=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env=env,
    )
    require(result.returncode == 0, f"{argv[0]} failed (exit {result.returncode}); see diagnostics")
    return result.stdout.strip()


def azure(config: DemoConfig, *args):
    return json.loads(
        execute(
            [
                "az",
                *args,
                "--subscription",
                config.subscription,
                "--output",
                "json",
                "--only-show-errors",
            ]
        )
    )


def selection_environment(identity: DemoConfig):
    return {
        **os.environ,
        "PLANE_DEMO_EXPECT_STEM": identity.stem,
        "PLANE_DEMO_EXPECT_SUBSCRIPTION": identity.subscription,
        "PLANE_DEMO_EXPECT_LOCATION": identity.location,
        "PLANE_DEMO_EXPECT_VAULT": identity.vault_name,
    }


def base_deployment(config: DemoConfig):
    records = azure(
        config,
        "deployment",
        "sub",
        "list",
        "--query",
        f"[?name=='{config.stem}-bootstrap']",
    )
    require(isinstance(records, list) and len(records) <= 1, "Ambiguous base deployment")
    if not records:
        return None
    record = records[0]
    base = deployment_outputs(record)
    require(
        base.get("foundation", {}).get("environmentMode") == AZURE_ENVIRONMENT_MODE,
        "This workflow requires a fresh prepared-environment deployment",
    )
    merge_foundations(config, base, [])
    return record


def catalog(config: DemoConfig, base: dict):
    records = azure(
        config,
        "deployment",
        "sub",
        "list",
        "--query",
        f"[?starts_with(name, '{config.stem}-environment-')]",
    )
    require(isinstance(records, list), "Invalid isolated deployment list")
    return merge_foundations(config, base, records)


def operator_config(identity: DemoConfig, document: dict, artifacts: dict) -> OperatorConfig:
    require(
        artifacts.get("status") == "artifacts_verified"
        and artifacts.get("content_verified") is True,
        "Artifacts were not verified",
    )
    revision = artifacts["source_revision"]
    selected = DemoConfig.from_values(
        {
            **identity.values(include_secrets=True),
            "DEMO_REVISION": revision,
        }
    )
    return OperatorConfig.from_dict(
        {
            "version": 1,
            **document,
            "allocations": {item["slot"]: item for item in document["allocations"]},
            "recipes": artifacts["recipes"],
            "images": artifacts["images"],
            "bootstrapIdentity": selected.public_values(),
        },
        identity=selected,
    )


class EnvironmentOperator:
    def __init__(self, identity: DemoConfig, workspace: Path, base: dict):
        self.identity, self.workspace, self.base = identity, workspace, base
        self.namespace = identity.namespace("management")
        self.operator_namespace = f"{identity.stem}-operations"
        self.context, self.kubeconfig = management.management_access(identity, workspace)
        self.kube = [
            "kubectl",
            "--kubeconfig",
            self.kubeconfig,
            "--context",
            self.context,
            "--namespace",
            self.namespace,
            "--request-timeout=30s",
        ]
        self.labels = {
            "plane-demo/project": identity.project,
            "plane-demo/deployment": identity.deployment,
            "plane-demo/environment": "azure",
        }
        self.token = "operator:" + uuid4().hex
        self.lease_uid = None
        self.release_safe = True

    def command(self, *args, value=None):
        return execute([*self.kube, *args], value=value)

    def record_command(self, *args, value=None):
        return self.command("--namespace", self.operator_namespace, *args, value=value)

    def get(self, kind, name, *, records=False):
        command = self.record_command if records else self.command
        text = command("get", kind, name, "--ignore-not-found", "-o", "json")
        return json.loads(text) if text else None

    def owned(self, resource, *, records=False):
        require(
            isinstance(resource, dict)
            and resource.get("metadata", {}).get("namespace")
            == (self.operator_namespace if records else self.namespace)
            and resource["metadata"].get("uid")
            and not resource["metadata"].get("deletionTimestamp")
            and all(
                resource["metadata"].get("labels", {}).get(k) == v for k, v in self.labels.items()
            ),
            "Environment operation ownership mismatch",
        )
        return resource

    def ensure_namespace(self):
        for name in (self.namespace, self.operator_namespace):
            namespace = self.get("namespace", name)
            if namespace is None:
                namespace = json.loads(
                    self.command(
                        "create",
                        "-f",
                        "-",
                        "-o",
                        "json",
                        value={
                            "apiVersion": "v1",
                            "kind": "Namespace",
                            "metadata": {"name": name, "labels": self.labels},
                        },
                    )
                )
            require(
                all(
                    namespace["metadata"].get("labels", {}).get(k) == v
                    for k, v in self.labels.items()
                ),
                "Management namespace ownership mismatch",
            )
        for kind, fields in (
            (
                "Role",
                {
                    "rules": [
                        {
                            "apiGroups": ["coordination.k8s.io"],
                            "resources": ["leases"],
                            "resourceNames": [LEASE_NAME],
                            "verbs": ["get"],
                        }
                    ]
                },
            ),
            (
                "RoleBinding",
                {
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "Role",
                        "name": "environment-lease-observer",
                    },
                    "subjects": [
                        {
                            "kind": "ServiceAccount",
                            "name": "provisioner",
                            "namespace": self.namespace,
                        }
                    ],
                },
            ),
        ):
            name = "environment-lease-observer"
            existing = self.get(kind.lower(), name, records=True)
            if existing is None:
                self.record_command(
                    "create",
                    "-f",
                    "-",
                    value={
                        "apiVersion": "rbac.authorization.k8s.io/v1",
                        "kind": kind,
                        "metadata": {
                            "name": name,
                            "namespace": self.operator_namespace,
                            "labels": self.labels,
                        },
                        **fields,
                    },
                )
            else:
                self.owned(existing, records=True)
                require(
                    all(existing.get(key) == value for key, value in fields.items()),
                    "Operator Lease observation permissions differ",
                )

    def acquire(self, *, cleanup_target=None):
        self.ensure_namespace()
        lease = self.get("lease", LEASE_NAME, records=True)
        if lease is None:
            lease = json.loads(
                self.record_command(
                    "create",
                    "-f",
                    "-",
                    "-o",
                    "json",
                    value={
                        "apiVersion": "coordination.k8s.io/v1",
                        "kind": "Lease",
                        "metadata": {
                            "name": LEASE_NAME,
                            "namespace": self.operator_namespace,
                            "labels": self.labels,
                        },
                        "spec": {"holderIdentity": ""},
                    },
                )
            )
        self.owned(lease, records=True)
        holder = lease.get("spec", {}).get("holderIdentity", "")
        if holder:
            jobs = json.loads(self.command("get", "jobs", "-o", "json"))["items"]
            matches = [job for job in jobs if job["metadata"].get("uid") == holder]
            require(
                len(matches) == 1, "An active or interrupted environment operator holds the Lease"
            )
            job = self.owned(matches[0])
            name = job["metadata"]["name"]
            administrative = (
                name == "deploy-management"
                or name == "prepare-shared"
                or any(
                    name.startswith(prefix)
                    and isolated_pair(name.removeprefix(prefix)) == name.removeprefix(prefix)
                    for prefix in ("prepare-", "retire-")
                    if name.startswith(prefix + "isolated-")
                )
            )
            require(
                administrative
                and job["metadata"].get("labels", {}).get("plane-demo/operator")
                == "management-deploy",
                "The Lease holder is not an owned administrative Job",
            )
            failed_cleanup = (
                cleanup_target is not None
                and (
                    cleanup_target == "all"
                    or name in {f"prepare-{cleanup_target}", f"retire-{cleanup_target}"}
                )
                and any(
                    item.get("type") == "Failed" and item.get("status") == "True"
                    for item in job.get("status", {}).get("conditions", [])
                )
            )
            require(
                any(
                    item.get("type") == "Complete" and item.get("status") == "True"
                    for item in job.get("status", {}).get("conditions", [])
                )
                or failed_cleanup,
                "An active, failed, or interrupted environment Job holds the Lease",
            )
            if failed_cleanup:
                pods = json.loads(
                    self.command(
                        "get",
                        "pods",
                        "-l",
                        f"batch.kubernetes.io/controller-uid={holder}",
                        "-o",
                        "json",
                    )
                )
                require(
                    isinstance(pods.get("items"), list)
                    and all(
                        pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}
                        for pod in pods["items"]
                    ),
                    "A failed environment Job still has active Pods",
                )
        self.lease_uid = lease["metadata"]["uid"]
        self.change_holder(lease, self.token)

    def change_holder(self, lease, holder):
        self.owned(lease, records=True)
        require(lease["metadata"]["uid"] == self.lease_uid, "Environment Lease identity changed")
        return json.loads(
            self.record_command(
                "patch",
                "lease",
                LEASE_NAME,
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {"op": "test", "path": "/metadata/uid", "value": self.lease_uid},
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": lease["metadata"]["resourceVersion"],
                        },
                        {
                            "op": "test",
                            "path": "/spec/holderIdentity",
                            "value": lease["spec"]["holderIdentity"],
                        },
                        {"op": "replace", "path": "/spec/holderIdentity", "value": holder},
                    ]
                ),
                "-o",
                "json",
            )
        )

    def guard(self):
        require(load_config(ROOT / ".env") == self.identity, "Operator configuration changed")
        lease = self.owned(self.get("lease", LEASE_NAME, records=True), records=True)
        require(
            lease["metadata"]["uid"] == self.lease_uid
            and lease["spec"].get("holderIdentity") == self.token,
            "Environment operator Lease was lost",
        )
        return lease

    def release(self):
        self.change_holder(self.guard(), "")

    def management_radius_ready(self):
        expected = next(item for item in self.base["allocations"] if item["slot"] == "management")
        client_id = expected["identities"]["radius"]["clientId"]
        tenant_id = self.base["foundation"]["tenantId"]
        for name in ("applications-rp", "bicep-de", "ucp", "dynamic-rp"):
            output = self.command(
                "--namespace",
                "radius-system",
                "get",
                "deployment",
                name,
                "--ignore-not-found",
                "-o",
                "json",
            )
            if not output:
                return False
            deployment = json.loads(output)
            if not (
                deployment["spec"]["template"]
                .get("metadata", {})
                .get("labels", {})
                .get("azure.workload.identity/use")
                == "true"
                and deployment.get("status", {}).get("availableReplicas", 0) >= 1
                and deployment.get("status", {}).get("observedGeneration", 0)
                >= deployment["metadata"]["generation"]
            ):
                return False
            account = json.loads(
                self.command(
                    "--namespace",
                    "radius-system",
                    "get",
                    "serviceaccount",
                    name,
                    "-o",
                    "json",
                )
            )
            annotations = account["metadata"].get("annotations", {})
            if (
                annotations.get("azure.workload.identity/client-id") != client_id
                or annotations.get("azure.workload.identity/tenant-id") != tenant_id
            ):
                return False
        return True

    def ensure_management_radius(self):
        self.guard()
        if self.management_radius_ready():
            status("success", "Management Radius: verified without reinstalling")
            return
        name = "management-radius-installation"
        require(
            self.get("configmap", name, records=True) is None,
            "Management Radius installation was interrupted or changed; inspect it before recovery",
        )
        radius_namespace = self.get("namespace", "radius-system")
        if radius_namespace is not None:
            deployments = json.loads(
                self.command(
                    "--namespace",
                    "radius-system",
                    "get",
                    "deployments",
                    "-o",
                    "json",
                )
            )
            require(
                isinstance(deployments.get("items"), list) and not deployments["items"],
                "Unrecorded management Radius resources exist; inspect them before installation",
            )
        self.record_command(
            "create",
            "-f",
            "-",
            value={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "immutable": True,
                "metadata": {
                    "name": name,
                    "namespace": self.operator_namespace,
                    "labels": self.labels,
                },
                "data": {"operator": self.token, "leaseUID": self.lease_uid},
            },
        )
        allocation = next(item for item in self.base["allocations"] if item["slot"] == "management")
        self.release_safe = False
        with progress("Management Radius installation"):
            result = json.loads(
                execute(
                    [
                        "bash",
                        str(ROOT / "scripts/operations/install-radius.sh"),
                        "--workspace-root",
                        str(self.workspace),
                        "--context",
                        self.context,
                        "--kubeconfig",
                        self.kubeconfig,
                        "--config",
                        str(self.workspace / "radius.yaml"),
                        "--client-id",
                        allocation["identities"]["radius"]["clientId"],
                        "--tenant-id",
                        self.base["foundation"]["tenantId"],
                    ],
                    timeout=1800,
                )
            )
        require(
            result == {"context": self.context, "workload_identity_verified": True}
            and self.management_radius_ready(),
            "Management Radius did not pass identity/readiness verification",
        )
        self.release_safe = True

    def journals(self):
        rows = json.loads(
            self.record_command(
                "get",
                "configmaps",
                "-l",
                "plane-demo/environment-record=true",
                "-o",
                "json",
            )
        )["items"]
        result = []
        for row in rows:
            self.owned(row, records=True)
            value = json.loads(row["data"]["record.json"])
            pair = value["pairId"]
            require(
                set(value) == {"pairId", "allocationStart", "baseDeploymentId", "state"}
                and value["state"]
                in {"reserved", "submitting", "foundation", "available", "retired"}
                and isolated_pair(pair) == pair
                and row["metadata"]["name"] == f"environment-{pair}"
                and value["baseDeploymentId"]
                == (
                    f"/subscriptions/{self.identity.subscription}/providers/Microsoft.Resources/"
                    f"deployments/{self.identity.stem}-bootstrap"
                ),
                "Environment allocation journal differs",
            )
            result.append(value)
        return result

    def reserve(self, pair: str, environments: dict):
        self.guard()
        records = self.journals()
        recorded = {item["pairId"]: item for item in records}
        for name, known in environments.items():
            require(
                name in recorded
                and recorded[name]["allocationStart"] == known["allocationStart"]
                and (recorded[name]["state"] == "retired") == (known["state"] == "retired"),
                "An Azure environment has no matching allocation journal",
            )
        existing = [record for record in records if record["pairId"] == pair]
        require(len(existing) <= 1, "Duplicate environment journal")
        if existing:
            require(existing[0]["state"] != "retired", "Retired environment names cannot be reused")
            return existing[0], False
        record = {
            "pairId": pair,
            "allocationStart": next_allocation_start(records),
            "baseDeploymentId": (
                f"/subscriptions/{self.identity.subscription}/providers/Microsoft.Resources/"
                f"deployments/{self.identity.stem}-bootstrap"
            ),
            "state": "reserved",
        }
        self.record_command(
            "create",
            "-f",
            "-",
            value={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": f"environment-{pair}",
                    "namespace": self.operator_namespace,
                    "labels": {**self.labels, "plane-demo/environment-record": "true"},
                },
                "data": {"record.json": json.dumps(record, sort_keys=True)},
            },
        )
        return record, True

    def mark(self, pair: str, state: str):
        self.guard()
        name = f"environment-{pair}"
        row = self.owned(self.get("configmap", name, records=True), records=True)
        record = json.loads(row["data"]["record.json"])
        require(record["pairId"] == pair, "Environment journal target differs")
        record["state"] = state
        self.record_command(
            "patch",
            "configmap",
            name,
            "--type=json",
            "-p",
            json.dumps(
                [
                    {"op": "test", "path": "/metadata/uid", "value": row["metadata"]["uid"]},
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": row["metadata"]["resourceVersion"],
                    },
                    {
                        "op": "replace",
                        "path": "/data/record.json",
                        "value": json.dumps(record, sort_keys=True),
                    },
                ]
            ),
        )

    def run_job(self, config: OperatorConfig, name: str):
        self.guard()
        existing = self.get("job", name)
        if existing:
            self.owned(existing)
            complete = any(
                item.get("type") == "Complete" and item.get("status") == "True"
                for item in existing.get("status", {}).get("conditions", [])
            )
            require(complete, "Existing environment Job is not complete; inspect its owned logs")
            saved = self.owned(self.get("configmap", f"{name}-config"))
            original = OperatorConfig.from_dict(
                json.loads(saved["data"]["provisioning.json"]), identity=config.identity
            )
            require(
                original.identity.public_values() == config.identity.public_values()
                and original.images == config.images
                and all(
                    original.allocations[slot] == config.allocations.get(slot)
                    for slot in original.allocations
                ),
                "Completed Job inputs differ from the selected environment",
            )
            config = original

        def on_job(job):
            self.guard()
            complete = any(
                item.get("type") == "Complete" and item.get("status") == "True"
                for item in job.get("status", {}).get("conditions", [])
            )
            if not complete:
                self.release_safe = False
                self.change_holder(self.guard(), job["metadata"]["uid"])

        with progress(f"Environment Job: {name}"):
            result = management.deploy_selected(
                config,
                self.context,
                self.kubeconfig,
                name=name,
                on_job=on_job,
                lease_uid=self.lease_uid,
            )
        lease = self.owned(self.get("lease", LEASE_NAME, records=True), records=True)
        if lease["spec"]["holderIdentity"] != self.token:
            require(
                lease["spec"]["holderIdentity"] == result["job_uid"],
                "Environment Job no longer owns its Lease",
            )
            self.change_holder(lease, self.token)
        self.release_safe = True
        status("success", f"Environment Job: {name} completed")
        return result

    def require_default_ready(self, configuration: OperatorConfig):
        job = self.owned(self.get("job", "prepare-shared"))
        require(
            any(
                condition.get("type") == "Complete" and condition.get("status") == "True"
                for condition in job.get("status", {}).get("conditions", [])
            ),
            "Prepare the shared environment before adding isolated capacity",
        )
        saved = self.owned(self.get("configmap", "prepare-shared-config"))
        value = json.loads(saved["data"]["provisioning.json"])
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        require(
            saved.get("immutable") is True
            and saved["metadata"].get("ownerReferences")
            == [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": "prepare-shared",
                    "uid": job["metadata"]["uid"],
                }
            ]
            and job["metadata"].get("annotations", {}).get("plane-demo/config-sha256") == digest,
            "The shared preparation input record changed",
        )
        original = OperatorConfig.from_dict(value)
        require(
            original.identity.public_values() == configuration.identity.public_values()
            and original.images == configuration.images,
            "Use the source revision and artifacts that prepared the default environment",
        )

    def isolated_foundation(self, pair, record, *, fresh):
        self.guard()
        name = environment_deployment(self.identity, pair)
        records = azure(
            self.identity,
            "deployment",
            "sub",
            "list",
            "--query",
            f"[?name=='{name}']",
        )
        require(isinstance(records, list) and len(records) <= 1, "Ambiguous isolated foundation")
        if records:
            require(
                (records[0].get("tags") or {}).get("plane-demo/environment-state") != "retired",
                "Retired environment names cannot be reused",
            )
            outputs = deployment_outputs(records[0])
            require(
                outputs["environment"]["allocationStart"] == record["allocationStart"],
                "Existing isolated foundation allocation differs",
            )
            return
        require(
            fresh or record.get("state") == "reserved",
            "Interrupted isolated foundation submission requires explicit recovery",
        )
        self.check_new_foundation(pair, record["allocationStart"])
        template = self.workspace / "isolated.json"
        execute(
            [
                str(Path.home() / ".rad/bin/bicep"),
                "build",
                str(ROOT / "infra/bootstrap/isolated.bicep"),
                "--outfile",
                str(template),
            ]
        )
        token = azure(self.identity, "account", "show")
        require(
            token.get("user", {}).get("type") == "user", "An interactive Azure operator is required"
        )
        operator = azure(self.identity, "ad", "signed-in-user", "show", "--query", "id")
        from uuid import UUID

        UUID(operator)
        from plane_demo.management.providers.secret_store import CredentialScope

        scope = CredentialScope(self.identity.project, self.identity.deployment, "azure")
        credentials = [scope.secret_name("management", "cp_" + pair.replace("-", "_"))]
        credentials += [
            scope.secret_name(f"{pair}-{role}", credential)
            for role, names in (
                ("control", ("demoKey", "cp_api", "cp_reconciler", "dp_reconciler")),
                ("data", ("demoKey",)),
            )
            for credential in names
        ]
        parameters = self.workspace / "isolated-parameters.json"
        write_json(
            parameters,
            {
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                "contentVersion": "1.0.0.0",
                "parameters": {
                    key: {"value": value}
                    for key, value in {
                        "base": self.base,
                        "pairId": pair,
                        "allocationStart": record["allocationStart"],
                        "operatorObjectId": operator,
                        "applicationCredentialNames": credentials,
                    }.items()
                },
            },
        )
        self.guard()
        result = azure(
            self.identity,
            "deployment",
            "sub",
            "validate",
            "--name",
            name,
            "--location",
            self.identity.location,
            "--template-file",
            str(template),
            "--parameters",
            f"@{parameters}",
        )
        require(
            result.get("properties", {}).get("provisioningState") == "Succeeded",
            "Isolated foundation validation failed",
        )
        self.guard()
        self.mark(pair, "submitting")
        self.release_safe = False
        with progress(f"Isolated foundation: {pair}"):
            result = execute(
                [
                    "az",
                    "deployment",
                    "sub",
                    "create",
                    "--name",
                    name,
                    "--location",
                    self.identity.location,
                    "--template-file",
                    str(template),
                    "--parameters",
                    f"@{parameters}",
                    "--subscription",
                    self.identity.subscription,
                    "--output",
                    "json",
                ],
                timeout=3600,
            )
        require(
            json.loads(result).get("properties", {}).get("provisioningState") == "Succeeded",
            "Isolated foundation did not complete",
        )
        self.mark(pair, "foundation")
        self.release_safe = True

    def check_new_foundation(self, pair, start):
        from scripts.operations.azure.plane_policy import ROLE_NAMES, role_id

        self.guard()
        groups = azure(self.identity, "group", "list", "--query", "[].{name:name,id:id}")
        expected = {
            self.identity.plane_group(f"{pair}-{role}") + suffix
            for role in ("control", "data")
            for suffix in ("", "-nodes")
        }
        require(
            isinstance(groups, list)
            and all(
                isinstance(item, dict) and isinstance(item.get("name"), str) for item in groups
            ),
            "Resource-group collision check is incomplete",
        )
        require(
            not {item["name"].lower() for item in groups} & expected,
            "An isolated group already exists without its foundation record; resources retained",
        )
        for key in (
            "postgresApplication",
            "redisApplication",
            "childClusterRecipe",
            "childIdentityFederation",
        ):
            identifier = role_id(
                self.identity.subscription, f"{self.identity.stem}-{pair}", ROLE_NAMES[key][0]
            )
            existing = azure(
                self.identity, "role", "definition", "list", "--name", identifier.rsplit("/", 1)[1]
            )
            require(existing == [], "An isolated custom role already exists without its foundation")
        foundation = self.base["foundation"]
        expected_vnet = (
            f"/subscriptions/{self.identity.subscription}/resourceGroups/rg-{self.identity.stem}-platform/"
            f"providers/Microsoft.Network/virtualNetworks/vnet-{self.identity.stem}"
        )
        require(
            foundation["virtualNetworkId"].casefold() == expected_vnet.casefold(),
            "Base network identity differs",
        )
        network = azure(self.identity, "network", "vnet", "show", "--ids", expected_vnet)
        require(
            isinstance(network, dict)
            and network.get("id", "").casefold() == expected_vnet.casefold()
            and network.get("provisioningState") == "Succeeded"
            and isinstance(network.get("tags"), dict)
            and network.get("addressSpace", {}).get("addressPrefixes") == ["10.64.0.0/16"]
            and all(
                network.get("tags", {}).get(key) == value
                for key, value in {
                    "project": self.identity.project,
                    "deployment": self.identity.deployment,
                    "environment": "azure",
                    "managedBy": "radius-todolist-app",
                }.items()
            )
            and isinstance(network.get("subnets"), list),
            "Base network ownership or subnet inventory differs",
        )
        wanted_names = {
            f"snet-{pair}-{role}-{suffix}"
            for role in ("control", "data")
            for suffix in ("nodes", "gateway", "endpoints", "postgresql")
        }
        wanted_ranges = [
            IPv4Network(f"10.64.{offset + index}.0/{mask}")
            for index in (start, start + 1)
            for offset, mask in ((0, 24), (16, 24), (32, 27), (48, 27))
        ]
        for subnet in network["subnets"]:
            require(isinstance(subnet, dict), "Invalid subnet observation")
            require(
                isinstance(subnet.get("name"), str) and subnet["name"].lower() not in wanted_names,
                "An isolated subnet already exists",
            )
            prefixes = (
                [subnet["addressPrefix"]]
                if subnet.get("addressPrefix") is not None
                else subnet.get("addressPrefixes")
            )
            require(
                isinstance(prefixes, list)
                and bool(prefixes)
                and all(isinstance(prefix, str) for prefix in prefixes),
                "Subnet addresses are missing",
            )
            for prefix in prefixes:
                observed = IPv4Network(prefix, strict=True)
                require(
                    not any(observed.overlaps(candidate) for candidate in wanted_ranges),
                    "The reserved isolated address range overlaps an existing subnet",
                )

    def verify_ready(self, configuration: OperatorConfig, pair: str):
        """Observe workloads through owned access without replaying preparation."""
        self.guard()
        for role in ("control", "data"):
            slot = f"{pair}-{role}"
            value = execute(
                [
                    "bash",
                    str(ROOT / "scripts/operations/api.sh"),
                    f"{role}:{pair}",
                    "GET",
                    "/healthz",
                ],
                timeout=180,
            )
            require(
                json.loads(value) == {"status": "ok"}, "Prepared environment API is not healthy"
            )
            for component in (f"{role}-api", f"{role}-reconciler"):
                execute(
                    [
                        "bash",
                        str(ROOT / "scripts/operations/kube.sh"),
                        slot,
                        "rollout",
                        "status",
                        f"deployment/{component}",
                        "--timeout=60s",
                    ],
                    timeout=180,
                )
        self.guard()
