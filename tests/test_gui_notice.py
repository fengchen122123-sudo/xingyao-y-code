import gui_bridge


def test_strip_ansi_markup_removes_color_tags():
    assert gui_bridge._strip_ansi_markup(
        "<ansiyellow>ℹ 已折叠 4 条旧工具结果以节省上下文。</ansiyellow>"
    ) == "ℹ 已折叠 4 条旧工具结果以节省上下文。"


def test_strip_ansi_markup_handles_various_colors():
    assert gui_bridge._strip_ansi_markup("<ansigreen>✔ 完成</ansigreen>") == "✔ 完成"
    assert gui_bridge._strip_ansi_markup(
        "\n<ansigray>🧠 正在后台智能压缩...</ansigray>"
    ) == "🧠 正在后台智能压缩..."


def test_strip_ansi_markup_keeps_plain_text_and_html_like_content():
    assert gui_bridge._strip_ansi_markup("普通文本") == "普通文本"
    assert gui_bridge._strip_ansi_markup("看看 <div> 标签") == "看看 <div> 标签"


def test_post_notice_broadcasts_notice_event_to_all_clients():
    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    emitted = []
    bridge.emit_event = lambda t, d: emitted.append((t, d))

    res = bridge.post_notice("已切换活动模型: k3", "会话一")

    assert res == {"ok": True}
    assert emitted == [("notice", {
        "message": "已切换活动模型: k3",
        "session_name": "会话一",
        "kind": "notice",
    })]


def test_post_notice_supports_error_kind_and_rejects_invalid_values():
    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    emitted = []
    bridge.emit_event = lambda t, d: emitted.append((t, d))

    bridge.post_notice("消息发送失败: x", "s1", "error")
    assert emitted[0][1]["kind"] == "error"

    # 非法 kind 兜底为 notice，避免前端渲染出未识别角色
    bridge.post_notice("提示", "s1", "bogus")
    assert emitted[1][1]["kind"] == "notice"

    res = bridge.post_notice("   ", "s1")
    assert res["ok"] is False
    assert len(emitted) == 2


def test_background_task_finished_event_carries_owning_session():
    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    bridge._shutting_down = False
    emitted = []
    bridge.emit_event = lambda t, d: emitted.append((t, d))

    bridge._on_background_task_finished("e0c2c3be", "npm run build", 129, "", "")

    assert len(emitted) == 1
    event_type, payload = emitted[0]
    assert event_type == "background_task_finished"
    assert payload["task_id"] == "e0c2c3be"
    assert payload["returncode"] == 129
    assert payload["session_name"] == ""

    bridge._start_bg_command_summary = lambda *args, **kwargs: None
    bridge._on_background_task_finished("t2", "pip install x", 0, "会话一", "tail")
    assert emitted[1][1]["session_name"] == "会话一"


def test_notice_emitter_carries_owning_session():
    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    emitted = []
    bridge.emit_event = lambda t, d: emitted.append((t, d))

    emit = bridge._notice_emitter("会话A")
    emit("compress", "<ansiyellow>ℹ 已折叠 1 条旧工具结果以节省上下文。</ansiyellow>")

    assert emitted == [("notice", {
        "message": "ℹ 已折叠 1 条旧工具结果以节省上下文。",
        "session_name": "会话A",
    })]


def test_notice_emitter_handles_missing_session_name():
    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    emitted = []
    bridge.emit_event = lambda t, d: emitted.append((t, d))

    bridge._notice_emitter(None)("compress", "普通提示")

    assert emitted[0][1]["session_name"] == ""
    assert emitted[0][1]["message"] == "普通提示"


def test_compress_notice_callbacks_keep_session_ownership():
    from pathlib import Path

    module_src = Path(gui_bridge.__file__).read_text(encoding="utf-8")

    assert module_src.count("notify=self._notice_emitter(session_name)") >= 3
    assert 'emit_event("notice", {"message": _strip_ansi_markup(message)})' not in module_src
