from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

import pytest
from starlette.testclient import TestClient

from testbench.portal.render import render_results
from testbench.portal.settings import Settings
from tests.testbench.helpers import login, rows, sign_override

NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)
Factory = Callable[..., TestClient]


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/", "/search", "/results", "/claims/AB12345678", "/export.csv"])
def test_unauthenticated_requests_redirect_to_login(client_factory: Factory, path: str) -> None:
    response = client_factory().get(path)
    assert response.status_code == 302 and response.headers["location"] == "/login"


@pytest.mark.unit
def test_login_and_healthz_are_public(client_factory: Factory) -> None:
    client = client_factory()
    assert client.get("/login").status_code == 200
    health = client.get("/healthz")
    assert health.status_code == 200 and health.text == "ok"


@pytest.mark.unit
def test_wrong_password_is_refused(client_factory: Factory) -> None:
    client = client_factory()
    response = client.post("/login", data={"username": "demo", "password": "nope"})
    assert response.status_code == 401 and "Unknown username or password" in response.text
    assert client.get("/results").status_code == 302


@pytest.mark.unit
def test_results_render_the_request_days_dataset(client_factory: Factory) -> None:
    client = client_factory(clock=lambda: NOW)
    login(client)
    response = client.get("/results")
    assert response.status_code == 200
    assert rows(response.text) == rows(render_results(NOW.date()))
    assert "data-sd-truth" not in response.text


@pytest.mark.unit
def test_the_served_day_is_utc_not_local(client_factory: Factory) -> None:
    utc_minus_5 = dt.timezone(dt.timedelta(hours=-5))
    client = client_factory(clock=lambda: dt.datetime(2026, 9, 9, 23, 30, tzinfo=utc_minus_5))
    login(client)
    served = rows(client.get("/results").text)
    assert served == rows(render_results(dt.date(2026, 9, 10)))
    assert served != rows(render_results(dt.date(2026, 9, 9)))


@pytest.mark.unit
def test_a_signed_override_steers_the_served_dataset(
    client_factory: Factory, override_secret: str
) -> None:
    client = client_factory(clock=lambda: NOW)
    login(client)
    other = dt.date(2026, 9, 1)
    signed = client.get("/results", headers=sign_override(other, override_secret))
    unsigned = client.get("/results", headers={"X-Testbench-Date": other.isoformat()})
    assert rows(signed.text) == rows(render_results(other))
    assert rows(unsigned.text) == rows(render_results(NOW.date()))


@pytest.mark.unit
def test_an_unknown_claim_is_an_html_404(client_factory: Factory) -> None:
    client = client_factory(clock=lambda: NOW)
    login(client)
    response = client.get("/claims/not-a-claim")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.unit
def test_the_access_log_records_the_user_agent(
    client_factory: Factory, caplog: pytest.LogCaptureFixture
) -> None:
    """Brief §3.3: the runner identifies itself with a contact URL. The bench is where that
    claim is checked, so the bench must record it (Task 12 proves it end to end)."""
    caplog.set_level(logging.INFO, logger="testbench.access")
    agent = "selector-drift/1 (+https://example.invalid/contact)"
    client_factory().get("/healthz", headers={"User-Agent": agent})
    assert any(
        getattr(r, "user_agent", None) == agent and getattr(r, "http_path", None) == "/healthz"
        for r in caplog.records
        if r.name == "testbench.access"
    )


@pytest.mark.unit
def test_without_a_configured_secret_each_process_gets_a_random_one() -> None:
    """`docker compose up` from `.env.example` must boot, and no constant may stand in for a
    secret: an unset session secret becomes a per-process random one; an unset override secret
    switches the override off."""
    a, b = Settings.from_env({}), Settings.from_env({})
    assert a.session_secret != b.session_secret and len(a.session_secret) >= 64
    assert a.override_secret is None and a.cookie_secure is True
