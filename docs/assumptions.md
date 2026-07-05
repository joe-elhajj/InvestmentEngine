# Owned Assumptions

Every number the engine uses that is not a measured fact lives here.  Assumptions
are updated **only when their external anchor changes**, never to alter a desired
output.  Each change to this file should be committed with a clear explanation of
why the anchor moved — the config hash in every DurabilityScore makes every change
an auditable event.

---

## Valuation assumptions (`config.yaml → valuation`)

| Assumption | Value | External anchor | Review cadence |
|---|---|---|---|
| `assumed_tax_rate` | 21 % | U.S. statutory corporate rate (Tax Cuts and Jobs Act 2017) | On any federal tax-rate change |
| `normalized_fcf_years` | 5 years | Standard analyst convention for a mid-cycle FCF estimate that irons out one-off items; last-N avoids overweighting stale data | When the typical cycle length for the coverage universe changes (e.g., extending to 7 for capital-intensive sectors) |
| `min_history_years` | 4 years | Minimum annual data points required before a delivered-growth CAGR is considered reliable; below this the CAGR endpoint sensitivity is too high | As needed; consider raising to 5 if coverage skews toward recently-listed companies |
| DCF `wacc` (base) | 9 % | Risk-free 10Y Treasury (~4.5 %) + equity risk premium (~4.5 %) | Annually, or when the 10Y moves >100 bps for >3 months |
| DCF `wacc` (bear) | 11 % | Same anchor, stressed by +200 bps | Same as base |
| DCF `wacc` (bull) | 8 % | Same anchor, relaxed by -100 bps | Same as base |
| DCF `terminal_growth` (base) | 2.5 % | Long-run nominal U.S. GDP growth consensus | Annually |
| DCF `terminal_growth` (bear) | 1.5 % | Same anchor, stressed | Same as base |
| DCF `terminal_growth` (bull) | 3.0 % | Same anchor, optimistic | Same as base |
| DCF `projection_years` | 5 | Standard analyst convention | As needed |

---

## Durability scoring assumptions (`config.yaml → durability`)

### Weights

These reflect analyst judgment on the relative importance of each category.
They are documented, not discovered, and change only when the investment thesis
about what drives durable compounding changes.

| Category | Weight | Rationale |
|---|---|---|
| `reinvestment_engine` | 30 % | ROIC × reinvestment rate is the primary compounder |
| `quality_persistence` | 25 % | Persistence distinguishes structural moats from cyclical luck |
| `balance_sheet_resilience` | 20 % | Optionality in downturns; avoids permanent impairment |
| `capital_discipline` | 15 % | Alignment: dilution and SBC erode per-share value |
| `optionality_proxies` | 10 % | R&D and capex intensity signal future reinvestment |

### Thresholds

| Threshold | Value | Rationale |
|---|---|---|
| `cost_of_capital` | 8 % | ROIC hurdle for the absolute scoring curve; below WACC = value destruction |
| `roic_threshold` | 15 % | ROIC level that counts as "strong" for persistence scoring |
| `stability_delta_threshold` | 5 pts | Composite swing under ±20 % reinvestment-rate perturbation that flags instability |

### Score band imputation

| Parameter | Value | Rationale |
|---|---|---|
| `pessimistic_impute` | 25 pts | Below-median fill for missing metrics when computing the low-band |
| `optimistic_impute` | 75 pts | Above-median fill for missing metrics when computing the high-band |

---

## Classification overrides (`config.yaml → classification.overrides`)

Analyst-owned overrides for security classification.  An override of `"operating"`
bypasses BOTH the form-history fund detection AND the financial-issuer SIC exclusion
(SIC 6000–6799) in `durability.score()`.  Overrides win over all automated inference.

| Ticker | Override | Rationale |
|---|---|---|
| `MARA` | `operating` | Bitcoin miner; files 10-K; SIC 6199 (Finance Services) would otherwise trigger exclusion despite being an operating company with production metrics |

**When to add an override**:
- Company files 10-K or 20-F (operating annual forms) but is misclassified due to its SIC code.
- You have confirmed via the SEC EDGAR filings that this is an operating business, not a financial intermediary.

**When NOT to use an override**:
- The company is a genuine financial intermediary (bank, insurer, REIT) — those exclusions exist for modeling reasons, not just SIC assignment.
- The company files fund forms (N-CSR, N-PORT, N-1A, 485BPOS) — those are definitive fund signals that overrides should not circumvent.

---

## Universe assumptions (`config.yaml → universe`)

| Assumption | Value | External anchor | Review cadence |
|---|---|---|---|
| Universe file | `config/sp500_universe.txt` | S&P 500 constituent list | Quarterly (March, June, September, December rebalance) |
| `version` | `2026-Q3` | S&P quarterly rebalance cycle | Bump immediately after each rebalance; commit as a dated, deliberate event |

**Rebalance protocol**: after each S&P constituent change, update
`config/sp500_universe.txt` and bump `universe.version` in `config.yaml`.
Commit both in a single commit with message "universe: rebalance YYYY-QN".
The config hash in every DurabilityScore will change, making cross-date
comparisons explicit rather than silent.

---

## Implied growth assumptions (`config.yaml → valuation.dcf`)

The reverse-DCF uses the **base scenario** WACC and terminal growth from above.
These are systematic — the same for every company — so that implied growth rates
are comparable across tickers.  The analyst never adjusts these per company.

| Parameter | Value |
|---|---|
| WACC used for implied growth | 9 % (base scenario) |
| Terminal growth used | 2.5 % (base scenario) |
| Projection years | 5 |
| Bisection bracket | −20 % to +60 % |

---

## Known SEC EDGAR data-source limitations (Session B.2)

These are not owned assumptions — nothing here has a value to review or an
anchor to update. They are genuine, diagnosed limitations of SEC EDGAR's
`companyfacts` payload and of `engine/edgar.py`'s multi-year series assembly,
recorded so a future session doesn't re-diagnose the same symptom from
scratch. Each was confirmed against raw `companyfacts` JSON, not inferred.

**Genuine data absence (not a bug).** NVDA's capex has zero annual
(10-K, full-year-duration) points under either candidate XBRL tag for
FY2012–FY2021 — only partial-year 10-Q cumulative points exist for those
years. GOOGL's diluted/basic share-count tags have zero history before 2022
in the same cached `companyfacts` payload that has full history for
`Revenues`/`NetIncomeLoss` back to 2013, ruling out cache staleness. Both are
absence-is-not-zero cases: the pipeline correctly reports `None` and a gap
for these ticker/period combinations rather than fabricating a value. There
is no fix — the data was never filed in a form this pipeline can parse.

**Share-count series discontinuities (Session B.2 S2, PR-A/PR-C).**
`_detect_split_contamination()` in `engine/durability.py` flags a single-year
≥2x or ≤0.5x jump in a diluted-share series as a probable discontinuity and
drops the series from `capital_discipline` scoring rather than scoring it as
extreme dilution (Option A / reject-and-gap, decided in Session B — Item 1).
Phase 1 of Session B.2 confirmed the likely mechanism against raw
`companyfacts` JSON for AAPL, NVDA, and AMZN: `_annual_points()`'s
prefer-latest-filed dedup causes some fiscal years to get retroactively
split-adjusted via a later filing's comparative reach-back (typically 2–3
years), while older years — which no later filing reaches back to — never
get restated. This produces a spurious jump at the *reach-back boundary*,
not the real corporate-action date; the flagged boundary's ratio matches the
company's real historical split ratio almost exactly, but the flagged fiscal
year does not match the real split date. Per the Option 1 decision,
`_annual_points()`'s selection logic is unchanged — the fix is disclosure
only: the gap message now names this mechanism as the likely cause (without
over-asserting certainty; a genuine unadjusted split is not ruled out) and
cites the two seam filings (form, SEC accession number, filed date) via
`Fact.source_ref()` (added in PR-A), so the claim is independently checkable
against the filings. The Phase 3 verification sweep (12 tickers: V, RKLB,
NVDA, META, CRM, CAT, BE, AXON, AAPL, GOOGL, TSLA, AMZN) confirmed zero
composite/discipline score movement from this change — every flagged
boundary is identical before and after; only the gap message text changed.

**Known limitation (backlog, not fixed):** this under-credits genuine
split/restructured companies on `capital_discipline` relative to identical
peers without a split — the category composite renormalizes over one fewer
sub-score instead of crediting real buyback behavior. Resolving this
requires an owned split/corporate-actions table (a future session), not a
heuristic guess at the adjustment factor.

**Open investigation, not yet fixed — "B.3" (read-only, queued before
Session C).** The Phase 3 sweep reconfirmed that META carries a genuine
`None`-year, which is why its `gross_profit` gap correctly stays present
(see the PR-B item below) — but this sweep did not re-derive the underlying
cause; that diagnosis (specific phantom dates, which XBRL concept, which
filing) was established in an earlier session and has not been re-verified
here. The suspected mechanism, not yet re-confirmed in this session: unlike
flow concepts, which `_annual_points()` filters by duration (350–380 days),
instant (balance-sheet) concepts have no duration to filter on and currently
have no fiscal-year-end *alignment* check in its place, so an off-cycle
instant snapshot embedded in a filing's footnote tables could be accepted as
if it were a real fiscal year-end. The fix shape, if the diagnosis holds: derive
the true annual period-ends from validated flow concepts and accept only
instants whose date matches one of them, rejecting off-cycle snapshots.
Because instant concepts feed invested capital (ROIC → reinvestment,
quality) and net debt/liquid assets/coverage (resilience), the blast radius
is potentially three of five durability categories on META — severity not
yet established. Full diagnosis (exact dates, exact concept, whether the
scoring functions actually ingest the phantom year or filter it out) is the
subject of the queued read-only B.3 session, not settled here.

**gross_profit false-gap reconciliation (Session B.2 S3, PR-B).**
`engine/pipeline.py`'s gap-accounting snapshotted `res.gaps` before the
per-year fallback computation ran, so a `gross_profit` gap could survive in
the reported gap list even when every year's fallback actually resolved a
value. The fix clears the gap only when every year in `annual_series` has a
non-`None` `gross_profit` after the fallback runs. The Phase 3 sweep found
this fires for CAT, GOOGL, and AMZN — not only the originally-diagnosed CAT
— because the condition is general, not CAT-specific. META's `gross_profit`
gap correctly stays present: it has a genuine `None` year from the B.3
phantom-period-end issue above, so the fallback cannot resolve every year,
and the gap is real, not a false positive. No composite or discipline score
changed for any of these tickers — this fix only removes a spurious entry
from the disclosed gap list.

---

## What this file is NOT

- It does not document EDGAR XBRL tag choices (those are in `engine/edgar.py`).
- It does not document scoring curve shapes (those are in `engine/durability.py`
  with inline comments).
- It does not record per-run outputs; for audit trails of specific scores, use
  the `config_hash` field stamped on every `DurabilityScore`.
