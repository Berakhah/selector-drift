"""The one DOM scrubber (Task 19 fix rounds 1-2). capture() calls it on every capture, and
Task 33's evidence writer reuses it -- there is deliberately no second credential predicate.

What it removes, by sensitivity:

CLIENT_CONFIDENTIAL -- default-deny on form values, so nothing depends on a heuristic:
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


def _looks_like_url(value: str) -> bool:
    v = value.strip()
    return bool(_URLISH.match(v)) or (bool(v) and "?" in v and not re.search(r"\s", v))


def _strip_css(css: str) -> str:
    """Every url(...) and @import in a stylesheet or a style attribute, stripped."""
    css = _CSS_URL.sub(lambda m: m[1] + m[2] + _strip_url(m[3]) + m[4], css)
    return _CSS_IMPORT.sub(lambda m: m[1] + m[2] + _strip_url(m[3]) + m[4], css)


def _replace_strict(text: str) -> str:
    """JWTs and bearer tokens replaced in place; everything else left as written."""
    text = _BEARER.sub(lambda m: f"{m[1]} {_SCRUBBED}", text)
    return _JWT.sub(_SCRUBBED, text)


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
                    if el.tag in _CONTROLS or el.tag in ("option", "button") or _is_editable(el)
                ],
                credential=False,
            )

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
                    self.changed = True

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
                elif _looks_like_url(value):
                    self._set(el, attr, _strip_url(value))
                elif attr in _FREE_TEXT_ATTRS:
                    self._set(el, attr, _replace_strict(value))
                elif _token_shaped(value):
                    self._set(el, attr, "")

    def text(self) -> None:
        for el in _elements(self.root):
            for slot in ("text", "tail"):
                value = getattr(el, slot)
                if value and (_JWT.search(value) or _BEARER.search(value)):
                    setattr(el, slot, _replace_strict(value))
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
    scrub.stylesheets()
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
