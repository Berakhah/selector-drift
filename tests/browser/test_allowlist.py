"""Unit tests for the allowlist's origin parsing and the proxy bypass list it produces."""

import datetime as dt

import pytest

from selector_drift.browser import allowlist, driver
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import Basis, ScopeRecord

TODAY = dt.date(2026, 9, 7)


def _target(*origins):
    return authorise(
        ScopeRecord(
            target="testbench",
            basis=Basis.SELF_OWNED,
            authorising_party="me",
            authorised_on=dt.date(2026, 9, 1),
            expires_on=dt.date(2027, 9, 1),
            evidence_uri=None,
            evidence_sha256=None,
            origins=list(origins),
            rate_limit={"requests": 1, "per_seconds": 30},
            user_agent_contact="https://example.invalid/contact",
            first_heal_policy="required",
            legal_review="self_drafted",
        ),
        today=TODAY,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://*/",
        "http://*",
        "http://a.example,*/",
        "http://a.example;*/",
        "http://a example/",
        "http://a.example\t/",
        "<local>",
        "http://<local>/",
        "<-loopback>",
        "http://user@a.example/",
        "ftp://a.example/",
        "data:text/html,hi",
        "about:blank",
        "",
        "http:///x",
        "http://a.example:99999/",
        "http://[::1%,*]/",
        "http://[fe80::1%eth0]/",
        "http://[::1%;*]/",
        "http://[::1%25,*]/",
        "http://0x7f.1/",
        "http://2130706433/",
        "http://127.000.0.1/",
        "http://a.example:" + chr(0xFF18) + chr(0xFF10) + "/",  # full-width digits
        "http://a.example:080/",
    ],
)
def test_origin_of_refuses_anything_that_is_not_a_plain_http_origin(url):
    assert allowlist.origin_of(url) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://a.example/x?y#z", "https://a.example:443"),
        ("http://a.example/", "http://a.example:80"),
        ("http://A.Example:8080/p", "http://a.example:8080"),
        ("http://127.0.0.1:5000/", "http://127.0.0.1:5000"),
        ("http://[::1]:8000/", "http://[::1]:8000"),
    ],
)
def test_origin_of_normalises_a_request_url(url, origin):
    assert allowlist.origin_of(url) == origin


@pytest.mark.unit
def test_proxy_for_an_https_origin_on_the_default_port():
    proxy = allowlist.proxy_for(_target("https://portal.example"))
    assert proxy == {
        "server": "http://egress-denied.invalid:1",
        "bypass": "<-loopback>,https://portal.example:443",
    }


@pytest.mark.unit
def test_proxy_for_an_http_origin_on_a_non_default_port():
    proxy = allowlist.proxy_for(_target("http://127.0.0.1:8080", "https://b.example"))
    assert proxy["bypass"] == "<-loopback>,http://127.0.0.1:8080,https://b.example:443"
    assert proxy["bypass"].startswith("<-loopback>,")


@pytest.mark.unit
def test_webrtc_cannot_leave_by_udp(monkeypatch):
    """UDP bypasses both the proxy and the route handler."""
    monkeypatch.delenv("SD_CHROMIUM_NO_SANDBOX", raising=False)
    assert driver._launch_args() == ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    monkeypatch.setenv("SD_CHROMIUM_NO_SANDBOX", "1")
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in driver._launch_args()
    assert "--no-sandbox" in driver._launch_args()
