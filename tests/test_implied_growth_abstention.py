"""
tests/test_implied_growth_abstention.py — fix/implied-growth-abstention
red/green suite.

Census (read-only, prior session) found: engine/screen.py's
_implied_growth_columns() collapses every implied_growth_result is None
case into ONE guessed reason ("normalized free cash flow is zero or
negative") whenever the reporting currency is USD -- regardless of the
TRUE cause. Confirmed live on the watchlist: NOW has a healthy $4.05B
positive normalized_fcf but net_debt is None (long_term_debt didn't
resolve) -- the dashboard told the user FCF was non-positive, which is
false. SPCX has literally no FCF/revenue data at all -- same wrong
"zero or negative" claim, which implies a number was computed when none
was.

This file is the RED phase for that fix. engine/pipeline.py's derive()
gains ImpliedGrowthAbstainReason, stamped once (same pattern as
RndRegime in fix/f14-rnd-disclosure) so screen.py never re-derives or
guesses the cause.
"""
from __future__ import annotations

import pytest

from engine.edgar import CompanyData, Fact
from engine.market import Quote
from engine.pipeline import derive, ImpliedGrowthAbstainReason
from engine.screen import _gap_bracket_bound, _implied_growth_columns
from engine.valuation import ImpliedGrowthResult

_BASE_CFG = {"valuation": {"assumed_tax_rate": 0.21}}


def _instant(metric, period_end, val):
    return Fact(metric, val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", f"{period_end[:4]}-02-15")


def _flow(metric, year, val):
    return Fact(metric, val, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year + 1}-02-15")


def _make_res(cd, cfg=None, price=100.0, shares=100.0):
    cfg = cfg or _BASE_CFG
    q = Quote(cd.ticker, price=price, shares_outstanding=shares, market_cap=price * shares, source="test")
    return derive(cd, q, cfg)


# ---------------------------------------------------------------------------
# T1 -- NOW-shaped: positive normalized FCF, net_debt=None
# ---------------------------------------------------------------------------

def _now_shaped_company() -> CompanyData:
    """Healthy FCF margin every year, but NO long_term_debt/short_term_debt
    at all -- total_debt (and therefore net_debt) can never resolve,
    exactly NOW's live shape: normalized_fcf is a real positive number,
    net_debt is the actual blocker."""
    cd = CompanyData(ticker="NOWLIKE", cik="1000000001", name="Now-Like Co",
                      sic="7372", sic_description="Prepackaged Software")
    years = list(range(2018, 2024))
    cd.series = {
        "total_assets": [_instant("total_assets", f"{y}-12-31", 5000.0 * (1.1 ** (y - 2018))) for y in years],
        "total_equity": [_instant("total_equity", f"{y}-12-31", 3000.0 * (1.1 ** (y - 2018))) for y in years],
        "cash":         [_instant("cash",         f"{y}-12-31",  500.0) for y in years],
        "revenue":      [_flow("revenue",      y, 2000.0 * (1.2 ** (y - 2018))) for y in years],
        "cfo":          [_flow("cfo",          y,  700.0 * (1.2 ** (y - 2018))) for y in years],
        "capex":        [_flow("capex",        y,   50.0) for y in years],
        "net_income":   [_flow("net_income",   y,  300.0 * (1.2 ** (y - 2018))) for y in years],
        # NO long_term_debt / short_term_debt at all -- total_debt/net_debt stay None
    }
    return cd


def test_t1_now_shaped_note_names_net_debt_not_fcf():
    cd = _now_shaped_company()
    res = _make_res(cd, price=200.0, shares=100.0)

    # Setup checks: confirms this fixture actually reproduces NOW's shape.
    assert res.normalized_fcf is not None and res.normalized_fcf > 0, (
        "setup check: fixture must have a real positive normalized_fcf, like NOW's "
        "actual $4.05B -- otherwise this isn't testing the misattribution bug"
    )
    period = max(res.annual_series)
    assert res.annual_series[period].net_debt is None, (
        "setup check: fixture must have net_debt=None, like NOW's actual long_term_debt gap"
    )
    assert res.implied_growth_result is None

    assert res.implied_growth_abstain_reason is ImpliedGrowthAbstainReason.NET_DEBT_MISSING

    _, _, note = _implied_growth_columns(res, cd)
    assert "net_debt" in note.lower() or "net debt" in note.lower(), (
        f"note must name net_debt as the cause, got: {note!r}"
    )
    assert "zero or negative" not in note and "non-positive" not in note.lower(), (
        f"note must NOT claim FCF is non-positive when normalized_fcf is a real "
        f"positive ${res.normalized_fcf:,.0f} -- current (wrong) note text is: {note!r}"
    )


# ---------------------------------------------------------------------------
# T2 -- SPCX-shaped: no FCF/revenue pairs at all
# ---------------------------------------------------------------------------

def _spcx_shaped_company() -> CompanyData:
    """Total data desert: no cfo/capex/revenue at all -- normalized_fcf's
    _normalized_fcf() hits its `not pairs` branch, not a computed-and-
    rejected margin. Exactly SPCX's live shape."""
    cd = CompanyData(ticker="SPCXLIKE", cik="1000000002", name="SpcxLike Co",
                      sic="3760", sic_description="Guided Missiles")
    years = list(range(2022, 2024))
    cd.series = {
        "total_assets": [_instant("total_assets", f"{y}-12-31", 1000.0) for y in years],
        # No revenue, cfo, capex, cash, debt -- nothing else resolves either.
    }
    return cd


def test_t2_spcx_shaped_note_says_no_data_not_zero_or_negative():
    cd = _spcx_shaped_company()
    res = _make_res(cd, price=50.0, shares=100.0)

    assert res.normalized_fcf is None
    assert res.implied_growth_result is None
    assert res.implied_growth_abstain_reason is ImpliedGrowthAbstainReason.NO_DATA

    _, _, note = _implied_growth_columns(res, cd)
    assert "zero or negative" not in note, (
        f"'zero or negative' implies a margin was computed and found non-positive -- "
        f"SPCX has NO FCF/revenue pairs to compute a margin from at all. "
        f"Current (wrong) note text is: {note!r}"
    )
    assert "no data" in note.lower() or "no fcf" in note.lower(), (
        f"note must say no data was available, got: {note!r}"
    )


# ---------------------------------------------------------------------------
# T3 -- AXON-shaped: bracket_hit=True, upper -- a REAL result, not absence
# ---------------------------------------------------------------------------

def _bracket_hit_igr(bound="upper") -> ImpliedGrowthResult:
    growth = 0.60 if bound == "upper" else -0.20
    return ImpliedGrowthResult(
        implied_growth=growth, bracket_hit=True, bracket_bound=bound,
        normalized_fcf=100.0, assumptions={}, lineage=f"bracket {bound} hit: test",
    )


class _FakeAnalysisResultForBracket:
    """Minimal stand-in exposing only what _gap_bracket_bound reads --
    avoids standing up a full derive() pipeline just to test this one
    pure extraction, same rationale as _implied_growth_columns's own
    existing tests (see TestBracketUpperHitNote in test_screen.py)."""
    def __init__(self, implied_growth_result):
        self.implied_growth_result = implied_growth_result


def test_t3_bracket_hit_upper_yields_a_bracket_bound_not_none():
    res = _FakeAnalysisResultForBracket(_bracket_hit_igr("upper"))
    assert _gap_bracket_bound(res) == "upper", (
        "a bracket-hit result is a REAL computed result (the market price is off-"
        "scale rich even at the model's max growth ceiling), not an absence -- the "
        "Gap cell must be told which bracket edge fired so it can render a distinct "
        "marker instead of blending this into the same n/a bucket as genuine absence"
    )


def test_t3_bracket_hit_lower_yields_a_bracket_bound_not_none():
    res = _FakeAnalysisResultForBracket(_bracket_hit_igr("lower"))
    assert _gap_bracket_bound(res) == "lower"


def test_t3_converged_result_yields_no_bracket_bound():
    igr = ImpliedGrowthResult(
        implied_growth=0.12, bracket_hit=False, bracket_bound=None,
        normalized_fcf=100.0, assumptions={}, lineage="test",
    )
    res = _FakeAnalysisResultForBracket(igr)
    assert _gap_bracket_bound(res) is None


def test_t3_no_result_yields_no_bracket_bound():
    res = _FakeAnalysisResultForBracket(None)
    assert _gap_bracket_bound(res) is None


# ---------------------------------------------------------------------------
# T4 -- derive() gap-logs an "implied_growth: <reason>" entry for every
#        abstention path, one line, correct cause
# ---------------------------------------------------------------------------

def _ig_gap_lines(res) -> list[str]:
    return [g for g in res.gaps if g.startswith("implied_growth:")]


def test_t4_net_debt_missing_gap_logged():
    res = _make_res(_now_shaped_company(), price=200.0, shares=100.0)
    lines = _ig_gap_lines(res)
    assert len(lines) == 1, f"expected exactly one implied_growth: gap entry, got: {lines}"
    assert "net_debt" in lines[0].lower() or "net debt" in lines[0].lower()


def test_t4_no_data_gap_logged():
    res = _make_res(_spcx_shaped_company(), price=50.0, shares=100.0)
    lines = _ig_gap_lines(res)
    assert len(lines) == 1, f"expected exactly one implied_growth: gap entry, got: {lines}"
    assert "no fcf" in lines[0].lower() or "no data" in lines[0].lower()


def test_t4_currency_gated_gap_logged():
    cd = _now_shaped_company()
    cd.reporting_currency = "EUR"
    # give it real debt so the ONLY blocker is currency
    cd.series["long_term_debt"] = [
        _instant("long_term_debt", f"{y}-12-31", 100.0) for y in range(2018, 2024)
    ]
    cd.series["short_term_debt"] = [
        _instant("short_term_debt", f"{y}-12-31", 0.0) for y in range(2018, 2024)
    ]
    res = _make_res(cd, price=200.0, shares=100.0)
    assert res.implied_growth_abstain_reason is ImpliedGrowthAbstainReason.CURRENCY_GATED
    lines = _ig_gap_lines(res)
    assert len(lines) == 1, f"expected exactly one implied_growth: gap entry, got: {lines}"
    assert "currency" in lines[0].lower() or "eur" in lines[0].lower()


def test_t4_fcf_nonpositive_gap_logged():
    cd = _now_shaped_company()
    # Flip cfo negative every year so median FCF margin is <= 0.
    cd.series["cfo"] = [_flow("cfo", y, -700.0) for y in range(2018, 2024)]
    cd.series["long_term_debt"] = [
        _instant("long_term_debt", f"{y}-12-31", 100.0) for y in range(2018, 2024)
    ]
    cd.series["short_term_debt"] = [
        _instant("short_term_debt", f"{y}-12-31", 0.0) for y in range(2018, 2024)
    ]
    res = _make_res(cd, price=200.0, shares=100.0)
    assert res.implied_growth_abstain_reason is ImpliedGrowthAbstainReason.FCF_NONPOSITIVE
    lines = _ig_gap_lines(res)
    assert len(lines) == 1, f"expected exactly one implied_growth: gap entry, got: {lines}"
    assert "margin" in lines[0].lower() or "fcf" in lines[0].lower()


def test_t4_bracket_hit_gap_logged():
    # A company priced so rich the reverse-DCF's +60% ceiling can't justify it.
    cd = _now_shaped_company()
    cd.series["long_term_debt"] = [
        _instant("long_term_debt", f"{y}-12-31", 100.0) for y in range(2018, 2024)
    ]
    cd.series["short_term_debt"] = [
        _instant("short_term_debt", f"{y}-12-31", 0.0) for y in range(2018, 2024)
    ]
    res = _make_res(cd, price=1_000_000.0, shares=100.0)  # absurdly rich price -> bracket hit
    assert res.implied_growth_result is not None
    assert res.implied_growth_result.bracket_hit is True
    lines = _ig_gap_lines(res)
    assert len(lines) == 1, f"expected exactly one implied_growth: gap entry, got: {lines}"
    assert "bracket" in lines[0].lower()


# ---------------------------------------------------------------------------
# Defensive: no fallback branch that guesses
# ---------------------------------------------------------------------------

def test_unrecognized_abstain_reason_never_guesses_a_specific_cause():
    """A directly-constructed AnalysisResult with implied_growth_result=None
    but no abstain_reason stamped (the same "unstamped" shape every non-
    derive() test call site in this repo already produces) must render an
    honest 'unresolved' note, never a specific wrong claim like "FCF is
    zero or negative" when nobody actually determined that."""
    from engine.pipeline import AnalysisResult

    cd = _now_shaped_company()
    res = AnalysisResult(
        company=cd,
        quote=Quote(cd.ticker, price=200.0, shares_outstanding=100.0, market_cap=20000.0, source="test"),
    )
    assert res.implied_growth_result is None
    assert res.implied_growth_abstain_reason is None

    _, _, note = _implied_growth_columns(res, cd)
    assert "unresolved" in note.lower(), f"expected an honest 'unresolved' note, got: {note!r}"
    assert "zero or negative" not in note, (
        f"must never guess a specific wrong cause when none was determined, got: {note!r}"
    )
