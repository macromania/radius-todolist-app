import base64
import importlib.util
import json
import os
import ssl
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import NAMESPACE_DNS, uuid5

import httpx
import pytest
from test_acceptance import faults
from test_acceptance import runner_module as runner
from test_export_state import export_module
from test_first_continuation import HarnessRunCase

from scripts.operations.config import DemoConfig, initialize_config

REVISION = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64
SOURCE = {"commit": REVISION, "committed_at": "2026-09-14T00:00:00+00:00", "worktree_dirty": False}
KEY = "$(id)" + "x" * 40


def uid(name):
    return str(uuid5(NAMESPACE_DNS, name))


class NativeCommands:
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.calls = []
        self.key = None
        self.mutate = lambda value: None
        self.returncode = 0
        self.image_id = IMAGE_ID
        self.running_id = IMAGE_ID
        self.journals = {}
        self.version = 0

    def metadata(self, name):
        return {
            "name": name,
            "uid": uid(name),
            "labels": {
                "plane-demo/project": self.config.project,
                "plane-demo/deployment": self.config.deployment,
                "plane-demo/environment": self.config.environment,
            },
        }

    def __call__(self, argv, **options):
        self.calls.append((argv, options))
        assert options["cwd"] == self.root
        assert str(self.root / ".state") not in json.dumps(argv)
        if self.returncode:
            return subprocess.CompletedProcess(argv, self.returncode, "", "sensitive-native-error")
        if argv[:2] == ["bash", "-c"]:
            assert options["env"]["PLANE_DEMO_EXPECT_ENV"] == self.config.environment
            assert argv[2] == faults.DISCOVER_SLOT
            assert argv[-3] == str(self.root)
            work, slot = Path(argv[-2]), argv[-1]
            context = self.config.slot_name(slot)
            profile = work / slot / "kubeconfig"
            profile.parent.mkdir(mode=0o700)
            profile.write_text(
                json.dumps(
                    {
                        "current-context": context,
                        "contexts": [
                            {"name": context, "context": {"cluster": context, "user": context}}
                        ],
                        "clusters": [
                            {
                                "name": context,
                                "cluster": {
                                    "server": "https://cluster.example.test",
                                    "certificate-authority-data": "Y2E=",
                                },
                            }
                        ],
                        "users": [{"name": context, "user": {}}],
                    }
                )
            )
            profile.chmod(0o600)
            index = faults.LOCAL_SLOTS.index(slot)
            node = {
                "Id": str(index + 1) * 64,
                "Name": "/" + context + "-control-plane",
                "Config": {
                    "Labels": {
                        "io.x-k8s.kind.cluster": context,
                        "io.x-k8s.kind.role": "control-plane",
                    }
                },
                "State": {"Running": True},
                "NetworkSettings": {"Networks": {"kind": {"IPAddress": f"172.18.0.{index + 2}"}}},
            }
            value = {
                "slot": slot,
                "project": self.config.project,
                "deployment": self.config.deployment,
                "environment": self.config.environment,
                "context": context,
                "kubeconfig": str(profile),
                "namespace": {"metadata": self.metadata(self.config.namespace(slot))},
                "cluster": {"metadata": {**self.metadata("kube-system"), "uid": uid(context)}},
                "node": [node],
                "docker_host": "unix:///synthetic/docker.sock",
                "url": (
                    f"http://127.0.0.1:{35490 + index}"
                    if self.config.environment == "local"
                    else f"https://{context}.centralus.cloudapp.azure.com"
                ),
            }
            self.mutate(value)
        elif argv[0] == "kubectl":
            namespace = argv[argv.index("--namespace") + 1]
            action = next(word for word in argv if word in {"get", "create", "replace"})
            if action in {"create", "replace"}:
                value = json.loads(options["input"])
                metadata = value["metadata"]
                key = (namespace, metadata["name"])
                previous = self.journals.get(key)
                valid = (
                    previous is None
                    if action == "create"
                    else (
                        previous is not None
                        and metadata.get("uid") == previous["metadata"]["uid"]
                        and metadata.get("resourceVersion")
                        == previous["metadata"]["resourceVersion"]
                    )
                )
                if not valid:
                    return subprocess.CompletedProcess(argv, 1, "", "conflict")
                self.version += 1
                metadata["uid"] = (
                    uid(namespace + metadata["name"] + str(self.version))
                    if previous is None
                    else previous["metadata"]["uid"]
                )
                metadata["resourceVersion"] = str(self.version)
                self.journals[key] = json.loads(json.dumps(value))
                return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
            offset = argv.index("get")
            kind, name = argv[offset + 1 : offset + 3]
            if kind == "configmap":
                value = self.journals.get((namespace, name))
                return subprocess.CompletedProcess(argv, 0, json.dumps(value) if value else "", "")
            elif kind == "secret":
                key = self.key or name + namespace + "-synthetic-key"
                if argv[-1].startswith("jsonpath="):
                    output = (
                        name + "\n" + namespace + "\n" + base64.b64encode(key.encode()).decode()
                    )
                    return subprocess.CompletedProcess(argv, 0, output, "")
                value = {
                    "metadata": {"name": name, "namespace": namespace},
                    "data": {"DEMO_KEY": base64.b64encode(key.encode()).decode()},
                }
            else:
                context = argv[argv.index("--context") + 1]
                value = {"metadata": self.metadata(name)}
                if name == "kube-system":
                    value["metadata"]["uid"] = uid(context)
        elif argv[0] == "docker":
            if argv[3:5] == ["image", "inspect"]:
                value = [
                    {
                        "Id": self.image_id,
                        "Os": "linux",
                        "Architecture": "arm64",
                        "Config": {
                            "User": "10001:10001",
                            "Labels": {"org.opencontainers.image.revision": REVISION},
                        },
                    }
                ]
            else:
                assert "crictl" in argv
                value = {"status": {"id": self.running_id}}
        else:
            raise AssertionError(f"Unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")


@pytest.fixture(params=["local", "azure"])
def live(tmp_path, monkeypatch, request):
    monkeypatch.setattr(faults, "ROOT", tmp_path)
    config = DemoConfig(
        request.param,
        "demo",
        "team",
        subscription="11111111-1111-1111-1111-111111111111" if request.param == "azure" else None,
        location="centralus" if request.param == "azure" else None,
    )
    initialize_config(config, tmp_path / ".env")
    commands = NativeCommands(tmp_path, config)
    configuration = faults.LiveConfiguration(tmp_path / ".env", execute=commands)
    yield configuration, commands
    configuration.close()


def test_live_endpoint_uses_native_discovery_and_private_run_access(live):
    config, commands = live
    url, key = config.endpoint("data:shared")
    target = config.target("shared-data")
    assert target.context == config.config.stem + "-shared-data"
    assert target.namespace == target.context + "-data"
    assert target.component("data-api") == {"deployment": "data-api", "container": "data-api"}
    assert target.kubeconfig.stat().st_mode & 0o777 == 0o600
    assert config.endpoint("data:shared") == (url, key)
    assert sum(argv[0] == "bash" for argv, _ in commands.calls) == 1
    assert sum("secret" in argv for argv, _ in commands.calls) == 1
    assert all(key not in json.dumps(argv) for argv, _ in commands.calls)
    assert not (config.path.parent / ".state").exists()
    work = config.root
    config.close()
    assert not work.exists()


@pytest.mark.parametrize("drift", ["context", "namespace", "url", "uid", "kubeconfig"])
def test_discovery_rejects_scope_drift_before_reading_keys(live, drift):
    config, commands = live

    def mutate(value):
        if drift == "namespace":
            value["namespace"]["metadata"]["labels"]["plane-demo/deployment"] = "foreign"
        elif drift == "uid":
            value["cluster"]["metadata"]["uid"] = "invalid"
        else:
            value[drift] = "http://foreign.example.test"

    commands.mutate = mutate
    with pytest.raises(faults.AcceptanceError):
        config.endpoint("management")
    assert not any("secret" in argv for argv, _ in commands.calls)


@pytest.mark.parametrize("key", ["short", KEY + " ", KEY + "\r\n", KEY + "\u00e9"])
def test_runtime_api_key_is_header_safe_and_errors_do_not_disclose_it(live, key):
    config, commands = live
    commands.key = key
    with pytest.raises(faults.AcceptanceError, match="invalid_demo_key") as error:
        config.endpoint("management")
    assert key not in str(error.value)


def test_dotenv_change_or_native_failure_never_uses_old_inventory(live):
    config, commands = live
    commands.returncode = 1
    with pytest.raises(faults.AcceptanceError, match="live_discovery_command_failed") as error:
        config.endpoint("management")
    assert "sensitive" not in str(error.value)
    initialize_config(DemoConfig("local", "other", "run"), config.path)
    with pytest.raises(faults.AcceptanceError, match="configuration_identity_changed"):
        config.endpoint("management")
    assert len(commands.calls) == 1


def test_saved_configuration_is_not_a_fallback(live):
    config, commands = live
    with pytest.raises(faults.AcceptanceError, match="checkout_dotenv"):
        faults.LiveConfiguration(config.path.parent / "acceptance.json", execute=commands)
    assert commands.calls == []


def test_live_clients_use_own_keys_and_literal_headers(live):
    config, commands = live
    commands.key = KEY
    requests = []
    apis = runner.APIs(
        config,
        factory=lambda url, key: runner.Client(
            url,
            key,
            transport=httpx.MockTransport(
                lambda request: (
                    requests.append(request) or httpx.Response(200, json={"status": "ok"})
                )
            ),
        ),
    )
    try:
        apis.client("management").request("GET", "/healthz")
        assert requests[0].headers["X-Demo-Key"] == KEY
        with pytest.raises(faults.AcceptanceError, match="plane_keys_are_not_distinct"):
            apis.client("data:shared")
    finally:
        apis.close()


def test_pair_inventory_uses_live_cluster_and_gateway_identity_not_management_columns(live):
    config, commands = live
    apis = runner.APIs(
        config,
        factory=lambda url, key: runner.Client(
            url,
            key,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "ok"})),
        ),
    )
    subject = runner.Runner(config, "scenario", apis=apis)
    subject.tenants = {
        "shared-a": {"pair_id": "shared"},
        "shared-b": {"pair_id": "shared"},
        "isolated-c": {"pair_id": "isolated-1"},
    }
    try:
        inventory = subject.pair_inventory()
        subject.check_shared_pair()
        identities = [
            item[role + "_cluster_id"]
            for item in inventory.values()
            for role in ("control", "data")
        ]
        assert len(set(identities)) == 4
        assert all(config.config.stem in identifier for identifier in identities)
        assert not any("exec" in argv and "python" in argv for argv, _ in commands.calls)
        assert "management.pairs" not in runner.IDENTITY_PROBE
    finally:
        subject.apis.close()


@pytest.mark.parametrize("wrong_source", [False, True])
def test_live_workload_checks_running_code_without_saved_image_review(
    live, monkeypatch, wrong_source
):
    config, commands = live
    subject = runner.Runner(config, "scenario")
    subject.record["source"] = SOURCE
    target = config.target("management")
    reference = (
        f"localhost/{config.config.stem}-api:{REVISION}"
        if config.environment == "local"
        else config.config.registry_name + ".azurecr.io/plane-api@" + IMAGE_ID
    )
    pod = {
        "metadata": {"uid": uid("pod")},
        "spec": {
            "containers": [{"name": "management-api", "image": reference}],
            "nodeName": target.local.get("node", {}).get("name"),
        },
        "status": {"containerStatuses": [{"name": "management-api", "imageID": IMAGE_ID}]},
    }
    expected = {"src/plane_demo/management/api.py": "a" * 64}
    monkeypatch.setattr(runner, "source_hashes", lambda _: expected)
    monkeypatch.setattr(runner, "local_source_hashes", lambda _: expected)
    kube = SimpleNamespace(
        target=target,
        pod=Mock(return_value=pod),
        exec_json=Mock(
            return_value={"files": {} if wrong_source else expected, "parent_dsn_present": False}
        ),
    )
    try:
        if wrong_source:
            with pytest.raises(faults.AcceptanceError, match="deployed_source_hash_mismatch"):
                subject.verify_workload(kube, "management-api")
        else:
            subject.verify_workload(kube, "management-api")
            assert subject.record["events"][-1]["source_hashes"] == expected
        kube.exec_json.assert_called_once_with(
            "management-api", runner.SOURCE_PROBE, json.dumps(list(expected))
        )
        assert not list(config.root.rglob("*.json"))
    finally:
        subject.apis.close()


@pytest.mark.parametrize("mode", ["all", "outages", "verify-existing"])
def test_live_modes_dispatch_both_outage_algorithms(live, monkeypatch, capsys, mode):
    config, _ = live
    monkeypatch.setattr(faults, "command", lambda _: "")
    monkeypatch.setattr(faults, "source_metadata", lambda: SOURCE)
    for name in (
        "management_image",
        "scenario",
        "verify_existing",
        "applied",
        "workload_evidence",
        "collect_timelines",
    ):
        monkeypatch.setattr(runner.Runner, name, Mock())

    def ready(subject, tenant):
        subject.tenants[tenant] = {"pair_id": "isolated-1" if tenant == "isolated-c" else "shared"}

    monkeypatch.setattr(runner.Runner, "wait_ready", ready)
    management, control = Mock(), Mock()
    monkeypatch.setattr(runner.Runner, "management_outage", management)
    monkeypatch.setattr(runner.Runner, "control_outage", control)
    assert runner.main(["--execute", "--mode", mode], configuration_factory=lambda _: config) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "passed"
    management.assert_called_once()
    control.assert_called_once()


def test_live_entrypoint_emits_report_and_invokes_existing_scenario(live, monkeypatch, capsys):
    config, _ = live
    monkeypatch.setattr(faults, "command", lambda _: "")
    monkeypatch.setattr(faults, "source_metadata", lambda: SOURCE)
    monkeypatch.setattr(
        faults, "protected_write", Mock(side_effect=AssertionError("saved inventory"))
    )
    management = Mock()
    scenario = Mock()
    monkeypatch.setattr(runner.Runner, "management_image", management)
    monkeypatch.setattr(runner.Runner, "scenario", scenario)
    assert (
        runner.main(
            ["--mode", "scenario", "--execute", "--environment", config.environment],
            configuration_factory=lambda _: config,
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "passed" and report["report_storage"] == "configmap+stdout"
    assert "evidence" not in report and KEY not in json.dumps(report)
    management.assert_called_once()
    scenario.assert_called_once()


def test_native_discovery_bridge_is_valid_bash():
    subprocess.run(["bash", "-n", "-c", faults.DISCOVER_SLOT], check=True)
    assert 'source "$1/scripts/lib/env.sh"' in faults.DISCOVER_SLOT
    assert 'source "$1/scripts/lib/discovery.sh"' in faults.DISCOVER_SLOT
    assert 'demo_open_slot "$3"' in faults.DISCOVER_SLOT


def test_api_wrapper_delegates_without_endpoint_files(live, monkeypatch):
    config, _ = live
    source = Path(__file__).resolve().parents[2] / "scripts/harness/api.py"
    spec = importlib.util.spec_from_file_location("live_api_wrapper", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", config.path.parent)
    execute = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(module.subprocess, "run", execute)
    assert module.main([config.environment, "management", "GET", "/healthz"]) == 0
    assert execute.call_args.args[0] == [
        "bash",
        str(config.path.parent / "scripts/operations/api.sh"),
        "management",
        "GET",
        "/healthz",
    ]


@pytest.mark.parametrize("provisioning_status", ["running", "succeeded"])
def test_export_entrypoint_is_an_optional_live_api_report(
    live, monkeypatch, capsys, provisioning_status
):
    config, commands = live

    def handle(request):
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/tenants/shared-a":
            return httpx.Response(
                200,
                json={
                    "tenant_id": "shared-a",
                    "pair_id": "shared",
                    "provisioning_status": provisioning_status,
                    "onboarding_status": "ready",
                },
            )
        return httpx.Response(404, json={"detail": "not_found"})

    apis = runner.APIs(
        config,
        factory=lambda url, key: runner.Client(url, key, transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(runner, "APIs", lambda _: apis)
    monkeypatch.setattr(runner.faults, "LiveConfiguration", lambda _: config)
    monkeypatch.setattr(export_module, "acceptance_module", lambda: runner)
    assert (
        export_module.main(
            ["--once", "--config", str(config.path), "--environment", config.environment]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "observed"
    assert set(report["endpoints"]) == (
        {"management", "control:shared", "data:shared"}
        if provisioning_status == "succeeded"
        else {"management"}
    )
    assert report["tenants"]["shared-a"]["onboarding_status"] == "ready"
    assert "synthetic-key" not in json.dumps(report)
    assert "key_file" not in json.dumps(report)
    assert not config.root.exists()
    assert not (config.path.parent / ".state").exists()
    assert not any("python" in argv and "exec" in argv for argv, _ in commands.calls)


def test_live_client_waits_for_gateway_readiness_without_retrying_tls_failures(live):
    config, _ = live
    responses = iter(
        [httpx.Response(503, text="backend starting"), httpx.Response(200, json={"status": "ok"})]
    )
    client = runner.Client(
        "https://demo.cloudapp.azure.com",
        KEY,
        transport=httpx.MockTransport(lambda _: next(responses)),
    )
    apis = SimpleNamespace(client=lambda _: client, close=client.close)
    clock = [0]
    subject = runner.Runner(
        config,
        "scenario",
        apis=apis,
        clock=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    try:
        assert subject.client("management") is client
        assert clock[0] > 0
    finally:
        client.close()

    def invalid_certificate(_):
        raise httpx.ConnectError("private diagnostic") from ssl.SSLCertVerificationError()

    client = runner.Client(
        "https://demo.cloudapp.azure.com",
        KEY,
        transport=httpx.MockTransport(invalid_certificate),
    )
    try:
        with pytest.raises(faults.AcceptanceError, match="gateway_tls_verification_failed"):
            client.ready()
    finally:
        client.close()


def test_live_fault_entrypoint_refuses_invalid_parent_discovery_before_mutation(live, capsys):
    config, commands = live
    assert (
        faults.main(
            ["--execute", "--slot", "shared-control", "--component", "control-reconciler"],
            configuration_factory=lambda _: config,
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"] == "parent_discovery_response_invalid"
    assert not any("create" in argv or "replace" in argv for argv, _ in commands.calls)


@pytest.mark.parametrize("selection", ["foreign", "legacy", "wrong-revision", "mutable"])
def test_live_workload_rejects_an_unselected_image_before_code_probe(live, selection):
    config, _ = live
    subject = runner.Runner(config, "scenario")
    subject.record["source"] = SOURCE
    target = config.target("management")
    local_image = {
        "foreign": f"localhost/other-team-local-api:{REVISION}",
        "legacy": f"localhost/radplanes-plane-api:{REVISION}",
        "wrong-revision": f"localhost/{config.config.stem}-api:" + "c" * 40,
        "mutable": f"localhost/{config.config.stem}-api:latest",
    }[selection]
    pod = {
        "metadata": {"uid": uid("pod")},
        "spec": {
            "containers": [
                {
                    "name": "management-api",
                    "image": "foreign.azurecr.io/api@" + IMAGE_ID
                    if config.environment == "azure"
                    else local_image,
                }
            ]
        },
        "status": {"containerStatuses": [{"name": "management-api", "imageID": IMAGE_ID}]},
    }
    kube = SimpleNamespace(target=target, pod=Mock(return_value=pod), exec_json=Mock())
    try:
        with pytest.raises(faults.AcceptanceError, match="image"):
            subject.verify_workload(kube, "management-api")
        kube.exec_json.assert_not_called()
    finally:
        subject.apis.close()


def test_runner_environment_expectation_rejects_mismatch_before_actions(live, monkeypatch, capsys):
    config, commands = live
    expectation = "azure" if config.environment == "local" else "local"
    construct = Mock(side_effect=AssertionError("runner constructed before environment guard"))
    monkeypatch.setattr(runner, "Runner", construct)
    assert (
        runner.main(
            ["--execute", "--environment", expectation], configuration_factory=lambda _: config
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"] == "harness_environment_mismatch"
    construct.assert_not_called()
    assert commands.calls == []
    assert not config.root.exists()


def test_export_environment_expectation_rejects_mismatch_before_actions(live, monkeypatch, capsys):
    config, commands = live
    expectation = "azure" if config.environment == "local" else "local"
    report = Mock(side_effect=AssertionError("live report before environment guard"))
    monkeypatch.setattr(export_module, "acceptance_module", lambda: runner)
    monkeypatch.setattr(runner.faults, "LiveConfiguration", lambda _: config)
    monkeypatch.setattr(export_module, "live_report", report)
    assert export_module.main(["--once", "--environment", expectation]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "report_environment_mismatch"
    report.assert_not_called()
    assert commands.calls == []
    assert not config.root.exists()


def test_native_bridge_checks_reloaded_environment_before_opening_a_slot(tmp_path):
    library = tmp_path / "scripts/lib"
    library.mkdir(parents=True)
    (library / "env.sh").write_text(
        "demo_load_env() { DEMO_ENV=azure; }\n"
        "demo_error() { printf '%s\\n' \"$1\" >&2; return 1; }\n"
    )
    (library / "discovery.sh").write_text(
        "demo_open_slot() { printf 'unexpected action' > \"$CALLS\"; exit 9; }\n"
    )
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "-c", faults.DISCOVER_SLOT, "test", str(tmp_path), str(tmp_path), "management"],
        env={**os.environ, "PLANE_DEMO_EXPECT_ENV": "local", "CALLS": str(calls)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Environment changed" in result.stderr
    assert not calls.exists()


class LiveScenarioTests(HarnessRunCase):
    def test_existing_admission_algorithm_runs_without_saved_inventory_or_status_endpoints(self):
        from unittest.mock import patch

        self.tenants.clear()
        selected = DemoConfig(
            "azure", "demo", "team", "11111111-1111-1111-1111-111111111111", "centralus"
        )
        initialize_config(selected, self.project / ".env")
        commands = NativeCommands(self.project, selected)
        configuration = faults.LiveConfiguration(self.project / ".env", execute=commands)
        self.addCleanup(configuration.close)
        self.values["images"] = {
            role: selected.registry_name + f".azurecr.io/plane-{role}@" + IMAGE_ID
            for role in ("api", "provisioner")
        }
        urls = {
            f"https://{selected.slot_name(slot)}.centralus.cloudapp.azure.com": (
                "management"
                if slot == "management"
                else slot.rsplit("-", 1)[1] + ":" + slot.rsplit("-", 1)[0]
            )
            for slot in faults.LOCAL_SLOTS
        }
        apis = runner.APIs(
            configuration,
            factory=lambda url, key: runner.Client(
                url,
                key,
                transport=httpx.MockTransport(lambda request: self.request(urls[url], request)),
            ),
        )
        self.clients = apis.clients

        def kube(target):
            value = self.kube(target.slot)
            value.target = target
            return value

        tenant = self.tenant

        def current_tenant(*args):
            value = tenant(*args)
            value.pop("control_url", None)
            value.pop("data_url", None)
            return value

        subject = runner.Runner(
            configuration,
            "scenario",
            apis=apis,
            kube_factory=kube,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.runner = subject
        subject.collect_timelines = Mock(return_value={"unchanged": True})
        subject.control_poll_observed = Mock(return_value=True)
        with (
            patch.object(self, "tenant", side_effect=current_tenant),
            patch.object(runner, "paused_reconciler", side_effect=self.pause),
            patch.object(runner, "source_hashes", return_value={"source.py": "c" * 64}),
            patch.object(faults, "command", return_value=""),
            patch.object(faults, "source_metadata", return_value=self.current_source),
            patch.object(faults, "read_json", side_effect=AssertionError("saved inventory read")),
            patch.object(faults, "protected_write", side_effect=AssertionError("saved report")),
        ):
            subject.run()
        assert subject.record["outcome"] == "passed"
        assert [
            event["tenant"]
            for event in subject.record["events"]
            if event["type"] == "tenant_accepted"
        ] == ["shared-a", "shared-b", "isolated-c"]
        assert all(
            "control_url" not in value and "data_url" not in value
            for value in subject.tenants.values()
        )
        assert any(
            event["type"] == "immediate_child_readiness_proved"
            for event in subject.record["events"]
        )
        assert any(event["type"] == "data_api_permissions" for event in subject.record["events"])
        assert not subject.path.exists()
