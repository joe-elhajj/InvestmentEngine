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

# Bisection bracket for implied growth: sane range that covers virtually all
# real companies; anything outside is flagged rather than extrapolated.
_IMPLIED_GROWTH_LOWER = -0.20   # -20 %
_IMPLIED_GROWTH_UPPER = +0.60   # +60 %
_IMPLIED_GROWTH_TOL   = 1e-7    # convergence: growth rate accuracy ~0.00001 %
_IMPLIED_GROWTH_ITERS = 60


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
class ImpliedGrowthResult:
    """Output of the reverse-DCF solve."""
    implied_growth: float         # the g that makes DCF = price, or bracket bound
    bracket_hit: bool             # True when price falls outside [-20 %, +60 %] range
    bracket_bound: Optional[str]  # "lower" | "upper" when bracket_hit, else None
    normalized_fcf: float         # the norm_fcf used in the solve
    assumptions: dict             # wacc, terminal_growth, projection_years (from config)
    lineage: str                  # human-readable description of what was solved


def _implied_growth_bisect(
    price: float,
    shares: float,
    net_debt: float,
    norm_fcf: float,
    wacc: float,
    g_term: float,
    n: int,
) -> Optional[ImpliedGrowthResult]:
    """
    Core bisection solve for a single (wacc, terminal_growth) bundle.
    Callers guarantee price/shares/norm_fcf are truthy and net_debt is not None.

    Bisection over [−20 %, +60 %]. Returns ImpliedGrowthResult with
    bracket_hit=True when price falls outside the bracket (reported, not
    crashed). Reuses two_stage_dcf as the forward model — not a reimplementation.
    """
    assump = {"wacc": wacc, "terminal_growth": g_term, "projection_years": n}

    def _fvps(g: float) -> Optional[float]:
        a = {"projection_years": n, "wacc": wacc, "terminal_growth": g_term, "fcf_growth": g}
        return two_stage_dcf(norm_fcf, net_debt, shares, None, "implied", a).fair_value_per_share

    def _f(g: float) -> Optional[float]:
        fv = _fvps(g)
        return (fv - price) if fv is not None else None

    f_low  = _f(_IMPLIED_GROWTH_LOWER)
    f_high = _f(_IMPLIED_GROWTH_UPPER)
    if f_low is None or f_high is None:
        return None

    # f is monotone increasing: fvps(g) increases with g.
    if f_low > 0:
        # fvps(−20 %) > price → company is cheap, implied g is below lower bracket
        return ImpliedGrowthResult(
            implied_growth=_IMPLIED_GROWTH_LOWER, bracket_hit=True, bracket_bound="lower",
            normalized_fcf=norm_fcf, assumptions=assump,
            lineage=f"bracket lower hit: fair value at g={_IMPLIED_GROWTH_LOWER:.0%} > price",
        )
    if f_high < 0:
        # fvps(+60 %) < price → price extremely expensive, implied g above upper bracket
        return ImpliedGrowthResult(
            implied_growth=_IMPLIED_GROWTH_UPPER, bracket_hit=True, bracket_bound="upper",
            normalized_fcf=norm_fcf, assumptions=assump,
            lineage=f"bracket upper hit: fair value at g={_IMPLIED_GROWTH_UPPER:.0%} < price",
        )

    lo, hi = _IMPLIED_GROWTH_LOWER, _IMPLIED_GROWTH_UPPER
    g_mid = (lo + hi) / 2.0
    for _ in range(_IMPLIED_GROWTH_ITERS):
        g_mid = (lo + hi) / 2.0
        if (hi - lo) < _IMPLIED_GROWTH_TOL:
            break
        fm = _f(g_mid)
        if fm is None:
            break
        if fm < 0:
            lo = g_mid   # fair value < price → need higher growth
        else:
            hi = g_mid   # fair value > price → need lower growth

    return ImpliedGrowthResult(
        implied_growth=g_mid, bracket_hit=False, bracket_bound=None,
        normalized_fcf=norm_fcf, assumptions=assump,
        lineage=f"bisection solved g={g_mid:.4%} (WACC={wacc:.1%}, terminal_g={g_term:.1%}, n={n})",
    )


def implied_growth(
    price: Optional[float],
    shares: Optional[float],
    net_debt: Optional[float],
    norm_fcf: Optional[float],
    config: dict,
) -> Optional[ImpliedGrowthResult]:
    """
    Reverse DCF: find the constant FCF growth rate g such that
    two_stage_dcf(norm_fcf, ..., fcf_growth=g).fair_value_per_share == price.

    WACC and terminal_growth are taken from config.valuation.dcf.scenarios.base —
    systematic assumptions that are never varied per company.  Only g is solved.

    Returns None when inputs are insufficient. See _implied_growth_bisect for
    the shared bisection core (also used by expectations_gap_band to solve
    the same reverse-DCF under the bull/bear bundles).
    """
    if not (price and price > 0):
        return None
    if not (shares and shares > 0):
        return None
    if net_debt is None:
        return None
    if norm_fcf is None:
        return None

    dcf_cfg = config.get("valuation", {}).get("dcf", {})
    base_sc  = dcf_cfg.get("scenarios", {}).get("base", {})
    wacc     = float(base_sc.get("wacc", 0.09))
    g_term   = float(base_sc.get("terminal_growth", 0.025))
    n        = int(dcf_cfg.get("projection_years", 5))

    return _implied_growth_bisect(price, shares, net_debt, norm_fcf, wacc, g_term, n)


_BAND_SCENARIOS = ("bull", "base", "bear")


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass
class ScenarioGapResult:
    """One scenario bundle's reverse-DCF solve and resulting expectations gap."""
    scenario: str                  # "bull" | "base" | "bear"
    wacc: float
    terminal_growth: float
    implied_growth: float          # solved g, or the clamped bracket bound
    gap: float                     # implied_growth - delivered_growth (always computed;
                                    # `converged` marks whether this is a real solve)
    converged: bool                # False when the bisection hit a bracket bound
    bracket_bound: Optional[str]   # "lower" | "upper" when not converged, else None


@dataclass
class ExpectationsGapBand:
    """
    The expectations gap computed under all three OWNED scenario bundles
    (bull/base/bear), rather than base alone. Only ever constructed when
    band_status is COMPLETE or PARTIAL — NO_BAND (base failed to converge,
    or a prerequisite input/delivered_growth is missing) means no object is
    built at all; today's single-scenario gap is what renders in that case.
    """
    band_status: str                        # "COMPLETE" | "PARTIAL"
    base_gap: float                         # byte-matches today's single-scenario gap
    delivered_growth: float
    scenarios: dict                         # {"bull": ScenarioGapResult, "base": ..., "bear": ...}
    fragile: str                            # "FRAGILE" | "STABLE" | "UNDETERMINABLE"


def expectations_gap_band(
    price: Optional[float],
    shares: Optional[float],
    net_debt: Optional[float],
    norm_fcf: Optional[float],
    delivered_growth: Optional[float],
    config: dict,
) -> Optional[ExpectationsGapBand]:
    """
    Runs the reverse-DCF bisection under each of the three owned scenario
    bundles (bull/base/bear — full (wacc, terminal_growth) pairs read from
    config.valuation.dcf.scenarios; no WACC-alone sweep, no new config keys).
    Delivered growth is scenario-invariant and is computed once by the
    caller, then subtracted three times.

    Returns None (no band at all) when:
      - any reverse-DCF prerequisite input is missing (mirrors implied_growth's
        own None-gating), or delivered_growth is None, or
      - a scenario bundle isn't configured, or
      - the base scenario itself fails to converge (bracket_hit) — identical
        to today's no-gap case; the band never manufactures signal where
        today's single gap does not exist.

    Raises AssertionError if a COMPLETE band violates the monotonicity
    invariant (bull <= base <= bear implied growth) — that is a computation
    bug, never a reporting choice.
    """
    if not (price and price > 0):
        return None
    if not (shares and shares > 0):
        return None
    if net_debt is None:
        return None
    if norm_fcf is None:
        return None
    if delivered_growth is None:
        return None

    dcf_cfg = config.get("valuation", {}).get("dcf", {})
    n = int(dcf_cfg.get("projection_years", 5))
    scenarios_cfg = dcf_cfg.get("scenarios", {})

    results: dict = {}
    for name in _BAND_SCENARIOS:
        sc = scenarios_cfg.get(name)
        if not sc:
            return None   # bundle not configured — no band possible
        wacc = float(sc.get("wacc", 0.09))
        g_term = float(sc.get("terminal_growth", 0.025))
        igr = _implied_growth_bisect(price, shares, net_debt, norm_fcf, wacc, g_term, n)
        if igr is None:
            return None
        results[name] = ScenarioGapResult(
            scenario=name, wacc=wacc, terminal_growth=g_term,
            implied_growth=igr.implied_growth,
            gap=igr.implied_growth - delivered_growth,
            converged=not igr.bracket_hit,
            bracket_bound=igr.bracket_bound,
        )

    base = results["base"]
    if not base.converged:
        return None   # base failed — identical to today's no-gap case

    converged_count = sum(1 for r in results.values() if r.converged)
    if converged_count == len(_BAND_SCENARIOS):
        bull_g, base_g, bear_g = (
            results["bull"].implied_growth, results["base"].implied_growth, results["bear"].implied_growth,
        )
        if not (bull_g <= base_g <= bear_g):
            raise AssertionError(
                "expectations_gap_band: monotonicity violated — implied growth must "
                f"satisfy bull <= base <= bear, got bull={bull_g:.4%} base={base_g:.4%} "
                f"bear={bear_g:.4%}"
            )
        band_status = "COMPLETE"
        signs = {_sign(r.gap) for r in results.values()}
        fragile = "FRAGILE" if len(signs) > 1 else "STABLE"
    else:
        band_status = "PARTIAL"
        fragile = "UNDETERMINABLE"

    return ExpectationsGapBand(
        band_status=band_status,
        base_gap=base.gap,
        delivered_growth=delivered_growth,
        scenarios=results,
        fragile=fragile,
    )


@dataclass
class RelativeValuation:
    pe: Optional[float]
    ev_ebitda: Optional[float]
    fcf_yield: Optional[float]
    peer_pe_median: Optional[float] = None
    peer_ev_ebitda_median: Optional[float] = None
    peer_fcf_yield_median: Optional[float] = None
