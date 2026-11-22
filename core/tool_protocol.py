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

    depends_on: tuple[int, ...] | None = None

    # 副作用范围，供调度与权限系统读取。
    # 合法值：none | workspace | git | network | system
    # 为什么默认 "none"：v1 不参与调度决策，仅作为契约元数据（为二期拓扑重排
    # 与权限补充信号铺路）；默认值保证插件/测试等未声明的构造维持旧语义。
    side_effect_scope: str = "none"

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
        return str(self.content)
