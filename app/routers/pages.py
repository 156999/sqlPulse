from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import db
from app.auth import get_current_user
from app.config import settings
from app.services import connection_manager
from app.templating import templates

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
    return templates.TemplateResponse(request, "monitor.html", {"run": run, "tasks": tasks})


@router.get("/runs/{run_id}/report")
def report_page(request: Request, run_id: str):
    run = db.get_run(run_id)
    if not run:
        return templates.TemplateResponse(request, "error.html", {"message": f"任务 {run_id} 不存在"}, status_code=404)
    report = db.get_report(run_id)
    if not report:
        from app.services.report import generate_report
        report = generate_report(run_id)
    from app.services.l1 import build_view
    l1_view = build_view(report["l1"], report.get("l0") or {}) if report else None
    return templates.TemplateResponse(
        request, "report.html",
        {"run": run, "report": report, "l1_view": l1_view},
    )
