"""
test_frontend_accordion.py — DOM-level check that no accordion section
renders open, using a real tag/attribute parser executed in Node (not a
substring/regex grep) against the actual server-rendered fragment HTML.

Context: engine/report_html.py's `open_=True` was removed from the
Financial position section (commit 4d0277b), but a live server kept
showing it open anyway. Investigation traced that to a stale running
uvicorn process that had loaded main's pre-fix code before this branch
was checked out — not a code defect (verified by spinning up an isolated
server on this branch and confirming zero open sections in the real
served HTML). This test locks in the fix at two levels so a REAL
regression — HTML or JS — would be caught structurally:

1. A real tag/attribute parse of the actual rendered fragment HTML (not
   a raw substring search — it won't false-positive on an unrelated
   attribute whose name or value happens to contain "open").
2. A structural guarantee that nothing in app.js's row-expand path
   (toggleAccordion, or anywhere else) programmatically opens a section.

No new dependency (jsdom, etc.): Node has no built-in DOM, and this
project's frontend has no build step / node_modules, so the parser here
is a small, dependency-free tag tokenizer, not a full DOM implementation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import report_html as RH
from tests.test_report_html import _company_result

_APP_JS = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text()

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available in this environment")

_PARSE_DETAILS_OPEN_STATE_SRC = r"""
function parseDetailsOpenState(html) {
  // Walks every <details ...> opening tag and returns an array of
  // booleans: true if that tag carries the boolean `open` attribute
  // (per HTML semantics, presence alone means open -- `open`, `open=""`,
  // and `open="open"` are all open; absence means closed). A real
  // tag/attribute parse, not a substring search.
  var results = [];
  var tagRe = /<details\b([^>]*)>/g;
  var tagMatch;
  while ((tagMatch = tagRe.exec(html)) !== null) {
    var attrs = tagMatch[1];
    var attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(=("[^"]*"|'[^']*'|[^\s>]+))?/g;
    var isOpen = false;
    var attrMatch;
    while ((attrMatch = attrRe.exec(attrs)) !== null) {
      if (attrMatch[1] === "open") { isOpen = true; break; }
    }
    results.push(isOpen);
  }
  return results;
}
"""


def _parse_open_states(html: str) -> list:
    script = f"""
{_PARSE_DETAILS_OPEN_STATE_SRC}
console.log(JSON.stringify(parseDetailsOpenState({json.dumps(html)})));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestNoDetailsOpenOnRowExpand:
    def test_equity_fragment_has_no_open_details_by_a_real_tag_parse(self):
        frag = RH.render_fragment(_company_result())
        states = _parse_open_states(frag)
        # Financial position, Growth, Margins & returns, Data gaps, Flags
        # at minimum (Latest quarter is absent for this fixture — no
        # quarterly data — same count floor test_report_html.py itself uses).
        assert len(states) >= 4
        assert all(state is False for state in states), states

    def test_parser_correctly_detects_an_open_section_when_present(self):
        """Sanity check on the parser itself — if it can't detect a
        genuinely open section, the assertion above proves nothing."""
        html = (
            '<details class="report-section" open><summary>X</summary></details>'
            '<details class="report-section"><summary>Y</summary></details>'
        )
        assert _parse_open_states(html) == [True, False]

    def test_parser_handles_open_with_an_explicit_value(self):
        """`open=""` and `open="open"` are both the open state per HTML
        boolean-attribute rules — not just the bare `open` form."""
        assert _parse_open_states('<details open=""><summary>X</summary></details>') == [True]
        assert _parse_open_states('<details open="open"><summary>X</summary></details>') == [True]

    def test_parser_does_not_false_positive_on_an_unrelated_attribute(self):
        """A future `data-open-label="..."` or similar must not be
        mistaken for the boolean `open` attribute — this is exactly what
        a raw substring grep would get wrong."""
        html = '<details class="report-section" data-open-label="foo"><summary>X</summary></details>'
        assert _parse_open_states(html) == [False]


class TestNoJavaScriptForcesASectionOpen:
    """Structural guarantee on top of the DOM-level check above: nothing
    in the accordion's expand path programmatically opens a section."""

    def _extract_function(self, name: str) -> str:
        start = _APP_JS.index(f"function {name}(")
        depth = 0
        i = _APP_JS.index("{", start)
        body_start = i
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

    def test_toggle_accordion_never_sets_open_programmatically(self):
        body = self._extract_function("toggleAccordion")
        assert ".open = true" not in body
        assert ".open=true" not in body
        assert 'setAttribute("open"' not in body
        assert "setAttribute('open'" not in body
        assert ".click()" not in body

    def test_no_open_forcing_pattern_anywhere_in_app_js(self):
        assert ".open = true" not in _APP_JS
        assert ".open=true" not in _APP_JS
        assert 'setAttribute("open"' not in _APP_JS
        assert "setAttribute('open'" not in _APP_JS
