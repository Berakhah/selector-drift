"""Fix-wave item 4: `docker-compose.yml` must fail closed if `POSTGRES_PASSWORD` is unset,
rather than silently starting Postgres with the `changeme_local_dev_only` placeholder --
`compose up` without an `.env` would otherwise put a guessable password on a database that,
per this project's own scope-of-trust rules, holds run history, heal events, and the selector
cache. `${VAR:?message}` makes compose refuse to start rather than default; `${VAR:-default}`
does the opposite and is exactly what this test forbids."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


@pytest.mark.unit
def test_postgres_password_fails_closed_rather_than_defaulting() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:-" not in text, (
        "POSTGRES_PASSWORD must not have a ${VAR:-default} fallback -- it should fail closed "
        "with ${POSTGRES_PASSWORD:?...} instead."
    )
    assert re.search(r"POSTGRES_PASSWORD:\?\S", text), (
        "POSTGRES_PASSWORD must use the ${POSTGRES_PASSWORD:?message} fail-closed form."
    )


@pytest.mark.unit
def test_the_worker_connects_as_its_own_role_and_never_holds_the_superuser_credential() -> None:
    """The worker renders hostile HTML. Its Postgres credential is sd_worker's (alembic 0003:
    read and append to audit_log, nothing else), never the compose superuser's, and it fails
    closed without a password instead of defaulting."""
    env = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["worker"]["environment"]
    url = env["SD_DATABASE_URL"]
    assert url.startswith("postgresql+psycopg://sd_worker:"), url
    assert re.search(r"\$\{SD_WORKER_PASSWORD:\?\S", url), url
    assert "POSTGRES_PASSWORD" not in url and "POSTGRES_USER" not in url, url
    assert not any("POSTGRES_PASSWORD" in str(v) for v in env.values())


@pytest.mark.unit
def test_the_worker_receives_the_redaction_key_and_fails_closed_without_it() -> None:
    env = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["worker"]["environment"]
    value = env["SD_REDACTION_KEY"]
    assert re.fullmatch(r"\$\{SD_REDACTION_KEY:\?\S.*\}", value), (
        "the worker's SD_REDACTION_KEY must use the fail-closed ${VAR:?message} form; "
        f"got {value!r}"
    )
