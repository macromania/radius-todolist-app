import copy
import importlib.util
import io
import json
import shutil
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

SPEC = importlib.util.spec_from_file_location(
    "acceptance_runner_under_test", Path(__file__).resolve().parents[2] / "harness/test-e2e.py"
)
runner_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner_module
SPEC.loader.exec_module(runner_module)
faults = runner_module.faults
Error = faults.AcceptanceError
POD_UID = "11111111-1111-4111-8111-111111111111"
POLICY_UID = "22222222-2222-4222-8222-222222222222"


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class StateCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(".state") / ("e2e-unit-" + uuid4().hex)
        self.root.mkdir(parents=True, mode=0o700)
        self.addCleanup(lambda: shutil.rmtree(self.root))
        for name in ("cluster.kubeconfig", "management.key"):
            (self.root / name).write_text("synthetic-unit-value-" + "x" * 40)
            (self.root / name).chmod(0o600)
        (self.root / "cluster.kubeconfig").write_text(
            json.dumps(
                {
                    "contexts": [
                        {"name": "radplanes-shared-control", "context": {"cluster": "scoped"}}
                    ],
                    "clusters": [
                        {"name": "scoped", "cluster": {"server": "https://cluster.example.test"}}
                    ],
                }
            )
        )
        self.values = {
            "version": 1,
            "environment": "azure",
            "project": "radplanes",
            "synthetic_data": True,
            "endpoints_file": "endpoints.json",
            "images": {
                "api": "project.azurecr.io/api@sha256:" + "a" * 64,
                "provisioner": "project.azurecr.io/provisioner@sha256:" + "b" * 64,
            },
            "targets": {
                "shared-control": {
                    "context": "radplanes-shared-control",
                    "kubeconfig": "cluster.kubeconfig",
                    "namespace": "radplanes-shared-control-control",
                    "cluster_uid": "33333333-3333-4333-8333-333333333333",
                    "namespace_uid": "44444444-4444-4444-8444-444444444444",
                    "components": {
                        "control-reconciler": {
                            "deployment": "control-reconciler",
                            "container": "control-reconciler",
                        }
                    },
                    "parent": {
                        "host": "management.example.test",
                        "port": 5432,
                        "allowed_cidrs": ["10.42.32.0/27"],
                    },
                }
            },
        }
        self.config_path = self.root / "acceptance.json"
        self.write_config()
        self.config = faults.Configuration(self.config_path)
        self.clock = Clock()

    def write_config(self):
        self.config_path.write_text(json.dumps(self.values))


class FakeKube:
    def __init__(self, target):
        self.target = target
        self.policy = None
        self.create_then_fail = False
        self.delete_fails = False
        self.never_enforces = False
        self.existing_survives = False
        self.local_fails = False
        self.scope_ok = True
        self.addresses = ["10.42.32.4"]
        self.calls = []
        self.probes = []

    def verify_scope(self):
        faults.require(self.scope_ok, "cluster_uid_mismatch")

    def pod(self, _component):
        return {
            "metadata": {"name": "control-reconciler-pod", "uid": POD_UID},
            "status": {
                "containerStatuses": [
                    {"name": "control-reconciler", "imageID": "registry/api@sha256:" + "a" * 64}
                ]
            },
        }

    def policies(self, exclude=None):
        if self.policy and self.policy["metadata"]["name"] != exclude:
            return [
                {
                    "kind": "CiliumNetworkPolicy",
                    "name": self.policy["metadata"]["name"],
                    "uid": self.policy["metadata"]["uid"],
                    "spec": self.policy["spec"],
                }
            ]
        return []

    def json(self, *args):
        self.calls.append(args)
        if args[1] == "customresourcedefinition":
            return {
                "spec": {"versions": [{"name": "v2", "served": True}]},
                "status": {"conditions": [{"type": "Established", "status": "True"}]},
            }
        return copy.deepcopy(self.policy)

    def optional(self, _resource, _name):
        return copy.deepcopy(self.policy)

    def run(self, *args, payload=None, **_kwargs):
        self.calls.append(args)
        if args[0] == "create":
            self.policy = json.loads(payload)
            self.policy["metadata"]["uid"] = POLICY_UID
            if self.create_then_fail:
                raise Error("ambiguous_create_response")
        return ""

    def delete_uid(self, plural, name, uid, *, group=""):
        self.calls.append(("delete_uid", plural, name, uid, group))
        if self.delete_fails:
            raise Error("delete_failed")
        self.assert_uid = uid
        self.policy = None


class FakeProbe:
    def __init__(self, kube, _component, _pod):
        self.kube = kube
        self.closed = False
        kube.probes.append(self)

    def request(self, action):
        blocked = bool(self.kube.policy) and not self.kube.never_enforces
        if action == "baseline":
            ok = True
        elif action == "local":
            ok = not (blocked and self.kube.local_fails)
        elif action == "existing":
            ok = not blocked or self.kube.existing_survives
        else:
            ok = not blocked
        return {"ok": ok, "network_failure": not ok, "ips": self.kube.addresses}

    def close(self):
        self.closed = True


class FaultTests(StateCase):
    def make_fault(self):
        self.kube = FakeKube(self.config.target("shared-control"))
        return faults.ParentFault(
            self.config,
            "shared-control",
            "control-reconciler",
            self.root / "evidence" / "fault.json",
            kube_factory=lambda _target: self.kube,
            probe_factory=FakeProbe,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def test_deny_is_exact_scoped_and_does_not_enable_default_deny(self):
        target = self.config.target("shared-control")
        cidrs = faults.parent_cidrs(target, ["10.42.32.4"])
        policy = faults.deny_policy(target, "control-reconciler", cidrs, "a" * 12)
        self.assertEqual(policy["metadata"]["namespace"], target.namespace)
        self.assertEqual(
            policy["spec"]["endpointSelector"]["matchLabels"],
            {
                "plane-demo/project": "radplanes",
                "plane-demo/component": "control-reconciler",
            },
        )
        self.assertEqual(policy["spec"]["enableDefaultDeny"], {"ingress": False, "egress": False})
        self.assertEqual(
            policy["spec"]["egressDeny"],
            [
                {
                    "toCIDR": ["10.42.32.4/32"],
                    "toPorts": [{"ports": [{"port": "5432", "protocol": "TCP"}]}],
                }
            ],
        )
        self.assertNotIn("egress", policy["spec"])

    def test_wide_public_loopback_and_unallocated_destinations_are_rejected(self):
        target = self.config.target("shared-control")
        for address in ("8.8.8.8", "127.0.0.1", "169.254.169.254", "10.99.0.1"):
            with self.subTest(address=address), self.assertRaises(Error):
                faults.parent_cidrs(target, [address])
        with self.assertRaises(Error):
            faults.deny_policy(target, "control-reconciler", ["10.42.32.0/27"], "a" * 12)

    def test_fault_proves_both_connections_and_restores_original_policy_set(self):
        fault = self.make_fault()
        with fault:
            self.assertIsNotNone(self.kube.policy)
            fault.assert_blocked()
        self.assertIsNone(self.kube.policy)
        self.assertEqual(self.kube.assert_uid, POLICY_UID)
        self.assertTrue(fault.record["restored"])
        self.assertEqual(fault.record["original_policies"], fault.record["restored_policies"])
        actions = [value["action"] for value in fault.record["probes"]]
        self.assertIn("fresh", actions)
        self.assertIn("existing", actions)
        self.assertIn("local", actions)
        self.assertTrue(all(probe.closed for probe in self.kube.probes))

    def test_body_failure_still_removes_fault(self):
        fault = self.make_fault()
        with self.assertRaisesRegex(Error, "sample_failed"):
            with fault:
                raise Error("sample_failed")
        self.assertIsNone(self.kube.policy)
        self.assertTrue(fault.record["restored"])
        self.assertEqual(fault.record["outcome"], "failed")

    def test_ambiguous_create_is_looked_up_and_removed(self):
        fault = self.make_fault()
        self.kube.create_then_fail = True
        with self.assertRaisesRegex(Error, "ambiguous_create"):
            with fault:
                self.fail("activation should fail")
        self.assertIsNone(self.kube.policy)
        self.assertTrue(fault.record["restored"])

    def test_non_enforced_policy_is_not_a_success(self):
        fault = self.make_fault()
        self.kube.never_enforces = True
        with self.assertRaisesRegex(Error, "not_enforced"):
            with fault:
                self.fail("policy creation alone must not pass")
        self.assertIsNone(self.kube.policy)
        self.assertTrue(fault.record["restored"])

    def test_surviving_existing_connection_fails_and_restores(self):
        fault = self.make_fault()
        self.kube.existing_survives = True
        with self.assertRaisesRegex(Error, "existing_connection_not_blocked"):
            with fault:
                self.fail("an existing connection remained usable")
        self.assertIsNone(self.kube.policy)

    def test_fault_must_preserve_local_database_or_kubernetes_access(self):
        fault = self.make_fault()
        self.kube.local_fails = True
        with self.assertRaisesRegex(Error, "local_prerequisite"):
            with fault:
                self.fail("local access was also blocked")
        self.assertIsNone(self.kube.policy)

    def test_restoration_failure_is_never_swallowed(self):
        fault = self.make_fault()
        with self.assertRaisesRegex(Error, "fault_restoration_failed"):
            with fault:
                self.kube.delete_fails = True
        self.assertFalse(fault.record["restored"])
        self.assertEqual(fault.record["outcome"], "restoration_failed")

    def test_replaced_policy_is_not_deleted(self):
        fault = self.make_fault()
        with self.assertRaisesRegex(Error, "fault_restoration_failed"):
            with fault:
                self.kube.policy["metadata"]["uid"] = str(uuid4())
        self.assertIsNotNone(self.kube.policy)
        self.assertFalse(any(call[0] == "delete_uid" for call in self.kube.calls))

    def test_failed_scope_check_never_creates_a_policy(self):
        fault = self.make_fault()
        self.kube.scope_ok = False
        with self.assertRaisesRegex(Error, "cluster_uid_mismatch"):
            with fault:
                self.fail("wrong cluster")
        self.assertFalse(any(call[0] == "create" for call in self.kube.calls))

    def test_manual_restore_validates_recorded_identity(self):
        fault = self.make_fault()
        fault.activate()
        restored = faults.ParentFault.from_evidence(
            self.config,
            fault.evidence_path,
            kube_factory=lambda _target: self.kube,
            probe_factory=FakeProbe,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        restored.restore()
        fault.probe.close()
        self.assertIsNone(self.kube.policy)
        self.assertTrue(restored.record["restored"])

    def test_manual_restore_refuses_changed_namespace_uid(self):
        fault = self.make_fault()
        fault.activate()
        self.values["targets"]["shared-control"]["namespace_uid"] = str(uuid4())
        self.write_config()
        with self.assertRaisesRegex(Error, "identity_changed"):
            faults.ParentFault.from_evidence(self.config, fault.evidence_path)
        self.write_config()
        fault.restore()

    def test_local_configuration_requires_its_own_state_before_kubernetes(self):
        self.values["environment"] = "local"
        self.write_config()
        factory = Mock()
        with self.assertRaisesRegex(Error, "local_configuration_scope"):
            faults.ParentFault(
                faults.Configuration(self.config_path),
                "shared-control",
                "control-reconciler",
                self.root / "evidence" / "fault.json",
                kube_factory=factory,
            )
        factory.assert_not_called()


class ConfigAndHTTPTests(StateCase):
    def test_failed_evidence_write_preserves_last_complete_restore_record(self):
        path = self.root / "evidence" / "record.json"
        faults.protected_write(path, {"policy_uid": POLICY_UID})
        with patch.object(faults.json, "dump", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                faults.protected_write(path, {"policy_uid": "incomplete"})
        self.assertEqual(json.loads(path.read_text()), {"policy_uid": POLICY_UID})
        self.assertEqual(list(path.parent.glob("*.writing")), [])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_insecure_kubeconfig_is_rejected_before_any_cluster_command(self):
        path = self.root / "cluster.kubeconfig"
        values = json.loads(path.read_text())
        values["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
        path.write_text(json.dumps(values))
        with self.assertRaisesRegex(Error, "insecure_kubernetes_transport"):
            self.config.target("shared-control")

    def test_key_must_be_private_and_stay_in_environment_state(self):
        with self.assertRaisesRegex(Error, "path_escape"):
            self.config.file("../somewhere.key", secret=True)
        (self.root / "management.key").chmod(0o644)
        with self.assertRaisesRegex(Error, "permissions"):
            self.config.file("management.key", secret=True)

    def test_kubectl_never_uses_implicit_context_or_namespace(self):
        target = self.config.target("shared-control")
        kube = faults.Kubectl(target)
        argv = kube.argv("get", "pods")
        self.assertIn(str(target.kubeconfig), argv)
        self.assertEqual(argv[argv.index("--context") + 1], target.context)
        self.assertEqual(argv[argv.index("--namespace") + 1], target.namespace)
        self.assertIn("--request-timeout=0", kube.argv("exec", streaming=True))
        self.assertIn("--request-timeout=15s", argv)

    def test_kubectl_scope_checks_both_actual_uids(self):
        target = self.config.target("shared-control")
        responses = [
            json.dumps({"metadata": {"uid": target.cluster_uid}}),
            json.dumps({"metadata": {"uid": str(uuid4())}}),
        ]
        kube = faults.Kubectl(target, runner=Mock(side_effect=responses))
        with self.assertRaisesRegex(Error, "namespace_uid_mismatch"):
            kube.verify_scope()

    def test_policy_snapshot_detects_changes_without_copying_sensitive_rules(self):
        target = self.config.target("shared-control")
        secret = "must-not-copy-header-value"
        values = [
            {"items": []},
            {
                "items": [
                    {
                        "kind": "CiliumNetworkPolicy",
                        "metadata": {"name": "original-policy", "uid": POLICY_UID},
                        "spec": {"egress": [{"header": secret}]},
                    }
                ]
            },
        ]
        kube = faults.Kubectl(
            target, runner=Mock(side_effect=[json.dumps(value) for value in values])
        )
        snapshot = kube.policies()
        self.assertNotIn(secret, json.dumps(snapshot))
        self.assertEqual(len(snapshot[0]["spec_sha256"]), 64)

    def test_http_uses_header_auth_and_does_not_follow_redirect(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://other.example.test"})

        client = runner_module.Client(
            "https://test.centralus.cloudapp.azure.com",
            "unit-" + "x" * 40,
            transport=httpx.MockTransport(handler),
        )
        self.addCleanup(client.close)
        with self.assertRaisesRegex(Error, "unexpected_api_status"):
            client.request("GET", "/tenants/alpha")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["X-Demo-Key"], client.key)
        self.assertNotIn(client.key, str(requests[0].url))

    def test_server_error_content_is_not_reflected(self):
        secret = "not-for-evidence-secret"
        client = runner_module.Client(
            "https://test.centralus.cloudapp.azure.com",
            "unit-" + "x" * 40,
            transport=httpx.MockTransport(lambda _: httpx.Response(500, text=secret)),
        )
        self.addCleanup(client.close)
        with self.assertRaises(Error) as error:
            client.request("GET", "/tenants/alpha")
        self.assertNotIn(secret, str(error.exception))

    def test_response_stream_is_bounded(self):
        client = runner_module.Client(
            "https://test.centralus.cloudapp.azure.com",
            "unit-" + "x" * 40,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1_000_001)),
        )
        self.addCleanup(client.close)
        with self.assertRaisesRegex(Error, "response_too_large"):
            client.request("GET", "/tenants/alpha")

    def test_actual_pod_programs_parse_with_project_interpreter(self):
        for name, code in (
            ("parent_probe", faults.PROBE_CODE),
            ("source_probe", runner_module.SOURCE_PROBE),
            ("identity_probe", runner_module.IDENTITY_PROBE),
            ("data_api_permissions_probe", runner_module.DATA_API_PERMISSIONS_PROBE),
        ):
            with self.subTest(name=name):
                compile(code, name, "exec")
        self.assertIn('tcp_user_timeout="3000"', faults.PROBE_CODE)
        self.assertIn('connection.execute("SELECT 1")', faults.PROBE_CODE)

    def test_both_clis_require_explicit_execution(self):
        for main in (runner_module.main, faults.main):
            with self.subTest(main=main), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["--config", str(self.config_path)])
                self.assertEqual(error.exception.code, 2)


class TimelineAndDeadlineTests(unittest.TestCase):
    def test_timeline_reconstructs_all_pages_without_assuming_consecutive_ids(self):
        rows = [
            {"event_id": 1, "type": "configuration_created", "version": 1},
            {"event_id": 4, "type": "configuration_updated", "version": 2},
            {"event_id": 7, "type": "config_applied", "version": 2},
        ]
        client = Mock()
        client.request.side_effect = [
            (200, {"timeline": rows[:2], "next_after_event_id": 4}, {}),
            (200, {"timeline": rows[2:], "next_after_event_id": None}, {}),
        ]
        self.assertEqual(runner_module.timeline(client, "alpha", "control"), rows)
        self.assertIn("after_event_id=4", client.request.call_args.args[1])

    def test_transitive_or_repeated_child_reports_are_rejected(self):
        for parent, rows in (
            ("management", [{"event_id": 1, "type": "config_applied", "version": 1}]),
            (
                "control",
                [
                    {"event_id": 1, "type": "config_applied", "version": 1},
                    {"event_id": 2, "type": "config_applied", "version": 1},
                ],
            ),
        ):
            client = Mock()
            client.request.return_value = (
                200,
                {"timeline": rows, "next_after_event_id": None},
                {},
            )
            with self.subTest(parent=parent), self.assertRaises(Error):
                runner_module.timeline(client, "alpha", parent)

    def test_success_arriving_after_deadline_still_fails(self):
        clock = Clock()

        def slow():
            clock.sleep(31)
            return True

        with self.assertRaisesRegex(Error, "deadline"):
            runner_module.until(slow, timeout=30, clock=clock, sleep=clock.sleep)

    def test_exact_deadline_false_does_not_loop_forever(self):
        clock = Clock()
        with self.assertRaisesRegex(Error, "deadline"):
            runner_module.until(lambda: False, timeout=2, clock=clock, sleep=clock.sleep)
        self.assertEqual(clock.value, 2)


class RunnerTests(StateCase):
    def new_runner(self, apis=None):
        return runner_module.Runner(
            self.config,
            "all",
            apis=apis or Mock(),
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def test_real_onboard_path_calls_202_duplicate_409_and_busy_503(self):
        tenant, operation_id, onboarding_id = "shared-a", str(uuid4()), str(uuid4())
        calls = []
        responses = [
            (404, {"detail": "tenant_not_found"}, {}),
            (202, {"operation_id": operation_id, "status_url": "/tenants/shared-a"}, {}),
            (409, {"status_url": "/tenants/shared-a"}, {"Location": "/tenants/shared-a"}),
            (200, {"status": "running"}, {}),
            (503, {"detail": "provisioner_busy"}, {"Retry-After": "5"}),
            (404, {"detail": "tenant_not_found"}, {}),
            (
                200,
                {
                    "tenant_id": tenant,
                    "operation_id": operation_id,
                    "onboarding_id": onboarding_id,
                    "pair_id": "shared",
                    "provisioning_status": "running",
                    "onboarding_status": "ready",
                    "control_record": {
                        "status": "created",
                        "observed_revision": 1,
                        "reported_at": "2026-09-09T00:00:00Z",
                    },
                },
                {},
            ),
            (200, {"status": "succeeded"}, {}),
            (409, {"status_url": "/tenants/shared-a"}, {}),
        ]

        def handler(request):
            calls.append((request.method, request.url.path))
            status, body, headers = responses.pop(0)
            return httpx.Response(status, json=body, headers=headers)

        client = runner_module.Client(
            "https://test.centralus.cloudapp.azure.com",
            "unit-" + "x" * 40,
            transport=httpx.MockTransport(handler),
        )
        self.addCleanup(client.close)
        apis = Mock()
        apis.client.return_value = client
        runner = self.new_runner(apis)
        result = runner.onboard(tenant, "shared", check_busy=True)
        self.assertEqual(result["pair_id"], "shared")
        self.assertFalse(responses)
        self.assertEqual(sum(method == "POST" for method, _ in calls), 4)
        self.assertTrue(runner.record["events"][0]["busy_verified"])

    def test_continuity_has_ten_successes_over_sixty_seconds_and_restart(self):
        counter = 3

        def current():
            return {
                "counter": counter,
                "applied_version": 2,
                "onboarding_id": "onboarding",
                "message": "last applied",
            }

        def increment(*_args, **_kwargs):
            nonlocal counter
            counter += 1
            return 200, current(), {}

        data = Mock()
        data.get.side_effect = lambda _tenant: current()
        data.request.side_effect = increment
        apis = Mock()
        apis.client.return_value = data
        runner = self.new_runner(apis)
        runner.tenants["shared-a"] = {"pair_id": "shared"}
        runner.restart_data_api = Mock()
        fault = SimpleNamespace(component="data-reconciler", assert_blocked=Mock())
        runner.continuity(fault, "shared-a", 2, restart=True)
        self.assertEqual(counter, 13)
        self.assertGreaterEqual(self.clock.value, 60)
        self.assertEqual(data.request.call_count, 10)
        self.assertEqual(fault.assert_blocked.call_count, 3)
        runner.restart_data_api.assert_called_once()

    def test_control_outage_run_path_uses_data_parent_fault_and_latest_only(self):
        runner = self.new_runner()
        runner.tenants["shared-a"] = {"pair_id": "shared"}
        control = Mock()
        versions = iter((2, 3))
        control.request.side_effect = lambda *_args, **kwargs: (
            200,
            {"desired": {"version": next(versions), "message": kwargs["body"]["message"]}},
            {},
        )
        control.get.return_value = {"data_config": {"status": "pending"}}
        runner.apis.client.return_value = control
        runner.applied = Mock(
            side_effect=[
                {"applied_version": 1, "counter": 3},
                {"applied_version": 3, "counter": 13},
            ]
        )
        runner.continuity = Mock(return_value={"counter": 13})
        context = Mock()
        context.__enter__ = Mock(return_value=context)
        context.__exit__ = Mock(return_value=False)
        context.record = {}
        context.recovery_deadline = 30
        runner.fault_factory = Mock(return_value=context)
        with patch.object(
            runner_module,
            "timeline",
            return_value=[
                {"type": "config_applied", "version": 1},
                {"type": "config_applied", "version": 3},
            ],
        ):
            runner.control_outage()
        self.assertEqual(
            runner.fault_factory.call_args.args[1:3], ("shared-data", "data-reconciler")
        )
        runner.continuity.assert_called_once_with(context, "shared-a", 1, restart=True)
        self.assertEqual(runner.applied.call_args.kwargs["version"], 3)
        context.__exit__.assert_called_once()

    def test_management_outage_uses_control_parent_fault_and_checks_reporting_recovery(self):
        runner = self.new_runner()
        runner.tenants["shared-a"] = {"pair_id": "shared"}
        control = Mock()
        control.request.side_effect = lambda *_args, **kwargs: (
            200,
            {"desired": {"version": 2, "message": kwargs["body"]["message"]}},
            {},
        )
        runner.apis.client.return_value = control
        runner.applied = Mock(return_value={"applied_version": 1, "counter": 3})
        runner.continuity = Mock()
        context = Mock()
        context.record = {"restoration_started_at": "2026-09-09T09:00:00+00:00"}
        context.recovery_started = 0
        context.recovery_deadline = 30
        context.__enter__ = Mock(return_value=context)
        context.__exit__ = Mock(return_value=False)
        runner.fault_factory = Mock(return_value=context)
        kube = Mock()
        kube.pod.return_value = {"metadata": {"name": "scoped-reconciler"}}
        kube.target.component.return_value = {"container": "control-reconciler"}
        kube.run.return_value = (
            "2026-09-09T09:00:01Z INFO control_poll examined=2 succeeded=2 failed=0"
        )
        runner.kube = Mock(return_value=kube)
        with patch.object(runner_module, "timeline", return_value=[]):
            runner.management_outage()
        self.assertEqual(
            runner.fault_factory.call_args.args[1:3], ("shared-control", "control-reconciler")
        )
        runner.continuity.assert_called_once_with(context, "shared-a", 2)
        self.assertEqual(runner.record["events"][-1]["type"], "management_link_recovered")

    def test_restart_deletes_only_old_pod_uid_and_keeps_one_replica(self):
        runner = self.new_runner()
        runner.verify_workload = Mock()
        kube = Mock()
        deployment = {
            "metadata": {"name": "data-api", "uid": POLICY_UID},
            "spec": {"replicas": 1},
        }
        new_uid = str(uuid4())
        kube.deployment.return_value = deployment
        kube.pod.side_effect = [
            {"metadata": {"name": "data-api-old", "uid": POD_UID}},
            {"metadata": {"name": "data-api-new", "uid": new_uid}},
        ]
        runner.kube = Mock(return_value=kube)
        baseline = {"applied_version": 1, "onboarding_id": str(uuid4()), "counter": 12}
        runner.apis.client.return_value.get.return_value = baseline
        runner.restart_data_api("shared", "shared-a", baseline)
        kube.delete_uid.assert_called_once_with("pods", "data-api-old", POD_UID)
        patch_body = json.loads(kube.run.call_args.args[-1])
        self.assertEqual(
            patch_body[-1],
            {
                "op": "replace",
                "path": "/spec/replicas",
                "value": 1,
            },
        )
        self.assertEqual(runner.record["events"][-1]["new_pod_uid"], new_uid)
        runner.verify_workload.assert_called_once_with(
            kube, "data-api", pod={"metadata": {"name": "data-api-new", "uid": new_uid}}
        )

    def test_top_level_run_invokes_both_outages_and_records_failure(self):
        runner = self.new_runner()
        runner.management_image = Mock()
        runner.scenario = Mock()
        runner.management_outage = Mock()
        runner.control_outage = Mock(side_effect=Error("outage_failed"))
        with (
            patch.object(faults, "command", return_value=""),
            patch.object(faults, "source_metadata", return_value={"commit": "a" * 40}),
        ):
            with self.assertRaisesRegex(Error, "outage_failed"):
                runner.run()
        runner.scenario.assert_called_once()
        runner.management_outage.assert_called_once()
        runner.control_outage.assert_called_once()
        self.assertEqual(runner.record["outcome"], "failed")
        self.assertEqual(json.loads(runner.path.read_text())["error"], "outage_failed")
        runner.apis.close.assert_called_once()

    def pause_fixture(self, grace=30, drain_at=0):
        kube = Mock()
        state = {"replicas": 1}
        target = {
            "metadata": {"name": "data-reconciler", "uid": POD_UID},
            "spec": {
                "replicas": 1,
                "template": {"spec": {"terminationGracePeriodSeconds": grace}},
            },
        }

        def deployment(_component):
            return {**target, "spec": {**target["spec"], "replicas": state["replicas"]}}

        def run(*args, **_kwargs):
            if args[0] == "patch":
                operations = json.loads(args[-1])
                self.assertEqual(
                    operations[0],
                    {
                        "op": "test",
                        "path": "/metadata/uid",
                        "value": POD_UID,
                    },
                )
                state["replicas"] = operations[-1]["value"]
            return ""

        kube.deployment.side_effect = deployment
        kube.run.side_effect = run
        kube.labels.return_value = {"plane-demo/project": "radplanes"}
        kube.json.side_effect = lambda *_: {"items": [{}] if self.clock() < drain_at else []}
        return kube, state

    def test_pause_restores_replica_after_assertion_failure(self):
        kube, state = self.pause_fixture()
        with self.assertRaisesRegex(Error, "deliberate"):
            with runner_module.paused_reconciler(kube, clock=self.clock, sleep=self.clock.sleep):
                self.assertEqual(state["replicas"], 0)
                raise Error("deliberate")
        self.assertEqual(state["replicas"], 1)

    def test_pause_allows_full_termination_grace_and_controller_delay(self):
        for grace, drain_at in ((30, 32), (90, 110)):
            with self.subTest(grace=grace):
                self.clock.value = 0
                kube, state = self.pause_fixture(grace=grace, drain_at=drain_at)
                with runner_module.paused_reconciler(
                    kube, clock=self.clock, sleep=self.clock.sleep
                ):
                    self.assertEqual(state["replicas"], 0)
                    self.assertGreaterEqual(self.clock(), drain_at)
                self.assertEqual(state["replicas"], 1)

    def test_pause_timeout_still_restores_the_original_replica(self):
        kube, state = self.pause_fixture(grace=30, drain_at=61)
        with self.assertRaisesRegex(Error, "reconciler_pause_drain_timeout"):
            with runner_module.paused_reconciler(kube, clock=self.clock, sleep=self.clock.sleep):
                self.fail("A non-drained reconciler must never admit another tenant")
        self.assertEqual(state["replicas"], 1)

    def test_pause_invalid_grace_is_rejected_before_mutation(self):
        for grace in (-1, 301, True, "30", None):
            with self.subTest(grace=grace):
                kube, _ = self.pause_fixture(grace=grace)
                with self.assertRaisesRegex(Error, "invalid_pause_termination_grace"):
                    with runner_module.paused_reconciler(
                        kube, clock=self.clock, sleep=self.clock.sleep
                    ):
                        self.fail("Invalid shutdown metadata must fail before mutation")
                kube.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
