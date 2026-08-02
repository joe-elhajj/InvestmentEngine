"""
Tests for F-6: convertible-debt tag family unmapped in edgar.py.

SHIPPED IN THIS PR (long-term half only): us-gaap:ConvertibleLongTermNotesPayable
is now mapped into `long_term_debt`. It is the ONLY debt tag NOW reports
($1.491B at 2025-12-31), and FLNC's only one from FY2025 ($390.8M); without it
total_debt dead-ends at None and implied growth abstains on a company whose
debt is plainly filed.

DELIBERATELY NOT SHIPPED (short-term half): us-gaap:ConvertibleDebtCurrent.
It is a SUBSET of the total-debt tag us-gaap:LongTermDebt, and _build_year_entry
SUMS the long and short lists, so mapping it double-counts every year a filer
reports both. Confirmed against real PANW data — it reports LongTermDebt and
ConvertibleDebtCurrent with no LongTermDebtNoncurrent/LongTermDebtCurrent/
DebtCurrent at all, so today's total_debt is CORRECT and mapping the tag would
regress it:

    FY      LongTermDebt   ConvertibleDebtCurrent   summed      true
    2021    3.226B         1.558B                   4.784B      3.226B
    2022    3.677B         3.677B                   7.354B      3.677B   (2x)
    2023    1.992B         1.992B                   3.983B      1.992B   (2x)
    2024    —              0.964B                   0.964B      ok
    2025    —              0                        0           ok

The two strict-xfail cases below are the red target for the total_debt
combination PR, which has to learn that `LongTermDebt` already includes the
current portion. When that lands, drop the xfail markers and they become
permanent regression tests — do not delete them to make the finding go away.

These are OFFLINE, SYNTHETIC tests — no network, no live SEC data. The real
NOW/PANW/FLNC facts are reproduced here only as minimal fixtures.

    test_convertible_long_term_maps                  -> GREEN (the shipped fix)
    test_long_term_family_picks_one_not_additive     -> GREEN (pick-one guard)
    test_existing_long_term_debt_still_maps          -> GREEN (regression control)
    test_no_debt_concepts_yields_no_entries          -> GREEN (invariant guard)
    test_convertible_debt_current_zero_preserved     -> XFAIL(strict) tag unmapped
    test_panw_total_debt_double_count_at_combination -> XFAIL(strict) sums subset

RECONCILED AGAINST REAL CODE (edgar.py:167-178), 2026-08-02:
    1. There are no LONG_TERM_DEBT_CONCEPTS / SHORT_TERM_DEBT_CONCEPTS
       constants. The concept lists live in the single `CONCEPTS` dict as
       `Concept` dataclasses: CONCEPTS["long_term_debt"] / ["short_term_debt"],
       each holding an ordered `candidates` tuple of (taxonomy, tag) pairs.
    2. There is no build_debt_series(). The series-builder that iterates
       `candidates` is the staticmethod
           EdgarClient._resolve(facts, concept, cutoff_year, valid_ends=None)
       and it returns a LIST of Fact objects, not a {period_end: value} dict.
       _by_period_end() below adapts it without a truthiness filter (see
       test_convertible_debt_current_zero_preserved for why that matters).
    3. Fixture level is correct — _resolve consumes raw SEC companyfacts JSON
       (facts -> taxonomy -> tag -> units -> currency). One required key was
       missing: `filed`. _annual_points uses u.get("filed", "") to pick the
       most recent restatement, but _resolve then indexes p["filed"] directly
       to build the Fact, so its absence is a KeyError, not a soft default.
    4. Period-end key type is a plain ISO string ("2025-12-31") — Fact.period_end
       is `str`. The original .get()/membership assertions were already right.

    Also relevant: both debt concepts are INSTANTS (flow=False), so the fixture
    needs no `start` key (the 350-380 day duration filter is flow-only), and
    passing valid_ends=None admits every point without fiscal-year-end
    alignment filtering. cutoff_year is set well below the fixture years so the
    `int(end[:4]) < cutoff_year` filter never trims a point under test.

"""

import pytest

from engine.edgar import CONCEPTS, CompanyData, EdgarClient, Fact
from engine.pipeline import derive_annual_series

# Well below every fixture period so the cutoff filter never trims a point.
_CUTOFF_YEAR = 2000


def _companyfacts(concept_values):
    """Minimal SEC companyfacts structure.

    concept_values: {concept_name: [(end_iso, val), ...]} placed under
    facts -> us-gaap -> <concept> -> units -> USD.

    `filed` is required by _resolve (it indexes p["filed"] to build the Fact),
    and is what _annual_points uses to prefer the latest restatement.
    """
    us_gaap = {}
    for concept, points in concept_values.items():
        us_gaap[concept] = {
            "units": {
                "USD": [
                    {"end": end, "val": val, "form": "10-K",
                     "fy": int(end[:4]), "fp": "FY",
                     "filed": f"{int(end[:4]) + 1}-02-15",
                     "accn": "0000000000-00-000000"}
                    for end, val in points
                ]
            }
        }
    return {"facts": {"us-gaap": us_gaap}}


def _by_period_end(facts, concept):
    """Run the real mapper and index its list[Fact] by period_end.

    Deliberately keyed on Fact.value with no truthiness filter — a filed 0.0
    must survive this adapter, or the test below would pass for the wrong
    reason.
    """
    resolved = EdgarClient._resolve(facts, concept, _CUTOFF_YEAR, None)
    return {f.period_end: f.value for f in resolved}


# --- RED: the two new tags must map -----------------------------------------

def test_convertible_long_term_maps():
    """NOW files us-gaap:ConvertibleLongTermNotesPayable = $1.491B at
    2025-12-31 — real, material long-term debt. Currently unmapped, so the
    long_term_debt series is empty and total_debt dead-ends at None. After the
    fix the value must land in the long-term series."""
    facts = _companyfacts({
        "ConvertibleLongTermNotesPayable": [("2025-12-31", 1_491_000_000)],
    })
    series = _by_period_end(facts, CONCEPTS["long_term_debt"])
    assert series.get("2025-12-31") == 1_491_000_000.0


def test_long_term_family_picks_one_not_additive():
    """Within a single Concept, _resolve picks ONE candidate per period_end by
    list priority — it never sums across candidates.

    This is what makes appending ConvertibleLongTermNotesPayable safe: a filer
    reporting BOTH the comprehensive us-gaap:LongTermDebt and the convertible
    slice must resolve to the comprehensive figure alone. If _resolve were
    additive, this same PR would have introduced the exact double-count it
    avoids by leaving ConvertibleDebtCurrent unmapped.

    500M (LongTermDebt, higher priority) — never 800M (sum), never 300M."""
    facts = _companyfacts({
        "LongTermDebt": [("2025-12-31", 500_000_000)],
        "ConvertibleLongTermNotesPayable": [("2025-12-31", 300_000_000)],
    })
    series = _by_period_end(facts, CONCEPTS["long_term_debt"])
    assert series["2025-12-31"] == 500_000_000.0

    # And the winner is recorded for lineage as the comprehensive tag.
    resolved = EdgarClient._resolve(facts, CONCEPTS["long_term_debt"], _CUTOFF_YEAR, None)
    assert resolved[0].concept == "us-gaap:LongTermDebt"


@pytest.mark.xfail(
    strict=True,
    reason="F-6 short-term half: us-gaap:ConvertibleDebtCurrent is a SUBSET of "
           "us-gaap:LongTermDebt and _build_year_entry sums the long+short "
           "lists, so mapping it double-counts (PANW FY2021-23). Blocked on "
           "the total_debt combination PR; unmark this test when that lands.",
)
def test_convertible_debt_current_zero_preserved():
    """PANW files us-gaap:ConvertibleDebtCurrent = 0 at 2025-07-31 after
    repaying $965.6M of converts in FY2025.

    Absence-is-not-zero, load-bearing: the entry must EXIST and equal 0.0.
    A filed 0 is a real answer ('we now carry no convertible current debt'),
    distinct from absence ('we don't know'). A truthiness filter (`if val:`)
    would drop this 0 and reintroduce the bug one layer down — these three
    assertions exist specifically to catch that."""
    facts = _companyfacts({
        "ConvertibleDebtCurrent": [("2025-07-31", 0)],
    })
    series = _by_period_end(facts, CONCEPTS["short_term_debt"])
    assert "2025-07-31" in series           # entry present, not dropped
    assert series["2025-07-31"] is not None  # not coerced to absence
    assert series["2025-07-31"] == 0.0       # the real, filed zero


# --- GREEN: guardrails (must pass BEFORE and AFTER the fix) ------------------

def test_existing_long_term_debt_still_maps():
    """Regression control: an already-mapped tag keeps working, proving the
    fix is additive, not a rewrite of the mapper."""
    facts = _companyfacts({
        "LongTermDebt": [("2024-12-31", 500_000_000)],
    })
    series = _by_period_end(facts, CONCEPTS["long_term_debt"])
    assert series.get("2024-12-31") == 500_000_000.0


def test_no_debt_concepts_yields_no_entries():
    """Invariant guard from the other side: a facts set with no debt concepts
    yields an EMPTY series (so total_debt resolves to None downstream), never a
    defaulted 0. Guards against the new concepts over-matching or a zero-default
    creeping in with the fix."""
    facts = _companyfacts({
        "Revenues": [("2024-12-31", 10_000_000_000)],  # unrelated concept
    })
    series = _by_period_end(facts, CONCEPTS["long_term_debt"])
    assert series == {}


# --- XFAIL(strict): red target for the total_debt combination PR -------------

def _fact(metric, value, period_end, concept):
    return Fact(metric, value, period_end, int(period_end[:4]), concept,
                "10-K", f"{int(period_end[:4]) + 1}-09-15",
                accn="0000000000-00-000000")


@pytest.mark.xfail(
    strict=True,
    reason="F-6 combination level: _build_year_entry sums long_term_debt + "
           "short_term_debt unconditionally, but us-gaap:LongTermDebt already "
           "INCLUDES the current portion, so a co-filed ConvertibleDebtCurrent "
           "is double-counted. Fix belongs in the total_debt combination PR "
           "(key on Fact.concept); unmark this test when that lands.",
)
def test_panw_total_debt_double_count_at_combination():
    """PANW FY2023: the SAME $1.9915B of convertible notes is filed under both
    us-gaap:LongTermDebt (a TOTAL-debt tag, current portion included) and
    us-gaap:ConvertibleDebtCurrent (the current slice of it). True total debt is
    $1.9915B; summing the two lists yields $3.983B — exactly double.

    Deliberately built at the COMBINATION level: the series are constructed
    directly rather than extracted from companyfacts, so the probe isolates
    _build_year_entry's summing and stays red regardless of whether
    ConvertibleDebtCurrent is currently in the concept list. Both Facts carry
    their real `concept` lineage, which is the signal the combination fix needs
    to recognise LongTermDebt as already-total."""
    pe = "2023-07-31"
    cd = CompanyData(
        ticker="PANW", cik="0001327567", name="Palo Alto Networks Inc",
        sic="7372", sic_description="Prepackaged Software",
        series={
            "total_assets": [_fact("total_assets", 14_501_500_000, pe, "us-gaap:Assets")],
            "long_term_debt": [
                _fact("long_term_debt", 1_991_500_000, pe, "us-gaap:LongTermDebt")
            ],
            "short_term_debt": [
                _fact("short_term_debt", 1_991_500_000, pe, "us-gaap:ConvertibleDebtCurrent")
            ],
        },
    )
    annual = derive_annual_series(cd, {})
    assert annual[pe].total_debt == 1_991_500_000.0
