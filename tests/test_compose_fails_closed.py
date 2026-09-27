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
