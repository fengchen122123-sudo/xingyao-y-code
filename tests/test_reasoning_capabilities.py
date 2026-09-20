from core.reasoning_capabilities import (
    build_reasoning_snapshot,
    get_reasoning_capability,
    normalize_reasoning_model_name,
)


def test_transport_prefix_case_and_glm_alias_resolve_to_same_model_capability():
    """传输前缀、大小写或已知命名空间不能改变同一模型的能力。"""
    assert normalize_reasoning_model_name("openai/K3-256K") == "k3-256k"
    assert normalize_reasoning_model_name("anthropic/kimi-for-coding") == "kimi-for-coding"
    assert normalize_reasoning_model_name("zai-org/GLM-5.2") == "glm-5.2"
    assert normalize_reasoning_model_name("openai/qwen3.8-falsh") == "qwen3.8-flash"
    assert normalize_reasoning_model_name("qwen38-flash") == "qwen3.8-flash"


def test_deepseek_prefixed_names_resolve_to_official_thinking_capability():
    """任意厂商的 deepseek 前缀模型（不区分大小写）都按官方思考模式文档适配。

    依据 https://api-docs.deepseek.com/zh-cn/guides/thinking_mode：
    thinking.type 控制开关、reasoning_effort 控制强度，档位 默认/关闭/低/高/最高。
    """
    for name in (
        "deepseek-flash",
        "openai/DEEPSEEK-Flash",
        "DeepSeek-v9-future-model",
        "anthropic/deepseek-r3-agent",
        "https://gateway.example/v1/deepseek-custom",
    ):
        capability = get_reasoning_capability(name)
        assert capability.kind == "effort", name
        assert capability.options == ("default", "off", "low", "high", "max"), name
        assert capability.request_strategy == "thinking_object_effort", name


def test_glm53_variant_names_inherit_family_capability():
    """glm-5.3 家族未登记变体（如网关挂载名 flashx）自动继承强制思考能力。

    能力表精确登记了 glm-5.3 / glm-5.3-flash / glm-5.3-free；其余以
    glm-5.3 开头的变体按家族能力适配：low/high/max 三档，不可关闭。
    """
    for name in (
        "glm-5.3-flashx",
        "openai/glm-5.3-flashx",
        "GLM-5.3-Turbo",
        "https://gateway.example/v1/glm-5.3-custom",
    ):
        capability = get_reasoning_capability(name)
        assert capability.kind == "effort", name
        assert capability.options == ("default", "low", "high", "max"), name
        assert capability.request_strategy == "thinking_object_effort", name


def test_glm53_prefix_rule_does_not_spill_to_other_glm_generations():
    """前缀规则只作用于 glm-5.3 家族，其他代际未知变体仍按未适配处理。"""
    for name in ("glm-5.4", "glm-5.2-plus", "my-glm-5.3-clone"):
        assert get_reasoning_capability(name).kind == "hidden", name


def test_non_deepseek_unknown_models_stay_hidden():
    """deepseek 前缀规则不得外溢：其他未登记模型仍按未适配处理。"""
    for name in (
        "some-future-model",
        "my-deepseek-clone",
        "my-agnes-clone",
        "qwen3.9-max",
    ):
        assert get_reasoning_capability(name).kind == "hidden", name


def test_agnes_prefixed_names_resolve_to_toggle_capability():
    """任何 agnes 前缀模型（不区分大小写，含未来新版本）统一按
    chat_template_kwargs.enable_thinking 开关适配，无需逐个登记。"""
    for name in (
        "agnes-3.0-flash",
        "openai/AGNES-4.0-future",
        "agnes-9",
        "https://gateway.example/v1/agnes-custom",
    ):
        capability = get_reasoning_capability(name)
        assert capability.kind == "toggle", name
        assert capability.options == ("default", "off", "on"), name
        assert capability.request_strategy == "agnes_enable_thinking", name


def test_each_adjustable_model_exposes_only_its_real_reasoning_choices():
    """模型切换不能继续显示旧的通用 low/high/max 档位。"""
    expected = {
        "k3": ("default", "low", "high", "max"),
        "k3-256k": ("default", "low", "high", "max"),
        "kimi-k3": ("default", "low", "high", "max"),
        "kimi-for-coding": ("default", "low", "high", "max"),
        "deepseek-flash": ("default", "off", "low", "high", "max"),
        "deepseek-v4-flash-0731": ("default", "off", "low", "high", "max"),
        "deepseek-v4.1-flash": ("default", "off", "low", "high", "max"),
        "deepseek-v4-pro": ("default", "off", "high", "max"),
        "deepseek-v4-pro-0813": ("default", "off", "low", "high", "max"),
        "qwen3.8-max": ("default", "off", "low", "medium", "xhigh"),
        "qwen3.8-flash": ("default", "off", "low", "medium", "xhigh"),
        "qwen38-flash": ("default", "off", "low", "medium", "xhigh"),
        "qwen3.8-falsh": ("default", "off", "low", "medium", "xhigh"),
        "qwen3.8-27b": ("default", "off", "low", "medium", "xhigh"),
        "qwen38-27b": ("default", "off", "low", "medium", "xhigh"),
        "glm-5.2": ("default", "off", "high", "max"),
        "zai-org/GLM-5.2": ("default", "off", "high", "max"),
        "seed-2.1-turbo": ("default", "off", "low", "medium", "high"),
        "seed-2.1-pro": ("default", "off", "low", "medium", "high"),
        "doubao-seed-2.0-code": ("default", "off", "low", "medium", "high"),
        "mimo-v2.5-pro": ("default", "off", "high"),
        "sensenova-6.8-flash-lite": ("default", "off", "low", "medium", "high"),
        "kimi-k2.6": ("default", "off", "on"),
        "kimi-k2.5": ("default", "off", "on"),
        "qwen3.7-max": ("default", "off", "on"),
        "glm-5": ("default", "off", "on"),
        "glm-5.1": ("default", "off", "on"),
        "MiniMax-M3": ("default", "off", "on"),
        "LongCat-2.0": ("default", "off", "on"),
        "step-3.7-flash": ("default", "low", "medium", "high"),
        "step-router-v1": ("default", "low", "medium", "high"),
        "agnes-2.5-flash": ("default", "off", "on"),
        "agnes-3.0-flash": ("default", "off", "on"),
        "hy3": ("default", "off", "on"),
    }

    for model_name, options in expected.items():
        assert get_reasoning_capability(model_name).options == options


def test_fixed_models_are_visible_locked_on_and_unknown_models_are_hidden():
    """固定思考必须显示为禁用的开启；未知或动态模型不能获得猜测档位。"""
    for model_name in (
        "kimi-k2.7-code",
        "minimax-m2.5",
        "minimax-m2.7",
    ):
        fixed = build_reasoning_snapshot({"name": model_name})
        assert fixed == {
            "kind": "fixed",
            "visible": True,
            "disabled": True,
            "disabled_reason": "该模型始终开启思考",
            "choice": "on",
            "options": [],
        }

    for model_name in (
        "sensenova-6.7-flash-lite",
        "step-explore",
        "doubao-seed-evolving",
        "ark-code-latest",
        "some-future-model",
    ):
        hidden = build_reasoning_snapshot({"name": model_name})
        assert hidden["kind"] == "hidden"
        assert hidden["visible"] is False
        assert hidden["options"] == []


def test_snapshot_serializes_labels_in_capability_order():
    """前端菜单必须收到后端定义的精确顺序和中文标签。"""
    snapshot = build_reasoning_snapshot({"name": "qwen3.8-max"}, "medium")
    assert snapshot["choice"] == "medium"
    assert snapshot["options"] == [
        {"value": "default", "label": "默认"},
        {"value": "off", "label": "关闭"},
        {"value": "low", "label": "低"},
        {"value": "medium", "label": "中"},
        {"value": "xhigh", "label": "超高"},
    ]
