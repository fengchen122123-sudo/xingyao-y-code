"""
core/agent_service.py
=====================
无界面（headless）的一轮对话准备逻辑。

TUI (main.py) 与 GUI (gui_bridge.py) 共用同一套上下文组装流程，
保证两端的回答质量完全一致：记忆压缩、系统提示词、运行上下文、
后台子代理结果注入等步骤只在这里实现一次。

约束：
- 本模块绝不 import prompt_toolkit / main，所有界面副作用通过 notify 回调外抛。
- 所有耗时步骤之间检查 cancel_event，让"准备请求"阶段也能被用户打断。
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
from typing import Any, Callable, Optional

from core.context_builder import TIME_NOTE_PREFIX

logger = logging.getLogger(__name__)

# Deprecated：四级水位线阶梯（下方 SNIP/ELIDE/SUMMARIZE_RATIO）已取代单一阈值。
# 保留此常量与 prepare_turn_messages 的 compress_threshold 形参仅为向后兼容，
# 阶梯逻辑内部不再使用。
DEFAULT_COMPRESS_THRESHOLD = 0.70

# 四级水位线（DeepSeek 前缀缓存友好，设计见
# docs/superpowers/plans/2026-08-05-deepseek-cache-99.md）：
# 本地改写（snip/elide）只发生在保护区之外、每条消息最多两次且字节永久冻结；
# 付费 LLM 摘要（全量前缀重写，一整条 10 万级 token 的纯 miss 请求）推迟到 95% 水位才触发。
SNIP_RATIO = 0.60
ELIDE_RATIO = 0.80
SUMMARIZE_RATIO = 0.95

# 兜底系统提示词：提示词文件全部缺失时也不能让整轮对话失败
FALLBACK_SYSTEM_PROMPT = "你叫星瑶，是一位顶尖的代码编程助手。"

# 通用编码规范（并入稳定系统提示词前缀，跨轮不变，对任何编码项目适用）。
# 作为稳定前缀的扩充块，同时帮助 DeepSeek 磁盘缓存跨越更多完整缓存块。
_GENERAL_CODING_STANDARDS = """【通用编码规范】
1. 修改代码前先理解现有结构与调用链，坚持最小改动原则，不做无关重构。
2. 改动后必须运行相关测试或编译/语法检查验证，验证通过才能声明任务完成。
3. 错误排查先定位根因（读报错、复现、查最近变更），再给出 1-3 个可选方案，不盲目重试。
4. 工具调用失败要如实保留错误信息，不隐瞒、不谎报成功；同因错误最多重试 2 次。
5. 结论优先，再展开细节；不过度寒暄，保持专业高效。
6. 删除文件/目录、危险 Git 操作、批量修改、安装全局依赖等破坏性操作先请求用户确认。
7. 新增功能优先复用现有工具、函数与模式，避免重复造轮子。
8. 命名遵循现有代码风格，注释补充必要上下文而非复述代码。
9. 处理文本数据注意编码一致性，避免 GBK/UTF-8 混淆导致乱码。
10. 长任务分阶段反馈进度，避免长时间静默。
11. 交付代码遵循对应语言的社区规范，添加必要注释，给出可直接运行的完整代码块。
12. 优先给出可执行的结论，不确定时明确说明假设，不臆测需求。"""

# 后台子代理结果注入时，单条结果的最大长度
_BG_RESULT_MAX_CHARS = 1500

# MCP schema 预热标记：进程内只尝试一次
_MCP_WARMUP_STARTED = False


def _warmup_mcp_schemas_once() -> None:
    """后台线程预热 MCP 连接，让 tools 数组从第二轮起保持稳定。

    MCP 是惰性连接：若等到会话中某轮才连上，get_tools_schema() 返回的
    tools 数组会从空变满，导致 provider 侧 tools/前缀缓存整体失效。
    首轮准备时在后台提前触发连接（CONNECT_TIMEOUT=10s，绝不阻塞当前轮），
    连接失败也不影响对话主流程。
    """
    global _MCP_WARMUP_STARTED
    if _MCP_WARMUP_STARTED:
        return
    _MCP_WARMUP_STARTED = True
    try:
        from core.mcp_client import mcp_manager
        mcp_manager._load_config()
        servers = mcp_manager.config.get("mcpServers", {}) if isinstance(mcp_manager.config, dict) else {}
        enabled = [
            name for name, conf in servers.items()
            if not (isinstance(conf, dict) and conf.get("enabled", True) is False)
        ]
        if not enabled or mcp_manager.servers:
            return
        threading.Thread(
            target=mcp_manager._ensure_connected,
            name="mcp-schema-warmup",
            daemon=True,
        ).start()
    except Exception:
        logger.debug("MCP schema 预热失败，已忽略", exc_info=True)


def _repo_root() -> str:
    """返回项目根目录（core/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def evict_oldest_turn(history: list) -> bool:
    """从内存历史中丢弃最早的一个完整轮次（user 开头到下一个 user 之前）。

    返回 True 表示确实丢弃了内容，False 表示没有可丢弃的轮次。
    """
    start = next(
        (i for i, msg in enumerate(history) if msg.get("role") != "system"),
        None,
    )
    if start is None:
        return False
    end = next(
        (j for j in range(start + 1, len(history)) if history[j].get("role") == "user"),
        len(history),
    )
    del history[start:end]
    return True


def load_system_prompt(is_gui: bool = False) -> str:
    """三级兜底读取系统提示词：配置路径 → 仓库内提示词文件 → 内置字面量。

    is_gui=True 时优先读取 GUI 专用提示词（system_prompt_gui.md），
    找不到则回退到通用提示词，确保任何情况下都不会因文件缺失而整轮失败。
    """
    system_prompt = ""
    try:
        if is_gui:
            from config import SYSTEM_PROMPT_GUI_FILE
            prompt_path = SYSTEM_PROMPT_GUI_FILE
            if os.path.exists(prompt_path):
                with open(prompt_path, "r", encoding="utf-8") as f:
                    system_prompt = f.read().strip()
            if not system_prompt:
                fallback_path = os.path.join(_repo_root(), "system_prompt_gui.md")
                if os.path.exists(fallback_path):
                    with open(fallback_path, "r", encoding="utf-8") as f:
                        system_prompt = f.read().strip()
            if system_prompt:
                return system_prompt
            # GUI 专用提示词不存在时回退到通用提示词
        from config import SYSTEM_PROMPT_FILE
        prompt_path = SYSTEM_PROMPT_FILE
        if os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                system_prompt = f.read().strip()
        if not system_prompt:
            fallback_path = os.path.join(_repo_root(), "system_prompt.md")
            if os.path.exists(fallback_path):
                with open(fallback_path, "r", encoding="utf-8") as f:
                    system_prompt = f.read().strip()
    except Exception as e:
        logging.warning("读取 system prompt 失败: %s", e)
        system_prompt = ""
    if not system_prompt:
        # 最后一道兜底，确保任何情况下都不会因为提示词文件缺失而整轮失败
        system_prompt = FALLBACK_SYSTEM_PROMPT
    return system_prompt


def build_stable_system_message(is_gui: bool = False) -> dict:
    """组装每轮请求首位的稳定系统提示词消息，供请求组装与 Token 估算复用。

    稳定前缀 = 基础系统提示词 + 决策策略 + 通用编码规范 + 长期记忆
               + 技能目录 + 自定义子 Agent 清单。
    后四块内容跨轮基本不变（记忆仅在用户显式要求记录/归纳时变化，技能与子 Agent
    在会话内稳定），并入稳定前缀可把前缀撑到 2048 token 的整数倍以上，
    显著提升 DeepSeek 磁盘缓存（按 2048 token 块匹配）的命中覆盖范围。

    注意：任何跨轮不变的静态内容都应优先并入这里，而不是放进动态运行上下文块，
    动态块每轮变化会打断 provider 侧缓存前缀。
    """
    from core.context_builder import build_stable_prompt

    parts = [build_stable_prompt(load_system_prompt(is_gui))]
    parts.append(_GENERAL_CODING_STANDARDS)
    try:
        import core.memory_kairos as memory_kairos
        memory_section = memory_kairos.build_memory_system_section()
        if memory_section.strip():
            parts.append(memory_section)
    except Exception:
        pass
    try:
        from core.skills import get_active_skills_catalog
        skills_catalog = get_active_skills_catalog()
        if skills_catalog.strip():
            parts.append(skills_catalog)
    except Exception:
        pass
    try:
        from core.subagent_registry import get_custom_subagents_catalog
        subagents_catalog = get_custom_subagents_catalog()
        if subagents_catalog.strip():
            parts.append(subagents_catalog)
    except Exception:
        pass
    return {"role": "system", "content": "\n\n".join(parts)}

def estimate_history_tokens_with_system(model_name: str, history: list, is_gui: bool = False) -> int:
    """估算一轮请求的实际 Token 占用：历史消息 + 首部稳定系统提示词。

    主系统提示词每轮现组、不落库，纯历史估算会漏掉它。
    仅作为“无真实用量记录”时的兜底口径（见 get_session_token_baseline）。
    """
    from core.llm import estimate_tokens
    return estimate_tokens(model_name, [build_stable_system_message(is_gui)] + list(history or []))


def get_session_token_baseline(manager, session_name: str, model_name: str,
                               history: list | None = None) -> int:
    """会话上下文占用基线：优先用本会话最近一次真实 API 用量。

    下一轮请求的 prompt 必然包含上一轮的 prompt + completion（回复会进入历史），
    所以最近一次真实 prompt_tokens + completion_tokens 就是最准的占用基线。
    只有在会话没有任何真实记录时（老会话首次使用）才退化到本地估算。
    """
    try:
        last = manager.get_last_token_usage(session_name)
    except Exception:
        last = None
    if last:
        return int(last.get("prompt_tokens") or 0) + int(last.get("completion_tokens") or 0)
    if history is None:
        history = manager.get_history(session_name)
    return estimate_history_tokens_with_system(model_name, history)


def build_runtime_context(model_name: str, permission_mode: str):
    """组装本轮的运行上下文（时间 / 计划快照 / 工作目录）。

    长期记忆、技能目录、自定义子 Agent 清单已并入稳定系统提示词
    （build_stable_system_message，跨轮不变的大段静态内容），
    这里不再重复注入，避免动态块膨胀与重复消耗 token。
    """
    from core.context_builder import RuntimeContext
    from core.plan import get_default_plan_manager

    now = datetime.datetime.now()
    plan_items = get_default_plan_manager().snapshot()
    active_plan = "\n".join(
        f"{item.get('stage', 'build')}/{item['status']}: {item['subject']}" for item in plan_items
    ) or "无"
    return RuntimeContext(
        model_name=model_name,
        permission_mode=permission_mode,
        working_dir=os.getcwd(),
        current_time=now.strftime("%Y-%m-%d %H:%M:%S"),
        active_plan=active_plan,
    )


def _insert_before_last_user(history: list, message: dict) -> None:
    """把 system 消息插到最后一条 user 消息之前；没有 user 结尾则直接追加。"""
    if history and history[-1].get("role") == "user":
        history.insert(len(history) - 1, message)
    else:
        history.append(message)


def _drop_legacy_leading_systems(history: list) -> list:
    """丢弃首个 user 消息之前的 system 消息（历史遗留的稳定提示词）。

    稳定提示词每轮重新组装插入到 position 0，不持久化；append-only 持久化的
    上下文块（动态块/时间注记/后台汇总）按设计都位于某条 user 消息之后，
    不受本清理影响。
    """
    cleaned = []
    seen_user = False
    for msg in history:
        if msg.get("role") == "system" and not seen_user:
            continue
        cleaned.append(msg)
        if msg.get("role") == "user":
            seen_user = True
    return cleaned


def _reposition_context_blocks(history: list) -> list:
    """把持久化的上下文块在内存中前移回对应 user 消息之前。

    持久化时上下文块按自然追加顺序落在当轮 user 消息之后（DB: [user][块]）；
    发给模型时在内存中还原为 [块][user]，与当轮新注入的块位置约定一致。
    该重排每轮确定性执行，保证跨轮请求的 prefix 字节级连续（缓存命中关键）。
    """
    out: list = []
    for msg in history:
        if msg.get("role") != "system" or not out:
            out.append(msg)
            continue
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") == "user":
                out.insert(i, msg)
                break
        else:
            out.append(msg)
    return out


def _persist_context_block(manager, session_name: str, content: str) -> None:
    """把上下文块以 system 角色持久化到会话历史（append-only，永不删除）。

    落库失败只记录日志：下一轮会基于 DB 内容重新注入，不影响本轮请求。
    """
    try:
        manager.add_message(session_name, "system", content)
    except Exception:
        logger.warning("上下文块持久化失败，下一轮将重新注入", exc_info=True)


def _strip_time_note_from_content(content):
    """从 user 消息内容末尾移除上一轮追加的时间注记。

    时间注记格式为 ``\\n\\n【当前时间】YYYY-MM-DD HH:MM:SS``，
    可能被上一次 CAS 写入也可能被旧的 role=system 持久化方式留下。
    """
    text = content
    if not isinstance(text, str):
        return text
    marker = "\n\n" + TIME_NOTE_PREFIX
    idx = text.rfind(marker)
    if idx != -1 and text[idx + len(marker):].strip():
        # 仅在时间注记位于末尾时才移除
        suffix = text[idx + len(marker):]
        if len(suffix) <= 25:  # YYYY-MM-DD HH:MM:SS = 19 字符，留余量
            text = text[:idx]
    return text


def _append_time_note_to_last_user(history, time_note, manager, session_name):
    """把时间注记内联到最后一条 user 消息 content 末尾并 CAS 持久化。

    时间注记不再作为独立 role=system 消息持久化（那样会使 system_sha256
    每轮变化，打断 provider 侧前缀缓存）。改为追加到当前轮 user 消息
    content 末尾，通过 CAS 写回 DB。
    """
    if not history:
        return
    # 找到最后一条 user 消息
    last_user_idx = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return
    original_content = history[last_user_idx].get("content", "")
    if not isinstance(original_content, str):
        # 非文本 content（如媒体消息），跳过时间注记注入
        return
    # 移除上一轮可能遗留的时间注记，避免重复堆积
    cleaned = _strip_time_note_from_content(original_content)
    new_content = cleaned + "\n\n" + time_note
    history[last_user_idx]["content"] = new_content
    # CAS 持久化：把修改写回 DB（期望旧值 = 原始 content）
    try:
        manager.replace_latest_user_message_content(
            session_name, original_content, new_content
        )
    except Exception:
        logger.warning("时间注记 CAS 持久化失败，仅影响内存副本", exc_info=True)


def build_background_task_summary(session_name: str) -> str:
    """提取本会话中所有终态且尚未消费的后台子代理任务，拼成注入用的 system 文本。

    没有可汇总内容时返回空字符串。drain 语义会把任务标记为已消费，避免重复注入。
    """
    from core.tools import subagent_manager as _sm_for_fallback

    terminal_bg_tasks = _sm_for_fallback.drain_terminal_background_tasks(session_name)
    if not terminal_bg_tasks:
        return ""

    summary_lines = ["📦 后台子代理任务结果汇总（请基于以下结果回复用户）："]
    has_failed = False
    for t in terminal_bg_tasks:
        status_label = {
            "completed": "已完成",
            "failed": "失败",
            "timed_out": "超时",
            "cancelled": "已取消",
        }.get(t.status, t.status)
        summary_lines.append("")
        summary_lines.append(f"【任务 {t.task_id}（{status_label}）】")
        summary_lines.append(f"原始描述: {t.description}")
        if t.status == "completed":
            # 成功任务：注入结果供主代理汇总
            result_text = (t.result or "").strip()
            # 截断过长的结果，避免淹没上下文
            if len(result_text) > _BG_RESULT_MAX_CHARS:
                result_text = result_text[:_BG_RESULT_MAX_CHARS] + "...（已截断）"
            if result_text:
                summary_lines.append(f"结果: {result_text}")
            else:
                summary_lines.append("结果: （空）")
        elif t.status == "cancelled":
            # 用户主动取消：如实列出即可，绝不附“接手完成”指令——否则用户取消后的
            # 下一个回合（自动汇总或手动消息）会把被取消的任务接手做完，直接违背用户意图
            summary_lines.append("该任务已被用户主动取消，无需接手完成，也不要重新派发或重试。")
        else:
            # 失败/超时任务：主代理应接手用自身工具完成
            has_failed = True
            if t.error:
                err_short = t.error[:300]
                summary_lines.append(f"失败原因: {err_short}")
            summary_lines.append(
                "请用自身工具（如 search_news / fetch_url / get_weather 等）"
                "接手完成该任务的目标，不要重新派发子代理。"
            )
    summary_lines.append("")
    if has_failed:
        summary_lines.append(
            "请汇总所有已完成任务的结果，并对接手完成的失败任务一并汇总后回复用户。"
            "若任务确实无法完成（如网络受限），请明确告知用户原因。"
        )
    else:
        summary_lines.append("请基于以上结果汇总后回复用户。")
    return "\n".join(summary_lines)


def prepare_turn_messages(
    manager,
    session_name: str,
    model_name: str,
    context_window: int,
    *,
    permission_mode: str,
    cancel_event: Optional[threading.Event] = None,
    notify: Optional[Callable[[str, str], None]] = None,
    api_key: str = "",
    api_base: str = "",
    compress_threshold: float = DEFAULT_COMPRESS_THRESHOLD,
    is_gui: bool = False,
) -> list:
    """组装一轮对话真正发给大模型的完整消息列表。

    调用前提：用户本轮输入已经写入数据库（manager.add_message）。

    步骤严格保持与历史 TUI 实现一致的顺序：
      1. 读取入库边界已经维护完成的 LLM 历史 + 估算 token
      2. 按四级水位线逐级升级：snip → elide → 智能压缩 → 物理折叠 → 滑窗淘汰
      3. 剔除历史遗留 system 消息
      4. 读取系统提示词（三级兜底）
      5. 组装 RuntimeContext，插入稳定提示词 + 动态上下文
      6. 注入后台子代理结果汇总

    参数：
      notify: 可选的 notify(kind, message) 回调，用于把压缩进度等提示交回界面层，
              本模块自身绝不打印任何内容。
    """
    from core.llm import estimate_tokens, session_token_usage, token_usage_lock

    def _notify(kind: str, message: str) -> None:
        if notify is None:
            return
        try:
            notify(kind, message)
        except Exception:
            # 界面回调异常绝不能影响对话主流程
            logger.debug("notify 回调异常，已忽略", exc_info=True)

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # MCP schema 预热（进程内一次，后台线程，不阻塞本轮）：稳定后续轮次的 tools 数组
    _warmup_mcp_schemas_once()

    try:
        window = int(context_window or 0)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        window = 128000

    # 获取专门供给大模型的已压缩截断与记忆挂载历史
    history = manager.get_history_for_llm(session_name)

    # 只在这一层组装一个稳定提示词和一份当前运行上下文。
    from core.context_builder import (
        DYNAMIC_CONTEXT_PREFIX,
        build_dynamic_prompt,
        build_time_note,
    )

    # 提前组装首部稳定系统提示词：Token 估算必须把它计入，
    # 否则口径会低于实际请求（系统提示词不落库、每轮现组）
    system_message = build_stable_system_message(is_gui)

    # 【四级水位线阶梯】在向大模型请求前，按水位逐级升级维护强度。
    # 触发判断优先用本会话最近一次真实 API 用量作基线（下一轮 prompt 必然包含
    # 上一轮的 prompt + completion）；无真实记录时才用含系统提示词的估算兜底。
    # 任何本地改写发生后真实基线即失效，退化到含系统提示词的估算指导后续层级。
    estimated_tokens = get_session_token_baseline(
        manager, session_name, model_name, history=history
    )

    # Tier 1 (≥60%)：本地 snip 旧工具结果（一次性改写并冻结，不重排、不重写前缀主体）
    if estimated_tokens > window * SNIP_RATIO and not _cancelled():
        snipped, _ = manager.maintain_tool_results(session_name, tier=1)
        if snipped:
            _notify("compress", f"<ansiyellow>ℹ 已折叠 {snipped} 条旧工具结果以节省上下文。</ansiyellow>")
            history = manager.get_history_for_llm(session_name)
            estimated_tokens = estimate_tokens(model_name, [system_message] + history)

    # Tier 2 (≥80%)：旧工具结果整体占位符化（snipped 升级一次）
    if estimated_tokens > window * ELIDE_RATIO and not _cancelled():
        _, elided = manager.maintain_tool_results(session_name, tier=2)
        if elided:
            _notify("compress", f"<ansiyellow>ℹ 已归档 {elided} 条旧工具结果。</ansiyellow>")
            history = manager.get_history_for_llm(session_name)
            estimated_tokens = estimate_tokens(model_name, [system_message] + history)

    # Tier 3 (≥95%)：压缩经济学——本地维护已把压力拉回线下则跳过付费摘要
    if estimated_tokens > window * SUMMARIZE_RATIO and not _cancelled():
        # 超过 0.95 水位才触发智能记忆压缩提炼。
        # 注意：compress_session_memory 在调用线程内同步执行且暂不支持取消参数，
        # 压缩期间的取消信号要等压缩返回后才会生效（接口约束，暂不透传取消）。
        from core.memory_compressor import compress_session_memory
        compress_session_memory(
            manager,
            session_name,
            model_name,
            api_key,
            api_base,
            output_callback=lambda message: _notify("compress", message),
        )
        history = manager.get_history_for_llm(session_name)
        # 压缩/裁剪改变了历史，此后的真实用量基线已失效，
        # 退化到含系统提示词的估算来指导后续的折叠与滑窗淘汰
        estimated_tokens = estimate_tokens(model_name, [system_message] + history)

        # 兜底：如果总结归纳后依然超限，触发物理大文件折叠机制
        if estimated_tokens > window * SUMMARIZE_RATIO:
            manager.prune_history_files(session_name, max_turns=1, retain_recent=4)
            history = manager.get_history_for_llm(session_name)
            estimated_tokens = estimate_tokens(model_name, [system_message] + history)

        # 智能滑窗淘汰：如果仍超限，按完整轮次（user 开头到下一个 user 之前）
        # 为单位淘汰最早的旧消息，直到满足 Token 安全阈值。
        # 淘汰必须在 DB 层推进 summarized_until_msg_id 持久化（一次性水位推进，
        # 不改写消息字节）：纯内存淘汰下一轮 prepare 会从 DB 读回全部历史，淘汰失效。
        while len(history) > 2 and estimated_tokens > window * SUMMARIZE_RATIO:
            if not manager.evict_oldest_turn_persistent(session_name):
                break
            history = manager.get_history_for_llm(session_name)
            estimated_tokens = estimate_tokens(model_name, [system_message] + history)

        # 淘汰到底（仅剩最后轮次）仍超窗口：剩下的单条消息本身就超限。
        # 明确告知用户，而不是裸发一个几乎必然被 provider 拒绝的超限请求。
        if estimated_tokens > window and not _cancelled():
            _notify("compress", "<ansired>⚠️ 当前上下文在压缩与淘汰后仍超过模型窗口，本轮请求可能被 provider 拒绝；建议精简输入、拆分任务或开启新会话。</ansired>")

    # 更新最新 Token 数供界面显示（与 UI 线程并发读写，必须持锁）
    with token_usage_lock:
        session_token_usage["latest_total"] = estimated_tokens

    # 只在这一层组装当轮的运行上下文（稳定提示词已在估算前组装）。
    # 1) 清理 legacy 首部 system（稳定提示词每轮现组）；持久化的上下文块保留
    history = _drop_legacy_leading_systems(history)
    # 2) 持久化的上下文块（DB 中位于当轮 user 之后）前移回 user 之前，
    #    每轮确定性重排，保证跨轮请求 prefix 字节级连续
    history = _reposition_context_blocks(history)

    runtime = build_runtime_context(model_name, permission_mode)
    dynamic_prompt = build_dynamic_prompt(runtime)
    history.insert(0, system_message)

    # 3) 动态运行上下文块：内容不变则沿用历史中已持久化的那份（不重注、不删除），
    #    变化时才追加新块并持久化——append-only 是跨轮缓存命中的关键
    latest_dynamic = next(
        (
            str(m.get("content") or "")
            for m in reversed(history)
            if m.get("role") == "system"
            and str(m.get("content") or "").startswith(DYNAMIC_CONTEXT_PREFIX)
        ),
        None,
    )
    if dynamic_prompt != latest_dynamic:
        _insert_before_last_user(history, {"role": "system", "content": dynamic_prompt})
        _persist_context_block(manager, session_name, dynamic_prompt)

    # 4) 时间注记：追加到最后一条 user 消息内容末尾并持久化。
    #    以前时间注记是独立 role=system 消息，每轮新增一条会导致 system_sha256
    #    变化，使 provider 侧前缀缓存从 system 块开始整体失效（实测命中率
    #    从 ~99% 跌至 0-4%，多消耗数百万 tokens）。改为内联到 user 消息后，
    #    system_messages 数组跨轮稳定，前缀缓存可覆盖整个对话历史。
    time_note = build_time_note(runtime)
    _append_time_note_to_last_user(history, time_note, manager, session_name)

    # 主代理汇总/兜底机制：提取本会话中所有终态（成功+失败）且尚未消费的后台子代理任务，
    # 注入到 history 中作为 system 消息，让主代理能汇总成功结果并用自身工具接手失败任务。
    try:
        summary_msg = build_background_task_summary(session_name)
        if summary_msg:
            # 与其他上下文块同样 append-only：注入一次后持久化保留
            _insert_before_last_user(history, {"role": "system", "content": summary_msg})
            _persist_context_block(manager, session_name, summary_msg)
    except Exception:
        # 汇总机制失败不能影响主流程，但必须留诊断痕迹（裸吞会让注入失效不可排查）
        logger.warning("后台子代理汇总注入失败，已跳过本轮汇总", exc_info=True)

    return history


# ---------------------------------------------------------------------------
# /undo 的可复用非界面部分：TUI 与 GUI 共用
# ---------------------------------------------------------------------------

def undo_last_edit(manager, session_name: str, force: bool = False) -> tuple[bool, str]:
    """撤销星瑶在本会话最近一次的文件修改，并同步抹除对应的模型记忆。

    返回 (ok, message)：
      - ok=False 时 message 为失败原因（界面层直接展示即可）
      - 删除"星瑶新建文件"属于高危操作，未传 force=True 时会拒绝并给出提示
    """
    from core.memory import get_undo_state, set_undo_state
    from core.tools import write_file, _resolve_workspace_path, _atomic_write_edit_file, sandbox_bypass

    state = get_undo_state(session_name)
    file_path = state.get("file_path")
    old_content = state.get("old_content")

    if not file_path:
        return False, "当前会话没有可以撤销的操作记录。"
    if old_content is None:
        # old_content 为 None 表示缺少原始内容快照，直接写 None 会损坏文件
        return False, "撤销失败：缺少文件原始内容记录，无法安全恢复。"

    # 删除新建文件是高危操作：要求显式确认，避免跨会话/误操作导致用户数据丢失
    will_delete = old_content == ""
    if will_delete and not force:
        return False, f"该操作将删除星瑶新建的文件 {file_path}，需要显式确认后才能执行。"

    try:
        if will_delete:
            if os.path.exists(file_path):
                os.remove(file_path)
            message = f"撤销成功！已删除星瑶新建的文件: {file_path}"
        else:
            # 撤销是用户显式发起的恢复操作：沙盒外路径也直接放行，
            # 避免之前单次批准写出的沙盒外文件无法撤销
            with sandbox_bypass():
                result = write_file(file_path, old_content)
                if isinstance(result, str) and result.startswith("🚨"):
                    # write_file 的大文件覆盖保护会拒绝恢复超过 500 行/50KB 的已有文件，
                    # 但撤销本来就是刻意的整文件回滚：绕过该保护改走原子直写，
                    # 仍通过 _resolve_workspace_path 保留工作区沙箱校验。
                    resolved = _resolve_workspace_path(file_path)
                    write_err = _atomic_write_edit_file(resolved, "utf-8", old_content)
                    if write_err:
                        return False, f"撤销失败：{write_err}"
            message = f"撤销成功！已将 {file_path} 恢复到上一个版本。"

        # 清理大模型的错误记忆，防止幻觉
        manager.undo_last_interaction(session_name)
        # 清空状态防止重复撤销
        set_undo_state(None, None, session_name=session_name)
        return True, message
    except Exception as e:
        return False, f"撤销失败: {e}"
