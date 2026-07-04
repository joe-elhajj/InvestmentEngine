"""
test_flags.py — tests for engine/flags.py, Tier 2's LLM boundary.

verify_verbatim() is THE key test in this file: it's the only thing
standing between a model's output and a fabricated quote reaching a
user. Every other test here mocks the model call (engine.flags._call_model)
— nothing in this file makes a real network/API call.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.flags import (
    Flag,
    FilingRef,
    FlagsResult,
    apply_overrides,
    compute_cost_usd,
    estimate_extraction_cost_usd,
    extract_flags_raw,
    get_flags,
    is_cached,
    validate_flags_config,
    verify_verbatim,
)


def _filing_ref(accession="0000320193-24-000123") -> FilingRef:
    return FilingRef(
        form="10-K", accession=accession, period_ending="2024-01-28",
        filed="2024-02-21", url="https://example.com/10k.htm",
    )


_CFG = {"flags": {"model": "claude-sonnet-5", "temperature": 0, "prompt_version": "v1"}}


def _mock_model_response(payload: list) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(payload)
    response = MagicMock()
    response.content = [block]
    return response


# ---------------------------------------------------------------------------
# verify_verbatim() — THE key test.
# ---------------------------------------------------------------------------

class TestVerifyVerbatim:
    def test_exact_span_passes(self):
        source = "Our competitor XYZ Corp filed a lawsuit against us in March 2024."
        snippet = "Our competitor XYZ Corp filed a lawsuit against us in March 2024."
        assert verify_verbatim(snippet, source) is True

    def test_exact_substring_of_longer_source_passes(self):
        source = "Preamble text. Our competitor XYZ Corp filed a lawsuit against us in March 2024. Trailing text."
        snippet = "Our competitor XYZ Corp filed a lawsuit against us in March 2024."
        assert verify_verbatim(snippet, source) is True

    def test_fabricated_snippet_not_in_source_is_rejected(self):
        source = "Our competitor XYZ Corp filed a lawsuit against us in March 2024."
        snippet = "This sentence was invented by the model and never appeared in the filing."
        assert verify_verbatim(snippet, source) is False

    def test_paraphrased_snippet_is_rejected(self):
        """Close-but-not-exact must fail — this is not a fuzzy matcher."""
        source = "Our competitor XYZ Corp filed a lawsuit against us in March 2024."
        snippet = "XYZ Corp, a competitor, sued the company in March 2024."
        assert verify_verbatim(snippet, source) is False

    def test_case_or_whitespace_altered_snippet_is_rejected(self):
        """Deliberately dumb: no normalization of any kind."""
        source = "Our competitor XYZ Corp filed a lawsuit against us in March 2024."
        assert verify_verbatim("our competitor xyz corp filed a lawsuit against us in march 2024.", source) is False
        assert verify_verbatim("Our competitor XYZ Corp filed a lawsuit  against us in March 2024.", source) is False

    def test_empty_snippet_is_rejected(self):
        assert verify_verbatim("", "some source text") is False

    def test_empty_source_is_rejected(self):
        assert verify_verbatim("some snippet", "") is False


# ---------------------------------------------------------------------------
# extract_flags_raw() — end-to-end drop/keep + dropped_count + logging
# ---------------------------------------------------------------------------

class TestExtractFlagsRaw:
    def test_verbatim_snippet_is_kept_and_marked_verified(self):
        sections = {"1A": "Our competitor XYZ Corp filed a lawsuit against us in March 2024."}
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([
            {"label": "Patent lawsuit", "snippet": "Our competitor XYZ Corp filed a lawsuit against us in March 2024.",
             "severity": "red", "item": "1A"},
        ])
        result = extract_flags_raw("NVDA", sections, _filing_ref(), _CFG, client)
        assert len(result.flags) == 1
        assert result.flags[0].verified_verbatim is True
        assert result.flags[0].source == "model"
        assert result.dropped_count == 0

    def test_fabricated_snippet_is_dropped_and_counted(self, capsys):
        sections = {"1A": "Our competitor XYZ Corp filed a lawsuit against us in March 2024."}
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([
            {"label": "Fabricated", "snippet": "This text does not appear anywhere in the filing.",
             "severity": "red", "item": "1A"},
        ])
        result = extract_flags_raw("NVDA", sections, _filing_ref(), _CFG, client)
        assert len(result.flags) == 0
        assert result.dropped_count == 1
        assert "! NVDA: dropped non-verbatim snippet" in capsys.readouterr().err

    def test_mixed_batch_keeps_real_drops_fake(self):
        sections = {"1A": "Our competitor XYZ Corp filed a lawsuit against us in March 2024."}
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([
            {"label": "Patent lawsuit", "snippet": "Our competitor XYZ Corp filed a lawsuit against us in March 2024.",
             "severity": "red", "item": "1A"},
            {"label": "Fabricated", "snippet": "Not in the filing.", "severity": "red", "item": "1A"},
        ])
        result = extract_flags_raw("NVDA", sections, _filing_ref(), _CFG, client)
        assert len(result.flags) == 1
        assert result.flags[0].label == "Patent lawsuit"
        assert result.dropped_count == 1

    def test_snippet_validated_against_its_own_cited_item_only(self):
        """A snippet that's verbatim in Item 1 but claims to be from Item 1A
        must still be dropped — validation is per-item, not whole-document."""
        sections = {
            "1": "We sell widgets internationally.",
            "1A": "Risks include competition and regulation.",
        }
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([
            {"label": "Wrong item", "snippet": "We sell widgets internationally.",
             "severity": "yellow", "item": "1A"},
        ])
        result = extract_flags_raw("NVDA", sections, _filing_ref(), _CFG, client)
        assert len(result.flags) == 0
        assert result.dropped_count == 1

    def test_invalid_severity_is_dropped(self):
        sections = {"1A": "Some risk text here."}
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([
            {"label": "Bad severity", "snippet": "Some risk text here.", "severity": "orange", "item": "1A"},
        ])
        result = extract_flags_raw("NVDA", sections, _filing_ref(), _CFG, client)
        assert len(result.flags) == 0
        assert result.dropped_count == 1

    def test_malformed_json_response_yields_zero_flags_not_a_crash(self):
        sections = {"1A": "Some risk text."}
        client = MagicMock()
        response = MagicMock()
        block = MagicMock()
        block.type = "text"
        block.text = "I cannot comply with this request."
        response.content = [block]
        client.messages.create.return_value = response
        result = extract_flags_raw("NVDA", sections, _filing_ref(), _CFG, client)
        assert result.flags == []
        assert result.dropped_count == 0  # extraction-quality issue, not a verbatim-drop

    def test_stamps_model_prompt_version_and_extracted_at(self):
        sections = {"1A": "Risk text."}
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([])
        result = extract_flags_raw("NVDA", sections, _filing_ref(), _CFG, client)
        assert result.model == "claude-sonnet-5"
        assert result.prompt_version == "v1"
        assert result.extracted_at
        assert result.ticker == "NVDA"


# ---------------------------------------------------------------------------
# apply_overrides() — deterministic, exact-label-match post-processing
# ---------------------------------------------------------------------------

class TestApplyOverrides:
    def _raw(self, flags=None, dropped=0) -> FlagsResult:
        return FlagsResult(
            ticker="NVDA", model="claude-sonnet-5", prompt_version="v1",
            extracted_at="2026-01-01T00:00:00+00:00", filing=_filing_ref(),
            flags=flags or [], dropped_count=dropped,
        )

    def test_no_override_for_ticker_returns_input_unchanged(self):
        raw = self._raw([Flag(label="X", snippet="s", severity="red", item="1A", verified_verbatim=True)])
        result = apply_overrides(raw, {}, {})
        assert result is raw

    def test_remove_drops_by_exact_label(self):
        raw = self._raw([
            Flag(label="Keep me", snippet="a", severity="red", item="1A", verified_verbatim=True),
            Flag(label="Remove me", snippet="b", severity="red", item="1A", verified_verbatim=True),
        ])
        overrides = {"NVDA": {"remove": ["Remove me"]}}
        result = apply_overrides(raw, overrides, {})
        labels = [f.label for f in result.flags]
        assert labels == ["Keep me"]

    def test_demote_forces_yellow_by_exact_label(self):
        raw = self._raw([Flag(label="Overweighted", snippet="a", severity="red", item="1A", verified_verbatim=True)])
        overrides = {"NVDA": {"demote": ["Overweighted"]}}
        result = apply_overrides(raw, overrides, {})
        assert result.flags[0].severity == "yellow"

    def test_add_filing_sourced_is_verbatim_validated_and_tagged_override(self):
        sections = {"1A": "Customer concentration risk: one customer is 19% of revenue."}
        overrides = {"NVDA": {"add": [
            {"label": "Customer concentration", "severity": "red", "item": "1A",
             "snippet": "Customer concentration risk: one customer is 19% of revenue."},
        ]}}
        result = apply_overrides(self._raw(), overrides, sections)
        assert len(result.flags) == 1
        assert result.flags[0].source == "override"
        assert result.flags[0].verified_verbatim is True

    def test_add_filing_sourced_but_not_verbatim_is_dropped_and_counted(self):
        sections = {"1A": "Some real filing text."}
        overrides = {"NVDA": {"add": [
            {"label": "Fake add", "severity": "red", "item": "1A", "snippet": "text that is not in the filing"},
        ]}}
        result = apply_overrides(self._raw(dropped=0), overrides, sections)
        assert result.flags == []
        assert result.dropped_count == 1

    def test_add_analyst_sourced_skips_verbatim_check_and_is_tagged(self):
        overrides = {"NVDA": {"add": [
            {"label": "Earnings call note", "severity": "yellow", "item": "1A",
             "snippet": "management lowered guidance on the Q3 call", "source": "analyst"},
        ]}}
        result = apply_overrides(self._raw(), overrides, {})  # no filing sections at all
        assert len(result.flags) == 1
        assert result.flags[0].source == "analyst"
        assert result.flags[0].verified_verbatim is True

    def test_remove_demote_add_compose_in_one_call(self):
        raw = self._raw([
            Flag(label="Stay red", snippet="a", severity="red", item="1A", verified_verbatim=True),
            Flag(label="Get demoted", snippet="b", severity="red", item="1A", verified_verbatim=True),
            Flag(label="Get removed", snippet="c", severity="green", item="1", verified_verbatim=True),
        ])
        overrides = {"NVDA": {
            "remove": ["Get removed"],
            "demote": ["Get demoted"],
            "add": [{"label": "New analyst flag", "severity": "red", "item": "1A",
                     "snippet": "anything", "source": "analyst"}],
        }}
        result = apply_overrides(raw, overrides, {})
        by_label = {f.label: f for f in result.flags}
        assert set(by_label) == {"Stay red", "Get demoted", "New analyst flag"}
        assert by_label["Stay red"].severity == "red"
        assert by_label["Get demoted"].severity == "yellow"
        assert by_label["New analyst flag"].source == "analyst"

    def test_does_not_mutate_input_flag_objects(self):
        """Cached Flag instances must never be mutated in place — apply_overrides
        is called fresh on every request against what may be a shared cached object."""
        original = Flag(label="Demote target", snippet="a", severity="red", item="1A", verified_verbatim=True)
        raw = self._raw([original])
        apply_overrides(raw, {"NVDA": {"demote": ["Demote target"]}}, {})
        assert original.severity == "red"  # unchanged


# ---------------------------------------------------------------------------
# get_flags() — cache-or-extract, then overrides fresh every call
# ---------------------------------------------------------------------------

class TestGetFlagsCaching:
    def _client_returning(self, payload):
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response(payload)
        return client

    def test_second_call_is_a_cache_hit_model_not_called_again(self, tmp_path):
        sections = {"1A": "Risk text that stays constant."}
        client = self._client_returning([])
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path)
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path)
        assert client.messages.create.call_count == 1

    def test_refresh_true_forces_a_new_model_call(self, tmp_path):
        sections = {"1A": "Risk text that stays constant."}
        client = self._client_returning([])
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path)
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path, force_refresh=True)
        assert client.messages.create.call_count == 2

    def test_refresh_true_re_stamps_extracted_at(self, tmp_path):
        sections = {"1A": "Risk text that stays constant."}
        client = self._client_returning([])
        r1 = get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path)
        r2 = get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path, force_refresh=True)
        assert r1.extracted_at != r2.extracted_at

    def test_different_accession_is_a_separate_cache_entry(self, tmp_path):
        sections = {"1A": "Risk text."}
        client = self._client_returning([])
        get_flags("NVDA", sections, _filing_ref("acc-1"), _CFG, client, tmp_path)
        get_flags("NVDA", sections, _filing_ref("acc-2"), _CFG, client, tmp_path)
        assert client.messages.create.call_count == 2

    def test_overrides_apply_on_a_cache_hit_without_a_model_call(self, tmp_path):
        """Editing config.yaml's overrides must take effect immediately on the
        NEXT request without needing ?refresh=true or a re-extraction."""
        sections = {"1A": "Our competitor XYZ Corp filed a lawsuit against us in March 2024."}
        client = self._client_returning([
            {"label": "Patent lawsuit", "snippet": "Our competitor XYZ Corp filed a lawsuit against us in March 2024.",
             "severity": "red", "item": "1A"},
        ])
        cfg_no_override = _CFG
        cfg_with_override = {"flags": {**_CFG["flags"], "overrides": {"NVDA": {"demote": ["Patent lawsuit"]}}}}

        r1 = get_flags("NVDA", sections, _filing_ref(), cfg_no_override, client, tmp_path)
        assert r1.flags[0].severity == "red"

        r2 = get_flags("NVDA", sections, _filing_ref(), cfg_with_override, client, tmp_path)
        assert r2.flags[0].severity == "yellow"
        assert client.messages.create.call_count == 1  # still just the one extraction


# ---------------------------------------------------------------------------
# is_cached() — the free existence check the spend gate uses
# ---------------------------------------------------------------------------

class TestIsCached:
    def test_false_before_any_extraction(self, tmp_path):
        assert is_cached(tmp_path, "acc-1", "v1", "claude-sonnet-5") is False

    def test_true_after_get_flags_extracts_and_caches(self, tmp_path):
        sections = {"1A": "Risk text."}
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([])
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path)
        assert is_cached(tmp_path, _filing_ref().accession, "v1", "claude-sonnet-5") is True

    def test_does_not_call_the_model(self, tmp_path):
        assert is_cached(tmp_path, "acc-1", "v1", "claude-sonnet-5") is False  # no client needed at all


# ---------------------------------------------------------------------------
# get_flags() call_info — reports what actually happened, for usage logging
# ---------------------------------------------------------------------------

class TestGetFlagsCallInfo:
    def test_live_extraction_reports_cache_status_live(self, tmp_path):
        sections = {"1A": "Risk text."}
        client = MagicMock()
        response = _mock_model_response([])
        response.usage = MagicMock(input_tokens=500, output_tokens=42)
        client.messages.create.return_value = response
        call_info = {}
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path, call_info=call_info)
        assert call_info == {"cache_status": "live", "input_tokens": 500, "output_tokens": 42}

    def test_cache_hit_reports_cache_status_from_cache_with_no_tokens(self, tmp_path):
        sections = {"1A": "Risk text."}
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([])
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path)  # populate cache
        call_info = {}
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path, call_info=call_info)
        assert call_info == {"cache_status": "from_cache", "input_tokens": None, "output_tokens": None}

    def test_force_refresh_reports_cache_status_refresh(self, tmp_path):
        sections = {"1A": "Risk text."}
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([])
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path)
        call_info = {}
        get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path, force_refresh=True, call_info=call_info)
        assert call_info["cache_status"] == "refresh"

    def test_call_info_none_is_a_no_op_backward_compatible_default(self, tmp_path):
        """Every pre-existing caller passes no call_info at all — must not
        raise or change behavior."""
        sections = {"1A": "Risk text."}
        client = MagicMock()
        client.messages.create.return_value = _mock_model_response([])
        result = get_flags("NVDA", sections, _filing_ref(), _CFG, client, tmp_path)
        assert isinstance(result, FlagsResult)


# ---------------------------------------------------------------------------
# Cost estimation / stamping (config.yaml flags.pricing)
# ---------------------------------------------------------------------------

class TestCostHelpers:
    _PRICED_CFG = {
        "flags": {
            "model": "claude-sonnet-4-6",
            "pricing": {
                "claude-sonnet-4-6": {"input_per_million": 3.00, "output_per_million": 15.00},
            },
        },
    }

    def test_estimate_uses_pinned_model_pricing(self):
        est = estimate_extraction_cost_usd(self._PRICED_CFG)
        assert est is not None
        assert est > 0

    def test_estimate_is_none_when_pinned_model_has_no_pricing_entry(self):
        cfg = {"flags": {"model": "claude-sonnet-5", "pricing": {}}}
        assert estimate_extraction_cost_usd(cfg) is None

    def test_estimate_is_none_when_no_pricing_table_at_all(self):
        assert estimate_extraction_cost_usd({"flags": {"model": "claude-sonnet-5"}}) is None

    def test_compute_cost_matches_hand_calculated_value(self):
        # 15,000 input tokens @ $3/M + 600 output tokens @ $15/M, scaled here
        # to round numbers: 1,000,000 input @ $3/M + 1,000,000 output @ $15/M = $18.
        cost = compute_cost_usd(self._PRICED_CFG, "claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert cost == pytest.approx(18.0)

    def test_compute_cost_is_zero_for_unpriced_model(self):
        cost = compute_cost_usd(self._PRICED_CFG, "some-other-model", 1_000_000, 1_000_000)
        assert cost == 0.0

    def test_compute_cost_treats_missing_token_counts_as_zero(self):
        cost = compute_cost_usd(self._PRICED_CFG, "claude-sonnet-4-6", None, None)
        assert cost == 0.0


# ---------------------------------------------------------------------------
# validate_flags_config() — fail fast at startup
# ---------------------------------------------------------------------------

class TestValidateFlagsConfig:
    def test_no_flags_section_is_fine(self):
        validate_flags_config({})

    def test_valid_minimal_config_passes(self):
        validate_flags_config({"flags": {"model": "claude-sonnet-5"}})

    def test_missing_model_raises(self):
        with pytest.raises(ValueError, match="model"):
            validate_flags_config({"flags": {"temperature": 0}})

    def test_non_numeric_temperature_raises(self):
        with pytest.raises(ValueError, match="temperature"):
            validate_flags_config({"flags": {"model": "m", "temperature": "cold"}})

    def test_overrides_must_be_a_mapping(self):
        with pytest.raises(ValueError, match="overrides"):
            validate_flags_config({"flags": {"model": "m", "overrides": ["not", "a", "dict"]}})

    def test_add_entry_missing_required_key_raises(self):
        cfg = {"flags": {"model": "m", "overrides": {"NVDA": {"add": [
            {"label": "X", "severity": "red", "item": "1A"},  # missing snippet
        ]}}}}
        with pytest.raises(ValueError, match="snippet"):
            validate_flags_config(cfg)

    def test_add_entry_bad_severity_raises(self):
        cfg = {"flags": {"model": "m", "overrides": {"NVDA": {"add": [
            {"label": "X", "snippet": "s", "severity": "purple", "item": "1A"},
        ]}}}}
        with pytest.raises(ValueError, match="severity"):
            validate_flags_config(cfg)

    def test_add_entry_bad_source_raises(self):
        cfg = {"flags": {"model": "m", "overrides": {"NVDA": {"add": [
            {"label": "X", "snippet": "s", "severity": "red", "item": "1A", "source": "ceo"},
        ]}}}}
        with pytest.raises(ValueError, match="source"):
            validate_flags_config(cfg)

    def test_demote_must_be_a_list_of_strings(self):
        cfg = {"flags": {"model": "m", "overrides": {"NVDA": {"demote": "not a list"}}}}
        with pytest.raises(ValueError, match="demote"):
            validate_flags_config(cfg)

    def test_real_config_yaml_validates(self):
        """The actual shipped config.yaml must always pass this."""
        import yaml
        from pathlib import Path as P
        repo_root = P(__file__).resolve().parent.parent
        with open(repo_root / "config.yaml") as f:
            cfg = yaml.safe_load(f)
        validate_flags_config(cfg)
