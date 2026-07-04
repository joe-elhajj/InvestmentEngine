"""
test_frontend_flags_error.py — executes app.js's extractErrorMessage()
directly in Node.js.

/api/flags/{ticker}'s `detail` field is either a plain string (404/503 —
"No 10-K filing found", "Anthropic API key not configured") or the richer
structured envelope app/main.py's _describe_exception() builds for an
actual SDK/extraction failure ({state, error_type, message,
sdk_status_code, sdk_body}). extractErrorMessage() is the one place that
has to handle both shapes without ever rendering "[object Object]" — a
real risk once the detail became an object instead of always a string.
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


_SRC = _extract_function("extractErrorMessage")


def _run(detail, fallback="HTTP 502"):
    script = f"""
{_SRC}
console.log(JSON.stringify(extractErrorMessage({json.dumps(detail)}, {json.dumps(fallback)})));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_function_found_in_app_js():
    assert "function extractErrorMessage(detail, fallback)" in _SRC


def test_plain_string_detail_passes_through():
    assert _run("No 10-K filing found for SPY.") == "No 10-K filing found for SPY."


def test_structured_object_detail_extracts_message_field():
    detail = {
        "state": "error",
        "error_type": "RuntimeError",
        "message": "connection reset by peer",
        "sdk_status_code": None,
        "sdk_body": None,
    }
    assert _run(detail) == "connection reset by peer"


def test_never_renders_object_object_for_a_message_less_object():
    """If a future error shape has no `message` field, fall back to the
    generic HTTP status text rather than stringifying the raw object."""
    assert _run({"foo": "bar"}) == "HTTP 502"


def test_null_or_missing_detail_falls_back():
    assert _run(None) == "HTTP 502"
