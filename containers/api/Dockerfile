FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src" UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN mkdir -p .build-work && \
    TMPDIR=/app/.build-work UV_CACHE_DIR=/app/.uv-cache \
    uv sync --frozen --no-dev --no-install-project && \
    rm -rf .build-work .uv-cache
COPY src ./src
COPY sql ./sql
RUN find src -type d -name __pycache__ -prune -exec rm -rf '{}' ';' && \
    python -m compileall -q src && \
    useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin plane
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
USER 10001:10001
EXPOSE 8088
CMD ["python", "-m", "plane_demo.management_api"]
