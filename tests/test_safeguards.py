from datetime import datetime

from stock_assistant.database import initialize, upsert_records
from stock_assistant.safeguards import assess_data_quality, expected_daily_date, stock_risk


def test_expected_daily_date_uses_previous_session_before_close(tmp_path):
    path = tmp_path / "quality.db"
    initialize(path)
    upsert_records(path, "trade_calendar", [
        {"exchange": "SSE", "cal_date": "20260720", "is_open": 1, "pretrade_date": None},
        {"exchange": "SSE", "cal_date": "20260721", "is_open": 1, "pretrade_date": "20260720"},
    ], ["exchange", "cal_date", "is_open", "pretrade_date"])
    assert expected_daily_date(path, datetime(2026, 7, 21, 10)) == "20260720"
    assert expected_daily_date(path, datetime(2026, 7, 21, 16)) == "20260721"


def test_quality_blocks_incomplete_or_stale_data(tmp_path):
    path = tmp_path / "quality.db"
    initialize(path)
    result = assess_data_quality(path, {"snapshot_at": "2026-07-21T09:45:00", "money": 10, "sector": 2, "status": "PARTIAL"}, datetime(2026, 7, 21, 10))
    assert result["status"] == "BLOCKED"
    assert result["confidence"] < .5


def test_known_risk_event_rejects_stock(tmp_path):
    path = tmp_path / "risk.db"
    initialize(path)
    upsert_records(path, "stocks", [{"ts_code": "000001.SZ", "symbol": "000001", "name": "样例", "list_date": "20000101", "list_status": "L"}], ["ts_code", "symbol", "name", "list_date", "list_status"])
    upsert_records(path, "risk_events", [{"ts_code": "000001.SZ", "event_date": "20260701", "risk_type": "监管", "title": "收到立案告知书", "expires_at": "20260930"}], ["ts_code", "event_date", "risk_type", "title", "expires_at"])
    blocked, reason, confidence = stock_risk(path, "000001.SZ", "20260721")
    assert blocked and "立案" in reason and confidence == 1
