"""
core/model_selector.py
/models 指令触发的全屏 TUI 选择器。
返回格式：FormattedTextControl 兼容的 [(style, text), ...] 元组列表。
"""
import uuid
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
from prompt_toolkit.styles import Style
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.containers import ConditionalContainer

from core.model_manager import (
    load_config, set_active_model, add_provider,
    add_model_to_provider, delete_model, get_active_model
)

STYLE = Style.from_dict({
    "header":        "bold #00b4d8",
    "footer":        "#445566",
    "provider":      "bold #c9d1d9",
    "model.active":  "bold #00ffa3",
    "model.normal":  "#718096",
    "model.cursor":  "bold #ffffff bg:#1a2a3a",
    "add.cursor":    "bold #f6c90e bg:#1a2a3a",
    "add.normal":    "#f6c90e",
    "dialog.title":  "bold #00b4d8",
    "dialog.hint":   "#445566",
    "proto.cursor":  "bold #ffffff bg:#1a2a3a",
    "proto.normal":  "#718096",
    "proto.ok":      "bold #00ffa3",
    "input.label":   "bold #00b4d8",
    "input.sep":     "#1a2a3a",
    "ok":            "bold #00ffa3",
    # 覆盖 prompt_toolkit 默认的蓝底色光标 —— 关键！
    "cursor-column":  "",
    "cursor-line":    "",
    "focused  cursor-column": "bg:#2a3a4a",
})

PROTOCOLS = [
    ("openai",    "Chat Completions  (/chat/completions)"),
    ("anthropic", "Anthropic Messages (/v1/messages)"),
]


def _t(style: str, text: str):
    """快捷构造 (style, text) 元组"""
    return (style, text)


def open_model_selector(on_model_selected=None):
    state = {
        "mode":           "list",   # list | wizard
        "cursor":         0,
        "items":          [],
        "proto_cursor":   0,
        "add_step":       0,        # 0=name 1=url 2=key 3=proto 4=model_name 5=ctx 6=more?
        "add_data":       {},
        "new_provider_id": None,
        "message":        "",
    }

    # ── 输入缓冲区（向导用）──
    input_buf = Buffer()

    def build_items():
        cfg = load_config()
        active_id = cfg.get("active_model_id")
        items = []
        for provider in cfg.get("providers", []):
            items.append({"type": "provider", "data": provider})
            for model in provider.get("models", []):
                items.append({
                    "type":     "model",
                    "data":     model,
                    "provider": provider,
                    "active":   model["id"] == active_id,
                })
        items.append({"type": "add"})
        return items

    state["items"] = build_items()

    # ── 跳过 provider 行的辅助 ──
    def _skip_providers(direction=1):
        n = len(state["items"])
        for _ in range(n):
            if state["items"][state["cursor"]]["type"] != "provider":
                break
            state["cursor"] = (state["cursor"] + direction) % n

    # ════════════════ 渲染列表 ════════════════
    def render_list():
        out = []
        out += [_t("class:header", "  Y-code 模型管理器\n")]
        out += [_t("class:footer", "  " + "─" * 44 + "\n")]

        for i, item in enumerate(state["items"]):
            is_cur = (i == state["cursor"])
            t = item["type"]

            if t == "provider":
                p = item["data"]
                proto = p.get("protocol", "openai")
                out += [_t("class:provider", f"  ▸ {p['name']}  [{proto}]\n")]

            elif t == "model":
                m = item["data"]
                bullet = "●" if item["active"] else "○"
                ctx = m.get("context_window", 0)
                ctx_str = f"{ctx // 1000}k" if ctx >= 1000 else str(ctx)
                label = f"  {bullet} {m['name']}  [{ctx_str}]"
                if is_cur:
                    out += [_t("class:model.cursor", f"  ▶{label}\n")]
                elif item["active"]:
                    out += [_t("class:model.active", f"   {label}\n")]
                else:
                    out += [_t("class:model.normal", f"   {label}\n")]

            elif t == "add":
                if is_cur:
                    out += [_t("class:add.cursor", "  ▶   ＋ 添加新厂商\n")]
                else:
                    out += [_t("class:add.normal", "      ＋ 添加新厂商\n")]

        out += [_t("class:footer", "  " + "─" * 44 + "\n")]
        out += [_t("class:footer", "  ↑↓ 移动  Enter 激活  D 删除  Q 退出\n")]
        if state["message"]:
            out += [_t("class:ok", f"  {state['message']}\n")]
        return out

    # ════════════════ 渲染向导 ════════════════
    def render_wizard():
        step = state["add_step"]
        data = state["add_data"]
        out = []
        out += [_t("class:header", "  ＋ 添加新厂商 — 向导\n")]
        out += [_t("class:footer", "  " + "─" * 44 + "\n")]

        steps_map = {
            0: ("步骤 1/5  厂商名称",     "输入厂商名称，如 DeepSeek："),
            1: ("步骤 2/5  API Base URL", "输入 Base URL，如 https://api.deepseek.com/v1："),
            2: ("步骤 3/5  API Key",       "输入 API Key："),
            4: ("步骤 5a  模型名称",        "输入模型名称，如 deepseek-chat："),
            5: ("步骤 5b  最大上下文",      "输入上下文 Token 数，如 128000："),
        }

        # 前几步已填写的值回显
        if step > 0 and "name" in data:
            out += [_t("class:dialog.hint", f"  厂商名称: {data['name']}\n")]
        if step > 1 and "base_url" in data:
            out += [_t("class:dialog.hint", f"  Base URL: {data['base_url']}\n")]
        if step > 2 and "api_key" in data:
            out += [_t("class:dialog.hint", f"  API Key:  {'*' * 8}\n")]

        if step in steps_map:
            title, hint = steps_map[step]
            out += [_t("class:dialog.title", f"  {title}\n")]
            out += [_t("class:dialog.hint",  f"  {hint}\n")]

        elif step == 3:
            out += [_t("class:dialog.title", "  步骤 4/5  协议类型\n")]
            out += [_t("class:dialog.hint",  "  ↑↓ 选择，Enter 确认：\n")]
            for pi, (_, plabel) in enumerate(PROTOCOLS):
                if pi == state["proto_cursor"]:
                    out += [_t("class:proto.cursor", f"  ▶ {plabel}\n")]
                else:
                    out += [_t("class:proto.normal", f"    {plabel}\n")]

        elif step == 6:
            out += [_t("class:ok", "  ✔ 模型已添加！\n")]
            out += [_t("class:dialog.hint", "  是否继续为该厂商添加更多模型？\n")]
            opts = ["  ＋ 继续添加该厂商的模型", "  ✔ 完成并保存"]
            for oi, o in enumerate(opts):
                if oi == state["proto_cursor"]:
                    out += [_t("class:proto.cursor", f"  ▶{o}\n")]
                else:
                    out += [_t("class:proto.normal", f"   {o}\n")]

        out += [_t("class:footer", "  " + "─" * 44 + "\n")]
        if step not in (3, 6):
            out += [_t("class:footer", "  Enter 确认，Esc 返回\n")]
        return out

    # ════════════════ 布局 ════════════════
    def get_main_content():
        if state["mode"] == "list":
            return render_list()
        return render_wizard()

    main_ctrl = FormattedTextControl(get_main_content, focusable=True)
    main_win  = Window(content=main_ctrl, wrap_lines=False)

    @Condition
    def need_input():
        return state["mode"] == "wizard" and state["add_step"] not in (3, 6)

    def get_input_label():
        step = state["add_step"]
        hints = {
            0: "输入厂商名称，按 Enter 确认",
            1: "输入 Base URL，按 Enter 确认",
            2: "输入 API Key，按 Enter 确认",
            4: "输入模型名称，按 Enter 确认",
            5: "输入 Token 数量，按 Enter 确认",
        }
        hint = hints.get(step, "")
        return [
            ("class:footer", "  " + "─" * 44 + "\n"),
            ("class:dialog.title", f"  ▶ {hint}\n"),
            ("class:dialog.title", "  > "),
        ]

    input_label_ctrl = FormattedTextControl(get_input_label)

    layout = Layout(
        HSplit([
            main_win,
            ConditionalContainer(
                content=Window(content=input_label_ctrl, height=3),
                filter=need_input,
            ),
            ConditionalContainer(
                content=Window(content=BufferControl(buffer=input_buf), height=1),
                filter=need_input,
            ),
        ]),
        focused_element=main_win,
    )

    # ════════════════ 按键绑定 ════════════════
    kb = KeyBindings()

    # ── 列表：上下移动 ──
    @kb.add("up", filter=Condition(lambda: state["mode"] == "list"))
    def _up(event):
        state["cursor"] = max(0, state["cursor"] - 1)
        _skip_providers(-1)

    @kb.add("down", filter=Condition(lambda: state["mode"] == "list"))
    def _down(event):
        state["cursor"] = min(len(state["items"]) - 1, state["cursor"] + 1)
        _skip_providers(1)

    # ── 列表：Enter ──
    @kb.add("enter", filter=Condition(lambda: state["mode"] == "list"))
    def _enter_list(event):
        item = state["items"][state["cursor"]]
        if item["type"] == "model":
            # 增加安全拦截：如果当前 Token 消耗超过目标模型上限，禁止切换！
            from core.llm import session_token_usage
            target_ctx = item["data"].get("context_window", 128000)
            if session_token_usage["latest_total"] > target_ctx:
                state["message"] = f"❌ 无法切换：当前 Token 消耗 ({session_token_usage['latest_total']}) 已超过目标模型上限 ({target_ctx})，强行切换将导致崩溃！"
                return
                
            set_active_model(item["data"]["id"])
            state["message"] = f"✔ 已激活: {item['data']['name']}"
            state["items"] = build_items()
            if on_model_selected:
                info = get_active_model()
                if info:
                    on_model_selected(info)
            event.app.exit()

        elif item["type"] == "add":
            state["mode"] = "wizard"
            state["add_step"] = 0
            state["add_data"] = {}
            state["proto_cursor"] = 0
            input_buf.set_document(Document(""))
            layout.focus(input_buf)

    # ── 列表：D 删除 ──
    @kb.add("d", filter=Condition(lambda: state["mode"] == "list"))
    def _delete(event):
        item = state["items"][state["cursor"]]
        if item["type"] == "model":
            delete_model(item["data"]["id"])
            state["items"] = build_items()
            state["cursor"] = min(state["cursor"], len(state["items"]) - 1)
            _skip_providers(1)
            state["message"] = f"已删除: {item['data']['name']}"

    # ── 列表：Q 退出 ──
    @kb.add("q", filter=Condition(lambda: state["mode"] == "list"))
    @kb.add("c-c")
    def _quit(event):
        event.app.exit()

    # ── 向导：协议 & 确认选项 ↑↓ ──
    @kb.add("up", filter=Condition(lambda: state["mode"] == "wizard" and state["add_step"] in (3, 6)))
    def _w_up(event):
        state["proto_cursor"] = max(0, state["proto_cursor"] - 1)

    @kb.add("down", filter=Condition(lambda: state["mode"] == "wizard" and state["add_step"] in (3, 6)))
    def _w_down(event):
        mx = len(PROTOCOLS) - 1 if state["add_step"] == 3 else 1
        state["proto_cursor"] = min(mx, state["proto_cursor"] + 1)

    # ── 向导：Enter 推进步骤 ──
    @kb.add("enter", filter=Condition(lambda: state["mode"] == "wizard"))
    def _w_enter(event):
        step = state["add_step"]
        text = input_buf.text.strip()

        if step == 0:
            if not text:
                return
            state["add_data"]["name"] = text
            input_buf.set_document(Document(""))
            state["add_step"] = 1

        elif step == 1:
            if not text:
                return
            state["add_data"]["base_url"] = text
            input_buf.set_document(Document(""))
            state["add_step"] = 2

        elif step == 2:
            if not text:
                return
            state["add_data"]["api_key"] = text
            input_buf.set_document(Document(""))
            state["add_step"] = 3
            # 协议选择不需要输入框，切焦点到主窗口
            layout.focus(main_win)

        elif step == 3:
            state["add_data"]["proto_idx"] = state["proto_cursor"]
            pid = add_provider(
                name=state["add_data"]["name"],
                base_url=state["add_data"]["base_url"],
                api_key=state["add_data"]["api_key"],
                protocol=PROTOCOLS[state["add_data"]["proto_idx"]][0],
            )
            state["new_provider_id"] = pid
            state["proto_cursor"] = 0
            input_buf.set_document(Document(""))
            layout.focus(input_buf)
            state["add_step"] = 4

        elif step == 4:
            if not text:
                return
            state["add_data"]["model_name"] = text
            input_buf.set_document(Document(""))
            state["add_step"] = 5

        elif step == 5:
            try:
                ctx = int(text)
            except ValueError:
                ctx = 128000
            add_model_to_provider(
                provider_id=state["new_provider_id"],
                name=state["add_data"]["model_name"],
                context_window=ctx,
            )
            input_buf.set_document(Document(""))
            state["proto_cursor"] = 0
            layout.focus(main_win)
            state["add_step"] = 6

        elif step == 6:
            if state["proto_cursor"] == 0:
                # 继续添加模型
                state["add_data"].pop("model_name", None)
                state["proto_cursor"] = 0
                input_buf.set_document(Document(""))
                layout.focus(input_buf)
                state["add_step"] = 4
            else:
                # 完成
                state["items"] = build_items()
                state["mode"] = "list"
                state["message"] = "✔ 厂商已保存！"
                layout.focus(main_win)

    # ── 向导：Esc 返回 ──
    @kb.add("escape", filter=Condition(lambda: state["mode"] == "wizard"))
    def _w_esc(event):
        state["mode"] = "list"
        state["items"] = build_items()
        layout.focus(main_win)

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=STYLE,
        full_screen=True,
        mouse_support=False,
    )
    app.run()
