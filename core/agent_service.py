"""
core/agent_service.py
=====================
无界面（headless）的一轮对话准备逻辑。

CLI (main.py) 与 GUI (gui_bridge.py) 共用同一套上下文组装流程，
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
import tempfile
import threading
from typing import Any, Callable, Optional

from core.context_builder import DYNAMIC_CONTEXT_PREFIX, TIME_NOTE_PREFIX

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

# 单次请求准备允许滑窗淘汰的最大轮次数（微调淘汰，严禁无节制清空会话）
MAX_EVICT_TURNS = 2

# 兜底系统提示词：提示词文件全部缺失时也不能让整轮对话失败
FALLBACK_SYSTEM_PROMPT = "你叫{agent_name}，是一位顶尖的代码编程助手。"

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

_BG_SUMMARY_PREFIX = "📦 后台子代理任务结果汇总"

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
    """读取系统提示词：只从随包文件加载（仓库内 / 安装版 _internal），不从用户目录读取。

    设计决策（2026-08-21）：系统提示词是应用行为契约的一部分，跟随安装包分发，
    不允许用户在 ~/.ycode 下修改或替换（用户目录只放数据与记忆，不放行为指令）。
    用户可控的自定义入口是 ~/.ycode/memory/ 全局记忆体系，不是提示词文件。

    严格分离（2026-08-21）：GUI 与 CLI 各自只读自己的提示词文件，绝不回退混用——
    GUI 读 system_prompt_gui.md，CLI 读 system_prompt.md。GUI 版包含"不支持斜杠
    命令、引导用户看右侧面板"等 GUI 专属规则，回退到 CLI 版会让星瑶在图形界面里
    建议用户使用根本不存在的 /mcp /tasks 等命令，属于必须避免的行为错乱。
    """
    system_prompt = ""
    try:
        filename = "system_prompt_gui.md" if is_gui else "system_prompt.md"
        prompt_path = os.path.join(_repo_root(), filename)
        if os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                system_prompt = f.read().strip()
            if system_prompt:
                return system_prompt
    except Exception as e:
        logging.warning("读取 system prompt 失败: %s", e)
        system_prompt = ""
    if not system_prompt:
        # 最后一道兜底，确保任何情况下都不会因为提示词文件缺失而整轮失败
        try:
            from core.persona_settings import get_agent_display_name
            system_prompt = FALLBACK_SYSTEM_PROMPT.format(agent_name=get_agent_display_name())
        except Exception:
            system_prompt = FALLBACK_SYSTEM_PROMPT.format(agent_name="星瑶")
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
    # 人设设置块：仅当用户在设置页自定义了名称/称呼/人设时非空；
    # 默认配置返回空串，不影响 byte-stability。跨轮不变，归入稳定前缀。
    try:
        from core.persona_settings import build_persona_prompt_block

        persona_block = build_persona_prompt_block()
        if persona_block:
            parts.append(persona_block)
    except Exception:
        pass
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
    """会话上下文占用基线：真实 API 用量与本地估算取较小值。

    下一轮请求的 prompt 必然包含上一轮的 prompt + completion（回复会进入历史），
    正常情况下最近一次真实 prompt_tokens + completion_tokens 是最准的占用基线。
    但从大窗口模型切到小窗口模型后，上一轮真实用量仍是大窗口下的旧值，会虚高于
    当前历史的实际占用；此时含系统提示词的本地估算更贴近真实，取较小值可避免
    对着过期的大基线误判"无需压缩"。无真实记录时退化为纯本地估算。
    """
    try:
        last = manager.get_last_token_usage(session_name)
    except Exception:
        last = None
    if history is None:
        history = manager.get_history(session_name)
    estimated = estimate_history_tokens_with_system(model_name, history)
    if last:
        usage_model = str(last.get("model_name") or "").strip()
        current_model = str(model_name or "").strip()
        # 最近真实用量属于别的模型时分词口径已失效。尤其切到分词密度
        # 更高的模型，继续取 min(real, estimated) 会低估并延迟压缩。
        if usage_model and usage_model != current_model:
            return estimated
        real = int(last.get("prompt_tokens") or 0) + int(last.get("completion_tokens") or 0)
        if real > 0:
            return min(real, estimated)
    return estimated


def build_runtime_context(model_name: str, permission_mode: str, api_base: str = "", turn_source: str = "local"):
    """组装本轮的运行上下文（时间 / 计划快照 / 工作目录 / 电脑操控能力）。

    长期记忆、技能目录、自定义子 Agent 清单已并入稳定系统提示词
    （build_stable_system_message，跨轮不变的大段静态内容），
    这里不再重复注入，避免动态块膨胀与重复消耗 token。

    电脑操控能力（Task 10，设计 9.3）在 Turn 准备起点解析一次并传入
    RuntimeContext：capability 关闭时动态块字节与历史版本完全一致。
    turn_source 是入口层受控值（CLI/GUI 本地回合固定 local；LAN 标记由
    LanServer dispatch 注入，Task 15 接入），绝不从用户消息或模型参数读取。
    Task 11a F1 收敛：解析结果同时存入线程级 Turn handoff，同一回合的
    chat_with_model_events 取出复用，capability 每 Turn 只解析一次。
    api_base 必须与 chat_with_model_events 实际使用的 base 一致（入口层
    同一来源传入）：native_openai 官方路由判定依赖它，漏传会让原生能力
    在生产入口永远以 computer_protocol_mismatch 拒绝（审查修复轮 I-1）。
    """
    from core.computer.capability import set_turn_computer_capability
    from core.context_builder import RuntimeContext
    from core.command_shell import describe_selected_command_shell
    from core.llm import resolve_turn_computer_capability
    from core.plan import get_default_plan_manager
    from core.workspace_context import get_workdir

    now = datetime.datetime.now()
    plan_items = get_default_plan_manager().snapshot()
    active_plan = "\n".join(
        f"{item.get('stage', 'build')}/{item['status']}: {item['subject']}" for item in plan_items
    ) or "无"
    computer_capability = resolve_turn_computer_capability(
        model_name, api_base=api_base or None, turn_source=turn_source
    )
    set_turn_computer_capability(computer_capability)
    # 按回合撤销纠正块（规格 15.3）：无纠正记录/服务未配置时为空串，
    # 动态块字节与历史版本一致；有纠正时按规范化工作区跨会话生效。
    from core.turn_undo.prompt_context import build_workspace_correction_section

    workspace_corrections = build_workspace_correction_section(get_workdir())
    return RuntimeContext(
        model_name=model_name,
        permission_mode=permission_mode,
        working_dir=str(get_workdir()),
        current_time=now.strftime("%Y-%m-%d %H:%M:%S"),
        active_plan=active_plan,
        command_shell=describe_selected_command_shell(),
        computer_capability=computer_capability,
        workspace_corrections=workspace_corrections,
    )


def _drop_legacy_context_systems(history: list) -> list:
    """丢弃历史中所有 role=system 的消息（旧格式遗留的上下文块）。

    动态运行上下文 / 时间注记 / 后台汇总这些上下文块曾经以 system 角色
    独立持久化，现已改为内联到 user 消息；历史里残留的 system 块应整体丢弃，
    否则会把 system 消息留在 user 之后，违反 provider 的
    「system 必须在数组开头」约束。
    """
    return [m for m in history if m.get("role") != "system"]


def strip_context_suffix(content):
    """移除 user 消息内容末尾内联的上下文后缀（动态块 / 后台汇总 / 时间注记）。

    上下文后缀按固定顺序追加：动态块 → 后台汇总 → 时间注记。此处反向剥离，
    直到末尾不再有可识别的上下文块标记为止，避免重入/重试时重复堆积。

    用途有两处：
      1. 请求组装重入前清理（内部 _append_context_suffix_to_last_user 调用）；
      2. 历史消息展示层（GUI 气泡 / CLI 回放）——数据库里持久化的 user 消息
         仍带着当轮上下文后缀（那是给模型看的运行快照），但展示给用户时应剥掉，
         否则历史对话里每条提问都拖着一大块「当前运行上下文 / 当前时间」。
    """
    text = content
    if not isinstance(text, str):
        return text
    changed = True
    while changed:
        changed = False
        # 时间注记在最后（格式：YYYY-MM-DD HH:MM:SS，19 字符，留余量）
        marker = "\n\n" + TIME_NOTE_PREFIX
        idx = text.rfind(marker)
        if idx != -1:
            suffix = text[idx + len(marker):].strip()
            if suffix and len(suffix) <= 25 and "\n" not in suffix:
                text = text[:idx]
                changed = True
                continue
        # 后台汇总块
        marker = "\n\n" + _BG_SUMMARY_PREFIX
        idx = text.rfind(marker)
        if idx != -1:
            text = text[:idx]
            changed = True
            continue
        # 动态运行上下文块
        marker = "\n\n" + DYNAMIC_CONTEXT_PREFIX
        idx = text.rfind(marker)
        if idx != -1:
            text = text[:idx]
            changed = True
            continue
    return text


def _append_context_suffix_to_last_user(
    history, dynamic_prompt, summary_msg, time_note, manager, session_name
):
    """把动态运行上下文 + 后台汇总 + 时间注记内联到最后一条 user 消息末尾并 CAS 持久化。

    上下文块不再作为独立 role=system 消息持久化（那会把 system 消息留到
    user 之后，违反 provider 的「system 必须在开头」约束）。改为内联到
    user 消息 content 末尾，messages 数组里只保留首位的稳定 system 提示词。
    """
    if not history:
        return
    last_user_idx = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return
    original_content = history[last_user_idx].get("content", "")
    if not isinstance(original_content, str):
        # 非文本 content（如媒体消息），跳过上下文内联
        return
    cleaned = strip_context_suffix(original_content)
    suffix_parts = [p for p in (dynamic_prompt, summary_msg, time_note) if p]
    if not suffix_parts:
        return
    new_content = cleaned + "\n\n" + "\n\n".join(suffix_parts)
    history[last_user_idx]["content"] = new_content
    try:
        manager.replace_latest_user_message_content(
            session_name, original_content, new_content
        )
    except Exception:
        logger.warning("上下文后缀 CAS 持久化失败，仅影响内存副本", exc_info=True)


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


def _prune_history_in_memory_to_fit_window(
    history: list, window: int, model_name: str, system_message: dict,
    estimated_tokens: int,
) -> tuple:
    """请求侧内存级轮次裁剪：仅在待发请求内存列表中按完整轮次剔除最早历史，
    保证发给小窗口模型的载荷处于 window * SUMMARIZE_RATIO 之内。
    绝不触碰 DB，绝不推进水位，保留数据库历史完整性。
    与 evict_oldest_turn_persistent 的区别：那个是 DB 层永久推进（下一轮读不回），
    这个只影响本次请求载荷，下一轮历史会原样从 DB 读回——所以它无法“省 token”，
    只用于在压缩/淘汰被禁时保证本轮不因超窗被 provider 拒绝。

    估算成本：每轮只对被剔除片段做一次局部估算并做减法，不对全量历史反复重扫
    （80 万 token 全量重扫可达数十秒，且这条路正跑在用户等回复的同步路径上）。
    返回 (裁剪掉的轮次数, 剩余估算 token)。
    """
    from core.llm import estimate_tokens

    limit = window * SUMMARIZE_RATIO
    remaining = int(estimated_tokens)
    pruned_turns = 0
    while remaining > limit:
        # 轮次起点口径与 memory.evict_oldest_turn_persistent 保持一致：
        # 第一条非 system 消息即轮次开头。摘要行是 get_history_for_llm 现插的
        # role=user 伪消息（DB 侧不存在），故此处显式跳过。改这里必须同步改那边，
        # tests/test_memory.py::test_memory_and_db_turn_boundary_agree 会把关。
        start_idx = None
        for i, m in enumerate(history):
            role = m.get("role")
            if role == "system":
                continue
            if role == "user" and str(m.get("content") or "").startswith("[历史对话记忆摘要"):
                continue
            start_idx = i
            break
        if start_idx is None:
            break
        end_idx = None
        for j in range(start_idx + 1, len(history)):
            if history[j].get("role") == "user":
                end_idx = j
                break
        if end_idx is None:
            break
        removed = history[start_idx:end_idx]
        remaining -= estimate_tokens(model_name, removed)
        del history[start_idx:end_idx]
        pruned_turns += 1
    return pruned_turns, remaining


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
    turn_source: str = "local",
) -> list:
    """组装一轮对话真正发给大模型的完整消息列表。

    调用前提：用户本轮输入已经写入数据库（manager.add_message）。

    步骤严格保持与历史 CLI 实现一致的顺序：
      1. 读取入库边界已经维护完成的 LLM 历史 + 估算 token
      2. 按四级水位线逐级升级：snip → elide → 智能压缩 → 物理折叠 → 滑窗淘汰
      3. 剔除历史遗留 system 消息
      4. 读取系统提示词（三级兜底）
      5. 组装 RuntimeContext，插入稳定提示词 + 动态上下文
      6. 注入后台子代理结果汇总

    参数：
      notify: 可选的 notify(kind, message) 回调，用于把压缩进度等提示交回界面层，
              本模块自身绝不打印任何内容。
      turn_source: 入口层受控回合来源（local|lan，设计 18）。LAN 消息回合由
              LanServer dispatch 注入 "lan"，本地 GUI/CLI 固定 local；决定
              本轮电脑操控能力解析（lan 固定禁用）。
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
        build_dynamic_prompt,
        build_time_note,
    )

    # 提前组装首部稳定系统提示词：Token 估算必须把它计入，
    # 否则口径会低于实际请求（系统提示词不落库、每轮现组）
    system_message = build_stable_system_message(is_gui)

    # 【四级水位线阶梯】在向大模型请求前，按水位逐级升级维护强度。
    # 触发判断用本会话最近一次真实 API 用量与本地估算的较小值作基线：
    # 真实用量通常更准，但从大窗口模型切到小窗口模型后，上一轮真实用量仍是
    # 大窗口下的旧值，会虚高于当前历史的实际占用——此时取估算能让压缩对新
    # 窗口真正生效，而不是对着过期的大基线误判"无需压缩"。
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
        from core.memory_compressor import (
            compress_session_memory,
            is_session_compressing,
            CompressionOutcome,
        )
        outcome = compress_session_memory(
            manager,
            session_name,
            model_name,
            api_key,
            api_base,
            output_callback=lambda message: _notify("compress", message),
            cancel_event=cancel_event,
            return_outcome=True,
        )
        if not isinstance(outcome, CompressionOutcome):
            outcome = CompressionOutcome("compressed" if outcome else "failed")

        history = manager.get_history_for_llm(session_name)
        estimated_tokens = estimate_tokens(model_name, [system_message] + history)

        # 兜底：如果总结归纳后依然超限，触发物理大文件折叠机制
        if estimated_tokens > window * SUMMARIZE_RATIO:
            manager.prune_history_files(session_name, max_turns=1, retain_recent=4)
            history = manager.get_history_for_llm(session_name)
            estimated_tokens = estimate_tokens(model_name, [system_message] + history)

        # 智能滑窗淘汰（熔断 + 并发 + 最大轮次约束）：
        # 1. 压缩失败，或中止于「一段都没落库」的 timeout / cancelled —— 二者语义等价，
        #    DB 里没有新摘要覆盖待淘汰历史，一律禁止推水位（否则用户按「停止」后
        #    下一轮就会偷偷抹掉 2 轮未摘要的原始对话）。
        # 2. 后台正有该会话压缩在跑（或命中 already_running）时严禁推水位：
        #    evict 是无 CAS 的裸 UPDATE，会打翻后台压缩的乐观锁、令成果全废。
        # 3. conflict_stale 属于「别的线程已摘要成功」，有兜底，允许正常微调淘汰。
        # 4. 其余情况（含 partial_timeout / partial_cancelled 等部分提交中止）最多
        #    淘汰 MAX_EVICT_TURNS 轮做微调。注意部分提交中止时，从已提交水位起
        #    淘汰的是未提交段轮次（无摘要兜底）——受硬上限约束的有损微调，属
        #    有意权衡；超窗兜底由下方 I4 内存级轮次裁剪保证，下一轮压缩会从
        #    已提交水位续推补摘要。
        # 注：熔断阈值保持 1.2*window 是有意的——轻微超窗（0.95~1.2）时留着 2 轮微调
        # 换可用性，比整轮拒绝服务更划算，别顺手收紧成 0.95。
        compress_unprotected = outcome.status == "failed" or outcome.reason in {
            "timeout", "cancelled",
        }
        compress_running = (
            is_session_compressing(manager, session_name)
            or outcome.reason == "already_running"
        )

        if compress_unprotected and estimated_tokens > window * 1.2:
            _notify("compress", "<ansired>⚠️ 历史记忆压缩未能完成（服务商限流或已被取消），当前上下文仍严重超出当前模型窗口。为保护您的历史对话完整，系统已停止自动淘汰历史；本轮请求将在内存中临时按轮次裁剪以保证正常回复。</ansired>")
        elif compress_running:
            _notify("compress", "<ansiyellow>ℹ 后台记忆压缩仍在进行中，本轮请求将在内存中临时按轮次裁剪以适配当前模型窗口。</ansiyellow>")
        else:
            evict_count = 0
            while (
                evict_count < MAX_EVICT_TURNS
                and len(history) > 2
                and estimated_tokens > window * SUMMARIZE_RATIO
            ):
                if not manager.evict_oldest_turn_persistent(session_name):
                    break
                evict_count += 1
                history = manager.get_history_for_llm(session_name)
                estimated_tokens = estimate_tokens(model_name, [system_message] + history)

        # 【I4 请求侧内存级轮次裁剪】：
        # 无论是因为熔断、后台压缩进行中还是淘汰微调后仍超窗，
        # 绝不把超窗的载荷裸发给模型导致 400 报错。
        # 在内存中临时按完整轮次安全剔除最早历史，保证本轮请求正常发出且 DB 零破坏。
        if estimated_tokens > window * SUMMARIZE_RATIO:
            from core.memory import _sanitize_tool_call_pairing
            pruned_count, estimated_tokens = _prune_history_in_memory_to_fit_window(
                history, window, model_name, system_message, estimated_tokens
            )
            if pruned_count > 0:
                history = _sanitize_tool_call_pairing(history)
                estimated_tokens = estimate_tokens(model_name, [system_message] + history)

        if estimated_tokens > window and not _cancelled():
            _notify("compress", "<ansired>⚠️ 当前上下文单条消息本身已超过模型窗口，本轮请求可能被 provider 拒绝；建议精简输入或开启新会话。</ansired>")

    # 更新最新 Token 数供界面显示（与 UI 线程并发读写，必须持锁）
    with token_usage_lock:
        session_token_usage["latest_total"] = estimated_tokens

    # 只在这一层组装当轮的运行上下文（稳定提示词已在估算前组装）。
    # 1) 丢弃历史遗留的所有 system 上下文块（旧格式）：动态块/时间注记/后台汇总
    #    现已内联到 user 消息，稳定提示词每轮现组插到首位，历史里不应再有 system 块
    history = _drop_legacy_context_systems(history)

    runtime = build_runtime_context(model_name, permission_mode, api_base=api_base, turn_source=turn_source)
    dynamic_prompt = build_dynamic_prompt(runtime)
    history.insert(0, system_message)

    # 2) 动态运行上下文块 + 后台汇总 + 时间注记统一内联到最后一条 user 消息末尾。
    #    以前动态块/汇总块是独立 role=system 消息插到 user 之前，多轮对话里会落到
    #    历史中间（前面的 user 之后），违反 provider 的「system 必须在开头」约束。
    time_note = build_time_note(runtime)
    try:
        summary_msg = build_background_task_summary(session_name)
    except Exception:
        logger.warning("后台子代理汇总注入失败，已跳过本轮汇总", exc_info=True)
        summary_msg = ""
    _append_context_suffix_to_last_user(
        history, dynamic_prompt, summary_msg, time_note, manager, session_name
    )

    return history


