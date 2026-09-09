import copy
import os
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import test_acceptance as base
import test_export_state as export_tests

runner_module = base.runner_module
faults = base.faults
Error = base.Error
TIMESTAMP = "2026-09-09T12:00:00+00:00"


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class APIState:
    def __init__(self):
        self.values = {
            name: {
                "onboarding_id": str(uuid4()),
                "version": 1,
                "message": "initial-" + name,
                "counter": 0,
                "pair_id": "isolated-1" if name == "isolated-c" else "shared",
            }
            for name in ("shared-a", "shared-b", "isolated-c")
        }
        self.ignore_message = False
        self.echo_requested = False
        self.reset_counter = False
        self.summary_changes = {}
        self.requested = {}
        self.on_read = lambda: None

    def control(self, tenant):
        self.on_read()
        value = self.values[tenant]
        result = {
            "tenant_id": tenant,
            "onboarding_id": value["onboarding_id"],
            "desired": {"message": value["message"], "version": value["version"]},
            "data_config": {
                "status": "applied",
                "last_applied_version": value["version"],
                "reported_at": TIMESTAMP,
            },
        }
        for key, change in self.summary_changes.items():
            if key in {"onboarding_id", "desired"}:
                result[key] = change
            else:
                result["data_config"][key] = change
        return result

    def data(self, tenant):
        value = self.values[tenant]
        return {
            "tenant_id": tenant,
            "onboarding_id": value["onboarding_id"],
            "message": value["message"],
            "applied_version": value["version"],
            "counter": value["counter"],
        }


class FakeAPI:
    def __init__(self, state, target):
        self.state, self.target = state, target
        self.key = target + "-synthetic-key"

    def get(self, tenant):
        return (
            self.state.control(tenant)
            if self.target.startswith("control:")
            else self.state.data(tenant)
        )

    def request(self, method, path, *, body=None, key=None, **_kwargs):
        if path == "/healthz":
            return 200, {"status": "ok"}, {}
        if path == "/api/container-info":
            return 404, {"detail": "not_found"}, {}
        if key is not None and key != self.key:
            return 401, {"detail": "invalid_demo_key"}, {}
        tenant = path.split("/")[2].split("?")[0]
        value = self.state.values[tenant]
        if method == "PUT":
            value["version"] += 1
            self.state.requested[tenant] = body["message"]
            if not self.state.ignore_message:
                value["message"] = body["message"]
            if self.state.reset_counter:
                value["counter"] = 0
            message = body["message"] if self.state.echo_requested else value["message"]
            return 200, {"desired": {"version": value["version"], "message": message}}, {}
        if method == "POST":
            value["counter"] += 1
        return 200, self.get(tenant), {}

    def close(self):
        pass


class FakeAPIs:
    def __init__(self, state):
        self.clients = {
            name: FakeAPI(state, name)
            for name in (
                "management",
                "control:shared",
                "data:shared",
                "control:isolated-1",
                "data:isolated-1",
            )
        }

    def client(self, name):
        return self.clients[name]

    def close(self):
        pass


class RootAuthorityTests(base.StateCase):
    def test_f022_relative_config_and_evidence_ignore_foreign_cwd(self):
        foreign = (faults.ROOT / self.root / "foreign").resolve()
        foreign.mkdir()
        relative = self.config_path
        evidence = self.root / "evidence" / "anchored.json"
        with working_directory(foreign):
            config = faults.Configuration(relative)
            self.assertEqual(config.path, (faults.ROOT / relative).resolve())
            faults.protected_write(evidence, {"root": "anchored"})
        self.assertTrue((faults.ROOT / evidence).is_file())
        self.assertFalse((foreign / evidence).exists())

    def test_f022_arbitrary_project_is_rejected(self):
        self.values["project"] = "another-project"
        self.write_config()
        with self.assertRaisesRegex(Error, "invalid_project"):
            faults.Configuration(self.config_path)

    def test_f022_cwd_cannot_authorize_an_external_config(self):
        with patch.object(faults, "read_json", return_value=self.values) as read:
            with patch.object(Path, "cwd", return_value=Path("/unrelated-project")):
                with self.assertRaisesRegex(Error, "state_path_escape"):
                    faults.Configuration(Path("/unrelated-project/.state/acceptance.json"))
            read.assert_not_called()

    def test_f022_git_commands_are_explicitly_rooted(self):
        response = subprocess.CompletedProcess(["git"], 0, "value", "")
        with patch.object(faults.subprocess, "run", return_value=response) as run:
            faults.command(["git", "rev-parse", "HEAD"])
        self.assertEqual(run.call_args.kwargs["cwd"], faults.ROOT)

    def test_f022_source_hashes_do_not_depend_on_cwd(self):
        expected = runner_module.source_hashes("data-api")
        foreign = (faults.ROOT / self.root / "foreign").resolve()
        foreign.mkdir()
        with working_directory(foreign):
            self.assertEqual(runner_module.source_hashes("data-api"), expected)


class ExportRootAndProvenanceTests(unittest.TestCase):
    setUp = export_tests.ExportTests.setUp
    exporter = export_tests.ExportTests.exporter

    def test_f022_exporter_config_uses_immutable_root(self):
        foreign = (export_tests.export_module.ROOT / self.root / "foreign").resolve()
        foreign.mkdir()
        with working_directory(foreign):
            exporter = self.exporter()
            self.assertEqual(
                exporter.config_path, (export_tests.export_module.ROOT / self.config).resolve()
            )
            exporter.run(watch=False, emit=lambda _: None)
        self.assertTrue((self.root / "acceptance.json").exists())

    def test_f027_export_includes_coordinator_and_configured_image_digests(self):
        exporter = self.exporter()
        exporter.run(watch=False, emit=lambda _: None)
        current = export_tests.json.loads((self.root / "acceptance.json").read_text())
        self.assertEqual(current["images"], self.values["images"])
        self.assertEqual(
            current["targets"]["management"]["components"]["provisioner"]["container"],
            "container-provisioner",
        )

    def test_f027_older_owned_export_is_upgraded_without_manual_edits(self):
        self.exporter().run(watch=False, emit=lambda _: None)
        path = self.root / "acceptance.json"
        previous = export_tests.json.loads(path.read_text())
        previous.pop("images")
        previous["targets"]["management"]["components"].pop("provisioner")
        path.write_text(export_tests.json.dumps(previous))
        self.exporter().run(watch=False, emit=lambda _: None)
        current = export_tests.json.loads(path.read_text())
        self.assertEqual(current["images"], self.values["images"])
        self.assertIn("provisioner", current["targets"]["management"]["components"])


class BehavioralFixTests(base.StateCase):
    def make_runner(self):
        self.api_state = APIState()
        runner = runner_module.Runner(
            self.config,
            "all",
            apis=FakeAPIs(self.api_state),
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        runner.tenants = {
            name: {"pair_id": value["pair_id"], "onboarding_id": value["onboarding_id"]}
            for name, value in self.api_state.values.items()
        }
        return runner

    def test_f023_ignored_put_message_is_not_accepted(self):
        runner = self.make_runner()
        self.api_state.ignore_message = True
        with self.assertRaisesRegex(Error, "configuration_update_response_ignored_message"):
            runner.check_updates_and_counters()

    def test_f023_echoed_but_unpersisted_message_is_not_accepted(self):
        runner = self.make_runner()
        self.api_state.ignore_message = True
        self.api_state.echo_requested = True
        with self.assertRaisesRegex(Error, "requested_configuration_message_not_persisted"):
            runner.check_updates_and_counters()

    def test_f023_counter_reset_during_configuration_is_not_accepted(self):
        runner = self.make_runner()
        self.api_state.reset_counter = True
        with self.assertRaisesRegex(Error, "counter_changed_during_configuration"):
            runner.check_updates_and_counters()

    def test_f023_requests_and_counters_are_checked_after_management_poll(self):
        runner = self.make_runner()
        runner.check_updates_and_counters()
        runner.control_poll_observed = Mock(return_value=True)
        runner.assert_updates_survived_poll()
        self.assertEqual(runner.control_poll_observed.call_count, 2)
        for tenant, expected in runner.expectations.items():
            self.assertEqual(expected["message"], self.api_state.requested[tenant])
            self.assertGreater(expected["counter"], 0)
        self.api_state.values["shared-a"]["counter"] = 0
        with self.assertRaisesRegex(Error, "counter_changed_after_management_poll"):
            runner.assert_updates_survived_poll()

    def test_f023_management_poll_cannot_reset_requested_configuration(self):
        runner = self.make_runner()
        runner.check_updates_and_counters()

        def poll(*_args):
            self.api_state.values["shared-a"]["message"] = "reset-to-initial"
            return True

        runner.control_poll_observed = poll
        with self.assertRaisesRegex(Error, "configuration_changed_after_management_poll"):
            runner.assert_updates_survived_poll()

    def test_f024_stale_applied_version_is_rejected(self):
        runner = self.make_runner()
        self.api_state.values["shared-a"].update(version=2, message="requested")
        self.api_state.summary_changes["last_applied_version"] = 1
        with self.assertRaisesRegex(Error, "control_applied_version_mismatch"):
            runner.applied("shared-a", version=2)

    def test_f024_missing_or_invalid_report_timestamp_is_rejected(self):
        for timestamp in (None, "", "not-a-time", "2026-09-09T12:00:00"):
            with self.subTest(timestamp=timestamp):
                runner = self.make_runner()
                self.api_state.summary_changes["reported_at"] = timestamp
                with self.assertRaisesRegex(Error, "report_timestamp"):
                    runner.applied("shared-a")

    def test_f024_control_onboarding_and_desired_version_must_match(self):
        runner = self.make_runner()
        self.api_state.summary_changes["onboarding_id"] = str(uuid4())
        with self.assertRaisesRegex(Error, "control_onboarding_mismatch"):
            runner.applied("shared-a")
        self.api_state.summary_changes = {}
        with self.assertRaisesRegex(Error, "desired_version_mismatch"):
            runner.applied("shared-a", version=2)

    def test_f025_empty_control_timeline_cannot_pass_collection(self):
        runner = self.make_runner()
        management = [
            {"event_id": 1, "type": "tenant_requested", "version": 1},
            {"event_id": 2, "type": "control_record_created", "version": 1},
        ]
        with patch.object(
            runner_module,
            "timeline",
            side_effect=lambda _client, _tenant, parent: (
                management if parent == "management" else []
            ),
        ):
            with self.assertRaisesRegex(Error, "configuration_created_event_missing"):
                runner.collect_timelines()

    def history(self):
        runner = self.make_runner()
        self.api_state.values["shared-a"].update(version=3, message="latest")
        control = self.api_state.control("shared-a")
        onboarding = runner.tenants["shared-a"]["onboarding_id"]
        rows = [
            {"event_id": 1, "type": "configuration_created", "version": 1},
            {"event_id": 3, "type": "configuration_updated", "version": 2},
            {"event_id": 4, "type": "configuration_updated", "version": 3},
            {"event_id": 8, "type": "config_applied", "version": 3, "received_at": TIMESTAMP},
        ]
        return control, onboarding, rows

    def test_f025_missing_update_or_latest_report_is_rejected(self):
        control, onboarding, rows = self.history()
        with self.assertRaisesRegex(Error, "configuration_updated_events_incomplete"):
            runner_module.control_history([rows[0], *rows[2:]], control, onboarding)
        with self.assertRaisesRegex(Error, "latest_applied_report_missing"):
            runner_module.control_history(rows[:-1], control, onboarding)

    def test_f025_skipped_intermediate_applied_versions_remain_valid(self):
        control, onboarding, rows = self.history()
        runner_module.control_history(rows, control, onboarding)
        rows[-1]["received_at"] = "2026-09-09T11:00:00+00:00"
        with self.assertRaisesRegex(Error, "applied_report_timestamp_mismatch"):
            runner_module.control_history(rows, control, onboarding)

    def test_f026_cleanup_consumes_the_same_recovery_deadline(self):
        kube = base.FakeKube(self.config.target("shared-control"))
        clock = self.clock

        class SlowProbe(base.FakeProbe):
            def request(self, action):
                if action == "fresh" and not self.kube.policy:
                    clock.sleep(12)
                return super().request(action)

            def close(self):
                if not self.closed:
                    clock.sleep(8)
                super().close()

        fault = faults.ParentFault(
            self.config,
            "shared-control",
            "control-reconciler",
            self.root / "evidence" / "deadline.json",
            kube_factory=lambda _: kube,
            probe_factory=SlowProbe,
            clock=clock,
            sleep=clock.sleep,
        )
        with fault:
            pass
        self.assertTrue(fault.record["restored"])
        self.assertEqual(fault.recovery_started, 0)
        self.assertEqual(fault.recovery_deadline, 30)
        self.assertEqual(clock(), 28)
        runner = self.make_runner()
        self.api_state.on_read = lambda: clock.sleep(3)
        with self.assertRaisesRegex(Error, "convergence_deadline_exceeded"):
            runner.applied("shared-a", deadline=fault.recovery_deadline)

    def test_f026_already_expired_deadline_does_not_poll_again(self):
        runner = self.make_runner()
        self.clock.sleep(31)
        self.api_state.on_read = Mock()
        with self.assertRaisesRegex(Error, "convergence_deadline_exceeded"):
            runner.applied("shared-a", deadline=30)
        self.api_state.on_read.assert_not_called()

    def test_f026_late_successful_restore_query_does_not_pass(self):
        kube = base.FakeKube(self.config.target("shared-control"))
        clock = self.clock

        class LateProbe(base.FakeProbe):
            def request(self, action):
                if action == "fresh" and not self.kube.policy:
                    clock.sleep(31)
                return super().request(action)

        fault = faults.ParentFault(
            self.config,
            "shared-control",
            "control-reconciler",
            self.root / "evidence" / "late-restore.json",
            kube_factory=lambda _: kube,
            probe_factory=LateProbe,
            clock=clock,
            sleep=clock.sleep,
        )
        with self.assertRaisesRegex(Error, "fault_restoration_failed"):
            with fault:
                pass
        self.assertIsNone(kube.policy)
        self.assertFalse(fault.record["restored"])


class ProvenanceFixTests(base.StateCase):
    def runner(self):
        return runner_module.Runner(
            self.config,
            "all",
            apis=Mock(),
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def kube(self, component, uid=base.POD_UID):
        role = "provisioner" if component == "provisioner" else "api"
        image = self.values["images"][role]
        pod = {
            "metadata": {"name": component + "-pod", "uid": uid},
            "spec": {"containers": [{"name": component, "image": image}]},
            "status": {
                "containerStatuses": [
                    {
                        "name": component,
                        "imageID": "registry/runtime@sha256:" + "c" * 64,
                    }
                ]
            },
        }
        kube = Mock()
        kube.target = SimpleNamespace(
            slot="management", component=lambda _: {"container": component}
        )
        kube.pod.return_value = pod
        kube.exec_json.return_value = {
            "files": runner_module.source_hashes(component),
            "parent_dsn_present": False,
        }
        return kube, pod

    def test_f027_coordinator_manifest_covers_provider_and_bootstrap_code(self):
        files = runner_module.source_files("provisioner")
        for name in (
            "src/plane_demo/management/provisioner.py",
            "src/plane_demo/management/provisioning.py",
            "src/plane_demo/management/providers/azure.py",
            "src/plane_demo/management/providers/commands.py",
            "src/plane_demo/management/providers/credentials.py",
            "operations/run-certificate-job.py",
            "infra/radius/modules/child-cluster.bicep",
        ):
            self.assertIn(name, files)

    def test_f027_provider_hash_mismatch_fails_even_when_api_hashes_match(self):
        runner = self.runner()
        kube, _ = self.kube("provisioner")
        path = "src/plane_demo/management/providers/azure.py"
        kube.exec_json.return_value["files"][path] = "0" * 64
        with self.assertRaisesRegex(Error, "deployed_source_hash_mismatch"):
            runner.verify_workload(kube, "provisioner")

    def test_f027_wrong_coordinator_image_reference_is_rejected(self):
        runner = self.runner()
        kube, pod = self.kube("provisioner")
        pod["spec"]["containers"][0]["image"] = self.values["images"]["api"]
        with self.assertRaisesRegex(Error, "workload_image_reference_mismatch"):
            runner.verify_workload(kube, "provisioner")
        kube.exec_json.assert_not_called()

    def test_f027_management_run_path_checks_api_and_coordinator(self):
        runner = self.runner()
        kube = Mock()
        runner.kube = Mock(return_value=kube)
        runner.verify_workload = Mock()
        runner.management_image()
        self.assertEqual(
            [call.args[1] for call in runner.verify_workload.call_args_list],
            ["management-api", "provisioner"],
        )

    def test_f027_replacement_data_pod_is_rehashed_before_restart_is_accepted(self):
        runner = self.runner()
        new_uid = str(uuid4())
        kube, new = self.kube("data-api", new_uid)
        old = copy.deepcopy(new)
        old["metadata"]["uid"] = base.POD_UID
        old["metadata"]["name"] = "old-data-api"
        kube.pod.side_effect = [old, new, new]
        kube.deployment.return_value = {
            "metadata": {"name": "data-api", "uid": base.POLICY_UID},
            "spec": {"replicas": 1},
        }
        kube.exec_json.return_value["files"]["src/plane_demo/data/api.py"] = "0" * 64
        runner.kube = Mock(return_value=kube)
        baseline = {"onboarding_id": str(uuid4()), "applied_version": 1, "counter": 9}
        runner.apis.client.return_value.get.return_value = baseline
        with self.assertRaisesRegex(Error, "deployed_source_hash_mismatch"):
            runner.restart_data_api("shared", "shared-a", baseline)
        self.assertFalse(
            any(
                item["type"] == "data_api_restarted_without_parent"
                for item in runner.record["events"]
            )
        )
