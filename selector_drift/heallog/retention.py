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
  "replay held"), so evidence a human may still need is not silently purged.
- T26A follow-up: a replay file holds extracted client data, so it is not held forever. It expires
  after the target's evidence_retention_days counted from its `quarantine_replayed` audit row
  (audited `replay_expired`; the replayed row stays as the record; a null policy keeps it), which
  also releases the heal bundles it pinned. A replay file no audit row names (hold() crashed or
  its commit failed between the write and the audit) is swept past ORPHAN_GRACE
  (`replay_orphan_removed`). A quarantine's own `<key>.parquet` is never a replay and never swept.
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

import fnmatch
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from prefect import flow
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from selector_drift.config import get_settings
from selector_drift.db.models import AuditLog, HealEvent, Quarantine, Run, ScopeRecordRow
from selector_drift.db.session import Session
from selector_drift.heallog.bundle import HEALS_DIR, LAST_ACCEPTED_DIR, is_link
from selector_drift.heallog.evidence import heal_bundle_dir, last_accepted_names

ACTOR = "system:evidence_retention"
LEFTOVER_GRACE = timedelta(hours=1)  # a write in progress is never swept
ORPHAN_GRACE = timedelta(days=1)  # a heal row may be committed just after its bundle
_LEFTOVER_PREFIXES = (".tmp-", ".old-")
_QUARANTINE_DIR = "quarantine"  # == orchestrate.accept.QUARANTINE_DIR; a test pins the two
_REPLAY_GLOB = "*.replay-*.parquet"


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


def _replays(s: OrmSession) -> list[tuple[str, datetime, str]]:
    """(run key, replayed at, stored data_uri) of every audited replay, oldest first."""
    found: list[tuple[str, datetime, str]] = []
    for subject, at, detail in s.execute(
        select(AuditLog.subject, AuditLog.at, AuditLog.detail)
        .where(AuditLog.action == "quarantine_replayed")
        .order_by(AuditLog.at, AuditLog.id)
    ):
        uri = detail.get("data_uri") if isinstance(detail, dict) else None
        if isinstance(uri, str):
            found.append((subject, at, uri))
    return found


def _replay_name(uri: str) -> str | None:
    """The file name a stored data_uri names, if it is a replay's: `<key>.replay-<id>.parquet`.
    The stored path is only a hint of where hold() wrote it -- the lake may have moved since --
    so the file is found by this name under today's quarantine directory, and nothing else the
    row could name is ever a candidate for deletion."""
    name = Path(uri).name
    return name if fnmatch.fnmatchcase(name, _REPLAY_GLOB) and not name.startswith(".") else None


def _quarantine_dir() -> Path:
    return get_settings().lake_root / _QUARANTINE_DIR


def _expire_replays(s: OrmSession, policy: dict[str, int | None], today: date) -> int:
    """A replay holds extracted client data, so it lives as long as the target's own
    evidence_retention_days from the day it was replayed, never longer. No stated policy, or a
    self-owned target (None), keeps it. Expiring it also releases the heal bundles it pinned."""
    directory = _quarantine_dir()
    if is_link(directory):
        return 0
    deleted = 0
    targets = dict(s.execute(select(Run.run_key, Run.target)).tuples().all())
    for run_key, at, uri in _replays(s):
        days = policy.get(targets.get(run_key, ""))
        if days is None or (today - at.date()).days < days:
            continue
        name = _replay_name(uri)
        if name is None:
            # Audited, never followed: a stored string is not an authority to delete anything.
            _audit(s, "retention_skipped", run_key, {"why": "not a replay file", "data_uri": uri})
            continue
        path = directory / name
        if not (path.exists() or is_link(path)):
            continue
        detail = {"data_uri": str(path), "retention_days": days, "replayed_at": at.isoformat()}
        if _remove(s, path, "replay_expired", run_key, detail):
            deleted += 1
    return deleted


def _sweep_replay_orphans(s: OrmSession, now: datetime) -> int:
    """hold() writes a replay file and then commits its audit row. A crash or a failed commit
    between the two leaves client data that retention cannot see: swept past ORPHAN_GRACE.
    Audited by NAME, not by stored path, so a moved lake never turns audited replays into
    orphans."""
    directory = _quarantine_dir()
    if not directory.is_dir() or is_link(directory):
        return 0
    audited = {Path(uri).name for _, _, uri in _replays(s)}
    removed = 0
    for entry in sorted(directory.glob(_REPLAY_GLOB)):
        if (
            entry.name in audited
            or entry.name.startswith(".")
            or _age(entry, now) <= ORPHAN_GRACE
            or not _remove(
                s,
                entry,
                "replay_orphan_removed",
                f"path:{_QUARANTINE_DIR}/{entry.name}",
                {"why": "no audit row names it"},
            )
        ):
            continue
        removed += 1
    return removed


def _replay_held(s: OrmSession) -> set[str]:
    """Run keys with a replay still held on disk beside a decision already taken."""
    directory = _quarantine_dir()
    return {
        run_key
        for run_key, _, uri in _replays(s)
        if (name := _replay_name(uri)) is not None and (directory / name).is_file()
    }


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
        deleted += _expire_replays(s, policy, today)  # before the heals they pin are judged
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
        deleted += _sweep_replay_orphans(s, now)
    return deleted
