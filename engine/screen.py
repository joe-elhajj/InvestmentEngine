"""
screen.py — batch durability screener.

Routes each ticker:
  - Equities with EDGAR companyfacts → durability scorecard + valuation
  - ETFs / funds (no EDGAR fundamentals) → flagged, never scored
  - Network / data failures → logged, run continues

Outputs a ranked Markdown + HTML table (matching existing report style).
Sort modes: --sort durability | --sort quality-value
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
    dcf_upside: Optional[float]          # base-case DCF upside vs price
    quality_value_score: Optional[float] # composite penalized by downside
    flag: str                            # "" | "ETF/fund" | "excluded" | "error:<msg>"
    excluded: bool = False


# ---------------------------------------------------------------------------
# ETF / fund detection
# ---------------------------------------------------------------------------

_ETF_KEYWORDS = ("ETF", "FUND", "TRUST", "ISHARES", "SPDR", "VANGUARD", "INVESCO")


def _is_etf(ticker: str, name: str) -> bool:
    combined = (ticker + " " + name).upper()
    return any(kw in combined for kw in _ETF_KEYWORDS)


def _has_fundamentals(cd_series: dict) -> bool:
    """A company without revenue and total_assets data is treated as a fund."""
    return bool(cd_series.get("revenue") or cd_series.get("total_assets"))


# ---------------------------------------------------------------------------
# Per-ticker processing
# ---------------------------------------------------------------------------

def _process_one(
    ticker: str,
    client: EdgarClient,
    cfg: dict,
    history_years: int,
) -> ScreenRow:
    try:
        cd = client.get_company(ticker, history_years)
    except Exception as e:
        return ScreenRow(
            ticker=ticker,
            composite=None, composite_low=None, composite_high=None,
            cat_reinvestment=None, cat_quality=None, cat_resilience=None,
            cat_discipline=None, cat_optionality=None,
            completeness=None, is_stable=None, stability_delta=None,
            config_hash=None, dcf_upside=None, quality_value_score=None,
            flag=f"error:{type(e).__name__}: {e}",
        )

    # ETF / fund routing
    if _is_etf(ticker, cd.name) or not _has_fundamentals(cd.series):
        return ScreenRow(
            ticker=ticker,
            composite=None, composite_low=None, composite_high=None,
            cat_reinvestment=None, cat_quality=None, cat_resilience=None,
            cat_discipline=None, cat_optionality=None,
            completeness=None, is_stable=None, stability_delta=None,
            config_hash=None, dcf_upside=None, quality_value_score=None,
            flag="ETF/fund — not scored, separate lens pending",
        )

    try:
        quote = get_quote(ticker)
        res = derive(cd, quote, cfg)
        ds = D.score(res, cfg)
    except Exception as e:
        return ScreenRow(
            ticker=ticker,
            composite=None, composite_low=None, composite_high=None,
            cat_reinvestment=None, cat_quality=None, cat_resilience=None,
            cat_discipline=None, cat_optionality=None,
            completeness=None, is_stable=None, stability_delta=None,
            config_hash=None, dcf_upside=None, quality_value_score=None,
            flag=f"error:{type(e).__name__}: {e}",
        )

    if ds.excluded:
        return ScreenRow(
            ticker=ticker,
            composite=None, composite_low=None, composite_high=None,
            cat_reinvestment=None, cat_quality=None, cat_resilience=None,
            cat_discipline=None, cat_optionality=None,
            completeness=ds.data_completeness, is_stable=None, stability_delta=None,
            config_hash=ds.config_hash, dcf_upside=None, quality_value_score=None,
            flag=ds.exclusion_reason, excluded=True,
        )

    def _cat(name: str) -> Optional[float]:
        c = ds.categories.get(name)
        return c.composite if c else None

    dcf_upside = res.dcf.get("base", None)
    dcf_up_val = dcf_upside.upside_vs_price if dcf_upside else None

    # quality-value: composite penalized by downside (negative upside → lower rank)
    qv: Optional[float] = None
    if dcf_up_val is not None:
        penalty = max(0.0, -dcf_up_val) * 100.0   # 10% downside → -10 pts
        qv = ds.composite - penalty
    else:
        qv = ds.composite

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
        dcf_upside=dcf_up_val,
        quality_value_score=qv,
        flag="",
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _pct(x: Optional[float]) -> str:
    return f"{x*100:.1f}%" if x is not None else "n/a"


def _pts(x: Optional[float]) -> str:
    return f"{x:.1f}" if x is not None else "n/a"


def _render_md(rows: list[ScreenRow]) -> str:
    lines = [
        f"# Durability Screen — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "| Ticker | Score | Band | Reinv | Quality | Resilience | Discipline | Optionality | Complete | Stable | DCF upside | Hash | Flag |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        band = f"{_pts(r.composite_low)}–{_pts(r.composite_high)}" if r.composite is not None else "n/a"
        stable = ("yes" if r.is_stable else "⚠ unstable") if r.is_stable is not None else "n/a"
        lines.append(
            f"| {r.ticker} | {_pts(r.composite)} | {band} "
            f"| {_pts(r.cat_reinvestment)} | {_pts(r.cat_quality)} "
            f"| {_pts(r.cat_resilience)} | {_pts(r.cat_discipline)} "
            f"| {_pts(r.cat_optionality)} | {_pct(r.completeness)} "
            f"| {stable} | {_pct(r.dcf_upside)} | {r.config_hash or 'n/a'} "
            f"| {r.flag} |"
        )
    return "\n".join(lines)


def _render_html(rows: list[ScreenRow]) -> str:
    def e(s: str) -> str:
        return escape(str(s))

    header = (
        "<tr><th>Ticker</th><th>Score</th><th>Band</th>"
        "<th>Reinv</th><th>Quality</th><th>Resilience</th>"
        "<th>Discipline</th><th>Optionality</th><th>Complete</th>"
        "<th>Stable</th><th>DCF upside</th><th>Hash</th><th>Flag</th></tr>"
    )
    body_rows = []
    for r in rows:
        band = f"{_pts(r.composite_low)}–{_pts(r.composite_high)}" if r.composite is not None else "n/a"
        stable = ("yes" if r.is_stable else "⚠ unstable") if r.is_stable is not None else "n/a"
        body_rows.append(
            f"<tr><td>{e(r.ticker)}</td><td>{e(_pts(r.composite))}</td>"
            f"<td>{e(band)}</td>"
            f"<td>{e(_pts(r.cat_reinvestment))}</td><td>{e(_pts(r.cat_quality))}</td>"
            f"<td>{e(_pts(r.cat_resilience))}</td><td>{e(_pts(r.cat_discipline))}</td>"
            f"<td>{e(_pts(r.cat_optionality))}</td><td>{e(_pct(r.completeness))}</td>"
            f"<td>{e(stable)}</td><td>{e(_pct(r.dcf_upside))}</td>"
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
        "<main style='max-width:1400px;margin:0 auto;padding:24px'>"
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
    Screen a list of tickers. Returns sorted ScreenRows.

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

    sort_key = (
        (lambda r: r.quality_value_score or 0.0)
        if sort_mode == "quality-value"
        else (lambda r: r.composite or 0.0)
    )
    rows.sort(key=sort_key, reverse=True)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d")
        (out_dir / f"screen_{ts}.md").write_text(_render_md(rows))
        (out_dir / f"screen_{ts}.html").write_text(_render_html(rows))
        if verbose:
            print(f"Screen written to {out_dir}/screen_{ts}.{{md,html}}", file=sys.stderr)

    return rows


if __name__ == "__main__":
    import argparse
    import yaml

    ap = argparse.ArgumentParser(description="Durability batch screener")
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--sort", default="durability", choices=["durability", "quality-value"])
    ap.add_argument("--out", default="reports", help="output directory")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--verbose", action="store_true", default=True)
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    rows = run_screen(
        tickers=args.tickers,
        cfg=cfg,
        sort_mode=args.sort,
        out_dir=Path(args.out),
        verbose=True,
    )

    for r in rows:
        score = f"{r.composite:.1f}" if r.composite is not None else "excl."
        flag = f" [{r.flag}]" if r.flag else ""
        print(f"{r.ticker:6s}  {score:6s}{flag}")
