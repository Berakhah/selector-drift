import datetime as dt

import pytest

from selector_drift.validate.history import AcceptedRun, History


def _run(day: int, rows: list[dict[str, str]]) -> AcceptedRun:
    return AcceptedRun(
        run_key=f"testbench:2:2026-09-{day:02d}",
        window=dt.date(2026, 9, day),
        rows=tuple(rows),
        natural_key=("claim_number",),
    )


H = History(
    runs=(
        _run(1, [{"claim_number": "AB00000001", "amount": "1.00"}]),
        _run(
            2,
            [
                {"claim_number": "AB00000001", "amount": "2.00"},
                {"claim_number": "AB00000002", "amount": "3.00"},
            ],
        ),
    )
)


@pytest.mark.unit
def test_previous_is_the_newest_accepted_run():
    assert H.previous() is H.runs[-1] and History().previous() is None and len(H) == 2


@pytest.mark.unit
def test_values_flattens_every_run_oldest_first():
    assert H.values("amount") == ["1.00", "2.00", "3.00"] and H.values("nope") == []


@pytest.mark.unit
def test_by_key_joins_on_the_natural_key_and_skips_keyless_rows():
    run = _run(3, [{"claim_number": "AB00000009", "amount": "9.00"}, {"amount": "orphan"}])
    assert run.by_key() == {("AB00000009",): {"claim_number": "AB00000009", "amount": "9.00"}}


@pytest.mark.unit
def test_trailing_keeps_the_newest_n_runs():
    assert H.trailing(1).runs == (H.runs[-1],) and len(H.trailing(0)) == 0
    assert H.trailing(5).runs == H.runs
