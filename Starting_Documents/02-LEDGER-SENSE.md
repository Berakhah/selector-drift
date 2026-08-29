# 02 — Ledger Sense

**Vertical document intelligence with confidence routing, for construction progress billing.**

Weeks 5–12 · Repo: `ledger-sense` · Accent: ledger green
Depends on: `00-SHARED-FOUNDATION.md`

---

## HOW TO USE THIS DOCUMENT

```
Read 00-SHARED-FOUNDATION.md and then 02-LEDGER-SENSE.md, both in full.

Use the superpowers skills in order: brainstorming → using-git-worktrees →
writing-plans → subagent-driven-development → test-driven-development →
requesting-code-review → verification-before-completion.

Two things in this brief will feel like they contradict the source report I based
it on. They are corrections, not mistakes, and they are load-bearing:

  1. This CANNOT be a vision-model-first product. I have no GPU and a $0 budget,
     which means free-tier inference, which means the provider may train on inputs.
     Real client invoices therefore cannot go to a model. The extraction path must
     be deterministic-first. Section 3 explains the architecture that results.

  2. The AIA G702/G703 forms are copyrighted and actively enforced. I cannot ship
     them, recreate them, or distribute a corpus of them. Section 10, B1.

Design around both before writing any plan. Section 13 lists what I have left open.
```

---

## 1. MISSION

Documents in, validated accounting entries out — and everything the model is unsure about lands in a review queue instead of quietly becoming a wrong number.

One sentence for a buyer: *"I will measure what your current process actually costs you in week one, then automate the part that is safe to automate and show you the exceptions I caught that a human would have missed."*

---

## 2. THE EVIDENCE

### 2.1 What failed verification — and why that is the pitch

The entire category quotes an unverifiable industry average at prospects. Specifically:

- **"Manual invoice processing costs $13.11."** Attributed to Ardent Partners' *State of ePayables* with no year given, absent from the 2025 edition, and every search path terminates at document-automation vendors citing each other. Related figures — $12.98–15.97 for low-volume businesses, 32–40 invoices per accountant per day — trace to an unattributed name and to a blog citing its own earlier post.
- **"83% of knowledge workers overspend time on data entry."** Attributed to a Smartsheet survey containing no such figure. The real Smartsheet number is 40%, from 2017.

**This is a better position to sell from, not a worse one.** You will do the thing nobody else in the category does: **measure the client's actual cost in the first week** — document volume, minutes per document, loaded hourly rate, exception rate — and price against their real number. That conversation is itself the differentiator, and it produces a case study with a defensible figure instead of a borrowed one.

### 2.2 What survives

- **Salesforce, State of Sales (2024, n=5,500 across 27 countries):** sales representatives spend 9% of their time on manual data entry.
- **Versapay (2022, n=1,000 executives at $100M+ companies):** 82% said their company lost revenue due to miscommunication in the invoice-to-cash cycle. Note the actual wording — broader and vaguer than "invoicing conflicts."
- **Structural, and the strongest argument:** enterprise AP automation is priced and implemented for companies with a controller and an ERP. The forty-person contractor running QuickBooks and a shared inbox has nothing between "nothing" and a $500/month platform with a three-month implementation.
- **Market signal from the source report:** Ecommerce Management +130% and Supply Chain & Logistics PM +37% earnings growth on Upwork — back-office operational pain, document-heavy, under-tooled. And the report's own live-listing sample repeatedly shows document-extraction and back-office automation briefs with real budgets.

### 2.3 Why construction

High document pain, buyers with real money, almost no technical competition, and — critically — **domain rules that are objectively checkable**, which is what makes the validation layer a moat rather than a feature. Medical (EOBs, denial codes) and freight (BOLs, accessorials) are the alternates and are deliberately out of scope for v1.

---

## 3. THE ARCHITECTURE THE $0 CONSTRAINT FORCES

This section overrides the source report's description of the product. Read it carefully.

The source report specifies "vision-model structured extraction into a typed schema, with a confidence score per field." At $0 with no GPU, the only vision models available are free-tier hosted ones, and the most capable free vision tier states that content may be used to improve the provider's products. **A subcontractor's invoice with bank details on it is `CLIENT_CONFIDENTIAL` and cannot go there.** (`00`, R4.)

So the extraction pipeline inverts:

```
                                 ┌─────────────────────────────────────┐
 Document ──▶ Classify ──▶ Layout parse ──▶ Field extraction ──▶ Validate ──▶ Route
              (rules)      (Docling /       (anchor + geometry +   (domain     (confidence
                            PDF text        regex + table          rules)      thresholds)
                            layer / OCR)    reconstruction)
                                 │
                                 └── escalation, SYNTHETIC/REDACTED only ──▶ model lane
```

**Layer 1 — deterministic, handles the majority.** Digital PDFs carry a text layer with coordinates; pay applications and most subcontractor invoices are generated by software, not photographed. Extract by anchor text plus geometry: find the label, take the value in the adjacent cell. Table reconstruction from ruling lines and column alignment. Library candidates, all permissively licensed: `pdfplumber`, `Docling` (MIT), `PyMuPDF` (**AGPL — flag before adopting**, `pypdfium2` is the Apache/BSD alternative), `RapidOCR` or `Tesseract` for the scanned minority, `img2table` for ruled tables.

**Layer 2 — validation, and this is the actual product.** See §5. Domain rules catch errors that extraction confidence never would.

**Layer 3 — model escalation, tightly fenced.** A model lane exists for documents the deterministic path cannot handle. It may only receive payloads labelled `SYNTHETIC` or `REDACTED`, and `REDACTED` requires the client's written opt-in plus the verification pass in `bok-core`. For a client unwilling to opt in, the product still works — it just routes more to the review queue, which is honest and which you say out loud in the sales call.

**This constraint is a sales asset.** "Your documents never leave your infrastructure and are never sent to a model provider" is a sentence every enterprise AP vendor wishes it could say. Lead with it.

**Upgrade path to state, not hide:** if the client will pay for inference, a paid key with a no-training contractual term unlocks the vision lane. The abstraction already exists; it is a config change, not a rewrite.

---

## 4. DOCUMENT SCOPE (v1)

1. **Pay application, G702-format** — the summary certificate: contract sum, change orders, completed to date, stored materials, retainage, previous payments, current payment due.
2. **Continuation sheet, G703-format** — line-item schedule of values: item, scheduled value, previous work, this period, stored materials, total completed and stored, %, balance to finish, retainage.
3. **Subcontractor invoice** — the everyday case.
4. **Lien waiver** — four types (conditional/unconditional × progress/final). **Extract and validate only; never generate.** See B4.

Out of scope for v1: change orders, submittals, certified payroll, insurance certificates. Each is a v2 line item and each will be asked for.

---

## 5. THE VALIDATION LAYER — THE MOAT

Not OCR. Anyone can OCR. These are rules a general-purpose extractor cannot know, and each one is a demo moment.

**Arithmetic integrity**
- G703 line items must foot **exactly** to the G702 summary. Not approximately — exactly. A discrepancy is a finding, always.
- Per line: previous + this period + stored materials = total completed and stored.
- Percentage complete = total completed and stored ÷ scheduled value, to the contract's stated rounding.
- Balance to finish = scheduled value − total completed and stored.
- Current payment due = completed and stored − retainage − previous payments, plus approved change orders.

**Retainage**
- Retainage percentage matches the contract term.
- Variable per-line retainage (the G703 column reserved for it) reconciles to the summary retainage line.
- Retainage stepping: many contracts reduce the rate at 50% completion or release at substantial completion. Flag when the applied rate does not match the contract stage. This single rule catches real money and is the rule that makes a controller sit up.

**Stored materials**
- Material billed as stored but not installed must not also appear in completed work. **Double-counting stored materials is the classic pay-application error** and it is silent.
- When material moves from stored to installed, it must leave the stored column in the same period.
- Stored materials typically require backup (supplier invoice, proof of insurance, bonded storage) — flag when claimed without it.

**Change orders**
- Approved change orders reconcile to the adjusted contract sum.
- Work billed against an unapproved or absent change order is a flag, not a pass.

**Vendor and duplicate integrity**
- Duplicate detection across full history, not just the current period: same vendor + amount + date window, same invoice number, same line-item fingerprint.
- Vendor-name fuzzy resolution to a canonical record — with a **human confirmation step on first match**. Auto-merging two vendors is an unrecoverable error.
- **Bank-detail change detection.** If a vendor's payment details differ from the canonical record, that is a hard stop with a loud flag. Invoice redirection fraud is the single most expensive failure mode in AP, and catching one instance pays for the product for years. It is also why you must never auto-approve a change: see B5.

**Tax and jurisdiction**
- Sales/use tax applicability by jurisdiction and by material vs labour split.
- Tax-exempt project handling.

**Lien waiver integrity**
- Waiver type matches the payment stage (conditional-on-progress for a progress payment; unconditional only after funds clear).
- Through-date matches the billing period.
- Amount matches the payment.
- Some states mandate statutory forms with specific language — detect deviation and flag. Do not opine on it.

**Money arithmetic — a hard rule.** Never floats. `Decimal` with an explicit context, or integer minor units, throughout. Rounding policy documented and tested. Property-based tests via `hypothesis` on every arithmetic rule. A rounding bug in an accounting product is not a bug, it is the end of the client relationship.

---

## 6. CONFIDENCE ROUTING — AND THE CALIBRATION PROBLEM

The design is: high-confidence fields post automatically; anything below threshold enters a review queue showing the document with the field boxed.

**The trap nobody in this category addresses:** raw model or OCR confidence is not calibrated. A field reported at 0.94 is not correct 94% of the time. Routing on an uncalibrated score is guessing while displaying a number — precisely the sin this whole programme is a reaction to.

Requirements:
1. Every field carries a `Claim` with `method` and `confidence`.
2. `confidence` renders as **uncalibrated** (greyed, labelled) until a reliability curve exists for that field type.
3. Calibration is fitted on a labelled set (isotonic regression or Platt scaling), per field type, and the reliability diagram ships in `docs/methodology.md`.
4. Thresholds are set from the calibration curve against a stated target — e.g. "auto-post at the confidence where observed field accuracy exceeds 99.5%" — not chosen by feel.
5. Until calibrated, **everything routes to review.** A product that reviews everything and is honest beats a product that auto-posts and is wrong.

This is where Ground Truth (brief 04) attaches, and why it is sequenced after this project.

**Never claim full automation.** Buyers know it is false, and the claim is why they distrust incumbents. The honest metric — straight-through rate, published including the misses — is more persuasive to the buyer you actually want.

---

## 7. WRITEBACK — QUICKBOOKS ONLINE / XERO

The highest-consequence code in the product. You are writing to someone's books.

**Rules:**
- **Draft only.** Create unapproved bills / draft transactions. A human approves in the accounting system. v1 never posts an approved transaction, no matter what the client asks for.
- **Idempotency keys** on every write. A retry must not create a duplicate bill. Test this by force-failing mid-write.
- **Never write to a closed period.** Check the book-close date first; refuse and explain.
- **Full audit trail:** what was written, from which document, on whose approval, with a reversal path and a one-click undo that reverses cleanly.
- **Sandbox-first development.** Both platforms offer free developer sandboxes. Production app access requires review and takes time — **verify current requirements, review timelines and connection limits yourself before planning around them**; these change and I have not verified them for this brief.
- OAuth 2.0 token storage encrypted at rest with rotation; token compromise means access to a client's financials.

---

## 8. UI/UX SPECIFICATION

Light-first editorial instrument. Ledger green used for reconciliation states only.

**Screens**

1. **Baseline study.** The billable first week, and the first thing a client sees. Volume counter, a timing instrument for sampled documents, loaded-rate input, exception log. Output: a one-page measured-cost report with a real confidence interval, rendered as a print-ready document. This screen is the sales artifact.
2. **Inbox.** Email watcher + drag-and-drop. Documents as cards with type, vendor, amount, status, exception count. Keyboard-navigable.
3. **Review queue — the core screen and the signature moment.** Split view: the document rendered at fidelity on the left with the field under review boxed and everything else dimmed; on the right, the extracted value in an editable field, the validation rules that fired, and the provenance rail showing exactly how the value was derived. `J`/`K` moves between exceptions, `Enter` accepts, `E` edits, `R` rejects. A reviewer should clear fifty exceptions without touching the mouse. The signature detail: as you accept a field, the box on the document fills with a thin green rule that *stays*, so the document visually accumulates verification. That is the feeling the product sells — watching a document become trustworthy.
4. **Reconciliation view.** G703 lines against the G702 summary, side by side, with the discrepancy highlighted at the exact cell and the arithmetic shown inline. This is what makes a controller believe you understand their world.
5. **Vendor ledger.** Canonical vendors, aliases, payment-detail history with a change timeline. Bank-detail changes rendered as a red-lined diff with the date and source document.
6. **Metrics.** Documents processed, straight-through rate, exceptions caught, dollars reconciled against *their measured baseline*, and time saved with its calculation visible. Every figure is a `Claim`; every figure opens the rail. **Publish the misses too** — a metrics page that shows its own failure rate is more persuasive than one that doesn't.

**The demo that closes deals.** Sixty seconds processing twenty synthetic construction pay applications, where the exception queue catches a duplicate submission, a retainage miscalculation, and a stored-materials double-count. Show the straight-through rate honestly, misses included. A demo that shows its own failure modes is more persuasive than one that doesn't, to exactly the buyer you want.

---

## 9. COMMERCIALS

| Offer | Price |
|---|---|
| Baseline study | $2–4k · one week |
| Freelance build | $8–25k |
| SaaS | $500–1,500 / mo per firm |

**Best buyer: bookkeeping firms** — they feel the pain across 20+ clients at once, and one relationship gives you a document corpus, a design partner and a referral channel simultaneously.

---

## 10. BLINDSPOT REGISTER

**B1 — AIA copyright is real and enforced.** G702 and G703 are copyrighted by the American Institute of Architects (registered trademarks, active enforcement, a published copyright-violation reporting address). The purchaser's licence permits reproducing a limited number of copies of a *completed* form for a specific project only. Consequences, all mandatory:
- Never ship, recreate, or redistribute blank G702/G703 forms — not in the repo, not in the demo, not in docs.
- Never publish a corpus of real AIA forms.
- Your synthetic corpus uses **your own layout** with equivalent column semantics. Mimicking the column *math* is fine; reproducing the form's design is not.
- Describe outputs as "G702-format" or "pay application," and carry the non-affiliation notice that every competitor in this space carries: not developed, endorsed, approved, sponsored by or affiliated with the AIA; AIA®, G702®, G703® are registered trademarks of the American Institute of Architects; references are for compatibility description only.
- Processing a client's own purchased, completed form on their behalf is fine. Distributing it is not.
- Note also that the current G702/G703 edition is the 1992 revision, which is why real-world documents carry decades-old edition markings — build for that, and expect wide layout variation between the licensed PDF and the many software products that print onto it.

**B2 — The data boundary (see §3).** This is the one that ends the business if you get it wrong. A client discovering their subcontractor invoices went to a free tier that trains on inputs is a breach conversation, possibly a contractual one. Enforce it in code, test it, and put the guarantee in writing in your proposal.

**B3 — You have no documents and cannot get them easily.** Real pay applications are commercially sensitive. The plan: (1) a synthetic generator producing realistic pay applications with deliberately injected errors of each class — this is a first-class deliverable, not a test fixture, and it is what your demo runs on; (2) public agency and university bid packages sometimes publish completed pay-application-format sheets, useful for understanding real-world layout variance — treat with the copyright care in B1; (3) one design partner, run the baseline study at cost in exchange for a document corpus and a named case study, with a written data agreement covering use, retention and deletion. Treat that partner's validation rules as your product spec.

**B4 — Do not generate legal documents.** Lien waivers are legal instruments; several states mandate statutory forms with specific language. Generating one, or advising on which to use, edges toward unauthorised practice of law and creates liability out of proportion to the feature's value. Extract, validate, flag deviation, and stop. Say so explicitly in the UI and in the docs.

**B5 — Fraud detection creates liability in both directions.** If you flag a legitimate bank-detail change and payment is delayed, that is a client problem. If you *fail* to flag a fraudulent one and payment goes to an attacker, that is potentially your problem. Position: the tool surfaces changes and requires human confirmation; it never approves, never auto-updates a payment record, and the contract says the client is responsible for verification. This is a stance to take deliberately, with your terms written to match — not a detail to discover after an incident.

**B6 — Auto-merging vendors is unrecoverable.** Fuzzy name resolution will eventually decide that two genuinely different entities are one. Every first-time match requires human confirmation; merges are reversible; the canonical record keeps provenance for every alias.

**B7 — Confidence is uncalibrated (see §6).** The most common failure in this entire category, and the one you can most credibly claim to have fixed.

**B8 — Floating-point money.** Covered in §5 and repeated here because it is the single most common silent defect in accounting software written by generalists.

**B9 — You become a data processor the moment you accept a document.** Encryption at rest, retention policy with automatic deletion, documented deletion procedure, a signable data agreement, and full-disk encryption plus a dedicated work environment on your own machine. Prepare this before the first document arrives, not after the first client asks.

**B10 — Enterprise AP automation is a saturated category and buyers are sceptical.** Generic "AI invoice OCR" is approaching saturation. Your defence is the vertical: the validation rules, and writeback into the accounting system that vertical actually uses. If a discovery call turns into a feature comparison against a horizontal AP platform, you have already lost — steer back to the measured baseline and the domain rules.

**B11 — Scanned and photographed documents are the hard tail.** A phone photo of a crumpled invoice defeats geometry-based extraction. Handle it by routing to review with an honest "cannot extract" rather than guessing, and measure what proportion of the client's real intake this is *during the baseline study* — before you promise a straight-through rate.

**B12 — Accounting-platform API access is a gate you do not control.** Production access requires application review. Timelines, connection limits and requirements change. Verify current terms yourself, and have a CSV/IIF export fallback so the product is useful on day one even if API approval is pending.

**B13 — "The client's real per-invoice cost" must be measured defensibly.** The whole differentiator collapses if your baseline study is vibes. Specify the method: sample size, sampling frame, timing protocol, what counts as touch time, loaded-rate definition, and a reported confidence interval. Write it in `docs/methodology.md` and hand it to the client with the number.

---

## 11. TEST STRATEGY

- **Synthetic corpus generator** producing pay applications with each error class injected, at known ground truth. Every validation rule has a fixture that must trip it and a clean control that must not.
- **Property tests** (`hypothesis`) on every arithmetic rule: G703-to-G702 footing, retainage, stored materials, percentage complete. Invariants must hold across randomly generated schedules of values.
- **Money tests:** no float anywhere in the money path, enforced by a lint rule and a test.
- **Boundary tests:** `CLIENT_CONFIDENTIAL` cannot reach a free-tier lane. Redaction verification catches a deliberately planted identifier.
- **Writeback tests** against sandbox accounts: idempotency under forced retry, closed-period refusal, reversal correctness.
- **Calibration tests:** confidence renders as uncalibrated until a reliability curve exists; thresholds derive from the curve, not from constants.
- **Degraded mode:** with all providers disabled, the deterministic path still extracts and validates digital PDFs end to end.
- **E2E:** Playwright through the review queue, keyboard-only, fifty exceptions cleared without a mouse.
- **Accessibility:** axe, zero serious/critical.

---

## 12. DEFINITION OF DONE — WEEK 12

1. Baseline study tooling produces a print-ready measured-cost report with a stated method and confidence interval.
2. Deterministic extraction handles digital-PDF pay applications and subcontractor invoices end to end, no model required.
3. All validation rules in §5 implemented, each with a tripping fixture and a clean control.
4. Confidence routing live, with calibration state visible and thresholds derived from a reliability curve.
5. Review queue clears fifty exceptions keyboard-only.
6. QuickBooks Online **or** Xero draft writeback working against sandbox, with idempotency, closed-period refusal and reversal proven by test.
7. Synthetic corpus generator shipped and documented; the sixty-second demo runs on it.
8. Boundary enforcement proven: no client document can reach a free-tier model.
9. `docs/methodology.md` states the baseline-study method, the calibration basis, and the measured straight-through rate including misses.
10. AIA non-affiliation notice present; no AIA form artifacts anywhere in the repo.
11. First paying client, or a signed design-partner agreement with a document corpus.

---

## 13. OPEN DECISIONS — ASK ME THESE

1. **Do I have a design partner in reach** — a bookkeeper, a contractor, a construction controller — or does the plan need to assume synthetic-only through week 12? This changes the entire sequence.
2. **QuickBooks Online or Xero first?** Pick one; supporting both in v1 doubles the highest-risk surface. Which does the construction SMB segment I can actually reach use?
3. **Subcontractor invoices first, or pay applications first?** Invoices are higher volume and easier; pay applications are the moat and the harder demo. I lean pay applications for differentiation — argue me out of it if the risk is wrong.
4. **Am I willing to offer a fully local, air-gapped deployment** as the premium tier? It is a strong differentiator and it means supporting a deployment I cannot debug remotely.
5. **What is my stance on the fraud-detection liability (B5),** and do I need a lawyer to review my terms before the first client? My instinct is yes before any paid engagement.
6. **Self-hosted Postgres only, or is Supabase acceptable** given that the data is client financial documents? A third-party data processor is a question the client will ask.
7. **How much construction domain knowledge do I actually have?** If the honest answer is "little," the plan needs a week of domain immersion — reading real pay applications, talking to a controller — before any validation rule gets written. Rules invented from a web search will be subtly wrong in ways that destroy credibility on the first call.
