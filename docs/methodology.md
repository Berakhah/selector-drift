# Methodology

## The distributional check

| Choice | Value | Why |
|---|---|---|
| Statistic | PSI = Σ (cᵢ − rᵢ) · ln((cᵢ + ε)/(rᵢ + ε)) | symmetric, bounded below by 0, one number to print |
| Reference window | trailing **30** accepted runs, same target, same spec version, same field | named, so `n=` has a defined denominator |
| Binning | **10** quantile bins, edges from the reference window only (`np.quantile`, linear) | edges from the current run would measure the run against itself |
| Unparseable values | an 11th bin that the reference never populates | a column of `n/a` is a shift, not a skipped row |
| Zero bins | `ε = 1e-6` added to both proportions | `ln(0)` is `-inf`; PSI would silently become `inf` and quarantine everything |
| Categorical fields | category proportions over the union of categories, no binning | a reference of text values selects this path |
| Threshold | **0.20** | the conventional "significant shift" line; stated in every verdict |
| Which fields | `FieldTarget.distributional` (Task 5), default `False` | PSI over an identifier column is noise |
| Persistence | **2** consecutive breaching runs before alerting (steady state only) | B7 |

A heal-log line such as "distribution unchanged (PSI 0.03, threshold 0.20, n=847)" is produced by
`DistributionalAssertion.check()` in `selector_drift/validate/assertions/distribution.py`; `n` is the
number of reference values in the trailing 30 accepted runs. Warm-up per target: the bench 5 runs,
client targets 30 (the scope record's `warmup_runs`). During warm-up the check returns INSUFFICIENT,
so a heal quarantines until a human confirms it.

Minimum sample: below **10** values in either the reference window or the current run the check
also returns INSUFFICIENT (`MIN_VALUES`, the same floor as the row-key continuity check's
`MIN_JOINED`) — a handful of values is not evidence of a shift or of its absence. The statistic is
implemented once, in `selector_drift/validate/stats.py` (`psi_of_values()`); the data-quality
distribution-shift rule calls the same function, so the two can never print different PSIs for one
field. A value equal to a bin edge falls in the lower bin (`np.searchsorted(..., side="left")`).
