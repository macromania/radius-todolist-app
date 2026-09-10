import json
from unittest.mock import Mock

import pytest
import yaml
from local_support import bootstrap, common, images


class InstallCommands:
    def __init__(self):
        self.calls = []
        self.mode = "660"
        self.pod_daemon = "desktop-daemon"
        self.sidecar = False

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if "stat" in args:
            return f"0 0 {self.mode}\n"
        if args[0] == "docker" and "{{.ID}}" in args:
            return "desktop-daemon\n"
        if args[-1].endswith("--format '{{.ID}}'"):
            return self.pod_daemon + "\n"
        if "sha256sum" in args:
            return "verified-binary-hash  /dynamic-rp\n"
        return ""

    def json(self, args):
        self.calls.append((args, {}))
        if "deploy" in args:
            containers = [{"name": "dynamic-rp", "image": common.image_names()["executor"]}]
            if self.sidecar:
                containers.append({"name": "unexpected-sidecar"})
            return {"spec": {"template": {"spec": {"containers": containers}}}}
        if "configmap" in args:
            return {"data": {"radius-self-host.yaml": "terraform:\n  path: /terraform\n"}}
        raise AssertionError(args)

    def apply(self, data):
        self.calls.append((common.kube("apply", "-f", "-"), {"data": json.dumps(data)}))


@pytest.fixture
def install_commands(local_state, monkeypatch):
    commands = InstallCommands()
    monkeypatch.setattr(
        bootstrap,
        "require_inspection",
        Mock(
            return_value={
                "images": {"executor": {"contents": "verified-binary-hash  /dynamic-rp\n"}},
            }
        ),
    )
    monkeypatch.setattr(bootstrap, "verify_management", Mock())
    monkeypatch.setattr(bootstrap, "verify_encryption", Mock())
    return commands


def test_install_entrypoint_applies_only_management_executor_overlay(
    local_state,
    install_commands,
    monkeypatch,
):
    monkeypatch.setattr(bootstrap, "Commands", lambda: install_commands)
    assert bootstrap.main(["install", "--execute"]) == 0
    installed = json.loads((local_state / "installed.json").read_text())
    assert installed["socketDaemonId"] == "desktop-daemon"
    assert installed["supplementalGroups"] == [0]
    patches = [a for a, _ in install_commands.calls if "patch" in a]
    assert len(patches) == 2
    assert all("radius-system" in a for a in patches)
    deployment = next(a for a in patches if "deployment" in a)
    assert "dynamic-rp" in deployment
    patch = json.loads(deployment[-1])
    pod = patch["spec"]["template"]["spec"]
    assert pod["securityContext"] == {
        "supplementalGroups": [0],
        "fsGroup": 65532,
        "fsGroupChangePolicy": "OnRootMismatch",
    }
    assert pod["containers"][0]["securityContext"]["runAsUser"] == 65532
    assert "command" not in pod["containers"][0]
    configmap = next(a for a in patches if "configmap" in a)
    config = yaml.safe_load(json.loads(configmap[-1])["data"]["radius-self-host.yaml"])
    assert config["terraform"] == {"path": "/terraform", "logLevel": "OFF"}
    bootstrap.verify_encryption.assert_called_once_with(
        install_commands,
        "radius-system",
        "radius-encryption-key",
    )
    all_commands = [a for a, _ in install_commands.calls]
    install = next(a for a in all_commands if "install" in a)
    assert f"dynamicrp.image={common.image_names()['executor']}" in install
    assert not any(a[0] == "kind" and "create" in a for a in all_commands)
    assert any("Demo.Platform/clusters" not in a and "resource-type" in a for a in all_commands)


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("mode", "600", "not group-writable"),
        ("pod_daemon", "another-daemon", "does not address"),
        ("sidecar", True, "Unexpected dynamic-rp"),
    ],
)
def test_install_failures_do_not_claim_ready_or_escalate_uid(
    local_state,
    install_commands,
    field,
    value,
    error,
):
    setattr(install_commands, field, value)
    with pytest.raises(common.LocalError, match=error):
        bootstrap.install(install_commands)
    assert not (local_state / "installed.json").exists()
    assert not any("chmod" in a or "chown" in a for a, _ in install_commands.calls)
    assert (local_state / "install-started.json").exists()


def test_inspected_image_must_match_current_architecture_and_id(local_state, monkeypatch):
    names = common.image_names()
    common.write_private(
        local_state / "image-review.json",
        {
            "architecture": "arm64",
            "images": {kind: {"name": name, "id": kind} for kind, name in names.items()},
        },
    )
    monkeypatch.setattr(images, "architecture", lambda _: "arm64")
    commands = Mock()
    commands.json.side_effect = [[{"Id": "executor"}], [{"Id": "operator"}]]
    assert images.require_inspection(commands)["architecture"] == "arm64"
    commands.json.side_effect = [[{"Id": "rebuilt-uninspected-image"}]]
    with pytest.raises(common.LocalError, match="changed after inspection"):
        images.require_inspection(commands)


def test_capacity_and_all_ports_are_recorded_before_management_create(
    local_state,
    monkeypatch,
):
    calls = []
    held = [Mock() for _ in range(10)]
    monkeypatch.setattr(bootstrap, "require_inspection", Mock())
    monkeypatch.setattr(bootstrap, "reserve_ports", Mock(return_value=held))
    commands = Mock()

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["kind", "version"]:
            return "kind v0.31.0"
        if "version" in args and args[0] == "rad":
            return "0.60.2"
        if args[-2:] == ["config", "current-context"]:
            return common.MANAGEMENT
        if args[0] == "kind" and "create" in args:
            assert all(sock.close.called for sock in held)
            record = json.loads((local_state / "bootstrap-started.json").read_text())
            assert record["reservedPorts"] == list(range(35490, 35500))
            assert record["dockerCapacity"]["NCPU"] == 10
            common.write_private(local_state / "home/.kube/config", "offline-kubeconfig")
        return ""

    commands.run.side_effect = run
    commands.json.side_effect = [
        {"NCPU": 10, "MemTotal": 25162678272, "Architecture": "aarch64"},
        [
            {
                "Id": "management-id",
                "Name": f"/{common.MANAGEMENT}-control-plane",
                "Config": {"Labels": {"io.x-k8s.kind.cluster": common.MANAGEMENT}},
                "NetworkSettings": {"Networks": {"kind": {"IPAddress": "172.18.0.2"}}},
            }
        ],
    ]
    bootstrap.create(commands, "/var/run/docker.sock")
    creation = next(args for args in calls if args[0] == "kind" and "create" in args)
    assert common.MANAGEMENT in creation
    assert "--kubeconfig" in creation and "--config" in creation
    assert common.NODE_IMAGE in creation
    rename = next(args for args in calls if "rename-context" in args)
    assert rename == [
        "kubectl",
        "--kubeconfig",
        str(local_state / "home/.kube/config"),
        "--context",
        f"kind-{common.MANAGEMENT}",
        "config",
        "rename-context",
        f"kind-{common.MANAGEMENT}",
        common.MANAGEMENT,
    ]
    readiness = next(args for args in calls if "--for=condition=Ready" in args)
    assert calls.index(creation) < calls.index(rename) < calls.index(readiness)
    assert readiness[readiness.index("--context") + 1] == common.MANAGEMENT
    encryption = yaml.safe_load((local_state / "management/encryption.yaml").read_text())
    assert encryption["resources"][0]["providers"][0]["aescbc"]
    assert (local_state / "management/encryption.yaml").stat().st_mode & 0o777 == 0o600
