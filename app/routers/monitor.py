import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app import db
from app.config import settings
from app.services.collector import collector, read_per_sql_history

router = APIRouter()


@router.get("/api/runs/{run_id}/metrics")
def get_metrics(run_id: str, ts_from: int = None, ts_to: int = None):
    if not db.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"任务 {run_id} 不存在")
    points = db.get_metrics(run_id, ts_from, ts_to)
    return {"points": points}


@router.get("/api/runs/{run_id}/per-sql-metrics")
def get_per_sql_metrics(run_id: str):
    """每条 SQL 的 P95/P99 等时序（来源 locust --csv-full-history）。"""
    if not db.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"任务 {run_id} 不存在")
    path = settings.data_dir / "locust" / f"{run_id}_stats_history.csv"
    return read_per_sql_history(path)


@router.get("/api/runs/{run_id}/stream")
async def stream(run_id: str):
    if not db.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"任务 {run_id} 不存在")
    q = collector.subscribe(run_id)

    async def gen():
        try:
            # 先回填已有历史点，保证刷新页面不丢曲线
            for p in db.get_metrics(run_id):
                yield f"data: {json.dumps(p, ensure_ascii=False)}\n\n"
            while True:
                run = db.get_run(run_id)
                if not run or run["status"] != "running":
                    yield "event: end\ndata: {}\n\n"
                    break
                try:
                    point = await asyncio.wait_for(q.get(), timeout=5.0)
                    yield f"data: {json.dumps(point, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            collector.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
