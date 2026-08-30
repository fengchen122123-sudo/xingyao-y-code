"""Pure state machine for compact CLI tool-progress output."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import time
from types import MappingProxyType
from typing import Callable, Mapping

from core.ui_markers import ANSI_CYAN, ANSI_GREEN, ANSI_RED, ANSI_RESET


_DISPLAY_CATEGORIES = frozenset(
    {"read", "search", "command", "mutation", "skill", "subagent", "other"}
)


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
        # Moving a replacement to the end preserves the latest active action.
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
            lines.append(f"{ANSI_CYAN}✦{ANSI_RESET} 使用技能：{event.target}\n")
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

    def record_diff(self, tool_call_id: str, file_path: str, added: int, removed: int) -> list[str]:
        """Emit the authoritative diff summary for one mutation tool call."""
        self._diff_tool_ids.add(str(tool_call_id))
        lines = self.flush()
        lines.append(
            f"{ANSI_GREEN}✓{ANSI_RESET} 已修改 {ANSI_CYAN}{file_path}{ANSI_RESET}"
            f" · {ANSI_GREEN}+{added}{ANSI_RESET} / {ANSI_RED}-{removed}{ANSI_RESET} 行\n"
        )
        return lines

    def flush(self) -> list[str]:
        group = self._pending
        self._pending = None
        if group is None:
            return []
        duration = _format_duration(group.duration_ms)
        if group.category == "read":
            line_count = group.metrics.get("line_count", 0)
            metric = f" · {line_count} 行" if line_count else ""
            text = f"已读取 {group.count} 个文件{metric} · {duration}"
        else:
            text = f"已完成 {group.count} 次搜索 · {duration}"
        return [f"{ANSI_GREEN}✓{ANSI_RESET} {text}\n"]

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

    def _standalone_lines(self, event: ToolDisplayEvent) -> list[str]:
        if event.ok is False:
            return self._failure_lines(event)
        return self._success_lines(event)

    @staticmethod
    def _success_lines(event: ToolDisplayEvent) -> list[str]:
        if event.category == "command":
            # D8：后台任务只表示"已启动"，不能显示"命令执行完成"误导用户
            summary = (
                "后台任务已启动（用 /tasks 管理）"
                if event.background
                else "命令执行完成"
            )
        else:
            summary = f"已{event.action}"
        target = f" {event.target}" if event.target and event.category != "command" else ""
        lines = [
            f"{ANSI_GREEN}✓{ANSI_RESET} {summary}{target} · {_format_duration(event.duration_ms or 0)}\n"
        ]
        if event.command:
            lines.append(f"  └─ {event.command}\n")
        return lines

    @staticmethod
    def _failure_lines(event: ToolDisplayEvent) -> list[str]:
        lines = [
            f"{ANSI_RED}✗{ANSI_RESET} {event.action}失败 · {_format_duration(event.duration_ms or 0)}\n"
        ]
        if event.command:
            lines.append(f"  ├─ {event.command}\n")
        if event.error_summary:
            lines.append(f"  └─ {event.error_summary}\n")
        elif event.target and not event.command:
            lines.append(f"  └─ {event.target}\n")
        return lines

    @staticmethod
    def _protocol_error_line(reason: str) -> str:
        return f"{ANSI_RED}✗{ANSI_RESET} 未知工具事件 · {reason}\n"


def _format_duration(duration_ms: int) -> str:
    return f"{duration_ms / 1000:.1f}s"
