"""
Synthetic-data tests for the deterministic core.

These do NOT hit the network. They build fake EDGAR-shaped data and verify the
math, the peer logic, the DCF, and that a full report renders.
"""

import pytest

from engine.edgar import EdgarClient, CONCEPTS, Concept, CompanyData, Fact
from engine import metrics as M
from engine import peers as P
from engine import valuation as V
from engine.market import Quote
from engine.pipeline import derive
from engine import report as R


# --- 1. EDGAR annual-point extraction (flows + instants, dedup, restatement) ---
def test_edgar_parsing():
    units_flow = [
        {"start": "2021-09-26", "end": "2022-09-24", "val": 394328, "form": "10-K", "filed": "2022-10-28"},
        {"start": "2020-09-27", "end": "2021-09-25", "val": 365817, "form": "10-K", "filed": "2021-10-29"},
        # a quarterly point that must be ignored:
        {"start": "2022-06-26", "end": "2022-09-24", "val": 90146, "form": "10-Q", "filed": "2022-10-28"},
        # a restated prior year filed later (should win over original):
        {"start": "2020-09-27", "end": "2021-09-25", "val": 365000, "form": "10-K", "filed": "2022-10-28"},
    ]
    pts = EdgarClient._annual_points(units_flow, is_flow=True, cutoff_year=2000)
    assert len(pts) == 2, "flow: only annual 10-K points kept"
    assert any(p["val"] == 365000 for p in pts), "flow: restated value wins (latest filed)"

    units_instant = [
        {"end": "2022-09-24", "val": 352755, "form": "10-K", "filed": "2022-10-28"},
        {"end": "2021-09-25", "val": 351002, "form": "10-K", "filed": "2021-10-29"},
    ]
    pts_i = EdgarClient._annual_points(units_instant, is_flow=False, cutoff_year=2000)
    assert len(pts_i) == 2, "instant: both year-ends kept"


def test_edgar_tag_stitching():
    def make_flow_point(year, val):
        return {
            "start": f"{year-1}-12-31",
            "end": f"{year}-12-31",
            "val": val,
            "form": "10-K",
            "filed": f"{year+1}-02-15",
        }

    pts_a = [make_flow_point(y, 100.0 * (1.1 ** (y - 2010))) for y in range(2010, 2019)]
    pts_b = [make_flow_point(y, 100.0 * (1.1 ** (y - 2010))) for y in range(2019, 2024)]
    facts = {
        "facts": {
            "us-gaap": {
                "RevA": {"units": {"USD": pts_a}},
                "RevB": {"units": {"USD": pts_b}},
            }
        }
    }
    concept = Concept("revenue", True, (("us-gaap", "RevA"), ("us-gaap", "RevB")))
    series = EdgarClient._resolve(facts, concept, cutoff_year=2000)
    assert len(series) == 14, "stitch: tag transitions preserve full history"
    assert series[0].concept == "us-gaap:RevA", "stitch: earlier tag wins when available"
    assert series[-1].concept == "us-gaap:RevB", "stitch: later tag used for new years"
    values = [(f.fiscal_year, f.value) for f in series]
    cagr_10 = M.cagr_over(values, 10)
    assert abs(cagr_10.value - 0.10) < 1e-6, "stitch: 10y CAGR spans full stitched series"


def test_period_consistency():
    """Verify that derived balance-sheet figures enforce period consistency."""
    def make_instant(period_end, val):
        return Fact("test", val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", "2026-02-15")

    cd = CompanyData(ticker="PERI", cik="0000000002", name="Period Co",
                     sic="7372", sic_description="Prepackaged Software")
    cd.series = {
        "total_assets": [make_instant("2025-12-31", 1000.0), make_instant("2026-12-31", 1100.0)],
        "cash": [make_instant("2025-12-31", 100.0), make_instant("2026-12-31", 150.0)],
        "short_term_investments": [make_instant("2025-12-31", 50.0)],  # missing 2026
        "long_term_investments": [],
        "long_term_debt": [make_instant("2026-12-31", 200.0)],
        "short_term_debt": [make_instant("2026-12-31", 50.0)],
    }

    quote = Quote("PERI", price=100.0, shares_outstanding=10.0, market_cap=1000.0, source="test")
    cfg = {"valuation": {"assumed_tax_rate": 0.21}}
    res = derive(cd, quote, cfg)

    assert res.derived["liquid_assets"] == 150.0, "period: liquid_assets uses only same-period components"
    assert any("short_term_investments" in g and "2026-12-31" in g for g in res.gaps), \
        "period: gaps report STI period mismatch"
    assert res.derived["total_debt"] == 250.0, "period: total_debt still computes same period"


def test_roic_uses_cash_not_liquid_assets():
    """Regression test: invested_capital must use plain `cash`, not `liquid_assets`."""
    def make_instant(period_end, val):
        return Fact("test", val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", "2026-02-15")

    cd = CompanyData(ticker="ROIC", cik="0000000005", name="Roic Co",
                     sic="7372", sic_description="Prepackaged Software")
    # anchor period 2026-12-31
    cd.series = {
        "total_assets": [make_instant("2026-12-31", 1000.0)],
        "total_equity": [make_instant("2026-12-31", 70.0)],
        "long_term_debt": [make_instant("2026-12-31", 90.0)],
        "short_term_debt": [make_instant("2026-12-31", 0.0)],
        # cash is small
        "cash": [make_instant("2026-12-31", 30.0)],
        # investments make liquid_assets much larger
        "short_term_investments": [make_instant("2026-12-31", 50.0)],
        "long_term_investments": [make_instant("2026-12-31", 60.0)],
        # operating income to derive NOPAT
        "operating_income": [make_instant("2026-12-31", 100.0)],
    }

    quote = Quote("ROIC", price=10.0, shares_outstanding=1.0, market_cap=10.0, source="test")
    cfg = {"valuation": {"assumed_tax_rate": 0.21}}
    res = derive(cd, quote, cfg)

    # invested_capital should be debt + equity - cash = 90 + 70 - 30 = 130
    # NOPAT = operating_income * (1 - tax_rate) = 100 * 0.79 = 79 -> ROIC = 79 / 130
    expected_roic = 79.0 / 130.0
    assert abs(res.ratios["roic"].value - expected_roic) < 1e-9, "roic uses cash not liquid_assets"


def test_quarterly_resolution():
    client = EdgarClient("unit-test@company.com")

    def make_q_point(start, end, val, form, filed):
        return {"start": start, "end": end, "val": val, "form": form, "filed": filed}

    facts = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            make_q_point("2026-01-01", "2026-04-02", 10, "10-Q", "2026-04-15"),
                            make_q_point("2026-01-01", "2026-06-30", 20, "10-Q", "2026-07-15"),
                        ]
                    }
                },
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            make_q_point("2026-01-01", "2026-04-02", 2, "10-Q", "2026-04-15"),
                            make_q_point("2026-01-01", "2026-06-30", 5, "10-Q", "2026-07-15"),
                        ]
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            make_q_point("2026-01-01", "2026-04-02", 1, "10-Q", "2026-04-15"),
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            {"end": "2026-03-31", "val": 100, "form": "10-Q", "filed": "2026-04-15"},
                        ]
                    }
                },
            }
        }
    }

    cd = CompanyData(ticker="QTR", cik="0000000003", name="Quarter Co",
                     sic="7372", sic_description="Prepackaged Software")
    cd.series = {
        "revenue": [Fact("revenue", 100.0, "2025-12-31", 2025, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "10-K", "2026-02-15")]
    }
    client._populate_quarterly(cd, facts, cutoff_year=2000)
    assert cd.quarterly.get("revenue") is not None, "quarterly: proper quarter point selected"
    assert cd.quarterly["revenue"].value == 10, "quarterly: excludes longer YTD 10-Q point"
    assert cd.quarterly["revenue"].period_end == "2026-04-02", "quarterly: uses quarter period end"

    cd2 = CompanyData(ticker="QTR2", cik="0000000004", name="Quarter Co 2",
                      sic="7372", sic_description="Prepackaged Software")
    cd2.series = {
        "revenue": [Fact("revenue", 100.0, "2026-12-31", 2026, "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "10-K", "2027-02-15")]
    }
    client._populate_quarterly(cd2, facts, cutoff_year=2000)
    assert cd2.quarterly == {}, "quarterly: not populated when 10-Q is older than latest 10-K"


# --- 2. metrics ---
def test_metrics():
    assert abs(M.cagr(100, 200, 1).value - 1.0) < 1e-9, "cagr basic"
    assert abs(M.cagr(100, 200, 10).value - 0.0717734) < 1e-5, "cagr 10y ~7.18%"
    assert M.cagr(-5, 200, 5).value is None, "cagr negative endpoint -> None"

    series = [(2017, 100), (2018, 110), (2019, 121), (2020, 133), (2021, 146), (2022, 161)]
    m5 = M.cagr_over(series, 5)
    assert abs(m5.value - 0.10) < 0.01, "cagr_over 5y ~10%"

    assert abs(M.net_margin(20, 100).value - 0.20) < 1e-9, "net margin"
    assert M.pe_ratio(100, -2).value is None, "P/E on negative EPS -> None"
    assert M.roe(50, -10).value is None, "ROE on negative equity -> None"
    assert M.interest_coverage(100, 0).value is None, "interest coverage handles 0 -> None"


# --- 3. peer comp-set + scoring ---
def test_peers():
    candidates = [
        {"ticker": "MSFT", "sic": "7372", "size": 2.5e12},
        {"ticker": "GOOGL", "sic": "7370", "size": 1.7e12},   # same 2-digit family (73)
        {"ticker": "XOM", "sic": "2911", "size": 4.0e11},     # different family -> drop
        {"ticker": "TINY", "sic": "7372", "size": 1.0e9},     # too small -> drop
    ]
    band = {"lower_multiple": 0.25, "upper_multiple": 4.0}
    decisions = P.build_peer_set("7373", 3.0e12, candidates, band, "two_digit", [])
    inc = {d.ticker for d in decisions if d.included}
    assert {"MSFT", "GOOGL"} <= inc, "peers: same SIC-2 + in band included"
    assert "XOM" not in inc, "peers: different SIC family excluded"
    assert "TINY" not in inc, "peers: out-of-band size excluded"

    rs = P.relative_score("net_margin", 0.25, [0.10, 0.15, 0.20, 0.30])
    assert rs.percentile == 75.0, "rel score: percentile computed"
    assert abs(rs.peer_median - 0.175) < 1e-9, "rel score: median computed"


# --- 4. DCF + sensitivity ---
def test_dcf():
    a = {"projection_years": 5, "wacc": 0.09, "terminal_growth": 0.025,
         "fcf_growth": [0.08, 0.07, 0.06, 0.05, 0.04]}
    res = V.two_stage_dcf(100.0, net_debt=50.0, shares=10.0,
                          current_price=80.0, scenario_name="base", assumptions=a)
    assert res.equity_value > 0, "dcf: positive equity value"
    assert res.fair_value_per_share is not None, "dcf: per-share computed"
    assert res.upside_vs_price is not None, "dcf: upside computed"

    bad = dict(a); bad["wacc"] = 0.02; bad["terminal_growth"] = 0.03
    res_bad = V.two_stage_dcf(100.0, 0.0, 10.0, 80.0, "bad", bad)
    assert len(res_bad.warnings) > 0, "dcf: warns when wacc <= term growth"

    grid = V.sensitivity_grid(100.0, 50.0, 10.0, a, [0.08, 0.09], [0.02, 0.03])
    assert len(grid) == 2 and len(grid[0.08]) == 2, "dcf: sensitivity grid shape"
    assert grid[0.08][0.03] > grid[0.09][0.02], "dcf: lower wacc -> higher value"


# --- 5. full pipeline + report render on a synthetic company ---
def _fake_company():
    cd = CompanyData(ticker="TEST", cik="0000000001", name="Test Co",
                     sic="7372", sic_description="Prepackaged Software")

    def facts(metric, vals):
        # vals: list of (year, value), oldest first
        return [Fact(metric, v, f"{y}-12-31", y, f"us-gaap:{metric}", "10-K", f"{y+1}-02-15")
                for y, v in vals]

    years = list(range(2017, 2023))
    cd.series = {
        "revenue": facts("Revenues", [(y, 1000 * (1.1 ** (y - 2017))) for y in years]),
        "net_income": facts("NetIncomeLoss", [(y, 200 * (1.1 ** (y - 2017))) for y in years]),
        "operating_income": facts("OperatingIncomeLoss", [(2022, 260)]),
        "gross_profit": facts("GrossProfit", [(2022, 600)]),
        "cfo": facts("NetCash...Operating", [(2022, 300)]),
        "capex": facts("PaymentsToAcquirePPE", [(2022, 60)]),
        "dep_amort": facts("DepreciationAndAmortization", [(2022, 40)]),
        "total_assets": facts("Assets", [(2022, 2000)]),
        "current_assets": facts("AssetsCurrent", [(2022, 800)]),
        "current_liabilities": facts("LiabilitiesCurrent", [(2022, 400)]),
        "total_equity": facts("StockholdersEquity", [(2022, 900)]),
        "cash": facts("CashAndCashEquivalents", [(2022, 300)]),
        "long_term_debt": facts("LongTermDebt", [(2022, 500)]),
        "short_term_debt": facts("DebtCurrent", [(2022, 100)]),
        "interest_expense": facts("InterestExpense", [(2022, 20)]),
    }
    return cd


def test_pipeline_and_report(tmp_path):
    cfg = {
        "valuation": {
            "assumed_tax_rate": 0.21,
            "dcf": {
                "projection_years": 5,
                "scenarios": {
                    "base": {"fcf_growth": [0.08, 0.07, 0.06, 0.05, 0.04],
                             "terminal_growth": 0.025, "wacc": 0.09},
                    "bull": {"fcf_growth": 0.12, "terminal_growth": 0.03, "wacc": 0.08},
                },
                "sensitivity": {"wacc": [0.08, 0.09, 0.10],
                                "terminal_growth": [0.02, 0.025, 0.03]},
            },
        }
    }
    cd = _fake_company()
    quote = Quote("TEST", price=50.0, shares_outstanding=100.0,
                  market_cap=5000.0, source="manual override")
    res = derive(cd, quote, cfg)

    assert abs(res.derived["fcf"] - 240) < 1e-6, "pipeline: fcf = cfo - capex"
    assert res.ratios["net_margin"].value is not None, "pipeline: net margin resolved"
    assert abs(res.growth["revenue"][5].value - 0.10) < 0.01, "pipeline: revenue 5y CAGR ~10%"
    assert "base" in res.dcf, "pipeline: DCF base scenario present"
    assert len(res.sensitivity) == 3, "pipeline: sensitivity grid present"

    peer_table = [P.relative_score("net_margin", res.ratios["net_margin"].value,
                                   [0.15, 0.18, 0.22])]
    md = R.render(res, peer_table)
    assert "Fundamental Analysis" in md, "report: renders header"
    assert "us-gaap:" in md, "report: includes lineage source"
    assert "Data gaps" in md, "report: includes data-gaps section"
    (tmp_path / "reports_sample_TEST.md").write_text(md)


# --- 6. None-propagation: no total_assets (item 2 + item 3) ---
def test_derive_no_total_assets():
    """No exception when total_assets is absent; total_debt is None (not 0.0) with a gap."""
    def make_flow(year, val):
        return Fact("revenue", val, f"{year}-12-31", year, "us-gaap:Revenues", "10-K", f"{year+1}-01-15")

    cd = CompanyData(ticker="NOAS", cik="0000000099", name="No Assets Co",
                     sic="7372", sic_description="Prepackaged Software")
    cd.series = {
        "revenue": [make_flow(2022, 500.0), make_flow(2023, 550.0)],
        "net_income": [make_flow(2022, 50.0)],
    }

    quote = Quote("NOAS", price=10.0, shares_outstanding=10.0, market_cap=100.0, source="test")
    cfg = {"valuation": {"assumed_tax_rate": 0.21}}
    res = derive(cd, quote, cfg)

    assert res.derived.get("total_debt") is None, "total_debt must be None (not 0.0) when anchor absent"
    assert any("total_debt" in g for g in res.gaps), "gap must be logged for total_debt absence"


# --- 7. Stale equity → invested_capital None + gap (item 4) ---
def test_stale_equity_gap():
    """total_equity only at a stale period: must not be used; gap logged; roe is None."""
    def make_instant(period_end, val):
        return Fact("test", val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", "2026-02-15")

    cd = CompanyData(ticker="STALE", cik="0000000020", name="Stale Equity Co",
                     sic="7372", sic_description="Prepackaged Software")
    cd.series = {
        "total_assets": [make_instant("2026-12-31", 1000.0)],   # anchor = 2026-12-31
        "total_equity": [make_instant("2025-12-31", 500.0)],    # only 2025 → stale
        "long_term_debt": [make_instant("2026-12-31", 200.0)],
        "short_term_debt": [make_instant("2026-12-31", 50.0)],
        "cash": [make_instant("2026-12-31", 100.0)],
    }

    quote = Quote("STALE", price=10.0, shares_outstanding=10.0, market_cap=100.0, source="test")
    cfg = {"valuation": {"assumed_tax_rate": 0.21}}
    res = derive(cd, quote, cfg)

    assert res.derived.get("total_equity") is None, "stale equity must not be used"
    assert any("total_equity" in g and "2026-12-31" in g for g in res.gaps), \
        "gap must reference the anchor period that had no equity"
    assert res.ratios["roe"].value is None, "ROE must be None when equity is absent"


# --- 8. Gross profit fallback from revenue - cost_of_revenue (item 6) ---
def test_gross_profit_fallback():
    """When gross_profit absent, derive it from revenue - cost_of_revenue; lineage recorded."""
    def make_fact(metric, year, val, concept):
        return Fact(metric, val, f"{year}-12-31", year, concept, "10-K", f"{year+1}-02-15")

    cd = CompanyData(ticker="GP", cik="0000000030", name="Gross Profit Co",
                     sic="7372", sic_description="Software")
    cd.series = {
        "total_assets": [make_fact("total_assets", 2022, 500.0, "us-gaap:Assets")],
        "revenue":      [make_fact("revenue", 2022, 1000.0, "us-gaap:Revenues")],
        "cost_of_revenue": [make_fact("cost_of_revenue", 2022, 400.0, "us-gaap:CostOfRevenue")],
        # gross_profit deliberately absent
    }

    quote = Quote("GP", price=10.0, shares_outstanding=10.0, market_cap=100.0, source="test")
    cfg = {"valuation": {"assumed_tax_rate": 0.21}}
    res = derive(cd, quote, cfg)

    assert res.derived.get("gross_profit") == 600.0, \
        "fallback gross_profit must equal revenue - cost_of_revenue"
    assert "gross_profit" in res.derived_lineage, "lineage must record the derivation"
    assert "derived" in res.derived_lineage["gross_profit"], "lineage must be labelled 'derived'"
