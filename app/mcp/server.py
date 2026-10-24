import argparse
from typing import Literal

from mcp.server.fastmcp import FastMCP

from app import db
from app.config import settings
from app.mcp import datagen_server
from app.services import connection_manager


INSTRUCTIONS = """
SQL Pulse datagen MCP server.

Use saved SQL Pulse connections only. Tools never return database passwords.
Read table metadata and validate rules before creating datagen SQL jobs.
Dependency writes require confirm_dependency_writes=true.
Shell datagen is not exposed through MCP.
"""


def resolve_mcp_user(user_id: str | None = None, username: str | None = None) -> dict | None:
    db.init_schema()
    resolved_user_id = user_id or settings.mcp_user_id
    resolved_username = username or settings.mcp_username
    if resolved_user_id:
        user = db.get_user(resolved_user_id)
        if user is None:
            raise ValueError(f"MCP user_id {resolved_user_id} 不存在")
        return user
    if resolved_username:
        user = db.get_user_by_username(resolved_username)
        if user is None:
            raise ValueError(f"MCP username {resolved_username} 不存在")
        return user
    if not settings.auth_enabled:
        return {"id": connection_manager.ANONYMOUS_USER_ID, "username": "anonymous"}
    raise ValueError("MCP 需要明确用户上下文：设置 MCP_USER_ID/MCP_USERNAME，或启动时传 --user-id/--user")


def create_mcp_server(user: dict | None) -> FastMCP:
    server = FastMCP(
        name="sqlpulse-datagen",
        instructions=INSTRUCTIONS.strip(),
        host=settings.mcp_host,
        port=settings.mcp_port,
    )

    @server.tool(description="列出当前 SQL Pulse 用户可用的已保存数据库连接；不返回数据库密码。")
    def list_my_connections() -> dict:
        return datagen_server.list_my_connections(user)

    @server.tool(description="查询当前用户某个连接下的数据表。")
    def list_datagen_tables(connection_id: str, query: str = "") -> dict:
        return datagen_server.list_datagen_tables(user, connection_id, query)

    @server.tool(description="读取目标表元数据，包括列、唯一索引、外键、检查约束和未识别信息。")
    def get_datagen_table_metadata(connection_id: str, table: str) -> dict:
        return datagen_server.get_datagen_table_metadata(user, connection_id, table)

    @server.tool(description="根据数据库元数据归一化字段规则，返回调整、错误、警告和 metadata。")
    def apply_datagen_rules(connection_id: str, table: str, rules: list[dict], row_count: int) -> dict:
        return datagen_server.apply_datagen_rules(user, connection_id, table, rules, row_count)

    @server.tool(description="只校验字段规则，不回写归一化结果。")
    def validate_datagen_rules(connection_id: str, table: str, rules: list[dict], row_count: int) -> dict:
        return datagen_server.validate_datagen_rules(user, connection_id, table, rules, row_count)

    @server.tool(description="生成依赖表补数计划。strategy 可为 target_only、direct_dependencies、full_dependencies。")
    def build_datagen_dependency_plan(
        connection_id: str,
        table: str,
        target_rows: int,
        strategy: Literal["target_only", "direct_dependencies", "full_dependencies"] = "direct_dependencies",
    ) -> dict:
        return datagen_server.build_datagen_dependency_plan(user, connection_id, table, target_rows, strategy)

    @server.tool(description="创建 SQL 造数任务并返回 job_id。只支持 SQL 模式，不接受数据库密码或任意 DSN。")
    def create_datagen_sql_job(
        connection_id: str,
        name: str,
        sql: str,
        variables: dict[str, str] | None = None,
        row_count: int = 1,
        target_table: str | None = None,
        field_rules: list[dict] | None = None,
        dependency_strategy: Literal["target_only", "direct_dependencies", "full_dependencies"] = "target_only",
        confirm_dependency_writes: bool = False,
        dependency_plan: dict | None = None,
    ) -> dict:
        return datagen_server.create_datagen_sql_job(
            user,
            {
                "connection_id": connection_id,
                "name": name,
                "sql": sql,
                "variables": variables or {},
                "row_count": row_count,
                "target_table": target_table,
                "field_rules": field_rules or [],
                "dependency_strategy": dependency_strategy,
                "confirm_dependency_writes": confirm_dependency_writes,
                "dependency_plan": dependency_plan or {},
            },
        )

    @server.tool(description="查询当前用户自己的造数任务状态。")
    def get_datagen_job(job_id: str) -> dict:
        return datagen_server.get_datagen_job(user, job_id)

    @server.tool(description="读取当前用户自己的造数任务日志尾部。")
    def get_datagen_job_log(job_id: str, tail_chars: int = 20_000) -> dict:
        return datagen_server.get_datagen_job_log(user, job_id, tail_chars)

    @server.tool(description="停止当前用户自己的运行中造数任务。")
    def stop_datagen_job(job_id: str) -> dict:
        return datagen_server.stop_datagen_job(user, job_id)

    return server


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SQL Pulse datagen MCP server.")
    parser.add_argument("--user-id", default=None, help="SQL Pulse user id for this MCP process.")
    parser.add_argument("--user", default=None, help="SQL Pulse username for this MCP process.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport. External desktop clients usually use stdio.",
    )
    parser.add_argument("--host", default=None, help="Host for SSE/streamable-http transports.")
    parser.add_argument("--port", type=int, default=None, help="Port for SSE/streamable-http transports.")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    if args.host:
        settings.mcp_host = args.host
    if args.port:
        settings.mcp_port = args.port
    user = resolve_mcp_user(user_id=args.user_id, username=args.user)
    create_mcp_server(user).run(transport=args.transport)


if __name__ == "__main__":
    main()
