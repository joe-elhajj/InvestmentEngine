# PR 2a — matched-window (Option C) vs Option A delta table

Regenerated fresh (v2, post no-adjustment-path routing fix) from two
live-cache captures:
- Option A: `main` @ `da977e2` (worktree), `rnd_capitalization.enabled=true` (local, not committed)
- Option C: `rnd-matched-window` branch, `rnd_capitalization.enabled=true` (local, not committed)

Fix note: AMZN/COST/V (NO_RND, no adjustment path) previously showed
`roic_mean_C=None` due to a routing bug (matched-window view applied
even with zero adjusted years, coercing genuine GAAP data to None).
Fixed to route through the untouched full-history GAAP path when no
adjustment path exists -- this table reflects the corrected values
(roic_mean_C now equals roic_mean_A for all three, byte-identical to
regime-off).

| Ticker | roic_mean A | roic_mean C | changed | compounding_proxy A | compounding_proxy C | matched-window GAAP mean (C) |
|---|---|---|---|---|---|---|
| AAPL | 0.3831 | 0.393 | True | 0.0568 | 0.0275 | 0.49310000000000004 |
| AMAT | 0.2341 | 0.2592 | True | 0.2236 | 0.0853 | 0.3281 |
| AMZN | 0.1814 | 0.1814 | False | 0.3991 | 0.3991 | None |
| AXON | 0.0978 | 0.0978 | False | 0.3816 | 0.3816 | 0.0262 |
| BE | -0.244 | -0.0532 | True | -0.5125 | -0.1117 | None |
| CAT | 0.1362 | 0.1362 | False | -0.0598 | -0.0598 | 0.147 |
| COST | 0.2857 | 0.2857 | False | 0.0565 | 0.0565 | None |
| CRM | 0.0464 | 0.0701 | True | 0.1882 | 0.232 | 0.0379 |
| GOOGL | 0.1827 | 0.2028 | True | 0.1612 | 0.1302 | 0.21030000000000001 |
| META | 0.2453 | 0.2663 | True | 0.8433 | 0.1983 | 0.2832 |
| MSFT | 0.2276 | 0.2145 | True | 0.1511 | 0.1091 | 0.2365 |
| NVDA | 0.2812 | 0.3107 | True | 0.2582 | 0.1843 | 0.37450000000000006 |
| RKLB | -0.2549 | -0.0212 | True | -23.638 | -1.9699 | None |
| TSLA | -0.0472 | 0.1103 | True | -0.0877 | 0.2049 | 0.0816 |
| V | 0.2955 | 0.2955 | False | 0.0832 | 0.0832 | None |

## changed flag summary

- AAPL: changed=True
- AMAT: changed=True
- AMZN: changed=False
- AXON: changed=False
- BE: changed=True
- CAT: changed=False
- COST: changed=False
- CRM: changed=True
- GOOGL: changed=True
- META: changed=True
- MSFT: changed=True
- NVDA: changed=True
- RKLB: changed=True
- TSLA: changed=True
- V: changed=False
