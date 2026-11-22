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
    assert _command_matches_prefix("git", ("git",)) is True
    assert _command_matches_prefix("git status", ("git",)) is False
    assert _command_matches_prefix("gitfoo", ("git",)) is False
    assert _command_matches_prefix("gitfoo --bar", ("git",)) is False


def test_inline_exec_command_is_not_session_allowlisted():
    assert _session_prefix_for_command('python -c "print(1)"') is None
    assert _session_prefix_for_command("node -e 'console.log(1)'") is None
    assert _session_prefix_for_command("python script.py") == ("python", "script.py")
    assert _session_prefix_for_command("git status") == ("git", "status")
    assert _session_prefix_for_command("") is None


def test_inline_module_exec_flags_are_not_session_allowlisted():
    assert _session_prefix_for_command("python -m pip install requests") is None
    assert _session_prefix_for_command("python -m http.server 8080") is None
    assert _session_prefix_for_command('node -p "1+1"') is None
    assert _session_prefix_for_command("perl -p -i -e 's/a/b/' file") is None
    assert _session_prefix_for_command("python -V") == ("python", "-V")
    assert _session_prefix_for_command("python --version") == ("python", "--version")
    assert _session_prefix_for_command("python -m pip --version") is None
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
    legit = '{"content": "line1\\nline2"}'
    assert _escape_control_chars_in_json_strings(legit) == legit
    assert json.loads(_escape_control_chars_in_json_strings(legit)) == {
        "content": "line1\nline2"
    }

    raw = '{"content": "line1\nline2"}'
    assert json.loads(_escape_control_chars_in_json_strings(raw)) == {
        "content": "line1\nline2"
    }

    structured = '{\n  "a": 1\n}'
    assert _escape_control_chars_in_json_strings(structured) == structured
    assert json.loads(_escape_control_chars_in_json_strings(structured)) == {"a": 1}


def _patch_session(monkeypatch, session_id):
    monkeypatch.setattr("core.app_context.get_current_session", lambda: session_id)


def _patch_model_cfg(monkeypatch, cfg):
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, base_url=None: cfg,
    )


def _without_cache_extras(result):
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
    assert opts["max_tokens"] == 131072


def test_kimi_third_party_openai_route_sends_cache_key(monkeypatch):
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
    from core.llm import _provider_request_options, _supports_cache_keepalive

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options("openai/kimi-k3", api_base)

    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")
    assert _supports_cache_keepalive("openai/kimi-k3", api_base) is True


def test_kimi_anthropic_route_sends_metadata_user_id(monkeypatch):
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options("anthropic/kimi-for-coding", "https://api.kimi.com/coding")

    assert opts["metadata"]["user_id"].startswith("ycode-")
    assert "session_abc123" not in opts["metadata"]["user_id"]
    assert "extra_body" not in opts
    assert opts["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_non_kimi_model_gets_no_cache_affinity(monkeypatch):
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
            "high",
            {"max_tokens": 128000, "extra_body": {"reasoning_effort": "high"}},
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
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, {"reasoning_choice": choice})

    result = _provider_request_options(model_name, "https://gateway.example/v1")

    assert _without_cache_extras(result) == expected


@pytest.mark.parametrize(
    ("model_name", "choice", "expected"),
    [
        (
            "openai/kimi-k2.6",
            "high",
            {"max_tokens": 128000, "extra_body": {"reasoning_effort": "high"}},
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
            {"max_tokens": 131000, "extra_body": {"enable_thinking": True, "reasoning_effort": "low"}},
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
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, model_cfg)

    result = _provider_request_options(model_name, "https://gateway.example/v1")
    assert _without_cache_extras(result) == expected


def test_provider_options_use_exact_model_id_when_routes_are_duplicated(monkeypatch):
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
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, {"reasoning_choice": "default"})

    result = _provider_request_options(
        "openai/qwen3.8-max", "https://gateway.example/v1"
    )
    assert _without_cache_extras(result) == {"max_tokens": 131000}


def test_effort_without_enabled_thinking_fails_before_transport():
    from core.llm import _assert_reasoning_request_consistent
    from core.reasoning_capabilities import get_reasoning_capability

    capability = get_reasoning_capability("deepseek-v4-flash")

    with pytest.raises(ValueError, match="思考强度.*未开启思考"):
        _assert_reasoning_request_consistent(
            {"reasoning_effort": "high"}, capability
        )


def test_reasoning_effort_injected_for_kimi_openai_route(monkeypatch):
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, {"reasoning_effort": "low"})

    opts = _provider_request_options("openai/kimi-k3-256k", "https://api.moonshot.cn/v1")

    assert opts["extra_body"]["reasoning_effort"] == "low"
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")


def test_reasoning_effort_skipped_on_anthropic_route_and_invalid_value_fails(monkeypatch):
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")

    _patch_model_cfg(monkeypatch, {"reasoning_effort": "high"})
    opts = _provider_request_options("anthropic/kimi-for-coding", "https://api.kimi.com/coding")
    assert "extra_body" not in opts

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
    ("choice", "budget_tokens"), [("low", 1024), ("high", 4096), ("max", 16384)]
)
def test_kimi_family_reasoning_choice_maps_to_anthropic_thinking_budget(
    monkeypatch, choice, budget_tokens
):
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, {"reasoning_choice": choice})

    opts = _provider_request_options(
        "anthropic/kimi-k2.6", "https://api.moonshot.cn/anthropic"
    )

    assert opts["thinking"] == {"type": "enabled", "budget_tokens": budget_tokens}
    assert "reasoning_effort" not in opts
    assert "reasoning_effort" not in opts.get("extra_body", {})


def test_kimi_k27_code_reasoning_choice_injects_effort(monkeypatch):
    from core.llm import _provider_request_options

    _patch_model_cfg(monkeypatch, {"reasoning_choice": "max"})

    opts = _provider_request_options("openai/kimi-k2.7-code", "https://gateway.example/v1")

    assert opts["extra_body"]["reasoning_effort"] == "max"


@pytest.mark.parametrize(
    ("choice", "effort"),
    [("low", "low"), ("medium", "medium"), ("high", "high")],
)
def test_stepfun_anthropic_route_maps_effort_to_output_config(
    monkeypatch, choice, effort
):
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
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, {"max_tokens": 8192, "enable_thinking": None, "thinking_budget": None})

    opts = _provider_request_options("openai/kimi-k3", "https://api.moonshot.cn/v1")

    assert opts["max_tokens"] == 8192
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")


def test_kimi_cache_affinity_isolated_by_scope_and_tool_contract(monkeypatch):
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

    messages = [{"role": "system", "content": "stable system prompt"}]
    messages.extend(manager.get_history("overflow-refill-session"))
    assert len(messages) == 9

    _refill_messages_after_compression(messages, manager, "overflow-refill-session")

    assert messages[0] == {"role": "system", "content": "stable system prompt"}
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("[历史对话记忆摘要")
    assert "这是压缩后的记忆摘要" in messages[1]["content"]
    contents = [str(m.get("content")) for m in messages]
    for summarized in (f"消息 {i}" for i in range(5)):
        assert summarized not in contents
    for active in (f"消息 {i}" for i in range(5, 8)):
        assert active in contents


def test_overflow_refill_preserves_extra_system_blocks_and_drops_legacy_db_systems():
    from core.llm import _refill_messages_after_compression
    from core.memory import MemoryManager

    manager = MemoryManager(":memory:")
    manager.create_session_explicit("overflow-refill-legacy", workdir="GLOBAL", source="GUI")
    manager.add_message("overflow-refill-legacy", "user", "最新问题")
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
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, None)

    opts = _provider_request_options(model_name, api_base)

    assert opts["max_tokens"] == expected_max_tokens


def test_explicit_config_max_tokens_wins_over_builtin_table(monkeypatch):
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, {"max_tokens": 8192})

    opts = _provider_request_options(
        "openai/deepseek-v4-pro", "https://api.deepseek.com"
    )

    assert opts["max_tokens"] == 8192


def test_unregistered_model_default_choice_leaves_request_untouched():
    from core.llm import _apply_reasoning_choice

    for model_cfg in ({}, {"reasoning_choice": "default"}):
        result = {}
        capability = _apply_reasoning_choice(
            result, "openai/brand-new-model", model_cfg
        )

        assert capability.kind == "effort"
        assert capability.options == ("default", "off", "low", "medium", "high", "xhigh", "max")
        assert result == {}


def test_unregistered_model_default_choice_keeps_other_params_intact():
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
