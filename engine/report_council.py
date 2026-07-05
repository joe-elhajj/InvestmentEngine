"""
report_council.py — render a Tier 3 CouncilResult as a self-contained,
print-first HTML "Council Review" research note.

Same pure-function pattern as report.py/report_html.py: render() takes
already-computed data and returns HTML text, does no network access, no
file I/O, and makes no LLM calls — every word in the output comes from the
CouncilResult already cached on disk by engine/council.py's convene(). The
three extra optional parameters beyond the (council_result, quote, config)
the caller (app/main.py) already has cheaply available from Tier 1/2's own
existing cached reads (company name, flags cache status) — matching
report_html.py's own precedent of render_fragment() taking optional extras
(peer_table, durability_composite) beyond its one core object.

Below render(), a small set of on-disk cache helpers (mirroring
engine/council.py's own is_cached()/load_cached()/_save_cached() — reusing
its exact _cache_key() so a report always lands beside the CouncilResult
it was rendered from) store the rendered HTML and, once generated, the PDF
bytes. These DO touch the filesystem — same "engine/ module with cache
I/O alongside its pure computation" shape as engine/flags.py and
engine/council.py themselves, not a departure from it. PDF *generation*
(the html_to_pdf() call) deliberately lives in app/pdf.py, not here —
engine/ never depends on app/, and turning HTML into PDF bytes is a
web-layer/subprocess concern, not a rendering one.

Formatting note: advisor/reviewer/chairman text is real model prose, not
structured data — it uses light markdown (**bold**, `code`, "- " bullets,
"1. " numbered lists) by convention of the Round 1/2/3 prompts in
engine/council.py, not by any guarantee. _prose() below is a small,
self-contained, tolerant converter (escape-first, then bold/code/italic
inline spans, paragraph and list detection) — never a dependency, and
never a hard requirement: unparseable text still renders as plain
escaped paragraphs, never dropped.
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import Optional

from engine.council import (
    AdvisorOpinion,
    ChairmanOutput,
    CouncilResult,
    ReviewNote,
    _cache_key,
    _extract_field,
    _split_sections,
)
from engine.market import Quote

_POSITION_TAXONOMY = ("ACCUMULATE", "HOLD", "TRIM", "AVOID", "INSUFFICIENT EVIDENCE")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    ax = abs(x)
    if ax >= 1e12:
        return f"${x/1e12:.2f}T"
    if ax >= 1e9:
        return f"${x/1e9:.2f}B"
    if ax >= 1e6:
        return f"${x/1e6:.1f}M"
    return f"${x:,.2f}"


def _fmt_usd_precise(x: Optional[float]) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


# ---------------------------------------------------------------------------
# Tiny, tolerant "lite markdown" -> HTML converter. Escapes HTML first, so
# nothing the model wrote can inject markup; then layers minimal inline
# formatting and block structure (paragraphs, bullet/numbered lists) on
# top. Never raises — an input that doesn't match any pattern still comes
# out as a safe, escaped paragraph.
# ---------------------------------------------------------------------------

def _inline(text: str) -> str:
    t = escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`([^`]+?)`", r"<code>\1</code>", t)
    t = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", t)
    return t


def _prose(text: Optional[str]) -> str:
    """Blocks separated by a blank line; each block is either a paragraph
    or a run of "- " / "N. " list items, rendered as <ul>/<ol>."""
    if not text or not text.strip():
        return ""
    blocks = re.split(r"\n\s*\n", text.strip())
    html_parts = []
    for block in blocks:
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        if all(re.match(r"^-\s+", ln) for ln in lines):
            items = "".join(f"<li>{_inline(re.sub(r'^-\s+', '', ln))}</li>" for ln in lines)
            html_parts.append(f"<ul class=\"prose-list\">{items}</ul>")
        elif all(re.match(r"^\d+\.\s+", ln) for ln in lines):
            items = "".join(f"<li>{_inline(re.sub(r'^\d+\.\s+', '', ln))}</li>" for ln in lines)
            html_parts.append(f"<ol class=\"prose-list\">{items}</ol>")
        else:
            html_parts.append(f"<p>{_inline(' '.join(lines))}</p>")
    return "".join(html_parts)


def _split_numbered_items(text: Optional[str]) -> list[str]:
    """Splits a "1. ...\n\n2. ...\n\n3. ..." section into its numbered
    items — each item keeps everything up to (not including) the next
    top-level number, so a multi-paragraph item stays intact."""
    if not text or not text.strip():
        return []
    matches = list(re.finditer(r"(?m)^(\d+)\.\s+", text))
    if not matches:
        return [text.strip()]
    items = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        items.append(text[start:end].strip())
    return items


def _strip_trailing_fields(text: str, field_names: list[str]) -> str:
    """Removes the trailing "FIELD: value" lines the Round 1 prompt
    mandates (POSITION/AGAINST/CONFIDENCE) from the displayed reasoning
    body — they're already surfaced separately (verdict badge, confidence
    number, callout), so leaving them in the prose would duplicate them
    verbatim at the end of a paragraph, exactly what a real research note
    would never do."""
    pattern = r"(?im)^\s*(?:" + "|".join(field_names) + r")\s*:.*$"
    cut = None
    for m in re.finditer(pattern, text):
        if cut is None or m.start() < cut:
            cut = m.start()
    return text[:cut].strip() if cut is not None else text.strip()


def _confidence_mover(chairman_text: str) -> Optional[str]:
    """The sentence(s) immediately after "CONFIDENCE: <n>" in the VERDICT
    block, up to the next blank line — the chairman's own stated "what
    would move confidence up/down a notch," which _parse_chairman()
    (engine/council.py) never stores as its own field (only verdict/
    confidence are extracted from that block). Re-derived here from the
    raw text already sitting in the cache — no new computation, no model
    call, a presentation-layer parse of already-cached prose."""
    sections = _split_sections(chairman_text)
    verdict_block = sections.get("VERDICT", "")
    m = re.search(r"(?im)^\s*CONFIDENCE\s*:\s*\d\s*\n(.*?)(?:\n\s*\n|\Z)", verdict_block, re.S)
    if not m:
        return None
    para = " ".join(ln.strip() for ln in m.group(1).strip().splitlines() if ln.strip())
    return para or None


def _split_review(text: str) -> Optional[dict]:
    """Best-effort extraction of the reviewer prompt's three asked-for
    parts (strongest / weakest / contradiction) from **bold**-marked
    prose — a convention every real run on record follows, never a
    guarantee. Returns None (not a partial/garbled dict) if the pattern
    doesn't match, so the caller can fall back to the full raw text
    rather than show a broken table cell."""
    m_s = re.search(r"\*\*Strongest[:\s]*([^*]*)\*\*(.*?)(?=\*\*Weakest|\*\*Contradiction|\Z)", text, re.I | re.S)
    m_w = re.search(r"\*\*Weakest[:\s]*([^*]*)\*\*(.*?)(?=\*\*Strongest|\*\*Contradiction|\Z)", text, re.I | re.S)
    m_c = re.search(r"\*\*Contradiction[:\s]*([^*]*)\*\*(.*)", text, re.I | re.S)
    if not (m_s and m_w and m_c):
        return None
    return {
        "strongest": (m_s.group(1).strip(" .:") + " " + m_s.group(2).strip()).strip(),
        "weakest": (m_w.group(1).strip(" .:") + " " + m_w.group(2).strip()).strip(),
        "contradiction": (m_c.group(1).strip(" .:") + " " + m_c.group(2).strip()).strip(),
    }


def _status_chip(text: str) -> tuple[str, str, str]:
    """Pulls a leading/trailing **OPEN**/**RESOLVED**-prefixed marker
    (contradiction ledger items) out of an item's text, returning (label,
    css_class, remaining_text). The whole bold span is kept as the chip
    label (e.g. "RESOLVED (PARTIALLY)"), not just the first word, so a
    qualified resolution isn't flattened into a bare "RESOLVED". Defaults
    to "OPEN" — noted, not silently dropped — if no marker is present at
    all, since an unmarked contradiction is still open by definition."""
    m = re.search(r"\*\*\s*((?:OPEN|RESOLVED)[^*]*)\*\*", text, re.I)
    if not m:
        return "OPEN", "open", text
    label = re.sub(r"\s+", " ", m.group(1)).strip().upper()
    css_class = "resolved" if label.startswith("RESOLVED") else "open"
    remaining = (text[:m.start()] + text[m.end():]).strip(" —-\n")
    return label, css_class, remaining


def _owner_tag(text: str) -> tuple[Optional[str], str]:
    """Pulls a leading "**Owner: X.**" tag out of an action item, returning
    (owner_or_None, remaining_text)."""
    m = re.match(r"\s*\*\*\s*Owner\s*:\s*([^*.]+)\.?\s*\*\*\s*", text, re.I)
    if not m:
        return None, text
    return m.group(1).strip(), text[m.end():].strip()


def _dissent_blocks(text: Optional[str]) -> list[dict]:
    """Splits the dissent section into one signed block per "- **NAME
    (POSITION, confidence N)**..." bullet. Falls back to a single
    unattributed block (the raw prose, still fully rendered) if that
    pattern isn't found — e.g. a genuinely unanimous run's "no dissent"
    sentence, which has nothing to attribute."""
    if not text or not text.strip():
        return []
    matches = list(re.finditer(r"(?m)^-\s+\*\*(.+?)\*\*\s*", text))
    if not matches:
        return [{"header": None, "body": _prose(text)}]
    blocks = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append({"header": _inline(m.group(1).strip()), "body": _prose(text[start:end].strip())})
    return blocks


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _masthead(cr: CouncilResult, quote: Optional[Quote], config: dict, company_name: Optional[str]) -> str:
    verdict = cr.chairman.verdict or "n/a"
    universe_version = (config or {}).get("universe", {}).get("version") or "n/a"
    price = _fmt_usd_precise(quote.price) if quote is not None else "n/a"
    mcap = _fmt_money(quote.market_cap) if quote is not None else "n/a"
    name_html = f" &middot; {escape(company_name)}" if company_name else ""
    return f"""
<header class="masthead">
  <div class="masthead-top">
    <div class="masthead-brand">InvestmentEngine <span class="masthead-brand-sub">— Council Review</span></div>
    <div class="verdict-badge verdict-{escape(verdict.replace(' ', '-').lower())}">{escape(verdict)}</div>
  </div>
  <h1 class="masthead-ticker">{escape(cr.ticker)}<span class="masthead-name">{name_html}</span></h1>
  <table class="masthead-meta">
    <tr>
      <td><span class="meta-label">Convened</span><span class="meta-value">{escape(cr.meta.convened_at)}</span></td>
      <td><span class="meta-label">Model</span><span class="meta-value">{escape(cr.meta.model)} &middot; prompt {escape(cr.meta.prompt_version)}</span></td>
      <td><span class="meta-label">Config hash</span><span class="meta-value">{escape(cr.meta.config_hash)}</span></td>
    </tr>
    <tr>
      <td><span class="meta-label">Universe</span><span class="meta-value">{escape(str(universe_version))}</span></td>
      <td><span class="meta-label">Price at generation</span><span class="meta-value">{price}</span></td>
      <td><span class="meta-label">Market cap</span><span class="meta-value">{mcap}</span></td>
    </tr>
  </table>
</header>
"""


def _executive_summary(cr: CouncilResult) -> str:
    mover = _confidence_mover(cr.chairman.text)
    mover_html = f'<p class="confidence-mover">{_inline(mover)}</p>' if mover else '<p class="absence-marker">No confidence-mover statement recorded for this run.</p>'
    rows = []
    for a in cr.advisors:
        pos = a.position or "unparsed"
        conf = f"{a.confidence}/5" if a.confidence is not None else "n/a"
        rows.append(
            f'<tr><td class="l">{escape(a.name)}</td>'
            f'<td class="pos-{escape((a.position or "na").replace(" ", "-").lower())}">{escape(pos)}</td>'
            f"<td>{escape(conf)}</td></tr>"
        )
    conf_str = f"{cr.chairman.confidence}/5" if cr.chairman.confidence is not None else "n/a"
    return f"""
<section class="report-section exec-summary" style="break-inside:avoid">
  <h2>Executive summary</h2>
  <p class="exec-verdict"><span class="verdict-word">{escape(cr.chairman.verdict or 'n/a')}</span> &middot; confidence {escape(conf_str)}</p>
  {mover_html}
  <table class="agent-table">
    <thead><tr><th class="l">Advisor</th><th>Position</th><th>Confidence</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>
"""


def _evidence_status_section(cr: CouncilResult, flags_status: Optional[dict]) -> str:
    thesis_line = (
        "Pre-thesis — no existing thesis journal entry to audit."
        if cr.meta.thesis_status == "pre_thesis"
        else f"Thesis present (hash {escape(cr.meta.thesis_hash or 'n/a')})."
    )
    if flags_status is None:
        flags_html = '<p class="absence-marker">Flags cache status not available to this render.</p>'
    elif not flags_status.get("cached"):
        flags_html = '<p class="absence-marker">No cached Tier 2 flag extraction for this filing.</p>'
    else:
        flags_html = (
            f"<p>Filing: {escape(str(flags_status.get('form') or 'n/a'))} "
            f"({escape(str(flags_status.get('accession') or 'n/a'))}), "
            f"period ending {escape(str(flags_status.get('period_ending') or 'n/a'))}, "
            f"filed {escape(str(flags_status.get('filed') or 'n/a'))} &middot; "
            f"{flags_status.get('count', 0)} flag(s) extracted.</p>"
        )
    return f"""
<section class="report-section" style="break-inside:avoid">
  <h2>Evidence bundle status</h2>
  <p><span class="ev-label">Quant —</span> Tier 1 canonical JSON (EDGAR filings + market data), deterministic, no model involvement.</p>
  <p><span class="ev-label">Flags —</span></p>
  {flags_html}
  <p><span class="ev-label">Thesis —</span> {thesis_line}</p>
</section>
"""


def _round1_section(cr: CouncilResult) -> str:
    cards = []
    for a in cr.advisors:
        against = _extract_field(a.text, "AGAINST")
        body = _strip_trailing_fields(a.text, ["POSITION", "AGAINST", "CONFIDENCE"])
        conf = f"{a.confidence}/5" if a.confidence is not None else "n/a"
        against_html = (
            f'<div class="against-callout"><span class="against-label">Strongest point against its own position</span>'
            f"<p>{_inline(against)}</p></div>"
            if against else
            '<div class="against-callout absence-marker">No "against" line recorded for this advisor.</div>'
        )
        cards.append(f"""
    <div class="advisor-card" style="break-inside:avoid">
      <div class="advisor-head">
        <span class="advisor-name">{escape(a.name)}</span>
        <span class="pos-{escape((a.position or 'na').replace(' ', '-').lower())}">{escape(a.position or 'unparsed')}</span>
        <span class="advisor-confidence">confidence {escape(conf)}</span>
      </div>
      {_prose(body)}
      {against_html}
    </div>""")
    return f"""
<section class="report-section">
  <h2>Round 1 — Agent opinions</h2>
  {''.join(cards)}
</section>
"""


def _round2_section(cr: CouncilResult) -> str:
    rows = []
    for r in cr.reviews:
        split = _split_review(r.text)
        if split:
            rows.append(
                f'<tr style="break-inside:avoid"><td class="l">{escape(r.reviewer)}</td>'
                f'<td>{_inline(split["strongest"])}</td>'
                f'<td>{_inline(split["weakest"])}</td>'
                f'<td>{_inline(split["contradiction"])}</td></tr>'
            )
        else:
            rows.append(
                f'<tr style="break-inside:avoid"><td class="l">{escape(r.reviewer)}</td>'
                f'<td colspan="3">{_prose(r.text)}</td></tr>'
            )
    return f"""
<section class="report-section">
  <h2>Round 2 — Blind peer review</h2>
  <table class="review-table">
    <thead><tr><th class="l">Reviewer</th><th class="l">Strongest opinion</th><th class="l">Weakest reasoning</th><th class="l">Contradiction surfaced</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>
"""


def _contradiction_section(sections: dict) -> str:
    raw = sections.get("contradiction_ledger")
    if not raw:
        return _absent_section("Contradiction ledger")
    items = _split_numbered_items(raw)
    rows = []
    for item in items:
        label, css_class, remaining = _status_chip(item)
        rows.append(
            f'<div class="ledger-item" style="break-inside:avoid">'
            f'<span class="status-chip status-{css_class}">{escape(label)}</span>'
            f"{_prose(remaining)}</div>"
        )
    return f"""
<section class="report-section">
  <h2>Contradiction ledger</h2>
  {''.join(rows)}
</section>
"""


def _thesis_section(sections: dict) -> str:
    raw = sections.get("thesis_journal_delta")
    if not raw:
        return _absent_section("Thesis journal delta")
    blocks = re.split(r"\n\s*\n", raw.strip())
    intro, checklist = [], []
    for block in blocks:
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if lines and all(re.match(r"^-\s+", ln) for ln in lines):
            checklist.extend(re.sub(r"^-\s+", "", ln) for ln in lines)
        else:
            intro.append(block)
    intro_html = _prose("\n\n".join(intro)) if intro else ""
    checklist_html = (
        '<ul class="falsification-checklist">'
        + "".join(f'<li><span class="check-mark">&#9744;</span>{_inline(item)}</li>' for item in checklist)
        + "</ul>"
        if checklist else '<p class="absence-marker">No falsification criteria drafted for this run.</p>'
    )
    return f"""
<section class="report-section">
  <h2>Thesis journal delta</h2>
  {intro_html}
  {checklist_html}
</section>
"""


def _action_items_section(sections: dict) -> str:
    raw = sections.get("action_items")
    if not raw:
        return _absent_section("Action items")
    items = _split_numbered_items(raw)
    rows = []
    for item in items:
        owner, remaining = _owner_tag(item)
        owner_html = f'<span class="owner-tag">{escape(owner)}</span>' if owner else ""
        rows.append(f'<li style="break-inside:avoid">{owner_html}{_prose(remaining)}</li>')
    return f"""
<section class="report-section">
  <h2>Action items</h2>
  <ol class="action-items-list">{''.join(rows)}</ol>
</section>
"""


def _risk_and_dissent_section(sections: dict) -> str:
    risk_raw = sections.get("risk_register")
    risk_html = (
        "<ol class=\"risk-list\">" + "".join(
            f'<li style="break-inside:avoid">{_prose(item)}</li>' for item in _split_numbered_items(risk_raw)
        ) + "</ol>"
        if risk_raw else '<p class="absence-marker">No risk register recorded for this run.</p>'
    )
    dissent_raw = sections.get("dissent")
    dissent_blocks = _dissent_blocks(dissent_raw)
    if dissent_blocks:
        dissent_html = "".join(
            f'<div class="dissent-block" style="break-inside:avoid">'
            + (f'<div class="dissent-header">{b["header"]}</div>' if b["header"] else "")
            + f'{b["body"]}</div>'
            for b in dissent_blocks
        )
    else:
        dissent_html = '<p class="absence-marker">No dissent recorded for this run.</p>'
    return f"""
<section class="report-section" style="break-inside:avoid">
  <h2>Risk register</h2>
  {risk_html}
</section>
<section class="report-section">
  <h2>Dissent</h2>
  {dissent_html}
</section>
"""


def _absent_section(title: str) -> str:
    """Absence-is-not-zero applies to report sections too: a missing
    chairman section renders an explicit marker under its own heading,
    never a silently-vanished section."""
    return f"""
<section class="report-section" style="break-inside:avoid">
  <h2>{escape(title)}</h2>
  <p class="absence-marker">Not present in this council run's cached output.</p>
</section>
"""


def _footer(cr: CouncilResult) -> str:
    return f"""
<footer class="report-footer">
  <p>Accession {escape(cr.meta.accession)} &middot; config hash {escape(cr.meta.config_hash)} &middot; generated {escape(cr.meta.convened_at)}</p>
  <p class="disclaimer">This is decision-support output generated by an adversarial council of language models reasoning over cached, deterministic Tier 1/2 evidence. It is not investment advice.</p>
</footer>
"""


# ---------------------------------------------------------------------------
# Top-level render
# ---------------------------------------------------------------------------

_STYLE = """
:root{
  --paper:#fbfaf7; --ink:#1a1a1a; --muted:#5b5648; --hairline:#d8d3c6;
  --accent:#7a1f1f; --good:#1d7d54; --bad:#b3271e; --chip-open:#b3271e; --chip-resolved:#1d7d54;
  --mono:"SF Mono","Cascadia Code",ui-monospace,Menlo,monospace;
  --serif:Georgia,"Iowan Old Style","Times New Roman",serif;
}
*{box-sizing:border-box}
body{
  margin:0;padding:40px 56px;background:var(--paper);color:var(--ink);
  font-family:var(--serif);font-size:14px;line-height:1.6;
}
.masthead{border-bottom:3px double var(--ink);padding-bottom:16px;margin-bottom:28px}
.masthead-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.masthead-brand{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.masthead-brand-sub{color:var(--ink);font-weight:700}
.masthead-ticker{font-size:32px;margin:4px 0 14px;letter-spacing:-0.01em}
.masthead-name{font-size:18px;font-weight:400;color:var(--muted)}
.verdict-badge{
  font-family:var(--mono);font-size:13px;font-weight:700;letter-spacing:.06em;
  padding:6px 16px;border:2px solid var(--ink);border-radius:2px;text-transform:uppercase;
}
.verdict-avoid{border-color:var(--bad);color:var(--bad)}
.verdict-accumulate{border-color:var(--good);color:var(--good)}
.verdict-trim,.verdict-hold{border-color:var(--muted);color:var(--muted)}
table.masthead-meta{width:100%;border-collapse:collapse;margin-top:10px}
table.masthead-meta td{padding:4px 24px 4px 0;vertical-align:top}
.meta-label{display:block;font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.meta-value{display:block;font-family:var(--mono);font-size:12.5px;color:var(--ink)}
h2{
  font-family:var(--mono);font-size:12px;text-transform:uppercase;letter-spacing:.1em;
  border-bottom:1px solid var(--ink);padding-bottom:6px;margin:0 0 14px;
}
.report-section{margin-bottom:30px}
.exec-verdict{font-size:18px;margin:0 0 8px}
.verdict-word{font-weight:700;font-family:var(--mono)}
.confidence-mover{font-style:italic;color:var(--muted);margin:0 0 16px}
table.agent-table{width:100%;border-collapse:collapse;font-size:13px}
table.agent-table th{text-align:right;border-bottom:1px solid var(--ink);padding:6px 8px;font-family:var(--mono);font-size:10px;text-transform:uppercase}
table.agent-table th.l,table.agent-table td.l{text-align:left}
table.agent-table td{padding:6px 8px;border-bottom:1px solid var(--hairline);font-family:var(--mono);text-align:right}
.pos-avoid{color:var(--bad);font-weight:700}
.pos-accumulate{color:var(--good);font-weight:700}
.pos-trim,.pos-hold{color:var(--muted);font-weight:700}
.pos-na,.pos-insufficient-evidence{color:var(--muted);font-style:italic}
.ev-label{font-weight:700}
.advisor-card{border:1px solid var(--hairline);border-radius:3px;padding:16px 18px;margin-bottom:14px}
.advisor-head{display:flex;gap:14px;align-items:baseline;margin-bottom:8px;border-bottom:1px solid var(--hairline);padding-bottom:8px}
.advisor-name{font-family:var(--mono);font-weight:700;font-size:13px}
.advisor-confidence{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--muted)}
.against-callout{
  margin-top:10px;padding:10px 14px;background:#f3ede0;border-left:3px solid var(--accent);
  font-size:13px;
}
.against-label{display:block;font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);margin-bottom:4px}
table.review-table{width:100%;border-collapse:collapse;font-size:12.5px}
table.review-table th{text-align:left;border-bottom:1px solid var(--ink);padding:6px 8px;font-family:var(--mono);font-size:10px;text-transform:uppercase}
table.review-table td{padding:8px;border-bottom:1px solid var(--hairline);vertical-align:top}
.ledger-item{border:1px solid var(--hairline);border-radius:3px;padding:12px 14px;margin-bottom:10px;position:relative}
.status-chip{
  font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:.05em;
  padding:2px 8px;border-radius:999px;float:right;text-transform:uppercase;
}
.status-open{background:#fbe9e7;color:var(--chip-open)}
.status-resolved{background:#e6f4ec;color:var(--chip-resolved)}
.falsification-checklist{list-style:none;padding:0;margin:10px 0 0}
.falsification-checklist li{padding:5px 0 5px 26px;position:relative;border-bottom:1px solid var(--hairline)}
.check-mark{position:absolute;left:0;font-size:15px}
.action-items-list{padding-left:22px}
.action-items-list li{margin-bottom:10px}
.owner-tag{
  font-family:var(--mono);font-size:9px;font-weight:700;text-transform:uppercase;
  background:var(--ink);color:var(--paper);padding:2px 8px;border-radius:2px;margin-right:8px;
}
.risk-list{padding-left:22px}
.risk-list li{margin-bottom:10px}
.dissent-block{border-left:3px solid var(--ink);padding:8px 16px;margin-bottom:14px;background:#f6f4ee}
.dissent-header{font-family:var(--mono);font-weight:700;font-size:12px;margin-bottom:6px}
.absence-marker{color:var(--muted);font-style:italic}
.prose-list{margin:8px 0;padding-left:22px}
.prose-list li{margin-bottom:6px}
.report-footer{margin-top:36px;padding-top:14px;border-top:1px solid var(--hairline);font-family:var(--mono);font-size:10px;color:var(--muted)}
.disclaimer{margin-top:6px;font-style:italic}
@media print{
  body{padding:16mm 18mm}
  .report-section,.advisor-card,.ledger-item,.dissent-block{break-inside:avoid}
  h2{break-after:avoid}
}
"""


def render(
    council_result: CouncilResult,
    quote: Optional[Quote] = None,
    config: Optional[dict] = None,
    company_name: Optional[str] = None,
    flags_status: Optional[dict] = None,
) -> str:
    """Returns a complete, self-contained HTML document — no external CSS,
    fonts, or scripts. Pure function: reads only what's passed in."""
    config = config or {}
    cr = council_result
    sections = cr.chairman.sections or {}

    body = (
        _masthead(cr, quote, config, company_name)
        + _executive_summary(cr)
        + _evidence_status_section(cr, flags_status)
        + _round1_section(cr)
        + _round2_section(cr)
        + _contradiction_section(sections)
        + _thesis_section(sections)
        + _action_items_section(sections)
        + _risk_and_dissent_section(sections)
        + _footer(cr)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(cr.ticker)} — Council Review</title>
<style>{_STYLE}</style>
</head>
<body>
<main class="report">
{body}
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# On-disk cache — HTML and PDF artifacts, keyed identically to
# engine.council's own cache entry (same accession/thesis_tag/
# prompt_version/model, reusing its exact _cache_key()) so a report always
# lives beside the CouncilResult it was generated from and can never
# silently drift onto the wrong ticker/filing/config combination.
# ---------------------------------------------------------------------------

def _report_path(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str, ext: str) -> Path:
    return cache_dir / f"{_cache_key(accession, thesis_tag, prompt_version, model)}.{ext}"


def is_html_cached(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str) -> bool:
    return _report_path(cache_dir, accession, thesis_tag, prompt_version, model, "html").exists()


def load_cached_html(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str) -> Optional[str]:
    p = _report_path(cache_dir, accession, thesis_tag, prompt_version, model, "html")
    return p.read_text(encoding="utf-8") if p.exists() else None


def save_cached_html(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str, html: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _report_path(cache_dir, accession, thesis_tag, prompt_version, model, "html").write_text(html, encoding="utf-8")


def is_pdf_cached(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str) -> bool:
    return _report_path(cache_dir, accession, thesis_tag, prompt_version, model, "pdf").exists()


def load_cached_pdf(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str) -> Optional[bytes]:
    p = _report_path(cache_dir, accession, thesis_tag, prompt_version, model, "pdf")
    return p.read_bytes() if p.exists() else None


def save_cached_pdf(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str, pdf_bytes: bytes) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _report_path(cache_dir, accession, thesis_tag, prompt_version, model, "pdf").write_bytes(pdf_bytes)
