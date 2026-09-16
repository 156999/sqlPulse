from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app import db
from app.config import settings

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/")
def index():
    return RedirectResponse(url="/runs/new")


@router.get("/runs/new")
def new_run_page(request: Request):
    default_dsn = {
        "host": settings.target_db_host,
        "port": settings.target_db_port,
        "user": settings.target_db_user,
        "password": settings.target_db_password,
        "database": settings.target_db_name,
    }
    return templates.TemplateResponse(request, "new_run.html", {"default_dsn": default_dsn})


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
    return templates.TemplateResponse(request, "report.html", {"run": run, "report": report})
