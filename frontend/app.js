// app.js — Security Analysis Dashboard frontend.
// Vanilla JS, no build step, no framework, no CDN. Talks to the FastAPI
// backend in app/main.py over the endpoints documented there.

(function () {
  "use strict";

  // ---- small formatting helpers (mirror engine/screen.py's display rules) ----

  function fmtScore(v) {
    return v === null || v === undefined ? null : v.toFixed(1);
  }

  function fmtPct(v, decimals) {
    if (v === null || v === undefined) return null;
    var s = (v * 100).toFixed(decimals === undefined ? 1 : decimals) + "%";
    return s.replace("-", "−"); // true minus, not ASCII hyphen
  }

  function fmtSignedPct(v, decimals) {
    if (v === null || v === undefined) return null;
    var sign = v >= 0 ? "+" : "−";
    return sign + Math.abs(v * 100).toFixed(decimals === undefined ? 1 : decimals) + "%";
  }

  function fmtAum(v) {
    if (v === null || v === undefined) return null;
    var a = Math.abs(v);
    if (a >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return "$" + (v / 1e6).toFixed(0) + "M";
    return "$" + v.toFixed(0);
  }

  function fmtOverlap(v, count) {
    if (v === null || v === undefined) return null;
    var pct = (v * 100).toFixed(1) + "%";
    if (count !== null && count !== undefined) {
      var noun = count === 1 ? "holding" : "holdings";
      return pct + " (" + count + " " + noun + ")";
    }
    return pct;
  }

  // Never show a raw Python exception string in the UI.
  function humanizeReason(flag) {
    if (!flag) return "—";
    if (flag.indexOf("error:") === 0) return "Data error (see server logs)";
    return flag;
  }

  // <td> builder: shows "n/a" in muted italics when the raw value is absent,
  // never coerces a missing value to 0.
  function td(displayValue, opts) {
    opts = opts || {};
    var cell = document.createElement("td");
    if (opts.cls) cell.className = opts.cls;
    if (displayValue === null || displayValue === undefined) {
      cell.classList.add("na");
      cell.textContent = "n/a";
      return cell;
    }
    cell.textContent = displayValue;
    return cell;
  }

  // Inline score-bar cell (the signature element) for the six 0-100
  // universe-relative percentiles. `rawValue` (the actual number, or
  // null/undefined) decides whether a bar exists AT ALL — absence-is-
  // not-zero: a missing score renders through the exact same "n/a" path
  // as td() above (no track element in the DOM whatsoever), while a
  // genuine score of 0 still renders a real track with a zero-width fill,
  // which is what makes "computed, zero" visually distinct from "never
  // computed." `displayValue` is always fmtScore()'s output, unchanged —
  // this function only ever adds a visual bar alongside a number that was
  // already going to be shown; it never changes what number is shown.
  function scoreCell(rawValue, displayValue, opts) {
    opts = opts || {};
    if (rawValue === null || rawValue === undefined) {
      var naCell = document.createElement("td");
      naCell.className = "na";
      naCell.textContent = "n/a";
      return naCell;
    }
    var cell = document.createElement("td");
    cell.className = "score-cell" + (opts.composite ? " score-composite" : "");
    var num = document.createElement("span");
    num.className = "score-num";
    num.textContent = displayValue;
    cell.appendChild(num);
    var track = document.createElement("div");
    track.className = "score-track";
    var fill = document.createElement("div");
    fill.className = "score-fill";
    // Defensive clamp on the BAR's pixel width only — rawValue is already
    // a 0-100 percentile by contract; this never touches displayValue,
    // i.e. never changes the number shown, only guards the bar from
    // overflowing its track if a value were ever out of range.
    var pct = Math.max(0, Math.min(100, rawValue));

    // Composite (DURABILITY) is banded — below 40 amber (a real concern
    // worth flagging), above 70 teal (comfortably durable) — since it's
    // the one headline, aggregate score, not a peer of the six sub-scores
    // it's built from. Sub-scores stay a single teal hue (CSS default on
    // .score-fill) so the eye ranks by bar LENGTH, not by color — a
    // rainbow per metric would undercut that.
    if (opts.composite) {
      if (pct < 40) fill.classList.add("score-fill-band-low");
      else if (pct > 70) fill.classList.add("score-fill-band-high");
    }

    if (opts.animate) {
      // First-render-only fill animation (the one moment of delight):
      // paint at 0 width, then flip to the real width on the next frame
      // so the CSS `transition:width 400ms ease-out` on .score-fill
      // actually animates instead of jumping straight to its end state.
      // Staggered per row via a small setTimeout, capped so a long
      // watchlist doesn't cascade for multiple seconds.
      fill.style.width = "0%";
      var delay = Math.min(opts.stagger || 0, 15) * 20;
      window.setTimeout(function () {
        fill.style.width = pct + "%";
      }, delay);
    } else {
      fill.style.width = pct + "%";
    }

    track.appendChild(fill);
    cell.appendChild(track);
    return cell;
  }

  // Durability-gaps presence indicator (DUR): a small outline chip
  // prepended to the Durability (composite) score cell's number when
  // DurabilityScore.gaps is non-empty -- net-cash resilience, mixed-basis,
  // short-history, split-contamination disclosures that fire during
  // scoring but, before this change, never reached any user-facing
  // surface. Reserved slot on EVERY row (present in the DOM but
  // visibility:hidden when there's nothing to disclose), same pattern as
  // the Gap column's INH chip -- keeps the score-num's layout invariant
  // across rows and keeps a gapless row's slot out of the accessibility
  // tree, rather than announcing a phantom DUR. Full gap text lives in the
  // tooltip and the per-ticker fragment -- never inline in the table.
  function appendDurGapsIndicator(compositeCell, durabilityGaps) {
    var numEl = compositeCell.querySelector(".score-num");
    if (!numEl) return;
    var slot = document.createElement("span");
    slot.className = "dur-gaps-slot";
    slot.textContent = "DUR";
    if (durabilityGaps && durabilityGaps.length) {
      slot.classList.add("dur-gaps-present", "has-tooltip");
      slot.tabIndex = 0;
      var tip = document.createElement("div");
      tip.className = "th-tooltip";
      tip.textContent = "Durability-scoring disclosures: " + durabilityGaps.join(" | ");
      slot.appendChild(tip);
    }
    numEl.insertBefore(slot, numEl.firstChild);
  }

  // Directional Gap pill. Sign (never inferred by CSS — computed here,
  // same as before) picks the fixed hue class; magnitude only scales the
  // --gap-alpha custom property within that hue, preserving the same
  // 0.06-0.16 alpha range the previous inline-style version used. A null
  // gap renders a distinct neutral pill (not the plain italic "n/a" used
  // elsewhere) — the brief's own call for this column specifically.
  function gapCell(rawValue, displayValue) {
    var cell = document.createElement("td");
    cell.className = "gap-cell";
    // Inherit-chip slot: reserved on EVERY gap cell (before the pill), with
    // its "INH" text already in place but invisible (visibility:hidden --
    // hidden from sighted users AND assistive tech, unlike opacity/color
    // tricks) when there's no upstream caveat. Keeping the same text content
    // in the DOM at all times (rather than inserting it only when needed)
    // is what keeps every pill's right edge aligned to the same column
    // regardless of whether this row ends up with a visible chip — see
    // appendInheritChip below, which only toggles visibility/styling on
    // this same node instead of appending a separate one after the pill.
    var slot = document.createElement("span");
    slot.className = "gap-inherit-slot";
    slot.textContent = "INH";
    cell.appendChild(slot);
    // Fragility slot (PR 3, expectations-gap scenario band): a second
    // reserved slot, same visibility:hidden pattern as .gap-inherit-slot
    // immediately above -- both sit to the LEFT of the pill, so a FRAG
    // chip never collides with the INH chip (they're just two chips in
    // the same pre-pill run) nor with .row-remove-wrap (absolutely
    // positioned at the cell's own right edge, over the pill itself).
    // Default hidden text is "FRAG" (the common COMPLETE/FRAGILE case);
    // appendFragChip() below swaps to "FRAG?" only for the rarer
    // UNDETERMINABLE (PARTIAL band) case.
    var fragSlot = document.createElement("span");
    fragSlot.className = "gap-frag-slot";
    fragSlot.textContent = "FRAG";
    cell.appendChild(fragSlot);
    var pill = document.createElement("span");
    if (rawValue === null || rawValue === undefined) {
      pill.className = "gap-pill gap-pill-na";
      pill.textContent = "n/a";
    } else {
      pill.className = "gap-pill " + (rawValue > 0 ? "gap-pill-pos" : "gap-pill-neg");
      pill.textContent = displayValue;
      var magnitude = Math.min(Math.abs(rawValue), 0.30);
      var alpha = 0.06 + (magnitude / 0.30) * 0.10;
      pill.style.setProperty("--gap-alpha", alpha.toFixed(3));
    }
    cell.appendChild(pill);
    return cell;
  }

  // Session B basis-disclosure badge: a small superscript marker + hover
  // tooltip attached to an existing cell, reusing the generic .has-tooltip/
  // .th-tooltip pattern (already shared by column headers and the analyze
  // fragment's stat cards) rather than inventing a new tooltip mechanism.
  // Purely additive — never changes the cell's existing displayValue/pill,
  // only appends a marker beside it when the caller's condition is true.
  function appendBasisBadge(cell, text, tooltip) {
    var badge = document.createElement("span");
    badge.className = "basis-badge has-tooltip";
    badge.tabIndex = 0;
    badge.textContent = text;
    var tip = document.createElement("div");
    tip.className = "th-tooltip";
    tip.textContent = tooltip;
    badge.appendChild(tip);
    cell.appendChild(badge);
  }

  // Gap-inheritance marker: an "INH" text chip -- same .basis-badge visual
  // vocabulary as the MKT/WIN/REV origin badges, but outline-only (no
  // filled background) so it reads one step quieter, matching that
  // inheritance is a lesser signal than a direct caveat. Reuses
  // .has-tooltip/.th-tooltip like appendBasisBadge above. Toggles
  // visibility/styling on the slot gapCell() already reserved (with the
  // same "INH" text already in place, just hidden) to the LEFT of the
  // pill -- rather than appending a new node after it -- so the chip never
  // collides with the hover-remove × that's absolutely positioned at the
  // cell's own right edge, and so uninherited rows' hidden slot still
  // occupies the identical width.
  function appendInheritChip(cell, reasons) {
    var slot = cell.querySelector(".gap-inherit-slot");
    slot.classList.add("inherit-chip", "has-tooltip");
    slot.tabIndex = 0;
    var tip = document.createElement("div");
    tip.className = "th-tooltip";
    tip.textContent = "Inherits: " + reasons.join(", ");
    slot.appendChild(tip);
  }

  // Expectations-gap fragility chip (PR 3): fires on the reserved
  // .gap-frag-slot gapCell() always creates (see above). "FRAGILE" means a
  // COMPLETE band (all three scenarios converged) whose gap sign flips
  // across bull/base/bear -- the base-case pill's direction isn't robust to
  // the WACC/terminal-growth assumption. "UNDETERMINABLE" means a PARTIAL
  // band (a scenario's bisection missed the bracket) -- fragility can't be
  // assessed at all, a distinct state from "not fragile," never collapsed
  // into it. scenarioGaps is the row's expectations_gap_scenarios list
  // ([{scenario, gap, converged}, ...]) formatted into one tooltip line.
  function appendFragChip(cell, variant, scenarioGaps) {
    var slot = cell.querySelector(".gap-frag-slot");
    if (!slot || !variant) return;
    var parts = (scenarioGaps || []).map(function (s) {
      return s.scenario + ": " + (s.converged ? fmtSignedPct(s.gap) : "bracket not converged");
    });
    if (variant === "FRAGILE") {
      slot.classList.add("frag-present", "has-tooltip");
    } else if (variant === "UNDETERMINABLE") {
      slot.textContent = "FRAG?";
      slot.classList.add("frag-undeterminable", "has-tooltip");
    } else {
      return;
    }
    slot.tabIndex = 0;
    var tip = document.createElement("div");
    tip.className = "th-tooltip";
    tip.textContent = "Scenario gaps — " + parts.join(" | ");
    slot.appendChild(tip);
  }

  // ---- API helpers ----

  function apiGet(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
      return r.json();
    });
  }

  // ---- watchlist rendering ----

  var els = {
    searchInput: document.getElementById("search-input"),
    searchStatus: document.getElementById("search-status"),
    searchConfirm: document.getElementById("search-confirm"),
    searchConfirmText: document.getElementById("search-confirm-text"),
    searchClassificationBadge: document.getElementById("search-classification-badge"),
    searchAddBtn: document.getElementById("search-add-btn"),
    refreshBtn: document.getElementById("refresh-btn"),
    exportCsvBtn: document.getElementById("export-csv-btn"),
    exportPdfBtn: document.getElementById("export-pdf-btn"),
    statusBanner: document.getElementById("status-banner"),
    emptyState: document.getElementById("empty-state"),
    equitiesSection: document.getElementById("equities-section"),
    etfSection: document.getElementById("etf-section"),
    excludedSection: document.getElementById("excluded-section"),
    excludedScroll: document.getElementById("excluded-scroll"),
    excludedEmpty: document.getElementById("excluded-empty"),
    diagnosticsSection: document.getElementById("diagnostics-section"),
    equitiesBody: document.querySelector("#equities-table tbody"),
    etfBody: document.querySelector("#etf-table tbody"),
    excludedBody: document.querySelector("#excluded-table tbody"),
    diagnosticsBody: document.querySelector("#diagnostics-table tbody"),
    diagnosticsStamp: document.getElementById("diagnostics-stamp"),
    diagnosticsClean: document.getElementById("diagnostics-clean"),
    diagnosticsTableWrap: document.getElementById("diagnostics-table-wrap"),
    metaLine: document.getElementById("meta-line"),
    usageBtn: document.getElementById("usage-btn"),
    usageModalOverlay: document.getElementById("usage-modal-overlay"),
    usageModalBody: document.getElementById("usage-modal-body"),
    usageModalClose: document.getElementById("usage-modal-close"),
  };

  var currentSearchTicker = null; // ticker the search confirm card currently refers to

  function showBanner(text, isError) {
    els.statusBanner.textContent = text;
    els.statusBanner.classList.toggle("error", !!isError);
    els.statusBanner.classList.remove("hidden");
  }

  function hideBanner() {
    els.statusBanner.classList.add("hidden");
  }

  // expandable=true adds the hover chevron + tooltip that hints the row
  // opens an accordion; excluded rows pass false since there's nothing to
  // expand (durability scoring never ran for them).
  function tickerCell(ticker, expandable) {
    var cell = document.createElement("td");
    cell.className = "tk";
    cell.textContent = ticker;
    if (expandable) {
      var hint = document.createElement("span");
      hint.className = "expand-hint";
      hint.textContent = "▸";
      cell.appendChild(hint);
      cell.title = "Click to view analysis for " + ticker;
    }
    return cell;
  }

  // ---- inline report accordion (equity/ETF rows only) ----
  //
  // fragmentCache: ticker -> already-fetched fragment HTML, so re-expanding
  //   a row the user previously opened is instant, no re-fetch.
  // fragmentInFlight: ticker -> in-flight fetch Promise, so a double-click
  //   (or clicking the same row again before the first fetch resolves)
  //   reuses the one request instead of firing a second.
  // expandedRows: ticker -> the currently-inserted accordion <tr>, so a
  //   second click on the same row collapses it, and a late-arriving
  //   fetch response doesn't overwrite a row the user already collapsed.

  var fragmentCache = {};
  var fragmentInFlight = {};
  var expandedRows = {};

  // ---- column sorting (equities + ETF tables) ----
  //
  // equitiesData/etfData hold the rows exactly as last returned by
  // /api/screen — this is "default order" for the 3-state cycle below.
  // Sorting never re-fetches or mutates that array; it only changes what
  // order renderEquitiesBody()/renderEtfBody() rebuild the tbody in.
  var equitiesData = [];
  var etfData = [];
  var sortState = {
    equities: { key: null, dir: null },
    etf: { key: null, dir: null },
  };

  var EQUITIES_SORT_LABELS = {
    ticker: "Ticker", composite: "Durability", cat_reinvestment: "Reinv",
    cat_quality: "Quality", cat_resilience: "Resilience", cat_discipline: "Discipline",
    cat_optionality: "Optionality", implied_fcf_growth: "Implied g",
    delivered_fcf_growth: "Delivered g", expectations_gap: "Gap",
  };
  var ETF_SORT_LABELS = {
    ticker: "Ticker", expense_ratio: "Exp Ratio", aum: "AUM",
    overlap_with_screen: "Overlap w/ Singles",
  };

  // Implied g / Gap are gated (implied_growth_note set) the same way the
  // rendered cell is — sorting must use the same "effective" value that's
  // actually on screen, not a raw value the display suppressed to n/a.
  function equitiesSortValue(row, key) {
    if (key === "implied_fcf_growth" || key === "expectations_gap") {
      return row.implied_growth_note ? null : row[key];
    }
    return row[key];
  }

  function etfSortValue(row, key) {
    return row[key];
  }

  // Stable sort by `key`/`dir` ("asc"|"desc") using valueOf(row,key) to pull
  // the comparable value. n/a (null/undefined) ALWAYS sorts to the bottom,
  // in both directions — absence-is-not-zero applies to ordering too, so a
  // missing value is never coerced to 0 or -Infinity.
  function sortRows(rows, key, dir, valueOf) {
    if (!key || !dir) return rows.slice();
    var withIndex = rows.map(function (r, i) { return { r: r, i: i }; });
    withIndex.sort(function (a, b) {
      var va = valueOf(a.r, key), vb = valueOf(b.r, key);
      var aNa = va === null || va === undefined;
      var bNa = vb === null || vb === undefined;
      if (aNa && bNa) return a.i - b.i;
      if (aNa) return 1;
      if (bNa) return -1;
      var cmp = (typeof va === "string" || typeof vb === "string")
        ? String(va).localeCompare(String(vb))
        : (va < vb ? -1 : va > vb ? 1 : 0);
      if (cmp === 0) return a.i - b.i;
      return dir === "asc" ? cmp : -cmp;
    });
    return withIndex.map(function (x) { return x.r; });
  }

  function updateSortArrows(tableId, state) {
    document.querySelectorAll("#" + tableId + " thead th[data-sort-key]").forEach(function (th) {
      var isActive = !!(state.key && th.getAttribute("data-sort-key") === state.key);
      // Visual "this column is driving the current order" signal — a
      // class toggle only, no effect on sortRows()/sortState themselves.
      th.classList.toggle("sorted", isActive);
      var arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      arrow.textContent = isActive ? (state.dir === "asc" ? " ▲" : " ▼") : "";
    });
  }

  function updateSortCaption(captionId, state, labels) {
    var el = document.getElementById(captionId);
    if (!el) return;
    el.textContent = state.key ? "Sorted by: " + labels[state.key] + " (" + state.dir + ")" : "";
  }

  // Any accordion currently open for one of these tickers is tied to a
  // <tr> DOM node this render is about to discard — drop the stale
  // reference so a later click creates a fresh accordion instead of
  // thinking one is already open and immediately closing (no-op-looking)
  // on a detached node.
  function clearExpandedFor(rows) {
    rows.forEach(function (r) { delete expandedRows[r.ticker]; });
  }

  // Score bars animate their fill-in ONCE, on the very first render — a
  // sort click rebuilds the whole tbody (same as any other re-render) but
  // must never replay the stagger animation, or every sort would look
  // like a fresh page load. Set true at the end of the first
  // renderEquitiesBody() call; scoreCell() reads it (via `animate` below)
  // BEFORE that row's own call, since forEach hasn't finished yet.
  var scoreBarsAnimated = false;

  function renderEquitiesBody() {
    var rows = sortRows(equitiesData, sortState.equities.key, sortState.equities.dir, equitiesSortValue);
    clearExpandedFor(equitiesData);
    els.equitiesBody.innerHTML = "";
    var animate = !scoreBarsAnimated;
    rows.forEach(function (r, i) { els.equitiesBody.appendChild(renderEquitiesRow(r, i, animate)); });
    scoreBarsAnimated = true;
    updateSortArrows("equities-table", sortState.equities);
    updateSortCaption("equities-sort-caption", sortState.equities, EQUITIES_SORT_LABELS);
  }

  function renderEtfBody() {
    var rows = sortRows(etfData, sortState.etf.key, sortState.etf.dir, etfSortValue);
    clearExpandedFor(etfData);
    els.etfBody.innerHTML = "";
    rows.forEach(function (r) { els.etfBody.appendChild(renderEtfRow(r)); });
    updateSortArrows("etf-table", sortState.etf);
    updateSortCaption("etf-sort-caption", sortState.etf, ETF_SORT_LABELS);
  }

  // Click cycle per column: unsorted/other-column -> desc -> asc -> back
  // to default (key cleared). Clicking a different column always starts
  // that column fresh at desc.
  function attachSortHandler(tableId, stateKey, renderFn) {
    var thead = document.querySelector("#" + tableId + " thead");
    thead.addEventListener("click", function (ev) {
      var th = ev.target.closest("th[data-sort-key]");
      if (!th) return;
      var key = th.getAttribute("data-sort-key");
      var st = sortState[stateKey];
      if (st.key !== key) {
        st.key = key;
        st.dir = "desc";
      } else if (st.dir === "desc") {
        st.dir = "asc";
      } else {
        st.key = null;
        st.dir = null;
      }
      renderFn();
    });
  }

  attachSortHandler("equities-table", "equities", renderEquitiesBody);
  attachSortHandler("etf-table", "etf", renderEtfBody);

  // Height-animates .accordion-content open: paint at max-height:0 (already
  // set by the caller before insertion), then flip to the real scrollHeight
  // on the next frame so the CSS `transition:max-height 200ms ease-out`
  // actually animates instead of jumping straight to "auto". ≤200ms, the
  // one other animated moment besides the score-bar fill-in.
  //
  // Bug (reported): rows below an open panel render on top of it instead
  // of reflowing down. Root cause: this function set max-height to a
  // PIXEL VALUE measured once at open time and never released it — every
  // <tr> below depends on the table layout algorithm's computed height for
  // this row, which is governed by that pinned pixel cap, not by the
  // panel's actual current content. Any change after the open transition
  // (the real fragment replacing the "Loading…" placeholder, a nested
  // <details> toggle, the Sources reveal) then sizes against a STALE cap
  // instead of the row's true height, leaving the table's own row-height
  // bookkeeping out of sync with what's rendered — which is what let
  // subsequent rows overlap instead of reflowing. Fix: once the open
  // transition finishes, release max-height to "none" (unconstrained) so
  // the browser's ordinary auto-height table layout governs this row from
  // then on, exactly like every other row — no pixel value to go stale.
  // The transitionend listener is tracked on the element itself so
  // collapseAccordionRow (below) can cancel a still-pending one before it
  // fires mid-collapse.
  function expandAccordionContent(inner) {
    if (inner._openHandler) {
      inner.removeEventListener("transitionend", inner._openHandler);
      inner._openHandler = null;
    }
    requestAnimationFrame(function () {
      // settleAccordionContent (below) may already have taken over this
      // exact element between this rAF being queued and it actually
      // running (the first-open race) — if so, this callback is stale and
      // must be a no-op, or it would reintroduce a pixel max-height cap
      // right after settle deliberately released it to "none".
      if (inner._settled) return;
      inner.style.maxHeight = inner.scrollHeight + "px";
      inner.style.opacity = "1";
      var onOpenEnd = function (ev) {
        if (ev.propertyName !== "max-height") return;
        inner.removeEventListener("transitionend", onOpenEnd);
        inner._openHandler = null;
        inner.style.maxHeight = "none";
      };
      inner._openHandler = onOpenEnd;
      inner.addEventListener("transitionend", onOpenEnd);
    });
  }

  // Used specifically when content that's already inserted (the "Loading…"
  // placeholder) gets REPLACED by different content — fetch completion
  // (real fragment) or a failed-fetch error message. This is exactly the
  // first-open race reported: expandAccordionContent()'s animate-then-
  // release-on-transitionend approach depends on a NEW transitionend
  // firing for THIS specific style change, but if the fetch resolves fast
  // enough, its completion can land in the same animation frame as the
  // placeholder's own still-pending rAF — both call expandAccordionContent
  // in sequence, one cancelling/overwriting the other's bookkeeping, and
  // in some interleavings the transitionend that's supposed to release the
  // real (larger) content's height either never fires cleanly or fires
  // for the wrong measurement. The signature was exactly "only the FIRST
  // open of a cold cache" — every later open (fragmentCache hit) only ever
  // calls expandAccordionContent ONCE, so there's no second call to race
  // against. Fix: when swapping in replacement content, release the
  // height constraint to "none" immediately and unconditionally — no rAF,
  // no transition, no dependency on event ordering at all. This one swap
  // loses its grow animation (an instant reveal instead of a 200ms one) in
  // exchange for a row height that is CORRECT from the instant the real
  // content exists, on every open including the very first.
  function settleAccordionContent(inner) {
    if (inner._openHandler) {
      inner.removeEventListener("transitionend", inner._openHandler);
      inner._openHandler = null;
    }
    inner._settled = true; // makes a still-queued expandAccordionContent rAF (if any) a no-op
    inner.style.maxHeight = "none";
    inner.style.opacity = "1";
  }

  // Reverses the same transition on collapse, then removes the row once
  // it's actually finished (transitionend) rather than mid-animation — a
  // short setTimeout fallback guarantees the row is removed even if the
  // event never fires (e.g. content with no measurable transition).
  function collapseAccordionRow(accRow) {
    var inner = accRow.querySelector(".accordion-content");
    if (!inner) {
      accRow.remove();
      return;
    }
    // Cancel a pending "release to none" from expandAccordionContent above
    // — if the user collapses before that ever fires, it must not clobber
    // the collapse transition we're about to start.
    if (inner._openHandler) {
      inner.removeEventListener("transitionend", inner._openHandler);
      inner._openHandler = null;
    }
    var removed = false;
    function finish() {
      if (removed) return;
      removed = true;
      accRow.remove();
    }
    inner.style.maxHeight = inner.scrollHeight + "px"; // lock in the current height first
    requestAnimationFrame(function () {
      inner.style.maxHeight = "0px";
      inner.style.opacity = "0";
    });
    inner.addEventListener("transitionend", function onEnd(ev) {
      if (ev.propertyName !== "max-height") return;
      inner.removeEventListener("transitionend", onEnd);
      finish();
    });
    window.setTimeout(finish, 250);
  }

  function toggleAccordion(ticker, row) {
    var existing = expandedRows[ticker];
    if (existing) {
      collapseAccordionRow(existing);
      delete expandedRows[ticker];
      row.classList.remove("row-expanded");
      return;
    }

    row.classList.add("row-expanded");
    var accRow = document.createElement("tr");
    accRow.className = "accordion-row";
    var cell = document.createElement("td");
    cell.colSpan = row.cells.length;
    var inner = document.createElement("div");
    inner.className = "accordion-content";
    inner.style.maxHeight = "0px";
    inner.style.opacity = "0";
    cell.appendChild(inner);
    accRow.appendChild(cell);
    row.parentNode.insertBefore(accRow, row.nextSibling);
    expandedRows[ticker] = accRow;

    if (fragmentCache[ticker]) {
      inner.innerHTML = fragmentCache[ticker];
      expandAccordionContent(inner);
      return;
    }

    inner.innerHTML = '<div class="accordion-loading">Loading analysis for ' + ticker + '…</div>';
    expandAccordionContent(inner);

    var promise = fragmentInFlight[ticker];
    if (!promise) {
      promise = fetch("/api/analyze/" + encodeURIComponent(ticker) + "/fragment")
        .then(function (r) { return r.text(); })
        .finally(function () { delete fragmentInFlight[ticker]; });
      fragmentInFlight[ticker] = promise;
    }
    promise
      .then(function (html) {
        fragmentCache[ticker] = html;
        // Only touch the DOM if this row is still the one currently expanded
        // for this ticker — the user may have collapsed it while we waited.
        if (expandedRows[ticker] === accRow) {
          inner.innerHTML = html;
          settleAccordionContent(inner);
        }
      })
      .catch(function (e) {
        if (expandedRows[ticker] === accRow) {
          inner.innerHTML = '<div class="accordion-loading">Failed to load analysis: ' + e.message + "</div>";
          settleAccordionContent(inner);
        }
      });
  }

  // "Sources" toggle inside an expanded fragment (engine/report_html.py's
  // render_fragment): a single delegated listener, since fragment HTML is
  // injected via innerHTML after this script has already run. Toggling a
  // class on the enclosing .report-section is the whole mechanism — no JS
  // state to track, since each expanded ticker's fragment is its own DOM
  // subtree (state is naturally per-ticker, never global), and it resets
  // to "off" whenever the fragment is re-rendered from fragmentCache.
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".sources-toggle");
    if (!btn) return;
    var section = btn.closest(".report-section");
    if (section) section.classList.toggle("sources-on");
    btn.classList.toggle("active");
  });

  // A mouse click on a tabindex="0" element assigns it focus by default,
  // which used to pin its tooltip open (:focus) until the user clicked
  // elsewhere. The CSS now keys tooltip visibility off :focus-visible
  // instead, but this preventDefault() is belt-and-suspenders: it stops
  // the browser from assigning focus on mousedown at all, so no
  // :focus-visible heuristic quirk can reintroduce the stuck-open bug.
  // preventDefault() here does not cancel the subsequent click event, so
  // header click-to-sort still fires normally.
  document.addEventListener("mousedown", function (ev) {
    if (ev.target.closest(".has-tooltip")) ev.preventDefault();
  });

  // ---- Flags (Tier 2) — spend-gated. Expanding the "Flags" <details>
  // (engine/report_html.py's _flags_section()) always does a FREE check
  // against /api/flags/{ticker}: that endpoint itself never makes a paid
  // model call unless the request carries ?extract=true or ?refresh=true
  // (app/main.py's get_flags()), so this auto-check can never trigger
  // spend on its own. Three renderable states come back:
  //   state: "not_cached" -> placeholder card + estimated cost + an
  //     "Extract flags" button, which is the ONLY thing that ever fires
  //     ?extract=true.
  //   state: "ok"         -> flags rendered normally, plus a small
  //     "Re-extract" link that fires ?refresh=true after a confirm().
  //   non-2xx (no API key, credits exhausted, etc.) -> the server's own
  //     error detail, verbatim — never a fabricated fallback message.

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  var _SEVERITY_ORDER = { red: 0, yellow: 1, green: 2 };

  function fmtEstimatedCost(v) {
    return v === null || v === undefined ? "unknown (no pricing configured for this model)" : "~$" + v.toFixed(2);
  }

  function renderNotCachedFlags(body, data) {
    body.innerHTML =
      '<div class="flags-placeholder">'
      + '<p class="report-caption">Qualitative flags not yet extracted for this ticker.</p>'
      + '<p class="flags-estimate">Estimated cost: ' + escapeHtml(fmtEstimatedCost(data.estimated_cost_usd)) + "</p>"
      + '<button type="button" class="btn btn-primary flags-extract-btn">Extract flags</button>'
      + "</div>";
  }

  function renderOkFlags(body, data) {
    var flags = data.flags || [];
    var inner;
    if (flags.length === 0) {
      inner = '<p class="report-caption">No flags extracted for this filing.</p>';
    } else {
      // Red first, then yellow, then green — anything with an unrecognized
      // severity sorts last rather than being dropped.
      var sorted = flags.slice().sort(function (a, b) {
        var oa = _SEVERITY_ORDER.hasOwnProperty(a.severity) ? _SEVERITY_ORDER[a.severity] : 3;
        var ob = _SEVERITY_ORDER.hasOwnProperty(b.severity) ? _SEVERITY_ORDER[b.severity] : 3;
        return oa - ob;
      });

      var html = '<ul class="flags-list">';
      sorted.forEach(function (f) {
        var analystTag = f.source === "analyst" ? '<span class="flag-analyst-tag">Analyst</span>' : "";
        html += '<li class="flag-item">'
          + '<span class="flag-dot flag-dot-' + escapeHtml(f.severity) + '"></span>'
          + '<span class="flag-label">' + escapeHtml(f.label) + "</span>"
          + analystTag
          + '<span class="flag-item-cite">Item ' + escapeHtml(f.item) + "</span>"
          + '<blockquote class="flag-snippet">“' + escapeHtml(f.snippet) + "”</blockquote>"
          + "</li>";
      });
      html += "</ul>";

      var filing = data.filing || {};
      var filingLink = filing.url
        ? '<a href="' + escapeHtml(filing.url) + '" target="_blank" rel="noopener">' + escapeHtml(filing.accession || filing.url) + "</a>"
        : escapeHtml(filing.accession || "n/a");
      html += '<p class="flags-provenance">'
        + "Model: " + escapeHtml(data.model) + " &middot; Prompt: " + escapeHtml(data.prompt_version)
        + " &middot; Extracted: " + escapeHtml(data.extracted_at)
        + " &middot; Filing: " + filingLink
        + "</p>";
      inner = html;
    }

    var pricingNote = data.pricing_unknown
      ? '<p class="flags-pricing-unknown">Pricing not configured for this model — cost not recorded for this call.</p>'
      : "";

    body.innerHTML =
      '<div class="flags-toolbar"><button type="button" class="flags-reextract-btn">Re-extract</button></div>'
      + pricingNote
      + inner;
  }

  function renderFlagsBody(body, data) {
    if (data.state === "not_cached") {
      renderNotCachedFlags(body, data);
    } else {
      renderOkFlags(body, data);
    }
  }

  // A failed extraction's `detail` is either a plain string (404/503 —
  // "No 10-K filing found", "Anthropic API key not configured") or the
  // richer structured envelope app/main.py's _describe_exception() builds
  // for an actual SDK/extraction failure: {state, error_type, message,
  // sdk_status_code, sdk_body}. Extract the human-readable message from
  // either shape — never render "[object Object]".
  function extractErrorMessage(detail, fallback) {
    if (!detail) return fallback;
    if (typeof detail === "string") return detail;
    if (typeof detail === "object" && detail.message) return detail.message;
    return fallback;
  }

  function loadFlags(details, query) {
    var ticker = details.dataset.ticker;
    var body = details.querySelector(".flags-body");
    fetch("/api/flags/" + encodeURIComponent(ticker) + (query ? "?" + query : ""))
      .then(function (r) {
        if (!r.ok) {
          // .catch() here only covers a body that fails to parse as JSON
          // (falls back to {}) — it must NOT also swallow the throw in
          // the .then() below, or a real "detail" message from the server
          // gets replaced by the generic "HTTP <status>" every time.
          return r.json().catch(function () { return {}; }).then(function (b) {
            throw new Error(extractErrorMessage(b.detail, "HTTP " + r.status));
          });
        }
        return r.json();
      })
      .then(function (data) { renderFlagsBody(body, data); })
      .catch(function (e) {
        body.innerHTML = '<p class="report-caption">Flags unavailable: ' + escapeHtml(e.message) + "</p>";
      });
  }

  // The native "toggle" event on <details> does not bubble, so a normal
  // document-level delegated listener (bubbling phase) never sees it —
  // this uses the CAPTURING phase instead, which travels from document
  // down to the target regardless of the event's own bubbling flag, so
  // one listener still covers every .flags-section injected later via
  // innerHTML (same reasoning as the .sources-toggle click delegation
  // above, adapted for a non-bubbling event type). This fires the FREE
  // cache-status check described above — never the paid extraction.
  document.addEventListener("toggle", function (ev) {
    var details = ev.target;
    if (!details.classList || !details.classList.contains("flags-section")) return;
    if (!details.open || details.dataset.loaded === "true") return;
    details.dataset.loaded = "true";
    loadFlags(details);
  }, true);

  // "Extract flags" — the ONLY control that can ever cause a paid call on
  // a never-extracted ticker. Delegated (innerHTML-injected buttons).
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".flags-extract-btn");
    if (!btn) return;
    var details = btn.closest(".flags-section");
    if (!details) return;
    var body = details.querySelector(".flags-body");
    btn.disabled = true;
    btn.textContent = "Extracting…";
    loadFlags(details, "extract=true");
  });

  // "Re-extract" on already-cached flags — a repeat spend, so it's gated
  // behind a confirm() (same "no silent paid action" bar as the button
  // above, plus a guard against an accidental re-click on data you
  // already have for free).
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".flags-reextract-btn");
    if (!btn) return;
    var details = btn.closest(".flags-section");
    if (!details) return;
    var ticker = details.dataset.ticker;
    if (!window.confirm("Re-extract flags for " + ticker + "? This makes a new, billed model call.")) return;
    btn.disabled = true;
    btn.textContent = "Extracting…";
    loadFlags(details, "refresh=true");
  });

  // ---- Council (Tier 3) — spend-gated, reuses the exact same pattern as
  // Flags above: expanding the "Council" <details>
  // (engine/report_html.py's _council_section()) always does a FREE check
  // against GET /api/council/{ticker} — that endpoint never makes a paid
  // model call on its own. Three renderable states come back:
  //   state: "blocked_no_flags" -> Tier 2 flags aren't extracted yet for
  //     this filing. The readiness checklist still renders (quant may
  //     still be available), and Consult Council still works — the
  //     inline confirm below is what tells the analyst the run will be
  //     "test mode" (quant only, no qualitative flags).
  //   state: "not_cached"       -> readiness checklist + a call/cost
  //     estimate + Consult Council, which is the ONLY thing that ever
  //     fires ?convene=true.
  //   state: "ok"               -> the cached council record (or one just
  //     convened), plus a Re-consult control that fires
  //     ?convene=true&refresh=true after an inline confirm.
  // Unlike Flags' re-extract (a window.confirm()), the spend confirmation
  // here is inline — matching the remove-row pattern (no viewport-bottom
  // bars) — since a council convene is a heavier, more consequential
  // action worth a proper explanatory chip, not a one-line browser dialog.

  function renderCouncilReadiness(data) {
    var status = data.evidence_status || {};
    var quantOk = status.quant === "ok";
    var flagsOk = status.flags === "cached";
    var rows = [
      { ok: quantOk, label: "Quantitative analysis", note: quantOk ? null : "not available" },
      { ok: flagsOk, label: "Qualitative flags", note: flagsOk ? null : "not extracted" },
      { ok: false, label: "Thesis", note: "no thesis on file (not yet built)" },
    ];
    var html = '<ul class="council-readiness">';
    rows.forEach(function (r) {
      html += '<li class="council-readiness-item ' + (r.ok ? "ok" : "missing") + '">'
        + '<span class="council-check">' + (r.ok ? "✓" : "✗") + "</span>"
        + '<span class="council-readiness-label">' + escapeHtml(r.label) + "</span>"
        + (r.note ? '<span class="council-readiness-note">' + escapeHtml(r.note) + "</span>" : "")
        + "</li>";
    });
    html += "</ul>";
    return { html: html, flagsOk: flagsOk };
  }

  var closeOpenCouncilConfirm = null; // currently-open inline confirm's own reset fn, or null

  document.addEventListener("click", function (ev) {
    if (closeOpenCouncilConfirm && !ev.target.closest(".council-consult-wrap")) {
      closeOpenCouncilConfirm();
    }
  });

  // Builds the trigger button + inline confirm chip. flagsAvailable controls
  // the confirm copy ("test mode" caveat when Tier 2 flags aren't cached);
  // isReconsult controls the button label and whether ?refresh=true is
  // appended to the eventual POST.
  function consultControl(ticker, details, flagsAvailable, isReconsult) {
    var wrap = document.createElement("div");
    wrap.className = "council-consult-wrap";

    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = isReconsult ? "btn council-reconsult-btn" : "btn btn-primary council-consult-btn";
    trigger.textContent = isReconsult ? "Re-consult council" : "Consult council";
    wrap.appendChild(trigger);

    function showTrigger() {
      if (closeOpenCouncilConfirm === showTrigger) closeOpenCouncilConfirm = null;
      wrap.innerHTML = "";
      wrap.appendChild(trigger);
    }

    function showConfirm() {
      if (closeOpenCouncilConfirm) closeOpenCouncilConfirm();
      wrap.innerHTML = "";

      var confirmWrap = document.createElement("div");
      confirmWrap.className = "council-confirm";

      var label = document.createElement("p");
      label.className = "council-confirm-label";
      label.textContent = flagsAvailable
        ? "This runs a multi-call, paid council convene for " + ticker + " using the quantitative analysis and cached qualitative flags."
        : "Qualitative flags are not extracted for " + ticker + " yet — the council will run in test mode, using quantitative analysis only. Still a multi-call, paid operation.";

      var actions = document.createElement("div");
      actions.className = "council-confirm-actions";

      var yes = document.createElement("button");
      yes.type = "button";
      yes.className = "council-confirm-yes";
      yes.textContent = "Confirm & consult";

      var no = document.createElement("button");
      no.type = "button";
      no.className = "council-confirm-no";
      no.textContent = "Cancel";

      actions.appendChild(yes);
      actions.appendChild(no);
      confirmWrap.appendChild(label);
      confirmWrap.appendChild(actions);
      wrap.appendChild(confirmWrap);

      yes.addEventListener("click", function (ev) {
        ev.stopPropagation();
        yes.disabled = true;
        no.disabled = true;
        yes.textContent = "Consulting…";
        loadCouncil(details, isReconsult ? "convene=true&refresh=true" : "convene=true");
      });
      no.addEventListener("click", function (ev) {
        ev.stopPropagation();
        showTrigger();
      });

      closeOpenCouncilConfirm = showTrigger;
    }

    trigger.addEventListener("click", function (ev) {
      ev.stopPropagation();
      showConfirm();
    });

    return wrap;
  }

  function renderCouncilAccess(body, details, data) {
    var readiness = renderCouncilReadiness(data);
    var costLine = data.estimated_cost_usd !== undefined
      ? '<p class="council-estimate">Estimated cost: ' + escapeHtml(fmtEstimatedCost(data.estimated_cost_usd)) + " (" + data.calls + " calls)</p>"
      : "";
    var message = data.state === "blocked_no_flags" && data.message
      ? '<p class="report-caption">' + escapeHtml(data.message) + "</p>"
      : "";
    body.innerHTML =
      '<div class="council-access">' + readiness.html + message + costLine + '<div class="council-action"></div></div>';
    body.querySelector(".council-action").appendChild(
      consultControl(details.dataset.ticker, details, readiness.flagsOk, false)
    );
  }

  function renderOkCouncil(body, details, data) {
    var meta = data.meta || {};
    var chairman = data.chairman || {};
    var advisors = data.advisors || [];

    var advisorsHtml = advisors.length
      ? '<ul class="council-advisors">' + advisors.map(function (a) {
          return '<li class="council-advisor-item">'
            + '<span class="council-advisor-name">' + escapeHtml(a.name) + "</span>"
            + '<span class="council-advisor-position">' + escapeHtml(a.position || "unparsed") + "</span>"
            + "</li>";
        }).join("") + "</ul>"
      : "";

    var flagsHtml = (meta.status_flags || []).length
      ? '<p class="council-status-flags">' + meta.status_flags.map(escapeHtml).join(", ") + "</p>"
      : "";

    body.innerHTML =
      '<div class="council-toolbar"></div>'
      + '<div class="council-verdict">'
      + '<span class="council-verdict-label">' + escapeHtml(chairman.verdict || "n/a") + "</span>"
      + "</div>"
      + flagsHtml
      + advisorsHtml
      + '<p class="council-provenance">'
      + "Model: " + escapeHtml(meta.model || "n/a") + " &middot; Prompt: " + escapeHtml(meta.prompt_version || "n/a")
      + " &middot; Convened: " + escapeHtml(meta.convened_at || "n/a")
      + "</p>";

    var toolbar = body.querySelector(".council-toolbar");
    toolbar.appendChild(reportLinks(details.dataset.ticker));
    toolbar.appendChild(consultControl(details.dataset.ticker, details, true, true));
  }

  // "View report" / "Download PDF" — cache-only reads (engine/report_council.py
  // + app/pdf.py): never trigger a council convene or a Tier 2 extraction,
  // so these are plain links, not gated behind any confirm. Same
  // ghost-button family as Consult/Re-consult/Extract/Re-extract.
  function reportLinks(ticker) {
    var wrap = document.createElement("div");
    wrap.className = "council-report-links";

    var viewLink = document.createElement("a");
    viewLink.className = "btn council-report-link";
    viewLink.textContent = "View report";
    viewLink.href = "/api/council/" + encodeURIComponent(ticker) + "/report.html";
    viewLink.target = "_blank";
    viewLink.rel = "noopener";

    var pdfLink = document.createElement("a");
    pdfLink.className = "btn council-report-link";
    pdfLink.textContent = "Download PDF";
    pdfLink.href = "/api/council/" + encodeURIComponent(ticker) + "/report.pdf";
    pdfLink.download = ticker + "-council-review.pdf";

    wrap.appendChild(viewLink);
    wrap.appendChild(pdfLink);
    return wrap;
  }

  function renderCouncilBody(body, details, data) {
    if (data.state === "ok") {
      renderOkCouncil(body, details, data);
    } else {
      renderCouncilAccess(body, details, data);
    }
  }

  function loadCouncil(details, query) {
    var ticker = details.dataset.ticker;
    var body = details.querySelector(".council-body");
    var isConsult = !!query && query.indexOf("convene=true") !== -1;
    var url = "/api/council/" + encodeURIComponent(ticker) + (query ? "?" + query : "");
    (isConsult ? fetch(url, { method: "POST" }) : fetch(url))
      .then(function (r) {
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (b) {
            throw new Error(extractErrorMessage(b.detail, "HTTP " + r.status));
          });
        }
        return r.json();
      })
      .then(function (data) { renderCouncilBody(body, details, data); })
      .catch(function (e) {
        body.innerHTML = '<p class="report-caption">Council unavailable: ' + escapeHtml(e.message) + "</p>";
      });
  }

  // Same non-bubbling-"toggle"-event workaround as Flags above.
  document.addEventListener("toggle", function (ev) {
    var details = ev.target;
    if (!details.classList || !details.classList.contains("council-section")) return;
    if (!details.open || details.dataset.loaded === "true") return;
    details.dataset.loaded = "true";
    loadCouncil(details);
  }, true);

  // ---- remove from watchlist: inline confirm, immediate removal ----
  //
  // Bug (reported): click × -> a confirm bar at the bottom of the
  // viewport -> clicking Remove does nothing visible -> only a full page
  // reload shows the ticker gone. Root cause was the SUCCESS handler, not
  // a lost DOM reference: on a successful DELETE it called runScreen(),
  // which re-fetches the watchlist and kicks off a brand-new /api/screen
  // background job — polled every 3s, ~60s for a full watchlist — before
  // the table changes at all. Removing one row was silently routed
  // through "recompute every row's durability score from scratch," which
  // is why it looked broken rather than just slow.
  //
  // Fix: remove the row (and its accordion sibling, if expanded) directly
  // from the DOM and from the same in-memory array renderEquitiesBody()/
  // renderEtfBody() re-render from on sort, the moment the DELETE
  // succeeds — no re-screen involved. `dataArray` is null for the
  // Excluded table, which has no persistent sortable array to begin with
  // (renderScreen() rebuilds it directly from fetched data each run).
  //
  // This also replaces the old viewport-bottom confirm bar with an inline
  // "Remove? ✓ / ✕" affordance that morphs in place inside the same
  // control the × lives in — the confirmation now lives exactly where
  // the intent was expressed, closer to iOS swipe-to-delete than a modal.

  var closeOpenRemoveConfirm = null; // currently-open inline confirm's own reset fn, or null

  document.addEventListener("click", function (ev) {
    if (closeOpenRemoveConfirm && !ev.target.closest(".row-remove-wrap")) {
      closeOpenRemoveConfirm();
    }
  });

  function removeControl(ticker, tr, dataArray, sectionEl, tableBodyEl, onRemoved) {
    var wrap = document.createElement("span");
    wrap.className = "row-remove-wrap";

    var trigger = document.createElement("span");
    trigger.className = "row-remove";
    trigger.textContent = "×";
    trigger.title = "Remove " + ticker + " from watchlist";
    wrap.appendChild(trigger);

    function showTrigger() {
      if (closeOpenRemoveConfirm === showTrigger) closeOpenRemoveConfirm = null;
      wrap.innerHTML = "";
      wrap.appendChild(trigger);
    }

    function showConfirm() {
      if (closeOpenRemoveConfirm) closeOpenRemoveConfirm();
      wrap.innerHTML = "";

      var confirmWrap = document.createElement("span");
      confirmWrap.className = "row-remove-confirm";

      var label = document.createElement("span");
      label.className = "row-remove-label";
      label.textContent = "Remove?";

      var yes = document.createElement("button");
      yes.type = "button";
      yes.className = "row-remove-yes";
      yes.textContent = "✓";
      yes.title = "Confirm — remove " + ticker;

      var no = document.createElement("button");
      no.type = "button";
      no.className = "row-remove-no";
      no.textContent = "✕";
      no.title = "Cancel";

      confirmWrap.appendChild(label);
      confirmWrap.appendChild(yes);
      confirmWrap.appendChild(no);
      wrap.appendChild(confirmWrap);

      yes.addEventListener("click", function (ev) {
        ev.stopPropagation();
        yes.disabled = true;
        no.disabled = true;
        doRemove();
      });
      no.addEventListener("click", function (ev) {
        ev.stopPropagation();
        showTrigger();
      });

      closeOpenRemoveConfirm = showTrigger;
    }

    function doRemove() {
      fetch("/api/watchlist/" + encodeURIComponent(ticker), { method: "DELETE" })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          closeOpenRemoveConfirm = null;
          // An open accordion for this ticker is a sibling <tr> tied to
          // this one — drop it too, or it's left dangling with no parent
          // row once `tr` itself is removed below.
          if (expandedRows[ticker]) {
            expandedRows[ticker].remove();
            delete expandedRows[ticker];
          }
          if (dataArray) {
            var idx = dataArray.findIndex(function (r2) { return r2.ticker === ticker; });
            if (idx !== -1) dataArray.splice(idx, 1);
          }
          tr.remove();
          if (sectionEl && tableBodyEl && tableBodyEl.children.length === 0) {
            sectionEl.classList.add("hidden");
          }
          // Table-specific follow-up (e.g. Excluded's own empty-state
          // toggle, which never hides its whole section — see
          // updateExcludedEmptyState()) — optional, most callers pass none.
          if (onRemoved) onRemoved();
          checkEmptyWatchlist();
        })
        .catch(function (e) {
          showBanner("Failed to remove " + ticker + ": " + e.message, true);
          showTrigger();
        });
    }

    trigger.addEventListener("click", function (ev) {
      ev.stopPropagation();
      showConfirm();
    });

    return wrap;
  }

  // After any single-row removal, the watchlist may now be completely
  // empty — the same "nothing to show" state runScreen() already renders
  // when /api/watchlist reports zero tickers, applied directly since the
  // removal itself is the new source of truth for the count (no need for
  // a fresh fetch just to learn what we already know). Also hides the
  // three per-table sections so a cascade of individual removals doesn't
  // leave e.g. Excluded's own "no excluded securities" empty-state
  // showing alongside the global one — a real empty watchlist gets ONE
  // empty state, not several nested ones.
  function checkEmptyWatchlist() {
    var allEmpty = els.equitiesBody.children.length === 0
      && els.etfBody.children.length === 0
      && els.excludedBody.children.length === 0;
    if (allEmpty) {
      els.emptyState.classList.remove("hidden");
      els.metaLine.classList.add("hidden");
      els.equitiesSection.classList.add("hidden");
      els.etfSection.classList.add("hidden");
      els.excludedSection.classList.add("hidden");
    }
  }

  // The remove control is absolutely positioned (see .row-remove-wrap in
  // styles.css), so it must live INSIDE a real <td> — never appended as
  // an extra <tr> child, which would be invalid HTML with an off-by-one
  // column count.
  function appendRemoveControl(lastCell, ticker, tr, dataArray, sectionEl, tableBodyEl, onRemoved) {
    lastCell.appendChild(removeControl(ticker, tr, dataArray, sectionEl, tableBodyEl, onRemoved));
    return lastCell;
  }

  // Wires the whole-row click -> accordion toggle for equity/ETF rows.
  // The remove "×" already stopPropagation()s, so it doesn't trigger this.
  function makeExpandable(tr, ticker) {
    tr.addEventListener("click", function () { toggleAccordion(ticker, tr); });
    return tr;
  }

  // Tints whichever cell corresponds to the table's currently-active sort
  // key (if any) with .col-sorted — a ~2% wash so it's visually obvious
  // what's ordering the table from every row, not just the header glyph.
  // Purely a class toggle on an already-built cell; never touches
  // sortState/sortRows() itself.
  function markSortedCell(cellsByKey, activeKey) {
    if (activeKey && cellsByKey[activeKey]) cellsByKey[activeKey].classList.add("col-sorted");
  }

  function renderEquitiesRow(row, index, animate) {
    var tr = document.createElement("tr");
    var cellsByKey = {};

    var tickerTd = tickerCell(row.ticker, true);
    tr.appendChild(tickerTd);
    cellsByKey.ticker = tickerTd;

    var compositeTd = scoreCell(row.composite, fmtScore(row.composite), { composite: true, animate: animate, stagger: index });
    appendDurGapsIndicator(compositeTd, row.durability_gaps);
    tr.appendChild(compositeTd);
    cellsByKey.composite = compositeTd;

    [
      ["cat_reinvestment", row.cat_reinvestment],
      ["cat_quality", row.cat_quality],
      ["cat_resilience", row.cat_resilience],
      ["cat_discipline", row.cat_discipline],
      ["cat_optionality", row.cat_optionality],
    ].forEach(function (pair) {
      var key = pair[0], value = pair[1];
      var cell = scoreCell(value, fmtScore(value), { animate: animate, stagger: index });
      tr.appendChild(cell);
      cellsByKey[key] = cell;
    });

    var gated = !!row.implied_growth_note;
    var impliedTd = td(gated ? null : fmtPct(row.implied_fcf_growth));
    // Trust-tier indicator (Session B, the "Visa condition"): only when a
    // headline number actually rests on a lower-trust input — a market-
    // vendor (yfinance) share count because EDGAR's diluted_shares
    // extraction failed for this ticker. Not shown for every yfinance-
    // sourced quote, only where trust tier changes the interpretation of
    // this score-derived number.
    var hasMkt = row.quote_source === "yfinance" && row.diluted_shares_gap;
    if (hasMkt) {
      appendBasisBadge(
        impliedTd, "mkt",
        "Share count from yfinance (market-vendor tier) — EDGAR diluted_shares " +
          "unavailable for this ticker (e.g. a multi-class share structure). " +
          "Implied growth here rests on a lower-trust input than every other row."
      );
    }
    tr.appendChild(impliedTd);
    cellsByKey.implied_fcf_growth = impliedTd;

    var deliveredTd = td(fmtPct(row.delivered_fcf_growth));
    // Stale-window note (Session B): cagr_over's window can run far past
    // the requested horizon when a data gap forces the earliest usable
    // point much further back (e.g. NVDA's permanent capex absence). Only
    // shown when that note is actually present in the label.
    var hasWin = !!(row.delivered_growth_label && row.delivered_growth_label.indexOf("window:") !== -1);
    if (hasWin) {
      appendBasisBadge(deliveredTd, "win", "Basis: " + row.delivered_growth_label);
    }
    // Mixed-base marker (moved here from the Gap column — badge-placement
    // fix): REV qualifies DELIVERED's basis (a revenue-CAGR fallback, not
    // FCF), not the gap itself. On every row currently carrying it the gap
    // is n/a (no FCF history to solve a like-for-like comparison against),
    // so badging the Gap cell was marking a number that doesn't exist.
    // Tooltip is conditional: the common case today has no computed gap at
    // all; the mixed-base-comparison wording only applies on the (currently
    // untriggered) path where a gap IS computed despite the fallback.
    var hasRev = !!(row.delivered_growth_label && row.delivered_growth_label.indexOf("revenue CAGR") === 0);
    if (hasRev) {
      var revTooltip = gated
        ? "Delivered growth is a revenue-CAGR fallback (FCF history non-positive " +
          "or unavailable), not FCF. No expectations gap is computed for this row."
        : "Delivered growth is a revenue-CAGR fallback (FCF history non-positive " +
          "or unavailable), not FCF. This Gap compares implied FCF growth " +
          "against delivered REVENUE growth — not a like-for-like FCF gap.";
      appendBasisBadge(deliveredTd, "rev", revTooltip);
    }
    tr.appendChild(deliveredTd);
    cellsByKey.delivered_fcf_growth = deliveredTd;

    var gapVal = gated ? null : row.expectations_gap;
    var gapTd = gapCell(gapVal, gated ? null : fmtSignedPct(row.expectations_gap));
    // Inheritance marker: the Gap is implied minus delivered, so it
    // silently inherits whichever upstream caveats apply to either
    // component. Only shown when the Gap itself is a real, computed number
    // — an n/a pill already discloses its own absence and has nothing to
    // inherit into (this is why hasRev alone, on today's always-gated
    // rows, never lights this up — only the untriggered REV-with-a-real-
    // gap path would). An outline-only "INH" chip, quieter than a filled
    // basis-badge: "this value has an upstream caveat," one step down from
    // the direct disclosures already on Implied g/Delivered g themselves.
    if (!gated) {
      var inherited = [];
      if (hasMkt) inherited.push("MKT (implied uses vendor-tier share count)");
      if (hasWin) {
        var winMatch = /window: (\d+)y actual vs (\d+)y requested/.exec(row.delivered_growth_label);
        inherited.push(winMatch
          ? "WIN (delivered window " + winMatch[1] + "y vs " + winMatch[2] + "y requested)"
          : "WIN (delivered uses an extended CAGR window)");
      }
      if (hasRev) inherited.push("REV (delivered is revenue CAGR, not FCF)");
      if (inherited.length) {
        appendInheritChip(gapTd, inherited);
      }
    }
    // Expectations-gap scenario band (PR 3): FRAG fires on a COMPLETE band
    // whose sign flips bull/base/bear; FRAG? fires on a PARTIAL band, where
    // fragility is UNDETERMINABLE rather than silently "not fragile". No
    // band at all (NO_BAND -- band_status null) renders nothing extra, same
    // as every row before this PR.
    if (row.expectations_gap_band_status === "COMPLETE" && row.expectations_gap_fragile === "FRAGILE") {
      appendFragChip(gapTd, "FRAGILE", row.expectations_gap_scenarios);
    } else if (row.expectations_gap_band_status === "PARTIAL") {
      appendFragChip(gapTd, "UNDETERMINABLE", row.expectations_gap_scenarios);
    }
    cellsByKey.expectations_gap = gapTd;
    tr.appendChild(appendRemoveControl(gapTd, row.ticker, tr, equitiesData, els.equitiesSection, els.equitiesBody));

    markSortedCell(cellsByKey, sortState.equities.key);
    return makeExpandable(tr, row.ticker);
  }

  function renderEtfRow(row) {
    var tr = document.createElement("tr");
    var cellsByKey = {};

    var tickerTd = tickerCell(row.ticker, true);
    tr.appendChild(tickerTd);
    cellsByKey.ticker = tickerTd;

    tr.appendChild(td(row.name, { cls: "l" }));

    var expenseTd = td(fmtPct(row.expense_ratio, 2));
    tr.appendChild(expenseTd);
    cellsByKey.expense_ratio = expenseTd;

    var aumTd = td(fmtAum(row.aum));
    tr.appendChild(aumTd);
    cellsByKey.aum = aumTd;

    var overlapTd = td(fmtOverlap(row.overlap_with_screen, row.overlap_count));
    tr.appendChild(overlapTd);
    cellsByKey.overlap_with_screen = overlapTd;

    var flagCell = td(row.flag, { cls: "l" });
    tr.appendChild(appendRemoveControl(flagCell, row.ticker, tr, etfData, els.etfSection, els.etfBody));

    markSortedCell(cellsByKey, sortState.etf.key);
    return makeExpandable(tr, row.ticker);
  }

  // Excluded's own empty state (a proper icon + "No excluded securities in
  // this run" message, never just a bare header over white void) —
  // toggled here instead of hiding the whole section, since the section
  // itself stays visible whenever the watchlist as a whole isn't empty
  // (see renderScreen() and checkEmptyWatchlist()).
  function updateExcludedEmptyState() {
    var empty = els.excludedBody.children.length === 0;
    els.excludedScroll.classList.toggle("hidden", empty);
    els.excludedEmpty.classList.toggle("hidden", !empty);
  }

  function renderExcludedRow(row) {
    // No accordion here — durability scoring didn't run for excluded
    // tickers, so there's no analysis to expand.
    var tr = document.createElement("tr");
    tr.appendChild(tickerCell(row.ticker));
    var reasonCell = td(humanizeReason(row.flag), { cls: "l" });
    // sectionEl is null here (unlike equities/etf): Excluded's SECTION
    // never auto-hides on empty, only its inner content swaps to the
    // empty-state message via the onRemoved callback.
    tr.appendChild(appendRemoveControl(reasonCell, row.ticker, tr, null, null, els.excludedBody, updateExcludedEmptyState));
    return tr;
  }

  // ---- Data Diagnostics (below Excluded, collapsed by default) ----
  //
  // Uses the same equities rows already returned by /api/screen (each
  // ScreenRow is serialized in full via dataclasses.asdict() — see
  // app/main.py's _serialize_screen()) — no second screen run.
  //
  // Exceptions-only: a ticker that's 100% complete, stable, and note-free
  // is pure noise here (its band/completeness/stable columns are always
  // identical) and is omitted entirely. universe_version/config_hash are
  // identical across every row in a single run, so they're shown once as
  // a run-level stamp instead of repeated per row.

  function bandDisplay(row) {
    var loNa = row.composite_low === null || row.composite_low === undefined;
    var hiNa = row.composite_high === null || row.composite_high === undefined;
    if (loNa && hiNa) return null;
    var lo = loNa ? "n/a" : row.composite_low.toFixed(1);
    var hi = hiNa ? "n/a" : row.composite_high.toFixed(1);
    return lo + "–" + hi;
  }

  function stableDisplay(row) {
    if (row.is_stable === null || row.is_stable === undefined) return null;
    return row.is_stable ? "yes" : "unstable";
  }

  function diagnosticsRowNeeded(row) {
    var completenessFull = row.completeness !== null && row.completeness !== undefined && row.completeness >= 1;
    var unstable = row.is_stable === false;
    var hasNote = !!(row.flag && row.flag.length);
    return !completenessFull || unstable || hasNote;
  }

  function renderDiagnosticsRow(row) {
    var tr = document.createElement("tr");
    tr.appendChild(td(row.ticker, { cls: "l" }));
    tr.appendChild(td(bandDisplay(row), { cls: "l" }));
    tr.appendChild(td(fmtPct(row.completeness)));
    tr.appendChild(td(stableDisplay(row), { cls: "l" }));
    tr.appendChild(td(row.flag ? humanizeReason(row.flag) : null, { cls: "l" }));
    return tr;
  }

  function renderDiagnostics(data) {
    var equities = data.equities;
    var exceptions = equities.filter(diagnosticsRowNeeded);

    var stampParts = [];
    if (data.universe) stampParts.push("Universe " + data.universe);
    if (data.config_hash) stampParts.push("config " + data.config_hash);
    els.diagnosticsStamp.textContent = stampParts.join(" · ");

    els.diagnosticsBody.innerHTML = "";
    exceptions.forEach(function (r) { els.diagnosticsBody.appendChild(renderDiagnosticsRow(r)); });

    els.diagnosticsTableWrap.classList.toggle("hidden", exceptions.length === 0);
    els.diagnosticsClean.classList.toggle("hidden", exceptions.length !== 0);
    els.diagnosticsSection.classList.toggle("hidden", equities.length === 0);
  }

  function renderScreen(data) {
    // A fresh screen run replaces every row, so any accordion <tr>
    // currently inserted is gone too — drop the stale DOM references. The
    // fetched fragment HTML in fragmentCache is still valid and reused if
    // the ticker is expanded again. A new run's data is also a new
    // "default order" baseline, so any active sort resets.
    expandedRows = {};
    equitiesData = data.equities.slice();
    etfData = data.etfs.slice();
    sortState.equities = { key: null, dir: null };
    sortState.etf = { key: null, dir: null };

    els.excludedBody.innerHTML = "";

    renderEquitiesBody();
    renderEtfBody();
    data.excluded.forEach(function (r) { els.excludedBody.appendChild(renderExcludedRow(r)); });
    renderDiagnostics(data);

    els.equitiesSection.classList.toggle("hidden", data.equities.length === 0);
    els.etfSection.classList.toggle("hidden", data.etfs.length === 0);
    // Excluded's SECTION always shows once we're here (runScreen() already
    // guarantees the watchlist as a whole isn't empty — see its own
    // total===0 early return) — an empty Excluded gets its own proper
    // empty-state message instead of disappearing entirely.
    els.excludedSection.classList.remove("hidden");
    updateExcludedEmptyState();

    buildMetaLine(data);
  }

  // Provenance footer: mono, small, muted-but-legible, with the config
  // hash as its own copyable code chip (click to copy) rather than plain
  // inline text — this is the "which assumption set produced these
  // numbers" audit trail, worth making it easy to paste elsewhere.
  function buildMetaLine(data) {
    els.metaLine.innerHTML = "";
    var any = false;
    if (data.universe) {
      var uni = document.createElement("span");
      uni.textContent = "Universe " + data.universe;
      els.metaLine.appendChild(uni);
      any = true;
    }
    if (data.config_hash) {
      var chip = document.createElement("code");
      chip.className = "meta-chip";
      chip.textContent = data.config_hash;
      chip.title = "Click to copy the config hash";
      chip.addEventListener("click", function () {
        if (!navigator.clipboard || !navigator.clipboard.writeText) return;
        navigator.clipboard.writeText(data.config_hash).then(function () {
          chip.classList.add("copied");
          window.setTimeout(function () { chip.classList.remove("copied"); }, 1200);
        });
      });
      els.metaLine.appendChild(chip);
      any = true;
    }
    if (data.generated_at) {
      var gen = document.createElement("span");
      gen.textContent = "Generated " + data.generated_at;
      els.metaLine.appendChild(gen);
      any = true;
    }
    els.metaLine.classList.toggle("hidden", !any);
  }

  // ---- screen job: kick off + poll every 3s until done/error ----

  var screenPollTimer = null;

  function stopPolling() {
    if (screenPollTimer) {
      clearInterval(screenPollTimer);
      screenPollTimer = null;
    }
  }

  function pollScreenStatus(jobId) {
    stopPolling();
    screenPollTimer = setInterval(function () {
      apiGet("/api/screen/status/" + jobId)
        .then(function (job) {
          if (job.status === "running") return;
          stopPolling();
          if (job.status === "done") {
            hideBanner();
            renderScreen(job.result);
          } else {
            showBanner("Screen run failed: " + (job.error || "unknown error"), true);
          }
        })
        .catch(function (e) {
          stopPolling();
          showBanner("Lost contact with the screen job: " + e.message, true);
        });
    }, 3000);
  }

  function runScreen() {
    apiGet("/api/watchlist").then(function (wl) {
      var total = wl.tickers.length + wl.etfs.length;
      if (total === 0) {
        els.emptyState.classList.remove("hidden");
        els.equitiesSection.classList.add("hidden");
        els.etfSection.classList.add("hidden");
        els.excludedSection.classList.add("hidden");
        els.metaLine.classList.add("hidden");
        hideBanner();
        return;
      }
      els.emptyState.classList.add("hidden");
      showBanner("Running screen… this takes about 60 seconds for a full watchlist.");
      fetch("/api/screen")
        .then(function (r) { return r.json(); })
        .then(function (payload) { pollScreenStatus(payload.job_id); })
        .catch(function (e) { showBanner("Failed to start screen run: " + e.message, true); });
    });
  }

  // ---- search bar: debounced preflight + inline add/analyze confirm ----

  var searchDebounce = null;

  function setClassificationBadge(text, pending) {
    els.searchClassificationBadge.textContent = text;
    els.searchClassificationBadge.classList.toggle("pending", !!pending);
  }

  function resetSearchConfirm() {
    currentSearchTicker = null;
    els.searchConfirm.classList.add("hidden");
    setClassificationBadge("pending", true);
  }

  // Fetches the resolved equity/ETF classification for the confirm card's
  // badge. Fired right after a search finds a ticker; the badge starts on
  // "pending" and updates in place once this resolves — classification
  // needs an EDGAR (and sometimes yfinance) lookup, so it's slower than
  // the instant found/not-found search feedback and shown separately.
  function fetchClassificationBadge(ticker) {
    apiGet("/api/classify/" + encodeURIComponent(ticker))
      .then(function (res) {
        if (currentSearchTicker !== ticker) return; // stale — card moved on
        if (res.kind) {
          setClassificationBadge(res.label, false);
        } else {
          setClassificationBadge("pending", true);
        }
      })
      .catch(function () {
        if (currentSearchTicker !== ticker) return;
        setClassificationBadge("pending", true);
      });
  }

  els.searchInput.addEventListener("input", function () {
    var raw = els.searchInput.value.trim().toUpperCase();
    if (searchDebounce) clearTimeout(searchDebounce);
    els.searchStatus.textContent = "";
    els.searchStatus.className = "search-status";
    resetSearchConfirm();

    if (!raw) return;

    searchDebounce = setTimeout(function () {
      apiGet("/api/search/" + encodeURIComponent(raw))
        .then(function (res) {
          if (els.searchInput.value.trim().toUpperCase() !== raw) return; // stale response
          if (res.found) {
            els.searchStatus.textContent = "✓";
            els.searchStatus.className = "search-status found";
            currentSearchTicker = raw;
            var label = res.name ? raw + " — " + res.name : raw;
            els.searchConfirmText.textContent = label;
            setClassificationBadge("pending", true);
            els.searchConfirm.classList.remove("hidden");
            fetchClassificationBadge(raw);
          } else {
            els.searchStatus.textContent = "✗";
            els.searchStatus.className = "search-status not-found";
          }
        })
        .catch(function () {
          els.searchStatus.textContent = "✗";
          els.searchStatus.className = "search-status not-found";
        });
    }, 300);
  });

  els.searchInput.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && currentSearchTicker) {
      addSearchedTickerToWatchlist();
    }
  });

  els.searchAddBtn.addEventListener("click", addSearchedTickerToWatchlist);

  function addSearchedTickerToWatchlist() {
    if (!currentSearchTicker) return;
    // No client-chosen type: the backend resolves equity vs. ETF/fund the
    // same way it resolved the badge above (evidence-based classification),
    // never a client-supplied default.
    fetch("/api/watchlist/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: currentSearchTicker }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (b) { throw new Error(b.detail || "add failed"); });
        return r.json();
      })
      .then(function () {
        resetSearchConfirm();
        els.searchInput.value = "";
        els.searchStatus.textContent = "";
        runScreen();
      })
      .catch(function (e) { showBanner("Failed to add ticker: " + e.message, true); });
  }

  // ---- export: CSV + PDF ----

  function csvEscape(v) {
    v = String(v);
    return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }

  // Absence-is-not-zero for exports too, but CSV's blank convention is an
  // empty field, not the "n/a" label the UI shows — a spreadsheet reading
  // "n/a" into a numeric column would coerce it to NaN/text, while a
  // genuinely empty cell stays absent.
  function csvVal(v) {
    return v === null || v === undefined ? "" : v;
  }

  function downloadCsv(filename, lines) {
    var blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8;" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // Exports rows in whatever order is currently on screen (same sortRows()
  // call renderEquitiesBody()/renderEtfBody() use), not the default order.
  function exportEquitiesCsv() {
    if (equitiesData.length === 0) return;
    var rows = sortRows(equitiesData, sortState.equities.key, sortState.equities.dir, equitiesSortValue);
    var lines = [[
      "Ticker", "Durability", "Reinv", "Quality", "Resilience", "Discipline",
      "Optionality", "Implied g", "Delivered g", "Gap",
    ].map(csvEscape).join(",")];
    rows.forEach(function (r) {
      var gated = !!r.implied_growth_note;
      lines.push([
        r.ticker,
        csvVal(fmtScore(r.composite)),
        csvVal(fmtScore(r.cat_reinvestment)),
        csvVal(fmtScore(r.cat_quality)),
        csvVal(fmtScore(r.cat_resilience)),
        csvVal(fmtScore(r.cat_discipline)),
        csvVal(fmtScore(r.cat_optionality)),
        csvVal(gated ? null : fmtPct(r.implied_fcf_growth)),
        csvVal(fmtPct(r.delivered_fcf_growth)),
        csvVal(gated ? null : fmtSignedPct(r.expectations_gap)),
      ].map(csvEscape).join(","));
    });
    downloadCsv("equities.csv", lines);
  }

  function exportEtfCsv() {
    if (etfData.length === 0) return;
    var rows = sortRows(etfData, sortState.etf.key, sortState.etf.dir, etfSortValue);
    var lines = [["Ticker", "Name", "Exp Ratio", "AUM", "Overlap w/ Singles", "Evidence"].map(csvEscape).join(",")];
    rows.forEach(function (r) {
      lines.push([
        r.ticker,
        csvVal(r.name),
        csvVal(fmtPct(r.expense_ratio, 2)),
        csvVal(fmtAum(r.aum)),
        csvVal(fmtOverlap(r.overlap_with_screen, r.overlap_count)),
        csvVal(r.flag),
      ].map(csvEscape).join(","));
    });
    downloadCsv("etfs_funds.csv", lines);
  }

  els.exportCsvBtn.addEventListener("click", function () {
    exportEquitiesCsv();
    exportEtfCsv();
  });

  els.exportPdfBtn.addEventListener("click", function () { window.print(); });

  // ---- refresh button ----

  els.refreshBtn.addEventListener("click", runScreen);

  // ---- Usage modal (Tier 2 spend visibility, GET /api/usage) ----

  var _MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function fmtUsd(v) {
    return v === null || v === undefined ? "—" : "$" + v.toFixed(2);
  }

  function fmtMonthLabel(key) {
    var parts = key.split("-");
    return _MONTH_NAMES[parseInt(parts[1], 10) - 1] + " " + parts[0];
  }

  function renderUsageModal(data) {
    var rows = (data.monthly_breakdown || []).map(function (m) {
      return "<tr><td class=\"l\">" + escapeHtml(fmtMonthLabel(m.month)) + "</td>"
        + "<td>" + escapeHtml(fmtUsd(m.cost_usd)) + "</td>"
        + "<td>" + escapeHtml(String(m.calls)) + "</td></tr>";
    }).join("");

    var unknownPricingNote = data.rows_with_unknown_pricing
      ? '<p class="usage-unknown-pricing-note">'
        + data.rows_with_unknown_pricing
        + (data.rows_with_unknown_pricing === 1 ? " call has" : " calls have")
        + " no pricing configured and are excluded from the totals above — the real spend is at least this much."
        + "</p>"
      : "";

    els.usageModalBody.innerHTML =
      '<div class="usage-stats">'
      + '<div class="usage-stat"><span class="usage-stat-label">Lifetime spend</span><span class="usage-stat-value">' + escapeHtml(fmtUsd(data.lifetime_total_usd)) + "</span></div>"
      + '<div class="usage-stat"><span class="usage-stat-label">This month</span><span class="usage-stat-value">' + escapeHtml(fmtUsd(data.current_month.cost_usd)) + "</span></div>"
      + '<div class="usage-stat"><span class="usage-stat-label">This year</span><span class="usage-stat-value">' + escapeHtml(fmtUsd(data.current_year.cost_usd)) + "</span></div>"
      + '<div class="usage-stat"><span class="usage-stat-label">Trailing-12mo projection</span><span class="usage-stat-value">' + escapeHtml(fmtUsd(data.trailing_12mo_projection_usd)) + "</span></div>"
      + "</div>"
      + '<p class="usage-projection-note">Projection = trailing 30-day spend &times; 12, assuming the current usage rate continues.</p>'
      + unknownPricingNote
      + '<div class="surface"><div class="scroll"><table class="usage-table">'
      + '<thead><tr><th class="l">Month</th><th>Cost</th><th>Calls</th></tr></thead>'
      + "<tbody>" + rows + "</tbody>"
      + "</table></div></div>";
  }

  function openUsageModal() {
    els.usageModalOverlay.classList.remove("hidden");
    els.usageModalBody.innerHTML = '<p class="report-caption">Loading…</p>';
    apiGet("/api/usage")
      .then(renderUsageModal)
      .catch(function (e) {
        els.usageModalBody.innerHTML = '<p class="report-caption">Usage unavailable: ' + escapeHtml(e.message) + "</p>";
      });
  }

  function closeUsageModal() {
    els.usageModalOverlay.classList.add("hidden");
  }

  els.usageBtn.addEventListener("click", openUsageModal);
  els.usageModalClose.addEventListener("click", closeUsageModal);
  els.usageModalOverlay.addEventListener("click", function (ev) {
    if (ev.target === els.usageModalOverlay) closeUsageModal();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && !els.usageModalOverlay.classList.contains("hidden")) closeUsageModal();
  });

  // ---- boot ----

  runScreen();
})();
