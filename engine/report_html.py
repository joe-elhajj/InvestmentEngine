"""report_html.py — render an AnalysisResult as a self-contained HTML file."""

from __future__ import annotations

from datetime import datetime
from html import escape

from engine.pipeline import AnalysisResult


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


def _one_line_verdict(res: AnalysisResult) -> str:
    q = res.quote
    price = _fmt_currency(q.price) if q.price is not None else "n/a"
    market_cap = _fmt_currency(q.market_cap) if q.market_cap is not None else "n/a"
    base = res.dcf.get("base") if res.dcf else None
    if base:
        upside = base.upside_vs_price
        direction = "upside" if upside is not None and upside >= 0 else "downside"
        return (f"Ticker {escape(res.company.ticker)} · Price {price} · Market cap {market_cap} · "
                f"DCF base-case {direction}: {_fmt_pct(upside) if upside is not None else 'n/a'}")
    return f"Ticker {escape(res.company.ticker)} · Price {price} · Market cap {market_cap}"


def _section(title: str, content: str) -> str:
    return f"<section><h2>{escape(title)}</h2>{content}</section>"


def _row(label: str, value: str, source: str) -> str:
    return ("<tr>"
            f"<td>{escape(label)}</td>"
            f"<td>{value}</td>"
            f"<td><code>{source}</code></td>"
            "</tr>")


def render(res: AnalysisResult, peer_table: list | None = None) -> str:
    cd = res.company
    q = res.quote
    summary_text = _one_line_verdict(res)

    fcf_source = _derived_source("FCF = CFO - capex", [
        ("CFO", cd.latest("cfo")),
        ("capex", cd.latest("capex")),
    ])
    debt_source = _derived_source("total debt = long_term_debt + short_term_debt", [
        ("long_term_debt", cd.latest("long_term_debt")),
        ("short_term_debt", cd.latest("short_term_debt")),
    ])
    liquid_source = _derived_source("liquid assets = cash + short_term_investments + long_term_investments", [
        ("cash", cd.latest("cash")),
        ("short_term_investments", cd.latest("short_term_investments")),
        ("long_term_investments", cd.latest("long_term_investments")),
    ])

    items = [
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

    rows_html = "\n".join(_row(label, value, source) for label, value, source in items)
    position_html = (
        "<table><thead><tr><th>Item</th><th>Value</th><th>Source</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
    )

    quarter_html = ""
    if res.latest_quarter:
        q = res.latest_quarter
        facts = q["facts"]
        q_fcf_source = _derived_source("FCF = CFO - capex", [
            ("CFO", facts.get("cfo")), ("capex", facts.get("capex"))
        ])
        q_debt_source = _derived_source("total debt = long_term_debt + short_term_debt", [
            ("long_term_debt", facts.get("long_term_debt")),
            ("short_term_debt", facts.get("short_term_debt")),
        ])
        q_liquid_source = _derived_source("liquid assets = cash + short_term_investments + long_term_investments", [
            ("cash", facts.get("cash")),
            ("short_term_investments", facts.get("short_term_investments")),
            ("long_term_investments", facts.get("long_term_investments")),
        ])
        q_items = [
            ("Revenue", _fmt_number(q.get("revenue")), _src(facts.get("revenue"))),
            ("Operating income", _fmt_number(q.get("operating_income")), _src(facts.get("operating_income"))),
            ("Net income", _fmt_number(q.get("net_income")), _src(facts.get("net_income"))),
            ("Free cash flow", _fmt_number(q.get("fcf")), q_fcf_source),
            ("Total assets", _fmt_number(q.get("total_assets")), _src(facts.get("total_assets"))),
            ("Total equity", _fmt_number(q.get("total_equity")), _src(facts.get("total_equity"))),
            ("Total debt", _fmt_number(q.get("total_debt")), q_debt_source),
            ("Liquid assets (cash + securities)", _fmt_number(q.get("liquid_assets")), q_liquid_source),
            ("Cash", _fmt_number(q.get("cash")), _src(facts.get("cash"))),
        ]
        q_rows = "".join(_row(label, value, source) for label, value, source in q_items)
        q_margin_rows = "".join(
            f"<tr><td>{escape(label)}</td><td>{_fmt_pct(q['margins'][key].value) if q['margins'][key].value is not None else 'n/a'}</td></tr>"
            for label, key in [("Operating margin", "operating_margin"), ("Net margin", "net_margin"), ("FCF margin", "fcf_margin")]
        )
        quarter_html = _section(
            "Most Recent Quarter (10-Q)",
            f"<p>Quarter ended {escape(q['period_end'])} · filed {escape(q['filed'])}</p>"
            f"<table><thead><tr><th>Item</th><th>Value</th><th>Source</th></tr></thead><tbody>{q_rows}</tbody></table>"
            f"<p><strong>Quarter margins</strong></p>"
            f"<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>{q_margin_rows}</tbody></table>"
        )

    growth_rows = []
    for label, g in res.growth.items():
        def cell(y):
            m = g.get(y)
            if not m or m.value is None:
                return "n/a"
            return _fmt_pct(m.value) + (" *" if m.note else "")
        growth_rows.append(
            f"<tr><td>{escape(label)}</td><td>{cell(5)}</td><td>{cell(10)}</td><td>{cell(15)}</td></tr>"
        )
    growth_html = (
        "<table><thead><tr><th>Metric</th><th>5y</th><th>10y</th><th>15y</th></tr></thead>"
        f"<tbody>{''.join(growth_rows)}</tbody></table>"
    )

    ratio_rows = []
    order = [
        ("Gross margin", "gross_margin", True), ("Operating margin", "operating_margin", True),
        ("Net margin", "net_margin", True), ("FCF margin", "fcf_margin", True),
        ("ROE", "roe", True), ("ROA", "roa", True), ("ROIC", "roic", True), ("ROCE", "roce", True),
        ("Current ratio", "current_ratio", False), ("Debt/Equity", "debt_to_equity", False),
        ("Interest coverage", "interest_coverage", False), ("FCF conversion", "fcf_conversion", False),
    ]
    for label, key, is_pct in order:
        m = res.ratios.get(key)
        if m is None:
            val = "n/a"
        elif m.value is None:
            val = f"n/a ({m.note})" if m.note else "n/a"
        else:
            val = _fmt_pct(m.value) if is_pct else f"{m.value:.2f}"
        ratio_rows.append(f"<tr><td>{escape(label)}</td><td>{escape(val)}</td></tr>")
    ratios_html = (
        "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead>"
        f"<tbody>{''.join(ratio_rows)}</tbody></table>"
    )

    peer_html = ""
    if peer_table:
        rows = []
        for rs in peer_table:
            pct = f"{rs.percentile:.0f}" if rs.percentile is not None else "n/a"
            def f(x, p=False):
                if x is None:
                    return "n/a"
                return _fmt_pct(x) if p else f"{x:.2f}"
            rows.append(
                "<tr>"
                f"<td>{escape(rs.metric)}</td>"
                f"<td>{escape(f(rs.target))}</td>"
                f"<td>{escape(f(rs.peer_median))}</td>"
                f"<td>{escape(pct)}</td>"
                f"<td>{escape(f(rs.zscore))}</td>"
                f"<td>{rs.n_peers}</td>"
                "</tr>"
            )
        peer_html = (
            "<h3>Peer-relative comparison</h3>"
            "<table><thead><tr><th>Metric</th><th>Target</th><th>Peer median</th>"
            "<th>Percentile</th><th>z-score</th><th>n</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    valuation_html = ""
    rv = res.rel_val
    rel_html = ""
    if rv:
        pe = f"{rv.pe:.1f}" if rv.pe is not None else "n/a"
        ev = f"{rv.ev_ebitda:.1f}" if rv.ev_ebitda is not None else "n/a"
        rel_html = (
            "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead>"
            f"<tbody><tr><td>P/E</td><td>{pe}</td></tr>"
            f"<tr><td>EV/EBITDA</td><td>{ev}</td></tr>"
            f"<tr><td>FCF yield</td><td>{_fmt_pct(rv.fcf_yield)}</td></tr></tbody></table>"
        )
    dcf_html = ""
    if res.dcf:
        rows = []
        for name, d in res.dcf.items():
            a = d.assumptions
            fv = _fmt_currency(d.fair_value_per_share)
            up = _fmt_pct(d.upside_vs_price) if d.upside_vs_price is not None else "n/a"
            rows.append(
                "<tr>"
                f"<td>{escape(name)}</td><td>{a['wacc']:.1%}</td><td>{a['terminal_growth']:.1%}</td>"
                f"<td>{fv}</td><td>{up}</td>"
                "</tr>"
            )
            for warn in d.warnings:
                rows.append(f"<tr><td colspan=5>⚠ {escape(warn)}</td></tr>")
        dcf_html = (
            "<p><strong>DCF (assumptions are yours, from config.yaml):</strong></p>"
            "<table><thead><tr><th>Scenario</th><th>WACC</th><th>Term. g</th>"
            "<th>Fair value/share</th><th>Upside vs price</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
        if res.sensitivity:
            gs = sorted(next(iter(res.sensitivity.values())).keys())
            header = "<tr><th>WACC \\ g</th>" + "".join(f"<th>{g:.1%}</th>" for g in gs) + "</tr>"
            body = ""
            for wv in sorted(res.sensitivity.keys()):
                cells = "".join(
                    f"<td>{_fmt_currency(res.sensitivity[wv][g])}</td>"
                    for g in gs
                )
                body += f"<tr><td>{wv:.1%}</td>{cells}</tr>"
            dcf_html += (
                "<p><strong>Sensitivity — base scenario fair value/share (rows=WACC, cols=terminal g):</strong></p>"
                f"<table><thead>{header}</thead><tbody>{body}</tbody></table>"
            )
    if rel_html or dcf_html:
        valuation_html = f"{rel_html}{dcf_html}"

    gaps_html = ""
    if res.gaps:
        gaps_items = "".join(f"<li>{escape(g)}</li>" for g in res.gaps)
        gaps_html = f"<p>The following could not be resolved from EDGAR and were excluded from the analysis (do not treat absence as zero):</p><ul>{gaps_items}</ul>"
    else:
        gaps_html = "<p>- None — all targeted concepts resolved.</p>"

    html = f"""
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
<div class="verdict" style="border-color: {_verdict_color(res.dcf.get('base').upside_vs_price if res.dcf and res.dcf.get('base') else None)}; color: {_verdict_color(res.dcf.get('base').upside_vs_price if res.dcf and res.dcf.get('base') else None)};">
<strong>{escape(summary_text)}</strong>
</div>
</header>
{_section('Financial position (latest FY)', position_html)}
{quarter_html}
{_section('Growth (CAGR)', growth_html)}
{_section('Margins, returns, leverage', ratios_html)}
{peer_html}
{_section('Valuation', valuation_html)}
{_section('Data gaps / not verified', gaps_html)}
<div class="footer">Fundamentals: SEC EDGAR XBRL companyfacts (primary source). Prices/multiples: see price source above. This report is a research input, not investment advice.</div>
</main>
</body>
</html>
"""
    return html
