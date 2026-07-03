"""
etf.py — Lightweight ETF/fund profile via yfinance (market-vendor data, lower trust tier).

EtfProfile is fully defensive: any exception at any stage leaves fields as None.
Callers must handle None for every field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class EtfProfile:
    ticker: str
    name: Optional[str] = None
    category: Optional[str] = None
    expense_ratio: Optional[float] = None       # annual net expense ratio (0–1 scale)
    total_assets: Optional[float] = None         # AUM in USD
    top10_concentration: Optional[float] = None  # fraction of AUM in top 10 holdings
    # list of (ticker_str, weight_fraction) — only holdings with valid weights
    top_holdings: list[tuple[str, float]] = field(default_factory=list)


def fetch_etf_profile(ticker: str) -> EtfProfile:
    """
    Fetch ETF metadata via yfinance.  Always returns an EtfProfile; any
    exception at any stage results in None fields rather than propagating.

    Data quality: market-vendor (yfinance) — best-effort, not filing-grade.
    """
    profile = EtfProfile(ticker=ticker)
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed; ETF profile for %s will have no data", ticker)
        return profile

    try:
        t = yf.Ticker(ticker)
    except Exception as exc:
        log.warning("yfinance Ticker(%s) failed: %s", ticker, exc)
        return profile

    # Basic info
    try:
        info = t.info or {}
        profile.name         = info.get("longName") or info.get("shortName")
        profile.category     = info.get("category")
        profile.total_assets = info.get("totalAssets") or info.get("netAssets")

        raw_er = info.get("annualReportExpenseRatio") or info.get("totalExpenseRatio")
        if raw_er is not None:
            try:
                er_f = float(raw_er)
                # yfinance sometimes returns already in fraction, sometimes in percent
                profile.expense_ratio = er_f if er_f <= 1.0 else er_f / 100.0
            except (TypeError, ValueError):
                pass
    except Exception as exc:
        log.warning("yfinance info fetch for %s failed: %s", ticker, exc)

    # Top holdings
    try:
        holdings_df = getattr(t, "get_holdings", lambda: None)()
        if holdings_df is None:
            holdings_df = getattr(t, "holdings", None)
        if holdings_df is not None and not holdings_df.empty:
            holdings: list[tuple[str, float]] = []
            total_weight = 0.0
            for sym, row in holdings_df.iterrows():
                if sym is None:
                    continue
                try:
                    wt = float(row.get("Holding Percent", row.iloc[0] if len(row) else None))
                except (TypeError, ValueError, IndexError):
                    continue
                if not (0 < wt <= 1):
                    # Some sources return 0-100; normalise
                    if 0 < wt <= 100:
                        wt = wt / 100.0
                    else:
                        continue
                holdings.append((str(sym).upper(), wt))
                total_weight += wt
            profile.top_holdings = sorted(holdings, key=lambda x: x[1], reverse=True)
            if len(profile.top_holdings) >= 10:
                top10_w = sum(w for _, w in profile.top_holdings[:10])
                profile.top10_concentration = top10_w
            elif profile.top_holdings:
                profile.top10_concentration = total_weight
    except Exception as exc:
        log.warning("yfinance holdings fetch for %s failed: %s", ticker, exc)

    return profile
