"""MCP interface for SQL Pulse load-test runs."""
import argparse
import json
from typing import Optional
from typing import Any

from mcp.server.fastmcp import FastMCP

from app import db
from app.config import settings
from app.mcp.server import resolve_mcp_user
from app.models import TaskCreate, SqlInput
from app.services import connection_manager, run_jobs
from app.services.run_form import preview


def create_run_mcp(user: dict | None) -> FastMCP:
    server = FastMCP(name="sqlpulse-loadtest", instructions=(
        "SQL Pulse load test MCP. Use saved connections only. Preview inputs before starting; "
        "poll get_run until finished/failed/cancelled, then read report."
    ), host=settings.mcp_host, port=settings.mcp_port)
    user_id = connection_manager.current_user_id(user)

    @server.tool(description="列出当前用户保存的数据库连接，不包含密码。")
    def list_my_connections() -> dict[str, Any]:
        return {"connections": [connection_manager.to_public_connection(c) for c in db.list_connections(user_id)]}

    @server.tool(description="校验压测 SQL 或任务组配置，不执行压测。")
    def preview_run(sql_content: Optional[str] = None, groups: Optional[list[dict]] = None,
                    variables: Optional[dict[str, str]] = None) -> dict[str, Any]:
        try:
            body = SqlInput(sql_content=sql_content, groups=groups, variables=variables or {})
            return preview(body)
        except Exception as exc:
            return {"ok": False, "errors": [{"message": str(exc)}]}

    @server.tool(description="启动异步 SQL 压测任务。仅支持当前用户已保存的 connection_id。")
    def create_run(name: str, connection_id: str, concurrency: int, spawn_rate: int, duration_sec: int,
                   sql_content: Optional[str] = None, groups: Optional[list[dict]] = None,
                   variables: Optional[dict[str, str]] = None, sql_source: str = "mcp") -> dict[str, Any]:
        try:
            if sql_content is not None and len(sql_content) > 200_000:
                return {"error": "sql_content 超过 200000 字符限制"}
            if groups is not None and len(groups) > 100:
                return {"error": "groups 最多 100 组"}
            body = TaskCreate(name=name, connection_id=connection_id, concurrency=concurrency,
                              spawn_rate=spawn_rate, duration_sec=duration_sec,
                              sql_content=sql_content, groups=groups, variables=variables or {},
                              sql_source=sql_source)
            return run_jobs.create_run(user_id, body)
        except Exception as exc:
            return {"error": str(getattr(exc, "detail", str(exc)))}

    @server.tool(description="查询自己的压测任务状态与最终/当前摘要指标。")
    def get_run(run_id: str) -> dict[str, Any]:
        try: return run_jobs.run_view(run_jobs.require_owned_run(user_id, run_id))
        except KeyError: return {"error": "任务不存在"}

    @server.tool(description="查询自己的压测任务指标时序，最多返回 300 个点。")
    def get_run_metrics(run_id: str, ts_from: Optional[int] = None, ts_to: Optional[int] = None) -> dict[str, Any]:
        try: run_jobs.require_owned_run(user_id, run_id)
        except KeyError: return {"error": "任务不存在"}
        points = db.get_metrics(run_id, ts_from, ts_to)
        stride = max(1, (len(points) + 299) // 300)
        return {"run_id": run_id, "points": points[::stride], "total_points": len(points)}

    @server.tool(description="停止自己的运行中压测任务。")
    def stop_run(run_id: str) -> dict[str, Any]:
        try:
            run_jobs.require_owned_run(user_id, run_id)
            from app.services.runner import runner
            return {"status": runner.stop(run_id)}
        except KeyError: return {"error": "任务不存在"}
        except Exception as exc: return {"error": str(exc)}

    @server.tool(description="读取自己的压测报告。任务结束后返回 L0/L1 与 Markdown。")
    def get_run_report(run_id: str) -> dict[str, Any]:
        try: run_jobs.require_owned_run(user_id, run_id)
        except KeyError: return {"error": "任务不存在"}
        report = db.get_report(run_id)
        if not report:
            from app.services.report import generate_report
            report = generate_report(run_id)
        if not report: return {"status": "pending", "message": "报告尚未生成"}
        try:
            with open(report["md_path"], encoding="utf-8") as f: markdown = f.read()
        except OSError: markdown = ""
        return {"run_id": run_id, "l0": json.loads(report["l0_json"]),
                "l1": json.loads(report["l1_json"]), "markdown": markdown}

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id")
    parser.add_argument("--user")
    parser.add_argument("--transport", choices=("stdio", "sse", "streamable-http"), default="stdio")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.host: settings.mcp_host = args.host
    if args.port: settings.mcp_port = args.port
    user = resolve_mcp_user(args.user_id, args.user)
    create_run_mcp(user).run(transport=args.transport)


if __name__ == "__main__": main()
