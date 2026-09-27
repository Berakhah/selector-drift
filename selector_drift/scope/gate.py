"""The scope gate: the only place an `AuthorisedTarget` is minted.

`authorise()` refuses closed -- every failure mode raises `ScopeRefusal` rather than
returning something falsy or logging and continuing. No target reaches the browser driver,
the resolver or the orchestrator without passing through here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from pydantic import ValidationError

# Aliased, never `from ... import _MINT` (fix round 1, I-2): this module's own namespace must
# not re-export the mint token or the minting flag either.
from selector_drift.scope import _token as _token_module
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.record import AuthorisedTarget, Basis, ScopeRecord


def record_sha256(record: ScopeRecord) -> str:
    """The hash `authorise()` mints, over the record's canonical JSON -- every field, keys
    sorted, no whitespace -- not over the YAML bytes (`authorise()` never receives those).
    Two files differing only in comments or key order hash the same; any change to a value
    does not.
    """
    canonical = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_evidence_bytes(evidence_uri: str) -> bytes:
    """Fail closed: a non-`file://` URI, a URI naming a host, a malformed URI (`urlparse`
    itself can raise `ValueError`, e.g. on an embedded null byte), or a missing/unreadable
    file is all a `ScopeRefusal`, never an uncaught exception (M-4). Uses `urllib.parse` +
    `url2pathname` so a Windows `Path.as_uri()` result (e.g. ``file:///D:/foo/bar.pdf``)
    round-trips correctly.
    """
    try:
        parsed = urlparse(evidence_uri)
    except ValueError as exc:
        raise ScopeRefusal(f"evidence_uri could not be parsed: {evidence_uri!r} ({exc})") from exc

    if parsed.scheme != "file":
        raise ScopeRefusal(f"evidence_uri must be a file:// URI, got: {evidence_uri!r}")

    if parsed.netloc not in ("", "localhost"):
        raise ScopeRefusal(f"evidence_uri must not name a host: {evidence_uri!r}")

    try:
        path = Path(url2pathname(parsed.path))
        return path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ScopeRefusal(f"evidence_uri could not be read: {evidence_uri!r} ({exc})") from exc


def authorise(record: ScopeRecord, *, today: date) -> AuthorisedTarget:
    """Refuses, in order, on each of:

    - the record does not re-validate (see below) -- a mutated or `model_construct`-built
      record is not trusted just because its type is `ScopeRecord` (fix round 1, I-1)
    - `expires_on < today`
    - `authorised_on > expires_on`
    - `authorised_on > today` (a record signed tomorrow authorises nothing today, B4)
    - `basis` outside the v1 enum (design 4.2; belt-and-braces alongside the re-validation
      above, which already forecloses this for anything reaching this line)
    - `basis is CLIENT_AUTHORISED and evidence_uri is None`
    - `evidence_uri` set and its bytes do not hash to `evidence_sha256`
    - `basis is CLIENT_AUTHORISED and evidence_retention_days is None` (API spec 7.4, M30)
    - `origins` empty (H2)
    - `rate_limit.requests < 1 or per_seconds < 1` (H4)

    `today` is always passed in, never defaulted -- every caller passes
    `datetime.now(UTC).date()` (M20).
    """
    # I-1: re-validate what we were handed. `ScopeRecord` is frozen, but frozen only blocks
    # *assignment* -- `model_construct()` builds an instance without ever calling the
    # validators, and nothing stops a caller passing one in. Round-tripping through
    # `model_validate(record.model_dump())` re-runs every field validator (enum membership,
    # the `ge=1` constraints, ...) against the record's actual current values, and a
    # `ValidationError` here becomes a `ScopeRefusal` rather than propagating as a crash or,
    # worse, being skipped entirely.
    try:
        # warnings=False: dumping a `model_construct()`-built record with mismatched field
        # types (exactly the case this re-validation exists to catch) otherwise emits a
        # pydantic UserWarning per mismatched field on every refusal -- noise, not signal.
        record = ScopeRecord.model_validate(record.model_dump(warnings=False))
    except ValidationError as exc:
        raise ScopeRefusal(f"record failed re-validation: {exc}") from exc

    if record.expires_on < today:
        raise ScopeRefusal("authorisation expired")

    if record.authorised_on > record.expires_on:
        raise ScopeRefusal("authorised_on is after expires_on")

    if record.authorised_on > today:
        raise ScopeRefusal("authorised_on is in the future")

    if record.basis not in (Basis.SELF_OWNED, Basis.CLIENT_AUTHORISED):
        raise ScopeRefusal("basis outside the v1 enum")

    if record.basis is Basis.CLIENT_AUTHORISED and record.evidence_uri is None:
        raise ScopeRefusal("evidence_uri is required for a client-authorised target")

    if record.evidence_uri is not None:
        actual_sha256 = hashlib.sha256(_read_evidence_bytes(record.evidence_uri)).hexdigest()
        if actual_sha256 != record.evidence_sha256:
            raise ScopeRefusal("evidence_sha256 does not match the bytes at evidence_uri")

    if record.basis is Basis.CLIENT_AUTHORISED and record.evidence_retention_days is None:
        raise ScopeRefusal("evidence_retention_days is required for a client-authorised target")

    if not record.origins:
        raise ScopeRefusal("origins must not be empty")

    if record.rate_limit.requests < 1 or record.rate_limit.per_seconds < 1:
        raise ScopeRefusal("rate_limit must be at least 1 request per at least 1 second")

    # I-2: only this call to AuthorisedTarget(...) may succeed. _MINTING is reset in `finally`
    # regardless of outcome, so it is never left set after authorise() returns or raises.
    reset_token = _token_module._MINTING.set(True)
    try:
        return AuthorisedTarget(
            _token_module._MINT,
            target=record.target,
            basis=record.basis,
            rate_limit=record.rate_limit,
            origins=tuple(record.origins),
            user_agent_contact=record.user_agent_contact,
            first_heal_policy=record.first_heal_policy,
            sensitivity=record.sensitivity,
            expires_on=record.expires_on,
            record_sha256=record_sha256(record),
            warmup_runs=record.warmup_runs,
            evidence_retention_days=record.evidence_retention_days,
        )
    finally:
        _token_module._MINTING.reset(reset_token)
