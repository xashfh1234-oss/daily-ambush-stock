from stock_assistant.database import backup_database, initialize, query


def test_initialize_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    initialize(path)
    initialize(path)
    tables = query(path, "SELECT name FROM sqlite_master WHERE type='table'")
    names = {row["name"] for row in tables}
    assert {"stocks", "daily_prices", "financial_indicators", "trades", "scores", "intraday_money_flow", "intraday_sectors", "intraday_limit_pool"} <= names
    sync_columns = {row[1] for row in query(path, "PRAGMA table_info(stock_sync_status)")}
    assert {"price_error", "financial_error"} <= sync_columns
    assert query(path, "PRAGMA journal_mode")[0][0] == "wal"
    backup = backup_database(path)
    assert backup is not None and backup.exists()
