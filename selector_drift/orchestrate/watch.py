"""Every fifteen minutes: the checks that have no run to hang off (Task 29A).

One feed being down must not stop the other feeds, and must not be a warning either: every
declared target is attempted, then the flow run fails naming the ones that did not ingest.
"""

from __future__ import annotations

import logging

from prefect import flow

from selector_drift.orchestrate.deploys import declared_feeds, ingest_deploys
from selector_drift.quality.alerts import (
    check_alert_channel,
    notify_false_accepts,
    notify_missed_heartbeats,
)

WATCH_TIMEOUT_SECONDS = 300

_log = logging.getLogger(__name__)


@flow(name="alert-watch", retries=0, timeout_seconds=WATCH_TIMEOUT_SECONDS)
def alert_watch() -> None:
    check_alert_channel()
    notify_missed_heartbeats()
    notify_false_accepts()
    failed: list[str] = []
    for target in sorted(declared_feeds()):
        try:
            ingest_deploys(target)
        except Exception as exc:  # collected, re-raised below
            _log.error("deploy ingest failed for %s (%s)", target, type(exc).__name__)
            failed.append(target)
    if failed:  # fails the flow run: visible, not a warning
        raise RuntimeError(f"deploy ingest failed for {failed}")
