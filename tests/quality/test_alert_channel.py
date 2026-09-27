from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from selector_drift.config import get_settings
from selector_drift.quality.alerts import WEBHOOK_ENV, post_alert, sanitize_for_alert

HOOK = "https://hooks.example.invalid/sd"


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    """The webhook is read through Settings (Task 5), which is cached per process."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.unit
def test_an_unset_webhook_is_loud_and_sends_nothing(monkeypatch, caplog) -> None:
    monkeypatch.delenv(WEBHOOK_ENV, raising=False)
    assert post_alert("failed", "testbench", "testbench:1:2026-09-07", "boom") is False
    assert "ALERT NOT SENT" in caplog.text


@pytest.mark.unit
def test_an_alert_posts_one_json_body_and_nothing_from_the_page(monkeypatch, httpx_mock) -> None:
    monkeypatch.setenv(WEBHOOK_ENV, HOOK)
    httpx_mock.add_response(url=HOOK, method="POST")
    assert post_alert("refused", "testbench", "testbench:1:2026-09-07", "expired") is True
    (request,) = httpx_mock.get_requests()
    body = json.loads(request.content)
    assert set(body) == {"kind", "target", "run_key", "summary", "at"}
    assert (body["kind"], body["target"], body["summary"]) == ("refused", "testbench", "expired")


@pytest.mark.unit
def test_a_failing_webhook_never_fails_the_run(monkeypatch, httpx_mock) -> None:
    monkeypatch.setenv(WEBHOOK_ENV, HOOK)
    httpx_mock.add_response(url=HOOK, method="POST", status_code=500)
    assert post_alert("failed", "testbench", None, "boom") is False


@pytest.mark.unit
def test_post_alert_never_raises_even_on_an_url_httpx_cannot_construct(monkeypatch, caplog) -> None:
    """M-1 (final review): post_alert's docstring promises "Never raises", but the old code
    caught only httpx.HTTPError. httpx.InvalidURL is a plain Exception subclass, raised by
    httpx.post() itself (before any request is sent) for a URL like this one -- a non-numeric
    port. A typo'd SD_ALERT_WEBHOOK_URL must not turn a refusal or failure into a crash."""
    monkeypatch.setenv(WEBHOOK_ENV, "http://x.example.invalid:abc/")
    assert post_alert("failed", "testbench", None, "boom") is False
    assert "alert webhook failed" in caplog.text
    # The log line must not leak the webhook URL's own (secret) path/query.
    assert "x.example.invalid" not in caplog.text


@pytest.mark.unit
def test_sanitize_for_alert_strips_page_content_and_keeps_the_exception_type() -> None:
    """I-2 (final review): the alert body must carry only the exception type and a sanitised
    first line, never the DOM content Playwright's own "Call log:" section quotes."""
    exc = RuntimeError(
        "Timeout 5000ms exceeded.\n"
        "Call log:\n"
        "  - locator resolved to <td>AB12345678</td>\n"
        "  - waiting for element to be visible"
    )
    summary = sanitize_for_alert(exc)
    assert "RuntimeError" in summary
    assert "<td" not in summary
    assert "AB12345678" not in summary
    assert "Call log" not in summary


@pytest.mark.unit
def test_sanitize_for_alert_strips_markup_even_on_the_first_line() -> None:
    exc = ValueError("bad cell <td class='x'>AB12345678</td> found")
    summary = sanitize_for_alert(exc)
    assert "<td" not in summary
    assert "AB12345678" not in summary
