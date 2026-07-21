from datetime import date

from stock_assistant.batch_sync import StockSyncTimeout, _filter_from_local, _market_history_start, _run_with_timeout, _static_filter
from stock_assistant.database import initialize, upsert_records


def test_filter_rejects_st_and_low_turnover(tmp_path):
    path = tmp_path / "batch.db"
    initialize(path)
    assert not _filter_from_local(path, "000001.SZ", "ST样例", "20200101")[0]
    prices = [{"ts_code": "000001.SZ", "trade_date": f"202501{i:02d}", "amount": 50_000} for i in range(1, 21)]
    upsert_records(path, "daily_prices", prices, ["ts_code", "trade_date", "amount"])
    allowed, reason = _filter_from_local(path, "000001.SZ", "样例", "20200101")
    assert not allowed
    assert "1亿元" in reason


def test_static_filter_runs_before_download():
    assert _static_filter({"ts_code": "000001.SZ", "name": "ST样例", "market": "SZ", "list_date": "20200101"}) == "ST或退市风险"
    assert _static_filter({"ts_code": "830001.BJ", "name": "样例", "market": "BJ", "list_date": "20200101"}) == "北交所"
    assert _static_filter({"ts_code": "920008.SH", "name": "样例", "market": "SH", "list_date": "19900101"}) == "北交所"


def test_stock_request_timeout():
    import time

    try:
        _run_with_timeout(1, time.sleep, 2)
        assert False, "应触发超时"
    except StockSyncTimeout:
        pass


def test_market_history_backfills_until_250_sessions():
    end = date(2026, 7, 20)
    assert _market_history_start("20260717", 144, end).strftime("%Y%m%d") == "20250526"
    assert _market_history_start("20260717", 250, end).strftime("%Y%m%d") == "20260710"
