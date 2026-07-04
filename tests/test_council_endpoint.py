"""
test_council_endpoint.py — tests for GET/POST /api/council/{ticker}
(app/main.py), Tier 3's spend-gated web boundary.

Mirrors tests/test_app.py's TestFlagsEndpoint / TestFlagsExplicitTriggerGate
/ TestFlagsUsageLogging patterns exactly: every EDGAR/model call is mocked,
nothing here makes a real network call. `client` fixture, `_filing_sections`
and `_model_response`-style helpers are re-declared locally (small, and
keeps this file independent of test_app.py's internals).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.watchlist as watchlist_mod
from app.main import app as fastapi_app
from engine import flags as FLAGS
from engine.edgar import CompanyData, Fact
from engine.filings import FilingSections
from engine.market import Quote
from engine.pipeline import AnalysisResult


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    quote = Quote("NVDA", price=None, shares_outstanding=None, market_cap=None, source="test")
    res = AnalysisResult(company=cd, quote=quote)
    res.gaps = []
    return res


def _block(text: str, type_="text"):
    b = MagicMock()
    b.type = type_
    b.text = text
    return b


def _response(text: str, input_tokens=100, output_tokens=50, stop_reason="end_turn"):
    r = MagicMock()
    r.content = [_block(text)]
    r.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    r.stop_reason = stop_reason
    return r


_ADVISOR_NAMES = ("BEAR_ADVOCATE", "BULL_STEELMAN", "ASSUMPTION_AUDITOR", "BASE_RATE_OUTSIDER", "EXECUTION_REALIST")
_VALID_POSITIONS = {
    "BEAR_ADVOCATE": "AVOID", "BULL_STEELMAN": "ACCUMULATE", "ASSUMPTION_AUDITOR": "HOLD",
    "BASE_RATE_OUTSIDER": "TRIM", "EXECUTION_REALIST": "INSUFFICIENT EVIDENCE",
}


def _round1_text() -> str:
    parts = ["### EVIDENCE_INTEGRITY_NOTE", "No known anomalies."]
    for name in _ADVISOR_NAMES:
        parts.append(f"### {name}")
        parts.append(f"Evidence-backed body.\nPOSITION: {_VALID_POSITIONS[name]}\nAGAINST: x\nCONFIDENCE: 3")
    return "\n\n".join(parts)


def _chairman_text() -> str:
    return "\n\n".join([
        "### VERDICT", "VERDICT: HOLD\nCONFIDENCE: 3\nUp: x. Down: y.",
        "### CONTRADICTION_LEDGER", "None.",
        "### THESIS_JOURNAL_DELTA", "n/a.",
        "### ACTION_ITEMS", "1. Watch next filing.",
        "### RISK_REGISTER", "1. Risk (BEAR_ADVOCATE).",
        "### DISSENT", "None.",
    ])


def _seven_call_responses() -> list:
    return [_response(_round1_text())] + [_response(f"review {i}") for i in range(5)] + [_response(_chairman_text())]


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


@pytest.fixture(autouse=True)
def _cfg_pricing(client):
    """The shipped config.yaml pins claude-sonnet-5 but doesn't price it —
    give every test here a real pricing entry so cost/estimate assertions
    aren't all forced to check for None."""
    fastapi_app.state.cfg.setdefault("flags", {})["pricing"] = {
        fastapi_app.state.cfg["flags"]["model"]: {"input_per_million": 2.0, "output_per_million": 10.0},
    }


# ---------------------------------------------------------------------------
# GET /api/council/{ticker} — never spends
# ---------------------------------------------------------------------------

class TestGetCouncil:
    def test_flags_not_cached_returns_blocked_no_flags_no_model_call(self, client, tmp_path):
        fs = _filing_sections()
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", tmp_path / "flags"), \
             patch("app.main._COUNCIL_CACHE_DIR", tmp_path / "council"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs):
            resp = client.get("/api/council/NVDA")
        assert resp.status_code == 200
        assert resp.json()["state"] == "blocked_no_flags"
        fastapi_app.state.anthropic_client.messages.create.assert_not_called()

    def test_cache_miss_with_flags_cached_returns_estimate_no_model_call(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache = tmp_path / "flags"
        _seed_flags_cache(flags_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", tmp_path / "council"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main._get_analysis_result", return_value=_analysis_result()):
            resp = client.get("/api/council/NVDA")
        body = resp.json()
        assert body["state"] == "not_cached"
        assert body["calls"] == 7
        assert body["estimated_cost_usd"] > 0
        assert body["evidence_status"] == {"quant": "ok", "flags": "cached", "thesis": "pre_thesis"}
        fastapi_app.state.anthropic_client.messages.create.assert_not_called()

    def test_no_filing_found_returns_404(self, client, tmp_path):
        with patch("app.main._FLAGS_CACHE_DIR", tmp_path / "flags"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=None):
            resp = client.get("/api/council/SPY")
        assert resp.status_code == 404

    def test_cached_council_record_served_without_a_model_call(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        fastapi_app.state.anthropic_client.messages.create.side_effect = _seven_call_responses()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()):
            client.post("/api/council/NVDA?convene=true")
            fastapi_app.state.anthropic_client.messages.create.reset_mock()
            resp = client.get("/api/council/NVDA")
        assert resp.json()["state"] == "ok"
        assert resp.json()["cache_status"] == "cached"
        fastapi_app.state.anthropic_client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/council/{ticker}?convene=true — the only path that spends
# ---------------------------------------------------------------------------

class TestConveneCouncil:
    def test_bare_post_without_convene_true_is_rejected_no_model_call(self, client, tmp_path):
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", tmp_path / "flags"):
            resp = client.post("/api/council/NVDA")
        assert resp.status_code == 400
        fastapi_app.state.anthropic_client.messages.create.assert_not_called()

    def test_flags_not_cached_returns_409_no_model_call(self, client, tmp_path):
        fs = _filing_sections()
        fastapi_app.state.anthropic_client = MagicMock()
        with patch("app.main._FLAGS_CACHE_DIR", tmp_path / "flags"), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs):
            resp = client.post("/api/council/NVDA?convene=true")
        assert resp.status_code == 409
        fastapi_app.state.anthropic_client.messages.create.assert_not_called()

    def test_no_api_key_returns_503(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache = tmp_path / "flags"
        _seed_flags_cache(flags_cache)
        fastapi_app.state.anthropic_client = None
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs):
            resp = client.post("/api/council/NVDA?convene=true")
        assert resp.status_code == 503

    def test_runs_exactly_seven_calls_in_order_and_caches(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        fastapi_app.state.anthropic_client.messages.create.side_effect = _seven_call_responses()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()):
            resp = client.post("/api/council/NVDA?convene=true")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "ok"
        assert body["cache_status"] == "live"
        assert len(body["advisors"]) == 5
        assert len(body["reviews"]) == 5
        assert body["chairman"]["verdict"] == "HOLD"
        assert fastapi_app.state.anthropic_client.messages.create.call_count == 7

    def test_writes_seven_usage_rows_with_correct_call_types(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        fastapi_app.state.anthropic_client.messages.create.side_effect = _seven_call_responses()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()):
            client.post("/api/council/NVDA?convene=true")

        import app.usage as usage_mod
        conn = usage_mod._connect(usage_mod._resolve(None))
        try:
            rows = conn.execute(
                "SELECT ticker, call_type, cache_status FROM flag_extraction_usage ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 7
        assert all(r[0] == "NVDA" for r in rows)
        assert [r[1] for r in rows] == (
            ["council_opinions"] + ["council_review"] * 5 + ["council_chairman"]
        )
        assert all(r[2] == "live" for r in rows)

    def test_second_convene_without_refresh_is_a_cache_hit_zero_new_rows(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        fastapi_app.state.anthropic_client.messages.create.side_effect = _seven_call_responses()
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()):
            client.post("/api/council/NVDA?convene=true")
            resp2 = client.post("/api/council/NVDA?convene=true")
        assert resp2.json()["cache_status"] == "from_cache"
        assert fastapi_app.state.anthropic_client.messages.create.call_count == 7  # not 14

        import app.usage as usage_mod
        conn = usage_mod._connect(usage_mod._resolve(None))
        try:
            count = conn.execute("SELECT COUNT(*) FROM flag_extraction_usage").fetchone()[0]
        finally:
            conn.close()
        assert count == 7  # no new rows from the cache-hit convene

    def test_failed_call_mid_run_logs_partial_usage_returns_502_nothing_cached(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        fastapi_app.state.anthropic_client.messages.create.side_effect = [
            _response(_round1_text()), _response("review 0"), RuntimeError("SDK exploded"),
        ]
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()):
            resp = client.post("/api/council/NVDA?convene=true")
            assert resp.status_code == 502
            assert "SDK exploded" in resp.json()["detail"]

            # nothing cached — the very next GET must still see not_cached
            get_resp = client.get("/api/council/NVDA")
        assert get_resp.json()["state"] == "not_cached"

        import app.usage as usage_mod
        conn = usage_mod._connect(usage_mod._resolve(None))
        try:
            rows = conn.execute("SELECT call_type FROM flag_extraction_usage ORDER BY id").fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == ["council_opinions", "council_review"]  # 2 completed calls logged, not 3rd

    def test_refresh_true_reconvenes_and_logs_seven_more_rows(self, client, tmp_path):
        fs = _filing_sections()
        flags_cache, council_cache = tmp_path / "flags", tmp_path / "council"
        _seed_flags_cache(flags_cache)
        fastapi_app.state.anthropic_client = MagicMock()
        fastapi_app.state.anthropic_client.messages.create.side_effect = (
            _seven_call_responses() + _seven_call_responses()
        )
        with patch("app.main._FLAGS_CACHE_DIR", flags_cache), \
             patch("app.main._COUNCIL_CACHE_DIR", council_cache), \
             patch.object(fastapi_app.state.filings_client, "latest_10k_sections", return_value=fs), \
             patch("app.main.run_single_ticker", return_value=_analysis_result()):
            client.post("/api/council/NVDA?convene=true")
            resp2 = client.post("/api/council/NVDA?convene=true&refresh=true")
        assert resp2.json()["cache_status"] == "live"
        assert fastapi_app.state.anthropic_client.messages.create.call_count == 14

        import app.usage as usage_mod
        conn = usage_mod._connect(usage_mod._resolve(None))
        try:
            count = conn.execute("SELECT COUNT(*) FROM flag_extraction_usage").fetchone()[0]
        finally:
            conn.close()
        assert count == 14
