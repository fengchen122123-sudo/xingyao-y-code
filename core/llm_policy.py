"""声明式模型方言与提供商能力策略体系。

将散落在系统各处的厂商特例（思维链字段、缓存亲和机制、特定参数允许度、特殊请求头）
收拢为不可变的数据类策略（ModelPolicy），消除基于模型名硬编码的条件分叉。
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any


@dataclass(frozen=True)
class ModelPolicy:
    """定义单一模型或厂商方言的行为边界与能力约束。"""

    provider_kind: str
    """方言标识，例如 deepseek, moonshot, zhipu, openai, openai_reasoning, standard"""

    reasoning_field: str = "reasoning_content"
    """思维链提取字段名称 (reasoning_content / reasoning / thought)"""

    supports_temperature: bool = True
    """是否支持传递 temperature 参数；思考模型 (如 o1, o3-mini) 需为 False"""

    supports_stream_options: bool = True
    """是否支持 stream_options: {"include_usage": True} 以便在流式中返回使用量统计"""

    default_headers: Dict[str, str] = field(default_factory=dict)
    """该方言默认需要注入的 HTTP 请求头"""

    default_extra_body: Dict[str, Any] = field(default_factory=dict)
    """该方言默认需要补充的请求体扩展字段"""


# 预置方言策略注册表
POLICY_REGISTRY: Dict[str, ModelPolicy] = {
    # DeepSeek 官方
    "deepseek": ModelPolicy(
        provider_kind="deepseek",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={},
        default_extra_body={},
    ),
    # 月之暗面 Kimi
    "moonshot": ModelPolicy(
        provider_kind="moonshot",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={},
        default_extra_body={},
    ),
    # 智谱 GLM
    "zhipu": ModelPolicy(
        provider_kind="zhipu",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={},
        default_extra_body={},
    ),
    # OpenAI 思考模型 (o1, o3-mini)：native 引擎不接手（需 max_completion_tokens /
    # developer 角色改写，见 llm_transport._is_native_supported_model），该策略
    # 仅作为识别标记与 litellm 路径的方言描述。
    "openai_reasoning": ModelPolicy(
        provider_kind="openai_reasoning",
        reasoning_field="reasoning_content",
        supports_temperature=False,  # 不允许自定义 temperature
        supports_stream_options=True,
        default_headers={},
        default_extra_body={},
    ),
    # 通义千问 (DashScope)
    "dashscope": ModelPolicy(
        provider_kind="dashscope",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={},
        default_extra_body={},
    ),
    # MiniMax
    "minimax": ModelPolicy(
        provider_kind="minimax",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={},
        default_extra_body={},
    ),
    # 标准 OpenAI 兼容兜底 (兼容 OneAPI, NewAPI, vLLM, Ollama 等)
    "standard": ModelPolicy(
        provider_kind="standard",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={},
        default_extra_body={},
    ),
}


def get_model_policy(
    model_name: Optional[str] = None,
    api_base: Optional[str] = None,
    provider_id: Optional[str] = None,
) -> ModelPolicy:
    """根据给定的模型名、Base URL 或明确的 provider_id 获取对应的方言策略。

    优先按显式 provider_id 匹配，其次按模型名前缀与 Base URL 启发式识别，兜底返回 standard 策略。
    """
    if provider_id and provider_id in POLICY_REGISTRY:
        return POLICY_REGISTRY[provider_id]

    m_lower = (model_name or "").lower()
    base_lower = (api_base or "").lower()

    # 1. DeepSeek
    if (
        "deepseek" in m_lower
        or "api.deepseek.com" in base_lower
    ):
        return POLICY_REGISTRY["deepseek"]

    # 2. Moonshot / Kimi
    if (
        "moonshot" in m_lower
        or "kimi" in m_lower
        or "api.moonshot.cn" in base_lower
    ):
        return POLICY_REGISTRY["moonshot"]

    # 3. 智谱 GLM
    if (
        "glm" in m_lower
        or "zhipu" in m_lower
        or "bigmodel.cn" in base_lower
    ):
        return POLICY_REGISTRY["zhipu"]

    # 4. OpenAI 思考模型 (o1, o3, etc.)
    if (
        m_lower.startswith("o1")
        or m_lower.startswith("o3")
        or "/o1" in m_lower
        or "/o3" in m_lower
    ):
        return POLICY_REGISTRY["openai_reasoning"]

    # 5. 通义千问 / DashScope
    if (
        "qwen" in m_lower
        or "dashscope" in m_lower
        or "dashscope.aliyuncs.com" in base_lower
    ):
        return POLICY_REGISTRY["dashscope"]

    # 6. MiniMax
    if (
        "minimax" in m_lower
        or "abab" in m_lower
        or "api.minimax.chat" in base_lower
    ):
        return POLICY_REGISTRY["minimax"]

    # 兜底：标准 OpenAI 兼容
    return POLICY_REGISTRY["standard"]
