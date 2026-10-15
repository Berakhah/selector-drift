"""What happens after the gate. The ordering in this file is the C2 argument applied to data, to
the selector cache, and to evidence (API spec 7.2).

    settle():
        redact_evidence(evidence)          in memory, BEFORE any write   | RedactionFailed:
                                                                            hold(), nothing else
        for each heal: write_heal_bundle   its 'before' is last-accepted AS IT IS NOW
                       replay_bundle       -> heal_events.replayed_ok
        ValidatedExtraction -> accept():   land.write -> cache.put -> last-accepted   (C2)
        QuarantinedRun      -> hold():     candidate parquet + quarantine row; nothing else

`accept()` is the ONLY call site of the three acceptance writes (pinned by
tests/orchestrate/test_evidence_ordering.py); each runs only after the previous one returned.

A decision already taken is final (fix round 2). `hold()` of a window whose quarantine was
promoted or discarded never overwrites the promoted rows and never re-opens the decision: the
replay is held on its own `quarantine/<key>.replay-<uuid>.parquet`, audited `quarantine_replayed`,
listed by `validate.quarantine.replays_of`, and protected from evidence retention while it
exists. The intended path for a human who discarded a run and then FIXED THE CAUSE is a re-run
under a new run key / an explicit operator action -- out of scope for 26A, a Task 26B follow-up.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pyarrow as pa
import pyarrow.parquet as pq
from bok_core.boundary.egress import Plane
from pydantic import TypeAdapter

from selector_drift.config import get_settings
from selector_drift.db.models import AuditLog, HealEvent, Quarantine
from selector_drift.db.session import Session
from selector_drift.extract.models import CandidateExtraction
from selector_drift.heallog.capture import Evidence
from selector_drift.heallog.evidence import (
    RedactedEvidence,
    RedactionFailed,
    redact_evidence,
    write_heal_bundle,
    write_last_accepted,
)
from selector_drift.heallog.replay import (
    ExpectedHeal,
    HealTimeCache,
    NotReproducible,
    replay_bundle,
)
from selector_drift.land import writer as land_writer
from selector_drift.land.layout import PROVENANCE_COL, fs_key
from selector_drift.orchestrate.cache_writer import CacheWriter
from selector_drift.resolve.cache import CachedSelector
from selector_drift.resolve.trace import ResolvedElement
from selector_drift.scope.record import AuthorisedTarget
from selector_drift.spec.schema import FieldTarget, TaskSpec
from selector_drift.validate.gate import ValidatedExtraction
from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Outcome, Verdict

RunState = Literal["completed", "healed", "quarantined"]
REDACTION_FAILED = "evidence redaction failed"
QUARANTINE_DIR = "quarantine"  # under lake_root; never matched by land.layout.DATA_GLOB
_VERDICTS = TypeAdapter(list[Verdict])
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Accepted:
    """What accept() wrote, and the means to take it back (fix round 2).

    `rollback` removes the lake file and restores the cache rows this call wrote; a caller that
    still has a step to take after accept() (a promotion's mark) runs it if that step fails.
    `finish` is the compaction accept() deferred so that the file stays removable until then."""

    path: Path
    rollback: Callable[[], None]
    finish: Callable[[], None]


def _undo(steps: Sequence[Callable[[], None]]) -> None:
    """Run every compensation, newest first; one failing never stops the others. A failed
    compensation is logged loudly (type only: never page text) and re-raised after the rest."""
    failed: list[BaseException] = []
    for step in reversed(steps):
        try:
            step()
        except Exception as exc:  # keep compensating; report all of them
            failed.append(exc)
            _log.error("compensation step failed: %s", type(exc).__name__)
    if failed:
        raise RuntimeError(f"{len(failed)} compensation step(s) failed") from failed[0]


def accept(
    validated: ValidatedExtraction,
    elements: Mapping[str, ResolvedElement],
    *,
    cache: CacheWriter,
    commit_last_accepted: Callable[[], object],
    attempt: int | None = None,
    defer_compaction: bool = False,
) -> Accepted:
    """The ONLY call site of the three acceptance writes, in this order. Each runs only after
    the previous one returned; an exception stops the rest (C2, API spec 7.2).

    All-or-nothing for what it controls: if the cache write or the last-accepted move fails,
    the lake file is removed and the cache rows this call wrote are restored before the
    exception propagates -- nothing is left that a later discard would contradict. (The
    last-accepted page itself is idempotent to re-promote and is not reverted.)"""
    path = land_writer.write(validated, attempt=attempt, compact=False)  # Task 28: attempt
    steps: list[Callable[[], None]] = [lambda: path.unlink(missing_ok=True)]
    try:
        for field_name, element in elements.items():
            steps.append(cache.put(validated, element, field=field_name))
        commit_last_accepted()
    except BaseException:
        with contextlib.suppress(RuntimeError):  # logged by _undo; the caller must see the original
            _undo(steps)
        raise

    def finish() -> None:
        land_writer.compact_if_needed(path)

    if not defer_compaction:
        finish()
    return Accepted(path, lambda: _undo(steps), finish)


def hold(
    run_key: str, reason: str, verdicts: Sequence[Verdict], candidate: CandidateExtraction
) -> str:
    """Quarantine: keep the data, the verdicts and the reason -- never the lake. Returns the
    data_uri. Values plus the lake's provenance column (Task 28), so a promotion can rebuild the
    Claims the gate judged instead of re-judging bare strings."""
    root = get_settings().lake_root / QUARANTINE_DIR
    root.mkdir(parents=True, exist_ok=True)
    records = [
        {name: str(claim.value) for name, claim in row.items()}
        | {
            PROVENANCE_COL: json.dumps(
                {
                    name: claim.model_dump(mode="json", exclude={"value"})
                    for name, claim in row.items()
                },
                sort_keys=True,
            )
        }
        for row in candidate.rows
    ]
    table = (
        pa.Table.from_pylist(records)
        if records
        else pa.table({PROVENANCE_COL: pa.array([], pa.string())})
    )
    stem = fs_key(run_key)  # ':' is illegal in a Windows file name
    with Session() as s:
        # The row lock serialises a replay with a human's decision (fix round 1).
        existing = s.get(Quarantine, run_key, with_for_update=True)
        resolved_as = existing.resolved_as if existing is not None else None
        # A decision already taken is never re-opened, and the rows it promoted are never
        # overwritten: a replay of a resolved window is held on its own file and only audited.
        final = root / (
            f"{stem}.replay-{uuid.uuid4().hex[:12]}.parquet"
            if resolved_as is not None
            else f"{stem}.parquet"
        )
        tmp = final.with_name(f".{final.name}.tmp")
        try:
            pq.write_table(table, tmp)
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        if resolved_as is not None:
            s.add(
                AuditLog(
                    at=dt.datetime.now(dt.UTC),
                    actor="scheduler",
                    action="quarantine_replayed",
                    subject=run_key,
                    detail={"resolved_as": resolved_as, "reason": reason, "data_uri": str(final)},
                )
            )
        else:
            s.merge(
                Quarantine(
                    run_key=run_key,
                    reason=reason,
                    verdicts=_VERDICTS.dump_python(list(verdicts), mode="json"),
                    data_uri=str(final),
                    resolved_as=None,
                    actor=None,
                    resolved_at=None,
                )
            )
        s.commit()
    return str(final)


def redaction_verdict(exc: RedactionFailed) -> Verdict:
    """RedactionFailed messages name counts and causes, never page text (Task 33)."""
    return Verdict(
        check="EvidenceRedaction",
        outcome=Outcome.FAIL,
        threshold="verification re-runs every detector and finds nothing",
        observed="redaction not proven",
        reason=str(exc) or type(exc).__name__,
    )


def bundle_and_replay(
    heal: HealEvent,
    target: str,
    after: RedactedEvidence,
    field_target: FieldTarget,
    cached_before: CachedSelector | None,
    node_id: str,
) -> None:
    """Write the heal bundle (its 'before' is last-accepted AS IT IS NOW), then prove the heal
    reproduces from what was stored. EvidenceCorrupted propagates: a bundle that fails its own
    hash seconds after being written is a disk fault, and the run fails loudly."""
    uri = write_heal_bundle(heal.id, target, after, Plane.CLIENT)
    try:
        replay_bundle(
            Path(uri),
            field_target,
            HealTimeCache(cached_before),
            ExpectedHeal(heal.winning_rung, node_id),
        )
        reproduced = True
    except NotReproducible:
        reproduced = False
    with Session() as s:
        row = s.get(HealEvent, heal.id)
        if row is None:
            raise LookupError(f"heal {heal.id} vanished before its evidence was recorded")
        row.evidence_uri = uri
        row.replayed_ok = reproduced
        s.commit()


def settle(
    *,
    target: AuthorisedTarget,
    spec: TaskSpec,
    candidate: CandidateExtraction,
    result: ValidatedExtraction | QuarantinedRun,
    evidence: Evidence,
    elements: Mapping[str, ResolvedElement],
    heals: Sequence[HealEvent],
    cache: CacheWriter,
    cache_before: Mapping[str, CachedSelector | None],
    redaction_key: bytes,
    attempt: int | None = None,
) -> RunState:
    try:
        redacted = redact_evidence(evidence, key=redaction_key)  # before ANY write
    except RedactionFailed as exc:
        # Ledger 670: recorded with its reason and quarantined, even when the gate passed --
        # an accepted run whose evidence cannot be stored is a heal nobody could replay (B5).
        # The gate's own verdicts stay with it.
        hold(
            candidate.run_key,
            REDACTION_FAILED,
            [redaction_verdict(exc), *result.verdicts],
            candidate,
        )
        return "quarantined"

    fields = {t.name: t for t in spec.targets}
    for heal in heals:  # before last-accepted can move
        bundle_and_replay(
            heal,
            target.target,
            redacted,
            fields[heal.field],
            cache_before.get(heal.field),
            elements[heal.field].node_id,
        )

    if isinstance(result, ValidatedExtraction):
        # Only a heal changes what a field's selector is. A rung-1 hit already IS its cache
        # entry; re-writing it would only move set_by_run_key off the run that set it.
        healed = {name: el for name, el in elements.items() if el.rung_id != 1}
        accept(
            result,
            healed,
            cache=cache,
            attempt=attempt,
            commit_last_accepted=lambda: write_last_accepted(redacted, target.target, Plane.CLIENT),
        )
        return "healed" if heals else "completed"

    hold(result.run_key, result.reason, result.verdicts, candidate)
    return "quarantined"
