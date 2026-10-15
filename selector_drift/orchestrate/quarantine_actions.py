"""The two decisions a human can take on a quarantined run, built where the product may write,
and handed to the API -- which may not (Task 34, api-is-read-side).

Promotion re-runs the gate with human_confirmed=True (Task 25): it supplies the missing
confirmation and never overrides a FAIL. Then, in this order -- the record follows the data
(ledger 2026-09-26):

    quarantine.promote_and_land()   all under ONE quarantine row lock:
        re-gate                     StillInvalid -> nothing written, quarantine open
        accept_promoted()           accept(): land.write -> cache.put -> last-accepted   (C2)
        mark                        resolved_as="promoted" + the audit row
    heal_events.promoted_by         the heals this human confirmed
    record_intervention()           "manual_promote" (Task 29A)

A lake failure part-way rolls back and leaves the quarantine open and retryable, never
"promoted" with nothing landed. A discard racing a promotion waits on the row and is then
refused (AlreadyResolved): the audit trail cannot read "discarded" over rows that landed. A
discard writes nothing but the audited decision.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
from bok_core.boundary.egress import Plane
from bok_core.provenance.claim import Claim
from lxml.html import HtmlElement
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import HealEvent, Quarantine, Resolution
from selector_drift.db.session import Session
from selector_drift.extract.models import CandidateExtraction, Row
from selector_drift.heallog.bundle import AFTER_HTML, read_heal_manifest, verified_bytes
from selector_drift.heallog.evidence import promote_heal_bundle_to_last_accepted
from selector_drift.land.layout import PROVENANCE_COL
from selector_drift.orchestrate.accept import accept
from selector_drift.orchestrate.cache_writer import CacheWriter
from selector_drift.orchestrate.history import history_for
from selector_drift.orchestrate.interventions import record_intervention
from selector_drift.orchestrate.pipeline import default_confirmers
from selector_drift.orchestrate.run_record import parse_run_key
from selector_drift.resolve.cache import SelectorCache
from selector_drift.resolve.page import NODE_ATTR, StaticPageSource
from selector_drift.resolve.trace import AnchorEvidence, CascadeTrace, ResolvedElement
from selector_drift.spec.loader import load_scope, load_spec, scope_file, spec_file
from selector_drift.validate import quarantine
from selector_drift.validate.checks import checks_for
from selector_drift.validate.gate import HealContext, ValidatedExtraction
from selector_drift.validate.quarantine import GateInputs


@dataclass(frozen=True)
class QuarantineActions:
    promote: Callable[[str, str, str], None]  # (run_key, actor, reason)
    discard: Callable[[str, str, str], None]


def _latest_resolutions(session: OrmSession, run_key: str) -> dict[str, Resolution]:
    """Each field's resolution in the run's LAST attempt (a retry writes a second row)."""
    out: dict[str, Resolution] = {}
    for r in session.scalars(
        select(Resolution).where(Resolution.run_key == run_key).order_by(Resolution.id)
    ):
        out[r.field] = r
    return out


def _heals_of(session: OrmSession, run_key: str) -> list[HealEvent]:
    """The heals of the run's last attempt: those tied to its latest resolutions."""
    latest = {r.id for r in _latest_resolutions(session, run_key).values()}
    return [
        h
        for h in session.scalars(
            select(HealEvent).where(HealEvent.run_key == run_key).order_by(HealEvent.id)
        )
        if h.resolution_id in latest
    ]


def element_from_heal(session: OrmSession, heal: HealEvent) -> ResolvedElement:
    resolution = session.get(Resolution, heal.resolution_id) if heal.resolution_id else None
    if resolution is None or resolution.node_id is None:
        raise LookupError(f"heal {heal.id} has no resolution to rebuild its element from")
    return ResolvedElement(
        selector=heal.selector_after,
        node_id=resolution.node_id,
        anchor=AnchorEvidence.model_validate(heal.anchor_after),
        rung_id=heal.winning_rung,
    )


def _page_of(heals: Sequence[HealEvent]) -> StaticPageSource | None:
    """The DOM the run saw, from its newest heal bundle, verified against its manifest. None when
    nothing was stored (a redaction failure) -- the gate then has no DOM evidence, and says so."""
    latest = next((h for h in reversed(heals) if h.evidence_uri is not None), None)
    if latest is None or latest.evidence_uri is None:
        return None
    bundle = Path(latest.evidence_uri)
    dom = verified_bytes(bundle, read_heal_manifest(bundle).files, AFTER_HTML).decode("utf-8")
    return StaticPageSource(dom)


def _stored_node(page: StaticPageSource | None, live_id: str | None) -> str | None:
    """A live node id (`data-sd-node` token) in the stored page's node-id space (its lxml path),
    so IndependentRungAgreement compares like with like. A token the stored page does not carry
    exactly once is passed through unchanged: it cannot match a path, so a heal whose node
    cannot be located in its own evidence FAILs rather than escaping the comparison."""
    if page is None or live_id is None:
        return live_id
    hits = page.root().xpath(f"//*[@{NODE_ATTR}=$n]", n=live_id)
    found = [h for h in hits if isinstance(h, HtmlElement)] if isinstance(hits, list) else []
    return page.node_id(found[0]) if len(found) == 1 else live_id


def _candidate(
    run_key: str, data_uri: str, spec_version: int, winners: Mapping[str, int]
) -> CandidateExtraction:
    """The held rows as the Claims the gate judged: values plus the provenance hold() stored."""
    rows: list[Row] = []
    for record in pq.read_table(data_uri).to_pylist():
        prov = json.loads(record.pop(PROVENANCE_COL, None) or "{}")
        rows.append(
            {
                name: Claim.model_validate(prov[name] | {"value": value})
                for name, value in record.items()
                if name in prov
            }
        )
    trace = {name: CascadeTrace(attempts=(), winner=rung) for name, rung in winners.items()}
    return CandidateExtraction(rows=rows, trace=trace, spec_version=spec_version, run_key=run_key)


def rebuild_gate_inputs(run_key: str) -> GateInputs:
    """Task 25's `Rebuild`: the gate's inputs for a quarantined run, rebuilt from what was
    stored -- the held parquet, the history before its window, the heal bundle's `after` page,
    its last attempt's resolutions and heals, and checks_for with the target's own warm-up and
    first-heal policy. Never a live page."""
    target, version, window = parse_run_key(run_key)
    spec = load_spec(spec_file(f"{target}.yaml"))
    if spec.version != version:
        raise ValueError(
            f"{run_key} was judged under spec v{version}; the spec is now v{spec.version} -- "
            "discard it and let the next run judge afresh"
        )
    record = load_scope(scope_file(spec.scope_ref))  # ScopeRefusal: no promotion either
    with Session() as s:
        q = s.get(Quarantine, run_key)
        if q is None or not q.data_uri:
            raise LookupError(f"{run_key} holds no quarantined data to promote")
        data_uri = q.data_uri
        resolutions = _latest_resolutions(s, run_key)
        heals = _heals_of(s, run_key)
        history = history_for(s, target, spec, before=window)
    unresolved = sorted(
        t.name
        for t in spec.targets
        if t.name not in resolutions or resolutions[t.name].winning_rung is None
    )
    candidate_winners = {
        name: r.winning_rung for name, r in resolutions.items() if r.winning_rung is not None
    }
    candidate = _candidate(run_key, data_uri, spec.version, candidate_winners)
    if unresolved or not candidate.rows:
        raise ValueError(
            f"{run_key} held no rows (escalated or misaligned): nothing to promote -- discard it"
        )
    page, cache = _page_of(heals), SelectorCache()
    contexts = {
        t.name: HealContext(
            healed=resolutions[t.name].winning_rung != 1,
            winning_rung=candidate_winners[t.name],
            page=page,
            target=t,
            cache=cache,
            node_id=_stored_node(page, resolutions[t.name].node_id),
        )
        for t in spec.targets
    }
    checks = {
        t.name: checks_for(
            spec,
            t,
            default_confirmers(),
            warmup_runs=record.warmup_runs,
            first_heal_policy=record.first_heal_policy,
        )
        for t in spec.targets
    }
    return GateInputs(candidate=candidate, history=history, contexts=contexts, checks=checks)


def accept_promoted(run_key: str, validated: ValidatedExtraction, *, cache: CacheWriter) -> Path:
    """A promoted run reaches the same single call site as a gate-accepted one, accept(). Its
    healed fields' elements come from its heal rows; a rung-1 field's entry is the one it hit.
    Its accepted page is its newest heal bundle's 'after' half; a quarantine with no stored
    evidence (a redaction failure) leaves last-accepted where it was."""
    if validated.run_key != run_key:
        raise ValueError(f"a validated extraction of another run ({validated.run_key})")
    target, _version, _window = parse_run_key(run_key)
    with Session() as s:
        heals = _heals_of(s, run_key)
        elements = {h.field: element_from_heal(s, h) for h in heals}
    latest = next((h for h in reversed(heals) if h.evidence_uri is not None), None)

    def commit() -> None:
        if latest is not None:
            promote_heal_bundle_to_last_accepted(latest.id, target, Plane.CLIENT)

    return accept(validated, elements, cache=cache, commit_last_accepted=commit)


def _mark_heals_promoted(run_key: str, actor: str) -> None:
    now = dt.datetime.now(dt.UTC)
    with Session() as s:
        for h in _heals_of(s, run_key):
            h.promoted_by, h.promoted_at = actor, now
        s.commit()


def build_quarantine_actions() -> QuarantineActions:
    def _promote(run_key: str, actor: str, reason: str) -> None:
        def land(validated: ValidatedExtraction) -> None:
            if validated.run_key != run_key:  # Task 25 minor: the mark trusts any verdicts
                raise ValueError(
                    f"promotion of {run_key} re-gated another run ({validated.run_key})"
                )
            accept_promoted(run_key, validated, cache=CacheWriter.for_run_key(run_key))

        # One row lock from the re-gate to the mark (fix round 1): a racing discard waits, then
        # is refused, so the audit trail never disagrees with what landed.
        quarantine.promote_and_land(
            run_key, actor=actor, reason=reason, rebuild=rebuild_gate_inputs, land=land
        )
        _mark_heals_promoted(run_key, actor)
        target, _version, _window = parse_run_key(run_key)
        record_intervention(
            "manual_promote",
            actor,
            target,
            "*",
            {"run_key": run_key, "state": "quarantined"},
            {"run_key": run_key, "state": "promoted"},
            reason,
        )

    def _discard(run_key: str, actor: str, reason: str) -> None:
        # Writes nothing but the audited decision (quarantine.discard): no data, no selector
        # changes, so not an intervention (declared in docs/methodology.md).
        quarantine.discard(run_key, actor=actor, reason=reason)

    return QuarantineActions(promote=_promote, discard=_discard)
