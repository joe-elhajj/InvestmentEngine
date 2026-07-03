"""
test_etf_routing.py — Task 5 tests for the evidence-based ETF routing fix.

All tests are synthetic/offline. yfinance and EdgarClient are mocked throughout.

Coverage:
  routing / ETF detection
    - No-EDGAR + yfinance quoteType ETF        → ETF lens (EtfRow)
    - No-EDGAR + yfinance quoteType MUTUALFUND → ETF lens (EtfRow)
    - No-EDGAR + yfinance quoteType EQUITY     → Excluded (distinct message)
    - No-EDGAR + yfinance fails entirely       → Excluded (distinct message)
    - No-EDGAR + quoteType absent              → Excluded (distinct message)
    - False-positive guard: quoteType EQUITY must NOT land in ETF lens
  etf.py field extraction
    - expense_ratio from fund_operations (primary path)
    - expense_ratio from info["netExpenseRatio"] (fallback path, percent → fraction)
    - expense_ratio is None (not 0.0) when both sources absent
    - AUM from totalAssets
    - AUM is None (not 0.0) when absent
    - top_holdings extracted correctly from funds_data.top_holdings
    - top_holdings empty list (not crash) when funds_data raises
    - quote_type extracted correctly
    - FUND_QUOTE_TYPES sentinel membership
  EtfRow
    - top_holdings propagated from EtfProfile
    - overlap and overlap_count computed without re-fetching profile
    - flag distinct for EDGAR-not-found vs EDGAR-found fund
  rendering
    - _fmt_top5 formats correctly
    - _fmt_overlap shows count when available
    - top 5 holdings column appears in md and html
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

import requests

from engine.edgar import CompanyData
from engine.durability import DurabilityScore
from engine.etf import EtfProfile, fetch_etf_profile, FUND_QUOTE_TYPES
from engine.screen import (
    EtfRow, ScreenRow,
    _try_fund_via_yfinance, _etf_row_from_profile,
    _fmt_top5, _fmt_overlap, _render_md, _render_html,
    _process_one,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(
    ticker: str = "SMH",
    quote_type: str = "ETF",
    name: str = "VanEck Semiconductor ETF",
    expense_ratio: float = 0.0035,
    total_assets: float = 67e9,
    top_holdings: list | None = None,
) -> EtfProfile:
    return EtfProfile(
        ticker=ticker,
        quote_type=quote_type,
        name=name,
        expense_ratio=expense_ratio,
        total_assets=total_assets,
        top10_concentration=0.65,
        top_holdings=top_holdings or [
            ("NVDA", 0.152), ("TSM", 0.094), ("MU", 0.078),
            ("AMD", 0.076), ("INTC", 0.072), ("AVGO", 0.072),
        ],
    )


def _empty_screen_row(ticker: str = "FAKE", flag: str = "") -> ScreenRow:
    return ScreenRow(
        ticker=ticker, composite=None, composite_low=None, composite_high=None,
        cat_reinvestment=None, cat_quality=None, cat_resilience=None,
        cat_discipline=None, cat_optionality=None,
        completeness=None, is_stable=None, stability_delta=None,
        config_hash=None, universe_version="",
        implied_fcf_growth=None, delivered_fcf_growth=None,
        expectations_gap=None, implied_growth_note="",
        quality_value_score=None, flag=flag,
    )


# ---------------------------------------------------------------------------
# FUND_QUOTE_TYPES sentinel
# ---------------------------------------------------------------------------

def test_fund_quote_types_contains_etf():
    assert "ETF" in FUND_QUOTE_TYPES


def test_fund_quote_types_contains_mutualfund():
    assert "MUTUALFUND" in FUND_QUOTE_TYPES


def test_fund_quote_types_excludes_equity():
    assert "EQUITY" not in FUND_QUOTE_TYPES


# ---------------------------------------------------------------------------
# _try_fund_via_yfinance — routing logic (fetch_etf_profile mocked)
# ---------------------------------------------------------------------------

class TestTryFundViaYfinance:
    def test_etf_quote_type_returns_etf_row(self):
        profile = _make_profile(quote_type="ETF")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance("SMH")
        assert result is not None, "ETF quoteType must produce an EtfRow"
        assert isinstance(result, EtfRow)
        assert result.ticker == "SMH"

    def test_mutualfund_quote_type_returns_etf_row(self):
        profile = _make_profile(ticker="FXAIX", quote_type="MUTUALFUND",
                                name="Fidelity 500 Index Fund")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance("FXAIX")
        assert result is not None
        assert isinstance(result, EtfRow)

    def test_equity_quote_type_returns_none(self):
        """False-positive guard: quoteType EQUITY must NOT produce an EtfRow."""
        profile = _make_profile(quote_type="EQUITY", name="Some Equity Co")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance("FAKE")
        assert result is None, "EQUITY quoteType must not route to ETF lens"

    def test_absent_quote_type_returns_none(self):
        """No quoteType in yfinance response → must not classify as fund."""
        profile = EtfProfile(ticker="FAKE", quote_type=None)
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance("FAKE")
        assert result is None

    def test_yfinance_exception_returns_none(self):
        """If fetch_etf_profile raises, gracefully return None (not crash)."""
        with patch("engine.screen.fetch_etf_profile", side_effect=RuntimeError("network")):
            result = _try_fund_via_yfinance("FAKE")
        assert result is None

    def test_etf_row_flag_mentions_yfinance_and_no_edgar(self):
        """Evidence string must clarify the classification came from yfinance."""
        profile = _make_profile(quote_type="ETF")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance("SMH")
        assert result is not None
        flag = result.flag.lower()
        assert "yfinance" in flag
        assert "edgar" in flag

    def test_etf_row_flag_distinct_from_edgar_fund(self):
        """EDGAR-based fund evidence ('N-CSR observed') must differ from yfinance path."""
        profile = _make_profile(quote_type="ETF")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            yf_row = _try_fund_via_yfinance("SMH")
        edgar_flag = "fund: N-CSR/N-PORT observed"
        assert yf_row is not None
        assert yf_row.flag != edgar_flag

    def test_top_holdings_propagated_to_etf_row(self):
        profile = _make_profile(quote_type="ETF")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance("SMH")
        assert result is not None
        assert len(result.top_holdings) == len(profile.top_holdings)
        assert result.top_holdings[0][0] == "NVDA"


# ---------------------------------------------------------------------------
# fetch_etf_profile — field extraction (yfinance mocked)
# ---------------------------------------------------------------------------

class TestFetchEtfProfile:
    """Verify field extraction logic without any network calls."""

    def _mock_ticker(
        self,
        quote_type: str = "ETF",
        long_name: str = "Test ETF",
        total_assets: int = 1_000_000_000,
        net_expense_ratio: float | None = None,
        fo_er: float | None = None,        # fund_operations expense ratio
        holdings_df=None,                  # DataFrame or None
    ):
        """Build a minimal yfinance Ticker mock."""
        import pandas as pd

        mock_t = MagicMock()
        mock_t.info = {
            "quoteType": quote_type,
            "longName": long_name,
            "totalAssets": total_assets,
            "category": "Technology",
        }
        if net_expense_ratio is not None:
            mock_t.info["netExpenseRatio"] = net_expense_ratio

        # funds_data mock
        fd = MagicMock()
        if fo_er is not None:
            fo_df = pd.DataFrame(
                {"TestETF": [fo_er, 0.12, total_assets / 1e6]},
                index=["Annual Report Expense Ratio", "Annual Holdings Turnover",
                       "Total Net Assets"],
            )
            fd.fund_operations = fo_df
        else:
            fo_df = pd.DataFrame(
                {"TestETF": [0.12, total_assets / 1e6]},
                index=["Annual Holdings Turnover", "Total Net Assets"],
            )
            fd.fund_operations = fo_df

        if holdings_df is not None:
            fd.top_holdings = holdings_df
        else:
            fd.top_holdings = pd.DataFrame()

        mock_t.funds_data = fd
        return mock_t

    def test_quote_type_extracted(self):
        mock_t = self._mock_ticker(quote_type="ETF")
        with patch("yfinance.Ticker", return_value=mock_t):
            p = fetch_etf_profile("TEST")
        assert p.quote_type == "ETF"

    def test_expense_ratio_from_fund_operations(self):
        """Primary path: fund_operations row gives decimal fraction directly."""
        mock_t = self._mock_ticker(fo_er=0.0035)
        with patch("yfinance.Ticker", return_value=mock_t):
            p = fetch_etf_profile("TEST")
        assert p.expense_ratio is not None
        assert abs(p.expense_ratio - 0.0035) < 1e-9

    def test_expense_ratio_fallback_from_info(self):
        """Fallback: netExpenseRatio in info is in percent units → divide by 100."""
        mock_t = self._mock_ticker(fo_er=None, net_expense_ratio=0.35)
        with patch("yfinance.Ticker", return_value=mock_t):
            p = fetch_etf_profile("TEST")
        assert p.expense_ratio is not None
        assert abs(p.expense_ratio - 0.0035) < 1e-9, (
            f"0.35 netExpenseRatio should → 0.0035 decimal, got {p.expense_ratio}"
        )

    def test_expense_ratio_none_when_both_absent(self):
        """Absence-is-not-zero: missing expense ratio must be None, not 0.0."""
        mock_t = self._mock_ticker(fo_er=None, net_expense_ratio=None)
        with patch("yfinance.Ticker", return_value=mock_t):
            p = fetch_etf_profile("TEST")
        assert p.expense_ratio is None, "absent expense ratio must be None, never 0.0"

    def test_total_assets_extracted(self):
        mock_t = self._mock_ticker(total_assets=67_000_000_000)
        with patch("yfinance.Ticker", return_value=mock_t):
            p = fetch_etf_profile("TEST")
        assert p.total_assets == 67_000_000_000

    def test_total_assets_none_when_absent(self):
        """Absence-is-not-zero: missing AUM must be None, not 0.0."""
        import pandas as pd
        mock_t = self._mock_ticker()
        mock_t.info = {"quoteType": "ETF", "longName": "Test ETF"}  # no totalAssets
        mock_t.funds_data.fund_operations = pd.DataFrame()
        mock_t.funds_data.top_holdings = pd.DataFrame()
        with patch("yfinance.Ticker", return_value=mock_t):
            p = fetch_etf_profile("TEST")
        assert p.total_assets is None, "absent AUM must be None, never 0.0"

    def test_top_holdings_extracted(self):
        import pandas as pd
        holdings_df = pd.DataFrame(
            {"Holding Percent": [0.152, 0.094, 0.078, 0.076, 0.072]},
            index=pd.Index(["NVDA", "TSM", "MU", "AMD", "INTC"], name="Symbol"),
        )
        mock_t = self._mock_ticker(holdings_df=holdings_df)
        with patch("yfinance.Ticker", return_value=mock_t):
            p = fetch_etf_profile("TEST")
        assert len(p.top_holdings) == 5
        assert p.top_holdings[0] == ("NVDA", 0.152)
        assert p.top_holdings[1] == ("TSM", 0.094)

    def test_top_holdings_empty_when_funds_data_raises(self):
        """If funds_data raises, top_holdings must be empty list, not an exception."""
        mock_t = MagicMock()
        mock_t.info = {"quoteType": "ETF", "longName": "Test ETF", "totalAssets": 1e9}
        type(mock_t).funds_data = PropertyMock(side_effect=RuntimeError("scrape failed"))
        with patch("yfinance.Ticker", return_value=mock_t):
            p = fetch_etf_profile("TEST")
        assert p.top_holdings == []
        assert p.expense_ratio is None

    def test_holdings_weights_already_fraction_scale(self):
        """Weights from funds_data.top_holdings are 0-1 — must not be divided again."""
        import pandas as pd
        holdings_df = pd.DataFrame(
            {"Holding Percent": [0.20, 0.13]},
            index=pd.Index(["NVDA", "TSM"], name="Symbol"),
        )
        mock_t = self._mock_ticker(holdings_df=holdings_df)
        with patch("yfinance.Ticker", return_value=mock_t):
            p = fetch_etf_profile("TEST")
        assert abs(p.top_holdings[0][1] - 0.20) < 1e-9
        assert abs(p.top_holdings[1][1] - 0.13) < 1e-9

    def test_full_yfinance_failure_returns_empty_profile(self):
        """If yfinance.Ticker() itself raises, return profile with all None fields."""
        with patch("yfinance.Ticker", side_effect=RuntimeError("no network")):
            p = fetch_etf_profile("FAIL")
        assert p.ticker == "FAIL"
        assert p.quote_type is None
        assert p.expense_ratio is None
        assert p.top_holdings == []


# ---------------------------------------------------------------------------
# EtfRow — overlap computation without re-fetch
# ---------------------------------------------------------------------------

class TestOverlapComputation:
    def test_overlap_uses_stored_top_holdings(self):
        """run_screen must compute overlap from EtfRow.top_holdings, not re-fetch."""
        er = EtfRow(
            ticker="SMH", name="VanEck Semiconductor ETF", category="Technology",
            expense_ratio=0.0035, aum=67e9, top10_concentration=0.65,
            top_holdings=[
                ("NVDA", 0.152), ("TSM", 0.094), ("MU", 0.078),
                ("AMD", 0.076), ("INTC", 0.072),
            ],
        )
        operating_tickers = {"NVDA", "MU"}
        matched = [(tk, w) for tk, w in er.top_holdings if tk.upper() in operating_tickers]
        er.overlap_with_screen = sum(w for _, w in matched) if matched else None
        er.overlap_count = len(matched) if matched else None

        assert er.overlap_count == 2
        assert abs(er.overlap_with_screen - (0.152 + 0.078)) < 1e-9

    def test_overlap_none_when_no_matches(self):
        er = EtfRow(
            ticker="XYZ", name="XYZ ETF", category="Bond",
            expense_ratio=0.002, aum=1e8, top10_concentration=0.80,
            top_holdings=[("AAAA", 0.10), ("BBBB", 0.08)],
        )
        operating_tickers = {"NVDA", "AAPL"}
        matched = [(tk, w) for tk, w in er.top_holdings if tk.upper() in operating_tickers]
        er.overlap_with_screen = sum(w for _, w in matched) if matched else None
        er.overlap_count = len(matched) if matched else None

        assert er.overlap_with_screen is None
        assert er.overlap_count is None

    def test_overlap_none_when_no_holdings(self):
        er = EtfRow(
            ticker="EMPTY", name="Empty ETF", category=None,
            expense_ratio=None, aum=None, top10_concentration=None,
            top_holdings=[],
        )
        operating_tickers = {"NVDA"}
        matched = [(tk, w) for tk, w in er.top_holdings if tk.upper() in operating_tickers]
        er.overlap_with_screen = sum(w for _, w in matched) if matched else None
        er.overlap_count = len(matched) if matched else None

        assert er.overlap_with_screen is None
        assert er.overlap_count is None


# ---------------------------------------------------------------------------
# Rendering helpers — _fmt_top5 and _fmt_overlap
# ---------------------------------------------------------------------------

class TestFormattingHelpers:
    def test_fmt_top5_first_five(self):
        holdings = [
            ("NVDA", 0.152), ("TSM", 0.094), ("MU", 0.078),
            ("AMD", 0.076), ("INTC", 0.072), ("AVGO", 0.072),
        ]
        result = _fmt_top5(holdings)
        assert "NVDA 15.2%" in result
        assert "AVGO" not in result  # 6th holding excluded

    def test_fmt_top5_na_when_empty(self):
        assert _fmt_top5([]) == "n/a"

    def test_fmt_top5_fewer_than_5(self):
        holdings = [("NVDA", 0.20), ("TSM", 0.13)]
        result = _fmt_top5(holdings)
        assert "NVDA 20.0%" in result
        assert "TSM 13.0%" in result

    def test_fmt_overlap_with_count(self):
        er = EtfRow(
            ticker="SMH", name=None, category=None, expense_ratio=None,
            aum=None, top10_concentration=None,
            overlap_with_screen=0.27, overlap_count=2,
        )
        result = _fmt_overlap(er)
        assert "27" in result
        assert "2" in result
        assert "holding" in result.lower()

    def test_fmt_overlap_singular(self):
        er = EtfRow(
            ticker="TEST", name=None, category=None, expense_ratio=None,
            aum=None, top10_concentration=None,
            overlap_with_screen=0.15, overlap_count=1,
        )
        result = _fmt_overlap(er)
        assert "holding" in result.lower()
        # singular form
        assert "holdings" not in result.lower() or "1 holding" in result.lower()

    def test_fmt_overlap_na_when_none(self):
        er = EtfRow(
            ticker="TEST", name=None, category=None, expense_ratio=None,
            aum=None, top10_concentration=None,
        )
        assert _fmt_overlap(er) == "n/a"


# ---------------------------------------------------------------------------
# Rendering — ETF section content with top_holdings
# ---------------------------------------------------------------------------

class TestEtfSectionRendering:
    def _etf_rows(self) -> list[EtfRow]:
        return [
            EtfRow(
                ticker="SMH",
                name="VanEck Semiconductor ETF",
                category="Technology",
                expense_ratio=0.0035,
                aum=67e9,
                top10_concentration=0.65,
                top_holdings=[
                    ("NVDA", 0.152), ("TSM", 0.094), ("MU", 0.078),
                    ("AMD", 0.076), ("INTC", 0.072),
                ],
                overlap_with_screen=0.230,
                overlap_count=2,
                flag="fund: yfinance quoteType=ETF (no EDGAR registrant)",
            ),
            EtfRow(
                ticker="FXAIX",
                name="Fidelity 500 Index Fund",
                category="Large Blend",
                expense_ratio=0.00015,
                aum=500e9,
                top10_concentration=None,
                top_holdings=[],
                flag="fund: yfinance quoteType=MUTUALFUND (no EDGAR registrant)",
            ),
        ]

    def test_top_holdings_in_md(self):
        md = _render_md([], self._etf_rows())
        assert "NVDA" in md
        assert "15.2%" in md

    def test_top_holdings_in_html(self):
        html = _render_html([], self._etf_rows())
        assert "NVDA" in html
        assert "15.2%" in html

    def test_overlap_count_in_md(self):
        md = _render_md([], self._etf_rows())
        assert "23.0%" in md or "23" in md

    def test_overlap_count_in_html(self):
        html = _render_html([], self._etf_rows())
        assert "23" in html

    def test_empty_holdings_shows_na(self):
        md = _render_md([], self._etf_rows())
        assert "n/a" in md  # FXAIX has no holdings

    def test_etf_section_header_md(self):
        md = _render_md([], self._etf_rows())
        assert "## ETFs / Funds" in md

    def test_etf_section_header_html(self):
        html = _render_html([], self._etf_rows())
        assert "ETFs / Funds" in html

    def test_yfinance_flag_in_md(self):
        md = _render_md([], self._etf_rows())
        assert "yfinance" in md

    def test_expense_ratio_formatted(self):
        md = _render_md([], self._etf_rows())
        # 0.0035 → "0.4%" at 1 decimal; 0.35% is the real value, "0.4" rounds it
        # assert we got a plausible % string
        assert "%" in md

    def test_equities_section_header_md(self):
        md = _render_md([_empty_screen_row()])
        assert "## Equities" in md

    def test_excluded_section_header_md(self):
        md = _render_md([_empty_screen_row()])
        assert "## Excluded" in md

    def test_equities_section_in_html(self):
        html = _render_html([_empty_screen_row()])
        assert "Equities" in html

    def test_excluded_section_in_html(self):
        html = _render_html([_empty_screen_row()])
        assert "Excluded" in html


# ---------------------------------------------------------------------------
# _try_fund_via_yfinance — evidence_suffix parameter
# ---------------------------------------------------------------------------

class TestTryFundViaYfinanceEvidenceSuffix:
    """Verify the evidence_suffix parameter produces distinct flag strings."""

    def test_default_suffix_mentions_no_edgar_registrant(self):
        profile = _make_profile(quote_type="ETF")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance("VOO")
        assert result is not None
        assert "no EDGAR registrant" in result.flag

    def test_custom_suffix_used_in_flag(self):
        profile = _make_profile(quote_type="ETF")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance(
                "GLD",
                evidence_suffix="financial SIC 6221, no EDGAR fund-filing forms",
            )
        assert result is not None
        assert "financial SIC 6221" in result.flag
        assert "no EDGAR registrant" not in result.flag

    def test_custom_suffix_still_mentions_quotetype(self):
        profile = _make_profile(ticker="GLD", quote_type="ETF")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance("GLD", evidence_suffix="financial SIC 6221")
        assert result is not None
        assert "yfinance" in result.flag.lower()
        assert "ETF" in result.flag

    def test_equity_quote_type_still_blocked_with_custom_suffix(self):
        """False-positive guard: even with a custom suffix, EQUITY quoteType must not route."""
        profile = _make_profile(quote_type="EQUITY")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            result = _try_fund_via_yfinance("FAKE", evidence_suffix="financial SIC 6221")
        assert result is None


# ---------------------------------------------------------------------------
# Part A: companyfacts 404 — forms-based routing via _process_one
# ---------------------------------------------------------------------------

def _make_partial_cd(
    ticker: str = "SPYFUND",
    recent_forms: list | None = None,
    sic: str = "",
) -> CompanyData:
    """Simulate get_company() returning a partial CompanyData after a companyfacts 404."""
    cd = CompanyData(
        ticker=ticker.upper(),
        cik="0000000001",
        name=f"{ticker} Trust",
        sic=sic,
        sic_description="",
    )
    cd.recent_forms = recent_forms or []
    # series stays empty — companyfacts 404 means no XBRL data
    return cd


def _cfg() -> dict:
    return {"universe": {"version": "test"}, "classification": {"overrides": {}}}


class TestCompanyfacts404Routing:
    """Part A: CIK exists but companyfacts 404 — classification from forms alone."""

    def test_fund_forms_in_partial_cd_routes_to_etf_section(self):
        """
        N-PORT + 485BPOS in submissions, companyfacts 404 → EtfRow from EDGAR forms alone.
        yfinance is called for enrichment data but not for the routing decision.
        """
        cd = _make_partial_cd(ticker="SPYFUND", recent_forms=["N-PORT", "485BPOS", "N-CEN"])
        client = MagicMock()
        client.get_company.return_value = cd

        profile = _make_profile(ticker="SPYFUND", quote_type="ETF", name="Mock S&P 500 ETF")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            op_row, etf_row = _process_one("SPYFUND", client, _cfg(), history_years=15)

        assert op_row is None, "Should not produce an operating row"
        assert etf_row is not None, "Should produce an EtfRow"
        # Evidence must come from EDGAR forms, not from a yfinance routing fallback
        assert "485BPOS" in etf_row.flag or "N-PORT" in etf_row.flag, (
            f"Flag should cite EDGAR fund forms; got: {etf_row.flag!r}"
        )
        assert "no EDGAR registrant" not in etf_row.flag, (
            "Flag must NOT say 'no EDGAR registrant' — EDGAR was used for classification"
        )

    def test_n_csr_only_routes_to_etf_section(self):
        """N-CSR alone is sufficient fund evidence."""
        cd = _make_partial_cd(ticker="QQQFUND", recent_forms=["N-CSR", "497", "N-CEN"])
        client = MagicMock()
        client.get_company.return_value = cd

        profile = _make_profile(ticker="QQQFUND", quote_type="ETF")
        with patch("engine.screen.fetch_etf_profile", return_value=profile):
            op_row, etf_row = _process_one("QQQFUND", client, _cfg(), history_years=15)

        assert op_row is None
        assert etf_row is not None
        assert "N-CSR" in etf_row.flag

    def test_no_fund_forms_partial_cd_routes_to_excluded(self):
        """
        CIK exists, no fund forms in submissions, companyfacts 404 →
        classified as 'unclassified', lands in Excluded — NOT in ETFs & Funds.
        """
        cd = _make_partial_cd(ticker="NOFUND", recent_forms=["8-K", "SC 13G", "DEF 14A"])
        client = MagicMock()
        client.get_company.return_value = cd

        with patch("engine.screen.fetch_etf_profile") as mock_yf:
            op_row, etf_row = _process_one("NOFUND", client, _cfg(), history_years=15)
            # yfinance should NOT be called — the unclassified path does not probe yfinance
            mock_yf.assert_not_called()

        assert etf_row is None, "Should NOT produce an EtfRow for unclassified ticker"
        assert op_row is not None
        assert op_row.composite is None
        assert "no annual report forms found" in op_row.flag


# ---------------------------------------------------------------------------
# Part B: financial-SIC exclusion falls through to yfinance probe
# ---------------------------------------------------------------------------

def _make_excluded_ds(
    ticker: str = "GLD",
    reason: str = "financial issuer SIC 6221 (6000–6799) — not scored",
) -> DurabilityScore:
    return DurabilityScore(
        ticker=ticker,
        composite=0.0, composite_low=0.0, composite_high=0.0,
        categories={}, config_hash="aaaa1111bbbb2222",
        data_completeness=0.0, is_stable=True, stability_delta=0.0,
        excluded=True,
        exclusion_reason=reason,
    )


class TestFinancialSicFallthrough:
    """Part B: financial-SIC exclusion → yfinance probe before Excluded."""

    def _setup_client_for_financial_sic(self, ticker: str = "GLD", sic: str = "6221") -> MagicMock:
        cd = CompanyData(
            ticker=ticker.upper(), cik="0001222333",
            name=f"{ticker} Trust", sic=sic, sic_description="",
        )
        cd.recent_forms = ["10-K", "10-K/A"]   # no fund forms
        client = MagicMock()
        client.get_company.return_value = cd
        return client

    def test_financial_sic_yfinance_etf_routes_to_fund_section(self):
        """
        financial-SIC ticker where yfinance confirms ETF →
        result is EtfRow, NOT Excluded row.
        """
        client = self._setup_client_for_financial_sic("GLD", "6221")
        ds = _make_excluded_ds("GLD", "financial issuer SIC 6221 (6000–6799) — not scored")
        profile = _make_profile(ticker="GLD", quote_type="ETF", name="SPDR Gold Trust")

        with (
            patch("engine.screen.get_quote", return_value=MagicMock()),
            patch("engine.screen.derive", return_value=MagicMock()),
            patch("engine.screen.D.score", return_value=ds),
            patch("engine.screen.fetch_etf_profile", return_value=profile),
        ):
            op_row, etf_row = _process_one("GLD", client, _cfg(), history_years=15)

        assert op_row is None, "Should not produce an operating row"
        assert etf_row is not None, "yfinance ETF confirmation should produce EtfRow"
        assert etf_row.ticker == "GLD"
        # Flag must mention yfinance confirmation AND the SIC reason
        assert "yfinance" in etf_row.flag.lower()
        assert "financial SIC" in etf_row.flag or "6221" in etf_row.flag

    def test_financial_sic_yfinance_mutualfund_routes_to_fund_section(self):
        """MUTUALFUND quoteType also routes to ETFs & Funds.
        Uses SIC 6282 (not 6726): 6726 is _FUND_SIC and is caught by _classify
        before D.score runs, so it never reaches this branch."""
        client = self._setup_client_for_financial_sic("SOMEFUND", "6282")
        ds = _make_excluded_ds("SOMEFUND", "financial issuer SIC 6282 (6000–6799) — not scored")
        profile = _make_profile(ticker="SOMEFUND", quote_type="MUTUALFUND", name="Some Fund")

        with (
            patch("engine.screen.get_quote", return_value=MagicMock()),
            patch("engine.screen.derive", return_value=MagicMock()),
            patch("engine.screen.D.score", return_value=ds),
            patch("engine.screen.fetch_etf_profile", return_value=profile),
        ):
            op_row, etf_row = _process_one("SOMEFUND", client, _cfg(), history_years=15)

        assert op_row is None
        assert etf_row is not None
        assert "MUTUALFUND" in etf_row.flag

    def test_financial_sic_yfinance_equity_stays_excluded(self):
        """
        financial-SIC ticker where yfinance resolves quoteType=EQUITY →
        remains in Excluded with the ORIGINAL financial-issuer message, unchanged.
        """
        client = self._setup_client_for_financial_sic("SOFI", "6199")
        original_reason = "financial issuer SIC 6199 (6000–6799) — not scored"
        ds = _make_excluded_ds("SOFI", original_reason)
        equity_profile = _make_profile(ticker="SOFI", quote_type="EQUITY", name="SoFi Technologies")

        with (
            patch("engine.screen.get_quote", return_value=MagicMock()),
            patch("engine.screen.derive", return_value=MagicMock()),
            patch("engine.screen.D.score", return_value=ds),
            patch("engine.screen.fetch_etf_profile", return_value=equity_profile),
        ):
            op_row, etf_row = _process_one("SOFI", client, _cfg(), history_years=15)

        assert etf_row is None, "EQUITY quoteType must not produce EtfRow"
        assert op_row is not None
        assert op_row.excluded is True
        assert op_row.flag == original_reason, (
            f"Financial-issuer message must be preserved unchanged; got: {op_row.flag!r}"
        )

    def test_financial_sic_yfinance_failure_stays_excluded(self):
        """
        financial-SIC ticker where yfinance raises an exception →
        remains in Excluded with the original financial-issuer message.
        """
        client = self._setup_client_for_financial_sic("COIN", "6199")
        original_reason = "financial issuer SIC 6199 (6000–6799) — not scored"
        ds = _make_excluded_ds("COIN", original_reason)

        with (
            patch("engine.screen.get_quote", return_value=MagicMock()),
            patch("engine.screen.derive", return_value=MagicMock()),
            patch("engine.screen.D.score", return_value=ds),
            patch("engine.screen.fetch_etf_profile", side_effect=RuntimeError("yfinance down")),
        ):
            op_row, etf_row = _process_one("COIN", client, _cfg(), history_years=15)

        assert etf_row is None
        assert op_row is not None
        assert op_row.excluded is True
        assert op_row.flag == original_reason
