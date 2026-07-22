import sqlite3
from datetime import date
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    ts_code TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT NOT NULL,
    area TEXT,
    industry TEXT,
    market TEXT,
    list_date TEXT,
    list_status TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS trade_calendar (
    exchange TEXT NOT NULL,
    cal_date TEXT NOT NULL,
    is_open INTEGER NOT NULL,
    pretrade_date TEXT,
    PRIMARY KEY (exchange, cal_date)
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code TEXT NOT NULL,
    shares INTEGER NOT NULL CHECK(shares >= 0),
    cost_price REAL NOT NULL CHECK(cost_price > 0),
    stop_price REAL,
    industry TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    shares INTEGER NOT NULL CHECK(shares > 0),
    price REAL NOT NULL CHECK(price > 0),
    fee REAL NOT NULL DEFAULT 0,
    traded_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS daily_prices (
    ts_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, pre_close REAL,
    pct_chg REAL, vol REAL, amount REAL,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_prices_date ON daily_prices(trade_date);
CREATE TABLE IF NOT EXISTS daily_basic (
    ts_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    turnover_rate REAL, volume_ratio REAL,
    pe_ttm REAL, pb REAL, ps_ttm REAL,
    total_mv REAL, circ_mv REAL,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS financial_indicators (
    ts_code TEXT NOT NULL,
    ann_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    roe REAL, debt_to_assets REAL,
    revenue_yoy REAL, netprofit_yoy REAL,
    ocf_to_or REAL, grossprofit_margin REAL,
    PRIMARY KEY (ts_code, ann_date, end_date)
);
CREATE INDEX IF NOT EXISTS idx_financial_ann_date ON financial_indicators(ann_date);
CREATE TABLE IF NOT EXISTS scores (
    ts_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    fundamental_score REAL NOT NULL,
    trend_score REAL NOT NULL,
    momentum_score REAL NOT NULL,
    risk_score REAL NOT NULL,
    liquidity_score REAL NOT NULL,
    total_score REAL NOT NULL,
    reason TEXT,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    snapshot_date TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    total_equity REAL NOT NULL,
    drawdown REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stock_sync_status (
    ts_code TEXT PRIMARY KEY,
    price_status TEXT NOT NULL DEFAULT 'PENDING',
    eligible INTEGER,
    filter_reason TEXT,
    financial_status TEXT NOT NULL DEFAULT 'PENDING',
    last_price_date TEXT,
    price_source TEXT,
    error TEXT,
    price_error TEXT,
    financial_error TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS batch_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL CHECK(stage IN ('MARKET','FINANCIAL')),
    status TEXT NOT NULL DEFAULT 'STARTING',
    batch_limit INTEGER NOT NULL,
    current INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    current_code TEXT,
    pid INTEGER,
    message TEXT,
    heartbeat_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ambush_signals (
    ts_code TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    stage TEXT NOT NULL,
    score REAL NOT NULL,
    close REAL NOT NULL,
    confirm_price REAL,
    invalid_price REAL,
    reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, signal_date)
);
CREATE INDEX IF NOT EXISTS idx_ambush_signals_date ON ambush_signals(signal_date);
CREATE TABLE IF NOT EXISTS intraday_money_flow (
    snapshot_at TEXT NOT NULL, trade_date TEXT NOT NULL, ts_code TEXT NOT NULL,
    name TEXT, price REAL, pct_chg REAL,
    main_net REAL, main_pct REAL, super_net REAL, large_net REAL,
    medium_net REAL, small_net REAL, three_day_main REAL, three_day_small REAL,
    source TEXT,
    PRIMARY KEY (snapshot_at, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_intraday_money_snapshot ON intraday_money_flow(snapshot_at);
CREATE TABLE IF NOT EXISTS intraday_sectors (
    snapshot_at TEXT NOT NULL, trade_date TEXT NOT NULL, sector_name TEXT NOT NULL,
    pct_chg REAL, main_net REAL, main_pct REAL, leading_stock TEXT,
    source TEXT,
    PRIMARY KEY (snapshot_at, sector_name)
);
CREATE TABLE IF NOT EXISTS intraday_limit_pool (
    snapshot_at TEXT NOT NULL, trade_date TEXT NOT NULL, ts_code TEXT NOT NULL,
    name TEXT, pool_type TEXT NOT NULL, price REAL, pct_chg REAL, amount REAL,
    turnover_rate REAL, seal_funds REAL, first_seal_time TEXT, last_seal_time TEXT,
    break_count INTEGER, board_count INTEGER, limit_stats TEXT, industry TEXT,
    PRIMARY KEY (snapshot_at, ts_code, pool_type)
);
CREATE TABLE IF NOT EXISTS intraday_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_at TEXT NOT NULL,
    status TEXT NOT NULL, money_count INTEGER DEFAULT 0, sector_count INTEGER DEFAULT 0,
    limit_count INTEGER DEFAULT 0, broken_count INTEGER DEFAULT 0, message TEXT
);
CREATE TABLE IF NOT EXISTS recommendation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
    status TEXT NOT NULL, market_state TEXT, up_ratio REAL, median_pct REAL,
    candidate_count INTEGER NOT NULL DEFAULT 0, pushed INTEGER NOT NULL DEFAULT 0,
    message TEXT, error TEXT, slot_label TEXT, is_final INTEGER NOT NULL DEFAULT 0,
    data_status TEXT, data_confidence REAL, recommendation_allowed INTEGER NOT NULL DEFAULT 0,
    is_catchup INTEGER NOT NULL DEFAULT 0, change_summary TEXT,
    push_key TEXT, push_status TEXT, push_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recommendation_items (
    run_id INTEGER NOT NULL, rank_no INTEGER NOT NULL, ts_code TEXT NOT NULL,
    name TEXT, industry TEXT, score REAL, price REAL, pct_chg REAL,
    main_net REAL, small_net REAL, volume_ratio REAL, position60 REAL,
    confirm_price REAL, invalid_price REAL, source TEXT, reason TEXT,
    appearance_count INTEGER NOT NULL DEFAULT 1, lifecycle TEXT, final_score REAL,
    data_confidence REAL, exit_reason TEXT, active INTEGER NOT NULL DEFAULT 1,
    formal INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id,ts_code),
    FOREIGN KEY (run_id) REFERENCES recommendation_runs(id)
);
CREATE TABLE IF NOT EXISTS scheduler_state (
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS risk_events (
    ts_code TEXT NOT NULL, event_date TEXT NOT NULL, risk_type TEXT NOT NULL,
    title TEXT NOT NULL, source TEXT, expires_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(ts_code,event_date,risk_type,title)
);
CREATE TABLE IF NOT EXISTS push_outbox (
    push_key TEXT PRIMARY KEY, run_id INTEGER, text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, created_at TEXT NOT NULL, sent_at TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize(path: Path) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(stock_sync_status)")}
        if "price_error" not in columns:
            connection.execute("ALTER TABLE stock_sync_status ADD COLUMN price_error TEXT")
        if "financial_error" not in columns:
            connection.execute("ALTER TABLE stock_sync_status ADD COLUMN financial_error TEXT")
        if "price_source" not in columns:
            connection.execute("ALTER TABLE stock_sync_status ADD COLUMN price_source TEXT")
        money_columns = {row[1] for row in connection.execute("PRAGMA table_info(intraday_money_flow)")}
        if "source" not in money_columns:
            connection.execute("ALTER TABLE intraday_money_flow ADD COLUMN source TEXT")
        sector_columns = {row[1] for row in connection.execute("PRAGMA table_info(intraday_sectors)")}
        if "source" not in sector_columns:
            connection.execute("ALTER TABLE intraday_sectors ADD COLUMN source TEXT")
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(recommendation_runs)")}
        if "slot_label" not in run_columns:
            connection.execute("ALTER TABLE recommendation_runs ADD COLUMN slot_label TEXT")
        if "is_final" not in run_columns:
            connection.execute("ALTER TABLE recommendation_runs ADD COLUMN is_final INTEGER NOT NULL DEFAULT 0")
        item_columns = {row[1] for row in connection.execute("PRAGMA table_info(recommendation_items)")}
        if "appearance_count" not in item_columns:
            connection.execute("ALTER TABLE recommendation_items ADD COLUMN appearance_count INTEGER NOT NULL DEFAULT 1")
        if "lifecycle" not in item_columns:
            connection.execute("ALTER TABLE recommendation_items ADD COLUMN lifecycle TEXT")
        if "final_score" not in item_columns:
            connection.execute("ALTER TABLE recommendation_items ADD COLUMN final_score REAL")
        migrations = {
            "recommendation_runs": {
                "data_status": "TEXT", "data_confidence": "REAL",
                "recommendation_allowed": "INTEGER NOT NULL DEFAULT 0",
                "is_catchup": "INTEGER NOT NULL DEFAULT 0", "change_summary": "TEXT",
                "push_key": "TEXT", "push_status": "TEXT", "push_attempts": "INTEGER NOT NULL DEFAULT 0",
            },
            "recommendation_items": {"data_confidence": "REAL", "exit_reason": "TEXT", "active": "INTEGER NOT NULL DEFAULT 1", "formal": "INTEGER NOT NULL DEFAULT 0"},
        }
        for table, additions in migrations.items():
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for column, definition in additions.items():
                if column not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def backup_database(path: Path, keep: int = 7) -> Path | None:
    path = Path(path)
    if not path.exists():
        return None
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"stock_assistant_{date.today().isoformat()}.db"
    if not destination.exists():
        with sqlite3.connect(path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
    backups = sorted(backup_dir.glob("stock_assistant_*.db"), reverse=True)
    for old_backup in backups[keep:]:
        old_backup.unlink()
    return destination


def upsert_records(path: Path, table: str, records: list[dict], columns: list[str]) -> int:
    if not records:
        return 0
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns[1:])
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT DO UPDATE SET {updates}"
    )
    values = [[record.get(column) for column in columns] for record in records]
    with connect(path) as connection:
        connection.executemany(sql, values)
    return len(records)


def query(path: Path, sql: str, parameters=()) -> list[sqlite3.Row]:
    with connect(path) as connection:
        return connection.execute(sql, parameters).fetchall()


def execute(path: Path, sql: str, parameters=()) -> int:
    with connect(path) as connection:
        cursor = connection.execute(sql, parameters)
        return cursor.lastrowid
