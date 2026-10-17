from __future__ import annotations

import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from testbench.portal.app import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(app: FastAPI) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("bench server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _bench_env(mp: pytest.MonkeyPatch, override_secret: str) -> None:
    # Generated per session: no secret, test or otherwise, is ever a literal in the repo.
    mp.setenv("TESTBENCH_SESSION_SECRET", secrets.token_hex(32))
    mp.setenv("TESTBENCH_OVERRIDE_SECRET", override_secret)
    mp.setenv("TESTBENCH_COOKIE_SECURE", "0")  # the test server is plain http


@pytest.fixture(scope="session")
def override_secret() -> str:
    return secrets.token_hex(32)


@pytest.fixture(scope="session")
def bench_server(override_secret: str) -> Iterator[str]:
    """The portal with NO mutations, for tests of the portal's own behaviour (Task 7). Those
    tests must not start failing the day the schedule draws TableToDiv or VirtualisedRows."""
    with pytest.MonkeyPatch.context() as mp:
        _bench_env(mp, override_secret)
        app = create_app(catalogue=())
    yield from _serve(app)


@pytest.fixture(scope="session")
def mutating_bench_server(override_secret: str) -> Iterator[str]:
    """The portal exactly as deployed: the production CATALOGUE, the real clock."""
    with pytest.MonkeyPatch.context() as mp:
        _bench_env(mp, override_secret)
        app = create_app()
    yield from _serve(app)


@pytest.fixture
def client_factory(
    monkeypatch: pytest.MonkeyPatch, override_secret: str
) -> Callable[..., TestClient]:
    _bench_env(monkeypatch, override_secret)

    def make(**kwargs: Any) -> TestClient:
        kwargs.setdefault("catalogue", ())
        return TestClient(create_app(**kwargs), follow_redirects=False)

    return make


# ------------------------------------------------------------------------------------------------
# Task 26C: the scorer's fixtures. Pages are the bench's own (mutate_html over render_results,
# the production catalogue), stamped with data-sd-node the way a LivePageSource stamps them, then
# put through the product's real evidence path (redact_evidence -> write_heal_bundle) -- so the
# scorer is tested against bundles exactly as the product stores them, redacted seed tag and all.
# The redaction key is minted per test: no secret, test or otherwise, is a literal in the repo.

import datetime as dt  # noqa: E402
import os  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

import lxml.html  # noqa: E402
from bok_core.boundary.egress import Plane  # noqa: E402
from bok_core.boundary.sensitivity import Sensitivity  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session as OrmSession  # noqa: E402

from testbench.answers.key import expected_xpaths  # noqa: E402
from testbench.mutations.base import parse, serialise  # noqa: E402
from testbench.portal.hook import mutate_html  # noqa: E402
from testbench.portal.render import render_results  # noqa: E402
from testbench.schedule import scheduled_names  # noqa: E402


def _first_day(start: dt.date, wanted: Callable[[list[str]], bool]) -> dt.date:
    for offset in range(3650):
        day = start + dt.timedelta(days=offset)
        if wanted(list(scheduled_names(day, "testbench"))):
            return day
    raise LookupError("no such bench day within ten years")


# A day with no DecoyColumn, and the first day DecoyColumn runs.
PLAIN_DAY = _first_day(dt.date(2026, 10, 3), lambda names: "DecoyColumn" not in names)
DECOY_DAY = _first_day(dt.date(2026, 10, 3), lambda names: "DecoyColumn" in names)
SPEC_VERSION = 2  # specs/testbench.yaml


def bench_page(day: dt.date) -> str:
    """The page the bench serves on `day`, with every element stamped `n<k>` in document order."""
    doc = parse(mutate_html(day, render_results(day)))
    for k, el in enumerate(e for e in doc.iter() if isinstance(e.tag, str)):
        el.set("data-sd-node", f"n{k}")
    return serialise(doc)


def captured_at(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(4, 12), tzinfo=dt.UTC)


def _redacted(raw: str, day: dt.date, key: bytes) -> Any:
    from selector_drift.heallog.capture import Evidence
    from selector_drift.heallog.evidence import PiiMasker, redact_evidence

    evidence = Evidence.from_static(
        raw,
        run_key=f"testbench:{SPEC_VERSION}:{day.isoformat()}",
        sensitivity=Sensitivity.SYNTHETIC,
        masker=PiiMasker(key=key, sensitivity=Sensitivity.SYNTHETIC),
        captured_at=captured_at(day),
    )
    return redact_evidence(evidence, key=key, deadline_s=300)


def redacted_after(raw: str, day: dt.date, key: bytes) -> str:
    """`raw` as the product would store it as after.html."""
    return str(_redacted(raw, day, key).html.decode("utf-8"))


def live_token(page: str, path: str) -> str:
    root = lxml.html.document_fromstring(page)
    (el,) = root.getroottree().xpath(path)
    return str(el.get("data-sd-node"))


def decoy_path(day: dt.date) -> str:
    """Row 1's decoy cell: DecoyColumn inserts it immediately before the real amount."""
    marked = parse(mutate_html(day, render_results(day, mark_truth=True)))
    real = marked.xpath('//*[@data-sd-truth="amount"]')[0]
    decoy = real.getprevious()
    assert decoy is not None and decoy.get("data-sd-truth") is None
    return str(marked.getroottree().getpath(decoy))


@dataclass(frozen=True)
class SeededHeal:
    id: int
    run_key: str
    expected: str  # the judgement value this heal must receive


@pytest.fixture
def redaction_key() -> bytes:
    return os.urandom(32)


@pytest.fixture
def db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redaction_key: bytes
) -> Iterator[OrmSession]:
    """The per-worker *_test database (tests/conftest.py guards the name), emptied; evidence
    under a per-test root, redaction under the per-test key."""
    from selector_drift.config import get_settings

    monkeypatch.setenv("SD_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    monkeypatch.setenv("SD_REDACTION_KEY", redaction_key.hex())
    get_settings.cache_clear()
    from selector_drift.db.session import Session, engine

    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE runs, run_attempts, heartbeats, resolutions, audit_log, "
                "scope_records RESTART IDENTITY CASCADE"
            )
        )
    with Session() as s:
        yield s
    get_settings.cache_clear()


def _seed_heal(
    s: OrmSession,
    *,
    day: dt.date,
    field: str,
    node_id: str,
    state: str,
    key: bytes,
    quarantine: str | None = "absent",
    store: bool = True,
    error: str | None = None,
) -> tuple[int, str]:
    """One run, its resolutions row and heal_events row, and (if `store`) its heal bundle
    written by the product's own write path. `quarantine`: "absent" (no row), None
    (unresolved), or the resolved_as value. `error` is the run's error column."""
    from selector_drift.db.models import HealEvent, Quarantine, Resolution, Run
    from selector_drift.heallog.evidence import write_heal_bundle

    run_key = f"testbench:{SPEC_VERSION}:{day.isoformat()}"
    now = dt.datetime.now(dt.UTC)
    s.add(Run(run_key=run_key, target="testbench", spec_version=SPEC_VERSION, window=day,
              first_started_at=now, state=state, error=error))  # fmt: skip
    s.flush()
    res = Resolution(run_key=run_key, target="testbench", field=field, winning_rung=3,
                     attempts=[], node_id=node_id, ms=1, at=now)  # fmt: skip
    s.add(res)
    s.flush()
    heal = HealEvent(run_key=run_key, resolution_id=res.id, target="testbench", field=field,
                     broke_at=now, winning_rung=3, selector_after="//td", anchor_after={},
                     trace=[], verdicts=[], evidence_uri=None)  # fmt: skip
    s.add(heal)
    s.flush()
    if quarantine != "absent":
        resolved = quarantine is not None
        s.add(Quarantine(run_key=run_key, reason="test", verdicts=[], data_uri="unused",
                         resolved_as=quarantine, actor="tester" if resolved else None,
                         resolved_at=now if resolved else None))  # fmt: skip
    if store:
        heal.evidence_uri = write_heal_bundle(
            heal.id, "testbench", _redacted(bench_page(day), day, key), Plane.CLIENT
        )
    s.commit()
    return heal.id, run_key


@pytest.fixture
def seeded_bench_heals(db: OrmSession, redaction_key: bytes) -> list[SeededHeal]:
    """Five correct heals, every one settled; what differs is whether the product accepted
    them -- by the product's own predicate (orchestrate.history.accepted_run)."""
    out: list[SeededHeal] = []
    cases: list[tuple[str, bool, str, str | None, str]] = [
        # (field, live node id?, run state, quarantine, expected judgement)
        ("claim_number", False, "healed", "absent", "true_accept"),
        ("amount", False, "quarantined", "discarded", "false_refusal"),  # a human said no
        ("reserve", True, "quarantined", "promoted", "true_accept"),
        ("status", False, "failed", "absent", "false_refusal"),  # failed terminally, no write
        # A stale quarantine row beside a run that settled healed: the product landed its rows.
        ("filed", False, "healed", "discarded", "true_accept"),
    ]
    for offset, (field, live, state, quarantine, expected) in enumerate(cases):
        day = PLAIN_DAY + dt.timedelta(days=offset)
        path = expected_xpaths(day, field)[0]
        node = live_token(bench_page(day), path) if live else path
        heal_id, run_key = _seed_heal(
            db, day=day, field=field, node_id=node, state=state, key=redaction_key,
            quarantine=quarantine,
        )  # fmt: skip
        out.append(SeededHeal(heal_id, run_key, expected))
    return out


SeedDecoy = Callable[..., SeededHeal]


@pytest.fixture
def seed_decoy(db: OrmSession, redaction_key: bytes) -> SeedDecoy:
    """A heal that locked onto DecoyColumn's plausible adjacent column, in a run settled (or
    not) as asked: seed_decoy(state=..., quarantine=..., error=...)."""
    path = decoy_path(DECOY_DAY)
    assert path not in expected_xpaths(DECOY_DAY, "amount")

    def make(
        *, state: str, quarantine: str | None = "absent", error: str | None = None
    ) -> SeededHeal:
        heal_id, run_key = _seed_heal(
            db, day=DECOY_DAY, field="amount", node_id=path, state=state, key=redaction_key,
            quarantine=quarantine, error=error,
        )  # fmt: skip
        return SeededHeal(heal_id, run_key, "")

    return make


@pytest.fixture
def decoy_heal_quarantined(seed_decoy: SeedDecoy) -> SeededHeal:
    """The gate held the decoy heal; no human has decided yet (first-heal confirmation)."""
    return seed_decoy(state="quarantined", quarantine=None)


@pytest.fixture
def heal_without_evidence(db: OrmSession, redaction_key: bytes) -> SeededHeal:
    """Redaction failed at heal time, so nothing was stored (accept.py REDACTION_FAILED)."""
    heal_id, run_key = _seed_heal(
        db, day=PLAIN_DAY, field="amount", node_id=expected_xpaths(PLAIN_DAY, "amount")[0],
        state="quarantined", key=redaction_key, quarantine="discarded", store=False,
    )  # fmt: skip
    return SeededHeal(heal_id, run_key, "unscorable")
