"""Non-public singleton coordinator. Interrupted work is never replayed."""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg

from plane_demo.management.providers.azure import AzureProvider
from plane_demo.management.providers.commands import Commands
from plane_demo.management.providers.credentials import StoredCredentials
from plane_demo.management.providers.discovery import read_runtime_configuration
from plane_demo.management.providers.identity import PUBLIC_KEYS, DemoConfig
from plane_demo.management.providers.local import LocalProvider
from plane_demo.management.providers.local_config import LocalConfig
from plane_demo.management.providers.secret_store import (
    CredentialScope,
    KubernetesCredentialStore,
    StoreError,
    azure_key_vault_store,
)
from plane_demo.management.provisioning import OperatorConfig, ProvisioningError, provision_pair
from plane_demo.shared.db import ProvisionerAlreadyRunning, provisioner_session

logger = logging.getLogger(__name__)


@contextmanager
def service_provider(
    config: LocalConfig | OperatorConfig,
    root: Path,
    *,
    guard: Callable[[], None] | None = None,
    writer_guard: Callable[[], None] | None = None,
):
    identity = config.identity
    if identity is None:
        raise ProvisioningError("bootstrap_identity_required")
    scope = CredentialScope(identity.project, identity.deployment, identity.environment)
    with TemporaryDirectory(prefix="plane-provisioner-") as directory, ExitStack() as clients:
        workspace = Path(directory)
        contexts = {
            config.allocation(slot)["context"]
            if isinstance(config, LocalConfig)
            else config.workspace(slot)
            for slot in config.allocations
        }
        commands = Commands(
            root, state_root=workspace, local=isinstance(config, LocalConfig), contexts=contexts
        )
        if guard is not None:
            commands.guard = guard
        check_writer = writer_guard or (lambda: commands.guard())
        if isinstance(config, LocalConfig):
            from kubernetes import client as kube_client
            from kubernetes import config as kube_config
            from kubernetes.config.config_exception import ConfigException

            configuration = kube_client.Configuration()
            try:
                kube_config.load_incluster_config(client_configuration=configuration)
            except ConfigException:
                raise ProvisioningError("in_cluster_management_access_required") from None
            api_client = clients.enter_context(kube_client.ApiClient(configuration))
            backend = KubernetesCredentialStore(
                scope,
                config.namespace("management"),
                kube_client.CoreV1Api(api_client),
                singleton_guard=check_writer,
            )
        else:
            try:
                from azure.identity import WorkloadIdentityCredential
            except ImportError:
                raise ProvisioningError("credential_store_dependency_missing") from None
            if (
                os.environ.get("AZURE_CLIENT_ID") != config.coordinator_identity["clientId"]
                or os.environ.get("AZURE_TENANT_ID") != config.foundation["tenantId"]
            ):
                raise ProvisioningError("workload_identity_mismatch")
            credential = clients.enter_context(WorkloadIdentityCredential())
            backend = azure_key_vault_store(
                scope,
                f"https://{config.foundation['vaultName']}.vault.azure.net",
                credential=credential,
                singleton_writer=True,
                singleton_guard=check_writer,
            )
            clients.callback(backend.close)
        credentials = StoredCredentials(config, backend)
        if isinstance(config, LocalConfig):
            yield LocalProvider(config, root, credentials, commands, workspace=workspace)
        else:
            yield AzureProvider(config, root, credentials, commands, workspace=workspace)


def check_session(operations) -> None:
    # This is the lock-holding connection, not a fresh health-check connection.
    operations.connection.execute("SELECT 1").fetchone()


def read_pair(operations, pair_id: str) -> dict:
    """Read logical placement on the singleton session; operation writes stay in db.py."""
    pair = operations.connection.execute(
        "SELECT pair_id,isolation,reporting_role,stage FROM management.pairs WHERE pair_id=%s",
        (pair_id,),
    ).fetchone()
    if pair is None:
        raise ProvisioningError("pair_not_found")
    return pair


def run_once(operations, provider) -> bool:
    operation = operations.claim_pending()
    if operation is None:
        return False
    stage = "starting"

    def observe(value: str) -> None:
        nonlocal stage
        stage = value
        operations.observe(operation.operation_id, stage)
        logger.info("provisioning operation=%s stage=%s", operation.operation_id, stage)

    try:
        pair = read_pair(operations, operation.pair_id)
        provision_pair(operation, provider, pair, observe)
    except psycopg.Error:
        raise
    except Exception as error:
        code = error.code if isinstance(error, ProvisioningError) else "provider_contract_failed"
        logger.error(
            "provisioning_failed operation=%s stage=%s code=%s category=%s",
            operation.operation_id,
            stage,
            code,
            type(error).__name__,
        )
        operations.observe(operation.operation_id, stage, status="failed", error_code=code)
        return True
    operations.complete(operation.operation_id)
    return True


def run_loop(
    operations,
    provider,
    *,
    sleep=time.sleep,
    stopped=lambda: False,
    prepare: bool = True,
) -> None:
    provider.commands.guard = lambda: check_session(operations)
    operations.interrupt_running()
    if prepare:
        provider.authenticate(workload_required=True)
        provider.connect_management()
        provider.verify_recipes()
    logger.info("provisioner_ready")
    while not stopped():
        check_session(operations)
        run_once(operations, provider)
        sleep(5)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        selected = os.environ.get("PROVIDER")
        if selected not in ("azure", "local"):
            raise ProvisioningError("provider_required")
        root = Path(os.environ.get("PROJECT_ROOT", os.getcwd())).resolve()
        identity = DemoConfig.from_values(
            {key: os.environ[key] for key in PUBLIC_KEYS if key in os.environ}
        )
        if identity.environment != selected:
            raise ProvisioningError("provider_identity_mismatch")
        config = read_runtime_configuration(identity, root)

        def terminate(_signum, _frame):
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, terminate)
        with service_provider(config, root) as provider:
            provider.authenticate(workload_required=True)
            provider.connect_management()
            provider.credentials.assert_management(config)
            dsn = os.environ.get("MANAGEMENT_DSN", "")
            if not dsn or dsn != provider.credentials.dsn("management", "mgmt_provisioner"):
                raise ProvisioningError("provisioner_dsn_mismatch")
            with provisioner_session(dsn) as operations:
                run_loop(operations, provider)
    except (psycopg.Error, ProvisionerAlreadyRunning) as error:
        logger.critical("singleton_session_stopped category=%s", type(error).__name__)
        return 1
    except (ProvisioningError, StoreError, ValueError, KeyError, OSError) as error:
        logger.critical(
            "provisioner_startup_failed code=%s",
            error.code
            if isinstance(error, (ProvisioningError, StoreError))
            else type(error).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
