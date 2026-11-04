"""The browser must not inherit the worker's secrets.

Chromium renders hostile HTML and runs with --no-sandbox inside the worker container (compose
`worker` comment). Playwright launches it with the parent's whole environment unless `env=` is
given, so a renderer compromise would otherwise read the database password, the redaction key and
any credential KEK from /proc/<pid>/environ. The browser gets an allowlist and nothing else.
"""

import datetime as dt

import pytest

from selector_drift import browser
from selector_drift.browser import driver
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import Basis, ScopeRecord

_SECRETS = {
    "SD_DATABASE_URL": "postgresql+psycopg://u:hunter2@postgres/db",
    "SD_REDACTION_KEY": "redaction-key-canary",
    "SD_CREDENTIAL_KEK": "kek-canary",
    "SD_GITHUB_TOKEN": "ghp_canary",
    "SD_SCORER_PASSWORD": "scorer-canary",
    "POSTGRES_PASSWORD": "pg-canary",
    "AWS_SECRET_ACCESS_KEY": "aws-canary",
}


@pytest.mark.unit
def test_the_browser_environment_carries_no_secret(monkeypatch):
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin")

    env = driver._browser_env()

    assert env["PATH"] == "/usr/bin"  # what Chromium genuinely needs still gets through
    leaked = {k for k in env if k in _SECRETS}
    assert not leaked, leaked
    assert not [v for v in env.values() if any(s in v for s in _SECRETS.values())], env


@pytest.mark.unit
def test_an_unlisted_variable_is_dropped_by_default(monkeypatch):
    """An allowlist, not a denylist: a secret nobody has thought of yet is excluded too."""
    monkeypatch.setenv("SD_SOME_FUTURE_SECRET", "x")
    assert "SD_SOME_FUTURE_SECRET" not in driver._browser_env()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_launches_chromium_with_the_filtered_environment(monkeypatch):
    today = dt.datetime.now(dt.UTC).date()
    target = authorise(
        ScopeRecord(
            target="driver-fixture",
            basis=Basis.SELF_OWNED,
            authorising_party="selector-drift test suite",
            authorised_on=today - dt.timedelta(days=1),
            expires_on=today + dt.timedelta(days=30),
            evidence_uri=None,
            evidence_sha256=None,
            origins=["http://127.0.0.1:1"],
            rate_limit={"requests": 1, "per_seconds": 30},
            user_agent_contact="https://example.invalid/contact",
            first_heal_policy="required",
            legal_review="self_drafted",
        ),
        today=today,
    )
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda _t: "http://127.0.0.1:1/")
    monkeypatch.setenv("SD_REDACTION_KEY", _SECRETS["SD_REDACTION_KEY"])
    launched: dict[str, object] = {}

    class _Stop(Exception):
        pass

    class _Chromium:
        async def launch(self, **kwargs: object) -> None:
            launched.update(kwargs)
            raise _Stop

    class _Playwright:
        chromium = _Chromium()

    class _Manager:
        async def __aenter__(self) -> _Playwright:
            return _Playwright()

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr("selector_drift.browser.driver.async_playwright", lambda: _Manager())

    with pytest.raises(_Stop):
        async with browser.driver.open(target):
            pass

    env = launched.get("env")
    assert isinstance(env, dict), "launch() was not given an explicit env, so Chromium inherits all"
    assert "SD_REDACTION_KEY" not in env
