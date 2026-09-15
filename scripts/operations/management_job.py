"""Ownership guards for the canonical, serialized management deployment Job."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError

from plane_demo.management.provisioning import OperatorConfig, ProvisioningError

JOB_NAME = "deploy-management"
NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def require_stopped_writer(
    applications, pods, namespace: str, *, operator_job_uid: str | None = None
) -> None:
    try:
        deployment = applications.read_namespaced_deployment(
            "provisioner", namespace, _request_timeout=(5, 15)
        )
    except ApiException as error:
        if error.status == 404:
            deployment = None
        else:
            raise ProvisioningError("operator_observation_failed") from None
    except HTTPError:
        raise ProvisioningError("operator_observation_failed") from None
    if deployment is not None and (
        deployment.metadata.name != "provisioner"
        or deployment.metadata.namespace != namespace
        or deployment.spec.replicas != 0
        or deployment.status is None
        or deployment.metadata.generation is None
        or (deployment.status.observed_generation or 0) < deployment.metadata.generation
        or any(
            (getattr(deployment.status, field, None) or 0) != 0
            for field in ("replicas", "ready_replicas", "available_replicas", "updated_replicas")
        )
    ):
        raise ProvisioningError("provisioner_must_be_stopped_for_credential_creation")
    try:
        current = pods.list_namespaced_pod(
            namespace,
            field_selector="spec.serviceAccountName=provisioner",
            _request_timeout=(5, 15),
        )
    except (ApiException, HTTPError):
        raise ProvisioningError("operator_observation_failed") from None
    if any(
        operator_job_uid is None
        or not any(
            owner.kind == "Job"
            and owner.name == JOB_NAME
            and owner.uid == operator_job_uid
            and owner.controller is True
            for owner in (pod.metadata.owner_references or [])
        )
        for pod in current.items
    ):
        raise ProvisioningError("provisioner_must_be_stopped_for_credential_creation")


@contextmanager
def bootstrap_guards(configuration: OperatorConfig):
    identity = configuration.identity
    if identity is None:
        raise ProvisioningError("bootstrap_identity_required")
    namespace = configuration.namespace("management")
    uid = os.environ.get("OPERATOR_JOB_UID", "")
    try:
        UUID(uid)
    except ValueError:
        raise ProvisioningError("operator_job_identity_required") from None
    if NAMESPACE_FILE.read_text().strip() != namespace:
        raise ProvisioningError("operator_owner_mismatch")
    settings = client.Configuration()
    try:
        config.load_incluster_config(client_configuration=settings)
    except ConfigException:
        raise ProvisioningError("in_cluster_management_access_required") from None
    labels = {
        "plane-demo/project": identity.project,
        "plane-demo/deployment": identity.deployment,
        "plane-demo/environment": identity.environment,
        "plane-demo/operator": "management-deploy",
    }
    with client.ApiClient(settings) as connection:
        jobs, applications = client.BatchV1Api(connection), client.AppsV1Api(connection)
        pods = client.CoreV1Api(connection)

        def active() -> None:
            try:
                job = jobs.read_namespaced_job(JOB_NAME, namespace, _request_timeout=(5, 15))
            except (ApiException, HTTPError):
                raise ProvisioningError("operator_observation_failed") from None
            metadata = job.metadata
            if (
                metadata.name != JOB_NAME
                or metadata.namespace != namespace
                or metadata.uid != uid
                or metadata.deletion_timestamp is not None
                or any((metadata.labels or {}).get(key) != value for key, value in labels.items())
                or job.spec.parallelism != 1
                or job.spec.completions != 1
                or job.spec.suspend
                or job.spec.template.spec.service_account_name != "provisioner"
                or len(job.spec.template.spec.containers) != 1
                or job.spec.template.spec.containers[0].name != "operator"
                or job.spec.template.spec.containers[0].image != configuration.images["provisioner"]
                or any(
                    condition.status == "True" and condition.type in {"Complete", "Failed"}
                    for condition in (job.status.conditions or [])
                )
            ):
                raise ProvisioningError("operator_owner_mismatch")

        def writer() -> None:
            active()
            require_stopped_writer(applications, pods, namespace, operator_job_uid=uid)

        active()
        yield active, writer
