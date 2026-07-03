"""
test_derived_sums.py — absence-is-not-zero regression tests for every
derived-sum site audited in pipeline.py (Task 1).

Bug: a derived sum of optional components (total_debt = long_term_debt +
short_term_debt, liquid_assets = cash + short_term_investments +
long_term_investments) silently became 0.0 when ALL of its components were
absent, because each component defaulted to 0.0 before summing. Reported
via CAT's 2026-03-31 10-Q, where total_debt rendered as $0 with both
long_term_debt and short_term_debt genuinely unreported for the quarter.

Rule being tested everywhere below: a derived sum is None only when NONE of
its components resolve; when at least one resolves, sum the resolved ones
and log the missing ones as gaps — never silently substitute 0 for an
absent component.
"""

from __future__ import annotations

from engine.edgar import CompanyData, Fact
from engine.market import Quote
from engine.pipeline import derive, derive_annual_series

_CFG = {"valuation": {"assumed_tax_rate": 0.21}}


def _instant(metric: str, period_end: str, val: float) -> Fact:
    return Fact(metric, val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", "2026-02-15")


def _flow(metric: str, year: int, val: float) -> Fact:
    return Fact(metric, val, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year + 1}-02-15")


def _qfact(metric: str, val: float, period_end: str = "2026-03-31") -> Fact:
    return Fact(metric, val, period_end, 2026, "us-gaap:Test", "10-Q", "2026-04-20")


# ---------------------------------------------------------------------------
# Annual path (_build_year_entry / derive_annual_series)
# ---------------------------------------------------------------------------

class TestAnnualLiquidAssets:
    def test_all_components_absent_is_none_not_zero(self):
        cd = CompanyData(ticker="ANN3", cik="0000000101", name="Annual Three",
                          sic="7372", sic_description="Software")
        cd.series = {
            "total_assets": [_instant("total_assets", "2026-12-31", 1000.0)],
            "revenue": [_flow("revenue", 2026, 400.0)],
            # no cash / short_term_investments / long_term_investments at all
        }
        annual = derive_annual_series(cd, _CFG)
        yd = annual["2026-12-31"]
        assert yd.liquid_assets is None
        assert yd.cash is None

    def test_partial_components_sum_with_gap_for_missing(self):
        cd = CompanyData(ticker="ANN4", cik="0000000102", name="Annual Four",
                          sic="7372", sic_description="Software")
        cd.series = {
            "total_assets": [_instant("total_assets", "2026-12-31", 1000.0)],
            "cash": [_instant("cash", "2026-12-31", 40.0)],
            # short_term_investments / long_term_investments absent
        }
        annual = derive_annual_series(cd, _CFG)
        yd = annual["2026-12-31"]
        assert yd.liquid_assets == 40.0
        assert yd.cash == 40.0


class TestAnnualTotalDebt:
    def test_all_components_absent_is_none_not_zero(self):
        cd = CompanyData(ticker="ANN5", cik="0000000103", name="Annual Five",
                          sic="7372", sic_description="Software")
        cd.series = {
            "total_assets": [_instant("total_assets", "2026-12-31", 1000.0)],
            # no long_term_debt / short_term_debt at all
        }
        annual = derive_annual_series(cd, _CFG)
        assert annual["2026-12-31"].total_debt is None

    def test_partial_components_sum_with_gap_for_missing(self):
        cd = CompanyData(ticker="ANN6", cik="0000000104", name="Annual Six",
                          sic="7372", sic_description="Software")
        cd.series = {
            "total_assets": [_instant("total_assets", "2026-12-31", 1000.0)],
            "long_term_debt": [_instant("long_term_debt", "2026-12-31", 300.0)],
            # short_term_debt reported for a prior period only, not 2026-12-31 —
            # this is the "period mismatch" case that triggers a gap message
            # (a component with no series at all, ever, logs no gap — there is
            # nothing to point to as "the latest available value").
            "short_term_debt": [_instant("short_term_debt", "2025-12-31", 50.0)],
        }
        annual = derive_annual_series(cd, _CFG)
        yd = annual["2026-12-31"]
        assert yd.total_debt == 300.0
        assert any("short_term_debt" in g and "2025-12-31" in g for g in yd.gaps)


class TestAnnualDownstreamComposites:
    """net_debt / invested_capital / capital_employed must not silently
    treat an absent input as 0 — they must propagate None."""

    def _cd(self, **series) -> CompanyData:
        cd = CompanyData(ticker="ANN7", cik="0000000105", name="Annual Seven",
                          sic="7372", sic_description="Software")
        cd.series = {"total_assets": [_instant("total_assets", "2026-12-31", 1000.0)], **series}
        return cd

    def test_net_debt_none_when_total_debt_absent(self):
        cd = self._cd(cash=[_instant("cash", "2026-12-31", 100.0)])
        yd = derive_annual_series(cd, _CFG)["2026-12-31"]
        assert yd.total_debt is None
        assert yd.liquid_assets == 100.0
        assert yd.net_debt is None, "net_debt must be None, not liquid_assets negated"

    def test_net_debt_computes_when_both_sides_resolve(self):
        cd = self._cd(
            long_term_debt=[_instant("long_term_debt", "2026-12-31", 300.0)],
            cash=[_instant("cash", "2026-12-31", 100.0)],
        )
        yd = derive_annual_series(cd, _CFG)["2026-12-31"]
        assert yd.net_debt == 200.0

    def test_invested_capital_none_when_cash_absent(self):
        cd = self._cd(
            total_equity=[_instant("total_equity", "2026-12-31", 500.0)],
            long_term_debt=[_instant("long_term_debt", "2026-12-31", 300.0)],
            # no cash anywhere → cash is None, not 0
        )
        yd = derive_annual_series(cd, _CFG)["2026-12-31"]
        assert yd.cash is None
        assert yd.invested_capital is None, "invested_capital must not treat absent cash as 0"

    def test_capital_employed_none_when_current_liabilities_absent(self):
        cd = self._cd()  # no current_liabilities series at all
        yd = derive_annual_series(cd, _CFG)["2026-12-31"]
        assert yd.current_liabilities is None
        assert yd.capital_employed is None, (
            "capital_employed must not silently treat absent current_liabilities as 0"
        )

    def test_capital_employed_computes_when_present(self):
        cd = self._cd(current_liabilities=[_instant("current_liabilities", "2026-12-31", 150.0)])
        yd = derive_annual_series(cd, _CFG)["2026-12-31"]
        assert yd.capital_employed == 850.0


# ---------------------------------------------------------------------------
# Quarterly path (derive()'s cd.quarterly branch) — the exact reported bug
# ---------------------------------------------------------------------------

def _quote() -> Quote:
    return Quote("CAT", price=300.0, shares_outstanding=500.0, market_cap=150_000.0, source="test")


class TestQuarterlyTotalDebt:
    def test_all_components_absent_is_none_not_zero(self):
        """The exact reported defect: CAT 10-Q, 2026-03-31, both debt components unreported."""
        cd = CompanyData(ticker="CAT", cik="0000018230", name="Caterpillar",
                          sic="3531", sic_description="Construction Machinery")
        cd.quarterly = {
            "revenue": _qfact("revenue", 16000.0),
            # long_term_debt / short_term_debt not reported this quarter
        }
        res = derive(cd, _quote(), _CFG)
        assert res.latest_quarter["total_debt"] is None, (
            "total_debt must be None, not $0, when both debt components are absent"
        )
        assert any(
            "long_term_debt" in g and "short_term_debt" in g and "2026-03-31" in g
            for g in res.gaps
        ), "missing quarterly debt components must be logged as a gap"

    def test_partial_components_sum_with_gap_for_missing(self):
        cd = CompanyData(ticker="CAT", cik="0000018230", name="Caterpillar",
                          sic="3531", sic_description="Construction Machinery")
        cd.quarterly = {
            "revenue": _qfact("revenue", 16000.0),
            "long_term_debt": _qfact("long_term_debt", 25000.0),
            # short_term_debt not reported
        }
        res = derive(cd, _quote(), _CFG)
        assert res.latest_quarter["total_debt"] == 25000.0
        assert any("short_term_debt" in g and "2026-03-31" in g for g in res.gaps)


class TestQuarterlyLiquidAssetsAndCash:
    def test_all_components_absent_is_none_not_zero(self):
        cd = CompanyData(ticker="CAT", cik="0000018230", name="Caterpillar",
                          sic="3531", sic_description="Construction Machinery")
        cd.quarterly = {"revenue": _qfact("revenue", 16000.0)}
        res = derive(cd, _quote(), _CFG)
        assert res.latest_quarter["liquid_assets"] is None
        assert res.latest_quarter["cash"] is None, "standalone cash field must be None, not 0"

    def test_partial_components_sum_with_gap_for_missing(self):
        cd = CompanyData(ticker="CAT", cik="0000018230", name="Caterpillar",
                          sic="3531", sic_description="Construction Machinery")
        cd.quarterly = {
            "revenue": _qfact("revenue", 16000.0),
            "cash": _qfact("cash", 5000.0),
            # short_term_investments / long_term_investments not reported
        }
        res = derive(cd, _quote(), _CFG)
        assert res.latest_quarter["liquid_assets"] == 5000.0
        assert res.latest_quarter["cash"] == 5000.0
        assert any(
            "short_term_investments" in g and "long_term_investments" in g and "2026-03-31" in g
            for g in res.gaps
        )
