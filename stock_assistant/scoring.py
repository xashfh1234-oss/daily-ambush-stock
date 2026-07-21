from dataclasses import dataclass
import math

import numpy as np
import pandas as pd


def _bounded(value, low, high) -> float:
    if value is None or pd.isna(value):
        return 0.0
    return float(np.clip((value - low) / (high - low), 0, 1))


@dataclass(frozen=True)
class ScoreResult:
    fundamental: float
    trend: float
    momentum: float
    risk: float
    liquidity: float
    total: float
    reasons: tuple[str, ...]


def score_stock(prices: pd.DataFrame, basic: dict, financial: dict | None) -> ScoreResult:
    if len(prices) < 60:
        raise ValueError("至少需要60个交易日行情")
    prices = prices.sort_values("trade_date").copy()
    close = prices["close"].astype(float)
    ma20, ma60 = close.tail(20).mean(), close.tail(60).mean()
    last = close.iloc[-1]
    return20 = last / close.iloc[-21] - 1 if len(close) >= 21 else 0
    return60 = last / close.iloc[-60] - 1
    volatility = close.pct_change().tail(60).std() * math.sqrt(252)
    avg_amount_yuan = prices["amount"].astype(float).tail(20).mean() * 1000

    trend = 25 * (0.5 * (last > ma20) + 0.5 * (ma20 > ma60))
    momentum = 15 * (0.6 * _bounded(return20, -0.10, 0.20) + 0.4 * _bounded(return60, -0.20, 0.40))
    risk = 10 * (1 - _bounded(volatility, 0.15, 0.70))
    liquidity = 10 * _bounded(avg_amount_yuan, 100_000_000, 1_000_000_000)

    financial = financial or {}
    roe = _bounded(financial.get("roe"), 0, 20)
    growth = (_bounded(financial.get("revenue_yoy"), -10, 30) + _bounded(financial.get("netprofit_yoy"), -20, 40)) / 2
    cash = _bounded(financial.get("ocf_to_or"), 0, 20)
    debt = 1 - _bounded(financial.get("debt_to_assets"), 30, 80)
    pe = basic.get("pe_ttm")
    valuation = 0 if pe is None or pd.isna(pe) or pe <= 0 else 1 - _bounded(pe, 10, 80)
    fundamental = 40 * (0.25 * roe + 0.25 * growth + 0.20 * cash + 0.15 * debt + 0.15 * valuation)
    total = fundamental + trend + momentum + risk + liquidity

    reasons = []
    if last > ma20 > ma60:
        reasons.append("均线呈多头结构")
    if return20 > 0.05:
        reasons.append("近20日动量较强")
    if financial.get("roe") is not None and financial.get("roe", 0) >= 10:
        reasons.append("ROE较好")
    if financial.get("revenue_yoy") is not None and financial.get("revenue_yoy", 0) > 0:
        reasons.append("营业收入同比增长")
    if avg_amount_yuan >= 100_000_000:
        reasons.append("流动性达标")
    return ScoreResult(
        round(fundamental, 2), round(trend, 2), round(momentum, 2),
        round(risk, 2), round(liquidity, 2), round(total, 2), tuple(reasons),
    )


def eligible_stock(name: str, list_date: str, average_amount_yuan: float, as_of: str) -> tuple[bool, str]:
    if "ST" in name.upper() or "退" in name:
        return False, "ST或退市风险股票"
    listed_days = (pd.Timestamp(as_of) - pd.Timestamp(list_date)).days
    if listed_days < 365:
        return False, "上市不足一年"
    if average_amount_yuan < 100_000_000:
        return False, "日均成交额不足1亿元"
    return True, "通过"

