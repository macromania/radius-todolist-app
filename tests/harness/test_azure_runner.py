import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("azure_harness", ROOT / "harness/run-azure.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
COMMIT = "a" * 40
SECRET = "synthetic-federation-token-never-log"


def configuration():
    prefix = f"/subscriptions/{runner.SUBSCRIPTION}/resourceGroups/"
    client = "11111111-1111-4111-8111-111111111111"
    allocations = {}
    for index, slot in enumerate(
        ("management", "shared-control", "shared-data", "isolated-1-control", "isolated-1-data")
    ):
        allocation = {
            "slot": slot,
            "clusterName": f"aks-radplanes-{slot}",
            "clusterResourceGroup": f"rg-radplanes-{slot}-cluster",
            "appResourceGroup": f"rg-radplanes-{slot}-app",
            "certificateName": f"gateway-{slot}",
            "acmeStateSecretName": f"acme-{slot}",
            "certificateIssuerSubject": "system:serviceaccount:radplanes-system:certificate-issuer",
            "gatewaySubnetCidr": f"10.64.{16 + index}.0/24",
            "apiPrivateIp": f"10.64.{index}.240",
            "challengePrivateIp": f"10.64.{index}.241",
            "identities": {"certificateIssuer": {"clientId": client, "id": prefix + "identity"}},
        }
        for field in ("clusterResourceGroup", "appResourceGroup"):
            allocation[field + "Id"] = prefix + allocation[field]
        for field in (
            "nodeSubnetId",
            "gatewaySubnetId",
            "privateEndpointSubnetId",
            "postgresqlSubnetId",
        ):
            allocation[field] = prefix + "rg-radplanes-platform/subnets/" + field
        allocations[slot] = allocation
    return {
        "version": 1,
        "foundation": {
            "projectName": "radplanes",
            "location": "centralus",
            "subscriptionId": runner.SUBSCRIPTION,
            "tenantId": client,
            "registryName": "demoregistry",
            "registryLoginServer": "demoregistry.azurecr.io",
            "egressIp": "5.6.7.8",
            "authorizedIpRanges": ["5.6.7.8/32"],
        },
        "allocations": allocations,
        "coordinatorIdentity": {"clientId": client},
        "managementCluster": {
            "name": runner.CLUSTER,
            "resourceGroup": runner.GROUP,
            "id": prefix
            + runner.GROUP
            + "/providers/Microsoft.ContainerService/managedClusters/"
            + runner.CLUSTER,
        },
        "recipes": {
            name: {
                "reference": f"demoregistry.azurecr.io/recipes/{name}:v1",
                "digest": "sha256:" + "b" * 64,
            }
            for name in ("cluster", "postgresql", "gateway", "redis")
        },
        "images": {
            role: f"demoregistry.azurecr.io/{role}@sha256:" + "b" * 64
            for role in ("api", "provisioner")
        },
    }


class AzureRunnerTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ".state/check" / ("azure-runner-" + uuid4().hex)
        self.state = self.root / ".state/azure"
        self.state.mkdir(parents=True)
        self.stack = ExitStack()
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(runner, "ROOT", self.root))
        self.stack.enter_context(patch.object(runner, "WORKSPACE", self.root))
        self.run = self.stack.enter_context(
            patch.object(
                runner.subprocess, "run", side_effect=AssertionError("external command forbidden")
            )
        )
        self.popen = self.stack.enter_context(
            patch.object(
                runner.subprocess, "Popen", side_effect=AssertionError("external process forbidden")
            )
        )
        self.config = configuration()
        self.config_path = self.state / "provisioning.json"
        self.config_path.write_text(json.dumps(self.config))
        self.namespace = self.root / "namespace"
        self.namespace.write_text(runner.NAMESPACE)
        self.token = self.root / "tokens/token"
        self.token.parent.mkdir()
        self.token.write_text(SECRET)
        self.stack.enter_context(patch.object(runner, "NAMESPACE_FILE", self.namespace))
        self.stack.enter_context(patch.object(runner, "TOKEN_ROOT", self.token.parent))
        self.lock = os.open(self.state / "harness.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, self.lock)
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "CONFIRM_AZURE": "yes",
                    "AZURE_CLIENT_ID": self.config["coordinatorIdentity"]["clientId"],
                    "AZURE_TENANT_ID": self.config["foundation"]["tenantId"],
                    "AZURE_FEDERATED_TOKEN_FILE": str(self.token),
                    "HARNESS_LOCK_FD": str(self.lock),
                    "EXPECTED_SOURCE_COMMIT": COMMIT,
                },
            )
        )

    def test_real_operator_validation_and_fixed_project_allocation_guards(self):
        self.assertEqual(
            runner.configuration(Path(".state/azure/provisioning.json"))["images"],
            self.config["images"],
        )
        cases = [
            ("foundation", "projectName", "other"),
            ("foundation", "subscriptionId", "22222222-2222-4222-8222-222222222222"),
            ("managementCluster", "name", "foreign"),
            ("managementCluster", "id", "foreign"),
            ("images", "provisioner", "demoregistry.azurecr.io/provisioner:latest"),
        ]
        for section, key, value in cases:
            with self.subTest(section=section, key=key):
                config = configuration()
                config[section][key] = value
                self.config_path.write_text(json.dumps(config))
                with self.assertRaises((runner.HarnessError, ValueError)):
                    runner.configuration(self.config_path)
        config = configuration()
        config["allocations"]["shared-data"]["clusterName"] = "foreign"
        self.config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(runner.HarnessError, "allocation_mismatch"):
            runner.configuration(self.config_path)
        self.run.assert_not_called()

    def test_state_paths_do_not_trust_cwd_or_symlinks(self):
        self.assertEqual(
            runner.state_path(Path(".state/azure/provisioning.json")), self.config_path
        )
        for path in (self.root / "foreign.json", Path("../elsewhere")):
            with self.assertRaises(runner.HarnessError):
                runner.state_path(path)
        link = self.state / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(runner.HarnessError):
            runner.state_path(link / "config")

    def test_source_guard_includes_untracked_exercised_code_and_real_git_root(self):
        for dirty, git_root in (("?? harness/run-azure.py", str(self.root)), ("", "/foreign")):
            calls = []

            def git(argv, calls=calls, git_root=git_root, dirty=dirty):
                calls.append(argv)
                if "--show-toplevel" in argv:
                    return git_root
                return dirty if "status" in argv else COMMIT

            with patch.object(runner, "command", side_effect=git):
                with self.assertRaises(runner.HarnessError):
                    runner.source_commit()
            if dirty:
                status = next(a for a in calls if "status" in a)
                self.assertIn("--untracked-files=all", status)
                for path in ("harness", "src", "operations", "infra", "images", "sql"):
                    self.assertIn(path, status)

    def test_clean_committed_source_and_tracked_state_refusal(self):
        def git(argv):
            if "--show-toplevel" in argv:
                return str(self.root)
            if argv[1:] == ["rev-parse", "HEAD"]:
                return COMMIT
            return ".state/azure/key" if "ls-tree" in argv and bad_state else ""

        with patch.object(runner, "command", side_effect=git):
            bad_state = False
            self.assertEqual(runner.source_commit(), COMMIT)
            bad_state = True
            with self.assertRaisesRegex(runner.HarnessError, "tracked_state_refused"):
                runner.source_commit()

    def test_job_has_only_private_harness_volume_and_existing_identity(self):
        pvc, cm, job = runner.resources(
            self.config, "demo-acceptance-test", b"bundle", COMMIT, "all"
        )
        self.assertEqual(pvc["metadata"]["name"], "harness-state")
        self.assertEqual(pvc["spec"]["storageClassName"], "radplanes-provisioner")
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "8Gi")
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "provisioner")
        self.assertEqual(pod["securityContext"]["runAsUser"], 10001)
        self.assertEqual(pod["securityContext"]["fsGroup"], 10001)
        self.assertEqual(pod["securityContext"]["fsGroupChangePolicy"], "OnRootMismatch")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 10800)
        self.assertEqual(
            job["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"], "true"
        )
        mounts = pod["containers"][0]["volumeMounts"]
        self.assertEqual(
            {m["mountPath"] for m in mounts}, {"/bundle", "/workspace", "/workspace/.state"}
        )
        self.assertNotIn("ports", pod["containers"][0])
        self.assertEqual(pod["containers"][0]["image"], self.config["images"]["provisioner"])
        self.assertEqual(
            pod["containers"][0]["command"][-3:],
            [COMMIT, hashlib.sha256(b"bundle").hexdigest(), "all"],
        )
        self.assertEqual(
            set(cm["data"]), {"bootstrap.py", "provisioning.json", "bootstrap.outputs.json"}
        )
        self.assertEqual(base64.b64decode(cm["binaryData"]["source.bundle"]), b"bundle")
        encoded = json.dumps([pvc, cm, job])
        for forbidden in ("operator-state", "provisioner-state", SECRET, "credentials.json"):
            self.assertNotIn(forbidden, encoded)
        with self.assertRaisesRegex(runner.HarnessError, "source_configmap_too_large"):
            runner.resources(self.config, "demo-acceptance-test", b"x" * 700_000, COMMIT, "all")

    def launch(self, *, inspected=True, jobs=None):
        (self.state / "images.json").write_text(
            json.dumps(
                {
                    **self.config["images"],
                    "content_verified": True,
                }
            )
        )

        def bundle(argv):
            self.assertEqual(argv[:3], ["git", "bundle", "create"])
            self.assertEqual(argv[-1], "HEAD")
            Path(argv[3]).write_bytes(b"actual-head-bundle")
            return ""

        with (
            patch.object(runner, "source_commit", return_value=COMMIT),
            patch.object(runner, "command", side_effect=bundle) as git,
            patch.object(
                runner,
                "invoke",
                side_effect=[
                    {"logs": json.dumps({"items": jobs or []})},
                    {"exitCode": 0},
                ],
            ) as invoke,
        ):
            result = runner.launch(self.config, "demo-acceptance-test", "all", inspected, COMMIT)
        return result, git, invoke

    def test_host_submits_exact_manifest_without_claiming_completion(self):
        result, git, invoke = self.launch()
        self.assertEqual(result["outcome"], "submitted_not_completed")
        git.assert_called_once()
        path = self.state / "harness/demo-acceptance-test.json"
        self.assertEqual(
            invoke.call_args.args,
            (
                "kubectl apply --server-side --field-manager=radplanes-harness "
                "-f demo-acceptance-test.json",
                "--file",
                str(path),
            ),
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        manifest = json.loads(path.read_text())
        self.assertEqual(
            base64.b64decode(manifest["items"][1]["binaryData"]["source.bundle"]),
            b"actual-head-bundle",
        )

    def test_host_refuses_uninspected_images_and_nonterminal_job(self):
        with self.assertRaisesRegex(runner.HarnessError, "image_inspection_required"):
            self.launch(inspected=False)
        job = {
            "metadata": {
                "name": "demo-acceptance-old",
                "namespace": runner.NAMESPACE,
                "labels": runner.LABELS,
            },
            "status": {},
        }
        for status in ({}, {"conditions": [{"type": "FailureTarget", "status": "True"}]}):
            job["status"] = status
            with self.assertRaisesRegex(runner.HarnessError, "harness_job_nonterminal"):
                self.launch(jobs=[job])
        self.assertFalse((self.state / "harness").exists())

    def test_false_inspection_flag_and_reference_mismatch_fail_before_invoke(self):
        for verified, image in ((False, self.config["images"]["provisioner"]), (True, "other")):
            (self.state / "images.json").write_text(
                json.dumps(
                    {
                        **self.config["images"],
                        "provisioner": image,
                        "content_verified": verified,
                    }
                )
            )
            with self.assertRaises(runner.HarnessError):
                runner.launch(self.config, "demo-acceptance-test", "all", True, COMMIT)
        self.run.assert_not_called()

    def test_bounded_job_name_cannot_inject_a_remote_command(self):
        for name in ("foreign", "demo-acceptance;echo", "demo-acceptance-" + "a" * 17):
            with self.assertRaisesRegex(runner.HarnessError, "job_name_invalid"):
                runner.launch(self.config, name, "all", True, COMMIT)
        self.run.assert_not_called()

    def test_remote_exit_code_and_command_errors_never_expose_raw_output(self):
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess(
            ["az"], 0, json.dumps({"exitCode": 42, "logs": SECRET}), ""
        )
        with self.assertRaisesRegex(runner.HarnessError, "^management_command_failed$"):
            runner.invoke("kubectl get jobs")
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index("--subscription") + 1], runner.SUBSCRIPTION)
        self.assertEqual(self.run.call_args.kwargs["cwd"], self.root)
        self.run.side_effect = subprocess.CalledProcessError(1, ["az", SECRET], stderr=SECRET)
        with self.assertRaisesRegex(runner.HarnessError, "^command_failed$"):
            runner.command(["git", "status"])

    def test_opt_in_is_checked_before_loading_configuration(self):
        with patch.dict(os.environ, {"CONFIRM_AZURE": "no"}), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(runner.main(["--execute"]), 1)
        self.assertEqual(json.loads(out.getvalue())["error"], "azure_confirmation_required")
        self.run.assert_not_called()
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                runner.main([])

    def bootstrap(self, *, bad_digest=False, bad_commit=False):
        bundle_dir = self.root / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "source.bundle").write_bytes(b"bundle")
        for name in ("provisioning.json", "bootstrap.outputs.json"):
            (bundle_dir / name).write_text(json.dumps(self.config))
        key = self.state / "retained-key"
        key.write_text("retain")
        key.chmod(0o600)
        real_path = Path

        def path(value):
            return {"/workspace": self.root, "/bundle": bundle_dir}.get(value, real_path(value))

        def git(argv, **kwargs):
            self.assertTrue(kwargs["check"])
            return SimpleNamespace(
                stdout=("b" * 40 if bad_commit else COMMIT) if argv[1] == "rev-parse" else ""
            )

        class ExecReached(BaseException):
            pass

        self.run.side_effect = git
        with (
            patch("pathlib.Path", side_effect=path),
            patch.object(os, "chdir"),
            patch.object(os, "umask"),
            patch.object(os, "execv", side_effect=ExecReached) as execute,
            patch.object(
                sys,
                "argv",
                [
                    "bootstrap.py",
                    COMMIT,
                    "bad" if bad_digest else hashlib.sha256(b"bundle").hexdigest(),
                    "outages",
                ],
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            try:
                exec(compile(runner.BOOTSTRAP, "<bootstrap>", "exec"), {})
            except ExecReached:
                os.close(int(os.environ["HARNESS_LOCK_FD"]))
            except SystemExit as error:
                self.assertEqual(error.code, 1)
        return key, execute, output.getvalue()

    def test_bootstrap_checks_real_head_and_bundle_without_cloning_over_state(self):
        key, execute, output = self.bootstrap()
        self.assertEqual(output, "")
        self.assertEqual(key.read_text(), "retain")
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.environ["GIT_CONFIG_KEY_0"], "safe.directory")
        self.assertEqual(os.environ["GIT_CONFIG_VALUE_0"], str(self.root))
        self.assertEqual(
            [call.args[0][1:] for call in self.run.call_args_list],
            [
                ["init", "."],
                ["fetch", str(self.root / "bundle/source.bundle"), "HEAD"],
                ["checkout", "--detach", "FETCH_HEAD"],
                ["rev-parse", "HEAD"],
            ],
        )
        self.assertEqual(
            execute.call_args.args[1][-4:], ["--in-cluster", "--mode", "outages", "--execute"]
        )

    def test_bootstrap_rejects_tampered_bundle_before_git(self):
        _, execute, output = self.bootstrap(bad_digest=True)
        self.run.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(json.loads(output)["outcome"], "failed")

    def test_bootstrap_rejects_wrong_commit_before_execution(self):
        _, execute, output = self.bootstrap(bad_commit=True)
        execute.assert_not_called()
        self.assertEqual(json.loads(output)["error"], "harness_bootstrap_failed")

    def test_authentication_uses_rotating_projected_token_and_private_cache(self):
        env = {**os.environ, "AZURE_CONFIG_DIR": str(self.state / "az")}
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=0)
        for token in (SECRET, SECRET + "-rotated"):
            self.token.write_text(token)
            runner.authenticate(self.config, env)
            argv = self.run.call_args.args[0]
            self.assertEqual(argv[argv.index("--federated-token") + 1], token)
            self.assertEqual(self.run.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(self.run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(
                self.run.call_args.kwargs["env"]["AZURE_CONFIG_DIR"], str(self.state / "az")
            )
        self.run.side_effect = subprocess.CalledProcessError(1, [SECRET], stderr=SECRET)
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(runner.HarnessError, "^workload_login_failed$"):
                runner.authenticate(self.config, env)
        self.assertNotIn(SECRET, output.getvalue())

    def test_refresh_runs_every_ten_minutes_and_signals_failure(self):
        stop, failed = Mock(), Mock()
        stop.wait.side_effect = [False, False]
        with patch.object(runner, "authenticate", side_effect=[None, ValueError(SECRET)]) as login:
            runner.refresh_login(self.config, {}, stop, failed)
        self.assertEqual(login.call_count, 2)
        self.assertEqual([call.args for call in stop.wait.call_args_list], [(600,), (600,)])
        failed.set.assert_called_once()

    def test_stale_export_pid_timestamp_or_readiness_cannot_start_scenario(self):
        path, now = self.state / "export-status.json", datetime.now(UTC)
        valid = {
            "pid": 123,
            "observed_at": now.isoformat(),
            "ready_for_onboarding": True,
            "outcome": "waiting",
        }
        for change in (
            {"pid": 122},
            {"observed_at": (now - timedelta(days=1)).isoformat()},
            {"ready_for_onboarding": False},
            {"outcome": "failed"},
        ):
            path.write_text(json.dumps({**valid, **change}))
            self.assertFalse(runner.fresh_export(path, 123, now.timestamp() - 1))
        path.write_text(json.dumps(valid))
        self.assertTrue(runner.fresh_export(path, 123, now.timestamp() - 1))

    def flow(
        self,
        *,
        scenario_code=0,
        evidence_outcome="passed",
        refresh_failure=False,
        exporter_code=None,
        stale=False,
        interrupt=False,
        forced=False,
        mode="all",
    ):
        thread = Mock(ident=1)
        events = []
        exporter = Mock(pid=101, returncode=exporter_code)
        exporter.poll.side_effect = lambda: exporter.returncode
        scenario = Mock(pid=102, returncode=None)

        def poll():
            if scenario.returncode is None and not (refresh_failure or interrupt):
                scenario.returncode = scenario_code
            return scenario.returncode

        scenario.poll.side_effect = poll
        scenario.wait.side_effect = (
            [subprocess.TimeoutExpired("scenario", 60), -9] if forced else None
        )
        stale_path = self.state / "export-status.json"
        stale_path.write_text(
            json.dumps(
                {
                    "pid": 101,
                    "ready_for_onboarding": True,
                    "observed_at": "2000-01-01T00:00:00+00:00",
                }
            )
        )
        handlers = {}

        def set_signal(number, handler):
            old = handlers.get(number)
            handlers[number] = handler
            return old

        def spawn(argv, **kwargs):
            self.assertEqual(kwargs["cwd"], self.root)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["env"]["PYTHONPATH"], str(self.root / "src"))
            self.assertEqual(kwargs["env"]["AZURE_CONFIG_DIR"], str(self.state / "az"))
            if "export-state.py" in argv[1]:
                self.assertEqual(argv[2:], ["--watch", "--timeout", "10800"])
                if not stale:
                    stale_path.write_text(
                        json.dumps(
                            {
                                "pid": 101,
                                "ready_for_onboarding": True,
                                "outcome": "export_complete" if exporter_code == 0 else "waiting",
                                "observed_at": datetime.now(UTC).isoformat(),
                            }
                        )
                    )
                return exporter
            self.assertEqual(
                argv[2:],
                ["--config", str(self.state / "acceptance.json"), "--mode", mode, "--execute"],
            )
            evidence = self.state / "evidence/acceptance-fresh.json"
            evidence.parent.mkdir()
            now = datetime.now(UTC).isoformat()
            evidence.write_text(
                json.dumps(
                    {
                        "outcome": evidence_outcome,
                        "mode": mode,
                        "project": "radplanes",
                        "environment": "azure",
                        "source": {"commit": COMMIT, "worktree_dirty": False},
                        "started_at": now,
                        "finished_at": now,
                    }
                )
            )
            kwargs["stdout"].write(
                json.dumps(
                    {
                        "outcome": "passed",
                        "mode": mode,
                        "evidence": str(evidence),
                    }
                )
            )
            kwargs["stdout"].flush()
            if refresh_failure:
                factory.call_args.kwargs["args"][3].set()
            if interrupt:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return scenario

        self.popen.side_effect = spawn
        with (
            patch.object(runner, "authenticate") as authenticate,
            patch.object(runner.threading, "Thread", return_value=thread) as factory,
            patch.object(runner.signal, "signal", side_effect=set_signal),
            patch.object(runner.os, "kill", side_effect=lambda pid, sig: events.append((pid, sig))),
            patch.object(
                runner.os, "killpg", side_effect=lambda pid, sig: events.append((pid, sig))
            ),
            patch.object(runner.time, "sleep"),
        ):
            result = runner.in_cluster(self.config, mode, COMMIT)
        thread.start.assert_called_once()
        thread.join.assert_called_once_with(timeout=60)
        self.assertIs(factory.call_args.kwargs["target"], runner.refresh_login)
        authenticate.assert_called_once()
        return result, events

    def test_pass_requires_scenario_exit_and_matching_fresh_evidence(self):
        result, _ = self.flow(mode="outages", exporter_code=0)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["mode"], "outages")
        self.assertEqual(result["evidence"], ".state/azure/evidence/acceptance-fresh.json")
        self.assertEqual(
            set(result["diagnostics"]),
            {"exporter-progress", "exporter-errors", "scenario-errors"},
        )
        for relative in result["diagnostics"].values():
            path = self.root / relative
            self.assertTrue(path.is_relative_to(self.state / "harness"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_evidence_never_becomes_success_despite_zero_exit(self):
        result, _ = self.flow(evidence_outcome="failed")
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["error"], "acceptance_evidence_failed")

    def test_evidence_rejects_wrong_mode_commit_project_and_stale_run(self):
        result, _ = self.flow()
        evidence = self.root / result["evidence"]
        original = json.loads(evidence.read_text())
        output = self.state / "summary.json"
        output.write_text(
            json.dumps({"outcome": "passed", "mode": "all", "evidence": str(evidence)})
        )
        for changes in (
            {"mode": "scenario"},
            {"project": "foreign"},
            {"source": {"commit": "b" * 40, "worktree_dirty": False}},
            {"source": {"commit": COMMIT, "worktree_dirty": True}},
            {"started_at": "2000-01-01T00:00:00+00:00"},
        ):
            with self.subTest(changes=changes):
                evidence.write_text(json.dumps({**original, **changes}))
                with self.assertRaisesRegex(runner.HarnessError, "acceptance_evidence_failed"):
                    runner.verify_evidence(
                        output, "all", COMMIT, datetime.now(UTC).timestamp() - 10
                    )

    def test_nonzero_scenario_fails_even_with_passed_evidence(self):
        result, _ = self.flow(scenario_code=1)
        self.assertEqual(result["error"], "scenario_process_failed")
        self.assertEqual(result["outcome"], "failed")

    def test_exporter_failure_prevents_scenario_start(self):
        result, _ = self.flow(exporter_code=1)
        self.assertEqual(result["error"], "exporter_failed")
        self.assertEqual(self.popen.call_count, 1)

    def test_stale_snapshot_with_completed_exporter_does_not_start_scenario(self):
        result, _ = self.flow(exporter_code=0, stale=True)
        self.assertEqual(result["error"], "exporter_exited_before_readiness")
        self.assertEqual(self.popen.call_count, 1)

    def test_auth_failure_terminates_scenario_before_exporter(self):
        result, events = self.flow(refresh_failure=True)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["error"], "workload_login_failed")
        self.assertEqual(events, [(102, signal.SIGTERM), (101, signal.SIGTERM)])

    def test_signal_allows_restoration_and_forced_kill_reports_unknown(self):
        result, events = self.flow(interrupt=True, forced=True)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["error"], "scenario_restoration_unknown")
        self.assertEqual(
            events, [(102, signal.SIGTERM), (102, signal.SIGKILL), (101, signal.SIGTERM)]
        )

    def test_namespace_or_identity_mismatch_never_authenticates(self):
        for variable in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "EXPECTED_SOURCE_COMMIT"):
            with patch.dict(os.environ, {variable: "foreign"}):
                with self.assertRaisesRegex(runner.HarnessError, "identity_or_source_mismatch"):
                    runner.in_cluster(self.config, "all", COMMIT)
        self.namespace.write_text("foreign")
        with self.assertRaisesRegex(runner.HarnessError, "in_cluster_namespace_required"):
            runner.in_cluster(self.config, "all", COMMIT)
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_initial_login_failure_starts_no_children_or_refresh_thread(self):
        with (
            patch.object(runner, "authenticate", side_effect=ValueError(SECRET)),
            patch.object(runner.threading, "Thread") as thread,
        ):
            thread.return_value.ident = None
            result = runner.in_cluster(self.config, "all", COMMIT)
        thread.return_value.start.assert_not_called()
        self.popen.assert_not_called()
        self.assertEqual(result["outcome"], "failed")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_in_cluster_cli_writes_nonsecret_persistent_termination_json(self):
        termination = self.root / "termination"
        result = {"outcome": "failed", "error": "workload_login_failed", "mode": "all"}
        with (
            patch.object(runner, "source_commit", return_value=COMMIT),
            patch.object(runner, "in_cluster", return_value=result),
            patch.object(runner, "TERMINATION", termination),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(runner.main(["--in-cluster", "--execute"]), 1)
        self.assertEqual(json.loads(termination.read_text()), result)
        self.assertEqual(json.loads((self.state / "harness/termination.json").read_text()), result)
        self.assertNotIn(SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()
