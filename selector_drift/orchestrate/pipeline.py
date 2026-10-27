"""The product, in order. Every write here is reachable only through the gate's output.

    observe_spec                         (Task 29A: a spec edit is recorded even on a refusal)
    authorise(scope)                     | ScopeRefusal -> state=refused, no attempt, no browser
    attempt row, authorisation audit
    driver.open(target, credentials)     one browser, one context
      run_steps(navigate)
      snapshot -> capture(evidence)      BEFORE any resolution: the page the resolver saw
      resolve_cascade per field          one resolutions row each, win or lose (C4)
                                         | EscalateToHuman -> hold("escalated"), quarantined
      record_boxes                       while the live page is still open
    extract.read                         | RowMisaligned -> hold(...), quarantined
    history_for -> gate_run              every field judged, with the node it extracted
    record_heals                         heal_events rows (evidence_uri filled by settle)
    settle                               accept.py: redaction, bundles, then accept() or hold()
    evaluate (accepted runs only)        quality reads landed rows
    record_attempt, record_run, notify   every settled run is notified (Task 29A)

The cache is written in exactly one place, accept.accept(), and only with a
ValidatedExtraction -- never at heal time, never from a quarantined run (C2, ledger 504).
"""

from __future__ import annotations

import datetime as dt
import logging
import traceback
from collections.abc import Callable, Mapping, Sequence

from bok_core.findings import Finding

from selector_drift.browser import driver  # module attribute: tests patch driver.open
from selector_drift.browser.live_capture import LiveCapture
from selector_drift.browser.navigate import run_steps
from selector_drift.browser.page_source import LivePageSource
from selector_drift.config import get_settings
from selector_drift.credentials.provider import CredentialProvider
from selector_drift.db.models import HealEvent, Quarantine, Run
from selector_drift.db.session import Session
from selector_drift.extract.models import CandidateExtraction
from selector_drift.extract.reader import RowMisaligned, read
from selector_drift.heallog.capture import capture, record_boxes
from selector_drift.heallog.evidence import PiiMasker
from selector_drift.orchestrate.accept import RunState, hold, settle
from selector_drift.orchestrate.cache_writer import CacheWriter
from selector_drift.orchestrate.history import history_for
from selector_drift.orchestrate.interventions import observe_spec
from selector_drift.orchestrate.run_record import (
    next_attempt,
    record_attempt,
    record_authorisation,
    record_run,
    run_key,
)
from selector_drift.quality.alerts import notify, post_alert
from selector_drift.quality.contracts import evaluate
from selector_drift.resolve.cache import CachedSelector, SelectorCache
from selector_drift.resolve.cascade import resolve_cascade
from selector_drift.resolve.escalation import EscalateToHuman
from selector_drift.resolve.rungs.r2_axtree import AxTreeRung
from selector_drift.resolve.rungs.r3_anchors import AnchorRung
from selector_drift.resolve.rungs.r4_heuristics import HeuristicRung
from selector_drift.resolve.trace import CascadeTrace, ResolvedElement, Rung
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.gate import authorise
from selector_drift.spec.loader import load_scope, load_spec, scope_file, spec_file, target_of
from selector_drift.validate.assertions.independence import continuity_alert
from selector_drift.validate.checks import checks_for
from selector_drift.validate.gate import HealContext, ValidatedExtraction, gate_run
from selector_drift.validate.verdict import Confirmation, Outcome, Verdict

_log = logging.getLogger(__name__)
ESCALATED = "escalated"
MISALIGNED = "fields do not share a row"
CredentialsFor = Callable[[str], CredentialProvider]


def default_confirmers() -> tuple[Rung, ...]:
    """The concrete rungs the gate re-derives and confirms with. orchestrate/ is the only
    package that may construct them (judge-reaches-rungs-only-by-protocol)."""
    return (AxTreeRung(), AnchorRung(), HeuristicRung())


def run_for(key: str) -> Run:
    """The settled run, for notify() (Task 29A). Every settled run is passed to notify()."""
    with Session() as s:
        run = s.get(Run, key)
        if run is None:
            raise LookupError(f"no run {key}")
        return run


def _confirmer(verdicts: Sequence[Verdict], field: str) -> Confirmation | None:
    found = [
        v.confirmed_by
        for v in verdicts
        if v.field == field and v.outcome is Outcome.PASS and v.confirmed_by is not None
    ]
    dom = [c for c in found if c.rung is not None]
    best = dom or found
    return best[0] if best else None


def record_heals(
    run_key: str,
    traces: Mapping[str, CascadeTrace],
    elements: Mapping[str, ResolvedElement],
    verdicts: Sequence[Verdict],
    *,
    target: str,
    cache_before: Mapping[str, CachedSelector | None],
) -> list[HealEvent]:
    """One heal_events row per field whose trace healed, tied to its resolutions row. Written
    for accepted and quarantined runs alike: a refused heal is still a heal the log shows."""
    now = dt.datetime.now(dt.UTC)
    heals: list[HealEvent] = []
    with Session() as s:
        for name, trace in traces.items():
            if not trace.healed or trace.winner is None or name not in elements:
                continue
            conf, before, el = _confirmer(verdicts, name), cache_before.get(name), elements[name]
            heals.append(
                HealEvent(
                    run_key=run_key,
                    resolution_id=trace.resolution_id,
                    target=target,
                    field=name,
                    broke_at=now,
                    winning_rung=trace.winner,
                    confirmer_rung=conf.rung if conf else None,
                    confirmer_family=conf.family.value if conf else None,
                    selector_before=before.selector if before else None,
                    selector_after=el.selector,
                    anchor_before=before.anchor.model_dump(mode="json") if before else None,
                    anchor_after=el.anchor.model_dump(mode="json"),
                    trace=trace.as_json(),
                    verdicts=[v.model_dump(mode="json") for v in verdicts if v.field == name],
                    evidence_uri=None,
                    replayed_ok=None,
                    promoted_by=None,
                    promoted_at=None,
                )
            )
        s.add_all(heals)
        s.commit()
        for h in heals:
            s.refresh(h)
            s.expunge(h)
    return heals


def _alignment_verdict(exc: RowMisaligned) -> Verdict:
    return Verdict(
        check="RowAlignment",
        outcome=Outcome.FAIL,
        threshold="every field resolves into the natural key's row list",
        observed="fields in different row lists",
        reason=f"fields from different row lists would be zipped into wrong rows: {exc}",
    )


def _escalation_verdict(field: str, exc: EscalateToHuman) -> Verdict:
    """Design 5.3 / 9.3: no rung 1-4 resolved. A refusal the human reads, never a guess."""
    return Verdict(
        check="Escalated",
        outcome=Outcome.FAIL,
        threshold="a rung 1-4 resolves the field",
        observed="no rung resolved",
        reason=exc.reason,
        field=field,
    )


def _quarantine_reason(key: str) -> str | None:
    with Session() as s:
        q = s.get(Quarantine, key)
        return q.reason if q is not None else None


def _evaluate(key: str, target: str) -> tuple[list[Finding], str | None]:
    """Quality over the landed rows (Task 29). Any failure is mapped, never swallowed (ledger
    588; fix round 1): the rows are already landed and accepted, so the run keeps its state --
    marking it failed would drop an accepted run from history -- the reason is recorded on it,
    and a human is told."""
    try:
        return evaluate(key), None
    except Exception as exc:  # nothing after the lake write may fail the run
        # The TYPE only: the message of a quality/DuckDB error can embed page values (PII, client
        # data), and this note is persisted on the Run and posted in an alert. This differs from
        # flows._record_failure on purpose: there a FAILED run keeps the raw str(exc) in
        # runs.error inside the client boundary and only the alert is sanitised, because the two
        # are different strings. Here one string is both, so it can only be the type; an operator
        # who needs the cause reads the frames in the log.
        note = f"quality not evaluated: {type(exc).__name__}"
        # Fix round 2: the note and the alert say THAT it failed; the log says WHERE. Frames and
        # the exception type only -- the message may carry page text, so it is never logged
        # (logger.exception would print it).
        _log.error(
            "quality evaluation failed for %s: %s\n%s",
            key,
            type(exc).__name__,
            "".join(traceback.format_tb(exc.__traceback__)),
        )
        post_alert("finding", target, key, note)
        return [], note


async def run_once(
    spec_path: str,
    *,
    window: dt.date,
    credentials_for: CredentialsFor,
    confirmers: Sequence[Rung],
) -> str:
    settings = get_settings()
    stem = target_of(spec_path)
    spec = load_spec(spec_file(spec_path))  # Task 5's resolver: no double prefix
    key = run_key(stem, spec.version, window.isoformat())
    observe_spec(stem, spec)  # Task 29A (M1): even on a run that is then refused
    try:
        record = load_scope(scope_file(spec.scope_ref))
        if record.target != stem:
            raise ScopeRefusal(
                f"spec {spec_path} is for target {stem!r} but its scope record "
                f"names {record.target!r}"
            )
        target = authorise(record, today=window)  # the UTC date of the slot (M20)
    except ScopeRefusal as exc:  # no attempt row, no browser (Task 6, 34B)
        record_run(
            key, target=stem, spec_version=spec.version, window=window, state="refused",
            error=exc.reason,
        )  # fmt: skip
        post_alert("refused", stem, key, exc.reason)
        notify([], run_for(key))
        return key
    attempt = next_attempt(key)  # M13: appends; never Prefect's run_count
    record_attempt(key, attempt, "started", None, target.record_sha256)
    record_authorisation(key, target.target, target.record_sha256, record=record)  # Task 2

    def settled(state: RunState, rows: int, error: str | None, findings: list[Finding]) -> str:
        record_attempt(key, attempt, state, error, target.record_sha256)
        record_run(
            key, target=target.target, spec_version=spec.version, window=window, state=state,
            rows_written=rows, error=error,
        )  # fmt: skip
        notify(findings, run_for(key))  # every settled run (Task 29A, M2)
        return key

    # Read before resolution: a heal's replay must run against the entry rung 1 declined. A
    # cache read error propagates and fails the attempt (fail-closed, ledger 395).
    reader = SelectorCache()
    cache_before = {t.name: reader.get(target.target, t.name, spec.version) for t in spec.targets}
    creds = credentials_for(target.target).get(target)  # CredentialsUnavailable fails the run
    elements: dict[str, ResolvedElement] = {}
    traces: dict[str, CascadeTrace] = {}
    contexts: dict[str, HealContext] = {}
    escalated: list[Verdict] = []
    async with driver.open(target, creds) as session:
        async with session.page.submitting_forms():  # logging in is a POST; nothing after is
            await run_steps(session.page, spec.navigate, creds)
        page = await LivePageSource.snapshot(session.page)
        live = LiveCapture.of(session.page, page.dom())
        evidence = await capture(
            live, run_key=key, masker=PiiMasker(settings.redaction_key, target.sensitivity)
        )
        for t in spec.targets:
            try:
                el, trace = resolve_cascade(page, t, reader, run_key=key)  # one row (C4)
            except EscalateToHuman as exc:  # quarantine, never fail the flow (design 9.3)
                if exc.trace is not None:
                    traces[t.name] = exc.trace
                escalated.append(_escalation_verdict(t.name, exc))
                continue
            elements[t.name], traces[t.name] = el, trace
            contexts[t.name] = HealContext(
                healed=trace.healed,
                winning_rung=trace.winner or 5,
                page=page,
                target=t,
                cache=reader,
                node_id=el.node_id,  # ledger 551: the node this run extracts
            )
        evidence = await record_boxes(
            evidence, live, {name: el.node_id for name, el in elements.items()}
        )

    empty = CandidateExtraction(rows=[], trace=traces, spec_version=spec.version, run_key=key)
    if escalated:
        # A retry would re-run the cascade and write a second resolutions row and a second
        # corpus case for the same field, so this settles the run instead.
        hold(key, ESCALATED, escalated, empty)
        return settled("quarantined", 0, ESCALATED, [])
    try:
        candidate = read(
            evidence.dom.value,
            elements,
            spec,
            run_key=key,
            snapshot_id=evidence.sha256,
            traces=traces,
            observed_at=evidence.captured_at,
        )
    except RowMisaligned as exc:  # a LookupError (a node gone) is not caught: the run fails
        hold(key, MISALIGNED, [_alignment_verdict(exc)], empty)
        return settled("quarantined", 0, MISALIGNED, [])

    with Session() as db:
        history = history_for(db, target.target, spec, before=window)
    checks = {
        t.name: checks_for(
            spec,
            t,
            confirmers,
            warmup_runs=target.warmup_runs,
            first_heal_policy=target.first_heal_policy,
        )
        for t in spec.targets
    }
    result = gate_run(candidate, history, contexts, checks)
    heals = record_heals(
        key, traces, elements, result.verdicts, target=target.target, cache_before=cache_before
    )
    for v in result.verdicts:  # ledger #32: tolerated, but a human hears
        if continuity_alert(v):
            post_alert(
                "finding",
                target.target,
                key,
                f"{v.field}: row-key continuity {v.observed} below {v.threshold} on a cache hit "
                "-- tolerated as churn, reported for review",
            )
    state = settle(
        target=target,
        spec=spec,
        candidate=candidate,
        result=result,
        evidence=evidence,
        elements=elements,
        heals=heals,
        cache=CacheWriter(target=target.target, spec_version=spec.version),
        cache_before=cache_before,
        redaction_key=settings.redaction_key,
        attempt=attempt,
    )
    if state == "quarantined" or not isinstance(result, ValidatedExtraction):
        # settle() accepts nothing but a ValidatedExtraction; the isinstance is for the reader
        # and the type checker, never an `assert` that `python -O` would strip.
        return settled("quarantined", 0, _quarantine_reason(key), [])
    findings, note = _evaluate(key, target.target)  # after the lake write: it reads landed rows
    return settled(state, len(result.rows), note, findings)
