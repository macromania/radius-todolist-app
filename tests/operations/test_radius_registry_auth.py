import base64
import errno
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
REFRESH_TOKEN = "synthetic-acr-refresh-token"
ACCESS_TOKEN = "synthetic-repository-access-token"
REPOSITORY = "radius-recipe-staging/probe"


def prepare_credentials(workspace, host, **payload):
    docker = workspace / "docker"
    docker.mkdir(mode=0o700, exist_ok=True)
    token = workspace / "registry-token.json"
    token.write_text(json.dumps({"loginServer": host, "accessToken": REFRESH_TOKEN, **payload}))
    token.chmod(0o600)
    return subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; ROOT=$1; source "$ROOT/scripts/operations/azure/azure.shlib"; '
            "azure_registry_credentials",
            "test",
            str(ROOT),
        ],
        env={
            **os.environ,
            "AZURE_WORKSPACE": str(workspace),
            "DOCKER_CONFIG": str(docker),
            "HOST": host,
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def test_writer_creates_only_private_refresh_credentials(tmp_path):
    result = prepare_credentials(tmp_path, "synthetic.azurecr.io")
    assert result.returncode == 0, result.stderr
    config = tmp_path / "docker/config.json"
    assert json.loads(config.read_text()) == {
        "auths": {"synthetic.azurecr.io": {"identitytoken": REFRESH_TOKEN}},
    }
    assert config.stat().st_mode & 0o777 == 0o600
    assert not result.stdout
    assert REFRESH_TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        {"loginServer": "foreign.azurecr.io"},
        {"accessToken": ""},
        {"accessToken": "invalid token"},
        {"accessToken": None},
    ],
)
def test_writer_rejects_invalid_credentials_without_disclosure(tmp_path, payload):
    result = prepare_credentials(tmp_path, "synthetic.azurecr.io", **payload)
    assert result.returncode != 0 and not result.stdout
    assert "authentication response is invalid" in result.stderr
    assert REFRESH_TOKEN not in result.stderr
    assert not (tmp_path / "docker/config.json").exists()


def test_writer_does_not_replace_an_existing_config_or_follow_a_symlink(tmp_path):
    docker = tmp_path / "docker"
    docker.mkdir(mode=0o700)
    destination = docker / "config.json"
    destination.write_text('{"credsStore":"do-not-use"}')
    result = prepare_credentials(tmp_path, "synthetic.azurecr.io")
    assert result.returncode != 0
    assert destination.read_text() == '{"credsStore":"do-not-use"}'
    destination.unlink()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve")
    destination.symlink_to(unrelated)
    result = prepare_credentials(tmp_path, "synthetic.azurecr.io")
    assert result.returncode != 0 and unrelated.read_text() == "preserve"


class RegistryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def reply(self, code, body=b"", **headers):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/json")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def dispatch(self):
        parsed = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        host = self.server.registry_host
        if parsed.path == "/oauth2/token":
            if self.command == "GET":
                self.server.token_requests.append(("GET", {}))
                self.reply(401, b'{"errors":[{"code":"UNAUTHORIZED"}]}')
                return
            values = parse_qs(body.decode())
            self.server.token_requests.append(("POST", values))
            if (
                self.command != "POST"
                or values.get("grant_type") != ["refresh_token"]
                or values.get("refresh_token") != [REFRESH_TOKEN]
                or values.get("service") != [host]
            ):
                self.reply(401)
                return
            self.reply(200, json.dumps({"access_token": ACCESS_TOKEN}).encode())
            return
        if self.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
            self.reply(
                401,
                **{
                    "WWW-Authenticate": f'Bearer realm="http://{host}/oauth2/token",service="{host}"',
                },
            )
            return
        prefix = f"/v2/{REPOSITORY}/"
        if parsed.path == "/v2/":
            self.reply(200, b"{}")
        elif parsed.path == prefix + "blobs/uploads/" and self.command == "POST":
            with self.server.data_lock:
                identifier = str(len(self.server.uploads) + 1)
                self.server.uploads[identifier] = b""
            self.reply(
                202,
                **{
                    "Location": f"http://{host}{prefix}blobs/uploads/{identifier}",
                    "Docker-Upload-UUID": identifier,
                },
            )
        elif parsed.path.startswith(prefix + "blobs/uploads/") and self.command == "PUT":
            digest = parse_qs(parsed.query)["digest"][0]
            if digest != "sha256:" + hashlib.sha256(body).hexdigest():
                self.reply(400)
                return
            self.server.blobs[digest] = body
            self.reply(
                201,
                **{
                    "Location": f"http://{host}{prefix}blobs/{digest}",
                    "Docker-Content-Digest": digest,
                },
            )
        elif parsed.path.startswith(prefix + "blobs/"):
            digest = parsed.path.removeprefix(prefix + "blobs/")
            if digest in self.server.blobs:
                self.reply(200, self.server.blobs[digest], **{"Docker-Content-Digest": digest})
            else:
                self.reply(404)
        elif parsed.path.startswith(prefix + "manifests/"):
            reference = parsed.path.removeprefix(prefix + "manifests/")
            if self.command == "PUT":
                digest = "sha256:" + hashlib.sha256(body).hexdigest()
                self.server.manifests[reference] = self.server.manifests[digest] = body
                self.reply(201, **{"Docker-Content-Digest": digest})
            elif reference in self.server.manifests:
                value = self.server.manifests[reference]
                self.reply(
                    200,
                    value,
                    **{
                        "Docker-Content-Digest": "sha256:" + hashlib.sha256(value).hexdigest(),
                    },
                )
            else:
                self.reply(404)
        else:
            self.reply(404)

    do_GET = dispatch
    do_HEAD = dispatch
    do_POST = dispatch
    do_PUT = dispatch


@pytest.fixture
def local_registry():
    settings = dict(
        line.split("=", 1)
        for line in (ROOT / "ports.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    reserved = {
        int(value)
        for key, value in settings.items()
        if key.startswith("TEST_") and key.endswith("_PORT")
    }
    server = None
    for port in range(
        int(settings["TEST_PORT_BLOCK_START"]), int(settings["TEST_PORT_BLOCK_END"]) + 1
    ):
        if port in reserved:
            continue
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), RegistryHandler)
            break
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
    if server is None:
        pytest.skip("No free unassigned port in the project's disposable test block")
    server.registry_host = f"127.0.0.1:{server.server_port}"
    server.token_requests = []
    server.blobs = {}
    server.manifests = {}
    server.uploads = {}
    server.data_lock = threading.Lock()
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/v2/")
            assert connection.getresponse().status == 401
        finally:
            connection.close()
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("refresh_credential", [False, True])
def test_real_radius_uses_oauth_for_the_build_credentials(
    tmp_path, local_registry, refresh_credential
):
    radius = shutil.which("rad")
    bicep = Path(os.environ.get("RADIUS_BICEP", Path.home() / ".rad/bin/bicep"))
    if radius is None or not bicep.is_file():
        pytest.skip("The pinned Radius and Bicep tools are required for the real client probe")
    version = subprocess.run(
        [radius, "version", "--cli"], capture_output=True, text=True, check=True
    )
    assert "0.60.2" in version.stdout
    host = local_registry.registry_host
    result = prepare_credentials(tmp_path, host)
    assert result.returncode == 0, result.stderr
    if not refresh_credential:
        basic = base64.b64encode(
            f"00000000-0000-0000-0000-000000000000:{REFRESH_TOKEN}".encode()
        ).decode()
        (tmp_path / "docker/config.json").write_text(json.dumps({"auths": {host: {"auth": basic}}}))
    home = tmp_path / "radius-home"
    (home / ".rad/bin").mkdir(parents=True, mode=0o700)
    (home / ".rad/bin/bicep").symlink_to(bicep)
    template = tmp_path / "probe.bicep"
    template.write_text("output message string = 'synthetic-registry-auth-probe'\n")
    result = subprocess.run(
        [
            radius,
            "--config",
            str(tmp_path / "radius.yaml"),
            "bicep",
            "publish",
            "--file",
            str(template),
            "--target",
            f"br:{host}/{REPOSITORY}:probe",
            "--plain-http",
        ],
        env={**os.environ, "HOME": str(home), "DOCKER_CONFIG": str(tmp_path / "docker")},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert REFRESH_TOKEN not in result.stdout + result.stderr
    assert ACCESS_TOKEN not in result.stdout + result.stderr
    methods = [method for method, _ in local_registry.token_requests]
    if not refresh_credential:
        assert result.returncode != 0 and "401" in result.stdout + result.stderr
        assert methods and set(methods) == {"GET"}
        assert not local_registry.manifests
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert methods and set(methods) == {"POST"}
    assert all(
        values["grant_type"] == ["refresh_token"] for _, values in local_registry.token_requests
    )
    manifest = json.loads(local_registry.manifests["probe"])
    layer = manifest["layers"][0]
    assert layer["mediaType"] == "application/vnd.ms.bicep.module.layer.v1+json"
    compiled = json.loads(local_registry.blobs[layer["digest"]])
    assert compiled["outputs"]["message"]["value"] == "synthetic-registry-auth-probe"
