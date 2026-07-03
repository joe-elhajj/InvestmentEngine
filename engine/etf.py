"""
etf.py — Lightweight ETF/fund profile via yfinance (market-vendor data, lower trust tier).

EtfProfile is fully defensive: any exception at any stage leaves fields as None.
Callers must handle None for every field.

Verified against yfinance 1.4.x field layout (2025):
  - quoteType:    info["quoteType"]                  → "ETF" | "MUTUALFUND" | "EQUITY" | …
  - name:         info["longName"] / info["shortName"]
  - category:     info["category"]
  - totalAssets:  info["totalAssets"] or info["netAssets"]  (int / float, USD)
  - expense_ratio (0–1 decimal fraction):
      primary  → funds_data.fund_operations row "Annual Report Expense Ratio" (already decimal)
      fallback → info["netExpenseRatio"] / 100.0              (percent units in info)
  - top_holdings: funds_data.top_holdings DataFrame
      index = Symbol string, column "Holding Percent" = weight 0–1 scale
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# quoteType values that indicate a fund / ETF
FUND_QUOTE_TYPES = {"ETF", "MUTUALFUND"}


@dataclass
class EtfProfile:
    ticker: str
    quote_type: Optional[str] = None             # yfinance quoteType; ETF / MUTUALFUND / …
    name: Optional[str] = None
    category: Optional[str] = None
    expense_ratio: Optional[float] = None        # 0–1 decimal fraction; None ≠ 0.0
    total_assets: Optional[float] = None         # AUM in USD
    top10_concentration: Optional[float] = None  # fraction of AUM in top 10 holdings
    # (ticker_str, weight_fraction) pairs, sorted by weight descending
    top_holdings: list[tuple[str, float]] = field(default_factory=list)


def fetch_etf_profile(ticker: str) -> EtfProfile:
    """
    Fetch ETF/fund metadata via yfinance.  Always returns an EtfProfile; any
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

    # ── Basic info ──────────────────────────────────────────────────────────
    try:
        info = t.info or {}
        profile.quote_type   = info.get("quoteType")
        profile.name         = info.get("longName") or info.get("shortName")
        profile.category     = info.get("category")
        profile.total_assets = info.get("totalAssets") or info.get("netAssets") or None
    except Exception as exc:
        log.warning("yfinance info fetch for %s failed: %s", ticker, exc)
        info = {}

    # ── Expense ratio ────────────────────────────────────────────────────────
    # Primary: fund_operations DataFrame → "Annual Report Expense Ratio" row
    # already in decimal-fraction form (e.g. 0.0035 = 0.35%).
    # Fallback: info["netExpenseRatio"] which is in percent units (0.35 = 0.35%).
    try:
        fo = t.funds_data.fund_operations
        if fo is not None and "Annual Report Expense Ratio" in fo.index:
            raw = fo.loc["Annual Report Expense Ratio"].iloc[0]
            if raw is not None:
                profile.expense_ratio = float(raw)
    except Exception as exc:
        log.debug("funds_data.fund_operations for %s: %s", ticker, exc)

    if profile.expense_ratio is None:
        raw_er = info.get("netExpenseRatio")
        if raw_er is not None:
            try:
                profile.expense_ratio = float(raw_er) / 100.0
            except (TypeError, ValueError):
                pass

    # ── Top holdings ─────────────────────────────────────────────────────────
    # funds_data.top_holdings: DataFrame with Symbol as index,
    # "Holding Percent" column containing weights in 0–1 scale.
    try:
        th = t.funds_data.top_holdings
        if th is not None and not th.empty:
            holdings: list[tuple[str, float]] = []
            total_w = 0.0
            for sym in th.index:
                if sym is None:
                    continue
                try:
                    wt = float(th.loc[sym, "Holding Percent"])
                except (KeyError, TypeError, ValueError):
                    continue
                if wt <= 0 or wt > 1:
                    continue
                holdings.append((str(sym).upper(), wt))
                total_w += wt
            profile.top_holdings = sorted(holdings, key=lambda x: x[1], reverse=True)
            n = len(profile.top_holdings)
            if n >= 10:
                profile.top10_concentration = sum(w for _, w in profile.top_holdings[:10])
            elif n > 0:
                profile.top10_concentration = total_w
    except Exception as exc:
        log.warning("yfinance holdings fetch for %s failed: %s", ticker, exc)

    return profile
