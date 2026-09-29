"""飞书指令集：解析 `/xxx` 并构造对应的卡片（对齐 opencode-mobile 的指令表）。

指令表：/help /panel /status /new [标题] /list /switch <序号> /model [名] /agent /inbox /stop
除斜杠指令外的任何消息都直接作为 prompt 发给当前绑定的星瑶会话。

翻页是**无状态**的：页码放在按钮的 callback value 里（`{"kind":"cmd","cmd":"/list","arg":"page:2"}`），
服务端不保存翻页游标——与 opencode-mobile 的「无状态翻页」一致。
"""

from typing import Any, Dict, List, Optional, Tuple

from core.feishu.cards import (
    TEMPLATE_BLUE,
    btn,
    btn_flow,
    card2,
    clip,
    md,
    note,
)

PAGE_SIZE = 8

HELP_TEXT = """**星瑶 · 飞书指令**

- `/help`　本帮助
- `/panel`　功能面板（按钮即指令）
- `/status`　绑定与连接状态
- `/new [标题]`　新建星瑶会话并绑定到本会话
- `/list`　会话列表（可切换绑定）
- `/switch <序号>`　按列表序号切换绑定
- `/model [序号或名称]`　模型列表 / 直接切换
- `/agent`　查看当前人设（人设切换在桌面端设置里改）
- `/inbox`　待拍板收件箱（权限待批 + 提问待答）
- `/stop`　中止当前会话正在跑的任务

其余任何消息都会直接发给当前绑定的星瑶会话。"""


def parse_command(text: str) -> Tuple[str, str]:
    """把 `/cmd arg...` 拆成 (命令, 参数)。非指令返回 ("", "")。"""
    value = (text or "").strip()
    if not value.startswith("/"):
        return "", ""
    parts = value.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


def _page_from_arg(arg: str) -> int:
    if arg.startswith("page:"):
        try:
            return max(1, int(arg.split(":", 1)[1]))
        except ValueError:
            return 1
    return 1


def _pager(cmd: str, page: int, total: int) -> Optional[Dict[str, Any]]:
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if pages <= 1:
        return None
    buttons = []
    if page > 1:
        buttons.append(btn("◀ 上一页", "default", {"kind": "cmd", "cmd": cmd, "arg": f"page:{page - 1}"}))
    buttons.append(btn(f"第 {page}/{pages} 页", "default", {"kind": "cmd", "cmd": cmd, "arg": f"page:{page}"}))
    if page < pages:
        buttons.append(btn("下一页 ▶", "default", {"kind": "cmd", "cmd": cmd, "arg": f"page:{page + 1}"}))
    return btn_flow(buttons)


def panel_card() -> Dict[str, Any]:
    """功能面板：按钮即指令。"""
    return card2("🎛 星瑶 功能面板", TEMPLATE_BLUE, [
        btn_flow([
            btn("📥 收件箱", "default", {"kind": "cmd", "cmd": "/inbox"}),
            btn("📈 状态", "default", {"kind": "cmd", "cmd": "/status"}),
            btn("📜 会话列表", "default", {"kind": "cmd", "cmd": "/list"}),
        ]),
        btn_flow([
            btn("🆕 新建会话", "primary", {"kind": "cmd", "cmd": "/new"}),
            btn("⏹ 中止", "danger", {"kind": "cmd", "cmd": "/stop"}),
            btn("🧠 模型", "default", {"kind": "cmd", "cmd": "/model"}),
            btn("🤖 人设", "default", {"kind": "cmd", "cmd": "/agent"}),
        ]),
    ])


def status_card(*, session_label: str, session_name: str, workdir: str, model_name: str,
                connection_state: str, approvers_count: int, pending_count: int,
                agent_name: str) -> Dict[str, Any]:
    lines = [
        f"**连接**　{connection_state}",
        f"**绑定会话**　{session_label or '（未绑定）'}",
        f"**会话 ID**　{session_name or '—'}",
        f"**工作目录**　{workdir or '自由对话'}",
        f"**当前模型**　{model_name or '—'}",
        f"**人设**　{agent_name or '星瑶'}",
        f"**审批白名单**　{approvers_count} 人",
        f"**待拍板**　{pending_count} 项",
    ]
    if approvers_count == 0:
        lines.append("")
        lines.append("⚠️ 未配置审批白名单，飞书侧暂不可审批（设置 → 飞书集成）")
    return card2("📈 星瑶 状态", TEMPLATE_BLUE, [md("\n".join(lines))])


def session_list_card(*, sessions: List[Dict[str, Any]], current_session: str, page: int) -> Dict[str, Any]:
    """会话列表卡：当前项打标 + 点按钮切换 + 无状态翻页。"""
    elements: List[Dict[str, Any]] = [
        btn_flow([btn("🆕 新建会话", "primary", {"kind": "cmd", "cmd": "/new"})]),
    ]
    start = (page - 1) * PAGE_SIZE
    page_items = sessions[start:start + PAGE_SIZE]
    if not page_items:
        elements.append(md("（还没有会话）"))
    for offset, item in enumerate(page_items):
        index = start + offset + 1
        name = str(item.get("name") or "")
        title = str(item.get("display_name") or name)
        is_current = name == current_session
        marker = "（当前）" if is_current else ""
        elements.append(md(f"**{index}. {title}**{marker}\n`{name}`"))
        if is_current:
            elements.append(btn_flow([btn("📍 当前会话", "default", {"kind": "cmd", "cmd": "/status"})]))
        else:
            elements.append(btn_flow([btn("💬 切换到此会话", "primary",
                                          {"kind": "cmd", "cmd": "/switch", "arg": str(index)})]))
    pager = _pager("/list", page, len(sessions))
    if pager:
        elements.append(pager)
    return card2("🖥 星瑶 会话", TEMPLATE_BLUE, elements)


def model_list_card(*, models: List[Dict[str, Any]], active_model_id: str, page: int) -> Dict[str, Any]:
    elements: List[Dict[str, Any]] = []
    start = (page - 1) * PAGE_SIZE
    page_items = models[start:start + PAGE_SIZE]
    if not page_items:
        elements.append(md("（没有可用模型，请先在桌面端配置厂商与模型）"))
    for offset, item in enumerate(page_items):
        index = start + offset + 1
        model_id = str(item.get("id") or "")
        name = str(item.get("name") or model_id)
        provider = str(item.get("provider") or "")
        is_current = model_id == active_model_id
        marker = "（当前）" if is_current else ""
        elements.append(md(f"**{index}. {name}**{marker}\n`{model_id}`" + (f" · {provider}" if provider else "")))
        if is_current:
            elements.append(btn_flow([btn("📍 当前模型", "default", {"kind": "cmd", "cmd": "/status"})]))
        else:
            elements.append(btn_flow([btn("🧠 切换到此模型", "primary",
                                          {"kind": "cmd", "cmd": "/model", "arg": model_id})]))
    pager = _pager("/model", page, len(models))
    if pager:
        elements.append(pager)
    return card2("🧠 星瑶 模型", TEMPLATE_BLUE, elements)


def agent_card(*, agent_name: str, model_name: str) -> Dict[str, Any]:
    """星瑶没有人格化的多 Agent 概念：这里只读展示人设与模型。"""
    lines = [
        f"**当前人设**　{agent_name or '星瑶'}",
        f"**当前模型**　{model_name or '—'}",
        "",
        "星瑶没有多 Agent 切换（与 opencode 的 Agent 概念不同）；",
        "人设名称与提示词在桌面端「设置 → 人设设置」里修改。",
    ]
    return card2("🤖 星瑶 人设", TEMPLATE_BLUE, [md("\n".join(lines))])


def inbox_card(*, items: List[Dict[str, Any]], page: int) -> Dict[str, Any]:
    """待拍板收件箱：权限待批 + 提问待答。"""
    if not items:
        return card2("📥 待拍板收件箱", TEMPLATE_BLUE, [md("✅ 没有等你拍板的事。")])
    elements: List[Dict[str, Any]] = []
    start = (page - 1) * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]
    for offset, item in enumerate(page_items):
        index = start + offset + 1
        kind = "提问" if item.get("is_question") else "权限"
        elements.append(md(
            f"**{index}. [{kind}] {clip(str(item.get('title') or ''), 80)}**\n"
            f"会话 {item.get('session_label') or '—'}"
        ))
        elements.append(btn_flow([btn("▶ 处理", "primary",
                                      {"kind": "cmd", "cmd": "/inbox", "arg": f"open:{item.get('request_id')}"})]))
    pager = _pager("/inbox", page, len(items))
    if pager:
        elements.append(pager)
    return card2("📥 待拍板收件箱", TEMPLATE_BLUE, elements)


def stall_card(*, session_label: str, last_text: str, minutes: int) -> Dict[str, Any]:
    """卡住提醒卡。"""
    elements = [
        md(f"**会话**　{session_label}\n**最近指令**　{clip(last_text or '—', 120)}\n"
           f"已 {minutes} 分钟无任何输出。"),
        btn_flow([
            btn("💬 切换到此会话", "primary", {"kind": "cmd", "cmd": "/status"}),
            btn("⏹ 中止执行", "danger", {"kind": "cmd", "cmd": "/stop"}),
        ]),
        note("忽略（它可能只是在跑长任务）——同一会话 2 小时内最多提醒一次"),
    ]
    return card2("🐢 星瑶疑似卡住", "orange", elements)


def text_card(title: str, text: str, template: str = TEMPLATE_BLUE) -> Dict[str, Any]:
    """纯信息卡（/help 等）。"""
    return card2(title, template, [md(clip(text))])
