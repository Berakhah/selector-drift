"""Part 2 of Task 26A (API spec 7.2): what happens after the gate, in order.

Redaction is proven before anything is written; a heal bundle's `before` is last-accepted AS IT
IS at heal time; last-accepted moves only on acceptance, after land.write returns, at the one
call site the cache write-back shares (C2 applied to evidence)."""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bok_core.boundary.egress import Plane
from bok_core.boundary.sensitivity import Sensitivity
from bok_core.provenance.claim import Claim, Method
from bok_core.provenance.source import DomSource
from sqlalchemy import text

from selector_drift.config import get_settings
from selector_drift.db.models import HealEvent, Quarantine, Run
from selector_drift.db.session import Session, engine
from selector_drift.extract.models import CandidateExtraction
from selector_drift.heallog.bundle import (
    AFTER_HTML,
    BEFORE_HTML,
    MANIFEST,
    PAGE_HTML,
    read_heal_manifest,
)
from selector_drift.heallog.capture import Evidence
from selector_drift.heallog.evidence import (
    PiiMasker,
    heal_bundle_dir,
    last_accepted_dir,
    redact_evidence,
    write_last_accepted,
)
from selector_drift.heallog.replay import HealTimeCache
from selector_drift.orchestrate import accept as accept_mod
from selector_drift.orchestrate.accept import RunState, settle
from selector_drift.resolve.cascade import resolve_cascade
from selector_drift.resolve.page import StaticPageSource
from selector_drift.resolve.trace import ResolvedElement
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget, Basis, ScopeRecord
from selector_drift.spec.loader import load_spec
from selector_drift.validate.gate import HealContext, ValidatedExtraction, gate
from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Outcome, Verdict

KEY = b"test-only-redaction-key-not-a-secret!"  # tests only; production reads SD_REDACTION_KEY
REPO = Path(__file__).resolve().parents[2]
SPEC = load_spec(REPO / "specs" / "testbench.yaml")
CLAIM_NUMBER = SPEC.targets[0]
RUN_KEY = "testbench:2:2026-09-07"
OLD_PAGE = """<!doctype html><html><body><table><thead><tr><th data-sd-node="n0">Claim Number</th></tr></thead>
<tbody><tr><td data-sd-node="n1" data-testid="claim-number">AB12345678</td></tr></tbody></table>
</body></html>"""  # noqa: E501
NEW_PAGE = OLD_PAGE.replace("Claim Number", "Claim Reference")  # label reword
# An identifier in an element with no node id: the screenshot cannot have masked it.
LEAKY_PAGE = NEW_PAGE.replace("</table>", "</table><p>test.person@example.invalid</p>")

Result = ValidatedExtraction | QuarantinedRun


class Passes:
    name = "Passes"

    def check(self, candidate: object, history: object, ctx: object) -> Verdict:
        return Verdict(
            check="Passes", outcome=Outcome.PASS, threshold=None, observed=None, reason="test"
        )


class Fails:
    name = "Fails"

    def check(self, candidate: object, history: object, ctx: object) -> Verdict:
        return Verdict(
            check="Fails", outcome=Outcome.FAIL, threshold="PASS", observed="FAIL", reason="test"
        )


class FakeCache:
    """Stands in for CacheWriter (records puts) and for the reader HealContext needs."""

    def __init__(self) -> None:
        self.puts: list[ResolvedElement] = []

    def get(self, target: str, field: str, spec_version: int | None = None) -> None:
        return None

    def put(self, validated: ValidatedExtraction, element: ResolvedElement, *, field: str) -> None:
        self.puts.append(element)


@pytest.fixture(autouse=True)
def evidence_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SD_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    monkeypatch.setenv("SD_LAKE_ROOT", str(tmp_path / "lake"))
    monkeypatch.setenv("SD_REDACTION_KEY", KEY.hex())
    get_settings.cache_clear()
    with engine.begin() as c:
        c.execute(
            text("TRUNCATE runs, heal_events, quarantine, heartbeats RESTART IDENTITY CASCADE")
        )
    yield
    get_settings.cache_clear()


@pytest.fixture
def lands(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    written: list[object] = []

    def write(v: object, attempt: int | None = None) -> Path:
        written.append(v)
        return Path("/lake/x.parquet")

    monkeypatch.setattr(accept_mod.land_writer, "write", write)
    return written


def _target() -> AuthorisedTarget:
    rec = ScopeRecord(
        target="testbench",
        basis=Basis.SELF_OWNED,
        authorising_party="me",
        authorised_on=dt.date(2026, 9, 1),
        expires_on=dt.date(2027, 9, 1),
        evidence_uri=None,
        evidence_sha256=None,
        origins=["https://bench.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="auto_promote",
        legal_review="self_drafted",
        warmup_runs=5,
    )
    return authorise(rec, today=dt.date(2026, 9, 7))


def _run(
    html: str, checks: Sequence[object], *, masked: bool = True
) -> tuple[Evidence, ResolvedElement, CandidateExtraction, Result, HealEvent]:
    """Resolve, extract one claim, gate, and record one heal -- the pipeline's shape."""
    ev = Evidence.from_static(
        html,
        run_key=RUN_KEY,
        sensitivity=Sensitivity.SYNTHETIC,
        masker=PiiMasker(KEY, Sensitivity.SYNTHETIC) if masked else None,
    )
    el, trace = resolve_cascade(
        StaticPageSource(html), CLAIM_NUMBER, HealTimeCache(None), record=False
    )
    claim = Claim(
        value="AB12345678",
        method=Method.DETERMINISTIC,
        source=DomSource(snapshot_id=ev.sha256, selector=el.selector, rung=el.rung_id),
        observed_at=datetime.now(UTC),
    )
    candidate = CandidateExtraction(
        rows=[{"claim_number": claim}],
        trace={"claim_number": trace},
        spec_version=2,
        run_key=RUN_KEY,
    )
    ctx = HealContext(
        healed=True, winning_rung=el.rung_id, page=None, target=CLAIM_NUMBER, cache=FakeCache()
    )
    result = gate(candidate, None, ctx, checks)  # type: ignore[arg-type]
    with Session() as s:
        s.merge(
            Run(
                run_key=RUN_KEY,
                target="testbench",
                spec_version=2,
                window=dt.date(2026, 9, 7),
                first_started_at=datetime.now(UTC),
                state="completed",
            )
        )
        s.flush()  # no ORM relationship orders the FK: the runs row goes first
        heal = HealEvent(
            run_key=RUN_KEY,
            target="testbench",
            field="claim_number",
            broke_at=datetime.now(UTC),
            winning_rung=el.rung_id,
            selector_after=el.selector,
            anchor_after=el.anchor.model_dump(mode="json"),
            trace=[],
            verdicts=[],
        )
        s.add(heal)
        s.commit()
        s.refresh(heal)
        s.expunge(heal)
    return ev, el, candidate, result, heal


def _settle(
    ev: Evidence,
    el: ResolvedElement,
    candidate: CandidateExtraction,
    result: Result,
    heal: HealEvent,
    cache: FakeCache,
) -> RunState:
    return settle(
        target=_target(),
        spec=SPEC,
        candidate=candidate,
        result=result,
        evidence=ev,
        elements={"claim_number": el},
        heals=[heal],
        cache=cache,  # type: ignore[arg-type]
        cache_before={"claim_number": None},
        redaction_key=KEY,
    )


def _seed_last_accepted(html: str) -> bytes:
    old = Evidence.from_static(
        html,
        run_key="testbench:2:2026-09-06",
        sensitivity=Sensitivity.SYNTHETIC,
        masker=PiiMasker(KEY, Sensitivity.SYNTHETIC),
    )
    write_last_accepted(redact_evidence(old, key=KEY), "testbench", Plane.CLIENT)
    return (last_accepted_dir("testbench") / PAGE_HTML).read_bytes()


@pytest.mark.integration
def test_a_quarantined_run_does_not_replace_last_accepted(lands: list[object]) -> None:
    old = _seed_last_accepted(OLD_PAGE)
    old_manifest = (last_accepted_dir("testbench") / MANIFEST).read_bytes()
    cache = FakeCache()
    ev, el, candidate, result, heal = _run(NEW_PAGE, [Fails()])
    assert _settle(ev, el, candidate, result, heal, cache) == "quarantined"
    assert (last_accepted_dir("testbench") / PAGE_HTML).read_bytes() == old
    assert (last_accepted_dir("testbench") / MANIFEST).read_bytes() == old_manifest
    assert lands == [] and cache.puts == []  # C2: no lake row, no cache row
    with Session() as s:
        q = s.get(Quarantine, RUN_KEY)
        assert q is not None and q.data_uri and Path(q.data_uri).exists()  # the data is kept


@pytest.mark.integration
def test_heal_bundle_before_is_the_last_accepted_page(lands: list[object]) -> None:
    old = _seed_last_accepted(OLD_PAGE)
    ev, el, candidate, result, heal = _run(NEW_PAGE, [Passes()])
    assert _settle(ev, el, candidate, result, heal, FakeCache()) == "healed"
    bundle = heal_bundle_dir(heal.id)
    assert (bundle / BEFORE_HTML).read_bytes() == old  # copied at heal time
    assert (last_accepted_dir("testbench") / PAGE_HTML).read_bytes() == (
        bundle / AFTER_HTML
    ).read_bytes()  # then moved, on acceptance
    manifest = read_heal_manifest(bundle)
    assert manifest.before is not None and manifest.before.run_key == "testbench:2:2026-09-06"


@pytest.mark.integration
def test_first_heal_of_a_target_reports_before_not_captured(lands: list[object]) -> None:
    ev, el, candidate, result, heal = _run(NEW_PAGE, [Passes()])
    _settle(ev, el, candidate, result, heal, FakeCache())
    manifest = read_heal_manifest(heal_bundle_dir(heal.id))
    assert manifest.before is None and manifest.before_unavailable_reason is None
    assert BEFORE_HTML not in manifest.files
    assert '"before"' not in (heal_bundle_dir(heal.id) / MANIFEST).read_text()


@pytest.mark.integration
def test_a_redaction_failure_quarantines_and_writes_nothing(lands: list[object]) -> None:
    """Ledger 670: RedactionFailed is recorded with its reason and the run quarantined -- even
    when the gate had passed it -- and no evidence, lake row or cache row is written."""
    old = _seed_last_accepted(OLD_PAGE)
    cache = FakeCache()
    ev, el, candidate, result, heal = _run(LEAKY_PAGE, [Passes()], masked=False)
    assert isinstance(result, ValidatedExtraction)
    assert _settle(ev, el, candidate, result, heal, cache) == "quarantined"
    assert (last_accepted_dir("testbench") / PAGE_HTML).read_bytes() == old
    assert not heal_bundle_dir(heal.id).exists()
    assert lands == [] and cache.puts == []
    with Session() as s:
        q = s.get(Quarantine, RUN_KEY)
        assert q is not None and q.reason == "evidence redaction failed"
        assert q.verdicts[0]["check"] == "EvidenceRedaction"
        assert "not masked" in q.verdicts[0]["reason"]
        assert [v["check"] for v in q.verdicts[1:]] == ["Passes"]  # the gate's verdicts kept
        row = s.get(HealEvent, heal.id)
        assert row is not None and row.evidence_uri is None


@pytest.mark.integration
def test_a_redaction_failure_on_a_quarantined_run_keeps_the_gates_verdicts(
    lands: list[object],
) -> None:
    ev, el, candidate, result, heal = _run(LEAKY_PAGE, [Fails()], masked=False)
    assert _settle(ev, el, candidate, result, heal, FakeCache()) == "quarantined"
    with Session() as s:
        q = s.get(Quarantine, RUN_KEY)
        assert q is not None
        assert [v["check"] for v in q.verdicts] == ["EvidenceRedaction", "Fails"]


@pytest.mark.integration
def test_last_accepted_moves_only_after_land_write_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _seed_last_accepted(OLD_PAGE)
    cache = FakeCache()

    def refuse(v: object, attempt: int | None = None) -> Path:
        raise OSError("lake unavailable")

    monkeypatch.setattr(accept_mod.land_writer, "write", refuse)
    ev, el, candidate, result, heal = _run(NEW_PAGE, [Passes()])
    with pytest.raises(OSError):
        _settle(ev, el, candidate, result, heal, cache)
    assert cache.puts == []  # C2
    assert (last_accepted_dir("testbench") / PAGE_HTML).read_bytes() == old


@pytest.mark.integration
def test_a_heal_is_replayed_from_its_bundle_and_the_outcome_recorded(
    lands: list[object],
) -> None:
    ev, el, candidate, result, heal = _run(NEW_PAGE, [Passes()])
    _settle(ev, el, candidate, result, heal, FakeCache())
    with Session() as s:
        row = s.get(HealEvent, heal.id)
        assert row is not None
        assert row.evidence_uri == str(heal_bundle_dir(heal.id))
        assert row.replayed_ok is True


@pytest.mark.integration
def test_an_accepted_run_lands_then_writes_the_cache_with_its_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    cache = FakeCache()

    def write(v: object, attempt: int | None = None) -> Path:
        order.append(f"land:{attempt}")
        return Path("/lake/x.parquet")

    monkeypatch.setattr(accept_mod.land_writer, "write", write)
    real = cache.put

    def put(validated: ValidatedExtraction, element: ResolvedElement, *, field: str) -> None:
        order.append("cache")
        real(validated, element, field=field)

    cache.put = put  # type: ignore[method-assign]
    ev, el, candidate, result, heal = _run(NEW_PAGE, [Passes()])
    state = settle(
        target=_target(),
        spec=SPEC,
        candidate=candidate,
        result=result,
        evidence=ev,
        elements={"claim_number": el},
        heals=[heal],
        cache=cache,  # type: ignore[arg-type]
        cache_before={"claim_number": None},
        redaction_key=KEY,
        attempt=3,
    )
    assert state == "healed" and order == ["land:3", "cache"]
    assert cache.puts == [el]


@pytest.mark.unit
def test_the_three_acceptance_writes_have_one_call_site() -> None:
    """C2 + API spec 7.2. land.write, cache.put and the last-accepted replacement appear
    together, in one function, and nowhere else in the product."""
    calls: dict[str, list[str]] = {"land_writer.write": [], ".put": [], "commit_last_accepted": []}
    movers: list[str] = []  # what may replace last-accepted, outside its defining module
    for path in sorted(Path("selector_drift").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            src = ast.unparse(node.func)
            if src == "land_writer.write":
                calls["land_writer.write"].append(str(path))
            elif src.endswith("cache.put"):
                calls[".put"].append(str(path))
            elif src == "commit_last_accepted":
                calls["commit_last_accepted"].append(str(path))
            if src in ("write_last_accepted", "promote_heal_bundle_to_last_accepted"):
                movers.append(str(path))
    accept_py = str(Path("selector_drift/orchestrate/accept.py"))
    assert all(v == [accept_py] for v in calls.values()), calls
    # Each is called only as the commit_last_accepted callable accept() runs last -- settle()'s
    # for a gate-accepted run, accept_promoted()'s for a promotion -- or inside heallog itself.
    allowed = {
        accept_py,
        str(Path("selector_drift/orchestrate/quarantine_actions.py")),
        str(Path("selector_drift/heallog/evidence.py")),
    }
    assert set(movers) <= allowed, movers


@pytest.mark.unit
def test_hold_writes_no_lake_file_and_accept_is_the_only_lake_writer() -> None:
    """hold() keeps the candidate under lake_root/quarantine/, which the lake view never reads
    (land.layout.DATA_GLOB is target=*/window=*/*.parquet)."""
    from selector_drift.land.layout import DATA_GLOB

    assert not Path("quarantine/testbench_2_2026-09-07.parquet").match(DATA_GLOB)
