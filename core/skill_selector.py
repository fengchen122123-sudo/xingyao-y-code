from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import DynamicStyle
from prompt_toolkit.data_structures import Point

from core.themes import build_selector_style
from core.skills import (
    scan_all_skills,
    get_active_skill_ids,
    save_active_skill_ids,
    get_skill_dirs,
    skill_source_label,
)

STYLE = build_selector_style()

def _t(style: str, text: str):
    return (style, text)

def open_skill_selector(on_closed=None):
    state = {
        "cursor": 0,
        "skills": scan_all_skills(),
        "active_ids": set(get_active_skill_ids())
    }

    def render_header():
        out = []
        out += [_t("class:header", "  ✦ 星瑶Code 技能插件管理器 (通用 SKILL.md)\n")]
        out += [_t("class:footer", "  " + "─" * 56 + "\n")]
        active_dirs = get_skill_dirs()
        if active_dirs:
            out += [_t("class:footer", "  扫描目录:\n")]
            for d in active_dirs:
                out += [_t("class:footer", f"    - {d}\n")]
            out += [_t("", "\n")]
        return out

    def _header_line_count():
        dirs = get_skill_dirs()
        return 2 + (1 + len(dirs) + 1 if dirs else 0)

    def render_skills():
        out = []
        if not state["skills"]:
            out += [_t("class:empty",
                       "  (没有扫描到任何技能。请在上述扫描目录中放入带有 SKILL.md 的文件夹)\n")]
        else:
            for i, skill in enumerate(state["skills"]):
                is_cursor = (i == state["cursor"])
                is_active = (skill["id"] in state["active_ids"])

                if is_cursor:
                    st = "class:skill.cursor"
                    prefix = " > "
                else:
                    st = "class:skill.active" if is_active else "class:skill.normal"
                    prefix = "   "

                check = "[✓]" if is_active else "[ ]"
                desc = skill["description"]
                if len(desc) > 40:
                    desc = desc[:37] + "..."
                src_tag = f"[{skill_source_label(skill)}]"
                line = f"{prefix}{check} {skill['name']} {src_tag} - {desc}\n"
                out.append(_t(st, line))
        return out

    def get_cursor_position():
        return Point(x=0, y=state["cursor"])

    def render_footer():
        return [_t("class:footer", "  [↑/↓] 移动   [Enter] 开启/关闭   [Ctrl+D/Esc] 保存并退出")]

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        if state["skills"]:
            state["cursor"] = (state["cursor"] - 1) % len(state["skills"])
        event.app.invalidate()

    @kb.add("down")
    def _down(event):
        if state["skills"]:
            state["cursor"] = (state["cursor"] + 1) % len(state["skills"])
        event.app.invalidate()

    @kb.add("enter")
    def _enter(event):
        if not state["skills"]:
            return
        cur_skill = state["skills"][state["cursor"]]
        sid = cur_skill["id"]
        if sid in state["active_ids"]:
            state["active_ids"].remove(sid)
        else:
            state["active_ids"].add(sid)
        event.app.invalidate()

    @kb.add("c-c")
    @kb.add("c-d")
    @kb.add("escape")
    def _exit(event):
        save_active_skill_ids(list(state["active_ids"]))
        event.app.exit()
        if on_closed:
            on_closed()

    h = _header_line_count()

    root_container = HSplit([
        Window(
            content=FormattedTextControl(render_header),
            height=h,
            dont_extend_height=True,
            wrap_lines=False,
        ),
        Window(
            content=FormattedTextControl(
                render_skills,
                get_cursor_position=get_cursor_position,
                focusable=True,
            ),
            wrap_lines=False,
            allow_scroll_beyond_bottom=False,
        ),
        Window(
            content=FormattedTextControl(render_footer),
            height=1,
            dont_extend_height=True,
            wrap_lines=False,
            style="class:footer",
        ),
    ])

    app = Application(
        layout=Layout(root_container),
        key_bindings=kb,
        style=DynamicStyle(build_selector_style),
        full_screen=True,
    )

    app.run()
