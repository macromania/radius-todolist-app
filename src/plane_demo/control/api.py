import psycopg
from fastapi import Depends, HTTPException, Query

from plane_demo.shared.auth import authenticate
from plane_demo.shared.db import add_database_error_handler, connect, page
from plane_demo.shared.http import base_app
from plane_demo.shared.models import ConfigurationRequest, TenantId
from plane_demo.shared.settings import Settings


def create_app(settings: Settings):
    app = base_app(settings)
    add_database_error_handler(app)
    authenticated = [Depends(authenticate(settings.demo_key))]

    @app.get("/tenants/{tenant_id}", dependencies=authenticated)
    def tenant_status(
        tenant_id: TenantId,
        after_event_id: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        with connect(settings.control_dsn, settings.timeout_seconds) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            desired = connection.execute(
                "SELECT * FROM control.tenant_config WHERE tenant_id=%s", (tenant_id,)
            ).fetchone()
            if not desired:
                raise HTTPException(404, "tenant_not_found")
            success = connection.execute(
                "SELECT version, received_at FROM control.events WHERE tenant_id=%s "
                "AND onboarding_id=%s AND source='data' AND type='config_applied' "
                "ORDER BY version DESC LIMIT 1",
                (tenant_id, desired["onboarding_id"]),
            ).fetchone()
            current_failure = connection.execute(
                "SELECT event_id FROM control.events WHERE tenant_id=%s "
                "AND onboarding_id=%s AND version=%s AND source='data' "
                "AND type='config_apply_failed'",
                (tenant_id, desired["onboarding_id"], desired["version"]),
            ).fetchone()
            latest = connection.execute(
                "SELECT type, version, received_at, error_code FROM control.events "
                "WHERE tenant_id=%s AND source='data' ORDER BY event_id DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            timeline, next_id = page(
                connection.execute(
                    "SELECT event_id,type,version,error_code,received_at FROM control.events "
                    "WHERE tenant_id=%s AND event_id>%s ORDER BY event_id LIMIT %s",
                    (tenant_id, after_event_id, limit + 1),
                ).fetchall(),
                limit,
            )
        applied = success["version"] if success else None
        if applied == desired["version"]:
            status = "applied"
        elif current_failure:
            status = "failed"
        else:
            status = "pending"
        return {
            "tenant_id": tenant_id,
            "onboarding_id": desired["onboarding_id"],
            "desired": {"message": desired["message"], "version": desired["version"]},
            "data_config": {
                "status": status,
                "last_applied_version": applied,
                "reported_at": success["received_at"] if success else None,
                "last_report": latest,
            },
            "timeline": timeline,
            "next_after_event_id": next_id,
        }

    @app.put("/tenants/{tenant_id}/configuration", dependencies=authenticated)
    def update_configuration(tenant_id: TenantId, request: ConfigurationRequest):
        try:
            with connect(settings.control_dsn, settings.timeout_seconds) as connection:
                row = connection.execute(
                    "SELECT control.update_configuration(%s,%s) AS version",
                    (tenant_id, request.message),
                ).fetchone()
        except psycopg.Error as error:
            if error.sqlstate == "PT404":
                raise HTTPException(404, "tenant_not_found") from None
            raise
        return {"tenant_id": tenant_id, "desired": {"message": request.message, **row}}

    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env("control_api")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.listen_port, access_log=False)


if __name__ == "__main__":
    main()
