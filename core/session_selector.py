from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

import os

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

def open_session_selector(on_closed=None, on_switch=None, on_rename=None):
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
        # 只显示当前工作目录下创建的会话（历史遗留的无目录会话仍可见）
        "sessions": mgr.get_all_sessions(workdir=os.getcwd()) if mgr else [],
        "current": cur_session,
        "notice": "",
        "confirm_delete": None,
    }

    def render_list():
        out = []
        out += [_t("class:header", "  Y-code 历史会话管家 (Sessions)\n")]
        out += [_t("class:footer", f"  当前目录: {os.getcwd()}\n")]
        out += [_t("", "  -----------------------------------------\n")]

        if not state["sessions"]:
            out += [_t("class:empty", "  (当前目录下没有任何历史会话。)\n")]
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
                # 最近活跃时间（历史遗留会话无时间记录时显示 -）
                time_str = session.get("updated_at") or "-"
                if len(time_str) >= 16:
                    time_str = time_str[:16]  # 精确到分即可
                line = f"{prefix}{check} {session['name']}  [{time_str}]  (包含 {session['count']} 条消息)\n"
                out.append(_t(st, line))

        out += [_t("", "\n")]
        if state["notice"]:
            out += [_t("class:empty", f"  {state['notice']}\n")]
        out += [_t("class:footer", "  [↑/↓] 移动   [Enter] 切换到选中的会话   [R/F2] 重命名会话   [Delete/Backspace] 删除   [Esc] 退出\n")]
        return out

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        if state["sessions"]:
            state["cursor"] = (state["cursor"] - 1) % len(state["sessions"])
            state["confirm_delete"] = None
            state["notice"] = ""

    @kb.add("down")
    def _down(event):
        if state["sessions"]:
            state["cursor"] = (state["cursor"] + 1) % len(state["sessions"])
            state["confirm_delete"] = None
            state["notice"] = ""

    @kb.add("enter")
    def _enter(event):
        if not state["sessions"]:
            return
            
        cur = state["sessions"][state["cursor"]]
        if on_switch:
            on_switch(cur["name"])
        event.app.exit()

    @kb.add("r")
    @kb.add("f2")
    def _rename(event):
        if not state["sessions"]:
            return
            
        cur = state["sessions"][state["cursor"]]
        if on_rename:
            on_rename(cur["name"])
        event.app.exit()

    @kb.add("delete")
    @kb.add("backspace")
    def _delete(event):
        if not state["sessions"]:
            return

        cur = state["sessions"][state["cursor"]]
        # 禁止删除当前活跃会话，否则主程序继续写已删会话会导致历史悄悄蒸发
        if cur["name"] == state["current"]:
            state["notice"] = "⚠️ 不能删除当前活跃会话，请先切换到其它会话。"
            state["confirm_delete"] = None
            return
        # 两段式确认：第一次按 Delete 仅提示，再按一次才真正删除
        if state["confirm_delete"] != cur["name"]:
            state["confirm_delete"] = cur["name"]
            state["notice"] = f"再次按 Delete 确认删除会话: {cur['name']}"
            return

        state["confirm_delete"] = None
        state["notice"] = ""
        if mgr:
            mgr.delete_session(cur["name"])
            state["sessions"] = mgr.get_all_sessions(workdir=os.getcwd())
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
