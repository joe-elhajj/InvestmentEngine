# Session D — Full-System Audit Report

Branch `session-d-audit` off `main` @ `7ac069e`. Read-only throughout —
see the read-only proof at the end of this file. Full evidence, every
prediction registered before each probe, and the raw supporting data
live in `audit/session_d/phase0_map.md` (Phases 0–3, written
incrementally); this file is the Phase 4 synthesis: the findings
ledger, the dispositions table, and the probe-suite summary.

---

## Findings ledger

Ranked most-severe first within each classification tier. Every finding
is unfixed — read-only audit, all become post-audit PRs.

### BUG

**F-14 — R&D-UNADJ badge never fires for an FPI with usable R&D data
(both report renderers).** HIGH severity, ranked first for the
post-audit queue. `report.py` and `report_html.py` both check "does
`roic_adjusted` resolve?" before checking "is this an FPI?" — the FPI
abstention (`_rnd_unadj_reason`'s `if fpi: return "IFRS filer — pending
disposition"`) is only ever consulted as a fallback explanation for
absence, never as a gate on display in its own right. Durability
SCORING correctly abstains (confirmed via clean, unannotated sub-score
lineage — no fix needed there). The bug is display-only, but it
actively shows a number — R&D-adjusted ROIC for a filer under IAS 38,
where the adjustment's error is of ambiguous sign per the codebase's own
documented rule — with zero badge, zero caveat. Confirmed end-to-end
with a synthesized FPI fixture carrying a full, real R&D series
(Phase 3g). No real FPI exists in the audited 15 to have ever triggered
this live. **Remedy**: check `is_fpi()` first in both renderers,
mirroring `durability.py`'s own correct ordering (FPI check before
data-availability check).

**F-6 — a majority of the gate-untestable population is likely
extraction-miss, not genuine data absence.** Of 94 S&P 500 names
gate-untestable (no computable `net_debt`/`EBITDA`), direct XBRL-tag
inspection confirmed 2 names (GM, KO) missing debt data only because
`engine/edgar.py`'s concept list doesn't include the combined
debt-and-capital-lease tag family (`LongTermDebtAndCapitalLeaseObligations*`,
`DebtAndCapitalLeaseObligations*`) several large companies now use. A
full-population scoping pass (not a sample) found this confirmed for
**28 of the 58 net_debt-missing names** (48%); the `ebitda`-missing
group (36 names) uses a different, entirely unscoped mechanism where
the initial 8-name sample already found 2 of 3 likely extraction-miss
(IBM, OXY). **Estimated true extraction-miss share across the full 94:
a floor of 30%, very plausibly 50%+ once fully scoped** — reversing the
comfortable "mostly legitimate, thin coverage" reading the bare 18.7%
untestable rate would suggest alone. **Remedy**: add the confirmed
alternate concept family to `engine/edgar.py`'s debt-concept list as a
fallback; separately scope and fix the `ebitda`-missing mechanism
(likely a different alternate-concept or derived-computation gap).

**F-8 — a `revenue`-gated ratio treats exact `0.0` the same as `None`.**
Confirmed via the exact-zero injection sweep (2b), which exists
specifically to catch this class of violation. At least one ratio
guards with a truthy check (`if revenue and revenue > 0`) rather than
`if revenue is not None and revenue > 0` — a real (if extreme, e.g.
pre-revenue) `revenue == 0.0` year is silently excluded the same way a
missing value would be, instead of being scored as a real ratio or
explicitly disclosed as excluded. Not exhaustively enumerated across
every `revenue`-consuming guard in this pass — confirmed for one
mechanism, flagged as a lead. **Remedy**: audit every truthy guard on a
financial value for the `is not None` vs truthy distinction; this is
exactly the class of bug the codebase's own absence-is-not-zero
invariant exists to prevent.

**F-11 — a torn cache file permanently poisons one ticker until manual
deletion.** Moderate severity. `EdgarClient._read_cache` doesn't
distinguish "no cache" from "corrupt cache" — a truncated/corrupted
`.cache/edgar/{cik}_{kind}.json` (the exact failure mode the
architecture notes' own non-atomic-write concern predicts) raises
uncaught, forever, until a human finds and deletes the file. Every real
call site already catches the resulting exception gracefully (screen
batch continues, web endpoints return clean 502s) — this never crashes
the app, it just gets one ticker permanently stuck. Confirmed against a
scratch copy of AAPL's real cache file; the real cache was never
touched. **Remedy**: catch `JSONDecodeError`/`UnicodeDecodeError`
specifically in `_read_cache`, log a `! {cik}_{kind}: cache corrupt,
refetching` diagnostic, return `None` (falls through to the existing
fetch-and-overwrite path).

**F-13 / F-9 (fold into one PR) — gate lineage text is stale relative
to the min()-based cap.** Minor-to-moderate. F-13: the lineage string
unconditionally says `"-> composite capped {cap}"` even when
`min(ungated, cap)` left the composite unchanged (confirmed live: NEE's
displayed Durability value is 44.6, but its own tooltip says "capped
45.0"). F-9: at a ratio that rounds to the same displayed value as the
threshold (e.g. 6.01 vs 6.0), the lineage prints as the self-contradictory
`"6.0 > 6.0"` — cosmetic only, the underlying comparison is confirmed
correct (2c). Both are residues of the same PR 4 min()-fix leaving old-
behavior display strings behind. **Remedy**: make the lineage string
conditional on whether the cap actually changed the composite; show
one more decimal of ratio precision when it would otherwise round to
the threshold's own displayed value.

### DISCLOSURE-GAP

**F-7 — nine trend/mean-based sub-scores silently narrow their window
with no disclosure.** HIGH among non-BUG findings (analyst priority
note: generalizes a principle already adopted once). Confirmed via the
None-injection sweep (2a): removing the latest year's value for
`capex`, `gross_margin`, `invested_capital`, `nopat`, `operating_margin`,
`research_asset`, `revenue`, `rnd`, or `sbc` correctly drops that year
from the relevant trend/mean (per-year list comprehensions already
filter `is not None` — no zero-coercion), but produces **zero new
gaps-list entry** for any of them — unlike `reinvestment_engine`'s
R&D-adjusted `roic_mean`/`compounding_proxy`, which DOES get an
explicit "rests on N of M years" disclosure, but only under the R&D
regime. **This is the same disease as the original ds.gaps bug and as
F-14**: a caveat is computable, but nothing renders it. **Remedy**:
extend the short-history disclosure pattern already built for
`reinvestment_engine` to the other 9 trend-based sub-scores across
`quality_persistence`, `capital_discipline`, and `optionality_proxies` —
completing a principle already adopted, not inventing a new one.

**F-2 — `valuation.min_history_years` silently bypasses
`durability.py`'s own config hash.** `_config_hash` is computed from
`_resolve_config`'s narrow `{weights, thresholds, score_band,
rnd_capitalization, gates, universe_version}` subset — `min_history_years`
is read directly from the raw config later in `score()`, never folded
into that subset. Two runs with different `min_history_years` values
would report an IDENTICAL hash while potentially disclosing different
short-history gaps — a real hole in the hash's own stated integrity
purpose ("scores from different assumption sets are identifiable").
Affects disclosure content only, not the numeric composite. **Remedy**:
fold `min_history_years` into `_resolve_config`'s returned dict.

**F-1 — the peer-comparison feature (`engine/peers.py::build_peer_set`)
is wired into the CLI (`analyze.py`) but never the web app.** Both real
web-app render call sites (`app/main.py`) hardcode `peer_table=None`.
`peers.match_sic`/`size_band`/`exclusions`/`universes` are NOT dead
keys (my first-pass grep suggested this, corrected after finding
`analyze.py`) — they're read and used, just by a different entry point
than the dashboard. No documentation found either way on whether this
is an intentional scope boundary. **Remedy: analyst decision** — wire
`peer_table` into the web app's analyze endpoints, or document that
peer comparison is deliberately CLI-only.

### ACCIDENT-NOT-DECISION

**F-10 — `valuation.dcf.scenarios` has no schema validation, unlike
`durability.gates` right next to it.** A duplicate scenario key in
`config.yaml` is silently resolved by PyYAML's own last-key-wins
semantics before the application ever sees it; a missing scenario key
(`bear` absent) makes `expectations_gap_band()` silently return `None`
for every ticker, forever, with zero diagnostic. Contrast:
`durability.gates`'s `_resolve_gates()` raises `ValueError` loudly on
the equivalent malformed input. Confirmed via direct construction (2e)
— genuinely inverted bull/bear VALUES do correctly raise
`AssertionError` (the monotonicity guard works), but structural
config errors (duplicate/missing keys) are invisible. **Remedy**: a
`_resolve_scenarios`-style strict validator mirroring `_resolve_gates`.

**F-12 — the dashboard's actual chip DOM has no read-only regression
path.** Confirmed while probing 3b: `/api/screen`'s composite-cell chip
markup (`.gate-slot`, `.dur-gaps-slot`, `.gap-inherit-slot`,
`.gap-frag-slot`) can only be exercised by adding a ticker to the real
watchlist or editing `config.yaml`'s gate threshold — both blocked as
out-of-scope mutations during this audit, both handled correctly by
falling back to the fragment surface instead (which uses tooltips/
captions, not the same chip vocabulary, so it's a narrower substitute,
not equivalent coverage). A future change to `frontend/app.js`'s
chip-slot markup could regress coexistence with nothing to catch it.
**Remedy**: a headless-DOM or snapshot test harness for
`frontend/app.js`'s row-rendering functions, independent of live
watchlist/server state.

### UNDOCUMENTED-ASSUMPTION

**F-4 / F-3b (one future PR) — PR 3's expectations-gap band has no
`docs/assumptions.md` section at all**, unlike R&D capitalization and
the durability gates (both shipped with a dedicated section). No
anchor exists for: the `[-0.20, +0.60]` reverse-DCF bracket (F-3b — its
VALUE is now affirmed by 1d's 2.6% PARTIAL rate, but it remains
hardcoded in `engine/valuation.py` rather than config-owned, and
undocumented either way); the `_sign(0.0) == 0` design choice that
makes a touch-zero band fire FRAGILE even with no genuine directional
disagreement (confirmed live, 2d) — a decision I recall reasoning
through during PR 3's construction, but never written down anywhere
retrievable in the committed codebase; the `[-0.30, +0.30]` fragment
rendering axis. **Remedy**: write the missing section, covering all
three.

### NEEDS-ANALYST-DECISION (dispositions, not bugs)

**F-5 — the Weights section's AFFIRMED sensitivity claim no longer
holds under the now-committed regime-on config.** Both halves of
Session C's documented claim are empirically false today: the tau
floor (documented ≥0.9429) is now 0.9048 on three specs, and the
"top-5 set is invariant" claim is falsified — under
`capital_discipline −5pp`, META (76.75→78.42) genuinely overtakes
MSFT, COST, and AMAT, entering the top 5 and displacing AMAT (traced
at the composite-arithmetic level, not inferred from a crossing count).
This is a disposition problem, not a scoring bug — `durability.py`'s
arithmetic is not in question, the documented claim is simply stale for
a config it was never measured against. **Analyst decision needed**:
re-disposition the Weights section for the regime-on reality (full
sensitivity table in `phase1_sensitivity_table.md`), and decide whether
META's new sensitivity to `capital_discipline` cuts warrants a
fragility annotation or a weight reconsideration.

### BACKLOG-CONFIRM

No items in this pass required only a "yes, still on the backlog,
nothing new" confirmation distinct from the findings above — every
Session C backlog item this audit touched (curve saturation,
`roic_threshold` cliff proximity) either got fresh regime-on evidence
(folded into F-5/1b) or was out of this session's scope.

---

## Meta-finding — test-strategy principle

**F-7 and F-14 share the same root test-strategy gap.** Every
abstain-and-disclose path in this codebase had a unit test for the
ABSTENTION decision itself (does the value correctly come back
`None`/unadjusted?) but none had an end-to-end test asserting the
DISCLOSURE actually RENDERS in a real report/fragment. The original
`ds.gaps` bug (fixed in PR #52) was this same pattern at a different
layer. **Principle for future work**: every abstain-and-disclose path
needs an end-to-end test that asserts the disclosure text appears in
the actual rendered output (markdown/HTML/dashboard), not only that the
underlying value is correctly withheld or computed.

---

## Dispositions table

Every assumption in `docs/assumptions.md`. Analyst-owned calls (weights,
thresholds, gate parameters) never auto-affirmed by a script — each row
below reflects this session's actual evidence, not a default assumption
that "no news is good news."

| Assumption | Disposition | Evidence pointer |
|---|---|---|
| `assumed_tax_rate` (21%) | AFFIRMED (carried over) | Not re-tested this session — out of scope; no new evidence against it |
| `normalized_fcf_years` (5) | AFFIRMED (carried over) | Not re-tested this session |
| `min_history_years` (4) | NOT-DISPOSITIONED (value); **F-2 open** (hash coverage) | Phase 0e — value itself not stress-tested this session, only its exclusion from the config hash |
| DCF `wacc`/`terminal_growth` (bear/base/bull) | AFFIRMED (carried over from Session C) | Not independently re-litigated; feeds F-3a's bracket test only |
| DCF `projection_years` (5) | AFFIRMED (carried over) | Not re-tested |
| Durability `weights` (5 categories) | **NEEDS-ANALYST-DECISION** (downgraded from AFFIRMED) | Phase 1a / **F-5** — tau floor and top-5-set invariance both empirically broken under regime-on |
| Durability `thresholds` (cost_of_capital, roic_threshold, stability_delta_threshold) | AFFIRMED (re-confirmed regime-on) | Phase 1a — same conclusions as Session C, freshly re-derived |
| Score band imputation (pessimistic/optimistic 25/75) | **AFFIRMED** (upgraded from NOT-DISPOSITIONED) | Phase 1e — now tested on 66 real names; composite point correctly inert, band width scales exactly linearly |
| R&D capitalization `enabled` (true) | AFFIRMED (carried over from PR 2b's own evidence package) | Not re-litigated; downstream effects re-confirmed clean (3a, 3f) |
| R&D capitalization `amortization_years` (5) | AFFIRMED-WITH-FRAGILITY (carried over from PR 2b) | Not re-tested this session |
| Durability gates `balance_sheet_leverage` threshold (6.0) | **AFFIRMED** (freshly re-confirmed) | Phase 1c — 2.8% of the S&P 500 gates at this threshold, sits cleanly in the tail |
| Durability gates cap (45.0) | AFFIRMED (value); **F-13 open** (display bug) | Phase 3b — min() mechanics correct; lineage text stale in the display layer only |
| `classification.overrides` (MARA) | AFFIRMED (carried over) | Out of scope this session |
| `universe.file`/`version` (2026-Q3) | AFFIRMED (operational, not a calibration question) | Phase 0b — confirmed live and read correctly |
| Reverse-DCF bisection bracket (−20%/+60%) | **AFFIRMED** (value, F-3a); **NEEDS-ANALYST-DECISION** (governance, F-3b) | Phase 1d — 2.6% PARTIAL rate across 502 names; still hardcoded in code and undocumented in `docs/assumptions.md` |
| PR 3 expectations-gap band (fragility sign(0) rule, `[-0.30,+0.30]` render axis) | **NOT-DISPOSITIONED** | No `docs/assumptions.md` section exists at all — **F-4** |
| Gate-untestable population (18.7% of S&P 500) | **NEEDS-ANALYST-DECISION** (root cause now **BUG**-confirmed, F-6) | Phase 1c/F-6/scoping — floor 30%, plausibly 50%+ true extraction-miss share |
| `flags.*`/`council.*` (model, pricing, prompt_version) | OUT OF SCOPE this session | This audit's probes targeted the deterministic Tier 1 pipeline, durability, gates, and the expectations-gap band — the Tier 2/3 LLM boundaries were not independently re-audited here |

---

## Probe suite summary

`tests/session_d_probes/test_session_d_findings.py` — 13 quarantined
probes, formalizing the key assertions from Phases 2–3 into real,
re-runnable pytest coverage (in addition to the many ad-hoc scripts run
directly against live/cached data throughout Phases 0–3, whose raw
output is preserved in `audit/session_d/*.txt`/`*.json`).

```
7 passed, 6 xfailed
```

- **7 passed** — confirmed-correct behavior, now real regression
  coverage: gate immunity to the R&D regime toggle (3a), all 5 gate
  branch-order boundary combos (2c, parametrized), and the NO_RND
  control group showing zero R&D artifacts regime-on (3f-style).
- **6 xfailed, `strict=True`** (an unexpected pass would itself fail the
  suite) — one per confirmed finding with a clean, isolatable
  assertion: F-2 (hash coverage), F-7 (trend-window disclosure), F-8
  (revenue truthy-check), F-10 (scenario schema validation), F-13 (gate
  lineage staleness), F-14 (FPI badge). Each xfail's `reason=` cites the
  finding ID — when a post-audit PR fixes one, removing its `xfail`
  marker turns it into a permanent regression test with no further
  rewrite needed.

Findings without a clean, isolated unit-level assertion (F-1 — a
wiring/scope question; F-4/F-3b — a documentation gap; F-5 — a
disposition question; F-6 — a population-level extraction-rate
estimate; F-9 — folded into F-13's probe; F-11/F-12 — required a live
server or scratch-copy file corruption, not naturally expressed as a
quarantined unit test) are evidenced instead by the raw script output
already saved under `audit/session_d/` and referenced from
`phase0_map.md`'s per-phase sections.

**Full suite, this branch, everything together**:
```
770 passed, 2 skipped, 6 xfailed
```
(763 pre-existing + 7 new confirmed-correct probes = 770; 6 xfailed as
designed; 2 skipped are pre-existing and unrelated to this audit.)

---

## Read-only proof

```
$ git diff main -- engine/ app/ frontend/ config.yaml
(empty)

$ git status --short
?? audit/session_d/
?? tests/session_d_probes/
```

Zero changes to `engine/`, `app/`, `frontend/`, `config.yaml`, or any
pre-existing test file, at any point across Phases 0–4. The only new
content on this branch is this audit report (plus its supporting raw-
data files) and the quarantined probe suite — exactly the branch's
stated scope. Verified after every single sub-phase throughout this
session, not just at the end.

Two real-world permission boundaries were hit and respected during
Phase 3 rather than routed around: a temporary `config.yaml` gate-
threshold edit (to force AXON to gate for a live-render check) and a
temporary watchlist addition (`NEE`, as a config-free substitute) were
both blocked by the auto-mode classifier as mutations outside this
audit's approved scope. Both were reverted/confirmed-untouched
immediately, and the corresponding probes were completed via read-only
alternatives instead (documented in Phase 3b).

---

## Status

**Phases 0–4 complete.** 14 findings + 1 meta-finding recorded across
the system's config surface, disclosure surface, invariant boundaries,
and cross-feature seams. Nothing fixed — this entire branch is
evidence-gathering. Every finding above is a candidate for its own
post-audit PR; `report.md`'s findings ledger already carries a
one-line proposed remedy for each, and the dispositions table separates
"needs a code fix" from "needs an analyst decision" from "needs
documentation only."

**STOP. No commit until this report is reviewed.**
