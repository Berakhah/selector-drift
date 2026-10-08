# tests/land/test_changeset.py
import csv
import json
import socket
from pathlib import Path

import pytest

from selector_drift.land.changeset import changeset
from selector_drift.land.writer import write
from tests.land.factory import RUN_KEY, make_validated, rows

RUN = rows(3)  # AB10000000 100.00 | AB10000001 107.50 | AB10000002 115.00


def _prior_csv(tmp_path: Path) -> Path:
    p = tmp_path / "prior.csv"
    p.write_text(
        "claim_number,amount\n"
        "AB10000000,100.00\n"  # unchanged
        "AB10000001,999.99\n"  # changed
        "AB19999999,5.00\n",  # absent from the run: not a deletion
        encoding="utf-8",
    )
    return p


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def run_with_changes(lake_dir) -> str:
    write(make_validated(RUN))
    return RUN_KEY


@pytest.fixture
def no_network(monkeypatch) -> list[object]:
    attempts: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        attempts.append(args)
        raise OSError("network blocked by the test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return attempts


@pytest.mark.unit
def test_changeset_marks_every_line_and_defaults_to_not_accepted(run_with_changes, tmp_path):
    lines = _read_csv(changeset(run_with_changes, _prior_csv(tmp_path), "csv"))
    assert len(lines) == 3  # three rows x one non-key field
    assert {r["action"] for r in lines} == {"add", "change", "unchanged"}
    assert all(r["accept"] == "false" for r in lines)


@pytest.mark.unit
def test_a_changed_line_reports_both_values(run_with_changes, tmp_path):
    line = next(
        r
        for r in _read_csv(changeset(run_with_changes, _prior_csv(tmp_path), "csv"))
        if r["action"] == "change"
    )
    assert (line["claim_number"], line["field"]) == ("AB10000001", "amount")
    assert (line["prior_value"], line["new_value"]) == ("999.99", "107.50")


@pytest.mark.unit
def test_an_added_row_has_no_prior_value(run_with_changes, tmp_path):
    line = next(
        r
        for r in _read_csv(changeset(run_with_changes, _prior_csv(tmp_path), "csv"))
        if r["action"] == "add"
    )
    assert line["claim_number"] == "AB10000002" and line["prior_value"] == ""


@pytest.mark.unit
def test_a_row_absent_from_the_run_is_not_reported_as_a_deletion(run_with_changes, tmp_path):
    lines = _read_csv(changeset(run_with_changes, _prior_csv(tmp_path), "csv"))
    assert "AB19999999" not in {r["claim_number"] for r in lines}
    assert {r["action"] for r in lines} <= {"add", "change", "unchanged"}


@pytest.mark.unit
def test_json_output_carries_the_same_lines_with_a_boolean_accept(run_with_changes, tmp_path):
    prior = tmp_path / "prior.json"
    prior.write_text(
        json.dumps([{"claim_number": "AB10000001", "amount": "999.99"}]), encoding="utf-8"
    )
    lines = json.loads(changeset(run_with_changes, prior, "json").read_text(encoding="utf-8"))
    assert len(lines) == 3 and all(line["accept"] is False for line in lines)
    assert sum(line["action"] == "change" for line in lines) == 1


@pytest.mark.unit
def test_changeset_opens_no_network_connection(run_with_changes, tmp_path, no_network):
    # Fixed (M19): the first draft grepped the source for three library names.
    out = changeset(run_with_changes, _prior_csv(tmp_path), "csv")
    assert out.is_file()
    assert no_network == []


@pytest.mark.unit
def test_a_formula_from_the_portal_cannot_execute_in_the_clients_spreadsheet(lake_dir, tmp_path):
    hostile = rows(1)
    hostile[0]["amount"] = '=HYPERLINK("https://evil.example.invalid","1.00")'
    write(make_validated(hostile))
    line = _read_csv(changeset(RUN_KEY, _prior_csv(tmp_path), "csv"))[0]
    assert line["new_value"].startswith("'=")


@pytest.mark.unit
def test_the_file_is_named_by_fs_key_and_lands_in_out_dir(run_with_changes, tmp_path):
    out = changeset(run_with_changes, _prior_csv(tmp_path), "csv", out_dir=tmp_path / "cs")
    assert out == tmp_path / "cs" / "testbench_2_2026-09-07.csv"
    assert not list((tmp_path / "cs").glob("*.tmp"))


@pytest.mark.unit
def test_a_prior_without_the_natural_key_is_refused(run_with_changes, tmp_path):
    bad = tmp_path / "prior.csv"
    bad.write_text("amount\n1.00\n", encoding="utf-8")
    with pytest.raises(ValueError, match="claim_number"):
        changeset(run_with_changes, bad, "csv")


@pytest.mark.unit
def test_a_run_with_no_landed_rows_has_nothing_to_export(lake_dir, tmp_path):
    with pytest.raises(LookupError, match="no landed rows"):
        changeset(RUN_KEY, _prior_csv(tmp_path), "csv")


@pytest.mark.unit
def test_an_unknown_format_is_refused(tmp_path):
    with pytest.raises(ValueError, match="csv or json"):
        changeset(RUN_KEY, tmp_path / "prior.csv", "xlsx")  # type: ignore[arg-type]


@pytest.mark.unit
def test_a_later_attempt_of_the_same_run_supersedes_the_earlier_one(lake_dir, tmp_path):
    write(make_validated(rows(3)))
    revised = rows(3)
    revised[1]["amount"] = "999.99"
    write(make_validated(revised))
    lines = _read_csv(changeset(RUN_KEY, _prior_csv(tmp_path), "csv"))
    assert len(lines) == 3
    assert {r["claim_number"]: r["action"] for r in lines}["AB10000001"] == "unchanged"


@pytest.mark.unit
def test_only_the_specs_fields_are_exported(lake_dir, tmp_path):
    write(make_validated(rows(2, extra={"retired_col": "x"})))
    lines = _read_csv(changeset(RUN_KEY, _prior_csv(tmp_path), "csv"))
    assert {r["field"] for r in lines} == {"amount"}
