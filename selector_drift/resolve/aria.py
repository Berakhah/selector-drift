"""Role and accessible-name computation over a parsed DOM.

One implementation serves both PageSources (R9): StaticPageSource runs it over a stored
snapshot, LivePageSource over the live page's serialised DOM, so the two cannot disagree
about a name. Agreement with Chromium's own accessibility tree is checked in e2e
(tests/resolve/test_page_source_parity.py), not assumed.

Scope: the subset of ARIA 1.2 / HTML-AAM / accname 1.2 a data portal uses -- tables and
grids, headers, form labels, aria-label, aria-labelledby, img alt, caption, legend. Not
implemented, deliberately: CSS generated content, shadow DOM, aria-owns, and embedded-control
values inside a name. A page that needs them fails to resolve and escalates; it never guesses.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lxml.html import HtmlElement


def _words(spec: str) -> frozenset[str]:
    return frozenset(spec.split())


def _pairs(spec: str) -> dict[str, str]:
    return {k: v for k, _, v in (p.partition("=") for p in spec.split())}


KNOWN_ROLES = _words(
    "alert article banner button caption cell checkbox columnheader combobox complementary "
    "contentinfo definition dialog document figure form grid gridcell group heading img link "
    "list listbox listitem main menu menuitem meter navigation option paragraph progressbar "
    "radio region row rowgroup rowheader searchbox separator slider spinbutton status switch "
    "tab table tablist tabpanel term textbox tree treegrid treeitem"
)
NAME_FROM_CONTENT = _words(
    "button cell checkbox columnheader gridcell heading link menuitem option radio row "
    "rowheader switch tab term treeitem"
)
CELL_ROLES = frozenset({"cell", "gridcell", "columnheader", "rowheader"})
DATA_CELL_ROLES = frozenset({"cell", "gridcell"})
TABLE_ROLES = frozenset({"table", "grid", "treegrid"})
NON_RENDERED = _words("head script style template noscript meta link title base")
_INLINE = _words(
    "a abbr b bdi bdo cite code data dfn em i img kbd label mark q s samp small span strong "
    "sub sup time u var"
)


# HTML-AAM implicit roles, for the tags whose role does not depend on context.
_SIMPLE = _pairs(
    "article=article aside=complementary button=button caption=caption dd=definition "
    "details=group dialog=dialog dt=term fieldset=group figure=figure form=form h1=heading "
    "h2=heading h3=heading h4=heading h5=heading h6=heading hr=separator li=listitem main=main "
    "menu=list meter=meter nav=navigation ol=list option=option output=status p=paragraph "
    "progress=progressbar table=table tbody=rowgroup textarea=textbox tfoot=rowgroup "
    "thead=rowgroup tr=row ul=list"
)
_INPUT = _pairs(
    "button=button checkbox=checkbox email=textbox image=button number=spinbutton radio=radio "
    "range=slider reset=button search=searchbox submit=button tel=textbox text=textbox "
    "url=textbox password=textbox"
)
_SECTIONING = frozenset({"article", "aside", "main", "nav", "section"})

Hidden = Callable[[HtmlElement], bool]


def tag_of(el: HtmlElement) -> str:
    tag = el.tag
    return tag.lower() if isinstance(tag, str) else ""


def element_children(el: HtmlElement) -> list[HtmlElement]:
    return [c for c in el if isinstance(c, HtmlElement) and isinstance(c.tag, str)]


def parent_of(el: HtmlElement) -> HtmlElement | None:
    parent = el.getparent()
    return parent if isinstance(parent, HtmlElement) else None


def ancestors(el: HtmlElement) -> list[HtmlElement]:
    out: list[HtmlElement] = []
    node = parent_of(el)
    while node is not None:
        out.append(node)
        node = parent_of(node)
    return out


def role_of(el: HtmlElement) -> str | None:
    for token in (el.get("role") or "").split():
        t = token.casefold()
        if t in ("presentation", "none"):
            return None
        if t in KNOWN_ROLES:
            return t
    return _implicit_role(el)


def _implicit_role(el: HtmlElement) -> str | None:
    tag = tag_of(el)
    if tag == "td":
        return "gridcell" if _table_role(el) in ("grid", "treegrid") else "cell"
    if tag == "th":
        return _th_role(el)
    if tag in ("a", "area"):
        return "link" if el.get("href") is not None else None
    if tag == "input":
        return _INPUT.get((el.get("type") or "text").casefold())
    if tag == "select":
        size = el.get("size") or "1"
        multi = el.get("multiple") is not None or (size.isdigit() and int(size) > 1)
        return "listbox" if multi else "combobox"
    if tag == "img":
        return None if el.get("alt") == "" else "img"
    if tag in ("header", "footer"):
        if any(tag_of(a) in _SECTIONING for a in ancestors(el)):
            return None
        return "banner" if tag == "header" else "contentinfo"
    if tag == "section":
        named = el.get("aria-label") or el.get("aria-labelledby")
        return "region" if named else None
    return _SIMPLE.get(tag)


def _table_role(el: HtmlElement) -> str | None:
    for a in ancestors(el):
        if tag_of(a) == "table" or (a.get("role") or "").casefold() in TABLE_ROLES:
            return role_of(a)
    return None


def _th_role(th: HtmlElement) -> str:
    scope = (th.get("scope") or "").casefold()
    if scope in ("row", "rowgroup"):
        return "rowheader"
    if scope in ("col", "colgroup"):
        return "columnheader"
    row = parent_of(th)
    if row is None or tag_of(row) != "tr":
        return "columnheader"
    group = parent_of(row)
    if group is not None and tag_of(group) == "thead":
        return "columnheader"
    cells = [c for c in element_children(row) if tag_of(c) in ("td", "th")]
    return "columnheader" if all(tag_of(c) == "th" for c in cells) else "rowheader"


@dataclass(frozen=True)
class NameContext:
    hidden: Hidden  # excluded from the AX tree
    by_id: Mapping[str, HtmlElement]
    labels_for: Mapping[str, Sequence[HtmlElement]]  # id -> <label for=id> elements


def accessible_name(el: HtmlElement, ctx: NameContext) -> str:
    return " ".join(_text_alt(el, ctx, frozenset(), referenced=False, recursing=False).split())


def _text_alt(
    el: HtmlElement,
    ctx: NameContext,
    seen: frozenset[HtmlElement],
    *,
    referenced: bool,
    recursing: bool,
) -> str:
    if el in seen:
        return ""
    seen = seen | {el}
    if ctx.hidden(el) and not referenced:
        return ""
    if not referenced:
        refs = [ctx.by_id[i] for i in (el.get("aria-labelledby") or "").split() if i in ctx.by_id]
        if refs:
            joined = " ".join(
                _text_alt(r, ctx, seen, referenced=True, recursing=True) for r in refs
            )
            if joined.strip():
                return joined
    label = (el.get("aria-label") or "").strip()
    if label:
        return label
    native = _native(el, ctx, seen, referenced)
    if native is not None and native.strip():
        return native
    if recursing or role_of(el) in NAME_FROM_CONTENT:
        content = _from_content(el, ctx, seen, referenced)
        if content.strip():
            return content
    title = (el.get("title") or "").strip()
    if title:
        return title
    if tag_of(el) in ("input", "textarea"):
        return (el.get("placeholder") or "").strip()
    return ""


def _native(
    el: HtmlElement, ctx: NameContext, seen: frozenset[HtmlElement], referenced: bool
) -> str | None:
    tag = tag_of(el)
    kind = (el.get("type") or "text").casefold()
    if tag in ("img", "area") or (tag == "input" and kind == "image"):
        alt = el.get("alt")
        return None if alt is None else str(alt)
    if tag == "input" and kind in ("button", "submit", "reset"):
        default = {"submit": "Submit", "reset": "Reset"}.get(kind, "")
        return str(el.get("value") or default)
    if tag in ("input", "select", "textarea", "meter", "progress", "output"):
        labels = list(ctx.labels_for.get(el.get("id") or "", ()))
        labels += [a for a in ancestors(el) if tag_of(a) == "label"]
        texts = [_from_content(lab, ctx, seen, referenced) for lab in labels]
        return " ".join(t for t in texts if t.strip()) or None
    wrapper = {"table": "caption", "fieldset": "legend", "figure": "figcaption"}.get(tag)
    if wrapper is not None:
        for child in element_children(el):
            if tag_of(child) == wrapper:
                return _from_content(child, ctx, seen, referenced)
    return None


def _from_content(
    el: HtmlElement, ctx: NameContext, seen: frozenset[HtmlElement], referenced: bool
) -> str:
    parts = [el.text or ""]
    for child in el:
        if not isinstance(child, HtmlElement) or not isinstance(child.tag, str):
            parts.append(child.tail or "")
            continue
        tag = tag_of(child)
        if tag in NON_RENDERED:
            parts.append(child.tail or "")
            continue
        sep = "" if tag in _INLINE else " "
        if tag == "br":
            sep = " "
        sub = _text_alt(child, ctx, seen, referenced=referenced, recursing=True)
        parts.append(f"{sep}{sub}{sep}")
        parts.append(child.tail or "")
    return "".join(parts)
