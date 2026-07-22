import json

from stock_assistant.free_market_sources import TdxDailyClient, fetch_tencent_daily


class FakeTdxApi:
    def get_security_bars(self, *args):
        return [
            {"datetime": "2026-07-20 15:00", "open": 10, "high": 11, "low": 9.8, "close": 10.5, "vol": 1000, "amount": 1_000_000},
            {"datetime": "2026-07-21 15:00", "open": 10.5, "high": 11.2, "low": 10.4, "close": 11, "vol": 1200, "amount": 1_300_000},
        ]


def test_tdx_normalizes_daily_units():
    client = TdxDailyClient()
    client.api = FakeTdxApi()
    rows = client.fetch("000001.SZ", "20260720", "20260721")
    assert rows[-1]["trade_date"] == "20260721"
    assert rows[-1]["pre_close"] == 10.5
    assert rows[-1]["amount"] == 1300


def test_tencent_is_a_working_daily_fallback(monkeypatch):
    payload = {"code": 0, "data": {"sz000001": {"qfqday": [
        ["2026-07-20", "10", "10.5", "11", "9.8", "1000"],
        ["2026-07-21", "10.5", "11", "11.2", "10.4", "1200"],
    ]}}}

    class Response:
        def read(self):
            return json.dumps(payload).encode()

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    rows = fetch_tencent_daily("000001.SZ", "20260720", "20260721")
    assert len(rows) == 2
    assert rows[-1]["pct_chg"] > 0
    assert rows[-1]["amount"] > 0
