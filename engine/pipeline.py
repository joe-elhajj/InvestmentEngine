"""
pipeline.py — orchestration that turns raw EDGAR data into a structured result.

This is the deterministic spine: EDGAR -> derived metrics -> peers -> valuation.
No LLM anywhere in here. The model only enters in Phase 2 (red-flag extraction
from filing text and the council), and it will operate on the *output* of this
pipeline, never on raw "analyze TICKER" guesswork.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass, field
from typing import Optional

from engine import metrics as M
from engine.edgar import CompanyData, Fact, is_fpi
from engine.market import Quote
from engine import valuation as V
from engine.config import validate_dcf_config


# ---------------------------------------------------------------------------
# Per-year derived structure — one entry per fiscal year
# ---------------------------------------------------------------------------

@dataclass
class YearlyDerived:
    """All derived metrics for one fiscal year, period-consistently computed."""
    period_end: str
    year: int
    # --- flow items (income statement / cash flow) ---
    revenue: Optional[float]
    net_income: Optional[float]
    operating_income: Optional[float]
    gross_profit: Optional[float]   # may be derived from revenue - cost_of_revenue
    cfo: Optional[float]
    capex: Optional[float]
    dep_amort: Optional[float]
    interest_expense: Optional[float]
    sbc: Optional[float]
    rnd: Optional[float]
    # --- balance-sheet items (period-matched to this year's total_assets) ---
    total_assets: Optional[float]
    total_equity: Optional[float]   # None when no same-period match
    total_debt: Optional[float]     # absent treated as 0 when anchor exists
    liquid_assets: Optional[float]
    cash: Optional[float]           # plain cash (used in invested_capital)
    current_assets: Optional[float]
    current_liabilities: Optional[float]
    # --- derived composites ---
    fcf: Optional[float]
    invested_capital: Optional[float]
    nopat: Optional[float]
    net_debt: Optional[float]
    ebit: Optional[float]
    ebitda: Optional[float]
    capital_employed: Optional[float]
    # --- simple ratios ---
    gross_margin: Optional[float]
    operating_margin: Optional[float]
    # --- audit trail ---
    gaps: list[str] = field(default_factory=list)
    derived_lineage: dict[str, str] = field(default_factory=dict)
    # --- R&D capitalization (regime OFF by default; computed whenever data
    # allows, regardless of the regime toggle -- see engine/durability.py's
    # score() for the config-gated consumption, and engine/metrics.py for
    # the pure Damodaran capitalization math this is built from) ---
    research_asset: Optional[M.ResearchAsset] = None
    # This year's basis if it were to feed an R&D-adjusted average --
    # "adjusted"/"gaap_fallback"/None (excluded), from M.rnd_basis_tag().
    # Invariant across engine/durability.py's GAAP<->adjusted view swap
    # (dataclasses.replace only overwrites nopat/invested_capital there),
    # so it stays correct whichever view a sub-score computation reads.
    rnd_basis: Optional[str] = None


# ---------------------------------------------------------------------------
# R&D-capitalization regime applicability -- LAYER 1 ONLY.
#
# This decides whether the regime applies to a company AT ALL: is it
# switched on in config, and is the filer an FPI. It does NOT decide
# whether a given year's R&D window actually resolves -- that per-year
# availability check (n_adjusted_total) is LAYER 2, owned entirely by
# engine/durability.py's score() (the `if n_adjusted_total > 0:` branch),
# and is left untouched by this predicate on purpose: routing a
# no-adjustment-path company through the matched-window view on layer-1
# grounds alone would coerce its genuine GAAP figures to None (see
# durability.py's own module-level comment on this exact failure mode).
#
# Placement: here, in pipeline.py, not in durability.py, even though
# durability.py owns the only consumer that matters for scoring.
# durability.py already imports AnalysisResult from this module (see
# durability.py's own imports), and both renderers (report.py,
# report_html.py) already import AnalysisResult from here too -- so this
# module is the one every consumer of this predicate already depends on,
# with no new edge introduced. The reverse placement would require
# derive() (here) to import FROM durability.py to stamp AnalysisResult
# during derive() -- and durability.py already imports FROM pipeline.py,
# so that would be circular.
# ---------------------------------------------------------------------------

# Single source of truth for the rnd_capitalization section's defaults --
# durability.py's _resolve_config merges config.yaml onto this SAME dict
# (imported, not redefined) for its own _DEFAULT_RND_CAPITALIZATION, and
# rnd_regime_applies below reads its `enabled` default from it too. One
# constant, both importers, no third literal that could silently drift.
RND_CAPITALIZATION_DEFAULTS: dict = {
    "enabled": False,             # regime OFF by default -- see docs/assumptions.md
    "amortization_years": 5,
}


class RndRegime(enum.Enum):
    """
    Exactly one of three states -- never a (bool, reason: str | None) pair,
    which can drift out of sync with itself (an `applies=True` with a
    non-None reason, say). The reason is inherent to which member this is,
    not a separately settable field.
    """
    APPLIES = "applies"
    ABSTAINED_REGIME_DISABLED = "abstained_regime_disabled"
    ABSTAINED_IFRS_FPI = "abstained_ifrs_fpi"


_RND_REGIME_REASON_TEXT: dict[RndRegime, str] = {
    RndRegime.ABSTAINED_REGIME_DISABLED: "R&D capitalization regime disabled in config",
    RndRegime.ABSTAINED_IFRS_FPI: (
        "IFRS filer — R&D capitalization skipped (IAS 38 already capitalizes "
        "development costs to an unknown degree; stacking this adjustment on "
        "top would produce an error of ambiguous sign)"
    ),
}


def rnd_regime_reason_text(regime: RndRegime) -> Optional[str]:
    """Disclosure text for an ABSTAINED regime state; None for APPLIES
    (nothing to disclose at this layer)."""
    return _RND_REGIME_REASON_TEXT.get(regime)


def rnd_regime_applies(cd: CompanyData, cfg: dict) -> RndRegime:
    """
    Pure layer-1 predicate: does the R&D-capitalization regime apply to
    this company at all? Reads only rnd_capitalization.enabled and
    is_fpi(cd) -- never R&D data availability (layer 2).

    Precedence: regime_disabled outranks ifrs_fpi. If the regime is off
    globally, nothing is adjusted for anyone; an FPI in that state is in
    exactly the same position as a domestic filer, so surfacing "IFRS
    filer" would falsely single it out as the reason nothing was
    adjusted. The IFRS reason only applies when the regime is otherwise
    live.
    """
    enabled = bool(cfg.get("durability", {}).get("rnd_capitalization", {})
                   .get("enabled", RND_CAPITALIZATION_DEFAULTS["enabled"]))
    if not enabled:
        return RndRegime.ABSTAINED_REGIME_DISABLED
    fpi, _ = is_fpi(cd)
    if fpi:
        return RndRegime.ABSTAINED_IFRS_FPI
    return RndRegime.APPLIES


# ---------------------------------------------------------------------------
# AnalysisResult — the full output of derive()
# ---------------------------------------------------------------------------

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
    derived_lineage: dict = field(default_factory=dict)
    annual_series: dict = field(default_factory=dict)  # period_end -> YearlyDerived
    # --- B-series additions: normalized FCF, delivered/implied growth, expectations gap ---
    normalized_fcf: Optional[float] = None
    delivered_growth: Optional[float] = None
    delivered_growth_label: str = ""
    implied_growth_result: Optional[V.ImpliedGrowthResult] = None
    expectations_gap: Optional[float] = None
    # --- PR 3: expectations gap as a bull/base/bear band, additive to the above ---
    expectations_gap_band: Optional[V.ExpectationsGapBand] = None
    # --- PR A (F-14 fix): layer-1 R&D-regime applicability, stamped once by
    # derive() so renderers read it instead of each acquiring their own cfg ---
    rnd_regime: Optional[RndRegime] = None
    # --- fix/implied-growth-abstention: WHY implied_growth_result stayed
    # None, stamped once by derive() so screen.py's _implied_growth_columns()
    # never has to guess (the fixed bug: every None here used to render as
    # "FCF non-positive" regardless of the true cause). None only when
    # implied_growth_result actually resolved, OR when this AnalysisResult
    # was constructed directly rather than through derive() -- the latter
    # is the same "unstamped" shape RndRegime's own None-guard handles. ---
    implied_growth_abstain_reason: Optional["ImpliedGrowthAbstainReason"] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _series(cd: CompanyData, key: str) -> list[tuple[float, float]]:
    return [(f.fiscal_year, f.value) for f in cd.series.get(key, [])]


def _v(cd: CompanyData, key: str) -> Optional[float]:
    return cd.latest_value(key)


def _safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


# ---------------------------------------------------------------------------
# Per-year computation
# ---------------------------------------------------------------------------

def _debt_total(long_term: Optional[Fact], short_term: Optional[Fact]) -> Optional[float]:
    """Combine resolved debt facts without adding a total to its current slice."""
    if long_term is None:
        return short_term.value if short_term is not None else None
    if short_term is None:
        return long_term.value
    if long_term.period_end == short_term.period_end:
        if long_term.concept == "us-gaap:DebtAndCapitalLeaseObligations":
            return long_term.value  # includes short- and long-term obligations
        if long_term.concept in {
            "us-gaap:LongTermDebt",
            "us-gaap:LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        } and short_term.concept in {
            "us-gaap:LongTermDebtCurrent",
            "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",
            "us-gaap:ConvertibleDebtCurrent",
        }:
            return long_term.value
    return long_term.value + short_term.value


def _build_year_entry(
    cd: CompanyData, period_end: str, tax_rate: float, rnd_amortization_years: int = 5,
) -> YearlyDerived:
    """Derive all metrics for one fiscal year anchored at period_end."""
    year = int(period_end[:4])
    gaps: list[str] = []
    dl: dict[str, str] = {}

    def _pv(key: str) -> Optional[float]:
        """Period-matched value with staleness gap-logging. Returns the
        Fact's value for this exact period_end; when no exact match exists,
        logs a gap naming the latest period that DOES exist elsewhere in
        the series (when there is one) and returns None. Used uniformly
        for flow and balance-sheet concepts alike -- a flow concept (e.g.
        interest_expense) that only resolves for a stale period is exactly
        as real a degradation as a stale balance-sheet item, and must
        disclose the same way. Previously two separate functions (_fv:
        flows, silent; _bs: balance-sheet, logged) that differed only in
        this logging -- consolidated since giving _fv the same disclosure
        makes them behaviorally identical."""
        f = cd.value_for_period(key, period_end)
        if f is None:
            lat = cd.latest(key)
            if lat is not None:
                gaps.append(
                    f"{key}: no value for period {period_end} "
                    f"(latest available is {lat.period_end}, not used)"
                )
        return f.value if f else None

    # Flow items
    rev = _pv("revenue")
    ni = _pv("net_income")
    oi = _pv("operating_income")
    gp = _pv("gross_profit")
    cfo_val = _pv("cfo")
    capex_val = _pv("capex")
    da = _pv("dep_amort")
    interest = _pv("interest_expense")
    sbc_val = _pv("sbc")
    rnd_val = _pv("rnd")

    # total_assets: always present (period_end comes from its own series,
    # so this can never hit the missing branch above) — kept on _pv for
    # consistency, not because it needs the gap-logging path.
    total_assets = _pv("total_assets")

    # Liquid assets — each component period-matched with gap logging
    cash_fact = cd.value_for_period("cash", period_end)
    sti_fact = cd.value_for_period("short_term_investments", period_end)
    lti_fact = cd.value_for_period("long_term_investments", period_end)
    cash_val = cash_fact.value if cash_fact else 0.0
    sti_val = sti_fact.value if sti_fact else 0.0
    lti_val = lti_fact.value if lti_fact else 0.0
    if not cash_fact:
        lat = cd.latest("cash")
        if lat is not None:
            gaps.append(f"cash: no value for period {period_end} (latest available is {lat.period_end}, not used)")
    if not sti_fact:
        lat = cd.latest("short_term_investments")
        if lat is not None:
            gaps.append(f"short_term_investments: no value for period {period_end} (latest available is {lat.period_end}, not used)")
    if not lti_fact:
        lat = cd.latest("long_term_investments")
        if lat is not None:
            gaps.append(f"long_term_investments: no value for period {period_end} (latest available is {lat.period_end}, not used)")
    # Absence-is-not-zero: only None when NONE of the three components resolved.
    # When at least one resolves, sum the resolved ones — the missing ones are
    # already logged above as gaps, not silently folded into the total as 0.
    liquid_assets = (cash_val + sti_val + lti_val) if (cash_fact or sti_fact or lti_fact) else None
    cash = cash_fact.value if cash_fact else None  # plain cash for invested-capital; None, not 0

    # Total debt — period-matched with gap logging
    ltd_fact = cd.value_for_period("long_term_debt", period_end)
    std_fact = cd.value_for_period("short_term_debt", period_end)
    if not ltd_fact:
        lat = cd.latest("long_term_debt")
        if lat is not None:
            gaps.append(f"long_term_debt: no value for period {period_end} (latest available is {lat.period_end}, not used)")
    if not std_fact:
        lat = cd.latest("short_term_debt")
        if lat is not None:
            gaps.append(f"short_term_debt: no value for period {period_end} (latest available is {lat.period_end}, not used)")
    # Same rule: None only when BOTH components are absent, never a silent $0.
    total_debt = _debt_total(ltd_fact, std_fact)

    # Total equity — period-matched with gap logging
    equity = _pv("total_equity")

    # Current assets / liabilities
    cur_assets = _pv("current_assets")
    cur_liab = _pv("current_liabilities")

    # Gross profit fallback: revenue - cost_of_revenue when GrossProfit tag absent
    if gp is None:
        rev_f = cd.value_for_period("revenue", period_end)
        cor_f = cd.value_for_period("cost_of_revenue", period_end)
        if rev_f is not None and cor_f is not None:
            gp = rev_f.value - cor_f.value
            dl["gross_profit"] = (
                f"derived: revenue({rev_f.concept}) - cost_of_revenue({cor_f.concept})"
            )

    # Derived composites
    fcf = (cfo_val - capex_val) if (cfo_val is not None and capex_val is not None) else None
    # net_debt requires both components resolved — None (not a silent 0) when either is absent.
    net_debt = (total_debt - liquid_assets) if (total_debt is not None and liquid_assets is not None) else None
    ebit = oi
    ebitda = (oi + da) if (oi is not None and da is not None) else None
    # invested_capital: total_debt + equity - cash  (any absent term → None, never 0-substituted)
    invested_capital = (
        (total_debt + equity - cash)
        if (total_debt is not None and equity is not None and cash is not None)
        else None
    )
    # capital_employed: total_assets - current_liabilities (absent current_liabilities → None,
    # not silently treated as 0 — that would overstate capital_employed)
    capital_employed = (
        (total_assets - cur_liab) if (total_assets is not None and cur_liab is not None) else None
    )
    nopat = (ebit * (1 - tax_rate)) if ebit is not None else None

    # R&D capitalization research asset (Damodaran method) — computed
    # whenever the trailing amortization_years-year window of R&D fully
    # resolves, regardless of the durability.rnd_capitalization.enabled
    # toggle (that flag gates CONSUMPTION in engine/durability.py's score(),
    # not computation here). Built directly from cd's own raw R&D facts
    # rather than from a not-yet-built `annual` dict, since this function
    # processes one fiscal year at a time and every other anchor year gets
    # its own _build_year_entry() call.
    all_anchors = sorted(f.period_end for f in cd.series.get("total_assets", []))
    anchors_by_year = {int(pe[:4]): pe for pe in all_anchors}
    rnd_window: dict[str, Optional[float]] = {}
    for k in range(rnd_amortization_years):
        pe_k = anchors_by_year.get(year - k)
        if pe_k is None:
            continue
        f_k = cd.value_for_period("rnd", pe_k)
        rnd_window[pe_k] = f_k.value if f_k is not None else None
    research_asset = M.build_research_asset(rnd_window, rnd_amortization_years) if rnd_window else None
    rnd_basis = M.rnd_basis_tag(nopat, invested_capital, research_asset, rnd_val)

    return YearlyDerived(
        period_end=period_end, year=year,
        revenue=rev, net_income=ni, operating_income=oi, gross_profit=gp,
        cfo=cfo_val, capex=capex_val, dep_amort=da,
        interest_expense=interest, sbc=sbc_val, rnd=rnd_val,
        total_assets=total_assets, total_equity=equity, total_debt=total_debt,
        liquid_assets=liquid_assets, cash=cash,
        current_assets=cur_assets, current_liabilities=cur_liab,
        fcf=fcf, invested_capital=invested_capital, nopat=nopat,
        net_debt=net_debt, ebit=ebit, ebitda=ebitda,
        capital_employed=capital_employed,
        gross_margin=_safe_div(gp, rev),
        operating_margin=_safe_div(oi, rev),
        gaps=gaps, derived_lineage=dl,
        research_asset=research_asset,
        rnd_basis=rnd_basis,
    )


# ---------------------------------------------------------------------------
# B-series helpers: normalized FCF, delivered growth
# ---------------------------------------------------------------------------

class ImpliedGrowthAbstainReason(enum.Enum):
    """
    Layer-1 reason tag for why derive() never computed
    AnalysisResult.implied_growth_result at all -- stamped once (see
    AnalysisResult.implied_growth_abstain_reason) so screen.py's
    _implied_growth_columns() renders the TRUE cause instead of guessing.
    The fixed bug: every None here used to render as "FCF non-positive"
    regardless of actual cause -- e.g. a real positive normalized_fcf
    blocked only by a missing net_debt, or a total data desert with no
    FCF/revenue pairs to compute a margin from at all.

    bracket_hit is deliberately NOT a member here: hitting the bisection
    bracket IS a computed result (implied_growth_result is NOT None), not
    an abstention -- see _gap_bracket_bound in screen.py, which reads
    ImpliedGrowthResult.bracket_bound directly instead.
    """
    CURRENCY_GATED = "currency_gated"              # reporting_currency != USD
    NO_DATA = "no_data"                            # no FCF/revenue pairs to compute a margin from
    FCF_NONPOSITIVE = "fcf_nonpositive"             # a margin WAS computed, and it's <= 0
    NET_DEBT_MISSING = "net_debt_missing"
    PRICE_SHARES_MISSING = "price_shares_missing"  # market-vendor-tier quote unavailable


def _normalized_fcf(
    annual: dict[str, YearlyDerived],
    window: int = 5,
) -> tuple[Optional[float], str, Optional[ImpliedGrowthAbstainReason]]:
    """
    Normalized FCF = median(FCF margin over last `window` fiscal years) × latest revenue.

    Restricting to the recent window reflects current structural economics better
    than a full 15-year history that may include a different business model.
    Configurable via valuation.normalized_fcf_years (default 5).

    Returns (value, lineage, abstain_reason). abstain_reason is None
    exactly when value is not None (nothing to abstain from); otherwise
    it's NO_DATA (no pairs at all, or the latest period's revenue itself
    is absent -- there was nothing to compute FROM) or FCF_NONPOSITIVE (a
    margin WAS computed, and rejected as <= 0) -- these are NOT the same
    claim, and conflating them is the bug this reason tag exists to fix.
    """
    all_pairs = sorted(
        [
            (pe, yd.fcf / yd.revenue)
            for pe, yd in annual.items()
            if yd.fcf is not None and yd.revenue is not None and yd.revenue > 0
        ],
        key=lambda x: x[0],
    )
    pairs = all_pairs[-window:]  # keep only the last `window` fiscal years

    if not pairs:
        return None, "normalized_fcf: no FCF/revenue pairs in annual series", ImpliedGrowthAbstainReason.NO_DATA

    margins = [m for _, m in pairs]
    periods = [pe for pe, _ in pairs]
    median_margin = statistics.median(margins)

    if median_margin <= 0:
        return None, (
            f"normalized_fcf: median FCF margin {median_margin:.2%} ≤ 0 — not meaningful"
        ), ImpliedGrowthAbstainReason.FCF_NONPOSITIVE

    latest = annual[max(annual)]
    if latest.revenue is None:
        return None, "normalized_fcf: latest revenue absent", ImpliedGrowthAbstainReason.NO_DATA

    val = median_margin * latest.revenue
    lineage = (
        f"normalized_fcf: median FCF margin {median_margin:.3%} × "
        f"revenue {latest.revenue:.0f} "
        f"(window={window}, periods: {', '.join(p[:4] for p in periods)})"
    )
    return val, lineage, None


def _delivered_growth(
    annual: dict[str, YearlyDerived],
    min_history: int = 4,
) -> tuple[Optional[float], str]:
    """
    Historical FCF CAGR (≥2 strictly positive FCF years).
    Falls back to revenue CAGR when FCF history is non-positive or unavailable.
    Returns (value, label).

    Requires at least `min_history` annual data points (configurable via
    valuation.min_history_years, default 4) to avoid CAGR-on-2-points noise.
    """
    if len(annual) < min_history:
        return (
            None,
            f"insufficient history ({len(annual)} annual points, need {min_history})",
        )

    fcf_pts = [(yd.year, yd.fcf) for yd in annual.values() if yd.fcf is not None]
    if len(fcf_pts) >= 2 and all(v > 0 for _, v in fcf_pts):
        m = M.cagr_over(fcf_pts, 5)
        if m.value is not None:
            label = "FCF CAGR" + (f" ({m.note})" if m.note else "")
            return m.value, label

    rev_pts = [(yd.year, yd.revenue) for yd in annual.values() if yd.revenue is not None]
    m = M.cagr_over(rev_pts, 5)
    label = "revenue CAGR (FCF history non-positive or unavailable)" + (f" ({m.note})" if m.note else "")
    return m.value, label


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def derive_annual_series(cd: CompanyData, config: dict) -> dict[str, YearlyDerived]:
    """
    Build a period-consistent YearlyDerived entry for every fiscal year that has
    a total_assets anchor.  Returns an ordered dict keyed by period_end string
    (oldest first).

    Period consistency is enforced within each year: every balance-sheet
    component is matched to that year's total_assets period_end.  Absence is
    never treated as zero for equity; a gap is logged instead.
    """
    tax_rate = config.get("valuation", {}).get("assumed_tax_rate", 0.21)
    # Read directly from the raw config dict (not durability._resolve_config,
    # which pipeline.py must not import -- durability.py imports FROM
    # pipeline.py, and screen.py imports durability.py, so the reverse
    # import would be circular). Default (5) matches config.yaml's own
    # default so behavior is correct even when the section is omitted.
    rnd_years = int(
        config.get("durability", {}).get("rnd_capitalization", {}).get("amortization_years", 5)
    )
    anchors = [f.period_end for f in cd.series.get("total_assets", [])]
    return {pe: _build_year_entry(cd, pe, tax_rate, rnd_years) for pe in sorted(anchors)}


def derive(cd: CompanyData, quote: Quote, config: dict) -> AnalysisResult:
    validate_dcf_config(config)
    res = AnalysisResult(company=cd, quote=quote)
    res.rnd_regime = rnd_regime_applies(cd, config)
    res.gaps = list(cd.unresolved)

    val_cfg = config.get("valuation", {})
    tax_rate = val_cfg.get("assumed_tax_rate", 0.21)
    fcf_window    = int(val_cfg.get("normalized_fcf_years", 5))
    min_history   = int(val_cfg.get("min_history_years", 4))

    annual = derive_annual_series(cd, config)
    res.annual_series = annual

    # S3 fix (Session B.2 PR-B): res.gaps above was snapshotted from
    # cd.unresolved BEFORE the per-year revenue - cost_of_revenue fallback
    # (_build_year_entry, inside derive_annual_series just above) ever ran —
    # so a ticker whose GrossProfit tag never resolves directly (confirmed:
    # META, CAT) kept reporting a false "gross_profit" gap even when every
    # year's value actually resolved via the fallback. Reconcile now that
    # the real, per-year answer is known. Keyed on `is not None`, never
    # truthiness — a legitimately zero gross profit must not re-open the
    # gap. Narrow and single-metric on purpose: only removed when EVERY
    # year resolved — a still-partial fallback (e.g. cost_of_revenue itself
    # missing for some years) is a real, ongoing gap and stays reported.
    if "gross_profit" in res.gaps and annual and all(yd.gross_profit is not None for yd in annual.values()):
        res.gaps.remove("gross_profit")

    # Normalized FCF and delivered growth (both depend only on annual series)
    nfcf_val, nfcf_lineage, nfcf_abstain_reason = _normalized_fcf(annual, window=fcf_window)
    res.normalized_fcf = nfcf_val
    if nfcf_val is None:
        res.gaps.append(nfcf_lineage)
    else:
        res.derived_lineage["normalized_fcf"] = nfcf_lineage

    dg_val, dg_label = _delivered_growth(annual, min_history=min_history)
    res.delivered_growth = dg_val
    res.delivered_growth_label = dg_label
    if dg_val is None:
        res.gaps.append(f"delivered_growth: {dg_label}")

    if annual:
        yd = annual[max(annual)]
        res.gaps.extend(yd.gaps)
        res.derived_lineage.update(yd.derived_lineage)
        rev       = yd.revenue
        ni        = yd.net_income
        oi        = yd.operating_income
        gp        = yd.gross_profit
        cfo       = yd.cfo
        capex     = yd.capex
        da        = yd.dep_amort
        interest  = yd.interest_expense
        assets    = yd.total_assets
        equity    = yd.total_equity
        cur_assets = yd.current_assets
        cur_liab  = yd.current_liabilities
        total_debt     = yd.total_debt
        liquid_assets  = yd.liquid_assets
        net_debt       = yd.net_debt
        fcf            = yd.fcf
        invested_capital = yd.invested_capital
        nopat          = yd.nopat
        ebit           = yd.ebit
        ebitda         = yd.ebitda
        capital_employed = yd.capital_employed
        cash           = yd.cash
        rnd_val        = yd.rnd
        research_asset = yd.research_asset
    else:
        # No balance-sheet anchor (total_assets absent); flow metrics still available
        rev      = _v(cd, "revenue")
        ni       = _v(cd, "net_income")
        oi       = _v(cd, "operating_income")
        gp       = _v(cd, "gross_profit")
        cfo      = _v(cd, "cfo")
        capex    = _v(cd, "capex")
        da       = _v(cd, "dep_amort")
        interest = _v(cd, "interest_expense")
        assets   = None;  equity = None
        cur_assets = None;  cur_liab = None
        total_debt = None;  liquid_assets = None
        cash = _v(cd, "cash")
        fcf = (cfo - capex) if (cfo is not None and capex is not None) else None
        net_debt = None;  invested_capital = None
        ebit   = oi
        ebitda = (oi + da) if (oi is not None and da is not None) else None
        capital_employed = None
        nopat  = (ebit * (1 - tax_rate)) if ebit is not None else None
        rnd_val = None
        research_asset = None
        res.gaps.append(
            "total_debt: no anchor period (total_assets absent); treated as absent, not zero"
        )

    res.derived = {
        "revenue": rev, "net_income": ni, "operating_income": oi, "gross_profit": gp,
        "fcf": fcf, "ebitda": ebitda, "total_debt": total_debt,
        "net_debt": net_debt, "liquid_assets": liquid_assets,
        "cash": cash, "total_equity": equity, "total_assets": assets,
    }

    if cd.quarterly:
        def _qv(key: str) -> Optional[float]:
            fact = cd.quarterly.get(key)
            return fact.value if fact else None
        q_revenue          = _qv("revenue")
        q_net_income       = _qv("net_income")
        q_operating_income = _qv("operating_income")
        q_cfo              = _qv("cfo")
        q_capex            = _qv("capex")
        q_fcf = (q_cfo - q_capex) if (q_cfo is not None and q_capex is not None) else None
        q_period_end = max(f.period_end for f in cd.quarterly.values())
        q_filed      = max(f.filed     for f in cd.quarterly.values())

        def _qsum(*keys: str) -> Optional[float]:
            """
            Sum whichever of `keys` resolve in the latest quarter. Absence-is-
            not-zero: None only when NONE of them resolve — a missing component
            is logged as a gap, never silently substituted with 0 into the total
            (this is the exact defect that showed total_debt as $0 for CAT's
            2026-03-31 10-Q when both long_term_debt and short_term_debt were
            unreported for the quarter).
            """
            vals = [_qv(k) for k in keys]
            missing = [k for k, v in zip(keys, vals) if v is None]
            if missing:
                res.gaps.append(
                    f"{'/'.join(missing)}: no value in latest quarter ({q_period_end})"
                )
            if keys == ("long_term_debt", "short_term_debt"):
                return _debt_total(cd.quarterly.get(keys[0]), cd.quarterly.get(keys[1]))
            present = [v for v in vals if v is not None]
            return sum(present) if present else None

        q_total_debt    = _qsum("long_term_debt", "short_term_debt")
        q_liquid_assets = _qsum("cash", "short_term_investments", "long_term_investments")
        q_cash          = _qv("cash")
        q_total_assets  = _qv("total_assets")
        q_total_equity  = _qv("total_equity")
        res.latest_quarter = {
            "period_end": q_period_end, "filed": q_filed,
            "revenue": q_revenue, "net_income": q_net_income,
            "operating_income": q_operating_income, "fcf": q_fcf,
            "total_assets": q_total_assets, "total_equity": q_total_equity,
            "total_debt": q_total_debt, "cash": q_cash, "liquid_assets": q_liquid_assets,
            "margins": {
                "operating_margin": M.operating_margin(q_operating_income, q_revenue),
                "net_margin":       M.net_margin(q_net_income, q_revenue),
                "fcf_margin":       M.fcf_margin(q_fcf, q_revenue),
            },
            "facts": cd.quarterly,
        }
    else:
        res.latest_quarter = {}

    # Growth (CAGR) on revenue and net income (full multi-year series)
    for label, key in (("revenue", "revenue"), ("net_income", "net_income")):
        s = _series(cd, key)
        res.growth[label] = {y: M.cagr_over(s, y) for y in (5, 10, 15)}

    # Margins, returns, leverage
    r = res.ratios
    r["gross_margin"]       = M.gross_margin(gp, rev)
    r["operating_margin"]   = M.operating_margin(oi, rev)
    r["net_margin"]         = M.net_margin(ni, rev)
    r["fcf_margin"]         = M.fcf_margin(fcf, rev)
    r["roe"]                = M.roe(ni, equity)
    r["roa"]                = M.roa(ni, assets)
    r["roic"]               = M.roic(nopat, invested_capital)
    # R&D-capitalization-adjusted ROIC — always computed when data allows,
    # independent of durability.rnd_capitalization.enabled (that flag gates
    # only the durability score's reinvestment_engine consumption; this
    # report-level ratio is a display-when-computable figure per the
    # "abstain and disclose" calibration principle, not a regime-gated one).
    r["roic_adjusted"]      = M.adjusted_roic(nopat, invested_capital, research_asset, rnd_val)
    r["roce"]               = M.roce(ebit, capital_employed)
    r["current_ratio"]      = M.current_ratio(cur_assets, cur_liab)
    r["debt_to_equity"]     = M.debt_to_equity(total_debt, equity)
    r["interest_coverage"]  = M.interest_coverage(ebit, interest)
    r["fcf_conversion"]     = M.fcf_conversion(fcf, ni)

    # Currency gate (A4): market-mixed outputs require USD reporting.
    # Ratio/growth metrics above are currency-invariant and always computed.
    # rel_val, DCF, implied_growth, and expectations_gap are gated when non-USD.
    reporting_ccy = cd.reporting_currency
    if reporting_ccy != "USD":
        ccy_gap = (
            f"valuation n/a — reporting currency {reporting_ccy} vs USD market data; "
            "no FX conversion performed"
        )
        res.gaps.append(ccy_gap)
        res.implied_growth_abstain_reason = ImpliedGrowthAbstainReason.CURRENCY_GATED
        res.gaps.append(
            f"implied_growth: reporting currency {reporting_ccy} vs USD market data"
        )
        res.rel_val = V.RelativeValuation(pe=None, ev_ebitda=None, fcf_yield=None)
        return res

    # Relative valuation multiples
    price  = quote.price
    mc     = quote.market_cap
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
    dcf_cfg = val_cfg.get("dcf", {})
    if fcf is not None and fcf > 0 and dcf_cfg:
        if net_debt is None:
            res.gaps.append("dcf: net_debt unavailable; DCF skipped")
        else:
            common = {"projection_years": dcf_cfg.get("projection_years", 5)}
            for name, sc in dcf_cfg.get("scenarios", {}).items():
                a = dict(common); a.update(sc)
                res.dcf[name] = V.two_stage_dcf(fcf, net_debt, shares, price, name, a)
            sens = dcf_cfg.get("sensitivity")
            base = dcf_cfg.get("scenarios", {}).get("base")
            if sens and base:
                a = dict(common); a.update(base)
                res.sensitivity = V.sensitivity_grid(
                    fcf, net_debt, shares, a,
                    sens.get("wacc", []), sens.get("terminal_growth", []))
    elif not dcf_cfg:
        res.gaps.append("dcf: no assumptions configured")
    else:
        res.gaps.append("dcf: base FCF unavailable or non-positive")

    # Implied growth (reverse DCF) and expectations gap
    # Uses normalized_fcf (B1) as the base FCF; systematic WACC/terminal_g from
    # config. Restructured as explicit if/elif/else (fix/implied-growth-
    # abstention) so each non-computing path stamps its OWN true cause onto
    # implied_growth_abstain_reason and gap-logs it -- the original single
    # AND-condition collapsed all four preconditions into one silent None,
    # which screen.py's _implied_growth_columns() then had to guess at
    # (always "FCF non-positive", regardless of which precondition actually
    # failed). Branch order = priority when more than one would fail
    # (normalized_fcf first, since it's the base input; then net_debt; then
    # the market-vendor-tier quote) -- logically equivalent to the original
    # AND-condition's negation, so the success path below is unchanged.
    if res.normalized_fcf is None:
        res.implied_growth_abstain_reason = nfcf_abstain_reason
        res.gaps.append(f"implied_growth: {nfcf_lineage}")
    elif net_debt is None:
        res.implied_growth_abstain_reason = ImpliedGrowthAbstainReason.NET_DEBT_MISSING
        res.gaps.append("implied_growth: net_debt unavailable")
    elif not (quote.price and quote.price > 0
              and quote.shares_outstanding and quote.shares_outstanding > 0):
        res.implied_growth_abstain_reason = ImpliedGrowthAbstainReason.PRICE_SHARES_MISSING
        res.gaps.append("implied_growth: price or share count unavailable (market-vendor tier)")
    else:
        res.implied_growth_result = V.implied_growth(
            price=quote.price,
            shares=quote.shares_outstanding,
            net_debt=net_debt,
            norm_fcf=res.normalized_fcf,
            config=config,
        )
        igr = res.implied_growth_result
        if igr.bracket_hit:
            # A REAL result, not an abstention -- the market price is
            # off-scale rich/cheap even at the model's growth ceiling/
            # floor. Still worth a gap-log line (T4): "gaps reads
            # complete without cross-inference" applies to this path too,
            # even though it renders differently (see _gap_bracket_bound
            # in screen.py) from a genuine absence.
            res.gaps.append(f"implied_growth: {igr.lineage}")
        elif res.delivered_growth is not None:
            res.expectations_gap = igr.implied_growth - res.delivered_growth

        # Same reverse-DCF, run under all three owned scenario bundles (PR 3).
        # Additive only: res.expectations_gap above is untouched by this call.
        res.expectations_gap_band = V.expectations_gap_band(
            price=quote.price,
            shares=quote.shares_outstanding,
            net_debt=net_debt,
            norm_fcf=res.normalized_fcf,
            delivered_growth=res.delivered_growth,
            config=config,
        )
        band = res.expectations_gap_band
        if band is not None and band.band_status == "PARTIAL":
            failed = [name for name, sc in band.scenarios.items() if not sc.converged]
            res.gaps.append(
                "expectations_gap: " + ", ".join(failed) +
                " implied growth outside bisection bracket — band incomplete"
            )

    return res
