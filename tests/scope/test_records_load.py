from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.gate import authorise
from selector_drift.spec.loader import load_scope, load_scope_text
from tests._day_one import PLACEHOLDER, SCOPES
from tests._day_one import blocked_on_day_one_values as _blocked_on_day_one_values


@pytest.mark.unit
@_blocked_on_day_one_values
def test_all_three_day_one_targets_authorise() -> None:
    """`today` is pinned to the record's own authorised_on, not to the wall clock (M20):
    a test that compares dt.date.today() to a hardcoded expires_on is green until that
    date and red afterwards for no reason anyone will remember."""
    # PLACEHOLDER -- P-3 replaces the two control-arm names before day one (OQ1).
    names = {"testbench", "agent-perimeter", "ledger-sense"}
    got = {authorise(r, today=r.authorised_on).target for r in map(load_scope, SCOPES)}
    assert got == names


@pytest.mark.unit
@_blocked_on_day_one_values
def test_every_scope_record_names_at_least_one_https_origin() -> None:
    """H2. The origins are the browser's allowlist; a deployed target is reached over TLS."""
    for p in SCOPES:
        origins = load_scope(p).origins
        assert origins and all(o.startswith("https://") for o in origins), p.name


@pytest.mark.unit
def test_no_scope_record_carries_an_unresolved_placeholder() -> None:
    """B5/P-2/P-3. Brief 3.3 requires the agent to identify itself with a working contact URL;
    a 404 is a compliance claim that fails the first time anyone checks it. The first draft
    checked only for the string USER and so passed with `<real-account>` still in place."""
    for p in SCOPES:
        found = PLACEHOLDER.findall(p.read_text(encoding="utf-8"))
        assert not found, f"{p.name}: {found}"


@pytest.mark.unit
@_blocked_on_day_one_values
def test_only_the_bench_auto_promotes() -> None:
    auto = {
        load_scope(p).target for p in SCOPES if load_scope(p).first_heal_policy == "auto_promote"
    }
    assert auto == {"testbench"}


@pytest.mark.unit
@_blocked_on_day_one_values
def test_only_the_bench_warms_up_fast() -> None:
    """M18. Five runs is permitted only because the bench has an answer key."""
    fast = {load_scope(p).target for p in SCOPES if load_scope(p).warmup_runs < 30}
    assert fast == {"testbench"}


@pytest.mark.unit
@_blocked_on_day_one_values
def test_no_scope_record_is_dated_in_the_future() -> None:
    """B4. authorised_on is the day the record was signed. A past date stays past, so this
    reads the wall clock without becoming a time bomb."""
    today = dt.datetime.now(dt.UTC).date()
    for p in SCOPES:
        assert load_scope(p).authorised_on <= today, p.name


@pytest.mark.unit
def test_no_scope_record_authors_its_own_sensitivity() -> None:
    """R4/H7. Sensitivity is derived from basis; a typo in an authored label publishes a
    client portal."""
    for p in SCOPES:
        assert "sensitivity" not in yaml.safe_load(p.read_text(encoding="utf-8")), p.name


@pytest.mark.unit
def test_an_unknown_target_is_refused_by_the_loader(tmp_path: Path) -> None:
    """Design 4.3: unknown target -> refuse. A missing file is a refusal, not a crash."""
    with pytest.raises(ScopeRefusal, match="unknown target"):
        load_scope(tmp_path / "nobody.yaml")


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "!!python/object/apply:os.system ['echo pwned']",
        "target: x\nbasis: public-permissive\n",
    ],
)
def test_a_malformed_scope_record_is_a_refusal(text: str) -> None:
    """R13/H5 and design 4.2: a YAML object tag and a basis outside the v1 enum both refuse."""
    with pytest.raises(ScopeRefusal):
        load_scope_text(text)
