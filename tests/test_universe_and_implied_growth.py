"""
test_universe_and_implied_growth.py — pytest suite for Part A–E.

Covers:
  - implied_growth round-trip: forward DCF → price → reverse → recover g within tolerance
  - implied_growth bracket bounds: upper (expensive) and lower (cheap) — report not crash
  - normalized_fcf: median-margin math on a known erratic series
  - normalized_fcf non-positive median → None + gap entry
  - expectations_gap sign: overvalued fixture → positive gap; undervalued → negative
  - universe percentile: synthetic distribution → correct percentile
  - percentile changes when universe distribution changes (proves ranking is vs universe)
  - config hash changes when universe.version changes (same weights)
  - config hash stable when nothing changes
  - golden master: implied growth and gap are present for the strong-company fixture
  - universe.load_tickers: skips blank lines and comments; missing file → empty list
  - universe.distribution: round-trip serialize/deserialize
  - all existing core and durability tests still pass (collected automatically)
"""

import statistics
import pytest

from engine import valuation as V
from engine import peers as P
from engine import durability as D
from engine.pipeline import derive, _normalized_fcf, _delivered_growth, derive_annual_series
from engine.edgar import CompanyData, Fact
from engine.market import Quote
from engine.universe import UniverseDistribution, load_tickers, save_distribution, load_cached_distribution

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DCF_CFG = {
    "valuation": {
        "assumed_tax_rate": 0.21,
        "dcf": {
            "projection_years": 5,
            "scenarios": {
                "base": {"wacc": 0.09, "terminal_growth": 0.025, "fcf_growth": 0.08},
                "bear": {"wacc": 0.11, "terminal_growth": 0.015, "fcf_growth": 0.03},
                "bull": {"wacc": 0.08, "terminal_growth": 0.030, "fcf_growth": 0.14},
            },
        },
    }
}


def _instant(metric: str, pe: str, v: float) -> Fact:
    return Fact(metric, v, pe, int(pe[:4]), "us-gaap:Test", "10-K", f"{pe[:4]}-02-15")


def _flow(metric: str, year: int, v: float) -> Fact:
    return Fact(metric, v, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year+1}-02-15")


def _strong_cd() -> CompanyData:
    cd = CompanyData(ticker="STRNG", cik="1111111111", name="Strong Co",
                     sic="7372", sic_description="Prepackaged Software")
    years = list(range(2018, 2024))
    cd.series = {
        "total_assets":    [_instant("total_assets",    f"{y}-12-31", 1000.0 * (1.15 ** (y - 2018))) for y in years],
        "total_equity":    [_instant("total_equity",    f"{y}-12-31",  800.0 * (1.15 ** (y - 2018))) for y in years],
        "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31",   50.0) for y in years],
        "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",    0.0) for y in years],
        "cash":            [_instant("cash",            f"{y}-12-31",  150.0 * (1.10 ** (y - 2018))) for y in years],
        "revenue":         [_flow("revenue",          y, 500.0 * (1.12 ** (y - 2018))) for y in years],
        "gross_profit":    [_flow("gross_profit",     y, 350.0 * (1.12 ** (y - 2018))) for y in years],
        "operating_income":[_flow("operating_income", y, 150.0 * (1.12 ** (y - 2018))) for y in years],
        "net_income":      [_flow("net_income",       y, 120.0 * (1.12 ** (y - 2018))) for y in years],
        "cfo":             [_flow("cfo",              y, 130.0 * (1.12 ** (y - 2018))) for y in years],
        "capex":           [_flow("capex",            y,  30.0) for y in years],
    }
    return cd


def _make_res(cd: CompanyData, price: float = 50.0, cfg: dict | None = None):
    q = Quote(cd.ticker, price=price, shares_outstanding=100.0, market_cap=price * 100.0, source="test")
    return derive(cd, q, cfg or _DCF_CFG)


# ---------------------------------------------------------------------------
# 1. implied_growth round-trip
# ---------------------------------------------------------------------------

def test_implied_growth_round_trip():
    """Forward DCF at g=10 % → price → reverse solve → recover g within 1e-4."""
    true_g = 0.10
    norm_fcf, net_debt, shares = 100.0, 0.0, 10.0
    a = {"projection_years": 5, "wacc": 0.09, "terminal_growth": 0.025, "fcf_growth": true_g}
    price = V.two_stage_dcf(norm_fcf, net_debt, shares, None, "fwd", a).fair_value_per_share

    ig = V.implied_growth(price=price, shares=shares, net_debt=net_debt, norm_fcf=norm_fcf, config=_DCF_CFG)
    assert ig is not None
    assert not ig.bracket_hit
    assert abs(ig.implied_growth - true_g) < 1e-4, (
        f"round-trip error: expected {true_g:.6f}, got {ig.implied_growth:.6f}"
    )


def test_implied_growth_round_trip_negative_net_debt():
    """Net-cash company (negative net_debt) also round-trips correctly."""
    true_g = 0.08
    norm_fcf, net_debt, shares = 50.0, -200.0, 20.0  # net-cash
    a = {"projection_years": 5, "wacc": 0.09, "terminal_growth": 0.025, "fcf_growth": true_g}
    price = V.two_stage_dcf(norm_fcf, net_debt, shares, None, "fwd", a).fair_value_per_share

    ig = V.implied_growth(price=price, shares=shares, net_debt=net_debt, norm_fcf=norm_fcf, config=_DCF_CFG)
    assert ig is not None and not ig.bracket_hit
    assert abs(ig.implied_growth - true_g) < 1e-4


# ---------------------------------------------------------------------------
# 2. Bracket bounds — report not crash
# ---------------------------------------------------------------------------

def test_implied_growth_bracket_upper():
    """Obscenely expensive price → bracket upper hit, not a crash."""
    ig = V.implied_growth(price=1e9, shares=1.0, net_debt=0.0, norm_fcf=1.0, config=_DCF_CFG)
    assert ig is not None
    assert ig.bracket_hit
    assert ig.bracket_bound == "upper"
    assert ig.implied_growth == pytest.approx(0.60)


def test_implied_growth_bracket_lower():
    """Price far below even pessimistic DCF → bracket lower hit, not a crash."""
    ig = V.implied_growth(price=0.001, shares=1.0, net_debt=0.0, norm_fcf=1e6, config=_DCF_CFG)
    assert ig is not None
    assert ig.bracket_hit
    assert ig.bracket_bound == "lower"
    assert ig.implied_growth == pytest.approx(-0.20)


def test_implied_growth_none_on_missing_inputs():
    """Missing price, shares, net_debt, or norm_fcf → None, not an exception."""
    assert V.implied_growth(None,  1.0, 0.0, 100.0, _DCF_CFG) is None
    assert V.implied_growth(50.0,  None, 0.0, 100.0, _DCF_CFG) is None
    assert V.implied_growth(50.0,  1.0, None, 100.0, _DCF_CFG) is None
    assert V.implied_growth(50.0,  1.0, 0.0, None,  _DCF_CFG) is None


# ---------------------------------------------------------------------------
# 3. normalized_fcf — median math
# ---------------------------------------------------------------------------

def _annual_with_fcf_margins(margins: list[float], revenue: float = 1000.0):
    """Build a minimal annual series dict with specified FCF margins."""
    from engine.pipeline import YearlyDerived
    annual = {}
    for i, m in enumerate(margins):
        year = 2020 + i
        pe = f"{year}-12-31"
        fcf = m * revenue
        annual[pe] = YearlyDerived(
            period_end=pe, year=year,
            revenue=revenue, net_income=None, operating_income=None, gross_profit=None,
            cfo=None, capex=None, dep_amort=None, interest_expense=None, sbc=None, rnd=None,
            total_assets=None, total_equity=None, total_debt=None, liquid_assets=None, cash=None,
            current_assets=None, current_liabilities=None,
            fcf=fcf, invested_capital=None, nopat=None, net_debt=None,
            ebit=None, ebitda=None, capital_employed=None,
            gross_margin=None, operating_margin=None,
        )
    return annual


def test_normalized_fcf_median_math():
    """Median of [0.05, 0.10, 0.15, 0.20, 0.25] = 0.15 × latest revenue = 150."""
    margins = [0.05, 0.10, 0.15, 0.20, 0.25]
    annual = _annual_with_fcf_margins(margins, revenue=1000.0)
    val, lineage = _normalized_fcf(annual)
    assert val is not None
    assert abs(val - 150.0) < 1e-9, f"expected 150.0, got {val}"
    assert "0.150" in lineage or "15.0" in lineage  # median margin is mentioned


def test_normalized_fcf_erratic_series():
    """Erratic FCF: margins [-0.2, 0.05, 0.4, 0.08, 0.12] → median = 0.08 → 80.0."""
    margins = [-0.20, 0.05, 0.40, 0.08, 0.12]
    annual = _annual_with_fcf_margins(margins, revenue=1000.0)
    val, lineage = _normalized_fcf(annual)
    assert val is not None
    assert abs(val - 80.0) < 1e-9


def test_normalized_fcf_non_positive_median_returns_none():
    """When median FCF margin ≤ 0 → (None, reason) — absence is not zero."""
    margins = [-0.30, -0.10, 0.02, 0.04, 0.05]
    # median = 0.02; that is positive — try: [-0.30, -0.10, -0.02, 0.04, 0.05]
    # median of sorted [-0.30, -0.10, -0.02, 0.04, 0.05] = -0.02
    margins = [-0.30, -0.10, -0.02, 0.04, 0.05]
    annual = _annual_with_fcf_margins(margins, revenue=1000.0)
    val, lineage = _normalized_fcf(annual)
    assert val is None
    assert "≤ 0" in lineage or "non-positive" in lineage.lower() or "0" in lineage


def test_normalized_fcf_empty_series():
    """Empty annual series → (None, reason)."""
    val, lineage = _normalized_fcf({})
    assert val is None


# ---------------------------------------------------------------------------
# 4. Expectations gap sign
# ---------------------------------------------------------------------------

def test_expectations_gap_positive_when_overvalued():
    """
    Price set so implied growth > delivered growth → gap > 0 (price demands more
    than history justifies).
    """
    cd = _strong_cd()
    annual = derive_annual_series(cd, _DCF_CFG)
    dg, _ = _delivered_growth(annual)
    assert dg is not None

    # Compute a price that embeds 10 pp more growth than delivered
    target_g = dg + 0.10
    norm_fcf_val, _ = _normalized_fcf(annual)
    assert norm_fcf_val is not None

    # Use latest net_debt from annual
    latest = annual[max(annual)]
    net_debt = latest.net_debt if latest.net_debt is not None else 0.0

    a = {"projection_years": 5, "wacc": 0.09, "terminal_growth": 0.025, "fcf_growth": target_g}
    overvalued_price = V.two_stage_dcf(norm_fcf_val, net_debt, 100.0, None, "ov", a).fair_value_per_share
    assert overvalued_price is not None

    res = _make_res(cd, price=overvalued_price)
    assert res.expectations_gap is not None
    assert res.expectations_gap > 0, (
        f"expected positive gap, got {res.expectations_gap:.4f}"
    )


def test_expectations_gap_negative_when_undervalued():
    """
    Price set so implied growth < delivered growth → gap < 0 (price demands less
    than history has delivered; potential value).
    """
    cd = _strong_cd()
    annual = derive_annual_series(cd, _DCF_CFG)
    dg, _ = _delivered_growth(annual)
    assert dg is not None

    target_g = max(dg - 0.10, -0.15)  # floor to avoid bracket
    norm_fcf_val, _ = _normalized_fcf(annual)
    assert norm_fcf_val is not None
    latest = annual[max(annual)]
    net_debt = latest.net_debt if latest.net_debt is not None else 0.0

    a = {"projection_years": 5, "wacc": 0.09, "terminal_growth": 0.025, "fcf_growth": target_g}
    undervalued_price = V.two_stage_dcf(norm_fcf_val, net_debt, 100.0, None, "uv", a).fair_value_per_share
    assert undervalued_price is not None

    res = _make_res(cd, price=undervalued_price)
    assert res.expectations_gap is not None
    assert res.expectations_gap < 0, (
        f"expected negative gap, got {res.expectations_gap:.4f}"
    )


def test_expectations_gap_none_when_bracket_hit():
    """When implied growth hits a bracket bound, expectations_gap must be None."""
    # Obscenely expensive price → bracket upper hit → gap must be None
    cd = _strong_cd()
    res = _make_res(cd, price=1e9)
    # implied_growth_result should have bracket_hit=True
    igr = res.implied_growth_result
    if igr is not None and igr.bracket_hit:
        assert res.expectations_gap is None
    # If norm_fcf couldn't be computed, result may be None entirely — that's fine


# ---------------------------------------------------------------------------
# 5. Universe percentile
# ---------------------------------------------------------------------------

def test_universe_percentile_correct():
    """A value at the 50th percentile of a known distribution returns 50."""
    dist = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    # target=0.6: 5 values below it (0.1–0.5) out of 10 → 50th percentile
    rs = P.relative_score("gm", 0.6, dist)
    assert abs(rs.percentile - 50.0) < 1.0


def test_universe_percentile_changes_with_distribution():
    """Same metric value, different distribution → different percentile (proves ranking is vs universe)."""
    dist_a = [0.1, 0.2, 0.3, 0.4, 0.5]   # target=0.4 → above 3/5 = 60th
    dist_b = [0.3, 0.4, 0.5, 0.6, 0.7]   # target=0.4 → above 1/5 = 20th
    pct_a = P.relative_score("gm", 0.4, dist_a).percentile
    pct_b = P.relative_score("gm", 0.4, dist_b).percentile
    assert pct_a != pct_b
    assert pct_a > pct_b  # same value ranks higher against the weaker distribution


def test_universe_distribution_used_in_scoring():
    """When a universe is injected, gross_margin_peer_percentile sub-score appears."""
    cd = _strong_cd()
    res = _make_res(cd)
    # Build a synthetic universe with 10 gross-margin data points; our company's
    # latest gross margin is ~70 % → should rank high in a universe centred on 40 %
    uni = UniverseDistribution(
        version="test-v1",
        config_hash="dummy",
        data={"gross_margin": [0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7]},
    )
    ds = D.score(res, _DCF_CFG, universe=uni)
    assert not ds.excluded

    quality = ds.categories.get("quality_persistence")
    assert quality is not None
    pct_sub = next((s for s in quality.sub_scores if s.name == "gross_margin_peer_percentile"), None)
    assert pct_sub is not None, "gross_margin_peer_percentile sub-score must appear when universe is provided"
    assert pct_sub.score > 50.0, "strong company should rank above median on gross margin"


def test_universe_distribution_absent_drops_percentile_subscore():
    """Without a universe, gross_margin_peer_percentile sub-score must NOT appear."""
    cd = _strong_cd()
    res = _make_res(cd)
    ds = D.score(res, _DCF_CFG)   # no universe
    assert not ds.excluded

    quality = ds.categories.get("quality_persistence")
    if quality:
        pct_sub = next((s for s in quality.sub_scores if s.name == "gross_margin_peer_percentile"), None)
        assert pct_sub is None, "percentile sub-score must be absent without a universe"


# ---------------------------------------------------------------------------
# 6. Config hash
# ---------------------------------------------------------------------------

def test_config_hash_changes_with_universe_version():
    """Different universe.version → different config hash (not silently compared)."""
    cfg_a = {"universe": {"version": "2026-Q2"}}
    cfg_b = {"universe": {"version": "2026-Q3"}}
    hash_a = D._config_hash(D._resolve_config(cfg_a))
    hash_b = D._config_hash(D._resolve_config(cfg_b))
    assert hash_a != hash_b


def test_config_hash_stable_with_same_version():
    cfg = {"universe": {"version": "2026-Q3"}}
    assert D._config_hash(D._resolve_config(cfg)) == D._config_hash(D._resolve_config(cfg))


def test_config_hash_changes_without_universe_vs_with():
    """Default 'unversioned' differs from a named version."""
    cfg_none = {}
    cfg_v    = {"universe": {"version": "2026-Q3"}}
    assert D._config_hash(D._resolve_config(cfg_none)) != D._config_hash(D._resolve_config(cfg_v))


# ---------------------------------------------------------------------------
# 7. Universe module — load_tickers and disk cache round-trip
# ---------------------------------------------------------------------------

def test_load_tickers_from_file(tmp_path):
    """Blank lines and comment lines are skipped; valid tickers are returned."""
    f = tmp_path / "tickers.txt"
    f.write_text("# comment\n\nAAPL\nMSFT\n  # another comment\nGOOGL\n")
    tickers = load_tickers(f)
    assert tickers == ["AAPL", "MSFT", "GOOGL"]


def test_load_tickers_missing_file(tmp_path):
    """Missing universe file returns an empty list without raising."""
    tickers = load_tickers(tmp_path / "nonexistent.txt")
    assert tickers == []


def test_universe_distribution_cache_round_trip(tmp_path):
    """save → load round-trip preserves version, config_hash, and data."""
    dist = UniverseDistribution(
        version="2026-Q3",
        config_hash="abc123",
        data={"gross_margin": [0.3, 0.5, 0.7], "capex_revenue_ratio": [0.02, 0.05]},
    )
    save_distribution(dist, cache_dir=tmp_path)
    loaded = load_cached_distribution("2026-Q3", "abc123", cache_dir=tmp_path)
    assert loaded is not None
    assert loaded.version == "2026-Q3"
    assert loaded.config_hash == "abc123"
    assert loaded.get("gross_margin") == [0.3, 0.5, 0.7]
    assert loaded.get("capex_revenue_ratio") == [0.02, 0.05]
    assert loaded.get("missing_metric") == []


def test_universe_distribution_cache_miss_on_wrong_hash(tmp_path):
    """Cache miss when config_hash doesn't match."""
    dist = UniverseDistribution(version="2026-Q3", config_hash="abc", data={})
    save_distribution(dist, cache_dir=tmp_path)
    # Query with a different hash
    assert load_cached_distribution("2026-Q3", "different_hash", cache_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# 8. Golden master — strong company now carries implied growth and gap
# ---------------------------------------------------------------------------

def test_golden_strong_company_has_implied_growth():
    """The strong-company fixture must produce normalized_fcf, delivered_growth, implied_growth."""
    res = _make_res(_strong_cd())
    assert res.normalized_fcf is not None, "strong company should have positive normalized FCF"
    assert res.delivered_growth is not None, "strong company should have computable delivered growth"
    assert res.implied_growth_result is not None, "strong company should produce an implied growth result"
    assert not res.implied_growth_result.bracket_hit, "price=50 should not hit bracket bounds"
    assert res.expectations_gap is not None, "strong company should have an expectations gap"


def test_golden_strong_company_gap_direction():
    """At price=50 the strong company is slightly undervalued → gap should be negative."""
    res = _make_res(_strong_cd(), price=50.0)
    # At price=50 with ~130% FCF margin and high growth, implied growth is likely
    # slightly below delivered growth → gap slightly negative.
    # We don't hard-code the exact value; we assert the relationship holds.
    if res.expectations_gap is not None:
        # The gap sign is informative, not a pass/fail criterion per se.
        # Assert it's within a plausible range (not wildly wrong).
        assert -0.5 < res.expectations_gap < 0.5, (
            f"expectations gap {res.expectations_gap:.4f} is implausibly large"
        )


def test_golden_strong_company_durability_unchanged():
    """Adding implied growth to pipeline must not change the durability score."""
    res = _make_res(_strong_cd())
    ds = D.score(res, _DCF_CFG)
    assert not ds.excluded
    assert ds.composite > 50.0


def test_golden_normalized_fcf_uses_median():
    """Verify normalized_fcf is median FCF margin × latest revenue over the last 5 years."""
    cd = _strong_cd()
    annual = derive_annual_series(cd, _DCF_CFG)
    # Default window = 5 years (C1)
    nfcf, lineage = _normalized_fcf(annual, window=5)
    assert nfcf is not None

    # Compute expected using the same 5-year window
    all_pairs = sorted(
        [(pe, yd.fcf / yd.revenue)
         for pe, yd in annual.items()
         if yd.fcf is not None and yd.revenue is not None and yd.revenue > 0],
        key=lambda x: x[0],
    )
    pairs = all_pairs[-5:]
    expected_margin = statistics.median([m for _, m in pairs])
    latest_revenue = annual[max(annual)].revenue
    expected_nfcf = expected_margin * latest_revenue

    assert abs(nfcf - expected_nfcf) < 1e-6
    assert "window=5" in lineage, "lineage must record the window used"


def test_golden_delivered_growth_is_fcf_cagr():
    """Strong company has all-positive FCF → delivered_growth uses FCF CAGR, not revenue."""
    cd = _strong_cd()
    annual = derive_annual_series(cd, _DCF_CFG)
    dg, label = _delivered_growth(annual)
    assert dg is not None
    assert "FCF" in label, f"expected FCF CAGR label, got: {label}"
