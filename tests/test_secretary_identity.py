import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.app_context as app_context
import core.secretary as secretary
from core.memory import MemoryManager


@pytest.fixture
def manager(tmp_path):
    m = MemoryManager(str(tmp_path / "s.db"))
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


def test_secretary_name_roundtrip(manager):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    assert manager.get_session_secretary_name("s_sec") == "星河"
    assert manager.get_session_source("s_sec") == "SECRETARY"

    manager.create_session_explicit("s_plain", workdir="GLOBAL", source="GUI")
    assert manager.get_session_secretary_name("s_plain") == ""
    assert manager.get_session_source("s_plain") == "GUI"
    assert manager.get_session_secretary_name("s_missing") == ""
    assert manager.get_session_source("s_missing") == ""


def test_set_session_secretary_name_updates_and_reports_rowcount(manager):
    manager.create_session_explicit(
        "s_rename", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    assert manager.set_session_secretary_name("s_rename", "小满") is True
    assert manager.get_session_secretary_name("s_rename") == "小满"

    assert manager.set_session_secretary_name("s_rename", "  ") is True
    assert manager.get_session_secretary_name("s_rename") == ""

    assert manager.set_session_secretary_name("s_nope", "小满") is False


def test_validate_secretary_name_rejects_blank_and_long():
    assert secretary.validate_secretary_name("  星河  ") == "星河"
    assert secretary.validate_secretary_name("星" * 20) == "星" * 20
    with pytest.raises(ValueError):
        secretary.validate_secretary_name("   ")
    with pytest.raises(ValueError):
        secretary.validate_secretary_name("星" * 21)
    with pytest.raises(ValueError):
        secretary.validate_secretary_name(None)


def test_is_secretary_session_and_get_secretary_name(manager):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.create_session_explicit("s_plain", workdir="GLOBAL", source="GUI")

    assert secretary.is_secretary_session("s_sec") is True
    assert secretary.is_secretary_session("s_plain") is False
    assert secretary.is_secretary_session("s_missing") is False

    assert secretary.get_secretary_name("s_sec") == "星河"
    assert secretary.get_secretary_name("s_plain") == ""


def test_active_agent_name_switches_by_session(manager, monkeypatch):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.create_session_explicit("s_plain", workdir="GLOBAL", source="GUI")
    monkeypatch.setattr(
        "core.persona_settings.get_agent_display_name", lambda: "设置页名字"
    )

    app_context.set_current_session("s_sec")
    assert secretary.get_active_agent_name() == "星河"

    app_context.set_current_session("s_plain")
    assert secretary.get_active_agent_name() == "设置页名字"


def test_active_agent_name_falls_back_for_unnamed_secretary(manager):
    manager.create_session_explicit("s_noname", workdir="GLOBAL", source="SECRETARY")
    app_context.set_current_session("s_noname")
    assert secretary.get_active_agent_name() == secretary.FALLBACK_SECRETARY_NAME


def test_active_agent_name_never_raises_on_backend_error(monkeypatch):
    class Boom:
        def get_session_source(self, *a, **k):
            raise RuntimeError("boom")

        def get_session_secretary_name(self, *a, **k):
            raise RuntimeError("boom")

    secretary.invalidate_secretary_cache()
    app_context.set_current_session("s_broken")
    monkeypatch.setattr(app_context, "get_manager", lambda: Boom())
    monkeypatch.setattr(
        "core.persona_settings.get_agent_display_name", lambda: "设置页名字"
    )
    # 数据层抛错时回落 persona 名，绝不冒泡
    assert secretary.get_active_agent_name() == "设置页名字"
    secretary.invalidate_secretary_cache()


# ---------------------------------------------------------------------------
# 名字块与缓存失效
# ---------------------------------------------------------------------------

def test_build_secretary_name_block():
    block = secretary.build_secretary_name_block("星河")
    assert "星河" in block and "秘书" in block
    assert secretary.build_secretary_name_block("") == ""


def test_invalidate_secretary_cache_refreshes_name(manager):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    assert secretary.get_secretary_name("s_sec") == "星河"

    manager.set_session_secretary_name("s_sec", "小满")
    assert secretary.get_secretary_name("s_sec") == "星河"

    secretary.invalidate_secretary_cache("s_sec")
    assert secretary.get_secretary_name("s_sec") == "小满"

    manager.set_session_secretary_name("s_sec", "云疏")
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_name("s_sec") == "云疏"


_SECRETARY_PROMPT_MARKER = "用户的私人秘书"
_PERSONA_BLOCK_HEADER = "# 人设设置（来自设置页，用户自定义）"


def test_secretary_session_stable_prompt_carries_secretary_name(manager, monkeypatch):
    import core.agent_service as agent_service
    import core.persona_settings as persona_settings

    manager.create_session_explicit(
        "s_sec_prompt",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="星河",
    )
    secretary.invalidate_secretary_cache()
    # 人设块故意设为非空：秘书会话必须跳过它，否则星瑶人设会串进秘书身份
    monkeypatch.setattr(
        persona_settings,
        "build_persona_prompt_block",
        lambda: _PERSONA_BLOCK_HEADER + "\n\n名字覆盖：你是「星瑶」。",
    )

    content = agent_service.build_stable_system_message(
        is_gui=True, session_name="s_sec_prompt"
    )["content"]

    assert "星河" in content
    assert _SECRETARY_PROMPT_MARKER in content
    assert "{{secretary_name}}" not in content
    assert _PERSONA_BLOCK_HEADER not in content


def test_plain_session_stable_prompt_bytes_unchanged(manager):
    import core.agent_service as agent_service

    manager.create_session_explicit("s_plain_prompt", workdir="GLOBAL", source="GUI")
    secretary.invalidate_secretary_cache()
    app_context.set_current_session("s_plain_prompt")

    with_name = agent_service.build_stable_system_message(
        is_gui=True, session_name="s_plain_prompt"
    )["content"]
    without_name = agent_service.build_stable_system_message(is_gui=True)["content"]

    assert with_name == without_name
    assert _SECRETARY_PROMPT_MARKER not in with_name
    assert "{{secretary_name}}" not in with_name


def test_secretary_prompt_falls_back_when_file_missing(tmp_path, manager, monkeypatch):
    import core.agent_service as agent_service

    manager.create_session_explicit(
        "s_sec_fallback",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="望舒",
    )
    secretary.invalidate_secretary_cache()

    monkeypatch.setattr(
        agent_service, "SECRETARY_PROMPT_FILENAME", "system_prompt_secretary_gui.none.md"
    )
    content = agent_service.build_stable_system_message(
        is_gui=True, session_name="s_sec_fallback"
    )["content"]
    assert _SECRETARY_PROMPT_MARKER not in content
    assert "{{secretary_name}}" not in content
    assert "望舒" in content
    # 名字替换只对"提示词正文"负责：整条 system message 还挂着技能目录、子 Agent
    # 清单、记忆索引等外部文本，而内置技能描述里本来就可能出现默认名
    # （skills/plugin-creator 的 description 就写着"为星瑶开发插件"）。
    # 断言必须收窄到正文，否则技能目录一改就误报。
    prompt_text = agent_service.load_system_prompt(is_gui=True, session_name="s_sec_fallback")
    assert "望舒" in prompt_text
    assert "星瑶" not in prompt_text

    monkeypatch.setattr(agent_service, "_repo_root", lambda: str(tmp_path))
    fallback = agent_service.load_system_prompt(is_gui=True, session_name="s_sec_fallback")
    assert "望舒" in fallback and "星瑶" not in fallback


def test_plain_session_unaffected_when_prompt_files_missing(tmp_path, monkeypatch):
    import core.agent_service as agent_service
    import core.persona_settings as persona_settings

    monkeypatch.setattr(agent_service, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        agent_service, "SECRETARY_PROMPT_FILENAME", "system_prompt_secretary_gui.none.md"
    )
    app_context.set_current_session("")

    content = agent_service.load_system_prompt(is_gui=True, session_name="s_not_secretary")
    expected = agent_service.FALLBACK_SYSTEM_PROMPT.format(
        agent_name=persona_settings.get_agent_display_name()
    )
    assert content == expected


_SECRETARY_PERSONA_HEADER = "# 秘书人设（本会话专属，由用户设定）"


def test_secretary_persona_roundtrip(manager):
    manager.create_session_explicit(
        "s_persona",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="星河",
        secretary_persona="你是一位做事讲究先后的行政秘书。",
    )
    assert manager.get_session_secretary_persona("s_persona") == "你是一位做事讲究先后的行政秘书。"

    manager.create_session_explicit("s_plain_persona", workdir="GLOBAL", source="GUI")
    assert manager.get_session_secretary_persona("s_plain_persona") == ""
    assert manager.get_session_secretary_persona("s_missing_persona") == ""

    assert manager.set_session_secretary_persona("s_persona", "新人设") is True
    assert manager.get_session_secretary_persona("s_persona") == "新人设"
    assert manager.set_session_secretary_persona("s_persona", "  ") is True
    assert manager.get_session_secretary_persona("s_persona") == ""
    assert manager.set_session_secretary_persona("s_nope_persona", "x") is False


def test_validate_secretary_persona_allows_empty_and_rejects_too_long():
    assert secretary.validate_secretary_persona("") == ""
    assert secretary.validate_secretary_persona("   ") == ""
    assert secretary.validate_secretary_persona(None) == ""
    assert secretary.validate_secretary_persona("  人设  ") == "人设"
    assert secretary.validate_secretary_persona("设" * secretary.MAX_PERSONA_LEN)
    with pytest.raises(ValueError):
        secretary.validate_secretary_persona("设" * (secretary.MAX_PERSONA_LEN + 1))
    with pytest.raises(ValueError):
        secretary.validate_secretary_persona(123)


def test_secretary_persona_read_and_cache_invalidation(manager):
    manager.create_session_explicit(
        "s_persona_cache",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="星河",
        secretary_persona="旧人设",
    )
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona("s_persona_cache") == "旧人设"

    manager.set_session_secretary_persona("s_persona_cache", "新人设")
    assert secretary.get_secretary_persona("s_persona_cache") == "旧人设"

    secretary.invalidate_secretary_cache("s_persona_cache")
    assert secretary.get_secretary_persona("s_persona_cache") == "新人设"
    manager.create_session_explicit("s_plain_cache", workdir="GLOBAL", source="GUI")
    manager.set_session_secretary_persona("s_plain_cache", "不该出现")
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona("s_plain_cache") == ""


def test_build_secretary_persona_block():
    block = secretary.build_secretary_persona_block("你是一位说话简短直接的秘书。")
    assert _SECRETARY_PERSONA_HEADER in block
    assert "你是一位说话简短直接的秘书。" in block
    assert "取代" in block and "绝对红线" in block
    assert secretary.build_secretary_persona_block("") == ""
    assert secretary.build_secretary_persona_block(None) == ""


def test_secretary_prompt_injects_custom_persona(manager, monkeypatch):
    import core.agent_service as agent_service
    import core.persona_settings as persona_settings

    manager.create_session_explicit(
        "s_prompt_persona",
        workdir="GLOBAL",
        source="SECRETARY",
        secretary_name="星河",
        secretary_persona="你是一位说话像老朋友的秘书，偶尔会用一个语气词。",
    )
    secretary.invalidate_secretary_cache()
    monkeypatch.setattr(
        persona_settings,
        "build_persona_prompt_block",
        lambda: _PERSONA_BLOCK_HEADER + "\n\n名字覆盖：你是「星瑶」。",
    )
    prompt = agent_service.load_system_prompt(is_gui=True, session_name="s_prompt_persona")

    assert prompt.startswith(_SECRETARY_PERSONA_HEADER)
    assert "你是一位说话像老朋友的秘书" in prompt
    assert _SECRETARY_PROMPT_MARKER in prompt
    assert "{{secretary_name}}" not in prompt
    assert "星河" in prompt
    assert _PERSONA_BLOCK_HEADER not in prompt


def test_secretary_prompt_without_persona_has_no_persona_block(manager):
    import core.agent_service as agent_service

    manager.create_session_explicit(
        "s_prompt_default", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    secretary.invalidate_secretary_cache()
    prompt = agent_service.load_system_prompt(is_gui=True, session_name="s_prompt_default")
    assert _SECRETARY_PERSONA_HEADER not in prompt
    assert prompt.startswith("你是「星河」，用户的私人秘书。")


def _lightweight_bridge(manager):
    import gui_bridge

    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._memory = manager
    bridge.get_sessions = lambda: []
    return bridge


def test_bridge_create_secretary_with_persona_and_update_it(manager):
    bridge = _lightweight_bridge(manager)
    created = bridge.create_secretary_session("新会话", "星河", "你是说话干脆的行政秘书。")
    assert created["ok"] is True
    session = created["session_name"]
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona(session) == "你是说话干脆的行政秘书。"

    updated = bridge.set_secretary_persona(session, "改成一个爱开玩笑的秘书。")
    assert updated["ok"] is True and updated["session_name"] == session
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona(session) == "改成一个爱开玩笑的秘书。"

    assert bridge.set_secretary_persona(session, "  ")["ok"] is True
    secretary.invalidate_secretary_cache()
    assert secretary.get_secretary_persona(session) == ""


def test_bridge_rejects_bad_persona_and_non_secretary_session(manager):
    bridge = _lightweight_bridge(manager)
    # 超长人设拒绝，且不产生会话记录（否则会留下一个名字合法但人设非法的孤儿会话）
    too_long = "设" * (secretary.MAX_PERSONA_LEN + 1)
    created = bridge.create_secretary_session("新会话", "星河", too_long)
    assert created["ok"] is False
    secretary.invalidate_secretary_cache()
    assert [s for s in manager.get_all_sessions() if s["source"] == "SECRETARY"] == []

    manager.create_session_explicit("s_plain_bridge", workdir="GLOBAL", source="GUI")
    secretary.invalidate_secretary_cache()
    assert bridge.set_secretary_persona("s_plain_bridge", "人设")["ok"] is False


def test_thread_session_wins_over_ui_current_session(manager):
    manager.create_session_explicit(
        "s_ui_secretary", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.create_session_explicit("s_dispatched", workdir="GLOBAL", source="GUI")
    secretary.invalidate_secretary_cache()

    app_context.set_current_session("s_ui_secretary")
    app_context.set_thread_session("s_dispatched")
    try:
        assert secretary.is_secretary_session() is False
        assert secretary.get_active_agent_name() != "星河"
        assert secretary.is_secretary_session("s_ui_secretary") is True
    finally:
        app_context.set_thread_session(None)

    app_context.set_current_session("s_dispatched")
    app_context.set_thread_session("s_ui_secretary")
    try:
        assert secretary.is_secretary_session() is True
        assert secretary.get_active_agent_name() == "星河"
    finally:
        app_context.set_thread_session(None)

    app_context.set_current_session("s_ui_secretary")
    assert secretary.is_secretary_session() is True
    app_context.set_current_session("s_dispatched")
    assert secretary.is_secretary_session() is False
    app_context.set_current_session("")


def test_dispatched_turn_gets_full_toolset(manager):
    manager.create_session_explicit(
        "s_ui_secretary", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.create_session_explicit("s_dispatched", workdir="GLOBAL", source="GUI")
    secretary.invalidate_secretary_cache()

    import core.llm as llm

    def _schema_names():
        return {
            str(schema.get("function", {}).get("name", ""))
            for schema in llm.get_tools_schema()
        }

    app_context.set_current_session("s_ui_secretary")
    app_context.set_thread_session("s_dispatched")
    try:
        names = _schema_names()
        assert {"execute_command", "write_file", "edit_file"} <= names
        assert "dispatch_task" not in names
    finally:
        app_context.set_thread_session(None)

    app_context.set_current_session("s_dispatched")
    app_context.set_thread_session("s_ui_secretary")
    try:
        names = _schema_names()
        assert "dispatch_task" in names
        assert "execute_command" not in names
        assert "write_file" not in names
    finally:
        app_context.set_thread_session(None)
        app_context.set_current_session("")
