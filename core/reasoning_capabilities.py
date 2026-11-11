"""按模型名维护思考模式能力，不把供应商当作能力来源。"""

from dataclasses import dataclass


# 思考档位在界面直接显示英文原值（default/off/low/medium/high/xhigh/max）。
# 不再维护中文别名表：档位语义由各家厂商定义，中文翻译反而容易误读
# （例如 DeepSeek 的 xhigh 实际映射到 high，直呼原名更不容易想当然）。


@dataclass(frozen=True)
class ReasoningCapability:
    kind: str
    options: tuple[str, ...]
    request_strategy: str
    disabled_reason: str = ""
    # 官方默认档位：模型在不做任何覆盖时自己使用的档位，仅用于 GUI 打开弹窗时的
    # 自动预选。没有官方依据的模型保持空串（不预选），不猜测。
    default_choice: str = ""


# 已确认该模型不支持思考（显式登记用）：GUI 侧隐藏思考按钮。
# 注意它只用于"精确事实"的隐藏，不能当作"未登记"的兜底——那会让厂商新增
# 或网关自定义名的模型凭空失去可操作的思考开关。
HIDDEN_REASONING_CAPABILITY = ReasoningCapability("hidden", (), "hidden")

# 未登记进能力表的模型（厂商新增、网关自定义名）：没有可依据的厂商事实，
# 给出通用强度能力（默认/关闭/低/中/高/极高/最高），显示全部档位供用户
# 自行选择是否注入 reasoning_effort。不提供 "on"：effort_switch 策略下
# "开启但不定档"没有可靠表达，宁可不给这个档位，也不发一个会被网关拒绝的值。
# 编码策略选 effort_switch（只注入顶层 reasoning_effort）而不是厂商私有的
# thinking 对象：未登记模型多为网关自定义名，OpenAI 标准字段被接受的概率更高。
# 与 HIDDEN_REASONING_CAPABILITY 的区别：hidden 是"已确认该模型不支持思考"的
# 精确事实（显式登记），本兜底只是"尚未登记"，GUI 必须显示可操作的选项。
UNKNOWN_REASONING_CAPABILITY = ReasoningCapability(
    "effort",
    ("default", "off", "low", "medium", "high", "xhigh", "max"),
    "effort_switch",
)
REASONING_CAPABILITIES: dict[str, ReasoningCapability] = {}


def _register(names: tuple[str, ...], capability: ReasoningCapability) -> None:
    for name in names:
        REASONING_CAPABILITIES[name.lower()] = capability



_register(
    ("seed-2.1-turbo", "seed-2.1-pro", "doubao-seed-2.0-code"),
    ReasoningCapability(
        "effort",
        ("default", "off", "low", "medium", "high"),
        "thinking_object_effort",
    ),
)
# 小米 MiMo（mimo.mi.com 官方 Deep Thinking 文档，2026-09-22 V2.6 系列发布）：
# 深度思考只有 thinking.type enabled/disabled 开关、默认开启，官方未提供强度档位；
# OpenAI 兼容路由经 extra_body 透传 thinking 对象。v2.5 / v2.5-pro 将于
# 2026-10-21 下线，下线前与 V2.6 系列同为开关式思考。
_register(
    (
        "mimo-v2.6-pro",
        "mimo-v2.6-flash",
        "mimo-v2.6-pro-ultraspeed",
        "mimo-v2.5",
        "mimo-v2.5-pro",
    ),
    ReasoningCapability(
        "toggle", ("default", "off", "on"), "thinking_object_toggle",
        default_choice="on",
    ),
)
# 商汤 sensenova-6.8-flash-lite：官方文档 reasoning_effort 取值 low/medium/high/none
#（none=关闭思考），缺省默认 medium。
_register(
    ("sensenova-6.8-flash-lite",),
    ReasoningCapability(
        "effort",
        ("default", "off", "low", "medium", "high"),
        "effort_switch",
        default_choice="medium",
    ),
)
_register(
    ("minimax-m3", "longcat-2.0"),
    ReasoningCapability(
        "toggle", ("default", "off", "on"), "thinking_object_toggle"
    ),
)
_register(
    ("minimax-m2.5", "minimax-m2.7"),
    ReasoningCapability("fixed", (), "always_on", "该模型始终开启思考"),
)
# neohorse-1-9b（tokenrhythm.studio）：实测为带 reasoning_content 的推理模型，
# 推理与正文共用 max_tokens 预算，接口未提供关闭/调档参数，故按"始终开启思考"
# 登记（不注入任何思考参数，GUI 侧只读展示）。同系 1-4b（含 LM Studio 量化名）
# 按同系列推断登记为同款。
_register(
    ("neohorse-1-9b", "neohorse-1-4b", "neohorse-1-4b@q4_k_m"),
    ReasoningCapability("fixed", (), "always_on", "该模型始终开启思考"),
)
# 中转站 GPT 系（网关自定义名）：实测接受 OpenAI 风格 reasoning_effort。
# gpt-5.6-sol 接受 "none"（可关思考）；gpt-6-astra 明确拒绝 "none"
# （HTTP 400 Unsupported value），不提供 off 档。
_register(
    ("gpt-5.6-sol",),
    ReasoningCapability(
        "effort", ("default", "off", "low", "medium", "high"), "effort_switch"
    ),
)
_register(
    ("gpt-6-astra",),
    ReasoningCapability(
        "effort", ("default", "low", "medium", "high"), "effort_switch"
    ),
)
# 阶跃星辰：推理模型始终输出 reasoning，用 reasoning_effort 控制深度（low/medium/high），无 off 开关。
# token plan 全系 LLM（step-3.7-flash / step-router-v1 / step-5-preview /
# step-3.5-flash / step-3.5-flash-2603）统一按此适配（官方 /v1/models 的
# reasoning_effort_support_list 均为 low/medium/high）。
_register(
    (
        "step-3.7-flash",
        "step-router-v1",
        "step-5-preview",
        "step-3.5-flash",
        "step-3.5-flash-2603",
    ),
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
# thinking.type 控制开关，reasoning_effort 控制强度，默认开启、effort 默认 high。
# 服务端真正区分的强度只有 low/high/max，但官方同时接受 minimal/medium/xhigh/ultra，
# 按文档映射表分别折叠为 low/high/high/max——即 xhigh 与 high 等价、ultra 与 max 等价。
# 这里保留 xhigh（官方承认的取值，界面上可选到），ultra 不单列。
# 任何厂商中以 deepseek 开头（不区分大小写）的模型统一按此适配。
_DEEPSEEK_PREFIX_CAPABILITY = ReasoningCapability(
    "effort",
    ("default", "off", "low", "high", "xhigh", "max"),
    "thinking_object_effort",
    default_choice="high",
)

# Agnes AI：OpenAI 兼容接口用 chat_template_kwargs.enable_thinking 开关思考，
# 无强度档位。任何 agnes 前缀模型（不区分大小写，含未来新版本）统一按此
# 适配，无需逐个登记。
_AGNES_PREFIX_CAPABILITY = ReasoningCapability(
    "toggle", ("default", "off", "on"), "agnes_enable_thinking"
)

# Kimi K3 官方：reasoning_effort 仅支持 low/high/max，默认 max，思考不可关闭
# （无 off/on 档）。K2.5/K2.6/K2.7-code 及 kimi-for-coding（K2.8 Preview）按
# 家族统一走同一能力；任何 kimi 前缀模型（不区分大小写，含未来新版本）统一
# 继承，无需逐个登记。裸名 k3 / k3-256k 不带 kimi 前缀，需精确登记指向
# 同一常量；kimi-for-coding 一并显式登记（见下方 _register）。
_KIMI_PREFIX_CAPABILITY = ReasoningCapability(
    "effort", ("default", "low", "high", "max"), "always_on_effort",
    default_choice="max",
)

# GLM 家族：按用户决策全系统一 low/high/max 不可关。官方 5.2 支持关闭思考，
# 但 5.3 强制思考（thinking.type 传 disabled 会报错），取家族交集：
# reasoning_effort 仅 low/high/max，默认 max。任何 glm 前缀模型（不区分
# 大小写，含未来代际）统一继承，无需逐个登记。
_GLM_PREFIX_CAPABILITY = ReasoningCapability(
    "effort", ("default", "low", "high", "max"), "thinking_object_effort",
    default_choice="max",
)

# Qwen 官方（百炼 3.8 系文档）：reasoning_effort 支持 low/medium/xhigh，默认
# xhigh，none 关闭思考（映射为 off 档）；OpenAI 兼容路由用 enable_thinking +
# reasoning_effort 编码。按用户决策 qwen 全家族统一（不限 3.8 代际）：任何
# qwen 前缀模型（含裸名 qwen、老代际 qwen3.7 与未来代际）统一继承。
_QWEN_PREFIX_CAPABILITY = ReasoningCapability(
    "effort",
    ("default", "off", "low", "medium", "xhigh"),
    "enable_thinking_effort",
    default_choice="xhigh",
)

# 裸名/别名精确登记：不带家族前缀的名字（k3 / k3-256k / kimi-for-coding）
# 与 qwen38 缩写、拼写容错别名，统一指向对应家族常量。
_register(("k3", "k3-256k", "kimi-for-coding"), _KIMI_PREFIX_CAPABILITY)
_register(
    (
        "qwen3.8-max",
        "qwen3.8-27b",
        "qwen38-27b",
        "qwen3.8-flash",
        "qwen38-flash",
        "qwen3.8-falsh",
        "qwen38-falsh",
    ),
    _QWEN_PREFIX_CAPABILITY,
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
    if normalized == "z-ai/glm-5.3-free":
        return "glm-5.3-free"
    if normalized in {"qwen3.8-falsh", "qwen38-falsh", "qwen38-flash"}:
        return "qwen3.8-flash"
    return normalized


def get_reasoning_capability(model_name: str) -> ReasoningCapability:
    """按模型名取思考能力：显式登记 > 家族前缀规则 > 未登记兜底。

    未登记模型不再隐藏，而是返回 UNKNOWN_REASONING_CAPABILITY（通用开关），
    只有显式登记为 HIDDEN_REASONING_CAPABILITY 的模型才隐藏。
    """
    normalized = normalize_reasoning_model_name(model_name)
    registered = REASONING_CAPABILITIES.get(normalized)
    if registered is not None:
        return registered
    leaf = _model_leaf_name(normalized)
    if leaf.startswith("deepseek"):
        return _DEEPSEEK_PREFIX_CAPABILITY
    if leaf.startswith("agnes"):
        return _AGNES_PREFIX_CAPABILITY
    if leaf.startswith("kimi"):
        return _KIMI_PREFIX_CAPABILITY
    if leaf.startswith("glm"):
        return _GLM_PREFIX_CAPABILITY
    if leaf.startswith("qwen"):
        return _QWEN_PREFIX_CAPABILITY
    # 未登记（厂商新增 / 网关自定义名）：没有可依据的厂商事实，给通用开关兜底，
    # 让 GUI 仍能显示可操作的思考开关；显式的 hidden 登记已在上面命中返回。
    return UNKNOWN_REASONING_CAPABILITY


# 档位显示标签：CLI 的 /effort 与 GUI 选择器共用同一份快照，label 就是给人看的
# 文案。必须给中文——把 value 直接当 label 会输出 "○ off(off)" 这种重复且不可读
# 的文案（历史遗留：标签表收敛时误把 label 简化成 value）。
_REASONING_LEVEL_LABELS = {
    "default": "默认",
    "off": "关闭",
    "on": "开启",
    "none": "关闭",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "极高",
    "max": "最高",
}

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
        "default_choice": capability.default_choice,
        "options": [
            {"value": value, "label": _REASONING_LEVEL_LABELS.get(value, value)}
            for value in capability.options
        ],
    }
