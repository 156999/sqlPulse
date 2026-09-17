import json
from pathlib import Path
from uuid import uuid4

import pymysql
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from app import db
from app.config import settings
from app.models import DataGenJobCreate
from app.services.datagen import datagen_executor
from app.services.runner import now_iso
from app.services.script_runner import tail_file

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

_ENV_VARS = [
    "TARGET_DB_HOST",
    "TARGET_DB_PORT",
    "TARGET_DB_USER",
    "TARGET_DB_PASSWORD",
    "TARGET_DB_NAME",
]


def _safe_job(job: dict) -> dict:
    try:
        payload = json.loads(job.get("input_json") or "{}")
    except ValueError:
        payload = {}
    return {
        "job_id": job["id"],
        "name": job["name"],
        "mode": job["mode"],
        "status": job["status"],
        "source": payload.get("source", "paste"),
        "rows_affected": job.get("rows_affected"),
        "error_msg": job.get("error_msg"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "ended_at": job.get("ended_at"),
    }


@router.get("/datagen")
def datagen_page(request: Request):
    default_dsn = {
        "host": settings.target_db_host,
        "port": settings.target_db_port,
        "user": settings.target_db_user,
        "password": settings.target_db_password,
        "database": settings.target_db_name,
    }
    return templates.TemplateResponse(
        request,
        "datagen.html",
        {
            "default_dsn": default_dsn,
            "jobs": db.list_datagen_jobs(),
            "script_enabled": settings.datagen_script_execution_enabled,
            "env_vars": _ENV_VARS,
            "max_sql_chars": settings.datagen_max_sql_chars,
            "max_script_chars": settings.datagen_max_script_chars,
        },
    )


@router.get("/api/datagen/jobs")
def list_datagen_jobs():
    return [_safe_job(job) for job in db.list_datagen_jobs()]


@router.post("/api/datagen/jobs", status_code=201)
def create_datagen_job(body: DataGenJobCreate):
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="造数内容为空")
    if body.mode == "shell" and not settings.datagen_script_execution_enabled:
        raise HTTPException(status_code=400, detail="Shell 造数未开启（DATAGEN_SCRIPT_EXECUTION_ENABLED=false）")
    max_chars = settings.datagen_max_sql_chars if body.mode == "sql" else settings.datagen_max_script_chars
    if len(content) > max_chars:
        raise HTTPException(status_code=400, detail=f"造数内容超过 {max_chars} 字符限制")

    try:
        conn = pymysql.connect(
            host=body.db_dsn.host,
            port=body.db_dsn.port,
            user=body.db_dsn.user,
            password=body.db_dsn.password,
            database=body.db_dsn.database,
            connect_timeout=3,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"数据库连接失败：{e}")

    job_id = uuid4().hex[:8]
    row = {
        "id": job_id,
        "name": body.name,
        "mode": body.mode,
        "status": "pending",
        "target_dsn_json": body.db_dsn.model_dump_json(),
        "input_json": json.dumps({"source": body.source, "content": content}, ensure_ascii=False),
        "created_at": now_iso(),
    }
    db.create_datagen_job(row)
    try:
        datagen_executor.start(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("start datagen job {} failed", job_id)
        raise HTTPException(status_code=500, detail=f"启动造数任务失败：{e}")
    return {"job_id": job_id}


@router.get("/api/datagen/jobs/{job_id}")
def get_datagen_job(job_id: str):
    job = db.get_datagen_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"造数任务 {job_id} 不存在")
    return _safe_job(job)


@router.post("/api/datagen/jobs/{job_id}/stop")
def stop_datagen_job(job_id: str):
    try:
        status = datagen_executor.stop(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"造数任务 {job_id} 不存在")
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": status}


@router.get("/api/datagen/jobs/{job_id}/log")
def get_datagen_log(job_id: str):
    job = db.get_datagen_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"造数任务 {job_id} 不存在")
    log_path = job.get("log_path")
    if not log_path or not Path(log_path).exists():
        return PlainTextResponse("", media_type="text/plain; charset=utf-8")
    return PlainTextResponse(tail_file(Path(log_path), size=200_000), media_type="text/plain; charset=utf-8")
