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
    if (opts.tint) {
      var rgb = opts.tint > 0 ? "200,54,47" : "29,125,84";
      var color = opts.tint > 0 ? "var(--bad)" : "var(--good)";
      var magnitude = Math.min(Math.abs(opts.tint), 0.30);
      var alpha = 0.06 + (magnitude / 0.30) * 0.10;
      cell.style.backgroundColor = "rgba(" + rgb + "," + alpha.toFixed(3) + ")";
      cell.style.color = color;
    }
    return cell;
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
    statusBanner: document.getElementById("status-banner"),
    emptyState: document.getElementById("empty-state"),
    equitiesSection: document.getElementById("equities-section"),
    etfSection: document.getElementById("etf-section"),
    excludedSection: document.getElementById("excluded-section"),
    equitiesBody: document.querySelector("#equities-table tbody"),
    etfBody: document.querySelector("#etf-table tbody"),
    excludedBody: document.querySelector("#excluded-table tbody"),
    metaLine: document.getElementById("meta-line"),
    removeConfirm: document.getElementById("remove-confirm"),
    removeConfirmText: document.getElementById("remove-confirm-text"),
    removeConfirmBtn: document.getElementById("remove-confirm-btn"),
    removeCancelBtn: document.getElementById("remove-cancel-btn"),
  };

  var currentSearchTicker = null; // ticker the confirm bar currently refers to
  var pendingRemoveTicker = null;

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
      var arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      arrow.textContent = (state.key && th.getAttribute("data-sort-key") === state.key)
        ? (state.dir === "asc" ? " ▲" : " ▼")
        : "";
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

  function renderEquitiesBody() {
    var rows = sortRows(equitiesData, sortState.equities.key, sortState.equities.dir, equitiesSortValue);
    clearExpandedFor(equitiesData);
    els.equitiesBody.innerHTML = "";
    rows.forEach(function (r) { els.equitiesBody.appendChild(renderEquitiesRow(r)); });
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

  function toggleAccordion(ticker, row) {
    var existing = expandedRows[ticker];
    if (existing) {
      existing.remove();
      delete expandedRows[ticker];
      row.classList.remove("row-expanded");
      return;
    }

    row.classList.add("row-expanded");
    var accRow = document.createElement("tr");
    accRow.className = "accordion-row";
    var cell = document.createElement("td");
    cell.colSpan = row.cells.length;
    accRow.appendChild(cell);
    row.parentNode.insertBefore(accRow, row.nextSibling);
    expandedRows[ticker] = accRow;

    if (fragmentCache[ticker]) {
      cell.innerHTML = fragmentCache[ticker];
      return;
    }

    cell.innerHTML = '<div class="accordion-loading">Loading analysis for ' + ticker + '…</div>';

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
          cell.innerHTML = html;
        }
      })
      .catch(function (e) {
        if (expandedRows[ticker] === accRow) {
          cell.innerHTML = '<div class="accordion-loading">Failed to load analysis: ' + e.message + "</div>";
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

  function removeButton(ticker) {
    var btn = document.createElement("span");
    btn.className = "row-remove";
    btn.textContent = "×";
    btn.title = "Remove " + ticker + " from watchlist";
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      confirmRemove(ticker);
    });
    return btn;
  }

  function confirmRemove(ticker) {
    pendingRemoveTicker = ticker;
    els.removeConfirmText.textContent = "Remove " + ticker + " from the watchlist?";
    els.removeConfirm.classList.remove("hidden");
  }

  els.removeCancelBtn.addEventListener("click", function () {
    pendingRemoveTicker = null;
    els.removeConfirm.classList.add("hidden");
  });

  els.removeConfirmBtn.addEventListener("click", function () {
    if (!pendingRemoveTicker) return;
    var ticker = pendingRemoveTicker;
    els.removeConfirm.classList.add("hidden");
    fetch("/api/watchlist/" + encodeURIComponent(ticker), { method: "DELETE" })
      .then(function () {
        pendingRemoveTicker = null;
        runScreen();
      })
      .catch(function (e) { showBanner("Failed to remove " + ticker + ": " + e.message, true); });
  });

  // The remove "×" is absolutely positioned (see .row-remove in styles.css),
  // so it must live INSIDE a real <td> — never appended as an extra <tr>
  // child, which would be invalid HTML with an off-by-one column count.
  function appendRemoveButton(lastCell, ticker) {
    lastCell.appendChild(removeButton(ticker));
    return lastCell;
  }

  // Wires the whole-row click -> accordion toggle for equity/ETF rows.
  // The remove "×" already stopPropagation()s, so it doesn't trigger this.
  function makeExpandable(tr, ticker) {
    tr.addEventListener("click", function () { toggleAccordion(ticker, tr); });
    return tr;
  }

  function renderEquitiesRow(row) {
    var tr = document.createElement("tr");
    tr.appendChild(tickerCell(row.ticker, true));
    tr.appendChild(td(fmtScore(row.composite)));
    tr.appendChild(td(fmtScore(row.cat_reinvestment)));
    tr.appendChild(td(fmtScore(row.cat_quality)));
    tr.appendChild(td(fmtScore(row.cat_resilience)));
    tr.appendChild(td(fmtScore(row.cat_discipline)));
    tr.appendChild(td(fmtScore(row.cat_optionality)));
    var gated = !!row.implied_growth_note;
    tr.appendChild(td(gated ? null : fmtPct(row.implied_fcf_growth)));
    tr.appendChild(td(fmtPct(row.delivered_fcf_growth)));
    var gapVal = gated ? null : row.expectations_gap;
    var gapCell = td(gated ? null : fmtSignedPct(row.expectations_gap), { tint: gapVal });
    tr.appendChild(appendRemoveButton(gapCell, row.ticker));
    return makeExpandable(tr, row.ticker);
  }

  function renderEtfRow(row) {
    var tr = document.createElement("tr");
    tr.appendChild(tickerCell(row.ticker, true));
    tr.appendChild(td(row.name, { cls: "l" }));
    tr.appendChild(td(fmtPct(row.expense_ratio, 2)));
    tr.appendChild(td(fmtAum(row.aum)));
    tr.appendChild(td(fmtOverlap(row.overlap_with_screen, row.overlap_count)));
    var flagCell = td(row.flag, { cls: "l" });
    tr.appendChild(appendRemoveButton(flagCell, row.ticker));
    return makeExpandable(tr, row.ticker);
  }

  function renderExcludedRow(row) {
    // No accordion here — durability scoring didn't run for excluded
    // tickers, so there's no analysis to expand.
    var tr = document.createElement("tr");
    tr.appendChild(tickerCell(row.ticker));
    var reasonCell = td(humanizeReason(row.flag), { cls: "l" });
    tr.appendChild(appendRemoveButton(reasonCell, row.ticker));
    return tr;
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

    els.equitiesSection.classList.toggle("hidden", data.equities.length === 0);
    els.etfSection.classList.toggle("hidden", data.etfs.length === 0);
    els.excludedSection.classList.toggle("hidden", data.excluded.length === 0);

    var parts = [];
    if (data.universe) parts.push("Universe: " + data.universe);
    if (data.config_hash) parts.push("Config: " + data.config_hash);
    if (data.generated_at) parts.push("Generated: " + data.generated_at);
    els.metaLine.textContent = parts.join("  ·  ");
    els.metaLine.classList.toggle("hidden", parts.length === 0);
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

  // ---- refresh button ----

  els.refreshBtn.addEventListener("click", runScreen);

  // ---- boot ----

  runScreen();
})();
