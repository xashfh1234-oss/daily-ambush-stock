from __future__ import annotations

from datetime import datetime
import sys

from .background_jobs import _now
from .batch_sync import sync_financial_batch, sync_market_batch
from .config import settings
from .database import execute, initialize, query


class JobStopped(Exception):
    pass


def run(job_id: int) -> None:
    path = settings.database_path
    initialize(path)
    rows = query(path, "SELECT * FROM batch_jobs WHERE id=?", (job_id,))
    if not rows:
        raise RuntimeError("后台任务不存在")
    job = dict(rows[0])
    execute(path, "UPDATE batch_jobs SET status='RUNNING',updated_at=? WHERE id=?", (_now(), job_id))

    def progress(current: int, total: int, code: str) -> None:
        state = query(path, "SELECT status,heartbeat_at FROM batch_jobs WHERE id=?", (job_id,))[0]
        heartbeat = datetime.fromisoformat(state["heartbeat_at"])
        heartbeat_age = (datetime.now() - heartbeat).total_seconds()
        if state["status"] == "STOP_REQUESTED":
            raise JobStopped("用户点击停止")
        if heartbeat_age > 90:
            raise JobStopped("浏览器已关闭，心跳超时")
        execute(
            path,
            "UPDATE batch_jobs SET current=?,total=?,current_code=?,updated_at=? WHERE id=?",
            (current, total, code, _now(), job_id),
        )

    try:
        if job["stage"] == "MARKET":
            result = sync_market_batch(path, job["batch_limit"], progress)
        else:
            result = sync_financial_batch(path, job["batch_limit"], 2, progress)
        message = f"成功 {result.succeeded}，失败 {result.failed}"
        status = "COMPLETED" if result.failed == 0 else "FAILED" if result.succeeded == 0 else "PARTIAL"
        execute(path, "UPDATE batch_jobs SET status=?,message=?,updated_at=? WHERE id=?", (status, message, _now(), job_id))
    except JobStopped as error:
        execute(path, "UPDATE batch_jobs SET status='STOPPED',message=?,updated_at=? WHERE id=?", (str(error), _now(), job_id))
    except Exception as error:
        execute(path, "UPDATE batch_jobs SET status='FAILED',message=?,updated_at=? WHERE id=?", (str(error)[:500], _now(), job_id))


if __name__ == "__main__":
    run(int(sys.argv[1]))
