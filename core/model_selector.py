"""
core/model_selector.py
/models 指令触发的全屏 TUI 选择器。
返回格式：FormattedTextControl 兼容的 [(style, text), ...] 元组列表。
"""
import uuid
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
from prompt_toolkit.styles import Style
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.containers import ConditionalContainer

from core.model_manager import (
    load_config, set_active_model, add_provider,
    add_model_to_provider, delete_model, get_active_model,
    update_provider_and_model
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
    "wiz.title":     "bold #00b4d8",
    "wiz.hint":      "#445566",
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


def _latest_total_tokens() -> int:
    """读取当前会话最新一轮的 token 总量（持锁，与 core.llm 的后台写线程互斥）。"""
    from core.llm import session_token_usage, token_usage_lock
    with token_usage_lock:
        return session_token_usage["latest_total"]


def open_model_selector(on_model_selected=None):
    state = {
        "mode":           "list",   # list | wizard | edit | addmodel
        "cursor":         0,
        "items":          [],
        "proto_cursor":   0,
        "add_step":       0,        # 0=name 1=url 2=key 3=proto 4=model_name 5=ctx 6=more?
        "add_data":       {},
        "new_provider_id": None,
        "message":        "",
        "testing":        False,
        "edit_step":      0,
        "edit_data":      {},
        "edit_model_id":   None,
        "edit_provider_id":None,
        "am_step":        0,        # 为已有厂商添加模型：0=model_name 1=ctx
        "am_data":        {},
        "am_provider_id":  None,
    }

    def _test_and_save_edited_model(event, ctx):
        import threading
        state["message"] = "⏳ 正在验证连接模型..."
        state["testing"] = True
        event.app.invalidate()

        # 后台线程不直接触碰 UI，所有界面更新回投到 prompt_toolkit 主循环
        def _ui(callback):
            event.app.loop.call_soon_threadsafe(callback)

        def test_conn():
            # 注：worker 线程直接写 state 的 str/bool 字段（CPython 单赋值是原子的），
            # 渲染线程最坏读到短暂旧值，风险可接受；UI 刷新统一走 _ui() 回投主循环。
            try:
                import litellm
                url = state["edit_data"].get("base_url", "").strip()
                key = state["edit_data"].get("api_key", "").strip()
                model = state["edit_data"].get("model_name", "").strip()
                
                # 获取原厂商的协议类型
                proto = "openai"
                cfg = load_config()
                for provider in cfg.get("providers", []):
                    if provider["id"] == state["edit_provider_id"]:
                        proto = provider.get("protocol", "openai")
                        break
                
                test_model = model
                if not model.startswith(f"{proto}/"):
                    test_model = f"{proto}/{model}"
                
                kwargs = {
                    "model": test_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                    "timeout": 8,
                }
                if key:
                    kwargs["api_key"] = key
                if url:
                    kwargs["api_base"] = url
                
                litellm.completion(**kwargs)
                
                # 验证成功，保存修改的厂商与模型
                update_provider_and_model(
                    provider_id=state["edit_provider_id"],
                    model_id=state["edit_model_id"],
                    base_url=url,
                    api_key=key,
                    model_name=model,
                    context_window=ctx,
                )
                
                state["message"] = "✔ 修改已成功测试并保存！"
                state["mode"] = "list"
                state["items"] = build_items()
                _ui(lambda: layout.focus(main_win))
            except Exception as e:
                err_msg = str(e)
                if "APIConnectionError" in err_msg:
                    err_msg = "网络连接失败，请检查 Base URL 是否正确或网络是否通畅。"
                elif "AuthenticationError" in err_msg or "401" in err_msg:
                    err_msg = "认证失败，请检查 API Key 是否正确。"
                elif "NotFoundError" in err_msg or "404" in err_msg:
                    err_msg = f"模型或 API 路径不存在，请检查模型名称 '{model}' 或 Base URL 是否正确。"
                else:
                    err_msg = f"连接失败: {err_msg}"
                
                state["message"] = f"❌ {err_msg[:75]}"
            finally:
                state["testing"] = False
                _ui(event.app.invalidate)
                
        threading.Thread(target=test_conn, daemon=True).start()

    def _test_and_save_model(event, ctx):
        import threading
        state["message"] = "⏳ 正在验证连接模型..."
        state["testing"] = True
        event.app.invalidate()

        # 后台线程不直接触碰 UI，所有界面更新回投到 prompt_toolkit 主循环
        def _ui(callback):
            event.app.loop.call_soon_threadsafe(callback)

        def test_conn():
            # 注：worker 线程直接写 state 的 str/bool 字段（CPython 单赋值是原子的），
            # 渲染线程最坏读到短暂旧值，风险可接受；UI 刷新统一走 _ui() 回投主循环。
            try:
                import litellm
                url = state["add_data"].get("base_url", "").strip()
                key = state["add_data"].get("api_key", "").strip()
                model = state["add_data"].get("model_name", "").strip()
                proto_idx = state["add_data"].get("proto_idx", 0)
                proto = PROTOCOLS[proto_idx][0]
                
                test_model = model
                if not model.startswith(f"{proto}/"):
                    test_model = f"{proto}/{model}"
                
                kwargs = {
                    "model": test_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                    "timeout": 8,
                }
                if key:
                    kwargs["api_key"] = key
                if url:
                    kwargs["api_base"] = url
                
                litellm.completion(**kwargs)
                
                # 验证成功，保存厂商与模型
                if not state.get("new_provider_id"):
                    pid = add_provider(
                        name=state["add_data"]["name"],
                        base_url=state["add_data"]["base_url"],
                        api_key=state["add_data"]["api_key"],
                        protocol=proto,
                    )
                    state["new_provider_id"] = pid
                
                add_model_to_provider(
                    provider_id=state["new_provider_id"],
                    name=model,
                    context_window=ctx,
                )
                
                state["message"] = "✔ 模型连接测试成功，已保存！"
                state["add_step"] = 6
                _ui(lambda: layout.focus(main_win))
            except Exception as e:
                err_msg = str(e)
                if "APIConnectionError" in err_msg:
                    err_msg = "网络连接失败，请检查 Base URL 是否正确或网络是否通畅。"
                elif "AuthenticationError" in err_msg or "401" in err_msg:
                    err_msg = "认证失败，请检查 API Key 是否正确。"
                elif "NotFoundError" in err_msg or "404" in err_msg:
                    err_msg = f"模型或 API 路径不存在，请检查模型名称 '{model}' 或 Base URL 是否正确。"
                else:
                    err_msg = f"连接失败: {err_msg}"
                
                state["message"] = f"❌ {err_msg[:75]}"
            finally:
                state["testing"] = False
                _ui(event.app.invalidate)
                
        threading.Thread(target=test_conn, daemon=True).start()

    def _test_and_save_added_model(event, ctx):
        """为已有厂商添加模型：复用该厂商的 URL/Key/协议做连接测试，通过后保存"""
        import threading
        state["message"] = "⏳ 正在验证连接模型..."
        state["testing"] = True
        event.app.invalidate()

        # 后台线程不直接触碰 UI，所有界面更新回投到 prompt_toolkit 主循环
        def _ui(callback):
            event.app.loop.call_soon_threadsafe(callback)

        def test_conn():
            # 注：worker 线程直接写 state 的 str/bool 字段（CPython 单赋值是原子的），
            # 渲染线程最坏读到短暂旧值，风险可接受；UI 刷新统一走 _ui() 回投主循环。
            model = state["am_data"].get("model_name", "").strip()
            try:
                import litellm
                # 读取目标厂商的连接信息
                url, key, proto = "", "", "openai"
                provider_name = ""
                cfg = load_config()
                for provider in cfg.get("providers", []):
                    if provider["id"] == state["am_provider_id"]:
                        url = provider.get("base_url", "")
                        key = provider.get("api_key", "")
                        proto = provider.get("protocol", "openai")
                        provider_name = provider.get("name", "")
                        break

                test_model = model
                if not model.startswith(f"{proto}/"):
                    test_model = f"{proto}/{model}"

                kwargs = {
                    "model": test_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                    "timeout": 8,
                }
                if key:
                    kwargs["api_key"] = key
                if url:
                    kwargs["api_base"] = url

                litellm.completion(**kwargs)

                # 验证成功，保存模型到已有厂商
                add_model_to_provider(
                    provider_id=state["am_provider_id"],
                    name=model,
                    context_window=ctx,
                )

                state["message"] = f"✔ 模型已添加到「{provider_name}」！"
                state["mode"] = "list"
                state["items"] = build_items()
                _ui(lambda: layout.focus(main_win))
            except Exception as e:
                err_msg = str(e)
                if "APIConnectionError" in err_msg:
                    err_msg = "网络连接失败，请检查 Base URL 是否正确或网络是否通畅。"
                elif "AuthenticationError" in err_msg or "401" in err_msg:
                    err_msg = "认证失败，请检查 API Key 是否正确。"
                elif "NotFoundError" in err_msg or "404" in err_msg:
                    err_msg = f"模型或 API 路径不存在，请检查模型名称 '{model}' 或 Base URL 是否正确。"
                else:
                    err_msg = f"连接失败: {err_msg}"

                state["message"] = f"❌ {err_msg[:75]}"
            finally:
                state["testing"] = False
                _ui(event.app.invalidate)

        threading.Thread(target=test_conn, daemon=True).start()

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
            items.append({"type": "add_model", "provider": provider})
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

            elif t == "add_model":
                if is_cur:
                    out += [_t("class:add.cursor", "  ▶   ＋ 添加模型\n")]
                else:
                    out += [_t("class:add.normal", "      ＋ 添加模型\n")]

            elif t == "add":
                if is_cur:
                    out += [_t("class:add.cursor", "  ▶   ＋ 添加新厂商\n")]
                else:
                    out += [_t("class:add.normal", "      ＋ 添加新厂商\n")]

        out += [_t("class:footer", "  " + "─" * 44 + "\n")]
        out += [_t("class:footer", "  ↑↓ 移动  Enter 激活  E 编辑  D 删除  Esc 退出\n")]
        if state["message"]:
            out += [_t("class:ok", f"  {state['message']}\n")]
        return out

    # ════════════════ 渲染编辑向导 ════════════════
    def render_edit_wizard():
        step = state["edit_step"]
        data = state["edit_data"]
        out = []
        out += [_t("class:header", "  ✎ 修改已添加的模型 — 向导\n")]
        out += [_t("class:footer", "  " + "─" * 44 + "\n")]

        steps_map = {
            0: ("步骤 1/4  API Base URL", "编辑 Base URL，如 https://api.deepseek.com/v1："),
            1: ("步骤 2/4  API Key",       "编辑 API Key："),
            2: ("步骤 3/4  模型名称",        "编辑模型名称，如 deepseek-chat："),
            3: ("步骤 4/4  最大上下文",      "编辑上下文 Token 数，如 128000："),
        }

        # 回显修改的值
        if step > 0 and "base_url" in data:
            out += [_t("class:wiz.hint", f"  Base URL: {data['base_url']}\n")]
        if step > 1 and "api_key" in data:
            out += [_t("class:wiz.hint", f"  API Key:  {'*' * 8}\n")]
        if step > 2 and "model_name" in data:
            out += [_t("class:wiz.hint", f"  模型名称: {data['model_name']}\n")]

        if step in steps_map:
            title, hint = steps_map[step]
            out += [_t("class:wiz.title", f"  {title}\n")]
            out += [_t("class:wiz.hint",  f"  {hint}\n")]

        out += [_t("class:footer", "  " + "─" * 44 + "\n")]
        if state["message"]:
            style = "class:ok" if not state["message"].startswith("❌") else "class:empty"
            out += [_t(style, f"  {state['message']}\n")]
        out += [_t("class:footer", "  Enter 确认，Esc 返回\n")]
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
            out += [_t("class:wiz.hint", f"  厂商名称: {data['name']}\n")]
        if step > 1 and "base_url" in data:
            out += [_t("class:wiz.hint", f"  Base URL: {data['base_url']}\n")]
        if step > 2 and "api_key" in data:
            out += [_t("class:wiz.hint", f"  API Key:  {'*' * 8}\n")]

        if step in steps_map:
            title, hint = steps_map[step]
            out += [_t("class:wiz.title", f"  {title}\n")]
            out += [_t("class:wiz.hint",  f"  {hint}\n")]

        elif step == 3:
            out += [_t("class:wiz.title", "  步骤 4/5  协议类型\n")]
            out += [_t("class:wiz.hint",  "  ↑↓ 选择，Enter 确认：\n")]
            for pi, (_, plabel) in enumerate(PROTOCOLS):
                if pi == state["proto_cursor"]:
                    out += [_t("class:proto.cursor", f"  ▶ {plabel}\n")]
                else:
                    out += [_t("class:proto.normal", f"    {plabel}\n")]

        elif step == 6:
            out += [_t("class:ok", "  ✔ 模型已添加！\n")]
            out += [_t("class:wiz.hint", "  是否继续为该厂商添加更多模型？\n")]
            opts = ["  ＋ 继续添加该厂商的模型", "  ✔ 完成并保存"]
            for oi, o in enumerate(opts):
                if oi == state["proto_cursor"]:
                    out += [_t("class:proto.cursor", f"  ▶{o}\n")]
                else:
                    out += [_t("class:proto.normal", f"   {o}\n")]

        out += [_t("class:footer", "  " + "─" * 44 + "\n")]
        if state["message"]:
            style = "class:ok" if not state["message"].startswith("❌") else "class:empty"
            out += [_t(style, f"  {state['message']}\n")]
        if step not in (3, 6):
            out += [_t("class:footer", "  Enter 确认，Esc 返回\n")]
        return out

    # ════════════════ 渲染添加模型向导 ════════════════
    def render_addmodel_wizard():
        step = state["am_step"]
        data = state["am_data"]
        provider_name = ""
        cfg = load_config()
        for provider in cfg.get("providers", []):
            if provider["id"] == state["am_provider_id"]:
                provider_name = provider.get("name", "")
                break

        out = []
        out += [_t("class:header", f"  ＋ 为「{provider_name}」添加模型 — 向导\n")]
        out += [_t("class:footer", "  " + "─" * 44 + "\n")]

        steps_map = {
            0: ("步骤 1/2  模型名称",   "输入模型名称，如 deepseek-chat："),
            1: ("步骤 2/2  最大上下文", "输入上下文 Token 数，如 128000："),
        }

        if step > 0 and "model_name" in data:
            out += [_t("class:wiz.hint", f"  模型名称: {data['model_name']}\n")]

        if step in steps_map:
            title, hint = steps_map[step]
            out += [_t("class:wiz.title", f"  {title}\n")]
            out += [_t("class:wiz.hint",  f"  {hint}\n")]

        out += [_t("class:footer", "  " + "─" * 44 + "\n")]
        if state["message"]:
            style = "class:ok" if not state["message"].startswith("❌") else "class:empty"
            out += [_t(style, f"  {state['message']}\n")]
        out += [_t("class:footer", "  Enter 确认，Esc 返回\n")]
        return out

    # ════════════════ 布局 ════════════════
    def get_main_content():
        if state["mode"] == "list":
            return render_list()
        elif state["mode"] == "edit":
            return render_edit_wizard()
        elif state["mode"] == "addmodel":
            return render_addmodel_wizard()
        return render_wizard()

    main_ctrl = FormattedTextControl(get_main_content, focusable=True)
    main_win  = Window(content=main_ctrl, wrap_lines=False)

    @Condition
    def need_input():
        if state["mode"] == "addmodel":
            return True
        return state["mode"] in ("wizard", "edit") and not (state["mode"] == "wizard" and state["add_step"] in (3, 6))

    def get_input_header():
        if state["mode"] == "addmodel":
            hints = {
                0: "输入模型名称，按 Enter 确认",
                1: "输入 Token 数量，按 Enter 确认",
            }
            hint = hints.get(state["am_step"], "")
            return [
                ("class:footer", "  " + "─" * 44 + "\n"),
                ("class:wiz.title", f"  ▶ {hint}\n"),
            ]

        if state["mode"] == "edit":
            step = state["edit_step"]
            hints = {
                0: "编辑 Base URL，按 Enter 确认",
                1: "编辑 API Key，按 Enter 确认",
                2: "编辑模型名称，按 Enter 确认",
                3: "编辑 Token 数量，按 Enter 确认",
            }
            hint = hints.get(step, "")
            return [
                ("class:footer", "  " + "─" * 44 + "\n"),
                ("class:wiz.title", f"  ▶ {hint}\n"),
            ]

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
            ("class:wiz.title", f"  ▶ {hint}\n"),
        ]

    input_header_ctrl = FormattedTextControl(get_input_header)
    input_prefix_ctrl = FormattedTextControl([("class:wiz.title", "  > ")])

    layout = Layout(
        HSplit([
            main_win,
            ConditionalContainer(
                content=HSplit([
                    Window(content=input_header_ctrl, height=2),
                    VSplit([
                        Window(content=input_prefix_ctrl, width=4),
                        Window(content=BufferControl(buffer=input_buf), height=1),
                    ])
                ]),
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
            target_ctx = item["data"].get("context_window", 128000)
            latest_total = _latest_total_tokens()
            if latest_total > target_ctx:
                state["message"] = f"❌ 无法切换：当前 Token 消耗 ({latest_total}) 已超过目标模型上限 ({target_ctx})，强行切换将导致崩溃！"
                return
                
            set_active_model(item["data"]["id"])
            state["message"] = f"✔ 已激活: {item['data']['name']}"
            state["items"] = build_items()
            if on_model_selected:
                info = get_active_model()
                if info:
                    on_model_selected(info)
            event.app.exit()

        elif item["type"] == "add_model":
            p = item["provider"]
            state["mode"] = "addmodel"
            state["am_step"] = 0
            state["am_data"] = {}
            state["am_provider_id"] = p["id"]
            state["message"] = ""
            input_buf.set_document(Document(""))
            layout.focus(input_buf)

        elif item["type"] == "add":
            state["mode"] = "wizard"
            state["add_step"] = 0
            state["add_data"] = {}
            state["proto_cursor"] = 0
            state["message"] = ""
            input_buf.set_document(Document(""))
            layout.focus(input_buf)

    # ── 列表：E 编辑 ──
    @kb.add("e", filter=Condition(lambda: state["mode"] == "list"))
    def _edit_list(event):
        item = state["items"][state["cursor"]]
        if item["type"] == "model":
            m = item["data"]
            p = item["provider"]
            state["mode"] = "edit"
            state["edit_step"] = 0
            state["edit_model_id"] = m["id"]
            state["edit_provider_id"] = p["id"]
            state["edit_data"] = {
                "base_url": p.get("base_url", ""),
                "api_key": p.get("api_key", ""),
                "model_name": m.get("name", ""),
                "context_window": m.get("context_window", 128000)
            }
            state["message"] = ""
            input_buf.set_document(Document(state["edit_data"]["base_url"]))
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

    # ── 列表：Esc/Q 退出 ──
    @kb.add("escape", filter=Condition(lambda: state["mode"] == "list"))
    @kb.add("q", filter=Condition(lambda: state["mode"] == "list"))
    @kb.add("c-c")
    def _quit(event):
        event.app.exit()

    # ── 向导：协议 & 确认选项 ↑↓ ──
    @kb.add("up", filter=Condition(lambda: state["mode"] == "wizard" and state["add_step"] in (3, 6)))
    def _w_up(event):
        if state.get("testing"):
            return
        state["proto_cursor"] = max(0, state["proto_cursor"] - 1)

    @kb.add("down", filter=Condition(lambda: state["mode"] == "wizard" and state["add_step"] in (3, 6)))
    def _w_down(event):
        if state.get("testing"):
            return
        mx = len(PROTOCOLS) - 1 if state["add_step"] == 3 else 1
        state["proto_cursor"] = min(mx, state["proto_cursor"] + 1)

    # ── 向导：Enter 推进步骤 ──
    @kb.add("enter", filter=Condition(lambda: state["mode"] == "wizard"))
    def _w_enter(event):
        if state.get("testing"):
            return
            
        state["message"] = ""
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
            state["add_data"]["context_window"] = ctx
            
            # 触发连接测试与保存逻辑
            _test_and_save_model(event, ctx)

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

    # ── 向导：Esc 返回上一步 ──
    @kb.add("escape", filter=Condition(lambda: state["mode"] == "wizard"))
    def _w_esc(event):
        if state.get("testing"):
            return
            
        state["message"] = ""
        step = state["add_step"]
        if step == 0:
            # 第一步按 Esc，退出向导
            state["mode"] = "list"
            state["items"] = build_items()
            layout.focus(main_win)
        elif step in (1, 2):
            state["add_step"] -= 1
            prev_field = {1: "name", 2: "base_url"}[step]
            input_buf.set_document(Document(state["add_data"].get(prev_field, "")))
        elif step == 3:
            state["add_step"] = 2
            input_buf.set_document(Document(state["add_data"].get("api_key", "")))
            layout.focus(input_buf)
        elif step == 4:
            state["add_step"] = 3
            state["proto_cursor"] = state["add_data"].get("proto_idx", 0)
            layout.focus(main_win)
        elif step == 5:
            state["add_step"] = 4
            input_buf.set_document(Document(state["add_data"].get("model_name", "")))

    # ── 添加模型向导：Enter 推进步骤 ──
    @kb.add("enter", filter=Condition(lambda: state["mode"] == "addmodel"))
    def _am_enter(event):
        if state.get("testing"):
            return

        state["message"] = ""
        step = state["am_step"]
        text = input_buf.text.strip()

        if step == 0:
            if not text:
                return
            state["am_data"]["model_name"] = text
            input_buf.set_document(Document(""))
            state["am_step"] = 1

        elif step == 1:
            try:
                ctx = int(text)
            except ValueError:
                ctx = 128000
            state["am_data"]["context_window"] = ctx
            _test_and_save_added_model(event, ctx)

    # ── 添加模型向导：Esc 返回上一步 ──
    @kb.add("escape", filter=Condition(lambda: state["mode"] == "addmodel"))
    def _am_esc(event):
        if state.get("testing"):
            return

        state["message"] = ""
        step = state["am_step"]
        if step == 0:
            state["mode"] = "list"
            state["items"] = build_items()
            layout.focus(main_win)
        elif step == 1:
            state["am_step"] = 0
            input_buf.set_document(Document(state["am_data"].get("model_name", "")))

    # ── 编辑向导：Enter 推进步骤 ──
    @kb.add("enter", filter=Condition(lambda: state["mode"] == "edit"))
    def _w_edit_enter(event):
        if state.get("testing"):
            return
            
        state["message"] = ""
        step = state["edit_step"]
        text = input_buf.text.strip()

        if step == 0:
            if not text:
                return
            state["edit_data"]["base_url"] = text
            input_buf.set_document(Document(state["edit_data"]["api_key"]))
            state["edit_step"] = 1

        elif step == 1:
            if not text:
                return
            state["edit_data"]["api_key"] = text
            input_buf.set_document(Document(state["edit_data"]["model_name"]))
            state["edit_step"] = 2

        elif step == 2:
            if not text:
                return
            state["edit_data"]["model_name"] = text
            input_buf.set_document(Document(str(state["edit_data"]["context_window"])))
            state["edit_step"] = 3

        elif step == 3:
            try:
                ctx = int(text)
            except ValueError:
                ctx = 128000
            state["edit_data"]["context_window"] = ctx
            _test_and_save_edited_model(event, ctx)

    # ── 编辑向导：Esc 返回上一步 ──
    @kb.add("escape", filter=Condition(lambda: state["mode"] == "edit"))
    def _w_edit_esc(event):
        if state.get("testing"):
            return
            
        state["message"] = ""
        step = state["edit_step"]
        if step == 0:
            state["mode"] = "list"
            state["items"] = build_items()
            layout.focus(main_win)
        elif step == 1:
            state["edit_step"] = 0
            input_buf.set_document(Document(state["edit_data"].get("base_url", "")))
        elif step == 2:
            state["edit_step"] = 1
            input_buf.set_document(Document(state["edit_data"].get("api_key", "")))
        elif step == 3:
            state["edit_step"] = 2
            input_buf.set_document(Document(state["edit_data"].get("model_name", "")))

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=STYLE,
        full_screen=True,
        mouse_support=False,
    )
    app.run()
