"""Small, explicitly scoped command helpers for the local feasibility gate."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import subprocess
import time
from pathlib import Path

from docker_desktop import docker_host

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".state/local"
MANAGEMENT = "radplanes-local-management"
CHILD = "radplanes-local-shared-control"
CONTEXT = MANAGEMENT
NODE_IMAGE = (
    "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
)
RADIUS_IMAGE = (
    "ghcr.io/radius-project/dynamic-rp:0.60@sha256:"
    "225dcb42382cc8f83fa1eda22e2a193fce9a76428af39cb67e9033172ecd4783"
)
PYTHON_IMAGE = (
    "docker.io/library/python:3.13.12-alpine3.23@sha256:"
    "bb1f2fdb1065c85468775c9d680dcd344f6442a2d1181ef7916b60a623f11d40"
)
POSTGRES_IMAGE = (
    "docker.io/library/postgres:17.8-alpine3.23@sha256:"
    "3430fe182f5065a6ea505c3d432d2c7fff18fbab954df8f277c1dbf4c70124af"
)
ENVOY_IMAGE = (
    "docker.io/envoyproxy/envoy:v1.37.1@sha256:"
    "29496a88fba9c4c9cdef4afe8fec70f536c5ba111b1c2bddbc5436b091ceca33"
)
NAMESPACE = "radplanes-local-access"
GROUP = "radplanes-local"
ENVIRONMENT = "cluster-gate"
SCOPE = f"/planes/radius/local/resourceGroups/{GROUP}/providers"
ENVIRONMENT_ID = f"{SCOPE}/Applications.Core/environments/{ENVIRONMENT}"
RESOURCE_ID = f"{SCOPE}/Demo.Platform/clusters/shared-control"
ACCESS_SECRET = f"{CHILD}-access"


class LocalError(RuntimeError):
    """An explicit operator failure, with no automatic repair or adoption."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_dir(path: Path) -> Path:
    if not path.is_relative_to(STATE):
        raise LocalError("Local state must stay under the project .state/local directory")
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise LocalError("Symlinks are not permitted in local state paths")
    STATE.parent.mkdir(exist_ok=True, mode=0o700)
    current = STATE
    for part in ("", *path.relative_to(STATE).parts):
        current = current / part
        current.mkdir(exist_ok=True, mode=0o700)
        if current.stat().st_mode & 0o077:
            raise LocalError(f"Local state directory is not private: {current}")
    return path


def write_private(path: Path, value: bytes | str | dict | list) -> None:
    private_dir(path.parent)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, indent=2) + "\n"
    if isinstance(value, str):
        value = value.encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value)


def image_names() -> dict[str, str]:
    revision = digest((ROOT / "images/radius-kind/Dockerfile").read_bytes())[:16]
    return {
        target: f"localhost/radplanes-radius-kind-{target}:{revision}"
        for target in ("executor", "operator")
    }


def environment() -> dict[str, str]:
    home = private_dir(STATE / "home")
    private_dir(home / ".kube")
    # Do not inherit cloud credentials, Terraform overrides, proxies, or CLI defaults.
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "KUBECONFIG": str(home / ".kube/config"),
        "DOCKER_HOST": docker_host(),
        "KIND_EXPERIMENTAL_PROVIDER": "docker",
        "KIND_EXPERIMENTAL_DOCKER_NETWORK": "kind",
        "LC_ALL": "C",
    }


def kube(*args: str) -> list[str]:
    return [
        "kubectl",
        "--kubeconfig",
        str(STATE / "home/.kube/config"),
        "--context",
        CONTEXT,
        "--request-timeout=30s",
        *args,
    ]


def rad(*args: str, workspace: bool = True) -> list[str]:
    command = ["rad", "--config", str(STATE / "radius.yaml"), *args]
    if workspace:
        command += ["--workspace", MANAGEMENT]
    return command


def docker(*args: str) -> list[str]:
    return ["docker", "--host", docker_host(), *args]


class Commands:
    def __init__(self) -> None:
        self.env = environment()
        self.sequence = 0
        self.deadline: float | None = None

    def run(
        self,
        args: list[str],
        *,
        data: str | None = None,
        timeout: int = 120,
        visible: bool = False,
        expected_codes: tuple[int, ...] = (0,),
    ) -> str:
        self.sequence += 1
        if self.deadline is not None:
            remaining = int(self.deadline - time.monotonic())
            if remaining < 1:
                raise LocalError("Gate reporting deadline exceeded; remote execution may continue")
            timeout = min(timeout, remaining)
        try:
            result = subprocess.run(
                args,
                input=data,
                text=True,
                env=self.env,
                cwd=ROOT,
                capture_output=not visible,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise LocalError(
                f"{args[0]} exceeded {timeout}s; remote execution may still be running. "
                "Do not retry creation or adopt a partial cluster."
            ) from None
        if result.returncode not in expected_codes:
            path = STATE / "diagnostics" / f"command-{os.getpid()}-{self.sequence}.txt"
            write_private(path, (result.stderr or "") + (result.stdout or ""))
            raise LocalError(
                f"{args[0]} failed (exit {result.returncode}); private details: {path}"
            )
        return result.stdout or ""

    def json(self, args: list[str], **kwargs) -> dict | list:
        return json.loads(self.run(args, **kwargs))

    def create_cluster_resource(self) -> None:
        import httpx
        from kubernetes import client, config
        from kubernetes.config.config_exception import ConfigException

        configuration = client.Configuration()
        try:
            config.load_kube_config(
                config_file=str(STATE / "home/.kube/config"),
                context=CONTEXT,
                client_configuration=configuration,
                persist_config=False,
                temp_file_path=str(private_dir(STATE / "client")),
            )
            if not configuration.verify_ssl or configuration.host != "https://127.0.0.1:35495":
                raise LocalError("The Radius client must use the verified management endpoint")
            if not all(
                isinstance(path, str) and Path(path).resolve().is_relative_to(STATE / "client")
                for path in (
                    configuration.ssl_ca_cert,
                    configuration.cert_file,
                    configuration.key_file,
                )
            ):
                raise LocalError("Management client certificates must use private project files")
            context = ssl.create_default_context(cafile=configuration.ssl_ca_cert)
            context.load_cert_chain(configuration.cert_file, configuration.key_file)
            with httpx.Client(
                verify=context,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(60, connect=5),
            ) as api:
                response = api.put(
                    configuration.host + "/apis/api.ucp.dev/v1alpha3" + RESOURCE_ID,
                    params={"api-version": "2025-08-01-preview"},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    json=json.loads((STATE / "prepared/child.json").read_text()),
                )
            if response.status_code not in (200, 201, 202):
                raise LocalError(f"Radius creation failed with HTTP status {response.status_code}")
        except (ConfigException, httpx.HTTPError, ssl.SSLError):
            raise LocalError("The authenticated Radius Kubernetes transport failed") from None

    def apply(self, objects: dict | list) -> None:
        payload = (
            objects
            if isinstance(objects, dict)
            else {
                "apiVersion": "v1",
                "kind": "List",
                "items": objects,
            }
        )
        self.run(kube("apply", "-f", "-"), data=json.dumps(payload))


def node_address(node: dict, name: str) -> str:
    import ipaddress

    if node["Name"] != f"/{name}-control-plane":
        raise LocalError("Unexpected Docker node name")
    if node["Config"]["Labels"].get("io.x-k8s.kind.cluster") != name:
        raise LocalError("Unexpected Docker ownership label")
    address = ipaddress.IPv4Address(node["NetworkSettings"]["Networks"]["kind"]["IPAddress"])
    if not address.is_private or address.is_loopback or address.is_unspecified:
        raise LocalError("Expected a private, non-loopback kind node address")
    return str(address)


def containers(commands: Commands, name: str | None = None) -> list[str]:
    args = docker("ps", "-aq", "--no-trunc")
    if name:
        args += ["--filter", f"label=io.x-k8s.kind.cluster={name}"]
    return sorted(commands.run(args).split())


def state_secret_name() -> str:
    suffix = digest(f"{ENVIRONMENT}-{RESOURCE_ID}".lower().encode())[:40]
    return f"tfstate-default-{suffix}"
