"""
screen.py — batch durability screener.

Routes each ticker:
  - Operating equities (10-K domestic or 20-F/40-F FPI) → durability scorecard + growth signals
  - Funds (N-CSR/N-PORT/N-1A/485BPOS/485APOS forms, SIC 6726, or yfinance quoteType
    ETF/MUTUALFUND) → ETF lens section
  - Unknown/unclassified → flagged row, not scored
  - Network / data failures → logged, run continues; one bad ticker never kills the run

Classification is evidence-based (B1): determined from the SEC submissions form history,
not inferred from absent fundamentals.  A company whose XBRL concepts all fail to resolve
is still classified operating if it filed 10-K or 20-F.

For tickers absent from EDGAR (ETFs, most CEFs), yfinance quoteType is used as a *second*
evidence source:
  - quoteType ETF or MUTUALFUND → route to ETF lens
  - quoteType EQUITY or yfinance failure → route to Excluded ("not classifiable as fund")
  Never classify as fund from EDGAR absence alone.

Analyst overrides (B2) in config.classification.overrides win over inference
and over the financial-issuer SIC exclusion.

Sort modes
----------
--sort durability     : ranked by durability composite (highest first)
--sort quality-value  : ranked by (composite_percentile − gap_percentile) within the batch.
                        High durability AND low/negative expectations gap → top rank.
                        Companies without a solvable expectations gap are ranked last.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional

from engine.edgar import CompanyData, EdgarClient
from engine.market import get_quote
from engine.pipeline import AnalysisResult, derive
from engine import durability as D
from engine.etf import EtfProfile, fetch_etf_profile, FUND_QUOTE_TYPES

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Implied-growth "n/a" reason text — single source of truth. Both consumers
# (ScreenRow.implied_growth_note, folded into ScreenRow.flag for the
# diagnostics/excluded Notes column) read the same string built once here,
# so the dashboard's Data Diagnostics section and this module's own HTML
# report can never diverge into two separate copies of the same reason.
# ---------------------------------------------------------------------------

_IG_NOTE_BRACKET_UPPER_HIT = (
    "Reverse-DCF: market implies >60% annual growth, above the model's "
    "+60% solver ceiling — expectations gap not computable (price is "
    "off-scale rich vs. current FCF)."
)
_IG_NOTE_FCF_NON_POSITIVE = (
    "Reverse-DCF: normalized free cash flow is zero or negative, so "
    "implied growth cannot be computed (no positive FCF base to grow "
    "from)."
)


def _implied_growth_columns(res: AnalysisResult, cd: CompanyData) -> tuple:
    """
    Pure: derives (implied_g, gap, ig_note) from an already-computed
    AnalysisResult + CompanyData, no I/O and no scoring dependency —
    extracted out of _score_ticker() so the n/a-reason text can be unit
    tested directly without standing up a full mocked EDGAR/quote/
    durability pipeline.
    """
    igr = res.implied_growth_result
    if igr is None:
        implied_g = None
        if cd.reporting_currency != "USD":
            ig_note = (
                f"n/a — valuation gated: reporting currency {cd.reporting_currency} "
                "vs USD market data"
            )
        else:
            ig_note = f"n/a — {_IG_NOTE_FCF_NON_POSITIVE}"
    elif igr.bracket_hit and igr.bracket_bound == "upper":
        implied_g = None
        ig_note = f"n/a — {_IG_NOTE_BRACKET_UPPER_HIT}"
    elif igr.bracket_hit:
        implied_g = None
        bound = igr.bracket_bound or "?"
        ig_note = (
            f"n/a — bracket {bound} hit "
            f"(implied g {'<' if bound == 'lower' else '>'} "
            f"{igr.implied_growth:.0%})"
        )
    else:
        implied_g = igr.implied_growth
        ig_note = ""

    gap = res.expectations_gap if (igr is not None and not igr.bracket_hit) else None
    return implied_g, gap, ig_note


# ---------------------------------------------------------------------------
# Routing outcome per ticker (operating securities)
# ---------------------------------------------------------------------------

@dataclass
class ScreenRow:
    ticker: str
    composite: Optional[float]
    composite_low: Optional[float]
    composite_high: Optional[float]
    cat_reinvestment: Optional[float]
    cat_quality: Optional[float]
    cat_resilience: Optional[float]
    cat_discipline: Optional[float]
    cat_optionality: Optional[float]
    completeness: Optional[float]
    is_stable: Optional[bool]
    stability_delta: Optional[float]
    config_hash: Optional[str]
    universe_version: str
    # Growth signals (B-series)
    implied_fcf_growth: Optional[float]      # from reverse DCF
    delivered_fcf_growth: Optional[float]    # historical FCF or revenue CAGR
    expectations_gap: Optional[float]        # implied − delivered (positive = priced for more)
    implied_growth_note: str                 # empty when valid; reason when n/a
    # Batch-relative sort score (computed after all tickers are processed)
    quality_value_score: Optional[float]     # composite_pct − gap_pct; None if gap unavailable
    flag: str                                # "" | classification evidence | "error:<msg>"
    excluded: bool = False
    # Basis-disclosure signals (Session B): computed upstream, previously
    # dropped before reaching the UI. delivered_growth_label distinguishes
    # the clean FCF-CAGR path from the revenue-CAGR fallback (a mixed-base
    # comparison against implied_fcf_growth) and, since Session B's cagr_over
    # extension, may also carry a stale-window note (e.g. NVDA's permanent
    # capex absence forcing a 14y-vs-5y-requested window). quote_source and
    # diluted_shares_gap together identify the "Visa condition": a
    # market-vendor-tier (yfinance) share count feeding a headline number
    # because EDGAR's diluted_shares extraction failed — not every
    # yfinance-sourced quote, only the ones where trust tier actually
    # changes a score-derived number's interpretation.
    delivered_growth_label: str = ""
    quote_source: str = ""
    diluted_shares_gap: bool = False


# ---------------------------------------------------------------------------
# ETF / fund row (separate display section)
# ---------------------------------------------------------------------------

@dataclass
class EtfRow:
    ticker: str
    name: Optional[str]
    category: Optional[str]
    expense_ratio: Optional[float]                       # 0–1 decimal fraction; None ≠ 0.0
    aum: Optional[float]
    top10_concentration: Optional[float]
    # Top holdings stored for overlap computation and display (no re-fetch needed)
    top_holdings: list[tuple[str, float]] = field(default_factory=list)
    overlap_with_screen: Optional[float] = None          # weight-fraction overlap
    overlap_count: Optional[int] = None                  # # of matching holdings
    flag: str = ""


# ---------------------------------------------------------------------------
# B1: Evidence-based security classification
# ---------------------------------------------------------------------------

_FUND_FORMS = {"N-CSR", "N-PORT", "N-1A", "485BPOS", "485APOS"}
_FUND_SIC   = 6726

_ETF_KEYWORDS = ("ETF", "FUND", "TRUST", "ISHARES", "SPDR", "VANGUARD", "INVESCO")


def _is_etf(ticker: str, name: str) -> bool:
    """Legacy name-based heuristic (kept for backward-compat tests). Not used for routing."""
    combined = (ticker + " " + name).upper()
    return any(kw in combined for kw in _ETF_KEYWORDS)


def _has_fundamentals(cd_series: dict) -> bool:
    """Legacy check (kept for backward-compat tests). Not used for routing."""
    return bool(cd_series.get("revenue") or cd_series.get("total_assets"))


def _classify(ticker: str, cd: CompanyData, overrides: dict) -> tuple[str, str]:
    """
    Return (classification, evidence_string) for a ticker.

    Classifications:
      "operating_domestic" — filed 10-K
      "operating_fpi"      — filed 20-F or 40-F
      "fund"               — filed N-CSR/N-PORT/N-1A/485BPOS/485APOS, or SIC 6726
      "financial"          — SIC 6000–6799 (no annual FPI/fund forms overriding it)
      "unclassified"       — EDGAR registrant exists but no annual forms found

    Analyst overrides in config.classification.overrides win over ALL inference.
    """
    override = overrides.get(ticker.upper())
    if override:
        log.info("Classification override applied: %s → %s", ticker, override)
        return override, f"analyst override: {override}"

    forms = set(cd.recent_forms)

    # Fund indicators take priority over annual-report forms
    fund_evidence = forms & _FUND_FORMS
    sic_int = None
    try:
        sic_int = int(cd.sic)
    except (ValueError, TypeError):
        pass

    if fund_evidence or sic_int == _FUND_SIC:
        evidence_parts = sorted(fund_evidence)
        if sic_int == _FUND_SIC:
            evidence_parts.append("SIC 6726")
        evidence = "fund: " + "/".join(evidence_parts) + " observed"
        return "fund", evidence

    # FPI annual filers (20-F or 40-F)
    fpi_forms = [f for f in forms if f.startswith(("20-F", "40-F"))]
    if fpi_forms:
        ccy = cd.reporting_currency
        evidence = f"FPI: {fpi_forms[0][:4]} observed, reporting {ccy}"
        return "operating_fpi", evidence

    # Domestic annual filers
    if any(f.startswith("10-K") for f in forms):
        return "operating_domestic", "10-K observed"

    # Financial SIC (6000-6799) without annual forms
    if sic_int is not None and 6000 <= sic_int <= 6799:
        return "financial", f"financial issuer SIC {sic_int} (6000–6799)"

    return "unclassified", "no annual report forms found"


# ---------------------------------------------------------------------------
# Per-ticker processing helpers
# ---------------------------------------------------------------------------

def _empty_row(ticker: str, flag: str, excluded: bool = False,
               completeness: Optional[float] = None,
               config_hash: Optional[str] = None,
               universe_version: str = "") -> ScreenRow:
    return ScreenRow(
        ticker=ticker,
        composite=None, composite_low=None, composite_high=None,
        cat_reinvestment=None, cat_quality=None, cat_resilience=None,
        cat_discipline=None, cat_optionality=None,
        completeness=completeness, is_stable=None, stability_delta=None,
        config_hash=config_hash, universe_version=universe_version,
        implied_fcf_growth=None, delivered_fcf_growth=None,
        expectations_gap=None, implied_growth_note="",
        quality_value_score=None, flag=flag, excluded=excluded,
    )


def _etf_row_from_profile(profile: EtfProfile, evidence: str) -> EtfRow:
    """Build an EtfRow from a fully-fetched EtfProfile.  Overlap filled in later."""
    return EtfRow(
        ticker=profile.ticker,
        name=profile.name,
        category=profile.category,
        expense_ratio=profile.expense_ratio,
        aum=profile.total_assets,
        top10_concentration=profile.top10_concentration,
        top_holdings=list(profile.top_holdings),
        overlap_with_screen=None,
        overlap_count=None,
        flag=evidence,
    )


def _try_fund_via_yfinance(
    ticker: str,
    evidence_suffix: str = "no EDGAR registrant",
) -> Optional[EtfRow]:
    """
    Uses yfinance quoteType as positive evidence for fund classification.

    Called in two situations:
      1. EDGAR has no registrant for this ticker (evidence_suffix default).
      2. EDGAR has a registrant but D.score() excluded it via financial-SIC
         (caller passes a descriptive suffix).

    Returns an EtfRow (overlap not yet filled) if quoteType is ETF or MUTUALFUND.
    Returns None if quoteType is EQUITY, unknown, or yfinance itself fails.
    Never classifies as fund from EDGAR absence alone.
    """
    try:
        profile = fetch_etf_profile(ticker)
    except Exception as exc:
        log.debug("yfinance probe for %s raised: %s", ticker, exc)
        return None

    qt = (profile.quote_type or "").upper()
    if qt not in FUND_QUOTE_TYPES:
        if qt:
            log.debug("%s: yfinance quoteType=%s — not a fund, routing to Excluded", ticker, qt)
        return None

    evidence = f"fund: yfinance quoteType={profile.quote_type} ({evidence_suffix})"
    return _etf_row_from_profile(profile, evidence)


def _process_one(
    ticker: str,
    client: EdgarClient,
    cfg: dict,
    history_years: int,
    operating_tickers: Optional[set[str]] = None,
) -> tuple[Optional[ScreenRow], Optional[EtfRow]]:
    """
    Process a single ticker.  Returns (ScreenRow, None) for operating securities,
    (None, EtfRow) for funds, and (ScreenRow, None) with a flag for errors.

    EDGAR is the primary classification source.  For tickers absent from EDGAR,
    yfinance quoteType is used as a second evidence source before routing to Excluded.
    """
    universe_version = cfg.get("universe", {}).get("version", "")
    overrides = cfg.get("classification", {}).get("overrides", {})

    # ── EDGAR lookup ────────────────────────────────────────────────────────
    try:
        cd = client.get_company(ticker, history_years)
    except ValueError as e:
        if "not found in SEC ticker map" in str(e):
            # Second evidence source: yfinance quoteType
            etf_row = _try_fund_via_yfinance(ticker)
            if etf_row is not None:
                return None, etf_row
            # yfinance confirms non-fund (or also failed) → Excluded with distinct reason
            return _empty_row(
                ticker,
                "no EDGAR registrant, not classifiable as fund",
                universe_version=universe_version,
            ), None
        return _empty_row(
            ticker, f"error:{type(e).__name__}: {e}",
            universe_version=universe_version,
        ), None
    except Exception as e:
        return _empty_row(
            ticker, f"error:{type(e).__name__}: {e}",
            universe_version=universe_version,
        ), None

    # ── EDGAR-based classification ───────────────────────────────────────────
    classification, evidence = _classify(ticker, cd, overrides)

    if classification == "fund":
        profile = fetch_etf_profile(ticker)
        etf_row = _etf_row_from_profile(profile, evidence)
        return None, etf_row

    if classification == "skip":
        return _empty_row(ticker, f"skipped: {evidence}",
                          universe_version=universe_version), None

    # For "unclassified" — include in operating rows with flag, don't try to score
    if classification == "unclassified":
        return _empty_row(ticker, evidence,
                          universe_version=universe_version), None

    # "operating_domestic", "operating_fpi", or override → attempt scoring
    # Pass the override flag so durability.score() can bypass financial-SIC exclusion
    try:
        quote = get_quote(ticker)
        res = derive(cd, quote, cfg)
        ds = D.score(res, cfg, override_classification=overrides.get(ticker.upper()))
    except Exception as e:
        return _empty_row(ticker, f"error:{type(e).__name__}: {e}",
                          universe_version=universe_version), None

    if ds.excluded:
        # Probe yfinance before finalising as Excluded.  Catches commodity ETFs
        # (e.g. GLD, SLV) and other fund-structured vehicles that hold EDGAR
        # registrations with financial-SIC codes but are legitimately funds.
        etf_row = _try_fund_via_yfinance(
            ticker,
            evidence_suffix=f"financial SIC {cd.sic}, no EDGAR fund-filing forms",
        )
        if etf_row is not None:
            return None, etf_row
        return _empty_row(
            ticker, ds.exclusion_reason, excluded=True,
            completeness=ds.data_completeness, config_hash=ds.config_hash,
            universe_version=universe_version,
        ), None

    def _cat(name: str) -> Optional[float]:
        c = ds.categories.get(name)
        return c.composite if c else None

    # Build growth-signal columns
    implied_g, gap, ig_note = _implied_growth_columns(res, cd)

    # Compose diagnostics flag: combine evidence + ig_note + currency info
    flag_parts: list[str] = []
    if classification == "operating_fpi":
        flag_parts.append(evidence)
    if ig_note:
        flag_parts.append(ig_note)
    flag = " · ".join(flag_parts)

    return ScreenRow(
        ticker=ticker,
        composite=ds.composite,
        composite_low=ds.composite_low,
        composite_high=ds.composite_high,
        cat_reinvestment=_cat("reinvestment_engine"),
        cat_quality=_cat("quality_persistence"),
        cat_resilience=_cat("balance_sheet_resilience"),
        cat_discipline=_cat("capital_discipline"),
        cat_optionality=_cat("optionality_proxies"),
        completeness=ds.data_completeness,
        is_stable=ds.is_stable,
        stability_delta=ds.stability_delta,
        config_hash=ds.config_hash,
        universe_version=universe_version,
        implied_fcf_growth=implied_g,
        delivered_fcf_growth=res.delivered_growth,
        expectations_gap=gap,
        implied_growth_note=ig_note,
        quality_value_score=None,   # filled in batch step
        flag=flag,
        delivered_growth_label=res.delivered_growth_label,
        quote_source=quote.source,
        diluted_shares_gap="diluted_shares" in res.gaps,
    ), None


# ---------------------------------------------------------------------------
# Batch quality-value score
# ---------------------------------------------------------------------------

def _assign_quality_value_scores(rows: list[ScreenRow]) -> None:
    """
    quality_value_score = composite_percentile − gap_percentile  (within batch).

    High durability AND low/negative expectations gap → high score.
    Computed after all tickers are scored; companies without a solvable gap
    receive None and are ranked last in quality-value mode.
    """
    eligible = [r for r in rows if r.composite is not None and r.expectations_gap is not None]
    if not eligible:
        return

    composites = [r.composite for r in eligible]
    gaps       = [r.expectations_gap for r in eligible]
    n = len(eligible)

    for r in eligible:
        comp_pct = 100.0 * sum(1 for c in composites if c < r.composite) / n
        gap_pct  = 100.0 * sum(1 for g in gaps       if g < r.expectations_gap) / n
        r.quality_value_score = comp_pct - gap_pct


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _pct(x: Optional[float], decimals: int = 1) -> str:
    return f"{x * 100:.{decimals}f}%" if x is not None else "n/a"


def _pts(x: Optional[float]) -> str:
    return f"{x:.1f}" if x is not None else "n/a"


def _signed_pct(x: Optional[float], decimals: int = 1) -> str:
    if x is None:
        return "n/a"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x * 100:.{decimals}f}%"


def _gap_style(gap: Optional[float]) -> str:
    """
    Inline box-shadow tint for the Gap <td> — muted slate-blue proportional to |gap|.
    Uses box-shadow rather than background so it layers over zebra-banding and hover.
    """
    if gap is None:
        return ""
    magnitude = min(abs(gap), 0.30)
    alpha     = (magnitude / 0.30) * 0.22
    return f"box-shadow:inset 0 0 0 1000px rgba(94,121,180,{alpha:.3f})"


def _completeness_display(r: ScreenRow) -> str:
    """Show completeness '—' for excluded/fund/error rows; percentage for scored rows."""
    if r.excluded or r.completeness is None:
        return "—"
    return _pct(r.completeness)


def _fmt_aum(aum: Optional[float]) -> str:
    if aum is None:
        return "n/a"
    if aum >= 1e9:
        return f"${aum / 1e9:.1f}B"
    if aum >= 1e6:
        return f"${aum / 1e6:.0f}M"
    return f"${aum:.0f}"


def _fmt_top5(top_holdings: list[tuple[str, float]], n: int = 5) -> str:
    """Format top-N holdings as a compact string: 'NVDA 15.2%, TSM 9.4%, …'"""
    if not top_holdings:
        return "n/a"
    items = [f"{tk} {w * 100:.1f}%" for tk, w in top_holdings[:n]]
    return ", ".join(items)


def _fmt_overlap(er: EtfRow) -> str:
    """Format overlap as '27.0% (3 holdings)' or 'n/a'."""
    if er.overlap_with_screen is None:
        return "n/a"
    pct_str = f"{er.overlap_with_screen * 100:.1f}%"
    if er.overlap_count is not None:
        suffix = "holding" if er.overlap_count == 1 else "holdings"
        return f"{pct_str} ({er.overlap_count} {suffix})"
    return pct_str


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _render_md(
    rows: list[ScreenRow],
    etf_rows: Optional[list[EtfRow]] = None,
    sort_mode: str = "durability",
) -> str:
    ts      = datetime.now().strftime("%Y-%m-%d %H:%M")
    uni_ver = next((r.universe_version for r in rows if r.universe_version), "—")
    cfg_h   = next((r.config_hash     for r in rows if r.config_hash),     "—")
    sort_label = (
        "durability score" if sort_mode == "durability"
        else "quality-value (composite percentile − gap percentile)"
    )

    lines: list[str] = [
        "# Durability & Expectations Screen",
        "",
        f"Generated: {ts}  ·  Universe: {uni_ver}  ·  Config: `{cfg_h}`",
        f"Sorted by: {sort_label}",
        "",
        "## Equities",
        "",
        "| Ticker | Durability | Reinv | Quality | Resilience | Discipline"
        " | Optionality | Implied g | Delivered g | Gap |",
        "|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for r in rows:
        impl_g = _pct(r.implied_fcf_growth) if not r.implied_growth_note else "n/a"
        gap    = (_signed_pct(r.expectations_gap)
                  if r.expectations_gap is not None and not r.implied_growth_note
                  else "n/a")
        lines.append(
            f"| {r.ticker}"
            f" | {_pts(r.composite)}"
            f" | {_pts(r.cat_reinvestment)}"
            f" | {_pts(r.cat_quality)}"
            f" | {_pts(r.cat_resilience)}"
            f" | {_pts(r.cat_discipline)}"
            f" | {_pts(r.cat_optionality)}"
            f" | {impl_g}"
            f" | {_pct(r.delivered_fcf_growth)}"
            f" | {gap} |"
        )

    lines += [
        "",
        "_Gap = growth the price implies minus growth delivered."
        "  Larger absolute gap = bigger embedded expectation._",
        "",
        "## Excluded",
        "",
        "| Ticker | Band | Completeness | Stable | Universe | Config Hash | Flag |",
        "|:---|:---|---:|:---|:---|:---|:---|",
    ]

    for r in rows:
        band   = (f"{_pts(r.composite_low)}–{_pts(r.composite_high)}"
                  if r.composite is not None else "—")
        stable = ("yes" if r.is_stable else "⚠ unstable") if r.is_stable is not None else "—"
        lines.append(
            f"| {r.ticker}"
            f" | {band}"
            f" | {_completeness_display(r)}"
            f" | {stable}"
            f" | {r.universe_version or '—'}"
            f" | `{r.config_hash or '—'}`"
            f" | {r.flag or '—'} |"
        )

    # ETF / Fund section
    if etf_rows:
        lines += [
            "",
            "## ETFs / Funds",
            "",
            "> Holdings and fee data from market vendor (yfinance), best-effort,"
            " not filing-grade.",
            "",
            "| Ticker | Name | Exp Ratio | AUM | Top 5 Holdings | Overlap w/ Singles | Flag |",
            "|:---|:---|---:|---:|:---|---:|:---|",
        ]
        for er in etf_rows:
            exp_r = _pct(er.expense_ratio) if er.expense_ratio is not None else "n/a"
            top5  = _fmt_top5(er.top_holdings)
            ovlp  = _fmt_overlap(er)
            lines.append(
                f"| {er.ticker}"
                f" | {er.name or '—'}"
                f" | {exp_r}"
                f" | {_fmt_aum(er.aum)}"
                f" | {top5}"
                f" | {ovlp}"
                f" | {er.flag or '—'} |"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

def _render_html(
    rows: list[ScreenRow],
    etf_rows: Optional[list[EtfRow]] = None,
    sort_mode: str = "durability",
) -> str:  # noqa: C901
    e = escape

    ts          = datetime.now().strftime("%Y-%m-%d %H:%M")
    uni_ver     = next((r.universe_version for r in rows if r.universe_version), "—")
    cfg_h       = next((r.config_hash     for r in rows if r.config_hash),     "—")
    sort_label  = (
        "durability score" if sort_mode == "durability"
        else "quality-value (composite percentile − gap percentile)"
    )

    css = """\
:root{
  --bg:#0a0c10;--surf:#12151b;--surf2:#0d1016;--bdr:#1e242e;
  --txt:#dde3ef;--dim:#8898b5;--mute:#71809b;
  --mono:"SF Mono","Cascadia Code",ui-monospace,Menlo,monospace
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  background:var(--bg);color:var(--txt);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased
}
.wrap{max-width:1700px;margin:0 auto;padding:32px 24px 64px}
.hdr{margin-bottom:32px;padding-bottom:20px;border-bottom:1px solid var(--bdr)}
.hdr h1{font-size:17px;font-weight:600;letter-spacing:-.01em;margin-bottom:6px}
.hdr .meta{font-size:11px;color:var(--dim);letter-spacing:.02em;font-variant-numeric:tabular-nums}
.mono{font-family:var(--mono);font-size:10px;letter-spacing:.04em}
.sec-lbl{
  font-size:9.5px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
  color:var(--mute);margin-bottom:10px
}
table{width:100%;border-collapse:collapse}
thead th{
  position:sticky;top:0;z-index:2;background:var(--surf);
  font-size:9.5px;font-weight:700;letter-spacing:.10em;text-transform:uppercase;
  color:var(--mute);padding:10px 12px 9px;border-bottom:1px solid var(--bdr);
  text-align:right;white-space:nowrap;user-select:none
}
thead th.l{text-align:left}
td{
  padding:7px 12px;border-bottom:1px solid var(--bdr);
  font-variant-numeric:tabular-nums;font-family:var(--mono);
  font-size:12.5px;color:var(--txt);text-align:right;white-space:nowrap
}
td.tk{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  font-size:13px;font-weight:600;color:var(--txt);text-align:left;letter-spacing:.01em
}
td.tk.dim{color:var(--dim);font-weight:500}
tbody tr:nth-child(even) td{background:var(--surf2)}
tbody tr:hover td{background:#171b28cc!important}
.na{
  color:var(--mute);font-style:italic;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  font-size:11px;font-variant-numeric:normal
}
.primary-sec{margin-bottom:14px}
.legend{font-size:11px;color:var(--mute);margin-top:12px;line-height:1.65}
.sort-note{font-size:10px;color:var(--mute);margin-top:5px;letter-spacing:.03em}
.diag-sec{margin-top:44px}
.diag-sec table thead th{font-size:9px;padding:6px 12px 5px}
.diag-sec table td{font-size:11px;padding:4px 12px;color:var(--mute);font-variant-numeric:tabular-nums}
.diag-sec table td.tk{font-size:11px;font-weight:500;color:var(--dim)}
.flag-cell{
  font-style:italic;max-width:280px;overflow:hidden;text-overflow:ellipsis;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  font-variant-numeric:normal;font-size:10.5px
}
.etf-sec{margin-top:44px}
.etf-sec table thead th{font-size:9px;padding:6px 12px 5px}
.etf-sec table td{font-size:11px;padding:4px 12px;color:var(--mute);font-variant-numeric:tabular-nums}
.etf-sec table td.tk{font-size:11px;font-weight:500;color:var(--dim)}
.etf-sec td.holdings{
  font-size:10.5px;text-align:left;max-width:320px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  font-variant-numeric:normal
}
.caveat{font-size:10px;color:var(--mute);margin-top:8px;font-style:italic}"""

    def _score_td(val: Optional[float]) -> str:
        if val is None:
            return '<td class="na">—</td>'
        return f'<td>{e(_pts(val))}</td>'

    def _pct_td(val: Optional[float]) -> str:
        if val is None:
            return '<td class="na">n/a</td>'
        return f'<td>{e(_pct(val))}</td>'

    def _dash_td(val: Optional[str], cls: str = "") -> str:
        if not val:
            return f'<td class="na{(" " + cls) if cls else ""}">—</td>'
        return f'<td{(" class=\"" + cls + "\"") if cls else ""}>{e(val)}</td>'

    def _gap_td(r: ScreenRow) -> str:
        if r.expectations_gap is not None and not r.implied_growth_note:
            style = _gap_style(r.expectations_gap)
            return f'<td style="{style}">{e(_signed_pct(r.expectations_gap))}</td>'
        return '<td class="na">n/a</td>'

    def _impl_td(r: ScreenRow) -> str:
        if r.implied_fcf_growth is not None and not r.implied_growth_note:
            return f'<td>{e(_pct(r.implied_fcf_growth))}</td>'
        return '<td class="na">n/a</td>'

    # Primary signal rows (Equities)
    sig_rows: list[str] = []
    for r in rows:
        tk_cls = "tk dim" if (r.excluded or (r.flag and r.composite is None)) else "tk"
        sig_rows.append(
            f'<tr>'
            f'<td class="{tk_cls}">{e(r.ticker)}</td>'
            f'{_score_td(r.composite)}'
            f'{_score_td(r.cat_reinvestment)}'
            f'{_score_td(r.cat_quality)}'
            f'{_score_td(r.cat_resilience)}'
            f'{_score_td(r.cat_discipline)}'
            f'{_score_td(r.cat_optionality)}'
            f'{_impl_td(r)}'
            f'{_pct_td(r.delivered_fcf_growth)}'
            f'{_gap_td(r)}'
            f'</tr>'
        )

    # Diagnostics (Excluded) rows
    diag_rows: list[str] = []
    for r in rows:
        band   = (f"{_pts(r.composite_low)}–{_pts(r.composite_high)}"
                  if r.composite is not None else "—")
        stable = ("yes" if r.is_stable else "⚠ unstable") if r.is_stable is not None else "—"
        diag_rows.append(
            f'<tr>'
            f'<td class="tk">{e(r.ticker)}</td>'
            f'<td>{e(band)}</td>'
            f'<td>{e(_completeness_display(r))}</td>'
            f'<td>{e(stable)}</td>'
            f'<td>{e(r.universe_version or "—")}</td>'
            f'<td><span class="mono">{e(r.config_hash or "—")}</span></td>'
            f'<td class="flag-cell">{e(r.flag or "—")}</td>'
            f'</tr>'
        )

    # ETF rows
    etf_html_rows: list[str] = []
    for er in (etf_rows or []):
        exp_r = _pct(er.expense_ratio) if er.expense_ratio is not None else None
        top5  = _fmt_top5(er.top_holdings) or None
        ovlp  = _fmt_overlap(er) if er.overlap_with_screen is not None else None
        etf_html_rows.append(
            f'<tr>'
            f'<td class="tk">{e(er.ticker)}</td>'
            f'{_dash_td(er.name)}'
            f'{_dash_td(exp_r)}'
            f'{_dash_td(_fmt_aum(er.aum))}'
            f'{_dash_td(top5, cls="holdings")}'
            f'{_dash_td(ovlp)}'
            f'<td class="flag-cell">{e(er.flag or "—")}</td>'
            f'</tr>'
        )

    etf_section = ""
    if etf_html_rows:
        etf_section = f"""
<section class="etf-sec">
  <div class="sec-lbl">ETFs / Funds</div>
  <table>
    <thead>
      <tr>
        <th class="l">Ticker</th>
        <th class="l">Name</th>
        <th>Exp Ratio</th>
        <th>AUM</th>
        <th class="l">Top 5 Holdings</th>
        <th>Overlap w/ Singles</th>
        <th class="l">Flag</th>
      </tr>
    </thead>
    <tbody>
      {''.join(etf_html_rows)}
    </tbody>
  </table>
  <p class="caveat">Holdings and fee data from market vendor (yfinance), best-effort, not filing-grade.</p>
</section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Durability &amp; Expectations Screen</title>
<style>
{css}
</style>
</head>
<body>
<div class="wrap">

<header class="hdr">
  <h1>Durability &amp; Expectations Screen</h1>
  <p class="meta">Generated {e(ts)}&nbsp;&nbsp;&middot;&nbsp;&nbsp;Universe: {e(uni_ver)}&nbsp;&nbsp;&middot;&nbsp;&nbsp;Config:&nbsp;<span class="mono">{e(cfg_h)}</span></p>
</header>

<section class="primary-sec">
  <div class="sec-lbl">Equities</div>
  <table>
    <thead>
      <tr>
        <th class="l">Ticker</th>
        <th>Durability</th>
        <th>Reinv</th>
        <th>Quality</th>
        <th>Resilience</th>
        <th>Discipline</th>
        <th>Optionality</th>
        <th>Implied&nbsp;g</th>
        <th>Delivered&nbsp;g</th>
        <th>Gap</th>
      </tr>
    </thead>
    <tbody>
      {''.join(sig_rows)}
    </tbody>
  </table>
  <p class="legend">Gap = growth the price implies minus growth delivered &nbsp;&middot;&nbsp; deeper blue tint = larger embedded expectation, regardless of direction</p>
  <p class="sort-note">Sorted by: {e(sort_label)}</p>
</section>

<section class="diag-sec">
  <div class="sec-lbl">Excluded &amp; diagnostics</div>
  <table>
    <thead>
      <tr>
        <th class="l">Ticker</th>
        <th class="l">Band</th>
        <th>Completeness</th>
        <th class="l">Stable</th>
        <th class="l">Universe</th>
        <th class="l">Config Hash</th>
        <th class="l">Flag</th>
      </tr>
    </thead>
    <tbody>
      {''.join(diag_rows)}
    </tbody>
  </table>
</section>
{etf_section}
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_screen(
    tickers: list[str],
    cfg: dict,
    sort_mode: str = "durability",
    out_dir: Optional[Path] = None,
    verbose: bool = True,
) -> tuple[list[ScreenRow], list[EtfRow]]:
    """
    Screen a list of tickers.  Returns (operating_rows, etf_rows).

    sort_mode: "durability" | "quality-value"
    One ticker failing never kills the run.
    """
    sec_cfg = cfg.get("sec", {})
    history_years = cfg.get("report", {}).get("history_years", 15)

    client = EdgarClient(
        user_agent=sec_cfg.get("user_agent", ""),
        request_delay=sec_cfg.get("request_delay_seconds", 0.2),
        cache_dir=cfg.get("cache", {}).get("dir", ".cache/edgar"),
        cache_ttl_seconds=int(cfg.get("cache", {}).get("ttl_seconds", 86400)),
    )

    rows: list[ScreenRow] = []
    etf_rows: list[EtfRow] = []

    for tk in tickers:
        if verbose:
            print(f"  screening {tk} ...", file=sys.stderr)
        op_row, etf_row = _process_one(tk, client, cfg, history_years)
        if op_row is not None:
            rows.append(op_row)
        if etf_row is not None:
            etf_rows.append(etf_row)
        if verbose and ((op_row and op_row.flag) or etf_row):
            flag = op_row.flag if op_row else etf_row.flag
            print(f"    ! {tk}: {flag}", file=sys.stderr)

    # Compute overlap from the stored top_holdings — no re-fetch needed.
    # Overlap is defined over scored operating tickers only.
    operating_tickers = {r.ticker.upper() for r in rows if r.composite is not None}
    for er in etf_rows:
        if er.top_holdings and operating_tickers:
            matched = [(tk, w) for tk, w in er.top_holdings if tk.upper() in operating_tickers]
            er.overlap_with_screen = sum(w for _, w in matched) if matched else None
            er.overlap_count = len(matched) if matched else None

    # Compute batch-level quality-value scores before sorting
    _assign_quality_value_scores(rows)

    if sort_mode == "quality-value":
        rows.sort(
            key=lambda r: r.quality_value_score if r.quality_value_score is not None else -999.0,
            reverse=True,
        )
    else:
        rows.sort(key=lambda r: r.composite or 0.0, reverse=True)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d")
        (out_dir / f"screen_{ts}.md").write_text(
            _render_md(rows, etf_rows, sort_mode)
        )
        (out_dir / f"screen_{ts}.html").write_text(
            _render_html(rows, etf_rows, sort_mode)
        )
        if verbose:
            print(f"Screen written to {out_dir}/screen_{ts}.{{md,html}}", file=sys.stderr)

    return rows, etf_rows
