from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import DynamicStyle

import os

import core.app_context as app_context
from core.themes import build_selector_style


def get_manager():
    return app_context.get_manager()

def get_current_session():
    return app_context.get_current_session() or "default"

STYLE = build_selector_style()

def _t(style: str, text: str):
    return (style, text)

def open_session_selector(on_closed=None, on_switch=None, on_rename=None,
                          manager=None, current_session=None):
    try:
        mgr = manager if manager is not None else get_manager()
        cur_session = current_session if current_session else get_current_session()
    except Exception:
        mgr = None
        cur_session = "default"

    try:
        # DB 锁定/异常不能导致选择器打开即崩溃：空列表兜底，用户仍可 Esc 退出
        sessions = mgr.get_all_sessions(workdir=os.getcwd()) if mgr else []
    except Exception:
        sessions = []

    state = {
        "cursor": 0,
        "sessions": sessions,
        "current": cur_session,
        "notice": "",
        "confirm_delete": None,
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
        if state["cursor"] >= len(state["sessions"]):
            state["cursor"] = max(0, len(state["sessions"]) - 1)

    def render_list():
        out = []
        view_label = "已归档列表" if state["archived_view"] else "活跃列表"
        out += [_t("class:header", f"  ✦ 星瑶Code 历史会话管家 (Sessions) · {view_label}\n")]
        out += [_t("class:footer", f"  当前目录: {os.getcwd()}\n")]
        out += [_t("class:footer", "  " + "─" * 56 + "\n")]

        if not state["sessions"]:
            if state["archived_view"]:
                out += [_t("class:empty", "  (当前目录下没有已归档会话。)\n")]
            else:
                out += [_t("class:empty", "  (当前目录下没有任何历史会话。)\n")]
        else:
            for i, session in enumerate(state["sessions"]):
                is_cursor = (i == state["cursor"])
                is_active = (session["name"] == state["current"])

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
                time_str = session.get("updated_at") or "-"
                if len(time_str) >= 16:
                    time_str = time_str[:16]
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
        if state["archived_view"] or not state["sessions"]:
            return

        cur = state["sessions"][state["cursor"]]
        # 与「不能删除当前活跃会话」同源的保护：归档当前会话会让正在使用的会话
        # 从默认列表消失，用户下次打开管家就找不到它了
        if cur["name"] == state["current"]:
            state["notice"] = "⚠️ 不能归档当前活跃会话，请先切换到其它会话。"
            return

        name = cur["name"]
        if mgr and mgr.archive_session(name):
            state["notice"] = f"✔ 已归档：{name}（按 tab 查看归档区）"
            reload_sessions()
        else:
            state["notice"] = f"❌ 归档失败：{name}"

    @kb.add("u")
    def _unarchive(event):
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
        style=DynamicStyle(build_selector_style),
        full_screen=True
    )
    
    app.run()
