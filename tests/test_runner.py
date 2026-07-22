from types import SimpleNamespace
from datetime import datetime

import pandas as pd

from stock_assistant.database import initialize, query
from stock_assistant import runner


def test_run_once_saves_and_pushes_recommendation(tmp_path, monkeypatch):
    path = tmp_path / "runner.db"
    initialize(path)
    monkeypatch.setattr(runner, "settings", SimpleNamespace(database_path=path))
    monkeypatch.setattr(runner, "_ensure_base_data", lambda **kwargs: None)
    monkeypatch.setattr(runner, "_refresh_daily_if_due", lambda now: None)
    monkeypatch.setattr(runner, "sync_intraday_data", lambda path: {"money": 100, "sector": 10})
    monkeypatch.setattr(runner, "_market_snapshot", lambda path: {"state": "偏强", "up_ratio": .6, "median": .5})
    candidate = pd.DataFrame([{
        "ts_code": "000001.SZ", "name": "样例", "industry": "银行", "strategy_score": 80,
        "price": 10, "pct_chg": 2, "main_net": 100, "small_net": -50,
        "volume_ratio": 1.5, "position60": .4, "confirm_price": 10.5,
        "invalid_price": 9.5, "source": "测试", "reason": "全部满足",
    }])
    monkeypatch.setattr(runner, "build_intraday_candidates", lambda path: {"综合伏击候选": candidate})
    monkeypatch.setattr(runner, "sync_candidate_risks", lambda *args: 0)
    pushed = []
    monkeypatch.setattr(runner, "_push_wechat", pushed.append)

    run_id = runner.run_once(push=True, refresh_daily=False)
    run = dict(query(path, "SELECT * FROM recommendation_runs WHERE id=?", (run_id,))[0])
    assert run["status"] == "COMPLETED"
    assert run["candidate_count"] == 1
    assert run["pushed"] == 1
    assert len(query(path, "SELECT * FROM recommendation_items WHERE run_id=?", (run_id,))) == 1
    assert "样例" in pushed[0]
    assert run["slot_label"] == "手动"


def test_due_scan_uses_key_times():
    assert runner._due_scan(datetime(2026, 7, 21, 9, 45)) == ("09:45", False, False)
    assert runner._due_scan(datetime(2026, 7, 21, 14, 45)) == ("14:45", True, False)
    assert runner._due_scan(datetime(2026, 7, 21, 10, 20)) == ("10:05", False, True)
    assert runner._due_scan(datetime(2026, 7, 21, 12, 0)) is None
    assert runner._due_scan(datetime(2026, 7, 19, 9, 45)) is None
