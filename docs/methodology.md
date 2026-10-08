# Methodology

## The distributional check

| Choice | Value | Why |
|---|---|---|
| Statistic | PSI = Σ (cᵢ − rᵢ) · ln(cᵢ/rᵢ) over Jeffreys-smoothed proportions (below) | symmetric, bounded below by 0, one number to print |
| Reference window | trailing **30** accepted runs, same target, same spec version, same field | named, so `n=` has a defined denominator |
| Binning | **10** quantile bins, edges from the reference window only (`np.quantile`, linear), flanked by an **underflow** bin (value < the reference minimum) and an **overflow** bin (value > the reference maximum); the minimum and maximum themselves are inside | edges from the current run would measure the run against itself. Without the outer bins a constant reference (edges collapse to one) put every lower value in its single bin -- PSI 0, "unchanged" -- for a heal from 100.00 onto 50.00 (review fix round 1) |
| Unparseable values | a last bin, on either side: values that do not parse, blank cells included. Under the 95% rule below the reference may hold up to 5% of its non-blank cells there, plus any blanks | a column of `n/a` is a shift, not a skipped row |
| Zero bins | Jeffreys smoothing: **+0.5** (`JEFFREYS`) to every bin's or category's count on both sides before normalising, over the bins populated on either side (a bin empty on both carries no information and is dropped) | `ln(0)` is `-inf`; PSI would silently become `inf` and quarantine everything. Replaced an `ε = 1e-6` floor on proportions (fix round 2), under which each empty bin cost ~1.15 and small samples breached by noise alone |
| Categorical fields | category counts over the union of categories, no binning, smoothed the same way. Judged only when n_current ≥ **5** · k (`MIN_EXPECTED_PER_BIN`), k = categories on either side; below that, INSUFFICIENT (the reason states k and n_current, never cell text) | a reference of text values selects this path. The χ² null assumes ~5 expected per bin; a categorical k is unbounded, so a 900-category field healed onto a disjoint column scored PSI 0.75 against a critical value of 11.4 and passed. The numeric path is not capped: its k is at most 13 and its behaviour is measured below |
| Threshold | **0.20**; a configured threshold must be in (0, 1], and `bins` in [2, 10] (power was measured at 10) | the conventional "significant shift" line; stated in every verdict |
| Noise floor | breach needs PSI > max(0.20, critical); critical = χ²₍ₖ₋₁₎(1 − α) · (1/n_current + 1/n_reference), **α = 0.01** (`ALPHA`), k = bins (or categories) populated in the reference or the current run. k = 1 (both sides entirely in the same bin or category: a constant that stayed constant) is PSI 0, no shift; two different constants are k = 2 and take the normal rule | under the null PSI ≈ χ²₍ₖ₋₁₎ · (1/n_c + 1/n_r): over a small page, 0.20 is sampling noise. χ² quantile: exact for df 1–2, Wilson–Hilferty otherwise (stdlib, no scipy); stated in every verdict |
| Numeric or categorical | numeric when **≥ 95%** of the reference's **non-blank** cells parse (`NUMERIC_PARSE_FLOOR`); blank and whitespace-only cells are left out of that denominator but still go to the unparseable bin, as does the rest, on either side | one accepted `N/A` must not turn every amount into its own category, and neither must empty cells: amounts with 6% blanks were categorical, and a heal onto ten times the values passed (PSI 0.39 against critical 31.5) |
| Which fields | `FieldTarget.distributional` (Task 5), default `False` | PSI over an identifier column is noise |
| Persistence | **2** consecutive breaching runs before alerting (steady state only) | B7 |

A heal-log line such as "distribution unchanged (PSI 0.03, threshold 0.20, n=847)" is produced by
`DistributionalAssertion.check()` in `selector_drift/validate/assertions/distribution.py`; `n` is the
number of reference values in the trailing 30 accepted runs. Warm-up per target: the bench 5 runs,
client targets 30 (the scope record's `warmup_runs`). During warm-up the check returns INSUFFICIENT,
so a heal quarantines until a human confirms it. INSUFFICIENT means only a genuine lack of basis:
warm-up, an empty side, too few values, or a categorical field with fewer than 5 current values
per category.

Minimum sample: below **20** values in either the reference window or the current run the check
returns INSUFFICIENT (`MIN_VALUES`) -- a heal quarantines, a cache hit is tolerated, and no alert
fires. A PASS from a check with no power would be fail-open, so the floor is set from measured
power: the smallest current n at which a ×10 shift is detected at least 95% of the time (bench
distribution, reference 30 runs × 50 values, 1000 draws per n), rounded up to a multiple of 10.
Before the underflow/overflow bins existed that n was 35 and the floor was 40; the overflow bin
sees a ×10 shift at far smaller n, and the rule, applied unchanged, gives 20.

Every number in this section is printed by `python scripts/measure_psi_power.py` (fixed seeds).
The script decides each breach through `DistributionalAssertion.judge()` -- the product's own
breach rule, the one `assess()` applies -- so it measures the product, not a copy of it.

| n_current | ×10 shift detected | False breach, same distribution |
|---|---|---|
| 10 | 35.4% | 0.0% |
| 15 | 80.3% | 0.0% |
| **20** (chosen) | **97.1%** | **0.1%** |
| 25 | 99.9% | 0.1% |
| 30 | 100.0% | 0.2% |
| 35 | 100.0% | 0.5% |
| 40 | 100.0% | 1.0% |
| 45 | 100.0% | 0.8% |

False breach on the same distribution at larger samples (separate draws): n = 40 **0.8%**,
n = 50 **0.9%**, n = 200 **0.0%**. Rows below 20 are measurements of the statistic only: the
check does not judge there (INSUFFICIENT).

A constant field that stayed constant (PASS, PSI 0) needs the same 20 values. The statistic is
implemented once, in `selector_drift/validate/stats.py`; the data-quality distribution-shift rule
calls the same `psi_result()` and `critical_psi()` -- the same statistic and the same breach rule,
PSI over max(threshold, critical) -- so the two can never print different PSIs or disagree on a
breach for one field. A value equal to a bin edge falls in the lower bin
(`np.searchsorted(..., side="left")`).

### Power against the bench's decoy

**Power is honest, and low for subtle shifts.** Against the bench's adversarial `DecoyColumn` (a
"Prior Amount" column at 80–97% of the real amount) on a 50-row page, this check detected
**7.3%** of 300 decoy heals (false breach on the true values of the same pages: 0.7%). A decoy
that close to the real column barely moves a distribution that spans three orders of magnitude;
it is caught by the row-key continuity and independent-rung checks, not by PSI. This check exists
for gross shifts — another unit, another column of a different scale, a column of text, a heal
onto values outside the range history has ever seen — and the false-breach rate above is the price
it does not charge. ALPHA and the threshold were not tuned to make these numbers look better.
