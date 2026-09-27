from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from selector_drift.config import get_settings
from selector_drift.quality.alerts import WEBHOOK_ENV, post_alert

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
