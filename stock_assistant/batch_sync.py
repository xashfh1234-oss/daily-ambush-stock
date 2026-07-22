from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import date, timedelta
import os
import signal
import time
from typing import Callable

import pandas as pd

from .data_sources import BaoStockSource, normalize_code
from .database import execute, initialize, query, upsert_records
from .free_market_sources import TdxDailyClient, fetch_tencent_daily
from .market import build_scores


ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class BatchSummary:
    processed: int
    succeeded: int
    failed: int
    eligible: int


class StockSyncTimeout(TimeoutError):
    pass


PRICE_HISTORY_TARGET = 250
PRICE_HISTORY_CALENDAR_DAYS = 420


def _run_with_timeout(seconds: int, operation, *args, **kwargs):
    def timeout_handler(_signum, _frame):
        raise StockSyncTimeout(f"单只股票数据请求超过{seconds}秒")

    previous = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(seconds)
    try:
        return operation(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


_WORKER_BAOSTOCK = None
_WORKER_TDX = None


def _baostock_worker_initialize() -> None:
    global _WORKER_BAOSTOCK
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock工作进程登录失败：{login.error_msg}")
    _WORKER_BAOSTOCK = bs


def _baostock_healthcheck(attempts: int = 3) -> None:
    """Fail the batch as a whole when the upstream service is unavailable."""
    import baostock as bs

    last_error = "未知错误"
    for attempt in range(attempts):
        login = bs.login()
        if login.error_code == "0":
            bs.logout()
            return
        last_error = login.error_msg
        if attempt + 1 < attempts:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"BaoStock服务暂时不可用，未修改股票状态：{last_error}")


def _market_worker_fetch(code: str, start_date: str, end_date: str) -> tuple[str, list[dict]]:
    global _WORKER_TDX
    if _WORKER_TDX is None:
        _WORKER_TDX = TdxDailyClient()
        _WORKER_TDX.connect()
    errors = []
    try:
        return "通达信", _run_with_timeout(20, _WORKER_TDX.fetch, code, start_date, end_date)
    except Exception as error:
        errors.append(f"通达信: {error}")
    try:
        return "腾讯", _run_with_timeout(20, fetch_tencent_daily, code, start_date, end_date)
    except Exception as error:
        errors.append(f"腾讯: {error}")
    raise RuntimeError("；".join(errors))


def _tdx_worker_initialize() -> None:
    global _WORKER_TDX
    _WORKER_TDX = TdxDailyClient()
    _WORKER_TDX.connect()


def _financial_worker_fetch(code: str, quarters_back: int) -> list[dict]:
    global _WORKER_BAOSTOCK
    if _WORKER_BAOSTOCK is None:
        _baostock_worker_initialize()
    return _run_with_timeout(
        90, BaoStockSource().fetch_financial_indicators,
        code, quarters_back, _WORKER_BAOSTOCK,
    )


def prepare_queue(path) -> int:
    initialize(path)
    execute(
        path,
        """INSERT INTO stock_sync_status(ts_code)
        SELECT ts_code FROM stocks WHERE list_status='L'
        ON CONFLICT(ts_code) DO NOTHING""",
    )
    return query(path, "SELECT COUNT(*) n FROM stock_sync_status")[0]["n"]


def queue_incremental_update(path) -> int:
    return execute(
        path,
        """UPDATE stock_sync_status SET price_status='PENDING'
        WHERE price_status='DONE' AND (
          eligible=1 OR (
            filter_reason='20日平均成交额低于1亿元'
            AND datetime(updated_at) <= datetime('now','localtime','-7 days')
          )
        )""",
    )


def queue_history_backfill(path, target: int = PRICE_HISTORY_TARGET) -> int:
    """Queue eligible stocks whose local history has fewer than target sessions."""
    return execute(
        path,
        """UPDATE stock_sync_status SET price_status='PENDING',price_error=NULL
        WHERE eligible=1 AND (
          SELECT COUNT(*) FROM daily_prices p WHERE p.ts_code=stock_sync_status.ts_code
        ) < ?""",
        (target,),
    )


def _static_filter(row) -> str | None:
    name = (row["name"] or "").upper()
    if "ST" in name or "退" in name:
        return "ST或退市风险"
    # Recognize legacy 920xxx.SH rows created before the BJ mapping fix.
    if row["market"] == "BJ" or str(row["ts_code"]).startswith("920"):
        return "北交所"
    list_date = row["list_date"]
    if list_date and list_date != "19900101":
        if (pd.Timestamp.today().normalize() - pd.Timestamp(list_date)).days < 365:
            return "上市不足一年"
    return None


def _update(path, code: str, **values) -> None:
    values["updated_at"] = pd.Timestamp.now().isoformat()
    assignments = ",".join(f"{key}=?" for key in values)
    execute(path, f"UPDATE stock_sync_status SET {assignments} WHERE ts_code=?", (*values.values(), code))


def _filter_from_local(path, code: str, name: str, list_date: str) -> tuple[bool, str]:
    upper_name = (name or "").upper()
    if "ST" in upper_name or "退" in upper_name:
        return False, "ST或退市风险"
    if list_date and list_date != "19900101":
        if (pd.Timestamp.today().normalize() - pd.Timestamp(list_date)).days < 365:
            return False, "上市不足一年"
    amounts = query(
        path,
        "SELECT amount FROM daily_prices WHERE ts_code=? ORDER BY trade_date DESC LIMIT 20",
        (code,),
    )
    if len(amounts) < 20:
        return False, "行情不足20日"
    average_yuan = sum(float(row["amount"] or 0) for row in amounts) / len(amounts) * 1000
    if average_yuan < 100_000_000:
        return False, "20日平均成交额低于1亿元"
    return True, "通过"


def _market_history_start(saved: str | None, local_count: int, end: date) -> pd.Timestamp:
    """Backfill enough calendar time to display 250 trading sessions."""
    if not saved or local_count < PRICE_HISTORY_TARGET:
        return pd.Timestamp(end - timedelta(days=PRICE_HISTORY_CALENDAR_DAYS))
    return pd.Timestamp(saved) - pd.Timedelta(days=7)


def sync_market_batch(path, limit: int | None = None, callback: ProgressCallback | None = None) -> BatchSummary:
    prepare_queue(path)
    sql = """SELECT q.ts_code,s.name,s.list_date,s.market FROM stock_sync_status q
             JOIN stocks s ON s.ts_code=q.ts_code
             WHERE q.price_status IN ('PENDING','FAILED') ORDER BY q.ts_code"""
    rows = query(path, sql)
    if limit:
        rows = rows[:limit]
    total = len(rows)
    succeeded = failed = eligible_count = 0
    end = date.today()
    fetch_items = []
    processed = 0
    for row in rows:
        code = row["ts_code"]
        static_reason = _static_filter(row)
        if static_reason:
            _update(path, code, price_status="DONE", eligible=0, filter_reason=static_reason, price_error=None)
            succeeded += 1
            processed += 1
            if callback:
                callback(processed, total, code)
            continue
        saved = query(path, "SELECT last_price_date FROM stock_sync_status WHERE ts_code=?", (code,))[0]["last_price_date"]
        local_count = query(path, "SELECT COUNT(*) n FROM daily_prices WHERE ts_code=?", (code,))[0]["n"]
        code_start = _market_history_start(saved, int(local_count), end)
        fetch_items.append((row, code_start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))

    worker_count = max(1, min(int(os.getenv("MARKET_SYNC_WORKERS", "4")), 8))
    pending = deque(fetch_items)
    active_workers = worker_count
    pool_restarts = 0
    while pending:
        executor = ProcessPoolExecutor(max_workers=active_workers, initializer=_tdx_worker_initialize)
        futures = {}
        pool_broken = False

        def submit_one() -> bool:
            if not pending:
                return False
            item = pending.popleft()
            try:
                future = executor.submit(_market_worker_fetch, item[0]["ts_code"], item[1], item[2])
            except BrokenProcessPool:
                pending.appendleft(item)
                return False
            futures[future] = item
            return True

        for _ in range(active_workers * 2):
            if not submit_one():
                break
        try:
            while futures and not pool_broken:
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = futures.pop(future)
                    row = item[0]
                    code = row["ts_code"]
                    try:
                        price_source, records = future.result()
                    except BrokenProcessPool:
                        retry_items = [item, *futures.values()]
                        futures.clear()
                        pending.extendleft(reversed(retry_items))
                        pool_broken = True
                        break
                    except Exception as error:
                        processed += 1
                        if callback:
                            callback(processed, total, code)
                        _update(path, code, price_status="FAILED", price_error=str(error)[:500])
                        failed += 1
                    else:
                        processed += 1
                        if callback:
                            callback(processed, total, code)
                        upsert_records(
                            path, "daily_prices", records,
                            ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"],
                        )
                        allowed, reason = _filter_from_local(path, code, row["name"], row["list_date"])
                        latest = query(path, "SELECT MAX(trade_date) d FROM daily_prices WHERE ts_code=?", (code,))[0]["d"]
                        _update(path, code, price_status="DONE", eligible=int(allowed), filter_reason=reason,
                                last_price_date=latest, price_source=price_source, price_error=None)
                        succeeded += 1
                        eligible_count += int(allowed)
                    if not pool_broken:
                        submit_one()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if pool_broken:
            pool_restarts += 1
            if pool_restarts >= 3:
                raise RuntimeError("行情子进程池连续异常退出，任务已暂停且未处理股票保留待重试")
            if active_workers > 1:
                active_workers = 1
    return BatchSummary(total, succeeded, failed, eligible_count)


def sync_financial_batch(path, limit: int | None = None, years_back: int = 2,
                         callback: ProgressCallback | None = None) -> BatchSummary:
    rows = query(
        path,
        """SELECT q.ts_code FROM stock_sync_status q
        WHERE q.eligible=1 AND q.financial_status IN ('PENDING','FAILED') ORDER BY q.ts_code""",
    )
    if limit:
        rows = rows[:limit]
    total = len(rows)
    succeeded = failed = 0
    if rows:
        _baostock_healthcheck()
    # Ranking uses the latest disclosed record, so one broadly reported
    # quarter is sufficient for the full-market batch. Manual single-stock
    # sync still supports multiple years of history.
    quarters_back = 1
    worker_count = max(1, min(int(os.getenv("FINANCIAL_SYNC_WORKERS", "4")), 6))
    pending = deque(rows)
    executor = ProcessPoolExecutor(max_workers=worker_count, initializer=_baostock_worker_initialize)
    futures = {}

    def submit_one() -> bool:
        if not pending:
            return False
        row = pending.popleft()
        futures[executor.submit(_financial_worker_fetch, row["ts_code"], quarters_back)] = row
        return True

    for _ in range(worker_count * 2):
        if not submit_one():
            break
    processed = 0
    try:
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                row = futures.pop(future)
                code = row["ts_code"]
                try:
                    records = future.result()
                    upsert_records(
                        path, "financial_indicators", records,
                        ["ts_code", "ann_date", "end_date", "roe", "debt_to_assets", "revenue_yoy", "netprofit_yoy", "ocf_to_or", "grossprofit_margin"],
                    )
                    _update(path, code, financial_status="DONE", financial_error=None)
                    succeeded += 1
                except BrokenProcessPool as error:
                    raise RuntimeError("财务同步子进程异常退出，未完成股票保留待重试") from error
                except Exception as error:
                    _update(path, code, financial_status="FAILED", financial_error=str(error)[:500])
                    failed += 1
                processed += 1
                if callback:
                    callback(processed, total, code)
                submit_one()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    build_scores(path)
    return BatchSummary(total, succeeded, failed, total)


def sync_statistics(path) -> dict:
    initialize(path)
    row = query(
        path,
        """SELECT COUNT(*) total,
        SUM(price_status='DONE') price_done,
        SUM(price_status='FAILED') price_failed,
        SUM(eligible=1) eligible,
        SUM(eligible=0) filtered,
        SUM(financial_status='DONE') financial_done
        FROM stock_sync_status""",
    )[0]
    return {key: int(row[key] or 0) for key in row.keys()}
