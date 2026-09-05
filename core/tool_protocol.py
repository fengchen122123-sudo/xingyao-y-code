"""Contracts shared by tool registration, execution and rendering."""

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


ToolExecutor = Callable[..., "ToolResult"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    execute: ToolExecutor
    read_only: bool = False
    mutates_files: bool = False
    requires_permission: bool = True
    cancellable: bool = True
    parallel_safe: bool = False
    activity_description: str = ""

    def api_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    changed_files: list[str] = field(default_factory=list)
    diff: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # follow-up 私有消息载荷（设计 19.3）：仅固定 computer 适配器使用，
    # 默认空且不可变；现有工具不传该字段时行为与序列化完全不变。
    followup_messages: tuple[dict[str, Any], ...] = ()

    def to_message_content(self) -> str:
        """Return the compact content that belongs in model conversation history."""
        return str(self.content)
