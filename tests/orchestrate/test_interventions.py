"""Task 29A: the intervention ledger (M1). 'Zero manual selector fixes' is falsifiable only if
every manual fix is a row.

Adapted from the brief: its two wiring tests (`run_once` observes the spec before resolving;
`quarantine_actions` records a manual promote) pin code Task 26A creates -- pipeline.py and
quarantine_actions.py do not exist yet, and the brief's own Step 7 hands that wiring to 26A's
tests. They are not reproduced here."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import AuditLog, Intervention
from selector_drift.db.session import Session, engine
from selector_drift.orchestrate.interventions import (
    observe_spec,
    record_cache_edit,
    record_intervention,
    spec_changes,
)
from selector_drift.spec.schema import TaskSpec
from tests.land.factory import SPEC_YAML

T0 = dt.datetime(2026, 10, 1, 4, 12, tzinfo=dt.UTC)


def _spec(**changes: object) -> TaskSpec:
    body = yaml.safe_load(SPEC_YAML)
    return TaskSpec.model_validate(body | changes)


@pytest.fixture
def ledger() -> Iterator[OrmSession]:
    with engine.begin() as c:
        c.execute(text("TRUNCATE interventions, audit_log RESTART IDENTITY CASCADE"))
    with Session() as s:
        yield s


def _rows(s: OrmSession) -> list[Intervention]:
    s.expire_all()
    return list(s.query(Intervention).order_by(Intervention.id))


@pytest.mark.integration
def test_first_sight_of_a_spec_is_a_baseline_not_an_edit(ledger) -> None:
    assert observe_spec("testbench", _spec(), now=T0) == []
    assert _rows(ledger) == []
    seen = ledger.query(AuditLog).filter_by(action="spec_seen", subject="testbench").one()
    assert seen.detail["spec"]["schedule"] == "12 4 * * *" and len(seen.detail["sha256"]) == 64


@pytest.mark.integration
def test_an_unchanged_spec_records_nothing(ledger) -> None:
    observe_spec("testbench", _spec(), now=T0)
    assert observe_spec("testbench", _spec(), now=T0 + dt.timedelta(days=1)) == []
    assert ledger.query(AuditLog).filter_by(action="spec_seen").count() == 1


@pytest.mark.integration
def test_a_spec_edit_between_runs_is_recorded_as_an_intervention(ledger) -> None:
    observe_spec("testbench", _spec(), now=T0)
    edited = yaml.safe_load(SPEC_YAML)
    edited["targets"][0]["description"] = "the reference cell"
    ids = observe_spec("testbench", TaskSpec.model_validate(edited), now=T0 + dt.timedelta(days=1))
    (row,) = _rows(ledger)
    assert ids == [row.id]
    assert row.kind == "spec_edit" and row.field == "targets.claim_number"
    assert row.before != row.after
    assert row.after is not None and row.after["target"]["description"] == "the reference cell"
    assert row.actor == "system:spec_loader"


@pytest.mark.integration
def test_a_schedule_edit_has_the_shape_derive_replays(ledger) -> None:
    # Amended 2026-09-26; pinned by Task 34B's test_a_schedule_edit_changes_the_expected_slots.
    observe_spec("testbench", _spec(), now=T0)
    observe_spec("testbench", _spec(schedule="12 4,16 * * *"), now=T0 + dt.timedelta(days=1))
    (row,) = _rows(ledger)
    assert (row.kind, row.field) == ("spec_edit", "schedule")
    assert row.before == {"schedule": "12 4 * * *"}
    assert row.after == {"schedule": "12 4,16 * * *"}
    assert row.at == T0 + dt.timedelta(days=1)


@pytest.mark.integration
def test_every_changed_field_is_its_own_row(ledger) -> None:
    observe_spec("testbench", _spec(), now=T0)
    observe_spec(
        "testbench",
        _spec(schedule="0 5 * * *", row_scope="each result card"),
        now=T0 + dt.timedelta(days=1),
    )
    assert {r.field for r in _rows(ledger)} == {"schedule", "row_scope"}


@pytest.mark.integration
def test_an_edit_back_is_another_edit(ledger) -> None:
    observe_spec("testbench", _spec(), now=T0)
    observe_spec("testbench", _spec(schedule="0 5 * * *"), now=T0 + dt.timedelta(days=1))
    observe_spec("testbench", _spec(), now=T0 + dt.timedelta(days=2))
    assert [(r.before, r.after) for r in _rows(ledger)] == [
        ({"schedule": "12 4 * * *"}, {"schedule": "0 5 * * *"}),
        ({"schedule": "0 5 * * *"}, {"schedule": "12 4 * * *"}),
    ]


@pytest.mark.integration
def test_specs_are_tracked_per_target(ledger) -> None:
    observe_spec("testbench", _spec(), now=T0)
    assert observe_spec("other", _spec(schedule="0 5 * * *"), now=T0) == []  # its own baseline
    assert _rows(ledger) == []


@pytest.mark.integration
def test_the_ledger_is_queryable_because_the_headline_depends_on_it(ledger) -> None:
    # Replaces the first draft's manual_fix_count(): the COUNT belongs to derive.py (34B).
    record_intervention(
        "cache_edit",
        "op1",
        "testbench",
        "amount",
        {"selector": "td:nth-child(3)"},
        {"selector": "td:nth-child(4)"},
        "hand-fixed after a reorder",
        at=T0,
    )
    since = ledger.query(Intervention).filter(Intervention.at >= T0).count()
    assert since == 1


@pytest.mark.integration
def test_a_cache_edit_is_an_intervention(ledger) -> None:
    rid = record_cache_edit(
        "op1", "testbench", "amount", {"selector": "a"}, {"selector": "b"}, "manual fix"
    )
    (row,) = _rows(ledger)
    assert (row.id, row.kind, row.actor, row.field) == (rid, "cache_edit", "op1", "amount")


@pytest.mark.unit
def test_an_intervention_needs_a_known_kind_and_an_actor() -> None:
    with pytest.raises(ValueError, match="kind"):
        record_intervention("hotfix", "op1", "testbench", "x", None, None, "r")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="actor"):
        record_intervention("spec_edit", "", "testbench", "x", None, None, "r")
    with pytest.raises(ValueError, match="actor"):
        record_intervention("spec_edit", "   ", "testbench", "x", None, None, "r")


@pytest.mark.unit
def test_a_target_field_called_schedule_cannot_collide_with_the_schedule() -> None:
    body: dict[str, Any] = yaml.safe_load(SPEC_YAML)
    old = TaskSpec.model_validate(body)
    body["targets"].append(
        {"name": "schedule", "description": "a column named schedule", "assertions": []}
    )
    new = TaskSpec.model_validate(body)
    ((field, before, after),) = spec_changes(old, new)
    assert field == "targets.schedule"
    assert before == {"target": None} and after["target"]["name"] == "schedule"


@pytest.mark.unit
def test_spec_changes_names_top_level_fields_and_skips_the_version() -> None:
    changes = spec_changes(_spec(), _spec(version=3, natural_key=["claim_number", "amount"]))
    assert [c[0] for c in changes] == ["natural_key"]
