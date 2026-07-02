"""
screen.py — batch durability screener.

Routes each ticker:
  - Equities with EDGAR fundamentals → durability scorecard + growth signals
  - ETFs / funds (no EDGAR fundamentals or ETF name pattern) → flagged, not scored
  - Network / data failures → logged, run continues; one bad ticker never kills the run

Outputs a ranked Markdown + HTML table (matching existing report style).

Sort modes
----------
--sort durability     : ranked by durability composite (highest first)
--sort quality-value  : ranked by (composite_percentile − gap_percentile) within the batch.
                        High durability AND low/negative expectations gap → top rank.
                        Formula: composite_pct − gap_pct  (both 0–100 within the batch).
                        Companies without a solvable expectations gap are ranked last.

Per-company DCF upside has been removed from batch output (C1).  DCF belongs in
the per-ticker deep-dive (analyze.py), where the analyst owns the assumptions.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional

from engine.edgar import EdgarClient
from engine.market import get_quote
from engine.pipeline import derive
from engine import durability as D


# ---------------------------------------------------------------------------
# Routing outcome per ticker
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
    flag: str                                # "" | "ETF/fund" | "excluded" | "error:<msg>"
    excluded: bool = False


# ---------------------------------------------------------------------------
# ETF / fund detection
# ---------------------------------------------------------------------------

_ETF_KEYWORDS = ("ETF", "FUND", "TRUST", "ISHARES", "SPDR", "VANGUARD", "INVESCO")


def _is_etf(ticker: str, name: str) -> bool:
    combined = (ticker + " " + name).upper()
    return any(kw in combined for kw in _ETF_KEYWORDS)


def _has_fundamentals(cd_series: dict) -> bool:
    """A company with neither revenue nor total_assets is treated as a fund."""
    return bool(cd_series.get("revenue") or cd_series.get("total_assets"))


# ---------------------------------------------------------------------------
# Per-ticker processing
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


def _process_one(
    ticker: str,
    client: EdgarClient,
    cfg: dict,
    history_years: int,
) -> ScreenRow:
    universe_version = cfg.get("universe", {}).get("version", "")
    try:
        cd = client.get_company(ticker, history_years)
    except Exception as e:
        return _empty_row(ticker, f"error:{type(e).__name__}: {e}",
                          universe_version=universe_version)

    # ETF / fund routing
    if _is_etf(ticker, cd.name) or not _has_fundamentals(cd.series):
        return _empty_row(ticker, "ETF/fund — not scored, separate lens pending",
                          universe_version=universe_version)

    try:
        quote = get_quote(ticker)
        res = derive(cd, quote, cfg)
        ds = D.score(res, cfg)
    except Exception as e:
        return _empty_row(ticker, f"error:{type(e).__name__}: {e}",
                          universe_version=universe_version)

    if ds.excluded:
        return _empty_row(
            ticker, ds.exclusion_reason, excluded=True,
            completeness=ds.data_completeness, config_hash=ds.config_hash,
            universe_version=universe_version,
        )

    def _cat(name: str) -> Optional[float]:
        c = ds.categories.get(name)
        return c.composite if c else None

    # Build growth-signal columns
    igr = res.implied_growth_result
    if igr is None:
        implied_g = None
        ig_note = "n/a — not meaningful: normalized FCF unavailable or non-positive"
    elif igr.bracket_hit:
        implied_g = None
        bound = igr.bracket_bound or "?"
        ig_note = (
            f"n/a — not meaningful: bracket {bound} hit "
            f"(implied g {'<' if bound == 'lower' else '>'} "
            f"{igr.implied_growth:.0%})"
        )
    else:
        implied_g = igr.implied_growth
        ig_note = ""

    gap = res.expectations_gap if (igr is not None and not igr.bracket_hit) else None

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
        flag="",
    )


# ---------------------------------------------------------------------------
# Batch quality-value score (C3)
# ---------------------------------------------------------------------------

def _assign_quality_value_scores(rows: list[ScreenRow]) -> None:
    """
    quality_value_score = composite_percentile − gap_percentile  (within batch).

    High durability AND low/negative expectations gap → high score.
    Computed after all tickers are scored; companies without a solvable gap
    receive None and are ranked last in quality-value mode.

    Formula is intentionally simple and transparent: no magic, no weighting.
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
# Rendering
# ---------------------------------------------------------------------------

def _pct(x: Optional[float], decimals: int = 1) -> str:
    return f"{x * 100:.{decimals}f}%" if x is not None else "n/a"


def _pts(x: Optional[float]) -> str:
    return f"{x:.1f}" if x is not None else "n/a"


def _render_md(rows: list[ScreenRow]) -> str:
    lines = [
        f"# Durability Screen — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "| Ticker | Score | Band | Reinv | Quality | Resilience | Discipline"
        " | Optionality | Complete | Stable | Implied g | Delivered g"
        " | Gap | Univ | Hash | Flag |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        band   = f"{_pts(r.composite_low)}–{_pts(r.composite_high)}" if r.composite is not None else "n/a"
        stable = ("yes" if r.is_stable else "⚠ unstable") if r.is_stable is not None else "n/a"
        impl_g = _pct(r.implied_fcf_growth) if not r.implied_growth_note else r.implied_growth_note
        lines.append(
            f"| {r.ticker} | {_pts(r.composite)} | {band}"
            f" | {_pts(r.cat_reinvestment)} | {_pts(r.cat_quality)}"
            f" | {_pts(r.cat_resilience)} | {_pts(r.cat_discipline)}"
            f" | {_pts(r.cat_optionality)} | {_pct(r.completeness)}"
            f" | {stable} | {impl_g} | {_pct(r.delivered_fcf_growth)}"
            f" | {_pct(r.expectations_gap)} | {r.universe_version or 'n/a'}"
            f" | {r.config_hash or 'n/a'} | {r.flag} |"
        )
    return "\n".join(lines)


def _render_html(rows: list[ScreenRow]) -> str:
    def e(s: str) -> str:
        return escape(str(s))

    header = (
        "<tr><th>Ticker</th><th>Score</th><th>Band</th>"
        "<th>Reinv</th><th>Quality</th><th>Resilience</th>"
        "<th>Discipline</th><th>Optionality</th><th>Complete</th>"
        "<th>Stable</th><th>Implied&nbsp;g</th><th>Delivered&nbsp;g</th>"
        "<th>Gap</th><th>Univ</th><th>Hash</th><th>Flag</th></tr>"
    )
    body_rows = []
    for r in rows:
        band   = f"{_pts(r.composite_low)}–{_pts(r.composite_high)}" if r.composite is not None else "n/a"
        stable = ("yes" if r.is_stable else "⚠ unstable") if r.is_stable is not None else "n/a"
        impl_g = _pct(r.implied_fcf_growth) if not r.implied_growth_note else r.implied_growth_note
        body_rows.append(
            f"<tr><td>{e(r.ticker)}</td><td>{e(_pts(r.composite))}</td>"
            f"<td>{e(band)}</td>"
            f"<td>{e(_pts(r.cat_reinvestment))}</td><td>{e(_pts(r.cat_quality))}</td>"
            f"<td>{e(_pts(r.cat_resilience))}</td><td>{e(_pts(r.cat_discipline))}</td>"
            f"<td>{e(_pts(r.cat_optionality))}</td><td>{e(_pct(r.completeness))}</td>"
            f"<td>{e(stable)}</td>"
            f"<td>{e(impl_g)}</td>"
            f"<td>{e(_pct(r.delivered_fcf_growth))}</td>"
            f"<td>{e(_pct(r.expectations_gap))}</td>"
            f"<td>{e(r.universe_version or 'n/a')}</td>"
            f"<td><code>{e(r.config_hash or 'n/a')}</code></td>"
            f"<td>{e(r.flag)}</td></tr>"
        )
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Durability Screen</title>"
        "<style>body{background:#0f172a;color:#e2e8f0;font-family:system-ui}"
        "table{border-collapse:collapse;width:100%}th,td{padding:8px 10px;"
        "border:1px solid #334155;text-align:left}th{background:#1e293b}"
        "tbody tr:nth-child(even){background:#111827}</style></head><body>"
        "<main style='max-width:1600px;margin:0 auto;padding:24px'>"
        f"<h1>Durability Screen — {escape(datetime.now().strftime('%Y-%m-%d %H:%M'))}</h1>"
        f"<table><thead>{header}</thead><tbody>{''.join(body_rows)}</tbody></table>"
        "</main></body></html>"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_screen(
    tickers: list[str],
    cfg: dict,
    sort_mode: str = "durability",
    out_dir: Optional[Path] = None,
    verbose: bool = True,
) -> list[ScreenRow]:
    """
    Screen a list of tickers.  Returns sorted ScreenRows.

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
    for tk in tickers:
        if verbose:
            print(f"  screening {tk} ...", file=sys.stderr)
        row = _process_one(tk, client, cfg, history_years)
        rows.append(row)
        if verbose and row.flag:
            print(f"    ! {tk}: {row.flag}", file=sys.stderr)

    # Compute batch-level quality-value scores before sorting
    _assign_quality_value_scores(rows)

    if sort_mode == "quality-value":
        # Companies without a solvable gap ranked last
        rows.sort(
            key=lambda r: r.quality_value_score if r.quality_value_score is not None else -999.0,
            reverse=True,
        )
    else:
        rows.sort(key=lambda r: r.composite or 0.0, reverse=True)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d")
        (out_dir / f"screen_{ts}.md").write_text(_render_md(rows))
        (out_dir / f"screen_{ts}.html").write_text(_render_html(rows))
        if verbose:
            print(f"Screen written to {out_dir}/screen_{ts}.{{md,html}}", file=sys.stderr)

    return rows
