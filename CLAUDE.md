# Investment Engine — Claude Code context

## System overview

The engine is a three-tier investment research system. **Tier 1 (built)** is a
fully deterministic fundamental-analysis pipeline: SEC EDGAR filings are the
authoritative data source, every number is derived arithmetically with no model
involvement, and all assumptions live in version-controlled config. **Tier 2
(in progress)** adds LLM-driven qualitative extraction layered on top of the
deterministic output; the first slice — verbatim red/green flag extraction from
10-K Item 1/1A/7 text (`engine/flags.py`) — is built. Moat classification and
other Tier 2 annotations are not yet built. **Tier 3 (built)** is an LLM
council that synthesises Tier 1 signals and Tier 2 annotations into a final
research memo; the production endpoint (`engine/council.py`, a spend-gated
`/api/council/{ticker}`) runs a 7-call hybrid structure — one combined Round 1
across all five advisors, five isolated Round 2 peer reviews, one Chairman
synthesis. Tiers 2 and 3 consume Tier 1 output; they never alter it.

## Module responsibilities

| File | Owns |
|---|---|
| `engine/edgar.py` | SEC EDGAR fetch: filing forms, XBRL tags, multi-currency annual series |
| `engine/metrics.py` | Pure financial calculations — no I/O, no network, no model |
| `engine/pipeline.py` | Deterministic spine: EDGAR → metrics → peers → valuation (no LLM) |
| `engine/valuation.py` | Relative and absolute valuation; all assumptions come from `config.yaml` |
| `engine/durability.py` | Five-category business-durability scorecard with lineage tracing |
| `engine/peers.py` | Analyst-owned comp set; peer-relative percentile ranking |
| `engine/universe.py` | S&P 500 reference population for universe-relative percentile scoring |
| `engine/market.py` | Current price and share count (market-vendor tier; isolated from filings) |
| `engine/etf.py` | ETF/fund profile via yfinance (market-vendor tier, lower trust, fully defensive) |
| `engine/screen.py` | Batch screener: routes tickers to Equities / ETFs & Funds / Excluded |
| `engine/report.py` | Render `AnalysisResult` to Markdown with full EDGAR citation per figure |
| `engine/report_html.py` | Render `AnalysisResult` to self-contained HTML |
| `engine/filings.py` | Tier 2: fetch/parse 10-K document text into Item 1/1A/7 sections (no model involvement) |
| `engine/flags.py` | Tier 2: LLM verbatim-selection flag extraction + the verbatim validator (see invariant below) |
| `engine/council.py` | Tier 3: adversarial council synthesis — 5 advisors → 5 blind reviews → Chairman (see invariant below) |

## Non-negotiable invariants

**Absence-is-not-zero.** A `None` field means the data point was not found or
not applicable. Never substitute `0.0` or any default value silently. Callers
must propagate `None`; renderers must display a meaningful marker (`—`, `n/a`).

**LLMs never touch arithmetic.** All financial calculations live in
`engine/metrics.py` and `engine/valuation.py` as pure functions. No model call
may derive, adjust, or override a numeric result. This is unchanged and
absolute — the exception below does not weaken it.

**LLM verbatim-selection boundary (Tier 2).** The only sanctioned use of a
model anywhere in this codebase is to SELECT verbatim text spans from a
filing — never to generate, paraphrase, or compute. Every snippet a model
returns (`engine/flags.py`) MUST be validated as an exact substring of the
source filing text it was given; any snippet that is not an exact substring
is DROPPED as a hallucination and logged to stdout/stderr
(`! TICKER: dropped non-verbatim snippet`) — never surfaced to a user. A
flag's `verified_verbatim` field is set ONLY by this validator, never trusted
from the model's own claim. This validation is non-negotiable and has its own
dedicated test (`tests/test_flags.py::TestVerifyVerbatim`).

**LLM synthesis boundary (Tier 3).** The second — and, alongside Tier 2's
verbatim-selection boundary, only — sanctioned use of a model in this
codebase is to SYNTHESISE and ARGUE over evidence Tier 1/2 already produced
(`engine/council.py`). It never derives, adjusts, or recomputes a number;
every prompt requires "not in evidence" as a valid answer instead. A council
run never triggers a Tier 2 flag extraction of its own — if flags aren't
already cached for a ticker, `/api/council/{ticker}` reports that plainly and
stops. Spend discipline mirrors Tier 2 exactly: `GET` never spends, `POST
?convene=true` is the only path that does, and only a fully successful run is
ever cached — a failed or partial run is never served back as if real.

**Evidence-based classification.** A security's type (operating equity, FPI,
fund) is inferred only from positive evidence: SEC form history for equities
and FPIs, `quoteType` from yfinance for ETFs/funds. Fund status must never be
inferred from EDGAR absence alone — absence of a CIK means "unknown", not
"fund". A `quoteType` of `ETF` or `MUTUALFUND` is the required positive
confirmation.

**Config-hash discipline.** Every `DurabilityScore` embeds the first 16 hex
characters of the SHA-256 hash of `config.yaml`. Any assumption change changes
the hash, making cross-company comparisons across different assumption sets
immediately identifiable. Never hardcode assumptions in code; they belong in
`config.yaml`.

**No credentials in commands or output.** Never extract, print, or embed
credentials or tokens in bash commands or command output. Use the authenticated
`gh` CLI for all GitHub operations (`gh pr create`, `gh pr checks`,
`gh pr merge`); never raw `curl` with an `Authorization` header.

## Conventions in use

**pytest.** Tests use plain `assert` — no `unittest`-style methods, no
`self.assert*`. Fixtures are function-scoped by default.

**Dataclasses.** All structured data types use `@dataclass`. No ad-hoc `dict`
for public interfaces; `None` as a typed field expresses "field exists but
value unavailable".

**Gap-logging.** `screen.py` prints `! TICKER: reason` to stdout whenever a
ticker is flagged, excluded, or routed unexpectedly. This is the primary
diagnostic signal for screening runs. `engine/flags.py` reuses the same
`! TICKER: reason` convention (to stderr) for dropped non-verbatim snippets.

**Derived-lineage pattern.** `DurabilityScore` stores per-sub-score rationale
strings alongside numeric values so reports can reproduce exactly how each
number was computed without re-running the pipeline.

## Workflow

Branch → PR → CI green → merge. Never merge on red CI. The CI run is the
single source of truth for whether a change is safe to land; local test runs
are for speed of iteration, not for authorising a merge.

## Pointers

- **`docs/assumptions.md`** — every owned assumption with its external anchor,
  current value, and review cadence. Update this whenever `config.yaml`
  valuation or scenario parameters change.
- **`config.yaml`** — all tunables: SEC credentials, history window,
  classification overrides, valuation assumptions, DCF scenarios, screening
  weights, Tier 2's `flags:` section (pinned model/prompt_version + pricing
  table + analyst `overrides:`), and Tier 3's `council:` section
  (prompt_version only — reuses `flags.model`/`flags.pricing` directly). The
  code never invents values at runtime.
- **`.claude/skills/llm-council/SKILL.md`** — the manually-run Tier 3
  adversarial council skill, triggered by "run council on TICKER", "council
  this", "war room". Same five-advisor → peer-review → Chairman shape as
  `engine/council.py`'s production endpoint below, but ad hoc and
  conversational rather than spend-gated/cached/API-driven.
- **`engine/council.py` + `GET`/`POST /api/council/{ticker}`** — the
  production Tier 3 endpoint. `GET` reports `blocked_no_flags` /
  `not_cached` (with a cost estimate) / the cached record, and never spends;
  `POST ?convene=true` is the only path that runs the real 7 calls.

## Merge safety (non-negotiable — three silent-loss incidents to date)
Before ANY `gh pr merge`:
1. `git log origin/<branch>..<branch>` MUST be empty — unpushed commits +
   `--delete-branch` = permanent-looking work loss (recoverable only via
   reflog/fsck within ~90 days).
2. CI green counts ONLY if the run timestamp matches the current branch tip.
   A stale green from an earlier push is not validation.
3. After deleting a branch, if recovering lost commits, use
   `git fsck --unreachable --no-reflogs` — reflog-walking alone missed
   orphaned commits above a tip twice.
