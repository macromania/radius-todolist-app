import logging
import time

import psycopg

from plane_demo.shared.db import connect
from plane_demo.shared.models import ReconcileResult
from plane_demo.shared.settings import Settings

logger = logging.getLogger(__name__)


def report(settings: Settings, tenant: dict, success: bool, error: str | None = None):
    with connect(settings.management_dsn, settings.timeout_seconds) as connection:
        connection.execute(
            "SELECT management.report_control(%s,%s,%s,%s,%s)",
            (
                tenant["tenant_id"],
                tenant["onboarding_id"],
                tenant["desired_revision"],
                "control_record_created" if success else "control_record_failed",
                error,
            ),
        )


def run_once(settings: Settings) -> ReconcileResult:
    result = ReconcileResult()
    try:
        with connect(settings.management_dsn, settings.timeout_seconds) as connection:
            tenants = connection.execute(
                "SELECT t.* FROM management.tenants t JOIN management.pairs p USING(pair_id) "
                "WHERE t.pair_id=%s AND p.stage='available' ORDER BY tenant_id",
                (settings.pair_id,),
            ).fetchall()
    except psycopg.Error as error:
        logger.warning("management_poll_failed sqlstate=%s", error.sqlstate or "unavailable")
        result.failed += 1
        return result
    for tenant in tenants:
        result.examined += 1
        try:
            with connect(settings.control_dsn, settings.timeout_seconds) as connection:
                connection.execute(
                    "SELECT control.ensure_tenant(%s,%s,%s,%s)",
                    (
                        tenant["tenant_id"],
                        tenant["onboarding_id"],
                        tenant["pair_id"],
                        tenant["initial_message"],
                    ),
                )
        except psycopg.Error as error:
            logger.warning("control_record_failed sqlstate=%s", error.sqlstate or "unavailable")
            result.failed += 1
            try:
                report(settings, tenant, False, "control_write_failed")
            except psycopg.Error as report_error:
                logger.warning(
                    "control_report_failed sqlstate=%s", report_error.sqlstate or "unavailable"
                )
            continue
        try:
            report(settings, tenant, True)
            result.succeeded += 1
        except psycopg.Error as error:
            logger.warning("control_report_failed sqlstate=%s", error.sqlstate or "unavailable")
            result.failed += 1
    return result


def main() -> None:
    settings = Settings.from_env("control_reconciler")
    logging.basicConfig(level=logging.INFO)
    while True:
        result = run_once(settings)
        logger.info(
            "control_poll examined=%d succeeded=%d failed=%d",
            result.examined,
            result.succeeded,
            result.failed,
        )
        time.sleep(settings.poll_interval)


if __name__ == "__main__":
    main()
