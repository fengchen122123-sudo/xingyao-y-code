"""秘书身份解析：当前会话生效的显示名 / 自称 / 专属人设的唯一事实源。

背景：侧栏「秘书」分组下的会话是独立身份实体——每个会话各自起名（落库
`sessions.secretary_name`），此后该会话的显示与系统提示词身份全程使用这个名字，
且不复用设置页的人设名。

专属人设（2026-10-20）：每个秘书都是用户自己创建的独立 agent，可另存一份
`sessions.secretary_persona` 覆盖内置默认秘书人设；留空即用默认，且与设置页
「星瑶人设」互不影响（永远不回落到 persona_settings）。

设计约束：
- 本模块只做"读 + 解析"，写入仍由 MemoryManager 承担；
- 容错优先：任何异常都回落 persona 名（或空串），绝不抛错——身份解析处在每轮
  请求与工具提示的高频路径上，不能因一次解析失败拖垮整轮；
- 会话名 → (来源, 名字, 人设) 做进程内缓存，避免高频路径每轮打 DB；创建 /
  改名 / 改人设 / 删除会话的调用方负责调用 `invalidate_secretary_cache` 失效。
"""

from __future__ import annotations

import threading

SECRETARY_SOURCE = "SECRETARY"
# 历史遗留的未命名秘书会话兜底名（本次改动之前创建的秘书会话没有名字）
FALLBACK_SECRETARY_NAME = "秘书"

# 名字长度约束：strip 后 1..20 字符；非法值拒绝而非静默截断
_MAX_NAME_LEN = 20
# 人设长度上限：与设置页人设（persona_settings.MAX_PERSONA_LEN）同口径，
# 避免两套"人设"对同一类文本给出不同长度限制
MAX_PERSONA_LEN = 20000

# 进程内缓存：键 = 会话名，值 = (source, name, persona)。会话名是稳定标识，
# 缓存值随创建 / 改名 / 改人设 / 删除失效，不设 TTL。
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[str, str, str]] = {}


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


def validate_secretary_persona(persona) -> str:
    """校验并返回去首尾空白后的秘书人设；空值合法（= 使用内置默认人设）。

    只有非空且超长才抛错——人设是可选项，留空必须能建秘书，不能把"没写人设"
    当成非法输入拒掉。
    """
    if persona is None:
        return ""
    if not isinstance(persona, str):
        raise ValueError("秘书人设必须是文本")
    cleaned = persona.strip()
    if len(cleaned) > MAX_PERSONA_LEN:
        raise ValueError(f"秘书人设过长（最多 {MAX_PERSONA_LEN} 字符）")
    return cleaned


def _resolve_session_name(session_name):
    """统一会话名入口：显式传入 > 本回合线程所属会话 > UI 当前会话。

    为什么线程会话必须优先（2026-11-12 修复）：``get_current_session`` 的语义是
    "UI 当前查看的会话"，会随用户在侧栏切换而变化。后台回合（秘书派活、星轨
    自动化、忙时会话补发）跑在独立线程里，若只看 UI 当前会话，用户在秘书会话
    里派活的那一刻，被派的目标会话就会被判成秘书会话：工具面被裁成秘书只读集
    （实测 harness_steps.tools_bytes=25206，与秘书会话逐字节相同），系统提示词
    也换成秘书契约，模型于是回答"本会话是秘书会话，不能执行命令、不能写文件"。
    回合线程在 ``app_context.set_thread_session`` 里绑定了真实会话，它才是本回合
    的身份事实源；没有线程绑定的交互路径仍回落到 UI 当前会话，行为与改动前一致。
    （对应 app_context.py 对两者的区分：current=UI 视角，thread=回合视角）
    """
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
    """读取 (source, name, persona)；无管理器 / 查询异常回落 ("", "", "")。"""
    try:
        from core import app_context

        manager = app_context.get_manager()
        if manager is None:
            return ("", "", "")
        source = manager.get_session_source(session_name) or ""
        name = manager.get_session_secretary_name(session_name) or ""
    except Exception:
        return ("", "", "")
    # 人设单独读取：测试替身 / 老实现可能没有该方法，缺它只应损失人设，
    # 不该连来源与名字一起丢掉
    persona = ""
    try:
        persona = manager.get_session_secretary_persona(session_name) or ""
    except Exception:
        persona = ""
    return (str(source), str(name), str(persona))


def _cached_identity(session_name):
    """按会话名取 (source, name, persona)，命中缓存则不打 DB。"""
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

# 仅秘书会话可见的调度工具：普通会话工具集零变化（设计第 2/7 节）
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

# 秘书会话的额外允许项：这些工具不是只读（network / system / 跨会话写），
# 但属于秘书的核心职责或无害的公开信息查询。其余允许项一律由
# ``side_effect_scope == "none"`` 自动覆盖。
SECRETARY_ALLOWED_TOOLS = SECRETARY_ONLY_TOOLS | frozenset({
    "get_weather",
    "search_news",
    "fetch_url",
    # 项目画像写入：作用域 system（只写 ~/.ycode/memory/projects/，跨项目），
    # 必须按名字显式放行；普通会话同样可见（特性 1）。
    "remember_decision",
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


def build_secretary_persona_block(persona) -> str:
    """构建注入秘书系统提示词的"专属人设"块；人设为空返回空串（不注入）。

    为什么单独成块并显式声明"取代"：秘书提示词随包分发、开头就写着默认定位
    （可靠干练、结论先行、不寒暄…）。用户自定义人设若只是追加在后面，模型会
    同时看到两套性格描述而互相打架，所以必须写明覆盖范围；但绝对红线、调度
    硬约束、工具与安全规则不属于"人设"，一律保留。
    """
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
