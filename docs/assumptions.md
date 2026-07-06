# Owned Assumptions

Every number the engine uses that is not a measured fact lives here.  Assumptions
are updated **only when their external anchor changes**, never to alter a desired
output.  Each change to this file should be committed with a clear explanation of
why the anchor moved — the config hash in every DurabilityScore makes every change
an auditable event.

---

## Calibration principles

These govern how every assumption below is designed and every future one
should be. The system's goal is to be calibrated — neither systematically
optimistic nor systematically conservative.

1. **Bound, don't assert.** When an assumption's correct value is uncertain,
   measure whether the choice materially moves outputs (sensitivity on the
   audited set) rather than defending the point value. Immaterial
   assumptions get documented as measured non-issues; material ones get
   fragility annotations.
2. **Band over point.** Where an assumption has a scenario structure (e.g.
   WACC), signals derived from it are reported as ranges across scenarios,
   with the base case as the anchor. A signal whose sign flips across the
   band is disclosed as fragile, not reported at its base value alone.
3. **Abstain and disclose.** When an input required for an adjustment,
   gate, or signal is missing — or when an adjustment's error cannot even
   be signed — the system abstains, computes the unadjusted figure, and
   badges the abstention with its reason. It never defaults in either the
   flattering or the punishing direction.

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

**Sensitivity (2026-07-05, Session C Phase 2/4, baseline config_hash
`81dc10d28f028cad`) — all 5 weights: AFFIRMED.** Composite ranking robust
under one-at-a-time ±5pp — no single-weight perturbation drops Kendall tau
below +0.9429 vs baseline, and the top-5 SET is invariant (nothing outside
{AMAT, V, NVDA, MSFT, COST} enters under any single-weight move — MSFT
scored for this audit only, see the sensitivity-only note below). Two
disclosed knife-edges within that set: AMAT/V (baseline #1/#2, separated by
only 0.72 pts) swap rank-1 under `reinvestment_engine`−5pp and
`optionality_proxies`+5pp; and MSFT/COST swap under those same two plus the
equal-weights anchor. The equal-weights structural anchor
(20/20/20/20/20) drops tau to +0.8857 and additionally swaps NVDA/COST —
i.e. NVDA's edge over COST is weight-driven (from the reinvestment/quality
over-weighting relative to equal), not structure-driven. Neither knife-edge
involves a mispriced weight; both are low-confidence orderings the model
should not be leaned on for those specific adjacent pairs.

**Sensitivity (2026-07-05) — `optionality_proxies` (10 %): AFFIRMED +
ANNOTATED.** Being the smallest weight, ±5pp is a ±50% *relative* change —
the largest proportional stress any single weight receives in this sweep.
Its composite swings concentrate in the low-durability, speculative names
(RKLB ±1.83, BE ±2.22, AXON ±2.34) rather than the compounders (AMAT ±0.79,
COST ±0.38, NVDA ±0.04) — the weight matters most exactly where the
category composite itself is least trustworthy (thin sub-score coverage on
newer/smaller names), not where the analyst relies on it most.

**Sensitivity (2026-07-05) — `reinvestment_engine` (30 %): AFFIRMED +
ANNOTATED.** Downside asymmetry: −5pp is among the softest rank-breaks in
the sweep (tau +0.9429, tied with `optionality_proxies`+5pp) while +5pp
holds rank exactly (tau +1.0000). This weight has the least headroom to
reduce — cutting the primary-compounder weight is what actually moves
rankings; increasing it does not disturb the order the other weights
already agree on.

### Thresholds

| Threshold | Value | Rationale |
|---|---|---|
| `cost_of_capital` | 8 % | ROIC hurdle for the absolute scoring curve; below WACC = value destruction |
| `roic_threshold` | 15 % | ROIC level that counts as "strong" for persistence scoring |
| `stability_delta_threshold` | 5 pts | Composite swing under ±20 % reinvestment-rate perturbation that flags instability |

**Sensitivity (2026-07-05, Session C Phase 2/4) — all 3 thresholds:
AFFIRMED.** `cost_of_capital` and `roic_threshold` both hold exactly at
their baseline value (delta 0.0000, tau +1.0000 for every ticker) — see the
cliff-proximity backlog item below for `roic_threshold` specifically.
`stability_delta_threshold` is provably inert on the composite at every
tested value (4, 5, 6 — all delta 0.0000 for every ticker): by
construction (`durability.py`'s `is_stable = stability_delta <=
stability_delta_thresh`) it only ever gates the `is_stable` boolean flag,
never the composite or any category/sub-score.

### Score band imputation

| Parameter | Value | Rationale |
|---|---|---|
| `pessimistic_impute` | 25 pts | Below-median fill for missing metrics when computing the low-band |
| `optimistic_impute` | 75 pts | Above-median fill for missing metrics when computing the high-band |

**Sensitivity (2026-07-05, Session C Phase 2/4) — `pessimistic_impute` /
`optimistic_impute`: NOT DISPOSITIONED — untestable on the audited set.**
`_compute_composite`'s impute value only ever fires for a category with
zero sub-scores, and none of the 15 audited tickers (the Session B.4
fourteen plus MSFT) has one — `imputed_cats` is 0 and band width
(`composite_high` − `composite_low`) is 0.0000 for every ticker, under
every variant tested (20/80, 25/75, 30/70). This is a correct result, not
low sensitivity, and not the same as AFFIRMED: there is no empirical
evidence either way for these two values from this set. Practical exposure
is low regardless — imputes affect only the low/high band, never the point
composite, and no priority name currently triggers them. Testing this
properly requires `--universe` (a sparse-coverage name with a genuinely
missing category), not done as part of this disposition.

**Ledger-accuracy note (2026-07-05, Session C Phase 1.5).** This table's key
names were always correct — they document what `engine/durability.py`'s
`_resolve_config()` actually reads. `config.yaml`'s on-disk keys, however,
did not match: `thresholds` used `roic_cost_of_capital`/`roic_above_threshold`/
`stability_flag_delta` instead of `cost_of_capital`/`roic_threshold`/
`stability_delta_threshold`, and `score_band` was never read from config at
all (two module-level constants, `_IMPUTE_PESSIMISTIC`/`_IMPUTE_OPTIMISTIC`,
were used directly). Five of these ten values were dead on disk — editing
them in `config.yaml` silently did nothing, in direct violation of "never
hardcode assumptions in code; they belong in `config.yaml`." Values were
numerically identical to the code's own defaults, so no score was ever
affected by this bug; a Session C sensitivity audit surfaced it before any
value was ever perturbed. Fixed by renaming the on-disk keys to match, wiring
`score_band` through `_resolve_config` (replacing the two module constants),
and adding a strict-keys guard so an unrecognized key under
`durability.weights`/`thresholds`/`score_band` now raises instead of
silently no-oping. Rescored the Session B.4 fourteen tickers before/after:
composite, every category composite, every sub-score, and both score bands
were byte-identical — only `config_hash` changed (expected: `score_band` is
now part of the hashed resolved config, and the threshold section's keys
changed name).

### Session C findings not yet actioned

Two items the Phase 2 sweep surfaced, filed as backlog — **not resolved
here**. Neither is an assumption-value question; both concern how the
existing curves and thresholds interact with real company data, which
belongs in `engine/durability.py`'s scoring logic, not in this file's
values.

1. **`roic_threshold` cliff proximity.** NVDA, V, CAT, GOOGL, and AMZN each
   have a fiscal year whose ROIC sits within ~1pp of the 0.15 counting bar
   (min distance to threshold: NVDA 0.24pp, GOOGL 0.33pp, CAT 0.40pp, AMZN
   0.57pp, V 0.58pp). Because `roic_years_above_threshold` counts years on
   a strict binary (`ROIC >= roic_threshold`), these five names' durability
   is knife-edge on this one assumption in a way the dashboard currently
   never discloses — a small, real ROIC restatement or a future year's
   result landing a hair below 15% flips that year's count with no visual
   warning. Candidate: a disclosed dashboard marker (e.g. `CLIFF`),
   analogous to the existing `REV`/`MKT`/`WIN` basis-disclosure badges from
   Session B. Not implemented here.
2. **Curve saturation.** 35.9% of sub-scores (84/234) are pinned at floor
   or ceiling at baseline across the 15 audited tickers. The compounders
   ceiling-saturate on multiple categories at once (META 7/17, NVDA 6/16,
   COST 6/15, AMAT 6/17) — meaning the current curve shapes can't
   discriminate further among already-excellent names on those specific
   sub-scores. This doesn't affect current ranking (a saturated sub-score
   still contributes its ceiling value correctly), but it matters for any
   future use of the composite for position sizing rather than pure
   ranking, where two "both ceiling" names would look identical despite a
   real underlying difference the curve can no longer see. This is a
   curve-shape concern living in `engine/durability.py`'s scoring
   functions, not a `config.yaml` value — out of scope for a docs-only
   disposition.

### Session C follow-up: net_debt/EBITDA curve domain (fixed 2026-07-05)

`_score_resilience`'s `net_debt_ebitda` sub-score previously gated on
`if ebitda > 0:` with no else branch, so `ebitda <= 0` silently dropped the
metric regardless of `net_debt`'s sign — a levered, unprofitable company got
no worst-case floor, and a net-cash, unprofitable company got no credit and
no disclosure. Both outcomes are wrong in different directions, and neither
was visible in `ds.gaps`.

Fixed rule, applied only when `ebitda <= 0` (both `net_debt`/`ebitda`
non-`None`; the `ebitda > 0` branch and its existing thresholds are
unchanged):
- `net_debt > 0` (levered): `net_debt_ebitda` SubScore, `score=0.0`, raw
  `"EBITDA <= 0 with net debt — floored (worst-case debt service)."` — real
  score movement for any name in this branch.
- `net_debt <= 0` (net cash): no SubScore; a `ds.gaps` entry
  `"net_debt_ebitda: EBITDA <= 0 with net cash — outside ratio domain, not
  scored."` — disclosed, not silent, but no score is invented for a ratio
  with no defined sign here.

This curve-domain rule **post-dates every audit baseline above** (Session C
Phases 1.5/2/4, and the fourteen/fifteen-ticker sweeps they reference) —
those baselines were computed under the old silent-drop behavior. Rescored
the 8-ticker watchlist (V, NVDA, META, CAT, CRM, AXON, BE, RKLB)
before/after: RKLB is the only name with `ebitda <= 0` (net cash, so the
disclose-only branch fires), and its composite is byte-identical
before/after — only its `ds.gaps` disclosure changes, since a disclosed
non-domain gap renormalizes category weight exactly as the prior silent
omission did. No watchlist ticker currently falls into the net-debt-floor
branch, so no name shows real score movement today; any future name with
`net_debt > 0` and `ebitda <= 0` will.

## R&D capitalization assumptions (`config.yaml → durability.rnd_capitalization`)

GAAP expenses R&D immediately, which understates invested capital and
distorts ROIC comparability between R&D-heavy and capex-heavy businesses.
When enabled, the engine capitalizes R&D into a research asset (Damodaran
method): the last N years of R&D expense are amortized straight-line over
N years; NOPAT is adjusted by (current-year R&D − amortization); invested
capital is increased by the unamortized research asset balance. All
arithmetic is deterministic from the EDGAR R&D expense series; both GAAP
and adjusted ROIC are always reported with full lineage.

| Assumption | Value | External anchor | Review cadence |
|---|---|---|---|
| `enabled` | false (until regime-flip PR) | Regime toggle; config hash makes the active regime explicit on every score | On regime adoption, with dual-regime rescore evidence attached |
| `amortization_years` | 5 | Approximate product-cycle length across the coverage universe (Damodaran sector tables range 3y short-cycle tech to 10y pharma); uniform value preserves cross-ticker comparability at the cost of sector precision. Materiality to be bounded by a 3/5/7 window sensitivity in the regime-flip PR | If coverage skews toward long-cycle R&D (pharma, aerospace), revisit uniform vs. per-sector table |
| History-insufficiency rule | Full window or no adjustment | A partial research asset understates invested capital and overstates adjusted ROIC — failing in exactly the direction the adjustment exists to correct. Below N years of R&D history: GAAP ROIC only, badge R&D-UNADJ with reason | Fixed; structural |
| No-R&D distinction | Tag absent across all filings = legitimate zero adjustment (not a gap); tag present in some years but missing in others = gap, no adjustment, disclosed | Evidence-based classification: absence of the concept is not absence of the data | Fixed; structural |
| IFRS filer rule | FPIs (20-F filers) receive no adjustment; badge R&D-UNADJ ("IFRS filer — pending disposition") | IAS 38 already capitalizes development costs to an unknown degree; stacking the adjustment produces an error of ambiguous sign. Abstain-and-disclose (calibration principle 3) | On deliberate IFRS disposition (backlog) |
| Matched-window rule (Option C) | `roic_mean`/`reinvestment_rate`/`compounding_proxy` span ONLY the years that clear the full `amortization_years` research-asset window when the regime is on; the matched-window GAAP-basis mean (recorded in lineage for delta comparison) is derived from that SAME year-set | Isolates the capitalization effect as the only variable between the two means — a delta computed over mismatched windows would conflate "capitalization effect" with "different sample of years," making the comparison meaningless. Replaces the interim Option-A approach (a formerly-mixed name's average used a blend of adjusted and GAAP-fallback years); Option A's classifier/gap machinery is kept live as an invariant tripwire against a future regression that reintroduces mixing, not removed | Fixed; structural |
| Short-history disclosure threshold | `valuation.min_history_years` (existing key, currently 4 — not a new assumption) | Reuses the same reliability threshold already applied to delivered-growth CAGR: an adjusted-window mean resting on fewer years than that bar is just as unreliable as a CAGR computed the same way. Fires only when the regime is on and the window is non-empty but short — silent for NO_RND/FPI/fully-unadjusted names, where there's no window to call "short" (a near-universal disclosure there would be wallpaper, not signal) | Shared with `valuation.min_history_years`'s own review cadence |

Single-year `roic_latest` is unaffected by the matched-window rule — a
single year was never subject to the mixed-basis problem the window
restriction exists to fix, and restricting it would risk losing a young or
short-history company's latest-year ROIC entirely rather than reporting a
GAAP fallback. It keeps using the fallback-inclusive (Option A) view.

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

**Phantom period-ends — RESOLVED (Session B.3 diagnosis + Session B.4
fix, 2026-07-05).** Confirmed by B.3 (read-only) against raw `companyfacts`
JSON: unlike flow concepts, which `_annual_points()` filters by duration
(350–380 days), instant (balance-sheet) concepts had no fiscal-year-end
*alignment* check at all — an off-cycle quarterly snapshot embedded in a
filing's footnote tables (confirmed for META: `us-gaap:Assets` tagged at
2016-03-31/06-30/09-30, all from the FY2016 10-K, alongside the real
2016-12-31 point) was accepted as if it were a real fiscal year-end,
producing phantom entries in `res.annual_series`. B.3 also traced the
severity directly: the phantom entries' flow-derived fields (`nopat`,
`gross_margin`, etc.) are always `None` — no flow concept ever gets an
off-cycle annual point, since the duration filter is unconditional — so
`_score_reinvestment`/`_score_quality`/`_score_resilience` never ingested
them; a strip-and-rescore comparison confirmed composite and every category
byte-identical with and without the phantoms, for META, BE, and AXON (the
three affected tickers found in a 12-ticker scope check).

Session B.4 fixed this structurally (PR-1): derives the true fiscal-year-end
anchor set from the already duration-validated `revenue` series and rejects
instant points whose `end` isn't within 3 days of an anchor (absorbing
52/53-week calendar drift without admitting a ~90-day-off quarterly
snapshot). Falls back to unfiltered admission when revenue has no history
(shell/new listing). META's `annual_series` went from 18 to 15 entries
(the three 2016 phantoms removed); BE from 17 to 11; AXON from 19 to 17;
every other ticker unchanged. PR-2 additionally hardened
`_score_reinvestment`'s reinvestment-rate arithmetic to pair-gate
`invested_capital` and `nopat` on the *same* `YearlyDerived` entry (matching
`roic_vals`'s existing pattern) — closing a dormant bug where a duplicate-
year entry could contribute its own `invested_capital` to a delta paired
against a *different* real entry's `nopat` via a year-keyed lookup. This was
never observed firing (no phantom or near-anchor entry in the current
universe has `invested_capital` resolved), but it is no longer possible by
construction.

The Session B.4 verification sweep (14 tickers: the 12 above plus COST and
AMAT) confirmed zero composite/category movement, zero change to
`delivered_growth`/its label/`normalized_fcf`/`expectations_gap`, for every
ticker — including COST and AMAT, which have no off-cycle instants at all.

**Known limitation (backlog, not fixed).** PR-1's 3-day tolerance
correctly re-admits genuine near-fiscal-year-end instants (e.g. a "beginning
of year" snapshot tagged one day after the prior year's close), but those
survivors still take their `fiscal_year` label from `int(end[:4])` — the
instant's own year, not the fiscal year it actually represents. Confirmed
live: BE (`2019-01-01`, `2020-01-01`) and AXON (`2018-01-01`, `2019-01-01`)
each retain one such near-anchor entry per affected year, mislabeled one
year later than the FYE it represents, coexisting with the real entry for
that fiscal year — a duplicate-year condition (harmless today only because
these particular entries lack `invested_capital`; PR-2 makes the arithmetic
safe regardless). The structural cure is for a tolerance-matched instant's
`fiscal_year` to come from the *matched anchor's* fiscal year, not from its
own `end` date — not implemented here; flagged for a future session.

**gross_profit false-gap reconciliation (Session B.2 S3, PR-B) — corrected
2026-07-05.** `engine/pipeline.py`'s gap-accounting snapshotted `res.gaps`
before the per-year fallback computation ran, so a `gross_profit` gap could
survive in the reported gap list even when every year's fallback actually
resolved a value. The fix clears the gap only when every year in
`annual_series` has a non-`None` `gross_profit` after the fallback runs.
The Session B.2 Phase 3 sweep found this fired for CAT, GOOGL, and AMZN —
not only the originally-diagnosed CAT — and reported that META's gap
correctly stayed present, reasoning it had "a genuine `None` year" and
"the gap is real, not a false positive." **That reasoning no longer holds.**
Session B.4 (PR-1) proved the `None`-year was phantom-manufactured: once
the three off-cycle 2016 entries are removed, every one of META's *real*
years resolves `gross_profit` via the fallback, and the gap clears — exactly
like CAT, GOOGL, and AMZN. There was never a genuine gap; the phantom
entries were the only thing keeping PR-B's `all(...)` condition from being
satisfied. No composite or discipline score changed for any of these
tickers — this fix, and PR-1's downstream effect on it, only affect the
disclosed gap list.

---

## What this file is NOT

- It does not document EDGAR XBRL tag choices (those are in `engine/edgar.py`).
- It does not document scoring curve shapes (those are in `engine/durability.py`
  with inline comments).
- It does not record per-run outputs; for audit trails of specific scores, use
  the `config_hash` field stamped on every `DurabilityScore`.
