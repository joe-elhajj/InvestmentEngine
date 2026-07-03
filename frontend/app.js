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
    searchAddBtn: document.getElementById("search-add-btn"),
    searchAnalyzeBtn: document.getElementById("search-analyze-btn"),
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

  function openAnalyze(ticker) {
    window.open("/api/analyze/" + encodeURIComponent(ticker), "_blank");
  }

  function tickerCell(ticker) {
    var cell = document.createElement("td");
    cell.className = "tk";
    cell.textContent = ticker;
    var arrow = document.createElement("span");
    arrow.className = "goto";
    arrow.textContent = "→";
    cell.appendChild(arrow);
    cell.title = "Open full analysis for " + ticker;
    cell.addEventListener("click", function () { openAnalyze(ticker); });
    return cell;
  }

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

  function renderEquitiesRow(row) {
    var tr = document.createElement("tr");
    tr.appendChild(tickerCell(row.ticker));
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
    return tr;
  }

  function renderEtfRow(row) {
    var tr = document.createElement("tr");
    tr.appendChild(tickerCell(row.ticker));
    tr.appendChild(td(row.name, { cls: "l" }));
    tr.appendChild(td(fmtPct(row.expense_ratio, 2)));
    tr.appendChild(td(fmtAum(row.aum)));
    tr.appendChild(td(fmtOverlap(row.overlap_with_screen, row.overlap_count)));
    var flagCell = td(row.flag, { cls: "l" });
    tr.appendChild(appendRemoveButton(flagCell, row.ticker));
    return tr;
  }

  function renderExcludedRow(row) {
    var tr = document.createElement("tr");
    tr.appendChild(tickerCell(row.ticker));
    var reasonCell = td(humanizeReason(row.flag), { cls: "l" });
    tr.appendChild(appendRemoveButton(reasonCell, row.ticker));
    return tr;
  }

  function renderScreen(data) {
    els.equitiesBody.innerHTML = "";
    els.etfBody.innerHTML = "";
    els.excludedBody.innerHTML = "";

    data.equities.forEach(function (r) { els.equitiesBody.appendChild(renderEquitiesRow(r)); });
    data.etfs.forEach(function (r) { els.etfBody.appendChild(renderEtfRow(r)); });
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

  function resetSearchConfirm() {
    currentSearchTicker = null;
    els.searchConfirm.classList.add("hidden");
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
            els.searchConfirm.classList.remove("hidden");
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
      openAnalyze(currentSearchTicker);
    }
  });

  els.searchAnalyzeBtn.addEventListener("click", function () {
    if (currentSearchTicker) openAnalyze(currentSearchTicker);
  });

  els.searchAddBtn.addEventListener("click", function () {
    if (!currentSearchTicker) return;
    var type = document.querySelector('input[name="add-type"]:checked').value;
    fetch("/api/watchlist/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: currentSearchTicker, type: type }),
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
  });

  // ---- refresh button ----

  els.refreshBtn.addEventListener("click", runScreen);

  // ---- boot ----

  runScreen();
})();
