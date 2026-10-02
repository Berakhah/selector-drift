"""The bench page, unmutated and under one mutation class, with its answer key alongside.

`marked` is the same page rendered with data-sd-truth on every true cell (Task 7), put through
the same mutation instance -- Task 8's technique -- so `truth()` lists lxml paths that are
directly comparable with a StaticPageSource node_id.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import pytest
from lxml import html as lh

from selector_drift.resolve.page import StaticPageSource
from selector_drift.spec.loader import load_spec
from testbench.mutations.base import Mutation, apply_all
from testbench.mutations.catalogue import CATALOGUE
from testbench.portal.render import render_results
from tests.testbench.helpers import FIXTURE_DAY

SPEC = load_spec(Path("specs/testbench.yaml"))  # version 2 (Task 12 Step 0): six fields
CLAIM_NUMBER = next(t for t in SPEC.targets if t.name == "claim_number")
AMOUNT = next(t for t in SPEC.targets if t.name == "amount")
RESERVE = next(t for t in SPEC.targets if t.name == "reserve")
RUN_KEY = "testbench:2:2026-10-01"


@dataclass(frozen=True)
class BenchPage:
    html: str
    marked: str

    def source(self) -> StaticPageSource:
        return StaticPageSource(self.html)

    def truth(self, field: str) -> list[str]:
        root = lh.fromstring(self.marked)
        tree = root.getroottree()
        return [tree.getpath(e) for e in root.xpath("//*[@data-sd-truth=$f]", f=field)]

    def mutated(self, name: str) -> BenchPage:
        entry = next(e for e in CATALOGUE if e.name == name)
        return self.mutated_by(entry.build(random.Random(0)))

    def mutated_by(self, mutation: Mutation) -> BenchPage:
        """A specific instance -- e.g. HiddenDecoy(variant="aria-hidden") for the parity test."""
        return BenchPage(apply_all([mutation], self.html), apply_all([mutation], self.marked))


@pytest.fixture
def bench() -> BenchPage:
    return BenchPage(render_results(FIXTURE_DAY), render_results(FIXTURE_DAY, mark_truth=True))
