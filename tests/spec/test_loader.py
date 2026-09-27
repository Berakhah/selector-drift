from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from selector_drift.config import get_settings
from selector_drift.spec.loader import (
    load_scope,
    load_spec,
    load_spec_text,
    scope_file,
    spec_file,
    target_of,
)
from selector_drift.spec.schema import SpecError
from tests._day_one import blocked_on_day_one_values as _blocked_on_day_one_values

REPO = Path(__file__).resolve().parents[2]
SPECS = sorted((REPO / "specs").glob("*.yaml"))
UNSAFE_YAML = {
    "load",
    "unsafe_load",
    "full_load",
    "load_all",
    "unsafe_load_all",
    "full_load_all",
    "Loader",
    "UnsafeLoader",
    "FullLoader",
}

# Deviation from the brief (controller rulings 2 and 3, 2026-09-28, recorded in the Task 5
# report, fix round 1 minor 2): the day-one scope records still carry <UPPER-CASE> placeholders,
# and ruling 3's new ScopeRecord.user_agent_contact validation refuses a placeholder URL at load
# -- by design, so that an unresolved placeholder cannot silently pass as a real contact URL.
# This test loads the real scope records (via `load_scope`) to check they name the right target,
# so it inherits the same block as the five tests in tests/scope/test_records_load.py until day
# one's values land, rather than raising `ScopeRefusal` from that validator. The gate itself
# (PLACEHOLDER regex + skipif) is defined once in tests/_day_one.py and imported here, not
# redefined, so this file and tests/scope/test_records_load.py cannot diverge (fix round 1,
# Important 1).


@pytest.mark.unit
def test_a_target_is_described_in_words_not_a_dom_path() -> None:
    for p in SPECS:
        for t in load_spec(p).targets:
            assert " " in t.description and "#" not in t.description, (p.name, t.name)
            assert "." not in t.description, (p.name, t.name)


@pytest.mark.unit
def test_every_target_declares_at_least_one_assertion() -> None:
    for p in SPECS:
        assert all(t.assertions for t in load_spec(p).targets), p.name


@pytest.mark.unit
def test_every_spec_declares_a_natural_key_that_exists_in_its_schema() -> None:
    """M5. Task 28 upserts on it, Task 30 diffs on it, Task 22 joins on it."""
    for p in SPECS:
        spec = load_spec(p)
        assert spec.natural_key
        assert set(spec.natural_key) <= set(spec.expected_schema)


@pytest.mark.unit
def test_the_loader_refuses_a_yaml_tag_that_would_construct_an_object() -> None:
    """R13/H5. yaml.safe_load, always. A task spec is config; config must never be code."""
    with pytest.raises(yaml.YAMLError):
        load_spec_text("!!python/object/apply:os.system ['echo pwned']")


@pytest.mark.unit
def test_an_invalid_spec_is_a_spec_error_naming_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "ghost.yaml"
    bad.write_text("version: [not, an, int]\n", encoding="utf-8")
    with pytest.raises(SpecError, match=r"ghost\.yaml"):
        load_spec(bad)


@pytest.mark.unit
def test_a_missing_spec_is_a_spec_error(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="no task spec"):
        load_spec(tmp_path / "absent.yaml")


@pytest.mark.unit
@_blocked_on_day_one_values
def test_every_spec_file_is_named_for_its_scope_records_target() -> None:
    """Task 6 writes the heartbeat before the spec loads, keyed by the file stem."""
    for p in SPECS:
        spec = load_spec(p)
        assert Path(spec.scope_ref).stem == target_of(p)
        assert load_scope(REPO / spec.scope_ref).target == target_of(p)


@pytest.mark.unit
def test_every_schedule_is_distinct_so_runs_never_share_a_worker_minute() -> None:
    schedules = [load_spec(p).schedule for p in SPECS]
    assert len(set(schedules)) == len(schedules)


@pytest.mark.unit
def test_spec_and_scope_paths_honour_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SD_SPEC_DIR", str(tmp_path / "sp"))
    monkeypatch.setenv("SD_SCOPE_DIR", str(tmp_path / "sc"))
    get_settings.cache_clear()
    try:
        assert spec_file("specs/testbench.yaml") == tmp_path / "sp" / "testbench.yaml"
        assert spec_file("testbench.yaml") == tmp_path / "sp" / "testbench.yaml"
        assert spec_file(tmp_path / "x.yaml") == tmp_path / "x.yaml"  # absolute: as given
        assert scope_file("scopes/testbench.yaml") == tmp_path / "sc" / "testbench.yaml"
    finally:
        get_settings.cache_clear()


@pytest.mark.unit
def test_no_module_calls_an_unsafe_yaml_loader() -> None:
    """Global constraint: every YAML loader is yaml.safe_load (R13/H5)."""
    offenders = []
    for path in (REPO / "selector_drift").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "yaml"
                and node.attr in UNSAFE_YAML
            ):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} yaml.{node.attr}")
    assert not offenders, offenders
