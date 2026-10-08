"""The one alert channel v1 has (M2). One webhook URL (Settings.alert_webhook_url), two layers:

- `post_alert()` (Task 6) -- the undeduplicated day-one channel for `refused` / `failed`.
- `notify()`, `notify_false_accepts()`, `notify_missed_heartbeats()` (Task 29A) -- the
  deduplicated channel for quarantines, HIGH+ findings, false accepts and missed heartbeats.
  One alert per (target, rule) per UTC day, recorded in `audit_log(action="alert_sent")` --
  never process memory, so ten flows and a restart still send one -- and recorded only once it
  is DELIVERED: a failed POST is retried on the next check (B7).

The body never carries page content or credentials (brief rule 6, Never 6). `notify()`'s body
is an allow-list (BODY_KEYS) -- never a claim, a value, evidence or `to_sarif` output, whose
bok_provenance carries cell values by design. The webhook URL is itself a bearer credential, so
it is never logged: a filter on the `httpx`/`httpcore` loggers drops any record that carries it.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from urllib.parse import urlsplit

import httpx
from croniter import croniter
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as OrmSession

from selector_drift._bok_shim import Finding, Severity
from selector_drift.config import get_settings
from selector_drift.db.models import AuditLog, HealEvent, Heartbeat, Run
from selector_drift.db.session import Session
from selector_drift.spec.loader import load_spec

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


# ---------------------------------------------------------------- Task 29A: the notify layer

ACTOR: Final = "system:alerts"
SENT: Final = "alert_sent"
SCORER: Final = "testbench.score"  # Task 26C's actor on audit_log(action="scored")
PAGE: Final = frozenset({Severity.CRITICAL, Severity.HIGH})
_RANK: Final = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}
HEARTBEAT_GRACE: Final = dt.timedelta(minutes=30)
SLOT_TOLERANCE: Final = dt.timedelta(minutes=1)
# notify_false_accepts looks at today and yesterday (UTC) only: configuring a webhook must not
# page every historical day at once, nor an unconfigured channel re-warn the whole history.
FALSE_ACCEPT_DAYS: Final = 2
# A logger-level filter sees only records logged on that exact logger, never ones propagated
# from its children, so every logger the http client stack writes to is named here.
HTTP_CLIENT_LOGGERS: Final = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
# The whole webhook body. Anything not named here never leaves the boundary (rule 6).
BODY_KEYS: Final = ("target", "rule", "severity", "title", "run_key", "threshold", "day")


def _secret_fragments(url: str) -> list[str]:
    """The webhook URL and its path: either one in a log line leaks the bearer credential."""
    path = urlsplit(url).path
    return [url, path] if len(path) > 1 else [url]


class _HideWebhook(logging.Filter):
    """httpx logs every request URL at INFO; an incoming-webhook URL is a bearer credential."""

    def filter(self, record: logging.LogRecord) -> bool:
        url = webhook_url()
        if not url:
            return True
        message = record.getMessage()
        return not any(fragment in message for fragment in _secret_fragments(url))


_HIDE_WEBHOOK: Final = _HideWebhook()
for _name in HTTP_CLIENT_LOGGERS:
    logging.getLogger(_name).addFilter(_HIDE_WEBHOOK)


def _channel() -> tuple[str | None, str]:
    """(deliverable URL, why not). Task 6's webhook_url() (Settings), refused unless https: the
    URL is a bearer credential and must not cross the network in the clear."""
    url = webhook_url()
    if url is None:
        return None, f"{WEBHOOK_ENV} is unset"
    if not url.startswith("https://"):
        return None, f"{WEBHOOK_ENV} is set but refused (not an https URL)"
    return url, ""


@dataclass(frozen=True)
class Alert:
    target: str
    rule: str
    severity: Severity
    title: str
    run_key: str | None
    threshold: str | None = None
    day: dt.date | None = None

    def body(self, day: dt.date) -> dict[str, str | None]:
        # An allow-list (BODY_KEYS). Never a claim, an extracted value or evidence (rule 6).
        return {
            "target": self.target,
            "rule": self.rule,
            "severity": self.severity.value,
            "title": self.title,
            "run_key": self.run_key,
            "threshold": self.threshold,
            "day": day.isoformat(),
        }


def check_alert_channel() -> bool:
    url, why = _channel()
    if url is None:
        _log.warning("%s: alerts will be LOGGED ONLY and reach nobody", why)
        return False
    return True


def _post(url: str, body: dict[str, str | None]) -> bool:
    try:
        httpx.post(url, json=body, timeout=TIMEOUT_SECONDS).raise_for_status()
    except Exception as exc:  # M-1: httpx.InvalidURL is not an httpx.HTTPError
        # Never str(exc): an HTTPStatusError's text carries the URL.
        _log.error(
            "alert webhook delivery failed (%s); retried on the next check", type(exc).__name__
        )
        return False
    return True


def _unique(alerts: Sequence[Alert]) -> list[Alert]:
    """One alert per (target, rule, day): the most severe of them."""
    best: dict[tuple[str, str, dt.date | None], Alert] = {}
    for a in alerts:
        k = (a.target, a.rule, a.day)
        if k not in best or _RANK[a.severity] < _RANK[best[k].severity]:
            best[k] = a
    return list(best.values())


def _dispatch(alerts: Sequence[Alert], now: dt.datetime) -> None:
    url, why = _channel()
    for alert in _unique(alerts):
        day = alert.day or now.astimezone(UTC).date()
        subject = f"{alert.target}:{alert.rule}:{day.isoformat()}"
        if url is None:
            # Said every time, recorded never: nothing was delivered.
            _log.warning(
                "ALERT NOT DELIVERED (%s): %s %s %s -- %s",
                why,
                alert.severity.value,
                alert.target,
                alert.rule,
                alert.title,
            )
            continue
        with Session() as s:
            # Serialise concurrent flows on one subject: check, deliver, record, release. The
            # lock is transaction-scoped, so it ends at commit or when the session closes.
            s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": subject})
            sent = s.scalar(
                select(AuditLog.id)
                .where(AuditLog.action == SENT, AuditLog.subject == subject)
                .limit(1)
            )
            if sent is not None:
                continue
            if not _post(url, alert.body(day)):
                continue  # not delivered, so not recorded: the next check retries it
            s.add(
                AuditLog(
                    at=now,
                    actor=ACTOR,
                    action=SENT,
                    subject=subject,
                    detail={
                        "target": alert.target,
                        "rule": alert.rule,
                        "severity": alert.severity.value,
                        "run_key": alert.run_key,
                    },
                )
            )
            s.commit()


def notify(findings: Sequence[Finding], run: Run, *, now: dt.datetime | None = None) -> None:
    """Fires on a quarantined run and on every HIGH+ finding. Task 26A calls it on every
    settled run."""
    alerts: list[Alert] = []
    if run.state == "quarantined":
        alerts.append(
            Alert(
                run.target,
                "quarantine",
                Severity.HIGH,
                f"{run.run_key} quarantined: data held, nothing written",
                run.run_key,
            )
        )
    for f in findings:
        if f.severity in PAGE:
            threshold = f.properties.get("threshold")
            alerts.append(
                Alert(
                    run.target,
                    str(f.properties.get("rule", f.id)),
                    f.severity,
                    f.title,
                    run.run_key,
                    str(threshold) if threshold is not None else None,
                )
            )
    _dispatch(alerts, now or dt.datetime.now(UTC))


def _target_of(s: OrmSession, row: AuditLog) -> str | None:
    """The scorer's row shape (Task 26C, ledger 2026-09-26): subject = str(heal_id),
    actor = 'testbench.score', detail.run_key = the heal's run. The run_key names the target;
    the heal id is the fallback for a row written before detail.run_key existed."""
    run_key = (row.detail or {}).get("run_key")
    if isinstance(run_key, str) and ":" in run_key:
        return run_key.split(":", 1)[0]
    if row.subject.isdigit():
        heal = s.get(HealEvent, int(row.subject))
        return heal.target if heal is not None else None
    return None


def notify_false_accepts(*, now: dt.datetime | None = None) -> None:
    """HIGH on every scored row whose judgement is false_accept: a wrong heal the gate let
    through (Task 26C). Deduplicated per (target, false_accept) per UTC day of the scoring."""
    now = now or dt.datetime.now(UTC)
    today = now.astimezone(UTC).date()
    since = dt.datetime.combine(today - dt.timedelta(days=FALSE_ACCEPT_DAYS - 1), dt.time(), UTC)
    alerts: list[Alert] = []
    with Session() as s:
        rows = s.scalars(
            select(AuditLog).where(
                AuditLog.actor == SCORER,
                AuditLog.at >= since,
                AuditLog.action == "scored",
                AuditLog.detail["judgement"].astext == "false_accept",
            )
        ).all()
        for row in rows:
            target = _target_of(s, row)
            if target is not None:
                run_key = (row.detail or {}).get("run_key")
                alerts.append(
                    Alert(
                        target,
                        "false_accept",
                        Severity.HIGH,
                        # The subject is a heal id; anything else is never echoed (rule 6).
                        "the gate accepted a wrong heal (heal "
                        f"{row.subject if row.subject.isdigit() else 'unknown'})",
                        run_key if isinstance(run_key, str) else None,
                        day=row.at.astimezone(UTC).date(),
                    )
                )
    _dispatch(alerts, now)


def notify_missed_heartbeats(*, now: dt.datetime | None = None) -> None:
    """HIGH when a target's latest cron slot (at least HEARTBEAT_GRACE old) has no heartbeat.
    A target that has never written a heartbeat has not started its log: not missed."""
    now = now or dt.datetime.now(UTC)
    alerts: list[Alert] = []
    with Session() as s:
        for path in sorted(get_settings().spec_dir.glob("*.yaml")):
            target = path.stem  # spec files are <target>.yaml
            first = s.scalar(
                select(func.min(Heartbeat.scheduled_for)).where(Heartbeat.target == target)
            )
            if first is None:
                continue
            cron = croniter(load_spec(path).schedule, now.astimezone(UTC) - HEARTBEAT_GRACE)
            slot = cron.get_prev(datetime)
            if slot < first:
                continue
            hit = s.scalar(
                select(Heartbeat.id)
                .where(
                    Heartbeat.target == target,
                    Heartbeat.scheduled_for >= slot - SLOT_TOLERANCE,
                    Heartbeat.scheduled_for <= slot + SLOT_TOLERANCE,
                )
                .limit(1)
            )
            if hit is None:
                alerts.append(
                    Alert(
                        target,
                        "missed_heartbeat",
                        Severity.HIGH,
                        f"{target}: no heartbeat for the {slot:%Y-%m-%d %H:%M}Z slot",
                        None,
                        day=slot.date(),
                    )
                )
    _dispatch(alerts, now)


def notify_deploy_feed_missing(target: str, *, now: dt.datetime | None = None) -> None:
    """A declared self-host deploy log that does not exist yet is a condition, not a crash: one
    deduplicated alert per target per UTC day (Task 29A fix round 1). The path is not sent."""
    _dispatch(
        [
            Alert(
                target,
                "deploy_feed_missing",
                Severity.MEDIUM,
                f"{target}: self-host deploy log not found; no deploy history ingested",
                None,
            )
        ],
        now or dt.datetime.now(UTC),
    )
