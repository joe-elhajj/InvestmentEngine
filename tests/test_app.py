"""
test_app.py — tests for the FastAPI web layer in app/main.py.

Offline: every EDGAR/market/yfinance-touching call is mocked. TestClient is
used as a context manager so the lifespan (which builds app.state.cfg /
app.state.client / job stores) actually runs, exactly as it would under
uvicorn — but nothing here makes a real network call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.watchlist as watchlist_mod
from app.main import app as fastapi_app, _get_analysis_result
from engine.edgar import CompanyData, Fact
from engine.etf import EtfProfile
from engine.filings import FilingSections
from engine.market import Quote
from engine.pipeline import AnalysisResult
from engine.screen import EtfRow, ScreenRow


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolated watchlist DB per test; runs the real lifespan (no network calls in it)."""
    monkeypatch.setattr(watchlist_mod, "DB_PATH", tmp_path / "watchlist.db")
    with TestClient(fastapi_app) as c:
        yield c


def _screen_row(ticker: str, composite=None, **overrides) -> ScreenRow:
    base = dict(
        ticker=ticker, composite=composite, composite_low=None, composite_high=None,
        cat_reinvestment=None, cat_quality=None, cat_resilience=None,
        cat_discipline=None, cat_optionality=None,
        completeness=None, is_stable=None, stability_delta=None,
        config_hash="abc123", universe_version="2026-Q3",
        implied_fcf_growth=None, delivered_fcf_growth=None,
        expectations_gap=None, implied_growth_note="",
        quality_value_score=None, flag="",
    )
    base.update(overrides)
    return ScreenRow(**base)


# ---------------------------------------------------------------------------
# GET /api/watchlist
# ---------------------------------------------------------------------------

class TestWatchlistEndpoints:
    """
    add_to_watchlist() now resolves classification server-side (Task 2) —
    every test here mocks app.main._resolve_classification so these stay
    offline; tests that care about the resolved kind override the default.
    """

    @pytest.fixture(autouse=True)
    def _mock_classification(self):
        with patch(
            "app.main._resolve_classification",
            return_value={"kind": "equity", "label": "Equity — 10-K filer"},
        ):
            yield

    def test_get_watchlist_empty(self, client):
        resp = client.get("/api/watchlist")
        assert resp.status_code == 200
        assert resp.json() == {"tickers": [], "etfs": []}

    def test_get_watchlist_shape_after_add(self, client):
        def resolve(ticker):
            if ticker == "VOO":
                return {"kind": "etf", "label": "ETF/Fund — fund forms observed"}
            return {"kind": "equity", "label": "Equity — 10-K filer"}

        with patch("app.main._resolve_classification", side_effect=resolve):
            client.post("/api/watchlist/add", json={"ticker": "NVDA"})
            client.post("/api/watchlist/add", json={"ticker": "VOO"})
        resp = client.get("/api/watchlist")
        body = resp.json()
        assert body["tickers"] == ["NVDA"]
        assert body["etfs"] == ["VOO"]

    def test_add_valid_ticker_strips_and_uppercases(self, client):
        resp = client.post("/api/watchlist/add", json={"ticker": "  nvda  "})
        assert resp.status_code == 200
        assert resp.json()["tickers"] == ["NVDA"]

    def test_add_ticker_with_dot_accepted(self, client):
        """'.' must be allowed — real tickers like BRK.B / TSMC34.SA need it."""
        resp = client.post("/api/watchlist/add", json={"ticker": "TSMC34.SA"})
        assert resp.status_code == 200
        assert "TSMC34.SA" in resp.json()["tickers"]

    def test_add_empty_ticker_rejected(self, client):
        resp = client.post("/api/watchlist/add", json={"ticker": "   "})
        assert resp.status_code == 400

    def test_add_too_long_ticker_rejected(self, client):
        resp = client.post("/api/watchlist/add", json={"ticker": "ABCDEFGHIJK"})
        assert resp.status_code == 400

    def test_add_non_alphanumeric_ticker_rejected(self, client):
        resp = client.post("/api/watchlist/add", json={"ticker": "NVDA; DROP"})
        assert resp.status_code == 400

    def test_add_rejects_junk_without_touching_store(self, client):
        client.post("/api/watchlist/add", json={"ticker": "NVDA!!"})
        resp = client.get("/api/watchlist")
        assert resp.json() == {"tickers": [], "etfs": []}

    def test_add_routes_to_etf_table_when_classified_as_fund(self, client):
        with patch(
            "app.main._resolve_classification",
            return_value={"kind": "etf", "label": "ETF/Fund — fund forms observed"},
        ):
            resp = client.post("/api/watchlist/add", json={"ticker": "VOO"})
        body = resp.json()
        assert body["etfs"] == ["VOO"]
        assert body["tickers"] == []
        assert body["resolved_kind"] == "etf"

    def test_add_falls_back_to_equities_bucket_when_pending(self, client):
        """Genuinely unresolvable classification still needs somewhere to
        live (the schema has only two buckets) — but the response reports
        kind=None honestly rather than claiming a resolved answer."""
        with patch(
            "app.main._resolve_classification",
            return_value={"kind": None, "label": "pending"},
        ):
            resp = client.post("/api/watchlist/add", json={"ticker": "NEWCO"})
        body = resp.json()
        assert body["tickers"] == ["NEWCO"]
        assert body["resolved_kind"] is None
        assert body["resolved_label"] == "pending"

    def test_delete_removes_ticker(self, client):
        client.post("/api/watchlist/add", json={"ticker": "NVDA"})
        resp = client.delete("/api/watchlist/NVDA")
        assert resp.status_code == 200
        assert resp.json()["tickers"] == []

    def test_delete_is_case_insensitive(self, client):
        client.post("/api/watchlist/add", json={"ticker": "NVDA"})
        resp = client.delete("/api/watchlist/nvda")
        assert resp.json()["tickers"] == []


# ---------------------------------------------------------------------------
# GET /api/classify/{ticker} — evidence-based classification (Task 2)
# ---------------------------------------------------------------------------

def _cd(recent_forms, sic="7372", reporting_currency="USD") -> CompanyData:
    return CompanyData(
        ticker="TEST", cik="0000000001", name="Test Co",
        sic=sic, sic_description="Software",
        recent_forms=recent_forms, reporting_currency=reporting_currency,
    )


class TestClassifyEndpoint:
    def test_domestic_10k_filer_is_equity(self, client):
        with patch.object(fastapi_app.state.client, "get_company", return_value=_cd(["10-K"])):
            resp = client.get("/api/classify/AAPL")
        body = resp.json()
        assert body["kind"] == "equity"
        assert "10-K" in body["label"]

    def test_fpi_20f_filer_is_equity(self, client):
        with patch.object(
            fastapi_app.state.client, "get_company",
            return_value=_cd(["20-F"], reporting_currency="TWD"),
        ):
            resp = client.get("/api/classify/TSM")
        body = resp.json()
        assert body["kind"] == "equity"
        assert "foreign private issuer" in body["label"].lower()

    def test_fund_forms_is_etf(self, client):
        with patch.object(
            fastapi_app.state.client, "get_company",
            return_value=_cd(["N-CSR", "N-PORT"], sic="6726"),
        ):
            resp = client.get("/api/classify/SPY")
        body = resp.json()
        assert body["kind"] == "etf"

    def test_no_edgar_registrant_falls_back_to_yfinance_etf(self, client):
        fake_profile = MagicMock(quote_type="ETF")
        with (
            patch.object(
                fastapi_app.state.client, "get_company",
                side_effect=ValueError("Ticker 'VOO' not found in SEC ticker map."),
            ),
            patch("app.main.fetch_etf_profile", return_value=fake_profile),
        ):
            resp = client.get("/api/classify/VOO")
        body = resp.json()
        assert body["kind"] == "etf"

    def test_no_edgar_registrant_and_not_a_fund_is_pending(self, client):
        fake_profile = MagicMock(quote_type="EQUITY")
        with (
            patch.object(
                fastapi_app.state.client, "get_company",
                side_effect=ValueError("Ticker 'FAKE' not found in SEC ticker map."),
            ),
            patch("app.main.fetch_etf_profile", return_value=fake_profile),
        ):
            resp = client.get("/api/classify/FAKE")
        body = resp.json()
        assert body["kind"] is None
        assert body["label"] == "pending"

    def test_financial_sic_confirmed_fund_via_yfinance_is_etf(self, client):
        """Commodity trusts like GLD: financial SIC, no fund forms, but
        yfinance confirms ETF — same fallback screen.py uses."""
        fake_profile = MagicMock(quote_type="ETF")
        with (
            patch.object(
                fastapi_app.state.client, "get_company",
                return_value=_cd(["10-K"], sic="6221"),
            ),
            patch("app.main.fetch_etf_profile", return_value=fake_profile),
        ):
            resp = client.get("/api/classify/GLD")
        body = resp.json()
        assert body["kind"] == "etf"

    def test_financial_sic_no_fund_confirmation_stays_equity(self, client):
        """A real bank (JPM) genuinely files a 10-K and yfinance reports
        quoteType=EQUITY, not a fund — it correctly classifies as equity
        for watchlist bucketing. durability.py separately excludes it from
        SCORING for financial-SIC reasons, but that's a distinct concern
        (shown later in the screen's Excluded section) from "is this a
        stock or a fund," which is all this endpoint answers."""
        fake_profile = MagicMock(quote_type="EQUITY")
        with (
            patch.object(
                fastapi_app.state.client, "get_company",
                return_value=_cd(["10-K"], sic="6021"),
            ),
            patch("app.main.fetch_etf_profile", return_value=fake_profile),
        ):
            resp = client.get("/api/classify/JPM")
        body = resp.json()
        assert body["kind"] == "equity"
        assert "10-K" in body["label"]

    def test_unclassified_is_pending(self, client):
        with patch.object(
            fastapi_app.state.client, "get_company",
            return_value=_cd(["8-K", "DEF 14A"]),
        ):
            resp = client.get("/api/classify/WEIRD")
        body = resp.json()
        assert body["kind"] is None
        assert body["label"] == "pending"

    def test_analyst_override_bypasses_financial_sic_exclusion(self, client):
        """MARA-style override: config.yaml says 'operating' despite a
        financial SIC — classify endpoint must honor it, same as screen.py."""
        fastapi_app.state.cfg.setdefault("classification", {}).setdefault("overrides", {})["MARA"] = "operating"
        try:
            with patch.object(
                fastapi_app.state.client, "get_company",
                return_value=_cd(["10-K"], sic="6199"),
            ):
                resp = client.get("/api/classify/MARA")
        finally:
            del fastapi_app.state.cfg["classification"]["overrides"]["MARA"]
        body = resp.json()
        assert body["kind"] == "equity"

    def test_edgar_network_failure_is_pending_not_500(self, client):
        with patch.object(
            fastapi_app.state.client, "get_company", side_effect=RuntimeError("network down"),
        ):
            resp = client.get("/api/classify/AAPL")
        assert resp.status_code == 200
        assert resp.json() == {"kind": None, "label": "pending"}


# ---------------------------------------------------------------------------
# GET /api/search/{ticker}
# ---------------------------------------------------------------------------

class TestSearchEndpoint:
    def test_found_ticker_returns_name(self, client):
        with (
            patch.object(fastapi_app.state.client, "ticker_to_cik", return_value="0000320193"),
            patch("app.main._lookup_ticker_name", return_value="Apple Inc."),
        ):
            resp = client.get("/api/search/AAPL")
        assert resp.status_code == 200
        assert resp.json() == {"found": True, "name": "Apple Inc."}

    def test_not_found_ticker(self, client):
        with patch.object(
            fastapi_app.state.client, "ticker_to_cik",
            side_effect=ValueError("Ticker 'FAKE' not found in SEC ticker map."),
        ):
            resp = client.get("/api/search/FAKE")
        assert resp.status_code == 200
        assert resp.json() == {"found": False, "name": None}

    def test_ticker_map_network_failure_degrades_to_not_found(self, client):
        """Instant-feedback endpoint must never 500 just because SEC is unreachable."""
        with patch.object(
            fastapi_app.state.client, "ticker_to_cik",
            side_effect=RuntimeError("network down"),
        ):
            resp = client.get("/api/search/AAPL")
        assert resp.status_code == 200
        assert resp.json()["found"] is False

    def test_found_but_name_lookup_fails_still_reports_found(self, client):
        with (
            patch.object(fastapi_app.state.client, "ticker_to_cik", return_value="0000320193"),
            patch("app.main._lookup_ticker_name", side_effect=RuntimeError("scrape failed")),
        ):
            resp = client.get("/api/search/AAPL")
        assert resp.json() == {"found": True, "name": None}


# ---------------------------------------------------------------------------
# GET /api/analyze/{ticker}
# ---------------------------------------------------------------------------

class TestAnalyzeEndpoint:
    def test_success_returns_rendered_html(self, client):
        with (
            patch("app.main.run_single_ticker", return_value="fake-analysis-result") as mock_run,
            patch("app.main.RH.render", return_value="<html>report</html>") as mock_render,
        ):
            resp = client.get("/api/analyze/AAPL")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.text == "<html>report</html>"
        mock_run.assert_called_once_with(
            "AAPL", fastapi_app.state.cfg, fastapi_app.state.client
        )
        mock_render.assert_called_once_with("fake-analysis-result", peer_table=None)

    def test_failure_returns_clean_error_page_not_traceback(self, client):
        with patch(
            "app.main.run_single_ticker",
            side_effect=ValueError("Ticker 'FAKE' not found in SEC ticker map."),
        ):
            resp = client.get("/api/analyze/FAKE")
        assert resp.status_code == 502
        assert resp.headers["content-type"].startswith("text/html")
        assert "Traceback" not in resp.text
        assert "not found in SEC ticker map" in resp.text
        assert "FAKE" in resp.text

    def test_second_call_within_ttl_is_served_from_cache(self, client):
        with (
            patch("app.main.run_single_ticker", return_value="res") as mock_run,
            patch("app.main.RH.render", return_value="<html>cached</html>"),
        ):
            first = client.get("/api/analyze/AAPL")
            second = client.get("/api/analyze/AAPL")
        assert first.text == second.text == "<html>cached</html>"
        mock_run.assert_called_once()  # not called twice — second hit the cache

    def test_ticker_normalized_to_uppercase(self, client):
        with (
            patch("app.main.run_single_ticker", return_value="res") as mock_run,
            patch("app.main.RH.render", return_value="<html>x</html>"),
        ):
            client.get("/api/analyze/aapl")
        mock_run.assert_called_once_with("AAPL", fastapi_app.state.cfg, fastapi_app.state.client)


# ---------------------------------------------------------------------------
# GET /api/analyze/{ticker}/json
# ---------------------------------------------------------------------------

def _real_analysis_result() -> AnalysisResult:
    """A real (not mocked) AnalysisResult with several genuinely-None fields,
    so we can verify the JSON payload keeps them as null rather than
    coercing to 0 or ''."""
    cd = CompanyData(
        ticker="AAPL", cik="0000320193", name="Apple Inc.",
        sic="3674", sic_description="Semiconductors",
        recent_forms=["10-K"],
    )
    cd.series = {
        "revenue": [Fact("revenue", 100.0, "2026-09-30", 2026, "us-gaap:Revenues",
                         "10-K", "2026-11-01", "USD")],
    }
    quote = Quote("AAPL", price=None, shares_outstanding=None, market_cap=None, source="test")
    res = AnalysisResult(company=cd, quote=quote)
    res.gaps = ["dcf: base FCF unavailable or non-positive"]
    res.derived = {"revenue": 100.0, "net_income": None}
    res.derived_lineage = {"gross_profit": "derived: revenue - cost_of_revenue"}
    res.rel_val = None
    res.implied_growth_result = None
    res.expectations_gap = None
    return res


class TestAnalyzeJsonEndpoint:
    def test_returns_full_serialization_with_config_hash(self, client):
        with patch("app.main.run_single_ticker", return_value=_real_analysis_result()):
            resp = client.get("/api/analyze/AAPL/json")
        assert resp.status_code == 200
        body = resp.json()
        for key in (
            "company", "quote", "derived", "growth", "ratios", "rel_val", "dcf",
            "sensitivity", "gaps", "latest_quarter", "derived_lineage",
            "annual_series", "normalized_fcf", "delivered_growth",
            "implied_growth_result", "expectations_gap", "config_hash",
        ):
            assert key in body, f"{key!r} missing from JSON payload"

    def test_none_fields_serialize_as_null_never_zero_or_empty_string(self, client):
        with patch("app.main.run_single_ticker", return_value=_real_analysis_result()):
            resp = client.get("/api/analyze/AAPL/json")
        body = resp.json()
        assert body["rel_val"] is None
        assert body["implied_growth_result"] is None
        assert body["expectations_gap"] is None
        assert body["derived"]["net_income"] is None
        assert body["quote"]["price"] is None
        assert body["quote"]["shares_outstanding"] is None

    def test_present_fields_keep_real_values(self, client):
        with patch("app.main.run_single_ticker", return_value=_real_analysis_result()):
            resp = client.get("/api/analyze/AAPL/json")
        body = resp.json()
        assert body["derived"]["revenue"] == 100.0
        assert body["company"]["ticker"] == "AAPL"
        assert body["gaps"] == ["dcf: base FCF unavailable or non-positive"]
        assert isinstance(body["config_hash"], str) and len(body["config_hash"]) == 16

    def test_failure_returns_502_with_reason(self, client):
        with patch(
            "app.main.run_single_ticker",
            side_effect=ValueError("Ticker 'FAKE' not found in SEC ticker map."),
        ):
            resp = client.get("/api/analyze/FAKE/json")
        assert resp.status_code == 502
        assert "not found in SEC ticker map" in resp.json()["detail"]

    def test_json_and_full_page_share_the_same_cache(self, client):
        """Both endpoints go through _get_analysis_result — one fetch serves both."""
        with (
            patch("app.main.run_single_ticker", return_value=_real_analysis_result()) as mock_run,
            patch("app.main.RH.render", return_value="<html>x</html>"),
        ):
            client.get("/api/analyze/AAPL")
            client.get("/api/analyze/AAPL/json")
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# GET /api/analyze/{ticker}/fragment
# ---------------------------------------------------------------------------

class TestAnalyzeFragmentEndpoint:
    """
    analyze_fragment() now routes by classification (Task 2) — every test
    here mocks app.main._resolve_classification as "equity" by default so
    the existing AnalysisResult-based path is exercised offline; the ETF
    routing tests below override it.
    """

    @pytest.fixture(autouse=True)
    def _mock_classification(self):
        with patch(
            "app.main._resolve_classification",
            return_value={"kind": "equity", "label": "Equity — 10-K filer"},
        ):
            yield

    def test_success_returns_fragment_with_no_html_wrapper(self, client):
        with patch("app.main.run_single_ticker", return_value=_real_analysis_result()):
            resp = client.get("/api/analyze/AAPL/fragment")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "<html" not in resp.text
        assert "<head" not in resp.text
        assert resp.text.strip().startswith('<div class="report-fragment">')

    def test_composite_shown_when_durability_scores_it(self, client):
        fake_score = MagicMock(composite=71.4, excluded=False)
        with (
            patch("app.main.run_single_ticker", return_value=_real_analysis_result()),
            patch("app.main.D.score", return_value=fake_score) as mock_score,
        ):
            resp = client.get("/api/analyze/AAPL/fragment")
        assert "71.4" in resp.text
        mock_score.assert_called_once()

    def test_composite_na_when_durability_excludes_it(self, client):
        """Sparse fixture data (no financial-SIC exclusion, just insufficient
        series) — durability.score() genuinely can't score it, so the
        fragment must show n/a, never a fabricated 0."""
        with patch("app.main.run_single_ticker", return_value=_real_analysis_result()):
            resp = client.get("/api/analyze/AAPL/fragment")
        assert resp.status_code == 200
        assert '<span class="stat-value">n/a</span>' in resp.text

    def test_failure_returns_inline_error_fragment_not_full_page(self, client):
        with patch(
            "app.main.run_single_ticker",
            side_effect=ValueError("Ticker 'FAKE' not found in SEC ticker map."),
        ):
            resp = client.get("/api/analyze/FAKE/fragment")
        assert resp.status_code == 502
        assert "<html" not in resp.text
        assert "Traceback" not in resp.text
        assert "not found in SEC ticker map" in resp.text
        assert 'class="report-fragment report-error"' in resp.text

    def test_shares_cache_with_full_page_and_json(self, client):
        with (
            patch("app.main.run_single_ticker", return_value=_real_analysis_result()) as mock_run,
            patch("app.main.RH.render", return_value="<html>x</html>"),
        ):
            client.get("/api/analyze/AAPL")
            client.get("/api/analyze/AAPL/json")
            client.get("/api/analyze/AAPL/fragment")
        mock_run.assert_called_once()


class TestAnalyzeFragmentEtfRouting:
    """Task 2: a ticker classified as a fund gets the ETF-specific sections
    (Profile, Overlap detail) built from engine.etf's profile — never the
    equity sections, which would be wall-to-wall n/a for a fund."""

    def _profile(self, **overrides):
        profile = EtfProfile(
            ticker="VOO", quote_type="ETF", name="Vanguard S&P 500 ETF",
            category="Large Blend", expense_ratio=0.0003, total_assets=500e9,
            top10_concentration=0.35,
            top_holdings=[("AAPL", 0.07), ("MSFT", 0.06), ("NVDA", 0.05)],
        )
        for k, v in overrides.items():
            setattr(profile, k, v)
        return profile

    def _patches(self, profile=None, quote=None, watchlist_tickers=None):
        stack = contextlib.ExitStack()
        stack.enter_context(patch(
            "app.main._resolve_classification",
            return_value={"kind": "etf", "label": "ETF/Fund — fund forms observed"},
        ))
        stack.enter_context(patch("app.main.fetch_etf_profile", return_value=profile or self._profile()))
        stack.enter_context(patch(
            "app.main.get_quote",
            return_value=quote or Quote("VOO", price=520.0, shares_outstanding=None,
                                         market_cap=None, source="test"),
        ))
        stack.enter_context(patch("app.main.watchlist.load", return_value={
            "tickers": watchlist_tickers if watchlist_tickers is not None else ["AAPL"],
            "etfs": [],
        }))
        return stack

    def test_fund_routes_to_etf_sections(self, client):
        with self._patches():
            resp = client.get("/api/analyze/VOO/fragment")
        assert resp.status_code == 200
        assert "Profile" in resp.text
        assert "Overlap detail" in resp.text

    def test_fund_never_renders_equity_sections(self, client):
        with self._patches():
            resp = client.get("/api/analyze/VOO/fragment")
        for heading in ("Financial position", "Latest quarter", "Growth", "Margins &amp; returns", "Valuation &amp; sensitivity"):
            assert heading not in resp.text

    def test_fund_never_calls_run_single_ticker(self, client):
        with self._patches():
            with patch("app.main.run_single_ticker") as mock_run:
                client.get("/api/analyze/VOO/fragment")
            mock_run.assert_not_called()

    def test_fund_summary_strip_is_price_and_aum_only(self, client):
        with self._patches():
            resp = client.get("/api/analyze/VOO/fragment")
        assert "Price" in resp.text
        assert "AUM" in resp.text
        assert "Durability" not in resp.text
        assert "Expectations Gap" not in resp.text
        assert "DCF" not in resp.text

    def test_fund_profile_shows_vendor_tier_source(self, client):
        with self._patches():
            resp = client.get("/api/analyze/VOO/fragment")
        assert "market-vendor tier" in resp.text
        assert "Vanguard S&P 500 ETF" in resp.text

    def test_fund_overlap_detail_shows_matched_watchlist_tickers(self, client):
        with self._patches(watchlist_tickers=["AAPL", "MSFT"]):
            resp = client.get("/api/analyze/VOO/fragment")
        assert "AAPL" in resp.text
        assert "MSFT" in resp.text
        assert "7.0%" in resp.text  # AAPL's weight
        assert "2 watchlist" in resp.text

    def test_fund_no_overlap_shows_clean_message(self, client):
        with self._patches(watchlist_tickers=["ZZZZ"]):
            resp = client.get("/api/analyze/VOO/fragment")
        assert "No overlap" in resp.text

    def test_fund_classification_evidence_shown(self, client):
        with self._patches():
            resp = client.get("/api/analyze/VOO/fragment")
        assert "fund forms observed" in resp.text


# ---------------------------------------------------------------------------
# Shared analysis cache: TTL expiry + per-ticker lock coalescing
# ---------------------------------------------------------------------------

class TestAnalysisCacheTtlAndLocking:
    def test_cache_expires_after_ttl(self, client):
        fastapi_app.state.cfg.setdefault("web", {})["analysis_cache_ttl_seconds"] = 100
        fake_now = [1_000_000.0]
        with (
            patch("app.main.time.time", side_effect=lambda: fake_now[0]),
            patch("app.main.run_single_ticker", return_value="res") as mock_run,
            patch("app.main.RH.render", return_value="<html>x</html>"),
        ):
            client.get("/api/analyze/AAPL")
            assert mock_run.call_count == 1

            fake_now[0] += 50  # well within the 100s TTL
            client.get("/api/analyze/AAPL")
            assert mock_run.call_count == 1, "still within TTL — must not re-fetch"

            fake_now[0] += 60  # 110s elapsed total — past the 100s TTL
            client.get("/api/analyze/AAPL")
            assert mock_run.call_count == 2, "past TTL — must re-fetch"

    def test_concurrent_requests_for_uncached_ticker_fetch_only_once(self, client):
        """Two concurrent callers for the same never-cached ticker must coalesce
        into a single run_single_ticker call via the per-ticker asyncio.Lock —
        the second caller finds the cache warm instead of double-hitting EDGAR."""
        call_count = {"n": 0}

        def slow_fetch(ticker, cfg, edgar_client):
            call_count["n"] += 1
            import time as _time
            _time.sleep(0.05)
            return f"result-for-{ticker}"

        async def scenario():
            with patch("app.main.run_single_ticker", side_effect=slow_fetch):
                return await asyncio.gather(
                    _get_analysis_result("NVDA"),
                    _get_analysis_result("NVDA"),
                )

        results = asyncio.run(scenario())
        assert call_count["n"] == 1, "concurrent requests for the same ticker must coalesce"
        assert results == ["result-for-NVDA", "result-for-NVDA"]

    def test_different_tickers_do_not_block_each_other(self, client):
        """The lock is per-ticker — concurrent requests for DIFFERENT tickers
        must both proceed (not serialize behind one shared lock)."""
        call_count = {"n": 0}

        def slow_fetch(ticker, cfg, edgar_client):
            call_count["n"] += 1
            import time as _time
            _time.sleep(0.05)
            return f"result-for-{ticker}"

        async def scenario():
            with patch("app.main.run_single_ticker", side_effect=slow_fetch):
                return await asyncio.gather(
                    _get_analysis_result("AAPL"),
                    _get_analysis_result("MSFT"),
                )

        results = asyncio.run(scenario())
        assert call_count["n"] == 2
        assert set(results) == {"result-for-AAPL", "result-for-MSFT"}


# ---------------------------------------------------------------------------
# GET /api/screen + /api/screen/status/{job_id}
# ---------------------------------------------------------------------------

class TestScreenEndpoint:
    def test_triggers_run_screen_with_watchlist_tickers(self, client):
        with patch(
            "app.main._resolve_classification",
            return_value={"kind": "equity", "label": "Equity — 10-K filer"},
        ):
            client.post("/api/watchlist/add", json={"ticker": "NVDA"})
            client.post("/api/watchlist/add", json={"ticker": "VOO"})

        with patch("app.main.run_screen", return_value=([], [])) as mock_screen:
            start = client.get("/api/screen")
            assert start.status_code == 202
            job_id = start.json()["job_id"]
            status = client.get(f"/api/screen/status/{job_id}")

        assert status.json()["status"] == "done"
        called_tickers = mock_screen.call_args[0][0]
        assert set(called_tickers) == {"NVDA", "VOO"}

    def test_membership_split_and_none_fields_are_null_not_zero(self, client):
        scored = _screen_row(
            "MSFT", composite=82.3, composite_low=None, cat_reinvestment=None,
            expectations_gap=None,
        )
        excluded = _screen_row("JPM", composite=None, flag="financial issuer SIC 6021")
        etf = EtfRow(
            ticker="VOO", name="Vanguard S&P 500", category=None,
            expense_ratio=None, aum=None, top10_concentration=None,
        )

        with patch("app.main.run_screen", return_value=([scored, excluded], [etf])):
            start = client.get("/api/screen")
            job_id = start.json()["job_id"]
            status = client.get(f"/api/screen/status/{job_id}")

        result = status.json()["result"]
        assert [r["ticker"] for r in result["equities"]] == ["MSFT"]
        assert [r["ticker"] for r in result["excluded"]] == ["JPM"]
        assert [r["ticker"] for r in result["etfs"]] == ["VOO"]

        msft = result["equities"][0]
        # None must serialize as JSON null, never silently become 0.
        assert msft["composite_low"] is None
        assert msft["cat_reinvestment"] is None
        assert msft["expectations_gap"] is None
        assert msft["composite"] == 82.3  # the real, present value stays a real value

        voo = result["etfs"][0]
        assert voo["expense_ratio"] is None
        assert voo["aum"] is None

        assert result["config_hash"] == "abc123"
        assert result["universe"] == "2026-Q3"
        assert "generated_at" in result

    def test_unknown_job_id_returns_404(self, client):
        resp = client.get("/api/screen/status/does-not-exist")
        assert resp.status_code == 404

    def test_screen_job_error_is_captured_not_raised(self, client):
        with patch("app.main.run_screen", side_effect=RuntimeError("EDGAR is down")):
            start = client.get("/api/screen")
            job_id = start.json()["job_id"]
            status = client.get(f"/api/screen/status/{job_id}")
        assert status.json()["status"] == "error"
        assert "EDGAR is down" in status.json()["error"]

    def test_empty_watchlist_does_not_crash(self, client):
        with patch("app.main.run_screen", return_value=([], [])) as mock_screen:
            start = client.get("/api/screen")
            job_id = start.json()["job_id"]
            status = client.get(f"/api/screen/status/{job_id}")
        mock_screen.assert_called_once_with([], fastapi_app.state.cfg, out_dir=None, verbose=False)
        assert status.json()["status"] == "done"
        assert status.json()["result"]["equities"] == []


# ---------------------------------------------------------------------------
# GET /api/flags/{ticker} — Tier 2's LLM boundary
# ---------------------------------------------------------------------------

def _filing_sections(sections=None) -> FilingSections:
    return FilingSections(
        ticker="NVDA", cik="0001045810", accession="0000320193-24-000123",
        form="10-K", filed="2024-02-21", period_ending="2024-01-28",
        url="https://example.com/nvda10k.htm",
        sections=sections if sections is not None else {
            "1A": "Our competitor XYZ Corp filed a lawsuit against us in March 2024.",
        },
    )


def _model_response(payload: list) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(payload)
    response = MagicMock()
    response.content = [block]
    return response


class TestFlagsEndpoint:
    """
    Offline: every test here patches app.state.filings_client's fetch AND
    engine.flags._call_model — no real EDGAR or Anthropic call. A fresh
    tmp_path cache dir per test means no test depends on (or pollutes) a
    prior test's cached extraction.
    """

    @pytest.fixture(autouse=True)
    def _mock_anthropic_and_cache_dir(self, client, tmp_path):
        # Depends on `client` (not just tmp_path) so it runs AFTER the
        # TestClient context manager's lifespan — that lifespan sets
        # app.state.anthropic_client itself (None in a real environment
        # with no ANTHROPIC_API_KEY), and would silently undo this
        # override if this fixture ran first. A real environment with no
        # key would otherwise short-circuit on the endpoint's 503 branch
        # before ever reaching the code these tests are actually about.
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", tmp_path):
            yield

    def test_schema_full_pinned_fields_stamped(self, client):
        fs = _filing_sections()
        canned = _model_response([
            {"label": "Patent lawsuit", "severity": "red", "item": "1A",
             "snippet": "Our competitor XYZ Corp filed a lawsuit against us in March 2024."},
        ])
        with patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("engine.flags._call_model", return_value=canned.content[0].text):
            resp = client.get("/api/flags/NVDA")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "NVDA"
        assert body["model"]
        assert body["prompt_version"]
        assert body["extracted_at"]
        assert body["filing"] == {
            "form": "10-K", "accession": "0000320193-24-000123",
            "period_ending": "2024-01-28", "filed": "2024-02-21",
            "url": "https://example.com/nvda10k.htm",
        }
        assert len(body["flags"]) == 1
        assert body["flags"][0]["verified_verbatim"] is True
        assert body["flags"][0]["severity"] == "red"
        assert body["flags"][0]["item"] == "1A"
        assert body["dropped_count"] == 0

    def test_fabricated_snippet_dropped_via_the_real_endpoint(self, client):
        fs = _filing_sections()
        canned_text = _model_response([
            {"label": "Fabricated", "severity": "red", "item": "1A", "snippet": "not in the filing at all"},
        ]).content[0].text
        with patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("engine.flags._call_model", return_value=canned_text):
            resp = client.get("/api/flags/NVDA")
        body = resp.json()
        assert body["flags"] == []
        assert body["dropped_count"] == 1

    def test_no_filing_found_returns_404(self, client):
        with patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=None):
            resp = client.get("/api/flags/SPY")
        assert resp.status_code == 404

    def test_no_api_key_configured_returns_503(self, client):
        fastapi_app.state.anthropic_client = None
        fs = _filing_sections()
        with patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs):
            resp = client.get("/api/flags/NVDA")
        assert resp.status_code == 503

    def test_second_request_is_a_cache_hit_model_not_called_again(self, client):
        fs = _filing_sections()
        canned_text = _model_response([
            {"label": "Patent lawsuit", "severity": "red", "item": "1A",
             "snippet": "Our competitor XYZ Corp filed a lawsuit against us in March 2024."},
        ]).content[0].text
        with patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("engine.flags._call_model", return_value=canned_text) as mock_call:
            client.get("/api/flags/NVDA")
            client.get("/api/flags/NVDA")
            assert mock_call.call_count == 1

    def test_refresh_true_re_calls_model_and_re_stamps(self, client):
        fs = _filing_sections()
        canned_text = _model_response([]).content[0].text
        with patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("engine.flags._call_model", return_value=canned_text) as mock_call:
            r1 = client.get("/api/flags/NVDA").json()
            r2 = client.get("/api/flags/NVDA?refresh=true").json()
            assert mock_call.call_count == 2
        assert r1["extracted_at"] != r2["extracted_at"]

    def test_override_applied_through_the_endpoint(self, client):
        fs = _filing_sections()
        canned_text = _model_response([
            {"label": "Patent lawsuit", "severity": "red", "item": "1A",
             "snippet": "Our competitor XYZ Corp filed a lawsuit against us in March 2024."},
        ]).content[0].text
        overrides = {"NVDA": {"demote": ["Patent lawsuit"]}}
        original_overrides = fastapi_app.state.cfg.get("flags", {}).get("overrides", {})
        fastapi_app.state.cfg.setdefault("flags", {})["overrides"] = overrides
        try:
            with patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
                 patch("engine.flags._call_model", return_value=canned_text):
                resp = client.get("/api/flags/NVDA")
        finally:
            fastapi_app.state.cfg["flags"]["overrides"] = original_overrides
        assert resp.json()["flags"][0]["severity"] == "yellow"

    def test_analyst_sourced_add_is_tagged_in_response(self, client):
        fs = _filing_sections()
        canned_text = _model_response([]).content[0].text
        overrides = {"NVDA": {"add": [
            {"label": "Earnings call note", "severity": "yellow", "item": "1A",
             "snippet": "management lowered guidance on the Q3 call", "source": "analyst"},
        ]}}
        original_overrides = fastapi_app.state.cfg.get("flags", {}).get("overrides", {})
        fastapi_app.state.cfg.setdefault("flags", {})["overrides"] = overrides
        try:
            with patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
                 patch("engine.flags._call_model", return_value=canned_text):
                resp = client.get("/api/flags/NVDA")
        finally:
            fastapi_app.state.cfg["flags"]["overrides"] = original_overrides
        flags = resp.json()["flags"]
        assert len(flags) == 1
        assert flags[0]["source"] == "analyst"
