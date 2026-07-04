"""
test_council.py — tests for engine/council.py, Tier 3's adversarial
synthesis boundary.

Every test here mocks the Anthropic client (or calls internal helpers
directly) — nothing makes a real network call. `extract_text()` and the
`thinking={"type": "disabled"}` kwarg are the two hard-won lessons from
experiment/council-multicall (~$3 of live debugging); both get dedicated
structural tests here, same discipline as test_flags.py's
TestVerifyVerbatim being THE key test in that file.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine import council as C
from engine.flags import FilingRef, Flag, FlagsResult


def _filing_ref(accession="0000320193-24-000123") -> FilingRef:
    return FilingRef(
        form="10-K", accession=accession, period_ending="2024-01-28",
        filed="2024-02-21", url="https://example.com/10k.htm",
    )


def _flags_result() -> FlagsResult:
    return FlagsResult(
        ticker="NVDA", model="claude-sonnet-5", prompt_version="v1",
        extracted_at="2026-01-01T00:00:00+00:00", filing=_filing_ref(),
        flags=[Flag(label="Customer concentration", snippet="one customer is 19% of revenue",
                     severity="red", item="1A", verified_verbatim=True)],
        dropped_count=0,
    )


def _bundle(accession="0000320193-24-000123", pre_thesis=True) -> C.EvidenceBundle:
    return C.assemble_bundle(
        ticker="NVDA",
        quant={"company": {"ticker": "NVDA"}, "gaps": [], "config_hash": "abc123"},
        flags_result=_flags_result(),
        thesis=None if pre_thesis else {"thesis": "durable compounder", "durability_at_registration": 72},
        accession=accession,
        config_hash="abc123",
    )


_CFG = {"flags": {"model": "claude-sonnet-5", "pricing": {
    "claude-sonnet-5": {"input_per_million": 2.00, "output_per_million": 10.00},
}}, "council": {"prompt_version": "v1"}}


def _block(text: str, type_: str = "text"):
    b = MagicMock()
    b.type = type_
    b.text = text
    return b


def _response(text: str, input_tokens=100, output_tokens=50, stop_reason="end_turn", blocks=None):
    r = MagicMock()
    r.content = blocks if blocks is not None else [_block(text)]
    r.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    r.stop_reason = stop_reason
    return r


def _round1_text(positions: dict, integrity_note="No known false gaps in this bundle.") -> str:
    parts = ["### EVIDENCE_INTEGRITY_NOTE", integrity_note]
    for name in C._ADVISOR_NAMES:
        pos = positions[name]
        parts.append(f"### {name}")
        parts.append(
            "Cites evidence item A, evidence item B, evidence item C.\n"
            f"POSITION: {pos}\nAGAINST: a real counterpoint.\nCONFIDENCE: 3"
        )
    return "\n\n".join(parts)


_VALID_POSITIONS = {
    "BEAR_ADVOCATE": "AVOID",
    "BULL_STEELMAN": "ACCUMULATE",
    "ASSUMPTION_AUDITOR": "HOLD",
    "BASE_RATE_OUTSIDER": "TRIM",
    "EXECUTION_REALIST": "INSUFFICIENT EVIDENCE",
}


def _chairman_text(verdict="HOLD") -> str:
    return "\n\n".join([
        "### VERDICT",
        f"VERDICT: {verdict}\nCONFIDENCE: 3\nUp: a durability re-rating. Down: a guidance miss.",
        "### CONTRADICTION_LEDGER",
        "None found — RESOLVED.",
        "### THESIS_JOURNAL_DELTA",
        "n/a — pre-thesis mode, no journal entry to edit yet.",
        "### ACTION_ITEMS",
        "1. Watch next 10-Q (user).",
        "### RISK_REGISTER",
        "1. Customer concentration (BEAR_ADVOCATE).",
        "### DISSENT",
        "None — unanimous on direction.",
    ])


def _client_with_responses(responses: list) -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = responses
    return client


def _happy_path_responses(round1_positions=None) -> list:
    round1 = _response(_round1_text(round1_positions or _VALID_POSITIONS))
    reviews = [_response(f"Review text #{i}") for i in range(5)]
    chairman = _response(_chairman_text())
    return [round1] + reviews + [chairman]


# ---------------------------------------------------------------------------
# extract_text() — THE key safety mechanism against thinking-only truncation.
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_text_only_response(self):
        assert C.extract_text([_block("hello")]) == "hello"

    def test_thinking_then_text_concatenates_only_text(self):
        blocks = [_block("reasoning...", type_="thinking"), _block("final answer")]
        assert C.extract_text(blocks) == "final answer"

    def test_multiple_text_blocks_concatenated(self):
        blocks = [_block("part one "), _block("part two")]
        assert C.extract_text(blocks) == "part one part two"

    def test_thinking_only_raises_specific_truncation_message(self):
        blocks = [_block("reasoning...", type_="thinking")]
        with pytest.raises(RuntimeError, match="thinking-only"):
            C.extract_text(blocks)

    def test_no_blocks_at_all_raises_generic_message(self):
        with pytest.raises(RuntimeError, match="No text block found"):
            C.extract_text([])


# ---------------------------------------------------------------------------
# _call() — structural test: thinking must be disabled on EVERY call.
# ---------------------------------------------------------------------------

class TestCallDisablesThinking:
    def test_thinking_disabled_kwarg_present(self):
        client = _client_with_responses([_response("hi")])
        C._call(client, "claude-sonnet-5", "system", "user", max_tokens=3000,
                call_type="council_opinions", call_info=None)
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["thinking"] == {"type": "disabled"}

    def test_max_tokens_passed_through(self):
        client = _client_with_responses([_response("hi")])
        C._call(client, "claude-sonnet-5", "system", "user", max_tokens=6000,
                call_type="council_chairman", call_info=None)
        assert client.messages.create.call_args.kwargs["max_tokens"] == 6000

    def test_call_info_none_is_a_no_op(self):
        client = _client_with_responses([_response("hi")])
        C._call(client, "claude-sonnet-5", "s", "u", max_tokens=100, call_type="council_review", call_info=None)
        # no exception, nothing to assert beyond "didn't crash"

    def test_call_info_appends_one_record_with_stop_reason_and_tokens(self):
        client = _client_with_responses([_response("hi", input_tokens=500, output_tokens=42, stop_reason="end_turn")])
        call_info = []
        C._call(client, "claude-sonnet-5", "s", "u", max_tokens=100, call_type="council_review", call_info=call_info)
        assert call_info == [{
            "call_type": "council_review", "input_tokens": 500, "output_tokens": 42, "stop_reason": "end_turn",
        }]


# ---------------------------------------------------------------------------
# Section splitting / advisor parsing / position taxonomy
# ---------------------------------------------------------------------------

class TestSplitSectionsAndParsing:
    def test_splits_named_sections(self):
        text = "### FOO\nfoo body\n\n### BAR\nbar body"
        sections = C._split_sections(text)
        assert sections == {"FOO": "foo body", "BAR": "bar body"}

    def test_no_headers_returns_empty_dict(self):
        assert C._split_sections("just plain text, no headers") == {}

    def test_valid_position_parses_and_normalizes(self):
        block = "text\nPOSITION: AVOID\nAGAINST: x\nCONFIDENCE: 4"
        opinion = C._parse_advisor_block("BEAR_ADVOCATE", block)
        assert opinion.position == "AVOID"
        assert opinion.confidence == 4
        assert opinion.parsed is True

    def test_off_taxonomy_position_is_unparsed(self):
        block = "text\nPOSITION: Overvalued / SELL-side skeptic\nCONFIDENCE: 3"
        opinion = C._parse_advisor_block("ASSUMPTION_AUDITOR", block)
        assert opinion.position is None
        assert opinion.parsed is False

    def test_missing_position_line_is_unparsed(self):
        opinion = C._parse_advisor_block("EXECUTION_REALIST", "no position line here at all")
        assert opinion.position is None
        assert opinion.parsed is False

    def test_position_with_trailing_text_still_matches_prefix(self):
        block = "POSITION: HOLD (pending further data)\nCONFIDENCE: 2"
        opinion = C._parse_advisor_block("ASSUMPTION_AUDITOR", block)
        assert opinion.position == "HOLD"

    def test_confidence_out_of_range_is_none(self):
        assert C._extract_confidence("CONFIDENCE: 9") is None

    def test_chairman_parses_verdict_and_all_sections(self):
        chairman = C._parse_chairman(_chairman_text(verdict="TRIM"))
        assert chairman.verdict == "TRIM"
        assert chairman.confidence == 3
        assert chairman.parsed is True
        assert set(chairman.sections) == {
            "contradiction_ledger", "thesis_journal_delta", "action_items", "risk_register", "dissent",
        }

    def test_chairman_missing_sections_is_unparsed_but_keeps_raw_text(self):
        chairman = C._parse_chairman("no headers, no verdict, just prose")
        assert chairman.parsed is False
        assert chairman.verdict is None
        assert chairman.text == "no headers, no verdict, just prose"


# ---------------------------------------------------------------------------
# Anonymization — Round 2 must never leak advisor role names.
# ---------------------------------------------------------------------------

class TestAnonymizeOthers:
    def _advisors(self):
        return [C._parse_advisor_block(name, f"body for {name}\nPOSITION: HOLD\nCONFIDENCE: 3")
                for name in C._ADVISOR_NAMES]

    def test_excludes_own_opinion(self):
        advisors = self._advisors()
        anon = C._anonymize_others(advisors, "BEAR_ADVOCATE")
        assert "body for BEAR_ADVOCATE" not in anon

    def test_includes_all_four_others(self):
        advisors = self._advisors()
        anon = C._anonymize_others(advisors, "BEAR_ADVOCATE")
        for name in C._ADVISOR_NAMES:
            if name != "BEAR_ADVOCATE":
                assert f"body for {name}" in anon

    def test_no_advisor_role_name_appears_as_a_label(self):
        advisors = self._advisors()
        anon = C._anonymize_others(advisors, "BULL_STEELMAN")
        for name in C._ADVISOR_NAMES:
            assert f"=== {name} ===" not in anon
        assert "=== Opinion A ===" in anon

    def test_labels_exactly_four_opinions(self):
        advisors = self._advisors()
        anon = C._anonymize_others(advisors, "BULL_STEELMAN")
        for letter in "ABCD":
            assert f"=== Opinion {letter} ===" in anon


# ---------------------------------------------------------------------------
# convene() — the full 7-call orchestration
# ---------------------------------------------------------------------------

class TestConvene:
    def test_seven_calls_in_order(self):
        client = _client_with_responses(_happy_path_responses())
        call_info = []
        C.convene("NVDA", _bundle(), _CFG, client, call_info=call_info)
        assert client.messages.create.call_count == 7
        types = [c["call_type"] for c in call_info]
        assert types == (
            ["council_opinions"] + ["council_review"] * 5 + ["council_chairman"]
        )

    def test_advisor_positions_parsed_correctly(self):
        client = _client_with_responses(_happy_path_responses())
        result = C.convene("NVDA", _bundle(), _CFG, client, call_info=[])
        by_name = {a.name: a for a in result.advisors}
        assert by_name["BEAR_ADVOCATE"].position == "AVOID"
        assert by_name["BULL_STEELMAN"].position == "ACCUMULATE"
        assert all(a.parsed for a in result.advisors)

    def test_reviews_have_one_per_advisor_seat(self):
        client = _client_with_responses(_happy_path_responses())
        result = C.convene("NVDA", _bundle(), _CFG, client, call_info=[])
        assert [r.reviewer for r in result.reviews] == list(C._ADVISOR_NAMES)

    def test_chairman_parsed(self):
        client = _client_with_responses(_happy_path_responses())
        result = C.convene("NVDA", _bundle(), _CFG, client, call_info=[])
        assert result.chairman.verdict == "HOLD"
        assert result.chairman.parsed is True

    def test_evidence_integrity_note_stamped_into_meta(self):
        client = _client_with_responses(
            _happy_path_responses(),
        )
        result = C.convene("NVDA", _bundle(), _CFG, client, call_info=[])
        assert "No known false gaps" in result.meta.evidence_integrity_note

    def test_clean_run_has_no_status_flags(self):
        client = _client_with_responses(_happy_path_responses())
        result = C.convene("NVDA", _bundle(), _CFG, client, call_info=[])
        assert result.meta.status_flags == []

    def test_total_tokens_and_cost_summed_across_all_seven_calls(self):
        responses = [_response(_round1_text(_VALID_POSITIONS), input_tokens=1000, output_tokens=200)]
        responses += [_response(f"r{i}", input_tokens=500, output_tokens=50) for i in range(5)]
        responses += [_response(_chairman_text(), input_tokens=2000, output_tokens=400)]
        client = _client_with_responses(responses)
        result = C.convene("NVDA", _bundle(), _CFG, client, call_info=[])
        assert result.meta.total_input_tokens == 1000 + 5 * 500 + 2000
        assert result.meta.total_output_tokens == 200 + 5 * 50 + 400
        assert result.meta.total_cost_usd > 0

    def test_reviewer_input_never_contains_an_advisor_role_name(self):
        client = _client_with_responses(_happy_path_responses())
        C.convene("NVDA", _bundle(), _CFG, client, call_info=[])
        review_calls = client.messages.create.call_args_list[1:6]
        for call in review_calls:
            user_msg = call.kwargs["messages"][0]["content"]
            for name in C._ADVISOR_NAMES:
                assert f"=== {name} ===" not in user_msg

    def test_off_taxonomy_position_triggers_one_retry_then_recovers(self):
        bad_positions = dict(_VALID_POSITIONS)
        bad_positions["ASSUMPTION_AUDITOR"] = "Overvalued / SELL-side skeptic"
        responses = [
            _response(_round1_text(bad_positions)),          # Round 1 attempt 1 — violation
            _response(_round1_text(_VALID_POSITIONS)),        # Round 1 attempt 2 (retry) — clean
        ]
        responses += [_response(f"r{i}") for i in range(5)]
        responses += [_response(_chairman_text())]
        client = _client_with_responses(responses)
        call_info = []
        result = C.convene("NVDA", _bundle(), _CFG, client, call_info=call_info)
        assert client.messages.create.call_count == 8  # one extra Round 1 call
        assert result.meta.status_flags == []  # retry succeeded, no degradation
        assert result.advisors[2].position == "HOLD"  # ASSUMPTION_AUDITOR recovered

    def test_off_taxonomy_position_still_bad_after_retry_marks_format_degraded(self):
        bad_positions = dict(_VALID_POSITIONS)
        bad_positions["ASSUMPTION_AUDITOR"] = "Overvalued / SELL-side skeptic"
        responses = [
            _response(_round1_text(bad_positions)),  # attempt 1 — violation
            _response(_round1_text(bad_positions)),  # attempt 2 (retry) — still violates
        ]
        responses += [_response(f"r{i}") for i in range(5)]
        responses += [_response(_chairman_text())]
        client = _client_with_responses(responses)
        result = C.convene("NVDA", _bundle(), _CFG, client, call_info=[])
        assert client.messages.create.call_count == 8
        assert "format_degraded" in result.meta.status_flags
        # raw text is preserved even though position didn't parse
        assert result.advisors[2].parsed is False
        assert "Overvalued" in result.advisors[2].text

    def test_max_tokens_stop_reason_marks_truncated(self):
        responses = _happy_path_responses()
        responses[-1] = _response(_chairman_text(), stop_reason="max_tokens")  # chairman truncated
        client = _client_with_responses(responses)
        result = C.convene("NVDA", _bundle(), _CFG, client, call_info=[])
        assert "truncated" in result.meta.status_flags

    def test_partial_calls_logged_before_a_mid_run_failure(self):
        """A failure on call 4 (the third review) must not erase the
        usage records for the three calls that already completed."""
        responses = [_response(_round1_text(_VALID_POSITIONS))]
        responses += [_response("r0"), _response("r1"), RuntimeError("SDK exploded")]
        client = MagicMock()
        client.messages.create.side_effect = responses
        call_info = []
        with pytest.raises(RuntimeError, match="SDK exploded"):
            C.convene("NVDA", _bundle(), _CFG, client, call_info=call_info)
        assert len(call_info) == 3  # round1 + 2 successful reviews, logged despite the failure
        assert [c["call_type"] for c in call_info] == ["council_opinions", "council_review", "council_review"]


# ---------------------------------------------------------------------------
# Caching — only a fully successful convene() is ever cached.
# ---------------------------------------------------------------------------

class TestCaching:
    def test_not_cached_before_any_run(self, tmp_path):
        assert C.is_cached(tmp_path, "acc-1", "prethesis", "v1", "claude-sonnet-5") is False

    def test_get_council_runs_and_caches_on_first_call(self, tmp_path):
        client = _client_with_responses(_happy_path_responses())
        bundle = _bundle(accession="acc-1")
        result = C.get_council("NVDA", bundle, _CFG, client, tmp_path, call_info=[])
        assert isinstance(result, C.CouncilResult)
        assert C.is_cached(tmp_path, "acc-1", "prethesis", "v1", "claude-sonnet-5") is True

    def test_second_call_is_a_cache_hit_no_new_model_calls(self, tmp_path):
        client = _client_with_responses(_happy_path_responses())
        bundle = _bundle(accession="acc-2")
        C.get_council("NVDA", bundle, _CFG, client, tmp_path, call_info=[])
        call_info2 = []
        C.get_council("NVDA", bundle, _CFG, client, tmp_path, call_info=call_info2)
        assert client.messages.create.call_count == 7  # not 14
        assert call_info2 == []  # nothing new happened

    def test_force_refresh_reconvenes(self, tmp_path):
        client = _client_with_responses(_happy_path_responses() + _happy_path_responses())
        bundle = _bundle(accession="acc-3")
        C.get_council("NVDA", bundle, _CFG, client, tmp_path, call_info=[])
        call_info2 = []
        C.get_council("NVDA", bundle, _CFG, client, tmp_path, force_refresh=True, call_info=call_info2)
        assert client.messages.create.call_count == 14
        assert len(call_info2) == 7

    def test_failed_run_is_never_cached(self, tmp_path):
        responses = [_response(_round1_text(_VALID_POSITIONS))]
        responses += [RuntimeError("boom")]
        client = MagicMock()
        client.messages.create.side_effect = responses
        bundle = _bundle(accession="acc-4")
        with pytest.raises(RuntimeError):
            C.get_council("NVDA", bundle, _CFG, client, tmp_path, call_info=[])
        assert C.is_cached(tmp_path, "acc-4", "prethesis", "v1", "claude-sonnet-5") is False

    def test_load_cached_round_trips_all_fields(self, tmp_path):
        client = _client_with_responses(_happy_path_responses())
        bundle = _bundle(accession="acc-5")
        original = C.get_council("NVDA", bundle, _CFG, client, tmp_path, call_info=[])
        loaded = C.load_cached(tmp_path, "acc-5", "prethesis", "v1", "claude-sonnet-5")
        assert loaded.ticker == original.ticker
        assert [a.name for a in loaded.advisors] == [a.name for a in original.advisors]
        assert loaded.chairman.verdict == original.chairman.verdict
        assert loaded.meta.config_hash == original.meta.config_hash

    def test_different_accession_is_a_separate_cache_entry(self, tmp_path):
        client = _client_with_responses(_happy_path_responses() + _happy_path_responses())
        C.get_council("NVDA", _bundle(accession="acc-a"), _CFG, client, tmp_path, call_info=[])
        C.get_council("NVDA", _bundle(accession="acc-b"), _CFG, client, tmp_path, call_info=[])
        assert client.messages.create.call_count == 14


# ---------------------------------------------------------------------------
# Cost helpers — absence-is-not-zero for a real API call's cost.
# ---------------------------------------------------------------------------

class TestCostHelpers:
    def test_estimate_positive_when_pricing_present(self):
        est = C.estimate_council_cost_usd(_CFG, "some bundle text")
        assert est is not None and est > 0

    def test_estimate_none_when_pricing_missing(self):
        cfg = {"flags": {"model": "claude-sonnet-5", "pricing": {}}}
        assert C.estimate_council_cost_usd(cfg, "some bundle text") is None

    def test_estimate_none_when_bundle_text_missing(self):
        """An estimate that can't see the evidence it would be sizing is
        not a number worth reporting — same absence-is-not-zero discipline
        as the missing-pricing case."""
        assert C.estimate_council_cost_usd(_CFG, None) is None
        assert C.estimate_council_cost_usd(_CFG, "") is None

    def test_larger_bundle_estimates_more_expensive_than_a_smaller_one(self):
        """The whole point of the fix: a fixed baseline couldn't distinguish
        a small evidence bundle from a large one, and was wrong-low by more
        than 2x on a real CAT convene as a result."""
        small = C.estimate_council_cost_usd(_CFG, "x" * 4_000)
        large = C.estimate_council_cost_usd(_CFG, "x" * 200_000)
        assert small < large

    def test_cat_sized_bundle_lands_near_the_observed_real_cost(self):
        """A real live convene on CAT (~50k-token bundle, ~200k chars at the
        chars-per-token heuristic) cost $0.83 for all 7 calls. This asserts
        a band, not an exact value — it's an estimate, and output size in
        particular varies run to run."""
        cat_sized_bundle = "x" * 200_000
        est = C.estimate_council_cost_usd(_CFG, cat_sized_bundle)
        assert 0.60 <= est <= 1.10

    def test_compute_none_when_pricing_missing(self):
        cfg = {"flags": {"model": "claude-sonnet-5", "pricing": {}}}
        assert C.compute_council_cost_usd(cfg, "claude-sonnet-5", 1000, 100) is None

    def test_compute_matches_hand_calculated_value(self):
        cost = C.compute_council_cost_usd(_CFG, "claude-sonnet-5", 1_000_000, 1_000_000)
        assert cost == pytest.approx(12.0)  # 1M @ $2/M + 1M @ $10/M

    def test_compute_treats_missing_token_counts_as_zero(self):
        assert C.compute_council_cost_usd(_CFG, "claude-sonnet-5", None, None) == 0.0


# ---------------------------------------------------------------------------
# validate_council_config()
# ---------------------------------------------------------------------------

class TestValidateCouncilConfig:
    def test_no_council_section_is_fine(self):
        C.validate_council_config({})

    def test_valid_config_passes(self):
        C.validate_council_config({"council": {"prompt_version": "v1"}})

    def test_non_string_prompt_version_raises(self):
        with pytest.raises(ValueError, match="prompt_version"):
            C.validate_council_config({"council": {"prompt_version": 1}})

    def test_real_config_yaml_validates(self):
        import yaml
        repo_root = Path(__file__).resolve().parent.parent
        with open(repo_root / "config.yaml") as f:
            cfg = yaml.safe_load(f)
        C.validate_council_config(cfg)


# ---------------------------------------------------------------------------
# EvidenceBundle / render_bundle_text
# ---------------------------------------------------------------------------

class TestBundle:
    def test_pre_thesis_bundle_has_no_thesis_hash(self):
        bundle = _bundle(pre_thesis=True)
        assert bundle.pre_thesis is True
        assert bundle.thesis_hash is None

    def test_thesis_present_bundle_gets_a_hash(self):
        bundle = _bundle(pre_thesis=False)
        assert bundle.pre_thesis is False
        assert bundle.thesis_hash is not None
        assert len(bundle.thesis_hash) == 16

    def test_render_bundle_text_includes_pre_thesis_note(self):
        text = C.render_bundle_text(_bundle(pre_thesis=True))
        assert "PRE-THESIS MODE" in text

    def test_render_bundle_text_includes_quant_and_flags_json(self):
        text = C.render_bundle_text(_bundle())
        assert "NVDA" in text
        assert "Customer concentration" in text
