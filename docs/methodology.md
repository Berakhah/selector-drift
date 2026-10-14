# Methodology

## The distributional check

| Choice | Value | Why |
|---|---|---|
| Statistic | PSI = Σ (cᵢ − rᵢ) · ln(cᵢ/rᵢ) over Jeffreys-smoothed proportions (below) | symmetric, bounded below by 0, one number to print |
| Reference window | trailing **30** accepted runs, same target, same spec version, same field | named, so `n=` has a defined denominator |
| Binning | **10** quantile bins, edges from the reference window only (`np.quantile`, linear), flanked by an **underflow** bin (value < the reference minimum) and an **overflow** bin (value > the reference maximum); the minimum and maximum themselves are inside | edges from the current run would measure the run against itself. Without the outer bins a constant reference (edges collapse to one) put every lower value in its single bin -- PSI 0, "unchanged" -- for a heal from 100.00 onto 50.00 (review fix round 1) |
| Blank and unparseable cells | on the numeric path, two last bins, each counted on either side: a **blank** bin (empty or whitespace-only cells) and an **unparseable** bin (text that is not a number). Under the 95% rule below the reference may hold up to 5% of its non-blank cells in the unparseable bin | a column of `n/a` is a shift, not a skipped row. Blanks and text shared one bin until fix round 2: a field 90% blank healed onto a column 90% blank and 10% "see notes" kept that bin's proportion and passed (PSI 0.044 against critical 0.24) |
| Zero bins | Jeffreys smoothing: **+0.5** (`JEFFREYS`) to every bin's or category's count on both sides before normalising, over the bins populated on either side (a bin empty on both carries no information and is dropped) | `ln(0)` is `-inf`; PSI would silently become `inf` and quarantine everything. Replaced an `ε = 1e-6` floor on proportions (fix round 2), under which each empty bin cost ~1.15 and small samples breached by noise alone |
| Categorical fields | category counts over the union of categories, no binning, smoothed the same way. Judged only when n_current ≥ **5** · k (`MIN_EXPECTED_PER_BIN`), k = categories on either side; below that, INSUFFICIENT (the reason states k and n_current, never cell text) | a reference of text values selects this path. The χ² null assumes ~5 expected per bin; a categorical k is unbounded, so a 900-category field healed onto a disjoint column scored PSI 0.75 against a critical value of 11.4 and passed. The numeric path is not capped: its k is at most 14 and its behaviour is measured below |
| Threshold | **0.20**; a configured threshold must be in (0, 1], and `bins` in [2, 10] (power was measured at 10) | the conventional "significant shift" line; stated in every verdict |
| Noise floor | breach needs PSI > max(0.20, critical); critical = χ²₍ₖ₋₁₎(1 − α) · (1/n_current + 1/n_reference), **α = 0.01** (`ALPHA`), k = bins (or categories) populated in the reference or the current run. k = 1 (both sides entirely in the same bin or category: a constant that stayed constant) is PSI 0, no shift; two different constants are k = 2 and take the normal rule | under the null PSI ≈ χ²₍ₖ₋₁₎ · (1/n_c + 1/n_r): over a small page, 0.20 is sampling noise. χ² quantile: exact for df 1–2, Wilson–Hilferty otherwise (stdlib, no scipy); stated in every verdict |
| Numeric or categorical | numeric when **≥ 95%** of the reference's **non-blank** cells parse (`NUMERIC_PARSE_FLOOR`); blank and whitespace-only cells are left out of that denominator and go to the blank bin. A numeric reference with fewer than `MIN_VALUES` parseable values, or a numeric current run with fewer than `MIN_VALUES` non-blank cells, is INSUFFICIENT | one accepted `N/A` must not turn every amount into its own category, and neither must empty cells: amounts with 6% blanks were categorical, and a heal onto ten times the values passed (PSI 0.39 against critical 31.5). Quantile edges from a handful of numbers (599 blanks and one `5`) are degenerate, so that is no basis rather than a verdict |
| Which fields | `FieldTarget.distributional` (Task 5), default `False` | PSI over an identifier column is noise |
| Persistence | **2** consecutive breaching runs before alerting (steady state only) | B7 |

A heal-log line such as "distribution unchanged (PSI 0.03, threshold 0.20, n=847)" is produced by
`DistributionalAssertion.check()` in `selector_drift/validate/assertions/distribution.py`; `n` is the
number of reference values in the trailing 30 accepted runs. Warm-up per target: the bench 5 runs,
client targets 30 (the scope record's `warmup_runs`). During warm-up the check returns INSUFFICIENT,
so a heal quarantines until a human confirms it. INSUFFICIENT means only a genuine lack of basis:
warm-up, an empty side, too few values, a numeric reference with too few parseable values, a
numeric current run with too few non-blank cells, or a categorical field with fewer than 5 current
values per category.

Minimum sample: below **40** values in either the reference window or the current run the check
returns INSUFFICIENT (`MIN_VALUES`): a heal quarantines, a cache hit is tolerated, and no alert
fires. On the numeric path n counts **parseable values** on the reference side and **non-blank
cells** (numbers and unparseable text, blanks excluded) on the current side, so ten amounts among
ninety blanks are ten values of evidence, not a hundred (fix round 3: they caught a ×3 shift 6.3%
of the time, and one wild value among 99 blanks passed). Text counts on the current side because
it cannot dilute power: a numeric reference is at most 5% text, so text in the current run is
itself a shift. A PASS from a check with no power
would be fail-open, so the floor is set from measured power: the smallest current n at which
**both a ×10 and a ×3 shift** are detected at least 95% of the time (bench distribution,
reference 30 runs × 50 values, 1000 draws per n), rounded up to a multiple of 10.

Why two shifts. The rule used ×10 alone until fix round 1 added the overflow bin. About 29% of a
×10-shifted bench sample lies above the reference maximum, so the overflow bin alone catches a ×10
shift from a handful of values: ×10 alone gave a floor of 20, where a ×3 shift -- most of whose
values stay inside the reference range -- was caught only 49.1% of the time. A floor that passes
heals the check cannot see is fail-open, so ×3 joined the criterion (fix round 2): n = 35 catches
×3 93.7% of the time, n = 40 98.0%, so the floor is 40.

Every number in this section is printed by `python scripts/measure_psi_power.py` (fixed seeds).
The script decides each breach through `DistributionalAssertion._judge()` -- the product's own
breach rule, the one `assess()` applies after its guards; it is private because it skips them,
and the script needs it only to measure below the floor -- so it measures the product, not a copy.

| n_current | ×10 shift detected | ×3 shift detected | False breach, same distribution |
|---|---|---|---|
| 10 | 32.6% | 3.8% | 0.0% |
| 15 | 78.4% | 22.6% | 0.0% |
| 20 | 96.5% | 49.1% | 0.0% |
| 25 | 99.7% | 73.4% | 0.0% |
| 30 | 100.0% | 86.3% | 0.3% |
| 35 | 100.0% | 93.7% | 0.5% |
| **40** (chosen) | **100.0%** | **98.0%** | **0.9%** |
| 45 | 100.0% | 99.2% | 0.5% |
| 50 | 100.0% | 99.6% | 1.0% |
| 55 | 100.0% | 99.8% | 1.4% |
| 60 | 100.0% | 100.0% | 1.2% |

False breach on the same distribution at larger samples (separate draws): n = 40 **0.8%**,
n = 50 **0.9%**, n = 200 **0.0%**. Rows below 40 are measurements of the statistic only: the
check does not judge there (INSUFFICIENT).

A constant field that stayed constant (PASS, PSI 0) needs the same 40 values. The statistic is
implemented once, in `selector_drift/validate/stats.py`. Task 29's distribution-shift rule calls
`DistributionalAssertion.assess()`, so the gate and the quality screen share every guard (warm-up,
`MIN_VALUES`, the parseable-reference and non-blank-current floors, the categorical expected-count
floor) and the breach
rule, and can never print different PSIs or disagree on a breach for one field. A value equal to a
bin edge falls in the lower bin (`np.searchsorted(..., side="left")`).

### Power against the bench's decoy

**Power is honest, and low for subtle shifts.** Against the bench's adversarial `DecoyColumn` (a
"Prior Amount" column at 80–97% of the real amount) on a 50-row page, this check detected
**7.3%** of 300 decoy heals (false breach on the true values of the same pages: 0.7%). A decoy
that close to the real column barely moves a distribution that spans three orders of magnitude;
it is caught by the row-key continuity and independent-rung checks, not by PSI. This check exists
for gross shifts — another unit, another column of a different scale, a column of text, a heal
onto values outside the range history has ever seen — and the false-breach rate above is the price
it does not charge. ALPHA and the threshold were not tuned to make these numbers look better.

## First-heal confirmation: a known limit

The selector cache holds one selector per field, so a portal that flips between two layouts asks a human once per flip, not once ever: each flip is a heal to a selector the cache no longer holds, and `FirstHealConfirmation` quarantines it again.

## Value attacks on correct nodes: a known limit

A value attack on the right nodes (the bench's `ValueSwap`) is refused only once a target has history: on its first run there is no accepted run for `RowKeyContinuity` to compare against, so it is written (pinned by `tests/suite/test_adversarial_refused.py`).

## Retention

| Store | self-owned target | client-authorised target |
|---|---|---|
| `evidence/last-accepted/<target>/` | overwritten on every accepted run; one copy | same |
| `evidence/heals/<heal_id>/` | kept for the life of the log | deleted after the scope record's `evidence_retention_days` (required for this basis; 90 offered by default) |

Deletion is done by the daily `evidence-retention` Prefect deployment (03:30 UTC). Each deletion
writes an `audit_log` row (`action = evidence_expired`); the `heal_events` row and every published
count remain, and the heal screen reads the bundle as *expired* rather than *corrupted*. A heal
whose target has no scope record is kept and audited (`retention_skipped`), never deleted on a
guess. The directory deleted is derived from the heal's integer id under the evidence root, never
from the stored `evidence_uri`, and a bundle that is a link is skipped and audited.

On disk the last-accepted directory is not named after the target: it is
`last-accepted/t-<keyed digest of the target>`, so no listing or error message names a client.
The digest is keyed with `SD_REDACTION_KEY`; rotating that key orphans every last-accepted page,
and the next heal of each target then records no *before* half (not a corrupted one).

**Disk budget.** One heal bundle is two redacted HTML snapshots and two full-page PNGs at 1280 px
wide — budget 4 MB. The VM (P-4) reserves 20 GB for `evidence/` and `lake/` together; at the bench's
observed heal rate the ninety-day log needs well under 2 GB. `du -sh evidence lake` is checked at
each weekly publish and the figure is recorded in the published log's changelog. Per-file caps
bound any single read: manifest 2 MiB, HTML 20 MiB, PNG 50 MiB; a capture over a cap is refused,
never truncated.

## Evidence redaction: what it does not catch

Evidence goes through one path (`heallog/evidence.py`): the shared scrubber, bok-core `redact()`,
a structural check that the screenshot masked every identifier and credential node, then
`egress.write_guarded` for every file. These are the gaps that path does **not** close, stated so
that no published claim outruns them.

- **Person names and addresses are not detected.** bok-core's detectors cover emails, IBANs,
  phones, digit runs, cards, tax ids, routing and account numbers (`redact.COVERED`); person
  names and addresses are `redact.NOT_COVERED`. At CLIENT_CONFIDENTIAL every form value is
  removed structurally (default-deny), but a name or address printed as page text survives in
  the stored HTML and is not masked in the screenshot. This is why a client bundle is stored as
  CLIENT_CONFIDENTIAL on the CLIENT plane and never published, whatever its redaction block says.
  `test_a_person_name_does_not_survive_into_stored_evidence` is a strict xfail until bok-core
  ships the detector.
- **Redaction works on the serialised HTML.** An identifier split by markup
  (`1234<span></span>5678`) is not one run to the redactor. The screenshot masker reads rendered
  text the way it paints and masks such an element, but the stored HTML keeps the split digits.
- **Redaction changes identifiers the heal screen shows.** Every 8+ digit run and long upper-case
  code (claim numbers, amounts) becomes a keyed surrogate. Heals replay on the redacted snapshot
  (`test_a_heal_replays_from_its_redacted_bundle`); a value assertion cannot be re-checked from
  evidence.
- **The manifest digest is a plain sha256** (the API spec 7.2 shape). It detects corruption, not
  tampering: anyone who can write the CLIENT-plane store can rewrite a file and its manifest
  together. That person already holds every piece of evidence, so a keyed digest would protect
  nothing they could not read; it would matter only if bundles were shipped elsewhere.
- **The manifest is not redacted.** It holds only system-minted values — run keys, minted node
  ids, field names, boxes, counts — each validated before it is written.
- **A long opaque token inside `title` / `aria-label` / `alt` / `placeholder` is kept** (Task 19
  N-I1 ruling): the rungs read those attributes, so only JWTs, bearer tokens, credential header
  values and URLs are removed from them.
- **Over-masked credential-like inputs.** The credential predicate is deliberately wide; a value
  it blanks is gone from the stored DOM, so a heal that extracted such an input's value would
  replay differently from the live run. No current target extracts one.
- **The masker sees the raw DOM.** `PiiMasker` never logs and never puts DOM text in an
  exception, and `capture()` wraps whatever a masker raises; a custom masker must keep the same
  rule.
- **Time budget.** Redaction is never skipped or truncated to meet a deadline: a DOM over the
  HTML cap is refused and the run records it. bok-core's measured cost is about 1.2 s per MB of
  DOM, up to ~16 s/MB on adversarial input.
- **Screenshot verification refuses, it does not repair.** Pixel verification checks every masked
  element's box before and after the shot and the boxes of the text inside it; text clipped by
  an `overflow: hidden` ancestor can make a clean capture refuse (a false refusal, never a pass
  with colour in a box). Pages taller than 32 768 px are refused. A frame in `designMode` is
  masked whole and a top document in `designMode` refuses — at CLIENT_CONFIDENTIAL only;
  SYNTHETIC and PUBLIC pages keep the heuristic floor.
- **getBoundingClientRect.** Recorded boxes come from the page's own `getBoundingClientRect`
  (a source-text assertion pins that, per the Task 19 brief); page script could misreport them.
  They are display hints for the heal screen, not part of the mask, which is classified and
  verified over CDP.
