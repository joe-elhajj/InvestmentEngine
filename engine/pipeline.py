"""
pipeline.py — orchestration that turns raw EDGAR data into a structured result.

This is the deterministic spine: EDGAR -> derived metrics -> peers -> valuation.
No LLM anywhere in here. The model only enters in Phase 2 (red-flag extraction
from filing text and the council), and it will operate on the *output* of this
pipeline, never on raw "analyze TICKER" guesswork.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine import metrics as M
from engine.edgar import CompanyData
from engine.market import Quote
from engine import valuation as V


@dataclass
class AnalysisResult:
    company: CompanyData
    quote: Quote
    derived: dict = field(default_factory=dict)       # latest single-value metrics
    growth: dict = field(default_factory=dict)        # CAGR metrics
    ratios: dict = field(default_factory=dict)        # M.Metric objects
    rel_val: Optional[V.RelativeValuation] = None
    dcf: dict = field(default_factory=dict)           # scenario -> DCFResult
    sensitivity: dict = field(default_factory=dict)
    gaps: list = field(default_factory=list)
    latest_quarter: dict = field(default_factory=dict)
    derived_lineage: dict = field(default_factory=dict)  # key -> lineage string for computed values


def _series(cd: CompanyData, key: str) -> list[tuple[float, float]]:
    return [(f.fiscal_year, f.value) for f in cd.series.get(key, [])]


def _v(cd: CompanyData, key: str) -> Optional[float]:
    return cd.latest_value(key)


def derive(cd: CompanyData, quote: Quote, config: dict) -> AnalysisResult:
    res = AnalysisResult(company=cd, quote=quote)
    res.gaps = list(cd.unresolved)

    rev = _v(cd, "revenue")
    ni = _v(cd, "net_income")
    oi = _v(cd, "operating_income")
    gp = _v(cd, "gross_profit")
    cfo = _v(cd, "cfo")
    capex = _v(cd, "capex")
    da = _v(cd, "dep_amort")
    assets = _v(cd, "total_assets")
    # equity: resolved below after anchor_period is known (period-matched)
    cur_assets = _v(cd, "current_assets")
    cur_liab = _v(cd, "current_liabilities")
    interest = _v(cd, "interest_expense")

    # Determine anchor period for balance-sheet figures (latest total_assets)
    assets_fact = cd.latest("total_assets")
    anchor_period = assets_fact.period_end if assets_fact else None

    # For balance-sheet multi-component figures, enforce period consistency
    # All components must be from the same period_end to avoid mixing dates.
    liquid_assets = None
    cash_for_invested = 0.0
    if anchor_period:
        cash_fact = cd.value_for_period("cash", anchor_period)
        sti_fact = cd.value_for_period("short_term_investments", anchor_period)
        lti_fact = cd.value_for_period("long_term_investments", anchor_period)

        cash_val = cash_fact.value if cash_fact else 0.0
        sti_val = sti_fact.value if sti_fact else 0.0
        lti_val = lti_fact.value if lti_fact else 0.0

        # Track missing period matches in gaps
        if not cash_fact and cd.latest("cash"):
            cash_latest = cd.latest("cash")
            res.gaps.append(f"cash: no value for period {anchor_period} (latest available is {cash_latest.period_end}, not used)")
        if not sti_fact and cd.latest("short_term_investments"):
            sti_latest = cd.latest("short_term_investments")
            res.gaps.append(f"short_term_investments: no value for period {anchor_period} (latest available is {sti_latest.period_end}, not used)")
        if not lti_fact and cd.latest("long_term_investments"):
            lti_latest = cd.latest("long_term_investments")
            res.gaps.append(f"long_term_investments: no value for period {anchor_period} (latest available is {lti_latest.period_end}, not used)")

        liquid_assets = cash_val + sti_val + lti_val

        # cash_for_invested is the plain `cash` concept (period-matched)
        cash_for_invested = cash_val

    # For total_debt, enforce same period consistency.
    # When anchor_period is absent, total_debt is absent — never 0.0.
    total_debt = None
    if anchor_period:
        ltd_fact = cd.value_for_period("long_term_debt", anchor_period)
        std_fact = cd.value_for_period("short_term_debt", anchor_period)

        ltd_val = ltd_fact.value if ltd_fact else 0.0
        std_val = std_fact.value if std_fact else 0.0

        # Track missing period matches in gaps
        if not ltd_fact and cd.latest("long_term_debt"):
            ltd_latest = cd.latest("long_term_debt")
            res.gaps.append(f"long_term_debt: no value for period {anchor_period} (latest available is {ltd_latest.period_end}, not used)")
        if not std_fact and cd.latest("short_term_debt"):
            std_latest = cd.latest("short_term_debt")
            res.gaps.append(f"short_term_debt: no value for period {anchor_period} (latest available is {std_latest.period_end}, not used)")

        total_debt = ltd_val + std_val
    else:
        res.gaps.append("total_debt: no anchor period (total_assets absent); treated as absent, not zero")

    # Period-match total_equity to anchor_period — same gap-logging pattern as cash/debt.
    if anchor_period:
        equity_fact = cd.value_for_period("total_equity", anchor_period)
        equity = equity_fact.value if equity_fact else None
        if not equity_fact and cd.latest("total_equity"):
            eq_latest = cd.latest("total_equity")
            res.gaps.append(f"total_equity: no value for period {anchor_period} (latest available is {eq_latest.period_end}, not used)")
    else:
        equity = None

    # Gross profit fallback: revenue - cost_of_revenue (period-matched) when GrossProfit unresolved.
    if gp is None and anchor_period:
        rev_fact_ap = cd.value_for_period("revenue", anchor_period)
        cor_fact_ap = cd.value_for_period("cost_of_revenue", anchor_period)
        if rev_fact_ap is not None and cor_fact_ap is not None:
            gp = rev_fact_ap.value - cor_fact_ap.value
            res.derived_lineage["gross_profit"] = (
                f"derived: revenue({rev_fact_ap.concept}) - cost_of_revenue({cor_fact_ap.concept})"
            )

    # net_debt: only defined when both components are present
    net_debt = (total_debt - (liquid_assets or 0.0)) if (liquid_assets is not None and total_debt is not None) else None
    ebit = oi
    ebitda = (oi + da) if (oi is not None and da is not None) else None

    # Ensure invested_capital uses the plain `cash` concept only (period-matched when anchor exists,
    # otherwise fall back to latest cash). This prevents accidental use of `liquid_assets`.
    if anchor_period:
        cash_for_invested_val = cash_for_invested
    else:
        cash_for_invested_val = _v(cd, "cash")

    invested_capital = (total_debt + equity - (cash_for_invested_val or 0.0)) if (equity is not None and total_debt is not None and cash_for_invested_val is not None) else None
    capital_employed = (assets - (cur_liab or 0.0)) if assets is not None else None
    # rough NOPAT using a flat tax assumption from config (owned assumption, not guessed silently)
    tax_rate = config.get("valuation", {}).get("assumed_tax_rate", 0.21)
    nopat = ebit * (1 - tax_rate) if ebit is not None else None

    # Get latest cash and other balances for display purposes
    cash = _v(cd, "cash")

    # Compute FCF early since it's used in ratios
    fcf = (cfo - capex) if (cfo is not None and capex is not None) else None

    res.derived = {
        "revenue": rev, "net_income": ni, "operating_income": oi, "gross_profit": gp,
        "fcf": fcf,
        "ebitda": ebitda, "total_debt": total_debt,
        "net_debt": net_debt, "liquid_assets": liquid_assets,
        "cash": cash, "total_equity": equity, "total_assets": assets,
    }

    if cd.quarterly:
        def _qv(key: str) -> Optional[float]:
            fact = cd.quarterly.get(key)
            return fact.value if fact else None
        q_revenue = _qv("revenue")
        q_net_income = _qv("net_income")
        q_operating_income = _qv("operating_income")
        q_cfo = _qv("cfo")
        q_capex = _qv("capex")
        q_ebit = q_operating_income
        q_fcf = (q_cfo - q_capex) if (q_cfo is not None and q_capex is not None) else None
        q_ltd = _qv("long_term_debt") or 0.0
        q_std = _qv("short_term_debt") or 0.0
        q_cash = _qv("cash") or 0.0
        q_sti = _qv("short_term_investments") or 0.0
        q_lti = _qv("long_term_investments") or 0.0
        q_total_assets = _qv("total_assets")
        q_total_equity = _qv("total_equity")
        q_total_debt = q_ltd + q_std
        q_liquid_assets = q_cash + q_sti + q_lti
        q_period_end = max(f.period_end for f in cd.quarterly.values())
        q_filed = max(f.filed for f in cd.quarterly.values())

        res.latest_quarter = {
            "period_end": q_period_end,
            "filed": q_filed,
            "revenue": q_revenue,
            "net_income": q_net_income,
            "operating_income": q_operating_income,
            "fcf": q_fcf,
            "total_assets": q_total_assets,
            "total_equity": q_total_equity,
            "total_debt": q_total_debt,
            "cash": q_cash,
            "liquid_assets": q_liquid_assets,
            "margins": {
                "operating_margin": M.operating_margin(q_operating_income, q_revenue),
                "net_margin": M.net_margin(q_net_income, q_revenue),
                "fcf_margin": M.fcf_margin(q_fcf, q_revenue),
            },
            "facts": cd.quarterly,
        }
    else:
        res.latest_quarter = {}

    # growth (CAGR) on revenue, net income, FCF
    for label, key in (("revenue", "revenue"), ("net_income", "net_income")):
        s = _series(cd, key)
        res.growth[label] = {y: M.cagr_over(s, y) for y in (5, 10, 15)}

    # margins + returns + leverage
    r = res.ratios
    r["gross_margin"] = M.gross_margin(gp, rev)
    r["operating_margin"] = M.operating_margin(oi, rev)
    r["net_margin"] = M.net_margin(ni, rev)
    r["fcf_margin"] = M.fcf_margin(fcf, rev)
    r["roe"] = M.roe(ni, equity)
    r["roa"] = M.roa(ni, assets)
    r["roic"] = M.roic(nopat, invested_capital)
    r["roce"] = M.roce(ebit, capital_employed)
    r["current_ratio"] = M.current_ratio(cur_assets, cur_liab)
    r["debt_to_equity"] = M.debt_to_equity(total_debt, equity)
    r["interest_coverage"] = M.interest_coverage(ebit, interest)
    r["fcf_conversion"] = M.fcf_conversion(fcf, ni)

    # relative valuation multiples (need price)
    price = quote.price
    mc = quote.market_cap
    shares = quote.shares_outstanding
    eps = (ni / shares) if (ni is not None and shares) else None
    # EV requires net_debt; skip and log gap when unavailable
    ev = (mc + net_debt) if (mc is not None and net_debt is not None) else None
    if mc is not None and net_debt is None:
        res.gaps.append("ev: net_debt unavailable; EV/EBITDA excluded")
    res.rel_val = V.RelativeValuation(
        pe=M.pe_ratio(price, eps).value,
        ev_ebitda=M.ev_ebitda(ev, ebitda).value,
        fcf_yield=M.fcf_yield(fcf, mc).value,
    )

    # DCF (only if we have a base FCF and net_debt is known)
    dcf_cfg = config.get("valuation", {}).get("dcf", {})
    if fcf is not None and fcf > 0 and dcf_cfg:
        if net_debt is None:
            res.gaps.append("dcf: net_debt unavailable; DCF skipped")
        else:
            common = {"projection_years": dcf_cfg.get("projection_years", 5)}
            for name, sc in dcf_cfg.get("scenarios", {}).items():
                a = dict(common)
                a.update(sc)
                res.dcf[name] = V.two_stage_dcf(fcf, net_debt, shares, price, name, a)
            sens = dcf_cfg.get("sensitivity")
            base = dcf_cfg.get("scenarios", {}).get("base")
            if sens and base:
                a = dict(common); a.update(base)
                res.sensitivity = V.sensitivity_grid(
                    fcf, net_debt, shares, a, sens.get("wacc", []), sens.get("terminal_growth", []))
    elif not dcf_cfg:
        res.gaps.append("dcf: no assumptions configured")
    else:
        res.gaps.append("dcf: base FCF unavailable or non-positive")

    return res
