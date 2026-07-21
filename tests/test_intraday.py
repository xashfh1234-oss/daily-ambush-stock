import pandas as pd

from stock_assistant.database import initialize, query
from stock_assistant.intraday import _tradable, latest_intraday_run, sync_intraday_data


def test_intraday_tradable_filter():
    assert _tradable("000001.SZ", "平安银行")
    assert not _tradable("300001.SZ", "特锐德")
    assert not _tradable("302132.SZ", "中航成飞")
    assert not _tradable("688001.SH", "华兴源创")
    assert not _tradable("920008.BJ", "成电光信")
    assert not _tradable("000002.SZ", "ST样例")


def test_sync_intraday_data_persists_normalized_snapshot(tmp_path, monkeypatch):
    import akshare as ak

    path = tmp_path / "intraday.db"
    initialize(path)
    today = pd.DataFrame([{
        "代码": "000001", "名称": "平安银行", "最新价": 10, "今日涨跌幅": 1,
        "今日主力净流入-净额": 100, "今日主力净流入-净占比": 2,
        "今日超大单净流入-净额": 40, "今日大单净流入-净额": 60,
        "今日中单净流入-净额": -20, "今日小单净流入-净额": -80,
    }])
    three = pd.DataFrame([{"代码": "000001", "3日主力净流入-净额": 300, "3日小单净流入-净额": -200}])
    sector = pd.DataFrame([{
        "名称": "银行", "今日涨跌幅": 1, "今日主力净流入-净额": 1000,
        "今日主力净流入-净占比": 3, "今日主力净流入最大股": "平安银行",
    }])
    limit_pool = pd.DataFrame([{
        "代码": "000001", "名称": "平安银行", "最新价": 10, "涨跌幅": 10,
        "成交额": 200_000_000, "换手率": 5, "封板资金": 20_000_000,
        "首次封板时间": "095000", "最后封板时间": "100000", "炸板次数": 0,
        "连板数": 1, "涨停统计": "1/1", "所属行业": "银行",
    }])
    monkeypatch.setattr(ak, "stock_individual_fund_flow_rank", lambda indicator: today if indicator == "今日" else three)
    monkeypatch.setattr(ak, "stock_sector_fund_flow_rank", lambda **kwargs: sector)
    monkeypatch.setattr(ak, "stock_zt_pool_em", lambda **kwargs: limit_pool)
    monkeypatch.setattr(ak, "stock_zt_pool_zbgc_em", lambda **kwargs: pd.DataFrame())

    result = sync_intraday_data(path)
    assert result["status"] == "COMPLETED"
    assert result["money"] == result["sector"] == 1
    assert result["limit"] == 1
    assert result["broken"] == 0
    assert query(path, "SELECT small_net FROM intraday_money_flow")[0]["small_net"] == -80
    assert latest_intraday_run(path)["status"] == "COMPLETED"
