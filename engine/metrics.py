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
    elif m.value is not None and elapsed > target_years:
        # Session B disclosure: a data gap between the two chosen endpoints
        # (e.g. a permanent companyfacts absence — see docs/assumptions.md)
        # can force the earliest candidate far past the requested window —
        # stale, not "extra data." Distinct wording from the elapsed <
        # target_years note above so callers/UI can key on "window:"
        # specifically.
        m.note = f"window: {elapsed:.0f}y actual vs {target_years}y requested (sparse early data)"
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


# ---- R&D capitalization (Damodaran method) --------------------------------
@dataclass
class ResearchAsset:
    """A capitalized R&D research asset built from a trailing
    amortization_years-year window of R&D expense, straight-line amortized.
    schedule entries are (period_end, original_rnd_value, unamortized_fraction),
    oldest-first, for lineage — so a renderer can show exactly which years
    contributed and how much of each is still on the books."""
    unamortized_balance: float
    current_amortization: float
    schedule: list[tuple[str, float, float]]


def build_research_asset(rnd_series: dict, amortization_years: int) -> Optional[ResearchAsset]:
    """
    Damodaran R&D capitalization: an R&D dollar spent in year t is written
    off in equal installments of 1/N over years t, t+1, ..., t+N-1 (N total
    years, starting the same year it's spent). rnd_series maps period_end
    -> R&D expense (or None); the valuation year is the LATEST period_end
    present. Full-window-or-nothing: requires amortization_years CONSECUTIVE
    fiscal years (by year number, not merely N dict entries — a fiscal-year
    gap inside the window fails this just as a genuinely missing year does)
    of non-None R&D ending at the valuation year. Returns None otherwise —
    a partial research asset would understate invested capital and
    overstate adjusted ROIC, exactly backwards from what the adjustment
    exists to correct (docs/assumptions.md's history-insufficiency rule).
    """
    if not rnd_series or amortization_years <= 0:
        return None
    n = amortization_years
    valuation_year = int(max(rnd_series)[:4])
    by_year = {int(pe[:4]): (pe, val) for pe, val in rnd_series.items()}

    window = []  # (years_ago, period_end, value), years_ago=0 is the valuation year
    for years_ago in range(n):
        yr = valuation_year - years_ago
        if yr not in by_year:
            return None
        pe, val = by_year[yr]
        if val is None:
            return None
        window.append((years_ago, pe, val))

    unamortized_balance = sum(val * (n - years_ago) / n for years_ago, _, val in window)
    current_amortization = sum(val / n for years_ago, _, val in window if years_ago >= 1)
    schedule = sorted(
        ((pe, val, (n - years_ago) / n) for years_ago, pe, val in window),
        key=lambda row: row[0],
    )
    return ResearchAsset(
        unamortized_balance=unamortized_balance,
        current_amortization=current_amortization,
        schedule=schedule,
    )


def rnd_adjusted_nopat_and_ic(nopat, invested_capital, research_asset, current_rnd):
    """
    NOPAT_adj = nopat + current_rnd - amortization; IC_adj = invested_capital
    + unamortized_balance. Absence-is-not-zero: any missing input means the
    adjustment cannot be computed at all (not "as if R&D were zero") — both
    return None, never a silent partial adjustment. Shared by adjusted_roic
    (below, which reduces this pair to a ratio) and durability.py's
    reinvestment-engine regime swap (which needs the numerator/denominator
    separately, not their quotient).
    """
    if nopat is None or invested_capital is None or research_asset is None or current_rnd is None:
        return None, None
    nopat_adj = nopat + current_rnd - research_asset.current_amortization
    ic_adj = invested_capital + research_asset.unamortized_balance
    return nopat_adj, ic_adj


def adjusted_roic(nopat, invested_capital, research_asset, current_rnd):
    """R&D-capitalization-adjusted ROIC. Returns a data-missing Metric (never
    a silent partial adjustment) if any input is None; otherwise reuses
    roic()'s own non-positive-invested-capital guard for consistency."""
    nopat_adj, ic_adj = rnd_adjusted_nopat_and_ic(nopat, invested_capital, research_asset, current_rnd)
    if nopat_adj is None or ic_adj is None:
        return Metric("roic_adjusted", None, "missing data for R&D-adjusted ROIC")
    m = roic(nopat_adj, ic_adj)
    m.name = "roic_adjusted"
    return m


def rnd_basis_tag(nopat, invested_capital, research_asset, current_rnd):
    """
    Per-year basis tag for a single fiscal year, used by the R&D-
    capitalization mixed-basis disclosure (averaged metrics like roic_mean/
    compounding_proxy can span years on different bases when a company's
    early history predates a full amortization-years R&D window):

      "adjusted"      -- this year's research asset resolved; it would
                         contribute an R&D-adjusted nopat/invested_capital
                         to an average.
      "gaap_fallback"  -- nopat/invested_capital resolve in GAAP but the
                         R&D adjustment specifically didn't for this year
                         (insufficient trailing window, or no R&D data);
                         it would contribute a plain-GAAP value instead.
      None             -- neither resolves. Absence-is-not-zero: this year
                         is excluded from the average entirely upstream,
                         and must count toward NEITHER basis nor any total
                         here either.
    """
    nopat_adj, ic_adj = rnd_adjusted_nopat_and_ic(nopat, invested_capital, research_asset, current_rnd)
    if nopat_adj is not None and ic_adj is not None:
        return "adjusted"
    if nopat is not None and invested_capital is not None:
        return "gaap_fallback"
    return None


def classify_basis_mix(bases):
    """
    Classify the basis composition of an averaged metric's constituent
    years. `bases` must already exclude None-tagged (excluded) years --
    the caller filters those out upstream, since a None year contributes
    to neither count. Returns (classification, n_adjusted, n_fallback):

      "clean"       -- every year adjusted (n_fallback == 0)
      "mixed"       -- >=1 adjusted AND >=1 fallback (the AND is
                       deliberate: a NO_RND series is all-fallback, zero
                       adjusted, and must classify "unadjusted", never
                       "mixed" via a careless OR)
      "unadjusted"  -- zero adjusted years (n_adjusted == 0), whether
                       because every year individually fell back or
                       because there was never any R&D data at all
    """
    n_adjusted = bases.count("adjusted")
    n_fallback = bases.count("gaap_fallback")
    if n_adjusted == 0:
        return "unadjusted", n_adjusted, n_fallback
    if n_fallback == 0:
        return "clean", n_adjusted, n_fallback
    return "mixed", n_adjusted, n_fallback


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
