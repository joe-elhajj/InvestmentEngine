"""
test_frontend_council.py — covers the Tier 3 Council accordion's spend gate.

Spec requirement: clicking "Consult council" (or "Re-consult council") must
never fire the paid endpoint directly — it opens an inline confirm chip
(matching the remove-row pattern: no viewport-bottom bars), and only the
explicit "Confirm & consult" click may trigger the actual call
(loadCouncil(), which is what performs the GET/POST to
/api/council/{ticker} — covered separately by tests/test_council_endpoint.py
against the real FastAPI app). This file isolates consultControl() itself
and stubs loadCouncil() as a spy, so what's under test is exactly the
gating contract: no confirm, no call.

No jsdom, matching this repo's established pattern (see
test_frontend_remove.py): a small hand-rolled dependency-free DOM stub.
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


_CONSULT_CONTROL_SRC = _extract_function("consultControl")

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


def test_consult_control_found_in_app_js():
    assert "function consultControl(ticker, details, flagsAvailable, isReconsult)" in _CONSULT_CONTROL_SRC


class TestConsultConfirmGate:
    def _harness(self, flags_available="true", is_reconsult="false") -> str:
        return f"""
{_DOM_STUB}
{_CONSULT_CONTROL_SRC}

var closeOpenCouncilConfirm = null;
var loadCouncilCalls = [];
function loadCouncil(details, query) {{ loadCouncilCalls.push(query); }}

var details = {{ dataset: {{ ticker: "NVDA" }} }};
var wrap = consultControl("NVDA", details, {flags_available}, {is_reconsult});
"""

    def test_initial_state_shows_trigger_not_confirm(self):
        script = self._harness() + """
console.log(JSON.stringify({ triggerText: wrap.children[0].textContent, childCount: wrap.children.length }));
"""
        result = _run(script)
        assert result["childCount"] == 1
        assert result["triggerText"] == "Consult council"

    def test_clicking_trigger_shows_confirm_without_calling_load_council(self):
        script = self._harness() + """
var trigger = wrap.children[0];
trigger.dispatch("click");
console.log(JSON.stringify({
  confirmClass: wrap.children[0].className,
  loadCouncilCalls: loadCouncilCalls,
}));
"""
        result = _run(script)
        assert result["confirmClass"] == "council-confirm"
        assert result["loadCouncilCalls"] == []

    def test_clicking_cancel_reverts_to_trigger_without_calling_load_council(self):
        script = self._harness() + """
var trigger = wrap.children[0];
trigger.dispatch("click");
var confirmChip = wrap.children[0];
var no = confirmChip.children[1].children[1];
no.dispatch("click");
console.log(JSON.stringify({
  backToTrigger: wrap.children[0] === trigger,
  loadCouncilCalls: loadCouncilCalls,
}));
"""
        result = _run(script)
        assert result["backToTrigger"] is True
        assert result["loadCouncilCalls"] == []

    def test_clicking_confirm_calls_load_council_with_convene_true(self):
        script = self._harness() + """
var trigger = wrap.children[0];
trigger.dispatch("click");
var confirmChip = wrap.children[0];
var yes = confirmChip.children[1].children[0];
yes.dispatch("click");
console.log(JSON.stringify({ loadCouncilCalls: loadCouncilCalls }));
"""
        result = _run(script)
        assert result["loadCouncilCalls"] == ["convene=true"]

    def test_reconsult_confirm_calls_load_council_with_refresh_too(self):
        script = self._harness(is_reconsult="true") + """
var trigger = wrap.children[0];
trigger.dispatch("click");
var confirmChip = wrap.children[0];
var yes = confirmChip.children[1].children[0];
yes.dispatch("click");
console.log(JSON.stringify({ loadCouncilCalls: loadCouncilCalls, triggerText: trigger.textContent }));
"""
        result = _run(script)
        assert result["loadCouncilCalls"] == ["convene=true&refresh=true"]
        assert result["triggerText"] == "Re-consult council"

    def test_flags_unavailable_confirm_copy_mentions_test_mode(self):
        script = self._harness(flags_available="false") + """
var trigger = wrap.children[0];
trigger.dispatch("click");
var confirmChip = wrap.children[0];
console.log(JSON.stringify({ labelText: confirmChip.children[0].textContent }));
"""
        result = _run(script)
        assert "test mode" in result["labelText"]

    def test_flags_available_confirm_copy_does_not_mention_test_mode(self):
        script = self._harness(flags_available="true") + """
var trigger = wrap.children[0];
trigger.dispatch("click");
var confirmChip = wrap.children[0];
console.log(JSON.stringify({ labelText: confirmChip.children[0].textContent }));
"""
        result = _run(script)
        assert "test mode" not in result["labelText"]
