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


def sanitize_for_alert(exc: BaseException) -> str:
    """The exception type plus a sanitised first line of its message -- never the full text
    (I-2, final review). The alert channel (SD_ALERT_WEBHOOK_URL, typically Slack or another
    third party) is outside the client boundary (CLAUDE.md rule 6); a Playwright locator
    timeout's "Call log:" section routinely quotes the DOM content it was matching against, e.g.
    "locator resolved to <td>AB12345678</td>". The full text stays only in run_attempts.error /
    runs.error, inside the boundary.

    Two cuts, both defensive: everything after the first newline is dropped (where Playwright's
    call log lives), and everything from the first '<' onward is dropped too, in case markup
    ever appears on the first line itself."""
    first_line = str(exc).split("\n", 1)[0]
    first_line = first_line.split("<", 1)[0].rstrip()
    kind = type(exc).__name__
    return f"{kind}: {first_line}" if first_line else kind


def post_alert(kind: AlertKind, target: str, run_key: str | None, summary: str) -> bool:
    """True when the webhook accepted the alert. Never raises (M-1, final review): a broken
    channel -- a dead webhook, a malformed URL httpx itself refuses to construct -- must not
    turn a refusal or failure into a crash. Logged at ERROR instead, without the webhook URL
    (which may carry a secret path)."""
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
    except Exception as exc:
        # M-1: was `except httpx.HTTPError`, which does not cover httpx.InvalidURL (raised by
        # httpx.post() itself, before any request is sent, for a URL it cannot construct at
        # all -- a plain Exception subclass) or any other surprise from the http client. The
        # webhook URL itself is never logged: it is exactly the kind of thing that carries a
        # secret path.
        _log.error("alert webhook failed (%s): %s %s", type(exc).__name__, kind, target)
        return False
    return True
