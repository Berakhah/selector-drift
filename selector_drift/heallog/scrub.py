"""The one DOM scrubber (Task 19 fix rounds 1-2). capture() calls it on every capture, and
Task 33's evidence writer reuses it -- there is deliberately no second credential predicate.

What it removes, by sensitivity:

CLIENT_CONFIDENTIAL -- default-deny on form values, so nothing depends on a heuristic
  (fix round 1: role=textbox/searchbox/combobox/spinbutton count as editable, and every
  aria-valuetext is blanked):
  every input's and button's `value`; `selected`/`value` on option and select; and the WHOLE
  SUBTREE of every textarea, output, datalist and [contenteditable] element -- descendants
  included (fix round 2, N-M3: a cell inside an editable region is gone, so a heal on it
  replays as EvidenceCorrupted, and a heal on a readonly display input's value cannot be
  reproduced; both are ruled trade-offs of default-deny).
PUBLIC / SYNTHETIC -- the heuristic credential floor: the same removals, but only on controls
  the shared predicate (`credential_controls`) flags.

At EVERY sensitivity (credential rules, not PII rules):
  - script / noscript / template bodies emptied (__NEXT_DATA__, inline bearer tokens, hidden
    login templates). <style> is kept: it is layout only, and StaticPageSource reads it to
    decide visibility -- only the URLs inside it are stripped.
  - HTML comments and processing instructions dropped; `srcdoc` dropped.
  - URLs: query string, fragment, ;matrix params and user:pass@ userinfo stripped, and a
    token-shaped path segment replaced -- in every URL-bearing attribute, srcset entry and
    meta refresh, in ANY other attribute value or meta content that looks like a URL
    (scheme://, protocol-relative //, or a whitespace-free path containing '?'), and inside
    every CSS url(...) / @import in style attributes and <style> blocks (fix round 2, N-I2).
  - meta content blanked when its name/property/http-equiv is credential-named; any other
    attribute whose NAME is credential-named (data-token, data-auth, data-api-key) blanked,
    unless its value is purely numeric or boolean (data-pin-count="2" is not a secret).
  - an attribute VALUE that looks like a JWT, a bearer token, or a long opaque secret is
    blanked. "Long opaque" is >= 32 characters of base64/hex alphabet with both a letter and
    a digit and no whitespace (the length of a 128-bit hex key). A bearer token is "Bearer"
    followed by >= 20 token characters including a digit, '.' or '=' (fix round 2, N-I1:
    "Basic Coverage" and "Token expiration date" are English).
  - JWTs and bearer tokens inside text, and inside the free-text attributes the rungs read
    (aria-label, title, alt, placeholder), are REPLACED in place -- never the opaque rule there.
  - Task 33 (R15/H6): HTTP credential header lines inside text or a free-text attribute --
    "Cookie: name=value", "Set-Cookie: ...", "Authorization: <scheme> <token>", and the
    X-API-Key / X-Auth-Token / X-CSRF-Token / X-XSRF-Token family -- keep their header name and
    lose their value. A text node shaped like Playwright storage state ("cookies": [ /
    "localStorage": [ / "sessionStorage": [) is emptied whole.
  - Task 33 (N2-M2): a URL-shaped quoted string anywhere in CSS (image-set("/a.png?sig=..")),
    a URL-shaped word inside a non-free-text attribute value ("open /dl?token=.. now"), and,
    inside the free-text attributes, a scheme or protocol-relative URL among words or a
    relative "/path?query" word are stripped like any other URL. "Paid?", "a=b?" and
    "Amount?" are not URLs and are kept (N2-M1 parity).

Never touched (the cascade reads them): KEPT_ATTRS, and the IDREF attributes (aria-labelledby,
aria-describedby, aria-controls, aria-owns, aria-activedescendant, aria-details,
aria-errormessage, for, headers, list, form) -- they point at ids that are themselves kept, so
blanking them would hide nothing and break the accessible-name computation.

The DOM is returned byte-for-byte when nothing matched, and re-serialised otherwise. Apart from
the subtrees emptied above (script/noscript/template everywhere; the CLIENT_CONFIDENTIAL form
subtrees), element structure is unchanged, so an lxml path minted before the scrub names the
same element after it (replay compares paths).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator

import lxml.html
from bok_core.boundary.sensitivity import Sensitivity
from lxml import etree
from lxml.html import HtmlElement

from selector_drift.resolve.page import NODE_ATTR

# -- the credential predicate: ONE definition, shared by the scrubber and the screenshot floor --

# Whole tokens (after camelCase/acronym splitting, accent folding and leetspeak folding).
_WORDS = frozenset(
    {
        "password", "passwd", "passwort", "kennwort", "contrasena", "senha", "wachtwoord",
        "pwd", "pswd", "pword", "pass", "passcode", "passphrase", "passe",
        "otp", "totp", "hotp", "mfa", "2fa", "pin", "secret", "token", "csrf", "xsrf",
        "apikey", "auth", "authorization", "bearer", "jwt", "sid", "sessionid", "sessid",
        "jsessionid", "phpsessid", "credential", "credentials", "cvv", "cvc", "csc",
        "privatekey", "accesskey", "secretkey",
    }
)  # fmt: skip
# Token roots matched as a prefix or suffix ("accessToken", "tokens", "clientsecret").
_ROOTS = ("token", "secret", "passw", "apikey", "sessionid")
# "pass..." tokens that are not credentials.
_NOT_PASS = frozenset(
    {"passenger", "passengers", "passport", "passage", "passed", "passing", "passive", "passes",
     "passthrough", "passover", "passable"}
)  # fmt: skip
# Root matches that are ordinary words.
_NOT_ROOT = frozenset({"secretary", "secretaries", "secretariat"})
# Adjacent-token phrases, within one descriptor.
_PHRASES = frozenset(
    {
        ("api", "key"), ("access", "key"), ("private", "key"), ("secret", "key"),
        ("session", "id"), ("session", "key"), ("session", "token"),
        ("verification", "code"), ("security", "code"), ("auth", "code"), ("access", "code"),
        ("recovery", "code"), ("backup", "code"), ("one", "time"), ("mot", "de", "passe"),
        ("security", "question"), ("security", "answer"), ("secret", "question"),
        ("secret", "answer"), ("pin", "code"), ("pin", "number"), ("card", "number"),
    }
)  # fmt: skip
# Words that make a short input inside a labelled group an OTP box.
_GROUP_WORDS = frozenset({"code", "otp", "verification", "passcode", "pin", "totp", "mfa"})
_CREDENTIAL_TYPES = frozenset({"password"})
_CREDENTIAL_AUTOCOMPLETE = frozenset({"current-password", "new-password", "one-time-code"})
_CONTROLS = ("input", "textarea", "select", "output", "datalist")
_SPLIT_OTP_MAXLEN = 8

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN = re.compile(r"[a-z0-9]+")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s"})


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", _CAMEL.sub(" ", text))
    folded = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    return _TOKEN.findall(folded)


def _token_hits(token: str) -> bool:
    for form in {token, token.translate(_LEET)}:
        if form in _WORDS:
            return True
        if form not in _NOT_ROOT and any(form.startswith(r) or form.endswith(r) for r in _ROOTS):
            return True
        if form.startswith("pass") and form not in _NOT_PASS and len(form) <= 12:
            return True
        if "pwd" in form or "pswd" in form:
            return True
    return False


def is_credential_text(text: str) -> bool:
    """True when a name, label or attribute name reads like a credential."""
    tokens = _tokens(text)
    if any(_token_hits(t) for t in tokens):
        return True
    for n in (2, 3):
        if any(tuple(tokens[i : i + n]) in _PHRASES for i in range(len(tokens) - n + 1)):
            return True
    return False


class _Index:
    """Label text by `for`, and text by id -- built once per DOM, so the predicate is one pass."""

    def __init__(self, root: HtmlElement) -> None:
        self.label_for: dict[str, list[str]] = {}
        self.by_id: dict[str, str] = {}
        for el in _elements(root):
            if el.tag == "label" and el.get("for"):
                self.label_for.setdefault(str(el.get("for")), []).append(el.text_content())
            ident = el.get("id")
            if ident and ident not in self.by_id:
                self.by_id[ident] = el.text_content()

    def labelled_by(self, el: HtmlElement) -> list[str]:
        return [self.by_id.get(ref, "") for ref in (el.get("aria-labelledby") or "").split()]

    def descriptors(self, el: HtmlElement) -> list[str]:
        own = [el.get(a) or "" for a in ("name", "id", "aria-label", "placeholder", "title")]
        own += self.label_for.get(el.get("id") or "", [])
        own += [a.text_content() for a in el.iterancestors("label")]
        own += self.labelled_by(el)
        return own

    def group_text(self, el: HtmlElement) -> str:
        """The nearest labelled wrapper: fieldset legend, role=group, or any aria-labelled
        ancestor -- how split OTP boxes are labelled."""
        for anc in el.iterancestors():
            if not isinstance(anc.tag, str):
                continue
            parts = [anc.get("aria-label") or "", *self.labelled_by(anc)]
            if anc.tag == "fieldset":
                parts += [lg.text_content() for lg in anc.iterchildren("legend")]
            text = " ".join(p for p in parts if p)
            if text or anc.tag == "fieldset" or anc.get("role") in ("group", "radiogroup"):
                return text
        return ""


def _elements(root: HtmlElement) -> Iterator[HtmlElement]:
    for el in root.iter():
        if isinstance(el, HtmlElement) and isinstance(el.tag, str):
            yield el


# ARIA roles whose element takes typed input (Task 33 fix round 1): treated like
# [contenteditable] -- emptied at CLIENT_CONFIDENTIAL and masked whole in the screenshot.
EDITABLE_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})
# Value-bearing ARIA attributes blanked at CLIENT_CONFIDENTIAL (a slider's spoken value).
_ARIA_VALUES = ("aria-valuetext",)


def _is_editable(el: HtmlElement) -> bool:
    flag = el.get("contenteditable")
    if flag is not None and flag.strip().lower() != "false":
        return True
    return (el.get("role") or "").strip().lower() in EDITABLE_ROLES


def _unrendered(el: HtmlElement) -> bool:
    return any(a.tag in ("template", "noscript") for a in el.iterancestors())


def _is_credential_control(el: HtmlElement, index: _Index) -> bool:
    kind = (el.get("type") or "text").strip().lower()
    if el.tag == "input" and kind in _CREDENTIAL_TYPES | {"hidden"}:
        return True
    auto = set((el.get("autocomplete") or "").lower().split())
    if auto & _CREDENTIAL_AUTOCOMPLETE or any(a.startswith("cc-") for a in auto):
        return True
    if any(is_credential_text(d) for d in index.descriptors(el) if d):
        return True
    maxlength = (el.get("maxlength") or "").strip()
    short = maxlength.isdigit() and int(maxlength) <= _SPLIT_OTP_MAXLEN
    group = index.group_text(el)
    if group and is_credential_text(group):
        return True
    return short and bool(set(_tokens(group)) & _GROUP_WORDS)


def credential_controls(root: HtmlElement) -> list[HtmlElement]:
    """Every rendered control whose value is, or may be, a credential. Controls inside
    template/noscript are not rendered (and their content is emptied by the scrub), so they are
    neither masked nor a reason to refuse."""
    candidates = [
        el
        for el in _elements(root)
        if (el.tag in _CONTROLS or _is_editable(el)) and not _unrendered(el)
    ]
    if not candidates:
        return []
    index = _Index(root)
    return [el for el in candidates if _is_credential_control(el, index)]


def parse(dom: str) -> HtmlElement:
    """Raises ValueError on a DOM lxml cannot parse (callers map it to their own error)."""
    try:
        return lxml.html.document_fromstring(dom)
    except etree.ParserError as exc:
        raise ValueError("the DOM could not be parsed") from exc


def credential_node_ids(dom: str) -> tuple[frozenset[str], int]:
    """The node ids of every credential control, and how many carry no node id."""
    found: set[str] = set()
    unmaskable = 0
    for el in credential_controls(parse(dom)):
        ident = el.get(NODE_ATTR)
        if ident:
            found.add(str(ident))
        else:
            unmaskable += 1
    return frozenset(found), unmaskable


# -- the scrub ------------------------------------------------------------------------------

_URL_ATTRS = frozenset(
    {"href", "src", "action", "formaction", "poster", "cite", "data", "background",
     "xlink:href", "ping", "longdesc", "manifest", "codebase", "usemap", "icon", "archive"}
)  # fmt: skip
# Read by the cascade and never a secret: exempt from the token-SHAPE rule (not from URL rules).
KEPT_ATTRS = frozenset(
    {"id", "class", "role", "headers", "for", "name", "type", "style", "lang", "dir",
     "colspan", "rowspan", "scope", "tabindex", "hidden", NODE_ATTR, "data-testid"}
)  # fmt: skip
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
_BEARER = re.compile(r"(?i)\b(bearer)\s+((?=[A-Za-z0-9._~+/=-]*[0-9.=])[A-Za-z0-9._~+/=-]{20,})")
_OPAQUE = re.compile(r"^[A-Za-z0-9+/_=.-]{32,}$")
_REFRESH_URL = re.compile(r"(?i)(url\s*=\s*)(['\"]?)([^'\"]*)")
_SCRUBBED = "[scrubbed]"
# They reference ids that are kept; blanking them hides nothing and breaks accessible names.
_IDREF_ATTRS = frozenset(
    {"aria-labelledby", "aria-describedby", "aria-controls", "aria-owns",
     "aria-activedescendant", "aria-details", "aria-errormessage", "for", "headers", "list",
     "form"}
)  # fmt: skip
# Free text the rungs read: only strict JWT/Bearer shapes are replaced here, in place.
_FREE_TEXT_ATTRS = frozenset({"aria-label", "title", "alt", "placeholder"})
_NUMERIC_OR_BOOL = re.compile(r"(?i)\s*(?:[-+]?\d+(?:\.\d+)?|true|false|yes|no|on|off)?\s*")
_URLISH = re.compile(r"^\s*(?:[A-Za-z][A-Za-z0-9+.-]*://|//)")
_USERINFO = re.compile(r"^(\s*(?:[A-Za-z][A-Za-z0-9+.-]*:)?//)[^/?#@\s]*@")
_CSS_URL = re.compile(r"(?is)(url\(\s*)(['\"]?)(.*?)(\2\s*\))")
_CSS_IMPORT = re.compile(r"(?i)(@import\s+)(['\"])(.*?)(\2)")
_CSS_STRING = re.compile(r"(\"|')((?:(?!\1)[^\\\n]|\\.)*)\1")
# Credential-bearing HTTP header lines (Task 33, R15/H6). The name is kept, the value goes.
# Authorization needs a scheme word, and Cookie a name=value pair, so the field labels an
# insurance portal prints ("Prior Authorization: Approved") are left alone.
_HEADER_LINES = (
    re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)([^\s=;:]+=[^\r\n<]*)"),
    re.compile(
        r"(?i)\b((?:proxy-)?authorization\s*:\s*)"
        r"((?:bearer|basic|digest|negotiate|token|apikey|hmac|aws4-hmac-sha256)\s+[^\r\n<]+)"
    ),
    re.compile(r"(?i)\b((?:x-)?(?:api-key|auth-token|csrf-token|xsrf-token)\s*:\s*)(\S[^\r\n<]*)"),
)
# Playwright storage-state / web-storage dumps: the whole text node goes.
_STORAGE_STATE = re.compile(r'(?i)"(?:cookies|localStorage|sessionStorage)"\s*:\s*\[')


def _looks_like_url(value: str, *, free_text: bool = False) -> bool:
    """scheme:// or protocol-relative //. Otherwise -- and never for the free-text attributes
    (fix round 3, N2-M1: aria-label="Paid?" is a question, not a URL) -- a whitespace-free
    value with '?' that also carries '/' or '=' (a path or a query string)."""
    v = value.strip()
    if _URLISH.match(v):
        return True
    if free_text or not v or re.search(r"\s", v):
        return False
    return "?" in v and ("/" in v or "=" in v)


def _strip_css(css: str) -> str:
    """Every url(...) and @import in a stylesheet or a style attribute stripped, and (N2-M2)
    any other quoted string in it that looks like a URL (image-set, custom properties)."""
    css = _CSS_URL.sub(lambda m: m[1] + m[2] + _strip_url(m[3]) + m[4], css)
    css = _CSS_IMPORT.sub(lambda m: m[1] + m[2] + _strip_url(m[3]) + m[4], css)
    return _CSS_STRING.sub(
        lambda m: m[1] + (_strip_url(m[2]) if _looks_like_url(m[2]) else m[2]) + m[1], css
    )


def _strict_hits(text: str) -> bool:
    return bool(
        _JWT.search(text) or _BEARER.search(text) or any(h.search(text) for h in _HEADER_LINES)
    )


def _replace_strict(text: str) -> str:
    """JWTs, bearer tokens and credential header values replaced in place; everything else
    left as written."""
    for header in _HEADER_LINES:
        text = header.sub(lambda m: m[1] + _SCRUBBED, text)
    text = _BEARER.sub(lambda m: f"{m[1]} {_SCRUBBED}", text)
    return _JWT.sub(_SCRUBBED, text)


def _strip_prose_urls(value: str, *, free_text: bool) -> str:
    """N2-M2: a URL among words. Each whitespace-separated word that is a URL is stripped.
    In a free-text attribute only a scheme or protocol-relative URL, or a relative word
    starting with '/' that carries a query, counts (so "Paid?" and "a=b?" stay)."""

    def word(match: re.Match[str]) -> str:
        w = match.group(0)
        if free_text:
            urlish = bool(_URLISH.match(w)) or (w.startswith("/") and "?" in w)
        else:
            urlish = _looks_like_url(w)
        return _strip_url(w) if urlish else w

    return re.sub(r"\S+", word, value)


def _strip_url(url: str) -> str:
    """Userinfo, query, fragment and ;matrix params gone; a token-shaped path segment
    replaced."""
    url = _USERINFO.sub(lambda m: m[1], url)
    cut = re.sub(r";[^/]*", "", re.split(r"[?#]", url, maxsplit=1)[0])
    return "/".join(_SCRUBBED if _token_shaped(seg) else seg for seg in cut.split("/"))


def _strip_srcset(value: str) -> str:
    out = []
    for candidate in value.split(","):
        parts = candidate.strip().split(None, 1)
        if parts:
            out.append(" ".join([_strip_url(parts[0]), *parts[1:]]))
    return ", ".join(out)


def _token_shaped(value: str) -> bool:
    v = value.strip()
    if _JWT.search(v) or _BEARER.search(v):
        return True
    return bool(_OPAQUE.fullmatch(v) and re.search(r"[A-Za-z]", v) and re.search(r"[0-9]", v))


class _Scrub:
    def __init__(self, root: HtmlElement, *, deny_all: bool) -> None:
        self.root = root
        self.deny_all = deny_all
        self.changes = 0  # every value, subtree, node or text the scrub removed or rewrote

    def _set(self, el: HtmlElement, attr: str, value: str | None) -> None:
        current = el.get(attr)
        if value is None:
            if current is not None:
                del el.attrib[attr]
                self.changes += 1
        elif current != value:
            el.set(attr, value)
            self.changes += 1

    def _empty(self, el: HtmlElement) -> None:
        if el.text or len(el):
            for child in list(el):
                el.remove(child)
            el.text = None
            self.changes += 1

    def _drop_preserving_tail(self, node: etree._Element) -> None:
        parent = node.getparent()
        if parent is None:
            return
        tail = node.tail
        prev = node.getprevious()
        if tail:
            if prev is not None:
                prev.tail = (prev.tail or "") + tail
            else:
                parent.text = (parent.text or "") + tail
        parent.remove(node)
        self.changes += 1

    def form_values(self) -> None:
        # The credential floor always runs (it also empties a credential select's option
        # text); CLIENT_CONFIDENTIAL then denies every remaining form value.
        self._clear(credential_controls(self.root), credential=True)
        if self.deny_all:
            self._clear(
                [
                    el
                    for el in _elements(self.root)
                    if el.tag in _CONTROLS or el.tag in ("option", "button") or _is_editable(el)
                ],
                credential=False,
            )
            for el in _elements(self.root):
                for attr in _ARIA_VALUES:
                    self._set(el, attr, None)

    def _clear(self, targets: list[HtmlElement], *, credential: bool) -> None:
        for el in targets:
            if el.tag in ("input", "button"):
                self._set(el, "value", None)
            elif el.tag in ("textarea", "output", "datalist") or _is_editable(el):
                self._empty(el)
            elif el.tag == "select":
                self._set(el, "value", None)
                for opt in el.iter("option"):
                    self._set(opt, "selected", None)
                    self._set(opt, "value", None)
                    if credential:
                        self._empty(opt)
            elif el.tag == "option":
                self._set(el, "selected", None)
                self._set(el, "value", None)

    def executable(self) -> None:
        for el in list(self.root.iter("script", "noscript", "template")):
            if isinstance(el, HtmlElement):
                self._empty(el)

    def stylesheets(self) -> None:
        for el in self.root.iter("style"):
            if isinstance(el, HtmlElement) and el.text:
                stripped = _strip_css(el.text)
                if stripped != el.text:
                    el.text = stripped
                    self.changes += 1

    def comments(self) -> None:
        for node in list(self.root.iter(etree.Comment, etree.ProcessingInstruction)):
            self._drop_preserving_tail(node)
        for sibling in (*self.root.itersiblings(preceding=True), *self.root.itersiblings()):
            if isinstance(sibling, etree._Comment | etree._ProcessingInstruction):
                self.changes += 1  # outside <html>: dropped by re-serialisation

    def attributes(self) -> None:
        for el in _elements(self.root):
            if el.tag == "meta":
                keyed = " ".join(el.get(a) or "" for a in ("name", "property", "http-equiv"))
                if is_credential_text(keyed):
                    self._set(el, "content", "")
                elif (el.get("http-equiv") or "").lower() == "refresh":
                    content = el.get("content") or ""
                    self._set(
                        el,
                        "content",
                        _REFRESH_URL.sub(lambda m: m[1] + m[2] + _strip_url(m[3]), content),
                    )
                elif _looks_like_url(el.get("content") or ""):
                    self._set(el, "content", _strip_url(el.get("content") or ""))
            for attr in list(el.attrib):
                value = el.get(attr) or ""
                bare = attr.removeprefix("data-").removeprefix("aria-")
                if attr == "srcdoc":
                    self._set(el, attr, None)
                elif attr in _URL_ATTRS:
                    self._set(el, attr, _strip_url(value))
                elif attr in ("srcset", "imagesrcset"):
                    self._set(el, attr, _strip_srcset(value))
                elif attr == "style":
                    self._set(el, attr, _strip_css(value))
                elif attr in KEPT_ATTRS or attr in _IDREF_ATTRS:
                    continue  # read by the cascade
                elif is_credential_text(bare) and not _NUMERIC_OR_BOOL.fullmatch(value):
                    self._set(el, attr, "")
                elif attr in _FREE_TEXT_ATTRS:
                    self._set(el, attr, _replace_strict(_strip_prose_urls(value, free_text=True)))
                elif _looks_like_url(value):
                    self._set(el, attr, _strip_url(value))
                elif _token_shaped(value):
                    self._set(el, attr, "")
                elif re.search(r"\s", value):
                    self._set(el, attr, _strip_prose_urls(value, free_text=False))

    def text(self) -> None:
        for el in _elements(self.root):
            for slot in ("text", "tail"):
                value = getattr(el, slot)
                if not value:
                    continue
                if _STORAGE_STATE.search(value):
                    setattr(el, slot, _SCRUBBED)
                    self.changes += 1
                    continue
                cleaned = _strip_prose_urls(_replace_strict(value), free_text=True)
                if cleaned != value:
                    # Credential header values, JWT/bearer tokens, and (fix round 1) the query,
                    # fragment and token path segments of a URL printed as visible text.
                    setattr(el, slot, cleaned)
                    self.changes += 1


def form_control_node_ids(dom: str) -> frozenset[str]:
    """Node ids of every rendered input, textarea, select and [contenteditable] -- exactly what
    LiveCapture's CLIENT_CONFIDENTIAL default-deny pass masks by itself, whatever their ids
    (hidden inputs paint nothing). N-M4: capture() need not ask the live page to find these
    by id at CLIENT_CONFIDENTIAL, so a re-rendered control is masked, not refused."""
    found: set[str] = set()
    for el in _elements(parse(dom)):
        if (el.tag in ("input", "textarea", "select") or _is_editable(el)) and not _unrendered(el):
            ident = el.get(NODE_ATTR)
            if ident:
                found.add(str(ident))
    return frozenset(found)


def scrub_dom_counted(html: str, *, sensitivity: Sensitivity) -> tuple[str, int]:
    """scrub_dom, and how many removals or rewrites it made (Task 33's manifest records it)."""
    root = parse(html)
    scrub = _Scrub(root, deny_all=sensitivity is Sensitivity.CLIENT_CONFIDENTIAL)
    # Executable/hidden content first, so a control inside a template is gone, not judged.
    scrub.executable()
    scrub.form_values()
    scrub.comments()
    scrub.stylesheets()
    scrub.attributes()
    scrub.text()
    if not scrub.changes:
        return html, 0
    doctype = root.getroottree().docinfo.doctype or None
    return str(lxml.html.tostring(root, encoding="unicode", doctype=doctype)), scrub.changes


def scrub_dom(html: str, *, sensitivity: Sensitivity) -> str:
    """The DOM with every credential (and, at CLIENT_CONFIDENTIAL, every form value) removed.

    Returned unchanged, byte for byte, when nothing matched. Raises ValueError on a DOM that
    cannot be parsed."""
    return scrub_dom_counted(html, sensitivity=sensitivity)[0]


__all__ = [
    "KEPT_ATTRS",
    "credential_controls",
    "credential_node_ids",
    "form_control_node_ids",
    "is_credential_text",
    "parse",
    "scrub_dom",
    "scrub_dom_counted",
]
