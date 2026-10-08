import threading
from pathlib import Path

import pytest

from selector_drift.land import catalog, writer
from selector_drift.land import compact as compact_mod
from selector_drift.land.catalog import query
from selector_drift.land.compact import compact
from selector_drift.land.writer import fs_key, write
from tests.land.factory import RUN_KEY, make_validated, parquet_files, rows, scalar

VIEW = "select claim_number, amount, run_key, ingested_at from lake order by claim_number"


def _writes(n: int) -> None:
    for _ in range(n):
        write(make_validated(rows(3)))


@pytest.mark.integration
def test_compaction_folds_a_runs_files_without_changing_the_view(lake_dir: Path) -> None:
    # Fixed: the first draft wrote five *windows* and expected fewer than five files. Windows are
    # partitions; compaction cannot fold across them. Five attempts in one window is the real case.
    _writes(5)
    before = query(VIEW).fetchall()
    compact("testbench")
    assert len(parquet_files(lake_dir)) == 1
    assert query(VIEW).fetchall() == before


@pytest.mark.integration
def test_every_intermediate_state_of_the_swap_shows_the_same_rows(
    lake_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # L7. Query between every rename compaction makes: the view never changes.
    _writes(4)
    before = query(VIEW).fetchall()
    real = compact_mod._move
    seen: list[list[tuple[object, ...]]] = []

    def observed(src: Path, dst: Path) -> None:
        real(src, dst)
        seen.append(query(VIEW).fetchall())

    monkeypatch.setattr(compact_mod, "_move", observed)
    compact("testbench")
    assert len(seen) == 5  # one swap-in, four retirements
    assert all(state == before for state in seen)


@pytest.mark.integration
def test_a_reader_holding_a_stale_listing_retries_instead_of_failing(
    lake_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writes(3)
    stale = catalog.lake_files()
    compact("testbench")
    real = catalog.lake_files
    calls = {"n": 0}

    def listing(target: str | None = None) -> list[Path]:
        calls["n"] += 1
        return stale if calls["n"] == 1 else real(target)

    monkeypatch.setattr(catalog, "lake_files", listing)
    assert scalar(query("select count(*) from lake")) == 3
    assert calls["n"] == 2


@pytest.mark.integration
def test_compaction_under_a_concurrent_reader_raises_nothing(lake_dir: Path) -> None:
    _writes(6)
    errors: list[BaseException] = []
    counts: set[int] = set()
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                counts.add(scalar(query("select count(*) from lake")))
            except BaseException as exc:  # the test records everything
                errors.append(exc)

    t = threading.Thread(target=reader)
    t.start()
    try:
        compact("testbench")
    finally:
        stop.set()
        t.join()
    assert errors == []
    assert counts <= {3}
    assert len(parquet_files(lake_dir)) == 1


@pytest.mark.integration
def test_superseded_files_are_renamed_aside_not_unlinked(lake_dir: Path) -> None:
    _writes(3)
    originals = {p.name for p in parquet_files(lake_dir)}
    compact("testbench")
    assert {p.name for p in (lake_dir / "_retired").rglob("*.parquet")} == originals


@pytest.mark.integration
def test_a_partition_over_the_threshold_compacts_itself(
    lake_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # P4: compaction existed and nothing ever called it.
    monkeypatch.setattr(writer, "COMPACT_AFTER_FILES", 3)
    _writes(4)
    assert len(parquet_files(lake_dir)) == 1
    assert scalar(query("select count(*) from lake")) == 3


@pytest.mark.unit
def test_the_default_threshold_is_twenty_four() -> None:
    assert writer.COMPACT_AFTER_FILES == 24


@pytest.mark.integration
def test_history_still_finds_a_runs_rows_after_compaction(lake_dir: Path) -> None:
    # Task 26A's history.py globs {fs_key(run_key)}-*.parquet inside the partition.
    _writes(3)
    compact("testbench")
    part = lake_dir / "target=testbench" / "window=2026-09-07"
    assert len(list(part.glob(f"{fs_key(RUN_KEY)}-*.parquet"))) == 1


@pytest.mark.integration
def test_a_write_after_compaction_numbers_its_attempt_past_the_folded_file(lake_dir: Path) -> None:
    # The compacted file keeps the highest attempt it folded (3); counting files would say 2.
    _writes(3)
    compact("testbench")
    nxt = write(make_validated(rows(3)))
    assert nxt.name.startswith(f"{fs_key(RUN_KEY)}-4-")
    assert scalar(query("select count(*) from lake")) == 3


@pytest.mark.integration
def test_compaction_never_folds_two_runs_together(lake_dir: Path) -> None:
    # A spec bump mid-day puts two run keys in one partition; each folds only into itself.
    _writes(2)
    write(make_validated(rows(3), "testbench:3:2026-09-07"))
    write(make_validated(rows(3), "testbench:3:2026-09-07"))
    compact("testbench")
    names = sorted(p.name for p in parquet_files(lake_dir))
    assert len(names) == 2
    assert names[0].startswith(fs_key(RUN_KEY)) and names[1].startswith("testbench_3_2026-09-07")


@pytest.mark.integration
def test_a_compaction_already_running_is_not_doubled(lake_dir: Path) -> None:
    _writes(3)
    (lake_dir / ".compact-testbench.lock").write_text("", encoding="utf-8")
    compact("testbench")
    assert len(parquet_files(lake_dir)) == 3
