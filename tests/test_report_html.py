"""
test_report_html.py — tests for engine/report_html.py's two renderers.

Verifies the Task 4 refactor: render() (dark, full-page) and
render_fragment() (light, embeddable) are thin renderers over the same
shared section-builders — nothing about which figures appear should differ
between them, only how they're laid out.
"""

from __future__ import annotations

import re

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
        assert "DCF Base (Systematic)" in frag

    def test_durability_composite_none_shows_na_not_zero(self):
        """stat-value-na is a styling hook only (renders n/a in neutral
        gray instead of the bold near-ink used for a real value) — the
        displayed text is still exactly "n/a", never a fabricated 0."""
        frag = RH.render_fragment(_company_result(), durability_composite=None)
        assert '<span class="stat-value stat-value-na">n/a</span>' in frag

    def test_dcf_stat_has_tooltip(self):
        """Task 5: DCF card relabeled with a tooltip explaining the systematic,
        cross-ticker-comparable nature of the single-stage DCF."""
        frag = RH.render_fragment(_company_result())
        assert '<div class="stat has-tooltip" tabindex="0">' in frag
        assert "Single-stage DCF under systematic config assumptions" in frag
        assert "The expectations gap is the primary signal." in frag

    def test_data_gaps_explanatory_line_and_list_when_gaps_present(self):
        res = _company_result()
        res.gaps = ["revenue: no value for period 2025-12-31 (latest available is 2024-12-31, not used)"]
        frag = RH.render_fragment(res)
        assert "Could not be resolved from EDGAR; excluded rather than defaulted to zero." in frag
        assert 'class="gaps-list"' in frag
        assert res.gaps[0] in frag

    def test_data_gaps_none_message_when_no_gaps(self):
        frag = RH.render_fragment(_company_result())  # fixture has no gaps
        assert "None — all targeted concepts resolved." in frag
        assert "gaps-list" not in frag

    def test_collapsible_sections_present(self):
        frag = RH.render_fragment(_company_result())
        assert frag.count("<details") >= 4
        assert "<summary>Financial position</summary>" in frag
        assert "<summary>Growth</summary>" in frag
        assert "<summary>Margins &amp; returns</summary>" in frag  # html.escape()'d "&"

    def test_no_section_open_by_default_on_row_expand(self):
        """Every <details> in the equity fragment — including Financial
        position, which used to auto-open — must render collapsed. The
        user opens each section they want; nothing is pre-expanded."""
        frag = RH.render_fragment(_company_result())
        details_tags = re.findall(r"<details[^>]*>", frag)
        assert len(details_tags) >= 4
        assert all(" open" not in tag for tag in details_tags)

    def test_lineage_reachable_via_hover_title(self):
        """Lineage isn't a visible column in the fragment, but must still be reachable."""
        frag = RH.render_fragment(_company_result())
        assert "<th>Source</th>" not in frag  # condensed — no visible source column
        assert 'title="' in frag              # but the lineage is on a hover title

    def test_sources_toggle_present_for_financial_position(self):
        """Task 4: per-figure lineage is restored via a Sources toggle, not
        just the hover title — a muted monospace sub-row per data row,
        default hidden (revealed by the frontend's .sources-on class)."""
        frag = RH.render_fragment(_company_result())
        assert '<button class="sources-toggle" type="button">Sources</button>' in frag
        assert 'class="section-toolbar"' in frag
        assert 'class="source-row"' in frag
        assert 'class="l source-cell"' in frag

    def test_sources_toggle_lineage_matches_hover_title(self):
        """The revealed sub-row and the hover title carry the same lineage string."""
        frag = RH.render_fragment(_company_result())
        # Revenue's source is a plain Fact (us-gaap:Test | 10-K | period ... | filed ...)
        assert "us-gaap:Test | 10-K | period 2025-12-31 | filed 2026-02-15" in frag

    def test_sources_toggle_absent_from_sections_without_lineage(self):
        """Growth/Margins/Valuation/Data gaps never had per-row source
        strings — no toggle should be fabricated for them."""
        frag = RH.render_fragment(_company_result())
        # Exactly one toggle: Financial position (this fixture has no
        # quarterly data, so Latest quarter never renders).
        assert frag.count('class="sources-toggle"') == 1

    def test_source_row_hidden_by_default_in_markup(self):
        """Default off means the CSS class, not inline display — the toggle
        is a frontend interaction, not something the renderer decides per-request."""
        frag = RH.render_fragment(_company_result())
        assert "sources-on" not in frag  # never rendered server-side as "on"


class TestFlagsSection:
    """Task 5 (Tier 2): a 'Flags' <details> on the equity fragment only —
    never the ETF fragment, since funds don't file 10-Ks. Unlike every
    other section, its content is a client-side-populated placeholder
    (app.js fetches /api/flags/{ticker} on first expand), not server-
    rendered data — so these tests check the wiring (data-ticker, class,
    placeholder), not any actual flag content."""

    def test_flags_section_present_on_equity_fragment(self):
        frag = RH.render_fragment(_company_result())
        assert 'class="report-section flags-section"' in frag
        assert "<summary>Flags</summary>" in frag

    def test_flags_section_carries_the_correct_ticker(self):
        frag = RH.render_fragment(_company_result())
        assert 'data-ticker="RPT"' in frag  # _company_result()'s CompanyData ticker

    def test_flags_section_is_closed_by_default(self):
        frag = RH.render_fragment(_company_result())
        # The <details ...> tag for flags-section must not carry ` open`
        import re
        match = re.search(r'<details class="report-section flags-section"[^>]*>', frag)
        assert match
        assert " open" not in match.group(0)

    def test_flags_section_absent_from_etf_fragment(self):
        from engine.etf import EtfProfile
        profile = EtfProfile(ticker="QQQ", name="Invesco QQQ Trust", quote_type="ETF")
        frag = RH.render_etf_fragment(profile, price=500.0, evidence="ETF/Fund — fund forms observed", overlap_matches=[])
        assert "flags-section" not in frag
        assert "<summary>Flags</summary>" not in frag


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
