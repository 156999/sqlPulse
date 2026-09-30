"""模型客户端验证入口。

用法：
    python -m app.ai.verify_llm_client [--prompt TEXT] [--tool-loop] [--check-errors]
                                       [--skip-chat] [--base-url URL] [--model NAME]
                                       [--timeout-seconds N] [--max-retries N]

- 默认动作：打印配置快照（不含密钥），做一次普通文本往返。
- --tool-loop   额外做一次工具调用往返：模型要求调用工具 → 回传工具结果 → 取最终回答。
                只验证「工具调用 ID 保留 + 工具结果回传」这条链路，**不执行任何真实工具**，
                发给模型的工具结果是一段固定的合成数据。
- --check-errors 额外做错误路径检查：缺密钥 / 入参非法 / 连接失败 / 鉴权失败（401 需外网）。
- --skip-chat   只做上面两项额外检查，不调用普通文本往返。

密钥只从 LLM_API_KEY（或仓库根目录 .env）读取，本入口不回显、不写日志、不接受命令行传入。

退出码：
    0  全部步骤成功
    1  部分步骤失败（或模型调用因缺少 LLM_API_KEY 未执行）
    2  全部步骤失败
"""
import argparse
import json
import math
import sys
import time

from app.ai.llm_client import (
    LlmClient, LlmConfig, assistant_message, tool_result_message,
)

# 只用于验证的合成工具定义与合成工具结果：不是 SQLPulse 的真实工具，也不会被执行
DEMO_TOOL = {
    "type": "function",
    "function": {
        "name": "get_run_metrics",
        "description": "读取压测任务的指标摘要（本工具为验证用替身，只返回固定示例数据）。",
        "parameters": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "8 位十六进制任务 ID"}},
            "required": ["run_id"],
        },
    },
}
DEMO_TOOL_RESULT = {
    "ok": True,
    "run_id": "bb01ea65",
    "data": {"note": "合成示例数据：本次验证未执行真实诊断工具", "summary": {"qps_mean": 12.3}},
}
DEFAULT_PROMPT = "任务 bb01ea65 表现如何？请先调用工具获取数据，再用一句话总结。"
DEFAULT_CHAT_PROMPT = "用一句话回答：pong"


def _replace_nonfinite(obj):
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _replace_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_nonfinite(v) for v in obj]
    return obj


def _dump(obj) -> tuple:
    """严格序列化（allow_nan=False）；出现非有限浮点数时替换为 null 后重试。"""
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), False
    except ValueError:
        return json.dumps(_replace_nonfinite(obj), ensure_ascii=False, indent=2, allow_nan=False), True


def _timed(fn) -> tuple:
    t0 = time.perf_counter()
    result = fn()
    return result, int((time.perf_counter() - t0) * 1000)


def _step(name: str, result: dict, ms: int, expect: str = None, extra: dict = None) -> dict:
    """把一次 chat()/检查结果记成一个步骤；expect 给出期望的错误码。"""
    step = {"step": name, "duration_ms": ms}
    if extra:
        step.update(extra)
    if result.get("ok"):
        step["ok"] = True
        step["data"] = result.get("data")
    else:
        step["ok"] = False
        step["error"] = result.get("error")
    if result.get("warnings"):
        step["warnings"] = result["warnings"]
    if expect is not None:
        got = (result.get("error") or {}).get("code")
        step["expected_error"] = expect
        step["ok"] = got == expect
        if not step["ok"]:
            step["note"] = f"期望错误码 {expect}，实际 {got}"
    return step


def _config_step(config: LlmConfig, overrides: dict) -> dict:
    snapshot = config.masked()
    for k, v in overrides.items():
        if v is not None:
            snapshot[k] = v
    warnings = []
    if not config.api_key:
        warnings.append("未配置 LLM_API_KEY：真实调用会失败，只能做 --check-errors 的错误路径检查")
    return {"step": "config", "ok": True, "data": snapshot, "warnings": warnings}


def _chat_step(client: LlmClient, prompt: str) -> dict:
    messages = [{"role": "user", "content": prompt}]
    result, ms = _timed(lambda: client.chat(messages))
    step = _step("chat", result, ms)
    if step["ok"]:
        data = step["data"]
        step["checks"] = {
            "has_text": bool(data.get("text")),
            "finish_reason": data.get("finish_reason"),
            "reasoning_returned": bool(data.get("reasoning_content")),
            "usage_present": bool(data.get("usage")),
            "tool_calls_present": bool(data.get("tool_calls")),
        }
    return step


def _tool_loop_step(client: LlmClient, prompt: str) -> dict:
    """一次工具调用往返：请求工具 → 回传合成工具结果 → 取最终回答。不执行任何真实工具。"""
    messages = [
        {"role": "system", "content": "你是 SQLPulse 的压测诊断助手。"},
        {"role": "user", "content": prompt},
    ]
    first, ms1 = _timed(lambda: client.chat(messages, tools=[DEMO_TOOL], tool_choice="auto"))
    step = {"step": "tool_loop", "duration_ms": ms1}
    if not first.get("ok"):
        step["ok"] = False
        step["error"] = first.get("error")
        step["phase"] = "第一次请求（期望模型发起工具调用）"
        return step

    data = first["data"]
    calls = data.get("tool_calls") or []
    if not calls:
        step["ok"] = False
        step["phase"] = "第一次请求"
        step["data"] = {"text": data.get("text"), "finish_reason": data.get("finish_reason")}
        step["note"] = "模型没有发起工具调用，无法验证工具结果回传链路"
        if first.get("warnings"):
            step["warnings"] = first["warnings"]
        return step

    call = calls[0]
    assistant_msg = assistant_message(first)
    tool_msg = tool_result_message(call["id"], DEMO_TOOL_RESULT)

    # 回传时保留 assistant 消息（含工具调用 ID 与 reasoning_content）与工具结果消息
    second_messages = messages + [assistant_msg, tool_msg]
    second, ms2 = _timed(
        lambda: client.chat(second_messages, tools=[DEMO_TOOL], tool_choice="auto")
    )
    step["duration_ms"] = ms1 + ms2
    step["checks"] = {
        "tool_call_id": call["id"],
        "tool_name": call["name"],
        "arguments_raw": call["arguments_raw"],
        "arguments_parsed": call["arguments"],
        "reasoning_content_round_tripped": bool(assistant_msg.get("reasoning_content")),
        "assistant_message_keys": sorted(assistant_msg.keys()),
        "round_trip_ok": bool(second.get("ok")),
        "final_finish_reason": (second.get("data") or {}).get("finish_reason") if second.get("ok") else None,
        "second_call_error": (second.get("error") or {}).get("code") if not second.get("ok") else None,
    }
    step["messages_sent"] = second_messages
    if second.get("ok"):
        step["ok"] = True
        step["data"] = {"final_text": second["data"].get("text"), "usage": second["data"].get("usage")}
    else:
        step["ok"] = False
        step["error"] = second.get("error")
        step["phase"] = "第二次请求（回传工具结果后）"
    warnings = list(first.get("warnings") or []) + list(second.get("warnings") or [])
    if warnings:
        step["warnings"] = warnings
    return step


def _error_steps(config: LlmConfig, real_base_url: str, model: str) -> list:
    """错误路径检查；前三项不依赖网络，401 一项需要外网。"""
    steps: list = []

    # 1) 缺密钥
    client = LlmClient(LlmConfig(api_key="", base_url=real_base_url, model=model, max_retries=0))
    result, ms = _timed(lambda: client.chat([{"role": "user", "content": "ping"}]))
    steps.append(_step("no_api_key", result, ms, expect="LLM_CONFIG_ERROR"))

    # 2) 入参校验（本地拦截；含思考模式与 tool_choice=required 的冲突）
    client = LlmClient(LlmConfig(api_key="verification-fake-key", base_url=real_base_url,
                                model=model, max_retries=0))
    for name, kwargs in (
        ("invalid_messages", {"messages": []}),
        ("invalid_max_tokens", {"messages": [{"role": "user", "content": "ping"}],
                                "max_tokens": 0}),
        ("thinking_with_required_tool_choice", {
            "messages": [{"role": "user", "content": "ping"}],
            "tools": [DEMO_TOOL], "tool_choice": "required",
        }),
    ):
        result, ms = _timed(lambda kw=kwargs: client.chat(**kw))
        steps.append(_step(name, result, ms, expect="LLM_INVALID_REQUEST"))

    # 3) 连接失败（本机 9 号端口，不发外部请求）
    client = LlmClient(LlmConfig(api_key="verification-fake-key",
                                 base_url="http://127.0.0.1:9", model=model, max_retries=0))
    result, ms = _timed(lambda: client.chat([{"role": "user", "content": "ping"}]))
    steps.append(_step("connection_error", result, ms, expect="LLM_CONNECTION_ERROR"))

    # 4) 鉴权失败：用无效密钥打真实地址，需要外网
    client = LlmClient(LlmConfig(api_key="verification-invalid-key",
                                 base_url=real_base_url, model=model, max_retries=0))
    result, ms = _timed(lambda: client.chat([{"role": "user", "content": "ping"}]))
    step = _step("auth_failed_401", result, ms, expect="LLM_AUTH_FAILED")
    step["external_dependency"] = f"需要能访问 {real_base_url}（用无效密钥，不产生费用）"
    if not step["ok"] and (result.get("error") or {}).get("code") == "LLM_CONNECTION_ERROR":
        step["note"] = "外网不可达，401 映射未验证"
    steps.append(step)
    return steps


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ai.verify_llm_client",
        description="验证模型客户端：配置、文本往返、工具调用回传、错误路径",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0=全部成功；1=部分失败；2=全部失败",
    )
    parser.add_argument("--prompt", default=None,
                        help=f"工具往返用的提示词（默认：{DEFAULT_PROMPT}）")
    parser.add_argument("--tool-loop", action="store_true",
                        help="额外验证工具调用往返（模型请求工具 → 回传合成工具结果 → 取最终回答）")
    parser.add_argument("--check-errors", action="store_true",
                        help="额外做错误路径检查（缺密钥/入参/连接/401）")
    parser.add_argument("--skip-chat", action="store_true", help="跳过普通文本往返")
    parser.add_argument("--base-url", default=None, help="覆盖 LLM_BASE_URL")
    parser.add_argument("--model", default=None, help="覆盖 LLM_MODEL")
    parser.add_argument("--timeout-seconds", type=float, default=None, help="覆盖 LLM_TIMEOUT_SECONDS")
    parser.add_argument("--max-retries", type=int, default=None, help="覆盖 LLM_MAX_RETRIES")
    args = parser.parse_args(argv)

    overrides = {
        "base_url": args.base_url, "model": args.model,
        "timeout_seconds": args.timeout_seconds, "max_retries": args.max_retries,
    }
    try:
        config = LlmConfig(**{k: v for k, v in overrides.items() if v is not None})
    except Exception as e:  # 配置本身非法（如超时不是数字）
        payload = {
            "config": overrides,
            "error": {"code": "LLM_CONFIG_ERROR", "message": f"{type(e).__name__}: {e}",
                      "retryable": False},
            "exit_code": 2,
        }
        print(_dump(payload)[0])
        print("退出码 2（配置错误）", file=sys.stderr)
        return 2

    steps: list = [_config_step(config, overrides)]
    client = LlmClient(config)

    if args.check_errors:
        steps += _error_steps(config, config.base_url, config.model)

    if not args.skip_chat:
        steps.append(_chat_step(client, DEFAULT_CHAT_PROMPT))
    if args.tool_loop:
        steps.append(_tool_loop_step(client, args.prompt or DEFAULT_PROMPT))

    executed = [s for s in steps if s.get("step") not in ("config",)]
    ok_flags = [s.get("ok") for s in executed]
    if not executed:
        exit_code = 2
    elif all(ok_flags):
        exit_code = 0
    elif any(ok_flags):
        exit_code = 1
    else:
        exit_code = 2

    payload = {
        "config": config.masked(),
        "steps": steps,
        "exit_code": exit_code,
        "notes": [
            "密钥只从 LLM_API_KEY / 仓库根目录 .env 读取，本入口不接受也不回显密钥。",
            "工具调用只做链路验证：不执行任何真实诊断工具，工具结果是合成数据。",
        ],
    }
    text, sanitized = _dump(payload)
    print(text)
    if sanitized:
        exit_code = max(exit_code, 1)
        print("注意：结果含非有限浮点数，已替换为 null", file=sys.stderr)
    if executed and not config.api_key:
        print("提示：未配置 LLM_API_KEY，真实调用步骤会失败（--check-errors 可离线验证错误路径）",
              file=sys.stderr)
    print(f"退出码 {exit_code}（0=全部成功，1=部分失败，2=全部失败）", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
