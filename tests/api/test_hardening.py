import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from selector_drift.api.auth import create_operator
from selector_drift.api.errors import install_problem_handlers
from selector_drift.api.main import create_app
from selector_drift.config import ConfigError, Settings
from selector_drift.db.models import AuditLog, Operator
from selector_drift.db.session import Session
from tests.api.conftest import LOGIN


@pytest.mark.integration
def test_a_non_404_http_error_keeps_its_real_status_title_and_headers(anon_client):
    r = anon_client.get(LOGIN)  # login is POST-only
    assert r.status_code == 405
    body = r.json()
    assert body["status"] == 405 and body["type"].endswith("http-error")
    assert body["title"] == "Method Not Allowed"
    assert "POST" in r.headers["allow"]
    assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.unit
def test_an_unhandled_exception_is_a_generic_500_problem_without_the_message():
    app = FastAPI()
    app.state.settings = Settings.for_typegen()
    install_problem_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret-internal-detail $argon2id$")

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"].endswith("internal") and r.json()["status"] == 500
    assert "secret-internal-detail" not in r.text and "argon2" not in r.text


def _fake_tty(monkeypatch, getpass):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", getpass)


@pytest.mark.unit
def test_create_operator_rejects_a_bad_username_before_prompting(monkeypatch, capsys):
    from selector_drift.__main__ import main

    def no_prompt(_: str) -> str:
        raise AssertionError("prompted for a password")

    _fake_tty(monkeypatch, no_prompt)
    assert main(["create-operator", "Bad Name!"]) == 2
    assert "username" in capsys.readouterr().err


@pytest.mark.integration
def test_create_operator_duplicate_is_one_clean_line(clean_db, monkeypatch, capsys):
    from selector_drift.__main__ import main

    with Session() as s:
        create_operator(s, "dup1", "a-long-enough-generated-pw")
        s.commit()
    _fake_tty(monkeypatch, lambda _: "another-long-enough-pw")
    assert main(["create-operator", "dup1"]) == 2
    err = capsys.readouterr().err
    assert "operator dup1 already exists" in err and "$argon2" not in err
    with Session() as s:
        assert len(s.scalars(select(Operator)).all()) == 1


def _prod(origin: str) -> Settings:
    return dataclasses.replace(
        Settings.for_typegen(), env="production", fixture_mode=False, web_origin=origin
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "origin",
    [
        "http://sd.example.com",
        "https://sd.example.com/app",
        "https://sd.example.com?x=1",
        "https://sd.example.com#f",
        "https://localhost",
        "https://127.0.0.1:3000",
        "https://[::1]",
        "sd.example.com",
    ],
)
def test_production_refuses_a_bad_web_origin(origin):
    with pytest.raises(ConfigError, match="SD_WEB_ORIGIN"):
        create_app(settings=_prod(origin))


@pytest.mark.integration
def test_production_normalises_the_web_origin_for_the_guard(clean_db):
    app = create_app(settings=_prod("https://SD.Example.com:443"))
    c = TestClient(app, base_url="https://testserver")
    ok = c.post("/api/v1/auth/logout", headers={"Origin": "https://sd.example.com"})
    assert ok.status_code == 401  # past the guard, refused for want of a session
    bad = c.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example.com"})
    assert bad.status_code == 403


@pytest.mark.integration
def test_a_full_verify_cap_is_refused_at_once_with_429(anon_client, operator, monkeypatch):
    import threading

    from selector_drift.api import auth

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(auth, "_verify_slots", slots)
    assert slots.acquire(blocking=False)  # every slot is busy
    try:
        r = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    finally:
        slots.release()
    assert r.status_code == 429 and r.json()["type"].endswith("throttled")
    assert "busy" in r.json()["detail"] and r.headers["Retry-After"] == "1"
    # a refused attempt is not a guess: it must not count toward the username's 5
    with Session() as s:
        n = s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "login_failure")
        )
    assert n == 0
    ok = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    assert ok.status_code == 200
