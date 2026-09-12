# Weight sensitivity

Computed 2026-09-12 UTC on revision `00d13c8c9546079a0df668c3d043941fd4cb6635`
(verified against GitHub `main`, including the reinvestment-stability correction).
Baseline durability fingerprint: `40b8e5a8706994c0`.
`config.yaml` SHA-256: `5962f90907bd1c5873270faa3dffae0caf9f55af17f486016ce2be78bd767190`.

The baseline weights from `durability.weights` are 30/25/20/15/10 percent
for reinvestment, quality, resilience, capital discipline and optionality.
R&D capitalization is enabled with a five-year amortization window in every
variant. No ticker was excluded or failed to derive.

## Results

Baseline descending rank:
V, NVDA, MSFT, AMAT, COST, META, GOOGL, AMZN, AAPL, CAT, CRM, TSLA, AXON, BE, RKLB.

| Weight perturbation | Kendall tau-b | Top five, descending composite | Membership change |
|---|---:|---|---|
| reinvestment_engine +5pp | 0.9429 | V, NVDA, MSFT, META, AMAT | META replaces COST |
| reinvestment_engine −5pp | 0.8857 | V, NVDA, COST, AMAT, MSFT | None |
| quality_persistence +5pp | 0.9810 | V, NVDA, MSFT, COST, AMAT | None |
| quality_persistence −5pp | 0.9810 | V, NVDA, AMAT, MSFT, COST | None |
| balance_sheet_resilience +5pp | 1.0000 | V, NVDA, MSFT, AMAT, COST | None |
| balance_sheet_resilience −5pp | 1.0000 | V, NVDA, MSFT, AMAT, COST | None |
| capital_discipline +5pp | 0.9048 | V, NVDA, AMAT, COST, MSFT | None |
| capital_discipline −5pp | 0.9429 | V, NVDA, MSFT, META, COST | META replaces AMAT |
| optionality_proxies +5pp | 0.9048 | V, NVDA, COST, AMAT, MSFT | None |
| optionality_proxies −5pp | 0.9619 | V, NVDA, MSFT, AMAT, META | META replaces COST |
| Equal 20/20/20/20/20 | 0.8095 | V, NVDA, COST, AMZN, AMAT | AMZN replaces MSFT |

The single-weight minimum is 0.8857; three single-weight variants change
top-five membership. Equal weights is a separate structural stress, not a
single-weight perturbation. These observations describe this sample and
input snapshot, not a guaranteed ranking floor or an endorsement of weights.

## Procedure and scope

Run `.venv/bin/python audit/sensitivity.py` from the repository root.
The unchanged harness loads `config.yaml`, derives each company once, and
calls `run_sweep` on the shared results. For each ±5pp change to weight `w`,
the other weights are multiplied by `(1 − new_w) / (1 − old_w)`.
Only the weight map changes; all other settings, including R&D treatment
and gates, remain fixed. No universe distribution is supplied to `score()`
by this harness, so peer-percentile sub-scores requiring it are omitted.

Use the harness's `kendall_tau` on baseline and perturbed composite vectors
in the same ticker order. Use `rank_order(scores, tickers)[:5]` for top five;
compare those ticker sets to identify entrants and exits. Tau handles score
ties; ranked lists use the ticker as their deterministic tiebreaker.

The 15-name sample is `ALL_TICKERS` in the harness. MSFT is included for
sensitivity coverage; this run does not independently validate its source
data. SEC data uses the client's normal cache/refresh policy and market
quotes are requested normally. Latest annual periods in this run are
2025 for most names, 2026-01-25 for NVDA, 2026-01-31 for CRM and 2026-06-30
for MSFT. Data refreshes can change the results even with unchanged code.
This is a sample weight sweep, not an S&P 500 robustness census or a test
of different R&D amortization windows.
