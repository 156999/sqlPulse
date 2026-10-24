import json
import re
from pathlib import Path
from typing import Optional
from uuid import uuid4

from loguru import logger

from app import db
from app.config import settings
from app.models import DataGenJobCreate, DbDsn
from app.services import connection_manager
from app.services.datagen import datagen_executor
from app.services.datagen_dependencies import build_dependency_plan
from app.services.datagen_rules import apply_database_rules, list_table_names, read_table_metadata, validate_field_rules
from app.services.runner import now_iso
from app.services.script_runner import tail_file

_RISKY_SQL_RE = re.compile(r"\b(drop|truncate|alter|create\s+user|grant)\b", re.IGNORECASE)


def safe_job(job: dict) -> dict:
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


def resolve_body_dsn(body, user_id: str) -> DbDsn:
    if (body.connection_id is None) == (body.db_dsn is None):
        raise ValueError("connection_id 与 db_dsn 必须且只能提供一个")
    if body.connection_id is not None:
        return connection_manager.resolve_connection(user_id, body.connection_id)
    return body.db_dsn


def _dsn_dict(dsn: DbDsn | dict) -> dict:
    return dsn.model_dump() if hasattr(dsn, "model_dump") else dsn


def _compile_sql(content: str, variables: dict[str, str]) -> None:
    if _RISKY_SQL_RE.search(content):
        raise ValueError("造数 SQL 包含高风险语句，MCP/造数任务默认拒绝 DROP、TRUNCATE、ALTER、CREATE USER、GRANT")
    from app.services.sql_executor import split_sql_statements
    from app.services.sql_params import compile_statement, compile_variables

    definitions = compile_variables(variables)
    statements = split_sql_statements(content)
    if not statements:
        raise ValueError("没有可执行的 SQL 语句")
    for i, statement in enumerate(statements, 1):
        compile_statement(statement, definitions, f"造数 SQL 第 {i} 条")


def list_my_connections(user_id: str) -> dict:
    return {"connections": [connection_manager.to_public_connection(c) for c in db.list_connections(user_id)]}


def list_jobs(user_id: str) -> list[dict]:
    return [safe_job(job) for job in db.list_datagen_jobs_for_user(user_id)]


def list_tables(user_id: str, connection_id: str, query: str = "") -> dict:
    dsn = connection_manager.resolve_connection(user_id, connection_id)
    return {"tables": list_table_names(dsn.model_dump(), query)}


def get_table_metadata(user_id: str, connection_id: str, table: str) -> dict:
    dsn = connection_manager.resolve_connection(user_id, connection_id)
    return read_table_metadata(dsn.model_dump(), table)


def validate_rules(user_id: str, connection_id: str, table: str, rules: list[dict], row_count: int) -> dict:
    metadata = get_table_metadata(user_id, connection_id, table)
    return {"ok": True, "errors": validate_field_rules(metadata, rules, row_count), "metadata": metadata}


def apply_rules(user_id: str, connection_id: str, table: str, rules: list[dict], row_count: int) -> dict:
    metadata = get_table_metadata(user_id, connection_id, table)
    return {**apply_database_rules(metadata, rules, row_count), "metadata": metadata}


def dependency_plan(user_id: str, connection_id: str, table: str, target_rows: int, strategy: str) -> dict:
    dsn = connection_manager.resolve_connection(user_id, connection_id)
    return build_dependency_plan(dsn.model_dump(), table, target_rows, strategy)


def dependency_plan_from_body(user_id: str, body) -> dict:
    dsn = resolve_body_dsn(body, user_id)
    return build_dependency_plan(_dsn_dict(dsn), body.table, body.target_rows, body.strategy)


def metadata_from_body(user_id: str, body) -> dict:
    dsn = resolve_body_dsn(body, user_id)
    return read_table_metadata(_dsn_dict(dsn), body.table)


def list_tables_from_body(user_id: str, body) -> dict:
    dsn = resolve_body_dsn(body, user_id)
    return {"tables": list_table_names(_dsn_dict(dsn), body.query)}


def validate_rules_from_body(user_id: str, body) -> dict:
    metadata = metadata_from_body(user_id, body)
    return {"ok": True, "errors": validate_field_rules(metadata, body.rules, body.row_count), "metadata": metadata}


def apply_rules_from_body(user_id: str, body) -> dict:
    metadata = metadata_from_body(user_id, body)
    return {**apply_database_rules(metadata, body.rules, body.row_count), "metadata": metadata}


def create_job(user_id: str, body: DataGenJobCreate, *, allow_shell: bool = True) -> dict:
    content = body.content.strip()
    if not content:
        raise ValueError("造数内容为空")
    if body.mode == "shell":
        if not allow_shell or not settings.datagen_script_execution_enabled:
            raise ValueError("Shell 造数未开启（DATAGEN_SCRIPT_EXECUTION_ENABLED=false）")
    max_chars = settings.datagen_max_sql_chars if body.mode == "sql" else settings.datagen_max_script_chars
    if len(content) > max_chars:
        raise ValueError(f"造数内容超过 {max_chars} 字符限制")
    if body.mode == "sql":
        _compile_sql(content, body.variables)
    if body.dependency_strategy != "target_only" and not body.confirm_dependency_writes:
        raise ValueError("依赖表写入需要 confirm_dependency_writes=true")

    dsn = resolve_body_dsn(body, user_id)
    result = connection_manager.test_dsn(dsn)
    if not result["ok"]:
        raise ConnectionError(f"数据库连接失败：{result['error']}")

    applied_rules = body.field_rules
    unique_indexes = []
    if body.target_table:
        metadata = read_table_metadata(_dsn_dict(dsn), body.target_table)
        applied = apply_database_rules(metadata, body.field_rules, body.row_count)
        if not applied["ok"]:
            messages = "；".join(f"{item.get('column') or '-'}：{item['message']}" for item in applied["errors"])
            raise ValueError(f"数据库规则复验失败：{messages}")
        applied_rules = applied["rules"]
        unique_indexes = metadata.get("unique_indexes") or []

    job_id = uuid4().hex[:8]
    db.create_datagen_job(
        {
            "id": job_id,
            "owner_user_id": user_id,
            "name": body.name,
            "mode": body.mode,
            "status": "pending",
            "target_dsn_json": dsn.model_dump_json(),
            "input_json": json.dumps(
                {
                    "source": body.source,
                    "content": content,
                    "variables": body.variables,
                    "row_count": body.row_count,
                    "target_table": body.target_table,
                    "field_rules": applied_rules,
                    "unique_indexes": unique_indexes,
                    "dependency_strategy": body.dependency_strategy,
                    "dependency_plan": body.dependency_plan,
                    "confirm_dependency_writes": body.confirm_dependency_writes,
                },
                ensure_ascii=False,
            ),
            "created_at": now_iso(),
        }
    )
    try:
        datagen_executor.start(job_id)
    except Exception:
        logger.exception("start datagen job {} failed", job_id)
        raise
    job = db.get_datagen_job_for_user(user_id, job_id)
    return {"job_id": job_id, "status": job["status"] if job else "pending"}


def create_sql_job(user_id: str, payload: dict) -> dict:
    body = DataGenJobCreate(
        name=payload["name"],
        mode="sql",
        source=payload.get("source", "paste"),
        content=payload["sql"],
        variables=payload.get("variables") or {},
        row_count=payload.get("row_count", 1),
        target_table=payload.get("target_table"),
        field_rules=payload.get("field_rules") or [],
        connection_id=payload["connection_id"],
        dependency_strategy=payload.get("dependency_strategy", "target_only"),
        dependency_plan=payload.get("dependency_plan") or {},
        confirm_dependency_writes=payload.get("confirm_dependency_writes", False),
    )
    return create_job(user_id, body, allow_shell=False)


def get_job(user_id: str, job_id: str) -> dict:
    job = db.get_datagen_job_for_user(user_id, job_id)
    if not job:
        raise KeyError(f"造数任务 {job_id} 不存在")
    return safe_job(job)


def get_job_log(user_id: str, job_id: str, tail_chars: int = 200_000) -> dict:
    job = db.get_datagen_job_for_user(user_id, job_id)
    if not job:
        raise KeyError(f"造数任务 {job_id} 不存在")
    log_path = job.get("log_path")
    if not log_path or not Path(log_path).exists():
        return {"job_id": job_id, "log": ""}
    return {"job_id": job_id, "log": tail_file(Path(log_path), size=tail_chars)}


def stop_job(user_id: str, job_id: str) -> dict:
    if not db.get_datagen_job_for_user(user_id, job_id):
        raise KeyError(f"造数任务 {job_id} 不存在")
    return {"status": datagen_executor.stop(job_id)}
