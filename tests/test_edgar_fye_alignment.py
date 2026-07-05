"""
test_edgar_fye_alignment.py — Session B.4 PR-1: fiscal-year-end alignment for
instant (balance-sheet) concepts.

_annual_points()'s duration filter only ever protected flow concepts (income
statement / cash flow, which have start+end ~365 days apart). Instant concepts
have no duration to filter on, so an off-cycle balance-sheet snapshot embedded
in a 10-K's footnote tables (e.g. a "Selected Quarterly Financial Data" table)
was previously indistinguishable from a real fiscal-year-end (Session B.3:
confirmed for META's us-gaap:Assets at 2016-03-31/06-30/09-30, all tagged
under the FY2016 10-K itself). These tests are raw-JSON-shaped, mirroring the
real companyfacts structure, not calling the network.
"""

from __future__ import annotations

from engine.edgar import EdgarClient, Concept


def _flow_point(end: str, start: str, val: float = 100.0, form: str = "10-K", filed: str | None = None):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed or f"{end[:4]}-02-15"}


def _instant_point(end: str, val: float = 100.0, form: str = "10-K", filed: str | None = None):
    return {"end": end, "val": val, "form": form, "filed": filed or f"{end[:4]}-02-15"}


# ---------------------------------------------------------------------------
# _valid_annual_ends
# ---------------------------------------------------------------------------

def test_valid_annual_ends_none_when_no_revenue_history():
    """No-flow-anchors edge case: alignment can't be determined, not 'reject everything'."""
    assert EdgarClient._valid_annual_ends([]) is None


def test_valid_annual_ends_returns_set_of_period_ends():
    assert EdgarClient._valid_annual_ends(["2022-12-31", "2023-12-31"]) == {"2022-12-31", "2023-12-31"}


# ---------------------------------------------------------------------------
# _near_any_end
# ---------------------------------------------------------------------------

def test_near_any_end_exact_match():
    assert EdgarClient._near_any_end("2023-12-31", {"2023-12-31"}, 3) is True


def test_near_any_end_within_tolerance():
    assert EdgarClient._near_any_end("2023-01-29", {"2023-01-27"}, 3) is True


def test_near_any_end_outside_tolerance():
    assert EdgarClient._near_any_end("2023-09-30", {"2023-12-31"}, 3) is False


def test_near_any_end_invalid_date_string_is_not_near_anything():
    assert EdgarClient._near_any_end("not-a-date", {"2023-12-31"}, 3) is False


# ---------------------------------------------------------------------------
# _annual_points — the instant alignment gate
# ---------------------------------------------------------------------------

def test_meta_shape_off_cycle_quarterly_snapshots_rejected():
    """The exact real-world shape: four Assets points from the same 10-K, one
    real fiscal-year-end (12-31) and three off-cycle footnote snapshots
    (03-31, 06-30, 09-30). Only the real one survives when revenue's
    period-ends (the anchor set) contain just 2016-12-31."""
    units = [
        _instant_point("2016-03-31", 52262000000, filed="2017-02-03"),
        _instant_point("2016-06-30", 55968000000, filed="2017-02-03"),
        _instant_point("2016-09-30", 60007000000, filed="2017-02-03"),
        _instant_point("2016-12-31", 64961000000, filed="2017-02-03"),
    ]
    valid_ends = {"2016-12-31"}
    pts = EdgarClient._annual_points(units, is_flow=False, cutoff_year=2000, valid_ends=valid_ends)
    assert [p["end"] for p in pts] == ["2016-12-31"]


def test_drift_shape_instant_within_tolerance_admitted():
    """52/53-week fiscal calendars drift a few days year to year (NVDA's
    late-January FYE, e.g. 2023-01-29 -> 2024-01-28). An instant tagged 2 days
    off the revenue anchor must still be admitted."""
    units = [_instant_point("2023-01-31", 1000.0)]  # 2 days after the anchor
    valid_ends = {"2023-01-29"}
    pts = EdgarClient._annual_points(units, is_flow=False, cutoff_year=2000, valid_ends=valid_ends)
    assert [p["end"] for p in pts] == ["2023-01-31"]


def test_quarterly_shape_instant_far_from_anchor_rejected():
    """An instant ~90 days from the nearest anchor (a real quarterly snapshot,
    not FYE drift) must be rejected."""
    units = [_instant_point("2023-09-30", 1000.0)]  # ~91 days before 2023-12-31
    valid_ends = {"2023-12-31"}
    pts = EdgarClient._annual_points(units, is_flow=False, cutoff_year=2000, valid_ends=valid_ends)
    assert pts == []


def test_no_anchors_admits_instants_unchanged():
    """valid_ends=None (no revenue history to derive anchors from) must fall
    back to admitting instants exactly as before this feature existed —
    alignment can't be checked, so nothing is rejected on that basis."""
    units = [
        _instant_point("2016-03-31", 1.0),
        _instant_point("2016-12-31", 2.0),
    ]
    pts = EdgarClient._annual_points(units, is_flow=False, cutoff_year=2000, valid_ends=None)
    assert {p["end"] for p in pts} == {"2016-03-31", "2016-12-31"}


def test_flow_concepts_unaffected_by_valid_ends():
    """valid_ends must be ignored entirely for flow concepts — they are
    already protected by the duration filter, and this PR must not change
    flow selection at all. Use a valid_ends set that would reject every
    point below if (incorrectly) applied, to prove it's never consulted."""
    units = [
        _flow_point("2022-12-31", "2022-01-01", val=100.0),
        _flow_point("2023-12-31", "2023-01-01", val=110.0),
    ]
    valid_ends_that_would_reject_everything = {"1900-01-01"}
    pts_without = EdgarClient._annual_points(units, is_flow=True, cutoff_year=2000, valid_ends=None)
    pts_with = EdgarClient._annual_points(
        units, is_flow=True, cutoff_year=2000, valid_ends=valid_ends_that_would_reject_everything
    )
    assert [p["end"] for p in pts_without] == ["2022-12-31", "2023-12-31"]
    assert pts_with == pts_without


# ---------------------------------------------------------------------------
# _resolve — end-to-end threading (revenue anchors -> instant concept)
# ---------------------------------------------------------------------------

def test_resolve_end_to_end_meta_shape():
    """Full _resolve() path: total_assets has the same four-point META shape,
    revenue has only the real fiscal-year-end. Confirms the anchor set
    computed from a resolved revenue series correctly gates a DIFFERENT
    concept's resolution, matching how get_company() threads it."""
    facts = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [_flow_point("2016-12-31", "2016-01-01", val=27638000000.0)]}
                },
                "Assets": {
                    "units": {
                        "USD": [
                            _instant_point("2016-03-31", 52262000000, filed="2017-02-03"),
                            _instant_point("2016-06-30", 55968000000, filed="2017-02-03"),
                            _instant_point("2016-09-30", 60007000000, filed="2017-02-03"),
                            _instant_point("2016-12-31", 64961000000, filed="2017-02-03"),
                        ]
                    }
                },
            }
        }
    }
    revenue_concept = Concept("revenue", True, (("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),))
    revenue_facts = EdgarClient._resolve(facts, revenue_concept, cutoff_year=2000)
    valid_ends = EdgarClient._valid_annual_ends([f.period_end for f in revenue_facts])
    assert valid_ends == {"2016-12-31"}

    assets_concept = Concept("total_assets", False, (("us-gaap", "Assets"),))
    assets_facts = EdgarClient._resolve(facts, assets_concept, cutoff_year=2000, valid_ends=valid_ends)
    assert [f.period_end for f in assets_facts] == ["2016-12-31"]


def test_resolve_falls_back_unchanged_when_revenue_has_no_history():
    """A company with no revenue history at all (shell/new listing edge case)
    must still resolve instants — no anchors means no alignment check, not
    rejection of every instant point."""
    facts = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            _instant_point("2016-03-31", 1.0),
                            _instant_point("2016-12-31", 2.0),
                        ]
                    }
                },
            }
        }
    }
    valid_ends = EdgarClient._valid_annual_ends([])
    assert valid_ends is None
    assets_concept = Concept("total_assets", False, (("us-gaap", "Assets"),))
    assets_facts = EdgarClient._resolve(facts, assets_concept, cutoff_year=2000, valid_ends=valid_ends)
    assert {f.period_end for f in assets_facts} == {"2016-03-31", "2016-12-31"}
