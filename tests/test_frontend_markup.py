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

_INDEX_HTML = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text()


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
