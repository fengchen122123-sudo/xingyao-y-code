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
    ("k3", "k3-256k", "kimi-k3"),
    ReasoningCapability(
        "effort", ("default", "low", "high", "max"), "always_on_effort"
    ),
)
_register(
    ("deepseek-v4-flash", "deepseek-v4-flash-0731"),
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
_register(
    ("glm-5.2",),
    ReasoningCapability(
        "effort", ("default", "off", "high", "max"), "thinking_object_effort"
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
_register(
    ("kimi-for-coding", "kimi-k2.7-code", "minimax-m2.5", "minimax-m2.7"),
    ReasoningCapability("fixed", (), "always_on", "该模型始终开启思考"),
)
_register(
    (
        "deepseek-v4-flash-vision-exp",
        "agnes-2.0-flash",
        "agnes-2.5-flash",
        "sensenova-6.7-flash-lite",
        "step-3.7-flash",
        "step-explore",
        "step-router-v1",
        "doubao-seed-evolving",
        "ark-code-latest",
    ),
    HIDDEN_REASONING_CAPABILITY,
)


def normalize_reasoning_model_name(model_name: str) -> str:
    """移除传输协议前缀并折叠已确认的同模型别名。"""
    normalized = str(model_name or "").strip().lower()
    prefix, separator, suffix = normalized.partition("/")
    if separator and prefix in {"openai", "anthropic"}:
        normalized = suffix
    if normalized == "zai-org/glm-5.2":
        return "glm-5.2"
    return normalized


def get_reasoning_capability(model_name: str) -> ReasoningCapability:
    return REASONING_CAPABILITIES.get(
        normalize_reasoning_model_name(model_name),
        HIDDEN_REASONING_CAPABILITY,
    )


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
