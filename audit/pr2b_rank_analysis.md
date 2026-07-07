# PR 2b — rank analysis (Evidence 2)

Composite ranks, regime OFF vs ON (amortization_years=5, shipped default).

**Kendall tau-b: 0.9048**

## Rank order

| Rank | OFF | ON |
|---|---|---|
| 1 | AMAT (82.89) | V (82.18) |
| 2 | V (82.18) | NVDA (80.26) |
| 3 | NVDA (79.60) | AMAT (77.71) |
| 4 | MSFT (78.37) | MSFT (77.61) |
| 5 | COST (77.46) | COST (77.46) |
| 6 | GOOGL (75.98) | META (76.75) |
| 7 | AMZN (75.80) | GOOGL (76.39) |
| 8 | AAPL (73.54) | AMZN (75.80) |
| 9 | META (70.40) | AAPL (73.79) |
| 10 | CAT (63.82) | CAT (63.25) |
| 11 | CRM (53.29) | CRM (55.89) |
| 12 | TSLA (41.16) | TSLA (55.38) |
| 13 | AXON (32.42) | AXON (39.38) |
| 14 | BE (26.39) | BE (27.88) |
| 15 | RKLB (16.49) | RKLB (16.81) |

## Full crossing table

Total pairwise rank transpositions: 5

| Pair | OFF values | ON values | Winner OFF | Winner ON |
|---|---|---|---|---|
| AAPL vs META | 73.5449 / 70.4000 | 73.7915 / 76.7527 | AAPL | META |
| AMAT vs NVDA | 82.8942 / 79.5962 | 77.7091 / 80.2558 | AMAT | NVDA |
| AMAT vs V | 82.8942 / 82.1777 | 77.7091 / 82.1777 | AMAT | V |
| AMZN vs META | 75.7954 / 70.4000 | 75.7954 / 76.7527 | AMZN | META |
| GOOGL vs META | 75.9838 / 70.4000 | 76.3921 / 76.7527 | GOOGL | META |

## Priority-set check

Priority set: ['AMAT', 'COST', 'GOOGL', 'META', 'MSFT', 'NVDA']

**5 crossing(s) involve a priority-set name:**

| Pair | OFF values | ON values | Score delta cause |
|---|---|---|---|
| AAPL vs META | 73.5449 / 70.4000 | 73.7915 / 76.7527 | AAPL Δ=+0.2467, META Δ=+6.3527 |
| AMAT vs NVDA | 82.8942 / 79.5962 | 77.7091 / 80.2558 | AMAT Δ=-5.1851, NVDA Δ=+0.6596 |
| AMAT vs V | 82.8942 / 82.1777 | 77.7091 / 82.1777 | AMAT Δ=-5.1851, V Δ=+0.0000 |
| AMZN vs META | 75.7954 / 70.4000 | 75.7954 / 76.7527 | AMZN Δ=+0.0000, META Δ=+6.3527 |
| GOOGL vs META | 75.9838 / 70.4000 | 76.3921 / 76.7527 | GOOGL Δ=+0.4084, META Δ=+6.3527 |

## Note on tau vs crossing table

Per the Session C lesson (tau can mask an adjacent single-pair swap):
tau-b=0.9048 is reported ALONGSIDE the full crossing table above,
not as a substitute for it. Every transposition is listed regardless
of whether it moves tau meaningfully.
