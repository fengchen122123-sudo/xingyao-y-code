from __future__ import annotations

import json

import pytest

from core import novel_service
from gui_bridge import ApiBridge

STATUS_CONTRACT_FIELDS = {
    "is_novel",
    "title",
    "target_words",
    "chapters_written",
    "chapters",
    "total_words",
    "today_words",
    "next_chapter",
    "outline_ready",
    "active_foreshadows",
    "last_commit",
}


@pytest.fixture
def bridge():
    return ApiBridge.__new__(ApiBridge)


def _make_novel_project(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / novel_service.NOVEL_MARKER).write_text(
        json.dumps(
            {
                "schema": novel_service.NOVEL_SCHEMA,
                "title": "长夜行",
                "genre": "玄幻",
                "platform": "起点",
                "target_words": 1000000,
                "engine": novel_service.ENGINE_NAME,
                "engine_version": novel_service.ENGINE_VERSION,
                "created_at": "2026-10-28T00:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    for rel in novel_service.NOVEL_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "正文" / "第001章_夜行.md").write_text(
        "# 第001章 夜行\n\n他推门而入，风灌进来。\n",
        encoding="utf-8",
    )
    (root / "大纲" / "细纲_第001章.md").write_text(
        "# 第001章 细纲\n",
        encoding="utf-8",
    )
    return root


def test_get_novel_status_returns_contract_fields(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.get_novel_status(str(project))

    assert result["is_novel"] is True
    assert set(result) == STATUS_CONTRACT_FIELDS
    assert result["title"] == "长夜行"
    assert result["chapters_written"] == 1
    assert result["last_commit"] == 0
    assert result["next_chapter"] == 1
    assert result["outline_ready"] is True
    assert result["active_foreshadows"] == 0
    assert isinstance(result["total_words"], int) and result["total_words"] >= 0
    assert isinstance(result["today_words"], int) and result["today_words"] >= 0

    assert json.loads(json.dumps(result, ensure_ascii=False))["is_novel"] is True


def test_get_novel_status_non_novel_dir(bridge, tmp_path):
    plain = tmp_path / "普通目录"
    plain.mkdir()
    (plain / "README.md").write_text("不是小说项目", encoding="utf-8")

    assert bridge.get_novel_status(str(plain)) == {"is_novel": False}


def test_get_novel_status_missing_workdir(bridge, tmp_path):
    result = bridge.get_novel_status(str(tmp_path / "不存在的小说项目"))

    assert result["is_novel"] is False
    assert result["error"]
    assert "不存在" in result["error"]


@pytest.mark.parametrize("workdir", ["", "   ", "GLOBAL"])
def test_get_novel_status_rejects_blank_or_global(bridge, workdir):
    result = bridge.get_novel_status(workdir)

    assert result["is_novel"] is False
    assert result["error"]


CHAPTER_FILE = "正文/第001章_夜行.md"


def test_get_novel_chapter_returns_content_and_path(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.get_novel_chapter(str(project), 1)

    assert result["ok"] is True
    assert result["chapter"] == 1
    assert result["path"] == CHAPTER_FILE
    original = (project / "正文" / "第001章_夜行.md").read_text(encoding="utf-8")
    assert result["content"] == original
    assert result["visible_chars"] == (
        novel_service.novel_wordcount(text=original)["visible_chars"]
    )
    assert result["visible_chars"] > 0
    assert (project / "正文" / "第001章_夜行.md").read_text(encoding="utf-8") == original


def test_get_novel_chapter_accepts_file_name(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.get_novel_chapter(str(project), "第001章_夜行.md")

    assert result["ok"] is True
    assert result["chapter"] == 1
    assert result["path"] == CHAPTER_FILE


def test_get_novel_chapter_missing_chapter(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.get_novel_chapter(str(project), 9)

    assert result["ok"] is False
    assert "找不到第 9 章" in result["error"]
    assert not (project / "正文" / "第009章_夜行.md").exists()


def test_save_novel_chapter_overwrites_and_reports_words(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")
    new_text = "# 第001章 夜行（修订）\n\n他推门而入，风灌进来，带起一室尘埃。\n"

    result = bridge.save_novel_chapter(str(project), 1, new_text)

    assert result["ok"] is True
    assert result["chapter"] == 1
    assert result["path"] == CHAPTER_FILE
    target = project / "正文" / "第001章_夜行.md"
    assert target.read_text(encoding="utf-8") == new_text
    assert result["visible_chars"] == (
        novel_service.novel_wordcount(text=new_text)["visible_chars"]
    )
    assert novel_service.novel_status(str(project))["total_words"] == result["visible_chars"]
    assert b"\r\n" not in target.read_bytes()


def test_save_novel_chapter_does_not_create_missing_chapter(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.save_novel_chapter(str(project), 9, "不该被写出来的正文")

    assert result["ok"] is False
    assert "找不到第 9 章" in result["error"]
    assert sorted(p.name for p in (project / "正文").iterdir()) == ["第001章_夜行.md"]


def test_novel_chapter_rejects_non_novel_and_blank_workdir(bridge, tmp_path):
    plain = tmp_path / "普通目录"
    plain.mkdir()
    (plain / "README.md").write_text("不是小说项目", encoding="utf-8")

    for result in (
        bridge.get_novel_chapter(str(plain), 1),
        bridge.save_novel_chapter(str(plain), 1, "正文"),
        bridge.get_novel_chapter("", 1),
        bridge.save_novel_chapter("GLOBAL", 1, "正文"),
    ):
        assert result["ok"] is False
        assert result["error"]


def test_list_novel_projects_returns_root_and_projects(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.list_novel_projects(str(project))

    assert result["ok"] is True, result
    assert result["root"] == str(project.resolve())
    assert result["projects"] == [
        {"path": ".", "title": "长夜行", "chapters_written": 1}
    ]
    assert json.loads(json.dumps(result, ensure_ascii=False))["ok"] is True


def test_list_novel_projects_rejects_blank_global_and_missing(bridge, tmp_path):
    for workdir in ("", "   ", "GLOBAL", str(tmp_path / "不存在的工作区")):
        result = bridge.list_novel_projects(workdir)

        assert result["ok"] is False, result
        assert result["error"]
        assert "不存在" in result["error"]
        assert "projects" not in result


def test_export_novel_returns_path_and_chapter_count(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.export_novel(str(project), "md")

    assert result["ok"] is True, result
    assert result["path"] == "导出/长夜行.md"
    assert result["chapters"] == 1
    assert isinstance(result["visible_chars"], int) and result["visible_chars"] > 0
    target = project / "导出" / "长夜行.md"
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("# 长夜行")


def test_export_novel_rejects_invalid_fmt_and_missing_workdir(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    bad_format = bridge.export_novel(str(project), "docx")
    assert bad_format["ok"] is False
    assert "md / txt" in bad_format["error"]
    assert not (project / "导出").exists()

    missing = bridge.export_novel(str(tmp_path / "不存在的小说项目"))
    assert missing["ok"] is False
    assert missing["error"]
    assert "不存在" in missing["error"]


WORKBENCH_CONTRACT_FIELDS = {"is_novel", "foreshadows", "characters", "volumes", "recent_daily"}
WORKBENCH_FORESHADOW_FIELDS = {
    "id", "summary", "status", "importance", "planted_chapter",
    "planned_resolution_chapter", "updated_chapter", "overdue", "planned_overdue",
    "stale_chapters",
}
WORKBENCH_CHARACTER_FIELDS = {
    "name", "identity", "state", "location", "goal", "knowledge",
    "abilities_resources", "open_threads", "relationships",
}
WORKBENCH_VOLUME_FIELDS = {"no", "name", "chars", "current"}


def _write_workbench_tracking(project):
    (project / "追踪" / novel_service.TRACKING_STATE_NAME).write_text(
        json.dumps(
            {
                "last_committed_chapter": 25,
                "foreshadow": {
                    "F001": {
                        "id": "F001", "summary": "玉佩裂纹", "status": "已埋",
                        "importance": "高", "planted_chapter": 3,
                        "planned_resolution_chapter": 21, "updated_chapter": 3,
                    },
                },
                "characters": {
                    "阿七": {"identity": "镖师", "open_threads": ["找回镖车"]},
                    "李昭": {"state": "负伤", "knowledge": ["玉佩来历"]},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (project / "大纲" / "卷纲_第001卷.md").write_text(
        "# 第001卷 卷纲\n\n- 章节范围：第20-30章\n", encoding="utf-8"
    )


def test_get_novel_workbench_returns_contract_structure(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")
    _write_workbench_tracking(project)

    result = bridge.get_novel_workbench(str(project))

    assert result["is_novel"] is True
    assert set(result) == WORKBENCH_CONTRACT_FIELDS
    foreshadow, = result["foreshadows"]
    assert set(foreshadow) == WORKBENCH_FORESHADOW_FIELDS
    assert foreshadow["id"] == "F001"
    assert foreshadow["overdue"] is True
    assert foreshadow["planned_overdue"] is True
    assert foreshadow["stale_chapters"] == 22
    assert {item["name"] for item in result["characters"]} == {"李昭", "阿七"}
    for character in result["characters"]:
        assert set(character) == WORKBENCH_CHARACTER_FIELDS
    volume, = result["volumes"]
    assert set(volume) == WORKBENCH_VOLUME_FIELDS
    assert volume == {"no": 1, "name": "卷纲_第001卷", "chars": volume["chars"], "current": True}
    assert len(result["recent_daily"]) == 7
    assert json.loads(json.dumps(result, ensure_ascii=False))["is_novel"] is True


def test_get_novel_workbench_rejects_non_novel_missing_and_blank(bridge, tmp_path):
    plain = tmp_path / "普通目录"
    plain.mkdir()
    (plain / "README.md").write_text("不是小说项目", encoding="utf-8")

    assert bridge.get_novel_workbench(str(plain)) == {"is_novel": False}

    missing = bridge.get_novel_workbench(str(tmp_path / "不存在的小说项目"))
    assert missing["is_novel"] is False
    assert "不存在" in missing["error"]

    for workdir in ("", "GLOBAL"):
        result = bridge.get_novel_workbench(workdir)
        assert result["is_novel"] is False
        assert result["error"]


def test_read_novel_doc_returns_text_and_chars(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")
    target = project / "大纲" / "卷纲_第001卷.md"
    text = "# 第001卷 卷纲\n\n- 章节范围：第1-30章\n"
    target.write_text(text, encoding="utf-8")

    result = bridge.read_novel_doc(str(project), "大纲/卷纲_第001卷.md")

    assert result["ok"] is True, result
    assert result["text"] == text
    assert result["path"] == "大纲/卷纲_第001卷.md"
    assert result["chars"] == novel_service.novel_wordcount(text=text)["visible_chars"]
    assert result["chars"] > 0
    assert target.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "rel_path",
    ["../xxx.md", "正文/第001章_夜行.md", "大纲/../../xxx.md"],
)
def test_read_novel_doc_rejects_out_of_whitelist_paths(bridge, tmp_path, rel_path):
    project = _make_novel_project(tmp_path / "长夜行")
    (tmp_path / "xxx.md").write_text("项目外的文件\n", encoding="utf-8")

    result = bridge.read_novel_doc(str(project), rel_path)

    assert result["ok"] is False, result
    assert "text" not in result
    assert result["error"]


def test_read_novel_doc_rejects_absolute_path(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.read_novel_doc(str(project), str((project / "大纲" / "细纲_第001章.md").resolve()))

    assert result["ok"] is False
    assert "绝对路径" in result["error"]


def test_read_novel_doc_rejects_non_markdown_and_missing_file(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")
    (project / "大纲" / "节奏表.txt").write_text("不是 md\n", encoding="utf-8")

    not_markdown = bridge.read_novel_doc(str(project), "大纲/节奏表.txt")
    assert not_markdown["ok"] is False
    assert ".md" in not_markdown["error"]

    missing = bridge.read_novel_doc(str(project), "设定/角色/查无此人.md")
    assert missing["ok"] is False
    assert "不存在" in missing["error"]
    assert missing["error"] != not_markdown["error"]


def test_read_novel_doc_rejects_non_novel_missing_and_blank_workdir(bridge, tmp_path):
    plain = tmp_path / "普通目录"
    plain.mkdir()
    (plain / "README.md").write_text("不是小说项目", encoding="utf-8")

    non_novel = bridge.read_novel_doc(str(plain), "大纲/x.md")
    assert non_novel["ok"] is False
    assert non_novel["is_novel"] is False

    for workdir in (str(tmp_path / "不存在的小说项目"), "", "GLOBAL"):
        result = bridge.read_novel_doc(workdir, "大纲/x.md")
        assert result["ok"] is False
        assert "不存在" in result["error"]


AUDIT_RESULT_FIELDS = {"scope", "findings", "summary", "skipped"}


def test_run_novel_audit_returns_ok_with_contract_fields(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.run_novel_audit(str(project))

    assert result["ok"] is True, result
    assert AUDIT_RESULT_FIELDS <= set(result)
    assert result["scope"] == "chapter"
    assert isinstance(result["findings"], list)
    assert isinstance(result["skipped"], list)
    assert set(result["summary"]) == {"S1", "S2", "S3", "S4"}
    assert all(isinstance(count, int) for count in result["summary"].values())


def test_run_novel_audit_passes_scope_and_chapter_through(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    all_scope = bridge.run_novel_audit(str(project), scope="all")
    assert all_scope["ok"] is True, all_scope
    assert all_scope["scope"] == "all"

    one_chapter = bridge.run_novel_audit(str(project), scope="chapter", chapter=1)
    assert one_chapter["ok"] is True, one_chapter
    assert one_chapter["scope"] == "chapter"


@pytest.mark.parametrize("workdir", ["GLOBAL", "", None])
def test_run_novel_audit_rejects_blank_global_workdir(bridge, workdir):
    result = bridge.run_novel_audit(workdir)

    assert result["ok"] is False
    assert "不存在" in result["error"]
    assert "findings" not in result


def test_run_novel_audit_rejects_missing_and_non_novel_dir(bridge, tmp_path):
    plain = tmp_path / "普通目录"
    plain.mkdir()
    (plain / "README.md").write_text("不是小说项目", encoding="utf-8")

    non_novel = bridge.run_novel_audit(str(plain))
    assert non_novel["ok"] is False
    assert non_novel["error"]

    missing = bridge.run_novel_audit(str(tmp_path / "不存在的小说项目"))
    assert missing["ok"] is False
    assert "不存在" in missing["error"]


def test_run_novel_audit_surfaces_invalid_scope_error_from_service(bridge, tmp_path):
    project = _make_novel_project(tmp_path / "长夜行")

    result = bridge.run_novel_audit(str(project), scope="everything")

    service_result = novel_service.novel_audit(str(project), scope="everything")
    assert result["ok"] is False, result
    assert "scope" in result["error"]
    assert result["error"] == service_result["error"]
    assert "findings" not in result


FORESHADOW_TABLE_HEADER = (
    "| ID | 内容 | 埋设章 | 计划回收章 | 状态 | 重要度 | 最近变更章 |\n"
    "|---|---|---:|---:|---|---|---:|\n"
)


def _foreshadow_row(identifier, summary, planted, planned, status, updated):
    planned_cell = f"第{planned}章" if planned else "—"
    return (
        f"| {identifier} | {summary} | 第{planted}章 | {planned_cell} | "
        f"{status} | 高 | 第{updated}章 |\n"
    )


def _make_audit_fix_project(root):
    project = _make_novel_project(root)
    state = {
        "schema": 1,
        "state_revision": 3,
        "last_committed_chapter": 100,
        "foreshadow": {
            "F001": {
                "id": "F001",
                "importance": "高",
                "planned_resolution_chapter": 50,
                "planted_chapter": 55,
                "status": "已埋",
                "summary": "玄真观旧物",
                "updated_chapter": 55,
            },
            "F002": {
                "id": "F002",
                "importance": "高",
                "planned_resolution_chapter": None,
                "planted_chapter": 70,
                "status": "已埋",
                "summary": "接引簿两个活人",
                "updated_chapter": 70,
            },
        },
    }
    tracking = project / "追踪"
    (tracking / "_tracking-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (tracking / "伏笔.md").write_text(
        "# 伏笔当前状态\n\n"
        "> 状态修订：3。每个 ID 只保留一行当前状态；历史变化见 `逐章记录/`。\n\n"
        + FORESHADOW_TABLE_HEADER
        + _foreshadow_row("F001", "玄真观旧物", 55, 50, "已埋", 55)
        + _foreshadow_row("F002", "接引簿两个活人", 70, None, "已埋", 70),
        encoding="utf-8",
    )
    return project


def _overdue_finding(project, identifier="F001"):
    result = novel_service.novel_audit(str(project), scope="all")
    assert result["ok"] is True, result
    for finding in result["findings"]:
        if finding["type"] == "foreshadow_overdue" and identifier in finding["message"]:
            return finding
    raise AssertionError(f"审计没有报出 {identifier} 的超期结论：{result['findings']}")


def test_novel_audit_fix_dry_run_changes_nothing(tmp_path):
    project = _make_audit_fix_project(tmp_path / "长夜行")
    state_path = project / "追踪" / "_tracking-state.json"
    table_path = project / "追踪" / "伏笔.md"
    state_before = state_path.read_text(encoding="utf-8")
    table_before = table_path.read_text(encoding="utf-8")

    result = novel_service.novel_audit_fix(
        str(project), _overdue_finding(project), "defer", dry_run=True
    )

    assert result["ok"] is True, result
    assert result["dry_run"] is True
    assert result["foreshadow_id"] == "F001"
    assert result["before"] == {"planned_resolution_chapter": 50, "updated_chapter": 55}
    assert result["after"] == {"planned_resolution_chapter": None, "updated_chapter": 100}
    assert result["would_change"] is True
    assert state_path.read_text(encoding="utf-8") == state_before
    assert table_path.read_text(encoding="utf-8") == table_before


def test_novel_audit_fix_defer_updates_state_and_one_table_row(tmp_path):
    project = _make_audit_fix_project(tmp_path / "长夜行")
    table_path = project / "追踪" / "伏笔.md"
    table_before = table_path.read_text(encoding="utf-8")

    result = novel_service.novel_audit_fix(
        str(project), _overdue_finding(project), "defer"
    )

    assert result["ok"] is True, result
    assert result["dry_run"] is False
    assert result["after"] == {"planned_resolution_chapter": None, "updated_chapter": 100}

    state = json.loads(
        (project / "追踪" / "_tracking-state.json").read_text(encoding="utf-8")
    )
    row = state["foreshadow"]["F001"]
    assert row["planned_resolution_chapter"] is None
    assert row["updated_chapter"] == 100
    assert row["summary"] == "玄真观旧物"
    assert row["planted_chapter"] == 55
    assert row["status"] == "已埋"

    table_after = table_path.read_text(encoding="utf-8")
    lines_after = table_after.splitlines()
    lines_before = table_before.splitlines()
    assert len(lines_after) == len(lines_before)
    changed = [i for i, (a, b) in enumerate(zip(lines_before, lines_after)) if a != b]
    assert len(changed) == 1
    target = lines_after[changed[0]]
    assert "| F001 |" in target
    assert target.split("|")[4].strip() == "—"
    assert [l for i, l in enumerate(lines_after) if i != changed[0]] == [
        l for i, l in enumerate(lines_before) if i != changed[0]
    ]


def test_novel_audit_fix_reschedule_rejects_past_chapter(tmp_path):
    project = _make_audit_fix_project(tmp_path / "长夜行")
    state_path = project / "追踪" / "_tracking-state.json"
    table_path = project / "追踪" / "伏笔.md"
    state_before = state_path.read_text(encoding="utf-8")
    table_before = table_path.read_text(encoding="utf-8")

    for illegal in (100, 60):
        result = novel_service.novel_audit_fix(
            str(project),
            _overdue_finding(project),
            "reschedule",
            planned_resolution_chapter=illegal,
        )
        assert result["ok"] is False, result
        assert "不能早于" in result["error"]

    assert state_path.read_text(encoding="utf-8") == state_before
    assert table_path.read_text(encoding="utf-8") == table_before


def test_novel_audit_fix_rejects_unsupported_type_without_writing(tmp_path):
    project = _make_audit_fix_project(tmp_path / "长夜行")
    state_path = project / "追踪" / "_tracking-state.json"
    table_path = project / "追踪" / "伏笔.md"
    state_before = state_path.read_text(encoding="utf-8")
    table_before = table_path.read_text(encoding="utf-8")

    finding = {
        "level": "S4",
        "type": "ai_patterns",
        "file": "正文/第001章_夜行.md",
        "message": "第1章命中一级禁用词「仿佛」1 处，按 banned-words.md 应替换",
    }
    result = novel_service.novel_audit_fix(str(project), finding, "defer")

    assert result["ok"] is False, result
    assert result["supported"] is False
    assert "该类型不支持自动修复" in result["error"]
    assert state_path.read_text(encoding="utf-8") == state_before
    assert table_path.read_text(encoding="utf-8") == table_before


def _make_planned_scope_project(root):
    project = _make_novel_project(root)
    state = {
        "schema": 1,
        "state_revision": 1,
        "last_committed_chapter": 100,
        "foreshadow": {
            "F001": {
                "id": "F001",
                "importance": "高",
                "planned_resolution_chapter": 50,
                "planted_chapter": 40,
                "status": "已埋",
                "summary": "青铜钥匙",
                "updated_chapter": 40,
            },
            "F002": {
                "id": "F002",
                "importance": "低",
                "planned_resolution_chapter": None,
                "planted_chapter": 1,
                "status": "已埋",
                "summary": "长线悬念",
                "updated_chapter": 1,
            },
        },
    }
    tracking = project / "追踪"
    (tracking / "_tracking-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (tracking / "伏笔.md").write_text(
        "# 伏笔当前状态\n\n"
        + FORESHADOW_TABLE_HEADER
        + _foreshadow_row("F001", "青铜钥匙", 40, 50, "已埋", 40)
        + _foreshadow_row("F002", "长线悬念", 1, None, "已埋", 1),
        encoding="utf-8",
    )
    return project


def test_audit_foreshadow_overdue_only_counts_planned_rows(tmp_path):
    project = _make_planned_scope_project(tmp_path / "长夜行")

    result = novel_service.novel_audit(str(project), scope="all")

    assert result["ok"] is True, result
    overdue = [f for f in result["findings"] if f["type"] == "foreshadow_overdue"]
    assert len(overdue) == 1, overdue
    assert "F001" in overdue[0]["message"]
    assert "计划第50章回收" in overdue[0]["message"]
    assert "逾期 50 章" in overdue[0]["message"]
    assert result["summary"]["S2"] == 1
    assert "F002" not in json.dumps(overdue, ensure_ascii=False)


def test_audit_fix_defer_clears_overdue_warning(tmp_path):
    project = _make_planned_scope_project(tmp_path / "长夜行")
    finding = _overdue_finding(project, "F001")

    result = novel_service.novel_audit_fix(str(project), finding, "defer")

    assert result["ok"] is True, result
    assert result["after"]["planned_resolution_chapter"] is None
    assert result["still_reported"] is False
    again = novel_service.novel_audit(str(project), scope="all")
    still = [
        f for f in again["findings"]
        if f["type"] == "foreshadow_overdue" and "F001" in f["message"]
    ]
    assert still == []


def test_run_novel_audit_fix_bridge_defer_dry_run(bridge, tmp_path):
    project = _make_planned_scope_project(tmp_path / "长夜行")
    state_path = project / "追踪" / "_tracking-state.json"
    table_path = project / "追踪" / "伏笔.md"
    state_before = state_path.read_text(encoding="utf-8")
    table_before = table_path.read_text(encoding="utf-8")

    result = bridge.run_novel_audit_fix(
        str(project),
        finding=_overdue_finding(project, "F001"),
        action="defer",
        dry_run=True,
    )

    assert result["ok"] is True, result
    assert result["dry_run"] is True
    assert result["foreshadow_id"] == "F001"
    assert result["would_change"] is True
    assert state_path.read_text(encoding="utf-8") == state_before
    assert table_path.read_text(encoding="utf-8") == table_before


@pytest.mark.parametrize("workdir", ["GLOBAL", "", None])
def test_run_novel_audit_fix_rejects_blank_global_workdir(bridge, workdir):
    result = bridge.run_novel_audit_fix(
        workdir, finding={"type": "foreshadow_overdue", "message": "伏笔 F001 计划…"}
    )

    assert result["ok"] is False
    assert "不存在" in result["error"]
    assert "before" not in result
