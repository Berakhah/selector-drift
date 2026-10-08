"""Stored credentials -- the last resort (design 2.1 Q2; brief rule 4), with a written
acknowledgement. `put()` and `get()` refuse until the client's signed acknowledgement is on file
by its SHA-256 -- the same integrity move as the scope record's `evidence_sha256`, so "they
agreed to this" is checkable, not remembered.

The acknowledgements are an append-only history (`acknowledgements.bin`), sealed with AES-GCM
under the client's data key with AAD `ack|<client>`: a forged or edited file -- written by
anyone without the KEK -- fails closed. Each recording is also written to `audit_log` before it
is added to the history, so which acknowledgement was in force stays checkable later even if
the store's directory is lost.

Envelope encryption: a key-encryption key (KEK, `Settings.credential_kek`, from
SD_CREDENTIAL_KEK, never on disk) wraps a per-client AES-256 data key (`datakey.bin`, AAD
`datakey|<client>`); the data key seals each target's credential with AES-GCM under AAD
`<client>|<target>`, so a blob copied to another client, or renamed to another target, fails
authentication instead of decrypting. The design supports KEK rotation -- only `datakey.bin`
is wrapped by the KEK, so rotation re-wraps one small file per client -- but no rotation tool
exists yet.

Files are written through an exclusive, randomly named, fsync'd temp file: replaced atomically
(`os.replace`), or -- for the data key -- linked into place as an atomic create-if-absent, so a
crash mid-write never leaves a truncated key. Mode 0600 (directories 0700); on POSIX a key,
credential or acknowledgement file readable by group or others is refused, as Task 31 does for
session files. Stored cookies pass the same H2 filter as every provider. Nothing here logs,
reprs or raises a key, a password, a cookie value or a username.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from selector_drift.config import get_settings
from selector_drift.credentials.provider import (
    Credentials,
    CredentialsUnavailable,
    NotSupported,
    select_cookies,
    utc_now,
)
from selector_drift.scope.record import AuthorisedTarget

log = logging.getLogger(__name__)

KEK_ENV: Final = "SD_CREDENTIAL_KEK"  # named in messages only; config.py reads it
AUDIT_ACTION: Final = "credential_acknowledged"
_KEY_BYTES: Final = 32
_MAGIC: Final = b"SDC1"
_NONCE: Final = 12
_TAG: Final = 16
_ACKS: Final = "acknowledgements.bin"
_DATAKEY: Final = "datakey.bin"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GROUP_OR_OTHER = stat.S_IRWXG | stat.S_IRWXO
_TEMP_FLAGS: Final = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)  # POSIX: never write through a planted symlink
    | getattr(os, "O_BINARY", 0)  # Windows: no newline translation
)


class StoreError(Exception):
    """The store cannot be opened or written. Never carries key or credential material."""


class AcknowledgementRequired(StoreError):
    """No verifiable written acknowledgement on file for this client."""


def _kek() -> bytes:
    secret = get_settings().credential_kek
    if secret is None:
        raise StoreError(f"{KEK_ENV} is not set; stored credentials cannot be opened")
    key: bytes | None = None
    # Raised below, outside the handler: ValueError's text may quote the input.
    with contextlib.suppress(ValueError):
        key = bytes.fromhex(secret.get_secret_value())
    if key is None:
        raise StoreError(f"{KEK_ENV} is not hex")
    if len(key) != _KEY_BYTES:
        raise StoreError(f"{KEK_ENV} must be {_KEY_BYTES} bytes ({2 * _KEY_BYTES} hex characters)")
    return key


def _seal(plaintext: bytes, key: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    return _MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _unseal(blob: bytes, key: bytes, aad: bytes, what: str) -> bytes:
    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + _NONCE + _TAG:
        raise StoreError(f"{what} is not a sealed credential file")
    nonce, sealed = blob[len(_MAGIC) : len(_MAGIC) + _NONCE], blob[len(_MAGIC) + _NONCE :]
    plaintext: bytes | None = None
    with contextlib.suppress(InvalidTag):
        plaintext = AESGCM(key).decrypt(nonce, sealed, aad)
    if plaintext is None:
        raise StoreError(
            f"{what} cannot be decrypted: wrong key, edited, or moved from another client or target"
        )
    return plaintext


def _read_owner_only(path: Path, what: str) -> bytes:
    # POSIX only, explicitly (as session.py): Windows has no mode bits to read. On POSIX --
    # where the product runs -- this always runs.
    if os.name == "posix" and path.stat().st_mode & _GROUP_OR_OTHER:
        raise StoreError(f"{what} ({path.name}) is readable by group or others; chmod 600 it")
    return path.read_bytes()


def _temp_copy(path: Path, data: bytes) -> Path:
    """`data` in a new, exclusive, randomly named, fsync'd 0600 file beside `path`. On any
    failure nothing is left behind."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, _TEMP_FLAGS, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _replace(path: Path, data: bytes) -> None:
    """Atomic replace: readers see the old file or the whole new one, never a partial."""
    tmp = _temp_copy(path, data)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _create(path: Path, data: bytes) -> None:
    """Atomic create-if-absent: `os.link` publishes the complete temp file under `path`, or
    raises FileExistsError if another process got there first. Never a truncated file."""
    tmp = _temp_copy(path, data)
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _audit_acknowledgement(client_id: str, entry: dict[str, str]) -> None:
    """One `audit_log` row per recorded acknowledgement. Imported here, not at module load:
    resolving a provider must not build a database engine."""
    from selector_drift.db.models import AuditLog
    from selector_drift.db.session import Session

    with Session() as s:
        s.add(
            AuditLog(
                at=dt.datetime.fromisoformat(entry["at"]),
                actor=entry["actor"],
                action=AUDIT_ACTION,
                subject=client_id,
                detail={"sha256": entry["sha256"], "document": entry["document"]},
            )
        )
        s.commit()


def _history(plaintext: bytes) -> list[dict[str, str]] | None:
    """The decrypted history, or None if it is not a well-formed one."""
    try:
        entries = json.loads(plaintext)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(entries, list) or not entries:
        return None
    for e in entries:
        if not isinstance(e, dict) or not all(isinstance(v, str) for v in e.values()):
            return None
        if not _SHA256.fullmatch(e.get("sha256", "")) or not e.get("actor"):
            return None
    return entries


class EncryptedStore:
    def __init__(
        self,
        client_id: str,
        *,
        root: Path | None = None,
        now: Callable[[], dt.datetime] = utc_now,
    ) -> None:
        if not _ID.fullmatch(client_id):
            raise ValueError(f"{client_id!r} is not a client id (lowercase, digits, '-')")
        self.client_id = client_id
        self._dir = (root if root is not None else get_settings().credential_store) / client_id
        self._now = now

    def __repr__(self) -> str:
        return f"EncryptedStore({self.client_id!r})"

    # -- the written acknowledgement -----------------------------------------------------------

    def _ack_aad(self) -> bytes:
        return f"ack|{self.client_id}".encode()

    def _read_history(self, key: bytes) -> list[dict[str, str]]:
        """The verified history; [] if none is on file. Raises StoreError if the file does not
        decrypt under this client's key or is not a well-formed history."""
        path = self._dir / _ACKS
        if not path.is_file():
            return []
        what = "the acknowledgement history"
        plaintext = _unseal(_read_owner_only(path, what), key, self._ack_aad(), what)
        entries = _history(plaintext)
        if entries is None:
            raise StoreError(f"{self.client_id}: {what} is malformed")
        return entries

    def acknowledgements(self) -> list[dict[str, str]]:
        """Every acknowledgement recorded for this client, oldest first."""
        if not (self._dir / _ACKS).is_file():
            return []
        return self._read_history(self._data_key())

    def record_acknowledgement(self, document: Path, *, actor: str) -> str:
        """Append the SHA-256 of the client's signed acknowledgement that stored credentials are
        a last resort to the sealed history, after writing it to `audit_log`. Returns the digest.
        A history that cannot be verified is refused, never overwritten. The document itself is
        not copied into the store."""
        content = document.read_bytes()
        if not content:
            raise StoreError(f"{document.name} is empty; it cannot be a signed acknowledgement")
        if not actor.strip():
            raise StoreError("an acknowledgement is recorded by a named actor")
        key = self._data_key()
        history = self._read_history(key)  # raises on a forged or edited history
        entry = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "document": document.name,
            "actor": actor.strip(),
            "at": self._now().isoformat(),
        }
        _audit_acknowledgement(self.client_id, entry)  # audited first: never in force unaudited
        history.append(entry)
        _replace(self._dir / _ACKS, _seal(json.dumps(history).encode(), key, self._ack_aad()))
        log.info(
            "credential store %s: acknowledgement %s recorded", self.client_id, entry["sha256"]
        )
        return entry["sha256"]

    def _require_acknowledgement(self) -> None:
        if not (self._dir / _ACKS).is_file():  # before any key is touched: nothing on disk
            raise AcknowledgementRequired(
                f"{self.client_id}: stored credentials are a last resort and need the client's "
                "written acknowledgement on file (record_acknowledgement)"
            )
        key = self._data_key()  # a wrong or missing KEK is its own error, not "no ack"
        # Permissions, too, are their own error ("chmod 600"), checked before verification.
        _read_owner_only(self._dir / _ACKS, "the acknowledgement history")
        verified = True
        try:
            self._read_history(key)
        except StoreError:
            verified = False
        if not verified:
            raise AcknowledgementRequired(
                f"{self.client_id}: the acknowledgement on file cannot be verified (edited, "
                "forged, or moved from another client) -- record it again from the signed copy"
            )

    # -- keys ----------------------------------------------------------------------------------

    def _data_key(self) -> bytes:
        path = self._dir / _DATAKEY
        aad = f"datakey|{self.client_id}".encode()
        kek = _kek()  # before anything touches disk: no KEK, no store
        if not path.is_file():
            key = AESGCM.generate_key(bit_length=256)
            try:
                _create(path, _seal(key, kek, aad))
            except FileExistsError:
                pass  # another process won the race: read its key below
            else:
                return key
        return _unseal(_read_owner_only(path, "the data key"), kek, aad, "the data key")

    def _blob(self, target: str) -> Path:
        if not _ID.fullmatch(target):
            raise ValueError(f"{target!r} is not a target name")
        return self._dir / f"{target}.bin"

    # -- CredentialProvider --------------------------------------------------------------------

    def put(self, target: AuthorisedTarget, creds: Credentials) -> None:
        self._require_acknowledgement()
        payload = json.dumps(
            {
                "cookies": creds.cookies,
                "username": creds.username,
                "password": creds.password.get_secret_value() if creds.password else None,
            }
        ).encode("utf-8")
        aad = f"{self.client_id}|{target.target}".encode()
        _replace(self._blob(target.target), _seal(payload, self._data_key(), aad))
        log.info("credential store %s: sealed the credential for %s", self.client_id, target.target)

    def get(self, target: AuthorisedTarget) -> Credentials:
        self._require_acknowledgement()
        path = self._blob(target.target)
        if not path.is_file():
            raise StoreError(f"{self.client_id}: no stored credential for {target.target}")
        aad = f"{self.client_id}|{target.target}".encode()
        sealed = _read_owner_only(path, "the credential")
        data = json.loads(_unseal(sealed, self._data_key(), aad, "the credential"))
        live: list[dict[str, Any]] | None = None
        if data["cookies"]:
            # H2: the same filter as every provider -- host-only clones of live cookies for the
            # authorised origin; what was stored is not trusted for scope.
            picked = select_cookies(data["cookies"], target.origins, self._now())
            if picked.unreadable:
                raise StoreError(
                    f"{self.client_id}: {picked.unreadable} stored cookie(s) for "
                    f"{target.target} have an unreadable expiry"
                )
            log.info(
                "credential store %s for %s: %d live cookie(s); %d dropped as off-origin, "
                "%d expired",
                self.client_id,
                target.target,
                len(picked.live),
                picked.off_origin,
                picked.expired,
            )
            live = picked.live or None
        if not live and not (data["username"] and data["password"]):
            raise CredentialsUnavailable(
                f"{self.client_id}: the stored credential for {target.target} has neither a live "
                "cookie for the authorised origin nor a username and password"
            )
        return Credentials(cookies=live, username=data["username"], password=data["password"])

    def get_totp_code(self, target: AuthorisedTarget) -> str:
        raise NotSupported(
            "a stored credential is never a second factor: storing a TOTP seed defeats it "
            "(brief rule 4) -- use the client's broker"
        )
