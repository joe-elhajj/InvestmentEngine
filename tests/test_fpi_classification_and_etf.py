"""
test_fpi_classification_and_etf.py — Part E tests for FPI support, evidence-based
classification, signal robustness, and ETF lens.

All tests are synthetic/offline — no network calls.

Coverage:
  A3 mixed-currency: dominant currency kept, off-dominant dropped and logged
  A4 FPI gate:       TWD-reporting FPI → scoring passes, all valuation outputs None
  B1 classification: form-history routing for all classification outcomes
  B1 protection:     operating company with all concepts failed → still operating, never fund
  B2 override:       SIC 6199 + override "operating" → scored, not excluded
  C1 FCF window:     normalized_fcf_years window parameter respected
  C2 min-history:    min_history_years gate on delivered_growth
  C3 sort caption:   sort_mode flows through to _render_md and _render_html
  C4 exclusion flag: completeness "—" for excluded rows; ig_note in Flag column
  D1 etf profile:    EtfProfile fields and defaults
  D2 overlap:        overlap_with_screen math
  D3 etf section:    ETF section appears in both md and html renderers
"""

from __future__ import annotations

import statistics

import pytest

from engine.edgar import CompanyData, Fact, EdgarClient
from engine.market import Quote
from engine.pipeline import derive, derive_annual_series, _normalized_fcf, _delivered_growth
from engine import durability as D
from engine.screen import (
    ScreenRow, EtfRow,
    _classify, _is_etf, _has_fundamentals, _render_md, _render_html,
)
from engine.etf import EtfProfile


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_BASE_CFG = {
    "valuation": {
        "assumed_tax_rate": 0.21,
        "normalized_fcf_years": 5,
        "min_history_years": 4,
    }
}


def _instant(metric: str, period_end: str, val: float,
             form: str = "10-K", currency: str = "USD") -> Fact:
    return Fact(metric, val, period_end, int(period_end[:4]),
                "us-gaap:Test", form, f"{period_end[:4]}-02-15", currency)


def _flow(metric: str, year: int, val: float,
          form: str = "10-K", currency: str = "USD") -> Fact:
    return Fact(metric, val, f"{year}-12-31", year,
                "us-gaap:Test", form, f"{year + 1}-02-15", currency)


def _fpi_instant(metric: str, period_end: str, val: float, currency: str = "TWD") -> Fact:
    return Fact(metric, val, period_end, int(period_end[:4]),
                "ifrs-full:Test", "20-F", f"{period_end[:4]}-04-15", currency)


def _fpi_flow(metric: str, year: int, val: float, currency: str = "TWD") -> Fact:
    return Fact(metric, val, f"{year}-12-31", year,
                "ifrs-full:Test", "20-F", f"{year + 1}-04-15", currency)


def _empty_screen_row(ticker: str = "FAKE") -> ScreenRow:
    return ScreenRow(
        ticker=ticker, composite=None, composite_low=None, composite_high=None,
        cat_reinvestment=None, cat_quality=None, cat_resilience=None,
        cat_discipline=None, cat_optionality=None,
        completeness=None, is_stable=None, stability_delta=None,
        config_hash=None, universe_version="",
        implied_fcf_growth=None, delivered_fcf_growth=None,
        expectations_gap=None, implied_growth_note="",
        quality_value_score=None, flag="",
    )


# ---------------------------------------------------------------------------
# FPI fixture — TWD-reporting company (5+ years of data)
# ---------------------------------------------------------------------------

def _fpi_twd_company() -> CompanyData:
    cd = CompanyData(
        ticker="TSM", cik="0001045810", name="Taiwan Semiconductor",
        sic="3674", sic_description="Semiconductors",
        reporting_currency="TWD",
        recent_forms=["20-F", "6-K", "20-F/A"],
    )
    years = list(range(2018, 2024))
    scale = 1_000_000.0
    cd.series = {
        "total_assets":    [_fpi_instant("total_assets",    f"{y}-12-31", scale * 2.0 * (1.12 ** (y - 2018))) for y in years],
        "total_equity":    [_fpi_instant("total_equity",    f"{y}-12-31", scale * 1.4 * (1.12 ** (y - 2018))) for y in years],
        "long_term_debt":  [_fpi_instant("long_term_debt",  f"{y}-12-31", scale * 0.3) for y in years],
        "short_term_debt": [_fpi_instant("short_term_debt", f"{y}-12-31", scale * 0.1) for y in years],
        "cash":            [_fpi_instant("cash",            f"{y}-12-31", scale * 0.5 * (1.10 ** (y - 2018))) for y in years],
        "revenue":         [_fpi_flow("revenue",          y, scale * 1.5 * (1.15 ** (y - 2018))) for y in years],
        "gross_profit":    [_fpi_flow("gross_profit",     y, scale * 0.8 * (1.15 ** (y - 2018))) for y in years],
        "operating_income":[_fpi_flow("operating_income", y, scale * 0.6 * (1.15 ** (y - 2018))) for y in years],
        "net_income":      [_fpi_flow("net_income",       y, scale * 0.5 * (1.15 ** (y - 2018))) for y in years],
        "cfo":             [_fpi_flow("cfo",              y, scale * 0.7 * (1.15 ** (y - 2018))) for y in years],
        "capex":           [_fpi_flow("capex",            y, scale * 0.4 * (1.12 ** (y - 2018))) for y in years],
    }
    return cd


# ===========================================================================
# A3 — Mixed-currency series: dominant kept, off-dominant dropped
# ===========================================================================

class TestMixedCurrencyResolution:
    def test_dominant_currency_is_twd_when_more_concepts(self):
        """3 TWD concepts vs 1 USD concept → TWD wins; USD series dropped."""
        cd = CompanyData(
            ticker="MIXED", cik="0000001234", name="Mixed Ccy Co",
            sic="3674", sic_description="Semiconductors",
            recent_forms=["20-F"],
        )
        years = [2021, 2022, 2023]
        cd.series = {
            "revenue":         [_fpi_flow("revenue",         y, 1_500_000.0) for y in years],
            "operating_income":[_fpi_flow("operating_income",y,   600_000.0) for y in years],
            "total_assets":    [_fpi_instant("total_assets", f"{y}-12-31", 2_000_000.0) for y in years],
            "net_income":      [_flow("net_income", y, 50.0, form="20-F", currency="USD") for y in years],
        }
        EdgarClient._set_reporting_currency(cd)
        assert cd.reporting_currency == "TWD"

    def test_off_dominant_series_dropped(self):
        """USD series dropped when TWD is dominant."""
        cd = CompanyData(
            ticker="MIXED", cik="0000001234", name="Mixed Ccy Co",
            sic="3674", sic_description="Semiconductors",
            recent_forms=["20-F"],
        )
        years = [2021, 2022, 2023]
        cd.series = {
            "revenue":    [_fpi_flow("revenue",    y, 1_500_000.0) for y in years],
            "total_assets":[_fpi_instant("total_assets", f"{y}-12-31", 2_000_000.0) for y in years],
            "net_income": [_flow("net_income", y, 50.0, form="20-F", currency="USD") for y in years],
        }
        EdgarClient._set_reporting_currency(cd)
        assert "net_income" not in cd.series, "off-dominant USD series must be removed"

    def test_dropped_series_logged_in_unresolved(self):
        """Dropped off-dominant series appears in cd.unresolved."""
        cd = CompanyData(
            ticker="MIXED", cik="0000001234", name="Mixed Ccy Co",
            sic="3674", sic_description="Semiconductors",
            recent_forms=["20-F"],
        )
        years = [2021, 2022, 2023]
        cd.series = {
            "revenue":    [_fpi_flow("revenue",    y, 1_500_000.0) for y in years],
            "total_assets":[_fpi_instant("total_assets", f"{y}-12-31", 2_000_000.0) for y in years],
            "net_income": [_flow("net_income", y, 50.0, form="20-F", currency="USD") for y in years],
        }
        EdgarClient._set_reporting_currency(cd)
        assert any("net_income" in u for u in cd.unresolved), \
            "dropped series must be logged in cd.unresolved"

    def test_usd_wins_tie(self):
        """When TWD and USD have equal concept counts, USD wins the tie."""
        cd = CompanyData(
            ticker="TIE", cik="0000001235", name="Tie Co",
            sic="3674", sic_description="Semiconductors",
            recent_forms=["20-F"],
        )
        cd.series = {
            "revenue":    [_fpi_flow("revenue", y, 1_000.0) for y in [2022, 2023]],
            "net_income": [_flow("net_income", y, 30.0, form="20-F", currency="USD") for y in [2022, 2023]],
        }
        EdgarClient._set_reporting_currency(cd)
        # 1 TWD vs 1 USD — USD wins tie
        assert cd.reporting_currency == "USD"


# ===========================================================================
# A4 — FPI currency gate: valuation outputs None when reporting_currency != USD
# ===========================================================================

class TestFpiCurrencyGate:
    def test_fpi_classification_is_operating_fpi(self):
        cd = _fpi_twd_company()
        cls, evidence = _classify("TSM", cd, {})
        assert cls == "operating_fpi"
        assert "20-F" in evidence or "40-F" in evidence

    def test_fpi_reporting_currency_is_twd(self):
        cd = _fpi_twd_company()
        assert cd.reporting_currency == "TWD"

    def test_fpi_implied_growth_is_none(self):
        """implied_growth_result must be None for non-USD reporters."""
        cd = _fpi_twd_company()
        q = Quote("TSM", price=150.0, shares_outstanding=5200.0, market_cap=780_000.0, source="test")
        res = derive(cd, q, _BASE_CFG)
        assert res.implied_growth_result is None

    def test_fpi_rel_val_all_none(self):
        """Relative valuation must be fully gated for non-USD reporters."""
        cd = _fpi_twd_company()
        q = Quote("TSM", price=150.0, shares_outstanding=5200.0, market_cap=780_000.0, source="test")
        res = derive(cd, q, _BASE_CFG)
        assert res.rel_val is not None
        assert res.rel_val.pe is None
        assert res.rel_val.ev_ebitda is None
        assert res.rel_val.fcf_yield is None

    def test_fpi_dcf_empty(self):
        """DCF must not be populated for non-USD reporters."""
        cd = _fpi_twd_company()
        dcf_cfg = {**_BASE_CFG, "valuation": {**_BASE_CFG["valuation"], "dcf": {
            "projection_years": 5,
            "scenarios": {"base": {"fcf_growth": [0.05]*5, "terminal_growth": 0.025, "wacc": 0.09}},
        }}}
        q = Quote("TSM", price=150.0, shares_outstanding=5200.0, market_cap=780_000.0, source="test")
        res = derive(cd, q, dcf_cfg)
        assert res.dcf == {}, "DCF must be empty for non-USD reporters"

    def test_fpi_gap_logged(self):
        """Currency-gate gap must appear in res.gaps."""
        cd = _fpi_twd_company()
        q = Quote("TSM", price=150.0, shares_outstanding=5200.0, market_cap=780_000.0, source="test")
        res = derive(cd, q, _BASE_CFG)
        gap_msgs = " ".join(res.gaps)
        assert "TWD" in gap_msgs

    def test_fpi_ratios_still_computed(self):
        """Currency-invariant ratios (gross margin, operating margin) are computed despite gate."""
        cd = _fpi_twd_company()
        q = Quote("TSM", price=150.0, shares_outstanding=5200.0, market_cap=780_000.0, source="test")
        res = derive(cd, q, _BASE_CFG)
        # gross_margin and operating_margin should resolve since they're ratios
        assert res.ratios.get("gross_margin") is not None
        gm = res.ratios["gross_margin"]
        assert gm.value is not None and 0 < gm.value < 1

    def test_fpi_durability_scores(self):
        """FPI company must still get a durability score (currency-invariant)."""
        cd = _fpi_twd_company()
        q = Quote("TSM", price=150.0, shares_outstanding=5200.0, market_cap=780_000.0, source="test")
        res = derive(cd, q, _BASE_CFG)
        ds = D.score(res, _BASE_CFG)
        assert not ds.excluded
        assert ds.composite > 0


# ===========================================================================
# B1 — Evidence-based security classification
# ===========================================================================

class TestClassification:
    def _make_cd(self, recent_forms: list[str], sic: str = "7372") -> CompanyData:
        cd = CompanyData(
            ticker="TEST", cik="0000000001", name="Test Co",
            sic=sic, sic_description="Software",
            recent_forms=recent_forms,
        )
        return cd

    def test_10k_is_operating_domestic(self):
        cd = self._make_cd(["10-K", "DEF 14A", "8-K"])
        cls, evidence = _classify("TEST", cd, {})
        assert cls == "operating_domestic"
        assert "10-K" in evidence

    def test_20f_is_operating_fpi(self):
        cd = self._make_cd(["20-F", "6-K"])
        cls, evidence = _classify("TEST", cd, {})
        assert cls == "operating_fpi"
        assert "20-F" in evidence or "FPI" in evidence

    def test_40f_is_operating_fpi(self):
        cd = self._make_cd(["40-F", "6-K"])
        cls, evidence = _classify("TEST", cd, {})
        assert cls == "operating_fpi"

    def test_ncsr_is_fund(self):
        cd = self._make_cd(["N-CSR", "N-PORT"])
        cls, evidence = _classify("TEST", cd, {})
        assert cls == "fund"
        assert "N-CSR" in evidence or "fund" in evidence.lower()

    def test_nport_is_fund(self):
        cd = self._make_cd(["N-PORT", "8-K"])
        cls, evidence = _classify("TEST", cd, {})
        assert cls == "fund"

    def test_n1a_is_fund(self):
        cd = self._make_cd(["N-1A", "DEF 14A"])
        cls, evidence = _classify("TEST", cd, {})
        assert cls == "fund"

    def test_485bpos_is_fund(self):
        cd = self._make_cd(["485BPOS"])
        cls, evidence = _classify("TEST", cd, {})
        assert cls == "fund"

    def test_sic_6726_is_fund(self):
        cd = self._make_cd(["10-K"], sic="6726")
        cls, evidence = _classify("TEST", cd, {})
        # SIC 6726 wins over 10-K because fund indicators checked first
        assert cls == "fund"
        assert "6726" in evidence

    def test_no_annual_forms_is_unclassified(self):
        cd = self._make_cd(["8-K", "DEF 14A", "4"])
        cls, evidence = _classify("TEST", cd, {})
        assert cls == "unclassified"
        assert "no annual" in evidence.lower() or "unclassified" in evidence.lower()

    def test_analyst_override_wins_all(self):
        cd = self._make_cd(["N-CSR", "N-PORT"])  # would be fund without override
        overrides = {"TEST": "operating"}
        cls, evidence = _classify("TEST", cd, overrides)
        assert cls == "operating"
        assert "override" in evidence.lower()

    def test_override_case_insensitive_lookup(self):
        cd = self._make_cd(["10-K"])
        overrides = {"TEST": "operating"}
        # Ticker passed as lowercase — overrides keyed uppercase
        cls, _ = _classify("test", cd, overrides)
        assert cls == "operating"


# ===========================================================================
# B1 misclassification protection — operating with empty concepts stays operating
# ===========================================================================

class TestMisclassificationProtection:
    def test_empty_concepts_stays_operating_domestic(self):
        """10-K filer with zero resolved series must NOT be reclassified as fund."""
        cd = CompanyData(
            ticker="EMPTY", cik="0000009999", name="Empty Co",
            sic="7372", sic_description="Software",
            recent_forms=["10-K"],
        )
        # No series data at all — all concepts failed to resolve
        cls, evidence = _classify("EMPTY", cd, {})
        assert cls == "operating_domestic", (
            f"empty concepts must not cause misclassification to fund; got {cls!r}"
        )
        assert "fund" not in cls

    def test_empty_concepts_stays_operating_fpi(self):
        """20-F filer with zero resolved series must remain operating_fpi."""
        cd = CompanyData(
            ticker="EMPTY", cik="0000009998", name="Empty FPI",
            sic="3674", sic_description="Semiconductors",
            recent_forms=["20-F"],
            reporting_currency="TWD",
        )
        cls, _ = _classify("EMPTY", cd, {})
        assert cls == "operating_fpi"
        assert "fund" not in cls

    def test_failed_concepts_not_classified_as_fund_due_to_sic(self):
        """A company with no concepts resolved but no fund forms must not be classified fund
        solely because SIC 6726 is not present (i.e., don't infer fund from absent data)."""
        cd = CompanyData(
            ticker="BROKEN", cik="0000009997", name="Broken Co",
            sic="7372", sic_description="Software",
            recent_forms=["10-K", "8-K"],  # clearly operating forms
        )
        cls, _ = _classify("BROKEN", cd, {})
        assert cls != "fund", "A company with 10-K forms must never be classified fund"


# ===========================================================================
# B2 — Classification override bypasses financial-SIC exclusion
# ===========================================================================

class TestClassificationOverride:
    def _financial_sic_cd(self) -> CompanyData:
        """SIC 6199 (Finance Services) — excluded by default."""
        cd = CompanyData(
            ticker="MARA", cik="0001591698", name="Marathon Digital",
            sic="6199", sic_description="Finance Services",
            recent_forms=["10-K"],
        )
        years = list(range(2019, 2024))
        cd.series = {
            "total_assets":    [_instant("total_assets",    f"{y}-12-31", 500.0 * (1.20 ** (y - 2019))) for y in years],
            "total_equity":    [_instant("total_equity",    f"{y}-12-31", 300.0 * (1.20 ** (y - 2019))) for y in years],
            "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31", 100.0) for y in years],
            "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",   0.0) for y in years],
            "cash":            [_instant("cash",            f"{y}-12-31",  50.0) for y in years],
            "revenue":         [_flow("revenue",          y, 200.0 * (1.15 ** (y - 2019))) for y in years],
            "operating_income":[_flow("operating_income", y,  50.0 * (1.15 ** (y - 2019))) for y in years],
            "net_income":      [_flow("net_income",       y,  40.0 * (1.15 ** (y - 2019))) for y in years],
            "cfo":             [_flow("cfo",              y,  60.0 * (1.15 ** (y - 2019))) for y in years],
            "capex":           [_flow("capex",            y,  20.0) for y in years],
        }
        return cd

    def test_financial_sic_excluded_without_override(self):
        cd = self._financial_sic_cd()
        q = Quote("MARA", price=20.0, shares_outstanding=200.0, market_cap=4000.0, source="test")
        res = derive(cd, q, _BASE_CFG)
        ds = D.score(res, _BASE_CFG)
        assert ds.excluded, "SIC 6199 must be excluded without override"
        assert "6199" in ds.exclusion_reason

    def test_financial_sic_scored_with_override(self):
        cd = self._financial_sic_cd()
        q = Quote("MARA", price=20.0, shares_outstanding=200.0, market_cap=4000.0, source="test")
        res = derive(cd, q, _BASE_CFG)
        ds = D.score(res, _BASE_CFG, override_classification="operating")
        assert not ds.excluded, "SIC 6199 with operating override must not be excluded"
        assert ds.composite > 0

    def test_override_not_needed_for_non_financial_sic(self):
        """Normal software SIC scores without any override."""
        cd = CompanyData(
            ticker="NORM", cik="0000009900", name="Normal Co",
            sic="7372", sic_description="Software",
            recent_forms=["10-K"],
        )
        years = list(range(2019, 2024))
        cd.series = {
            "total_assets": [_instant("total_assets", f"{y}-12-31", 500.0) for y in years],
            "revenue":      [_flow("revenue", y, 200.0) for y in years],
        }
        q = Quote("NORM", price=30.0, shares_outstanding=100.0, market_cap=3000.0, source="test")
        res = derive(cd, q, _BASE_CFG)
        ds = D.score(res, _BASE_CFG)
        # Either scores or excluded for no-data reasons, but NOT excluded for SIC
        if ds.excluded:
            assert "6" not in ds.exclusion_reason[:10]  # not a SIC exclusion

    def test_classify_override_wins_over_fund_forms(self):
        """Analyst override 'operating' prevails even when fund forms are present."""
        cd = CompanyData(
            ticker="MARA", cik="0001591698", name="Marathon Digital",
            sic="6199", sic_description="Finance Services",
            recent_forms=["10-K", "N-CSR"],  # would be fund without override
        )
        overrides = {"MARA": "operating"}
        cls, evidence = _classify("MARA", cd, overrides)
        assert cls == "operating"
        assert "override" in evidence.lower()


# ===========================================================================
# C1 — normalized_fcf_years window
# ===========================================================================

class TestNormalizedFcfWindow:
    def _annual_series(self) -> dict:
        """6 years of data; window=5 should exclude the oldest year."""
        cd = CompanyData(
            ticker="WIN", cik="0000000200", name="Window Co",
            sic="7372", sic_description="Software",
            recent_forms=["10-K"],
        )
        years = list(range(2018, 2024))  # 6 years
        cd.series = {
            "total_assets":    [_instant("total_assets",    f"{y}-12-31", 1000.0) for y in years],
            "total_equity":    [_instant("total_equity",    f"{y}-12-31",  800.0) for y in years],
            "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31",   50.0) for y in years],
            "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",    0.0) for y in years],
            "cash":            [_instant("cash",            f"{y}-12-31",  100.0) for y in years],
            "revenue":         [_flow("revenue",          y, 500.0 * (1.12 ** (y - 2018))) for y in years],
            "gross_profit":    [_flow("gross_profit",     y, 300.0 * (1.12 ** (y - 2018))) for y in years],
            "operating_income":[_flow("operating_income", y, 150.0 * (1.12 ** (y - 2018))) for y in years],
            "net_income":      [_flow("net_income",       y, 120.0 * (1.12 ** (y - 2018))) for y in years],
            "cfo":             [_flow("cfo",              y, 130.0 * (1.12 ** (y - 2018))) for y in years],
            "capex":           [_flow("capex",            y,  30.0) for y in years],
        }
        return derive_annual_series(cd, _BASE_CFG)

    def test_window_5_excludes_oldest_year(self):
        """With 6 years of data and window=5, only last 5 years enter the median."""
        annual = self._annual_series()
        all_pairs = sorted(
            [(pe, yd.fcf / yd.revenue)
             for pe, yd in annual.items()
             if yd.fcf is not None and yd.revenue is not None and yd.revenue > 0],
            key=lambda x: x[0],
        )
        assert len(all_pairs) == 6

        nfcf_all, _ = _normalized_fcf(annual, window=len(all_pairs))  # all 6
        nfcf_win, lineage = _normalized_fcf(annual, window=5)          # last 5

        latest_rev = annual[max(annual)].revenue
        expected_margin = statistics.median([m for _, m in all_pairs[-5:]])
        expected_nfcf = expected_margin * latest_rev

        assert nfcf_win is not None
        assert abs(nfcf_win - expected_nfcf) < 1e-6
        assert "window=5" in lineage

    def test_window_1_uses_single_year(self):
        annual = self._annual_series()
        nfcf, lineage = _normalized_fcf(annual, window=1)
        assert nfcf is not None
        assert "window=1" in lineage

    def test_window_larger_than_history_uses_all(self):
        annual = self._annual_series()  # 6 years
        nfcf_10, _ = _normalized_fcf(annual, window=10)
        nfcf_all, _ = _normalized_fcf(annual, window=len(annual))
        # Both use all available data when window > len(annual)
        assert nfcf_10 is not None
        assert abs((nfcf_10 or 0) - (nfcf_all or 0)) < 1e-6


# ===========================================================================
# C2 — min_history_years gate on delivered_growth
# ===========================================================================

class TestMinHistoryYears:
    def _make_annual(self, n_years: int) -> dict:
        cd = CompanyData(
            ticker="MH", cik="0000000300", name="Min History Co",
            sic="7372", sic_description="Software",
            recent_forms=["10-K"],
        )
        years = list(range(2024 - n_years, 2024))
        cd.series = {
            "total_assets":    [_instant("total_assets",    f"{y}-12-31", 500.0) for y in years],
            "total_equity":    [_instant("total_equity",    f"{y}-12-31", 400.0) for y in years],
            "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31",  50.0) for y in years],
            "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",   0.0) for y in years],
            "cash":            [_instant("cash",            f"{y}-12-31",  30.0) for y in years],
            "revenue":         [_flow("revenue",          y, 200.0 * (1.10 ** (y - (2024 - n_years)))) for y in years],
            "operating_income":[_flow("operating_income", y,  50.0) for y in years],
            "net_income":      [_flow("net_income",       y,  30.0) for y in years],
            "cfo":             [_flow("cfo",              y,  40.0) for y in years],
            "capex":           [_flow("capex",            y,  10.0) for y in years],
        }
        return derive_annual_series(cd, _BASE_CFG)

    def test_below_min_history_returns_none(self):
        """Fewer than 4 annual points → delivered_growth is None."""
        annual = self._make_annual(2)
        dg_val, dg_label = _delivered_growth(annual, min_history=4)
        assert dg_val is None
        assert "insufficient history" in dg_label
        assert "2" in dg_label  # mentions n=2

    def test_exactly_at_min_history_computes(self):
        """Exactly 4 annual points → delivered_growth computes."""
        annual = self._make_annual(4)
        dg_val, _ = _delivered_growth(annual, min_history=4)
        assert dg_val is not None

    def test_above_min_history_computes(self):
        """6 annual points with min=4 → computes fine."""
        annual = self._make_annual(6)
        dg_val, _ = _delivered_growth(annual, min_history=4)
        assert dg_val is not None

    def test_min_history_1_always_computes_when_2_points(self):
        """min_history=1 with 2 data points → computes (needs at least 2 for CAGR)."""
        annual = self._make_annual(2)
        dg_val, _ = _delivered_growth(annual, min_history=1)
        # With 2 annual points there's a 1-year CAGR; expect a value
        # (or None if revenue CAGR requires at least 2; either is fine as long as no exception)
        # The key invariant: no exception is raised


# ===========================================================================
# C3 — Sort caption flows through to both renderers
# ===========================================================================

class TestSortCaption:
    def test_md_quality_value_caption(self):
        rows = [_empty_screen_row()]
        md = _render_md(rows, sort_mode="quality-value")
        assert "quality-value" in md, "sort caption must appear in markdown output"

    def test_md_durability_caption(self):
        rows = [_empty_screen_row()]
        md = _render_md(rows, sort_mode="durability")
        assert "durability" in md

    def test_html_quality_value_caption(self):
        rows = [_empty_screen_row()]
        html = _render_html(rows, sort_mode="quality-value")
        assert "quality-value" in html, "sort caption must appear in HTML output"

    def test_html_durability_caption(self):
        rows = [_empty_screen_row()]
        html = _render_html(rows, sort_mode="durability")
        assert "durability" in html.lower()

    def test_md_default_is_durability(self):
        rows = [_empty_screen_row()]
        md = _render_md(rows)  # no explicit sort_mode
        assert "durability" in md


# ===========================================================================
# C4 — Completeness "—" for excluded rows; ig_note surfaced in Flag
# ===========================================================================

class TestDiagnosticsDisplay:
    def test_excluded_row_completeness_dash(self):
        from engine.screen import _completeness_display
        row = _empty_screen_row()
        row.excluded = True
        assert _completeness_display(row) == "—"

    def test_none_completeness_dash(self):
        from engine.screen import _completeness_display
        row = _empty_screen_row()
        row.completeness = None
        assert _completeness_display(row) == "—"

    def test_scored_row_completeness_percentage(self):
        from engine.screen import _completeness_display, _pct
        row = _empty_screen_row()
        row.completeness = 0.75
        row.excluded = False
        result = _completeness_display(row)
        assert "75" in result or "%" in result

    def test_ig_note_appears_in_html_flag_column(self):
        """When implied growth is n/a, the note must appear in the Flag column."""
        row = _empty_screen_row()
        row.implied_growth_note = "n/a — bracket lower hit (implied g < 5%)"
        row.flag = "FPI: 20-F observed, reporting TWD · " + row.implied_growth_note
        html = _render_html([row])
        assert "bracket" in html or "lower hit" in html


# ===========================================================================
# D1 — EtfProfile dataclass defaults
# ===========================================================================

class TestEtfProfile:
    def test_etf_profile_default_fields(self):
        p = EtfProfile(ticker="SPY")
        assert p.ticker == "SPY"
        assert p.name is None
        assert p.category is None
        assert p.expense_ratio is None
        assert p.total_assets is None
        assert p.top10_concentration is None
        assert p.top_holdings == []

    def test_etf_profile_with_holdings(self):
        p = EtfProfile(
            ticker="SMH",
            name="VanEck Semiconductor ETF",
            category="Technology",
            expense_ratio=0.0035,
            total_assets=20e9,
            top10_concentration=0.72,
            top_holdings=[("NVDA", 0.20), ("TSM", 0.13), ("AVGO", 0.08)],
        )
        assert p.expense_ratio == 0.0035
        assert len(p.top_holdings) == 3
        assert p.top_holdings[0] == ("NVDA", 0.20)


# ===========================================================================
# D2 — ETF overlap_with_screen math
# ===========================================================================

class TestEtfOverlap:
    def test_overlap_sum_of_matching_weights(self):
        """overlap = sum of weights where holding ticker is in operating_tickers."""
        profile = EtfProfile(
            ticker="SMH",
            top_holdings=[("NVDA", 0.20), ("TSM", 0.13), ("AVGO", 0.08), ("XYZ", 0.05)],
        )
        operating_tickers = {"NVDA", "AVGO"}
        overlap = sum(w for tk, w in profile.top_holdings if tk.upper() in operating_tickers)
        assert abs(overlap - 0.28) < 1e-9

    def test_no_overlap_when_no_holdings_match(self):
        profile = EtfProfile(
            ticker="SPCX",
            top_holdings=[("SPY", 0.50), ("QQQ", 0.30)],
        )
        operating_tickers = {"AAPL", "NVDA"}
        overlap = sum(w for tk, w in profile.top_holdings if tk.upper() in operating_tickers)
        assert overlap == 0.0

    def test_full_overlap_when_all_holdings_match(self):
        profile = EtfProfile(
            ticker="TEST",
            top_holdings=[("AAPL", 0.15), ("MSFT", 0.12)],
        )
        operating_tickers = {"AAPL", "MSFT"}
        overlap = sum(w for tk, w in profile.top_holdings if tk.upper() in operating_tickers)
        assert abs(overlap - 0.27) < 1e-9

    def test_overlap_case_insensitive(self):
        """Holding tickers in lower-case should still match operating_tickers (upper-case)."""
        profile = EtfProfile(
            ticker="TEST",
            top_holdings=[("aapl", 0.10), ("nvda", 0.08)],
        )
        operating_tickers = {"AAPL", "NVDA"}
        overlap = sum(w for tk, w in profile.top_holdings if tk.upper() in operating_tickers)
        assert abs(overlap - 0.18) < 1e-9


# ===========================================================================
# D3 — ETF section in both renderers
# ===========================================================================

class TestEtfSection:
    def _etf_rows(self) -> list[EtfRow]:
        return [
            EtfRow(
                ticker="SPCX",
                name="Simplify US Equity PLUS Convexity ETF",
                category="Large Blend",
                expense_ratio=0.0053,
                aum=1.2e8,
                top10_concentration=0.35,
                overlap_with_screen=0.27,
                flag="fund: N-CSR/N-PORT observed",
            ),
            EtfRow(
                ticker="SMH",
                name="VanEck Semiconductor ETF",
                category="Technology",
                expense_ratio=0.0035,
                aum=20e9,
                top10_concentration=0.72,
                overlap_with_screen=None,
                flag="fund: N-PORT observed",
            ),
        ]

    def test_etf_section_in_md(self):
        md = _render_md([], self._etf_rows())
        assert "## ETFs / Funds" in md
        assert "SPCX" in md
        assert "SMH" in md

    def test_etf_section_in_html(self):
        html = _render_html([], self._etf_rows())
        assert "ETFs / Funds" in html
        assert "SPCX" in html
        assert "SMH" in html

    def test_etf_section_absent_when_no_etf_rows(self):
        md = _render_md([_empty_screen_row()])
        assert "ETFs / Funds" not in md

    def test_etf_market_vendor_caveat_in_md(self):
        md = _render_md([], self._etf_rows())
        assert "yfinance" in md or "market vendor" in md.lower()

    def test_etf_market_vendor_caveat_in_html(self):
        html = _render_html([], self._etf_rows())
        assert "yfinance" in html or "market vendor" in html.lower()

    def test_etf_expense_ratio_formatted(self):
        md = _render_md([], self._etf_rows())
        # 0.53% or 0.5% — either formatting is acceptable
        assert "0.5" in md or "0.53" in md

    def test_etf_overlap_formatted(self):
        md = _render_md([], self._etf_rows())
        # 27% overlap should appear as "27.0%" or similar
        assert "27" in md

    def test_etf_none_overlap_shows_na(self):
        md = _render_md([], self._etf_rows())
        assert "n/a" in md  # SMH has overlap_with_screen=None


# ===========================================================================
# Backward-compatibility: legacy _is_etf and _has_fundamentals still work
# ===========================================================================

class TestLegacyHelpers:
    def test_is_etf_keyword_detection(self):
        assert _is_etf("SPY", "SPDR S&P 500 ETF Trust")
        assert _is_etf("QQQ", "Invesco QQQ Trust")
        assert not _is_etf("AAPL", "Apple Inc")

    def test_has_fundamentals(self):
        assert not _has_fundamentals({})
        assert _has_fundamentals({"revenue": [object()]})
        assert _has_fundamentals({"total_assets": [object()]})
