import psycopg
from fastapi import Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from plane_demo.auth import authenticate
from plane_demo.db import add_database_error_handler, connect, page
from plane_demo.http import base_app
from plane_demo.models import TenantId, TenantRequest
from plane_demo.settings import Settings


def create_app(settings: Settings):
    app = base_app(settings)
    add_database_error_handler(app)
    authenticated = [Depends(authenticate(settings.demo_key))]

    @app.post("/tenants", dependencies=authenticated, status_code=202)
    def create_tenant(request: TenantRequest):
        try:
            with connect(settings.management_dsn, settings.timeout_seconds) as connection:
                row = connection.execute(
                    "SELECT management.accept_tenant(%s,%s,%s) AS operation_id",
                    (request.tenant_id, request.isolation, request.initial_message),
                ).fetchone()
        except psycopg.Error as error:
            if error.sqlstate == "PT409":
                return JSONResponse(
                    {"detail": "duplicate_tenant", "status_url": f"/tenants/{request.tenant_id}"},
                    status_code=409,
                    headers={"Location": f"/tenants/{request.tenant_id}"},
                )
            if error.sqlstate in {"PT503", "PT507"}:
                raise HTTPException(
                    503,
                    detail="provisioner_busy"
                    if error.sqlstate == "PT503"
                    else "allocation_unavailable",
                    headers={"Retry-After": "5"},
                ) from None
            raise
        return JSONResponse(
            jsonable_encoder(
                {
                    "tenant_id": request.tenant_id,
                    "operation_id": row["operation_id"],
                    "status_url": f"/tenants/{request.tenant_id}",
                    "operation_url": f"/operations/{row['operation_id']}",
                }
            ),
            status_code=202,
            headers={"Location": f"/tenants/{request.tenant_id}"},
        )

    @app.get("/tenants/{tenant_id}", dependencies=authenticated)
    def tenant_status(
        tenant_id: TenantId,
        after_event_id: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        with connect(settings.management_dsn, settings.timeout_seconds) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = connection.execute(
                "SELECT t.*, p.control_url, p.data_url, o.operation_id,"
                " o.status AS provisioning_status, o.stage, o.error_code "
                "FROM management.tenants t JOIN management.pairs p USING(pair_id) "
                "JOIN management.operations o USING(tenant_id) WHERE tenant_id=%s",
                (tenant_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "tenant_not_found")
            report = connection.execute(
                "SELECT type, version, received_at FROM management.events "
                "WHERE tenant_id=%s AND onboarding_id=%s AND source='control' "
                "ORDER BY (type='control_record_created') DESC, event_id DESC LIMIT 1",
                (tenant_id, row["onboarding_id"]),
            ).fetchone()
            timeline, next_id = page(
                connection.execute(
                    "SELECT event_id,type,version,error_code,stage,received_at "
                    "FROM management.events WHERE tenant_id=%s AND event_id>%s "
                    "ORDER BY event_id LIMIT %s",
                    (tenant_id, after_event_id, limit + 1),
                ).fetchall(),
                limit,
            )
        created = report is not None and report["type"] == "control_record_created"
        return {
            "tenant_id": tenant_id,
            "onboarding_id": row["onboarding_id"],
            "isolation": row["isolation"],
            "pair_id": row["pair_id"],
            "operation_id": row["operation_id"],
            "provisioning_status": row["provisioning_status"],
            "provisioning_stage": row["stage"],
            "error_code": row["error_code"],
            "onboarding_status": "ready" if created else "pending",
            "control_record": {
                "status": "created" if created else ("failed" if report else "pending"),
                "observed_revision": report["version"] if report else None,
                "reported_at": report["received_at"] if report else None,
            },
            "control_url": row["control_url"],
            "data_url": row["data_url"],
            "timeline": timeline,
            "next_after_event_id": next_id,
        }

    @app.get("/operations/{operation_id}", dependencies=authenticated)
    def operation_status(operation_id: str):
        from uuid import UUID

        try:
            identifier = UUID(operation_id)
        except ValueError:
            raise HTTPException(422, "invalid_operation_id") from None
        with connect(settings.management_dsn, settings.timeout_seconds) as connection:
            row = connection.execute(
                "SELECT * FROM management.operations WHERE operation_id=%s", (identifier,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "operation_not_found")
        return row

    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env("management_api")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.listen_port, access_log=False)


if __name__ == "__main__":
    main()
