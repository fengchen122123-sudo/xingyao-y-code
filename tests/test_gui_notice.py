"""GUI notice 桥接层：CLI 的 <ansi*> 着色标记必须在桥接层剥离，否则原样显示给用户。"""
import gui_bridge


def test_strip_ansi_markup_removes_color_tags():
    assert gui_bridge._strip_ansi_markup(
        "<ansiyellow>ℹ 已折叠 4 条旧工具结果以节省上下文。</ansiyellow>"
    ) == "ℹ 已折叠 4 条旧工具结果以节省上下文。"


def test_strip_ansi_markup_handles_various_colors():
    assert gui_bridge._strip_ansi_markup("<ansigreen>✔ 完成</ansigreen>") == "✔ 完成"
    # 首尾换行/空白也一并去掉（CLI 用 \n 分段，GUI notice 卡片会渲染成空行）
    assert gui_bridge._strip_ansi_markup(
        "\n<ansigray>🧠 正在后台智能压缩...</ansigray>"
    ) == "🧠 正在后台智能压缩..."


def test_strip_ansi_markup_keeps_plain_text_and_html_like_content():
    assert gui_bridge._strip_ansi_markup("普通文本") == "普通文本"
    # 非 ansi 前缀的尖括号内容不误伤（如用户文本里的 <div>）
    assert gui_bridge._strip_ansi_markup("看看 <div> 标签") == "看看 <div> 标签"


def test_post_notice_broadcasts_notice_event_to_all_clients():
    """客户端本地提示经 post_notice 桥方法广播（emit_event 自带桌面+手机双端派发）。"""
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

    # 空内容不广播
    res = bridge.post_notice("   ", "s1")
    assert res["ok"] is False
    assert len(emitted) == 2
