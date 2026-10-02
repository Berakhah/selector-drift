from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from selector_drift.config import get_settings
from selector_drift.resolve import cascade
from selector_drift.resolve.cache import NO_CACHE
from selector_drift.resolve.cascade import REGISTERED_RUNGS, resolve_cascade
from selector_drift.resolve.escalation import EscalateToHuman
from selector_drift.resolve.page import StaticPageSource
from selector_drift.resolve.rungs import r5_visual
from selector_drift.resolve.rungs.r5_visual import CORPUS_ENV, VisualRung
from selector_drift.resolve.trace import CascadeTrace, Family, RungAttempt
from selector_drift.spec.schema import FieldTarget
from tests.resolve.conftest import CLAIM_NUMBER, RUN_KEY

UNRESOLVABLE = "<html><body><p>nothing here but Test Person One and ZZ99999999</p></body></html>"

SECRET_CLAIMANT = "ZELDA-QUILLFEATHER-7731"
SECRET_VALUE = "XQ99999999"
# Deliberately unstructured -- no header, label or role any rung recognises -- so rungs 1-4
# genuinely miss (rather than a table whose header text happens to satisfy rung 2/3/4's
# structural match regardless of whether the cell value passes the spec's format assertion,
# which the cascade itself does not enforce outside rung 1's cache check).
SECRET_PAGE = (
    f"<html><body><p>{SECRET_CLAIMANT} filed a claim, reference {SECRET_VALUE}</p></body></html>"
)


@pytest.fixture
def unresolvable_page() -> StaticPageSource:
    return StaticPageSource(UNRESOLVABLE)


@pytest.mark.unit
def test_rung_five_is_last_and_has_no_family() -> None:
    assert (VisualRung.rung_id, VisualRung.family) == (5, Family.NONE)


@pytest.mark.unit
def test_all_five_rungs_are_registered_in_order() -> None:
    """Task 41 asserts the same."""
    assert [r.rung_id for r in REGISTERED_RUNGS] == [1, 2, 3, 4, 5]


@pytest.mark.unit
def test_rung_five_escalates_and_never_returns_an_element(unresolvable_page, tmp_path) -> None:
    with pytest.raises(EscalateToHuman):
        VisualRung(corpus=tmp_path).resolve(unresolvable_page, CLAIM_NUMBER)


@pytest.mark.unit
def test_rung_five_records_a_corpus_case(unresolvable_page, tmp_path) -> None:
    with pytest.raises(EscalateToHuman) as caught:
        VisualRung(corpus=tmp_path).resolve(unresolvable_page, CLAIM_NUMBER)
    [case] = list(tmp_path.glob("*.json"))
    assert caught.value.case == case
    data = json.loads(case.read_text(encoding="utf-8"))
    assert data["field"] == "claim_number" and data["model_calls"] == 0
    assert len(data["dom_sha256"]) == 64


@pytest.mark.unit
def test_the_corpus_case_never_contains_the_page(unresolvable_page, tmp_path) -> None:
    """Never #6: a DOM is redacted before storage and never leaves the client's boundary.
    The corpus holds a hash and a pointer; the redacted page lives in the evidence bundle."""
    with pytest.raises(EscalateToHuman):
        VisualRung(corpus=tmp_path).resolve(unresolvable_page, CLAIM_NUMBER)
    text = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert "Test Person One" not in text and "ZZ99999999" not in text and "<p>" not in text


@pytest.mark.unit
def test_a_scheduled_escalation_carries_the_cascade_and_the_run_key(
    unresolvable_page, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        cascade, "REGISTERED_RUNGS", [*cascade.REGISTERED_RUNGS[:-1], VisualRung(corpus=tmp_path)]
    )
    rows: list[CascadeTrace] = []

    def recorder(
        *,
        run_key: str,
        portal: str,
        target: FieldTarget,
        trace: CascadeTrace,
        node_id: str | None,
        ms: int,
    ) -> int:
        rows.append(trace)
        return 7

    with pytest.raises(EscalateToHuman) as caught:
        resolve_cascade(
            unresolvable_page, CLAIM_NUMBER, NO_CACHE, run_key=RUN_KEY, recorder=recorder
        )
    data = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert data["run_key"] == RUN_KEY
    assert [a["rung"] for a in data["attempts"]] == [1, 2, 3, 4]
    trace = caught.value.trace
    assert trace is not None and trace.winner is None and trace.resolution_id == 7
    assert [a.outcome for a in trace.attempts][-1] == "escalated" and len(rows) == 1


@pytest.mark.unit
def test_replay_never_reaches_rung_five(unresolvable_page, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(CORPUS_ENV, str(tmp_path))
    get_settings.cache_clear()  # Settings is cached per process (Task 5)
    with pytest.raises(EscalateToHuman):
        resolve_cascade(unresolvable_page, CLAIM_NUMBER, NO_CACHE, record=False)
    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.unit
def test_the_corpus_location_comes_from_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(CORPUS_ENV, str(tmp_path / "c"))
    get_settings.cache_clear()
    assert VisualRung().corpus == tmp_path / "c" == get_settings().escalation_corpus
    get_settings.cache_clear()


@pytest.mark.unit
def test_the_same_page_and_field_is_one_case_not_many(unresolvable_page, tmp_path) -> None:
    for _ in range(3):
        with pytest.raises(EscalateToHuman):
            VisualRung(corpus=tmp_path).resolve(unresolvable_page, CLAIM_NUMBER)
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.unit
def test_rung_five_makes_no_model_call_and_opens_no_socket() -> None:
    """Zero model calls in v1 (Global Constraints). Task 1's allow-list test covers bok_core;
    this covers every other way out of the process."""
    tree = ast.parse(Path(r5_visual.__file__).read_text(encoding="utf-8"))
    imported = {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    imported |= {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert imported <= {
        "__future__",
        "datetime",
        "hashlib",
        "json",
        "os",
        "collections",
        "pathlib",
        "selector_drift",
    }


@pytest.mark.unit
def test_an_attempt_list_is_recorded_as_given(unresolvable_page, tmp_path) -> None:
    tried = (RungAttempt(2, Family.TEXT_SEMANTIC, "no_match", 1),)
    with pytest.raises(EscalateToHuman):
        VisualRung(corpus=tmp_path).escalate(
            unresolvable_page, CLAIM_NUMBER, run_key=None, attempts=tried
        )
    data = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert data["attempts"] == [tried[0].as_json()]


@pytest.mark.unit
def test_the_corpus_case_from_a_full_cascade_run_never_leaks_page_text(
    monkeypatch, tmp_path
) -> None:
    """Drive rungs 1-4 to miss on a page containing a recognisable secret (a claimant name and
    a claim-number-shaped value), then escalate. Miss.detail strings written by rungs 1-4 can
    embed page text (header text, accessible names, cell values) -- the case must never carry
    them. RungAttempt (what the cascade accumulates and what the case stores) has no detail
    field at all, only rung/family/outcome/ms, so this is also a regression test for that."""
    monkeypatch.setattr(
        cascade, "REGISTERED_RUNGS", [*cascade.REGISTERED_RUNGS[:-1], VisualRung(corpus=tmp_path)]
    )
    page = StaticPageSource(SECRET_PAGE)

    def recorder(**_: object) -> int:
        return 1

    with pytest.raises(EscalateToHuman) as caught:
        resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, run_key=RUN_KEY, recorder=recorder)
    case = caught.value.case
    assert case is not None
    text = case.read_text(encoding="utf-8")
    assert SECRET_CLAIMANT not in text
    assert SECRET_VALUE not in text
    assert "<" not in text  # no HTML
    assert "css_path" not in text and "nth-of-type" not in text  # no selector strings
    assert "screenshot" not in text.casefold() and "base64" not in text.casefold()
    assert str(caught.value).find(SECRET_CLAIMANT) == -1
    assert str(caught.value).find(SECRET_VALUE) == -1


@pytest.mark.unit
def test_a_field_name_cannot_escape_the_corpus_directory(tmp_path) -> None:
    """FieldName is pattern-constrained (^[a-z][a-z0-9_]{0,63}$) so a real FieldTarget can never
    carry '../../etc/x' -- but record_case must not trust that invariant blindly. Bypass
    pydantic validation with model_construct to prove the corpus write still lands inside
    `corpus`, never above it."""
    hostile = FieldTarget.model_construct(
        name="../../../etc/passwd",
        description="hostile",
        label=None,
        relation=None,
        assertions=[],
        distributional=False,
    )
    page = StaticPageSource(UNRESOLVABLE)
    with pytest.raises(EscalateToHuman) as caught:
        VisualRung(corpus=tmp_path).escalate(page, hostile, run_key=None, attempts=())
    case = caught.value.case
    assert case is not None
    assert case.resolve().is_relative_to(tmp_path.resolve())
    assert list(tmp_path.rglob("*.json")) == [case]
