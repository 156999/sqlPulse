"""网页端 AI 诊断：密钥表单（表单 POST + 303 重定向）与 htmx 诊断片段。

密钥只经表单体传输，日志/响应/HTML 只出现掩码；诊断 job 由 diagnose_runner
在后台线程执行，页面用 htmx 轮询片段直到 finished/failed。
"""
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import db
from app.ai import diagnose_runner, key_store
from app.auth import require_user
from app.routers.pages import _profile_context
from app.services import connection_manager
from app.templating import templates

router = APIRouter()


def _user_id(user: Optional[dict]) -> str:
    return connection_manager.current_user_id(user)


@router.post("/api/ai/key")
def save_ai_key(request: Request, api_key: str = Form(""),
                user: Optional[dict] = Depends(require_user)):
    key_store.warn_default_secret()
    api_key = api_key.strip()
    error = key_store.validate(api_key)
    if error:
        ctx = _profile_context(request, error)
        return templates.TemplateResponse(request, "profile.html", ctx, status_code=400)
    db.upsert_ai_key(_user_id(user), key_store.encrypt(api_key), key_store.mask(api_key))
    return RedirectResponse("/profile#ai-key", status_code=303)


@router.post("/api/ai/key/delete")
def delete_ai_key(user: Optional[dict] = Depends(require_user)):
    db.delete_ai_key(_user_id(user))
    return RedirectResponse("/profile#ai-key", status_code=303)


@router.post("/api/runs/{run_id}/ai/diagnose")
def start_diagnose(request: Request, run_id: str, user: Optional[dict] = Depends(require_user)):
    if not db.get_run(run_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    user_id = _user_id(user)
    inflight = db.list_inflight_ai_jobs(run_id)
    if inflight:
        job = inflight[0]  # 已有进行中的诊断：复用该 job，不重复建
    elif db.count_inflight_ai_jobs_by_user(user_id) >= diagnose_runner.MAX_INFLIGHT_PER_USER:
        return _fragment(request, run_id, None, user_id, error={
            "code": "CONCURRENCY_LIMIT",
            "message": "同时进行的 AI 诊断已达上限，请等之前的诊断结束后再试",
        })
    else:
        job = diagnose_runner.start_job(run_id, user_id)
    return _fragment(request, run_id, job, user_id)


@router.get("/api/runs/{run_id}/ai/diagnose/{job_id}/fragment")
def diagnose_fragment(request: Request, run_id: str, job_id: str, user: Optional[dict] = Depends(require_user)):
    if not db.get_run(run_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    job = db.get_ai_job(job_id)
    if not job or job["run_id"] != run_id:
        raise HTTPException(status_code=404, detail="诊断任务不存在")
    return _fragment(request, run_id, job, _user_id(user))


def _fragment(request: Request, run_id: str, job: Optional[dict], user_id: str,
              error: Optional[dict] = None):
    if error is not None:
        job_view = {"status": "failed", "error": error}
    else:
        job_view = diagnose_runner.job_view(job)
    return templates.TemplateResponse(request, "ai_diagnose.html", {
        "fragment_only": True, "run_id": run_id,
        "ai": diagnose_runner.key_state(user_id),
        "job_view": job_view,
    })
