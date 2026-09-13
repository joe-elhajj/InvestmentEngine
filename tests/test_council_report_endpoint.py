"""
test_council_report_endpoint.py — tests for GET /api/council/{ticker}/report.html
and GET /api/council/{ticker}/report.pdf (app/main.py), the Step 3 access
surface for engine/report_council.py + app/pdf.py.

Every EDGAR/model/subprocess-touching call is mocked, matching
test_council_endpoint.py's own convention — nothing here makes a real
network call, spawns a real Chrome process, or spends a real dollar.
Critically, these tests assert the ANTHROPIC client is never invoked
(rendering reads the council/flags caches only) and that PDF generation
(app.pdf.html_to_pdf, mocked here) is called at most once per distinct
report — a second request must be served from the on-disk report cache.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.watchlist as watchlist_mod
from app.main import app as fastapi_app
from app.pdf import PdfGenerationError
from engine.council import AdvisorOpinion, ChairmanOutput, CouncilMeta, CouncilResult, ReviewNote, _save_cached
from engine.edgar import CompanyData, Fact
from engine.filings import FilingSections
from engine.market import Quote
from engine.pipeline import AnalysisResult


@pytest.fixture
def client(tmp_path, monkeypatch, app_test_config):
    monkeypatch.setattr(watchlist_mod, "DB_PATH", tmp_path / "watchlist.db")
    with TestClient(fastapi_app) as c:
        yield c


def _filing_sections(accession="0000320193-24-000123") -> FilingSections:
    return FilingSections(
        ticker="NVDA", cik="0001045810", accession=accession,
        form="10-K", filed="2024-02-21", period_ending="2024-01-28",
        url="https://example.com/nvda10k.htm",
        sections={"1A": "Our competitor XYZ Corp filed a lawsuit against us in March 2024."},
    )


def _analysis_result() -> AnalysisResult:
    cd = CompanyData(
        ticker="NVDA", cik="0001045810", name="NVIDIA Corp",
        sic="3674", sic_description="Semiconductors", recent_forms=["10-K"],
    )
    cd.series = {
        "revenue": [Fact("revenue", 100.0, "2026-09-30", 2026, "us-gaap:Revenues", "10-K", "2026-11-01", "USD")],
    }
    quote = Quote("NVDA", price=123.45, shares_outstanding=1000.0, market_cap=123450.0, source="test")
    res = AnalysisResult(company=cd, quote=quote)
    res.gaps = []
    return res


def _seed_flags_cache(cache_dir, accession="0000320193-24-000123", model="claude-sonnet-5", prompt_version="v1"):
    from engine.flags import Flag, FilingRef, FlagsResult, _save_cached_raw
    result = FlagsResult(
        ticker="NVDA", model=model, prompt_version=prompt_version,
        extracted_at="2026-01-01T00:00:00+00:00",
        filing=FilingRef(form="10-K", accession=accession, period_ending="2024-01-28",
                          filed="2024-02-21", url="https://example.com/nvda10k.htm"),
        flags=[Flag(label="Customer concentration", snippet="one customer is 19% of revenue",
                     severity="red", item="1A", verified_verbatim=True)],
        dropped_count=0,
    )
    _save_cached_raw(cache_dir, accession, prompt_version, model, result)


def _seed_council_cache(cache_dir, accession="0000320193-24-000123", model="claude-sonnet-5", prompt_version="v1"):
    meta = CouncilMeta(
        ticker="NVDA", model=model, prompt_version=prompt_version, config_hash="abc123",
        convened_at="2026-01-01T00:00:00+00:00", accession=accession,
        thesis_status="pre_thesis", thesis_hash=None, evidence_integrity_note="none",
        total_cost_usd=0.5, total_input_tokens=1000, total_output_tokens=500,
        status_flags=[], calls=[],
    )
    chairman = ChairmanOutput(
        verdict="HOLD", confidence=3, text="### VERDICT\nVERDICT: HOLD\nCONFIDENCE: 3\nUp: x. Down: y.",
        sections={"contradiction_ledger": "1. None. **OPEN**", "thesis_journal_delta": "n/a",
                   "action_items": "1. **Owner: User.** Watch.", "risk_register": "1. Risk.", "dissent": "None."},
        parsed=True,
    )
    result = CouncilResult(
        ticker="NVDA",
        advisors=[AdvisorOpinion(name="BEAR_ADVOCATE", position="AVOID", confidence=3,
                                   text="Body.\nPOSITION: AVOID\nAGAINST: x\nCONFIDENCE: 3", parsed=True)],
        reviews=[ReviewNote(reviewer="BEAR_ADVOCATE", text="review text")],
        chairman=chairman, meta=meta,
    )
    _save_cached(cache_dir, accession, "prethesis", prompt_version, model, result)
    return result


@pytest.fixture(autouse=True)
def _cfg_pricing(client):
    fastapi_app.state.cfg.setdefault("flags", {})["pricing"] = {
        fastapi_app.state.cfg["flags"]["model"]: {"input_per_million": 2.0, "output_per_million": 10.0},
    }


class TestReportHtmlEndpoint:
    def test_no_council_result_returns_404(self, client, tmp_path):
        fs = _filing_sections()
        with patch("app.main._FLAGS_CACHE_DIR", tmp_path / "flags"), \
             patch("app.main._COUNCIL_CACHE_DIR", tmp_path / "council"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs):
            resp = client.get("/api/council/NVDA/report.html")
        assert resp.status_code == 404
        assert "Convene first" in resp.json()["detail"]

    def test_cached_council_result_renders_200_html(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        _seed_council_cache(council_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch("app.main._COUNCIL_REPORTS_CACHE_DIR", tmp_path / "reports"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()):
            resp = client.get("/api/council/NVDA/report.html")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert ">NVDA<" in resp.text
        assert "HOLD" in resp.text
        fastapi_app.state.anthropic_client.messages.create.assert_not_called()

    def test_rendering_never_calls_the_anthropic_client(self, client, tmp_path):
        """Zero new LLM calls anywhere — rendering reads the council and
        flags caches only, never engine.flags._call_model or a council
        convene."""
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        _seed_council_cache(council_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch("app.main._COUNCIL_REPORTS_CACHE_DIR", tmp_path / "reports"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()), \
             patch("engine.flags._call_model") as mock_flags_call:
            resp = client.get("/api/council/NVDA/report.html")
        assert resp.status_code == 200
        mock_flags_call.assert_not_called()
        fastapi_app.state.anthropic_client.messages.create.assert_not_called()

    def test_second_request_is_served_from_the_report_cache(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        reports_cache = tmp_path / "reports"
        _seed_flags_cache(flags_cache)
        _seed_council_cache(council_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch("app.main._COUNCIL_REPORTS_CACHE_DIR", reports_cache), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()), \
             patch("app.main.RC.render", wraps=__import__("engine.report_council", fromlist=["render"]).render) as mock_render:
            r1 = client.get("/api/council/NVDA/report.html")
            r2 = client.get("/api/council/NVDA/report.html")
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.text == r2.text
        mock_render.assert_called_once()


class TestReportPdfEndpoint:
    def test_no_council_result_returns_404(self, client, tmp_path):
        fs = _filing_sections()
        with patch("app.main._FLAGS_CACHE_DIR", tmp_path / "flags"), \
             patch("app.main._COUNCIL_CACHE_DIR", tmp_path / "council"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs):
            resp = client.get("/api/council/NVDA/report.pdf")
        assert resp.status_code == 404

    def test_cached_council_result_generates_pdf(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        _seed_council_cache(council_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch("app.main._COUNCIL_REPORTS_CACHE_DIR", tmp_path / "reports"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()), \
             patch("app.main.PDF.html_to_pdf", return_value=b"%PDF-1.4 fake pdf bytes") as mock_pdf:
            resp = client.get("/api/council/NVDA/report.pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == b"%PDF-1.4 fake pdf bytes"
        mock_pdf.assert_called_once()
        fastapi_app.state.anthropic_client.messages.create.assert_not_called()

    def test_second_pdf_request_is_served_from_cache_no_regeneration(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        reports_cache = tmp_path / "reports"
        _seed_flags_cache(flags_cache)
        _seed_council_cache(council_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch("app.main._COUNCIL_REPORTS_CACHE_DIR", reports_cache), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()), \
             patch("app.main.PDF.html_to_pdf", return_value=b"%PDF-1.4 fake pdf bytes") as mock_pdf:
            r1 = client.get("/api/council/NVDA/report.pdf")
            r2 = client.get("/api/council/NVDA/report.pdf")
        assert r1.content == r2.content == b"%PDF-1.4 fake pdf bytes"
        mock_pdf.assert_called_once()

    def test_pdf_generation_failure_returns_502_not_a_fake_success(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        _seed_council_cache(council_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch("app.main._COUNCIL_REPORTS_CACHE_DIR", tmp_path / "reports"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()), \
             patch("app.main.PDF.html_to_pdf", side_effect=PdfGenerationError("chrome not found")):
            resp = client.get("/api/council/NVDA/report.pdf")
        assert resp.status_code == 502
        assert "chrome not found" in resp.json()["detail"]

    def test_pdf_generation_never_calls_the_anthropic_client_or_flags_model(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        _seed_council_cache(council_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch("app.main._COUNCIL_REPORTS_CACHE_DIR", tmp_path / "reports"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()), \
             patch("app.main.PDF.html_to_pdf", return_value=b"%PDF-1.4") as mock_pdf, \
             patch("engine.flags._call_model") as mock_flags_call:
            resp = client.get("/api/council/NVDA/report.pdf")
        assert resp.status_code == 200
        mock_flags_call.assert_not_called()
        fastapi_app.state.anthropic_client.messages.create.assert_not_called()
        mock_pdf.assert_called_once()
