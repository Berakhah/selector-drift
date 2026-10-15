"""Closes M30. Client heal bundles are deleted after the scope record's
evidence_retention_days; the heal_events row and every count derived from it remain, and each
deletion is an audit_log row (which is how the read path tells *expired* from *corrupted*).

S-M4: the directory deleted is derived from the heal's integer id under the store root
(heallog.evidence.heal_bundle_dir), never from the stored evidence_uri string; a bundle that is
a link is skipped and audited, never followed.

Fix round 1:
- I2: a heal whose run has an unresolved quarantine is kept (audited `retention_skipped`,
  "quarantine open") -- promoting it later needs its bundle.
- Fix round 2 (T26A): a replay of an already-decided window is held in
  quarantine/*.replay-*.parquet (audited `quarantine_replayed`) and never re-opens the decision.
  While that file exists its run's heal bundles are kept too (`retention_skipped`,
  "replay held"), so evidence a human may still need is not silently purged. Retention never
  touches the replay files themselves.
- I4: every audit row is COMMITTED before its delete, so a crash mid-sweep can leave a bundle
  that is audited as expired but still present (retried next day), never one that is gone
  without its `evidence_expired` row.
- I3: client-confidential directories nothing can reach are swept, each audited:
  * a bundle whose heal row never got its evidence_uri is retained like any other (the heal
    row, not the uri, decides);
  * a heals/<n> directory with no heal row at all, past ORPHAN_GRACE;
  * crash leftovers (.tmp-*, .old-*) in heals/ and last-accepted/, past LEFTOVER_GRACE;
  * a last-accepted/t-* directory that no live (unexpired) scope record names.
  Audit subjects are store-relative paths or heal ids -- never a client's target name for a
  directory nobody can attribute.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from prefect import flow
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from selector_drift.config import get_settings
from selector_drift.db.models import AuditLog, HealEvent, Quarantine, ScopeRecordRow
from selector_drift.db.session import Session
from selector_drift.heallog.bundle import HEALS_DIR, LAST_ACCEPTED_DIR, is_link
from selector_drift.heallog.evidence import heal_bundle_dir, last_accepted_names

ACTOR = "system:evidence_retention"
LEFTOVER_GRACE = timedelta(hours=1)  # a write in progress is never swept
ORPHAN_GRACE = timedelta(days=1)  # a heal row may be committed just after its bundle
_LEFTOVER_PREFIXES = (".tmp-", ".old-")


def _audit(s: OrmSession, action: str, subject: str, detail: dict[str, Any]) -> None:
    """Committed on its own, before the delete it records (I4)."""
    s.add(
        AuditLog(at=datetime.now(UTC), actor=ACTOR, action=action, subject=subject, detail=detail)
    )
    s.commit()


def _age(path: Path, now: datetime) -> timedelta:
    return now - datetime.fromtimestamp(path.lstat().st_mtime, UTC)


def _remove(s: OrmSession, path: Path, action: str, subject: str, detail: dict[str, Any]) -> bool:
    """Audit, then delete. A link is never followed: it is audited as skipped."""
    if is_link(path):
        _audit(s, "retention_skipped", subject, {**detail, "why": "a link"})
        return False
    _audit(s, action, subject, detail)
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        _audit(s, "retention_delete_failed", subject, {"error": type(exc).__name__})
        return False
    return True


def _sweep_dir(
    s: OrmSession, directory: Path, now: datetime, keep: Callable[[str], bool], orphan_action: str
) -> int:
    """Leftovers and unreachable entries in heals/ or last-accepted/. `keep(name)` is True for
    an entry that something still reaches."""
    removed = 0
    if not directory.is_dir() or is_link(directory):
        return 0
    store = directory.name
    for entry in sorted(directory.iterdir()):
        name = entry.name
        subject = f"path:{store}/{name}"
        if name.startswith(_LEFTOVER_PREFIXES):
            if _age(entry, now) > LEFTOVER_GRACE and _remove(
                s, entry, "evidence_leftover_removed", subject, {"kind": name[:5]}
            ):
                removed += 1
        elif (
            not keep(name)
            and _age(entry, now) > (ORPHAN_GRACE if store == HEALS_DIR else LEFTOVER_GRACE)
            and _remove(s, entry, orphan_action, subject, {"why": "nothing references it"})
        ):
            removed += 1
    return removed


def _replay_held(s: OrmSession) -> set[str]:
    """Run keys with a replay still held on disk beside a decision already taken."""
    held: set[str] = set()
    for subject, detail in s.execute(
        select(AuditLog.subject, AuditLog.detail).where(AuditLog.action == "quarantine_replayed")
    ):
        uri = detail.get("data_uri") if isinstance(detail, dict) else None
        if isinstance(uri, str) and Path(uri).is_file():
            held.add(subject)
    return held


@flow(name="evidence_retention")
def evidence_retention(today: date | None = None) -> int:
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC)
    deleted = 0
    root = get_settings().evidence_root
    with Session() as s:
        policy: dict[str, int | None] = {}
        live: set[str] = set()
        for row in s.scalars(select(ScopeRecordRow).order_by(ScopeRecordRow.loaded_at)):
            policy[row.target] = row.evidence_retention_days  # latest load wins
            if row.expires_on >= today:
                live.add(row.target)
        open_quarantine = set(
            s.scalars(select(Quarantine.run_key).where(Quarantine.resolved_as.is_(None)))
        )
        replay_held = _replay_held(s)
        heals = list(s.scalars(select(HealEvent).order_by(HealEvent.id)))
        heal_ids = {heal.id for heal in heals}
        for heal in heals:
            # The heal row decides, not its evidence_uri: a bundle whose uri was never
            # persisted is retained like any other (I3).
            bundle = heal_bundle_dir(heal.id)
            if not bundle.exists() and not is_link(bundle):
                continue
            subject = f"heal:{heal.id}"
            if heal.run_key in open_quarantine:
                _audit(
                    s,
                    "retention_skipped",
                    subject,
                    {"target": heal.target, "why": "quarantine open"},
                )
                continue
            if heal.run_key in replay_held:
                _audit(
                    s,
                    "retention_skipped",
                    subject,
                    {"target": heal.target, "why": "replay held"},
                )
                continue
            if heal.target not in policy:
                # Fail loud, not open: no record means no stated policy. Keep, and say so.
                _audit(
                    s,
                    "retention_skipped",
                    subject,
                    {"target": heal.target, "why": "no scope record"},
                )
                continue
            days = policy[heal.target]
            if days is None or (today - heal.broke_at.date()).days < days:
                continue  # self-owned: life of the log; or not yet due
            detail = {
                "target": heal.target,
                "retention_days": days,
                "broke_at": heal.broke_at.isoformat(),
            }
            if _remove(s, bundle, "evidence_expired", subject, detail):
                deleted += 1

        deleted += _sweep_dir(
            s,
            root / HEALS_DIR,
            now,
            lambda name: not name.isdigit() or int(name) in heal_ids,
            "evidence_orphan_removed",
        )
        reachable = last_accepted_names(sorted(live))
        deleted += _sweep_dir(
            s,
            root / LAST_ACCEPTED_DIR,
            now,
            lambda name: name in reachable,
            "last_accepted_removed",
        )
    return deleted
