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

logger = logging.getLogger(__name__)

# 记忆压缩触发阈值默认值：历史 token 数占上下文窗口的比例超过此值即触发压缩。
# 与 main.py 的 MEMORY_COMPRESS_THRESHOLD 语义一致（0.70 折中值）。
DEFAULT_COMPRESS_THRESHOLD = 0.70

# 兜底系统提示词：提示词文件全部缺失时也不能让整轮对话失败
FALLBACK_SYSTEM_PROMPT = "你叫星瑶，是一位顶尖的代码编程助手。"

# 后台子代理结果注入时，单条结果的最大长度
_BG_RESULT_MAX_CHARS = 1500


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


def load_system_prompt() -> str:
    """三级兜底读取系统提示词：配置路径 → 仓库内 system_prompt.md → 内置字面量。"""
    system_prompt = ""
    try:
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


def build_runtime_context(model_name: str, permission_mode: str):
    """组装本轮的运行上下文（时间 / 计划快照 / 长期记忆 / 技能 / 自定义子 Agent）。"""
    from core.context_builder import RuntimeContext
    from core.skills import get_active_skills_catalog
    from core.subagent_registry import get_custom_subagents_catalog
    from core.plan import get_default_plan_manager
    import core.memory_kairos as memory_kairos

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
        memory=memory_kairos.build_memory_system_section(),
        skills=get_active_skills_catalog() or "无",
        custom_subagents=get_custom_subagents_catalog() or "无",
    )


def _insert_before_last_user(history: list, message: dict) -> None:
    """把 system 消息插到最后一条 user 消息之前；没有 user 结尾则直接追加。"""
    if history and history[-1].get("role") == "user":
        history.insert(len(history) - 1, message)
    else:
        history.append(message)


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
        else:
            # 失败任务：主代理应接手用自身工具完成
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
) -> list:
    """组装一轮对话真正发给大模型的完整消息列表。

    调用前提：用户本轮输入已经写入数据库（manager.add_message）。

    步骤严格保持与历史 TUI 实现一致的顺序：
      1. 清理超长历史文件记录 (prune_history_files)
      2. 取压缩后的 LLM 历史 + 估算 token
      3. 超阈值时逐级升级：智能压缩 → 物理折叠 → 滑窗淘汰
      4. 剔除历史遗留 system 消息
      5. 读取系统提示词（三级兜底）
      6. 组装 RuntimeContext，插入稳定提示词 + 动态上下文
      7. 注入后台子代理结果汇总

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

    try:
        window = int(context_window or 0)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        window = 128000
    limit = window * float(compress_threshold)

    # 【阶段4】后台静默清理超长历史文件记录
    manager.prune_history_files(session_name, max_turns=16, retain_recent=4)

    # 获取专门供给大模型的已压缩截断与记忆挂载历史
    history = manager.get_history_for_llm(session_name)

    # 【全新机制】在向大模型请求前，实时估算当前历史的 Token 数
    estimated_tokens = estimate_tokens(model_name, history)

    if estimated_tokens > limit and not _cancelled():
        # 超过阈值触发智能记忆压缩提炼。
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
        estimated_tokens = estimate_tokens(model_name, history)

        # 兜底：如果总结归纳后依然超限，触发物理大文件折叠机制
        if estimated_tokens > limit:
            manager.prune_history_files(session_name, max_turns=1, retain_recent=4)
            history = manager.get_history_for_llm(session_name)
            estimated_tokens = estimate_tokens(model_name, history)

        # 智能滑窗淘汰：如果仍超限，按完整轮次（user 开头到下一个 user 之前）
        # 为单位从内存中丢弃最早的旧消息，直到满足 Token 安全阈值
        while len(history) > 2 and estimated_tokens > limit:
            if not evict_oldest_turn(history):
                break
            estimated_tokens = estimate_tokens(model_name, history)

    # 更新最新 Token 数供界面显示（与 UI 线程并发读写，必须持锁）
    with token_usage_lock:
        session_token_usage["latest_total"] = estimated_tokens

    # 只在这一层组装一个稳定提示词和一份当前运行上下文。
    from core.context_builder import (
        build_dynamic_prompt,
        build_stable_prompt,
        without_system_messages,
    )
    history = without_system_messages(history)

    system_prompt = load_system_prompt()
    runtime = build_runtime_context(model_name, permission_mode)
    stable_prompt = build_stable_prompt(system_prompt)
    dynamic_prompt = build_dynamic_prompt(runtime)
    history.insert(0, {"role": "system", "content": stable_prompt})
    # 动态上下文独立为第二条 system 消息并靠近当前用户输入，
    # 保持第一条 system 消息内容稳定，提升 provider 侧 prefix caching 命中率。
    _insert_before_last_user(history, {"role": "system", "content": dynamic_prompt})

    # 主代理汇总/兜底机制：提取本会话中所有终态（成功+失败）且尚未消费的后台子代理任务，
    # 注入到 history 中作为 system 消息，让主代理能汇总成功结果并用自身工具接手失败任务。
    try:
        summary_msg = build_background_task_summary(session_name)
        if summary_msg:
            # 插入到用户消息之前，让主代理把汇总上下文当作本轮的额外信息处理
            _insert_before_last_user(history, {"role": "system", "content": summary_msg})
    except Exception:
        # 汇总机制失败不能影响主流程
        pass

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
    from core.tools import write_file, _resolve_workspace_path, _atomic_write_edit_file

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
