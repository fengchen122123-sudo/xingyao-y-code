"""按模型名维护思考模式能力，不把供应商当作能力来源。"""

from dataclasses import dataclass


REASONING_LABELS = {
    "default": "默认",
    "off": "关闭",
    "on": "开启",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "超高",
    "max": "最高",
}


@dataclass(frozen=True)
class ReasoningCapability:
    kind: str
    options: tuple[str, ...]
    request_strategy: str
    disabled_reason: str = ""


HIDDEN_REASONING_CAPABILITY = ReasoningCapability("hidden", (), "hidden")
REASONING_CAPABILITIES: dict[str, ReasoningCapability] = {}


def _register(names: tuple[str, ...], capability: ReasoningCapability) -> None:
    for name in names:
        REASONING_CAPABILITIES[name.lower()] = capability


_register(
    ("k3", "k3-256k", "kimi-k3", "kimi-k3-256k"),
    ReasoningCapability(
        "effort", ("default", "low", "high", "max"), "always_on_effort"
    ),
)
_register(
    (
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
        "deepseek-v4.1-flash",
    ),
    ReasoningCapability(
        "effort",
        ("default", "off", "low", "high", "max"),
        "thinking_object_effort",
    ),
)
_register(
    ("deepseek-v4-pro",),
    ReasoningCapability(
        "effort", ("default", "off", "high", "max"), "thinking_object_effort"
    ),
)
_register(
    ("deepseek-v4-pro-0813",),
    ReasoningCapability(
        "effort",
        ("default", "off", "low", "high", "max"),
        "thinking_object_effort",
    ),
)
_register(
    ("qwen3.8-max",),
    ReasoningCapability(
        "effort",
        ("default", "off", "low", "medium", "xhigh"),
        "enable_thinking_effort",
    ),
)
# qwen38-27b 是 qwen3.8-27b 的同模型别名，两者档位一致。
_register(
    ("qwen3.8-27b", "qwen38-27b"),
    ReasoningCapability(
        "effort",
        ("default", "off", "low", "medium", "xhigh"),
        "enable_thinking_effort",
    ),
)
# qwen38-flash / qwen3.8-falsh 是同模型别名及拼写容错，档位与 qwen3.8-max 一致。
_register(
    ("qwen3.8-flash", "qwen38-flash", "qwen3.8-falsh", "qwen38-falsh"),
    ReasoningCapability(
        "effort",
        ("default", "off", "low", "medium", "xhigh"),
        "enable_thinking_effort",
    ),
)
_register(
    ("glm-5.2",),
    ReasoningCapability(
        "effort", ("default", "off", "high", "max"), "thinking_object_effort"
    ),
)
# GLM-5.3/5.3-flash 强制思考：不支持关闭（thinking.type 传 disabled 会报错），
# reasoning_effort 仅支持 low/high/max。
_register(
    ("glm-5.3", "glm-5.3-flash"),
    ReasoningCapability(
        "effort", ("default", "low", "high", "max"), "thinking_object_effort"
    ),
)
_register(
    ("seed-2.1-turbo", "seed-2.1-pro", "doubao-seed-2.0-code"),
    ReasoningCapability(
        "effort",
        ("default", "off", "low", "medium", "high"),
        "thinking_object_effort",
    ),
)
_register(
    ("mimo-v2.5-pro",),
    ReasoningCapability(
        "effort", ("default", "off", "high"), "effort_switch"
    ),
)
# 商汤 sensenova-6.8-flash-lite：官方文档 reasoning_effort 取值 low/medium/high/none
#（none=关闭思考），缺省默认 medium，与 mimo 同为 effort_switch 策略。
_register(
    ("sensenova-6.8-flash-lite",),
    ReasoningCapability(
        "effort",
        ("default", "off", "low", "medium", "high"),
        "effort_switch",
    ),
)
_register(
    (
        "kimi-k2.6",
        "kimi-k2.5",
        "glm-5",
        "glm-5.1",
        "minimax-m3",
        "longcat-2.0",
    ),
    ReasoningCapability(
        "toggle", ("default", "off", "on"), "thinking_object_toggle"
    ),
)
_register(
    ("qwen3.7-max",),
    ReasoningCapability(
        "toggle", ("default", "off", "on"), "enable_thinking_toggle"
    ),
)
# kimi-for-coding 现指向 K2.8 Preview：与 K3 一致支持 reasoning_effort 的 low/high/max
# 三档（默认 max），思考不可关闭（无 off/on 档）。
_register(
    ("kimi-for-coding",),
    ReasoningCapability(
        "effort", ("default", "low", "high", "max"), "always_on_effort"
    ),
)
_register(
    ("kimi-k2.7-code", "minimax-m2.5", "minimax-m2.7"),
    ReasoningCapability("fixed", (), "always_on", "该模型始终开启思考"),
)
# neohorse-1-9b（tokenrhythm.studio）：实测为带 reasoning_content 的推理模型，
# 推理与正文共用 max_tokens 预算，接口未提供关闭/调档参数，故按"始终开启思考"
# 登记（不注入任何思考参数，GUI 侧只读展示）。
_register(
    ("neohorse-1-9b",),
    ReasoningCapability("fixed", (), "always_on", "该模型始终开启思考"),
)
# 阶跃星辰：推理模型始终输出 reasoning，用 reasoning_effort 控制深度（low/medium/high），无 off 开关。
_register(
    ("step-3.7-flash", "step-router-v1"),
    ReasoningCapability(
        "effort", ("default", "low", "medium", "high"), "always_on_effort"
    ),
)
# 腾讯混元（hy3，经 CloudBase AI 网关 OpenAI 兼容接口）：顶层 enable_thinking 开关，无强度档位。
_register(
    ("hy3",),
    ReasoningCapability(
        "toggle", ("default", "off", "on"), "enable_thinking_toggle"
    ),
)
_register(
    (
        "sensenova-6.7-flash-lite",
        "step-explore",
        "doubao-seed-evolving",
        "ark-code-latest",
    ),
    HIDDEN_REASONING_CAPABILITY,
)


# DeepSeek 官方思考模式（https://api-docs.deepseek.com/zh-cn/guides/thinking_mode）：
# thinking.type 控制开关，reasoning_effort 控制强度（low/high/max），默认开启、
# effort 默认 high。任何厂商中以 deepseek 开头（不区分大小写）的模型统一按此
# 适配，无需逐个登记；能力表中的显式登记仅用于别名折叠等特例。
_DEEPSEEK_PREFIX_CAPABILITY = ReasoningCapability(
    "effort", ("default", "off", "low", "high", "max"), "thinking_object_effort"
)

# Agnes AI：OpenAI 兼容接口用 chat_template_kwargs.enable_thinking 开关思考，
# 无强度档位。任何 agnes 前缀模型（不区分大小写，含未来新版本）统一按此
# 适配，无需逐个登记。
_AGNES_PREFIX_CAPABILITY = ReasoningCapability(
    "toggle", ("default", "off", "on"), "agnes_enable_thinking"
)


def _model_leaf_name(normalized: str) -> str:
    """取传输前缀或网关 URL 之后的裸模型名。"""
    scheme, separator, suffix = normalized.partition("://")
    name = suffix if separator else normalized
    return name.split("/")[-1]


def normalize_reasoning_model_name(model_name: str) -> str:
    """移除传输协议前缀并折叠已确认的同模型别名。"""
    normalized = str(model_name or "").strip().lower()
    prefix, separator, suffix = normalized.partition("/")
    if separator and prefix in {"openai", "anthropic"}:
        normalized = suffix
    if normalized == "zai-org/glm-5.2":
        return "glm-5.2"
    if normalized in {"qwen3.8-falsh", "qwen38-falsh", "qwen38-flash"}:
        return "qwen3.8-flash"
    return normalized


def get_reasoning_capability(model_name: str) -> ReasoningCapability:
    normalized = normalize_reasoning_model_name(model_name)
    registered = REASONING_CAPABILITIES.get(normalized)
    if registered is not None:
        return registered
    if _model_leaf_name(normalized).startswith("deepseek"):
        return _DEEPSEEK_PREFIX_CAPABILITY
    if _model_leaf_name(normalized).startswith("agnes"):
        return _AGNES_PREFIX_CAPABILITY
    return HIDDEN_REASONING_CAPABILITY


def build_reasoning_snapshot(model: dict | None, choice: str = "default") -> dict:
    """生成可直接交给 GUI 的不可猜测能力快照。"""
    model_data = model or {}
    model_name = str(model_data.get("litellm_model") or model_data.get("name") or "")
    capability = get_reasoning_capability(model_name)
    fixed = capability.kind == "fixed"
    return {
        "kind": capability.kind,
        "visible": capability.kind != "hidden",
        "disabled": fixed,
        "disabled_reason": capability.disabled_reason,
        "choice": "on" if fixed else choice,
        "options": [
            {"value": value, "label": REASONING_LABELS[value]}
            for value in capability.options
        ],
    }
