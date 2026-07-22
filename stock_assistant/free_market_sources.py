from __future__ import annotations

import json
from datetime import datetime
import urllib.parse
import urllib.request

import pandas as pd

from .data_sources import normalize_code


# 只使用已实测能直连的公开通达信节点，连接失败时按顺序切换。
TDX_ENDPOINTS = (
    ("180.153.18.170", 7709),
    ("180.153.18.172", 80),
    ("202.108.253.139", 80),
    ("60.191.117.167", 7709),
    ("115.238.56.198", 7709),
)


class TdxDailyClient:
    name = "通达信公开行情"

    def __init__(self):
        self.api = None
        self.endpoint = None

    def connect(self) -> None:
        from pytdx.hq import TdxHq_API

        errors = []
        for host, port in TDX_ENDPOINTS:
            api = TdxHq_API(heartbeat=True, auto_retry=True, raise_exception=True)
            try:
                if api.connect(host, port, time_out=2):
                    self.api, self.endpoint = api, f"{host}:{port}"
                    return
            except Exception as error:
                errors.append(f"{host}:{port} {error}")
            try:
                api.disconnect()
            except Exception:
                pass
        raise RuntimeError("通达信节点均不可用：" + "；".join(errors[-3:]))

    def close(self) -> None:
        if self.api:
            try:
                self.api.disconnect()
            except Exception:
                pass
        self.api = None

    def fetch(self, code: str, start_date: str, end_date: str, count: int = 420) -> list[dict]:
        if self.api is None:
            self.connect()
        digits, ts_code, _ = normalize_code(code)
        market = 1 if ts_code.endswith(".SH") else 0
        try:
            bars = self.api.get_security_bars(9, market, digits, 0, min(count, 800))
        except Exception:
            self.close()
            self.connect()
            bars = self.api.get_security_bars(9, market, digits, 0, min(count, 800))
        if not bars:
            raise RuntimeError("通达信返回空行情")
        frame = pd.DataFrame(bars)
        frame["trade_date"] = pd.to_datetime(frame["datetime"]).dt.strftime("%Y%m%d")
        frame = frame[(frame["trade_date"] >= start_date) & (frame["trade_date"] <= end_date)].copy()
        if frame.empty:
            raise RuntimeError("通达信指定日期无行情")
        frame["ts_code"] = ts_code
        frame["pre_close"] = frame["close"].shift(1)
        frame["pct_chg"] = (frame["close"] / frame["pre_close"] - 1) * 100
        # TDX vol 为手、amount 为元；库内 amount 统一为千元。
        frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce") / 1000
        columns = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]
        return frame[columns].to_dict("records")


def fetch_tencent_daily(code: str, start_date: str, end_date: str, count: int = 420) -> list[dict]:
    digits, ts_code, symbol = normalize_code(code)
    parameter = f"{symbol.replace('.', '')},day,,,{min(count, 640)},qfq"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode({"param": parameter})
    # 免费国内行情使用直连，避免系统代理造成 SSL 中断。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    payload = json.loads(opener.open(url, timeout=12).read())
    node = payload.get("data", {}).get(symbol.replace(".", ""), {})
    days = node.get("qfqday") or node.get("day") or []
    records = []
    previous = None
    for day in days:
        if len(day) < 6:
            continue
        trade_date = str(day[0]).replace("-", "")
        if not start_date <= trade_date <= end_date:
            continue
        open_price, close, high, low, volume = map(float, day[1:6])
        pct = (close / previous - 1) * 100 if previous else None
        average_price = (open_price + close + high + low) / 4
        # 腾讯 K 线不返回历史成交额，使用 OHLC 均价×成交量估算，仅作补漏。
        amount_thousand = average_price * volume / 10
        records.append({
            "ts_code": ts_code, "trade_date": trade_date, "open": open_price, "high": high, "low": low,
            "close": close, "pre_close": previous, "pct_chg": pct, "vol": volume, "amount": amount_thousand,
        })
        previous = close
    if not records:
        raise RuntimeError("腾讯K线返回空数据")
    return records
