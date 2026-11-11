"""斜杠命令分发层（core/slash_commands/dispatch.py）的纯逻辑测试。

分发层存在的意义是把「未命中 / prompt / action / notice / error」这五种结局一次
性收敛好，入口层才不用各写一遍分支。所以这里逐个结局各造一条用例：既验分类，
也验关键字段（prompt 里带没带参数、action 的动作名对不对、error 里有没有可定位
的信息），而不只是断言 ok/not ok。

不 import GUI，也不启动任何回合：这个模块的设计目标就是纯逻辑可测。
"""

import pytest

from core.slash_commands import local
from core.slash_commands.dispatch import dispatch_command
from core.slash_commands.specs import register_builtin_commands


@pytest.fixture(autouse=True)
def _ensure_builtins():
    """每个用例前补齐内置命令注册。

    注册函数本身幂等，而分发层只在白名单为空时自动补注册；其它用例（例如
    test_slash_local_commands 里的 registry.reset()）可能把表清空，显式补一次
    就不会出现「单独跑过、组合跑挂」的顺序依赖。
    """
    register_builtin_commands()


def test_path_like_text_is_not_a_command():
    """/usr/bin 这类路径提问必须原样放行，否则用户问个绝对路径都被吞。"""
    result = dispatch_command("/usr/bin 是什么")

    assert result.handled is False


def test_prompt_command_expands_args_into_prompt():
    """/fix 只展开提示词：参数必须落在 prompt 里，且不产生本地副作用。"""
    result = dispatch_command("/fix boom")

    assert result.handled is True
    assert result.kind == "prompt"
    assert "boom" in result.prompt


def test_remember_without_content_reports_error():
    """空参数的 /remember 必须报用法错误：放行会把空行写进记忆日志。"""
    result = dispatch_command("/remember   ")

    assert result.handled is True
    assert result.kind == "error"
    assert "用法" in result.error


def test_remember_success_becomes_notice(monkeypatch):
    """本地命令成功不是 prompt 也不是 action，而是就地完成的 notice。

    monkeypatch 打在 local 模块内的名字上：命令层只负责校验与包装，
    真实写文件属于 core.memory_kairos 的职责，这里不该真写。
    """
    monkeypatch.setattr(local, "append_to_daily_log", lambda entry: True)

    result = dispatch_command("/remember 用中文回答")

    assert result.handled is True
    assert result.kind == "notice"
    assert "记忆" in result.notice


def test_agent_command_becomes_action_with_type():
    """/agent 只产出动作描述，核心层不启动子代理（副作用留给入口适配层）。"""
    result = dispatch_command("/agent explore 分析主流程")

    assert result.kind == "action"
    assert result.action["action"] == "spawn_subagent"
    assert result.action["subagent_type"] == "explore"


def test_workflow_with_bad_json_reports_json_error():
    """JSON 语法错误必须尽早暴露成 error，而不是带进工作流跑到一半才炸。"""
    result = dispatch_command("/workflow 发布 {bad}")

    assert result.kind == "error"
    assert "JSON" in result.error


def test_double_slash_is_escaped_text():
    """// 开头是转义写法：整条按普通消息发送，不能当命令执行。"""
    result = dispatch_command("//fix 正文")

    assert result.handled is False


def test_compact_without_compress_capability_reports_error():
    """/compact 依赖入口注入 compress；当前 ctx 给 None，应报能力缺失而不是成功。

    这条用例同时固化了一个已知限制：GUI 目前没有把会话压缩能力注入 ctx，
    所以 /compact 在 GUI 上表现为「当前通道不支持手动压缩」，属于预期行为。
    """
    result = dispatch_command("/compact", ctx={"compress": None})

    assert result.kind == "error"
    assert "不支持" in result.error
