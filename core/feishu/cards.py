"""飞书卡片 2.0 构件库（对齐 opencode-mobile `src/feishu/cards.ts`）。

注释里这几条是实测踩出来的红线，别改：
1. 2.0 不支持 1.0 的 `note` 标签（报 230099）——落款一律用 markdown；
2. 按钮回调必须 `behaviors:[{type:'callback',value}]`，旧写法在长连接下收不到回调；
3. 回调结果卡必须包成 `{card:{type:'raw',data}}` 随响应帧返回，裸卡片会被飞书回滚；
4. `input` 的 `max_length` 上限 1000，超了整卡被拒（230099 / 11310）；
5. `collapsible_panel` 老租户不支持会被整卡拒——只在正文真放不下时用。
"""

import json
from typing import Any, Dict, List, Optional

SCHEMA = "2.0"
MAX_MARKDOWN_CHARS = 4000
MAX_INPUT_LENGTH = 1000

TEMPLATE_GREEN = "green"
TEMPLATE_ORANGE = "orange"
TEMPLATE_RED = "red"
TEMPLATE_BLUE = "blue"

CardElement = Dict[str, Any]


# ---------------------------------------------------------------- 基础构件

def card2(title: str, template: str, elements: List[CardElement]) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "body": {"elements": [e for e in elements if e]},
    }


def md(content: str) -> CardElement:
    return {"tag": "markdown", "content": content or ""}


def note(content: str) -> CardElement:
    """落款/提示行：2.0 无 note 标签，用 markdown 替代（红线 1）。"""
    return md(content)


def btn(text: str, type_: str, value: Dict[str, Any]) -> CardElement:
    """按钮：必须走 callback 行为（红线 2）。"""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": type_,
        "size": "medium",
        "behaviors": [{"type": "callback", "value": value or {}}],
    }


def btn_row(left: CardElement, right: CardElement) -> CardElement:
    return {
        "tag": "column_set",
        "flex_mode": "bisect",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [left]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [right]},
        ],
    }


def btn_flow(buttons: List[CardElement]) -> CardElement:
    return {
        "tag": "column_set",
        "flex_mode": "flow",
        "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in buttons],
    }


def input_field(name: str, placeholder: str, max_length: int = MAX_INPUT_LENGTH) -> CardElement:
    """表单输入框：提交后回调 `action.form_value` 携带 `{name: 值}`。"""
    return {
        "tag": "input",
        "name": name,
        "max_length": min(int(max_length or MAX_INPUT_LENGTH), MAX_INPUT_LENGTH),  # 红线 4
        "placeholder": {"tag": "plain_text", "content": placeholder},
    }


def form(name: str, elements: List[CardElement]) -> CardElement:
    return {"tag": "form", "name": name, "elements": elements}


def submit_btn(text: str, route_name: str) -> CardElement:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": "primary",
        "size": "medium",
        "form_action_type": "submit",
        "name": route_name,
    }


def collapse(title: str, elements: List[CardElement]) -> CardElement:
    """折叠面板（红线 5：字段多一个整卡被拒；无 expand 属性，默认折叠态）。"""
    return {
        "tag": "collapsible_panel",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "background_color": "grey",
            "vertical_align": "center",
        },
        "border": {"color": "grey", "corner_radius": "6px"},
        "elements": elements,
    }


def hr() -> CardElement:
    return {"tag": "hr"}


def clip(text: str, max_chars: int = MAX_MARKDOWN_CHARS) -> str:
    value = text or ""
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 20] + "\n\n…（内容过长已截断）"


def card_response(card: Dict[str, Any]) -> Dict[str, Any]:
    """响应帧包装：裸卡片会被飞书回滚（红线 3）。"""
    return {"card": {"type": "raw", "data": card}}


def with_status(card: Dict[str, Any], text: str) -> Dict[str, Any]:
    """在卡片副本末尾追加一行状态（原地回执用，保留原内容）。

    飞书会用回调返回的卡片**原地替换**被点的那张卡：直接返回一张小回执卡会把
    原内容（比如星瑶的回复）冲掉。这里基于**原始卡片的副本**追加状态行——
    原内容保留，且每次都从原始卡片重建，反复提交不会累积多行状态。
    """
    import copy

    new_card = copy.deepcopy(card or {})
    body = new_card.setdefault("body", {})
    elements = body.setdefault("elements", [])
    elements.append(md(text))
    return new_card


def build_result_card(title: str, lines: List[str]) -> Dict[str, Any]:
    """操作结果卡（随回调响应帧返回，原地替换被点的卡）。"""
    negative = any(word in title for word in ("拒绝", "取消", "失败", "超时", "已处理", "无权"))
    return card2(title, TEMPLATE_RED if negative else TEMPLATE_GREEN, [md(line) for line in lines if line])


# ---------------------------------------------------------------- 业务卡片

def context_markdown(session_label: str, workdir: str = "") -> str:
    """卡片上的「会话 / 工作目录」两行。

    工作目录一律给出行：`GLOBAL`（自由对话）没有真实目录，显示成
    「自由对话（未绑定目录）」而不是留空——留空会让人以为"没取到"。
    """
    lines = [f"**会话**　{session_label}"]
    lines.append(f"**工作目录**　{workdir or '自由对话（未绑定目录）'}")
    return "\n".join(lines)


def completion_card(session_label: str, content: str, workdir: str = "",
                    cancelled: bool = False, with_reply: bool = True) -> Dict[str, Any]:
    """执行完成卡（被中止时同款换橙色标题）。"""
    title = "⏹ 星瑶已中止" if cancelled else "✅ 星瑶已完成"
    template = TEMPLATE_ORANGE if cancelled else TEMPLATE_GREEN
    elements: List[CardElement] = [md(context_markdown(session_label, workdir))]
    if content:
        elements += [hr(), md(clip(content))]
    if with_reply:
        # 卡上直接追问：表单提交后回调 action.form_value = {"reply_text": 文本}，
        # 路由名（提交按钮的 name）是 "reply"。
        # 红线：同一个 form 内各元素的 name 必须唯一——输入框与提交按钮若同名，
        # 飞书整卡拒收（实测 230099 / 11310: name(reply) duplicate）。
        elements.append(form("reply_form", [
            input_field("reply_text", "继续这个话题…"),
            submit_btn("发送", "reply"),
        ]))
    elements.append(note("星瑶 · 引用回复本卡亦可 · 直接在会话里发消息同样有效"))
    return card2(title, template, elements)


def failure_card(session_label: str, message: str) -> Dict[str, Any]:
    body = [md(context_markdown(session_label)), hr(), md(clip(message or "未知错误"))]
    return card2("⚠️ 星瑶执行出错", TEMPLATE_RED, body)


# ---------------------------------------------------------------- 审批 / 提问

# 动作名归一成中文（对齐 opencode-mobile「动作名被归一成中文，不是原始英文标识」）
ACTION_LABELS = {
    "run_command": "执行命令",
    "execute_command": "执行命令",
    "shell_command": "执行命令",
    "write_file": "写入文件",
    "create_file": "新建文件",
    "edit_file": "修改文件",
    "delete_file": "删除文件",
    "move_file": "移动文件",
    "read_file": "读取文件",
    "list_directory": "列出目录",
    "search_files": "搜索文件",
    "fetch_url": "访问网络",
    "external_directory": "访问项目外目录",
    "undo_turn": "撤销回合",
    "ask_user": "提问",
    "subagent": "子任务",
    "browser": "浏览器操作",
    "computer_use": "电脑操控",
}


def action_label(function_name: str) -> str:
    name = str(function_name or "")
    if name in ACTION_LABELS:
        return ACTION_LABELS[name]
    if name.startswith("browser_"):
        return "浏览器操作"
    if name.startswith("computer_"):
        return "电脑操控"
    return name or "未知操作"


def target_lines(arguments: Dict[str, Any], limit: int = 20, max_len: int = 300) -> List[str]:
    """把参数渲染成「目标」行（列表参数按条目展开，便于按条数折叠）。"""
    if not isinstance(arguments, dict):
        return []
    preferred = ("path", "paths", "file", "files", "command", "cmd", "pattern", "query", "url", "target")
    lines: List[str] = []
    for key in preferred:
        if key not in arguments:
            continue
        value = arguments[key]
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value]
            items = [v for v in items if v]
            for item in items[:limit]:
                lines.append(f"**{key}**　{clip(item, max_len)}")
            if len(items) > limit:
                lines.append(f"**{key}**　…（还有 {len(items) - limit} 项）")
            continue
        text = str(value).strip()
        if text:
            lines.append(f"**{key}**　{clip(text, max_len)}")
    return lines


def permission_card(*, action_name: str, summary: str, targets: List[str], session_label: str,
                    workdir: str = "", request_id: str, allow_always: bool,
                    buttons_enabled: bool = True, hint: str = "") -> Dict[str, Any]:
    """权限审批卡。未配置审批白名单时 buttons_enabled=False（fail-closed，不渲染按钮）。"""
    title = f"⛔ 需要授权 · {action_label(action_name)}"
    elements: List[CardElement] = [
        md(f"**动作**　{action_label(action_name)}（{action_name}）"),
    ]
    if summary:
        elements.append(md(clip(summary)))
    if targets:
        if len(targets) > 5 or any(len(t) > 300 for t in targets):
            elements.append(collapse(f"全部目标（{len(targets)} 项）", [md(t) for t in targets]))
        else:
            elements.append(md("**目标**\n" + "\n".join(targets)))
    elements.append(md(context_markdown(session_label, workdir)))
    if hint:
        elements.append(note(hint))

    if buttons_enabled:
        buttons = [
            btn("✅ 批准一次", "primary", {"kind": "perm", "request_id": request_id, "allow": True, "always": False}),
        ]
        if allow_always:
            buttons.append(btn("✅ 总是批准", "primary",
                               {"kind": "perm", "request_id": request_id, "allow": True, "always": True}))
        buttons.append(btn("✖ 拒绝", "danger", {"kind": "perm", "request_id": request_id, "allow": False}))
        elements.append(btn_flow(buttons))
    return card2(title, TEMPLATE_RED, elements)


def question_card(*, question: str, options: List[str], session_label: str,
                  request_id: str, buttons_enabled: bool = True, hint: str = "") -> Dict[str, Any]:
    """提问卡：选项按钮 + 自由输入框（提交后回调 form_value={"answer": 文本}）。"""
    elements: List[CardElement] = [md(clip(question or "星瑶有问题需要你确认"))]
    if buttons_enabled:
        for index, option in enumerate(options):
            elements.append(btn(str(option), "default",
                                {"kind": "ask", "request_id": request_id, "index": index}))
        elements.append(form("answer_form", [
            input_field("answer", "其他（自己输入）"),
            submit_btn("✅ 提交回答", "ask"),
        ]))
    elements.append(md(context_markdown(session_label)))
    if hint:
        elements.append(note(hint))
    return card2("❓ 星瑶提问", TEMPLATE_BLUE, elements)


# ---------------------------------------------------------------- 解析

def extract_text(message: Dict[str, Any]) -> Optional[str]:
    """从飞书消息体取纯文本（非文本消息返回 None）。"""
    if str(message.get("message_type") or "") != "text":
        return None
    raw = message.get("content")
    if not raw:
        return ""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""
    return str((parsed or {}).get("text") or "").strip()


def strip_mention(text: str) -> str:
    """去掉群里 @机器人 留下的占位前缀（与上游 gateway 一致）。"""
    value = (text or "").lstrip()
    while value.startswith("@"):
        parts = value.split(None, 1)
        if len(parts) < 2:
            return ""
        value = parts[1].lstrip()
    return value
