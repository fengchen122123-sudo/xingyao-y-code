"""会话 AI 总结命名：prompt 构造、结果清洗与一次性生成。

只做无状态逻辑，不碰数据库与 GUI；GUI/CLI 各自负责调度与落库。
"""

from __future__ import annotations

import logging
import re

from core.llm_native_transport import native_completion

logger = logging.getLogger("py.warnings")

_TITLE_MAX_CHARS = 12
_MATERIAL_MAX_CHARS = 1200
_PLACEHOLDER_SESSION_RE = re.compile(r"^session_[a-f0-9]{6}$")
_PREFIX_RE = re.compile(r"^\s*(?:会话)?标题\s*[:：]\s*")
_MARKDOWN_RE = re.compile(r"[`*_#>\[\]()]")
_PUNCT_RE = re.compile(r'[\s"\'“”‘’，。！？、；：,.!?;:·…—\-]')


def is_placeholder_session_name(name) -> bool:
    """是否为新建会话的流水号占位名（session_xxxxxx）。"""
    return bool(_PLACEHOLDER_SESSION_RE.match(str(name or "")))


def message_text(content) -> str:
    """把字符串或多模态内容块统一转成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return " ".join(parts)
    return str(content or "")


def build_title_prompt(user_text, assistant_text) -> str:
    user_text = str(user_text or "")[:_MATERIAL_MAX_CHARS].strip()
    assistant_text = str(assistant_text or "")[:_MATERIAL_MAX_CHARS].strip()
    return (
        "你是会话起名助手。请根据下面这轮对话，提炼一个能概括本次任务主题的中文会话标题。\n"
        "要求：\n"
        "1. 必须是 4 到 12 个汉字，不要标点符号、引号、Markdown 或任何解释说明。\n"
        "2. 直接返回标题本身，不要加“标题：”前缀。\n"
        "3. 优先概括用户想完成的事情，其次参考助手的处理结果。\n\n"
        f"用户：{user_text}\n\n"
        f"助手：{assistant_text}\n\n"
        "会话标题："
    )


def sanitize_generated_title(raw):
    """清洗模型输出；清洗后为空或疑似占位名时返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if is_placeholder_session_name(text):
        return None
    text = text.splitlines()[0]
    text = _PREFIX_RE.sub("", text)
    text = _MARKDOWN_RE.sub("", text)
    text = text.strip().strip('"\'“”‘’')
    text = _PUNCT_RE.sub("", text)
    text = text.strip()[:_TITLE_MAX_CHARS]
    if not text or is_placeholder_session_name(text):
        return None
    return text


def generate_session_title(model, api_key, base_url, user_text, assistant_text, timeout=10):
    """调用当前会话模型生成标题；任何失败都返回 None，由调用方兜底。"""
    model = str(model or "").strip()
    if not model:
        return None
    if not str(user_text or "").strip() and not str(assistant_text or "").strip():
        return None
    try:
        response = native_completion(
            model=model,
            api_key=str(api_key or ""),
            api_base=str(base_url or ""),
            messages=[{
                "role": "user",
                "content": build_title_prompt(user_text, assistant_text),
            }],
            timeout=timeout,
        )
        content = response.choices[0].message.content
    except Exception:
        logger.warning("会话 AI 命名请求失败", exc_info=True)
        return None
    return sanitize_generated_title(content)
