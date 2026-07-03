"""
test_app.py — tests for the FastAPI web layer in app/main.py.

Offline: every EDGAR/market/yfinance-touching call is mocked. TestClient is
used as a context manager so the lifespan (which builds app.state.cfg /
app.state.client / job stores) actually runs, exactly as it would under
uvicorn — but nothing here makes a real network call.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.watchlist as watchlist_mod
from app.main import app as fastapi_app
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
    def test_get_watchlist_empty(self, client):
        resp = client.get("/api/watchlist")
        assert resp.status_code == 200
        assert resp.json() == {"tickers": [], "etfs": []}

    def test_get_watchlist_shape_after_add(self, client):
        client.post("/api/watchlist/add", json={"ticker": "NVDA", "type": "equity"})
        client.post("/api/watchlist/add", json={"ticker": "VOO", "type": "etf"})
        resp = client.get("/api/watchlist")
        body = resp.json()
        assert body["tickers"] == ["NVDA"]
        assert body["etfs"] == ["VOO"]

    def test_add_valid_ticker_strips_and_uppercases(self, client):
        resp = client.post("/api/watchlist/add", json={"ticker": "  nvda  ", "type": "equity"})
        assert resp.status_code == 200
        assert resp.json()["tickers"] == ["NVDA"]

    def test_add_ticker_with_dot_accepted(self, client):
        """'.' must be allowed — real tickers like BRK.B / TSMC34.SA need it."""
        resp = client.post("/api/watchlist/add", json={"ticker": "TSMC34.SA", "type": "equity"})
        assert resp.status_code == 200
        assert "TSMC34.SA" in resp.json()["tickers"]

    def test_add_empty_ticker_rejected(self, client):
        resp = client.post("/api/watchlist/add", json={"ticker": "   ", "type": "equity"})
        assert resp.status_code == 400

    def test_add_too_long_ticker_rejected(self, client):
        resp = client.post("/api/watchlist/add", json={"ticker": "ABCDEFGHIJK", "type": "equity"})
        assert resp.status_code == 400

    def test_add_non_alphanumeric_ticker_rejected(self, client):
        resp = client.post("/api/watchlist/add", json={"ticker": "NVDA; DROP", "type": "equity"})
        assert resp.status_code == 400

    def test_add_rejects_junk_without_touching_store(self, client):
        client.post("/api/watchlist/add", json={"ticker": "NVDA!!", "type": "equity"})
        resp = client.get("/api/watchlist")
        assert resp.json() == {"tickers": [], "etfs": []}

    def test_delete_removes_ticker(self, client):
        client.post("/api/watchlist/add", json={"ticker": "NVDA", "type": "equity"})
        resp = client.delete("/api/watchlist/NVDA")
        assert resp.status_code == 200
        assert resp.json()["tickers"] == []

    def test_delete_is_case_insensitive(self, client):
        client.post("/api/watchlist/add", json={"ticker": "NVDA", "type": "equity"})
        resp = client.delete("/api/watchlist/nvda")
        assert resp.json()["tickers"] == []


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

    def test_second_call_same_day_is_served_from_cache(self, client):
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
# GET /api/screen + /api/screen/status/{job_id}
# ---------------------------------------------------------------------------

class TestScreenEndpoint:
    def test_triggers_run_screen_with_watchlist_tickers(self, client):
        client.post("/api/watchlist/add", json={"ticker": "NVDA", "type": "equity"})
        client.post("/api/watchlist/add", json={"ticker": "VOO", "type": "etf"})

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
