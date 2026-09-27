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

from selector_drift.scope._token import _MINT
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
    """Fail closed: a non-`file://` URI or a missing/unreadable file is a `ScopeRefusal`,
    never an uncaught exception. Uses `urllib.parse` + `url2pathname` so a Windows
    `Path.as_uri()` result (e.g. ``file:///D:/foo/bar.pdf``) round-trips correctly.
    """
    parsed = urlparse(evidence_uri)
    if parsed.scheme != "file":
        raise ScopeRefusal(f"evidence_uri must be a file:// URI, got: {evidence_uri!r}")
    path = Path(url2pathname(parsed.path))
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ScopeRefusal(f"evidence_uri could not be read: {evidence_uri!r} ({exc})") from exc


def authorise(record: ScopeRecord, *, today: date) -> AuthorisedTarget:
    """Refuses, in order, on each of:

    - `expires_on < today`
    - `authorised_on > expires_on`
    - `authorised_on > today` (a record signed tomorrow authorises nothing today, B4)
    - `basis is CLIENT_AUTHORISED and evidence_uri is None`
    - `evidence_uri` set and its bytes do not hash to `evidence_sha256`
    - `basis is CLIENT_AUTHORISED and evidence_retention_days is None` (API spec 7.4, M30)
    - `origins` empty (H2)
    - `rate_limit.requests < 1 or per_seconds < 1` (H4)

    `today` is always passed in, never defaulted -- every caller passes
    `datetime.now(UTC).date()` (M20).
    """
    if record.expires_on < today:
        raise ScopeRefusal("authorisation expired")

    if record.authorised_on > record.expires_on:
        raise ScopeRefusal("authorised_on is after expires_on")

    if record.authorised_on > today:
        raise ScopeRefusal("authorised_on is in the future")

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

    return AuthorisedTarget(
        _MINT,
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
