# 04 — Ground Truth

**An eval and regression harness for one regulated task.**

Weeks 21–26 · Repo: `ground-truth` · Accent: measure slate-blue
Depends on: `00-SHARED-FOUNDATION.md`. Composes with `02-LEDGER-SENSE.md`.

---

## HOW TO USE THIS DOCUMENT

```
Read 00-SHARED-FOUNDATION.md and then 04-GROUND-TRUTH.md, both in full.
If Ledger Sense (brief 02) exists by now, read it too — this project consumes its
golden set and supplies its calibration curves.

Use the superpowers skills in order: brainstorming → using-git-worktrees →
writing-plans → subagent-driven-development → test-driven-development →
requesting-code-review → verification-before-completion.

The single most important instruction in this brief: DO NOT BUILD AN EVAL PLATFORM.
That category fails structurally and Section 4 explains why. Build a vertical
harness plus a public decision artifact. If the design starts growing a generic
runner, a plugin system, or a "bring your own task" abstraction, stop and tell me.

Section 13 lists the open decisions. Section 10 is where most of the real
difficulty lives — statistics, contamination, and judge bias. Read it before
designing anything.
```

---

## 1. MISSION

The honest answer to three questions no vendor will answer straight: **is it accurate enough, is the cheap model good enough, and did last week's silent model update break it.**

One sentence for a buyer: *"Before you build anything, I will tell you which of your intents are safe to automate, which must route to a human, and what it costs — with a published methodology and a number you can reproduce."*

---

## 2. THE VERIFICATION PASS MADE THIS PROJECT'S CASE FOR IT

The source report's first edition backed this project with precise-looking support-industry benchmarks: 41.2% median deflection, 58.7% top quartile, CSAT of 4.10 versus 4.30, per-intent scores of 4.41 and 3.34, re-contact 11.3% versus 8.7%.

**None of them exist.** They are attributed to Zendesk's *CX Trends 2026*, which is an attitudinal opinion survey containing no deflection rates, no medians, no quartiles and no CSAT scores of any kind. The figures appear on exactly one aggregator blog and nowhere else on the indexed web.

A widely-cited *"Gartner, February 2026"* finding that AI deflects 45% of queries but only 14% resolve turned out to be two unrelated numbers from an August 2024 release, fielded December 2023 — before LLM agents — welded together and redated. The 45% is customers saying the company misunderstood them.

**That is the market this project sells into, and it just proved its own thesis.**

Vendor deflection claims are single case studies presented as averages: Decagon's "80%" is Duolingo's number; Sierra's "80%" is Airtable's; Ada's "84%" is unqualified. Vendors define "resolution" themselves and routinely count deflection — the customer stopped talking — as resolution. Practitioners call it *containment theater*: a customer who gave up in frustration is scored as resolved.

**The macro picture that does survive, with its caveats stated:**
- **McKinsey, August 2026:** only 37% of enterprises attribute any EBIT impact to AI; just 6% attribute 5% or more while calling the impact significant.
- **McKinsey, November 2025:** no more than 10% of respondents report scaling AI agents in any individual function.
- **Gartner:** predicts over 40% of agentic projects cancelled by end-2027.
- **PwC 29th CEO Survey (n=4,454):** 56% say they have seen no significant financial benefit to date.
- **Stanford Foundation Model Transparency Index:** mean fell from 58 in May 2024 to 40.69 in December 2025. Models are becoming less legible as dependence on them grows.
- **MIT's famous 95%,** with the caveats stated plainly because you will be asked: 52 interviews and 153 conference-recruited survey respondents, roughly 80% of whom never piloted a custom AI tool; the success bar was sustained P&L impact within six months; all four authors build or sell agentic AI frameworks. The headline is weaker than it sounds. The qualitative finding underneath — budget went to the visible front office, payback showed up in the back office — is what actually matters.

---

## 3. THE TASK — PICK ONE AND OWN IT

**Recommended: pay-application and invoice field-extraction accuracy**, composing directly with Ledger Sense. Reasons: the golden set already exists by week 21, the domain partner already trusts you, the labels are objective (a number is right or wrong), and it closes Ledger Sense's calibration gap, which is that product's most serious weakness.

Alternates if that path is blocked: contract clause extraction; clinical note summarisation fidelity (higher stakes, harder access, PHI complications that collide badly with free-tier inference).

**Deliberately narrow. One task, owned completely, beats five tasks covered shallowly** — and narrowness is the only thing that keeps this from drifting into the eval platform trap.

---

## 4. THE TRAP, AND THE EXPLICIT NON-GOAL

**Do not build an eval platform.** The category fails structurally: the buyer must be technical enough to want APIs but not technical enough to run their own evals — a narrow band — and the feature keeps getting absorbed into observability tooling.

Build instead:
- a **vertical harness** — narrow, opinionated, one task;
- a **public decision artifact** — the benchmark and leaderboard with a stated methodology;
- a **paid diagnostic** — the triage deliverable in §5, which is the commercial wedge.

Non-goals, stated so they can be enforced in review: no plugin system, no generic task abstraction, no multi-tenant SaaS, no "bring your own eval," no observability product, no prompt management.

---

## 5. WHAT YOU BUILD

**Golden set** — 200–500 labelled items with a domain-specific rubric. **The rubric is the asset; the code is commodity.** The rubric defines, in writing, what counts as correct for each field type: exact match, numeric tolerance, normalised match, acceptable variants. Written before labelling, versioned, published.

**Graders** — deterministic wherever possible: exact field match, numeric tolerance, citation-exists, schema validity, arithmetic consistency. LLM-as-judge **only** where a deterministic grader genuinely cannot reach, and where used, you report **judge-versus-human agreement** with a confidence interval. Almost nobody does this, and it is a headline for the methodology page.

**Define your terms in public.** State exactly what "accurate," "resolved," "deflected," "correct" and "field" mean in your methodology. Given that the incumbent benchmarks are either self-defined or invented, a published definition is a genuine competitive asset.

**Model × cost matrix** — every candidate scored on accuracy, dollars per thousand items, and p95 latency. The buyer's real question is *"do I need a frontier model for this?"* and no vendor is incentivised to answer it. See B4 for the honesty problem this creates on a $0 budget.

**Calibration output** — reliability curves per field type, fitted and published, feeding Ledger Sense's confidence routing. This is the deliverable that makes brief 02's threshold routing legitimate rather than decorative.

**Regression watch** — scheduled re-runs alerting on drift when a provider silently updates a model behind a stable name, with proper statistics so noise is not reported as regression. See B3.

**The triage deliverable — the commercial wedge.** Ingest the client's historical task distribution and output: *"automate these six intents, route these four to a human, never automate these two,"* with the expected-value calculation behind each. Expected value, not accuracy: a 92%-accurate intent with a $4 cost of error is automatable; a 97%-accurate intent with a $40,000 cost of error is not. Making the cost of error explicit is the entire value of the artifact, and it is the conversation that leads to a build engagement.

---

## 6. ARCHITECTURE

```
ground_truth/
  golden/        item schema, rubric versioning, label storage, split management
  graders/
    deterministic/  exact, numeric_tolerance, normalised, schema, arithmetic
    judge/          constrained-output judge, agreement measurement, bias controls
  runner/        pytest-style harness (or inspect-ai / promptfoo if not owning the runner)
  stats/         bootstrap CIs, agreement (Cohen's / Krippendorff's), multiple-comparison
                 correction, sequential testing for regression alerts, MDE calculator
  matrix/        model × cost × latency evaluation, list-price cost model
  calibrate/     reliability curves, isotonic / Platt, export for consumers
  regression/    scheduled runs, drift detection, fingerprint tracking
  triage/        task distribution ingest, expected-value model, recommendation output
  publish/       static leaderboard generator, methodology renderer
web/             Next.js 15 + bok-ui (or a static site — see B9)
```

Result history in DuckDB; scheduled runs via the same orchestration as brief 03; every model call recorded through `bok-core`'s ledger (`00`, R7), which is what makes fingerprint tracking possible at all.

---

## 7. UI/UX SPECIFICATION

Light-first editorial instrument. Slate-blue used for measurement states only.

This product is a **published document with an interface**, not a dashboard. Design it like a well-set research report that happens to be interactive.

**Screens**

1. **Leaderboard — the public artifact and the signature screen.** Models ranked on the task, with accuracy, cost per thousand, p95 latency, and — the thing nobody else shows — **a confidence interval rendered as an actual interval, not a number**. Where two models' intervals overlap, the interface says so in words: *"not distinguishable at this sample size."* That single behaviour is the entire brand. A methodology link sits beside the title, not in a footer.
2. **Item explorer.** Every golden-set item, its label, each model's output, each grader's verdict. Filterable by failure mode. This is what a sceptical buyer opens to check you.
3. **Judge agreement.** Judge verdicts against human verdicts, agreement statistic with its interval, and the confusion matrix. Published, including where the judge is weak.
4. **Calibration.** Reliability diagram per field type, with the fitted curve, the sample size, and the resulting threshold recommendation. Exportable for Ledger Sense.
5. **Regression watch.** Score over time per model, with the significance test result stated, not implied. Alerts render with the test used and the p-value or interval — never a bare red arrow.
6. **Triage report.** The paid deliverable: intents ranked by expected value of automation, each with accuracy, cost of error, volume, and the recommendation, in a print-ready one-page format with the calculation shown.

**Copy discipline.** No claim without its sample. Every headline number carries its n and its interval. Empty states state what has not been measured yet rather than implying zero.

---

## 8. THE DISTRIBUTION MOVE

Publish the benchmark and the leaderboard, with a cost column and a stated methodology. In a category where the headline benchmarks turn out to be fabricated, **being the reproducible one is the entire moat.**

Publish alongside: the rubric, the public split of the golden set, the grader code, the analysis scripts, the raw per-item results, and a dated changelog. Someone must be able to re-run your numbers and get your numbers.

---

## 9. COMMERCIALS

| Offer | Price |
|---|---|
| Paid diagnostic | $3–8k · two weeks |
| Harness build | $10–30k |
| Regression monitoring | $1.5–4k / mo |

**Strategic value:** a paid foot in the door before any build, and it attaches to Projects 1–3. Every conversation the other three projects start can be opened with the diagnostic.

---

## 10. BLINDSPOT REGISTER

**B1 — You are labelling your own golden set, alone.** Single-annotator ground truth is a known weakness and a sceptical buyer will raise it. Mitigations, all of which go in the methodology: a written rubric authored *before* labelling; blind re-labelling of a random 20% after a two-week gap with intra-rater agreement reported; adjudication rules for ambiguous items recorded as decisions rather than resolved silently; and where possible a second labeller — the design partner from Ledger Sense — on a subset, with inter-rater agreement reported. Report the disagreement rate honestly. Reporting it is more credible than a suspiciously clean set.

**B2 — LLM-as-judge is biased in known, measurable ways.** Position bias (order of presented options), verbosity bias (longer answers scored higher), self-preference (a model favouring its own family's output), and sensitivity to prompt phrasing. Controls: randomise presentation order and test for order effects; strip length cues where possible; never use a model from the same family as the model under test as its sole judge; use n≥3 samples with majority vote; and measure judge-versus-human agreement with a proper statistic and interval. If agreement is poor, say so and fall back to deterministic grading rather than shipping a judge you cannot defend.

**B3 — Regression alerting is a statistics problem, not a threshold problem.** Daily re-runs plus a fixed threshold equals alert spam, then muted alerts, then a missed real regression. Requirements: a pre-registered analysis plan; bootstrap confidence intervals on every reported score; multiple-comparison correction across models × field types (Benjamini–Hochberg); a minimum detectable effect calculation so you know what your sample can and cannot see; and sequential testing so repeated looks at the data do not inflate the false-positive rate. State the test used in every alert.

**B4 — On a $0 budget your cost column is mostly zero, and that is dishonest if unhandled.** Free tiers make measured cost meaningless and free-tier latency is not representative of paid latency. Handle it explicitly: the cost column uses **published list prices for the paid tier**, clearly labelled as list price rather than measured spend; latency is reported as measured-on-free-tier with a prominent caveat that it is not a paid-tier latency estimate. Alternatively, report latency as a distribution and decline to rank on it. Fabricating a cost comparison would be the exact failure this entire programme exists to correct.

**B5 — Publishing your golden set contaminates it.** Anything public may be trained on by future models, after which your benchmark measures memorisation. Requirements: split the set into a public split (published, for reproducibility) and a **private held-out split that is never published** and against which the headline numbers are computed; publish hashes of the private items so you can later prove they existed at a given date without revealing them; rotate a portion of the private set periodically; and state the contamination policy on the methodology page. Almost no public benchmark does this properly and saying so is a differentiator.

**B6 — Models change silently behind stable names.** That is half the product's reason to exist and it is also a measurement hazard for you. Record everything available per call — model id, any system fingerprint or version metadata the provider exposes, response metadata, timestamp — and detect change statistically when metadata is absent. On free tiers you may not get a fingerprint at all; say so rather than implying you can pin a version you cannot.

**B7 — Temperature zero is not determinism.** Providers do not guarantee reproducibility even at temperature 0, and free tiers may route across hardware. Run n≥5 per item, report mean and variance, and treat any single-run number as provisional. A benchmark that reports a single decimal from one run is not measuring what it claims.

**B8 — Free-tier rate limits will shape your experimental design.** A 500-item golden set × 6 models × 5 runs is 15,000 calls, which no free tier will absorb quickly. Design for it: aggressive response caching (`00`, R6), batched scheduling across providers with different limits, overnight runs, and a documented run schedule. Also record which provider served each call — if you route the same model through two providers with different quantisation or serving stacks, you may be measuring the serving stack.

**B9 — Publicly benchmarking vendors creates exposure.** You are making public quantitative claims about commercial products. Requirements: a methodology page linked from every result; a right of reply with a published process and a stated response window; versioned, dated results with a changelog and never silent edits; measured claims only, no characterisation of a vendor's honesty; and a correction policy you actually follow. Consider whether the leaderboard should cover *models* rather than *vendor products* — models are a much lower-friction target and the comparison is more meaningful anyway.

**B10 — The golden set requires domain access you do not have.** Same solution as Ledger Sense: one design partner, deep. Sequenced after Ledger Sense, that partner is already yours and the documents are already labelled — which is the entire reason this project is fourth and not first. If Ledger Sense did not produce a partner, this project's plan must change, and that is a question for brainstorming rather than an assumption.

**B11 — Cost of error is the hardest input in the triage deliverable and the client must own it.** You cannot estimate what a wrong extraction costs their business. Interview for it, document the assumption in the client's own words, run sensitivity analysis across a range, and show how the recommendation changes across that range. A recommendation that flips at a plausible alternative assumption must be presented as flipping, not as a conclusion.

**B12 — Scope creep into the platform trap (see §4).** Enforce it in code review. Every time a "make it generic" instinct appears, it is the category failure arriving.

**B13 — The 26-week sequence may not survive contact with reality.** If earlier phases slipped, this project's dependency on Ledger Sense's golden set may not be satisfiable. It must be able to stand alone with a synthetic golden set — weaker, but shippable — rather than blocking. (`00`, B5.)

---

## 11. TEST STRATEGY

- **Grader tests.** Every deterministic grader has cases at its boundaries: numeric tolerance at the edge, normalisation of known variants, schema violations.
- **Judge bias tests.** Assert order randomisation occurs; assert an order-effect test runs and is reported; assert a same-family judge is rejected for a same-family model under test.
- **Statistics tests.** Bootstrap CIs validated against known distributions; multiple-comparison correction verified against a worked example; sequential testing verified to control the false-positive rate under a null simulation. **Statistics code without tests is how a benchmark becomes fiction.**
- **Contamination tests.** Assert private-split items never appear in published output. Assert published hashes match.
- **Reproducibility test.** Re-run the analysis from the published raw results and assert the published numbers regenerate exactly.
- **Calibration tests.** Fitted curves validated on held-out data; assert exported thresholds match the curve.
- **Degraded mode.** With all model providers disabled, deterministic graders still run against stored outputs and the analysis still produces a report.
- **E2E and accessibility** as per the shared foundation.

---

## 12. DEFINITION OF DONE — WEEK 26

1. Golden set built, 200–500 items, with a written rubric authored before labelling, versioned and published.
2. Public and private splits enforced in code, contamination policy published, private hashes published.
3. Deterministic graders for every field type where one is possible.
4. Judge implemented with bias controls, and judge-versus-human agreement measured, reported with an interval, and published — including where it is weak.
5. Model × cost × latency matrix, with cost as labelled list price and latency correctly caveated.
6. Calibration curves fitted per field type and exported in a form Ledger Sense consumes.
7. Regression watch running on a schedule with correct statistics and alerts that state their test.
8. Triage deliverable produced end to end for at least one real or realistic task distribution, with sensitivity analysis.
9. Public leaderboard deployed, with methodology, rubric, grader code, analysis scripts, raw results, changelog and right-of-reply process.
10. Reproducibility test passes: published numbers regenerate from published data.
11. `docs/methodology.md` defines every term used in every published number.

---

## 13. OPEN DECISIONS — ASK ME THESE

1. **Did Ledger Sense produce a design partner and a labelled corpus?** If not, this project's plan changes materially and I want to decide how before you write it.
2. **Own the runner, or adopt one** (`inspect-ai`, `promptfoo`)? Owning it is more work and more control; adopting one is faster and means my methodology sits on someone else's semantics.
3. **Leaderboard of models, or of vendor products?** Models are lower-friction and more meaningful; products are more commercially provocative and generate more attention. B9 makes me lean models.
4. **How large is the private split,** and how often does it rotate? This is a trade between statistical power and contamination resistance and I want it decided deliberately.
5. **Do I publish where my judge is weak?** I believe yes — it is the thesis — but argue the other side before I commit.
6. **Is the triage deliverable sellable standalone in week 22,** before the harness is finished? If so it should be sequenced first within this phase, because it is the revenue.
7. **What is my right-of-reply process,** concretely — contact method, response window, and what I publish if a vendor disputes a result?
