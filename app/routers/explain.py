"""EXPLAIN 采集的两个入口：只读 JSON 接口 + 展示页。

产物由 `app/services/explain_probe.py` 在**压测结束后**写入 `data/explain/{run_id}.json`
（调用点 `runner.py::LocustRunner._probe_explain_after_run()`）。
本模块只读它，不产生、不修改 —— 因此整条链路是"引擎写产物 / 页面按约定读"，不碰 DB schema。

注意：产物只在 run 走到收尾之后才存在。状态为 pending/running 的 run 打开此页
会看到空态卡片，这是预期行为，不是 404。
"""
from fastapi import APIRouter, HTTPException, Request

from app import db
from app.services import explain_probe, explain_view
from app.templating import templates

router = APIRouter()


@router.get("/api/runs/{run_id}/explain")
def get_explain(run_id: str):
    """原始产物。不存在时 404 —— 与 `/api/runs/{run_id}` 的口径一致。"""
    artifact = explain_probe.load_artifact(run_id)
    if artifact is None:
        if not db.get_run(run_id):
            raise HTTPException(status_code=404, detail=f"任务 {run_id} 不存在")
        raise HTTPException(status_code=404, detail=f"任务 {run_id} 没有 EXPLAIN 采集产物")
    return artifact


@router.get("/runs/{run_id}/explain")
def explain_page(request: Request, run_id: str):
    run = db.get_run(run_id)
    if not run:
        return templates.TemplateResponse(
            request, "error.html", {"message": f"任务 {run_id} 不存在"}, status_code=404
        )
    view = explain_view.build_view(explain_probe.load_artifact(run_id), run)
    return templates.TemplateResponse(request, "explain.html", {"run": run, "view": view})
