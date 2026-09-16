import json
from uuid import uuid4

import pymysql
from fastapi import APIRouter, HTTPException
from loguru import logger

from app import db
from app.config import settings
from app.models import DbDsn, TaskCreate
from app.services.runner import now_iso, runner

router = APIRouter()


@router.post("/api/db/test")
def test_db(dsn: DbDsn):
    try:
        conn = pymysql.connect(
            host=dsn.host, port=dsn.port, user=dsn.user,
            password=dsn.password, database=dsn.database, connect_timeout=3,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/runs", status_code=201)
def create_run(body: TaskCreate):
    try:
        conn = pymysql.connect(
            host=body.db_dsn.host, port=body.db_dsn.port, user=body.db_dsn.user,
            password=body.db_dsn.password, database=body.db_dsn.database, connect_timeout=3,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"数据库连接失败：{e}")

    run_id = uuid4().hex[:8]
    row = {
        "id": run_id,
        "name": body.name,
        "status": "pending",
        "sql_source": body.sql_source,
        "sql_content": body.sql_content,
        "concurrency": body.concurrency,
        "spawn_rate": body.spawn_rate,
        "duration_sec": body.duration_sec,
        "db_dsn_json": body.db_dsn.model_dump_json(),
        "created_at": now_iso(),
    }
    db.create_run(row)
    try:
        runner.start(run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("start run {} failed", run_id)
        raise HTTPException(status_code=500, detail=f"启动压测失败：{e}")
    return {"run_id": run_id}


@router.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str):
    try:
        status = runner.stop(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"任务 {run_id} 不存在")
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": status}


@router.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"任务 {run_id} 不存在")
    return {
        "run_id": run["id"], "name": run["name"], "status": run["status"],
        "sql_source": run["sql_source"], "concurrency": run["concurrency"],
        "spawn_rate": run["spawn_rate"], "duration_sec": run["duration_sec"],
        "error_msg": run["error_msg"], "created_at": run["created_at"],
        "started_at": run["started_at"], "ended_at": run["ended_at"],
    }
