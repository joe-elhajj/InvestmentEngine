"""
test_gap_scenario_band.py — PR 3: expectations gap as a bull/base/bear band.

Covers:
  - expectations_gap_band() reads (wacc, terminal_growth) PAIRS from config,
    not a WACC-alone sweep (dead-key prevention: changing a bundle value
    moves that scenario's implied growth)
  - band is None for NO_BAND: delivered_growth is None, a bundle isn't
    configured, or the base scenario itself fails to converge
  - base_gap byte-matches implied_growth() - delivered_growth computed
    independently (the ADDITIVE invariant)
  - COMPLETE band: monotonic bull <= base <= bear implied growth
  - sign flip across scenarios -> FRAGILE; magnitude-only variation -> STABLE
  - PARTIAL (a scenario's bisection misses the bracket) -> fragile is
    UNDETERMINABLE, never silently STABLE
  - a genuinely inverted (pathological) bundle ordering trips the
    monotonicity AssertionError guard
  - pipeline wiring: res.expectations_gap_band is additive, gaps disclosure
    on PARTIAL, NO_BAND mirrors today's no-gap case
"""

import pytest

from engine import valuation as V
from engine.pipeline import derive
from engine.edgar import CompanyData, Fact
from engine.market import Quote

# ---------------------------------------------------------------------------
# Shared config/fixtures
# ---------------------------------------------------------------------------

_DCF_CFG = {
    "valuation": {
        "assumed_tax_rate": 0.21,
        "min_history_years": 4,
        "dcf": {
            "projection_years": 5,
            "scenarios": {
                "bear": {"wacc": 0.11, "terminal_growth": 0.015, "fcf_growth": 0.03},
                "base": {"wacc": 0.09, "terminal_growth": 0.025, "fcf_growth": 0.08},
                "bull": {"wacc": 0.08, "terminal_growth": 0.030, "fcf_growth": 0.14},
            },
        },
    }
}


_DEFAULT_PRICE = 216.4529461607036  # base-case fair value at g=10% -- lands
# mid-bracket (neither -20% nor +60% bound) for base/bull/bear alike, so
# COMPLETE-band fixtures don't accidentally hit a bracket edge.


def _band(price=_DEFAULT_PRICE, shares=10.0, net_debt=0.0, norm_fcf=100.0,
          delivered_growth=0.05, config=None):
    return V.expectations_gap_band(
        price=price, shares=shares, net_debt=net_debt, norm_fcf=norm_fcf,
        delivered_growth=delivered_growth, config=config or _DCF_CFG,
    )


# ---------------------------------------------------------------------------
# NO_BAND / None-propagation
# ---------------------------------------------------------------------------

def test_band_none_when_delivered_growth_none():
    assert _band(delivered_growth=None) is None


def test_band_none_when_price_missing():
    assert _band(price=None) is None


def test_band_none_when_shares_missing():
    assert _band(shares=None) is None


def test_band_none_when_net_debt_missing():
    assert _band(net_debt=None) is None


def test_band_none_when_norm_fcf_missing():
    assert _band(norm_fcf=None) is None


def test_band_none_when_bundle_not_configured():
    cfg = {
        "valuation": {"dcf": {"projection_years": 5, "scenarios": {
            "base": {"wacc": 0.09, "terminal_growth": 0.025},
            "bull": {"wacc": 0.08, "terminal_growth": 0.030},
            # bear missing entirely
        }}}
    }
    assert _band(config=cfg) is None


def test_band_none_when_base_fails_to_converge():
    """An absurdly high price makes even the base scenario's bisection miss
    the +60% bracket -- identical to today's no-gap case, no band at all."""
    band = _band(price=1e9, shares=1.0, norm_fcf=1.0)
    assert band is None


# ---------------------------------------------------------------------------
# Dead-key prevention: bundles are read from config, not hardcoded
# ---------------------------------------------------------------------------

def test_band_reads_bundles_from_config_not_hardcoded():
    band_default = _band()
    assert band_default is not None
    default_bear_g = band_default.scenarios["bear"].implied_growth

    cfg2 = {
        "valuation": {"dcf": {"projection_years": 5, "scenarios": {
            "bear": {"wacc": 0.20, "terminal_growth": 0.015},  # far harsher than 0.11
            "base": {"wacc": 0.09, "terminal_growth": 0.025},
            "bull": {"wacc": 0.08, "terminal_growth": 0.030},
        }}}
    }
    band_changed = _band(config=cfg2)
    assert band_changed is not None
    assert band_changed.scenarios["bear"].implied_growth != pytest.approx(default_bear_g)
    assert band_changed.scenarios["bear"].wacc == 0.20


# ---------------------------------------------------------------------------
# Additive invariant: base_gap byte-matches independently computed single gap
# ---------------------------------------------------------------------------

def test_base_gap_byte_matches_independent_single_scenario_gap():
    price, shares, net_debt, norm_fcf, delivered = _DEFAULT_PRICE, 10.0, 0.0, 100.0, 0.05
    igr = V.implied_growth(price, shares, net_debt, norm_fcf, _DCF_CFG)
    assert igr is not None and not igr.bracket_hit
    expected_gap = igr.implied_growth - delivered

    band = _band(price=price, shares=shares, net_debt=net_debt, norm_fcf=norm_fcf,
                 delivered_growth=delivered)
    assert band is not None
    assert band.base_gap == expected_gap
    assert band.scenarios["base"].implied_growth == igr.implied_growth


# ---------------------------------------------------------------------------
# COMPLETE band: monotonicity
# ---------------------------------------------------------------------------

def test_complete_band_is_monotonic_bull_le_base_le_bear():
    band = _band()
    assert band is not None
    assert band.band_status == "COMPLETE"
    bull_g = band.scenarios["bull"].implied_growth
    base_g = band.scenarios["base"].implied_growth
    bear_g = band.scenarios["bear"].implied_growth
    assert bull_g <= base_g <= bear_g


def test_monotonicity_guard_trips_on_pathological_inverted_bundles():
    """A deliberately inverted config (bull harsher than bear) is a real
    computation-order violation -- must raise, never silently render."""
    cfg = {
        "valuation": {"dcf": {"projection_years": 5, "scenarios": {
            "bull": {"wacc": 0.11, "terminal_growth": 0.015},   # harsh, mislabeled "bull"
            "base": {"wacc": 0.09, "terminal_growth": 0.025},
            "bear": {"wacc": 0.08, "terminal_growth": 0.030},   # lenient, mislabeled "bear"
        }}}
    }
    with pytest.raises(AssertionError):
        _band(config=cfg)


# ---------------------------------------------------------------------------
# Fragility: sign flip vs magnitude-only variation
# ---------------------------------------------------------------------------

def test_sign_flip_across_scenarios_is_fragile():
    probe = _band(delivered_growth=0.0)
    assert probe is not None and probe.band_status == "COMPLETE"
    bull_g = probe.scenarios["bull"].implied_growth
    bear_g = probe.scenarios["bear"].implied_growth
    midpoint = (bull_g + bear_g) / 2.0

    band = _band(delivered_growth=midpoint)
    assert band is not None
    assert band.band_status == "COMPLETE"
    assert band.fragile == "FRAGILE"
    signs = {(1 if band.scenarios[s].gap > 0 else -1) for s in ("bull", "base", "bear")}
    assert len(signs) > 1, "fixture setup failed to produce a genuine sign flip"


def test_magnitude_only_variation_is_stable_not_fragile():
    """Delivered growth far below every scenario's implied growth: every
    gap is positive (same sign), magnitudes differ -- must NOT be FRAGILE."""
    band = _band(delivered_growth=-0.90)
    assert band is not None
    assert band.band_status == "COMPLETE"
    gaps = [band.scenarios[s].gap for s in ("bull", "base", "bear")]
    assert all(g > 0 for g in gaps)
    assert len(set(gaps)) == 3, "magnitudes should differ across scenarios"
    assert band.fragile == "STABLE"


# ---------------------------------------------------------------------------
# PARTIAL band: bear bracket failure -> UNDETERMINABLE, never silently STABLE
# ---------------------------------------------------------------------------

def _bear_only_partial_price(norm_fcf=100.0, net_debt=0.0, shares=10.0):
    """Finds a price where bear's bisection misses the +60% bracket while
    base (and bull) still converge -- bear's higher WACC + lower terminal
    growth means, for a fixed g, its forward fair value is always <= base's
    and bull's, so there's a price band strictly above what bear can explain
    at g=60% but still below what base can explain at g=60%."""
    common = {"projection_years": 5}
    fvps_bear_max = V.two_stage_dcf(
        norm_fcf, net_debt, shares, None, "bear",
        {**common, "wacc": 0.11, "terminal_growth": 0.015, "fcf_growth": 0.60},
    ).fair_value_per_share
    fvps_base_max = V.two_stage_dcf(
        norm_fcf, net_debt, shares, None, "base",
        {**common, "wacc": 0.09, "terminal_growth": 0.025, "fcf_growth": 0.60},
    ).fair_value_per_share
    assert fvps_base_max > fvps_bear_max, "fixture assumption violated -- adjust WACC spread"
    return (fvps_bear_max + fvps_base_max) / 2.0


def test_partial_band_bear_bracket_failure_is_undeterminable():
    price = _bear_only_partial_price()
    band = _band(price=price, delivered_growth=0.05)
    assert band is not None
    assert band.band_status == "PARTIAL"
    assert band.scenarios["base"].converged
    assert band.scenarios["bull"].converged
    assert not band.scenarios["bear"].converged
    assert band.scenarios["bear"].bracket_bound == "upper"
    assert band.fragile == "UNDETERMINABLE"
    assert band.fragile not in ("STABLE", "FRAGILE")


# ---------------------------------------------------------------------------
# Pipeline wiring (engine/pipeline.py::derive)
# ---------------------------------------------------------------------------

def _instant(metric: str, pe: str, v: float) -> Fact:
    return Fact(metric, v, pe, int(pe[:4]), "us-gaap:Test", "10-K", f"{pe[:4]}-02-15")


def _flow(metric: str, year: int, v: float) -> Fact:
    return Fact(metric, v, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year + 1}-02-15")


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


def _derive_strong(price=50.0):
    cd = _strong_cd()
    q = Quote(cd.ticker, price=price, shares_outstanding=100.0, market_cap=price * 100.0, source="test")
    return derive(cd, q, _DCF_CFG)


def test_pipeline_wires_band_additively():
    res = _derive_strong()
    assert res.expectations_gap is not None
    assert res.expectations_gap_band is not None
    assert res.expectations_gap_band.base_gap == res.expectations_gap


def test_pipeline_no_band_when_delivered_growth_none():
    cd = CompanyData(ticker="THIN", cik="2222222222", name="Thin Co",
                      sic="7372", sic_description="Prepackaged Software")
    # Only 2 years of revenue -- below min_history_years=4, delivered_growth None.
    cd.series = {
        "total_assets":    [_instant("total_assets", "2022-12-31", 500.0), _instant("total_assets", "2023-12-31", 550.0)],
        "total_equity":    [_instant("total_equity", "2022-12-31", 400.0), _instant("total_equity", "2023-12-31", 440.0)],
        "revenue":         [_flow("revenue", 2022, 200.0), _flow("revenue", 2023, 220.0)],
        "cfo":             [_flow("cfo", 2022, 50.0), _flow("cfo", 2023, 55.0)],
        "capex":           [_flow("capex", 2022, 10.0), _flow("capex", 2023, 10.0)],
    }
    q = Quote(cd.ticker, price=50.0, shares_outstanding=10.0, market_cap=500.0, source="test")
    res = derive(cd, q, _DCF_CFG)
    assert res.delivered_growth is None
    assert res.expectations_gap_band is None
    assert res.expectations_gap is None


def test_pipeline_partial_band_appends_gaps_disclosure():
    # Derive once at a throwaway price to read this fixture's actual
    # normalized_fcf/net_debt/shares, then compute the real bear-only
    # PARTIAL boundary price from those exact values (not guessed constants)
    # and derive again at that price.
    probe = _derive_strong(price=50.0)
    price = _bear_only_partial_price(
        norm_fcf=probe.normalized_fcf, net_debt=probe.derived["net_debt"],
        shares=probe.quote.shares_outstanding,
    )
    res = _derive_strong(price=price)
    band = res.expectations_gap_band
    assert band is not None
    assert band.band_status == "PARTIAL"
    assert any(
        g.startswith("expectations_gap: ") and "band incomplete" in g and "bear" in g
        for g in res.gaps
    )
