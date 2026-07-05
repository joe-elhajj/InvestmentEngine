"""
pdf.py — html_to_pdf(html) -> bytes, isolated behind one small function so
the PDF mechanism can be swapped later without touching any caller.

Mechanism chosen: headless Chrome's own --print-to-pdf CLI flag, over
WeasyPrint. Reasoning:
  - Zero new Python dependency. Chrome is already installed on this
    machine (the dashboard's own "Save as PDF" button already relies on
    it, via window.print()) — WeasyPrint would be a new pip package that
    ALSO drags in system-level dependencies (Pango, Cairo, GDK-PixBuf,
    typically via Homebrew) not otherwise needed by this project at all.
  - CSS fidelity. Chrome's print path is a full, current browser
    rendering engine — flexbox, modern CSS selectors, @media print,
    break-inside — exactly what engine/report_council.py's hand-written
    stylesheet uses. WeasyPrint's CSS support is a deliberately-scoped
    subset (partial/older flexbox, no CSS Grid) that's less predictable
    for a stylesheet not specifically written against its limitations.

Free, idempotent, no network access, no LLM/extraction/council call: this
only ever converts an ALREADY-RENDERED HTML string into bytes. It never
reads the council cache, the flags cache, or anything else on its own —
the caller (app/main.py) is responsible for producing the HTML first.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


class PdfGenerationError(RuntimeError):
    """Raised on any failure — never returns bytes that merely look like a PDF."""


def _find_chrome() -> str:
    for candidate in _CHROME_CANDIDATES:
        if candidate.startswith("/"):
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    raise PdfGenerationError(
        "No Chrome/Chromium binary found — checked: " + ", ".join(_CHROME_CANDIDATES)
    )


def html_to_pdf(html: str, timeout: float = 30.0) -> bytes:
    """Renders a self-contained HTML string to PDF bytes via headless
    Chrome. Raises PdfGenerationError (never a silent empty/garbage
    result) if Chrome can't be found, exits non-zero, times out, or
    produces no output file."""
    chrome = _find_chrome()
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "report.html"
        pdf_path = Path(tmp) / "report.pdf"
        html_path.write_text(html, encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf_path}",
                    html_path.as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise PdfGenerationError(f"Chrome print-to-pdf timed out after {timeout}s") from e
        if proc.returncode != 0 or not pdf_path.exists():
            raise PdfGenerationError(
                f"Chrome print-to-pdf failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}"
            )
        return pdf_path.read_bytes()
