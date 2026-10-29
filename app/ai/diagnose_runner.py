"""网页端 AI 诊断的后台执行：ai_jobs 状态机 + daemon 线程（复刻 runner.py 的模式）。

密钥来源优先级：用户自填 key > 服务器默认 key（LLM_API_KEY）> 无（job 会失败）。
线程内任何异常都落 ai_jobs.error_code，不冒到请求线程；诊断结果整体存 result_json，
其中不含任何密钥。key_state()/job_view() 是报告页与片段的模板数据入口。
"""
import json
import math
import re
import threading
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit
from uuid import uuid4

from loguru import logger

from app import db
from app.ai import key_store
from app.ai.diagnose import diagnose_run
from app.ai.llm_client import LlmClient, LlmConfig
from app.ai.providers import PROVIDERS, resolve_base_url

# 同一用户同时进行的诊断上限：防止多页面/误点重复烧钱
MAX_INFLIGHT_PER_USER = 2

# 服务端默认模型配置：进程内固定一份，报告页每次渲染不重复读 .env
_server_config = LlmConfig()


class _JobError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def key_state(user_id: str) -> dict:
    """当前厂商/模型与各厂商密钥状态（页面与报告卡片共用）。

    source：user=用户 key；server=服务器默认 key（仅 DeepSeek）；
    broken=密文解不开；none=未配置。删除密钥会保留 ai_prefs 里的模型偏好。
    """
    rows = {row["provider"]: row for row in db.list_ai_keys(user_id)}
    pref = db.get_ai_pref(user_id) or {}
    providers = []
    for spec in PROVIDERS.values():
        row = rows.get(spec.key)
        entry = {"key": spec.key, "label": spec.label, "models": list(spec.models),
                 "source": "none", "masked": None}
        if row and row.get("key_encrypted"):
            if key_store.decrypt(row["key_encrypted"]) is None:
                entry["source"], entry["masked"] = "broken", row["masked"]
            else:
                entry["source"], entry["masked"] = "user", row["masked"]
        elif spec.key == "deepseek" and _server_config.api_key:
            entry["source"] = "server"
            entry["masked"] = key_store.mask(_server_config.api_key)
        providers.append(entry)

    active_key = pref.get("provider") or "deepseek"
    spec = PROVIDERS.get(active_key) or PROVIDERS["deepseek"]
    active = next(p for p in providers if p["key"] == spec.key)
    model = pref.get("model") or (spec.models[0] if spec.models else _server_config.model)
    if spec.models and model not in spec.models:
        model = spec.models[0]
    return {
        "source": active["source"], "masked": active["masked"],
        "provider": spec.key, "provider_label": spec.label,
        "model": model,
        "base_url_host": _base_url_host(resolve_base_url(spec)),
        "available_models": {p["key"]: p["models"] for p in providers},
        "providers": providers,
    }


def has_server_key(provider_key: str = "deepseek") -> bool:
    """服务器默认 key 只服务 DeepSeek 基线。"""
    return provider_key == "deepseek" and bool(_server_config.api_key)


def _base_url_host(base_url: str) -> str:
    try:
        return urlsplit(base_url).hostname or base_url
    except ValueError:
        return base_url


def _build_client(api_key: Optional[str], model: Optional[str] = None,
                  provider_key: Optional[str] = None) -> LlmClient:
    """用户密钥/服务器默认密钥 + 用户模型与厂商偏好 → 客户端。"""
    key = (provider_key or "").strip()
    spec = PROVIDERS.get(key) if key else None
    if spec is not None:
        options = {"provider_key": spec.key, "base_url": resolve_base_url(spec)}
        if api_key:
            options["api_key"] = api_key
        if model:
            options["model"] = model
        return LlmClient(LlmConfig(**options))
    if api_key:
        options = {"api_key": api_key}
        if model:
            options["model"] = model
        return LlmClient(LlmConfig(**options))
    if model:
        return LlmClient(LlmConfig(model=model))
    return LlmClient.from_env()


def start_job(run_id: str, user_id: str, *, client_factory=None) -> Optional[dict]:
    """创建 pending job 并启动后台线程；client_factory(api_key, model, provider_key)
    是单测/验证的注入缝。"""
    job_id = uuid4().hex
    db.create_ai_job(job_id, run_id, user_id, "pending")
    threading.Thread(
        target=_run, args=(job_id, run_id, user_id, client_factory), daemon=True
    ).start()
    return db.get_ai_job(job_id)


def _run(job_id: str, run_id: str, user_id: str, client_factory) -> None:
    db.update_ai_job(job_id, {"status": "running", "started_at": now_iso()})
    error_code = error_msg = None
    try:
        pref = db.get_ai_pref(user_id) or {}
        spec = PROVIDERS.get(pref.get("provider") or "deepseek") or PROVIDERS["deepseek"]
        provider_key = spec.key
        model = pref.get("model") or (spec.models[0] if spec.models else None)
        if spec.models and model not in spec.models:
            model = spec.models[0]
        row = db.get_ai_key(user_id, provider_key)
        api_key = None
        if row and row.get("key_encrypted"):
            api_key = key_store.decrypt(row["key_encrypted"])
            if api_key is None:
                raise _JobError(
                    "KEY_UNDECRYPTABLE",
                    "已保存的 API Key 无法解密（APP_SECRET_KEY 可能已更换），请重新填写",
                )
        elif provider_key == "deepseek" and _server_config.api_key:
            api_key = _server_config.api_key
        if not api_key:
            raise _JobError(
                "LLM_CONFIG_ERROR",
                f"{spec.label} 未配置 API Key，请到用户信息页填写",
            )
        client = (client_factory(api_key, model, provider_key) if client_factory
                  else _build_client(api_key, model, provider_key))
        result = diagnose_run(run_id, client=client)
        if result.get("ok") is True:
            data = result.get("data")
            if isinstance(data, dict):
                data["provider"] = provider_key
            db.update_ai_job(job_id, {
                "status": "finished",
                "result_json": _dump(result),
                "ended_at": now_iso(),
            })
            logger.info("ai job {} finished: run={}", job_id, run_id)
            return
        err = result.get("error") or {}
        code = err.get("code") or "DIAGNOSE_FAILED"
        message = err.get("message") or "诊断失败"
        if code == "LLM_FAILED":
            # diagnose 把客户端错误码折进了 message（"模型未成功返回：{code}"）；
            # 拆出内层码，页面才能映射成可读提示。
            m = re.search(r"模型未成功返回：([A-Z_]+)", message)
            if m:
                code = m.group(1)
        raise _JobError(code, message)
    except _JobError as e:
        error_code, error_msg = e.code, e.message
        logger.warning("ai job {} failed: {} {}", job_id, error_code, error_msg)
    except Exception as e:
        error_code, error_msg = "JOB_INTERNAL_ERROR", f"{type(e).__name__}: {e}"
        logger.exception("ai job {} crashed", job_id)
    db.update_ai_job(job_id, {
        "status": "failed", "error_code": error_code, "error_msg": error_msg,
        "ended_at": now_iso(),
    })


def _dump(obj) -> str:
    """严格序列化；出现非有限浮点数时替换为 null 后重试。"""
    try:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False)
    except ValueError:
        return json.dumps(_replace_nonfinite(obj), ensure_ascii=False, allow_nan=False)


def _replace_nonfinite(obj):
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _replace_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_nonfinite(v) for v in obj]
    return obj


def job_view(job: Optional[dict]) -> dict:
    """job 行 → 模板视图：解析 result_json / 错误信息，不含密钥。"""
    if not job:
        return {"status": "none"}
    view = {
        "job_id": job["id"], "status": job["status"],
        "created_at": job["created_at"], "started_at": job["started_at"],
        "ended_at": job["ended_at"],
    }
    if job.get("result_json"):
        try:
            result = json.loads(job["result_json"])
        except ValueError:
            view["error"] = {"code": "RESULT_CORRUPT", "message": "诊断结果数据损坏"}
            return view
        view["result"] = result.get("data") or {}
        view["warnings"] = result.get("warnings") or []
    if job.get("error_code"):
        view["error"] = {"code": job["error_code"], "message": job["error_msg"] or ""}
    return view
