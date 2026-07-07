# PR 2b — window sensitivity (Evidence 3)

amortization_years in {3, 5, 7}, regime ON for all three. Composite
ranks compared pairwise (3v5, 5v7, 3v7): tau-b + full crossing table +
priority-set check for each pair.

## Composite by window

| Ticker | n=3 | n=5 | n=7 |
|---|---|---|---|
| AAPL | 74.1356 | 73.7915 | 71.8029 |
| AMAT | 76.5942 | 77.7091 | 77.5948 |
| AMZN | 75.7954 | 75.7954 | 75.7954 |
| AXON | 39.5717 | 39.3831 | 39.6893 |
| BE | 28.7443 | 27.8771 | 30.2611 |
| CAT | 64.5543 | 63.2539 | 68.4308 |
| COST | 77.4630 | 77.4630 | 77.4630 |
| CRM | 56.6044 | 55.8874 | 55.8708 |
| GOOGL | 76.5288 | 76.3921 | 76.5444 |
| META | 74.4394 | 76.7527 | 76.5787 |
| MSFT | 77.3395 | 77.6072 | 75.2717 |
| NVDA | 80.2402 | 80.2558 | 79.7207 |
| RKLB | 16.4864 | 16.8139 | 16.4864 |
| TSLA | 55.3561 | 55.3824 | 56.0857 |
| V | 82.1777 | 82.1777 | 82.1777 |

## n=3 vs n=5

**Kendall tau-b: 0.9048**

Total pairwise rank transpositions: 5

| Pair | n=3 values | n=5 values | Winner n=3 | Winner n=5 |
|---|---|---|---|---|
| AMAT vs COST | 76.5942 / 77.4630 | 77.7091 / 77.4630 | COST | AMAT |
| AMAT vs MSFT | 76.5942 / 77.3395 | 77.7091 / 77.6072 | MSFT | AMAT |
| AMZN vs META | 75.7954 / 74.4394 | 75.7954 / 76.7527 | AMZN | META |
| COST vs MSFT | 77.4630 / 77.3395 | 77.4630 / 77.6072 | COST | MSFT |
| GOOGL vs META | 76.5288 / 74.4394 | 76.3921 / 76.7527 | GOOGL | META |

Priority-set crossings (n=3 vs n=5): 5

| Pair | n=3 values | n=5 values |
|---|---|---|
| AMAT vs COST | 76.5942 / 77.4630 | 77.7091 / 77.4630 |
| AMAT vs MSFT | 76.5942 / 77.3395 | 77.7091 / 77.6072 |
| AMZN vs META | 75.7954 / 74.4394 | 75.7954 / 76.7527 |
| COST vs MSFT | 77.4630 / 77.3395 | 77.4630 / 77.6072 |
| GOOGL vs META | 76.5288 / 74.4394 | 76.3921 / 76.7527 |

## n=5 vs n=7

**Kendall tau-b: 0.9048**

Total pairwise rank transpositions: 5

| Pair | n=5 values | n=7 values | Winner n=5 | Winner n=7 |
|---|---|---|---|---|
| AMZN vs MSFT | 75.7954 / 77.6072 | 75.7954 / 75.2717 | MSFT | AMZN |
| COST vs MSFT | 77.4630 / 77.6072 | 77.4630 / 75.2717 | MSFT | COST |
| CRM vs TSLA | 55.8874 / 55.3824 | 55.8708 / 56.0857 | CRM | TSLA |
| GOOGL vs MSFT | 76.3921 / 77.6072 | 76.5444 / 75.2717 | MSFT | GOOGL |
| META vs MSFT | 76.7527 / 77.6072 | 76.5787 / 75.2717 | MSFT | META |

Priority-set crossings (n=5 vs n=7): 4

| Pair | n=5 values | n=7 values |
|---|---|---|
| AMZN vs MSFT | 75.7954 / 77.6072 | 75.7954 / 75.2717 |
| COST vs MSFT | 77.4630 / 77.6072 | 77.4630 / 75.2717 |
| GOOGL vs MSFT | 76.3921 / 77.6072 | 76.5444 / 75.2717 |
| META vs MSFT | 76.7527 / 77.6072 | 76.5787 / 75.2717 |

## n=3 vs n=7

**Kendall tau-b: 0.8476**

Total pairwise rank transpositions: 8

| Pair | n=3 values | n=7 values | Winner n=3 | Winner n=7 |
|---|---|---|---|---|
| AMAT vs COST | 76.5942 / 77.4630 | 77.5948 / 77.4630 | COST | AMAT |
| AMAT vs MSFT | 76.5942 / 77.3395 | 77.5948 / 75.2717 | MSFT | AMAT |
| AMZN vs META | 75.7954 / 74.4394 | 75.7954 / 76.5787 | AMZN | META |
| AMZN vs MSFT | 75.7954 / 77.3395 | 75.7954 / 75.2717 | MSFT | AMZN |
| CRM vs TSLA | 56.6044 / 55.3561 | 55.8708 / 56.0857 | CRM | TSLA |
| GOOGL vs META | 76.5288 / 74.4394 | 76.5444 / 76.5787 | GOOGL | META |
| GOOGL vs MSFT | 76.5288 / 77.3395 | 76.5444 / 75.2717 | MSFT | GOOGL |
| META vs MSFT | 74.4394 / 77.3395 | 76.5787 / 75.2717 | MSFT | META |

Priority-set crossings (n=3 vs n=7): 7

| Pair | n=3 values | n=7 values |
|---|---|---|
| AMAT vs COST | 76.5942 / 77.4630 | 77.5948 / 77.4630 |
| AMAT vs MSFT | 76.5942 / 77.3395 | 77.5948 / 75.2717 |
| AMZN vs META | 75.7954 / 74.4394 | 75.7954 / 76.5787 |
| AMZN vs MSFT | 75.7954 / 77.3395 | 75.7954 / 75.2717 |
| GOOGL vs META | 76.5288 / 74.4394 | 76.5444 / 76.5787 |
| GOOGL vs MSFT | 76.5288 / 77.3395 | 76.5444 / 75.2717 |
| META vs MSFT | 74.4394 / 77.3395 | 76.5787 / 75.2717 |

## Disposition recommendation (PENDING analyst sign-off)

Total crossings across all three pairwise window comparisons: 18.

**PENDING** -- per the calibration principles (bound, don't assert):
this section reports what was measured; it does not resolve the
disposition. The analyst owns the final call on whether the observed
rank movement across the 3/5/7 window is a measured non-issue or
requires a fragility annotation naming the affected ranks.
