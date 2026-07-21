from stock_assistant.database import initialize, upsert_records
from stock_assistant.market import market_as_of_date


def test_market_date_ignores_isolated_newer_quote(tmp_path):
    path = tmp_path / "market.db"
    initialize(path)
    records = []
    for code in ("000001.SZ", "000002.SZ", "000003.SZ"):
        records.append({"ts_code": code, "trade_date": "20250102", "close": 10})
    records.append({"ts_code": "000001.SZ", "trade_date": "20250103", "close": 11})
    upsert_records(path, "daily_prices", records, ["ts_code", "trade_date", "close"])
    assert market_as_of_date(path) == "20250102"


def test_market_date_uses_eligible_universe_coverage(tmp_path):
    path = tmp_path / "eligible-market.db"
    initialize(path)
    eligible_codes = [f"00000{i}.SZ" for i in range(1, 6)]
    filtered_codes = [f"60000{i}.SH" for i in range(1, 6)]
    statuses = [
        {"ts_code": code, "eligible": int(code in eligible_codes)}
        for code in eligible_codes + filtered_codes
    ]
    upsert_records(path, "stock_sync_status", statuses, ["ts_code", "eligible"])
    prices = [
        {"ts_code": code, "trade_date": "20250102", "close": 10}
        for code in eligible_codes + filtered_codes
    ]
    prices.extend(
        {"ts_code": code, "trade_date": "20250103", "close": 11}
        for code in eligible_codes[:4]
    )
    upsert_records(path, "daily_prices", prices, ["ts_code", "trade_date", "close"])
    assert market_as_of_date(path) == "20250103"
