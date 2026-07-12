from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

# 延迟导入，避免循环引用
def get_manager():
    from main import manager
    return manager

def get_current_session():
    from main import current_session
    return current_session

STYLE = Style.from_dict({
    "header":          "bold #00b4d8",
    "footer":          "#445566",
    "session.active":  "bold #00ffa3",
    "session.normal":  "#718096",
    "session.cursor":  "bold #ffffff bg:#1a2a3a",
    "ok":              "bold #00ffa3",
    "empty":           "#f6c90e",
    "cursor-column":   "",
})

def _t(style: str, text: str):
    return (style, text)

def open_session_selector(on_closed=None, on_switch=None):
    """
    提供全屏终端 UI 用于浏览、切换、删除会话记录。
    """
    try:
        mgr = get_manager()
        cur_session = get_current_session()
    except Exception:
        # 如果无法获取到，可能是某些上下文问题，给个空数据做防御
        mgr = None
        cur_session = "default"

    state = {
        "cursor": 0,
        "sessions": mgr.get_all_sessions() if mgr else [],
        "current": cur_session
    }

    def render_list():
        out = []
        out += [_t("class:header", "  Y-code 历史会话管家 (Sessions)\n")]
        out += [_t("", "  -----------------------------------------\n")]

        if not state["sessions"]:
            out += [_t("class:empty", "  (没有任何历史会话。)\n")]
        else:
            for i, session in enumerate(state["sessions"]):
                is_cursor = (i == state["cursor"])
                is_active = (session["name"] == state["current"])

                # 样式组合
                if is_cursor:
                    st = "class:session.cursor"
                    prefix = " > "
                else:
                    if is_active:
                        st = "class:session.active"
                        prefix = "   "
                    else:
                        st = "class:session.normal"
                        prefix = "   "

                check = "[当前]" if is_active else "[    ]"
                line = f"{prefix}{check} {session['name']} (包含 {session['count']} 条消息)\n"
                out.append(_t(st, line))

        out += [_t("", "\n")]
        out += [_t("class:footer", "  [↑/↓] 移动   [Enter] 切换到选中的会话   [Delete/Backspace] 删除选中的会话   [Esc] 退出\n")]
        return out

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        if state["sessions"]:
            state["cursor"] = (state["cursor"] - 1) % len(state["sessions"])

    @kb.add("down")
    def _down(event):
        if state["sessions"]:
            state["cursor"] = (state["cursor"] + 1) % len(state["sessions"])

    @kb.add("enter")
    def _enter(event):
        if not state["sessions"]:
            return
            
        cur = state["sessions"][state["cursor"]]
        if on_switch:
            on_switch(cur["name"])
        event.app.exit()

    @kb.add("delete")
    @kb.add("backspace")
    def _delete(event):
        if not state["sessions"]:
            return
        
        cur = state["sessions"][state["cursor"]]
        if mgr:
            mgr.delete_session(cur["name"])
            state["sessions"] = mgr.get_all_sessions()
            # 光标边界防御
            if state["cursor"] >= len(state["sessions"]):
                state["cursor"] = max(0, len(state["sessions"]) - 1)

    @kb.add("c-c")
    @kb.add("c-d")
    @kb.add("escape")
    def _exit(event):
        event.app.exit()
        if on_closed:
            on_closed()

    root_container = HSplit([
        Window(content=FormattedTextControl(render_list))
    ])

    app = Application(
        layout=Layout(root_container),
        key_bindings=kb,
        style=STYLE,
        full_screen=True
    )
    
    app.run()
