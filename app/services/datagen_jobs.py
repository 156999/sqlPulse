import json
import hashlib
import re
from decimal import Decimal
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
from app.services.credential_crypto import encrypt_dsn_snapshot
from app.services.runner import now_iso
from app.services.script_runner import tail_file
from app.ai import key_store
from app.ai.diagnose_runner import _build_client
from app.ai.providers import PROVIDERS

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


def _ai_client_for_user(user_id: str):
    from app import db
    from app.ai.diagnose_runner import _server_config

    pref = db.get_ai_pref(user_id) or {}
    provider = pref.get("provider") or "deepseek"
    spec = PROVIDERS.get(provider)
    if not spec:
        raise ValueError("AI 厂商配置无效，请到用户信息页重新选择模型")
    model = pref.get("model") or spec.models[0]
    if model not in spec.models:
        model = spec.models[0]
    row = db.get_ai_key(user_id, provider)
    api_key = key_store.decrypt(row["key_encrypted"]) if row and row.get("key_encrypted") else None
    if row and row.get("key_encrypted") and not api_key:
        raise ValueError("已保存的 AI API Key 无法解密，请到用户信息页重新填写")
    if not api_key and provider == "deepseek" and _server_config.api_key:
        api_key = _server_config.api_key
    if not api_key:
        raise ValueError(f"{spec.label} 未配置 API Key，请到用户信息页填写")
    return _build_client(api_key, model, provider)


def _validate_ai_statements(metadata: dict, rules: list[dict], statements: list, row_count: int, fk_values_source: dict | None = None) -> str:
    from app.services.sql_executor import split_sql_statements

    if not isinstance(statements, list) or len(statements) != row_count:
        raise ValueError(f"AI 返回语句数量不匹配，应为 {row_count} 条")
    columns = {c["name"]: c for c in metadata.get("columns", [])}
    rule_map = {r.get("column"): r for r in rules}
    unique_rows = {tuple(idx["columns"]): set() for idx in metadata.get("unique_indexes", [])}
    lines = []
    safe_table = metadata["table"]
    for i, statement in enumerate(statements, 1):
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError(f"第 {i} 条 SQL 为空")
        parts = split_sql_statements(statement)
        if len(parts) != 1:
            raise ValueError(f"第 {i} 条必须只包含一条 INSERT")
        match = re.fullmatch(r"(?is)\s*INSERT\s+INTO\s+(`?[A-Za-z_][A-Za-z0-9_]*`?)\s*\(([^)]*)\)\s*VALUES\s*\((.*)\)\s*;?\s*", parts[0])
        if not match:
            raise ValueError(f"第 {i} 条不是受支持的 INSERT VALUES 语句")
        table = match.group(1)
        raw_names = [part.strip() for part in match.group(2).split(",")]
        if any(not re.fullmatch(r"`?[A-Za-z_][A-Za-z0-9_]*`?", name) for name in raw_names):
            raise ValueError(f"第 {i} 条字段列表格式无效")
        names = [part.strip("`\"") for part in raw_names]
        raw_values = match.group(3)
        values = []
        token, quoted, escaped = "", False, False
        for char in raw_values:
            if char == "'" and not escaped: quoted = not quoted
            if char == "," and not quoted:
                values.append(token.strip()); token = ""; continue
            token += char
            escaped = char == "\\" and not escaped
            if char != "\\": escaped = False
        values.append(token.strip())
        if table.strip("`\"") != safe_table or len(names) != len(values):
            raise ValueError(f"第 {i} 条目标表或字段/值数量无效")
        seen = set()
        if any(rule.get("generator") != "omit" and rule.get("column") not in names for rule in rules):
            raise ValueError(f"第 {i} 条缺少配置了生成规则的字段")
        parsed_values = {}
        for name, value in zip(names, values):
            name = name.strip("`\"")
            if name not in columns or name in seen:
                raise ValueError(f"第 {i} 条包含未知或重复字段：{name}")
            seen.add(name)
            col = columns[name]
            rule = rule_map.get(name, {})
            if rule.get("generator") == "omit" or "auto_increment" in str(col.get("extra", "")).lower() or "generated" in str(col.get("extra", "")).lower():
                raise ValueError(f"第 {i} 条不应写入字段 {name}")
            if value.upper() == "NULL":
                parsed_value = None
            elif re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", value):
                parsed_value = Decimal(value)
            elif len(value) >= 2 and value[0] == "'" and value[-1] == "'":
                parsed_value = value[1:-1].replace("''", "'").replace("\\\\", "\\")
            else:
                raise ValueError(f"第 {i} 条字段 {name} 不是安全 SQL 字面量")
            parsed_values[name] = parsed_value
            if name in {fk.get("COLUMN_NAME") for fk in metadata.get("foreign_keys", [])}:
                candidates = (fk_values_source or {}).get(name, [])
                if parsed_value is None or str(parsed_value) not in {str(value) for value in candidates}:
                    raise ValueError(f"第 {i} 条字段 {name} 必须使用父表中提供的真实外键值")
            if parsed_value is None and not col.get("nullable", True):
                raise ValueError(f"第 {i} 条字段 {name} 不允许 NULL")
            enum_values = col.get("enum_values") or []
            if parsed_value is not None and enum_values and str(parsed_value) not in [str(v) for v in enum_values]:
                raise ValueError(f"第 {i} 条字段 {name} 不在数据库 ENUM 允许值中")
            if isinstance(parsed_value, str) and col.get("max_length") and len(parsed_value) > int(col["max_length"]):
                raise ValueError(f"第 {i} 条字段 {name} 超出长度限制")
            params = rule.get("params") or {}
            generator = rule.get("generator")
            base = rule.get("base_generator") if generator == "sample" else generator
            if parsed_value is not None and base in ("rand", "randf"):
                try:
                    number = Decimal(str(parsed_value))
                    if number < Decimal(str(params.get("min", "-Infinity"))) or number > Decimal(str(params.get("max", "Infinity"))):
                        raise ValueError(f"第 {i} 条字段 {name} 超出配置范围")
                except Exception as exc:
                    if isinstance(exc, ValueError) and "超出配置范围" in str(exc): raise
                    raise ValueError(f"第 {i} 条字段 {name} 应为数值")
            if base == "pick" and params.get("values") and parsed_value not in params["values"] and str(parsed_value) not in [str(x) for x in params["values"]]:
                raise ValueError(f"第 {i} 条字段 {name} 不在枚举候选值中")
        for name, col in columns.items():
            if name not in seen and not col.get("nullable", True) and col.get("default") is None and "auto_increment" not in str(col.get("extra", "")).lower() and "generated" not in str(col.get("extra", "")).lower():
                raise ValueError(f"第 {i} 条缺少必填字段 {name}")
        for cols, values_seen in unique_rows.items():
            if all(name in seen for name in cols):
                key = tuple(str(parsed_values[name]) for name in cols)
                if key in values_seen:
                    raise ValueError(f"生成结果违反联合唯一约束：{', '.join(cols)}")
                values_seen.add(key)
        lines.append(parts[0].rstrip("; ") + ";")
    return "\n".join(lines)


def generate_ai_sql(user_id: str, body) -> dict:
    from app.services.datagen_rules import validate_field_rules

    metadata = metadata_from_body(user_id, body)
    # 单轮 LLM 输出长度有限，不能把表单允许的百万行数原样交给模型。
    # 采用明确的 AI 专用上限，普通大批量造数仍可走原有表单/规则执行链路。
    if body.row_count > 100:
        raise ValueError("AI 单次最多生成 100 条 SQL，请调低生成条数后重试；更大批量请使用现有表单造数")
    applied = apply_database_rules(metadata, body.rules, body.row_count)
    if not applied.get("ok"):
        errors = applied.get("errors") or validate_field_rules(metadata, body.rules, body.row_count)
        raise ValueError("字段规则未通过：" + "；".join(f"{e.get('column')}: {e.get('message')}" for e in errors))
    rules = applied["rules"]
    if any(r.get("status") == "unapplied" for r in rules):
        raise ValueError("请先应用数据库规则")
    configured = {rule.get("column") for rule in rules}
    missing = [c["name"] for c in metadata.get("columns", []) if c["name"] not in configured and not c.get("nullable", True) and c.get("default") is None and "auto_increment" not in str(c.get("extra", "")).lower() and "generated" not in str(c.get("extra", "")).lower()]
    if missing:
        raise ValueError("以下必填字段没有生成规则：" + "、".join(missing))
    fk_values = {}
    if metadata.get("foreign_keys"):
        dsn = resolve_body_dsn(body, user_id)
        from app.services.datagen_dependencies import _safe_identifier
        import pymysql
        conn = pymysql.connect(host=dsn.host, port=dsn.port, user=dsn.user, password=dsn.password,
                               database=dsn.database, connect_timeout=3, autocommit=True,
                               cursorclass=pymysql.cursors.DictCursor)
        try:
            with conn.cursor() as cur:
                for fk in metadata["foreign_keys"]:
                    column, parent_table = fk["COLUMN_NAME"], fk["REFERENCED_TABLE_NAME"]
                    parent_column = fk["REFERENCED_COLUMN_NAME"]
                    cur.execute(f"SELECT DISTINCT {_safe_identifier(parent_column)} AS value FROM {_safe_identifier(parent_table)} WHERE {_safe_identifier(parent_column)} IS NOT NULL ORDER BY {_safe_identifier(parent_column)} LIMIT 100")
                    fk_values[column] = [row["value"] for row in cur.fetchall()]
                    if not fk_values[column]:
                        raise ValueError(f"外键 {column} 对应父表 {parent_table}.{parent_column} 没有可用数据")
        finally:
            conn.close()
    schema = {
        "table": metadata["table"],
        "columns": [{k: c.get(k) for k in ("name", "data_type", "column_type", "nullable", "default", "max_length", "numeric_precision", "numeric_scale", "extra", "key", "enum_values", "sensitive")} for c in metadata["columns"]],
        "unique_indexes": metadata.get("unique_indexes", []),
        "foreign_keys": metadata.get("foreign_keys", []),
        "foreign_key_candidates": fk_values,
        "checks": metadata.get("checks", []),
        "rules": rules,
        "row_count": body.row_count,
    }
    messages = [
        {"role": "system", "content": "你是 MySQL 单表造数 SQL 生成器。只依据给定表结构、外键候选值和字段规则生成 JSON。JSON 格式为 {\"statements\":[...]}。必须恰好生成 row_count 条，每条是单行 INSERT INTO `目标表` (`列`) VALUES (字面量...)；不允许多语句、函数、子查询、注释、其他表或非 INSERT SQL。omit 列不可写。严格遵守范围、长度、枚举、非空和唯一约束。外键字段必须从 foreign_key_candidates 对应数组中选择真实值，候选数组不为空时必须生成 SQL。禁止输出密钥或其他解释文本。"},
        {"role": "user", "content": json.dumps(schema, ensure_ascii=False, default=str)},
    ]
    result = _ai_client_for_user(user_id).chat(messages, response_format={"type": "json_object"}, max_tokens=min(32768, max(2048, body.row_count * 350)))
    if not result.get("ok"):
        error = result.get("error") or {}
        raise ValueError(error.get("message") or "AI SQL 生成失败")
    response_text = (result.get("data") or {}).get("text") or ""
    response_data = result.get("data") or {}
    finish_reason = response_data.get("finish_reason")
    logger.info(
        "AI datagen response: user={} table={} rows={} model={} finish={} chars={} sha256={} preview={!r}",
        user_id, metadata["table"], body.row_count, response_data.get("model"), finish_reason,
        len(response_text), hashlib.sha256(response_text.encode("utf-8")).hexdigest()[:16],
        response_text[:1600],
    )
    if finish_reason == "length":
        raise ValueError("AI 输出达到长度上限，结果被截断；请减少生成条数后重试")
    try:
        payload = json.loads(response_text)
    except (ValueError, TypeError):
        if not response_text.strip():
            raise ValueError("AI 未返回 SQL 内容，请稍后重试")
        raise ValueError("AI 返回内容不是有效 JSON，请减少生成条数或重试")
    if not isinstance(payload, dict):
        raise ValueError("AI 返回 JSON 结构无效")
    if not isinstance(payload.get("statements"), list) or not payload["statements"]:
        reason = payload.get("error") or payload.get("reason") or "AI 未生成语句"
        raise ValueError(f"AI 无法生成 SQL：{reason}")
    sql = _validate_ai_statements(metadata, rules, payload.get("statements"), body.row_count, fk_values)
    if len(sql) > settings.datagen_max_sql_chars:
        raise ValueError(f"生成 SQL 超过 {settings.datagen_max_sql_chars} 字符限制，请减少生成条数")
    return {"ok": True, "table": metadata["table"], "statement_count": body.row_count, "sql": sql}


def create_ai_job(user_id: str, body) -> dict:
    generated = generate_ai_sql(user_id, body)
    sql_job = DataGenJobCreate(
        name=body.name,
        mode="sql",
        source="paste",
        content=generated["sql"],
        row_count=1,
        target_table=generated["table"],
        field_rules=body.rules,
        connection_id=body.connection_id,
        db_dsn=body.db_dsn,
        dependency_strategy=body.dependency_strategy,
        dependency_plan=body.dependency_plan,
        confirm_dependency_writes=body.confirm_dependency_writes,
    )
    job = create_job(user_id, sql_job, allow_shell=False, ai_generated=True)
    return {**job, "statement_count": generated["statement_count"]}


def create_job(user_id: str, body: DataGenJobCreate, *, allow_shell: bool = True,
               ai_generated: bool = False) -> dict:
    content = body.content.strip()
    if not content:
        raise ValueError("造数内容为空")
    if body.mode == "shell":
        if not allow_shell or not settings.datagen_script_execution_enabled:
            raise ValueError("Shell 造数未开启（DATAGEN_SCRIPT_EXECUTION_ENABLED=false）")
    max_chars = settings.datagen_max_script_chars if body.mode == "shell" else settings.datagen_max_sql_chars
    if len(content) > max_chars:
        raise ValueError(f"造数内容超过 {max_chars} 字符限制")
    if body.mode in ("sql", "multi"):
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
            "target_dsn_json": json.dumps(encrypt_dsn_snapshot(_dsn_dict(dsn)), ensure_ascii=False),
            "input_json": json.dumps(
                {
                    "source": body.source,
                    "ai_generated": ai_generated,
                    "content": content,
                    "variables": body.variables,
                    "row_count": body.row_count,
                    "concurrency": body.concurrency,
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
        connection_id=payload.get("connection_id"),
        db_dsn=payload.get("db_dsn"),
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
