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
    # 内置默认表行为保持不变
    assert opts["max_tokens"] == 32000


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

    assert opts == {}


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
    ) == {}

    _patch_model_cfg(monkeypatch, {
        "enable_thinking": False,
        "reasoning_effort": "max",
    })
    disabled = _provider_request_options(
        "openai/deepseek-v4-pro", "https://api.deepseek.com"
    )
    assert "reasoning_effort" not in disabled
    assert disabled["extra_body"]["thinking"] == {"type": "disabled"}


def test_reasoning_effort_injected_for_kimi_openai_route(monkeypatch):
    """配置 reasoning_effort 后，Kimi OpenAI 兼容路由注入到 extra_body。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")
    _patch_model_cfg(monkeypatch, {"reasoning_effort": "low"})

    opts = _provider_request_options("openai/kimi-k3-256k", "https://api.moonshot.cn/v1")

    assert opts["extra_body"]["reasoning_effort"] == "low"
    # 与 prompt_cache_key 共存于同一 extra_body
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")


def test_reasoning_effort_skipped_on_anthropic_route_and_invalid_value(monkeypatch):
    """Anthropic 路由不注入 reasoning_effort；非法值被忽略且不影响其他参数。"""
    from core.llm import _provider_request_options

    _patch_session(monkeypatch, "session_abc123")

    # Anthropic 路由：推理强度由 thinking_budget 控制
    _patch_model_cfg(monkeypatch, {"reasoning_effort": "high"})
    opts = _provider_request_options("anthropic/kimi-for-coding", "https://api.kimi.com/coding")
    assert "extra_body" not in opts

    # 非法值：忽略，其余参数正常
    _patch_model_cfg(monkeypatch, {"reasoning_effort": "turbo"})
    opts = _provider_request_options("openai/kimi-k3", "https://api.moonshot.cn/v1")
    assert "reasoning_effort" not in opts["extra_body"]
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")


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
