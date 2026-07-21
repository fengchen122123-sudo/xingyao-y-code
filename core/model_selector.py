"""
core/model_selector.py
/models 指令触发的全屏 TUI 选择器。
包含 12 大常用预设厂商（只需输入 API Key）及自定义厂商添加功能。
"""
import io
import uuid
import threading
import contextlib
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
    update_provider_and_model, add_preset_provider_with_models
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
    "cursor-column":  "",
    "cursor-line":    "",
    "focused  cursor-column": "bg:#2a3a4a",
})

PROTOCOLS = [
    ("openai",    "Chat Completions  (/chat/completions)"),
    ("anthropic", "Anthropic Messages (/v1/messages)"),
]

# 内置 12 大快捷预设厂商配置
PRESET_PROVIDERS = [
    {
        "name": "月之暗面kimi-for-coding",
        "base_url": "https://api.kimi.com/coding",
        "protocol": "anthropic",
        "models": [
            {"name": "kimi-for-coding", "context_window": 256000},
            {"name": "K3", "context_window": 1000000},
        ],
    },
    {
        "name": "月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "protocol": "openai",
        "models": [
            {"name": "kimi-k3", "context_window": 1000000},
            {"name": "kimi-k2.7-code", "context_window": 256000},
            {"name": "kimi-k2.6", "context_window": 256000},
        ],
    },
    {
        "name": "深度求索",
        "base_url": "https://api.deepseek.com",
        "protocol": "openai",
        "models": [
            {"name": "deepseek-v4-pro", "context_window": 1000000},
            {"name": "deepseek-v4-flash", "context_window": 1000000},
        ],
    },
    {
        "name": "Agnes AI",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "protocol": "openai",
        "models": [
            {"name": "agnes-2.0-flash", "context_window": 1000000},
        ],
    },
    {
        "name": "美团",
        "base_url": "https://api.longcat.chat/openai/v1",
        "protocol": "openai",
        "models": [
            {"name": "LongCat-2.0", "context_window": 1000000},
        ],
    },
    {
        "name": "商汤日日新",
        "base_url": "https://token.sensenova.cn/v1",
        "protocol": "openai",
        "models": [
            {"name": "sensenova-6.7-flash-lite", "context_window": 256000},
        ],
    },
    {
        "name": "火山AgnetPlan",
        "base_url": "https://ark.cn-beijing.volces.com/api/plan",
        "protocol": "anthropic",
        "models": [
            {"name": "doubao-seed-2.0-code", "context_window": 256000},
            {"name": "glm-5.2", "context_window": 1000000},
            {"name": "doubao-seed-evolving", "context_window": 1000000},
            {"name": "ark-code-latest", "context_window": 256000},
        ],
    },
    {
        "name": "火山CodingPlan",
        "base_url": "https://ark.cn-beijing.volces.com/api/coding",
        "protocol": "anthropic",
        "models": [
            {"name": "doubao-seed-2.0-code", "context_window": 256000},
            {"name": "glm-5.2", "context_window": 1000000},
            {"name": "ark-code-latest", "context_window": 256000},
        ],
    },
    {
        "name": "阶跃星辰",
        "base_url": "https://api.stepfun.com/step_plan/v1",
        "protocol": "openai",
        "models": [
            {"name": "step-3.7-flash", "context_window": 256000},
            {"name": "step-router-v1", "context_window": 256000},
        ],
    },
    {
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "protocol": "openai",
        "models": [
            {"name": "zai-org/GLM-5.2", "context_window": 1000000},
        ],
    },
    {
        "name": "minimax",
        "base_url": "https://api.minimaxi.com/v1",
        "protocol": "openai",
        "models": [
            {"name": "MiniMax-M3", "context_window": 1000000},
            {"name": "MiniMax-M2.7", "context_window": 204800},
        ],
    },
    {
        "name": "智谱",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "protocol": "openai",
        "models": [
            {"name": "glm-5.2", "context_window": 1000000},
            {"name": "glm-5.1", "context_window": 1000000},
        ],
    },
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
        "preset_cursor":  0,
        "selected_preset": None,
        "proto_cursor":   0,
        "add_step":       0,        # 0=select_preset_or_custom, 100=preset_key, 1=custom_name...
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

    # 显式使用 multiline=False 限制单行输入框，绝不允许插入换行符造成残留与扩展
    input_buf = Buffer(multiline=False)

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

    def _test_and_save_edited_model(event, ctx):
        state["message"] = "⏳ 正在验证连接模型..."
        state["testing"] = True
        event.app.invalidate()

        def _ui(callback):
            event.app.loop.call_soon_threadsafe(callback)

        def test_conn():
            try:
                import litellm
                litellm.suppress_debug_info = True
                litellm.set_verbose = False
                url = state["edit_data"].get("base_url", "").strip()
                key = state["edit_data"].get("api_key", "").strip()
                model = state["edit_data"].get("model_name", "").strip()
                
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
                    "timeout": 15,
                }
                if key:
                    kwargs["api_key"] = key
                if url:
                    kwargs["api_base"] = url
                
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    litellm.completion(**kwargs)
                
                update_provider_and_model(
                    provider_id=state["edit_provider_id"],
                    model_id=state["edit_model_id"],
                    base_url=url,
                    api_key=key,
                    model_name=model,
                    context_window=ctx,
                )
                
                def _on_success():
                    state["message"] = "✔ 修改已成功测试并保存！"
                    state["mode"] = "list"
                    state["items"] = build_items()
                    input_buf.set_document(Document(""))
                    main_win.vertical_scroll = 0
                    layout.focus(main_win)
                    event.app.invalidate()

                _ui(_on_success)
            except Exception as e:
                err_msg = _format_test_error(e, model)
                def _on_err():
                    state["message"] = f"❌ {err_msg[:75]}"
                    event.app.invalidate()
                _ui(_on_err)
            finally:
                state["testing"] = False
                _ui(event.app.invalidate)
                
        threading.Thread(target=test_conn, daemon=True).start()

    def _test_and_save_preset_provider(event, key):
        """测试预设厂商连接并批量添加预置模型"""
        preset = state.get("selected_preset")
        if not preset:
            return
        state["message"] = f"⏳ 正在验证连接预设厂商「{preset['name']}」..."
        state["testing"] = True
        event.app.invalidate()

        def _ui(callback):
            event.app.loop.call_soon_threadsafe(callback)

        def test_conn():
            url = preset["base_url"]
            proto = preset["protocol"]
            first_model = preset["models"][0]["name"]
            try:
                import litellm
                litellm.suppress_debug_info = True
                litellm.set_verbose = False
                test_model = first_model
                if not first_model.startswith(f"{proto}/"):
                    test_model = f"{proto}/{first_model}"

                kwargs = {
                    "model": test_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                    "timeout": 15,
                }
                if key:
                    kwargs["api_key"] = key
                if url:
                    kwargs["api_base"] = url

                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    litellm.completion(**kwargs)

                pid = add_preset_provider_with_models(
                    name=preset["name"],
                    base_url=url,
                    api_key=key,
                    protocol=proto,
                    models=preset["models"],
                )

                def _on_success():
                    state["message"] = f"✔ 预设厂商「{preset['name']}」已成功验证保存（包含 {len(preset['models'])} 个模型）！"
                    state["mode"] = "list"
                    state["items"] = build_items()
                    input_buf.set_document(Document(""))
                    main_win.vertical_scroll = 0
                    layout.focus(main_win)
                    event.app.invalidate()

                _ui(_on_success)
            except Exception as e:
                err_msg = _format_test_error(e, first_model)
                def _on_err():
                    state["message"] = f"❌ {err_msg[:75]}"
                    event.app.invalidate()
                _ui(_on_err)
            finally:
                state["testing"] = False
                _ui(event.app.invalidate)

        threading.Thread(target=test_conn, daemon=True).start()

    def _test_and_save_model(event, ctx):
        state["message"] = "⏳ 正在验证连接模型..."
        state["testing"] = True
        event.app.invalidate()

        def _ui(callback):
            event.app.loop.call_soon_threadsafe(callback)

        def test_conn():
            try:
                import litellm
                litellm.suppress_debug_info = True
                litellm.set_verbose = False
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
                    "timeout": 15,
                }
                if key:
                    kwargs["api_key"] = key
                if url:
                    kwargs["api_base"] = url
                
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    litellm.completion(**kwargs)
                
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
                
                def _on_success():
                    state["message"] = "✔ 模型连接测试成功，已保存！"
                    state["add_step"] = 6
                    main_win.vertical_scroll = 0
                    layout.focus(main_win)
                    event.app.invalidate()
                _ui(_on_success)
            except Exception as e:
                err_msg = _format_test_error(e, model)
                def _on_err():
                    state["message"] = f"❌ {err_msg[:75]}"
                    event.app.invalidate()
                _ui(_on_err)
            finally:
                state["testing"] = False
                _ui(event.app.invalidate)
                
        threading.Thread(target=test_conn, daemon=True).start()

    def _test_and_save_added_model(event, ctx):
        state["message"] = "⏳ 正在验证连接模型..."
        state["testing"] = True
        event.app.invalidate()

        def _ui(callback):
            event.app.loop.call_soon_threadsafe(callback)

        def test_conn():
            model = state["am_data"].get("model_name", "").strip()
            try:
                import litellm
                litellm.suppress_debug_info = True
                litellm.set_verbose = False
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
                    "timeout": 15,
                }
                if key:
                    kwargs["api_key"] = key
                if url:
                    kwargs["api_base"] = url

                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    litellm.completion(**kwargs)

                add_model_to_provider(
                    provider_id=state["am_provider_id"],
                    name=model,
                    context_window=ctx,
                )

                def _on_success():
                    state["message"] = f"✔ 模型已添加到「{provider_name}」！"
                    state["mode"] = "list"
                    state["items"] = build_items()
                    input_buf.set_document(Document(""))
                    main_win.vertical_scroll = 0
                    layout.focus(main_win)
                    event.app.invalidate()
                _ui(_on_success)
            except Exception as e:
                err_msg = _format_test_error(e, model)
                def _on_err():
                    state["message"] = f"❌ {err_msg[:75]}"
                    event.app.invalidate()
                _ui(_on_err)
            finally:
                state["testing"] = False
                _ui(event.app.invalidate)

        threading.Thread(target=test_conn, daemon=True).start()

    def _skip_providers(direction=1):
        n = len(state["items"])
        for _ in range(n):
            if state["items"][state["cursor"]]["type"] != "provider":
                break
            state["cursor"] = (state["cursor"] + direction) % n

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

    def render_wizard():
        step = state["add_step"]
        data = state["add_data"]
        out = []
        out += [_t("class:header", "  ＋ 添加新厂商 — 向导\n")]
        out += [_t("class:footer", "  " + "─" * 44 + "\n")]

        if step == 0:
            out += [_t("class:wiz.title", "  步骤 1  选择预设厂商或自定义添加\n")]
            out += [_t("class:wiz.hint",  "  ↑↓ 移动选择，Enter 确认：\n")]
            for pi, preset in enumerate(PRESET_PROVIDERS):
                m_count = len(preset.get("models", []))
                proto = preset.get("protocol", "openai")
                label = f"  [预设] {preset['name']}  ({proto} | {m_count} 个模型)"
                if pi == state["preset_cursor"]:
                    out += [_t("class:proto.cursor", f"  ▶ {label}\n")]
                else:
                    out += [_t("class:proto.normal", f"    {label}\n")]

            custom_idx = len(PRESET_PROVIDERS)
            if state["preset_cursor"] == custom_idx:
                out += [_t("class:add.cursor", "  ▶ ＋ 自定义手动添加厂商\n")]
            else:
                out += [_t("class:add.normal", "      ＋ 自定义手动添加厂商\n")]

            out += [_t("class:footer", "  " + "─" * 44 + "\n")]
            if state["message"]:
                out += [_t("class:ok", f"  {state['message']}\n")]
            out += [_t("class:footer", "  ↑↓ 选择预设，Enter 确认，Esc 返回列表\n")]
            return out

        if step == 100:
            preset = state.get("selected_preset") or {}
            out += [_t("class:wiz.title", f"  快捷配置预设厂商 — 「{preset.get('name', '')}」\n")]
            out += [_t("class:wiz.hint",  f"  Base URL:  {preset.get('base_url', '')}\n")]
            out += [_t("class:wiz.hint",  f"  API 协议:  {preset.get('protocol', '')}\n")]
            models_str = ", ".join(m["name"] for m in preset.get("models", []))
            out += [_t("class:wiz.hint",  f"  预置模型:  {models_str}\n\n")]
            out += [_t("class:wiz.title",  "  ▶ 请在下方输入该厂商的 API Key 并按 Enter 验证保存：\n")]

            out += [_t("class:footer", "  " + "─" * 44 + "\n")]
            if state["message"]:
                style = "class:ok" if not state["message"].startswith("❌") else "class:empty"
                out += [_t(style, f"  {state['message']}\n")]
            out += [_t("class:footer", "  Enter 验证并提交，Esc 返回厂商选择\n")]
            return out

        steps_map = {
            1: ("步骤 1/5  厂商名称",     "输入厂商名称，如 DeepSeek："),
            2: ("步骤 2/5  API Base URL", "输入 Base URL，如 https://api.deepseek.com/v1："),
            3: ("步骤 3/5  API Key",       "输入 API Key："),
            4: ("步骤 5a  模型名称",        "输入模型名称，如 deepseek-chat："),
            5: ("步骤 5b  最大上下文",      "输入上下文 Token 数，如 128000："),
        }

        if step > 1 and "name" in data:
            out += [_t("class:wiz.hint", f"  厂商名称: {data['name']}\n")]
        if step > 2 and "base_url" in data:
            out += [_t("class:wiz.hint", f"  Base URL: {data['base_url']}\n")]
        if step > 3 and "api_key" in data:
            out += [_t("class:wiz.hint", f"  API Key:  {'*' * 8}\n")]

        if step in steps_map:
            title, hint = steps_map[step]
            out += [_t("class:wiz.title", f"  {title}\n")]
            out += [_t("class:wiz.hint",  f"  {hint}\n")]

        elif step == 3.5 or step == 3:
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
        if step not in (0, 3, 6):
            out += [_t("class:footer", "  Enter 确认，Esc 返回\n")]
        return out

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
        if state["mode"] == "edit":
            return True
        if state["mode"] == "wizard":
            return state["add_step"] not in (0, 3, 3.5, 6)
        return False

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
        if step == 100:
            preset = state.get("selected_preset") or {}
            return [
                ("class:footer", "  " + "─" * 44 + "\n"),
                ("class:wiz.title", f"  ▶ 输入「{preset.get('name', '')}」的 API Key，按 Enter 确认\n"),
            ]

        hints = {
            1: "输入厂商名称，按 Enter 确认",
            2: "输入 Base URL，按 Enter 确认",
            3: "输入 API Key，按 Enter 确认",
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

    kb = KeyBindings()

    @kb.add("up", filter=Condition(lambda: state["mode"] == "list"))
    def _up(event):
        if state["cursor"] == 0:
            # 已在列表顶部，不允许向上绕行到底部
            return
        state["cursor"] = max(0, state["cursor"] - 1)
        _skip_providers(-1)

    @kb.add("down", filter=Condition(lambda: state["mode"] == "list"))
    def _down(event):
        state["cursor"] = min(len(state["items"]) - 1, state["cursor"] + 1)
        _skip_providers(1)

    @kb.add("enter", filter=Condition(lambda: state["mode"] == "list"))
    def _enter_list(event):
        item = state["items"][state["cursor"]]
        if item["type"] == "model":
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
            main_win.vertical_scroll = 0
            layout.focus(input_buf)

        elif item["type"] == "add":
            state["mode"] = "wizard"
            state["add_step"] = 0
            state["preset_cursor"] = 0
            state["selected_preset"] = None
            state["add_data"] = {}
            state["proto_cursor"] = 0
            state["message"] = ""
            input_buf.set_document(Document(""))
            main_win.vertical_scroll = 0
            layout.focus(main_win)

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
            main_win.vertical_scroll = 0
            layout.focus(input_buf)

    @kb.add("d", filter=Condition(lambda: state["mode"] == "list"))
    def _delete(event):
        item = state["items"][state["cursor"]]
        if item["type"] == "model":
            delete_model(item["data"]["id"])
            state["items"] = build_items()
            state["cursor"] = min(state["cursor"], len(state["items"]) - 1)
            _skip_providers(1)
            state["message"] = f"已删除: {item['data']['name']}"

    @kb.add("escape", filter=Condition(lambda: state["mode"] == "list"))
    @kb.add("q", filter=Condition(lambda: state["mode"] == "list"))
    @kb.add("c-c")
    def _quit(event):
        event.app.exit()

    @kb.add("up", filter=Condition(lambda: state["mode"] == "wizard" and state["add_step"] in (0, 3, 3.5, 6)))
    def _w_up(event):
        if state.get("testing"):
            return
        if state["add_step"] == 0:
            state["preset_cursor"] = max(0, state["preset_cursor"] - 1)
        elif state["add_step"] in (3, 3.5, 6):
            state["proto_cursor"] = max(0, state["proto_cursor"] - 1)

    @kb.add("down", filter=Condition(lambda: state["mode"] == "wizard" and state["add_step"] in (0, 3, 3.5, 6)))
    def _w_down(event):
        if state.get("testing"):
            return
        if state["add_step"] == 0:
            state["preset_cursor"] = min(len(PRESET_PROVIDERS), state["preset_cursor"] + 1)
        elif state["add_step"] in (3, 3.5):
            state["proto_cursor"] = min(len(PROTOCOLS) - 1, state["proto_cursor"] + 1)
        elif state["add_step"] == 6:
            state["proto_cursor"] = min(1, state["proto_cursor"] + 1)

    @kb.add("enter", filter=Condition(lambda: state["mode"] == "wizard"))
    def _w_enter(event):
        if state.get("testing"):
            return
            
        state["message"] = ""
        step = state["add_step"]
        text = input_buf.text.strip()

        if step == 0:
            if state["preset_cursor"] < len(PRESET_PROVIDERS):
                state["selected_preset"] = PRESET_PROVIDERS[state["preset_cursor"]]
                state["add_step"] = 100
                input_buf.set_document(Document(""))
                main_win.vertical_scroll = 0
                layout.focus(input_buf)
            else:
                state["selected_preset"] = None
                state["add_step"] = 1
                input_buf.set_document(Document(""))
                main_win.vertical_scroll = 0
                layout.focus(input_buf)

        elif step == 100:
            if not text:
                return
            input_buf.set_document(Document(""))
            _test_and_save_preset_provider(event, text)

        elif step == 1:
            if not text:
                return
            state["add_data"]["name"] = text
            input_buf.set_document(Document(""))
            state["add_step"] = 2

        elif step == 2:
            if not text:
                return
            state["add_data"]["base_url"] = text
            input_buf.set_document(Document(""))
            state["add_step"] = 3

        elif step == 3:
            state["add_data"]["api_key"] = text
            input_buf.set_document(Document(""))
            state["add_step"] = 3.5
            layout.focus(main_win)

        elif step == 3.5:
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
            input_buf.set_document(Document(""))
            _test_and_save_model(event, ctx)

        elif step == 6:
            if state["proto_cursor"] == 0:
                state["add_data"].pop("model_name", None)
                state["proto_cursor"] = 0
                input_buf.set_document(Document(""))
                layout.focus(input_buf)
                state["add_step"] = 4
            else:
                state["items"] = build_items()
                state["mode"] = "list"
                state["message"] = "✔ 厂商已保存！"
                input_buf.set_document(Document(""))
                main_win.vertical_scroll = 0
                layout.focus(main_win)

    @kb.add("escape", filter=Condition(lambda: state["mode"] == "wizard"))
    def _w_esc(event):
        if state.get("testing"):
            return
            
        state["message"] = ""
        step = state["add_step"]
        if step in (0, 100):
            if step == 100:
                state["add_step"] = 0
                state["selected_preset"] = None
                input_buf.set_document(Document(""))
                main_win.vertical_scroll = 0
                layout.focus(main_win)
            else:
                state["mode"] = "list"
                state["items"] = build_items()
                input_buf.set_document(Document(""))
                main_win.vertical_scroll = 0
                layout.focus(main_win)
        elif step == 1:
            state["add_step"] = 0
            input_buf.set_document(Document(""))
            main_win.vertical_scroll = 0
            layout.focus(main_win)
        elif step in (2, 3):
            state["add_step"] -= 1
            prev_field = {2: "name", 3: "base_url"}[step]
            input_buf.set_document(Document(state["add_data"].get(prev_field, "")))
        elif step == 3.5:
            state["add_step"] = 3
            input_buf.set_document(Document(state["add_data"].get("api_key", "")))
            layout.focus(input_buf)
        elif step == 4:
            state["add_step"] = 3.5
            state["proto_cursor"] = state["add_data"].get("proto_idx", 0)
            input_buf.set_document(Document(""))
            layout.focus(main_win)
        elif step == 5:
            state["add_step"] = 4
            input_buf.set_document(Document(state["add_data"].get("model_name", "")))

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
            input_buf.set_document(Document(""))
            _test_and_save_added_model(event, ctx)

    @kb.add("escape", filter=Condition(lambda: state["mode"] == "addmodel"))
    def _am_esc(event):
        if state.get("testing"):
            return

        state["message"] = ""
        step = state["am_step"]
        if step == 0:
            state["mode"] = "list"
            state["items"] = build_items()
            input_buf.set_document(Document(""))
            main_win.vertical_scroll = 0
            layout.focus(main_win)
        elif step == 1:
            state["am_step"] = 0
            input_buf.set_document(Document(state["am_data"].get("model_name", "")))

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
            input_buf.set_document(Document(""))
            _test_and_save_edited_model(event, ctx)

    @kb.add("escape", filter=Condition(lambda: state["mode"] == "edit"))
    def _w_edit_esc(event):
        if state.get("testing"):
            return
            
        state["message"] = ""
        step = state["edit_step"]
        if step == 0:
            state["mode"] = "list"
            state["items"] = build_items()
            input_buf.set_document(Document(""))
            main_win.vertical_scroll = 0
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
