"""模型厂商注册表（服务端常量）。

base_url 只来自这里或服务端环境变量，**绝不接受用户输入**；模型由各厂商
models 白名单约束。新增厂商 = 加一条 ProviderSpec。
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    key: str                    # 内部标识，同时用于环境变量覆盖名
    label: str                  # 页面展示名
    base_url: str               # 服务端固定地址
    models: tuple[str, ...]     # 模型白名单（官方 ID）
    token_limit_field: str = "max_tokens"   # 该厂商的输出上限字段名
    max_output_tokens: int = 0              # 0 = 使用客户端全局上限
    supports_json_mode: bool = True         # 是否发送 response_format
    thinking_style: str = "deepseek"        # deepseek / thinking_object / qwen / none
    auth_style: str = "bearer"              # bearer / x-api-key


PROVIDERS: dict[str, ProviderSpec] = {
    "deepseek": ProviderSpec(
        key="deepseek", label="DeepSeek",
        base_url="https://api.deepseek.com",
        models=("deepseek-flash", "deepseek-v4-pro"),
        thinking_style="deepseek",
    ),
    "qwen": ProviderSpec(
        key="qwen", label="阿里云百炼 Qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=("qwen3.8-flash",),
        # 官方兼容接口只文档化了 max_tokens；thinking 默认开启，非流式 JSON 需显式关闭
        token_limit_field="max_tokens",
        max_output_tokens=131072,
        thinking_style="qwen",
    ),
    "mimo": ProviderSpec(
        key="mimo", label="小米 MiMo",
        base_url="https://api.xiaomimimo.com/v1",
        models=("mimo-v2.6-flash",),
        token_limit_field="max_completion_tokens",  # MiMo 官方字段（含推理 token）
        max_output_tokens=131072,
        thinking_style="thinking_object",
    ),
}


def resolve_base_url(spec: ProviderSpec) -> str:
    """服务端 base_url 覆盖：LLM_<PROVIDER>_BASE_URL；DeepSeek 兼容旧 LLM_BASE_URL。"""
    env_key = f"LLM_{spec.key.upper()}_BASE_URL"
    if spec.key == "deepseek":
        return (os.environ.get("LLM_BASE_URL", "").strip()
                or os.environ.get(env_key, "").strip()
                or spec.base_url)
    return os.environ.get(env_key, "").strip() or spec.base_url


def parse_model_value(value: str) -> tuple[ProviderSpec, str] | tuple[None, None]:
    """页面提交的 "provider|model" → (spec, model)。非法组合返回 (None, None)。"""
    if not isinstance(value, str) or "|" not in value:
        return None, None
    provider_key, _, model = value.partition("|")
    spec = PROVIDERS.get(provider_key.strip())
    model = model.strip()
    if spec is None or model not in spec.models:
        return None, None
    return spec, model
