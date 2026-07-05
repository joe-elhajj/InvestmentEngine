"""
durability.py — deterministic, lineage-traced business-durability scorecard.

Five weighted categories measure how durable a business's competitive position
and capital-allocation discipline are, independent of current valuation.

Design rules
------------
- No LLM, no network calls.  Everything is computable from an AnalysisResult
  and an optional peer comp-set.
- Every sub-score carries: raw value, source lineage, and years it covers.
- Missing data → the metric is DROPPED and remaining weights renormalized.
  A gap is never scored as 0 or neutral.
- Financial issuers (SIC 6000–6799) are detected and rejected: no sub-scores,
  just an "excluded" flag.
- Runtime invariants are verified before any score is emitted (C1).
- Score bands quantify uncertainty from missing data (C2).
- Config hash enables cross-company comparability (C3).
- Stability perturbation flags reinvestment-rate sensitivity (C4).

Known open investigation (Session A Item 1 sweep, as of this writing): the
share-count discontinuity detector below (_detect_split_contamination) found
flagged boundaries for AAPL/TSLA/AMZN whose RATIOS match those companies'
real historical split ratios almost exactly, but whose fiscal years do NOT
match the real split dates. That "ratio right, year wrong" signature points
at engine/edgar.py's multi-year series assembly (likely comparative-column
splicing across filings) rather than confirming genuine unadjusted splits
landing on those exact years. Root cause under investigation in a follow-up
session (B.2) — not yet fixed, not yet fully diagnosed as of this comment.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from typing import Optional

from engine.pipeline import AnalysisResult, YearlyDerived
from engine import peers as P
from engine.universe import UniverseDistribution


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS: dict[str, float] = {
    "reinvestment_engine": 0.30,
    "quality_persistence": 0.25,
    "balance_sheet_resilience": 0.20,
    "capital_discipline": 0.15,
    "optionality_proxies": 0.10,
}

_DEFAULT_THRESHOLDS: dict[str, float] = {
    "roic_threshold": 0.15,          # years above this count as strong
    "cost_of_capital": 0.08,         # anchor for ROIC absolute curve
    "stability_delta_threshold": 5.0, # composite-point swing that flags unstable
}

_DEFAULT_SCORE_BAND: dict[str, float] = {
    "pessimistic_impute": 25.0,  # sub-score points (0-100 scale), not a decimal
    "optimistic_impute": 75.0,
}


def _merge_strict(dur: dict, section_name: str, defaults: dict[str, float]) -> dict:
    """
    Merge one durability.<section_name> sub-dict onto its defaults, failing
    loudly on any key the code doesn't consume (Session C Phase 1.5: closes
    the class of bug where config.yaml's on-disk threshold/score_band keys
    silently didn't match what _resolve_config's defaults expected, making
    five of ten durability assumptions dead on disk while docs/assumptions.md
    claimed they were live). A typo or stale key here must be loud, not a
    silent no-op.
    """
    section = dur.get(section_name, {})
    unknown = set(section) - set(defaults)
    if unknown:
        raise ValueError(
            f"config.yaml durability.{section_name} has unrecognized key(s) "
            f"{sorted(unknown)} — not consumed anywhere in engine/durability.py. "
            f"Expected keys: {sorted(defaults)}."
        )
    merged = dict(defaults)
    merged.update(section)
    return merged


def _resolve_config(cfg: dict) -> dict:
    """Merge user config with defaults; return fully populated durability config.

    The universe_version is folded in so scores computed against different
    universe snapshots produce different hashes and are not silently compared.
    """
    dur = cfg.get("durability", {})
    weights = _merge_strict(dur, "weights", _DEFAULT_WEIGHTS)
    thresholds = _merge_strict(dur, "thresholds", _DEFAULT_THRESHOLDS)
    score_band = _merge_strict(dur, "score_band", _DEFAULT_SCORE_BAND)
    universe_version = cfg.get("universe", {}).get("version", "unversioned")
    return {
        "weights": weights, "thresholds": thresholds, "score_band": score_band,
        "universe_version": universe_version,
    }


def _config_hash(resolved_cfg: dict) -> str:
    canonical = json.dumps(resolved_cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Sub-score data structures
# ---------------------------------------------------------------------------

@dataclass
class SubScore:
    name: str
    score: float            # 0–100
    raw: object             # the underlying value(s) used
    source: str             # lineage / fact description
    years_covered: list[int]


@dataclass
class CategoryScore:
    name: str
    sub_scores: list[SubScore]
    weight: float           # renormalized weight
    composite: float        # weighted mean of sub-scores (0–100)


@dataclass
class DurabilityScore:
    ticker: str
    composite: float                    # 0–100
    composite_low: float                # pessimistic band (C2)
    composite_high: float               # optimistic band (C2)
    categories: dict[str, CategoryScore]
    config_hash: str                    # C3
    data_completeness: float            # fraction of metrics that resolved
    is_stable: bool                     # C4: composite delta under threshold
    stability_delta: float              # composite swing from ±20% perturbation
    excluded: bool = False
    exclusion_reason: str = ""
    gaps: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scoring curves
# ---------------------------------------------------------------------------

def _roic_curve(roic: Optional[float], cost_of_capital: float) -> Optional[float]:
    """
    Piecewise-linear ROIC → 0-100.
      0  at ROIC ≤ 0
      50 at ROIC = cost_of_capital
      100 at ROIC ≥ 2 × cost_of_capital
    """
    if roic is None:
        return None
    if roic <= 0:
        return 0.0
    coc = cost_of_capital
    if roic >= 2 * coc:
        return 100.0
    if roic <= coc:
        return 50.0 * roic / coc
    return 50.0 + 50.0 * (roic - coc) / coc


def _trend_score(values: list[float]) -> Optional[float]:
    """
    Simple linear-regression slope normalised to 0-100.
    Positive slope → higher score; zero slope → 50; uncapped within ±50.
    Returns None if fewer than 2 points.
    """
    n = len(values)
    if n < 2:
        return None
    xs = list(range(n))
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(values)
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom == 0:
        return 50.0
    slope = sum((xs[i] - x_mean) * (values[i] - y_mean) for i in range(n)) / denom
    # normalise: ±1 unit/year → ±25 points; cap at 0-100
    score = 50.0 + slope * 25.0 / max(abs(y_mean), 1e-9)
    return max(0.0, min(100.0, score))


def _percentile_score(target: Optional[float], peers: list[float]) -> Optional[float]:
    """Map target's percentile within peers to 0-100."""
    rs = P.relative_score("_", target, peers)
    return rs.percentile  # already 0-100


def _cv_score(values: list[float]) -> Optional[float]:
    """
    Coefficient of variation → stability sub-score.
    CV=0 → 100 (perfectly stable); CV=1 → 0; linear between.
    """
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    if mean == 0:
        return None
    cv = statistics.pstdev(values) / abs(mean)
    return max(0.0, 100.0 * (1.0 - cv))


# ---------------------------------------------------------------------------
# Category scorers
# ---------------------------------------------------------------------------

def _score_reinvestment(
    annual: dict[str, YearlyDerived],
    coc: float,
    reinv_perturb: float = 0.0,
) -> list[SubScore]:
    """
    Category 1: Reinvestment engine.
    Metrics:
      - ROIC latest
      - ROIC multi-year mean
      - Reinvestment rate (smoothed)
      - Compounding proxy = mean ROIC × reinvestment rate
    """
    sub: list[SubScore] = []
    periods = sorted(annual)
    roic_vals = [(yd.year, yd.nopat, yd.invested_capital) for yd in annual.values()
                 if yd.nopat is not None and yd.invested_capital is not None and yd.invested_capital > 0]

    roic_by_year = [(y, n / ic) for y, n, ic in roic_vals]
    years_list = [y for y, _ in roic_by_year]
    roic_list  = [r for _, r in roic_by_year]

    # Latest ROIC
    if roic_list:
        latest_roic = roic_list[-1]
        s = _roic_curve(latest_roic, coc)
        if s is not None:
            sub.append(SubScore(
                name="roic_latest", score=s,
                raw=round(latest_roic, 4), source=f"NOPAT/IC period {periods[-1]}",
                years_covered=[years_list[-1]],
            ))

    # Multi-year mean ROIC
    if len(roic_list) >= 2:
        mean_roic = statistics.fmean(roic_list)
        s = _roic_curve(mean_roic, coc)
        if s is not None:
            sub.append(SubScore(
                name="roic_mean", score=s,
                raw=round(mean_roic, 4), source=f"mean ROIC over {len(roic_list)} years",
                years_covered=years_list,
            ))

    # Reinvestment rate: ΔIC / NOPAT, smoothed across years. Pair-gated per
    # entry (Session B.4 PR-2) -- both invested_capital AND nopat must
    # resolve on the SAME YearlyDerived entry to contribute, exactly like
    # roic_vals above. This closes a dormant bug: previously ic_vals
    # filtered only on invested_capital while nopat was looked up from a
    # SEPARATE dict keyed by integer fiscal_year -- so a second entry
    # sharing a `.year` label with a real entry (e.g. a tolerance-matched
    # near-anchor instant, mislabeled to the following fiscal year --
    # confirmed live for BE/AXON post-PR-1) could contribute its own
    # invested_capital to a delta paired against the REAL entry's nopat,
    # silently mixing two different periods under one year label. Filtering
    # the pair list itself on both fields makes that structurally
    # impossible: each list element is one YearlyDerived entry's own data.
    ic_nopat_vals = [
        (yd.year, yd.invested_capital, yd.nopat) for yd in annual.values()
        if yd.invested_capital is not None and yd.nopat is not None
    ]
    reinv_rates = []
    reinv_years = []
    for i in range(1, len(ic_nopat_vals)):
        _, ic_prev, _ = ic_nopat_vals[i - 1]
        y_curr, ic_curr, nopat_curr = ic_nopat_vals[i]
        if ic_prev > 0 and nopat_curr and nopat_curr > 0:
            delta_ic = ic_curr - ic_prev
            rr = delta_ic / nopat_curr
            reinv_rates.append(rr * (1.0 + reinv_perturb))
            reinv_years.append(y_curr)

    if reinv_rates:
        mean_rr = statistics.fmean(reinv_rates)
        # Ideal reinvestment rate ~40-80%; score peaks around 60%
        rr_score = max(0.0, min(100.0, 100.0 - abs(mean_rr - 0.60) * 100.0))
        sub.append(SubScore(
            name="reinvestment_rate", score=rr_score,
            raw=round(mean_rr, 4), source=f"ΔIC/NOPAT smoothed over {len(reinv_rates)} transitions",
            years_covered=reinv_years,
        ))

        # Compounding proxy: mean ROIC × mean reinvestment rate
        if roic_list:
            proxy = statistics.fmean(roic_list) * mean_rr
            proxy_score = max(0.0, min(100.0, proxy * 500.0))  # ~0.20 → 100
            sub.append(SubScore(
                name="compounding_proxy", score=proxy_score,
                raw=round(proxy, 4),
                source=f"mean ROIC ({statistics.fmean(roic_list):.2%}) × mean reinv rate ({mean_rr:.2%})",
                years_covered=reinv_years,
            ))

    return sub


def _score_quality(
    annual: dict[str, YearlyDerived],
    roic_threshold: float,
    universe_gross_margins: list[float],
) -> list[SubScore]:
    """Category 2: Quality persistence."""
    sub: list[SubScore] = []
    periods = sorted(annual)
    roic_vals = [(yd.year, yd.nopat / yd.invested_capital)
                 for yd in annual.values()
                 if yd.nopat is not None and yd.invested_capital is not None and yd.invested_capital > 0]
    roic_list  = [r for _, r in roic_vals]
    roic_years = [y for y, _ in roic_vals]

    if roic_list:
        # Years above threshold
        above = sum(1 for r in roic_list if r >= roic_threshold)
        frac = above / len(roic_list)
        sub.append(SubScore(
            name="roic_years_above_threshold", score=frac * 100.0,
            raw=f"{above}/{len(roic_list)}", source=f"ROIC≥{roic_threshold:.0%} across {len(roic_list)} years",
            years_covered=roic_years,
        ))

        # CV (volatility)
        cv_s = _cv_score(roic_list)
        if cv_s is not None:
            sub.append(SubScore(
                name="roic_stability_cv", score=cv_s,
                raw=round(statistics.pstdev(roic_list) / abs(statistics.fmean(roic_list)), 4),
                source=f"CV of ROIC over {len(roic_list)} years",
                years_covered=roic_years,
            ))

        # Trend
        ts = _trend_score(roic_list)
        if ts is not None:
            sub.append(SubScore(
                name="roic_trend", score=ts,
                raw=[round(r, 4) for r in roic_list], source=f"slope of ROIC over {len(roic_list)} years",
                years_covered=roic_years,
            ))

    # Gross margin
    gm_vals = [(yd.year, yd.gross_margin) for yd in annual.values() if yd.gross_margin is not None]
    gm_years = [y for y, _ in gm_vals]
    gm_list  = [g for _, g in gm_vals]
    if gm_list:
        # Universe-relative level (A4: ranks against the full S&P 500 distribution, not ad-hoc set)
        gm_pct = _percentile_score(gm_list[-1], universe_gross_margins)
        if gm_pct is not None:
            sub.append(SubScore(
                name="gross_margin_peer_percentile", score=gm_pct,
                raw=round(gm_list[-1], 4), source=f"gross margin at {periods[-1]} vs {len(universe_gross_margins)} universe peers",
                years_covered=[gm_years[-1]],
            ))
        # Trend
        ts = _trend_score(gm_list)
        if ts is not None:
            sub.append(SubScore(
                name="gross_margin_trend", score=ts,
                raw=[round(g, 4) for g in gm_list], source=f"slope of gross margin over {len(gm_list)} years",
                years_covered=gm_years,
            ))

    # Operating margin trend
    om_vals = [(yd.year, yd.operating_margin) for yd in annual.values() if yd.operating_margin is not None]
    om_years = [y for y, _ in om_vals]
    om_list  = [m for _, m in om_vals]
    if om_list:
        ts = _trend_score(om_list)
        if ts is not None:
            sub.append(SubScore(
                name="operating_margin_trend", score=ts,
                raw=[round(m, 4) for m in om_list], source=f"slope of operating margin over {len(om_list)} years",
                years_covered=om_years,
            ))

    return sub


def _score_resilience(annual: dict[str, YearlyDerived]) -> tuple[list[SubScore], list[str]]:
    """Category 3: Balance-sheet resilience.

    Returns (sub_scores, gaps). The gaps list carries disclosure-only
    findings that don't produce a SubScore -- mirroring how
    _detect_split_contamination's extra_gaps reach ds.gaps from outside a
    SubScore, this channel lets the net-cash/EBITDA<=0 case below disclose
    without inventing a score for a ratio that has no defined sign here.
    """
    sub: list[SubScore] = []
    gaps: list[str] = []
    periods = sorted(annual)
    if not periods:
        return sub, gaps
    latest = annual[periods[-1]]

    # Net debt / EBITDA
    nd = latest.net_debt
    ebitda = latest.ebitda
    if nd is not None and ebitda is not None:
        if ebitda > 0:
            ratio = nd / ebitda
            # net-cash company (ratio < 0) → best score (100)
            if ratio < 0:
                score = 100.0
            elif ratio > 8:
                score = 0.0
            else:
                score = max(0.0, 100.0 - ratio * 12.5)
            sub.append(SubScore(
                name="net_debt_ebitda", score=score,
                raw=round(ratio, 3), source=f"net_debt/EBITDA at {periods[-1]}",
                years_covered=[latest.year],
            ))
        elif nd > 0:
            # EBITDA <= 0 with net debt: the ratio's sign is undefined but the
            # risk is real and worse than any levered-but-profitable case --
            # floor to the worst score rather than silently dropping the metric.
            sub.append(SubScore(
                name="net_debt_ebitda", score=0.0,
                raw="EBITDA <= 0 with net debt — floored (worst-case debt service).",
                source=f"net_debt/EBITDA at {periods[-1]}",
                years_covered=[latest.year],
            ))
        else:
            # EBITDA <= 0 with net cash: outside the ratio's domain in the
            # other direction -- unlike the net-debt case there's no
            # worst-case direction to floor to, so disclose instead of score.
            gaps.append(
                "net_debt_ebitda: EBITDA <= 0 with net cash — outside ratio "
                "domain, not scored."
            )

    # Interest coverage — read Metric.note to distinguish "no debt" from "data missing"
    ebit = latest.ebit
    interest = latest.interest_expense
    # We call metrics.interest_coverage and inspect the result
    from engine import metrics as M
    ic_metric = M.interest_coverage(ebit, interest)
    if ic_metric.value is not None:
        v = ic_metric.value
        ic_score = min(100.0, max(0.0, (v / 20.0) * 100.0))   # 20× = perfect
        sub.append(SubScore(
            name="interest_coverage", score=ic_score,
            raw=round(v, 2), source=f"EBIT/interest at {periods[-1]}",
            years_covered=[latest.year],
        ))
    elif ic_metric.note == "no interest expense":
        # Debt-free: maximum sub-score
        sub.append(SubScore(
            name="interest_coverage", score=100.0,
            raw="no interest expense", source=f"no interest at {periods[-1]}",
            years_covered=[latest.year],
        ))
    # else: data missing → drop metric (do not append)

    # Count of negative-FCF years
    fcf_vals = [(yd.year, yd.fcf) for yd in annual.values() if yd.fcf is not None]
    if fcf_vals:
        neg_count = sum(1 for _, f in fcf_vals if f < 0)
        frac_neg = neg_count / len(fcf_vals)
        sub.append(SubScore(
            name="negative_fcf_years", score=max(0.0, 100.0 - frac_neg * 100.0),
            raw=f"{neg_count}/{len(fcf_vals)}",
            source=f"negative-FCF years over {len(fcf_vals)} years",
            years_covered=[y for y, _ in fcf_vals],
        ))

    return sub, gaps


def _detect_split_contamination(series: list[tuple[int, float]]) -> Optional[tuple[int, int]]:
    """
    Scans an as-filed (year, value) share-count series for a single-year jump
    too large to be real per-share issuance or buybacks — NVDA's diluted
    share count goes 2.535B (FY2022) -> 25.07B (FY2023), a ratio consistent
    with a 10-for-1 split, not 889% dilution. The exact cause of a flagged
    jump is NOT diagnosed by this function and must not be asserted by a
    caller: an Item 1 sweep across AAPL/TSLA/AMZN found flagged-boundary
    RATIOS that match those companies' real historical split ratios almost
    exactly, but fiscal years that do NOT match the real split dates —
    which is the signature of multi-filing comparative splicing (as-filed
    and later-restated points for the same nominal fiscal year merged into
    one series), not necessarily an unadjusted split or corporate action
    landing on that exact year. Root cause is under active investigation in
    a follow-up session; this function and its caller only know "this
    ratio is too large to be real dilution/buybacks," nothing more.

    Returns (year_before, year_after) of the FIRST such jump found (series
    sorted ascending internally, so callers don't have to guarantee order),
    or None when the series is clean. A ratio >=2x or <=0.5x between
    adjacent fiscal years is treated as a probable discontinuity — real
    single-year dilution/buyback swings of that magnitude are not a thing
    outside a restructuring event or a data-assembly artifact; RKLB's OWN
    genuine year-over-year dilution from FY2022 onward (466M -> 482M ->
    496M -> 531M, ~3-7%/yr) stays far under this threshold and is scored
    normally.

    Deliberately a standalone function, not inlined into
    _score_capital_discipline(), so detection has exactly one call site
    (engine.durability.score()) and one thing to unit-test.
    """
    ordered = sorted(series, key=lambda pair: pair[0])
    for (y0, v0), (y1, v1) in zip(ordered, ordered[1:]):
        if v0 <= 0:
            continue
        ratio = v1 / v0
        if ratio >= 2.0 or ratio <= 0.5:
            return (y0, y1)
    return None


def _score_capital_discipline(
    annual: dict[str, YearlyDerived],
    diluted_shares_series: list[tuple[int, float]],
) -> list[SubScore]:
    """Category 4: Capital discipline."""
    sub: list[SubScore] = []
    from engine import metrics as M

    # Diluted share-count CAGR (shrinking = good)
    if len(diluted_shares_series) >= 2:
        cagr_metric = M.cagr_over(diluted_shares_series, 5)
        if cagr_metric.value is not None:
            # Negative CAGR (buybacks) → high score; +5% growth → 0
            raw = cagr_metric.value
            score = max(0.0, min(100.0, 50.0 - raw * 1000.0))  # -5% → 100, 0% → 50, +5% → 0
            sub.append(SubScore(
                name="share_count_cagr", score=score,
                raw=round(raw, 5), source=f"diluted shares CAGR over {len(diluted_shares_series)} years",
                years_covered=[y for y, _ in diluted_shares_series],
            ))

    # SBC / revenue
    sbc_rev = [(yd.year, yd.sbc / yd.revenue)
               for yd in annual.values()
               if yd.sbc is not None and yd.revenue is not None and yd.revenue > 0]
    if sbc_rev:
        mean_ratio = statistics.fmean([r for _, r in sbc_rev])
        # < 2% → excellent (100); > 15% → poor (0)
        score = max(0.0, min(100.0, 100.0 - mean_ratio * 666.7))
        sub.append(SubScore(
            name="sbc_revenue_ratio", score=score,
            raw=round(mean_ratio, 4),
            source=f"SBC/revenue mean over {len(sbc_rev)} years",
            years_covered=[y for y, _ in sbc_rev],
        ))

    return sub


def _score_optionality(
    annual: dict[str, YearlyDerived],
    universe_capex_revenue: Optional[list[float]] = None,
    universe_rnd_revenue: Optional[list[float]] = None,
) -> list[SubScore]:
    """Category 5: Optionality proxies (label must say 'proxy').

    When universe distributions are provided, universe-relative percentile
    sub-scores are added alongside the absolute-curve sub-scores.
    """
    sub: list[SubScore] = []
    ucr = universe_capex_revenue or []
    urn = universe_rnd_revenue or []

    # R&D / revenue
    rnd_rev = [(yd.year, yd.rnd / yd.revenue)
               for yd in annual.values()
               if yd.rnd is not None and yd.revenue is not None and yd.revenue > 0]
    if rnd_rev:
        vals = [r for _, r in rnd_rev]
        years = [y for y, _ in rnd_rev]
        mean_ratio = statistics.fmean(vals)
        # treat 10–20% as ideal (absolute curve)
        score = max(0.0, min(100.0, mean_ratio * 500.0))
        sub.append(SubScore(
            name="rnd_revenue_proxy", score=score,
            raw=round(mean_ratio, 4), source=f"R&D/revenue mean over {len(vals)} years",
            years_covered=years,
        ))
        # Universe percentile: higher R&D/revenue → more investment in future optionality
        if urn:
            pct = _percentile_score(mean_ratio, urn)
            if pct is not None:
                sub.append(SubScore(
                    name="rnd_revenue_universe_pct", score=pct,
                    raw=round(mean_ratio, 4),
                    source=f"R&D/revenue vs {len(urn)} universe peers",
                    years_covered=years,
                ))
        ts = _trend_score(vals)
        if ts is not None:
            sub.append(SubScore(
                name="rnd_trend_proxy", score=ts,
                raw=[round(v, 4) for v in vals], source=f"slope of R&D/revenue over {len(vals)} years",
                years_covered=years,
            ))

    # Capex / revenue
    cx_rev = [(yd.year, yd.capex / yd.revenue)
              for yd in annual.values()
              if yd.capex is not None and yd.revenue is not None and yd.revenue > 0]
    if cx_rev:
        cx_years = [y for y, _ in cx_rev]
        mean_ratio = statistics.fmean([r for _, r in cx_rev])
        # high capex is a mixed signal; moderate ~5% → good (absolute curve)
        score = max(0.0, min(100.0, 100.0 - abs(mean_ratio - 0.05) * 500.0))
        sub.append(SubScore(
            name="capex_revenue_proxy", score=score,
            raw=round(mean_ratio, 4), source=f"capex/revenue mean over {len(cx_rev)} years",
            years_covered=cx_years,
        ))
        # Universe percentile: higher capex → more physical investment (reinvestment optionality)
        if ucr:
            pct = _percentile_score(mean_ratio, ucr)
            if pct is not None:
                sub.append(SubScore(
                    name="capex_revenue_universe_pct", score=pct,
                    raw=round(mean_ratio, 4),
                    source=f"capex/revenue vs {len(ucr)} universe peers",
                    years_covered=cx_years,
                ))

    return sub


# ---------------------------------------------------------------------------
# Composite assembly with renormalization (C1, C2)
# ---------------------------------------------------------------------------

def _category_composite(sub_scores: list[SubScore]) -> float:
    if not sub_scores:
        return 50.0  # no data → neutral before renorm drops it
    return statistics.fmean([s.score for s in sub_scores])


def _compute_composite(
    cat_scores: dict[str, list[SubScore]],
    raw_weights: dict[str, float],
    impute: Optional[float] = None,
) -> tuple[float, dict[str, CategoryScore], float]:
    """
    Returns (composite, category_map, data_completeness).
    impute=None  → only categories with ≥1 sub-score contribute (renormalize).
    impute=float → missing sub-scores use that constant (for band calculations).
    """
    active: dict[str, tuple[float, list[SubScore]]] = {}
    for cat, subs in cat_scores.items():
        if subs:
            active[cat] = (raw_weights.get(cat, 0.0), subs)
        elif impute is not None:
            # synthetic placeholder
            fake = [SubScore(name="_imputed", score=impute, raw=None, source="imputed", years_covered=[])]
            active[cat] = (raw_weights.get(cat, 0.0), fake)

    total_w = sum(w for w, _ in active.values())
    if total_w == 0:
        return 50.0, {}, 0.0

    composite = 0.0
    cat_map: dict[str, CategoryScore] = {}
    for cat, (w, subs) in active.items():
        norm_w = w / total_w
        cat_comp = _category_composite(subs)
        composite += norm_w * cat_comp
        cat_map[cat] = CategoryScore(name=cat, sub_scores=subs, weight=norm_w, composite=cat_comp)

    all_cats = set(cat_scores) | set(active)
    has_data = sum(1 for c in all_cats if cat_scores.get(c))
    completeness = has_data / max(len(all_cats), 1)
    return composite, cat_map, completeness


# ---------------------------------------------------------------------------
# Runtime invariants (C1)
# ---------------------------------------------------------------------------

def _check_invariants(score: DurabilityScore) -> None:
    """Raise ValueError naming the violated invariant if any check fails."""
    for cat in score.categories.values():
        for ss in cat.sub_scores:
            if not (0.0 <= ss.score <= 100.0):
                raise ValueError(
                    f"Invariant violated: sub-score '{ss.name}' = {ss.score} not in [0, 100]"
                )

    weight_sum = sum(c.weight for c in score.categories.values())
    if abs(weight_sum - 1.0) > 1e-9 and score.categories:
        raise ValueError(
            f"Invariant violated: renormalized weights sum to {weight_sum:.10f}, not 1.0"
        )

    if not (0.0 < score.data_completeness <= 1.0):
        raise ValueError(
            f"Invariant violated: data_completeness = {score.data_completeness} not in (0, 1]"
        )

    # Independently recompute composite
    recomputed = sum(c.weight * c.composite for c in score.categories.values())
    if score.categories and abs(recomputed - score.composite) > 1e-9:
        raise ValueError(
            f"Invariant violated: composite {score.composite:.6f} != "
            f"independently recomputed {recomputed:.6f}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_FINANCIAL_SIC_RANGE = (6000, 6799)


def score(
    res: AnalysisResult,
    config: dict,
    universe: Optional[UniverseDistribution] = None,
    override_classification: Optional[str] = None,
) -> DurabilityScore:
    """
    Compute the durability scorecard for a company.

    Parameters
    ----------
    res                     : output of pipeline.derive()
    config                  : full config dict (durability section is optional)
    universe                : pre-built UniverseDistribution for peer-relative percentile
                              sub-scores.  When None, percentile sub-scores that require a
                              population (gross margin, capex/R&D intensity) are omitted and
                              the remaining weights renormalize.  Tests inject synthetic
                              distributions; the universe build never runs in CI.
    override_classification : analyst-owned override from config.classification.overrides.
                              When set to "operating", the financial-SIC exclusion is bypassed.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    dcfg = _resolve_config(config)
    cfg_hash = _config_hash(dcfg)
    weights = dcfg["weights"]
    thresholds = dcfg["thresholds"]
    score_band = dcfg["score_band"]
    coc = thresholds["cost_of_capital"]
    roic_thresh = thresholds["roic_threshold"]
    stability_delta_thresh = thresholds["stability_delta_threshold"]

    ticker = res.company.ticker

    # Financial-issuer exclusion — bypassed when analyst override is "operating"
    try:
        sic_int = int(res.company.sic)
        if _FINANCIAL_SIC_RANGE[0] <= sic_int <= _FINANCIAL_SIC_RANGE[1]:
            if override_classification == "operating":
                _logger.info(
                    "%s: financial SIC %d exclusion bypassed by analyst override", ticker, sic_int
                )
            else:
                return DurabilityScore(
                    ticker=ticker, composite=0.0, composite_low=0.0, composite_high=0.0,
                    categories={}, config_hash=cfg_hash,
                    data_completeness=0.0, is_stable=True, stability_delta=0.0,
                    excluded=True,
                    exclusion_reason=f"financial issuer SIC {sic_int} (6000–6799) — not scored",
                )
    except (ValueError, TypeError):
        pass

    annual = res.annual_series

    # Pull per-metric distributions from the universe object (empty lists when no universe)
    uni_gm  = universe.get("gross_margin")        if universe else []
    uni_cx  = universe.get("capex_revenue_ratio") if universe else []
    uni_rnd = universe.get("rnd_revenue_ratio")   if universe else []

    # Diluted shares series (from cd.series, populated by edgar)
    diluted_series = [
        (f.fiscal_year, f.value)
        for f in res.company.series.get("diluted_shares", [])
    ]

    # Share-count discontinuity check (Session A/Item 1): a single-year jump
    # in the raw diluted-share series reads as extreme "dilution" to a plain
    # CAGR, floor-clamping share_count_cagr to 0 for reasons that have
    # nothing to do with capital discipline. Reject-and-gap, not infer-and-
    # adjust (Joe's call): the contaminated series is dropped entirely
    # (empty list -> _score_capital_discipline's own `len(...) >= 2` check
    # naturally omits the sub-score — absent, not a fabricated 0), and the
    # omission is logged as an explicit gap naming the probable
    # discontinuity so it's discoverable later, not silently missing.
    # The Item 1 sweep found flagged boundaries whose RATIOS match known
    # split ratios for AAPL/TSLA/AMZN but whose fiscal years do NOT match
    # those companies' real split dates — evidence pointing at multi-filing
    # comparative splicing in engine/edgar.py's series assembly rather than
    # (or in addition to) genuine unadjusted corporate actions. Session B.2
    # Phase 1 confirmed this mechanism against raw companyfacts JSON for
    # AAPL/NVDA/AMZN (exact ratio matches landing at the wrong fiscal-year
    # boundary). Per the B.2 Option 1 decision, _annual_points()'s
    # prefer-latest-filed selection is NOT changed here — the gap message
    # below names the diagnosed mechanism as the likely cause (without
    # over-asserting certainty) and cites the two seam filings via
    # Fact.source_ref() so the claim is independently checkable.
    # Known limitation (backlog, not fixed here): this under-credits
    # genuine split/restructured companies on discipline relative to
    # identical peers without one — the category composite renormalizes
    # over one fewer sub-score instead of crediting real buyback behavior.
    # Resolving that requires an owned split/corporate-actions table
    # (Session C), not a heuristic guess at the adjustment factor.
    extra_gaps: list[str] = []
    split_jump = _detect_split_contamination(diluted_series)
    if split_jump is not None:
        y0, y1 = split_jump
        diluted_facts = res.company.series.get("diluted_shares", [])
        f0 = next((f for f in diluted_facts if f.fiscal_year == y0), None)
        f1 = next((f for f in diluted_facts if f.fiscal_year == y1), None)
        seam_ref = (
            f" — seam filings: FY{y0} from {f0.source_ref()}, FY{y1} from {f1.source_ref()}"
            if f0 is not None and f1 is not None else ""
        )
        extra_gaps.append(
            f"share_count_cagr: probable share-count discontinuity (likely a "
            f"filing-vintage splice — later comparatives split-adjusted, older "
            f"years stranded pre-split — or a genuine unadjusted split) "
            f"between FY{y0} and FY{y1} — unadjusted "
            "diluted-share series rejected, not scored as dilution"
            f"{seam_ref}"
        )
        diluted_series_for_scoring: list[tuple[int, float]] = []
    else:
        diluted_series_for_scoring = diluted_series

    resilience_sub, resilience_gaps = _score_resilience(annual)
    extra_gaps.extend(resilience_gaps)

    cat_scores: dict[str, list[SubScore]] = {
        "reinvestment_engine": _score_reinvestment(annual, coc),
        "quality_persistence": _score_quality(annual, roic_thresh, uni_gm),
        "balance_sheet_resilience": resilience_sub,
        "capital_discipline": _score_capital_discipline(annual, diluted_series_for_scoring),
        "optionality_proxies": _score_optionality(annual, uni_cx, uni_rnd),
    }

    composite, cat_map, completeness = _compute_composite(cat_scores, weights)

    # No metrics scored at all — return exclusion rather than violating the (0, 1] invariant
    if completeness == 0.0:
        return DurabilityScore(
            ticker=ticker,
            composite=50.0, composite_low=25.0, composite_high=75.0,
            categories={}, config_hash=cfg_hash,
            data_completeness=0.0, is_stable=True, stability_delta=0.0,
            excluded=True,
            exclusion_reason="no scoreable metrics after data filtering",
            gaps=list(res.gaps) + extra_gaps,
        )

    # Score band (C2) — impute pessimistic / optimistic for missing metrics
    composite_low, _, _  = _compute_composite(cat_scores, weights, impute=score_band["pessimistic_impute"])
    composite_high, _, _ = _compute_composite(cat_scores, weights, impute=score_band["optimistic_impute"])

    # Stability perturbation (C4): ±20% on reinvestment rate
    reinv_plus  = _score_reinvestment(annual, coc, reinv_perturb=+0.20)
    reinv_minus = _score_reinvestment(annual, coc, reinv_perturb=-0.20)
    cat_plus  = dict(cat_scores); cat_plus["reinvestment_engine"]  = reinv_plus
    cat_minus = dict(cat_scores); cat_minus["reinvestment_engine"] = reinv_minus
    c_plus,  _, _ = _compute_composite(cat_plus,  weights)
    c_minus, _, _ = _compute_composite(cat_minus, weights)
    stability_delta = abs(c_plus - c_minus)
    is_stable = stability_delta <= stability_delta_thresh

    result = DurabilityScore(
        ticker=ticker,
        composite=composite,
        composite_low=composite_low,
        composite_high=composite_high,
        categories=cat_map,
        config_hash=cfg_hash,
        data_completeness=completeness,
        is_stable=is_stable,
        stability_delta=stability_delta,
        gaps=list(res.gaps) + extra_gaps,
    )

    _check_invariants(result)
    return result
