import copy
import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/operations"))
import environment_job as subject  # noqa: E402

from plane_demo.management.providers.credentials import StoredCredentials  # noqa: E402
from plane_demo.management.providers.identity import DemoConfig  # noqa: E402
from plane_demo.management.provisioning import ProvisioningError  # noqa: E402


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    identity = DemoConfig(
        "azure", "sample", "demo", "11111111-1111-1111-1111-111111111111", "eastus2"
    )
    namespace = identity.namespace("management")
    name, uid, pod_uid = (
        "prepare-shared",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    )
    configuration = NS(
        prepared_environments=True,
        identity=identity,
        namespace=lambda _: namespace,
        images={"provisioner": "registry/provisioner@sha256:" + "a" * 64},
        pair_slots=[{"pair_id": "shared", "reporting_role": "cp_shared"}],
        to_dict=lambda: {"selected": "immutable"},
    )
    labels = {
        "plane-demo/project": "sample",
        "plane-demo/deployment": "demo",
        "plane-demo/environment": "azure",
    }
    job = NS(
        metadata=NS(
            name=name,
            namespace=namespace,
            uid=uid,
            deletion_timestamp=None,
            labels=labels,
            annotations={"plane-demo/config-sha256": subject.config_hash(configuration)},
        ),
        spec=NS(
            parallelism=1,
            completions=1,
            suspend=False,
            template=NS(
                spec=NS(
                    service_account_name="provisioner",
                    containers=[
                        NS(
                            name="operator",
                            image=configuration.images["provisioner"],
                            command=[
                                "python",
                                "scripts/operations/prepare-environment.py",
                                "--pair",
                                "shared",
                                "--config",
                                "/gate/provisioning.json",
                            ],
                        )
                    ],
                )
            ),
        ),
        status=NS(conditions=[]),
    )
    pod = NS(
        metadata=NS(
            uid=pod_uid, owner_references=[NS(kind="Job", controller=True, name=name, uid=uid)]
        ),
        status=NS(phase="Running"),
    )
    lease_uid = "77777777-7777-7777-7777-777777777777"
    lease = NS(metadata=NS(labels=labels, uid=lease_uid), spec=NS(holder_identity=uid))
    jobs, pods, leases, apps = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    jobs.read_namespaced_job.return_value = job
    pods.list_namespaced_pod.return_value = NS(items=[pod])
    leases.read_namespaced_lease.return_value = lease
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    claimed = []

    def claim(namespace, body, **kwargs):
        if claimed:
            raise ApiException(status=409)
        body.metadata.uid = "44444444-4444-4444-4444-444444444444"
        claimed.append(body)
        return body

    pods.create_namespaced_config_map.side_effect = claim
    pods.read_namespaced_config_map.side_effect = lambda *a, **kw: claimed[0]
    namespace_file = tmp_path / "namespace"
    namespace_file.write_text(namespace)
    monkeypatch.setattr(subject, "NAMESPACE_FILE", namespace_file)
    for key, value in {
        "OPERATOR_JOB_NAME": name,
        "OPERATOR_JOB_UID": uid,
        "OPERATOR_POD_UID": pod_uid,
        "OPERATOR_LEASE_UID": lease_uid,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(subject.config, "load_incluster_config", lambda **kw: None)
    monkeypatch.setattr(subject.client, "ApiClient", MagicMock())
    for name, value in (
        ("BatchV1Api", jobs),
        ("CoreV1Api", pods),
        ("CoordinationV1Api", leases),
        ("AppsV1Api", apps),
    ):
        monkeypatch.setattr(subject.client, name, lambda _, result=value: result)
    return configuration, job, pod, lease, jobs, pods, claimed


def test_environment_job_claims_once_and_rechecks_lease_before_mutations(guarded):
    configuration, _, _, lease, _, _, claimed = guarded
    with subject.environment_guards(configuration) as (active, writer):
        writer()
        assert len(claimed) == 1 and claimed[0].immutable
        assert claimed[0].data["podUID"] == "33333333-3333-3333-3333-333333333333"
        lease.spec.holder_identity = "foreign"
        with pytest.raises(ProvisioningError, match="lease_lost"):
            active()


def test_replacement_pod_cannot_replay_an_attempt_under_the_same_job(guarded, monkeypatch):
    configuration, _, pod, _, _, _, claimed = guarded
    with subject.environment_guards(configuration):
        pass
    pod.metadata.uid = "55555555-5555-5555-5555-555555555555"
    monkeypatch.setenv("OPERATOR_POD_UID", pod.metadata.uid)
    with pytest.raises(ProvisioningError, match="attempt_already_claimed"):
        with subject.environment_guards(configuration):
            pytest.fail("A replacement Pod replayed administrative work")
    assert len(claimed) == 1


@pytest.mark.parametrize("changed", ["image", "command", "digest", "lease", "pod", "job"])
def test_unowned_or_changed_execution_stops_before_attempt_claim(guarded, changed):
    configuration, job, pod, lease, _, _, claimed = guarded
    if changed == "image":
        job.spec.template.spec.containers[0].image = "foreign"
    elif changed == "command":
        job.spec.template.spec.containers[0].command = ["sh"]
    elif changed == "digest":
        job.metadata.annotations["plane-demo/config-sha256"] = "foreign"
    elif changed == "lease":
        lease.spec.holder_identity = "foreign"
    elif changed == "pod":
        pod.metadata.uid = "foreign"
    else:
        job.metadata.uid = "foreign"
    with pytest.raises(ProvisioningError):
        with subject.environment_guards(configuration):
            pytest.fail("Invalid execution entered")
    assert not claimed


def test_terminal_predecessor_is_allowed_only_with_verified_owner(guarded):
    configuration, job, pod, _, jobs, pods, _ = guarded
    previous = copy.deepcopy(pod)
    previous.metadata.uid = "55555555-5555-5555-5555-555555555555"
    previous.metadata.owner_references[0].uid = "66666666-6666-6666-6666-666666666666"
    previous.metadata.owner_references[0].name = "deploy-management"
    previous.status.phase = "Succeeded"
    predecessor = copy.deepcopy(job)
    predecessor.metadata.uid = previous.metadata.owner_references[0].uid
    jobs.read_namespaced_job.side_effect = lambda name, *a, **kw: (
        predecessor if name == "deploy-management" else job
    )
    pods.list_namespaced_pod.return_value = NS(items=[previous, pod])
    with subject.environment_guards(configuration) as (_, writer):
        writer()
        predecessor.metadata.labels["plane-demo/deployment"] = "foreign"
        with pytest.raises(ProvisioningError, match="competing_environment_writer"):
            writer()


def test_preparation_entrypoint_acquires_database_singleton_before_credential_writes(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/operations/prepare-environment.py"
    spec = importlib.util.spec_from_file_location("guarded_environment_entrypoint", path)
    operator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operator)
    configuration = NS(pair_slots=[{"pair_id": "shared"}])
    monkeypatch.setattr(operator.OperatorConfig, "load", lambda _: configuration)
    provider = MagicMock()
    provider.credentials = MagicMock(spec=StoredCredentials)
    provider.credentials.dsn.return_value = "synthetic-dsn-not-used"
    held = MagicMock()
    held.connection.execute.return_value.fetchone.return_value = None
    captured = {}

    @contextmanager
    def guarded_job(_):
        yield MagicMock(), MagicMock()

    @contextmanager
    def factory(config, root, **kwargs):
        captured.update(kwargs)
        with pytest.raises(ProvisioningError, match="database_lock_required"):
            kwargs["writer_guard"]()
        yield provider

    @contextmanager
    def singleton(dsn):
        assert dsn == "synthetic-dsn-not-used"
        yield held

    def perform(selected_provider, selected_config, pair, retire, guard):
        assert selected_provider is provider and selected_config is configuration
        assert pair == "shared" and retire is False
        captured["writer_guard"]()
        guard()
        return 0

    monkeypatch.setattr(operator, "environment_guards", guarded_job)
    monkeypatch.setattr(operator, "service_provider", factory)
    monkeypatch.setattr(operator, "provisioner_session", singleton)
    monkeypatch.setattr(operator, "execute_environment", perform)
    monkeypatch.setattr(
        sys, "argv", ["prepare-environment.py", "--pair", "shared", "--config", "unused"]
    )
    assert operator.main() == 0
    held.claim_pending.assert_not_called()
    held.interrupt_running.assert_not_called()
    provider.seed_management_workspace.assert_called_once()
