import pytest
from types import SimpleNamespace
from core.llm import (
    _command_matches_prefix,
    _escape_control_chars_in_json_strings,
    _format_session_command_rule,
    _session_prefix_for_command,
    chat_with_model,
)
import json


def test_chat_with_model_success(mocker):
    """测试大模型通信是否正常 (使用 Mock 拦截真实网络请求)"""

    delta = SimpleNamespace(
        content="你好，我是伪造的 AI 回复！",
        reasoning_content="",
        tool_calls=None,
    )
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=delta)],
        usage=None,
    )

    mocker.patch('core.llm.completion', return_value=iter([chunk]))

    reply = "".join(chat_with_model(
        model_name="gpt-4o",
        api_key="fake-key-123",
        messages=[{"role": "user", "content": "你好"}],
        stream=True,
    ))

    assert reply == "你好，我是伪造的 AI 回复！"


def test_command_prefix_requires_boundary():
    """放行 'git' 后 'gitfoo' 不得命中，'git status' 才命中。"""
    assert _command_matches_prefix("git", ("git",)) is True
    assert _command_matches_prefix("git status", ("git",)) is False
    assert _command_matches_prefix("gitfoo", ("git",)) is False
    assert _command_matches_prefix("gitfoo --bar", ("git",)) is False


def test_inline_exec_command_is_not_session_allowlisted():
    """含 -c/-e 等内联执行参数的命令不生成会话放行前缀。"""
    assert _session_prefix_for_command('python -c "print(1)"') is None
    assert _session_prefix_for_command("node -e 'console.log(1)'") is None
    assert _session_prefix_for_command("python script.py") == ("python", "script.py")
    assert _session_prefix_for_command("git status") == ("git", "status")
    assert _session_prefix_for_command("") is None


def test_inline_module_exec_flags_are_not_session_allowlisted():
    """-m / -p 等价于任意模块/代码执行，不予会话放行；但只读查询例外。"""
    assert _session_prefix_for_command("python -m pip install requests") is None
    assert _session_prefix_for_command("python -m http.server 8080") is None
    assert _session_prefix_for_command('node -p "1+1"') is None
    assert _session_prefix_for_command("perl -p -i -e 's/a/b/' file") is None
    # 只读查询（-V/--version/-h/--help）可以放行至前缀白名单
    assert _session_prefix_for_command("python -V") == ("python", "-V")
    assert _session_prefix_for_command("python --version") == ("python", "--version")
    assert _session_prefix_for_command("python -m pip --version") is None
    # 普通脚本/子命令路径仍按原规则放行
    assert _session_prefix_for_command("python script.py") == ("python", "script.py")
    assert _session_prefix_for_command("pip install requests") == ("pip", "install")


def test_stored_status_rule_cannot_approve_reset_or_shell_composition():
    stored_rules = {_session_prefix_for_command("git status")}

    assert any(_command_matches_prefix("git status --short", rule) for rule in stored_rules)
    assert not any(_command_matches_prefix("git reset --hard", rule) for rule in stored_rules)
    assert not any(_command_matches_prefix("git status; whoami", rule) for rule in stored_rules)


def test_session_command_rule_feedback_describes_actual_match_mode():
    assert _format_session_command_rule(("python", "--version")) == "精确规则：python --version"
    assert _format_session_command_rule(("git", "status")) == "子命令规则：git status（允许后续参数）"


def test_json_repair_only_escapes_raw_control_chars_inside_strings():
    """合法的 \\n 转义保持不动，字符串值内部的裸换行才会被转义。"""
    legit = '{"content": "line1\\nline2"}'
    assert _escape_control_chars_in_json_strings(legit) == legit
    assert json.loads(_escape_control_chars_in_json_strings(legit)) == {
        "content": "line1\nline2"
    }

    raw = '{"content": "line1\nline2"}'
    assert json.loads(_escape_control_chars_in_json_strings(raw)) == {
        "content": "line1\nline2"
    }

    # 结构空白不受影响
    structured = '{\n  "a": 1\n}'
    assert _escape_control_chars_in_json_strings(structured) == structured
    assert json.loads(_escape_control_chars_in_json_strings(structured)) == {"a": 1}


# ---------------------------------------------------------------------------
# Kimi 请求附加项：prompt_cache_key 缓存亲和 + reasoning_effort 推理强度
# ---------------------------------------------------------------------------


def _patch_session(monkeypatch, session_id):
    monkeypatch.setattr("core.app_context.get_current_session", lambda: session_id)


def _patch_model_cfg(monkeypatch, cfg):
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, base_url=None: cfg,
    )


def _without_cache_extras(result):
    """剥离缓存附加项（Qwen 会话缓存头 / Kimi 亲和键）。

    思考档位契约测试只校验推理参数编码；缓存附加项由家族判定测试
    （test_cache_family_routing.py）与 qwen/kimi 专项用例覆盖。
    """
    cleaned = dict(result)
    cleaned.pop("extra_headers", None)
    extra_body = cleaned.get("extra_body")
    if isinstance(extra_body, dict):
        cleaned["extra_body"] = {
            key: value
            for key, value in extra_body.items()
            if key != "prompt_cache_key"
        }
    return cleaned


def test_kimi_openai_route_sends_prompt_cache_key(monkeypatch):
    """OpenAI 兼容路由携带会话级稳定且不暴露原会话名的缓存键。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options("openai/kimi-k3", "https://api.moonshot.cn/v1")

    cache_key = opts["extra_body"]["prompt_cache_key"]
    assert cache_key.startswith("ycode-")
    assert "session_abc123" not in cache_key
    assert _provider_request_options(
        "openai/kimi-k3", "https://api.moonshot.cn/v1"
    )["extra_body"]["prompt_cache_key"] == cache_key
    assert "metadata" not in opts
    # 内置默认表：k3 → 131072
    assert opts["max_tokens"] == 131072


def test_kimi_third_party_openai_route_sends_cache_key(monkeypatch):
    """第三方 OpenAI 网关同样发送缓存亲和键：判定只看模型家族。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options(
        "openai/kimi-k2.6", "https://tokenrhythm.studio/v1"
    )

    assert opts["max_tokens"] == 128000
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")


@pytest.mark.parametrize("api_base", [
    "http://api.moonshot.cn/v1",
    "ftp://api.moonshot.ai/v1",
    "https://user:pass@api.kimi.com/coding/v1",
    "https://api.kimi.com:8443/coding/v1",
    "https://api.kimi.com/coding-evil/v1",
])
def test_kimi_cache_affinity_covers_noncanonical_api_bases(monkeypatch, api_base):
    """任意 api_base（含非规范官方 URL）都发送缓存亲和键：判定只看模型家族。"""
    from core.llm import _provider_request_options, _supports_cache_keepalive

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options("openai/kimi-k3", api_base)

    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")
    assert _supports_cache_keepalive("openai/kimi-k3", api_base) is True


def test_kimi_anthropic_route_sends_metadata_user_id(monkeypatch):
    """Anthropic 路由（api.kimi.com/coding）：用 metadata.user_id 做缓存亲和。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options("anthropic/kimi-for-coding", "https://api.kimi.com/coding")

    assert opts["metadata"]["user_id"].startswith("ycode-")
    assert "session_abc123" not in opts["metadata"]["user_id"]
    assert "extra_body" not in opts
    # 原有 thinking 配置不受影响
    assert opts["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_non_kimi_model_gets_no_cache_affinity(monkeypatch):
    """非 Kimi 模型不得附加 prompt_cache_key / metadata，行为与改动前一致。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options("openai/gpt-4o", "https://api.openai.com/v1")

    assert opts == {"max_tokens": 32000}


def test_official_deepseek_v4_uses_explicit_thinking_contract(monkeypatch):
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options(
        "openai/deepseek-v4-pro", "https://api.deepseek.com"
    )

    assert opts["reasoning_effort"] == "max"
    assert opts["extra_body"]["thinking"] == {"type": "enabled"}


def test_deepseek_thinking_is_explicit_for_aggregators(monkeypatch):
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, None)
    assert _provider_request_options(
        "openai/deepseek-v4-pro", "https://aggregator.example/v1"
    ) == {"max_tokens": 384000}

    _patch_model_cfg(monkeypatch, {
        "enable_thinking": False,
        "reasoning_effort": "max",
    })
    disabled = _provider_request_options(
        "openai/deepseek-v4-pro", "https://api.deepseek.com"
    )
    assert "reasoning_effort" not in disabled
    assert disabled["extra_body"]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    ("model_name", "choice", "expected"),
    [
        (
            "openai/kimi-k3",
            "high",
            {"max_tokens": 131072, "extra_body": {"reasoning_effort": "high"}},
        ),
        (
            "openai/deepseek-v4-flash",
            "high",
            {
                "max_tokens": 384000,
                "extra_body": {"thinking": {"type": "enabled"}},
                "reasoning_effort": "high",
                "allowed_openai_params": ["reasoning_effort"],
            },
        ),
        (
            "openai/qwen3.8-max",
            "medium",
            {"max_tokens": 131000, "extra_body": {"enable_thinking": True, "reasoning_effort": "medium"}},
        ),
        (
            "openai/glm-5.2",
            "max",
            {
                "max_tokens": 128000,
                "extra_body": {"thinking": {"type": "enabled"}},
                "reasoning_effort": "max",
                "allowed_openai_params": ["reasoning_effort"],
            },
        ),
        (
            "openai/kimi-k2.6",
            "off",
            {"max_tokens": 128000, "extra_body": {"thinking": {"type": "disabled"}}},
        ),
        (
            "openai/LongCat-2.0",
            "on",
            {"max_tokens": 128000, "extra_body": {"thinking": {"type": "enabled"}}},
        ),
        (
                "openai/mimo-v2.5-pro",
                "off",
                {"max_tokens": 131072, "extra_body": {"thinking": {"type": "disabled"}}},        ),
        (
                "openai/mimo-v2.6-pro",
                "on",
                {"max_tokens": 131072, "extra_body": {"thinking": {"type": "enabled"}}},        ),
        (
            "openai/qwen3.7-max",
            "off",
            {"max_tokens": 131000, "extra_body": {"enable_thinking": False}},
        ),
        (
            "openai/seed-2.1-pro",
            "low",
            {
                "max_tokens": 131000,
                "extra_body": {"thinking": {"type": "enabled"}},
                "reasoning_effort": "low",
                "allowed_openai_params": ["reasoning_effort"],
            },
        ),
    ],
)
def test_model_reasoning_choice_uses_its_exact_request_contract(
    monkeypatch, model_name, choice, expected
):
    """不同模型不能复用一套会遗漏开关或发送非法档位的参数。"""
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, {"reasoning_choice": choice})

    result = _provider_request_options(model_name, "https://gateway.example/v1")

    assert _without_cache_extras(result) == expected


@pytest.mark.parametrize(
    ("model_name", "choice", "expected"),
    [
        (
            "openai/kimi-k2.6",
            "off",
            {"max_tokens": 128000, "extra_body": {"thinking": {"type": "disabled"}}},
        ),
        (
            "openai/qwen3.7-max",
            "off",
            {"max_tokens": 131000, "extra_body": {"enable_thinking": False}},
        ),
        (
            "openai/kimi-k3",
            "high",
            {"max_tokens": 8192, "extra_body": {"reasoning_effort": "high"}},
        ),
    ],
)
def test_new_reasoning_choice_owns_the_request_contract_over_legacy_fields(
    monkeypatch, model_name, choice, expected
):
    """新选择存在时，旧 budget/enable 字段不能再生成第二套冲突参数。"""
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, {
        "reasoning_choice": choice,
        "enable_thinking": choice != "off",
        "thinking_budget": 4096,
        "max_tokens": 8192 if model_name.endswith("kimi-k3") else None,
    })

    result = _provider_request_options(model_name, "https://gateway.example/v1")

    if result.get("max_tokens") is None:
        result.pop("max_tokens", None)
    assert _without_cache_extras(result) == expected


@pytest.mark.parametrize(
    ("model_name", "model_cfg", "expected"),
    [
        (
            "openai/qwen3.7-max",
            {"enable_thinking": True},
            {"max_tokens": 131000, "extra_body": {"enable_thinking": True}},
        ),
        (
            "openai/kimi-k3",
            {"enable_thinking": True, "reasoning_effort": "high"},
            {"max_tokens": 131072, "extra_body": {"reasoning_effort": "high"}},
        ),
        (
            "openai/qwen3.7-max",
            {"thinking_budget": 4096},
            {"max_tokens": 131000},
        ),
    ],
)
def test_registered_models_migrate_legacy_reasoning_without_double_encoding(
    monkeypatch, model_name, model_cfg, expected
):
    """已注册模型的历史字段也只能由模型专属策略编码。"""
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, model_cfg)

    result = _provider_request_options(model_name, "https://gateway.example/v1")
    assert _without_cache_extras(result) == expected


def test_provider_options_use_exact_model_id_when_routes_are_duplicated(monkeypatch):
    """同一路由的两个模型 ID 可以保存不同选择，请求必须读取当前 ID。"""
    from core.llm import _provider_request_options

    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_id",
        lambda model_id: {"id": model_id, "reasoning_choice": "off"},
        raising=False,
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda *_args, **_kwargs: {"id": "first", "reasoning_choice": "high"},
    )

    result = _provider_request_options(
        "openai/deepseek-v4-flash",
        "https://gateway.example/v1",
        model_id="second",
    )

    assert result == {"max_tokens": 384000, "extra_body": {"thinking": {"type": "disabled"}}}


def test_default_reasoning_choice_adds_no_user_override(monkeypatch):
    """默认必须保留模型端行为，不能暗中强制某个档位。"""
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, {"reasoning_choice": "default"})

    result = _provider_request_options(
        "openai/qwen3.8-max", "https://gateway.example/v1"
    )
    assert _without_cache_extras(result) == {"max_tokens": 131000}


def test_effort_without_enabled_thinking_fails_before_transport():
    """请求中出现强度却没有开启思考时必须立即失败。"""
    from core.llm import _assert_reasoning_request_consistent
    from core.reasoning_capabilities import get_reasoning_capability

    capability = get_reasoning_capability("deepseek-v4-flash")

    with pytest.raises(ValueError, match="思考强度.*未开启思考"):
        _assert_reasoning_request_consistent(
            {"reasoning_effort": "high"}, capability
        )


def test_reasoning_effort_injected_for_kimi_openai_route(monkeypatch):
    """配置 reasoning_effort 后，Kimi OpenAI 兼容路由注入到 extra_body。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, {"reasoning_effort": "low"})

    opts = _provider_request_options("openai/kimi-k3-256k", "https://api.moonshot.cn/v1")

    assert opts["extra_body"]["reasoning_effort"] == "low"
    # 与 prompt_cache_key 共存于同一 extra_body
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")


def test_reasoning_effort_skipped_on_anthropic_route_and_invalid_value_fails(monkeypatch):
    """Anthropic 固定路由不注入档位；损坏配置必须显式失败。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")

    # Anthropic 路由：推理强度由 thinking_budget 控制
    _patch_model_cfg(monkeypatch, {"reasoning_effort": "high"})
    opts = _provider_request_options("anthropic/kimi-for-coding", "https://api.kimi.com/coding")
    assert "extra_body" not in opts

    # 非法值：不能静默忽略后继续发送
    _patch_model_cfg(monkeypatch, {"reasoning_effort": "turbo"})
    with pytest.raises(ValueError, match="turbo"):
        _provider_request_options("openai/kimi-k3", "https://api.moonshot.cn/v1")


@pytest.mark.parametrize(
    ("choice", "budget_tokens"),
    [("low", 1024), ("high", 4096), ("max", 16384)],
)
def test_k3_reasoning_choice_maps_to_anthropic_thinking_budget(
    monkeypatch, choice, budget_tokens
):
    """K3 的分级思考在 Anthropic 路由必须转换为原生 token 预算。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, {"reasoning_choice": choice})

    opts = _provider_request_options(
        "anthropic/k3", "https://api.kimi.com/coding"
    )

    assert opts["thinking"] == {
        "type": "enabled",
        "budget_tokens": budget_tokens,
    }
    assert opts["max_tokens"] == 131072
    assert "reasoning_effort" not in opts
    assert "reasoning_effort" not in opts.get("extra_body", {})


@pytest.mark.parametrize(
    ("choice", "effort"),
    [("low", "low"), ("medium", "medium"), ("high", "high")],
)
def test_stepfun_anthropic_route_maps_effort_to_output_config(
    monkeypatch, choice, effort
):
    """阶跃 Anthropic 兼容路由：档位映射为官方 output_config.effort，而非 K3 的 budget。"""
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, {"reasoning_choice": choice})

    opts = _provider_request_options(
        "anthropic/step-3.7-flash", "https://api.stepfun.com/step_plan"
    )

    assert opts["output_config"] == {"effort": effort}
    assert opts["allowed_openai_params"] == ["output_config"]
    assert "thinking" not in opts
    assert "reasoning_effort" not in opts
    assert "reasoning_effort" not in opts.get("extra_body", {})


def test_unknown_always_on_effort_model_fails_on_anthropic_route(monkeypatch):
    """未适配 Anthropic 协议的 always_on_effort 模型必须显式失败，不得伪造参数。"""
    from core.llm import _provider_request_options
    from core.reasoning_capabilities import (
        REASONING_CAPABILITIES,
        ReasoningCapability,
    )

    monkeypatch.setitem(
        REASONING_CAPABILITIES,
        "future-model",
        ReasoningCapability("effort", ("default", "high"), "always_on_effort"),
    )
    _patch_model_cfg(monkeypatch, {"reasoning_choice": "high"})

    with pytest.raises(
        ValueError,
        match="Anthropic 路由不支持该模型的 reasoning_effort 参数",
    ):
        _provider_request_options(
            "anthropic/future-model", "https://gateway.example/v1"
        )


def test_explicit_model_config_still_gets_cache_affinity(monkeypatch):
    """用户显式配置 max_tokens 时，缓存亲和依然生效（改动前该分支会提前返回）。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, {"max_tokens": 8192, "enable_thinking": None, "thinking_budget": None})

    opts = _provider_request_options("openai/kimi-k3", "https://api.moonshot.cn/v1")

    assert opts["max_tokens"] == 8192
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")


def test_kimi_cache_affinity_isolated_by_scope_and_tool_contract(monkeypatch):
    """主代理、子代理及不同工具契约不得共享缓存通道。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    def key(scope, contract):
        return _provider_request_options(
            "openai/kimi-for-coding",
            "https://api.kimi.com/coding/v1",
            cache_scope=scope,
            tool_contract=contract,
        )["extra_body"]["prompt_cache_key"]

    main = key("main", "tools-v1")
    assert key("main", "tools-v1") == main
    assert key("subagent:explore", "tools-v1") != main
    assert key("main", "tools-v2") != main


def test_explicit_cache_session_is_independent_of_current_ui_session(monkeypatch):
    """后台子代理必须锁定启动时父会话，不能被之后的 UI 会话切换影响。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "ui-session-changed")
    _patch_model_cfg(monkeypatch, None)

    first = _provider_request_options(
        "openai/kimi-for-coding",
        "https://api.kimi.com/coding/v1",
        cache_scope="subagent:test",
        cache_session_id="captured-parent-session",
    )["extra_body"]["prompt_cache_key"]
    _patch_session(monkeypatch, "another-ui-session")
    second = _provider_request_options(
        "openai/kimi-for-coding",
        "https://api.kimi.com/coding/v1",
        cache_scope="subagent:test",
        cache_session_id="captured-parent-session",
    )["extra_body"]["prompt_cache_key"]

    assert first == second


def test_cache_request_classification_reports_exact_invalidation_reason():
    from core.llm import _classify_cache_request

    session = "telemetry-classification-session"
    base_messages = [{"role": "system", "content": "stable"}]

    assert _classify_cache_request(
        session, "main", "openai/kimi", "https://api.kimi.test/v1", "tools-a", base_messages
    ) == "冷启动"
    assert _classify_cache_request(
        session,
        "main",
        "openai/kimi",
        "https://api.kimi.test/v1",
        "tools-a",
        base_messages + [{"role": "user", "content": "append"}],
    ) == ""
    assert _classify_cache_request(
        session,
        "main",
        "openai/kimi",
        "https://api.kimi.test/v1",
        "tools-b",
        base_messages + [{"role": "user", "content": "append"}],
    ) == "工具契约变化"
    assert _classify_cache_request(
        session,
        "main",
        "openai/other-kimi",
        "https://api.kimi.test/v1",
        "tools-b",
        base_messages + [{"role": "user", "content": "append"}],
    ) == "模型或接口变化"
    assert _classify_cache_request(
        session,
        "main",
        "openai/other-kimi",
        "https://api.kimi.test/v1",
        "tools-b",
        [{"role": "system", "content": "rewritten"}],
    ) == "上下文压缩或历史重写"


def test_cache_usage_separates_latest_hot_and_session_cumulative():
    from core.llm import _record_cache_usage

    session = "telemetry-usage-session"
    cold = _record_cache_usage(session, "main", 1000, 0, "冷启动")
    assert cold["latest_rate"] == 0
    assert cold["hot_prompt"] == 0
    assert cold["cumulative_rate"] == 0

    hot = _record_cache_usage(session, "main", 1000, 990, "")
    assert hot["latest_rate"] == 99
    assert hot["hot_rate"] == 99
    assert hot["hot_turns"] == 1
    assert hot["cumulative_rate"] == 49.5
    assert hot["invalidation_reason"] == ""


def test_overflow_refill_uses_compressed_history_and_keeps_system_prefix():
    """溢出强制压缩后的历史回填必须走 get_history_for_llm 并保留系统提示词。

    修复前误用全量 get_history：刚被摘要排除的消息被原样塞回（重试载荷
    不降反升，兜底路径必然再次超限并报出误导性错误）、记忆摘要不挂载、
    首部系统提示词（每轮现组、不落库）整条丢失。
    """
    from core.llm import _refill_messages_after_compression
    from core.memory import MemoryManager

    manager = MemoryManager(":memory:")
    manager.create_session_explicit("overflow-refill-session", workdir="GLOBAL", source="GUI")
    for i in range(8):
        manager.add_message(
            "overflow-refill-session",
            "user" if i % 2 == 0 else "assistant",
            f"消息 {i}",
        )

    # 模拟强制压缩已完成：前 5 条被摘要排除（水位推进），摘要落库
    session_row = manager.conn.execute(
        "SELECT id FROM sessions WHERE session_name = ?", ("overflow-refill-session",)
    ).fetchone()
    session_id = session_row[0]
    ids = [
        row[0]
        for row in manager.conn.execute(
            "SELECT id FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    ]
    manager.conn.execute(
        "UPDATE sessions SET memory_summary = ?, summarized_until_msg_id = ? WHERE id = ?",
        ("这是压缩后的记忆摘要", ids[4], session_id),
    )
    manager.conn.commit()

    # 溢出时刻的内存列表：系统提示词 + 全量历史（含已被摘要排除的消息）
    messages = [{"role": "system", "content": "stable system prompt"}]
    messages.extend(manager.get_history("overflow-refill-session"))
    assert len(messages) == 9

    _refill_messages_after_compression(messages, manager, "overflow-refill-session")

    # 1. 系统提示词保留在首位（每轮现组、不落库，DB 重读拿不回它）
    assert messages[0] == {"role": "system", "content": "stable system prompt"}
    # 2. 记忆摘要作为首条 user 伪消息挂载
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("[历史对话记忆摘要")
    assert "这是压缩后的记忆摘要" in messages[1]["content"]
    # 3. 已被摘要排除的消息不得回流
    contents = [str(m.get("content")) for m in messages]
    for summarized in (f"消息 {i}" for i in range(5)):
        assert summarized not in contents
    # 4. 活跃区消息（水位之后）完整保留
    for active in (f"消息 {i}" for i in range(5, 8)):
        assert active in contents


def test_overflow_refill_preserves_extra_system_blocks_and_drops_legacy_db_systems():
    """回填须保留内存列表中的全部 system 块（含会话内注入块），并丢弃 DB 旧格式
    遗留的 system 上下文块（与 prepare_turn_messages 的清理口径一致）。"""
    from core.llm import _refill_messages_after_compression
    from core.memory import MemoryManager

    manager = MemoryManager(":memory:")
    manager.create_session_explicit("overflow-refill-legacy", workdir="GLOBAL", source="GUI")
    manager.add_message("overflow-refill-legacy", "user", "最新问题")
    # 模拟 DB 中遗留的旧格式 system 上下文块
    session_row = manager.conn.execute(
        "SELECT id FROM sessions WHERE session_name = ?", ("overflow-refill-legacy",)
    ).fetchone()
    manager.conn.execute(
        "INSERT INTO messages (session_id, role, content) VALUES (?, 'system', 'legacy context block')",
        (session_row[0],),
    )
    manager.conn.commit()

    messages = [
        {"role": "system", "content": "stable system prompt"},
        {"role": "system", "content": "# Auto Memory\n注入块"},
        {"role": "user", "content": "最新问题"},
    ]
    _refill_messages_after_compression(messages, manager, "overflow-refill-legacy")

    assert [m["role"] for m in messages] == ["system", "system", "user"]
    assert messages[0]["content"] == "stable system prompt"
    assert messages[1]["content"] == "# Auto Memory\n注入块"
    # DB 遗留 system 块不回流（否则违反 system 必须在开头的协议约束）
    assert all("legacy context block" != str(m.get("content")) for m in messages)


@pytest.mark.parametrize(
    ("model_name", "api_base", "expected_max_tokens"),
    [
        ("openai/deepseek-v4-pro", "https://api.deepseek.com", 384000),
        (
            "openai/deepseek-v4.1-flash",
            "https://tokenrhythm.studio/v1",
            384000,
        ),
        (
            "openai/DeepSeek-v9-future-model",
            "https://gateway.example/v1",
            384000,
        ),
        ("openai/glm-5", "https://gateway.example/v1", 128000),
        ("openai/glm-5.1", "https://gateway.example/v1", 128000),
        ("openai/glm-5.2", "https://gateway.example/v1", 128000),
        ("openai/glm-5.3", "https://gateway.example/v1", 131000),
        ("openai/glm-5.3-flash", "https://gateway.example/v1", 131000),
        ("openai/qwen3.8-max", "https://gateway.example/v1", 131000),
        ("openai/qwen38-27b", "https://gateway.example/v1", 131000),
        ("openai/kimi-k2.5", "https://tokenrhythm.studio/v1", 64000),
        ("openai/kimi-k2.6", "https://tokenrhythm.studio/v1", 128000),
        ("openai/kimi-k2.7-code", "https://tokenrhythm.studio/v1", 16000),
        ("openai/kimi-k2.7-code", "https://gateway.example/v1", 128000),
        ("openai/k3", "https://api.moonshot.cn/v1", 131072),
        ("anthropic/k3-256k", "https://api.kimi.com/coding", 131072),
        ("openai/kimi-k3", "https://gateway.example/v1", 131072),
        ("openai/LongCat-2.0", "https://gateway.example/v1", 128000),
        ("openai/seed-2.1-pro", "https://gateway.example/v1", 131000),
        ("openai/seed-2.1-turbo", "https://gateway.example/v1", 131000),
        ("openai/mimo-v2.5-pro", "https://gateway.example/v1", 131072),
        ("openai/mimo-v2.6-pro", "https://gateway.example/v1", 131072),
        ("openai/mimo-v2.6-flash", "https://gateway.example/v1", 131072),
        ("openai/mimo-v2.6-pro-ultraspeed", "https://gateway.example/v1", 131072),
        ("openai/minimax-m2.5", "https://gateway.example/v1", 200000),
        ("openai/minimax-m2.7", "https://gateway.example/v1", 192000),
        ("openai/gpt-4o", "https://gateway.example/v1", 32000),
        ("openai/step-3.7-flash", "https://gateway.example/v1", 32000),
        ("openai/kimi-for-coding", "https://gateway.example/v1", 32000),
    ],
)
def test_builtin_default_max_tokens_table(
    monkeypatch, model_name, api_base, expected_max_tokens
):
    """未显式配置 max_tokens 时，按内置默认表给出各模型的输出上限。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options(model_name, api_base)

    assert opts["max_tokens"] == expected_max_tokens


def test_explicit_config_max_tokens_wins_over_builtin_table(monkeypatch):
    """config.json 显式 max_tokens 优先于内置默认表。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, {"max_tokens": 8192})

    opts = _provider_request_options(
        "openai/deepseek-v4-pro", "https://api.deepseek.com"
    )

    assert opts["max_tokens"] == 8192


def test_unregistered_model_default_choice_leaves_request_untouched():
    """未登记模型在默认档绝不污染请求体：不注入任何思考参数。

    未登记进能力表的模型（厂商新增、网关自定义名）不再是 hidden——那会让它们
    凭空失去可操作的思考开关。现在它们拿通用 effort 能力，但"没存过任何思考
    选择"时必须保持请求体零变化，这是老 hidden 静默回落想守护的核心意图。
    """
    from core.llm import _apply_reasoning_choice

    for model_cfg in ({}, {"reasoning_choice": "default"}):
        result = {}
        capability = _apply_reasoning_choice(
            result, "openai/brand-new-model", model_cfg
        )

        assert capability.kind == "effort"
        assert capability.options == ("default", "off", "low", "medium", "high", "max")
        assert result == {}


def test_unregistered_model_default_choice_keeps_other_params_intact():
    """默认档的零注入只针对思考参数，不得清改请求里已有的其他字段。"""
    from core.llm import _apply_reasoning_choice

    result = {"max_tokens": 128000}
    capability = _apply_reasoning_choice(
        result,
        "openai/qwen-unknown-variant",
        {},
    )

    assert capability.kind == "effort"
    assert result == {"max_tokens": 128000}


def test_unregistered_model_explicit_choice_injects_reasoning_effort():
    """未登记模型显式选择强度时只注入顶层 reasoning_effort，关闭注入 none。

    未登记模型多为网关自定义名，走 OpenAI 标准字段（effort_switch）比厂商私有
    的 thinking 对象被接受的概率更高。
    """
    from core.llm import _apply_reasoning_choice

    for choice, expected in (("high", "high"), ("medium", "medium"), ("off", "none")):
        result = {}
        capability = _apply_reasoning_choice(
            result,
            "openai/brand-new-model",
            {"reasoning_choice": choice},
        )

        assert capability.kind == "effort"
        assert result["reasoning_effort"] == expected
        assert "extra_body" not in result


def test_explicitly_hidden_model_silently_falls_back_without_injection():
    """显式登记为 hidden 的模型存过任何思考选择时依然静默回落、零注入。

    hidden 是"已确认该模型不支持思考"的精确事实（如 sensenova-6.7-flash-lite），
    与"只是没登记的新模型"不同：后者拿 toggle 可操作开关，前者必须继续走
    _apply_reasoning_choice 里的 hidden 短路分支，不因残留的历史思考选择报错，
    也不往请求体里注入任何思考参数。
    """
    from core.llm import _apply_reasoning_choice

    for model_cfg in (
        {"reasoning_choice": "on"},
        {"reasoning_choice": "off"},
        {"enable_thinking": True},
        {"enable_thinking": False},
        {"reasoning_effort": "high"},
    ):
        result = {"max_tokens": 128000}
        capability = _apply_reasoning_choice(
            result, "openai/sensenova-6.7-flash-lite", model_cfg
        )

        assert capability.kind == "hidden"
        assert capability.options == ()
        assert result == {"max_tokens": 128000}



def test_unknown_deepseek_prefixed_model_applies_official_thinking_encoding(
    monkeypatch,
):
    """任意厂商的 deepseek 前缀新模型按官方思考协议编码，不报错。

    依据 https://api-docs.deepseek.com/zh-cn/guides/thinking_mode：
    关闭 = thinking.type=disabled；开启 = thinking.type=enabled + reasoning_effort。
    """
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(
        monkeypatch, {"reasoning_choice": "off", "enable_thinking": False}
    )
    opts = _provider_request_options(
        "openai/DeepSeek-Flash-X1", "https://gateway.example/v1"
    )

    assert opts["max_tokens"] == 384000
    assert opts["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in opts

    _patch_model_cfg(
        monkeypatch, {"reasoning_choice": "high", "enable_thinking": True}
    )
    opts = _provider_request_options(
        "openai/DeepSeek-Flash-X1", "https://gateway.example/v1"
    )

    assert opts["extra_body"]["thinking"] == {"type": "enabled"}
    assert opts["reasoning_effort"] == "high"
    assert opts["allowed_openai_params"] == ["reasoning_effort"]
