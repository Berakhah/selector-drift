import datetime as dt
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
from pathlib import Path

import pytest

from selector_drift.config import get_settings
from selector_drift.credentials import store as store_mod
from selector_drift.credentials.provider import Credentials, NotSupported
from selector_drift.credentials.store import AcknowledgementRequired, EncryptedStore, StoreError
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import Basis, ScopeRecord

# Generated at import: no credential-shaped literal is committed, and each run differs.
COOKIE = f"sid-test-only-{secrets.token_hex(8)}"
PASSWORD = f"pw-test-only-{secrets.token_hex(8)}"
USERNAME = "svc-user-test-only"


@pytest.fixture(autouse=True)
def kek(monkeypatch):
    key = os.urandom(32).hex()  # generated per test: no key material in the repo
    monkeypatch.setenv("SD_CREDENTIAL_KEK", key)
    get_settings.cache_clear()  # the KEK is a Settings field (config.py reads the environment)
    yield key
    get_settings.cache_clear()


@pytest.fixture
def ack(tmp_path):
    doc = tmp_path / "signed-acknowledgement.pdf"
    doc.write_bytes(b"client countersigned: stored credentials, last resort")
    return doc


def _store(root, client="client-a", ack_doc=None) -> EncryptedStore:
    s = EncryptedStore(client, root=root)
    if ack_doc is not None:
        s.record_acknowledgement(ack_doc, actor="op1")
    return s


def _creds() -> Credentials:
    return Credentials(
        username=USERNAME,
        password=PASSWORD,
        cookies=[{"name": "s", "value": COOKIE, "domain": "x.example.invalid"}],
    )


def _all_bytes(root) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


@pytest.mark.unit
def test_store_holds_no_plaintext_at_rest(tmp_path, target, ack):
    root = tmp_path / "store"
    _store(root, ack_doc=ack).put(target, _creds())
    blob = _all_bytes(root)
    for secret in (PASSWORD.encode(), USERNAME.encode(), COOKIE.encode()):
        assert secret not in blob


@pytest.mark.unit
def test_the_data_key_is_never_on_disk_unwrapped(tmp_path, target, ack, kek):
    s = _store(tmp_path / "store", ack_doc=ack)
    s.put(target, _creds())
    on_disk = _all_bytes(tmp_path / "store")
    assert s._data_key() not in on_disk
    assert bytes.fromhex(kek) not in on_disk and kek.encode() not in on_disk  # nor the KEK


@pytest.mark.unit
def test_each_client_gets_a_distinct_data_key(tmp_path):
    a = EncryptedStore("a", root=tmp_path)._data_key()
    b = EncryptedStore("b", root=tmp_path)._data_key()
    assert a != b and len(a) == len(b) == 32
    assert EncryptedStore("a", root=tmp_path)._data_key() == a  # stable per client


@pytest.mark.unit
def test_round_trip_recovers_the_credential(tmp_path, target, ack):
    s = _store(tmp_path / "store", ack_doc=ack)
    s.put(target, _creds())
    got = s.get(target)
    assert got.password is not None and got.password.get_secret_value() == PASSWORD
    assert got.username == USERNAME and got.cookies == _creds().cookies


@pytest.mark.unit
def test_nothing_is_stored_without_a_written_acknowledgement(tmp_path, target):
    with pytest.raises(AcknowledgementRequired):
        EncryptedStore("client-a", root=tmp_path / "store").put(target, _creds())
    assert not (tmp_path / "store").exists()  # refused before anything touched disk


@pytest.mark.unit
def test_nothing_is_read_without_a_written_acknowledgement(tmp_path, target, ack):
    root = tmp_path / "store"
    _store(root, ack_doc=ack).put(target, _creds())
    (root / "client-a" / "acknowledgement.json").unlink()
    with pytest.raises(AcknowledgementRequired):
        EncryptedStore("client-a", root=root).get(target)


@pytest.mark.unit
def test_the_acknowledgement_records_the_documents_sha256(tmp_path, ack):
    s = EncryptedStore("client-a", root=tmp_path / "store")
    digest = s.record_acknowledgement(ack, actor="op1")
    assert digest == hashlib.sha256(ack.read_bytes()).hexdigest()
    body = json.loads((tmp_path / "store" / "client-a" / "acknowledgement.json").read_text())
    assert body["sha256"] == digest and body["actor"] == "op1"
    assert body["document"] == ack.name


@pytest.mark.unit
@pytest.mark.parametrize(
    "body", ["", "not json", "[]", '{"sha256": "short"}', '{"actor": "op1"}', '{"sha256": 7}']
)
def test_a_malformed_acknowledgement_is_no_acknowledgement(tmp_path, target, body):
    d = tmp_path / "store" / "client-a"
    d.mkdir(parents=True)
    (d / "acknowledgement.json").write_text(body, encoding="utf-8")
    with pytest.raises(AcknowledgementRequired):
        EncryptedStore("client-a", root=tmp_path / "store").put(target, _creds())


@pytest.mark.unit
def test_an_empty_document_or_actor_is_not_an_acknowledgement(tmp_path, ack):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    s = EncryptedStore("client-a", root=tmp_path / "store")
    with pytest.raises(StoreError):
        s.record_acknowledgement(empty, actor="op1")
    with pytest.raises(StoreError):
        s.record_acknowledgement(ack, actor="  ")
    assert not (tmp_path / "store").exists()


@pytest.mark.unit
def test_a_blob_moved_to_another_client_does_not_decrypt(tmp_path, target, ack):
    root = tmp_path / "store"
    _store(root, "client-a", ack).put(target, _creds())
    other = _store(root, "client-b", ack)
    other._data_key()
    shutil.copy(root / "client-a" / "testbench.bin", root / "client-b" / "testbench.bin")
    with pytest.raises(StoreError, match="cannot be decrypted"):
        other.get(target)


def _other_target(name: str):
    """A second target, minted through the gate exactly as tests/credentials/conftest.py does."""
    rec = ScopeRecord(
        target=name,
        basis=Basis.SELF_OWNED,
        authorising_party="me",
        authorised_on=dt.date(2026, 1, 1),
        expires_on=dt.date(2027, 12, 31),
        evidence_uri=None,
        evidence_sha256=None,
        origins=["https://bench.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
    )
    return authorise(rec, today=dt.date(2026, 9, 7))


@pytest.mark.unit
def test_a_blob_renamed_to_another_target_does_not_decrypt(tmp_path, target, ack):
    root = tmp_path / "store"
    s = _store(root, "client-a", ack)
    s.put(_other_target("other"), _creds())
    shutil.copy(root / "client-a" / "other.bin", root / "client-a" / "testbench.bin")
    with pytest.raises(StoreError, match="cannot be decrypted"):
        s.get(target)
    assert s.get(_other_target("other")).username == USERNAME  # intact under its own name


@pytest.mark.unit
def test_the_wrong_kek_cannot_open_the_store(tmp_path, target, ack, monkeypatch):
    s = _store(tmp_path / "store", ack_doc=ack)
    s.put(target, _creds())
    monkeypatch.setenv("SD_CREDENTIAL_KEK", os.urandom(32).hex())
    get_settings.cache_clear()
    with pytest.raises(StoreError, match="cannot be decrypted"):
        s.get(target)


@pytest.mark.unit
def test_no_kek_no_store(tmp_path, monkeypatch):
    monkeypatch.delenv("SD_CREDENTIAL_KEK")
    get_settings.cache_clear()
    with pytest.raises(StoreError, match="SD_CREDENTIAL_KEK"):
        EncryptedStore("client-a", root=tmp_path)._data_key()
    assert not any(tmp_path.rglob("*.bin"))


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["zz" * 32, "ab" * 16, "ab" * 33])
def test_a_malformed_kek_is_refused_without_echoing_it(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("SD_CREDENTIAL_KEK", bad)
    get_settings.cache_clear()
    with pytest.raises(StoreError, match="SD_CREDENTIAL_KEK") as exc:
        EncryptedStore("client-a", root=tmp_path)._data_key()
    assert bad not in str(exc.value) and exc.value.__context__ is None


@pytest.mark.unit
def test_a_stored_credential_is_never_a_second_factor(tmp_path, target):
    with pytest.raises(NotSupported, match="TOTP seed"):
        EncryptedStore("client-a", root=tmp_path).get_totp_code(target)


@pytest.mark.unit
def test_the_store_logs_nothing_secret(tmp_path, target, ack, caplog, kek):
    with caplog.at_level(logging.DEBUG):
        s = _store(tmp_path / "store", ack_doc=ack)
        s.put(target, _creds())
        s.get(target)
    for secret in (PASSWORD, COOKIE, kek, s._data_key().hex()):
        assert secret not in caplog.text


@pytest.mark.unit
def test_the_repr_names_the_client_only(tmp_path, target, ack):
    s = _store(tmp_path / "store", ack_doc=ack)
    s.put(target, _creds())
    assert repr(s) == "EncryptedStore('client-a')"
    assert s._data_key().hex() not in repr(vars(s))


@pytest.mark.unit
@pytest.mark.parametrize("client", ["../x", "Client", "", "a/b", "-a"])
def test_a_client_id_cannot_escape_the_store(tmp_path, client):
    with pytest.raises(ValueError):
        EncryptedStore(client, root=tmp_path)


@pytest.mark.unit
def test_the_default_root_is_the_configured_credential_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SD_CREDENTIAL_STORE", str(tmp_path / "creds"))
    get_settings.cache_clear()
    EncryptedStore("client-a")._data_key()
    assert (tmp_path / "creds" / "client-a" / "datakey.bin").is_file()


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits; the product runs on Linux")
def test_every_file_is_owner_only(tmp_path, target, ack):
    root = tmp_path / "store"
    _store(root, ack_doc=ack).put(target, _creds())
    files = [p for p in root.rglob("*") if p.is_file()]
    assert {p.name for p in files} == {"acknowledgement.json", "datakey.bin", "testbench.bin"}
    for p in files:
        assert p.stat().st_mode & 0o077 == 0, p.name
    assert (root / "client-a").stat().st_mode & 0o077 == 0


@pytest.mark.unit
@pytest.mark.parametrize("name", ["datakey.bin", "testbench.bin"])
def test_the_permission_check_runs_whenever_os_name_is_posix(
    tmp_path, target, ack, monkeypatch, name
):
    """The gate itself, exercised on every host (as Task 31 does for session files): with
    os.name reporting "posix" and a key or credential file reporting group/other read, the
    store refuses to open it -- the check never silently passes on POSIX."""
    root = tmp_path / "store"
    s = _store(root, ack_doc=ack)
    s.put(target, _creds())
    victim = root / "client-a" / name
    real_stat = Path.stat

    def stat_0644(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        if self == victim:
            return os.stat_result((0o100644, *tuple(st)[1:]))
        return os.stat_result((0o100600, *tuple(st)[1:])) if stat.S_ISREG(st.st_mode) else st

    monkeypatch.setattr(store_mod.os, "name", "posix")
    monkeypatch.setattr(Path, "stat", stat_0644)
    with pytest.raises(StoreError, match="chmod 600"):
        s.get(target)


@pytest.mark.unit
def test_the_permission_check_passes_owner_only_files_under_posix(
    tmp_path, target, ack, monkeypatch
):
    """The other half of the forced-posix test: 0600 files open normally."""
    root = tmp_path / "store"
    s = _store(root, ack_doc=ack)
    s.put(target, _creds())
    real_stat = Path.stat

    def stat_0600(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        return os.stat_result((0o100600, *tuple(st)[1:])) if stat.S_ISREG(st.st_mode) else st

    monkeypatch.setattr(store_mod.os, "name", "posix")
    monkeypatch.setattr(Path, "stat", stat_0600)
    assert s.get(target).username == USERNAME
