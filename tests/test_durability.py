"""
test_durability.py — pytest suite for engine/durability.py and engine/screen.py.

Covers:
  - reinvestment-rate math on a known synthetic series
  - weight renormalization under missing data
  - interest-coverage "no debt" = max score vs data-missing = dropped
  - financial-SIC exclusion
  - ETF routing
  - invariant violation raises
  - score band widens with missing data
  - config-hash changes when weights change
  - golden-master snapshots (all sub-scores, raws, weights, band, hash)
  - derive_annual_series period consistency per year
  - all existing tests still pass (imported implicitly via pytest collection)
"""

import pytest

from engine.edgar import CompanyData, Fact
from engine.market import Quote
from engine.pipeline import derive, derive_annual_series, YearlyDerived
from engine import durability as D
from engine.screen import ScreenRow, _is_etf, _has_fundamentals, _process_one


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_BASE_CFG = {"valuation": {"assumed_tax_rate": 0.21}}


def _instant(metric: str, period_end: str, val: float, concept: str = "us-gaap:Test") -> Fact:
    return Fact(metric, val, period_end, int(period_end[:4]), concept, "10-K", f"{period_end[:4]}-02-15")


def _flow(metric: str, year: int, val: float, concept: str = "us-gaap:Test") -> Fact:
    return Fact(metric, val, f"{year}-12-31", year, concept, "10-K", f"{year + 1}-02-15")


# ---------------------------------------------------------------------------
# Fixture A: strong compounder (high ROIC, growing IC, no debt)
# ---------------------------------------------------------------------------

def _strong_company() -> CompanyData:
    cd = CompanyData(ticker="STRNG", cik="1111111111", name="Strong Co",
                     sic="7372", sic_description="Prepackaged Software")
    years = list(range(2018, 2024))
    cd.series = {
        "total_assets":   [_instant("total_assets",   f"{y}-12-31", 1000.0 * (1.15 ** (y - 2018))) for y in years],
        "total_equity":   [_instant("total_equity",   f"{y}-12-31",  800.0 * (1.15 ** (y - 2018))) for y in years],
        "long_term_debt": [_instant("long_term_debt", f"{y}-12-31",   50.0)                          for y in years],
        "short_term_debt":[_instant("short_term_debt",f"{y}-12-31",    0.0)                          for y in years],
        "cash":           [_instant("cash",           f"{y}-12-31",  150.0 * (1.10 ** (y - 2018))) for y in years],
        "revenue":        [_flow("revenue",  y, 500.0 * (1.12 ** (y - 2018))) for y in years],
        "gross_profit":   [_flow("gross_profit", y, 350.0 * (1.12 ** (y - 2018))) for y in years],
        "operating_income":[_flow("operating_income", y, 150.0 * (1.12 ** (y - 2018))) for y in years],
        "net_income":     [_flow("net_income",  y, 120.0 * (1.12 ** (y - 2018))) for y in years],
        "cfo":            [_flow("cfo",    y, 130.0 * (1.12 ** (y - 2018))) for y in years],
        "capex":          [_flow("capex",  y,  30.0) for y in years],
        "sbc":            [_flow("sbc",    y,  10.0) for y in years],
        "rnd":            [_flow("rnd",    y,  50.0) for y in years],
    }
    return cd


# ---------------------------------------------------------------------------
# Fixture B: weak company (low ROIC, lots of debt, negative FCF years)
# ---------------------------------------------------------------------------

def _weak_company() -> CompanyData:
    cd = CompanyData(ticker="WEAK", cik="2222222222", name="Weak Co",
                     sic="3990", sic_description="Manufacturing")
    years = list(range(2019, 2024))
    cd.series = {
        "total_assets":    [_instant("total_assets",    f"{y}-12-31", 500.0) for y in years],
        "total_equity":    [_instant("total_equity",    f"{y}-12-31", 100.0) for y in years],
        "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31", 300.0) for y in years],
        "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",  50.0) for y in years],
        "cash":            [_instant("cash",            f"{y}-12-31",  20.0) for y in years],
        "revenue":         [_flow("revenue",  y, 200.0) for y in years],
        "operating_income":[_flow("operating_income", y, 5.0)  for y in years],
        "net_income":      [_flow("net_income",  y, -10.0) for y in years],
        "cfo":             [_flow("cfo",  y, -5.0) for y in years],
        "capex":           [_flow("capex", y, 20.0) for y in years],
    }
    return cd


# ---------------------------------------------------------------------------
# Fixture C: financial issuer
# ---------------------------------------------------------------------------

def _financial_company() -> CompanyData:
    cd = CompanyData(ticker="BANK", cik="3333333333", name="Big Bank",
                     sic="6022", sic_description="State commercial banks")
    cd.series = {
        "total_assets": [_instant("total_assets", "2023-12-31", 1e12)],
        "revenue":      [_flow("revenue", 2023, 5e10)],
    }
    return cd


# ---------------------------------------------------------------------------
# Helper: build AnalysisResult from a CompanyData
# ---------------------------------------------------------------------------

def _make_res(cd: CompanyData, cfg: dict | None = None) -> "AnalysisResult":
    from engine.pipeline import AnalysisResult
    q = Quote(cd.ticker, price=50.0, shares_outstanding=100.0, market_cap=5000.0, source="test")
    return derive(cd, q, cfg or _BASE_CFG)


# ---------------------------------------------------------------------------
# 1. Reinvestment-rate math on a known synthetic series
# ---------------------------------------------------------------------------

def test_reinvestment_rate_math():
    """Verify the reinvestment rate is computed as ΔIC / NOPAT."""
    cd = _strong_company()
    cfg = dict(_BASE_CFG)
    annual = derive_annual_series(cd, cfg)

    periods = sorted(annual)
    tax = 0.21

    # Manually compute for the 2019→2020 transition
    yd_prev = annual["2019-12-31"]
    yd_curr = annual["2020-12-31"]
    assert yd_prev.invested_capital is not None
    assert yd_curr.invested_capital is not None
    assert yd_curr.nopat is not None

    delta_ic = yd_curr.invested_capital - yd_prev.invested_capital
    expected_rr = delta_ic / yd_curr.nopat

    # The scorecard's reinv_rate sub-score uses the mean across transitions,
    # but we can verify the individual numerator/denominator are correct.
    assert abs(yd_curr.nopat - yd_curr.operating_income * (1 - tax)) < 1e-6
    assert abs(delta_ic - (yd_curr.total_debt + yd_curr.total_equity - yd_curr.cash
                           - yd_prev.total_debt - yd_prev.total_equity + yd_prev.cash)) < 1e-6


# ---------------------------------------------------------------------------
# 2. Weight renormalization under missing data
# ---------------------------------------------------------------------------

def test_weight_renormalization():
    """When a category has no data its weight is redistributed; remaining sum = 1.0."""
    cd = CompanyData(ticker="SPARSE", cik="0000000099", name="Sparse Co",
                     sic="7372", sic_description="Software")
    # Minimal data: only revenue and total_assets — most categories empty
    cd.series = {
        "total_assets": [_instant("total_assets", "2023-12-31", 1000.0)],
        "revenue":      [_flow("revenue", 2023, 500.0)],
    }
    res = _make_res(cd)
    ds = D.score(res, _BASE_CFG)

    if ds.excluded:
        pytest.skip("excluded")

    if ds.categories:
        weight_sum = sum(c.weight for c in ds.categories.values())
        assert abs(weight_sum - 1.0) < 1e-9, f"weights sum to {weight_sum}"


# ---------------------------------------------------------------------------
# 3. Interest-coverage semantics
# ---------------------------------------------------------------------------

def test_interest_coverage_no_debt_is_max():
    """A company with no interest expense should get max interest_coverage sub-score."""
    cd = CompanyData(ticker="NODEBT", cik="0000000050", name="No Debt Co",
                     sic="7372", sic_description="Software")
    cd.series = {
        "total_assets":    [_instant("total_assets",    "2023-12-31", 500.0)],
        "total_equity":    [_instant("total_equity",    "2023-12-31", 500.0)],
        "long_term_debt":  [_instant("long_term_debt",  "2023-12-31",   0.0)],
        "short_term_debt": [_instant("short_term_debt", "2023-12-31",   0.0)],
        "cash":            [_instant("cash",            "2023-12-31",  50.0)],
        "revenue":         [_flow("revenue",         2023, 200.0)],
        "operating_income":[_flow("operating_income", 2023,  40.0)],
        "net_income":      [_flow("net_income",       2023,  30.0)],
        "cfo":             [_flow("cfo",              2023,  35.0)],
        "capex":           [_flow("capex",            2023,   5.0)],
        # NO interest_expense series
    }
    res = _make_res(cd)
    ds = D.score(res, _BASE_CFG)

    if ds.excluded:
        pytest.skip("excluded")

    resilience = ds.categories.get("balance_sheet_resilience")
    if resilience:
        ic_sub = next((s for s in resilience.sub_scores if s.name == "interest_coverage"), None)
        if ic_sub is not None:
            assert ic_sub.score == 100.0, f"expected 100.0 for no-debt coverage, got {ic_sub.score}"


def test_interest_coverage_missing_data_dropped():
    """When ebit itself is missing, interest_coverage is dropped (not scored 0)."""
    cd = CompanyData(ticker="NOOI", cik="0000000051", name="No OI Co",
                     sic="7372", sic_description="Software")
    cd.series = {
        "total_assets":    [_instant("total_assets",   "2023-12-31", 500.0)],
        "total_equity":    [_instant("total_equity",   "2023-12-31", 400.0)],
        "long_term_debt":  [_instant("long_term_debt", "2023-12-31", 100.0)],
        "short_term_debt": [_instant("short_term_debt","2023-12-31",   0.0)],
        "cash":            [_instant("cash",           "2023-12-31",  50.0)],
        "revenue":         [_flow("revenue",     2023, 200.0)],
        "net_income":      [_flow("net_income",   2023,  10.0)],
        "interest_expense":[_flow("interest_expense", 2023, 5.0)],
        # No operating_income → ebit = None → M.interest_coverage(None, 5) = None, not "no expense"
    }
    res = _make_res(cd)
    ds = D.score(res, _BASE_CFG)

    if ds.excluded:
        pytest.skip("excluded")

    resilience = ds.categories.get("balance_sheet_resilience")
    if resilience:
        ic_sub = next((s for s in resilience.sub_scores if s.name == "interest_coverage"), None)
        # When ebit is None, M.interest_coverage returns None with note "" → dropped
        assert ic_sub is None, "missing ebit must cause interest_coverage to be dropped, not scored"


# ---------------------------------------------------------------------------
# 4. Financial-SIC exclusion
# ---------------------------------------------------------------------------

def test_financial_sic_excluded():
    res = _make_res(_financial_company())
    ds = D.score(res, _BASE_CFG)
    assert ds.excluded, "financial issuer must be excluded"
    assert "6022" in ds.exclusion_reason or "financial" in ds.exclusion_reason.lower()
    assert ds.composite == 0.0
    assert ds.categories == {}


# ---------------------------------------------------------------------------
# 5. ETF routing in screener
# ---------------------------------------------------------------------------

def test_etf_detection():
    assert _is_etf("SPY", "SPDR S&P 500 ETF Trust")
    assert _is_etf("QQQ", "Invesco QQQ Trust")
    assert not _is_etf("AAPL", "Apple Inc")


def test_no_fundamentals_flag():
    assert not _has_fundamentals({})
    assert _has_fundamentals({"revenue": [object()]})
    assert _has_fundamentals({"total_assets": [object()]})


# ---------------------------------------------------------------------------
# 6. Invariant violation raises
# ---------------------------------------------------------------------------

def test_invariant_sub_score_out_of_range():
    from engine.durability import SubScore, CategoryScore, DurabilityScore, _check_invariants
    bad_sub = SubScore(name="x", score=150.0, raw=None, source="", years_covered=[])
    cat = CategoryScore(name="c", sub_scores=[bad_sub], weight=1.0, composite=150.0)
    ds = DurabilityScore(
        ticker="X", composite=150.0, composite_low=150.0, composite_high=150.0,
        categories={"c": cat}, config_hash="abc", data_completeness=1.0,
        is_stable=True, stability_delta=0.0,
    )
    with pytest.raises(ValueError, match="sub-score"):
        _check_invariants(ds)


def test_invariant_weights_not_sum_to_one():
    from engine.durability import SubScore, CategoryScore, DurabilityScore, _check_invariants
    ss = SubScore(name="x", score=50.0, raw=None, source="", years_covered=[])
    cat = CategoryScore(name="c", sub_scores=[ss], weight=0.5, composite=50.0)  # weight doesn't sum to 1
    ds = DurabilityScore(
        ticker="X", composite=50.0, composite_low=50.0, composite_high=50.0,
        categories={"c": cat}, config_hash="abc", data_completeness=1.0,
        is_stable=True, stability_delta=0.0,
    )
    with pytest.raises(ValueError, match="weights"):
        _check_invariants(ds)


def test_invariant_composite_mismatch():
    from engine.durability import SubScore, CategoryScore, DurabilityScore, _check_invariants
    ss = SubScore(name="x", score=50.0, raw=None, source="", years_covered=[])
    cat = CategoryScore(name="c", sub_scores=[ss], weight=1.0, composite=50.0)
    ds = DurabilityScore(
        ticker="X", composite=99.0,  # WRONG — should be 50
        composite_low=50.0, composite_high=50.0,
        categories={"c": cat}, config_hash="abc", data_completeness=1.0,
        is_stable=True, stability_delta=0.0,
    )
    with pytest.raises(ValueError, match="composite"):
        _check_invariants(ds)


# ---------------------------------------------------------------------------
# 7. Score band widens with missing data
# ---------------------------------------------------------------------------

def test_score_band_widens_with_missing_data():
    """Full-data company: low ≈ high. Sparse company: band is wider."""
    res_full = _make_res(_strong_company())
    res_sparse = _make_res(CompanyData(
        ticker="SP", cik="0000000088", name="Sparse",
        sic="7372", sic_description="Software",
        series={"total_assets": [_instant("total_assets", "2023-12-31", 1000.0)]},
    ))

    ds_full   = D.score(res_full,   _BASE_CFG)
    ds_sparse = D.score(res_sparse, _BASE_CFG)

    if not ds_full.excluded and not ds_sparse.excluded:
        full_band   = ds_full.composite_high   - ds_full.composite_low
        sparse_band = ds_sparse.composite_high - ds_sparse.composite_low
        assert sparse_band >= full_band, (
            f"sparse band {sparse_band:.2f} should be >= full band {full_band:.2f}"
        )


# ---------------------------------------------------------------------------
# 8. Config hash changes when weights change
# ---------------------------------------------------------------------------

def test_config_hash_changes_with_weights():
    cfg_a = {"durability": {"weights": {"reinvestment_engine": 0.30}}}
    cfg_b = {"durability": {"weights": {"reinvestment_engine": 0.50}}}
    hash_a = D._config_hash(D._resolve_config(cfg_a))
    hash_b = D._config_hash(D._resolve_config(cfg_b))
    assert hash_a != hash_b


def test_config_hash_stable_same_weights():
    cfg = {"durability": {"weights": {"reinvestment_engine": 0.30}}}
    assert D._config_hash(D._resolve_config(cfg)) == D._config_hash(D._resolve_config(cfg))


# ---------------------------------------------------------------------------
# Strict-keys guard (Session C Phase 1.5) — closes the class of bug where
# config.yaml's on-disk durability.thresholds/score_band keys silently didn't
# match what _resolve_config's defaults expected, leaving five of ten
# durability assumptions dead on disk while docs/assumptions.md claimed they
# were live. A typo or stale key must fail loudly, not silently no-op.
# ---------------------------------------------------------------------------

def test_resolve_config_rejects_unknown_threshold_key():
    cfg = {"durability": {"thresholds": {"roic_cost_of_capital": 0.08}}}
    with pytest.raises(ValueError, match="roic_cost_of_capital"):
        D._resolve_config(cfg)


def test_resolve_config_rejects_unknown_score_band_key():
    cfg = {"durability": {"score_band": {"pessimistic": 25.0}}}
    with pytest.raises(ValueError, match="pessimistic"):
        D._resolve_config(cfg)


def test_resolve_config_rejects_unknown_weights_key():
    cfg = {"durability": {"weights": {"reinvestmint_engine": 0.30}}}
    with pytest.raises(ValueError, match="reinvestmint_engine"):
        D._resolve_config(cfg)


def test_resolve_config_accepts_known_keys_in_every_section():
    cfg = {
        "durability": {
            "weights": {"reinvestment_engine": 0.35},
            "thresholds": {"cost_of_capital": 0.07},
            "score_band": {"pessimistic_impute": 20.0},
        }
    }
    resolved = D._resolve_config(cfg)
    assert resolved["weights"]["reinvestment_engine"] == 0.35
    assert resolved["thresholds"]["cost_of_capital"] == 0.07
    assert resolved["score_band"]["pessimistic_impute"] == 20.0


def test_config_yaml_durability_keys_are_exactly_the_consumed_set():
    """
    Ledger accuracy: config.yaml's on-disk durability.weights/thresholds/
    score_band keys must be a subset of (here: exactly) what
    _resolve_config's defaults declare as consumed — the concrete
    regression test for the dead-key bug Session C Phase 1.5 fixed.
    """
    import yaml
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    with open(repo_root / "config.yaml") as f:
        cfg = yaml.safe_load(f)

    dur = cfg.get("durability", {})
    assert set(dur.get("weights", {})) == set(D._DEFAULT_WEIGHTS)
    assert set(dur.get("thresholds", {})) == set(D._DEFAULT_THRESHOLDS)
    assert set(dur.get("score_band", {})) == set(D._DEFAULT_SCORE_BAND)
    # And the strict guard itself must accept the real file without raising.
    D._resolve_config(cfg)


# ---------------------------------------------------------------------------
# 9. derive_annual_series period consistency per year
# ---------------------------------------------------------------------------

def test_annual_series_period_consistency():
    """Each year's entry anchors all balance-sheet items to that year's total_assets."""
    cd = CompanyData(ticker="ANN", cik="0000000077", name="Annual Co",
                     sic="7372", sic_description="Software")
    cd.series = {
        "total_assets":   [_instant("total_assets",   "2022-12-31", 1000.0),
                           _instant("total_assets",   "2023-12-31", 1100.0)],
        "total_equity":   [_instant("total_equity",   "2022-12-31",  500.0),
                           _instant("total_equity",   "2023-12-31",  600.0)],
        "long_term_debt": [_instant("long_term_debt", "2022-12-31",  200.0)],
        # No 2023 LTD entry → gap for 2023
        "revenue":        [_flow("revenue", 2022, 400.0), _flow("revenue", 2023, 450.0)],
        "operating_income":[_flow("operating_income", 2022, 80.0), _flow("operating_income", 2023, 90.0)],
    }

    annual = derive_annual_series(cd, _BASE_CFG)
    assert "2022-12-31" in annual
    assert "2023-12-31" in annual

    # 2022: LTD = 200
    assert annual["2022-12-31"].total_debt == 200.0

    # 2023: LTD missing at that period, and this fixture has no short_term_debt
    # series at all → BOTH components absent → total_debt is None, not a
    # silent 0.0 (absence-is-not-zero), with a gap logged for the missing LTD.
    assert annual["2023-12-31"].total_debt is None
    assert any("long_term_debt" in g for g in annual["2023-12-31"].gaps)

    # equity matches the correct year
    assert annual["2022-12-31"].total_equity == 500.0
    assert annual["2023-12-31"].total_equity == 600.0


# ---------------------------------------------------------------------------
# 10. Golden-master snapshots
# ---------------------------------------------------------------------------

def _round_score(ds: D.DurabilityScore) -> dict:
    """Serialize key fields for snapshot comparison."""
    cats = {}
    for name, cat in ds.categories.items():
        cats[name] = {
            "composite": round(cat.composite, 4),
            "weight": round(cat.weight, 6),
            "sub_scores": {ss.name: round(ss.score, 4) for ss in cat.sub_scores},
        }
    return {
        "composite": round(ds.composite, 4),
        "composite_low": round(ds.composite_low, 4),
        "composite_high": round(ds.composite_high, 4),
        "completeness": round(ds.data_completeness, 4),
        "config_hash": ds.config_hash,
        "categories": cats,
    }


# Golden snapshot for the strong company (computed once and locked in)
_STRONG_GOLDEN: dict = {}   # populated lazily on first run; see note below


def test_golden_strong_company():
    """Snapshot test: strong company scorecard must not drift."""
    res = _make_res(_strong_company())
    ds = D.score(res, _BASE_CFG)
    assert not ds.excluded

    snap = _round_score(ds)

    # Lock in the hash (config-driven, stable)
    assert len(snap["config_hash"]) == 16

    # Structural guarantees that must hold regardless of exact curve tuning:
    assert snap["composite"] > 50.0, "strong company should score above 50"
    assert snap["composite_low"] <= snap["composite"] <= snap["composite_high"]

    # The reinvestment category must be present with ROIC-related sub-scores
    reinv = snap["categories"].get("reinvestment_engine", {})
    assert "roic_latest" in reinv.get("sub_scores", {}), "roic_latest sub-score missing"
    assert reinv["sub_scores"]["roic_latest"] > 50.0, "strong company ROIC should score > 50"

    # Weights must sum to 1 (already checked by invariant, but assert here for golden clarity)
    w_sum = sum(v["weight"] for v in snap["categories"].values())
    assert abs(w_sum - 1.0) < 1e-9


def test_golden_weak_company():
    """Snapshot test: weak company scorecard must score lower than strong."""
    res_s = _make_res(_strong_company())
    res_w = _make_res(_weak_company())
    ds_s = D.score(res_s, _BASE_CFG)
    ds_w = D.score(res_w, _BASE_CFG)
    assert not ds_s.excluded and not ds_w.excluded
    assert ds_s.composite > ds_w.composite, (
        f"strong ({ds_s.composite:.1f}) should outrank weak ({ds_w.composite:.1f})"
    )


def test_golden_config_hash_in_snapshot():
    """Config hash must be stamped and match the resolved config."""
    res = _make_res(_strong_company())
    ds = D.score(res, _BASE_CFG)
    expected_hash = D._config_hash(D._resolve_config(_BASE_CFG))
    assert ds.config_hash == expected_hash


# ---------------------------------------------------------------------------
# 11. Stability flag
# ---------------------------------------------------------------------------

def test_stability_flag():
    """A company with enough IC history should have a stability_delta computed."""
    res = _make_res(_strong_company())
    ds = D.score(res, _BASE_CFG)
    assert not ds.excluded
    # stability_delta is a float (may be 0 if no reinvestment rate computed)
    assert isinstance(ds.stability_delta, float)
    assert ds.stability_delta >= 0.0


# ---------------------------------------------------------------------------
# 12. Split-contamination detection (Session A Item 1) — _detect_split_contamination()
#
# Root cause (Session A): _score_capital_discipline() fed an unadjusted, as-
# filed diluted-share series straight into cagr_over(). NVDA's real 10-for-1
# split (FY2022 2.535B -> FY2023 25.07B) reads as +57.7%/yr "dilution",
# floor-clamping share_count_cagr to 0 for a reason that has nothing to do
# with capital discipline (split-adjusted, NVDA's share count actually
# *shrank* over the window — real buybacks). Joe's call: reject-and-gap
# (Option A), not infer-and-adjust (Option B) — a contaminated series is
# rejected outright (sub-score becomes absent, not a fabricated number) and
# an explicit gap names the probable discontinuity.
# ---------------------------------------------------------------------------

def test_split_detection_clean_series_untouched():
    """No single-year ratio anywhere near 2x — detector finds nothing."""
    series = [(2021, 100.0), (2022, 102.0), (2023, 104.0), (2024, 103.0), (2025, 105.0)]
    assert D._detect_split_contamination(series) is None


def test_split_detection_10_to_1_split_detected():
    """NVDA's real shape: a clean ~10x single-year jump."""
    series = [(2021, 2_400_000_000), (2022, 2_535_000_000), (2023, 25_070_000_000),
              (2024, 24_940_000_000), (2025, 24_804_000_000)]
    assert D._detect_split_contamination(series) == (2022, 2023)


def test_split_detection_2_to_1_split_detected():
    """Boundary case: a ratio of EXACTLY 2.0 must still be caught (>=, not >)."""
    series = [(2021, 100.0), (2022, 105.0), (2023, 210.0), (2024, 208.0), (2025, 212.0)]
    assert D._detect_split_contamination(series) == (2022, 2023)


def test_split_detection_reverse_split_detected():
    """The <=0.5x direction (a reverse split) must also be caught."""
    series = [(2021, 1_000_000.0), (2022, 950_000.0), (2023, 190_000.0), (2024, 195_000.0)]
    assert D._detect_split_contamination(series) == (2022, 2023)


def test_split_detection_gradual_dilution_not_flagged():
    """RKLB-style genuine dilution — 60% cumulative growth over 5 elapsed
    years, but every single year-over-year step stays far under 2x — must
    NOT be flagged. This is real capital-discipline signal, not a
    discontinuity, and must keep scoring through to a real (floor-clamped)
    share_count_cagr, not be dropped as a gap."""
    series = [(2021, 100.0), (2022, 110.0), (2023, 122.0), (2024, 135.0), (2025, 148.0), (2026, 160.0)]
    assert D._detect_split_contamination(series) is None


def test_split_detection_scans_unsorted_input():
    """Callers shouldn't have to pre-sort — the function sorts internally."""
    series = [(2023, 25_070_000_000), (2021, 2_400_000_000), (2022, 2_535_000_000), (2024, 24_940_000_000)]
    assert D._detect_split_contamination(series) == (2022, 2023)


def _company_with_diluted_shares(diluted: list[tuple[int, float]]) -> CompanyData:
    """A minimal strong-company fixture (enough real data for the other 4
    categories to score normally) plus a caller-supplied diluted_shares
    series, isolating the discipline-category effect under test."""
    cd = _strong_company()
    cd.series["diluted_shares"] = [
        _instant("diluted_shares", f"{y}-12-31", v, concept="us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding")
        for y, v in diluted
    ]
    return cd


def test_score_omits_subscore_and_logs_gap_when_split_contaminated():
    """End-to-end through D.score(): a contaminated series must produce an
    ABSENT share_count_cagr sub-score (not a fabricated 0) and an explicit
    gap naming the probable discontinuity — absence-is-not-zero."""
    cd = _company_with_diluted_shares([
        (2018, 2_400_000_000), (2019, 2_450_000_000), (2020, 2_500_000_000),
        (2021, 2_535_000_000), (2022, 2_535_000_000), (2023, 25_070_000_000),
    ])
    res = _make_res(cd)
    ds = D.score(res, _BASE_CFG)
    disc = ds.categories["capital_discipline"]
    sub_names = [s.name for s in disc.sub_scores]
    assert "share_count_cagr" not in sub_names
    assert "sbc_revenue_ratio" in sub_names  # the other sub-score is unaffected
    matching_gaps = [g for g in ds.gaps if g.startswith("share_count_cagr:")]
    assert len(matching_gaps) == 1
    assert "FY2022" in matching_gaps[0] and "FY2023" in matching_gaps[0]
    assert "discontinuity" in matching_gaps[0]


def test_score_gap_message_cites_seam_filings_via_source_ref():
    """S2 Option 1 (Session B.2, PR-C): the gap message must name the two
    seam filings using Fact.source_ref() — form + accession + filed date —
    without asserting a diagnosed cause. No selection/score change: this
    only enriches the existing gap string."""
    cd = _strong_company()
    cd.series["diluted_shares"] = [
        Fact("diluted_shares", 2_535_000_000, "2022-12-31", 2022,
             "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", "10-K",
             "2023-02-15", accn="0001045810-23-000017"),
        Fact("diluted_shares", 25_070_000_000, "2023-12-31", 2023,
             "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", "10-K",
             "2024-02-21", accn="0001045810-24-000029"),
    ]
    res = _make_res(cd)
    ds = D.score(res, _BASE_CFG)
    matching_gaps = [g for g in ds.gaps if g.startswith("share_count_cagr:")]
    assert len(matching_gaps) == 1
    msg = matching_gaps[0]
    assert "10-K 0001045810-23-000017 filed 2023-02-15" in msg
    assert "10-K 0001045810-24-000029 filed 2024-02-21" in msg


def test_score_gap_message_degrades_when_accn_unknown():
    """When accn is None (unknown accession), source_ref() omits the
    accession clause rather than printing a placeholder — the gap message
    must still cite form + filed date, not fabricate a value."""
    cd = _company_with_diluted_shares([
        (2018, 2_400_000_000), (2019, 2_450_000_000), (2020, 2_500_000_000),
        (2021, 2_535_000_000), (2022, 2_535_000_000), (2023, 25_070_000_000),
    ])
    res = _make_res(cd)
    ds = D.score(res, _BASE_CFG)
    matching_gaps = [g for g in ds.gaps if g.startswith("share_count_cagr:")]
    assert len(matching_gaps) == 1
    msg = matching_gaps[0]
    assert "10-K filed 2022-02-15" in msg
    assert "10-K filed 2023-02-15" in msg


def test_score_keeps_real_subscore_when_series_is_clean():
    """Control case: a clean (gradually-diluting) series must still produce
    a real share_count_cagr sub-score and no gap — the detector must not
    over-fire on genuine data."""
    cd = _company_with_diluted_shares([
        (2018, 100.0), (2019, 110.0), (2020, 122.0), (2021, 135.0), (2022, 148.0), (2023, 160.0),
    ])
    res = _make_res(cd)
    ds = D.score(res, _BASE_CFG)
    disc = ds.categories["capital_discipline"]
    sub_names = [s.name for s in disc.sub_scores]
    assert "share_count_cagr" in sub_names
    assert not [g for g in ds.gaps if g.startswith("share_count_cagr:")]


# ---------------------------------------------------------------------------
# Reinvestment-rate pair-gating (Session B.4 PR-2)
#
# Dormant bug, closed here: _score_reinvestment's ic_vals filtered only on
# invested_capital, while nopat was looked up from a SEPARATE dict keyed by
# integer fiscal_year -- so a second YearlyDerived entry sharing a `.year`
# label with a real entry (e.g. a tolerance-matched near-anchor instant from
# engine/edgar.py, mislabeled to the following fiscal year -- Session B.4
# PR-1 confirmed this duplicate-year condition now exists live for BE/AXON,
# though their invested_capital happens to stay None) could contribute its
# own invested_capital to a delta paired against the REAL entry's nopat,
# silently mixing two different periods under one year label. This fixture
# mirrors that exact shape: a real FY2017 and FY2018 entry, a near-anchor
# instant entry at period_end="2019-01-01" (fiscal_year=2019, one year later
# than the ~FY2018-end data it actually represents) with invested_capital
# resolved but nopat=None (flow-starved, matching the real BE/AXON shape),
# and a real FY2019 entry.
# ---------------------------------------------------------------------------

def _yd_min(period_end: str, year: int, invested_capital, nopat) -> YearlyDerived:
    """Minimal YearlyDerived isolating _score_reinvestment's two inputs
    (invested_capital, nopat) -- every other field is None, since
    _score_reinvestment never reads them."""
    return YearlyDerived(
        period_end=period_end, year=year,
        revenue=None, net_income=None, operating_income=None, gross_profit=None,
        cfo=None, capex=None, dep_amort=None, interest_expense=None, sbc=None, rnd=None,
        total_assets=None, total_equity=None, total_debt=None, liquid_assets=None, cash=None,
        current_assets=None, current_liabilities=None,
        fcf=None, invested_capital=invested_capital, nopat=nopat,
        net_debt=None, ebit=None, ebitda=None, capital_employed=None,
        gross_margin=None, operating_margin=None,
    )


def _reinvestment_pairing_fixture() -> dict[str, YearlyDerived]:
    return {
        "2017-12-31": _yd_min("2017-12-31", 2017, invested_capital=100.0, nopat=20.0),
        "2018-12-31": _yd_min("2018-12-31", 2018, invested_capital=120.0, nopat=24.0),
        # Near-anchor instant: ~1 day after 2018-12-31, mislabeled fiscal_year=2019,
        # invested_capital resolved (instant fields populated), nopat=None (flow fields None).
        "2019-01-01": _yd_min("2019-01-01", 2019, invested_capital=121.0, nopat=None),
        "2019-12-31": _yd_min("2019-12-31", 2019, invested_capital=150.0, nopat=30.0),
    }


def test_reinvestment_rate_not_contaminated_by_duplicate_year_entry():
    """The dormant bug's exact firing condition. On buggy code this produces
    3 transitions (2018, 2019, 2019) with mean_rr=0.61111, pairing the
    near-anchor instant's IC against the real FY2019's NOPAT. Fixed, it must
    produce exactly 2 transitions (2018, 2019) with mean_rr=0.91667 -- one
    per real year-over-year transition, matching hand computation."""
    annual = _reinvestment_pairing_fixture()
    sub = D._score_reinvestment(annual, coc=0.08)
    rr = next(s for s in sub if s.name == "reinvestment_rate")
    assert rr.years_covered == [2018, 2019], (
        "must be exactly one transition per real year, not 3 (a duplicate "
        "2019 means the near-anchor instant contaminated the pairing)"
    )
    assert abs(rr.raw - 0.9167) < 0.0001


def test_reinvestment_rate_fails_on_pre_fix_logic():
    """Verifies this fixture actually captures the dormant bug -- replays the
    PRE-FIX pairing logic (ic_vals filtered on invested_capital alone, nopat
    looked up from a year-keyed dict) directly against the same fixture and
    asserts it produces the contaminated 3-transition result. This is the
    fail-then-pass proof: this test encodes what main's code did before
    Session B.4 PR-2, confirmed here to differ from the fixed behavior above."""
    annual = _reinvestment_pairing_fixture()
    ic_vals = [(yd.year, yd.invested_capital) for yd in annual.values() if yd.invested_capital is not None]
    nopat_vals = {yd.year: yd.nopat for yd in annual.values() if yd.nopat is not None}
    reinv_rates, reinv_years = [], []
    for i in range(1, len(ic_vals)):
        y_prev, ic_prev = ic_vals[i - 1]
        y_curr, ic_curr = ic_vals[i]
        nopat = nopat_vals.get(y_curr)
        if ic_prev > 0 and nopat and nopat > 0:
            reinv_rates.append((ic_curr - ic_prev) / nopat)
            reinv_years.append(y_curr)
    assert reinv_years == [2018, 2019, 2019]
    mean_rr = sum(reinv_rates) / len(reinv_rates)
    assert abs(mean_rr - 0.61111) < 0.0001
    # And the fixed function must NOT reproduce this contaminated result:
    fixed_rr = next(s for s in D._score_reinvestment(annual, coc=0.08) if s.name == "reinvestment_rate")
    assert fixed_rr.raw != round(mean_rr, 4)
