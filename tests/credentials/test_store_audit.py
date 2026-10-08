"""The acknowledgement reaches audit_log for real (fix round 1): "the acknowledgement that was in
force is checkable later" holds even if the store's own directory is lost."""

import os
import secrets

import pytest
from sqlalchemy import select

from selector_drift.config import get_settings
from selector_drift.credentials.store import EncryptedStore
from selector_drift.db.models import AuditLog
from selector_drift.db.session import Session


@pytest.fixture(autouse=True)
def kek(monkeypatch):
    monkeypatch.setenv("SD_CREDENTIAL_KEK", os.urandom(32).hex())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.integration
def test_each_acknowledgement_is_written_to_audit_log(tmp_path):
    client = f"client-{secrets.token_hex(4)}"  # unique per run: the test database persists
    doc = tmp_path / "ack.pdf"
    doc.write_bytes(b"client countersigned: stored credentials, last resort")
    s = EncryptedStore(client, root=tmp_path / "store")
    first = s.record_acknowledgement(doc, actor="op1")
    doc.write_bytes(b"client countersigned again")
    second = s.record_acknowledgement(doc, actor="op2")
    with Session() as db:
        rows = (
            db.execute(
                select(AuditLog)
                .where(AuditLog.action == "credential_acknowledged", AuditLog.subject == client)
                .order_by(AuditLog.id)
            )
            .scalars()
            .all()
        )
    assert [(r.actor, r.detail["sha256"]) for r in rows] == [("op1", first), ("op2", second)]
    assert all(r.detail["document"] == "ack.pdf" for r in rows)
