"""Validated host-side provider context for the canonical local deployment command."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from plane_demo.management.providers.commands import Commands, write_private
from plane_demo.management.providers.credentials import StoredCredentials
from plane_demo.management.providers.local import LocalProvider, decode_access
from plane_demo.management.providers.local_artifacts import binding_inputs, prepared
from plane_demo.management.providers.local_config import LocalConfig
from plane_demo.management.providers.secret_store import CredentialScope, KubernetesCredentialStore
from plane_demo.management.provisioning import ProvisioningError
from scripts.operations.management_job import require_stopped_writer

LEASE_NAME = "management-bootstrap"


def read_profile(path: Path, context: str, config: LocalConfig) -> str:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ProvisioningError("invalid_local_kubeconfig")
        text = stream.read(65537)
    if len(text) > 65536 or context != config.allocation("management")["context"]:
        raise ProvisioningError("invalid_local_kubeconfig")
    _, ca, _ = decode_access(text, context, child=False)
    if hashlib.sha256(ca).hexdigest() != config.management_cluster["caSHA256"]:
        raise ProvisioningError("local_management_identity_mismatch")
    return text


def docker_host() -> str:
    environment = {"PATH": os.environ["PATH"], "HOME": str(Path.home())}
    result = subprocess.run(
        [
            "docker",
            "context",
            "inspect",
            "desktop-linux",
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ProvisioningError("local_docker_context_unavailable")
    try:
        host = json.loads(result.stdout)
    except ValueError:
        raise ProvisioningError("local_docker_context_unavailable") from None
    if (
        not isinstance(host, str)
        or not host.startswith("unix:///")
        or any(value in host for value in ("\n", "\r", "?", "#", "/../"))
    ):
        raise ProvisioningError("local_docker_context_unavailable")
    return host


def extract_prepared(commands: Commands, workspace: Path, inputs: dict) -> Path:
    host = docker_host()
    image = inputs["images"]["operator"]
    observed = commands.json(["docker", "--host", host, "image", "inspect", image["reference"]])
    if not isinstance(observed, list) or len(observed) != 1 or observed[0].get("Id") != image["id"]:
        raise ProvisioningError("local_operator_image_mismatch")
    container = commands.run(
        [
            "docker",
            "--host",
            host,
            "create",
            "--network",
            "none",
            "--entrypoint",
            "/bin/true",
            image["id"],
        ]
    )
    if not re.fullmatch(r"[a-f0-9]{64}", container):
        raise ProvisioningError("local_operator_container_invalid")
    directory = workspace / "prepared"
    directory.mkdir(mode=0o700)
    try:
        commands.run(
            [
                "docker",
                "--host",
                host,
                "cp",
                f"{container}:/opt/radplanes/bootstrap/.",
                str(directory),
            ]
        )
    finally:
        commands.run(["docker", "--host", host, "rm", container])
    if any(
        path.is_symlink() or not (path.is_file() or path.is_dir()) for path in directory.rglob("*")
    ):
        raise ProvisioningError("local_prepared_assets_invalid")
    try:
        bundle = prepared(directory)
    except (OSError, ValueError, KeyError, TypeError):
        raise ProvisioningError("local_prepared_assets_invalid") from None
    if bundle["revision"] != inputs["revision"] or bundle["dependencies"] != inputs["dependencies"]:
        raise ProvisioningError("local_prepared_assets_mismatch")
    return directory


@contextmanager
def local_operator_provider(
    config: LocalConfig, root: Path, *, kubeconfig: Path, context: str
) -> Iterator[LocalProvider]:
    identity = config.identity
    if identity is None or identity.environment != "local":
        raise ProvisioningError("bootstrap_identity_required")
    profile = read_profile(kubeconfig, context, config)
    scope = CredentialScope(identity.project, identity.deployment, "local")
    namespace = config.namespace("management")
    with TemporaryDirectory(prefix="plane-local-operator-") as temporary:
        workspace = Path(temporary)
        source = workspace / "home/.kube/config"
        write_private(source, profile)
        access, ca, server = decode_access(profile, context, child=False)
        certificate_directory = workspace / "certificates"
        certificate_directory.mkdir(mode=0o700)
        certificate = certificate_directory / "client.crt"
        key = certificate_directory / "client.key"
        authority = certificate_directory / "ca.crt"
        try:
            user = access["users"][0]["user"]
            write_private(authority, ca.decode("ascii"))
            write_private(
                certificate,
                base64.b64decode(user["client-certificate-data"], validate=True).decode("ascii"),
            )
            write_private(
                key, base64.b64decode(user["client-key-data"], validate=True).decode("ascii")
            )
        except (UnicodeError, ValueError, KeyError, TypeError):
            raise ProvisioningError("invalid_local_kubeconfig") from None
        settings = client.Configuration()
        settings.host = server
        settings.verify_ssl = True
        settings.ssl_ca_cert = str(authority)
        settings.cert_file = str(certificate)
        settings.key_file = str(key)
        settings.tls_server_name = access["clusters"][0]["cluster"].get("tls-server-name")
        with client.ApiClient(settings) as connection:
            core = client.CoreV1Api(connection)
            applications = client.AppsV1Api(connection)
            coordination = client.CoordinationV1Api(connection)
            try:
                owner = core.read_namespace(namespace, _request_timeout=(5, 15))
            except (ApiException, HTTPError):
                raise ProvisioningError("local_management_identity_mismatch") from None
            if owner.metadata.name != namespace or any(
                (owner.metadata.labels or {}).get(key) != value
                for key, value in scope.owner_labels().items()
            ):
                raise ProvisioningError("local_management_identity_mismatch")
            resource = (
                f"/apis/api.ucp.dev/v1alpha3/planes/radius/local/resourceGroups/{config.radius_group}"
                "/providers/Applications.Core/environments/management"
            )
            try:
                environment = connection.call_api(
                    resource,
                    "GET",
                    query_params=[("api-version", "2023-10-01-preview")],
                    response_type="object",
                    auth_settings=["BearerToken"],
                    _return_http_data_only=True,
                    _request_timeout=(5, 15),
                )
            except (ApiException, HTTPError):
                raise ProvisioningError("local_recipe_discovery_failed") from None
            try:
                inputs = binding_inputs(environment, config.resource_prefix, config.radius_group)
            except (ValueError, KeyError, TypeError):
                raise ProvisioningError("local_recipe_binding_invalid") from None
            if any(
                inputs["images"][role]
                != {
                    "reference": config.images[role],
                    "id": config.image_ids[role],
                }
                for role in ("api", "provisioner")
            ) or (identity.revision is not None and inputs["revision"] != identity.revision):
                raise ProvisioningError("local_runtime_image_mismatch")
            commands = Commands(
                root,
                state_root=workspace,
                local=True,
                contexts={config.allocation(slot)["context"] for slot in config.allocations},
            )
            assets = extract_prepared(commands, workspace, inputs)
            holder = str(uuid4())
            lease_uid = None
            labels = {**scope.owner_labels(), "plane-demo/operator": "management-deploy"}

            def read_active():
                if lease_uid is None:
                    raise ProvisioningError("management_bootstrap_not_locked")
                try:
                    current = coordination.read_namespaced_lease(
                        LEASE_NAME, namespace, _request_timeout=(5, 15)
                    )
                except (ApiException, HTTPError):
                    raise ProvisioningError("management_bootstrap_observation_failed") from None
                if (
                    current.metadata.name != LEASE_NAME
                    or current.metadata.namespace != namespace
                    or current.metadata.uid != lease_uid
                    or current.metadata.deletion_timestamp
                    or current.spec.holder_identity != holder
                    or any(
                        (current.metadata.labels or {}).get(key) != value
                        for key, value in labels.items()
                    )
                ):
                    raise ProvisioningError("management_bootstrap_owner_mismatch")
                return current

            def active() -> None:
                read_active()

            def writer():
                active()
                require_stopped_writer(applications, core, namespace)

            backend = KubernetesCredentialStore(scope, namespace, core, singleton_guard=writer)
            credentials = StoredCredentials(config, backend)
            provider = LocalProvider(
                config, root, credentials, commands, workspace=workspace, prepared_assets=assets
            )
            provider.authenticate()
            provider.connect_management()
            provider.verify_recipes()
            try:
                lease = coordination.create_namespaced_lease(
                    namespace,
                    {
                        "apiVersion": "coordination.k8s.io/v1",
                        "kind": "Lease",
                        "metadata": {"name": LEASE_NAME, "namespace": namespace, "labels": labels},
                        "spec": {"holderIdentity": holder},
                    },
                    _request_timeout=(5, 15),
                )
            except ApiException as error:
                code = (
                    "management_bootstrap_active_or_interrupted"
                    if error.status == 409
                    else "management_bootstrap_lock_failed"
                )
                raise ProvisioningError(code) from None
            except HTTPError:
                raise ProvisioningError("management_bootstrap_lock_failed") from None
            lease_uid = lease.metadata.uid
            if not lease_uid:
                raise ProvisioningError("management_bootstrap_lock_failed")
            commands.guard = active
            try:
                active()
                yield provider
            finally:
                current = read_active()
                try:
                    coordination.delete_namespaced_lease(
                        LEASE_NAME,
                        namespace,
                        body=client.V1DeleteOptions(
                            preconditions=client.V1Preconditions(
                                uid=lease_uid,
                                resource_version=current.metadata.resource_version,
                            )
                        ),
                        _request_timeout=(5, 15),
                    )
                except (ApiException, HTTPError):
                    raise ProvisioningError("management_bootstrap_unlock_failed") from None
