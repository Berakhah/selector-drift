"""Reproduce the published rung distribution (Task 26 Step 6, run by Task 26A; B11).

Run from the repository root, against a TEST database (it truncates the run tables first):

    SD_DATABASE_URL=postgresql+psycopg://...@127.0.0.1:5432/selector_drift_test \\
        python scripts/bench_rung_campaign.py [DAYS]
    python -m selector_drift.orchestrate.rung_stats

The assembled product -- scope gate, Chromium, navigation, the five-rung cascade, the gate, the
lake, quality, evidence -- runs once per day against the bench portal served in-process with its
production mutation schedule (testbench.schedule: one or two benign classes every day, an
adversarial class about one day in seven), from 2026-01-01 for DAYS days (default 60).

The operator policy is mechanical, so the figure is reproducible: every run the gate holds is
offered for promotion the same day; a hold the gate lets a human confirm (it was held only for
missing evidence) is promoted, and one it still refuses (a FAIL -- StillInvalid) is discarded.
That is the most permissive operator the product allows, which favours rung-1 hits the next day;
the share is reported as measured under it. The rung shares come from
`python -m selector_drift.orchestrate.rung_stats` over the `resolutions` table this writes --
every field resolution, won or not -- never from this script's own arithmetic.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

import uvicorn
import yaml
from pydantic import SecretStr
from sqlalchemy import text

REPO = Path(__file__).resolve().parents[1]
START = dt.date(2026, 1, 1)
FAST = {"requests": 100, "per_seconds": 1}  # the limiter runs; the bench is local and ours


class _DemoAccount:
    """The bench's public demo account (bench spec 3.2), as the spec's login steps need."""

    def get(self, target: object) -> object:
        from selector_drift.credentials.provider import Credentials

        return Credentials(username="demo", password=SecretStr("demo"))

    def get_totp_code(self, target: object) -> str:
        raise NotImplementedError("the bench has no second factor")


def _environment(tmp: Path) -> None:
    url = os.environ.get("SD_DATABASE_URL", "")
    if not url.rsplit("/", 1)[-1].endswith("_test"):
        sys.exit("refusing: SD_DATABASE_URL must name a database ending in _test (it is truncated)")
    for name, sub in (
        ("SD_SCOPE_DIR", "scopes"),
        ("SD_LAKE_ROOT", "lake"),
        ("SD_EVIDENCE_ROOT", "evidence"),
        ("SD_SESSION_DIR", "sessions"),
        ("SD_ESCALATION_CORPUS", "corpus"),
    ):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(tmp / sub)
    os.environ["SD_SPEC_DIR"] = str(REPO / "specs")
    os.environ["SD_REDACTION_KEY"] = os.urandom(32).hex()
    os.environ["TESTBENCH_SESSION_SECRET"] = secrets.token_hex(32)
    os.environ["TESTBENCH_OVERRIDE_SECRET"] = secrets.token_hex(32)
    os.environ["TESTBENCH_COOKIE_SECURE"] = "0"
    os.environ["PREFECT_LOGGING_TO_API_ENABLED"] = "false"


def _scope(tmp: Path, origin: str, end: dt.date) -> None:
    record = {
        "target": "testbench",
        "basis": "self-owned",
        "authorising_party": "Selector Drift bench campaign",
        "authorised_on": START.isoformat(),
        # driver.open() re-checks expiry against the real clock, so it outlives today too.
        "expires_on": max(end, dt.datetime.now(dt.UTC).date() + dt.timedelta(days=365)).isoformat(),
        "evidence_uri": None,
        "evidence_sha256": None,
        "origins": [origin],
        "rate_limit": FAST,
        "user_agent_contact": "https://example.invalid/contact",
        "first_heal_policy": "auto_promote",  # the bench's own record (scopes/testbench.yaml)
        "warmup_runs": 5,
        "legal_review": "self_drafted",
    }
    (tmp / "scopes" / "testbench.yaml").write_text(yaml.safe_dump(record), encoding="utf-8")


def _serve(app: object) -> tuple[str, uvicorn.Server]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)  # type: ignore[arg-type]
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            sys.exit("bench server did not start")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server


def main(days: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sd-campaign-"))
    _environment(tmp)
    # Imported after the environment is set: db.session builds its engine at import time.
    from selector_drift.browser import registry
    from selector_drift.config import get_settings
    from selector_drift.db.models import Run
    from selector_drift.db.session import Session, engine
    from selector_drift.orchestrate.pipeline import default_confirmers, run_once
    from selector_drift.orchestrate.quarantine_actions import build_quarantine_actions
    from selector_drift.orchestrate.rung_stats import format_stats, rung_stats
    from selector_drift.validate.quarantine import StillInvalid
    from testbench.portal.app import create_app
    from testbench.schedule import scheduled_names

    get_settings.cache_clear()
    with engine.begin() as c:  # a *_test database only (checked above)
        c.execute(
            text(
                "TRUNCATE runs, run_attempts, heartbeats, resolutions, heal_events, quarantine, "
                "findings, audit_log, scope_records, selector_cache, interventions "
                "RESTART IDENTITY CASCADE"
            )
        )
    today = {"day": START}
    app = create_app(
        clock=lambda: dt.datetime.combine(today["day"], dt.time(4, 12), tzinfo=dt.UTC)
    )  # catalogue=None: the production CATALOGUE and schedule
    url, server = _serve(app)
    end = START + dt.timedelta(days=days - 1)
    _scope(tmp, url, end)
    registry.url_for = lambda target: url
    actions = build_quarantine_actions()
    states: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    try:
        for i in range(days):
            day = START + dt.timedelta(days=i)
            today["day"] = day
            key = asyncio.run(
                run_once(
                    "testbench.yaml",
                    window=day,
                    credentials_for=lambda target: _DemoAccount(),  # type: ignore[arg-type,return-value]
                    confirmers=default_confirmers(),
                )
            )
            with Session() as s:
                run = s.get(Run, key)
                state = run.state if run is not None else "missing"
            states[state] += 1
            decision = ""
            if state == "quarantined":
                try:
                    actions.promote(
                        key, "campaign", "operator policy: confirm what the gate allows"
                    )
                    decision = "promoted"
                except StillInvalid:
                    actions.discard(key, "campaign", "operator policy: the gate still refuses it")
                    decision = "discarded"
                except ValueError as exc:  # rebuild_gate_inputs: nothing to promote (held empty)
                    # Classified apart from a gate refusal and printed, so a real bug that raises
                    # ValueError is seen in the campaign output instead of counted as a refusal.
                    actions.discard(key, "campaign", f"unpromotable: {exc}"[:500])
                    decision = "unpromotable"
                    print(f"  {key}: unpromotable ({exc})")
                decisions[decision] += 1
            print(f"{day}  {state:<11} {decision:<9} {','.join(scheduled_names(day, 'testbench'))}")
    finally:
        server.should_exit = True
    print(f"\nruns: {dict(states)}  holds: {dict(decisions)}")
    print(format_stats(rung_stats()))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 60)
