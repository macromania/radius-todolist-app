import base64
import io
import json
import os
import subprocess
import tarfile
from unittest.mock import MagicMock, Mock

import pytest
import yaml
from local_support import ROOT, bootstrap, child, common, gate, images, prepare, server


@pytest.mark.parametrize(
    "module,args",
    [
        (bootstrap, ["create"]),
        (bootstrap, ["install"]),
        (images, ["build"]),
        (images, ["inspect"]),
        (gate, ["run"]),
        (gate, ["delete"]),
    ],
)
def test_preview_entrypoints_never_construct_live_commands(module, args, monkeypatch):
    commands = Mock(side_effect=AssertionError("Live command in preview"))
    monkeypatch.setattr(module, "Commands", commands)
    assert module.main(args) == 0
    commands.assert_not_called()


def test_isolated_home_and_every_context_are_explicit(local_state, monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/unrelated/kubeconfig")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "must-not-inherit")
    monkeypatch.setenv("TF_CLI_ARGS", "must-not-inherit")
    commands = common.Commands()
    assert commands.env["HOME"] == str(local_state / "home")
    assert commands.env["KUBECONFIG"] == str(local_state / "home/.kube/config")
    assert "AZURE_CLIENT_SECRET" not in commands.env
    assert "TF_CLI_ARGS" not in commands.env
    assert common.CONTEXT in common.kube("get", "nodes")
    assert common.CONTEXT == common.MANAGEMENT
    assert child.CONTEXT == common.CHILD
    assert "--kubeconfig" in common.kube("get", "nodes")
    assert str(local_state / "radius.yaml") in common.rad("group", "list")
    assert common.MANAGEMENT in common.rad("group", "list")
    assert common.DOCKER_HOST in common.docker("info")


def test_private_files_reject_symlinks_and_public_state(local_state, tmp_path):
    common.write_private(local_state / "nested/record.json", {"safe": True})
    assert (local_state / "nested").stat().st_mode & 0o777 == 0o700
    assert (local_state / "nested/record.json").stat().st_mode & 0o777 == 0o600
    target = tmp_path / "unrelated"
    target.write_text("do not change")
    link = local_state / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        common.write_private(link, "overwritten")
    assert target.read_text() == "do not change"
    (local_state / "nested").chmod(0o755)
    with pytest.raises(common.LocalError, match="private"):
        common.write_private(local_state / "nested/new.json", {})


def test_command_failures_are_private_and_timeouts_are_not_cancellation(
    local_state,
    monkeypatch,
    capsys,
):
    commands = common.Commands()
    monkeypatch.setattr(
        common.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                ["rad"],
                1,
                "credential-output",
                "credential-error",
            )
        ),
    )
    with pytest.raises(common.LocalError, match="private details"):
        commands.run(["rad", "--config", "unused"])
    assert "credential" not in capsys.readouterr().out
    diagnostic = next((local_state / "diagnostics").iterdir())
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(
        common.subprocess,
        "run",
        Mock(
            side_effect=subprocess.TimeoutExpired("rad", 3, output="credential-output"),
        ),
    )
    with pytest.raises(common.LocalError, match="remote execution may still"):
        commands.run(["rad"], timeout=3)


@pytest.fixture
def native_transport(local_state, monkeypatch):
    import httpx
    from kubernetes import config

    prepare.prepare()
    settings = {"status": 202, "location": "https://127.0.0.1:35495/redirected", "error": None}
    requests = []
    options = {}
    context = MagicMock()
    original_client = httpx.Client

    def configure(**kwargs):
        configuration = kwargs["client_configuration"]
        configuration.host = "https://127.0.0.1:35495"
        configuration.verify_ssl = True
        configuration.ssl_ca_cert = str(local_state / "client/ca")
        configuration.cert_file = str(local_state / "client/cert")
        configuration.key_file = str(local_state / "client/key")

    def respond(request):
        requests.append(request)
        if settings["error"]:
            raise settings["error"]
        if request.url.path == "/redirected":
            return httpx.Response(200)
        return httpx.Response(settings["status"], headers={"Location": settings["location"]})

    def client_factory(**kwargs):
        options.update(kwargs)
        return original_client(
            transport=httpx.MockTransport(respond),
            follow_redirects=kwargs["follow_redirects"],
            trust_env=kwargs["trust_env"],
            timeout=kwargs["timeout"],
        )

    loader = Mock(side_effect=configure)
    ssl_factory = Mock(return_value=context)
    monkeypatch.setattr(config, "load_kube_config", loader)
    monkeypatch.setattr(common.ssl, "create_default_context", ssl_factory)
    monkeypatch.setattr(httpx, "Client", client_factory)
    return settings, requests, options, loader, context, ssl_factory


def test_native_radius_creation_uses_json_and_private_explicit_tls_configuration(
    local_state, native_transport
):
    _, requests, options, loader, context, ssl_factory = native_transport
    common.Commands().create_cluster_resource()
    assert loader.call_args.kwargs["config_file"] == str(local_state / "home/.kube/config")
    assert loader.call_args.kwargs["context"] == common.CONTEXT
    assert loader.call_args.kwargs["persist_config"] is False
    assert loader.call_args.kwargs["temp_file_path"] == str(local_state / "client")
    ssl_factory.assert_called_once_with(cafile=str(local_state / "client/ca"))
    context.load_cert_chain.assert_called_once_with(
        str(local_state / "client/cert"), str(local_state / "client/key")
    )
    assert options["verify"] is context
    assert options["follow_redirects"] is False and options["trust_env"] is False
    assert options["timeout"].connect == 5 and options["timeout"].read == 60
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "PUT"
    assert request.url.path == "/apis/api.ucp.dev/v1alpha3" + common.RESOURCE_ID
    assert request.url.params["api-version"] == "2025-08-01-preview"
    assert request.headers["content-type"] == request.headers["accept"] == "application/json"
    assert json.loads(request.content) == json.loads(
        (local_state / "prepared/child.json").read_text()
    )


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize(
    "location", ["https://127.0.0.1:35495/redirected", "https://foreign.invalid/redirected"]
)
def test_native_creation_never_follows_redirects_or_replays_put(native_transport, status, location):
    settings, requests, *_ = native_transport
    settings.update(status=status, location=location)
    with pytest.raises(common.LocalError, match=f"HTTP status {status}"):
        common.Commands().create_cluster_resource()
    assert len(requests) == 1


def test_native_creation_transport_failure_is_not_retried(native_transport):
    import httpx

    settings, requests, *_ = native_transport
    settings["error"] = httpx.ReadError("disconnected")
    with pytest.raises(common.LocalError, match="transport failed"):
        common.Commands().create_cluster_resource()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "host,verify_ssl",
    [("https://foreign.invalid", True), ("https://127.0.0.1:35495", False)],
)
def test_native_radius_creation_rejects_changed_endpoint_or_disabled_tls(
    local_state, monkeypatch, host, verify_ssl
):
    import httpx
    from kubernetes import config

    def configure(**kwargs):
        kwargs["client_configuration"].host = host
        kwargs["client_configuration"].verify_ssl = verify_ssl

    monkeypatch.setattr(config, "load_kube_config", configure)
    api_class = Mock()
    monkeypatch.setattr(httpx, "Client", api_class)
    with pytest.raises(common.LocalError, match="verified management endpoint"):
        common.Commands().create_cluster_resource()
    api_class.assert_not_called()


def test_module_archive_is_deterministic_and_allowlisted(local_state):
    first, second = prepare.module_archive(), prepare.module_archive()
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
        assert archive.getnames() == list(prepare.MODULE_FILES)
        assert all(entry.isfile() and entry.mode == 0o644 for entry in archive)
        assert (
            b'provider "registry.terraform.io/tehcyx/kind"'
            in archive.extractfile(
                ".terraform.lock.hcl",
            ).read()
        )
    result = prepare.prepare()
    assert result["liveStatus"] == "not-run"
    assert (local_state / "prepared/module.tar.gz").read_bytes() == first
    env = json.loads((local_state / "prepared/environment.json").read_text())["properties"]
    recipe = env["recipes"]["Demo.Platform/clusters"]["default"]
    assert recipe["templatePath"].endswith(f"/{common.digest(first)}.tar.gz")
    assert ".radius-system.svc.cluster.local:18080/" in recipe["templatePath"]
    assert set(env["recipeConfig"]["env"]) == {
        "DOCKER_HOST",
        "KIND_EXPERIMENTAL_PROVIDER",
        "KIND_EXPERIMENTAL_DOCKER_NETWORK",
    }
    configmap = json.loads((local_state / "prepared/module-server.json").read_text())["items"][0]
    assert configmap["immutable"] is True
    assert base64.b64decode(configmap["binaryData"]["archive.tar.gz"]) == first
    assert set(configmap["data"]) == {"server.py"}


@pytest.mark.parametrize("request_path", ["/", "/../credentials.json", "/archive.tar.gz?state=1"])
def test_static_module_server_has_no_directory_or_arbitrary_file_access(tmp_path, request_path):
    archive = tmp_path / "archive"
    archive.write_bytes(b"non-secret-module")
    handler = server.handler(archive, common.digest(archive.read_bytes()), module_root=tmp_path)
    instance = object.__new__(handler)
    instance.path = request_path
    instance.send_error = Mock()
    instance.do_GET()
    instance.send_error.assert_called_once_with(404)


def test_static_module_server_checks_bytes_and_serves_exact_hash(tmp_path):
    archive = tmp_path / "archive"
    archive.write_bytes(b"non-secret-module")
    with pytest.raises(ValueError, match="digest"):
        server.handler(archive, "wrong", module_root=tmp_path)
    sha = common.digest(archive.read_bytes())
    handler = server.handler(archive, sha, module_root=tmp_path)
    instance = object.__new__(handler)
    instance.path, instance.wfile = f"/{sha}.tar.gz", io.BytesIO()
    instance.send_response, instance.send_header, instance.end_headers = Mock(), Mock(), Mock()
    instance.do_GET()
    assert instance.wfile.getvalue() == b"non-secret-module"
    instance.send_response.assert_called_once_with(200)


def test_management_and_executor_mount_only_the_socket_and_protected_encryption_file():
    config = bootstrap.management_config("/project/.state/local/key", "/var/run/docker.sock")
    mounts = config["nodes"][0]["extraMounts"]
    assert {m["hostPath"] for m in mounts} == {
        "/var/run/docker.sock",
        "/project/.state/local/key",
    }
    assert config["networking"] == {"apiServerAddress": "127.0.0.1", "apiServerPort": 35495}
    assert config["nodes"][0]["extraPortMappings"][0]["hostPort"] == 35490
    patch = yaml.safe_load(config["nodes"][0]["kubeadmConfigPatches"][0])
    assert patch["apiVersion"] == "kubeadm.k8s.io/v1beta3"
    assert patch["apiServer"]["extraArgs"] == {
        "encryption-provider-config": "/etc/kubernetes/radplanes/encryption.yaml"
    }
    overlay = yaml.safe_load((ROOT / "operations/local/dynamic-rp-overlay.yaml").read_text())
    assert overlay["metadata"] == {"name": "dynamic-rp", "namespace": "radius-system"}
    spec = overlay["spec"]["template"]["spec"]
    assert spec["volumes"] == [
        {
            "name": "local-docker",
            "hostPath": {
                "path": "/run/radplanes/docker.sock",
                "type": "Socket",
            },
        }
    ]
    assert spec["containers"][0]["securityContext"]["runAsUser"] == 65532
    assert "command" not in spec["containers"][0]
    assert spec["securityContext"]["fsGroup"] == 65532
    assert {m["name"] for m in spec["initContainers"][0]["volumeMounts"]} == {"terraform"}
    with pytest.raises(common.LocalError):
        bootstrap.management_config("/safe", "/Users/operator/.docker")


def test_reserve_all_ten_ports_before_bootstrap_without_binding_in_this_test(monkeypatch):
    sockets = []

    def socket_factory(*args):
        sock = Mock()
        sockets.append(sock)
        return sock

    monkeypatch.setattr(bootstrap.socket, "socket", socket_factory)
    held = bootstrap.reserve_ports()
    assert held == sockets
    assert [s.bind.call_args.args[0] for s in sockets] == [
        ("127.0.0.1", p) for p in range(35490, 35500)
    ]
    for sock in held:
        sock.close()


def test_bootstrap_rejects_existing_management_before_host_kind(local_state, monkeypatch):
    monkeypatch.setattr(bootstrap, "require_inspection", Mock())
    commands = Mock()
    commands.run.return_value = "existing-node"
    with pytest.raises(common.LocalError, match="already started"):
        bootstrap.create(commands, "/var/run/docker.sock")
    assert all(call.args[0][0] != "kind" for call in commands.run.call_args_list)


def test_encryption_proof_reads_real_etcd_value_not_config_metadata():
    commands = Mock()
    commands.json.return_value = {
        "kvs": [
            {
                "value": base64.b64encode(b"k8s:enc:aescbc:v1:local-key:ciphertext").decode(),
            }
        ]
    }
    bootstrap.verify_encryption(commands, "radius-system", "test")
    args = commands.json.call_args.args[0]
    assert "etcdctl" in args and "get" in args
    assert "/registry/secrets/radius-system/test" in args
    commands.json.return_value = {"kvs": [{"value": base64.b64encode(b"plaintext").decode()}]}
    with pytest.raises(common.LocalError, match="not encrypted"):
        bootstrap.verify_encryption(commands, "radius-system", "test")


def test_child_bootstrap_run_path_verifies_tls_and_creates_workload(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    access = tmp_path / "access"
    password = tmp_path / "password"
    access.write_text(
        json.dumps(
            {
                "current-context": child.CONTEXT,
                "clusters": [
                    {
                        "name": child.CONTEXT,
                        "cluster": {
                            "server": "https://172.18.0.3:6443",
                            "tls-server-name": child.CHILD,
                            "certificate-authority-data": "offline-test-ca",
                        },
                    }
                ],
            }
        )
    )
    password.write_text("offline-test-password")
    monkeypatch.setattr(child, "WORK", work)
    monkeypatch.setattr(child, "KUBECONFIG", work / "home/.kube/config")
    monkeypatch.setattr(child, "ACCESS", access)
    monkeypatch.setattr(child, "PASSWORD", password)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "--raw=/readyz" in args:
            return "ok\n"
        if "logs" in args:
            return "child-to-parent-password-authenticated\n"
        return ""

    monkeypatch.setattr(child, "run", run)
    negative = Mock(return_value=subprocess.CompletedProcess([], 1, "", "x509: name mismatch"))
    monkeypatch.setattr(child.subprocess, "run", negative)
    monkeypatch.setenv("HOME", os.environ["HOME"])
    monkeypatch.setenv("KUBECONFIG", "test")
    result = child.bootstrap(
        {
            "runId": "abcdef012345",
            "childAddress": "172.18.0.3",
            "parentAddress": "172.18.0.2",
            "postgresImage": common.POSTGRES_IMAGE,
            "envoyImage": common.ENVOY_IMAGE,
        }
    )
    assert result["childTLS"] and result["radiusWorkload"] and result["childRadius"]
    assert "--tls-server-name=not-the-child.invalid" in negative.call_args.args[0]
    assert any("install" in args and "rad" == args[0] for args, _ in calls)
    assert any("Applications.Core/containers" in args for args, _ in calls)
    assert all("kind" != args[0] and "docker" != args[0] for args, _ in calls)
    for args, _ in calls:
        if args[0] == "kubectl":
            assert child.CONTEXT in args and "--kubeconfig" in args
        else:
            assert "--config" in args
    fixture_call = next(
        kwargs["data"] for args, kwargs in calls if args[-3:] == ["create", "-f", "-"]
    )
    probe = next(obj for obj in fixture_call["items"] if obj["kind"] == "Job")
    command = probe["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "deliberately-wrong" in command and "password authentication failed" in command
    assert "offline-test-password" not in command


def test_image_build_is_native_and_does_not_suppress_output(monkeypatch):
    commands = Mock()
    monkeypatch.setattr(images, "architecture", Mock(return_value="arm64"))
    images.build(commands)
    assert commands.run.call_count == 2
    for call in commands.run.call_args_list:
        assert call.kwargs["visible"] is True
        assert "linux/arm64" in call.args[0]
        assert str(ROOT / "images/radius-kind") == call.args[0][-1]


def test_postgres_and_bootstrap_fixtures_do_not_mount_docker_or_publish_postgres():
    objects = gate.parent_postgres("abcdef012345", "offline-secret")
    service = next(o for o in objects if o["kind"] == "Service")
    assert service["spec"]["ports"] == [{"port": 5432, "targetPort": 5432, "nodePort": 31543}]
    deployment = next(o for o in objects if o["kind"] == "Deployment")
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    assert next(e for e in env if e["name"] == "POSTGRES_PASSWORD")["valueFrom"]
    job = gate.bootstrap_job("abcdef012345", "172.18.0.2", "172.18.0.3")[-1]
    spec = job["spec"]["template"]["spec"]
    assert not spec["automountServiceAccountToken"]
    assert spec["containers"][0]["command"][-1] == "--execute"
    assert all("hostPath" not in v for v in spec["volumes"])
    assert spec["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert "offline-secret" not in json.dumps(job)


def test_new_sources_do_not_change_plane_declarations_or_supply_azure_operations():
    for path in (ROOT / "operations/local").glob("*.py"):
        text = path.read_text()
        assert '["az",' not in text
        assert '"--insecure-skip-tls-verify"' not in text
    recipe = ROOT / "infra/radius/recipes/local/cluster"
    assert "timeouts {" not in (recipe / "main.tf").read_text()
    assert "kind_cluster.child.kubeconfig" not in (recipe / "outputs.tf").read_text()
    assert "local.pod_access" not in (recipe / "outputs.tf").read_text()
    assert "shared-control" in (recipe / "variables.tf").read_text()
    assert "TF_VAR_" not in (ROOT / "operations/local/prepare.py").read_text()
