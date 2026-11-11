"""工具类斜杠命令（/agent /skill /workflow /commit /forget）的测试。

这五条命令的设计约束是「核心层零副作用」：handler 只解析参数并返回带 action 的
动作描述，真正执行交给 GUI/CLI 适配层。因此这里全部是纯函数断言——不启动子代理、
不调用真实 git、不写记忆文件——一旦哪个实现偷偷执行了副作用，这些用例就会暴露出来
（例如会抛异常或返回非纯数据结构）。
"""

import pytest

from core.slash_commands import registry, tooling
from core.slash_commands.specs import register_builtin_commands


def test_agent_without_task_returns_usage():
    """/agent 缺任务时必须报用法：只有类型没有任务，子代理无事可做。"""
    result = tooling.handle_agent("explore")

    assert result["ok"] is False
    assert "用法" in result["msg"]


def test_agent_splits_type_and_task():
    """首个空白切分类型与任务；任务里的空格要原样保留，不能被切成碎片。"""
    result = tooling.handle_agent("explore 分析主流程")

    assert result["ok"] is True
    assert result["action"] == "spawn_subagent"
    assert result["subagent_type"] == "explore"
    assert result["task"] == "分析主流程"


def test_skill_without_name_returns_usage():
    """技能名是唯一必填项，空参数必须拦住。"""
    result = tooling.handle_skill("   ")

    assert result["ok"] is False
    assert "用法" in result["msg"]


def test_skill_allows_empty_requirement(monkeypatch):
    """补充要求可为空（返回 ""），但非空时要原样带出。

    技能表用 monkeypatch 顶住：本机/CI 上未必装了 debug 技能，若真去扫磁盘，
    这条用例能否通过就取决于机器上装了什么，属于典型的环境依赖。
    """
    import core.skills as skills_mod

    monkeypatch.setattr(
        skills_mod,
        "get_active_skills_list",
        lambda: {
            "debug": {
                # 自述名与短 ID 一致：handler 优先用自述名做展示，
                # 两者相同时下面的断言才能精确校验「原样带出用户输入」。
                "name": "debug",
                "id": "/tmp/x/SKILL.md",
                "body": "技能正文",
                "content": "技能全文",
                "description": "调试技能",
            }
        },
    )
    result = tooling.handle_skill("debug 补充说明")

    assert result["ok"] is True
    assert result["action"] == "use_skill"
    assert result["skill"] == "debug"
    assert result["requirement"] == "补充说明"
    assert "技能正文" in result["content"]


def test_workflow_without_name_returns_usage():
    """/workflow 缺名称时报用法，连 JSON 都不必看。"""
    result = tooling.handle_workflow("")

    assert result["ok"] is False
    assert "用法" in result["msg"]


def test_workflow_parses_json_args():
    """JSON 参数在这里就解析成对象，避免工作流跑到一半才发现参数非法。"""
    result = tooling.handle_workflow('发版 {"tag":"v1"}')

    assert result["ok"] is True
    assert result["action"] == "run_workflow"
    assert result["name"] == "发版"
    assert result["args"] == {"tag": "v1"}


def test_workflow_without_args_defaults_to_empty_dict():
    """空参数表示无参运行，兜底成 {} 而不是报错。"""
    result = tooling.handle_workflow("发版")

    assert result["ok"] is True
    assert result["args"] == {}


@pytest.mark.parametrize("bad", ['发版 {bad json}', '发版 {"tag":}'])
def test_workflow_with_invalid_json_reports_json_error(bad):
    """非法 JSON 要报 ok=False，且 msg 点明 JSON 让用户知道该改哪里。"""
    result = tooling.handle_workflow(bad)

    assert result["ok"] is False
    assert "JSON" in result["msg"]


def test_commit_returns_action_with_confirm_required():
    """提交是不可逆的写操作：只产出动作描述并要求入口层二次确认。"""
    result = tooling.handle_commit("修复登录")

    assert result["ok"] is True
    assert result["action"] == "commit"
    assert result["message"] == "修复登录"
    assert result["confirm_required"] is True


def test_commit_allows_blank_message():
    """说明文字可为空：自动生成还是提示用户补写由适配层决定。"""
    result = tooling.handle_commit("  ")

    assert result["ok"] is True
    assert result["message"] == ""


def test_commit_without_yes_is_not_confirmed():
    """两阶段提交的第一阶段：裸 /commit 只暂存并索取确认，confirmed 必须为假。

    这是防误提交的关键开关——若这里被误判成已确认，适配层会直接落库。
    """
    result = tooling.handle_commit("修复登录")

    assert result["action"] == "commit"
    assert result["confirmed"] is False
    assert result["message"] == "修复登录"
    # 兼容既有语义：无论是否确认，动作性质仍是需确认的写操作
    assert result["confirm_required"] is True


def test_commit_with_yes_is_confirmed_and_strips_flag():
    """第二阶段：--yes 表示用户已确认，message 只保留其后的说明文字。"""
    result = tooling.handle_commit("--yes 修复登录")

    assert result["action"] == "commit"
    assert result["confirmed"] is True
    assert result["message"] == "修复登录"


def test_commit_with_bare_yes_confirms_with_empty_message():
    """只写 --yes 不写说明也要能提交：message 为空，交给适配层自动生成信息。"""
    result = tooling.handle_commit("--yes")

    assert result["confirmed"] is True
    assert result["message"] == ""


def test_forget_returns_action_with_confirm_required_and_rejects_blank():
    """/forget 需要关键词 + 确认；空关键词若放行可能连匹配全部记忆整片删除。"""
    result = tooling.handle_forget("旧偏好")

    assert result["ok"] is True
    assert result["action"] == "forget_memory"
    assert result["keyword"] == "旧偏好"
    assert result["confirm_required"] is True

    blank = tooling.handle_forget("   ")
    assert blank["ok"] is False
    assert "用法" in blank["msg"]


def test_forget_without_yes_is_not_confirmed():
    """两阶段删除的第一阶段：裸 /forget 只统计命中行，confirmed 必须为假。

    这是防误删的开关——若这里被误判成已确认，适配层会直接删掉当天日志与索引的命中行。
    """
    result = tooling.handle_forget("旧偏好")

    assert result["ok"] is True
    assert result["action"] == "forget_memory"
    assert result["keyword"] == "旧偏好"
    assert result["confirmed"] is False
    # 兼容既有语义：无论是否确认，动作性质仍是需确认的高危操作
    assert result["confirm_required"] is True


def test_forget_with_yes_is_confirmed_and_strips_flag():
    """第二阶段：--yes 表示用户已确认，keyword 只保留其后的关键词原文。"""
    result = tooling.handle_forget("--yes 旧偏好")

    assert result["ok"] is True
    assert result["action"] == "forget_memory"
    assert result["confirmed"] is True
    assert result["keyword"] == "旧偏好"


def test_forget_with_bare_yes_returns_usage():
    """只写 --yes 没有关键词必须报用法：确认了却没有删除目标，等价于盲目删除。"""
    result = tooling.handle_forget("--yes")

    assert result["ok"] is False
    assert "用法" in result["msg"]


def test_register_builtin_commands_registers_tooling_kinds():
    """登记后 kind 必须符合约定：/skill、/commit 走 hybrid，其余三条 local。"""
    registry.reset()
    try:
        register_builtin_commands()
        register_builtin_commands()  # 幂等：GUI 与 CLI 启动各调一次

        expected = {
            "/agent": "local",
            "/skill": "hybrid",
            "/workflow": "local",
            "/commit": "hybrid",
            "/forget": "local",
        }
        for name, kind in expected.items():
            spec = registry.get(name)
            assert spec is not None, name
            assert spec.kind == kind, name
            assert spec.handler is not None, name
    finally:
        registry.reset()
