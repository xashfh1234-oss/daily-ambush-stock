from stock_assistant.background_jobs import active_job, latest_job, request_stop
from stock_assistant.database import execute, initialize


def test_stop_request(tmp_path):
    path = tmp_path / "jobs.db"
    initialize(path)
    execute(path, """INSERT INTO batch_jobs(stage,status,batch_limit,heartbeat_at,created_at,updated_at)
                    VALUES('MARKET','RUNNING',10,'2026-01-01','2026-01-01','2026-01-01')""")
    assert active_job(path)["stage"] == "MARKET"
    assert request_stop(path)
    assert latest_job(path)["status"] == "STOP_REQUESTED"
