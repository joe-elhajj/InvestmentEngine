# Phase 1c/1d — Gate threshold curve and expectations-gap band census

Full S&P 500 universe, 502/503 tickers derived (99.8% coverage; 1
failure — `GDDY`, transient EDGAR read-timeout). Raw output:
`phase1cd_universe_raw.json`.

## 1c. Gate threshold sweep (4.0 → 8.0, step 0.5)

| Threshold | Gated | % of index | Gate-untestable | Pass/N.A. |
|---:|---:|---:|---:|---:|
| 4.0 | 40 | 8.0% | 94 (18.7%) | 368 |
| 4.5 | 33 | 6.6% | 94 | 375 |
| 5.0 | 28 | 5.6% | 94 | 380 |
| 5.5 | 19 | 3.8% | 94 | 389 |
| **6.0 (committed)** | **14** | **2.8%** | 94 | 394 |
| 6.5 | 11 | 2.2% | 94 | 397 |
| 7.0 | 10 | 2.0% | 94 | 398 |
| 7.5 | 10 | 2.0% | 94 | 398 |
| 8.0 | 10 | 2.0% | 94 | 398 |

Monotonically decreasing as expected; 6.0 sits cleanly in the tail with
real headroom before the curve flattens (7.0+). `gate_untestable` is
flat at every threshold (mechanically expected — untestability depends
only on data resolution, not the threshold value). **Disposition: gate
threshold VALUE affirmed (F-3a-style confirmation); gate-untestable
population size is F-6 (BUG, scoped separately).**

Names gated at the committed threshold (6.0): `BAX, CMS, CNP, GIS, HAS,
HPE, IFF, LITE, NEE, SJM, SNDK, SNPS, TAP, VTRS`.

## 1d. Expectations-gap-band PARTIAL census

| Band status | Count | Share |
|---|---:|---:|
| COMPLETE | 304 | 60.6% |
| NO_BAND | 185 | 36.8% |
| PARTIAL | 13 | 2.6% |

Edge that failed among the 13 PARTIAL names: **bear 9, bull 4** (bear
dominates as hypothesized; bull is a real 31% minority, not noise).
PARTIAL tickers: `DOC, FOX, FOXA, JBHT, LEN, PODD, PYPL, RCL, TER,
TRGP, TXN, URI, VRT`.

Fragility census among the 304 COMPLETE bands: **FRAGILE 85 (28.0%),
STABLE 219 (72.0%)**. More than 1 in 4 companies with a computable band
show a scenario-dependent sign flip.

**Disposition: F-3a (bracket VALUE) AFFIRMED** — a 2.6% PARTIAL rate is
low, not broad; the `[-0.20, +0.60]` bracket is not mis-sized for
today's market. **F-3b (bracket governance — hardcoded, undocumented)
remains open**, folds with F-4 into a future band-docs PR.
