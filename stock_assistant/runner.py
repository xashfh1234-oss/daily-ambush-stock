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


load_dotenv(BASE_DIR / ".env")
SCAN_INTERVAL_MINUTES = max(5, int(os.getenv("SCAN_INTERVAL_MINUTES", "30")))
WECHAT_WEBHOOK = os.getenv("WECHAT_WEBHOOK", "http://127.0.0.1:9406/send-text")
WECHAT_TO = os.getenv("WECHAT_TO", "").strip()
MAX_PUSH_CANDIDATES = max(1, int(os.getenv("MAX_PUSH_CANDIDATES", "10")))


def _state(key: str) -> str | None:
    rows = query(settings.database_path, "SELECT value FROM scheduler_state WHERE key=?", (key,))
    return rows[0]["value"] if rows else None


def _set_state(key: str, value: str) -> None:
    upsert_records(settings.database_path, "scheduler_state", [{"key": key, "value": value}], ["key", "value"])


def _ensure_base_data() -> None:
    if query(settings.database_path, "SELECT COUNT(*) n FROM stocks")[0]["n"] == 0:
        AkShareSource().sync_stocks(settings.database_path)
        try:
            BaoStockSource().sync_industries(settings.database_path)
        except Exception:
            pass
    prepare_queue(settings.database_path)
    if query(settings.database_path, "SELECT COUNT(*) n FROM daily_prices")[0]["n"] == 0:
        sync_market_batch(settings.database_path)


def _refresh_daily_if_due(now: datetime) -> None:
    today = now.strftime("%Y%m%d")
    phase = "close" if now.time() >= time(15, 35) else "morning"
    key = f"daily_{phase}"
    if _state(key) == today:
        return
    queue_incremental_update(settings.database_path)
    sync_market_batch(settings.database_path)
    _set_state(key, today)


def _market_snapshot(path) -> dict:
    rows = query(path, "SELECT pct_chg FROM intraday_money_flow WHERE snapshot_at=(SELECT MAX(snapshot_at) FROM intraday_money_flow) AND pct_chg IS NOT NULL")
    if not rows:
        return {"state": "未知", "up_ratio": None, "median": None}
    values = pd.Series([float(row["pct_chg"]) for row in rows])
    up_ratio, median = float((values > 0).mean()), float(values.median())
    state = "退潮" if up_ratio <= .40 or median < -.5 else "偏强" if up_ratio >= .55 and median > 0 else "震荡"
    return {"state": state, "up_ratio": up_ratio, "median": median}


def _format_message(run_at: str, market: dict, candidates: pd.DataFrame, sync_result: dict) -> str:
    up = "--" if market["up_ratio"] is None else f"{market['up_ratio']:.1%}"
    median = "--" if market["median"] is None else f"{market['median']:.2f}%"
    lines = [
        f"【每日伏击股】{run_at[5:16].replace('T', ' ')}",
        f"市场：{market['state']}｜上涨占比 {up}｜涨跌中位数 {median}",
        f"数据：资金 {sync_result['money']}｜板块 {sync_result['sector']}｜候选 {len(candidates)}",
    ]
    if candidates.empty:
        lines.append("本次没有同时满足全部条件的股票。")
    else:
        for index, row in enumerate(candidates.head(MAX_PUSH_CANDIDATES).itertuples(), 1):
            lines.extend([
                f"\n{index}. {row.name} {row.ts_code}｜评分 {row.strategy_score:.1f}",
                f"现价 {row.price:.2f}｜涨幅 {row.pct_chg:.2f}%｜量比 {row.volume_ratio:.2f}｜60日位置 {row.position60:.0%}",
                f"确认 {row.confirm_price:.3f}｜失效 {row.invalid_price:.3f}｜{row.industry}",
            ])
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


def run_once(push: bool = True, refresh_daily: bool = True) -> int:
    initialize(settings.database_path)
    run_at = datetime.now().isoformat(timespec="seconds")
    run_id = execute(settings.database_path, "INSERT INTO recommendation_runs(run_at,status) VALUES(?,?)", (run_at, "RUNNING"))
    try:
        _ensure_base_data()
        if refresh_daily:
            _refresh_daily_if_due(datetime.now())
        sync_result = sync_intraday_data(settings.database_path)
        candidates = build_intraday_candidates(settings.database_path)["综合伏击候选"]
        market = _market_snapshot(settings.database_path)
        message = _format_message(run_at, market, candidates, sync_result)
        records = []
        for rank_no, row in enumerate(candidates.to_dict("records"), 1):
            records.append({
                "run_id": run_id, "rank_no": rank_no, "ts_code": row["ts_code"], "name": row.get("name"),
                "industry": row.get("industry"), "score": row.get("strategy_score"), "price": row.get("price"),
                "pct_chg": row.get("pct_chg"), "main_net": row.get("main_net"), "small_net": row.get("small_net"),
                "volume_ratio": row.get("volume_ratio"), "position60": row.get("position60"),
                "confirm_price": row.get("confirm_price"), "invalid_price": row.get("invalid_price"),
                "source": row.get("source"), "reason": row.get("reason"),
            })
        upsert_records(settings.database_path, "recommendation_items", records, list(records[0]) if records else [])
        pushed = 0
        if push:
            _push_wechat(message)
            pushed = 1
        execute(
            settings.database_path,
            """UPDATE recommendation_runs SET status='COMPLETED',market_state=?,up_ratio=?,median_pct=?,candidate_count=?,pushed=?,message=? WHERE id=?""",
            (market["state"], market["up_ratio"], market["median"], len(candidates), pushed, message, run_id),
        )
        return run_id
    except Exception as error:
        execute(settings.database_path, "UPDATE recommendation_runs SET status='FAILED',error=? WHERE id=?", (str(error)[:2000], run_id))
        raise


def _in_market_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    current = now.time()
    return time(9, 35) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0)


def run_daemon() -> None:
    initialize(settings.database_path)
    while query(settings.database_path, "SELECT COUNT(*) n FROM daily_prices")[0]["n"] == 0:
        try:
            print(f"[{datetime.now():%F %T}] 正在初始化独立股票、行业和日线数据", flush=True)
            _ensure_base_data()
        except Exception as error:
            print(f"[{datetime.now():%F %T}] 首次初始化失败，60秒后重试：{error}", flush=True)
            time_module.sleep(60)
    while True:
        now = datetime.now()
        slot = f"{now:%Y%m%d%H}{now.minute // SCAN_INTERVAL_MINUTES}"
        try:
            if _in_market_window(now) and _state("last_scan_slot") != slot:
                run_once(push=True, refresh_daily=True)
                _set_state("last_scan_slot", slot)
            elif now.weekday() < 5 and now.time() >= time(15, 35) and _state("daily_close") != now.strftime("%Y%m%d"):
                _ensure_base_data()
                _refresh_daily_if_due(now)
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
