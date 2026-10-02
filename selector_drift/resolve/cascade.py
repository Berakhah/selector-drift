"""The cascade: cheapest and most reliable rung first, every attempt recorded."""

from __future__ import annotations

import dataclasses
import time
from typing import Protocol, runtime_checkable

from selector_drift.resolve.cache import CacheReader
from selector_drift.resolve.escalation import EscalateToHuman
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.recording import ResolutionRecorder, record_resolution
from selector_drift.resolve.rungs.r2_axtree import AxTreeRung
from selector_drift.resolve.trace import (
    AttemptOutcome,
    CascadeTrace,
    DiagnosableRung,
    Miss,
    ResolvedElement,
    Rung,
    RungAttempt,
)
from selector_drift.spec.schema import FieldTarget

# Tasks 14-18 each add one import above and one entry here, cheapest rung first.
REGISTERED_RUNGS: list[Rung] = [AxTreeRung()]


@runtime_checkable
class CacheBound(Protocol):
    """Rung 1: rebuilt per call around the caller's cache and the run's spec version."""

    def bind(self, cache: CacheReader, *, portal: str, spec_version: int | None) -> Rung: ...


@runtime_checkable
class Escalator(Protocol):
    """Rung 5: told what the cascade already tried, so the corpus case carries it."""

    def escalate(
        self,
        page: PageSource,
        target: FieldTarget,
        *,
        run_key: str | None,
        attempts: tuple[RungAttempt, ...],
    ) -> ResolvedElement: ...


def parse_run_key(run_key: str) -> tuple[str, int]:
    """Task 6's run_key(): f"{target}:{spec_version}:{window}"."""
    portal, version, window = run_key.split(":")
    if not portal or not version.isdigit() or not window:
        raise ValueError(f"not a run key: {run_key!r}")
    return portal, int(version)


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def resolve_cascade(
    page: PageSource,
    target: FieldTarget,
    cache: CacheReader,
    *,
    record: bool = True,
    run_key: str | None = None,
    recorder: ResolutionRecorder = record_resolution,
) -> tuple[ResolvedElement, CascadeTrace]:
    """First rung to return an element wins. Raises EscalateToHuman when none does.

    record=True (a scheduled run) needs run_key, writes exactly one resolutions row win or
    lose (C4) and may run rung 5. record=False (replay, the suites) writes nothing and never
    runs rung 5, whose escalation would otherwise add a corpus case per failed replay.
    """
    if record and run_key is None:
        raise ValueError("record=True writes resolutions.run_key (a FK); pass run_key")
    portal, spec_version = parse_run_key(run_key) if run_key is not None else ("", None)
    rungs = [
        r.bind(cache, portal=portal, spec_version=spec_version) if isinstance(r, CacheBound) else r
        for r in REGISTERED_RUNGS
        if record or r.rung_id < 5
    ]

    started = time.perf_counter()
    attempts: list[RungAttempt] = []
    winner: ResolvedElement | None = None
    escalation: EscalateToHuman | None = None
    for rung in rungs:
        t0 = time.perf_counter()
        outcome: AttemptOutcome
        try:
            if isinstance(rung, Escalator):
                result: ResolvedElement | Miss | None = rung.escalate(
                    page, target, run_key=run_key, attempts=tuple(attempts)
                )
            elif isinstance(rung, DiagnosableRung):
                result = rung.attempt(page, target)
            else:
                result = rung.resolve(page, target)
        except EscalateToHuman as exc:
            attempts.append(RungAttempt(rung.rung_id, rung.family, "escalated", _ms(t0)))
            escalation = exc
            break
        if isinstance(result, ResolvedElement):
            node = page.node(result.node_id)
            if result.rung_id != rung.rung_id or node is None or not page.visible(node):
                outcome = "not_visible"  # H9, enforced here too, whatever the rung did
            else:
                attempts.append(RungAttempt(rung.rung_id, rung.family, "resolved", _ms(t0)))
                winner = result
                break
        else:
            outcome = result.outcome if isinstance(result, Miss) else "no_match"
        attempts.append(RungAttempt(rung.rung_id, rung.family, outcome, _ms(t0)))

    trace = CascadeTrace(
        attempts=tuple(attempts), winner=winner.rung_id if winner is not None else None
    )
    if record and run_key is not None:
        rid = recorder(
            run_key=run_key,
            portal=portal,
            target=target,
            trace=trace,
            node_id=winner.node_id if winner is not None else None,
            ms=_ms(started),
        )
        trace = dataclasses.replace(trace, resolution_id=rid)
    if winner is None:
        reason = escalation.reason if escalation else f"no rung resolved {target.name!r}"
        raise EscalateToHuman(
            reason, trace=trace, case=escalation.case if escalation else None
        ) from escalation
    return winner, trace
