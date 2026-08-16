"""GUI notice 桥接层：TUI 的 <ansi*> 着色标记必须在桥接层剥离，否则原样显示给用户。"""
import gui_bridge


def test_strip_ansi_markup_removes_color_tags():
    assert gui_bridge._strip_ansi_markup(
        "<ansiyellow>ℹ 已折叠 4 条旧工具结果以节省上下文。</ansiyellow>"
    ) == "ℹ 已折叠 4 条旧工具结果以节省上下文。"


def test_strip_ansi_markup_handles_various_colors():
    assert gui_bridge._strip_ansi_markup("<ansigreen>✔ 完成</ansigreen>") == "✔ 完成"
    assert gui_bridge._strip_ansi_markup(
        "\n<ansigray>🧠 正在后台智能压缩...</ansigray>"
    ) == "\n🧠 正在后台智能压缩..."


def test_strip_ansi_markup_keeps_plain_text_and_html_like_content():
    assert gui_bridge._strip_ansi_markup("普通文本") == "普通文本"
    # 非 ansi 前缀的尖括号内容不误伤（如用户文本里的 <div>）
    assert gui_bridge._strip_ansi_markup("看看 <div> 标签") == "看看 <div> 标签"
