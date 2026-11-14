"""GUI 桥层的星流工作流方法测试（get_workflows / get_workflow_detail /
pick 占位 / import_workflows / save_workflow / delete_workflow）。

桥实例用 ``ApiBridge.__new__`` 只装必需属性（gui_bridge 测试既有惯例）；
工作流目录全部指到 tmp（home 由 monkeypatch 接管），绝不触碰真实用户目录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gui_bridge import ApiBridge

_WORKFLOW = """/* zcode-workflow
description: Review changes against a base ref.
args:
  base:
    type: string
    required: true
*/
phase("Collect");
return String(args.base);
"""


def _bridge(shutting_down: bool = False) -> ApiBridge:
    instance = ApiBridge.__new__(ApiBridge)
    instance._shutting_down = shutting_down
    return instance


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("YCODE_DATA_DIR", str(home / ".ycode"))
    return home, root


def test_get_workflows_lists_user_and_project_scopes(env):
    home, root = env
    _write(home / ".ycode" / "workflows" / "shared.dwf.ts", _WORKFLOW)
    _write(root / ".ycode" / "workflows" / "shared.dwf.ts", _WORKFLOW.replace("Review", "Project"))
    _write(home / ".ycode" / "workflows" / "broken.dwf.ts", "no frontmatter")

    result = _bridge().get_workflows(str(root))
    assert result["ok"] is True
    names = [item["name"] for item in result["workflows"]]
    # 项目档遮蔽同名全局档；坏文件进 invalid
    assert names == ["shared"]
    assert result["workflows"][0]["scope"] == "project"
    assert result["workflows"][0]["description"].startswith("Project")
    assert [item["name"] for item in result["invalid"]] == ["broken"]


def test_get_workflow_detail_returns_body_or_friendly_error(env):
    home, root = env
    _write(home / ".ycode" / "workflows" / "pr.dwf.ts", _WORKFLOW)

    result = _bridge().get_workflow_detail("pr", str(root))
    assert result["ok"] is True
    assert 'phase("Collect");' in result["workflow"]["body"]
    assert result["workflow"]["args"]["base"]["required"] is True

    missing = _bridge().get_workflow_detail("nope", str(root))
    assert missing["ok"] is False
    assert "找不到工作流" in missing["msg"]


def test_import_workflows_from_picked_files(env):
    home, root = env
    picked = tmp_dir = env[0].parent / "picked"
    _write(picked / "pr.dwf.ts", _WORKFLOW)
    _write(picked / "bad.txt", "not a workflow")
    _write(picked / "broken.dwf.ts", "garbage")

    bridge = _bridge()
    result = bridge.import_workflows(
        [str(picked / "pr.dwf.ts"), str(picked / "bad.txt"), str(picked / "broken.dwf.ts")],
        str(root),
        "project",
    )
    assert result["ok"] is True
    assert [item["name"] for item in result["imported"]] == ["pr"]
    errors = {item["name"]: item["error"] for item in result["failed"]}
    assert "仅支持" in errors["bad.txt"]
    assert "missing_frontmatter" in errors["broken.dwf.ts"]
    copied = root / ".ycode" / "workflows" / "pr.dwf.ts"
    assert copied.read_text(encoding="utf-8") == _WORKFLOW  # 逐字节复制

    # 同名再导：exists 标记 + overwrite=True 覆盖
    again = bridge.import_workflows([str(picked / "pr.dwf.ts")], str(root), "project")
    assert again["failed"][0]["exists"] is True
    overwrite = bridge.import_workflows([str(picked / "pr.dwf.ts")], str(root), "project", True)
    assert [item["name"] for item in overwrite["imported"]] == ["pr"]


def test_save_and_delete_workflow_roundtrip(env):
    home, root = env
    bridge = _bridge()

    payload = {
        "name": "my-flow",
        "description": "My flow.",
        "when_to_use": "When asked.",
        "args": {"n": {"type": "number", "default": 3}},
        "body": 'phase("Run");\nreturn 1;\n',
        "scope": "project",
        "workspace_root": str(root),
    }
    result = bridge.save_workflow(payload)
    assert result["ok"] is True, result
    saved = root / ".ycode" / "workflows" / "my-flow.dwf.ts"
    text = saved.read_text(encoding="utf-8")
    assert text.startswith("/* zcode-workflow")
    assert 'phase("Run");' in text
    # 技能同步生成
    assert (home / ".ycode" / "skills" / "zcode-workflows" / "my-flow" / "SKILL.md").is_file()

    # 同名不覆盖；overwrite=True 覆盖
    dup = bridge.save_workflow(dict(payload))
    assert dup["ok"] is False and "已存在同名" in dup["errors"][0]
    updated = bridge.save_workflow({**payload, "description": "My flow v2.", "overwrite": True})
    assert updated["ok"] is True

    detail = bridge.get_workflow_detail("my-flow", str(root))
    assert detail["workflow"]["description"] == "My flow v2."

    deleted = bridge.delete_workflow("my-flow", "project", str(root))
    assert deleted["ok"] is True
    assert not saved.exists()
    assert not (home / ".ycode" / "skills" / "zcode-workflows" / "my-flow").exists()

    missing = bridge.delete_workflow("my-flow", "project", str(root))
    assert missing["ok"] is False and "没有工作流" in missing["msg"]


def test_save_rejected_while_shutting_down(env):
    result = _bridge(shutting_down=True).save_workflow(
        {"name": "x", "description": "d", "body": "b", "scope": "user"}
    )
    assert result["ok"] is False


def test_workflow_runs_bridge_roundtrip(env, tmp_path):
    from core.memory import MemoryManager
    from core.workflows import runs as runs_module

    storage = runs_module.init_run_storage(MemoryManager(str(tmp_path / "runs.db")))
    try:
        bridge = _bridge()
        bridge._workflow_run_storage = storage
        run = storage.create_run("pr", scope="project", args={"base": "main"})

        result = bridge.get_workflow_runs("pr")
        assert result["ok"] is True
        assert [r["id"] for r in result["runs"]] == [run["id"]]
        assert result["runs"][0]["args"] == {"base": "main"}

        cleared = bridge.clear_workflow_runs("pr")
        assert cleared["ok"] is True
        assert bridge.get_workflow_runs("pr")["runs"] == []
    finally:
        runs_module._storage = None


def test_workflow_runs_bridge_tolerates_missing_storage(env):
    bridge = _bridge()  # ApiBridge.__new__：不带 _workflow_run_storage 属性
    result = bridge.get_workflow_runs("pr")
    assert result["ok"] is True and result["runs"] == []


def test_results_are_json_serializable(env):
    home, root = env
    picked = home / "pick"
    _write(picked / "pr.dwf.ts", _WORKFLOW)
    bridge = _bridge()
    json.dumps(bridge.import_workflows([str(picked / "pr.dwf.ts")], str(root), "user"))
    json.dumps(
        bridge.save_workflow(
            {"name": "f", "description": "d", "body": "x", "scope": "user"}
        )
    )
