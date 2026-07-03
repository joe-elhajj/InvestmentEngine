"""
analysis.py — single-ticker entry point.

Composes the EDGAR fetch, market quote, and the pure `derive()` pipeline
into one call. This is the one place a caller (CLI, batch screen, or a
future web layer) reaches to turn a ticker into a full AnalysisResult;
pipeline.derive() itself stays I/O-free.
"""

from __future__ import annotations

from typing import Optional

from engine.edgar import EdgarClient
from engine.market import get_quote
from engine.pipeline import AnalysisResult, derive


def run_single_ticker(
    ticker: str,
    cfg: dict,
    client: EdgarClient,
    manual_price: Optional[float] = None,
    manual_shares: Optional[float] = None,
    history_years: int = 15,
) -> AnalysisResult:
    """
    Fetch `ticker` from EDGAR (via the caller-supplied `client`) and the
    market quote, then run the deterministic pipeline.

    `client` is passed in rather than constructed here so a long-lived
    caller (e.g. a web server) can reuse one EdgarClient — and its
    in-memory ticker map — across many calls instead of re-fetching the
    SEC ticker list on every request.

    Propagates exceptions from the EDGAR fetch unchanged: absence-is-not-
    zero means a failed or missing lookup is a real error, never degraded
    into an empty or fabricated AnalysisResult.
    """
    cd = client.get_company_with_latest_quarter(ticker, history_years)
    quote = get_quote(ticker, manual_price, manual_shares)
    return derive(cd, quote, cfg)
