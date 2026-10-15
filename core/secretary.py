"""秘书身份解析：当前会话生效的显示名 / 自称的唯一事实源。

背景：侧栏「秘书」分组下的会话是独立身份实体——每个会话各自起名（落库
`sessions.secretary_name`），此后该会话的显示与系统提示词身份全程使用这个名字，
且不复用设置页的人设名。

设计约束：
- 本模块只做"读 + 解析"，写入仍由 MemoryManager 承担；
- 容错优先：任何异常都回落 persona 名（或空串），绝不抛错——身份解析处在每轮
  请求与工具提示的高频路径上，不能因一次解析失败拖垮整轮；
- 会话名 → (来源, 名字) 做进程内缓存，避免高频路径每轮打 DB；创建 / 改名 / 删除
  会话的调用方负责调用 `invalidate_secretary_cache` 失效。
"""

from __future__ import annotations

import threading

SECRETARY_SOURCE = "SECRETARY"
# 历史遗留的未命名秘书会话兜底名（本次改动之前创建的秘书会话没有名字）
FALLBACK_SECRETARY_NAME = "秘书"

# 名字长度约束：strip 后 1..20 字符；非法值拒绝而非静默截断
_MAX_NAME_LEN = 20

# 进程内缓存：键 = 会话名，值 = (source, name)。会话名是稳定标识，缓存值随
# 创建 / 改名 / 删除失效，不设 TTL。
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[str, str]] = {}


def validate_secretary_name(name) -> str:
    """校验并返回去首尾空白后的秘书名；非法值抛 ValueError。

    空 / 纯空白、超长一律拒绝，不做静默截断——名字要落库并写进提示词，静默截断
    会让用户看到的与系统认到的不一致。
    """
    if not isinstance(name, str):
        raise ValueError("秘书名字必须是文本")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("秘书名字不能为空")
    if len(cleaned) > _MAX_NAME_LEN:
        raise ValueError(f"秘书名字过长（最多 {_MAX_NAME_LEN} 字符）")
    return cleaned


def _resolve_session_name(session_name):
    """统一会话名入口：显式传入优先，缺省取当前会话；取不到返回空串。"""
    if session_name:
        return str(session_name)
    try:
        from core import app_context

        return str(app_context.get_current_session() or "")
    except Exception:
        return ""


def _load_identity(session_name):
    """读取 (source, name)；无管理器 / 查询异常回落 ("", "")。"""
    try:
        from core import app_context

        manager = app_context.get_manager()
        if manager is None:
            return ("", "")
        source = manager.get_session_source(session_name) or ""
        name = manager.get_session_secretary_name(session_name) or ""
        return (str(source), str(name))
    except Exception:
        return ("", "")


def _cached_identity(session_name):
    """按会话名取 (source, name)，命中缓存则不打 DB。"""
    with _CACHE_LOCK:
        cached = _CACHE.get(session_name)
    if cached is not None:
        return cached
    identity = _load_identity(session_name)
    with _CACHE_LOCK:
        _CACHE[session_name] = identity
    return identity


def invalidate_secretary_cache(session_name=None) -> None:
    """失效会话身份缓存；不传会话名则清空全部。

    创建 / 改名 / 删除会话后必须调用，否则高频路径会读到旧名字。
    """
    with _CACHE_LOCK:
        if session_name is None:
            _CACHE.clear()
        else:
            _CACHE.pop(str(session_name), None)


def is_secretary_session(session_name=None) -> bool:
    """判断会话是否属于秘书分组；会话记录查不到时按普通会话处理（False）。"""
    name = _resolve_session_name(session_name)
    if not name:
        return False
    source, _ = _cached_identity(name)
    return source == SECRETARY_SOURCE


def get_secretary_name(session_name=None) -> str:
    """取会话的秘书名；非秘书会话或未命名返回空串。"""
    name = _resolve_session_name(session_name)
    if not name:
        return ""
    source, secretary_name = _cached_identity(name)
    if source != SECRETARY_SOURCE:
        return ""
    return secretary_name


def get_active_agent_name() -> str:
    """返回当前会话生效的 agent 自称。

    秘书会话 → 秘书名（未命名回落「秘书」）；其余 → 设置页的智能体显示名。
    任何异常都回落 persona 名，绝不抛错。
    """
    try:
        if is_secretary_session():
            return get_secretary_name() or FALLBACK_SECRETARY_NAME
    except Exception:
        pass
    try:
        from core import persona_settings

        return persona_settings.get_agent_display_name() or persona_settings.DEFAULT_AGENT_NAME
    except Exception:
        # persona 模块本身也不可用时的最后兜底：与设置页默认名一致
        return "星瑶"


# ---------------------------------------------------------------------------
# 工具策略（秘书调度台设计 2026-10-15 第 6/7 节）：schema 过滤与执行门控的
# 单一事实源。两侧必须共用本策略，避免"模型看得见却执行不了"或反之。
# ---------------------------------------------------------------------------

# 仅秘书会话可见的调度工具：普通会话工具集零变化（设计第 2/7 节）
SECRETARY_ONLY_TOOLS = frozenset({
    "search_conversations",
    "dispatch_task",
    "open_session",
})

# 秘书会话的额外允许项：这些工具不是只读（network / system / 跨会话写），
# 但属于秘书的核心职责或无害的公开信息查询。其余允许项一律由
# ``side_effect_scope == "none"`` 自动覆盖。
SECRETARY_ALLOWED_TOOLS = SECRETARY_ONLY_TOOLS | frozenset({
    "get_weather",
    "search_news",
    "fetch_url",
})


def _plugin_tool_names() -> frozenset:
    """已加载插件提供的工具名集合；取不到时返回空集（不抛错）。

    插件工具的 side_effect_scope 常依赖默认值（none），仅凭作用域会把插件
    工具误放行；秘书会话只认内置工具（设计第 7 节），这里显式排除。
    """
    try:
        from core import plugin_loader

        names = set()
        for info in plugin_loader.get_plugin_infos() or ():
            if not isinstance(info, dict):
                continue
            for tool_name in info.get("tools") or ():
                names.add(str(tool_name))
        return frozenset(names)
    except Exception:
        return frozenset()


def secretary_allows_tool(tool_name: str, side_effect_scope: str = "") -> bool:
    """秘书会话是否允许该工具（策略单一事实源）。

    允许 = ``side_effect_scope == "none"``（只读类）或名字在
    ``SECRETARY_ALLOWED_TOOLS`` 里；其余（写文件 / 执行命令 / 电脑操控 /
    子代理 / 计划写 / 生图 / MCP / 插件）一律拒绝。空名字拒绝。
    """
    name = str(tool_name or "").strip()
    if not name:
        return False
    if name in _plugin_tool_names():
        return False
    if str(side_effect_scope or "").strip() == "none":
        return True
    return name in SECRETARY_ALLOWED_TOOLS


def is_secretary_tool_blocked(
    tool_name: str, side_effect_scope: str = "", session_name=None
) -> bool:
    """秘书会话里该工具是否被只读门控拒绝（工具执行前的统一判定）。

    ``session_name`` 缺省用当前会话解析；显式传入时以传入值为准（回合执行
    线程用 parent_session，避免用户切换视图导致误判）。身份解析异常按普通
    会话处理（False）——门控组件故障不得阻断正常会话。
    """
    try:
        if not is_secretary_session(session_name):
            return False
    except Exception:
        return False
    return not secretary_allows_tool(tool_name, side_effect_scope)


def build_secretary_name_block(name) -> str:
    """构建注入系统提示词的秘书名字块；名字为空时返回空串（不注入）。

    秘书名字块是"当前会话专属身份"的显式声明，需说明它与设置页人设无关，
    以免模型把两套身份混用。
    """
    try:
        cleaned = validate_secretary_name(name)
    except ValueError:
        cleaned = str(name or "").strip()
    if not cleaned:
        return ""
    return (
        "# 秘书身份\n\n"
        f"你是「{cleaned}」，用户的私人秘书。这个名字是本会话专属的，"
        "与设置页的智能体人设无关；不主动引用或提及设置页的智能体名称。"
    )
