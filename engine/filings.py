"""
filings.py — fetches the latest 10-K's actual document text and splits it
into the target sections (Item 1, Item 1A, Item 7) for Tier 2's LLM flag
extraction (engine/flags.py).

Pure fetch/parse: no model involvement anywhere in this file. EdgarClient
already owns SEC session/rate-limit/User-Agent discipline (engine/edgar.py);
this module only adds the raw-document fetch URL, HTML-to-text conversion,
and item-section splitting on top of it, plus its own on-disk cache keyed
by accession number (a filed 10-K is immutable — no TTL needed, unlike the
XBRL/submissions cache in engine/edgar.py which does expire).

Section-splitting caveat: 10-K HTML documents usually contain a table of
contents that lists "Item 1A." etc. before the real section headers, so a
naive first-match search would slice at the TOC instead of the body. This
uses a "last matching heading line wins" heuristic, which is good enough
for feeding an LLM (imprecise boundaries just mean slightly noisier context)
— it is NOT a source of hallucination risk, because engine/flags.py's
verbatim validator always checks a returned snippet against the EXACT
section text that was actually sent to the model, whatever its boundaries
turned out to be.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from engine.edgar import EdgarClient, SEC_SUBMISSIONS_URL

log = logging.getLogger(__name__)

SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{doc}"

# Cap per-section text so a single filing doesn't blow up the LLM prompt's
# size/cost. Truncation is applied BEFORE caching, so the cached text is
# exactly what any future extraction call will see (and exactly what the
# verbatim validator will check snippets against).
_MAX_SECTION_CHARS = 20_000
_TRUNCATION_MARKER = "\n...[truncated]"

# Item numbers that matter as SECTION BOUNDARIES, in filing order. Only "1",
# "1A", "7" are targets we return text for, but the others are needed to
# know where those three sections end.
_BOUNDARY_ITEMS = ["1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8", "9"]
_TARGET_ITEMS = ("1", "1A", "7")

# Matches a heading LINE like "Item 1A." or "Item 7. Management's Discussion"
# — anchored to the start of a line (re.MULTILINE), tolerant of a trailing
# short title (<=80 chars) but not a full paragraph, which is what
# disambiguates a real heading line from an inline body reference like
# "as discussed in Item 1A of this report, ...".
_ITEM_LINE_RE = re.compile(
    r"^\s*item\s+(1a|1b|1c|7a|7b|1|2|3|4|5|6|7|8|9)\.?\s*.{0,80}$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class FilingSections:
    ticker: str
    cik: str
    accession: str
    form: str
    filed: Optional[str]
    period_ending: Optional[str]
    url: str
    sections: dict = field(default_factory=dict)  # only items actually found, e.g. {"1": "...", "1A": "...", "7": "..."}


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def _normalize_item(raw: str) -> str:
    return raw.upper()


def _find_boundaries(text: str) -> dict[str, int]:
    """
    Returns {item_number: char_index} for the LAST occurrence of each
    boundary item's heading line — the "last match wins" TOC-avoidance
    heuristic described in the module docstring.
    """
    last_index: dict[str, int] = {}
    for match in _ITEM_LINE_RE.finditer(text):
        item = _normalize_item(match.group(1))
        if item in _BOUNDARY_ITEMS:
            last_index[item] = match.start()
    return last_index


def split_sections(text: str) -> dict[str, str]:
    """
    Pure text -> {item: section_text} split, no I/O — kept separate from
    the fetch/cache path so it's directly unit-testable against synthetic
    10-K-shaped text.
    """
    boundaries = _find_boundaries(text)
    if not boundaries:
        return {}

    ordered_positions = sorted(set(boundaries.values()) | {len(text)})

    def _end_of(start: int) -> int:
        for pos in ordered_positions:
            if pos > start:
                return pos
        return len(text)

    sections: dict[str, str] = {}
    for item in _TARGET_ITEMS:
        start = boundaries.get(item)
        if start is None:
            continue
        end = _end_of(start)
        section_text = text[start:end].strip()
        if len(section_text) > _MAX_SECTION_CHARS:
            section_text = section_text[:_MAX_SECTION_CHARS] + _TRUNCATION_MARKER
        if section_text:
            sections[item] = section_text
    return sections


def _find_latest_10k(subs: dict) -> Optional[dict]:
    """
    SEC's submissions "recent" arrays are already most-recent-first, so the
    first "10-K" match IS the latest — no sorting needed. Amendments
    (10-K/A) are deliberately excluded; this is scoped to the primary
    annual filing only.
    """
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    for i, form in enumerate(forms):
        if form == "10-K":
            return {
                "accessionNumber": accessions[i],
                "primaryDocument": primary_docs[i] if i < len(primary_docs) else "",
                "form": form,
                "filingDate": filing_dates[i] if i < len(filing_dates) else None,
                "reportDate": report_dates[i] if i < len(report_dates) else None,
            }
    return None


class FilingsClient:
    def __init__(self, edgar_client: EdgarClient, cache_dir: str = ".cache/filings", no_cache: bool = False):
        self._edgar = edgar_client
        self._cache_dir = Path(cache_dir)
        self._no_cache = no_cache

    def _cache_path(self, accession_nodash: str) -> Path:
        return self._cache_dir / f"{accession_nodash}.json"

    def _read_cache(self, accession_nodash: str) -> Optional[dict]:
        if self._no_cache:
            return None
        p = self._cache_path(accession_nodash)
        if not p.exists():
            return None
        # No TTL check: a filed 10-K's document text never changes, so a
        # cache entry keyed by accession number is valid forever — unlike
        # EdgarClient's XBRL/submissions cache, which is TTL-bounded because
        # a company's most-recent filing can change day to day.
        return json.loads(p.read_text())

    def _write_cache(self, accession_nodash: str, data: dict) -> None:
        if self._no_cache:
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(accession_nodash).write_text(json.dumps(data))

    def latest_10k_sections(self, ticker: str) -> Optional[FilingSections]:
        """Returns None if no 10-K is found in the ticker's filing history
        (e.g. an ETF/fund, or a foreign private issuer that files 20-F/40-F
        instead) — never fabricates a filing that doesn't exist."""
        cik = self._edgar.ticker_to_cik(ticker)
        subs = self._edgar._get_cached(cik, "submissions", SEC_SUBMISSIONS_URL.format(cik10=cik))
        latest = _find_latest_10k(subs)
        if latest is None:
            return None

        accession_nodash = latest["accessionNumber"].replace("-", "")
        cached = self._read_cache(accession_nodash)
        if cached is not None:
            return FilingSections(**cached)

        url = SEC_ARCHIVE_URL.format(
            cik_int=int(cik), accession_nodash=accession_nodash, doc=latest["primaryDocument"]
        )
        html = self._edgar.get_text(url)
        text = _html_to_text(html)
        sections = split_sections(text)

        result = FilingSections(
            ticker=ticker.upper(),
            cik=cik,
            accession=latest["accessionNumber"],
            form=latest["form"],
            filed=latest["filingDate"],
            period_ending=latest["reportDate"],
            url=url,
            sections=sections,
        )
        self._write_cache(accession_nodash, asdict(result))
        return result
