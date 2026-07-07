# Phase 1a — Weight/threshold/impute sensitivity, regime-ON

Fifteen names (Session B.4 fourteen + MSFT, sensitivity-only/not
A/B-verified). Baseline config_hash `39d382192c5e4f4f`. Full raw output:
`phase1a_sensitivity_raw.txt`.

## Kendall tau vs baseline, by perturbation

| Perturbation | Tau | Rank order changed? |
|---|---:|:---:|
| weight: reinvestment_engine +5pp | +0.9238 | yes |
| weight: reinvestment_engine −5pp | +0.9048 | yes |
| weight: quality_persistence +5pp | +0.9429 | yes |
| weight: quality_persistence −5pp | +1.0000 | no |
| weight: balance_sheet_resilience +5pp | +1.0000 | no |
| weight: balance_sheet_resilience −5pp | +0.9810 | yes |
| weight: capital_discipline +5pp | +0.9048 | yes |
| weight: capital_discipline −5pp | +0.9048 | yes |
| weight: optionality_proxies +5pp | +0.9238 | yes |
| weight: optionality_proxies −5pp | +0.9238 | yes |
| weight: equal-20/20/20/20/20 | +0.8286 | yes |
| threshold: cost_of_capital=0.07/0.08/0.09 | +1.0000 (all) | no |
| threshold: roic_threshold=0.125 | +0.9048 | yes |
| threshold: roic_threshold=0.15 (baseline) | +1.0000 | no |
| threshold: roic_threshold=0.175 | +0.9810 | yes |
| threshold: stability_delta_threshold (4/5/6) | +1.0000 (all) | no |
| impute: 20/80, 25/75, 30/70 | +1.0000 (all) | no |

**Minimum tau observed: +0.9048** (three specs) — below Session C's
documented regime-off floor of +0.9429. See report.md finding **F-5**.

## Top-5 set invariance — broken under `capital_discipline −5pp`

Baseline top 5 (by composite): V (82.18) > NVDA (80.26) > AMAT (77.71)
> MSFT (77.61) > COST (77.46), META 6th (76.75).

Under `capital_discipline −5pp`: V (81.82) > NVDA (81.02) > **META
(78.42)** > MSFT (78.28) > COST (77.68) > AMAT (77.45, now 6th).
**META enters the top 5, displacing AMAT** — traced from the raw
composite-delta table, not inferred from the crossing-count alone. See
**F-5**.

## Priority-set crossings (GOOGL/META/MSFT/NVDA/COST/AMAT)

| Perturbation | Crossings |
|---|---|
| weight: reinvestment_engine +5pp | GOOGL/COST, META/COST, META/AMAT, MSFT/AMAT |
| weight: reinvestment_engine −5pp | GOOGL/META, MSFT/COST, COST/AMAT |
| weight: quality_persistence +5pp | MSFT/COST, MSFT/AMAT, COST/AMAT |
| weight: capital_discipline +5pp | GOOGL/META, MSFT/COST |
| weight: capital_discipline −5pp | META/MSFT, META/COST, META/AMAT, MSFT/AMAT, COST/AMAT |
| weight: optionality_proxies +5pp | MSFT/COST, COST/AMAT |
| weight: optionality_proxies −5pp | GOOGL/COST, META/COST, MSFT/AMAT |
| weight: equal-20/20/20/20/20 | GOOGL/META, MSFT/COST, COST/AMAT |
| threshold: roic_threshold=0.125 | GOOGL/META, GOOGL/MSFT, GOOGL/COST, GOOGL/AMAT, MSFT/AMAT |
| threshold: roic_threshold=0.175 | MSFT/COST |

## Saturation census, regime-ON

AGGREGATE across 15 tickers: 81/236 pinned = **34.3%** (Session C
regime-off baseline: 35.9%, 84/234). Priority-name detail:

| Ticker | Pinned (regime-on) | Session C (regime-off) | Delta |
|---|---|---|---|
| META | 6/17 | 7/17 | −1 |
| NVDA | 6/16 | 6/16 | 0 |
| COST | 6/15 | 6/15 | 0 |
| AMAT | 5/17 | 6/17 | −1 |

All six priority names' pins are ceiling-only (zero floor pins). See
report.md — no new disposition needed beyond re-confirming Session C's
existing curve-saturation backlog item.

## Imputation testability — resolved this session

Session C: untestable (no ticker in the 15-name set had a fully-missing
category). This session: 66 real S&P 500 names (excluding 101 financial-
SIC-excluded names) have a genuinely partial category set. Tested on
all 66 — composite point delta is exactly 0.0000 in every case (correct,
by design); band width scales exactly linearly with the impute spread
and with the count of missing categories (e.g. ADM, one missing
category: 18.0/15.0/12.0 width at 20-80/25-75/30-70; ADP, two missing
categories: 30.0/25.0/20.0 — exactly double ADM's). See Phase 1e.
