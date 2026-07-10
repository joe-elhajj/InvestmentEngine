"""
tests/test_rnd_disclosure.py — PR A (fix F-14) red/green suite.

Session D's audit (audit/session_d/report.md) diagnosed F-14: the FPI
R&D-UNADJ abstention badge never fires for an FPI with a usable R&D
series, because report.py/report_html.py's Site C decides whether to
show a bare adjusted-ROIC percentage BEFORE ever consulting is_fpi() --
full diagnosis in /tmp/f14_diagnosis.md, sections 1-8.

This file is the RED phase for PR A. It also documents, empirically,
why /tmp/f14_diagnosis.md's own "Proposed fix" (a naive one-line
reordering of Site C) is REJECTED: see test_t1 below and its docstring.

Amendment 4a: every assertion here goes through the PUBLIC render
surface of both renderers -- R.render(res) and RH.render(res) -- never
a private producer like RH._ratio_rows(). A private-helper assertion
can pass on a correctly-built row that never actually reaches the
rendered document; that is the PR #52 failure mode, and it is exactly
what this suite exists to rule out for F-14's badge.
"""
import html as html_mod
import re

import pytest

from engine.edgar import CompanyData, Fact, is_fpi, classify_rnd_series
from engine.market import Quote
from engine.pipeline import derive
from engine import report as R
from engine import report_html as RH
from engine import durability as D

_BASE_CFG = {"valuation": {"assumed_tax_rate": 0.21}}


def _instant(metric, period_end, val):
    return Fact(metric, val, period_end, int(period_end[:4]), "us-gaap:Test", "10-K", f"{period_end[:4]}-02-15")


def _flow(metric, year, val):
    return Fact(metric, val, f"{year}-12-31", year, "us-gaap:Test", "10-K", f"{year + 1}-02-15")


def _base_series(years, rnd=False):
    series = {
        "total_assets":    [_instant("total_assets",    f"{y}-12-31", 5000.0 * (1.08 ** (y - years[0]))) for y in years],
        "total_equity":    [_instant("total_equity",    f"{y}-12-31", 2500.0 * (1.08 ** (y - years[0]))) for y in years],
        "long_term_debt":  [_instant("long_term_debt",  f"{y}-12-31", 1800.0) for y in years],
        "short_term_debt": [_instant("short_term_debt", f"{y}-12-31",  200.0) for y in years],
        "cash":            [_instant("cash",            f"{y}-12-31",  300.0) for y in years],
        "revenue":         [_flow("revenue",          y, 3000.0 * (1.10 ** (y - years[0]))) for y in years],
        "gross_profit":    [_flow("gross_profit",     y, 1800.0 * (1.10 ** (y - years[0]))) for y in years],
        "operating_income":[_flow("operating_income", y,  600.0 * (1.10 ** (y - years[0]))) for y in years],
        "net_income":      [_flow("net_income",       y,  450.0 * (1.10 ** (y - years[0]))) for y in years],
        "cfo":             [_flow("cfo",              y,  550.0 * (1.10 ** (y - years[0]))) for y in years],
        "capex":           [_flow("capex",             y,  100.0) for y in years],
        "dep_amort":       [_flow("dep_amort",         y,  120.0) for y in years],
    }
    if rnd:
        series["rnd"] = [_flow("rnd", y, 400.0 * (1.10 ** (y - years[0]))) for y in years]
    return series


def _domestic_no_rnd() -> CompanyData:
    cd = CompanyData(ticker="NORAND", cik="3000000001", name="No R&D Domestic Co",
                      sic="7372", sic_description="Prepackaged Software")
    years = list(range(2018, 2024))
    cd.series = _base_series(years, rnd=False)
    return cd


def _domestic_with_rnd() -> CompanyData:
    cd = CompanyData(ticker="DOMRND", cik="4000000002", name="Domestic R&D Co",
                      sic="3674", sic_description="Semiconductors")
    years = list(range(2016, 2026))
    cd.series = _base_series(years, rnd=True)
    return cd


def _fpi_no_rnd() -> CompanyData:
    cd = CompanyData(ticker="FPINORND", cik="9000000003", name="Foreign No-R&D Co",
                      sic="3674", sic_description="Semiconductors")
    cd.recent_forms = ["20-F", "20-F", "20-F"]
    years = list(range(2018, 2024))
    cd.series = _base_series(years, rnd=False)
    return cd


def _fpi_with_rnd() -> CompanyData:
    cd = CompanyData(ticker="FPITEST", cik="9999999999", name="Foreign Test Co",
                      sic="3674", sic_description="Semiconductors")
    cd.recent_forms = ["20-F", "20-F", "20-F"]
    years = list(range(2016, 2026))
    cd.series = _base_series(years, rnd=True)
    return cd


def _make_res(cd, cfg=None, price=80.0):
    cfg = cfg or _BASE_CFG
    q = Quote(cd.ticker, price=price, shares_outstanding=100.0, market_cap=price * 100.0, source="test")
    return derive(cd, q, cfg)


def _roic_adj_md_line(md: str) -> str | None:
    """The Markdown table row for the R&D-adjusted ROIC line, or None if absent."""
    lines = [line for line in md.splitlines() if "ROIC (R&D-adj)" in line]
    return lines[0] if lines else None


def _roic_adj_html_cell(html: str) -> str | None:
    """
    The raw (still HTML-escaped/markup-bearing) inner HTML of the ROIC
    (R&D-adj) table cell, or None if the row is absent entirely. Callers
    that just need "is the row present at all" (T1/T3) use this directly;
    callers that need the visible text vs. the tooltip separately use
    _roic_adj_html_visible_text / _roic_adj_html_tooltip below.
    """
    m = re.search(r"<td>ROIC \(R&amp;D-adj\)</td><td>(.*?)</td>", html)
    return m.group(1) if m else None


def _roic_adj_html_visible_text(html: str) -> str | None:
    """
    The user-visible text of the ROIC (R&D-adj) cell, unescaped: the
    chip <span>'s inner text when a chip is present (an ABSTAINED
    rnd_regime), or the whole cell's text otherwise (bare percentage /
    the plain "insufficient history" badge, neither of which is a chip).
    None if the row is absent.
    """
    cell = _roic_adj_html_cell(html)
    if cell is None:
        return None
    m = re.search(r"<span[^>]*>(.*?)</span>", cell)
    inner = m.group(1) if m else cell
    return html_mod.unescape(inner)


def _roic_adj_html_tooltip(html: str) -> str | None:
    """
    The ROIC (R&D-adj) chip's title= tooltip text, unescaped, or None if
    there's no chip at all (row absent, bare percentage, or the plain
    "insufficient history" badge -- none of those carry a tooltip).
    """
    cell = _roic_adj_html_cell(html)
    if cell is None:
        return None
    m = re.search(r'title="([^"]*)"', cell)
    return html_mod.unescape(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# T1 -- domestic, NO R&D series -> the row must not exist at all
# ---------------------------------------------------------------------------

def test_t1_domestic_no_rnd_omits_row_entirely():
    """
    Empirical finding (documented here, not asserted as red): CURRENT code
    (engine/report.py's Site C, unmodified) ALREADY omits the "ROIC
    (R&D-adj)" row entirely for a domestic filer with no R&D series --
    confirmed by running this exact fixture against unmodified report.py:
    zero lines contain "ROIC (R&D-adj)". This test therefore PASSES today,
    green, not red -- it is a regression guard on already-correct
    behavior, the same role test_norand_company_shows_zero_rnd_artifacts_
    regime_on plays in tests/session_d_probes/.

    It earns its place in this suite anyway because /tmp/f14_diagnosis.md's
    "Proposed fix" -- literally: call _rnd_unadj_reason(res) first, and
    only fall through to printing the bare percentage if it returns None
    -- silently drops the numeric-availability check when reordering. For
    this exact fixture, reason == None (state == "no_rnd") plus a naive
    "fall through to the bare percentage" step means printing
    _pct(adj.value) where adj.value is None. engine.report._pct(None)
    does NOT raise -- it returns "n/a" -- so the naive inversion would
    NOT crash; it would instead turn today's correctly-empty row into a
    SPURIOUS "| ROIC (R&D-adj) | n/a |" line for a company where nothing
    was ever adjustable. That is why the proposed fix is rejected: it
    regresses a case that already works, trading one bug (F-14) for
    another (a new phantom "n/a" row on every NO_RND company). PR A's
    real fix (Phase 2) must preserve this test's green status.
    """
    cd = _domestic_no_rnd()
    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": True}}}
    res = _make_res(cd, cfg=cfg)

    assert res.ratios.get("roic_adjusted") is None or res.ratios["roic_adjusted"].value is None

    md = R.render(res)
    html = RH.render(res)

    assert _roic_adj_md_line(md) is None, (
        f"expected zero ROIC (R&D-adj) lines in the Markdown report for a NO_RND domestic "
        f"filer, got: {_roic_adj_md_line(md)!r}"
    )
    assert _roic_adj_html_cell(html) is None, (
        f"expected zero ROIC (R&D-adj) rows in the HTML report for a NO_RND domestic "
        f"filer, got: {_roic_adj_html_cell(html)!r}"
    )


# ---------------------------------------------------------------------------
# T2 -- domestic, usable R&D, regime disabled -> badge with a NEW reason,
#        never a bare percentage
# ---------------------------------------------------------------------------

def test_t2_domestic_regime_disabled_shows_config_disabled_reason():
    """
    F-14-adjacent bug, currently RED: engine/pipeline.py's derive() builds
    research_asset/roic_adjusted whenever the R&D window resolves,
    UNCONDITIONALLY regardless of durability.rnd_capitalization.enabled
    (see /tmp/f14_diagnosis.md section 8f) -- and Site C's gate
    (`if adj is not None and adj.value is not None`) never reads the
    enabled flag either. So a domestic filer with usable R&D data gets a
    bare "ROIC (R&D-adj) | NN.N%" row even when the analyst has switched
    the regime OFF in config.yaml -- an assumption meant to be config-
    controlled is silently live in the report regardless of the toggle.

    Desired behavior: when the regime is disabled, the report must show
    an R&D-UNADJ badge with a reason that says so (a config-disabled
    reason, distinct from "IFRS filer" and "insufficient history" -- this
    string does not exist anywhere in the codebase yet), never the bare
    percentage.

    Amendment (fix/rnd-badge-layout): the badge's cell text is the SHORT
    reason only ("regime disabled") -- the full rationale is relocated,
    never shortened away: a footnote line beneath the Markdown ratio
    table, and the HTML chip's title= tooltip. Both are asserted here.
    """
    cd = _domestic_with_rnd()
    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": False}}}
    res = _make_res(cd, cfg=cfg)

    # Confirms the bug exists at the data layer: derive() computed a real
    # adjusted value even though the regime is off.
    adj = res.ratios.get("roic_adjusted")
    assert adj is not None and adj.value is not None, (
        "setup check: derive() should still compute roic_adjusted even with the regime "
        "disabled (that's the root cause T2 is guarding against) -- if this now fails, "
        "the bug has moved and this test needs to be revisited"
    )

    full_reason = "R&D capitalization regime disabled in config"

    md = R.render(res)
    html = RH.render(res)

    md_line = _roic_adj_md_line(md)
    assert md_line, "expected an R&D-UNADJ badge row in the Markdown report, got none"
    assert "R&D-UNADJ" in md_line, f"expected a badge, got a bare value: {md_line!r}"
    assert "%" not in md_line.split("|")[-2], f"must not show a bare percentage: {md_line!r}"
    assert "disabled" in md_line.lower(), (
        f"cell text must disclose 'regime disabled', not blend into an unrelated "
        f"reason: {md_line!r}"
    )
    assert full_reason not in md_line, (
        f"the full rationale must NOT sit inline in the cell (that's the layout bug "
        f"this PR fixes): {md_line!r}"
    )
    assert full_reason in md, (
        "the full rationale must appear as a footnote beneath the ratio table -- "
        "relocated, never shortened away"
    )

    visible = _roic_adj_html_visible_text(html)
    assert visible, "expected an R&D-UNADJ badge in the HTML report, got none"
    assert "R&D-UNADJ" in visible, f"expected a badge, got a bare value: {visible!r}"
    assert "%" not in visible, f"must not show a bare percentage: {visible!r}"
    assert "disabled" in visible.lower(), (
        f"visible chip text must disclose 'regime disabled': {visible!r}"
    )
    assert full_reason not in visible, (
        f"the full rationale must NOT sit in the visible chip text: {visible!r}"
    )

    tooltip = _roic_adj_html_tooltip(html)
    assert tooltip == full_reason, (
        f"the chip's title= tooltip must carry the full rationale verbatim, got: {tooltip!r}"
    )


# ---------------------------------------------------------------------------
# T3 -- FPI, NO R&D series -> must not claim "IFRS filer" as if something
#        was withheld
# ---------------------------------------------------------------------------

def test_t3_fpi_no_rnd_omits_row_entirely():
    """
    Currently RED, and a DIFFERENT bug from F-14 itself: engine.report.
    _rnd_unadj_reason() checks is_fpi() before classify_rnd_series(), so
    it returns "IFRS filer -- pending disposition" for ANY FPI whose
    roic_adjusted didn't compute -- including one with NO R&D data at
    all, where nothing was ever adjustable in the first place. An FPI
    with no R&D data is in the EXACT SAME state a domestic NO_RND filer
    is in (T1), and that filer gets no row at all -- not "not IFRS", but
    genuinely nothing, because there was never anything to disclose an
    abstention FROM. Same assertion shape as T1: zero "ROIC (R&D-adj)"
    lines, in both renderers.
    """
    cd = _fpi_no_rnd()
    assert is_fpi(cd)[0] is True
    state, _ = classify_rnd_series(cd)
    assert state == "no_rnd"

    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": True}}}
    res = _make_res(cd, cfg=cfg)

    md = R.render(res)
    html = RH.render(res)

    assert _roic_adj_md_line(md) is None, (
        f"expected zero ROIC (R&D-adj) lines in the Markdown report for an FPI with NO R&D "
        f"data, got: {_roic_adj_md_line(md)!r}"
    )
    assert _roic_adj_html_cell(html) is None, (
        f"expected zero ROIC (R&D-adj) rows in the HTML report for an FPI with NO R&D "
        f"data, got: {_roic_adj_html_cell(html)!r}"
    )


# ---------------------------------------------------------------------------
# T4 -- D.score()'s FPI-exclusion gate, WITH a control group
# ---------------------------------------------------------------------------

def test_t4_fpi_gate_with_domestic_control():
    """
    Confirms (empirically, per /tmp/f14_diagnosis.md section 8c) that
    D.score()'s FPI-exclusion gate (`use_rnd_adjusted_roic = enabled and
    not ticker_is_fpi`) already makes the R&D regime toggle a no-op for
    an FPI's composite. On its own that half is a vacuous pass: a gate
    that also disabled the toggle FOR EVERYONE would satisfy it too. The
    control group is what makes the test meaningful -- a domestic filer
    with the SAME R&D shape MUST show a different composite across the
    toggle, proving the toggle is actually live and it's specifically the
    FPI exclusion doing the work, not a global no-op.
    """
    cfg_on = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": True}}}
    cfg_off = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": False}}}

    fpi_on = D.score(_make_res(_fpi_with_rnd(), cfg=cfg_on), cfg_on)
    fpi_off = D.score(_make_res(_fpi_with_rnd(), cfg=cfg_off), cfg_off)
    assert fpi_on.composite == fpi_off.composite, (
        "FPI composite must be byte-identical across the regime toggle"
    )

    dom_on = D.score(_make_res(_domestic_with_rnd(), cfg=cfg_on), cfg_on)
    dom_off = D.score(_make_res(_domestic_with_rnd(), cfg=cfg_off), cfg_off)
    assert dom_on.composite != dom_off.composite, (
        "control group failed: a domestic filer with the same R&D shape must show a "
        "DIFFERENT composite across the toggle, or T4's FPI assertion above is vacuous "
        "(it would pass even if the toggle did nothing for anyone)"
    )


# ---------------------------------------------------------------------------
# T5 -- FPI, usable R&D, regime disabled -> precedence: regime_disabled
#        outranks ifrs_fpi
# ---------------------------------------------------------------------------

def test_t5_fpi_regime_disabled_outranks_ifrs_reason():
    """
    Currently RED. An FPI with a usable R&D series, with the regime
    switched OFF in config: two reasons could apply simultaneously here
    (it's an FPI, AND the regime is globally disabled), but only one
    should surface, and it must be the config-disabled one -- not "IFRS
    filer". If the regime is off, NOTHING is adjusted for ANYONE (see
    T2's domestic case); showing "IFRS filer" here would falsely imply
    this filer was singled out for an IFRS-specific abstention while
    domestic peers got adjusted, when in fact domestic peers are equally
    unadjusted right now because the regime is off entirely. The
    config-disabled reason is the one that's actually true and it must
    take precedence over the (also true, but less specific to why THIS
    company shows no adjustment right now) IFRS reason.

    Amendment (fix/rnd-badge-layout): same short/full split as T2. The
    IFRS reason -- short OR full -- must not leak in anywhere: cell,
    footnote, or tooltip.
    """
    cd = _fpi_with_rnd()
    assert is_fpi(cd)[0] is True

    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": False}}}
    res = _make_res(cd, cfg=cfg)

    # Setup check: derive() still computes roic_adjusted regardless of
    # the toggle (same root cause as T2), so the bug is live to guard against.
    adj = res.ratios.get("roic_adjusted")
    assert adj is not None and adj.value is not None, (
        "setup check: derive() should still compute roic_adjusted even with the regime "
        "disabled for an FPI with usable R&D -- if this now fails, this test needs revisiting"
    )

    full_reason = "R&D capitalization regime disabled in config"
    ifrs_full_reason_fragment = "IAS 38"

    md = R.render(res)
    html = RH.render(res)

    md_line = _roic_adj_md_line(md)
    assert md_line, "expected an R&D-UNADJ badge row in the Markdown report, got none"
    assert "R&D-UNADJ" in md_line, f"expected a badge, got a bare value: {md_line!r}"
    assert "IFRS filer" not in md_line, (
        f"regime-disabled must outrank the IFRS reason when both apply: {md_line!r}"
    )
    assert "disabled" in md_line.lower(), (
        f"cell text must disclose 'regime disabled': {md_line!r}"
    )
    assert full_reason in md, "the full regime-disabled rationale must appear as a footnote"
    assert ifrs_full_reason_fragment not in md, (
        "the IFRS rationale must not leak in anywhere (cell or footnote) once "
        "regime-disabled outranks it"
    )

    visible = _roic_adj_html_visible_text(html)
    assert visible, "expected an R&D-UNADJ badge in the HTML report, got none"
    assert "R&D-UNADJ" in visible, f"expected a badge, got a bare value: {visible!r}"
    assert "IFRS filer" not in visible, (
        f"regime-disabled must outrank the IFRS reason when both apply: {visible!r}"
    )
    assert "disabled" in visible.lower(), f"visible chip text must disclose 'regime disabled': {visible!r}"

    tooltip = _roic_adj_html_tooltip(html)
    assert tooltip == full_reason, (
        f"the chip's title= tooltip must carry the full regime-disabled rationale, "
        f"not the IFRS one: {tooltip!r}"
    )


# ---------------------------------------------------------------------------
# T6 -- rnd_regime=None must never fall through to a bare percentage
# ---------------------------------------------------------------------------

def _res_with_unstamped_regime():
    """
    A fully realistic AnalysisResult (built through derive(), same as
    every production path) but with rnd_regime reset to None afterward --
    simulating the state every non-derive() test call site
    (tests/test_report_html.py, tests/test_screen.py, tests/test_app.py,
    tests/test_council_endpoint.py, tests/test_council_report_endpoint.py)
    is already in by construction, PLUS a live, populated roic_adjusted
    ratio and a real R&D series, so Site C actually has a decision to make
    instead of hitting the no_rnd branch first.
    """
    cd = _domestic_with_rnd()
    cfg = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": True}}}
    res = _make_res(cd, cfg=cfg)
    assert res.ratios.get("roic_adjusted") is not None and res.ratios["roic_adjusted"].value is not None
    res.rnd_regime = None
    return res


def test_t6_unstamped_regime_never_emits_a_bare_percentage():
    """
    Currently RED: this is the None-regime hole. Site C's precedence chain
    is (1) no_rnd -> omit, (2) rnd_regime ABSTAINED -> badge, (3)
    roic_adjusted is None -> badge, (4) else -> bare percentage. Step (2)
    only fires `elif res.rnd_regime is not None and ... is not APPLIES`,
    so when rnd_regime is None outright, it's neither caught by (2) (guard
    requires `is not None`) nor by (3) (roic_adjusted is populated here) --
    it falls straight through to (4) and renders a bare, unchecked
    percentage exactly like F-14 did for ASML. A render surface must never
    print a number whose FPI/regime status it was never told.
    """
    res = _res_with_unstamped_regime()
    with pytest.raises(ValueError):
        R.render(res)
    with pytest.raises(ValueError):
        RH.render(res)


# ---------------------------------------------------------------------------
# T7 -- config-shape defaults: omitted rnd_capitalization / omitted
#        `enabled` alone must both match pre-PR (regime-off) behavior
# ---------------------------------------------------------------------------

def test_t7_omitted_config_keys_match_regime_off_behavior():
    """
    Guards Amendment 2: rnd_regime_applies's fallback for `enabled` must
    come from the SAME constant durability.py's _resolve_config merges
    against (engine.pipeline.RND_CAPITALIZATION_DEFAULTS), not an
    independently-typed literal that could silently drift from it. Two
    config shapes that omit the key by different routes -- omitting the
    whole `durability` section, and supplying a `durability` section with
    `rnd_capitalization` present but `enabled` itself missing -- must both
    resolve to the exact same regime-off composite as an explicit
    `enabled: False`.
    """
    cd = _domestic_with_rnd()
    cfg_explicit_off = {**_BASE_CFG, "durability": {"rnd_capitalization": {"enabled": False}}}
    cfg_no_durability_section = {**_BASE_CFG}
    cfg_enabled_key_missing = {**_BASE_CFG, "durability": {"rnd_capitalization": {"amortization_years": 7}}}

    ds_explicit = D.score(_make_res(cd, cfg=cfg_explicit_off), cfg_explicit_off)
    ds_no_section = D.score(_make_res(cd, cfg=cfg_no_durability_section), cfg_no_durability_section)
    ds_key_missing = D.score(_make_res(cd, cfg=cfg_enabled_key_missing), cfg_enabled_key_missing)

    assert ds_no_section.composite == ds_explicit.composite, (
        "omitting the whole durability section must match explicit enabled: False"
    )
    assert ds_key_missing.composite == ds_explicit.composite, (
        "omitting just the enabled key (rnd_capitalization section otherwise present) "
        "must match explicit enabled: False"
    )
