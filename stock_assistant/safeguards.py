from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from .data_sources import normalize_code
from .database import query, upsert_records


RISK_KEYWORDS = {
    "减持": ("减持", "拟减持"),
    "监管": ("立案", "调查", "监管", "处罚", "警示函"),
    "退市": ("退市风险", "终止上市", "可能被实施退市"),
    "业绩": ("预亏", "亏损", "大幅下降", "业绩变脸"),
    "解禁": ("解禁", "限售股上市流通"),
    "诉讼": ("重大诉讼", "重大仲裁"),
    "复牌": ("复牌",),
}


def sync_trade_calendar(path) -> int:
    import akshare as ak

    frame = ak.tool_trade_date_hist_sina()
    records = []
    for value in frame.iloc[:, 0].tolist():
        day = pd.Timestamp(value).strftime("%Y%m%d")
        records.append({"exchange": "SSE", "cal_date": day, "is_open": 1, "pretrade_date": None})
    return upsert_records(path, "trade_calendar", records, ["exchange", "cal_date", "is_open", "pretrade_date"])


def is_trading_day(path, now: datetime) -> bool:
    day = now.strftime("%Y%m%d")
    rows = query(path, "SELECT is_open FROM trade_calendar WHERE exchange='SSE' AND cal_date=?", (day,))
    return bool(rows[0]["is_open"]) if rows else now.weekday() < 5


def expected_daily_date(path, now: datetime) -> str | None:
    today = now.strftime("%Y%m%d")
    include_today = now.time() >= datetime.strptime("15:30", "%H:%M").time()
    operator = "<=" if include_today else "<"
    rows = query(path, f"SELECT MAX(cal_date) d FROM trade_calendar WHERE exchange='SSE' AND is_open=1 AND cal_date{operator}?", (today,))
    if rows and rows[0]["d"]:
        return rows[0]["d"]
    day = now.date() if include_today else now.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.strftime("%Y%m%d")


def assess_data_quality(path, sync_result: dict, now: datetime) -> dict:
    expected = expected_daily_date(path, now)
    latest = query(path, "SELECT MAX(trade_date) d FROM daily_prices")[0]["d"]
    coverage = query(path, "SELECT COUNT(DISTINCT ts_code) n FROM daily_prices WHERE trade_date=?", (expected,))[0]["n"] if expected else 0
    money = int(sync_result.get("money", 0))
    sector = int(sync_result.get("sector", 0))
    snapshot = pd.Timestamp(sync_result.get("snapshot_at")) if sync_result.get("snapshot_at") else None
    age_minutes = (pd.Timestamp(now) - snapshot).total_seconds() / 60 if snapshot is not None else 999
    source_rows = query(path, "SELECT source,COUNT(*) n FROM intraday_money_flow WHERE snapshot_at=? GROUP BY source", (sync_result.get("snapshot_at"),))
    sources = [row["source"] for row in source_rows]
    proxy = any("代理" in str(source) for source in sources)
    issues = []
    if not coverage:
        issues.append(f"缺少应检查交易日{expected or '未知'}的日线")
    if coverage < 3000:
        issues.append(f"日线覆盖不足({coverage})")
    if money < 1000:
        issues.append(f"资金覆盖不足({money})")
    if sector < 20:
        issues.append(f"板块覆盖不足({sector})")
    if age_minutes > 15:
        issues.append(f"盘中快照延迟{age_minutes:.0f}分钟")
    confidence = 1.0
    confidence -= .2 if proxy else 0
    confidence -= .2 if not coverage else 0
    confidence -= .2 if coverage < 3000 else 0
    confidence -= .2 if money < 1000 else 0
    confidence -= .1 if sector < 20 else 0
    confidence = max(0, confidence)
    blocking = coverage < 3000 or money < 1000 or sector < 20 or age_minutes > 15
    return {
        "status": "BLOCKED" if blocking else ("DEGRADED" if proxy or sync_result.get("status") != "COMPLETED" else "OK"),
        "confidence": confidence, "issues": issues, "sources": sources, "proxy": proxy,
        "latest_daily": latest, "expected_daily": expected, "coverage": coverage,
    }


def sync_risk_announcements(path, day: str) -> int:
    import akshare as ak

    frame = ak.stock_notice_report(symbol="全部", date=day)
    if frame.empty:
        return 0
    records = []
    for row in frame.to_dict("records"):
        title = str(row.get("公告标题") or row.get("标题") or "")
        code = str(row.get("代码") or row.get("股票代码") or "").zfill(6)
        if not title or len(code) != 6:
            continue
        for risk_type, words in RISK_KEYWORDS.items():
            if any(word in title for word in words):
                try:
                    _, ts_code, _ = normalize_code(code)
                except ValueError:
                    continue
                records.append({
                    "ts_code": ts_code, "event_date": day, "risk_type": risk_type,
                    "title": title, "source": "东方财富公告", "expires_at": (pd.Timestamp(day) + pd.Timedelta(days=90)).strftime("%Y%m%d"),
                })
    return upsert_records(path, "risk_events", records, ["ts_code", "event_date", "risk_type", "title", "source", "expires_at"])


def sync_candidate_risks(path, codes: list[str], day: str) -> int:
    """候选股公告二次校验：东方财富全市场公告失败时，使用巨潮逐只降级查询。"""
    import akshare as ak

    records = []
    start = (pd.Timestamp(day) - pd.Timedelta(days=90)).strftime("%Y%m%d")
    for code in codes[:20]:
        digits = code.split(".")[0]
        try:
            frame = ak.stock_zh_a_disclosure_report_cninfo(symbol=digits, market="沪深京", start_date=start, end_date=day)
        except Exception:
            continue
        for row in frame.to_dict("records"):
            title = str(row.get("公告标题") or row.get("标题") or "")
            event_date = pd.to_datetime(row.get("公告时间") or row.get("公告日期") or day, errors="coerce")
            event_date = day if pd.isna(event_date) else event_date.strftime("%Y%m%d")
            for risk_type, words in RISK_KEYWORDS.items():
                if any(word in title for word in words):
                    records.append({
                        "ts_code": code, "event_date": event_date, "risk_type": risk_type, "title": title,
                        "source": "巨潮资讯公告", "expires_at": (pd.Timestamp(event_date) + pd.Timedelta(days=90)).strftime("%Y%m%d"),
                    })
    return upsert_records(path, "risk_events", records, ["ts_code", "event_date", "risk_type", "title", "source", "expires_at"])


def stock_risk(path, code: str, as_of: str) -> tuple[bool, str, float]:
    stock = query(path, "SELECT name,list_date FROM stocks WHERE ts_code=?", (code,))
    if not stock:
        return True, "股票基础信息缺失", .5
    row = stock[0]
    name = str(row["name"] or "")
    if "ST" in name.upper() or "退" in name:
        return True, "ST或退市风险", 1.0
    if row["list_date"] and row["list_date"] != "19900101" and (pd.Timestamp(as_of) - pd.Timestamp(row["list_date"])).days < 365:
        return True, "上市不足一年", 1.0
    events = query(path, "SELECT risk_type,title FROM risk_events WHERE ts_code=? AND event_date<=? AND (expires_at IS NULL OR expires_at>=?) ORDER BY event_date DESC", (code, as_of, as_of))
    if events:
        return True, f"{events[0]['risk_type']}：{events[0]['title']}", 1.0
    basic = query(path, "SELECT pe_ttm FROM daily_basic WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1", (code,))
    if basic and basic[0]["pe_ttm"] is not None and float(basic[0]["pe_ttm"]) < 0:
        return True, "PE为负，公司亏损", 1.0
    financial = query(path, "SELECT netprofit_yoy FROM financial_indicators WHERE ts_code=? ORDER BY ann_date DESC LIMIT 2", (code,))
    if len(financial) >= 2 and all(item["netprofit_yoy"] is not None and float(item["netprofit_yoy"]) <= -50 for item in financial):
        return True, "连续两期净利润同比大幅下降", .9
    confidence = 1.0 if basic or financial else .7
    return False, "未发现已知风险" if confidence == 1 else "基本面数据不完整", confidence
