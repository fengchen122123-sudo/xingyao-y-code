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
    """system 提示词在最前，动态运行上下文与时间注记紧贴最后一条 user 消息之前。"""
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

    # 3) 动态上下文与时间注记依次位于用户消息之前
    dynamic = messages[-3]
    time_note = messages[-2]
    assert dynamic["role"] == "system"
    assert "【当前运行上下文】" in dynamic["content"]
    assert "当前权限模式：confirm" in dynamic["content"]
    # 时间已从动态块剥离，单独成块
    assert "当前时间" not in dynamic["content"]
    assert time_note["role"] == "system"
    assert time_note["content"].startswith("【当前时间】")

    # 4) 首轮恰好三条 system：稳定前缀 + 动态块 + 时间注记
    assert sum(1 for m in messages if m["role"] == "system") == 3


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


def test_compression_summary_survives_as_user_message(stub_llm, quiet_extras):
    """压缩摘要必须以 user 角色到达模型（不被 without_system_messages 误删）。"""
    from core.agent_service import prepare_turn_messages

    manager = _make_manager()
    session = "test_summary_reaches_model"
    manager.add_message(session, "user", "早期问题")
    manager.add_message(session, "assistant", "早期回答")
    manager.add_message(session, "user", "本轮问题")
    # 模拟压缩器写回的摘要与边界游标
    with manager._lock:
        manager.conn.execute(
            "UPDATE sessions SET memory_summary = ?, summarized_until_msg_id = 0 WHERE session_name = ?",
            ("用户之前在调试数据库连接问题。", session),
        )
        manager.conn.commit()

    messages = prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )

    summaries = [m for m in messages if "历史对话记忆摘要" in str(m.get("content", ""))]
    assert len(summaries) == 1
    assert summaries[0]["role"] == "user"
    assert "调试数据库连接问题" in summaries[0]["content"]
    # system 消息为稳定前缀 + 动态块 + 时间注记三条
    assert sum(1 for m in messages if m["role"] == "system") == 3


def test_mcp_warmup_starts_background_connect_once(monkeypatch):
    """MCP 预热：有启用的 server 时后台触发一次连接，且进程内不重复。"""
    import time

    import core.agent_service as agent_service

    calls = []

    class FakeManager:
        config = {"mcpServers": {"srv": {"command": "x"}}}
        servers = {}

        def _load_config(self):
            pass

        def _ensure_connected(self):
            calls.append(1)

    monkeypatch.setattr("core.mcp_client.mcp_manager", FakeManager())
    monkeypatch.setattr(agent_service, "_MCP_WARMUP_STARTED", False)

    agent_service._warmup_mcp_schemas_once()
    time.sleep(0.3)
    assert calls == [1]

    agent_service._warmup_mcp_schemas_once()
    time.sleep(0.1)
    assert calls == [1]


def test_mcp_warmup_skips_when_no_enabled_servers(monkeypatch):
    """MCP 预热：没有启用的 server（空配置或全部 disabled）时不触发连接。"""
    import core.agent_service as agent_service

    calls = []

    class FakeManager:
        config = {"mcpServers": {"srv": {"command": "x", "enabled": False}}}
        servers = {}

        def _load_config(self):
            pass

        def _ensure_connected(self):
            calls.append(1)

    monkeypatch.setattr("core.mcp_client.mcp_manager", FakeManager())
    monkeypatch.setattr(agent_service, "_MCP_WARMUP_STARTED", False)

    agent_service._warmup_mcp_schemas_once()
    assert calls == []


# ---------------------------------------------------------------------------
# append-only 上下文块：跨轮 prefix 连续性（缓存命中率改造）
# ---------------------------------------------------------------------------


def test_dynamic_context_not_duplicated_when_unchanged(stub_llm, quiet_extras):
    """运行上下文不变时第二轮不再重注动态块；时间注记每轮新增、旧的保留。"""
    from core.agent_service import prepare_turn_messages

    manager = _make_manager()
    session = "test_append_only"

    manager.add_message(session, "user", "问题一")
    prepare_turn_messages(manager, session, "openai/gpt-test", 128000, permission_mode="confirm")
    manager.add_message(session, "assistant", "回答一")
    manager.add_message(session, "user", "问题二")
    messages = prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )

    dynamics = [m for m in messages if "【当前运行上下文】" in str(m.get("content", ""))]
    time_notes = [
        m for m in messages if str(m.get("content", "")).startswith("【当前时间】")
    ]
    assert len(dynamics) == 1
    assert len(time_notes) == 2


def test_dynamic_context_appended_when_changed(stub_llm, quiet_extras):
    """运行上下文变化时追加新块，旧块保留（append-only），最新块更靠近当前输入。"""
    from core.agent_service import prepare_turn_messages

    manager = _make_manager()
    session = "test_dynamic_changed"

    manager.add_message(session, "user", "问题一")
    prepare_turn_messages(manager, session, "openai/gpt-test", 128000, permission_mode="confirm")
    manager.add_message(session, "assistant", "回答一")
    manager.add_message(session, "user", "问题二")
    messages = prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="full"
    )

    dynamics = [m for m in messages if "【当前运行上下文】" in str(m.get("content", ""))]
    assert len(dynamics) == 2
    # 旧块在前、新块在后，新块包含最新权限模式
    assert "当前权限模式：confirm" in dynamics[0]["content"]
    assert "当前权限模式：full" in dynamics[1]["content"]
    # 最新动态块位于最后一条 user 消息附近（其后只有时间注记与 user）
    last_dynamic_idx = max(
        i for i, m in enumerate(messages) if "【当前运行上下文】" in str(m.get("content", ""))
    )
    assert last_dynamic_idx >= len(messages) - 3


def test_prefix_continuity_across_turns(stub_llm, quiet_extras):
    """核心目标：下一轮请求的消息列表必须以前一轮的完整消息列表为前缀。"""
    from core.agent_service import prepare_turn_messages

    manager = _make_manager()
    session = "test_prefix_continuity"

    manager.add_message(session, "user", "问题一")
    turn1 = prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )
    manager.add_message(session, "assistant", "回答一")
    manager.add_message(session, "user", "问题二")
    turn2 = prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )

    # turn2 的前 len(turn1) 条与 turn1 完全一致 → provider 侧前缀缓存可全部命中
    assert turn2[: len(turn1)] == turn1
    # 后续依次为：上一轮的 assistant 回复、新一轮的时间注记、新一轮的用户输入
    assert turn2[len(turn1)]["role"] == "assistant"
    assert turn2[len(turn1)]["content"] == "回答一"


def test_reposition_context_blocks_moves_system_before_user():
    """DB 自然顺序 [user][块] 在内存中还原为 [块][user]，多块保持相对顺序。"""
    from core.agent_service import _reposition_context_blocks

    history = [
        {"role": "user", "content": "问题"},
        {"role": "system", "content": "【当前运行上下文】..."},
        {"role": "system", "content": "【当前时间】..."},
        {"role": "assistant", "content": "回答"},
    ]
    out = _reposition_context_blocks(history)
    assert [m["role"] for m in out] == ["system", "system", "user", "assistant"]
    assert out[0]["content"].startswith("【当前运行上下文】")
    assert out[1]["content"].startswith("【当前时间】")


def test_dynamic_prompt_excludes_time_and_marks_latest_wins():
    """动态块不含每轮必变的时间字段，且声明多份并存时以最新为准。"""
    from core.context_builder import RuntimeContext, build_dynamic_prompt, build_time_note

    runtime = RuntimeContext(
        model_name="m",
        permission_mode="confirm",
        working_dir="/tmp",
        current_time="2026-08-03 10:00:00",
    )
    dynamic = build_dynamic_prompt(runtime)
    assert "当前时间" not in dynamic
    assert "以最新一份为准" in dynamic
    assert build_time_note(runtime) == "【当前时间】2026-08-03 10:00:00"


def test_prepare_turn_messages_estimates_tokens_with_system_prompt(stub_llm, quiet_extras):
    """压缩阈值估算必须把每轮现组的系统提示词计入，与实际请求口径一致。"""
    from core.agent_service import prepare_turn_messages

    captured = []
    original = stub_llm.estimate_tokens

    def spy(model, messages):
        captured.append(messages)
        return original(model, messages)

    stub_llm.estimate_tokens = spy

    manager = _make_manager()
    session = "test_estimate_system"
    manager.add_message(session, "user", "你好")

    prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )

    assert captured, "prepare 阶段应至少估算一次 Token"
    first_messages = captured[0]
    # 估算列表首位必须是稳定系统提示词（不落库、每轮现组，纯历史估算会漏掉它）
    assert first_messages[0]["role"] == "system"
    assert len(first_messages[0]["content"]) > 0
    # 历史消息仍在估算范围内
    assert any(
        m.get("role") == "user" and m.get("content") == "你好" for m in first_messages
    )


def test_estimate_history_tokens_with_system_includes_system_prompt(stub_llm):
    """TUI/GUI 重算窗口占用的共享口径：高于纯历史估算，差额即系统提示词。"""
    from core.agent_service import estimate_history_tokens_with_system

    history = [{"role": "user", "content": "你好"}]
    history_only = stub_llm.estimate_tokens("openai/gpt-test", history)
    with_system = estimate_history_tokens_with_system("openai/gpt-test", history)
    assert with_system > history_only


def test_prepare_turn_messages_uses_real_usage_baseline(stub_llm, quiet_extras):
    """有真实用量记录时：压缩判断与窗口占用都用真实 prompt+completion，不再估算。"""
    from core.agent_service import prepare_turn_messages

    manager = _make_manager()
    session = "test_real_baseline"
    manager.add_message(session, "user", "你好")
    manager.add_token_usage("openai", "gpt-test", 8000, 500, 0, 8500, session_id=session)

    def boom(model, messages):
        raise AssertionError("有真实基线时不应再走本地估算")

    stub_llm.estimate_tokens = boom

    prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )
    # 窗口占用 = 最近一次真实 prompt + completion（下一轮请求必然包含上轮回复）
    assert stub_llm.session_token_usage["latest_total"] == 8500


def test_prepare_turn_messages_real_baseline_triggers_compression(stub_llm, quiet_extras, monkeypatch):
    """真实基线超过 0.95 水位时触发压缩；触发后才用估算指导后续收缩。"""
    import core.memory_compressor as memory_compressor
    from core.agent_service import prepare_turn_messages

    calls = []
    monkeypatch.setattr(
        memory_compressor,
        "compress_session_memory",
        lambda *args, **kwargs: calls.append(args),
    )

    manager = _make_manager()
    session = "test_real_baseline_compress"
    manager.add_message(session, "user", "你好")
    # 四级水位线后付费摘要只在 0.95 触发：128000 * 0.95 = 121600，真实基线 124500 超阈值
    manager.add_token_usage("openai", "gpt-test", 123500, 1000, 0, 124500, session_id=session)

    prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )
    assert len(calls) == 1


def test_prepare_turn_real_baseline_below_095_skips_compression(stub_llm, quiet_extras, monkeypatch):
    """真实基线处于 0.60~0.95 之间且无可折叠工具结果时：不触发付费摘要。"""
    import core.memory_compressor as memory_compressor
    from core.agent_service import prepare_turn_messages

    calls = []
    monkeypatch.setattr(
        memory_compressor,
        "compress_session_memory",
        lambda *args, **kwargs: calls.append(args),
    )

    manager = _make_manager()
    session = "test_real_baseline_no_compress"
    manager.add_message(session, "user", "你好")
    # 95000 / 128000 ≈ 0.74：过 snip 水位但没有工具结果可维护，未到 0.95 摘要水位
    manager.add_token_usage("openai", "gpt-test", 94000, 1000, 0, 95000, session_id=session)

    prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )
    assert calls == []


def test_get_session_token_baseline_prefers_real_usage(stub_llm):
    """共享基线：有真实记录直接返回 prompt+completion，不调用估算。"""
    from core.agent_service import get_session_token_baseline

    manager = _make_manager()
    manager.add_token_usage("openai", "m1", 1200, 300, 0, 1500, session_id="s1")

    def boom(model, messages):
        raise AssertionError("不应调用估算")

    stub_llm.estimate_tokens = boom
    assert get_session_token_baseline(manager, "s1", "openai/m1") == 1500


def test_get_session_token_baseline_falls_back_to_estimate(stub_llm):
    """共享基线：无真实记录（老会话首次）退化到含系统提示词的估算。"""
    from core.agent_service import get_session_token_baseline

    manager = _make_manager()
    manager.add_message("s1", "user", "你好")

    baseline = get_session_token_baseline(manager, "s1", "openai/m1")
    history_only = stub_llm.estimate_tokens(
        "openai/m1", [{"role": "user", "content": "你好"}]
    )
    assert baseline > history_only


# ---------------------------------------------------------------------------
# 四级水位线阶梯（0.60 snip / 0.80 elide / 0.95 LLM 摘要）
# ---------------------------------------------------------------------------


def _fill_big_tool_results(manager, session, count):
    """写入 count 组 read_file 大结果（assistant tool_call + tool 配对，id 唯一）。"""
    big = "\n".join(f"line-{i} " + "x" * 40 for i in range(300))
    manager.add_message(session, "user", "开始")
    for idx in range(count):
        manager.add_messages(session, [
            ("assistant", {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call-{idx}", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]}),
            ("tool", {"role": "tool", "tool_call_id": f"call-{idx}",
                      "name": "read_file", "content": big}),
        ])
    manager.add_message(session, "user", "继续")


def test_prepare_turn_snips_before_summarizing(stub_llm, quiet_extras, monkeypatch):
    """水位过 0.60 但未过 0.95 时：只做本地 snip，绝不调用 LLM 摘要。"""
    import core.memory_compressor as memory_compressor
    from core.agent_service import prepare_turn_messages
    from core.tool_result_maintenance import SNIPPED_PREFIX

    calls = []
    monkeypatch.setattr(
        memory_compressor,
        "compress_session_memory",
        lambda *args, **kwargs: calls.append(args),
    )

    manager = _make_manager()
    session = "tier-ladder"
    _fill_big_tool_results(manager, session, 5)
    # 窗口 100000：snip 水位 60000 < 初始估算；snip 后回落到 80000(elide)/95000(摘要) 之下
    prepare_turn_messages(
        manager, session, "deepseek-chat", 100000, permission_mode="auto"
    )
    assert calls == []  # 未触发付费摘要
    history = manager.get_history_for_llm(session)
    assert any(
        m.get("role") == "tool" and str(m.get("content", "")).startswith(SNIPPED_PREFIX)
        for m in history
    )


def test_prepare_turn_summarizes_only_above_095(stub_llm, quiet_extras, monkeypatch):
    """超过 0.95 且本地 snip/elide 无能为力（无工具结果）时，才调用 LLM 摘要。"""
    import core.memory_compressor as memory_compressor
    from core.agent_service import prepare_turn_messages

    calls = []
    monkeypatch.setattr(
        memory_compressor,
        "compress_session_memory",
        lambda *args, **kwargs: calls.append(args),
    )

    manager = _make_manager()
    session = "tier-summarize"
    # 用大量普通 user/assistant 消息（非 tool，snip 无能为力）制造高压
    for i in range(40):
        manager.add_message(session, "user", f"问题{i} " + "汉" * 300)
        manager.add_message(session, "assistant", f"回答{i} " + "汉" * 300)
    prepare_turn_messages(
        manager, session, "deepseek-chat", 16000, permission_mode="auto"
    )
    assert len(calls) == 1
