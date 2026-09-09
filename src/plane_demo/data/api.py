import logging

from fastapi import Depends, HTTPException
from kubernetes.client.exceptions import ApiException
from redis.exceptions import RedisError
from urllib3.exceptions import HTTPError

from plane_demo.shared.auth import authenticate
from plane_demo.shared.http import base_app
from plane_demo.shared.kube import ConfigMaps, ConfigurationInvalid, ConfigurationMissing
from plane_demo.shared.models import TenantId
from plane_demo.shared.settings import Settings, redis_client

logger = logging.getLogger(__name__)


def create_app(settings: Settings, *, config_maps=None, counter_store=None):
    app = base_app(settings)
    authenticated = [Depends(authenticate(settings.demo_key))]
    maps = config_maps if config_maps is not None else ConfigMaps(settings)
    counters = counter_store if counter_store is not None else redis_client(settings)

    def serve(tenant_id: str, increment: bool):
        try:
            applied = maps.read(tenant_id)
        except ConfigurationMissing:
            raise HTTPException(404, "tenant_config_not_applied") from None
        except ConfigurationInvalid:
            raise HTTPException(503, "tenant_config_invalid") from None
        except (ApiException, HTTPError, OSError):
            logger.warning("local_configuration_unavailable")
            raise HTTPException(503, "local_configuration_unavailable") from None
        key = f"plane-demo:{applied.onboarding_id}:{tenant_id}:counter"
        try:
            value = counters.incr(key) if increment else counters.get(key)
            counter = int(value) if value is not None else 0
        except (RedisError, ValueError, TypeError):
            logger.warning("local_counter_unavailable")
            raise HTTPException(503, "local_counter_unavailable") from None
        return {
            "tenant_id": tenant_id,
            "onboarding_id": applied.onboarding_id,
            "message": applied.message,
            "applied_version": applied.version,
            "counter": counter,
        }

    @app.get("/tenants/{tenant_id}", dependencies=authenticated)
    def read_tenant(tenant_id: TenantId):
        return serve(tenant_id, False)

    @app.post("/tenants/{tenant_id}/counter", dependencies=authenticated)
    def increment_counter(tenant_id: TenantId):
        return serve(tenant_id, True)

    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env("data_api")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.listen_port, access_log=False)


if __name__ == "__main__":
    main()
