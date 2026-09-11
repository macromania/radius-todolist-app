"""Non-public singleton coordinator. Interrupted work is never replayed."""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from dataclasses import asdict
from pathlib import Path

import psycopg

from plane_demo.management.providers.azure import AzureProvider
from plane_demo.management.providers.commands import Commands
from plane_demo.management.providers.credentials import Credentials
from plane_demo.management.providers.local import LocalProvider
from plane_demo.management.providers.local_config import LocalConfig
from plane_demo.management.provisioning import OperatorConfig, ProvisioningError, provision_pair
from plane_demo.shared.db import ProvisionerAlreadyRunning, provisioner_session

logger = logging.getLogger(__name__)


def check_session(operations) -> None:
    # This is the lock-holding connection, not a fresh health-check connection.
    operations.connection.execute("SELECT 1").fetchone()


def read_pair(operations, pair_id: str) -> dict:
    """Read inventory on the singleton session; operation writes stay in db.py."""
    pair = operations.connection.execute(
        "SELECT pair_id,isolation,reporting_role,stage,control_cluster_id,"
        "data_cluster_id,control_url,data_url FROM management.pairs WHERE pair_id=%s",
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
        result = provision_pair(operation, provider, pair, observe)
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
    operations.complete(operation.operation_id, **asdict(result))
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
        config_path = Path(
            os.environ.get("PROVISIONING_CONFIG", "/etc/plane-demo/provisioning.json")
        )
        config = (
            LocalConfig.load(config_path)
            if selected == "local"
            else OperatorConfig.load(config_path)
        )
        seed_json = os.environ.get("PROVISIONING_CREDENTIALS_JSON")
        seed = (
            json.loads(seed_json)
            if seed_json
            else json.loads(
                Path(
                    os.environ.get("PROVISIONING_CREDENTIALS", "/etc/plane-demo/credentials.json")
                ).read_text()
            )
        )
        credentials = Credentials(
            root / ".state" / selected / "credentials.json", seed, environment=selected
        )
        credentials.assert_management(config)
        dsn = os.environ.get("MANAGEMENT_DSN", "")
        if not dsn or dsn != credentials.dsn("management", "mgmt_provisioner"):
            raise ProvisioningError("provisioner_dsn_mismatch")
        if isinstance(config, LocalConfig):
            commands = Commands(root, state_root=root / ".state/local", local=True)
            provider = LocalProvider(config, root, credentials, commands)
        else:
            commands = Commands(root)
            provider = AzureProvider(config, root, credentials, commands)

        def terminate(_signum, _frame):
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, terminate)
        with provisioner_session(dsn) as operations:
            run_loop(operations, provider)
    except (psycopg.Error, ProvisionerAlreadyRunning) as error:
        logger.critical("singleton_session_stopped category=%s", type(error).__name__)
        return 1
    except (ProvisioningError, ValueError, KeyError, OSError) as error:
        logger.critical(
            "provisioner_startup_failed code=%s",
            error.code if isinstance(error, ProvisioningError) else type(error).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
