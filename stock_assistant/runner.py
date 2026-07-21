from __future__ import annotations

import argparse
from datetime import datetime, time
import json
import os
from pathlib import Path
import time as time_module
import urllib.request

import pandas as pd
from dotenv import load_dotenv

from .batch_sync import prepare_queue, queue_incremental_update, sync_market_batch
from .config import BASE_DIR, settings
from .data_sources import AkShareSource, BaoStockSource
from .database import execute, initialize, query, upsert_records
from .intraday import build_intraday_candidates, sync_intraday_data
from .safeguards import assess_data_quality, is_trading_day, sync_candidate_risks, sync_risk_announcements, sync_trade_calendar


load_dotenv(BASE_DIR / ".env")
SCAN_INTERVAL_MINUTES = max(5, int(os.getenv("SCAN_INTERVAL_MINUTES", "30")))
WECHAT_WEBHOOK = os.getenv("WECHAT_WEBHOOK", "http://127.0.0.1:9406/send-text")
WECHAT_TO = os.getenv("WECHAT_TO", "").strip()
MAX_PUSH_CANDIDATES = max(1, int(os.getenv("MAX_PUSH_CANDIDATES", "10")))
SCAN_TIMES = tuple(value.strip() for value in os.getenv("SCAN_TIMES", "09:45,10:30,11:20,14:00,14:45").split(",") if value.strip())


def _state(key: str) -> str | None:
    rows = query(settings.database_path, "SELECT value FROM scheduler_state WHERE key=?", (key,))
    return rows[0]["value"] if rows else None


def _set_state(key: str, value: str) -> None:
    upsert_records(settings.database_path, "scheduler_state", [{"key": key, "value": value}], ["key", "value"])


def _ensure_base_data(sync_pending: bool = True) -> None:
    if query(settings.database_path, "SELECT COUNT(*) n FROM stocks")[0]["n"] == 0:
        AkShareSource().sync_stocks(settings.database_path)
        try:
            BaoStockSource().sync_industries(settings.database_path)
        except Exception:
            pass
    prepare_queue(settings.database_path)
    pending = query(
        settings.database_path,
        "SELECT COUNT(*) n FROM stock_sync_status WHERE price_status='PENDING'",
    )[0]["n"]
    if pending and sync_pending:
        sync_market_batch(settings.database_path)


def _refresh_daily_if_due(now: datetime) -> None:
    today = now.strftime("%Y%m%d")
    phase = "close" if now.time() >= time(15, 35) else "morning"
    key = f"daily_{phase}"
    if _state(key) == today:
        return
    try:
        if _state("calendar_month") != today[:6]:
            sync_trade_calendar(settings.database_path)
            _set_state("calendar_month", today[:6])
    except Exception as error:
        print(f"[{datetime.now():%F %T}] 交易日历更新失败：{error}", flush=True)
    try:
        if _state("risk_notice") != today:
            sync_risk_announcements(settings.database_path, today)
            _set_state("risk_notice", today)
    except Exception as error:
        _set_state("risk_notice", today)
        print(f"[{datetime.now():%F %T}] 风险公告更新失败：{error}", flush=True)
    if phase == "close":
        _set_state("daily_close_attempt_at", now.isoformat(timespec="seconds"))
        queue_incremental_update(settings.database_path)
        sync_market_batch(settings.database_path)
    _set_state(key, today)


def _market_snapshot(path) -> dict:
    rows = query(path, "SELECT pct_chg FROM intraday_money_flow WHERE snapshot_at=(SELECT MAX(snapshot_at) FROM intraday_money_flow) AND pct_chg IS NOT NULL")
    if not rows:
        return {"state": "未知", "up_ratio": None, "median": None, "limit_count": 0, "broken_count": 0}
    values = pd.Series([float(row["pct_chg"]) for row in rows])
    up_ratio, median = float((values > 0).mean()), float(values.median())
    run = query(path, "SELECT limit_count,broken_count FROM intraday_sync_runs ORDER BY id DESC LIMIT 1")
    limit_count = int(run[0]["limit_count"] or 0) if run else 0
    broken_count = int(run[0]["broken_count"] or 0) if run else 0
    break_ratio = broken_count / max(limit_count + broken_count, 1)
    state = "退潮" if up_ratio <= .40 or median < -.5 or (limit_count + broken_count >= 10 and break_ratio >= .45) else "偏强" if up_ratio >= .55 and median > 0 and break_ratio < .30 else "震荡"
    return {"state": state, "up_ratio": up_ratio, "median": median, "limit_count": limit_count, "broken_count": broken_count}


def _track_candidates(path, run_at: str, candidates: pd.DataFrame, final: bool) -> tuple[pd.DataFrame, list[str]]:
    previous_runs = query(path, "SELECT id FROM recommendation_runs WHERE status='COMPLETED' AND substr(run_at,1,10)=? ORDER BY run_at", (run_at[:10],))
    histories = [
        {row["ts_code"]: dict(row) for row in query(path, "SELECT * FROM recommendation_items WHERE run_id=?", (run["id"],))}
        for run in previous_runs
    ]
    previous_codes = set(histories[-1]) if histories else set()
    current_codes = set(candidates["ts_code"]) if not candidates.empty else set()
    missing = previous_codes - current_codes
    exited = sorted(code for code in missing if histories[-1][code].get("lifecycle") == "待恢复") if histories else []
    if candidates.empty:
        result = pd.DataFrame()
    else:
        result = candidates.copy()
    # 首次掉出不立即删除，保留一个扫描周期作为“待恢复”；连续两次掉出才退出。
    carry = []
    if histories:
        for code in missing:
            old = histories[-1][code]
            if old.get("lifecycle") == "待恢复":
                continue
            carry.append({
                "ts_code": code, "name": old.get("name"), "industry": old.get("industry"),
                "strategy_score": old.get("score") or 0, "price": old.get("price"), "pct_chg": old.get("pct_chg"),
                "main_net": old.get("main_net") or 0, "main_pct": 0, "small_net": old.get("small_net") or 0,
                "volume_ratio": old.get("volume_ratio"), "position60": old.get("position60"),
                "confirm_price": old.get("confirm_price"), "invalid_price": old.get("invalid_price"),
                "source": old.get("source"), "reason": "本次未同时满足资金、板块或量价条件",
                "money_confidence": old.get("data_confidence") or .7, "fundamental_confidence": .7,
                "tail_strength": 0,
                "appearance_count": old.get("appearance_count") or 1, "lifecycle": "待恢复", "active": 0,
            })
    if carry:
        result = pd.concat([result, pd.DataFrame(carry)], ignore_index=True)
    if result.empty:
        return result, exited
    appearances, lifecycles, sector_rates = [], [], []
    for row in result.itertuples():
        if getattr(row, "lifecycle", None) == "待恢复":
            appearances.append(getattr(row, "appearance_count", 1))
            lifecycles.append("待恢复")
            sector_rates.append(0)
            continue
        count = 1 + sum(row.ts_code in snapshot for snapshot in histories)
        appearances.append(count)
        lifecycles.append("持续入选" if row.ts_code in previous_codes else ("新进入" if count == 1 else "重新进入"))
        hits = 1 + sum(any(item.get("industry") == row.industry for item in snapshot.values()) for snapshot in histories)
        sector_rates.append(hits / (len(histories) + 1))
    result["appearance_count"] = appearances
    result["lifecycle"] = lifecycles
    if "active" not in result:
        result["active"] = 1
    result["active"] = result["active"].fillna(1).astype(int)
    money_strength = result.get("main_pct", pd.Series(0, index=result.index)).rank(pct=True).fillna(.5)
    confidence = (result.get("money_confidence", pd.Series(.7, index=result.index)).fillna(.7) + result.get("fundamental_confidence", pd.Series(.7, index=result.index)).fillna(.7)) / 2
    result["data_confidence"] = confidence
    result["final_score"] = (
        result["strategy_score"].clip(0, 100) * .40
        + result["appearance_count"].clip(upper=5) * 4
        + money_strength * 15
        + pd.Series(sector_rates, index=result.index) * 10
        + result.get("tail_strength", pd.Series(0, index=result.index)).fillna(0) * 10
        + confidence * 5
    )
    return result.sort_values("final_score" if final else "strategy_score", ascending=False).reset_index(drop=True), exited


def _exit_reason(path, code: str, strong_sectors: set[str]) -> str:
    rows = query(path, "SELECT main_net,small_net,pct_chg FROM intraday_money_flow WHERE ts_code=? ORDER BY snapshot_at DESC LIMIT 1", (code,))
    stock = query(path, "SELECT industry FROM stocks WHERE ts_code=?", (code,))
    if rows:
        row = rows[0]
        if float(row["main_net"] or 0) <= 0:
            return "主力资金转负"
        if float(row["small_net"] or 0) >= 0:
            return "非大单资金未流出"
        if not -3 <= float(row["pct_chg"] or 0) < 5:
            return "涨跌幅离开伏击区间"
    if stock and str(stock[0]["industry"] or "") not in strong_sectors:
        return "板块跌出强势前三"
    return "量比、位置或趋势条件失效"


def _format_message(run_at: str, market: dict, candidates: pd.DataFrame, sync_result: dict, quality: dict, slot_label: str = "手动", final: bool = False, exited: list[str] | None = None, allowed: bool = False, catchup: bool = False) -> str:
    up = "--" if market["up_ratio"] is None else f"{market['up_ratio']:.1%}"
    median = "--" if market["median"] is None else f"{market['median']:.2f}%"
    lines = [
        f"【每日伏击股·{'尾盘最终名单' if final else '盘中观察'}】{run_at[5:16].replace('T', ' ')} {slot_label}{'（补跑）' if catchup else ''}",
        f"市场：{market['state']}｜上涨 {up}｜中位数 {median}｜涨停{market.get('limit_count', 0)}/炸板{market.get('broken_count', 0)}",
        f"数据：{quality['status']}·可信度{quality['confidence']:.0%}｜资金{sync_result['money']}｜板块{sync_result['sector']}",
    ]
    if quality["proxy"]:
        lines.append("注意：当前为新浪大单代理口径，“非大单”不等同于真实散户资金。")
    if quality["issues"]:
        lines.append("数据告警：" + "；".join(quality["issues"]))
    active = candidates[candidates.get("active", 1) == 1] if not candidates.empty else candidates
    formal = active[active.get("formal", 0) == 1] if not active.empty else active
    if not allowed:
        lines.append("本次不生成正式推荐，仅保留观察池。")
    shown = formal if final and allowed else active
    if shown.empty:
        lines.append("本次没有同时满足全部条件的股票。")
    else:
        for index, row in enumerate(shown.head(3 if final else MAX_PUSH_CANDIDATES).itertuples(), 1):
            lines.extend([
                f"\n{index}. [{getattr(row, 'lifecycle', '新进入')}·{getattr(row, 'appearance_count', 1)}次] {row.name} {row.ts_code}｜评分 {(getattr(row, 'final_score', row.strategy_score) if final else row.strategy_score):.1f}",
                f"现价 {row.price:.2f}｜涨幅 {row.pct_chg:.2f}%｜量比 {row.volume_ratio:.2f}｜60日位置 {row.position60:.0%}",
                f"确认 {row.confirm_price:.3f}｜失效 {row.invalid_price:.3f}｜{row.industry}",
            ])
    pending = candidates[candidates.get("active", 1) == 0] if not candidates.empty else candidates
    if not pending.empty:
        lines.append("\n待恢复：" + "、".join(f"{row.name}({row.ts_code})" for row in pending.head(10).itertuples()))
    if exited:
        strong = set()
        lines.append("\n已退出：" + "；".join(f"{code}({_exit_reason(settings.database_path, code, strong)})" for code in exited[:10]))
    if not final:
        lines.append("\n盘中结果仅用于跟踪，14:45 以全天持续性生成最终名单。")
    lines.append("\n仅供研究，不构成投资建议。")
    return "\n".join(lines)


def _push_wechat(text: str) -> None:
    if not WECHAT_TO:
        raise RuntimeError("未配置 WECHAT_TO")
    body = json.dumps({"to": WECHAT_TO, "text": text}, ensure_ascii=False).encode("utf-8")
    last_error = None
    for delay in (0, 3, 10):
        if delay:
            time_module.sleep(delay)
        try:
            request = urllib.request.Request(WECHAT_WEBHOOK, data=body, headers={"Content-Type": "application/json"}, method="POST")
            response = json.loads(urllib.request.urlopen(request, timeout=20).read())
            if not response.get("ok"):
                raise RuntimeError(str(response))
            return
        except Exception as error:
            last_error = error
    raise RuntimeError(f"微信推送失败：{last_error}")


def _queue_and_deliver(push_key: str, run_id: int | None, message: str) -> bool:
    now = datetime.now().isoformat(timespec="seconds")
    execute(settings.database_path, "INSERT OR IGNORE INTO push_outbox(push_key,run_id,text,created_at) VALUES(?,?,?,?)", (push_key, run_id, message, now))
    row = query(settings.database_path, "SELECT status,text,attempts FROM push_outbox WHERE push_key=?", (push_key,))[0]
    if row["status"] == "SENT":
        return True
    try:
        _push_wechat(row["text"])
        execute(settings.database_path, "UPDATE push_outbox SET status='SENT',attempts=attempts+1,last_error=NULL,sent_at=? WHERE push_key=?", (now, push_key))
        return True
    except Exception as error:
        execute(settings.database_path, "UPDATE push_outbox SET status='PENDING',attempts=attempts+1,last_error=? WHERE push_key=?", (str(error)[:1000], push_key))
        return False


def _retry_outbox() -> None:
    rows = query(settings.database_path, "SELECT push_key,run_id,text FROM push_outbox WHERE status='PENDING' AND attempts<10 ORDER BY created_at LIMIT 5")
    for row in rows:
        sent = _queue_and_deliver(row["push_key"], row["run_id"], row["text"])
        if sent and row["run_id"]:
            execute(settings.database_path, "UPDATE recommendation_runs SET pushed=1,push_status='SENT' WHERE id=?", (row["run_id"],))


def _changes(path, run_id: int, candidates: pd.DataFrame, market: dict) -> tuple[bool, str]:
    prior = query(path, "SELECT id,market_state FROM recommendation_runs WHERE id<? AND status='COMPLETED' ORDER BY id DESC LIMIT 1", (run_id,))
    if not prior:
        return True, "首次扫描"
    old = {row["ts_code"]: dict(row) for row in query(path, "SELECT * FROM recommendation_items WHERE run_id=? AND active=1", (prior[0]["id"],))}
    current = {row["ts_code"]: row for row in candidates[candidates["active"] == 1].to_dict("records")} if not candidates.empty else {}
    new, gone = set(current) - set(old), set(old) - set(current)
    score_changed = any(abs(float(current[code].get("final_score") or 0) - float(old[code].get("final_score") or 0)) >= 5 for code in set(current) & set(old))
    state_changed = prior[0]["market_state"] != market["state"]
    parts = []
    if new:
        parts.append(f"新增{len(new)}")
    if gone:
        parts.append(f"减少{len(gone)}")
    if score_changed:
        parts.append("评分明显变化")
    if state_changed:
        parts.append(f"市场转为{market['state']}")
    return bool(parts), "、".join(parts) or "名单与评分无明显变化"


def run_once(push: bool = True, refresh_daily: bool = True, slot_label: str = "手动", final: bool = False, catchup: bool = False) -> int:
    initialize(settings.database_path)
    run_at = datetime.now().isoformat(timespec="seconds")
    run_id = execute(settings.database_path, "INSERT INTO recommendation_runs(run_at,status,slot_label,is_final,is_catchup) VALUES(?,?,?,?,?)", (run_at, "RUNNING", slot_label, int(final), int(catchup)))
    try:
        _ensure_base_data(sync_pending=False)
        if refresh_daily:
            _refresh_daily_if_due(datetime.now())
        sync_result = sync_intraday_data(settings.database_path)
        quality = assess_data_quality(settings.database_path, sync_result, datetime.now())
        candidate_result = build_intraday_candidates(settings.database_path)
        candidates = candidate_result["综合伏击候选"]
        if not candidates.empty:
            sync_candidate_risks(settings.database_path, candidates["ts_code"].tolist(), run_at[:10].replace("-", ""))
            candidates = build_intraday_candidates(settings.database_path)["综合伏击候选"]
        candidates, exited = _track_candidates(settings.database_path, run_at, candidates, final)
        market = _market_snapshot(settings.database_path)
        allowed = quality["status"] != "BLOCKED" and market["state"] != "退潮"
        if not candidates.empty:
            candidates["formal"] = 0
            if allowed:
                active_index = candidates[candidates["active"] == 1].head(3 if market["state"] == "偏强" else 1).index
                candidates.loc[active_index, "formal"] = 1
        changed, change_summary = _changes(settings.database_path, run_id, candidates, market)
        message = _format_message(run_at, market, candidates, sync_result, quality, slot_label, final, exited, allowed, catchup)
        records = []
        for rank_no, row in enumerate(candidates.to_dict("records"), 1):
            records.append({
                "run_id": run_id, "rank_no": rank_no, "ts_code": row["ts_code"], "name": row.get("name"),
                "industry": row.get("industry"), "score": row.get("strategy_score"), "price": row.get("price"),
                "pct_chg": row.get("pct_chg"), "main_net": row.get("main_net"), "small_net": row.get("small_net"),
                "volume_ratio": row.get("volume_ratio"), "position60": row.get("position60"),
                "confirm_price": row.get("confirm_price"), "invalid_price": row.get("invalid_price"),
                "source": row.get("source"), "reason": row.get("reason"),
                "appearance_count": row.get("appearance_count", 1), "lifecycle": row.get("lifecycle"),
                "final_score": row.get("final_score"), "data_confidence": row.get("data_confidence"),
                "exit_reason": row.get("reason") if not row.get("active", 1) else None,
                "active": row.get("active", 1), "formal": row.get("formal", 0),
            })
        upsert_records(settings.database_path, "recommendation_items", records, list(records[0]) if records else [])
        pushed = 0
        push_key = f"recommendation:{run_at[:10]}:{slot_label}" if slot_label != "手动" else f"recommendation:manual:{run_id}"
        push_status = "SKIPPED"
        if push and (final or changed):
            pushed = int(_queue_and_deliver(push_key, run_id, message))
            push_status = "SENT" if pushed else "PENDING"
        execute(
            settings.database_path,
            """UPDATE recommendation_runs SET status='COMPLETED',market_state=?,up_ratio=?,median_pct=?,candidate_count=?,pushed=?,message=?,
            data_status=?,data_confidence=?,recommendation_allowed=?,change_summary=?,push_key=?,push_status=?,push_attempts=COALESCE((SELECT attempts FROM push_outbox WHERE push_key=?),0) WHERE id=?""",
            (market["state"], market["up_ratio"], market["median"], int(candidates["active"].sum()) if not candidates.empty else 0, pushed, message,
             quality["status"], quality["confidence"], int(allowed), change_summary, push_key, push_status, push_key, run_id),
        )
        return run_id
    except Exception as error:
        execute(settings.database_path, "UPDATE recommendation_runs SET status='FAILED',error=? WHERE id=?", (str(error)[:2000], run_id))
        raise


def _due_scan(now: datetime) -> tuple[str, bool, bool] | None:
    if now.weekday() >= 5:
        return None
    if time(11, 30) < now.time() < time(13, 0) or now.time() > time(15, 0):
        return None
    due = []
    for label in SCAN_TIMES:
        scheduled = datetime.strptime(f"{now:%F} {label}", "%Y-%m-%d %H:%M")
        age = (now - scheduled).total_seconds() / 60
        max_age = 15 if label == "14:45" else 45
        if 0 <= age <= max_age:
            due.append((scheduled, label, label == "14:45", age > 5))
    return (due[-1][1], due[-1][2], due[-1][3]) if due else None


def _send_daily_health(now: datetime) -> None:
    day = now.strftime("%Y%m%d")
    if _state("health_push") == day:
        return
    latest = query(settings.database_path, "SELECT data_status,data_confidence FROM recommendation_runs WHERE status='COMPLETED' ORDER BY id DESC LIMIT 1")
    scans = query(settings.database_path, "SELECT COUNT(*) n,SUM(CASE WHEN pushed=1 THEN 1 ELSE 0 END) pushed FROM recommendation_runs WHERE substr(run_at,1,10)=? AND status='COMPLETED'", (now.strftime("%Y-%m-%d"),))[0]
    coverage = query(settings.database_path, "SELECT COUNT(DISTINCT ts_code) n FROM daily_prices WHERE trade_date=(SELECT MAX(trade_date) FROM daily_prices)")[0]["n"]
    pending = query(settings.database_path, "SELECT COUNT(*) n FROM push_outbox WHERE status='PENDING'")[0]["n"]
    status = latest[0]["data_status"] if latest else "无扫描"
    confidence = float(latest[0]["data_confidence"] or 0) if latest else 0
    message = f"【每日伏击股·服务日报】\n服务正常｜数据{status}·{confidence:.0%}\n行情覆盖：{coverage}｜今日扫描：{scans['n']}/5｜推送成功：{scans['pushed'] or 0}｜待重试：{pending}"
    if _queue_and_deliver(f"health:{day}", None, message):
        _set_state("health_push", day)


def _close_refresh_due(now: datetime) -> bool:
    if _state("daily_close") == now.strftime("%Y%m%d"):
        return False
    attempted = _state("daily_close_attempt_at")
    if not attempted:
        return True
    try:
        return (now - datetime.fromisoformat(attempted)).total_seconds() >= 1800
    except ValueError:
        return True


def run_daemon() -> None:
    initialize(settings.database_path)
    try:
        sync_trade_calendar(settings.database_path)
    except Exception as error:
        print(f"[{datetime.now():%F %T}] 交易日历初始化失败：{error}", flush=True)
    while query(settings.database_path, "SELECT COUNT(*) n FROM daily_prices")[0]["n"] == 0:
        prepare_queue(settings.database_path)
        pending = query(
            settings.database_path,
            "SELECT COUNT(*) n FROM stock_sync_status WHERE price_status='PENDING'",
        )[0]["n"]
        if not pending:
            break
        try:
            print(f"[{datetime.now():%F %T}] 正在初始化独立股票、行业和日线数据", flush=True)
            _ensure_base_data()
        except Exception as error:
            print(f"[{datetime.now():%F %T}] 首次初始化失败，60秒后重试：{error}", flush=True)
            time_module.sleep(60)
    if query(settings.database_path, "SELECT COUNT(*) n FROM recommendation_runs")[0]["n"] == 0:
        try:
            run_once(push=True, refresh_daily=False)
        except Exception as error:
            print(f"[{datetime.now():%F %T}] 首次推荐失败，将在下个时段重试：{error}", flush=True)
    while True:
        now = datetime.now()
        due = _due_scan(now) if is_trading_day(settings.database_path, now) else None
        try:
            _retry_outbox()
            label, final, catchup = due if due else ("", False, False)
            slot = f"{now:%Y%m%d}-{label}" if due else ""
            if due and _state(f"scan_{slot}") != "DONE":
                run_once(push=True, refresh_daily=True, slot_label=label, final=final, catchup=catchup)
                _set_state(f"scan_{slot}", "DONE")
            elif is_trading_day(settings.database_path, now) and now.time() >= time(15, 35) and _close_refresh_due(now):
                try:
                    _refresh_daily_if_due(now)
                finally:
                    # 即使日线源失败，也必须告知用户当日服务和数据状态。
                    _send_daily_health(now)
        except Exception as error:
            print(f"[{datetime.now():%F %T}] {error}", flush=True)
        time_module.sleep(60)


def main() -> None:
    parser = argparse.ArgumentParser(description="每日伏击股后台任务")
    parser.add_argument("--once", action="store_true", help="立即执行一次")
    parser.add_argument("--no-push", action="store_true", help="仅保存结果，不推送微信")
    args = parser.parse_args()
    if args.once:
        print(f"recommendation_run={run_once(push=not args.no_push)}")
    else:
        run_daemon()


if __name__ == "__main__":
    main()
