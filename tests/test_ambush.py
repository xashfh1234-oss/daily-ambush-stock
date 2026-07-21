import pandas as pd

from stock_assistant.ambush import (
    _features, ambush_signal_history, ambush_signal_performance,
    ambush_stock_diagnostics, market_environment, save_ambush_signals,
)
from stock_assistant.database import initialize, upsert_records


def test_ambush_features_from_daily_prices():
    count = 80
    close = [10 + index * 0.01 for index in range(count)]
    frame = pd.DataFrame({
        "trade_date": pd.date_range("2025-01-01", periods=count).strftime("%Y%m%d"),
        "open": close, "high": [value * 1.01 for value in close],
        "low": [value * 0.99 for value in close], "close": close,
        "vol": [1000] * 79 + [1500], "amount": [200_000] * count,
        "pct_chg": [0.1] * count,
    })
    result = _features(frame)
    assert result is not None
    assert 1.4 < result["volume_ratio"] < 1.6
    assert result["average_amount"] >= 100_000_000


def test_ambush_features_handle_empty_frame():
    assert _features(pd.DataFrame()) is None


def test_ambush_diagnostics_explains_each_filter():
    count = 80
    close = [10 + index * 0.01 for index in range(count)]
    frame = pd.DataFrame({
        "trade_date": pd.date_range("2025-01-01", periods=count).strftime("%Y%m%d"),
        "open": close, "high": [value * 1.01 for value in close],
        "low": [value * 0.99 for value in close], "close": close,
        "vol": [1000] * 79 + [1500], "amount": [200_000] * count,
        "pct_chg": [0.1] * count,
    })
    result = ambush_stock_diagnostics(frame)
    assert result is not None
    assert len(result["checks"]) == 7
    assert result["passed_count"] == sum(bool(passed) for _, passed, _ in result["checks"])
    assert result["confirm_price"] > 0
    assert result["invalid_price"] > 0


def test_save_and_read_signal_history(tmp_path):
    path = tmp_path / "signals.db"
    initialize(path)
    upsert_records(path, "stocks", [{"ts_code": "000001.SZ", "name": "样例"}], ["ts_code", "name"])
    frame = pd.DataFrame([{
        "ts_code": "000001.SZ", "trade_date": "20250101", "stage": "观察",
        "total_score": 70, "close": 10, "confirm_price": 11,
        "invalid_price": 9, "reason": "测试",
    }])
    assert save_ambush_signals(path, frame) == 1
    history = ambush_signal_history(path)
    assert len(history) == 1
    assert history.iloc[0]["score"] == 70


def test_market_environment_and_signal_performance(tmp_path):
    path = tmp_path / "environment.db"
    initialize(path)
    stocks = [{"ts_code": f"00000{i}.SZ", "name": f"样例{i}"} for i in range(1, 6)]
    upsert_records(path, "stocks", stocks, ["ts_code", "name"])
    prices = []
    for i, stock in enumerate(stocks, 1):
        for day, change in (("20250101", 0), ("20250102", i), ("20250103", i + 1)):
            prices.append({"ts_code": stock["ts_code"], "trade_date": day, "close": 10 + i, "high": 12, "low": 9, "pct_chg": change})
    upsert_records(path, "daily_prices", prices, ["ts_code", "trade_date", "close", "high", "low", "pct_chg"])
    environment = market_environment(path, "20250102")
    assert environment["state"] == "偏强"
    signal = pd.DataFrame([{
        "ts_code": "000001.SZ", "trade_date": "20250101", "stage": "观察",
        "total_score": 70, "close": 10, "confirm_price": 11,
        "invalid_price": 8, "reason": "测试",
    }])
    save_ambush_signals(path, signal)
    performance = ambush_signal_performance(path)
    assert len(performance) == 1
    assert bool(performance.iloc[0]["confirmed_5d"])
