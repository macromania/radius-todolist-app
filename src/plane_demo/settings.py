import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

import redis

TENANT_PATTERN = r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?"


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"required setting: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    demo_key: str = field(default="", repr=False)
    management_dsn: str = field(default="", repr=False)
    control_dsn: str = field(default="", repr=False)
    redis_url: str = field(default="", repr=False)
    pair_id: str = ""
    project_id: str = ""
    namespace: str = ""
    poll_interval: float = 5
    timeout_seconds: int = 5
    body_limit: int = 8192
    listen_port: int = 8088
    challenge_directory: str = "/challenges"

    @classmethod
    def from_env(cls, role: str) -> "Settings":
        api = role in {"management_api", "control_api", "data_api"}
        management = role in {"management_api", "control_reconciler"}
        control = role in {"control_api", "control_reconciler", "data_reconciler"}
        data = role in {"data_api", "data_reconciler"}
        paired = role in {"control_reconciler", "data_reconciler", "data_api"}
        settings = cls(
            demo_key=required("DEMO_KEY") if api else "",
            management_dsn=required("MANAGEMENT_DSN") if management else "",
            control_dsn=required("CONTROL_DSN") if control else "",
            redis_url=os.environ.get("REDIS_URL", ""),
            pair_id=required("PAIR_ID") if paired else "",
            project_id=required("PROJECT_ID") if data else "",
            namespace=required("KUBE_NAMESPACE") if data else "",
            poll_interval=float(os.environ.get("POLL_INTERVAL_SECONDS", "5")),
            timeout_seconds=int(os.environ.get("TIMEOUT_SECONDS", "5")),
            body_limit=int(os.environ.get("HTTP_BODY_LIMIT", "8192")),
            listen_port=int(os.environ.get("LISTEN_PORT", "8088")),
            challenge_directory=os.environ.get("CHALLENGE_DIRECTORY", "/challenges"),
        )
        if api and len(settings.demo_key) < 32:
            raise ValueError("DEMO_KEY must contain at least 32 characters")
        if not 0 < settings.poll_interval <= 60:
            raise ValueError("POLL_INTERVAL_SECONDS must be in (0, 60]")
        if not 1 <= settings.timeout_seconds <= 30:
            raise ValueError("TIMEOUT_SECONDS must be in [1, 30]")
        if not 4096 <= settings.body_limit <= 65536:
            raise ValueError("HTTP_BODY_LIMIT must be in [4096, 65536]")
        if not 1024 <= settings.listen_port <= 65535:
            raise ValueError("LISTEN_PORT must be an unprivileged TCP port")
        for value in (settings.pair_id, settings.project_id, settings.namespace):
            if value and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
                raise ValueError("invalid project, pair, or namespace identifier")
        return settings


def redis_client(settings: Settings) -> redis.Redis:
    options = {
        "socket_connect_timeout": settings.timeout_seconds,
        "socket_timeout": settings.timeout_seconds,
        "decode_responses": True,
    }
    url = settings.redis_url or os.environ.get("CONNECTION_REDIS_URL", "")
    if url:
        if not url.startswith(("redis://", "rediss://")):
            raise ValueError("Redis URL must use redis:// or rediss://")
        return redis.Redis.from_url(url, **options)
    host = required("CONNECTION_REDIS_HOST")
    tls = required("CONNECTION_REDIS_TLS").lower()
    if tls not in {"true", "false"}:
        raise ValueError("CONNECTION_REDIS_TLS must be true or false")
    # Radius recipes encode the standalone password for URL construction.
    password = unquote(required("CONNECTION_REDIS_PASSWORD"))
    return redis.Redis(
        host=host,
        port=int(required("CONNECTION_REDIS_PORT")),
        password=password,
        ssl=tls == "true",
        **options,
    )
