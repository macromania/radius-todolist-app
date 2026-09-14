"""Run the real Bash helper and its node-side shell against synthetic CLI doubles."""

import base64
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/operations/local/encryption.sh"
NODE_IMAGE = (
    "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
)
CLUSTER = "radplanes-encryption-test"
NODE = CLUSTER + "-control-plane"
CONTEXT = "kind-" + CLUSTER
CLUSTER_UID = "11111111-1111-4111-8111-111111111111"
NODE_UID = "22222222-2222-4222-8222-222222222222"
PROFILE_PATH = "/etc/kubernetes/radplanes/encryption.yaml"
SYNTHETIC_KEY = base64.b64encode(b"A" * 32).decode()

# No Docker/Kubernetes client is contacted. The node shell runs against a test-owned
# filesystem with synthetic /dev/urandom bytes and Linux stat/sha256sum doubles.
CLI_DOUBLE = r"""
import base64
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

root = Path(os.environ["FAKE_ROOT"])
state_path = root / "state.json"
state = json.loads(state_path.read_text())
tool = Path(sys.argv[0]).name
args = sys.argv[1:]

def save():
    state_path.write_text(json.dumps(state))

def output(value):
    print(json.dumps(value))

def die():
    print("synthetic-private-failure-value")
    print("synthetic-private-failure-value", file=sys.stderr)
    sys.exit(7)

def translated(value):
    return value.replace("/etc/kubernetes", str(root / "node/etc/kubernetes")).replace(
        "/run/radplanes-encryption.lock", str(root / "node/run/radplanes-encryption.lock"))

if tool in {"head", "stat", "sha256sum"}:
    if tool == "head":
        assert args == ["-c", "32", "/dev/urandom"]
        with (root / "key-generations").open("a") as stream:
            stream.write("generated\n")
        sys.stdout.buffer.write(b"A" * 32)
    elif tool == "stat":
        assert args[:2] == ["-c", "%u:%g:%a"]
        path = Path(args[-1])
        assert path.is_relative_to(root / "node")
        owner = "501:20" if state.get("foreign_file_owner") else "0:0"
        print(f"{owner}:{stat.S_IMODE(path.stat().st_mode):o}")
    else:
        path = Path(args[-1])
        assert path.is_relative_to(root / "node")
        print(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + str(path))
    sys.exit(0)

with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps({
        "tool": tool, "args": args,
        "env": {key: value for key, value in os.environ.items() if key in {
            "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "KUBECONFIG",
            "KUBERNETES_MASTER", "HTTPS_PROXY", "AZURE_CONFIG_DIR",
        }},
    }) + "\n")

if tool == "docker":
    assert args[:2] in [["--context", "desktop-linux"], [
        "--host", "unix:///Users/operator/.docker/run/docker.sock"]]
    if state.get("context_unavailable") and args[0] == "--context":
        die()
    args = args[2:]
    if args[:3] == ["inspect", "--type", "container"]:
        assert args[3:] == [state["node"]]
        if state.get("failure") == "hang":
            time.sleep(60)
        if state.get("failure") == "inspect":
            die()
        output([state["inspection"]])
    else:
        assert args[:3] == ["exec", "--user", "0"]
        args = args[3:]
        if args[0] == "-i":
            args = args[1:]
        assert args[0] == state["node"]
        command = args[1:]
        if command == ["timeout", "--version"]:
            if state.get("missing_node_timeout"):
                die()
            print("timeout (GNU coreutils) offline-double")
            sys.exit(0)
        if command[0] == "sha256sum":
            if state.get("failure") == "digest":
                die()
            command = [str(root / "bin/sha256sum"), translated(command[1])]
            sys.exit(subprocess.run(command).returncode)
        assert command[:4] == ["timeout", "--signal=TERM", "--kill-after=2s", "20s"]
        timeout_args, command = command[1:4], command[4:]
        assert command[0] == "sh"
        key_install = command[1] == "-s"
        if state.get("failure") == ("key" if key_install else "manifest"):
            die()
        data = sys.stdin.read()
        if key_install:
            state["key_checks"] = state.get("key_checks", 0) + 1
        else:
            state["manifest_writes"] = state.get("manifest_writes", 0) + 1
        if key_install and state.get("concurrent_manifest_change"):
            with (root / "node/etc/kubernetes/manifests/kube-apiserver.yaml").open("a") as stream:
                stream.write("# concurrent edit\n")
        save()
        result = subprocess.run(
            [os.environ["FAKE_GNU_TIMEOUT"], *timeout_args,
             "/bin/sh", *[translated(part) for part in command[1:]]],
            input=translated(data) if key_install else data,
            capture_output=True, text=True,
        )
        if result.returncode == 0 and not key_install:
            state = json.loads(state_path.read_text())
            state["installed"] = True
            save()
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
elif tool == "kubectl":
    assert args[:6] == [
        "--kubeconfig", str(root / "selected.kubeconfig"),
        "--context", state["context"], "--request-timeout=10s", args[5],
    ]
    args = args[5:]
    namespace = None
    if args[:1] == ["-n"]:
        namespace, args = args[1], args[2:]
    if args == ["config", "view", "--minify", "-o", "json"]:
        output(state["configuration"])
    elif args == ["get", "namespace", "kube-system", "-o", "json"]:
        if state.get("failure") == "namespace":
            die()
        uid = state["cluster_uid"]
        if state.get("replace_namespace") and state.get("installed"):
            uid = "99999999-9999-4999-8999-999999999999"
        output({"kind": "Namespace", "metadata": {"name": "kube-system", "uid": uid}})
    elif args == ["get", "node", state["node"], "-o", "json"]:
        output({"kind": "Node", "metadata": {"name": state["node"], "uid": state["node_uid"]},
                "status": {"nodeInfo": {"kubeletVersion": "v1.35.0"}}})
    elif args == ["get", "pod", "kube-apiserver-" + state["node"], "-o", "json"]:
        assert namespace == "kube-system"
        pod = state["pod"]
        if state.get("installed"):
            manifest = root / "node/etc/kubernetes/manifests/kube-apiserver.yaml"
            pod = json.loads(manifest.read_text())
            pod["metadata"].update(
                name="kube-apiserver-" + state["node"],
                uid="44444444-4444-4444-8444-444444444444",
                annotations={"kubernetes.io/config.source": "file",
                             "kubernetes.io/config.mirror": "new-mirror-hash"},
                ownerReferences=[{"kind": "Node", "name": state["node"], "uid": state["node_uid"]}],
            )
            pod["spec"]["nodeName"] = state["node"]
            pod["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
        output(pod)
    elif args == ["get", "--raw=/readyz"]:
        state["ready_requests"] = state.get("ready_requests", 0) + 1
        save()
        if state.get("ready_delay", 0) >= state["ready_requests"]:
            sys.exit(1)
        print("ok")
    elif args == ["create", "-f", "-", "-o", "json"]:
        assert namespace == "default"
        if state.get("failure") == "create":
            die()
        value = json.loads(sys.stdin.read())
        assert value["metadata"] == {
            "generateName": "radplanes-encryption-probe-", "namespace": "default"}
        value["metadata"].update(
            name="radplanes-encryption-probe-test1",
            uid="55555555-5555-4555-8555-555555555555",
        )
        value["data"] = {
            key: base64.b64encode(text.encode()).decode()
            for key, text in value.pop("stringData").items()
        }
        state["probe"] = value
        save()
        output(value)
    elif args[0] == "exec":
        assert namespace == "kube-system"
        assert args[1:4] == ["etcd-" + state["node"], "--", "etcdctl"]
        assert "--dial-timeout=5s" in args and "--command-timeout=5s" in args
        key = args[args.index("get") + 1]
        assert key == "/registry/secrets/default/" + state["probe"]["metadata"]["name"]
        if state.get("failure") == "etcd":
            die()
        if state.get("interrupt_etcd"):
            (root / "interrupt-ready").write_text("ready")
            time.sleep(60)
        payload = b"plain-secret" if state.get("plaintext") else (
            b"k8s:enc:aescbc:v1:local-key:" + b"\x01\xffsynthetic-ciphertext")
        output({"kvs": [{"key": base64.b64encode(key.encode()).decode(),
                        "value": base64.b64encode(payload).decode()}]})
    elif args[:2] == ["get", "secret"]:
        assert namespace == "default"
        assert args[2:] == [state["probe"]["metadata"]["name"], "-o", "json"]
        state["probe_gets"] = state.get("probe_gets", 0) + 1
        save()
        if state.get("cleanup_read_failure"):
            die()
        value = state["probe"]
        if state.get("replace_probe"):
            value["metadata"]["uid"] = "99999999-9999-4999-8999-999999999999"
            save()
        if state.get("wrong_decryption"):
            value["data"]["probe"] = base64.b64encode(b"wrong-value").decode()
        output(value)
    elif args[:2] == ["delete", "--raw"]:
        assert namespace is None
        assert args[2:] == [
            "/api/v1/namespaces/default/secrets/" + state["probe"]["metadata"]["name"], "-f", "-"]
        options = json.loads(sys.stdin.read())
        assert set(options) == {"apiVersion", "kind", "preconditions"}
        assert options["apiVersion"] == "v1" and options["kind"] == "DeleteOptions"
        state["delete_options"] = options
        if state.get("replace_before_delete"):
            state["probe"]["metadata"]["uid"] = "99999999-9999-4999-8999-999999999999"
        save()
        if (state.get("cleanup_delete_failure") or
                options["preconditions"] != {"uid": state["probe"]["metadata"]["uid"]}):
            die()
        state.pop("probe")
        state["probe_deletes"] = state.get("probe_deletes", 0) + 1
        save()
    else:
        raise AssertionError(args)
else:
    raise AssertionError(tool)
"""


@pytest.fixture
def node_lab(tmp_path):
    jq = shutil.which("jq")
    if jq is None:
        pytest.fail("jq is required for the native helper's offline tests")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "cli-double"
    executable.write_text(f"#!{sys.executable}\n" + CLI_DOUBLE)
    executable.chmod(0o700)
    for name in ("docker", "kubectl", "head", "stat", "sha256sum"):
        (bin_dir / name).symlink_to(executable)
    (bin_dir / "jq").symlink_to(jq)
    (bin_dir / "sleep").symlink_to(shutil.which("sleep"))
    (bin_dir / "python3").symlink_to(sys.executable)
    manifests = tmp_path / "node/etc/kubernetes/manifests"
    manifests.mkdir(parents=True)
    (tmp_path / "node/run").mkdir()
    (manifests / "kube-apiserver.yaml").write_text("kind: Pod\n# original node-owned manifest\n")
    (tmp_path / "selected.kubeconfig").write_text("synthetic selected access, never global\n")
    state = {
        "node": NODE,
        "context": CONTEXT,
        "cluster_uid": CLUSTER_UID,
        "node_uid": NODE_UID,
        "inspection": {
            "Name": "/" + NODE,
            "Id": "a" * 64,
            "State": {"Running": True},
            "Config": {
                "Image": NODE_IMAGE,
                "Labels": {
                    "io.x-k8s.kind.cluster": CLUSTER,
                    "io.x-k8s.kind.role": "control-plane",
                },
            },
            "Mounts": [],
            "NetworkSettings": {
                "Ports": {
                    "6443/tcp": [
                        {
                            "HostIp": "127.0.0.1",
                            "HostPort": "35512",
                        }
                    ]
                }
            },
        },
        "configuration": {
            "current-context": CONTEXT,
            "contexts": [{"name": CONTEXT, "context": {"cluster": CONTEXT, "user": CONTEXT}}],
            "clusters": [
                {
                    "name": CONTEXT,
                    "cluster": {
                        "server": "https://127.0.0.1:35512",
                        "certificate-authority-data": "DATA+OMITTED",
                    },
                }
            ],
            "users": [
                {
                    "name": CONTEXT,
                    "user": {
                        "client-certificate-data": "DATA+OMITTED",
                        "client-key-data": "DATA+OMITTED",
                    },
                }
            ],
        },
        "pod": {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "kube-apiserver-" + NODE,
                "namespace": "kube-system",
                "uid": "33333333-3333-4333-8333-333333333333",
                "resourceVersion": "123",
                "managedFields": [{"manager": "kubelet"}],
                "ownerReferences": [{"kind": "Node", "name": NODE, "uid": NODE_UID}],
                "annotations": {
                    "kubernetes.io/config.source": "file",
                    "kubernetes.io/config.mirror": "original-mirror-hash",
                    "kubeadm.kubernetes.io/kube-apiserver.advertise-address.endpoint": (
                        "172.18.0.2:6443"
                    ),
                },
                "labels": {"component": "kube-apiserver", "tier": "control-plane"},
            },
            "spec": {
                "nodeName": NODE,
                "hostNetwork": True,
                "priorityClassName": "system-node-critical",
                "priority": 2000001000,
                "schedulerName": "default-scheduler",
                "serviceAccountName": "default",
                "automountServiceAccountToken": True,
                "tolerations": [{"operator": "Exists"}],
                "enableServiceLinks": True,
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [
                    {
                        "name": "kube-apiserver",
                        "image": "registry.k8s.io/kube-apiserver:v1.35.0",
                        "command": ["kube-apiserver", "--advertise-address=172.18.0.2"],
                        "ports": [{"name": "probe-port", "containerPort": 6443, "protocol": "TCP"}],
                        "startupProbe": {
                            "httpGet": {"scheme": "HTTPS", "path": "/livez", "port": "probe-port"}
                        },
                        "livenessProbe": {
                            "httpGet": {"scheme": "HTTPS", "path": "/livez", "port": "probe-port"}
                        },
                        "readinessProbe": {
                            "httpGet": {"scheme": "HTTPS", "path": "/readyz", "port": "probe-port"}
                        },
                        "resources": {"requests": {"cpu": "250m"}},
                        "volumeMounts": [
                            {
                                "name": "k8s-certs",
                                "mountPath": "/etc/kubernetes/pki",
                                "readOnly": True,
                            },
                            {
                                "name": "kube-api-access-test",
                                "mountPath": "/var/run/secrets/kubernetes.io",
                            },
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "k8s-certs",
                        "hostPath": {
                            "path": "/etc/kubernetes/pki",
                            "type": "DirectoryOrCreate",
                        },
                    },
                    {"name": "kube-api-access-test", "projected": {"sources": []}},
                ],
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    return tmp_path


def state(lab):
    return json.loads((lab / "state.json").read_text())


def update(lab, **values):
    current = state(lab)
    current.update(values)
    (lab / "state.json").write_text(json.dumps(current))


def calls(lab):
    path = lab / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def invoke(lab, *extra, xtrace=False, path=None, shell=None, interrupt=None, home=None):
    timeout = shutil.which("gtimeout") or shutil.which("timeout")
    if timeout is None:
        pytest.fail("GNU coreutils timeout is required for node-side deadline tests")
    environment = {
        **os.environ,
        "FAKE_ROOT": str(lab),
        "FAKE_GNU_TIMEOUT": timeout,
        "PATH": path or str(lab / "bin") + os.pathsep + os.environ["PATH"],
        "DOCKER_HOST": "tcp://foreign:2375",
        "DOCKER_CONTEXT": "colima",
        "DOCKER_CONFIG": "/foreign",
        "KUBECONFIG": "/foreign/global-config",
        "KUBERNETES_MASTER": "https://foreign",
        "HTTPS_PROXY": "https://foreign",
        **({"HOME": str(home)} if home else {}),
    }
    command = [
        shell or shutil.which("bash"),
        *(["-x"] if xtrace else []),
        str(SCRIPT),
        "--cluster",
        CLUSTER,
        "--node",
        NODE,
        "--kubeconfig",
        str(lab / "selected.kubeconfig"),
        "--context",
        CONTEXT,
        *extra,
    ]
    if interrupt is None:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=40,
        )
    with subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            deadline = time.monotonic() + 20
            while not (lab / "interrupt-ready").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("Helper did not reach the owned probe")
                time.sleep(0.05)
            os.killpg(process.pid, interrupt)
            stdout, stderr = process.communicate(timeout=35)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def test_real_bash_and_node_shell_install_without_host_key_or_admission_fields(node_lab):
    result = invoke(node_lab)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "cluster": CLUSTER,
        "node": NODE,
        "nodeId": "a" * 64,
        "clusterUID": CLUSTER_UID,
        "keyStatus": "created",
        "provider": "aescbc",
        "keyName": "local-key",
        "syntheticCiphertextVerified": True,
    }
    profile = node_lab / "node/etc/kubernetes/radplanes/encryption.yaml"
    config = json.loads(profile.read_text())
    assert config["resources"] == [
        {
            "resources": ["secrets"],
            "providers": [
                {"aescbc": {"keys": [{"name": "local-key", "secret": SYNTHETIC_KEY}]}},
                {"identity": {}},
            ],
        }
    ]
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile.parent.stat().st_mode) == 0o700
    assert not (node_lab / "node/run/radplanes-encryption.lock").exists()
    manifest = json.loads(
        (node_lab / "node/etc/kubernetes/manifests/kube-apiserver.yaml").read_text()
    )
    assert manifest["metadata"]["name"] == "kube-apiserver"
    assert set(manifest["metadata"]) == {"name", "namespace", "labels", "annotations"}
    assert all(
        key.startswith("kubeadm.kubernetes.io/") for key in manifest["metadata"]["annotations"]
    )
    assert set(manifest["spec"]) == {
        "containers",
        "hostNetwork",
        "priorityClassName",
        "securityContext",
        "volumes",
    }
    assert "status" not in manifest
    assert manifest["spec"]["volumes"][-1] == {
        "name": "radplanes-encryption",
        "hostPath": {"path": PROFILE_PATH, "type": "File"},
    }
    assert all(
        not item["name"].startswith("kube-api-access-") for item in manifest["spec"]["volumes"]
    )
    container = manifest["spec"]["containers"][0]
    assert container["command"][-1] == "--encryption-provider-config=" + PROFILE_PATH
    assert container["command"][1] == "--advertise-address=172.18.0.2"
    assert container["ports"] == [{"name": "probe-port", "containerPort": 6443, "protocol": "TCP"}]
    for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
        assert container[probe]["httpGet"]["port"] == container["ports"][0]["name"]
    assert state(node_lab)["probe_deletes"] == 1
    assert "probe" not in state(node_lab)
    assert SYNTHETIC_KEY not in result.stdout + result.stderr
    assert all(path.is_relative_to(node_lab / "node") for path in node_lab.rglob("encryption.yaml"))


def test_rerun_verifies_same_key_and_does_not_rewrite_manifest(node_lab):
    first = invoke(node_lab)
    assert first.returncode == 0, first.stderr
    directory = node_lab / "node/etc/kubernetes"
    files = [
        directory / "radplanes/encryption.yaml",
        directory / "radplanes/encryption.owner",
        directory / "manifests/kube-apiserver.yaml",
    ]
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
    second = invoke(node_lab)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["keyStatus"] == "existing"
    assert before == [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
    assert (node_lab / "key-generations").read_text().splitlines() == ["generated"]
    assert state(node_lab)["manifest_writes"] == 1
    assert state(node_lab)["key_checks"] == 2
    assert state(node_lab)["probe_deletes"] == 2


@pytest.mark.parametrize(
    "change", ["key", "owner", "permissions", "file-owner", "missing-owner", "symlink"]
)
def test_rerun_refuses_unowned_or_changed_material_without_rotation(node_lab, change):
    assert invoke(node_lab).returncode == 0
    directory = node_lab / "node/etc/kubernetes/radplanes"
    if change == "key":
        path = directory / "encryption.yaml"
        path.write_text(
            path.read_text().replace(SYNTHETIC_KEY, base64.b64encode(b"B" * 32).decode())
        )
    elif change == "owner":
        (directory / "encryption.owner").write_text("foreign ownership")
    elif change == "permissions":
        (directory / "encryption.yaml").chmod(0o644)
    elif change == "file-owner":
        update(node_lab, foreign_file_owner=True)
    elif change == "symlink":
        profile = directory / "encryption.yaml"
        foreign = node_lab / "node/foreign-profile"
        profile.rename(foreign)
        profile.symlink_to(foreign)
    else:
        (directory / "encryption.owner").unlink()
    result = invoke(node_lab)
    assert result.returncode != 0
    assert "node_key_ownership_or_installation" in result.stderr
    assert (node_lab / "key-generations").read_text().splitlines() == ["generated"]
    assert state(node_lab)["manifest_writes"] == 1


def test_existing_directory_is_not_adopted(node_lab):
    directory = node_lab / "node/etc/kubernetes/radplanes"
    directory.mkdir(mode=0o700)
    result = invoke(node_lab)
    assert result.returncode != 0
    assert not (node_lab / "key-generations").exists()
    assert not state(node_lab).get("manifest_writes")


@pytest.mark.parametrize("change", ["cluster", "image", "port", "bind-key", "bind-manifest"])
def test_foreign_nodes_and_checkout_backing_mounts_are_refused(node_lab, change):
    inspection = state(node_lab)["inspection"]
    if change == "cluster":
        inspection["Config"]["Labels"]["io.x-k8s.kind.cluster"] = "foreign"
    elif change == "image":
        inspection["Config"]["Image"] = "kindest/node:latest"
    elif change == "port":
        inspection["NetworkSettings"]["Ports"]["6443/tcp"][0]["HostIp"] = "0.0.0.0"
    else:
        inspection["Mounts"] = [
            {
                "Type": "bind",
                "Source": "/checkout/private",
                "Destination": PROFILE_PATH
                if change == "bind-key"
                else "/etc/kubernetes/manifests",
            }
        ]
    update(node_lab, inspection=inspection)
    result = invoke(node_lab)
    assert result.returncode != 0
    assert "node_identity_mismatch" in result.stderr
    assert len(calls(node_lab)) == 1


@pytest.mark.parametrize("change", ["exec", "server", "context", "insecure"])
def test_untrusted_access_never_contacts_kubernetes(node_lab, change):
    configuration = state(node_lab)["configuration"]
    if change == "exec":
        configuration["users"][0]["user"] = {"exec": {"command": "az", "args": ["login"]}}
    elif change == "server":
        configuration["clusters"][0]["cluster"]["server"] = "https://foreign:35512"
    elif change == "context":
        configuration["current-context"] = "foreign"
    else:
        configuration["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    update(node_lab, configuration=configuration)
    result = invoke(node_lab)
    assert result.returncode != 0
    assert "unsafe_kubeconfig" in result.stderr
    assert len(calls(node_lab)) == 2


@pytest.mark.parametrize("change", ["owner", "command", "args", "mount"])
def test_wrong_apiserver_or_existing_foreign_configuration_never_generates_key(node_lab, change):
    pod = state(node_lab)["pod"]
    container = pod["spec"]["containers"][0]
    if change == "owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = CLUSTER_UID
    elif change in {"command", "args"}:
        container.setdefault(change, []).append("--encryption-provider-config=/foreign")
    else:
        container["volumeMounts"].append({"name": "foreign", "mountPath": PROFILE_PATH})
    update(node_lab, pod=pod)
    result = invoke(node_lab)
    assert result.returncode != 0
    assert not (node_lab / "key-generations").exists()


@pytest.mark.parametrize(
    "failure", ["inspect", "namespace", "digest", "key", "manifest", "create", "etcd"]
)
def test_command_failure_stops_and_redacts_diagnostics(node_lab, failure):
    update(node_lab, failure=failure)
    result = invoke(node_lab, xtrace=True)
    assert result.returncode != 0
    assert "synthetic-private-failure-value" not in result.stdout + result.stderr
    assert SYNTHETIC_KEY not in result.stdout + result.stderr
    assert "node-encryption-synthetic-proof" not in result.stdout + result.stderr
    assert "syntheticCiphertextVerified" not in result.stdout
    assert state(node_lab).get("probe_deletes", 0) == (1 if failure == "etcd" else 0)


@pytest.mark.parametrize("problem", ["plaintext", "wrong_decryption", "replace_namespace"])
def test_proof_uses_real_request_paths_and_rejects_failed_invariants(node_lab, problem):
    update(node_lab, **{problem: True})
    result = invoke(node_lab)
    assert result.returncode != 0
    assert "syntheticCiphertextVerified" not in result.stdout
    assert state(node_lab).get("probe_deletes", 0) == (0 if problem == "replace_namespace" else 1)
    if problem == "replace_namespace":
        assert "probe" not in state(node_lab)
    else:
        assert any("etcdctl" in call["args"] for call in calls(node_lab))


def test_resolved_docker_host_works_without_user_context_in_scoped_home(node_lab):
    home = node_lab / "scoped-home"
    home.mkdir()
    update(node_lab, context_unavailable=True)
    result = invoke(
        node_lab,
        "--docker-host",
        "unix:///Users/operator/.docker/run/docker.sock",
        home=home,
    )
    assert result.returncode == 0, result.stderr
    assert all(call["args"][0] == "--host" for call in calls(node_lab) if call["tool"] == "docker")


def test_restart_waits_for_actual_ready_api_and_keeps_explicit_contexts(node_lab):
    update(node_lab, ready_delay=1)
    result = invoke(node_lab, "--docker-host", "unix:///Users/operator/.docker/run/docker.sock")
    assert result.returncode == 0, result.stderr
    assert state(node_lab)["ready_requests"] >= 2
    for call in calls(node_lab):
        assert not set(call["env"]) & {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "KUBECONFIG",
            "KUBERNETES_MASTER",
            "HTTPS_PROXY",
        }
        assert "node-encryption-synthetic-proof" not in call["args"]
        assert SYNTHETIC_KEY not in " ".join(call["args"])
        if call["tool"] == "docker":
            assert call["args"][:2] == [
                "--host",
                "unix:///Users/operator/.docker/run/docker.sock",
            ]
        else:
            assert call["args"][:4] == [
                "--kubeconfig",
                str(node_lab / "selected.kubeconfig"),
                "--context",
                CONTEXT,
            ]


@pytest.mark.parametrize("tool", ["docker", "python3"])
def test_missing_tool_fails_before_any_infrastructure_call(node_lab, tool):
    (node_lab / f"bin/{tool}").unlink()
    result = invoke(node_lab, path=str(node_lab / "bin"))
    assert result.returncode != 0
    assert "required_tool_missing" in result.stderr
    assert not calls(node_lab)


def test_remote_docker_override_is_rejected(node_lab):
    result = invoke(node_lab, "--docker-host", "tcp://foreign:2375")
    assert result.returncode != 0
    assert "invalid_docker_socket" in result.stderr
    assert not calls(node_lab)


def test_concurrent_static_manifest_change_is_not_overwritten(node_lab):
    update(node_lab, concurrent_manifest_change=True)
    result = invoke(node_lab)
    assert result.returncode != 0
    assert "manifest_installation" in result.stderr
    manifest = node_lab / "node/etc/kubernetes/manifests/kube-apiserver.yaml"
    assert manifest.read_text() == (
        "kind: Pod\n# original node-owned manifest\n# concurrent edit\n"
    )
    assert not state(node_lab).get("installed")
    assert not state(node_lab).get("probe")


def test_actual_command_path_has_a_timeout(node_lab):
    update(node_lab, failure="hang")
    started = time.monotonic()
    result = invoke(node_lab)
    assert time.monotonic() - started < 38
    assert result.returncode != 0
    assert "node_inspection" in result.stderr
    assert len(calls(node_lab)) == 1
    assert not (node_lab / "key-generations").exists()


def test_node_deadlines_are_present_on_both_mutating_execs_and_bash32_rerun(node_lab):
    result = invoke(node_lab, shell="/bin/bash")
    assert result.returncode == 0, result.stderr
    mutations = [
        call["args"]
        for call in calls(node_lab)
        if call["tool"] == "docker" and "-i" in call["args"]
    ]
    assert len(mutations) == 2
    for argv in mutations:
        start = argv.index("timeout")
        assert argv[start : start + 5] == [
            "timeout",
            "--signal=TERM",
            "--kill-after=2s",
            "20s",
            "sh",
        ]
    assert any(call["args"][-2:] == ["timeout", "--version"] for call in calls(node_lab))
    result = invoke(node_lab, shell="/bin/bash")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["keyStatus"] == "existing"
    assert state(node_lab)["manifest_writes"] == 1


def test_missing_node_timeout_blocks_mutation(node_lab):
    update(node_lab, missing_node_timeout=True)
    result = invoke(node_lab)
    assert result.returncode != 0
    assert "node_timeout_required" in result.stderr
    assert not (node_lab / "key-generations").exists()
    assert not state(node_lab).get("manifest_writes")


@pytest.mark.parametrize("replacement", ["replace_probe", "replace_before_delete"])
def test_cleanup_preserves_foreign_replacements_and_original_failure(node_lab, replacement):
    update(node_lab, plaintext=True, **{replacement: True})
    result = invoke(node_lab)
    assert result.returncode == 1
    assert "Node encryption failed: ciphertext_proof" in result.stderr
    assert "Node encryption cleanup failed:" in result.stderr
    current = state(node_lab)
    assert current["probe"]["metadata"]["uid"] == "99999999-9999-4999-8999-999999999999"
    assert not current.get("probe_deletes")
    if replacement == "replace_before_delete":
        assert current["delete_options"]["preconditions"] == {
            "uid": "55555555-5555-4555-8555-555555555555",
        }


@pytest.mark.parametrize("failure", ["cleanup_read_failure", "cleanup_delete_failure"])
def test_cleanup_failure_is_reported_without_masking_original_error(node_lab, failure):
    update(node_lab, plaintext=True, **{failure: True})
    result = invoke(node_lab)
    assert result.returncode == 1
    assert "Node encryption failed: ciphertext_proof" in result.stderr
    assert "Node encryption cleanup failed:" in result.stderr
    assert "synthetic-private-failure-value" not in result.stdout + result.stderr
    assert not state(node_lab).get("probe_deletes")
    assert "probe" in state(node_lab)


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_interrupt_attempts_owned_probe_cleanup_and_preserves_status(node_lab, number):
    update(node_lab, interrupt_etcd=True)
    result = invoke(node_lab, shell="/bin/bash", interrupt=number)
    assert result.returncode == 128 + number, result.stderr
    assert state(node_lab)["probe_deletes"] == 1
    assert state(node_lab)["delete_options"]["preconditions"] == {
        "uid": "55555555-5555-4555-8555-555555555555",
    }
    assert "probe" not in state(node_lab)
    assert "syntheticCiphertextVerified" not in result.stdout


def test_failed_interrupt_cleanup_does_not_replace_signal_exit_status(node_lab):
    update(node_lab, interrupt_etcd=True, cleanup_delete_failure=True)
    result = invoke(node_lab, shell="/bin/bash", interrupt=signal.SIGTERM)
    assert result.returncode == 143, result.stderr
    assert "Node encryption cleanup failed:" in result.stderr
    assert "probe" in state(node_lab)


def running(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


@pytest.mark.parametrize("leader_exits", [False, True])
def test_one_second_deadline_kills_children_holding_stdout(tmp_path, leader_exits):
    source = SCRIPT.read_text()
    bounded = "bounded() {" + source.split("bounded() {", 1)[1].split("\ndocker_cli()", 1)[0]
    pid_file = tmp_path / "child.pid"
    child_code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(3)"
    parent_code = (
        "import subprocess,sys,time;"
        "from pathlib import Path;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        f"Path({str(pid_file)!r}).write_text(str(child.pid));"
        + ("sys.exit(0)" if leader_exits else "time.sleep(3)")
    )
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(20)"])
    try:
        started = time.monotonic()
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                bounded + '\nbounded 1 "$@"',
                "bounded-regression",
                sys.executable,
                "-c",
                parent_code,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert time.monotonic() - started < 2.5
        assert result.returncode == 124, result.stderr
        assert result.stdout == ""
        assert not running(int(pid_file.read_text()))
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)
