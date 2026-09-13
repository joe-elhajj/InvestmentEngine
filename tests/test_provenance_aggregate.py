"""
tests/test_provenance_aggregate.py — feature/provenance-and-lighter-accent,
Part 1 red/green suite.

Recon (prior turn): MKT/WIN/REV/INH/DUR are all currently rendered as up
to 5 separate small per-cell badges in frontend/app.js's renderEquitiesRow
(hasMkt/hasWin/hasRev/appendInheritChip/appendDurGapsIndicator) -- with no
JS test framework in this repo to assert against that rendering directly.
This PR moves the AGGREGATION LOGIC (which of the 6 provenance notes are
active, and their row-specific detail) into engine/screen.py::
_provenance_notes(), a pure function ported 1:1 from app.js's existing
conditions -- so the decision is testable, and app.js becomes a thin
renderer of row.provenance_notes.

feature/chip-legend (follow-up): _provenance_notes()' detail field is
now ROW-SPECIFIC ONLY (None for MKT/REV/INH/DUR, which never vary by
row) -- the generic "what does this code mean" definitions that used to
live inline in each detail string moved OUT, to a legend modal, sourced
from docs/assumptions.md's badge vocabulary. One place, not two copies.

Verdict chips (FRAG/GATE/GATE?/FRAG?) are NOT provenance and are not
touched by _provenance_notes at all -- there is no shared code path, so
there is nothing to test here for them; their own existing tests
(gate_status_of, _gap_band_columns) are untouched and still pass.
"""
from __future__ import annotations

from engine.edgar import CompanyData, Fact
from engine.market import Quote
from engine.pipeline import derive, RndRegime
from engine.screen import _provenance_notes, _rnd_unadj_note

_BASE_CFG = {"valuation": {"assumed_tax_rate": 0.21}}


def _instant(metric, period_end, val):
    return Fact(metric, val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", f"{period_end[:4]}-02-15")


def _flow(metric, year, val):
    return Fact(metric, val, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year + 1}-02-15")


# ---------------------------------------------------------------------------
# _provenance_notes() -- pure aggregation, no I/O
# ---------------------------------------------------------------------------

def test_row_with_three_notes_yields_three_entries_with_correct_codes():
    notes = _provenance_notes(
        quote_source="yfinance",
        diluted_shares_gap=True,           # -> MKT
        delivered_growth_label="window: 14y actual vs 5y requested",  # -> WIN
        implied_growth_note="",            # not gated -> INH eligible
        durability_gaps=[],
        rnd_unadj_reason=None,
    )
    codes = [c for c, _ in notes]
    # MKT + WIN present and gap not gated -> INH also fires (inherits from MKT/WIN)
    assert codes == ["MKT", "WIN", "INH"], f"expected MKT, WIN, INH in order, got: {codes}"
    assert len(notes) == 3


def test_row_specific_detail_only_where_it_varies_by_row():
    """feature/chip-legend: detail is None (bare code) for MKT/INH -- they
    never carry row-specific information, only the generic definition
    now living solely in the legend. WIN keeps its row-specific window
    years as a SHORT fragment (no more "delivered window ... requested"
    sentence -- that generic framing moved to the legend too)."""
    notes = _provenance_notes(
        quote_source="yfinance",
        diluted_shares_gap=True,
        delivered_growth_label="window: 14y actual vs 5y requested",
        implied_growth_note="",
        durability_gaps=[],
        rnd_unadj_reason=None,
    )
    by_code = dict(notes)
    assert by_code["MKT"] is None
    assert by_code["WIN"] == "14y vs 5y"
    assert by_code["INH"] is None


def test_row_with_zero_notes_yields_empty_list():
    notes = _provenance_notes(
        quote_source="edgar",
        diluted_shares_gap=False,
        delivered_growth_label="",
        implied_growth_note="",
        durability_gaps=[],
        rnd_unadj_reason=None,
    )
    assert notes == [], f"expected zero provenance notes, got: {notes}"


def test_none_safety_no_phantom_entries_no_crash():
    """None/absent inputs across the board must never become an empty-
    string entry or inflate the count -- absence of caveats is absence
    of chip, not a chip with blank content."""
    notes = _provenance_notes(
        quote_source="",
        diluted_shares_gap=False,
        delivered_growth_label=None,
        implied_growth_note=None,
        durability_gaps=None,
        rnd_unadj_reason=None,
    )
    assert notes == []


def test_rev_and_dur_and_rnd_all_independently_fire():
    notes = _provenance_notes(
        quote_source="edgar",
        diluted_shares_gap=False,
        delivered_growth_label="revenue CAGR (FCF history non-positive or unavailable)",
        implied_growth_note="n/a — some reason",  # gated -> INH does NOT fire even though REV present
        durability_gaps=["net-cash resilience disclosure"],
        rnd_unadj_reason="IFRS filer",  # _rnd_unadj_note()'s own (now-shortened) return shape
    )
    codes = [c for c, _ in notes]
    assert codes == ["REV", "DUR", "RND"], f"expected REV, DUR, RND (no INH -- gated), got: {codes}"
    by_code = dict(notes)
    assert by_code["REV"] is None
    assert by_code["DUR"] is None
    assert by_code["RND"] == "IFRS filer", (
        "RND's detail must be the short, row-specific reason only -- the "
        "generic 'R&D capitalization skipped' framing now lives in the "
        "legend, not duplicated here"
    )


def test_gated_row_never_shows_inh_even_with_mkt_and_win_present():
    """INH's own existing condition (see app.js's `if (!gated)` guard) --
    a gated row (implied_growth_note truthy) never gets an INH note,
    because the Gap column itself is already n/a and has nothing to
    inherit into."""
    notes = _provenance_notes(
        quote_source="yfinance",
        diluted_shares_gap=True,
        delivered_growth_label="window: 14y actual vs 5y requested",
        implied_growth_note="n/a — bracket upper hit",
        durability_gaps=[],
        rnd_unadj_reason=None,
    )
    codes = [c for c, _ in notes]
    assert "INH" not in codes
    assert codes == ["MKT", "WIN"]


# ---------------------------------------------------------------------------
# _rnd_unadj_note() -- row-level R&D-UNADJ, new signal (didn't exist before)
# ---------------------------------------------------------------------------

def _fpi_with_rnd(price=80.0) -> tuple:
    cd = CompanyData(ticker="FPITEST", cik="9999999999", name="Foreign Test Co",
                      sic="3674", sic_description="Semiconductors")
    cd.recent_forms = ["20-F", "20-F", "20-F"]
    years = list(range(2016, 2026))
    cd.series = {
        "total_assets":    [_instant("total_assets",    f"{y}-12-31", 5000.0 * (1.08 ** (y - 2016))) for y in years],
        "total_equity":    [_instant("total_equity",    f"{y}-12-31", 2500.0 * (1.08 ** (y - 2016))) for y in years],
        "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31", 1800.0) for y in years],
        "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",  200.0) for y in years],
        "cash":            [_instant("cash",            f"{y}-12-31",  300.0) for y in years],
        "revenue":         [_flow("revenue",          y, 3000.0 * (1.10 ** (y - 2016))) for y in years],
        "gross_profit":    [_flow("gross_profit",     y, 1800.0 * (1.10 ** (y - 2016))) for y in years],
        "operating_income":[_flow("operating_income", y,  600.0 * (1.10 ** (y - 2016))) for y in years],
        "net_income":      [_flow("net_income",       y,  450.0 * (1.10 ** (y - 2016))) for y in years],
        "cfo":             [_flow("cfo",              y,  550.0 * (1.10 ** (y - 2016))) for y in years],
        "capex":           [_flow("capex",             y,  100.0) for y in years],
        "dep_amort":       [_flow("dep_amort",         y,  120.0) for y in years],
        "rnd":             [_flow("rnd",               y,  400.0 * (1.10 ** (y - 2016))) for y in years],
    }
    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": True}}}
    q = Quote(cd.ticker, price=price, shares_outstanding=100.0, market_cap=price * 100.0, source="test")
    return derive(cd, q, cfg)


def test_rnd_unadj_note_fires_for_fpi_with_usable_rnd():
    res = _fpi_with_rnd()
    assert res.rnd_regime is RndRegime.ABSTAINED_IFRS_FPI
    note = _rnd_unadj_note(res)
    assert note is not None
    assert "ifrs" in note.lower()


def test_rnd_unadj_note_is_none_for_no_rnd_company():
    """Same T1/T3 invariant as fix/f14-rnd-disclosure: a company with NO
    R&D data at all gets no note -- nothing was ever adjustable, so
    there's nothing to disclose an abstention FROM."""
    cd = CompanyData(ticker="NORAND", cik="3000000001", name="No R&D Co",
                      sic="7372", sic_description="Prepackaged Software")
    years = list(range(2018, 2024))
    cd.series = {
        "total_assets": [_instant("total_assets", f"{y}-12-31", 1000.0) for y in years],
        "total_equity": [_instant("total_equity", f"{y}-12-31",  800.0) for y in years],
        "revenue":      [_flow("revenue",      y,  500.0) for y in years],
        "cfo":          [_flow("cfo",          y,  100.0) for y in years],
        "capex":        [_flow("capex",        y,   10.0) for y in years],
    }
    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": True}}}
    q = Quote(cd.ticker, price=50.0, shares_outstanding=100.0, market_cap=5000.0, source="test")
    res = derive(cd, q, cfg)
    assert _rnd_unadj_note(res) is None


def test_rnd_unadj_note_is_none_when_regime_applies():
    """A domestic filer with usable R&D and the regime live -- adjustment
    actually happened, nothing to disclose."""
    cd = CompanyData(ticker="DOMRND", cik="4000000002", name="Domestic R&D Co",
                      sic="3674", sic_description="Semiconductors")
    years = list(range(2016, 2026))
    cd.series = {
        "total_assets":    [_instant("total_assets",    f"{y}-12-31", 5000.0) for y in years],
        "total_equity":    [_instant("total_equity",    f"{y}-12-31", 2500.0) for y in years],
        "revenue":         [_flow("revenue",          y, 3000.0) for y in years],
        "cfo":             [_flow("cfo",              y,  550.0) for y in years],
        "capex":           [_flow("capex",             y,  100.0) for y in years],
        "rnd":             [_flow("rnd",               y,  400.0) for y in years],
    }
    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": True}}}
    q = Quote(cd.ticker, price=80.0, shares_outstanding=100.0, market_cap=8000.0, source="test")
    res = derive(cd, q, cfg)
    assert res.rnd_regime is RndRegime.APPLIES
    assert _rnd_unadj_note(res) is None
