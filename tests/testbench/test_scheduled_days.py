"""Every class composed the way the schedule composes them: one or two benign classes in a
random order, then sometimes one adversarial class on top (Task 8)."""

from __future__ import annotations

import datetime as dt

import pytest

from testbench.answers.key import FIELDS
from testbench.mutations.base import apply_all, parse, serialise
from testbench.mutations.catalogue import CATALOGUE
from testbench.portal.render import PAGE_SIZE, render_results
from testbench.schedule import mutations_for, scheduled_names

START = dt.date(2026, 9, 1)
DAYS = [START + dt.timedelta(days=i) for i in range(120)]
YEAR = [START + dt.timedelta(days=i) for i in range(365)]


@pytest.mark.unit
def test_every_class_is_scheduled_within_a_year() -> None:
    seen = {name for day in YEAR for name in scheduled_names(day, "testbench")}
    assert seen == {e.name for e in CATALOGUE}


@pytest.mark.unit
def test_every_scheduled_day_keeps_the_answer_key_intact() -> None:
    """Task 8's attributes-only rule, for real schedules rather than one class at a time: the
    marked twin keeps fifty true cells per field and differs from the served page by the marker
    alone -- whichever classes the day drew, in whichever order."""
    page, marked_page = render_results(START), render_results(START, mark_truth=True)
    for day in DAYS:
        chosen = mutations_for(day, "testbench")
        marked = parse(apply_all(chosen, marked_page))
        for field in FIELDS:
            assert len(marked.xpath("//*[@data-sd-truth=$f]", f=field)) == PAGE_SIZE, (day, field)
        for el in marked.xpath("//*[@data-sd-truth]"):
            del el.attrib["data-sd-truth"]
        assert serialise(marked) == serialise(parse(apply_all(chosen, page))), day
