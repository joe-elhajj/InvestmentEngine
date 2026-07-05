"""
test_frontend_accordion_reflow.py — Bug 1 (Phase 1, dark-instrument-redesign
branch): expanded accordion panels didn't occupy real layout space, so rows
below rendered on top of an open panel instead of reflowing down (META over
NVDA's open Financial Position; Excluded over SPY's open Profile).

Root cause: expandAccordionContent() measured .accordion-content's
scrollHeight once at open time and pinned max-height to that exact pixel
value forever — the table's row-height bookkeeping for that <tr> then
tracked a frozen number instead of the panel's actual current content, so
anything that changed the panel's real height afterward (the real fragment
replacing the "Loading…" placeholder, a nested <details> toggle, the Sources
reveal) left the row's layout height out of sync with what was rendered,
which is what let it overlap the next row instead of reflowing.

Fix: once the open transition's transitionend fires, release max-height to
"none" so ordinary browser auto-height table layout governs the row from
then on — no pixel value left to go stale. collapseAccordionRow() cancels a
still-pending release so it can't fire mid-collapse and clobber the close
animation.

No real browser/layout engine is available in this environment (no
Chromium/Playwright/Selenium, and this repo's own convention is a
dependency-free Node DOM stub, not jsdom — see test_frontend_remove.py) — so
this cannot assert real pixel offsets ("the next row's bounding box starts
below the panel's bottom edge"). What it CAN and does assert, against the
actual production functions: the exact mechanism that guarantees that
outcome — the row's height constraint is released back to "none"
(unconstrained/auto) once the open transition completes, and a collapse
started before that release fires cancels it cleanly rather than racing it.
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


_EXPAND_SRC = _extract_function("expandAccordionContent")
_COLLAPSE_SRC = _extract_function("collapseAccordionRow")

_STUB = r"""
function makeNode(tag) {
  var node = { tagName: tag, style: {}, scrollHeight: 0, _handlers: {}, _removed: false };
  node.querySelector = function (sel) {
    return sel === ".accordion-content" ? (node._innerRef || null) : null;
  };
  node.addEventListener = function (type, fn) { (node._handlers[type] = node._handlers[type] || []).push(fn); };
  node.removeEventListener = function (type, fn) {
    var arr = node._handlers[type] || [];
    var idx = arr.indexOf(fn);
    if (idx !== -1) arr.splice(idx, 1);
  };
  node.dispatch = function (type, detail) {
    var ev = detail || {};
    (node._handlers[type] || []).slice().forEach(function (fn) { fn(ev); });
  };
  node.remove = function () { node._removed = true; };
  return node;
}
global.document = { createElement: function (tag) { return makeNode(tag); } };
global.__rafQueue = [];
global.requestAnimationFrame = function (cb) { global.__rafQueue.push(cb); };
function flushRaf() { var q = global.__rafQueue; global.__rafQueue = []; q.forEach(function (cb) { cb(); }); }
global.window = { setTimeout: function () {} };
"""


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_expand_and_collapse_functions_found_in_app_js():
    assert "function expandAccordionContent(inner)" in _EXPAND_SRC
    assert "function collapseAccordionRow(accRow)" in _COLLAPSE_SRC


class TestOpenReleasesTheHeightConstraint:
    def test_max_height_pinned_to_scroll_height_during_the_open_transition(self):
        script = f"""
{_STUB}
{_EXPAND_SRC}
var inner = makeNode("div");
inner.scrollHeight = 480;
expandAccordionContent(inner);
flushRaf();
console.log(JSON.stringify({{ maxHeight: inner.style.maxHeight, opacity: inner.style.opacity }}));
"""
        result = _run(script)
        assert result["maxHeight"] == "480px"
        assert result["opacity"] == "1"

    def test_max_height_released_to_none_once_the_open_transition_ends(self):
        """The actual fix: after transitionend fires for max-height, the
        row's height constraint is released — from then on the table's
        native auto-height layout governs this row, so it can never be
        stuck at a stale pixel value while sibling rows below assume a
        different (smaller) box for it."""
        script = f"""
{_STUB}
{_EXPAND_SRC}
var inner = makeNode("div");
inner.scrollHeight = 480;
expandAccordionContent(inner);
flushRaf();
inner.dispatch("transitionend", {{ propertyName: "max-height" }});
console.log(JSON.stringify({{ maxHeight: inner.style.maxHeight }}));
"""
        result = _run(script)
        assert result["maxHeight"] == "none"

    def test_unrelated_transitionend_property_does_not_release_early(self):
        script = f"""
{_STUB}
{_EXPAND_SRC}
var inner = makeNode("div");
inner.scrollHeight = 480;
expandAccordionContent(inner);
flushRaf();
inner.dispatch("transitionend", {{ propertyName: "opacity" }});
console.log(JSON.stringify({{ maxHeight: inner.style.maxHeight }}));
"""
        result = _run(script)
        assert result["maxHeight"] == "480px"

    def test_a_second_expand_call_cancels_a_stale_pending_release(self):
        """Mirrors the real cache-miss sequence: the "Loading…" placeholder
        opens first (queuing a release), then the real fragment replaces it
        and opens again before the first transition ever ends. The first
        (stale) handler must not fire after the second call takes over."""
        script = f"""
{_STUB}
{_EXPAND_SRC}
var inner = makeNode("div");
inner.scrollHeight = 40; // "Loading…" placeholder height
expandAccordionContent(inner);
flushRaf();
inner.scrollHeight = 900; // real fragment content, taller
expandAccordionContent(inner);
flushRaf();
var handlerCountAfterSecondCall = (inner._handlers["transitionend"] || []).length;
inner.dispatch("transitionend", {{ propertyName: "max-height" }});
console.log(JSON.stringify({{
  maxHeightBeforeEnd: "900px",
  handlerCountAfterSecondCall: handlerCountAfterSecondCall,
  maxHeightAfterEnd: inner.style.maxHeight,
}}));
"""
        result = _run(script)
        assert result["handlerCountAfterSecondCall"] == 1
        assert result["maxHeightAfterEnd"] == "none"


class TestCollapseCancelsAPendingOpenRelease:
    def test_collapsing_before_open_release_fires_still_removes_the_row(self):
        script = f"""
{_STUB}
{_EXPAND_SRC}
{_COLLAPSE_SRC}
var accRow = makeNode("tr");
var inner = makeNode("div");
accRow._innerRef = inner;
inner.scrollHeight = 300;
expandAccordionContent(inner);
flushRaf(); // max-height now "300px", release-to-none still pending

collapseAccordionRow(accRow);
flushRaf(); // collapse's own RAF sets max-height to "0px"
inner.dispatch("transitionend", {{ propertyName: "max-height" }});
console.log(JSON.stringify({{ maxHeight: inner.style.maxHeight, removed: accRow._removed }}));
"""
        result = _run(script)
        # The pending open-release handler was cancelled — a single
        # max-height transitionend now resolves to the COLLAPSE's own
        # handler (row removed), not a stray "none" from the open path.
        assert result["maxHeight"] == "0px"
        assert result["removed"] is True

    def test_no_accordion_content_child_removes_the_row_immediately(self):
        script = f"""
{_STUB}
{_COLLAPSE_SRC}
var accRow = makeNode("tr");
collapseAccordionRow(accRow);
console.log(JSON.stringify({{ removed: accRow._removed }}));
"""
        result = _run(script)
        assert result["removed"] is True
