"""core/agent_service.py 与相关解耦改动的单元测试。

沙盒约束：模块顶层不 import litellm；对 estimate_tokens 等重依赖一律打桩。
"""

import os
import sys
import types

import hashlib

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
    # Task 10：build_runtime_context 从 core.llm 导入 Turn 能力解析；
    # 桩返回 None（capability 关闭）保持动态上下文字节与历史版本一致。
    stub.resolve_turn_computer_capability = lambda *args, **kwargs: None
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
    """system 提示词唯一且在最前，动态上下文与时间注记内联到最后一条 user 消息。"""
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

    # 1) 第一条且唯一一条 system：稳定提示词
    assert messages[0]["role"] == "system"
    assert len(messages[0]["content"]) > 0
    assert sum(1 for m in messages if m["role"] == "system") == 1

    # 2) 最后一条仍是用户本轮输入，动态运行上下文 + 时间注记已内联到其 content
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"].startswith("第二个问题")
    assert "【当前运行上下文】" in messages[-1]["content"]
    assert "当前权限模式：confirm" in messages[-1]["content"]
    assert "【当前时间】" in messages[-1]["content"]

    # 3) 历史 user 消息不含动态块（动态块只内联到最后一条 user）
    assert "【当前运行上下文】" not in messages[1]["content"]


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


def test_prepare_turn_messages_does_not_prune_history_on_normal_read(
    monkeypatch, stub_llm, quiet_extras
):
    from core.agent_service import prepare_turn_messages

    manager = _make_manager()
    session = "test_prepare_read_only"
    manager.add_message(session, "user", "正常低水位消息")
    calls = []
    monkeypatch.setattr(
        manager, "prune_history_files", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )

    assert calls == []


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
    """权限模式规范化：只允许 confirm / accept_edits / plan / full 四个值。"""
    assert app_context.normalize_permission_mode("confirm") == "confirm"
    assert app_context.normalize_permission_mode("accept_edits") == "accept_edits"
    assert app_context.normalize_permission_mode("plan") == "plan"
    assert app_context.normalize_permission_mode("full") == "full"

    # 历史遗留 / 中文标签
    assert app_context.normalize_permission_mode("default") == "confirm"
    assert app_context.normalize_permission_mode("默认模式") == "confirm"
    assert app_context.normalize_permission_mode("accept-edits") == "accept_edits"
    assert app_context.normalize_permission_mode("自动编辑") == "accept_edits"
    assert app_context.normalize_permission_mode("计划模式") == "plan"
    assert app_context.normalize_permission_mode("YOLO (全部允许)") == "full"

    # 未知值、空值与已下线的历史 auto 模式一律回落到最保守的 confirm，绝不放宽权限
    assert app_context.normalize_permission_mode("") == "confirm"
    assert app_context.normalize_permission_mode(None) == "confirm"
    assert app_context.normalize_permission_mode("随便乱写") == "confirm"
    assert app_context.normalize_permission_mode("AUTO") == "confirm"


def test_set_permission_mode_stores_canonical_value():
    """setter 必须落规范值，getter 读回同一个规范值。"""
    original = app_context.get()
    try:
        app_context.register(None)
        assert app_context.set_permission_mode("计划模式") == "plan"
        assert app_context.get_permission_mode() == "plan"
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


def _begin_undo_turn(tmp_path, session, turn):
    """新按回合撤销：配置服务并开始一个绑定当前线程的回合检查点。"""
    from core.memory import MemoryManager
    from core.turn_undo import service as turn_undo_service
    from core.turn_undo.models import OwnerKind

    manager = MemoryManager(str(tmp_path / f"ycode-{turn}.db"))
    storage = turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / f"undo_blobs-{turn}"
    )
    manager.add_messages(session, [("user", "hi")])
    session_id = storage.resolve_session_id(session)
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=session_id,
        turn_id=turn,
        workspace_root=str(tmp_path),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    return storage, checkpoint, ctx


def turn_undo_service_finish(storage, ctx):
    from core.turn_undo import service as turn_undo_service

    return turn_undo_service.finish_turn(ctx)


def test_write_file_records_undo_state(tmp_path, monkeypatch):
    """D3：write_file 也必须记录按回合撤销状态。

    新建文件与覆盖已有文件都要产生精确的 before/after 记录；
    删除新建文件的确认语义由多文件事务与预检承担（见 turn_undo 事务测试）。
    """
    from core.tools import write_file

    session = "test_write_file_undo"
    monkeypatch.chdir(tmp_path)

    new_file = tmp_path / "created-by-write.txt"
    old_file = tmp_path / "existing.txt"
    old_file.write_text("旧内容v1", encoding="utf-8")

    storage, checkpoint, ctx = _begin_undo_turn(tmp_path, session, "turn-write-1")
    try:
        # 1) 新建文件：记录 before=missing
        result = write_file(str(new_file), "新内容")
        assert "写入成功" in result
        # 2) 覆盖已有文件：记录最初 before 字节
        result = write_file(str(old_file), "旧内容v1\n追加新内容")
        assert "写入成功" in result
    finally:
        from core.turn_undo.models import TurnUndoStatus

        assert turn_undo_service_finish(storage, ctx) is TurnUndoStatus.READY

    files = {row.display_path: row for row in storage.list_files(checkpoint.checkpoint_id)}
    assert set(files) == {"created-by-write.txt", "existing.txt"}
    created = files["created-by-write.txt"]
    assert created.before_kind == "missing"
    assert created.after_sha256 == hashlib.sha256("新内容".encode("utf-8")).hexdigest()
    existing = files["existing.txt"]
    assert existing.before_sha256 == hashlib.sha256("旧内容v1".encode("utf-8")).hexdigest()
    assert existing.after_sha256 == hashlib.sha256(
        "旧内容v1\n追加新内容".encode("utf-8")
    ).hexdigest()
    assert existing.verified_at_ms is not None


def turn_undo_service_finish(storage, ctx):
    from core.turn_undo import service as turn_undo_service

    return turn_undo_service.finish_turn(ctx)


def test_write_file_undo_preserves_preexisting_empty_file(tmp_path, monkeypatch):
    """覆盖前已存在的空文件必须以“存在但为空”的精确 before 状态入记录。"""
    from core.tools import write_file
    from core.turn_undo.models import TurnUndoStatus

    session = "test_write_file_empty_undo"
    target = tmp_path / "empty.txt"
    target.write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    storage, checkpoint, ctx = _begin_undo_turn(tmp_path, session, "turn-empty-1")
    try:
        assert "写入成功" in write_file(str(target), "new content")
    finally:
        assert turn_undo_service_finish(storage, ctx) is TurnUndoStatus.READY

    row = storage.list_files(checkpoint.checkpoint_id)[0]
    assert row.before_kind == "regular_file"
    assert row.before_exists == 1
    assert row.before_size == 0
    assert row.before_sha256 == hashlib.sha256(b"").hexdigest()
    assert target.read_bytes() == b"new content"


def test_write_file_undo_restores_original_non_utf8_bytes(tmp_path, monkeypatch):
    """撤销记录必须保存覆盖前的原始字节，不得把解码替换字符写入快照。"""
    from core.tools import write_file
    from core.turn_undo.models import TurnUndoStatus

    session = "test_write_file_gbk_undo"
    target = tmp_path / "legacy-gbk.txt"
    original = "旧版中文内容".encode("gbk")
    target.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    storage, checkpoint, ctx = _begin_undo_turn(tmp_path, session, "turn-gbk-1")
    try:
        assert "写入成功" in write_file(str(target), "replacement")
    finally:
        assert turn_undo_service_finish(storage, ctx) is TurnUndoStatus.READY

    row = storage.list_files(checkpoint.checkpoint_id)[0]
    assert row.before_sha256 == hashlib.sha256(original).hexdigest()
    # 快照内容是原始 GBK 字节，而非 UTF-8 解码替换后的文本
    assert storage.blob_store.read_bytes(row.before_blob_sha256) == original
    assert target.read_bytes() == b"replacement"


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
    # system 消息只有稳定前缀一条（动态块与时间注记均已内联到 user 消息）
    assert sum(1 for m in messages if m["role"] == "system") == 1


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


def test_dynamic_context_inlined_to_each_user_turn(stub_llm, quiet_extras):
    """动态上下文块内联到每条 user 消息末尾，作为各自轮次的运行环境快照。"""
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
    # 两条 user 各内联一份动态块（各自轮次的运行环境快照）
    assert len(dynamics) == 2
    assert all(m["role"] == "user" for m in dynamics)
    time_notes_in_users = [
        m for m in messages
        if m.get("role") == "user" and "【当前时间】" in str(m.get("content", ""))
    ]
    assert len(time_notes_in_users) == 2


def test_dynamic_context_appended_when_changed(stub_llm, quiet_extras):
    """运行上下文变化时，新 user 消息内联最新动态块，旧 user 保留旧块。"""
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
    # 旧 user 内联 confirm 块、新 user 内联 full 块
    assert "当前权限模式：confirm" in dynamics[0]["content"]
    assert "当前权限模式：full" in dynamics[1]["content"]
    # 最新动态块内联在最后一条 user 消息里
    assert "当前权限模式：full" in messages[-1]["content"]


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
    # 后续依次为：上一轮的 assistant 回复、新一轮的用户输入（含内联时间注记）
    assert turn2[len(turn1)]["role"] == "assistant"
    assert turn2[len(turn1)]["content"] == "回答一"


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
    # 历史消息仍在估算范围内（user 消息含内联时间注记后缀）
    assert any(
        m.get("role") == "user" and "你好" in str(m.get("content", "")) for m in first_messages
    )


def test_estimate_history_tokens_with_system_includes_system_prompt(stub_llm):
    """CLI/GUI 重算窗口占用的共享口径：高于纯历史估算，差额即系统提示词。"""
    from core.agent_service import estimate_history_tokens_with_system

    history = [{"role": "user", "content": "你好"}]
    history_only = stub_llm.estimate_tokens("openai/gpt-test", history)
    with_system = estimate_history_tokens_with_system("openai/gpt-test", history)
    assert with_system > history_only


def test_prepare_turn_messages_uses_real_usage_baseline(stub_llm, quiet_extras):
    """小历史下真实基线小于估算：取较小真实值作基线，latest_total 跟随该基线。"""
    from core.agent_service import prepare_turn_messages

    manager = _make_manager()
    session = "test_real_baseline"
    manager.add_message(session, "user", "你好")
    manager.add_token_usage("openai", "gpt-test", 8000, 500, 0, 8500, session_id=session)

    prepare_turn_messages(
        manager, session, "openai/gpt-test", 128000, permission_mode="confirm"
    )
    # latest_total 取基线（真实 8500 与含系统提示词估算的较小值），不超真实用量
    latest = stub_llm.session_token_usage["latest_total"]
    assert 0 < latest <= 8500


def test_prepare_turn_messages_real_baseline_triggers_compression(stub_llm, quiet_extras, monkeypatch):
    """历史已膨胀到超阈值（真实与估算的较小值仍超 0.95）时触发压缩。"""
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
    # 写入足够大的历史，使含系统提示词的估算也超过 0.95 水位（128000*0.95=121600）
    big = "x" * 500000
    manager.add_message(session, "user", big)
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
    """共享基线：真实用量小于估算时取真实值（较小值）。"""
    from core.agent_service import get_session_token_baseline

    manager = _make_manager()
    manager.add_token_usage("openai", "m1", 1200, 300, 0, 1500, session_id="s1")

    assert get_session_token_baseline(manager, "s1", "openai/m1") == 1500


def test_get_session_token_baseline_takes_min_of_real_and_estimate(stub_llm):
    """共享基线：真实用量大于估算时取估算（切小窗口模型后的关键场景）。

    大窗口模型留下的高真实基线（124500）对当前小历史是过期高值，应退回
    含系统提示词的估算（取两者较小），否则压缩对小窗口永远不生效。
    """
    from core.agent_service import get_session_token_baseline

    manager = _make_manager()
    manager.add_message("s1", "user", "你好")
    manager.add_token_usage("openai", "m1", 123500, 1000, 0, 124500, session_id="s1")

    baseline = get_session_token_baseline(manager, "s1", "openai/m1")
    # 真实 124500 远超小历史估算（约数千），应取估算而非真实值
    assert baseline < 124500


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


def test_get_session_token_baseline_discards_real_usage_from_other_model(
    stub_llm, monkeypatch
):
    """切到分词更贵的模型时，旧模型的小 real 不能压低新模型估算。"""
    import core.agent_service as agent_service

    manager = _make_manager()
    manager.add_token_usage(
        "openai", "old-model", 80, 20, 0, 100, session_id="s1"
    )
    monkeypatch.setattr(
        agent_service,
        "estimate_history_tokens_with_system",
        lambda _model, _history: 900,
    )

    assert agent_service.get_session_token_baseline(
        manager, "s1", "openai/new-model", history=[]
    ) == 900


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
        manager, session, "deepseek-chat", 100000, permission_mode="confirm"
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
        manager, session, "deepseek-chat", 16000, permission_mode="confirm"
    )
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 取消语义：用户取消的任务绝不注入"接手完成"指令
# ---------------------------------------------------------------------------


def test_bg_summary_cancelled_task_not_takeover(monkeypatch):
    """cancelled 任务只如实列出并标注无需接手；failed 任务仍要求接手完成。"""
    from types import SimpleNamespace

    import core.tools as core_tools
    from core import agent_service

    tasks = [
        SimpleNamespace(
            task_id="t-cancel", status="cancelled", description="查新闻",
            result="", error="task cancelled",
        ),
        SimpleNamespace(
            task_id="t-failed", status="failed", description="抓网页",
            result="", error="boom",
        ),
    ]
    monkeypatch.setattr(
        core_tools.subagent_manager,
        "drain_terminal_background_tasks",
        lambda session: tasks,
    )

    summary = agent_service.build_background_task_summary("s1")

    cancelled_section = summary.split("【任务 t-cancel")[1].split("【任务 t-failed")[0]
    assert "已取消" in cancelled_section
    # “无需接手完成”本身含“接手完成”子串，断言必须用指令前缀区分
    assert "请用自身工具" not in cancelled_section
    assert "无需接手" in cancelled_section

    failed_section = summary.split("【任务 t-failed")[1]
    assert "请用自身工具" in failed_section


def test_failed_compression_severe_overflow_trips_circuit_breaker(tmp_path, monkeypatch, stub_llm):
    """当记忆压缩失败且历史严重超出模型窗口时，熔断停止滑窗淘汰，保护原始历史完整。"""
    from core.agent_service import prepare_turn_messages
    from core.memory import MemoryManager

    db = tmp_path / "cb.db"
    manager = MemoryManager(str(db))
    session = "s_cb"
    manager.create_session_explicit(session, workdir="GLOBAL", source="GUI")

    # 写入 10 轮对话（20 条消息）
    for i in range(10):
        manager.add_message(session, "user", f"用户提问第 {i} 轮 " + "内容 " * 50)
        manager.add_message(session, "assistant", f"模型回答第 {i} 轮 " + "细节 " * 50)

    notices = []
    # 模拟记忆压缩失败（返回 False）
    monkeypatch.setattr(
        "core.memory_compressor.compress_session_memory",
        lambda *a, **kw: False,
    )
    # 模拟 estimate_tokens 返回一个远超窗口的 token 数（如窗口 1000，当前 5000）
    monkeypatch.setattr(
        "core.llm.estimate_tokens",
        lambda model, msgs: 5000,
    )
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline",
        lambda *a, **kw: 5000,
    )

    prepare_turn_messages(
        manager,
        session,
        "openai/test-small",
        1000,
        permission_mode="confirm",
        notify=lambda k, msg: notices.append(msg),
    )

    # 验证触发了熔断提示
    assert any("停止自动淘汰历史" in n or "保护您的历史对话完整" in n for n in notices)
    # 验证历史消息没有被全部清空（仍然保留了全部消息）
    active_history = manager.get_history_for_llm(session)
    assert len(active_history) >= 20


def test_compress_already_running_no_rate_limit_error(tmp_path, monkeypatch, stub_llm):
    """当后台已有压缩正在执行（already_running）时，绝不误报服务商速率限制，保护 DB 水位，走内存裁剪。"""
    from core.agent_service import prepare_turn_messages
    from core.memory import MemoryManager
    from core.memory_compressor import CompressionOutcome

    db = tmp_path / "running.db"
    manager = MemoryManager(str(db))
    session = "s_run"
    manager.create_session_explicit(session, workdir="GLOBAL", source="GUI")

    for i in range(10):
        manager.add_message(session, "user", f"用户提问第 {i} 轮 " + "内容 " * 50)
        manager.add_message(session, "assistant", f"模型回答第 {i} 轮 " + "细节 " * 50)

    notices = []
    # 模拟防重入返回 already_running
    monkeypatch.setattr(
        "core.memory_compressor.compress_session_memory",
        lambda *a, **kw: CompressionOutcome("skipped", "already_running"),
    )
    monkeypatch.setattr(
        "core.llm.estimate_tokens",
        lambda model, msgs: 5000,
    )
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline",
        lambda *a, **kw: 5000,
    )

    prepare_turn_messages(
        manager,
        session,
        "openai/test-small",
        1000,
        permission_mode="confirm",
        notify=lambda k, msg: notices.append(msg),
    )

    # 验证：绝不误报速率限制/失败
    assert not any("未能成功" in n or "速率限制" in n for n in notices)
    # 验证：提示后台正在进行中
    assert any("后台记忆压缩仍在进行中" in n for n in notices)
    # 验证：DB 历史完整保留
    active_history = manager.get_history_for_llm(session)
    assert len(active_history) >= 20


def test_evict_max_turns_constraint(tmp_path, monkeypatch, stub_llm):
    """滑窗淘汰受 MAX_EVICT_TURNS 约束，单次最多淘汰 2 轮，严禁无节制清空会话。"""
    from core.agent_service import prepare_turn_messages, MAX_EVICT_TURNS
    from core.memory import MemoryManager
    from core.memory_compressor import CompressionOutcome

    db = tmp_path / "evict.db"
    manager = MemoryManager(str(db))
    session = "s_evict"
    manager.create_session_explicit(session, workdir="GLOBAL", source="GUI")

    for i in range(10):
        manager.add_message(session, "user", f"用户第 {i} 轮 " + "A" * 50)
        manager.add_message(session, "assistant", f"助手第 {i} 轮 " + "B" * 50)

    evict_calls = []
    orig_evict = manager.evict_oldest_turn_persistent
    def tracking_evict(s):
        evict_calls.append(s)
        return orig_evict(s)

    manager.evict_oldest_turn_persistent = tracking_evict

    monkeypatch.setattr(
        "core.memory_compressor.compress_session_memory",
        lambda *a, **kw: CompressionOutcome("compressed", "ok"),
    )
    monkeypatch.setattr(
        "core.llm.estimate_tokens",
        lambda model, msgs: 5000,
    )
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline",
        lambda *a, **kw: 5000,
    )

    prepare_turn_messages(
        manager,
        session,
        "openai/test-small",
        1000,
        permission_mode="confirm",
    )

    assert len(evict_calls) == MAX_EVICT_TURNS


def test_prune_history_in_memory_preserves_summary_and_last_turn():
    """内存级裁剪：保留历史记忆摘要和最新一轮，裁剪最早轮次直到 Token 满足阈值。"""
    from core.agent_service import _prune_history_in_memory_to_fit_window

    history = [
        {"role": "user", "content": "[历史对话记忆摘要 (以下是前段长对话的归纳记忆...)]\n这是重要背景摘要"},
        {"role": "user", "content": "最早的问题 1"},
        {"role": "assistant", "content": "最早的回答 1"},
        {"role": "user", "content": "中间的问题 2"},
        {"role": "assistant", "content": "中间的回答 2"},
        {"role": "user", "content": "最新的问题 3"},
    ]
    system_msg = {"role": "system", "content": "system prompt"}

    def mock_estimate(model, msgs):
        return len(msgs) * 100

    import core.llm
    orig_estimate = core.llm.estimate_tokens
    try:
        core.llm.estimate_tokens = mock_estimate
        # window=400, SUMMARIZE_RATIO=0.95 -> limit = 380 tokens
        # [system] + 6 msgs = 700 tokens > 380
        # 裁剪 1 轮（最早的问题1 + 回答1）后 -> 5 msgs = 500 tokens > 380
        # 裁剪 2 轮（中间的问题2 + 回答2）后 -> 3 msgs (system + 摘要 + 最新问题3) = 300 <= 380 -> 结束
        pruned, remaining = _prune_history_in_memory_to_fit_window(
            history, window=400, model_name="test", system_message=system_msg,
            estimated_tokens=700,
        )
        assert pruned == 2
        assert remaining == 300
        # 摘要必须保留在开头
        assert history[0]["content"].startswith("[历史对话记忆摘要")
        # 最新问题必须保留在末尾
        assert history[-1]["content"] == "最新的问题 3"
        # 最早和中间的轮次被剔除
        assert not any(m["content"] == "最早的问题 1" for m in history)
        assert not any(m["content"] == "中间的问题 2" for m in history)
    finally:
        core.llm.estimate_tokens = orig_estimate


@pytest.mark.parametrize("reason", ["timeout", "cancelled"])
def test_zero_summary_skipped_forbids_db_eviction(tmp_path, monkeypatch, stub_llm, reason):
    """skipped(timeout/cancelled) 意味着零新增摘要，等同 failed：严禁推 DB 水位。"""
    from core.agent_service import prepare_turn_messages
    from core.memory import MemoryManager
    from core.memory_compressor import CompressionOutcome

    manager = MemoryManager(str(tmp_path / (reason + ".db")))
    session = "s_" + reason
    manager.create_session_explicit(session, workdir="GLOBAL", source="GUI")
    for i in range(10):
        manager.add_message(session, "user", "提问 %d " % i + "内容 " * 50)
        manager.add_message(session, "assistant", "回答 %d " % i + "细节 " * 50)

    evict_calls = []
    monkeypatch.setattr(manager, "evict_oldest_turn_persistent",
                        lambda s: evict_calls.append(s) or False)
    monkeypatch.setattr("core.memory_compressor.compress_session_memory",
                        lambda *a, **kw: CompressionOutcome("skipped", reason))
    monkeypatch.setattr("core.llm.estimate_tokens",
                        lambda model, msgs: 5000)
    monkeypatch.setattr("core.agent_service.get_session_token_baseline",
                        lambda *a, **kw: 5000)

    prepare_turn_messages(manager, session, "openai/test-small", 1000,
                          permission_mode="confirm", notify=lambda k, m: None)

    assert evict_calls == []          # 一次都不许推 DB 水位
    assert len(manager.get_history_for_llm(session)) >= 20


def test_prune_history_in_memory_estimates_incrementally():
    """裁剪过程不得对全量历史反复重扫：全量估算次数必须与裁剪轮数解耦。"""
    import core.agent_service as ag
    import core.llm as llm

    history = [{"role": "user", "content": "[历史对话记忆摘要 (背景)]\n摘要保留"}]
    for i in range(12):
        history.append({"role": "user", "content": "问题 %d " % i + "内容 " * 400})
        history.append({"role": "assistant", "content": "回答 %d " % i + "细节 " * 400})

    full_scans = {"n": 0}
    def counting_estimate(model, msgs):
        if len(msgs) > 4:
            full_scans["n"] += 1
        return len(msgs) * 800

    orig = llm.estimate_tokens
    llm.estimate_tokens = counting_estimate
    try:
        pruned, remaining = ag._prune_history_in_memory_to_fit_window(
            history, 4000, "test/model", {"role": "system", "content": "s"},
            len(history) * 800,
        )
    finally:
        llm.estimate_tokens = orig

    assert pruned >= 5
    assert full_scans["n"] <= 2          # 只允许收尾校验，不许每轮全量重扫
    assert remaining <= 4000 * 0.95       # SUMMARIZE_RATIO
    assert history[0]["content"].startswith("[历史对话记忆摘要")


def test_build_referenced_sessions_block_content():
    from core.agent_service import build_referenced_sessions_block, REF_SESSIONS_PREFIX
    mgr = _make_manager()
    mgr.ensure_session("目标对话")
    mgr.add_message("目标对话", "user", "登录接口报 500 怎么查")
    mgr.add_message("目标对话", "assistant", "先看 gateway 日志，再查鉴权中间件")
    block = build_referenced_sessions_block(mgr, ["目标对话"], current_session="当前对话")
    assert block.startswith(REF_SESSIONS_PREFIX)
    assert "目标对话" in block
    assert "登录接口报 500" in block
    assert "先看 gateway 日志" in block


def test_build_referenced_sessions_block_skip_and_missing():
    from core.agent_service import build_referenced_sessions_block
    mgr = _make_manager()
    mgr.ensure_session("当前对话")
    block = build_referenced_sessions_block(
        mgr, ["当前对话", "不存在的对话"], current_session="当前对话"
    )
    assert "已跳过" in block  # 自引用与不存在的会话都如实标注，不抛错


def test_build_referenced_sessions_block_empty_refs():
    from core.agent_service import build_referenced_sessions_block
    assert build_referenced_sessions_block(_make_manager(), []) == ""


def test_get_session_memory_summary(tmp_path):
    from core.memory import MemoryManager
    mgr = MemoryManager(str(tmp_path / "t.db"))
    mgr.ensure_session("s1")
    assert mgr.get_session_memory_summary("s1") == ""
    assert mgr.get_session_memory_summary("不存在") == ""

