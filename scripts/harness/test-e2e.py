#!/usr/bin/env python3
"""Run real API acceptance and scoped outages only after an explicit --execute."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import ssl
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "plane_demo_fault_helper", ROOT / "scripts/harness/fault-parent-link.py"
)
faults = sys.modules.get(_spec.name)
if faults is None or Path(faults.__file__) != Path(_spec.origin):
    faults = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = faults
    _spec.loader.exec_module(faults)
AcceptanceError = faults.AcceptanceError
require = faults.require
TENANT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
REPORTS = {
    "control_record_created",
    "control_record_failed",
    "config_applied",
    "config_apply_failed",
}
PARENT_EVENTS = {
    "management": {
        "tenant_requested",
        "control_record_created",
        "control_record_failed",
        "provisioning_stage",
        "provisioning_started",
        "provisioning_succeeded",
        "provisioning_failed",
        "provisioning_interrupted",
    },
    "control": {
        "configuration_created",
        "configuration_updated",
        "config_applied",
        "config_apply_failed",
    },
}
MODULES = [
    "__init__",
    "shared/__init__",
    "shared/settings",
    "shared/auth",
    "shared/models",
    "shared/db",
    "shared/http",
    "shared/kube",
    "management/__init__",
    "management/api",
    "control/__init__",
    "control/api",
    "control/reconciler",
    "data/__init__",
    "data/api",
    "data/reconciler",
    "setup/__init__",
    "setup/acme_responder",
    "setup/bootstrap",
]

SOURCE_PROBE = r"""
import hashlib,json,os,sys
from pathlib import Path
root=Path("/app").resolve()
result={}
for relative in json.loads(sys.argv[1]):
    path=(root/relative).resolve()
    if not path.is_relative_to(root): raise ValueError("source_path_escape")
    result[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({"files":result,"parent_dsn_present":
                 any(name in os.environ for name in ("MANAGEMENT_DSN","CONTROL_DSN"))}))
"""

DATA_API_PERMISSIONS_PROBE = r"""
import json,sys
from kubernetes import client,config
from kubernetes.client.exceptions import ApiException
namespace=sys.argv[1]
expected=json.loads(sys.argv[2])
config.load_incluster_config()
authorization=client.AuthorizationV1Api()
permissions={}
for key,allowed in expected.items():
    resource,verb,*name=key.split(":")
    attributes={"namespace":namespace,"verb":verb,"group":"","resource":resource}
    if "/" in resource:
        attributes["resource"],attributes["subresource"]=resource.split("/",1)
    if name:
        attributes["name"]=name[0]
    review=authorization.create_self_subject_access_review(
        body={"apiVersion":"authorization.k8s.io/v1","kind":"SelfSubjectAccessReview",
              "spec":{"resourceAttributes":attributes}})
    permissions[key]=review.status.allowed
    if review.status.allowed is not allowed:
        raise RuntimeError("data_api_kubernetes_permissions_exceed_contract")
core=client.CoreV1Api()
for action in (lambda:core.read_namespaced_secret("data-reconciler-runtime",namespace),
               lambda:core.list_namespaced_secret(namespace,limit=1)):
    try:
        action()
    except ApiException as error:
        if error.status!=403:
            raise RuntimeError("data_api_secret_denial_not_authenticated_forbidden") from None
    else:
        raise RuntimeError("data_api_can_read_namespace_secrets")
print(json.dumps({"permissions":permissions,"parent_secret_get_status":403,
                  "secret_list_status":403}))
"""


def data_api_permissions(tenants):
    expected = {
        resource + ":" + verb: resource == "configmaps" and verb == "get"
        for resource, verbs in (
            ("configmaps", ("get", "list", "watch", "create", "update", "patch", "delete")),
            ("secrets", ("get", "list", "watch", "create", "update", "patch", "delete")),
            ("pods", ("create",)),
            ("serviceaccounts/token", ("create",)),
        )
        for verb in verbs
    }
    for account in (
        "data-api",
        "data-api-runtime",
        "data-reconciler",
        "database-init",
        "challenge",
    ):
        expected[f"serviceaccounts/token:create:{account}"] = False
    for tenant in tenants:
        for verb in ("list", "watch", "create", "update", "patch", "delete"):
            expected[f"configmaps:{verb}:tenant-{tenant}"] = False
    for verb in ("get", "list", "watch", "create", "update", "patch", "delete"):
        expected[f"secrets:{verb}:data-reconciler-runtime"] = False
    return expected


COORDINATOR_SCRIPTS = [
    "__init__.py",
    "config.py",
    "demo.py",
    "project.py",
    "management_job.py",
    "azure/registry_policy.py",
    "azure/registry-policy.json",
    "install-radius.py",
    "install-radius.sh",
    "deploy-plane.py",
    "register-radius.py",
    "issue-certificate.py",
    "acme-hook.py",
    "run-certificate-job.py",
]


def source_files(component):
    if component != "provisioner":
        return sorted(
            [f"src/plane_demo/{name}.py" for name in MODULES]
            + [
                "sql/management.sql",
                "sql/control.sql",
            ]
        )
    paths = [
        *ROOT.glob("src/plane_demo/**/*.py"),
        *ROOT.glob("sql/*.sql"),
        *ROOT.glob("infra/radius/apps/*.bicep"),
        *ROOT.glob("infra/radius/modules/*.bicep"),
        *ROOT.glob("infra/radius/types/*.yaml"),
        ROOT / "infra/radius/bicepconfig.json",
        ROOT / "infra/radius/environments/azure.bicep",
        ROOT / "scripts/__init__.py",
        *(ROOT / "scripts/operations" / name for name in COORDINATOR_SCRIPTS),
    ]
    return sorted(str(path.relative_to(ROOT)) for path in paths)


def source_hashes(component):
    hashes = {}
    for relative in source_files(component):
        path = (ROOT / relative).resolve()
        require(path.is_relative_to(ROOT), "source_path_escape")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def local_source_hashes(component):
    paths = [ROOT / "pyproject.toml", ROOT / "uv.lock", *ROOT.glob("sql/*.sql")]
    if component != "provisioner":
        paths += [ROOT / f"src/plane_demo/{name}.py" for name in MODULES]
    else:
        paths += [
            *ROOT.glob("src/plane_demo/**/*.py"),
            ROOT / "scripts/__init__.py",
            *(ROOT / "scripts/operations" / name for name in COORDINATOR_SCRIPTS),
            *ROOT.glob("scripts/operations/local/*.py"),
            *ROOT.glob("scripts/operations/local/*.yaml"),
            *ROOT.glob("scripts/operations/local/*.sh"),
            *ROOT.glob("scripts/recipes/local/cluster/*.sh"),
            *(
                path
                for path in (ROOT / "infra/radius").rglob("*")
                if path.is_file()
                and path.suffix in {".bicep", ".yaml", ".json", ".tf", ".hcl", ".sh"}
                and ".terraform" not in path.parts
                and ".build" not in path.parts
            ),
        ]
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


IDENTITY_PROBE = r"""
import json,os,ssl,sys
role=sys.argv[1]
local=len(sys.argv)>2 and sys.argv[2]=="local"
if role in ("management-api","control-api"):
    import psycopg
    from psycopg.rows import dict_row
    name="MANAGEMENT_DSN" if role=="management-api" else "CONTROL_DSN"
    with psycopg.connect(os.environ[name],connect_timeout=5,
                         options="-c statement_timeout=5000",row_factory=dict_row) as connection:
        identity=connection.execute(
            "SELECT current_database() AS database,inet_server_addr()::text AS server_address"
        ).fetchone()
        identity.update(host=connection.info.host,port=connection.info.port)
        if local:
            from psycopg.conninfo import conninfo_to_dict
            parameters=conninfo_to_dict(os.environ[name])
            if not parameters.get("password") or parameters.get("sslmode")!="disable":
                raise RuntimeError("local_postgresql_connection_contract")
            if connection.pgconn.ssl_in_use:
                raise RuntimeError("local_postgresql_transport_contract")
            identity["tls"]=False
        result={"postgresql":identity}
else:
    from plane_demo.shared.settings import Settings,redis_client
    store=redis_client(Settings.from_env("data_api"))
    if not store.ping(): raise RuntimeError("redis_ping_failed")
    connection=store.connection_pool.get_connection()
    try:
        connected=connection._sock
        result={"redis":{"host":connection.host,"port":connection.port,
                         "peer_address":connected.getpeername()[0],
                         "tls":isinstance(connected,ssl.SSLSocket)
                               and connected.version() is not None}}
        if local:
            import redis,secrets
            if not connection.password: raise RuntimeError("redis_password_missing")
            with redis.Redis(host=connection.host,port=connection.port,
                             username=connection.username,password=secrets.token_urlsafe(32),
                             socket_timeout=3,socket_connect_timeout=3) as wrong:
                try: wrong.ping()
                except redis.AuthenticationError: pass
                else: raise RuntimeError("redis_wrong_password_not_rejected")
            result["redis"]["authenticated"]=True
            result["redis"]["wrong_password_rejected"]=True
    finally:
        store.connection_pool.release(connection)
        store.close()
print(json.dumps(result))
"""


class Client:
    def __init__(self, url: str, key: str, *, transport=None):
        require(isinstance(key, str) and re.fullmatch(r"[!-~]{32,512}", key), "invalid_demo_key")
        self.url, self.key = url.rstrip("/"), key
        self.before_mutation = None
        self.client = httpx.Client(
            base_url=self.url,
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def ready(self):
        try:
            with self.client.stream("GET", "/healthz") as response:
                if response.status_code in {429, 502, 503, 504}:
                    return False
                require(response.status_code == 200, "gateway_health_contract_failed")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    require(len(content) <= 4096, "gateway_health_response_too_large")
                try:
                    value = json.loads(content)
                except ValueError:
                    raise AcceptanceError("gateway_health_contract_failed") from None
                require(value == {"status": "ok"}, "gateway_health_contract_failed")
                return True
        except httpx.HTTPError as error:
            cause = error
            while cause:
                if isinstance(cause, ssl.SSLError):
                    raise AcceptanceError("gateway_tls_verification_failed") from None
                cause = cause.__cause__
            if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
                return False
            raise AcceptanceError("api_transport_failed") from None

    def request(self, method: str, path: str, *, body=None, statuses=(200,), key=None):
        if self.before_mutation is not None and method not in {"GET", "HEAD", "OPTIONS"}:
            self.before_mutation()
        headers = {"X-Demo-Key": self.key if key is None else key}
        if key == "":
            headers = {}
        try:
            with self.client.stream(method, path, json=body, headers=headers) as response:
                require(response.status_code in statuses, "unexpected_api_status")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    require(len(content) <= 1_000_000, "api_response_too_large")
                status, response_headers = response.status_code, response.headers
        except httpx.HTTPError:
            raise AcceptanceError("api_transport_failed") from None
        try:
            value = json.loads(content)
        except ValueError:
            raise AcceptanceError("api_response_not_json") from None
        require(isinstance(value, dict), "api_response_not_object")
        return status, value, response_headers

    def get(self, tenant: str):
        return self.request("GET", f"/tenants/{tenant}")[1]

    def close(self):
        self.client.close()


class APIs:
    def __init__(self, configuration, factory=Client):
        self.configuration = configuration
        self.factory = factory
        self.clients = {}
        self.guard = None

    def client(self, target: str) -> Client:
        if target in self.clients:
            return self.clients[target]
        if self.configuration.live:
            url, key = self.configuration.endpoint(target)
        else:
            values = self.configuration.current()
            endpoints = faults.read_json(self.configuration.file(values.get("endpoints_file")))
            if target == "management":
                endpoint = endpoints.get("management")
            else:
                role, pair = target.split(":", 1)
                endpoint = endpoints.get("pairs", {}).get(pair, {}).get(role)
            require(isinstance(endpoint, dict), "endpoint_not_exported")
            url = endpoint.get("url", "")
            key_file = self.configuration.file(endpoint.get("key_file"), secret=True)
            require(key_file.stat().st_size <= 512, "api_key_file_too_large")
            key = key_file.read_text().strip()
        parsed = urlsplit(url)
        require(
            bool(parsed.hostname)
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            and parsed.path in {"", "/"},
            "invalid_endpoint_url",
        )
        if self.configuration.environment == "azure":
            require(
                parsed.scheme == "https" and parsed.hostname.endswith(".cloudapp.azure.com"),
                "azure_endpoint_requires_trusted_https",
            )
        else:
            ports = {
                "management": 35490,
                "control:shared": 35491,
                "data:shared": 35492,
                "control:isolated-1": 35493,
                "data:isolated-1": 35494,
            }
            require(
                parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
                and parsed.port == ports.get(target),
                "local_endpoint_not_reserved_loopback",
            )
        require(isinstance(key, str) and re.fullmatch(r"[!-~]{32,512}", key), "invalid_demo_key")
        require(
            all(existing.key != key for existing in self.clients.values()),
            "plane_keys_are_not_distinct",
        )
        client = self.factory(url, key)
        client.before_mutation = lambda: self.guard() if self.guard is not None else None
        self.clients[target] = client
        return client

    def close(self):
        for client in self.clients.values():
            client.close()


def until(
    check, *, timeout: float, deadline: float | None = None, clock=time.monotonic, sleep=time.sleep
):
    started = clock()
    end = min(started + timeout, deadline) if deadline is not None else started + timeout
    while True:
        require(clock() <= end, "convergence_deadline_exceeded")
        value = check()
        observed = clock()
        elapsed = observed - started
        require(observed <= end, "convergence_deadline_exceeded")
        if value:
            return value, elapsed
        require(observed < end, "convergence_deadline_exceeded")
        sleep(min(2, end - observed))


def report_time(value):
    require(isinstance(value, str) and bool(value), "report_timestamp_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AcceptanceError("report_timestamp_invalid") from None
    require(parsed.tzinfo is not None, "report_timestamp_requires_timezone")
    return parsed


def applied_summary(control, onboarding, version):
    require(control.get("onboarding_id") == onboarding, "control_onboarding_mismatch")
    require(control.get("desired", {}).get("version") == version, "desired_version_mismatch")
    data = control.get("data_config", {})
    require(data.get("status") == "applied", "control_not_applied")
    require(data.get("last_applied_version") == version, "control_applied_version_mismatch")
    report_time(data.get("reported_at"))


def control_history(rows, control, onboarding):
    version = control["desired"]["version"]
    require(type(version) is int and version >= 1, "invalid_desired_version")
    applied_summary(control, onboarding, version)
    created = [row for row in rows if row["type"] == "configuration_created"]
    require(
        len(created) == 1 and created[0]["version"] == 1,
        "configuration_created_event_missing_or_invalid",
    )
    updates = [row["version"] for row in rows if row["type"] == "configuration_updated"]
    require(
        len(updates) == version - 1
        and len(updates) == len(set(updates))
        and set(updates) == set(range(2, version + 1)),
        "configuration_updated_events_incomplete",
    )
    require(
        all(type(row.get("version")) is int and 1 <= row["version"] <= version for row in rows),
        "invalid_control_timeline_version",
    )
    latest = [row for row in rows if row["type"] == "config_applied" and row["version"] == version]
    require(len(latest) == 1, "latest_applied_report_missing")
    require(
        report_time(latest[0].get("received_at"))
        == report_time(control["data_config"]["reported_at"]),
        "applied_report_timestamp_mismatch",
    )


def timeline(client: Client, tenant: str, parent: str) -> list[dict]:
    cursor, result, seen = 0, [], set()
    while True:
        _, value, _ = client.request("GET", f"/tenants/{tenant}?after_event_id={cursor}&limit=2")
        rows = value.get("timeline")
        require(isinstance(rows, list) and len(rows) <= 2, "invalid_timeline_page")
        for row in rows:
            event_id = row.get("event_id")
            require(
                isinstance(event_id, int) and event_id > cursor and event_id not in seen,
                "timeline_order_or_duplicate",
            )
            require(row.get("type") in PARENT_EVENTS[parent], "transitive_or_unknown_parent_event")
            require(
                set(row) <= {"event_id", "type", "version", "error_code", "stage", "received_at"},
                "unexpected_timeline_fields",
            )
            result.append(row)
            seen.add(event_id)
            cursor = event_id
        require(len(result) <= 100_000, "timeline_exceeded_demo_scope")
        next_id = value.get("next_after_event_id")
        if next_id is None:
            break
        require(bool(rows) and next_id == cursor, "invalid_timeline_cursor")
    report_keys = [(row["type"], row["version"]) for row in result if row["type"] in REPORTS]
    require(len(report_keys) == len(set(report_keys)), "duplicate_child_report")
    return result


def replicas(kube, component: str, count: int, *, uid: str, expected: int):
    current = kube.deployment(component)
    require(current["metadata"]["uid"] == uid, "deployment_replaced_during_test")
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/spec/replicas", "value": expected},
        {"op": "replace", "path": "/spec/replicas", "value": count},
    ]
    kube.run(
        "patch", "deployment", current["metadata"]["name"], "--type=json", "-p", json.dumps(patch)
    )


@contextmanager
def paused_reconciler(kube, *, clock=time.monotonic, sleep=time.sleep):
    component = "data-reconciler"
    kube.verify_scope()
    original = kube.deployment(component)
    require(original["spec"].get("replicas", 1) == 1, "pause_requires_single_replica")
    grace = original["spec"]["template"]["spec"].get("terminationGracePeriodSeconds", 30)
    require(type(grace) is int and 0 <= grace <= 300, "invalid_pause_termination_grace")
    uid = original["metadata"]["uid"]
    try:
        replicas(kube, component, 0, uid=uid, expected=1)
        selector = ",".join(f"{key}={value}" for key, value in kube.labels(component).items())
        try:
            until(
                lambda: not kube.json("get", "pods", "-l", selector).get("items", []),
                timeout=grace + 30,
                clock=clock,
                sleep=sleep,
            )
        except AcceptanceError as error:
            if str(error) != "convergence_deadline_exceeded":
                raise
            raise AcceptanceError("reconciler_pause_drain_timeout") from None
        yield {"deployment": original["metadata"]["name"], "uid": uid, "original_replicas": 1}
    finally:
        current = kube.deployment(component)
        require(current["metadata"]["uid"] == uid, "paused_deployment_ownership_changed")
        count = current["spec"].get("replicas", 1)
        require(count in {0, 1}, "paused_replica_count_changed_externally")
        if count == 0:
            replicas(kube, component, 1, uid=uid, expected=0)
        kube.run(
            "rollout",
            "status",
            "deployment/" + current["metadata"]["name"],
            "--timeout=60s",
            timeout=70,
        )


class Runner:
    def __init__(
        self,
        configuration,
        mode: str,
        *,
        continue_first_from: str | None = None,
        apis=None,
        kube_factory=faults.Kubectl,
        fault_factory=None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.configuration, self.mode = configuration, mode
        self.journal = None
        self.continue_first_from = continue_first_from
        self.apis = apis if apis is not None else APIs(configuration)
        if configuration.live and isinstance(self.apis, APIs):
            self.apis.guard = self.check_journal
        self.kube_factory = (
            configuration.kube
            if configuration.live and kube_factory is faults.Kubectl
            else kube_factory
        )
        self.fault_factory = fault_factory or faults.fault_class(configuration)
        self.clock, self.sleep = clock, sleep
        self.run_id = uuid4().hex
        self.path = configuration.root / "evidence" / f"acceptance-{self.run_id}.json"
        if continue_first_from is not None:
            require(
                not self.path.exists() and not self.path.is_symlink(), "continuation_output_exists"
            )
        self.record = {
            "version": 1,
            "run_id": self.run_id,
            "mode": mode,
            "outcome": "not_run",
            "environment": configuration.environment,
            "project": configuration.project,
            "started_at": faults.utc_now(),
            "events": [],
        }
        if mode == "verify-existing":
            require(
                not self.path.exists() and not self.path.is_symlink(), "acceptance_output_exists"
            )
            self.record.update(scope="existing-tenants-only", admission_checks_performed=False)
        names = configuration.current().get("tenants", {})
        self.names = [
            names.get(key, default)
            for key, default in [
                ("shared_a", "shared-a"),
                ("shared_b", "shared-b"),
                ("isolated", "isolated-c"),
            ]
        ]
        require(
            len(set(self.names)) == 3 and all(TENANT.fullmatch(name) for name in self.names),
            "invalid_acceptance_tenant_names",
        )
        require(
            configuration.current().get("synthetic_data") is True,
            "synthetic_data_confirmation_required",
        )
        self.tenants = {}
        self.expectations = {}
        self.updates_finished_at = None
        self.images = {}

    def load_first_admission(self):
        relative = self.continue_first_from
        require(self.mode in {"scenario", "all"}, "continuation_requires_scenario")
        prior_journal = None
        if self.configuration.live:
            require(isinstance(relative, str), "continuation_journal_required")
            name = relative.split("@")[0]
            prior_journal = faults.ConfigMapJournal(
                self.configuration.kube(self.configuration.target("management")),
                name,
                "acceptance",
            )
            raw = faults.canonical_json(prior_journal.load(relative)).encode()
            prior_journal.check()
            expected_id = name.removeprefix("plane-demo-acceptance-")
        else:
            require(
                self.configuration.environment == "azure"
                and self.configuration.root == ROOT / ".state/azure"
                and isinstance(relative, str)
                and re.fullmatch(r"\.state/azure/evidence/acceptance-[a-f0-9]{32}\.json", relative),
                "continuation_path_refused",
            )
            path = faults.state_path(Path(relative))
            require(path == ROOT / relative, "continuation_symlink_refused")
            path = self.configuration.file(
                str(path.relative_to(self.configuration.root)), secret=True
            )
            with path.open("rb") as stream:
                raw = stream.read(2_000_001)
            expected_id = path.stem.removeprefix("acceptance-")
        require(len(raw) <= 2_000_000, "continuation_evidence_too_large")

        def unique_fields(pairs):
            value = dict(pairs)
            require(len(value) == len(pairs), "continuation_duplicate_field")
            return value

        prior = json.loads(raw, object_pairs_hook=unique_fields)
        require(
            isinstance(prior, dict)
            and type(prior.get("version")) is int
            and prior["version"] == 1
            and prior.get("project") == self.configuration.project
            and prior.get("environment") == self.configuration.environment
            and prior.get("mode") == self.mode
            and prior.get("outcome")
            in ({"failed", "running"} if self.configuration.live else {"failed"})
            and prior.get("run_id") == expected_id
            and prior["run_id"] != self.run_id
            and "continued_first_from" not in prior,
            "continuation_identity_mismatch",
        )
        source = prior.get("source")
        require(
            isinstance(source, dict)
            and set(source) == {"commit", "committed_at", "worktree_dirty"}
            and isinstance(source["commit"], str)
            and re.fullmatch(r"[a-f0-9]{40,64}", source["commit"])
            and source["worktree_dirty"] is False,
            "continuation_source_invalid",
        )
        started, finished = (
            report_time(prior.get("started_at")),
            report_time(
                prior.get("finished_at")
                or (prior_journal.updated_at if prior_journal is not None else None)
            ),
        )
        require(
            report_time(source["committed_at"])
            <= started
            <= finished
            <= report_time(self.record["started_at"]),
            "continuation_timestamps_invalid",
        )
        require(
            report_time(
                faults.command(["git", "show", "-s", "--format=%cI", source["commit"]]).strip()
            )
            == report_time(source["committed_at"]),
            "continuation_source_mismatch",
        )
        events = prior.get("events")
        prefix = ["acceptance_started", "workload_image", "workload_image", "tenant_accepted"]
        expanded = prefix + ["management_ready", "data_applied"] + ["workload_image"] * 2
        expanded += ["data_api_permissions", "workload_image", "workload_image"]
        require(
            isinstance(events, list)
            and all(isinstance(event, dict) for event in events)
            and [event.get("type") for event in events]
            in (
                [prefix, expanded]
                if self.configuration.live
                else [
                    prefix,
                    prefix + ["management_ready", "data_applied"] + ["workload_image"] * 4,
                ]
            )
            and [(event.get("slot"), event.get("component")) for event in events[1:3]]
            == [("management", "management-api"), ("management", "provisioner")],
            "continuation_progress_refused",
        )
        observed = started
        for event in events:
            at = report_time(event.get("at"))
            require(observed <= at <= finished, "continuation_event_timestamp_invalid")
            observed = at
        admission = events[3]
        require(
            set(admission)
            == {"type", "at", "tenant", "http_status", "operation_id", "busy_verified"}
            and admission["tenant"] == self.names[0]
            and type(admission["http_status"]) is int
            and admission["http_status"] == 202
            and admission["busy_verified"] is True
            and isinstance(admission["operation_id"], str)
            and str(UUID(admission["operation_id"])) == admission["operation_id"],
            "continuation_admission_invalid",
        )
        if len(events) > 4:
            ready, applied = events[4:6]
            require(
                ready.get("tenant") == admission["tenant"]
                and ready.get("pair_id") == "shared"
                and ready.get("operation_id") == admission["operation_id"]
                and applied.get("tenant") == admission["tenant"]
                and type(applied.get("version")) is int
                and applied["version"] == 1
                and [
                    (event.get("slot"), event.get("component"))
                    for event in events[6:]
                    if event["type"] == "workload_image"
                ]
                == [
                    ("shared-control", "control-api"),
                    ("shared-control", "control-reconciler"),
                    ("shared-data", "data-api"),
                    ("shared-data", "data-reconciler"),
                ]
                and all(
                    isinstance(event.get("pod_uid"), str)
                    and str(UUID(event["pod_uid"])) == event["pod_uid"]
                    for event in events[6:]
                ),
                "continuation_read_only_progress_invalid",
            )
            if self.configuration.live:
                permissions = events[8]
                require(
                    permissions.get("slot") == "shared-data"
                    and permissions.get("service_account") == "data-api-runtime"
                    and permissions.get("permissions") == data_api_permissions(self.names)
                    and permissions.get("parent_secret_get_status") == 403
                    and permissions.get("secret_list_status") == 403,
                    "continuation_data_api_permissions_invalid",
                )
        if prior_journal is not None:
            prior_journal.claim_continuation(self.run_id)
        self.record["continued_first_from"] = {
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "run_id": prior["run_id"],
            "source": source,
            "started_at": prior["started_at"],
            "finished_at": finished.isoformat(),
            "admission": admission,
        }

    def save(self, event: str, **details):
        self.record["events"].append({"type": event, "at": faults.utc_now(), **details})
        self.persist()

    def persist(self):
        if self.configuration.live:
            if self.journal is None:
                self.journal = faults.ConfigMapJournal(
                    self.configuration.kube(self.configuration.target("management")),
                    "plane-demo-acceptance-" + self.run_id,
                    "acceptance",
                )
                self.journal.start(self.record)
                self.record["journal"] = self.journal.reference
            else:
                self.journal.save(self.record)
        else:
            faults.protected_write(self.path, self.record)

    def check_journal(self):
        if self.configuration.live:
            require(self.journal is not None, "acceptance_journal_not_committed")
            self.journal.check()

    def kube(self, slot: str):
        kube = self.kube_factory(self.configuration.target(slot))
        kube.verify_scope()
        return kube

    def client(self, target: str, *, timeout=30):
        def exported():
            try:
                client = self.apis.client(target)
                if self.configuration.live and not client.ready():
                    return None
                return client
            except AcceptanceError as error:
                if str(error) not in {"endpoint_not_exported", "state_file_missing"}:
                    raise
                return None

        return until(exported, timeout=timeout, clock=self.clock, sleep=self.sleep)[0]

    def wait_initial_exports(self, pair: str):
        deadline = self.clock() + 300
        try:
            for role in ("control", "data"):
                self.client(f"{role}:{pair}", timeout=max(0, deadline - self.clock()))
        except AcceptanceError as error:
            if str(error) != "convergence_deadline_exceeded":
                raise
            raise AcceptanceError("initial_export_discovery_timeout") from None

    def wait_ready(self, tenant: str):
        management = self.client("management")
        timeout = self.configuration.current().get("onboarding_timeout_seconds", 3600)
        require(isinstance(timeout, int) and 30 <= timeout <= 7200, "invalid_onboarding_timeout")

        def ready():
            value = management.get(tenant)
            require(
                value.get("provisioning_status") not in {"failed", "interrupted"},
                "onboarding_operation_failed",
            )
            require(
                "data_config" not in value and "redis_health" not in value,
                "management_reported_transitive_health",
            )
            if value.get("onboarding_status") == "ready":
                require(
                    value.get("control_record", {}).get("status") == "created",
                    "ready_without_control_report",
                )
                require(
                    value["control_record"].get("observed_revision") == 1,
                    "unexpected_control_revision",
                )
                return value
            return None

        value, elapsed = until(ready, timeout=timeout, clock=self.clock, sleep=self.sleep)
        self.tenants[tenant] = value
        self.save(
            "management_ready",
            tenant=tenant,
            pair_id=value["pair_id"],
            operation_id=value["operation_id"],
            elapsed_seconds=elapsed,
            reported_at=value["control_record"]["reported_at"],
        )
        return value

    def onboard(self, tenant: str, isolation: str, *, check_busy=False):
        management = self.client("management")
        management.request("GET", f"/tenants/{tenant}", statuses=(404,))
        body = {
            "tenant_id": tenant,
            "isolation": isolation,
            "initial_message": f"acceptance-{self.run_id[:8]}-{tenant}",
        }
        started = self.clock()
        status, accepted, _ = management.request("POST", "/tenants", body=body, statuses=(202,))
        require(self.clock() - started <= 5, "onboarding_acceptance_not_prompt")
        UUID(accepted["operation_id"])
        self.expectations[tenant] = {"version": 1, "message": body["initial_message"]}
        require(accepted.get("status_url") == f"/tenants/{tenant}", "unexpected_tenant_status_url")
        _, duplicate, headers = management.request("POST", "/tenants", body=body, statuses=(409,))
        require(
            duplicate.get("status_url") == accepted["status_url"]
            and headers.get("Location") == accepted["status_url"],
            "duplicate_missing_original_status_url",
        )
        if check_busy:
            operation = management.request("GET", f"/operations/{accepted['operation_id']}")[1]
            require(operation.get("status") in {"pending", "running"}, "busy_window_not_observed")
            sentinel = "busy-" + self.run_id[:12]
            _, busy, headers = management.request(
                "POST", "/tenants", body={**body, "tenant_id": sentinel}, statuses=(503,)
            )
            require(
                busy.get("detail") == "provisioner_busy"
                and headers.get("Retry-After", "").isdigit()
                and 1 <= int(headers["Retry-After"]) <= 60,
                "busy_contract_failed",
            )
            management.request("GET", f"/tenants/{sentinel}", statuses=(404,))
        self.save(
            "tenant_accepted",
            tenant=tenant,
            http_status=status,
            operation_id=accepted["operation_id"],
            busy_verified=check_busy,
        )
        ready = self.wait_ready(tenant)
        self.wait_operation(accepted["operation_id"])
        management.request("POST", "/tenants", body=body, statuses=(409,))
        return ready

    def wait_operation(self, operation_id):
        management = self.client("management")

        def completed():
            operation = management.request("GET", f"/operations/{operation_id}")[1]
            require(
                operation.get("status") not in {"failed", "interrupted"},
                "onboarding_operation_failed",
            )
            return operation.get("status") == "succeeded"

        until(completed, timeout=30, clock=self.clock, sleep=self.sleep)

    def continue_first(self):
        prior = self.record["continued_first_from"]
        tenant, operation_id = self.names[0], prior["admission"]["operation_id"]

        def check(value):
            require(
                value.get("tenant_id") == tenant
                and value.get("operation_id") == operation_id
                and value.get("pair_id") == "shared"
                and value.get("isolation") == "shared",
                "continuation_tenant_mismatch",
            )
            require(
                value.get("provisioning_status") in {"pending", "running", "succeeded"}
                and value.get("onboarding_status") in {"pending", "ready"}
                and value.get("control_record", {}).get("status") != "failed",
                "onboarding_operation_failed",
            )

        check(self.client("management").get(tenant))
        self.expectations[tenant] = {
            "version": 1,
            "message": f"acceptance-{prior['run_id'][:8]}-{tenant}",
        }
        self.save("first_tenant_continued", tenant=tenant, operation_id=operation_id)
        ready = self.wait_ready(tenant)
        check(ready)
        self.wait_operation(operation_id)
        return ready

    def applied(
        self,
        tenant: str,
        *,
        version: int | None = None,
        message: str | None = None,
        counter: int | None = None,
        timeout=30,
        deadline: float | None = None,
    ):
        pair = self.tenants[tenant]["pair_id"]
        control, data = self.client("control:" + pair), self.client("data:" + pair)
        onboarding = str(self.tenants[tenant]["onboarding_id"])

        def check():
            desired = control.get(tenant)
            wanted = desired["desired"]["version"] if version is None else version
            require(desired.get("onboarding_id") == onboarding, "control_onboarding_mismatch")
            require(desired["desired"]["version"] == wanted, "desired_version_mismatch")
            expected = self.expectations.get(tenant, {})
            wanted_message = message
            if wanted_message is None and expected.get("version") == wanted:
                wanted_message = expected["message"]
            if wanted_message is not None:
                require(
                    desired["desired"]["message"] == wanted_message,
                    "requested_configuration_message_not_persisted",
                )
            code, actual, _ = data.request("GET", f"/tenants/{tenant}", statuses=(200, 404))
            if code == 404:
                return None
            if actual.get("applied_version") != wanted:
                return None
            if desired.get("data_config", {}).get("status") != "applied":
                return None
            applied_summary(desired, onboarding, wanted)
            require(
                actual.get("onboarding_id") == onboarding,
                "data_onboarding_mismatch",
            )
            require(
                actual.get("message") == desired["desired"]["message"], "applied_message_mismatch"
            )
            if counter is not None:
                require(actual.get("counter") == counter, "counter_changed_during_configuration")
            return actual

        value, elapsed = until(
            check, timeout=timeout, deadline=deadline, clock=self.clock, sleep=self.sleep
        )
        observed = self.clock()
        self.save(
            "data_applied",
            tenant=tenant,
            version=value["applied_version"],
            elapsed_seconds=elapsed,
            recovery_elapsed_seconds=(
                observed - (deadline - faults.RECOVERY_SECONDS) if deadline is not None else None
            ),
            observed_monotonic=observed,
        )
        return value

    def verify_workload(self, kube, component, *, pod=None):
        pod = pod or kube.pod(component)
        container = kube.target.component(component)["container"]
        role = "provisioner" if component == "provisioner" else "api"
        if self.configuration.live:
            self.configuration.current()
            specs = [
                item
                for item in pod.get("spec", {}).get("containers", [])
                if item["name"] == container
            ]
            require(len(specs) == 1, "configured_container_missing")
            candidate = specs[0].get("image", "")
            if self.configuration.environment == "azure":
                registry = self.configuration.config.registry_name + ".azurecr.io/"
                require(
                    isinstance(candidate, str)
                    and candidate.startswith(registry)
                    and re.fullmatch(r"[a-z0-9./_-]+@sha256:[a-f0-9]{64}", candidate),
                    "configured_image_digest_missing",
                )
                self.images.setdefault(role, candidate)
                expected_image = self.images[role]
            else:
                revision = self.record["source"]["commit"]
                expected_image = f"localhost/{self.configuration.config.stem}-{role}:{revision}"
        else:
            expected_image = self.configuration.current().get("images", {}).get(role)
        if not self.configuration.live and self.configuration.environment == "azure":
            require(
                isinstance(expected_image, str)
                and re.fullmatch(r"[a-z0-9./_-]+@sha256:[a-f0-9]{64}", expected_image),
                "configured_image_digest_missing",
            )
        elif not self.configuration.live:
            review = self.configuration.current().get("local_images", {})
            source = self.record.get("source", {})
            require(
                review.get("version") == 1
                and review.get("content_verified") is True
                and review.get("source_revision") == source.get("commit")
                and source.get("worktree_dirty") is False
                and isinstance(expected_image, str)
                and expected_image == f"localhost/radplanes-plane-{role}:{source.get('commit')}"
                and review.get(role, {}).get("reference") == expected_image
                and report_time(source.get("committed_at"))
                <= report_time(review.get("inspected_at"))
                <= report_time(self.record["started_at"]),
                "local_image_review_mismatch",
            )
        specs = [
            item for item in pod.get("spec", {}).get("containers", []) if item["name"] == container
        ]
        statuses = [
            item
            for item in pod.get("status", {}).get("containerStatuses", [])
            if item["name"] == container
        ]
        require(
            len(specs) == 1 and specs[0].get("image") == expected_image,
            "workload_image_reference_mismatch",
        )
        require(
            len(statuses) == 1
            and isinstance(statuses[0].get("imageID"), str)
            and re.search(r"sha256:[a-f0-9]{64}$", statuses[0]["imageID"]),
            "running_image_digest_missing",
        )
        if self.configuration.live and self.configuration.environment == "local":
            require(
                pod["spec"].get("nodeName") == kube.target.local["node"]["name"],
                "local_workload_node_mismatch",
            )
            self.verify_live_local_image(kube, expected_image, statuses[0]["imageID"])
            expected = local_source_hashes(component)
        elif self.configuration.environment == "local":
            image = review[role]
            mapping = kube.target.local.get("image_ids", {}).get(component, {})
            require(
                re.fullmatch(r"sha256:[a-f0-9]{64}", image.get("image_id", ""))
                and mapping.get("image_id") == image["image_id"]
                and mapping.get("running_image_id") == statuses[0]["imageID"],
                "local_running_image_identity_mismatch",
            )
            expected = local_source_hashes(component)
            require(image.get("source_hashes") == expected, "local_review_source_hash_mismatch")
        else:
            expected = source_hashes(component)
        actual = kube.exec_json(component, SOURCE_PROBE, json.dumps(list(expected)))
        require(actual.get("files") == expected, "deployed_source_hash_mismatch")
        require(
            kube.pod(component)["metadata"]["uid"] == pod["metadata"]["uid"],
            "workload_replaced_during_provenance_check",
        )
        if component == "data-api":
            require(actual.get("parent_dsn_present") is False, "data_api_has_parent_dsn")
            require(
                pod["spec"].get("serviceAccountName") == "data-api-runtime",
                "data_api_runtime_identity_mismatch",
            )
            expected_permissions = data_api_permissions(self.names)
            permissions = kube.exec_json(
                component,
                DATA_API_PERMISSIONS_PROBE,
                kube.target.namespace,
                json.dumps(expected_permissions),
            )
            require(
                permissions.get("permissions") == expected_permissions
                and permissions.get("parent_secret_get_status") == 403
                and permissions.get("secret_list_status") == 403,
                "data_api_secret_boundary_not_verified",
            )
            self.save(
                "data_api_permissions",
                slot=kube.target.slot,
                pod_uid=pod["metadata"]["uid"],
                service_account="data-api-runtime",
                **permissions,
            )
        self.save(
            "workload_image",
            slot=kube.target.slot,
            component=component,
            pod_uid=pod["metadata"]["uid"],
            configured_image=expected_image,
            running_image_id=statuses[0]["imageID"],
            source_hashes=expected,
        )

    def verify_live_local_image(self, kube, reference, running):
        config = self.configuration
        docker = ["docker", "--host", kube.target.local["docker_host"]]
        values = json.loads(config.command([*docker, "image", "inspect", reference]))
        require(isinstance(values, list) and len(values) == 1, "local_image_ambiguous")
        image = values[0]
        expected = image.get("Id", "")
        require(
            re.fullmatch(r"sha256:[a-f0-9]{64}", expected)
            and image.get("Os") == "linux"
            and image.get("Architecture") in {"amd64", "arm64"}
            and image.get("Config", {}).get("User") == "10001:10001"
            and image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
            == self.record["source"]["commit"],
            "local_image_source_identity_mismatch",
        )
        node = kube.target.local["node"]["id"]

        def content(digest):
            require(
                isinstance(digest, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", digest),
                "local_containerd_content_id_invalid",
            )
            raw = config.command(
                [*docker, "exec", node, "ctr", "--namespace", "k8s.io", "content", "get", digest],
                binary=True,
            )
            require(
                isinstance(raw, bytes) and "sha256:" + hashlib.sha256(raw).hexdigest() == digest,
                "local_containerd_content_hash_mismatch",
            )
            value = json.loads(raw)
            require(isinstance(value, dict), "local_containerd_content_not_object")
            return value

        faults.verify_image_mapping(
            running,
            expected,
            image["Architecture"],
            content=content,
            inspect=lambda value: json.loads(
                config.command(
                    [
                        *docker,
                        "exec",
                        node,
                        "crictl",
                        "inspecti",
                        value.removeprefix("docker-pullable://").removeprefix("containerd://"),
                    ]
                )
            ),
        )

    def workload_evidence(self, pair: str):
        instances = {}
        for suffix, components in (
            ("control", ("control-api", "control-reconciler")),
            ("data", ("data-api", "data-reconciler")),
        ):
            kube = self.kube(pair + "-" + suffix)
            for component in components:
                self.verify_workload(kube, component)
            role = suffix + "-api"
            arguments = [role]
            if self.configuration.environment == "local":
                arguments.append("local")
            instances[suffix] = kube.exec_json(role, IDENTITY_PROBE, *arguments)
            if suffix == "data" and self.configuration.environment == "azure":
                require(instances[suffix]["redis"]["tls"] is True, "azure_redis_tls_not_enabled")
            elif self.configuration.environment == "local":
                identity = instances[suffix]["redis" if suffix == "data" else "postgresql"]
                expected_host = (
                    f"redis.{kube.target.namespace}.svc.cluster.local"
                    if suffix == "data"
                    else kube.target.local.get("node", {}).get("address")
                )
                require(
                    identity.get("tls") is False
                    and identity.get("port") == (6379 if suffix == "data" else 31543)
                    and isinstance(expected_host, str)
                    and bool(expected_host)
                    and identity.get("host") == expected_host,
                    "local_datastore_endpoint_mismatch",
                )
                if suffix == "data":
                    require(
                        identity.get("authenticated") is True
                        and identity.get("wrong_password_rejected") is True,
                        "local_redis_authentication_not_verified",
                    )
            instances[suffix]["cluster_uid"] = kube.target.cluster_uid
        return instances

    def management_image(self):
        kube = self.kube("management")
        self.verify_workload(kube, "management-api")
        self.verify_workload(kube, "provisioner")

    def pair_inventory(self):
        result = {}
        for pair in sorted({value["pair_id"] for value in self.tenants.values()}):
            require(pair in {"shared", "isolated-1"}, "unexpected_pair_assignment")
            result[pair] = {"pair_id": pair}
            for role in ("control", "data"):
                target = self.kube(pair + "-" + role).target
                require(bool(target.cluster_id), "live_cluster_identity_missing")
                result[pair][role + "_cluster_id"] = target.cluster_id
                result[pair][role + "_url"] = self.client(role + ":" + pair).url
        return result

    def scenario(self):
        first, second, isolated = self.names
        management = self.client("management")
        for name in self.names[1:] if self.continue_first_from else self.names:
            management.request("GET", f"/tenants/{name}", statuses=(404,))
        first_status = (
            self.continue_first()
            if self.continue_first_from
            else self.onboard(first, "shared", check_busy=True)
        )
        require(first_status["pair_id"] == "shared", "shared_pair_assignment_changed")
        self.wait_initial_exports("shared")
        self.applied(first, **(self.expectations[first] if self.continue_first_from else {}))
        initial_inventory = self.pair_inventory()["shared"]
        shared_instances = self.workload_evidence("shared")

        self.check_journal()
        with paused_reconciler(
            self.kube("shared-data"), clock=self.clock, sleep=self.sleep
        ) as pause:
            self.save("data_reconciler_paused", pause=pause)
            second_status = self.onboard(second, "shared")
            require(second_status["pair_id"] == "shared", "shared_pair_not_reused")
            control = self.client("control:shared").get(second)
            require(
                control["data_config"]["status"] == "pending"
                and control["data_config"]["last_applied_version"] is None,
                "data_should_be_pending_while_paused",
            )
            self.client("data:shared").request("GET", f"/tenants/{second}", statuses=(404,))
            self.save("immediate_child_readiness_proved", tenant=second, pause=pause)
        self.applied(second)
        require(
            self.pair_inventory()["shared"] == initial_inventory,
            "shared_infrastructure_inventory_changed",
        )
        require(
            self.workload_evidence("shared") == shared_instances,
            "shared_cluster_or_datastore_changed",
        )
        self.check_shared_pair()

        isolated_status = self.onboard(isolated, "isolated")
        isolated_pair = isolated_status["pair_id"]
        require(isolated_pair != "shared", "isolated_pair_not_dedicated")
        self.wait_initial_exports(isolated_pair)
        self.applied(isolated)
        isolated_instances = self.workload_evidence(isolated_pair)
        self.check_pair_isolation(isolated_pair, shared_instances, isolated_instances)
        self.check_configuration_and_idempotency()

    def verify_existing(self):
        for tenant in self.names:
            value = self.wait_ready(tenant)
            require(
                value.get("tenant_id") == tenant
                and value.get("isolation") == ("isolated" if tenant == self.names[2] else "shared")
                and isinstance(value.get("operation_id"), str)
                and str(UUID(value["operation_id"])) == value["operation_id"],
                "existing_tenant_identity_mismatch",
            )
            self.wait_operation(value["operation_id"])
        self.check_shared_pair()
        isolated_pair = self.tenants[self.names[2]]["pair_id"]
        require(isolated_pair and isolated_pair != "shared", "isolated_pair_not_dedicated")
        for pair in ("shared", isolated_pair):
            self.wait_initial_exports(pair)
        for tenant in self.names:
            self.applied(tenant)
        shared_instances = self.workload_evidence("shared")
        isolated_instances = self.workload_evidence(isolated_pair)
        self.check_pair_isolation(isolated_pair, shared_instances, isolated_instances)
        self.check_configuration_and_idempotency()

    def check_shared_pair(self):
        first, second = [self.tenants[tenant] for tenant in self.names[:2]]
        require(first["pair_id"] == second["pair_id"] == "shared", "shared_pair_assignment_changed")
        if self.configuration.live:
            for role in ("control", "data"):
                url = self.client(role + ":shared").url
                discovered, _key = self.configuration.endpoint(role + ":shared")
                require(url == discovered, "shared_urls_changed")
            return
        require(
            all(
                isinstance(first.get(key), str) and first[key] and first[key] == second.get(key)
                for key in ("control_url", "data_url")
            ),
            "shared_urls_changed",
        )

    def check_pair_isolation(self, isolated_pair, shared_instances, isolated_instances):
        inventory = self.pair_inventory()
        cluster_ids = [
            inventory[pair][key]
            for pair in ("shared", isolated_pair)
            for key in ("control_cluster_id", "data_cluster_id")
        ]
        require(all(cluster_ids) and len(set(cluster_ids)) == 4, "cluster_ids_not_dedicated")
        if self.configuration.environment == "local":
            stem = self.configuration.config.stem if self.configuration.live else "radplanes-local"
            require(
                isolated_pair == "isolated-1"
                and cluster_ids
                == [
                    f"kind://{stem}-{pair}-{role}"
                    for pair in ("shared", "isolated-1")
                    for role in ("control", "data")
                ],
                "local_cluster_inventory_mismatch",
            )
        cluster_uids = [self.configuration.target("management").cluster_uid] + [
            instances[role]["cluster_uid"]
            for instances in (shared_instances, isolated_instances)
            for role in ("control", "data")
        ]
        require(len(set(cluster_uids)) == 5, "actual_clusters_not_dedicated")
        for role, key in (("control", "postgresql"), ("data", "redis")):
            require(
                shared_instances[role][key]["host"] != isolated_instances[role][key]["host"],
                "isolated_datastore_endpoint_reused",
            )
        self.save(
            "pair_isolation",
            inventory=inventory,
            shared=shared_instances,
            isolated=isolated_instances,
            cluster_uids=cluster_uids,
        )

    def check_configuration_and_idempotency(self):
        self.check_updates_and_counters()
        before = self.collect_timelines()
        self.sleep(15)
        self.assert_updates_survived_poll()
        require(self.collect_timelines() == before, "unchanged_polls_added_events")
        self.management_image()
        self.save("three_poll_intervals_idempotent", seconds=15)

    def check_updates_and_counters(self):
        first, second, _isolated = self.names
        shared = self.client("data:shared")
        other_before = shared.get(second)["counter"]
        before = shared.get(first)["counter"]
        for offset in range(1, 4):
            actual = shared.request("POST", f"/tenants/{first}/counter")[1]
            require(actual["counter"] == before + offset, "counter_increment_not_atomic")
        require(shared.get(second)["counter"] == other_before, "shared_counter_scope_broken")
        for tenant in self.names:
            pair = self.tenants[tenant]["pair_id"]
            control = self.client("control:" + pair)
            data = self.client("data:" + pair)
            counter = data.get(tenant)["counter"]
            counter += 1
            seeded = data.request("POST", f"/tenants/{tenant}/counter")[1]
            require(seeded["counter"] == counter, "counter_increment_not_atomic")
            original = control.get(tenant)["desired"]["version"]
            for offset in (1, 2):
                requested = f"acceptance-update-{self.run_id[:8]}-{tenant}-{offset}"
                result = control.request(
                    "PUT",
                    f"/tenants/{tenant}/configuration",
                    body={"message": requested},
                )[1]
                require(
                    result["desired"]["version"] == original + offset,
                    "configuration_version_not_monotonic",
                )
                require(
                    result["desired"].get("message") == requested,
                    "configuration_update_response_ignored_message",
                )
            self.expectations[tenant] = {
                "version": original + 2,
                "message": requested,
                "counter": counter,
            }
            self.applied(tenant, **self.expectations[tenant])
        self.updates_finished_at = report_time(faults.utc_now())
        for target in self.apis.clients:
            client = self.client(target)
            tenant = (
                first
                if target in {"management", "control:shared", "data:shared"}
                else self.names[2]
            )
            client.request("GET", f"/tenants/{tenant}", key="", statuses=(401,))
            client.request("GET", f"/tenants/{tenant}", key="wrong", statuses=(401,))
            require(
                client.request("GET", "/healthz", key="")[1] == {"status": "ok"},
                "health_response_not_minimal",
            )
            client.request("GET", "/api/container-info", statuses=(404,))
        self.client("management").request(
            "GET", f"/tenants/{first}", key=self.client("control:shared").key, statuses=(401,)
        )
        self.save("configuration_counter_and_auth_checks", tenants=self.names)

    def control_poll_observed(self, pair, since):
        kube = self.kube(pair + "-control")
        pod = kube.pod("control-reconciler")
        output = kube.run(
            "logs",
            pod["metadata"]["name"],
            "-c",
            kube.target.component("control-reconciler")["container"],
            "--since=40s",
            "--tail=100",
            "--timestamps=true",
        )
        for line in output.splitlines():
            if re.search(r"control_poll examined=\d+ succeeded=([1-9]\d*) failed=0", line):
                if report_time(line.split(" ", 1)[0]) >= since:
                    return True
        return False

    def assert_updates_survived_poll(self):
        require(self.updates_finished_at is not None, "configuration_updates_not_exercised")
        for pair in {self.tenants[name]["pair_id"] for name in self.names}:
            require(
                self.control_poll_observed(pair, self.updates_finished_at),
                "management_poll_not_observed_after_updates",
            )
        for tenant in self.names:
            expected = self.expectations[tenant]
            pair = self.tenants[tenant]["pair_id"]
            control = self.client("control:" + pair).get(tenant)
            data = self.client("data:" + pair).get(tenant)
            onboarding = str(self.tenants[tenant]["onboarding_id"])
            applied_summary(control, onboarding, expected["version"])
            require(
                control["desired"]["message"] == expected["message"]
                and data.get("message") == expected["message"]
                and data.get("applied_version") == expected["version"]
                and data.get("onboarding_id") == onboarding,
                "configuration_changed_after_management_poll",
            )
            require(
                data.get("counter") == expected["counter"], "counter_changed_after_management_poll"
            )
        self.save("requested_messages_and_counters_survived_management_poll")

    def collect_timelines(self):
        result = {}
        for tenant in self.names:
            pair = self.tenants[tenant]["pair_id"]
            management = timeline(self.client("management"), tenant, "management")
            control = timeline(self.client("control:" + pair), tenant, "control")
            require(
                sum(row["type"] == "tenant_requested" for row in management) == 1,
                "tenant_requested_event_count",
            )
            require(
                sum(row["type"] == "control_record_created" for row in management) == 1,
                "control_record_event_count",
            )
            current = self.client("control:" + pair).get(tenant)
            control_history(control, current, str(self.tenants[tenant]["onboarding_id"]))
            result[tenant] = {"management": management, "control": control}
        self.save("complete_paginated_timelines", timelines=result)
        return result

    def restart_data_api(self, pair: str, tenant: str, baseline: dict):
        kube = self.kube(pair + "-data")
        deployment = kube.deployment("data-api")
        pod = kube.pod("data-api")
        old_uid = pod["metadata"]["uid"]
        started = self.clock()
        kube.delete_uid("pods", pod["metadata"]["name"], old_uid)
        replicas(kube, "data-api", 1, uid=deployment["metadata"]["uid"], expected=1)

        def replaced():
            try:
                new = kube.pod("data-api")
            except AcceptanceError as error:
                if str(error) in {"expected_one_scoped_pod", "pod_not_running"}:
                    return None
                raise
            if new["metadata"]["uid"] == old_uid:
                return None
            try:
                current = self.client("data:" + pair).get(tenant)
            except AcceptanceError as error:
                if str(error) in {"unexpected_api_status", "api_transport_failed"}:
                    return None
                raise
            require(
                current["applied_version"] == baseline["applied_version"]
                and current["onboarding_id"] == baseline["onboarding_id"]
                and current["counter"] == baseline["counter"],
                "data_state_lost_after_api_restart",
            )
            return new

        new, _ = until(
            replaced,
            timeout=30 - (self.clock() - started),
            clock=self.clock,
            sleep=self.sleep,
        )
        recovered_seconds = self.clock() - started
        self.verify_workload(kube, "data-api", pod=new)
        self.save(
            "data_api_restarted_without_parent",
            old_pod_uid=old_uid,
            new_pod_uid=new["metadata"]["uid"],
            recovery_seconds=recovered_seconds,
        )

    def continuity(self, fault, tenant: str, version: int, *, restart=False):
        pair = self.tenants[tenant]["pair_id"]
        data = self.client("data:" + pair)
        start = self.clock()
        baseline = data.get(tenant)
        previous = baseline["counter"]
        samples = []
        for index in range(10):
            due = start + 60 * index / 9
            if self.clock() < due:
                self.sleep(due - self.clock())
            if index in {0, 5, 9}:
                fault.assert_blocked()
            actual = data.request("POST", f"/tenants/{tenant}/counter")[1]
            require(
                actual["applied_version"] == version
                and actual["counter"] == previous + 1
                and actual["message"] == baseline["message"]
                and actual["onboarding_id"] == baseline["onboarding_id"],
                "data_continuity_failed",
            )
            previous = actual["counter"]
            samples.append({"at": faults.utc_now(), "counter": previous, "version": version})
            if restart and index == 4:
                self.restart_data_api(pair, tenant, actual)
        require(self.clock() - start >= 60 and len(samples) >= 10, "insufficient_outage_duration")
        self.save(
            "data_continuity",
            component=fault.component,
            tenant=tenant,
            samples=samples,
            duration_seconds=self.clock() - start,
            restarted=restart,
        )
        return actual

    def management_outage(self):
        tenant = self.names[0]
        require(self.tenants[tenant]["pair_id"] == "shared", "fault_tenant_not_in_shared_pair")
        baseline = self.applied(tenant)
        control = self.client("control:shared")
        before = timeline(self.client("management"), tenant, "management")
        evidence = self.configuration.root / "evidence" / f"{self.run_id}-management-link.json"
        self.check_journal()
        fault = self.fault_factory(
            self.configuration, "shared-control", "control-reconciler", evidence
        )
        fault.record["acceptance_run_id"] = self.run_id
        fault.record["source"] = self.record.get("source")
        with fault:
            requested = "management-link-outage-" + self.run_id[:8]
            result = control.request(
                "PUT",
                f"/tenants/{tenant}/configuration",
                body={"message": requested},
            )[1]["desired"]
            desired = result["version"]
            require(
                desired == baseline["applied_version"] + 1 and result.get("message") == requested,
                "outage_configuration_update_mismatch",
            )
            self.applied(tenant, version=desired, message=requested, counter=baseline["counter"])
            self.continuity(fault, tenant, desired)
            require(
                timeline(self.client("management"), tenant, "management") == before,
                "parent_reports_changed_while_link_blocked",
            )
        released = report_time(fault.record["restoration_started_at"])
        until(
            lambda: self.control_poll_observed("shared", released),
            timeout=30,
            deadline=fault.recovery_deadline,
            clock=self.clock,
            sleep=self.sleep,
        )
        elapsed = self.clock() - fault.recovery_started
        require(
            timeline(self.client("management"), tenant, "management") == before,
            "report_recovery_duplicated_events",
        )
        self.save(
            "management_link_recovered",
            evidence=fault.journal.reference if self.configuration.live else str(evidence),
            reporting_recovered_seconds=elapsed,
        )

    def control_outage(self):
        tenant = self.names[0]
        require(self.tenants[tenant]["pair_id"] == "shared", "fault_tenant_not_in_shared_pair")
        baseline = self.applied(tenant)
        control = self.client("control:shared")
        evidence = self.configuration.root / "evidence" / f"{self.run_id}-control-link.json"
        self.check_journal()
        fault = self.fault_factory(self.configuration, "shared-data", "data-reconciler", evidence)
        fault.record["acceptance_run_id"] = self.run_id
        fault.record["source"] = self.record.get("source")
        with fault:
            for offset in (1, 2):
                requested = f"control-link-outage-{self.run_id[:8]}-{offset}"
                updated = control.request(
                    "PUT",
                    f"/tenants/{tenant}/configuration",
                    body={"message": requested},
                )[1]
                require(
                    updated["desired"]["version"] == baseline["applied_version"] + offset,
                    "outage_update_version_mismatch",
                )
                require(
                    updated["desired"].get("message") == requested,
                    "outage_configuration_message_ignored",
                )
            continued = self.continuity(fault, tenant, baseline["applied_version"], restart=True)
            require(
                control.get(tenant)["data_config"]["status"] == "pending",
                "control_fabricated_current_application",
            )
        self.applied(
            tenant,
            version=baseline["applied_version"] + 2,
            message=requested,
            counter=continued["counter"],
            deadline=fault.recovery_deadline,
        )
        rows = timeline(control, tenant, "control")
        require(
            not any(
                row["type"] == "config_applied"
                and row["version"] == baseline["applied_version"] + 1
                for row in rows
            ),
            "unexpected_intermediate_version_replay",
        )
        self.save(
            "control_link_recovered_latest_only",
            evidence=fault.journal.reference if self.configuration.live else str(evidence),
        )

    def run(self):
        try:
            require(
                not faults.command(
                    [
                        "git",
                        "status",
                        "--porcelain",
                        "--",
                        "src/plane_demo",
                        "sql",
                        "infra/radius/apps",
                        "infra/radius/modules",
                        "scripts/harness/test-e2e.py",
                        "scripts/harness/fault-parent-link.py",
                        "scripts/harness/export-state.py",
                        "scripts/harness/local",
                        "scripts/lib",
                        "scripts/operations/api.sh",
                        "scripts/operations/endpoints.sh",
                        "images/api",
                        "images/provisioner",
                        "pyproject.toml",
                        "uv.lock",
                        *(
                            [
                                "scripts/operations/local",
                                "scripts/recipes/local",
                                "infra/radius",
                                "images/local-provisioner",
                                ".dockerignore",
                            ]
                            if self.configuration.environment == "local"
                            else []
                        ),
                        *source_files("provisioner"),
                    ]
                ).strip(),
                "acceptance_source_worktree_dirty",
            )
            self.record["source"] = faults.source_metadata()
            if self.configuration.live:
                require(
                    self.configuration.config.revision in (None, self.record["source"]["commit"]),
                    "selected_source_revision_mismatch",
                )
                self.record["report_storage"] = "configmap+stdout"
            if self.continue_first_from is not None:
                self.load_first_admission()
            self.record["outcome"] = "running"
            self.save("acceptance_started")
            self.management_image()
            if self.mode in {"scenario", "all"}:
                self.scenario()
            elif self.mode == "verify-existing":
                self.verify_existing()
            else:
                for tenant in self.names:
                    self.wait_ready(tenant)
                    self.applied(tenant)
                for pair in {value["pair_id"] for value in self.tenants.values()}:
                    self.workload_evidence(pair)
            if self.mode in {"outages", "all", "verify-existing"}:
                self.management_outage()
                self.control_outage()
                self.collect_timelines()
            self.record["outcome"] = "passed"
        except Exception as error:
            self.record["outcome"] = "failed"
            self.record["error"] = (
                str(error) if isinstance(error, AcceptanceError) else "acceptance_failed"
            )
            raise
        finally:
            self.record["finished_at"] = faults.utc_now()
            try:
                if not self.configuration.live or self.journal is not None:
                    self.persist()
            except AcceptanceError as error:
                self.record["outcome"] = "failed"
                self.record["error"] = str(error)
                raise
            finally:
                self.apis.close()


def main(argv=None, *, configuration_factory=faults.LiveConfiguration) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".env", help="Checkout .env only")
    parser.add_argument(
        "--environment",
        choices=("azure", "local"),
        help="Require this environment in the loaded .env",
    )
    parser.add_argument(
        "--mode",
        choices=("scenario", "outages", "all", "verify-existing"),
        default="all",
        help="Run admissions, outage checks, both, or verification of existing synthetic tenants.",
    )
    parser.add_argument(
        "--continue-first-from",
        help="Resume only the first admission from its owned acceptance ConfigMap NAME@UID@RUN_ID",
    )
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    runner = None
    configuration = None
    try:
        configuration = configuration_factory(args.config)
        require(
            args.environment is None or configuration.environment == args.environment,
            "harness_environment_mismatch",
        )
        runner = Runner(configuration, args.mode, continue_first_from=args.continue_first_from)
        with faults.interruption_is_failure():
            runner.run()
        print(
            json.dumps(
                runner.record
                if configuration.live
                else {"outcome": "passed", "mode": args.mode, "evidence": str(runner.path)}
            )
        )
        return 0
    except Exception as error:
        if configuration is not None and configuration.live:
            record = (
                runner.record
                if runner
                else {
                    "outcome": "failed",
                    "error": str(error) if isinstance(error, AcceptanceError) else "invalid_config",
                }
            )
            print(json.dumps(record))
            return 1
        print(
            json.dumps(
                {
                    "outcome": "failed",
                    "evidence": str(runner.path) if runner else None,
                    "error": runner.record.get("error", "acceptance_failed")
                    if runner
                    else str(error)
                    if isinstance(error, AcceptanceError)
                    else "invalid_config",
                }
            )
        )
        return 1
    finally:
        if configuration is not None and configuration.live:
            configuration.close()


if __name__ == "__main__":
    raise SystemExit(main())
