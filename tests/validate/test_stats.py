"""The one PSI in the product (validate/stats.py). Task 29 imports it; these tests moved with it."""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from selector_drift.validate.stats import (
    PSI_BINS,
    PSI_EPSILON,
    binned,
    category_proportions,
    floats,
    psi,
    psi_of_values,
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
