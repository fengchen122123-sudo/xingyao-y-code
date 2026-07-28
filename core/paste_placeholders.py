"""粘贴大段文本时折叠为占位符（类 Kimi Code CLI 体验）。

输入框只有 2 行高，粘贴多行/长文本会全部堆进输入区。这里把超阈值的
粘贴内容折叠成 `[粘贴文本 #N · 共 M 行]` 占位符插入 Buffer，原文存进
内存映射表；提交时再用 expand() 把占位符还原成完整原文发送给模型。

设计要点：
- 占位符在 Buffer 里是普通文本，用户可编辑/删除；删坏后正则匹配不上，
  expand() 会按字面文本原样保留，不会报错。
- 纯内存存储，不做磁盘持久化；输入历史（FileHistory）里存的是展开后
  的完整原文，所以跨会话用 ↑ 翻历史拿到的也是原文，与这里的映射表无关。
"""

from __future__ import annotations

import re

# 折叠阈值：行数 >= 3 或 字符数 >= 200 才折叠（输入框只有 2 行高，低阈值收益最大）
PASTE_CHAR_THRESHOLD = 200
PASTE_LINE_THRESHOLD = 3

_PLACEHOLDER_RE = re.compile(r"\[粘贴文本 #(?P<id>\d+)(?: · 共 (?P<lines>\d+) 行)?\]")


def normalize_pasted_text(text: str) -> str:
    """归一化粘贴文本：\\r\\n / \\r 统一为 \\n，并剔除孤立代理对字符
    （Windows 剪贴板偶见，直接插入会让后续 utf-8 编码炸掉）。"""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.encode("utf-8", "ignore").decode("utf-8", "ignore")


class PastedTextStore:
    """粘贴原文的内存映射表：占位符 id -> 完整原文。"""

    def __init__(self) -> None:
        self._entries: dict[int, str] = {}
        self._next_id = 1

    @staticmethod
    def _build_placeholder(paste_id: int, text: str) -> str:
        line_count = text.count("\n") + 1
        if line_count <= 1:
            return f"[粘贴文本 #{paste_id}]"
        return f"[粘贴文本 #{paste_id} · 共 {line_count} 行]"

    def maybe_placeholderize(self, text: str) -> str:
        """超阈值则登记原文并返回占位符，否则原样返回。"""
        line_count = text.count("\n") + 1
        if len(text) < PASTE_CHAR_THRESHOLD and line_count < PASTE_LINE_THRESHOLD:
            return text
        paste_id = self._next_id
        self._next_id += 1
        self._entries[paste_id] = text
        return self._build_placeholder(paste_id, text)

    def expand(self, text: str) -> str:
        """把文本中的占位符还原为原文；未登记的占位符按字面文本保留。"""
        def _sub(match: re.Match) -> str:
            entry = self._entries.get(int(match.group("id")))
            return entry if entry is not None else match.group(0)

        return _PLACEHOLDER_RE.sub(_sub, text)
