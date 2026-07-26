"""
core/mcp_selector.py
/mcp 指令触发的全屏 TUI 配置器。
支持浏览/启用/禁用/新增/删除 MCP server，无需手动编辑 JSON。
仿照 skill_selector.py 的实现风格。
"""
import subprocess
import sys

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.data_structures import Point
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition

from core.mcp_client import mcp_manager

STYLE = Style.from_dict({
    "header":        "bold #00b4d8",
    "footer":        "#445566",
    "mcp.on":        "bold #00ffa3",
    "mcp.off":       "#718096",
    "mcp.cursor":    "bold #ffffff bg:#1a2a3a",
    "ok":            "bold #00ffa3",
    "warn":          "#f6c90e",
    "err":           "#ff6b6b",
    "input.label":   "bold #00b4d8",
    "input.field":   "",
})

# 预设 MCP 模板：用户选中后按提示依次输入字段即可
#
# 字段说明：
#   name          MCP server 名称（写入 mcp.json 的 key）
#   command       启动命令（通常为 npx）
#   args          参数数组，支持 {{fieldName}} 占位符（用户输入会替换占位符）
#   fields        需要用户输入的字段列表，每项：
#                   {name, hint, secret?}
#                   - name   字段名（对应 args 中的 {{name}}；若无占位符则写入 env[name]）
#                   - hint   输入提示
#                   - secret 可选，True 表示掩码显示（默认 False）
#   desc          描述
#   signup_url    申请 Key 的官方页面（输入时显示，便于复制到浏览器）
#   support_level official(官方) / community(社区) / custom(需自研)
PRESET_MCPS = [
    # ─── 国内官方 ──────────────────────────────────────────
    {
        "name": "amap",
        "command": "npx",
        "args": ["-y", "@amap/amap-maps-mcp-server"],
        "fields": [
            {"name": "AMAP_MAPS_API_KEY", "hint": "your-amap-web-service-key"},
        ],
        "desc": "高德地图（地理编码/路径规划/天气/周边搜索 12 个工具）",
        "signup_url": "https://lbs.amap.com/api/mcp-server/summary",
        "support_level": "official",
    },
    {
        "name": "lark",
        "command": "npx",
        "args": ["-y", "@larksuiteoapi/lark-mcp", "mcp", "-a", "{{appId}}", "-s", "{{appSecret}}"],
        "fields": [
            {"name": "appId", "hint": "cli_xxxxx"},
            {"name": "appSecret", "hint": "xxxxx", "secret": True},
        ],
        "desc": "飞书（文档/多维表/日历/IM/任务/审批/云盘 全套能力）",
        "signup_url": "https://open.feishu.cn/app",
        "support_level": "official",
    },
    {
        "name": "wechat-publisher",
        "command": "npx",
        "args": ["-y", "wechat-official-publisher"],
        "fields": [
            {"name": "WECHAT_APP_ID", "hint": "wx_xxxxx"},
            {"name": "WECHAT_APP_SECRET", "hint": "xxxxx", "secret": True},
        ],
        "desc": "微信公众号文章发布（Markdown 转公众号草稿）",
        "signup_url": "https://mp.weixin.qq.com/",
        "support_level": "official",
    },
    # ─── 国内社区 ──────────────────────────────────────────
    {
        "name": "bilibili",
        "command": "npx",
        "args": ["-y", "@wangshunnn/bilibili-mcp-server"],
        "fields": [],
        "desc": "B站视频搜索/用户信息/视频详情（免登录）",
        "signup_url": "https://github.com/wangshunnn/bilibili-mcp-server",
        "support_level": "community",
    },
    # ─── 数据库 ────────────────────────────────────────────
    {
        "name": "sqlite",
        "command": "npx",
        "args": ["-y", "mcp-server-sqlite-npx", "--db-path", "{{dbPath}}"],
        "fields": [
            {"name": "dbPath", "hint": "C:/path/to/your.db 或 :memory:"},
        ],
        "desc": "SQLite 数据库（CRUD/Schema 查看）",
        "signup_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
        "support_level": "community",
    },
    {
        "name": "postgres",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres", "{{connStr}}"],
        "fields": [
            {"name": "connStr", "hint": "postgresql://user:pass@host:5432/db", "secret": True},
        ],
        "desc": "PostgreSQL 数据库（只读查询/Schema 查看）",
        "signup_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
        "support_level": "official",
    },
    # ─── 网页抓取/搜索 ─────────────────────────────────────
    {
        "name": "firecrawl",
        "command": "npx",
        "args": ["-y", "firecrawl-mcp"],
        "fields": [
            {"name": "FIRECRAWL_API_KEY", "hint": "fc-your-api-key"},
        ],
        "desc": "Firecrawl 网页抓取/搜索/爬取（1000 免费 credits/月）",
        "signup_url": "https://www.firecrawl.dev/",
        "support_level": "official",
    },
    {
        "name": "wigolo",
        "command": "npx",
        "args": ["-y", "wigolo"],
        "fields": [],
        "desc": "wigolo 本地优先免费搜索（需 1.5GB 磁盘）",
        "signup_url": "https://github.com/KnockOutEZ/wigolo",
        "support_level": "community",
    },
    # ─── 通用工具 ──────────────────────────────────────────
    {
        "name": "sequential-thinking",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "fields": [],
        "desc": "顺序思考 MCP server（结构化推理）",
        "signup_url": None,
        "support_level": "official",
    },
    {
        "name": "filesystem",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "$HOME"],
        "fields": [],
        "desc": "文件系统访问 MCP server",
        "signup_url": None,
        "support_level": "official",
    },
    # ─── 自研占位 ──────────────────────────────────────────
    {
        # 携程问道暂无官方 MCP server，XingYaoCompanion 项目里是自研 HTTP 客户端
        # 这里仅作为占位提示，引导用户去参考 XingYaoCompanion 项目的 wendao_client.py
        "name": "ctrip-wendao",
        "command": "",
        "args": [],
        "fields": [],
        "desc": "携程问道（无官方 MCP，需自研 HTTP 客户端，参考 XingYaoCompanion）",
        "signup_url": "https://externalcallback.ctrip.com/",
        "support_level": "custom",
    },
]


def _get_preset_fields(preset):
    """从预设提取字段列表，统一返回 [{name, hint, secret}] 格式。
    兼容旧的 env_key/env_hint 写法（现已废弃，但保留向后兼容）。"""
    if "fields" in preset:
        return preset["fields"]
    # 向后兼容：旧的 env_key/env_hint 单字段写法
    if preset.get("env_key"):
        return [{"name": preset["env_key"], "hint": preset.get("env_hint", "")}]
    return []


def _build_server_config(preset, values):
    """根据预设和用户输入值，组装最终的 args/env。
    - args 中的 {{fieldName}} 占位符会被替换为用户输入值
    - 未在 args 中出现的字段值，会被写入 env[fieldName]
    """
    args = list(preset.get("args", []))
    env = {}
    for field in _get_preset_fields(preset):
        name = field["name"]
        val = values.get(name, "")
        placeholder = "{{" + name + "}}"
        if any(placeholder in str(a) for a in args):
            args = [a.replace(placeholder, val) if isinstance(a, str) else a for a in args]
        else:
            env[name] = val
    return args, env


def _t(style, text):
    return (style, text)


def _mask_value(val):
    """掩码处理：保留前 4 + 后 4，中间用 **** 替换。
    遵循 user_profile 中"密码字段显示首4+****+尾4"的偏好。"""
    if not val or len(val) <= 8:
        return "*" * len(val) if val else ""
    return val[:4] + "****" + val[-4:]


def _get_clipboard_text():
    """读取系统剪贴板文本，按可靠性顺序尝试多种方案。

    1. prompt_toolkit 内置剪贴板（某些终端/前端支持）
    2. Windows PowerShell Get-Clipboard（Win10/11 默认可用）
    3. tkinter 跨平台回退（Python 标准库）
    """
    # 1. prompt_toolkit 内部剪贴板
    try:
        from prompt_toolkit.application.current import get_app
        data = get_app().clipboard.get_data()
        if data and data.text:
            return data.text
    except Exception:
        pass

    # 2. Windows PowerShell Get-Clipboard
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                capture_output=True,
                text=True,
                timeout=5,
                encoding="utf-8",
                errors="ignore",
            )
            if result.returncode == 0:
                return result.stdout.rstrip("\r\n")
        except Exception:
            pass

    # 3. tkinter 跨平台回退
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.destroy()
        return text
    except Exception:
        pass

    return ""


def _preset_line_count(preset):
    """计算单个预设模板在 render_add 中占用的行数。
    用于精确计算光标位置，确保窗口自动滚动到选中项。"""
    # 1 行：名称 + 等级 + 描述
    lines = 1
    # 1 行：command（custom 项无 command）
    if preset.get("command"):
        lines += 1
    # 1 行：需要字段
    if _get_preset_fields(preset):
        lines += 1
    # 1 行：获取 Key
    if preset.get("signup_url"):
        lines += 1
    # 1 行：⚠ 自研提示
    if preset.get("support_level") == "custom":
        lines += 1
    # 1 行：空行
    lines += 1
    return lines


def open_mcp_selector(on_closed=None):
    """全屏 TUI MCP 配置器。

    操作说明：
      ↑/↓ 移动光标
      Enter 切换 [✓]/[ ] 启用/禁用
      n 新增（从预设模板选）
      d 删除当前 server
      r 重连
      Ctrl+D/Esc 保存并退出
    """
    # 状态：servers 是从 mcp_manager 读取的配置列表
    state = {
        "cursor": 0,
        "servers": [],   # [{name, enabled, command, args, env, connected}]
        "mode": "list",  # list / add / input_env / confirm_delete / custom_notice
        "add_idx": 0,    # 新增时选中的预设索引
        "input_idx": 0,  # 当前输入字段的索引（多字段顺序输入）
        "input_values": {},  # 字段名 → 用户输入值
        "input_value": "",   # 当前字段正在输入的值
        "message": "",   # 底部消息提示
    }

    def reload_servers():
        mcp_manager._load_config()
        config = mcp_manager.config.get("mcpServers", {}) if isinstance(mcp_manager.config, dict) else {}
        result = []
        for name, conf in config.items():
            if not isinstance(conf, dict):
                continue
            result.append({
                "name": name,
                "enabled": conf.get("enabled", True) is not False,
                "command": conf.get("command", ""),
                "args": conf.get("args", []),
                "env": conf.get("env", {}),
                "connected": name in mcp_manager.servers,
            })
        state["servers"] = result
        if state["cursor"] >= len(state["servers"]):
            state["cursor"] = max(0, len(state["servers"]) - 1)

    reload_servers()

    def render_header():
        out = []
        out += [_t("class:header", "  Y-code MCP 服务管理器\n")]
        out += [_t("", "  -----------------------------------------\n")]
        out += [_t("class:footer", f"  配置文件: {mcp_manager.config_file}\n")]
        out += [_t("", "\n")]
        return out

    def _header_line_count():
        return 4

    def render_list():
        out = []
        if not state["servers"]:
            out += [_t("class:warn", "  (未配置任何 MCP 服务。按 [n] 从预设模板新增)\n")]
        else:
            for i, srv in enumerate(state["servers"]):
                is_cursor = (i == state["cursor"])
                if is_cursor:
                    st = "class:mcp.cursor"
                    prefix = " > "
                else:
                    st = "class:mcp.on" if srv["enabled"] else "class:mcp.off"
                    prefix = "   "
                check = "[✓]" if srv["enabled"] else "[ ]"
                conn = "已连接" if srv["connected"] else ("已禁用" if not srv["enabled"] else "未连接")
                name = srv["name"]
                cmd = srv["command"] + " " + " ".join(srv.get("args", []))
                if len(cmd) > 50:
                    cmd = cmd[:47] + "..."
                out += [_t(st, f"{prefix}{check} {name}  [{conn}]  {cmd}\n")]
        return out

    def render_add():
        out = []
        out += [_t("class:input.label", "  从预设模板选择 MCP server:\n")]
        out += [_t("", "\n")]
        for i, preset in enumerate(PRESET_MCPS):
            is_cursor = (i == state["add_idx"])
            st = "class:mcp.cursor" if is_cursor else "class:mcp.off"
            prefix = " > " if is_cursor else "   "
            # 维护等级标签：官方绿/社区黄/自研红
            level = preset.get("support_level", "community")
            if level == "official":
                level_tag = ("class:mcp.on", "[官方]")
            elif level == "custom":
                level_tag = ("class:err", "[自研]")
            else:
                level_tag = ("class:warn", "[社区]")
            out += [_t(st, f"{prefix}{preset['name']} ")]
            out += [level_tag]
            out += [_t(st, f" - {preset['desc']}\n")]
            if preset["command"]:
                out += [_t("class:footer", f"       command: {preset['command']} {' '.join(preset['args'])}\n")]
            fields = _get_preset_fields(preset)
            if fields:
                field_names = [f["name"] + ("*" if f.get("secret") else "") for f in fields]
                out += [_t("class:footer", f"       需要: {', '.join(field_names)}  (* 表示掩码)\n")]
            signup_url = preset.get("signup_url")
            if signup_url:
                out += [_t("class:warn", f"       获取 Key: {signup_url}\n")]
            if level == "custom":
                out += [_t("class:err", "       ⚠ 此项无现成 MCP，选中会提示如何自研\n")]
            out += [_t("", "\n")]
        out += [_t("class:footer", "  [↑/↓] 选择  [Enter] 确认  [Esc] 返回\n")]
        return out

    def render_input_env():
        out = []
        preset = PRESET_MCPS[state["add_idx"]]
        fields = _get_preset_fields(preset)
        if not fields:
            return out
        cur_field = fields[state["input_idx"]]
        # 多字段时显示进度
        if len(fields) > 1:
            out += [_t("class:footer", f"  字段 {state['input_idx'] + 1}/{len(fields)}\n")]
        out += [_t("class:input.label", f"  请输入 {cur_field['name']}:\n")]
        out += [_t("class:footer", f"  提示: {cur_field.get('hint', '')}\n")]
        # 显示申请 API Key 的官方链接（便于用户复制到浏览器打开）
        signup_url = preset.get("signup_url")
        if signup_url:
            out += [_t("class:warn", f"  获取 Key: {signup_url}\n")]
        # 显示已输入的字段（掩码处理 secret 字段）
        if state["input_values"]:
            out += [_t("", "\n")]
            out += [_t("class:footer", "  已输入:\n")]
            for i, f in enumerate(fields):
                if i == state["input_idx"]:
                    continue
                v = state["input_values"].get(f["name"], "")
                display = _mask_value(v) if f.get("secret") else v
                out += [_t("class:footer", f"    {f['name']} = {display}\n")]
        out += [_t("", "\n")]
        # 当前输入字段：secret 字段做掩码显示
        cur_display = _mask_value(state["input_value"]) if cur_field.get("secret") else state["input_value"]
        out += [_t("class:input.field", f"  > {cur_display}_\n")]
        out += [_t("", "\n")]
        if state["input_idx"] < len(fields) - 1:
            out += [_t("class:footer", "  [Enter] 下一个字段  [Esc] 返回  [Ctrl+V / 右键] 粘贴\n")]
        else:
            out += [_t("class:footer", "  [Enter] 确认添加  [Esc] 返回  [Ctrl+V / 右键] 粘贴\n")]
        return out

    def render_confirm_delete():
        out = []
        if state["servers"] and 0 <= state["cursor"] < len(state["servers"]):
            srv = state["servers"][state["cursor"]]
            out += [_t("class:err", f"  确认删除服务 {srv['name']}？\n")]
            out += [_t("", "\n")]
            out += [_t("class:footer", "  [y] 确认删除  [n/Esc] 取消\n")]
        return out

    def render_custom_notice():
        out = []
        preset = PRESET_MCPS[state["add_idx"]]
        out += [_t("class:err", f"  ⚠ {preset['name']} 没有现成的 MCP server\n")]
        out += [_t("", "\n")]
        out += [_t("class:input.label", "  说明:\n")]
        if preset["name"] == "ctrip-wendao":
            out += [_t("class:footer", "    携程问道暂无官方 MCP server，需要自研 HTTP 客户端。\n")]
            out += [_t("class:footer", "    端点: POST https://externalcallback.ctrip.com/skills/api/crew/qclaw/searchInfo\n")]
            out += [_t("class:footer", "    认证: 需要 Token（在携程问道开发者后台申请）\n")]
            out += [_t("", "\n")]
            out += [_t("class:warn", "  参考实现:\n")]
            out += [_t("class:footer", "    XingYaoCompanion/xingyao-backend/app/mcp/wendao_client.py\n")]
            out += [_t("class:footer", "    （基于 httpx 异步封装，含景点/机票/酒店查询）\n")]
            out += [_t("", "\n")]
            out += [_t("class:warn", "  在 ycode 集成方案（二选一）:\n")]
            out += [_t("class:footer", "    A. 内置工具: 参考 wendao_client.py 在 core/tools.py 写一个 wendao_search 工具\n")]
            out += [_t("class:footer", "    B. 自建 MCP: 把 wendao_client 包装成 stdio MCP server 后用 /mcp edit 配置\n")]
        else:
            out += [_t("class:footer", f"    {preset['desc']}\n")]
            out += [_t("class:footer", "    请参考官方文档自行实现后用 /mcp edit 配置\n")]
        out += [_t("", "\n")]
        out += [_t("class:footer", "  [任意键/Esc] 返回\n")]
        return out

    def render_body():
        mode = state["mode"]
        if mode == "add":
            return render_add()
        elif mode == "input_env":
            return render_input_env()
        elif mode == "confirm_delete":
            return render_confirm_delete()
        elif mode == "custom_notice":
            return render_custom_notice()
        else:
            return render_list()

    def render_footer():
        msg = state.get("message", "")
        if msg:
            # 截断过长的消息，确保单行显示（footer 高度固定为 1）
            text = f"  {msg}"
            if len(text) > 100:
                text = text[:97] + "..."
            return [_t("class:ok", text)]
        mode = state["mode"]
        if mode == "list":
            return [_t("class:footer", "  [↑/↓] 移动  [Enter] 启用/禁用  [n] 新增  [d] 删除  [r] 重连  [Ctrl+D/Esc] 退出")]
        return [_t("class:footer", "")]

    def _input_env_input_line_y():
        """计算 input_env 模式中 `> _` 输入行在 body 内的 y 坐标。"""
        preset = PRESET_MCPS[state["add_idx"]]
        fields = _get_preset_fields(preset)
        y = 0
        if len(fields) > 1:
            y += 1
        y += 1  # 请输入字段名
        y += 1  # 提示
        if preset.get("signup_url"):
            y += 1
        y += 1  # 空行
        if state["input_values"]:
            y += 1  # 已输入标签
            for f in fields:
                if f["name"] in state["input_values"]:
                    y += 1
        y += 1  # 空行
        return y

    def get_cursor_position():
        if state["mode"] == "list":
            return Point(x=0, y=state["cursor"])
        if state["mode"] == "add":
            # add 模式下每个预设占多行，需要累加前面所有预设的行数
            # render_add 开头有 2 行：标题 + 空行
            y = 2
            for i in range(state["add_idx"]):
                y += _preset_line_count(PRESET_MCPS[i])
            return Point(x=0, y=y)
        if state["mode"] == "input_env":
            # 输入模式：光标应放在输入行 `> _` 上，确保该行可见
            return Point(x=0, y=_input_env_input_line_y())
        return Point(x=0, y=0)

    kb = KeyBindings()

    # 共享的 app 引用，在 app 创建后填充，避免在按键回调里引用未定义的 event
    app_ref = [None]

    def set_message(msg):
        state["message"] = msg
        if app_ref[0] is not None:
            app_ref[0].invalidate()

    def invalidate():
        if app_ref[0] is not None:
            app_ref[0].invalidate()

    # ─── list 模式按键 ───────────────────────────────────────────
    @kb.add("up")
    def _up(event):
        if state["mode"] == "list" and state["servers"]:
            state["cursor"] = (state["cursor"] - 1) % len(state["servers"])
            state["message"] = ""
            invalidate()
        elif state["mode"] == "add":
            state["add_idx"] = (state["add_idx"] - 1) % len(PRESET_MCPS)
            invalidate()

    @kb.add("down")
    def _down(event):
        if state["mode"] == "list" and state["servers"]:
            state["cursor"] = (state["cursor"] + 1) % len(state["servers"])
            state["message"] = ""
            invalidate()
        elif state["mode"] == "add":
            state["add_idx"] = (state["add_idx"] + 1) % len(PRESET_MCPS)
            invalidate()

    @kb.add("enter")
    def _enter(event):
        mode = state["mode"]
        if mode == "list":
            if not state["servers"]:
                return
            srv = state["servers"][state["cursor"]]
            ok, msg = mcp_manager.set_server_enabled(srv["name"], not srv["enabled"])
            reload_servers()
            set_message(msg)
        elif mode == "add":
            preset = PRESET_MCPS[state["add_idx"]]
            # 自研项没有现成 MCP server，切到提示页引导用户
            if preset.get("support_level") == "custom" or not preset["command"]:
                state["mode"] = "custom_notice"
                invalidate()
                return
            fields = _get_preset_fields(preset)
            if fields:
                # 有字段需要输入：进入多字段顺序输入模式
                state["mode"] = "input_env"
                state["input_idx"] = 0
                state["input_values"] = {}
                state["input_value"] = ""
                invalidate()
            else:
                # 无需输入字段，直接添加
                ok, msg = mcp_manager.add_server(
                    name=preset["name"],
                    command=preset["command"],
                    args=list(preset["args"]),
                    env=None,
                    enabled=True,
                )
                state["mode"] = "list"
                reload_servers()
                set_message(msg)
        elif mode == "input_env":
            preset = PRESET_MCPS[state["add_idx"]]
            fields = _get_preset_fields(preset)
            if not fields:
                return
            cur_field = fields[state["input_idx"]]
            val = state["input_value"].strip()
            if not val:
                set_message(f"{cur_field['name']} 不能为空")
                return
            # 保存当前字段值
            state["input_values"][cur_field["name"]] = val
            state["input_value"] = ""
            # 切换到下一个字段，或全部输完后提交
            if state["input_idx"] < len(fields) - 1:
                state["input_idx"] += 1
                invalidate()
            else:
                # 所有字段输完，组装配置并添加
                args, env = _build_server_config(preset, state["input_values"])
                ok, msg = mcp_manager.add_server(
                    name=preset["name"],
                    command=preset["command"],
                    args=args,
                    env=env if env else None,
                    enabled=True,
                )
                state["mode"] = "list"
                state["input_idx"] = 0
                state["input_values"] = {}
                reload_servers()
                set_message(msg)
        elif mode == "confirm_delete":
            srv = state["servers"][state["cursor"]]
            ok, msg = mcp_manager.remove_server(srv["name"])
            state["mode"] = "list"
            reload_servers()
            set_message(msg)

    @kb.add("c-c")
    @kb.add("c-d")
    @kb.add("escape")
    def _exit(event):
        if state["mode"] in ("add", "input_env", "confirm_delete", "custom_notice"):
            # 返回 list 模式，不退出应用
            state["mode"] = "list"
            state["input_idx"] = 0
            state["input_values"] = {}
            state["input_value"] = ""
            state["message"] = ""
            invalidate()
        else:
            event.app.exit()
            if on_closed:
                on_closed()

    # 退格（input_env 模式）
    @kb.add("backspace")
    def _backspace(event):
        if state["mode"] == "input_env":
            if state["input_value"]:
                state["input_value"] = state["input_value"][:-1]
                invalidate()

    # 粘贴（input_env 模式）：Ctrl+V / Shift+Insert 从系统剪贴板粘贴文本
    @kb.add("c-v")
    @kb.add("s-insert")
    def _paste(event):
        if state["mode"] == "input_env":
            text = _get_clipboard_text()
            if text:
                # 剪贴板内容可能带换行，输入字段通常只需要单行，取第一行
                text = text.splitlines()[0] if text else ""
                state["input_value"] += text
                invalidate()
            else:
                set_message("剪贴板为空或无法读取")

    # 字符输入与单字符命令：统一在 <any> 处理器中处理
    # 重要：不能用 @kb.add("n") 等单字符绑定，否则在 input_env 模式下
    # 这些字符会被特定绑定拦截，无法输入到 API key 中（按键不冒泡到 <any>）。
    # <any> 是最低优先级绑定，只有当没有更具体的绑定匹配时才触发，
    # 因此 up/down/enter/backspace/escape 等控制键仍由各自绑定处理，
    # 而所有其他可打印字符在这里统一分发。
    @kb.add("<any>")
    def _any_key(event):
        try:
            ch = event.key_sequence[0].key
        except Exception:
            return
        # 只处理单字符可打印输入（ctrl/alt 组合键的 key 名长度 > 1，跳过）
        if len(ch) != 1 or not ch.isprintable():
            return
        mode = state["mode"]
        if mode == "input_env":
            # input_env 模式：接收所有字符作为输入
            state["input_value"] += ch
            invalidate()
        elif mode == "list":
            # list 模式：单字符命令 n/d/r
            low = ch.lower()
            if low == "n":
                state["mode"] = "add"
                state["add_idx"] = 0
                state["message"] = ""
                invalidate()
            elif low == "d" and state["servers"]:
                state["mode"] = "confirm_delete"
                invalidate()
            elif low == "r":
                ok, msg = mcp_manager.reconnect()
                reload_servers()
                set_message(msg)
        elif mode == "confirm_delete":
            # 确认删除：y 确认
            if ch.lower() == "y":
                srv = state["servers"][state["cursor"]]
                ok, msg = mcp_manager.remove_server(srv["name"])
                state["mode"] = "list"
                reload_servers()
                set_message(msg)

    h = _header_line_count()

    def _mouse_handler(mouse_event):
        """处理 body_window 的鼠标事件：右键释放时粘贴剪贴板。"""
        if state["mode"] != "input_env":
            return None
        if (
            mouse_event.button == MouseButton.RIGHT
            and mouse_event.event_type == MouseEventType.MOUSE_UP
        ):
            text = _get_clipboard_text()
            if text:
                # 剪贴板内容可能带换行，输入字段通常只需要单行，取第一行
                text = text.splitlines()[0] if text else ""
                state["input_value"] += text
                invalidate()
            return None
        return None

    # 主体 Window：可滚动，需保存引用以便 Layout 显式 focus
    # 关键点：prompt_toolkit 的 auto-scroll 只对"拥有焦点的 Window"生效，
    # 因此必须 layout.focus(body_window)，否则 get_cursor_position 返回的光标位置
    # 不会触发窗口滚动（表现为按 ↓ 无法向下翻页）。
    body_window = Window(
        content=FormattedTextControl(
            render_body,
            get_cursor_position=get_cursor_position,
            focusable=True,
            mouse_handler=_mouse_handler,
        ),
        wrap_lines=False,
        allow_scroll_beyond_bottom=False,
        # 界面用自定义文本渲染输入光标（`> _`），隐藏终端物理光标，
        # 避免光标停留在标题左上角等无关位置。
        always_hide_cursor=True,
    )

    root_container = HSplit([
        # 顶部：固定 Header
        Window(
            content=FormattedTextControl(render_header),
            height=h,
            dont_extend_height=True,
            wrap_lines=False,
        ),
        # 中间：可滚动主体
        body_window,
        # 底部：固定 Footer
        Window(
            content=FormattedTextControl(render_footer),
            height=1,
            dont_extend_height=True,
            wrap_lines=False,
            style="class:footer",
        ),
    ])

    layout = Layout(root_container)
    # 显式 focus 主体窗口，启用 prompt_toolkit 的 auto-scroll
    layout.focus(body_window)

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=STYLE,
        full_screen=True,
        mouse_support=True,
    )
    app_ref[0] = app

    app.run()
