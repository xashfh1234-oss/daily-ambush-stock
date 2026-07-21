from __future__ import annotations

import numpy as np
import pandas as pd

from .database import query, upsert_records
from .market import market_as_of_date


def _clip(value: float, low=0.0, high=1.0) -> float:
    return float(np.clip(value, low, high))


def _features(frame: pd.DataFrame) -> dict | None:
    if frame.empty or "trade_date" not in frame.columns:
        return None
    frame = frame.sort_values("trade_date").copy()
    numeric = ["open", "high", "low", "close", "vol", "amount", "pct_chg"]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close", "vol", "amount"])
    if len(frame) < 60:
        return None
    last = frame.iloc[-1]
    close = frame["close"]
    ma20, ma60 = close.tail(20).mean(), close.tail(60).mean()
    avg_vol20 = frame["vol"].tail(20).iloc[:-1].mean()
    avg_amount20 = frame["amount"].tail(20).mean() * 1000
    volume_ratio = float(last["vol"] / avg_vol20) if avg_vol20 else 0
    low60, high60 = frame["low"].tail(60).min(), frame["high"].tail(60).max()
    position60 = float((last["close"] - low60) / (high60 - low60)) if high60 > low60 else 0.5
    previous_high20 = frame["high"].iloc[-21:-1].max()
    return5 = float(last["close"] / close.iloc[-6] - 1)
    pct = float(last["pct_chg"] if not pd.isna(last["pct_chg"]) else close.pct_change().iloc[-1] * 100)
    upper_shadow = float((last["high"] - max(last["open"], last["close"])) / last["close"])
    volatility = float(close.pct_change().tail(20).std() * np.sqrt(252))
    recent = frame.tail(5)
    signed_amount = np.where(recent["close"].diff().fillna(0) >= 0, recent["amount"], -recent["amount"])
    acceptance = float(np.sum(signed_amount) / max(recent["amount"].sum(), 1))
    day_range = max(float(last["high"] - last["low"]), 0.001)
    close_strength = float((last["close"] - last["low"]) / day_range)
    support = float(min(ma20, frame["low"].tail(10).min()))
    return {
        "trade_date": str(last["trade_date"]), "close": float(last["close"]), "pct_chg": pct,
        "ma20": float(ma20), "ma60": float(ma60), "average_amount": avg_amount20,
        "volume_ratio": volume_ratio, "position60": position60, "return5": return5,
        "previous_high20": float(previous_high20), "upper_shadow": upper_shadow,
        "volatility": volatility, "acceptance": acceptance, "close_strength": close_strength,
        "support": support,
    }


def passes_ambush_filters(item: dict) -> bool:
    return (
        -3 <= item["pct_chg"] < 5
        and item["average_amount"] >= 100_000_000
        and 1.1 <= item["volume_ratio"] <= 2.5
        and item["position60"] <= 0.75
        and item["close"] >= item["ma60"] * 0.95
        and item["upper_shadow"] <= 0.04
        and item["return5"] <= 0.15
    )


def ambush_stock_diagnostics(frame: pd.DataFrame) -> dict | None:
    """Explain the ambush rules for one stock using only its local prices."""
    item = _features(frame)
    if item is None:
        return None
    checks = [
        ("当日涨幅", -3 <= item["pct_chg"] < 5, f"{item['pct_chg']:.2f}%（要求 -3%～5%）"),
        ("20日平均成交额", item["average_amount"] >= 100_000_000, f"{item['average_amount'] / 100_000_000:.2f}亿元（要求 ≥1亿元）"),
        ("成交量温和放大", 1.1 <= item["volume_ratio"] <= 2.5, f"量比 {item['volume_ratio']:.2f}（要求 1.10～2.50）"),
        ("股价位置", item["position60"] <= 0.75, f"60日位置 {item['position60']:.1%}（要求 ≤75%）"),
        ("中期趋势", item["close"] >= item["ma60"] * 0.95, f"收盘/MA60 {item['close'] / item['ma60']:.2%}（要求 ≥95%）"),
        ("上影风险", item["upper_shadow"] <= 0.04, f"上影 {item['upper_shadow']:.1%}（要求 ≤4%）"),
        ("短期涨幅", item["return5"] <= 0.15, f"近5日 {item['return5']:.1%}（要求 ≤15%）"),
    ]
    item["checks"] = checks
    item["passed_count"] = int(sum(bool(passed) for _, passed, _ in checks))
    item["is_eligible"] = all(passed for _, passed, _ in checks)
    item["confirm_price"] = round(item["previous_high20"], 3)
    item["invalid_price"] = round(item["support"] * 0.98, 3)
    item["distance_to_confirm"] = item["close"] / item["previous_high20"] - 1
    return item


def build_ambush_candidates(path, limit: int = 30, as_of_date: str | None = None) -> pd.DataFrame:
    as_of_date = as_of_date or market_as_of_date(path)
    if not as_of_date:
        return pd.DataFrame()
    date_rows = query(
        path,
        "SELECT DISTINCT trade_date FROM daily_prices WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 120",
        (as_of_date,),
    )
    if len(date_rows) < 60:
        return pd.DataFrame()
    earliest = date_rows[-1]["trade_date"]
    rows = query(
        path,
        """SELECT p.*,s.name,s.industry,s.list_date,s.market
        FROM daily_prices p JOIN stocks s ON s.ts_code=p.ts_code
        WHERE p.trade_date BETWEEN ? AND ? AND s.list_status='L' AND s.market!='BJ'
        ORDER BY p.ts_code,p.trade_date""",
        (earliest, as_of_date),
    )
    if not rows:
        return pd.DataFrame()
    all_prices = pd.DataFrame([dict(row) for row in rows])
    prepared = []
    for code, frame in all_prices.groupby("ts_code", sort=False):
        stock = frame.iloc[-1]
        # Only compare stocks whose latest quote belongs to the common market date.
        if str(stock["trade_date"]) != as_of_date:
            continue
        name = str(stock["name"])
        if "ST" in name.upper() or "退" in name:
            continue
        list_date = stock["list_date"]
        if list_date and list_date != "19900101" and (pd.Timestamp(as_of_date) - pd.Timestamp(list_date)).days < 365:
            continue
        feature = _features(frame)
        if feature:
            prepared.append({
                "ts_code": code, "name": name,
                "industry": str(stock["industry"] or "未分类"), "list_date": list_date,
                **feature,
            })
    if not prepared:
        return pd.DataFrame()

    universe = pd.DataFrame(prepared)
    classified = universe[universe["industry"] != "未分类"]
    industry_strength = classified.groupby("industry")["return5"].agg(["median", "count"])
    eligible = []
    for item in prepared:
        if not passes_ambush_filters(item):
            continue

        if item["industry"] in industry_strength.index and industry_strength.loc[item["industry"], "count"] >= 3:
            median = industry_strength.loc[item["industry"], "median"]
            sector_percentile = float((industry_strength["median"] <= median).mean())
            sector_available = True
        else:
            sector_percentile, sector_available = 0.5, False
        sector_score = 25 * sector_percentile
        acceptance_score = 25 * (0.6 * _clip((item["acceptance"] + 0.3) / 0.8) + 0.4 * item["close_strength"])
        volume_score = 20 * _clip(1 - abs(item["volume_ratio"] - 1.5) / 1.0)
        position_score = 15 * _clip(1 - item["position60"])
        trend_score = 10 * (0.5 * (item["close"] >= item["ma20"]) + 0.5 * (item["ma20"] >= item["ma60"]))
        risk_score = 5 * (0.6 * (1 - _clip(item["volatility"], 0.15, 0.70)) + 0.4 * (1 - _clip(item["upper_shadow"], 0, 0.04)))
        total = sector_score + acceptance_score + volume_score + position_score + trend_score + risk_score
        distance = item["close"] / item["previous_high20"] - 1
        stage = "已触发" if item["close"] > item["previous_high20"] and item["volume_ratio"] >= 1.3 else "接近触发" if distance >= -0.03 else "观察"
        reasons = [f"量比{item['volume_ratio']:.2f}", f"60日位置{item['position60']:.0%}"]
        if item["acceptance"] > 0:
            reasons.append("近5日量价承接偏强")
        if sector_available and sector_percentile >= 0.7:
            reasons.append("板块相对强势")
        if not sector_available:
            reasons.append("板块数据不足")
        eligible.append({
            **item, "stage": stage, "total_score": round(total, 2),
            "sector_score": round(sector_score, 2), "acceptance_score": round(acceptance_score, 2),
            "volume_score": round(volume_score, 2), "position_score": round(position_score, 2),
            "trend_score": round(trend_score, 2), "risk_score": round(risk_score, 2),
            "confirm_price": round(item["previous_high20"], 3), "invalid_price": round(item["support"] * 0.98, 3),
            "reason": "；".join(reasons), "sector_data_available": sector_available,
        })
    if not eligible:
        return pd.DataFrame()
    return pd.DataFrame(eligible).sort_values(["total_score", "volume_ratio"], ascending=False).head(limit).reset_index(drop=True)


def save_ambush_signals(path, candidates: pd.DataFrame) -> int:
    if candidates.empty:
        return 0
    records = [
        {
            "ts_code": row.ts_code, "signal_date": row.trade_date, "stage": row.stage,
            "score": row.total_score, "close": row.close,
            "confirm_price": row.confirm_price, "invalid_price": row.invalid_price,
            "reason": row.reason,
        }
        for row in candidates.itertuples()
    ]
    return upsert_records(
        path, "ambush_signals", records,
        ["ts_code", "signal_date", "stage", "score", "close", "confirm_price", "invalid_price", "reason"],
    )


def ambush_signal_history(path, limit: int = 100) -> pd.DataFrame:
    rows = query(
        path,
        """WITH latest AS (
          SELECT p.ts_code,p.close,p.trade_date FROM daily_prices p
          JOIN (SELECT ts_code,MAX(trade_date) trade_date FROM daily_prices GROUP BY ts_code) x
            ON x.ts_code=p.ts_code AND x.trade_date=p.trade_date
        )
        SELECT a.*,s.name,s.industry,l.close latest_close,l.trade_date latest_date,
               (l.close/a.close-1)*100 return_since_signal
        FROM ambush_signals a JOIN stocks s ON s.ts_code=a.ts_code
        LEFT JOIN latest l ON l.ts_code=a.ts_code
        ORDER BY a.signal_date DESC,a.score DESC LIMIT ?""",
        (limit,),
    )
    return pd.DataFrame([dict(row) for row in rows])


def market_environment(path, as_of_date: str | None = None) -> dict | None:
    as_of_date = as_of_date or market_as_of_date(path)
    if not as_of_date:
        return None
    rows = query(path, "SELECT pct_chg FROM daily_prices WHERE trade_date=? AND pct_chg IS NOT NULL", (as_of_date,))
    if not rows:
        return None
    changes = pd.Series([float(row["pct_chg"]) for row in rows])
    up_ratio = float((changes > 0).mean())
    limit_up = int((changes >= 9.5).sum())
    limit_down = int((changes <= -9.5).sum())
    median = float(changes.median())
    if up_ratio <= 0.40 or (limit_down >= max(limit_up, 1) and median < 0):
        state = "退潮"
    elif up_ratio >= 0.55 and median > 0:
        state = "偏强"
    else:
        state = "震荡"
    return {
        "trade_date": as_of_date, "state": state, "sample_size": len(changes),
        "up_ratio": up_ratio, "median_pct_chg": median,
        "limit_up": limit_up, "limit_down": limit_down,
    }


def ambush_signal_performance(path, horizons=(1, 3, 5, 10)) -> pd.DataFrame:
    signals = query(path, "SELECT * FROM ambush_signals ORDER BY signal_date,ts_code")
    if not signals:
        return pd.DataFrame()
    rows = []
    for signal in signals:
        prices = query(
            path,
            "SELECT trade_date,close,high,low FROM daily_prices WHERE ts_code=? AND trade_date>? ORDER BY trade_date LIMIT 10",
            (signal["ts_code"], signal["signal_date"]),
        )
        if not prices:
            continue
        result = {
            "ts_code": signal["ts_code"], "signal_date": signal["signal_date"],
            "stage": signal["stage"], "score": float(signal["score"]),
        }
        for horizon in horizons:
            result[f"return_{horizon}d"] = (
                (float(prices[horizon - 1]["close"]) / float(signal["close"]) - 1) * 100
                if len(prices) >= horizon else None
            )
        first5 = prices[:5]
        result["confirmed_5d"] = any(float(row["high"]) >= float(signal["confirm_price"] or float("inf")) for row in first5)
        result["invalidated_5d"] = any(float(row["low"]) <= float(signal["invalid_price"] or 0) for row in first5)
        rows.append(result)
    return pd.DataFrame(rows)
