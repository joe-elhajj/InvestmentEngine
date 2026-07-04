"""
test_frontend_markup.py — static checks on frontend/index.html.

Plain string/structural checks on the shipped HTML file itself (not a
browser test) — cheap, fast, and catch regressions like a header losing
its tooltip or the old Gap caption creeping back in. Visual/interactive
behavior (hover, click-to-toggle) was verified manually in a real
headless-Chrome session; these tests lock in the markup that behavior
depends on.
"""

from __future__ import annotations

import re
from pathlib import Path

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_INDEX_HTML = (_FRONTEND_DIR / "index.html").read_text()
_STYLES_CSS = (_FRONTEND_DIR / "styles.css").read_text()
_APP_JS = (_FRONTEND_DIR / "app.js").read_text()


def _table_headers(table_id: str) -> list[tuple[str, bool]]:
    """Returns [(header_label, has_tooltip), ...] for a table's thead row."""
    table_match = re.search(
        rf'<table id="{table_id}">.*?<thead>(.*?)</thead>', _INDEX_HTML, re.S
    )
    assert table_match, f"table#{table_id} not found in index.html"
    thead = table_match.group(1)
    headers = re.findall(r'<th class="([^"]*)"[^>]*>(.*?)</th>', thead, re.S)
    assert headers, f"no <th class=...> headers found for table#{table_id}"
    results = []
    for cls, inner in headers:
        label = re.sub(r"<div.*", "", inner, flags=re.S).strip()
        results.append((label, "has-tooltip" in cls))
    return results


class TestColumnHeaderTooltips:
    def test_every_equities_header_has_a_tooltip(self):
        headers = _table_headers("equities-table")
        assert len(headers) == 10
        for label, has_tooltip in headers:
            assert has_tooltip, f"Equities header {label!r} is missing a tooltip"

    def test_every_etf_header_has_a_tooltip_except_name(self):
        headers = _table_headers("etf-table")
        assert len(headers) == 6
        for label, has_tooltip in headers:
            if label == "Name":
                assert not has_tooltip, "ETF 'Name' column must NOT have a tooltip"
            else:
                assert has_tooltip, f"ETF header {label!r} is missing a tooltip"

    def test_evidence_column_present_flag_gone(self):
        labels = [label for label, _ in _table_headers("etf-table")]
        assert "Evidence" in labels
        assert "Flag" not in labels

    def test_tooltips_are_styled_divs_not_bare_title_attrs(self):
        """CSS-styled divs, not bare title="" attributes — an explicit
        requirement, since title="" tooltips can't be themed/sized."""
        assert 'class="th-tooltip"' in _INDEX_HTML
        thead_blocks = re.findall(r"<thead>.*?</thead>", _INDEX_HTML, re.S)
        assert thead_blocks
        for block in thead_blocks:
            assert 'title="' not in block

    def test_tooltip_headers_are_keyboard_focusable(self):
        """tabindex="0" so :focus-within tooltips are reachable without a mouse."""
        for table_id in ("equities-table", "etf-table"):
            match = re.search(
                rf'<table id="{table_id}">.*?<thead>(.*?)</thead>', _INDEX_HTML, re.S
            )
            tooltip_ths = re.findall(r'<th class="[^"]*has-tooltip[^"]*"([^>]*)>', match.group(1))
            assert tooltip_ths
            for attrs in tooltip_ths:
                assert 'tabindex="0"' in attrs


class TestGapCaptionRemoved:
    def test_static_gap_caption_line_is_gone(self):
        assert "Gap = growth the price implies" not in _INDEX_HTML
        assert 'class="legend"' not in _INDEX_HTML


class TestAccordionSectionHeaderAlignment:
    """Task 1: the accordion <td> app.js injects fragment HTML into has no
    class, so it inherits the generic `tbody td{text-align:right}` rule —
    text-align is inherited by every unstyled descendant. Locks in the
    root-cause reset (not a per-element whack-a-mole fix)."""

    def test_report_fragment_resets_text_align_left(self):
        assert re.search(r"\.report-fragment\{[^}]*text-align:left", _STYLES_CSS)

    def test_report_section_summary_is_explicitly_left_aligned(self):
        assert re.search(r"\.report-section summary\{[^}]*text-align:left", _STYLES_CSS)


class TestTooltipFocusVisiblePattern:
    """Task 2: a mouse click assigning :focus to a tabindex="0" header used
    to pin its tooltip open. The trigger rules must key off :focus-visible
    (keyboard-only), not :focus or :focus-within, and app.js should also
    preventDefault() on mousedown as defense-in-depth."""

    def test_tooltip_visibility_rules_use_focus_visible(self):
        assert ".has-tooltip:focus-visible .th-tooltip" in _STYLES_CSS
        assert "thead th.has-tooltip:focus-visible::before" in _STYLES_CSS

    def test_tooltip_trigger_rules_do_not_use_bare_focus_or_focus_within(self):
        assert ".has-tooltip:focus .th-tooltip" not in _STYLES_CSS
        assert ".has-tooltip:focus-within" not in _STYLES_CSS

    def test_mousedown_prevents_focus_assignment_on_tooltip_headers(self):
        assert '"mousedown"' in _APP_JS
        assert ".has-tooltip" in _APP_JS
        match = re.search(r'addEventListener\("mousedown".*?\}\);', _APP_JS, re.S)
        assert match, "no mousedown listener found in app.js"
        assert "preventDefault" in match.group(0)
        assert ".has-tooltip" in match.group(0)


class TestColumnSortAttributes:
    """Task 4: sort attributes present on exactly the specified columns —
    numeric score/growth columns + Ticker for equities; Exp Ratio/AUM/
    Overlap + Ticker for ETFs. Name and Evidence must stay unsortable."""

    def _sort_keys(self, table_id: str) -> dict[str, str | None]:
        match = re.search(
            rf'<table id="{table_id}">.*?<thead>(.*?)</thead>', _INDEX_HTML, re.S
        )
        assert match, f"table#{table_id} not found"
        thead = match.group(1)
        headers = re.findall(r'<th class="([^"]*)"([^>]*)>(.*?)</th>', thead, re.S)
        result = {}
        for cls, attrs, inner in headers:
            label = re.sub(r"<div.*", "", inner, flags=re.S).strip()
            key_match = re.search(r'data-sort-key="([^"]*)"', attrs)
            result[label] = key_match.group(1) if key_match else None
        return result

    def test_equities_sortable_columns(self):
        keys = self._sort_keys("equities-table")
        expected = {
            "Ticker": "ticker", "Durability": "composite", "Reinv": "cat_reinvestment",
            "Quality": "cat_quality", "Resilience": "cat_resilience",
            "Discipline": "cat_discipline", "Optionality": "cat_optionality",
            "Implied g": "implied_fcf_growth", "Delivered g": "delivered_fcf_growth",
            "Gap": "expectations_gap",
        }
        assert keys == expected

    def test_etf_sortable_columns(self):
        keys = self._sort_keys("etf-table")
        assert keys["Ticker"] == "ticker"
        assert keys["Exp Ratio"] == "expense_ratio"
        assert keys["AUM"] == "aum"
        assert keys["Overlap w/ Singles"] == "overlap_with_screen"

    def test_etf_name_and_evidence_are_not_sortable(self):
        keys = self._sort_keys("etf-table")
        assert keys["Name"] is None
        assert keys["Evidence"] is None

    def test_sort_arrow_and_caption_placeholders_present(self):
        assert 'class="sort-arrow"' in _INDEX_HTML
        assert 'id="equities-sort-caption"' in _INDEX_HTML
        assert 'id="etf-sort-caption"' in _INDEX_HTML


class TestExportButtons:
    """Task 5: Export CSV + Save as PDF in the app bar, near Refresh."""

    def test_export_buttons_present_in_topnav(self):
        topnav = re.search(r"<header class=\"topnav\">.*?</header>", _INDEX_HTML, re.S)
        assert topnav
        assert 'id="export-csv-btn"' in topnav.group(0)
        assert 'id="export-pdf-btn"' in topnav.group(0)
        assert 'id="refresh-btn"' in topnav.group(0)

    def test_pdf_button_uses_window_print(self):
        assert "window.print()" in _APP_JS

    def test_print_stylesheet_present(self):
        assert "@media print" in _STYLES_CSS

    def test_csv_export_never_writes_na_string_for_absent_values(self):
        """Absence-is-not-zero: CSV blanks are empty fields, never the UI's
        "n/a" label and never a coerced 0."""
        assert re.search(r"function csvVal\([^)]*\)\s*\{[^}]*return[^;]*\"\"", _APP_JS)


class TestDataDiagnosticsSection:
    """Task 6: collapsible section below Excluded, collapsed by default,
    reusing existing /api/screen data — no second screen run."""

    def test_diagnostics_details_present_and_collapsed_by_default(self):
        match = re.search(r'<details id="diagnostics-section"([^>]*)>', _INDEX_HTML)
        assert match, "diagnostics <details> not found"
        assert "open" not in match.group(1)

    def test_diagnostics_section_is_below_excluded_section(self):
        excluded_pos = _INDEX_HTML.index('id="excluded-section"')
        diagnostics_pos = _INDEX_HTML.index('id="diagnostics-section"')
        assert excluded_pos < diagnostics_pos

    def test_diagnostics_table_headers(self):
        """Universe/config_hash are gone from the per-row columns — see
        test_diagnostics_stamp_element_present for where they went instead."""
        match = re.search(
            r'<table id="diagnostics-table">.*?<thead>(.*?)</thead>', _INDEX_HTML, re.S
        )
        assert match
        labels = re.findall(r"<th[^>]*>(.*?)</th>", match.group(1))
        assert labels == ["Ticker", "Band", "Completeness", "Stable", "Notes"]

    def test_diagnostics_stamp_element_present(self):
        assert '<p id="diagnostics-stamp"' in _INDEX_HTML

    def test_diagnostics_clean_message_present_and_hidden_by_default(self):
        match = re.search(r'<p id="diagnostics-clean"([^>]*)>', _INDEX_HTML)
        assert match
        assert "hidden" in match.group(1)
        assert "no diagnostics to report" in _INDEX_HTML

    def test_no_second_screen_run_for_diagnostics(self):
        """Diagnostics must be populated from the same renderScreen() data
        as the other tables, not a second fetch to /api/screen."""
        assert "renderDiagnostics(data)" in _APP_JS
        diagnostics_fn = re.search(r"function renderDiagnostics\(.*?\n  \}", _APP_JS, re.S)
        assert diagnostics_fn
        assert "fetch(" not in diagnostics_fn.group(0)

    def test_exceptions_only_filter_present(self):
        """Task 3: only rows with completeness<100%, stable==no, or a note
        render at all — everything else is pure noise (band/completeness/
        stable identical, universe/config_hash repeated every row)."""
        assert "function diagnosticsRowNeeded" in _APP_JS
        assert ".filter(diagnosticsRowNeeded)" in _APP_JS
