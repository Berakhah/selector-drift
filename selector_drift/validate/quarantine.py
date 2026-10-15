# selector_drift/validate/quarantine.py
"""Quarantine keeps the run; a human promotes or discards it, audited (design 5.9).
Promotion RE-RUNS the gate with human_confirmed=True: it supplies missing evidence and never
skips a check. This module never lands data -- orchestrate.accept_promoted does (Task 26A)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import AuditLog, Quarantine
from selector_drift.db.session import Session
from selector_drift.extract.models import CandidateExtraction
from selector_drift.validate.assertions.base import Assertion
from selector_drift.validate.gate import HealContext, QuarantinedRun, ValidatedExtraction, gate_run
from selector_drift.validate.history import History
from selector_drift.validate.verdict import Outcome, Verdict

__all__ = [
    "AlreadyResolved",
    "GateInputs",
    "Landed",
    "NotInQuarantine",
    "QuarantinedRun",
    "Rebuild",
    "StillInvalid",
    "discard",
    "mark_promoted",
    "promote",
    "promote_and_land",
    "reconfirm",
]

_log = logging.getLogger(__name__)
REASON_MAX: Final = 500  # API spec 4.1: DecisionRequest.reason is 1-500


class StillInvalid(Exception):
    """A re-run gate still fails. Carries EVERY verdict so the caller can show which."""

    def __init__(self, verdicts: list[Verdict]) -> None:
        self.verdicts = verdicts
        self.failing = [v for v in verdicts if v.outcome is Outcome.FAIL]
        super().__init__(f"{len(self.failing)} check(s) still fail after human confirmation")


class NotInQuarantine(LookupError):
    pass


class AlreadyResolved(RuntimeError):
    pass


@dataclass(frozen=True)
class Landed:
    """What `land` hands back so promote_and_land can take a landing back if the mark fails.
    `rollback` undoes the landing; `finish` is the maintenance deferred until the mark commits
    (lake compaction cannot be undone, so it must not run before)."""

    rollback: Callable[[], None]
    finish: Callable[[], None]


@dataclass(frozen=True)
class GateInputs:
    candidate: CandidateExtraction
    history: History | None
    contexts: Mapping[str, HealContext]
    checks: Mapping[str, Sequence[Assertion]]


Rebuild = Callable[[str], GateInputs]  # orchestrate supplies it (Task 26A)
SessionFactory = Callable[[], OrmSession]


def reconfirm(run_key: str, inputs: GateInputs) -> ValidatedExtraction:
    if inputs.candidate.run_key != run_key:
        raise ValueError(f"inputs were rebuilt for another run ({inputs.candidate.run_key})")
    confirmed = {
        name: dataclasses.replace(c, human_confirmed=True) for name, c in inputs.contexts.items()
    }
    result = gate_run(inputs.candidate, inputs.history, confirmed, inputs.checks)
    if isinstance(result, QuarantinedRun):
        raise StillInvalid(list(result.verdicts))
    return result


def _decision(actor: str, reason: str) -> tuple[str, str]:
    actor, reason = actor.strip(), reason.strip()
    if not actor:
        raise ValueError("a decision needs an actor")
    if not reason or len(reason) > REASON_MAX:
        raise ValueError(f"a decision needs a reason of 1-{REASON_MAX} characters")
    return actor, reason


def _open(s: OrmSession, run_key: str) -> Quarantine:
    q = s.get(Quarantine, run_key, with_for_update=True)  # row lock: two decisions can't both pass
    if q is None:
        raise NotInQuarantine(f"{run_key} is not in quarantine")
    if q.resolved_as is not None:
        raise AlreadyResolved(f"{run_key} was already {q.resolved_as}")
    return q


def _dump(verdicts: Sequence[Verdict]) -> list[dict[str, object]]:
    return [v.model_dump(mode="json") for v in verdicts]


def promote(
    run_key: str,
    *,
    actor: str,
    reason: str,
    rebuild: Rebuild,
    session_factory: SessionFactory = Session,
) -> ValidatedExtraction:
    """Re-gate only. Marks nothing: the caller lands the data, then calls mark_promoted()
    (ledger 2026-09-26 -- the record must never run ahead of the data)."""
    actor, reason = _decision(actor, reason)
    with session_factory() as s:
        _open(s, run_key)  # refuse before any work
    try:
        return reconfirm(run_key, rebuild(run_key))
    except StillInvalid as exc:
        with session_factory() as s:
            s.add(
                AuditLog(
                    at=dt.datetime.now(dt.UTC),
                    actor=actor,
                    action="promote_refused",
                    subject=run_key,
                    detail={"reason": reason, "failing": _dump(exc.failing)},
                )
            )
            s.commit()
        raise


def _resolve_promoted(
    s: OrmSession,
    q: Quarantine,
    run_key: str,
    actor: str,
    reason: str,
    verdicts: Sequence[Verdict],
) -> None:
    now = dt.datetime.now(dt.UTC)
    q.resolved_as, q.actor, q.resolved_at = "promoted", actor, now
    s.add(
        AuditLog(
            at=now,
            actor=actor,
            action="promote",
            subject=run_key,
            detail={"reason": reason, "verdicts": _dump(verdicts)},
        )
    )


def mark_promoted(
    run_key: str,
    *,
    actor: str,
    reason: str,
    verdicts: Sequence[Verdict],
    session_factory: SessionFactory = Session,
) -> None:
    """Record a promotion whose data has ALREADY landed (Task 26A's accept_promoted)."""
    actor, reason = _decision(actor, reason)
    with session_factory() as s:
        q = _open(s, run_key)  # a concurrent decision wins first
        _resolve_promoted(s, q, run_key, actor, reason, verdicts)
        s.commit()


def promote_and_land(
    run_key: str,
    *,
    actor: str,
    reason: str,
    rebuild: Rebuild,
    land: Callable[[ValidatedExtraction], Landed],
    session_factory: SessionFactory = Session,
) -> ValidatedExtraction:
    """The whole promotion under ONE row lock (Task 26A fix round 1): re-gate, land, mark.

    A discard (or a second promotion) arriving meanwhile waits on the row and is then refused
    with AlreadyResolved, so the audit trail can never read "discarded" over rows that landed.
    If the re-gate fails or `land` raises, the transaction rolls back: nothing is marked and
    the quarantine stays open and retryable. A refused re-gate is audited before the lock goes.

    Fix round 2: the cache and the lake are written through their own sessions, so the rollback
    does not reach them. `land` therefore returns its own compensation (`Landed.rollback`), and
    it is run if anything after it fails -- the mark included -- so a failed promotion leaves no
    file and no cache row, the quarantine open, and a re-promotion clean (re-promotion is
    idempotent: nothing from the failed attempt remains to collide with).
    """
    actor, reason = _decision(actor, reason)
    with session_factory() as s:
        q = _open(s, run_key)  # the lock is held until the commit below, or the rollback
        try:
            validated = reconfirm(run_key, rebuild(run_key))
        except StillInvalid as exc:
            s.add(
                AuditLog(
                    at=dt.datetime.now(dt.UTC),
                    actor=actor,
                    action="promote_refused",
                    subject=run_key,
                    detail={"reason": reason, "failing": _dump(exc.failing)},
                )
            )
            s.commit()
            raise
        landed = land(validated)  # raises -> `land` has compensated itself; still open
        try:
            _resolve_promoted(s, q, run_key, actor, reason, validated.verdicts)
            s.commit()
        except BaseException:
            s.rollback()
            landed.rollback()  # under the row lock: a discard cannot slip in over landed rows
            raise
    try:
        landed.finish()  # the decision is final; maintenance failing must not undo it
    except Exception as exc:
        _log.error("post-promotion maintenance failed: %s", type(exc).__name__)
    return validated


def discard(
    run_key: str, *, actor: str, reason: str, session_factory: SessionFactory = Session
) -> None:
    actor, reason = _decision(actor, reason)
    now = dt.datetime.now(dt.UTC)
    with session_factory() as s:
        q = _open(s, run_key)
        q.resolved_as, q.actor, q.resolved_at = "discarded", actor, now
        s.add(
            AuditLog(
                at=now, actor=actor, action="discard", subject=run_key, detail={"reason": reason}
            )
        )
        s.commit()
