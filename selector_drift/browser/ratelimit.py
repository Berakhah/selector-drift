"""R8/H4: the scope record's rate limit, enforced across every process that touches a target.

Persistent, with no in-process state at all: the bucket IS the set of `audit_log` rows with
`action='ratelimit.acquire'` for the target. A per-run or per-process bucket says nothing about
ten runs starting at once; this one is shared by every worker that can reach the database.

`acquire()`, inside one transaction:

1. `pg_advisory_xact_lock(hashtext('ratelimit:' || target))` -- serialises every acquirer of
   this target, in any process, until the transaction ends;
2. counts this target's acquisitions in the last `per_seconds`, by Postgres `clock_timestamp()`
   (one clock for every process, never the caller's);
3. if the count has reached `requests`, sleeps -- still holding the lock -- until the oldest
   acquisition that must expire has aged out, then re-checks;
4. inserts its own row and commits, which releases the lock.

The limit comes from the `AuthorisedTarget` and nowhere else (design 4.3: "not a tunable").
Raising it means editing the scope record -- the document you would point at if anyone ever
asked why you accessed a system.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, insert, text

from selector_drift.db.models import AuditLog
from selector_drift.db.session import Session
from selector_drift.scope.record import AuthorisedTarget

ACTION: Final = "ratelimit.acquire"
ACTOR: Final = "system:ratelimit"
_MIN_SLEEP_SECONDS: Final = 0.05

_WINDOW_SQL: Final = text(
    """
    SELECT at, clock_timestamp() AS now
    FROM audit_log
    WHERE action = :action
      AND subject = :subject
      AND at > clock_timestamp() - make_interval(secs => :window)
    ORDER BY at
    """
)


def window_for(target: AuthorisedTarget) -> int:
    """The window in seconds. From the target, never from the environment or config."""
    return target.rate_limit.per_seconds


def acquire(target: AuthorisedTarget) -> None:
    """Block until `target` may make one more top-level navigation, then record it."""
    window = window_for(target)
    requests = target.rate_limit.requests
    params = {"action": ACTION, "subject": target.target, "window": window}

    with Session() as session, session.begin():
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"ratelimit:{target.target}"},
        )
        while True:
            rows = session.execute(_WINDOW_SQL, params).all()
            if len(rows) < requests:
                break
            # `requests` acquisitions must remain after this one lands, so the one that has to
            # age out first is the (len - requests)-th oldest.
            oldest: datetime = rows[len(rows) - requests].at
            now: datetime = rows[-1].now
            wait = (oldest + timedelta(seconds=window) - now).total_seconds()
            time.sleep(max(wait, _MIN_SLEEP_SECONDS))

        session.execute(
            insert(AuditLog).values(
                at=func.clock_timestamp(),
                actor=ACTOR,
                action=ACTION,
                subject=target.target,
                detail={
                    "requests": requests,
                    "per_seconds": window,
                    "record_sha256": target.record_sha256,
                },
            )
        )
