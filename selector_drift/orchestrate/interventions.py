"""The intervention ledger (M1). 'Zero manual selector fixes' is a claim only if every manual
fix is a row. This module writes rows; derive.py (Task 34B) is the only thing that counts them.

A spec file is edited outside the product, so there is no editor to hook: `observe_spec()` runs
at the top of every run (Task 26A wires it), stores the spec it saw in
`audit_log(action="spec_seen")`, and when the hash differs from the last one seen writes one
`spec_edit` row per changed field. First sight is a baseline, not an edit.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Final, Literal, get_args

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import AuditLog, Intervention
from selector_drift.db.session import Session
from selector_drift.spec.schema import TaskSpec

Kind = Literal["spec_edit", "cache_edit", "manual_promote"]
SPEC_SEEN: Final = "spec_seen"
LOADER_ACTOR: Final = "system:spec_loader"
# `targets` is diffed per FieldTarget (targets.<name>); `version` is bookkeeping -- a bump with no
# other change is not a human changing what is extracted.
_NOT_A_FIELD: Final = frozenset({"targets", "version"})

Change = tuple[str, dict[str, Any], dict[str, Any]]


def _row(
    kind: Kind,
    actor: str,
    target: str,
    field: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    reason: str,
    at: dt.datetime,
) -> Intervention:
    if kind not in get_args(Kind):
        raise ValueError(f"unknown intervention kind {kind!r}")
    if not actor.strip():
        raise ValueError("an intervention with no actor is not evidence")
    return Intervention(
        at=at,
        actor=actor,
        kind=kind,
        target=target,
        field=field,
        before=before,
        after=after,
        reason=reason,
    )


def record_intervention(
    kind: Kind,
    actor: str,
    target: str,
    field: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    reason: str,
    *,
    at: dt.datetime | None = None,
) -> int:
    row = _row(kind, actor, target, field, before, after, reason, at or dt.datetime.now(dt.UTC))
    with Session() as s:
        s.add(row)
        s.commit()
        return int(row.id)


def record_cache_edit(
    actor: str,
    target: str,
    field: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    reason: str,
) -> int:
    """The only sanctioned way to change a selector_cache row by hand."""
    return record_intervention("cache_edit", actor, target, field, before, after, reason)


def spec_changes(old: TaskSpec, new: TaskSpec) -> list[Change]:
    """One entry per changed field. Top-level fields by name, `{name: value}` on each side (the
    schedule's shape is pinned by derive._schedule_spans, Task 34B); each FieldTarget as
    `targets.<name>`, `{"target": ...}` on each side, so a target called `schedule` cannot
    collide with the schedule."""
    return _changes(old.model_dump(mode="json"), new.model_dump(mode="json"))


def _changes(o: dict[str, Any], n: dict[str, Any]) -> list[Change]:
    """On the JSON dumps (see _baseline for how a stored body is normalised first)."""
    out: list[Change] = []
    for key in sorted((set(o) | set(n)) - _NOT_A_FIELD):
        if o.get(key) != n.get(key):
            out.append((key, {key: o.get(key)}, {key: n.get(key)}))
    ot = {t["name"]: t for t in o.get("targets", [])}
    nt = {t["name"]: t for t in n.get("targets", [])}
    for name in sorted(set(ot) | set(nt)):
        if ot.get(name) != nt.get(name):
            out.append((f"targets.{name}", {"target": ot.get(name)}, {"target": nt.get(name)}))
    return out


def _last_seen(s: OrmSession, target: str) -> dict[str, Any] | None:
    last = s.scalars(
        select(AuditLog)
        .where(AuditLog.action == SPEC_SEEN, AuditLog.subject == target)
        .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        .limit(1)
    ).first()
    return dict(last.detail) if last is not None else None


def _baseline(stored: dict[str, Any]) -> dict[str, Any]:
    """A stored spec_seen body, normalised through today's TaskSpec so a field the schema gained
    since (with a default) is not a false spec_edit row in the ledger the headline is checked
    against (fix round 1). A body that no longer validates is diffed raw: it must not stop every
    run at its first step."""
    try:
        return TaskSpec.model_validate(stored).model_dump(mode="json")
    except ValidationError:
        return stored


def observe_spec(
    target: str,
    spec: TaskSpec,
    *,
    actor: str = LOADER_ACTOR,
    now: dt.datetime | None = None,
) -> list[int]:
    """The ids of the spec_edit rows written: none on first sight or an unchanged spec."""
    now = now or dt.datetime.now(dt.UTC)
    body = spec.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with Session() as s:
        # Two runs of one target starting together must not both diff against the same
        # baseline and record the edit twice.
        s.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"{SPEC_SEEN}:{target}"}
        )
        previous = _last_seen(s, target)
        if previous is not None and previous.get("sha256") == digest:
            return []
        s.add(
            AuditLog(
                at=now,
                actor=actor,
                action=SPEC_SEEN,
                subject=target,
                detail={"sha256": digest, "spec": body},
            )
        )
        rows: list[Intervention] = []
        if previous is not None:  # first sight is a baseline, not an edit
            reason = (
                f"spec file changed between runs ({str(previous['sha256'])[:12]} -> {digest[:12]})"
            )
            rows = [
                _row("spec_edit", actor, target, field, before, after, reason, now)
                for field, before, after in _changes(_baseline(dict(previous["spec"])), body)
            ]
            s.add_all(rows)
        s.commit()
        return [int(r.id) for r in rows]
