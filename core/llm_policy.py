from dataclasses import dataclass, field
import re
from typing import Optional, Dict, Any
from urllib.parse import quote

from core.version import __version__ as YCODE_VERSION


DEFAULT_USER_AGENT = f"XingYaoCode/{YCODE_VERSION}"

TOKENRHYTHM_DOMAIN = "tokenrhythm.studio"
TOKENRHYTHM_AGENT_HEADER = "X-TokenRhythm-Agent"
TOKENRHYTHM_AGENT_NAME = "XingYaoCode"


def tokenrhythm_agent_headers(url: Optional[str] = None) -> Dict[str, str]:
    if TOKENRHYTHM_DOMAIN in str(url or "").lower():
        return {TOKENRHYTHM_AGENT_HEADER: TOKENRHYTHM_AGENT_NAME}
    return {}


# 会话关联头：出站请求统一附带当前执行会话 ID，把本地会话与服务端调用日志
# 互相关联便于排查问题。值统一 percent-encode——HTTP 头值必须为 ASCII，
# 中文会话名（自动标题 / 用户改名）编码后可由服务端 URL-decode 还原。
SESSION_ID_HEADER = "X-Session-Id"


def session_id_headers() -> Dict[str, str]:
    # 延迟导入 app_context，避免传输层底端模块连带引入运行时上下文依赖。
    from core import app_context

    name = str(app_context.get_thread_session() or "")
    if not name:
        return {}
    return {SESSION_ID_HEADER: quote(name, safe="")}


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


POLICY_REGISTRY: Dict[str, ModelPolicy] = {
    "deepseek": ModelPolicy(
        provider_kind="deepseek",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={"User-Agent": DEFAULT_USER_AGENT},
        default_extra_body={},
    ),
    "moonshot": ModelPolicy(
        provider_kind="moonshot",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={"User-Agent": DEFAULT_USER_AGENT},
        default_extra_body={},
    ),
    "zhipu": ModelPolicy(
        provider_kind="zhipu",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={"User-Agent": DEFAULT_USER_AGENT},
        default_extra_body={},
    ),
    "openai_reasoning": ModelPolicy(
        provider_kind="openai_reasoning",
        reasoning_field="reasoning_content",
        supports_temperature=False,
        supports_stream_options=True,
        default_headers={"User-Agent": DEFAULT_USER_AGENT},
        default_extra_body={},
    ),
    "dashscope": ModelPolicy(
        provider_kind="dashscope",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={"User-Agent": DEFAULT_USER_AGENT},
        default_extra_body={},
    ),
    "minimax": ModelPolicy(
        provider_kind="minimax",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={"User-Agent": DEFAULT_USER_AGENT},
        default_extra_body={},
    ),
    # 标准 OpenAI 兼容兜底 (兼容 OneAPI, NewAPI, vLLM, Ollama 等)
    "standard": ModelPolicy(
        provider_kind="standard",
        reasoning_field="reasoning_content",
        supports_temperature=True,
        supports_stream_options=True,
        default_headers={"User-Agent": DEFAULT_USER_AGENT},
        default_extra_body={},
    ),
}


_KIMI_POLICY_NAME_RE = re.compile(r"(?:^|[^a-z0-9])k(?:2|3)(?:[._-]\d+)*(?=$|[^a-z0-9])")


def get_model_policy(
    model_name: Optional[str] = None,
    api_base: Optional[str] = None,
    provider_id: Optional[str] = None,
) -> ModelPolicy:
    """根据给定的模型名、Base URL 或明确的 provider_id 获取对应的方言策略。

    优先按显式 provider_id 匹配，其次按模型名子串与 Base URL 启发式识别，兜底返回 standard 策略。
    """
    if provider_id and provider_id in POLICY_REGISTRY:
        return POLICY_REGISTRY[provider_id]

    m_lower = (model_name or "").lower()
    base_lower = (api_base or "").lower()

    if (
        "deepseek" in m_lower
        or "api.deepseek.com" in base_lower
    ):
        return POLICY_REGISTRY["deepseek"]

    if (
        "moonshot" in m_lower
        or "kimi" in m_lower
        or _KIMI_POLICY_NAME_RE.search(m_lower)
        or "api.moonshot.cn" in base_lower
        or "api.moonshot.ai" in base_lower
        or "api.kimi.com" in base_lower
    ):
        return POLICY_REGISTRY["moonshot"]

    if (
        "glm" in m_lower
        or "zhipu" in m_lower
        or "bigmodel.cn" in base_lower
        or "api.z.ai" in base_lower
    ):
        return POLICY_REGISTRY["zhipu"]

    if (
        m_lower.startswith("o1")
        or m_lower.startswith("o3")
        or "/o1" in m_lower
        or "/o3" in m_lower
    ):
        return POLICY_REGISTRY["openai_reasoning"]

    if (
        "qwen" in m_lower
        or "dashscope" in m_lower
        or "dashscope.aliyuncs.com" in base_lower
        or "dashscope-intl.aliyuncs.com" in base_lower
        or "maas.aliyuncs.com" in base_lower
    ):
        return POLICY_REGISTRY["dashscope"]

    if (
        "minimax" in m_lower
        or "abab" in m_lower
        or "api.minimax.chat" in base_lower
        or "api.minimaxi.com" in base_lower
    ):
        return POLICY_REGISTRY["minimax"]

    # 兜底：标准 OpenAI 兼容
    return POLICY_REGISTRY["standard"]
