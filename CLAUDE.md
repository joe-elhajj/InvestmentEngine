# Investment Engine — Claude Code context

## System overview

The engine is a three-tier investment research system. **Tier 1 (built)** is a
fully deterministic fundamental-analysis pipeline: SEC EDGAR filings are the
authoritative data source, every number is derived arithmetically with no model
involvement, and all assumptions live in version-controlled config. Beyond the
core fundamentals pipeline, Tier 1 also owns the five-category durability
scorecard (`engine/durability.py`), the R&D capitalization regime (Damodaran
matched-window method, currently ON — see `docs/assumptions.md`), a raw-metric
balance-sheet-leverage gate that caps the durability composite independently of
the weighted score, and the bull/base/bear expectations-gap scenario band
(`engine/valuation.py`) — all still zero model involvement. **Tier 2
(in progress)** adds LLM-driven qualitative extraction layered on top of the
deterministic output; the first slice — verbatim red/green flag extraction from
10-K Item 1/1A/7 text (`engine/flags.py`) — is built. Moat classification and
other Tier 2 annotations are not yet built. **Tier 3 (built)** is an LLM
council that synthesises Tier 1 signals and Tier 2 annotations into a final
research memo; the production endpoint (`engine/council.py`, a spend-gated
`/api/council/{ticker}`) runs a 7-call hybrid structure — one combined Round 1
across all five advisors, five isolated Round 2 peer reviews, one Chairman
synthesis. Tiers 2 and 3 consume Tier 1 output; they never alter it.

A local FastAPI web app (`app/`) and dashboard (`frontend/`) wrap all three
tiers for an ongoing watchlist; `analyze.py` is the single-ticker CLI entry
point. A full-system audit (`audit/session_d/report.md`) recorded the current
set of known, unfixed findings against the live system — read it before
assuming a surprising result is a new bug; it may already be diagnosed there.

## Module responsibilities

| File | Owns |
|---|---|
| `engine/edgar.py` | SEC EDGAR fetch: filing forms, XBRL tags, multi-currency annual series, per-`Fact` filing lineage (`accn`, `source_ref()`) |
| `engine/metrics.py` | Pure financial calculations — no I/O, no network, no model |
| `engine/pipeline.py` | Deterministic spine: EDGAR → metrics → peers → valuation (no LLM) |
| `engine/valuation.py` | Relative and absolute valuation; the reverse-DCF bull/base/bear expectations-gap scenario band (`expectations_gap_band()`); all assumptions come from `config.yaml` |
| `engine/durability.py` | Five-category business-durability scorecard with lineage tracing; the R&D capitalization regime (matched-window); the raw-metric balance-sheet-leverage gate (`gate_status_of()`, `_evaluate_gate()`) |
| `engine/peers.py` | Analyst-owned comp set; peer-relative percentile ranking — wired into `analyze.py`'s CLI only, not the web app (known gap, see `audit/session_d/report.md`) |
| `engine/universe.py` | S&P 500 reference population for universe-relative percentile scoring |
| `engine/market.py` | Current price and share count (market-vendor tier; isolated from filings) |
| `engine/etf.py` | ETF/fund profile via yfinance (market-vendor tier, lower trust, fully defensive) |
| `engine/screen.py` | Batch screener: routes tickers to Equities / ETFs & Funds / Excluded |
| `engine/report.py` | Render `AnalysisResult` to Markdown with full EDGAR citation per figure |
| `engine/report_html.py` | Render `AnalysisResult` to self-contained HTML |
| `engine/filings.py` | Tier 2: fetch/parse 10-K document text into Item 1/1A/7 sections (no model involvement) |
| `engine/flags.py` | Tier 2: LLM verbatim-selection flag extraction + the verbatim validator (see invariant below) |
| `engine/council.py` | Tier 3: adversarial council synthesis — 5 advisors → 5 blind reviews → Chairman (see invariant below) |
| `engine/report_council.py` | Render a `CouncilResult` (Tier 3) to self-contained HTML/PDF, plus its disk cache |
| `analyze.py` | Single-ticker CLI entry point (Tier 1 report + optional peer comparison) |
| `app/main.py` | FastAPI web layer wrapping the engine: watchlist CRUD, `/api/analyze/{ticker}` (+ `/json`, `/fragment`), `/api/screen`, `/api/flags/{ticker}`, `/api/council/{ticker}` |
| `app/watchlist.py` / `app/usage.py` / `app/pdf.py` | SQLite-backed watchlist store; SQLite-backed Tier 2/3 spend ledger; isolated `html_to_pdf()` |
| `frontend/app.js` + `frontend/styles.css` | Dashboard UI (vanilla JS, no build step) — the badge/chip vocabulary below is implemented here and in `engine/report_html.py` together |

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

**Config-hash discipline.** Every `DurabilityScore` embeds a 16-hex-char
SHA-256 fingerprint so cross-company comparisons across different assumption
sets are identifiable. `engine/durability.py::_config_hash()` and
`app/main.py::_config_hash()` are two distinct functions with the same name
that hash different things: the former hashes only the resolved durability
`weights`/`thresholds`/`universe_version` subset, the latter hashes the full
`config.yaml` (including valuation/DCF assumptions) for the single-ticker
endpoint, which runs no durability scoring. Both changed hashes correctly
signal "different assumption set," but neither is "the hash of config.yaml"
on its own — a prior draft of this doc claimed that incorrectly. Disambiguating
the two names is tracked as a pending fix (Session B Item 5, not yet done).
Never hardcode assumptions in code; they belong in `config.yaml`.

**R&D matched-window adjustment applies only where an adjustment path
exists.** The matched-window view (`_rnd_matched_window_view`) must never be
built for a company with zero adjustment path (NO_RND, or real R&D data that
never accumulates a full consecutive window) — that company takes the
untouched full-history GAAP path instead, byte-identical to the regime being
off. Gate on the evidence (`n_adjusted_total > 0`) *before* building the
matched view, never infer "no adjustment" from an empty window after the
fact — routing a no-adjustment company through the matched-window view
coerces its genuine GAAP figures to `None` (presence treated as absence, the
inverse of absence-is-not-zero). This was a real, shipped bug (PR 2a) before
it became this invariant.

**Gates key on raw metrics, never on sub-scores.** A durability gate
(`engine/durability.py`'s `gates` layer) reads a raw filing value (e.g.
`net_debt`, `ebitda`) directly off `YearlyDerived`, never a computed
sub-score — a sub-score already carries curve/saturation artifacts between
the analyst's threshold belief and the number it fires on; the raw metric is
what the anchor in `docs/assumptions.md` is written in. A gate's cap is
`min(ungated_composite, cap)`, never an unconditional override — a veto must
only ever push a composite down or leave it unchanged, never raise one that
was already below the cap on its own merits (a real bug, caught live during
PR 4's own verification, is why this is a house rule and not just an
implementation detail).

**Every abstain-and-disclose path needs a render-assertion test.**
(Session D meta-finding.) A unit test proving a value correctly comes back
`None`/unadjusted is not sufficient — the codebase has shipped more than one
case (the original `ds.gaps` bug, the FPI R&D-UNADJ badge never firing for
an FPI with usable R&D data) where the abstention was computed correctly but
the disclosure never actually reached a rendered surface. Any new
abstain-and-disclose path needs a test that asserts the disclosure text
appears in the real rendered output (Markdown/HTML/dashboard), not only that
the underlying value is withheld or computed correctly.

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

**Badge/chip vocabulary.** A small, closed set of short-code badges discloses
upstream caveats on the dashboard and in the HTML fragment, each a reserved,
always-in-the-DOM slot (`visibility:hidden` when inapplicable, never
`display:none`, so layout never shifts and a clean row never announces a
phantom badge to assistive tech): **MKT** (a headline figure rests on a
market-vendor-tier input, e.g. yfinance share count, not EDGAR), **WIN**
(delivered growth uses an extended/non-standard CAGR window), **REV**
(delivered growth is a revenue-CAGR fallback, not FCF), **INH** (the Gap
column inherits an MKT/WIN/REV caveat from one of its inputs), **DUR** (a
`DurabilityScore.gaps` disclosure — net-cash resilience, mixed-basis,
short-history, split-contamination — as opposed to a pipeline `res.gaps`
entry), **FRAG** (the expectations-gap band's sign flips across bull/base/bear
scenarios) with an **UNDETERMINABLE** variant (**FRAG?**) when the band is
PARTIAL (a scenario's bisection missed its bracket, so fragility can't be
assessed at all — never silently collapsed into "not fragile"), and **GATE**
(the balance-sheet gate fired, composite capped) with an **UNTESTABLE**
variant (**GATE?**) when the gate's raw inputs don't resolve — absence is
never treated as a pass. New badges follow this same vocabulary rather than
inventing a new visual language.

## Workflow

Branch → PR → CI green → merge. Never merge on red CI. The CI run is the
single source of truth for whether a change is safe to land; local test runs
are for speed of iteration, not for authorising a merge.

PRs that add a user-facing feature or module must update README's
architecture/usage sections in the same PR.

## Pointers

- **`docs/assumptions.md`** — every owned assumption with its external anchor,
  current value, and review cadence. Update this whenever `config.yaml`
  valuation or scenario parameters change. Also documents known SEC EDGAR
  `companyfacts` data-source limitations (genuine data absence, share-count
  series discontinuities, the open "B.3" instant-concept fiscal-year-end
  alignment investigation) diagnosed in Session B.2 — read this before
  re-investigating a durability gap or a flagged discontinuity that looks
  like a bug; it may already be a diagnosed, documented limitation.
- **`config.yaml`** — all tunables: SEC credentials, history window,
  classification overrides, valuation assumptions, DCF scenarios (bull/base/
  bear bundles the expectations-gap band runs under), screening weights,
  `durability.rnd_capitalization` (the R&D regime toggle + amortization
  window) and `durability.gates` (the balance-sheet-leverage veto — an
  extensible list, one entry shipped so far), Tier 2's `flags:` section
  (pinned model/prompt_version + pricing table + analyst `overrides:`), and
  Tier 3's `council:` section (prompt_version only — reuses
  `flags.model`/`flags.pricing` directly). The code never invents values at
  runtime.
- **`.claude/skills/llm-council/SKILL.md`** — the manually-run Tier 3
  adversarial council skill, triggered by "run council on TICKER", "council
  this", "war room". Same five-advisor → peer-review → Chairman shape as
  `engine/council.py`'s production endpoint below, but ad hoc and
  conversational rather than spend-gated/cached/API-driven.
- **`engine/council.py` + `GET`/`POST /api/council/{ticker}`** — the
  production Tier 3 endpoint. `GET` reports `blocked_no_flags` /
  `not_cached` (with a cost estimate) / the cached record, and never spends;
  `POST ?convene=true` is the only path that runs the real 7 calls.
- **`audit/`** — dated audit evidence, never fixes. `audit/sensitivity.py`
  is the durability weight/threshold/impute sensitivity harness (Session C);
  `audit/session_d/report.md` is the current findings ledger + dispositions
  table from the most recent full-system audit — the source of truth for
  known, unfixed issues (read it before assuming a surprising result is a
  new bug). `tests/session_d_probes/` holds that audit's quarantined probe
  suite: probes that confirmed correct behavior run as plain tests; probes
  that caught a real, still-open finding are marked
  `@pytest.mark.xfail(strict=True, reason="F-N: ...")` citing the finding ID
  — when a post-audit PR fixes that finding, remove its `xfail` marker and
  the probe becomes a permanent regression test, no rewrite needed. Do not
  delete an xfail probe to make a finding "go away"; fix the underlying
  issue and let the probe flip green.

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
