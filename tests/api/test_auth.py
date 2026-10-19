import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher
from sqlalchemy import select

from selector_drift.api.auth import create_operator
from selector_drift.api.main import create_app
from selector_drift.db.models import Operator, OperatorSession
from selector_drift.db.session import Session
from tests.api.conftest import LOGIN, latest_audit, make_client

ME, LOGOUT = "/api/v1/auth/me", "/api/v1/auth/logout"


@pytest.mark.integration
def test_login_sets_an_httponly_strict_cookie_and_stores_only_its_hash(anon_client, operator):
    r = anon_client.post(LOGIN, json={"username": operator[0], "password": operator[1]})
    assert r.status_code == 200 and r.json() == {"username": "op1"}
    cookie = r.headers["set-cookie"].lower()
    for attr in ("sd_session=", "httponly", "secure", "samesite=strict", "path=/"):
        assert attr in cookie
    raw = anon_client.cookies["sd_session"]
    with Session() as s:
        row = s.scalars(select(OperatorSession)).one()
        assert row.token_sha256 == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in {str(getattr(row, c.key)) for c in OperatorSession.__table__.columns}
        assert row.expires_at - row.created_at == timedelta(hours=12)
    assert latest_audit().action == "login_success"


@pytest.mark.integration
def test_a_wrong_password_is_401_problem_json_and_audited(anon_client, operator):
    r = anon_client.post(LOGIN, json={"username": "op1", "password": "not-it"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"].endswith("unauthenticated")
    audit = latest_audit()
    assert audit.action == "login_failure" and audit.subject == "operator:op1"


@pytest.mark.integration
def test_me_is_401_without_a_session(anon_client):
    r = anon_client.get(ME)
    assert r.status_code == 401
    assert set(r.json()) >= {"type", "title", "status", "detail"}


@pytest.mark.integration
def test_logout_revokes_the_session_row(client):
    assert client.post(LOGOUT).status_code == 204
    assert client.get(ME).status_code == 401
    with Session() as s:
        assert s.scalars(select(OperatorSession)).one().revoked_at is not None
    assert latest_audit().action == "logout"


@pytest.mark.integration
def test_an_expired_session_is_401(client):
    with Session() as s:
        row = s.scalars(select(OperatorSession)).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    assert client.get(ME).status_code == 401


@pytest.mark.integration
def test_a_non_get_with_a_foreign_origin_is_refused(client, operator):
    r = client.post(LOGOUT, headers={"Origin": "https://evil.example.invalid"})
    assert r.status_code == 403 and r.json()["type"].endswith("bad-origin")
    assert client.get(ME).status_code == 200  # nothing happened
    r = client.post(
        LOGIN, json={"username": "op1", "password": operator[1]}, headers={"Origin": "null"}
    )
    assert r.status_code == 403


@pytest.mark.integration
def test_a_non_get_with_no_origin_is_refused(app, operator):
    from fastapi.testclient import TestClient

    bare = TestClient(app, base_url="https://testserver")
    r = bare.post(LOGIN, json={"username": "op1", "password": operator[1]})
    assert r.status_code == 403


@pytest.mark.integration
def test_six_failed_logins_throttle_and_survive_a_restart(settings, operator):
    c = make_client(create_app(settings=settings))
    for _ in range(5):
        assert c.post(LOGIN, json={"username": "op1", "password": "wrong"}).status_code == 401
    right = {"username": "op1", "password": operator[1]}
    r = c.post(LOGIN, json=right)  # the sixth attempt, with the RIGHT password
    assert r.status_code == 429 and r.json()["type"].endswith("throttled")
    assert 0 < int(r.headers["Retry-After"]) <= 15 * 60
    restarted = make_client(create_app(settings=settings))  # counted in the DB, not memory
    assert restarted.post(LOGIN, json=right).status_code == 429


@pytest.mark.integration
def test_a_disabled_operator_cannot_log_in(anon_client, operator):
    with Session() as s:
        s.scalars(select(Operator)).one().disabled_at = datetime.now(UTC)
        s.commit()
    r = anon_client.post(LOGIN, json={"username": "op1", "password": operator[1]})
    assert r.status_code == 401


@pytest.mark.integration
def test_validation_errors_never_echo_the_password(anon_client):
    r = anon_client.post(LOGIN, json={"username": "NOT VALID!", "password": "p4ss-secret-xyz"})
    assert r.status_code == 422 and r.json()["type"].endswith("invalid")
    assert "p4ss-secret-xyz" not in r.text


@pytest.mark.integration
def test_create_operator_stores_an_argon2id_hash(clean_db):
    with Session() as s:
        op = create_operator(s, "op2", "correct horse battery staple")
        s.commit()
        assert op.password_hash.startswith("$argon2id$")
        assert "correct horse" not in op.password_hash
        assert PasswordHasher().verify(op.password_hash, "correct horse battery staple")


@pytest.mark.unit
def test_create_operator_refuses_to_read_a_password_without_a_tty(monkeypatch, capsys):
    from selector_drift.__main__ import main

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["create-operator", "op9"]) == 2
    assert "terminal" in capsys.readouterr().err
