"""Thin MCP adapters for user-scoped SQL load-test operations."""
from app.mcp.auth import user_id_from_session
from app.services import run_jobs


def list_my_connections(user):
    return run_jobs.list_my_connections(user_id_from_session(user))


def preview_sql_run(sql_content=None, groups=None, variables=None):
    from app.models import SqlInput
    payload = {"variables": variables or {}}
    if sql_content is not None:
        payload["sql_content"] = sql_content
    if groups is not None:
        payload["groups"] = groups
    return run_jobs.preview_sql_run(SqlInput.model_validate(payload))


def create_sql_run(user, connection_id, name, concurrency, spawn_rate, duration_sec,
                   sql_content=None, groups=None, variables=None, sql_source="paste",
                   confirm_write_sql=False):
    from app.models import TaskCreate
    payload = {"connection_id": connection_id, "name": name, "concurrency": concurrency,
               "spawn_rate": spawn_rate, "duration_sec": duration_sec,
               "variables": variables or {}, "sql_source": sql_source}
    if sql_content is not None:
        payload["sql_content"] = sql_content
    if groups is not None:
        payload["groups"] = groups
    return run_jobs.create_sql_run(user_id_from_session(user), TaskCreate.model_validate(payload),
                                   mcp=True, confirm_write_sql=confirm_write_sql)


def get_sql_run(user, run_id):
    return run_jobs.get_sql_run(user_id_from_session(user), run_id)


def get_sql_run_metrics(user, run_id, since_ts=None, limit=120):
    return run_jobs.get_sql_run_metrics(user_id_from_session(user), run_id, since_ts, limit)


def stop_sql_run(user, run_id):
    return run_jobs.stop_sql_run(user_id_from_session(user), run_id)


def get_sql_run_report(user, run_id):
    return run_jobs.get_sql_run_report(user_id_from_session(user), run_id)


TOOLS = {
    "list_my_connections": list_my_connections,
    "preview_sql_run": preview_sql_run,
    "create_sql_run": create_sql_run,
    "get_sql_run": get_sql_run,
    "get_sql_run_metrics": get_sql_run_metrics,
    "stop_sql_run": stop_sql_run,
    "get_sql_run_report": get_sql_run_report,
}
