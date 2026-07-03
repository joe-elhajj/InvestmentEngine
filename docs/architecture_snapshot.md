# Architecture snapshot — for a FastAPI web layer

Read-only inspection, `2026-07-03`. All findings below are from reading the
current code on this branch, not from CLAUDE.md or memory.

## 1. Entry points

There is **no** `run_single_ticker(ticker) -> AnalysisResult` function. Single-ticker
analysis is only reachable by hand-assembling three calls, which is what
`analyze.py`'s `main()` does inline (`analyze.py:89-101`):

```python
client = EdgarClient(sec_cfg.get("user_agent", ""), sec_cfg.get("request_delay_seconds", 0.2))
cd     = client.get_company_with_latest_quarter(args.ticker, history_years)   # engine/edgar.py:362
quote  = get_quote(args.ticker, args.price, args.shares)                      # engine/market.py:26
res    = derive(cd, quote, cfg)                                              # engine/pipeline.py:334
```

Exact signatures:
- `EdgarClient.get_company_with_latest_quarter(self, ticker: str, history_years: int = 15) -> CompanyData` — `engine/edgar.py:362`
- `get_quote(ticker: str, manual_price: Optional[float] = None, manual_shares: Optional[float] = None) -> Quote` — `engine/market.py:26`
- `derive(cd: CompanyData, quote: Quote, config: dict) -> AnalysisResult` — `engine/pipeline.py:334`

This is the only way to get a single ticker's `AnalysisResult`; the batch
`screen.py` path (see §4) produces a different, lighter-weight `ScreenRow`,
not an `AnalysisResult`. If a web layer wants one-ticker analysis, it will
need to compose these three calls itself (or a thin wrapper should be added —
none exists today).

## 2. `AnalysisResult` shape

Defined in `engine/pipeline.py:70-89`, plain `@dataclass`, returned by `derive()`:

```python
@dataclass
class AnalysisResult:
    company: CompanyData
    quote: Quote
    derived: dict = field(default_factory=dict)          # latest single-value metrics
    growth: dict = field(default_factory=dict)            # CAGR metrics
    ratios: dict = field(default_factory=dict)             # M.Metric objects
    rel_val: Optional[V.RelativeValuation] = None
    dcf: dict = field(default_factory=dict)                # scenario -> DCFResult
    sensitivity: dict = field(default_factory=dict)
    gaps: list = field(default_factory=list)
    latest_quarter: dict = field(default_factory=dict)
    derived_lineage: dict = field(default_factory=dict)
    annual_series: dict = field(default_factory=dict)      # period_end -> YearlyDerived
    normalized_fcf: Optional[float] = None
    delivered_growth: Optional[float] = None
    delivered_growth_label: str = ""
    implied_growth_result: Optional[V.ImpliedGrowthResult] = None
    expectations_gap: Optional[float] = None
```

**Rendering is SEPARATE from computation.** `derive()` does no I/O and returns
a plain in-memory dataclass; nothing about it forces immediate rendering.
You can hold the `AnalysisResult` object, pass it around, cache it, serialize
it, and call the render functions on it later or multiple times (e.g. once
for HTML, once for markdown) — both renderers are pure functions of
`(AnalysisResult, peer_table)` (see §3). `analyze.py`'s `main()` happens to
call them back-to-back and write files immediately, but that's a caller
choice, not something baked into `derive()` or the render functions.

Two related dataclasses worth knowing about:
- `CompanyData` (`engine/edgar.py`) — raw EDGAR facts, ticker/CIK/SIC/forms/series.
- `YearlyDerived` (`engine/pipeline.py:26-63`) — per-fiscal-year derived metrics, one entry per `annual_series` key.

## 3. HTML render path

`engine/report_html.py:90`:

```python
def render(res: AnalysisResult, peer_table: list | None = None) -> str
```

Returns a plain Python `str` (a full self-contained HTML document) — it does
**not** write a file or touch stdout. `analyze.py` does the writing itself:

```python
html_out_path.write_text(RH.render(res, peer_table))   # analyze.py:120
```

The markdown equivalent is the same shape: `engine/report.py:52`,
`render(res: AnalysisResult, peer_table: list | None = None) -> str`.

Both renderers only import `engine.pipeline.AnalysisResult` for typing — no
network/disk access inside either module. Safe to call repeatedly, from any
thread/process, on an already-computed result.

## 4. Screen (batch) path

`engine/screen.py:1210`:

```python
def run_screen(
    tickers: list[str],
    cfg: dict,
    sort_mode: str = "durability",
    out_dir: Optional[Path] = None,
    verbose: bool = True,
) -> tuple[list[ScreenRow], list[EtfRow]]
```

Returns structured data — `(operating_rows, etf_rows)`, both lists of
dataclasses (`ScreenRow`, `EtfRow`, both in `engine/screen.py`) — regardless
of whether `out_dir` is given. File writing is conditional and separate:
when `out_dir` is not `None`, it additionally writes
`screen_<YYYYMMDD>.md`/`.html` via the internal `_render_md`/`_render_html`
functions. So `run_screen(tickers, cfg, out_dir=None)` gives you the rows
with zero file I/O — same separate-computation-from-rendering shape as §2/§3.

Per-ticker work inside `run_screen` goes through
`_process_one(ticker, client, cfg, history_years, ...)`, which builds its own
`EdgarClient`-driven `CompanyData` → `derive()` → `durability.score()` chain
per ticker — this is a parallel code path to `analyze.py`'s single-ticker
flow, not a wrapper around it. It does not produce an `AnalysisResult`
directly (though it calls `derive()` internally to get one, then discards
most of it down to a `ScreenRow`).

## 5. State / config

Config is loaded once, by the caller, and passed as a plain `dict` on every
call — there is no module-level cache or singleton:

```python
def load_config(path: str) -> dict:      # analyze.py:30
    with open(path) as f:
        return yaml.safe_load(f)
```

Every function that needs config takes it as a parameter (`derive(cd, quote,
config)`, `D.score(res, config, ...)`, `run_screen(tickers, cfg, ...)`, etc.).
Nothing reads `config.yaml` at import time.

Checked for module-level mutable state across `engine/*.py`: none found. The
only module-level assignments are constants (URLs, tuples, sets — e.g.
`engine/etf.py:28 FUND_QUOTE_TYPES`, `engine/edgar.py:36-44` SEC URL
templates). No global caches, no singletons, no `logging.basicConfig()` at
import time.

One piece of **instance-level** (not global) state to know about:
`EdgarClient._ticker_map` (`engine/edgar.py:264`) is populated lazily on
first `ticker_to_cik()` call and kept only for that client instance's
lifetime — it is not persisted to disk. A new `EdgarClient()` per request
would re-download the full SEC ticker list
(`https://www.sec.gov/files/company_tickers.json`, several MB) on every
request's first ticker lookup. For a long-lived server process, reusing one
`EdgarClient` instance (or otherwise caching the ticker map) avoids that.
This is orthogonal to the on-disk companyfacts/submissions cache in §6,
which the ticker map does not use.

This all looks safe to run inside a long-lived process: no import-time I/O,
no global config, cfg dict is just passed around. The one thing to design
around is deciding where a single `EdgarClient` (and its in-memory ticker
map) lives across requests.

## 6. EDGAR disk cache

`EdgarClient` (`engine/edgar.py:247-301`):
- Location: `cache_dir` constructor param, default `.cache/edgar` (relative
  to CWD), configurable via `config.yaml`'s `cache.dir` /
  `cache.ttl_seconds` (see `engine/screen.py`'s `run_screen`, which reads
  `cfg.get("cache", {}).get("dir", ".cache/edgar")`). `analyze.py`'s `main()`
  does **not** pass `cache_dir`/`cache_ttl_seconds` to `EdgarClient()` at all
  — it uses the constructor defaults (`.cache/edgar`, 86400s), so the CLI
  single-ticker path and the screen path land in the same on-disk cache
  directory by default.
- Key: `{cik}_{kind}.json` (`_cache_path`, `engine/edgar.py:269-270`) — e.g.
  `0000320193_submissions.json`, `0000320193_companyfacts.json`. Keyed by
  CIK (10-digit zero-padded string), not by ticker — two tickers that
  resolve to the same CIK share a cache entry, which is correct (tickers can
  change; CIK doesn't).
- TTL: default 86400s (24h), checked via file mtime (`_read_cache`,
  `engine/edgar.py:272-281`); expired entries are treated as a cache miss
  and re-fetched.
- **Not** cached: the SEC ticker→CIK map (see §5) — always a live fetch per
  `EdgarClient` instance's first use.
- **Concurrency safety**: plain `Path.write_text()` / `Path.read_text()`,
  no file locking, no atomic write (no write-to-temp-then-rename). Two
  concurrent requests for the same ticker will both miss the cache
  simultaneously (TTL check races), both hit the network, and both call
  `_write_cache` on the same path — the last writer wins with no
  corruption risk for a single writer, but a reader landing exactly between
  another process's `open(mode='w')` truncation and the subsequent
  `write()` completing could observe a partially-written file and get a
  `JSONDecodeError`. Low probability (single small `write_text()` call) but
  not guarded against. Worth a note if the FastAPI layer expects concurrent
  requests for the same ticker — either accept the small race, add a lock
  per cache key, or write-to-temp-then-`os.replace()` for atomicity.

## 7. Dependencies

`requirements.txt` (repo root), in full:

```
requests>=2.31
PyYAML>=6.0
pytest>=7.0
yfinance>=0.2.40
```

No `pyproject.toml`, no `setup.py`/`setup.cfg`, no lockfile. No FastAPI,
uvicorn, Flask, Starlette, or any ASGI/WSGI framework present anywhere in
the repo (checked via `grep -rl "fastapi\|uvicorn\|flask"` — zero hits).
Adding a FastAPI web layer means adding `fastapi` and an ASGI server
(`uvicorn`) as new dependencies; nothing today provides that surface.

Environment: Python 3.12.2 (both system `python3` and the repo's `.venv`).
No `.python-version` pin file.
