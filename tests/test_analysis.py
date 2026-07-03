"""
test_analysis.py — tests for engine.analysis.run_single_ticker.

Coverage:
  - composes get_company_with_latest_quarter -> get_quote -> derive, in that
    order, passing the right arguments to each
  - the caller-supplied client is used as-is; run_single_ticker never
    constructs its own EdgarClient
  - config dict, manual_price/manual_shares, and history_years all pass
    through unchanged
  - returns whatever derive() returns (the AnalysisResult)
  - propagates exceptions from the EDGAR fetch rather than swallowing them
    into an empty/fabricated result (absence-is-not-zero)
  - end-to-end with the real derive() against a minimal synthetic company
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from engine.analysis import run_single_ticker
from engine.edgar import CompanyData, Fact
from engine.market import Quote


def _cfg() -> dict:
    return {"valuation": {"assumed_tax_rate": 0.21}}


class TestComposition:
    def test_composes_edgar_quote_derive_in_order(self):
        client = MagicMock()
        sentinel_cd = MagicMock(name="CompanyData")
        client.get_company_with_latest_quarter.return_value = sentinel_cd

        sentinel_quote = MagicMock(name="Quote")
        sentinel_result = MagicMock(name="AnalysisResult")

        with (
            patch("engine.analysis.get_quote", return_value=sentinel_quote) as mock_quote,
            patch("engine.analysis.derive", return_value=sentinel_result) as mock_derive,
        ):
            cfg = _cfg()
            result = run_single_ticker("AAPL", cfg, client, history_years=15)

        client.get_company_with_latest_quarter.assert_called_once_with("AAPL", 15)
        mock_quote.assert_called_once_with("AAPL", None, None)
        mock_derive.assert_called_once_with(sentinel_cd, sentinel_quote, cfg)
        assert result is sentinel_result

    def test_client_passed_in_is_used_verbatim_never_constructed_internally(self):
        """The whole point of the signature: reuse a caller-owned client."""
        import engine.analysis as analysis_mod

        client = MagicMock()
        client.get_company_with_latest_quarter.return_value = MagicMock()
        with (
            patch("engine.analysis.get_quote", return_value=MagicMock()),
            patch("engine.analysis.derive", return_value=MagicMock()),
            patch.object(analysis_mod, "EdgarClient") as mock_cls,
        ):
            run_single_ticker("AAPL", _cfg(), client)

        mock_cls.assert_not_called()
        client.get_company_with_latest_quarter.assert_called_once()

    def test_manual_price_and_shares_passed_through(self):
        client = MagicMock()
        client.get_company_with_latest_quarter.return_value = MagicMock()
        with (
            patch("engine.analysis.get_quote", return_value=MagicMock()) as mock_quote,
            patch("engine.analysis.derive", return_value=MagicMock()),
        ):
            run_single_ticker(
                "AAPL", _cfg(), client,
                manual_price=195.0, manual_shares=15_300_000_000.0,
            )
        mock_quote.assert_called_once_with("AAPL", 195.0, 15_300_000_000.0)

    def test_history_years_passed_through(self):
        client = MagicMock()
        client.get_company_with_latest_quarter.return_value = MagicMock()
        with (
            patch("engine.analysis.get_quote", return_value=MagicMock()),
            patch("engine.analysis.derive", return_value=MagicMock()),
        ):
            run_single_ticker("TSM", _cfg(), client, history_years=20)
        client.get_company_with_latest_quarter.assert_called_once_with("TSM", 20)

    def test_history_years_default_is_15(self):
        client = MagicMock()
        client.get_company_with_latest_quarter.return_value = MagicMock()
        with (
            patch("engine.analysis.get_quote", return_value=MagicMock()),
            patch("engine.analysis.derive", return_value=MagicMock()),
        ):
            run_single_ticker("TSM", _cfg(), client)
        client.get_company_with_latest_quarter.assert_called_once_with("TSM", 15)

    def test_config_passed_through_unchanged(self):
        client = MagicMock()
        client.get_company_with_latest_quarter.return_value = MagicMock()
        cfg = {"valuation": {"assumed_tax_rate": 0.30}, "marker": "unique-cfg-object"}
        with (
            patch("engine.analysis.get_quote", return_value=MagicMock()),
            patch("engine.analysis.derive", return_value=MagicMock()) as mock_derive,
        ):
            run_single_ticker("AAPL", cfg, client)
        _, _, passed_cfg = mock_derive.call_args[0]
        assert passed_cfg is cfg, "cfg must be passed through identically, not copied or mutated"


class TestErrorPropagation:
    def test_edgar_lookup_failure_propagates(self):
        """A failed/missing EDGAR lookup must raise, not degrade to an empty result."""
        client = MagicMock()
        client.get_company_with_latest_quarter.side_effect = ValueError(
            "Ticker 'FAKE' not found in SEC ticker map."
        )
        with pytest.raises(ValueError, match="not found in SEC ticker map"):
            run_single_ticker("FAKE", _cfg(), client)

    def test_edgar_failure_short_circuits_before_quote_and_derive(self):
        """No partial/fabricated result: if EDGAR fails, quote/derive are never reached."""
        client = MagicMock()
        client.get_company_with_latest_quarter.side_effect = RuntimeError("network down")
        with (
            patch("engine.analysis.get_quote") as mock_quote,
            patch("engine.analysis.derive") as mock_derive,
        ):
            with pytest.raises(RuntimeError, match="network down"):
                run_single_ticker("FAKE", _cfg(), client)
        mock_quote.assert_not_called()
        mock_derive.assert_not_called()

    def test_quote_failure_propagates(self):
        """get_quote itself degrades gracefully internally, but if it still raises, don't hide it."""
        client = MagicMock()
        client.get_company_with_latest_quarter.return_value = MagicMock()
        with (
            patch("engine.analysis.get_quote", side_effect=RuntimeError("quote service down")),
            patch("engine.analysis.derive") as mock_derive,
        ):
            with pytest.raises(RuntimeError, match="quote service down"):
                run_single_ticker("AAPL", _cfg(), client)
        mock_derive.assert_not_called()


class TestEndToEnd:
    def test_returns_real_analysis_result(self):
        """Integration-style: real derive() against a minimal synthetic CompanyData/Quote."""
        cd = CompanyData(
            ticker="TEST", cik="0000000001", name="Test Co",
            sic="7372", sic_description="Software",
            recent_forms=["10-K"],
        )
        years = list(range(2020, 2024))
        cd.series = {
            "revenue": [
                Fact("revenue", 100.0 * (1.1 ** i), f"{y}-12-31", y,
                     "us-gaap:Revenues", "10-K", f"{y + 1}-02-15", "USD")
                for i, y in enumerate(years)
            ],
        }
        client = MagicMock()
        client.get_company_with_latest_quarter.return_value = cd
        quote = Quote("TEST", price=50.0, shares_outstanding=10.0, market_cap=500.0, source="test")

        with patch("engine.analysis.get_quote", return_value=quote):
            result = run_single_ticker("TEST", _cfg(), client)

        assert result.company is cd
        assert result.quote is quote
        assert result.derived.get("revenue") == pytest.approx(100.0 * (1.1 ** 3))
