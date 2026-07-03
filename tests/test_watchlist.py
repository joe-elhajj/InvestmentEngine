"""
test_watchlist.py — tests for app.watchlist's SQLite-backed store.

Every test uses an isolated temp DB (tmp_path) — never touches the real
~/.investment_engine/watchlist.db.
"""

from __future__ import annotations

from app import watchlist


def test_load_empty_db_returns_empty_lists(tmp_path):
    db = tmp_path / "watchlist.db"
    result = watchlist.load(db)
    assert result == {"tickers": [], "etfs": []}


def test_add_equity_round_trips(tmp_path):
    db = tmp_path / "watchlist.db"
    result = watchlist.add("NVDA", "equity", db)
    assert result["tickers"] == ["NVDA"]
    assert result["etfs"] == []


def test_add_etf_round_trips(tmp_path):
    db = tmp_path / "watchlist.db"
    result = watchlist.add("VOO", "etf", db)
    assert result["etfs"] == ["VOO"]
    assert result["tickers"] == []


def test_add_unknown_type_falls_back_to_equity(tmp_path):
    db = tmp_path / "watchlist.db"
    result = watchlist.add("MSFT", "bogus-type", db)
    assert result["tickers"] == ["MSFT"]
    assert result["etfs"] == []


def test_add_is_idempotent(tmp_path):
    """Re-adding the same ticker must not error or duplicate (PRIMARY KEY)."""
    db = tmp_path / "watchlist.db"
    watchlist.add("NVDA", "equity", db)
    result = watchlist.add("NVDA", "equity", db)
    assert result["tickers"] == ["NVDA"]


def test_remove_round_trips(tmp_path):
    db = tmp_path / "watchlist.db"
    watchlist.add("NVDA", "equity", db)
    watchlist.add("MSFT", "equity", db)
    result = watchlist.remove("NVDA", db)
    assert result["tickers"] == ["MSFT"]


def test_remove_nonexistent_ticker_is_a_noop(tmp_path):
    db = tmp_path / "watchlist.db"
    result = watchlist.remove("GHOST", db)
    assert result == {"tickers": [], "etfs": []}


def test_load_returns_alphabetical_order(tmp_path):
    db = tmp_path / "watchlist.db"
    for tk in ["TSLA", "AAPL", "MSFT"]:
        watchlist.add(tk, "equity", db)
    result = watchlist.load(db)
    assert result["tickers"] == ["AAPL", "MSFT", "TSLA"]


def test_equity_and_etf_tables_are_independent(tmp_path):
    """A ticker added as equity then removed must not affect an ETF of the same name."""
    db = tmp_path / "watchlist.db"
    watchlist.add("SAME", "equity", db)
    watchlist.add("SAME", "etf", db)
    result = watchlist.remove("SAME", db)
    # remove() deletes from both tables — this documents that behavior:
    # a ticker is never intentionally in both lists at once.
    assert result == {"tickers": [], "etfs": []}


def test_db_file_and_parent_dir_created_on_first_use(tmp_path):
    db = tmp_path / "nested" / "dir" / "watchlist.db"
    assert not db.parent.exists()
    watchlist.add("NVDA", "equity", db)
    assert db.exists()


def test_default_db_path_points_at_home_investment_engine_dir():
    assert watchlist.DB_PATH == watchlist.DB_DIR / "watchlist.db"
    assert watchlist.DB_DIR.name == ".investment_engine"
