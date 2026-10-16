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

A value attack on the right nodes (the bench's `ValueSwap`) is refused from the first run after a
human-confirmed run with history: `RowKeyContinuity` compares a run with the last accepted run, and
its collapse floor refuses a column whose values moved while every key stayed, even on a cache hit.
Before that there is nothing to compare against, and **a human confirming run one is the only
check**. A target's run one cannot write unconfirmed: with an empty cache every field is a heal,
and on a heal the VALUE family's missing basis is a refusal, so a cold-start target's first run is
always held for a human, whatever its page (pinned end to end by
`tests/orchestrate/test_pipeline_end_to_end.py` and `test_promotion_end_to_end.py`; the cache and
the history are populated only by that promotion). The Task 26 suite's "written on run one" case
(`tests/suite/test_adversarial_refused.py`) judges a pre-warmed cache with no history -- a state
the assembled pipeline cannot reach, because the cache is written only from an accepted run.

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
from the stored `evidence_uri` (a bundle whose uri was never persisted is retained like any other),
and a bundle that is a link is skipped and audited. A heal whose run still has an **open
quarantine** is kept (`retention_skipped`, "quarantine open") until it is promoted or discarded —
promotion needs the bundle. Every audit row is committed *before* its delete, so a crash leaves a
bundle audited as expired and retried, never one gone without its `evidence_expired` row.

A **replay** — a re-run of a window whose quarantine was already promoted or discarded — is held
on its own `lake/quarantine/<key>.replay-<id>.parquet` and audited `quarantine_replayed`. It holds
extracted client data, so it lives as long as the target's `evidence_retention_days` counted from
that audit row (a self-owned target keeps it for the life of the log), then is deleted and audited
`replay_expired`; the `quarantine_replayed` row stays as the record, and the API should show a
replay as *expired*, not as a dead link. While a replay exists its run's heal bundles are kept
(`retention_skipped`, "replay held"); expiring it releases them in the same sweep. A replay of a
target with no scope record is kept and audited, never deleted on a guess. The file is found by
its name under today's quarantine directory, never by the path stored in the audit row, and a row
naming anything that is not a replay file is audited and left alone. A replay file no audit row
names (a crash between its write and its commit) is removed after the one-day grace, audited
`replay_orphan_removed`.

The same run sweeps client data nothing can reach, each removal audited by store-relative path:
a `heals/<n>` directory with no heal row (after a one-day grace), crash leftovers (`.tmp-*`,
`.old-*`, after an hour), and a `last-accepted` directory that no unexpired scope record names —
so an expired or removed authorisation also takes its last accepted page with it.

On disk the last-accepted directory is not named after the target: it is
`last-accepted/t-<keyed digest of the target>`, so no listing or error message names a client.
The digest is keyed with the store's own path key (`evidence/.path-key`, created once), **not**
`SD_REDACTION_KEY`: rotating the redaction key renames nothing, and an operator answering a
deletion request finds a target's directory with `last_accepted_dir(target)`. The path key names
paths only; it protects no content. A last-accepted page records the sensitivity label it was
stored under, and it is never reused as the *before* half of a capture with another label
(`before_unavailable_reason = "label_mismatch"`).

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
- **Redaction works on values, never on markup** (fix round 1). Every text, tail and attribute
  value is redacted in the parsed tree and the tree re-serialised, so tags, attribute names and
  node ids are never touched (redacting the serialised page let one email detector match across
  `"`, `>` and `<` and swallow whole table rows). Each block element's *rendered* text — inline
  children joined as painted, entities decoded — is checked too: an identifier split by markup
  (`<span>4111</span> <span>1111</span>…`, `1234<b>5678</b>90`, `jane.okafor&#64;…`) is redacted
  as one run (its first text node takes the redacted run, the rest are blanked) and the block is
  masked in the screenshot. The result is verified twice — detectors re-run over every decoded
  value and run, and the serialised page must be a fixpoint of `redact()` (a page whose markup
  itself trips a detector is refused, never stored). Limit: runs are cut at block elements by
  tag, not by computed CSS, so an identifier split across two block elements styled `inline`
  is not joined; the masker and the stored HTML then both see two pieces.
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
  HTML cap is refused, and a redaction past `SD_REDACTION_DEADLINE_S` (default 60 s) raises
  `RedactionFailed` — nothing is stored, and the run quarantines with that reason. The deadline
  is checked between redaction passes (bok-core's redactor is linear-time but not interruptible),
  so a single pass can overrun it before the refusal. bok-core's measured cost is about 1.2 s
  per MB of DOM, up to ~16 s/MB on adversarial input.
- **CDP cost of text-quad verification.** Measured on the reviewer's probe: ~0.82 ms per text
  node per pass; at the 20 000-node cap both passes take ~33 s, inside the 60 s per-pass
  deadline but a real share of the 600 s flow budget on a page masked whole.
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

## The rung distribution (B11)

`python -m selector_drift.orchestrate.rung_stats`, run after the bench campaign below -- the
command's output, verbatim, not an estimate:

```
rung1 16.9% · rung2 69.7% · rung3 0.0% · rung4 13.1% · rung5 0.0%
```

**The campaign** (`scripts/bench_rung_campaign.py 60`, Task 26A, 2026-10-15). The assembled
product ran once a day for 60 days (2026-01-01 .. 2026-03-01) against the bench portal served
with its production mutation schedule: one or two benign classes every day, an adversarial class
about one day in seven. The denominator is every field resolution (`resolutions`, 6 fields x 60
runs), won or not. The operator policy is mechanical: every hold is offered for promotion the same
day -- promoted if the gate lets a human confirm it, discarded if it still FAILs.

| Outcome | Runs |
|---|---|
| completed (every field a rung-1 hit) | 7 |
| healed and written | 33 |
| held, then promoted (missing evidence only: run one, distributional warm-up, a virtualised table) | 5 |
| held, then discarded (a FAIL) | 15 |

Read the shares with the bench in mind. **Rung 1 is low because the bench mutates every day**: a
production portal changes rarely, so most of its days are rung-1 hits; the bench's are not by
design. Rung 5 is 0.0%: one field escalated to a human on a `DuplicateLabel` day (counted in
rung-5's denominator as a resolution no rung won, and held). Rung 3 won nothing: it looks
for the same expected label text as rung 2 (`resolve.labels.expected_label`), and rung 2 runs
first, so on this bench whatever rung 3 could anchor on, rung 2 had already resolved. Rungs 2 and 4 carried
every heal; with all model providers disabled (rung 5 is a refusal in v1), rungs 1-4 resolved
every field on every benign day.

**False refusals measured by the same campaign.** Of the 15 discards, 6 were adversarial days
refused correctly (`ValueSwap` x2, `LocaleFlip` x3, a `DuplicateLabel` escalation) and **9 were
benign days**, every one refused by `RowKeyContinuity` -- directly, or through
`IndependentRungAgreement`'s VALUE-family fallback -- on a heal of `status` (twice also
`amount`): continuity 0.84-0.94 against the 0.95 heal threshold. Claim statuses genuinely move day to day,
and the comparison is with the last *accepted* run, so after a refusal the baseline ages and the
next heal of a churning field is judged against more churn -- refusals cluster (five in a row at
the end of the campaign). The gate errs toward refusing, as designed; the cost is a human review
of a correct heal. This is reported, not tuned away: the 0.95 threshold is Task 22's published
number. Whether a non-key field that churns by nature should be judged against a per-field
churn baseline is an open question for review, not a change made here.

Adversarial days the gate accepted (`DecoyColumn`, `HiddenDecoy` x3, `ImageHeader`) and the
promoted `VirtualisedRows` day are not scored here: whether each write came from the true column
is answered from the answer key by Task 26C's scorer, which the product cannot see.

## Pipeline decisions (Task 26A)

Each of these is a choice the assembled pipeline makes; each is stated so review can reject it.

- **The cache is written only from an accepted run** -- a `ValidatedExtraction` minted by the
  gate, or a human promotion that re-ran the gate -- and only for the fields that healed: a rung-1
  hit already is its cache entry, so re-writing it would only move `set_by_run_key` off the run
  that set it. `accept()` is the single call site of `land.write`, `cache.put` and the
  last-accepted move, in that order.
- **A cache read error fails the run** (fail-closed; ledger 395). One unreadable cache row stops
  every run of that target with an alert until the row is fixed with `record_cache_edit()`; it is
  never silently treated as a miss.
- **`RedactionFailed` quarantines the run**, even one the gate passed, with reason
  `evidence redaction failed`, the redaction verdict first and the gate's verdicts kept. No heal
  bundle, no last-accepted move, no lake write, no cache write.
- **A lake write that raises** (including post-write compaction, Task 28 M2) fails the attempt
  before the cache or last-accepted move; Prefect's retry lands the run again under the next
  attempt number, and the lake view deduplicates.
- **Quality evaluation runs only on accepted runs**, after the lake write. Its two refusals --
  `LookupError` (nothing landed) and `ScopeRefusal` (the scope record changed mid-run) -- leave the
  run's state as accepted (the rows are already landed), record `quality not evaluated: ...` on
  the run and the attempt, and post a `finding` alert.
- **A navigation the portal answers with a 5xx fails the attempt** (`PortalUnavailable`), rather
  than resolving against an error page.
- **An escalated field quarantines the run** with reason `escalated` and no rows; a retry would
  write a second resolutions row and corpus case. The heals of the other fields in such a run are
  not recorded as heal events: the run holds nothing to promote.
- **The history** (`orchestrate.history.history_for`, the one builder the gate and the quality
  screen share) is the accepted and promoted runs of the current spec version, in windows before
  the one being judged, with rows read back through the lake view.
- **A discard is not an intervention.** It writes nothing but the audited decision
  (`audit_log action=discard`); interventions count changes to what is extracted. The sanctioned
  manual cache edit is `orchestrate.interventions.record_cache_edit()`; nothing in v1 edits the
  cache by hand, so nothing calls it yet.
- **Promotion re-gates from stored evidence**: the held rows with their provenance, the heal
  bundle's `after` page (redacted), each field's last-attempt resolution with its extracted node
  translated into the stored page's node ids, and the target's own warm-up and first-heal policy.
  It supplies the human confirmation and never overrides a FAIL (`StillInvalid`: nothing written,
  the quarantine left open, `promote_refused` audited).
- **Injected session cookies** are handed to Chromium host-only (`domain` without a leading dot),
  and a `__Host-` cookie keeps that form: Chromium refuses the `url=` form for a Secure cookie on
  an http origin. Both are pinned on the wire by `tests/browser/test_session_cookies.py`.

## Declared, not done in Task 26A

- **Chromium's proxy-bypass rule for `https://host:443` vs `https://host/`** (Task 4, ledger 176)
  is still unverified: no real https host on port 443 is reachable from the test environment. If
  the rule does not match, every request to such a target is blocked by the dead proxy, so the run
  fails closed; it is to be confirmed on the first https target before its first scheduled run.
- **The API's use of `build_quarantine_actions()`** (`__main__ serve-api` handing the two
  callables to `create_app()`) is Task 34's: the API does not exist yet.
- **Task 23's statistical concerns** (null-sample PSI breaching on small samples; one unparseable
  reference value flipping a numeric field to categorical) are unchanged here; only the third --
  the reference window ignoring the spec version -- is closed, by `history_for`.
- **Task 29's minor findings M1, M3, M4** (the zero-row CRITICAL path and the `unavailable`
  DomSource fallback untested; `suppressed` unasserted on a marked-expected test; the shim
  Finding's `extra="forbid"`) are test and comment work in `quality/`, not pipeline wiring. M2 --
  a newly added field's null rate silently disabled, a fail-open -- is fixed. M5 (two history
  builders) is closed by `history_for`.
- **Task 29's concerns**: the `to_rail` payload keys (contract 2.4 leaves them unpinned) are
  unchanged; "two runs sharing a window merge into one history entry" no longer arises -- under
  one spec version a window has exactly one run key, and `history_for` filters to the version.
- **Task 28's minor findings M3, M6, M7** (a stray parquet breaking later writes; orphaned staging
  directories; provenance dropping only top-level values) are land-internal. M2's run-state
  mapping is decided above.
- **Task 25's minor findings** other than the promoted run's key check (which `_promote` now
  makes before anything lands): the `FOR UPDATE` pre-check, actor/reason validation tested only
  via promote, and the fixed one-second race sleep.
- **Task 29A's deployment concerns** (the worker's `deploys.jsonl` mount, the GitHub feed re-read
  each check, the webhook title as free text) belong to the deploy work, not the pipeline.
