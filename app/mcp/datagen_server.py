from app.mcp.auth import user_id_from_session
from app.services import datagen_jobs


def list_my_connections(user: dict | None) -> dict:
    return datagen_jobs.list_my_connections(user_id_from_session(user))


def list_datagen_tables(user: dict | None, connection_id: str, query: str = "") -> dict:
    return datagen_jobs.list_tables(user_id_from_session(user), connection_id, query)


def get_datagen_table_metadata(user: dict | None, connection_id: str, table: str) -> dict:
    return datagen_jobs.get_table_metadata(user_id_from_session(user), connection_id, table)


def apply_datagen_rules(user: dict | None, connection_id: str, table: str, rules: list[dict], row_count: int) -> dict:
    return datagen_jobs.apply_rules(user_id_from_session(user), connection_id, table, rules, row_count)


def validate_datagen_rules(user: dict | None, connection_id: str, table: str, rules: list[dict], row_count: int) -> dict:
    return datagen_jobs.validate_rules(user_id_from_session(user), connection_id, table, rules, row_count)


def build_datagen_dependency_plan(
    user: dict | None,
    connection_id: str,
    table: str,
    target_rows: int,
    strategy: str = "direct_dependencies",
) -> dict:
    return datagen_jobs.dependency_plan(user_id_from_session(user), connection_id, table, target_rows, strategy)


def create_datagen_sql_job(user: dict | None, payload: dict) -> dict:
    return datagen_jobs.create_sql_job(user_id_from_session(user), payload)


def get_datagen_job(user: dict | None, job_id: str) -> dict:
    return datagen_jobs.get_job(user_id_from_session(user), job_id)


def get_datagen_job_log(user: dict | None, job_id: str, tail_chars: int = 20_000) -> dict:
    return datagen_jobs.get_job_log(user_id_from_session(user), job_id, tail_chars)


def stop_datagen_job(user: dict | None, job_id: str) -> dict:
    return datagen_jobs.stop_job(user_id_from_session(user), job_id)


TOOLS = {
    "list_my_connections": list_my_connections,
    "list_datagen_tables": list_datagen_tables,
    "get_datagen_table_metadata": get_datagen_table_metadata,
    "apply_datagen_rules": apply_datagen_rules,
    "validate_datagen_rules": validate_datagen_rules,
    "build_datagen_dependency_plan": build_datagen_dependency_plan,
    "create_datagen_sql_job": create_datagen_sql_job,
    "get_datagen_job": get_datagen_job,
    "get_datagen_job_log": get_datagen_job_log,
    "stop_datagen_job": stop_datagen_job,
}
