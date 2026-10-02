"""What a rung reads: a page, snapshotted once (R9/C6, P1).

Rungs take a PageSource, never a browser handle. Two implementations share one parser and
one role/name computation (aria.py) and differ in exactly two things -- how a node is
identified and how visibility is decided:

- StaticPageSource (here): node_id is lxml's el.getroottree().getpath(el), exactly, so it
  is directly comparable with the bench's answer key (Task 8) and Task 27's locate();
  visibility is inferred from markup and inline / <style> CSS.
- LivePageSource (browser/page_source.py): node_id is the injected data-sd-node value;
  visibility is what the browser computed, in the same evaluate() that injected the ids.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import lxml.html
from cssselect import SelectorError
from lxml import etree
from lxml.html import HtmlElement

from selector_drift.resolve.aria import (
    NON_RENDERED,
    NameContext,
    accessible_name,
    ancestors,
    element_children,
    role_of,
    tag_of,
)

NODE_ATTR = "data-sd-node"
# A path getpath() could have minted, and nothing else: anchored, so it still refuses XPath
# injection ("| //*", "..", predicates). Tag segments allow '-', '.' and '_' because getpath
# emits custom-element tags verbatim (FrameworkMarkup's angular dialect wraps in <app-root>).
_STATIC_ID = re.compile(r"^(?:/[a-z][a-z0-9._-]*(?:\[\d+\])?)+$")
_PX = re.compile(r"^(-?\d+(?:\.\d+)?)px$")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)


@dataclass(frozen=True)
class AxNode:
    """One node of the accessibility tree: a role, its computed name, the DOM node behind it."""

    role: str
    name: str
    node_id: str
    children: tuple[AxNode, ...] = ()

    def walk(self) -> Iterator[AxNode]:
        yield self
        for child in self.children:
            yield from child.walk()


class PageSource(Protocol):
    def dom(self) -> str: ...

    def ax(self) -> AxNode: ...

    def query(self, selector: str) -> list[HtmlElement]: ...

    def node_id(self, node: HtmlElement) -> str: ...

    def visible(self, node: HtmlElement) -> bool: ...

    # Additions to the five names above, all read-only and all computed from the same snapshot.
    def node(self, node_id: str) -> HtmlElement | None: ...

    def root(self) -> HtmlElement: ...

    def role(self, node: HtmlElement) -> str | None: ...

    def name(self, node: HtmlElement) -> str: ...


def _declarations(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split(";"):
        prop, sep, value = part.partition(":")
        if sep:
            out[prop.strip().casefold()] = value.replace("!important", "").strip().casefold()
    return out


def _style_rules(css: str) -> Iterator[tuple[str, dict[str, str]]]:
    """Top-level rules of a stylesheet. @-blocks (media queries) are skipped, not guessed at."""
    css = _COMMENT.sub("", css)
    i, n = 0, len(css)
    while i < n:
        brace = css.find("{", i)
        if brace < 0:
            return
        prelude = css[i:brace].strip()
        depth, j = 1, brace + 1
        while j < n and depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        if not prelude.startswith("@"):
            yield prelude, _declarations(css[brace + 1 : j - 1])
        i = j


def _px(value: str | None) -> float | None:
    m = _PX.match(value or "")
    return float(m.group(1)) if m else (0.0 if value == "0" else None)


class ParsedPage:
    """Everything both PageSources share. Subclasses decide identity and visibility."""

    def __init__(self, html: str) -> None:
        if not html.strip():
            raise ValueError("a PageSource needs a DOM; got an empty string")
        self._html = html
        self._root: HtmlElement = lxml.html.fromstring(html)
        self._by_id: dict[str, HtmlElement] = {}
        labels: defaultdict[str, list[HtmlElement]] = defaultdict(list)
        for el in self.elements():
            ident = el.get("id")
            if ident and ident not in self._by_id:
                self._by_id[ident] = el
            if tag_of(el) == "label" and el.get("for"):
                labels[el.get("for") or ""].append(el)
        self._names = NameContext(hidden=self.ax_hidden, by_id=self._by_id, labels_for=labels)
        self._role_memo: dict[HtmlElement, str | None] = {}
        self._name_memo: dict[HtmlElement, str] = {}
        self._ax: AxNode | None = None

    # -- the protocol --------------------------------------------------------------------
    def dom(self) -> str:
        return self._html

    def root(self) -> HtmlElement:
        return self._root

    def query(self, selector: str) -> list[HtmlElement]:
        """CSS, or XPath when the selector starts with '/' or '('. A bad selector matches
        nothing -- rung 1 then fails, which is the correct outcome for a broken selector."""
        try:
            if selector.startswith(("/", "(")):
                found = self._root.xpath(selector)
            else:
                found = self._root.cssselect(selector)
        except (etree.XPathError, SelectorError):
            return []
        if not isinstance(found, list):
            return []
        return [e for e in found if isinstance(e, HtmlElement) and isinstance(e.tag, str)]

    def role(self, node: HtmlElement) -> str | None:
        if node not in self._role_memo:
            self._role_memo[node] = role_of(node)
        return self._role_memo[node]

    def name(self, node: HtmlElement) -> str:
        if node not in self._name_memo:
            self._name_memo[node] = accessible_name(node, self._names)
        return self._name_memo[node]

    def visible(self, node: HtmlElement) -> bool:
        """H9: rendered, not aria-hidden, a box bigger than 1x1 px, on the page.

        Parity rule (documented 2026-09-26). A StaticPageSource has no layout, so for it an
        element is invisible when it or an ancestor is aria-hidden, display:none, carries the
        `hidden` attribute, or has a zero-size / off-screen INLINE or <style> declaration. A
        LivePageSource applies the same markup rule AND requires a real, on-page bounding box.
        The two agree on anything decidable from markup (the aria-hidden HiddenDecoy variant);
        they may disagree only where layout alone hides an element, and only in the direction
        of the live source seeing less."""
        return not self.ax_hidden(node) and self.boxed(node)

    def ax(self) -> AxNode:
        if self._ax is None:
            title = next((squashed(t) for t in self._root.iter("title")), "")
            self._ax = AxNode(
                role="document",
                name=title,
                node_id=self.node_id(self._root),
                children=tuple(self._ax_children(self._root)),
            )
        return self._ax

    def node_id(self, node: HtmlElement) -> str:
        raise NotImplementedError

    def node(self, node_id: str) -> HtmlElement | None:
        raise NotImplementedError

    # -- visibility, decided by the subclass ---------------------------------------------
    def ax_hidden(self, node: HtmlElement) -> bool:
        raise NotImplementedError

    def boxed(self, node: HtmlElement) -> bool:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------------------------
    def elements(self) -> Iterator[HtmlElement]:
        for el in self._root.iter():
            if isinstance(el, HtmlElement) and isinstance(el.tag, str):
                yield el

    def by_id(self, ident: str) -> HtmlElement | None:
        return self._by_id.get(ident)

    def _ax_children(self, el: HtmlElement) -> list[AxNode]:
        out: list[AxNode] = []
        for child in element_children(el):
            if self.ax_hidden(child):
                continue
            kids = self._ax_children(child)
            role = self.role(child)
            if role is None:
                out.extend(kids)
            else:
                out.append(
                    AxNode(
                        role=role,
                        name=self.name(child),
                        node_id=self.node_id(child),
                        children=tuple(kids),
                    )
                )
        return out


def squashed(el: HtmlElement) -> str:
    return " ".join(el.text_content().split())


class StaticPageSource(ParsedPage):
    """A stored DOM. No browser, no layout engine -- so no bbox(), by design (Task 19)."""

    def __init__(self, html: str) -> None:
        super().__init__(html)
        self._styles: dict[HtmlElement, dict[str, str]] = defaultdict(dict)
        for style in self._root.iter("style"):
            for prelude, decls in _style_rules(style.text or ""):
                for sel in prelude.split(","):
                    try:
                        matched = self._root.cssselect(sel.strip())
                    except SelectorError:
                        continue  # :hover and friends: not a static fact
                    for el in matched:
                        self._styles[el].update(decls)
        for el in self.elements():
            if el.get("style"):
                self._styles[el].update(_declarations(el.get("style") or ""))
        self._hidden_memo: dict[HtmlElement, bool] = {}
        self._boxed_memo: dict[HtmlElement, bool] = {}

    def node_id(self, node: HtmlElement) -> str:
        return str(node.getroottree().getpath(node))

    def node(self, node_id: str) -> HtmlElement | None:
        if not _STATIC_ID.fullmatch(node_id):
            return None
        hits = self.query(node_id)
        return hits[0] if len(hits) == 1 else None

    def _style(self, el: HtmlElement) -> dict[str, str]:
        return self._styles.get(el, {})

    def _hard_hidden(self, el: HtmlElement) -> bool:
        """Hidden in a way no descendant can undo."""
        return (
            tag_of(el) in NON_RENDERED
            or el.get("hidden") is not None
            or (el.get("aria-hidden") or "").casefold() == "true"
            or self._style(el).get("display") == "none"
            or (tag_of(el) == "input" and (el.get("type") or "").casefold() == "hidden")
        )

    def ax_hidden(self, node: HtmlElement) -> bool:
        if node not in self._hidden_memo:
            hidden = any(self._hard_hidden(e) for e in (node, *ancestors(node)))
            if not hidden:
                for e in (node, *ancestors(node)):  # nearest explicit visibility wins
                    vis = self._style(e).get("visibility")
                    if vis is not None:
                        hidden = vis in ("hidden", "collapse")
                        break
            self._hidden_memo[node] = hidden
        return self._hidden_memo[node]

    def _own_box_hidden(self, el: HtmlElement, *, self_node: bool) -> bool:
        s = self._style(el)
        width, height = _px(s.get("width")), _px(s.get("height"))
        tiny = (width is not None and width <= 1) or (height is not None and height <= 1)
        if tiny and (self_node or s.get("overflow") == "hidden"):
            return True
        if s.get("clip", "").replace(" ", "") in (
            "rect(0,0,0,0)",
            "rect(0000)",
            "rect(1px,1px,1px,1px)",
        ):
            return True
        if s.get("clip-path", "").replace(" ", "") in ("inset(50%)", "inset(100%)"):
            return True
        if s.get("position") in ("absolute", "fixed"):
            for side in ("left", "top"):
                offset = _px(s.get(side))
                if offset is not None and offset <= -500:
                    return True
        return False

    def boxed(self, node: HtmlElement) -> bool:
        if node not in self._boxed_memo:
            clipped = self._own_box_hidden(node, self_node=True) or any(
                self._own_box_hidden(a, self_node=False) for a in ancestors(node)
            )
            self._boxed_memo[node] = not clipped
        return self._boxed_memo[node]


__all__ = [
    "NODE_ATTR",
    "AxNode",
    "PageSource",
    "ParsedPage",
    "StaticPageSource",
    "squashed",
]
