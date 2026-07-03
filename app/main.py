"""
main.py — FastAPI application wrapping the existing engine.

Zero changes to engine/*.py. This module only composes existing, already-
tested entry points:
  - engine.analysis.run_single_ticker  (Branch 1's single-ticker wrapper)
  - engine.screen.run_screen           (existing batch screener)
  - engine.report_html.render          (existing per-company HTML renderer)

Run with:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import html
import re
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
from engine.edgar import EdgarClient, SEC_TICKERS_URL
from engine import report_html as RH
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

    # In-memory job store for /api/screen background runs, and a
    # same-calendar-day cache for /api/analyze. Both intentionally
    # process-lifetime only (plain dicts, no persistence) — cleared on
    # restart, which is fine for single-user local use.
    app.state.screen_jobs: dict[str, dict] = {}
    app.state.analyze_cache: dict[tuple[str, str], str] = {}

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
    type: str = "equity"  # "equity" | "etf"


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


@app.get("/api/watchlist")
def get_watchlist():
    return watchlist.load()


@app.post("/api/watchlist/add")
def add_to_watchlist(body: AddTickerRequest):
    ticker = _validate_ticker(body.ticker)
    type_ = "etf" if body.type == "etf" else "equity"
    return watchlist.add(ticker, type_)


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


@app.get("/api/analyze/{ticker}", response_class=HTMLResponse)
def analyze(ticker: str):
    tk = ticker.strip().upper()
    cache_key = (tk, datetime.now().strftime("%Y-%m-%d"))
    if cache_key in app.state.analyze_cache:
        return HTMLResponse(app.state.analyze_cache[cache_key])

    # Synchronous, blocking response rather than the background-task +
    # polling pattern used for /api/screen: the frontend navigates the
    # browser straight to this URL (new tab), so the simplest correct
    # behavior is a normal request/response the tab waits on — polling
    # would fight "just navigate to the URL," not simplify it.
    try:
        res = run_single_ticker(tk, app.state.cfg, app.state.client)
        rendered = RH.render(res, peer_table=None)
    except Exception as e:
        return HTMLResponse(_error_page(tk, e), status_code=502)

    app.state.analyze_cache[cache_key] = rendered
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
