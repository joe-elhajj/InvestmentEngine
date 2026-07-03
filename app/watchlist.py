"""
watchlist.py — SQLite-backed watchlist store.

Lives at ~/.investment_engine/watchlist.db — outside the repo, so it
survives branch switches and is never accidentally committed. Plain
synchronous sqlite3; no async ORM needed at this volume (a personal
watchlist of tens to low hundreds of tickers).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_DIR = Path.home() / ".investment_engine"
DB_PATH = DB_DIR / "watchlist.db"


def _resolve(db_path: Optional[Path]) -> Path:
    # Read the module-level DB_PATH at call time (not baked in as a default
    # argument value) so tests can monkeypatch app.watchlist.DB_PATH and
    # have every call — including ones from app/main.py that never pass
    # db_path explicitly — pick up the patched location.
    return db_path if db_path is not None else DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS equities (
    ticker TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS etfs (
    ticker TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    return conn


def load(db_path: Optional[Path] = None) -> dict:
    """Returns {"tickers": [...equities...], "etfs": [...]}, both alphabetical."""
    conn = _connect(_resolve(db_path))
    try:
        tickers = [row[0] for row in conn.execute("SELECT ticker FROM equities ORDER BY ticker")]
        etfs = [row[0] for row in conn.execute("SELECT ticker FROM etfs ORDER BY ticker")]
    finally:
        conn.close()
    return {"tickers": tickers, "etfs": etfs}


def add(ticker: str, type_: str, db_path: Optional[Path] = None) -> dict:
    """type_ is "etf" or "equity" (anything else falls back to equity)."""
    resolved = _resolve(db_path)
    table = "etfs" if type_ == "etf" else "equities"
    conn = _connect(resolved)
    try:
        conn.execute(
            f"INSERT OR IGNORE INTO {table} (ticker, added_at) VALUES (?, ?)",
            (ticker, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return load(resolved)


def remove(ticker: str, db_path: Optional[Path] = None) -> dict:
    """Removes from whichever table it's in (a ticker is never in both)."""
    resolved = _resolve(db_path)
    conn = _connect(resolved)
    try:
        conn.execute("DELETE FROM equities WHERE ticker = ?", (ticker,))
        conn.execute("DELETE FROM etfs WHERE ticker = ?", (ticker,))
        conn.commit()
    finally:
        conn.close()
    return load(resolved)
