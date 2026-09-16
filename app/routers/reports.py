from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app import db

router = APIRouter()


@router.get("/api/runs/{run_id}/report/download")
def download_report(run_id: str):
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
