"""
market.py — current price and share data.

Deliberately isolated from EDGAR. Fundamentals come from filings (authoritative,
slow-moving); price and current multiples come from a market feed (fast-moving,
and frankly more fragile). Keeping this behind one small interface means you can
swap the free default for a paid provider (FMP, EODHD, Polygon, ...) later
without touching the analytical core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Quote:
    ticker: str
    price: Optional[float]
    shares_outstanding: Optional[float]
    market_cap: Optional[float]
    source: str


def get_quote(ticker: str, manual_price: Optional[float] = None,
              manual_shares: Optional[float] = None) -> Quote:
    """
    If manual_price/shares are supplied (e.g. from config or CLI), use those and
    skip the network entirely — useful for reproducible/offline runs.
    Otherwise try yfinance. Failures degrade gracefully to an empty quote so the
    fundamental report still renders (multiples just get flagged as unavailable).
    """
    if manual_price is not None:
        mc = manual_price * manual_shares if manual_shares else None
        return Quote(ticker, manual_price, manual_shares, mc, "manual override")

    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        shares = info.get("sharesOutstanding")
        mc = info.get("marketCap") or (price * shares if price and shares else None)
        return Quote(ticker, price, shares, mc, "yfinance")
    except Exception as e:  # noqa: BLE001 - any failure should not kill the run
        return Quote(ticker, None, None, None, f"unavailable ({type(e).__name__})")
