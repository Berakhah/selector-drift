"""The assembled pipeline against the real bench portal (Task 26A): a real Chromium, the real
scope gate, cascade, gate, lake, quality and evidence. Integration-marked like Task 6's flow
tests, which already drive a real browser.

A target with an empty cache and no history quarantines its first run whatever the page
(user-confirmed ruling, ledger 557): every field is a heal, and on a heal the VALUE family has
nothing to compare against, so the strict path refuses. The tests that need an accepted run
reach one the only way production can -- a human promotion (test_promotion_end_to_end.py)."""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import (
    AuditLog,
    HealEvent,
    Heartbeat,
    Quarantine,
    Resolution,
    Run,
    RunAttempt,
    SelectorCacheEntry,
)
from selector_drift.db.session import Session
from selector_drift.land.catalog import query
from selector_drift.orchestrate.flows import extraction_flow
from selector_drift.orchestrate.history import history_for
from selector_drift.orchestrate.pipeline import default_confirmers, run_once
from selector_drift.resolve.trace import Rung
from selector_drift.spec.loader import load_spec
from tests.orchestrate.conftest import Bench, _DemoAccount

REPO = Path(__file__).resolve().parents[2]
SPEC = load_spec(REPO / "specs" / "testbench.yaml")  # version 2 (Task 12 Step 0)
ONCE = extraction_flow.with_options(retries=0)  # no 300 s retry sleeps in a test


def run(window: dt.date, confirmers: Sequence[Rung] | None = None) -> str:
    """One run of the product for an explicit window -- run_once, without the scheduler."""
    return asyncio.run(
        run_once(
            "testbench.yaml",
            window=window,
            credentials_for=lambda target: _DemoAccount(),
            confirmers=default_confirmers() if confirmers is None else confirmers,
        )
    )


def rows_in_lake(key: str) -> int:
    return len(query("select * from lake where run_key = ?", [key], target="testbench").rows)


def state_of(key: str) -> str:
    with Session() as s:
        run_ = s.get(Run, key)
        assert run_ is not None
        return run_.state


def cache_rows() -> int:
    with Session() as s:
        return int(s.scalar(select(func.count()).select_from(SelectorCacheEntry)) or 0)


@pytest.mark.integration
def test_a_cold_start_target_quarantines_its_first_run_and_writes_nothing(
    prefect_api: None, bench: Bench, session: OrmSession
) -> None:
    """Ledger 557 (user-confirmed), part one: empty cache, no history, a clean page. The run is
    held -- data, verdicts, heals and evidence kept -- and nothing reaches the lake, the cache
    or the history. Part two (the promotion) is in test_promotion_end_to_end.py."""
    key = ONCE("testbench.yaml")
    assert state_of(key) == "quarantined"
    assert rows_in_lake(key) == 0 and cache_rows() == 0
    assert history_for(session, "testbench", SPEC) is None
    q = session.get(Quarantine, key)
    assert q is not None and q.resolved_as is None and Path(q.data_uri).exists()
    refused = {v["check"] for v in q.verdicts if v["outcome"] == "INSUFFICIENT"}
    assert "RowKeyContinuity" in refused  # no accepted run to compare a heal against
    heals = session.query(HealEvent).filter_by(run_key=key).all()
    assert {h.field for h in heals} == {t.name for t in SPEC.targets}  # every field healed
    assert all(h.evidence_uri and h.replayed_ok for h in heals)  # stored and reproducible
    attempt = session.query(RunAttempt).filter_by(run_key=key).one()
    assert (attempt.state, attempt.error) == ("quarantined", q.reason)


@pytest.mark.integration
def test_a_refused_target_never_opens_a_browser_and_never_captures(
    prefect_api: None, db: None, expired_spec: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real only because pipeline.py calls `driver.open(...)` through the module (2026-09-26);
    # a `from ...driver import open` binding would make this patch vacuous.
    opened: list[int] = []
    monkeypatch.setattr("selector_drift.browser.driver.open", lambda *a, **k: opened.append(1))
    assert state_of(ONCE(expired_spec)) == "refused"
    assert opened == []


@pytest.mark.integration
def test_every_resolution_lands_in_the_resolutions_table(
    prefect_api: None, bench: Bench, session: OrmSession
) -> None:
    key = ONCE("testbench.yaml")
    rows = session.query(Resolution).filter_by(run_key=key).all()
    assert sorted(r.field for r in rows) == sorted(t.name for t in SPEC.targets)
    assert all(r.winning_rung in (2, 3, 4) for r in rows)  # cold start: rungs 2-4 carry it
    heals = {h.field: h.resolution_id for h in session.query(HealEvent).filter_by(run_key=key)}
    assert heals == {r.field: r.id for r in rows}  # each heal joins its resolution


@pytest.mark.integration
def test_an_authorised_run_audits_its_scope_record(
    prefect_api: None, bench: Bench, session: OrmSession
) -> None:
    """Task 2's promise, kept by the pipeline too (Task 6's record_authorisation)."""
    key = ONCE("testbench.yaml")
    assert session.query(AuditLog).filter_by(action="scope_authorised", subject=key).count() == 1


@pytest.mark.integration
def test_a_failed_run_writes_an_attempt_row_and_a_retry_writes_a_second(
    prefect_api: None, flaky_bench: Bench, session: OrmSession
) -> None:
    """M13. Attempt numbers come from next_attempt(), so the retry appends -- it never
    overwrites attempt 1 the way Prefect's run_count (1 for every new flow run) would."""
    with pytest.raises(Exception, match="HTTP 503"):
        ONCE("testbench.yaml")  # the portal answers 503
    key = ONCE("testbench.yaml")  # the retry settles (cold start: held, not failed)
    attempts = session.query(RunAttempt).filter_by(run_key=key).order_by(RunAttempt.attempt).all()
    assert [(a.attempt, a.state) for a in attempts] == [(1, "failed"), (2, "quarantined")]


@pytest.mark.integration
def test_an_escalated_field_quarantines_the_run_and_never_fails_the_flow(
    prefect_api: None, bench: Bench, session: OrmSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design 5.3 / 9.3: no rung resolved -> quarantine with reason 'escalated'. Raising instead
    would hand the run to Prefect's retries, duplicating resolutions rows and corpus cases."""
    from selector_drift.resolve.escalation import EscalateToHuman

    def refuse(page: object, target: object, cache: object, **kw: object) -> None:
        raise EscalateToHuman(f"no rung resolved {getattr(target, 'name', '?')!r}")

    monkeypatch.setattr("selector_drift.orchestrate.pipeline.resolve_cascade", refuse)
    key = ONCE("testbench.yaml")
    assert state_of(key) == "quarantined"
    q = session.get(Quarantine, key)
    assert q is not None and q.reason == "escalated" and q.data_uri
    assert {v["check"] for v in q.verdicts} == {"Escalated"}
    assert rows_in_lake(key) == 0 and cache_rows() == 0


@pytest.mark.integration
def test_every_fire_writes_exactly_one_heartbeat_including_refusals(
    prefect_api: None, db: None, expired_spec: str
) -> None:
    key = ONCE(expired_spec)
    with Session() as s:
        assert s.query(Heartbeat).filter_by(run_key=key).count() == 1


@pytest.mark.integration
def test_every_settled_run_is_notified(
    prefect_api: None, bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 29A: notify(findings, run) on every settled run -- here a quarantine."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.pipeline.notify",
        lambda findings, run_: seen.append((run_.run_key, run_.state)),
    )
    key = ONCE("testbench.yaml")
    assert seen == [(key, "quarantined")]


@pytest.mark.integration
def test_a_spec_edit_is_observed_even_on_a_refused_run(
    prefect_api: None, db: None, expired_spec: str
) -> None:
    """Task 29A (M1): observe_spec runs before authorisation."""
    ONCE(expired_spec)
    with Session() as s:
        assert s.query(AuditLog).filter_by(action="spec_seen", subject="expired").count() == 1


def _run_once_calls() -> list[ast.Call]:
    src = Path("selector_drift/orchestrate/pipeline.py").read_text(encoding="utf-8")
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_once"
    )
    return [c for c in ast.walk(fn) if isinstance(c, ast.Call)]


@pytest.mark.unit
def test_the_pipeline_observes_the_spec_before_it_resolves_anything() -> None:
    """Task 29A's dropped test, landed with pipeline.py."""
    lines: dict[str, int] = {}
    for c in _run_once_calls():
        lines.setdefault(ast.unparse(c.func), c.lineno)
    assert lines["observe_spec"] < lines["authorise"] < lines["resolve_cascade"]


@pytest.mark.unit
def test_every_heal_context_the_product_builds_carries_the_extracted_node() -> None:
    """Ledger 551: IndependentRungAgreement compares the re-derived node with ctx.node_id; a
    product context built without it would silently skip that comparison."""
    built = 0
    for path in Path("selector_drift").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "HealContext":
                built += 1
                kws = {k.arg for k in node.keywords}
                assert "node_id" in kws, (
                    f"{path}:{node.lineno} builds a HealContext without node_id"
                )
    assert built >= 2  # the pipeline and the promotion rebuild


@pytest.mark.unit
def test_capture_happens_before_any_resolution() -> None:
    """Evidence must be of the page the resolver actually saw (B5)."""
    lines: dict[str, int] = {}
    for c in _run_once_calls():
        lines.setdefault(ast.unparse(c.func), c.lineno)
    assert lines["capture"] < lines["resolve_cascade"] < lines["record_boxes"]
