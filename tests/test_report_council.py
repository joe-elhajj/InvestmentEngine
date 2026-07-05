"""
test_report_council.py — tests for engine/report_council.py's render().

Two fixtures: a "full" CouncilResult with every section populated and
realistic light-markdown prose (mirroring the exact conventions real CAT/
NVDA/META convenes on record produced — **bold**, `code`, "- " bullets,
"1. " numbered lists, "**OPEN**"/"**RESOLVED**" markers, "**Owner: X.**"
tags, "- **NAME (POSITION, confidence N)**" dissent headers), and a
"sparse" CouncilResult with an unparsed chairman (empty sections), no
AGAINST lines, no confidence-mover text, and an unparseable review — every
one of these must render an explicit absence marker, never silently
disappear the section (absence-is-not-zero applies to report sections
too, same as every other renderer in this codebase).
"""

from __future__ import annotations

from engine import report_council as RC
from engine.council import AdvisorOpinion, ChairmanOutput, CouncilMeta, CouncilResult, ReviewNote
from engine.market import Quote


def _meta(**overrides) -> CouncilMeta:
    base = dict(
        ticker="NVDA", model="claude-sonnet-5", prompt_version="v1", config_hash="ed3a4fdc366f8f1f",
        convened_at="2026-07-05T02:57:17+00:00", accession="0001045810-26-000021",
        thesis_status="pre_thesis", thesis_hash=None, evidence_integrity_note="none",
        total_cost_usd=0.89, total_input_tokens=399005, total_output_tokens=9463,
        status_flags=[], calls=[],
    )
    base.update(overrides)
    return CouncilMeta(**base)


_ADVISOR_TEXT = """The company trades at a valuation implying hypergrowth continuation. \
Per `dcf.base.upside_vs_price` the base case shows -62.3% downside. Multiple red flags \
point to fragility: "extended lead times of more than 12 months" (FLAGS, red).

POSITION: AVOID
AGAINST: The company has delivered 94.3% 5yr net income CAGR, vastly exceeding priced-in growth.
CONFIDENCE: 3"""

_REVIEW_TEXT_PARSED = """**Strongest: B.** It grounds the crux in the sensitivity grid itself \
and flags a real methodological issue.

**Weakest: A.** It dismisses red flags as benign without evidence.

**Contradiction:** B claims the bull case is conservative while C uses the same data to argue \
deceleration is already priced in."""

_REVIEW_TEXT_UNPARSED = "Free-form review text that never uses the bold Strongest/Weakest/Contradiction convention."

_CHAIRMAN_TEXT = """### VERDICT

VERDICT: TRIM
CONFIDENCE: 2
Confidence would move UP a notch if growth holds above 30%; confidence would move DOWN a notch \
if guidance misses.

Note on advisor naming: some unrelated aside that should not appear in the mover text.

### CONTRADICTION_LEDGER

1. **Growth durability.** Advisor A cites deceleration; Advisor B cites durability. **OPEN** — \
settled by observing the next two quarters.

2. **Base rate applicability.** BASE_RATE_OUTSIDER invokes reversion; BEAR_ADVOCATE concedes NVDA \
has defied it for years. **RESOLVED (partially)** — both sides agree this is a prior, not a fact.

### THESIS_JOURNAL_DELTA

This is confirmed **PRE-THESIS MODE** — no existing entry to audit.

- **New entry required specifying operative DCF case.** *Trigger: draft by next council cycle.*
- **Falsification trigger 1 — margin durability:** IF gross margin falls below 70%, THEN reassess.

### ACTION_ITEMS

1. **Owner: User.** Draft the formal thesis journal entry before any trade executes.
2. **Owner: Engine backlog.** Populate missing relative-value peer medians.

### RISK_REGISTER

1. **Valuation risk — no DCF scenario rationalizes current price.** Surfaced by BEAR_ADVOCATE.
2. **Operational fragility — supply constraints.** Surfaced by BEAR_ADVOCATE and BULL_STEELMAN.

### DISSENT

- **BULL_STEELMAN (ACCUMULATE, confidence 2)** dissents sharply, arguing the DCF architecture is \
conservative relative to delivered growth.
"""


def _full_council_result() -> CouncilResult:
    advisors = [
        AdvisorOpinion(name="BEAR_ADVOCATE", position="AVOID", confidence=3, text=_ADVISOR_TEXT, parsed=True),
        AdvisorOpinion(name="BULL_STEELMAN", position="ACCUMULATE", confidence=2, text=_ADVISOR_TEXT, parsed=True),
    ]
    reviews = [
        ReviewNote(reviewer="BEAR_ADVOCATE", text=_REVIEW_TEXT_PARSED),
        ReviewNote(reviewer="BULL_STEELMAN", text=_REVIEW_TEXT_UNPARSED),
    ]
    chairman = ChairmanOutput(
        verdict="TRIM", confidence=2, text=_CHAIRMAN_TEXT,
        sections={
            "contradiction_ledger": (
                "1. **Growth durability.** Advisor A cites deceleration; Advisor B cites durability. "
                "**OPEN** — settled by observing the next two quarters.\n\n"
                "2. **Base rate applicability.** BASE_RATE_OUTSIDER invokes reversion; BEAR_ADVOCATE "
                "concedes NVDA has defied it for years. **RESOLVED (partially)** — both sides agree "
                "this is a prior, not a fact."
            ),
            "thesis_journal_delta": (
                "This is confirmed **PRE-THESIS MODE** — no existing entry to audit.\n\n"
                "- **New entry required specifying operative DCF case.** *Trigger: draft by next council cycle.*\n"
                "- **Falsification trigger 1 — margin durability:** IF gross margin falls below 70%, THEN reassess."
            ),
            "action_items": (
                "1. **Owner: User.** Draft the formal thesis journal entry before any trade executes.\n\n"
                "2. **Owner: Engine backlog.** Populate missing relative-value peer medians."
            ),
            "risk_register": (
                "1. **Valuation risk — no DCF scenario rationalizes current price.** Surfaced by BEAR_ADVOCATE.\n\n"
                "2. **Operational fragility — supply constraints.** Surfaced by BEAR_ADVOCATE and BULL_STEELMAN."
            ),
            "dissent": (
                "- **BULL_STEELMAN (ACCUMULATE, confidence 2)** dissents sharply, arguing the DCF "
                "architecture is conservative relative to delivered growth."
            ),
        },
        parsed=True,
    )
    return CouncilResult(ticker="NVDA", advisors=advisors, reviews=reviews, chairman=chairman, meta=_meta())


def _sparse_council_result() -> CouncilResult:
    """Unparsed chairman (no sections at all), advisors with no AGAINST
    line, an unparseable chairman.text (no VERDICT block at all — no
    confidence-mover derivable), and a review with no bold markers."""
    advisors = [
        AdvisorOpinion(name="BEAR_ADVOCATE", position=None, confidence=None, text="No structured fields here at all.", parsed=False),
    ]
    reviews = [ReviewNote(reviewer="BEAR_ADVOCATE", text="Totally unstructured review prose.")]
    chairman = ChairmanOutput(verdict=None, confidence=None, text="unparseable garbage with no headers", sections={}, parsed=False)
    return CouncilResult(ticker="TEST", advisors=advisors, reviews=reviews, chairman=chairman, meta=_meta(ticker="TEST"))


_QUOTE = Quote(ticker="NVDA", price=194.83, shares_outstanding=24_000.0, market_cap=4_700_000_000_000.0, source="test")
_CONFIG = {"universe": {"version": "2026-Q3"}}
_FLAGS_STATUS = {
    "cached": True, "form": "10-K", "accession": "0001045810-26-000021",
    "period_ending": "2026-01-25", "filed": "2026-02-20", "count": 7,
}


class TestMasthead:
    def test_ticker_verdict_and_meta_present(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG, company_name="NVIDIA Corporation")
        assert ">NVDA<" in html
        assert "NVIDIA Corporation" in html
        assert 'verdict-badge verdict-trim">TRIM</div>' in html
        assert "claude-sonnet-5" in html
        assert "ed3a4fdc366f8f1f" in html
        assert "2026-Q3" in html

    def test_price_and_market_cap_rendered(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "$194.83" in html
        assert "$4.70T" in html

    def test_missing_quote_renders_na_not_zero(self):
        html = RC.render(_full_council_result(), quote=None, config=_CONFIG)
        assert "n/a" in html
        assert "$0" not in html

    def test_missing_company_name_omits_it_without_crashing(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG, company_name=None)
        assert ">NVDA<" in html


class TestExecutiveSummary:
    def test_verdict_confidence_and_agent_table_present(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "confidence 2/5" in html
        assert "BEAR_ADVOCATE" in html and "BULL_STEELMAN" in html
        assert 'pos-avoid">AVOID' in html
        assert 'pos-accumulate">ACCUMULATE' in html

    def test_confidence_mover_extracted_and_stray_note_excluded(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "Confidence would move UP a notch if growth holds above 30%" in html
        assert "confidence would move DOWN a notch if guidance misses" in html
        assert "Note on advisor naming" not in html

    def test_missing_confidence_mover_renders_absence_marker(self):
        html = RC.render(_sparse_council_result(), quote=None, config=_CONFIG)
        assert "No confidence-mover statement recorded for this run." in html


class TestEvidenceStatus:
    def test_flags_cached_status_shows_filing_details(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG, flags_status=_FLAGS_STATUS)
        assert "10-K" in html
        assert "7 flag(s) extracted" in html

    def test_flags_not_cached_shows_absence_marker(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG, flags_status={"cached": False})
        assert "No cached Tier 2 flag extraction for this filing." in html

    def test_flags_status_none_shows_absence_marker(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG, flags_status=None)
        assert "Flags cache status not available to this render." in html

    def test_pre_thesis_status_shown(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "Pre-thesis" in html


class TestRound1Opinions:
    def test_each_advisor_gets_a_card_with_position_and_confidence(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert html.count('class="advisor-card"') == 2
        assert "confidence 3/5" in html

    def test_reasoning_body_excludes_trailing_position_against_confidence_lines(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "hypergrowth continuation" in html  # the real reasoning prose survives
        assert "POSITION: AVOID" not in html
        assert "CONFIDENCE: 3" not in html

    def test_against_line_pulled_into_its_own_callout(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "Strongest point against its own position" in html
        assert "delivered 94.3% 5yr net income CAGR" in html

    def test_missing_against_line_renders_absence_marker(self):
        html = RC.render(_sparse_council_result(), quote=None, config=_CONFIG)
        assert 'No "against" line recorded for this advisor.' in html


class TestRound2PeerReview:
    def test_parsed_review_renders_as_three_columns(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "grounds the crux in the sensitivity grid" in html
        assert "dismisses red flags as benign" in html
        assert "bull case is conservative" in html

    def test_unparseable_review_falls_back_to_full_text_not_dropped(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "Free-form review text that never uses the bold" in html
        assert 'colspan="3"' in html


class TestContradictionLedger:
    def test_open_and_resolved_chips_rendered(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert 'status-chip status-open">OPEN<' in html
        assert 'status-chip status-resolved">RESOLVED (PARTIALLY)<' in html

    def test_absent_ledger_renders_explicit_marker(self):
        html = RC.render(_sparse_council_result(), quote=None, config=_CONFIG)
        assert "Not present in this council run's cached output." in html


class TestThesisJournalDelta:
    def test_intro_and_checklist_rendered(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "PRE-THESIS MODE" in html
        assert 'class="falsification-checklist"' in html
        assert "New entry required specifying operative DCF case" in html
        assert "Falsification trigger 1" in html


class TestActionItems:
    def test_owner_tags_preserved(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert '<span class="owner-tag">User</span>' in html
        assert '<span class="owner-tag">Engine backlog</span>' in html


class TestRiskAndDissent:
    def test_risk_register_items_rendered(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "no DCF scenario rationalizes current price" in html

    def test_dissent_block_has_signed_header(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert '<div class="dissent-header">BULL_STEELMAN (ACCUMULATE, confidence 2)</div>' in html

    def test_absent_dissent_renders_marker(self):
        html = RC.render(_sparse_council_result(), quote=None, config=_CONFIG)
        assert "No dissent recorded for this run." in html


class TestFooterAndSafety:
    def test_footer_has_accession_and_disclaimer(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert "0001045810-26-000021" in html
        assert "not investment advice" in html

    def test_output_is_a_complete_self_contained_document(self):
        html = RC.render(_full_council_result(), _QUOTE, _CONFIG)
        assert html.strip().startswith("<!doctype html>")
        assert "<style>" in html
        assert "<script" not in html  # no scripts — a static report, not the dashboard

    def test_html_injection_in_model_text_is_escaped(self):
        """Model text is untrusted prose from an LLM — a stray <script> or
        raw HTML tag anywhere in it must never survive into the DOM
        unescaped."""
        advisors = [AdvisorOpinion(
            name="BEAR_ADVOCATE", position="AVOID", confidence=3,
            text='<script>alert(1)</script>\nPOSITION: AVOID\nAGAINST: <img src=x onerror=alert(1)>\nCONFIDENCE: 3',
            parsed=True,
        )]
        chairman = ChairmanOutput(verdict="AVOID", confidence=3, text="no verdict block", sections={}, parsed=False)
        cr = CouncilResult(ticker="XSS", advisors=advisors, reviews=[], chairman=chairman, meta=_meta(ticker="XSS"))
        html = RC.render(cr, quote=None, config=_CONFIG)
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;" in html
