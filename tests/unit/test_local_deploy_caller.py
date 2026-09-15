import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from plane_demo.management.providers.credentials import StoredCredentials
from plane_demo.management.providers.identity import DemoConfig
from plane_demo.management.providers.local import decode_access
from plane_demo.management.providers.local_config import SLOTS
from plane_demo.management.providers.secret_store import CredentialScope, CredentialValue
from plane_demo.management.provisioning import ProvisioningError

ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40
KEYS = {
    "management": "synthetic-management-key-" + "x" * 40,
    "shared-control": "synthetic-control-key-" + "y" * 40,
}


def load_caller():
    spec = importlib.util.spec_from_file_location(
        "canonical_local_deploy", ROOT / "scripts/operations/local/deploy-demo.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


@pytest.fixture
def caller(tmp_path, monkeypatch):
    original_umask = os.umask(0o077)
    os.umask(original_umask)
    script = load_caller()
    identity = DemoConfig("local", "demo", "one", revision=REVISION)
    ca = base64.b64encode(b"synthetic CA").decode()
    public = {
        "version": 1,
        "provider": "local",
        "projectName": identity.project,
        "bootstrapIdentity": identity.public_values(),
        "allocations": {
            slot: {
                "slot": slot,
                "clusterName": identity.slot_name(slot),
                "context": identity.slot_name(slot),
                "gatewayPort": 35490 + index,
                "apiPort": 35495 + index,
            }
            for index, slot in enumerate(SLOTS)
        },
        "recipes": {
            kind: {
                "reference": "http://local-module-"
                + "b" * 20
                + ".radius-system.svc.cluster.local:18080/"
                + "c" * 64
                + ".tar.gz",
                "digest": "sha256:" + "c" * 64,
                "moduleServer": "local-module-" + "b" * 20,
            }
            for kind in ("cluster", "postgresql", "redis", "gateway")
        },
        "images": {
            role: {
                "reference": f"localhost/{identity.stem}-{role}:{REVISION}",
                "imageId": "sha256:" + hashlib.sha256(role.encode()).hexdigest(),
            }
            for role in ("api", "provisioner")
        },
        "managementCluster": {
            "clusterId": "kind://" + identity.slot_name("management"),
            "uid": "11111111-1111-4111-8111-111111111111",
            "nodeAddress": "172.18.0.2",
            "serviceAddress": "10.96.0.1",
            "caSHA256": hashlib.sha256(b"synthetic CA").hexdigest(),
        },
    }
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEMO_ENV=local\nDEMO_PROJECT=demo\nDEMO_DEPLOYMENT=one\n"
        f"DEMO_KEY_MANAGEMENT={KEYS['management']}\n"
        f"DEMO_KEY_SHARED_CONTROL={KEYS['shared-control']}\n"
    )
    env_file.chmod(0o600)
    types = tmp_path / "infra/radius/types"
    types.mkdir(parents=True)
    for kind in ("clusters", "postgresql", "gateways"):
        (types / f"{kind}.yaml").write_text("synthetic source")
    node_context = "kind-" + identity.slot_name("management")
    profile = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": node_context,
        "clusters": [
            {
                "name": node_context,
                "cluster": {
                    "server": "https://127.0.0.1:35495",
                    "certificate-authority-data": ca,
                },
            }
        ],
        "contexts": [
            {
                "name": node_context,
                "context": {
                    "cluster": node_context,
                    "user": node_context,
                },
            }
        ],
        "users": [
            {
                "name": node_context,
                "user": {
                    "client-certificate-data": ca,
                    "client-key-data": ca,
                },
            }
        ],
    }
    state = SimpleNamespace(
        script=script,
        root=tmp_path,
        public=public,
        commands=[],
        order=[],
        stored={},
        paths=[],
        active=False,
        fail_command=None,
        factory_error=None,
        seed_error=None,
        deploy_error=None,
        config=None,
        closed=False,
    )

    def run(commands, argv, *, env, timeout):
        kwargs = {
            "cwd": commands.root,
            "env": {**commands.environment, **env},
            "timeout": timeout,
        }
        state.commands.append((argv, kwargs))
        assert kwargs["cwd"] == tmp_path and kwargs["timeout"] == 600
        environment = kwargs["env"]
        assert not any(key.startswith(("DEMO_KEY_", "AZURE_")) for key in environment)
        assert "DOCKER_CONTEXT" not in environment and "DOCKER_CONFIG" not in environment
        assert not set(KEYS.values()).intersection(argv)
        if Path(argv[0]).name == state.fail_command:
            raise ProvisioningError("synthetic_command_failed")
        if argv[0] == "bash":
            assert argv == ["bash", str(tmp_path / "scripts/operations/local/setup.sh"), "inspect"]
            text = json.dumps(state.public)
        elif argv[0] == "rad":
            assert argv[1] == "--config"
            assert Path(argv[2]).parent == commands.state_root
            if argv[3:] == ["version", "--cli"]:
                text = "Release Version: v0.60.2"
            else:
                assert argv[3:5] == ["bicep", "publish-extension"]
                source = Path(argv[argv.index("--from-file") + 1])
                assert source.read_text() == "synthetic source"
                target = Path(argv[argv.index("--target") + 1])
                target.write_bytes(b"generated-" + source.stem.encode())
                text = ""
        elif Path(argv[0]).name == "bicep":
            assert argv[1:] == ["--version"]
            text = "Bicep CLI version 0.42.1"
        elif argv[0] == "docker":
            assert argv[1:4] == ["context", "inspect", "desktop-linux"]
            text = json.dumps("unix:///Users/operator/.docker/run/docker.sock")
        elif argv[0] == "kind":
            assert argv == ["kind", "get", "kubeconfig", "--name", identity.slot_name("management")]
            assert environment["DOCKER_HOST"] == "unix:///Users/operator/.docker/run/docker.sock"
            assert environment["KIND_EXPERIMENTAL_PROVIDER"] == "docker"
            assert Path(environment["HOME"]).stat().st_mode & 0o777 == 0o700
            text = json.dumps(profile)
        elif argv[0] == "kubectl":
            path = Path(argv[2])
            assert path.stat().st_mode & 0o777 == 0o600
            assert argv[3:] == [
                "config",
                "rename-context",
                node_context,
                identity.slot_name("management"),
            ]
            value = json.loads(path.read_text())
            value["current-context"] = argv[-1]
            value["contexts"][0]["name"] = argv[-1]
            path.write_text(json.dumps(value))
            text = "Context renamed"
        else:
            raise AssertionError(argv)
        return text

    class Store:
        scope = CredentialScope("demo", "one", "local")

        def get_or_create(self, slot, role, *, provided_value, require_existing=False):
            assert state.active and not require_existing
            if state.seed_error:
                raise ProvisioningError(state.seed_error)
            assert role == "demoKey" and provided_value == KEYS[slot]
            state.stored[slot] = provided_value
            state.order.append(("seed", slot))
            return CredentialValue(provided_value)

    @contextmanager
    def factory(config, root, *, kubeconfig, context):
        state.config = config
        state.paths.append(kubeconfig)
        assert root == tmp_path and context == config.allocation("management")["context"]
        assert config.identity.demo_keys == KEYS and config.identity.revision == REVISION
        assert "DEMO_KEY_MANAGEMENT" not in config.bootstrap_settings
        assert kubeconfig.stat().st_mode & 0o777 == 0o600
        for kind in ("clusters", "postgresql", "gateways"):
            artifact = types / f"{kind}.tgz"
            assert artifact.read_bytes() == b"generated-" + kind.encode()
            assert artifact.stat().st_mode & 0o777 == 0o644
        assert not list(types.glob(".extensions-*"))
        decode_access(kubeconfig.read_text(), context, child=False)
        if state.factory_error:
            raise ProvisioningError(state.factory_error)
        state.active = True
        state.order.append("lease-acquired")
        credentials = StoredCredentials(config, Store())

        def guard():
            if not state.active:
                raise ProvisioningError("management_bootstrap_owner_mismatch")

        credentials.bind(lambda slot: {}, lambda value: None, guard)

        def deploy_plane(slot):
            assert state.active and slot == "management" and state.stored == KEYS
            state.order.append(("deploy", slot))
            if state.deploy_error:
                raise ProvisioningError(state.deploy_error)
            return "http://127.0.0.1:35490"

        try:
            yield SimpleNamespace(credentials=credentials, deploy_plane=deploy_plane)
        finally:
            state.order.append("lease-released")
            state.active = False
            state.closed = True

    monkeypatch.setattr(script.Commands, "run", run)
    monkeypatch.setattr(script, "local_operator_provider", factory)
    monkeypatch.setattr(script, "ROOT", tmp_path)
    try:
        yield state
    finally:
        os.umask(original_umask)


def test_normal_caller_seeds_service_keys_before_management_deployment(caller):
    assert caller.script.deploy(caller.root) == "http://127.0.0.1:35490"
    assert caller.order == [
        "lease-acquired",
        ("seed", "management"),
        ("seed", "shared-control"),
        ("deploy", "management"),
        "lease-released",
    ]
    assert caller.closed and not any(path.exists() for path in caller.paths)
    assert {path.name for path in caller.root.iterdir()} == {".env", "infra"}
    assert all(args[0] not in {"az", "terraform"} for args, _ in caller.commands)


def test_main_reports_management_result_only_after_guarded_completion(caller, capsys):
    assert caller.script.main(["--execute"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "slot": "management",
        "url": "http://127.0.0.1:35490",
    }
    assert caller.closed and caller.order[-1] == "lease-released"
    assert ("deploy", "management") in caller.order
    assert not any(path.exists() for path in caller.paths)


@pytest.mark.parametrize("tool", ["bash", "rad", "bicep", "docker", "kind", "kubectl"])
def test_external_failure_never_activates_credentials_or_exposes_keys(caller, tool, capsys):
    caller.fail_command = tool
    assert caller.script.main(["--execute"]) == 1
    output = capsys.readouterr()
    assert all(value not in output.out + output.err for value in KEYS.values())
    assert not caller.stored and not caller.order
    assert not any(path.exists() for path in caller.paths)


def test_active_or_interrupted_lease_is_not_bypassed(caller):
    caller.factory_error = "management_bootstrap_active_or_interrupted"
    with pytest.raises(ProvisioningError, match=caller.factory_error):
        caller.script.deploy(caller.root)
    assert caller.order == [] and not caller.stored
    assert not any(path.exists() for path in caller.paths)


@pytest.mark.parametrize("failure", ["seed_error", "deploy_error"])
def test_failure_releases_public_factory_and_disposable_access(caller, failure):
    setattr(caller, failure, "guarded_operation_failed")
    with pytest.raises(ProvisioningError, match="guarded_operation_failed"):
        caller.script.deploy(caller.root)
    assert caller.closed and caller.order[-1] == "lease-released"
    assert not any(path.exists() for path in caller.paths)
    if failure == "seed_error":
        assert ("deploy", "management") not in caller.order


@pytest.mark.parametrize("change", ["deployment", "missing-revision", "secret-identity"])
def test_unowned_or_invalid_setup_identity_stops_before_access(caller, change):
    if change == "deployment":
        caller.public["bootstrapIdentity"]["DEMO_DEPLOYMENT"] = "another"
    elif change == "missing-revision":
        caller.public["bootstrapIdentity"].pop("DEMO_REVISION")
    else:
        caller.public["bootstrapIdentity"]["DEMO_KEY_MANAGEMENT"] = KEYS["management"]
    with pytest.raises(ProvisioningError):
        caller.script.deploy(caller.root)
    assert len(caller.commands) == 1 and not caller.order


def test_explicit_source_revision_cannot_be_replaced_by_discovery(caller):
    with (caller.root / ".env").open("a") as stream:
        stream.write("DEMO_REVISION=" + "b" * 40 + "\n")
    with pytest.raises(ProvisioningError, match="local_setup_identity_mismatch"):
        caller.script.deploy(caller.root)
    assert len(caller.commands) == 1


def test_legacy_marker_and_file_credentials_are_not_read(caller):
    state = caller.root / ".state/local"
    state.mkdir(parents=True)
    for name in (
        "credentials.json",
        "provisioning.json",
        "setup-demo-complete.json",
        "deploy-demo-intent.json",
        "deploy-demo-complete.json",
    ):
        (state / name).write_text("invalid retained data, not an authority")
    before = {path.name: path.read_bytes() for path in state.iterdir()}
    caller.script.deploy(caller.root)
    assert before == {path.name: path.read_bytes() for path in state.iterdir()}


def test_preview_and_nonlocal_configuration_never_run_operator_commands(caller):
    assert caller.script.main([]) == 0
    assert caller.commands == []
    (caller.root / ".env").write_text(
        "DEMO_ENV=azure\nDEMO_PROJECT=demo\nDEMO_DEPLOYMENT=one\n"
        "AZURE_SUBSCRIPTION_ID=11111111-1111-4111-8111-111111111111\nAZURE_LOCATION=centralus\n"
    )
    with pytest.raises(ProvisioningError, match="local_environment_required"):
        caller.script.deploy(caller.root)
    assert caller.commands == []


def test_fresh_extension_preparation_compiles_the_actual_database_template(tmp_path):
    script = load_caller()
    root = tmp_path / "checkout"
    workspace = tmp_path / "work"
    workspace.mkdir()
    sources = [
        ROOT / "infra/radius/bicepconfig.json",
        *ROOT.glob("infra/radius/types/*.yaml"),
        *ROOT.glob("infra/radius/modules/*.bicep"),
    ]
    for source in sources:
        target = root / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    types = root / "infra/radius/types"
    assert not list(types.glob("*.tgz"))
    commands = script.Commands(root, state_root=workspace, local=True)
    environment = {"HOME": str(Path.home())}
    script.prepare_extensions(commands, environment)
    assert {path.name for path in types.glob("*.tgz")} == {
        "clusters.tgz",
        "postgresql.tgz",
        "gateways.tgz",
    }
    assert not list(types.glob(".extensions-*"))
    output = commands.run(
        [
            str(Path.home() / ".rad/bin/bicep"),
            "build",
            str(root / "infra/radius/modules/database.bicep"),
            "--stdout",
        ],
        env=environment,
    )
    assert json.loads(output)["resources"]
    assert not (root / ".state").exists()


def test_nested_setup_timeout_stops_the_owned_descendant(tmp_path):
    script = load_caller()
    child_code = (
        "import os,signal,sys,time\n"
        "from pathlib import Path\n"
        "def stopped(*_):\n"
        "    Path(sys.argv[2]).write_text('terminated')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    pid_file, stopped_file = tmp_path / "child.pid", tmp_path / "child.stopped"
    parent_code = (
        "import signal,subprocess,sys\n"
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]])\n"
        "def stopped(*_):\n"
        "    child.wait(timeout=5)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        "child.wait()\n"
    )
    commands = script.Commands(tmp_path, state_root=tmp_path, local=True)
    with pytest.raises(ProvisioningError, match="local_setup_observation_failed"):
        script.command(
            commands,
            [sys.executable, "-c", parent_code, child_code, str(pid_file), str(stopped_file)],
            {},
            "local_setup_observation_failed",
            timeout=1,
        )
    assert stopped_file.read_text() == "terminated"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
