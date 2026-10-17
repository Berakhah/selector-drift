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
from enum import StrEnum
from pathlib import Path

import lxml.etree
import lxml.html
from bok_core.boundary.redact import redact
from bok_core.boundary.sensitivity import Labelled, Sensitivity
from prefect import flow
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from selector_drift.heallog.bundle import (
    AFTER_HTML,
    EvidenceCorrupted,
    read_heal_manifest,
    verified_bytes,
)
from testbench.answers.key import expected_xpaths
from testbench.portal.hook import SEED_META
from testbench.portal.hook import TARGET as TARGET
from testbench.schedule import scheduled_names, seed_for

ACTOR = "testbench.score"
ACTION = "scored"
UNSCORABLE = "unscorable"
# The label the bench's evidence is stored under: its scope record's basis is self-owned, which
# scope.record (R4) maps to SYNTHETIC.
BENCH_LABEL = Sensitivity.SYNTHETIC
# What the product counts as an accepted run (orchestrate.history.ACCEPTED_STATES; a unit test
# pins the two equal -- importing orchestrate here would drag selector_drift.db in indirectly).
ACCEPTED_STATES = ("completed", "healed")


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
    return lxml.html.document_fromstring(html)


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


# Accepted means what the product means (orchestrate.history): a quarantined run is accepted
# only once promoted; a run with no quarantine row only if it settled completed/healed -- a
# failed run wrote nothing. A heal with no resolutions row is selected too (left join), so it is
# recorded unscorable rather than left pending forever. SKIP LOCKED: two concurrent scorers
# never judge one heal twice.
_PENDING = text("""
    select h.id, h.run_key, h.field, h.evidence_uri, r.node_id,
           case when q.run_key is not null then coalesce(q.resolved_as = 'promoted', false)
                else coalesce(run.state in :accepted_states, false)
           end as accepted
      from heal_events h
      join runs run on run.run_key = h.run_key
      left join resolutions r on r.id = h.resolution_id
      left join quarantine q on q.run_key = h.run_key
     where h.target = :target
       and not exists (select 1 from audit_log a
                        where a.action = :action and a.actor = :actor
                          and a.subject = h.id::text)
     order by h.id
       for update of h skip locked
""").bindparams(bindparam("accepted_states", value=list(ACCEPTED_STATES), expanding=True))

_RECORD = text(
    "insert into audit_log (at, actor, action, subject, detail) "
    "values (now(), :actor, :action, :subject, cast(:detail as jsonb))"
)


def _judged(
    *, field: str, run_key: str, evidence_uri: str | None, node_id: str | None,
    accepted: bool, key: bytes,
) -> dict[str, object]:  # fmt: skip
    if evidence_uri is None:
        raise UnscorableHeal("no evidence was stored for this heal")
    if node_id is None:
        raise UnscorableHeal("the heal has no resolution node to score")
    bundle = Path(evidence_uri)
    manifest = read_heal_manifest(bundle)  # the bundle verified against its manifest
    after_html = verified_bytes(bundle, manifest.files, AFTER_HTML).decode("utf-8")
    day = seed_day(meta_seed(after_html), manifest.after.captured_at, key=key)
    resolved = truth_path(after_html, node_id)
    expected = expected_xpaths(day, field)
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
    """Score every bench heal with no 'scored' row yet; returns the heal ids scored. An
    unscorable heal is written, not skipped: skipping would leave it pending forever and hide
    it, and a published count of unscorable heals is itself a finding about the bench."""
    if redaction_key is None:
        from selector_drift.config import get_settings

        redaction_key = get_settings().redaction_key
    params = {"target": TARGET, "action": ACTION, "actor": ACTOR}
    scored: list[int] = []
    for row in session.execute(_PENDING, params).mappings().all():
        detail: dict[str, object]
        try:
            detail = _judged(
                field=row["field"], run_key=row["run_key"], evidence_uri=row["evidence_uri"],
                node_id=row["node_id"], accepted=bool(row["accepted"]), key=redaction_key,
            )  # fmt: skip
        except (UnscorableHeal, EvidenceCorrupted, LookupError, ValueError,
                lxml.etree.LxmlError) as exc:  # fmt: skip
            detail = {
                "field": row["field"],
                "judgement": UNSCORABLE,
                "reason": f"{type(exc).__name__}: {exc}",
                "run_key": row["run_key"],
            }
        session.execute(
            _RECORD,
            {"actor": ACTOR, "action": ACTION, "subject": str(row["id"]),
             "detail": json.dumps(detail)},
        )  # fmt: skip
        scored.append(int(row["id"]))
    session.commit()
    return scored


@flow(name="bench-scorer")
def score_pending_flow() -> int:
    from selector_drift.db.session import Session  # permitted: only-the-scorer-reads-the-product

    with Session() as s:
        return len(score_pending(s))
