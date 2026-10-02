"""TableToDiv: the <table> becomes a framework data grid of role-annotated <div>s.

The tag changes in place, so every attribute -- classes, ids, and on the answer key's twin the
data-sd-truth marker -- stays on the same node. Only the tag and a role are new.
"""

from __future__ import annotations

from lxml.html import HtmlElement

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import elements

TABLE_TAGS = ("table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td")
_ROLES = {
    "table": "table",
    "thead": "rowgroup",
    "tbody": "rowgroup",
    "tfoot": "rowgroup",
    "tr": "row",
    "td": "cell",
}


def _role(el: HtmlElement) -> str | None:
    if el.tag == "th":
        in_body = any(a.tag in ("tbody", "tfoot") for a in el.iterancestors())
        return "rowheader" if el.get("scope") == "row" or in_body else "columnheader"
    return _ROLES.get(str(el.tag))  # caption -> None: a plain div


class TableToDiv(MutationBase):
    def apply(self, html: str) -> str:
        doc = parse(html)
        for table in elements(doc.xpath("//table[not(ancestor::table)]")):
            nodes = [n for n in table.iter(*TABLE_TAGS) if isinstance(n, HtmlElement)]
            roles = [_role(n) for n in nodes]  # decided before any tag changes
            for node, role in zip(nodes, roles, strict=True):
                node.tag = "div"
                if role is not None and node.get("role") is None:
                    node.set("role", role)
        return serialise(doc)
