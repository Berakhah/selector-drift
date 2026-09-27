# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    PATH=/opt/venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY selector_drift ./selector_drift
COPY alembic.ini ./
COPY alembic ./alembic
COPY specs ./specs
COPY scopes ./scopes
RUN uv sync --frozen --no-dev \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /opt/pw-browsers \
 && useradd --uid 10001 --home-dir /home/app --create-home app \
 && mkdir -p /data && chown 10001:10001 /data

USER 10001:10001
CMD ["python", "-m", "selector_drift.orchestrate.deployments"]
