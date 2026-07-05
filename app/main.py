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
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import usage, watchlist
from engine.analysis import run_single_ticker
from engine import council as COUNCIL
from engine import durability as D
from engine.edgar import EdgarClient, SEC_TICKERS_URL
from engine.etf import fetch_etf_profile, FUND_QUOTE_TYPES
from engine import flags as FLAGS
from engine.filings import FilingsClient
from engine.market import get_quote
from engine.pipeline import AnalysisResult
from engine import report_html as RH
from engine.screen import _classify as _engine_classify
from engine.screen import run_screen

log = logging.getLogger(__name__)

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

    # Fail fast: a malformed flags.overrides in config.yaml surfaces at
    # startup, not on the first /api/flags/{ticker} request.
    FLAGS.validate_flags_config(cfg)
    COUNCIL.validate_council_config(cfg)

    # Filing text fetch/parse/cache (engine/filings.py) — reuses the same
    # EdgarClient session/rate-limit/User-Agent, own on-disk cache keyed by
    # accession number (immutable; no TTL, unlike EdgarClient's XBRL cache).
    app.state.filings_client = FilingsClient(
        app.state.client, cache_dir=str(REPO_ROOT / ".cache" / "filings")
    )

    # Tier 2's only LLM client. Constructed once for the process lifetime,
    # same pattern as EdgarClient above. anthropic.Anthropic() does NOT
    # raise just because ANTHROPIC_API_KEY is unset — it happily constructs
    # a client with api_key=None and would only fail later, confusingly, on
    # the first real .messages.create() call. Checked explicitly here so a
    # dev/test environment without a key still boots the app AND
    # /api/flags/{ticker} reports a clean 503 up front instead of a raw SDK
    # auth error surfacing mid-request.
    try:
        client = anthropic.Anthropic()
        app.state.anthropic_client = client if client.api_key else None
    except Exception:
        app.state.anthropic_client = None

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


async def _render_etf_fragment_for(ticker: str, evidence: str) -> str:
    """
    ETF/fund branch of the fragment router (Task 2). Fetches the profile +
    quote in parallel via the thread executor (both are blocking yfinance
    calls) — no TTL cache here, matching screen.py's own ETF path, which
    also re-fetches yfinance fresh every run.
    """
    loop = asyncio.get_running_loop()
    profile, quote = await asyncio.gather(
        loop.run_in_executor(None, fetch_etf_profile, ticker),
        loop.run_in_executor(None, get_quote, ticker),
    )
    # Overlap detail: which of the CURRENT watchlist's equities this fund
    # holds, and at what weight — the detail behind the screen table's
    # Overlap column. Recomputed against the live watchlist rather than a
    # past screen run's snapshot, since this fragment can be opened without
    # ever having run a screen.
    watchlist_tickers = {t.upper() for t in watchlist.load()["tickers"]}
    overlap_matches = [
        (tk, w) for tk, w in profile.top_holdings if tk.upper() in watchlist_tickers
    ]
    return RH.render_etf_fragment(profile, quote.price, evidence, overlap_matches)


@app.get("/api/analyze/{ticker}/fragment", response_class=HTMLResponse)
async def analyze_fragment(ticker: str):
    """
    Light HTML fragment (no <html>/<head>) for inline embedding in the
    dashboard's accordion. Routed by evidence-based classification: a fund
    gets the ETF-specific sections (Profile, Overlap detail) built from
    engine.etf's market-vendor profile — rendering the equity sections for
    a fund would be wall-to-wall n/a (funds don't file 10-Ks), which is
    correct per absence-is-not-zero but the wrong section set for the
    security type. Equities (and anything classification couldn't resolve)
    get the existing AnalysisResult-based fragment, sharing the same cache
    as the full page and /json.
    """
    tk = ticker.strip().upper()
    try:
        resolved = _resolve_classification(tk)
        if resolved["kind"] == "etf":
            rendered = await _render_etf_fragment_for(tk, resolved["label"])
        else:
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
# Flags — Tier 2 qualitative red/green flag extraction (the LLM boundary)
# ---------------------------------------------------------------------------

_FLAGS_CACHE_DIR = REPO_ROOT / ".cache" / "flags"


def _describe_exception(e: Exception) -> dict:
    """
    Best-effort structured description of any exception a flags
    extraction call can raise. The Anthropic SDK's own exception classes
    (anthropic.APIStatusError and subclasses) expose `.status_code` and
    `.body` for a real API-level failure; a plain network/programming
    error won't have those, so every field is read defensively via
    getattr and left None (never fabricated) when absent — this is what
    lets the client and the server log see the ACTUAL exception (type,
    message, and the API's own response body when there is one) instead
    of a bare "502 Bad Gateway" with no diagnostic content.
    """
    status_code = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    if body is None:
        response = getattr(e, "response", None)
        if response is not None:
            try:
                body = response.json()
            except Exception:
                body = getattr(response, "text", None)
    return {
        "error_type": type(e).__name__,
        "message": str(e) or type(e).__name__,
        "sdk_status_code": status_code,
        "sdk_body": body,
    }


@app.get("/api/flags/{ticker}")
async def get_flags(ticker: str, extract: bool = False, refresh: bool = False):
    """
    Spend-gated Tier 2 boundary. Fetches the latest 10-K's sections (a
    free EDGAR fetch — no model involved) to learn the filing's accession
    number, then decides what to do based on cache state and the caller's
    explicit signal:

      - already cached, no ?refresh=true  -> serve the cached (verbatim-
        validated, override-applied) flags. No model call, no spend —
        this is what a plain accordion-expand hits every time after the
        first extraction, and it's why this path needs no `extract` flag.
      - NOT cached, no ?extract=true, no ?refresh=true -> the "click to
        extract" placeholder envelope ({"state": "not_cached", ...}), a
        ticker-independent cost estimate, and NO model call. This is the
        only response a bare accordion-expand can ever get for a
        never-extracted ticker — auto-fetch of the paid call is
        impossible by construction, not just by frontend discipline.
      - ?extract=true (cache miss) or ?refresh=true (any cache state) ->
        an actual model call, gated on a configured Anthropic client.

    This endpoint is equity-only in practice: a fund's filing history has
    no 10-K, so engine.filings.FilingsClient.latest_10k_sections() returns
    None for it and this reports 404 — the same "no filing found" 404 an
    equity with no 10-K yet would get, no special ETF-detection branch
    needed.
    """
    tk = ticker.strip().upper()
    loop = asyncio.get_running_loop()
    try:
        filing_sections = await loop.run_in_executor(
            None, app.state.filings_client.latest_10k_sections, tk
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e) or type(e).__name__)

    if filing_sections is None:
        raise HTTPException(status_code=404, detail=f"No 10-K filing found for {tk}.")

    flags_cfg = app.state.cfg.get("flags", {})
    model = flags_cfg.get("model", "claude-sonnet-5")
    prompt_version = flags_cfg.get("prompt_version", "v1")
    already_cached = FLAGS.is_cached(_FLAGS_CACHE_DIR, filing_sections.accession, prompt_version, model)

    # Cache miss with no explicit trigger: report the placeholder state
    # and estimated cost, no model call — the ONLY response shape a bare
    # GET can produce for a ticker that's never been extracted.
    if not already_cached and not extract and not refresh:
        if app.state.anthropic_client is None:
            raise HTTPException(
                status_code=503,
                detail="Anthropic API key not configured (set ANTHROPIC_API_KEY).",
            )
        return {
            "state": "not_cached",
            "model": model,
            "estimated_cost_usd": FLAGS.estimate_extraction_cost_usd(app.state.cfg),
        }

    # From here, either the request will read from cache (no client
    # needed) or is about to make a real, billed model call (client
    # required) — figure out which before touching the API key check.
    live_call_needed = refresh or not already_cached
    if live_call_needed and app.state.anthropic_client is None:
        raise HTTPException(
            status_code=503,
            detail="Anthropic API key not configured (set ANTHROPIC_API_KEY).",
        )

    filing_ref = FLAGS.FilingRef(
        form=filing_sections.form,
        accession=filing_sections.accession,
        period_ending=filing_sections.period_ending,
        filed=filing_sections.filed,
        url=filing_sections.url,
    )
    call_info: dict = {}
    try:
        result = await loop.run_in_executor(
            None,
            FLAGS.get_flags,
            tk,
            filing_sections.sections,
            filing_ref,
            app.state.cfg,
            app.state.anthropic_client,
            _FLAGS_CACHE_DIR,
            refresh,
            call_info,
        )
    except Exception as e:
        # get_flags() only reaches its cache WRITE after extract_flags_raw()
        # returns successfully (engine/flags.py) — an exception here means
        # that never happened, so the cache is guaranteed untouched by this
        # failure: the next plain request naturally retries instead of
        # replaying a stored error. Status stays 502 (our server's proxy to
        # the upstream Anthropic API failed) — the fix is a richer BODY,
        # not a different status; surfacing the SDK's own status as our
        # HTTP status would wrongly imply the client's request was bad.
        # Full type/message/SDK body logged server-side AND returned to
        # the client as structured JSON, never a content-less "502".
        detail = _describe_exception(e)
        log.error("! %s: flag extraction failed — %s", tk, detail)
        raise HTTPException(status_code=502, detail={"state": "error", **detail})

    cache_status = call_info.get("cache_status", "from_cache")
    if cache_status == "from_cache":
        cost_usd = 0.0
    else:
        # None (not 0.0) when the pinned model has no config.yaml
        # flags.pricing entry — a real, billed call with unknown cost is
        # not the same thing as a free cache hit, and must never be
        # recorded as one. See engine.flags.compute_cost_usd.
        cost_usd = FLAGS.compute_cost_usd(
            app.state.cfg, model, call_info.get("input_tokens"), call_info.get("output_tokens"),
        )
    pricing_unknown = cache_status != "from_cache" and cost_usd is None

    usage.log_call(
        ticker=tk,
        model=model,
        prompt_version=prompt_version,
        input_tokens=call_info.get("input_tokens") or 0,
        output_tokens=call_info.get("output_tokens") or 0,
        cost_usd=cost_usd,
        cache_status=cache_status,
    )

    payload = asdict(result)
    payload["state"] = "ok"
    payload["cache_status"] = cache_status
    if pricing_unknown:
        payload["pricing_unknown"] = True
    return payload


# ---------------------------------------------------------------------------
# Usage — Tier 2 spend visibility (app/usage.py's SQLite ledger)
# ---------------------------------------------------------------------------

@app.get("/api/usage")
def get_usage():
    return usage.summary()


# ---------------------------------------------------------------------------
# Council — Tier 3 adversarial synthesis (engine/council.py). Spend-gated
# exactly like /api/flags/{ticker} above: GET never spends, POST
# ?convene=true is the only path that does. A council run reuses
# flags.model/flags.pricing directly and NEVER triggers a flag extraction
# of its own — if flags aren't cached yet for this ticker's latest 10-K,
# both endpoints report that plainly rather than spending on the user's
# behalf.
# ---------------------------------------------------------------------------

_COUNCIL_CACHE_DIR = REPO_ROOT / ".cache" / "council"

# No thesis-journal store exists in this codebase yet (grep confirms it —
# every bundle today is pre-thesis). This is the single place that fact is
# encoded, so wiring up a real thesis store later only touches this
# constant/helper, not either endpoint below.
_THESIS_TAG = "prethesis"


async def _council_preflight(tk: str):
    """Free (non-LLM) preflight shared by both endpoints: the latest 10-K's
    accession number plus whether Tier 2 flags are already cached for it.
    Never triggers a flag extraction."""
    loop = asyncio.get_running_loop()
    filing_sections = await loop.run_in_executor(
        None, app.state.filings_client.latest_10k_sections, tk
    )
    if filing_sections is None:
        raise HTTPException(status_code=404, detail=f"No 10-K filing found for {tk}.")

    flags_cfg = app.state.cfg.get("flags", {})
    flags_model = flags_cfg.get("model", "claude-sonnet-5")
    flags_prompt_version = flags_cfg.get("prompt_version", "v1")
    flags_cached = FLAGS.is_cached(_FLAGS_CACHE_DIR, filing_sections.accession, flags_prompt_version, flags_model)
    return filing_sections, flags_cached, flags_model, flags_prompt_version


async def _assemble_council_bundle(tk: str, filing_sections, flags_model: str, flags_prompt_version: str) -> COUNCIL.EvidenceBundle:
    """Only ever called once the preflight has confirmed flags are cached —
    get_flags() below will find the raw extraction on disk and never reach
    its model-call branch, so passing the (possibly None) anthropic client
    here can never trigger a paid call."""
    filing_ref = FLAGS.FilingRef(
        form=filing_sections.form, accession=filing_sections.accession,
        period_ending=filing_sections.period_ending, filed=filing_sections.filed,
        url=filing_sections.url,
    )
    loop = asyncio.get_running_loop()
    flags_result = await loop.run_in_executor(
        None, FLAGS.get_flags, tk, filing_sections.sections, filing_ref,
        app.state.cfg, app.state.anthropic_client, _FLAGS_CACHE_DIR, False, None,
    )
    quant_res = await _get_analysis_result(tk)
    quant_payload = asdict(quant_res)
    config_hash = _config_hash(app.state.cfg)
    quant_payload["config_hash"] = config_hash
    # Thesis journal not yet implemented as a store (see _THESIS_TAG) —
    # every bundle assembled here is pre-thesis until one exists.
    return COUNCIL.assemble_bundle(
        ticker=tk, quant=quant_payload, flags_result=flags_result, thesis=None,
        accession=filing_sections.accession, config_hash=config_hash,
    )


def _log_council_usage(tk: str, call_info: list, model: str, prompt_version: str, cache_status: str) -> None:
    """One usage row per real call in call_info — a run that fails partway
    through still logs whatever calls actually completed (and were
    billed) before the failure; nothing real ever goes unlogged."""
    for rec in call_info:
        usage.log_call(
            ticker=tk, model=model, prompt_version=prompt_version,
            input_tokens=rec.get("input_tokens") or 0, output_tokens=rec.get("output_tokens") or 0,
            cost_usd=COUNCIL.compute_council_cost_usd(app.state.cfg, model, rec.get("input_tokens"), rec.get("output_tokens")),
            cache_status=cache_status, call_type=rec["call_type"],
        )


@app.get("/api/council/{ticker}")
async def get_council(ticker: str):
    """Never spends. Reports one of three states: blocked_no_flags (Tier 2
    flags must be extracted first — this endpoint will not do it for you),
    not_cached (a cost/call estimate, no model call), or the full cached
    council record from a prior convene."""
    tk = ticker.strip().upper()
    filing_sections, flags_cached, flags_model, flags_prompt_version = await _council_preflight(tk)

    if not flags_cached:
        # _get_analysis_result is Tier 1's deterministic EDGAR/yfinance path
        # (cached per-ticker already, since the fragment this checklist
        # renders inside couldn't exist without a successful analysis) — safe
        # to call here even though flags aren't cached, unlike
        # _assemble_council_bundle below, which must never run before
        # flags_cached is confirmed True (it would trigger a real extraction
        # via FLAGS.get_flags on a cache miss).
        quant_status = "ok"
        try:
            await _get_analysis_result(tk)
        except Exception:
            quant_status = "error"
        return {
            "state": "blocked_no_flags",
            "message": f"Flags not extracted for {tk} yet. Extract them first: GET /api/flags/{tk}?extract=true.",
            "evidence_status": {"quant": quant_status, "flags": "not_extracted", "thesis": "pre_thesis"},
        }

    council_prompt_version = app.state.cfg.get("council", {}).get("prompt_version", "v1")
    cached = COUNCIL.is_cached(_COUNCIL_CACHE_DIR, filing_sections.accession, _THESIS_TAG, council_prompt_version, flags_model)
    if cached:
        result = COUNCIL.load_cached(_COUNCIL_CACHE_DIR, filing_sections.accession, _THESIS_TAG, council_prompt_version, flags_model)
        payload = asdict(result)
        payload["state"] = "ok"
        payload["cache_status"] = "cached"
        return payload

    # Assembling the bundle here (not just checking quant status) is what lets
    # the cost estimate below be sized to THIS ticker's real evidence volume
    # rather than a ticker-independent guess — a fixed baseline was tried
    # first and was wrong-low by more than 2x on a real convene (see
    # engine.council.estimate_council_cost_usd's docstring). This is still a
    # free operation: get_flags() only reads the on-disk cache (flags_cached
    # was already confirmed True above) and _get_analysis_result() is Tier
    # 1's deterministic EDGAR/yfinance path — neither ever calls the
    # Anthropic API, so GET still never spends.
    bundle_text = None
    try:
        bundle = await _assemble_council_bundle(tk, filing_sections, flags_model, flags_prompt_version)
        quant_status = "ok"
        bundle_text = COUNCIL.render_bundle_text(bundle)
    except Exception:
        quant_status = "error"

    return {
        "state": "not_cached",
        "model": flags_model,
        "calls": 7,
        "estimated_cost_usd": COUNCIL.estimate_council_cost_usd(app.state.cfg, bundle_text),
        "evidence_status": {"quant": quant_status, "flags": "cached", "thesis": "pre_thesis"},
    }


@app.post("/api/council/{ticker}")
async def convene_council(ticker: str, convene: bool = False, refresh: bool = False):
    """The ONLY path that spends. Requires ?convene=true explicitly — a
    bare POST is rejected, same discipline as /api/flags/{ticker}'s bare
    GET never triggering a paid call. ?refresh=true re-convenes past an
    existing cache entry."""
    tk = ticker.strip().upper()
    if not convene:
        raise HTTPException(
            status_code=400,
            detail="POST /api/council/{ticker} requires ?convene=true — that is the only path that spends.",
        )

    filing_sections, flags_cached, flags_model, flags_prompt_version = await _council_preflight(tk)
    if not flags_cached:
        raise HTTPException(
            status_code=409,
            detail=f"Flags not extracted for {tk} yet. Extract them first: GET /api/flags/{tk}?extract=true.",
        )
    if app.state.anthropic_client is None:
        raise HTTPException(
            status_code=503,
            detail="Anthropic API key not configured (set ANTHROPIC_API_KEY).",
        )

    bundle = await _assemble_council_bundle(tk, filing_sections, flags_model, flags_prompt_version)
    council_prompt_version = app.state.cfg.get("council", {}).get("prompt_version", "v1")

    loop = asyncio.get_running_loop()
    call_info: list = []
    try:
        result = await loop.run_in_executor(
            None, COUNCIL.get_council, tk, bundle, app.state.cfg, app.state.anthropic_client,
            _COUNCIL_CACHE_DIR, refresh, call_info,
        )
    except Exception as e:
        _log_council_usage(tk, call_info, flags_model, council_prompt_version, "refresh" if refresh else "live")
        raise HTTPException(status_code=502, detail=str(e) or type(e).__name__)

    if call_info:
        _log_council_usage(tk, call_info, flags_model, council_prompt_version, "refresh" if refresh else "live")

    payload = asdict(result)
    payload["state"] = "ok"
    payload["cache_status"] = "live" if call_info else "from_cache"
    return payload


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
