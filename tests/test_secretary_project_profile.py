"""项目画像（跨项目记忆，P2 任务 5）：画像读写、注入、工具层与派活注入。

隔离口径：每个用例把 HOME / USERPROFILE / YCODE_DATA_DIR 全部重定向到
``tmp_path``，画像只会落在 ``<tmp>/.ycode/memory/projects/``，绝不碰真实
``~/.ycode/memory/projects/``。
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.app_context as app_context
import core.secretary as secretary
from core import project_profile, secretary_dispatch, tools
from core.memory import MemoryManager
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """重定向家目录与数据目录：画像文件只能落在 tmp_path 内。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / ".ycode"))


@pytest.fixture(autouse=True)
def _clean_singleton():
    secretary_dispatch.unregister()
    yield
    secretary_dispatch.unregister()


@pytest.fixture()
def manager(tmp_path):
    m = MemoryManager(str(tmp_path / "ycode.db"))
    prev_ctx = app_context.get()
    prev_manager = app_context.get_manager()
    prev_session = app_context.get_current_session()
    app_context.register(None)
    app_context.set_manager(m)
    app_context.set_current_session("")
    secretary.invalidate_secretary_cache()
    try:
        yield m
    finally:
        secretary.invalidate_secretary_cache()
        app_context.set_current_session(prev_session)
        app_context.set_manager(prev_manager)
        app_context.register(prev_ctx)


WORKDIR = r"D:\proj\demo"


# ---------------------------------------------------------------------------
# 1/2 路径稳定性与空 workdir
# ---------------------------------------------------------------------------

def test_profile_path_stable_and_inside_projects(tmp_path):
    first = project_profile.profile_path(WORKDIR)
    second = project_profile.profile_path(r"d:/proj/demo/".replace("d:", "D:"))
    assert first == second
    assert first != project_profile.profile_path(r"D:\proj\other")
    # 分隔符 / 盘符大小写 / 尾部斜杠都被规范化掉
    assert first == project_profile.profile_path("D:/proj/demo/")
    root = tmp_path / ".ycode" / "memory" / "projects"
    assert str(first).startswith(str(root))
    assert first.name.endswith(".md") and len(first.stem) == 12


def test_empty_and_global_workdir_have_no_profile():
    assert project_profile.profile_path("") is None
    assert project_profile.profile_path("   ") is None
    assert project_profile.profile_path("GLOBAL") is None
    assert project_profile.read_profile("") == ""
    assert project_profile.read_profile("GLOBAL") == ""
    assert project_profile.append_section("", "技术栈", "x") == ""
    assert project_profile.append_task_log("GLOBAL", "2026-11-04 一行") is None
    assert project_profile.build_injection("") == ""


# ---------------------------------------------------------------------------
# 3/4 写入与章节校验
# ---------------------------------------------------------------------------

def test_append_section_roundtrip_and_second_append(tmp_path):
    assert project_profile.read_profile(WORKDIR) == ""

    message = project_profile.append_section(WORKDIR, "硬约束", "不许改动 gui_bridge 的入口签名")
    assert "已记入" in message and "硬约束" in message

    text = project_profile.read_profile(WORKDIR)
    assert "不许改动 gui_bridge 的入口签名" in text
    assert "workdir: D:/proj/demo" in text
    for name in project_profile.SECTION_ORDER:
        assert "## " + name in text

    project_profile.append_section(WORKDIR, "硬约束", "禁止在测试里连真实数据库")
    text = project_profile.read_profile(WORKDIR)
    assert "不许改动 gui_bridge 的入口签名" in text
    assert "禁止在测试里连真实数据库" in text
    # 章节只出现一次（追加而不是复制章节）
    assert text.count("## 硬约束") == 1


def test_append_section_rejects_illegal_section():
    with pytest.raises(ValueError):
        project_profile.append_section(WORKDIR, "随便写", "内容")
    with pytest.raises(ValueError):
        project_profile.append_section(WORKDIR, "技术栈", "   ")
    assert project_profile.read_profile(WORKDIR) == ""


# ---------------------------------------------------------------------------
# 5 注入只取三节 + 截断
# ---------------------------------------------------------------------------

def test_build_injection_only_three_sections_and_truncated():
    project_profile.append_section(WORKDIR, "技术栈", "Python 3.11 + pytest")
    project_profile.append_section(WORKDIR, "架构决策", "GUI 桥单例 + 线程上下文")
    project_profile.append_section(WORKDIR, "硬约束", "STACK-CONSTRAINT-MARKER")
    project_profile.append_section(WORKDIR, "踩坑记录", "PITFALL-MARKER 不要这么做")
    project_profile.append_section(WORKDIR, "用户偏好", "PREFERENCE-MARKER 喜欢简短回答")

    injection = project_profile.build_injection(WORKDIR)
    assert "Python 3.11 + pytest" in injection
    assert "GUI 桥单例 + 线程上下文" in injection
    assert "STACK-CONSTRAINT-MARKER" in injection
    assert "PITFALL-MARKER" not in injection
    assert "PREFERENCE-MARKER" not in injection
    assert "## 踩坑记录" not in injection
    assert "## 用户偏好" not in injection

    for index in range(60):
        project_profile.append_section(WORKDIR, "硬约束", f"约束条目{index}：这是一段很长的描述" * 3)
    long_injection = project_profile.build_injection(WORKDIR)
    assert len(long_injection) <= 800
    assert len(long_injection) >= 790
    assert project_profile.build_injection(WORKDIR, 100).endswith("…")


# ---------------------------------------------------------------------------
# 6 越界
# ---------------------------------------------------------------------------

def test_traversal_workdir_stays_inside_projects(tmp_path):
    root = tmp_path / ".ycode" / "memory" / "projects"
    for workdir in ("../../etc", "..\\..\\windows\\system32", "D:/proj/../../etc/passwd"):
        path = project_profile.profile_path(workdir)
        assert path is not None
        assert path.parent == root
        assert path.name.endswith(".md") and len(path.stem) == 12
    message = project_profile.append_section("../../etc", "技术栈", "依然安全")
    assert "已记入" in message
    written = list(root.glob("*.md"))
    assert len(written) == 1 and written[0].parent == root


# ---------------------------------------------------------------------------
# 7 remember_decision 工具层
# ---------------------------------------------------------------------------

def test_remember_decision_tool_layer():
    bad_section = tools.remember_decision(workdir=WORKDIR, section="随便写", content="内容")
    assert "记录失败" in bad_section and "随便写" in bad_section
    empty = tools.remember_decision(workdir=WORKDIR, section="技术栈", content="   ")
    assert "记录失败" in empty and "content" in empty
    too_long = tools.remember_decision(workdir=WORKDIR, section="技术栈", content="啊" * 1300)
    assert "记录失败" in too_long and "1200" in too_long

    ok = tools.remember_decision(workdir=WORKDIR, section="硬约束", content="只允许写 memory 目录")
    assert "已记入" in ok and "demo" in ok and "硬约束" in ok
    assert "只允许写 memory 目录" in project_profile.read_profile(WORKDIR)

    assert "该项目还没有画像" in tools.read_project_profile(workdir=r"D:\proj\empty")
    assert "只允许写 memory 目录" in tools.read_project_profile(workdir=WORKDIR)


# ---------------------------------------------------------------------------
# 8 派活注入
# ---------------------------------------------------------------------------

def _make_bridge(manager):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._current_session = "s_secretary"
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._cancel_events = {}
    bridge._is_turn_active = lambda s: False
    bridge._memory = manager
    bridge._active_turn_ids = {}
    bridge._active_turn_lock = threading.Lock()
    bridge._active_turn_snapshots = {}
    bridge._turn_owner_lock = threading.Lock()
    bridge._turn_owner_sessions = {}
    bridge._bg_summary_lock = threading.Lock()
    bridge._bg_summary_triggered = set()
    bridge._bg_summary_pending_sessions = set()
    bridge._user_cancelled_sessions = set()
    bridge._task_status = "idle"
    bridge._undo_turn_impact_for_event = lambda request: None
    bridge._get_cancel_event = lambda s: threading.Event()
    bridge.emit_event = lambda etype, payload: None
    spawned = []
    bridge._spawn_turn_worker = lambda *a, **k: spawned.append((a, k))
    bridge._register_secretary_dispatch_service()
    return bridge, spawned


def test_dispatch_injects_profile(manager, tmp_path):
    project = tmp_path / "proj-inject"
    project.mkdir()
    bridge, spawned = _make_bridge(manager)
    prompt = "把 README 补一版"

    # 无画像：原样投递
    secretary_dispatch.get().dispatch_task(
        target="新会话", prompt=prompt, workdir=str(project)
    )
    assert spawned[-1][0][0] == prompt

    # 有画像：前置项目背景块 + 【任务】
    project_profile.append_section(str(project), "硬约束", "Promise 一律用 async")
    secretary_dispatch.get().dispatch_task(
        target="新会话", prompt=prompt, workdir=str(project)
    )
    delivered = spawned[-1][0][0]
    assert delivered.startswith("【项目背景（自动带入，可忽略）】")
    assert "Promise 一律用 async" in delivered
    assert "【任务】" in delivered
    assert delivered.rstrip().endswith(prompt)
    # 台账仍记原始 prompt（注入只影响投递文本）
    rows = secretary_dispatch.get()._store.list()
    assert {row["prompt"] for row in rows} == {prompt}


# ---------------------------------------------------------------------------
# 9 可见性
# ---------------------------------------------------------------------------

def test_profile_tools_visible_in_secretary_schema_only(monkeypatch):
    import core.llm as llm

    class _NoMcp:
        def get_schemas(self):
            return []

    monkeypatch.setattr(llm, "_get_mcp_manager", lambda: _NoMcp())

    secretary_names = {s["function"]["name"] for s in llm._secretary_tools_schema()}
    assert "read_project_profile" in secretary_names
    assert "remember_decision" in secretary_names

    registry_names = {s["function"]["name"] for s in llm.tool_registry.api_schemas()}
    ordinary_names = {
        s["function"]["name"]
        for s in llm._ordinary_tools_schema(secretary.SECRETARY_ONLY_TOOLS)
    }
    # 特性 1（2026-11-07）：项目画像两工具已对普通会话开放（项目状态查看/编辑）
    assert "read_project_profile" in ordinary_names
    assert "remember_decision" in ordinary_names
    assert "read_project_profile" not in secretary.SECRETARY_ONLY_TOOLS
    assert "remember_decision" not in secretary.SECRETARY_ONLY_TOOLS
    assert "remember_decision" in secretary.SECRETARY_ALLOWED_TOOLS
    # 普通会话工具集 = 注册表全量剔除秘书专属项
    assert ordinary_names == registry_names - set(secretary.SECRETARY_ONLY_TOOLS)

    # 门控：remember_decision 作用域已改为 system（只写记忆目录），须按名字放行
    scopes = llm._registry_scope_map()
    assert scopes.get("remember_decision") == "system"
    assert secretary.secretary_allows_tool("remember_decision", "system") is True
    assert secretary.secretary_allows_tool("read_project_profile", "none") is True
    # 未列入白名单的写类工具仍被拒（放行是按名字，不是按作用域开口子）
    assert secretary.secretary_allows_tool("write_file", "workspace") is False
