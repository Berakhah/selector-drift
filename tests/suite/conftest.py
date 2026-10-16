"""The regression suite's fixtures (Task 26): one Evidence per mutation class, its answer key,
a cache warmed on the unmutated page, and a warmed-up History.

`testbench.answers` / `mark_truth` are permitted here -- tests only, never `selector_drift/`.
Task 26B imports every name below from this module and redefines none.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from bok_core.boundary.sensitivity import Sensitivity

from selector_drift.extract.models import CandidateExtraction
from selector_drift.extract.reader import RowMisaligned, read
from selector_drift.heallog.capture import Evidence
from selector_drift.heallog.replay import NotReproducible, replay
from selector_drift.resolve.cache import CachedSelector, CacheReader
from selector_drift.resolve.cascade import resolve_cascade
from selector_drift.resolve.page import StaticPageSource
from selector_drift.resolve.rungs.r2_axtree import AxTreeRung
from selector_drift.resolve.rungs.r3_anchors import AnchorRung
from selector_drift.resolve.rungs.r4_heuristics import HeuristicRung
from selector_drift.resolve.trace import CascadeTrace, Family, ResolvedElement, Rung
from selector_drift.scope.record import FirstHealPolicy
from selector_drift.spec.loader import load_spec
from selector_drift.spec.schema import FieldTarget
from selector_drift.validate.assertions.base import Assertion
from selector_drift.validate.checks import checks_for
from selector_drift.validate.gate import HealContext, ValidatedExtraction, gate_run
from selector_drift.validate.history import AcceptedRun, History
from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Confirmation, Verdict
from testbench.mutations.base import apply_all
from testbench.mutations.catalogue import CATALOGUE
from testbench.portal.render import render_results  # Task 7: render.py, not data.py

_REPO = Path(__file__).resolve().parents[2]
SPEC = load_spec(_REPO / "specs" / "testbench.yaml")  # version 2 (Task 12): six fields
CLAIM_NUMBER = next(t for t in SPEC.targets if t.name == "claim_number")
AMOUNT = next(t for t in SPEC.targets if t.name == "amount")
BASE_DAY = dt.date(2026, 10, 1)  # fixed: the suite must not depend on the clock (M20)
AT = dt.datetime(2026, 10, 1, 4, 12, tzinfo=dt.UTC)
RUN_KEY = f"testbench:2:{BASE_DAY}"
CONFIRMERS: list[Rung] = [AxTreeRung(), AnchorRung(), HeuristicRung()]
ALL_BENIGN_MUTATION_CLASSES = [e for e in CATALOGUE if not e.adversarial]  # 8
ALL_ADVERSARIAL_MUTATION_CLASSES = [e for e in CATALOGUE if e.adversarial]  # 7


def all_checks(field: FieldTarget, confirmers: Sequence[Rung] = CONFIRMERS) -> list[Assertion]:
    # The bench's scope record: warm-up 5, first_heal_policy auto_promote (Task 5). `confirmers`
    # is overridden only where a test swaps a rung into the cascade too, so the heal and its
    # re-derivation use the same rung instance, as in production.
    return checks_for(
        SPEC, field, confirmers, warmup_runs=5, first_heal_policy=FirstHealPolicy.AUTO_PROMOTE
    )


class _EmptyCache:
    def get(self, target: str, field: str, spec_version: int | None = None) -> None:
        return None


class _DictCache:
    """Read-only per-field cache satisfying CacheReader (Task 13)."""

    def __init__(self, entries: Mapping[str, CachedSelector]) -> None:
        self._entries = dict(entries)

    def get(
        self, target: str, field: str, spec_version: int | None = None
    ) -> CachedSelector | None:
        return self._entries.get(field)


def _resolve_all(html: str) -> dict[str, ResolvedElement]:
    page = StaticPageSource(html)
    return {t.name: resolve_cascade(page, t, _EmptyCache(), record=False)[0] for t in SPEC.targets}


def extract(
    el: ResolvedElement, field: str, *, dom: str, run_key: str = RUN_KEY
) -> CandidateExtraction:
    """One field's (possibly healed) element over the mutated page; the other fields resolve
    cleanly on the same page."""
    resolved = _resolve_all(dom) | {field: el}
    return read(
        dom, resolved, SPEC, run_key=run_key, snapshot_id="0" * 64, traces={}, observed_at=AT
    )


def judge_run(
    ev: Evidence,
    *,
    history: History | None,
    cache: CacheReader,
    confirmers: Sequence[Rung] = CONFIRMERS,
) -> tuple[dict[str, ResolvedElement], ValidatedExtraction | QuarantinedRun | None]:
    """The pipeline's decision core without its I/O (Task 26A): every field replayed against
    the cache on the stored DOM, extracted, and judged by gate_run with the REAL contexts --
    healed from the trace, page = a StaticPageSource over the same DOM. None: a field did not
    resolve or the fields do not share rows, so the run cannot write anything at all."""
    page = StaticPageSource(ev.dom.value)
    elements: dict[str, ResolvedElement] = {}
    traces: dict[str, CascadeTrace] = {}
    contexts: dict[str, HealContext] = {}
    for t in SPEC.targets:
        try:
            el, trace = replay(ev, t, cache)
        except NotReproducible:
            return elements, None
        elements[t.name], traces[t.name] = el, trace
        contexts[t.name] = HealContext(
            healed=trace.healed,
            winning_rung=trace.winner or 5,
            page=page,
            target=t,
            cache=cache,
        )
    try:
        candidate = read(
            ev.dom.value,
            elements,
            SPEC,
            run_key=RUN_KEY,
            snapshot_id=ev.sha256,
            traces=traces,
            observed_at=AT,
        )
    except RowMisaligned:
        return elements, None
    return elements, gate_run(
        candidate, history, contexts, {t.name: all_checks(t, confirmers) for t in SPEC.targets}
    )


def run_pipeline_on(
    ev: Evidence, *, history: History | None, cache: _DictCache
) -> ValidatedExtraction | QuarantinedRun:
    """Task 26B: judge_run's verdict alone. A benign page must never fail to resolve."""
    _, result = judge_run(ev, history=history, cache=cache)
    assert result is not None, "a benign mutation left a field unresolved or rows misaligned"
    return result


def verdicts_of(result: ValidatedExtraction | QuarantinedRun) -> list[Verdict]:
    return list(result.verdicts) if isinstance(result, QuarantinedRun) else []


def confirmer_of(result: ValidatedExtraction | QuarantinedRun, field: str) -> Confirmation | None:
    """What record_heals (Task 26A) would write to confirmer_rung / confirmer_family: a DOM
    confirmer when one passed, else the VALUE family, else None."""
    passing = [
        v.confirmed_by for v in result.verdicts if v.field == field and v.confirmed_by is not None
    ]
    dom = [c for c in passing if c.rung is not None]
    return (dom or passing or [None])[0]


def healed_fields(ev: Evidence, cache: _DictCache) -> list[tuple[str, int]]:
    """(field, winning rung) for every field rung 1 did not carry on this page."""
    page = StaticPageSource(ev.dom.value)
    out: list[tuple[str, int]] = []
    for t in SPEC.targets:
        _, trace = resolve_cascade(page, t, cache, record=False)
        if trace.winner != 1:
            out.append((t.name, trace.winner or 5))
    return out


def family_of(rung: int) -> Family:
    return {2: Family.TEXT_SEMANTIC, 3: Family.TEXT_SEMANTIC, 4: Family.STRUCTURAL}[rung]


@pytest.fixture
def warm_cache() -> Callable[[], _DictCache]:
    """A cache holding the UNMUTATED page's selectors and anchors, as after a clean run."""

    def make() -> _DictCache:
        clean = _resolve_all(render_results(BASE_DAY))
        return _DictCache(
            {
                name: CachedSelector(selector=el.selector, anchor=el.anchor, set_by_rung=el.rung_id)
                for name, el in clean.items()
            }
        )

    return make


_PAGES: dict[str, tuple[str, str]] = {}  # name -> (page, truth-marked twin)


def _pages(name: str) -> tuple[str, str]:
    """ONE mutation instance applied to the page and to its truth-marked twin (Task 7's
    mark_truth), so the answer is structurally identical to the page -- the technique Task 8's
    expected_xpaths uses."""
    if name not in _PAGES:
        entry = next(e for e in CATALOGUE if e.name == name)
        m = entry.build(random.Random(0))
        _PAGES[name] = (
            apply_all([m], render_results(BASE_DAY)),
            apply_all([m], render_results(BASE_DAY, mark_truth=True)),
        )
    return _PAGES[name]


@pytest.fixture(scope="session")
def bench_evidence() -> dict[str, Evidence]:
    """One page per mutation class, each with exactly that ONE class applied to BASE_DAY's
    page, so a failure names the class that caused it."""
    return {
        e.name: Evidence.from_static(
            _pages(e.name)[0], run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC, captured_at=AT
        )
        for e in CATALOGUE
    }


@pytest.fixture(scope="session")
def truth() -> dict[str, dict[str, list[str]]]:
    """name -> field -> the lxml paths of that field's true cells in the mutated page."""
    from lxml import html as lh

    out: dict[str, dict[str, list[str]]] = {}
    for e in CATALOGUE:
        root = lh.fromstring(_pages(e.name)[1])
        tree = root.getroottree()
        out[e.name] = {
            t.name: [tree.getpath(n) for n in root.xpath("//*[@data-sd-truth=$f]", f=t.name)]
            for t in SPEC.targets
        }
    return out


@pytest.fixture
def decoy_evidence(bench_evidence: dict[str, Evidence]) -> Evidence:
    return bench_evidence["DecoyColumn"]


@pytest.fixture(scope="session")
def history() -> History:
    """Six accepted clean runs (the bench warms up in 5), so every history-dependent check
    has a basis and a refusal cannot be blamed on warm-up."""
    runs = []
    for i in range(6):
        day = BASE_DAY - dt.timedelta(days=6 - i)
        html = render_results(day)
        cand = read(
            html,
            _resolve_all(html),
            SPEC,
            run_key=f"testbench:2:{day}",
            snapshot_id="0" * 64,
            traces={},
            observed_at=AT,
        )
        rows = tuple({k: str(c.value) for k, c in row.items()} for row in cand.rows)
        runs.append(
            AcceptedRun(
                run_key=f"testbench:2:{day}",
                window=day,
                rows=rows,
                natural_key=tuple(SPEC.natural_key),
            )
        )
    return History(runs=tuple(runs))
