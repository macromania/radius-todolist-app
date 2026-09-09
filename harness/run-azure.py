#!/usr/bin/env python3
"""Submit the opt-in Azure acceptance Job, or supervise its existing in-cluster harness."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path("/workspace")
SUBSCRIPTION = "a3ed6c04-563f-4855-ac84-bdf1e5fbc3fc"
NAMESPACE = "radplanes-management-management"
CLUSTER = "aks-radplanes-management"
GROUP = "rg-radplanes-management-cluster"
LABELS = {"project": "radplanes", "plane-demo/harness": "azure-acceptance"}
NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
TOKEN_ROOT = Path("/var/run/secrets/azure/tokens")
TERMINATION = Path("/dev/termination-log")
EXERCISED = [
    "harness",
    "src",
    "operations",
    "infra",
    "images",
    "sql",
    "pyproject.toml",
    "uv.lock",
    ".dockerignore",
]
sys.path.insert(0, str(ROOT / "src"))
from plane_demo.management.provisioning import OperatorConfig  # noqa: E402


class HarnessError(RuntimeError):
    """Only fixed, non-secret identifiers may be reported."""


def require(condition, code):
    if not condition:
        raise HarnessError(code)


def state_path(path):
    path = ROOT / path
    require(
        path.resolve() == path and path.is_relative_to(ROOT / ".state/azure"), "state_path_refused"
    )
    return path


def private_file(path, content):
    path = state_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    pending = path.with_name(path.name + "." + uuid4().hex)
    try:
        with pending.open("x") as stream:
            pending.chmod(0o600)
            stream.write(content)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def command(argv, *, env=None):
    try:
        result = subprocess.run(
            argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False
        )
        require(result.returncode == 0, "command_failed")
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise HarnessError("command_failed") from None


def source_commit():
    require(command(["git", "rev-parse", "--show-toplevel"]) == str(ROOT), "source_root_mismatch")
    commit = command(["git", "rev-parse", "HEAD"])
    require(re.fullmatch(r"[a-f0-9]{40,64}", commit), "source_commit_invalid")
    require(
        not command(["git", "status", "--porcelain", "--untracked-files=all", "--", *EXERCISED]),
        "exercised_source_dirty",
    )
    command(["git", "ls-files", "--error-unmatch", "harness/run-azure.py"])
    tracked = command(["git", "ls-tree", "-r", "--name-only", "HEAD"]).splitlines()
    require(
        not any(
            p.startswith((".state/", ".git/")) or Path(p).name == "credentials.json"
            for p in tracked
        ),
        "tracked_state_refused",
    )
    return commit


def configuration(path):
    config = OperatorConfig.load(state_path(path)).to_dict()
    require(config["foundation"]["subscriptionId"] == SUBSCRIPTION, "subscription_mismatch")
    target = config["managementCluster"]
    expected_id = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/"
        f"Microsoft.ContainerService/managedClusters/{CLUSTER}"
    )
    require(
        target.get("name") == CLUSTER
        and target.get("resourceGroup") == GROUP
        and target.get("id") == expected_id,
        "management_target_mismatch",
    )
    for slot, allocation in config["allocations"].items():
        require(
            allocation["clusterName"] == f"aks-radplanes-{slot}"
            and allocation["clusterResourceGroup"] == f"rg-radplanes-{slot}-cluster"
            and allocation["appResourceGroup"] == f"rg-radplanes-{slot}-app"
            and allocation["clusterResourceGroupId"]
            == f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-radplanes-{slot}-cluster"
            and allocation["appResourceGroupId"]
            == f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-radplanes-{slot}-app",
            "allocation_mismatch",
        )
    harness_identity(config)
    return config


def harness_identity(config):
    identity = config["foundation"].get("harnessIdentity")
    expected_id = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/"
        "Microsoft.ManagedIdentity/userAssignedIdentities/id-radplanes-harness"
    )
    require(
        isinstance(identity, dict) and identity.get("id") == expected_id,
        "harness_identity_required",
    )
    for field in ("clientId", "principalId"):
        value = identity.get(field)
        try:
            valid = isinstance(value, str) and str(UUID(value)) == value
        except ValueError:
            valid = False
        require(valid, "harness_identity_invalid")
    require(
        identity["clientId"] != config["coordinatorIdentity"]["clientId"],
        "harness_identity_must_be_separate",
    )
    return identity


BOOTSTRAP = r"""import fcntl, hashlib, json, os, pathlib, subprocess, sys
root, mount = pathlib.Path("/workspace"), pathlib.Path("/bundle")
commit, digest, mode = sys.argv[1:]
try:
    os.umask(0o077)
    bundle = mount / "source.bundle"
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != digest:
        raise ValueError()
    root.mkdir(exist_ok=True)
    os.chdir(root)
    os.environ.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory",
                      GIT_CONFIG_VALUE_0=str(root))
    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True,
                              text=True, timeout=120).stdout.strip()
    git("init", ".")
    git("fetch", str(bundle), "HEAD")
    git("checkout", "--detach", "FETCH_HEAD")
    if git("rev-parse", "HEAD") != commit:
        raise ValueError()
    state = root / ".state/azure"
    if state.resolve() != state:
        raise ValueError()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    lock = os.open(state / "harness.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(lock, True)
    for name in ("provisioning.json", "bootstrap.outputs.json"):
        destination = state / name
        if destination.is_symlink():
            raise ValueError()
        with destination.open("w") as stream:
            destination.chmod(0o600)
            stream.write((mount / name).read_text())
    os.environ.update(EXPECTED_SOURCE_COMMIT=commit, HARNESS_LOCK_FD=str(lock),
                      PYTHONPATH=str(root / "src"))
    os.execv(sys.executable, [sys.executable, str(root / "harness/run-azure.py"),
                             "--in-cluster", "--mode", mode, "--execute"])
except Exception:
    print(json.dumps({"outcome": "failed", "mode": mode, "commit": commit,
                      "evidence": None, "error": "harness_bootstrap_failed"}))
    sys.exit(1)
"""


def resources(config, name, bundle, commit, mode):
    metadata = {"namespace": NAMESPACE, "labels": LABELS}
    identity = harness_identity(config)
    bootstrap = {
        key: config[key] for key in ("foundation", "coordinatorIdentity", "managementCluster")
    }
    bootstrap["allocations"] = list(config["allocations"].values())
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "immutable": True,
        "metadata": {**metadata, "name": name + "-source"},
        "binaryData": {"source.bundle": base64.b64encode(bundle).decode()},
        "data": {
            "bootstrap.py": BOOTSTRAP,
            "provisioning.json": json.dumps(config),
            "bootstrap.outputs.json": json.dumps(bootstrap),
        },
    }
    require(len(json.dumps(cm).encode()) < 900 * 1024, "source_configmap_too_large")
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {**metadata, "name": "harness-state"},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": "radplanes-provisioner",
            "resources": {"requests": {"storage": "8Gi"}},
        },
    }
    service_account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            **metadata,
            "name": "harness",
            "annotations": {
                "azure.workload.identity/client-id": identity["clientId"],
                "azure.workload.identity/tenant-id": config["foundation"]["tenantId"],
            },
        },
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {**metadata, "name": name},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 10800,
            "template": {
                "metadata": {"labels": {**LABELS, "azure.workload.identity/use": "true"}},
                "spec": {
                    "serviceAccountName": "harness",
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 150,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "harness",
                            "image": config["images"]["provisioner"],
                            "terminationMessagePolicy": "FallbackToLogsOnError",
                            "workingDir": "/workspace",
                            "command": [
                                "/app/.venv/bin/python",
                                "/bundle/bootstrap.py",
                                commit,
                                hashlib.sha256(bundle).hexdigest(),
                                mode,
                            ],
                            "env": [{"name": "CONFIRM_AZURE", "value": "yes"}],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "source", "mountPath": "/bundle", "readOnly": True},
                                {"name": "workspace", "mountPath": "/workspace"},
                                {"name": "state", "mountPath": "/workspace/.state"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "source", "configMap": {"name": name + "-source"}},
                        {"name": "workspace", "emptyDir": {}},
                        {"name": "state", "persistentVolumeClaim": {"claimName": "harness-state"}},
                    ],
                },
            },
        },
    }
    return [pvc, cm, service_account, job]


def invoke(remote_command, *extra):
    result = json.loads(
        command(
            [
                "az",
                "aks",
                "command",
                "invoke",
                "--subscription",
                SUBSCRIPTION,
                "--name",
                CLUSTER,
                "--resource-group",
                GROUP,
                "--command",
                remote_command,
                *extra,
                "--output",
                "json",
                "--only-show-errors",
            ]
        )
    )
    require(result.get("exitCode") == 0, "management_command_failed")
    return result


def launch(config, name, mode, inspected, commit):
    require(re.fullmatch(r"demo-acceptance(?:-[a-z0-9]{1,16})?", name), "job_name_invalid")
    images = json.loads(state_path(Path(".state/azure/images.json")).read_text())
    require(inspected and images.get("content_verified") is True, "image_inspection_required")
    for role in ("api", "provisioner"):
        image = images.get(role)
        require(
            (image.get("reference") if isinstance(image, dict) else image)
            == config["images"][role],
            "inspected_image_mismatch",
        )
    jobs = json.loads(
        invoke(
            f"kubectl get jobs -n {NAMESPACE} "
            "-l project=radplanes,plane-demo/harness=azure-acceptance -o json"
        )["logs"]
    )
    for job in jobs["items"]:
        require(
            job["metadata"]["namespace"] == NAMESPACE
            and all(job["metadata"]["labels"].get(k) == v for k, v in LABELS.items()),
            "harness_job_ownership_mismatch",
        )
        require(
            any(
                c.get("type") in {"Complete", "Failed"} and c.get("status") == "True"
                for c in job.get("status", {}).get("conditions", [])
            ),
            "harness_job_nonterminal",
        )
        require(job["metadata"]["name"] != name, "harness_job_name_reused")
    manifest = state_path(Path(f".state/azure/harness/{name}.json"))
    bundle = manifest.with_suffix(".bundle")
    require(not manifest.exists() and not bundle.exists(), "harness_artifacts_exist")
    manifest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    command(["git", "bundle", "create", str(bundle), "HEAD"])
    bundle.chmod(0o600)
    require(source_commit() == commit, "source_changed_during_bundle")
    private_file(
        manifest,
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": resources(config, name, bundle.read_bytes(), commit, mode),
            }
        ),
    )
    invoke(
        f"kubectl apply --server-side --field-manager=radplanes-harness -f {manifest.name}",
        "--file",
        str(manifest),
    )
    return {
        "outcome": "submitted_not_completed",
        "namespace": NAMESPACE,
        "job": name,
        "mode": mode,
        "commit": commit,
    }


def authenticate(config, env):
    try:
        token = Path(env["AZURE_FEDERATED_TOKEN_FILE"]).read_text().strip()
        require(bool(token), "workload_login_failed")
        result = subprocess.run(
            [
                "az",
                "login",
                "--service-principal",
                "--username",
                harness_identity(config)["clientId"],
                "--tenant",
                config["foundation"]["tenantId"],
                "--federated-token",
                token,
                "--allow-no-subscriptions",
                "--output",
                "none",
                "--only-show-errors",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        require(result.returncode == 0, "workload_login_failed")
    except Exception:
        raise HarnessError("workload_login_failed") from None


def refresh_login(config, env, stop, failed):
    while not stop.wait(600):
        try:
            authenticate(config, env)
        except Exception:
            failed.set()
            return


def fresh_export(path, pid, started):
    try:
        value = json.loads(path.read_text())
        observed = datetime.fromisoformat(value["observed_at"]).timestamp()
        return (
            value.get("pid") == pid
            and value.get("ready_for_onboarding") is True
            and value.get("outcome") in {"waiting", "export_complete"}
            and started <= observed <= time.time()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def stop_child(process, timeout):
    if process is None or process.poll() is not None:
        return False
    try:
        # Signal the scenario itself first: its finally blocks need living kubectl children.
        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
        return False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        return True
    except ProcessLookupError:
        return False


def verify_evidence(output, mode, commit, started):
    summary = json.loads(output.read_text())
    require(
        summary.get("outcome") == "passed" and summary.get("mode") == mode,
        "scenario_summary_failed",
    )
    evidence = state_path(Path(summary["evidence"]))
    require(evidence.is_relative_to(ROOT / ".state/azure/evidence"), "evidence_path_refused")
    value = json.loads(evidence.read_text())
    require(
        value.get("outcome") == "passed"
        and value.get("mode") == mode
        and value.get("project") == "radplanes"
        and value.get("environment") == "azure"
        and value.get("source", {}).get("commit") == commit
        and value["source"].get("worktree_dirty") is False
        and started
        <= datetime.fromisoformat(value["started_at"]).timestamp()
        <= datetime.fromisoformat(value["finished_at"]).timestamp()
        <= time.time(),
        "acceptance_evidence_failed",
    )
    return str(evidence.relative_to(ROOT))


def in_cluster(config, mode, commit):
    require(
        ROOT == WORKSPACE and NAMESPACE_FILE.read_text().strip() == NAMESPACE,
        "in_cluster_namespace_required",
    )
    require(
        os.environ.get("AZURE_CLIENT_ID") == harness_identity(config)["clientId"]
        and os.environ.get("AZURE_TENANT_ID") == config["foundation"]["tenantId"]
        and os.environ.get("EXPECTED_SOURCE_COMMIT") == commit,
        "workload_identity_or_source_mismatch",
    )
    token = Path(os.environ.get("AZURE_FEDERATED_TOKEN_FILE", ""))
    require(
        token.is_absolute() and token.resolve().is_relative_to(TOKEN_ROOT.resolve()),
        "projected_token_required",
    )
    state = state_path(Path(".state/azure"))
    lock = int(os.environ["HARNESS_LOCK_FD"])
    require(
        os.fstat(lock).st_ino == (state / "harness.lock").stat().st_ino, "harness_lock_required"
    )
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    az_dir = state_path(state / "az")
    az_dir.mkdir(exist_ok=True, mode=0o700)
    az_dir.chmod(0o700)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "AZURE_CONFIG_DIR": str(az_dir)}
    stop, failed, interrupted = threading.Event(), threading.Event(), threading.Event()
    thread = threading.Thread(target=refresh_login, args=(config, env, stop, failed), daemon=True)
    previous = {
        s: signal.signal(s, lambda *_: interrupted.set()) for s in (signal.SIGTERM, signal.SIGINT)
    }
    exporter = scenario = None
    result = {"outcome": "failed", "mode": mode, "commit": commit, "evidence": None}
    streams = ExitStack()
    diagnostics = {}

    def log_stream(name):
        path = state / "harness" / f"{name}-{uuid4().hex}.log"
        private_file(path, "")
        diagnostics[name] = str(path.relative_to(ROOT))
        return streams.enter_context(path.open("w"))

    def healthy():
        require(not interrupted.is_set(), "harness_interrupted")
        require(not failed.is_set(), "workload_login_failed")
        require(exporter.poll() in (None, 0), "exporter_failed")

    def start(script, *args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
        return subprocess.Popen(
            [sys.executable, str(ROOT / "harness" / script), *args],
            cwd=ROOT,
            env=env,
            start_new_session=True,
            stdout=stdout,
            stderr=stderr,
        )

    try:
        authenticate(config, env)
        thread.start()
        started = time.time()
        exporter = start(
            "export-state.py",
            "--watch",
            "--timeout",
            "10800",
            stdout=log_stream("exporter-progress"),
            stderr=log_stream("exporter-errors"),
        )
        deadline = time.monotonic() + 600
        while not fresh_export(state / "export-status.json", exporter.pid, started):
            healthy()
            require(exporter.poll() is None, "exporter_exited_before_readiness")
            require(time.monotonic() < deadline, "export_readiness_timeout")
            time.sleep(1)
        healthy()
        output = state / "harness" / f"result-{uuid4().hex}.json"
        private_file(output, "")
        with output.open("w") as stream:
            started = time.time()
            scenario = start(
                "test-e2e.py",
                "--config",
                str(state / "acceptance.json"),
                "--mode",
                mode,
                "--execute",
                stdout=stream,
                stderr=log_stream("scenario-errors"),
            )
            while scenario.poll() is None:
                healthy()
                time.sleep(1)
        healthy()
        require(scenario.returncode == 0, "scenario_process_failed")
        result["evidence"] = verify_evidence(output, mode, commit, started)
        result["outcome"] = "passed"
    except Exception as error:
        result["error"] = str(error) if isinstance(error, HarnessError) else "harness_failed"
    finally:
        try:
            if stop_child(scenario, 60):
                result.update(
                    outcome="failed", error="scenario_restoration_unknown", restoration="unknown"
                )
        finally:
            try:
                if exporter is not None and exporter.poll() not in (None, 0):
                    result.update(outcome="failed", error="exporter_failed")
                stop_child(exporter, 10)
            finally:
                stop.set()
                if thread.ident is not None:
                    thread.join(timeout=60)
                if failed.is_set():
                    result.update(outcome="failed", error="workload_login_failed")
                if interrupted.is_set() and result["outcome"] == "passed":
                    result.update(outcome="failed", error="harness_interrupted")
                for number, handler in previous.items():
                    signal.signal(number, handler)
                streams.close()
                result["diagnostics"] = diagnostics
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".state/azure/provisioning.json")
    parser.add_argument("--name", default="demo-acceptance-" + uuid4().hex[:12])
    parser.add_argument("--mode", choices=("all", "scenario", "outages"), default="all")
    parser.add_argument("--images-inspected", action="store_true")
    parser.add_argument("--in-cluster", action="store_true")
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    commit = None
    try:
        require(os.environ.get("CONFIRM_AZURE") == "yes", "azure_confirmation_required")
        config, commit = configuration(args.config), source_commit()
        result = (
            in_cluster(config, args.mode, commit)
            if args.in_cluster
            else launch(config, args.name, args.mode, args.images_inspected, commit)
        )
    except Exception as error:
        code = str(error) if isinstance(error, HarnessError) else "azure_harness_failed"
        result = {
            "outcome": "failed",
            "mode": args.mode,
            "commit": commit,
            "evidence": None,
            "error": code,
        }
    if args.in_cluster:
        try:
            private_file(Path(".state/azure/harness/termination.json"), json.dumps(result))
            TERMINATION.write_text(json.dumps(result))
        except (OSError, HarnessError):
            result.update(outcome="failed", error="termination_write_failed")
    print(json.dumps(result))
    return 0 if result["outcome"] in {"passed", "submitted_not_completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
