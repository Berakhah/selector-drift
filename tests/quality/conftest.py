import datetime as dt
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from selector_drift.config import get_settings

BASE = dt.date(2026, 9, 1)
WARMUP = 5
Lander = Callable[[dt.date, list[dict[str, str]]], str]


def _scope(directory: Path) -> None:
    """A synthetic, fully-resolved scope record. The repo's scopes/testbench.yaml still carries
    day-one placeholders, which load_scope() rightly refuses."""
    today = dt.datetime.now(dt.UTC).date()
    rec = {
        "target": "testbench",
        "basis": "self-owned",
        "authorising_party": "Selector Drift test harness",
        "authorised_on": (today - dt.timedelta(days=1)).isoformat(),
        "expires_on": (today + dt.timedelta(days=365)).isoformat(),
        "evidence_uri": None,
        "evidence_sha256": None,
        "origins": ["https://testbench.example.invalid"],
        "rate_limit": {"requests": 1, "per_seconds": 30},
        "user_agent_contact": "https://example.invalid/contact",
        "first_heal_policy": "auto_promote",
        "warmup_runs": WARMUP,
        "legal_review": "self_drafted",
    }
    directory.mkdir(exist_ok=True)
    (directory / "testbench.yaml").write_text(yaml.safe_dump(rec), encoding="utf-8")


@pytest.fixture
def land(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Lander]:
    # Imported here, not at module level: a conftest under a directory named on the command line
    # loads before tests/conftest.py provisions the xdist worker database, and db.session builds
    # its engine at import time.
    from selector_drift.db.models import Run
    from selector_drift.db.session import Session, engine
    from selector_drift.land.writer import write
    from tests.land.factory import configure_lake, make_validated

    _scope(tmp_path / "scopes")
    monkeypatch.setenv("SD_SCOPE_DIR", str(tmp_path / "scopes"))
    configure_lake(tmp_path, monkeypatch)  # clears the Settings cache after setting both
    with engine.begin() as c:
        c.execute(text("TRUNCATE findings, audit_log, runs RESTART IDENTITY CASCADE"))

    def _land(day: dt.date, plain: list[dict[str, str]]) -> str:
        key = f"testbench:2:{day.isoformat()}"
        with Session() as s:
            s.merge(
                Run(
                    run_key=key,
                    target="testbench",
                    spec_version=2,
                    window=day,
                    first_started_at=dt.datetime.combine(day, dt.time(4, 12), dt.UTC),
                    state="completed",
                    rows_written=len(plain),
                )
            )
            s.commit()
        write(make_validated(plain, key))
        return key

    yield _land
    get_settings.cache_clear()
