"""Shared use cases for creating and accessing load-test runs."""
import json
import logging
from uuid import uuid4

from app import db
from app.services import connection_manager
from app.services.credential_crypto import encrypt_dsn_snapshot
from app.services.observability import build_snapshot
from app.services.runner import now_iso, runner

logger = logging.getLogger(__name__)


def require_owned_run(user_id: str, run_id: str) -> dict:
    run = db.get_run(run_id)
    if not run or run.get("owner_user_id") != user_id:
        raise KeyError(run_id)
    return run


def create_run(user_id: str, body) -> dict:
    from app.services.run_form import prepare

    try:
        parsed, groups, sql_content, _, _ = prepare(body)
    except ValueError as exc:
        error = ValueError(str(exc))
        error.detail = getattr(exc, "detail", str(exc))
        raise error from exc
    if (body.connection_id is None) == (body.db_dsn is None):
        raise ValueError("connection_id 与 db_dsn 必须且只能提供一个")
    dsn = (connection_manager.resolve_connection(user_id, body.connection_id)
           if body.connection_id else body.db_dsn)
    result = connection_manager.test_dsn(dsn)
    if not result["ok"]:
        raise ValueError(f"数据库连接失败：{result['error']}")
    run_id = uuid4().hex[:8]
    db.create_run({
        "id": run_id, "owner_user_id": user_id, "name": body.name, "status": "pending",
        "sql_source": body.sql_source, "sql_content": sql_content,
        "groups_json": json.dumps(groups, ensure_ascii=False),
        "variables_json": json.dumps(body.variables, ensure_ascii=False),
        "concurrency": body.concurrency, "spawn_rate": body.spawn_rate,
        "duration_sec": body.duration_sec,
        "db_dsn_json": json.dumps(encrypt_dsn_snapshot(dsn.model_dump()), ensure_ascii=False),
        "created_at": now_iso(),
    })
    try:
        runner.start(run_id)
    except Exception:
        # A failed launch must not leave an apparently queued run behind.
        db.update_run(run_id, {"status": "failed", "error_msg": "压测进程启动失败", "ended_at": now_iso()})
        logger.exception("run process start failed: %s", run_id)
        raise
    return {"run_id": run_id, "status": "running"}


def run_view(run: dict) -> dict:
    obs = build_snapshot(run, db.get_metrics(run["id"]))
    return {
        "run_id": run["id"], "name": run["name"], "status": run["status"],
        "concurrency": run["concurrency"], "spawn_rate": run["spawn_rate"],
        "duration_sec": run["duration_sec"], "error_msg": run.get("error_msg"),
        "created_at": run.get("created_at"), "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"), "final_metrics": obs.get("final_metrics"),
        "observability": obs,
    }
