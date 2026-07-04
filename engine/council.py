"""
council.py — Tier 3 adversarial council synthesis (engine/council.py).

The SECOND sanctioned use of a model in this codebase, distinct from Tier
2's verbatim-selection boundary (engine/flags.py). Here the model is
sanctioned to SYNTHESIZE and ARGUE over evidence already produced by Tier
1 (deterministic) and Tier 2 (verbatim-validated) — it never derives,
adjusts, or recomputes a number. Every prompt below instructs the model to
cite, not calculate; "not in evidence" is always a valid answer.

Hybrid 7-call structure (decided by the single-call vs multi-call
experiment on experiment/council-multicall — see
scripts/council_output_CAT_multicall.md for the concrete evidence this
design responds to):

  Round 1 (1 call)  — all five advisor opinions generated in a single
                       generation. Single-call format discipline was far
                       better than five separate advisor calls in the
                       experiment; opinions were substantively equivalent
                       either way.
  Round 2 (5 calls) — isolated blind peer review, one call per advisor
                       "seat," each seeing the other four opinions
                       anonymized. This is where the multi-call
                       experiment's context isolation demonstrably earned
                       its cost (e.g. the ROE-as-leverage-artifact
                       critique the single-call run never produced), so
                       it is NOT collapsed into Round 1's single call.
  Round 3 (1 call)  — Chairman synthesis over the bundle + all opinions +
                       all reviews.

Every call disables extended thinking (`thinking={"type": "disabled"}`) —
without it, claude-sonnet-5's adaptive thinking can consume an entire
max_tokens budget before emitting any output text, regardless of how high
the ceiling is set (verified the hard way on experiment/council-multicall,
~$3 of live debugging). `extract_text()` below is copied from that
experiment for the same reason: content blocks can be ThinkingBlock,
RedactedThinkingBlock, or TextBlock, so scanning by `type` (never
indexing `content[0]`) is required once thinking is anywhere in the mix.
"""

from __future__ import annotations

import hashlib
import json
import re
import textwrap
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine import flags as FLAGS

_ADVISOR_NAMES = (
    "BEAR_ADVOCATE",
    "BULL_STEELMAN",
    "ASSUMPTION_AUDITOR",
    "BASE_RATE_OUTSIDER",
    "EXECUTION_REALIST",
)

_POSITION_TAXONOMY = ("ACCUMULATE", "HOLD", "TRIM", "AVOID", "INSUFFICIENT EVIDENCE")


# ---------------------------------------------------------------------------
# Result shapes — structured, not one text blob, so a future UI can render
# advisor/review/chairman panes without re-parsing raw text. `parsed` is
# False (never an exception) whenever tolerant regex parsing can't find a
# clean POSITION/CONFIDENCE/VERDICT/section-header shape — `text` always
# carries the full raw response regardless.
# ---------------------------------------------------------------------------

@dataclass
class AdvisorOpinion:
    name: str
    position: Optional[str]      # one of _POSITION_TAXONOMY, or None if unparsed/off-taxonomy
    confidence: Optional[int]    # 1-5, or None
    text: str
    parsed: bool


@dataclass
class ReviewNote:
    reviewer: str                # which advisor "seat" wrote this review
    text: str


@dataclass
class ChairmanOutput:
    verdict: Optional[str]
    confidence: Optional[int]
    text: str
    sections: dict = field(default_factory=dict)  # e.g. {"contradiction_ledger": "...", ...}
    parsed: bool = False


@dataclass
class CouncilMeta:
    ticker: str
    model: str
    prompt_version: str
    config_hash: str
    convened_at: str
    accession: str
    thesis_status: str            # "pre_thesis" | "thesis_present"
    thesis_hash: Optional[str]
    evidence_integrity_note: str
    total_cost_usd: Optional[float]
    total_input_tokens: int
    total_output_tokens: int
    status_flags: list = field(default_factory=list)   # subset of {"truncated", "format_degraded"}
    calls: list = field(default_factory=list)          # per-call {call_type, input_tokens, output_tokens, stop_reason}


@dataclass
class CouncilResult:
    ticker: str
    advisors: list        # list[AdvisorOpinion]
    reviews: list          # list[ReviewNote]
    chairman: ChairmanOutput
    meta: CouncilMeta


# ---------------------------------------------------------------------------
# Evidence bundle — assembled BEFORE any advisor speaks. Quant is Tier 1's
# canonical JSON, flags is a CACHED Tier 2 extraction (never triggered from
# here), thesis is a thesis-journal entry if a thesis store exists — none
# does yet in this codebase, so every bundle today is pre-thesis.
# ---------------------------------------------------------------------------

@dataclass
class EvidenceBundle:
    ticker: str
    quant: dict
    flags: "FLAGS.FlagsResult"
    thesis: Optional[dict]
    pre_thesis: bool
    accession: str
    config_hash: str
    thesis_hash: Optional[str] = None


def assemble_bundle(
    ticker: str,
    quant: dict,
    flags_result: "FLAGS.FlagsResult",
    thesis: Optional[dict],
    accession: str,
    config_hash: str,
) -> EvidenceBundle:
    pre_thesis = thesis is None
    thesis_hash = None
    if not pre_thesis:
        canonical = json.dumps(thesis, sort_keys=True, separators=(",", ":"), default=str)
        thesis_hash = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return EvidenceBundle(
        ticker=ticker, quant=quant, flags=flags_result, thesis=thesis,
        pre_thesis=pre_thesis, accession=accession, config_hash=config_hash,
        thesis_hash=thesis_hash,
    )


def render_bundle_text(bundle: EvidenceBundle) -> str:
    """Serialize the bundle as the user message every Round 1/2/3 call is
    built on top of. Mirrors experiment/council-multicall's render_bundle()."""
    thesis_note = (
        "PRE-THESIS MODE: no thesis journal entry exists for this ticker. "
        "Round 3 will draft falsification criteria rather than audit them."
        if bundle.pre_thesis
        else json.dumps(bundle.thesis, indent=2)
    )
    return textwrap.dedent(f"""
        Evidence bundle for {bundle.ticker}:

        === QUANT (Tier 1, canonical) ===
        {json.dumps(bundle.quant, indent=2, default=str)}

        === FLAGS (Tier 2, cached extraction) ===
        {json.dumps(asdict(bundle.flags), indent=2, default=str)}

        === THESIS JOURNAL ===
        {thesis_note}

        Follow your role's instructions. Absence-is-not-zero: if a number
        you want is absent, say "not in evidence." Never derive.
    """).strip()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_ROUND1_SYSTEM = """You are running Round 1 of an adversarial investment council: five
independent advisors, each writing their own opinion on the evidence bundle
below. Produce all five opinions in this single response, each starting
with its own exact header line on its own line, and nothing else on that
line:

### EVIDENCE_INTEGRITY_NOTE
### BEAR_ADVOCATE
### BULL_STEELMAN
### ASSUMPTION_AUDITOR
### BASE_RATE_OUTSIDER
### EXECUTION_REALIST

Under EVIDENCE_INTEGRITY_NOTE, write a short note on any known false gaps
or data anomalies in the evidence bundle (e.g. a metric that reads as
missing in the quant JSON but is actually computable via a documented
fallback the engine didn't wire up). You write this once, here — every
advisor and the Chairman read it, no advisor repeats it. If you see no
such anomaly, say so in one sentence rather than omitting the section.

Each of the five advisors is a SEPARATE, ADVERSARIAL voice with a
different mandate — write as if five different analysts had never seen
each other's work; do not let one advisor's reasoning bleed into another's.
Each advisor:
- Writes at most 250 words.
- Cites at least 3 evidence items verbatim from the bundle, with their
  source (engine JSON path, flag citation, or "not in evidence" if
  absent). NEVER derives, adjusts, or recomputes a number.
- Ends with exactly these three lines, in this order:
  POSITION: <value>
  AGAINST: <the single strongest point against their own position>
  CONFIDENCE: <1-5>
- POSITION must be EXACTLY one of: ACCUMULATE / HOLD / TRIM / AVOID /
  INSUFFICIENT EVIDENCE. Any other position string is a format violation.

Advisor mandates:

BEAR_ADVOCATE — build the strongest sell/avoid case. Lean on high-salience
flags, the expectations gap if rich, durability weaknesses.

BULL_STEELMAN — build the strongest accumulate case. May NOT ignore red
flags — must contextualize each one it dismisses, by name.

ASSUMPTION_AUDITOR — attack the analyst-owned assumptions as applied to
THIS name: WACC, terminal growth, normalized-FCF window, universe-relative
percentiles. Ask which single assumption, if wrong, flips the verdict. May
reason about sensitivity direction but NEVER compute new values — cite the
engine's own scenario/sensitivity outputs.

BASE_RATE_OUTSIDER — reference-class forecasting. What happens to the
median company priced at this implied growth / this durability decile?
Ignore the story entirely; argue only from base rates and the quant
profile. Flag narrative-driven reasoning; label uncited priors as
"general priors, not in evidence."

EXECUTION_REALIST — assume the thesis is right; attack the
IMPLEMENTATION. Position sizing vs. sleeve rules (compounder vs.
satellite, no sector-ETF stacking), falsification criteria quality
(observable? dated? would you actually act?), what the journal entry must
say BEFORE capital moves."""

_REVIEWER_PROMPT = """You are one of five advisors on an adversarial investment
council, reviewing four anonymized peer opinions on the same evidence
bundle. Reviews may NOT reference advisor roles or names — argue the
content, not the mandate. Write at most 120 words: (1) strongest opinion
and why, (2) weakest reasoning and why, (3) one contradiction between any
two opinions, or between an opinion and the evidence. Refer to opinions by
their letter (A-D) only."""

_CHAIRMAN_PROMPT = """You are the CHAIRMAN of an adversarial investment council. You've
received five independent advisor opinions and their peer reviews.
Synthesize. Produce, in order, each under its own header line:

### VERDICT
One of ACCUMULATE / HOLD / TRIM / AVOID / INSUFFICIENT EVIDENCE — exactly
that string, on a line starting "VERDICT: ". Any other position string is
a format violation. Then a line "CONFIDENCE: <1-5>". Then one sentence on
what would move confidence UP a notch, and a separate sentence on what
would move confidence DOWN a notch — never only one direction.

### CONTRADICTION_LEDGER
Every evidence-stream contradiction surfaced, each marked RESOLVED (how)
or OPEN (what observable fact settles it).

### THESIS_JOURNAL_DELTA
Concrete edits — new/changed falsification criteria with observable
triggers and dates.

### ACTION_ITEMS
At most 5, each with an owner (user or engine backlog) and a trigger.

### RISK_REGISTER
Top 3 risks, each tagged with which advisor surfaced it.

### DISSENT
If any advisor's final position differs from the verdict, quote their
strongest surviving argument. Never present false unanimity — if the
council genuinely splits, say so.

NEVER compute a number; only cite what advisors and evidence already
contain."""


# ---------------------------------------------------------------------------
# SDK call + text extraction — copied from experiment/council-multicall's
# scripts/council_experiment.py, the offline-verified reference for this
# model/SDK version (anthropic==0.116.0).
# ---------------------------------------------------------------------------

_THINKING_TYPES = {"thinking", "redacted_thinking"}


def extract_text(content: list) -> str:
    """Find every TextBlock in a response's content list regardless of
    position or what else is present — extended thinking can put a
    ThinkingBlock anywhere, so `content[0].text` is fragile. Raises a
    diagnostic RuntimeError (thinking-only truncation vs. genuinely empty
    response) rather than an AttributeError, if no text block is found."""
    text_blocks = [block.text for block in content if getattr(block, "type", None) == "text"]
    if not text_blocks:
        seen_types = [getattr(block, "type", type(block).__name__) for block in content]
        if seen_types and all(t in _THINKING_TYPES for t in seen_types):
            raise RuntimeError(
                f"Response was thinking-only (block types: {seen_types}) — the model likely "
                "spent the whole max_tokens budget on extended thinking and hit the limit "
                "before emitting any text. Raise max_tokens and retry."
            )
        raise RuntimeError(
            f"No text block found in response content — got block types: {seen_types}"
        )
    return "".join(text_blocks)


def _call(
    client,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    call_type: str,
    call_info: Optional[list] = None,
) -> str:
    """One SDK call. Appends a per-call usage record to `call_info` (if
    given) BEFORE returning — so a caller iterating `call_info` after a
    later call raises still sees every call that actually completed and
    was billed, exception or not."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "disabled"},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = extract_text(response.content)
    usage_obj = getattr(response, "usage", None)
    record = {
        "call_type": call_type,
        "input_tokens": getattr(usage_obj, "input_tokens", None),
        "output_tokens": getattr(usage_obj, "output_tokens", None),
        "stop_reason": getattr(response, "stop_reason", None),
    }
    if call_info is not None:
        call_info.append(record)
    return text


# ---------------------------------------------------------------------------
# Parsing — tolerant regex only; a parse failure sets parsed=False and
# keeps the raw text, it never raises.
# ---------------------------------------------------------------------------

_SECTION_HEADER_RE = re.compile(r"(?m)^###\s*([A-Z_]+)\s*$")


def _split_sections(text: str) -> dict:
    matches = list(_SECTION_HEADER_RE.finditer(text))
    sections = {}
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[start:end].strip()
    return sections


def _extract_field(text: str, field_name: str) -> Optional[str]:
    m = re.search(rf"(?im)^\s*{field_name}\s*:\s*(.+)$", text)
    return m.group(1).strip() if m else None


def _normalize_position(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    cleaned = raw.strip().strip("*").strip()
    upper = cleaned.upper()
    for taxon in _POSITION_TAXONOMY:
        if upper == taxon or upper.startswith(taxon):
            return taxon
    return None


def _extract_confidence(text: str) -> Optional[int]:
    m = re.search(r"(?im)^\s*CONFIDENCE\s*:\s*(\d)", text)
    if not m:
        return None
    val = int(m.group(1))
    return val if 1 <= val <= 5 else None


def _parse_advisor_block(name: str, text: str) -> AdvisorOpinion:
    position = _normalize_position(_extract_field(text, "POSITION"))
    confidence = _extract_confidence(text)
    return AdvisorOpinion(
        name=name, position=position, confidence=confidence,
        text=text.strip(), parsed=position is not None,
    )


_CHAIRMAN_SECTION_MAP = {
    "CONTRADICTION_LEDGER": "contradiction_ledger",
    "THESIS_JOURNAL_DELTA": "thesis_journal_delta",
    "ACTION_ITEMS": "action_items",
    "RISK_REGISTER": "risk_register",
    "DISSENT": "dissent",
}


def _parse_chairman(text: str) -> ChairmanOutput:
    sections = _split_sections(text)
    verdict_block = sections.get("VERDICT", "")
    verdict = _normalize_position(_extract_field(verdict_block, "VERDICT"))
    confidence = _extract_confidence(verdict_block)
    parsed_sections = {
        out_key: sections[in_key] for in_key, out_key in _CHAIRMAN_SECTION_MAP.items() if in_key in sections
    }
    parsed = verdict is not None and len(parsed_sections) == len(_CHAIRMAN_SECTION_MAP)
    return ChairmanOutput(
        verdict=verdict, confidence=confidence, text=text.strip(),
        sections=parsed_sections, parsed=parsed,
    )


def _anonymize_others(advisors: list, exclude_name: str, rng=None) -> str:
    """The Round 2 anonymization: every OTHER advisor's opinion, relabeled
    Opinion A-D and order-shuffled, with no advisor name anywhere."""
    import random as _random
    rng = rng or _random
    others = [a for a in advisors if a.name != exclude_name]
    shuffled = list(others)
    rng.shuffle(shuffled)
    return "\n\n".join(f"=== Opinion {chr(65 + i)} ===\n{a.text}" for i, a in enumerate(shuffled))


# ---------------------------------------------------------------------------
# Round orchestration
# ---------------------------------------------------------------------------

def _run_round1_once(client, model: str, user_msg: str, call_info: Optional[list]) -> tuple:
    text = _call(client, model, _ROUND1_SYSTEM, user_msg, max_tokens=6000,
                 call_type="council_opinions", call_info=call_info)
    sections = _split_sections(text)
    integrity_note = sections.get("EVIDENCE_INTEGRITY_NOTE", "")
    advisors = [_parse_advisor_block(name, sections.get(name, "")) for name in _ADVISOR_NAMES]
    return advisors, integrity_note


def convene(
    ticker: str,
    bundle: EvidenceBundle,
    cfg: dict,
    client,
    call_info: Optional[list] = None,
) -> CouncilResult:
    """Runs the full 7-call council: Round 1 (1 call, retried once on a
    position-taxonomy violation), Round 2 (5 isolated review calls), Round
    3 (1 Chairman call). Never catches an SDK exception — a failure here
    propagates to the caller, which must not cache a partial result. Every
    call that DOES complete before a failure is still appended to
    `call_info`, so real spend is never silently dropped from the audit
    trail even when the run as a whole fails."""
    model = cfg.get("flags", {}).get("model", "claude-sonnet-5")
    prompt_version = cfg.get("council", {}).get("prompt_version", "v1")
    user_msg = render_bundle_text(bundle)

    advisors, integrity_note = _run_round1_once(client, model, user_msg, call_info)
    violations = [a.name for a in advisors if not a.parsed]
    if violations:
        advisors, integrity_note = _run_round1_once(client, model, user_msg, call_info)
        violations = [a.name for a in advisors if not a.parsed]
    format_degraded = bool(violations)

    reviews = []
    for name in _ADVISOR_NAMES:
        anon = _anonymize_others(advisors, name)
        review_user = f"{user_msg}\n\n=== PEER OPINIONS (anonymized) ===\n{anon}"
        review_text = _call(client, model, _REVIEWER_PROMPT, review_user, max_tokens=3000,
                             call_type="council_review", call_info=call_info)
        reviews.append(ReviewNote(reviewer=name, text=review_text.strip()))

    opinions_block = "\n\n".join(f"=== {a.name} ===\n{a.text}" for a in advisors)
    reviews_block = "\n\n".join(f"=== Review by {r.reviewer} ===\n{r.text}" for r in reviews)
    chairman_user = (
        f"{user_msg}\n\n=== ROUND 1 OPINIONS ===\n{opinions_block}"
        f"\n\n=== ROUND 2 REVIEWS ===\n{reviews_block}"
    )
    chairman_text = _call(client, model, _CHAIRMAN_PROMPT, chairman_user, max_tokens=6000,
                           call_type="council_chairman", call_info=call_info)
    chairman = _parse_chairman(chairman_text)

    calls = call_info if call_info is not None else []
    truncated = any(c.get("stop_reason") == "max_tokens" for c in calls)
    status_flags = []
    if truncated:
        status_flags.append("truncated")
    if format_degraded:
        status_flags.append("format_degraded")

    total_input = sum(c.get("input_tokens") or 0 for c in calls)
    total_output = sum(c.get("output_tokens") or 0 for c in calls)
    total_cost = compute_council_cost_usd(cfg, model, total_input, total_output) if calls else None

    meta = CouncilMeta(
        ticker=ticker, model=model, prompt_version=prompt_version,
        config_hash=bundle.config_hash, convened_at=datetime.now(timezone.utc).isoformat(),
        accession=bundle.accession,
        thesis_status="pre_thesis" if bundle.pre_thesis else "thesis_present",
        thesis_hash=bundle.thesis_hash, evidence_integrity_note=integrity_note,
        total_cost_usd=total_cost, total_input_tokens=total_input, total_output_tokens=total_output,
        status_flags=status_flags, calls=list(calls),
    )
    return CouncilResult(ticker=ticker, advisors=advisors, reviews=reviews, chairman=chairman, meta=meta)


# ---------------------------------------------------------------------------
# On-disk cache — keyed by (accession, thesis_tag, prompt_version, model).
# Only a fully SUCCESSFUL convene() is ever cached — same rule as
# engine/flags.py, learned there the hard way: a partial/failed run must
# never be served back as if it were a real result.
# ---------------------------------------------------------------------------

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.\-]")


def _cache_key(accession: str, thesis_tag: str, prompt_version: str, model: str) -> str:
    raw_key = f"{accession}_{thesis_tag}_{prompt_version}_{model}"
    return _UNSAFE_CHARS_RE.sub("_", raw_key)


def _cache_path(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str) -> Path:
    return cache_dir / f"{_cache_key(accession, thesis_tag, prompt_version, model)}.json"


def is_cached(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str) -> bool:
    return _cache_path(cache_dir, accession, thesis_tag, prompt_version, model).exists()


def load_cached(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str) -> Optional[CouncilResult]:
    p = _cache_path(cache_dir, accession, thesis_tag, prompt_version, model)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return CouncilResult(
        ticker=data["ticker"],
        advisors=[AdvisorOpinion(**a) for a in data["advisors"]],
        reviews=[ReviewNote(**r) for r in data["reviews"]],
        chairman=ChairmanOutput(**data["chairman"]),
        meta=CouncilMeta(**data["meta"]),
    )


def _save_cached(cache_dir: Path, accession: str, thesis_tag: str, prompt_version: str, model: str, result: CouncilResult) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache_path(cache_dir, accession, thesis_tag, prompt_version, model).write_text(json.dumps(asdict(result)))


def get_council(
    ticker: str,
    bundle: EvidenceBundle,
    cfg: dict,
    client,
    cache_dir: Path,
    force_refresh: bool = False,
    call_info: Optional[list] = None,
) -> CouncilResult:
    """Cache-or-convene. force_refresh=True (the endpoint's ?refresh=true)
    skips the cache READ but still WRITES the new result. `call_info`, if
    given, ends up empty on a cache hit (zero calls made) or populated
    with one record per real call on a live/refresh run — the caller uses
    len(call_info) to tell which happened, same convention as
    engine.flags.get_flags's call_info out-param."""
    model = cfg.get("flags", {}).get("model", "claude-sonnet-5")
    prompt_version = cfg.get("council", {}).get("prompt_version", "v1")
    thesis_tag = bundle.thesis_hash if not bundle.pre_thesis else "prethesis"

    cached = None if force_refresh else load_cached(cache_dir, bundle.accession, thesis_tag, prompt_version, model)
    if cached is not None:
        return cached

    result = convene(ticker, bundle, cfg, client, call_info=call_info)
    _save_cached(cache_dir, bundle.accession, thesis_tag, prompt_version, model, result)
    return result


# ---------------------------------------------------------------------------
# Cost estimation / stamping — reuses config.yaml `flags.pricing` directly
# (same pinned model, same spend ledger pricing table; no separate council
# pricing table to keep in sync). Missing pricing returns None, never a
# fabricated 0.0 — absence-is-not-zero applies to a real API call's cost
# exactly as much as to any other number in this codebase.
# ---------------------------------------------------------------------------

# Rough per-call token baselines for the 7-call structure, used only for
# the ticker-independent "not yet convened" cost estimate. The real,
# stamped cost of an actual run always comes from real SDK token counts
# (compute_council_cost_usd), never this baseline.
_BASELINE_ROUND1_INPUT = 20_000
_BASELINE_ROUND1_OUTPUT = 2_500
_BASELINE_ROUND2_INPUT = 22_000
_BASELINE_ROUND2_OUTPUT = 400
_BASELINE_ROUND3_INPUT = 26_000
_BASELINE_ROUND3_OUTPUT = 1_800


def _pricing_for(cfg: dict, model: str) -> Optional[dict]:
    return cfg.get("flags", {}).get("pricing", {}).get(model)


def estimate_council_cost_usd(cfg: dict) -> Optional[float]:
    """Ticker-independent estimate for the "not yet convened" GET response.
    Returns None (never a fabricated number) if the pinned model has no
    pricing entry in config.yaml flags.pricing."""
    model = cfg.get("flags", {}).get("model", "")
    pricing = _pricing_for(cfg, model)
    if not pricing:
        return None
    total_input = _BASELINE_ROUND1_INPUT + 5 * _BASELINE_ROUND2_INPUT + _BASELINE_ROUND3_INPUT
    total_output = _BASELINE_ROUND1_OUTPUT + 5 * _BASELINE_ROUND2_OUTPUT + _BASELINE_ROUND3_OUTPUT
    cost = (
        total_input / 1_000_000 * pricing.get("input_per_million", 0)
        + total_output / 1_000_000 * pricing.get("output_per_million", 0)
    )
    return round(cost, 2)


def compute_council_cost_usd(cfg: dict, model: str, input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    """Real cost from real SDK token counts, priced against config.yaml
    flags.pricing AT CALL TIME. Returns None (not 0.0) when the model has
    no pricing entry — an honest "we don't know" belongs in the stored
    record and the spend ledger, not a fabricated free call."""
    pricing = _pricing_for(cfg, model)
    if not pricing:
        return None
    cost = (
        (input_tokens or 0) / 1_000_000 * pricing.get("input_per_million", 0)
        + (output_tokens or 0) / 1_000_000 * pricing.get("output_per_million", 0)
    )
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Config validation — fail fast at startup, same discipline as
# engine.flags.validate_flags_config.
# ---------------------------------------------------------------------------

def validate_council_config(cfg: dict) -> None:
    council_cfg = cfg.get("council")
    if not council_cfg:
        return  # feature not configured yet — fine, endpoint reports "not configured" behavior via missing pricing/model
    if not isinstance(council_cfg.get("prompt_version", "v1"), str):
        raise ValueError("config.yaml council.prompt_version must be a string.")
