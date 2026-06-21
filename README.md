# Investment Engine — Phase 1 (deterministic fundamental core)

Pulls primary-source financials from SEC EDGAR, computes the full fundamental
picture (growth, margins, returns, leverage, peer-relative, valuation), and
writes an auditable Markdown report. **No LLM touches anything here** — that is
deliberate. The model enters in Phase 2 (red-flag extraction + the council),
operating on this engine's *output*, never on raw guesswork.

## Setup

```bash
cd "Investment Engine"          # the folder this unzips into
python -m venv .venv && source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
```

Then open `config.yaml` and set `sec.user_agent` to your real name + email.
**This is required** — SEC returns 403 without a declared contact.

## Run

```bash
python analyze.py AAPL                          # fundamentals only
python analyze.py AAPL --peers technology       # + peer comparison (universe in config)
python analyze.py AAPL --peers MSFT,GOOGL,DELL  # + peer comparison (explicit list)
python analyze.py AAPL --price 195 --shares 15300000000   # offline / reproducible
```

Output lands in `reports/<TICKER>_<date>.md`. The console prints every
peer inclusion/exclusion decision with its reason.

## Architecture (separation of concerns)

```
analyze.py            CLI + orchestration
engine/
  edgar.py            EDGAR client: ticker→CIK, SIC, companyfacts + concept resolution
  market.py           price/shares/market-cap (yfinance default; swap point for paid API)
  metrics.py          PURE math: CAGR, margins, ratios — unit tested
  peers.py            comp-set construction (you own it) + relative scoring
  valuation.py        relative multiples + 2-stage DCF + sensitivity grid
  pipeline.py         EDGAR data → derived metrics → peers → valuation
  report.py           render to Markdown with full lineage
config.yaml           all assumptions + curated peer universes (version-controlled)
tests/test_core.py    30 synthetic-data tests, no network
```

## Design decisions worth knowing (the "doing its best job" choices)

- **Concept resolution with fallbacks.** XBRL tags differ across companies and
  years. Each logical metric (revenue, FCF, ...) resolves against an *ordered*
  list of GAAP tags; the engine records which one actually hit. No silent
  guessing, and the report shows the exact concept used.
- **Provenance on every fundamental number.** Each figure cites its EDGAR
  concept, fiscal-period end, form, and filing date. The report's "Data gaps"
  section lists anything that could *not* be resolved, loudly — absence is never
  silently treated as zero.
- **You own the comp set.** Peers come from a curated candidate universe in
  `config.yaml`, then get filtered by SIC family + a size band + explicit
  exclusions. Every decision is logged with a reason. This is the line between
  an analyst's tool and a screener.
- **Assumptions live in config, never invented at runtime.** DCF WACC, terminal
  growth, and per-year FCF growth are yours, versioned, and printed alongside
  every output. The DCF always runs bull/base/bear plus a WACC × terminal-growth
  sensitivity grid, because a long-duration valuation is a *range*, not a point.
- **Meaningless ratios return n/a + a reason**, not a misleading number
  (P/E on negative earnings, ROE on negative equity, etc.).
- **Restated figures win.** When the same period appears in multiple filings,
  the most recently filed value supersedes the original.

## Known limits (honest, for Phase 2+)

- Fiscal years are labeled by period-end year; companies with off-calendar
  year-ends (e.g. Jan-ending retailers) get a label that can be off by one. The
  underlying data and CAGR spans use actual dates, so the math is correct.
- `yfinance` is free but fragile; isolate-and-swap is built in (`engine/market.py`).
- Trailing multiples only (no forward estimates yet).
- Red flags, qualitative review, and the council are Phase 2.

## Roadmap

- **Phase 2:** Claude reads the 10-K/proxy text for red flags (related-party,
  comp, accounting tells) → structured JSON with citations. Then the LLM Council
  takes this engine's report + your thesis and stress-tests it.
- **Phase 3:** Supabase storage + snapshots, n8n for scheduling/delivery, dashboard.
