"""
test_edgar_fact.py — engine.edgar.Fact's filing-lineage fields (Session B.2
PR-A): accn carried from the raw companyfacts point, and source_ref()'s
compact filing-reference format.
"""

from __future__ import annotations

from engine.edgar import Fact


def test_source_ref_includes_form_accession_and_filed_date():
    f = Fact(
        metric="revenue", value=215938000000.0, period_end="2026-01-25", fiscal_year=2026,
        concept="us-gaap:Revenues", form="10-K", filed="2026-02-25",
        accn="0001045810-26-000021",
    )
    assert f.source_ref() == "10-K 0001045810-26-000021 filed 2026-02-25"


def test_source_ref_omits_accession_clause_when_accn_is_none():
    """Absence-is-not-zero: a Fact built without an accession (accn defaults
    to None, never a fabricated empty string) must degrade gracefully — the
    accession clause is omitted entirely, not rendered as a blank/placeholder."""
    f = Fact(
        metric="test", value=1.0, period_end="2020-01-01", fiscal_year=2020,
        concept="us-gaap:Test", form="10-K", filed="2020-02-01",
    )
    assert f.accn is None
    assert f.source_ref() == "10-K filed 2020-02-01"
    assert "None" not in f.source_ref()
