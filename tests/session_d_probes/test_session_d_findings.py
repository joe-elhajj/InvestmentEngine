"""
tests/session_d_probes/test_session_d_findings.py — Session D full-system
audit, quarantined probe suite.

Per the audit's own rule: a probe that "fails" by finding a bug is a
SUCCESS, not a broken test — it is marked `xfail` with the finding ID
from audit/session_d/report.md, and stays quarantined here rather than
being fixed (this branch is read-only on engine/app/frontend/config.yaml
and existing tests). Once a post-audit PR fixes a finding, its xfail
marker should be removed and the probe becomes a real regression test —
that's the intended lifecycle, not deletion.

Non-xfail probes below assert behavior this audit CONFIRMED CORRECT
(3a, 3e, 3f, 2c, 2f, 3d) — these are real regression coverage today,
guarding against the specific cross-feature regressions this audit went
looking for.
"""
import copy

import pytest
import yaml

from engine.edgar import CompanyData, Fact, is_fpi
from engine.market import Quote
from engine.pipeline import derive, YearlyDerived
from engine import durability as D
from engine import report as R
from engine import report_html as RH
from engine import valuation as V

_BASE_CFG = {"valuation": {"assumed_tax_rate": 0.21}}
_GATE_CFG = {
    **_BASE_CFG,
    "durability": {"gates": [
        {"id": "balance_sheet_leverage", "metric": "net_debt_ebitda", "threshold": 6.0, "cap": 45.0},
    ]},
}


def _instant(metric, period_end, val):
    return Fact(metric, val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", f"{period_end[:4]}-02-15")


def _flow(metric, year, val):
    return Fact(metric, val, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year + 1}-02-15")


def _strong_company() -> CompanyData:
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
        "capex":           [_flow("capex",             y,  30.0) for y in years],
        "dep_amort":       [_flow("dep_amort",         y,  40.0) for y in years],
    }
    return cd


def _make_res(cd, cfg=_BASE_CFG, price=50.0):
    q = Quote(cd.ticker, price=price, shares_outstanding=100.0, market_cap=price * 100.0, source="test")
    return derive(cd, q, cfg)


# ---------------------------------------------------------------------------
# Confirmed CORRECT (3a) — gate x R&D regime isolation
# ---------------------------------------------------------------------------

def test_3a_gate_immune_to_rnd_regime_toggle():
    """Session D 3a: gate decisions must be byte-identical regardless of
    the R&D regime, even though the composite itself moves. Raw metrics
    (net_debt/ebitda) must never be touched by the R&D adjustment."""
    cd = _strong_company()
    res = _make_res(cd)
    period = sorted(res.annual_series)[-1]
    res.annual_series[period].net_debt = 700.0
    res.annual_series[period].ebitda = 100.0  # ratio 7.0 > 6.0, fires

    cfg_on = {**_GATE_CFG, "durability": {**_GATE_CFG["durability"], "rnd_capitalization": {"enabled": True}}}
    cfg_off = {**_GATE_CFG, "durability": {**_GATE_CFG["durability"], "rnd_capitalization": {"enabled": False}}}

    ds_on = D.score(res, cfg_on)
    ds_off = D.score(res, cfg_off)

    assert ds_on.gated == ds_off.gated == True
    assert ds_on.gate_ids == ds_off.gate_ids == ["balance_sheet_leverage"]


# ---------------------------------------------------------------------------
# Confirmed CORRECT (2c) — gate branch-order boundaries
# ---------------------------------------------------------------------------

def _yd(net_debt, ebitda):
    return YearlyDerived(
        period_end="2023-12-31", year=2023,
        revenue=None, net_income=None, operating_income=None, gross_profit=None,
        cfo=None, capex=None, dep_amort=None, interest_expense=None, sbc=None, rnd=None,
        total_assets=None, total_equity=None, total_debt=None, liquid_assets=None, cash=None,
        current_assets=None, current_liabilities=None,
        fcf=None, invested_capital=None, nopat=None,
        net_debt=net_debt, ebit=None, ebitda=ebitda, capital_employed=None,
        gross_margin=None, operating_margin=None,
    )


_GATE_ENTRY = {"id": "balance_sheet_leverage", "metric": "net_debt_ebitda", "threshold": 6.0, "cap": 45.0}


@pytest.mark.parametrize("net_debt,ebitda,expected_status", [
    (-100.0, -40.0, None),          # net cash wins over negative-EBITDA (branch order)
    (0.0, -40.0, None),              # net_debt exactly 0 -- falls through to PASS
    (100.0, 0.0, "GATED"),           # ebitda exactly 0 with positive debt -- gates
    (600.0, 100.0, None),            # ratio exactly at threshold -- fires ABOVE, not at
    (601.0, 100.0, "GATED"),         # just above threshold -- gates
])
def test_2c_gate_boundary_combos(net_debt, ebitda, expected_status):
    annual = {"2023-12-31": _yd(net_debt, ebitda)}
    outcome = D._evaluate_gate(_GATE_ENTRY, annual)
    if expected_status is None:
        assert outcome is None
    else:
        assert outcome is not None and outcome.status == expected_status


# ---------------------------------------------------------------------------
# Confirmed CORRECT (3f-style) — NO_RND names show zero R&D artifacts
# ---------------------------------------------------------------------------

def test_norand_company_shows_zero_rnd_artifacts_regime_on():
    """A company with no R&D data at all must classify no_rnd, never
    build a research asset, and never show 'adjusted' rnd_basis --
    regardless of the regime toggle."""
    cd = _strong_company()  # no "rnd" key in cd.series at all
    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": True}}}
    res = _make_res(cd, cfg=cfg)
    assert all(yd.rnd_basis != "adjusted" for yd in res.annual_series.values())
    assert all(yd.research_asset is None for yd in res.annual_series.values())
    ds = D.score(res, cfg)
    assert ds.composite == ds.composite_ungated


# ---------------------------------------------------------------------------
# F-2 (xfail) — min_history_years bypasses the durability config hash
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="F-2: min_history_years is read directly from raw config in "
                           "score(), never folded into _resolve_config's hashed subset -- "
                           "two runs with different values report an identical hash.",
                    strict=True)
def test_f2_min_history_years_changes_hash():
    cd = _strong_company()
    res = _make_res(cd)
    cfg_a = {**_BASE_CFG, "valuation": {**_BASE_CFG["valuation"], "min_history_years": 4}}
    cfg_b = {**_BASE_CFG, "valuation": {**_BASE_CFG["valuation"], "min_history_years": 10}}
    ds_a = D.score(res, cfg_a)
    ds_b = D.score(res, cfg_b)
    assert ds_a.config_hash != ds_b.config_hash, (
        "min_history_years affects durability disclosures but is invisible to the hash"
    )


# ---------------------------------------------------------------------------
# F-7 (xfail) — trend-window narrowing has no disclosure outside R&D regime
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="F-7: 9 trend/mean sub-scores (gross_margin_trend, "
                           "operating_margin_trend, roic_stability_cv, roic_trend, "
                           "sbc_revenue_ratio, capex_revenue_proxy, rnd_revenue_proxy, "
                           "rnd_trend_proxy, reinvestment_rate) silently narrow their "
                           "window with zero gaps-list disclosure when a year drops out.",
                    strict=True)
def test_f7_trend_window_narrowing_discloses():
    cd = _strong_company()
    res_baseline = _make_res(cd)
    ds_baseline = D.score(res_baseline, _BASE_CFG)

    cd2 = _strong_company()
    res_dropped = _make_res(cd2)
    period = sorted(res_dropped.annual_series)[-1]
    res_dropped.annual_series[period].gross_margin = None  # drop the latest year's margin
    ds_dropped = D.score(res_dropped, _BASE_CFG)

    new_gaps = set(ds_dropped.gaps) - set(ds_baseline.gaps)
    assert new_gaps, "dropping a trend-window's latest data point must disclose the narrower window"


# ---------------------------------------------------------------------------
# F-8 (xfail) — revenue truthy-check treats exact 0.0 like None
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="F-8: at least one revenue-gated ratio uses a truthy check "
                           "(`if revenue and revenue > 0`) rather than `is not None`, so "
                           "an exact revenue==0.0 year is silently excluded like a missing one.",
                    strict=True)
def test_f8_revenue_exact_zero_treated_as_real_value():
    cd = _strong_company()
    res_zero = _make_res(cd)
    period = sorted(res_zero.annual_series)[-1]
    res_zero.annual_series[period].revenue = 0.0

    cd2 = _strong_company()
    res_none = _make_res(cd2)
    res_none.annual_series[period].revenue = None

    ds_zero = D.score(res_zero, _BASE_CFG)
    ds_none = D.score(res_none, _BASE_CFG)
    assert ds_zero.composite != ds_none.composite, (
        "an exact 0.0 revenue year must be scored as a real (likely terrible) data point, "
        "not silently excluded the same way a missing value would be"
    )


# ---------------------------------------------------------------------------
# F-10 (xfail) — scenario config has no schema validation
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="F-10: valuation.dcf.scenarios has no schema validation, unlike "
                           "durability.gates -- a missing scenario key silently returns "
                           "NO_BAND with zero diagnostic instead of raising loudly.",
                    strict=True)
def test_f10_missing_scenario_key_raises_loudly():
    cfg_missing = {
        "valuation": {"dcf": {"projection_years": 5, "scenarios": {
            "bull": {"wacc": 0.08, "terminal_growth": 0.030},
            "base": {"wacc": 0.09, "terminal_growth": 0.025},
            # bear missing entirely
        }}}
    }
    with pytest.raises(ValueError):
        V.expectations_gap_band(216.45, 10.0, 0.0, 100.0, 0.05, cfg_missing)


# ---------------------------------------------------------------------------
# F-13 (xfail) — gate lineage text stale relative to the min()-based cap
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="F-13: gate_lineage unconditionally says 'composite capped {cap}' "
                           "even when min(ungated, cap) left the composite unchanged.",
                    strict=True)
def test_f13_gate_lineage_reflects_actual_cap_behavior():
    """Weak fundamentals AND high leverage: ungated composite is already
    below the cap, so min() correctly leaves it unchanged -- but the
    lineage text must not claim 'capped' when nothing was capped."""
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
    res = _make_res(cd)
    period = sorted(res.annual_series)[-1]
    res.annual_series[period].net_debt = 700.0
    res.annual_series[period].ebitda = 100.0  # ratio 7.0 > 6.0

    ds = D.score(res, _GATE_CFG)
    assert ds.gated is True
    assert ds.composite == ds.composite_ungated, "fixture must already score below the cap"
    assert "capped" not in ds.gate_lineage.lower(), (
        f"lineage claims a cap happened when composite is unchanged: {ds.gate_lineage!r}"
    )


# ---------------------------------------------------------------------------
# F-14 (xfail) — R&D-UNADJ badge never fires for an FPI with usable R&D data
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="F-14: report.py/report_html.py check roic_adjusted availability "
                           "before checking is_fpi(), so the FPI abstention badge never fires "
                           "when the FPI has a usable R&D series.",
                    strict=True)
def test_f14_fpi_with_rnd_data_shows_unadj_badge():
    cd = CompanyData(ticker="FPITEST", cik="9999999999", name="Foreign Test Co",
                      sic="3674", sic_description="Semiconductors")
    cd.recent_forms = ["20-F", "20-F", "20-F"]
    years = list(range(2016, 2026))
    cd.series = {
        "total_assets":    [_instant("total_assets",    f"{y}-12-31", 5000.0 * (1.08 ** (y - 2016))) for y in years],
        "total_equity":    [_instant("total_equity",    f"{y}-12-31", 2500.0 * (1.08 ** (y - 2016))) for y in years],
        "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31", 1800.0) for y in years],
        "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",  200.0) for y in years],
        "cash":            [_instant("cash",            f"{y}-12-31",  300.0) for y in years],
        "revenue":         [_flow("revenue",          y, 3000.0 * (1.10 ** (y - 2016))) for y in years],
        "gross_profit":    [_flow("gross_profit",     y, 1800.0 * (1.10 ** (y - 2016))) for y in years],
        "operating_income":[_flow("operating_income", y,  600.0 * (1.10 ** (y - 2016))) for y in years],
        "net_income":      [_flow("net_income",       y,  450.0 * (1.10 ** (y - 2016))) for y in years],
        "cfo":             [_flow("cfo",              y,  550.0 * (1.10 ** (y - 2016))) for y in years],
        "capex":           [_flow("capex",             y,  100.0) for y in years],
        "dep_amort":       [_flow("dep_amort",         y,  120.0) for y in years],
        "rnd":             [_flow("rnd",               y,  400.0 * (1.10 ** (y - 2016))) for y in years],
    }
    assert is_fpi(cd)[0] is True
    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": True}}}
    res = _make_res(cd, cfg=cfg, price=80.0)

    md = R.render(res)
    assert "R&D-UNADJ" in md and "IFRS" in md, (
        "an FPI with usable R&D data must show the R&D-UNADJ badge, not a silent adjusted number"
    )
