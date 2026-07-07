"""
test_gap_band_rendering.py — PR 3: expectations-gap scenario band rendering.

Covers the three rendering surfaces:
  - engine/report_html.py: render() (dark, full report) and render_fragment()
    (light, dashboard embed) both show the range strip + scenario table for
    a COMPLETE band, mark the failed edge + disclosure for a PARTIAL band,
    and render nothing new (byte-identical Valuation section) for NO_BAND.
  - engine/report.py: markdown scenario table + textual band, same states.
  - engine/screen.py: _gap_band_columns() maps a COMPLETE/PARTIAL/NO_BAND
    AnalysisResult onto the (band_status, fragile, scenarios) triple the
    dashboard's FRAG/FRAG? chip reads.
"""

from __future__ import annotations

from engine import report as R
from engine import report_html as RH
from engine import valuation as V
from engine.edgar import CompanyData, Fact
from engine.market import Quote
from engine.pipeline import derive
from engine.screen import _gap_band_columns

_FULL_CFG = {
    "valuation": {
        "assumed_tax_rate": 0.21,
        "min_history_years": 4,
        "dcf": {
            "projection_years": 5,
            "scenarios": {
                "bull": {"fcf_growth": [0.14, 0.12, 0.10, 0.08, 0.06], "terminal_growth": 0.030, "wacc": 0.08},
                "base": {"fcf_growth": [0.08, 0.07, 0.06, 0.05, 0.04], "terminal_growth": 0.025, "wacc": 0.09},
                "bear": {"fcf_growth": [0.03, 0.03, 0.02, 0.02, 0.02], "terminal_growth": 0.015, "wacc": 0.11},
            },
        },
    }
}

_NO_BEAR_CFG = {
    "valuation": {
        "assumed_tax_rate": 0.21,
        "dcf": {
            "projection_years": 5,
            "scenarios": {
                "bull": {"fcf_growth": [0.14] * 5, "terminal_growth": 0.030, "wacc": 0.08},
                "base": {"fcf_growth": [0.08] * 5, "terminal_growth": 0.025, "wacc": 0.09},
            },
        },
    }
}


def _instant(metric: str, period_end: str, val: float) -> Fact:
    return Fact(metric, val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", "2026-02-15")


def _flow(metric: str, year: int, val: float) -> Fact:
    return Fact(metric, val, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year + 1}-02-15")


def _company_cd() -> CompanyData:
    cd = CompanyData(ticker="RPT", cik="0000000200", name="Report Co",
                      sic="7372", sic_description="Software")
    years = list(range(2021, 2026))
    cd.series = {
        "total_assets":   [_instant("total_assets", f"{y}-12-31", 1000.0 * (1.1 ** i)) for i, y in enumerate(years)],
        "total_equity":   [_instant("total_equity", f"{y}-12-31", 600.0 * (1.1 ** i)) for i, y in enumerate(years)],
        "long_term_debt": [_instant("long_term_debt", f"{y}-12-31", 200.0) for y in years],
        "short_term_debt": [_instant("short_term_debt", f"{y}-12-31", 20.0) for y in years],
        "cash":           [_instant("cash", f"{y}-12-31", 150.0 * (1.1 ** i)) for i, y in enumerate(years)],
        "revenue":        [_flow("revenue", y, 500.0 * (1.12 ** i)) for i, y in enumerate(years)],
        "operating_income": [_flow("operating_income", y, 100.0 * (1.12 ** i)) for i, y in enumerate(years)],
        "net_income":     [_flow("net_income", y, 80.0 * (1.12 ** i)) for i, y in enumerate(years)],
        "cfo":            [_flow("cfo", y, 90.0 * (1.12 ** i)) for i, y in enumerate(years)],
        "capex":          [_flow("capex", y, 20.0) for y in years],
    }
    return cd


def _derive_at(price: float, cfg=_FULL_CFG):
    cd = _company_cd()
    quote = Quote("RPT", price=price, shares_outstanding=100.0, market_cap=price * 100.0, source="test")
    return derive(cd, quote, cfg)


def _complete_band_result():
    """A price landing mid-bracket for all three bundles -- COMPLETE band."""
    probe = _derive_at(300.0)
    fv = V.two_stage_dcf(
        probe.normalized_fcf, probe.derived["net_debt"], probe.quote.shares_outstanding,
        None, "base", {"projection_years": 5, "wacc": 0.09, "terminal_growth": 0.025, "fcf_growth": 0.12},
    ).fair_value_per_share
    return _derive_at(fv)


def _partial_band_result():
    """A price where bear's bisection misses the +60% bracket while base
    (and bull) still converge -- same construction as
    test_gap_scenario_band.py's _bear_only_partial_price, against this
    fixture's real normalized_fcf/net_debt/shares."""
    probe = _derive_at(300.0)
    common = {"projection_years": 5}
    nf, nd, sh = probe.normalized_fcf, probe.derived["net_debt"], probe.quote.shares_outstanding
    fvps_bear_max = V.two_stage_dcf(
        nf, nd, sh, None, "bear", {**common, "wacc": 0.11, "terminal_growth": 0.015, "fcf_growth": 0.60},
    ).fair_value_per_share
    fvps_base_max = V.two_stage_dcf(
        nf, nd, sh, None, "base", {**common, "wacc": 0.09, "terminal_growth": 0.025, "fcf_growth": 0.60},
    ).fair_value_per_share
    price = (fvps_bear_max + fvps_base_max) / 2.0
    return _derive_at(price)


def _no_band_result():
    """Bear bundle isn't configured at all -- NO_BAND, band is None."""
    return _derive_at(300.0, cfg=_NO_BEAR_CFG)


# ---------------------------------------------------------------------------
# Fixture sanity (guards the rendering tests below against silent fixture drift)
# ---------------------------------------------------------------------------

def test_fixture_complete_band_is_actually_complete():
    band = _complete_band_result().expectations_gap_band
    assert band is not None and band.band_status == "COMPLETE"


def test_fixture_partial_band_is_actually_partial():
    band = _partial_band_result().expectations_gap_band
    assert band is not None and band.band_status == "PARTIAL"
    assert not band.scenarios["bear"].converged


def test_fixture_no_band_is_actually_none():
    assert _no_band_result().expectations_gap_band is None


# ---------------------------------------------------------------------------
# report_html.py — dark full-page render()
# ---------------------------------------------------------------------------

class TestReportHtmlDarkRenderer:
    def test_complete_band_renders_strip_and_table(self):
        html = RH.render(_complete_band_result())
        assert '<div class="gap-band">' in html
        assert "gap-band-point-bull" in html
        assert "gap-band-point-base" in html
        assert "gap-band-point-bear" in html
        assert "gap-band-point-failed" not in html.split("</style>", 1)[1]

    def test_partial_band_marks_failed_edge_and_disclosure(self):
        html = RH.render(_partial_band_result())
        assert "gap-band-point-failed" in html.split("</style>", 1)[1]
        assert "band incomplete" in html

    def test_no_band_renders_no_gap_band_markup(self):
        html = RH.render(_no_band_result())
        assert '<div class="gap-band">' not in html


# ---------------------------------------------------------------------------
# report_html.py — light render_fragment()
# ---------------------------------------------------------------------------

class TestReportHtmlFragmentRenderer:
    def test_complete_band_renders_strip_and_table(self):
        html = RH.render_fragment(_complete_band_result())
        assert "gap-band-strip" in html
        assert "gap-band-table" in html
        assert html.count("gap-band-point-failed") == 0

    def test_fragile_complete_band_shows_disclosure(self):
        res = _complete_band_result()
        band = res.expectations_gap_band
        if band.fragile != "FRAGILE":
            # Force a sign flip the same way test_gap_scenario_band.py does,
            # keeping everything else about the fixture identical.
            bull_g = band.scenarios["bull"].implied_growth
            bear_g = band.scenarios["bear"].implied_growth
            forced = V.expectations_gap_band(
                price=res.quote.price, shares=res.quote.shares_outstanding,
                net_debt=res.derived["net_debt"], norm_fcf=res.normalized_fcf,
                delivered_growth=(bull_g + bear_g) / 2.0, config=_FULL_CFG,
            )
            assert forced is not None and forced.fragile == "FRAGILE"
            res.expectations_gap_band = forced
        html = RH.render_fragment(res)
        assert "FRAGILE" in html

    def test_partial_band_marks_failed_edge_and_disclosure(self):
        html = RH.render_fragment(_partial_band_result())
        assert "gap-band-point-failed" in html
        assert "band incomplete" in html
        assert "UNDETERMINABLE" in html

    def test_no_band_renders_no_gap_band_markup(self):
        html = RH.render_fragment(_no_band_result())
        assert "gap-band" not in html


# ---------------------------------------------------------------------------
# report.py — markdown renderer
# ---------------------------------------------------------------------------

class TestMarkdownRenderer:
    def test_complete_band_renders_scenario_table(self):
        md = R.render(_complete_band_result())
        assert "Expectations gap" in md
        assert "| bull |" in md
        assert "| base |" in md
        assert "| bear |" in md

    def test_partial_band_renders_disclosure_and_undeterminable(self):
        md = R.render(_partial_band_result())
        assert "not converged" in md
        assert "UNDETERMINABLE" in md

    def test_no_band_renders_no_scenario_table(self):
        md = R.render(_no_band_result())
        assert "bull/base/bear band" not in md


# ---------------------------------------------------------------------------
# screen.py — _gap_band_columns
# ---------------------------------------------------------------------------

class TestScreenGapBandColumns:
    def test_complete_band_columns(self):
        res = _complete_band_result()
        status, fragile, scenarios = _gap_band_columns(res)
        assert status == "COMPLETE"
        assert fragile in ("FRAGILE", "STABLE")
        assert {s["scenario"] for s in scenarios} == {"bull", "base", "bear"}
        assert all(s["converged"] for s in scenarios)

    def test_partial_band_columns(self):
        res = _partial_band_result()
        status, fragile, scenarios = _gap_band_columns(res)
        assert status == "PARTIAL"
        assert fragile == "UNDETERMINABLE"
        bear = next(s for s in scenarios if s["scenario"] == "bear")
        assert bear["converged"] is False

    def test_no_band_columns(self):
        res = _no_band_result()
        status, fragile, scenarios = _gap_band_columns(res)
        assert status is None
        assert fragile is None
        assert scenarios == []
