import json
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from loguru import logger

from app import db
from app.auth import get_current_user, require_user
from app.config import settings
from app.models import DataGenJobCreate, DataGenMetadataRequest, DataGenRuleValidationRequest, DataGenTableListRequest
from app.services import connection_manager
from app.services.datagen import datagen_executor
from app.services.datagen_rules import apply_database_rules, list_table_names, read_table_metadata, validate_field_rules
from app.services.runner import now_iso
from app.services.script_runner import tail_file
from app.templating import templates

router = APIRouter()

_ENV_VARS = [
    "TARGET_DB_HOST",
    "TARGET_DB_PORT",
    "TARGET_DB_USER",
    "TARGET_DB_PASSWORD",
    "TARGET_DB_NAME",
]


def _resolve_body_dsn(body, user: Optional[dict]):
    if (body.connection_id is None) == (body.db_dsn is None):
        raise HTTPException(status_code=400, detail="connection_id 与 db_dsn 必须且只能提供一个")
    user_id = connection_manager.current_user_id(user)
    if body.connection_id is not None:
        return connection_manager.resolve_connection(user_id, body.connection_id)
    return body.db_dsn


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
        "password": "",
        "database": settings.target_db_name,
    }
    user_id = connection_manager.current_user_id(get_current_user(request))
    return templates.TemplateResponse(
        request,
        "datagen.html",
        {
            "default_dsn": default_dsn,
            "connections": [connection_manager.to_public_connection(c) for c in db.list_connections(user_id)],
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
def create_datagen_job(body: DataGenJobCreate, user: Optional[dict] = Depends(require_user)):
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="造数内容为空")
    if body.mode == "shell" and not settings.datagen_script_execution_enabled:
        raise HTTPException(status_code=400, detail="Shell 造数未开启（DATAGEN_SCRIPT_EXECUTION_ENABLED=false）")
    max_chars = settings.datagen_max_sql_chars if body.mode == "sql" else settings.datagen_max_script_chars
    if len(content) > max_chars:
        raise HTTPException(status_code=400, detail=f"造数内容超过 {max_chars} 字符限制")
    if body.mode == "sql":
        try:
            from app.services.sql_params import compile_statement, compile_variables
            definitions = compile_variables(body.variables)
            statements = __import__("app.services.sql_executor", fromlist=["split_sql_statements"]).split_sql_statements(content)
            if not statements:
                raise ValueError("没有可执行的 SQL 语句")
            for i, statement in enumerate(statements, 1):
                compile_statement(statement, definitions, f"造数 SQL 第 {i} 条")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    dsn = _resolve_body_dsn(body, user)
    result = connection_manager.test_dsn(dsn)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=f"数据库连接失败：{result['error']}")
    applied_rules = body.field_rules
    unique_indexes = []
    if body.target_table:
        try:
            dsn_data = dsn.model_dump() if hasattr(dsn, "model_dump") else dsn
            metadata = read_table_metadata(dsn_data, body.target_table)
            applied = apply_database_rules(metadata, body.field_rules, body.row_count)
            if not applied["ok"]:
                messages = "；".join(f"{item.get('column') or '-'}：{item['message']}" for item in applied["errors"])
                raise HTTPException(status_code=400, detail=f"数据库规则复验失败：{messages}")
            applied_rules = applied["rules"]
            unique_indexes = metadata.get("unique_indexes") or []
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"数据库规则复验失败：{exc}")

    job_id = uuid4().hex[:8]
    row = {
        "id": job_id,
        "name": body.name,
        "mode": body.mode,
        "status": "pending",
        "target_dsn_json": dsn.model_dump_json(),
        "input_json": json.dumps({"source": body.source, "content": content,
                                   "variables": body.variables, "row_count": body.row_count,
                                   "target_table": body.target_table, "field_rules": applied_rules,
                                   "unique_indexes": unique_indexes}, ensure_ascii=False),
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


@router.post("/api/datagen/metadata")
def get_datagen_metadata(body: DataGenMetadataRequest, user: Optional[dict] = Depends(require_user)):
    dsn = _resolve_body_dsn(body, user)
    try:
        return read_table_metadata(dsn.model_dump() if hasattr(dsn, "model_dump") else dsn, body.table)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("read datagen metadata failed")
        raise HTTPException(status_code=400, detail=f"读取表结构失败：{exc}")


@router.post("/api/datagen/tables")
def list_datagen_tables(body: DataGenTableListRequest, user: Optional[dict] = Depends(require_user)):
    dsn = _resolve_body_dsn(body, user)
    try:
        dsn_data = dsn.model_dump() if hasattr(dsn, "model_dump") else dsn
        return {"tables": list_table_names(dsn_data, body.query)}
    except Exception as exc:
        logger.exception("list datagen tables failed")
        raise HTTPException(status_code=400, detail=f"读取数据表失败：{exc}")


@router.post("/api/datagen/rules/validate")
def validate_datagen_rules(body: DataGenRuleValidationRequest, user: Optional[dict] = Depends(require_user)):
    dsn = _resolve_body_dsn(body, user)
    try:
        metadata = read_table_metadata(dsn.model_dump() if hasattr(dsn, "model_dump") else dsn, body.table)
        return {"ok": True, "errors": validate_field_rules(metadata, body.rules, body.row_count), "metadata": metadata}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("validate datagen rules failed")
        raise HTTPException(status_code=400, detail=f"校验字段规则失败：{exc}")


@router.post("/api/datagen/rules/apply")
def apply_datagen_rules(body: DataGenRuleValidationRequest, user: Optional[dict] = Depends(require_user)):
    dsn = _resolve_body_dsn(body, user)
    try:
        dsn_data = dsn.model_dump() if hasattr(dsn, "model_dump") else dsn
        metadata = read_table_metadata(dsn_data, body.table)
        result = apply_database_rules(metadata, body.rules, body.row_count)
        return {**result, "metadata": metadata}
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise HTTPException(status_code=400, detail=f"应用数据库规则失败：{exc}")
    except Exception as exc:
        logger.exception("apply datagen rules failed")
        raise HTTPException(status_code=400, detail=f"应用数据库规则失败：{exc}")


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
