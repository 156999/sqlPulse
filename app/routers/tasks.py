import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app import db
from app.auth import require_user
from app.models import DbDsn, TaskCreate, SqlInput, FormTools, PreviewResult, SmartCompleteRequest
from app.services import connection_manager
from app.services.runner import parse_sql_tasks
from app.services.run_jobs import create_sql_run as create_run_service, get_sql_run, stop_sql_run
from app.services.run_form import preview, form_tools, FormError
from app.services.datagen_rules import read_table_metadata
from app.services.smart_completion import suggest

router = APIRouter()


@router.post("/api/db/test")
def test_db(dsn: DbDsn):
    return connection_manager.test_dsn(dsn)


@router.post("/api/runs/preview", response_model=PreviewResult)
def preview_run(body: SqlInput, user: Optional[dict] = Depends(require_user)):
    try:
        return preview(body)
    except (ValueError, OverflowError) as exc:
        return {"ok": False, "errors": [getattr(exc, "detail", {"message": str(exc)})]}


@router.post("/api/runs/smart-complete")
def smart_complete(body: SmartCompleteRequest, user: Optional[dict] = Depends(require_user)):
    try:
        user_id = connection_manager.current_user_id(user)
        dsn = connection_manager.resolve_connection(user_id, body.connection_id) if body.connection_id else body.db_dsn
        table_refs = list(re.finditer(
            r"\b(?:from|join)\s+`?(?P<table>[A-Za-z_][A-Za-z0-9_]*)`?"
            r"(?:\s+(?:as\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_]*))?",
            body.sql, re.I,
        ))
        if not table_refs:
            return {"suggestions": [], "message": "未识别到 FROM/JOIN 目标表"}
        tables = {}
        for ref in table_refs:
            table = ref.group("table")
            alias = (ref.group("alias") or table).lower()
            tables[alias] = read_table_metadata(dsn.model_dump(), table)
        metadata = {"tables": tables}
        return suggest(body.sql, metadata)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"读取表结构失败：{exc}") from exc


@router.post("/api/runs/form-tools")
def run_form_tools(body: FormTools, user: Optional[dict] = Depends(require_user)):
    try:
        return form_tools(body)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail=getattr(exc, "detail", {"message": str(exc)}))


@router.post("/api/runs", status_code=201)
def create_run(body: TaskCreate, user: Optional[dict] = Depends(require_user)):
    user_id = connection_manager.current_user_id(user)
    return create_run_service(user_id, body)


@router.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str, user: Optional[dict] = Depends(require_user)):
    return stop_sql_run(connection_manager.current_user_id(user), run_id)


@router.get("/api/runs/{run_id}")
def get_run(run_id: str, user: Optional[dict] = Depends(require_user)):
    return get_sql_run(connection_manager.current_user_id(user), run_id)
