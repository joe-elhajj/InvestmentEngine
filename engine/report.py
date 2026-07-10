"""
report.py — render an AnalysisResult to a Markdown report.

The report is built so a reader can trust it without re-deriving it:
  - every fundamental figure cites its EDGAR concept + period + form
  - multiples cite the market data source + as-of
  - a "Data gaps" section lists everything that could NOT be resolved, loudly,
    so missing data never masquerades as a clean result
This same structure becomes the evidence package handed to the council later.
"""

from __future__ import annotations

from datetime import datetime

from engine.edgar import Fact, classify_rnd_series
from engine.pipeline import AnalysisResult, RndRegime, rnd_regime_reason_text

# Short cell text for an ABSTAINED rnd_regime, paired with the full
# rationale from rnd_regime_reason_text() (pipeline.py) as a footnote
# beneath the ratio table -- the full IAS 38 rationale (~180 chars) blew
# out the table's width inline (live-verified on ASML). Renderer-owned,
# not pipeline.py's concern: pipeline.py owns WHY the regime doesn't
# apply, not how a Markdown table decides to lay that reason out.
_RND_REGIME_REASON_SHORT: dict[RndRegime, str] = {
    RndRegime.ABSTAINED_REGIME_DISABLED: "regime disabled",
    RndRegime.ABSTAINED_IFRS_FPI: "IFRS filer",
}


def _pct(x, nd=1):
    return f"{x*100:.{nd}f}%" if x is not None else "n/a"


def _num(x):
    if x is None:
        return "n/a"
    ax = abs(x)
    if ax >= 1e9:
        return f"${x/1e9:,.2f}B"
    if ax >= 1e6:
        return f"${x/1e6:,.1f}M"
    return f"${x:,.0f}"


def _ratio(m):
    if m is None or m.value is None:
        note = f" ({m.note})" if (m is not None and m.note) else ""
        return f"n/a{note}"
    return f"{m.value:.2f}"


def _derived_source(expression: str, inputs: list[tuple[str, object]]) -> str:
    parts = []
    for label, fact in inputs:
        if fact:
            parts.append(f"{label}: {fact.source()}")
        else:
            parts.append(f"{label}: n/a")
    return f"derived: {expression} | " + " | ".join(parts)


def render(res: AnalysisResult, peer_table: list | None = None, ds_gaps: list | None = None) -> str:
    cd = res.company
    q = res.quote
    out: list[str] = []
    w = out.append

    w(f"# {cd.name} ({cd.ticker}) — Fundamental Analysis")
    w(f"*Generated {datetime.now():%Y-%m-%d %H:%M} · CIK {cd.cik} · "
      f"SIC {cd.sic} {cd.sic_description}*")
    w("")
    w(f"**Price:** {('$%.2f' % q.price) if q.price else 'n/a'} "
      f"· **Market cap:** {_num(q.market_cap)} · *price source: {q.source}*")
    w("")

    # --- Financial position -------------------------------------------------
    w("## Financial position (latest FY)")
    d = res.derived
    fcf_src = _derived_source("FCF = CFO - capex", [
        ("CFO", cd.latest("cfo")), ("capex", cd.latest("capex"))])
    debt_src = _derived_source("total debt = long_term_debt + short_term_debt", [
        ("long_term_debt", cd.latest("long_term_debt")),
        ("short_term_debt", cd.latest("short_term_debt"))])
    liquid_src = _derived_source("liquid assets = cash + short_term_investments + long_term_investments", [
        ("cash", cd.latest("cash")),
        ("short_term_investments", cd.latest("short_term_investments")),
        ("long_term_investments", cd.latest("long_term_investments")),
    ])
    rows = [
        ("Revenue", _num(d["revenue"]), cd.latest("revenue")),
        ("Operating income", _num(d["operating_income"]), cd.latest("operating_income")),
        ("Net income", _num(d["net_income"]), cd.latest("net_income")),
        ("Free cash flow", _num(d["fcf"]), fcf_src),
        ("Total assets", _num(d["total_assets"]), cd.latest("total_assets")),
        ("Total equity", _num(d["total_equity"]), cd.latest("total_equity")),
        ("Total debt", _num(d["total_debt"]), debt_src),
        ("Liquid assets (cash + securities)", _num(d["liquid_assets"]), liquid_src),
        ("Cash", _num(d["cash"]), cd.latest("cash")),
    ]
    w("| Item | Value | Source |")
    w("|---|---|---|")
    for label, val, fact in rows:
        if isinstance(fact, str):
            src = fact
        else:
            src = fact.source() if fact else "derived"
        w(f"| {label} | {val} | {src} |")
    w("")

    if res.latest_quarter:
        q = res.latest_quarter
        f = q["facts"]
        q_fcf_src = _derived_source("FCF = CFO - capex", [
            ("CFO", f.get("cfo")), ("capex", f.get("capex"))])
        q_debt_src = _derived_source("total debt = long_term_debt + short_term_debt", [
            ("long_term_debt", f.get("long_term_debt")),
            ("short_term_debt", f.get("short_term_debt"))])
        q_liquid_src = _derived_source("liquid assets = cash + short_term_investments + long_term_investments", [
            ("cash", f.get("cash")),
            ("short_term_investments", f.get("short_term_investments")),
            ("long_term_investments", f.get("long_term_investments")),
        ])
        w("## Most recent quarter (10-Q)")
        w(f"*Quarter ended {q['period_end']} · filed {q['filed']}*")
        w("")
        q_rows = [
            ("Revenue", _num(q["revenue"]), f.get("revenue")),
            ("Operating income", _num(q["operating_income"]), f.get("operating_income")),
            ("Net income", _num(q["net_income"]), f.get("net_income")),
            ("Free cash flow", _num(q["fcf"]), q_fcf_src),
            ("Total assets", _num(q["total_assets"]), f.get("total_assets")),
            ("Total equity", _num(q["total_equity"]), f.get("total_equity")),
            ("Total debt", _num(q["total_debt"]), q_debt_src),
            ("Liquid assets (cash + securities)", _num(q["liquid_assets"]), q_liquid_src),
            ("Cash", _num(q["cash"]), f.get("cash")),
        ]
        w("| Item | Value | Source |")
        w("|---|---|---|")
        for label, val, fact in q_rows:
            src = fact.source() if isinstance(fact, Fact) else fact if isinstance(fact, str) else "derived"
            w(f"| {label} | {val} | {src} |")
        w("")
        w("### Quarter margins")
        qm = q["margins"]
        w("| Metric | Value |")
        w("|---|---|")
        for key in ("operating_margin", "net_margin", "fcf_margin"):
            metric = qm.get(key)
            if metric is None or metric.value is None:
                val = f"n/a ({metric.note})" if metric and metric.note else "n/a"
            else:
                val = _pct(metric.value)
            w(f"| {key.replace('_', ' ').title()} | {val} |")
        w("")

    # --- Growth -------------------------------------------------------------
    w("## Growth (CAGR)")
    w("| Metric | 5y | 10y | 15y |")
    w("|---|---|---|---|")
    for label, g in res.growth.items():
        def cell(y):
            m = g.get(y)
            if not m or m.value is None:
                return "n/a"
            star = " *" if m.note else ""
            return _pct(m.value) + star
        w(f"| {label} | {cell(5)} | {cell(10)} | {cell(15)} |")
    w("\n*\\* non-standard window (too little or too much history for the "
      "requested horizon) — see the underlying metric's note.*")
    w("")

    # --- Margins & returns --------------------------------------------------
    w("## Margins, returns, leverage")
    r = res.ratios
    order = [
        ("Gross margin", "gross_margin", True), ("Operating margin", "operating_margin", True),
        ("Net margin", "net_margin", True), ("FCF margin", "fcf_margin", True),
        ("ROE", "roe", True), ("ROA", "roa", True), ("ROIC", "roic", True), ("ROCE", "roce", True),
        ("Current ratio", "current_ratio", False), ("Debt/Equity", "debt_to_equity", False),
        ("Interest coverage", "interest_coverage", False), ("FCF conversion", "fcf_conversion", False),
    ]
    w("| Metric | Value |")
    w("|---|---|")
    rnd_unadj_footnote: str | None = None
    for label, key, is_pct in order:
        m = r.get(key)
        if m is None:
            val = "n/a"
        elif m.value is None:
            val = f"n/a ({m.note})" if m.note else "n/a"
        else:
            val = _pct(m.value) if is_pct else f"{m.value:.2f}"
        w(f"| {label} | {val} |")
        if key == "roic":
            # Dual ROIC (Damodaran R&D capitalization). Two layers compose
            # here, in precedence order: (1) no R&D series at all -- nothing
            # was ever adjustable, so no row, not a badge, exactly like a
            # legitimate NO_RND company always got; (2) res.rnd_regime
            # (stamped once by pipeline.derive(), see engine/pipeline.py) is
            # ABSTAINED -- regime disabled in config, or an IFRS filer -- so
            # a badge with THAT reason, regardless of whether roic_adjusted
            # happened to compute; (3) roic_adjusted didn't compute for some
            # other reason (e.g. a short/gapped window) -- a badge saying so;
            # (4) otherwise, the bare percentage. The ABSTAINED case prints
            # the SHORT reason inline and stashes the full IAS 38 rationale
            # for a footnote below the table -- the full text is too wide
            # for a table cell (~180 chars, blew out the table on ASML).
            adj = r.get("roic_adjusted")
            state, _ = classify_rnd_series(res.company)
            if state == "no_rnd":
                pass
            elif res.rnd_regime is None:
                # Loud, not a badge: an unstamped AnalysisResult means we were
                # never told this filer's FPI/regime status, so we cannot
                # safely choose between a badge and a bare percentage --
                # guessing risks reproducing F-14 itself (a real percentage
                # shown for a company that should have abstained).
                raise ValueError(
                    "AnalysisResult.rnd_regime is None but roic_adjusted has a decision to "
                    "render -- call pipeline.derive() (which stamps rnd_regime) rather than "
                    "constructing AnalysisResult directly when R&D data is present"
                )
            elif res.rnd_regime is not RndRegime.APPLIES:
                short = _RND_REGIME_REASON_SHORT[res.rnd_regime]
                w(f"| ROIC (R&D-adj) | n/a — **R&D-UNADJ** ({short}) |")
                rnd_unadj_footnote = rnd_regime_reason_text(res.rnd_regime)
            elif adj is None or adj.value is None:
                w("| ROIC (R&D-adj) | n/a — **R&D-UNADJ** (insufficient history) |")
            else:
                w(f"| ROIC (R&D-adj) | {_pct(adj.value)} |")
    w("")
    if rnd_unadj_footnote:
        w(f"*R&D-UNADJ: {rnd_unadj_footnote}*")
        w("")

    # --- Peer comparison ----------------------------------------------------
    if peer_table:
        w("## Peer-relative comparison")
        w("*Comp set built by SIC family + size band; see config and lineage below.*")
        w("| Metric | Target | Peer median | Percentile | z-score | n |")
        w("|---|---|---|---|---|---|")
        for rs in peer_table:
            def f(x, p=False):
                if x is None:
                    return "n/a"
                return _pct(x) if p else f"{x:.2f}"
            pct = f"{rs.percentile:.0f}" if rs.percentile is not None else "n/a"
            w(f"| {rs.metric} | {f(rs.target)} | {f(rs.peer_median)} | "
              f"{pct} | {f(rs.zscore)} | {rs.n_peers} |")
        w("")

    # --- Valuation ----------------------------------------------------------
    w("## Valuation")
    rv = res.rel_val
    if rv:
        pe = f"{rv.pe:.1f}" if rv.pe is not None else "n/a"
        ev = f"{rv.ev_ebitda:.1f}" if rv.ev_ebitda is not None else "n/a"
        w("**Relative (trailing):**")
        w("| Metric | Value |")
        w("|---|---|")
        w(f"| P/E | {pe} |")
        w(f"| EV/EBITDA | {ev} |")
        w(f"| FCF yield | {_pct(rv.fcf_yield)} |")
        w("")
    if res.dcf:
        w("**DCF (assumptions are yours, from config.yaml):**")
        w("| Scenario | WACC | Term. g | Fair value/share | Upside vs price |")
        w("|---|---|---|---|---|")
        for name, d in res.dcf.items():
            a = d.assumptions
            fv = f"${d.fair_value_per_share:,.2f}" if d.fair_value_per_share else "n/a"
            up = _pct(d.upside_vs_price) if d.upside_vs_price is not None else "n/a"
            w(f"| {name} | {a['wacc']:.1%} | {a['terminal_growth']:.1%} | {fv} | {up} |")
            for warn in d.warnings:
                w(f"|  | ⚠ {warn} | | | |")
        w("")
        if res.sensitivity:
            w("**Sensitivity — base scenario fair value/share (rows=WACC, cols=terminal g):**")
            gs = sorted(next(iter(res.sensitivity.values())).keys())
            w("| WACC \\ g | " + " | ".join(f"{g:.1%}" for g in gs) + " |")
            w("|" + "---|" * (len(gs) + 1))
            for wv in sorted(res.sensitivity.keys()):
                cells = []
                for g in gs:
                    fv = res.sensitivity[wv][g]
                    cells.append(f"${fv:,.0f}" if fv else "n/a")
                w(f"| {wv:.1%} | " + " | ".join(cells) + " |")
            w("")

    # --- Expectations gap band (PR 3) ---------------------------------------
    # Additive alongside the forward DCF table above: the SAME reverse-DCF
    # solved under all three owned scenario bundles, rather than base alone.
    # None (no section at all) when NO_BAND -- base failed to converge,
    # delivered_growth is unavailable, or a bundle isn't configured -- so a
    # ticker with no band renders byte-identical to before this PR.
    band = res.expectations_gap_band
    if band is not None:
        w("**Expectations gap — bull/base/bear band:**")
        w(f"Base case: {_pct(band.base_gap, nd=1)} "
          f"(implied {_pct(band.scenarios['base'].implied_growth)} vs "
          f"delivered {_pct(band.delivered_growth)})")
        w("")
        w("| Scenario | WACC | Term. g | Implied growth | Delivered growth | Gap |")
        w("|---|---|---|---|---|---|")
        for name in ("bull", "base", "bear"):
            sc = band.scenarios[name]
            ig = _pct(sc.implied_growth)
            if not sc.converged:
                ig += f" (bracket {sc.bracket_bound}, not converged)"
            gap = _pct(sc.gap)
            if not sc.converged:
                gap += " *"
            w(f"| {name} | {_pct(sc.wacc)} | {_pct(sc.terminal_growth)} | {ig} | "
              f"{_pct(band.delivered_growth)} | {gap} |")
        if band.band_status == "PARTIAL":
            failed = [n for n in ("bull", "base", "bear") if not band.scenarios[n].converged]
            w("")
            w(f"_\\* {', '.join(failed)} did not converge (bisection bracket exceeded) — "
              "band incomplete; the gap shown for that scenario is the clamped bracket "
              "bound, not a real solve._")
            w("_Fragility: UNDETERMINABLE — a band that can't be fully solved cannot be "
              "assessed for scenario-dependence._")
        elif band.fragile == "FRAGILE":
            w("")
            w("_FRAGILE: the sign of the expectations gap differs across scenarios — "
              "this signal's direction is not robust to the WACC/terminal-growth "
              "assumption chosen; treat the base-case number with caution._")
        w("")

    # --- Data gaps ------------------------------------------------------------
    # ONE shared "Data gaps" section, not two: res.gaps (pipeline, absence-is-
    # not-zero) and ds_gaps (DurabilityScore.gaps -- scoring-level disclosures
    # like the net-cash resilience note, mixed-basis, short-history) merge
    # here with a [DUR] provenance marker on the durability ones, so a
    # durability disclosure that fires is finally visible somewhere rather
    # than existing only in the computed DurabilityScore. ds_gaps defaults to
    # None (additive parameter) -- any caller not yet passing it renders
    # exactly as before this change.
    w("## Data gaps / not verified")
    all_gaps = list(res.gaps) + [f"[DUR] {g}" for g in (ds_gaps or [])]
    if all_gaps:
        legend = (
            " [DUR]-marked entries are durability-scoring disclosures, "
            "not pipeline data gaps."
            if ds_gaps else ""
        )
        w("The following could not be resolved from EDGAR and were excluded "
          f"from the analysis (do not treat absence as zero).{legend}")
        for g in all_gaps:
            w(f"- {g}")
    else:
        w("- None — all targeted concepts resolved.")
    w("")
    w("---")
    w("*Fundamentals: SEC EDGAR XBRL companyfacts (primary source). "
      "Prices/multiples: see price source above. "
      "This report is a research input, not investment advice or a decision.*")
    return "\n".join(out)
