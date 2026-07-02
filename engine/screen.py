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


def _signed_pct(x: Optional[float], decimals: int = 1) -> str:
    """Percentage with an explicit + sign for positive values (used for the Gap column)."""
    if x is None:
        return "n/a"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x * 100:.{decimals}f}%"


def _gap_style(gap: Optional[float]) -> str:
    """
    Inline box-shadow tint for the Gap <td> — muted slate-blue proportional to |gap|.

    Uses box-shadow rather than background so it layers correctly on top of
    zebra-banding and hover backgrounds without overriding them.
    Color signals expectation magnitude only, not direction (no red/green moralizing).
    """
    if gap is None:
        return ""
    magnitude = min(abs(gap), 0.30)              # cap sensitivity at ±30 %
    alpha     = (magnitude / 0.30) * 0.22        # 0 → invisible, ±30 % → 22 % blue overlay
    return f"box-shadow:inset 0 0 0 1000px rgba(94,121,180,{alpha:.3f})"


def _render_md(rows: list[ScreenRow], sort_mode: str = "durability") -> str:
    """
    Two-section markdown output: primary signals first, diagnostics below.
    Numeric columns are right-aligned via markdown alignment syntax (---:).
    """
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
        "## Primary signals",
        "",
        "| Ticker | Durability | Reinv | Quality | Resilience | Discipline"
        " | Optionality | Implied g | Delivered g | Gap |",
        "|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for r in rows:
        impl_g = _pct(r.implied_fcf_growth) if not r.implied_growth_note else "n/a"
        gap    = _signed_pct(r.expectations_gap) if r.expectations_gap is not None and not r.implied_growth_note else "n/a"
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
        "## Data quality & provenance",
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
            f" | {_pct(r.completeness)}"
            f" | {stable}"
            f" | {r.universe_version or '—'}"
            f" | `{r.config_hash or '—'}`"
            f" | {r.flag or '—'} |"
        )

    return "\n".join(lines)


def _render_html(rows: list[ScreenRow], sort_mode: str = "durability") -> str:  # noqa: C901
    """
    Institutional-grade terminal view.

    Layout — two zones:
      Primary signals  : decision-relevant columns, full visual weight.
      Data quality     : Band, Completeness, Stable, Universe, Hash, Flag —
                         audit/trust guardrails, visually recessed.

    Design tokens
      #0a0c10  near-black page background
      #12151b  elevated table surface
      #0d1016  even-row surface (slightly darker)
      #1e242e  hairline border (not a heavy box, just a rule)
      #dde3ef  primary text
      #7a8499  dim secondary text
      #454e63  muted / label text
      rgba(94,121,180,α)  muted slate-blue gap tint (α scales with |gap|)

    Gap column color is the ONLY color in the table — signals expectation
    magnitude, not buy/sell direction.  Uses box-shadow so it layers on top
    of zebra-banding and hover without overriding the underlying background.

    All numeric cells use font-variant-numeric:tabular-nums so digits align
    vertically (non-negotiable for a financial display).
    """
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
  --txt:#dde3ef;--dim:#7a8499;--mute:#454e63;
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

/* ── Header ────────────────────────────────────────────────────── */
.hdr{margin-bottom:32px;padding-bottom:20px;border-bottom:1px solid var(--bdr)}
.hdr h1{font-size:17px;font-weight:600;letter-spacing:-.01em;margin-bottom:6px}
.hdr .meta{font-size:11px;color:var(--dim);letter-spacing:.02em;
           font-variant-numeric:tabular-nums}
.mono{font-family:var(--mono);font-size:10px;letter-spacing:.04em}

/* ── Section labels ─────────────────────────────────────────────── */
.sec-lbl{
  font-size:9.5px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
  color:var(--mute);margin-bottom:10px
}

/* ── Shared table base ──────────────────────────────────────────── */
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

/* ── Primary signal table ───────────────────────────────────────── */
.primary-sec{margin-bottom:14px}
.legend{font-size:11px;color:var(--mute);margin-top:12px;line-height:1.65}
.sort-note{font-size:10px;color:var(--mute);margin-top:5px;letter-spacing:.03em}

/* ── Diagnostics (recessed) ─────────────────────────────────────── */
.diag-sec{margin-top:44px}
.diag-sec table thead th{font-size:9px;padding:6px 12px 5px}
.diag-sec table td{
  font-size:11px;padding:4px 12px;color:var(--mute);
  font-variant-numeric:tabular-nums
}
.diag-sec table td.tk{font-size:11px;font-weight:500;color:var(--dim)}
.flag-cell{
  font-style:italic;max-width:240px;overflow:hidden;text-overflow:ellipsis;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  font-variant-numeric:normal;font-size:10.5px
}"""

    def _score_td(val: Optional[float]) -> str:
        if val is None:
            return '<td class="na">—</td>'
        return f'<td>{e(_pts(val))}</td>'

    def _pct_td(val: Optional[float]) -> str:
        if val is None:
            return '<td class="na">n/a</td>'
        return f'<td>{e(_pct(val))}</td>'

    def _gap_td(r: ScreenRow) -> str:
        if r.expectations_gap is not None and not r.implied_growth_note:
            style = _gap_style(r.expectations_gap)
            return f'<td style="{style}">{e(_signed_pct(r.expectations_gap))}</td>'
        return '<td class="na">n/a</td>'

    def _impl_td(r: ScreenRow) -> str:
        if r.implied_fcf_growth is not None and not r.implied_growth_note:
            return f'<td>{e(_pct(r.implied_fcf_growth))}</td>'
        return '<td class="na">n/a</td>'

    # ── Primary signal rows ──────────────────────────────────────────
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

    # ── Diagnostics rows ─────────────────────────────────────────────
    diag_rows: list[str] = []
    for r in rows:
        band   = (f"{_pts(r.composite_low)}–{_pts(r.composite_high)}"
                  if r.composite is not None else "—")
        stable = ("yes" if r.is_stable else "⚠ unstable") if r.is_stable is not None else "—"
        diag_rows.append(
            f'<tr>'
            f'<td class="tk">{e(r.ticker)}</td>'
            f'<td>{e(band)}</td>'
            f'{_pct_td(r.completeness)}'
            f'<td>{e(stable)}</td>'
            f'<td>{e(r.universe_version or "—")}</td>'
            f'<td><span class="mono">{e(r.config_hash or "—")}</span></td>'
            f'<td class="flag-cell">{e(r.flag or "—")}</td>'
            f'</tr>'
        )

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
  <div class="sec-lbl">Primary signals</div>
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
  <div class="sec-lbl">Data quality &amp; provenance</div>
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
