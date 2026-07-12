from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from core.skills import scan_all_skills, get_active_skill_ids, save_active_skill_ids

STYLE = Style.from_dict({
    "header":        "bold #00b4d8",
    "footer":        "#445566",
    "skill.active":  "bold #00ffa3",
    "skill.normal":  "#718096",
    "skill.cursor":  "bold #ffffff bg:#1a2a3a",
    "ok":            "bold #00ffa3",
    "empty":         "#f6c90e",
    "cursor-column":  "",
})

def _t(style: str, text: str):
    return (style, text)

def open_skill_selector(on_closed=None):
    """
    提供全屏终端 UI 用于浏览和开启/关闭通用的 Agent Skill。
    """
    state = {
        "cursor": 0,
        "skills": scan_all_skills(),
        "active_ids": set(get_active_skill_ids())
    }

    def render_list():
        out = []
        out += [_t("class:header", "  Y-code 技能插件管理器 (通用 SKILL.md)\n")]
        out += [_t("", "  -----------------------------------------\n")]

        if not state["skills"]:
            out += [_t("class:empty", "  (没有扫描到任何技能。请在 .agents/skills 或 ~/.ycode/skills 中放入带有 SKILL.md 的文件夹)\n")]
        else:
            for i, skill in enumerate(state["skills"]):
                is_cursor = (i == state["cursor"])
                is_active = (skill["id"] in state["active_ids"])

                # 样式组合
                if is_cursor:
                    st = "class:skill.cursor"
                    prefix = " > "
                else:
                    if is_active:
                        st = "class:skill.active"
                        prefix = "   "
                    else:
                        st = "class:skill.normal"
                        prefix = "   "

                check = "[✓]" if is_active else "[ ]"
                # 显示名字和截断的描述
                desc = skill["description"]
                if len(desc) > 40:
                    desc = desc[:37] + "..."
                    
                line = f"{prefix}{check} {skill['name']} - {desc}\n"
                out.append(_t(st, line))

        out += [_t("", "\n")]
        out += [_t("class:footer", "  [↑/↓] 移动   [Enter] 开启/关闭   [Ctrl+D/Esc] 保存并退出\n")]
        return out

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        if state["skills"]:
            state["cursor"] = (state["cursor"] - 1) % len(state["skills"])

    @kb.add("down")
    def _down(event):
        if state["skills"]:
            state["cursor"] = (state["cursor"] + 1) % len(state["skills"])

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

    @kb.add("c-c")
    @kb.add("c-d")
    @kb.add("escape")
    def _exit(event):
        save_active_skill_ids(list(state["active_ids"]))
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
