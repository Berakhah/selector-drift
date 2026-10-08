import re
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from selector_drift.extract.models import CandidateExtraction
from selector_drift.land.catalog import query, run_columns
from selector_drift.land.compact import compact
from selector_drift.land.writer import fs_key, write
from tests.land.factory import RUN_KEY, make_validated, parquet_files, rows, scalar


@pytest.mark.unit
def test_write_rejects_an_unvalidated_candidate(candidate: CandidateExtraction) -> None:
    # The gate is the only way into the lake: a CandidateExtraction is refused before any I/O.
    with pytest.raises(TypeError, match="ValidatedExtraction"):
        write(candidate)  # type: ignore[arg-type]  # the point of the test


@pytest.mark.unit
def test_a_quarantined_run_never_reaches_the_lake(candidate: CandidateExtraction) -> None:
    """Moved here from Task 25 (it needs land.write, which this task creates): a QuarantinedRun
    is refused by type before any I/O, exactly like a raw candidate."""
    from selector_drift.validate.gate import QuarantinedRun
    from selector_drift.validate.verdict import Outcome, Verdict

    refused = QuarantinedRun(
        run_key=candidate.run_key,
        reason="test refusal",
        verdicts=(
            Verdict(check="T", outcome=Outcome.FAIL, threshold="x", observed="y", reason="r"),
        ),
    )
    with pytest.raises(TypeError, match="ValidatedExtraction"):
        write(refused)  # type: ignore[arg-type]


@pytest.mark.unit
def test_fs_key_removes_every_colon() -> None:
    # A run_key carries two colons, illegal in Windows file names (found 2026-09-26).
    assert fs_key("testbench:2:2026-09-07") == "testbench_2_2026-09-07"


@pytest.mark.integration
def test_each_write_is_one_named_file_carrying_run_key_and_ingested_at(lake_dir: Path) -> None:
    path = write(make_validated(rows(3)))
    assert path.parent == lake_dir / "target=testbench" / "window=2026-09-07"
    assert re.fullmatch(r"testbench_2_2026-09-07-1-[0-9A-HJKMNP-TV-Z]{26}\.parquet", path.name)
    table = pq.ParquetFile(path).read()
    assert {"run_key", "ingested_at"} <= set(table.column_names)
    assert set(table.column("run_key").to_pylist()) == {RUN_KEY}


@pytest.mark.integration
def test_a_second_write_of_the_same_run_is_attempt_two(lake_dir: Path) -> None:
    write(make_validated(rows(3)))
    second = write(make_validated(rows(3)))
    assert second.name.startswith(f"{fs_key(RUN_KEY)}-2-")


@pytest.mark.integration
def test_replaying_a_run_does_not_duplicate_rows(lake_dir: Path) -> None:
    v = make_validated(rows(3))
    write(v)
    write(v)
    assert scalar(query("select count(*) from lake")) == 3
    assert scalar(query("select count(*) from lake_raw")) == 6
    assert len(parquet_files(lake_dir)) == 2  # appended, not overwritten (M6)


@pytest.mark.integration
def test_a_late_record_lands_in_its_partition_without_rewriting_it(lake_dir: Path) -> None:
    first = write(make_validated(rows(3)))
    before = first.read_bytes()
    late = {"claim_number": "ZZ99999999", "amount": "42.00"}
    write(make_validated([*rows(3), late]))
    n = scalar(query('select count(*) from lake where "window" = ?', ["2026-09-07"]))
    assert n == 4
    assert first.read_bytes() == before  # the closed file is untouched
    assert len(parquet_files(lake_dir)) == 2


@pytest.mark.integration
def test_a_retry_appends_a_file_and_the_view_hides_the_superseded_rows(lake_dir: Path) -> None:
    # Fixed: the first draft read `select amount from lake limit 1`, which returns an arbitrary row.
    original = rows(3)
    corrected = [dict(r) for r in original]
    corrected[0]["amount"] = "999.99"
    write(make_validated(original))
    write(make_validated(corrected))
    assert len(parquet_files(lake_dir)) == 2
    assert scalar(query("select count(*) from lake")) == 3
    got = scalar(
        query("select amount from lake where claim_number = ?", [original[0]["claim_number"]])
    )
    assert got == "999.99"


@pytest.mark.integration
def test_the_same_keys_on_two_days_are_two_days_of_history(lake_dir: Path) -> None:
    write(make_validated(rows(3), "testbench:2:2026-09-07"))
    write(make_validated(rows(3), "testbench:2:2026-09-08"))
    assert scalar(query("select count(*) from lake")) == 6


@pytest.mark.integration
def test_a_natural_key_repeated_within_one_run_is_refused(lake_dir: Path) -> None:
    # Carried from Task 22: dedup would silently collapse the two rows into one.
    dup = rows(2)
    dup[1]["claim_number"] = dup[0]["claim_number"]
    with pytest.raises(ValueError, match="twice") as exc:
        write(make_validated(dup))
    assert "row 1" in str(exc.value)  # names the row index...
    assert dup[0]["claim_number"] not in str(exc.value)  # ...never the client's value
    assert dup[0]["amount"] not in str(exc.value)
    assert parquet_files(lake_dir) == []


@pytest.mark.integration
def test_a_row_without_its_natural_key_is_refused_by_index(lake_dir: Path) -> None:
    plain = rows(2)
    del plain[1]["claim_number"]
    with pytest.raises(ValueError, match="row 1") as exc:
        write(make_validated(plain))
    assert plain[1]["amount"] not in str(exc.value)
    assert parquet_files(lake_dir) == []


@pytest.mark.integration
@pytest.mark.parametrize("name", ["run_key", "ingested_at", "target", "window"])
def test_a_field_named_like_lake_metadata_is_refused(lake_dir: Path, name: str) -> None:
    with pytest.raises(ValueError, match="metadata"):
        write(make_validated(rows(2, extra={name: "client value"})))
    assert parquet_files(lake_dir) == []


@pytest.mark.integration
def test_no_shared_duckdb_file_is_ever_created(
    lake_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for _ in range(2):
        write(make_validated(rows(3)))
    query("select count(*) from lake")
    compact("testbench")
    assert not [p for p in tmp_path.rglob("*") if p.suffix in {".duckdb", ".wal", ".db"}]


@pytest.mark.integration
def test_an_empty_lake_is_queryable(lake_dir: Path) -> None:
    assert scalar(query("select count(*) from lake")) == 0
    assert scalar(query("select count(*) from lake_raw")) == 0


@pytest.mark.integration
def test_a_target_restricted_query_sees_only_that_target(lake_dir: Path) -> None:
    shutil.copy(
        lake_dir.parent / "specs" / "testbench.yaml",
        lake_dir.parent / "specs" / "control-app.yaml",
    )
    write(make_validated(rows(3)))
    write(make_validated(rows(2), "control-app:2:2026-09-07"))
    assert scalar(query("select count(*) from lake", target="control-app")) == 2
    assert scalar(query("select count(*) from lake")) == 5


@pytest.mark.integration
def test_rows_carry_their_key_and_their_provenance_without_the_value(lake_dir: Path) -> None:
    write(make_validated(rows(1)))
    row = query("select * from lake").dicts()[0]
    assert row["_sd_natural_key"] == '["AB10000000"]'
    assert row["target"] == "testbench" and row["window"] == "2026-09-07"
    assert "AB10000000" not in row["_sd_provenance"]
    assert '"selector": "td.claim_number"' in row["_sd_provenance"]


@pytest.mark.integration
def test_run_columns_are_the_runs_own_columns(lake_dir: Path) -> None:
    write(make_validated(rows(2, extra={"status": "OPEN"}), "testbench:2:2026-09-08"))
    write(make_validated(rows(2)))
    cols = run_columns(RUN_KEY)
    assert "status" not in cols
    assert {"claim_number", "amount", "run_key", "ingested_at"} <= set(cols)
