"""
test_durability_gates.py — PR 4: durability balance-sheet gate (raw-metric veto).

Covers:
  - _evaluate_gate's 5-branch decision tree, IN ORDER (net-cash checked
    before the negative-EBITDA case -- the RKLB-shaped ordering proof)
  - _resolve_gates: malformed/unknown-key/unsupported-metric config is loud
  - D.score() wiring: gated composite is capped (band collapses to the cap
    too), composite_ungated preserved, sub-scores untouched, pass-case is
    byte-identical to a gate-less config
  - config-driven threshold: dead-key prevention via a before/after wiring
    proof at a controlled synthetic ratio
  - extensibility: two gate entries in one list are both iterated; the
    most restrictive (lowest) cap wins when more than one fires
  - rendering: report_html.py fragment GATE/GATE? chip states; screen.py's
    D.gate_status_of() priority and full _process_one wiring
"""

from unittest.mock import MagicMock, patch

import pytest

from engine.edgar import CompanyData, Fact
from engine.market import Quote
from engine.pipeline import derive, YearlyDerived
from engine import durability as D
from engine import report_html as RH
from engine.screen import _process_one

_BASE_CFG = {"valuation": {"assumed_tax_rate": 0.21}}

_GATE_CFG = {
    **_BASE_CFG,
    "durability": {
        "gates": [
            {"id": "balance_sheet_leverage", "metric": "net_debt_ebitda", "threshold": 6.0, "cap": 45.0},
        ],
    },
}


def _instant(metric: str, period_end: str, val: float) -> Fact:
    return Fact(metric, val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", f"{period_end[:4]}-02-15")


def _flow(metric: str, year: int, val: float) -> Fact:
    return Fact(metric, val, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year + 1}-02-15")


def _yd(net_debt, ebitda, period_end="2023-12-31", year=2023) -> YearlyDerived:
    """Minimal YearlyDerived isolating _evaluate_gate's two inputs
    (net_debt, ebitda) -- every other field is None, since the gate never
    reads them."""
    return YearlyDerived(
        period_end=period_end, year=year,
        revenue=None, net_income=None, operating_income=None, gross_profit=None,
        cfo=None, capex=None, dep_amort=None, interest_expense=None, sbc=None, rnd=None,
        total_assets=None, total_equity=None, total_debt=None, liquid_assets=None, cash=None,
        current_assets=None, current_liabilities=None,
        fcf=None, invested_capital=None, nopat=None,
        net_debt=net_debt, ebit=None, ebitda=ebitda, capital_employed=None,
        gross_margin=None, operating_margin=None,
    )


_GATE_ENTRY = {"id": "balance_sheet_leverage", "metric": "net_debt_ebitda", "threshold": 6.0, "cap": 45.0}


# ---------------------------------------------------------------------------
# _evaluate_gate: 5-branch decision tree, in order
# ---------------------------------------------------------------------------

class TestEvaluateGateDecisionTree:
    def test_branch1_net_cash_not_applicable_even_with_negative_ebitda(self):
        """RKLB-shaped case: net cash AND EBITDA <= 0 -- branch 1 (net cash)
        must win over branch 3 (negative-EBITDA gate), proving the required
        check order."""
        annual = {"2023-12-31": _yd(net_debt=-100.0, ebitda=-40.0)}
        assert D._evaluate_gate(_GATE_ENTRY, annual) is None

    def test_branch1_net_cash_not_applicable_positive_ebitda(self):
        annual = {"2023-12-31": _yd(net_debt=-100.0, ebitda=50.0)}
        assert D._evaluate_gate(_GATE_ENTRY, annual) is None

    def test_branch2_untestable_missing_net_debt(self):
        annual = {"2023-12-31": _yd(net_debt=None, ebitda=50.0)}
        outcome = D._evaluate_gate(_GATE_ENTRY, annual)
        assert outcome is not None
        assert outcome.status == "UNTESTABLE"
        assert "net_debt" in outcome.gap_text

    def test_branch2_untestable_missing_ebitda(self):
        annual = {"2023-12-31": _yd(net_debt=100.0, ebitda=None)}
        outcome = D._evaluate_gate(_GATE_ENTRY, annual)
        assert outcome is not None
        assert outcome.status == "UNTESTABLE"
        assert "ebitda" in outcome.gap_text

    def test_branch3_gated_positive_net_debt_negative_ebitda(self):
        """Cannot service debt: strictly worse than any high ratio, must
        gate even though nd/ebitda computes negative (which would
        otherwise falsely read as 'below threshold')."""
        annual = {"2023-12-31": _yd(net_debt=100.0, ebitda=-10.0)}
        outcome = D._evaluate_gate(_GATE_ENTRY, annual)
        assert outcome is not None
        assert outcome.status == "GATED"
        assert outcome.cap == 45.0
        assert "EBITDA <= 0" in outcome.reason

    def test_branch4_gated_over_levered(self):
        annual = {"2023-12-31": _yd(net_debt=700.0, ebitda=100.0)}  # ratio 7.0 > 6.0
        outcome = D._evaluate_gate(_GATE_ENTRY, annual)
        assert outcome is not None
        assert outcome.status == "GATED"
        assert outcome.cap == 45.0
        assert "7.0" in outcome.reason and "6" in outcome.reason

    def test_branch5_pass_under_threshold(self):
        annual = {"2023-12-31": _yd(net_debt=300.0, ebitda=100.0)}  # ratio 3.0
        assert D._evaluate_gate(_GATE_ENTRY, annual) is None

    def test_branch5_pass_exactly_at_threshold(self):
        """Fires ABOVE threshold, not at-or-above -- exactly 6.0 must pass."""
        annual = {"2023-12-31": _yd(net_debt=600.0, ebitda=100.0)}  # ratio exactly 6.0
        assert D._evaluate_gate(_GATE_ENTRY, annual) is None

    def test_no_periods_returns_none(self):
        assert D._evaluate_gate(_GATE_ENTRY, {}) is None


# ---------------------------------------------------------------------------
# _resolve_gates: loud on malformed config
# ---------------------------------------------------------------------------

class TestResolveGatesConfig:
    def test_missing_gates_section_is_empty_list(self):
        dcfg = D._resolve_config(_BASE_CFG)
        assert dcfg["gates"] == []

    def test_valid_gate_resolves(self):
        dcfg = D._resolve_config(_GATE_CFG)
        assert dcfg["gates"] == [_GATE_ENTRY]

    def test_missing_required_key_raises(self):
        cfg = {"durability": {"gates": [{"id": "x", "metric": "net_debt_ebitda", "threshold": 6.0}]}}
        with pytest.raises(ValueError, match="malformed"):
            D._resolve_config(cfg)

    def test_unknown_key_raises(self):
        cfg = {"durability": {"gates": [
            {**_GATE_ENTRY, "unexpected_key": 1},
        ]}}
        with pytest.raises(ValueError, match="malformed"):
            D._resolve_config(cfg)

    def test_unsupported_metric_raises(self):
        cfg = {"durability": {"gates": [
            {"id": "x", "metric": "share_count_growth", "threshold": 1.0, "cap": 10.0},
        ]}}
        with pytest.raises(ValueError, match="unsupported metric"):
            D._resolve_config(cfg)

    def test_gates_not_a_list_raises(self):
        cfg = {"durability": {"gates": {"id": "x"}}}
        with pytest.raises(ValueError, match="must be a list"):
            D._resolve_config(cfg)


# ---------------------------------------------------------------------------
# D.score() wiring: cap mechanics, ungated preservation, pass-case identity
# ---------------------------------------------------------------------------

def _strong_company() -> CompanyData:
    cd = CompanyData(ticker="STRNG", cik="1111111111", name="Strong Co",
                      sic="7372", sic_description="Prepackaged Software")
    years = list(range(2018, 2024))
    cd.series = {
        "total_assets":    [_instant("total_assets",    f"{y}-12-31", 1000.0 * (1.15 ** (y - 2018))) for y in years],
        "total_equity":    [_instant("total_equity",    f"{y}-12-31",  800.0 * (1.15 ** (y - 2018))) for y in years],
        "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31",   50.0) for y in years],
        "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",    0.0) for y in years],
        "cash":            [_instant("cash",            f"{y}-12-31",  150.0 * (1.10 ** (y - 2018))) for y in years],
        "revenue":         [_flow("revenue",          y, 500.0 * (1.12 ** (y - 2018))) for y in years],
        "gross_profit":    [_flow("gross_profit",     y, 350.0 * (1.12 ** (y - 2018))) for y in years],
        "operating_income":[_flow("operating_income", y, 150.0 * (1.12 ** (y - 2018))) for y in years],
        "net_income":      [_flow("net_income",       y, 120.0 * (1.12 ** (y - 2018))) for y in years],
        "cfo":             [_flow("cfo",              y, 130.0 * (1.12 ** (y - 2018))) for y in years],
        "capex":           [_flow("capex",             y,  30.0) for y in years],
        "dep_amort":       [_flow("dep_amort",         y,  40.0) for y in years],
    }
    return cd


def _make_res(cd: CompanyData, cfg=_BASE_CFG):
    q = Quote(cd.ticker, price=50.0, shares_outstanding=100.0, market_cap=5000.0, source="test")
    return derive(cd, q, cfg)


def _force_latest_leverage(res, net_debt, ebitda):
    """Mutates the latest YearlyDerived's raw net_debt/ebitda directly --
    precise control over the gate's two inputs without fighting through
    EDGAR-fixture arithmetic to hit an exact ratio."""
    period = sorted(res.annual_series)[-1]
    res.annual_series[period].net_debt = net_debt
    res.annual_series[period].ebitda = ebitda
    return res


def _weak_company() -> CompanyData:
    """Weak fundamentals AND (once mutated below) high leverage -- the
    combination that surfaced the min()-not-override bug during live PR
    verification (AXON at a lowered test threshold: ungated composite
    already below the cap)."""
    cd = CompanyData(ticker="WEAK", cik="2222222222", name="Weak Co",
                      sic="3990", sic_description="Manufacturing")
    years = list(range(2019, 2024))
    cd.series = {
        "total_assets":    [_instant("total_assets",    f"{y}-12-31", 500.0) for y in years],
        "total_equity":    [_instant("total_equity",    f"{y}-12-31", 100.0) for y in years],
        "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31", 300.0) for y in years],
        "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",  50.0) for y in years],
        "cash":            [_instant("cash",            f"{y}-12-31",  20.0) for y in years],
        "revenue":         [_flow("revenue",  y, 200.0) for y in years],
        "operating_income":[_flow("operating_income", y, 5.0)  for y in years],
        "net_income":      [_flow("net_income",  y, -10.0) for y in years],
        "cfo":             [_flow("cfo",  y, -5.0) for y in years],
        "capex":           [_flow("capex", y, 20.0) for y in years],
    }
    return cd


class TestScoreGateWiring:
    def test_gate_never_inflates_an_already_below_cap_composite(self):
        """The min()-not-override bug this PR's live verification caught:
        a company can be over-levered AND already score below the cap on
        its own merits (weak fundamentals, high leverage are correlated,
        not opposed). Gating must disclose (chip/lineage fire) without
        RAISING the composite -- a veto that improves a score is backwards."""
        res = _make_res(_weak_company())
        _force_latest_leverage(res, net_debt=700.0, ebitda=100.0)  # ratio 7.0 > 6.0
        ds_nogate = D.score(res, _BASE_CFG)
        assert ds_nogate.composite < 45.0, "fixture must already score below the cap for this test to be meaningful"

        ds_gated = D.score(res, _GATE_CFG)
        assert ds_gated.gated is True
        assert ds_gated.composite == ds_nogate.composite, \
            "gating must never raise a composite that was already below the cap"
        assert ds_gated.composite_ungated == ds_nogate.composite

    def test_gate_fires_caps_composite_and_band_preserves_ungated(self):
        res = _make_res(_strong_company())
        _force_latest_leverage(res, net_debt=700.0, ebitda=100.0)  # ratio 7.0 > 6.0

        ds_nogate = D.score(res, _BASE_CFG)
        ds_gated = D.score(res, _GATE_CFG)

        assert ds_gated.gated is True
        assert ds_gated.gate_ids == ["balance_sheet_leverage"]
        assert ds_gated.composite == 45.0
        assert ds_gated.composite_low == 45.0
        assert ds_gated.composite_high == 45.0
        assert ds_gated.composite_ungated == ds_nogate.composite, \
            "ungated composite must byte-match the same score computed with no gate configured"
        assert "GATED[balance_sheet_leverage]" in ds_gated.gate_lineage
        assert "7.0" in ds_gated.gate_lineage and "6.0" in ds_gated.gate_lineage
        assert "45.0" in ds_gated.gate_lineage
        assert f"{ds_nogate.composite:.1f}" in ds_gated.gate_lineage

        # Sub-scores render at TRUE values -- the cap never reaches down
        # into categories/sub_scores.
        resilience = ds_gated.categories["balance_sheet_resilience"]
        nd_sub = next(s for s in resilience.sub_scores if s.name == "net_debt_ebitda")
        assert nd_sub.raw == pytest.approx(7.0)
        assert resilience.composite == ds_nogate.categories["balance_sheet_resilience"].composite

    def test_pass_case_byte_identical_to_gate_less_config(self):
        res = _make_res(_strong_company())
        _force_latest_leverage(res, net_debt=200.0, ebitda=100.0)  # ratio 2.0, well under 6.0

        ds_nogate = D.score(res, _BASE_CFG)
        ds_gate = D.score(res, _GATE_CFG)

        assert ds_gate.gated is False
        assert ds_gate.gate_ids == []
        assert ds_gate.gate_lineage == ""
        assert ds_gate.composite == ds_nogate.composite
        assert ds_gate.composite_low == ds_nogate.composite_low
        assert ds_gate.composite_high == ds_nogate.composite_high
        assert ds_gate.composite_ungated == ds_gate.composite

    def test_untestable_when_ebitda_missing_never_caps(self):
        res = _make_res(_strong_company())
        _force_latest_leverage(res, net_debt=200.0, ebitda=None)

        ds_nogate = D.score(res, _BASE_CFG)
        ds_gate = D.score(res, _GATE_CFG)

        assert ds_gate.gated is False
        assert ds_gate.gate_untestable_ids == ["balance_sheet_leverage"]
        assert any(g.startswith("balance_sheet_leverage:") for g in ds_gate.gaps)
        assert ds_gate.composite == ds_nogate.composite, "untestable must never cap"

    def test_not_applicable_when_net_cash_even_with_negative_ebitda(self):
        """RKLB-shaped: net cash, EBITDA<=0 -- gate doesn't run, no chip,
        no cap, no untestable disclosure either (it's not an absence, it's
        a real 'not applicable' verdict)."""
        res = _make_res(_strong_company())
        _force_latest_leverage(res, net_debt=-100.0, ebitda=-40.0)

        ds_nogate = D.score(res, _BASE_CFG)
        ds_gate = D.score(res, _GATE_CFG)

        assert ds_gate.gated is False
        assert ds_gate.gate_untestable_ids == []
        assert ds_gate.composite == ds_nogate.composite

    def test_threshold_is_config_driven_dead_key_prevention(self):
        """Synthetic ratio 5.0 sits strictly between 4.0 and 6.0: threshold
        6.0 must PASS, threshold 4.0 must GATE -- proves the config value
        is live, not decorative."""
        res = _make_res(_strong_company())
        _force_latest_leverage(res, net_debt=500.0, ebitda=100.0)  # ratio 5.0

        cfg_6 = _GATE_CFG
        ds_6 = D.score(res, cfg_6)
        assert ds_6.gated is False

        cfg_4 = {**_BASE_CFG, "durability": {"gates": [
            {"id": "balance_sheet_leverage", "metric": "net_debt_ebitda", "threshold": 4.0, "cap": 45.0},
        ]}}
        ds_4 = D.score(res, cfg_4)
        assert ds_4.gated is True
        assert ds_4.composite == 45.0

    def test_extensibility_two_gate_entries_both_iterated_most_restrictive_wins(self):
        res = _make_res(_strong_company())
        _force_latest_leverage(res, net_debt=800.0, ebitda=100.0)  # ratio 8.0

        cfg_two = {**_BASE_CFG, "durability": {"gates": [
            {"id": "gate_a", "metric": "net_debt_ebitda", "threshold": 6.0, "cap": 45.0},
            {"id": "gate_b", "metric": "net_debt_ebitda", "threshold": 4.0, "cap": 30.0},
        ]}}
        ds = D.score(res, cfg_two)
        assert ds.gated is True
        assert set(ds.gate_ids) == {"gate_a", "gate_b"}, "both list entries must be evaluated, not just the first"
        assert ds.composite == 30.0, "most restrictive (lowest) cap must win when more than one gate fires"

        # Now a ratio that fires ONLY the stricter gate (5.0 is > 4.0 but not > 6.0).
        res2 = _make_res(_strong_company())
        _force_latest_leverage(res2, net_debt=500.0, ebitda=100.0)  # ratio 5.0
        ds2 = D.score(res2, cfg_two)
        assert ds2.gate_ids == ["gate_b"]
        assert ds2.composite == 30.0


# ---------------------------------------------------------------------------
# D.gate_status_of: priority + rendering vocabulary
# ---------------------------------------------------------------------------

class TestGateStatusOf:
    def test_gated_takes_priority(self):
        ds = D.DurabilityScore(
            ticker="X", composite=45.0, composite_low=45.0, composite_high=45.0,
            categories={}, config_hash="h", data_completeness=1.0, is_stable=True, stability_delta=0.0,
            gated=True, gate_ids=["balance_sheet_leverage"], gate_lineage="GATED[...]",
            gate_untestable_ids=["some_other_gate"], gate_untestable_lineage="...",
        )
        status, tooltip = D.gate_status_of(ds)
        assert status == "GATED"
        assert tooltip == "GATED[...]"

    def test_untestable_when_not_gated(self):
        ds = D.DurabilityScore(
            ticker="X", composite=80.0, composite_low=80.0, composite_high=80.0,
            categories={}, config_hash="h", data_completeness=1.0, is_stable=True, stability_delta=0.0,
            gate_untestable_ids=["balance_sheet_leverage"], gate_untestable_lineage="untestable reason",
        )
        status, tooltip = D.gate_status_of(ds)
        assert status == "UNTESTABLE"
        assert tooltip == "untestable reason"

    def test_none_when_neither(self):
        ds = D.DurabilityScore(
            ticker="X", composite=80.0, composite_low=80.0, composite_high=80.0,
            categories={}, config_hash="h", data_completeness=1.0, is_stable=True, stability_delta=0.0,
        )
        status, tooltip = D.gate_status_of(ds)
        assert status is None
        assert tooltip == ""


# ---------------------------------------------------------------------------
# report_html.py fragment rendering
# ---------------------------------------------------------------------------

class TestReportHtmlGateChip:
    def _res(self):
        cd = _strong_company()
        q = Quote(cd.ticker, price=50.0, shares_outstanding=100.0, market_cap=5000.0, source="test")
        return derive(cd, q, _BASE_CFG)

    def test_gated_renders_gate_chip_with_lineage_tooltip(self):
        html = RH.render_fragment(
            self._res(), durability_composite=45.0,
            gate_status="GATED", gate_tooltip="GATED[balance_sheet_leverage]: net_debt/ebitda 7.0 > 6.0 -> composite capped 45.0 (ungated 72.3)",
        )
        assert 'class="gate-chip"' in html
        assert "GATE" in html
        assert "net_debt/ebitda 7.0" in html

    def test_untestable_renders_gate_chip_untestable_variant(self):
        html = RH.render_fragment(
            self._res(), durability_composite=80.0,
            gate_status="UNTESTABLE", gate_tooltip="balance_sheet_leverage: net_debt/EBITDA gate untestable — ebitda unavailable",
        )
        assert "gate-chip-untestable" in html
        assert "GATE?" in html

    def test_no_gate_status_renders_no_chip(self):
        html = RH.render_fragment(self._res(), durability_composite=80.0)
        assert "gate-chip" not in html


# ---------------------------------------------------------------------------
# screen.py: full _process_one wiring
# ---------------------------------------------------------------------------

def _leverage_company(ticker: str, cik: str, *, long_term_debt: float, cash: float) -> CompanyData:
    """oi=-50, dep_amort=10 -> ebitda=-40 (<=0) in every case -- net_debt
    sign controlled purely by the long_term_debt/cash split (same shape as
    test_durability.py's _ebitda_domain_company)."""
    cd = CompanyData(ticker=ticker, cik=cik, name=f"{ticker} Co",
                      sic="7372", sic_description="Prepackaged Software")
    cd.series = {
        "total_assets":     [_instant("total_assets",     "2023-12-31", 500.0)],
        "total_equity":     [_instant("total_equity",      "2023-12-31", 200.0)],
        "long_term_debt":   [_instant("long_term_debt",    "2023-12-31", long_term_debt)],
        "short_term_debt":  [_instant("short_term_debt",   "2023-12-31", 0.0)],
        "cash":             [_instant("cash",              "2023-12-31", cash)],
        "revenue":          [_flow("revenue",          2023, 200.0)],
        "operating_income": [_flow("operating_income", 2023, -50.0)],
        "net_income":       [_flow("net_income",        2023, -60.0)],
        "cfo":              [_flow("cfo",               2023, -40.0)],
        "capex":            [_flow("capex",              2023,  10.0)],
        "dep_amort":        [_flow("dep_amort",          2023,  10.0)],
    }
    cd.recent_forms = ["10-K"]
    return cd


class TestScreenRowGateWiring:
    def test_screenrow_gated_via_negative_ebitda_rule(self):
        cd = _leverage_company("GATEROW", "0000000096", long_term_debt=300.0, cash=20.0)  # net_debt=280>0, ebitda=-40
        quote = Quote("GATEROW", price=10.0, shares_outstanding=10.0, market_cap=100.0, source="test")
        mock_client = MagicMock()
        mock_client.get_company.return_value = cd
        with patch("engine.screen.get_quote", return_value=quote):
            row, etf_row = _process_one("GATEROW", mock_client, _GATE_CFG, history_years=15)
        assert etf_row is None
        assert row is not None
        assert row.gate_status == "GATED"
        assert "EBITDA <= 0" in row.gate_tooltip
        # This fixture's ungated composite is already well below the 45.0
        # cap (weak fundamentals AND high leverage) -- min() semantics mean
        # the cap doesn't move the number, but the chip/disclosure still
        # fire on the raw-metric condition regardless.
        assert row.composite == row.composite_ungated
        assert row.composite < 45.0

    def test_screenrow_not_applicable_net_cash_rklb_shaped(self):
        cd = _leverage_company("NETCASHROW", "0000000097", long_term_debt=0.0, cash=200.0)  # net_debt<0
        quote = Quote("NETCASHROW", price=10.0, shares_outstanding=10.0, market_cap=100.0, source="test")
        mock_client = MagicMock()
        mock_client.get_company.return_value = cd
        with patch("engine.screen.get_quote", return_value=quote):
            row, etf_row = _process_one("NETCASHROW", mock_client, _GATE_CFG, history_years=15)
        assert row is not None
        assert row.gate_status is None
        assert row.composite == row.composite_ungated
