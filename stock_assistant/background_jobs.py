from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys

from .database import execute, initialize, query


ACTIVE_STATUSES = ("STARTING", "RUNNING", "STOP_REQUESTED")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def active_job(path):
    initialize(path)
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    rows = query(path, f"SELECT * FROM batch_jobs WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT 1", ACTIVE_STATUSES)
    if not rows:
        return None
    job = dict(rows[0])
    pid = job.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            execute(
                path,
                "UPDATE batch_jobs SET status='FAILED',message='后台进程意外退出',updated_at=? WHERE id=?",
                (_now(), job["id"]),
            )
            return None
        except PermissionError:
            pass
    return job


def latest_job(path):
    initialize(path)
    rows = query(path, "SELECT * FROM batch_jobs ORDER BY id DESC LIMIT 1")
    return dict(rows[0]) if rows else None


def start_job(path: Path, stage: str, batch_limit: int) -> int:
    if active_job(path):
        raise RuntimeError("已有批量任务正在运行")
    stage = stage.upper()
    if stage not in {"MARKET", "FINANCIAL"}:
        raise ValueError("未知任务类型")
    now = _now()
    job_id = execute(
        path,
        """INSERT INTO batch_jobs(stage,status,batch_limit,heartbeat_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?)""",
        (stage, "STARTING", batch_limit, now, now, now),
    )
    log_path = path.parent / f"batch_job_{job_id}.log"
    log_handle = log_path.open("ab")
    process = subprocess.Popen(
        [sys.executable, "-m", "stock_assistant.batch_worker", str(job_id)],
        cwd=Path(__file__).resolve().parent.parent,
        stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True,
    )
    log_handle.close()
    execute(path, "UPDATE batch_jobs SET pid=?,updated_at=? WHERE id=?", (process.pid, _now(), job_id))
    return job_id


def touch_heartbeat(path) -> None:
    job = active_job(path)
    if job and job["status"] in {"STARTING", "RUNNING"}:
        execute(path, "UPDATE batch_jobs SET heartbeat_at=?,updated_at=? WHERE id=?", (_now(), _now(), job["id"]))


def request_stop(path) -> bool:
    job = active_job(path)
    if not job:
        return False
    execute(path, "UPDATE batch_jobs SET status='STOP_REQUESTED',updated_at=? WHERE id=?", (_now(), job["id"]))
    return True
