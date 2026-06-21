"""
metrics.py — deterministic financial calculations.

Every function here is a pure function of numbers in -> numbers out. No I/O,
no model, no network. This is the part that must be correct, so it is the part
that gets unit tested (see tests/test_core.py).

A recurring theme: when a ratio is mathematically defined but economically
meaningless (e.g. P/E on negative earnings), we return None plus a reason
rather than printing a misleading number. "Doing its best job" means refusing
to emit garbage that looks authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Metric:
    """A computed metric with an optional 'not meaningful' explanation."""
    name: str
    value: Optional[float]
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


def safe_div(num: Optional[float], den: Optional[float],
             allow_negative_den: bool = True) -> Optional[float]:
    if num is None or den is None or den == 0:
        return None
    if not allow_negative_den and den < 0:
        return None
    return num / den


def cagr(begin: Optional[float], end: Optional[float], years: float) -> Metric:
    """Compound annual growth rate. Undefined if sign flips or non-positive base."""
    if begin is None or end is None or years <= 0:
        return Metric("cagr", None, "missing data")
    if begin <= 0 or end <= 0:
        return Metric("cagr", None, "non-positive endpoint; CAGR not meaningful")
    return Metric("cagr", (end / begin) ** (1.0 / years) - 1.0)


def cagr_over(series: list[tuple[float, float]], target_years: int) -> Metric:
    """
    series: list of (year_end_ordinal, value) sorted ascending. We use the
    actual elapsed years between the chosen endpoints, so a '5-year' figure
    reflects the real gap even if a filing year is missing.
    """
    if len(series) < 2:
        return Metric(f"cagr_{target_years}y", None, "need >=2 annual points")
    latest_year, latest_val = series[-1]
    # find the earliest point that is at least `target_years` back, else the oldest
    candidate = series[0]
    for yr, val in series:
        if latest_year - yr >= target_years:
            candidate = (yr, val)
    begin_year, begin_val = candidate
    elapsed = latest_year - begin_year
    if elapsed <= 0:
        return Metric(f"cagr_{target_years}y", None, "insufficient history")
    m = cagr(begin_val, latest_val, elapsed)
    m.name = f"cagr_{target_years}y"
    if m.value is not None and elapsed < target_years:
        m.note = f"only {elapsed:.0f}y of history available"
    return m


# ---- margins -------------------------------------------------------------
def gross_margin(gross_profit, revenue):
    return Metric("gross_margin", safe_div(gross_profit, revenue))


def operating_margin(operating_income, revenue):
    return Metric("operating_margin", safe_div(operating_income, revenue))


def net_margin(net_income, revenue):
    return Metric("net_margin", safe_div(net_income, revenue))


def fcf_margin(fcf, revenue):
    return Metric("fcf_margin", safe_div(fcf, revenue))


# ---- returns -------------------------------------------------------------
def roe(net_income, total_equity):
    if total_equity is not None and total_equity <= 0:
        return Metric("roe", None, "negative/zero equity; ROE not meaningful")
    return Metric("roe", safe_div(net_income, total_equity))


def roa(net_income, total_assets):
    return Metric("roa", safe_div(net_income, total_assets))


def roic(nopat, invested_capital):
    if invested_capital is not None and invested_capital <= 0:
        return Metric("roic", None, "non-positive invested capital")
    return Metric("roic", safe_div(nopat, invested_capital))


def roce(ebit, capital_employed):
    if capital_employed is not None and capital_employed <= 0:
        return Metric("roce", None, "non-positive capital employed")
    return Metric("roce", safe_div(ebit, capital_employed))


# ---- leverage / liquidity ------------------------------------------------
def current_ratio(current_assets, current_liabilities):
    return Metric("current_ratio", safe_div(current_assets, current_liabilities))


def debt_to_equity(total_debt, total_equity):
    if total_equity is not None and total_equity <= 0:
        return Metric("debt_to_equity", None, "negative/zero equity")
    return Metric("debt_to_equity", safe_div(total_debt, total_equity))


def interest_coverage(ebit, interest_expense):
    ie = abs(interest_expense) if interest_expense is not None else None
    if ie == 0:
        return Metric("interest_coverage", None, "no interest expense")
    return Metric("interest_coverage", safe_div(ebit, ie))


def fcf_conversion(fcf, net_income):
    if net_income is not None and net_income <= 0:
        return Metric("fcf_conversion", None, "non-positive net income")
    return Metric("fcf_conversion", safe_div(fcf, net_income))


# ---- valuation multiples (need price/market data) ------------------------
def pe_ratio(price, eps):
    if eps is not None and eps <= 0:
        return Metric("pe", None, "negative/zero EPS; P/E not meaningful")
    return Metric("pe", safe_div(price, eps))


def ev_ebitda(enterprise_value, ebitda):
    if ebitda is not None and ebitda <= 0:
        return Metric("ev_ebitda", None, "non-positive EBITDA")
    return Metric("ev_ebitda", safe_div(enterprise_value, ebitda))


def fcf_yield(fcf, market_cap):
    return Metric("fcf_yield", safe_div(fcf, market_cap))


# ---- common-size ---------------------------------------------------------
def common_size(line_items: dict, base: Optional[float]) -> dict:
    """Express each line item as a fraction of a base (revenue or total assets)."""
    out = {}
    for k, v in line_items.items():
        out[k] = safe_div(v, base)
    return out
