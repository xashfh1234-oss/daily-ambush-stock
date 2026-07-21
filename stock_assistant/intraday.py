from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from .ambush import ambush_stock_diagnostics
from .data_sources import normalize_code
from .database import execute, query, upsert_records
from .market import price_frame
from .safeguards import stock_risk


def _number(value, default=None):
    result = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(result) else float(result)


def _money_yuan(value) -> float:
    if value is None or pd.isna(value):
        return 0.0
    text = str(value).replace(",", "").strip()
    multiplier = 1
    if text.endswith("亿"):
        multiplier, text = 100_000_000, text[:-1]
    elif text.endswith("万"):
        multiplier, text = 10_000, text[:-1]
    number = pd.to_numeric(text, errors="coerce")
    return (0.0 if pd.isna(number) else float(number)) * multiplier


def _percent(value) -> float:
    return _number(str(value).replace("%", ""), 0)


def _tradable(code: str, name: str) -> bool:
    digits = str(code).split(".")[0].zfill(6)
    upper_name = str(name or "").upper()
    return not digits.startswith(("4", "8", "920", "30", "688")) and "ST" not in upper_name and "退" not in upper_name


def sync_intraday_data(path) -> dict:
    """Fetch independent live datasets and retain the last good snapshot on failure."""
    import akshare as ak

    snapshot = datetime.now().isoformat(timespec="seconds")
    trade_date = datetime.now().strftime("%Y%m%d")
    counts = {"money": 0, "sector": 0, "limit": 0, "broken": 0}
    errors = []
    sina_individual = None

    try:
        today = ak.stock_individual_fund_flow_rank(indicator="今日")
        three = ak.stock_individual_fund_flow_rank(indicator="3日")
        three_map = {str(row["代码"]).zfill(6): row for row in three.to_dict("records")}
        records = []
        for row in today.to_dict("records"):
            digits = str(row["代码"]).zfill(6)
            _, code, _ = normalize_code(digits)
            other = three_map.get(digits, {})
            records.append({
                "snapshot_at": snapshot, "trade_date": trade_date, "ts_code": code,
                "name": row.get("名称"), "price": _number(row.get("最新价")),
                "pct_chg": _number(row.get("今日涨跌幅")),
                "main_net": _number(row.get("今日主力净流入-净额"), 0),
                "main_pct": _number(row.get("今日主力净流入-净占比"), 0),
                "super_net": _number(row.get("今日超大单净流入-净额"), 0),
                "large_net": _number(row.get("今日大单净流入-净额"), 0),
                "medium_net": _number(row.get("今日中单净流入-净额"), 0),
                "small_net": _number(row.get("今日小单净流入-净额"), 0),
                "three_day_main": _number(other.get("3日主力净流入-净额")),
                "three_day_small": _number(other.get("3日小单净流入-净额")),
                "source": "东方财富订单资金",
            })
        counts["money"] = upsert_records(path, "intraday_money_flow", records, list(records[0]) if records else [])
    except Exception as eastmoney_error:
        try:
            sina_individual = ak.stock_fund_flow_individual(symbol="即时")
            big_deals = ak.stock_fund_flow_big_deal()
            big_deals["代码"] = big_deals["股票代码"].astype(str).str.zfill(6)
            big_deals["金额元"] = pd.to_numeric(big_deals["成交额"], errors="coerce").fillna(0) * 10_000
            big_deals["有符号金额"] = np.where(big_deals["大单性质"].eq("买盘"), big_deals["金额元"], -big_deals["金额元"])
            big_map = big_deals.groupby("代码")["有符号金额"].sum().to_dict()
            records = []
            for row in sina_individual.to_dict("records"):
                digits = str(row["股票代码"]).zfill(6)
                _, code, _ = normalize_code(digits)
                main_net = float(big_map.get(digits, 0))
                total_net = _money_yuan(row.get("净额"))
                amount = _money_yuan(row.get("成交额"))
                records.append({
                    "snapshot_at": snapshot, "trade_date": trade_date, "ts_code": code,
                    "name": row.get("股票简称"), "price": _number(row.get("最新价")),
                    "pct_chg": _percent(row.get("涨跌幅")), "main_net": main_net,
                    "main_pct": main_net / amount * 100 if amount else 0,
                    "super_net": 0, "large_net": main_net, "medium_net": 0,
                    "small_net": total_net - main_net, "three_day_main": None, "three_day_small": None,
                    "source": "新浪大单代理",
                })
            counts["money"] = upsert_records(path, "intraday_money_flow", records, list(records[0]) if records else [])
            errors.append(f"东方财富资金流不可用，已降级新浪大单代理：{eastmoney_error}")
        except Exception as sina_error:
            errors.append(f"资金流：东方财富失败 {eastmoney_error}；新浪失败 {sina_error}")

    try:
        sectors = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        records = [{
            "snapshot_at": snapshot, "trade_date": trade_date, "sector_name": str(row.get("名称")),
            "pct_chg": _number(row.get("今日涨跌幅")), "main_net": _number(row.get("今日主力净流入-净额"), 0),
            "main_pct": _number(row.get("今日主力净流入-净占比"), 0),
            "leading_stock": row.get("今日主力净流入最大股"),
            "source": "东方财富行业资金",
        } for row in sectors.to_dict("records")]
        counts["sector"] = upsert_records(path, "intraday_sectors", records, list(records[0]) if records else [])
    except Exception as eastmoney_error:
        try:
            sectors = ak.stock_fund_flow_industry(symbol="即时")
            records = [{
                "snapshot_at": snapshot, "trade_date": trade_date, "sector_name": str(row.get("行业")),
                "pct_chg": _number(row.get("行业-涨跌幅")), "main_net": _number(row.get("净额"), 0) * 100_000_000,
                "main_pct": 0, "leading_stock": row.get("领涨股"), "source": "新浪行业资金",
            } for row in sectors.to_dict("records")]
            counts["sector"] = upsert_records(path, "intraday_sectors", records, list(records[0]) if records else [])
            errors.append(f"东方财富板块不可用，已降级新浪行业资金：{eastmoney_error}")
        except Exception as sina_error:
            errors.append(f"板块：东方财富失败 {eastmoney_error}；新浪失败 {sina_error}")

    # 涨停/炸板数量只用于判断市场情绪，不参与个股推荐。
    try:
        counts["limit"] = len(ak.stock_zt_pool_em(date=trade_date))
        counts["broken"] = len(ak.stock_zt_pool_zbgc_em(date=trade_date))
    except Exception as error:
        errors.append(f"涨停/炸板情绪：{error}")

    status = "COMPLETED" if not errors else "PARTIAL" if any(counts.values()) else "FAILED"
    execute(
        path,
        """INSERT INTO intraday_sync_runs(snapshot_at,status,money_count,sector_count,limit_count,broken_count,message)
        VALUES(?,?,?,?,?,?,?)""",
        (snapshot, status, counts["money"], counts["sector"], counts["limit"], counts["broken"], "；".join(errors)[:2000]),
    )
    return {"snapshot_at": snapshot, "status": status, **counts, "errors": errors}


def latest_intraday_run(path):
    rows = query(path, "SELECT * FROM intraday_sync_runs ORDER BY id DESC LIMIT 1")
    return dict(rows[0]) if rows else None


def _latest_frame(path, table: str) -> pd.DataFrame:
    rows = query(path, f"SELECT * FROM {table} WHERE snapshot_at=(SELECT MAX(snapshot_at) FROM {table})")
    return pd.DataFrame([dict(row) for row in rows])


def build_intraday_candidates(path) -> dict[str, pd.DataFrame]:
    money = _latest_frame(path, "intraday_money_flow")
    sectors = _latest_frame(path, "intraday_sectors")
    stocks = {row["ts_code"]: dict(row) for row in query(path, "SELECT ts_code,name,industry FROM stocks")}
    previous_money = {}
    if not money.empty:
        snapshot = str(money.iloc[0]["snapshot_at"])
        previous_rows = query(path, """SELECT * FROM intraday_money_flow WHERE snapshot_at=(
            SELECT MAX(snapshot_at) FROM intraday_money_flow WHERE snapshot_at<? AND trade_date=?
        )""", (snapshot, snapshot[:10].replace("-", "")))
        previous_money = {row["ts_code"]: dict(row) for row in previous_rows}

    top_sectors = pd.DataFrame()
    if not sectors.empty:
        top_sectors = sectors[(sectors["main_net"] > 0)].sort_values(["main_net", "pct_chg"], ascending=False).head(3).copy()
    available_industries = {str(item.get("industry") or "") for item in stocks.values()}
    if not money.empty and (top_sectors.empty or not set(top_sectors["sector_name"]) & available_industries):
        aggregated = money.copy()
        aggregated["sector_name"] = aggregated["ts_code"].map(lambda code: (stocks.get(code) or {}).get("industry"))
        aggregated = aggregated.dropna(subset=["sector_name"])
        top_sectors = (
            aggregated.groupby("sector_name", as_index=False)
            .agg(pct_chg=("pct_chg", "median"), main_net=("main_net", "sum"), main_pct=("main_pct", "mean"))
        )
        top_sectors["leading_stock"] = "按个股资金聚合"
        top_sectors["source"] = "个股大单资金聚合"
        top_sectors = top_sectors[top_sectors["main_net"] > 0].sort_values(["main_net", "pct_chg"], ascending=False).head(3)
    strong_sector_names = set(top_sectors["sector_name"]) if not top_sectors.empty else set()

    diagnostics = {}
    def diagnostic(code):
        if code not in diagnostics:
            diagnostics[code] = ambush_stock_diagnostics(price_frame(path, code))
        return diagnostics[code]

    combined_rows = []
    if not money.empty:
        valid = money[money.apply(lambda row: _tradable(row["ts_code"], row["name"]), axis=1)].copy()
        positive_max = max(float(valid["main_net"].max()), 1) if not valid.empty else 1
        sector_rank = {name: index for index, name in enumerate(top_sectors.get("sector_name", []), 1)}
        for row in valid.to_dict("records"):
            meta = stocks.get(row["ts_code"], {})
            industry = meta.get("industry") or "未分类"
            main_net, small_net = float(row.get("main_net") or 0), float(row.get("small_net") or 0)
            pct = float(row.get("pct_chg") or 0)
            three_main = row.get("three_day_main")
            base_conditions = main_net > 0 and small_net < 0 and -3 <= pct < 5
            if not base_conditions or industry not in strong_sector_names:
                continue
            diag = diagnostic(row["ts_code"])
            if not diag:
                continue
            if diag["average_amount"] < 100_000_000:
                continue
            three_day_ok = float(three_main) > 0 if three_main is not None and not pd.isna(three_main) else diag["acceptance"] > 0
            all_conditions = (
                three_day_ok and 1.1 <= diag["volume_ratio"] <= 2.5 and diag["position60"] <= .75
                and diag["close"] >= diag["ma60"] * .95 and diag["upper_shadow"] <= .04
                and diag["return5"] <= .15
            )
            if not all_conditions:
                continue
            blocked, risk_reason, fundamental_confidence = stock_risk(path, row["ts_code"], str(diag["trade_date"]))
            if blocked:
                continue
            sector_score = 1 - (sector_rank[industry] - 1) / max(len(strong_sector_names), 1)
            money_score = min(max(main_net, 0) / positive_max, 1)
            volume_score = max(0, 1 - abs(diag["volume_ratio"] - 1.5))
            position_score = 1 - diag["position60"]
            previous = previous_money.get(row["ts_code"])
            intraday_acceptance = .5 if not previous else float(
                main_net >= float(previous.get("main_net") or 0)
                and float(row.get("price") or 0) >= float(previous.get("price") or 0) * .995
            )
            tail_strength = intraday_acceptance if datetime.now().hour >= 14 else .5 * intraday_acceptance
            score = 30 * money_score + 25 * sector_score + 20 * volume_score + 15 * position_score + 10 * max((diag["acceptance"] + intraday_acceptance) / 2, 0)
            combined_rows.append({
                **row, "industry": industry, "volume_ratio": diag["volume_ratio"],
                "position60": diag["position60"], "confirm_price": diag["confirm_price"],
                "invalid_price": diag["invalid_price"], "strategy_score": round(score, 2),
                "fundamental_confidence": fundamental_confidence,
                "risk_check": risk_reason,
                "money_confidence": .75 if "代理" in str(row.get("source")) else 1.0,
                "intraday_acceptance": intraday_acceptance, "tail_strength": tail_strength,
                "reason": "全部满足：强势板块＋主力流入＋非大单流出＋涨幅<5%＋量能温和＋位置不高＋趋势未破坏",
            })

    def frame(rows):
        return pd.DataFrame(rows).sort_values("strategy_score", ascending=False).reset_index(drop=True) if rows else pd.DataFrame()
    return {"综合伏击候选": frame(combined_rows), "强势板块": top_sectors.reset_index(drop=True)}
