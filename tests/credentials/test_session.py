import datetime as dt
import json
import logging
import os
import secrets
from pathlib import Path

import pytest
from pydantic import ValidationError

import selector_drift.credentials.session as session_mod
from selector_drift.credentials.provider import (
    CredentialProvider,
    Credentials,
    CredentialsUnavailable,
    NotSupported,
)
from selector_drift.credentials.session import SessionInjection

# Generated per run, never a committed token: obviously fake and unique, so a test asserting
# its absence from a log, repr or exception cannot pass by coincidence.
COOKIE = f"fake-session-{secrets.token_hex(8)}"
PASSWORD = f"fake-password-{secrets.token_hex(8)}"
FUTURE = dt.datetime(2030, 1, 1, tzinfo=dt.UTC).timestamp()
PAST = dt.datetime(2020, 1, 1, tzinfo=dt.UTC).timestamp()


def _state(tmp_path: Path, cookies: list[dict]) -> Path:
    p = tmp_path / "testbench.json"
    p.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    if os.name == "posix":
        p.chmod(0o600)
    return p


def _cookie(domain: str = "bench.example.invalid", expires: float = FUTURE) -> dict:
    return {
        "name": "session",
        "value": COOKIE,
        "domain": domain,
        "path": "/",
        "expires": expires,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


@pytest.mark.unit
def test_password_is_a_secret_str():
    c = Credentials(password=PASSWORD)
    assert c.password is not None and c.password.get_secret_value() == PASSWORD
    assert PASSWORD not in repr(c) and PASSWORD not in str(c)


@pytest.mark.unit
def test_cookie_values_are_not_in_the_repr():
    c = Credentials(cookies=[_cookie()])
    assert c.cookies is not None and c.cookies[0]["value"] == COOKIE  # it is in there
    assert COOKIE not in repr(c) and COOKIE not in str(c)


@pytest.mark.unit
def test_credentials_are_frozen_and_refuse_unknown_fields():
    with pytest.raises(ValidationError):
        Credentials(totp_seed="x")
    c = Credentials(username="u")
    with pytest.raises(ValidationError):
        c.username = "v"


@pytest.mark.unit
def test_session_injection_is_a_credential_provider(tmp_path):
    assert isinstance(SessionInjection(_state(tmp_path, [_cookie()])), CredentialProvider)


@pytest.mark.unit
def test_session_injection_returns_the_cookies_for_the_authorised_origin(tmp_path, target):
    creds = SessionInjection(_state(tmp_path, [_cookie()])).get(target)
    assert creds.cookies is not None and [c["value"] for c in creds.cookies] == [COOKIE]
    assert creds.password is None and creds.username is None


@pytest.mark.unit
def test_a_parent_domain_cookie_and_a_browser_session_cookie_are_live(tmp_path, target):
    # Playwright writes expires=-1 for a browser-session cookie; a leading-dot parent domain is
    # a cookie the portal set for itself and its subdomains.
    # H2 (fix round 1): matched, but rewritten host-only to the exact authorised host -- never
    # handed over scoped wider than the authorised origin.
    state = _state(tmp_path, [_cookie(domain=".example.invalid", expires=-1)])
    creds = SessionInjection(state).get(target)
    assert [c["domain"] for c in creds.cookies or []] == ["bench.example.invalid"]
    assert creds.cookies and creds.cookies[0]["value"] == COOKIE


@pytest.mark.unit
@pytest.mark.parametrize(
    "domain", ["invalid", ".invalid", "example.invalid", ".BENCH.example.invalid"]
)
def test_a_broad_or_public_suffix_cookie_is_rewritten_to_the_host(tmp_path, target, domain):
    creds = SessionInjection(_state(tmp_path, [_cookie(domain=domain)])).get(target)
    assert [c["domain"] for c in creds.cookies or []] == ["bench.example.invalid"]


def _target(*origins: str):
    from selector_drift.scope.gate import authorise
    from selector_drift.scope.record import Basis, ScopeRecord

    rec = ScopeRecord(
        target="testbench",
        basis=Basis.SELF_OWNED,
        authorising_party="me",
        authorised_on=dt.date(2026, 1, 1),
        expires_on=dt.date(2027, 12, 31),
        evidence_uri=None,
        evidence_sha256=None,
        origins=list(origins),
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
    )
    return authorise(rec, today=dt.date(2026, 9, 7))


@pytest.mark.unit
def test_a_com_cookie_never_stays_broad(tmp_path):
    t = _target("https://portal.saasvendor.com")
    state = _state(tmp_path, [_cookie(domain="com"), _cookie(domain=".saasvendor.com")])
    creds = SessionInjection(state).get(t)
    assert [c["domain"] for c in creds.cookies or []] == ["portal.saasvendor.com"] * 2


@pytest.mark.unit
@pytest.mark.parametrize("domain", ["0.0.1", ".0.0.1", "0.1", "1"])
def test_an_ip_literal_host_matches_only_exactly(tmp_path, domain):
    t = _target("http://127.0.0.1:8080")
    with pytest.raises(CredentialsUnavailable, match="fresh session"):
        SessionInjection(_state(tmp_path, [_cookie(domain=domain)])).get(t)
    creds = SessionInjection(_state(tmp_path, [_cookie(domain="127.0.0.1")])).get(t)
    assert [c["domain"] for c in creds.cookies or []] == ["127.0.0.1"]


@pytest.mark.unit
def test_each_authorised_host_under_one_parent_gets_its_own_host_only_clone(tmp_path):
    t = _target("https://a.example.invalid", "https://b.example.invalid")
    creds = SessionInjection(_state(tmp_path, [_cookie(domain=".example.invalid")])).get(t)
    assert sorted(c["domain"] for c in creds.cookies or []) == [
        "a.example.invalid",
        "b.example.invalid",
    ]
    assert all(c["value"] == COOKIE for c in creds.cookies or [])


@pytest.mark.unit
def test_serialisation_never_carries_a_cookie_value(tmp_path, target):
    creds = SessionInjection(_state(tmp_path, [_cookie()])).get(target)
    assert creds.cookies and creds.cookies[0]["value"] == COOKIE  # in-process: the real value
    dumped = creds.model_dump()
    assert COOKIE not in repr(dumped)
    assert COOKIE not in creds.model_dump_json()
    assert COOKIE not in repr(creds.model_dump(mode="json"))
    # Everything but the value survives, so a dump is still useful for counts and diagnostics.
    assert dumped["cookies"][0]["domain"] == "bench.example.invalid"
    assert dumped["cookies"][0]["name"] == "session"


@pytest.mark.unit
def test_secrets_never_appear_in_a_log(tmp_path, target, caplog):
    # Fixed (M19-class): asserts on the cookie value get() actually loads, not one never read.
    state = _state(tmp_path, [_cookie(), _cookie(domain="tracker.example.org")])
    with caplog.at_level(logging.DEBUG):
        creds = SessionInjection(state).get(target)
    assert creds.cookies and creds.cookies[0]["value"] == COOKIE
    assert caplog.records, "get() is expected to log a count"
    assert COOKIE not in caplog.text


@pytest.mark.unit
def test_a_refusal_never_carries_a_cookie_value(tmp_path, target):
    state = _state(tmp_path, [_cookie(expires=PAST)])
    with pytest.raises(CredentialsUnavailable) as exc:
        SessionInjection(state).get(target)
    assert COOKIE not in str(exc.value) and COOKIE not in repr(exc.value)


@pytest.mark.unit
def test_a_cookie_for_another_origin_is_never_handed_over(tmp_path, target):
    # H2: the injected session may only ever reach the authorised origin.
    state = _state(
        tmp_path,
        [
            _cookie(),
            _cookie(domain=".tracker.example.invalid"),
            _cookie(domain="evilbench.example.invalid"),
            _cookie(domain="bench.example.invalid.evil.test"),
            _cookie(domain=""),
        ],
    )
    creds = SessionInjection(state).get(target)
    assert [c["domain"] for c in creds.cookies or []] == ["bench.example.invalid"]


@pytest.mark.unit
def test_an_expired_session_is_refused_not_used_logged_out(tmp_path, target):
    state = _state(tmp_path, [_cookie(expires=PAST)])
    with pytest.raises(CredentialsUnavailable, match="fresh session"):
        SessionInjection(state).get(target)


@pytest.mark.unit
def test_the_clock_is_injected(tmp_path, target):
    state = _state(tmp_path, [_cookie(expires=FUTURE)])
    later = dt.datetime(2031, 1, 1, tzinfo=dt.UTC)
    with pytest.raises(CredentialsUnavailable, match="fresh session"):
        SessionInjection(state, now=lambda: later).get(target)


@pytest.mark.unit
def test_a_session_with_only_foreign_cookies_is_refused(tmp_path, target):
    state = _state(tmp_path, [_cookie(domain="tracker.example.org")])
    with pytest.raises(CredentialsUnavailable, match="fresh session"):
        SessionInjection(state).get(target)


@pytest.mark.unit
def test_a_missing_state_file_is_refused(tmp_path, target):
    with pytest.raises(CredentialsUnavailable):
        SessionInjection(tmp_path / "absent.json").get(target)


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '{"cookies": "nope"}',
        '{"cookies": [{"domain": "bench.example.invalid", "expires": "soon"}]}',
    ],
)
def test_a_malformed_state_file_is_refused_not_crashed(tmp_path, target, body):
    p = tmp_path / "testbench.json"
    p.write_text(body, encoding="utf-8")
    if os.name == "posix":
        p.chmod(0o600)
    with pytest.raises(CredentialsUnavailable):
        SessionInjection(p).get(target)


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits; the product runs on Linux")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_a_state_file_readable_by_others_is_refused(tmp_path, target, mode):
    state = _state(tmp_path, [_cookie()])
    state.chmod(mode)
    with pytest.raises(CredentialsUnavailable, match="chmod 600"):
        SessionInjection(state).get(target)


@pytest.mark.unit
def test_the_permission_check_runs_whenever_os_name_is_posix(tmp_path, target, monkeypatch):
    """The gate itself, exercised on every host: with os.name reporting "posix" and the file's
    mode reporting group/other read, the file is refused -- the check never silently passes
    on POSIX."""
    state = _state(tmp_path, [_cookie()])
    real_stat = Path.stat

    def stat_0644(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        if self == state:
            return os.stat_result((0o100644, *tuple(st)[1:]))
        return st

    monkeypatch.setattr(session_mod.os, "name", "posix")
    monkeypatch.setattr(Path, "stat", stat_0644)
    with pytest.raises(CredentialsUnavailable, match="chmod 600"):
        SessionInjection(state).get(target)


@pytest.mark.unit
def test_session_injection_cannot_produce_a_totp_code(tmp_path, target):
    with pytest.raises(NotSupported):
        SessionInjection(_state(tmp_path, [_cookie()])).get_totp_code(target)


@pytest.mark.unit
def test_the_repr_names_the_file_not_its_contents(tmp_path):
    r = repr(SessionInjection(_state(tmp_path, [_cookie()])))
    assert "testbench.json" in r and COOKIE not in r
