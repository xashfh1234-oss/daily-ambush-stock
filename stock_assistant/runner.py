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
SCAN_TIMES = tuple(value.strip() for value in os.getenv("SCAN_TIMES", "09:45,10:30,11:20,14:00,14:45").split(",") if value.strip())


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
    pending = query(
        settings.database_path,
        "SELECT COUNT(*) n FROM stock_sync_status WHERE price_status IN ('PENDING', 'FAILED')",
    )[0]["n"]
    if pending:
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


def _track_candidates(path, run_at: str, candidates: pd.DataFrame, final: bool) -> tuple[pd.DataFrame, list[str]]:
    previous_runs = query(path, "SELECT id FROM recommendation_runs WHERE status='COMPLETED' AND substr(run_at,1,10)=? ORDER BY run_at", (run_at[:10],))
    histories = [
        {row["ts_code"]: dict(row) for row in query(path, "SELECT * FROM recommendation_items WHERE run_id=?", (run["id"],))}
        for run in previous_runs
    ]
    previous_codes = set(histories[-1]) if histories else set()
    current_codes = set(candidates["ts_code"]) if not candidates.empty else set()
    exited = sorted(previous_codes - current_codes)
    if candidates.empty:
        return candidates.copy(), exited
    result = candidates.copy()
    appearances, lifecycles, sector_rates = [], [], []
    for row in result.itertuples():
        count = 1 + sum(row.ts_code in snapshot for snapshot in histories)
        appearances.append(count)
        lifecycles.append("持续入选" if row.ts_code in previous_codes else ("新进入" if count == 1 else "重新进入"))
        hits = 1 + sum(any(item.get("industry") == row.industry for item in snapshot.values()) for snapshot in histories)
        sector_rates.append(hits / (len(histories) + 1))
    result["appearance_count"] = appearances
    result["lifecycle"] = lifecycles
    result["final_score"] = (
        result["strategy_score"].clip(0, 100) * .50
        + result["appearance_count"].clip(upper=5) * 5
        + result["main_net"].rank(pct=True).fillna(.5) * 15
        + pd.Series(sector_rates, index=result.index) * 10
    )
    return result.sort_values("final_score" if final else "strategy_score", ascending=False).reset_index(drop=True), exited


def _format_message(run_at: str, market: dict, candidates: pd.DataFrame, sync_result: dict, slot_label: str = "手动", final: bool = False, exited: list[str] | None = None) -> str:
    up = "--" if market["up_ratio"] is None else f"{market['up_ratio']:.1%}"
    median = "--" if market["median"] is None else f"{market['median']:.2f}%"
    lines = [
        f"【每日伏击股·{'尾盘最终名单' if final else '盘中观察'}】{run_at[5:16].replace('T', ' ')} {slot_label}",
        f"市场：{market['state']}｜上涨占比 {up}｜涨跌中位数 {median}",
        f"数据：资金 {sync_result['money']}｜板块 {sync_result['sector']}｜候选 {len(candidates)}",
    ]
    if candidates.empty:
        lines.append("本次没有同时满足全部条件的股票。")
    else:
        for index, row in enumerate(candidates.head(3 if final else MAX_PUSH_CANDIDATES).itertuples(), 1):
            lines.extend([
                f"\n{index}. [{getattr(row, 'lifecycle', '新进入')}·{getattr(row, 'appearance_count', 1)}次] {row.name} {row.ts_code}｜评分 {(getattr(row, 'final_score', row.strategy_score) if final else row.strategy_score):.1f}",
                f"现价 {row.price:.2f}｜涨幅 {row.pct_chg:.2f}%｜量比 {row.volume_ratio:.2f}｜60日位置 {row.position60:.0%}",
                f"确认 {row.confirm_price:.3f}｜失效 {row.invalid_price:.3f}｜{row.industry}",
            ])
    if exited:
        lines.append(f"\n已退出：{', '.join(exited[:10])}")
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


def run_once(push: bool = True, refresh_daily: bool = True, slot_label: str = "手动", final: bool = False) -> int:
    initialize(settings.database_path)
    run_at = datetime.now().isoformat(timespec="seconds")
    run_id = execute(settings.database_path, "INSERT INTO recommendation_runs(run_at,status,slot_label,is_final) VALUES(?,?,?,?)", (run_at, "RUNNING", slot_label, int(final)))
    try:
        _ensure_base_data()
        if refresh_daily:
            _refresh_daily_if_due(datetime.now())
        sync_result = sync_intraday_data(settings.database_path)
        candidates = build_intraday_candidates(settings.database_path)["综合伏击候选"]
        candidates, exited = _track_candidates(settings.database_path, run_at, candidates, final)
        market = _market_snapshot(settings.database_path)
        message = _format_message(run_at, market, candidates, sync_result, slot_label, final, exited)
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
                "final_score": row.get("final_score"),
            })
        upsert_records(settings.database_path, "recommendation_items", records, list(records[0]) if records else [])
        pushed = 0
        prior = query(settings.database_path, "SELECT id FROM recommendation_runs WHERE id<? AND status='COMPLETED' ORDER BY id DESC LIMIT 1", (run_id,))
        prior_codes = {row["ts_code"] for row in query(settings.database_path, "SELECT ts_code FROM recommendation_items WHERE run_id=?", (prior[0]["id"],))} if prior else set()
        current_codes = set(candidates["ts_code"]) if not candidates.empty else set()
        if push and (final or current_codes != prior_codes or not prior):
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


def _due_scan(now: datetime) -> tuple[str, bool] | None:
    if now.weekday() >= 5:
        return None
    for label in SCAN_TIMES:
        scheduled = datetime.strptime(f"{now:%F} {label}", "%Y-%m-%d %H:%M")
        if 0 <= (now - scheduled).total_seconds() < 300:
            return label, label == "14:45"
    return None


def run_daemon() -> None:
    initialize(settings.database_path)
    while True:
        prepare_queue(settings.database_path)
        pending = query(
            settings.database_path,
            "SELECT COUNT(*) n FROM stock_sync_status WHERE price_status IN ('PENDING', 'FAILED')",
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
        due = _due_scan(now)
        try:
            label, final = due if due else ("", False)
            slot = f"{now:%Y%m%d}-{label}" if due else ""
            if due and _state("last_scan_slot") != slot:
                run_once(push=True, refresh_daily=True, slot_label=label, final=final)
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
