"""The one PSI in the product (ledger 2026-09-26). DistributionalAssertion (Task 23) and the
data-quality distribution_shift rule (Task 29) both call psi_of_values(), so a heal-log line and a
quality row about the same field cannot disagree. validate/ must not import quality/, and this
task runs first, so the statistic lives here; the methodology table in docs/methodology.md
describes exactly this file."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from typing import Final

import numpy as np
import numpy.typing as npt

from selector_drift.validate.assertions._numbers import parse_number

Floats = npt.NDArray[np.float64]
PSI_BINS: Final = 10
PSI_EPSILON: Final = 1e-6


def psi(reference: Floats, current: Floats, epsilon: float = PSI_EPSILON) -> float:
    """PSI = sum (c_i - r_i) * ln((c_i + eps) / (r_i + eps)) over two proportion vectors.

    Refuses (ValueError) anything that is not a pair of same-length vectors of finite,
    non-negative proportions, and any epsilon that is not finite and positive: a NaN here would
    compare False against every threshold and silently pass, so it is never computed."""
    if not (math.isfinite(epsilon) and epsilon > 0):
        raise ValueError(f"epsilon must be finite and positive, got {epsilon!r}")
    r = np.asarray(reference, dtype=np.float64)
    c = np.asarray(current, dtype=np.float64)
    if r.shape != c.shape or r.ndim != 1:
        raise ValueError(f"PSI needs two equal-length vectors, got {r.shape} and {c.shape}")
    for p in (r, c):
        if not bool(np.all(np.isfinite(p))) or bool(np.any(p < 0)):
            raise ValueError("PSI inputs must be finite, non-negative proportions")
    return float(np.sum((c - r) * np.log((c + epsilon) / (r + epsilon))))


def floats(values: Sequence[str]) -> list[float] | None:
    """Every value as a number, or None -- which selects the categorical path. parse_number is
    ASCII-only and bounded, so a value is never NaN or infinite."""
    out: list[float] = []
    for v in values:
        n = parse_number(v)
        if n is None:
            return None
        out.append(float(n))
    return out


def _finite(values: Sequence[float]) -> npt.NDArray[np.float64]:
    a = np.asarray(values, dtype=np.float64)
    return a[np.isfinite(a)]


def quantile_edges(reference: Sequence[float], bins: int = PSI_BINS) -> tuple[float, ...]:
    """Inner quantile edges from the reference ONLY. The current run is never an input.
    Non-finite values are ignored (they cannot place an edge); ties collapse to one edge."""
    ref = _finite(reference)
    if ref.size == 0:
        return ()
    with np.errstate(over="ignore", invalid="ignore"):  # +/-1e308 interpolates to inf/NaN
        qs = np.quantile(ref, np.linspace(0.0, 1.0, bins + 1)[1:-1])
    return tuple(float(x) for x in np.unique(qs[np.isfinite(qs)]))


def binned(values: Sequence[float], edges: tuple[float, ...], unparseable: int) -> Floats:
    """Proportions over len(edges) + 1 value bins plus one last bin for values that are not
    numbers -- `unparseable`, and any non-finite float. A value equal to an edge falls in the
    lower bin (side="left")."""
    a = np.asarray(values, dtype=np.float64)
    finite = a[np.isfinite(a)]
    idx = np.searchsorted(np.asarray(edges, dtype=np.float64), finite, side="left")
    counts = np.bincount(idx, minlength=len(edges) + 1).astype(np.float64)
    counts = np.append(counts, np.float64(unparseable + (a.size - finite.size)))
    total = counts.sum()
    if total <= 0:
        raise ValueError("cannot bin an empty sample")
    return np.asarray(counts / total, dtype=np.float64)


def category_proportions(reference: Sequence[str], current: Sequence[str]) -> tuple[Floats, Floats]:
    """Proportions over the union of categories, in sorted order. Both inputs non-empty."""
    r, c = Counter(reference), Counter(current)
    cats = sorted(set(r) | set(c))
    rp = np.asarray([r[k] for k in cats], dtype=np.float64)
    cp = np.asarray([c[k] for k in cats], dtype=np.float64)
    return (
        np.asarray(rp / rp.sum(), dtype=np.float64),
        np.asarray(cp / cp.sum(), dtype=np.float64),
    )


def psi_of_values(
    reference: Sequence[str],
    current: Sequence[str],
    *,
    bins: int = PSI_BINS,
    epsilon: float = PSI_EPSILON,
) -> tuple[float, bool]:
    """(PSI, categorical). Numeric when every reference value parses: quantile bins fixed from
    the reference, current values that do not parse counted in an extra bin. Otherwise category
    proportions. Both inputs must be non-empty (ValueError otherwise)."""
    if not reference or not current:
        raise ValueError("PSI needs a non-empty reference and a non-empty current sample")
    ref_num = floats(reference)
    if ref_num is not None:
        edges = quantile_edges(ref_num, bins)
        parsed = [float(n) for v in current if (n := parse_number(v)) is not None]
        return (
            psi(
                binned(ref_num, edges, 0),
                binned(parsed, edges, len(current) - len(parsed)),
                epsilon,
            ),
            False,
        )
    r, c = category_proportions(reference, current)
    return psi(r, c, epsilon), True
