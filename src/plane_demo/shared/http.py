from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from plane_demo.shared.auth import BodyLimitMiddleware
from plane_demo.shared.settings import Settings


def base_app(settings: Settings) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.body_limit)

    @app.get("/livez")
    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse({"detail": "invalid_request"}, status_code=422)

    return app
