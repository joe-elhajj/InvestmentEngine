"""
test_frontend_remove.py — covers the remove-from-watchlist bug fix.

Bug (reported): click × -> a confirm bar at the bottom of the viewport ->
clicking Remove does nothing visible -> only a full page reload shows the
row gone. Root cause was NOT a lost DOM reference — it was the success
handler calling runScreen(), which kicks off a brand-new /api/screen
background job (polled every 3s, ~60s for a full watchlist) before the
table visibly changed at all. Fixed by removing the row (and splicing the
underlying sortable data array) directly on a successful DELETE, with no
re-screen involved, and replacing the viewport-bottom confirm bar with an
inline "Remove? [check] / [x]" affordance inside the row itself.

Two levels, matching this repo's established pattern (see
test_frontend_accordion.py): a real DOM-level exercise of the actual
extracted removeControl()/checkEmptyWatchlist() functions against a
minimal, dependency-free node/event stub (click -> confirm -> click yes ->
row gone, exactly the reported repro but in reverse), plus a structural
guarantee that the regression's actual root cause (routing removal
through runScreen()) can't silently come back.

No jsdom: this project's frontend has no build step / node_modules, so
the DOM here is a small hand-rolled stub — just enough createElement/
appendChild/classList/addEventListener/remove() to drive the real
production functions, not a re-implementation of their logic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_APP_JS = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text()

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available in this environment")


def _extract_function(name: str) -> str:
    """Brace-matches a top-level `function <name>(...) { ... }` out of app.js."""
    start = _APP_JS.index(f"function {name}(")
    depth = 0
    i = _APP_JS.index("{", start)
    while True:
        if _APP_JS[i] == "{":
            depth += 1
        elif _APP_JS[i] == "}":
            depth -= 1
            if depth == 0:
                return _APP_JS[start:i + 1]
        i += 1
        if i >= len(_APP_JS):
            raise AssertionError(f"unbalanced braces extracting {name}() from app.js")


_REMOVE_CONTROL_SRC = _extract_function("removeControl")
_CHECK_EMPTY_SRC = _extract_function("checkEmptyWatchlist")

# Minimal, dependency-free DOM stub — only what removeControl()/
# checkEmptyWatchlist() actually touch: createElement, className,
# textContent/title/type/disabled, classList add/remove/contains,
# appendChild/remove(), innerHTML-as-clear, addEventListener + a
# `dispatch` helper standing in for a real click event.
_DOM_STUB = r"""
function makeNode(tag) {
  var node = {
    tagName: tag, className: "", textContent: "", title: "", type: "",
    disabled: false, children: [], parentNode: null, _handlers: {},
    style: { setProperty: function () {} },
  };
  node.classList = {
    _set: {},
    add: function (c) { this._set[c] = true; },
    remove: function (c) { delete this._set[c]; },
    toggle: function (c, f) { if (f === undefined) this._set[c] = !this._set[c]; else this._set[c] = f; },
    contains: function (c) { return !!this._set[c]; },
  };
  node.appendChild = function (child) { child.parentNode = node; node.children.push(child); return child; };
  node.remove = function () {
    if (node.parentNode) {
      var idx = node.parentNode.children.indexOf(node);
      if (idx !== -1) node.parentNode.children.splice(idx, 1);
      node.parentNode = null;
    }
  };
  node.addEventListener = function (type, fn) { (node._handlers[type] = node._handlers[type] || []).push(fn); };
  node.dispatch = function (type) {
    (node._handlers[type] || []).forEach(function (fn) { fn({ stopPropagation: function () {} }); });
  };
  Object.defineProperty(node, "innerHTML", {
    get: function () { return ""; },
    set: function () { node.children = []; },
  });
  return node;
}
global.document = { createElement: function (tag) { return makeNode(tag); } };
"""


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_remove_control_and_check_empty_watchlist_found_in_app_js():
    assert "function removeControl(ticker, tr, dataArray, sectionEl, tableBodyEl, onRemoved)" in _REMOVE_CONTROL_SRC
    assert "function checkEmptyWatchlist()" in _CHECK_EMPTY_SRC


class TestClickRemoveConfirmRowGone:
    """The actual reported repro, exercised end to end: click the trigger,
    confirm, and assert the row is gone from its parent — all before any
    reload/re-fetch would ever happen."""

    def _harness(self, extra_setup: str = "", fetch_ok: bool = True) -> str:
        return f"""
{_DOM_STUB}
{_REMOVE_CONTROL_SRC}
{_CHECK_EMPTY_SRC}

var expandedRows = {{}};
var bannerCalls = [];
function showBanner(text, isError) {{ bannerCalls.push({{ text: text, isError: isError }}); }}
var closeOpenRemoveConfirm = null;

var equitiesBody = makeNode("tbody");
var etfBody = makeNode("tbody");
var excludedBody = makeNode("tbody");
var emptyState = makeNode("div");
emptyState.classList.add("hidden");
var metaLine = makeNode("p");
var els = {{ equitiesBody: equitiesBody, etfBody: etfBody, excludedBody: excludedBody,
             emptyState: emptyState, metaLine: metaLine,
             equitiesSection: makeNode("section"), etfSection: makeNode("section"),
             excludedSection: makeNode("section") }};

var dataArray = [{{ ticker: "AAPL" }}, {{ ticker: "MSFT" }}];
var tr = makeNode("tr");
equitiesBody.appendChild(tr);
var sectionEl = makeNode("section");
{extra_setup}

global.fetch = function (url, opts) {{
  global.__lastFetchUrl = url;
  global.__lastFetchOpts = opts;
  return Promise.resolve({{ ok: {str(fetch_ok).lower()}, status: {200 if fetch_ok else 500} }});
}};

var wrap = removeControl("AAPL", tr, dataArray, sectionEl, equitiesBody);
var trigger = wrap.children[0];
trigger.dispatch("click");
var confirmChip = wrap.children[0];
var yes = confirmChip.children[1];
yes.dispatch("click");

setTimeout(function () {{
  console.log(JSON.stringify({{
    trHasParent: tr.parentNode !== null,
    dataArrayTickers: dataArray.map(function (r) {{ return r.ticker; }}),
    fetchUrl: global.__lastFetchUrl,
    fetchMethod: global.__lastFetchOpts.method,
    bannerCalls: bannerCalls,
    wrapShowsTrigger: wrap.children[0] === trigger,
  }}));
}}, 20);
"""

    def test_trigger_click_shows_inline_confirm_not_a_bottom_bar(self):
        # Standalone (not derived from the full harness): just trigger's
        # click, then inspect the wrap — before "yes" is ever clicked —
        # to prove the confirm affordance is inline in the row itself,
        # not a separate viewport-fixed element (there's no such element
        # anywhere in this script for it to reach for).
        script = f"""
{_DOM_STUB}
{_REMOVE_CONTROL_SRC}
{_CHECK_EMPTY_SRC}
var expandedRows = {{}};
function showBanner() {{}}
var closeOpenRemoveConfirm = null;
var els = {{ equitiesBody: makeNode("tbody"), etfBody: makeNode("tbody"),
             excludedBody: makeNode("tbody"), emptyState: makeNode("div"), metaLine: makeNode("p") }};
var tr = makeNode("tr");
var wrap = removeControl("AAPL", tr, [{{ ticker: "AAPL" }}], makeNode("section"), makeNode("tbody"));
var trigger = wrap.children[0];
trigger.dispatch("click");
var confirmChip = wrap.children[0];
console.log(JSON.stringify({{
  confirmChipClass: confirmChip.className,
  labelText: confirmChip.children[0].textContent,
}}));
"""
        result_line = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
        assert result_line.returncode == 0, result_line.stderr
        data = json.loads(result_line.stdout.strip().splitlines()[-1])
        assert data["confirmChipClass"] == "row-remove-confirm"
        assert data["labelText"] == "Remove?"

    def test_confirmed_removal_detaches_the_row_from_its_parent(self):
        result = _run(self._harness())
        assert result["trHasParent"] is False

    def test_confirmed_removal_splices_the_ticker_out_of_the_data_array(self):
        """The array renderEquitiesBody()/renderEtfBody() rebuild the
        tbody FROM on every sort — if this isn't spliced too, the very
        next sort click resurrects the "removed" row."""
        result = _run(self._harness())
        assert result["dataArrayTickers"] == ["MSFT"]

    def test_removal_calls_the_real_delete_endpoint(self):
        result = _run(self._harness())
        assert result["fetchUrl"] == "/api/watchlist/AAPL"
        assert result["fetchMethod"] == "DELETE"

    def test_no_error_banner_on_success(self):
        result = _run(self._harness())
        assert result["bannerCalls"] == []

    def test_failed_delete_shows_banner_and_reverts_to_trigger_not_removed(self):
        result = _run(self._harness(fetch_ok=False))
        assert result["trHasParent"] is True
        assert result["dataArrayTickers"] == ["AAPL", "MSFT"]
        assert len(result["bannerCalls"]) == 1
        assert result["bannerCalls"][0]["isError"] is True
        assert "AAPL" in result["bannerCalls"][0]["text"]

    def test_an_open_accordion_row_for_the_removed_ticker_is_also_dropped(self):
        extra = 'var accRow = makeNode("tr"); equitiesBody.appendChild(accRow); expandedRows["AAPL"] = accRow;'
        script = self._harness(extra_setup=extra)
        script = script.replace(
            "trHasParent: tr.parentNode !== null,",
            "trHasParent: tr.parentNode !== null,\n    accordionRowGone: !expandedRows.hasOwnProperty(\"AAPL\"),",
        )
        result = _run(script)
        assert result["accordionRowGone"] is True


class TestCheckEmptyWatchlist:
    def _harness(self, equities_children, etf_children, excluded_children) -> str:
        def fill(count):
            return "\n".join(f'body.appendChild(makeNode("tr"));' for _ in range(count))

        return f"""
{_DOM_STUB}
{_CHECK_EMPTY_SRC}
var equitiesBody = makeNode("tbody"); (function(body){{ {fill(equities_children)} }})(equitiesBody);
var etfBody = makeNode("tbody"); (function(body){{ {fill(etf_children)} }})(etfBody);
var excludedBody = makeNode("tbody"); (function(body){{ {fill(excluded_children)} }})(excludedBody);
var emptyState = makeNode("div"); emptyState.classList.add("hidden");
var metaLine = makeNode("p");
var els = {{ equitiesBody: equitiesBody, etfBody: etfBody, excludedBody: excludedBody,
             emptyState: emptyState, metaLine: metaLine,
             equitiesSection: makeNode("section"), etfSection: makeNode("section"),
             excludedSection: makeNode("section") }};
checkEmptyWatchlist();
console.log(JSON.stringify({{
  emptyStateHidden: emptyState.classList.contains("hidden"),
  metaLineHidden: metaLine.classList.contains("hidden"),
}}));
"""

    def test_all_three_tables_empty_shows_empty_state(self):
        result = _run(self._harness(0, 0, 0))
        assert result["emptyStateHidden"] is False
        assert result["metaLineHidden"] is True

    def test_one_remaining_row_anywhere_keeps_empty_state_hidden(self):
        result = _run(self._harness(1, 0, 0))
        assert result["emptyStateHidden"] is True
        assert result["metaLineHidden"] is False

        result = _run(self._harness(0, 1, 0))
        assert result["emptyStateHidden"] is True

        result = _run(self._harness(0, 0, 1))
        assert result["emptyStateHidden"] is True


class TestRemovalNeverRoutesThroughAFullRescreen:
    """Structural guarantee on top of the behavioral tests above: the
    actual root cause (the success handler calling runScreen(), a slow
    full re-screen, instead of updating the DOM directly) can't silently
    come back."""

    def test_remove_control_never_calls_run_screen(self):
        assert "runScreen()" not in _REMOVE_CONTROL_SRC

    def test_remove_control_removes_the_row_directly(self):
        assert "tr.remove()" in _REMOVE_CONTROL_SRC

    def test_remove_control_splices_the_data_array_directly(self):
        assert "dataArray.splice" in _REMOVE_CONTROL_SRC

    def test_no_viewport_bottom_confirm_bar_markup_referenced(self):
        """Bug 3: the old #remove-confirm bottom bar element is gone, not
        just unused — app.js has no remaining reference to its id (the
        new inline chip's own class, row-remove-confirm, legitimately
        contains "remove-confirm" as a substring, so this checks the old
        element-id lookups specifically, not a blanket substring)."""
        assert 'getElementById("remove-confirm")' not in _APP_JS
        assert "getElementById('remove-confirm')" not in _APP_JS
        assert "pendingRemoveTicker" not in _APP_JS
