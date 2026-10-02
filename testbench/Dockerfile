# syntax=docker/dockerfile:1.7
# Multi-arch base images: built on the P-4 VM this is linux/arm64 natively (bench spec §7).
# CI proves the arm64 build explicitly (.github/workflows/testbench-image.yml).
# Pinned to the multi-arch index digest (S-I2), not a floating tag: `python:3.12-slim` and
# `uv:0.8` can both silently resolve to a different image on every rebuild otherwise. Digests
# captured 2026-10-02 via `docker buildx imagetools inspect <image>`; both are OCI image
# *indices* (not single-platform manifests), so linux/arm64 still resolves on the P-4 VM.
# python:3.12-slim -> 3.12.15-slim-trixie
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS build
COPY --from=ghcr.io/astral-sh/uv:0.8@sha256:1d31be550ff927957472b2a491dc3de1ea9b5c2d319a9cea5b6a48021e2990a6 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
# Exactly the bench's dependency group -- none of the product's (no prefect, playwright, duckdb).
RUN uv export --frozen --only-group testbench --no-emit-project -o /tmp/requirements.txt \
 && uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python -r /tmp/requirements.txt

FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
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
