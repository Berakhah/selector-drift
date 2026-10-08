"""The one PSI in the product (ledger 2026-09-26). Task 29's distribution-shift rule calls
DistributionalAssertion.assess() (validate/assertions/distribution.py), which computes this
module's statistic, so the gate and the quality screen share every guard and the breach rule and a
heal-log line and a quality row about the same field cannot disagree. validate/ must not import
quality/, and this task runs first, so the statistic lives here; the methodology table in
docs/methodology.md describes exactly this file."""

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
# Zero bins (fix round 2): Jeffreys smoothing -- half a count added to every bin populated on
# either side, before normalising. It replaced an epsilon floor on proportions, whose ln(r/1e-6)
# made each empty bin cost ~1.15 and turned small samples into breaches.
JEFFREYS: Final = 0.5
# A breach needs PSI over max(threshold, critical_psi(...)): the chi-square noise floor at ALPHA.
ALPHA: Final = 0.01
# A field is numeric when at least this share of the reference parses (fix round 1).
NUMERIC_PARSE_FLOOR: Final = 0.95
# The chi-square null behind critical_psi() assumes ~5 expected counts per bin. The numeric path's
# k is bounded (PSI_BINS + underflow + overflow + blank + unparseable); a categorical field's k
# is not, so there a current sample under MIN_EXPECTED_PER_BIN * k is no basis (review C2).
MIN_EXPECTED_PER_BIN: Final = 5


def psi(reference: Floats, current: Floats) -> float:
    """PSI = sum (c_i - r_i) * ln(c_i / r_i) over two proportion vectors (see smoothed()).

    Refuses (ValueError) anything that is not a pair of same-length vectors of finite, strictly
    positive proportions: a NaN here would compare False against every threshold and silently
    pass, and a zero is ln(0) -- so neither is ever computed."""
    r = np.asarray(reference, dtype=np.float64)
    c = np.asarray(current, dtype=np.float64)
    if r.shape != c.shape or r.ndim != 1:
        raise ValueError(f"PSI needs two equal-length vectors, got {r.shape} and {c.shape}")
    for p in (r, c):
        if not bool(np.all(np.isfinite(p))) or bool(np.any(p <= 0)):
            raise ValueError("PSI inputs must be finite, strictly positive proportions")
    return float(np.sum((c - r) * np.log(c / r)))


def floats(values: Sequence[str]) -> list[float] | None:
    """Every value as a number, or None. parse_number is ASCII-only and bounded, so a value is
    never NaN or infinite."""
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


def reference_bounds(reference: Sequence[float]) -> tuple[float, float]:
    """(min, max) of the reference's finite values: the cut-offs of the underflow and overflow
    bins. Refuses a reference with no finite value."""
    ref = _finite(reference)
    if ref.size == 0:
        raise ValueError("the reference holds no finite value to bound")
    return float(ref.min()), float(ref.max())


def bin_counts(
    values: Sequence[float],
    edges: tuple[float, ...],
    unparseable: int,
    bounds: tuple[float, float],
    *,
    blank: int,
) -> Floats:
    """Counts in this order: an UNDERFLOW bin (value < bounds[0], the reference minimum), the
    len(edges) + 1 quantile bins, an OVERFLOW bin (value > bounds[1], the reference maximum), a
    BLANK bin (`blank` empty or whitespace-only cells), and a last bin for text that is not a
    number -- `unparseable`, and any non-finite float.
    The outer bins are what let a constant reference see a move: its edges collapse to (c,), and
    without them every value <= c would share its one bin (fix round 1, review C1). Blanks and
    text are separate because a mostly-blank field healed onto a column of text kept one shared
    bin's proportion and passed (fix round 2). A value equal to an edge falls in the lower bin
    (side="left"); the bounds themselves are inside. Refuses an empty sample, a negative count,
    and bounds that are not a finite range."""
    lo, hi = bounds
    if not (math.isfinite(lo) and math.isfinite(hi) and lo <= hi):
        raise ValueError(f"bounds must be a finite range, got {bounds!r}")
    if blank < 0 or unparseable < 0:
        raise ValueError("blank and unparseable are counts, never negative")
    a = np.asarray(values, dtype=np.float64)
    finite = a[np.isfinite(a)]
    idx = np.searchsorted(np.asarray(edges, dtype=np.float64), finite, side="left") + 1
    idx = np.where(finite < lo, 0, np.where(finite > hi, len(edges) + 2, idx))
    counts = np.bincount(idx, minlength=len(edges) + 3).astype(np.float64)
    tail = [float(blank), float(unparseable + (a.size - finite.size))]
    counts = np.append(counts, np.asarray(tail, dtype=np.float64))
    if counts.sum() <= 0:
        raise ValueError("cannot bin an empty sample")
    return np.asarray(counts, dtype=np.float64)


def is_blank(cell: str) -> bool:
    """An empty or whitespace-only cell (str.strip(): NBSP and tabs included)."""
    return not cell.strip()


def category_counts(reference: Sequence[str], current: Sequence[str]) -> tuple[Floats, Floats]:
    """Counts over the union of categories, in sorted order."""
    r, c = Counter(reference), Counter(current)
    cats = sorted(set(r) | set(c))
    return (
        np.asarray([r[k] for k in cats], dtype=np.float64),
        np.asarray([c[k] for k in cats], dtype=np.float64),
    )


def smoothed(reference_counts: Floats, current_counts: Floats) -> tuple[Floats, Floats]:
    """Jeffreys smoothing: keep the bins populated on EITHER side (a bin empty on both carries
    no information), add JEFFREYS to every kept count on both sides, normalise. Every
    proportion is then strictly positive, so PSI is finite with no epsilon. Equal proportions
    stay equal whatever the two sample sizes. Refuses non-counts and an empty side."""
    r = np.asarray(reference_counts, dtype=np.float64)
    c = np.asarray(current_counts, dtype=np.float64)
    if r.shape != c.shape or r.ndim != 1:
        raise ValueError(f"need two equal-length count vectors, got {r.shape} and {c.shape}")
    for x in (r, c):
        if not bool(np.all(np.isfinite(x))) or bool(np.any(x < 0)) or x.sum() <= 0:
            raise ValueError("counts must be finite, non-negative, and not all zero")
    keep = (r > 0) | (c > 0)
    rs, cs = r[keep] + JEFFREYS, c[keep] + JEFFREYS
    return np.asarray(rs / rs.sum(), dtype=np.float64), np.asarray(cs / cs.sum(), dtype=np.float64)


def numeric_reference(reference: Sequence[str]) -> list[float] | None:
    """The reference's parsed values when at least NUMERIC_PARSE_FLOOR of its NON-BLANK cells
    parse -- the field is numeric -- else None, which selects the categorical path. One accepted
    "N/A" in a numeric column must not turn every amount into its own category (fix round 1),
    and neither must empty cells: a blank or whitespace-only cell is left out of the
    denominator (review C2(b)) and lands in its own blank bin (fix round 2)."""
    filled = [v for v in reference if not is_blank(v)]
    if not filled:
        return None
    parsed = [float(n) for v in filled if (n := parse_number(v)) is not None]
    return parsed if len(parsed) >= NUMERIC_PARSE_FLOOR * len(filled) else None


@dataclass(frozen=True)
class PsiResult:
    psi: float
    categorical: bool
    k: int  # effective bins (or categories): populated in the reference OR the current run
    reference_n: int
    current_n: int


def psi_result(
    reference: Sequence[str], current: Sequence[str], *, bins: int = PSI_BINS
) -> PsiResult:
    """PSI with what the noise floor needs. Numeric when numeric_reference() says so: quantile
    bins fixed from the reference's parsed values, flanked by an underflow and an overflow bin
    at the reference's min and max, then a blank bin and an unparseable-text bin, each counted on
    EITHER side. Otherwise category counts over the union of
    categories. Jeffreys-smoothed (smoothed()). k == 1 -- every value on both sides in the same
    bin or category -- is PSI 0: a constant field that stayed constant. Both inputs must be
    non-empty (ValueError otherwise)."""
    if not reference or not current:
        raise ValueError("PSI needs a non-empty reference and a non-empty current sample")
    ref_num = numeric_reference(reference)
    if ref_num is not None:
        edges, bounds = quantile_edges(ref_num, bins), reference_bounds(ref_num)
        parsed = [float(n) for v in current if (n := parse_number(v)) is not None]
        rb = sum(1 for v in reference if is_blank(v))
        cb = sum(1 for v in current if is_blank(v))
        rc = bin_counts(ref_num, edges, len(reference) - rb - len(ref_num), bounds, blank=rb)
        cc = bin_counts(parsed, edges, len(current) - cb - len(parsed), bounds, blank=cb)
        categorical = False
    else:
        rc, cc = category_counts(reference, current)
        categorical = True
    r, c = smoothed(rc, cc)
    return PsiResult(
        psi=psi(r, c),
        categorical=categorical,
        k=int(r.size),
        reference_n=len(reference),
        current_n=len(current),
    )


def psi_of_values(
    reference: Sequence[str], current: Sequence[str], *, bins: int = PSI_BINS
) -> tuple[float, bool]:
    """(PSI, categorical) -- psi_result() without the counts."""
    r = psi_result(reference, current, bins=bins)
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
