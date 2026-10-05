"""The one PSI in the product (validate/stats.py). Task 29 imports it; these tests moved with it."""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from selector_drift.validate.stats import (
    NUMERIC_PARSE_FLOOR,
    PSI_BINS,
    PSI_EPSILON,
    binned,
    category_proportions,
    chi2_quantile,
    critical_psi,
    floats,
    numeric_reference,
    psi,
    psi_of_values,
    psi_result,
    quantile_edges,
)


@pytest.mark.unit
def test_psi_of_a_distribution_against_itself_is_zero() -> None:
    values = [f"{100 + 7.5 * i:,.2f}" for i in range(100)]
    assert psi_of_values(values * 6, values)[0] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.unit
def test_psi_is_finite_when_a_bin_is_empty() -> None:
    # M16: without epsilon, log(0) makes PSI infinite and every run a breach.
    reference = [str(i) for i in range(100)]
    assert math.isfinite(psi_of_values(reference, ["5000"] * 10)[0])


@pytest.mark.unit
def test_psi_falls_back_to_category_proportions_for_text() -> None:
    same, categorical = psi_of_values(["Open", "Paid"] * 50, ["Open", "Paid"] * 50)
    assert categorical and same == pytest.approx(0.0, abs=1e-9)
    assert psi_of_values(["Open", "Paid"] * 50, ["Closed"] * 100)[0] > 0.20


@pytest.mark.unit
def test_unparseable_current_values_land_in_their_own_bin() -> None:
    reference = [f"{100 + i:,.2f}" for i in range(100)]
    assert psi_of_values(reference, ["n/a"] * 100)[0] > 0.20


@pytest.mark.unit
def test_the_published_constants() -> None:
    assert PSI_BINS == 10 and PSI_EPSILON == 1e-6


@pytest.mark.unit
def test_floats_is_all_or_nothing() -> None:
    assert floats(["1,234.50", "-3", "7"]) == [1234.5, -3.0, 7.0]
    assert floats(["1", "Open"]) is None
    # Not numbers to parse_number, so never NaN/inf floats.
    assert floats(["nan"]) is None and floats(["inf"]) is None and floats(["1e5"]) is None


@pytest.mark.unit
def test_quantile_edges_are_the_inner_deciles_of_the_reference() -> None:
    edges = quantile_edges([float(i) for i in range(101)])
    assert edges == pytest.approx(tuple(float(x) for x in range(10, 100, 10)))
    assert quantile_edges([]) == ()
    assert quantile_edges([5.0] * 50) == (5.0,)  # ties collapse; never duplicate edges


@pytest.mark.unit
def test_quantile_edges_ignore_non_finite_reference_values() -> None:
    edges = quantile_edges([float(i) for i in range(101)] + [math.inf, math.nan, -math.inf])
    assert all(math.isfinite(e) for e in edges)


@pytest.mark.unit
def test_binned_adds_the_unparseable_bin_last_and_sums_to_one() -> None:
    p = binned([1.0, 5.0, 9.0], (2.0, 6.0), unparseable=1)
    assert p.tolist() == [0.25, 0.25, 0.25, 0.25]
    # A value equal to an edge falls in the lower bin (side="left").
    assert binned([2.0], (2.0, 6.0), unparseable=0).tolist() == [1.0, 0.0, 0.0, 0.0]


@pytest.mark.unit
def test_binned_counts_non_finite_values_as_unparseable() -> None:
    p = binned([math.nan, math.inf, -math.inf, 1.0], (2.0,), unparseable=0)
    assert p.tolist() == [0.25, 0.0, 0.75]


@pytest.mark.unit
def test_binned_refuses_an_empty_sample() -> None:
    with pytest.raises(ValueError):
        binned([], (2.0,), unparseable=0)


@pytest.mark.unit
def test_category_proportions_cover_the_union_of_categories() -> None:
    r, c = category_proportions(["a", "a", "b"], ["c"])
    assert r.tolist() == pytest.approx([2 / 3, 1 / 3, 0.0])
    assert c.tolist() == [0.0, 0.0, 1.0]


@pytest.mark.unit
def test_psi_of_values_refuses_empty_inputs() -> None:
    with pytest.raises(ValueError):
        psi_of_values([], ["1"])
    with pytest.raises(ValueError):
        psi_of_values(["1"], [])


@pytest.mark.unit
@pytest.mark.parametrize("epsilon", [0.0, -1e-6, math.nan, math.inf])
def test_psi_refuses_an_epsilon_that_would_disable_the_guard(epsilon: float) -> None:
    with pytest.raises(ValueError):
        psi(np.array([0.5, 0.5]), np.array([1.0, 0.0]), epsilon)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [[math.nan, 1.0], [math.inf, 0.0], [-0.5, 1.5]],
    ids=["nan", "inf", "negative"],
)
def test_psi_refuses_proportions_that_are_not_proportions(bad: list[float]) -> None:
    with pytest.raises(ValueError):
        psi(np.array([0.5, 0.5]), np.array(bad))
    with pytest.raises(ValueError):
        psi(np.array(bad), np.array([0.5, 0.5]))


@pytest.mark.unit
def test_psi_refuses_mismatched_bins() -> None:
    with pytest.raises(ValueError):
        psi(np.array([1.0]), np.array([0.5, 0.5]))


# -- properties ---------------------------------------------------------------------------------

_proportions = st.lists(
    st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    min_size=2,
    max_size=12,
).filter(lambda xs: sum(xs) > 0)


def _normalise(xs: list[float]) -> np.ndarray:  # type: ignore[type-arg]
    a = np.asarray(xs, dtype=np.float64)
    return a / a.sum()


@pytest.mark.unit
@settings(max_examples=200, deadline=None)
@given(_proportions)
def test_property_identical_distributions_score_zero(xs: list[float]) -> None:
    p = _normalise(xs)
    assert psi(p, p) == pytest.approx(0.0, abs=1e-12)


@pytest.mark.unit
@settings(max_examples=200, deadline=None)
@given(st.data())
def test_property_psi_is_symmetric_non_negative_and_finite(data: st.DataObject) -> None:
    xs = data.draw(_proportions)
    ys = data.draw(
        st.lists(
            st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
            min_size=len(xs),
            max_size=len(xs),
        ).filter(lambda v: sum(v) > 0)
    )
    p, q = _normalise(xs), _normalise(ys)
    forward, backward = psi(p, q), psi(q, p)
    assert math.isfinite(forward) and forward >= -1e-12
    assert forward == pytest.approx(backward, rel=1e-9, abs=1e-12)


@pytest.mark.unit
@settings(max_examples=100, deadline=None)
@given(st.integers(min_value=2, max_value=20), st.integers(min_value=0, max_value=19))
def test_property_epsilon_keeps_every_empty_bin_finite(k: int, hot: int) -> None:
    # All mass in one bin on one side and in a different bin on the other: every ln() term
    # would be ln(0) or ln(x/0) without epsilon.
    p, q = np.zeros(k), np.zeros(k)
    p[hot % k], q[(hot + 1) % k] = 1.0, 1.0
    assert math.isfinite(psi(p, q)) and psi(p, q) > 0.20


_hostile_float = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.sampled_from([math.nan, math.inf, -math.inf, 1e308, -1e308, 0.0]),
)


@pytest.mark.unit
@settings(max_examples=200, deadline=None)
@given(
    st.lists(_hostile_float, min_size=1, max_size=60),
    st.lists(_hostile_float, min_size=1, max_size=60),
)
def test_property_nan_and_inf_values_never_produce_a_nan_psi(
    reference: list[float], current: list[float]
) -> None:
    edges = quantile_edges(reference)
    assert all(math.isfinite(e) for e in edges)
    score = psi(binned(reference, edges, 0), binned(current, edges, 0))
    assert math.isfinite(score)


_cell_text = st.one_of(
    st.text(max_size=80),
    st.sampled_from(["nan", "NaN", "inf", "-inf", "1e309", "9" * 64, "9" * 65, "", "  "]),
    st.decimals(allow_nan=False, allow_infinity=False, places=2).map(lambda d: f"{d:,.2f}"),
)


@pytest.mark.unit
@settings(max_examples=200, deadline=None)
@given(st.lists(_cell_text, min_size=1, max_size=40), st.lists(_cell_text, min_size=1, max_size=40))
def test_property_hostile_cell_text_always_yields_a_finite_psi(
    reference: list[str], current: list[str]
) -> None:
    score, _ = psi_of_values(reference, current)
    assert math.isfinite(score) and score >= -1e-12


# -- fix round 1: the sampling-noise floor and the numeric parse floor ---------------------------

# Upper-tail chi-square quantiles from standard tables (e.g. NIST/SEMATECH e-Handbook 1.3.6.7.4).
_CHI2_TABLE = [
    (1, 0.99, 6.635),
    (2, 0.99, 9.210),
    (1, 0.95, 3.841),
    (2, 0.95, 5.991),
    (3, 0.99, 11.345),
    (5, 0.99, 15.086),
    (9, 0.99, 21.666),
    (10, 0.99, 23.209),
    (9, 0.95, 16.919),
    (30, 0.99, 50.892),
]


@pytest.mark.unit
@pytest.mark.parametrize(("df", "p", "expected"), _CHI2_TABLE)
def test_chi2_quantile_matches_the_table(df: int, p: float, expected: float) -> None:
    # df 1 and 2 are computed exactly (Wilson-Hilferty is weak there): 1e-3 relative is the
    # table rounding. df >= 3 uses Wilson-Hilferty: within 0.5% of the table.
    tolerance = 1e-3 if df <= 2 else 5e-3
    assert chi2_quantile(df, p) == pytest.approx(expected, rel=tolerance)


@pytest.mark.unit
@pytest.mark.parametrize(("df", "p"), [(0, 0.99), (3, 0.0), (3, 1.0), (3, math.nan)])
def test_chi2_quantile_refuses_a_meaningless_question(df: int, p: float) -> None:
    with pytest.raises(ValueError):
        chi2_quantile(df, p)


@pytest.mark.unit
def test_critical_psi_is_the_chi_square_quantile_scaled_by_both_sample_sizes() -> None:
    # Under the null, PSI ~ chi2_{k-1} * (1/n_current + 1/n_reference).
    assert critical_psi(10, 3000, 100, 0.01) == pytest.approx(
        chi2_quantile(9, 0.99) * (1 / 100 + 1 / 3000)
    )
    with pytest.raises(ValueError):
        critical_psi(1, 3000, 100, 0.01)
    with pytest.raises(ValueError):
        critical_psi(10, 0, 100, 0.01)


@pytest.mark.unit
def test_psi_result_counts_the_effective_bins() -> None:
    reference = [f"{i}" for i in range(100)]
    r = psi_result(reference, reference)
    assert (r.k, r.categorical, r.reference_n, r.current_n) == (10, False, 100, 100)
    # A bin populated only by the current run counts: here the unparseable bin.
    assert psi_result(reference, [*reference, "n/a"]).k == 11
    assert psi_result(["Open", "Paid"] * 5, ["Closed"] * 3).k == 3
    assert psi_result(["Open"] * 5, ["Open"] * 3).k == 1
    assert psi_of_values(reference, reference) == (r.psi, r.categorical)


@pytest.mark.unit
def test_the_numeric_parse_floor_is_published() -> None:
    assert NUMERIC_PARSE_FLOOR == 0.95


@pytest.mark.unit
def test_one_unparseable_reference_value_does_not_make_a_numeric_field_categorical() -> None:
    reference = [f"{100 + i:,.2f}" for i in range(99)] + ["N/A"]
    assert numeric_reference(reference) is not None
    r = psi_result(reference, [f"{100 + i:,.2f}" for i in range(99)])
    # 0.01 of the reference in a bin the current leaves empty costs ~0.01 * ln(1e4) = 0.09.
    assert not r.categorical and r.psi < 0.20
    # The reference's own N/A sits in the unparseable bin, on its side only.
    assert psi_result(reference, ["n/a"] * 100).psi > 0.20


@pytest.mark.unit
def test_the_parse_floor_boundary() -> None:
    at_floor = [str(i) for i in range(95)] + ["x"] * 5
    below = [str(i) for i in range(94)] + ["x"] * 6
    assert numeric_reference(at_floor) is not None and not psi_result(at_floor, ["1"]).categorical
    assert numeric_reference(below) is None and psi_result(below, ["1"]).categorical
    assert numeric_reference([]) is None
