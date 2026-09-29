"""Pure page renderers: (day, inputs) -> HTML. The routes call exactly these, and so do the tests
of Tasks 8-11A -- one rendering path, so the "before" page a test mutates is the page the bench
serves."""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from testbench.portal.data import COLUMNS, Claim, Status, dataset_for, display_row, find_claim

PAGE_SIZE = 50
TEMPLATES = Path(__file__).parent / "templates"
_ENV = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_ERROR_TEXT = {404: "Not found", 405: "Method not allowed"}


def _status(text: str) -> Status | None:
    try:
        return Status(text)
    except ValueError:
        return None


def _day(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text) if text else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class SearchQuery:
    claim_number: str = ""
    claimant: str = ""
    status: Status | None = None
    filed_from: dt.date | None = None
    filed_to: dt.date | None = None

    @classmethod
    def parse(
        cls,
        *,
        claim_number: str = "",
        claimant: str = "",
        status: str = "",
        filed_from: str = "",
        filed_to: str = "",
    ) -> SearchQuery:
        return cls(
            claim_number.strip().upper(),
            claimant.strip(),
            _status(status),
            _day(filed_from),
            _day(filed_to),
        )

    def matches(self, claim: Claim) -> bool:
        return (
            (not self.claim_number or claim.claim_number.startswith(self.claim_number))
            and (not self.claimant or self.claimant.casefold() in claim.claimant.casefold())
            and (self.status is None or claim.status is self.status)
            and (self.filed_from is None or claim.filed >= self.filed_from)
            and (self.filed_to is None or claim.filed <= self.filed_to)
        )

    def params(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.claim_number:
            out.append(("claim_number", self.claim_number))
        if self.claimant:
            out.append(("claimant", self.claimant))
        if self.status is not None:
            out.append(("status", self.status.value))
        if self.filed_from is not None:
            out.append(("filed_from", self.filed_from.isoformat()))
        if self.filed_to is not None:
            out.append(("filed_to", self.filed_to.isoformat()))
        return out


def result_set(day: dt.date, query: SearchQuery | None = None) -> list[Claim]:
    q = query or SearchQuery()
    return [c for c in dataset_for(day) if q.matches(c)]


def render_results(
    day: dt.date, page: int = 1, query: SearchQuery | None = None, *, mark_truth: bool = False
) -> str:
    """`mark_truth` adds data-sd-truth="<field>" to every data cell. Only Task 8's answer key
    passes it; no route does."""
    q = query or SearchQuery()
    found = result_set(day, q)
    pages = max(1, math.ceil(len(found) / PAGE_SIZE))
    page = min(max(page, 1), pages)
    window = found[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    links = [
        (n, "/results?" + urlencode([*q.params(), ("page", str(n))])) for n in range(1, pages + 1)
    ]
    return _ENV.get_template("results.html").render(
        signed_in=True,
        columns=COLUMNS,
        rows=[display_row(c) for c in window],
        total=len(found),
        page=page,
        pages=pages,
        links=links,
        mark_truth=mark_truth,
        export_href="/export.csv?" + urlencode(q.params()),
    )


def render_detail(day: dt.date, claim_number: str) -> str | None:
    claim = find_claim(day, claim_number)
    if claim is None:
        return None
    return _ENV.get_template("detail.html").render(
        signed_in=True, columns=COLUMNS, row=display_row(claim)
    )


def render_login(error: str | None = None) -> str:
    return _ENV.get_template("login.html").render(signed_in=False, error=error)


def render_search() -> str:
    return _ENV.get_template("search.html").render(
        signed_in=True, statuses=[s.value for s in Status]
    )


def render_error(status: int) -> str:
    return _ENV.get_template("error.html").render(
        signed_in=False, status=status, message=_ERROR_TEXT.get(status, "Something went wrong")
    )


def results_csv(day: dt.date, query: SearchQuery | None = None) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([label for _, label in COLUMNS])
    for claim in result_set(day, query):
        row = display_row(claim)
        writer.writerow([row[key] for key, _ in COLUMNS])
    return buffer.getvalue()
