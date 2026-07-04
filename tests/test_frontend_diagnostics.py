"""
test_frontend_diagnostics.py — executes app.js's diagnosticsRowNeeded()
directly in Node.js.

diagnosticsRowNeeded() takes no DOM/global references (just a plain row
object), so it can be extracted from app.js by brace-matching and run
standalone in a Node subprocess — real coverage of the production
exceptions-only filter (Task 3), not a re-implementation of it in Python.

Skipped (not failed) if Node isn't available in the environment; Node
ships on GitHub-hosted Actions runners by default, so this should still
run in CI.
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
    return _APP_JS[body_start:i]


_ROW_NEEDED_SRC = _extract_function("diagnosticsRowNeeded")


def _needed(row: dict) -> bool:
    script = f"""
{_ROW_NEEDED_SRC}
console.log(JSON.stringify(diagnosticsRowNeeded({json.dumps(row)})));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _row(**overrides) -> dict:
    base = {"completeness": 1.0, "is_stable": True, "flag": ""}
    base.update(overrides)
    return base


def test_function_found_in_app_js():
    assert "function diagnosticsRowNeeded(row)" in _ROW_NEEDED_SRC


def test_fully_clean_row_is_omitted():
    """100% complete, stable, note-free — the exact case Task 3 says
    must vanish entirely."""
    assert _needed(_row()) is False


def test_incomplete_row_is_included():
    assert _needed(_row(completeness=0.8)) is True


def test_unstable_row_is_included():
    assert _needed(_row(is_stable=False)) is True


def test_row_with_a_note_is_included():
    assert _needed(_row(flag="n/a — Reverse-DCF: normalized free cash flow is zero or negative...")) is True


def test_null_completeness_is_not_treated_as_full():
    """absence-is-not-zero: an unknown completeness must not be silently
    treated as a clean 100% — it should still surface."""
    assert _needed(_row(completeness=None)) is True


def test_null_stable_alone_does_not_force_inclusion():
    """Only an EXPLICIT stable=false triggers inclusion on its own — an
    unknown (null) stability flag, with completeness=100% and no note,
    must not by itself force a row into the exceptions table."""
    assert _needed(_row(is_stable=None)) is False


def test_empty_string_flag_is_not_treated_as_a_note():
    assert _needed(_row(flag="")) is False
