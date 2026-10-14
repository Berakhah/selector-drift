"""Closes M30. Client heal bundles are deleted after the scope record's
evidence_retention_days; the heal_events row and every count derived from it remain, and each
deletion is an audit_log row (which is how the read path tells *expired* from *corrupted*).

S-M4: the directory deleted is derived from the heal's integer id under the store root
(heallog.evidence.heal_bundle_dir), never from the stored evidence_uri string; a bundle that is
a link is skipped and audited, never followed.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime

from prefect import flow
from sqlalchemy import select

from selector_drift.db.models import AuditLog, HealEvent, ScopeRecordRow
from selector_drift.db.session import Session
from selector_drift.heallog.bundle import is_link
from selector_drift.heallog.evidence import heal_bundle_dir

ACTOR = "system:evidence_retention"


@flow(name="evidence_retention")
def evidence_retention(today: date | None = None) -> int:
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC)
    deleted = 0
    with Session() as s:
        policy: dict[str, int | None] = {}
        for row in s.scalars(select(ScopeRecordRow).order_by(ScopeRecordRow.loaded_at)):
            policy[row.target] = row.evidence_retention_days  # latest load wins
        heals = s.scalars(
            select(HealEvent).where(HealEvent.evidence_uri.is_not(None)).order_by(HealEvent.id)
        )
        for heal in heals:
            bundle = heal_bundle_dir(heal.id)
            if not bundle.exists() and not is_link(bundle):
                continue
            if heal.target not in policy:
                # Fail loud, not open: no record means no stated policy. Keep, and say so.
                s.add(
                    AuditLog(
                        at=now,
                        actor=ACTOR,
                        action="retention_skipped",
                        subject=f"heal:{heal.id}",
                        detail={"target": heal.target, "why": "no scope record"},
                    )
                )
                continue
            days = policy[heal.target]
            if days is None or (today - heal.broke_at.date()).days < days:
                continue  # self-owned: life of the log; or not yet due
            if is_link(bundle):
                s.add(
                    AuditLog(
                        at=now,
                        actor=ACTOR,
                        action="retention_skipped",
                        subject=f"heal:{heal.id}",
                        detail={"target": heal.target, "why": "bundle is a link"},
                    )
                )
                continue
            shutil.rmtree(bundle)
            s.add(
                AuditLog(
                    at=now,
                    actor=ACTOR,
                    action="evidence_expired",
                    subject=f"heal:{heal.id}",
                    detail={
                        "target": heal.target,
                        "retention_days": days,
                        "broke_at": heal.broke_at.isoformat(),
                    },
                )
            )
            deleted += 1
        s.commit()
    return deleted
