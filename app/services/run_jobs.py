"""User-scoped SQL load-test operations shared by HTTP and MCP adapters."""
import json
import re
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from loguru import logger

from app import db
from app.config import settings
from app.models import TaskCreate
from app.services import connection_manager
from app.services.credential_crypto import encrypt_dsn_snapshot
from app.services.observability import build_snapshot
from app.services.run_form import code_text, prepare, preview as preview_form
from app.services.runner import now_iso, runner


def list_my_connections(user_id: str) -> dict:
    rows = db.list_connections(user_id)
    return {"connections": [
        {"id": r["id"], "name": r["name"], "host": r["host"], "port": r["port"],
         "user": r["user"], "database": r["database"],
         "has_password": bool(r.get("password_ciphertext") or r.get("password"))}
        for r in rows
    ]}


def preview_sql_run(body) -> dict:
    return preview_form(body)


def _owned_run(user_id: str, run_id: str) -> dict:
    run = db.get_run_for_user(user_id, run_id)
    if not run:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "任务不存在"})
    return run


def create_sql_run(user_id: str, body: TaskCreate, *, mcp: bool = False,
                   confirm_write_sql: bool = False) -> dict:
    if (body.connection_id is None) == (body.db_dsn is None):
        raise HTTPException(status_code=400, detail="connection_id 与 db_dsn 必须且只能提供一个")
    if mcp:
        if body.connection_id is None:
            raise HTTPException(status_code=400, detail={"code": "CONNECTION_NOT_FOUND", "message": "MCP 只能使用已保存连接"})
        if body.concurrency > min(100, settings.mcp_run_max_concurrency):
            raise HTTPException(status_code=400, detail={"code": "INVALID_LOAD_PROFILE", "message": "MCP 并发超过允许上限"})
        if body.spawn_rate > min(100, settings.mcp_run_max_concurrency):
            raise HTTPException(status_code=400, detail={"code": "INVALID_LOAD_PROFILE", "message": "MCP 升压速率超过允许上限"})
        if body.duration_sec > min(1800, settings.mcp_run_max_duration_sec):
            raise HTTPException(status_code=400, detail={"code": "INVALID_LOAD_PROFILE", "message": "MCP 持续时间超过允许上限"})
        if db.count_active_runs_for_user(user_id) >= max(1, settings.mcp_run_max_active_per_user):
            raise HTTPException(status_code=409, detail={"code": "RUN_LIMIT_REACHED", "message": "当前用户已有运行中的压测任务"})
        since = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        if db.count_recent_run_creates_for_user(user_id, since) >= max(1, settings.mcp_run_max_creates_per_hour):
            raise HTTPException(status_code=429, detail={"code": "RUN_RATE_LIMITED", "message": "当前用户每小时压测任务创建次数已达上限"})
        source_size = len(body.sql_content or "") + sum(len(group.sql) for group in (body.groups or []))
        if source_size > 200_000 or len(body.groups or []) > 50 or len(body.variables) > 100:
            raise HTTPException(status_code=400, detail={"code": "INVALID_SQL", "message": "MCP SQL 总长度最多 200000 字符、任务组最多 50 个、变量最多 100 个"})
    try:
        tasks, groups, sql_content, _, _ = prepare(body)
    except ValueError as exc:
        detail = str(exc) if body.groups is None else getattr(exc, "detail", {"message": str(exc)})
        raise HTTPException(status_code=400, detail=detail) from exc
    if mcp:
        for task in tasks:
            for statement in task["statements"]:
                normalized = code_text(statement)
                if re.search(r"\b(DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE)\b", normalized):
                    raise HTTPException(status_code=400, detail={"code": "INVALID_SQL", "message": "MCP 压测不允许破坏性 DDL 或权限管理语句"})
                if re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", normalized) and not confirm_write_sql:
                    raise HTTPException(status_code=400, detail={"code": "WRITE_CONFIRMATION_REQUIRED", "message": "写入型 SQL 必须显式设置 confirm_write_sql=true"})
    if body.connection_id is not None:
        dsn = connection_manager.resolve_connection(user_id, body.connection_id)
    else:
        dsn = body.db_dsn
    result = connection_manager.test_dsn(dsn)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=f"数据库连接失败：{result['error']}")

    run_id = uuid4().hex[:8]
    created_at = now_iso()
    db.create_run({
        "id": run_id, "owner_user_id": user_id, "name": body.name, "status": "pending",
        "sql_source": body.sql_source, "sql_content": sql_content,
        "groups_json": json.dumps(groups, ensure_ascii=False),
        "variables_json": json.dumps(body.variables, ensure_ascii=False),
        "concurrency": body.concurrency, "spawn_rate": body.spawn_rate,
        "duration_sec": body.duration_sec,
        "db_dsn_json": json.dumps(encrypt_dsn_snapshot(dsn.model_dump()), ensure_ascii=False),
        "created_at": created_at,
    })
    if mcp:
        db.insert_run_audit(user_id, run_id, "created", {
            "connection_id": body.connection_id, "concurrency": body.concurrency,
            "spawn_rate": body.spawn_rate, "duration_sec": body.duration_sec,
            "sql_chars": len(sql_content), "groups": len(groups),
            "contains_write_sql": any(re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", code_text(s))
                                       for task in tasks for s in task["statements"]),
        }, created_at)
    try:
        runner.start(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("start run {} failed", run_id)
        raise HTTPException(status_code=500, detail=f"启动压测失败：{exc}") from exc
    return {"run_id": run_id}


def get_sql_run(user_id: str, run_id: str) -> dict:
    run = _owned_run(user_id, run_id)
    snapshot = build_snapshot(run, db.get_metrics(run_id))
    return {
        "groups": json.loads(run.get("groups_json") or "[]"),
        "variables": json.loads(run.get("variables_json") or "{}"),
        "run_id": run["id"], "name": run["name"], "status": run["status"],
        "sql_source": run["sql_source"], "concurrency": run["concurrency"],
        "spawn_rate": run["spawn_rate"], "duration_sec": run["duration_sec"],
        "error_msg": run["error_msg"], "created_at": run["created_at"],
        "started_at": run["started_at"], "ended_at": run["ended_at"],
        "process_exit_code": run.get("process_exit_code"),
        "finalized_at": run.get("finalized_at"),
        "final_metrics": snapshot.get("final_metrics"), "observability": snapshot,
    }


def get_sql_run_metrics(user_id: str, run_id: str, since_ts: int | None = None, limit: int = 120) -> dict:
    _owned_run(user_id, run_id)
    if limit < 1 or limit > 600:
        raise HTTPException(status_code=400, detail={"code": "INVALID_METRICS_LIMIT", "message": "limit 必须在 1..600"})
    points = db.get_metrics(run_id, since_ts)
    points = points[-limit:]
    return {"run_id": run_id, "points": points, "window_scope": "current_window"}


def stop_sql_run(user_id: str, run_id: str) -> dict:
    run = _owned_run(user_id, run_id)
    try:
        status = runner.stop(run_id)
        db.insert_run_audit(user_id, run_id, "stopped", {"previous_status": run["status"]}, now_iso())
        return {"status": status}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "任务不存在"}) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail={"code": "RUN_NOT_STOPPABLE", "message": str(exc)}) from exc


def get_sql_run_report(user_id: str, run_id: str) -> dict:
    run = _owned_run(user_id, run_id)
    if run["status"] in ("pending", "running"):
        return {"run_id": run_id, "status": "not_ready"}
    report = db.get_report(run_id)
    if not report:
        return {"run_id": run_id, "status": "not_ready"}
    return {"run_id": run_id, "status": "ready", "l0": report["l0"], "l1": report["l1"]}
