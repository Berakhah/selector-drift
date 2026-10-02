"""Task 19 fix round 1, R5: one shared DOM scrubber (S-I2 + S-I3).

Every leaking shape the security review listed goes through capture() with an EMPTY masker, at
both CLIENT_CONFIDENTIAL (default-deny) and SYNTHETIC (the widened heuristic floor), and its
canary must be absent from Evidence.dom. The cascade must still resolve the same cells on the
scrubbed DOM.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import lxml.html
import pytest
from bok_core.boundary.sensitivity import Sensitivity

from selector_drift.heallog.capture import Evidence, capture, credential_input_nodes
from selector_drift.heallog.replay import replay
from selector_drift.heallog.scrub import scrub_dom
from selector_drift.resolve.cache import NO_CACHE
from selector_drift.resolve.cascade import resolve_cascade
from selector_drift.resolve.escalation import EscalateToHuman
from selector_drift.resolve.page import StaticPageSource
from selector_drift.spec.loader import load_spec
from testbench.mutations.base import apply_all
from testbench.mutations.catalogue import CATALOGUE
from testbench.portal.render import render_results
from tests.heallog.conftest import BENCH_HTML, CLAIM_NUMBER, RUN_KEY
from tests.heallog.test_capture import FakePage
from tests.testbench.helpers import FIXTURE_DAY

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJlLWNhbmFyeQ"
HEX = "0123456789abcdef0123456789abcdef0123"

# (id, markup, canary). Each canary is unique, so a hit names the shape that leaked.
SHAPES: list[tuple[str, str, str]] = [
    ("api_key", '<input name="api_key" value="CNRY-a01">', "CNRY-a01"),
    ("apiKey", '<input name="apiKey" value="CNRY-a02">', "CNRY-a02"),
    ("access_key", '<input name="access_key" value="CNRY-a03">', "CNRY-a03"),
    ("auth", '<input name="auth" value="CNRY-a04">', "CNRY-a04"),
    ("session_id", '<input name="session_id" value="CNRY-a05">', "CNRY-a05"),
    ("sid", '<input name="sid" value="CNRY-a06">', "CNRY-a06"),
    ("jwt", '<input name="jwt" value="CNRY-a07">', "CNRY-a07"),
    ("bearer", '<input name="bearer" value="CNRY-a08">', "CNRY-a08"),
    ("passw0rd", '<input name="passw0rd" value="CNRY-a09">', "CNRY-a09"),
    ("pword", '<input name="pword" value="CNRY-a10">', "CNRY-a10"),
    ("pswd", '<input name="pswd" value="CNRY-a11">', "CNRY-a11"),
    ("PINCode", '<input name="PINCode" value="CNRY-a12">', "CNRY-a12"),
    (
        "verification-code",
        '<label for="v">Enter the 6-digit verification code</label>'
        '<input id="v" name="code" value="CNRY-b01">',
        "CNRY-b01",
    ),
    (
        "security-code",
        '<label for="s">Security code</label><input id="s" name="sc" value="CNRY-b02">',
        "CNRY-b02",
    ),
    (
        "security-answer",
        '<label for="q">Answer to security question</label>'
        '<input id="q" name="a1" value="CNRY-b03">',
        "CNRY-b03",
    ),
    (
        "private-key",
        '<label for="k">SSH private key</label><textarea id="k" name="k1">CNRY-b04</textarea>',
        "CNRY-b04",
    ),
    ("de-passwort", '<label>Passwort <input name="f1" value="CNRY-c01"></label>', "CNRY-c01"),
    ("de-kennwort", '<input aria-label="Kennwort" value="CNRY-c02">', "CNRY-c02"),
    ("fr-mot-de-passe", '<input placeholder="Mot de passe" value="CNRY-c03">', "CNRY-c03"),
    ("es-contrasena", '<input aria-label="Contraseña" value="CNRY-c04">', "CNRY-c04"),
    ("es-contrasena-ascii", '<input aria-label="Contrasena" value="CNRY-c05">', "CNRY-c05"),
    ("pt-senha", '<input title="Senha" value="CNRY-c06">', "CNRY-c06"),
    ("it-password", '<input aria-label="La tua password" value="CNRY-c07">', "CNRY-c07"),
    (
        "split-otp",
        '<div aria-label="One-time passcode">'
        '<input maxlength="1" value="CNRY-d01"><input maxlength="1" value="CNRY-d01">'
        '<input maxlength="1" value="CNRY-d01"></div>',
        "CNRY-d01",
    ),
    (
        "split-code-fieldset",
        "<fieldset><legend>Verification</legend>"
        '<input maxlength="1" value="CNRY-d02"><input maxlength="1" value="CNRY-d02"></fieldset>',
        "CNRY-d02",
    ),
    ("contenteditable", '<div contenteditable="true" aria-label="PIN">CNRY-e01</div>', "CNRY-e01"),
    (
        "select-pin",
        '<select name="pin"><option selected value="CNRY-e02">CNRY-e02</option></select>',
        "CNRY-e02",
    ),
    ("output-token", '<output name="token">CNRY-e03</output>', "CNRY-e03"),
    (
        "datalist-token",
        '<datalist id="api-tokens"><option value="CNRY-e04"></option></datalist>',
        "CNRY-e04",
    ),
    ("cc-number", '<input autocomplete="cc-number" value="CNRY-f01">', "CNRY-f01"),
    ("cc-csc", '<input autocomplete="cc-csc" value="CNRY-f02">', "CNRY-f02"),
    ("cc-exp", '<input autocomplete="cc-exp" value="CNRY-f03">', "CNRY-f03"),
    ("meta-csrf", '<meta name="csrf-token" content="CNRY-g01">', "CNRY-g01"),
    ("meta-property", '<meta property="session-token" content="CNRY-g02">', "CNRY-g02"),
    ("script-bearer", '<script>var h = "Bearer CNRY-g03";</script>', "CNRY-g03"),
    (
        "next-data",
        '<script type="application/json" id="__NEXT_DATA__">{"accessToken":"CNRY-g04"}</script>',
        "CNRY-g04",
    ),
    ("noscript", "<noscript><img src='/p.gif' alt='CNRY-g05'></noscript>", "CNRY-g05"),
    ("template", '<template><input type="password" value="CNRY-g06"></template>', "CNRY-g06"),
    ("data-token", '<div data-token="CNRY-h01">x</div>', "CNRY-h01"),
    ("data-auth", '<div data-auth="CNRY-h02">x</div>', "CNRY-h02"),
    ("data-api-key", '<div data-api-key="CNRY-h03">x</div>', "CNRY-h03"),
    ("href-query", '<a href="/dl?token=CNRY-i01">dl</a>', "CNRY-i01"),
    ("action-matrix", '<form action="/x;jsessionid=CNRY-i02"></form>', "CNRY-i02"),
    ("src-signature", '<img src="/i.png?X-Amz-Signature=CNRY-i03" alt="">', "CNRY-i03"),
    ("href-fragment", '<a href="/cb#access_token=CNRY-i04">cb</a>', "CNRY-i04"),
    ("srcset", '<img srcset="/a.png?sig=CNRY-i05 1x, /b.png?sig=CNRY-i05 2x" alt="">', "CNRY-i05"),
    ("formaction", '<button formaction="/go?t=CNRY-i06">go</button>', "CNRY-i06"),
    ("poster", '<video poster="/v.jpg?t=CNRY-i07"></video>', "CNRY-i07"),
    (
        "meta-refresh",
        '<meta http-equiv="refresh" content="0; url=/next?token=CNRY-i08">',
        "CNRY-i08",
    ),
    ("comment", "<!-- session CNRY-j01 -->", "CNRY-j01"),
    ("srcdoc", '<iframe srcdoc="&lt;p&gt;CNRY-j02&lt;/p&gt;"></iframe>', "CNRY-j02"),
    ("jwt-attr", f'<div title="{JWT}">x</div>', JWT),
    ("hex-attr", f'<div data-x="{HEX}">x</div>', HEX),
    ("bearer-attr", '<div data-h="Bearer CNRY-k01.abcdefgh">x</div>', "CNRY-k01"),
    ("jwt-text", f"<p>token: {JWT}</p>", JWT),
]

BOTH = [Sensitivity.CLIENT_CONFIDENTIAL, Sensitivity.SYNTHETIC]


def _stamped_page(body: str) -> str:
    """Every element stamped the way LivePageSource stamps a live page."""
    root = lxml.html.document_fromstring(
        f"<!doctype html><html><head><title>t</title></head><body>{body}"
        "<p id='keep'>keep me</p></body></html>"
    )
    for i, el in enumerate(root.iter()):
        if isinstance(el.tag, str):
            el.set("data-sd-node", f"n{i}")
    return lxml.html.tostring(root, encoding="unicode", doctype="<!doctype html>")


def _names_nothing(dom: str) -> frozenset[str]:
    return frozenset()


@pytest.mark.unit
@pytest.mark.parametrize("sensitivity", BOTH, ids=lambda s: str(s))
@pytest.mark.parametrize(("shape", "markup", "canary"), SHAPES, ids=[s[0] for s in SHAPES])
def test_no_listed_leak_shape_reaches_the_stored_dom(
    shape: str, markup: str, canary: str, sensitivity: Sensitivity
) -> None:
    html = _stamped_page(markup)
    assert canary in html  # the shape really carries the canary before scrubbing
    page = FakePage(html, sensitivity=sensitivity)
    ev = asyncio.run(capture(page, run_key=RUN_KEY, masker=_names_nothing))
    assert canary not in ev.dom.value, f"{shape} leaked at {sensitivity}"
    assert "keep me" in ev.dom.value


# The rendered form-control shapes: these must also be in the screenshot floor (one predicate).
FORM_SHAPES = [s for s in SHAPES[: [x[0] for x in SHAPES].index("cc-exp") + 1]]
FORM_SHAPES = [s for s in FORM_SHAPES if s[0] != "datalist-token"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("shape", "markup", "canary"), FORM_SHAPES, ids=[s[0] for s in FORM_SHAPES]
)
def test_every_credential_form_shape_is_also_in_the_screenshot_floor(
    shape: str, markup: str, canary: str
) -> None:
    assert credential_input_nodes(_stamped_page(markup)), f"{shape} is not in the floor"


@pytest.mark.unit
@pytest.mark.parametrize(
    "benign",
    [
        '<input name="shipping_address" value="1 Test Street">',
        '<input name="passenger" value="Test Person One">',
        '<input name="bypass_reason" value="routine">',
        '<input name="passport_country" value="GB">',
        '<input name="search" placeholder="Search claims" value="AB12345678">',
        '<input name="claim_code" value="AB12345678">',
    ],
)
def test_the_widened_predicate_still_leaves_ordinary_fields_alone(benign: str) -> None:
    assert credential_input_nodes(_stamped_page(benign)) == frozenset()


@pytest.mark.unit
def test_client_confidential_keeps_what_the_cascade_reads() -> None:
    html = (
        '<html><body><table id="results" class="grid" role="table" aria-label="Claims">'
        '<tr><th id="h1">Claim Number</th></tr>'
        '<tr><td headers="h1" data-testid="claim-number" data-sd-node="n9">AB12345678</td></tr>'
        "</table><style>.grid{display:table}</style></body></html>"
    )
    out = scrub_dom(html, sensitivity=Sensitivity.CLIENT_CONFIDENTIAL)
    for kept in (
        'id="results"',
        'class="grid"',
        'role="table"',
        'aria-label="Claims"',
        'headers="h1"',
        'data-testid="claim-number"',
        'data-sd-node="n9"',
        "AB12345678",
        "Claim Number",
        ".grid{display:table}",
    ):
        assert kept in out


@pytest.mark.unit
def test_a_dom_with_nothing_to_scrub_is_returned_byte_for_byte() -> None:
    assert scrub_dom(BENCH_HTML, sensitivity=Sensitivity.CLIENT_CONFIDENTIAL) == BENCH_HTML


@pytest.mark.unit
def test_a_template_or_noscript_credential_no_longer_refuses_the_capture() -> None:
    """S-M5 re-check: R5 empties template/noscript, so an unstamped password inside one is not
    rendered, not stored, and not a reason to refuse."""
    html = (
        "<html><body><template><input type='password' value='CNRY-t1'></template>"
        "<noscript><input type='password' value='CNRY-t2'></noscript><p>x</p></body></html>"
    )
    for sensitivity in BOTH:
        ev = asyncio.run(
            capture(FakePage(html, sensitivity=sensitivity), run_key=RUN_KEY, masker=_names_nothing)
        )
        assert "CNRY-t1" not in ev.dom.value and "CNRY-t2" not in ev.dom.value


@pytest.mark.unit
def test_a_heal_reproduces_on_a_client_confidential_scrubbed_dom() -> None:
    html = BENCH_HTML.replace("Claim Number", "Claim Reference").replace(
        "<body>",
        '<body><!-- build 42 --><form action="/s?token=x"><input name="q" value="AB">'
        "</form><script>window.t='Bearer abcdefghij'</script>",
    )
    live, live_trace = resolve_cascade(
        StaticPageSource(html), CLAIM_NUMBER, NO_CACHE, record=False, run_key=RUN_KEY
    )
    ev = asyncio.run(
        capture(
            FakePage(html, sensitivity=Sensitivity.CLIENT_CONFIDENTIAL),
            run_key=RUN_KEY,
            masker=_names_nothing,
        )
    )
    assert ev.dom.value != html
    el, trace = replay(ev, CLAIM_NUMBER, NO_CACHE)
    assert (el.node_id, trace.winner) == (live.node_id, live_trace.winner)


def _outcomes(html: str) -> dict[str, tuple[str, int | None] | None]:
    spec = load_spec(Path("specs/testbench.yaml"))
    page = StaticPageSource(html)
    out: dict[str, tuple[str, int | None] | None] = {}
    for target in spec.targets:
        try:
            el, trace = resolve_cascade(page, target, NO_CACHE, record=False, run_key=RUN_KEY)
            out[target.name] = (el.node_id, trace.winner)
        except EscalateToHuman:
            out[target.name] = None
    return out


@pytest.mark.unit
def test_the_bench_results_page_resolves_every_field_the_same_after_scrubbing() -> None:
    raw = render_results(FIXTURE_DAY)
    scrubbed = scrub_dom(raw, sensitivity=Sensitivity.CLIENT_CONFIDENTIAL)
    before = _outcomes(raw)
    assert all(v is not None for v in before.values()), before
    assert _outcomes(scrubbed) == before


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
def test_every_mutated_bench_page_resolves_the_same_after_scrubbing(entry: object) -> None:
    raw = apply_all([entry.build(random.Random(0))], render_results(FIXTURE_DAY))  # type: ignore[attr-defined]
    scrubbed = scrub_dom(raw, sensitivity=Sensitivity.CLIENT_CONFIDENTIAL)
    assert _outcomes(scrubbed) == _outcomes(raw)


@pytest.mark.unit
def test_from_static_scrubs_with_the_same_floor() -> None:
    ev = Evidence.from_static(
        _stamped_page('<input name="api_key" value="CNRY-z1">'),
        run_key=RUN_KEY,
        sensitivity=Sensitivity.SYNTHETIC,
    )
    assert "CNRY-z1" not in ev.dom.value
