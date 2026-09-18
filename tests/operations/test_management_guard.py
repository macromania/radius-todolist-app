import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from plane_demo.management.providers.identity import DemoConfig
from plane_demo.management.provisioning import ProvisioningError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def guarded_job(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "bootstrap_job_guard", ROOT / "scripts/operations/management_job.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = DemoConfig(
        "azure", "sample", "demo", "11111111-1111-1111-1111-111111111111", "centralus"
    )
    namespace = identity.namespace("management")
    file = tmp_path / "namespace"
    file.write_text(namespace)
    monkeypatch.setattr(module, "NAMESPACE_FILE", file)
    uid = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setenv("OPERATOR_JOB_UID", uid)
    image = "registry.azurecr.io/provisioner@sha256:" + "a" * 64
    configuration = SimpleNamespace(
        prepared_environments=False,
        identity=identity,
        namespace=lambda slot: namespace,
        images={"provisioner": image},
    )
    job = SimpleNamespace(
        metadata=SimpleNamespace(
            name="deploy-management",
            namespace=namespace,
            uid=uid,
            deletion_timestamp=None,
            labels={
                "plane-demo/project": identity.project,
                "plane-demo/deployment": identity.deployment,
                "plane-demo/environment": identity.environment,
                "plane-demo/operator": "management-deploy",
            },
        ),
        spec=SimpleNamespace(
            parallelism=1,
            completions=1,
            suspend=False,
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    service_account_name="provisioner",
                    containers=[SimpleNamespace(name="operator", image=image)],
                )
            ),
        ),
        status=SimpleNamespace(conditions=[]),
    )
    jobs, apps, pods = MagicMock(), MagicMock(), MagicMock()
    jobs.read_namespaced_job.return_value = job
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    pods.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    monkeypatch.setattr(module.config, "load_incluster_config", lambda **kwargs: None)
    monkeypatch.setattr(module.client, "ApiClient", MagicMock())
    monkeypatch.setattr(module.client, "BatchV1Api", lambda _: jobs)
    monkeypatch.setattr(module.client, "AppsV1Api", lambda _: apps)
    monkeypatch.setattr(module.client, "CoreV1Api", lambda _: pods)
    return module, configuration, job, jobs, apps, pods


def test_bootstrap_guard_rechecks_owned_job_and_refuses_active_runtime_writer(guarded_job):
    module, configuration, job, jobs, apps, _ = guarded_job
    with module.bootstrap_guards(configuration) as (active, writer):
        writer()
        apps.read_namespaced_deployment.side_effect = None
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            metadata=SimpleNamespace(name="provisioner", namespace=job.metadata.namespace),
            spec=SimpleNamespace(replicas=1),
        )
        active()
        with pytest.raises(ProvisioningError, match="provisioner_must_be_stopped"):
            writer()
        job.metadata.uid = "33333333-3333-3333-3333-333333333333"
        with pytest.raises(ProvisioningError, match="operator_owner_mismatch"):
            active()
    assert jobs.read_namespaced_job.call_count >= 4


@pytest.mark.parametrize("changed", ["image", "owner", "suspended", "failed", "parallel"])
def test_bootstrap_guard_refuses_wrong_or_inactive_job(guarded_job, changed):
    module, configuration, job, _, _, _ = guarded_job
    if changed == "image":
        job.spec.template.spec.containers[0].image = "foreign"
    elif changed == "owner":
        job.metadata.labels["plane-demo/project"] = "foreign"
    elif changed == "suspended":
        job.spec.suspend = True
    elif changed == "failed":
        job.status.conditions = [SimpleNamespace(type="Failed", status="True")]
    else:
        job.spec.parallelism = 2
    with pytest.raises(ProvisioningError, match="operator_owner_mismatch"):
        with module.bootstrap_guards(configuration):
            pytest.fail("invalid job admitted")


@pytest.mark.parametrize("remaining", ["replicas", "generation", "pod", "orphan-pod"])
def test_zero_desired_replicas_is_not_observed_worker_termination(guarded_job, remaining):
    module, configuration, job, _, apps, pods = guarded_job
    deployment = SimpleNamespace(
        metadata=SimpleNamespace(
            name="provisioner", namespace=job.metadata.namespace, generation=2
        ),
        spec=SimpleNamespace(replicas=0),
        status=SimpleNamespace(
            observed_generation=2,
            replicas=0,
            ready_replicas=0,
            available_replicas=0,
            updated_replicas=0,
        ),
    )
    apps.read_namespaced_deployment.side_effect = None
    apps.read_namespaced_deployment.return_value = deployment
    if remaining == "replicas":
        deployment.status.replicas = 1
    elif remaining == "generation":
        deployment.status.observed_generation = 1
    else:
        pods.list_namespaced_pod.return_value = SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(
                        owner_references=[
                            SimpleNamespace(
                                kind="ReplicaSet",
                                name="provisioner-previous",
                                uid="33333333-3333-3333-3333-333333333333",
                                controller=True,
                            ),
                        ]
                    )
                ),
            ]
        )
        if remaining == "orphan-pod":
            apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    with module.bootstrap_guards(configuration) as (_, writer):
        with pytest.raises(ProvisioningError, match="provisioner_must_be_stopped"):
            writer()
