"""
main.py — FastAPI application wrapping the existing engine.

This module composes existing engine entry points:
  - engine.analysis.run_single_ticker  (single-ticker wrapper)
  - engine.screen.run_screen           (existing batch screener)
  - engine.report_html.render          (per-company HTML renderer)

Run with:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import watchlist
from engine.analysis import run_single_ticker
from engine import durability as D
from engine.edgar import EdgarClient, SEC_TICKERS_URL
from engine.etf import fetch_etf_profile, FUND_QUOTE_TYPES
from engine.pipeline import AnalysisResult
from engine import report_html as RH
from engine.screen import _classify as _engine_classify
from engine.screen import run_screen

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
CONFIG_PATH = REPO_ROOT / "config.yaml"


# ---------------------------------------------------------------------------
# Lifespan — construct process-lifetime state once, not per-request
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    sec_cfg = cfg.get("sec", {})

    app.state.cfg = cfg
    # ONE EdgarClient for the process lifetime. Deliberate: per
    # architecture_snapshot.md §5, a fresh EdgarClient re-downloads the
    # multi-MB SEC ticker list on its first lookup; reusing one instance
    # means that cost is paid at most once per server run, not once per
    # request. /api/analyze and /api/search use this shared client;
    # /api/screen calls engine.screen.run_screen, which (unmodified)
    # constructs its own client internally — that is existing engine
    # behavior, not something this web layer changes.
    #
    # Cache concurrency note (architecture_snapshot.md §6): the on-disk
    # EDGAR cache (.cache/edgar/*.json) is read/written with plain
    # write_text()/read_text() — no file locking, no atomic
    # write-then-rename. For single-user local use, the tiny race window
    # on two simultaneous requests for the same brand-new ticker is an
    # acceptable, documented limitation. Not fixing it here — that would
    # be an engine/edgar.py change, out of scope for this web layer.
    app.state.client = EdgarClient(
        user_agent=sec_cfg.get("user_agent", ""),
        request_delay=sec_cfg.get("request_delay_seconds", 0.2),
        cache_dir=str(REPO_ROOT / cfg.get("cache", {}).get("dir", ".cache/edgar")),
        cache_ttl_seconds=int(cfg.get("cache", {}).get("ttl_seconds", 86400)),
    )

    # Ticker -> company name, for /api/search's "name" field. Kept separate
    # from EdgarClient's own ticker->CIK map (which discards the name) so
    # we don't have to touch engine/edgar.py to expose it. Lazily populated
    # on first search, cached for the process lifetime.
    app.state.ticker_names: Optional[dict[str, str]] = None

    # In-memory job store for /api/screen background runs. Process-lifetime
    # only (plain dict, no persistence) — cleared on restart, fine for
    # single-user local use.
    app.state.screen_jobs: dict[str, dict] = {}

    # Shared AnalysisResult cache for every analyze-family endpoint (full
    # page, /json, /fragment) — one EDGAR/yfinance fetch serves all three
    # renderings. Keyed by ticker -> (fetched_at, AnalysisResult); TTL from
    # config (default below matches the EDGAR disk cache's own default).
    # A per-ticker asyncio.Lock means two concurrent requests for a ticker
    # that isn't cached yet wait on each other instead of both hitting
    # EDGAR — the second one finds the cache warm once it gets the lock.
    app.state.analysis_cache = {}   # dict[str, tuple[float, AnalysisResult]]
    app.state.analysis_locks = {}   # dict[str, asyncio.Lock]

    yield


app = FastAPI(title="Investment Engine", lifespan=lifespan)

# CORS: this is a local, single-user tool served from one origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Watchlist endpoints
# ---------------------------------------------------------------------------

class AddTickerRequest(BaseModel):
    ticker: str
    # No client-supplied type: the backend resolves equity vs. ETF/fund via
    # the same evidence-based classification engine.screen.py already uses
    # (form history / yfinance quoteType) — never a client-chosen default.


_TICKER_RE = re.compile(r"^[A-Z0-9.]+$")


def _validate_ticker(raw: str) -> str:
    tk = (raw or "").strip().upper()
    if not tk:
        raise HTTPException(status_code=400, detail="Ticker must not be empty.")
    if len(tk) > 10:
        raise HTTPException(status_code=400, detail="Ticker must be 10 characters or fewer.")
    if not _TICKER_RE.fullmatch(tk):
        raise HTTPException(
            status_code=400,
            detail="Ticker may only contain letters, digits, and '.'.",
        )
    return tk


# ---------------------------------------------------------------------------
# Classification — evidence-based equity/ETF resolution, reused by both the
# search card's badge and the watchlist add flow. Never fabricates a default:
# a ticker that can't be resolved reports kind=None ("pending"), matching
# screen.py's own "absence is not a classification" discipline.
# ---------------------------------------------------------------------------

_CLASSIFICATION_LABELS = {
    "operating_domestic": "Equity — 10-K filer",
    "operating_fpi": "Equity — foreign private issuer (20-F/40-F)",
    # _classify()'s analyst-override branch returns the override value
    # verbatim (config.yaml's overrides use "operating", not
    # "operating_domestic") — same equity bucket, distinct label so the
    # badge is honest about why it resolved.
    "operating": "Equity — analyst override",
}


def _fund_via_yfinance_label(ticker: str) -> Optional[dict]:
    try:
        profile = fetch_etf_profile(ticker)
    except Exception:
        return None
    qt = (profile.quote_type or "").upper()
    if qt in FUND_QUOTE_TYPES:
        return {"kind": "etf", "label": f"ETF/Fund — yfinance quoteType={profile.quote_type}"}
    return None


def _resolve_classification(ticker: str) -> dict:
    """Returns {"kind": "equity"|"etf"|None, "label": str}. kind=None ("pending")
    means the evidence didn't resolve cleanly — never a fabricated default."""
    overrides = app.state.cfg.get("classification", {}).get("overrides", {})
    try:
        # history_years=1: classification only needs recent_forms/sic, not a
        # deep XBRL history — the EDGAR disk cache means this costs nothing
        # extra on the network side regardless.
        cd = app.state.client.get_company(ticker, history_years=1)
    except ValueError:
        # No EDGAR registrant — yfinance quoteType is the second evidence
        # source, same fallback screen.py uses.
        return _fund_via_yfinance_label(ticker) or {"kind": None, "label": "pending"}
    except Exception:
        return {"kind": None, "label": "pending"}

    classification, _evidence = _engine_classify(ticker, cd, overrides)

    if classification == "fund":
        return {"kind": "etf", "label": "ETF/Fund — fund forms observed"}

    if classification in _CLASSIFICATION_LABELS:
        # Filing a 10-K/20-F/40-F makes it "operating" by form history, but
        # a financial-SIC issuer can still be economically a fund (GLD files
        # 10-Ks as a trust, yet is a commodity ETF) — this is the same
        # SIC-range check durability.score() applies independently of form
        # history, and the same yfinance probe screen.py runs before
        # finalising a financial-SIC exclusion. A real bank (JPM) has no
        # yfinance fund confirmation and correctly stays "equity" — it's
        # still a stock, just one durability.py won't score. An analyst
        # override of "operating" bypasses this probe entirely, same as it
        # bypasses durability.py's exclusion.
        try:
            sic_int = int(cd.sic)
        except (ValueError, TypeError):
            sic_int = None
        overridden = overrides.get(ticker.upper()) == "operating"
        if sic_int is not None and 6000 <= sic_int <= 6799 and not overridden:
            fund_label = _fund_via_yfinance_label(ticker)
            if fund_label is not None:
                return fund_label
        return {"kind": "equity", "label": _CLASSIFICATION_LABELS[classification]}

    if classification == "financial":
        # Financial-SIC with no annual forms at all — probe yfinance before
        # giving up (catches commodity trusts with no 10-K, same as
        # screen.py's fallback).
        return _fund_via_yfinance_label(ticker) or {"kind": None, "label": "pending"}

    return {"kind": None, "label": "pending"}  # "unclassified"


@app.get("/api/classify/{ticker}")
def classify_ticker(ticker: str):
    tk = ticker.strip().upper()
    if not tk:
        return {"kind": None, "label": "pending"}
    return _resolve_classification(tk)


@app.get("/api/watchlist")
def get_watchlist():
    return watchlist.load()


@app.post("/api/watchlist/add")
def add_to_watchlist(body: AddTickerRequest):
    ticker = _validate_ticker(body.ticker)
    resolved = _resolve_classification(ticker)
    # The watchlist schema only has two buckets; a genuinely unresolved
    # ticker still needs somewhere to live so the next screen run considers
    # it — screen.py re-derives its own authoritative classification from
    # scratch regardless of which bucket it started in (run_screen just
    # concatenates tickers + etfs into one list). Equities is the practical
    # default bucket; it is not presented to the user as a resolved answer.
    type_ = "etf" if resolved["kind"] == "etf" else "equity"
    result = watchlist.add(ticker, type_)
    result["resolved_kind"] = resolved["kind"]
    result["resolved_label"] = resolved["label"]
    return result


@app.delete("/api/watchlist/{ticker}")
def delete_from_watchlist(ticker: str):
    return watchlist.remove(ticker.strip().upper())


# ---------------------------------------------------------------------------
# Search — instant EDGAR ticker-map preflight, no data fetch
# ---------------------------------------------------------------------------

def _lookup_ticker_name(ticker: str) -> Optional[str]:
    if app.state.ticker_names is None:
        resp = app.state.client.session.get(SEC_TICKERS_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        app.state.ticker_names = {
            row["ticker"].upper(): row.get("title") for row in data.values()
        }
    return app.state.ticker_names.get(ticker)


@app.get("/api/search/{ticker}")
def search_ticker(ticker: str):
    tk = ticker.strip().upper()
    if not tk:
        return {"found": False, "name": None}
    try:
        app.state.client.ticker_to_cik(tk)
    except ValueError:
        return {"found": False, "name": None}
    except Exception:
        # Network hiccup fetching the ticker map itself — degrade to
        # "not found" rather than raising; this endpoint promises instant,
        # best-effort feedback, not a hard error surface.
        return {"found": False, "name": None}
    try:
        name = _lookup_ticker_name(tk)
    except Exception:
        name = None
    return {"found": True, "name": name}


# ---------------------------------------------------------------------------
# Analyze — single ticker, full HTML report
# ---------------------------------------------------------------------------

def _error_page(ticker: str, exc: Exception) -> str:
    """Same design tokens as the dashboard — never a raw traceback in the browser."""
    reason = html.escape(str(exc) or type(exc).__name__)
    tk = html.escape(ticker)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Analysis failed — {tk}</title>
<style>
:root{{
  --bg:#fbfbfd;--surface:#ffffff;--hairline:rgba(0,0,0,0.07);
  --text-1:#1d1d1f;--text-2:#6e6e73;--text-3:#86868b;--accent:#0066cc;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{
  background:var(--bg);color:var(--text-1);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Roboto,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;
}}
.card{{
  background:var(--surface);border:1px solid var(--hairline);border-radius:12px;
  box-shadow:0 1px 3px rgba(0,0,0,0.04);padding:40px;max-width:480px;text-align:center;
}}
h1{{font-size:1.25rem;font-weight:600;letter-spacing:-0.01em;margin-bottom:12px}}
p{{color:var(--text-2);font-size:0.875rem;line-height:1.6;margin-bottom:20px}}
.reason{{
  font-family:"SF Mono",ui-monospace,Menlo,monospace;font-size:0.75rem;color:var(--text-3);
  background:var(--bg);border-radius:8px;padding:10px 14px;margin-bottom:20px;
  word-break:break-word;text-align:left;
}}
a{{color:var(--accent);text-decoration:none;font-size:0.8125rem;font-weight:600}}
a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<div class="card">
  <h1>Couldn't analyze {tk}</h1>
  <p>The engine could not complete this analysis.</p>
  <div class="reason">{reason}</div>
  <a href="/">&larr; Back to dashboard</a>
</div>
</body>
</html>"""


def _config_hash(cfg: dict) -> str:
    """
    Fingerprint of the FULL config.yaml (valuation/DCF assumptions included) —
    distinct from durability.py's _config_hash, which hashes only the
    durability weights/thresholds/universe_version slice. This single-ticker
    endpoint runs no durability scoring, so it reports which assumption set
    produced its valuation numbers using the same canonical-JSON + sha256 +
    16-hex-char convention (config-hash discipline, CLAUDE.md).
    """
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


async def _get_analysis_result(ticker: str) -> AnalysisResult:
    """
    Shared cache-or-compute path for every analyze-family endpoint (full
    page, /json, /fragment) — guarantees at most one concurrent EDGAR/
    yfinance fetch per ticker, and that all three renderings come from the
    exact same underlying AnalysisResult.
    """
    ttl = app.state.cfg.get("web", {}).get("analysis_cache_ttl_seconds", 3600)

    cached = app.state.analysis_cache.get(ticker)
    if cached is not None and (time.time() - cached[0]) < ttl:
        return cached[1]

    lock = app.state.analysis_locks.setdefault(ticker, asyncio.Lock())
    async with lock:
        # Re-check after acquiring the lock: another request may have
        # populated the cache while we were waiting, in which case we
        # reuse it instead of hitting EDGAR a second time.
        cached = app.state.analysis_cache.get(ticker)
        if cached is not None and (time.time() - cached[0]) < ttl:
            return cached[1]

        # run_single_ticker is a blocking, real-network call — run it in
        # the default thread executor so it doesn't block the event loop
        # for requests about OTHER tickers while this one is in flight.
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None, run_single_ticker, ticker, app.state.cfg, app.state.client
        )
        app.state.analysis_cache[ticker] = (time.time(), res)
        return res


@app.get("/api/analyze/{ticker}", response_class=HTMLResponse)
async def analyze(ticker: str):
    tk = ticker.strip().upper()
    # Synchronous-feeling response rather than the background-task + polling
    # pattern used for /api/screen: the frontend navigates the browser
    # straight to this URL (new tab), so the simplest correct behavior is a
    # normal request/response the tab waits on — polling would fight "just
    # navigate to the URL," not simplify it.
    try:
        res = await _get_analysis_result(tk)
        rendered = RH.render(res, peer_table=None)
    except Exception as e:
        return HTMLResponse(_error_page(tk, e), status_code=502)
    return HTMLResponse(rendered)


@app.get("/api/analyze/{ticker}/json")
async def analyze_json(ticker: str):
    """
    Canonical machine-readable serialization of AnalysisResult — the single
    source of truth Tier 2/3 (and this app's own fragment renderer) consume.
    Every other rendering of a ticker's analysis must be derivable from this
    payload alone.
    """
    tk = ticker.strip().upper()
    try:
        res = await _get_analysis_result(tk)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e) or type(e).__name__)

    # dataclasses.asdict() recursively converts AnalysisResult and every
    # dataclass nested inside it (CompanyData, Quote, Fact, YearlyDerived,
    # Metric, DCFResult, ImpliedGrowthResult, RelativeValuation) into plain
    # dicts/lists — None stays None throughout; FastAPI's JSON encoding then
    # turns that into `null`, never 0 or "". No custom encoder needed.
    payload = asdict(res)
    payload["config_hash"] = _config_hash(app.state.cfg)
    return payload


def _fragment_error(ticker: str, exc: Exception) -> str:
    """Small inline error state for the accordion row — not a full-viewport
    page like _error_page(), since this is embedded under a table row, not
    navigated to directly."""
    reason = html.escape(str(exc) or type(exc).__name__)
    tk = html.escape(ticker)
    return (
        '<div class="report-fragment report-error">'
        f"<p>Couldn't analyze {tk}.</p>"
        f'<p class="report-caption">{reason}</p>'
        "</div>"
    )


@app.get("/api/analyze/{ticker}/fragment", response_class=HTMLResponse)
async def analyze_fragment(ticker: str):
    """
    Light HTML fragment (no <html>/<head>) for inline embedding in the
    dashboard's accordion — same underlying AnalysisResult as the full page
    and /json (shared cache), rendered by report_html.render_fragment().
    """
    tk = ticker.strip().upper()
    try:
        res = await _get_analysis_result(tk)
        # Durability scoring is pure/local (no network) — cheap enough to
        # run fresh per request rather than adding a second cache. Analyst
        # overrides apply here too, same as screen.py, so e.g. MARA shows a
        # real composite instead of "n/a".
        overrides = app.state.cfg.get("classification", {}).get("overrides", {})
        ds = D.score(res, app.state.cfg, override_classification=overrides.get(tk))
        composite = ds.composite if not ds.excluded else None
        rendered = RH.render_fragment(res, peer_table=None, durability_composite=composite)
    except Exception as e:
        return HTMLResponse(_fragment_error(tk, e), status_code=502)
    return HTMLResponse(rendered)


# ---------------------------------------------------------------------------
# Screen — batch watchlist run, background job + polling
# ---------------------------------------------------------------------------

def _serialize_screen(rows: list, etf_rows: list) -> dict:
    """
    dataclasses.asdict() preserves None as None (never coerces to 0), and
    FastAPI's JSON encoding turns Python None into JSON null — so no
    custom encoder is needed as long as we don't touch these values other
    than passing them through asdict().
    """
    equities = [asdict(r) for r in rows if r.composite is not None]
    excluded = [asdict(r) for r in rows if r.composite is None]
    etfs = [asdict(r) for r in etf_rows]
    config_hash = next((r.config_hash for r in rows if r.config_hash), None)
    universe = next((r.universe_version for r in rows if r.universe_version), None)
    return {
        "equities": equities,
        "etfs": etfs,
        "excluded": excluded,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_hash": config_hash,
        "universe": universe,
    }


def _run_screen_job(job_id: str, tickers: list[str]) -> None:
    try:
        rows, etf_rows = run_screen(tickers, app.state.cfg, out_dir=None, verbose=False)
        app.state.screen_jobs[job_id] = {
            "status": "done",
            "result": _serialize_screen(rows, etf_rows),
        }
    except Exception as e:
        app.state.screen_jobs[job_id] = {
            "status": "error",
            "result": None,
            "error": str(e) or type(e).__name__,
        }


@app.get("/api/screen")
def start_screen(background_tasks: BackgroundTasks):
    wl = watchlist.load()
    tickers = wl["tickers"] + wl["etfs"]
    job_id = uuid.uuid4().hex
    app.state.screen_jobs[job_id] = {"status": "running", "result": None}
    background_tasks.add_task(_run_screen_job, job_id, tickers)
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "running"})


@app.get("/api/screen/status/{job_id}")
def screen_status(job_id: str):
    job = app.state.screen_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id.")
    return job


# ---------------------------------------------------------------------------
# Static frontend — must be mounted LAST so it never shadows /api/* routes
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
