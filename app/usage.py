"""
usage.py — SQLite-backed spend ledger for Tier 2 flag extraction AND Tier 3
council calls (call_type distinguishes them; see below).

Lives in the same ~/.investment_engine/watchlist.db as the watchlist
tables (app/watchlist.py) — one local database for this single-user tool,
not a reason to invent a second file.

log_call() writes exactly one row per real model call — whether that's a
Tier 2 flag extraction (call_type="flags") or one of a Tier 3 council run's
seven calls (call_type="council_opinions" | "council_review" |
"council_chairman"). A "not yet extracted"/"not yet convened" check that
never reaches an actual model call writes nothing at all — nothing
happened yet to log. cost_usd is nullable: a real call whose pinned model
has no config.yaml pricing entry logs cost_usd=NULL ("pricing unknown"),
never a fabricated 0.0 — see summary()'s handling below. A genuine
cache-hit row (flags' "from_cache" status) is the one case that DOES stamp
cost_usd=0 deliberately, since nothing was called and nothing was spent.

Every live/refresh row's cost_usd is computed from config.yaml pricing AT
CALL TIME (engine.flags.compute_cost_usd / engine.council.compute_council_cost_usd)
and stored as-is, so a later price change in config.yaml never rewrites
history. cost_usd is NULLABLE: a live/refresh call whose model has no
config.yaml pricing entry stores cost_usd=NULL, never a fabricated 0.0 —
see those functions' docstrings. NULL must stay visibly distinct from a
from_cache row's genuine, by-construction 0.0.
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
    cost_usd REAL,
    called_at TEXT NOT NULL,
    cache_status TEXT NOT NULL,
    call_type TEXT NOT NULL DEFAULT 'flags'
);
"""

_BILLABLE_STATUSES = ("live", "refresh")


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrades a table created by an older version of this module — a
    brand-new table already matches _SCHEMA exactly (nullable cost_usd,
    call_type present), so PRAGMA table_info finds nothing to do and this
    is a no-op. An existing table with cost_usd NOT NULL needs a full
    rebuild (SQLite can't drop a column constraint in place); call_type is
    folded into that same rebuild pass when both are needed, so old rows
    never go through two separate migrations. An existing table that
    already has nullable cost_usd but just lacks call_type gets a plain
    ADD COLUMN instead — no rebuild needed for a new column with a default.
    """
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(flag_extraction_usage)").fetchall()}
    if not cols:
        return
    cost_usd_col = cols.get("cost_usd")
    needs_rebuild = cost_usd_col is not None and cost_usd_col[3] == 1  # notnull flag
    has_call_type = "call_type" in cols

    if needs_rebuild:
        conn.execute("ALTER TABLE flag_extraction_usage RENAME TO flag_extraction_usage_old")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO flag_extraction_usage "
            "(id, ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, called_at, cache_status, call_type) "
            "SELECT id, ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, called_at, cache_status, 'flags' "
            "FROM flag_extraction_usage_old"
        )
        conn.execute("DROP TABLE flag_extraction_usage_old")
        conn.commit()
    elif not has_call_type:
        conn.execute("ALTER TABLE flag_extraction_usage ADD COLUMN call_type TEXT NOT NULL DEFAULT 'flags'")
        conn.commit()


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
    _migrate(conn)
    return conn


def log_call(
    ticker: str,
    model: str,
    prompt_version: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Optional[float],
    cache_status: str,
    call_type: str = "flags",
    db_path: Optional[Path] = None,
) -> None:
    resolved = _resolve(db_path)
    conn = _connect(resolved)
    try:
        conn.execute(
            "INSERT INTO flag_extraction_usage "
            "(ticker, model, prompt_version, input_tokens, output_tokens, cost_usd, called_at, cache_status, call_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker.upper(), model, prompt_version, input_tokens, output_tokens,
                cost_usd, datetime.now(timezone.utc).isoformat(), cache_status, call_type,
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
    need. "calls" figures count every billable row (cache_status in
    "live"/"refresh") — a real model call happened whether or not its cost
    is known, whichever call_type it was. "cost_usd" sums, by contrast,
    only ever add up rows with a non-null cost: a pricing_unknown row
    (cost_usd IS NULL, see engine.flags.compute_cost_usd /
    engine.council.compute_council_cost_usd) is excluded from every sum
    rather than treated as a $0 contribution, which would silently
    understate every total — absence-is-not-zero applies to cost the same
    as any other figure. rows_with_unknown_pricing reports how many such
    rows exist so the dashboard can flag that the totals are a floor, not
    the full picture, until config.yaml's pricing table is filled in.

    trailing_12mo_projection_usd is an honest <trailing 30 days> x 12
    extrapolation, not a fitted model — the dashboard footnote spells out
    that assumption explicitly.
    """
    now = now or datetime.now(timezone.utc)
    resolved = _resolve(db_path)
    conn = _connect(resolved)
    try:
        rows = conn.execute(
            "SELECT model, cost_usd, called_at, cache_status, call_type FROM flag_extraction_usage"
        ).fetchall()
    finally:
        conn.close()

    billed = [
        (model, cost, called_at, call_type)
        for model, cost, called_at, status, call_type in rows
        if status in _BILLABLE_STATUSES
    ]

    cur_month_key = _month_key(now)
    cur_year = now.year
    cutoff_30d = now - timedelta(days=30)

    lifetime_total = 0.0
    month_cost, month_calls = 0.0, 0
    year_cost, year_calls = 0.0, 0
    trailing_30d_cost = 0.0
    rows_with_unknown_pricing = 0
    monthly: dict = {}       # "YYYY-MM" -> [cost, calls]
    per_model: dict = {}     # model -> [cost, calls]
    per_call_type: dict = {}  # call_type -> [cost, calls]

    for model, cost, called_at, call_type in billed:
        dt = datetime.fromisoformat(called_at)

        key = _month_key(dt)
        bucket = monthly.setdefault(key, [0.0, 0])
        bucket[1] += 1

        model_bucket = per_model.setdefault(model, [0.0, 0])
        model_bucket[1] += 1

        type_bucket = per_call_type.setdefault(call_type, [0.0, 0])
        type_bucket[1] += 1

        is_current_month = key == cur_month_key
        is_current_year = dt.year == cur_year
        if is_current_month:
            month_calls += 1
        if is_current_year:
            year_calls += 1

        if cost is None:
            rows_with_unknown_pricing += 1
            continue  # unknown cost never contributes to any $ sum

        lifetime_total += cost
        bucket[0] += cost
        model_bucket[0] += cost
        type_bucket[0] += cost
        if is_current_month:
            month_cost += cost
        if is_current_year:
            year_cost += cost
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
    per_call_type_breakdown = [
        {"call_type": call_type, "cost_usd": round(vals[0], 6), "calls": vals[1]}
        for call_type, vals in sorted(per_call_type.items())
    ]

    return {
        "lifetime_total_usd": round(lifetime_total, 6),
        "current_month": {"month": cur_month_key, "cost_usd": round(month_cost, 6), "calls": month_calls},
        "current_year": {"year": cur_year, "cost_usd": round(year_cost, 6), "calls": year_calls},
        "trailing_12mo_projection_usd": round(trailing_30d_cost * 12, 6),
        "monthly_breakdown": monthly_breakdown,
        "per_model": per_model_breakdown,
        "per_call_type": per_call_type_breakdown,
        "rows_with_unknown_pricing": rows_with_unknown_pricing,
    }
