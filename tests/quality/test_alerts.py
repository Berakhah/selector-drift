"""Task 29A: the deduplicated alert channel (M2, B7, brief rule 6).

Adapted from the brief: `Finding`/`Severity`/`to_sarif` come from `bok_core.findings`;
`Claim`/`DomSource`/`Method` from the installed bok_core.provenance submodules. The database
is touched only by integration tests (M26)."""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from bok_core.findings import Finding, Severity, to_sarif
from bok_core.provenance.claim import Claim, Method
from bok_core.provenance.source import DomSource
from sqlalchemy import text

from selector_drift.config import get_settings
from selector_drift.db.models import AuditLog, Heartbeat, Run
from selector_drift.db.session import Session, engine
from selector_drift.quality.alerts import (
    BODY_KEYS,
    check_alert_channel,
    notify,
    notify_false_accepts,
    notify_missed_heartbeats,
)
from tests.land.factory import SPEC_YAML

WEBHOOK = "https://hooks.example.invalid/services/T0/B0/test-only-not-a-secret"
NOW = dt.datetime(2026, 10, 1, 6, 0, tzinfo=dt.UTC)
KEY = "testbench:2:2026-10-01"
CELL = "AB12345678"


@pytest.fixture(autouse=True)
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "testbench.yaml").write_text(SPEC_YAML, encoding="utf-8")  # schedule 12 4 * * *
    monkeypatch.setenv("SD_SPEC_DIR", str(specs))
    monkeypatch.setenv("SD_ALERT_WEBHOOK_URL", WEBHOOK)  # Settings.alert_webhook_url
    get_settings.cache_clear()
    if request.node.get_closest_marker("integration") is not None:
        with engine.begin() as c:
            c.execute(
                text("TRUNCATE audit_log, heartbeats, heal_events, runs RESTART IDENTITY CASCADE")
            )
    yield
    get_settings.cache_clear()


def _run(state: str = "completed") -> Run:
    return Run(
        run_key=KEY,
        target="testbench",
        spec_version=2,
        window=NOW.date(),
        first_started_at=NOW,
        state=state,
    )


def _finding(severity: Severity, rule: str = "row_count") -> Finding:
    parent = Claim(
        value=CELL,
        method=Method.DETERMINISTIC,
        observed_at=NOW,
        source=DomSource(snapshot_id="e5" * 32, selector="td", rung=1),
    )
    derived = Claim(
        value=40, method=Method.DERIVED, observed_at=NOW, source=parent.source, parents=[parent]
    )
    return Finding(
        id=f"quality/{rule}/run",
        severity=severity,
        title=f"{rule} breached",
        claim=derived,
        properties={
            "rule": rule,
            "field": None,
            "observed": "40",
            "threshold": ">= 50",
            "threshold_reason": "why",
        },
    )


def _sent() -> int:
    with Session() as s:
        return s.query(AuditLog).filter_by(action="alert_sent").count()


@pytest.mark.integration
def test_a_quarantine_notifies_exactly_once(httpx_mock) -> None:
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    notify([], _run("quarantined"), now=NOW)
    notify([], _run("quarantined"), now=NOW)  # dedup per (target, rule) per day
    assert len(httpx_mock.get_requests()) == 1
    assert _sent() == 1


@pytest.mark.integration
def test_dedup_survives_a_restart_because_it_lives_in_the_audit_log(httpx_mock) -> None:
    # B7: another process (or this one before a restart) already delivered today's alert.
    with Session() as s:
        s.add(
            AuditLog(
                at=NOW,
                actor="system:alerts",
                action="alert_sent",
                subject="testbench:quarantine:2026-10-01",
                detail={},
            )
        )
        s.commit()
    notify([], _run("quarantined"), now=NOW + dt.timedelta(hours=3))
    assert httpx_mock.get_requests() == []


@pytest.mark.integration
def test_a_high_finding_alerts_and_a_medium_one_does_not(httpx_mock) -> None:
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    notify(
        [_finding(Severity.MEDIUM, "null_rate"), _finding(Severity.HIGH, "row_count")],
        _run(),
        now=NOW,
    )
    (request,) = httpx_mock.get_requests()
    assert b'"rule":"row_count"' in request.content.replace(b" ", b"")


@pytest.mark.integration
def test_two_high_findings_of_one_rule_are_one_alert(httpx_mock) -> None:
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    notify([_finding(Severity.HIGH), _finding(Severity.CRITICAL)], _run(), now=NOW)
    (request,) = httpx_mock.get_requests()
    assert json.loads(request.content)["severity"] == "CRITICAL"  # the worse of the two


@pytest.mark.integration
def test_the_same_rule_alerts_again_the_next_day(httpx_mock) -> None:
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    notify([], _run("quarantined"), now=NOW)
    notify([], _run("quarantined"), now=NOW + dt.timedelta(days=1))
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.integration
def test_the_webhook_body_carries_no_extracted_value(httpx_mock) -> None:
    # Brief rule 6: a portal value never leaves the client's boundary, not even in an alert.
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    notify([_finding(Severity.HIGH)], _run(), now=NOW)
    (request,) = httpx_mock.get_requests()
    assert CELL.encode() not in request.content and b"claim" not in request.content


@pytest.mark.integration
def test_the_webhook_body_is_the_allow_list_and_never_sarif(httpx_mock) -> None:
    # Never 6: to_sarif's bok_provenance carries the cell value by design, so the body must be
    # built from the allow-list -- never from the Finding, its claim chain or its SARIF form.
    finding = _finding(Severity.HIGH)
    distinctive = finding.model_copy(
        update={"properties": finding.properties | {"observed": CELL, "field": "claim_number"}}
    )
    assert CELL in json.dumps(to_sarif([distinctive], tool_name="t", tool_version="0"))
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    notify([distinctive], _run(), now=NOW)
    (request,) = httpx_mock.get_requests()
    body = json.loads(request.content)
    assert (
        set(body)
        == set(BODY_KEYS)
        == {
            "target",
            "rule",
            "severity",
            "title",
            "run_key",
            "threshold",
            "day",
        }
    )
    assert CELL.encode() not in request.content
    assert b"bok_provenance" not in request.content and b"observed" not in request.content


@pytest.mark.integration
def test_a_missing_webhook_logs_and_sends_nothing(httpx_mock, monkeypatch, caplog) -> None:
    monkeypatch.delenv("SD_ALERT_WEBHOOK_URL")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="selector_drift.quality.alerts"):
        notify([], _run("quarantined"), now=NOW)
    assert httpx_mock.get_requests() == []
    assert "SD_ALERT_WEBHOOK_URL" in caplog.text and "quarantine" in caplog.text
    assert _sent() == 0  # not delivered, so not "sent"


@pytest.mark.integration
def test_a_missing_webhook_says_so_every_time(httpx_mock, monkeypatch, caplog) -> None:
    monkeypatch.delenv("SD_ALERT_WEBHOOK_URL")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="selector_drift.quality.alerts"):
        notify([], _run("quarantined"), now=NOW)
        notify([], _run("quarantined"), now=NOW)
    warnings = [r for r in caplog.records if "ALERT NOT DELIVERED" in r.getMessage()]
    assert len(warnings) == 2 and all(r.levelno == logging.WARNING for r in warnings)


@pytest.mark.integration
def test_a_failed_delivery_is_retried_not_marked_sent(httpx_mock) -> None:
    httpx_mock.add_response(url=WEBHOOK, method="POST", status_code=500)
    httpx_mock.add_response(url=WEBHOOK, method="POST", status_code=200)
    notify([], _run("quarantined"), now=NOW)
    assert _sent() == 0
    notify([], _run("quarantined"), now=NOW)
    assert len(httpx_mock.get_requests()) == 2 and _sent() == 1


@pytest.mark.integration
def test_the_webhook_url_is_never_logged(httpx_mock, caplog) -> None:
    # An incoming-webhook URL is a bearer credential.
    httpx_mock.add_response(url=WEBHOOK, method="POST", status_code=500)
    with caplog.at_level(logging.DEBUG):
        notify([], _run("quarantined"), now=NOW)
    assert WEBHOOK not in caplog.text and "T0/B0" not in caplog.text


@pytest.mark.unit
def test_the_httpx_logger_drops_any_record_carrying_the_webhook(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 200 OK"', WEBHOOK)
        logging.getLogger("httpx").info("HTTP Request: GET https://api.example.invalid/ok")
    assert "T0/B0" not in caplog.text
    assert "api.example.invalid/ok" in caplog.text  # the filter is not a blanket mute


@pytest.mark.integration
def test_a_false_accept_pages_at_high_once(httpx_mock) -> None:
    # Amended 2026-09-26: a wrong heal the gate let through (Task 26C).
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    with Session() as s:
        # Task 26C's row shape: subject is the heal id, detail.run_key names the run.
        s.add(
            AuditLog(
                at=NOW,
                actor="testbench.score",
                action="scored",
                subject="7",
                detail={"judgement": "false_accept", "field": "amount", "run_key": KEY},
            )
        )
        s.commit()
    notify_false_accepts(now=NOW)
    notify_false_accepts(now=NOW + dt.timedelta(minutes=15))
    (request,) = httpx_mock.get_requests()
    body = request.content.replace(b" ", b"")
    assert b'"rule":"false_accept"' in body and b'"severity":"HIGH"' in body


@pytest.mark.integration
def test_a_correct_judgement_does_not_page(httpx_mock) -> None:
    with Session() as s:
        s.add(
            AuditLog(
                at=NOW,
                actor="testbench.score",
                action="scored",
                subject="7",
                detail={"judgement": "correct_refusal", "run_key": KEY},
            )
        )
        s.commit()
    notify_false_accepts(now=NOW)
    assert httpx_mock.get_requests() == []


@pytest.mark.integration
def test_a_missed_heartbeat_pages(httpx_mock) -> None:
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    yesterday = dt.datetime(2026, 9, 30, 4, 12, tzinfo=dt.UTC)
    with Session() as s:
        s.add(
            Heartbeat(target="testbench", scheduled_for=yesterday, fired_at=yesterday, run_key=None)
        )
        s.commit()
    notify_missed_heartbeats(now=NOW)  # today's 04:12 slot never fired
    (request,) = httpx_mock.get_requests()
    assert b'"rule":"missed_heartbeat"' in request.content.replace(b" ", b"")


@pytest.mark.integration
def test_a_fired_slot_does_not_page(httpx_mock) -> None:
    today = dt.datetime(2026, 10, 1, 4, 12, 3, tzinfo=dt.UTC)
    with Session() as s:
        s.add(
            Heartbeat(
                target="testbench",
                scheduled_for=today.replace(second=0),
                fired_at=today,
                run_key=KEY,
            )
        )
        s.commit()
    notify_missed_heartbeats(now=NOW)
    assert httpx_mock.get_requests() == []


@pytest.mark.integration
def test_a_target_whose_log_has_not_started_is_not_missed(httpx_mock) -> None:
    notify_missed_heartbeats(now=NOW)  # no heartbeat has ever been written
    assert httpx_mock.get_requests() == []


@pytest.mark.unit
def test_an_unconfigured_channel_says_so(monkeypatch, caplog) -> None:
    monkeypatch.delenv("SD_ALERT_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="selector_drift.quality.alerts"):
        assert check_alert_channel() is False
    assert "LOGGED ONLY" in caplog.text


@pytest.mark.unit
def test_a_plain_http_webhook_is_refused_and_not_logged(monkeypatch, caplog) -> None:
    monkeypatch.setenv("SD_ALERT_WEBHOOK_URL", "http://hooks.example.invalid/T9/B9/plain")
    get_settings.cache_clear()
    with caplog.at_level(logging.DEBUG):
        assert check_alert_channel() is False
    assert "T9/B9" not in caplog.text and "LOGGED ONLY" in caplog.text


@pytest.mark.unit
def test_a_configured_channel_is_ready() -> None:
    assert check_alert_channel() is True


# ------------------------------------------------------------------ fix round 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "logger",
    [
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
    ],
)
def test_every_http_client_logger_drops_the_webhook(logger: str, caplog) -> None:
    # A logger-level filter does not see records propagated from child loggers (M1, round 1).
    with caplog.at_level(logging.DEBUG):
        logging.getLogger(logger).debug("send_request_headers.started url=%s", WEBHOOK)
    assert "T0/B0" not in caplog.text


@pytest.mark.unit
def test_a_refused_webhook_says_refused_not_unset(monkeypatch, caplog) -> None:
    monkeypatch.setenv("SD_ALERT_WEBHOOK_URL", "http://hooks.example.invalid/T9/B9/plain")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="selector_drift.quality.alerts"):
        assert check_alert_channel() is False
        notify([], _run("quarantined"), now=NOW)  # undeliverable: nothing touches the database
    assert "refused" in caplog.text and "https" in caplog.text
    assert "unset" not in caplog.text and "not set" not in caplog.text
    assert "ALERT NOT DELIVERED" in caplog.text and "T9/B9" not in caplog.text


def _scored(at: dt.datetime, subject: str = "7", judgement: str = "false_accept") -> None:
    with Session() as s:
        s.add(
            AuditLog(
                at=at,
                actor="testbench.score",
                action="scored",
                subject=subject,
                detail={"judgement": judgement, "run_key": KEY},
            )
        )
        s.commit()


@pytest.mark.integration
def test_an_old_false_accept_does_not_page_when_the_channel_is_configured(httpx_mock) -> None:
    # M3, round 1: configuring a webhook must not page every historical day at once.
    _scored(NOW - dt.timedelta(days=5))
    _scored(NOW - dt.timedelta(days=2, hours=1))  # before the start of yesterday (UTC)
    notify_false_accepts(now=NOW)
    assert httpx_mock.get_requests() == []


@pytest.mark.integration
def test_yesterdays_false_accept_still_pages(httpx_mock) -> None:
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    _scored(dt.datetime(2026, 9, 30, 0, 5, tzinfo=dt.UTC))
    notify_false_accepts(now=NOW)
    (request,) = httpx_mock.get_requests()
    assert json.loads(request.content)["day"] == "2026-09-30"


@pytest.mark.integration
def test_an_unconfigured_channel_does_not_rewarn_the_whole_history(monkeypatch, caplog) -> None:
    monkeypatch.delenv("SD_ALERT_WEBHOOK_URL")
    get_settings.cache_clear()
    for days in range(3, 30):
        _scored(NOW - dt.timedelta(days=days), subject=str(days))
    with caplog.at_level(logging.WARNING, logger="selector_drift.quality.alerts"):
        notify_false_accepts(now=NOW)
    assert "ALERT NOT DELIVERED" not in caplog.text


# M7, round 1: title and threshold are the only free text in the allow-listed body. Every rule
# notify() can send has its shape pinned here -- rule names, field names, counts, rates and
# numbers only -- so no cell value can ride into a webhook in either field.
FIELD = r"(claim_number|amount)"
NUM = r"[0-9][0-9.]*"
TITLE_SHAPES = {
    "row_count": rf"Row count \d+ is below half the trailing median \({NUM}\)",
    "null_rate": rf"{FIELD} is empty in \d+% of rows",
    "schema_drift": r"Landed columns differ from the spec's expected schema",
    "distribution_shift": rf"{FIELD} distribution shifted \(PSI {NUM}\)",
}
THRESHOLD_SHAPES = {
    "row_count": rf">= {NUM}",
    "null_rate": rf"<= {NUM}",
    "schema_drift": r"columns and types exactly as expected_schema \(spec v\d+\)",
    "distribution_shift": rf"PSI {NUM}",
}


@pytest.mark.unit
def test_every_contract_rule_has_a_pinned_shape() -> None:
    import re

    src = (Path(__file__).resolve().parents[2] / "selector_drift/quality/contracts.py").read_text(
        encoding="utf-8"
    )
    rules = set(re.findall(r'rule="(\w+)"', src))
    assert rules == set(TITLE_SHAPES) == set(THRESHOLD_SHAPES), "pin the new rule's shape here"


@pytest.mark.integration
def test_every_rule_notify_can_send_carries_only_its_pinned_shape(land, httpx_mock) -> None:
    import re

    from selector_drift.quality.contracts import evaluate
    from tests.land.factory import rows
    from tests.quality.conftest import BASE

    for i in range(6):
        land(BASE + dt.timedelta(days=i), rows())
    day = BASE + dt.timedelta(days=6)
    findings = evaluate(land(day, rows(49, scale=3.0, blank_amounts=5, extra={"surprise": "x"})))
    assert {f.properties["rule"] for f in findings} == set(TITLE_SHAPES)
    for f in findings:
        rule = f.properties["rule"]
        assert re.fullmatch(TITLE_SHAPES[rule], f.title), (rule, f.title)
        assert re.fullmatch(THRESHOLD_SHAPES[rule], f.properties["threshold"]), rule
    httpx_mock.add_response(url=WEBHOOK, method="POST", is_reusable=True)
    run = _run()
    run.run_key = f"testbench:2:{day.isoformat()}"
    notify(findings, run, now=NOW)
    bodies = [json.loads(r.content) for r in httpx_mock.get_requests()]
    assert bodies and {b["rule"] for b in bodies} <= set(TITLE_SHAPES)
    for b in bodies:
        assert re.fullmatch(TITLE_SHAPES[b["rule"]], b["title"])
        assert re.fullmatch(THRESHOLD_SHAPES[b["rule"]], b["threshold"])


@pytest.mark.integration
def test_the_internal_rules_titles_carry_no_free_text(httpx_mock) -> None:
    import re

    httpx_mock.add_response(url=WEBHOOK, method="POST", is_reusable=True)
    _scored(NOW, subject="AB12345678 <td>")  # a malformed subject must not reach the title
    notify_false_accepts(now=NOW)
    notify([], _run("quarantined"), now=NOW)
    bodies = {b["rule"]: b for b in (json.loads(r.content) for r in httpx_mock.get_requests())}
    assert re.fullmatch(
        r"the gate accepted a wrong heal \(heal (\d+|unknown)\)", bodies["false_accept"]["title"]
    )
    assert CELL not in json.dumps(bodies)
    assert bodies["quarantine"]["title"] == f"{KEY} quarantined: data held, nothing written"


@pytest.mark.unit
def test_paging_never_relies_on_severity_ordering() -> None:
    # Severity is a StrEnum with no ordering: the explicit PAGE set and rank dict carry the
    # policy, so every ladder member must be ranked and PAGE must be drawn from the ladder.
    from selector_drift.quality.alerts import _RANK, PAGE

    assert set(_RANK) == set(Severity)
    assert len(set(_RANK.values())) == len(Severity)
    assert PAGE.issubset(Severity)
    assert frozenset({Severity.CRITICAL, Severity.HIGH}) == PAGE
    assert all(_RANK[p] < _RANK[o] for p in PAGE for o in set(Severity) - PAGE)
