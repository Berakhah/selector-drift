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
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

import pyarrow as pa
import pyarrow.parquet as pq
from bok_core.boundary.egress import Plane
from pydantic import TypeAdapter

from selector_drift.config import get_settings
from selector_drift.db.models import HealEvent, Quarantine
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


def accept(
    validated: ValidatedExtraction,
    elements: Mapping[str, ResolvedElement],
    *,
    cache: CacheWriter,
    commit_last_accepted: Callable[[], object],
    attempt: int | None = None,
) -> Path:
    """The ONLY call site of the three acceptance writes, in this order. Each runs only after
    the previous one returned; an exception stops the rest (C2, API spec 7.2)."""
    path = land_writer.write(validated, attempt=attempt)  # Task 28: the attempt is in the name
    for field_name, element in elements.items():
        cache.put(validated, element, field=field_name)
    commit_last_accepted()
    return path


def hold(
    run_key: str, reason: str, verdicts: Sequence[Verdict], candidate: CandidateExtraction
) -> str:
    """Quarantine: keep the data, the verdicts and the reason -- never the lake. Returns the
    data_uri. Values plus the lake's provenance column (Task 28), so a promotion can rebuild the
    Claims the gate judged instead of re-judging bare strings."""
    root = get_settings().lake_root / QUARANTINE_DIR
    root.mkdir(parents=True, exist_ok=True)
    final = root / f"{fs_key(run_key)}.parquet"  # ':' is illegal in a Windows file name
    tmp = final.with_name(f".{final.name}.tmp")
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
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    with Session() as s:
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
        accept(
            result,
            elements,
            cache=cache,
            attempt=attempt,
            commit_last_accepted=lambda: write_last_accepted(redacted, target.target, Plane.CLIENT),
        )
        return "healed" if heals else "completed"

    hold(result.run_key, result.reason, result.verdicts, candidate)
    return "quarantined"
