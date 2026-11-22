from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parents[1] / "system_prompt_secretary_gui.md"

FORBIDDEN_TOOLS = (
    "plan_write",
    "run_subagent",
    "edit_file",
    "write_file",
    "execute_command",
)

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
    text = _prompt()
    found = [name for name in FORBIDDEN_TOOLS if name in text]
    assert found == [], f"提示词仍提及秘书会话不可用的工具：{found}"


def test_prompt_lists_real_secretary_tools():
    text = _prompt()
    missing = [name for name in REQUIRED_TOOLS if name not in text]
    assert missing == [], f"提示词缺少真实可用工具：{missing}"


def test_dispatch_protocol_section_has_three_bucket_summary():
    body = _section("# 调度协议")
    assert body.strip(), "缺少 `# 调度协议` 章节"
    for bucket in ("进行中", "等你决策", "已完成"):
        assert bucket in body, f"`# 调度协议` 缺少汇总分段：{bucket}"


def test_prompt_has_no_subagent_section():
    assert "# 子 Agent" not in _prompt()


def test_prompt_still_opens_as_secretary():
    text = _prompt()
    first_line = text.splitlines()[0]
    assert first_line.startswith("你是「{{secretary_name}}」")
    assert "私人秘书" in first_line


def test_prompt_documents_dispatch_batch():
    body = _section("# 你的调度能力与硬约束")
    assert body.strip(), "缺少 `# 你的调度能力与硬约束` 章节"
    assert "dispatch_batch" in body, "`dispatch_batch` 已上线，提示词却没提"
    assert "depends_on" in body, "批量派活必须说明 depends_on 的用法"
    assert "8" in body, "批量派活必须写明单批上限 8 条"
    assert "阻断" in body, "批量派活必须说明上游失败时下游被阻断、不自动重试"
    assert "batch_id" in _section("# 调度协议"), "批量任务要按 batch_id 归组汇报"


def test_core_principles_have_no_build_verification_rule():
    body = _section("# 核心原则")
    assert body.strip(), "缺少 `# 核心原则` 章节"
    for phrase in ("编译", "运行检查"):
        assert phrase not in body, (
            f"`# 核心原则` 仍含秘书做不到的验证口径：{phrase}"
        )
    assert "list_dispatches" in body
