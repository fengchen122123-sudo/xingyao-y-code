"""core/agent_service.py 与相关解耦改动的单元测试。

沙盒约束：模块顶层不 import litellm；对 estimate_tokens 等重依赖一律打桩。
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.app_context as app_context
from core.memory import MemoryManager


# ---------------------------------------------------------------------------
# 打桩：core.llm 里只用到 estimate_tokens / session_token_usage / token_usage_lock
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_llm(monkeypatch):
    """把 core.llm 换成轻量桩，避免测试环境加载 litellm。"""
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
    monkeypatch.setitem(sys.modules, "core.llm", stub)
    return stub


@pytest.fixture
def quiet_extras(monkeypatch):
    """屏蔽运行上下文里的重依赖（技能扫描 / 记忆 / 子代理注册表 / 后台任务）。"""
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


def _make_manager():
    return MemoryManager(":memory:")


def test_prepare_turn_messages_orders_system_and_dynamic_context(stub_llm, quiet_extras):
    """system 提示词在最前，动态运行上下文紧贴最后一条 user 消息之前。"""
    from core.agent_service import prepare_turn_messages

    manager = _make_manager()
    session = "test_prepare"
    manager.add_message(session, "user", "第一个问题")
    manager.add_message(session, "assistant", "第一次回答")
    manager.add_message(session, "user", "第二个问题")

    messages = prepare_turn_messages(
        manager,
        session,
        "openai/gpt-test",
        128000,
        permission_mode="confirm",
    )

    # 1) 第一条必须是稳定 system 提示词
    assert messages[0]["role"] == "system"
    assert len(messages[0]["content"]) > 0

    # 2) 最后一条仍然是用户本轮输入
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "第二个问题"

    # 3) 动态上下文是倒数第二条，且确实是运行上下文块
    dynamic = messages[-2]
    assert dynamic["role"] == "system"
    assert "【当前运行上下文】" in dynamic["content"]
    assert "当前权限模式：confirm" in dynamic["content"]

    # 4) 历史中不残留上一轮的 system 消息（只有新插入的两条）
    assert sum(1 for m in messages if m["role"] == "system") == 2


def test_prepare_turn_messages_updates_token_usage(stub_llm, quiet_extras):
    """准备阶段会把估算出的窗口占用写回 session_token_usage。"""
    from core.agent_service import prepare_turn_messages

    manager = _make_manager()
    session = "test_tokens"
    manager.add_message(session, "user", "你好" * 10)

    prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="full"
    )
    assert stub_llm.session_token_usage["latest_total"] > 0


def test_evict_oldest_turn_removes_complete_rounds_only():
    """滑窗淘汰必须以 user 消息为轮次边界。"""
    from core.agent_service import evict_oldest_turn

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "t1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    assert evict_oldest_turn(history) is True
    assert [m["role"] for m in history] == ["system", "user", "assistant"]
    assert history[1]["content"] == "u2"


def test_normalize_permission_mode_maps_legacy_and_chinese_labels():
    """权限模式规范化：只允许 confirm / auto / plan / full 四个值。"""
    assert app_context.normalize_permission_mode("confirm") == "confirm"
    assert app_context.normalize_permission_mode("AUTO") == "auto"
    assert app_context.normalize_permission_mode("plan") == "plan"
    assert app_context.normalize_permission_mode("full") == "full"

    # 历史遗留 / 中文标签
    assert app_context.normalize_permission_mode("default") == "confirm"
    assert app_context.normalize_permission_mode("默认模式") == "confirm"
    assert app_context.normalize_permission_mode("自动接受编辑") == "auto"
    assert app_context.normalize_permission_mode("计划模式") == "plan"
    assert app_context.normalize_permission_mode("YOLO (全部允许)") == "full"

    # 未知值与空值一律回落到最保守的 confirm，绝不放宽权限
    assert app_context.normalize_permission_mode("") == "confirm"
    assert app_context.normalize_permission_mode(None) == "confirm"
    assert app_context.normalize_permission_mode("随便乱写") == "confirm"


def test_set_permission_mode_stores_canonical_value():
    """setter 必须落规范值，getter 读回同一个规范值。"""
    original = app_context.get()
    try:
        app_context.register(None)
        assert app_context.set_permission_mode("自动接受编辑") == "auto"
        assert app_context.get_permission_mode() == "auto"
        assert app_context.set_permission_mode("YOLO") == "full"
        assert app_context.get_permission_mode() == "full"
    finally:
        app_context.register(original)
        app_context.set_permission_mode("confirm")


def test_active_model_tuple_roundtrip():
    """set/get_active_model_tuple 供 core 层读取当前激活模型。"""
    app_context.set_active_model_tuple("openai/gpt-x", "sk-test", "https://api.example.com")
    assert app_context.get_active_model_tuple() == (
        "openai/gpt-x", "sk-test", "https://api.example.com",
    )


def test_clear_session_history_removes_messages_and_summary():
    """清空历史后消息为空，且记忆摘要游标一并重置。"""
    manager = _make_manager()
    session = "test_clear"
    manager.add_message(session, "user", "问题一")
    manager.add_message(session, "assistant", "回答一")
    assert len(manager.get_history(session)) == 2

    deleted = manager.clear_session_messages(session)
    assert deleted == 2
    assert manager.get_history(session) == []
    # 会话本身仍在，可继续使用
    assert manager.clear_session_messages(session) == 0
    assert manager.get_history_for_llm(session) == []

    # 不存在的会话安全返回 0
    assert manager.clear_session_messages("不存在的会话") == 0


def test_ensure_session_is_idempotent():
    """ensure_session 首次创建返回 True，重复调用返回 False。"""
    manager = _make_manager()
    assert manager.ensure_session("gui_boot", workdir="GLOBAL", source="GUI") is True
    assert manager.ensure_session("gui_boot", workdir="GLOBAL", source="GUI") is False
    sessions = {s["name"]: s for s in manager.get_all_sessions(workdir=None, include_empty=True)}
    assert sessions["gui_boot"]["workdir"] == "GLOBAL"


def test_undo_last_edit_restores_previous_content(tmp_path, monkeypatch):
    """撤销：把文件恢复为旧内容并抹掉上一轮对话记忆。"""
    from core import memory as memory_mod
    from core.agent_service import undo_last_edit

    manager = _make_manager()
    session = "test_undo"
    manager.add_message(session, "user", "改个文件")
    manager.add_message(session, "assistant", "改好了")

    target = tmp_path / "demo.txt"
    target.write_text("新内容", encoding="utf-8")

    memory_mod.set_undo_session(session)
    memory_mod.set_undo_state(str(target), "旧内容", session_name=session)

    # write_file 受工作目录沙盒限制，这里直接打桩验证撤销流程
    import core.tools as tools_mod
    written = {}

    def _fake_write(path, content):
        written["path"] = path
        written["content"] = content
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return "ok"

    monkeypatch.setattr(tools_mod, "write_file", _fake_write, raising=True)

    ok, msg = undo_last_edit(manager, session)
    assert ok is True, msg
    assert target.read_text(encoding="utf-8") == "旧内容"
    # 撤销后不可重复撤销
    ok2, msg2 = undo_last_edit(manager, session)
    assert ok2 is False
    assert "没有可以撤销" in msg2


def test_undo_last_edit_requires_force_to_delete_new_file(tmp_path):
    """删除星瑶新建的文件属于高危操作，未确认时必须拒绝。"""
    from core import memory as memory_mod
    from core.agent_service import undo_last_edit

    manager = _make_manager()
    session = "test_undo_force"
    target = tmp_path / "created.txt"
    target.write_text("星瑶新建的内容", encoding="utf-8")

    memory_mod.set_undo_session(session)
    memory_mod.set_undo_state(str(target), "", session_name=session)

    ok, msg = undo_last_edit(manager, session, force=False)
    assert ok is False
    assert "需要显式确认" in msg
    assert target.exists()

    ok, msg = undo_last_edit(manager, session, force=True)
    assert ok is True, msg
    assert not target.exists()
