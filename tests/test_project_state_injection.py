import os
import sys
import types
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import project_profile
from core.memory import MemoryManager

WORKDIR = r"D:\proj\demo"
SESSION = "test_project_state"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / ".ycode"))


@pytest.fixture
def stub_llm(monkeypatch):
    import threading

    stub = types.ModuleType("core.llm")
    stub.estimate_tokens = lambda model, messages: sum(
        len(str(m.get("content", ""))) for m in messages
    )
    stub.session_token_usage = {
        "cumulative_total": 0, "cumulative_prompt": 0, "cumulative_cached": 0,
        "latest_total": 0, "latest_prompt": 0, "latest_cached": 0,
        "max_window": 128000,
    }
    stub.token_usage_lock = threading.Lock()
    stub.resolve_turn_computer_capability = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "core.llm", stub)
    return stub


@pytest.fixture
def quiet_extras(monkeypatch):
    import core.agent_service as agent_service

    monkeypatch.setattr(
        agent_service, "build_background_task_summary", lambda session: "", raising=True
    )

    import core.skills as skills
    import core.subagent_registry as subagent_registry
    import core.memory_kairos as memory_kairos

    monkeypatch.setattr(skills, "get_active_skills_catalog", lambda: "", raising=True)
    monkeypatch.setattr(
        subagent_registry, "get_custom_subagents_catalog", lambda: "", raising=True
    )
    monkeypatch.setattr(
        memory_kairos, "build_memory_system_section", lambda: "无", raising=True
    )
    return True


def _manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


def _prepare(manager, session=SESSION):
    from core.agent_service import prepare_turn_messages

    return prepare_turn_messages(
        manager,
        session,
        "openai/gpt-test",
        128000,
        permission_mode="confirm",
    )


def test_state_section_injected_into_last_user_message(tmp_path, stub_llm, quiet_extras):
    project_profile.append_section(WORKDIR, project_profile.SECTION_STACK, "Python 3.11 + PyQt5")
    project_profile.append_section(
        WORKDIR, project_profile.SECTION_CONSTRAINT, "不得改动 gui_bridge 的桥签名"
    )

    manager = _manager(tmp_path)
    manager.ensure_session(SESSION, workdir=WORKDIR)
    manager.add_message(SESSION, "user", "接着改一下右侧面板")

    messages = _prepare(manager)

    tail = messages[-1]["content"]
    assert tail.startswith("接着改一下右侧面板")
    assert project_profile.STATE_HEADER in tail
    assert "Python 3.11 + PyQt5" in tail
    assert "不得改动 gui_bridge 的桥签名" in tail
    assert sum(1 for m in messages if m["role"] == "system") == 1


def test_state_section_uses_session_workdir_not_process_cwd(tmp_path, monkeypatch):
    other = r"D:\proj\other"
    project_profile.append_section(other, project_profile.SECTION_STACK, "别的项目的技术栈")
    project_profile.append_section(WORKDIR, project_profile.SECTION_STACK, "本项目技术栈")

    manager = _manager(tmp_path)
    manager.ensure_session(SESSION, workdir=WORKDIR)
    manager.add_message(SESSION, "user", "问题")

    from core.agent_service import _build_project_state_prompt

    block = _build_project_state_prompt(manager, SESSION)
    assert "本项目技术栈" in block
    assert "别的项目的技术栈" not in block


def test_no_injection_without_profile(tmp_path, stub_llm, quiet_extras):
    manager = _manager(tmp_path)
    manager.ensure_session(SESSION, workdir=WORKDIR)
    manager.add_message(SESSION, "user", "没有画像也要正常回答")

    messages = _prepare(manager)

    assert project_profile.STATE_HEADER not in messages[-1]["content"]
    assert "当前运行上下文" in messages[-1]["content"]


def test_injection_failure_degrades_silently(tmp_path, stub_llm, quiet_extras, monkeypatch):
    manager = _manager(tmp_path)

    def _boom(_name):
        raise RuntimeError("db 挂了")

    monkeypatch.setattr(manager, "get_session_workdir", _boom)
    monkeypatch.setattr(
        "core.workspace_context.get_workdir", lambda: tmp_path / "no-such-workdir"
    )
    manager.add_message(SESSION, "user", "取目录失败也要能聊")

    messages = _prepare(manager)

    assert project_profile.STATE_HEADER not in messages[-1]["content"]
    assert messages[-1]["content"].startswith("取目录失败也要能聊")


def test_build_state_section_empty_when_three_sections_blank(tmp_path):
    project_profile.append_section(WORKDIR, project_profile.SECTION_PITFALL, "踩过的坑")
    assert project_profile.build_state_section(WORKDIR) == ""
    assert project_profile.build_state_section(r"D:\proj\never") == ""
    assert project_profile.build_state_section("") == ""
    assert project_profile.build_state_section("GLOBAL") == ""


def _write_profile_with_updated(updated: str, stack: str = "- Python 3.11") -> None:
    path = project_profile.profile_path(WORKDIR)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nworkdir: D:/proj/demo\nupdated: %s\n---\n\n## 技术栈\n\n%s\n" % (updated, stack),
        encoding="utf-8",
    )


def test_stale_profile_marked_expired(tmp_path):
    old = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    _write_profile_with_updated(old)

    block = project_profile.build_state_section(WORKDIR)
    assert "状态可能已过期" in block
    assert "Python 3.11" in block


def test_fresh_profile_not_marked_expired(tmp_path):
    fresh = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    _write_profile_with_updated(fresh)

    block = project_profile.build_state_section(WORKDIR)
    assert "状态可能已过期" not in block
    assert "Python 3.11" in block


def test_state_section_truncated_to_limit(tmp_path):
    long_text = "需求细节" * 500
    _write_profile_with_updated(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), stack="- " + long_text
    )

    block = project_profile.build_state_section(WORKDIR)
    assert block
    assert len(block) <= project_profile.DEFAULT_INJECTION_LIMIT
    assert block.startswith(project_profile.STATE_HEADER)
    assert block.endswith("…")

    small = project_profile.build_state_section(WORKDIR, limit=300)
    assert len(small) <= 300


def test_profile_tools_visible_in_ordinary_schema(monkeypatch):
    import core.llm as llm
    import core.secretary as secretary

    class _NoMcp:
        def get_schemas(self):
            return []

    monkeypatch.setattr(llm, "_get_mcp_manager", lambda: _NoMcp())

    ordinary = {
        s["function"]["name"]
        for s in llm._ordinary_tools_schema(secretary.SECRETARY_ONLY_TOOLS)
    }
    assert "read_project_profile" in ordinary
    assert "remember_decision" in ordinary
    secretary_names = {s["function"]["name"] for s in llm._secretary_tools_schema()}
    assert {"read_project_profile", "remember_decision"} <= secretary_names


def test_write_rejected_outside_projects_root(tmp_path):
    root = project_profile.projects_root()
    outside = root / ".." / ".." / "escape.md"
    assert project_profile._write_text(outside, "不该落盘") is False
    assert not outside.exists()


def test_remember_decision_never_writes_into_target_dir(tmp_path):
    from core import tools

    target = str(tmp_path / "some_project")
    os.makedirs(target, exist_ok=True)

    message = tools.remember_decision(
        workdir=target, section=project_profile.SECTION_CONSTRAINT, content="只写记忆目录"
    )
    assert message.startswith("已记入")
    assert os.listdir(target) == []
    path = project_profile.profile_path(target)
    assert path.parent == project_profile.projects_root()
    assert path.exists()

    traversal = str(tmp_path) + "/../../evil"
    assert tools.remember_decision(
        workdir=traversal, section=project_profile.SECTION_STACK, content="越界测试"
    ).startswith("已记入")
    written = {p.name for p in project_profile.projects_root().glob("*.md")}
    assert len(written) == 2