"""Ground truth for a bench day. Importable by tests and testbench.score ONLY -- the
answer-key-isolation contract forbids every selector_drift module from importing any of testbench.

Values come from the dataset. Nodes come from a marked render: the true cells carry
data-sd-truth, the day's schedule is applied, and the marked nodes' absolute XPaths are the answer.
For VirtualisedRows the key lists every row the server sent; the browser keeps ten, and the
row-count contract (Task 29), not the resolver, is what must notice.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import lxml.html

from testbench.mutations.base import CatalogueEntry, parse
from testbench.portal.data import COLUMNS, display_row
from testbench.portal.hook import mutate_html
from testbench.portal.render import PAGE_SIZE, render_results, result_set

FIELDS: tuple[str, ...] = tuple(key for key, _ in COLUMNS)


def _check(field: str) -> None:
    if field not in FIELDS:
        raise KeyError(f"no such field on the bench: {field!r}")


def expected_values(day: dt.date, field: str, page: int = 1) -> list[str]:
    _check(field)
    window = result_set(day)[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    return [display_row(c)[field] for c in window]


def expected_xpaths(
    day: dt.date, field: str, page: int = 1, *, catalogue: Sequence[CatalogueEntry] | None = None
) -> list[str]:
    _check(field)
    rendered = render_results(day, page, mark_truth=True)
    marked = parse(mutate_html(day, rendered, catalogue=catalogue))
    tree = marked.getroottree()
    return [
        tree.getpath(el)
        for el in marked.xpath("//*[@data-sd-truth=$f]", f=field)
        if isinstance(el, lxml.html.HtmlElement)
    ]


def expected_selector(day: dt.date, field: str) -> str:
    """The first row's true node, as an absolute XPath. Kept for Task 26's single-element check."""
    paths = expected_xpaths(day, field)
    if not paths:
        raise LookupError(f"no {field} cell on the bench page for {day.isoformat()}")
    return paths[0]
