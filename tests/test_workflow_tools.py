"""workflow_list / workflow_run 工具 wrapper 测试。

wrapper 经 core.tool_registry 暴露给模型；这里直接调 wrapper 函数，
用 monkeypatch 把会话工作目录与用户主目录指到 tmp，绝不触碰真实目录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.tool_registry import (
    _workflow_finish_wrapper,
    _workflow_list_wrapper,
    _workflow_run_wrapper,
    _workflow_save_wrapper,
)
from core.workflows.store import workflows_dir, zcode_workflows_dir

_WORKFLOW = """/* zcode-workflow
description: Review changes against a base ref.
whenToUse: Use when the user asks to review changes.
args:
  base:
    type: string
    description: Git ref.
    required: true
  maxRounds:
    type: number
    default: 3
*/
phase("Collect");
const base = String(args.base);
return base;
"""


@pytest.fixture
def workflow_env(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        "core.workspace_context.get_workdir", lambda: root, raising=False
    )
    _write(zcode_workflows_dir("user", str(root), home=home), "pr-review", _WORKFLOW)
    from core.workflows.store import import_from_zcode

    import_from_zcode(str(root), home=home, scopes=("user",))
    return home, root


@pytest.fixture
def run_ledger(tmp_path):
    """注入运行台账（真 MemoryManager + tmp 库）；用例结束摘除进程级句柄。"""
    from core.memory import MemoryManager
    from core.workflows import runs as runs_module

    storage = runs_module.init_run_storage(MemoryManager(str(tmp_path / "runs.db")))
    yield storage
    runs_module._storage = None


def _write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.dwf.ts").write_text(text, encoding="utf-8")


def _error_text(result) -> str:
    assert result.is_error, f"期望失败，实际成功: {result.content[:200]}"
    return result.content


def test_workflow_list_empty_gives_import_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        "core.workspace_context.get_workdir", lambda: tmp_path / "proj", raising=False
    )
    result = _workflow_list_wrapper()
    assert not result.is_error
    assert "没有任何工作流" in result.content
    assert "workflow_save" in result.content


def test_workflow_list_lists_imported_workflow(workflow_env):
    result = _workflow_list_wrapper()
    assert not result.is_error
    assert "pr-review" in result.content
    assert "Review changes" in result.content
    assert "base:string*" in result.content

    detail = _workflow_list_wrapper(name="pr-review")
    assert not detail.is_error
    assert "```typescript" in detail.content
    assert 'phase("Collect");' in detail.content


def test_workflow_list_unknown_name_fails(workflow_env):
    assert "找不到" in _error_text(_workflow_list_wrapper(name="nope"))


def test_workflow_run_validates_args_with_all_errors(workflow_env):
    content = _error_text(_workflow_run_wrapper(name="pr-review", args={"oops": 1}))
    assert "未声明的参数: oops" in content
    assert "缺少必填参数: base" in content


def test_workflow_run_rejects_non_object_args(workflow_env):
    assert "args 必须是对象" in _error_text(_workflow_run_wrapper(name="pr-review", args=["x"]))


def test_workflow_run_returns_directive_with_contract_and_defaults(workflow_env):
    result = _workflow_run_wrapper(name="pr-review", args={"base": "origin/main"})
    assert not result.is_error
    content = result.content
    # 固定映射契约 + 生效参数（default 3 已填充）+ 脚本原文
    assert "工作流转译执行契约" in content
    assert '"base": "origin/main"' in content
    assert '"maxRounds": 3' in content
    assert "run_subagent" in content
    assert "world.run" in content
    assert 'phase("Collect");' in content


def test_workflow_run_unknown_name_lists_available(workflow_env):
    content = _error_text(_workflow_run_wrapper(name="nope"))
    assert "pr-review" in content


def test_workflow_save_creates_workflow_and_skill(workflow_env):
    home, root = workflow_env
    result = _workflow_save_wrapper(
        name="my-flow",
        description="每日构建摘要。",
        script='phase("Summarize");\nconst n = String(args.limit);\nreturn n;\n',
        when_to_use="用户要求构建摘要时",
        args={"limit": {"type": "number", "default": 5}},
        scope="project",
    )
    assert not result.is_error
    saved = root / ".ycode" / "workflows" / "my-flow.dwf.ts"
    assert saved.is_file()
    assert (home / ".ycode" / "skills" / "zcode-workflows" / "my-flow" / "SKILL.md").is_file()

    # 立即可运行：参数校验走声明
    listing = _workflow_list_wrapper()
    assert "my-flow" in listing.content
    run = _workflow_run_wrapper(name="my-flow", args={"limit": "not-a-number"})
    assert run.is_error and "必须是数字" in run.content


def test_workflow_save_duplicate_gates_behind_overwrite(workflow_env):
    _ = _workflow_save_wrapper(
        name="dup", description="d", script="phase(\"X\");\n", scope="project"
    )
    dup = _workflow_save_wrapper(name="dup", description="d2", script="phase(\"Y\");\n")
    assert dup.is_error and "已存在同名" in dup.content
    overwrite = _workflow_save_wrapper(
        name="dup", description="d2", script="phase(\"Y\");\n", overwrite=True
    )
    assert not overwrite.is_error


def test_workflow_save_rejects_bad_names(workflow_env):
    result = _workflow_save_wrapper(name="../escape", description="d", script="x")
    assert result.is_error and "名字" in result.content
    result = _workflow_save_wrapper(name="ok", description="", script="x")
    assert result.is_error and "description" in result.content
    result = _workflow_save_wrapper(name="ok", description="d", script="  ")
    assert result.is_error and "脚本" in result.content


def test_registry_exposes_workflow_tools_in_gui_profile():
    """工具注册进默认（GUI）registry；按用户要求 CLI 档剔除全部工作流工具。"""
    from core.tool_registry import CLI_EXCLUDED_TOOLS, build_default_registry

    registry = build_default_registry()
    for tool_name in ("workflow_list", "workflow_run", "workflow_save", "workflow_finish"):
        assert registry.get(tool_name).name == tool_name
        assert tool_name in CLI_EXCLUDED_TOOLS


def test_workflow_run_registers_run_and_directive_carries_run_info(workflow_env, run_ledger):
    result = _workflow_run_wrapper(name="pr-review", args={"base": "origin/main"})
    assert not result.is_error
    # 台账登记 running；记录的是生效参数（default 已填充）
    runs = run_ledger.list_runs("pr-review")
    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["args"] == {"base": "origin/main", "maxRounds": 3}
    # 指令带运行信息块：run_id / 产物目录 / 收尾要求 / 渐进呈现
    content = result.content
    assert runs[0]["id"] in content
    assert "workflow-artifacts" in content
    assert "workflow_finish" in content
    assert "▶ 阶段" in content
    assert "📦" in content


def test_workflow_finish_closes_run(workflow_env, run_ledger):
    _workflow_run_wrapper(name="pr-review", args={"base": "main"})
    run = run_ledger.list_runs("pr-review")[0]

    finish = _workflow_finish_wrapper(run_id=run["id"], ok=True, summary="2 处发现")
    assert not finish.is_error
    assert run_ledger.get_run(run["id"])["status"] == "completed"

    # 重复收尾：幂等接受，不改写
    again = _workflow_finish_wrapper(run_id=run["id"], ok=False, summary="迟到")
    assert not again.is_error
    assert run_ledger.get_run(run["id"])["status"] == "completed"


def test_workflow_finish_tolerates_unknown_and_missing_ledger(workflow_env, run_ledger):
    unknown = _workflow_finish_wrapper(run_id="wfr-nope", ok=True)
    assert not unknown.is_error  # 未知 run_id 静默接受

    from core.workflows import runs as runs_module

    saved = runs_module._storage
    runs_module._storage = None
    try:
        result = _workflow_finish_wrapper(run_id="wfr-x", ok=True)
        assert not result.is_error
        assert "无需收尾" in result.content  # 台账不可用：不报错不卡模型
    finally:
        runs_module._storage = saved


def test_workflow_run_without_ledger_still_executes(workflow_env):
    """台账未注入（CLI/headless 同态）：指令照常返回，无运行信息块。"""
    result = _workflow_run_wrapper(name="pr-review", args={"base": "main"})
    assert not result.is_error
    assert "run_id" not in result.content
    assert "工作流转译执行契约" in result.content
