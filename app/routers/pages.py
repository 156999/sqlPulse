from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import db
from app.auth import get_current_user
from app.config import settings
from app.services import connection_manager
from app.templating import templates
from app.services.observability import build_snapshot

router = APIRouter()


@router.get("/")
def index():
    return RedirectResponse(url="/runs/new")


@router.get("/runs/new")
def new_run_page(request: Request):
    default_dsn = {
        "host": settings.target_db_host,
        "port": settings.target_db_port,
        "user": settings.target_db_user,
        "password": "",
        "database": settings.target_db_name,
    }
    user_id = connection_manager.current_user_id(get_current_user(request))
    connections = [connection_manager.to_public_connection(c) for c in db.list_connections(user_id)]
    return templates.TemplateResponse(
        request,
        "new_run.html",
        {"default_dsn": default_dsn, "connections": connections},
    )


@router.get("/runs")
def runs_page(request: Request):
    runs = db.list_runs()
    for r in runs:
        r["has_report"] = db.get_report(r["id"]) is not None
    return templates.TemplateResponse(request, "runs.html", {"runs": runs})


@router.get("/runs/{run_id}/monitor")
def monitor_page(request: Request, run_id: str):
    run = db.get_run(run_id)
    if not run:
        return templates.TemplateResponse(request, "error.html", {"message": f"任务 {run_id} 不存在"}, status_code=404)
    from app.services.runner import parse_sql_tasks
    tasks = parse_sql_tasks(run["sql_content"])
    return templates.TemplateResponse(
        request,
        "monitor.html",
        {"run": run, "tasks": tasks, "observability": build_snapshot(run, db.get_metrics(run_id))},
    )


def _ai_card_state(request: Request, run_id: str) -> dict:
    """AI 诊断卡片的模板上下文：密钥状态 + 最近一次 job 视图。"""
    from app.ai import diagnose_runner

    user_id = connection_manager.current_user_id(get_current_user(request))
    return {
        "ai": diagnose_runner.key_state(user_id),
        "job_view": diagnose_runner.job_view(db.get_latest_ai_job(run_id)),
    }


def _profile_context(request: Request, error: str = "") -> dict:
    """用户信息页上下文：账号信息 + AI 密钥状态。"""
    from app.ai import diagnose_runner

    user = get_current_user(request)
    user_id = connection_manager.current_user_id(user)
    return {"user": user, "ai": diagnose_runner.key_state(user_id), "ai_key_error": error}


@router.get("/profile")
def profile_page(request: Request):
    return templates.TemplateResponse(request, "profile.html", _profile_context(request))


def _report_context(request: Request, run_id: str) -> Optional[dict]:
    """报告页模板上下文；任务不存在返回 None。"""
    run = db.get_run(run_id)
    if not run:
        return None
    report = db.get_report(run_id)
    if not report:
        from app.services.report import generate_report
        report = generate_report(run_id)
    from app.services.l1 import build_view
    l1_view = build_view(report["l1"], report.get("l0") or {}) if report else None
    ctx = {"run": run, "report": report, "l1_view": l1_view}
    ctx.update(_ai_card_state(request, run_id))
    return ctx


@router.get("/runs/{run_id}/report")
def report_page(request: Request, run_id: str):
    ctx = _report_context(request, run_id)
    if ctx is None:
        return templates.TemplateResponse(request, "error.html", {"message": f"任务 {run_id} 不存在"}, status_code=404)
    return templates.TemplateResponse(request, "report.html", ctx)
