from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from selector_drift.api.auth import create_operator
from selector_drift.api.main import create_app
from selector_drift.config import Settings
from selector_drift.db.models import AuditLog
from selector_drift.db.session import Session, engine

WEB_ORIGIN = "https://sd.example.invalid"
LOGIN = "/api/v1/auth/login"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return dataclasses.replace(
        Settings.for_typegen(),
        env="development",
        web_origin=WEB_ORIGIN,
        evidence_root=tmp_path / "evidence",
        fixture_evidence_root=tmp_path / "fixtures",
        lake_root=tmp_path / "lake",
    )


@pytest.fixture
def clean_db() -> Iterator[None]:
    with engine.begin() as c:
        c.execute(text("TRUNCATE operators, sessions, audit_log RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def app(settings, clean_db) -> FastAPI:
    return create_app(settings=settings)


@pytest.fixture
def operator(clean_db) -> tuple[str, str]:
    password = secrets.token_urlsafe(18)  # per-run, never committed
    with Session() as s:
        create_operator(s, "op1", password)
        s.commit()
    return "op1", password


def make_client(app: FastAPI) -> TestClient:
    # https: the session cookie is Secure, and an http:// client would never send it back.
    return TestClient(app, base_url="https://testserver", headers={"Origin": WEB_ORIGIN})


@pytest.fixture
def anon_client(app) -> TestClient:
    return make_client(app)


@pytest.fixture
def client(app, operator) -> TestClient:
    c = make_client(app)
    r = c.post(LOGIN, json={"username": operator[0], "password": operator[1]})
    assert r.status_code == 200, r.text
    c.session_user = operator[0]  # type: ignore[attr-defined]
    return c


def latest_audit(action: str | None = None) -> AuditLog:
    with Session() as s:
        q = select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
        if action is not None:
            q = q.where(AuditLog.action == action)
        row = s.scalars(q).one()
        s.expunge(row)
        return row
