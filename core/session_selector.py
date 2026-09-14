from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

import os

import core.app_context as app_context


# 会话数据来源：优先使用调用方显式传入的参数，其次回落到 app_context
# （彻底移除对 main 的 import，GUI/测试同样可直接调用本模块）
def get_manager():
    return app_context.get_manager()

def get_current_session():
    return app_context.get_current_session() or "default"

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

def open_session_selector(on_closed=None, on_switch=None, on_rename=None,
                          manager=None, current_session=None):
    """
    提供全屏终端 UI 用于浏览、切换、删除会话记录。

    参数 manager / current_session 由调用方（main.py）显式传入；
    未传入时回落到 app_context 中注册的运行时上下文。
    """
    try:
        mgr = manager if manager is not None else get_manager()
        cur_session = current_session if current_session else get_current_session()
    except Exception:
        # 如果无法获取到，可能是某些上下文问题，给个空数据做防御
        mgr = None
        cur_session = "default"

    try:
        # DB 锁定/异常不能导致选择器打开即崩溃：空列表兜底，用户仍可 Esc 退出
        sessions = mgr.get_all_sessions(workdir=os.getcwd()) if mgr else []
    except Exception:
        sessions = []

    state = {
        "cursor": 0,
        # 只显示当前工作目录下创建的会话（历史遗留的无目录会话仍可见）
        "sessions": sessions,
        "current": cur_session,
        "notice": "",
        "confirm_delete": None,
    }

    def render_list():
        out = []
        out += [_t("class:header", "  星瑶Code 历史会话管家 (Sessions)\n")]
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

    # 注意：不绑定 backspace——退格是几乎所有界面里的"返回"习惯键，
    # 绑定成删除会话会造成误触（即使有二次确认也很吓人）
    @kb.add("delete")
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
