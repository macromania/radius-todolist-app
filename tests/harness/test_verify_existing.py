import copy
import json
from unittest.mock import patch
from uuid import uuid4

import httpx
from test_first_continuation import Error, HarnessRunCase, azure


class InitialExportDiscoveryTests(HarnessRunCase):
    def exports_at(self, releases):
        def client(target):
            if self.clock() < releases.get(target, 0):
                raise Error("endpoint_not_exported")
            return self.clients[target]

        self.apis.client = client

    def test_fresh_run_waits_for_export_then_measures_convergence_separately(self):
        self.tenants.clear()
        self.exports_at({"control:isolated-1": 90, "data:isolated-1": 90})
        request = self.request

        def converging(target, message):
            response = request(target, message)
            if (
                target == "data:isolated-1"
                and message.url.path == "/tenants/isolated-c"
                and self.clock() < 110
            ):
                return httpx.Response(404, json={})
            return response

        with patch.object(self, "request", side_effect=converging):
            self.assertEqual(self.exercise(continuation=False), 0, self.summary)
        initial = next(
            e
            for e in self.runner.record["events"]
            if e["type"] == "data_applied" and e["tenant"] == "isolated-c"
        )
        self.assertEqual(initial["elapsed_seconds"], 20)
        self.assertEqual(self.clock(), 125)
        self.assertEqual(
            [call.args[0] for call in self.runner.wait_initial_exports.call_args_list],
            ["shared", "isolated-1"],
        )
        self.assertEqual(self.runner.onboard.call_count, 3)
        self.pause_context.assert_called_once()
        self.runner.management_outage.assert_called_once()
        self.runner.control_outage.assert_called_once()

    def test_first_continuation_also_waits_for_initial_shared_exports(self):
        self.exports_at({"control:shared": 90, "data:shared": 90})
        self.assertEqual(self.exercise(), 0, self.summary)
        self.assertEqual(self.clock(), 105)
        self.assertEqual(self.runner.wait_initial_exports.call_args_list[0].args, ("shared",))
        self.assertFalse(
            any(
                target == "management"
                and method == "POST"
                and (body or {}).get("tenant_id") == "shared-a"
                for target, method, _, body in self.calls
            )
        )

    def test_actual_convergence_still_fails_after_thirty_seconds_after_export(self):
        self.tenants.clear()
        self.exports_at({"control:isolated-1": 90, "data:isolated-1": 90})
        request = self.request

        def never_applied(target, message):
            response = request(target, message)
            if target == "data:isolated-1" and message.url.path == "/tenants/isolated-c":
                return httpx.Response(404, json={})
            return response

        with patch.object(self, "request", side_effect=never_applied):
            self.assertEqual(self.exercise(continuation=False), 1)
        self.assertEqual(self.summary["error"], "convergence_deadline_exceeded")
        self.assertEqual(self.clock(), 120)
        self.runner.check_updates_and_counters.assert_not_called()
        self.runner.management_outage.assert_not_called()

    def test_pair_export_discovery_has_one_shared_300_second_budget(self):
        self.tenants.clear()
        self.exports_at({"control:isolated-1": 250, "data:isolated-1": 400})
        self.assertEqual(self.exercise(continuation=False), 1)
        self.assertEqual(self.summary["error"], "initial_export_discovery_timeout")
        self.assertEqual(self.clock(), 300)
        self.runner.check_updates_and_counters.assert_not_called()

    def test_discovery_does_not_retry_auth_ownership_or_schema_errors(self):
        for code in (
            "credential_file_permissions",
            "configuration_identity_changed",
            "azure_endpoint_requires_trusted_https",
            "api_transport_failed",
        ):
            with self.subTest(code=code):

                def client(target, code=code):
                    if target == "control:shared":
                        raise Error(code)
                    return self.clients[target]

                self.apis.client = client
                self.assertEqual(self.exercise(), 1)
                self.assertEqual(self.summary["error"], code)
                self.assertEqual(self.clock(), 0)
                self.runner.onboard.assert_not_called()


class VerifyExistingTests(HarnessRunCase):
    def setUp(self):
        super().setUp()
        self.tenants = {
            name: {
                **self.tenant(name, str(uuid4()), self.old_id),
                "provisioning_status": "succeeded",
                "operation_status": "succeeded",
            }
            for name in ("shared-a", "shared-b", "isolated-c")
        }

    def verify(self):
        return self.exercise(continuation=False, mode="verify-existing")

    def assert_no_admissions(self):
        self.runner.scenario.assert_not_called()
        self.runner.onboard.assert_not_called()
        self.pause_context.assert_not_called()
        self.assertFalse(
            any(method == "POST" and path == "/tenants" for _, method, path, _ in self.calls)
        )

    def test_entrypoint_verifies_existing_scope_and_runs_all_remaining_assertions(self):
        previous = self.previous.read_bytes()
        # Current configuration/counters are the baseline, not an imported run's initial values.
        for value in self.tenants.values():
            value.update(version=4, message="current-message", counter=7)
        self.assertEqual(self.verify(), 0, self.summary)
        self.assert_no_admissions()
        self.assertEqual(self.previous.read_bytes(), previous)
        record = json.loads(self.runner.path.read_text())
        self.assertEqual(record["mode"], "verify-existing")
        self.assertEqual(record["scope"], "existing-tenants-only")
        self.assertIs(record["admission_checks_performed"], False)
        self.assertNotIn("continued_first_from", record)
        self.assertEqual(record["source"], self.current_source)
        self.assertFalse(
            {
                "tenant_accepted",
                "first_tenant_continued",
                "data_reconciler_paused",
                "immediate_child_readiness_proved",
            }
            & {e["type"] for e in record["events"]}
        )
        self.runner.verify_existing.assert_called_once()
        self.assertEqual(
            [c.args[0] for c in self.runner.wait_ready.call_args_list], self.runner.names
        )
        self.assertEqual(
            [c.args[0] for c in self.runner.wait_operation.call_args_list],
            [self.tenants[name]["operation_id"] for name in self.runner.names],
        )
        self.assertEqual(
            [c.args[0] for c in self.runner.wait_initial_exports.call_args_list],
            ["shared", "isolated-1"],
        )
        self.assertEqual(
            [c.args[0] for c in self.runner.applied.call_args_list[:3]], self.runner.names
        )
        self.runner.check_shared_pair.assert_called_once()
        self.runner.check_pair_isolation.assert_called_once()
        self.assertEqual(
            [c.args[0] for c in self.runner.workload_evidence.call_args_list],
            ["shared", "isolated-1"],
        )
        self.runner.check_configuration_and_idempotency.assert_called_once()
        self.runner.check_updates_and_counters.assert_called_once()
        self.runner.assert_updates_survived_poll.assert_called_once()
        self.assertEqual(self.runner.management_image.call_count, 2)
        self.assertEqual(self.runner.collect_timelines.call_count, 3)
        self.runner.management_outage.assert_called_once()
        self.runner.control_outage.assert_called_once()
        self.assertTrue(all(v["version"] == 6 for v in self.tenants.values()))
        self.assertEqual([self.tenants[n]["counter"] for n in self.runner.names], [11, 8, 8])
        output = self.state / "summary.json"
        output.write_text(json.dumps(self.summary))
        self.assertEqual(
            azure.runner.verify_evidence(
                output, "verify-existing", self.current_source["commit"], self.started
            ),
            str(self.runner.path.relative_to(self.project)),
        )

    def test_existing_startup_waits_for_ninety_second_export(self):
        def client(target):
            if target == "data:isolated-1" and self.clock() < 90:
                raise Error("state_file_missing")
            return self.clients[target]

        self.apis.client = client
        self.assertEqual(self.verify(), 0, self.summary)
        self.assertEqual(self.clock(), 105)
        self.assert_no_admissions()

    def test_missing_failed_or_interrupted_tenants_and_operations_refuse_mutations(self):
        original = copy.deepcopy(self.tenants)
        for name in self.tenants.copy():
            for field, value in (
                ("missing", None),
                ("provisioning_status", "failed"),
                ("provisioning_status", "interrupted"),
                ("operation_status", "failed"),
                ("operation_status", "interrupted"),
                ("operation_id", None),
                ("tenant_id", "foreign"),
                ("isolation", "wrong"),
            ):
                with self.subTest(tenant=name, field=field, value=value):
                    self.tenants = copy.deepcopy(original)
                    if field == "missing":
                        del self.tenants[name]
                    else:
                        self.tenants[name][field] = value
                    self.calls.clear()
                    self.assertEqual(self.verify(), 1)
                    self.assert_no_admissions()
                    self.assertTrue(all(method == "GET" for _, method, _, _ in self.calls))
                    self.runner.check_updates_and_counters.assert_not_called()
                    self.runner.management_outage.assert_not_called()

    def test_current_shared_assignment_and_dedicated_datastores_and_arm_ids_are_required(self):
        tenants, instances, inventory = [
            copy.deepcopy(value) for value in (self.tenants, self.instances, self.inventory)
        ]
        for section, keys, value, code in (
            ("tenants", ("shared-a", "pair_id"), "isolated-1", "shared_pair_assignment_changed"),
            ("tenants", ("shared-b", "pair_id"), "isolated-1", "shared_pair_assignment_changed"),
            ("tenants", ("shared-b", "data_url"), "different", "shared_urls_changed"),
            ("tenants", ("shared-b", "control_url"), None, "shared_urls_changed"),
            ("tenants", ("isolated-c", "pair_id"), "shared", "isolated_pair_not_dedicated"),
            (
                "inventory",
                ("isolated-1", "data_cluster_id"),
                "shared-data",
                "cluster_ids_not_dedicated",
            ),
            ("inventory", ("isolated-1", "control_cluster_id"), None, "cluster_ids_not_dedicated"),
            (
                "instances",
                ("isolated-1", "control", "postgresql", "host"),
                "shared-pg",
                "isolated_datastore_endpoint_reused",
            ),
            (
                "instances",
                ("isolated-1", "data", "redis", "host"),
                "shared-redis",
                "isolated_datastore_endpoint_reused",
            ),
        ):
            with self.subTest(section=section, keys=keys):
                self.tenants, self.instances, self.inventory = [
                    copy.deepcopy(value) for value in (tenants, instances, inventory)
                ]
                selected = getattr(self, section)
                for key in keys[:-1]:
                    selected = selected[key]
                selected[keys[-1]] = value
                self.calls.clear()
                self.assertEqual(self.verify(), 1)
                self.assertEqual(self.summary["error"], code)
                self.assert_no_admissions()
                self.runner.check_updates_and_counters.assert_not_called()
                self.assertTrue(all(method == "GET" for _, method, _, _ in self.calls))

    def test_five_live_cluster_uids_must_be_distinct(self):
        kube = self.kube
        for duplicate in ("management", "shared-data"):
            with self.subTest(duplicate=duplicate):

                def collision(slot, duplicate=duplicate):
                    result = kube(slot)
                    if slot == "isolated-1-data":
                        result.target.cluster_uid = duplicate
                    return result

                with patch.object(self, "kube", side_effect=collision):
                    self.assertEqual(self.verify(), 1)
                self.assertEqual(self.summary["error"], "actual_clusters_not_dedicated")
                self.runner.check_updates_and_counters.assert_not_called()

    def test_verify_existing_refuses_continuation_before_api_or_mutations(self):
        original = self.previous.read_bytes()
        self.assertEqual(self.exercise(mode="verify-existing"), 1)
        self.assertEqual(self.summary["error"], "continuation_requires_scenario")
        self.assertEqual(self.previous.read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_outages_mode_keeps_original_scope_and_does_not_run_new_verification(self):
        self.assertEqual(self.exercise(continuation=False, mode="outages"), 0, self.summary)
        self.assert_no_admissions()
        self.runner.verify_existing.assert_not_called()
        self.runner.wait_initial_exports.assert_not_called()
        self.runner.check_updates_and_counters.assert_not_called()
        self.runner.management_outage.assert_called_once()
        self.runner.control_outage.assert_called_once()
        self.assertEqual(self.runner.collect_timelines.call_count, 1)
        self.assertNotIn("scope", self.runner.record)

    def test_outages_and_default_client_keep_thirty_second_export_timeout(self):
        def client(target):
            if target != "management":
                raise Error("endpoint_not_exported")
            return self.clients[target]

        self.apis.client = client
        self.assertEqual(self.exercise(continuation=False, mode="outages"), 1)
        self.assertEqual(self.summary["error"], "convergence_deadline_exceeded")
        self.assertEqual(self.clock(), 30)
        self.runner.wait_initial_exports.assert_not_called()
