# PR 2b — dual-regime rescore (Evidence 1)

Regime OFF vs ON (amortization_years=5, the shipped default), full
14-ticker audited set + MSFT. Every row where ANY value moves carries
a cause line. Non-reinvestment_engine categories (balance_sheet_
resilience, capital_discipline, optionality_proxies, quality_
persistence) are confirmed byte-identical OFF vs ON for all 15 tickers
-- only reinvestment_engine ever moves, since it's the only category
that consumes the R&D-adjusted view.

## Composite summary

| Ticker | Composite OFF | Composite ON | Δ | Cause |
|---|---|---|---|---|
| AAPL | 73.5449 | 73.7915 | +0.2467 | roic_latest via R&D-adjusted ROIC (raw 0.8181->0.533, score 100.0->100.0); roic_mean via R&D-adjusted ROIC (raw 0.4617->0.393, score 100.0->100.0); reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.0527->0.0699, score 45.26817341262684->46.988208505554155); compounding_proxy via R&D-adjusted ROIC (raw 0.0243->0.0275, score 12.162289325378135->13.731096409038448) |
| AMAT | 82.8942 | 77.7091 | -5.1851 | roic_latest via R&D-adjusted ROIC (raw 0.3319->0.2657, score 100.0->100.0); roic_mean via R&D-adjusted ROIC (raw 0.2846->0.2592, score 100.0->100.0); reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.7532->0.329, score 84.6780412143039->72.90426852063436); compounding_proxy via R&D-adjusted ROIC (raw 0.2144->0.0853, score 100.0->42.639473245521245) |
| AMZN | 75.7954 | 75.7954 | +0.0000 | no movement |
| AXON | 32.4167 | 39.3831 | +6.9664 | roic_latest via R&D-adjusted ROIC (raw -0.0127->0.077, score 0.0->48.11315996384581); roic_mean via R&D-adjusted ROIC (raw 0.0262->0.0978, score 16.357593539203968->61.12931748485953); reinvestment_rate via R&D-adjusted invested-capital delta (raw 18.5695->3.902, score 0.0->0.0); compounding_proxy via R&D-adjusted ROIC (raw 0.486->0.3816, score 100.0->100.0); short-history gap now live: reinvestment_engine: adjusted-window roic_mean rests on 3 of 3 available years (below min_history_years=4); reinvestment_engine: adjusted-window compounding_proxy rests on 2 of 2 available years (below min_history_years=4) |
| BE | 26.3920 | 27.8771 | +1.4851 | roic_latest via R&D-adjusted ROIC (raw 0.0617->0.0934, score 38.549670816625714->58.35094034137231); roic_mean via R&D-adjusted ROIC (raw -0.4254->-0.0532, score 0.0->0.0); reinvestment_rate via R&D-adjusted invested-capital delta (raw 6.0273->2.1008, score 0.0->0.0); compounding_proxy via R&D-adjusted ROIC (raw -2.5639->-0.1117, score 0.0->0.0); short-history gap now live: reinvestment_engine: adjusted-window compounding_proxy rests on 2 of 2 available years (below min_history_years=4) |
| CAT | 63.8241 | 63.2539 | -0.5702 | roic_latest via R&D-adjusted ROIC (raw 0.1853->0.1753, score 100.0->100.0); roic_mean via R&D-adjusted ROIC (raw 0.147->0.1362, score 91.89793949685725->85.09795508704946); reinvestment_rate via R&D-adjusted invested-capital delta (raw -0.392->-0.439, score 0.8026560795358222->0.0); compounding_proxy via R&D-adjusted ROIC (raw -0.0576->-0.0598, score 0.0->0.0) |
| COST | 77.4630 | 77.4630 | +0.0000 | no movement |
| CRM | 53.2905 | 55.8874 | +2.5970 | roic_latest via R&D-adjusted ROIC (raw 0.0993->0.1041, score 62.085779726507084->65.08098484848485); roic_mean via R&D-adjusted ROIC (raw 0.0195->0.0701, score 12.195419096825761->43.826289195391865); reinvestment_rate via R&D-adjusted invested-capital delta (raw 19.5717->3.3087, score 0.0->0.0); compounding_proxy via R&D-adjusted ROIC (raw 0.3819->0.232, score 100.0->100.0) |
| GOOGL | 75.9838 | 76.3921 | +0.4084 | roic_latest via R&D-adjusted ROIC (raw 0.2354->0.2227, score 100.0->100.0); roic_mean via R&D-adjusted ROIC (raw 0.1877->0.2028, score 100.0->100.0); reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.7407->0.6418, score 85.93454403581276->95.82406850687036); compounding_proxy via R&D-adjusted ROIC (raw 0.1391->0.1302, score 69.52708294522293->65.08257643073513) |
| META | 70.4000 | 76.7527 | +6.3527 | roic_latest via R&D-adjusted ROIC (raw 0.274->0.2527, score 100.0->100.0); roic_mean via R&D-adjusted ROIC (raw 0.2559->0.2663, score 100.0->100.0); reinvestment_rate via R&D-adjusted invested-capital delta (raw 3.3389->0.7447, score 0.0->85.53077912693851); compounding_proxy via R&D-adjusted ROIC (raw 0.8543->0.1983, score 100.0->99.17131724857782) |
| MSFT | 78.3675 | 77.6072 | -0.7603 | roic_latest via R&D-adjusted ROIC (raw 0.2849->0.2567, score 100.0->100.0); roic_mean via R&D-adjusted ROIC (raw 0.2437->0.2145, score 100.0->100.0); reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.5206->0.5084, score 92.06028116177451->90.83928098769348); compounding_proxy via R&D-adjusted ROIC (raw 0.1269->0.1091, score 63.44763193779833->54.5309017688258) |
| NVDA | 79.5962 | 80.2558 | +0.6596 | roic_latest via R&D-adjusted ROIC (raw 0.6639->0.5936, score 100.0->100.0); roic_mean via R&D-adjusted ROIC (raw 0.3359->0.3107, score 100.0->100.0); reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.5324->0.5932, score 93.24366676049189->99.31880392737396); compounding_proxy via R&D-adjusted ROIC (raw 0.1788->0.1843, score 89.42311288326489->92.14269621692968) |
| RKLB | 16.4864 | 16.8139 | +0.3275 | roic_latest via R&D-adjusted ROIC (raw -0.202->0.007, score 0.0->4.3665057210031675); roic_mean via R&D-adjusted ROIC (raw -0.4658->-0.0212, score 0.0->0.0); reinvestment_rate via R&D-adjusted invested-capital delta (raw None->92.7406, score None->0.0); compounding_proxy via R&D-adjusted ROIC (raw None->-1.9699, score None->0.0); short-history gap now live: reinvestment_engine: adjusted-window roic_mean rests on 2 of 2 available years (below min_history_years=4); reinvestment_engine: adjusted-window compounding_proxy rests on 1 of 1 available years (below min_history_years=4) |
| TSLA | 41.1600 | 55.3824 | +14.2224 | roic_latest via R&D-adjusted ROIC (raw 0.0466->0.0798, score 29.14568564728845->49.85822910744741); roic_mean via R&D-adjusted ROIC (raw -0.0692->0.1103, score 0.0->68.91964271889006); reinvestment_rate via R&D-adjusted invested-capital delta (raw 1.7846->1.8584, score 0.0->0.0); compounding_proxy via R&D-adjusted ROIC (raw -0.1235->0.2049, score 0.0->100.0) |
| V | 82.1777 | 82.1777 | +0.0000 | no movement |

## Per-ticker reinvestment_engine sub-score detail

### AAPL

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.8181 | 0.533 |
| roic_mean | 100.0 | 100.0 | 0.4617 | 0.393 |
| reinvestment_rate | 45.26817341262684 | 46.988208505554155 | 0.0527 | 0.0699 |
| compounding_proxy | 12.162289325378135 | 13.731096409038448 | 0.0243 | 0.0275 |

Category composite: 64.35761568450124 (OFF) -> 65.17982622864815 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.8181->0.533, score 100.0->100.0)
- roic_mean via R&D-adjusted ROIC (raw 0.4617->0.393, score 100.0->100.0)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.0527->0.0699, score 45.26817341262684->46.988208505554155)
- compounding_proxy via R&D-adjusted ROIC (raw 0.0243->0.0275, score 12.162289325378135->13.731096409038448)

### AMAT

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.3319 | 0.2657 |
| roic_mean | 100.0 | 100.0 | 0.2846 | 0.2592 |
| reinvestment_rate | 84.6780412143039 | 72.90426852063436 | 0.7532 | 0.329 |
| compounding_proxy | 100.0 | 42.639473245521245 | 0.2144 | 0.0853 |

Category composite: 96.16951030357598 (OFF) -> 78.8859354415389 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.3319->0.2657, score 100.0->100.0)
- roic_mean via R&D-adjusted ROIC (raw 0.2846->0.2592, score 100.0->100.0)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.7532->0.329, score 84.6780412143039->72.90426852063436)
- compounding_proxy via R&D-adjusted ROIC (raw 0.2144->0.0853, score 100.0->42.639473245521245)

### AMZN

- `is_fpi`: False, `rnd_state`: no_rnd
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.1619 | 0.1619 |
| roic_mean | 100.0 | 100.0 | 0.1814 | 0.1814 |
| reinvestment_rate | 0.0 | 0.0 | 2.2007 | 2.2007 |
| compounding_proxy | 100.0 | 100.0 | 0.3991 | 0.3991 |

Category composite: 75.0 (OFF) -> 75.0 (ON)

No movement.

### AXON

- `is_fpi`: False, `rnd_state`: partial_gap
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 0.0 | 48.11315996384581 | -0.0127 | 0.077 |
| roic_mean | 16.357593539203968 | 61.12931748485953 | 0.0262 | 0.0978 |
| reinvestment_rate | 0.0 | 0.0 | 18.5695 | 3.902 |
| compounding_proxy | 100.0 | 100.0 | 0.486 | 0.3816 |

Category composite: 29.089398384800994 (OFF) -> 52.31061936217633 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw -0.0127->0.077, score 0.0->48.11315996384581)
- roic_mean via R&D-adjusted ROIC (raw 0.0262->0.0978, score 16.357593539203968->61.12931748485953)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 18.5695->3.902, score 0.0->0.0)
- compounding_proxy via R&D-adjusted ROIC (raw 0.486->0.3816, score 100.0->100.0)
- short-history gap now live: reinvestment_engine: adjusted-window roic_mean rests on 3 of 3 available years (below min_history_years=4); reinvestment_engine: adjusted-window compounding_proxy rests on 2 of 2 available years (below min_history_years=4)

New gaps under ON (not present OFF):
- reinvestment_engine: adjusted-window roic_mean rests on 3 of 3 available years (below min_history_years=4)
- reinvestment_engine: adjusted-window compounding_proxy rests on 2 of 2 available years (below min_history_years=4)

### BE

- `is_fpi`: False, `rnd_state`: partial_gap
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 38.549670816625714 | 58.35094034137231 | 0.0617 | 0.0934 |
| roic_mean | 0.0 | 0.0 | -0.4254 | -0.0532 |
| reinvestment_rate | 0.0 | 0.0 | 6.0273 | 2.1008 |
| compounding_proxy | 0.0 | 0.0 | -2.5639 | -0.1117 |

Category composite: 9.637417704156428 (OFF) -> 14.587735085343077 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.0617->0.0934, score 38.549670816625714->58.35094034137231)
- roic_mean via R&D-adjusted ROIC (raw -0.4254->-0.0532, score 0.0->0.0)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 6.0273->2.1008, score 0.0->0.0)
- compounding_proxy via R&D-adjusted ROIC (raw -2.5639->-0.1117, score 0.0->0.0)
- short-history gap now live: reinvestment_engine: adjusted-window compounding_proxy rests on 2 of 2 available years (below min_history_years=4)

New gaps under ON (not present OFF):
- reinvestment_engine: adjusted-window compounding_proxy rests on 2 of 2 available years (below min_history_years=4)

### CAT

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.1853 | 0.1753 |
| roic_mean | 91.89793949685725 | 85.09795508704946 | 0.147 | 0.1362 |
| reinvestment_rate | 0.8026560795358222 | 0.0 | -0.392 | -0.439 |
| compounding_proxy | 0.0 | 0.0 | -0.0576 | -0.0598 |

Category composite: 48.17514889409827 (OFF) -> 46.274488771762364 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.1853->0.1753, score 100.0->100.0)
- roic_mean via R&D-adjusted ROIC (raw 0.147->0.1362, score 91.89793949685725->85.09795508704946)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw -0.392->-0.439, score 0.8026560795358222->0.0)
- compounding_proxy via R&D-adjusted ROIC (raw -0.0576->-0.0598, score 0.0->0.0)

### COST

- `is_fpi`: False, `rnd_state`: no_rnd
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.3945 | 0.3945 |
| roic_mean | 100.0 | 100.0 | 0.2857 | 0.2857 |
| reinvestment_rate | 59.77489646669791 | 59.77489646669791 | 0.1977 | 0.1977 |
| compounding_proxy | 28.243981627922903 | 28.243981627922903 | 0.0565 | 0.0565 |

Category composite: 72.0047195236552 (OFF) -> 72.0047195236552 (ON)

No movement.

### CRM

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 62.085779726507084 | 65.08098484848485 | 0.0993 | 0.1041 |
| roic_mean | 12.195419096825761 | 43.826289195391865 | 0.0195 | 0.0701 |
| reinvestment_rate | 0.0 | 0.0 | 19.5717 | 3.3087 |
| compounding_proxy | 100.0 | 100.0 | 0.3819 | 0.232 |

Category composite: 43.570299705833214 (OFF) -> 52.22681851096918 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.0993->0.1041, score 62.085779726507084->65.08098484848485)
- roic_mean via R&D-adjusted ROIC (raw 0.0195->0.0701, score 12.195419096825761->43.826289195391865)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 19.5717->3.3087, score 0.0->0.0)
- compounding_proxy via R&D-adjusted ROIC (raw 0.3819->0.232, score 100.0->100.0)

### GOOGL

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.2354 | 0.2227 |
| roic_mean | 100.0 | 100.0 | 0.1877 | 0.2028 |
| reinvestment_rate | 85.93454403581276 | 95.82406850687036 | 0.7407 | 0.6418 |
| compounding_proxy | 69.52708294522293 | 65.08257643073513 | 0.1391 | 0.1302 |

Category composite: 88.86540674525892 (OFF) -> 90.22666123440138 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.2354->0.2227, score 100.0->100.0)
- roic_mean via R&D-adjusted ROIC (raw 0.1877->0.2028, score 100.0->100.0)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.7407->0.6418, score 85.93454403581276->95.82406850687036)
- compounding_proxy via R&D-adjusted ROIC (raw 0.1391->0.1302, score 69.52708294522293->65.08257643073513)

### META

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.274 | 0.2527 |
| roic_mean | 100.0 | 100.0 | 0.2559 | 0.2663 |
| reinvestment_rate | 0.0 | 85.53077912693851 | 3.3389 | 0.7447 |
| compounding_proxy | 100.0 | 99.17131724857782 | 0.8543 | 0.1983 |

Category composite: 75.0 (OFF) -> 96.17552409387909 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.274->0.2527, score 100.0->100.0)
- roic_mean via R&D-adjusted ROIC (raw 0.2559->0.2663, score 100.0->100.0)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 3.3389->0.7447, score 0.0->85.53077912693851)
- compounding_proxy via R&D-adjusted ROIC (raw 0.8543->0.1983, score 100.0->99.17131724857782)

### MSFT

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.2849 | 0.2567 |
| roic_mean | 100.0 | 100.0 | 0.2437 | 0.2145 |
| reinvestment_rate | 92.06028116177451 | 90.83928098769348 | 0.5206 | 0.5084 |
| compounding_proxy | 63.44763193779833 | 54.5309017688258 | 0.1269 | 0.1091 |

Category composite: 88.8769782748932 (OFF) -> 86.34254568912982 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.2849->0.2567, score 100.0->100.0)
- roic_mean via R&D-adjusted ROIC (raw 0.2437->0.2145, score 100.0->100.0)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.5206->0.5084, score 92.06028116177451->90.83928098769348)
- compounding_proxy via R&D-adjusted ROIC (raw 0.1269->0.1091, score 63.44763193779833->54.5309017688258)

### NVDA

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.6639 | 0.5936 |
| roic_mean | 100.0 | 100.0 | 0.3359 | 0.3107 |
| reinvestment_rate | 93.24366676049189 | 99.31880392737396 | 0.5324 | 0.5932 |
| compounding_proxy | 89.42311288326489 | 92.14269621692968 | 0.1788 | 0.1843 |

Category composite: 95.6666949109392 (OFF) -> 97.8653750360759 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.6639->0.5936, score 100.0->100.0)
- roic_mean via R&D-adjusted ROIC (raw 0.3359->0.3107, score 100.0->100.0)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 0.5324->0.5932, score 93.24366676049189->99.31880392737396)
- compounding_proxy via R&D-adjusted ROIC (raw 0.1788->0.1843, score 89.42311288326489->92.14269621692968)

### RKLB

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 0.0 | 4.3665057210031675 | -0.202 | 0.007 |
| roic_mean | 0.0 | 0.0 | -0.4658 | -0.0212 |
| reinvestment_rate | n/a | 0.0 | n/a | 92.7406 |
| compounding_proxy | n/a | 0.0 | n/a | -1.9699 |

Category composite: 0.0 (OFF) -> 1.0916264302507919 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw -0.202->0.007, score 0.0->4.3665057210031675)
- roic_mean via R&D-adjusted ROIC (raw -0.4658->-0.0212, score 0.0->0.0)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw None->92.7406, score None->0.0)
- compounding_proxy via R&D-adjusted ROIC (raw None->-1.9699, score None->0.0)
- short-history gap now live: reinvestment_engine: adjusted-window roic_mean rests on 2 of 2 available years (below min_history_years=4); reinvestment_engine: adjusted-window compounding_proxy rests on 1 of 1 available years (below min_history_years=4)

New gaps under ON (not present OFF):
- reinvestment_engine: adjusted-window roic_mean rests on 2 of 2 available years (below min_history_years=4)
- reinvestment_engine: adjusted-window compounding_proxy rests on 1 of 1 available years (below min_history_years=4)

### TSLA

- `is_fpi`: False, `rnd_state`: present
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 29.14568564728845 | 49.85822910744741 | 0.0466 | 0.0798 |
| roic_mean | 0.0 | 68.91964271889006 | -0.0692 | 0.1103 |
| reinvestment_rate | 0.0 | 0.0 | 1.7846 | 1.8584 |
| compounding_proxy | 0.0 | 100.0 | -0.1235 | 0.2049 |

Category composite: 7.286421411822112 (OFF) -> 54.69446795658437 (ON)

Cause(s):
- roic_latest via R&D-adjusted ROIC (raw 0.0466->0.0798, score 29.14568564728845->49.85822910744741)
- roic_mean via R&D-adjusted ROIC (raw -0.0692->0.1103, score 0.0->68.91964271889006)
- reinvestment_rate via R&D-adjusted invested-capital delta (raw 1.7846->1.8584, score 0.0->0.0)
- compounding_proxy via R&D-adjusted ROIC (raw -0.1235->0.2049, score 0.0->100.0)

### V

- `is_fpi`: False, `rnd_state`: no_rnd
- config_hash: 9ae2790c5e9f1f4f (OFF) -> 3c07d718e40b3c81 (ON)

| Sub-score | Score OFF | Score ON | Raw OFF | Raw ON |
|---|---|---|---|---|
| roic_latest | 100.0 | 100.0 | 0.4128 | 0.4128 |
| roic_mean | 100.0 | 100.0 | 0.2955 | 0.2955 |
| reinvestment_rate | 68.17060834402011 | 68.17060834402011 | 0.2817 | 0.2817 |
| compounding_proxy | 41.61694244505325 | 41.61694244505325 | 0.0832 | 0.0832 |

Category composite: 77.44688769726834 (OFF) -> 77.44688769726834 (ON)

No movement.

