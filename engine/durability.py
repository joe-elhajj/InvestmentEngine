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
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import statistics
from dataclasses import dataclass, field
from typing import Optional

from engine.config import DEFAULT_ASSUMED_TAX_RATE, DEFAULT_MIN_HISTORY_YEARS
from engine.pipeline import (
    AnalysisResult,
    RND_CAPITALIZATION_DEFAULTS,
    RndRegime,
    YearlyDerived,
    rnd_regime_applies,
)
from engine import metrics as M
from engine import peers as P
from engine.edgar import classify_rnd_series
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

# Imported, not redefined -- engine.pipeline.RND_CAPITALIZATION_DEFAULTS is
# the single source of truth this section merges onto; rnd_regime_applies
# (also in pipeline.py) reads its `enabled` fallback from the same object.
_DEFAULT_RND_CAPITALIZATION = RND_CAPITALIZATION_DEFAULTS

# Gates ship with NO defaults baked in here -- unlike weights/thresholds/
# score_band/rnd_capitalization above, a gate's threshold/cap ARE owned
# assumptions (docs/assumptions.md) that must live in config.yaml, not as a
# code fallback. An empty gates list (the default when the section is
# omitted) means no gate runs -- backward-compatible for any config that
# predates this PR.
_GATE_REQUIRED_KEYS = {"id", "metric", "threshold", "cap"}
_GATE_SUPPORTED_METRICS = {"net_debt_ebitda"}


def _resolve_gates(dur: dict) -> list[dict]:
    """
    Strict, loud validation of durability.gates (a LIST, so _merge_strict's
    flat-dict merge doesn't apply) -- same "typo must be loud, not a silent
    no-op" philosophy as _merge_strict above.
    """
    gates_raw = dur.get("gates", [])
    if not isinstance(gates_raw, list):
        raise ValueError("config.yaml durability.gates must be a list")
    gates: list[dict] = []
    for g in gates_raw:
        if not isinstance(g, dict):
            raise ValueError(f"config.yaml durability.gates entry {g!r} must be a mapping")
        missing = _GATE_REQUIRED_KEYS - set(g)
        unknown = set(g) - _GATE_REQUIRED_KEYS
        if missing or unknown:
            raise ValueError(
                f"config.yaml durability.gates entry {g!r} is malformed "
                f"(missing: {sorted(missing)}, unrecognized: {sorted(unknown)}). "
                f"Expected keys: {sorted(_GATE_REQUIRED_KEYS)}."
            )
        if g["metric"] not in _GATE_SUPPORTED_METRICS:
            raise ValueError(
                f"config.yaml durability.gates entry {g['id']!r} has unsupported "
                f"metric {g['metric']!r}. Supported: {sorted(_GATE_SUPPORTED_METRICS)}."
            )
        gates.append(dict(g))
    return gates


def _merge_strict(dur: dict, section_name: str, defaults: dict) -> dict:
    """Merge section defaults, rejecting unknown keys rather than silently ignoring them."""
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
    rnd_capitalization = _merge_strict(dur, "rnd_capitalization", _DEFAULT_RND_CAPITALIZATION)
    gates = _resolve_gates(dur)
    universe_version = cfg.get("universe", {}).get("version", "unversioned")
    return {
        "weights": weights, "thresholds": thresholds, "score_band": score_band,
        "rnd_capitalization": rnd_capitalization,
        "gates": gates,
        "universe_version": universe_version,
        # External valuation inputs used by durability disclosures and annual ROIC.
        "valuation": {
            "min_history_years": cfg.get("valuation", {}).get("min_history_years", DEFAULT_MIN_HISTORY_YEARS),
            "assumed_tax_rate": cfg.get("valuation", {}).get("assumed_tax_rate", DEFAULT_ASSUMED_TAX_RATE),
        },
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
    # Balance-sheet gates cap the weighted composite.
    # composite_ungated is the true weighted composite BEFORE any gate cap
    # -- always populated (equal to `composite` itself when no gate fires),
    # so "both gated and ungated numbers survive" holds unconditionally,
    # not only in the gated case. None only in the excluded/no-metrics
    # early-return paths, which never reach gate evaluation at all.
    composite_ungated: Optional[float] = None
    gated: bool = False
    gate_ids: list[str] = field(default_factory=list)
    gate_lineage: str = ""
    gate_untestable_ids: list[str] = field(default_factory=list)
    gate_untestable_lineage: str = ""


@dataclass
class GateOutcome:
    """Internal result of evaluating one durability.gates config entry
    against a company's latest raw metrics -- never a public API type,
    folded into DurabilityScore's gate_* fields by score()."""
    gate_id: str
    status: str              # "GATED" | "UNTESTABLE"
    cap: Optional[float] = None      # set only when status == "GATED"
    reason: str = ""                 # human-readable clause, no ungated value yet
    gap_text: Optional[str] = None   # set only when status == "UNTESTABLE"


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
# R&D-capitalization regime: reinvestment_engine's adjusted-ROIC view
# ---------------------------------------------------------------------------

def _rnd_adjusted_view(annual: dict[str, YearlyDerived]) -> dict[str, YearlyDerived]:
    """Return R&D-adjusted NOPAT and invested capital with per-year GAAP fallback.

    This view supplies single-year roic_latest, preserving a value when its
    research-asset window is incomplete. Multi-year adjusted averages use the
    matched-window view instead. score() controls regime eligibility.
    """
    view: dict[str, YearlyDerived] = {}
    for pe, yd in annual.items():
        nopat_adj, ic_adj = M.rnd_adjusted_nopat_and_ic(
            yd.nopat, yd.invested_capital, yd.research_asset, yd.rnd
        )
        if nopat_adj is not None and ic_adj is not None:
            view[pe] = dataclasses.replace(yd, nopat=nopat_adj, invested_capital=ic_adj)
        else:
            view[pe] = yd
    return view


def _rnd_matched_window_view(annual: dict[str, YearlyDerived]) -> dict[str, YearlyDerived]:
    """Exclude incomplete R&D windows from multi-year adjusted averages.

    Set NOPAT and invested capital to None for excluded years so GAAP fallback
    cannot enter an adjusted average. Requires at least one adjusted year;
    otherwise raise, since the caller must retain full-history GAAP scoring.
    """
    if not any(yd.rnd_basis == "adjusted" for yd in annual.values()):
        raise AssertionError(
            "_rnd_matched_window_view called with zero adjusted years across "
            "the whole series -- caller must route no-adjustment-path "
            "companies through the full-history GAAP path instead of "
            "building an all-excluded matched window"
        )
    view: dict[str, YearlyDerived] = {}
    for pe, yd in annual.items():
        nopat_adj, ic_adj = M.rnd_adjusted_nopat_and_ic(
            yd.nopat, yd.invested_capital, yd.research_asset, yd.rnd
        )
        if nopat_adj is not None and ic_adj is not None:
            view[pe] = dataclasses.replace(yd, nopat=nopat_adj, invested_capital=ic_adj)
        else:
            view[pe] = dataclasses.replace(yd, nopat=None, invested_capital=None)
    return view


def _rnd_matched_window_gaap_mean(annual: dict[str, YearlyDerived], matched_years: set) -> Optional[float]:
    """
    GAAP-basis ROIC mean over the EXACT SAME year-set that fed the
    R&D-adjusted mean (`matched_years`, derived once by the caller from
    the same classify_basis_mix pass that also drives the tripwire/
    short-history disclosures -- never re-selected here, so the two means
    cannot drift apart onto different windows). For delta-comparison
    lineage only (docs/assumptions.md: "delta isolates one cause") -- not
    a new displayed report row.
    """
    vals = [
        yd.nopat / yd.invested_capital for yd in annual.values()
        if yd.year in matched_years and yd.nopat is not None
        and yd.invested_capital is not None and yd.invested_capital > 0
    ]
    if not vals:
        return None
    return statistics.fmean(vals)


def _annotate_matched_window_gaap_mean(
    sub_scores: list[SubScore], annual: dict[str, YearlyDerived],
) -> list[SubScore]:
    """
    Appends the matched-window GAAP-basis mean to roic_mean's lineage, for
    delta-comparison (docs/assumptions.md: "delta isolates one cause") --
    not a new displayed report row. The window is exactly roic_mean's own
    years_covered (already restricted to adjusted-only years by
    _rnd_matched_window_view), so this can never select a different
    year-set than the adjusted mean itself came from -- one
    window-selection pass, read here rather than re-derived.
    """
    annotated: list[SubScore] = []
    for sub in sub_scores:
        if sub.name != "roic_mean":
            annotated.append(sub)
            continue
        matched_years = set(sub.years_covered)
        gaap_mean = _rnd_matched_window_gaap_mean(annual, matched_years)
        if gaap_mean is not None:
            annotated.append(dataclasses.replace(
                sub, source=f"{sub.source} — matched-window GAAP-basis mean: {gaap_mean:.2%}"
            ))
        else:
            annotated.append(sub)
    return annotated


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

    # Reinvestment rate: smooth ΔIC / NOPAT across years. Require both inputs
    # on the same annual entry to avoid mixing periods with a shared year label.
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


def _annotate_rnd_basis(
    sub_scores: list[SubScore], annual: dict[str, YearlyDerived], rnd_state: str,
) -> tuple[list[SubScore], list[str]]:
    """Annotate average ROIC and compounding scores when the R&D regime is active.

    Use contributing years' invariant rnd_basis tags for both lineage and gaps.
    Mixed bases always produce a gap; wholly unadjusted bases produce one only
    when R&D data exists. Clean bases and companies with no R&D remain silent.
    Returns (annotated_sub_scores, gap_strings).
    """
    basis_by_year = {yd.year: yd.rnd_basis for yd in annual.values()}
    annotated: list[SubScore] = []
    gaps: list[str] = []
    for sub in sub_scores:
        if sub.name not in ("roic_mean", "compounding_proxy"):
            annotated.append(sub)
            continue
        bases = [b for y in sub.years_covered if (b := basis_by_year.get(y)) is not None]
        cls, n_adj, n_fb = M.classify_basis_mix(bases)
        if cls == "clean":
            note = f"all {n_adj} years R&D-adjusted"
        elif cls == "mixed":
            note = f"{n_adj} R&D-adjusted + {n_fb} GAAP-fallback years (mixed basis)"
        else:
            note = f"all {n_fb} years GAAP basis"
        annotated.append(dataclasses.replace(sub, source=f"{sub.source} — {note}"))

        if cls == "mixed":
            gaps.append(
                f"reinvestment_engine: {sub.name} mixed basis "
                f"({n_adj} R&D-adj + {n_fb} GAAP-fallback)"
            )
        elif cls == "unadjusted" and rnd_state != "no_rnd":
            gaps.append(
                f"reinvestment_engine: {sub.name} regime enabled but 0 of "
                f"{n_fb} years R&D-adjusted (GAAP basis only)"
            )
    return annotated, gaps


def _merge_rnd_reinvestment_views(
    latest_view_subs: list[SubScore], matched_view_subs: list[SubScore],
) -> list[SubScore]:
    """
    roic_latest comes from the Option-A (fallback-inclusive) view --
    unchanged single-year behavior, since "mixed basis" never applied to
    a single year and a short-history company's latest year should still
    get a value rather than disappearing under the matched-window
    restriction. Every other sub-score (roic_mean, reinvestment_rate,
    compounding_proxy) comes from the Option-C (matched-window) view.
    _score_reinvestment is called twice, its own code unchanged either
    time -- this function only picks the right result per sub-score name,
    in the same order _score_reinvestment itself would have emitted them.
    """
    by_name_latest = {s.name: s for s in latest_view_subs}
    by_name_matched = {s.name: s for s in matched_view_subs}
    merged: list[SubScore] = []
    if "roic_latest" in by_name_latest:
        merged.append(by_name_latest["roic_latest"])
    for name in ("roic_mean", "reinvestment_rate", "compounding_proxy"):
        if name in by_name_matched:
            merged.append(by_name_matched[name])
    return merged


def _annotate_rnd_short_history(
    sub_scores: list[SubScore], annual: dict[str, YearlyDerived], min_history_years: int,
) -> list[str]:
    """Disclose adjusted averages supported by fewer than min_history_years.

    Count contributing years using the same basis classifier as lineage
    annotation. No adjusted years means no short-adjusted-history disclosure.
    """
    basis_by_year = {yd.year: yd.rnd_basis for yd in annual.values()}
    gaps: list[str] = []
    for sub in sub_scores:
        if sub.name not in ("roic_mean", "compounding_proxy"):
            continue
        bases = [b for y in sub.years_covered if (b := basis_by_year.get(y)) is not None]
        _cls, n_adj, n_fb = M.classify_basis_mix(bases)
        total_eligible = n_adj + n_fb
        if 0 < n_adj < min_history_years:
            gaps.append(
                f"reinvestment_engine: adjusted-window {sub.name} rests on "
                f"{n_adj} of {total_eligible} available years "
                f"(below min_history_years={min_history_years})"
            )
    return gaps


def _history_window_gaps(
    cat_scores: dict[str, list[SubScore]], annual: dict[str, YearlyDerived],
) -> list[str]:
    """Disclose filtered history without imposing a new scoring lookback.

    These metrics use all usable annual observations, not a fixed-year window.
    Compare their existing provenance with the supplied annual history; for
    reinvestment, years_covered identifies transition end years (N-1 possible).
    Use distinct chronological year labels: callers need not preserve insertion
    order, and multiple period entries can share a fiscal-year label.
    Omitted sub-scores retain their existing abstention behavior.
    """
    metrics = {
        "gross_margin_trend", "operating_margin_trend", "roic_stability_cv",
        "roic_trend", "sbc_revenue_ratio", "capex_revenue_proxy",
        "rnd_revenue_proxy", "rnd_trend_proxy", "reinvestment_rate",
    }
    years = sorted({yd.year for yd in annual.values()})
    gaps = []
    for subs in cat_scores.values():
        for sub in subs:
            if sub.name not in metrics:
                continue
            transitions = sub.name == "reinvestment_rate"
            expected = years[1:] if transitions else years
            covered = set(sub.years_covered) & set(expected)
            omitted = sorted(set(expected) - covered)
            if omitted:
                unit = "transitions" if transitions else "annual observations"
                if len(years) != len(annual) or len(set(sub.years_covered)) != len(sub.years_covered):
                    unit = "distinct transition end years" if transitions else "distinct fiscal years"
                label = "transition end years" if transitions else "years"
                gaps.append(
                    f"{sub.name}: shortened history — uses {len(covered)} "
                    f"of {len(expected)} available {unit}; omitted {label}: "
                    + ", ".join(map(str, omitted))
                    + ". Score uses remaining observations."
                )
    return gaps


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


# ---------------------------------------------------------------------------
# Gate layer: raw-metric veto on top of the weighted composite
# ---------------------------------------------------------------------------

def _evaluate_gate(gate: dict, annual: dict[str, YearlyDerived]) -> Optional[GateOutcome]:
    """
    Evaluate one durability.gates config entry against the latest
    YearlyDerived's RAW metrics -- the SAME latest.net_debt / latest.ebitda
    _score_resilience above reads, at the same latest period (sorted(annual)
    last entry). Gates key on raw metrics, never sub-scores: a sub-score's
    curve/saturation already lies between the analyst's threshold belief and
    the number it fires on; the raw metric is what the anchor in
    docs/assumptions.md is written in.

    Returns None for NOT APPLICABLE (net cash -- no leverage risk exists) or
    PASS (ratio at or under threshold). Returns a GateOutcome for GATED or
    UNTESTABLE otherwise. Decision order is load-bearing (net-cash checked
    BEFORE the negative-EBITDA case) -- see docstring order in the caller.
    """
    if gate["metric"] != "net_debt_ebitda":
        raise ValueError(f"_evaluate_gate: unsupported metric {gate['metric']!r}")

    periods = sorted(annual)
    if not periods:
        return None
    latest = annual[periods[-1]]
    nd, ebitda = latest.net_debt, latest.ebitda
    gate_id = gate["id"]
    threshold = float(gate["threshold"])
    cap = float(gate["cap"])

    # 1. Net cash: no leverage risk exists -- category error to gate it.
    if nd is not None and nd < 0:
        return None

    # 2. Absence-is-not-zero: a missing input is UNTESTABLE, never a pass.
    if nd is None or ebitda is None:
        missing = "net_debt" if nd is None else "ebitda"
        return GateOutcome(
            gate_id=gate_id, status="UNTESTABLE",
            gap_text=(
                f"{gate_id}: net_debt/EBITDA gate untestable — {missing} "
                f"unavailable at {periods[-1]}"
            ),
        )

    # 3. Positive net debt, EBITDA <= 0: strictly worse than any high ratio
    #    (owes money, no earnings to service it) -- the ratio would compute
    #    negative here and falsely read as "below threshold", so this must
    #    be checked BEFORE the ratio comparison, not fall through to it.
    if nd > 0 and ebitda <= 0:
        return GateOutcome(
            gate_id=gate_id, status="GATED", cap=cap,
            reason="net debt positive with EBITDA <= 0 (cannot service debt)",
        )

    # 4. Over-levered.
    ratio = nd / ebitda
    if ratio > threshold:
        return GateOutcome(
            gate_id=gate_id, status="GATED", cap=cap,
            reason=f"net_debt/ebitda {ratio:.1f} > {threshold:.1f}",
        )

    # 5. Pass.
    return None


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
    """Return the first probable share-count discontinuity, or None.

    Sort by year and flag consecutive observations with a ratio >=2 or <=0.5,
    skipping nonpositive starting values. A flagged jump does not establish
    its cause: filing-vintage splicing and corporate actions can both produce it.
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


def gate_status_of(ds: "DurabilityScore") -> tuple[Optional[str], str]:
    """
    Pure: (gate_status, tooltip) for a DurabilityScore -- "GATED" |
    "UNTESTABLE" | None (no chip, either PASS or NOT APPLICABLE, which are
    indistinguishable from the outside and both render nothing). GATED
    takes priority when a hypothetical future gate list has one gate fire
    and another go untestable on the same ticker -- the actionable,
    already-resolved veto is shown over an open question. Single source of
    truth so report_html.py / screen.py / report.py never each reimplement
    this priority rule.
    """
    if ds.gated:
        return "GATED", ds.gate_lineage
    if ds.gate_untestable_ids:
        return "UNTESTABLE", ds.gate_untestable_lineage
    return None, ""


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

    # Reject discontinuous share series rather than infer split adjustments.
    # Omit the dilution sub-score and disclose the seam filings as provenance.
    # This can omit genuine buyback information; remaining scores are renormalized.
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

    # Abstain from R&D adjustment for FPIs: IAS 38 already capitalizes development
    # costs to an unknown degree. Re-evaluate eligibility with this scoring config
    # rather than reuse res.rnd_regime, which may reflect a different config.
    use_rnd_adjusted_roic = rnd_regime_applies(res.company, config) is RndRegime.APPLIES
    matched_view = annual

    if use_rnd_adjusted_roic:
        # Use matched-window averages only if at least one year supports adjustment.
        # Otherwise retain full-history GAAP values; an incomplete R&D window must
        # not turn available GAAP figures into missing data.
        n_adjusted_total = sum(1 for yd in annual.values() if yd.rnd_basis == "adjusted")
        if n_adjusted_total > 0:
            # Multi-year scores use only fully adjusted years. Single-year roic_latest
            # retains GAAP fallback when its research-asset window is incomplete.
            latest_view = _rnd_adjusted_view(annual)
            matched_view = _rnd_matched_window_view(annual)
            reinvestment_sub = _merge_rnd_reinvestment_views(
                _score_reinvestment(latest_view, coc),
                _score_reinvestment(matched_view, coc),
            )
            reinvestment_sub = _annotate_matched_window_gaap_mean(reinvestment_sub, annual)
        else:
            # Without an adjustment path, retain full-history GAAP scoring. Basis
            # annotation discloses incomplete R&D history but stays silent for no R&D.
            reinvestment_sub = _score_reinvestment(annual, coc)

        # Basis annotation also checks the matched-window invariant: adjusted
        # averages must not contain GAAP-fallback years.
        rnd_state, _rnd_series = classify_rnd_series(res.company)
        reinvestment_sub, rnd_basis_gaps = _annotate_rnd_basis(reinvestment_sub, annual, rnd_state)
        extra_gaps.extend(rnd_basis_gaps)

        # Disclose short adjusted histories using the same min_history_years
        # threshold as delivered growth.
        min_history_years = int(config.get("valuation", {}).get("min_history_years", DEFAULT_MIN_HISTORY_YEARS))
        extra_gaps.extend(_annotate_rnd_short_history(reinvestment_sub, annual, min_history_years))
    else:
        reinvestment_sub = _score_reinvestment(annual, coc)

    cat_scores: dict[str, list[SubScore]] = {
        "reinvestment_engine": reinvestment_sub,
        "quality_persistence": _score_quality(annual, roic_thresh, uni_gm),
        "balance_sheet_resilience": resilience_sub,
        "capital_discipline": _score_capital_discipline(annual, diluted_series_for_scoring),
        "optionality_proxies": _score_optionality(annual, uni_cx, uni_rnd),
    }
    extra_gaps.extend(_history_window_gaps(cat_scores, annual))

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
    # Hold the baseline's history/basis fixed; roic_latest is not perturbed.
    reinv_plus = _merge_rnd_reinvestment_views(
        reinvestment_sub, _score_reinvestment(matched_view, coc, reinv_perturb=+0.20),
    )
    reinv_minus = _merge_rnd_reinvestment_views(
        reinvestment_sub, _score_reinvestment(matched_view, coc, reinv_perturb=-0.20),
    )
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
        composite_ungated=composite,
    )

    # _check_invariants independently recomputes composite from the
    # category weights/composites, so it must run against the TRUE weighted
    # composite -- BEFORE any gate cap is applied below. Gating is a
    # deliberate override layered on top of an already-validated number,
    # not a rewrite of the weighted-average math itself.
    _check_invariants(result)

    # Evaluate gates against raw annual metrics, not transformed sub-scores.
    fired: list[GateOutcome] = []
    untestable: list[GateOutcome] = []
    for gate_cfg in dcfg["gates"]:
        outcome = _evaluate_gate(gate_cfg, annual)
        if outcome is None:
            continue
        if outcome.status == "GATED":
            fired.append(outcome)
        else:
            untestable.append(outcome)
            result.gaps.append(outcome.gap_text)

    if fired:
        # Most restrictive (lowest) cap wins when more than one gate fires.
        winner = min(fired, key=lambda o: o.cap)
        result.gated = True
        result.gate_ids = [o.gate_id for o in fired]
        result.gate_lineage = (
            "; ".join(f"GATED[{o.gate_id}]: {o.reason} -> composite capped {o.cap:.1f}" for o in fired)
            + f" (ungated {result.composite_ungated:.1f})"
        )
        # Cap sits on the composite (and its band) ONLY -- category/sub-
        # score values above are untouched; the gap between strong
        # sub-scores and a capped composite IS the signal.
        #
        # min(), not an unconditional override: a company can simultaneously
        # be over-levered AND already score poorly on its own merits (weak
        # fundamentals and high leverage are, if anything, correlated) --
        # confirmed live during PR verification (AXON at a lowered test
        # threshold: ungated composite 39.4, cap 45.0). A veto exists to cap
        # an artificially high composite that leverage risk would otherwise
        # mislead through; it must never RAISE an already-low score. The
        # chip/lineage still fire on the raw-metric condition regardless --
        # the disclosure is about the leverage, not about whether the cap
        # happened to change the number.
        result.composite = min(result.composite, winner.cap)
        result.composite_low = min(result.composite_low, winner.cap)
        result.composite_high = min(result.composite_high, winner.cap)

    if untestable:
        result.gate_untestable_ids = [o.gate_id for o in untestable]
        result.gate_untestable_lineage = "; ".join(o.gap_text for o in untestable)

    return result
