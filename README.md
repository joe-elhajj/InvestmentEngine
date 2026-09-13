# Investment Engine

A three-tier investment research system. **Tier 1** is a fully
deterministic fundamental-analysis pipeline — SEC EDGAR filings are the
authoritative data source, every number is derived arithmetically with no
model involvement, and all assumptions live in version-controlled config.
**Tier 2** adds LLM-driven qualitative extraction layered on top of the
deterministic output (verbatim red/green flag extraction from 10-K text).
**Tier 3** is an LLM council that synthesizes Tier 1 signals and Tier 2
annotations into a final research memo. Tiers 2 and 3 consume Tier 1
output; they never alter it, and no model call anywhere in the codebase
ever derives, adjusts, or overrides a numeric result.

Two ways to use it: a one-shot CLI (`analyze.py`) for a single ticker's
Markdown/HTML report, and a local FastAPI web app + dashboard
(`app/`, `frontend/`) for an ongoing watchlist — batch durability
screening, the expectations-gap band, per-ticker Tier 2/3 drill-down.

## Setup

Use **CPython 3.12.2** (the tested interpreter) in a fresh virtual environment.
`requirements.txt` is the single exact-version lock for runtime and test
dependencies, including transitives. The setup targets macOS and
Linux (CI); other Python versions and Windows are not validated.

For a new checkout, use the commands below. For an existing clone, start
in its root directory and skip the first two commands.

```bash
git clone https://github.com/joe-elhajj/InvestmentEngine.git
cd InvestmentEngine
python3.12 --version                            # must report Python 3.12.2
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install pip==24.2
python -m pip install -r requirements.txt
python -m pip check
python -m pytest
```

Node.js 20 must also be on `PATH` for the JavaScript-backed tests, as in CI.
To update dependencies, change pins deliberately, resolve the complete
dependency set in a fresh environment, and run `pip check` and the full
test suite before committing the reviewed lock. Do not regenerate it from
an unrelated environment or upgrade packages as part of ordinary setup.
The optional macOS auto-start guide uses this same repository-local `.venv`.

Open `config.yaml` and set `sec.user_agent` to your real name + email —
**required**, SEC returns 403 without a declared contact.

Tier 2 (`/api/flags/{ticker}`) and Tier 3 (`/api/council/{ticker}`) need
`ANTHROPIC_API_KEY` in the environment at runtime; the CLI and Tier 1
web endpoints work without it.

## Run

**Single ticker, CLI:**

```bash
python analyze.py AAPL                          # fundamentals + durability
python analyze.py AAPL --peers technology       # + peer comparison (universe in config)
python analyze.py AAPL --peers MSFT,GOOGL,DELL  # + peer comparison (explicit list)
python analyze.py AAPL --price 195 --shares 15300000000   # manual market inputs; EDGAR still fetched
```

Output lands in `reports/<TICKER>_<date>.{md,html}`. The peer-comparison
path (`--peers`) is CLI-only today — the web app's analyze endpoints
don't wire it in.

**Web app + dashboard:**

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://localhost:8000`. Add tickers to a persistent watchlist
(SQLite, outside the repo at `~/.investment_engine/`), run a batch
durability screen, click any row to expand its full analysis inline —
financial position, growth, margins, valuation (including the
expectations-gap scenario band), durability scorecard, Tier 2 flags,
Tier 3 council, all from the same underlying `AnalysisResult`/
`DurabilityScore` the CLI produces. For auto-start-at-login and Dock
integration on macOS, see `app/INSTALL.md`.

**Batch screen, Python API:**

```python
from engine.screen import run_screen
# See run_screen() for arguments and supported sort modes.
```

## Architecture (separation of concerns)

```
analyze.py             Single-ticker CLI entry point
engine/
  edgar.py              SEC EDGAR client: ticker→CIK, SIC, companyfacts, concept resolution
  market.py              price/shares/market-cap (yfinance; isolated from filings)
  metrics.py             PURE math — CAGR, margins, ratios, R&D capitalization — unit tested
  pipeline.py             EDGAR data → derived metrics → valuation (no LLM or I/O)
  valuation.py            relative multiples, 2-stage DCF, reverse-DCF, the bull/base/bear
                          expectations-gap scenario band
  durability.py           five-category business-durability scorecard, the R&D capitalization
                          regime, and the balance-sheet-leverage gate (raw-metric veto)
  peers.py                comp-set construction (SIC + size band) + relative scoring — wired
                          into analyze.py's CLI only, not yet the web app
  universe.py             S&P 500 reference population for universe-relative percentile scoring
  etf.py                  ETF/fund profile via yfinance (market-vendor tier, fully defensive)
  screen.py               batch screener: routes tickers to Equities / ETFs & Funds / Excluded
  filings.py               Tier 2: fetch/parse 10-K text into Item 1/1A/7 sections
  flags.py                 Tier 2: LLM verbatim-selection flag extraction + validator
  council.py               Tier 3: adversarial council synthesis (5 advisors → peer review → Chairman)
  report.py                render AnalysisResult to Markdown, full EDGAR citation per figure
  report_html.py           render AnalysisResult to HTML (standalone doc + dashboard fragment)
  report_council.py        render a CouncilResult to self-contained HTML/PDF
app/                     FastAPI web layer wrapping the engine (watchlist, screen, analyze,
                          flags, council endpoints) — see app/INSTALL.md for the local-service setup
frontend/                 dashboard UI (vanilla JS + CSS, no build step)
config.yaml               every assumption + curated peer universes (version-controlled)
docs/assumptions.md       every owned assumption, its external anchor, and review cadence
audit/                    dated audit evidence and sensitivity harnesses (see below)
tests/                    pytest regression suite (synthetic fixtures + cached data)
```

## Design decisions worth knowing

- **Concept resolution with fallbacks.** XBRL tags differ across
  companies and years. Each logical metric resolves against an ordered
  list of GAAP tags; the report shows the exact concept used. Debt
  aggregation counts overlapping current portions only once.
- **Provenance on every fundamental number.** Each figure cites its
  EDGAR concept, fiscal-period end, form, and filing date. Every
  renderer's "Data gaps" section lists anything that could not be
  resolved, loudly — absence is never silently treated as zero.
- **Assumptions live in config, never invented at runtime.** DCF WACC,
  terminal growth, per-year FCF growth, durability weights/thresholds,
  the R&D-capitalization regime, and the balance-sheet gate's
  threshold/cap are all yours, versioned in `config.yaml`, and anchored
  with rationale in `docs/assumptions.md`. The reverse-DCF runs under
  all three owned scenario bundles (bull/base/bear), not just base, and
  discloses when the resulting signal's sign depends on which bundle
  you pick.
- **Config-hash discipline.** Every `DurabilityScore` embeds a
  16-hex-char fingerprint of the resolved assumption set (weights,
  thresholds, R&D regime, gates, universe version) so cross-company or
  cross-date comparisons under different assumptions are identifiable,
  never silently conflated.
- **A raw-metric gate caps the composite independently of the weighted
  score.** A company that's over-levered on net debt/EBITDA gets its
  durability composite capped regardless of how strong its other
  categories look — the gate reads raw filing data, never a derived
  sub-score, and both the gated and ungated numbers are always
  preserved and disclosed.
- **The only sanctioned model use is verbatim selection (Tier 2) and
  evidence synthesis (Tier 3).** No model call ever derives, adjusts,
  or computes a number. See `CLAUDE.md` for the full invariant list.
- **Meaningless ratios return n/a + a reason**, never a misleading
  number (P/E on negative earnings, ROE on negative equity, etc.).

## Engineering evidence

The [historical audit](audit/session_d/report.md), supporting files in
`audit/`, and [architecture snapshot](docs/architecture_snapshot.md)
record the repository at the time of inspection. They are retained as
engineering evidence, not a current unresolved-issue ledger. Subsequent
fixes and regression tests supersede findings in those snapshots.

## Known limits

- Fiscal years are labeled by period-end year; companies with
  off-calendar year-ends get a label that can be off by one. The
  underlying data and CAGR spans use actual dates, so the math is
  correct.
- `yfinance` is free but fragile; isolate-and-swap is built in
  (`engine/market.py`).
- Trailing multiples only (no forward estimates).
- Single-user, local-only web app — see `app/INSTALL.md`'s own
  limitations section (SQLite watchlist is single-process; the EDGAR
  disk cache isn't written atomically).
