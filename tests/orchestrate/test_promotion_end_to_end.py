"""Accepted runs, reached the only way production can reach them: a cold-start run held, a
human promotion, then the runs that follow (Task 26A, ledger 504 and 557, both user-confirmed).

Every run here is the real product against the real bench portal: Chromium, the scope gate,
the cascade, the gate, the lake, quality and evidence. Windows are explicit so one test can
run several days; the bench's own clock is moved between runs to schedule a mutation."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import (
    AuditLog,
    HealEvent,
    Intervention,
    Quarantine,
    Resolution,
    Run,
    SelectorCacheEntry,
)
from selector_drift.db.session import Session
from selector_drift.heallog.bundle import AFTER_HTML, PAGE_HTML
from selector_drift.heallog.evidence import heal_bundle_dir, last_accepted_dir
from selector_drift.orchestrate.history import history_for
from selector_drift.orchestrate.quarantine_actions import build_quarantine_actions
from selector_drift.resolve import cascade
from selector_drift.resolve.grid import column_header
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.rungs.base import css_path
from selector_drift.resolve.rungs.r1_cache import CacheRung
from selector_drift.resolve.rungs.r2_axtree import AxTreeRung
from selector_drift.resolve.rungs.r3_anchors import AnchorRung
from selector_drift.resolve.rungs.r4_heuristics import HeuristicRung
from selector_drift.resolve.rungs.r5_visual import VisualRung
from selector_drift.resolve.trace import AnchorEvidence, ResolvedElement, Rung
from selector_drift.spec.schema import FieldTarget
from selector_drift.validate.quarantine import StillInvalid
from tests.orchestrate.conftest import Bench
from tests.orchestrate.test_pipeline_end_to_end import SPEC, rows_in_lake, run, state_of
from tests.suite.test_adversarial_refused import _OffByOneColumnRung

W0 = dt.date(2026, 9, 1)
W1 = W0 + dt.timedelta(days=1)


def cache() -> dict[str, tuple[str, str]]:
    """field -> (selector, set_by_run_key): the whole selector cache for the bench."""
    with Session() as s:
        return {
            e.field: (e.selector, e.set_by_run_key)
            for e in s.scalars(
                select(SelectorCacheEntry).where(SelectorCacheEntry.target == "testbench")
            )
        }


def promote(key: str) -> None:
    build_quarantine_actions().promote(key, "op1", "first run checked against the portal")


def warm(bench: Bench) -> str:
    """The production path to a warm target: run one is held, a human promotes it."""
    key = run(W0)
    assert state_of(key) == "quarantined"
    promote(key)
    return key


@pytest.mark.integration
def test_a_cold_start_target_is_populated_only_by_the_human_promotion(
    bench: Bench, session: OrmSession
) -> None:
    """Ledger 557 (user-confirmed), part two. Before the promotion: nothing in the cache, the
    lake or the history. The promotion -- and nothing else -- lands the held rows, writes the
    cache and makes the run history; the next run is then an ordinary cache hit."""
    key = run(W0)
    assert state_of(key) == "quarantined"
    assert cache() == {} and rows_in_lake(key) == 0
    assert history_for(session, "testbench", SPEC) is None

    promote(key)

    assert {f: by for f, (_sel, by) in cache().items()} == {t.name: key for t in SPEC.targets}
    assert rows_in_lake(key) > 0
    history = history_for(session, "testbench", SPEC)
    assert history is not None and [r.run_key for r in history.runs] == [key]
    assert len(history.runs[0].rows) == rows_in_lake(key)
    with Session() as s:
        q = s.get(Quarantine, key)
        assert q is not None and (q.resolved_as, q.actor) == ("promoted", "op1")
        assert s.get(Run, key).state == "quarantined"  # the run record is not rewritten
        assert s.query(AuditLog).filter_by(action="promote", subject=key).count() == 1
        (iv,) = s.query(Intervention).filter_by(kind="manual_promote").all()
        assert (iv.actor, iv.target) == ("op1", "testbench")
        heals = s.query(HealEvent).filter_by(run_key=key).all()
        assert heals and all(h.promoted_by == "op1" and h.promoted_at for h in heals)
        latest = max(heals, key=lambda h: h.id)
    assert (last_accepted_dir("testbench") / PAGE_HTML).read_bytes() == (
        heal_bundle_dir(latest.id) / AFTER_HTML
    ).read_bytes()

    nxt = run(W1)  # the same clean page, the next day
    assert state_of(nxt) == "completed"
    assert rows_in_lake(nxt) > 0
    with Session() as s:
        assert s.query(HealEvent).filter_by(run_key=nxt).count() == 0
    assert {f: by for f, (_sel, by) in cache().items()} == {t.name: key for t in SPEC.targets}


@pytest.mark.integration
def test_a_clean_run_writes_rows_and_updates_nothing_else(
    bench: Bench, session: OrmSession
) -> None:
    warm(bench)
    before = cache()
    key = run(W1)
    assert session.get(Run, key).state == "completed"
    assert rows_in_lake(key) > 0
    assert session.query(HealEvent).filter_by(run_key=key).count() == 0
    assert cache() == before


@pytest.mark.integration
def test_a_healed_run_writes_rows_a_heal_event_and_the_cache(
    bench_mutated: Bench, session: OrmSession
) -> None:
    warm(bench_mutated)
    assert bench_mutated.mutated_day is not None
    bench_mutated.day = bench_mutated.mutated_day  # LabelReword is scheduled today
    before = cache()
    key = run(W1)
    assert state_of(key) == "healed", session.get(Quarantine, key)
    assert rows_in_lake(key) > 0
    heals = session.query(HealEvent).filter_by(run_key=key).all()
    assert heals and all(h.confirmer_family for h in heals)
    after = cache()
    for h in heals:  # every healed field's cache entry now names this run
        assert after[h.field] == (h.selector_after, key)
    unhealed = set(before) - {h.field for h in heals}
    assert {f: after[f] for f in unhealed} == {f: before[f] for f in unhealed}


class _RealisticOffByOne(_OffByOneColumnRung):
    """Task 26's off-by-one rung 4, made realistic: it records a real CSS path to the decoy cell
    and the decoy column's own header as its anchor -- exactly what a real structural heal
    would store. A poisoned cache entry built from it WOULD verify at rung 1 tomorrow, which is
    what makes the next-day test below able to see a cache written from a quarantined run."""

    def resolve(self, page: PageSource, target: FieldTarget) -> ResolvedElement | None:
        found = super().resolve(page, target)
        node = page.node(found.node_id) if found is not None else None
        if found is None or node is None:
            return None
        header = column_header(page, node)
        return ResolvedElement(
            selector=css_path(node),
            node_id=found.node_id,
            anchor=AnchorEvidence(header_text=page.name(header) if header is not None else None),
            rung_id=4,
        )


def fool_the_cascade(monkeypatch: pytest.MonkeyPatch) -> list[Rung]:
    """The plausible-but-wrong heal, made reachable in the live pipeline. The real cascade is
    not fooled by DecoyColumn (rung 2 heals to the true Amount column and rung 4 agrees), so an
    off-by-one rung 4 -- which trusts Amount's old column position and lands on the decoy
    "Prior Amount" cell -- is placed in the cascade where it wins for `amount`, and the SAME
    instance is injected as the gate's rung-4 healer, as in production. Returns the confirmers
    the run must use."""
    fooled = _RealisticOffByOne()
    monkeypatch.setattr(
        cascade,
        "REGISTERED_RUNGS",
        [CacheRung(), fooled, AxTreeRung(), AnchorRung(), HeuristicRung(), VisualRung()],
    )
    return [AxTreeRung(), AnchorRung(), fooled]


def decoy_day(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> list[Rung]:
    warm(bench)  # with the real cascade
    assert bench.mutated_day is not None
    bench.day = bench.mutated_day  # DecoyColumn is scheduled today
    return fool_the_cascade(monkeypatch)


def failing(key: str) -> list[dict[str, str]]:
    with Session() as s:
        q = s.get(Quarantine, key)
        assert q is not None
        return [v for v in q.verdicts if v["outcome"] == "FAIL"]


@pytest.mark.integration
def test_a_quarantined_run_writes_no_rows_and_does_not_touch_the_cache(
    bench_decoy: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heal onto the adjacent decoy column, on a warm target: refused by a FAIL -- the
    healer's node disagrees across families, or its values moved while the keys did not --
    and nothing reaches the lake or the cache (C2)."""
    confirmers = decoy_day(bench_decoy, monkeypatch)
    before = cache()
    key = run(W1, confirmers)
    assert state_of(key) == "quarantined"
    assert {(v["field"], v["check"]) for v in failing(key)} & {
        ("amount", "IndependentRungAgreement"),
        ("amount", "RowKeyContinuity"),
    }
    assert rows_in_lake(key) == 0
    assert cache() == before  # C2


@pytest.mark.integration
def test_the_next_run_after_a_quarantine_is_judged_as_strictly(
    bench_decoy: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ledger 504: had the refused heal written the cache, tomorrow's run would be a rung-1 hit
    on the decoy -- healed=False, the strict path never engaged, and the decoy written every
    night after. Instead `amount` heals again tomorrow and is refused again."""
    confirmers = decoy_day(bench_decoy, monkeypatch)
    first = run(W1, confirmers)
    second = run(W1 + dt.timedelta(days=1), confirmers)  # the bench still serves the decoy
    assert (state_of(first), state_of(second)) == ("quarantined", "quarantined")
    with Session() as s:
        amount = s.query(Resolution).filter_by(run_key=second, field="amount").one()
    assert amount.winning_rung != 1  # a heal again, judged strictly: never a cache hit
    assert {v["field"] for v in failing(second)} >= {"amount"}


@pytest.mark.integration
def test_a_human_cannot_promote_past_a_failing_check(
    bench_decoy: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promotion supplies the missing confirmation; it never overrides a FAIL. Nothing lands,
    the cache is untouched, and the quarantine stays open with the refusal audited."""
    confirmers = decoy_day(bench_decoy, monkeypatch)
    before = cache()
    key = run(W1, confirmers)
    assert failing(key)
    with pytest.raises(StillInvalid):
        promote(key)
    assert rows_in_lake(key) == 0 and cache() == before
    with Session() as s:
        q = s.get(Quarantine, key)
        assert q is not None and q.resolved_as is None
        assert s.query(AuditLog).filter_by(action="promote_refused", subject=key).count() == 1


@pytest.mark.integration
def test_an_escalated_run_has_nothing_to_promote(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    from selector_drift.resolve.escalation import EscalateToHuman

    def refuse(page: object, target: object, cache: object, **kw: object) -> None:
        raise EscalateToHuman("no rung resolved")

    monkeypatch.setattr("selector_drift.orchestrate.pipeline.resolve_cascade", refuse)
    key = run(W0)
    with pytest.raises(ValueError, match="nothing to promote"):
        promote(key)
    with Session() as s:
        q = s.get(Quarantine, key)
        assert q is not None and q.resolved_as is None
