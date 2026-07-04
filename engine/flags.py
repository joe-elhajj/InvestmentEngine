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

This endpoint is non-deterministic (same filing, same prompt, same model
can still return different flags between calls) — get_flags() caches the
raw result on disk keyed by (accession, prompt_version, model) so a
filing is extracted once, not on every request. Analyst overrides
(config.yaml `flags.overrides`) are layered on top of that cached raw
result fresh on every call — see get_flags() / apply_overrides().
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
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
    # "model" (LLM-selected, always verbatim-validated), "override" (analyst
    # config.yaml `add` entry whose snippet IS filing text, still verbatim-
    # validated), or "analyst" (config.yaml `add` entry explicitly marked
    # source: analyst — e.g. from an earnings call, not the filing itself,
    # so it is NOT verbatim-checked against the filing and must be visually
    # distinguishable in the dashboard).
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
# Analyst overrides (config.yaml `flags.overrides`) — applied fresh on
# every call, never baked into the cache (see get_flags()): editing
# config.yaml takes effect immediately, without needing ?refresh=true or
# a re-extraction, exactly like classification.overrides is applied fresh
# on every read rather than cached alongside EDGAR data.
# ---------------------------------------------------------------------------

def apply_overrides(raw: FlagsResult, overrides_cfg: dict, sections: dict) -> FlagsResult:
    """Deterministic post-processing by exact label match — same pattern as
    classification.overrides. Never mutates `raw` or its Flag objects in
    place (both may be shared cache instances); always returns a copy."""
    override = overrides_cfg.get(raw.ticker.upper())
    if not override:
        return raw

    flags = list(raw.flags)
    dropped = raw.dropped_count

    remove_labels = set(override.get("remove", []))
    if remove_labels:
        flags = [f for f in flags if f.label not in remove_labels]

    demote_labels = set(override.get("demote", []))
    if demote_labels:
        flags = [replace(f, severity="yellow") if f.label in demote_labels else f for f in flags]

    for add_spec in override.get("add", []):
        label = add_spec["label"]
        snippet = add_spec["snippet"]
        severity = add_spec["severity"]
        item = str(add_spec.get("item", "")).strip().upper()
        is_analyst_sourced = add_spec.get("source") == "analyst"

        if is_analyst_sourced:
            # Not filing text (e.g. an earnings call) — trusted as given,
            # tagged distinctly so the dashboard never presents it as a
            # model-selected filing quote.
            flags.append(Flag(label=label, snippet=snippet, severity=severity, item=item,
                               verified_verbatim=True, source="analyst"))
            continue

        # Claimed to be filing text — held to exactly the same verbatim
        # bar as a model-selected span, not a lesser one just because an
        # analyst typed it into config.yaml.
        section_text = sections.get(item, "")
        if verify_verbatim(snippet, section_text):
            flags.append(Flag(label=label, snippet=snippet, severity=severity, item=item,
                               verified_verbatim=True, source="override"))
        else:
            dropped += 1
            print(f"! {raw.ticker}: dropped non-verbatim snippet", file=sys.stderr)

    return replace(raw, flags=flags, dropped_count=dropped)


# ---------------------------------------------------------------------------
# On-disk cache — keyed by (accession, prompt_version, model). A filing +
# prompt + model triple is treated as one immutable extraction; the cache
# entry IS the pinned snapshot Tier 3 will consume.
# ---------------------------------------------------------------------------

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.\-]")


def _cache_key(accession: str, prompt_version: str, model: str) -> str:
    raw_key = f"{accession}_{prompt_version}_{model}"
    return _UNSAFE_CHARS_RE.sub("_", raw_key)


def _cache_path(cache_dir: Path, accession: str, prompt_version: str, model: str) -> Path:
    return cache_dir / f"{_cache_key(accession, prompt_version, model)}.json"


def _flag_from_dict(d: dict) -> Flag:
    return Flag(
        label=d["label"], snippet=d["snippet"], severity=d["severity"], item=d["item"],
        verified_verbatim=d["verified_verbatim"], source=d.get("source", "model"),
    )


def _load_cached_raw(cache_dir: Path, accession: str, prompt_version: str, model: str) -> Optional[FlagsResult]:
    p = _cache_path(cache_dir, accession, prompt_version, model)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return FlagsResult(
        ticker=data["ticker"], model=data["model"], prompt_version=data["prompt_version"],
        extracted_at=data["extracted_at"], filing=FilingRef(**data["filing"]),
        flags=[_flag_from_dict(f) for f in data["flags"]], dropped_count=data["dropped_count"],
    )


def _save_cached_raw(cache_dir: Path, accession: str, prompt_version: str, model: str, result: FlagsResult) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache_path(cache_dir, accession, prompt_version, model).write_text(json.dumps(asdict(result)))


def get_flags(
    ticker: str,
    sections: dict,
    filing: FilingRef,
    cfg: dict,
    client,
    cache_dir: Path,
    force_refresh: bool = False,
) -> FlagsResult:
    """
    Public entry point: cache-or-extract the raw model result, then apply
    config.yaml's flags.overrides fresh on every call. force_refresh=True
    (the endpoint's ?refresh=true) skips the cache READ (always re-calls
    the model) but still WRITES the new result, overwriting the old cache
    entry and re-stamping extracted_at.
    """
    flags_cfg = cfg.get("flags", {})
    model = flags_cfg.get("model", "claude-sonnet-5")
    prompt_version = flags_cfg.get("prompt_version", "v1")

    raw = None if force_refresh else _load_cached_raw(cache_dir, filing.accession, prompt_version, model)
    if raw is None:
        raw = extract_flags_raw(ticker, sections, filing, cfg, client)
        _save_cached_raw(cache_dir, filing.accession, prompt_version, model, raw)

    return apply_overrides(raw, flags_cfg.get("overrides", {}), sections)


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

    overrides = flags_cfg.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("config.yaml flags.overrides must be a mapping of ticker -> override.")

    for ticker, override in overrides.items():
        if not isinstance(override, dict):
            raise ValueError(f"config.yaml flags.overrides.{ticker} must be a mapping.")

        for add_spec in override.get("add", []):
            for key in ("label", "snippet", "severity", "item"):
                if key not in add_spec:
                    raise ValueError(f"config.yaml flags.overrides.{ticker}.add entry missing {key!r}.")
            if add_spec["severity"] not in _VALID_SEVERITIES:
                raise ValueError(
                    f"config.yaml flags.overrides.{ticker}.add severity must be one of {sorted(_VALID_SEVERITIES)}."
                )
            source = add_spec.get("source")
            if source is not None and source != "analyst":
                raise ValueError(
                    f"config.yaml flags.overrides.{ticker}.add source, if set, must be 'analyst'."
                )

        for key in ("demote", "remove"):
            val = override.get(key, [])
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise ValueError(f"config.yaml flags.overrides.{ticker}.{key} must be a list of strings.")
