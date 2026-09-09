import logging
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import psycopg
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row


@contextmanager
def connect(dsn: str, timeout: int = 5, *, autocommit: bool = False):
    with psycopg.connect(
        dsn,
        connect_timeout=timeout,
        options=f"-c statement_timeout={timeout * 1000} -c lock_timeout={timeout * 1000}",
        row_factory=dict_row,
        autocommit=autocommit,
    ) as connection:
        yield connection


def page(rows: list[dict], limit: int) -> tuple[list[dict], int | None]:
    more = len(rows) > limit
    selected = rows[:limit]
    return selected, selected[-1]["event_id"] if more else None


def add_database_error_handler(app):
    @app.exception_handler(psycopg.Error)
    async def database_error(_request, error):
        logging.getLogger(__name__).warning(
            "database_request_failed sqlstate=%s", error.sqlstate or "unavailable"
        )
        return JSONResponse({"detail": "database_unavailable"}, status_code=503)


class ProvisionerAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingOperation:
    operation_id: UUID
    tenant_id: str
    onboarding_id: UUID
    pair_id: str
    isolation: str
    initial_message: str


@contextmanager
def provisioner_session(dsn: str, timeout: int = 5):
    """Hold the singleton session lock; never reconnect this session implicitly."""
    with connect(dsn, timeout, autocommit=True) as connection:
        acquired = connection.execute(
            "SELECT pg_try_advisory_lock(35510,2) AS acquired"
        ).fetchone()["acquired"]
        if not acquired:
            raise ProvisionerAlreadyRunning("another provisioner holds the singleton lock")
        yield OperationStore(connection)


class OperationStore:
    def __init__(self, connection):
        self.connection = connection

    def _locked(self, operation_id):
        row = self.connection.execute(
            "SELECT t.*,o.operation_id,o.status,o.stage,o.error_code "
            "FROM management.tenants t JOIN management.operations o USING(tenant_id) "
            "WHERE operation_id=%s FOR UPDATE OF t",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("operation_not_found")
        return row

    def _observe(self, row, status, stage, error_code):
        self.connection.execute(
            "UPDATE management.operations SET status=%s,stage=%s,error_code=%s,"
            "updated_at=clock_timestamp() WHERE operation_id=%s",
            (status, stage, error_code, row["operation_id"]),
        )
        transition = "provisioning_stage" if status == "running" else f"provisioning_{status}"
        self.connection.execute(
            "INSERT INTO management.events"
            "(tenant_id,onboarding_id,pair_id,source,type,version,error_code,stage) "
            "VALUES (%s,%s,%s,'provisioner',%s,1,%s,%s)",
            (row["tenant_id"], row["onboarding_id"], row["pair_id"], transition, error_code, stage),
        )

    def interrupt_running(self) -> int:
        with self.connection.transaction():
            rows = self.connection.execute(
                "SELECT operation_id FROM management.operations "
                "WHERE status='running' ORDER BY created_at"
            ).fetchall()
            for operation in rows:
                row = self._locked(operation["operation_id"])
                self._observe(row, "interrupted", row["stage"], "provisioner_restarted")
        return len(rows)

    def claim_pending(self) -> PendingOperation | None:
        with self.connection.transaction():
            operation = self.connection.execute(
                "SELECT operation_id FROM management.operations "
                "WHERE status='pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if operation is None:
                return None
            row = self._locked(operation["operation_id"])
            self._observe(row, "running", "starting", None)
            return PendingOperation(
                **{key: row[key] for key in PendingOperation.__dataclass_fields__}
            )

    def observe(
        self,
        operation_id: UUID,
        stage: str,
        *,
        status: str = "running",
        error_code: str | None = None,
    ) -> None:
        if status not in {"running", "failed", "interrupted"}:
            raise ValueError("use complete() to finish infrastructure successfully")
        if (status == "running") != (error_code is None):
            raise ValueError("only failed/interrupted observations require an error code")
        with self.connection.transaction():
            row = self._locked(operation_id)
            if (row["status"], row["stage"], row["error_code"]) == (status, stage, error_code):
                return
            if row["status"] != "running":
                raise ValueError("operation_is_not_running")
            self._observe(row, status, stage, error_code)

    def complete(
        self,
        operation_id: UUID,
        *,
        control_cluster_id: str,
        data_cluster_id: str,
        control_url: str,
        data_url: str,
    ) -> None:
        from urllib.parse import urlsplit

        for endpoint in (control_url, data_url):
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("endpoint must be an HTTP(S) URL without credentials or query")
        if not control_cluster_id or not data_cluster_id:
            raise ValueError("cluster identifiers are required")
        with self.connection.transaction():
            row = self._locked(operation_id)
            if row["status"] != "running":
                raise ValueError("operation_is_not_running")
            self.connection.execute(
                "UPDATE management.pairs SET stage='available',control_cluster_id=%s,"
                "data_cluster_id=%s,control_url=%s,data_url=%s WHERE pair_id=%s",
                (control_cluster_id, data_cluster_id, control_url, data_url, row["pair_id"]),
            )
            self._observe(row, "succeeded", "available", None)
