"""Data-quality contracts over landed rows (design 9.4). Every finding is a Finding on the shared
severity ladder; every one states its threshold and why; every one carries a DERIVED Claim whose
parents are the DomSource claims it was computed from (INTERFACE-CONTRACT 4; H8/3, M7, M8).
Findings are persisted here and nowhere else.

Row values are client data (brief rule 6): no property, title or error raised here carries one.
They live only in the claim chain, which stays in our database."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from bok_core.findings import Finding, Severity
from bok_core.provenance.claim import Claim, Method
from bok_core.provenance.source import DomSource
from sqlalchemy import delete

from selector_drift.db.models import FindingRow
from selector_drift.db.session import Session
from selector_drift.land.catalog import query, run_columns
from selector_drift.land.layout import PROVENANCE_COL, is_data_column, split_run_key
from selector_drift.orchestrate.history import history_for
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.spec.loader import load_scope, load_spec, scope_file, spec_file
from selector_drift.spec.schema import TaskSpec
from selector_drift.validate.assertions.distribution import (
    ALPHA,
    MIN_VALUES,
    REFERENCE_RUNS,
    DistributionalAssertion,
)
from selector_drift.validate.history import History

ROW_COUNT_RATIO: Final = 0.5
ROW_COUNT_MIN_WINDOWS: Final = 3
ROW_COUNT_REASON: Final = (
    "fewer than half the trailing median means rows were lost -- a pagination change, a "
    "virtualised table or a row scope that shrank -- not a quiet day; the median, not the mean, "
    "so one month-end spike does not move the bar; no judgement before three windows of history"
)
NULL_RATE_FLOOR: Final = 0.05
NULL_RATE_REASON: Final = (
    "a field empty in more than 5% of rows, or in twice its trailing rate, usually means the "
    "resolver landed on a cell that is sometimes blank; the floor stops a field that is never "
    "empty from alerting on one blank"
)
SCHEMA_REASON: Final = (
    "the spec's expected_schema is the contract the client signed off; a column that appears, "
    "disappears or changes type is how a UI change the resolver survived still changes what lands"
)
# The same parameters as the gate's DistributionalAssertion (Task 23), passed to it explicitly;
# tests pin them equal to its defaults, so a heal-log line and a quality row cannot disagree.
PSI_THRESHOLD: Final = 0.20
PERSISTENCE: Final = 2
REFERENCE_WINDOWS: Final = REFERENCE_RUNS
PSI_REASON: Final = (
    f"PSI over {REFERENCE_RUNS} trailing accepted windows (10 quantile bins fixed from the "
    "reference, plus underflow, overflow, blank and text bins; category proportions for text), "
    "Jeffreys-smoothed (half a count per populated bin); a breach needs PSI over the larger of "
    f"{PSI_THRESHOLD:.2f} -- the conventional 'significant shift' line -- and the chi-square "
    f"noise floor at alpha {ALPHA:g}, so sampling noise in a small run is not a shift. No "
    f"judgement during the scope record's warm-up or on fewer than {MIN_VALUES} values. One "
    f"breach is MEDIUM; {PERSISTENCE} consecutive breaches the client did not mark expected are "
    "HIGH, so month-end does not page anyone (B7)"
)
MAX_PARENTS: Final = 50


@dataclass(frozen=True)
class _Run:
    target: str
    window: str
    run_key: str
    spec: TaskSpec
    warmup_runs: int
    columns: list[str]  # this run's own data columns (run_columns), not the target's union
    rows: list[dict[str, Any]]
    history: list[dict[str, Any]]  # every row of the prior windows, oldest window first


# ------------------------------------------------------------------ claims


def _is_null(v: object) -> bool:
    return v is None or str(v).strip() == ""


def _claims(rows: Sequence[dict[str, Any]], field: str) -> list[Claim[Any]]:
    out: list[Claim[Any]] = []
    for r in rows:
        prov = json.loads(r.get(PROVENANCE_COL) or "{}")
        if field in prov:
            out.append(Claim.model_validate(prov[field] | {"value": r.get(field)}))
    return out


def _derived(value: object, parents: list[Claim[Any]], spec: TaskSpec) -> Claim[Any]:
    caveat: str | None = None
    kept = parents[:MAX_PARENTS]
    if len(parents) > MAX_PARENTS:
        caveat = f"parents truncated to the first {MAX_PARENTS} of {len(parents)} cells"
    if kept:
        source = kept[0].source
    else:
        source = DomSource(snapshot_id="unavailable", selector=spec.row_scope, rung=0)
        caveat = "no rows were landed, so there is no cell for the chain to point at"
    return Claim(
        value=value,
        source=source,
        method=Method.DERIVED,
        confidence=None,
        observed_at=dt.datetime.now(dt.UTC),
        parents=kept,
        caveat=caveat,
    )


def _finding(
    run: _Run,
    *,
    rule: str,
    field: str | None,
    severity: Severity,
    title: str,
    observed: str,
    threshold: str,
    reason: str,
    parents: list[Claim[Any]],
    value: object,
    extra: dict[str, Any] | None = None,
) -> Finding:
    where = field or "*"
    return Finding(
        id=f"quality/{rule}/{field or 'run'}",
        severity=severity,
        title=title,
        claim=_derived(value, parents, run.spec),
        taxonomy_refs=[f"selector-drift:quality:{rule}"],
        logical_location=f"{run.target}/{where}",
        partial_fingerprints={
            "selectorDrift/v1": hashlib.sha256(f"{run.target}|{rule}|{where}".encode()).hexdigest()[
                :32
            ]
        },
        properties={
            "rule": rule,
            "field": field,
            "observed": observed,
            "threshold": threshold,
            "threshold_reason": reason,
            "run_key": run.run_key,
        }
        | (extra or {}),
    )


def _windows(rows: Sequence[dict[str, Any]], last: int) -> dict[str, list[dict[str, Any]]]:
    """Rows grouped by window, oldest first, the most recent `last` windows only."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["window"]), []).append(r)
    keep = sorted(out)[-last:] if last > 0 else []
    return {w: out[w] for w in keep}


# ------------------------------------------------------------------ the four rules


def _row_count(run: _Run) -> list[Finding]:
    counts = [len(w) for w in _windows(run.history, REFERENCE_WINDOWS).values()]
    if len(counts) < ROW_COUNT_MIN_WINDOWS:
        return []
    median = float(statistics.median(counts))
    bar = ROW_COUNT_RATIO * median
    if len(run.rows) >= bar:
        return []
    return [
        _finding(
            run,
            rule="row_count",
            field=None,
            severity=Severity.CRITICAL if not run.rows else Severity.HIGH,
            title=f"Row count {len(run.rows)} is below half the trailing median ({median:g})",
            observed=str(len(run.rows)),
            threshold=f">= {bar:g}",
            reason=ROW_COUNT_REASON,
            parents=_claims(run.rows, run.spec.natural_key[0]),
            value=len(run.rows),
            extra={"median": median, "windows": len(counts)},
        )
    ]


def _null_rate(run: _Run) -> list[Finding]:
    if not run.rows:
        return []
    out: list[Finding] = []
    windows = list(_windows(run.history, REFERENCE_WINDOWS).values())
    columns: dict[str, list[str]] = {}

    def existed(window: list[dict[str, Any]], name: str) -> bool:
        """M2 (Task 29 review, carried into 26A): a window whose files predate a newly added
        field reads it as NULL through the union view. Counting that window as 100% null
        raised the bar to ~2.0 and silenced the rule for 30 windows -- a fail-open."""
        keys = {str(r["run_key"]) for r in window}
        for key in keys:
            if key not in columns:
                columns[key] = run_columns(key)
        return any(name in columns[k] for k in keys)

    for t in run.spec.targets:
        if t.name in run.spec.expected_schema and t.name not in run.columns:
            continue  # an absent column is schema drift; one fact, one finding
        rate = sum(_is_null(r.get(t.name)) for r in run.rows) / len(run.rows)
        past = [
            sum(_is_null(r.get(t.name)) for r in w) / len(w) for w in windows if existed(w, t.name)
        ]
        bar = max(NULL_RATE_FLOOR, 2 * (statistics.fmean(past) if past else 0.0))
        if rate <= bar:
            continue
        out.append(
            _finding(
                run,
                rule="null_rate",
                field=t.name,
                severity=Severity.HIGH if rate >= 0.5 else Severity.MEDIUM,
                title=f"{t.name} is empty in {rate:.0%} of rows",
                observed=f"{rate:.3f}",
                threshold=f"<= {bar:.3f}",
                reason=NULL_RATE_REASON,
                parents=_claims(run.rows, t.name),
                value=round(rate, 6),
            )
        )
    return out


def _conforms(kind: str, value: str) -> bool:
    try:
        if kind == "decimal":
            return Decimal(value.replace(",", "")).is_finite()
        if kind == "integer":
            int(value.replace(",", ""))
        elif kind == "date":
            dt.date.fromisoformat(value)
    except (InvalidOperation, ValueError):
        return False
    return True


def _schema_drift(run: _Run) -> list[Finding]:
    if not run.rows:
        return []  # row_count already speaks for it
    columns = run.columns
    expected = {name: str(kind) for name, kind in run.spec.expected_schema.items()}
    missing = sorted(set(expected) - set(columns))
    unexpected = sorted(set(columns) - set(expected))
    mistyped = {
        name: n
        for name, kind in sorted(expected.items())
        if name in columns
        if (
            n := sum(
                1
                for r in run.rows
                if not _is_null(r.get(name)) and not _conforms(kind, str(r[name]))
            )
        )
        > 0
    }
    if not (missing or unexpected or mistyped):
        return []
    parts = [
        f"missing {missing}" if missing else "",
        f"unexpected {unexpected}" if unexpected else "",
        f"mistyped {mistyped}" if mistyped else "",
    ]
    return [
        _finding(
            run,
            rule="schema_drift",
            field=None,
            severity=Severity.HIGH,
            title="Landed columns differ from the spec's expected schema",
            observed="; ".join(p for p in parts if p),
            threshold=f"columns and types exactly as expected_schema (spec v{run.spec.version})",
            reason=SCHEMA_REASON,
            parents=[c for name in columns for c in _claims(run.rows[:1], name)],
            value={"missing": missing, "unexpected": unexpected, "mistyped": mistyped},
            extra={"missing": missing, "unexpected": unexpected, "mistyped": mistyped},
        )
    ]


def _history(run: _Run) -> History:
    """The accepted runs before this one, oldest first -- what the gate's DistributionalAssertion
    reads -- from orchestrate.history.history_for, the ONE builder the gate uses too (ledger
    583): accepted or promoted runs, under the current spec version only (ledger 471), with
    their persisted breaches and the client's marks. Enough runs to leave the scope record's
    warm-up, which may be longer than the reference window."""
    with Session() as s:
        found = history_for(
            s,
            run.target,
            run.spec,
            before=dt.date.fromisoformat(run.window),
            limit=max(REFERENCE_WINDOWS, run.warmup_runs),
        )
    return found if found is not None else History()


def _distribution_shift(run: _Run) -> list[Finding]:
    fields = [t for t in run.spec.targets if t.distributional]
    if not fields:
        return []
    gate = DistributionalAssertion(
        threshold=PSI_THRESHOLD, warmup_runs=run.warmup_runs, persistence=PERSISTENCE
    )
    history = _history(run)
    out: list[Finding] = []
    for t in fields:
        current = [str(r[t.name]) for r in run.rows if r.get(t.name) is not None]
        a = gate.assess_values(history, current, t.name)
        if isinstance(a, str) or not a.breach:
            continue  # no basis (warm-up, too few values) or no shift: nothing to report
        # A finding on EVERY breach (MEDIUM); HIGH -- what Task 29A pages -- only when it
        # persisted and the client did not mark the last one expected (B7).
        out.append(
            _finding(
                run,
                rule="distribution_shift",
                field=t.name,
                severity=Severity.HIGH if a.alert else Severity.MEDIUM,
                title=f"{t.name} distribution shifted (PSI {a.psi:.2f})",
                observed=f"{a.psi:.4f}",
                threshold=f"PSI {PSI_THRESHOLD:.2f}",
                reason=PSI_REASON,
                parents=_claims(run.rows, t.name),
                value=round(a.psi, 6),
                extra={
                    "critical": f"{a.critical:.4f}",
                    "alpha": gate.alpha,
                    "reference_n": a.reference_n,
                    "current_n": a.current_n,
                    "categorical": a.categorical,
                    "consecutive": 1 + a.prior_breaches,
                    "persistence": PERSISTENCE,
                    "suppressed": a.suppressed,
                },
            )
        )
    return out


# ------------------------------------------------------------------ entry point


def _persist(run_key: str, findings: list[Finding]) -> None:
    now = dt.datetime.now(dt.UTC)
    with Session() as s:
        s.execute(delete(FindingRow).where(FindingRow.run_key == run_key))
        s.add_all(
            FindingRow(
                id=f"{f.id}@{run_key}",
                run_key=run_key,
                severity=f.severity.value,
                rule=str(f.properties["rule"]),
                payload=f.model_dump(mode="json"),
                at=now,
            )
            for f in findings
        )
        s.commit()


def evaluate(run_key: str) -> list[Finding]:
    """Every rule over one landed run; persists the findings, replacing that run's earlier rows,
    so re-evaluating a replayed run is idempotent."""
    target, _version, window = split_run_key(run_key)
    columns = run_columns(run_key)
    if not columns:
        raise LookupError(f"run {run_key!r} has no landed files to evaluate")
    spec = load_spec(spec_file(f"{target}.yaml"))  # Task 5's resolvers, one rule
    scope = load_scope(scope_file(spec.scope_ref))
    if scope.target != target:
        raise ScopeRefusal(f"spec for {target!r} names the scope record of {scope.target!r}")
    # `select *` only: on a target whose lake has no data columns, naming one is a binder error.
    current = query("select * from lake where run_key = ?", [run_key], target=target).dicts()
    # The trailing windows the rules read: REFERENCE_WINDOWS, or the warm-up if it is longer, so
    # a scope record warming up over more than 30 runs can still leave warm-up.
    last = max(REFERENCE_WINDOWS, scope.warmup_runs)
    history = query(
        'select * from lake where "window" in (select distinct "window" from lake '
        f'where "window" < ? order by 1 desc limit {last:d}) order by "window", run_key',
        [window],
        target=target,
    ).dicts()
    run = _Run(
        target=target,
        window=window,
        run_key=run_key,
        spec=spec,
        warmup_runs=scope.warmup_runs,
        columns=[c for c in columns if is_data_column(c)],
        rows=current,
        history=history,
    )
    findings = [*_row_count(run), *_null_rate(run), *_schema_drift(run), *_distribution_shift(run)]
    _persist(run_key, findings)
    return findings
