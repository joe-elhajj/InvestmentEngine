"""
test_usage.py — tests for app/usage.py, the Tier 2 spend ledger.

Every test uses an explicit tmp_path db_path (never the real
~/.investment_engine/watchlist.db) and an explicit `now` passed to
summary() for deterministic month/year bucketing — no test depends on
the wall-clock date it happens to run on.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import usage


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "usage_test.db"


def _log(db_path, ticker="NVDA", model="claude-sonnet-5", prompt_version="v1",
          input_tokens=1000, output_tokens=100, cost_usd=0.01, cache_status="live",
          call_type="flags", called_at=None):
    usage.log_call(
        ticker=ticker, model=model, prompt_version=prompt_version,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost_usd, cache_status=cache_status, call_type=call_type, db_path=db_path,
    )
    if called_at is not None:
        # log_call always stamps "now" internally — tests that need a
        # specific historical called_at rewrite the just-inserted row's
        # timestamp directly rather than adding a test-only parameter to
        # the production log_call() signature.
        conn = usage._connect(db_path)
        try:
            conn.execute(
                "UPDATE flag_extraction_usage SET called_at = ? WHERE id = (SELECT MAX(id) FROM flag_extraction_usage)",
                (called_at,),
            )
            conn.commit()
        finally:
            conn.close()


class TestLogCall:
    def test_writes_a_row_with_all_fields(self, db_path):
        _log(db_path, ticker="nvda", model="claude-sonnet-5", prompt_version="v1",
             input_tokens=1234, output_tokens=56, cost_usd=0.054, cache_status="live")
        conn = usage._connect(db_path)
        try:
            row = conn.execute(
                "SELECT ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, cache_status "
                "FROM flag_extraction_usage"
            ).fetchone()
        finally:
            conn.close()
        # ticker is upper-cased on write, same discipline as watchlist.add()
        assert row == ("NVDA", "claude-sonnet-5", "v1", 1234, 56, 0.054, "live")

    def test_multiple_calls_accumulate_rows(self, db_path):
        _log(db_path)
        _log(db_path)
        _log(db_path)
        conn = usage._connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM flag_extraction_usage").fetchone()[0]
        finally:
            conn.close()
        assert count == 3


class TestSummary:
    def test_empty_db_reports_all_zeros(self, db_path):
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        assert result["lifetime_total_usd"] == 0.0
        assert result["current_month"] == {"month": "2026-07", "cost_usd": 0.0, "calls": 0}
        assert result["current_year"] == {"year": 2026, "cost_usd": 0.0, "calls": 0}
        assert result["trailing_12mo_projection_usd"] == 0.0
        assert len(result["monthly_breakdown"]) == 12
        assert all(m["cost_usd"] == 0.0 and m["calls"] == 0 for m in result["monthly_breakdown"])
        assert result["per_model"] == []

    def test_lifetime_total_sums_all_billed_calls(self, db_path):
        _log(db_path, cost_usd=1.5, cache_status="live", called_at="2026-01-15T00:00:00+00:00")
        _log(db_path, cost_usd=2.5, cache_status="refresh", called_at="2026-03-01T00:00:00+00:00")
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        assert result["lifetime_total_usd"] == 4.0

    def test_from_cache_rows_never_counted_as_billed(self, db_path):
        """cache hits are logged for audit (cost 0) but must not inflate
        any cost or call total in the summary."""
        _log(db_path, cost_usd=0.0, cache_status="from_cache", called_at="2026-07-01T00:00:00+00:00")
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        assert result["lifetime_total_usd"] == 0.0
        assert result["current_month"]["calls"] == 0
        assert result["monthly_breakdown"][0] == {"month": "2026-07", "cost_usd": 0.0, "calls": 0}

    def test_current_month_only_counts_that_month(self, db_path):
        _log(db_path, cost_usd=1.0, called_at="2026-07-01T00:00:00+00:00")
        _log(db_path, cost_usd=5.0, called_at="2026-06-30T23:59:59+00:00")
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        assert result["current_month"] == {"month": "2026-07", "cost_usd": 1.0, "calls": 1}

    def test_current_year_aggregates_across_months(self, db_path):
        _log(db_path, cost_usd=1.0, called_at="2026-01-05T00:00:00+00:00")
        _log(db_path, cost_usd=2.0, called_at="2026-06-15T00:00:00+00:00")
        _log(db_path, cost_usd=3.0, called_at="2025-12-31T00:00:00+00:00")  # prior year
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        assert result["current_year"] == {"year": 2026, "cost_usd": 3.0, "calls": 2}

    def test_monthly_bucketing_correct_across_a_year_boundary(self, db_path):
        _log(db_path, cost_usd=1.0, called_at="2025-12-20T00:00:00+00:00")
        _log(db_path, cost_usd=2.0, called_at="2026-01-05T00:00:00+00:00")
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        by_month = {m["month"]: m for m in result["monthly_breakdown"]}
        assert by_month["2025-12"] == {"month": "2025-12", "cost_usd": 1.0, "calls": 1}
        assert by_month["2026-01"] == {"month": "2026-01", "cost_usd": 2.0, "calls": 1}
        # the two months must be genuinely distinct buckets, not merged
        assert by_month["2025-12"] != by_month["2026-01"]

    def test_monthly_breakdown_spans_last_12_calendar_months_including_empty_ones(self, db_path):
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        months = [m["month"] for m in result["monthly_breakdown"]]
        assert months[0] == "2026-07"
        assert "2025-08" in months
        assert len(months) == len(set(months)) == 12

    def test_trailing_12mo_projection_is_trailing_30d_times_12(self, db_path):
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        _log(db_path, cost_usd=3.0, called_at="2026-06-20T00:00:00+00:00")   # within 30d
        _log(db_path, cost_usd=100.0, called_at="2026-01-01T00:00:00+00:00")  # outside 30d
        result = usage.summary(db_path=db_path, now=now)
        assert result["trailing_12mo_projection_usd"] == pytest.approx(3.0 * 12)

    def test_per_model_breakdown_groups_correctly(self, db_path):
        _log(db_path, model="claude-sonnet-4-6", cost_usd=1.0, called_at="2026-07-01T00:00:00+00:00")
        _log(db_path, model="claude-sonnet-4-6", cost_usd=2.0, called_at="2026-06-01T00:00:00+00:00")
        _log(db_path, model="claude-haiku-4-5-20251001", cost_usd=0.5, called_at="2026-05-01T00:00:00+00:00")
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        by_model = {m["model"]: m for m in result["per_model"]}
        assert by_model["claude-sonnet-4-6"] == {"model": "claude-sonnet-4-6", "cost_usd": 3.0, "calls": 2}
        assert by_model["claude-haiku-4-5-20251001"] == {"model": "claude-haiku-4-5-20251001", "cost_usd": 0.5, "calls": 1}

    def test_per_call_type_breakdown_groups_correctly(self, db_path):
        _log(db_path, cost_usd=1.0, cache_status="live", call_type="flags", called_at="2026-07-01T00:00:00+00:00")
        _log(db_path, cost_usd=2.0, cache_status="live", call_type="council_opinions", called_at="2026-07-01T00:00:00+00:00")
        _log(db_path, cost_usd=0.5, cache_status="live", call_type="council_review", called_at="2026-07-01T00:00:00+00:00")
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        by_type = {t["call_type"]: t for t in result["per_call_type"]}
        assert by_type["flags"] == {"call_type": "flags", "cost_usd": 1.0, "calls": 1}
        assert by_type["council_opinions"] == {"call_type": "council_opinions", "cost_usd": 2.0, "calls": 1}
        assert by_type["council_review"] == {"call_type": "council_review", "cost_usd": 0.5, "calls": 1}


# ---------------------------------------------------------------------------
# Null-cost (pricing_unknown) rows — see engine.flags.compute_cost_usd /
# engine.council.compute_council_cost_usd
# ---------------------------------------------------------------------------

class TestUnknownPricingRows:
    def test_null_cost_row_excluded_from_lifetime_total(self, db_path):
        _log(db_path, cost_usd=None, cache_status="live", called_at="2026-07-01T00:00:00+00:00")
        _log(db_path, cost_usd=2.0, cache_status="live", called_at="2026-07-02T00:00:00+00:00")
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        assert result["lifetime_total_usd"] == 2.0

    def test_null_cost_row_counted_in_rows_with_unknown_pricing(self, db_path):
        _log(db_path, cost_usd=None, cache_status="live", called_at="2026-07-01T00:00:00+00:00")
        _log(db_path, cost_usd=None, cache_status="refresh", called_at="2026-07-02T00:00:00+00:00")
        _log(db_path, cost_usd=1.0, cache_status="live", called_at="2026-07-03T00:00:00+00:00")
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        assert result["rows_with_unknown_pricing"] == 2

    def test_null_cost_row_still_counts_as_a_call(self, db_path):
        """A pricing_unknown call is a real, billed model call — it must
        still count toward "calls", just not toward any cost sum."""
        _log(db_path, cost_usd=None, cache_status="live", called_at="2026-07-01T00:00:00+00:00")
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        assert result["current_month"]["calls"] == 1
        assert result["current_year"]["calls"] == 1
        assert result["monthly_breakdown"][0]["calls"] == 1
        assert result["per_model"][0]["calls"] == 1

    def test_null_cost_row_excluded_from_monthly_and_per_model_sums(self, db_path):
        _log(db_path, model="claude-sonnet-5", cost_usd=None, called_at="2026-07-01T00:00:00+00:00")
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        result = usage.summary(db_path=db_path, now=now)
        assert result["monthly_breakdown"][0]["cost_usd"] == 0.0
        assert result["per_model"][0]["cost_usd"] == 0.0

    def test_empty_db_reports_zero_unknown_pricing_rows(self, db_path):
        now = datetime(2026, 7, 4, tzinfo=timezone.utc)
        assert usage.summary(db_path=db_path, now=now)["rows_with_unknown_pricing"] == 0


# ---------------------------------------------------------------------------
# Schema migration — an existing DB may predate nullable cost_usd, the
# call_type column, both, or neither. _migrate() must handle all four
# starting states idempotently: it must never assume which (if any) prior
# migration already ran against a given on-disk database.
# ---------------------------------------------------------------------------

class TestCostUsdNullableMigration:
    def test_migrates_pre_existing_not_null_schema(self, db_path):
        """Simulates a DB created before pricing_unknown rows existed
        (cost_usd NOT NULL) — the real ~/.investment_engine/watchlist.db
        may already be in this state. New code must open it without
        raising, preserve the existing row, and successfully write a new
        NULL-cost row."""
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE flag_extraction_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL, called_at TEXT NOT NULL, cache_status TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO flag_extraction_usage "
            "(ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, called_at, cache_status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("OLD", "claude-sonnet-5", "v1", 100, 10, 0.01, "2026-01-01T00:00:00+00:00", "live"),
        )
        conn.commit()
        conn.close()

        # Must not raise an IntegrityError on the old NOT NULL constraint.
        usage.log_call(
            ticker="NEW", model="claude-sonnet-5", prompt_version="v1",
            input_tokens=100, output_tokens=10, cost_usd=None, cache_status="live", db_path=db_path,
        )

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT ticker, cost_usd FROM flag_extraction_usage ORDER BY id").fetchall()
        finally:
            conn.close()
        assert rows == [("OLD", 0.01), ("NEW", None)]

    def test_migration_is_idempotent_on_an_already_nullable_table(self, db_path):
        _log(db_path, cost_usd=None)
        _log(db_path, cost_usd=1.0)  # second _connect() call must be a no-op migration
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM flag_extraction_usage").fetchone()[0]
        finally:
            conn.close()
        assert count == 2


class TestMigration:
    """A table created by an older version of this module (cost_usd NOT
    NULL, no call_type column) must upgrade in place — existing rows
    preserved, never dropped or reset. Also covers the intermediate state
    a real deployment can be in TODAY: cost_usd already nullable (from
    TestCostUsdNullableMigration's fix, above) but call_type still absent
    — _migrate() must not assume its own rebuild path is the only one that
    ever ran against a given database."""

    def _old_schema_db(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE flag_extraction_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                called_at TEXT NOT NULL,
                cache_status TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO flag_extraction_usage "
            "(ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, called_at, cache_status) "
            "VALUES ('NVDA', 'claude-sonnet-5', 'v1', 1000, 100, 0.05, '2026-01-01T00:00:00+00:00', 'live')"
        )
        conn.commit()
        conn.close()

    def _nullable_no_call_type_db(self, db_path):
        """The state a real database is in immediately after
        TestCostUsdNullableMigration's fix ran but before this call_type
        migration existed: cost_usd already nullable, call_type absent."""
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE flag_extraction_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL,
                called_at TEXT NOT NULL,
                cache_status TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO flag_extraction_usage "
            "(ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, called_at, cache_status) "
            "VALUES ('NVDA', 'claude-sonnet-5', 'v1', 1000, 100, NULL, '2026-01-01T00:00:00+00:00', 'live')"
        )
        conn.commit()
        conn.close()

    def test_call_type_column_added_with_flags_default(self, db_path):
        self._old_schema_db(db_path)
        conn = usage._connect(db_path)  # triggers _migrate()
        try:
            row = conn.execute("SELECT ticker, call_type FROM flag_extraction_usage").fetchone()
        finally:
            conn.close()
        assert row == ("NVDA", "flags")

    def test_existing_row_data_fully_preserved(self, db_path):
        self._old_schema_db(db_path)
        conn = usage._connect(db_path)
        try:
            row = conn.execute(
                "SELECT ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, called_at, cache_status "
                "FROM flag_extraction_usage"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("NVDA", "claude-sonnet-5", "v1", 1000, 100, 0.05, "2026-01-01T00:00:00+00:00", "live")

    def test_cost_usd_is_nullable_after_migration(self, db_path):
        self._old_schema_db(db_path)
        conn = usage._connect(db_path)
        try:
            conn.execute(
                "INSERT INTO flag_extraction_usage "
                "(ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, called_at, cache_status, call_type) "
                "VALUES ('AAPL', 'claude-sonnet-5', 'v1', 500, 50, NULL, '2026-02-01T00:00:00+00:00', 'live', 'flags')"
            )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM flag_extraction_usage WHERE cost_usd IS NULL").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_a_brand_new_db_needs_no_migration_and_already_has_call_type(self, db_path):
        # _connect() on a path with no existing file just creates the
        # current schema directly — _migrate() should be a clean no-op.
        conn = usage._connect(db_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(flag_extraction_usage)").fetchall()}
        finally:
            conn.close()
        assert "call_type" in cols

    def test_log_call_works_immediately_after_migration(self, db_path):
        self._old_schema_db(db_path)
        usage.log_call(
            ticker="MSFT", model="claude-sonnet-5", prompt_version="v1",
            input_tokens=200, output_tokens=20, cost_usd=0.01, cache_status="live",
            call_type="council_chairman", db_path=db_path,
        )
        conn = usage._connect(db_path)
        try:
            rows = conn.execute("SELECT ticker, call_type FROM flag_extraction_usage ORDER BY id").fetchall()
        finally:
            conn.close()
        assert rows == [("NVDA", "flags"), ("MSFT", "council_chairman")]

    def test_already_nullable_db_missing_only_call_type_upgrades_via_add_column_not_rebuild(self, db_path):
        """The exact scenario Task 2 is worried about: a real deployment
        where TestCostUsdNullableMigration's fix already ran (cost_usd
        nullable) but call_type didn't exist yet. Must add the column
        in place — never assume a full rebuild is required, and never
        assume this is the FIRST migration this database has ever seen."""
        self._nullable_no_call_type_db(db_path)
        conn = usage._connect(db_path)
        try:
            row = conn.execute("SELECT ticker, cost_usd, call_type FROM flag_extraction_usage").fetchone()
        finally:
            conn.close()
        assert row == ("NVDA", None, "flags")
