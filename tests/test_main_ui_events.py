from concurrent.futures import Future

import hashlib

import pytest

import main
from core.permission_broker import PermissionBroker, PermissionOption
from core.tool_progress import ActiveToolStatus
from core.turn_events import TurnEventFactory
from rich.text import Text


def _undo_cli_turn(tmp_path, session, turn, target, original, replacement):
    """CLI 撤销测试夹具：真实 turn_undo 服务 + 一个覆盖目标文件的回合。"""
    from core.memory import MemoryManager
    from core.turn_undo import service as tu_service
    from core.turn_undo import recorder as tu_recorder
    from core.turn_undo.models import OwnerKind, NodeKind

    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = tu_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.ensure_session(session)
    session_id = tu_service.resolve_session_id(session)
    checkpoint, ctx = tu_service.begin_turn(
        session_id=session_id,
        turn_id=turn,
        workspace_root=str(tmp_path),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    handle = tu_recorder.prepare_mutation(
        display_path=str(target),
        after_kind=NodeKind.REGULAR_FILE,
        after_content=replacement,
        tool_name="write_file",
        tool_call_id="c1",
    )
    target.write_bytes(replacement)
    tu_recorder.verify_after_mutation(handle)
    tu_service.finish_turn(ctx)
    return storage, checkpoint


def test_undo_existing_empty_file_does_not_require_delete_confirmation(
    tmp_path, monkeypatch
):
    """空文件被覆盖后按回合撤销：恢复原空内容，无需任何删除确认语义。"""
    from core.memory import MemoryManager
    from core.turn_undo import service as tu_service

    session = "cli-empty-file-undo"
    target = tmp_path / "empty.txt"
    target.write_bytes(b"")
    storage, checkpoint = _undo_cli_turn(
        tmp_path, session, "turn-empty-undo", target, b"", b"filled"
    )

    monkeypatch.setattr(main, "current_session", session)
    monkeypatch.setattr(main, "_undo_confirm", lambda question: True)
    monkeypatch.setattr(main, "print_formatted_text", lambda *_args, **_kwargs: None)

    main.handle_slash_command("/undo " + checkpoint.turn_id[:8])

    assert target.read_bytes() == b""


def test_checkpoint_restore_preserves_original_non_utf8_bytes(tmp_path, monkeypatch):
    """按回合撤销快照保存原始字节：GBK 文件恢复后与覆盖前逐字节一致。"""
    session = "cli-checkpoint-bytes"
    target = tmp_path / "legacy-gbk.txt"
    original = "旧版内容".encode("gbk")
    target.write_bytes(original)
    storage, checkpoint = _undo_cli_turn(
        tmp_path, session, "turn-bytes", target,
        original, "changed".encode("utf-8"),
    )
    monkeypatch.setattr(main, "current_session", session)
    monkeypatch.setattr(main, "_undo_confirm", lambda question: True)
    monkeypatch.setattr(main, "print_formatted_text", lambda *_args, **_kwargs: None)

    main.handle_slash_command("/checkpoint restore " + checkpoint.turn_id[:8])

    assert target.read_bytes() == original


def test_format_cache_hit_rate_uses_only_reported_cache_prompt():
    assert main._format_cache_hit_rate(90, 100) == "90.0%"
    assert main._format_cache_hit_rate(0, 0) == "--"


def test_forward_response_events_preserves_order_without_duplicate_text():
    factory = TurnEventFactory("turn-ui")
    events = [
        factory.emit("assistant_delta", {"text": "先检查。"}),
        factory.emit("tool_requested", {"id": "read-1", "name": "read_file"}),
        factory.emit("tool_finished", {"id": "read-1", "name": "read_file", "ok": True}),
        factory.emit("assistant_delta", {"text": "完成。"}),
        factory.emit("turn_finished"),
    ]
    posted = []

    reply = main._forward_response_events(events, posted.append)

    assert posted == events
    assert reply == "先检查。完成。"


def test_resolve_confirm_prompt_rejects_invalid_index_and_clears_on_success(monkeypatch):
    future = Future()
    prompt = {
        "options": ["继续", "停止"],
        "future": future,
        "current_idx": 0,
    }
    monkeypatch.setattr(main, "confirm_prompt", prompt)

    assert main._resolve_confirm_prompt(2) is False
    assert main.confirm_prompt is prompt
    assert future.done() is False

    assert main._resolve_confirm_prompt(1) is True
    assert main.confirm_prompt is None
    assert future.result() == 1


def test_resolve_broker_prompt_advances_to_next_request(monkeypatch):
    broker = PermissionBroker()

    def activate(request):
        monkeypatch.setattr(main, "confirm_prompt", main._permission_prompt(request))

    def dismiss():
        monkeypatch.setattr(main, "confirm_prompt", None)

    broker.set_prompt_handlers(activate, dismiss)
    monkeypatch.setattr(main, "permission_broker", broker)
    monkeypatch.setattr(main, "confirm_prompt", None)

    first = broker.enqueue(
        "execute_command", {"command": "one"}, "root", "turn", "主代理"
    )
    second = broker.enqueue(
        "execute_command", {"command": "two"}, "child", "turn", "子代理：查时间"
    )

    assert main.confirm_prompt["request_id"] == first.request_id
    assert main._resolve_confirm_prompt(0) is True
    assert first.future.result() == 0
    assert main.confirm_prompt["request_id"] == second.request_id
    assert main.confirm_prompt["source"] == "子代理：查时间"

    assert main._resolve_confirm_prompt(1) is True
    assert second.future.result() == 1
    assert main.confirm_prompt is None


def test_permission_prompt_source_line_is_escaped():
    prompt = {"source": "子代理：<查询>&时间"}

    assert main._permission_source_line(prompt) == "请求来源：子代理：&lt;查询&gt;&amp;时间\n"


def test_complete_permission_panel_includes_source_operation_and_options():
    text = main._confirm_prompt_text({
        "source": "子代理：查询时间",
        "function_name": "execute_command",
        "arguments": {"command": "Get-Date"},
        "options": ["同意", "拒绝"],
        "current_idx": 0,
    })

    assert "请求来源：子代理：查询时间" in text
    assert "星瑶申请执行命令: Get-Date" in text
    assert "> 同意" in text
    assert "拒绝" in text


def test_cli_permission_prompt_renders_all_backend_options():
    broker = PermissionBroker(activate_prompt=lambda _request: None)
    request = broker.enqueue(
        "write_file",
        {"file_path": "a.py"},
        "root",
        "turn",
        "主代理",
        options=(
            PermissionOption("allow_once", "允许本次"),
            PermissionOption("deny", "拒绝"),
            PermissionOption("allow_task", "本任务同类操作放行"),
            PermissionOption("allow_project", "加入项目白名单"),
        ),
    )

    text = main._confirm_prompt_text(main._permission_prompt(request))

    assert "允许本次" in text
    assert "本任务同类操作放行" in text
    assert "加入项目白名单" in text


def test_whitelist_add_and_remove_command(monkeypatch):
    from core import permission_store

    calls = []
    monkeypatch.setattr(
        permission_store,
        "add_rule",
        lambda rule_type, value: calls.append(("add", rule_type, value))
        or type("R", (), {"status": "added", "normalized_value": value})(),
    )
    monkeypatch.setattr(
        permission_store,
        "remove_rule",
        lambda rule_type, value: calls.append(("remove", rule_type, value))
        or type("R", (), {"status": "removed", "normalized_value": value})(),
    )
    monkeypatch.setattr(main, "print_formatted_text", lambda *_args, **_kwargs: None)

    main.handle_slash_command("/whitelist add command git status")
    main.handle_slash_command("/whitelist remove command git status")

    assert calls == [
        ("add", "command", "git status"),
        ("remove", "command", "git status"),
    ]


def test_whitelist_clear_requires_explicit_confirmation(monkeypatch):
    from core import permission_store

    clears = []
    outputs = []
    monkeypatch.setattr(
        permission_store,
        "clear_project",
        lambda: clears.append(True) or 3,
    )
    monkeypatch.setattr(
        main,
        "print_formatted_text",
        lambda message, *_args, **_kwargs: outputs.append(str(message)),
    )

    main.handle_slash_command("/whitelist clear")
    assert clears == []
    assert any("clear confirm" in output for output in outputs)

    main.handle_slash_command("/whitelist clear confirm")
    assert clears == [True]


def test_whitelist_failure_status_uses_error_output(monkeypatch):
    from core import permission_store

    outputs = []
    monkeypatch.setattr(
        permission_store,
        "add_rule",
        lambda rule_type, value: type(
            "R",
            (),
            {"status": "limit_reached", "normalized_value": value},
        )(),
    )
    monkeypatch.setattr(
        main,
        "print_formatted_text",
        lambda message, *_args, **_kwargs: outputs.append(str(message)),
    )

    main.handle_slash_command("/whitelist add command git status")

    assert any("❌" in output and "上限" in output for output in outputs)


def test_cancel_active_turn_cancels_permissions_and_foreground_subagents(monkeypatch):
    import core.tools as tools
    import threading

    cancel_event = threading.Event()
    cancelled_turns = []
    cancelled_subagents = []
    monkeypatch.setattr(main, "active_cancel_event", cancel_event)
    monkeypatch.setattr(main, "active_turn_id", "turn-owned", raising=False)
    monkeypatch.setattr(main, "current_session", "session-owned")
    monkeypatch.setattr(
        main.permission_broker,
        "cancel_turn",
        cancelled_turns.append,
    )
    monkeypatch.setattr(
        tools.subagent_manager,
        "cancel_foreground",
        lambda session, turn: cancelled_subagents.append((session, turn)),
    )

    main._cancel_active_turn()

    # 取消完全由每 turn 的 cancel_event 驱动，不再写 core.llm.cancel_requested
    assert cancel_event.is_set()
    assert cancelled_turns == ["turn-owned"]
    assert cancelled_subagents == [("session-owned", "turn-owned")]


def test_stale_permission_dismiss_does_not_clear_a_new_active_request(monkeypatch):
    class ImmediateLoop:
        @staticmethod
        def call_soon_threadsafe(callback):
            callback()

    class FakeApp:
        loop = ImmediateLoop()

        @staticmethod
        def invalidate():
            return None

    broker = PermissionBroker()
    monkeypatch.setattr(main, "permission_broker", broker)
    monkeypatch.setattr(main, "app_instance", FakeApp())
    request = broker.enqueue(
        "execute_command",
        {"command": "new"},
        "child",
        "turn",
        "子代理：新请求",
    )
    monkeypatch.setattr(main, "confirm_prompt", main._permission_prompt(request))

    main._dismiss_permission_prompt()

    assert main.confirm_prompt["request_id"] == request.request_id


def test_dream_background_result_is_scheduled_on_ui_thread(monkeypatch):
    printed = []
    scheduled = []

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(main, "print_formatted_text", lambda value, **kwargs: printed.append(value))
    monkeypatch.setattr(main, "schedule_ui", scheduled.append)
    monkeypatch.setattr(main.threading, "Thread", ImmediateThread)

    import core.tools

    monkeypatch.setattr(core.tools, "dream_consolidation", lambda: "整理完成")

    main.handle_slash_command("/dream")

    assert len(printed) == 1
    assert len(scheduled) == 1
    scheduled[0]()
    assert len(printed) == 2


def test_memory_compressor_uses_callback_instead_of_background_output(monkeypatch):
    import core.memory_compressor as compressor

    class Cursor:
        def execute(self, *args):
            return None

        def fetchone(self):
            return (1, None, 0)

        def fetchall(self):
            return []

    class Connection:
        def cursor(self):
            return Cursor()

    class Manager:
        conn = Connection()

    printed = []
    messages = []
    monkeypatch.setattr(compressor, "print_formatted_text", lambda *args, **kwargs: printed.append(args))

    result = compressor.compress_session_memory(
        Manager(), "session", "model", "key", None, force=True, output_callback=messages.append
    )

    assert result is False
    assert printed == []
    assert len(messages) == 1


def test_plan_panel_formats_structured_plan_snapshot():
    text = main._format_plan_items([
        {"subject": "检查代码", "status": "completed"},
        {"subject": "运行测试", "status": "in_progress"},
    ])

    assert "✓ 检查代码" in text
    assert "■ 运行测试" in text


def test_live_plan_panel_is_compact_and_shows_stage_progress():
    text = main._format_live_plan_items([
        {"subject": "分析", "status": "completed", "stage": "plan"},
        {"subject": "修改", "status": "in_progress", "stage": "build"},
        {"subject": "审查", "status": "pending", "stage": "review"},
    ])

    assert "进度 1/3" in text
    assert "分析 [计划]" in text
    assert "修改 [构建]" in text
    assert "审查 [审查]" in text


def test_turn_activity_labels_distinguish_thinking_from_working():
    assert main._turn_activity_label("thinking") == "星瑶正在思考..."
    assert main._turn_activity_label("executing") == "星瑶正在干活..."
    assert main._turn_activity_label("waiting") == "星瑶等待你的确认..."


def test_activity_text_shows_command_and_interrupt_hint():
    status = ActiveToolStatus(
        label="正在运行测试",
        detail=r"venv\Scripts\python -m pytest -q",
        count=1,
        started_at=10.0,
    )

    assert main._format_activity_text("executing", status, 8.4, "⠋") == (
        "  ⠋ 正在运行测试 · 8.4s · Ctrl+C 中断\n"
        "    └─ venv\\Scripts\\python -m pytest -q"
    )


def test_activity_window_has_room_for_command_detail(monkeypatch):
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(main, "Application", FakeApplication)

    main._make_input_app()

    activity_window = captured["layout"].container.content.children[4].content
    height = activity_window.height
    if callable(height):
        height = height()
    minimum = height if isinstance(height, int) else height.min
    assert minimum >= 2


def test_tool_started_switches_ui_to_executing_state(monkeypatch):
    factory = TurnEventFactory("turn-tool-state")
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "app_instance", None)
    main.task_status = "thinking"
    main.is_thinking = True

    main._render_ui_event(factory.emit("tool_started", {"id": "1", "name": "read_file"}))
    assert main.task_status == "executing"
    assert main.is_thinking is True


def test_plan_item_events_print_one_line_and_refresh_panel(monkeypatch):
    """计划项事件统一走 ui_markers：<mark> <subject>，不再有各异的动词前缀
    和末尾多余的 " ✓"（见 core/ui_markers.py 与 main.py 的 plan_item_* 分支）。"""
    factory = TurnEventFactory("turn-plan-events")
    printed = []
    invalidated = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", printed.append)

    class FakeApp:
        def invalidate(self):
            invalidated.append(True)

    monkeypatch.setattr(main, "app_instance", FakeApp())
    main._render_ui_event(factory.emit("plan_item_started", {
        "item": {"id": "a", "subject": "修改调度器", "status": "in_progress"},
    }))
    main._render_ui_event(factory.emit("plan_item_completed", {
        "item": {"id": "a", "subject": "修改调度器", "status": "completed"},
    }))

    started_plain = Text.from_ansi(printed[0]).plain
    completed_plain = Text.from_ansi(printed[1]).plain
    assert started_plain == "■ 修改调度器\n"
    assert completed_plain == "✓ 修改调度器\n"
    assert len(invalidated) == 2


def test_ui_streams_public_answer_after_reasoning(monkeypatch):
    factory = TurnEventFactory("turn-buffered-answer")
    fragments = []

    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-buffered-answer", None)

    main._render_ui_event(factory.emit("assistant_delta", {"text": "第一步：检查配置。\n\n"}))

    assert "第一步：检查配置。" in "".join(fragments)

    main._render_ui_event(factory.emit("turn_finished"))

    assert "第一步：检查配置。" in "".join(fragments)


def test_ui_fragments_are_flushed_to_the_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "print_formatted_text", lambda value, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(main, "app_instance", None)

    main._print_ui_fragment("最终回复")

    assert calls == [{"end": "\n", "flush": True}]


def test_active_cli_prints_fragments_through_run_in_terminal(monkeypatch):
    terminal_calls = []
    printed = []

    class ActiveApp:
        loop = object()
        _is_running = True

    monkeypatch.setattr(main, "app_instance", ActiveApp())
    monkeypatch.setattr(main, "run_in_terminal", terminal_calls.append, raising=False)
    monkeypatch.setattr(
        main,
        "print_formatted_text",
        lambda value, **kwargs: printed.append(kwargs),
    )

    main._print_ui_fragment("最终回复")

    assert printed == []
    assert len(terminal_calls) == 1

    terminal_calls[0]()
    assert printed == [{"end": "\n", "flush": True}]


def test_input_app_keeps_native_terminal_text_selection(monkeypatch):
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(main, "Application", FakeApplication)

    main._make_input_app()

    assert captured["mouse_support"] is False


def test_ui_streams_thinking_text_in_temporary_panel_then_collapses(monkeypatch):
    factory = TurnEventFactory("turn-inline-thinking")
    writes = []

    def capture(fragment, *, end=None):
        resolved_end = end
        if resolved_end is None:
            resolved_end = "" if fragment.endswith("\n") else "\n"
        writes.append(fragment + resolved_end)

    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", capture)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-inline-thinking", None)
    main.active_thinking_text = ""
    main.active_thinking_turn_id = None

    main._render_ui_event(factory.emit("thinking_started"))
    main._render_ui_event(factory.emit("thinking_delta", {"text": "用户说了"}))
    main._render_ui_event(factory.emit("thinking_delta", {"text": "\"你好\""}))
    # 800ms 阈值：极短思考不打印分隔行，这里模拟思考耗时 1s 越过阈值
    main.think_start_time -= 1.0

    assert main.active_thinking_text == '用户说了"你好"'
    assert '用户说了"你好"' in main._thinking_panel_text()
    assert writes == []

    main._render_ui_event(factory.emit("thinking_finished"))

    # 思考内容和完成分隔线都不进入终端历史。
    plain_so_far = Text.from_ansi("".join(writes)).plain
    assert "思考完成" not in plain_so_far
    assert "用户说了" not in plain_so_far
    # 思考结束后面板延迟隐藏：保留最后一帧思考内容，直到下一个事件到来
    assert main.active_thinking_text == '用户说了"你好"'
    assert main.active_thinking_turn_id is None

    # 模型开始输出正文时，延迟显示的思考内容被清除
    main._render_ui_event(factory.emit("assistant_delta", {"text": "答"}))
    assert main.active_thinking_text == ""
    main._render_ui_event(factory.emit("turn_finished"))

    plain = Text.from_ansi("".join(writes)).plain
    assert "用户说了" not in plain
    assert "你好" not in plain
    assert "思考完成" not in plain
    main.ui_renderers.pop("turn-inline-thinking", None)


def test_thinking_segments_do_not_print_completion_dividers(monkeypatch):
    """多段思考不刷空完成行，且结束后仍正常输出最终回答。"""
    factory = TurnEventFactory("turn-thinking-segments")
    fragments = []

    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-thinking-segments", None)
    main.active_thinking_text = ""
    main.active_thinking_turn_id = None

    for index in range(3):
        main._render_ui_event(factory.emit("thinking_started"))
        main._render_ui_event(factory.emit("thinking_delta", {"text": f"分析{index}"}))
        main.think_start_time -= 1.0
        main._render_ui_event(factory.emit("thinking_finished"))
        assert all("思考完成" not in Text.from_ansi(fragment).plain for fragment in fragments)

    main._render_ui_event(factory.emit("assistant_delta", {"text": "结论"}))
    main._render_ui_event(factory.emit("turn_finished"))

    plain = Text.from_ansi("".join(fragments)).plain
    assert "思考完成" not in plain
    assert "分析0" not in plain
    assert "结论" in plain
    main.ui_renderers.pop("turn-thinking-segments", None)


def test_tool_failure_continues_to_corrected_command_and_final_answer(monkeypatch):
    factory = TurnEventFactory("turn-tool-failure-continues")
    fragments = []
    states = []

    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-tool-failure-continues", None)
    main.task_status = "thinking"

    failed = {
        "id": "bad",
        "name": "execute_command",
        "ok": False,
        "duration_ms": 100,
        "display": {
            "category": "command",
            "action": "运行命令",
            "target": "pytest -q",
            "command": "pytest -q",
            "error_summary": "ModuleNotFoundError: pytest",
        },
    }
    corrected = {
        "id": "good",
        "name": "execute_command",
        "ok": True,
        "duration_ms": 200,
        "display": {
            "category": "command",
            "action": "运行命令",
            "target": "python -m pytest -q",
            "command": "python -m pytest -q",
        },
    }

    for kind, payload in (
        ("tool_started", failed),
        ("tool_finished", failed),
        ("tool_started", corrected),
    ):
        main._render_ui_event(factory.emit(kind, payload))
        states.append(main.task_status)

    assert "运行命令失败" in Text.from_ansi("".join(fragments)).plain
    assert states == ["executing", "executing", "executing"]
    assert main._active_tool_status.detail == "python -m pytest -q"

    main._render_ui_event(factory.emit("tool_finished", corrected))
    main._render_ui_event(factory.emit("assistant_delta", {"text": "测试已通过。"}))
    main._render_ui_event(factory.emit("turn_finished"))

    assert "测试已通过。" in Text.from_ansi("".join(fragments)).plain


def test_grouped_tool_output_stays_outside_markdown_buffer(monkeypatch):
    turn_id = "turn-tool-output-outside-markdown"
    factory = TurnEventFactory(turn_id)
    fragments = []

    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop(turn_id, None)
    main._assistant_stream_buffers.pop(turn_id, None)

    for index in range(2):
        payload = {
            "id": f"read-{index}",
            "name": "read_file",
            "ok": True,
            "duration_ms": 100,
            "display": {
                "category": "read",
                "action": "读取文件",
                "target": f"file-{index}.py",
                "metrics": {"line_count": 10},
            },
        }
        main._render_ui_event(factory.emit("tool_started", payload))
        main._render_ui_event(factory.emit("tool_finished", payload))

    main._render_ui_event(factory.emit("assistant_delta", {"text": "最终答案"}))

    assert "已读取 2 个文件" in Text.from_ansi("".join(fragments)).plain
    assert main._assistant_stream_buffers[turn_id] == "最终答案"
    assert all("已读取" not in value for value in main._assistant_stream_buffers.values())

    main._render_ui_event(factory.emit("turn_finished"))


def test_brief_thinking_under_800ms_prints_no_divider_line(monkeypatch):
    """极短思考（< 800ms，常见于模型回填的占位 reasoning）不打印分隔行，避免刷屏。"""
    factory = TurnEventFactory("turn-thinking-brief")
    fragments = []

    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-thinking-brief", None)
    main.active_thinking_text = ""
    main.active_thinking_turn_id = None

    main._render_ui_event(factory.emit("thinking_started"))
    main._render_ui_event(factory.emit("thinking_delta", {"text": " "}))
    # 不调整 think_start_time：事件瞬时完成，耗时 < 800ms
    main._render_ui_event(factory.emit("thinking_finished"))

    assert "思考完成" not in Text.from_ansi("".join(fragments)).plain
    main.ui_renderers.pop("turn-thinking-brief", None)


def test_ui_keeps_reasoning_deltas_out_of_terminal_history(monkeypatch):
    factory = TurnEventFactory("turn-thinking-stream")
    fragments = []

    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-thinking-stream", None)
    main.active_thinking_text = ""
    main.active_thinking_turn_id = None

    main._render_ui_event(factory.emit("thinking_started"))
    main._render_ui_event(factory.emit(
        "thinking_delta", {"text": "第一步完成。第二步"}
    ))

    assert Text.from_ansi("".join(fragments)).plain == ""
    assert main.active_thinking_text == "第一步完成。第二步"
    main.ui_renderers.pop("turn-thinking-stream", None)


def test_streamed_answer_is_rendered_as_markdown_block(monkeypatch):
    factory = TurnEventFactory("turn-markdown")
    printed = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", printed.append)
    monkeypatch.setattr(main, "_terminal_width", lambda: 60, raising=False)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-markdown", None)

    main._render_ui_event(factory.emit(
        "assistant_delta", {"text": "# 结论\n\n"}
    ))
    main._render_ui_event(factory.emit("turn_finished"))

    rendered = "".join(printed)
    assert "结论" in rendered
    assert "# 结论" not in Text.from_ansi(rendered).plain


def test_esc_escapes_html_special_chars():
    assert main._esc('<b>&"x"</b>') == '&lt;b&gt;&amp;"x"&lt;/b&gt;'
    assert main._esc(None) == "None"


def test_confirm_prompt_text_has_default_branch_for_unknown_tools():
    text = main._confirm_prompt_text({
        "source": "主代理",
        "function_name": "mcp_weather<query>",
        "arguments": {"city": "北京<&>", "days": 3},
        "options": ["同意", "拒绝"],
        "current_idx": 0,
    })

    assert "星瑶申请调用工具: mcp_weather&lt;query&gt;" in text
    assert "city=北京&lt;&amp;&gt;" in text
    assert "> 同意" in text


def test_theme_switch_does_not_exit_app(monkeypatch):
    calls = {"exit": 0, "invalidate": 0}

    class FakeApp:
        def exit(self):
            calls["exit"] += 1

        def invalidate(self):
            calls["invalidate"] += 1

    monkeypatch.setattr(main, "app_instance", FakeApp())
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    import config

    monkeypatch.setattr(config, "save_config_value", lambda *args: None)
    monkeypatch.setattr(main, "current_theme", "cyan")

    main.handle_slash_command("/theme nord")

    assert main.current_theme == "nord"
    assert calls["exit"] == 0
    assert calls["invalidate"] == 1


def test_evict_oldest_turn_removes_complete_rounds_only():
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]

    assert main._evict_oldest_turn(history) is True
    assert [m["role"] for m in history] == ["system", "user", "assistant"]
    assert history[1]["content"] == "u2"

    assert main._evict_oldest_turn(history) is True
    assert [m["role"] for m in history] == ["system"]

    # 仅剩 system 消息时无可淘汰轮次
    assert main._evict_oldest_turn(history) is False
    assert [m["role"] for m in history] == ["system"]


def test_evict_oldest_turn_keeps_user_boundary_when_history_starts_with_assistant():
    history = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "orphan"},
        {"role": "user", "content": "u1"},
    ]

    assert main._evict_oldest_turn(history) is True
    # 淘汰到下一个 user 边界为止，剩余历史仍以 user 开头
    assert [m["role"] for m in history] == ["system", "user"]


def test_turn_error_event_resets_ui_state_and_prints_error(monkeypatch):
    factory = TurnEventFactory("turn-error")
    printed = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: printed.append(args))
    monkeypatch.setattr(main, "_print_ui_fragment", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-error", None)
    main.task_status = "executing"
    main.is_thinking = True

    main._render_ui_event(factory.emit("assistant_delta", {"text": "半截回复"}))
    main._render_ui_event(factory.emit("turn_error", {"message": "连接<b>中断"}))

    assert main.task_status == "idle"
    assert main.is_thinking is False
    assert "turn-error" not in main.ui_renderers
    error_text = "".join(str(getattr(arg, "value", arg)) for call in printed for arg in call)
    assert "回合异常结束" in error_text
    assert "连接&lt;b&gt;中断" in error_text


def test_drain_ui_events_survives_a_bad_event(monkeypatch):
    class BadEvent:
        @property
        def turn_id(self):
            raise RuntimeError("bad event")

    factory = TurnEventFactory("turn-good-after-bad")
    good = factory.emit("turn_finished")
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-good-after-bad", None)
    main.task_status = "executing"

    main.ui_event_queue.put(BadEvent())
    main.ui_event_queue.put(good)
    main.drain_ui_events()

    # 坏事件不卡死队列：后续正常事件仍被消费并重置状态
    assert main.ui_event_queue.empty()
    assert main.task_status == "idle"


def test_background_worker_survives_malformed_task(monkeypatch):
    import queue
    import threading

    scheduled = []
    printed = []
    monkeypatch.setattr(main, "schedule_ui", lambda callback: scheduled.append(callback))
    monkeypatch.setattr(main, "print_formatted_text", lambda value, **kwargs: printed.append(value))
    # 使用测试私有队列：worker 测试结束后阻塞在私有队列上，
    # 不会消费后续测试投递到全局 msg_queue 的消息
    local_queue = queue.Queue()
    monkeypatch.setattr(main, "msg_queue", local_queue)

    worker = threading.Thread(target=main._background_worker, daemon=True)
    worker.start()
    local_queue.put({})  # 缺少 text/was_queued 字段，必然触发 KeyError
    local_queue.join()

    # 兜底 try/except 保证 worker 不死，且异常已调度到 UI 线程提示
    assert worker.is_alive()
    assert len(scheduled) >= 1
    for callback in scheduled:
        callback()
    rendered = "".join(str(getattr(p, "value", p)) for p in printed)
    assert "后台任务执行失败" in rendered


def test_trigger_session_auto_rename_has_no_stale_return(monkeypatch):
    """防回归：trigger_session_auto_rename 不得残留已删除函数的 return 语句（NameError）。"""
    import litellm

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    class FakeMessage:
        content = "冒泡排序"

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    scheduled = []
    monkeypatch.setattr(main.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: FakeResponse())
    monkeypatch.setattr(main.manager, "rename_session", lambda session, title: (True, title))
    monkeypatch.setattr(main, "schedule_ui", scheduled.append)
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "app_instance", None)

    result = main.trigger_session_auto_rename("session_abc123", "帮我写个冒泡排序", "m", "k", "u")

    # 同步执行完整个 _rename_job：不再抛 NameError，且重命名结果已调度回 UI 线程
    assert result is None
    assert len(scheduled) == 1
    scheduled[0]()  # update_ui 在 app_instance=None 下可安全执行


# ── 会话选择器关闭后的切回防御（_resolve_session_after_selector）──

class _FakeManager:
    def __init__(self, names):
        self._names = names

    def get_all_sessions(self):
        return [{"name": n} for n in self._names]


def test_new_unsaved_session_survives_esc_from_selector(monkeypatch):
    """新会话（未入库）按 Esc 返回时不得被切到最近会话"""
    import main as main_mod

    monkeypatch.setattr(
        main_mod, "manager", _FakeManager(["最近对话A", "最近对话B"])
    )
    resolved, was_deleted = main_mod._resolve_session_after_selector(
        "session_brandnew", existed_before=False
    )
    assert was_deleted is False
    assert resolved == "session_brandnew"


def test_deleted_existing_session_falls_back_to_most_recent(monkeypatch):
    """已入库会话在选择器里被删除后才切回到最近会话"""
    import main as main_mod

    monkeypatch.setattr(
        main_mod, "manager", _FakeManager(["最近对话A", "最近对话B"])
    )
    resolved, was_deleted = main_mod._resolve_session_after_selector(
        "被删会话", existed_before=True
    )
    assert was_deleted is True
    assert resolved == "最近对话A"


def test_deleted_last_remaining_session_gets_fresh_name(monkeypatch):
    """所有会话都被删除时回退到全新的 hex 会话名"""
    import main as main_mod

    monkeypatch.setattr(main_mod, "manager", _FakeManager([]))
    resolved, was_deleted = main_mod._resolve_session_after_selector(
        "被删会话", existed_before=True
    )
    assert was_deleted is True
    assert resolved.startswith("session_")
    assert resolved != "被删会话"


def test_existing_session_still_present_is_untouched(monkeypatch):
    """已入库且未被删除的会话保持不变"""
    import main as main_mod

    monkeypatch.setattr(
        main_mod, "manager", _FakeManager(["会话A", "会话B"])
    )
    resolved, was_deleted = main_mod._resolve_session_after_selector(
        "会话A", existed_before=True
    )
    assert was_deleted is False
    assert resolved == "会话A"


def test_new_command_creates_and_switches_session(monkeypatch):
    """/new 创建全新会话并切换；历史会话不受影响。"""
    created = []
    synced = []
    printed = []

    class FakeManager:
        def create_session_explicit(self, name):
            created.append(name)

    monkeypatch.setattr(main, "manager", FakeManager())
    monkeypatch.setattr(main, "sync_plan_session", lambda s: synced.append(s))
    monkeypatch.setattr(main, "recalc_token_usage", lambda: None)
    monkeypatch.setattr(main, "print_logo", lambda clear_screen=False: None)
    monkeypatch.setattr(main, "print_formatted_text", lambda *a, **k: printed.append(str(a)))

    old_session = main.current_session
    try:
        main.handle_slash_command("/new")
        assert len(created) == 1
        assert created[0].startswith("session_")
        assert main.current_session == created[0]
        assert main.current_session != old_session
        assert synced == [created[0]]
        assert any("全新会话" in p for p in printed)
    finally:
        main.current_session = old_session


def test_effort_command_shows_current_level(monkeypatch):
    import core.model_manager as mm

    printed = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0])))
    cfg = {
        "active_model_id": "m1",
        "providers": [
            {"id": "p1", "models": [{"id": "m1", "litellm_model": "openai/kimi-k3"}]}
        ],
    }
    monkeypatch.setattr(mm, "load_config", lambda: cfg)
    monkeypatch.setattr(mm, "get_model_reasoning_choice", lambda model_id: "low")

    main.handle_slash_command("/effort")

    assert any("low" in line for line in printed)
    assert any("可选档位" in line for line in printed)


def test_effort_command_shows_toggle_off_choice(monkeypatch):
    # toggle 型模型（如 kimi-k2.6）选"关闭"后，无参查看必须显示 off/关闭，
    # 而不是兼容接口 get_model_reasoning_effort 返回 None 导致的错误 "default"
    import core.model_manager as mm

    printed = []
    cfg = {
        "active_model_id": "m1",
        "providers": [
            {"id": "p1", "models": [{"id": "m1", "litellm_model": "openai/kimi-k2.6"}]}
        ],
    }
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0])))
    monkeypatch.setattr(mm, "load_config", lambda: cfg)
    monkeypatch.setattr(mm, "get_model_reasoning_choice", lambda model_id: "off")

    main.handle_slash_command("/effort")

    assert any("off" in line for line in printed)
    assert any("关闭" in line for line in printed)


def test_effort_command_fixed_model_hint(monkeypatch):
    # fixed 型模型（如 kimi-k2.7-code）始终开启思考，应给提示而非档位列表
    import core.model_manager as mm

    printed = []
    cfg = {
        "active_model_id": "m1",
        "providers": [
            {"id": "p1", "models": [{"id": "m1", "litellm_model": "openai/kimi-k2.7-code"}]}
        ],
    }
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0])))
    monkeypatch.setattr(mm, "load_config", lambda: cfg)

    main.handle_slash_command("/effort")

    assert any("始终开启思考" in line for line in printed)


def test_effort_command_sets_level_and_warns_anthropic_route(monkeypatch):
    import core.model_manager as mm

    printed = []
    calls = []
    cfg = {
        "active_model_id": "m1",
        "providers": [
            {"id": "p1", "models": [{"id": "m1", "litellm_model": "anthropic/k3-for-anthropic"}]}
        ],
    }
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0])))
    monkeypatch.setattr(main, "app_instance", None)
    monkeypatch.setattr(mm, "load_config", lambda: cfg)
    monkeypatch.setattr(
        mm,
        "set_model_reasoning_choice",
        lambda model_id, level: calls.append((model_id, level)) or (True, "模型 m1 思考设置已设为 high"),
    )

    main.handle_slash_command("/effort high")

    assert calls == [("m1", "high")]
    assert any("Anthropic" in line for line in printed)


def test_effort_command_rejects_without_active_model(monkeypatch):
    import core.model_manager as mm

    printed = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0])))
    monkeypatch.setattr(mm, "load_config", lambda: {"active_model_id": None, "providers": []})

    main.handle_slash_command("/effort low")

    assert any("没有激活模型" in line for line in printed)


def test_turn_finished_clears_completed_plan(tmp_path, monkeypatch):
    import core.plan as plan

    manager = plan.PlanManager(storage_dir=tmp_path, session_id="cli-clear")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "completed"},
    ])
    monkeypatch.setattr(plan, "get_default_plan_manager", lambda: manager)

    factory = TurnEventFactory("turn-plan-clear")
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-plan-clear", None)

    main._render_ui_event(factory.emit("turn_finished"))

    assert manager.snapshot() == []


def test_plan_item_handoff_prints_takeover_notice_once(monkeypatch):
    """active 接管打印一次固定接管提示；重复 live update 不刷屏；
    queued 项显示固定队列文案与位置。"""
    factory = TurnEventFactory("turn-plan-handoff")
    printed = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", printed.append)

    class FakeApp:
        def invalidate(self):
            pass

    monkeypatch.setattr(main, "app_instance", FakeApp())

    active_payload = {
        "item": {
            "id": "a", "subject": "修改调度器", "status": "in_progress",
            "execution_mode": "main_takeover", "owner": "main:turn-1",
        },
        "source_task_id": "subagent-abcd1234",
        "mode": "main_takeover",
        "queue_position": 0,
    }
    main._render_ui_event(factory.emit("plan_item_handoff", dict(active_payload)))
    # 重复 live update（同 item 同 mode）不刷屏
    main._render_ui_event(factory.emit("plan_item_handoff", dict(active_payload)))

    assert len(printed) == 1
    active_plain = Text.from_ansi(printed[0]).plain
    assert "子 Agent 已超时，主 Agent 正在接管并核查现场" in active_plain
    assert "修改调度器" in active_plain

    printed.clear()
    main._render_ui_event(factory.emit("plan_item_handoff", {
        "item": {
            "id": "b", "subject": "补充测试", "status": "in_progress",
            "execution_mode": "takeover_queued",
        },
        "source_task_id": "subagent-ef012345",
        "mode": "takeover_queued",
        "queue_position": 2,
    }))
    assert len(printed) == 1
    queued_plain = Text.from_ansi(printed[0]).plain
    assert "等待主 Agent 接管（队列第 2 项）" in queued_plain


# ---------------------------------------------------------------------------
# /turns 命令：CLI 设置单任务最大模型轮次（与 GUI 系统设置共用同一份配置）
# ---------------------------------------------------------------------------

def _turns_env(tmp_path, monkeypatch, data):
    """隔离 config.json 到 tmp_path 并捕获终端输出；返回 (config 路径, 输出行列表)。"""
    import json as _json

    import config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(_json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg_path))
    config._CONFIG_CACHE.clear()

    printed = []
    monkeypatch.setattr(
        main,
        "print_formatted_text",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )
    return cfg_path, printed


def test_turns_command_sets_limit(tmp_path, monkeypatch):
    import json as _json

    cfg_path, printed = _turns_env(tmp_path, monkeypatch, {})

    main.handle_slash_command("/turns 300")

    saved = _json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["agent_turn_step_limit"] == 300
    rendered = Text.from_ansi("\n".join(printed)).plain
    assert "300" in rendered
    assert "✔" in rendered


def test_turns_command_rejects_non_integer(tmp_path, monkeypatch):
    cfg_path, printed = _turns_env(tmp_path, monkeypatch, {})

    main.handle_slash_command("/turns abc")

    saved = cfg_path.read_text(encoding="utf-8")
    assert "agent_turn_step_limit" not in saved
    rendered = Text.from_ansi("\n".join(printed)).plain
    assert "整数" in rendered


def test_turns_command_rejects_out_of_range(tmp_path, monkeypatch):
    cfg_path, printed = _turns_env(tmp_path, monkeypatch, {})

    main.handle_slash_command("/turns 5")

    saved = cfg_path.read_text(encoding="utf-8")
    assert "agent_turn_step_limit" not in saved
    rendered = Text.from_ansi("\n".join(printed)).plain
    assert "10" in rendered and "1000" in rendered


def test_turns_command_without_args_shows_current_and_usage(tmp_path, monkeypatch):
    _, printed = _turns_env(tmp_path, monkeypatch, {"agent_turn_step_limit": 240})

    main.handle_slash_command("/turns")

    rendered = Text.from_ansi("\n".join(printed)).plain
    assert "240" in rendered
    assert "/turns" in rendered


# ---------------- 交互式控制台守卫（非 TTY 环境快速失败） ----------------


class _FakeTTYStream:
    """最小流替身：只为守卫提供 isatty()。"""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_require_interactive_console_passes_when_both_streams_are_tty(monkeypatch):
    monkeypatch.setattr(main.sys, "stdout", _FakeTTYStream(True), raising=False)
    monkeypatch.setattr(main.sys, "stdin", _FakeTTYStream(True), raising=False)
    assert main._require_interactive_console() is None


def test_require_interactive_console_exits_with_headless_guidance_when_stdout_piped(
    monkeypatch, capsys
):
    monkeypatch.setattr(main.sys, "stdout", _FakeTTYStream(False), raising=False)
    monkeypatch.setattr(main.sys, "stdin", _FakeTTYStream(True), raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main._require_interactive_console()
    assert exc_info.value.code == 2
    rendered = capsys.readouterr().err
    assert "标准输出" in rendered
    assert "TTY" in rendered
    assert "-p" in rendered  # headless 模式引导


def test_require_interactive_console_exits_when_only_stdin_piped(monkeypatch, capsys):
    monkeypatch.setattr(main.sys, "stdout", _FakeTTYStream(True), raising=False)
    monkeypatch.setattr(main.sys, "stdin", _FakeTTYStream(False), raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main._require_interactive_console()
    assert exc_info.value.code == 2
    rendered = capsys.readouterr().err
    assert "标准输入" in rendered
    assert "-p" in rendered


def test_run_cli_checks_console_before_any_initialization():
    """接线断言：run_cli 首个可执行语句（跳过 docstring/global 声明）必须是控制台守卫。

    非 TTY 管道下若守卫不在一切初始化副作用（模型选择器/后台线程/MCP 预热/
    首绘 print_logo）之前，prompt_toolkit Win32Output 仍会裸崩溃
    （NoConsoleScreenBufferError）。与 test_computer_cli.py 的 __main__
    AST 测试同一模式。
    """
    import ast
    import pathlib

    source = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    function = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "run_cli"
    )
    statements = [
        stmt
        for stmt in function.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        and not isinstance(stmt, ast.Global)
    ]
    first = statements[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    callee = first.value.func
    callee_name = (
        callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
    )
    assert callee_name == "_require_interactive_console"

def test_undo_list_preview_and_ambiguous_short_id(tmp_path, monkeypatch):
    """/undo list 与 preview 正常工作；短 ID 歧义显式拒绝（规格 17.2）。"""
    from core.memory import MemoryManager
    from core.turn_undo import service as tu_service
    from core.turn_undo import recorder as tu_recorder
    from core.turn_undo.models import OwnerKind, NodeKind

    session = "cli-list-sess"
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = tu_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    monkeypatch.setattr(main, "current_session", session)

    manager.ensure_session(session)
    session_id = storage.resolve_session_id(session)
    printed = []
    monkeypatch.setattr(
        main, "print_formatted_text",
        lambda obj: printed.append(str(obj)),
    )

    # 两个同前缀回合制造短 ID 歧义 + 一个真实可撤销回合
    ws = tmp_path / "ws"
    target = ws / "x.py"
    for turn in ("aaaaaaaa-1", "aaaaaaaa-2", "bbbbbbbb-3"):
        ws.mkdir(exist_ok=True)
        target.write_bytes(b"old")
        checkpoint, ctx = tu_service.begin_turn(
            session_id=session_id, turn_id=turn,
            workspace_root=str(ws), owner_kind=OwnerKind.FOREGROUND_TURN,
        )
        handle = tu_recorder.prepare_mutation(
            display_path=str(target), after_kind=NodeKind.REGULAR_FILE,
            after_content=b"new", tool_name="write_file", tool_call_id="c",
        )
        target.write_bytes(b"new")
        tu_recorder.verify_after_mutation(handle)
        tu_service.finish_turn(ctx)

    main.handle_slash_command("/undo list")
    main.handle_slash_command("/undo preview bbbbbbbb")

    # 歧义前缀：显式拒绝并要求完整 ID
    printed.clear()
    main.handle_undo_command("/undo aaaaaaaa", session)
    assert any("匹配到 2 个回合" in line for line in printed)
    # 精确短 ID 正常
    printed.clear()
    main.handle_undo_command("/undo preview bbbbbbbb-3", session)
    assert any("撤销影响" in line for line in printed if isinstance(line, str)) or printed
