"""Task 33 additions to the ONE scrubber (heallog.scrub): credential header text and storage
state (R15/H6), and the N2-M2 URL-token shapes Task 19 left for this task."""

from __future__ import annotations

import pytest
from bok_core.boundary.sensitivity import Sensitivity

from selector_drift.heallog.scrub import form_control_node_ids, scrub_dom, scrub_dom_counted

CANARY = "CNRY-t33-9f1e"

# (id, markup, canary that must not survive)
LEAKS: list[tuple[str, str]] = [
    ("cookie-header", f"<pre>Cookie: session={CANARY}</pre>"),
    ("set-cookie-header", f"<p>Set-Cookie: sid={CANARY}; Path=/; HttpOnly</p>"),
    ("authorization-bearer-short", f"<p>Authorization: Basic {CANARY}</p>"),
    ("x-api-key", f"<code>X-API-Key: {CANARY}</code>"),
    ("csrf-header", f"<code>X-CSRF-Token: {CANARY}</code>"),
    ("storage-state", f'<pre>{{"cookies": [{{"name": "sid", "value": "{CANARY}"}}]}}</pre>'),
    ("local-storage", f'<pre>{{"origins": [{{"localStorage": [{{"value": "{CANARY}"}}]}}]}}</pre>'),
    ("title-attr-header", f'<span title="Cookie: a={CANARY}">x</span>'),
    # N2-M2
    ("css-image-set-block", f'<style>.a{{background:image-set("/a.png?sig={CANARY}" 1x)}}</style>'),
    ("css-image-set-attr", f"<div style=\"background:image-set('/a.png?sig={CANARY}' 1x)\"></div>"),
    ("data-prose-url", f'<div data-help="open /dl?token={CANARY} now"></div>'),
    ("alt-prose-url", f'<img alt="see https://cdn.example.invalid/x?token={CANARY} here">'),
    ("title-relative-token-url", f'<a title="/dl?token={CANARY}">x</a>'),
    ("aria-label-prose-url", f'<b aria-label="file at //cdn.example.invalid/f?k={CANARY}">x</b>'),
]


@pytest.mark.unit
@pytest.mark.parametrize("label", [Sensitivity.CLIENT_CONFIDENTIAL, Sensitivity.SYNTHETIC], ids=str)
@pytest.mark.parametrize("name,markup", LEAKS, ids=[n for n, _ in LEAKS])
def test_the_leak_is_scrubbed(name: str, markup: str, label: Sensitivity) -> None:
    html = f"<html><head></head><body>{markup}</body></html>"
    out, count = scrub_dom_counted(html, sensitivity=label)
    assert CANARY not in out, name
    assert count >= 1


# What a portal prints and a rung reads must survive (N2-M1 parity, insurance labels).
KEPT: list[tuple[str, str]] = [
    ("prior-authorization-label", "<td>Prior Authorization: Approved</td>", "Approved"),
    ("question-label", '<th aria-label="Paid?">Paid?</th>', 'aria-label="Paid?"'),
    ("equals-question", '<th title="a=b?">x</th>', 'title="a=b?"'),
    ("css-content", '<style>.p::after{content:"Amount?"}</style>', '"Amount?"'),
    ("prose", '<div data-help="see the claims list now">x</div>', "see the claims list now"),
    ("cookie-word", "<p>Cookie policy: we use none</p>", "we use none"),
]


@pytest.mark.unit
@pytest.mark.parametrize("name,markup,kept", KEPT, ids=[n for n, _, _ in KEPT])
def test_ordinary_text_is_kept(name: str, markup: str, kept: str) -> None:
    html = f"<html><head></head><body>{markup}</body></html>"
    assert kept in scrub_dom(html, sensitivity=Sensitivity.CLIENT_CONFIDENTIAL), name


@pytest.mark.unit
def test_an_unchanged_dom_counts_zero_and_is_returned_byte_for_byte() -> None:
    html = "<html><body><p>Claim AB12345678</p></body></html>"
    assert scrub_dom_counted(html, sensitivity=Sensitivity.SYNTHETIC) == (html, 0)


@pytest.mark.unit
def test_form_control_ids_are_what_default_deny_masks_by_itself() -> None:
    html = (
        '<html><body><input data-sd-node="a"><input data-sd-node="h" type="hidden">'
        '<textarea data-sd-node="t"></textarea><select data-sd-node="s"></select>'
        '<div data-sd-node="e" contenteditable></div><div data-sd-node="f" '
        'contenteditable="false"></div><td data-sd-node="c">x</td>'
        '<template><input data-sd-node="z"></template></body></html>'
    )
    assert form_control_node_ids(html) == frozenset({"a", "h", "t", "s", "e"})


# -- fix round 1 -------------------------------------------------------------------------------

ARIA = (
    '<html><body><div role="textbox" aria-label="Notes" data-sd-node="n1">{c}-box</div>'
    '<div role="slider" aria-valuetext="{c}-slider" data-sd-node="n3"></div>'
    '<span role="combobox" data-sd-node="n5">{c}-combo</span>'
    '<div role="searchbox" data-sd-node="n6">{c}-search</div></body></html>'
)


@pytest.mark.unit
def test_client_confidential_default_deny_covers_editable_roles_and_value_text() -> None:
    out = scrub_dom(ARIA.format(c=CANARY), sensitivity=Sensitivity.CLIENT_CONFIDENTIAL)
    assert CANARY not in out
    assert 'data-sd-node="n1"' in out and 'aria-label="Notes"' in out


@pytest.mark.unit
def test_editable_roles_are_masked_by_default_deny_too() -> None:
    assert form_control_node_ids(ARIA.format(c=CANARY)) >= {"n1", "n5", "n6"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        f"Download: https://files.example.invalid/r.pdf?token={CANARY}",
        f"link //cdn.example.invalid/x#{CANARY}",
        f"/dl?sig={CANARY}",
    ],
)
def test_a_url_printed_as_visible_text_loses_its_query(text: str) -> None:
    html = f"<html><body><p>{text}</p></body></html>"
    for label in (Sensitivity.CLIENT_CONFIDENTIAL, Sensitivity.SYNTHETIC):
        out = scrub_dom(html, sensitivity=label)
        assert CANARY not in out
        assert "<p>" in out
