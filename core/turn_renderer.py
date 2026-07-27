"""Deterministic, terminal-only rendering for ordered turn events."""

from __future__ import annotations

import re
from typing import Any, Iterable

from core.thinking_stream import ThinkingStreamBuffer
from core.ui_markers import (
    ANSI_CYAN,
    ANSI_GRAY,
    ANSI_GREEN,
    ANSI_RED,
    ANSI_RESET,
    ANSI_YELLOW,
    MARK_DONE,
    MARK_FAILED,
    plan_color,
    plan_mark,
)


_OPEN_TAGS = ("<thinking>", "<think>")
_CLOSE_TAGS = ("</thinking>", "</think>")
_CODE_BLOCK_FENCES = ("```", "~~~")
_DIFF_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_DIFF_MARKER_RE = re.compile(r"^[\d\s]*([+\-])")


def _count_diff_changes(diff_text: str) -> tuple[int, int]:
    """粗略统计 diff 展示文本中的新增/删除行数，用于简短模式的单行摘要。

    diff_text 既可能是原始 unified diff，也可能是渲染成 Rich 表格/面板后的
    文本（core.llm._show_file_diff 生成，行首会带行号列与面板边框）。两种
    形式在剥离 ANSI 转义码、去掉行号列/边框后，内容列仍然以原始的 +/-/空格
    diff 前缀开头，因此可以用同一套启发式识别。
    """
    added = 0
    removed = 0
    for raw_line in diff_text.split("\n"):
        line = _DIFF_ANSI_RE.sub("", raw_line).strip().strip("│").strip()
        if line.startswith("+++") or line.startswith("---"):
            continue
        match = _DIFF_MARKER_RE.match(line)
        if not match:
            continue
        if match.group(1) == "+":
            added += 1
        else:
            removed += 1
    return added, removed


def _find_tag(text: str, tags: Iterable[str]) -> tuple[int, str] | None:
    found: list[tuple[int, str]] = []
    for tag in tags:
        index = text.find(tag)
        if index >= 0:
            found.append((index, tag))
    if not found:
        return None
    return min(found, key=lambda item: (item[0], -len(item[1])))


def _safe_prefix_length(text: str, tags: Iterable[str]) -> int:
    """Return the part that cannot be the beginning of a split tag."""
    tag_list = tuple(tags)
    max_tag_length = max((len(tag) for tag in tag_list), default=0)
    start = max(0, len(text) - max_tag_length + 1)
    for index in range(len(text), start - 1, -1):
        suffix = text[index:]
        if suffix and any(tag.startswith(suffix) for tag in tag_list):
            return index
    return len(text)


def sanitize_protocol_text(text: str) -> str:
    """清理输出文本中的内部协议标记、Advisor 调试头及裸露的 XML <tool_call> 标签。"""
    if not text or not isinstance(text, str):
        return text
    text = re.sub(r'<tool_call[^>]*>.*?</tool_call>', '', text, flags=re.DOTALL)
    text = re.sub(r'</?tool_call[^>]*>', '', text)
    text = re.sub(r'\[Advisor consultation[^\]]*\]', '', text)
    text = re.sub(r'\[Advisor review\]', '', text)
    text = re.sub(r'\[End of advisor consultation[^\]]*\]', '', text)
    # 圆括号形式的 advisor 内部说明（如 "(advisor returned no content)"）也会泄漏到正文，
    # 同样需要清理
    text = re.sub(r'\(\s*advisor[^\)]*\)', '', text, flags=re.IGNORECASE)
    return text


class TurnRenderer:
    """Accumulate assistant text and tool status lines without doing I/O."""

    def __init__(self, tool_verbose: bool = True) -> None:
        self._records: list[str] = []
        self._pending_text = ""
        self._in_thinking = False
        self._tool_lines: dict[str, tuple[str, str]] = {}
        self._errors: set[str] = set()
        self._emitted: list[str] = []
        self._finished = False
        self._completed_tool_count = 0
        # 工具调用细节是否在屏幕打印（False=简短模式：只显示"星瑶正在干活"汇总行）
        # 数据库记录不受此开关影响，event 仍带完整 summary 入库
        self._tool_verbose = bool(tool_verbose)
        self._completed_tools: list[tuple[str, str]] = []
        self._failed_tool_count = 0
        # 简短模式下"星瑶正在干活…"汇总行是否已打印过（每回合只打印一次）
        self._concise_header_emitted = False
        # 简短模式的收尾统计行是否已经生成过（避免重复调用 concise_recap 时重复输出）
        self._recap_emitted = False
        self._has_meaningful_assistant_text = False
        self._thinking_stream = ThinkingStreamBuffer()
        self._assistant_text_parts: list[str] = []
        self._suppress_assistant_emit = False
        self._in_code_block = False
        self._code_fence = ""

    def consume(self, kind: str, payload: dict[str, Any] | None = None) -> list[str]:
        if self._finished:
            return []
        self._emitted = []
        data = payload or {}

        if kind == "thinking_delta":
            text = str(data.get("text", ""))
            # 内部推理内容只消费不渲染
            for _ in self._thinking_stream.feed(text):
                pass
        elif kind == "thinking_finished":
            self._flush_thinking()
        elif kind == "assistant_delta":
            self._consume_text(str(data.get("text", "")))
        elif kind == "tool_requested":
            self._tool_requested(data)
        elif kind == "tool_finished":
            self._tool_finished(data)
        elif kind == "error":
            self._error(str(data.get("message", "Unknown error")))
        elif kind == "diff_ready":
            diff = str(data.get("text") or data.get("diff") or "")
            if diff:
                if self._tool_verbose:
                    self._append_line(diff)
                else:
                    file_path = str(data.get("file_path") or "").strip()
                    added, removed = _count_diff_changes(diff)
                    self._append_line(
                        f"✎ {ANSI_CYAN}{file_path}{ANSI_RESET}"
                        f"（{ANSI_GREEN}+{added}{ANSI_RESET} 行 / "
                        f"{ANSI_RED}-{removed}{ANSI_RESET} 行）"
                    )
        elif kind == "plan_updated":
            if data.get("live_only"):
                return []
            for item in data.get("items", []):
                status = str(item.get("status", "pending"))
                subject = str(item.get("subject", "")).strip()
                if subject:
                    mark = plan_mark(status)
                    color = plan_color(status)
                    self._append_line(f"  {color}{mark}{ANSI_RESET} {subject}")

        return list(self._emitted)

    def _consume_text(self, chunk: str) -> None:
        self._pending_text += chunk
        while self._pending_text:
            if self._in_thinking:
                closing = _find_tag(self._pending_text, _CLOSE_TAGS)
                if closing is None:
                    self._pending_text = self._pending_text[
                        _safe_prefix_length(self._pending_text, _CLOSE_TAGS):
                    ]
                    return
                index, tag = closing
                self._pending_text = self._pending_text[index + len(tag):]
                self._in_thinking = False
                continue

            if self._in_code_block:
                fence = self._code_fence
                closing = self._pending_text.find(fence)
                if closing == -1:
                    safe_length = _safe_prefix_length(
                        self._pending_text, (fence,)
                    )
                    if safe_length:
                        self._append_text(self._pending_text[:safe_length])
                        self._pending_text = self._pending_text[safe_length:]
                    return
                self._append_text(
                    self._pending_text[:closing + len(fence)]
                )
                self._pending_text = self._pending_text[closing + len(fence):]
                self._in_code_block = False
                self._code_fence = ""
                continue

            think_opening = _find_tag(self._pending_text, _OPEN_TAGS)
            code_opening = _find_tag(self._pending_text, _CODE_BLOCK_FENCES)

            # 代码块开始早于 think 开始：先处理代码块，跳过其中的 think 标签
            if code_opening is not None and (
                think_opening is None or code_opening[0] < think_opening[0]
            ):
                index, fence = code_opening
                if index:
                    # 递归处理代码块之前的普通文本（其中可能含 think 标签）
                    suffix = self._pending_text[index:]
                    self._pending_text = self._pending_text[:index]
                    self._consume_text("")
                    if self._pending_text:
                        # 递归调用后仍保留了被保守持留的尾部（例如一个尚未完整的
                        # <think> 标签前缀）。此时 fence 已经不再位于位置 0，不能
                        # 再假设 self._pending_text[:len(fence)] 就是围栏本身——
                        # 原样拼回并直接返回，等待下一块数据到达后重新判断；
                        # 回合结束时 finish_incremental 会把剩余内容当纯文本冲刷。
                        self._pending_text += suffix
                        return
                    self._pending_text = suffix
                self._append_text(self._pending_text[:len(fence)])
                self._pending_text = self._pending_text[len(fence):]
                self._in_code_block = True
                self._code_fence = fence
                continue

            if think_opening is None:
                safe_length = _safe_prefix_length(self._pending_text, _OPEN_TAGS)
                # 末尾的 ` 或 ~ 可能是 fenced code block 的开始，保守保留，
                # 避免 ```/~~~ 被拆散后无法识别为代码块。
                while safe_length > 0 and self._pending_text[safe_length - 1] in "`~":
                    safe_length -= 1
                if safe_length:
                    self._append_text(self._pending_text[:safe_length])
                    self._pending_text = self._pending_text[safe_length:]
                return

            index, tag = think_opening
            if index:
                self._append_text(self._pending_text[:index])
            self._pending_text = self._pending_text[index + len(tag):]
            self._in_thinking = True

    def _append_text(self, text: str) -> None:
        clean_text = sanitize_protocol_text(text)
        if not clean_text:
            return
        self._assistant_text_parts.append(clean_text)
        if not self._suppress_assistant_emit and clean_text:
            self._emitted.append(clean_text)
        if not self._is_meaningful_text(clean_text):
            return
        self._has_meaningful_assistant_text = True
        if self._records and not self._records[-1].endswith("\n"):
            self._records[-1] += clean_text
        else:
            self._records.append(clean_text)

    @staticmethod
    def _is_meaningful_text(text: str) -> bool:
        return bool(re.sub(r"[\s`*_#>~\-\[\](){}|.,!?，。！？:：;；/\\]+", "", text))

    def _append_line(self, text: str) -> None:
        self._finish_current_line()
        line = text.rstrip("\n") + "\n"
        self._records.append(line)
        self._emitted.append(line)

    def _finish_current_line(self) -> None:
        if self._records and not self._records[-1].endswith("\n"):
            self._records.append("\n")
            self._emitted.append("\n")

    def _flush_thinking(self) -> None:
        # 内部推理内容只消费不渲染
        for _ in self._thinking_stream.finish():
            pass

    def _tool_label(self, data: dict[str, Any]) -> str:
        summary = str(data.get("summary") or "").strip()
        if summary:
            return summary
        name = str(data.get("name") or "工具")
        preview = str(data.get("preview") or "").strip()
        return f"{name}({preview})" if preview else name

    def _styled_tool_summary(self, data: dict[str, Any]) -> str:
        summary = self._tool_label(data)
        target = str(data.get("preview") or "").strip()
        if not target or target not in summary:
            return summary
        before, _, after = summary.partition(target)
        return f"{before}{ANSI_CYAN}{target}{ANSI_RESET}{after}"

    def _tool_requested(self, data: dict[str, Any]) -> None:
        tool_id = str(data.get("id") or self._tool_label(data))
        label = self._tool_label(data)
        self._tool_lines[tool_id] = (label, "")
        # 详细模式：不再打印"星瑶正在 X"请求行，只做内部记录——工具的唯一一行
        # 反馈改到 _tool_finished 里打印（✓/✗ 汇总行），进行中的状态已经由
        # main.py 底部的活动指示行实时展示，这里重复打印只会让屏幕更吵。
        # 简短模式：仅首次工具调用时打印一行"星瑶正在干活"汇总（每回合一次）。
        if not self._tool_verbose and not self._concise_header_emitted:
            self._concise_header_emitted = True
            self._append_line(f"{ANSI_GREEN}●{ANSI_RESET} 星瑶正在干活…")

    def _tool_finished(self, data: dict[str, Any]) -> None:
        tool_id = str(data.get("id") or self._tool_label(data))
        requested_label = self._tool_lines.get(tool_id, ("", ""))[0]
        label = self._tool_label(data) if data.get("summary") else requested_label
        if not label:
            label = self._tool_label(data)
        ok = bool(data.get("ok", True))
        self._completed_tool_count += 1
        self._completed_tools.append((str(data.get("name", "tool")), label))
        if not ok:
            self._failed_tool_count += 1
        styled_summary = self._styled_tool_summary(data) if data.get("summary") else label
        if not self._tool_verbose:
            # 简短模式：所有工具调用（无论成功失败）都保持静默，不逐条刷屏；
            # 失败数量由回合收尾的 concise_recap 统计行汇总展示，
            # 用户可通过 /verbose on 切到详细模式查看每个工具的完整过程。
            return
        marker = MARK_DONE if ok else MARK_FAILED
        color = ANSI_GREEN if ok else ANSI_RED
        self._append_line(f"{color}{marker}{ANSI_RESET} {styled_summary}")

    def _error(self, message: str) -> None:
        if not message or message in self._errors:
            return
        self._errors.add(message)
        self._append_line(f"{ANSI_YELLOW}  ! 星瑶暂未完成当前步骤{ANSI_RESET}")

    def concise_recap(self) -> str:
        """简短模式下的回合收尾统计行：共执行 N 项操作。

        失败信息完全静默（不显示失败计数），用户可切到 /verbose on 查看详情。
        详细模式、本回合未执行任何工具、或本实例已经生成过收尾行时返回空串。
        由 main.py 在 turn_finished/turn_cancelled/turn_error 时调用一次，直接
        经 _print_ui_fragment 打印——不走 Markdown 渲染缓冲区，避免其中的
        ANSI 颜色码被当成 Markdown 源文本处理导致显示异常。
        """
        if self._tool_verbose or not self._completed_tools or self._recap_emitted:
            return ""
        self._recap_emitted = True
        total = len(self._completed_tools)
        return f"{ANSI_GRAY}● 共执行 {total} 项操作{ANSI_RESET}\n"

    def finish(self) -> str:
        if not self._finished:
            self._flush_pending()
            self._finished = True
        return "".join(self._records).rstrip("\n")

    def finish_incremental(self, include_assistant_text: bool = True) -> list[str]:
        """Flush only output that was waiting for a complete stream boundary."""
        if self._finished:
            return []
        self._emitted = []
        self._suppress_assistant_emit = not include_assistant_text
        try:
            self._flush_pending()
        finally:
            self._suppress_assistant_emit = False
        return list(self._emitted)

    def assistant_text(self) -> str:
        """Return the complete visible assistant reply after think tags are removed."""
        if not self._has_meaningful_assistant_text:
            return ""
        return "".join(self._assistant_text_parts)

    def _flush_pending(self) -> None:
        self._flush_thinking()
        if not self._in_thinking and self._pending_text:
            self._append_text(self._pending_text)
        self._pending_text = ""
        if self._has_meaningful_assistant_text:
            return
        if self._completed_tool_count:
            read_count = sum(name == "read_file" for name, _ in self._completed_tools)
            if read_count:
                summary = f"检查完成：已读取 {read_count} 个文件。模型未返回最终结论。"
            else:
                summary = f"检查完成：已执行 {self._completed_tool_count} 项工具操作。模型未返回最终结论。"
            self._append_line(f"  {summary}")
        else:
            # 回合结束但既无文字输出也未执行任何工具（如只思考未产出），
            # 给用户一个明确的兜底提示，避免看起来"什么都没发生"
            self._append_line("  星瑶本轮暂未返回内容。")

