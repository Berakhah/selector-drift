"""The one PSI in the product (ledger 2026-09-26). DistributionalAssertion (Task 23) and the
data-quality distribution_shift rule (Task 29) both call psi_of_values(), so a heal-log line and a
quality row about the same field cannot disagree. validate/ must not import quality/, and this
task runs first, so the statistic lives here; the methodology table in docs/methodology.md
describes exactly this file."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Final

import numpy as np
import numpy.typing as npt

from selector_drift.validate.assertions._numbers import parse_number

Floats = npt.NDArray[np.float64]
PSI_BINS: Final = 10
PSI_EPSILON: Final = 1e-6
# A breach needs PSI over max(threshold, critical_psi(...)): the chi-square noise floor at ALPHA.
ALPHA: Final = 0.01
# A field is numeric when at least this share of the reference parses (fix round 1).
NUMERIC_PARSE_FLOOR: Final = 0.95


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


def numeric_reference(reference: Sequence[str]) -> list[float] | None:
    """The reference's parsed values when at least NUMERIC_PARSE_FLOOR of them parse -- the
    field is numeric -- else None, which selects the categorical path. One accepted "N/A" in a
    numeric column must not turn every amount into its own category (fix round 1)."""
    if not reference:
        return None
    parsed = [float(n) for v in reference if (n := parse_number(v)) is not None]
    return parsed if len(parsed) >= NUMERIC_PARSE_FLOOR * len(reference) else None


@dataclass(frozen=True)
class PsiResult:
    psi: float
    categorical: bool
    k: int  # effective bins: populated in the reference OR the current run
    reference_n: int
    current_n: int


def psi_result(
    reference: Sequence[str],
    current: Sequence[str],
    *,
    bins: int = PSI_BINS,
    epsilon: float = PSI_EPSILON,
) -> PsiResult:
    """PSI with what the noise floor needs. Numeric when at least NUMERIC_PARSE_FLOOR of the
    reference parses: quantile bins fixed from the reference's parsed values, unparseable values
    on EITHER side counted in an extra bin. Otherwise category proportions over the union of
    categories. Both inputs must be non-empty (ValueError otherwise)."""
    if not reference or not current:
        raise ValueError("PSI needs a non-empty reference and a non-empty current sample")
    ref_num = numeric_reference(reference)
    if ref_num is not None:
        edges = quantile_edges(ref_num, bins)
        parsed = [float(n) for v in current if (n := parse_number(v)) is not None]
        r = binned(ref_num, edges, len(reference) - len(ref_num))
        c = binned(parsed, edges, len(current) - len(parsed))
        categorical = False
    else:
        r, c = category_proportions(reference, current)
        categorical = True
    return PsiResult(
        psi=psi(r, c, epsilon),
        categorical=categorical,
        k=int(np.count_nonzero((r > 0) | (c > 0))),
        reference_n=len(reference),
        current_n=len(current),
    )


def psi_of_values(
    reference: Sequence[str],
    current: Sequence[str],
    *,
    bins: int = PSI_BINS,
    epsilon: float = PSI_EPSILON,
) -> tuple[float, bool]:
    """(PSI, categorical) -- psi_result() without the counts."""
    r = psi_result(reference, current, bins=bins, epsilon=epsilon)
    return r.psi, r.categorical


def chi2_quantile(df: int, p: float) -> float:
    """The p-quantile of chi-square with `df` degrees of freedom, stdlib only (no scipy).
    Exact for df 1 and 2 (z_{(1+p)/2}^2 and -2 ln(1-p)); Wilson-Hilferty otherwise, within
    0.5% of the published tables for df >= 3 at the upper tail."""
    if df < 1 or not (0.0 < p < 1.0):  # NaN fails the comparison too
        raise ValueError(f"need df >= 1 and 0 < p < 1, got df={df}, p={p!r}")
    if df == 1:
        return NormalDist().inv_cdf((1.0 + p) / 2.0) ** 2
    if df == 2:
        return -2.0 * math.log1p(-p)
    z = NormalDist().inv_cdf(p)
    h = 2.0 / (9.0 * df)
    return df * (1.0 - h + z * math.sqrt(h)) ** 3


def critical_psi(k: int, reference_n: int, current_n: int, alpha: float = ALPHA) -> float:
    """The PSI that sampling noise alone exceeds with probability about `alpha`. Under the null
    PSI ~ chi2_{k-1} * (1/n_current + 1/n_reference), so a breach needs PSI over
    max(threshold, critical_psi(...)). Needs k >= 2 effective bins and both samples non-empty."""
    if k < 2 or reference_n < 1 or current_n < 1:
        raise ValueError(
            f"need k >= 2 and non-empty samples, got k={k}, n={reference_n}/{current_n}"
        )
    return chi2_quantile(k - 1, 1.0 - alpha) * (1.0 / current_n + 1.0 / reference_n)
