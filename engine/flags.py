"""
flags.py — Tier 2 qualitative red/green flag extraction from 10-K text.

This is the FIRST place an LLM enters the Investment Engine pipeline.
CLAUDE.md's "LLMs never touch arithmetic" invariant is unchanged and
absolute; this module adds one narrow, audited exception:

    LLMs may SELECT verbatim text spans from filings — never generate,
    paraphrase, or compute.

`verify_verbatim()` is the enforcement mechanism: every snippet a model
returns is checked as an EXACT substring of the filing section text it was
given. Anything that fails is dropped and logged (never surfaced), counted
in `dropped_count`. `verified_verbatim` on a Flag is set ONLY by this
validator — it is never trusted from the model's own claim.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_VALID_SEVERITIES = {"red", "yellow", "green"}
_TARGET_ITEMS = ("1", "1A", "7")

_SYSTEM_PROMPT = """You are assisting a deterministic financial-analysis pipeline that extracts qualitative red/green flags from SEC 10-K filings.

STRICT RULE: You may ONLY select verbatim spans of text that already exist, character-for-character, in the filing excerpts provided below. You must NEVER paraphrase, summarize, invent, compute, or lightly edit a quote. Every "snippet" you return will be checked against the source text with an exact substring match; anything that is not an exact match is discarded and logged as a hallucination.

For each qualitative red flag (elevated risk), yellow flag (worth watching), or green flag (notable strength/mitigant) you find, return an object with exactly these keys:
- label: a short 2-5 word name for the flag
- snippet: the EXACT verbatim text span from the filing that supports it (copy it exactly — do not alter capitalization, punctuation, or wording)
- severity: "red", "yellow", or "green"
- item: which section it came from ("1", "1A", or "7")

Return 3 to 8 flags total. Favor precision over recall: skip boilerplate risk factors that are generic to nearly every issuer (e.g. generic "our stock price may be volatile" or "we face competition" language) and only surface flags specific and material to THIS company's actual disclosed facts.

Respond with ONLY a JSON array of these objects. No other text, no markdown code fences, no rationale field, no keys beyond the four listed above."""


@dataclass
class Flag:
    label: str
    snippet: str
    severity: str            # "red" | "yellow" | "green"
    item: str                # "1" | "1A" | "7"
    verified_verbatim: bool
    # "model" for everything in this commit — the "override"/"analyst"
    # provenance values arrive with config.yaml overrides (a later commit).
    source: str = "model"


@dataclass
class FilingRef:
    form: str
    accession: str
    period_ending: Optional[str]
    filed: Optional[str]
    url: str


@dataclass
class FlagsResult:
    ticker: str
    model: str
    prompt_version: str
    extracted_at: str
    filing: FilingRef
    flags: list = field(default_factory=list)   # list[Flag]
    dropped_count: int = 0


# ---------------------------------------------------------------------------
# The verbatim validator — THE key safety mechanism in this module.
# ---------------------------------------------------------------------------

def verify_verbatim(snippet: str, source_text: str) -> bool:
    """
    Exact-substring check — the only thing standing between a model's
    output and a fabricated quote reaching a user. Deliberately dumb: no
    whitespace normalization, no fuzzy matching, no case-folding. "Exact
    substring" means exactly that.
    """
    if not snippet or not source_text:
        return False
    return snippet in source_text


# ---------------------------------------------------------------------------
# Model call + parsing
# ---------------------------------------------------------------------------

def _build_user_prompt(sections: dict) -> str:
    parts = [f"=== ITEM {item} ===\n{text}" for item, text in sections.items() if item in _TARGET_ITEMS]
    return "\n\n".join(parts)


def _call_model(client, model: str, temperature: float, prompt: str) -> str:
    """Isolated so tests can patch exactly this function (mock the API,
    assert call count) without needing a real anthropic.Anthropic client."""
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        temperature=temperature,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


def _parse_model_flags(raw_text: str) -> list:
    """Best-effort JSON extraction — models sometimes wrap JSON in prose
    despite instructions. A total parse failure returns [] rather than
    raising: that's an extraction-quality problem, not a verbatim-
    validation problem, so it does not touch dropped_count."""
    match = re.search(r"\[.*\]", raw_text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def extract_flags_raw(ticker: str, sections: dict, filing: FilingRef, cfg: dict, client) -> FlagsResult:
    """
    Calls the model, validates every candidate snippet as an exact
    substring of ITS OWN item's section text, and drops (counts, logs) any
    that fail.
    """
    flags_cfg = cfg.get("flags", {})
    model = flags_cfg.get("model", "claude-sonnet-5")
    temperature = flags_cfg.get("temperature", 0)
    prompt_version = flags_cfg.get("prompt_version", "v1")

    prompt = _build_user_prompt(sections)
    raw_text = _call_model(client, model, temperature, prompt)
    candidates = _parse_model_flags(raw_text)

    verified: list = []
    dropped = 0
    for c in candidates:
        if not isinstance(c, dict):
            dropped += 1
            continue
        label = str(c.get("label", "")).strip()
        snippet = c.get("snippet", "")
        severity = str(c.get("severity", "")).strip().lower()
        item = str(c.get("item", "")).strip().upper()
        section_text = sections.get(item, "")
        ok = bool(label) and severity in _VALID_SEVERITIES and isinstance(snippet, str) and verify_verbatim(snippet, section_text)
        if not ok:
            dropped += 1
            print(f"! {ticker}: dropped non-verbatim snippet", file=sys.stderr)
            continue
        verified.append(Flag(label=label, snippet=snippet, severity=severity, item=item,
                              verified_verbatim=True, source="model"))

    return FlagsResult(
        ticker=ticker.upper(),
        model=model,
        prompt_version=prompt_version,
        extracted_at=datetime.now(timezone.utc).isoformat(),
        filing=filing,
        flags=verified,
        dropped_count=dropped,
    )


# ---------------------------------------------------------------------------
# Config validation — fail fast at app startup, not at first request.
# ---------------------------------------------------------------------------

def validate_flags_config(cfg: dict) -> None:
    flags_cfg = cfg.get("flags")
    if not flags_cfg:
        return  # feature not configured yet — fine, endpoint reports "not configured"

    if not flags_cfg.get("model"):
        raise ValueError("config.yaml flags.model is required.")

    temperature = flags_cfg.get("temperature", 0)
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
        raise ValueError("config.yaml flags.temperature must be a number.")
