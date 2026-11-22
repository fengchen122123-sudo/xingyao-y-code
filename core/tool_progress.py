from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import re
import time
from types import MappingProxyType
from typing import Callable, Mapping

from core.ui_markers import (
    ANSI_RESET,
    diff_stat_bar,
    theme_ansi,
)


_DISPLAY_CATEGORIES = frozenset(
    {"read", "search", "command", "mutation", "skill", "subagent", "computer", "other"}
)

_CATEGORY_BADGES = {
    "read": "读取",
    "search": "搜索",
    "command": "命令",
    "mutation": "编辑",
    "skill": "技能",
    "subagent": "子代理",
    "computer": "桌面",
    "other": "工具",
}

_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")


@dataclass(frozen=True)
class ToolDisplayEvent:
    tool_id: str
    name: str
    category: str
    action: str
    target: str
    command: str
    metrics: Mapping[str, int]
    error_summary: str
    is_skill: bool
    ok: bool | None
    duration_ms: int | None
    background: bool = False

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ToolDisplayEvent":
        """Normalize presentation metadata without inspecting raw tool inputs or output."""
        display_value = payload.get("display")
        display = display_value if isinstance(display_value, Mapping) else {}
        category = str(display.get("category") or "other")
        if category not in _DISPLAY_CATEGORIES:
            category = "other"

        metrics_value = display.get("metrics")
        metrics = MappingProxyType(
            {
                str(key): value
                for key, value in metrics_value.items()
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            }
            if isinstance(metrics_value, Mapping)
            else {}
        )

        duration_value = payload.get("duration_ms")
        duration_ms = (
            max(0, int(duration_value))
            if isinstance(duration_value, (int, float)) and not isinstance(duration_value, bool)
            else None
        )
        ok_value = payload.get("ok")
        name = str(payload.get("name") or "tool")
        return cls(
            tool_id=str(payload.get("id") or ""),
            name=name,
            category=category,
            action=str(display.get("action") or name),
            target=str(display.get("target") or ""),
            command=str(display.get("command") or ""),
            metrics=metrics,
            error_summary=str(display.get("error_summary") or ""),
            is_skill=bool(display.get("is_skill")),
            ok=ok_value if isinstance(ok_value, bool) else None,
            duration_ms=duration_ms,
            background=bool(display.get("background")),
        )


@dataclass(frozen=True)
class ActiveToolStatus:
    label: str
    detail: str
    count: int
    started_at: float


@dataclass
class _PendingGroup:
    category: str
    count: int = 0
    duration_ms: int = 0
    metrics: dict[str, int] = field(default_factory=dict)


def _format_compact_diff_preview(diff_text: str, *, max_lines: int = 6) -> list[str]:
    if not diff_text:
        return []
    muted = theme_ansi("text_muted")
    ok_c = theme_ansi("ok")
    err_c = theme_ansi("err")
    changed: list[str] = []
    old_ln = 0
    new_ln = 0
    total_changes = 0
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("--- ") or raw_line.startswith("+++ "):
            continue
        hunk = _HUNK_HEADER_RE.match(raw_line)
        if hunk:
            old_ln = int(hunk.group(1))
            new_ln = int(hunk.group(2))
            continue
        if raw_line.startswith("\\"):
            continue
        if raw_line.startswith("-"):
            total_changes += 1
            if len(changed) < max_lines:
                body = raw_line[1:][:96]
                changed.append(f"    {muted}│ {old_ln:>4}{ANSI_RESET}  {err_c}- {body}{ANSI_RESET}\n")
            old_ln += 1
        elif raw_line.startswith("+"):
            total_changes += 1
            if len(changed) < max_lines:
                body = raw_line[1:][:96]
                changed.append(f"    {muted}│ {new_ln:>4}{ANSI_RESET}  {ok_c}+ {body}{ANSI_RESET}\n")
            new_ln += 1
        else:
            old_ln += 1
            new_ln += 1
    if total_changes > max_lines:
        changed.append(
            f"    {muted}│       … 已省略其余 {total_changes - max_lines} 行变更{ANSI_RESET}\n"
        )
    return changed


class ToolProgressAggregator:
    """Aggregate adjacent successful reads/searches and format all other results."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._active: OrderedDict[str, tuple[ToolDisplayEvent, float]] = OrderedDict()
        self._pending: _PendingGroup | None = None
        self._diff_tool_ids: set[str] = set()
        self._finished_tool_ids: set[str] = set()

    def start(self, payload: dict) -> None:
        event = ToolDisplayEvent.from_payload(payload)
        if not event.tool_id:
            return
        self._active.pop(event.tool_id, None)
        self._finished_tool_ids.discard(event.tool_id)
        self._diff_tool_ids.discard(event.tool_id)
        self._active[event.tool_id] = (event, self._clock())

    def finish(self, payload: dict) -> list[str]:
        event = ToolDisplayEvent.from_payload(payload)
        if not event.tool_id:
            lines = self.flush()
            lines.append(self._protocol_error_line("缺少工具 ID"))
            return lines
        if event.tool_id in self._finished_tool_ids:
            return []

        active = self._active.pop(event.tool_id, None)
        self._finished_tool_ids.add(event.tool_id)
        if event.duration_ms is None:
            started_at = active[1] if active is not None else self._clock()
            event = replace(
                event,
                duration_ms=max(0, round((self._clock() - started_at) * 1000)),
            )

        if active is None:
            lines = self.flush()
            lines.extend(self._standalone_lines(event))
            return lines
        if event.ok is False:
            lines = self.flush()
            lines.extend(self._failure_lines(event))
            return lines
        if event.is_skill or event.category == "skill":
            lines = self.flush()
            accent = theme_ansi("accent")
            accent_soft = theme_ansi("accent_soft")
            muted = theme_ansi("text_muted")
            lines.append(
                f"  {accent}✦{ANSI_RESET} {muted}技能{ANSI_RESET}  "
                f"使用技能：{accent_soft}{event.target}{ANSI_RESET}\n"
            )
            return lines
        if event.ok is True and event.category in {"read", "search"}:
            lines: list[str] = []
            if self._pending is not None and self._pending.category != event.category:
                lines.extend(self.flush())
            if self._pending is None:
                self._pending = _PendingGroup(category=event.category)
            self._pending.count += 1
            self._pending.duration_ms += event.duration_ms or 0
            for key, value in event.metrics.items():
                self._pending.metrics[key] = self._pending.metrics.get(key, 0) + value
            return lines

        lines = self.flush()
        if event.category == "mutation" and event.tool_id in self._diff_tool_ids:
            self._diff_tool_ids.remove(event.tool_id)
            return lines
        lines.extend(self._success_lines(event))
        return lines

    def record_diff(
        self,
        tool_call_id: str,
        file_path: str,
        added: int,
        removed: int,
        *,
        diff_text: str = "",
    ) -> list[str]:
        """Emit the authoritative diff summary (plus compact preview for small edits)."""
        self._diff_tool_ids.add(str(tool_call_id))
        lines = self.flush()
        ok_c = theme_ansi("ok")
        err_c = theme_ansi("err")
        accent_soft = theme_ansi("accent_soft")
        muted = theme_ansi("text_muted")
        stat_bar = diff_stat_bar(added, removed)
        stat_suffix = f" {stat_bar}" if stat_bar else ""
        lines.append(
            f"  {ok_c}✓{ANSI_RESET} {muted}编辑{ANSI_RESET}  "
            f"已修改 {accent_soft}{file_path}{ANSI_RESET}"
            f" {muted}·{ANSI_RESET} {ok_c}+{added}{ANSI_RESET} / {err_c}-{removed}{ANSI_RESET} 行"
            f"{stat_suffix}\n"
        )
        if diff_text and 1 <= (added + removed) <= 10:
            lines.extend(_format_compact_diff_preview(diff_text, max_lines=6))
        return lines

    def flush(self) -> list[str]:
        group = self._pending
        self._pending = None
        if group is None:
            return []
        ok_c = theme_ansi("ok")
        muted = theme_ansi("text_muted")
        duration = _format_duration(group.duration_ms)
        badge = _CATEGORY_BADGES.get(group.category, "工具")
        if group.category == "read":
            line_count = group.metrics.get("line_count", 0)
            metric = f" {muted}· {line_count} 行{ANSI_RESET}" if line_count else ""
            text = f"已读取 {group.count} 个文件{metric} {muted}· {duration}{ANSI_RESET}"
        else:
            text = f"已完成 {group.count} 次搜索 {muted}· {duration}{ANSI_RESET}"
        return [f"  {ok_c}✓{ANSI_RESET} {muted}{badge}{ANSI_RESET}  {text}\n"]

    def active_status(self) -> ActiveToolStatus | None:
        if not self._active:
            return None
        active = list(self._active.values())
        events = [event for event, _ in active]
        earliest_started_at = min(started for _, started in active)
        categories = {event.category for event in events}
        if len(events) > 1 and categories == {"read"}:
            return ActiveToolStatus(
                label=f"正在并行读取 {len(events)} 个文件",
                detail="",
                count=len(events),
                started_at=earliest_started_at,
            )
        if len(events) > 1 and categories == {"search"}:
            return ActiveToolStatus(
                label=f"正在并行搜索 {len(events)} 项",
                detail="",
                count=len(events),
                started_at=earliest_started_at,
            )
        event, started_at = active[-1]
        return ActiveToolStatus(
            label=f"正在{event.action}",
            detail=event.command or event.target,
            count=len(events),
            started_at=started_at,
        )

    def active_statuses(self) -> list[ActiveToolStatus]:
        """返回当前所有在途工具活动（按开始时间顺序）。"""
        return [
            ActiveToolStatus(
                label=f"正在{event.action}",
                detail=event.command or event.target,
                count=1,
                started_at=started_at,
            )
            for event, started_at in self._active.values()
        ]

    def _standalone_lines(self, event: ToolDisplayEvent) -> list[str]:
        if event.ok is False:
            return self._failure_lines(event)
        return self._success_lines(event)

    @staticmethod
    def _success_lines(event: ToolDisplayEvent) -> list[str]:
        ok_c = theme_ansi("ok")
        muted = theme_ansi("text_muted")
        soft = theme_ansi("text_soft")
        accent_soft = theme_ansi("accent_soft")
        badge = _CATEGORY_BADGES.get(event.category, "工具")
        if event.category == "command":
            # D8：后台任务只表示"已启动"，不能显示"命令执行完成"误导用户
            summary = (
                "后台任务已启动（用 /tasks 管理）"
                if event.background
                else "命令执行完成"
            )
        else:
            summary = f"已{event.action}"
        target = (
            f" {accent_soft}{event.target}{ANSI_RESET}"
            if event.target and event.category != "command"
            else ""
        )
        duration = _format_duration(event.duration_ms or 0)
        lines = [
            f"  {ok_c}✓{ANSI_RESET} {muted}{badge}{ANSI_RESET}  "
            f"{summary}{target} {muted}· {duration}{ANSI_RESET}\n"
        ]
        if event.command:
            lines.append(f"    {muted}└─{ANSI_RESET} {soft}{event.command}{ANSI_RESET}\n")
        return lines

    @staticmethod
    def _failure_lines(event: ToolDisplayEvent) -> list[str]:
        err_c = theme_ansi("err")
        muted = theme_ansi("text_muted")
        soft = theme_ansi("text_soft")
        duration = _format_duration(event.duration_ms or 0)
        lines = [
            f"  {err_c}✗{ANSI_RESET} {err_c}失败{ANSI_RESET}  "
            f"{event.action}失败 {muted}· {duration}{ANSI_RESET}\n"
        ]
        if event.command:
            lines.append(f"    {muted}├─{ANSI_RESET} {soft}{event.command}{ANSI_RESET}\n")
        if event.error_summary:
            lines.append(f"    {muted}└─{ANSI_RESET} {err_c}{event.error_summary}{ANSI_RESET}\n")
        elif event.target and not event.command:
            lines.append(f"    {muted}└─{ANSI_RESET} {soft}{event.target}{ANSI_RESET}\n")
        return lines

    @staticmethod
    def _protocol_error_line(reason: str) -> str:
        err_c = theme_ansi("err")
        muted = theme_ansi("text_muted")
        return f"  {err_c}✗{ANSI_RESET} {err_c}异常{ANSI_RESET}  未知工具事件 {muted}· {reason}{ANSI_RESET}\n"


def _format_duration(duration_ms: int) -> str:
    return f"{duration_ms / 1000:.1f}s"
