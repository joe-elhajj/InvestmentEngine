#!/usr/bin/env python3
"""
audit/sensitivity.py — Session C Phase 2: durability assumption
sensitivity harness.

Purpose
-------
Sessions A/B verified the pipeline's arithmetic and lineage. Session C
interrogates the analyst-owned ASSUMPTIONS themselves — the durability
weights, thresholds, and score-band imputes in config.yaml -> durability —
by perturbing them in memory and observing what actually moves.

This script produces sensitivity EVIDENCE. It draws no conclusions and
changes nothing:
  - config.yaml is never written.
  - Every perturbed run is a deep copy of the loaded config, mutated
    in-memory, passed straight to durability.score() and discarded.
  - Every perturbed run is labeled with the UNPERTURBED baseline's real
    config_hash plus an explicit, human-readable perturbation-spec string
    (e.g. "weight:reinvestment_engine+5pp") — never a synthetic hash, and
    the perturbed config's own hash is never presented as if it were an
    adopted assumption set.

Reuse discipline: derive() runs exactly ONCE per ticker (reusing the
on-disk EDGAR cache via engine.edgar.EdgarClient's normal caching), and the
resulting AnalysisResults are held and reused for every perturbation.
durability.score() is pure/local/no-network, so re-running it per
perturbation is cheap and never re-fetches or re-derives.

Uniform perturbation path (Session C Phase 1.5 amendment): weights,
thresholds, AND imputes all go through the public score(res, perturbed_cfg)
entry point uniformly. This was only possible for imputes after Phase 1.5
wired durability.score_band through _resolve_config — before that fix,
score_band was read from two hardcoded module constants and had no
config-driven path at all.

Usage
-----
    python audit/sensitivity.py                  # the Session B.4 fourteen + MSFT (sensitivity-only, not A/B-verified)
    python audit/sensitivity.py --universe        # + S&P 500 census (saturation/imputation only, no perturbation sweep)

Phase 3 (per the session plan): the operator runs this locally and pastes
the output back for interpretation in conversation — this script only
produces tables, it does not interpret them.
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from engine.edgar import EdgarClient  # noqa: E402
from engine.market import get_quote  # noqa: E402
from engine.pipeline import derive, AnalysisResult  # noqa: E402
from engine import durability as D  # noqa: E402
from engine import universe as U  # noqa: E402


# The Session B.4 fourteen — every one has been through Session A/B's
# arithmetic and lineage verification.
TICKERS = [
    "V", "RKLB", "NVDA", "META", "CRM", "CAT", "BE", "AXON",
    "AAPL", "GOOGL", "TSLA", "AMZN", "COST", "AMAT",
]

# MSFT: added for the six-name crossing-highlight set below (Session C
# kickoff named "GOOG/META/MSFT/NVDA/COST/AMAT" — GOOG corrected to GOOGL,
# this codebase's actual ticker everywhere else). MSFT was never part of
# the Session B.4 fourteen and its derive() output has never been through
# Session A/B's arithmetic/lineage verification — it is scored here purely
# for sensitivity purposes. Every table below tags it "(sensitivity-only,
# not A/B-verified)"; it is never silently presented as equivalent to the
# fourteen verified names.
UNVERIFIED_TICKERS = ["MSFT"]
ALL_TICKERS = TICKERS + UNVERIFIED_TICKERS

CROSSING_HIGHLIGHT = ["GOOGL", "META", "MSFT", "NVDA", "COST", "AMAT"]


def _tag(tk: str) -> str:
    """Ticker label for table rows -- marks MSFT's unverified provenance
    inline so it's never mistaken for one of the fourteen verified names."""
    return f"{tk}*" if tk in UNVERIFIED_TICKERS else tk


WEIGHT_KEYS = [
    "reinvestment_engine", "quality_persistence", "balance_sheet_resilience",
    "capital_discipline", "optionality_proxies",
]

THRESHOLD_VARIANTS = {
    "cost_of_capital": [0.07, 0.08, 0.09],
    "roic_threshold": [0.125, 0.15, 0.175],
    "stability_delta_threshold": [4.0, 5.0, 6.0],
}

IMPUTE_VARIANTS = [(20.0, 80.0), (25.0, 75.0), (30.0, 70.0)]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(REPO_ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def fetch_results(tickers: list[str], cfg: dict) -> dict[str, AnalysisResult]:
    """derive() exactly once per ticker, reusing the on-disk EDGAR cache.
    Held and reused for every perturbation below — never re-fetched."""
    client = EdgarClient(
        user_agent=cfg["sec"]["user_agent"],
        request_delay=cfg["sec"].get("request_delay_seconds", 0.2),
        cache_dir=str(REPO_ROOT / ".cache" / "edgar"),
    )
    out: dict[str, AnalysisResult] = {}
    for tk in tickers:
        try:
            # get_company(), not get_company_with_latest_quarter(): verified
            # byte-identical composite/category/sub-score output for NVDA
            # under this config (79.59617859515399 both paths) -- the two
            # only differ in cd.quarterly/res.latest_quarter (report-display
            # only) and one extra res.gaps entry about the latest 10-Q,
            # neither of which durability.score() ever reads.
            cd = client.get_company(tk, cfg["report"]["history_years"])
            quote = get_quote(tk)
            out[tk] = derive(cd, quote, cfg)
        except Exception as e:  # noqa: BLE001 - one bad ticker must not kill the sweep
            print(f"! {tk}: fetch/derive failed: {type(e).__name__}: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Perturbation-spec construction — every entry is (spec_string, perturbed_cfg)
# ---------------------------------------------------------------------------

def _renormalized_weights(base_weights: dict, key: str, delta: float) -> dict:
    """One-at-a-time +-delta on `key`, proportionally renormalizing the
    other four so the set still sums to 1.0 -- mirrors the ratio the
    analyst originally set between the OTHER four categories, rather than
    spreading the adjustment evenly."""
    new = dict(base_weights)
    new[key] = base_weights[key] + delta
    others_sum = sum(v for k, v in base_weights.items() if k != key)
    target_others_sum = 1.0 - new[key]
    scale = (target_others_sum / others_sum) if others_sum else 0.0
    for k in base_weights:
        if k != key:
            new[k] = base_weights[k] * scale
    return new


def build_perturbations(base_cfg: dict) -> list[tuple[str, dict]]:
    dur = base_cfg.get("durability", {})
    base_weights = dict(D._DEFAULT_WEIGHTS)
    base_weights.update(dur.get("weights", {}))

    specs: list[tuple[str, dict]] = []

    # (a) Weights: one-at-a-time +-5pp, proportional renormalization (10 runs)
    for key in WEIGHT_KEYS:
        for sign, label in [(+1, "+5pp"), (-1, "-5pp")]:
            new_weights = _renormalized_weights(base_weights, key, sign * 0.05)
            cfg = copy.deepcopy(base_cfg)
            cfg["durability"]["weights"] = new_weights
            specs.append((f"weight:{key}{label}", cfg))

    # Equal-weights structural stress anchor
    cfg = copy.deepcopy(base_cfg)
    cfg["durability"]["weights"] = {k: 0.20 for k in WEIGHT_KEYS}
    specs.append(("weight:equal-20-20-20-20-20", cfg))

    # (b) Thresholds — vary one key at a time, holding the other two at baseline
    base_thresholds = dict(D._DEFAULT_THRESHOLDS)
    base_thresholds.update(dur.get("thresholds", {}))
    for key, values in THRESHOLD_VARIANTS.items():
        for v in values:
            cfg = copy.deepcopy(base_cfg)
            new_thresholds = dict(base_thresholds)
            new_thresholds[key] = v
            cfg["durability"]["thresholds"] = new_thresholds
            specs.append((f"threshold:{key}={v}", cfg))

    # (c) Imputes — both keys vary together as a pair, per the spec's variant list
    for lo, hi in IMPUTE_VARIANTS:
        cfg = copy.deepcopy(base_cfg)
        cfg["durability"]["score_band"] = {"pessimistic_impute": lo, "optimistic_impute": hi}
        specs.append((f"impute:{lo:.0f}/{hi:.0f}", cfg))

    return specs


# ---------------------------------------------------------------------------
# Kendall tau-b (no scipy dependency — ties handled properly, O(n^2) is fine at n=14)
# ---------------------------------------------------------------------------

def kendall_tau(a: list[float], b: list[float]) -> float:
    n = len(a)
    n0 = n * (n - 1) // 2
    if n0 == 0:
        return float("nan")
    c = d = tied_a = tied_b = 0
    for i in range(n):
        for j in range(i + 1, n):
            da = a[i] - a[j]
            db = b[i] - b[j]
            if da == 0:
                tied_a += 1
            if db == 0:
                tied_b += 1
            if da != 0 and db != 0:
                if (da > 0) == (db > 0):
                    c += 1
                else:
                    d += 1
    denom = ((n0 - tied_a) * (n0 - tied_b)) ** 0.5
    if denom == 0:
        return float("nan")
    return (c - d) / denom


def rank_order(scores: dict[str, "D.DurabilityScore"], tickers: list[str]) -> list[str]:
    """Descending by composite; ticker name as a deterministic tiebreak."""
    return sorted(tickers, key=lambda tk: (-scores[tk].composite, tk))


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def run_sweep(results: dict[str, AnalysisResult], base_cfg: dict) -> dict:
    tickers = list(results)
    baseline_hash = D._config_hash(D._resolve_config(base_cfg))
    baseline_scores = {tk: D.score(res, base_cfg) for tk, res in results.items()}

    perturbations = build_perturbations(base_cfg)
    perturbed_scores_by_spec: dict[str, dict] = {}
    for spec, pcfg in perturbations:
        perturbed_scores_by_spec[spec] = {tk: D.score(res, pcfg) for tk, res in results.items()}

    return {
        "tickers": tickers,
        "baseline_hash": baseline_hash,
        "baseline_scores": baseline_scores,
        "perturbed_scores_by_spec": perturbed_scores_by_spec,
    }


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _spec_group(spec: str) -> str:
    return spec.split(":", 1)[0]


def print_header(sweep: dict) -> None:
    print("=" * 78)
    print("Session C Phase 2 — durability sensitivity sweep")
    print(f"Baseline config_hash: {sweep['baseline_hash']}  (real hash of the UNPERTURBED config)")
    print(f"Tickers: {', '.join(_tag(tk) for tk in sweep['tickers'])}")
    if any(tk in UNVERIFIED_TICKERS for tk in sweep["tickers"]):
        print(
            f"* = {', '.join(UNVERIFIED_TICKERS)} — sensitivity-only, NOT A/B-verified. "
            "Scored here purely to complete the six-name crossing-highlight set; "
            "never equivalent in provenance to the fourteen verified names."
        )
    print("Every perturbed run below is labeled <baseline_hash>::<perturbation-spec> —")
    print("never a synthetic hash, never a config.yaml write.")
    print("=" * 78)


def print_composite_delta_tables(sweep: dict) -> None:
    tickers = sweep["tickers"]
    baseline = sweep["baseline_scores"]
    by_group: dict[str, list[str]] = {}
    for spec in sweep["perturbed_scores_by_spec"]:
        by_group.setdefault(_spec_group(spec), []).append(spec)

    for group in ("weight", "threshold", "impute"):
        specs = by_group.get(group, [])
        if not specs:
            continue
        print(f"\n--- Composite delta ({group}) — {sweep['baseline_hash']}::<spec> ---")
        header = "ticker".ljust(7) + "baseline".rjust(10) + "".join(s.split(":", 1)[1].rjust(22) for s in specs)
        print(header)
        for tk in tickers:
            base_c = baseline[tk].composite
            row = _tag(tk).ljust(7) + f"{base_c:10.4f}"
            for spec in specs:
                pert_c = sweep["perturbed_scores_by_spec"][spec][tk].composite
                delta = pert_c - base_c
                row += f"{delta:+22.4f}"
            print(row)


def print_rank_tables(sweep: dict) -> None:
    tickers = sweep["tickers"]
    baseline = sweep["baseline_scores"]
    base_rank = rank_order(baseline, tickers)
    base_vals = [baseline[tk].composite for tk in tickers]

    print("\n--- Rank order + Kendall tau vs baseline ---")
    print(f"baseline rank: {' > '.join(_tag(tk) for tk in base_rank)}")
    for spec, scores in sweep["perturbed_scores_by_spec"].items():
        pert_rank = rank_order(scores, tickers)
        pert_vals = [scores[tk].composite for tk in tickers]
        tau = kendall_tau(base_vals, pert_vals)
        flag = "" if pert_rank == base_rank else "  <-- RANK ORDER CHANGED"
        print(f"{sweep['baseline_hash']}::{spec:38s} tau={tau:+.4f}{flag}")


def print_crossing_callouts(sweep: dict) -> None:
    tickers = sweep["tickers"]
    highlight = [tk for tk in CROSSING_HIGHLIGHT if tk in tickers]
    missing = [tk for tk in CROSSING_HIGHLIGHT if tk not in tickers]
    print("\n--- Rank crossings among the highlight set ---")
    print(f"Highlight set actually scored: {', '.join(_tag(tk) for tk in highlight)}")
    if missing:
        print(f"NOT in this run's ticker set (skipped): {', '.join(missing)}")

    baseline = sweep["baseline_scores"]
    pairs = [(a, b) for i, a in enumerate(highlight) for b in highlight[i + 1:]]
    base_order = {(a, b): baseline[a].composite > baseline[b].composite for a, b in pairs}

    any_crossing = False
    for spec, scores in sweep["perturbed_scores_by_spec"].items():
        crossed = []
        for a, b in pairs:
            pert_order = scores[a].composite > scores[b].composite
            if pert_order != base_order[(a, b)]:
                crossed.append((a, b))
        if crossed:
            any_crossing = True
            desc = ", ".join(f"{_tag(a)}/{_tag(b)}" for a, b in crossed)
            print(f"{sweep['baseline_hash']}::{spec:38s} crossings: {desc}")
    if not any_crossing:
        print("No rank crossings among the highlight set under any perturbation.")


def print_threshold_cliff_table(sweep: dict, base_cfg: dict, results: dict[str, AnalysisResult]) -> None:
    dur = base_cfg.get("durability", {})
    thresholds = dict(D._DEFAULT_THRESHOLDS)
    thresholds.update(dur.get("thresholds", {}))
    coc = thresholds["cost_of_capital"]
    roic_threshold = thresholds["roic_threshold"]

    print("\n--- Threshold-cliff table (distance from each scoring-curve breakpoint) ---")
    print(f"cost_of_capital={coc}, roic_threshold={roic_threshold}")
    header = (
        "ticker".ljust(7) + "latest_roic".rjust(14) + "dist_to_0".rjust(12)
        + "dist_to_coc".rjust(14) + "dist_to_2coc".rjust(14) + "min_dist_to_roic_thr".rjust(22)
    )
    print(header)
    for tk in sweep["tickers"]:
        ds = sweep["baseline_scores"][tk]
        cat = ds.categories.get("reinvestment_engine")
        latest_roic = None
        if cat:
            for s in cat.sub_scores:
                if s.name == "roic_latest":
                    latest_roic = s.raw
        # Per-year ROIC list, replicated from _score_quality's own selection
        # (read-only mirror -- does not call into durability.py internals).
        res = results[tk]
        roic_by_year = [
            yd.nopat / yd.invested_capital
            for yd in res.annual_series.values()
            if yd.nopat is not None and yd.invested_capital is not None and yd.invested_capital > 0
        ]
        min_dist_thr = min((abs(r - roic_threshold) for r in roic_by_year), default=None)

        if latest_roic is None:
            print(_tag(tk).ljust(7) + "n/a".rjust(14))
            continue
        row = (
            _tag(tk).ljust(7) + f"{latest_roic:14.4f}"
            + f"{latest_roic - 0.0:12.4f}"
            + f"{latest_roic - coc:14.4f}"
            + f"{latest_roic - 2 * coc:14.4f}"
            + (f"{min_dist_thr:22.4f}" if min_dist_thr is not None else "n/a".rjust(22))
        )
        print(row)


def print_saturation_census(sweep: dict) -> None:
    print("\n--- Saturation census (baseline) — % of sub-scores pinned at floor/ceiling ---")
    total = floor = ceiling = 0
    per_ticker = {}
    for tk in sweep["tickers"]:
        ds = sweep["baseline_scores"][tk]
        t = f_ = c_ = 0
        for cat in ds.categories.values():
            for s in cat.sub_scores:
                t += 1
                if s.score == 0.0:
                    f_ += 1
                elif s.score == 100.0:
                    c_ += 1
        per_ticker[tk] = (t, f_, c_)
        total += t
        floor += f_
        ceiling += c_

    print("ticker".ljust(7) + "n_subs".rjust(9) + "floor".rjust(9) + "ceiling".rjust(9) + "pinned%".rjust(11))
    for tk, (t, f_, c_) in per_ticker.items():
        pct = 100.0 * (f_ + c_) / t if t else 0.0
        print(_tag(tk).ljust(7) + f"{t:9d}" + f"{f_:9d}" + f"{c_:9d}" + f"{pct:10.1f}%")
    agg_pct = 100.0 * (floor + ceiling) / total if total else 0.0
    print(f"AGGREGATE across {len(sweep['tickers'])} tickers: {floor + ceiling}/{total} pinned = {agg_pct:.1f}%")


def print_imputation_census(sweep: dict) -> None:
    print("\n--- Imputation census — imputed categories + band width, baseline vs. each impute variant ---")
    header = (
        "ticker".ljust(7) + "imputed_cats".rjust(13) + "band@baseline".rjust(16)
        + "".join(f"band@{lo:.0f}/{hi:.0f}".rjust(14) for lo, hi in IMPUTE_VARIANTS)
    )
    print(header)
    all_cats = {"reinvestment_engine", "quality_persistence", "balance_sheet_resilience",
                "capital_discipline", "optionality_proxies"}
    any_imputed = False
    for tk in sweep["tickers"]:
        ds = sweep["baseline_scores"][tk]
        present = {c for c, cs in ds.categories.items() if cs.sub_scores}
        imputed_count = len(all_cats) - len(present)
        any_imputed = any_imputed or imputed_count > 0
        band_baseline = ds.composite_high - ds.composite_low
        row = _tag(tk).ljust(7) + f"{imputed_count:13d}" + f"{band_baseline:16.4f}"
        for lo, hi in IMPUTE_VARIANTS:
            spec = f"impute:{lo:.0f}/{hi:.0f}"
            pds = sweep["perturbed_scores_by_spec"][spec][tk]
            band = pds.composite_high - pds.composite_low
            row += f"{band:14.4f}"
        print(row)

    if not any_imputed:
        print(
            "\nUNTESTABLE from this ticker set: _compute_composite's impute value is only ever\n"
            "used for a category with ZERO sub-scores, and none of the tickers above has one --\n"
            "imputed_cats is 0 and band width is 0.0000 for every row, under every variant. This\n"
            "is a correct result, not low sensitivity: pessimistic_impute/optimistic_impute carry\n"
            "ZERO empirical sensitivity evidence from this set. They can only be exercised via\n"
            "--universe (sparse-coverage names with a genuinely missing category). Phase 4 cannot\n"
            "AFFIRM either impute key on the strength of this run alone."
        )


# ---------------------------------------------------------------------------
# --universe: census only (saturation + imputation), no perturbation sweep
# ---------------------------------------------------------------------------

def run_universe_census(base_cfg: dict) -> None:
    tickers = U.load_tickers()
    print(f"\n{'=' * 78}")
    print(f"--universe census: {len(tickers)} S&P 500 tickers (saturation + imputation only)")
    print("=" * 78)
    results = fetch_results(tickers, base_cfg)
    print(f"Successfully derived: {len(results)}/{len(tickers)}")

    scores = {tk: D.score(res, base_cfg) for tk, res in results.items()}
    sweep = {"tickers": list(results), "baseline_scores": scores, "perturbed_scores_by_spec": {}}
    print_saturation_census(sweep)

    # Imputation census without the perturbation sweep: baseline band width + imputed-category count only.
    print("\n--- Imputation census (universe, baseline only) ---")
    print("ticker".ljust(7) + "imputed_cats".rjust(13) + "band@baseline".rjust(16))
    all_cats = {"reinvestment_engine", "quality_persistence", "balance_sheet_resilience",
                "capital_discipline", "optionality_proxies"}
    any_imputed = False
    for tk in sorted(results):
        ds = scores[tk]
        present = {c for c, cs in ds.categories.items() if cs.sub_scores}
        imputed_count = len(all_cats) - len(present)
        any_imputed = any_imputed or imputed_count > 0
        band = ds.composite_high - ds.composite_low
        print(tk.ljust(7) + f"{imputed_count:13d}" + f"{band:16.4f}")

    if any_imputed:
        print(
            "\nAt least one universe ticker has a fully-missing category (imputed_cats > 0) --"
            "\nthis is where pessimistic_impute/optimistic_impute sensitivity is actually testable."
            "\nRe-run the full perturbation sweep restricted to these names to get real evidence"
            "\non the impute keys (this --universe pass is census-only, no perturbation applied)."
        )
    else:
        print(
            "\nSTILL UNTESTABLE: no universe ticker has a fully-missing category either --"
            "\npessimistic_impute/optimistic_impute remain without empirical sensitivity evidence."
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--universe", action="store_true",
        help="Also run the saturation/imputation census across the full S&P 500 universe (census only, no perturbation sweep, off by default).",
    )
    args = parser.parse_args()

    base_cfg = load_config()
    results = fetch_results(ALL_TICKERS, base_cfg)
    if not results:
        print("No tickers derived successfully — aborting.", file=sys.stderr)
        sys.exit(1)

    sweep = run_sweep(results, base_cfg)
    print_header(sweep)
    print_composite_delta_tables(sweep)
    print_rank_tables(sweep)
    print_crossing_callouts(sweep)
    print_threshold_cliff_table(sweep, base_cfg, results)
    print_saturation_census(sweep)
    print_imputation_census(sweep)

    if args.universe:
        run_universe_census(base_cfg)


if __name__ == "__main__":
    main()
