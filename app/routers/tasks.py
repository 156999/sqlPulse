import json
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app import db
from app.auth import require_user
from app.models import DbDsn, TaskCreate, SqlInput, FormTools, PreviewResult
from app.services import connection_manager
from app.services.runner import now_iso, runner, parse_sql_tasks
from app.services.run_form import prepare, preview, form_tools, FormError

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


@router.post("/api/runs/form-tools")
def run_form_tools(body: FormTools, user: Optional[dict] = Depends(require_user)):
    try:
        return form_tools(body)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail=getattr(exc, "detail", {"message": str(exc)}))


@router.post("/api/runs", status_code=201)
def create_run(body: TaskCreate, user: Optional[dict] = Depends(require_user)):
    if (body.connection_id is None) == (body.db_dsn is None):
        raise HTTPException(status_code=400, detail="connection_id 与 db_dsn 必须且只能提供一个")
    try:
        parsed, groups, sql_content, _, _ = prepare(body)
    except ValueError as exc:
        detail = str(exc) if body.groups is None else getattr(exc, 'detail', {'message': str(exc)})
        raise HTTPException(status_code=400, detail=detail)
    user_id = connection_manager.current_user_id(user)
    if body.connection_id is not None:
        dsn = connection_manager.resolve_connection(user_id, body.connection_id)
    else:
        dsn = body.db_dsn
    result = connection_manager.test_dsn(dsn)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=f"数据库连接失败：{result['error']}")

    run_id = uuid4().hex[:8]
    row = {
        "id": run_id,
        "name": body.name,
        "status": "pending",
        "sql_source": body.sql_source,
        "sql_content": sql_content,
        "groups_json": json.dumps(groups, ensure_ascii=False),
        "variables_json": json.dumps(body.variables, ensure_ascii=False),
        "concurrency": body.concurrency,
        "spawn_rate": body.spawn_rate,
        "duration_sec": body.duration_sec,
        "db_dsn_json": dsn.model_dump_json(),
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
        "groups": json.loads(run.get("groups_json") or "[]"),
        "variables": json.loads(run.get("variables_json") or "{}"),
        "run_id": run["id"], "name": run["name"], "status": run["status"],
        "sql_source": run["sql_source"], "concurrency": run["concurrency"],
        "spawn_rate": run["spawn_rate"], "duration_sec": run["duration_sec"],
        "error_msg": run["error_msg"], "created_at": run["created_at"],
        "started_at": run["started_at"], "ended_at": run["ended_at"],
    }
