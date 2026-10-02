# syntax=docker/dockerfile:1.7
# Multi-arch base images: built on the P-4 VM this is linux/arm64 natively (bench spec §7).
# CI proves the arm64 build explicitly (.github/workflows/testbench-image.yml).
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
# Exactly the bench's dependency group -- none of the product's (no prefect, playwright, duckdb).
RUN uv export --frozen --only-group testbench --no-emit-project -o /tmp/requirements.txt \
 && uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python -r /tmp/requirements.txt

FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bench
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY testbench/ /app/testbench/
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]
CMD ["uvicorn", "testbench.portal.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
