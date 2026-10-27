import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from loguru import logger

from app import db
from app.auth import get_current_user, require_user
from app.config import settings
from app.models import DataGenAnalyzeRequest, DataGenDependencyPlanRequest, DataGenJobCreate, DataGenMetadataRequest, DataGenRuleValidationRequest, DataGenTableListRequest
from app.services import connection_manager
from app.services.sql_executor import split_sql_statements
from app.services.sql_grouping import describe_statements
from app.services.script_runner import tail_file
from app.services import datagen_jobs
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
        "concurrency": payload.get("concurrency"),
        "rows_affected": job.get("rows_affected"),
        "error_msg": job.get("error_msg"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "ended_at": job.get("ended_at"),
    }


def _datagen_page_context(request: Request, initial_mode: str = "") -> dict:
    default_dsn = {
        "host": settings.target_db_host,
        "port": settings.target_db_port,
        "user": settings.target_db_user,
        "password": "",
        "database": settings.target_db_name,
    }
    user_id = connection_manager.current_user_id(get_current_user(request))
    return {
        "default_dsn": default_dsn,
        "connections": [connection_manager.to_public_connection(c) for c in db.list_connections(user_id)],
        "jobs": db.list_datagen_jobs_for_user(user_id),
        "script_enabled": settings.datagen_script_execution_enabled,
        "env_vars": _ENV_VARS,
        "max_sql_chars": settings.datagen_max_sql_chars,
        "max_script_chars": settings.datagen_max_script_chars,
        "initial_mode": initial_mode,
        "standalone": initial_mode == "multi",
    }


@router.get("/datagen")
def datagen_page(request: Request):
    return templates.TemplateResponse(request, "datagen.html", _datagen_page_context(request))


@router.get("/datagen/multi")
def datagen_multi_page(request: Request):
    """「多表导入」子栏目：复用造数模板，但不渲染标签栏，直接进入多表导入面板。"""
    return templates.TemplateResponse(
        request, "datagen.html", _datagen_page_context(request, initial_mode="multi")
    )


@router.get("/api/datagen/jobs")
def list_datagen_jobs(user: Optional[dict] = Depends(require_user)):
    return datagen_jobs.list_jobs(connection_manager.current_user_id(user))


@router.post("/api/datagen/jobs", status_code=201)
def create_datagen_job(body: DataGenJobCreate, user: Optional[dict] = Depends(require_user)):
    try:
        return datagen_jobs.create_job(connection_manager.current_user_id(user), body)
    except HTTPException:
        # 连接不存在等业务异常自带状态码，不能落到下面的 500 兜底
        raise
    except (ValueError, ConnectionError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("create datagen job failed")
        raise HTTPException(status_code=500, detail=f"启动造数任务失败：{e}")


@router.post("/api/datagen/analyze")
def analyze_datagen_content(body: DataGenAnalyzeRequest):
    """多表导入前的分组预览：按目标表拆分语句，供用户确认后再并发执行。"""
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="分析内容为空")
    if len(content) > settings.datagen_max_sql_chars:
        raise HTTPException(status_code=400, detail=f"造数内容超过 {settings.datagen_max_sql_chars} 字符限制")
    statements = split_sql_statements(content)
    if not statements:
        raise HTTPException(status_code=400, detail="没有可执行的 SQL 语句")
    return describe_statements(statements)


@router.post("/api/datagen/dependency-plan")
def get_datagen_dependency_plan(body: DataGenDependencyPlanRequest, user: Optional[dict] = Depends(require_user)):
    try:
        return datagen_jobs.dependency_plan_from_body(connection_manager.current_user_id(user), body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("build datagen dependency plan failed")
        raise HTTPException(status_code=400, detail=f"分析依赖表失败：{exc}")


@router.post("/api/datagen/metadata")
def get_datagen_metadata(body: DataGenMetadataRequest, user: Optional[dict] = Depends(require_user)):
    try:
        return datagen_jobs.metadata_from_body(connection_manager.current_user_id(user), body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("read datagen metadata failed")
        raise HTTPException(status_code=400, detail=f"读取表结构失败：{exc}")


@router.post("/api/datagen/tables")
def list_datagen_tables(body: DataGenTableListRequest, user: Optional[dict] = Depends(require_user)):
    try:
        return datagen_jobs.list_tables_from_body(connection_manager.current_user_id(user), body)
    except Exception as exc:
        logger.exception("list datagen tables failed")
        raise HTTPException(status_code=400, detail=f"读取数据表失败：{exc}")


@router.post("/api/datagen/rules/validate")
def validate_datagen_rules(body: DataGenRuleValidationRequest, user: Optional[dict] = Depends(require_user)):
    try:
        return datagen_jobs.validate_rules_from_body(connection_manager.current_user_id(user), body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("validate datagen rules failed")
        raise HTTPException(status_code=400, detail=f"校验字段规则失败：{exc}")


@router.post("/api/datagen/rules/apply")
def apply_datagen_rules(body: DataGenRuleValidationRequest, user: Optional[dict] = Depends(require_user)):
    try:
        return datagen_jobs.apply_rules_from_body(connection_manager.current_user_id(user), body)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise HTTPException(status_code=400, detail=f"应用数据库规则失败：{exc}")
    except Exception as exc:
        logger.exception("apply datagen rules failed")
        raise HTTPException(status_code=400, detail=f"应用数据库规则失败：{exc}")


@router.get("/api/datagen/jobs/{job_id}")
def get_datagen_job(job_id: str, user: Optional[dict] = Depends(require_user)):
    try:
        return datagen_jobs.get_job(connection_manager.current_user_id(user), job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"造数任务 {job_id} 不存在")


@router.post("/api/datagen/jobs/{job_id}/stop")
def stop_datagen_job(job_id: str, user: Optional[dict] = Depends(require_user)):
    try:
        return datagen_jobs.stop_job(connection_manager.current_user_id(user), job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"造数任务 {job_id} 不存在")
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/api/datagen/jobs/{job_id}/log")
def get_datagen_log(job_id: str, user: Optional[dict] = Depends(require_user)):
    try:
        log = datagen_jobs.get_job_log(connection_manager.current_user_id(user), job_id, tail_chars=200_000)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"造数任务 {job_id} 不存在")
    return PlainTextResponse(log["log"], media_type="text/plain; charset=utf-8")
