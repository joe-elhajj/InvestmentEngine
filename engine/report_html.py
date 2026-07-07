"""
report_html.py — render an AnalysisResult as HTML.

Two thin renderers share one set of section-builders:
  - render(res, peer_table)          -> full standalone dark HTML document
  - render_fragment(res, peer_table, durability_composite) -> light HTML
    fragment (no <html>/<head>) for inline embedding in the dashboard.

The builders below (`_position_rows`, `_quarter_section`, `_growth_rows`,
`_ratio_rows`, `_peer_rows`, `_rel_val_rows`, `_dcf_section`, `_gaps_list`,
`_summary`) decide WHAT data appears and in what order; they return plain
rows/dicts, already formatted with the shared `_fmt_*`/`_src` helpers. Each
renderer only decides HOW to lay that data out (dark full-page tables with
a visible Source column vs light collapsible sections with lineage in a
hover title). Nothing about which figures appear, or how they're computed
or formatted, is duplicated between the two.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Optional

from engine.edgar import classify_rnd_series, is_fpi
from engine.pipeline import AnalysisResult


# ---------------------------------------------------------------------------
# Formatting helpers (shared, format-agnostic)
# ---------------------------------------------------------------------------

def _fmt_currency(x: float | None) -> str:
    if x is None:
        return "n/a"
    ax = abs(x)
    if ax >= 1e12:
        return f"${x/1e12:.2f}T"
    if ax >= 1e9:
        return f"${x/1e9:.2f}B"
    if ax >= 1e6:
        return f"${x/1e6:.1f}M"
    return f"${x:,.0f}"


def _fmt_number(x: float | None) -> str:
    return _fmt_currency(x)


def _fmt_pct(x: float | None, nd: int = 1) -> str:
    return f"{x*100:.{nd}f}%" if x is not None else "n/a"


def _fmt_signed_pct(x: float | None, nd: int = 1) -> str:
    if x is None:
        return "n/a"
    sign = "+" if x >= 0 else "−"
    return f"{sign}{abs(x) * 100:.{nd}f}%"


def _ratio(m) -> str:
    if m is None or m.value is None:
        note = f" ({m.note})" if (m is not None and m.note) else ""
        return f"n/a{note}"
    return f"{m.value:.2f}"


def _src(fact_or_text) -> str:
    if isinstance(fact_or_text, str):
        return escape(fact_or_text)
    if fact_or_text is None:
        return "derived"
    return escape(fact_or_text.source())


def _derived_source(expression: str, inputs: list[tuple[str, object]]) -> str:
    parts = []
    for label, fact in inputs:
        if fact:
            parts.append(f"{label}: {fact.source()}")
        else:
            parts.append(f"{label}: n/a")
    return escape(f"derived: {expression} | " + " | ".join(parts))


def _verdict_color(value: float | None) -> str:
    if value is None:
        return "#999"
    if value >= 0:
        return "#56d364"
    return "#f85149"


# ---------------------------------------------------------------------------
# Shared section-builders — one source of truth for WHAT appears, consumed
# by both renderers. Every row is (label, formatted_value, source_string).
# ---------------------------------------------------------------------------

Row = tuple[str, str, str]


def _position_rows(res: AnalysisResult) -> list[Row]:
    cd = res.company
    fcf_source = _derived_source("FCF = CFO - capex", [
        ("CFO", cd.latest("cfo")),
        ("capex", cd.latest("capex")),
    ])
    debt_source = _derived_source("total debt = long_term_debt + short_term_debt", [
        ("long_term_debt", cd.latest("long_term_debt")),
        ("short_term_debt", cd.latest("short_term_debt")),
    ])
    liquid_source = _derived_source(
        "liquid assets = cash + short_term_investments + long_term_investments", [
            ("cash", cd.latest("cash")),
            ("short_term_investments", cd.latest("short_term_investments")),
            ("long_term_investments", cd.latest("long_term_investments")),
        ])
    return [
        ("Revenue", _fmt_number(res.derived.get("revenue")), _src(cd.latest("revenue"))),
        ("Operating income", _fmt_number(res.derived.get("operating_income")), _src(cd.latest("operating_income"))),
        ("Net income", _fmt_number(res.derived.get("net_income")), _src(cd.latest("net_income"))),
        ("Free cash flow", _fmt_number(res.derived.get("fcf")), fcf_source),
        ("Total assets", _fmt_number(res.derived.get("total_assets")), _src(cd.latest("total_assets"))),
        ("Total equity", _fmt_number(res.derived.get("total_equity")), _src(cd.latest("total_equity"))),
        ("Total debt", _fmt_number(res.derived.get("total_debt")), debt_source),
        ("Liquid assets (cash + securities)", _fmt_number(res.derived.get("liquid_assets")), liquid_source),
        ("Cash", _fmt_number(res.derived.get("cash")), _src(cd.latest("cash"))),
    ]


def _quarter_section(res: AnalysisResult) -> Optional[dict]:
    """Returns None when there's no latest-quarter data at all."""
    if not res.latest_quarter:
        return None
    lq = res.latest_quarter
    facts = lq["facts"]
    fcf_source = _derived_source("FCF = CFO - capex", [
        ("CFO", facts.get("cfo")), ("capex", facts.get("capex"))
    ])
    debt_source = _derived_source("total debt = long_term_debt + short_term_debt", [
        ("long_term_debt", facts.get("long_term_debt")),
        ("short_term_debt", facts.get("short_term_debt")),
    ])
    liquid_source = _derived_source(
        "liquid assets = cash + short_term_investments + long_term_investments", [
            ("cash", facts.get("cash")),
            ("short_term_investments", facts.get("short_term_investments")),
            ("long_term_investments", facts.get("long_term_investments")),
        ])
    rows: list[Row] = [
        ("Revenue", _fmt_number(lq.get("revenue")), _src(facts.get("revenue"))),
        ("Operating income", _fmt_number(lq.get("operating_income")), _src(facts.get("operating_income"))),
        ("Net income", _fmt_number(lq.get("net_income")), _src(facts.get("net_income"))),
        ("Free cash flow", _fmt_number(lq.get("fcf")), fcf_source),
        ("Total assets", _fmt_number(lq.get("total_assets")), _src(facts.get("total_assets"))),
        ("Total equity", _fmt_number(lq.get("total_equity")), _src(facts.get("total_equity"))),
        ("Total debt", _fmt_number(lq.get("total_debt")), debt_source),
        ("Liquid assets (cash + securities)", _fmt_number(lq.get("liquid_assets")), liquid_source),
        ("Cash", _fmt_number(lq.get("cash")), _src(facts.get("cash"))),
    ]
    margin_rows: list[tuple[str, str]] = [
        (label, _fmt_pct(lq["margins"][key].value) if lq["margins"][key].value is not None else "n/a")
        for label, key in [
            ("Operating margin", "operating_margin"),
            ("Net margin", "net_margin"),
            ("FCF margin", "fcf_margin"),
        ]
    ]
    return {
        "period_end": lq["period_end"],
        "filed": lq["filed"],
        "rows": rows,
        "margin_rows": margin_rows,
    }


def _growth_rows(res: AnalysisResult) -> list[tuple[str, str, str, str]]:
    def cell(g: dict, y: int) -> str:
        m = g.get(y)
        if not m or m.value is None:
            return "n/a"
        return _fmt_pct(m.value) + (" *" if m.note else "")

    return [
        (label, cell(g, 5), cell(g, 10), cell(g, 15))
        for label, g in res.growth.items()
    ]


_RATIO_ORDER = [
    ("Gross margin", "gross_margin", True), ("Operating margin", "operating_margin", True),
    ("Net margin", "net_margin", True), ("FCF margin", "fcf_margin", True),
    ("ROE", "roe", True), ("ROA", "roa", True), ("ROIC", "roic", True), ("ROCE", "roce", True),
    ("Current ratio", "current_ratio", False), ("Debt/Equity", "debt_to_equity", False),
    ("Interest coverage", "interest_coverage", False), ("FCF conversion", "fcf_conversion", False),
]


def _rnd_unadj_reason(res: AnalysisResult) -> Optional[str]:
    """
    R&D-UNADJ badge reason for the R&D-capitalization-adjusted ROIC row, or
    None when there's nothing to disclose. Per docs/assumptions.md's
    calibration principle 3 (abstain and disclose): a legitimate absence of
    R&D (NO_RND) is silent — zero adjustment is normal there, not an
    exception state — while an IFRS filer or an insufficient/gapped R&D
    history abstains loudly, with a reason distinguishing which.
    """
    fpi, _ = is_fpi(res.company)
    if fpi:
        return "IFRS filer — pending disposition"
    state, _ = classify_rnd_series(res.company)
    if state == "no_rnd":
        return None
    m = res.ratios.get("roic_adjusted")
    if m is not None and m.value is not None:
        return None  # adjustment succeeded -- no badge needed
    return "insufficient history"


def _ratio_rows(res: AnalysisResult) -> list[tuple[str, str]]:
    rows = []
    for label, key, is_pct in _RATIO_ORDER:
        m = res.ratios.get(key)
        if m is None:
            val = "n/a"
        elif m.value is None:
            val = f"n/a ({m.note})" if m.note else "n/a"
        else:
            val = _fmt_pct(m.value) if is_pct else f"{m.value:.2f}"
        rows.append((label, val))
        if key == "roic":
            # Dual ROIC (Damodaran R&D capitalization) -- displayed whenever
            # computable regardless of durability.rnd_capitalization.enabled
            # (that flag gates only the durability score's consumption; see
            # engine/durability.py). No row at all for a legitimate NO_RND
            # company -- zero adjustment is normal there, not an exception.
            # Plain text, not an HTML chip: this row's value string is
            # escape()'d at both call sites below like every other ratio,
            # and is not exempted from that here.
            adj = res.ratios.get("roic_adjusted")
            if adj is not None and adj.value is not None:
                rows.append(("ROIC (R&D-adj)", _fmt_pct(adj.value)))
            else:
                reason = _rnd_unadj_reason(res)
                if reason:
                    rows.append(("ROIC (R&D-adj)", f"n/a — R&D-UNADJ ({reason})"))
    return rows


def _peer_rows(peer_table: Optional[list]) -> Optional[list[tuple[str, str, str, str, str, int]]]:
    if not peer_table:
        return None

    def f(x, pct=False):
        if x is None:
            return "n/a"
        return _fmt_pct(x) if pct else f"{x:.2f}"

    return [
        (
            rs.metric, f(rs.target), f(rs.peer_median),
            f"{rs.percentile:.0f}" if rs.percentile is not None else "n/a",
            f(rs.zscore), rs.n_peers,
        )
        for rs in peer_table
    ]


def _rel_val_rows(res: AnalysisResult) -> Optional[list[tuple[str, str]]]:
    rv = res.rel_val
    if not rv:
        return None
    pe = f"{rv.pe:.1f}" if rv.pe is not None else "n/a"
    ev = f"{rv.ev_ebitda:.1f}" if rv.ev_ebitda is not None else "n/a"
    return [("P/E", pe), ("EV/EBITDA", ev), ("FCF yield", _fmt_pct(rv.fcf_yield))]


def _dcf_section(res: AnalysisResult) -> Optional[dict]:
    if not res.dcf:
        return None
    scenario_rows = []
    for name, d in res.dcf.items():
        a = d.assumptions
        scenario_rows.append({
            "name": name,
            "wacc": f"{a['wacc']:.1%}",
            "terminal_growth": f"{a['terminal_growth']:.1%}",
            "fair_value": _fmt_currency(d.fair_value_per_share),
            "upside": _fmt_pct(d.upside_vs_price) if d.upside_vs_price is not None else "n/a",
            "warnings": list(d.warnings),
        })
    sensitivity = None
    if res.sensitivity:
        g_values = sorted(next(iter(res.sensitivity.values())).keys())
        w_values = sorted(res.sensitivity.keys())
        sensitivity = {
            "g_values": [f"{g:.1%}" for g in g_values],
            # Each cell keeps BOTH the raw float (or None) and its already-
            # formatted display string — the raw value is never shown
            # itself, it only drives the fragment renderer's presentation-
            # only heat-tint (see render_fragment below); the full-page
            # renderer below unpacks and ignores it. Formatting/rounding is
            # unchanged either way — still exactly _fmt_currency's output.
            "rows": [
                (
                    f"{wv:.1%}",
                    [(res.sensitivity[wv][g], _fmt_currency(res.sensitivity[wv][g])) for g in g_values],
                )
                for wv in w_values
            ],
        }
    return {"scenarios": scenario_rows, "sensitivity": sensitivity}


# Expectations-gap scenario band (PR 3): the fixed axis a gap value is
# positioned against is a CONSTANT across tickers (not rescaled per-company)
# so the strip is visually comparable row to row, same principle as the
# durability score bars. Values beyond the axis still show their real
# number in the label; only the marker's pixel position clamps at the edge.
_GAP_BAND_AXIS_MIN = -0.30
_GAP_BAND_AXIS_MAX = 0.30


def _gap_band_axis_pct(gap: float) -> float:
    span = _GAP_BAND_AXIS_MAX - _GAP_BAND_AXIS_MIN
    pct = (gap - _GAP_BAND_AXIS_MIN) / span * 100.0
    return max(0.0, min(100.0, pct))


def _gap_band_section(res: AnalysisResult) -> Optional[dict]:
    """
    Shared data builder for the bull/base/bear expectations-gap band -- same
    pattern as _dcf_section: each renderer turns this dict into its own
    markup. None when no band exists (NO_BAND per PR 3's rule: base failed
    to converge, delivered_growth unavailable, or a bundle isn't
    configured) -- res.expectations_gap (today's single-scenario gap, shown
    via _summary()) is untouched either way.
    """
    band = res.expectations_gap_band
    if band is None:
        return None
    rows = []
    for name in ("bull", "base", "bear"):
        sc = band.scenarios[name]
        rows.append({
            "scenario": name,
            "wacc": _fmt_pct(sc.wacc),
            "terminal_growth": _fmt_pct(sc.terminal_growth),
            "implied_growth": _fmt_pct(sc.implied_growth),
            "gap": _fmt_signed_pct(sc.gap),
            "gap_raw": sc.gap,
            "converged": sc.converged,
            "bracket_bound": sc.bracket_bound,
            "axis_pct": _gap_band_axis_pct(sc.gap),
        })
    disclosure = None
    if band.band_status == "PARTIAL":
        failed = [r["scenario"] for r in rows if not r["converged"]]
        disclosure = (
            "expectations_gap: " + ", ".join(failed) +
            " implied growth outside bisection bracket — band incomplete"
        )
    return {
        "band_status": band.band_status,
        "fragile": band.fragile,
        "rows": rows,
        "disclosure": disclosure,
        "zero_pct": _gap_band_axis_pct(0.0),
        "delivered_growth": _fmt_pct(band.delivered_growth),
        "base_gap": _fmt_signed_pct(band.base_gap),
    }


def _gap_band_html(band_data: Optional[dict]) -> str:
    """
    Shared markup for the range strip + expanded scenario table -- used by
    both render() (dark full report) and render_fragment() (light
    dashboard embed). Colors/spacing come from CSS classes duplicated in
    each surface's own stylesheet (render()'s embedded <style> vs
    frontend/styles.css), same precedent as the DUR chip. All interpolated
    values are engine-formatted numbers/fixed scenario names -- never raw
    filing text -- so, like _dcf_section's table above, they're inserted
    unescaped.
    """
    if band_data is None:
        return ""
    rows = band_data["rows"]

    points_html = ""
    for r in rows:
        cls = "gap-band-point gap-band-point-" + r["scenario"]
        if r["scenario"] == "base":
            cls += " gap-band-point-emphasized"
        if not r["converged"]:
            cls += " gap-band-point-failed"
        title = (
            f"{r['scenario']}: {r['gap']}" if r["converged"]
            else f"{r['scenario']}: bracket {r['bracket_bound']} hit — not converged"
        )
        points_html += (
            f'<div class="{cls}" style="left:{r["axis_pct"]:.1f}%" '
            f'title="{escape(title)}"></div>'
        )

    converged_pcts = [r["axis_pct"] for r in rows if r["converged"]]
    track_html = ""
    if len(converged_pcts) >= 2:
        left, right = min(converged_pcts), max(converged_pcts)
        track_html = f'<div class="gap-band-track" style="left:{left:.1f}%;width:{(right - left):.1f}%"></div>'

    labels_html = "".join(
        '<span class="gap-band-label'
        + (" gap-band-label-emphasized" if r["scenario"] == "base" else "") + '">'
        + escape(r["scenario"]) + " " + escape(r["gap"] if r["converged"] else "n/a")
        + "</span>"
        for r in rows
    )

    disclosure_html = (
        f'<p class="report-caption gap-band-disclosure">{escape(band_data["disclosure"])}</p>'
        if band_data["disclosure"] else ""
    )
    fragile_html = ""
    if band_data["fragile"] == "FRAGILE":
        fragile_html = (
            '<p class="report-caption gap-band-disclosure">FRAGILE: the sign of the '
            "expectations gap differs across scenarios — this signal's direction is not "
            "robust to the WACC/terminal-growth assumption chosen.</p>"
        )
    elif band_data["fragile"] == "UNDETERMINABLE":
        fragile_html = (
            '<p class="report-caption gap-band-disclosure">Fragility: UNDETERMINABLE — '
            "a band that can't be fully solved cannot be assessed for scenario-dependence.</p>"
        )

    table_rows = "".join(
        f'<tr><td>{escape(r["scenario"])}</td><td>{r["wacc"]}</td><td>{r["terminal_growth"]}</td>'
        f'<td>{r["implied_growth"]}{" (not converged)" if not r["converged"] else ""}</td>'
        f'<td>{band_data["delivered_growth"]}</td><td>{r["gap"]}</td></tr>'
        for r in rows
    )

    return (
        '<div class="gap-band">'
        '<p class="report-caption">Expectations gap — bull/base/bear band</p>'
        '<div class="gap-band-strip">'
        f'<div class="gap-band-axis"><div class="gap-band-zero-tick" style="left:{band_data["zero_pct"]:.1f}%"></div>'
        f'{track_html}{points_html}</div>'
        f'<div class="gap-band-labels">{labels_html}</div>'
        "</div>"
        f"{disclosure_html}{fragile_html}"
        '<table class="gap-band-table"><thead><tr><th>Scenario</th><th>WACC</th><th>Term. g</th>'
        "<th>Implied growth</th><th>Delivered growth</th><th>Gap</th></tr></thead>"
        f"<tbody>{table_rows}</tbody></table>"
        "</div>"
    )


# DUR provenance chip: marks a Data-gaps entry as a DurabilityScore.gaps
# disclosure (scoring-level: net-cash resilience, mixed-basis, short-history,
# split-contamination) rather than a pipeline-level res.gaps entry
# (EDGAR-absence). Same outline-chip vocabulary as the R&D-UNADJ/INH-chip
# family (mono, uppercase, bordered, no fill) -- styled in render()'s own
# embedded <style> for the standalone document, and in frontend/styles.css
# for render_fragment() (which shares the dashboard's stylesheet). The
# chip's own markup is a hardcoded constant, never user data, so it's safe
# to splice in unescaped alongside the escape()'d gap text.
_DUR_CHIP = '<span class="dur-chip" title="Durability-scoring gap (not a pipeline data gap)">DUR</span> '


def _gaps_list(res: AnalysisResult, ds_gaps: Optional[list[str]] = None) -> list[tuple[str, bool]]:
    """
    (text, is_durability) pairs for the shared "Data gaps" section: res.gaps
    (pipeline, absence-is-not-zero) first, then ds_gaps (DurabilityScore.gaps,
    if provided) -- ONE list, not two sections. is_durability drives the DUR
    chip at render time. ds_gaps defaults to None (additive parameter) so any
    caller not yet passing a DurabilityScore renders exactly as before.
    """
    items: list[tuple[str, bool]] = [(g, False) for g in res.gaps]
    if ds_gaps:
        items += [(g, True) for g in ds_gaps]
    return items


def _gaps_li(gaps: list[tuple[str, bool]]) -> str:
    return "".join(
        f"<li>{_DUR_CHIP if is_dur else ''}{escape(g)}</li>" for g, is_dur in gaps
    )


def _basis_disclosure_tooltip(res: AnalysisResult) -> str:
    """
    Session B: the Expectations Gap number is implied-growth minus
    delivered-growth, but neither of those two components' basis is
    surfaced anywhere else on this fragment. Combine whichever basis
    caveats actually apply into one tooltip, attached to the one stat that
    exists here whose interpretation depends on them — empty string (no
    tooltip) in the common case where neither applies.
    """
    parts: list[str] = []
    label = res.delivered_growth_label
    if label.startswith("revenue CAGR"):
        # This surface checks res.expectations_gap directly (the real
        # attribute, already in hand here) while the frontend's equivalent
        # fix (PR #44) checks the `gated` proxy instead -- the two
        # conditions are proven equivalent (screen.py:70-104), just
        # expressed in the terms each surface has available. Not something
        # a future reader needs to reconcile.
        if res.expectations_gap is None:
            parts.append(
                "Delivered growth is a revenue-CAGR fallback (FCF history non-positive "
                "or unavailable), not FCF. No expectations gap is computed for this ticker."
            )
        else:
            parts.append(
                "Delivered growth is a revenue-CAGR fallback (FCF history non-positive "
                "or unavailable), not FCF — this Gap compares implied FCF growth "
                "against delivered REVENUE growth, not a like-for-like FCF gap."
            )
    if "window:" in label:
        parts.append(f"Delivered growth basis: {label}.")
    if res.quote.source == "yfinance" and "diluted_shares" in res.gaps:
        parts.append(
            "Implied growth's share count is from yfinance (market-vendor tier) — "
            "EDGAR diluted_shares was unavailable for this ticker."
        )
    return " ".join(parts)


def _summary(
    res: AnalysisResult,
    durability_composite: Optional[float] = None,
) -> dict:
    """
    One-line/summary-strip data shared by the dark header banner and the
    light fragment's summary strip. `durability_composite` is computed by
    the caller (durability.score() needs `cfg`, which this module doesn't
    take) — None when not available, rendered as n/a, never 0.
    """
    q = res.quote
    base = res.dcf.get("base") if res.dcf else None
    upside = base.upside_vs_price if base else None
    return {
        "ticker": res.company.ticker,
        "name": res.company.name,
        "price": _fmt_currency(q.price) if q.price is not None else "n/a",
        "market_cap": _fmt_currency(q.market_cap) if q.market_cap is not None else "n/a",
        "durability_composite": f"{durability_composite:.1f}" if durability_composite is not None else "n/a",
        "expectations_gap": _fmt_signed_pct(res.expectations_gap) if res.expectations_gap is not None else "n/a",
        "expectations_gap_raw": res.expectations_gap,
        "expectations_gap_tooltip": _basis_disclosure_tooltip(res),
        "dcf_upside": _fmt_pct(upside) if upside is not None else "n/a",
        "dcf_upside_raw": upside,
    }


# ---------------------------------------------------------------------------
# Dark full-page renderer
# ---------------------------------------------------------------------------

def _row_html(label: str, value: str, source: str) -> str:
    return ("<tr>"
            f"<td>{escape(label)}</td>"
            f"<td>{value}</td>"
            f"<td><code>{source}</code></td>"
            "</tr>")


def _section_html(title: str, content: str) -> str:
    return f"<section><h2>{escape(title)}</h2>{content}</section>"


def _table_html(header_cells: list[str], rows_html: str) -> str:
    header = "".join(f"<th>{escape(h)}</th>" for h in header_cells)
    return f"<table><thead><tr>{header}</tr></thead><tbody>{rows_html}</tbody></table>"


def render(res: AnalysisResult, peer_table: list | None = None, ds_gaps: list | None = None) -> str:
    cd = res.company
    summary = _summary(res)

    position_html = _table_html(
        ["Item", "Value", "Source"],
        "".join(_row_html(*r) for r in _position_rows(res)),
    )

    quarter_html = ""
    qs = _quarter_section(res)
    if qs:
        q_rows_html = "".join(_row_html(*r) for r in qs["rows"])
        q_margin_html = "".join(
            f"<tr><td>{escape(label)}</td><td>{value}</td></tr>"
            for label, value in qs["margin_rows"]
        )
        quarter_html = _section_html(
            "Most Recent Quarter (10-Q)",
            f"<p>Quarter ended {escape(qs['period_end'])} · filed {escape(qs['filed'])}</p>"
            f"{_table_html(['Item', 'Value', 'Source'], q_rows_html)}"
            f"<p><strong>Quarter margins</strong></p>"
            f"{_table_html(['Metric', 'Value'], q_margin_html)}"
        )

    growth_html = _table_html(
        ["Metric", "5y", "10y", "15y"],
        "".join(
            f"<tr><td>{escape(label)}</td><td>{y5}</td><td>{y10}</td><td>{y15}</td></tr>"
            for label, y5, y10, y15 in _growth_rows(res)
        ),
    )

    ratios_html = _table_html(
        ["Metric", "Value"],
        "".join(f"<tr><td>{escape(l)}</td><td>{escape(v)}</td></tr>" for l, v in _ratio_rows(res)),
    )

    peer_html = ""
    prows = _peer_rows(peer_table)
    if prows:
        rows_html = "".join(
            f"<tr><td>{escape(metric)}</td><td>{escape(target)}</td><td>{escape(median)}</td>"
            f"<td>{escape(pct)}</td><td>{escape(z)}</td><td>{n}</td></tr>"
            for metric, target, median, pct, z, n in prows
        )
        peer_html = (
            "<h3>Peer-relative comparison</h3>"
            + _table_html(["Metric", "Target", "Peer median", "Percentile", "z-score", "n"], rows_html)
        )

    rel_html = ""
    rvrows = _rel_val_rows(res)
    if rvrows:
        rel_html = _table_html(
            ["Metric", "Value"],
            "".join(f"<tr><td>{escape(l)}</td><td>{escape(v)}</td></tr>" for l, v in rvrows),
        )

    dcf_html = ""
    dcf = _dcf_section(res)
    if dcf:
        rows_html = ""
        for sc in dcf["scenarios"]:
            rows_html += (
                "<tr>"
                f"<td>{escape(sc['name'])}</td><td>{sc['wacc']}</td><td>{sc['terminal_growth']}</td>"
                f"<td>{sc['fair_value']}</td><td>{sc['upside']}</td>"
                "</tr>"
            )
            for warn in sc["warnings"]:
                rows_html += f"<tr><td colspan=5>⚠ {escape(warn)}</td></tr>"
        dcf_html = (
            "<p><strong>DCF (assumptions are yours, from config.yaml):</strong></p>"
            + _table_html(["Scenario", "WACC", "Term. g", "Fair value/share", "Upside vs price"], rows_html)
        )
        if dcf["sensitivity"]:
            sens = dcf["sensitivity"]
            header = "<tr><th>WACC \\ g</th>" + "".join(f"<th>{g}</th>" for g in sens["g_values"]) + "</tr>"
            body = "".join(
                f"<tr><td>{wacc_label}</td>" + "".join(f"<td>{c}</td>" for _, c in cells) + "</tr>"
                for wacc_label, cells in sens["rows"]
            )
            dcf_html += (
                "<p><strong>Sensitivity — base scenario fair value/share "
                "(rows=WACC, cols=terminal g):</strong></p>"
                f"<table><thead>{header}</thead><tbody>{body}</tbody></table>"
            )

    gap_band_html = _gap_band_html(_gap_band_section(res))
    valuation_html = (
        f"{rel_html}{dcf_html}{gap_band_html}" if (rel_html or dcf_html or gap_band_html) else ""
    )

    gaps = _gaps_list(res, ds_gaps)
    if gaps:
        gaps_items = _gaps_li(gaps)
        has_dur = any(is_dur for _, is_dur in gaps)
        legend = (
            ' A <span class="dur-chip">DUR</span>-marked entry is a '
            "durability-scoring disclosure, not a pipeline data gap."
            if has_dur else ""
        )
        gaps_html = (
            "<p>The following could not be resolved from EDGAR and were excluded "
            f"from the analysis (do not treat absence as zero).{legend}</p>"
            f"<ul>{gaps_items}</ul>"
        )
    else:
        gaps_html = "<p>- None — all targeted concepts resolved.</p>"

    verdict_text = (
        f"Ticker {escape(summary['ticker'])} · Price {summary['price']} · "
        f"Market cap {summary['market_cap']}"
    )
    if summary["dcf_upside_raw"] is not None:
        direction = "upside" if summary["dcf_upside_raw"] >= 0 else "downside"
        verdict_text += f" · DCF base-case {direction}: {summary['dcf_upside']}"
    verdict_color = _verdict_color(summary["dcf_upside_raw"])

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(cd.name)} ({escape(cd.ticker)}) — Fundamental Analysis</title>
<style>
body {{ background: #0f172a; color: #e2e8f0; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 0; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
a {{ color: #7dd3fc; }}
header h1 {{ margin: 0; font-size: clamp(2rem, 2.5vw, 3rem); }}
header p {{ margin: 8px 0 24px; color: #94a3b8; }}
.verdict {{ display: inline-flex; align-items: center; gap: 0.75rem; padding: 14px 18px; border-radius: 16px; background: #020617; border: 1px solid #334155; color: #c7d2fe; margin-bottom: 28px; }}
.section {{ margin-bottom: 32px; }}
section h2 {{ border-bottom: 1px solid #334155; padding-bottom: 10px; margin-bottom: 16px; color: #f8fafc; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
th, td {{ padding: 12px 14px; border: 1px solid #334155; text-align: left; }}
th {{ background: #1e293b; color: #e2e8f0; }}
tbody tr:nth-child(even) {{ background: #111827; }}
code {{ color: #cbd5e1; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace; white-space: pre-wrap; }}
.footer {{ color: #94a3b8; font-size: 0.95rem; margin-top: 40px; }}
.dur-chip {{ display:inline-block;margin-right:6px;padding:1px 5px;border-radius:5px;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:0.7rem;font-weight:700;letter-spacing:.02em;text-transform:uppercase;color:#fbbf24;border:1px solid rgba(251,191,36,0.35);cursor:default; }}
.report-caption {{ color: #94a3b8; font-size: 0.9rem; margin: 8px 0; }}
.gap-band {{ margin-top: 20px; }}
.gap-band-strip {{ margin: 10px 0 14px; }}
.gap-band-axis {{ position: relative; height: 8px; background: #1e293b; border-radius: 4px; margin: 0 4px 22px; }}
.gap-band-zero-tick {{ position: absolute; top: -4px; width: 2px; height: 16px; background: #64748b; transform: translateX(-1px); }}
.gap-band-track {{ position: absolute; top: 0; height: 8px; background: #334155; border-radius: 4px; }}
.gap-band-point {{ position: absolute; top: -4px; width: 16px; height: 16px; border-radius: 50%; background: #7dd3fc; border: 2px solid #0f172a; transform: translateX(-8px); }}
.gap-band-point-emphasized {{ width: 20px; height: 20px; background: #38bdf8; border-width: 3px; transform: translateX(-10px); z-index: 2; }}
.gap-band-point-failed {{ background: transparent; border: 2px dashed #f87171; }}
.gap-band-labels {{ display: flex; justify-content: space-between; font-size: 0.85rem; color: #cbd5e1; }}
.gap-band-label-emphasized {{ font-weight: 700; color: #f8fafc; }}
.gap-band-disclosure {{ color: #fbbf24; }}
.gap-band-table {{ margin-top: 10px; }}
</style>
</head>
<body>
<main>
<header>
<h1>{escape(cd.name)} ({escape(cd.ticker)})</h1>
<p>{escape(cd.sic)} {escape(cd.sic_description)} · CIK {escape(cd.cik)} · Generated {datetime.now():%Y-%m-%d %H:%M}</p>
<div class="verdict" style="border-color: {verdict_color}; color: {verdict_color};">
<strong>{escape(verdict_text)}</strong>
</div>
</header>
{_section_html('Financial position (latest FY)', position_html)}
{quarter_html}
{_section_html('Growth (CAGR)', growth_html)}
{_section_html('Margins, returns, leverage', ratios_html)}
{peer_html}
{_section_html('Valuation', valuation_html)}
{_section_html('Data gaps / not verified', gaps_html)}
<div class="footer">Fundamentals: SEC EDGAR XBRL companyfacts (primary source). Prices/multiples: see price source above. This report is a research input, not investment advice.</div>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Light fragment renderer — inline embedding in the dashboard (Task 4)
# ---------------------------------------------------------------------------

def _fr_row(label: str, value: str, source: str) -> str:
    """Fragment row: lineage sits behind a hover tooltip (title attr), not a
    visible third column — condensed-first layout."""
    title_attr = f' title="{source}"' if source else ""
    return (
        f'<tr><td class="l">{escape(label)}</td>'
        f'<td{title_attr}>{value}</td></tr>'
    )


def _fr_row_with_lineage(label: str, value: str, source: str) -> str:
    """
    Same data row as _fr_row (hover title kept as a quick peek), plus a
    second <tr> carrying the full lineage string in a muted monospace
    cell. The lineage row is hidden by CSS (.source-row) by default and
    revealed by the section's "Sources" toggle button — see
    _fr_details_with_sources(). `source` is already HTML-escaped by
    _src()/_derived_source(), so it's inserted as-is here (matching _fr_row).
    """
    title_attr = f' title="{source}"' if source else ""
    main = f'<tr><td class="l">{escape(label)}</td><td{title_attr}>{value}</td></tr>'
    lineage = f'<tr class="source-row"><td class="l source-cell" colspan="2">{source or "derived"}</td></tr>'
    return main + lineage


def _fr_table(header_cells: list[str], rows_html: str, left_cols: int = 1) -> str:
    ths = []
    for i, h in enumerate(header_cells):
        cls = "l" if i < left_cols else ""
        ths.append(f'<th class="{cls}">{escape(h)}</th>')
    return (
        '<div class="surface"><div class="scroll"><table>'
        f"<thead><tr>{''.join(ths)}</tr></thead><tbody>{rows_html}</tbody></table>"
        "</div></div>"
    )


def _fr_details(title: str, content: str, open_: bool = False) -> str:
    open_attr = " open" if open_ else ""
    return f'<details class="report-section"{open_attr}><summary>{escape(title)}</summary>{content}</details>'


def _flags_section(ticker: str) -> str:
    """
    Tier 2 (engine/flags.py) — unlike every other section in this fragment,
    this one is NOT server-rendered from AnalysisResult: it's a
    non-deterministic, separately-cached LLM extraction fetched from
    /api/flags/{ticker}, so the content here is just a placeholder that
    app.js populates on first expand (a delegated `toggle` listener on
    .flags-section, keyed off data-ticker). Closed by default, same as
    Growth/Margins/Valuation/Data gaps.
    """
    return (
        f'<details class="report-section flags-section" data-ticker="{escape(ticker)}">'
        "<summary>Flags</summary>"
        '<div class="flags-body"><p class="report-caption">Loading flags…</p></div>'
        "</details>"
    )


def _council_section(ticker: str) -> str:
    """
    Tier 3 (engine/council.py) — same non-deterministic, separately-cached
    pattern as _flags_section above: content is fetched client-side from
    /api/council/{ticker} on first expand, never server-rendered here.
    Equity-only (a fund has no 10-K, so it never has flags for a council to
    read either); closed by default, same as every other section.
    """
    return (
        f'<details class="report-section council-section" data-ticker="{escape(ticker)}">'
        "<summary>Council</summary>"
        '<div class="council-body"><p class="report-caption">Loading council…</p></div>'
        "</details>"
    )


def _fr_details_with_sources(title: str, content: str, open_: bool = False) -> str:
    """
    Same as _fr_details, plus a "Sources" toggle button in a small toolbar
    row between the <summary> and the table content — sibling to <summary>,
    not nested inside it, so clicking it never fights the native <details>
    expand/collapse. The button lives outside <summary> deliberately: the
    frontend wires a single delegated click listener (app.js) that toggles
    a "sources-on" class on this <details> element, which CSS uses to show
    the .source-row sub-rows _fr_row_with_lineage() emits. Default off, and
    scoped to this DOM subtree only — each expanded ticker's fragment is
    its own subtree, so this is "state per expanded ticker, not global" by
    construction, not by any JS bookkeeping.
    """
    open_attr = " open" if open_ else ""
    toolbar = (
        '<div class="section-toolbar">'
        '<button class="sources-toggle" type="button">Sources</button>'
        "</div>"
    )
    return (
        f'<details class="report-section"{open_attr}>'
        f"<summary>{escape(title)}</summary>{toolbar}{content}</details>"
    )


def _fr_stat(label: str, value: str, tint: Optional[float] = None, tooltip: Optional[str] = None) -> str:
    style = ""
    if tint is not None:
        magnitude = min(abs(tint), 0.30)
        alpha = 0.06 + (magnitude / 0.30) * 0.10
        rgb = "200,54,47" if tint > 0 else "29,125,84"
        color = "var(--bad)" if tint > 0 else "var(--good)"
        style = f' style="background-color:rgba({rgb},{alpha:.3f});color:{color}"'
    cls = "stat has-tooltip" if tooltip else "stat"
    tabindex_attr = ' tabindex="0"' if tooltip else ""
    tooltip_html = f'<div class="th-tooltip">{escape(tooltip)}</div>' if tooltip else ""
    # A stat whose value is the literal "n/a" string gets a distinct class
    # so CSS can render it in neutral gray instead of the bold near-ink
    # used for a real value — absence must never look like a real (if
    # oddly dark/bold) number. This never changes what text is shown,
    # only whether it's tagged for styling.
    value_cls = "stat-value stat-value-na" if value == "n/a" else "stat-value"
    return (
        f'<div class="{cls}"{style}{tabindex_attr}>'
        f'<span class="stat-label">{escape(label)}</span>'
        f'<span class="{value_cls}">{escape(value)}</span>'
        f"{tooltip_html}"
        "</div>"
    )


def render_fragment(
    res: AnalysisResult,
    peer_table: list | None = None,
    durability_composite: Optional[float] = None,
    ds_gaps: Optional[list[str]] = None,
) -> str:
    """
    Renders an HTML fragment (no <html>/<head>) styled to match the
    dashboard's light design system — meant to be embedded directly into
    the page (e.g. via innerHTML in an accordion row), not opened as a
    standalone document. Assumes the host page already loads the
    dashboard's stylesheet (same CSS custom properties: --bg, --surface,
    --text-1/2/3, --good/--bad, tabular-nums, etc.).
    """
    summary = _summary(res, durability_composite=durability_composite)

    summary_html = (
        '<div class="report-summary">'
        + _fr_stat("Price", summary["price"])
        + _fr_stat("Market Cap", summary["market_cap"])
        + _fr_stat("Durability", summary["durability_composite"])
        + _fr_stat(
            "Expectations Gap", summary["expectations_gap"], tint=summary["expectations_gap_raw"],
            tooltip=summary["expectations_gap_tooltip"] or None,
        )
        + _fr_stat(
            "DCF Base (Systematic)", summary["dcf_upside"],
            tooltip=(
                "Single-stage DCF under systematic config assumptions - comparable "
                "across tickers, conservative by construction for high-growth names. "
                "The expectations gap is the primary signal."
            ),
        )
        + "</div>"
    )

    position_html = _fr_details_with_sources(
        "Financial position",
        _fr_table(["Item", "Value"], "".join(_fr_row_with_lineage(*r) for r in _position_rows(res))),
    )

    quarter_html = ""
    qs = _quarter_section(res)
    if qs:
        q_rows_html = "".join(_fr_row_with_lineage(*r) for r in qs["rows"])
        q_margin_html = "".join(
            f'<tr><td class="l">{escape(label)}</td><td>{value}</td></tr>'
            for label, value in qs["margin_rows"]
        )
        quarter_html = _fr_details_with_sources(
            "Latest quarter",
            f'<p class="report-caption">Quarter ended {escape(qs["period_end"])} '
            f'· filed {escape(qs["filed"])}</p>'
            + _fr_table(["Item", "Value"], q_rows_html)
            + _fr_table(["Metric", "Value"], q_margin_html),
        )

    growth_html = _fr_details(
        "Growth",
        _fr_table(
            ["Metric", "5y", "10y", "15y"],
            "".join(
                f'<tr><td class="l">{escape(label)}</td><td>{y5}</td><td>{y10}</td><td>{y15}</td></tr>'
                for label, y5, y10, y15 in _growth_rows(res)
            ),
        ),
    )

    ratios_html = _fr_table(
        ["Metric", "Value"],
        "".join(f'<tr><td class="l">{escape(l)}</td><td>{escape(v)}</td></tr>' for l, v in _ratio_rows(res)),
    )
    prows = _peer_rows(peer_table)
    peer_html = ""
    if prows:
        peer_rows_html = "".join(
            f'<tr><td class="l">{escape(metric)}</td><td>{escape(target)}</td><td>{escape(median)}</td>'
            f'<td>{escape(pct)}</td><td>{escape(z)}</td><td>{n}</td></tr>'
            for metric, target, median, pct, z, n in prows
        )
        peer_html = (
            '<p class="report-caption">Peer-relative comparison</p>'
            + _fr_table(["Metric", "Target", "Peer median", "Percentile", "z-score", "n"], peer_rows_html)
        )
    margins_html = _fr_details("Margins & returns", ratios_html + peer_html)

    valuation_html = ""
    rvrows = _rel_val_rows(res)
    dcf = _dcf_section(res)
    gap_band_html = _gap_band_html(_gap_band_section(res))
    if rvrows or dcf or gap_band_html:
        content = ""
        if rvrows:
            content += _fr_table(
                ["Metric", "Value"],
                "".join(f'<tr><td class="l">{escape(l)}</td><td>{escape(v)}</td></tr>' for l, v in rvrows),
            )
        if dcf:
            rows_html = ""
            for sc in dcf["scenarios"]:
                rows_html += (
                    f'<tr><td class="l">{escape(sc["name"])}</td><td>{sc["wacc"]}</td>'
                    f'<td>{sc["terminal_growth"]}</td><td>{sc["fair_value"]}</td><td>{sc["upside"]}</td></tr>'
                )
                for warn in sc["warnings"]:
                    rows_html += f'<tr><td class="l na" colspan="5">{escape(warn)}</td></tr>'
            content += (
                '<p class="report-caption">DCF scenarios</p>'
                + _fr_table(
                    ["Scenario", "WACC", "Term. g", "Fair value/share", "Upside vs price"],
                    rows_html,
                )
            )
            if dcf["sensitivity"]:
                sens = dcf["sensitivity"]
                head_cells = ["WACC \\ g"] + sens["g_values"]
                # Two-axis heat tint: a presentation-only encoding derived
                # from the SAME already-computed fair-value numbers each
                # cell displays (deeper tint = higher fair value within
                # THIS grid) — never a new computation, and never shown as
                # a number itself, just a --heat custom property the CSS
                # reads. A missing cell (None) gets no tint at all, same
                # absence-is-not-zero rule the score bars follow: an
                # unknown fair value is not visually "the low end."
                raw_values = [v for _, cells in sens["rows"] for v, _ in cells if v is not None]
                lo = min(raw_values) if raw_values else 0.0
                hi = max(raw_values) if raw_values else 0.0
                spread = hi - lo

                def _heat_attr(v: Optional[float]) -> str:
                    if v is None or spread <= 0:
                        return ""
                    heat = (v - lo) / spread
                    return f' style="--heat:{heat:.3f}"'

                sens_rows_html = "".join(
                    f'<tr><td class="l">{wacc_label}</td>'
                    + "".join(f'<td class="sens-cell"{_heat_attr(v)}>{c}</td>' for v, c in cells)
                    + "</tr>"
                    for wacc_label, cells in sens["rows"]
                )
                content += (
                    '<p class="report-caption">Sensitivity — base scenario fair value/share '
                    "(rows=WACC, cols=terminal g)</p>"
                    + _fr_table(head_cells, sens_rows_html)
                )
        content += gap_band_html
        valuation_html = _fr_details("Valuation & sensitivity", content)

    gaps = _gaps_list(res, ds_gaps)
    if gaps:
        has_dur = any(is_dur for _, is_dur in gaps)
        legend = (
            ' A <span class="dur-chip">DUR</span>-marked entry is a '
            "durability-scoring disclosure, not a pipeline data gap."
            if has_dur else ""
        )
        gaps_html = (
            '<p class="report-caption">Could not be resolved from EDGAR; excluded '
            f"rather than defaulted to zero.{legend}</p>"
            '<ul class="gaps-list">' + _gaps_li(gaps) + "</ul>"
        )
    else:
        gaps_html = '<p class="report-caption">None — all targeted concepts resolved.</p>'
    gaps_section = _fr_details("Data gaps", gaps_html)

    flags_section = _flags_section(res.company.ticker)
    council_section = _council_section(res.company.ticker)

    return (
        '<div class="report-fragment">'
        + summary_html
        + position_html
        + quarter_html
        + growth_html
        + margins_html
        + valuation_html
        + gaps_section
        + flags_section
        + council_section
        + "</div>"
    )


# ---------------------------------------------------------------------------
# ETF/fund fragment renderer (Task 2) — a fund has no 10-K, so the equity
# sections above (Financial position, Latest quarter, Growth, Margins,
# Valuation) would be wall-to-wall n/a. That's correct per absence-is-not-
# zero, but it's the wrong SECTION SET for this security type, not a data
# gap to render through. Built from engine.etf.EtfProfile instead — a
# market-vendor-tier source, never filing-grade, and labeled as such on
# every field.
# ---------------------------------------------------------------------------

_VENDOR_TIER_SOURCE = "source: yfinance (market-vendor tier)"


def render_etf_fragment(
    profile,
    price: Optional[float],
    evidence: str,
    overlap_matches: list[tuple[str, float]],
) -> str:
    """
    profile: engine.etf.EtfProfile
    price: current quote price (None when unavailable) — a fund still
        trades, so this is a real market quote, not derived from `profile`.
    evidence: the classification evidence string (e.g. "ETF/Fund — fund
        forms observed" or "ETF/Fund — yfinance quoteType=ETF") — the same
        label the search badge and classify endpoint already use.
    overlap_matches: (ticker, weight) pairs — this fund's holdings that are
        also on the current equities watchlist (the detail behind the
        watchlist table's Overlap column).

    Durability/Gap/DCF do not apply to a fund and are omitted entirely from
    the summary strip rather than shown as n/a cards.
    """
    summary_html = (
        '<div class="report-summary">'
        + _fr_stat("Price", _fmt_currency(price) if price is not None else "n/a")
        + _fr_stat("AUM", _fmt_currency(profile.total_assets) if profile.total_assets is not None else "n/a")
        + "</div>"
    )

    profile_rows: list[Row] = [
        ("Name", profile.name or "n/a", _VENDOR_TIER_SOURCE),
        ("Classification evidence", evidence or "n/a", _VENDOR_TIER_SOURCE),
        ("Category / Index", profile.category or "n/a", _VENDOR_TIER_SOURCE),
        (
            "Expense ratio",
            _fmt_pct(profile.expense_ratio, 2) if profile.expense_ratio is not None else "n/a",
            _VENDOR_TIER_SOURCE,
        ),
        (
            "AUM",
            _fmt_currency(profile.total_assets) if profile.total_assets is not None else "n/a",
            _VENDOR_TIER_SOURCE,
        ),
        (
            "Top-10 concentration",
            _fmt_pct(profile.top10_concentration) if profile.top10_concentration is not None else "n/a",
            _VENDOR_TIER_SOURCE,
        ),
    ]
    profile_html = _fr_details(
        "Profile",
        _fr_table(["Item", "Value"], "".join(_fr_row(*r) for r in profile_rows)),
    )

    if overlap_matches:
        overlap_total = sum(w for _, w in overlap_matches)
        noun = "single" if len(overlap_matches) == 1 else "singles"
        overlap_rows_html = "".join(
            f'<tr><td class="l">{escape(tk)}</td>'
            f'<td title="{_VENDOR_TIER_SOURCE}">{_fmt_pct(w)}</td></tr>'
            for tk, w in overlap_matches
        )
        overlap_content = (
            f'<p class="report-caption">{len(overlap_matches)} watchlist {noun} held, '
            f"{_fmt_pct(overlap_total)} combined weight</p>"
            + _fr_table(["Ticker", "Weight"], overlap_rows_html)
        )
    else:
        overlap_content = (
            '<p class="report-caption">No overlap with the equities currently '
            "on your watchlist.</p>"
        )
    overlap_html = _fr_details("Overlap detail", overlap_content)

    return (
        '<div class="report-fragment">'
        + summary_html
        + profile_html
        + overlap_html
        + "</div>"
    )
