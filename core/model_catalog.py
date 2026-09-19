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
    },
    {
        "id": "moonshot",
        "name": "月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "protocol": "openai",
    },
    {
        "id": "deepseek",
        "name": "深度求索",
        "base_url": "https://api.deepseek.com",
        "protocol": "openai",
    },
    {
        "id": "agnes-ai",
        "name": "Agnes AI",
        "base_url": "https://apihub.agnes-ai.cn/v1",
        "protocol": "openai",
    },
    {
        "id": "token-rhythm",
        "name": "基元律动",
        "base_url": "https://tokenrhythm.studio/v1",
        "protocol": "openai",
    },
    {
        "id": "longcat",
        "name": "美团",
        "base_url": "https://api.longcat.chat/openai/v1",
        "protocol": "openai",
    },
    {
        "id": "sensenova",
        "name": "商汤日日新",
        "base_url": "https://token.sensenova.cn/v1",
        "protocol": "openai",
    },
    {
        "id": "stepfun",
        "name": "阶跃星辰",
        "base_url": "https://api.stepfun.com/step_plan/v1",
        "protocol": "openai",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "protocol": "openai",
    },
    {
        "id": "minimax",
        "name": "minimax",
        "base_url": "https://api.minimaxi.com/v1",
        "protocol": "openai",
    },
    {
        "id": "zhipu",
        "name": "智谱API",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "protocol": "openai",
    },
    {
        "id": "zhipu-coding-openai",
        "name": "智谱CodingPlan(OpenAI)",
        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
        "protocol": "openai",
    },
    {
        "id": "neohorse",
        "name": "neoHorse",
        "base_url": "https://tokenrhythm.studio/v1",
        "protocol": "openai",
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


def provider_route_name(protocol: str, model_name: str) -> str:
    """构造 provider 路由名（{protocol}/{model} 前缀串）；Responses 是 OpenAI
    兼容传输而非 provider 前缀。"""
    validate_protocol(protocol)
    if model_name.startswith(("openai/", "anthropic/")):
        return model_name
    prefix = "openai" if protocol == "responses" else protocol
    return f"{prefix}/{model_name}"
