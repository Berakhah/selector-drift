"""The one alert channel v1 has (M2). Task 29A builds notify() -- dedup, findings, quarantines,
missed heartbeats -- on top of post_alert(). The body never carries page content or credentials:
a target, a run key and a one-line summary."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

import httpx

from selector_drift.config import get_settings

AlertKind = Literal["refused", "failed", "quarantined", "missed_heartbeat", "finding"]
WEBHOOK_ENV = "SD_ALERT_WEBHOOK_URL"  # the variable Settings.alert_webhook_url reads (Task 5)
TIMEOUT_SECONDS = 10.0
MAX_SUMMARY = 500

_log = logging.getLogger(__name__)


def webhook_url() -> str | None:
    # Through Settings, never os.environ directly (2026-09-26): one place reads configuration.
    return get_settings().alert_webhook_url


def post_alert(kind: AlertKind, target: str, run_key: str | None, summary: str) -> bool:
    """True when the webhook accepted the alert. Never raises: a broken channel must not turn a
    refusal into a crash -- it is logged at ERROR instead."""
    url = webhook_url()
    if url is None:
        _log.error("ALERT NOT SENT (%s is unset): %s %s %s", WEBHOOK_ENV, kind, target, summary)
        return False
    payload = {
        "kind": kind,
        "target": target,
        "run_key": run_key,
        "summary": summary[:MAX_SUMMARY],
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        httpx.post(url, json=payload, timeout=TIMEOUT_SECONDS).raise_for_status()
    except httpx.HTTPError as exc:
        _log.error("alert webhook failed (%s): %s %s", type(exc).__name__, kind, target)
        return False
    return True
