"""
edgar.py — primary-source financial data from SEC EDGAR.

Design principles
-----------------
1. EDGAR is the single source of truth for fundamentals. Everything here is
   auditable: every value we return carries the exact XBRL concept, fiscal
   period end, and filing form it came from (see `Fact`).
2. XBRL tags are inconsistent across companies and over time. We resolve each
   *logical* metric (e.g. "revenue") against an ordered list of candidate GAAP
   or IFRS tags and record which one actually resolved. This is what stops the
   engine from silently failing or guessing.
3. Both US-GAAP (10-K) and IFRS (20-F / 40-F) filers are supported. IFRS
   candidates are listed AFTER us-gaap candidates so domestic companies are
   unaffected.  Currency is resolved per-concept (USD preferred when dominant;
   otherwise the non-USD currency with the most annual points).
4. SEC requires a declared User-Agent and rate-limits ~10 req/s. Both are
   handled here. Set your real contact in config.yaml.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

# Annual-report form prefixes accepted by _annual_points.
# 10-K  = domestic US annual
# 20-F  = Foreign Private Issuer annual
# 40-F  = Canadian FPI annual (MJDS)
_ANNUAL_FORM_PREFIXES = ("10-K", "20-F", "40-F")


# ---------------------------------------------------------------------------
# Concept map: logical metric -> ordered (taxonomy, tag) fallbacks.
# First tag that resolves wins; the winner is recorded for lineage.
# us-gaap candidates come first so domestic companies are unaffected.
# ifrs-full candidates are appended as fallbacks for FPI 20-F / 40-F filers.
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
        # IFRS fallbacks (verified against TSM/CCJ companyfacts)
        ("ifrs-full", "Revenue"),
        ("ifrs-full", "RevenueFromContractsWithCustomers"),
    )),
    "cost_of_revenue": Concept("cost_of_revenue", True, (
        ("us-gaap", "CostOfRevenue"),
        ("us-gaap", "CostOfGoodsAndServicesSold"),
        ("us-gaap", "CostOfGoodsSold"),
        ("ifrs-full", "CostOfSales"),
    )),
    "gross_profit": Concept("gross_profit", True, (
        ("us-gaap", "GrossProfit"),
        ("ifrs-full", "GrossProfit"),
    )),
    "operating_income": Concept("operating_income", True, (
        ("us-gaap", "OperatingIncomeLoss"),
        ("ifrs-full", "ProfitLossFromOperatingActivities"),
    )),
    "net_income": Concept("net_income", True, (
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "ProfitLoss"),
        ("ifrs-full", "ProfitLoss"),
        ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
    )),
    "interest_expense": Concept("interest_expense", True, (
        ("us-gaap", "InterestExpense"),
        ("us-gaap", "InterestExpenseNonoperating"),
        ("us-gaap", "InterestIncomeExpenseNet"),
        # FinanceCosts is the IFRS aggregate for interest and finance charges
        ("ifrs-full", "FinanceCosts"),
        ("ifrs-full", "InterestExpenseOnBorrowings"),
        ("ifrs-full", "InterestExpenseOnBonds"),
    )),
    "dep_amort": Concept("dep_amort", True, (
        ("us-gaap", "DepreciationDepletionAndAmortization"),
        ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
        ("us-gaap", "DepreciationAndAmortization"),
        ("us-gaap", "Depreciation"),
        ("us-gaap", "DepreciationNonproduction"),
        ("us-gaap", "AmortizationOfIntangibleAssets"),
        # IFRS: no combined tag in practice; DepreciationExpense is typically larger
        ("ifrs-full", "DepreciationAndAmortisationExpense"),
        ("ifrs-full", "DepreciationExpense"),
        ("ifrs-full", "AmortisationExpense"),
    )),
    # --- Cash flow (flows) ---
    "cfo": Concept("cfo", True, (
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    )),
    "capex": Concept("capex", True, (
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        # IFRS capex tag verified in TSM companyfacts
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipment"),
    )),
    # --- Balance sheet (instants) ---
    "total_assets": Concept("total_assets", False, (
        ("us-gaap", "Assets"),
        ("ifrs-full", "Assets"),
    )),
    "current_assets": Concept("current_assets", False, (
        ("us-gaap", "AssetsCurrent"),
        ("ifrs-full", "CurrentAssets"),
    )),
    "total_liabilities": Concept("total_liabilities", False, (
        ("us-gaap", "Liabilities"),
        ("ifrs-full", "Liabilities"),
    )),
    "current_liabilities": Concept("current_liabilities", False, (
        ("us-gaap", "LiabilitiesCurrent"),
        ("ifrs-full", "CurrentLiabilities"),
    )),
    "total_equity": Concept("total_equity", False, (
        ("us-gaap", "StockholdersEquity"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        ("ifrs-full", "Equity"),
        ("ifrs-full", "EquityAttributableToOwnersOfParent"),
    )),
    "cash": Concept("cash", False, (
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
        ("ifrs-full", "CashAndCashEquivalents"),
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
    # F-6: the convertible-debt tag family is the ONLY debt tag some filers
    # report once their conventional notes are retired — NOW carries
    # ConvertibleLongTermNotesPayable ($1.491B at 2025-12-31) and nothing else
    # (FLNC likewise from FY2025). Without it the series is empty, total_debt
    # dead-ends at None, and net_debt/implied-growth abstain on a company whose
    # debt is plainly filed. Listed AFTER the conventional us-gaap tags: within
    # a Concept, _resolve picks ONE candidate per period_end by list priority
    # (never sums), so a filer reporting both LongTermDebt and
    # ConvertibleLongTermNotesPayable resolves to the comprehensive figure, not
    # the convertible slice of it.
    #
    # The short-term counterpart (us-gaap:ConvertibleDebtCurrent, which is what
    # PANW reports) is deliberately NOT added here yet. It is a SUBSET of the
    # total-debt tag us-gaap:LongTermDebt, and total_debt sums the long and
    # short lists, so mapping it double-counts every year a filer reports both
    # — confirmed for PANW FY2021-23 (FY2022/23 exactly doubled). That must
    # wait on the total_debt combination fix, which has to learn that
    # `LongTermDebt` already includes the current portion. Red target for that
    # PR: tests/test_f6_convertible_debt.py, the two strict-xfail cases.
    "long_term_debt": Concept("long_term_debt", False, (
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("us-gaap", "LongTermDebt"),
        ("us-gaap", "ConvertibleLongTermNotesPayable"),
        ("ifrs-full", "LongtermBorrowings"),
    )),
    "short_term_debt": Concept("short_term_debt", False, (
        ("us-gaap", "DebtCurrent"),
        ("us-gaap", "ShortTermBorrowings"),
        ("us-gaap", "LongTermDebtCurrent"),
        ("ifrs-full", "ShorttermBorrowings"),
        ("ifrs-full", "CurrentPortionOfLongtermBorrowings"),
    )),
    # --- Compensation & investment (flows) ---
    "sbc": Concept("sbc", True, (
        ("us-gaap", "ShareBasedCompensation"),
        ("us-gaap", "AllocatedShareBasedCompensationExpense"),
    )),
    "rnd": Concept("rnd", True, (
        ("us-gaap", "ResearchAndDevelopmentExpense"),
        ("us-gaap", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
    )),
    # diluted_shares is a count (unit = "shares"), not a USD flow
    "diluted_shares": Concept("diluted_shares", True, (
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ), unit="shares"),
}


@dataclass
class Fact:
    """A single resolved data point with full provenance."""
    metric: str
    value: float
    period_end: str          # ISO date of the period end
    fiscal_year: int         # label derived from period_end year
    concept: str             # taxonomy:tag that actually resolved
    form: str                # e.g. 10-K, 20-F
    filed: str               # filing date
    currency: str = "USD"    # reporting currency for this data point
    # SEC accession number of the filing this point came from (e.g.
    # "0001045810-22-000036"). Present in the raw companyfacts point
    # (p["accn"]) but dropped by every Fact(...) construction site until
    # this field was added (Session A UNVERIFIED item / Session B.2 PR-A) —
    # absence-is-not-zero: an unknown accession is None, never a fabricated
    # empty string that would read as "known blank."
    accn: Optional[str] = None

    def source(self) -> str:
        return f"{self.concept} | {self.form} | period {self.period_end} | filed {self.filed}"

    def source_ref(self) -> str:
        """Compact filing reference for gap/lineage text — form + accession
        + filed date, e.g. "10-K 0001045810-22-000036 filed 2022-03-18".
        Omits the accession clause (rather than printing a placeholder)
        when accn is unknown."""
        if self.accn:
            return f"{self.form} {self.accn} filed {self.filed}"
        return f"{self.form} filed {self.filed}"


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
    # dominant reporting currency across all resolved concepts
    reporting_currency: str = "USD"
    # unique form types seen in recent filings (used for B1 classification)
    recent_forms: list[str] = field(default_factory=list)

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


# ---------------------------------------------------------------------------
# R&D capitalization support (Tier 1, deterministic — no model involvement)
# ---------------------------------------------------------------------------

_RND_NO_RND = "no_rnd"
_RND_PRESENT = "present"
_RND_PARTIAL_GAP = "partial_gap"


def is_fpi(cd: CompanyData) -> tuple[bool, str]:
    """
    Evidence-based FPI (Foreign Private Issuer) detection: filed 20-F or
    40-F. Never inferred from SIC or ticker. Mirrors engine/screen.py's own
    FPI evidence check (SEC form history) but is kept as a small, separate
    predicate here rather than imported from screen.py: screen.py already
    imports engine.durability (`from engine import durability as D`), so
    durability.py importing screen.py back for this check would be
    circular. Same evidence class (recent_forms), not a parallel inference
    rule.
    """
    fpi_forms = [f for f in cd.recent_forms if f.startswith(("20-F", "40-F"))]
    if fpi_forms:
        return True, f"FPI: {fpi_forms[0][:4]} observed"
    return False, ""


def classify_rnd_series(cd: CompanyData) -> tuple[str, dict]:
    """
    Extract the annual R&D expense series (one value per total_assets-
    anchored fiscal year, mirroring exactly what _build_year_entry's
    _pv("rnd") resolves per year in engine/pipeline.py) and classify it
    into exactly one of three states for the R&D-capitalization regime
    (engine/metrics.py::build_research_asset):

      "no_rnd"      -- the tag never resolves in ANY filing (zero raw facts
                       across every taxonomy candidate) -- a legitimate zero
                       adjustment, not a gap. No badge; nothing to disclose.
      "present"     -- the tag resolves for every total_assets-anchored year.
      "partial_gap" -- the tag resolves for at least one year but not every
                       anchored year -- a real degradation, not silence.
                       Already disclosed via _build_year_entry's existing
                       _pv("rnd") gap-logging path (no new gap message is
                       added here); this classification exists purely so
                       callers can distinguish it from "no_rnd" for badging.

    Returns (state, series) where series maps each total_assets anchor
    period_end -> R&D value (or None).
    """
    anchors = sorted(f.period_end for f in cd.series.get("total_assets", []))
    series: dict = {}
    for pe in anchors:
        f = cd.value_for_period("rnd", pe)
        series[pe] = f.value if f is not None else None

    if not cd.series.get("rnd"):
        return _RND_NO_RND, series
    if anchors and all(series[pe] is not None for pe in anchors):
        return _RND_PRESENT, series
    return _RND_PARTIAL_GAP, series


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        request_delay: float = 0.2,
        cache_dir: str = ".cache/edgar",
        cache_ttl_seconds: int = 86400,
        no_cache: bool = False,
    ):
        if not user_agent or "example.com" in user_agent:
            raise ValueError(
                "Set a real SEC User-Agent in config.yaml (sec.user_agent). "
                "SEC returns 403 without a declared contact."
            )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.delay = request_delay
        self._ticker_map: Optional[dict] = None
        self._cache_dir = Path(cache_dir)
        self._cache_ttl = cache_ttl_seconds
        self._no_cache = no_cache

    def _cache_path(self, cik: str, kind: str) -> Path:
        return self._cache_dir / f"{cik}_{kind}.json"

    def _read_cache(self, cik: str, kind: str) -> Optional[dict]:
        if self._no_cache:
            return None
        p = self._cache_path(cik, kind)
        if not p.exists():
            return None
        age = datetime.now().timestamp() - p.stat().st_mtime
        if age > self._cache_ttl:
            return None
        return json.loads(p.read_text())

    def _write_cache(self, cik: str, kind: str, data: dict) -> None:
        if self._no_cache:
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(cik, kind).write_text(json.dumps(data))

    def _get(self, url: str) -> dict:
        time.sleep(self.delay)  # be polite to SEC
        r = self.session.get(url, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_text(self, url: str) -> str:
        """
        Same session/rate-limit/User-Agent discipline as `_get`, but for
        fetching a raw filing document (HTML/text) rather than a JSON API
        response — e.g. the actual 10-K document body, as opposed to the
        submissions/companyfacts JSON. Public (not `_get_text`): this is a
        legitimate cross-module fetch primitive, used by engine/filings.py,
        not an EdgarClient-internal implementation detail.
        """
        time.sleep(self.delay)
        r = self.session.get(url, timeout=30)
        r.raise_for_status()
        return r.text

    def _get_cached(self, cik: str, kind: str, url: str) -> dict:
        cached = self._read_cache(cik, kind)
        if cached is not None:
            return cached
        data = self._get(url)
        self._write_cache(cik, kind, data)
        return data

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
        subs = self._get_cached(cik, "submissions", SEC_SUBMISSIONS_URL.format(cik10=cik))

        # Build CompanyData from submissions FIRST so recent_forms is always
        # populated before the companyfacts fetch, which may 404 for investment
        # companies (ETFs/UITs) that file prospectus/N-PORT forms but have no
        # XBRL financial statements.
        cd = CompanyData(
            ticker=ticker.upper(),
            cik=cik,
            name=subs.get("name", ""),
            sic=str(subs.get("sic", "")),
            sic_description=subs.get("sicDescription", ""),
            recent_forms=list(set(subs.get("filings", {}).get("recent", {}).get("form", []))),
        )

        # Fetch XBRL company facts.  Investment companies with CIKs often return
        # 404 here because they don't file XBRL financials.  On 404 return the
        # partial cd (recent_forms populated, series empty) so callers can
        # classify from form history alone without needing financial data.
        try:
            facts = self._get_cached(cik, "companyfacts", SEC_COMPANYFACTS_URL.format(cik10=cik))
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                log.debug(
                    "%s (CIK %s): companyfacts 404 — returning partial CompanyData (forms only)",
                    ticker, cik,
                )
                return cd
            raise

        cutoff_year = datetime.now().year - history_years

        # Resolve revenue first (Session B.4): its period-ends are already
        # duration-validated and become the fiscal-year-end anchor set that
        # instant concepts below use to reject off-cycle footnote snapshots.
        # This does not change revenue's own resolution in any way.
        revenue_concept = CONCEPTS["revenue"]
        revenue_facts = self._resolve(facts, revenue_concept, cutoff_year)
        if revenue_facts:
            cd.series["revenue"] = revenue_facts
        else:
            cd.unresolved.append("revenue")
        valid_ends = self._valid_annual_ends([f.period_end for f in revenue_facts])

        for key, concept in CONCEPTS.items():
            if key == "revenue":
                continue  # already resolved above
            facts_for_key = self._resolve(facts, concept, cutoff_year, valid_ends)
            if facts_for_key:
                cd.series[key] = facts_for_key
            else:
                cd.unresolved.append(key)

        # Determine reporting currency and drop off-currency series (A3)
        self._set_reporting_currency(cd)

        if include_quarterly:
            self._populate_quarterly(cd, facts, cutoff_year)
        return cd

    def get_company_with_latest_quarter(self, ticker: str, history_years: int = 15) -> CompanyData:
        return self.get_company(ticker, history_years, include_quarterly=True)

    @staticmethod
    def _dominant_currency_for_concept(
        gaap: dict, concept: Concept, cutoff_year: int, valid_ends: Optional[set] = None
    ) -> str:
        """
        Find the currency with the most annual points across all candidates.
        USD wins ties — domestic companies that also report in USD supplementally
        always stay on USD.
        """
        if concept.unit == "shares":
            return "shares"
        counts: dict[str, int] = {}
        for taxonomy, tag in concept.candidates:
            node = gaap.get(taxonomy, {}).get(tag)
            if not node:
                continue
            for currency, units_list in node.get("units", {}).items():
                if currency == "shares":
                    continue
                pts = EdgarClient._annual_points(units_list, concept.flow, cutoff_year, valid_ends)
                counts[currency] = counts.get(currency, 0) + len(pts)
        if not counts:
            return "USD"
        return max(counts, key=lambda c: (counts[c], 1 if c == "USD" else 0))

    @staticmethod
    def _resolve(facts: dict, concept: Concept, cutoff_year: int, valid_ends: Optional[set] = None) -> list:
        """Resolve each fiscal year against the highest-priority candidate that has data.

        This stitches across tag changes over time. For a given period end, we prefer
        the earlier-listed candidate, and within the same tag we retain the most recently
        filed restatement via _annual_points().

        For monetary concepts the dominant currency is determined first (A3), then
        each candidate is queried in that currency only — ensuring a consistent unit
        across all periods in the resolved series.
        """
        gaap = facts.get("facts", {})

        # Pick the single reporting currency for this concept
        unit = EdgarClient._dominant_currency_for_concept(gaap, concept, cutoff_year, valid_ends)

        selected: dict[str, tuple[int, dict, str]] = {}
        for priority, (taxonomy, tag) in enumerate(concept.candidates):
            node = gaap.get(taxonomy, {}).get(tag)
            if not node:
                continue
            units = node.get("units", {}).get(unit)
            if not units:
                continue
            points = EdgarClient._annual_points(units, concept.flow, cutoff_year, valid_ends)
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
            Fact(concept.key, p["val"], end, int(end[:4]), concept_label, p["form"], p["filed"],
                 currency=unit, accn=p.get("accn"))
            for end, (_, p, concept_label) in sorted(selected.items())
        ]

    @staticmethod
    def _set_reporting_currency(cd: CompanyData) -> None:
        """
        Determine dominant reporting currency across all resolved concepts (A3).
        If concepts resolve in mixed currencies, keep only the dominant currency's
        facts and log the rest as gaps.
        """
        currency_counts: Counter = Counter(
            series[0].currency
            for series in cd.series.values()
            if series and series[0].currency != "shares"
        )
        if not currency_counts:
            return  # shares-only company or no data — leave default "USD"

        dominant = max(currency_counts, key=lambda c: (currency_counts[c], 1 if c == "USD" else 0))
        cd.reporting_currency = dominant

        if len(currency_counts) > 1:
            # Drop off-currency series and log the loss as a gap
            for key in list(cd.series):
                s = cd.series[key]
                if s and s[0].currency not in (dominant, "shares"):
                    cd.unresolved.append(
                        f"{key}: resolved in {s[0].currency} but dominant is {dominant} — dropped"
                    )
                    del cd.series[key]

    # Fiscal-year-end alignment tolerance for instant concepts (Session B.4).
    # Real annual concepts (a flow and an instant for the SAME fiscal year, same
    # 10-K) should describe the same reporting date, but SEC XBRL tagging
    # occasionally introduces a filer-specific off-by-a-day-or-two drift between
    # concepts. 3 days comfortably absorbs that while staying far short of the
    # ~90-day gap to the nearest quarterly snapshot -- the actual thing this
    # tolerance must NOT admit.
    _FYE_TOLERANCE_DAYS = 3

    @staticmethod
    def _valid_annual_ends(revenue_period_ends: list) -> Optional[set]:
        """
        The set of TRUE annual fiscal-year-end dates, derived from the
        already-validated revenue series (Session B.4).

        Revenue is the natural spine: it exists for every operating company
        and its period-ends are already trustworthy by construction (they
        passed _annual_points' 350-380 day duration filter). Instant
        (balance-sheet) concepts have no duration to filter on, so a footnote
        table's off-cycle quarterly snapshot -- tagged under the very same
        annual filing as the real fiscal-year-end -- is otherwise
        indistinguishable from a real annual point (Session B.3: confirmed
        for META's us-gaap:Assets at 2016-03-31/06-30/09-30, all from the
        FY2016 10-K). This anchor set lets _annual_points reject those.

        Returns None when revenue has no annual data at all (a shell company,
        a new listing, or a company reporting under a candidate tag not yet
        seen) -- signaling "alignment can't be determined," not "reject
        everything." Callers must treat None as "admit instants unchanged."
        """
        if not revenue_period_ends:
            return None
        return set(revenue_period_ends)

    @staticmethod
    def _near_any_end(end: str, valid_ends: set, tolerance_days: int) -> bool:
        """True if `end` falls within `tolerance_days` of any date in `valid_ends`."""
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            return False
        for ve in valid_ends:
            try:
                ve_date = date.fromisoformat(ve)
            except ValueError:
                continue
            if abs((end_date - ve_date).days) <= tolerance_days:
                return True
        return False

    @staticmethod
    def _annual_points(units: list, is_flow: bool, cutoff_year: int, valid_ends: Optional[set] = None) -> list:
        """
        Collect one annual data point per fiscal-year-end.

        Flows (income/cash flow) have start+end ~365 days apart.
        Instants (balance sheet) have only a period end.
        Dedupe by period end, preferring the most recently filed value
        (restated figures supersede originals).

        Accepted annual filing forms: 10-K (domestic), 20-F (FPI), 40-F (Canadian FPI).
        Quarterly forms (10-Q, 6-K) and registration statements are excluded.

        `valid_ends` (Session B.4): for instant concepts only, the set of true
        fiscal-year-end dates derived from the revenue series (see
        _valid_annual_ends). An instant point whose `end` is not within
        _FYE_TOLERANCE_DAYS of any anchor is rejected -- it is an off-cycle
        snapshot, not a missing annual value, so it is dropped silently here
        rather than surfaced as a gap. When valid_ends is None (no revenue
        anchors available), instants are admitted exactly as before this
        parameter existed. Ignored entirely for flow concepts, which are
        already protected by the duration filter above.
        """
        by_end: dict[str, dict] = {}
        for u in units:
            form = u.get("form", "")
            if not any(form.startswith(pfx) for pfx in _ANNUAL_FORM_PREFIXES):
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
            elif valid_ends is not None:
                if not EdgarClient._near_any_end(end, valid_ends, EdgarClient._FYE_TOLERANCE_DAYS):
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
            Fact(concept.key, p["val"], end, int(end[:4]), concept_label, p["form"], p["filed"],
                 accn=p.get("accn"))
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
