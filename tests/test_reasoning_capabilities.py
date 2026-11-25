from core.reasoning_capabilities import (
    build_reasoning_snapshot,
    get_reasoning_capability,
    normalize_reasoning_model_name,
)


def test_transport_prefix_case_and_glm_alias_resolve_to_same_model_capability():
    assert normalize_reasoning_model_name("openai/K3-256K") == "k3-256k"
    assert normalize_reasoning_model_name("anthropic/kimi-for-coding") == "kimi-for-coding"
    assert normalize_reasoning_model_name("zai-org/GLM-5.2") == "glm-5.2"
    assert normalize_reasoning_model_name("openai/qwen3.8-falsh") == "qwen3.8-flash"
    assert normalize_reasoning_model_name("qwen38-flash") == "qwen3.8-flash"


def test_deepseek_prefixed_names_resolve_to_official_thinking_capability():
    for name in (
        "deepseek-flash",
        "openai/DEEPSEEK-Flash",
        "DeepSeek-v9-future-model",
        "anthropic/deepseek-r3-agent",
        "https://gateway.example/v1/deepseek-custom",
    ):
        capability = get_reasoning_capability(name)
        assert capability.kind == "effort", name
        assert capability.options == (
            "default", "off", "low", "high", "xhigh", "max"
        ), name
        assert capability.request_strategy == "thinking_object_effort", name


def test_glm53_variant_names_inherit_family_capability():
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
    for name in ("glm-5.4", "glm-5.2-plus", "Pro/glm-5.3-flashx", "z-ai/glm-5.3"):
        capability = get_reasoning_capability(name)
        assert capability.kind == "effort", name
        assert capability.options == ("default", "low", "high", "max"), name
        assert capability.request_strategy == "thinking_object_effort", name


def test_substring_family_rules_match_gateway_prefixed_and_k3_models():
    for name in (
        "global:deepseek-v4.1-flash",
        "Pro/deepseek-ai/DeepSeek-V4",
        "aws-deepseek-r1",
    ):
        cap = get_reasoning_capability(name)
        assert cap.options == ("default", "off", "low", "high", "xhigh", "max"), name
        assert cap.request_strategy == "thinking_object_effort", name

    for name in ("k3", "k3.1", "k3.1-pro", "k3.1-pro-highspeed", "vendor-kimi-k3.1"):
        cap = get_reasoning_capability(name)
        assert cap.options == ("default", "low", "high", "max"), name
        assert cap.request_strategy == "always_on_effort", name

    for name in ("ali-qwen3.8-max", "Pro/qwen3.7-flash"):
        cap = get_reasoning_capability(name)
        assert cap.options == ("default", "off", "low", "medium", "xhigh"), name
        assert cap.request_strategy == "enable_thinking_effort", name

    for name in ("custom-agnes-3.0", "proxy/agnes-2.5-flash"):
        cap = get_reasoning_capability(name)
        assert cap.options == ("default", "off", "on"), name
        assert cap.request_strategy == "agnes_enable_thinking", name


def test_unregistered_models_fall_back_to_generic_effort():
    for name in (
        "hy4-preview",
        "openai/hy4-preview",
        "muse-spark-1.3-contributor-free",
        "nemotron-3-ultra-free",
        "mimo-v2.5-free",
        "intern-latest",
        "senseaudio-s2",
        "https://gateway.example/v1/some-custom-model",
    ):
        capability = get_reasoning_capability(name)
        assert capability.kind == "effort", name
        assert capability.options == (
            "default", "off", "low", "medium", "high", "xhigh", "max"
        ), name
        assert capability.request_strategy == "effort_switch", name


def test_explicitly_hidden_models_stay_hidden():
    for name in (
        "sensenova-6.7-flash-lite",
        "step-explore",
        "doubao-seed-evolving",
        "ark-code-latest",
    ):
        capability = get_reasoning_capability(name)
        assert capability.kind == "hidden", name
        assert capability.options == (), name
        assert capability.request_strategy == "hidden", name


def test_agnes_prefixed_names_resolve_to_toggle_capability():
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
    expected = {
        "k3": ("default", "low", "high", "max"),
        "k3-256k": ("default", "low", "high", "max"),
        "kimi-k3": ("default", "low", "high", "max"),
        "kimi-for-coding": ("default", "low", "high", "max"),
        "deepseek-flash": ("default", "off", "low", "high", "xhigh", "max"),
        "deepseek-v4-flash-0731": ("default", "off", "low", "high", "xhigh", "max"),
        "deepseek-v4.1-flash": ("default", "off", "low", "high", "xhigh", "max"),
        "deepseek-v4-pro": ("default", "off", "low", "high", "xhigh", "max"),
        "deepseek-v4-pro-0813": ("default", "off", "low", "high", "xhigh", "max"),
        "qwen3.8-max": ("default", "off", "low", "medium", "xhigh"),
        "qwen3.8-flash": ("default", "off", "low", "medium", "xhigh"),
        "qwen38-flash": ("default", "off", "low", "medium", "xhigh"),
        "qwen3.8-falsh": ("default", "off", "low", "medium", "xhigh"),
        "qwen3.8-27b": ("default", "off", "low", "medium", "xhigh"),
        "qwen38-27b": ("default", "off", "low", "medium", "xhigh"),
        "glm-5.2": ("default", "low", "high", "max"),
        "zai-org/GLM-5.2": ("default", "low", "high", "max"),
        "seed-2.1-turbo": ("default", "off", "low", "medium", "high"),
        "seed-2.1-pro": ("default", "off", "low", "medium", "high"),
        "doubao-seed-2.0-code": ("default", "off", "low", "medium", "high"),
        "mimo-v2.6-pro": ("default", "off", "on"),
        "mimo-v2.6-flash": ("default", "off", "on"),
        "mimo-v2.6-pro-ultraspeed": ("default", "off", "on"),
        "mimo-v2.5": ("default", "off", "on"),
        "mimo-v2.5-pro": ("default", "off", "on"),
        "sensenova-6.8-flash-lite": ("default", "off", "low", "medium", "high"),
        "kimi-k2.6": ("default", "low", "high", "max"),
        "kimi-k2.5": ("default", "low", "high", "max"),
        "qwen3.7-max": ("default", "off", "low", "medium", "xhigh"),
        "glm-5": ("default", "low", "high", "max"),
        "glm-5.1": ("default", "low", "high", "max"),
        "MiniMax-M3": ("default", "off", "on"),
        "minimax-m3.1-flash": ("default", "off", "on"),
        "LongCat-2.0": ("default", "off", "on"),
        "LongCat-2.5": ("default", "off", "on"),
        "LongCat-2.5-Preview": ("default", "off", "on"),
        "step-3.7-flash": ("default", "low", "medium", "high"),
        "step-router-v1": ("default", "low", "medium", "high"),
        "agnes-2.5-flash": ("default", "off", "on"),
        "agnes-3.0-flash": ("default", "off", "on"),
        "hy3": ("default", "off", "on"),
    }

    for model_name, options in expected.items():
        assert get_reasoning_capability(model_name).options == options


def test_fixed_models_are_visible_locked_on_and_explicit_hidden_models_are_hidden():
    for model_name in (
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
            "default_choice": "",
            "options": [],
        }

    for model_name in (
        "sensenova-6.7-flash-lite",
        "step-explore",
        "doubao-seed-evolving",
        "ark-code-latest",
    ):
        hidden = build_reasoning_snapshot({"name": model_name})
        assert hidden["kind"] == "hidden"
        assert hidden["visible"] is False
        assert hidden["options"] == []

    for model_name in ("some-future-model", "hy4-preview"):
        fallback = build_reasoning_snapshot({"name": model_name})
        assert fallback["kind"] == "effort"
        assert fallback["visible"] is True
        assert fallback["options"] == [
            {"value": "default", "label": "默认"},
            {"value": "off", "label": "关闭"},
            {"value": "low", "label": "低"},
            {"value": "medium", "label": "中"},
            {"value": "high", "label": "高"},
            {"value": "xhigh", "label": "极高"},
            {"value": "max", "label": "最高"},
        ]


def test_unregistered_model_snapshot_exposes_effort_menu():
    snapshot = build_reasoning_snapshot({"name": "hy4-preview"})
    assert snapshot == {
        "kind": "effort",
        "visible": True,
        "disabled": False,
        "disabled_reason": "",
        "choice": "default",
        "default_choice": "",
        "options": [
            {"value": "default", "label": "默认"},
            {"value": "off", "label": "关闭"},
            {"value": "low", "label": "低"},
            {"value": "medium", "label": "中"},
            {"value": "high", "label": "高"},
            {"value": "xhigh", "label": "极高"},
            {"value": "max", "label": "最高"},
        ],
    }
    assert build_reasoning_snapshot({"name": "hy4-preview"}, "high")["choice"] == "high"


def test_snapshot_serializes_labels_in_capability_order():
    snapshot = build_reasoning_snapshot({"name": "qwen3.8-max"}, "medium")
    assert snapshot["choice"] == "medium"
    assert snapshot["options"] == [
        {"value": "default", "label": "默认"},
        {"value": "off", "label": "关闭"},
        {"value": "low", "label": "低"},
        {"value": "medium", "label": "中"},
        {"value": "xhigh", "label": "极高"},
    ]


def test_builtin_default_choice_only_for_models_with_official_facts():
    assert get_reasoning_capability("deepseek-v4-pro").default_choice == "high"
    assert get_reasoning_capability("deepseek-flash").default_choice == "high"
    assert get_reasoning_capability("openai/DeepSeek-v9-future").default_choice == "high"
    assert get_reasoning_capability("kimi-for-coding").default_choice == "max"
    assert get_reasoning_capability("kimi-k2.6").default_choice == "max"
    assert get_reasoning_capability("kimi-k3").default_choice == "max"
    assert get_reasoning_capability("k3").default_choice == "max"
    assert get_reasoning_capability("glm-5").default_choice == "max"
    assert get_reasoning_capability("glm-5.2").default_choice == "max"
    assert get_reasoning_capability("glm-5.3").default_choice == "max"
    assert get_reasoning_capability("qwen3.8-max").default_choice == "xhigh"
    assert get_reasoning_capability("qwen3.7-max").default_choice == "xhigh"
    assert get_reasoning_capability("sensenova-6.8-flash-lite").default_choice == "medium"
    assert get_reasoning_capability("mimo-v2.6-pro").default_choice == "on"

    for name in (
        "seed-2.1-turbo",
        "step-3.7-flash",
        "hy4-preview",
    ):
        assert get_reasoning_capability(name).default_choice == "", name


def test_snapshot_carries_builtin_default_choice():
    assert build_reasoning_snapshot({"name": "deepseek-v4-pro"})["default_choice"] == "high"
    assert build_reasoning_snapshot({"name": "kimi-for-coding"})["default_choice"] == "max"
    assert build_reasoning_snapshot({"name": "k3"})["default_choice"] == "max"
    assert build_reasoning_snapshot({"name": "kimi-k2.7-code"})["default_choice"] == "max"
    assert build_reasoning_snapshot({"name": "sensenova-6.7-flash-lite"})["default_choice"] == ""


def test_kimi_prefix_family_uses_always_on_effort_with_official_default():
    for name in (
        "kimi-k2.6",
        "kimi-k2.7-code",
        "kimi-k9-future",
        "openai/KIMI-K2.5",
        "https://gateway.example/v1/kimi-custom",
    ):
        capability = get_reasoning_capability(name)
        assert capability.kind == "effort", name
        assert capability.options == ("default", "low", "high", "max"), name
        assert capability.request_strategy == "always_on_effort", name
        assert capability.default_choice == "max", name


def test_glm_prefix_family_uses_thinking_object_effort_with_official_default():
    for name in (
        "glm-5",
        "glm-5.1",
        "glm-5.2",
        "glm-5.4-future",
        "zai-org/glm-6",
    ):
        capability = get_reasoning_capability(name)
        assert capability.kind == "effort", name
        assert capability.options == ("default", "low", "high", "max"), name
        assert capability.request_strategy == "thinking_object_effort", name
        assert capability.default_choice == "max", name


def test_qwen_prefix_family_keeps_enable_thinking_effort_with_official_default():
    for name in (
        "qwen3.8-max-preview",
        "qwen3.8-omni-flash",
        "qwen3.8-2.4t-a95b",
        "qwen3.7-max",
        "qwen3.9-max",
        "qwen-max",
        "qwen-plus",
        "qwen",
        "openai/qwen",
        "openai/QWEN3.6-PLUS",
        "https://gateway.example/v1/qwen-custom",
    ):
        capability = get_reasoning_capability(name)
        assert capability.kind == "effort", name
        assert capability.options == ("default", "off", "low", "medium", "xhigh"), name
        assert capability.request_strategy == "enable_thinking_effort", name
        assert capability.default_choice == "xhigh", name


def test_bare_family_names_hit_family_rules():
    expectations = {
        "openai/deepseek": (("default", "off", "low", "high", "xhigh", "max"), "thinking_object_effort", "high"),
        "deepseek": (("default", "off", "low", "high", "xhigh", "max"), "thinking_object_effort", "high"),
        "openai/kimi": (("default", "low", "high", "max"), "always_on_effort", "max"),
        "kimi": (("default", "low", "high", "max"), "always_on_effort", "max"),
        "openai/glm": (("default", "low", "high", "max"), "thinking_object_effort", "max"),
        "anthropic/kimi": (("default", "low", "high", "max"), "always_on_effort", "max"),
    }
    for name, (options, strategy, default) in expectations.items():
        capability = get_reasoning_capability(name)
        assert capability.options == options, name
        assert capability.request_strategy == strategy, name
        assert capability.default_choice == default, name


def test_unrelated_model_names_do_not_match_family_rules():
    for name in ("some-future-model", "rk3588-local", "ark-4k-vision"):
        capability = get_reasoning_capability(name)
        assert capability.kind == "effort", name
        assert capability.options == (
            "default", "off", "low", "medium", "high", "xhigh", "max"
        ), name
        assert capability.request_strategy == "effort_switch", name
        assert capability.default_choice == "", name
