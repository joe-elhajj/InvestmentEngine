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

## What this file is NOT

- It does not document EDGAR XBRL tag choices (those are in `engine/edgar.py`).
- It does not document scoring curve shapes (those are in `engine/durability.py`
  with inline comments).
- It does not record per-run outputs; for audit trails of specific scores, use
  the `config_hash` field stamped on every `DurabilityScore`.
