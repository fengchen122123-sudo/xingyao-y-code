"""守护测试：秘书提示词必须与秘书会话真实可用的工具集对齐。

背景（升级设计 2026-11-04 第 4 节）：秘书会话走只读门控
``core/secretary.py:secretary_allows_tool``，写文件、执行命令、起子代理、
计划写这几类工具在执行前就被拒。提示词若继续教模型调用它们，模型只会撞墙
并给出"我先用 plan_write 拆解"这类做不到的话术。以下断言是防回退护栏。
"""

from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parents[1] / "system_prompt_secretary_gui.md"

# 秘书会话里被门控直接拒绝的工具名：提示词正文不得出现（哪怕只是在举例）
FORBIDDEN_TOOLS = (
    "plan_write",
    "run_subagent",
    "edit_file",
    "write_file",
    "execute_command",
)

# 秘书会话真正可用的核心工具名：必须都在提示词里被写明用法
REQUIRED_TOOLS = (
    "list_dispatches",
    "get_dispatch_result",
    "dispatch_task",
    "dispatch_batch",
    "search_conversations",
    "open_session",
    "create_project",
    "remember_decision",
    "read_project_profile",
)


def _prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _section(heading: str) -> str:
    """取出一级标题 ``heading`` 的章节正文（到下一个一级标题为止）。

    章节不存在时返回空串，交由断言报错——不存在与内容为空在这里等价。
    """
    lines = _prompt().splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        return ""
    body = []
    for line in lines[start:]:
        if line.startswith("# "):
            break
        body.append(line)
    return "\n".join(body)


def test_prompt_does_not_teach_blocked_tools():
    """门控禁用的工具不得出现在提示词里（防止回退到"教模型做做不到的事"）。"""
    text = _prompt()
    found = [name for name in FORBIDDEN_TOOLS if name in text]
    assert found == [], f"提示词仍提及秘书会话不可用的工具：{found}"


def test_prompt_lists_real_secretary_tools():
    """秘书真实可用的工具必须在提示词里被写明。"""
    text = _prompt()
    missing = [name for name in REQUIRED_TOOLS if name not in text]
    assert missing == [], f"提示词缺少真实可用工具：{missing}"


def test_dispatch_protocol_section_has_three_bucket_summary():
    """存在 `# 调度协议` 章节，且章节内给出「进行中 / 等你决策 / 已完成」三段汇总格式。"""
    body = _section("# 调度协议")
    assert body.strip(), "缺少 `# 调度协议` 章节"
    for bucket in ("进行中", "等你决策", "已完成"):
        assert bucket in body, f"`# 调度协议` 缺少汇总分段：{bucket}"


def test_prompt_has_no_subagent_section():
    """提示词不再有 `# 子 Agent` 整章（秘书不能起子代理）。"""
    assert "# 子 Agent" not in _prompt()


def test_prompt_still_opens_as_secretary():
    """文件仍以秘书身份段开头，身份前缀不能被改写掉。"""
    text = _prompt()
    first_line = text.splitlines()[0]
    assert first_line.startswith("你是「{{secretary_name}}」")
    assert "私人秘书" in first_line


def test_prompt_documents_dispatch_batch():
    """P3 已上线的批量派活必须在提示词里写明用法（下标依赖 / 阻断不重试）。"""
    body = _section("# 你的调度能力与硬约束")
    assert body.strip(), "缺少 `# 你的调度能力与硬约束` 章节"
    assert "dispatch_batch" in body, "`dispatch_batch` 已上线，提示词却没提"
    assert "depends_on" in body, "批量派活必须说明 depends_on 的用法"
    assert "8" in body, "批量派活必须写明单批上限 8 条"
    assert "阻断" in body, "批量派活必须说明上游失败时下游被阻断、不自动重试"
    # 汇总格式也要覆盖批量：按 batch_id 归组
    assert "batch_id" in _section("# 调度协议"), "批量任务要按 batch_id 归组汇报"


def test_core_principles_have_no_build_verification_rule():
    """秘书跑不了命令：`# 核心原则` 不得再要求"通过测试/编译/运行检查"。"""
    body = _section("# 核心原则")
    assert body.strip(), "缺少 `# 核心原则` 章节"
    for phrase in ("编译", "运行检查"):
        assert phrase not in body, (
            f"`# 核心原则` 仍含秘书做不到的验证口径：{phrase}"
        )
    # 取而代之：结论必须来自台账真实返回或本轮真实读到的内容
    assert "list_dispatches" in body
