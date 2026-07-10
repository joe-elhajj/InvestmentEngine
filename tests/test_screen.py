"""
test_screen.py — tests for engine/screen.py's implied-growth n/a note text.

_implied_growth_columns() is the single source of truth for why implied
growth / expectations gap is n/a — both the dashboard's per-ticker gating
(ScreenRow.implied_growth_note) and the Data Diagnostics Notes column
(folded into ScreenRow.flag) read the exact same string from here, so
these tests exercise the pure function directly rather than standing up
a full mocked EDGAR/quote/durability pipeline just to reach it.
"""

from __future__ import annotations

import dataclasses

from engine.edgar import CompanyData
from engine.market import Quote
from engine.pipeline import AnalysisResult
from engine.screen import _empty_row, _implied_growth_columns
from engine.valuation import ImpliedGrowthResult


def _cd(reporting_currency="USD") -> CompanyData:
    return CompanyData(
        ticker="TEST", cik="0000000001", name="Test Co",
        sic="7372", sic_description="Software",
        reporting_currency=reporting_currency,
    )


def _res(implied_growth_result=None, expectations_gap=None) -> AnalysisResult:
    quote = Quote("TEST", price=100.0, shares_outstanding=10.0, market_cap=1000.0, source="test")
    return AnalysisResult(
        company=_cd(), quote=quote,
        implied_growth_result=implied_growth_result,
        expectations_gap=expectations_gap,
    )


def _igr(bracket_hit=False, bracket_bound=None, implied_growth=0.10) -> ImpliedGrowthResult:
    return ImpliedGrowthResult(
        implied_growth=implied_growth, bracket_hit=bracket_hit, bracket_bound=bracket_bound,
        normalized_fcf=50.0, assumptions={}, lineage="test",
    )


class TestBracketUpperHitNote:
    def test_note_is_the_rewritten_self_explanatory_text(self):
        igr = _igr(bracket_hit=True, bracket_bound="upper", implied_growth=0.60)
        res = _res(implied_growth_result=igr)
        implied_g, gap, note = _implied_growth_columns(res, _cd())
        assert implied_g is None
        assert gap is None
        assert note == (
            "n/a — Reverse-DCF: market implies >60% annual growth, above the "
            "model's +60% solver ceiling — expectations gap not computable "
            "(price is off-scale rich vs. current FCF)."
        )

    def test_old_terse_string_is_gone(self):
        igr = _igr(bracket_hit=True, bracket_bound="upper", implied_growth=0.60)
        res = _res(implied_growth_result=igr)
        _, _, note = _implied_growth_columns(res, _cd())
        assert "bracket upper hit" not in note


class TestFcfNonPositiveNote:
    def test_note_is_the_rewritten_self_explanatory_text(self):
        res = _res(implied_growth_result=None)  # igr is None → FCF path (USD reporter)
        implied_g, gap, note = _implied_growth_columns(res, _cd(reporting_currency="USD"))
        assert implied_g is None
        assert gap is None
        assert note == (
            "n/a — Reverse-DCF: normalized free cash flow is zero or negative, "
            "so implied growth cannot be computed (no positive FCF base to grow "
            "from)."
        )

    def test_old_terse_string_is_gone(self):
        res = _res(implied_growth_result=None)
        _, _, note = _implied_growth_columns(res, _cd(reporting_currency="USD"))
        assert "not meaningful" not in note


class TestUnaffectedPaths:
    """The rewrite must not touch the currency-gate or lower-bracket-hit
    notes, or the clean (no note) path — only the two strings the task
    named."""

    def test_currency_gate_note_unchanged(self):
        res = _res(implied_growth_result=None)
        _, _, note = _implied_growth_columns(res, _cd(reporting_currency="TWD"))
        assert note == "n/a — valuation gated: reporting currency TWD vs USD market data"

    def test_bracket_lower_hit_note_unchanged(self):
        igr = _igr(bracket_hit=True, bracket_bound="lower", implied_growth=-0.20)
        res = _res(implied_growth_result=igr)
        _, _, note = _implied_growth_columns(res, _cd())
        assert note == "n/a — bracket lower hit (implied g < -20%)"

    def test_clean_result_has_no_note(self):
        igr = _igr(bracket_hit=False, implied_growth=0.12)
        res = _res(implied_growth_result=igr, expectations_gap=0.03)
        implied_g, gap, note = _implied_growth_columns(res, _cd())
        assert implied_g == 0.12
        assert gap == 0.03
        assert note == ""


class TestScreenRowName:
    """
    feature/search-by-name: ScreenRow.name is threaded from CompanyData.name
    (EDGAR), already fetched for every row that got as far as an EDGAR
    lookup succeeding -- zero new network calls. None only for rows that
    never resolved a CompanyData at all; must propagate as None, never a
    coerced empty string, through both construction and JSON
    serialization (dataclasses.asdict(), which /api/screen's
    _serialize_screen uses verbatim).
    """

    def test_empty_row_carries_name_through_when_given(self):
        row = _empty_row("TEST", "skipped: some reason", name="Test Co")
        assert row.name == "Test Co"

    def test_empty_row_name_defaults_to_none(self):
        row = _empty_row("TEST", "no EDGAR registrant, not classifiable as fund")
        assert row.name is None

    def test_none_name_survives_asdict_as_none_not_empty_string(self):
        """/api/screen's _serialize_screen calls dataclasses.asdict() verbatim
        -- confirms a None name round-trips as None (renders as "ticker
        alone" in the frontend), never silently coerced to ""."""
        row = _empty_row("TEST", "some reason")
        d = dataclasses.asdict(row)
        assert d["name"] is None

    def test_real_name_survives_asdict(self):
        row = _empty_row("TEST", "some reason", name="Test Co")
        d = dataclasses.asdict(row)
        assert d["name"] == "Test Co"
