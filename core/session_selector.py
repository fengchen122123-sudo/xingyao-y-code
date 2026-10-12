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
    提供全屏终端 UI 用于浏览、切换、重命名、删除、归档会话记录。

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
        # False = 活跃列表（默认，不显示归档会话）；True = 已归档列表
        "archived_view": False,
    }

    def reload_sessions():
        """按当前视图重取数据源，并按现有边界防御修正光标。"""
        if mgr:
            try:
                state["sessions"] = mgr.get_all_sessions(
                    workdir=os.getcwd(), archived_only=state["archived_view"]
                )
            except Exception:
                # DB 锁定等异常不能让全屏 UI 崩溃：保留旧列表，用户仍可 Esc 退出
                pass
        # 光标边界防御
        if state["cursor"] >= len(state["sessions"]):
            state["cursor"] = max(0, len(state["sessions"]) - 1)

    def render_list():
        out = []
        view_label = "已归档列表" if state["archived_view"] else "活跃列表"
        out += [_t("class:header", f"  星瑶Code 历史会话管家 (Sessions) · {view_label}\n")]
        out += [_t("class:footer", f"  当前目录: {os.getcwd()}\n")]
        out += [_t("", "  -----------------------------------------\n")]

        if not state["sessions"]:
            if state["archived_view"]:
                out += [_t("class:empty", "  (当前目录下没有已归档会话。)\n")]
            else:
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
        if state["archived_view"]:
            hint = "  [↑/↓] 移动   [Enter] 切换到选中的会话(自动取消归档)   [U] 取消归档   [Tab] 回到活跃列表   [Delete/Backspace] 删除   [Esc] 退出\n"
        else:
            hint = "  [↑/↓] 移动   [Enter] 切换到选中的会话   [R/F2] 重命名会话   [A] 归档会话   [Tab] 查看已归档   [Delete/Backspace] 删除   [Esc] 退出\n"
        out += [_t("class:footer", hint)]
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
        # 不变量：切换到归档会话时自动取消归档，避免「会话处于归档态却正在被使用」；
        # 同时保证主程序关闭选择器后不会把「不在默认列表里」误判成会话已被删除
        if state["archived_view"] and mgr:
            mgr.unarchive_session(cur["name"])
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

    @kb.add("a")
    def _archive(event):
        # 归档视图里全是已归档会话，没有「归档」语义
        if state["archived_view"] or not state["sessions"]:
            return

        cur = state["sessions"][state["cursor"]]
        # 与「不能删除当前活跃会话」同源的保护：归档当前会话会让正在使用的会话
        # 从默认列表消失，用户下次打开管家就找不到它了
        if cur["name"] == state["current"]:
            state["notice"] = "⚠️ 不能归档当前活跃会话，请先切换到其它会话。"
            return

        # 归档可逆（随时可 u / tab 恢复），因此不做二次确认，只给底部状态行反馈
        name = cur["name"]
        if mgr and mgr.archive_session(name):
            state["notice"] = f"✔ 已归档：{name}（按 tab 查看归档区）"
            reload_sessions()
        else:
            state["notice"] = f"❌ 归档失败：{name}"

    @kb.add("u")
    def _unarchive(event):
        # 取消归档只在归档视图中有意义（活跃视图的提示行不出现该键位）
        if not state["archived_view"] or not state["sessions"]:
            return

        name = state["sessions"][state["cursor"]]["name"]
        if mgr and mgr.unarchive_session(name):
            state["notice"] = f"✔ 已取消归档：{name}（已恢复回活跃列表）"
            reload_sessions()
        else:
            state["notice"] = f"❌ 取消归档失败：{name}"

    @kb.add("tab")
    def _toggle_view(event):
        state["archived_view"] = not state["archived_view"]
        state["cursor"] = 0
        state["confirm_delete"] = None
        state["notice"] = ""
        reload_sessions()

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
            reload_sessions()

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
