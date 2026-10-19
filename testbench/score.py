"""Judges every bench heal against the answer key.

The ONLY bench module allowed to read the product's tables (import contract
only-the-scorer-reads-the-product, Task 8). Read-only against the product, except for the
audit_log rows it writes about its own judgements.

The row shape is a contract (pinned 2026-09-26): one audit_log row per heal, actor
'testbench.score', action 'scored', subject str(heal_id), and detail carrying `judgement`,
`field` and `run_key`. Task 29A's notify_false_accepts and Task 34B read exactly this shape.

The day a stored page was served is recovered from its sd-mutation-seed tag, never from the
clock: a run at 23:59 UTC can capture a page served for the previous day. Found while
implementing (2026-10-17): the product's evidence redaction (heallog.evidence, value-level since
Task 33 fix round 1) redacts attribute values too, and the seed's hex digest is almost always
matched by a detector (IBAN, digit runs) -- so a stored after.html rarely carries the raw seed.
A candidate day therefore also explains the tag when the bench's own tag content for that day,
redacted under the evidence key, is exactly what was stored. That comparison is exact (the
surrogates are keyed), so it never guesses between two days.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from enum import StrEnum
from pathlib import Path

import lxml.etree
import lxml.html
from bok_core.boundary.redact import redact
from bok_core.boundary.sensitivity import Labelled, Sensitivity
from prefect import flow
from sqlalchemy import Select, Text, cast, exists, select, text
from sqlalchemy.orm import Session

from selector_drift.db.models import AuditLog, HealEvent, Quarantine, Resolution, Run
from selector_drift.heallog.bundle import (
    AFTER_HTML,
    EvidenceCorrupted,
    read_heal_manifest,
    verified_bytes,
)
from selector_drift.heallog.evidence import heal_bundle_dir
from selector_drift.orchestrate.history import accepted_run
from selector_drift.orchestrate.run_record import NO_TERMINAL_STATE
from testbench.answers.key import expected_xpaths
from testbench.portal.hook import SEED_META
from testbench.portal.hook import TARGET as TARGET
from testbench.schedule import scheduled_names, seed_for

_log = logging.getLogger(__name__)
ACTOR = "testbench.score"
ACTION = "scored"
UNSCORABLE = "unscorable"
# The label the bench's evidence is stored under: its scope record's basis is self-owned, which
# scope.record (R4) maps to SYNTHETIC.
BENCH_LABEL = Sensitivity.SYNTHETIC


class Judgement(StrEnum):
    TRUE_ACCEPT = "true_accept"
    CORRECT_REFUSAL = "correct_refusal"
    FALSE_ACCEPT = "false_accept"  # a wrong heal the gate let through -- the one that matters
    FALSE_REFUSAL = "false_refusal"


class UnscorableHeal(Exception):
    """The stored page cannot be tied to a bench day, or its node cannot be found."""


def _explains(meta: str, day: dt.date, target: str, key: bytes | None) -> bool:
    seed = seed_for(day, target)
    if meta.split(";", 1)[0] == seed:
        return True  # an unredacted tag (or one no detector happened to match)
    if key is None:
        return False
    served = f"{seed};{','.join(scheduled_names(day, target))}"  # exactly what mutate_html writes
    return redact(Labelled(served, BENCH_LABEL), key=key).text == meta


def seed_day(
    meta_seed: str, captured_at: dt.datetime, target: str = TARGET, *, key: bytes | None = None
) -> dt.date:
    """The bench day whose seed explains `meta_seed` (the tag's content, or its seed alone):
    the capture's UTC date, else the day before. Neither -> UnscorableHeal, never a guess.
    `key` is the evidence redaction key, needed only when the stored tag was redacted."""
    at = captured_at if captured_at.tzinfo is not None else captured_at.replace(tzinfo=dt.UTC)
    today = at.astimezone(dt.UTC).date()
    for day in (today, today - dt.timedelta(days=1)):
        if _explains(meta_seed, day, target, key):
            return day
    raise UnscorableHeal(f"the stored seed tag matches neither {today} nor the day before")


def _parse(html: str) -> lxml.html.HtmlElement:
    # The answer key's parser (testbench.mutations.base.parse), so paths are computed alike.
    try:
        return lxml.html.document_fromstring(html)
    except (lxml.etree.LxmlError, ValueError):  # lxml's own ValueErrors: empty, encoding decl
        raise UnscorableHeal("the stored page could not be parsed") from None


def meta_seed(after_html: str) -> str:
    """The stored page's sd-mutation-seed tag content."""
    content = _parse(after_html).xpath(f"string(//meta[@name='{SEED_META}']/@content)")
    if not content:
        raise UnscorableHeal("the stored page carries no sd-mutation-seed tag")
    return str(content)


def truth_path(after_html: str, node_id: str) -> str:
    """Node identity, never selector text (C5): a StaticPageSource node_id is already lxml's
    getpath(); a LivePageSource one is the data-sd-node token, found in the stored page."""
    if node_id.startswith("/"):
        return node_id
    root = _parse(after_html)
    hits = root.xpath("//*[@data-sd-node=$n]", n=node_id)
    if len(hits) != 1:
        raise UnscorableHeal(f"live node {node_id!r} found {len(hits)} times in the stored page")
    return str(root.getroottree().getpath(hits[0]))


def judge(*, correct: bool, accepted: bool) -> Judgement:
    if accepted:
        return Judgement.TRUE_ACCEPT if correct else Judgement.FALSE_ACCEPT
    return Judgement.FALSE_REFUSAL if correct else Judgement.CORRECT_REFUSAL


def _pending() -> Select[tuple[int, str, str, str | None, str | None, bool]]:
    """Every bench heal whose outcome is SETTLED and that has no 'scored' row yet.

    A heal is scored once, so it is scored only once the product has decided (fix round 1, C1):
    - a run whose quarantine row is still unresolved is held -- a human may yet promote it, and
      a decoy promoted at 09:00 must count as a FALSE_ACCEPT, not stay frozen as the refusal it
      looked like at 04:42. So an undecided quarantine is ABSENT from the correct-refusal count
      until a human promotes or discards it;
    - a run held at NO_TERMINAL_STATE (run_record.record_attempt: in progress, or retrying) is
      held -- it may still settle `healed`.

    Accepted is the product's own predicate (orchestrate.history.accepted_run): the run settled
    completed/healed, or its quarantine was promoted. A heal with no resolutions row is selected
    too (outer join), so it is recorded unscorable rather than left pending forever. Run only
    under SCORER_LOCK_KEY (score_pending takes it first): that, not a row lock (the scorer's
    role has no UPDATE, so it cannot take one), is what stops two overlapping scorers judging
    one heal twice -- under READ COMMITTED the NOT EXISTS on audit_log is re-read only by a
    statement started after the first scorer commits."""
    undecided = select(Quarantine.run_key).where(Quarantine.resolved_as.is_(None))
    already = exists().where(
        AuditLog.action == ACTION,
        AuditLog.actor == ACTOR,
        AuditLog.subject == cast(HealEvent.id, Text),
    )
    return (
        select(
            HealEvent.id,
            HealEvent.run_key,
            HealEvent.field,
            HealEvent.evidence_uri,
            Resolution.node_id,
            accepted_run().label("accepted"),
        )
        .join(Run, Run.run_key == HealEvent.run_key)
        .outerjoin(Resolution, Resolution.id == HealEvent.resolution_id)
        .where(
            HealEvent.target == TARGET,
            HealEvent.run_key.not_in(undecided),
            Run.error.is_distinct_from(NO_TERMINAL_STATE),
            ~already,
        )
        .order_by(HealEvent.id)
    )


_RECORD = text(
    "insert into audit_log (at, actor, action, subject, detail) "
    "values (now(), :actor, :action, :subject, cast(:detail as jsonb))"
)
# Round 2: one scorer at a time. A stable bigint (b"sd-score" as a big-endian int); held for the
# whole transaction, so an overlapping run waits, then sees the first run's committed rows.
SCORER_LOCK_KEY = int.from_bytes(b"sd-score", "big", signed=True)
_LOCK = text("select pg_advisory_xact_lock(:k)")

# What the evidence path legitimately raises; each becomes an `unscorable` row. Anything else is
# a scorer bug: never laundered into a permanent row (see score_pending).
_EVIDENCE_ERRORS: tuple[type[Exception], ...] = (
    UnscorableHeal,
    EvidenceCorrupted,
    lxml.etree.LxmlError,
    UnicodeDecodeError,
    OSError,  # FileNotFoundError included
)
# Authored messages that carry only names, ids and caps -- never page text (bundle.py's
# EvidenceCorrupted messages name files, directories and byte caps; ours, days and node ids).
_MESSAGE_KEPT: tuple[type[Exception], ...] = (UnscorableHeal, EvidenceCorrupted)


class ScorerFailed(Exception):
    """Raised after the rest of the batch is committed: these heals hit an unexpected error and
    were left pending, with no audit row."""

    def __init__(self, heal_ids: list[int]) -> None:
        self.heal_ids = heal_ids
        super().__init__(
            f"{len(heal_ids)} bench heal(s) left pending by a scorer error: {heal_ids}"
        )


def _reason(exc: Exception) -> str:
    if isinstance(exc, _MESSAGE_KEPT):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _bundle(heal_id: int, evidence_uri: str | None) -> Path:
    """The heal's bundle directory, derived from its id under the store root (never trusted
    from the row): a stored uri naming any other directory is refused."""
    if evidence_uri is None:
        raise UnscorableHeal("no evidence was stored for this heal")
    bundle = heal_bundle_dir(heal_id)
    if Path(evidence_uri) != bundle:
        raise UnscorableHeal("evidence outside the store")
    return bundle


def _expected(day: dt.date, field: str) -> list[str]:
    try:
        return expected_xpaths(day, field)
    except KeyError:
        raise UnscorableHeal("the heal's field is not a bench field") from None


def _judged(
    *, heal_id: int, field: str, run_key: str, evidence_uri: str | None, node_id: str | None,
    accepted: bool, key: bytes,
) -> dict[str, object]:  # fmt: skip
    bundle = _bundle(heal_id, evidence_uri)
    if node_id is None:
        raise UnscorableHeal("the heal has no resolution node to score")
    manifest = read_heal_manifest(bundle)  # the bundle verified against its manifest
    after_html = verified_bytes(bundle, manifest.files, AFTER_HTML).decode("utf-8")
    day = seed_day(meta_seed(after_html), manifest.after.captured_at, key=key)
    resolved = truth_path(after_html, node_id)
    expected = _expected(day, field)
    # Any row's cell in the right column is the right column: row 3's Amount is a correct
    # heal, row 1's Reserve is not.
    correct = resolved in expected
    return {
        "day": day.isoformat(),
        "field": field,
        "resolved": resolved,
        "expected_first": expected[0] if expected else None,
        "judgement": judge(correct=correct, accepted=accepted).value,
        "run_key": run_key,
    }


def score_pending(session: Session, *, redaction_key: bytes | None = None) -> list[int]:
    """Score every settled bench heal with no 'scored' row yet (see _pending for what is held);
    returns the heal ids scored.

    One transaction, under the scorer's advisory lock, with each heal in its own savepoint. An
    evidence-path error (_EVIDENCE_ERRORS) is written as an `unscorable` row, not skipped:
    skipping would leave it pending forever and hide it, and a published count of unscorable
    heals is itself a finding about the bench. Any OTHER exception is a scorer bug: that heal's
    savepoint is rolled back (no row, it stays pending), its class name and id are logged
    (never the message), the rest of the batch is committed, and ScorerFailed is raised."""
    if redaction_key is None:
        from selector_drift.config import get_settings

        redaction_key = get_settings().redaction_key
    session.execute(_LOCK, {"k": SCORER_LOCK_KEY})  # first: the pending query runs after it
    scored: list[int] = []
    failed: list[int] = []
    for row in session.execute(_pending()).mappings().all():
        heal_id = int(row["id"])
        try:
            with session.begin_nested():
                detail: dict[str, object]
                try:
                    detail = _judged(
                        heal_id=heal_id, field=row["field"], run_key=row["run_key"],
                        evidence_uri=row["evidence_uri"], node_id=row["node_id"],
                        accepted=bool(row["accepted"]), key=redaction_key,
                    )  # fmt: skip
                except _EVIDENCE_ERRORS as exc:
                    detail = {
                        "field": row["field"],
                        "judgement": UNSCORABLE,
                        "reason": _reason(exc),
                        "run_key": row["run_key"],
                    }
                session.execute(
                    _RECORD,
                    {"actor": ACTOR, "action": ACTION, "subject": str(heal_id),
                     "detail": json.dumps(detail)},
                )  # fmt: skip
        except Exception as exc:  # a scorer bug: this heal only, and loudly at the end
            _log.error("bench heal %d not scored: %s", heal_id, type(exc).__name__)
            failed.append(heal_id)
            continue
        scored.append(heal_id)
    session.commit()
    if failed:
        raise ScorerFailed(failed)
    return scored


@flow(name="bench-scorer")
def score_pending_flow() -> int:
    # permitted: only-the-scorer-reads-the-product
    from selector_drift.db.session import Session as DbSession

    with DbSession() as s:
        return len(score_pending(s))
