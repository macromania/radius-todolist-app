import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from plane_demo.settings import Settings


def create_app(settings: Settings):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    root = Path(settings.challenge_directory).resolve()

    @app.get("/.well-known/acme-challenge/{token}", response_class=PlainTextResponse)
    def challenge(token: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,253}", token):
            raise HTTPException(404)
        try:
            path = (root / token).resolve()
            if not path.is_relative_to(root):
                raise HTTPException(404)
            with path.open("rb") as stream:
                value = stream.read(4097)
            if not value or len(value) > 4096:
                raise HTTPException(404)
            return PlainTextResponse(value, headers={"Cache-Control": "no-store"})
        except OSError:
            raise HTTPException(404) from None

    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env("acme_responder")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.listen_port, access_log=False)


if __name__ == "__main__":
    main()
