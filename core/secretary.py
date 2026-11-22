from __future__ import annotations

import os
import threading

SECRETARY_SOURCE = "SECRETARY"
# 历史遗留的未命名秘书会话兜底名（本次改动之前创建的秘书会话没有名字）
FALLBACK_SECRETARY_NAME = "秘书"

_MAX_NAME_LEN = 20
# 人设长度上限：与设置页人设（persona_settings.MAX_PERSONA_LEN）同口径，
# 避免两套"人设"对同一类文本给出不同长度限制
MAX_PERSONA_LEN = 20000

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[str, str, str]] = {}


def validate_secretary_name(name) -> str:
    if not isinstance(name, str):
        raise ValueError("秘书名字必须是文本")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("秘书名字不能为空")
    if len(cleaned) > _MAX_NAME_LEN:
        raise ValueError(f"秘书名字过长（最多 {_MAX_NAME_LEN} 字符）")
    return cleaned


def validate_secretary_persona(persona) -> str:
    if persona is None:
        return ""
    if not isinstance(persona, str):
        raise ValueError("秘书人设必须是文本")
    cleaned = persona.strip()
    if len(cleaned) > MAX_PERSONA_LEN:
        raise ValueError(f"秘书人设过长（最多 {MAX_PERSONA_LEN} 字符）")
    return cleaned


def _resolve_session_name(session_name):
    if session_name:
        return str(session_name)
    try:
        from core import app_context

        thread_getter = getattr(app_context, "get_thread_session", None)
        if thread_getter is not None:
            try:
                thread_session = str(thread_getter() or "")
            except Exception:
                thread_session = ""
            if thread_session:
                return thread_session
        return str(app_context.get_current_session() or "")
    except Exception:
        return ""


def _load_identity(session_name):
    try:
        from core import app_context

        manager = app_context.get_manager()
        if manager is None:
            return ("", "", "")
        source = manager.get_session_source(session_name) or ""
        name = manager.get_session_secretary_name(session_name) or ""
    except Exception:
        return ("", "", "")
    persona = ""
    try:
        persona = manager.get_session_secretary_persona(session_name) or ""
    except Exception:
        persona = ""
    return (str(source), str(name), str(persona))


def _cached_identity(session_name):
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
    source, _, _ = _cached_identity(name)
    return source == SECRETARY_SOURCE


def get_secretary_name(session_name=None) -> str:
    """取会话的秘书名；非秘书会话或未命名返回空串。"""
    name = _resolve_session_name(session_name)
    if not name:
        return ""
    source, secretary_name, _ = _cached_identity(name)
    if source != SECRETARY_SOURCE:
        return ""
    return secretary_name


def get_secretary_persona(session_name=None) -> str:
    """取会话的秘书专属人设；非秘书会话或未设置返回空串（= 用内置默认人设）。

    人设与设置页的星瑶人设是两套东西：这里只读会话级 `secretary_persona`，
    绝不回落到 persona_settings，否则"没填人设"的秘书会莫名其妙继承星瑶。
    """
    name = _resolve_session_name(session_name)
    if not name:
        return ""
    source, _, persona = _cached_identity(name)
    if source != SECRETARY_SOURCE:
        return ""
    return persona


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

SECRETARY_ONLY_TOOLS = frozenset({
    "search_conversations",
    "dispatch_task",
    # 批量派活（升级设计 2026-11-04 第 6.1 节）：一次派出多条并可声明批次内
    # 依赖，无依赖项并行、有依赖项等上游完成后自动起。走同一条回合通道，
    # 秘书仍无任何项目写权限。
    "dispatch_batch",
    "open_session",
    "create_project",
    # 派活台账只读查询（升级设计 2026-11-04 第 3.4 节）：秘书据此回答
    # 「刚才那个任务怎么样」，普通会话工具集零变化。
    "list_dispatches",
    "get_dispatch_result",
    # 跨项目与历史会话分析（只读浏览 + 按偏移分页读会话消息 + 个人记忆管理）：
    # 仅秘书会话可见，普通会话工具集零变化。
    "list_projects",
    "list_sessions",
    "read_session",
    "manage_memory",
    # 项目画像（升级设计 2026-11-04 第 5.2 节）：read_project_profile 是只读
    # （scope=none，本可自动放行，列出来是为了普通会话看不到它）；
    # remember_decision 是 workspace 作用域，必须显式放行才不被只读门控拦下
    # ——它唯一的写目标是 ~/.ycode/memory/projects/，路径越界由
    # core.project_profile 直接拒绝，秘书仍无项目目录写权限。
    # 2026-11-07（特性 1）：read_project_profile / remember_decision 已对普通
    # 会话开放（项目状态查看与编辑入口），故从本集合移除；remember_decision
    # 落在记忆目录（跨项目、非工作区），作用域已改 system，必须在
    # SECRETARY_ALLOWED_TOOLS 里显式放行，否则秘书反而用不了。
})

# 即使 side_effect_scope == "none"，也必须从秘书会话屏蔽的编码/内部调度工具：
# 秘书不能操作工作流状态机、子代理任务监控、代码计划状态机或 MCP 连接管理。
SECRETARY_BLOCKED_TOOLS = frozenset({
    "workflow_finish",
    "manage_task",
    "plan_read",
    "mcp_list_servers",
})

# 允许秘书使用的纯只读插件工具（其余插件工具仍一律排除）
SECRETARY_ALLOWED_PLUGIN_TOOLS = frozenset({
    "office_read",
})

# 秘书可在「已登记项目工作区」内免沙盒越界确认直接只读访问的工具集合
SECRETARY_READONLY_WORKSPACE_TOOLS = frozenset({
    "read_file",
    "list_directory",
    "directory_tree",
    "search_code",
    "find_definition",
    "get_outline",
    "find_references",
    "glob_files",
    "view_file_outline",
    "office_read",
})

# 秘书会话的额外允许项：这些工具不是只读（network / system / 跨会话写），
# 但属于秘书的核心职责或无害的公开信息查询。其余允许项一律由
# ``side_effect_scope == "none"`` 自动覆盖。
SECRETARY_ALLOWED_TOOLS = SECRETARY_ONLY_TOOLS | SECRETARY_ALLOWED_PLUGIN_TOOLS | frozenset({
    "get_weather",
    "search_news",
    "fetch_url",
    # 项目画像写入：作用域 system（只写 ~/.ycode/memory/projects/，跨项目），
    # 必须按名字显式放行；普通会话同样可见（特性 1）。
    "remember_decision",
    "automation_create",
    "automation_update",
    "automation_delete",
    "automation_run_now",
})


def _plugin_tool_names() -> frozenset:
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
    name = str(tool_name or "").strip()
    if not name:
        return False
    if name in SECRETARY_BLOCKED_TOOLS:
        return False
    if name in SECRETARY_ALLOWED_PLUGIN_TOOLS:
        return True
    if name in _plugin_tool_names():
        return False
    if str(side_effect_scope or "").strip() == "none":
        return True
    return name in SECRETARY_ALLOWED_TOOLS


def _get_registered_workspace_roots() -> list[str]:
    """收集所有已登记且非根目录的项目工作区路径（用于秘书跨项目只读免确认）。"""
    roots: list[str] = []
    seen: set[str] = set()
    try:
        from core import app_context

        manager = app_context.get_manager()
        if manager is None:
            return []
        candidates: list[str] = []
        get_ws = getattr(manager, "get_all_workspaces", None)
        if callable(get_ws):
            candidates.extend(get_ws() or ())
        get_sess = getattr(manager, "get_all_sessions", None)
        if callable(get_sess):
            for item in get_sess(include_empty=True, include_archived=False) or ():
                if isinstance(item, dict) and item.get("workdir"):
                    candidates.append(str(item["workdir"]))
    except Exception:
        return []

    for raw in candidates:
        text = str(raw or "").strip()
        if not text or text.upper() == "GLOBAL":
            continue
        try:
            resolved = os.path.realpath(os.path.expanduser(text))
            if os.name == "nt" and resolved.startswith("\\\\?\\"):
                resolved = resolved[4:]
        except OSError:
            continue
        # 拒绝驱动器根目录（如 '/' 或 'C:\\'），防止整盘被当成工作区放行
        if not resolved or os.path.dirname(resolved) == resolved:
            continue
        norm = os.path.normcase(resolved)
        if norm in seen:
            continue
        seen.add(norm)
        roots.append(resolved)
    return roots


def is_secretary_registered_workspace_read(
    tool_name: str,
    path_value: str,
    session_name=None,
) -> bool:
    """秘书会话的纯只读工具访问「已登记项目工作区」内路径时返回 True（免越界弹窗）。

    安全边界：
    1. 必须是秘书会话（显式 session_name 或当前执行线程/UI 会话）；
    2. 工具必须属于 ``SECRETARY_READONLY_WORKSPACE_TOOLS``，绝不放行任何写工具；
    3. 路径解析为真实绝对路径后必须严格落在已登记的非根目录项目工作区内。
    """
    name = str(tool_name or "").strip()
    if name not in SECRETARY_READONLY_WORKSPACE_TOOLS:
        return False
    if not path_value:
        return False
    try:
        if not is_secretary_session(session_name):
            return False
        target = os.path.realpath(os.path.expanduser(str(path_value)))
        if os.name == "nt" and target.startswith("\\\\?\\"):
            target = target[4:]
        target_norm = os.path.normcase(target)
    except Exception:
        return False
    if not target_norm:
        return False
    for root in _get_registered_workspace_roots():
        root_norm = os.path.normcase(root)
        try:
            if os.path.commonpath([target_norm, root_norm]) == root_norm:
                return True
        except ValueError:
            continue
    return False


def is_secretary_tool_blocked(
    tool_name: str, side_effect_scope: str = "", session_name=None
) -> bool:
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


def build_secretary_persona_block(persona) -> str:
    try:
        cleaned = validate_secretary_persona(persona)
    except ValueError:
        cleaned = str(persona or "").strip()
    if not cleaned:
        return ""
    return (
        "# 秘书人设（本会话专属，由用户设定）\n\n"
        "以下是用户为这位秘书设定的完整人设，取代内置默认的秘书定位与性格设定"
        "（可靠干练、信息优先、结论先行、不寒暄、不卖萌等风格描述一律不再沿用；"
        "秘书的名字与称呼以「秘书身份」为准）。绝对红线、调度能力与硬约束、"
        "核心原则、工具使用规范与工作流不受人设影响，仍然完整生效。\n"
        + cleaned
    )
