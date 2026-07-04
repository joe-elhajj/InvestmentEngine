"""
test_filings.py — tests for engine/filings.py (Tier 2's deterministic
fetch/parse layer — no model involvement anywhere in this file).

Offline: FilingsClient is always given a mocked EdgarClient stand-in here
(never a real one), so nothing in this file makes a real network call.
"""

from __future__ import annotations

import json

from engine.filings import FilingsClient, FilingSections, _find_latest_10k, split_sections


# ---------------------------------------------------------------------------
# split_sections() — pure text -> {item: text}, no I/O
# ---------------------------------------------------------------------------

_SAMPLE_10K_TEXT = """
Table of Contents
Item 1A. Risk Factors
Item 7. MD&A

Item 1.
Business
We design and sell widgets to customers worldwide.

Item 1A.
Risk Factors
Our widgets may become obsolete. A specific named lawsuit: XYZ Corp sued us in 2024.

Item 1B.
Unresolved Staff Comments
None.

Item 7.
Management Discussion and Analysis
Revenue increased 20% year over year.

Item 7A.
Quantitative Disclosures
Not applicable.
"""


class TestSplitSections:
    def test_skips_table_of_contents_and_splits_on_real_headers(self):
        sections = split_sections(_SAMPLE_10K_TEXT)
        assert "1" in sections and "1A" in sections and "7" in sections
        assert "We design and sell widgets" in sections["1"]
        assert "XYZ Corp sued us in 2024" in sections["1A"]
        assert "Revenue increased 20%" in sections["7"]

    def test_section_text_does_not_bleed_into_the_next_item(self):
        sections = split_sections(_SAMPLE_10K_TEXT)
        assert "Unresolved Staff Comments" not in sections["1A"]
        assert "Quantitative Disclosures" not in sections["7"]

    def test_no_item_headers_returns_empty_dict(self):
        assert split_sections("Just some plain text with no Item headers at all.") == {}

    def test_missing_target_item_is_absent_not_empty_string(self):
        text = "Item 1.\nBusiness\nSome business text here that is long enough to matter.\n"
        sections = split_sections(text)
        assert "1" in sections
        assert "1A" not in sections  # absence-is-not-zero: no fabricated empty string

    def test_long_section_is_truncated_with_marker(self):
        long_body = "x" * 30_000
        text = f"Item 1A.\nRisk Factors\n{long_body}\nItem 2.\nProperties\nSome text.\n"
        sections = split_sections(text)
        assert "...[truncated]" in sections["1A"]
        assert len(sections["1A"]) < 30_000


# ---------------------------------------------------------------------------
# _find_latest_10k() — most-recent-first, excludes amendments
# ---------------------------------------------------------------------------

def _subs(forms, accessions=None, docs=None, filing_dates=None, report_dates=None):
    n = len(forms)
    return {
        "filings": {
            "recent": {
                "form": forms,
                "accessionNumber": accessions or [f"acc-{i}" for i in range(n)],
                "primaryDocument": docs or [f"doc-{i}.htm" for i in range(n)],
                "filingDate": filing_dates or [f"2024-0{i + 1}-01" for i in range(n)],
                "reportDate": report_dates or [f"2023-12-3{i}" for i in range(n)],
            }
        }
    }


class TestFindLatest10K:
    def test_finds_the_first_10k_in_recent_order(self):
        subs = _subs(["8-K", "10-K", "10-K"], accessions=["a0", "a1", "a2"])
        latest = _find_latest_10k(subs)
        assert latest["accessionNumber"] == "a1"

    def test_excludes_10k_amendments(self):
        subs = _subs(["10-K/A", "8-K"], accessions=["a0", "a1"])
        assert _find_latest_10k(subs) is None

    def test_no_10k_at_all_returns_none(self):
        subs = _subs(["8-K", "4", "S-8"])
        assert _find_latest_10k(subs) is None

    def test_empty_submissions_returns_none(self):
        assert _find_latest_10k({}) is None


# ---------------------------------------------------------------------------
# FilingsClient — mocked EdgarClient stand-in, no real network
# ---------------------------------------------------------------------------

class _FakeEdgarClient:
    """Minimal stand-in for EdgarClient — only the methods FilingsClient uses."""

    def __init__(self, subs, html):
        self._subs = subs
        self._html = html
        self.get_text_call_count = 0

    def ticker_to_cik(self, ticker):
        return "0000320193"

    def _get_cached(self, cik, kind, url):
        return self._subs

    def get_text(self, url):
        self.get_text_call_count += 1
        self._last_url = url
        return self._html


_FAKE_HTML = """
<html><body>
<p>Item 1.</p><p>Business</p><p>We sell widgets internationally.</p>
<p>Item 1A.</p><p>Risk Factors</p><p>A material lawsuit was filed against us in 2024.</p>
<p>Item 7.</p><p>MD&amp;A</p><p>Revenue grew substantially this year.</p>
</body></html>
"""


class TestFilingsClient:
    def test_returns_none_when_no_10k_in_history(self, tmp_path):
        edgar = _FakeEdgarClient(_subs(["8-K"]), _FAKE_HTML)
        client = FilingsClient(edgar, cache_dir=str(tmp_path))
        assert client.latest_10k_sections("SPY") is None

    def test_fetches_and_splits_sections(self, tmp_path):
        subs = _subs(["10-K"], accessions=["0000320193-24-000123"], docs=["nvda10k.htm"])
        edgar = _FakeEdgarClient(subs, _FAKE_HTML)
        client = FilingsClient(edgar, cache_dir=str(tmp_path))
        result = client.latest_10k_sections("NVDA")
        assert isinstance(result, FilingSections)
        assert result.accession == "0000320193-24-000123"
        assert result.form == "10-K"
        assert "widgets internationally" in result.sections["1"]
        assert "lawsuit was filed against us in 2024" in result.sections["1A"]
        assert "Revenue grew substantially" in result.sections["7"]

    def test_builds_archive_url_with_cik_and_accession_nodash(self, tmp_path):
        subs = _subs(["10-K"], accessions=["0000320193-24-000123"], docs=["nvda10k.htm"])
        edgar = _FakeEdgarClient(subs, _FAKE_HTML)
        client = FilingsClient(edgar, cache_dir=str(tmp_path))
        result = client.latest_10k_sections("NVDA")
        assert result.url == (
            "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/nvda10k.htm"
        )

    def test_second_call_reuses_disk_cache_no_second_fetch(self, tmp_path):
        subs = _subs(["10-K"], accessions=["0000320193-24-000123"], docs=["nvda10k.htm"])
        edgar = _FakeEdgarClient(subs, _FAKE_HTML)
        client = FilingsClient(edgar, cache_dir=str(tmp_path))
        client.latest_10k_sections("NVDA")
        client.latest_10k_sections("NVDA")
        assert edgar.get_text_call_count == 1

    def test_cache_file_written_keyed_by_accession(self, tmp_path):
        subs = _subs(["10-K"], accessions=["0000320193-24-000123"], docs=["nvda10k.htm"])
        edgar = _FakeEdgarClient(subs, _FAKE_HTML)
        client = FilingsClient(edgar, cache_dir=str(tmp_path))
        client.latest_10k_sections("NVDA")
        cache_files = list(tmp_path.glob("*.json"))
        assert len(cache_files) == 1
        assert "000032019324000123" in cache_files[0].name

    def test_no_cache_mode_never_reads_or_writes(self, tmp_path):
        subs = _subs(["10-K"], accessions=["0000320193-24-000123"], docs=["nvda10k.htm"])
        edgar = _FakeEdgarClient(subs, _FAKE_HTML)
        client = FilingsClient(edgar, cache_dir=str(tmp_path), no_cache=True)
        client.latest_10k_sections("NVDA")
        client.latest_10k_sections("NVDA")
        assert edgar.get_text_call_count == 2
        assert not list(tmp_path.glob("*.json"))
