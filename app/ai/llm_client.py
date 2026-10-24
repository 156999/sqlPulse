"""SQLPulse 模型客户端（DeepSeek Chat Completions）。

只支持一种协议：``POST {base_url}/chat/completions``，``Authorization: Bearer <key>``。
不声称兼容其它 "OpenAI-compatible" 服务，换服务商需要另写适配。
以下为 2026-09 官方文档（api-docs.deepseek.com）中与本模块相关的要点：

- 模型名：``deepseek-flash`` / ``deepseek-v4-pro``。
- 可选 JSON 模式：``response_format={"type": "json_object"}``（本客户端通过 ``chat()``
  的同名参数透传，默认不发）。开启后服务端保证 ``content`` 是合法 JSON，但官方要求
  调用方**自己**在提示词里指示模型输出 JSON，否则可能一直输出空白直到触顶。
- 思考模式默认开启（``thinking.type=enabled``，``reasoning_effort`` 默认 high）。
  思考模式下 ``temperature`` / ``presence_penalty`` / ``frequency_penalty`` 不生效；
  ``tool_choice`` 只支持 ``none`` / ``auto``，``required`` 或指定具体工具会返回 400。
- **请求带 ``tools`` 时，历史 assistant 消息的 ``reasoning_content`` 必须原样回传**，
  否则 400 —— ``assistant_message()`` 负责保留。
- 错误码：400 格式错 / 401 鉴权失败 / 402 余额不足 / 422 参数错 / 429 限流 /
  500 服务错 / 503 过载。

职责边界：只负责发消息、收文本与工具调用、保留工具调用 ID、回传工具结果。
不读 SQLPulse 数据、不执行工具、不自动循环调用模型、不拼业务 Prompt、不写报告。
工具定义与模型返回的 ``arguments`` 都只当数据：不 eval、不 import、不执行。

``chat()`` 不抛异常，统一返回 ``{ok, data, warnings, error}``（与 app/ai/tools.py 同构）；
``error`` 为 ``{code, message, retryable}``。密钥只从配置读取，不写日志、不回显。
导入本模块不读配置、不建目录、不写文件；``LlmConfig`` 由调用方按需实例化。
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]

CHAT_PATH = "/chat/completions"
# max_tokens 允许范围（官方文档：1 ~ 384K）
MAX_TOKENS_LIMIT = 393216
THINKING_VALUES = ("enabled", "disabled")
# low/high/max 为文档值，minimal/medium/xhigh 为文档说明的兼容别名
EFFORT_VALUES = ("none", "low", "minimal", "medium", "high", "xhigh", "max")
TOOL_CHOICE_VALUES = ("none", "auto", "required")
MESSAGE_ROLES = ("system", "user", "assistant", "tool")

# 错误码 → 是否可重试（由调用方决定要不要再试；本客户端内部只按此做有限重试）
RETRYABLE_CODES = frozenset({
    "LLM_RATE_LIMITED", "LLM_SERVER_ERROR", "LLM_OVERLOADED",
    "LLM_TIMEOUT", "LLM_CONNECTION_ERROR",
})

_CRED_RE = re.compile(r"(?i)(api[_-]?key|authorization|password|passwd|pwd)\s*[=:]\s*[^\s,;]+")


class LlmConfig(BaseSettings):
    """模型配置：环境变量 ``LLM_*`` 优先，其次仓库根目录 ``.env``。

    api_key 只从环境/.env 读取，不提供默认值、不落盘、不打印。
    """

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8",
        env_prefix="LLM_", extra="ignore",
    )

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout_seconds: float = 60.0
    max_retries: int = 2
    # 以下三项留空表示用服务端默认（思考模式 enabled / effort high / 输出上限默认值）
    thinking: Optional[str] = None
    reasoning_effort: Optional[str] = None
    max_tokens: Optional[int] = None

    def masked(self) -> dict:
        """可安全打印/返回的配置快照（不含密钥本身）。"""
        return {
            "api_key_set": bool(self.api_key),
            "api_key_length": len(self.api_key) if self.api_key else 0,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
        }


def _redact(text: Any, secrets=()) -> str:
    """错误文本里可能的凭据先清洗，再返回/记录。"""
    if not isinstance(text, str):
        return text
    out = _CRED_RE.sub(lambda m: f"{m.group(1)}=***", text)
    for s in secrets:
        if s and s in out:
            out = out.replace(s, "***")
    return out


def _ok(data: dict, warnings: Optional[list] = None) -> dict:
    return {"ok": True, "data": data, "warnings": list(warnings or []), "error": None}


def _fail(code: str, message: str, retryable: bool, warnings: Optional[list] = None) -> dict:
    return {
        "ok": False,
        "data": None,
        "warnings": list(warnings or []),
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def _parse_tool_calls(raw_calls: Any, warnings: list) -> list:
    """把响应里的 tool_calls 规整成 {id, type, name, arguments_raw, arguments}。

    arguments_raw 是模型原样输出的字符串，回传时必须原样；arguments 是解析结果，
    解析失败为 None（模型不保证输出合法 JSON，也可能虚构参数，调用方必须校验）。
    """
    out: list = []
    for i, tc in enumerate(raw_calls or []):
        if not isinstance(tc, dict):
            warnings.append(f"第 {i} 个 tool_call 不是对象，已忽略")
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        raw = fn.get("arguments")
        parsed = None
        err = None
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except ValueError as e:
                err = f"arguments 不是合法 JSON：{e}"
            else:
                if isinstance(value, dict):
                    parsed = value
                else:
                    err = f"arguments 解析结果不是对象（{type(value).__name__}）"
        elif raw is None:
            err = "arguments 缺失"
        else:
            err = f"arguments 不是字符串（{type(raw).__name__}）"
        if err:
            warnings.append(f"tool_call {tc.get('id') or i} 的 {err}；调用方不要直接使用其入参")
        if not tc.get("id"):
            warnings.append(f"第 {i} 个 tool_call 没有 id，无法配对工具结果消息")
        out.append({
            "id": tc.get("id"),
            "type": tc.get("type") or "function",
            "name": fn.get("name"),
            "arguments_raw": raw,
            "arguments": parsed,
            "arguments_error": err,
        })
    return out


def assistant_message(result: dict) -> dict:
    """把一次成功响应的 data 还原成可回传的 assistant 消息。

    保留 tool_calls 的 id 与 arguments 原文，并保留 reasoning_content ——
    请求带 tools 时它必须原样回传，否则 API 返回 400。
    """
    data = result.get("data") if isinstance(result, dict) and "data" in result else result
    if not isinstance(data, dict):
        raise ValueError("assistant_message() 需要 chat() 返回的 data 或整个结果")
    msg: dict = {"role": "assistant", "content": data.get("text") or ""}
    if data.get("reasoning_content"):
        msg["reasoning_content"] = data["reasoning_content"]
    calls = data.get("tool_calls") or []
    if calls:
        msg["tool_calls"] = [
            {
                "id": c.get("id"),
                "type": c.get("type") or "function",
                "function": {"name": c.get("name"), "arguments": c.get("arguments_raw") or ""},
            }
            for c in calls
        ]
    return msg


def tool_result_message(tool_call_id: str, content) -> dict:
    """构造工具结果消息（role=tool），与 assistant 消息里的 tool_call id 配对。

    content 传 dict/list 会自动 JSON 序列化。回传顺序必须是
    [..., assistant 消息（含 tool_calls）, 工具结果消息, ...]。
    """
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        raise ValueError("tool_result_message() 需要非空 tool_call_id")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class LlmClient:
    """DeepSeek Chat Completions 客户端。线程安全（每次调用自建连接）。"""

    def __init__(self, config: LlmConfig):
        self.config = config

    @classmethod
    def from_env(cls) -> "LlmClient":
        return cls(LlmConfig())

    # ---- 请求构造与校验 ----

    def _endpoint(self) -> str:
        base = (self.config.base_url or "").strip().rstrip("/")
        if base.endswith(CHAT_PATH):
            return base
        return base + CHAT_PATH

    def _build_payload(self, messages, tools, tool_choice, thinking,
                       reasoning_effort, max_tokens, response_format) -> tuple:
        """返回 (payload, None) 或 (None, 错误结果)。"""
        if not isinstance(messages, list) or not messages:
            return None, _fail("LLM_INVALID_REQUEST", "messages 必须是非空列表", False)
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                return None, _fail("LLM_INVALID_REQUEST", f"messages[{i}] 不是对象", False)
            role = m.get("role")
            if role not in MESSAGE_ROLES:
                return None, _fail(
                    "LLM_INVALID_REQUEST",
                    f"messages[{i}].role={role!r} 非法，取值：{', '.join(MESSAGE_ROLES)}", False,
                )
            if role == "tool" and not m.get("tool_call_id"):
                return None, _fail(
                    "LLM_INVALID_REQUEST", f"messages[{i}] 是工具结果消息但缺少 tool_call_id", False,
                )
            if role == "assistant" and m.get("tool_calls") is not None:
                if not isinstance(m["tool_calls"], list):
                    return None, _fail(
                        "LLM_INVALID_REQUEST", f"messages[{i}].tool_calls 必须是列表", False,
                    )

        if tools is not None:
            if not isinstance(tools, list) or not tools:
                return None, _fail("LLM_INVALID_REQUEST", "tools 必须是非空列表或 None", False)
            for i, t in enumerate(tools):
                fn = t.get("function") if isinstance(t, dict) else None
                if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
                    return None, _fail(
                        "LLM_INVALID_REQUEST",
                        f"tools[{i}] 需要 {{'type': 'function', 'function': {{'name': ...}}}}", False,
                    )

        if isinstance(tool_choice, str) and tool_choice not in TOOL_CHOICE_VALUES:
            return None, _fail(
                "LLM_INVALID_REQUEST",
                f"tool_choice={tool_choice!r} 非法，取值：{', '.join(TOOL_CHOICE_VALUES)}", False,
            )
        if tool_choice is not None and not isinstance(tool_choice, (str, dict)):
            return None, _fail("LLM_INVALID_REQUEST", "tool_choice 必须是字符串或对象", False)

        eff_thinking = thinking if thinking is not None else self.config.thinking
        if eff_thinking is not None and eff_thinking not in THINKING_VALUES:
            return None, _fail(
                "LLM_INVALID_REQUEST",
                f"thinking={eff_thinking!r} 非法，取值：{', '.join(THINKING_VALUES)}", False,
            )
        eff_effort = reasoning_effort if reasoning_effort is not None else self.config.reasoning_effort
        if eff_effort is not None and eff_effort not in EFFORT_VALUES:
            return None, _fail(
                "LLM_INVALID_REQUEST",
                f"reasoning_effort={eff_effort!r} 非法，取值：{', '.join(EFFORT_VALUES)}", False,
            )
        eff_max_tokens = max_tokens if max_tokens is not None else self.config.max_tokens
        if eff_max_tokens is not None:
            if not isinstance(eff_max_tokens, int) or isinstance(eff_max_tokens, bool):
                return None, _fail("LLM_INVALID_REQUEST", "max_tokens 必须是整数", False)
            if not 1 <= eff_max_tokens <= MAX_TOKENS_LIMIT:
                return None, _fail(
                    "LLM_INVALID_REQUEST",
                    f"max_tokens 超出范围 1~{MAX_TOKENS_LIMIT}（收到 {eff_max_tokens}）", False,
                )

        # 本版只支持 JSON 模式；其它取值一律按参数错误返回，不静默忽略
        if response_format is not None:
            if not isinstance(response_format, dict) or response_format != {"type": "json_object"}:
                return None, _fail(
                    "LLM_INVALID_REQUEST",
                    'response_format 本版只支持 {"type": "json_object"}', False,
                )

        # 思考模式（含服务端默认开启的情况）不支持 required 与指定具体工具
        forced = tool_choice == "required" or isinstance(tool_choice, dict)
        if forced and eff_thinking != "disabled":
            return None, _fail(
                "LLM_INVALID_REQUEST",
                "思考模式下 tool_choice 只支持 none / auto；"
                "需要强制调用工具请先设置 thinking=disabled", False,
            )

        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            # 显式发送，避免依赖服务端默认值的变化
            "thinking": {"type": eff_thinking or "enabled"},
            "stream": False,
        }
        if eff_effort is not None:
            payload["reasoning_effort"] = eff_effort
        if eff_max_tokens is not None:
            payload["max_tokens"] = eff_max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        return payload, None

    # ---- 发送与解析 ----

    @staticmethod
    def _http_error(resp: httpx.Response, secrets=()) -> dict:
        try:
            body = resp.json()
        except ValueError:
            body = None
        detail = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or ""
            elif isinstance(err, str):
                detail = err
        if not detail:
            detail = (resp.text or "")[:300]
        detail = _redact(detail, secrets)
        code, retryable = {
            400: ("LLM_BAD_REQUEST", False),
            401: ("LLM_AUTH_FAILED", False),
            402: ("LLM_INSUFFICIENT_BALANCE", False),
            422: ("LLM_INVALID_PARAMS", False),
            429: ("LLM_RATE_LIMITED", True),
            500: ("LLM_SERVER_ERROR", True),
            503: ("LLM_OVERLOADED", True),
        }.get(resp.status_code, ("LLM_HTTP_ERROR", resp.status_code >= 500))
        message = f"HTTP {resp.status_code}"
        if detail:
            message += f"：{detail}"
        return _fail(code, message, retryable)

    def _parse_response(self, resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            return _fail("LLM_BAD_RESPONSE", "响应不是合法 JSON", False)
        if not isinstance(body, dict):
            return _fail("LLM_BAD_RESPONSE", "响应不是 JSON 对象", False)

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return _fail("LLM_BAD_RESPONSE", "响应缺少 choices", False)
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        finish_reason = choice.get("finish_reason")

        warnings: list = []
        tool_calls = _parse_tool_calls(message.get("tool_calls"), warnings)
        text = message.get("content")
        reasoning = message.get("reasoning_content")
        if finish_reason == "length":
            warnings.append("finish_reason=length：输出达到 max_tokens 上限被截断")
        elif finish_reason in ("content_filter", "insufficient_system_resource", "aborted"):
            warnings.append(f"finish_reason={finish_reason}：本轮输出未正常完成")
        if not tool_calls and not text:
            warnings.append("响应既没有文本也没有工具调用")

        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return _ok({
            "text": text,
            "reasoning_content": reasoning,
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
            "usage": usage,
            "model": body.get("model"),
            "id": body.get("id"),
        }, warnings)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> Optional[float]:
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return min(30.0, max(0.0, float(raw.strip())))
        except ValueError:
            return None  # HTTP-date 形式不支持，退回默认退避

    def chat(self, messages, *, tools=None, tool_choice=None, thinking=None,
             reasoning_effort=None, max_tokens=None, response_format=None) -> dict:
        """发一轮消息，返回 ``{ok, data, warnings, error}``。**不抛异常。**

        data：``text`` / ``reasoning_content`` / ``tool_calls``（含 id 与原始
        arguments）/ ``finish_reason`` / ``usage`` / ``model`` / ``id``。
        工具调用不会被执行，只原样返回给调用方决定下一步。
        ``response_format`` 默认 None（请求体里不带该字段，行为与旧版一致）；
        传 ``{"type": "json_object"}`` 开启 JSON 模式，调用方仍需在提示词里
        自行要求输出 JSON。
        """
        if not self.config.api_key:
            return _fail("LLM_CONFIG_ERROR", "未配置 LLM_API_KEY，无法调用模型", False)
        if not self.config.base_url:
            return _fail("LLM_CONFIG_ERROR", "未配置 LLM_BASE_URL", False)

        payload, err = self._build_payload(
            messages, tools, tool_choice, thinking, reasoning_effort, max_tokens,
            response_format,
        )
        if err is not None:
            return err

        url = self._endpoint()
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        secrets = (self.config.api_key,)
        attempts = max(0, int(self.config.max_retries)) + 1
        result = _fail("LLM_CLIENT_ERROR", "未发起请求", False)

        for attempt in range(attempts):
            retry_after = None
            try:
                with httpx.Client(timeout=self.config.timeout_seconds) as client:
                    resp = client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException:
                result = _fail(
                    "LLM_TIMEOUT",
                    f"请求超时（{self.config.timeout_seconds}s，第 {attempt + 1}/{attempts} 次）",
                    True,
                )
            except httpx.UnsupportedProtocol:
                return _fail("LLM_CONFIG_ERROR", f"LLM_BASE_URL 不是可用的 http(s) 地址：{url}", False)
            except httpx.HTTPError as e:
                result = _fail(
                    "LLM_CONNECTION_ERROR",
                    _redact(f"连接失败：{type(e).__name__}: {e}", secrets),
                    True,
                )
            else:
                if resp.status_code == 200:
                    return self._parse_response(resp)
                result = self._http_error(resp, secrets)
                if not result["error"]["retryable"]:
                    return result
                retry_after = self._retry_after(resp)

            if attempt + 1 < attempts:
                delay = min(8.0, 0.5 * (2 ** attempt))
                if retry_after is not None:
                    delay = max(delay, retry_after)
                time.sleep(delay)

        if attempts > 1:
            result["warnings"].append(f"已重试 {attempts - 1} 次仍未成功")
        return result
