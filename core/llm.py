import json
import logging
import os
import random
import re
import subprocess
import threading
import time
import uuid
# Python 3.10 及以下 concurrent.futures.TimeoutError 不是内建 TimeoutError 的别名，
# 权限等待轮询必须同时捕获两者，否则等待期直接崩溃
from concurrent.futures import TimeoutError as _FutureTimeoutError
from copy import deepcopy

# 使用 LiteLLM 本地模型价格表，跳过启动时的远程拉取
# （raw.githubusercontent.com 国内常超时，本地副本无功能影响，且启动更快）
# 必须在 import litellm 之前设置该环境变量才生效
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# 屏蔽 LiteLLM 启动时的调试噪音，但保留 WARNING 及以上的有用告警
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
logging.getLogger("litellm").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# litellm 懒加载：启动性能优化
# ---------------------------------------------------------------------------
# 旧实现：顶部直接 import litellm，导致每次启动都要等 litellm 加载（本地实测 ~1.4s，
#   打包后首次启动 litellm 会让 UI 等待 3-5s 才出现）。
# 新实现：UI 启动路径（main.py import core.llm）不再触发 litellm 加载，
#   只有后台 worker 首次真正调用 LLM 或 estimate_tokens 时才加载。
# 通过 _get_litellm() 单次懒加载 + 缓存，保证 litellm 只在第一次使用时被导入。
_litellm_module = None
_litellm_lock = threading.Lock()


def _get_litellm():
    """懒加载 litellm 模块，首次调用时 import 并执行一次性配置，后续调用直接返回缓存。

    线程安全：多线程并发首次调用时通过 _litellm_lock 保证只 import 一次。
    """
    global _litellm_module
    if _litellm_module is not None:
        return _litellm_module
    with _litellm_lock:
        if _litellm_module is not None:
            return _litellm_module
        import litellm as _litellm
        # 屏蔽启动调试噪音
        _litellm.suppress_debug_info = True
        _litellm_module = _litellm
        return _litellm


def _litellm_completion(**kwargs):
    """litellm.completion 的懒加载包装。"""
    return _get_litellm().completion(**kwargs)


def _litellm_token_counter(model, messages):
    """litellm.token_counter 的懒加载包装。"""
    return _get_litellm().token_counter(model=model, messages=messages)


def _litellm_supports_vision(model):
    """litellm.supports_vision 的懒加载包装。"""
    try:
        return _get_litellm().supports_vision(model=model)
    except Exception:
        return False


def _litellm_context_overflow_error():
    """litellm.ContextWindowExceededError 异常类的懒加载包装。"""
    return _get_litellm().ContextWindowExceededError


# 单次 API 请求超时（秒）：长思考模型的首个 token 可能较慢，不宜过紧
API_REQUEST_TIMEOUT_SECONDS = 90

# 单次 API 请求超时上限（秒）：超时随载荷 token 数放大（大上下文 + 深度思考
# 本来就需要几分钟），但必须有上限，避免挂起的连接无限等待
API_REQUEST_MAX_TIMEOUT_SECONDS = 600

# 单次请求等待反馈：超过阈值后按间隔向 UI 报一次“模型响应中”，
# 避免大载荷请求长时间无反馈被误判为卡死
WAIT_NOTICE_THRESHOLD_SECONDS = 60.0
WAIT_NOTICE_INTERVAL_SECONDS = 15.0

# 工具结果中的错误哨兵前缀（统一在此定义，供结果判断与构造处引用）
TOOL_ERROR_PREFIX = "🚨"
ASK_USER_ERROR_PREFIX = "无法提问："
TOOL_INTERNAL_ERROR_PREFIX = "Tool execution error:"
PERMISSION_DENIED_MESSAGE = f"{TOOL_ERROR_PREFIX} 用户拒绝了执行该操作。"
PERMISSION_CANCELLED_MESSAGE = f"{TOOL_ERROR_PREFIX} 操作已取消。"
PERMISSION_SYSTEM_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 权限询问系统异常，操作未能完成。"
    f"这不是用户拒绝，请稍后重试或检查权限组件状态。"
)
PATH_VALIDATION_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 无法验证路径是否位于当前工作区，已拒绝执行。"
)


class PermissionSystemError(Exception):
    """权限询问流程自身发生异常（如 UI 事件丢失、broker 状态不一致）。

    与"用户拒绝"(False)区分，避免模型把系统故障误解为用户主动拒绝。
    """
    pass

# 🌟 注意这里：我们把四个工具全都引进来了！
from core.tools import read_file, write_file, list_directory, execute_command, search_news, search_code, edit_file, manage_task, run_subagent, glob_files, fetch_url, get_weather, sandbox_bypass, is_outside_workspace
from core.mcp_client import mcp_manager
from core.permission_broker import CANCELLED_RESULT, permission_broker
from core.command_policy import (
    _session_rule_is_exact,
    command_matches_session_rule,
    session_rule_for_command,
)
from core.reasoning_stream import ReasoningStreamParser, split_reasoning_text
from core.tool_presenter import summarize_tool_finished, summarize_tool_started
from core.turn_events import TurnEventFactory
import core.app_context as app_context

# 会话授权白名单（当前会话生命周期内有效，切换会话时自动清理）
session_allowed_commands: set[tuple[str, ...]] = set()  # 结构化命令规则
session_allowed_write_files = set()   # 放行的写入文件绝对路径
session_allowed_edit_files = set()    # 放行的修改文件绝对路径
_last_tracked_session = None
_raw_permission_lock = threading.Lock()
# 保护会话放行白名单与 _last_tracked_session 的跨线程读写
_session_whitelist_lock = threading.Lock()
_plan_turn_lock = threading.Lock()
_plan_turn_started_at = {}

# GUI 模式权限回调：由 gui_bridge.py 在启动时注册
# 签名: (function_name: str, arguments: dict) -> bool
_gui_permission_callback = None
_PLAN_BYPASS_ERROR_PREFIX = "计划已创建：工具"
_PLAN_ROOT_CONTROL_TOOLS = frozenset({
    "plan_write",
    "plan_update",
    "plan_read",
    "plan_retry",
    "run_subagent",
    "ask_user",
})
# 只读探索类工具在计划模式下仍允许主 Agent 直接使用，
# 便于主 Agent 验证子代理产出、读取上下文，避免事事都开子代理导致超时循环。
_PLAN_READ_ONLY_TOOLS = frozenset({
    "read_file",
    "list_directory",
    "search_code",
    "find_definition",
    "glob_files",
    "fetch_url",
    "search_news",
    "get_weather",
    # 后台任务监控必须放行：模型用 run_subagent(run_in_background=True) 启动
    # 子代理后需要 manage_task(list/status) 轮询进度，拦截会让主 Agent 对自己
    # 启动的后台任务完全失明（kill 操作有独立的用户确认，不受此白名单影响）
    "manage_task",
})
_PLAN_BOUND_ORCHESTRATORS = frozenset({"run_subagent", "ask_user"})


def _mark_plan_turn(turn_id):
    if not turn_id:
        return
    now = time.monotonic()
    with _plan_turn_lock:
        _plan_turn_started_at[str(turn_id)] = now
        cutoff = now - 3600
        for stale_id, started_at in list(_plan_turn_started_at.items()):
            if started_at < cutoff:
                _plan_turn_started_at.pop(stale_id, None)


def _is_plan_turn(turn_id):
    if not turn_id:
        return False
    with _plan_turn_lock:
        return str(turn_id) in _plan_turn_started_at


def _has_pending_plan(session_id):
    if not session_id:
        return False
    from core.plan import get_default_plan_manager

    plan = get_default_plan_manager()
    if plan.session_id != str(session_id):
        return False
    return any(
        item.get("status") in {"pending", "in_progress"}
        for item in plan.snapshot()
    )

def check_session_reset():
    global _last_tracked_session
    with _session_whitelist_lock:
        current = app_context.get_current_session()
        if current and _last_tracked_session != current:
            _last_tracked_session = current
            session_allowed_commands.clear()
            session_allowed_write_files.clear()
            session_allowed_edit_files.clear()


def _session_prefix_for_command(cmd):
    """兼容历史扩展名称，委托给结构化命令授权策略。"""
    return session_rule_for_command(cmd)


def _command_matches_prefix(cmd, prefix):
    """兼容历史扩展名称，使用完整 token 元素匹配。"""
    return command_matches_session_rule(cmd, prefix)


def _format_session_command_rule(rule):
    command = subprocess.list2cmdline(rule)
    if _session_rule_is_exact(rule):
        return f"精确规则：{command}"
    return f"子命令规则：{command}（允许后续参数）"

def safe_print(formatted_text):
    """线程安全地在 TUI 终端输出格式化文本，避免破坏屏幕布局"""
    from prompt_toolkit import print_formatted_text
    _app = app_context.get_app_instance()
    if _app and _app.loop:
        _app.loop.call_soon_threadsafe(lambda: print_formatted_text(formatted_text))
    else:
        try:
            print_formatted_text(formatted_text)
        except Exception:
            pass

def ask_in_raw_terminal(function_name, arguments):
    """
    纯文本原始终端兜底：只在确实没有活动 TUI App 时才被调用（例如管道重定向或早期初始化时）。
    不使用任何 ANSI 转义序列或 msvcrt 菜单，直接用 print/input 避免污染 TUI 屏幕。
    """
    mode_display = {"confirm": "每次确认", "plan": "计划确认"}
    mode_tip = mode_display.get(app_context.get_permission_mode(), "确认")
    
    print("")  # 空行分隔
    if function_name == "write_file":
        file_path = arguments.get("file_path", "未知文件")
        content = arguments.get("content", "")
        print(f"[写入文件] 星瑶申请写入文件: {file_path}")
        lines = content.splitlines()
        if lines:
            preview = "\n".join(lines[:5])
            if len(lines) > 5:
                preview += f"\n   ... (共 {len(lines)} 行)"
            print(f"   [预览]:\n   {preview}")
    elif function_name == "edit_file":
        file_path = arguments.get("file_path", "未知文件")
        target = arguments.get("target_content", "")
        repl = arguments.get("replacement_content", "")
        print(f"[修改文件] 星瑶申请修改文件: {file_path}")
        print(f"   [-] 计划删除: {target.strip()[:100]}")
        print(f"   [+] 计划替换: {repl.strip()[:100]}")
    elif function_name == "execute_command":
        cmd = arguments.get("command", "")
        bg = arguments.get("run_in_background", False)
        bg_str = " (后台运行)" if bg else ""
        print(f"[执行命令] 星瑶申请执行命令{bg_str}: {cmd}")
    
    # 纯文本 input 模式，不含任何 ANSI 序列
    try:
        if function_name == "sandbox_outside_access":
            # 沙盒外路径访问必须每次确认，不提供 always 会话放行
            print("请输入 [y]es 同意本次, [n]o 拒绝 (沙盒外访问每次都需要确认)")
            res = input(f"批准该操作？(y/n, 模式:{mode_tip}, 默认 y): ").strip().lower()
        else:
            print("请输入 [y]es 同意本次, [n]o 拒绝, [a]lways 允许此类操作 (仅当前会话放行)")
            res = input(f"批准该操作？(y/n/a, 模式:{mode_tip}, 默认 y): ").strip().lower()
        
        if res in ("n", "no"):
            return False
            
        if res in ("a", "always", "y*"):
            if function_name == "execute_command":
                cmd = arguments.get("command", "")
                prefix = _session_prefix_for_command(cmd)
                if prefix is None:
                    print("[内联执行命令不做会话前缀放行，仅本次通过]")
                else:
                    with _session_whitelist_lock:
                        session_allowed_commands.add(prefix)
                    print(f"[当前会话已放行] {_format_session_command_rule(prefix)}")
            elif function_name == "write_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    with _session_whitelist_lock:
                        session_allowed_write_files.add(file_path)
                    print(f"[当前会话已放行写入]：{file_path}")
            elif function_name == "edit_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    with _session_whitelist_lock:
                        session_allowed_edit_files.add(file_path)
                    print(f"[当前会话已放行修改]：{file_path}")
            elif function_name == "sandbox_outside_access":
                # 沙盒外访问不支持会话放行，always 仅本次通过
                print("[沙盒外访问每次都需要确认，仅本次通过]")
            return True
            
        return res in ("", "y", "yes")
    except (KeyboardInterrupt, EOFError):
        print("\n[操作已取消]")
        return False

def check_permission_and_ask(
    function_name,
    arguments,
    event_callback=None,
    permission_owner=None,
    parent_turn_id=None,
    permission_source="主代理",
    cancel_event=None,
):
    # 0. 自动同步和重置会话级别的白名单缓存
    check_session_reset()

    # 1. 检查当前会话中是否已被用户“永久允许”
    if function_name == "execute_command":
        cmd = arguments.get("command", "")
        with _session_whitelist_lock:
            allowed = any(
                _command_matches_prefix(cmd, prefix)
                for prefix in session_allowed_commands
            )
        if allowed:
            if event_callback is None:
                from prompt_toolkit.formatted_text import HTML
                safe_print(HTML(f"<ansigreen>当前会话自动放行命令：{cmd}</ansigreen>"))
            else:
                _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
            return True
    elif function_name == "write_file":
        file_path = arguments.get("file_path", "")
        with _session_whitelist_lock:
            file_allowed = file_path in session_allowed_write_files
        if file_allowed:
            if event_callback is None:
                from prompt_toolkit.formatted_text import HTML
                safe_print(HTML(f"<ansigreen>当前会话自动放行写入：{file_path}</ansigreen>"))
            else:
                _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
            return True
    elif function_name == "edit_file":
        file_path = arguments.get("file_path", "")
        with _session_whitelist_lock:
            edit_allowed = file_path in session_allowed_edit_files
        if edit_allowed:
            if event_callback is None:
                from prompt_toolkit.formatted_text import HTML
                safe_print(HTML(f"<ansigreen>当前会话自动放行修改：{file_path}</ansigreen>"))
            else:
                _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
            return True

    # 2. 如果权限是完全放行，直接通过
    #    但 sandbox_outside_access（沙盒外路径访问）不受 full 模式自动放行，
    #    必须每次询问用户，确保沙盒外操作始终受控。
    if app_context.get_permission_mode() == "full" and function_name != "sandbox_outside_access":
        if event_callback is not None:
            _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
        return True

    # 2.5 auto（自动接受编辑）模式：只自动放行文件写入/修改，
    #     execute_command 等有副作用的操作仍然必须逐次确认。
    if app_context.get_permission_mode() == "auto" and function_name in ("write_file", "edit_file"):
        if event_callback is not None:
            _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
        return True

    # 3. 如果是计划模式且本轮对话的计划已经获得批准，直接通过
    #    但 sandbox_outside_access 不受计划批准自动放行，必须每次询问用户。
    if (
        app_context.get_permission_mode() == "plan"
        and app_context.get_plan_approved()
        and function_name != "sandbox_outside_access"
    ):
        if event_callback is not None:
            _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
        return True

    _notify_event(event_callback, "permission_requested", {
        "function_name": function_name,
        "arguments": dict(arguments),
        "source": permission_source,
    })

    # GUI 模式：使用已注册的 GUI 权限回调，避免阻塞在 input()
    _gui_cb = app_context.get_permission_callback()
    if _gui_cb is not None:
        return _gui_cb(function_name, arguments)

    # TUI 模式：通过 prompt_toolkit app 或终端 input()
    # GUI 模式没有 prompt_toolkit App，但已在 permission_broker 上注册了处理器，
    # 必须直接走下面的 broker 路径，绝不能退回阻塞式 input()。
    _app = app_context.get_app_instance()
    if (not _app or not _app.loop) and app_context.get_ui_flavor() != "gui":
        with _raw_permission_lock:
            return ask_in_raw_terminal(function_name, arguments)

    # owner/turn_id 统一来源：优先显式传入，turn 兜底 owner，线程名最后兜底
    owner_id = str(permission_owner or parent_turn_id or threading.current_thread().name)
    turn_id = str(parent_turn_id or owner_id)
    # 沙盒外路径访问必须每次确认，不提供“始终放行”选项
    _options = None
    if function_name == "sandbox_outside_access":
        _options = (
            "Yes, 同意本次操作 (y)",
            "No, 拒绝本次操作 (n)",
        )
    request = permission_broker.enqueue(
        function_name,
        arguments,
        owner_id,
        turn_id,
        permission_source,
        options=_options,
    )

    try:
        # 带短超时的轮询等待：取消信号到达时立即跳出，避免线程永久挂起；
        # 先查 future 再判 cancel，避免用户刚点“同意”被竞态误判为取消
        while True:
            if request.future.done():
                choice_idx = request.future.result()
                break
            if cancel_event is not None and cancel_event.is_set():
                permission_broker.cancel_owner(owner_id)
                choice_idx = CANCELLED_RESULT
                break
            try:
                choice_idx = request.future.result(timeout=0.1)
                break
            except (TimeoutError, _FutureTimeoutError):
                # Python 3.10 及以下 concurrent.futures.TimeoutError 不是内建
                # TimeoutError 的别名，只 except TimeoutError 会漏接，
                # 导致轮询循环直接抛 PermissionSystemError（等待期必崩）
                continue

        from prompt_toolkit.formatted_text import ANSI

        if choice_idx == CANCELLED_RESULT:
            if event_callback is None:
                safe_print(ANSI("   🛑 操作已取消"))
            else:
                _notify_event(event_callback, "permission_resolved", {
                    "approved": False,
                    "cancelled": True,
                })
            return False

        choices_map = {
            0: "\033[92m✓ 已同意本次操作\033[0m",
            1: "\033[91m✗ 已拒绝本次操作\033[0m",
            2: "\033[96m✨ 已选择始终放行此类操作 (当前会话)\033[0m"
        }

        if event_callback is None:
            if function_name == "execute_command":
                cmd = arguments.get("command", "")
                safe_print(ANSI(f"💻 星瑶申请执行命令: {cmd}"))
            elif function_name == "write_file":
                file_path = arguments.get("file_path", "")
                safe_print(ANSI(f"✍️ 星瑶申请写入文件: {file_path}"))
            elif function_name == "edit_file":
                file_path = arguments.get("file_path", "")
                safe_print(ANSI(f"📝 星瑶申请修改文件: {file_path}"))
            safe_print(ANSI(f"   👉 确认结果: {choices_map[choice_idx]}"))
        else:
            _notify_event(event_callback, "permission_resolved", {
                "approved": choice_idx != 1,
                "always": choice_idx == 2,
            })

        if choice_idx == 1:
            return False

        if choice_idx == 2:
            if function_name == "execute_command":
                cmd = arguments.get("command", "")
                prefix = _session_prefix_for_command(cmd)
                if prefix is None:
                    # 与 raw 终端路径行为对齐：内联执行命令不放行也不静默，明确提示
                    if event_callback is None:
                        safe_print(ANSI("   ⚠️ [内联执行命令不做会话前缀放行，仅本次通过]"))
                    else:
                        _notify_event(event_callback, "status", {
                            "message": "内联执行命令不做会话前缀放行，仅本次通过。",
                        })
                else:
                    with _session_whitelist_lock:
                        session_allowed_commands.add(prefix)
                    if event_callback is None:
                        safe_print(ANSI(f"   ✨ [当前会话已放行] {_format_session_command_rule(prefix)}"))
            elif function_name == "write_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    with _session_whitelist_lock:
                        session_allowed_write_files.add(file_path)
                    if event_callback is None:
                        safe_print(ANSI(f"   ✨ [当前会话已放行写入]：{file_path}"))
            elif function_name == "edit_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    with _session_whitelist_lock:
                        session_allowed_edit_files.add(file_path)
                    if event_callback is None:
                        safe_print(ANSI(f"   ✨ [当前会话已放行修改]：{file_path}"))
            # sandbox_outside_access 不提供“始终放行”选项（见 enqueue 处），
            # 沙盒外路径每次访问都必须逐次确认。
            return True

        approved = (choice_idx in (0, 2))
        # plan 模式仅在用户明确选择“始终放行”时才批准整轮计划，
        # “同意本次”(0) 只放行当前这一次操作
        if choice_idx == 2 and app_context.get_permission_mode() == "plan":
            app_context.set_plan_approved(True)
        return approved
    except Exception as e:
        _notify_event(event_callback, "error", {
            "message": f"权限询问交互发生异常: {str(e)}",
        })
        # 系统异常不应伪装成"用户拒绝"（False 会被模型理解为用户主动拒绝）。
        # 抛出 PermissionSystemError，由调用方返回专门的系统异常提示。
        permission_broker.cancel_owner(owner_id)
        raise PermissionSystemError(str(e)) from e
    finally:
        # 异常退出（含 KeyboardInterrupt 等 BaseException）时回收已入队请求，
        # 否则 broker 串行化会让后续所有权限提示永久排队
        if not request.future.done():
            permission_broker.cancel_owner(owner_id)

# 全局变量：分离“本次窗口占比”和“整体累积消耗”
session_token_usage = {
    # 累积消耗（用来算钱）
    "cumulative_total": 0,
    "cumulative_prompt": 0,
    "cumulative_completion": 0,
    "cumulative_cached": 0,
    
    # 窗口占用（用来防止爆 128k 上下文）
    "latest_total": 0,
    "latest_prompt": 0,
    "latest_cached": 0,
    
    "max_window": 128000  # 默认最大上下文窗口配置
}
# session_token_usage 会被后台 worker 线程与 UI 线程同时读写，所有读写必须走锁
token_usage_lock = threading.Lock()

# The registry is the canonical capability metadata source.
from core.tool_registry import build_default_registry
tool_registry = build_default_registry()


_mcp_first_wait_done = False


def get_tools_schema():
    """每次构建请求时新鲜获取工具 schema（registry 静态 + MCP 动态）。

    不做模块级一次性快照：启动时 MCP 连接失败不会导致本进程永久缺失
    MCP 工具 schema，且 mcp_client 为惰性连接，import 时不触发网络。

    进程内首次调用时同步等待 MCP 连接就绪（有界超时，空配置立即返回）：
    否则 MCP schema 会在第 N 轮突然出现在 tools 列表里，DeepSeek/Kimi
    前缀缓存从头部整体失效。后续调用恢复非阻塞，避免每轮都承担连接延迟。
    """
    global _mcp_first_wait_done
    if not _mcp_first_wait_done:
        _mcp_first_wait_done = True
        try:
            mcp_manager._ensure_connected()
        except Exception:
            pass
    return tool_registry.api_schemas() + mcp_manager.get_schemas()


def completion(**kwargs):
    """litellm.completion 的模块级导出的包装函数（可被测试 mocker.patch 拦截）。"""
    return _litellm_completion(**kwargs)


def __getattr__(name):
    # 兼容别名：惰性计算，避免 import 时触发 MCP 连接
    if name == "TOOLS_SCHEMA":
        return get_tools_schema()
    if name == "litellm":
        return _get_litellm()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# estimate_tokens 结果缓存：tiktoken 全量编码整个历史相当慢（长会话可达秒级），
# 而 UI 状态栏、压缩判断等会在内容未变时反复调用。用 (模型, 消息数, 总字符数)
# 作为签名，未变化时直接复用上次结果。
_token_estimate_cache = {"sig": None, "value": 0}


def _fold_old_media_for_estimate(messages):
    """估算前把历史图片折叠为占位符（与请求时 _strip_media_and_merge 的规则一致）。

    base64 原图动辄几十万字符，按字符数估算会虚高几个数量级，导致携带大图片的
    会话反复误触发记忆压缩。仅最近一条带图用户消息保留原样（交给 litellm 按
    图片实际规格估算），更早的一律折叠为短占位符。
    """
    last_media_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") in ("image", "image_url")
            for item in content
        ):
            last_media_idx = idx
        break
    folded = []
    for idx, msg in enumerate(messages):
        content = msg.get("content")
        if (
            idx != last_media_idx
            and isinstance(content, list)
            and any(
                isinstance(item, dict) and item.get("type") in ("image", "image_url")
                for item in content
            )
        ):
            folded.append({"role": msg.get("role"), "content": _flatten_media_content(content)})
        else:
            folded.append(msg)
    return folded


def estimate_tokens(model_name, messages):
    """本地离线快速估算 Token 数，防止超载崩溃（带签名缓存）"""
    if not messages:
        return 0
    try:
        total_len = sum(len(str(m.get("content") or "")) for m in messages)
        sig = (str(model_name), len(messages), total_len)
    except Exception:
        sig = None
    if sig is not None and _token_estimate_cache.get("sig") == sig:
        return _token_estimate_cache["value"]
    try:
        # 历史图片先折叠再计数：base64 字符量与图片真实 token 消耗无关
        value = _litellm_token_counter(model_name, _fold_old_media_for_estimate(messages))
    except Exception:
        # 如果模型不支持或报错，采用约 2 字符 / Token 的经验值进行兜底估算
        value = len(str(_fold_old_media_for_estimate(messages))) // 2
    if sig is not None:
        _token_estimate_cache["sig"] = sig
        _token_estimate_cache["value"] = value
    return value

def _yield_content(text):
    """假流式生成器：把完整文本逐字符 yield，模拟打字机效果"""
    for char in text:
        yield char


def _is_meaningful_assistant_content(text):
    return bool(re.sub(r"[\s`*_#>~\-\[\](){}|.,!?，。！？:：;；/\\]+", "", text or ""))


def _read_text_with_encoding_fallback(file_path):
    """读取文本文件：优先 UTF-8，失败回退 GBK（与 core.tools.edit_file 解码策略一致）。"""
    with open(file_path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def chat_with_model(model_name, api_key, messages, api_base=None, stream=False):
    """
    大模型通信网关：支持流式（stream=True）与非流式（stream=False，后台 Agent 自动消化）
    """
    gen = _chat_with_model_generator(
        model_name,
        api_key,
        messages,
        api_base=api_base,
        stream=False,
    )
    if stream:
        return gen
    else:
        # 非流式模式下，由包装函数在后台默默消耗生成器，完成所有工具调用与轮次迭代
        for _ in gen:
            pass
        if messages and messages[-1]["role"] == "assistant":
            # 尾部 assistant 消息 content 可能为 None（例如仅工具调用轮）：
            # 为保持兼容，统一归一化为空字符串返回，不向调用方暴露 None
            return messages[-1].get("content", "") or ""
        return ""


def chat_with_model_events(
    model_name,
    api_key,
    messages,
    api_base=None,
    persist=True,
    turn_id=None,
    cancel_event=None,
    allow_subagents=True,
    allowed_tools=None,
    consume_background_notifications=True,
    parent_session=None,
    parent_turn_id=None,
    permission_owner=None,
    permission_source="主代理",
):
    """Yield ordered :class:`TurnEvent` snapshots for one model turn.

    The legacy ``chat_with_model`` API remains text-oriented. This adapter is
    the event-oriented entry point used by the TUI and is deliberately kept
    separate while the existing tool loop is migrated incrementally.
    """
    if persist and not parent_session:
        raise ValueError("parent_session is required when persist=True")
    resolved_turn_id = turn_id or f"turn-{uuid.uuid4().hex[:8]}"
    resolved_parent_turn_id = parent_turn_id or resolved_turn_id
    factory = TurnEventFactory(resolved_turn_id)
    pending_events = []
    pending_lock = threading.Lock()
    parser = ReasoningStreamParser()
    thinking_open = False
    turn_failed = False

    def emit(kind, payload):
        nonlocal thinking_open
        with pending_lock:
            # 生成器侧的任何回调事件都发生在已 yield 文本之后：
            # 若 parser 已悄悄越过 </think>（闭合标签本身不产生文本 part），
            # 这里统一补发 thinking_finished，保证每个思考块只收到一组事件
            if thinking_open:
                pending_events.append(factory.emit("thinking_finished", {}))
                thinking_open = False
            pending_events.append(factory.emit(kind, payload))

    def drain_public_events():
        nonlocal turn_failed
        with pending_lock:
            ready = pending_events[:]
            del pending_events[:]
        if any(event.kind == "turn_error" for event in ready):
            turn_failed = True
        return ready

    def events_for_parts(parts):
        nonlocal thinking_open
        ready = []
        for part in parts:
            if part.kind == "thinking":
                if not thinking_open:
                    ready.append(factory.emit("thinking_started", {}))
                    thinking_open = True
                if part.text:
                    ready.append(factory.emit("thinking_delta", {"text": part.text}))
                continue

            if thinking_open:
                ready.append(factory.emit("thinking_finished", {}))
                thinking_open = False
            if part.text:
                ready.append(factory.emit("assistant_delta", {"text": part.text}))
        return ready

    generator = _chat_with_model_generator(
        model_name,
        api_key,
        messages,
        api_base=api_base,
        stream=persist,
        event_callback=emit,
        cancel_event=cancel_event,
        allow_subagents=allow_subagents,
        allowed_tools=allowed_tools,
        consume_background_notifications=consume_background_notifications,
        parent_session=parent_session,
        parent_turn_id=resolved_parent_turn_id,
        permission_owner=permission_owner or resolved_parent_turn_id,
        permission_source=permission_source,
    )
    try:
        for chunk in generator:
            yield from drain_public_events()
            if chunk:
                yield from events_for_parts(parser.feed(chunk))

        yield from drain_public_events()
        yield from events_for_parts(parser.finish())
        if thinking_open:
            yield factory.emit("thinking_finished", {})
            thinking_open = False
        # 异常结束的回合只发 turn_error，不再发 turn_finished，避免 UI 把失败当成功
        if not turn_failed:
            yield factory.emit("turn_finished", {})
    except KeyboardInterrupt:
        yield from drain_public_events()
        yield factory.emit("turn_cancelled", {})
        raise
    except Exception as exc:
        # 生成器前置区（记忆注入/通知 drain/请求构造）抛出的异常会穿透内部
        # try 直接传播到这里：补发 turn_error，保证调用方能感知回合失败
        yield from drain_public_events()
        yield factory.emit("turn_error", {"message": str(exc)})
        raise

# 支持原生推理字段回传的 provider 白名单（按 litellm model 前缀匹配）
_REASONING_FIELD_PROVIDERS = ("anthropic", "deepseek", "moonshot")

_KIMI_NAME_RE = re.compile(r"(?:^|[-_/])k(?:2|3)(?:[._-]\d+)?(?:$|[-_])")


def _is_kimi_family(model_name, api_base=""):
    """判断是否 Kimi/Moonshot 系模型（模型名优先，api_base 兜底）。

    经销商/聚合网关（api.scnet.cn、OpenRouter 等）的 base_url 不含
    moonshot 字样，必须靠模型名识别，否则所有 Kimi 深度适配全部失效。
    """
    m = str(model_name or "").split("/")[-1].lower()
    if "kimi" in m or "moonshot" in m:
        return True
    if _KIMI_NAME_RE.search(m):
        return True
    b = str(api_base or "").lower()
    return "moonshot" in b or "kimi" in b


def _supports_reasoning_fields(model_name):
    prefix = str(model_name or "").split("/")[0].lower()
    if prefix in _REASONING_FIELD_PROVIDERS:
        return True
    # Kimi 推理模型（含经销商 openai/ 路由）：多轮工具调用要求 assistant
    # 消息保留 reasoning_content，剥掉会导致 400 或推理链断裂
    return _is_kimi_family(model_name)


# 支持 prompt caching 的 provider 白名单（按 litellm model 前缀匹配）
# 仅 Anthropic 需要显式 cache_control 标记：
#   - anthropic: 需显式标记，命中后缓存 token 按 1/10 计费
#   - deepseek/openai: 自动缓存命中，无需标记；显式标记反而可能被某些严格校验的
#     OpenAI 兼容网关（如部分国产中转）拒绝为 400。保守起见不启用。
_CACHE_CONTROL_PROVIDERS = ("anthropic",)


def _supports_cache_control(model_name):
    """判断 provider 是否支持 cache_control 字段。

    保守策略：仅对 Anthropic 启用。其他 provider（含 OpenAI 兼容端点）自动缓存命中，
    无需显式标记；显式标记反而可能被严格校验的网关拒绝为 400。
    """
    prefix = str(model_name or "").split("/")[0].lower()
    return prefix in _CACHE_CONTROL_PROVIDERS


def _apply_cache_control(messages, model_name):
    """给消息列表加 cache_control 标记，启用 provider 侧 prompt caching。

    标记策略：
    - system 消息：必缓存（固定前缀，单次会话内不变）
    - 最后一条 assistant 消息：设置缓存边界。
      选择 assistant 而非倒数第 2 条，是因为工具轮次中 messages[-2] 可能是 tool 结果
      而非稳定前缀（多 tool 结果会让断点每轮移动，二级缓存几乎永远 miss）。
      最后一条 assistant 消息是上一轮的回复，在当前轮 tool 调用期间保持稳定，
      provider 可以缓存到该位置，下一轮 tool 结果追加时前缀稳定命中。

    Anthropic 限制：单次请求最多 4 个 cache_control 标记。
    本策略仅用 2 个，远低于上限，安全。

    本函数原地修改并返回 messages；调用方已通过 _strip_media_and_merge 处理过副本，
    不会污染 messages 列表本身。
    """
    if not _supports_cache_control(model_name):
        return messages
    if not messages:
        return messages

    # 清除已有标记，避免累积（_strip_media_and_merge 不传 cache_control，但兜底防御）
    for msg in messages:
        if isinstance(msg, dict):
            msg.pop("cache_control", None)

    # 1. 标记 system 消息（如果有）
    if messages[0].get("role") == "system":
        messages[0]["cache_control"] = {"type": "ephemeral"}

    # 2. 标记最后一条 assistant 消息（缓存边界）
    #    遍历从后往前找第一条 role=="assistant" 的消息。
    #    如果最后一条已经是 assistant（无 tool 调用），就标它；
    #    如果末尾是 tool 结果，就标它们之前的 assistant（更稳定）。
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            messages[i]["cache_control"] = {"type": "ephemeral"}
            break

    return messages


# _supports_vision 结果缓存（30s TTL）：该函数在工具循环里每轮都会被调用
# （最多 100 次/用户轮），底层的 litellm.supports_vision 查表并不便宜。
_vision_support_cache = {}


def _supports_vision(model_name):
    """判断模型是否支持图像输入。

    判定顺序：
    1. 模型配置中的 supports_vision 显式标注（config.json 的 models 条目，可选）
    2. LiteLLM 内置模型信息（知名模型）
    3. 未知模型默认按不支持处理，避免把 image_url 负载发给纯文本模型触发
       "Model only support text input" 等 400 错误
    """
    if not model_name:
        return False
    cached = _vision_support_cache.get(model_name)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    result = False
    try:
        from core.model_manager import load_config
        for provider in load_config().get("providers", []):
            for model in provider.get("models", []):
                if model.get("litellm_model") == model_name and "supports_vision" in model:
                    result = bool(model["supports_vision"])
                    _vision_support_cache[model_name] = (result, time.monotonic() + 30)
                    return result
    except Exception:
        pass
    try:
        if _litellm_supports_vision(model_name):
            result = True
    except Exception:
        pass
    _vision_support_cache[model_name] = (result, time.monotonic() + 30)
    return result


def _flatten_media_content(content_list):
    """把多模态 list 内容折叠为纯文本：图片用短占位符，文档也折叠为占位说明，绝不内联原始 base64 数据。"""
    text_parts = []
    for item in content_list:
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "text":
                text_parts.append(item.get("text", ""))
            elif item_type in ("image", "image_url"):
                text_parts.append("[图片]")
            elif item_type == "document":
                text_parts.append("[文档]")
            else:
                text_parts.append(str(item)[:200])
        else:
            text_parts.append(str(item))
    return " ".join(text_parts)


def _strip_media_and_merge(messages, model_name=None):
    """
    1. 视觉输入处理：支持视觉的模型保留最近一条消息中的图片/视频（多模态负载原样透传），
       更早的历史图片一律折叠为占位符，避免每轮重复发送烧 Token；
       不支持视觉的模型把图片折叠为占位符（绝不把 base64 当文本发给模型）。
       文档/文本文件已在 input_parser 中转为普通文本，此处不视为视觉输入。
    2. 合并连续相同 role 的消息，满足 LiteLLM / Anthropic 对 user/assistant 严格轮流的要求
    3. reasoning_content / thinking_blocks 仅对支持的 provider 回传，避免其他 provider 报 400；
       且仅随带 tool_calls 的消息回传（签名思考链要求），无工具调用的纯思考消息一律剥掉

    返回 (fixed_messages, folded_media_count, keep_media)。
    """
    keep_reasoning_fields = _supports_reasoning_fields(model_name)
    keep_media = _supports_vision(model_name)

    # 仅保留最近一条用户消息中的视觉输入（图片/视频），
    # 更早的历史图片全部折叠为占位符，避免重复发送、浪费 Token。
    last_media_idx = -1
    if keep_media:
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") in ("image", "image_url")
                for item in content
            ):
                last_media_idx = idx
            break

    folded_count = 0
    cleaned = []
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, list):
            if idx == last_media_idx:
                # 多模态负载原样保留，模型按 image_url 接收图片
                cleaned_msg = {"role": role, "content": content}
            else:
                cleaned_msg = {"role": role, "content": _flatten_media_content(content)}
                folded_count += sum(
                    1
                    for item in content
                    if isinstance(item, dict) and item.get("type") in ("image", "image_url")
                )
        else:
            cleaned_msg = {"role": role, "content": str(content) if content is not None else ""}

        # 保留工具链和 provider 原生推理字段，避免工具轮次破坏签名思考块。
        if "tool_calls" in msg and msg["tool_calls"]:
            cleaned_msg["tool_calls"] = msg["tool_calls"]
        if "tool_call_id" in msg:
            cleaned_msg["tool_call_id"] = msg["tool_call_id"]
        if "name" in msg:
            cleaned_msg["name"] = msg["name"]
        # 死思考不回传：reasoning/thinking 字段仅对带 tool_calls 的 assistant
        # 消息有协议价值（Kimi/Anthropic 的签名思考链要求）。无工具调用的纯
        # 思考消息（常见于模型只输出 reasoning 未生成正文的兜底轮）保留它，
        # 会让后续每一轮请求都重复上传这段废推理（可能长达数万字符），纯烧 Token。
        has_tool_calls = bool(msg.get("tool_calls"))
        if keep_reasoning_fields and has_tool_calls and msg.get("reasoning_content") is not None:
            cleaned_msg["reasoning_content"] = msg["reasoning_content"]
        if keep_reasoning_fields and has_tool_calls and msg.get("thinking_blocks") is not None:
            cleaned_msg["thinking_blocks"] = deepcopy(msg["thinking_blocks"])

        # 净化 assistant 消息：若 content 包含 protocol sanitised 占位符且有 tool_calls/reasoning，将 content 重置为空字符串
        if role == "assistant" and isinstance(cleaned_msg.get("content"), str):
            if "[System: Empty message content sanitised to satisfy protocol]" in cleaned_msg["content"]:
                cleaned_msg["content"] = ""

        # 过滤完全没有内容的空白 assistant 消息（无 content, 无 tool_calls, 无 reasoning）
        if role == "assistant":
            has_text = bool(cleaned_msg.get("content"))
            has_tools = bool(cleaned_msg.get("tool_calls"))
            has_reasoning = bool(cleaned_msg.get("reasoning_content") or cleaned_msg.get("thinking_blocks"))
            if not (has_text or has_tools or has_reasoning):
                continue

        cleaned.append(cleaned_msg)
        
    # 合并同角色消息 (注意：不要合并有 tool_calls 或 role 是 tool/system 的消息)
    fixed = []
    if cleaned:
        fixed.append(cleaned[0])
        for msg in cleaned[1:]:
            can_merge = (
                msg["role"] == fixed[-1]["role"] and
                msg["role"] not in ("tool", "system") and
                "tool_calls" not in msg and
                "tool_calls" not in fixed[-1] and
                "reasoning_content" not in msg and
                "reasoning_content" not in fixed[-1] and
                "thinking_blocks" not in msg and
                "thinking_blocks" not in fixed[-1] and
                # 多模态 list 内容不参与文本合并
                not isinstance(msg["content"], list) and
                not isinstance(fixed[-1]["content"], list)
            )
            if can_merge:
                fixed[-1]["content"] = (fixed[-1]["content"] or "") + "\n" + (msg["content"] or "")
            else:
                fixed.append(msg)
    return fixed, folded_count, keep_media


def _cache_affinity_session_id():
    """返回 Kimi prompt 缓存亲和使用的会话级稳定 ID。

    Moonshot 后端是多集群架构，请求带 prompt_cache_key（OpenAI 路由）或
    metadata.user_id（Anthropic 路由）时，网关会把同 ID 的请求优先路由到
    持有其前缀 KV cache 的集群，显著提高缓存命中率（命中部分自动折扣计费）。
    官方建议该值在会话生命周期内保持稳定，会话恢复后沿用原值——正好对应
    app_context 的 current_session；取不到时退化为进程级随机 ID（保底进程内一致）。
    """
    global _process_cache_key_fallback
    try:
        sid = str(app_context.get_current_session() or "").strip()
        if sid:
            return sid
    except Exception:
        pass
    if not _process_cache_key_fallback:
        _process_cache_key_fallback = f"ycode-{uuid.uuid4().hex[:12]}"
    return _process_cache_key_fallback


_process_cache_key_fallback = None

_REASONING_EFFORT_LEVELS = ("low", "high", "max")


def _attach_kimi_request_extras(result, model_name, model_cfg):
    """给 Kimi 系模型的请求参数附加缓存亲和与推理强度配置（原地修改 result）。

    - 缓存亲和：OpenAI 兼容路由传 prompt_cache_key，Anthropic 路由传 metadata.user_id。
    - reasoning_effort：K3 官方推理强度档位（low/high/max，默认 max），仅 OpenAI
      兼容路由支持；Anthropic 路由的推理强度由 thinking_budget 控制，不注入。
    """
    anthropic_route = str(model_name or "").split("/")[0].lower() == "anthropic"

    session_id = _cache_affinity_session_id()
    if session_id:
        if anthropic_route:
            result.setdefault("metadata", {"user_id": session_id})
        else:
            result.setdefault("extra_body", {})["prompt_cache_key"] = session_id

    effort = str(model_cfg.get("reasoning_effort") or "").strip().lower()
    if effort:
        if effort not in _REASONING_EFFORT_LEVELS:
            logger.warning(
                "模型配置 reasoning_effort=%r 无效（仅支持 %s），已忽略",
                effort, "/".join(_REASONING_EFFORT_LEVELS),
            )
        elif anthropic_route:
            # Anthropic 路由（api.kimi.com/coding 等）的推理强度用 thinking_budget 控制
            logger.info("reasoning_effort 仅对 OpenAI 兼容路由生效，Anthropic 路由请配置 thinking_budget")
        else:
            result.setdefault("extra_body", {})["reasoning_effort"] = effort


def _provider_request_options(model_name, api_base):
    """Return options required by a known provider/model pair.

    优先级：
    1. config.json 中模型条目的显式 thinking_budget / max_tokens / enable_thinking
    2. 内置的 Kimi/Moonshot 模型默认表
    3. 空字典（由调用方兜底 max_tokens=8192）

    无论走哪个分支，Kimi 系模型都会附加缓存亲和与 reasoning_effort 配置。
    """
    normalized_base = str(api_base or "").rstrip("/").lower()
    normalized_model = str(model_name or "").split("/")[-1].lower()

    # 1. 读取用户在 config.json 中的显式配置
    try:
        from core.model_manager import get_model_config_by_litellm_model
        model_cfg = get_model_config_by_litellm_model(model_name, api_base) or {}
    except Exception:
        model_cfg = {}

    cfg_max_tokens = model_cfg.get("max_tokens")
    cfg_thinking_budget = model_cfg.get("thinking_budget")
    cfg_enable_thinking = model_cfg.get("enable_thinking")

    # 判定同时看模型名与 api_base（经销商/聚合网关的 base_url 千奇百怪，
    # 只认 base 会漏判，比如 api.scnet.cn 上的 Kimi-K2.6）。
    kimi_family = _is_kimi_family(model_name, api_base)
    route_prefix = str(model_name or "").split("/")[0].lower()
    anthropic_route = route_prefix == "anthropic"

    result = {}

    if cfg_max_tokens is not None or cfg_thinking_budget is not None or cfg_enable_thinking is not None:
        # 用户显式配置优先
        if cfg_max_tokens is not None:
            result["max_tokens"] = int(cfg_max_tokens)
        # thinking 仅在显式启用或配置了 budget 时注入
        if cfg_enable_thinking is True or cfg_thinking_budget is not None:
            budget = int(cfg_thinking_budget) if cfg_thinking_budget is not None else 2048
            result["thinking"] = {"type": "enabled", "budget_tokens": budget}
            result["allowed_openai_params"] = ["thinking"]
    elif kimi_family:
        # 2. 内置默认表：Kimi/Moonshot 深度适配。
        is_kimi_coding = "api.kimi.com/coding" in normalized_base

        def _match_any(keys):
            return any(k in normalized_model for k in keys)

        is_k3 = _match_any(("k3",)) or "for-coding" in normalized_model

        if anthropic_route:
            # Anthropic 协议路由（api.kimi.com/coding 等 anthropic 兼容端点）：
            # 接受 Anthropic 形状的 thinking 参数。
            # 必须同时声明 allowed_openai_params：litellm 的客户端参数校验只把
            # Claude 系列识别为支持 thinking 的 anthropic 模型，对 "K3" 这类陌生
            # 模型名会直接抛 UnsupportedParamsError（请求根本发不出去），需要显式豁免。
            if is_k3:
                result = {
                    "thinking": {"type": "enabled", "budget_tokens": 4096 if is_kimi_coding else 2048},
                    "max_tokens": 32000 if is_kimi_coding else 16000,
                    "allowed_openai_params": ["thinking"],
                }
            else:
                result = {
                    "thinking": {"type": "enabled", "budget_tokens": 2048},
                    "max_tokens": 16000,
                    "allowed_openai_params": ["thinking"],
                }
        else:
            # OpenAI 兼容路由（api.moonshot.cn/v1 及各类经销商）：
            # 绝不发送 Anthropic 形状的 thinking 参数——Moonshot 的
            # /v1/chat/completions 不认这个键，发了会直接 400。
            # Kimi 推理模型（k2.5/k2.6/k2.7/k3 等）在该路由上会原生输出
            # reasoning_content，无需显式开启，只需给足输出上限。
            if is_k3:
                result = {"max_tokens": 32000}
            else:
                result = {"max_tokens": 16384}

    if kimi_family:
        _attach_kimi_request_extras(result, model_name, model_cfg)
    return result


def _complete_thinking_blocks(delta):
    """Extract only complete provider blocks that are safe to round-trip."""
    fields = getattr(delta, "provider_specific_fields", None) or {}
    blocks = fields.get("thinking_blocks") if isinstance(fields, dict) else None
    if not isinstance(blocks, list):
        return None
    complete = [
        deepcopy(block)
        for block in blocks
        if isinstance(block, dict)
        and (
            bool(block.get("signature"))
            or (
                block.get("type") == "redacted_thinking"
                and bool(block.get("data"))
            )
        )
    ]
    return complete or None

def _notify_event(event_callback, kind, payload):
    if event_callback is not None:
        event_callback(kind, payload)


def _tool_call_arguments(tc):
    try:
        return json.loads(tc["function"].get("arguments", "{}"))
    except (TypeError, ValueError):
        return {}


def _tool_event_payload(tc):
    function_name = tc["function"]["name"]
    arguments = _tool_call_arguments(tc)
    preview = ""
    if function_name in ("read_file", "write_file", "edit_file"):
        preview = arguments.get("file_path", "")
    elif function_name in ("search_code", "search_news"):
        preview = arguments.get("query", "")
    elif function_name == "fetch_url":
        preview = arguments.get("url", "")
    elif function_name == "get_weather":
        preview = arguments.get("city", "") or "自动定位"
    elif function_name == "execute_command":
        preview = arguments.get("command", "")
        # 长命令（尤其 PowerShell / curl / python -c）预览截短，避免刷屏
        if preview and len(preview) > 40:
            preview = preview[:37] + "..."
    elif function_name == "glob_files":
        preview = arguments.get("pattern", "")
    elif function_name == "list_directory":
        preview = arguments.get("dir_path", ".")
    elif function_name == "run_subagent":
        preview = arguments.get("task_description", "")
    return {
        "id": tc.get("id", ""),
        "name": function_name,
        "preview": str(preview)[:120],
        "arguments": arguments,
        "summary": summarize_tool_started(function_name, arguments),
    }


def _tool_output_preview(function_name, arguments, result):
    """Return a stable terminal summary without exposing raw tool output."""
    ok = _tool_result_ok(function_name, result)
    return summarize_tool_finished(function_name, arguments, result, ok)


# tool_finished 事件里携带的完整工具结果上限（字符数）：
# GUI 需要展开查看原始输出，但不能让单条事件把 IPC 通道撑爆。
TOOL_RESULT_EVENT_MAX_CHARS = 4000

# 模型只输出思考未生成正文时，促升为公开回复的思考预览长度上限（字符数）。
# 思考原文可能长达数万字，全量展示会把大段内心独白倾倒给用户。
PROMOTED_REASONING_PREVIEW_CHARS = 600


def _tool_result_text(result):
    """把工具消息正文取出并截断，作为 tool_finished 事件的 result 字段。

    这是附加字段：TUI 只读 summary，多出的 result 对它无副作用。
    """
    try:
        content = result.get("content", "") if isinstance(result, dict) else result
    except Exception:
        content = ""
    text = str(content or "")
    if len(text) > TOOL_RESULT_EVENT_MAX_CHARS:
        return text[:TOOL_RESULT_EVENT_MAX_CHARS] + "...（已截断）"
    return text


def _tool_result_ok(function_name, result):
    content = str(result.get("content", "") or "")
    if function_name in {"run_subagent", "ask_user"}:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("status"):
            return payload["status"] in {"running", "completed", "selected"}
    return not content.startswith((
        TOOL_ERROR_PREFIX,
        ASK_USER_ERROR_PREFIX,
        TOOL_INTERNAL_ERROR_PREFIX,
        _PLAN_BYPASS_ERROR_PREFIX,
        "计划执行约束：",
        "计划配置错误：",
        "计划运行状态由真实调度器管理",
        "新建计划项必须是 pending",
    ))


def _raise_if_tool_cancelled(cancel_event):
    # 取消判断只认每 turn 传入的 cancel_event，不再读全局粘性布尔
    if cancel_event is not None and cancel_event.is_set():
        raise KeyboardInterrupt("用户终止了工具执行。")


def _escape_control_chars_in_json_strings(text):
    """只转义字符串值内部的裸控制字符（\\n/\\r/\\t），结构空白与合法转义保持不动。"""
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    out = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\":
                out.append(ch)
                escaped = True
            elif ch == '"':
                out.append(ch)
                in_string = False
            elif ch in escapes:
                out.append(escapes[ch])
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_string = True
    return "".join(out)


def _repair_truncated_json(raw):
    """修复被截断的 JSON 字符串：补全未闭合的字符串、括号、花括号。

    当 LLM 生成的 tool call arguments 超过 API 传输限制时被截断，
    json.loads 会报 "Unterminated string" 错误。本函数只处理末尾截断：
    1. 补全未闭合的字符串（在末尾加引号）
    2. 移除对象/数组末尾多余的逗号
    3. 按栈顺序闭合所有未闭合的 [ { 括号
    4. 如果末尾是冒号（缺值），补空字符串

    注意：如果截断处之后还混入了非 JSON 文本（如模型把对话历史写进参数），
    本函数不再 aggressive 地回退截断，而是让 json.loads 继续失败，由调用方
    返回明确的参数解析错误，避免用被篡改的参数继续执行工具。
    """
    if not raw or not raw.strip():
        return raw

    in_string = False
    escaped = False
    stack = []

    for ch in raw:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "[{":
                stack.append(ch)
            elif ch == "]":
                if stack and stack[-1] == "[":
                    stack.pop()
            elif ch == "}":
                if stack and stack[-1] == "{":
                    stack.pop()

    repaired = raw.rstrip()

    # 如果末尾在字符串内（未闭合），先补上引号
    if in_string:
        # 移除末尾可能残留的反斜杠（避免转义刚补的引号）
        if repaired.endswith("\\"):
            repaired = repaired[:-1]
        repaired += '"'

    # 移除末尾多余的逗号（在闭合括号前）
    repaired = repaired.rstrip()
    while repaired.endswith(","):
        repaired = repaired[:-1].rstrip()

    # 如果末尾是冒号（缺值），补空字符串
    if repaired.endswith(":"):
        repaired += ' ""'

    # 按栈的逆序闭合括号
    closing_map = {"[": "]", "{": "}"}
    while stack:
        opener = stack.pop()
        repaired += closing_map[opener]

    return repaired


def _show_file_diff(file_path, old_content, new_content, event_callback, panel_title, truncated_note):
    """渲染文件修改 diff 面板（write_file / edit_file 共用）。"""
    try:
        from core.diff_viewer import generate_diff, render_diff_table
        from rich.panel import Panel
        from rich.console import Console
        diff_text = generate_diff(old_content, new_content, file_path)
        if not diff_text:
            return
        lines = diff_text.split('\n')
        max_lines = 25
        display_text = (
            '\n'.join(lines[:max_lines]) + f"\n... (省略 {len(lines) - max_lines} 行，{truncated_note})"
            if len(lines) > max_lines else diff_text
        )
        diff_table = render_diff_table(display_text)
        panel = Panel(diff_table, title=panel_title, border_style="green", expand=False)
        from prompt_toolkit.formatted_text import ANSI
        rich_console = Console(force_terminal=True, color_system="truecolor")
        with rich_console.capture() as capture:
            rich_console.print(panel)
        diff_output = capture.get()
        if event_callback is None:
            safe_print(ANSI(diff_output))
        else:
            _notify_event(event_callback, "diff_ready", {
                "file_path": file_path,
                # text: 终端用的 rich 面板（含 ANSI 颜色），TUI 直接打印
                "text": diff_output,
                # diff_text: 纯文本 unified diff，供 GUI 等非终端界面自行着色渲染
                # （ANSI 转义码在 HTML 里会原样显示成乱码）
                "diff_text": display_text,
            })
    except Exception as diff_err:
        logger.warning("渲染文件 diff 失败 (%s): %s", file_path, diff_err)



def _get_imom_active_publish_root():
    """读取 imom_locator 插件的当前活跃项目 publish_root，用于沙盒守卫。"""
    try:
        store_file = os.path.join(os.path.expanduser("~"), ".ycode", "imom_projects.json")
        with open(store_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        active = data.get("active")
        projects = data.get("projects", {})
        if active and active in projects:
            return projects[active].get("publish_root", "")
        if len(projects) == 1:
            return next(iter(projects.values())).get("publish_root", "")
    except Exception:
        pass
    return ""


def _sandbox_outside_guard(
    function_name,
    path_value,
    event_callback,
    permission_owner,
    parent_turn_id,
    permission_source,
    cancel_event,
):
    """沙盒外路径访问守卫：无论何种权限模式，每次访问工作目录外路径都必须询问用户。

    返回 (bypass_cm, error)：
    - (None, None)：在沙盒内，无需特殊处理
    - (cm, None)：用户同意本次，调用方需在 cm 上下文中执行工具
    - (None, err)：用户拒绝/取消，err 为工具结果文案
    """
    if not path_value:
        return None, None
    try:
        outside = is_outside_workspace(path_value)
    except Exception:
        return None, PATH_VALIDATION_ERROR_MESSAGE
    if not outside:
        return None, None
    from core import tools as _tools
    if function_name == "read_file":
        try:
            from core.skills import is_trusted_read_path
            if is_trusted_read_path(str(path_value)):
                return None, None
        except Exception:
            pass
    # 仅线程内单次放行（已批准的嵌套操作不重复询问）；
    # 会话级放行（/sandbox on、"始终放行"）不再跳过询问。
    if getattr(_tools._sandbox_tls, "depth", 0) > 0:
        return None, None

    try:
        approved = check_permission_and_ask(
            "sandbox_outside_access",
            {"path": str(path_value), "operation": function_name},
            event_callback=event_callback,
            permission_owner=permission_owner,
            parent_turn_id=parent_turn_id,
            permission_source=permission_source,
            cancel_event=cancel_event,
        )
    except PermissionSystemError:
        return None, PERMISSION_SYSTEM_ERROR_MESSAGE
    if approved:
        return sandbox_bypass(), None
    if cancel_event is not None and cancel_event.is_set():
        return None, PERMISSION_CANCELLED_MESSAGE
    return None, "🚨 用户拒绝了访问沙盒外路径的请求。沙盒外路径每次访问都需要用户确认。"


def _execute_single_tool_call(
    tc,
    event_callback=None,
    *,
    parent_session=None,
    parent_turn_id=None,
    cancel_event=None,
    allow_subagents=True,
    permission_owner=None,
    permission_source="主代理",
):
    """执行单个工具调用，并返回对应的 JSON 结果结构。"""
    function_name = tc["function"]["name"]
    arguments_str = tc["function"]["arguments"]
    parse_error = None
    try:
        arguments = json.loads(arguments_str)
    except Exception:
        try:
            # 兜底修复：仅转义字符串值内部的裸控制字符，合法 \\n 不会被二次转义
            arguments = json.loads(_escape_control_chars_in_json_strings(arguments_str))
        except Exception:
            try:
                # 二次修复：处理被截断的 JSON（补全未闭合字符串/括号）
                arguments = json.loads(_repair_truncated_json(arguments_str))
            except Exception as repair_err:
                parse_error = repair_err
                arguments = {}

    if parse_error is not None:
        # 参数 JSON 彻底无法解析：直接返回错误，避免用空参数继续执行产生更迷惑的行为。
        # 这对 plan_write 等写操作尤为重要，可防止模型把对话历史混进参数后误清空计划。
        return {
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": function_name,
            "content": (
                f"工具参数 JSON 解析失败：{parse_error}。"
                "请重新调用该工具，并确保 arguments 是合法、完整、未混入其他文本的 JSON。"
            ),
        }

    _raise_if_tool_cancelled(cancel_event)

    plan_enforced = allow_subagents and (
        _is_plan_turn(parent_turn_id) or _has_pending_plan(parent_session)
    )
    if plan_enforced:
        plan_item_id = str(arguments.get("plan_item_id", "") or "").strip()
        if function_name == "plan_write" and _is_plan_turn(parent_turn_id):
            from core.plan import get_default_plan_manager

            if any(
                item.get("status") != "pending"
                for item in get_default_plan_manager().snapshot()
            ):
                return {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": function_name,
                    "content": (
                        "计划执行约束：不能重建已有执行记录，请使用 "
                        "/plan retry <id> 重试失败项。"
                    ),
                }
        if function_name in _PLAN_BOUND_ORCHESTRATORS and not plan_item_id:
            return {
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": function_name,
                "content": (
                    f"{_PLAN_BYPASS_ERROR_PREFIX} {function_name} "
                    "必须填写 plan_item_id，不能执行未归属计划项的任务。"
                ),
            }
        if function_name in _PLAN_READ_ONLY_TOOLS:
            # 只读工具允许主 Agent 直接使用，便于验证子代理产出和读取上下文
            pass
        elif function_name not in _PLAN_ROOT_CONTROL_TOOLS:
            from core.plan import get_default_plan_manager as _get_plan
            _plan = _get_plan()
            ready_items = _plan.ready_items()
            ready_execution = [
                item for item in ready_items if item.get("kind") != "input_preparation"
            ]
            snapshot = _plan.snapshot()
            in_progress = any(
                item.get("status") == "in_progress" for item in snapshot
            )
            # 计划停滞且重试耗尽（剩余项全部 failed/blocked/cancelled 且均尝试>=2 次）时，
            # 放开主 Agent 直接调用作为兜底出口，避免子代理连续失败后主 Agent
            # 被逼入只能空转或等待用户介入的死局；首次失败仍必须走 run_subagent。
            stalled_items = [
                item for item in snapshot
                if item.get("status") in {"failed", "blocked", "cancelled"}
            ]
            retry_exhausted = bool(stalled_items) and all(
                int(item.get("attempts", 0)) >= 2 for item in stalled_items
            )
            if not ready_execution and not in_progress and retry_exhausted:
                pass
            else:
                # 构造可执行计划项列表，引导 LLM 使用正确的 plan_item_id
                ready_hints = ""
                if ready_execution:
                    hints = ", ".join(
                        f"'{item['id']}'({item['subject']})" for item in ready_execution[:5]
                    )
                    ready_hints = f" 当前可执行的计划项: [{hints}]。"
                return {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": function_name,
                    "content": (
                        f"{_PLAN_BYPASS_ERROR_PREFIX} {function_name} 必须通过 "
                        f"run_subagent(plan_item_id=...) 执行，不能由主 Agent 直接绕过计划调用。{ready_hints}"
                        "（若计划项多次失败（attempts>=2）全部停滞后，主 Agent 可直接调用工具修复。）"
                    ),
                }
            
    # 生成友好的中文提示
    friendly_msg = f"⚙️ 星瑶正在后台悄悄使用工具: {function_name} 哒～"
    if function_name == "read_file":
        friendly_msg = f"🔍 星瑶正在读取文件：{arguments.get('file_path', '未知文件')}"
    elif function_name == "write_file":
        friendly_msg = f"✍️ 星瑶正在写入文件：{arguments.get('file_path', '未知文件')}"
    elif function_name == "list_directory":
        friendly_msg = f"📂 星瑶正在查看目录：{arguments.get('dir_path', '当前目录')}"
    elif function_name == "execute_command":
        cmd = arguments.get('command', '...')
        if len(cmd) > 20: cmd = cmd[:20] + "..."
        friendly_msg = f"💻 星瑶正在执行命令：{cmd}"
    elif function_name == "search_code":
        friendly_msg = f"🔍 星瑶正在全局查找: {arguments.get('query', '')}"
    elif function_name == "find_definition":
        friendly_msg = f"🔍 星瑶正在查找符号定义: {arguments.get('symbol', '')}"
    elif function_name == "search_news":
        friendly_msg = f"🌐 星瑶正在联网搜索：{arguments.get('query', '')}"
    elif function_name == "fetch_url":
        friendly_msg = f"🌐 星瑶正在访问网址：{arguments.get('url', '')}"
    elif function_name == "get_weather":
        friendly_msg = f"🌤 星瑶正在查询天气：{arguments.get('city', '') or '自动定位'}"

    # The event-oriented path lets the UI render this status in order.
    from prompt_toolkit.formatted_text import HTML
    safe_friendly_msg = friendly_msg.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    if event_callback is None:
        safe_print(HTML(f"<ansigreen>[ {safe_friendly_msg} ]</ansigreen>"))

    result = ""
    # 权限由工具能力元数据决定，而不是散落的函数名白名单。
    try:
        requires_permission = tool_registry.get(function_name).requires_permission
    except KeyError:
        requires_permission = function_name in ("write_file", "edit_file", "execute_command")
    # 沙盒外的 write_file/edit_file 由 _sandbox_outside_guard 单独询问（文案含路径），
    # 跳过此处的通用确认，避免同一操作被连续询问两次
    _defer_to_sandbox_guard = False
    if requires_permission and function_name in ("write_file", "edit_file"):
        try:
            _defer_to_sandbox_guard = is_outside_workspace(arguments.get("file_path", ""))
        except Exception:
            result = PATH_VALIDATION_ERROR_MESSAGE
    if requires_permission and not result and not _defer_to_sandbox_guard:
        try:
            approved = check_permission_and_ask(
                function_name,
                arguments,
                event_callback=event_callback,
                permission_owner=permission_owner,
                parent_turn_id=parent_turn_id,
                permission_source=permission_source,
                cancel_event=cancel_event,
            )
        except PermissionSystemError:
            # 权限系统自身异常：明确告诉模型这是系统故障，不是用户拒绝
            result = PERMISSION_SYSTEM_ERROR_MESSAGE
        else:
            if not approved:
                # 取消与拒绝使用不同的工具结果文案，避免把“取消”误述为“拒绝”喂给模型
                if cancel_event is not None and cancel_event.is_set():
                    result = PERMISSION_CANCELLED_MESSAGE
                else:
                    result = PERMISSION_DENIED_MESSAGE

    _raise_if_tool_cancelled(cancel_event)

    try:
        if result in (
            PERMISSION_DENIED_MESSAGE,
            PERMISSION_CANCELLED_MESSAGE,
            PERMISSION_SYSTEM_ERROR_MESSAGE,
            PATH_VALIDATION_ERROR_MESSAGE,
        ):
            pass
        elif function_name == "read_file":
            bypass_cm, sandbox_err = _sandbox_outside_guard(
                function_name, arguments.get("file_path"), event_callback,
                permission_owner, parent_turn_id, permission_source, cancel_event,
            )
            if sandbox_err:
                result = sandbox_err
            elif bypass_cm:
                with bypass_cm:
                    result = tool_registry.execute(function_name, **arguments).to_message_content()
            else:
                result = tool_registry.execute(function_name, **arguments).to_message_content()
        elif function_name == "write_file":
            file_path = arguments.get("file_path", "")
            new_content = arguments.get("content", "")
            # 沙盒外写入必须每次询问用户（即使 full 模式）
            _bypass_cm, _sandbox_err = _sandbox_outside_guard(
                function_name, file_path, event_callback,
                permission_owner, parent_turn_id, permission_source, cancel_event,
            )
            if _sandbox_err:
                result = _sandbox_err
            else:
                _write_cm = _bypass_cm if _bypass_cm else sandbox_bypass()
                with _write_cm:
                    old_content = ""
                    if os.path.exists(file_path):
                        try:
                            old_content = _read_text_with_encoding_fallback(file_path)
                        except OSError as read_err:
                            logger.warning("读取旧文件内容失败 (%s): %s", file_path, read_err)
                            old_content = ""
                    write_result = write_file(file_path, new_content)
                if isinstance(write_result, str) and write_result.startswith("🚨"):
                    # 大文件覆盖保护等场景：write_file 拒绝写入，不应展示虚假 diff，也不记录 undo
                    result = write_result
                else:
                    # 写入成功后再记录 undo 快照，避免未发生的修改污染撤销历史
                    from core.memory import set_undo_state
                    set_undo_state(file_path, old_content)
                    # 生成 diff 并展示
                    _show_file_diff(
                        file_path,
                        old_content,
                        new_content,
                        event_callback,
                        panel_title=f"📝 [bold green]已修改文件: {file_path}[/bold green]",
                        truncated_note="已成功写入文件",
                    )
                    result = f"已成功将内容写入文件：{file_path}，并已向用户展示 Diff。"
        elif function_name == "run_subagent":
            if not allow_subagents:
                result = f"{TOOL_ERROR_PREFIX} 子代理不能启动其他子代理。"
            else:
                result = run_subagent(
                    task_description=arguments.get("task_description", ""),
                    subagent_type=arguments.get("subagent_type", "general"),
                    plan_item_id=arguments.get("plan_item_id", ""),
                    run_in_background=arguments.get("run_in_background", False),
                    # 不传时交给 run_subagent 按角色解析默认超时（只读角色更短）
                    timeout_seconds=arguments.get("timeout_seconds"),
                    parent_session=parent_session,
                    parent_turn_id=parent_turn_id,
                    parent_cancel_event=cancel_event,
                    event_callback=event_callback,
                )
        elif function_name == "ask_user":
            from core.ask_user import ask_user, normalize_question_request
            plan_item_id = str(arguments.get("plan_item_id", "") or "").strip()
            scheduler = None
            try:
                question, choices = normalize_question_request(
                    arguments.get("question", ""),
                    arguments.get("options"),
                )
            except ValueError as error:
                result = str(error)
            else:
                arguments["question"] = question
                arguments["options"] = choices

            if not result and plan_item_id:
                from core.task_scheduler import get_default_scheduler

                scheduler = get_default_scheduler()
                plan_item = next(
                    (
                        item
                        for item in scheduler.plan.snapshot()
                        if item["id"] == plan_item_id
                    ),
                    None,
                )
                # 收集可用的 input_preparation 项，用于错误提示
                input_prep_items = [
                    item for item in scheduler.plan.snapshot()
                    if item.get("kind") == "input_preparation"
                ]
                prep_hints = ""
                if input_prep_items:
                    ids = ", ".join(f"'{item['id']}'({item['subject']})" for item in input_prep_items[:5])
                    prep_hints = f" 当前 plan 中的 input_preparation 项: [{ids}]。"
                if plan_item is None:
                    result = json.dumps({
                        "status": "blocked",
                        "plan_item_id": plan_item_id,
                        "error": f"用户输入准备项 '{plan_item_id}' 不存在。{prep_hints}",
                    }, ensure_ascii=False)
                elif plan_item.get("kind") != "input_preparation":
                    result = (
                        f"无法提问：plan_item_id='{plan_item_id}' 的 kind 是 "
                        f"'{plan_item.get('kind', 'unknown')}'，不是 input_preparation。{prep_hints}"
                    )
                elif not scheduler.start_item(
                    plan_item_id,
                    owner="ask_user",
                    event_callback=event_callback,
                ):
                    result = json.dumps({
                        "status": "blocked",
                        "plan_item_id": plan_item_id,
                        "error": "计划项尚未满足依赖或当前计划不可执行。",
                    }, ensure_ascii=False)
                else:
                    try:
                        result = ask_user(
                            question,
                            choices,
                            owner_id=permission_owner,
                            parent_turn_id=parent_turn_id,
                            source=permission_source,
                            cancel_event=cancel_event,
                        )
                        try:
                            result_payload = json.loads(str(result))
                        except (TypeError, ValueError):
                            result_payload = None
                        if (
                            isinstance(result_payload, dict)
                            and result_payload.get("status") == "cancelled"
                        ):
                            scheduler.cancel(plan_item_id, "用户取消提问")
                        elif str(result).startswith("无法") or str(result).startswith(TOOL_ERROR_PREFIX):
                            scheduler.fail(plan_item_id, str(result))
                        elif cancel_event is not None and cancel_event.is_set():
                            scheduler.cancel(plan_item_id, "用户取消提问")
                            result = json.dumps({
                                "status": "cancelled",
                                "plan_item_id": plan_item_id,
                                "error": "用户取消提问",
                            }, ensure_ascii=False)
                        else:
                            scheduler.complete(plan_item_id, str(result))
                        _notify_event(event_callback, "plan_updated", {
                            "items": scheduler.plan.snapshot(),
                            "live_only": True,
                        })
                    except Exception as error:
                        scheduler.fail(plan_item_id, str(error))
                        raise
            elif not result:
                result = ask_user(
                    question,
                    choices,
                    owner_id=permission_owner,
                    parent_turn_id=parent_turn_id,
                    source=permission_source,
                    cancel_event=cancel_event,
                )
        elif function_name == "manage_task":
            # 透传当前回合的回调/权限 owner/取消事件（kwargs 形式）。
            # 按函数签名过滤，向后兼容尚未接受这些参数的 manage_task 版本。
            import inspect as _inspect

            _extra = {
                "event_callback": event_callback,
                "permission_owner": permission_owner,
                "cancel_event": cancel_event,
            }
            _supported = {
                key: value
                for key, value in _extra.items()
                if key in _inspect.signature(manage_task).parameters
            }
            result = manage_task(
                action=arguments.get("action"),
                task_id=arguments.get("task_id"),
                **_supported,
            )
        elif function_name in {
            "list_directory",
            "search_code",
            "find_definition",
            "execute_command",
            "search_news",
            "fetch_url",
            "get_weather",
            "glob_files",
            "plan_write",
            "plan_update",
            "plan_read",
            "plan_retry",
        }:
            # 只读目录类工具在沙盒外访问时需用户逐次确认（沙盒外访问不提供会话放行）
            bypass_cm, sandbox_err = None, None
            if function_name in ("list_directory", "search_code", "find_definition", "glob_files"):
                bypass_cm, sandbox_err = _sandbox_outside_guard(
                    function_name, arguments.get("dir_path", "."), event_callback,
                    permission_owner, parent_turn_id, permission_source, cancel_event,
                )
            if function_name == "execute_command" and cancel_event is not None:
                arguments["cancel_event"] = cancel_event

            if sandbox_err:
                result = sandbox_err
            elif bypass_cm:
                with bypass_cm:
                    result = tool_registry.execute(function_name, **arguments).to_message_content()
            else:
                result = tool_registry.execute(function_name, **arguments).to_message_content()
            if function_name in {"plan_write", "plan_update"}:
                if (
                    function_name == "plan_write"
                    and str(result).strip() == "计划已更新。"
                ):
                    _mark_plan_turn(parent_turn_id)
                from core.plan import get_default_plan_manager
                _notify_event(event_callback, "plan_updated", {
                    "items": get_default_plan_manager().snapshot(),
                    "live_only": True,
                })
        elif function_name == "edit_file":
            file_path = arguments.get("file_path")
            target = arguments.get("target_content")
            repl = arguments.get("replacement_content")

            # 沙盒外编辑必须先获批，再检查目标是否存在，避免把外部路径存在性泄露给调用方。
            _bypass_cm, _sandbox_err = _sandbox_outside_guard(
                function_name, file_path, event_callback,
                permission_owner, parent_turn_id, permission_source, cancel_event,
            )
            if _sandbox_err:
                result = _sandbox_err

            # 对过长的 target_content 提前拦截，避免 JSON 截断后反复失败浪费 token。
            # 模型应拆分为多个小片段编辑，或改用 write_file 重写整个文件。
            _EDIT_TARGET_MAX_LEN = 1500
            if not result and target and len(target) > _EDIT_TARGET_MAX_LEN:
                result = (
                    f"{TOOL_ERROR_PREFIX} edit_file 的 target_content 过长（"
                    f"{len(target)} 字符），超过 {_EDIT_TARGET_MAX_LEN} 字符限制，"
                    f"容易在传输过程中被截断。请将本次编辑拆分为多个不超过 {_EDIT_TARGET_MAX_LEN} "
                    f"字符的小段后重试；如需整体重写，请改用 write_file。"
                )
                return {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": function_name,
                    "content": result,
                }

            # 文件不存在时直接报错（edit_file 只能修改已有文件），
            # 避免对不存在的沙盒外路径做无意义的权限询问
            if not result and not os.path.isfile(file_path or ""):
                # 文件不存在时 target_content 必然无法定位，若走下面的截断校验
                # 会误报"JSON 修复后不完整"，掩盖真实原因（该用 write_file 建新文件）
                result = (
                    f"{TOOL_ERROR_PREFIX} edit_file 目标文件不存在：{file_path}。"
                    "edit_file 只能修改已有文件；创建新文件请改用 write_file。"
                )
                return {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": function_name,
                    "content": result,
                }

            if not result:
                _edit_cm = _bypass_cm if _bypass_cm else sandbox_bypass()
                with _edit_cm:
                    try:
                        old_content = _read_text_with_encoding_fallback(file_path)
                    except OSError as read_err:
                        logger.warning("读取旧文件内容失败 (%s): %s", file_path, read_err)
                        old_content = ""
                    # 不再做精确子串预检查：edit_file 内部 _fuzzy_replace 已实现
                    # 精确匹配、空白容错和 difflib 模糊匹配（相似度 >= 0.90），
                    # 预检查过严会导致仅有轻微空白差异的合法编辑被误拦截。
                    result = edit_file(
                        file_path,
                        target,
                        repl,
                        replace_all=bool(arguments.get("replace_all", False)),
                        start_line=arguments.get("start_line"),
                        end_line=arguments.get("end_line"),
                    )
                    if not result.startswith(TOOL_ERROR_PREFIX):
                        try:
                            new_content = _read_text_with_encoding_fallback(file_path)
                        except OSError as read_err:
                            logger.warning("读取新文件内容失败 (%s): %s", file_path, read_err)
                            new_content = ""
                        _show_file_diff(
                            file_path,
                            old_content,
                            new_content,
                            event_callback,
                            panel_title=f"📝 [bold green]局部修改文件: {file_path}[/bold green]",
                            truncated_note="已局部替换并修改成功",
                        )
        else:
            # 通用兜底：优先查进程内工具注册表（含 imom_locator 等插件工具），
            # 再降级到 MCP 工具，最后才报"未找到"。
            try:
                _spec = tool_registry.get(function_name)
            except KeyError:
                _spec = None
            if _spec is not None:
                # 插件工具（如 imom_locator）访问工作目录外文件，必须每次询问用户
                _plugin_guard_path = None
                if function_name == "imom_register_project":
                    # hint_path 缺省时兜底 publish_root，避免守卫被静默跳过
                    _plugin_guard_path = (
                        arguments.get("hint_path")
                        or arguments.get("publish_root")
                        or ""
                    )
                elif function_name.startswith("imom_"):
                    # 其他 imom 工具：取当前活跃项目的 publish_root 作为守卫路径
                    try:
                        _plugin_guard_path = _get_imom_active_publish_root()
                    except Exception:
                        _plugin_guard_path = None
                if _plugin_guard_path:
                    _p_bypass, _p_err = _sandbox_outside_guard(
                        function_name, _plugin_guard_path, event_callback,
                        permission_owner, parent_turn_id, permission_source, cancel_event,
                    )
                    if _p_err:
                        result = _p_err
                    else:
                        _p_cm = _p_bypass if _p_bypass else sandbox_bypass()
                        with _p_cm:
                            result = tool_registry.execute(function_name, **arguments).to_message_content()
                else:
                    result = tool_registry.execute(function_name, **arguments).to_message_content()
            elif any(s["function"]["name"] == function_name for s in mcp_manager.get_schemas()):
                result = mcp_manager.call_tool_sync(function_name, arguments)
            else:
                result = f"{TOOL_ERROR_PREFIX} 未找到名为 {function_name} 的工具！"
    except Exception as tool_err:
        result = f"{TOOL_ERROR_PREFIX} 调用工具 {function_name} 时发生系统内部错误：{str(tool_err)}"

    return {
        "role": "tool",
        "tool_call_id": tc["id"],
        "name": function_name,
        "content": _truncate_tool_result_for_context(function_name, str(result)),
    }


# ─── 工具结果入上下文前的截断 ────────────────────────────────────
# 这是最大的 token 泄漏点：一次 fetch_url 可以塞进 512KB（≈13 万 token）原文，
# 一次 pip install 的 stdout 可以是几 MB。截断只影响送进模型上下文/落库的内容，
# 不影响工具本身的执行结果与终端展示。
_TOOL_RESULT_MAX_CHARS = 24000            # 默认上限（约 8K token）
_TOOL_RESULT_LIMITS = {
    "execute_command": 16000,             # 保留头尾：报错信息几乎都在结尾
    "fetch_url": 16000,                   # 保留头部：正文开头信息密度最高
    "search_news": 8000,
}
_TOOL_RESULT_TAIL_TOOLS = {"execute_command"}
# read_file 自带行数/字节上限，且编辑流程依赖完整内容，不做二次截断
_TOOL_RESULT_EXEMPT = {"read_file"}


def _truncate_tool_result_for_context(function_name, text):
    if function_name in _TOOL_RESULT_EXEMPT:
        return text
    limit = _TOOL_RESULT_LIMITS.get(function_name, _TOOL_RESULT_MAX_CHARS)
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    if function_name in _TOOL_RESULT_TAIL_TOOLS:
        head_len = limit // 4
        head = text[:head_len]
        tail = text[-(limit - head_len):]
        return f"{head}\n…[输出过长，中间省略 {dropped} 字符]…\n{tail}"
    return (
        f"{text[:limit]}\n"
        f"…[结果过长，已截断 {dropped} 字符。如需后续内容请用更精确的参数重新调用]"
    )

def _call_vision_model_for_content(secondary, content, cancel_event=None, max_retries=3):
    """调用支持视觉的副模型，传入完整 content（含图片），返回回复文本；失败返回空字符串。"""
    kwargs = {
        "model": secondary["litellm_model"],
        "messages": [{"role": "user", "content": content}],
        "api_key": secondary["api_key"],
        "api_base": secondary["base_url"],
        "max_tokens": 2048,
        "timeout": API_REQUEST_TIMEOUT_SECONDS,
    }

    from concurrent.futures import ThreadPoolExecutor
    warning_logger = logging.getLogger("py.warnings")

    for attempt in range(max_retries):
        try:
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(_litellm_completion, **kwargs)
            try:
                while not future.done():
                    if cancel_event is not None and cancel_event.is_set():
                        future.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                        return ""
                    try:
                        future.result(timeout=0.1)
                    except Exception:
                        pass
                exc = future.exception()
                if exc is not None:
                    raise exc
                resp = future.result()
            finally:
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)

            if resp and getattr(resp, "choices", None):
                c = resp.choices[0].message.content
                if c is not None:
                    text = str(c).strip()
                    if text:
                        return text
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
        except Exception as e:
            warning_logger.warning(
                "副模型图片分析失败 (attempt %d/%d): %s %s",
                attempt + 1, max_retries, type(e).__name__, e,
            )
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    return ""


def _vision_fallback_text(user_text=""):
    """图片分析失败时替换用的提示文本，明确让主模型直接回复用户。"""
    hint = (
        "[当前主模型和副模型均不支持图片输入或图片分析失败，无法处理图片。"
        "请直接回复用户：无法分析图片，不要调用任何工具。]"
    )
    if user_text:
        return f"{user_text}\n\n{hint}"
    return hint


def _describe_images_with_secondary(messages, main_model_name, event_callback=None, cancel_event=None):
    """主模型不识图时，直接用副模型分析本轮最新图片并替换为文字描述/回答。"""
    if _supports_vision(main_model_name):
        return

    # 只处理最近一条用户消息中的图片；历史图片即使还在上下文里也不再重复分析
    target_msg = None
    for msg in reversed(messages):
        if msg.get("role") == "user":
            target_msg = msg
            break
    if target_msg is None:
        return

    content = target_msg.get("content")
    if not isinstance(content, list):
        return
    image_items = [
        item for item in content
        if isinstance(item, dict) and item.get("type") in ("image", "image_url")
    ]
    if not image_items:
        return
    text_items = [
        item for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]

    user_text = " ".join(t.get("text", "") for t in text_items).strip()

    from core.model_manager import get_secondary_model, get_secondary_roles
    roles = get_secondary_roles()
    secondary = get_secondary_model() if roles.get("vision") else None

    if not secondary or not _supports_vision(secondary["litellm_model"]):
        _notify_event(event_callback, "notice", {
            "message": "当前主模型不支持图片输入，且副模型无法分析图片，已将图片替换为占位说明。",
        })
        target_msg["content"] = _vision_fallback_text(user_text)
        return

    _notify_event(event_callback, "notice", {
        "message": f"主模型不支持图片输入，正在使用副模型 {secondary['name']} 分析图片...",
    })

    # 把用户的问题也一起给副模型，让副模型直接作答
    if user_text:
        vision_prompt = f"请基于图片的视觉内容回答以下问题：{user_text}"
    else:
        vision_prompt = (
            "请以视觉助手身份理解这张图片：先判断图片类型（如截图/海报/照片/图表/界面截图等），"
            "再说明图片的主题、关键视觉元素和传达的信息；"
            "如果图片包含文字，提炼文字核心要点而非逐字复制。"
        )
    sec_content = [{"type": "text", "text": vision_prompt}] + image_items

    answer = _call_vision_model_for_content(secondary, sec_content, cancel_event=cancel_event)
    if answer:
        if user_text:
            target_msg["content"] = f"{user_text}\n\n[图片分析结果]\n{answer}"
        else:
            target_msg["content"] = f"[图片分析结果]\n{answer}"
    else:
        target_msg["content"] = _vision_fallback_text(user_text)


def _chat_with_model_generator(
    model_name,
    api_key,
    messages,
    api_base=None,
    stream=False,
    event_callback=None,
    cancel_event=None,
    allow_subagents=True,
    allowed_tools=None,
    consume_background_notifications=True,
    parent_session=None,
    parent_turn_id=None,
    permission_owner=None,
    permission_source="主代理",
):
    """
    内部大模型驱动生成器：一律开启流式传输以对思考标签和工具链参数进行动态清洗和处理。
    """
    turn_snapshot = deepcopy(messages)
    # 1. 自动注入 KAIROS 长期记忆指示与 MEMORY.md 索引。
    # 注意：main.py 的动态上下文（尾部第二条 system 消息）里已经带了完整记忆段，
    # 所以必须检查所有 system 消息，避免重复注入（重复会翻倍 token 消耗）。
    # 同时绝不能把动态内容拼进 messages[0]——那是提示词缓存的稳定前缀，
    # 一旦每轮变化，provider 侧的 prompt cache 将永远无法命中。
    import core.memory_kairos as mk

    _memory_already_injected = any(
        msg.get("role") == "system" and "# Auto Memory" in str(msg.get("content") or "")
        for msg in messages
    )
    if not _memory_already_injected:
        memory_section = mk.build_memory_system_section()
        if memory_section and memory_section.strip():
            # 作为独立 system 消息插入到最后一条 user 消息之前（而非改写稳定前缀）
            insert_at = len(messages)
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    insert_at = i
                    break
            messages.insert(insert_at, {"role": "system", "content": memory_section})

    tool_activity_seen = False
    final_summary_retry_count = 0
    # 计划未执行完时的续推次数上限：防止模型反复提前收尾与续推消息互相打转
    plan_continue_nudges = 0
    pending_persistence = []

    def flush_persistence():
        """把本轮已确认的助手/工具消息按序落库（与正常结束路径一致）。"""
        nonlocal pending_persistence
        if stream and pending_persistence:
            _mgr = app_context.get_manager()
            if not _mgr:
                # 当前没有可用的存储管理器：保留队列，待下次 flush 时再尝试落库，
                # 直接清空会导致整批消息静默丢失。
                return
            try:
                _mgr.add_messages(parent_session, pending_persistence)
            except Exception as flush_err:
                # 落库失败不回滚内存：add_messages 可能已部分写入，回滚会造成
                # 内存/DB 分叉。仅上报错误事件并丢弃待写队列，避免重复写入。
                _notify_event(event_callback, "error", {
                    "message": f"消息持久化失败：{str(flush_err)}",
                })
            finally:
                del pending_persistence[:]

    # 2. 注入后台子 Agent 回报数据 (XML task-notification)
    from core.tools import subagent_manager
    drained_notifications = []
    if consume_background_notifications and parent_session:
        drained_notifications = subagent_manager.drain_notifications(parent_session)
        for notif in drained_notifications:
            notif_msg = {"role": "user", "content": notif}
            messages.append(notif_msg)
            # 立即加入待持久化队列并落库：即使本轮后续被取消，通知也不会丢失，
            # 否则模型上下文会出现"收到通知并回复了，但下一轮找不到通知"的矛盾。
            # 注意：队列元素必须是 (role, content) 二元组，与 add_messages 的解包约定一致。
            pending_persistence.append(("user", notif))
        flush_persistence()

    def rollback_turn_messages():
        """回滚本轮新增的消息残影；已 drain 的后台通知重新保留，避免丢失。"""
        messages[:] = turn_snapshot
        for notif in drained_notifications:
            messages.append({"role": "user", "content": notif})

    # 注意：不因“计划进行中”裁剪 tools——计划约束由执行层
    # （_execute_single_tool_call 的 plan_enforced 拦截）统一保证，裁剪是双重保险。
    # tools 列表逐字节稳定是 DeepSeek/Kimi 前缀缓存命中的前提，
    # 计划创建↔完成之间切换工具集会让缓存从头部整体失效，代价远大于省下的 schema token。
    # MCP 管理工具默认不进请求：用户几乎都通过 /mcp 界面管理 MCP，
    # 这 4 个 schema 每轮白吃约 500 token。需要让 AI 代管 MCP 时在
    # config.json 里设 "expose_mcp_admin_tools": true 即可恢复。
    _MCP_ADMIN_TOOLS = {"mcp_add_server", "mcp_remove_server", "mcp_set_enabled", "mcp_list_servers"}
    try:
        from core.model_manager import load_config as _load_cfg_for_tools
        _expose_mcp_admin = bool(_load_cfg_for_tools().get("expose_mcp_admin_tools", False))
    except Exception:
        _expose_mcp_admin = False
    request_tools = [
        schema
        for schema in get_tools_schema()
        if allow_subagents
        or schema.get("function", {}).get("name")
        not in {"run_subagent", "plan_write", "plan_update"}
        if allowed_tools is None
        or schema.get("function", {}).get("name") in allowed_tools
        if _expose_mcp_admin
        or (allowed_tools is not None and schema.get("function", {}).get("name") in allowed_tools)
        or schema.get("function", {}).get("name") not in _MCP_ADMIN_TOOLS
    ]

    kwargs = {
        "model": model_name,
        "api_key": api_key,
        # 占位：每次请求前都会在重试循环里用 _strip_media_and_merge 的结果覆盖，
        # 这里不再提前做一遍完整清洗（此前同一份数据每轮被处理两次，纯浪费 CPU）
        "messages": messages,
        # tools 每轮都带（含简单问候轮）：请求形状逐字节稳定是前缀缓存命中的前提，
        # 问候轮省下的 schema token 远小于缓存整体失效的代价；
        # 模型在 tool_choice=auto 下对闲聊本来就不会调用工具
        "tools": request_tools,
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
    }
    kwargs["tool_choice"] = "auto"
    kwargs.update(_provider_request_options(model_name, api_base))

    # 默认输出长度上限：防止免费/轻量模型（如 glm-4-flash）默认 max_tokens 过小
    # 导致长回复被静默截断。若 provider 已显式配置则尊重其值。
    if "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
        kwargs["max_tokens"] = 8192

    if api_base:
        kwargs["api_base"] = api_base

    # 主模型不识图时，尝试用副模型先分析图片并替换为文字描述。
    _describe_images_with_secondary(messages, model_name, event_callback=event_callback, cancel_event=cancel_event)

    # (pending_persistence and flush_persistence are now defined earlier)

    def raise_if_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("用户终止了对话。")

    try:
        max_turns = 100
        for turn in range(max_turns):
            raise_if_cancelled()

            # API 调用指数退避与 Jitter 抖动重试
            response_stream = None
            max_api_retries = 5
            base_delay = 1.0
            # 超窗自动压缩只允许一次：压缩后仍超限说明单条消息/附件本身过大，
            # 继续重试只会反复重发超大载荷空烧额度
            overflow_compress_attempts = 0

            for attempt in range(max_api_retries):
                raise_if_cancelled()
                try:
                    cleaned_messages, folded_count, keep_media = _strip_media_and_merge(messages, model_name)
                    if _is_kimi_family(model_name, api_base):
                        # Moonshot 推理模型要求所有带 tool_calls 的 assistant 消息
                        # 携带 reasoning_content；从数据库重建的历史消息可能已被剥掉，
                        # 这里补空格占位符（litellm 的 moonshot/ 路由会自动做，
                        # 但经销商常用的 openai/ 路由不会）。
                        for _m in cleaned_messages:
                            if (
                                _m.get("role") == "assistant"
                                and _m.get("tool_calls")
                                and not _m.get("reasoning_content")
                            ):
                                _m["reasoning_content"] = " "
                    kwargs["messages"] = _apply_cache_control(cleaned_messages, model_name)
                    # 历史图片折叠属于正常节省行为，不再向用户弹提示打扰
                    # 超时随载荷放大：超大上下文 + 深度思考的首个 token 可能要几分钟，
                    # 90s 一刀切会让大载荷反复超时重发（每次重发输入都全额计费）
                    try:
                        _est_prompt_tokens = int(estimate_tokens(model_name, cleaned_messages) or 0)
                    except Exception:
                        _est_prompt_tokens = 0
                    attempt_timeout = min(
                        API_REQUEST_MAX_TIMEOUT_SECONDS,
                        API_REQUEST_TIMEOUT_SECONDS + max(0, _est_prompt_tokens) // 1500,
                    )
                    # 将同步阻塞的 completion 放到线程池中执行，使主循环可以响应取消事件。
                    # 网络请求一旦发出无法真正中断，但调用方无需傻等到超时才能退出。
                    from concurrent.futures import ThreadPoolExecutor
                    executor = ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(completion, **kwargs, timeout=attempt_timeout)
                    wait_started_at = time.monotonic()
                    last_wait_notice_at = wait_started_at
                    try:
                        while not future.done():
                            raise_if_cancelled()
                            try:
                                future.result(timeout=0.1)
                            except Exception:
                                # result(timeout=0.1) 在超时或异常时都会抛；
                                # 交给下面统一处理 future.exception()
                                pass
                            # 等待超过阈值后按间隔报一次等待状态，避免大载荷请求
                            # 长时间无反馈被误判为“卡死”（重试提示只在异常后才有）
                            waited = time.monotonic() - wait_started_at
                            if (
                                waited >= WAIT_NOTICE_THRESHOLD_SECONDS
                                and time.monotonic() - last_wait_notice_at >= WAIT_NOTICE_INTERVAL_SECONDS
                            ):
                                last_wait_notice_at = time.monotonic()
                                wait_message = (
                                    f"模型响应中，已等待 {int(waited)} 秒"
                                    f"（本轮载荷约 {_est_prompt_tokens // 1000}k tokens，超时上限 {attempt_timeout} 秒）；"
                                    "超大上下文与深度思考可能较慢，可按 Ctrl+C 中断"
                                )
                                if event_callback is None:
                                    print(f"\n[⏳ {wait_message}]")
                                else:
                                    _notify_event(event_callback, "status", {"message": wait_message})
                        if future.cancelled():
                            raise KeyboardInterrupt("用户终止了对话。")
                        exc = future.exception()
                        if exc is not None:
                            raise exc
                        response_stream = future.result()
                    finally:
                        # 用户取消时避免 shutdown(wait=True) 阻塞到网络请求完成（最长 90s）
                        future.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                    break
                except Exception as api_err:
                    err_str = str(api_err).lower()
                    # 上下文窗口超限自动压缩历史记录并重试：
                    # 优先按异常类型判断，字符串匹配作为兜底且要求上下文相关关键词组合，
                    # 避免把 rate limit 等错误误判为超限
                    is_context_overflow = isinstance(api_err, _litellm_context_overflow_error())
                    if not is_context_overflow:
                        is_context_overflow = (
                            "prompt is too long" in err_str
                            or "context window" in err_str
                            or "context_length" in err_str
                            or (
                                "exceeds" in err_str
                                and ("context" in err_str or "token" in err_str or "length" in err_str)
                            )
                        )
                    if is_context_overflow:
                        if overflow_compress_attempts >= 1:
                            # 已压缩过一次仍然超限：单条消息/附件本身过大，继续重试只会
                            # 反复重发超大载荷空烧额度。直接失败并给出可操作的指引。
                            raise RuntimeError(
                                "历史压缩后仍超出模型上下文窗口，本轮请求已中止，"
                                "以避免反复重发超大载荷空耗额度。"
                                "建议 /new 开新会话，或移除超大附件/图片后重试。"
                            ) from api_err
                        overflow_compress_attempts += 1
                        try:
                            if event_callback is None:
                                print("\n[⚠️ 检测到上下文窗口超限，正在尝试压缩历史记录以缩减 Prompt 长度...]")
                            else:
                                _notify_event(event_callback, "status", {
                                    "message": "检测到上下文窗口超限，正在压缩历史记录。",
                                })
                            from core.memory_compressor import compress_session_memory
                            # 复用全局单例 manager（共享 SQLite 连接 + 锁），
                            # 避免每次新建 MemoryManager 导致连接泄漏
                            manager = app_context.get_manager()
                            # 子代理回合必须压缩自己的会话（parent_session），
                            # 不能误压主 UI 会话
                            session = parent_session or app_context.get_current_session()
                            # compress_session_memory 是正确的压缩函数（位于 memory_compressor），
                            # 旧的 mm.compress_session 是不存在的方法名，会抛 AttributeError 被吞掉
                            compress_session_memory(
                                manager,
                                session,
                                api_model=model_name,
                                api_key=api_key,
                                api_url=api_base,
                                force=True,
                                output_callback=(
                                    (lambda msg: _notify_event(event_callback, "status", {"message": re.sub(r"<[^>]+>", "", msg)}))
                                    if event_callback is not None else None
                                ),
                            )
                            # 原地替换为压缩后历史：禁止重新绑定局部名，
                            # 否则调用方持有的旧列表会失联、取消回滚也会作用错对象
                            messages[:] = manager.get_history(session)
                            # 压缩已成功落库：同步回合快照，避免后续异常回滚把
                            # 内存恢复成未压缩状态（与 DB 分叉且下轮必再超限）
                            turn_snapshot = deepcopy(messages)
                            if attempt < max_api_retries - 1:
                                continue  # 跳回重试当前 API
                            # 已是最后一次重试，压缩后仍无法发出请求，抛出原始错误
                            # 而不是带着 response_stream=None 继续迭代导致 TypeError
                            raise api_err
                        except Exception as compress_err:
                            if event_callback is None:
                                from prompt_toolkit.formatted_text import HTML

                                safe_print(HTML("<ansiyellow>历史记录暂未完成压缩</ansiyellow>"))
                            else:
                                _notify_event(event_callback, "error", {
                                    "message": str(compress_err),
                                })

                    if attempt < max_api_retries - 1:
                        # 速率限制（429 / TooManyRequests / RateLimitError）需要更长退避，
                        # 普通网络抖动用短退避即可。
                        is_rate_limit = (
                            "rate limit" in err_str
                            or "too many requests" in err_str
                            or "too frequent" in err_str
                            or "AccountRateLimitExceeded" in str(api_err)
                        )
                        if is_rate_limit:
                            rate_limit_base_delay = 5.0
                            sleep_time = (rate_limit_base_delay * (2 ** attempt)) + (random.random() * 1.0)
                            retry_message = (
                                f"触发 API 速率限制，正在进行第 {attempt+1}/{max_api_retries} 次重试，"
                                f"等待 {sleep_time:.1f} 秒"
                            )
                        else:
                            sleep_time = (base_delay * (2 ** attempt)) + (random.random() * 0.5)
                            retry_message = (
                                f"API 暂未成功，正在进行第 {attempt+1}/{max_api_retries} 次重试，"
                                f"等待 {sleep_time:.1f} 秒"
                            )
                        if event_callback is None:
                            print(f"\n[⚠️ {retry_message}]")
                        else:
                            _notify_event(event_callback, "status", {"message": retry_message})
                        # 将 sleep 切成小段并检查取消事件，避免用户按 Ctrl+C 后长时间无响应
                        sleep_deadline = time.monotonic() + sleep_time
                        while time.monotonic() < sleep_deadline:
                            raise_if_cancelled()
                            remaining = sleep_deadline - time.monotonic()
                            time.sleep(min(0.1, remaining) if remaining > 0 else 0.1)
                    else:
                        raise api_err

            accumulated_content = ""
            tool_calls_dict = {}
            in_native_think = False
            provider_thinking_blocks = None
            finish_reason = None

            stream_usage = None
            for chunk in response_stream:
                raise_if_cancelled()

                # 同一流中可能出现多个带 usage 的 chunk，只保留最后一个，
                # 在流结束后统一记账一次，避免 cumulative_* 被反复累加
                if getattr(chunk, 'usage', None):
                    stream_usage = chunk.usage

                if not chunk.choices:
                    continue

                chunk_finish_reason = getattr(chunk.choices[0], "finish_reason", None)
                if chunk_finish_reason:
                    finish_reason = chunk_finish_reason

                delta = chunk.choices[0].delta

                complete_blocks = _complete_thinking_blocks(delta)
                if complete_blocks is not None:
                    provider_thinking_blocks = complete_blocks
                
                # reasoning_content (DeepSeek R1/V3)
                # 注意：thinking 事件只由下游 parser 根据 <think> 文本统一转换，
                # 此处不再直接发事件，避免双重发射
                reasoning = getattr(delta, "reasoning_content", "") or ""
                if reasoning:
                    if not in_native_think:
                        yield "<think>"
                        accumulated_content += "<think>"
                        in_native_think = True
                    yield reasoning
                    accumulated_content += reasoning
                    
                # Content text
                content = getattr(delta, "content", "") or ""
                if content:
                    if in_native_think:
                        yield "</think>"
                        accumulated_content += "</think>"
                        in_native_think = False
                    yield content
                    accumulated_content += content
                    
                # Tool Calls
                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_dict:
                            tool_calls_dict[idx] = {
                                "id": None,
                                "type": "function",
                                "function": {"name": "", "arguments": ""}
                            }
                        # 某些 provider 首 chunk 的 tool_call id 为 None，在后续 chunk 才给出。
                        # 这里用 None 占位，遇到真实 id 再回填；始终没有则用 call_{idx} 兜底。
                        tc_id = getattr(tc, "id", None)
                        existing_id = tool_calls_dict[idx].get("id")
                        if tc_id is not None:
                            if existing_id is None or str(existing_id).startswith("call_"):
                                tool_calls_dict[idx]["id"] = tc_id
                        elif existing_id is None:
                            tool_calls_dict[idx]["id"] = f"call_{idx}"
                        # name 只在为空时赋值：provider 重复发 name 时 += 会拼出
                        # "read_fileread_file" 之类的坏名字
                        if getattr(tc.function, "name", None) and not tool_calls_dict[idx]["function"]["name"]:
                            tool_calls_dict[idx]["function"]["name"] = tc.function.name
                        if getattr(tc.function, "arguments", None):
                            tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments
            
            if in_native_think:
                yield "</think>"
                accumulated_content += "</think>"

            # 输出被模型长度限制截断：给出明确提示，避免用户以为任务已完成。
            if finish_reason == "length":
                truncated_notice = (
                    "\n[回复已被模型长度限制截断。如需完整结果，请尝试："
                    "1) 让模型分步骤输出；2) 换用输出上限更高的模型；"
                    "3) 在 config.json 中为当前模型增大 max_tokens。]"
                )
                _notify_event(event_callback, "notice", {"message": truncated_notice.strip()})

            # 流结束：按最终 usage chunk 统一记账一次（读写均走锁）
            if stream_usage is not None:
                cache_details = getattr(stream_usage, "prompt_tokens_details", None)
                latest_cached = (
                    cache_details.cached_tokens
                    if cache_details is not None and hasattr(cache_details, "cached_tokens")
                    else 0
                ) or 0
                with token_usage_lock:
                    session_token_usage["latest_total"] = getattr(stream_usage, "total_tokens", 0) or 0
                    session_token_usage["latest_prompt"] = getattr(stream_usage, "prompt_tokens", 0) or 0
                    # completion = total - prompt（多数 provider 不直接给 completion_tokens）
                    session_token_usage["latest_cached"] = latest_cached
                    session_token_usage["cumulative_total"] += session_token_usage["latest_total"]
                    session_token_usage["cumulative_prompt"] += session_token_usage["latest_prompt"]
                    session_token_usage["cumulative_completion"] += max(
                        0, session_token_usage["latest_total"] - session_token_usage["latest_prompt"]
                    )
                    session_token_usage["cumulative_cached"] += latest_cached

            reasoning_content, public_content = split_reasoning_text(accumulated_content)
            from core.turn_renderer import sanitize_protocol_text
            public_content = sanitize_protocol_text(public_content)

            # 兜底：模型只输出 thinking 但未生成正文时，把思考过程作为回复展示，
            # 避免 UI 在 "思考完成" 后出现空白，给用户造成 Agent 没有响应的错觉。
            # 注意：工具调用轮保持 content 为 None，不污染工具链签名。
            # 思考原文可能极长（数万字），直接全量促升会把大段内心独白倾倒给用户，
            # 因此只保留开头一段预览，其余截断。
            raw_content_meaningful = _is_meaningful_assistant_content(public_content)
            promoted_reasoning = False
            if (
                not tool_calls_dict
                and not raw_content_meaningful
                and _is_meaningful_assistant_content(reasoning_content)
            ):
                reasoning_preview = reasoning_content.strip()
                if len(reasoning_preview) > PROMOTED_REASONING_PREVIEW_CHARS:
                    reasoning_preview = (
                        reasoning_preview[:PROMOTED_REASONING_PREVIEW_CHARS].rstrip()
                        + " …（思考内容过长，已截断）"
                    )
                public_content = reasoning_preview + "\n\n[以上为模型思考过程，未生成正式回复]"
                promoted_reasoning = True

            # 只允许公开正文写入记忆，避免把原生推理误当作长期事实。
            memory_tags = mk.extract_memory_tags(public_content)
            for tag in memory_tags:
                try:
                    mk.append_to_daily_log(tag)
                except Exception as log_err:
                    if event_callback is None:
                        print(f"\n[⚠️ 写入日记账记忆失败: {str(log_err)}]")
                    else:
                        _notify_event(event_callback, "error", {
                            "message": f"写入日记账记忆失败：{str(log_err)}",
                        })

            tool_calls_list = []
            for idx in sorted(tool_calls_dict.keys()):
                tool_calls_list.append(tool_calls_dict[idx])
                
            message_to_append = {
                "role": "assistant",
                "content": public_content or None,
                "reasoning_content": reasoning_content or None,
            }
            if provider_thinking_blocks is not None:
                message_to_append["thinking_blocks"] = provider_thinking_blocks
            if tool_calls_list:
                message_to_append["tool_calls"] = tool_calls_list
            messages.append(message_to_append)

            persisted_message = {
                "role": "assistant",
                "content": public_content or None,
                "reasoning_content": reasoning_content or None,
            }
            if tool_calls_list:
                persisted_message["tool_calls"] = tool_calls_list

            # 用"促升前"的原始正文判断：思考内容被促升为正文后不能算作有效结论，
            # 否则模型纯思考收尾时会跳过最终总结重试，用户什么都看不到。
            # 除"工具跑完却没给结论"外，"只输出思考、既没调工具也没给正文"同样是
            # 模型未完成本轮任务的表现（常见于模型本打算调工具却只输出了 reasoning），
            # 也纳入催促重试，避免把整段思考当作回合结果草草收尾。
            needs_final_summary = (
                not tool_calls_list
                and not raw_content_meaningful
                and (tool_activity_seen or promoted_reasoning)
            )
            # 只有真正要走重试分支时才跳过持久化（重试会 pop 掉这条消息）；
            # 重试机会用尽后的最终消息必须照常入库，否则内存与 DB 分叉
            will_retry_summary = needs_final_summary and final_summary_retry_count == 0

            if stream and not will_retry_summary:
                pending_persistence.append(("assistant", persisted_message))

            if not tool_calls_list:
                if will_retry_summary:
                    final_summary_retry_count += 1
                    # 上一轮因 max_tokens 截断（finish_reason == "length"）导致正文
                    # 未输出时，用相同上限重试几乎必然再次把预算全烧在思考上。
                    # 临时翻倍输出上限（封顶 32768）并在催促文案中要求压缩思考，
                    # 提高重试成功率。
                    length_truncated = finish_reason == "length"
                    if length_truncated:
                        if "max_completion_tokens" in kwargs:
                            _cur_max = int(kwargs.get("max_completion_tokens") or 8192)
                            kwargs["max_completion_tokens"] = min(_cur_max * 2, 32768)
                        else:
                            _cur_max = int(kwargs.get("max_tokens") or 8192)
                            kwargs["max_tokens"] = min(_cur_max * 2, 32768)
                    length_hint = (
                        "上一轮回复因输出长度限制被截断：请大幅压缩思考过程，"
                        "尽快开始输出正式内容。"
                        if length_truncated else ""
                    )
                    # 空 assistant 消息（content 为 None）不入库也不留内存：
                    # 留在历史里会让下一轮 API 报 400（Anthropic 拒绝空 content）
                    messages.pop()
                    if tool_activity_seen:
                        nudge_text = (
                            "工具已经执行完毕。现在必须只输出最终中文检查结论，"
                            "说明发现的问题或下一步建议；不要调用工具，也不要只输出标点或 Markdown 标记。"
                            + length_hint
                        )
                    else:
                        # 模型只输出思考、未调用任何工具也未给出正文：催促它真正行动，
                        # 而不是把内心独白当成交付结果。
                        nudge_text = (
                            "你刚才只输出了思考过程，既没有调用工具执行任何操作，"
                            "也没有给出正式回复。请立即继续：需要执行操作就调用相应工具，"
                            "任务已完成就直接输出中文结论；不要只输出思考内容。"
                            + length_hint
                        )
                    summary_request = {
                        "role": "user",
                        "content": nudge_text,
                    }
                    messages.append(summary_request)
                    # 该 user 催促消息也需要持久化，否则下一轮重建历史时上下文会缺失
                    pending_persistence.append(("user", summary_request))
                    # 内部兜底重试静默进行：底部状态栏已显示"正在干活..."，
                    # 这里不再发 status 通知暴露"模型未生成结论"的内部细节。
                    continue
                # 计划仍有可执行项时不允许提前收尾：向模型注入续推消息，
                # 要求继续通过 run_subagent 完成剩余计划项（最多续推 2 次，
                # 避免模型坚持收尾时与续推消息无限打转）。
                if allow_subagents and parent_session and plan_continue_nudges < 2:
                    try:
                        from core.plan import get_default_plan_manager as _get_plan_for_nudge
                        _nudge_plan = _get_plan_for_nudge()
                        _nudge_ready = [
                            item for item in _nudge_plan.ready_items()
                            if item.get("kind") != "input_preparation"
                        ] if _nudge_plan.session_id == str(parent_session) else []
                        _nudge_running = any(
                            item.get("status") == "in_progress"
                            for item in _nudge_plan.snapshot()
                        ) if _nudge_plan.session_id == str(parent_session) else False
                    except Exception:
                        _nudge_ready, _nudge_running = [], False
                    if _nudge_ready and not _nudge_running:
                        plan_continue_nudges += 1
                        hints = ", ".join(
                            f"'{item['id']}'({item['subject']})" for item in _nudge_ready[:5]
                        )
                        nudge_request = {
                            "role": "user",
                            "content": (
                                f"计划尚未执行完毕，仍有可执行的计划项: [{hints}]。"
                                "请继续调用 run_subagent(plan_item_id=...) 执行剩余计划项；"
                                "若某项确实无法完成，请先用 plan_read 查看状态并明确说明原因，"
                                "不要在计划未完成时直接结束回答。"
                            ),
                        }
                        messages.append(nudge_request)
                        pending_persistence.append(("user", nudge_request))
                        _notify_event(event_callback, "notice", {
                            "message": "计划仍有未执行项，已提醒模型继续执行。",
                        })
                        continue
                # 模型未调用工具且未生成任何正文：给出明确提示，避免 UI 在
                # "思考完成" 后陷入沉默（常见于某些模型只输出 reasoning 或空回复）。
                if not _is_meaningful_assistant_content(public_content):
                    fallback_msg = "模型本轮未返回有效正文内容（可能只输出了思考过程或空回复）。"
                    if reasoning_content and reasoning_content.strip():
                        fallback_msg += " 如需查看思考过程，可尝试换用支持推理展示的模型或重新提问。"
                    _notify_event(event_callback, "notice", {"message": fallback_msg})
                # 促升的思考正文此前只写入了历史、从未推送到 UI——用户会看到
                # 回合默默结束什么都没输出。此处补推给界面流式显示。
                if promoted_reasoning and public_content:
                    yield from _yield_content(public_content)
                flush_persistence()
                return

            # 输出被 max_tokens 截断时，工具参数几乎必然不完整（JSON 修复只能补全
            # 语法，无法找回被截断的内容）。此时绝不能执行任何工具——尤其是
            # write_file/edit_file 会把半截内容写进文件。改为向模型返回错误结果，
            # 让它拆小参数后重新调用。
            if finish_reason == "length":
                truncated_err = (
                    f"{TOOL_ERROR_PREFIX} 本次工具调用参数因输出长度限制被截断，未执行。"
                    "请把参数拆小（例如分多次 edit_file、缩短 content）后重新调用。"
                )
                for tc in tool_calls_list:
                    synth = {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": truncated_err,
                    }
                    messages.append(synth)
                    if stream:
                        pending_persistence.append(("tool", synth))
                flush_persistence()
                _notify_event(event_callback, "notice", {
                    "message": "模型输出被长度限制截断，已拦截本批工具调用并要求模型拆小参数重试。",
                })
                continue

            tool_activity_seen = True
            if event_callback is None:
                app_context.set_task_status('executing')
                app_context.invalidate_ui()
            else:
                _notify_event(event_callback, "status", {"state": "executing"})

            def execute_in_context(tool_call):
                return _execute_single_tool_call(
                    tool_call,
                    event_callback=event_callback,
                    parent_session=parent_session,
                    parent_turn_id=parent_turn_id,
                    cancel_event=cancel_event,
                    allow_subagents=allow_subagents,
                    permission_owner=permission_owner,
                    permission_source=permission_source,
                )

            # 只把显式声明为 parallel_safe 的相邻工具放入同一并发批次。
            batches = []
            for tc in tool_calls_list:
                func_name = tc["function"]["name"]
                try:
                    is_parallel = tool_registry.get(func_name).parallel_safe
                except KeyError:
                    is_parallel = False
                if batches and batches[-1][0] and is_parallel:
                    batches[-1][1].append(tc)
                else:
                    batches.append((is_parallel, [tc]))

            for is_parallel, batch in batches:
                raise_if_cancelled()

                for tc in batch:
                    _notify_event(event_callback, "tool_requested", _tool_event_payload(tc))
                for tc in batch:
                    _notify_event(event_callback, "tool_started", _tool_event_payload(tc))
                yield ""

                if is_parallel and len(batch) > 1:
                    # 并发上限固定为 4，结果仍按模型工具调用顺序写回。
                    # 不用 with 语句：with 的 __exit__ 会 shutdown(wait=True)，
                    # 取消时会卡住 UI 直到所有并行工具（可能是长时子代理）跑完。
                    from concurrent.futures import ThreadPoolExecutor
                    executor = ThreadPoolExecutor(max_workers=min(len(batch), 4))
                    try:
                        results = list(executor.map(execute_in_context, batch))
                    finally:
                        executor.shutdown(wait=False, cancel_futures=True)
                    messages.extend(results)
                    for tc, res in zip(batch, results):
                        arguments = _tool_call_arguments(tc)
                        _notify_event(event_callback, "tool_finished", {
                            **_tool_event_payload(tc),
                            "ok": _tool_result_ok(tc["function"]["name"], res),
                            "summary": _tool_output_preview(
                                tc["function"]["name"], arguments, res
                            ),
                            # 完整工具结果（截断）：GUI 需要展开查看，TUI 忽略此字段
                            "result": _tool_result_text(res),
                        })
                    if stream:
                        pending_persistence.extend(("tool", res) for res in results)
                else:
                    # 修改类工具：串行处理以保证授权及状态顺序
                    for tc in batch:
                        raise_if_cancelled()
                        res = execute_in_context(tc)
                        messages.append(res)
                        arguments = _tool_call_arguments(tc)
                        _notify_event(event_callback, "tool_finished", {
                            **_tool_event_payload(tc),
                            "ok": _tool_result_ok(tc["function"]["name"], res),
                            "summary": _tool_output_preview(
                                tc["function"]["name"], arguments, res
                            ),
                            # 完整工具结果（截断）：GUI 需要展开查看，TUI 忽略此字段
                            "result": _tool_result_text(res),
                        })
                        if stream:
                            pending_persistence.append(("tool", res))
                yield ""
            
            if event_callback is None:
                app_context.set_task_status('executing')
                app_context.invalidate_ui()
            else:
                _notify_event(event_callback, "status", {"state": "executing"})
            
        # 超过轮次上限：提示消息写入历史并与正常结束一样落库，避免下一轮丢失上下文
        msg = f"{TOOL_ERROR_PREFIX} 任务轮次超过上限（100 轮），请把任务拆小一点再试！"
        limit_message = {"role": "assistant", "content": msg}
        messages.append(limit_message)
        if stream:
            pending_persistence.append(("assistant", deepcopy(limit_message)))
        flush_persistence()
        yield from _yield_content(msg)
        return

    except KeyboardInterrupt:
        # 关闭底层 HTTP 流：否则每次取消都会泄漏一个连接/套接字
        try:
            response_stream.close()
        except Exception:
            pass
        # 用户中断：安全回退删除此轮 Turn 添加的一切消息残影
        rollback_turn_messages()
        raise
    except Exception as e:
        try:
            response_stream.close()
        except Exception:
            pass
        # 异常路径同样回滚本轮未持久化的内存消息：既避免留下带 tool_calls 却无
        # tool 响应的悬空 assistant 消息（下一轮 API 会 400），也保持内存历史与 DB 一致
        rollback_turn_messages()
        if event_callback is not None:
            _notify_event(event_callback, "error", {
                "message": str(e),
            })
            # 专用结束事件：chat_with_model_events 不再为异常回合发 turn_finished
            _notify_event(event_callback, "turn_error", {
                "message": str(e),
            })
        else:
            from prompt_toolkit.formatted_text import HTML

            safe_print(HTML("<ansiyellow>星瑶暂未完成当前步骤</ansiyellow>"))
        return
