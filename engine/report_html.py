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
            "rows": [
                (f"{wv:.1%}", [_fmt_currency(res.sensitivity[wv][g]) for g in g_values])
                for wv in w_values
            ],
        }
    return {"scenarios": scenario_rows, "sensitivity": sensitivity}


def _gaps_list(res: AnalysisResult) -> list[str]:
    return list(res.gaps)


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


def render(res: AnalysisResult, peer_table: list | None = None) -> str:
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
                f"<tr><td>{wacc_label}</td>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
                for wacc_label, cells in sens["rows"]
            )
            dcf_html += (
                "<p><strong>Sensitivity — base scenario fair value/share "
                "(rows=WACC, cols=terminal g):</strong></p>"
                f"<table><thead>{header}</thead><tbody>{body}</tbody></table>"
            )

    valuation_html = f"{rel_html}{dcf_html}" if (rel_html or dcf_html) else ""

    gaps = _gaps_list(res)
    if gaps:
        gaps_items = "".join(f"<li>{escape(g)}</li>" for g in gaps)
        gaps_html = (
            "<p>The following could not be resolved from EDGAR and were excluded "
            "from the analysis (do not treat absence as zero):</p>"
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


def _fr_stat(label: str, value: str, tint: Optional[float] = None) -> str:
    style = ""
    if tint is not None:
        magnitude = min(abs(tint), 0.30)
        alpha = 0.06 + (magnitude / 0.30) * 0.10
        rgb = "200,54,47" if tint > 0 else "29,125,84"
        color = "var(--bad)" if tint > 0 else "var(--good)"
        style = f' style="background-color:rgba({rgb},{alpha:.3f});color:{color}"'
    return (
        f'<div class="stat"{style}>'
        f'<span class="stat-label">{escape(label)}</span>'
        f'<span class="stat-value">{escape(value)}</span>'
        "</div>"
    )


def render_fragment(
    res: AnalysisResult,
    peer_table: list | None = None,
    durability_composite: Optional[float] = None,
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
        + _fr_stat("Expectations Gap", summary["expectations_gap"], tint=summary["expectations_gap_raw"])
        + _fr_stat("DCF Base Upside", summary["dcf_upside"])
        + "</div>"
    )

    position_html = _fr_details(
        "Financial position",
        _fr_table(["Item", "Value"], "".join(_fr_row(*r) for r in _position_rows(res))),
        open_=True,
    )

    quarter_html = ""
    qs = _quarter_section(res)
    if qs:
        q_rows_html = "".join(_fr_row(*r) for r in qs["rows"])
        q_margin_html = "".join(
            f'<tr><td class="l">{escape(label)}</td><td>{value}</td></tr>'
            for label, value in qs["margin_rows"]
        )
        quarter_html = _fr_details(
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
    if rvrows or dcf:
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
                sens_rows_html = "".join(
                    f'<tr><td class="l">{wacc_label}</td>' + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
                    for wacc_label, cells in sens["rows"]
                )
                content += (
                    '<p class="report-caption">Sensitivity — base scenario fair value/share '
                    "(rows=WACC, cols=terminal g)</p>"
                    + _fr_table(head_cells, sens_rows_html)
                )
        valuation_html = _fr_details("Valuation & sensitivity", content)

    gaps = _gaps_list(res)
    if gaps:
        gaps_html = "<ul>" + "".join(f"<li>{escape(g)}</li>" for g in gaps) + "</ul>"
    else:
        gaps_html = '<p class="report-caption">None — all targeted concepts resolved.</p>'
    gaps_section = _fr_details("Data gaps", gaps_html)

    return (
        '<div class="report-fragment">'
        + summary_html
        + position_html
        + quarter_html
        + growth_html
        + margins_html
        + valuation_html
        + gaps_section
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
        open_=True,
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
