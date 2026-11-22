from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from core.context_builder import RuntimeContext


class Persistence(str, Enum):

    PREFIX = "prefix"          # 稳定前缀：每请求重注入，必须字节稳定（计入缓存前缀）
    PERSISTED = "persisted"
    PER_REQUEST = "per_request"


class Channel(str, Enum):

    SYSTEM = "system"
    USER_REMINDER = "user_reminder"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class InjectionSpec:

    source: str
    persistence: Persistence
    channel: Channel
    section_builder: Callable[[], str]


class ContextInjectionRegistry:

    def __init__(self) -> None:
        self._specs: dict[str, InjectionSpec] = {}

    def register(self, spec: InjectionSpec) -> None:
        if not spec.source:
            raise ValueError("injection source must not be empty")
        if spec.source in self._specs:
            raise ValueError(f"duplicate injection source: {spec.source}")
        self._specs[spec.source] = spec

    def specs(self) -> tuple[InjectionSpec, ...]:
        """按登记顺序返回全部描述符（只读快照，供测试 / 审计 / 后续管线遍历）。"""
        return tuple(self._specs.values())

    def build_prefix_sections(self) -> list[str]:
        """构建所有 PREFIX + SYSTEM 的注入（进稳定前缀）。"""
        return self._collect(Persistence.PREFIX, Channel.SYSTEM)

    def build_persisted_sections(self) -> list[str]:
        """构建所有 PERSISTED + SYSTEM 的注入（进动态块）。"""
        return self._collect(Persistence.PERSISTED, Channel.SYSTEM)

    def build_per_request_reminders(self) -> list[str]:
        """构建所有 PER_REQUEST + USER_REMINDER 的注入（内联 user 消息）。"""
        return self._collect(Persistence.PER_REQUEST, Channel.USER_REMINDER)

    def persisted_sources(self) -> frozenset[str]:
        return frozenset(
            spec.source
            for spec in self._specs.values()
            if spec.persistence == Persistence.PERSISTED
        )

    def _collect(self, persistence: Persistence, channel: Channel) -> list[str]:
        sections: list[str] = []
        for spec in self._specs.values():
            if spec.persistence != persistence or spec.channel != channel:
                continue
            text = spec.section_builder()
            if text:
                sections.append(text)
        return sections


def _unbound_section_builder() -> str:
    return ""


def _build_context_prefix(is_gui: bool, session_name: str | None = None) -> str:
    from core.agent_service import load_system_prompt
    from core.context_builder import build_stable_prompt

    if session_name:
        try:
            from core.secretary import is_secretary_session

            if is_secretary_session(session_name):
                return (load_system_prompt(is_gui, session_name) or "").strip()
        except Exception:
            # 身份解析异常按普通会话处理：前缀字节与改动前一致，绝不打断组装
            pass
    return build_stable_prompt(load_system_prompt(is_gui))


def _build_stable_system_sections(session_name=None) -> str:
    from core.agent_service import build_stable_system_extra_sections

    return "\n\n".join(build_stable_system_extra_sections(session_name))


def _runtime_block_builder(
    runtime_provider: Callable[[], "RuntimeContext"] | None,
) -> Callable[[], str]:
    if runtime_provider is None:
        return _unbound_section_builder

    def builder() -> str:
        from core.context_builder import _build_runtime_context_block

        return _build_runtime_context_block(runtime_provider())

    return builder


def _time_note_builder(
    runtime_provider: Callable[[], "RuntimeContext"] | None,
) -> Callable[[], str]:
    """注入点 4：时间注记（PER_REQUEST/USER_REMINDER）→ ``build_time_note``。

    每轮必变，内联到最后一条 user 消息，不产生新的 system 消息
    （规格 02 §2.4：独立 system 消息会使 system_sha256 每轮变化，破坏前缀缓存）。
    """
    if runtime_provider is None:
        return _unbound_section_builder

    def builder() -> str:
        from core.context_builder import build_time_note

        return build_time_note(runtime_provider())

    return builder


def _computer_section_builder(
    runtime_provider: Callable[[], "RuntimeContext"] | None,
) -> Callable[[], str]:
    if runtime_provider is None:
        return _unbound_section_builder

    def builder() -> str:
        from core.context_builder import _build_computer_prompt_section

        return _build_computer_prompt_section(runtime_provider().computer_capability)

    return builder


def _workspace_corrections_builder(
    runtime_provider: Callable[[], "RuntimeContext"] | None,
) -> Callable[[], str]:
    if runtime_provider is None:
        return _unbound_section_builder

    def builder() -> str:
        return getattr(runtime_provider(), "workspace_corrections", "") or ""

    return builder


def _build_file_reference() -> str:
    return ""


SOURCE_CONTEXT_PREFIX = "context_prefix"
SOURCE_STABLE_SYSTEM_SECTIONS = "stable_system_sections"
SOURCE_RUNTIME_CONTEXT_BLOCK = "runtime_context_block"
SOURCE_TIME_NOTE = "time_note"
SOURCE_FILE_REFERENCE = "file_reference"
SOURCE_COMPUTER_CONTROL_SECTION = "computer_control_section"
SOURCE_WORKSPACE_CORRECTIONS = "workspace_corrections"

INJECTION_SOURCES: tuple[str, ...] = (
    SOURCE_CONTEXT_PREFIX,
    SOURCE_STABLE_SYSTEM_SECTIONS,
    SOURCE_RUNTIME_CONTEXT_BLOCK,
    SOURCE_TIME_NOTE,
    SOURCE_FILE_REFERENCE,
    SOURCE_COMPUTER_CONTROL_SECTION,
    SOURCE_WORKSPACE_CORRECTIONS,
)


def build_default_injection_registry(
    *,
    is_gui: bool = False,
    runtime_provider: Callable[[], "RuntimeContext"] | None = None,
    session_name: str | None = None,
) -> ContextInjectionRegistry:
    registry = ContextInjectionRegistry()

    registry.register(InjectionSpec(
        source=SOURCE_CONTEXT_PREFIX,
        persistence=Persistence.PREFIX,
        channel=Channel.SYSTEM,
        section_builder=lambda: _build_context_prefix(is_gui, session_name),
    ))

    registry.register(InjectionSpec(
        source=SOURCE_STABLE_SYSTEM_SECTIONS,
        persistence=Persistence.PREFIX,
        channel=Channel.SYSTEM,
        section_builder=lambda: _build_stable_system_sections(session_name),
    ))

    registry.register(InjectionSpec(
        source=SOURCE_RUNTIME_CONTEXT_BLOCK,
        persistence=Persistence.PERSISTED,
        channel=Channel.SYSTEM,
        section_builder=_runtime_block_builder(runtime_provider),
    ))

    registry.register(InjectionSpec(
        source=SOURCE_TIME_NOTE,
        persistence=Persistence.PER_REQUEST,
        channel=Channel.USER_REMINDER,
        section_builder=_time_note_builder(runtime_provider),
    ))

    registry.register(InjectionSpec(
        source=SOURCE_FILE_REFERENCE,
        persistence=Persistence.PER_REQUEST,
        channel=Channel.USER_REMINDER,
        section_builder=_build_file_reference,
    ))

    registry.register(InjectionSpec(
        source=SOURCE_COMPUTER_CONTROL_SECTION,
        persistence=Persistence.PERSISTED,
        channel=Channel.SYSTEM,
        section_builder=_computer_section_builder(runtime_provider),
    ))

    registry.register(InjectionSpec(
        source=SOURCE_WORKSPACE_CORRECTIONS,
        persistence=Persistence.PERSISTED,
        channel=Channel.SYSTEM,
        section_builder=_workspace_corrections_builder(runtime_provider),
    ))

    return registry
