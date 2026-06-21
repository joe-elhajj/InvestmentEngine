"""
valuation.py — relative and absolute valuation.

Two hard rules:
  1. Assumptions are owned by you, in config.yaml, never invented at runtime.
     The DCF prints exactly which assumptions produced each number.
  2. A DCF on a long-duration grower is mostly terminal value and therefore
     mostly assumption. So we always run bull/base/bear and a sensitivity grid
     over WACC x terminal growth, to show the answer is a *range*, not a point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DCFResult:
    scenario: str
    assumptions: dict
    pv_explicit: float
    pv_terminal: float
    enterprise_value: float
    equity_value: float
    fair_value_per_share: Optional[float]
    upside_vs_price: Optional[float]   # fraction, e.g. 0.15 = +15%
    warnings: list = field(default_factory=list)


def two_stage_dcf(
    base_fcf: float,
    net_debt: float,
    shares: Optional[float],
    current_price: Optional[float],
    scenario_name: str,
    assumptions: dict,
) -> DCFResult:
    """
    base_fcf      : starting free cash flow (latest annual FCF, or a normalized value)
    assumptions   : {
        projection_years, terminal_growth, wacc,
        fcf_growth: list[float] (one per projection year) OR single float
    }
    """
    warnings: list[str] = []
    n = int(assumptions["projection_years"])
    wacc = float(assumptions["wacc"])
    g_term = float(assumptions["terminal_growth"])
    growth = assumptions["fcf_growth"]
    if isinstance(growth, (int, float)):
        growth = [float(growth)] * n
    if len(growth) < n:
        growth = list(growth) + [growth[-1]] * (n - len(growth))

    if wacc <= g_term:
        warnings.append(f"WACC ({wacc:.1%}) <= terminal growth ({g_term:.1%}); terminal value invalid")

    # explicit FCF projection
    fcf = base_fcf
    pv_explicit = 0.0
    last_fcf = base_fcf
    for t in range(1, n + 1):
        fcf = fcf * (1.0 + growth[t - 1])
        pv_explicit += fcf / ((1.0 + wacc) ** t)
        last_fcf = fcf

    # terminal value via Gordon growth on year-N FCF
    if wacc > g_term:
        terminal_value = last_fcf * (1.0 + g_term) / (wacc - g_term)
        pv_terminal = terminal_value / ((1.0 + wacc) ** n)
    else:
        pv_terminal = 0.0

    ev = pv_explicit + pv_terminal
    equity_value = ev - net_debt
    fv_per_share = equity_value / shares if shares else None
    upside = None
    if fv_per_share is not None and current_price:
        upside = fv_per_share / current_price - 1.0

    return DCFResult(
        scenario=scenario_name,
        assumptions={"projection_years": n, "wacc": wacc,
                     "terminal_growth": g_term, "fcf_growth": growth},
        pv_explicit=pv_explicit, pv_terminal=pv_terminal,
        enterprise_value=ev, equity_value=equity_value,
        fair_value_per_share=fv_per_share, upside_vs_price=upside,
        warnings=warnings,
    )


def sensitivity_grid(base_fcf, net_debt, shares, base_assumptions,
                     wacc_range: list[float], g_range: list[float]) -> dict:
    """Returns {wacc: {g: fair_value_per_share}} for the base scenario."""
    grid: dict = {}
    for w in wacc_range:
        grid[w] = {}
        for g in g_range:
            a = dict(base_assumptions)
            a["wacc"], a["terminal_growth"] = w, g
            res = two_stage_dcf(base_fcf, net_debt, shares, None, "sensitivity", a)
            grid[w][g] = res.fair_value_per_share
    return grid


@dataclass
class RelativeValuation:
    pe: Optional[float]
    ev_ebitda: Optional[float]
    fcf_yield: Optional[float]
    peer_pe_median: Optional[float] = None
    peer_ev_ebitda_median: Optional[float] = None
    peer_fcf_yield_median: Optional[float] = None
