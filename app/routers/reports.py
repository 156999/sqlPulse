from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse
from typing import Optional

from app import db
from app.auth import require_user
from app.services import connection_manager, run_jobs

router = APIRouter()


@router.get("/api/runs/{run_id}/report/download")
def download_report(run_id: str, user: Optional[dict] = Depends(require_user)):
    try: run_jobs.require_owned_run(connection_manager.current_user_id(user), run_id)
    except KeyError: raise HTTPException(status_code=404, detail="任务不存在")
    report = db.get_report(run_id)
    if not report:
        from app.services.report import generate_report
        report = generate_report(run_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告尚未生成")
    return FileResponse(
        report["md_path"], media_type="text/markdown",
        filename=f"sqlpulse_report_{run_id}.md",
    )
