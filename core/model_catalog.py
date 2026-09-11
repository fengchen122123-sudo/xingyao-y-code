"""CLI 与 GUI 共用的模型协议和预置厂商目录。"""

from copy import deepcopy


API_PROTOCOLS = (
    {"id": "anthropic", "label": "Anthropic Messages (/v1/messages)", "path": "/v1/messages"},
    {"id": "openai", "label": "Chat Completions (/chat/completions)", "path": "/chat/completions"},
    {"id": "responses", "label": "Responses (/responses)", "path": "/responses"},
)


PRESET_PROVIDERS = (
    {
        "id": "kimi-coding-openai",
        "name": "月之暗面kimi-for-coding（OpenAI格式）",
        "base_url": "https://api.kimi.com/coding/v1",
        "protocol": "openai",
        "models": [
            {"name": "kimi-for-coding", "context_window": 1000000, "supports_vision": True,
             "supports_function_calling": True, "max_tokens": 131072},
            {"name": "k3", "context_window": 1000000, "supports_vision": True,
             "supports_function_calling": True, "computer_protocol": "generic"},
            {"name": "k3-256k", "context_window": 256000, "supports_vision": True,
             "supports_function_calling": True, "computer_protocol": "generic"},
        ],
    },
)


def get_preset_providers() -> list[dict]:
    """返回隔离副本，避免 CLI/GUI 交互状态污染共享目录。"""
    return deepcopy(PRESET_PROVIDERS)


def get_preset_provider(preset_id: str) -> dict:
    for provider in PRESET_PROVIDERS:
        if provider["id"] == preset_id:
            return deepcopy(provider)
    raise KeyError(f"未知预置厂商: {preset_id}")


def protocol_choices() -> list[tuple[str, str]]:
    return [(item["id"], item["label"]) for item in API_PROTOCOLS]


def validate_protocol(protocol: str) -> str:
    if protocol not in {item["id"] for item in API_PROTOCOLS}:
        raise ValueError(f"不支持的 API 格式: {protocol}")
    return protocol


def litellm_model_name(protocol: str, model_name: str) -> str:
    """构造 LiteLLM 路由名；Responses 是 OpenAI 兼容传输而非 provider 前缀。"""
    validate_protocol(protocol)
    if model_name.startswith(("openai/", "anthropic/")):
        return model_name
    prefix = "openai" if protocol == "responses" else protocol
    return f"{prefix}/{model_name}"
