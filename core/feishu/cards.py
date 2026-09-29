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


def build_result_card(title: str, lines: List[str]) -> Dict[str, Any]:
    """操作结果卡（随回调响应帧返回，原地替换被点的卡）。"""
    negative = any(word in title for word in ("拒绝", "取消", "失败", "超时", "已处理", "无权"))
    return card2(title, TEMPLATE_RED if negative else TEMPLATE_GREEN, [md(line) for line in lines if line])


# ---------------------------------------------------------------- 业务卡片

def context_markdown(session_label: str, workdir: str = "") -> str:
    lines = [f"**会话**　{session_label}"]
    if workdir:
        lines.append(f"**工作目录**　{workdir}")
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
        # 卡上直接追问：表单提交后回调 action.form_value = {"reply": 文本}
        elements.append(form("reply_form", [
            input_field("reply", "继续这个话题…"),
            submit_btn("发送", "reply"),
        ]))
    elements.append(note("星瑶 · 引用回复本卡亦可 · 直接在会话里发消息同样有效"))
    return card2(title, template, elements)


def failure_card(session_label: str, message: str) -> Dict[str, Any]:
    body = [md(context_markdown(session_label)), hr(), md(clip(message or "未知错误"))]
    return card2("⚠️ 星瑶执行出错", TEMPLATE_RED, body)


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
