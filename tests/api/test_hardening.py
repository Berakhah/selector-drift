import dataclasses
import secrets

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
from tests.api.conftest import LOGIN, latest_audit


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


@pytest.mark.integration
def test_failures_for_unknown_usernames_write_no_audit_rows_but_are_counted(
    anon_client, operator, caplog
):
    from selector_drift.api.auth import unknown_user_failures

    before = unknown_user_failures()
    for i in range(8):
        r = anon_client.post(LOGIN, json={"username": f"ghost{i}", "password": "wrong-guess"})
        assert r.status_code == 401
    with Session() as s:
        n = s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "login_failure")
        )
    assert n == 0
    assert unknown_user_failures() == before + 8
    assert "ghost" not in caplog.text  # the typed name is attacker-controlled input


@pytest.mark.integration
def test_a_wrong_password_for_a_real_operator_is_still_audited(anon_client, operator):
    anon_client.post(LOGIN, json={"username": "op1", "password": "wrong-guess"})
    assert latest_audit("login_failure").subject == "operator:op1"


@pytest.mark.integration
def test_unlock_clears_the_throttle_and_leaves_the_failure_history(anon_client, operator):
    from selector_drift.api.auth import unlock_operator

    for _ in range(5):
        anon_client.post(LOGIN, json={"username": "op1", "password": "wrong-guess"})
    right = {"username": "op1", "password": operator[1]}
    assert anon_client.post(LOGIN, json=right).status_code == 429
    with Session() as s:
        unlock_operator(s, "op1")
        s.commit()
    assert anon_client.post(LOGIN, json=right).status_code == 200
    with Session() as s:
        failures = s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "login_failure")
        )
        unlock = s.scalars(select(AuditLog).where(AuditLog.action == "login_unlock")).one()
    assert failures == 5  # evidence kept
    assert unlock.actor == "system:unlock-operator" and unlock.subject == "operator:op1"


@pytest.mark.integration
def test_failures_after_an_unlock_count_again(anon_client, operator):
    from selector_drift.api.auth import unlock_operator

    with Session() as s:
        unlock_operator(s, "op1")
        s.commit()
    for _ in range(5):
        assert (
            anon_client.post(LOGIN, json={"username": "op1", "password": "wrong-guess"}).status_code
            == 401
        )
    r = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    assert r.status_code == 429


@pytest.mark.integration
def test_unlocking_an_unknown_operator_is_refused(clean_db):
    from selector_drift.api.auth import unlock_operator
    from selector_drift.api.errors import Invalid

    with Session() as s, pytest.raises(Invalid):
        unlock_operator(s, "nobody")


@pytest.mark.integration
def test_unlock_operator_cli_reports_and_audits(operator, capsys):
    from selector_drift.__main__ import main

    assert main(["unlock-operator", "op1"]) == 0
    assert "op1" in capsys.readouterr().out
    assert latest_audit("login_unlock").subject == "operator:op1"
    assert main(["unlock-operator", "nobody"]) == 2


def _bad(client, name):
    return client.post(LOGIN, json={"username": name, "password": "wrong-guess"})


@pytest.mark.integration
def test_unknown_usernames_are_throttled_exactly_like_real_ones(anon_client, operator):
    ghost = "ghost-" + secrets.token_hex(4)
    real = [_bad(anon_client, "op1") for _ in range(6)]
    unknown = [_bad(anon_client, ghost) for _ in range(6)]
    assert [r.status_code for r in unknown] == [r.status_code for r in real]
    assert [r.status_code for r in unknown] == [401] * 5 + [429]
    assert unknown[5].json()["type"] == real[5].json()["type"]
    assert unknown[5].json()["type"].endswith("throttled")
    assert unknown[5].json()["detail"].split(" in ")[0] == real[5].json()["detail"].split(" in ")[0]
    assert 0 < int(unknown[5].headers["Retry-After"]) <= 900


@pytest.mark.integration
def test_throttled_unknown_names_write_no_audit_rows_and_are_never_logged(
    anon_client, operator, caplog
):
    ghost = "ghost-" + secrets.token_hex(4)
    for _ in range(7):
        _bad(anon_client, ghost)
    with Session() as s:
        n = s.scalar(select(func.count()).select_from(AuditLog))
    assert n == 1  # only the operator_created row
    assert ghost not in caplog.text


@pytest.mark.unit
def test_unknown_throttle_keys_are_bounded(monkeypatch):
    from datetime import UTC, datetime

    from selector_drift.api import auth

    monkeypatch.setattr(auth, "UNKNOWN_KEYS_CAP", 3)
    now = datetime.now(UTC)
    for i in range(10):
        auth._unknown_record(f"name{i}", now)
        assert len(auth._unknown_hits) <= 3
    assert auth._unknown_wait("name9", now) is None  # newest kept, no throttle at 1 failure


@pytest.mark.unit
def test_unknown_throttle_prunes_entries_older_than_the_window():
    from datetime import UTC, datetime, timedelta

    from selector_drift.api import auth

    name = "old-" + secrets.token_hex(4)
    t0 = datetime.now(UTC) - auth.THROTTLE_WINDOW - timedelta(minutes=1)
    for _ in range(auth.THROTTLE_LIMIT):
        auth._unknown_record(name, t0)
    assert auth._unknown_wait(name, t0) is not None
    assert auth._unknown_wait(name, datetime.now(UTC)) is None


@pytest.mark.integration
def test_a_future_dated_unlock_cannot_hide_new_failures(anon_client, operator):
    from datetime import UTC, datetime, timedelta

    with Session() as s:
        s.add(
            AuditLog(
                at=datetime.now(UTC) + timedelta(hours=1),
                actor="system:unlock-operator",
                action="login_unlock",
                subject="operator:op1",
                detail={},
            )
        )
        s.commit()
    for _ in range(5):
        assert _bad(anon_client, "op1").status_code == 401
    r = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    assert r.status_code == 429


@pytest.mark.integration
def test_the_advisory_lock_is_released_on_the_unknown_user_path(anon_client, operator):
    ghost = "ghost-" + secrets.token_hex(4)
    assert _bad(anon_client, ghost).status_code == 401
    second = _bad(anon_client, ghost)
    assert second.status_code == 401  # not 429 "in progress"


@pytest.mark.integration
def test_a_disabled_operators_failures_are_still_audited(anon_client, operator):
    from datetime import UTC, datetime

    with Session() as s:
        s.scalars(select(Operator)).one().disabled_at = datetime.now(UTC)
        s.commit()
    r = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    assert r.status_code == 401
    assert latest_audit("login_failure").subject == "operator:op1"


@pytest.mark.unit
def test_unlock_operator_cli_rejects_a_bad_name(capsys):
    from selector_drift.__main__ import main

    assert main(["unlock-operator", "BAD NAME"]) == 2


@pytest.mark.integration
def test_retry_after_stays_positive_after_an_unlock_and_five_new_failures(anon_client, operator):
    from selector_drift.api.auth import unlock_operator

    for _ in range(5):
        _bad(anon_client, "op1")
    with Session() as s:
        unlock_operator(s, "op1")
        s.commit()
    for _ in range(5):
        assert _bad(anon_client, "op1").status_code == 401
    r = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    assert r.status_code == 429
    assert 0 < int(r.headers["Retry-After"]) <= 900
