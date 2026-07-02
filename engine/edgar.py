"""
edgar.py — primary-source financial data from SEC EDGAR.

Design principles
-----------------
1. EDGAR is the single source of truth for fundamentals. Everything here is
   auditable: every value we return carries the exact XBRL concept, fiscal
   period end, and filing form it came from (see `Fact`).
2. XBRL tags are inconsistent across companies and over time. We resolve each
   *logical* metric (e.g. "revenue") against an ordered list of candidate GAAP
   tags and record which one actually resolved. This is what stops the engine
   from silently failing or guessing.
3. SEC requires a declared User-Agent and rate-limits ~10 req/s. Both are
   handled here. Set your real contact in config.yaml.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import requests

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"


# ---------------------------------------------------------------------------
# Concept map: logical metric -> ordered (taxonomy, tag) fallbacks.
# First tag that resolves wins; the winner is recorded for lineage.
# `flow` = income/cash-flow item (has a duration); otherwise balance-sheet instant.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Concept:
    key: str
    flow: bool
    candidates: tuple  # tuple of (taxonomy, tag)
    unit: str = "USD"


CONCEPTS: dict[str, Concept] = {
    # --- Income statement (flows) ---
    "revenue": Concept("revenue", True, (
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    )),
    "cost_of_revenue": Concept("cost_of_revenue", True, (
        ("us-gaap", "CostOfRevenue"),
        ("us-gaap", "CostOfGoodsAndServicesSold"),
        ("us-gaap", "CostOfGoodsSold"),
    )),
    "gross_profit": Concept("gross_profit", True, (
        ("us-gaap", "GrossProfit"),
    )),
    "operating_income": Concept("operating_income", True, (
        ("us-gaap", "OperatingIncomeLoss"),
    )),
    "net_income": Concept("net_income", True, (
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "ProfitLoss"),
    )),
    "interest_expense": Concept("interest_expense", True, (
        ("us-gaap", "InterestExpense"),
        ("us-gaap", "InterestExpenseNonoperating"),
        ("us-gaap", "InterestIncomeExpenseNet"),
    )),
    "dep_amort": Concept("dep_amort", True, (
        ("us-gaap", "DepreciationDepletionAndAmortization"),
        ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
        ("us-gaap", "DepreciationAndAmortization"),
        ("us-gaap", "Depreciation"),
        ("us-gaap", "DepreciationNonproduction"),
        ("us-gaap", "AmortizationOfIntangibleAssets"),
    )),
    # --- Cash flow (flows) ---
    "cfo": Concept("cfo", True, (
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    )),
    "capex": Concept("capex", True, (
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
    )),
    # --- Balance sheet (instants) ---
    "total_assets": Concept("total_assets", False, (
        ("us-gaap", "Assets"),
    )),
    "current_assets": Concept("current_assets", False, (
        ("us-gaap", "AssetsCurrent"),
    )),
    "total_liabilities": Concept("total_liabilities", False, (
        ("us-gaap", "Liabilities"),
    )),
    "current_liabilities": Concept("current_liabilities", False, (
        ("us-gaap", "LiabilitiesCurrent"),
    )),
    "total_equity": Concept("total_equity", False, (
        ("us-gaap", "StockholdersEquity"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    )),
    "cash": Concept("cash", False, (
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    )),
    "short_term_investments": Concept("short_term_investments", False, (
        ("us-gaap", "MarketableSecuritiesCurrent"),
        ("us-gaap", "ShortTermInvestments"),
        ("us-gaap", "AvailableForSaleSecuritiesCurrent"),
        ("us-gaap", "MarketableSecurities"),
    )),
    "long_term_investments": Concept("long_term_investments", False, (
        ("us-gaap", "MarketableSecuritiesNoncurrent"),
        ("us-gaap", "LongTermInvestments"),
        ("us-gaap", "AvailableForSaleSecuritiesNoncurrent"),
        ("us-gaap", "LongTermInvestmentsAndReceivablesNet"),
    )),
    "long_term_debt": Concept("long_term_debt", False, (
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("us-gaap", "LongTermDebt"),
    )),
    "short_term_debt": Concept("short_term_debt", False, (
        ("us-gaap", "DebtCurrent"),
        ("us-gaap", "ShortTermBorrowings"),
        ("us-gaap", "LongTermDebtCurrent"),
    )),
}


@dataclass
class Fact:
    """A single resolved data point with full provenance."""
    metric: str
    value: float
    period_end: str          # ISO date of the period end
    fiscal_year: int         # label derived from period_end year
    concept: str             # taxonomy:tag that actually resolved
    form: str                # e.g. 10-K
    filed: str               # filing date

    def source(self) -> str:
        return f"{self.concept} | {self.form} | period {self.period_end} | filed {self.filed}"


@dataclass
class CompanyData:
    ticker: str
    cik: str
    name: str
    sic: str
    sic_description: str
    # metric -> list[Fact], sorted by period_end ascending (oldest first)
    series: dict = field(default_factory=dict)
    quarterly: dict[str, "Fact"] = field(default_factory=dict)
    # logical metrics that could not be resolved at all
    unresolved: list = field(default_factory=list)

    def latest(self, metric: str) -> Optional[Fact]:
        s = self.series.get(metric)
        return s[-1] if s else None

    def latest_value(self, metric: str) -> Optional[float]:
        f = self.latest(metric)
        return f.value if f else None

    def value_for_period(self, metric: str, period_end: str) -> Optional[Fact]:
        """Return the Fact for this metric if it has a value for the given period_end, else None."""
        s = self.series.get(metric)
        if not s:
            return None
        for fact in s:
            if fact.period_end == period_end:
                return fact
        return None


class EdgarClient:
    def __init__(self, user_agent: str, request_delay: float = 0.2):
        if not user_agent or "example.com" in user_agent:
            raise ValueError(
                "Set a real SEC User-Agent in config.yaml (sec.user_agent). "
                "SEC returns 403 without a declared contact."
            )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.delay = request_delay
        self._ticker_map: Optional[dict] = None

    def _get(self, url: str) -> dict:
        time.sleep(self.delay)  # be polite to SEC
        r = self.session.get(url, timeout=30)
        r.raise_for_status()
        return r.json()

    def ticker_to_cik(self, ticker: str) -> str:
        if self._ticker_map is None:
            data = self._get(SEC_TICKERS_URL)
            self._ticker_map = {
                row["ticker"].upper(): str(row["cik_str"]).zfill(10)
                for row in data.values()
            }
        cik = self._ticker_map.get(ticker.upper())
        if cik is None:
            raise ValueError(f"Ticker {ticker!r} not found in SEC ticker map.")
        return cik

    def get_company(self, ticker: str, history_years: int = 15, include_quarterly: bool = False) -> CompanyData:
        cik = self.ticker_to_cik(ticker)
        subs = self._get(SEC_SUBMISSIONS_URL.format(cik10=cik))
        facts = self._get(SEC_COMPANYFACTS_URL.format(cik10=cik))

        cd = CompanyData(
            ticker=ticker.upper(),
            cik=cik,
            name=subs.get("name", ""),
            sic=str(subs.get("sic", "")),
            sic_description=subs.get("sicDescription", ""),
        )

        cutoff_year = datetime.now().year - history_years
        for key, concept in CONCEPTS.items():
            facts_for_key = self._resolve(facts, concept, cutoff_year)
            if facts_for_key:
                cd.series[key] = facts_for_key
            else:
                cd.unresolved.append(key)
        if include_quarterly:
            self._populate_quarterly(cd, facts, cutoff_year)
        return cd

    def get_company_with_latest_quarter(self, ticker: str, history_years: int = 15) -> CompanyData:
        return self.get_company(ticker, history_years, include_quarterly=True)

    @staticmethod
    def _resolve(facts: dict, concept: Concept, cutoff_year: int) -> list:
        """Resolve each fiscal year against the highest-priority candidate that has data.

        This stitches across tag changes over time. For a given period end, we prefer
        the earlier-listed candidate, and within the same tag we retain the most recently
        filed restatement via _annual_points()."""
        gaap = facts.get("facts", {})
        selected: dict[str, tuple[int, dict, str]] = {}
        for priority, (taxonomy, tag) in enumerate(concept.candidates):
            node = gaap.get(taxonomy, {}).get(tag)
            if not node:
                continue
            units = node.get("units", {}).get(concept.unit)
            if not units:
                continue
            points = EdgarClient._annual_points(units, concept.flow, cutoff_year)
            if not points:
                continue
            concept_label = f"{taxonomy}:{tag}"
            for p in points:
                end = p["end"]
                if end not in selected:
                    selected[end] = (priority, p, concept_label)
        if not selected:
            return []
        return [
            Fact(concept.key, p["val"], end, int(end[:4]), concept_label, p["form"], p["filed"])
            for end, (_, p, concept_label) in sorted(selected.items())
        ]

    @staticmethod
    def _annual_points(units: list, is_flow: bool, cutoff_year: int) -> list:
        """
        Collect one annual data point per fiscal-year-end.

        Flows (income/cash flow) have start+end ~365 days apart.
        Instants (balance sheet) have only a period end.
        Dedupe by period end, preferring the most recently filed value
        (restated figures supersede originals). 10-K (annual) forms only.
        """
        by_end: dict[str, dict] = {}
        for u in units:
            form = u.get("form", "")
            if not form.startswith("10-K"):
                continue
            end = u.get("end")
            if not end:
                continue
            if is_flow:
                start = u.get("start")
                if not start:
                    continue
                try:
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                except ValueError:
                    continue
                if not (350 <= days <= 380):   # full-year duration only
                    continue
            if int(end[:4]) < cutoff_year:
                continue
            prev = by_end.get(end)
            if prev is None or u.get("filed", "") > prev.get("filed", ""):
                by_end[end] = u
        ordered = sorted(by_end.values(), key=lambda x: x["end"])
        return ordered

    @staticmethod
    def _quarterly_points(units: list, is_flow: bool, cutoff_year: int) -> list:
        """
        Collect one quarterly data point per period end.

        Flows (income/cash flow) must be a single quarter duration.
        Instants (balance sheet) accept the latest 10-Q endpoint normally.
        Dedupe by period end, preferring the most recently filed value.
        """
        by_end: dict[str, dict] = {}
        for u in units:
            form = u.get("form", "")
            if not form.startswith("10-Q"):
                continue
            end = u.get("end")
            if not end:
                continue
            if is_flow:
                start = u.get("start")
                if not start:
                    continue
                try:
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                except ValueError:
                    continue
                if not (80 <= days <= 100):
                    continue
            if int(end[:4]) < cutoff_year:
                continue
            prev = by_end.get(end)
            if prev is None or u.get("filed", "") > prev.get("filed", ""):
                by_end[end] = u
        ordered = sorted(by_end.values(), key=lambda x: x["end"])
        return ordered

    @staticmethod
    def _resolve_quarterly(facts: dict, concept: Concept, cutoff_year: int) -> list:
        gaap = facts.get("facts", {})
        selected: dict[str, tuple[int, dict, str]] = {}
        for priority, (taxonomy, tag) in enumerate(concept.candidates):
            node = gaap.get(taxonomy, {}).get(tag)
            if not node:
                continue
            units = node.get("units", {}).get(concept.unit)
            if not units:
                continue
            points = EdgarClient._quarterly_points(units, concept.flow, cutoff_year)
            if not points:
                continue
            concept_label = f"{taxonomy}:{tag}"
            for p in points:
                end = p["end"]
                if end not in selected:
                    selected[end] = (priority, p, concept_label)
        if not selected:
            return []
        return [
            Fact(concept.key, p["val"], end, int(end[:4]), concept_label, p["form"], p["filed"])
            for end, (_, p, concept_label) in sorted(selected.items())
        ]

    @staticmethod
    def _populate_quarterly(cd: CompanyData, facts: dict, cutoff_year: int) -> None:
        quarterly: dict[str, Fact] = {}
        latest_quarter_end = ""
        for key, concept in CONCEPTS.items():
            points = EdgarClient._resolve_quarterly(facts, concept, cutoff_year)
            if points:
                latest = points[-1]
                quarterly[key] = latest
                if latest.period_end > latest_quarter_end:
                    latest_quarter_end = latest.period_end
        if not latest_quarter_end:
            return
        annual_latest_end = ""
        for facts_list in cd.series.values():
            if facts_list:
                candidate = facts_list[-1].period_end
                if candidate > annual_latest_end:
                    annual_latest_end = candidate
        if latest_quarter_end <= annual_latest_end:
            return
        cd.quarterly = {
            key: fact for key, fact in quarterly.items()
            if fact.period_end == latest_quarter_end
        }
