from __future__ import annotations

import json
from pathlib import Path

from core.project_prompt_suggestions import build_workspace_prompt_suggestions


def test_global_and_missing_workdir_return_empty(tmp_path: Path) -> None:
    assert build_workspace_prompt_suggestions("") == []
    assert build_workspace_prompt_suggestions("GLOBAL") == []
    assert build_workspace_prompt_suggestions("global") == []
    assert build_workspace_prompt_suggestions(str(tmp_path / "missing")) == []


def test_novel_workspace_suggestions_reflect_book_and_chapters(tmp_path: Path) -> None:
    novel_dir = tmp_path / "novel_a"
    novel_dir.mkdir()
    (novel_dir / ".novel.json").write_text(
        json.dumps({"schema": 1, "title": "修仙债主"}, ensure_ascii=False),
        encoding="utf-8",
    )
    body_dir = novel_dir / "正文"
    body_dir.mkdir()
    (body_dir / "第001章_开篇.md").write_text("第一章正文", encoding="utf-8")
    (body_dir / "第002章_入宗.md").write_text("第二章正文", encoding="utf-8")
    track_dir = novel_dir / "追踪"
    track_dir.mkdir()
    (track_dir / "tracking-state.json").write_text("{}", encoding="utf-8")

    items = build_workspace_prompt_suggestions(str(novel_dir))
    assert 2 <= len(items) <= 3
    labels = " | ".join(i["label"] for i in items)
    assert "修仙债主" in labels
    assert "2 章" in labels
    assert items[-1]["id"] == "novel-next"


def test_code_workspace_prefers_git_conflicts_and_dirty_state(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest"}}', encoding="utf-8")
    (tmp_path / "tests").mkdir()

    dirty_items = build_workspace_prompt_suggestions(
        str(tmp_path),
        git_snapshot={
            "branch_name": "dev",
            "staged": [{"path": "a.ts"}],
            "unstaged": [{"path": "b.ts"}],
            "untracked": [],
            "conflicts": [],
            "ahead": 0,
        },
    )
    assert dirty_items[0]["id"] == "git-dirty"
    assert "2 处未提交改动" in dirty_items[0]["label"]
    assert any(item["id"] == "code-tests" for item in dirty_items)

    conflict_items = build_workspace_prompt_suggestions(
        str(tmp_path),
        git_snapshot={
            "branch_name": "dev",
            "staged": [],
            "unstaged": [],
            "untracked": [],
            "conflicts": [{"path": "x.ts", "kind": "both_modified"}],
            "ahead": 0,
        },
    )
    assert conflict_items[0]["id"] == "git-conflicts"
    assert "1 个 Git 合并冲突" in conflict_items[0]["label"]


def test_general_workspace_uses_files_and_recent_session(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_ws"
    empty_dir.mkdir()
    assert build_workspace_prompt_suggestions(str(empty_dir)) == []

    docs_dir = tmp_path / "docs_ws"
    docs_dir.mkdir()
    (docs_dir / "notes.md").write_text("笔记", encoding="utf-8")
    (docs_dir / "plan.txt").write_text("计划", encoding="utf-8")

    items = build_workspace_prompt_suggestions(
        str(docs_dir),
        recent_session_titles=["整理季度汇报重点"],
    )
    ids = [item["id"] for item in items]
    assert "dir-overview" in ids
    assert "dir-docs" in ids
    assert "recent-followup" in ids
