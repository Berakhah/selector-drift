"""The one DOM scrubber (Task 19 fix round 1, R5). capture() calls it on every capture, and
Task 33's evidence writer reuses it -- there is deliberately no second credential predicate.

What it removes, by sensitivity:

CLIENT_CONFIDENTIAL -- default-deny on form values, so nothing depends on a heuristic:
  every input's `value`; every textarea's text; `selected`/`value` on option and select; the
  contents of [contenteditable], output and datalist.
PUBLIC / SYNTHETIC -- the heuristic credential floor: the same removals, but only on controls
  `is_credential_control()` flags.

At EVERY sensitivity (these are credential rules, not PII rules, and cost the cascade nothing):
  - script / noscript / template bodies emptied (__NEXT_DATA__, inline bearer tokens, hidden
    login templates). <style> is kept: it is layout only, and StaticPageSource reads it to
    decide visibility, so dropping it would change what replay resolves.
  - HTML comments dropped; `srcdoc` dropped.
  - query string, fragment and ;matrix params stripped from every URL-bearing attribute, srcset
    entries and meta refresh -- pre-signed links and ?token= downloads are routine on portals.
  - meta content blanked when its name/property/http-equiv is credential-named; any attribute
    whose NAME is credential-named (data-token, data-auth, data-api-key) blanked.
  - any attribute VALUE that looks like a JWT, a bearer token, or a long opaque secret blanked;
    JWTs and bearer tokens in text are replaced. "Long" is >= 32 characters of base64/hex
    alphabet with both a letter and a digit and no whitespace: 32 is the length of a 128-bit
    hex key, the shortest secret we treat as worth hiding; human-readable values are shorter
    or contain spaces. The structural attributes the cascade reads (KEPT_ATTRS) are exempt.

The DOM is returned byte-for-byte when nothing matched, and re-serialised otherwise; element
structure is never changed, so an lxml path minted before the scrub names the same element
after it (replay compares paths).
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


def _is_editable(el: HtmlElement) -> bool:
    flag = el.get("contenteditable")
    return flag is not None and flag.strip().lower() != "false"


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
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_OPAQUE = re.compile(r"^[A-Za-z0-9+/_=.-]{32,}$")
_REFRESH_URL = re.compile(r"(?i)(url\s*=\s*)(['\"]?)([^'\"]*)")
_SCRUBBED = "[scrubbed]"


def _strip_url(url: str) -> str:
    """Query, fragment and ;matrix params gone; a token-shaped path segment replaced."""
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
        self.changed = False

    def _set(self, el: HtmlElement, attr: str, value: str | None) -> None:
        current = el.get(attr)
        if value is None:
            if current is not None:
                del el.attrib[attr]
                self.changed = True
        elif current != value:
            el.set(attr, value)
            self.changed = True

    def _empty(self, el: HtmlElement) -> None:
        if el.text or len(el):
            for child in list(el):
                el.remove(child)
            el.text = None
            self.changed = True

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
        self.changed = True

    def form_values(self) -> None:
        # The credential floor always runs (it also empties a credential select's option
        # text); CLIENT_CONFIDENTIAL then denies every remaining form value.
        self._clear(credential_controls(self.root), credential=True)
        if self.deny_all:
            self._clear(
                [
                    el
                    for el in _elements(self.root)
                    if el.tag in _CONTROLS or el.tag == "option" or _is_editable(el)
                ],
                credential=False,
            )

    def _clear(self, targets: list[HtmlElement], *, credential: bool) -> None:
        for el in targets:
            if el.tag == "input":
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

    def comments(self) -> None:
        for node in list(self.root.iter(etree.Comment, etree.ProcessingInstruction)):
            self._drop_preserving_tail(node)
        for sibling in (*self.root.itersiblings(preceding=True), *self.root.itersiblings()):
            if isinstance(sibling, etree._Comment | etree._ProcessingInstruction):
                self.changed = True  # outside <html>: dropped by re-serialisation

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
            for attr in list(el.attrib):
                value = el.get(attr) or ""
                bare = attr.removeprefix("data-").removeprefix("aria-")
                if attr == "srcdoc":
                    self._set(el, attr, None)
                elif attr in _URL_ATTRS:
                    self._set(el, attr, _strip_url(value))
                elif attr in ("srcset", "imagesrcset"):
                    self._set(el, attr, _strip_srcset(value))
                elif attr not in KEPT_ATTRS and (is_credential_text(bare) or _token_shaped(value)):
                    self._set(el, attr, "")

    def text(self) -> None:
        for el in _elements(self.root):
            for slot in ("text", "tail"):
                value = getattr(el, slot)
                if value and (_JWT.search(value) or _BEARER.search(value)):
                    scrubbed = _BEARER.sub(lambda m: f"{m[1]} {_SCRUBBED}", value)
                    setattr(el, slot, _JWT.sub(_SCRUBBED, scrubbed))
                    self.changed = True


def scrub_dom(html: str, *, sensitivity: Sensitivity) -> str:
    """The DOM with every credential (and, at CLIENT_CONFIDENTIAL, every form value) removed.

    Returned unchanged, byte for byte, when nothing matched. Raises ValueError on a DOM that
    cannot be parsed."""
    root = parse(html)
    scrub = _Scrub(root, deny_all=sensitivity is Sensitivity.CLIENT_CONFIDENTIAL)
    # Executable/hidden content first, so a control inside a template is gone, not judged.
    scrub.executable()
    scrub.form_values()
    scrub.comments()
    scrub.attributes()
    scrub.text()
    if not scrub.changed:
        return html
    doctype = root.getroottree().docinfo.doctype or None
    return str(lxml.html.tostring(root, encoding="unicode", doctype=doctype))


__all__ = [
    "KEPT_ATTRS",
    "credential_controls",
    "credential_node_ids",
    "is_credential_text",
    "parse",
    "scrub_dom",
]
