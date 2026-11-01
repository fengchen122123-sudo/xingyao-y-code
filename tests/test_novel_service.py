"""core.novel_service 小说工作区初始化测试（pytest + tmp_path）。

覆盖设计文档 §11 P1 验收的第一条：
novel_init 在空目录生成完整目录树，7 个 Agent 文件可被
core.subagent_registry.parse_agent_md 解析且 name / allowed_tools 符合 §6.1 表格。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from core import novel_service
from core.subagent_registry import parse_agent_md

# 设计文档 §6.1「七个 Agent 的最终配置」：name -> (base_type, model, allowed_tools)
EXPECTED_AGENTS = {
    "story-architect": (
        "general",
        "inherit",
        {"read_file", "glob_files", "search_code", "write_file", "edit_file"},
    ),
    "narrative-writer": (
        "general",
        "inherit",
        {"read_file", "glob_files", "search_code", "write_file", "edit_file", "execute_command"},
    ),
    "character-designer": (
        "general",
        "inherit",
        {"read_file", "glob_files", "search_code", "write_file", "edit_file"},
    ),
    "story-researcher": (
        "general",
        "inherit",
        {"read_file", "glob_files", "search_code", "write_file", "execute_command", "search_news", "fetch_url"},
    ),
    "consistency-checker": (
        "review",
        "secondary",
        {"read_file", "glob_files", "search_code"},
    ),
    "chapter-extractor": (
        "explore",
        "secondary",
        {"read_file", "glob_files", "search_code"},
    ),
    "story-explorer": (
        "explore",
        "secondary",
        {"read_file", "glob_files", "search_code"},
    ),
}

# 设计文档 §4.1 项目目录
EXPECTED_DIRS = ("设定", "设定/角色", "大纲", "正文", "追踪", "追踪/逐章记录", "对标")

# 星瑶 frontmatter 允许的字段（严格单行，其余一律丢弃）
ALLOWED_FRONTMATTER_KEYS = {
    "name", "description", "when_to_use", "base_type", "model", "allowed_tools",
}


@pytest.fixture(autouse=True)
def _require_upstream_story_setup():
    """上游技能包缺失时跳过：属于环境缺依赖，不是用例失败。"""
    agents_dir = novel_service.upstream_agents_dir()
    references_dir = novel_service.upstream_references_dir()
    missing = [str(p) for p in (agents_dir, references_dir) if not p.is_dir()]
    if not missing:
        missing = [
            str(agents_dir / f"{spec['name']}.md")
            for spec in novel_service.AGENT_SPECS
            if not (agents_dir / f"{spec['name']}.md").is_file()
        ]
    if missing:
        pytest.skip(
            "上游 story-setup 技能包未安装，跳过小说工作区初始化用例（缺失："
            + "、".join(missing)
            + "）"
        )


def _read_frontmatter_keys(path: Path) -> list[str]:
    """读原始文件，返回 frontmatter 的 key 列表（用于校验字段收紧）。"""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} 缺少 frontmatter"
    header = text.split("---\n", 2)[1]
    keys = []
    for line in header.splitlines():
        if not line.strip():
            continue
        assert not line.startswith((" ", "\t")), f"{path} frontmatter 出现续行：{line!r}"
        assert line.count(":") >= 1, f"{path} 出现非 key: value 行：{line!r}"
        keys.append(line.partition(":")[0].strip())
    return keys


def test_init_creates_tree_and_marker(tmp_path):
    """空目录初始化：目录齐全，.novel.json 可 json.load 且字段符合 §4.2。"""
    result = novel_service.init_novel_project(
        str(tmp_path),
        "长夜行",
        genre="玄幻",
        platform="起点",
        target_words=1000000,
    )

    assert result["ok"] is True
    assert result["already_initialized"] is False
    assert {rel + "/" for rel in EXPECTED_DIRS}.issubset(set(result["created"]))
    for rel in EXPECTED_DIRS:
        assert (tmp_path / rel).is_dir(), f"缺少目录 {rel}"

    marker = tmp_path / novel_service.NOVEL_MARKER
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert data["title"] == "长夜行"
    assert data["genre"] == "玄幻"
    assert data["platform"] == "起点"
    assert data["target_words"] == 1000000
    assert data["engine"] == "story-ycode"
    assert data["engine_version"] == "1.0.0"
    # ISO8601 UTC：可直接按固定格式解析
    assert isinstance(data["created_at"], str)
    datetime.strptime(data["created_at"], "%Y-%m-%dT%H:%M:%SZ")


def test_agents_parse_and_match_design_table(tmp_path):
    """7 个 Agent 可被星瑶解析器解析，name / base_type / model / allowed_tools 对齐设计表。"""
    novel_service.init_novel_project(str(tmp_path), "长夜行")
    agents_dir = tmp_path / ".ycode" / "agents"

    files = sorted(p.name for p in agents_dir.glob("*.md"))
    assert files == sorted(f"{name}.md" for name in EXPECTED_AGENTS)

    for name, (base_type, model, tools) in EXPECTED_AGENTS.items():
        path = agents_dir / f"{name}.md"
        config = parse_agent_md(str(path))
        assert config is not None, f"{name} 解析失败"
        assert config["name"] == name
        assert config["base_type"] == base_type
        assert config["model"] == model
        assert config["allowed_tools"] == tools

        # description 单行且不超过 200 字
        assert "\n" not in config["description"]
        assert 0 < len(config["description"]) <= 200
        # frontmatter 只保留约定的 6 个字段，且全部单行
        keys = _read_frontmatter_keys(path)
        assert set(keys) == ALLOWED_FRONTMATTER_KEYS

        # 上游写死的 .claude 路径必须改写成项目内 .ycode 路径 + 二级兜底
        body = config["system_prompt"]
        assert ".claude/skills/story-setup" not in body
        assert ".ycode/skills/story-ycode/references/agent-references/" in body
        assert "~/.agents/skills/story-setup/references/agent-references/" in body


def test_agent_references_copied(tmp_path):
    """上游 agent-references 全部 .md（含子目录）复制到项目内资料目录。"""
    novel_service.init_novel_project(str(tmp_path), "长夜行")
    target = (
        tmp_path / ".ycode" / "skills" / "story-ycode" / "references" / "agent-references"
    )
    assert (target / "agent-reference-profiles.md").is_file()
    assert (target / "writing-craft.md").is_file()

    upstream = novel_service.upstream_references_dir()
    upstream_md = sorted(p.relative_to(upstream).as_posix() for p in upstream.rglob("*.md"))
    target_md = sorted(p.relative_to(target).as_posix() for p in target.rglob("*.md"))
    assert target_md == upstream_md


def test_init_is_idempotent(tmp_path):
    """重复初始化：created 为空、项目标记与 Agent 文件不被覆盖。"""
    first = novel_service.init_novel_project(str(tmp_path), "长夜行", genre="玄幻")
    assert first["created"]

    marker = tmp_path / novel_service.NOVEL_MARKER
    marker_before = marker.read_bytes()
    agent_path = tmp_path / ".ycode" / "agents" / "story-architect.md"
    agent_before = agent_path.read_bytes()

    second = novel_service.init_novel_project(str(tmp_path), "另一本书", genre="都市")

    assert second["ok"] is True
    assert second["already_initialized"] is True
    assert second["created"] == []
    assert second["skipped"]
    assert marker.read_bytes() == marker_before
    assert json.loads(marker_before.decode("utf-8"))["title"] == "长夜行"
    assert agent_path.read_bytes() == agent_before


def test_init_without_upstream_uses_bundled_resources(tmp_path, monkeypatch):
    """自包含契约：上游包不可用也能完成部署，Agent 与资料库取自插件自带资源。

    upstream_agents_dir() / upstream_references_dir() 优先用
    plugins/novel_workspace 下的自带副本，只有自带缺失才回退上游技能包，
    因此 STORY_SETUP_ENV 指向不存在的目录不应影响部署结果。
    """
    monkeypatch.setenv(novel_service.STORY_SETUP_ENV, str(tmp_path / "no-such-upstream"))
    root = tmp_path / "book"

    result = novel_service.init_novel_project(str(root), "长夜行")

    assert result["ok"] is True
    assert (root / novel_service.NOVEL_MARKER).is_file()

    agents_dir = root / ".ycode" / "agents"
    deployed = sorted(p.name for p in agents_dir.glob("*.md"))
    assert deployed == sorted(f"{name}.md" for name in EXPECTED_AGENTS)
    assert len(deployed) == 7
    for name in EXPECTED_AGENTS:
        assert (agents_dir / f"{name}.md").is_file(), f"缺少 {name}.md"

    references = (
        root / ".ycode" / "skills" / "story-ycode" / "references" / "agent-references"
    )
    assert (references / "agent-reference-profiles.md").is_file()
    # 自带资源可用时不应出现"上游…缺失"的降级条目
    assert not any("上游" in item for item in result["skipped"])


def test_init_without_bundled_and_upstream_degrades_gracefully(tmp_path, monkeypatch):
    """真降级：自带资源与上游包都缺失时仍建目录与标记，Agent/资料部署进 skipped。"""
    monkeypatch.setattr(novel_service, "bundled_novel_root", lambda: tmp_path / "no-bundled")
    monkeypatch.setenv(novel_service.STORY_SETUP_ENV, str(tmp_path / "no-such-upstream"))
    root = tmp_path / "book"

    result = novel_service.init_novel_project(str(root), "长夜行")

    assert result["ok"] is True
    assert (root / novel_service.NOVEL_MARKER).is_file()
    assert not (root / ".ycode" / "agents").exists()
    assert any("上游" in item for item in result["skipped"])


def _load_plugin_module():
    """按 plugin_loader 的方式从文件加载插件入口模块。"""
    plugin_path = (
        Path(__file__).resolve().parent.parent
        / "plugins" / "novel_workspace" / "plugin.py"
    )
    spec = importlib.util.spec_from_file_location("novel_workspace_plugin_test", plugin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRegistry:
    def __init__(self):
        self.specs = {}

    def register(self, spec):
        self.specs[spec.name] = spec


def test_plugin_registers_novel_init(tmp_path):
    """插件薄壳注册 novel_init：元数据为写操作 + 需授权，执行转发核心实现。"""
    plugin = _load_plugin_module()
    registry = _FakeRegistry()
    plugin.register(registry)

    assert {"novel_init"}.issubset(set(registry.specs))
    spec = registry.specs["novel_init"]
    assert spec.read_only is False
    assert spec.mutates_files is True
    assert spec.requires_permission is True
    assert spec.input_schema["required"] == ["workdir", "title"]

    result = spec.execute(workdir=str(tmp_path / "book"), title="长夜行")
    payload = json.loads(result.content)
    assert payload["ok"] is True
    assert (tmp_path / "book" / novel_service.NOVEL_MARKER).is_file()


# ---------------------------------------------------------------------------
# 第二块：vendor 脚本接入（novel_outline / novel_track）
# ---------------------------------------------------------------------------

def _make_novel_project(root: Path) -> Path:
    """造一个最小小说项目：.novel.json + 大纲/正文/追踪 目录（不依赖上游技能包）。"""
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
    for rel in ("大纲", "正文", "追踪", "追踪/逐章记录"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def _write_outline(root: Path, name: str = "卷纲_第001卷.md") -> Path:
    """写一份带作用域声明的卷纲样例（outline_view.py 的段头约定）。"""
    path = root / "大纲" / name
    path.write_text(
        "\n".join(
            [
                "# 第001卷 卷纲",
                "",
                "> 作用域：卷级常任",
                "## 全卷基调",
                "- 冷峻、克制，不写爽感外溢",
                "",
                "> 作用域：单元级 D1-01",
                "## 剧情单元 D1-01 入城",
                "- 单元ID：D1-01",
                "- 章节范围：第1-3章",
                "- 目标：主角混入商队进城",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _context_block() -> dict:
    """tracking_commit.py 要求的 context.position 等必填字段。"""
    return {
        "position": {
            "volume": "第001卷",
            "volume_start_chapter": 1,
            "story_time": "第一日 黄昏",
            "scene": "北城门",
        },
        "long_term_constraints": [],
        "active_character_names": [],
        "continuity_risks": [],
    }


def _init_document() -> dict:
    """tracking_commit.py init 子命令的最小合法输入文档。"""
    context = _context_block()
    context["recent_chapters"] = []
    context["next_chapter_commitments"] = []
    return {
        "schema_version": 1,
        "book_title": "长夜行",
        "last_chapter": 0,
        "context": context,
        "character_snapshots": {},
        "foreshadow": [],
        "timeline_events": [],
    }


def _append_document(chapter: int = 1, expected_revision: int = 0) -> dict:
    """tracking_commit.py commit 子命令的最小合法 append 事务文档。"""
    return {
        "schema_version": 1,
        "mode": "append",
        "chapter": chapter,
        "chapter_title": "夜行",
        "expected_state_revision": expected_revision,
        "delta": {
            "result": "主角抵达城门并混入车队",
            "character_changes": [],
            "foreshadow_changes": [],
            "timeline_events": [],
            "constraints": [],
            "next_chapter_commitments": ["进城找客栈"],
            "retired_context_items": [],
            "retired_characters": [],
        },
        "context": _context_block(),
        "character_snapshots": {},
    }


def test_track_init_creates_state_file(tmp_path):
    """novel_track(init) 建立追踪状态文件与派生视图，返回脚本 JSON。"""
    root = _make_novel_project(tmp_path / "book")

    result = novel_service.novel_track(str(root), "init", _init_document())

    assert result["ok"] is True, result
    assert result["last_committed_chapter"] == 0
    assert result["state_revision"] == 0
    state_file = root / "追踪" / "_tracking-state.json"
    assert state_file.is_file()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_committed_chapter"] == 0
    assert (root / "追踪" / "上下文.md").is_file()
    # 事务临时文件不留在项目里
    assert not list(root.glob("novel-track-*.json"))


def test_track_duplicate_append_is_rejected(tmp_path):
    """同章重复 append 返回错误，且不覆盖已提交的逐章记录。"""
    root = _make_novel_project(tmp_path / "book")
    assert novel_service.novel_track(str(root), "init", _init_document())["ok"] is True

    first = novel_service.novel_track(str(root), "append", _append_document(1, 0))
    assert first["ok"] is True, first
    assert first["last_committed_chapter"] == 1
    delta_file = root / "追踪" / "逐章记录" / "第001章.md"
    assert delta_file.is_file()
    delta_before = delta_file.read_bytes()

    # 修订号已前进到 1，再交一次第 1 章的 append：脚本按章号守卫拒绝
    second = novel_service.novel_track(str(root), "append", _append_document(1, 1))
    assert second["ok"] is False
    assert "append chapter must be 2" in (second["error"] + second["stderr"])
    assert delta_file.read_bytes() == delta_before
    state = json.loads((root / "追踪" / "_tracking-state.json").read_text(encoding="utf-8"))
    assert state["last_committed_chapter"] == 1


def test_track_rejects_invalid_mode(tmp_path):
    """mode 只接受 init / append / revision，非法值在调脚本前直接报错。"""
    root = _make_novel_project(tmp_path / "book")

    result = novel_service.novel_track(str(root), "commit", {"schema_version": 1})

    assert result["ok"] is False
    assert "mode 只接受" in result["error"]
    assert not (root / "追踪" / "_tracking-state.json").exists()


def test_outline_returns_unit_section(tmp_path):
    """novel_outline 取到指定单元的卷纲段文本（含卷级常任）。"""
    root = _make_novel_project(tmp_path / "book")
    _write_outline(root)

    result = novel_service.novel_outline(str(root), unit_id="D1-01", stage="write")

    assert result["ok"] is True, result
    assert "剧情单元 D1-01 入城" in result["text"]
    assert "全卷基调" in result["text"]
    assert "冷峻" in result["text"]


def test_outline_unknown_unit_returns_error(tmp_path):
    """单元不存在时返回 ok=False，并带回脚本 stderr（不静默降级）。"""
    root = _make_novel_project(tmp_path / "book")
    _write_outline(root)

    result = novel_service.novel_outline(str(root), unit_id="D9-99")

    assert result["ok"] is False
    assert "找不到单元 D9-99" in (result["error"] + result["stderr"])


def test_outline_and_track_require_novel_marker(tmp_path):
    """无 .novel.json 时两个工具都返回 is_novel=False，不抛异常。"""
    plain = tmp_path / "plain"
    plain.mkdir()

    for result in (
        novel_service.novel_outline(str(plain)),
        novel_service.novel_track(str(plain), "init", _init_document()),
    ):
        assert result["ok"] is False
        assert result["is_novel"] is False


def test_vendor_scripts_for_outline_and_track_match_lock():
    """三个脚本存在，VERSION.lock 可解析，sha256 与实际文件逐一致。"""
    vendor = novel_service.vendor_dir()
    lock_path = vendor / novel_service.VERSION_LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    assert lock["source"].startswith("https://github.com/")
    assert lock["skill"] == "story-long-write"
    assert lock["fetched_at"].endswith("Z")
    assert set(lock["files"]) == set(novel_service.VENDOR_SCRIPTS)

    for name, expected in lock["files"].items():
        path = vendor / name
        assert path.is_file(), f"缺少 vendored 脚本 {name}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == expected, f"{name} 哈希与 VERSION.lock 不一致"

    license_text = (vendor / "LICENSE-oh-story").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "oh-story-claudecode" in license_text


def test_missing_vendor_script_outline_and_track_return_error(tmp_path, monkeypatch):
    """vendor 目录被移走/脚本缺失时返回结构化错误，不抛异常。"""
    root = _make_novel_project(tmp_path / "book")
    _write_outline(root)
    monkeypatch.setenv(novel_service.VENDOR_ENV, str(tmp_path / "no-vendor"))

    outline = novel_service.novel_outline(str(root), unit_id="D1-01")
    assert outline["ok"] is False
    assert "vendored 脚本缺失" in outline["error"]
    assert "outline_view.py" in outline["error"]

    track = novel_service.novel_track(str(root), "init", _init_document())
    assert track["ok"] is False
    assert "vendored 脚本缺失" in track["error"]
    assert "tracking_commit.py" in track["error"]
    assert not (root / "追踪" / "_tracking-state.json").exists()


def test_plugin_registers_outline_and_track_tools(tmp_path):
    """插件注册 novel_outline（只读）与 novel_track（写操作 + 需授权）。"""
    plugin = _load_plugin_module()
    registry = _FakeRegistry()
    plugin.register(registry)

    assert {"novel_init", "novel_outline", "novel_track"}.issubset(set(registry.specs))

    outline_spec = registry.specs["novel_outline"]
    assert outline_spec.read_only is True
    assert outline_spec.mutates_files is False
    assert outline_spec.requires_permission is False
    assert outline_spec.input_schema["required"] == ["workdir"]
    assert outline_spec.input_schema["properties"]["stage"]["enum"] == ["outline", "write"]

    track_spec = registry.specs["novel_track"]
    assert track_spec.read_only is False
    assert track_spec.mutates_files is True
    assert track_spec.requires_permission is True
    assert track_spec.side_effect_scope == "workspace"
    assert track_spec.input_schema["required"] == ["workdir", "mode", "transaction"]
    assert track_spec.input_schema["properties"]["mode"]["enum"] == ["init", "append", "revision"]

    # 工具执行体转发核心实现：非小说目录返回 is_novel=False，且标记为错误结果
    result = outline_spec.execute(workdir=str(tmp_path / "plain"))
    assert result.is_error is True
    assert json.loads(result.content)["is_novel"] is False


# ---------------------------------------------------------------------------
# 第三块：只读工具（novel_status / novel_wordcount）
# ---------------------------------------------------------------------------

def _vendor_wordcount():
    """按文件路径加载 vendored wordcount_core，作为独立口径参照。"""
    path = novel_service.vendor_script("wordcount_core.py")
    assert path is not None, "vendored wordcount_core.py 缺失"
    spec = importlib.util.spec_from_file_location("test_wordcount_core", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_chapter(root: Path, chapter: int, name: str, text: str) -> Path:
    """按 第NNN章_章名.md 契约写一章正文。"""
    path = root / "正文" / f"第{chapter:03d}章_{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_status_counts_chapters_and_total_words(tmp_path):
    """3 章样例：章节数与可见字数与 wordcount_core 直接调用一致，顺带校验追踪字段。"""
    root = _make_novel_project(tmp_path / "book")
    texts = {
        1: "# 第001章 夜行\n\n风起。\n",
        2: "正文甲乙丙丁\n第二行",
        3: "---\n字数目标: 1000\n---\n# 第003章 黎明\n终章。\n",
    }
    for number, text in texts.items():
        _write_chapter(root, number, f"测试章{number}", text)

    # 追踪状态：已提交 3 章；两条伏笔一活跃一回收
    (root / "追踪" / novel_service.TRACKING_STATE_NAME).write_text(
        json.dumps(
            {
                "last_committed_chapter": 3,
                "foreshadow": {
                    "F001": {"id": "F001", "status": "已埋"},
                    "F002": {"id": "F002", "status": "已回收"},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "大纲" / "细纲_第004章.md").write_text("# 第004章 细纲\n", encoding="utf-8")

    status = novel_service.novel_status(str(root))
    wordcount = _vendor_wordcount()

    assert status["is_novel"] is True
    assert status["title"] == "长夜行"
    assert status["chapters_written"] == 3
    assert status["total_words"] == sum(
        wordcount.count_visible_chars(text) for text in texts.values()
    )
    assert status["total_words"] > 0
    assert status["last_commit"] == 3
    assert status["next_chapter"] == 4
    assert status["active_foreshadows"] == 1
    assert status["outline_ready"] is True


def test_status_today_words_uses_mtime(tmp_path):
    """今日字数只统计 mtime 落在本地当天的章节。"""
    root = _make_novel_project(tmp_path / "book")
    _write_chapter(root, 1, "今日", "今日码字三千")
    old_path = _write_chapter(root, 2, "旧章", "旧章内容不同")
    old_timestamp = datetime(2024, 1, 1, 12, 0, 0).timestamp()
    os.utime(old_path, (old_timestamp, old_timestamp))

    status = novel_service.novel_status(str(root))
    wordcount = _vendor_wordcount()

    assert status["today_words"] == wordcount.count_visible_chars("今日码字三千")
    assert status["today_words"] > 0
    assert status["total_words"] == (
        status["today_words"] + wordcount.count_visible_chars("旧章内容不同")
    )


def test_status_non_novel_returns_flag_only(tmp_path):
    """非小说项目只返回 {"is_novel": False}，不带任何多余字段。"""
    plain = tmp_path / "plain"
    plain.mkdir()

    assert novel_service.novel_status(str(plain)) == {"is_novel": False}


def test_wordcount_text_and_file_agree(tmp_path):
    """同一段文本，直接传 text 与传 workdir+chapter 读文件结果一致。"""
    root = _make_novel_project(tmp_path / "book")
    text = "---\n字数目标: 1000\n---\n# 第002章 夜行\n可见字数统计ABC 123。\n"
    _write_chapter(root, 2, "夜行", text)

    by_text = novel_service.novel_wordcount(text=text)
    by_file = novel_service.novel_wordcount(workdir=str(root), chapter=2)
    wordcount = _vendor_wordcount()

    assert by_text["ok"] is True
    assert by_file["ok"] is True
    assert by_text["chapter"] is None
    assert by_file["chapter"] == 2
    assert by_text["visible_chars"] == by_file["visible_chars"]
    assert by_text["visible_chars"] == wordcount.count_visible_chars(text)
    # 两个入参都缺：参数错误而不是抛异常
    assert novel_service.novel_wordcount()["ok"] is False


def test_status_empty_project_defaults(tmp_path):
    """空项目：next_chapter=1、last_commit=0、active_foreshadows=0，字段严格对齐契约。"""
    root = _make_novel_project(tmp_path / "book")

    status = novel_service.novel_status(str(root))

    assert set(status) == {
        "is_novel", "title", "target_words", "chapters_written", "chapters",
        "total_words", "today_words", "next_chapter", "outline_ready",
        "active_foreshadows", "last_commit",
    }
    assert status["is_novel"] is True
    assert status["target_words"] == 1000000
    assert status["chapters"] == []
    assert status["chapters_written"] == 0
    assert status["total_words"] == 0
    assert status["today_words"] == 0
    assert status["last_commit"] == 0
    assert status["next_chapter"] == 1
    assert status["active_foreshadows"] == 0
    assert status["outline_ready"] is False


# ---------------------------------------------------------------------------
# 第四块：确定性审计（novel_audit）
# ---------------------------------------------------------------------------

def _write_tracking_state(root: Path, payload: dict) -> Path:
    """直接写 追踪/_tracking-state.json（审计只读状态，不依赖脚本校验）。"""
    path = root / "追踪" / novel_service.TRACKING_STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_audit_tracking_state_mismatch_returns_s1(tmp_path):
    """追踪最近提交章（1）落后于正文最大章号（2）→ tracking_state 记 S1。"""
    root = _make_novel_project(tmp_path / "book")
    _write_chapter(root, 1, "夜行", "# 第001章 夜行\n\n风起。\n")
    _write_chapter(root, 2, "入城", "# 第002章 入城\n\n雨落。\n")
    _write_tracking_state(root, {"last_committed_chapter": 1})

    result = novel_service.novel_audit(str(root), scope="all")

    assert result["ok"] is True, result
    assert result["scope"] == "all"
    assert set(result["summary"]) == {"S1", "S2", "S3", "S4"}
    tracked = [f for f in result["findings"] if f["type"] == "tracking_state"]
    assert len(tracked) == 1, result["findings"]
    assert tracked[0]["level"] == "S1"
    assert tracked[0]["file"] == "追踪/_tracking-state.json"
    assert "第1章" in tracked[0]["message"] and "第2章" in tracked[0]["message"]
    assert result["summary"]["S1"] == 1
    # 状态对齐后不再报 S1
    _write_tracking_state(root, {"last_committed_chapter": 2})
    assert novel_service.novel_audit(str(root), scope="all")["summary"]["S1"] == 0


def test_audit_foreshadow_overdue_returns_s2(tmp_path):
    """伏笔第3章埋设、最近提交到第25章（差 22 章 > 20）→ 该伏笔记 S2。"""
    root = _make_novel_project(tmp_path / "book")
    _write_chapter(root, 25, "远行", "# 第025章 远行\n\n雪停了。\n")
    _write_tracking_state(root, {
        "last_committed_chapter": 25,
        "foreshadow": {
            "F001": {
                "id": "F001",
                "summary": "玉佩裂纹",
                "planted_chapter": 3,
                "planned_resolution_chapter": None,
                "status": "已埋",
                "importance": "高",
                "updated_chapter": 3,
            },
            # 差 20 章：正好等于阈值，不应报
            "F002": {
                "id": "F002",
                "summary": "旧信",
                "planted_chapter": 5,
                "planned_resolution_chapter": 30,
                "status": "已埋",
                "importance": "中",
                "updated_chapter": 5,
            },
            # 已回收：不报
            "F003": {
                "id": "F003",
                "summary": "断刀",
                "planted_chapter": 2,
                "planned_resolution_chapter": 10,
                "status": "已回收",
                "importance": "低",
                "updated_chapter": 10,
            },
        },
    })

    result = novel_service.novel_audit(str(root), scope="all")

    assert result["ok"] is True, result
    overdue = [f for f in result["findings"] if f["type"] == "foreshadow_overdue"]
    assert [f["level"] for f in overdue] == ["S2"], result["findings"]
    assert "F001" in overdue[0]["message"]
    assert result["summary"]["S2"] == 1
    assert result["summary"]["S1"] == 0


def test_audit_non_novel_project_returns_is_novel_false(tmp_path):
    """非小说项目：返回 ok=False + is_novel=False，不带 findings。"""
    plain = tmp_path / "plain"
    plain.mkdir()

    result = novel_service.novel_audit(str(plain))

    assert result["ok"] is False
    assert result["is_novel"] is False
    assert "error" in result
    assert "findings" not in result


def test_audit_ai_patterns_flags_banned_words_and_formulas(tmp_path):
    """AI 味检查为本地确定性扫描：禁用词、套句式、章末预告各出 S4，不进 skipped。"""
    root = _make_novel_project(tmp_path / "book")
    _write_chapter(
        root,
        1,
        "试写",
        "# 第001章 试写\n\n"
        "他仿佛看见了什么。她仿佛听见了什么。\n\n"
        "他不是冷漠，而是绝望。他不是退让，而是等待。\n\n"
        "他不知道的是，更大的风暴即将来临。\n",
    )

    result = novel_service.novel_audit(str(root))

    assert result["ok"] is True, result
    assert result["scope"] == "chapter"
    findings = [f for f in result["findings"] if f["type"] == "ai_patterns"]
    assert findings, result
    assert {f["level"] for f in findings} == {"S4"}
    assert {f["file"] for f in findings} == {"正文/第001章_试写.md"}
    messages = " ".join(f["message"] for f in findings)
    assert "仿佛" in messages
    assert "不是A，而是B" in messages
    assert "他不知道的是" in messages
    # 检查已真正执行：不再以 skipped 冒充通过
    assert not [s for s in result["skipped"] if s["check"] == "ai_patterns"]
    # 空项目没有追踪状态：tracking_state 记 S3，与 AI 味检查互不影响
    assert result["summary"]["S3"] >= 1


def test_audit_rejects_invalid_scope_and_checks(tmp_path):
    """scope 只接受 chapter / all，checks 只接受 auto；非法值在扫描前直接报错。"""
    root = _make_novel_project(tmp_path / "book")

    bad_scope = novel_service.novel_audit(str(root), scope="volume")
    assert bad_scope["ok"] is False
    assert "scope 只接受" in bad_scope["error"]

    bad_checks = novel_service.novel_audit(str(root), checks="ai_patterns")
    assert bad_checks["ok"] is False
    assert "checks 只接受" in bad_checks["error"]

    # 指定章的正文不存在：报参数错误而不是静默返回空结论
    missing_chapter = novel_service.novel_audit(str(root), scope="chapter", chapter=99)
    assert missing_chapter["ok"] is False
    assert "第 99 章" in missing_chapter["error"]


# ---------------------------------------------------------------------------
# 第五块：多书发现与导出（P3-B）
# ---------------------------------------------------------------------------

def _retitle(root: Path, title: str) -> Path:
    """改写 .novel.json 的 title，用来区分同工作区里的多本书。"""
    marker = root / novel_service.NOVEL_MARKER
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["title"] = title
    marker.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return marker


def test_discover_self_is_novel_project(tmp_path):
    """工作区自身是小说项目：只回一条，path 为 "."，章数只按正文文件数统计。"""
    root = _make_novel_project(tmp_path)
    _write_chapter(root, 1, "夜行", "# 第001章 夜行\n\n风起。\n")
    _write_chapter(root, 2, "入城", "# 第002章 入城\n\n雨落。\n")
    # 不合章命名契约的文件不计入章节数（只数不读）
    (root / "正文" / "笔记.md").write_text("随笔，不是章节。\n", encoding="utf-8")

    result = novel_service.discover_novel_projects(str(root))

    assert result["ok"] is True, result
    assert Path(result["root"]) == root
    assert result["projects"] == [
        {"path": ".", "title": "长夜行", "chapters_written": 2}
    ]


def test_discover_lists_books_in_subdirs_and_stops_at_marker(tmp_path):
    """工作区不是小说项目：扫出 a / b 两本；命中 .novel.json 后不再向下钻。"""
    workspace = tmp_path / "工作区"
    workspace.mkdir()
    (workspace / "README.md").write_text("普通工作区\n", encoding="utf-8")

    book_a = _make_novel_project(workspace / "a")
    _retitle(book_a, "甲书")
    _write_chapter(book_a, 1, "夜行", "# 第001章 夜行\n\n风起。\n")
    book_b = _make_novel_project(workspace / "b")
    _retitle(book_b, "乙书")
    _write_chapter(book_b, 1, "入城", "# 第001章 入城\n\n雨落。\n")
    _write_chapter(book_b, 2, "黎明", "# 第002章 黎明\n\n雪停。\n")
    # 书内部再放一本"书"（深度 2，仍在扫描范围内）：命中 a 后不应再往下钻
    nested = book_a / "参考"
    nested.mkdir(parents=True)
    (nested / novel_service.NOVEL_MARKER).write_text(
        json.dumps({"schema": novel_service.NOVEL_SCHEMA, "title": "误判书"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = novel_service.discover_novel_projects(str(workspace))

    assert result["ok"] is True, result
    assert [
        (item["path"], item["title"], item["chapters_written"])
        for item in result["projects"]
    ] == [("a", "甲书", 1), ("b", "乙书", 2)]
    assert "误判书" not in [item["title"] for item in result["projects"]]


def test_discover_skips_hidden_and_export_dirs(tmp_path):
    """隐藏目录与「导出」目录里的 .novel.json 不收录；不存在的目录回结构化错误。"""
    workspace = tmp_path / "工作区"
    workspace.mkdir()
    for name in (".hidden", "导出"):
        skipped = workspace / name
        skipped.mkdir(parents=True)
        (skipped / novel_service.NOVEL_MARKER).write_text(
            json.dumps(
                {"schema": novel_service.NOVEL_SCHEMA, "title": f"不该出现-{name}"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    book = _make_novel_project(workspace / "真书")
    _retitle(book, "真书")
    _write_chapter(book, 1, "夜行", "# 第001章 夜行\n\n风起。\n")

    result = novel_service.discover_novel_projects(str(workspace))

    assert result["ok"] is True, result
    assert [(item["path"], item["title"]) for item in result["projects"]] == [("真书", "真书")]

    missing = novel_service.discover_novel_projects(str(tmp_path / "不存在的目录"))
    assert missing["ok"] is False
    assert "不存在" in missing["error"]


def test_discover_empty_workspace_returns_empty_list(tmp_path):
    """空工作区不是错误：ok=True 且 projects 为空数组。"""
    empty = tmp_path / "空工作区"
    empty.mkdir()

    result = novel_service.discover_novel_projects(str(empty))

    assert result["ok"] is True, result
    assert result["projects"] == []


def test_export_md_keeps_body_and_orders_chapters(tmp_path):
    """md 导出：产物落在 导出/书名.md，保留正文原文、按章号升序、带书名标题。"""
    root = _make_novel_project(tmp_path / "book")
    _write_chapter(root, 1, "夜行", "# 第001章 夜行\n\n他推门而入，风灌进来。\n")
    _write_chapter(root, 2, "入城", "# 第002章 入城\n\n雨落在青石板上。\n")

    result = novel_service.export_novel(str(root))

    assert result["ok"] is True, result
    assert result["path"] == "导出/长夜行.md"
    assert result["chapters"] == 2
    target = root / "导出" / "长夜行.md"
    assert target.is_file()

    text = target.read_text(encoding="utf-8")
    assert text.startswith("# 长夜行")
    assert "他推门而入，风灌进来。" in text
    assert "雨落在青石板上。" in text
    assert text.index("第001章") < text.index("第002章")
    # 可见字数与 novel_status 同口径，不另算一套
    assert result["visible_chars"] == novel_service.novel_status(str(root))["total_words"]
    assert result["visible_chars"] > 0


def test_export_txt_strips_frontmatter_and_heading_marks(tmp_path):
    """txt 导出：frontmatter 块与行首 # 标题被去掉，只留正文文字。"""
    root = _make_novel_project(tmp_path / "book")
    _write_chapter(
        root,
        1,
        "夜行",
        "---\n字数目标: 1000\n---\n# 第001章 夜行\n\n他推门而入，风灌进来。\n",
    )

    result = novel_service.export_novel(str(root), "txt")

    assert result["ok"] is True, result
    assert result["path"] == "导出/长夜行.txt"
    text = (root / "导出" / "长夜行.txt").read_text(encoding="utf-8")
    assert "---" not in text
    assert "字数目标" not in text
    assert "他推门而入，风灌进来。" in text
    assert "第001章 夜行" in text
    assert not [line for line in text.splitlines() if line.lstrip().startswith("#")]


def test_export_rejects_invalid_format_and_empty_body(tmp_path):
    """非法 fmt 报「只接受 md / txt」；正文为空时报错且不生成导出文件。"""
    root = _make_novel_project(tmp_path / "book")
    _write_chapter(root, 1, "夜行", "# 第001章 夜行\n\n风起。\n")

    bad_format = novel_service.export_novel(str(root), "pdf")
    assert bad_format["ok"] is False
    assert "md / txt" in bad_format["error"]
    assert not (root / "导出").exists()

    empty_root = _make_novel_project(tmp_path / "empty_book")
    empty = novel_service.export_novel(str(empty_root))
    assert empty["ok"] is False
    assert "正文目录没有可导出的章节" in empty["error"]
    assert not (empty_root / "导出").exists()

    # 非小说项目：返回 is_novel=False，同样不落任何文件
    plain = tmp_path / "plain"
    plain.mkdir()
    non_novel = novel_service.export_novel(str(plain))
    assert non_novel["ok"] is False
    assert non_novel["is_novel"] is False
    non_novel = novel_service.export_novel(str(plain))
    assert non_novel["ok"] is False
    assert non_novel["is_novel"] is False
    assert not (plain / "导出").exists()


# ---------------------------------------------------------------------------
# 第五块：novel_status 透出 target_words / chapters（前端逐章字数）
# ---------------------------------------------------------------------------

def _set_marker_field(root: Path, **fields) -> None:
    """改写 .novel.json 的指定字段，其余键保持原样。"""
    path = root / novel_service.NOVEL_MARKER
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_status_exposes_target_words_and_chapters(tmp_path):
    """目标字数从 marker 透出；chapters 的章号/章名/字数/提交态/细纲态逐章对齐。"""
    root = _make_novel_project(tmp_path / "book")
    _set_marker_field(root, target_words=750000)
    texts = {
        1: "# 第001章 夜行\n\n风起。\n",
        2: "正文甲乙丙丁\n第二行",
        3: "---\n字数目标: 1000\n---\n# 第003章 黎明\n终章。\n",
    }
    for number, text in texts.items():
        _write_chapter(root, number, f"测试章{number}", text)
    (root / "大纲" / "细纲_第001章.md").write_text("# 第001章 细纲\n", encoding="utf-8")
    _write_tracking_state(root, {"last_committed_chapter": 2})
    wordcount = _vendor_wordcount()

    status = novel_service.novel_status(str(root))

    assert status["target_words"] == 750000
    chapters = status["chapters"]
    assert [item["number"] for item in chapters] == [1, 2, 3]
    assert [item["name"] for item in chapters] == [
        "第001章_测试章1",
        "第002章_测试章2",
        "第003章_测试章3",
    ]
    for item in chapters:
        number = item["number"]
        assert item["words"] == wordcount.count_visible_chars(texts[number])
        # 与 novel_wordcount 的文件口径一致，不另算一套
        assert item["words"] == novel_service.novel_wordcount(
            workdir=str(root), chapter=number
        )["visible_chars"]
        assert item["submitted"] == (number <= 2)
        assert item["outline_ready"] == (number == 1)
    # 无 tracking actual 时，逐章字数之和就是原有 total_words
    assert status["total_words"] == sum(item["words"] for item in chapters)


def test_status_chapter_words_prefer_tracking_actual(tmp_path):
    """追踪记录 actual 存在时优先于现算可见字数；total_words 仍是原口径。"""
    root = _make_novel_project(tmp_path / "book")
    first_text = "短正文"
    second_text = "第二章正文也不长"
    _write_chapter(root, 1, "夜行", first_text)
    _write_chapter(root, 2, "入城", second_text)
    _write_tracking_state(
        root,
        {
            "last_committed_chapter": 1,
            "wordcount_records": {
                "1": {
                    "actual": 2777,
                    "target": 2500,
                    "status": "ok",
                    "metric": "visible_chars_v1",
                    "body_sha256": "0" * 64,
                },
                # actual 非正数视为不可用，回落现算值
                "2": {"actual": 0, "target": 2500},
            },
        },
    )
    wordcount = _vendor_wordcount()

    status = novel_service.novel_status(str(root))
    first, second = status["chapters"]

    assert first["words"] == 2777
    assert second["words"] == wordcount.count_visible_chars(second_text)
    assert status["total_words"] == (
        wordcount.count_visible_chars(first_text)
        + wordcount.count_visible_chars(second_text)
    )


def test_status_keeps_unreadable_chapter_with_zero_words(tmp_path, monkeypatch):
    """正文读不出时该章仍出现在 chapters 里，words 为 0，不整章消失。"""
    root = _make_novel_project(tmp_path / "book")
    _write_chapter(root, 1, "夜行", "可见正文")
    _write_chapter(root, 2, "空章", "")
    original_read = novel_service._read_body_text

    def fake_read(path: Path):
        if path.name.startswith("第002章"):
            return None
        return original_read(path)

    monkeypatch.setattr(novel_service, "_read_body_text", fake_read)

    status = novel_service.novel_status(str(root))
    first, second = status["chapters"]

    assert status["chapters_written"] == 2
    assert [first["number"], second["number"]] == [1, 2]
    assert second["name"] == "第002章_空章"
    assert second["words"] == 0
    assert first["words"] == _vendor_wordcount().count_visible_chars("可见正文")
    assert status["total_words"] == first["words"]


def test_status_chapter_volume_only_when_outline_range_reliable(tmp_path):
    """卷纲声明章号范围且归属唯一时才填 volume；范围重叠则整项省略。"""
    root = _make_novel_project(tmp_path / "book")
    _write_outline(root)  # 卷纲_第001卷.md：单元 D1-01 声明「章节范围：第1-3章」
    for number in (1, 2, 3, 4):
        _write_chapter(root, number, f"测试章{number}", f"第{number}章正文")

    status = novel_service.novel_status(str(root))
    by_number = {item["number"]: item for item in status["chapters"]}

    assert [by_number[number]["volume"] for number in (1, 2, 3)] == [1, 1, 1]
    assert "volume" not in by_number[4]

    # 第二卷声明与第一卷重叠：2、3 章归属不可靠 → 省略；4 章唯一归属第二卷
    (root / "大纲" / "卷纲_第002卷.md").write_text(
        "# 第002卷 卷纲\n\n- 章节范围：第2-5章\n", encoding="utf-8"
    )

    status = novel_service.novel_status(str(root))
    by_number = {item["number"]: item for item in status["chapters"]}

    assert by_number[1]["volume"] == 1
    assert "volume" not in by_number[2]
    assert "volume" not in by_number[3]
    assert by_number[4]["volume"] == 2


def test_status_target_words_defaults_to_zero(tmp_path):
    """marker 缺 target_words 字段或标记损坏时，target_words 回落 0。"""
    root = _make_novel_project(tmp_path / "book")
    (root / novel_service.NOVEL_MARKER).write_text(
        json.dumps({"schema": novel_service.NOVEL_SCHEMA, "title": "长夜行"}),
        encoding="utf-8",
    )

    assert novel_service.novel_status(str(root))["target_words"] == 0

    (root / novel_service.NOVEL_MARKER).write_text("{坏掉的 JSON", encoding="utf-8")

    broken = novel_service.novel_status(str(root))
    assert broken["is_novel"] is True
    assert broken["target_words"] == 0


def test_status_non_novel_has_no_chapter_fields(tmp_path):
    """非小说项目仍然只返回 {"is_novel": False}，不附带 chapters / target_words。"""
    plain = tmp_path / "plain"
    plain.mkdir()

    assert novel_service.novel_status(str(plain)) == {"is_novel": False}


# ---------------------------------------------------------------------------
# 第六块：GUI 工作台聚合与文档预览（novel_workbench / read_novel_doc）
# ---------------------------------------------------------------------------

WORKBENCH_TOP_FIELDS = {"is_novel", "foreshadows", "characters", "volumes", "recent_daily"}
FORESHADOW_FIELDS = {
    "id", "summary", "status", "importance", "planted_chapter",
    "planned_resolution_chapter", "updated_chapter", "overdue",
    "planned_overdue", "stale_chapters",
}
CHARACTER_FIELDS = {
    "name", "identity", "state", "location", "goal", "knowledge",
    "abilities_resources", "open_threads", "relationships",
}
VOLUME_FIELDS = {"no", "name", "chars", "current"}


def _foreshadow_row(identifier: str, planted, status: str = "已埋", **extra) -> dict:
    """构造一条伏笔记录：字段命名与 tracking_commit.py 写出的状态一致。"""
    row = {
        "id": identifier,
        "summary": f"{identifier} 的摘要",
        "status": status,
        "importance": extra.pop("importance", "中"),
        "planted_chapter": planted,
        "planned_resolution_chapter": extra.pop("planned_resolution_chapter", 0),
        "updated_chapter": extra.pop("updated_chapter", planted if isinstance(planted, int) else 0),
    }
    row.update(extra)
    return row


def test_workbench_returns_four_tables_with_contract_fields(tmp_path):
    """小说项目：四张表齐全，每条记录的字段集与契约完全一致。"""
    root = _make_novel_project(tmp_path / "book")
    _write_tracking_state(root, {
        "last_committed_chapter": 5,
        "foreshadow": {"F001": _foreshadow_row("F001", 3)},
        "characters": {"李昭": {"identity": "剑客", "open_threads": ["追凶"]}},
    })

    result = novel_service.novel_workbench(str(root))

    assert set(result) == WORKBENCH_TOP_FIELDS
    assert result["is_novel"] is True
    assert set(result["foreshadows"][0]) == FORESHADOW_FIELDS
    assert set(result["characters"][0]) == CHARACTER_FIELDS
    for item in result["volumes"]:
        assert set(item) == VOLUME_FIELDS
    for item in result["recent_daily"]:
        assert set(item) == {"date", "words"}
    # 只读：不写任何文件、状态文件内容未变
    assert json.loads(
        (root / "追踪" / novel_service.TRACKING_STATE_NAME).read_text(encoding="utf-8")
    )["last_committed_chapter"] == 5


def test_workbench_non_novel_and_missing_state_defaults(tmp_path):
    """非小说项目只回 {"is_novel": False}；缺状态文件时四张表为空/固定 7 天。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert novel_service.novel_workbench(str(plain)) == {"is_novel": False}

    root = _make_novel_project(tmp_path / "book")
    result = novel_service.novel_workbench(str(root))

    assert result["is_novel"] is True
    assert result["foreshadows"] == []
    assert result["characters"] == []
    assert result["volumes"] == []
    assert len(result["recent_daily"]) == novel_service.DAILY_WINDOW_DAYS
    assert {item["words"] for item in result["recent_daily"]} == {0}


def test_workbench_foreshadows_sorted_and_overdue_matches_audit(tmp_path):
    """排序为 超期 → 搁置章数 → 埋设章；overdue 与 novel_audit 的超期判定逐条一致。"""
    root = _make_novel_project(tmp_path / "book")
    _write_tracking_state(root, {
        "last_committed_chapter": 25,
        "foreshadow": {
            # 差 22 章 > 20 → 超期
            "F001": _foreshadow_row("F001", 3, planned_resolution_chapter=21),
            # 差 20 章，正好等于阈值 → 不算超期（与 audit 同为严格大于）
            "F002": _foreshadow_row("F002", 5, planned_resolution_chapter=30),
            # 已回收：即使搁置最久也不算超期
            "F003": _foreshadow_row("F003", 2, status="已回收"),
            # 差 15 章 → 不超期
            "F004": _foreshadow_row("F004", 10),
            # 埋设章缺失/为 0：audit 跳过，面板同样不报超期
            "F005": _foreshadow_row("F005", 0),
        },
    })

    workbench = novel_service.novel_workbench(str(root))
    rows = workbench["foreshadows"]

    assert [row["id"] for row in rows] == ["F001", "F005", "F003", "F002", "F004"]
    # F005 埋设章为 0，stale 按 max(0, 25-0)=25 计，因此排在已回收的 F003 之前
    assert [row["stale_chapters"] for row in rows] == [22, 25, 23, 20, 15]
    assert [row["overdue"] for row in rows] == [True, False, False, False, False]

    audit = novel_service.novel_audit(str(root), scope="all")
    overdue_messages = [
        item["message"] for item in audit["findings"] if item["type"] == "foreshadow_overdue"
    ]
    for row in rows:
        audit_hit = any(f"伏笔 {row['id']} " in message for message in overdue_messages)
        assert row["overdue"] is audit_hit, row


def test_workbench_characters_defaults_and_sorting(tmp_path):
    """字符串字段缺省给空串、数组字段缺省给 []；有 open_threads 的排前面再按名字。"""
    root = _make_novel_project(tmp_path / "book")
    _write_tracking_state(root, {
        "characters": {
            "阿七": {"identity": "镖师"},
            "李昭": {"state": "负伤", "open_threads": ["追凶"], "knowledge": ["玉佩来历"]},
            "丙三": {"knowledge": None, "relationships": ["与李昭同门"], "open_threads": []},
        },
    })

    characters = novel_service.novel_workbench(str(root))["characters"]

    assert [item["name"] for item in characters] == ["李昭", "丙三", "阿七"]
    by_name = {item["name"]: item for item in characters}
    for item in characters:
        assert set(item) == CHARACTER_FIELDS
        for key in ("identity", "state", "location", "goal"):
            assert isinstance(item[key], str)
        for key in ("knowledge", "abilities_resources", "open_threads", "relationships"):
            assert isinstance(item[key], list)
    assert by_name["阿七"]["identity"] == "镖师"
    assert by_name["阿七"]["state"] == ""
    assert by_name["阿七"]["knowledge"] == []
    assert by_name["李昭"]["open_threads"] == ["追凶"]
    assert by_name["丙三"]["knowledge"] == []
    assert by_name["丙三"]["relationships"] == ["与李昭同门"]


def test_workbench_volumes_current_requires_unique_range_hit(tmp_path):
    """current 只在卷纲章号范围恰好唯一命中下一章时为 True；重叠时全为 False。"""
    root = _make_novel_project(tmp_path / "book")
    _write_outline(root)  # 卷纲_第001卷.md：章节范围 第1-3章
    (root / "大纲" / "卷纲_第002卷.md").write_text(
        "# 第002卷 卷纲\n\n- 章节范围：第4-8章\n", encoding="utf-8"
    )
    # 上次提交到第 4 章 → 下一章为第 5 章，唯一落在第二卷
    _write_tracking_state(root, {"last_committed_chapter": 4})

    volumes = novel_service.novel_workbench(str(root))["volumes"]

    assert [item["no"] for item in volumes] == [1, 2]
    assert [item["name"] for item in volumes] == ["卷纲_第001卷", "卷纲_第002卷"]
    assert [item["current"] for item in volumes] == [False, True]
    wordcount = _vendor_wordcount()
    for item in volumes:
        text = (root / "大纲" / f"{item['name']}.md").read_text(encoding="utf-8")
        assert item["chars"] == wordcount.count_visible_chars(text)
        assert item["chars"] > 0

    # 第一卷改声明为第1-6章、第二卷为第2-8章：第 5 章同时落在两卷 → 归属不可靠，
    # 两卷都不标当前卷（与 novel_status 省略 volume 字段同一判定）
    (root / "大纲" / "卷纲_第001卷.md").write_text(
        "# 第001卷 卷纲\n\n- 章节范围：第1-6章\n", encoding="utf-8"
    )
    (root / "大纲" / "卷纲_第002卷.md").write_text(
        "# 第002卷 卷纲\n\n- 章节范围：第2-8章\n", encoding="utf-8"
    )
    assert [item["current"] for item in novel_service.novel_workbench(str(root))["volumes"]] == [
        False, False
    ]


def test_workbench_recent_daily_has_seven_days_aggregated_by_mtime(tmp_path):
    """recent_daily 固定 7 项、按日期升序；按章节文件 mtime 落到当天，窗口外不计。"""
    root = _make_novel_project(tmp_path / "book")
    wordcount = _vendor_wordcount()
    today_text = "今日正文若干字"
    _write_chapter(root, 1, "今日", today_text)
    three_days_ago = _write_chapter(root, 2, "三天前", "三天前的正文")
    three_days_ago_timestamp = (
        datetime.now() - timedelta(days=3)
    ).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    os.utime(three_days_ago, (three_days_ago_timestamp, three_days_ago_timestamp))
    old_path = _write_chapter(root, 3, "旧章", "很久以前的正文")
    old_timestamp = datetime(2024, 1, 1, 12, 0, 0).timestamp()
    os.utime(old_path, (old_timestamp, old_timestamp))

    rows = novel_service.novel_workbench(str(root))["recent_daily"]

    assert len(rows) == 7
    dates = [datetime.strptime(row["date"], "%Y-%m-%d").date() for row in rows]
    assert dates == sorted(dates)
    assert dates[-1] == date.today()
    assert dates[0] == date.today() - timedelta(days=6)
    by_date = {row["date"]: row["words"] for row in rows}
    assert by_date[date.today().isoformat()] == wordcount.count_visible_chars(today_text)
    assert by_date[dates[3].isoformat()] == wordcount.count_visible_chars("三天前的正文")
    # 窗口外的旧章不计入任何一天；7 天字数之和等于窗口内正文
    assert sum(by_date.values()) == (
        wordcount.count_visible_chars(today_text)
        + wordcount.count_visible_chars("三天前的正文")
    )


def test_read_novel_doc_allows_whitelisted_dirs_only(tmp_path):
    """大纲/设定/追踪 下的 .md 可读，返回原文、相对路径与同口径可见字数。"""
    root = _make_novel_project(tmp_path / "book")
    wordcount = _vendor_wordcount()
    docs = {
        "大纲": ("大纲/卷纲_第001卷.md", "# 第001卷 卷纲\n\n- 章节范围：第1-30章\n"),
        "设定": ("设定/世界观.md", "# 世界观\n\n主角所在的城市常年下雨。\n"),
        "追踪": ("追踪/追踪备忘.md", "# 备忘\n\n伏笔 F001 待回收。\n"),
    }
    for rel, text in docs.values():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    for rel, text in docs.values():
        result = novel_service.read_novel_doc(str(root), rel)

        assert result["ok"] is True, result
        assert result["text"] == text
        assert result["path"] == rel
        assert result["chars"] == wordcount.count_visible_chars(text)
        assert (root / rel).read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "rel_path",
    [
        "../外部.md",              # .. 穿越
        "正文/第001章_夜行.md",     # 白名单外目录
        "大纲/../../外部.md",       # 目录内再穿越
    ],
)
def test_read_novel_doc_rejects_escaping_paths(tmp_path, rel_path):
    """越界路径一律拒绝：三段文案都带拒绝理由，且绝不返回文件内容。"""
    root = _make_novel_project(tmp_path / "book")
    _write_chapter(root, 1, "夜行", "# 第001章 夜行\n\n风起。\n")
    (tmp_path / "外部.md").write_text("项目外的内容\n", encoding="utf-8")

    result = novel_service.read_novel_doc(str(root), rel_path)

    assert result["ok"] is False, result
    assert "text" not in result
    assert result["error"]


def test_read_novel_doc_rejects_absolute_non_md_and_oversize(tmp_path):
    """绝对路径、非 .md、超过 2MB 各自返回不同 error，不抛异常。"""
    root = _make_novel_project(tmp_path / "book")
    outline = _write_outline(root)

    absolute = novel_service.read_novel_doc(str(root), str(outline.resolve()))
    assert absolute["ok"] is False
    assert "绝对路径" in absolute["error"]

    chart = root / "大纲" / "节奏表.json"
    chart.write_text("{}", encoding="utf-8")
    not_markdown = novel_service.read_novel_doc(str(root), "大纲/节奏表.json")
    assert not_markdown["ok"] is False
    assert ".md" in not_markdown["error"]

    big = root / "设定" / "大部头.md"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"a" * (novel_service.DOC_PREVIEW_MAX_BYTES + 1))
    oversize = novel_service.read_novel_doc(str(root), "设定/大部头.md")
    assert oversize["ok"] is False
    assert oversize["error"] == "文件过大，不在面板内预览"

    missing = novel_service.read_novel_doc(str(root), "设定/查无此档.md")
    assert missing["ok"] is False
    assert "不存在" in missing["error"]
    assert len({absolute["error"], not_markdown["error"], missing["error"]}) == 3


def test_read_novel_doc_non_novel_project_returns_flag(tmp_path):
    """非小说项目：返回 is_novel=False 的结构化错误，不读任何文件。"""
    plain = tmp_path / "plain"
    plain.mkdir()

    result = novel_service.read_novel_doc(str(plain), "大纲/x.md")

    assert result["ok"] is False
    assert result["is_novel"] is False
    assert "缺少 .novel.json" in result["error"]

