"""上下文注入描述符与注册表（面向 ZCode 能力借鉴 · 规格 02 · 阶段 B1-B6）。

把星瑶现有的全部注入点统一登记为 ``InjectionSpec``（source × persistence ×
channel 三个字段，规格 02 §2.1），并为「渲染管线接管 / 压缩按类保留 / Hooks
统一落点」提供一张可查询的清单 + 组装入口。

设计边界（规格 02 §3.1「包装而非重写」）：

- B1-B3：只登记不渲染，零字节变化；
- B4（本文件当前状态）：渲染管线接管——``agent_service.build_stable_system_message``
  遍历 ``build_prefix_sections()``、``context_builder.build_dynamic_prompt`` 遍历
  ``build_persisted_sections()``、``agent_service`` 的时间注记取用
  ``build_per_request_reminders()``；三处均在改造前字节输出上做等价拆分，
  由 sha256 golden 与 tests/test_context_injection.py 的逐点对比守护；
- ``section_builder`` 一律包装现有函数（无参闭包 / lambda），绝不复制其文本
  逻辑——避免未来出现"两份实现"的漂移；
- 为什么需要 provider 参数：运行上下文块 / 时间注记 / 电脑操控说明 / 按回合
  纠正都依赖每轮 ``RuntimeContext``，而注册表刻意不持有会话状态；调用方通过
  ``build_default_injection_registry`` 的 provider 参数注入状态来源，未绑定时
  这些 source 的 builder 返回空串（规格 02 §2.1：空串表示不注入）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # 仅供类型标注：运行时零导入，避免任何导入环
    from core.context_builder import RuntimeContext


class Persistence(str, Enum):
    """这条注入在会话历史中的存活策略（规格 02 §2.1）。"""

    PREFIX = "prefix"          # 稳定前缀：每请求重注入，必须字节稳定（计入缓存前缀）
    PERSISTED = "persisted"    # 持久化：进历史，压缩时保留
    PER_REQUEST = "per_request"  # 仅当前请求：不落历史


class Channel(str, Enum):
    """这条注入的投递形态（规格 02 §2.1）。"""

    SYSTEM = "system"                # 进 system prompt
    USER_REMINDER = "user_reminder"  # 作为 user 消息内联
    TOOL_RESULT = "tool_result"      # 内联进工具结果


@dataclass(frozen=True)
class InjectionSpec:
    """单条注入点描述符。

    为什么只有两个分类维度（persistence × channel）：规格 02 §1.4/§6 明确
    不照搬 ZCode 的 27 种 source 与 provider 可见性维度，星瑶 GUI+CLI 无多端
    渲染差异，维度越少越不容易成为新的复杂度源。
    """

    source: str                # 来源标识，如 "context_prefix" / "time_note"
    persistence: Persistence
    channel: Channel
    section_builder: Callable[[], str]  # 返回注入文本；空串表示本次不注入


class ContextInjectionRegistry:
    """注入点注册表（规格 02 §2.5 接口）。

    只做登记与查询。B4 起三个组装入口（稳定前缀 / 动态块 / 每轮提醒）已由渲染
    管线接管：查询方法的返回值就是各通道的最终拼接片段，因此保持"按登记顺序
    返回全部非空片段"的语义不变。
    """

    def __init__(self) -> None:
        self._specs: dict[str, InjectionSpec] = {}

    def register(self, spec: InjectionSpec) -> None:
        """注册一个注入点；重复 source 抛 ValueError（规格 02 §2.5）。

        为什么重复 source 必须抛错：source 是压缩保留（persisted_sources）
        与未来 Hooks 落点的唯一键，静默覆盖会让"清单"与实际注入点脱节。
        """
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
        """返回所有 PERSISTED 类注入的 source 名，供压缩保留逻辑使用。

        只按 persistence 维度筛选（不限定 channel）：规格 02 §2.6 指出星瑶的
        PERSISTED 注入基本合并进单条动态 system 消息，压缩保留的粒度是"这条
        消息"，未来语义为"历史事实"的 PERSISTED + USER_REMINDER（如 todo 提醒）
        同样应被保留。
        """
        return frozenset(
            spec.source
            for spec in self._specs.values()
            if spec.persistence == Persistence.PERSISTED
        )

    def _collect(self, persistence: Persistence, channel: Channel) -> list[str]:
        """按 persistence + channel 收集注入文本；空串 = 不注入（规格 02 §2.1）。"""
        sections: list[str] = []
        for spec in self._specs.values():
            if spec.persistence != persistence or spec.channel != channel:
                continue
            text = spec.section_builder()
            if text:
                sections.append(text)
        return sections


# ---------------------------------------------------------------------------
# section_builder：一律包装现有函数，不复制文本逻辑
# ---------------------------------------------------------------------------

def _unbound_section_builder() -> str:
    """未绑定运行时状态的 builder：返回空串（= 不注入）。

    为什么返回空串而不是抛错：注册表在"只登记不渲染"的 B1-B3 阶段就存在，
    当初的任何查询时机都不应失败；渲染接管（B4）后，未绑定 provider 仍等价于
    "该注入本轮不存在"（规格 02 §2.1：空串表示不注入），由调用方通过
    ``build_default_injection_registry(runtime_provider=...)`` 完成状态绑定。
    """
    return ""


def _build_context_prefix(is_gui: bool) -> str:
    """注入点 1：base_prompt + 决策规则（PREFIX/SYSTEM）。

    为什么包一层：现有 ``build_stable_prompt(base_prompt)`` 需要外部提供
    base_prompt，而 ``InjectionSpec.section_builder`` 约定无参；这里按端读取
    随包系统提示词后直接调用现有函数，包装层不产生任何新文本。
    """
    from core.agent_service import load_system_prompt
    from core.context_builder import build_stable_prompt

    return build_stable_prompt(load_system_prompt(is_gui))


def _build_stable_system_sections(session_name=None) -> str:
    """注入点 2：通用编码规范 / 长期记忆 / 技能目录 / 子 Agent 清单段（PREFIX/SYSTEM）。

    包装 ``agent_service.build_stable_system_extra_sections``（B4 从
    ``build_stable_system_message`` 里拆出的「build_stable_prompt 之外」部分），
    按原有条件与顺序以 "\\n\\n" 连接。

    为什么必须拆分（规格 02 §3.2 步骤 2）：注入点 1（base_prompt + 决策规则）与
    注入点 2 是两条 PREFIX/SYSTEM source，渲染管线按 source 取值再拼；若本段仍返回
    ``build_stable_system_message`` 的完整 content（内含注入点 1），前缀会重复拼接
    一整份 base_prompt。拆分后两段各自完整，且分段点落在原有 "\\n\\n" 连接符上，
    join 结合律保证 ``build_stable_system_message`` 的输出逐字节不变。
    本段与 GUI/CLI 无关，故不再接收 is_gui（改造前该参数只用于读取 base_prompt）。

    ``session_name`` 透传给 ``build_stable_system_extra_sections``，用于会话级
    快照（规格见 agent_service 同名字段）；为空时走无快照的原始渲染路径。
    """
    from core.agent_service import build_stable_system_extra_sections

    return "\n\n".join(build_stable_system_extra_sections(session_name))


def _runtime_block_builder(
    runtime_provider: Callable[[], "RuntimeContext"] | None,
) -> Callable[[], str]:
    """注入点 3：运行上下文基础块（PERSISTED/SYSTEM）。

    B4 起指向 ``context_builder._build_runtime_context_block``——build_dynamic_prompt
    拆出的"基础块"（不含电脑操控说明与按回合纠正，二者是独立的注入点 6/7）。
    为什么不能指向 build_dynamic_prompt：那会让 PERSISTED/SYSTEM 通道出现
    "全量动态块 + 电脑操控 + 纠正"三项重复拼接，且注册表 builder → build_dynamic_prompt
    → 注册表 形成无限递归。
    """
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
    """注入点 6：电脑操控说明（PERSISTED/SYSTEM）→ ``_build_computer_prompt_section``。

    capability 关闭时现有函数本身返回空串（不注入），因此未绑定 provider 时
    与"能力关闭"在字节上等价，同样安全。
    """
    if runtime_provider is None:
        return _unbound_section_builder

    def builder() -> str:
        from core.context_builder import _build_computer_prompt_section

        return _build_computer_prompt_section(runtime_provider().computer_capability)

    return builder


def _workspace_corrections_builder(
    runtime_provider: Callable[[], "RuntimeContext"] | None,
) -> Callable[[], str]:
    """注入点 7：按回合撤销纠正（PERSISTED/SYSTEM）。

    现有实现没有独立函数：改造前该文本由 ``build_dynamic_prompt`` 内联追加，
    来源是 ``RuntimeContext.workspace_corrections`` 字段。此处按字段直读包装
    （与 build_dynamic_prompt 改造前的取值写法 `getattr(...) or ""` 完全一致），
    B4 起由渲染管线把本段拼在动态块末尾。
    """
    if runtime_provider is None:
        return _unbound_section_builder

    def builder() -> str:
        return getattr(runtime_provider(), "workspace_corrections", "") or ""

    return builder


def _build_file_reference() -> str:
    """注入点 5：@ 文件引用（PER_REQUEST/USER_REMINDER）——解析期内联，无独立函数。

    现有实现中，@ 引用的注入文本由 ``input_parser.parse_user_input`` 直接拼进
    user 消息（text_content 的 "[附带上下文: ...]" / "[附带图片文件: ...]" 块），
    不存在可独立指向的无参函数；按规格 02 §2.2 的精神登记 source 与分类，
    文本构建留待需要接管时再从 parse_user_input 提取（B4 只接管时间注记：
    PER_REQUEST/USER_REMINDER 通道里本 source 返回空串被过滤，不参与拼接，
    因此时间注记的拼接结果与改造前逐字节一致）。
    """
    return ""


# ---------------------------------------------------------------------------
# 默认登记表：规格 02 §2.2 归分表的 7 个现有注入点
# ---------------------------------------------------------------------------

SOURCE_CONTEXT_PREFIX = "context_prefix"
SOURCE_STABLE_SYSTEM_SECTIONS = "stable_system_sections"
SOURCE_RUNTIME_CONTEXT_BLOCK = "runtime_context_block"
SOURCE_TIME_NOTE = "time_note"
SOURCE_FILE_REFERENCE = "file_reference"
SOURCE_COMPUTER_CONTROL_SECTION = "computer_control_section"
SOURCE_WORKSPACE_CORRECTIONS = "workspace_corrections"

# 登记顺序 = 规格 02 §2.2 表格顺序，供测试与审计枚举比对。
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
    """登记规格 02 §2.2 的 7 个现有注入点，返回全新注册表。

    为什么每次返回新实例：避免模块级可变全局状态——注册表被多处共享时，
    "重复 source 抛错"与测试隔离都会变得不可靠（规格 02 §5：注册表不得
    成为新的复杂度源）。

    :param is_gui: 稳定前缀（注入点 1）读取哪份随包系统提示词（GUI/CLI 分离）；
        注入点 2 与端口径无关，不使用该参数。
    :param runtime_provider: 注入点 3/4/6/7 的每轮 RuntimeContext 来源；
        未传时这些 source 的 builder 返回空串（不注入），由调用方
        （B4 起的渲染管线：build_stable_system_message / build_dynamic_prompt /
        agent_service 的时间注记取用点）完成状态绑定。
    :param session_name: 注入点 2 的会话级快照键；为 None 时不使用快照
        （Token 估算、测试等无会话上下文场景），渲染字节与改造前一致。
    """
    registry = ContextInjectionRegistry()

    # 1) base_prompt + 决策规则 → PREFIX/SYSTEM（已计入缓存前缀，规格 02 §2.3 准入极严）
    registry.register(InjectionSpec(
        source=SOURCE_CONTEXT_PREFIX,
        persistence=Persistence.PREFIX,
        channel=Channel.SYSTEM,
        section_builder=lambda: _build_context_prefix(is_gui),
    ))

    # 2) 通用编码规范 / 长期记忆 / 技能目录 / 子 Agent 清单 → PREFIX/SYSTEM
    #    （跨轮基本不变；B4 起与注入点 1 拆开各自独立取值，见 _build_stable_system_sections）
    registry.register(InjectionSpec(
        source=SOURCE_STABLE_SYSTEM_SECTIONS,
        persistence=Persistence.PREFIX,
        channel=Channel.SYSTEM,
        section_builder=lambda: _build_stable_system_sections(session_name),
    ))

    # 3) 运行上下文块（模型/权限/目录/计划）→ PERSISTED/SYSTEM
    #    （内容不变时不重复注入，本身已是"半持久化"，规格 02 §2.2）
    registry.register(InjectionSpec(
        source=SOURCE_RUNTIME_CONTEXT_BLOCK,
        persistence=Persistence.PERSISTED,
        channel=Channel.SYSTEM,
        section_builder=_runtime_block_builder(runtime_provider),
    ))

    # 4) 时间注记 → PER_REQUEST/USER_REMINDER（每轮必变，内联 user 消息）
    registry.register(InjectionSpec(
        source=SOURCE_TIME_NOTE,
        persistence=Persistence.PER_REQUEST,
        channel=Channel.USER_REMINDER,
        section_builder=_time_note_builder(runtime_provider),
    ))

    # 5) @ 文件引用 → PER_REQUEST/USER_REMINDER（单次引用，解析期内联）
    registry.register(InjectionSpec(
        source=SOURCE_FILE_REFERENCE,
        persistence=Persistence.PER_REQUEST,
        channel=Channel.USER_REMINDER,
        section_builder=_build_file_reference,
    ))

    # 6) 电脑操控说明 → PERSISTED/SYSTEM（跟随动态块）
    registry.register(InjectionSpec(
        source=SOURCE_COMPUTER_CONTROL_SECTION,
        persistence=Persistence.PERSISTED,
        channel=Channel.SYSTEM,
        section_builder=_computer_section_builder(runtime_provider),
    ))

    # 7) 按回合撤销纠正 → PERSISTED/SYSTEM（跟随动态块，字段直读包装）
    registry.register(InjectionSpec(
        source=SOURCE_WORKSPACE_CORRECTIONS,
        persistence=Persistence.PERSISTED,
        channel=Channel.SYSTEM,
        section_builder=_workspace_corrections_builder(runtime_provider),
    ))

    return registry
