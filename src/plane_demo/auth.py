import hmac

from fastapi import Header, HTTPException
from starlette.responses import JSONResponse


def authenticate(expected: str):
    if len(expected) < 32:
        raise ValueError("DEMO_KEY must contain at least 32 characters")

    def check(x_demo_key: str | None = Header(default=None)) -> None:
        if not x_demo_key or not hmac.compare_digest(
            x_demo_key.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(401, detail="invalid_demo_key")

    return check


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                return await JSONResponse({"detail": "request_body_too_large"}, status_code=413)(
                    scope, receive, send
                )
            if not message.get("more_body", False):
                break
        consumed = False

        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
