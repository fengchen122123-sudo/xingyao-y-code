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
