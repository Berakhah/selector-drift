"""Stored credentials -- the last resort (design 2.1 Q2; brief rule 4), with a written
acknowledgement. `put()` and `get()` refuse until the client's signed acknowledgement is on file
by its SHA-256 -- the same integrity move as the scope record's `evidence_sha256`, so "they
agreed to this" is checkable, not remembered.

Envelope encryption: a key-encryption key (KEK, `Settings.credential_kek`, from
SD_CREDENTIAL_KEK, never on disk) wraps a per-client AES-256 data key (`datakey.bin`, AAD
`datakey|<client>`); the data key seals each target's credential with AES-GCM under AAD
`<client>|<target>`, so a blob copied to another client, or renamed to another target, fails
authentication instead of decrypting. Rotating the KEK re-wraps one small file per client.

Files are written atomically with mode 0600 (directories 0700), and on POSIX a key or credential
file readable by group or others is refused, as Task 31 does for session files. Nothing here
logs, reprs or raises a key, a password, a cookie value or a username.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from selector_drift.config import get_settings
from selector_drift.credentials.provider import Credentials, NotSupported
from selector_drift.scope.record import AuthorisedTarget

log = logging.getLogger(__name__)

KEK_ENV: Final = "SD_CREDENTIAL_KEK"  # named in messages only; config.py reads it
_KEY_BYTES: Final = 32
_MAGIC: Final = b"SDC1"
_NONCE: Final = 12
_ACK: Final = "acknowledgement.json"
_DATAKEY: Final = "datakey.bin"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GROUP_OR_OTHER = stat.S_IRWXG | stat.S_IRWXO


class StoreError(Exception):
    """The store cannot be opened or written. Never carries key or credential material."""


class AcknowledgementRequired(StoreError):
    """No written acknowledgement on file for this client."""


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
    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + _NONCE + 16:
        raise StoreError(f"{what} is not a sealed credential file")
    nonce, sealed = blob[len(_MAGIC) : len(_MAGIC) + _NONCE], blob[len(_MAGIC) + _NONCE :]
    plaintext: bytes | None = None
    with contextlib.suppress(InvalidTag):
        plaintext = AESGCM(key).decrypt(nonce, sealed, aad)
    if plaintext is None:
        raise StoreError(
            f"{what} cannot be decrypted: wrong key, or moved from another client or target"
        )
    return plaintext


def _read_owner_only(path: Path, what: str) -> bytes:
    # POSIX only, explicitly (as session.py): Windows has no mode bits to read. On POSIX --
    # where the product runs -- this always runs.
    if os.name == "posix" and path.stat().st_mode & _GROUP_OR_OTHER:
        raise StoreError(f"{what} ({path.name}) is readable by group or others; chmod 600 it")
    return path.read_bytes()


def _mkdir_owner_only(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def _write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Atomic, owner-only. `exclusive` creates or fails (FileExistsError) -- the data key is
    generated once per client, and a second process racing to create it must read the winner's."""
    _mkdir_owner_only(path.parent)
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class EncryptedStore:
    def __init__(self, client_id: str, *, root: Path | None = None) -> None:
        if not _ID.fullmatch(client_id):
            raise ValueError(f"{client_id!r} is not a client id (lowercase, digits, '-')")
        self.client_id = client_id
        self._dir = (root if root is not None else get_settings().credential_store) / client_id

    def __repr__(self) -> str:
        return f"EncryptedStore({self.client_id!r})"

    # -- the written acknowledgement -----------------------------------------------------------

    def record_acknowledgement(self, document: Path, *, actor: str) -> str:
        """Record the SHA-256 of the client's signed acknowledgement that stored credentials are
        a last resort. Returns the digest. The document itself is not copied into the store."""
        content = document.read_bytes()
        if not content:
            raise StoreError(f"{document.name} is empty; it cannot be a signed acknowledgement")
        if not actor.strip():
            raise StoreError("an acknowledgement is recorded by a named actor")
        digest = hashlib.sha256(content).hexdigest()
        body = {
            "sha256": digest,
            "document": document.name,
            "actor": actor.strip(),
            "at": dt.datetime.now(dt.UTC).isoformat(),
        }
        _write(self._dir / _ACK, json.dumps(body, sort_keys=True).encode("utf-8"))
        log.info("credential store %s: acknowledgement %s recorded", self.client_id, digest)
        return digest

    def _require_acknowledgement(self) -> None:
        path = self._dir / _ACK
        body: Any = None
        if path.is_file():
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = None
        digest = body.get("sha256") if isinstance(body, dict) else None
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise AcknowledgementRequired(
                f"{self.client_id}: stored credentials are a last resort and need the client's "
                "written acknowledgement on file (record_acknowledgement)"
            )

    # -- keys ----------------------------------------------------------------------------------

    def _data_key(self) -> bytes:
        path = self._dir / _DATAKEY
        aad = f"datakey|{self.client_id}".encode()
        kek = _kek()  # before anything touches disk: no KEK, no store
        if not path.is_file():
            key = AESGCM.generate_key(bit_length=256)
            try:
                _write(path, _seal(key, kek, aad), exclusive=True)
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
        _write(self._blob(target.target), _seal(payload, self._data_key(), aad))
        log.info("credential store %s: sealed the credential for %s", self.client_id, target.target)

    def get(self, target: AuthorisedTarget) -> Credentials:
        self._require_acknowledgement()
        path = self._blob(target.target)
        if not path.is_file():
            raise StoreError(f"{self.client_id}: no stored credential for {target.target}")
        aad = f"{self.client_id}|{target.target}".encode()
        sealed = _read_owner_only(path, "the credential")
        data = json.loads(_unseal(sealed, self._data_key(), aad, "the credential"))
        return Credentials(
            cookies=data["cookies"], username=data["username"], password=data["password"]
        )

    def get_totp_code(self, target: AuthorisedTarget) -> str:
        raise NotSupported(
            "a stored credential is never a second factor: storing a TOTP seed defeats it "
            "(brief rule 4) -- use the client's broker"
        )
