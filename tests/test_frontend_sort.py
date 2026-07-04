"""
test_frontend_sort.py — executes app.js's sortRows() directly in Node.js.

sortRows() takes no DOM/global references (just rows/key/dir/valueOf), so
it can be extracted from app.js by brace-matching and run standalone in a
Node subprocess — real coverage of the production sort algorithm itself,
not just a re-implementation of it in Python. This is the test for the
absence-is-not-zero invariant applied to sorting (CLAUDE.md): a null/
undefined value must sort to the bottom in BOTH directions, never
coerced to 0 or -Infinity.

Skipped (not failed) if Node isn't available in the environment; Node
ships on GitHub-hosted Actions runners by default, so this should still
run in CI.
"""

from __future__ import annotations

import json
import re
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


_SORT_ROWS_SRC = _extract_function("sortRows")


def _run_sort(rows, key, dir_, value_expr):
    """value_expr is a JS expression string for valueOf(row, key), e.g. 'row.v'."""
    script = f"""
{_SORT_ROWS_SRC}
var rows = {json.dumps(rows)};
var result = sortRows(rows, {json.dumps(key)}, {json.dumps(dir_)}, function(row, key) {{
  return {value_expr};
}});
console.log(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_ROWS = [
    {"id": "A", "v": 10},
    {"id": "B", "v": None},
    {"id": "C", "v": 30},
    {"id": "D", "v": None},
    {"id": "E", "v": 20},
]


def test_sort_rows_function_found_in_app_js():
    assert "function sortRows(rows, key, dir, valueOf)" in _SORT_ROWS_SRC


def test_na_rows_sort_last_in_descending_order():
    result = _run_sort(_ROWS, "v", "desc", "row.v")
    assert [r["id"] for r in result] == ["C", "E", "A", "B", "D"]


def test_na_rows_sort_last_in_ascending_order():
    """The invariant this test locks in: n/a is NEVER coerced to 0 or
    -Infinity, so it doesn't jump to the front just because ascending
    order would otherwise put small numbers first."""
    result = _run_sort(_ROWS, "v", "asc", "row.v")
    assert [r["id"] for r in result] == ["A", "E", "C", "B", "D"]


def test_na_values_keep_original_relative_order():
    result = _run_sort(_ROWS, "v", "desc", "row.v")
    na_ids = [r["id"] for r in result if r["v"] is None]
    assert na_ids == ["B", "D"]


def test_all_na_column_is_a_stable_noop():
    rows = [{"id": "A", "v": None}, {"id": "B", "v": None}, {"id": "C", "v": None}]
    result = _run_sort(rows, "v", "desc", "row.v")
    assert [r["id"] for r in result] == ["A", "B", "C"]


def test_no_key_returns_original_order_unchanged():
    """dir/key cleared (the 3rd click state) must restore default order."""
    result = _run_sort(_ROWS, None, None, "row.v")
    assert [r["id"] for r in result] == ["A", "B", "C", "D", "E"]


def test_alpha_sort_uses_string_compare_not_numeric_coercion():
    rows = [{"id": "1", "t": "beta"}, {"id": "2", "t": "alpha"}, {"id": "3", "t": None}]
    result = _run_sort(rows, "t", "asc", "row.t")
    assert [r["id"] for r in result] == ["2", "1", "3"]
