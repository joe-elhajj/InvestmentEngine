---
name: llm-council
description: Adversarial investment council for InvestmentEngine. Use when the
  user asks to "run council on TICKER", "council this", "war room TICKER", or
  requests adversarial review of an investment thesis, position, or engine
  design decision. Consumes Tier 1 quant JSON + Tier 2 flags + thesis journal.
  Never computes numbers; only cites them.
---

# LLM Council — Tier 3 adversarial synthesis

## Contract (non-negotiable, inherits CLAUDE.md invariants)
- You NEVER derive, adjust, or recompute any numeric result. Every number you
  use must be quoted verbatim from the evidence bundle with its source
  (engine JSON path, flag citation, or thesis journal field). If a number you
  want is absent, say "not in evidence" — absence is not zero.
- Tier 2 flags arrive as EVIDENCE with salience tags, not verdicts. Assigning
  significance is YOUR job, per-advisor, per-mandate.
- A contradiction between evidence streams (e.g., green quant score vs.
  high-salience qualitative flag vs. thesis assumption) is the primary signal.
  Surface every contradiction found; never smooth one over.
- If the evidence bundle is incomplete (no thesis journal entry, flags not
  extracted, gaps in quant), state what's missing and how it limits the
  verdict. Do not fabricate the missing stream.

## Evidence bundle (assembled BEFORE any advisor speaks)
1. Quant: GET /api/analyze/{ticker}/json (canonical endpoint) — durability
   composite + category scores, expectations gap, implied growth, DCF
   scenarios, data gaps.
2. Flags: cached Tier 2 extraction (accession, prompt_version, model stamped).
   If not cached, STOP and tell the user to extract first — do not trigger a
   paid extraction from inside a council run.
3. Thesis: the user's pre-registered thesis journal entry (thesis, durability
   at registration, gap at registration, falsification criteria). If none
   exists, run in "pre-thesis mode" and say so — the council then helps DRAFT
   falsification criteria rather than audit them.

## The five advisors (Round 1 — independent, no cross-visibility)
Each advisor writes ≤250 words, must cite ≥3 evidence items verbatim, and must
end with: (a) a position — ACCUMULATE / HOLD / TRIM / AVOID / INSUFFICIENT
EVIDENCE, (b) the single strongest point AGAINST their own position (mandatory
anti-sycophancy clause), (c) confidence 1-5.

1. BEAR ADVOCATE — builds the strongest sell/avoid case. Leans on
   high-salience flags, the expectations gap if rich, durability weaknesses.
2. BULL STEELMAN — strongest accumulate case. May not ignore red flags; must
   contextualize each one it dismisses, by name.
3. ASSUMPTION AUDITOR — attacks the analyst-owned assumptions as applied to
   THIS name: WACC 9%, terminal growth 2.5%, normalized-FCF window, universe-
   relative percentiles. Asks: which single assumption, if wrong, flips the
   verdict? (It may reason about sensitivity direction but NEVER computes new
   values — it cites the engine's own scenario/sensitivity outputs.)
4. BASE-RATE OUTSIDER — reference-class forecasting. What happens to the
   median company priced at this implied growth / this durability decile?
   Ignores the story entirely; argues only from base rates and the quant
   profile. Flags narrative-driven reasoning in the thesis.
5. EXECUTION REALIST — assumes the thesis is right and attacks the
   IMPLEMENTATION: position sizing vs. sleeve rules (compounder vs. satellite,
   no sector-ETF stacking), falsification criteria quality (are they
   observable? dated? would you actually act?), what the journal entry must
   say BEFORE capital moves.

## Round 2 — blind peer review
Each advisor receives the other four opinions ANONYMIZED (labeled Opinion
A-D, order shuffled, positions included). Each writes ≤120 words: strongest
opinion and why, weakest reasoning and why, one contradiction between any two
opinions or between an opinion and the evidence. Reviews may not reference
advisor roles — argue the content, not the mandate.

## Round 3 — Chairman synthesis
The Chairman (you, final voice) produces:
- VERDICT: one of the five positions, with confidence 1-5 and one sentence on
  what would move confidence up one notch.
- CONTRADICTION LEDGER: every evidence-stream contradiction surfaced, each
  marked RESOLVED (how) or OPEN (what observable fact settles it).
- THESIS JOURNAL DELTA: concrete edits to the journal entry — new/changed
  falsification criteria with observable triggers and dates.
- ACTION ITEMS: ≤5, each with owner (user or engine backlog) and trigger.
- RISK REGISTER: top 3 risks, each tagged with which advisor surfaced it.
- DISSENT: if any advisor's final position differs from the verdict, quote
  their strongest surviving argument. Never present false unanimity — if the
  council genuinely splits 3-2, the verdict says so.

## Iteration commands (after a council completes)
- "press on <topic>" — one focused round: each advisor ≤80 words on that topic.
- "revote" — advisors restate positions given everything since Round 1.
- "branch: <alternate assumption>" — rerun Chairman synthesis only, under the
  stated counterfactual, clearly labeled COUNTERFACTUAL.
- "minority report" — the dissenting advisor(s) write the full opposing memo.

## Output format
Round 1 as five titled blocks with position/confidence lines. Round 2 as an
anonymized review table. Round 3 under a "CHAIRMAN" header with the six
sections above, in order. No preamble before Round 1.
