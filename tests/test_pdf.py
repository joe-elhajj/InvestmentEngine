"""
test_pdf.py — tests for app/pdf.py's html_to_pdf(), the isolated PDF
mechanism used by GET /api/council/{ticker}/report.pdf.

Every subprocess call here is mocked — no real Chrome invocation, matching
this repo's convention of never touching a real external process/network
call in the test suite.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app import pdf as PDF


class TestFindChrome:
    def test_finds_the_hardcoded_macos_path_when_it_exists(self):
        with patch("app.pdf.Path.exists", return_value=True):
            found = PDF._find_chrome()
        assert found == PDF._CHROME_CANDIDATES[0]

    def test_raises_when_nothing_is_found(self):
        with patch("app.pdf.Path.exists", return_value=False), \
             patch("app.pdf.shutil.which", return_value=None):
            with pytest.raises(PDF.PdfGenerationError):
                PDF._find_chrome()


class TestHtmlToPdf:
    def test_success_returns_the_written_pdf_bytes(self, tmp_path):
        fake_pdf_bytes = b"%PDF-1.4 fake"

        def fake_run(cmd, **kwargs):
            # The real implementation is told to write to a path passed
            # via --print-to-pdf=<path> — honor that so read-back works.
            pdf_arg = next(a for a in cmd if a.startswith("--print-to-pdf="))
            out_path = pdf_arg.split("=", 1)[1]
            with open(out_path, "wb") as f:
                f.write(fake_pdf_bytes)
            return MagicMock(returncode=0, stderr="")

        with patch("app.pdf._find_chrome", return_value="/fake/chrome"), \
             patch("app.pdf.subprocess.run", side_effect=fake_run):
            result = PDF.html_to_pdf("<html><body>hi</body></html>")
        assert result == fake_pdf_bytes

    def test_nonzero_exit_raises_pdf_generation_error(self):
        with patch("app.pdf._find_chrome", return_value="/fake/chrome"), \
             patch("app.pdf.subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")):
            with pytest.raises(PDF.PdfGenerationError, match="boom"):
                PDF.html_to_pdf("<html></html>")

    def test_missing_output_file_raises_even_on_zero_exit(self):
        """A real observed failure mode: Chrome exits 0 but writes nothing
        (e.g. a bad URL) — must not be treated as success."""
        with patch("app.pdf._find_chrome", return_value="/fake/chrome"), \
             patch("app.pdf.subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
            with pytest.raises(PDF.PdfGenerationError):
                PDF.html_to_pdf("<html></html>")

    def test_timeout_raises_pdf_generation_error(self):
        with patch("app.pdf._find_chrome", return_value="/fake/chrome"), \
             patch("app.pdf.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="chrome", timeout=30)):
            with pytest.raises(PDF.PdfGenerationError, match="timed out"):
                PDF.html_to_pdf("<html></html>", timeout=30)

    def test_no_chrome_found_raises_before_ever_calling_subprocess(self):
        with patch("app.pdf._find_chrome", side_effect=PDF.PdfGenerationError("no chrome")):
            with pytest.raises(PDF.PdfGenerationError, match="no chrome"):
                PDF.html_to_pdf("<html></html>")
