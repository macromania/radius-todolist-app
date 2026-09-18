"""Guards for one owned administrative Pod and its serialized environment operation."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError

from plane_demo.management.provisioning import OperatorConfig, ProvisioningError

LEASE_NAME = "environment-operator"
NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def config_hash(configuration: OperatorConfig) -> str:
    return hashlib.sha256(
        json.dumps(configuration.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def assert_job(job, configuration, namespace, name, uid, digest):
    metadata = job.metadata
    pod = job.spec.template.spec
    if name == "deploy-management":
        expected_command = [
            "python",
            "scripts/operations/deploy-plane.py",
            "--slot",
            "management",
            "--config",
            "/gate/provisioning.json",
        ]
    else:
        action, pair = name.split("-", 1)
        expected_command = [
            "python",
            "scripts/operations/prepare-environment.py",
            "--pair",
            pair,
            "--config",
            "/gate/provisioning.json",
            *(["--retire"] if action == "retire" else []),
        ]
    if (
        metadata.name != name
        or metadata.namespace != namespace
        or metadata.uid != uid
        or metadata.deletion_timestamp is not None
        or (metadata.annotations or {}).get("plane-demo/config-sha256") != digest
        or job.spec.parallelism != 1
        or job.spec.completions != 1
        or job.spec.suspend
        or pod.service_account_name != "provisioner"
        or len(pod.containers) != 1
        or pod.containers[0].name != "operator"
        or pod.containers[0].image != configuration.images["provisioner"]
        or pod.containers[0].command != expected_command
        or any(
            condition.status == "True" and condition.type in {"Complete", "Failed", "FailureTarget"}
            for condition in (job.status.conditions or [])
        )
    ):
        raise ProvisioningError("operator_owner_mismatch")
    identity = configuration.identity
    expected = {
        "plane-demo/project": identity.project,
        "plane-demo/deployment": identity.deployment,
        "plane-demo/environment": "azure",
    }
    if any((metadata.labels or {}).get(key) != value for key, value in expected.items()):
        raise ProvisioningError("operator_owner_mismatch")


@contextmanager
def environment_guards(configuration: OperatorConfig):
    if not configuration.prepared_environments or configuration.identity is None:
        raise ProvisioningError("prepared_environments_required")
    name = os.environ.get("OPERATOR_JOB_NAME", "")
    uid, pod_uid = os.environ.get("OPERATOR_JOB_UID", ""), os.environ.get("OPERATOR_POD_UID", "")
    lease_uid = os.environ.get("OPERATOR_LEASE_UID", "")
    namespace = configuration.namespace("management")
    try:
        UUID(uid)
        UUID(pod_uid)
        UUID(lease_uid)
    except ValueError:
        raise ProvisioningError("operator_job_identity_required") from None
    allowed = {
        "deploy-management",
        *(f"prepare-{item['pair_id']}" for item in configuration.pair_slots),
        *(
            f"retire-{item['pair_id']}"
            for item in configuration.pair_slots
            if item["pair_id"] != "shared"
        ),
    }
    if name not in allowed or NAMESPACE_FILE.read_text().strip() != namespace:
        raise ProvisioningError("operator_owner_mismatch")
    settings = client.Configuration()
    try:
        config.load_incluster_config(client_configuration=settings)
    except ConfigException:
        raise ProvisioningError("in_cluster_management_access_required") from None
    with client.ApiClient(settings) as connection:
        jobs, pods = client.BatchV1Api(connection), client.CoreV1Api(connection)
        leases, apps = client.CoordinationV1Api(connection), client.AppsV1Api(connection)
        digest = config_hash(configuration)
        claim_name = f"{name}-attempt"
        claim_uid = None

        def active():
            try:
                job = jobs.read_namespaced_job(name, namespace, _request_timeout=(5, 15))
                assert_job(job, configuration, namespace, name, uid, digest)
                lease = leases.read_namespaced_lease(
                    LEASE_NAME,
                    f"{configuration.identity.stem}-operations",
                    _request_timeout=(5, 15),
                )
                if lease.spec.holder_identity != uid or lease.metadata.uid != lease_uid:
                    raise ProvisioningError("environment_operator_lease_lost")
                for key in (
                    "plane-demo/project",
                    "plane-demo/deployment",
                    "plane-demo/environment",
                ):
                    if (lease.metadata.labels or {}).get(key) != job.metadata.labels.get(key):
                        raise ProvisioningError("environment_operator_lease_lost")
                if claim_uid is not None:
                    claim = pods.read_namespaced_config_map(
                        claim_name, namespace, _request_timeout=(5, 15)
                    )
                    owners = claim.metadata.owner_references or []
                    if (
                        claim.metadata.uid != claim_uid
                        or claim.metadata.name != claim_name
                        or claim.metadata.namespace != namespace
                        or len(owners) != 1
                        or owners[0].kind != "Job"
                        or owners[0].name != name
                        or owners[0].uid != uid
                        or claim.immutable is not True
                        or claim.data != {"jobUID": uid, "podUID": pod_uid, "configSHA256": digest}
                    ):
                        raise ProvisioningError("operator_attempt_changed")
            except (ApiException, HTTPError):
                raise ProvisioningError("operator_observation_failed") from None

        def writer():
            active()
            try:
                deployment = apps.read_namespaced_deployment(
                    "provisioner", namespace, _request_timeout=(5, 15)
                )
            except ApiException as error:
                if error.status != 404:
                    raise ProvisioningError("operator_observation_failed") from None
            except HTTPError:
                raise ProvisioningError("operator_observation_failed") from None
            else:
                if deployment.spec.replicas != 0 or any(
                    getattr(deployment.status, key, None) or 0
                    for key in ("replicas", "ready_replicas", "available_replicas")
                ):
                    raise ProvisioningError("competing_environment_writer")
            try:
                current = pods.list_namespaced_pod(
                    namespace,
                    field_selector="spec.serviceAccountName=provisioner",
                    _request_timeout=(5, 15),
                )
                own_pods = 0
                for pod in current.items:
                    owners = [
                        owner
                        for owner in (pod.metadata.owner_references or [])
                        if owner.kind == "Job" and owner.controller is True
                    ]
                    if len(owners) != 1:
                        raise ProvisioningError("competing_environment_writer")
                    owner = owners[0]
                    if owner.uid == uid and pod.metadata.uid == pod_uid:
                        own_pods += 1
                        continue
                    if pod.status.phase not in {"Succeeded", "Failed"}:
                        raise ProvisioningError("competing_environment_writer")
                    predecessor = jobs.read_namespaced_job(
                        owner.name, namespace, _request_timeout=(5, 15)
                    )
                    if predecessor.metadata.uid != owner.uid or any(
                        (predecessor.metadata.labels or {}).get(key)
                        != configuration.identity.public_values().get(setting)
                        for key, setting in (
                            ("plane-demo/project", "DEMO_PROJECT"),
                            ("plane-demo/deployment", "DEMO_DEPLOYMENT"),
                            ("plane-demo/environment", "DEMO_ENV"),
                        )
                    ):
                        raise ProvisioningError("competing_environment_writer")
                if own_pods != 1:
                    raise ProvisioningError("operator_pod_identity_missing")
            except (ApiException, HTTPError):
                raise ProvisioningError("operator_observation_failed") from None

        writer()
        try:
            claim = pods.create_namespaced_config_map(
                namespace,
                client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(
                        name=claim_name,
                        namespace=namespace,
                        owner_references=[
                            client.V1OwnerReference(
                                api_version="batch/v1",
                                kind="Job",
                                name=name,
                                uid=uid,
                            )
                        ],
                    ),
                    immutable=True,
                    data={"jobUID": uid, "podUID": pod_uid, "configSHA256": digest},
                ),
                _request_timeout=(5, 15),
            )
        except ApiException as error:
            code = (
                "operator_attempt_already_claimed"
                if error.status == 409
                else "operator_observation_failed"
            )
            raise ProvisioningError(code) from None
        except HTTPError:
            raise ProvisioningError("operator_observation_failed") from None
        claim_uid = claim.metadata.uid
        try:
            UUID(claim_uid)
        except (TypeError, ValueError, AttributeError):
            raise ProvisioningError("operator_attempt_not_recorded") from None
        active()
        yield active, writer
