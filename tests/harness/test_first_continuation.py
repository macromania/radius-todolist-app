import copy
import hashlib
import io
import json
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import test_acceptance as base
import test_azure_runner as azure

module, faults, Error = base.runner_module, base.faults, base.Error


class HarnessRunCase(base.StateCase):
    def setUp(self):
        super().setUp()
        self.project = self.root.resolve()
        self.state = self.project / ".state/azure"
        self.state.mkdir(parents=True)
        self.config_path = self.state / "acceptance.json"
        self.write_config()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for owner in (module, faults, azure.runner):
            self.stack.enter_context(patch.object(owner, "ROOT", self.project))
        self.old_id = "1a" * 16
        self.relative = f".state/azure/evidence/acceptance-{self.old_id}.json"
        self.previous = self.project / self.relative
        self.previous.parent.mkdir(mode=0o700)
        now = datetime.now(UTC)
        committed, started, finished = [(now - timedelta(minutes=n)).isoformat() for n in (4, 3, 2)]
        self.source = {"commit": "a" * 40, "committed_at": committed, "worktree_dirty": False}
        self.current_source = {
            **self.source,
            "commit": "b" * 40,
            "committed_at": (now - timedelta(minutes=1)).isoformat(),
        }
        self.operation = str(uuid4())
        self.prior = {
            "version": 1,
            "run_id": self.old_id,
            "mode": "all",
            "outcome": "failed",
            "error": "operator_interrupted",
            "project": "radplanes",
            "environment": "azure",
            "started_at": started,
            "finished_at": finished,
            "source": self.source,
            "events": [
                {"type": "acceptance_started", "at": started},
                *[
                    {
                        "type": "workload_image",
                        "at": started,
                        "slot": "management",
                        "component": component,
                        "pod_uid": str(uuid4()),
                        "configured_image": self.values["images"][role],
                        "running_image_id": self.values["images"][role],
                        "source_hashes": {"source.py": "c" * 64},
                    }
                    for component, role in (
                        ("management-api", "api"),
                        ("provisioner", "provisioner"),
                    )
                ],
                {
                    "type": "tenant_accepted",
                    "at": started,
                    "tenant": "shared-a",
                    "operation_id": self.operation,
                    "http_status": 202,
                    "busy_verified": True,
                },
            ],
        }
        self.write_prior()
        self.tenants = {"shared-a": self.tenant("shared-a", self.operation, self.old_id)}
        self.calls = []
        self.probe_calls = []
        self.pods = {}
        self.identity_error = None
        self.paused = False
        self.clients = {
            target: module.Client(
                "https://test.centralus.cloudapp.azure.com",
                target + "-synthetic-key",
                transport=httpx.MockTransport(
                    lambda request, target=target: self.request(target, request)
                ),
            )
            for target in (
                "management",
                "control:shared",
                "data:shared",
                "control:isolated-1",
                "data:isolated-1",
            )
        }
        for client in self.clients.values():
            self.addCleanup(client.close)
        self.apis = SimpleNamespace(
            clients=self.clients, client=self.clients.__getitem__, close=Mock()
        )
        self.instances = {
            pair: {
                "control": {"cluster_uid": pair + "-control", "postgresql": {"host": pair + "-pg"}},
                "data": {
                    "cluster_uid": pair + "-data",
                    "redis": {"host": pair + "-redis", "tls": True},
                },
            }
            for pair in ("shared", "isolated-1")
        }
        self.inventory = {
            pair: {
                "control_cluster_id": pair + "-control",
                "data_cluster_id": pair + "-data",
            }
            for pair in self.instances
        }

    def kube(self, slot):
        target = SimpleNamespace(
            slot=slot,
            cluster_uid=slot,
            component=lambda name: {"container": name},
            namespace=f"radplanes-{slot}-{slot.rsplit('-', 1)[-1]}",
        )
        kube = Mock(target=target)

        def pod(component):
            role = "provisioner" if component == "provisioner" else "api"
            image = self.values["images"][role]
            uid = self.pods.setdefault((slot, component), str(uuid4()))
            return {
                "metadata": {"uid": uid},
                "spec": {
                    "containers": [{"name": component, "image": image}],
                    "serviceAccountName": "data-api-runtime"
                    if component == "data-api"
                    else component,
                },
                "status": {"containerStatuses": [{"name": component, "imageID": image}]},
            }

        def probe(component, script, *_args):
            self.probe_calls.append((slot, component, script))
            if script == module.SOURCE_PROBE:
                return {"files": {"source.py": "c" * 64}, "parent_dsn_present": False}
            if script == module.DATA_API_PERMISSIONS_PROBE:
                return {
                    "permissions": json.loads(_args[-1]),
                    "parent_secret_get_status": 403,
                    "secret_list_status": 403,
                }
            self.assertEqual(script, module.IDENTITY_PROBE)
            if self.identity_error:
                raise Error(self.identity_error)
            pair, role = slot.rsplit("-", 1)
            return copy.deepcopy(self.instances[pair][role])

        kube.pod.side_effect = pod
        kube.exec_json.side_effect = probe
        return kube

    def write_prior(self):
        self.previous.write_text(json.dumps(self.prior, indent=2) + "\n")
        self.previous.chmod(0o600)

    def tenant(self, name, operation, run_id):
        pair = "isolated-1" if name == "isolated-c" else "shared"
        return {
            "tenant_id": name,
            "operation_id": operation,
            "onboarding_id": str(uuid4()),
            "isolation": "isolated" if pair != "shared" else "shared",
            "pair_id": pair,
            "provisioning_status": "running",
            "onboarding_status": "ready",
            "control_record": {
                "status": "created",
                "observed_revision": 1,
                "reported_at": self.prior["started_at"],
            },
            "control_url": pair + "-control",
            "data_url": pair + "-data",
            "version": 1,
            "message": f"acceptance-{run_id[:8]}-{name}",
            "counter": 0,
        }

    def request(self, target, request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((target, method, path, body))
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.headers.get("X-Demo-Key") != self.clients[target].key:
            return httpx.Response(401, json={"detail": "unauthorized"})
        if path == "/api/container-info":
            return httpx.Response(404, json={})
        if path == "/tenants" and method == "POST":
            name = body["tenant_id"]
            if name.startswith("busy-"):
                return httpx.Response(
                    503, json={"detail": "provisioner_busy"}, headers={"Retry-After": "5"}
                )
            if name in self.tenants:
                return httpx.Response(
                    409,
                    json={"status_url": f"/tenants/{name}"},
                    headers={"Location": f"/tenants/{name}"},
                )
            value = self.tenant(name, str(uuid4()), self.runner.run_id)
            value["message"] = body["initial_message"]
            self.tenants[name] = value
            return httpx.Response(
                202, json={"operation_id": value["operation_id"], "status_url": f"/tenants/{name}"}
            )
        if path.startswith("/operations/"):
            value = next(v for v in self.tenants.values() if path.endswith(v["operation_id"]))
            return httpx.Response(
                200,
                json={
                    "status": value.get(
                        "operation_status", "succeeded" if value.get("observed") else "running"
                    )
                },
            )
        name = path.split("/")[2]
        if name not in self.tenants:
            return httpx.Response(404, json={})
        value = self.tenants[name]
        pending = self.paused and name == "shared-b"
        if target == "management":
            value["observed"] = True
            return httpx.Response(200, json=value)
        if target.startswith("control:"):
            if method == "PUT":
                value["version"] += 1
                value["message"] = body["message"]
            return httpx.Response(
                200,
                json={
                    "onboarding_id": value["onboarding_id"],
                    "desired": {"version": value["version"], "message": value["message"]},
                    "data_config": {
                        "status": "pending" if pending else "applied",
                        "last_applied_version": None if pending else value["version"],
                        "reported_at": self.prior["started_at"],
                    },
                },
            )
        if pending:
            return httpx.Response(404, json={})
        if method == "POST":
            value["counter"] += 1
        return httpx.Response(200, json={**value, "applied_version": value["version"]})

    @contextmanager
    def pause(self, *_args, **_kwargs):
        self.paused = True
        try:
            yield {"scoped": True}
        finally:
            self.paused = False

    def exercise(self, *, continuation=True, mode="all", path=None):
        runner_class = module.Runner

        def build(*args, **kwargs):
            self.runner = value = runner_class(
                *args, **kwargs, apis=self.apis, clock=self.clock, sleep=self.clock.sleep
            )
            value.configuration.target = Mock(
                return_value=SimpleNamespace(cluster_uid="management")
            )
            value.kube = Mock(side_effect=self.kube)
            value.management_image = Mock(wraps=value.management_image)
            value.pair_inventory = Mock(return_value=self.inventory)
            value.workload_evidence = Mock(wraps=value.workload_evidence)
            value.check_updates_and_counters = Mock(wraps=value.check_updates_and_counters)
            value.collect_timelines = Mock(return_value={"unchanged": True})
            value.control_poll_observed = Mock(return_value=True)
            value.assert_updates_survived_poll = Mock(wraps=value.assert_updates_survived_poll)
            value.management_outage, value.control_outage = Mock(), Mock()
            for name in (
                "scenario",
                "verify_existing",
                "onboard",
                "wait_ready",
                "wait_operation",
                "wait_initial_exports",
                "applied",
                "check_shared_pair",
                "check_pair_isolation",
                "check_configuration_and_idempotency",
            ):
                setattr(value, name, Mock(wraps=getattr(value, name)))
            return value

        def git(argv):
            if argv[:3] == ["git", "show", "-s"]:
                faults.require(argv[-1] == self.source["commit"], "unknown_source_commit")
                return self.source["committed_at"]
            self.assertEqual(argv[:2], ["git", "status"])
            return ""

        argv = ["--config", ".state/azure/acceptance.json", "--mode", mode, "--execute"]
        if continuation:
            argv += ["--continue-first-from", path if path is not None else self.relative]
        self.started = datetime.now(UTC).timestamp()
        with (
            patch.object(module, "Runner", side_effect=build),
            patch.object(module, "paused_reconciler", side_effect=self.pause) as pause,
            patch.object(module, "source_hashes", return_value={"source.py": "c" * 64}),
            patch.object(faults, "command", side_effect=git),
            patch.object(faults, "source_metadata", return_value=self.current_source),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.pause_context = pause
            code = module.main(argv)
        self.summary = json.loads(output.getvalue())
        return code


class FirstContinuationTests(HarnessRunCase):
    def assert_completed_scenario(self):
        original = self.previous.read_bytes()
        self.assertEqual(self.exercise(), 0, self.summary)
        self.assertEqual(self.previous.read_bytes(), original)
        self.assertEqual(self.previous.stat().st_mode & 0o777, 0o600)
        record = json.loads(self.runner.path.read_text())
        predecessor = record["continued_first_from"]
        self.assertEqual(predecessor["path"], self.relative)
        self.assertEqual(predecessor["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(predecessor["source"], self.source)
        self.assertEqual(predecessor["run_id"], self.old_id)
        self.assertEqual(predecessor["admission"], self.prior["events"][3])
        self.assertEqual(predecessor["started_at"], self.prior["started_at"])
        self.assertEqual(predecessor["finished_at"], self.prior["finished_at"])
        self.assertEqual(record["source"], self.current_source)
        self.assertNotEqual(record["run_id"], self.old_id)
        accepted = [e for e in record["events"] if e["type"] == "tenant_accepted"]
        self.assertEqual([e["tenant"] for e in accepted], ["shared-b", "isolated-c"])
        mutations = [
            body["tenant_id"]
            for target, method, path, body in self.calls
            if target == "management" and method == "POST" and path == "/tenants"
        ]
        self.assertEqual(mutations, ["shared-b"] * 3 + ["isolated-c"] * 3)
        self.assertEqual(
            self.calls[:2],
            [
                ("management", "GET", "/tenants/shared-b", None),
                ("management", "GET", "/tenants/isolated-c", None),
            ],
        )
        self.assertIn(("management", "GET", f"/operations/{self.operation}", None), self.calls)
        self.assertEqual(self.runner.management_image.call_count, 2)
        self.assertEqual(self.runner.pair_inventory.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in self.runner.workload_evidence.call_args_list],
            ["shared", "shared", "isolated-1"],
        )
        self.assertEqual(
            self.probe_calls[2:9],
            [
                ("shared-control", "control-api", module.SOURCE_PROBE),
                ("shared-control", "control-reconciler", module.SOURCE_PROBE),
                ("shared-control", "control-api", module.IDENTITY_PROBE),
                ("shared-data", "data-api", module.SOURCE_PROBE),
                ("shared-data", "data-api", module.DATA_API_PERMISSIONS_PROBE),
                ("shared-data", "data-reconciler", module.SOURCE_PROBE),
                ("shared-data", "data-api", module.IDENTITY_PROBE),
            ],
        )
        self.runner.check_updates_and_counters.assert_called_once()
        self.runner.assert_updates_survived_poll.assert_called_once()
        self.assertEqual(self.runner.collect_timelines.call_count, 3)
        self.runner.management_outage.assert_called_once()
        self.runner.control_outage.assert_called_once()
        self.assertEqual([self.tenants[name]["counter"] for name in self.runner.names], [4, 1, 1])
        self.assertTrue(all(v["version"] == 3 for v in self.tenants.values()))
        self.assertIn("immediate_child_readiness_proved", [e["type"] for e in record["events"]])
        output = self.state / "summary.json"
        output.write_text(json.dumps(self.summary))
        self.assertEqual(
            azure.runner.verify_evidence(
                output, "all", self.current_source["commit"], self.started
            ),
            str(self.runner.path.relative_to(self.project)),
        )

    def test_matching_all_run_continues_only_first_and_runs_remaining_scenario(self):
        self.assert_completed_scenario()

    def test_scenario_mode_continues_without_claiming_outages(self):
        self.prior["mode"] = "scenario"
        self.write_prior()
        self.assertEqual(self.exercise(mode="scenario"), 0, self.summary)
        self.runner.management_outage.assert_not_called()
        self.runner.control_outage.assert_not_called()

    def test_reported_predecessor_metadata_and_original_message_match_contract(self):
        self.old_id = "c680a8557f3b4722b01f7815ad1b644b"
        self.operation = "036be342-eb25-459c-97a9-1b6af2229a15"
        self.relative = f".state/azure/evidence/acceptance-{self.old_id}.json"
        self.previous = self.project / self.relative
        self.source = {
            "commit": "788cbda7684c7466dc945e2b3fd130d94862dc13",
            "committed_at": "2026-09-09T21:49:09+04:00",
            "worktree_dirty": False,
        }
        self.prior.update(
            run_id=self.old_id,
            source=self.source,
            started_at="2026-09-09T17:52:35.874258+00:00",
            finished_at="2026-09-09T17:58:15.504750+00:00",
        )
        # The supplied summary does not include raw workload probes or their timestamps.
        for event in self.prior["events"][:-1]:
            event["at"] = self.prior["started_at"]
        self.prior["events"][-1].update(
            at="2026-09-09T17:53:02.951206+00:00", operation_id=self.operation
        )
        self.write_prior()
        self.tenants["shared-a"] = self.tenant("shared-a", self.operation, self.old_id)
        self.assertEqual(self.tenants["shared-a"]["message"], "acceptance-c680a855-shared-a")
        self.assertEqual(self.exercise(), 0, self.summary)
        prior = self.runner.record["continued_first_from"]
        self.assertEqual(prior["source"], self.source)
        self.assertEqual(prior["admission"], self.prior["events"][-1])
        self.assertIn(("management", "GET", f"/operations/{self.operation}", None), self.calls)

    def read_only_prior(self):
        self.old_id = "997de287a5134b7fb6b3adb95e850eb6"
        self.operation = "43bbaeb1-8250-49db-8d70-15ab73a9da73"
        self.relative = f".state/azure/evidence/acceptance-{self.old_id}.json"
        self.previous = self.project / self.relative
        self.source = {
            "commit": "058d8784f732c4347a1d66bfbfe289ab6393806b",
            "committed_at": "2026-09-10T01:33:24+04:00",
            "worktree_dirty": False,
        }
        self.prior.update(
            run_id=self.old_id,
            source=self.source,
            error="azure_redis_tls_not_enabled",
            started_at="2026-09-09T21:45:26.048583+00:00",
            finished_at="2026-09-09T22:33:19.764704+00:00",
        )
        # Probe payloads and individual event times are synthetic, not copied PVC bytes.
        events = self.prior["events"]
        for event in events:
            event["at"] = self.prior["started_at"]
        events[3]["operation_id"] = self.operation
        at = self.prior["finished_at"]
        events += [
            {
                "type": "management_ready",
                "at": at,
                "tenant": "shared-a",
                "pair_id": "shared",
                "operation_id": self.operation,
                "elapsed_seconds": 1,
                "reported_at": at,
            },
            {
                "type": "data_applied",
                "at": at,
                "tenant": "shared-a",
                "version": 1,
                "elapsed_seconds": 1,
                "recovery_elapsed_seconds": None,
                "observed_monotonic": 1,
            },
            *[
                {
                    **events[1],
                    "at": at,
                    "slot": slot,
                    "component": component,
                    "pod_uid": str(uuid4()),
                }
                for slot, component in (
                    ("shared-control", "control-api"),
                    ("shared-control", "control-reconciler"),
                    ("shared-data", "data-api"),
                    ("shared-data", "data-reconciler"),
                )
            ],
        ]
        self.write_prior()
        self.tenants["shared-a"] = {
            **self.tenant("shared-a", self.operation, self.old_id),
            "provisioning_status": "succeeded",
            "operation_status": "succeeded",
        }

    def test_reported_read_only_failure_reruns_first_verification_and_all_remaining_steps(self):
        self.read_only_prior()
        self.assertEqual(self.tenants["shared-a"]["message"], "acceptance-997de287-shared-a")
        self.assert_completed_scenario()

    def test_read_only_progress_rejects_changed_order_components_tenant_version_or_ids(self):
        self.read_only_prior()
        original = copy.deepcopy(self.prior["events"])
        for index, field, value in (
            (4, "tenant", "shared-b"),
            (4, "pair_id", "isolated-1"),
            (4, "operation_id", str(uuid4())),
            (4, "operation_id", None),
            (5, "tenant", "isolated-c"),
            (5, "version", 2),
            (5, "version", True),
            (6, "slot", "isolated-1-control"),
            (6, "component", "control-reconciler"),
            (7, "component", "control-api"),
            (8, "component", "data-reconciler"),
            (9, "slot", "shared-control"),
            (9, "component", "data-api"),
            (6, "pod_uid", None),
            (9, "pod_uid", "not-a-uuid"),
        ):
            with self.subTest(index=index, field=field, value=value):
                self.prior["events"] = copy.deepcopy(original)
                self.prior["events"][index][field] = value
                self.write_prior()
                self.assertEqual(self.exercise(), 1)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.probe_calls, [])

    def test_only_complete_read_only_boundary_is_allowed_and_continuations_cannot_chain(self):
        self.read_only_prior()
        original = copy.deepcopy(self.prior["events"])
        invalid = [original[:length] for length in range(5, 10)] + [
            [*original, {"type": name, "at": self.prior["finished_at"]}]
            for name in (
                "data_reconciler_paused",
                "tenant_accepted",
                "data_applied",
                "configuration_counter_and_auth_checks",
                "data_continuity",
                "management_link_recovered",
                "control_link_recovered_latest_only",
            )
        ]
        invalid.append([*original[:4], original[5], original[4], *original[6:]])
        for events in invalid:
            with self.subTest(types=[event["type"] for event in events]):
                self.prior["events"] = events
                self.write_prior()
                self.assertEqual(self.exercise(), 1)
                self.assertEqual(self.calls, [])
        self.prior["events"] = original
        self.prior["continued_first_from"] = {"path": "an-earlier-run"}
        self.write_prior()
        self.assertEqual(self.exercise(), 1)
        self.assertEqual(self.calls, [])

    def test_read_only_evidence_never_skips_current_first_state_or_identity_failures(self):
        self.read_only_prior()
        original = copy.deepcopy(self.tenants["shared-a"])
        for field, changed in (
            ("version", 2),
            ("message", "changed-first"),
            ("operation_id", str(uuid4())),
            ("provisioning_status", "failed"),
            ("operation_status", "interrupted"),
            ("tls", False),
            ("identity_error", "current_identity_probe_failed"),
        ):
            with self.subTest(field=field):
                self.tenants["shared-a"] = copy.deepcopy(original)
                self.instances["shared"]["data"]["redis"]["tls"] = True
                self.identity_error = None
                if field == "tls":
                    self.instances["shared"]["data"]["redis"]["tls"] = changed
                elif field == "identity_error":
                    self.identity_error = changed
                else:
                    self.tenants["shared-a"][field] = changed
                self.calls.clear()
                self.assertEqual(self.exercise(), 1)
                self.assertFalse(any(method != "GET" for _, method, _, _ in self.calls))
                self.runner.check_updates_and_counters.assert_not_called()
                self.runner.management_outage.assert_not_called()
                self.assertNotIn(
                    "data_reconciler_paused", [e["type"] for e in self.runner.record["events"]]
                )
                if field == "tls":
                    self.assertEqual(self.summary["error"], "azure_redis_tls_not_enabled")

    def test_read_only_evidence_still_refuses_existing_remaining_tenants(self):
        self.read_only_prior()
        for name in ("shared-b", "isolated-c"):
            with self.subTest(tenant=name):
                self.tenants[name] = self.tenant(name, str(uuid4()), self.old_id)
                self.calls.clear()
                self.assertEqual(self.exercise(), 1)
                self.assertFalse(any(method != "GET" for _, method, _, _ in self.calls))
                del self.tenants[name]

    def test_existing_first_waits_for_ready_and_operation_completion_without_replay(self):
        request = self.request
        reads, operations = 0, 0

        def pending(target, message):
            nonlocal reads, operations
            response = request(target, message)
            if target == "management" and message.url.path == "/tenants/shared-a":
                reads += 1
                if reads <= 2:
                    return httpx.Response(
                        200,
                        json={
                            **response.json(),
                            "onboarding_status": "pending",
                            "control_record": {"status": "pending"},
                        },
                    )
            if message.url.path == f"/operations/{self.operation}":
                operations += 1
                if operations == 1:
                    return httpx.Response(200, json={"status": "running"})
            return response

        with patch.object(self, "request", side_effect=pending):
            self.assertEqual(self.exercise(), 0, self.summary)
        self.assertGreaterEqual(reads, 3)
        self.assertEqual(operations, 2)
        self.assertFalse(
            any(
                target == "management"
                and method == "POST"
                and (body or {}).get("tenant_id") == "shared-a"
                for target, method, _, body in self.calls
            )
        )

    def test_colliding_output_never_overwrites_original_failed_evidence(self):
        original = self.previous.read_bytes()
        with patch.object(module, "uuid4", return_value=SimpleNamespace(hex=self.old_id)):
            self.assertEqual(self.exercise(), 1)
        self.assertEqual(self.previous.read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_default_still_demands_fresh_first_and_performs_original_admission(self):
        self.tenants.clear()
        self.assertEqual(self.exercise(continuation=False), 0, self.summary)
        self.assertNotIn("continued_first_from", self.runner.record)
        first = next(e for e in self.runner.record["events"] if e["type"] == "tenant_accepted")
        self.assertEqual(first["tenant"], "shared-a")
        self.assertEqual(first["http_status"], 202)
        self.assertIs(first["busy_verified"], True)
        self.assertTrue(
            any(
                (body or {}).get("tenant_id", "").startswith("busy-")
                for _, method, _, body in self.calls
                if method == "POST"
            )
        )

    def test_default_does_not_implicitly_continue_an_existing_first(self):
        self.assertEqual(self.exercise(continuation=False), 1)
        self.assertFalse(any(method != "GET" for _, method, _, _ in self.calls))

    def test_tampered_or_foreign_prior_fails_before_any_api_call(self):
        cases = [
            (("version",), 2),
            (("version",), True),
            (("project",), "foreign"),
            (("environment",), "local"),
            (("mode",), "scenario"),
            (("outcome",), "passed"),
            (("outcome",), "running"),
            (("run_id",), "2b" * 16),
            (("source", "commit"), "bad"),
            (("source", "commit"), "c" * 40),
            (("source", "worktree_dirty"), True),
            (("source", "committed_at"), None),
            (("source", "committed_at"), "2000-01-01T00:00:00+00:00"),
            (("started_at",), "2000-01-01T00:00:00+00:00"),
            (("finished_at",), "2999-01-01T00:00:00+00:00"),
            (("events", 0, "at"), "2000-01-01T00:00:00"),
            (("events", 2, "at"), "2000-01-01T00:00:00+00:00"),
            (("events", 1, "slot"), "foreign"),
            (("events", 3, "tenant"), "shared-b"),
            (("events", 3, "operation_id"), "invalid"),
            (("events", 3, "http_status"), 409),
            (("events", 3, "busy_verified"), False),
            (("events", 3, "unexpected"), "reject"),
        ]
        original = copy.deepcopy(self.prior)
        for keys, change in cases:
            with self.subTest(keys=keys, change=change):
                self.prior = copy.deepcopy(original)
                value = self.prior
                for key in keys[:-1]:
                    value = value[key]
                value[keys[-1]] = change
                self.write_prior()
                before = self.previous.read_bytes()
                self.assertEqual(self.exercise(), 1)
                self.assertEqual(self.previous.read_bytes(), before)
                self.assertEqual(self.calls, [])

    def test_progress_missing_admission_and_unknown_admission_shape_are_refused(self):
        original = copy.deepcopy(self.prior["events"])
        events = [
            original[:-1],
            [],
            [*original, original[-1]],
            [*original[:-1], {k: v for k, v in original[-1].items() if k != "busy_verified"}],
        ] + [
            [*original, {"type": name, "at": self.prior["started_at"]}]
            for name in (
                "management_ready",
                "data_applied",
                "data_reconciler_paused",
                "immediate_child_readiness_proved",
                "pair_isolation",
                "configuration_counter_and_auth_checks",
                "complete_paginated_timelines",
                "data_continuity",
                "management_link_recovered",
                "control_link_recovered_latest_only",
                "first_tenant_continued",
            )
        ]
        for value in events:
            with self.subTest(events=value):
                self.prior["events"] = value
                self.write_prior()
                self.assertEqual(self.exercise(), 1)
                self.assertEqual(self.calls, [])

    def test_paths_permissions_size_and_outages_fail_closed(self):
        for path in (
            "/" + self.relative,
            "../" + self.relative,
            "./" + self.relative,
            self.relative.replace("evidence/", "evidence/../evidence/"),
            self.relative.replace(self.old_id, "x" * 32),
            self.relative.replace(self.old_id, "a" * 33),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.exercise(path=path), 1)
        self.assertEqual(self.exercise(mode="outages"), 1)
        self.previous.chmod(0o644)
        self.assertEqual(self.exercise(), 1)
        self.previous.chmod(0o600)
        self.previous.write_text(
            json.dumps(self.prior).replace(
                '"outcome": "failed"', '"outcome": "passed", "outcome": "failed"'
            )
        )
        self.assertEqual(self.exercise(), 1)
        self.previous.write_bytes(b" " * 2_000_001)
        self.assertEqual(self.exercise(), 1)
        self.previous.write_bytes(b"{invalid")
        self.assertEqual(self.exercise(), 1)
        self.previous.unlink()
        self.assertEqual(self.exercise(), 1)
        outside = self.project / "foreign.json"
        outside.write_text(json.dumps(self.prior))
        outside.chmod(0o600)
        self.previous.symlink_to(outside)
        self.assertEqual(self.exercise(), 1)
        self.assertEqual(self.calls, [])

    def test_wrong_current_operation_pair_status_or_changed_config_never_admits_second(self):
        original = copy.deepcopy(self.tenants["shared-a"])
        for key, value in (
            ("operation_id", str(uuid4())),
            ("tenant_id", "foreign"),
            ("pair_id", "isolated-1"),
            ("isolation", "isolated"),
            ("provisioning_status", "failed"),
            ("provisioning_status", "interrupted"),
            ("operation_status", "failed"),
            ("operation_status", "interrupted"),
            ("version", 2),
            ("message", "changed-after-admission"),
        ):
            with self.subTest(key=key, value=value):
                self.tenants["shared-a"] = {**original, key: value}
                self.calls.clear()
                self.assertEqual(self.exercise(), 1)
                self.assertFalse(any(method != "GET" for _, method, _, _ in self.calls))
                self.runner.check_updates_and_counters.assert_not_called()
                self.runner.management_outage.assert_not_called()

    def test_existing_remaining_or_missing_first_is_not_skipped(self):
        original = copy.deepcopy(self.tenants)
        for name in ("shared-b", "isolated-c", "missing-first"):
            with self.subTest(name=name):
                self.tenants = copy.deepcopy(original)
                if name == "missing-first":
                    self.tenants.clear()
                else:
                    self.tenants[name] = self.tenant(name, str(uuid4()), self.old_id)
                self.calls.clear()
                self.assertEqual(self.exercise(), 1)
                self.assertFalse(any(method != "GET" for _, method, _, _ in self.calls))
