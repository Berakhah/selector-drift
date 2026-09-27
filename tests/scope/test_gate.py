import dataclasses
import datetime as dt
import hashlib
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget, Basis, RateLimit, ScopeRecord

TODAY = dt.date(2026, 9, 7)


def rec(**kw):
    base = dict(
        target="testbench",
        basis=Basis.SELF_OWNED,
        authorising_party="me",
        authorised_on=dt.date(2026, 9, 1),
        expires_on=dt.date(2027, 9, 1),
        evidence_uri=None,
        evidence_sha256=None,
        origins=["https://bench.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
    )
    return ScopeRecord(**(base | kw))


@pytest.mark.unit
def test_valid_self_owned_record_is_authorised():
    assert authorise(rec(), today=TODAY).target == "testbench"


@pytest.mark.unit
def test_expired_authorisation_is_refused():
    with pytest.raises(ScopeRefusal, match="expired"):
        authorise(rec(expires_on=dt.date(2026, 9, 6)), today=TODAY)


@pytest.mark.unit
def test_public_permissive_basis_does_not_exist():
    with pytest.raises(ValueError):
        rec(basis="public-permissive")


@pytest.mark.unit
def test_client_authorised_without_evidence_is_refused():
    with pytest.raises(ScopeRefusal, match="evidence_uri"):
        authorise(rec(basis=Basis.CLIENT_AUTHORISED, evidence_uri=None), today=TODAY)


@pytest.mark.unit
def test_authorised_target_cannot_be_built_outside_the_gate():
    with pytest.raises(TypeError):
        AuthorisedTarget(target="anything", basis=Basis.SELF_OWNED)  # type: ignore[call-arg]


@pytest.mark.unit
def test_a_forged_token_is_rejected_by_identity_not_by_type():
    """M29. A leading underscore is a convention. Someone WILL import _ScopeToken --
    probably a test fixture at 2am. Type-checking the token catches nothing; only
    identity against the singleton minted in gate.py does."""
    from selector_drift.scope._token import _ScopeToken

    with pytest.raises(ScopeRefusal, match="mint"):
        AuthorisedTarget(
            _ScopeToken(),
            target="anything",
            basis=Basis.SELF_OWNED,
            rate_limit=RateLimit(1, 30),
            origins=[],
            user_agent_contact="",
            first_heal_policy="required",
            sensitivity="SYNTHETIC",
            expires_on=dt.date(2027, 1, 1),
            record_sha256="0" * 64,
        )


@pytest.mark.unit
def test_sensitivity_is_derived_from_basis_and_cannot_be_authored():
    """R4/H7. SYNTHETIC skips the redaction argument and permits publication.
    If it is a field, one typo publishes a client portal."""
    assert "sensitivity" not in ScopeRecord.model_fields
    assert rec(basis=Basis.SELF_OWNED).sensitivity == "SYNTHETIC"
    assert (
        rec(
            basis=Basis.CLIENT_AUTHORISED, evidence_uri="file://a", evidence_sha256="0" * 64
        ).sensitivity
        == "CLIENT_CONFIDENTIAL"
    )


@pytest.mark.unit
def test_client_authorised_evidence_must_hash_to_what_the_record_claims(tmp_path):
    """R3/H10."""
    doc = tmp_path / "authorisation.pdf"
    doc.write_bytes(b"countersigned")
    with pytest.raises(ScopeRefusal, match="evidence_sha256"):
        authorise(
            rec(basis=Basis.CLIENT_AUTHORISED, evidence_uri=doc.as_uri(), evidence_sha256="0" * 64),
            today=TODAY,
        )


@pytest.mark.unit
def test_a_record_with_no_origins_is_refused():
    """H2. An empty allowlist means the browser may go anywhere, which is the
    opposite of what a scope record is for."""
    with pytest.raises(ScopeRefusal, match="origins"):
        authorise(rec(origins=[]), today=TODAY)


@pytest.mark.unit
def test_authorised_on_after_expires_on_is_refused():
    with pytest.raises(ScopeRefusal):
        authorise(
            rec(authorised_on=dt.date(2027, 1, 1), expires_on=dt.date(2026, 12, 1)), today=TODAY
        )


@pytest.mark.unit
def test_expiry_is_evaluated_in_utc_never_local_time():
    """M20. The run window, the mutation seed and this check must agree on a
    calendar or the bench's answer key drifts by a day at the boundary."""
    import selector_drift.scope.gate as g

    assert "date.today()" not in inspect.getsource(g)


def _client_rec(tmp_path, **kw):
    doc = tmp_path / "authorisation.pdf"
    doc.write_bytes(b"countersigned")
    return rec(
        basis=Basis.CLIENT_AUTHORISED,
        evidence_uri=doc.as_uri(),
        evidence_sha256=hashlib.sha256(b"countersigned").hexdigest(),
        **kw,
    )


@pytest.mark.unit
def test_client_target_without_retention_days_is_refused(tmp_path):
    """API spec 7.4 / M30. A refusal, not a default."""
    with pytest.raises(ScopeRefusal, match="evidence_retention_days"):
        authorise(_client_rec(tmp_path), today=TODAY)


@pytest.mark.unit
def test_client_target_with_retention_days_is_authorised(tmp_path):
    t = authorise(_client_rec(tmp_path, evidence_retention_days=90), today=TODAY)
    assert t.evidence_retention_days == 90


@pytest.mark.unit
def test_self_owned_target_may_keep_evidence_for_the_life_of_the_log():
    assert authorise(rec(), today=TODAY).evidence_retention_days is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -1])
def test_warmup_runs_must_be_at_least_one(bad):
    with pytest.raises(ValidationError):
        rec(warmup_runs=bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -30])
def test_retention_days_must_be_positive_when_set(bad):
    with pytest.raises(ValidationError):
        rec(evidence_retention_days=bad)


@pytest.mark.unit
def test_warmup_defaults_to_thirty_and_reaches_the_authorised_target():
    assert rec().warmup_runs == 30
    assert authorise(rec(warmup_runs=5), today=TODAY).warmup_runs == 5


@pytest.mark.unit
def test_a_record_signed_in_the_future_is_refused():
    """B4. A record dated tomorrow authorises nothing today."""
    with pytest.raises(ScopeRefusal, match="future"):
        authorise(rec(authorised_on=TODAY + dt.timedelta(days=1)), today=TODAY)


@pytest.mark.unit
def test_record_sha256_is_the_canonical_json_of_the_record():
    """The hash authorise() mints is over the record's values, key order and comments aside."""
    import json

    from selector_drift.scope.gate import record_sha256

    r = rec()
    canonical = json.dumps(r.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    assert (
        authorise(r, today=TODAY).record_sha256
        == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        == record_sha256(r)
    )
    assert record_sha256(rec(authorising_party="someone else")) != record_sha256(r)


# --- Fix round 1 ------------------------------------------------------------------------


@pytest.mark.unit
def test_mutating_a_scope_record_after_construction_is_rejected():
    """I-1. `ScopeRecord` is frozen: attribute assignment must fail, not silently mutate a
    validated record into an unvalidated one in place."""
    r = rec()
    with pytest.raises(Exception):  # noqa: B017 -- pydantic raises ValidationError here,
        # not a plain assertion the test needs to name; any raise proves assignment failed.
        r.basis = Basis.CLIENT_AUTHORISED  # type: ignore[misc]


@pytest.mark.unit
def test_model_construct_bypass_with_invalid_basis_is_refused_by_authorise():
    """I-1. `model_construct()` skips validation entirely, so a `ScopeRecord` with a `basis`
    outside the v1 enum is constructible. `authorise()` must still refuse it via
    re-validation, not mint from whatever it was handed."""
    r = ScopeRecord.model_construct(
        target="testbench",
        basis="public-permissive",
        authorising_party="me",
        authorised_on=dt.date(2026, 9, 1),
        expires_on=dt.date(2027, 9, 1),
        evidence_uri=None,
        evidence_sha256=None,
        origins=["https://bench.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
        warmup_runs=30,
        evidence_retention_days=None,
    )
    with pytest.raises(ScopeRefusal):
        authorise(r, today=TODAY)


@pytest.mark.unit
def test_model_construct_bypass_with_bad_rate_limit_dict_is_refused_not_attributeerror():
    """I-1. `model_construct()` does not coerce a dict into `RateLimit`, so `record.rate_limit`
    is a plain dict here. Without re-validation, `record.rate_limit.requests` inside
    `authorise()` would raise `AttributeError`; with it, the malformed shape is caught as a
    `ScopeRefusal` before any attribute access on it happens."""
    r = ScopeRecord.model_construct(
        target="testbench",
        basis=Basis.SELF_OWNED,
        authorising_party="me",
        authorised_on=dt.date(2026, 9, 1),
        expires_on=dt.date(2027, 9, 1),
        evidence_uri=None,
        evidence_sha256=None,
        origins=["https://bench.example.invalid"],
        rate_limit={"per_seconds": 30},  # missing "requests" -- not a RateLimit
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
        warmup_runs=30,
        evidence_retention_days=None,
    )
    with pytest.raises(ScopeRefusal):
        authorise(r, today=TODAY)


@pytest.mark.unit
def test_dataclasses_replace_on_an_authorised_target_is_refused():
    """I-2. `dataclasses.replace()` passes the already-minted `_MINT` token straight back
    through `__init__`, so an identity-only check would let it re-mint outside the gate.
    The minting context flag must also be required, and it is unset once `authorise()` has
    returned."""
    target = authorise(rec(), today=TODAY)
    with pytest.raises(ScopeRefusal, match="mint"):
        dataclasses.replace(target, origins=("*",))


@pytest.mark.unit
def test_no_module_outside_scope_references_the_mint_internals():
    """I-2. The identity check plus the minting context flag, and the import-linter
    contract, are the only paths to `_MINT`/`_MINTING`. Nothing outside `selector_drift.scope`
    should even textually mention them -- a module that does is either working around the
    gate or about to."""
    offenders = []
    for path in Path("selector_drift").rglob("*.py"):
        parts = path.relative_to("selector_drift").parts
        if parts and parts[0] == "scope":
            continue
        text = path.read_text(encoding="utf-8")
        if "_MINT" in text or "_MINTING" in text:
            offenders.append(str(path))
    assert not offenders, offenders


@pytest.mark.unit
def test_evidence_uri_with_embedded_null_byte_is_refused_not_crashed(tmp_path):
    """M-4. `url2pathname`'s percent-decoding can produce a path containing an embedded
    null byte, which raises `ValueError` deep inside `Path.read_bytes()`. That must surface
    as a `ScopeRefusal`, never an uncaught `ValueError`."""
    with pytest.raises(ScopeRefusal):
        authorise(
            rec(
                basis=Basis.CLIENT_AUTHORISED,
                evidence_uri="file:///a%00b",
                evidence_sha256="0" * 64,
                evidence_retention_days=90,
            ),
            today=TODAY,
        )


@pytest.mark.unit
def test_evidence_uri_naming_a_host_is_refused():
    """M-4. A `file://` URI with a non-empty, non-`localhost` netloc names a remote or
    UNC-style host; the gate must not attempt to read it."""
    with pytest.raises(ScopeRefusal):
        authorise(
            rec(
                basis=Basis.CLIENT_AUTHORISED,
                evidence_uri="file://example.invalid/etc/passwd",
                evidence_sha256="0" * 64,
                evidence_retention_days=90,
            ),
            today=TODAY,
        )
