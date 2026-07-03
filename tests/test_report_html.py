"""
test_report_html.py — tests for engine/report_html.py's two renderers.

Verifies the Task 4 refactor: render() (dark, full-page) and
render_fragment() (light, embeddable) are thin renderers over the same
shared section-builders — nothing about which figures appear should differ
between them, only how they're laid out.
"""

from __future__ import annotations

from engine import report_html as RH
from engine.edgar import CompanyData, Fact
from engine.market import Quote
from engine.pipeline import derive

_CFG = {
    "valuation": {
        "assumed_tax_rate": 0.21,
        "dcf": {
            "projection_years": 5,
            "scenarios": {
                "base": {"fcf_growth": [0.06] * 5, "terminal_growth": 0.025, "wacc": 0.09},
            },
        },
    }
}


def _instant(metric: str, period_end: str, val: float) -> Fact:
    return Fact(metric, val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", "2026-02-15")


def _flow(metric: str, year: int, val: float) -> Fact:
    return Fact(metric, val, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year + 1}-02-15")


def _company_result():
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
    quote = Quote("RPT", price=50.0, shares_outstanding=100.0, market_cap=5000.0, source="test")
    return derive(cd, quote, _CFG)


class TestDarkFullPageRenderer:
    def test_is_a_complete_standalone_document(self):
        html = RH.render(_company_result())
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "<html" in html and "<head" in html and "</html>" in html

    def test_includes_all_expected_sections(self):
        html = RH.render(_company_result())
        for heading in (
            "Financial position (latest FY)", "Growth (CAGR)",
            "Margins, returns, leverage", "Valuation", "Data gaps / not verified",
        ):
            assert heading in html

    def test_source_column_visible_for_lineage(self):
        """Dark page keeps the third Source column visible (not hover-only)."""
        html = RH.render(_company_result())
        assert "<th>Source</th>" in html
        assert "<code>" in html  # lineage strings rendered as visible <code>


class TestLightFragmentRenderer:
    def test_no_html_or_head_wrapper(self):
        frag = RH.render_fragment(_company_result())
        assert "<html" not in frag
        assert "<head" not in frag
        assert "<!DOCTYPE" not in frag
        assert frag.strip().startswith('<div class="report-fragment">')

    def test_includes_gap_section(self):
        frag = RH.render_fragment(_company_result())
        assert "Data gaps" in frag

    def test_includes_summary_strip_with_all_five_stats(self):
        frag = RH.render_fragment(_company_result(), durability_composite=71.4)
        assert "Price" in frag
        assert "Market Cap" in frag
        assert "Durability" in frag
        assert "71.4" in frag
        assert "Expectations Gap" in frag
        assert "DCF Base Upside" in frag

    def test_durability_composite_none_shows_na_not_zero(self):
        frag = RH.render_fragment(_company_result(), durability_composite=None)
        assert '<span class="stat-value">n/a</span>' in frag

    def test_collapsible_sections_present(self):
        frag = RH.render_fragment(_company_result())
        assert frag.count("<details") >= 4
        assert "<summary>Financial position</summary>" in frag
        assert "<summary>Growth</summary>" in frag
        assert "<summary>Margins &amp; returns</summary>" in frag  # html.escape()'d "&"

    def test_lineage_reachable_via_hover_title(self):
        """Lineage isn't a visible column in the fragment, but must still be reachable."""
        frag = RH.render_fragment(_company_result())
        assert "<th>Source</th>" not in frag  # condensed — no visible source column
        assert 'title="' in frag              # but the lineage is on a hover title


class TestSharedBuildersProduceConsistentData:
    """The dark and light renderers must show the SAME figures — only the
    layout differs. This is the regression test for 'do not fork the report
    logic into two copies.'"""

    def test_revenue_matches_between_renderers(self):
        res = _company_result()
        dark = RH.render(res)
        light = RH.render_fragment(res)
        revenue_str = RH._fmt_number(res.derived.get("revenue"))
        assert revenue_str in dark
        assert revenue_str in light

    def test_dcf_fair_value_matches_between_renderers(self):
        res = _company_result()
        dark = RH.render(res)
        light = RH.render_fragment(res)
        base = res.dcf.get("base")
        if base is not None:
            fv = RH._fmt_currency(base.fair_value_per_share)
            assert fv in dark
            assert fv in light

    def test_gaps_list_identical_between_renderers(self):
        res = _company_result()
        for g in res.gaps:
            assert g in RH.render(res)
            assert g in RH.render_fragment(res)
