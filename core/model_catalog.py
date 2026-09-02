"""CLI 与 GUI 共用的模型协议和预置厂商目录。"""

from copy import deepcopy


API_PROTOCOLS = (
    {"id": "anthropic", "label": "Anthropic Messages (/v1/messages)", "path": "/v1/messages"},
    {"id": "openai", "label": "Chat Completions (/chat/completions)", "path": "/chat/completions"},
    {"id": "responses", "label": "Responses (/responses)", "path": "/responses"},
)


PRESET_PROVIDERS = (
    {
        "id": "kimi-coding-anthropic",
        "name": "月之暗面kimi-for-coding",
        "base_url": "https://api.kimi.com/coding",
        "protocol": "anthropic",
        "models": [
            {"name": "kimi-for-coding", "context_window": 262144, "supports_vision": True},
            {"name": "k3", "context_window": 1000000, "supports_vision": True},
            {"name": "k3-256k", "context_window": 256000, "supports_vision": True},
        ],
    },
    {
        "id": "kimi-coding-openai",
        "name": "月之暗面kimi-for-coding（OpenAI格式）",
        "base_url": "https://api.kimi.com/coding/v1",
        "protocol": "openai",
        "models": [
            {"name": "kimi-for-coding", "context_window": 262144, "supports_vision": True},
            {"name": "k3", "context_window": 1000000, "supports_vision": True},
            {"name": "k3-256k", "context_window": 256000, "supports_vision": True},
        ],
    },
    {
        "id": "moonshot",
        "name": "月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "protocol": "openai",
        "models": [
            {"name": "kimi-k3", "context_window": 1000000, "supports_vision": True},
            {"name": "kimi-k2.7-code", "context_window": 262144, "supports_vision": False},
            {"name": "kimi-k2.6", "context_window": 262144, "supports_vision": False},
        ],
    },
    {
        "id": "deepseek",
        "name": "深度求索",
        "base_url": "https://api.deepseek.com",
        "protocol": "responses",
        "models": [
            {"name": "deepseek-v4-pro", "context_window": 1000000, "supports_vision": False},
            {"name": "deepseek-v4-flash", "context_window": 1000000, "supports_vision": False},
            {"name": "deepseek-v4-flash-vision-exp", "context_window": 1000000, "supports_vision": True},
        ],
    },
    {
        "id": "agnes-ai",
        "name": "Agnes AI",
        "base_url": "https://apihub.agnes-ai.cn/v1",
        "protocol": "openai",
        "models": [
            {"name": "agnes-2.0-flash", "context_window": 1000000, "supports_vision": True},
            {"name": "agnes-2.5-flash", "context_window": 1000000, "supports_vision": True},
        ],
    },
    {
        "id": "token-rhythm",
        "name": "基元律动",
        "base_url": "https://tokenrhythm.studio/v1",
        "protocol": "openai",
        "models": [
            {"name": "deepseek-v4-flash-0731", "context_window": 1000000, "supports_vision": False},
            {"name": "deepseek-v4-pro-0813", "context_window": 1000000, "supports_vision": False},
            {"name": "minimax-m2.5", "context_window": 200000, "supports_vision": False},
            {"name": "minimax-m2.7", "context_window": 200000, "supports_vision": False},
            {"name": "mimo-v2.5-pro", "context_window": 256000, "supports_vision": False},
            {"name": "kimi-k2.5", "context_window": 256000, "supports_vision": True},
            {"name": "kimi-k2.6", "context_window": 256000, "supports_vision": True},
            {"name": "kimi-k2.7-code", "context_window": 256000, "supports_vision": True},
            {"name": "qwen3.7-max", "context_window": 1000000, "supports_vision": False},
            {"name": "qwen3.8-max", "context_window": 1000000, "supports_vision": True},
            {"name": "qwen3.8-27b", "context_window": 1000000, "supports_vision": True},
            {"name": "glm-5", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.1", "context_window": 200000, "supports_vision": False},
            {"name": "glm-5.2", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.3", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.3-flash", "context_window": 1000000, "supports_vision": True},
            {"name": "seed-2.1-turbo", "context_window": 262144, "supports_vision": True},
            {"name": "seed-2.1-pro", "context_window": 262144, "supports_vision": True},
        ],
    },
    {
        "id": "longcat",
        "name": "美团",
        "base_url": "https://api.longcat.chat/openai/v1",
        "protocol": "openai",
        "models": [
            {"name": "LongCat-2.0", "context_window": 1000000, "supports_vision": False},
        ],
    },
    {
        "id": "sensenova",
        "name": "商汤日日新",
        "base_url": "https://token.sensenova.cn/v1",
        "protocol": "openai",
        "models": [
            {"name": "sensenova-6.8-flash-lite", "context_window": 256000, "supports_vision": True},
            {"name": "deepseek-v4-pro", "context_window": 1000000, "supports_vision": False},
            {"name": "deepseek-v4-flash", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.2", "context_window": 1000000, "supports_vision": False},
            {"name": "kimi-k3", "context_window": 1000000, "supports_vision": True},
        ],
    },
    {
        "id": "volcengine-agent-plan",
        "name": "火山AgnetPlan",
        "base_url": "https://ark.cn-beijing.volces.com/api/plan",
        "protocol": "anthropic",
        "models": [
            {"name": "doubao-seed-2.0-code", "context_window": 256000, "supports_vision": True},
            {"name": "glm-5.2", "context_window": 1000000, "supports_vision": False},
            {"name": "doubao-seed-evolving", "context_window": 1000000, "supports_vision": True},
            {"name": "ark-code-latest", "context_window": 256000, "supports_vision": True},
        ],
    },
    {
        "id": "volcengine-coding-plan",
        "name": "火山CodingPlan",
        "base_url": "https://ark.cn-beijing.volces.com/api/coding",
        "protocol": "anthropic",
        "models": [
            {"name": "doubao-seed-2.0-code", "context_window": 256000, "supports_vision": True},
            {"name": "glm-5.2", "context_window": 1000000, "supports_vision": False},
            {"name": "ark-code-latest", "context_window": 256000, "supports_vision": True},
        ],
    },
    {
        "id": "stepfun",
        "name": "阶跃星辰",
        "base_url": "https://api.stepfun.com/step_plan/v1",
        "protocol": "openai",
        "models": [
            {"name": "step-3.7-flash", "context_window": 256000, "supports_vision": True},
            {"name": "step-router-v1", "context_window": 256000, "supports_vision": False},
        ],
    },
    {
        "id": "stepfun-anthropic",
        "name": "阶跃星辰（Anthropic格式）",
        "base_url": "https://api.stepfun.com/step_plan",
        "protocol": "anthropic",
        "models": [
            {"name": "step-3.7-flash", "context_window": 256000, "supports_vision": True},
            {"name": "step-router-v1", "context_window": 256000, "supports_vision": False},
            {"name": "step-explore", "context_window": 1000000, "supports_vision": False},
        ],
    },
    {
        "id": "siliconflow",
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "protocol": "openai",
        "models": [
            {"name": "zai-org/GLM-5.2", "context_window": 1000000, "supports_vision": False},
        ],
    },
    {
        "id": "minimax",
        "name": "minimax",
        "base_url": "https://api.minimaxi.com/v1",
        "protocol": "openai",
        "models": [
            {"name": "MiniMax-M3", "context_window": 1000000, "supports_vision": True},
            {"name": "MiniMax-M2.7", "context_window": 204800, "supports_vision": True},
        ],
    },
    {
        "id": "zhipu",
        "name": "智谱API",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "protocol": "openai",
        "models": [
            {"name": "glm-5.3", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.3-flash", "context_window": 1000000, "supports_vision": True},
            {"name": "glm-5.2", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.1", "context_window": 1000000, "supports_vision": False},
        ],
    },
    {
        "id": "zhipu-coding-openai",
        "name": "智谱CodingPlan(OpenAI)",
        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
        "protocol": "openai",
        "models": [
            {"name": "glm-5.3", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.3-flash", "context_window": 1000000, "supports_vision": True},
            {"name": "glm-5.2", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.1", "context_window": 1000000, "supports_vision": False},
        ],
    },
    {
        "id": "zhipu-coding-anthropic",
        "name": "智谱CodingPlan(Anthropic)",
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "protocol": "anthropic",
        "models": [
            {"name": "glm-5.3", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.3-flash", "context_window": 1000000, "supports_vision": True},
            {"name": "glm-5.2", "context_window": 1000000, "supports_vision": False},
            {"name": "glm-5.1", "context_window": 1000000, "supports_vision": False},
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
