"""
usage.py — SQLite-backed spend ledger for Tier 2 flag extraction calls.

Lives in the same ~/.investment_engine/watchlist.db as the watchlist
tables (app/watchlist.py) — one local database for this single-user tool,
not a reason to invent a second file.

log_call() writes exactly one row per request to /api/flags/{ticker} that
actually resolves to flag data — whether that meant a real paid model call
("live"/"refresh") or reusing an already-cached extraction ("from_cache").
Cache-hit rows are stamped cost_usd=0 and are never counted in the billed
totals summary() reports, but they stay in the table for an honest audit
trail of how often cached flags were viewed. A "not yet extracted" check
that never reaches an actual result (the placeholder state) writes nothing
at all — nothing happened yet to log.

Every live/refresh row's cost_usd is computed from config.yaml pricing AT
CALL TIME (engine.flags.compute_cost_usd) and stored as-is, so a later
price change in config.yaml never rewrites history.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app import watchlist

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flag_extraction_usage (
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
"""

_BILLABLE_STATUSES = ("live", "refresh")


def _resolve(db_path: Optional[Path]) -> Path:
    # Reads app.watchlist.DB_PATH at CALL TIME (via the module object, not
    # `from app.watchlist import DB_PATH`, which would bind the value once
    # at import time) — so tests that monkeypatch watchlist.DB_PATH to an
    # isolated tmp_path db (see test_app.py's `client` fixture) transparently
    # redirect this module's writes too. One db path constant, not two that
    # could silently drift apart.
    return db_path if db_path is not None else watchlist.DB_PATH


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    return conn


def log_call(
    ticker: str,
    model: str,
    prompt_version: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    cache_status: str,
    db_path: Optional[Path] = None,
) -> None:
    resolved = _resolve(db_path)
    conn = _connect(resolved)
    try:
        conn.execute(
            "INSERT INTO flag_extraction_usage "
            "(ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, called_at, cache_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker.upper(), model, prompt_version, input_tokens, output_tokens,
                cost_usd, datetime.now(timezone.utc).isoformat(), cache_status,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _trailing_months(now: datetime, count: int) -> list:
    """Most-recent-first list of the last `count` calendar-month keys,
    including the current month — used so monthly_breakdown always shows
    a full, contiguous 12-month table even for months with zero calls."""
    keys = []
    y, m = now.year, now.month
    for _ in range(count):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return keys


def summary(db_path: Optional[Path] = None, now: Optional[datetime] = None) -> dict:
    """
    Everything the /api/usage endpoint and the dashboard's Usage modal
    need. "calls"/"cost_usd" figures throughout only count billable rows
    (cache_status in "live"/"refresh") — a from_cache row costs 0 by
    construction, but it also shouldn't inflate a "calls" count that's
    meant to answer "how many times did I pay for this."

    trailing_12mo_projection_usd is an honest <trailing 30 days> x 12
    extrapolation, not a fitted model — the dashboard footnote spells out
    that assumption explicitly.
    """
    now = now or datetime.now(timezone.utc)
    resolved = _resolve(db_path)
    conn = _connect(resolved)
    try:
        rows = conn.execute(
            "SELECT model, cost_usd, called_at, cache_status FROM flag_extraction_usage"
        ).fetchall()
    finally:
        conn.close()

    billed = [(model, cost, called_at) for model, cost, called_at, status in rows if status in _BILLABLE_STATUSES]

    cur_month_key = _month_key(now)
    cur_year = now.year
    cutoff_30d = now - timedelta(days=30)

    lifetime_total = 0.0
    month_cost, month_calls = 0.0, 0
    year_cost, year_calls = 0.0, 0
    trailing_30d_cost = 0.0
    monthly: dict = {}   # "YYYY-MM" -> [cost, calls]
    per_model: dict = {}  # model -> [cost, calls]

    for model, cost, called_at in billed:
        dt = datetime.fromisoformat(called_at)
        lifetime_total += cost

        key = _month_key(dt)
        bucket = monthly.setdefault(key, [0.0, 0])
        bucket[0] += cost
        bucket[1] += 1

        model_bucket = per_model.setdefault(model, [0.0, 0])
        model_bucket[0] += cost
        model_bucket[1] += 1

        if key == cur_month_key:
            month_cost += cost
            month_calls += 1
        if dt.year == cur_year:
            year_cost += cost
            year_calls += 1
        if dt >= cutoff_30d:
            trailing_30d_cost += cost

    # Rounded to 6dp (float noise cleanup only) rather than 2dp: a single
    # flag extraction can genuinely cost a fraction of a cent, and rounding
    # to cents here would silently zero out a real, tracked charge in
    # "lifetime_total_usd" etc. Cent-level rounding is a DISPLAY decision —
    # the dashboard's Usage modal formats with toFixed(2) — not something
    # to bake into the numbers this endpoint reports.
    monthly_breakdown = [
        {"month": key, "cost_usd": round(monthly.get(key, [0.0, 0])[0], 6), "calls": monthly.get(key, [0.0, 0])[1]}
        for key in _trailing_months(now, 12)
    ]
    per_model_breakdown = [
        {"model": model, "cost_usd": round(vals[0], 6), "calls": vals[1]}
        for model, vals in sorted(per_model.items())
    ]

    return {
        "lifetime_total_usd": round(lifetime_total, 6),
        "current_month": {"month": cur_month_key, "cost_usd": round(month_cost, 6), "calls": month_calls},
        "current_year": {"year": cur_year, "cost_usd": round(year_cost, 6), "calls": year_calls},
        "trailing_12mo_projection_usd": round(trailing_30d_cost * 12, 6),
        "monthly_breakdown": monthly_breakdown,
        "per_model": per_model_breakdown,
    }
