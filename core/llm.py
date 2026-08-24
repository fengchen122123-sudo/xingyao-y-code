import json
import hashlib
import logging
import os
import queue
import random
import re
import subprocess
import threading
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit
# Python 3.10 及以下 concurrent.futures.TimeoutError 不是内建 TimeoutError 的别名，
# 权限等待轮询必须同时捕获两者，否则等待期直接崩溃
from concurrent.futures import Future, TimeoutError as _FutureTimeoutError
from copy import deepcopy
from typing import Dict, List

from core.harness.request_envelope import (
    PreparedStreamRequest,
    RequestEnvelope,
    _responses_input_from_messages,
    _responses_tools_from_chat_tools,
    prepare_stream_request,
)
from core.harness.journal import journal_storage_supported
from core.harness.runtime import TurnRuntime

# 使用 LiteLLM 本地模型价格表，跳过启动时的远程拉取
# （raw.githubusercontent.com 国内常超时，本地副本无功能影响，且启动更快）
# 必须在 import litellm 之前设置该环境变量才生效
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# 屏蔽 LiteLLM 启动时的调试噪音，但保留 WARNING 及以上的有用告警
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
logging.getLogger("litellm").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _submit_daemon_call(function, *args, **kwargs):
    """Run a blocking call without adding a non-daemon interpreter-exit hook."""
    future = Future()

    def runner():
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)

    threading.Thread(target=runner, daemon=True, name="ycode-blocking-call").start()
    return future

# ---------------------------------------------------------------------------
# 在飞 LLM 请求硬中断注册表
# 协作式取消（cancel_event）只能让主循环在下一个检查点退出，阻塞中的 HTTP 读
# 仍要等到数据到达或超时（随载荷放大，封顶数百秒），用户点“停止”后连接仍在静默
# 接收。这里把每个在飞流登记到对应 cancel_event 名下，停止路径调用
# abort_inflight_streams() 直接关闭底层流，让阻塞读立即抛错退出。
# 铁律：只关闭单个 response 流，绝不触碰 litellm 共享的连接池/client；
# 关闭动作本身绝不向外抛异常（中断路径不能制造新故障）。
# ---------------------------------------------------------------------------
_inflight_lock = threading.Lock()
_inflight_streams: Dict["threading.Event", List["_InflightStream"]] = {}


def _close_stream_quietly(stream):
    """尽力关闭单个流对象（不触碰共享 client/连接池），吞掉一切异常。

    litellm 的 CustomStreamWrapper 没有 close()，真正的可关闭对象藏在
    .completion_stream（openai SDK Stream，close() 会关底层 httpx response）
    或 .response（httpx.Response）里，逐层探测。
    """
    seen = set()
    queue = [stream]
    while queue:
        target = queue.pop(0)
        if target is None or id(target) in seen:
            continue
        seen.add(id(target))
        close = getattr(target, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
            return
        queue.append(getattr(target, "completion_stream", None))
        queue.append(getattr(target, "response", None))


class _InflightStream:
    """一个在飞 LLM 请求的可关闭句柄；attach 前/后调用 close 都安全且幂等。"""

    __slots__ = ("stream", "closed")

    def __init__(self):
        self.stream = None
        self.closed = False

    def attach(self, stream):
        with _inflight_lock:
            if not self.closed:
                self.stream = stream
                return
        # attach 前已被 abort：立即关闭新建流，避免泄漏连接
        _close_stream_quietly(stream)

    def close(self):
        with _inflight_lock:
            if self.closed:
                return
            self.closed = True
            stream = self.stream
        if stream is not None:
            _close_stream_quietly(stream)


def _register_inflight_stream(cancel_event):
    """请求发出前登记；返回句柄，由流消费方在结束后 _unregister_inflight_stream。"""
    if cancel_event is None:
        return None
    holder = _InflightStream()
    with _inflight_lock:
        _inflight_streams.setdefault(cancel_event, []).append(holder)
    return holder


def _unregister_inflight_stream(cancel_event, holder):
    if cancel_event is None or holder is None:
        return
    with _inflight_lock:
        holders = _inflight_streams.get(cancel_event)
        if not holders:
            return
        try:
            holders.remove(holder)
        except ValueError:
            pass
        if not holders:
            _inflight_streams.pop(cancel_event, None)


def abort_inflight_streams(cancel_event):
    """硬中断指定取消事件名下的全部在飞 LLM 请求（停止按钮/Ctrl+C 路径调用）。

    调用方必须先 set cancel_event 再调用本函数：这样被关闭一侧的读循环
    会把中断识别为用户取消（KeyboardInterrupt 打捞路径），而非网络错误重试。
    """
    if cancel_event is None:
        return
    with _inflight_lock:
        holders = list(_inflight_streams.get(cancel_event, ()))
    for holder in holders:
        try:
            holder.close()
        except Exception:
            pass

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


def _patch_litellm_stream_usage_details(_litellm):
    """修复 litellm 1.83.7 流式 usage 丢失 prompt_tokens_details 的问题。

    部分 OpenAI 兼容通道（如 DeepSeek 中转）流式最终 chunk 的 usage 以 dict 形式
    穿过 CustomStreamWrapper，litellm 在组装响应时只取 prompt/completion/total
    三个字段重建 Usage，prompt_tokens_details.cached_tokens 被丢弃，
    导致 ycode 的缓存命中记账恒为 0（状态栏“命中 0 (0%)”）。
    这里把 dict usage 提前转成 litellm.Usage（其构造器保留全部明细字段），
    下游 isinstance(usage, Usage) 分支即原样透传。
    升级 litellm 后若官方已修复，此补丁无害（转换是幂等的）。
    """
    try:
        from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    except Exception:
        return
    if getattr(CustomStreamWrapper, _STREAM_USAGE_PATCH_FLAG, False):
        return
    _original = CustomStreamWrapper.handle_openai_chat_completion_chunk

    def _patched(self, chunk):
        response_obj = _original(self, chunk)
        if response_obj is None:
            return response_obj
        usage = response_obj.get("usage")
        if isinstance(usage, dict) and (
            "prompt_tokens_details" in usage or "prompt_cache_hit_tokens" in usage
        ):
            try:
                response_obj["usage"] = _litellm.Usage(**usage)
            except Exception:
                pass
        return response_obj

    CustomStreamWrapper.handle_openai_chat_completion_chunk = _patched
    setattr(CustomStreamWrapper, _STREAM_USAGE_PATCH_FLAG, True)


_STREAM_USAGE_PATCH_FLAG = "_ycode_stream_usage_patched"


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
        _patch_litellm_stream_usage_details(_litellm)
        _litellm_module = _litellm
        return _litellm


def _litellm_completion(**kwargs):
    """litellm.completion 的懒加载包装。"""
    return _get_litellm().completion(**kwargs)


def _litellm_responses(**kwargs):
    """litellm.responses 的懒加载包装。"""
    return _get_litellm().responses(**kwargs)


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
# 计划模式硬拦截：只读工具（requires_permission=False）不受影响；
# 沙盒外只读访问仍由 _sandbox_outside_guard 逐次询问
PLAN_MODE_DENIED_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 当前处于计划模式：只允许读取信息和制定计划，"
    "不允许执行任何修改/命令。请改用 plan_write 把操作步骤写入计划，"
    "并提示用户切换到「变更前确认」「自动编辑」或「完全访问」模式后再执行。"
)
# 计划模式下仍然放行的规划类工具（其余需要权限的工具一律拦截）
_PLAN_MODE_ALLOWED_TOOLS = frozenset({
    "plan_write", "plan_read", "plan_update", "plan_retry", "ask_user",
})
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
# 注意：core.mcp_client 不在此处顶部 import（mcp 包会连带 pydantic/httpx/anyio，
# 冷启动约 1.1s）。mcp_manager 通过下方模块级 __getattr__ 惰性加载，首次访问时
# 才 import core.mcp_client；启动时的预热由 main.py 的 _preload_mcp 后台线程负责。
from core.permission_broker import CANCELLED_RESULT, permission_broker
from core.command_policy import (
    _session_rule_is_exact,
    command_matches_session_rule,
    session_rule_for_command,
)
from core.reasoning_stream import ReasoningStreamParser, split_reasoning_text
from core.harness.stream_assembler import StreamAssembler, complete_thinking_blocks
from core.tool_presenter import (
    build_tool_display,
    summarize_tool_finished,
    summarize_tool_started,
)
from core.turn_events import TurnEventFactory
import core.app_context as app_context
from core import permission_store

# 会话授权白名单（当前会话生命周期内有效，切换会话时自动清理）
# 模块级三个集合是"未绑定线程"（CLI 主流程 / 测试主线程）的默认桶；
# 多会话并行下，绑定了线程会话的回合（GUI 回合/子代理线程）使用
# _SESSION_GRANTS 里各自会话的专属桶，互不可见（详见 _session_grants_bucket）。
session_allowed_commands: set[tuple[str, ...]] = set()  # 结构化命令规则
session_allowed_write_files = set()   # 放行的写入文件绝对路径
session_allowed_edit_files = set()    # 放行的修改文件绝对路径
# 内联/复合命令的「本会话精确放行」：点“总是允许”后仅对完全相同的命令免问，
# 不做前缀扩展、不落盘（防投毒）。结构化命令仍走上面的 commands 桶。
session_allowed_exact_commands: set[str] = set()
_SESSION_GRANTS: dict[str, dict[str, set]] = {}
_SESSION_GRANTS_MAX_SESSIONS = 128
_last_tracked_session = None
_raw_permission_lock = threading.Lock()
# 保护会话放行白名单与 _last_tracked_session 的跨线程读写
_session_whitelist_lock = threading.Lock()


def _session_grants_bucket() -> dict[str, set] | None:
    """当前线程会话的授权桶；未绑定线程返回 None（调用方走模块级默认集合）。

    桶结构：{"commands": set, "write_files": set, "edit_files": set, "exact_commands": set}
    """
    sid = app_context.get_thread_session()
    if not sid:
        return None
    with _session_whitelist_lock:
        bucket = _SESSION_GRANTS.get(sid)
        if bucket is None:
            if len(_SESSION_GRANTS) >= _SESSION_GRANTS_MAX_SESSIONS:
                _SESSION_GRANTS.pop(next(iter(_SESSION_GRANTS)), None)
            bucket = {"commands": set(), "write_files": set(), "edit_files": set(), "exact_commands": set()}
            _SESSION_GRANTS[sid] = bucket
        return bucket


def clear_session_grants(session_id: str) -> None:
    """清空指定会话的授权桶（会话删除/测试隔离时调用）。"""
    with _session_whitelist_lock:
        _SESSION_GRANTS.pop(str(session_id), None)


def _grant_command_add(prefix: tuple[str, ...]) -> None:
    bucket = _session_grants_bucket()
    with _session_whitelist_lock:
        if bucket is None:
            session_allowed_commands.add(prefix)
        else:
            bucket["commands"].add(prefix)


def _grant_write_add(canonical_path: str) -> None:
    bucket = _session_grants_bucket()
    with _session_whitelist_lock:
        if bucket is None:
            session_allowed_write_files.add(canonical_path)
        else:
            bucket["write_files"].add(canonical_path)


def _grant_edit_add(canonical_path: str) -> None:
    bucket = _session_grants_bucket()
    with _session_whitelist_lock:
        if bucket is None:
            session_allowed_edit_files.add(canonical_path)
        else:
            bucket["edit_files"].add(canonical_path)


def _grant_command_hit(command: str) -> bool:
    bucket = _session_grants_bucket()
    with _session_whitelist_lock:
        prefixes = session_allowed_commands if bucket is None else bucket["commands"]
        return any(_command_matches_prefix(command, prefix) for prefix in prefixes)


def _grant_exact_command_add(command: str) -> None:
    bucket = _session_grants_bucket()
    key = str(command or "").strip()
    if not key:
        return
    with _session_whitelist_lock:
        if bucket is None:
            session_allowed_exact_commands.add(key)
        else:
            bucket["exact_commands"].add(key)


def _grant_exact_command_hit(command: str) -> bool:
    bucket = _session_grants_bucket()
    key = str(command or "").strip()
    if not key:
        return False
    with _session_whitelist_lock:
        s = session_allowed_exact_commands if bucket is None else bucket["exact_commands"]
        return key in s


def _grant_write_hit(canonical_path: str) -> bool:
    bucket = _session_grants_bucket()
    with _session_whitelist_lock:
        paths = session_allowed_write_files if bucket is None else bucket["write_files"]
        return canonical_path in paths


def _grant_edit_hit(canonical_path: str) -> bool:
    bucket = _session_grants_bucket()
    with _session_whitelist_lock:
        paths = session_allowed_edit_files if bucket is None else bucket["edit_files"]
        return canonical_path in paths


def _canonical_permission_path(value):
    # 相对路径按线程本地 workdir 解析（GUI 多会话并行时进程 cwd 可能停在
    # 安装目录，裸 abspath 会把相对路径规范化到错误位置，白名单授予与命中
    # 错位）；绝对路径行为与原 realpath+normcase 一致。
    try:
        from core.tools import _resolve_against_workdir

        return os.path.normcase(str(_resolve_against_workdir(value)))
    except (OSError, ValueError, TypeError):
        return ""
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
    # 多会话并行：本线程绑定了执行会话时，授权已按会话分桶、互不可见，
    # UI 切换全局会话（用户只是去别的会话看一眼）不再收回运行中回合的授权。
    # 仅未绑定线程（CLI 单会话主流程）保留"切会话即收回"的旧语义。
    if app_context.get_thread_session():
        return
    with _session_whitelist_lock:
        current = app_context.get_current_session()
        if current and _last_tracked_session != current:
            _last_tracked_session = current
            session_allowed_commands.clear()
            session_allowed_write_files.clear()
            session_allowed_edit_files.clear()
            session_allowed_exact_commands.clear()
            reset_session_scope = True
        else:
            reset_session_scope = False
    if reset_session_scope:
        # 会话级沙盒放行与三个白名单同生命周期：切会话一并收回
        from core.tools import set_session_sandbox_bypass

        set_session_sandbox_bypass(False)


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
    """线程安全地在 CLI 终端输出格式化文本，避免破坏屏幕布局"""
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
    纯文本原始终端兜底：只在确实没有活动 CLI App 时才被调用（例如管道重定向或早期初始化时）。
    不使用任何 ANSI 转义序列或 msvcrt 菜单，直接用 print/input 避免污染 CLI 屏幕。
    """
    mode_display = {
        "confirm": "每次确认",
        "accept_edits": "自动编辑",
        "plan": "计划确认",
        "full": "完全访问",
    }
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
            # 沙盒外路径访问可会话级放行，但不做持久化落盘
            print("请输入 [y]es 同意本次, [n]o 拒绝, [a]lways 本会话内放行沙盒外访问")
            res = input(f"批准该操作？(y/n/a, 模式:{mode_tip}, 默认 y): ").strip().lower()
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
                    _grant_command_add(prefix)
                    print(f"[当前会话已放行] {_format_session_command_rule(prefix)}")
            elif function_name == "write_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    _grant_write_add(_canonical_permission_path(file_path))
                    print(f"[当前会话已放行写入]：{file_path}")
            elif function_name == "edit_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    _grant_edit_add(_canonical_permission_path(file_path))
                    print(f"[当前会话已放行修改]：{file_path}")
            elif function_name == "sandbox_outside_access":
                # 会话级放行（不落盘）：本会话内后续沙盒外访问免询问
                from core.tools import set_session_sandbox_bypass

                set_session_sandbox_bypass(True)
                print("[当前会话已放行沙盒外访问]")
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

    # 1. 检查当前会话/当前项目中是否已被用户“始终允许”
    #    会话白名单（内存）与项目持久化白名单（~/.ycode/permissions.json）依次命中
    if function_name == "execute_command":
        cmd = arguments.get("command", "")
        session_hit = _grant_command_hit(cmd)
        exact_hit = _grant_exact_command_hit(cmd)
        persistent_hit = False
        if not session_hit:
            try:
                rules = permission_store.project_rules()
                persistent_hit = any(
                    _command_matches_prefix(cmd, prefix)
                    for prefix in rules["commands"]
                )
            except Exception:
                persistent_hit = False
        if session_hit or exact_hit or persistent_hit:
            scope_tip = "当前会话" if (session_hit or exact_hit) else "项目白名单（持久化）"
            if event_callback is None:
                from prompt_toolkit.formatted_text import HTML
                safe_print(HTML(f"<ansigreen>{scope_tip}自动放行命令：{cmd}</ansigreen>"))
            else:
                _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
            return True
    elif function_name == "write_file":
        file_path = arguments.get("file_path", "")
        canonical_path = _canonical_permission_path(file_path)
        session_hit = _grant_write_hit(canonical_path)
        persistent_hit = False
        if not session_hit:
            try:
                rules = permission_store.project_rules()
                persistent_hit = canonical_path in rules["write_files"]
            except Exception:
                persistent_hit = False
        if session_hit or persistent_hit:
            scope_tip = "当前会话" if session_hit else "项目白名单（持久化）"
            if event_callback is None:
                from prompt_toolkit.formatted_text import HTML
                safe_print(HTML(f"<ansigreen>{scope_tip}自动放行写入：{file_path}</ansigreen>"))
            else:
                _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
            return True
    elif function_name == "edit_file":
        file_path = arguments.get("file_path", "")
        canonical_path = _canonical_permission_path(file_path)
        session_hit = _grant_edit_hit(canonical_path)
        persistent_hit = False
        if not session_hit:
            try:
                rules = permission_store.project_rules()
                persistent_hit = canonical_path in rules["edit_files"]
            except Exception:
                persistent_hit = False
        if session_hit or persistent_hit:
            scope_tip = "当前会话" if session_hit else "项目白名单（持久化）"
            if event_callback is None:
                from prompt_toolkit.formatted_text import HTML
                safe_print(HTML(f"<ansigreen>{scope_tip}自动放行修改：{file_path}</ansigreen>"))
            else:
                _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
            return True
    elif function_name == "sandbox_outside_access":
        # 会话级沙盒放行（用户此前选过“本会话内放行沙盒外访问”）：
        # 免询问直接通过；切会话即收回，不做持久化
        from core.tools import is_session_sandbox_bypass_enabled

        if is_session_sandbox_bypass_enabled():
            if event_callback is None:
                from prompt_toolkit.formatted_text import HTML
                safe_print(HTML("<ansigreen>当前会话已放行沙盒外访问</ansigreen>"))
            else:
                _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
            return True

    # 2. 模式级快路径
    #    full：沙盒内全部放行；accept_edits：工作区内文件编辑放行（命令仍询问）。
    #    sandbox_outside_access（沙盒外路径访问）不受任何模式自动放行，
    #    必须逐次询问用户（或走上面的会话级放行），确保沙盒外操作始终受控。
    _mode = app_context.get_permission_mode()
    if _mode == "full" and function_name != "sandbox_outside_access":
        if event_callback is not None:
            _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
        return True
    if _mode == "accept_edits" and function_name in ("write_file", "edit_file"):
        # 工作区外的 write/edit 不会以 write_file/edit_file 名义到达这里
        # （pipeline 已先转给沙盒 guard 询问 sandbox_outside_access），
        # 因此此分支放行的只有工作区内编辑
        if event_callback is not None:
            _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
        return True

    # 3. 计划模式的修改/执行类操作不会走到这里（在 _execute_single_tool_call
    #    中已被 PLAN_MODE_DENIED_MESSAGE 硬拦截）；走到这里的只剩
    #    sandbox_outside_access 等必须逐次询问的场景，直接进入询问流程。

    _notify_event(event_callback, "permission_requested", {
        "function_name": function_name,
        "arguments": dict(arguments),
        "source": permission_source,
    })

    # GUI 模式：使用已注册的 GUI 权限回调，避免阻塞在 input()
    _gui_cb = app_context.get_permission_callback()
    if _gui_cb is not None:
        return _gui_cb(function_name, arguments)

    # cli 模式：通过 prompt_toolkit app 或终端 input()
    # GUI 模式没有 prompt_toolkit App，但已在 permission_broker 上注册了处理器，
    # 必须直接走下面的 broker 路径，绝不能退回阻塞式 input()。
    _app = app_context.get_app_instance()
    if (not _app or not _app.loop) and app_context.get_ui_flavor() != "gui":
        with _raw_permission_lock:
            return ask_in_raw_terminal(function_name, arguments)

    # owner/turn_id 统一来源：优先显式传入，turn 兜底 owner，线程名最后兜底
    owner_id = str(permission_owner or parent_turn_id or threading.current_thread().name)
    turn_id = str(parent_turn_id or owner_id)
    # 沙盒外路径访问提供会话级放行（不落盘、切会话收回、任何模式都不自动放行）
    _options = None
    if function_name == "sandbox_outside_access":
        _options = (
            "Yes, 同意本次操作 (y)",
            "No, 拒绝本次操作 (n)",
            "Always, 本会话内放行沙盒外访问 (a)",
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
            2: "\033[96m✨ 已选择始终允许此类操作\033[0m"
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
            safe_print(ANSI(f"   👉 确认结果: {choices_map.get(choice_idx, choices_map[0])}"))
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
                    # 内联/复合命令：无法安全提取前缀，改为「本会话精确放行」——
                    # 仅对完全相同的命令免问，不做前缀扩展、不落盘（防投毒）。
                    _grant_exact_command_add(cmd)
                    if event_callback is None:
                        safe_print(ANSI("   ✨ [本会话已放行此命令（不持久化）]"))
                    else:
                        _notify_event(event_callback, "status", {
                            "message": "已在本会话内放行此命令（不持久化，仅对完全相同的命令生效）。",
                        })
                else:
                    _grant_command_add(prefix)
                    persisted = False
                    try:
                        persisted = permission_store.add_command_rule(prefix)
                    except Exception:
                        persisted = False
                    if event_callback is None:
                        if persisted:
                            safe_print(ANSI(f"   ✨ [项目白名单已放行（持久化）] {_format_session_command_rule(prefix)}"))
                        else:
                            safe_print(ANSI(f"   ✨ [当前会话已放行] {_format_session_command_rule(prefix)}"))
            elif function_name == "write_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    canonical = _canonical_permission_path(file_path)
                    _grant_write_add(canonical)
                    persisted = False
                    try:
                        persisted = permission_store.add_write_file(canonical)
                    except Exception:
                        persisted = False
                    if event_callback is None:
                        scope_tip = "项目白名单（持久化）" if persisted else "当前会话"
                        safe_print(ANSI(f"   ✨ [{scope_tip}已放行写入]：{file_path}"))
            elif function_name == "edit_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    canonical = _canonical_permission_path(file_path)
                    _grant_edit_add(canonical)
                    persisted = False
                    try:
                        persisted = permission_store.add_edit_file(canonical)
                    except Exception:
                        persisted = False
                    if event_callback is None:
                        scope_tip = "项目白名单（持久化）" if persisted else "当前会话"
                        safe_print(ANSI(f"   ✨ [{scope_tip}已放行修改]：{file_path}"))
            elif function_name == "sandbox_outside_access":
                # 会话级放行：不落盘，切会话即收回（见 enqueue 处选项说明）
                from core.tools import set_session_sandbox_bypass

                set_session_sandbox_bypass(True)
                if event_callback is None:
                    safe_print(ANSI("   ✨ [当前会话已放行沙盒外访问]"))
            return True

        approved = (choice_idx in (0, 2))
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
    "cumulative_cache_prompt": 0,
    
    # 窗口占用（用来防止爆 128k 上下文）
    "latest_total": 0,
    "latest_prompt": 0,
    "latest_cached": 0,

    # 缓存遥测（仅主代理镜像到状态栏；按会话独立累计）
    "cache_latest_rate": 0.0,
    "cache_hot_rate": 0.0,
    "cache_cumulative_rate": 0.0,
    "cache_hot_turns": 0,
    "cache_cumulative_cached": 0,
    "cache_invalidation_reason": "",
    
    "max_window": 128000  # 默认最大上下文窗口配置
}
# session_token_usage 会被后台 worker 线程与 UI 线程同时读写，所有读写必须走锁
token_usage_lock = threading.Lock()

_cache_request_states = {}
_cache_usage_states = {}


def _cache_usage_key(session_id, model_name, api_base, cache_scope="main"):
    """生成缓存遥测身份；同会话的不同模型、接口与作用域绝不合并。"""
    return (
        str(session_id),
        str(model_name or "").lower(),
        str(api_base or "").rstrip("/").lower(),
        str(cache_scope or "main"),
    )


def _cache_message_fingerprints(messages):
    fingerprints = []
    for message in messages or []:
        normalized = {
            key: value
            for key, value in message.items()
            if key != "cache_control"
        }
        payload = _canonical_diagnostic_json(normalized)
        fingerprints.append(hashlib.sha256(payload.encode("utf-8")).hexdigest())
    return tuple(fingerprints)


def _canonical_diagnostic_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _build_cache_request_diagnostic(
    session_id,
    cache_scope,
    model_name,
    api_base,
    tools,
    messages,
):
    """构建不含正文的确定性请求形状诊断。"""
    normalized_messages = [
        {key: value for key, value in message.items() if key != "cache_control"}
        for message in (messages or [])
    ]
    system_messages = [
        message for message in normalized_messages
        if message.get("role") == "system"
    ]
    normalized_tools = list(tools or [])
    messages_json = _canonical_diagnostic_json(normalized_messages)
    system_json = _canonical_diagnostic_json(system_messages)
    tools_json = _canonical_diagnostic_json(normalized_tools)
    return {
        "session_id": str(session_id),
        "model": str(model_name or "").lower(),
        "api_base": str(api_base or "").rstrip("/").lower(),
        "cache_scope": str(cache_scope or "main"),
        "messages_sha256": hashlib.sha256(messages_json.encode("utf-8")).hexdigest(),
        "system_sha256": hashlib.sha256(system_json.encode("utf-8")).hexdigest(),
        "tools_sha256": hashlib.sha256(tools_json.encode("utf-8")).hexdigest(),
        "message_count": len(normalized_messages),
        "system_message_count": len(system_messages),
        "request_bytes": len(messages_json.encode("utf-8")),
        "system_bytes": len(system_json.encode("utf-8")),
        "tools_bytes": len(tools_json.encode("utf-8")),
        "_message_fingerprints": _cache_message_fingerprints(normalized_messages),
    }


def _classify_cache_request_details(
    session_id,
    cache_scope,
    model_name,
    api_base,
    tool_contract,
    messages,
    tools=None,
):
    """记录请求形状并返回可持久化的原因、哈希与公共前缀长度。"""
    state_key = (str(session_id), str(cache_scope or "main"))
    details = _build_cache_request_diagnostic(
        session_id, cache_scope, model_name, api_base, tools, messages
    )
    current = {
        "model": details["model"],
        "api_base": details["api_base"],
        "tool_contract": str(tool_contract or details["tools_sha256"]),
        "messages": details["_message_fingerprints"],
    }
    with token_usage_lock:
        previous = _cache_request_states.get(state_key)
        common_prefix = 0
        if previous is not None:
            for old_hash, new_hash in zip(previous["messages"], current["messages"]):
                if old_hash != new_hash:
                    break
                common_prefix += 1
        if previous is None:
            reason = "冷启动"
        elif (
            previous["model"] != current["model"]
            or previous["api_base"] != current["api_base"]
        ):
            reason = "模型或接口变化"
        elif previous["tool_contract"] != current["tool_contract"]:
            reason = "工具契约变化"
        elif common_prefix != len(previous["messages"]):
            reason = "上下文压缩或历史重写"
        else:
            reason = ""
        _cache_request_states[state_key] = current
        if len(_cache_request_states) > 128:
            _cache_request_states.pop(next(iter(_cache_request_states)))
    details["request_reason"] = reason
    details["common_prefix_messages"] = common_prefix
    del details["_message_fingerprints"]
    return details


def _classify_cache_request(
    session_id,
    cache_scope,
    model_name,
    api_base,
    tool_contract,
    messages,
):
    """记录请求形状并返回确定性的缓存失效原因；空字符串表示严格追加。"""
    return _classify_cache_request_details(
        session_id,
        cache_scope,
        model_name,
        api_base,
        tool_contract,
        messages,
    )["request_reason"]


def _record_cache_usage(
    session_id,
    cache_scope,
    prompt_tokens,
    cached_tokens,
    invalidation_reason,
    model_name="",
    api_base="",
):
    """分别累计当前轮、热启动与会话总命中率，并返回该通道的最新快照。"""
    if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        raise ValueError("prompt_tokens must be a non-negative integer")
    if isinstance(cached_tokens, bool) or not isinstance(cached_tokens, int) or cached_tokens < 0:
        raise ValueError("cached_tokens must be a non-negative integer")
    if cached_tokens > prompt_tokens:
        raise ValueError("cached_tokens cannot exceed prompt_tokens")
    prompt = prompt_tokens
    cached = cached_tokens
    state_key = _cache_usage_key(
        session_id, model_name, api_base, cache_scope
    )
    with token_usage_lock:
        state = _cache_usage_states.setdefault(state_key, {
            "cumulative_prompt": 0,
            "cumulative_cached": 0,
            "hot_prompt": 0,
            "hot_cached": 0,
            "hot_turns": 0,
            "latest_prompt": 0,
            "latest_cached": 0,
            "usage_valid": True,
            "usage_error": "",
            "invalidation_reason": "",
        })
        state["latest_prompt"] = prompt
        state["latest_cached"] = cached
        state["cumulative_prompt"] += prompt
        state["cumulative_cached"] += cached
        if not invalidation_reason:
            state["hot_prompt"] += prompt
            state["hot_cached"] += cached
            state["hot_turns"] += 1
        state["usage_valid"] = True
        state["usage_error"] = ""
        state["invalidation_reason"] = str(invalidation_reason or "")

        snapshot = dict(state)
        snapshot["latest_rate"] = (cached / prompt * 100) if prompt else 0.0
        snapshot["hot_rate"] = (
            state["hot_cached"] / state["hot_prompt"] * 100
            if state["hot_prompt"] else 0.0
        )
        snapshot["cumulative_rate"] = (
            state["cumulative_cached"] / state["cumulative_prompt"] * 100
            if state["cumulative_prompt"] else 0.0
        )
        snapshot["invalidation_reason"] = str(invalidation_reason or "")

        if str(cache_scope or "main") == "main":
            session_token_usage["cache_latest_rate"] = snapshot["latest_rate"]
            session_token_usage["cache_hot_rate"] = snapshot["hot_rate"]
            session_token_usage["cache_cumulative_rate"] = snapshot["cumulative_rate"]
            session_token_usage["cache_hot_turns"] = snapshot["hot_turns"]
            session_token_usage["cache_cumulative_cached"] = snapshot["cumulative_cached"]
            session_token_usage["cache_invalidation_reason"] = snapshot["invalidation_reason"]
        return snapshot


def _mark_cache_usage_unavailable(
    session_id,
    model_name,
    api_base,
    cache_scope="main",
    reason="usage 不可用",
):
    """标记最新缓存 usage 不可用，同时保留此前已验证的累计计数。"""
    state_key = _cache_usage_key(
        session_id, model_name, api_base, cache_scope
    )
    with token_usage_lock:
        state = _cache_usage_states.setdefault(state_key, {
            "cumulative_prompt": 0,
            "cumulative_cached": 0,
            "hot_prompt": 0,
            "hot_cached": 0,
            "hot_turns": 0,
            "latest_prompt": 0,
            "latest_cached": 0,
            "usage_valid": False,
            "usage_error": "",
            "invalidation_reason": "",
        })
        state["usage_valid"] = False
        state["usage_error"] = str(reason or "usage 不可用")
        if str(cache_scope or "main") == "main":
            session_token_usage["cache_invalidation_reason"] = state["usage_error"]


def get_token_usage_snapshot(
    session_id,
    model_name,
    api_base,
    cache_scope="main",
):
    """返回线程安全的 token 与当前缓存身份快照，不暴露内部可变状态。"""
    state_key = _cache_usage_key(
        session_id, model_name, api_base, cache_scope
    )
    with token_usage_lock:
        state = _cache_usage_states.get(state_key)
        window = {
            "latest_total": int(session_token_usage.get("latest_total", 0) or 0),
            "latest_prompt": int(session_token_usage.get("latest_prompt", 0) or 0),
            "latest_cached": int(session_token_usage.get("latest_cached", 0) or 0),
            "max_window": int(session_token_usage.get("max_window", 0) or 0),
        }
        process = {
            "cumulative_total": int(session_token_usage.get("cumulative_total", 0) or 0),
            "cumulative_prompt": int(session_token_usage.get("cumulative_prompt", 0) or 0),
            "cumulative_completion": int(
                session_token_usage.get("cumulative_completion", 0) or 0
            ),
            "cumulative_cached": int(session_token_usage.get("cumulative_cached", 0) or 0),
            "cumulative_cache_prompt": int(
                session_token_usage.get("cumulative_cache_prompt", 0) or 0
            ),
        }
        if state is None:
            cache = {
                "available": False,
                "usage_valid": False,
                "latest": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
                "hot": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
                "session_all": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
                "hot_turns": 0,
                "invalidation_reason": "",
            }
        else:
            valid = bool(state.get("usage_valid", True))

            def _rate(cached_value, prompt_value):
                if not valid or not prompt_value:
                    return None
                return cached_value / prompt_value * 100

            latest_prompt = int(state.get("latest_prompt", 0) or 0)
            latest_cached = int(state.get("latest_cached", 0) or 0)
            hot_prompt = int(state.get("hot_prompt", 0) or 0)
            hot_cached = int(state.get("hot_cached", 0) or 0)
            cumulative_prompt = int(state.get("cumulative_prompt", 0) or 0)
            cumulative_cached = int(state.get("cumulative_cached", 0) or 0)
            cache = {
                "available": valid,
                "usage_valid": valid,
                "latest": {
                    "prompt_tokens": latest_prompt,
                    "cached_tokens": latest_cached,
                    "rate": _rate(latest_cached, latest_prompt),
                },
                "hot": {
                    "prompt_tokens": hot_prompt,
                    "cached_tokens": hot_cached,
                    "rate": _rate(hot_cached, hot_prompt),
                },
                "session_all": {
                    "prompt_tokens": cumulative_prompt,
                    "cached_tokens": cumulative_cached,
                    "rate": _rate(cumulative_cached, cumulative_prompt),
                },
                "hot_turns": int(state.get("hot_turns", 0) or 0),
                "invalidation_reason": str(
                    state.get("usage_error")
                    if not valid
                    else state.get("invalidation_reason", "")
                ),
            }
    return {"window": window, "process": process, "cache": cache}


def rename_cache_usage_session(old_name, new_name):
    """会话重命名时原子迁移缓存请求形状与 usage 遥测。"""
    old_key = str(old_name)
    new_key = str(new_name)
    if old_key == new_key:
        return
    with token_usage_lock:
        for mapping in (_cache_request_states, _cache_usage_states):
            moves = [
                (key, (new_key, *key[1:]), value)
                for key, value in list(mapping.items())
                if key and key[0] == old_key
            ]
            for source, target, value in moves:
                mapping[target] = value
                del mapping[source]


def clear_cache_usage_session(session_name):
    """清理指定会话的进程内缓存遥测，防止同名新会话继承旧状态。"""
    session_key = str(session_name)
    with token_usage_lock:
        for mapping in (_cache_request_states, _cache_usage_states):
            for key in [key for key in mapping if key and key[0] == session_key]:
                del mapping[key]


def _persist_token_usage(model_name, prompt_tokens, completion_tokens,
                         cached_tokens, total_tokens, session_id=None,
                         api_base="", cache_scope="legacy"):
    """把本轮真实 token 用量追加到本地 token_usage 表，供 /usage 按模型/日期统计。

    失败仅记日志，绝不影响对话主流程。model_name 为 litellm 模型串
    （如 "openai/deepseek-v4-pro"），按第一个 "/" 拆出 provider 与 model。
    """
    try:
        from core import app_context
        manager = app_context.get_manager()
        if manager is None:
            return
        full_name = str(model_name or "")
        if "/" in full_name:
            provider, model = full_name.split("/", 1)
        else:
            provider, model = "", full_name
        manager.add_token_usage(
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            session_id=session_id,
            api_base=api_base,
            cache_scope=cache_scope,
        )
    except Exception as _persist_err:
        try:
            logger.debug("token 用量持久化失败（已忽略）: %s", _persist_err)
        except Exception:
            pass


_USAGE_FIELD_MISSING = object()


def _usage_field(value, name):
    """读取 dict 或 SDK 对象字段，并保留“缺失”与显式 None/0 的区别。"""
    if isinstance(value, dict):
        return value[name] if name in value else _USAGE_FIELD_MISSING
    if value is None:
        return _USAGE_FIELD_MISSING
    return getattr(value, name, _USAGE_FIELD_MISSING)


def _optional_nonnegative_int(raw, field_name, errors):
    """把服务端整数规范为 int；拒绝会丢精度或掩盖异常的隐式转换。"""
    if raw is _USAGE_FIELD_MISSING or raw is None:
        return None
    if isinstance(raw, bool):
        errors.append(f"{field_name} must be a non-negative integer")
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    else:
        errors.append(f"{field_name} must be a non-negative integer")
        return None
    if value < 0:
        errors.append(f"{field_name} must be a non-negative integer")
        return None
    return value


def _normalize_server_usage(usage):
    """归一化并严格校验一次服务端 usage，不估算或夹断任何真实 token。"""
    errors = []
    prompt_raw = _usage_field(usage, "prompt_tokens")
    completion_raw = _usage_field(usage, "completion_tokens")
    total_raw = _usage_field(usage, "total_tokens")

    # Anthropic 协议端点（Kimi /coding、聚合网关等）返回 input_tokens/
    # output_tokens 且通常不返回 total_tokens。OpenAI 字段缺失时改读
    # anthropic 字段兜底，避免整条记账被 "prompt_tokens is required" 拒绝。
    if prompt_raw is _USAGE_FIELD_MISSING or prompt_raw is None:
        anthropic_prompt = _usage_field(usage, "input_tokens")
        if anthropic_prompt is not _USAGE_FIELD_MISSING and anthropic_prompt is not None:
            prompt_raw = anthropic_prompt
    if completion_raw is _USAGE_FIELD_MISSING or completion_raw is None:
        anthropic_completion = _usage_field(usage, "output_tokens")
        if anthropic_completion is not _USAGE_FIELD_MISSING and anthropic_completion is not None:
            completion_raw = anthropic_completion

    prompt = _optional_nonnegative_int(prompt_raw, "prompt_tokens", errors)
    completion = _optional_nonnegative_int(
        completion_raw, "completion_tokens", errors
    )
    total = _optional_nonnegative_int(total_raw, "total_tokens", errors)

    if prompt_raw is _USAGE_FIELD_MISSING or prompt_raw is None:
        errors.append("prompt_tokens is required")
    if total_raw is _USAGE_FIELD_MISSING or total_raw is None:
        # Anthropic 协议不返回 total_tokens：由 input+output 推导，不算缺失。
        if prompt is not None and completion is not None:
            total = prompt + completion
        else:
            errors.append("total_tokens is required")

    details = _usage_field(usage, "prompt_tokens_details")
    details_cached_raw = _usage_field(details, "cached_tokens")
    direct_cached_raw = _usage_field(usage, "prompt_cache_hit_tokens")
    anthropic_cached_raw = _usage_field(usage, "cache_read_input_tokens")
    miss_raw = _usage_field(usage, "prompt_cache_miss_tokens")

    details_cache_reported = (
        details_cached_raw is not _USAGE_FIELD_MISSING
        and details_cached_raw is not None
    )
    direct_cache_reported = (
        direct_cached_raw is not _USAGE_FIELD_MISSING
        and direct_cached_raw is not None
    )
    details_cached = _optional_nonnegative_int(
        details_cached_raw, "prompt_tokens_details.cached_tokens", errors
    )
    direct_cached = _optional_nonnegative_int(
        direct_cached_raw, "prompt_cache_hit_tokens", errors
    )
    if (
        details_cache_reported
        and direct_cache_reported
        and details_cached is not None
        and direct_cached is not None
        and details_cached != direct_cached
    ):
        errors.append("cached token sources disagree")

    if details_cache_reported:
        cached_raw = details_cached_raw
    elif direct_cache_reported:
        cached_raw = direct_cached_raw
    else:
        cached_raw = anthropic_cached_raw
    cache_reported = cached_raw is not _USAGE_FIELD_MISSING and cached_raw is not None
    miss_reported = miss_raw is not _USAGE_FIELD_MISSING and miss_raw is not None

    cached = _optional_nonnegative_int(cached_raw, "cached_tokens", errors)
    cache_miss = _optional_nonnegative_int(miss_raw, "cache_miss_tokens", errors)

    completion_was_reported = (
        completion_raw is not _USAGE_FIELD_MISSING and completion_raw is not None
    )
    if not completion_was_reported and prompt is not None and total is not None:
        if total < prompt:
            errors.append("total_tokens is smaller than prompt_tokens")
        else:
            completion = total - prompt

    if (
        prompt is not None
        and completion is not None
        and total is not None
        and prompt + completion != total
    ):
        errors.append("prompt + completion does not equal total_tokens")
    if cached is not None and prompt is not None and cached > prompt:
        errors.append("cached_tokens exceeds prompt_tokens")
    if miss_reported and not cache_reported:
        errors.append("cache miss reported without cache hit")
    if (
        cache_reported
        and miss_reported
        and cached is not None
        and cache_miss is not None
        and prompt is not None
        and cached + cache_miss != prompt
    ):
        errors.append("cache hit + miss does not equal prompt_tokens")

    return {
        "valid": not errors,
        "errors": tuple(errors),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached if cache_reported else None,
        "cache_miss_tokens": cache_miss if miss_reported else None,
        "cache_reported": cache_reported,
    }


def _extract_cached_tokens(usage):
    """兼容旧调用方：读取缓存命中 token；未报告时返回 0。"""
    record = _normalize_server_usage(usage)
    return int(record["cached_tokens"] or 0)


def _persist_cache_request_diagnostic(request_details, usage):
    """补齐 usage 并持久化请求诊断；失败不影响回复或正式 token 记账。"""
    if not request_details:
        logger.warning("缓存请求诊断缺少请求形状，已跳过持久化")
        return
    if usage is None:
        usage_record = {
            "valid": False,
            "errors": ("usage missing",),
            "prompt_tokens": None,
            "cached_tokens": None,
            "cache_miss_tokens": None,
            "cache_reported": False,
        }
    else:
        usage_record = _normalize_server_usage(usage)
    diagnostic = dict(request_details)
    diagnostic.update({
        "prompt_tokens": usage_record["prompt_tokens"],
        "cached_tokens": usage_record["cached_tokens"],
        "cache_miss_tokens": usage_record["cache_miss_tokens"],
        "cache_reported": usage_record["cache_reported"],
        "usage_valid": usage_record["valid"],
        "usage_errors": tuple(usage_record["errors"]),
    })
    try:
        manager = app_context.get_manager()
        if manager is not None:
            manager.add_cache_request_diagnostic(diagnostic)
    except Exception as exc:
        logger.warning("缓存请求诊断持久化失败: %s", exc)


def _account_server_usage(
    usage,
    *,
    model_name,
    api_base,
    session_id,
    cache_scope,
    invalidation_reason,
):
    """校验并一次性记账最终 usage；无效记录绝不进入正式统计。"""
    record = _normalize_server_usage(usage)
    if not record["valid"]:
        reason = "usage 不一致: " + "; ".join(record["errors"])
        if str(cache_scope or "main") == "main":
            _mark_cache_usage_unavailable(
                session_id, model_name, api_base, cache_scope, reason
            )
        logger.warning(
            "拒绝 token 记账 model=%s api_base=%s session=%s scope=%s errors=%s raw=%r",
            model_name,
            api_base,
            session_id,
            cache_scope,
            record["errors"],
            usage,
        )
        return None

    prompt_tokens = record["prompt_tokens"]
    completion_tokens = record["completion_tokens"]
    total_tokens = record["total_tokens"]
    cached_tokens = record["cached_tokens"]
    cache_reported = record["cache_reported"]
    main_scope = str(cache_scope or "main") == "main"

    with token_usage_lock:
        session_token_usage["cumulative_total"] += total_tokens
        session_token_usage["cumulative_prompt"] += prompt_tokens
        session_token_usage["cumulative_completion"] += completion_tokens
        if cache_reported:
            session_token_usage["cumulative_cached"] += cached_tokens
            session_token_usage["cumulative_cache_prompt"] += prompt_tokens
        if main_scope:
            session_token_usage["latest_total"] = total_tokens
            session_token_usage["latest_prompt"] = prompt_tokens
            session_token_usage["latest_cached"] = cached_tokens or 0

    if main_scope:
        if cache_reported:
            _record_cache_usage(
                session_id,
                cache_scope,
                prompt_tokens,
                cached_tokens,
                invalidation_reason,
                model_name,
                api_base,
            )
        else:
            _mark_cache_usage_unavailable(
                session_id,
                model_name,
                api_base,
                cache_scope,
                "服务端未报告缓存 token 明细",
            )

    _persist_token_usage(
        model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        session_id=session_id,
        api_base=api_base,
        cache_scope=cache_scope,
    )
    return record

# The registry is the canonical capability metadata source.
from core.tool_registry import build_default_registry
tool_registry = build_default_registry()


_mcp_first_wait_done = False


def _get_mcp_manager():
    """惰性获取 mcp_manager 单例：import core.mcp_client 会拉起 mcp 包全家桶
    （pydantic/httpx/anyio，冷启动约 1.1s），故延迟到首次实际使用时才加载。
    每次调用时读取属性，不做缓存，保证测试 monkeypatch core.mcp_client.mcp_manager 生效。"""
    from core.mcp_client import mcp_manager
    return mcp_manager


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
            _get_mcp_manager()._ensure_connected()
        except Exception:
            pass
    return tool_registry.api_schemas() + _get_mcp_manager().get_schemas()


def completion(**kwargs):
    """litellm.completion 的模块级导出的包装函数（可被测试 mocker.patch 拦截）。"""
    return _litellm_completion(**kwargs)


def _obj_value(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _chat_chunk(*, content="", reasoning="", tool_calls=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls or [],
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _responses_usage_as_chat_usage(response):
    usage = _obj_value(response, "usage")
    if usage is None:
        return None
    input_details = _obj_value(usage, "input_tokens_details")
    cached_tokens = int(_obj_value(input_details, "cached_tokens", 0) or 0)
    prompt_tokens = int(_obj_value(usage, "input_tokens", 0) or 0)
    completion_tokens = int(_obj_value(usage, "output_tokens", 0) or 0)
    total_tokens = int(
        _obj_value(usage, "total_tokens", prompt_tokens + completion_tokens)
        or prompt_tokens + completion_tokens
    )
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )


def _responses_stream_as_chat_chunks(events):
    """Responses 原始流 → chat chunks 的映射；生成器关闭时连带关闭底层流。"""
    try:
        yield from _responses_events_as_chat_chunks(events)
    finally:
        _close_stream_quietly(events)


def _responses_events_as_chat_chunks(events):
    """将 Responses SSE 事件严格映射成现有工具循环消费的 chat chunks。"""
    saw_tool_calls = False
    ignored = {
        "response.created",
        "response.in_progress",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.done",
        "response.refusal.done",
        "response.output_text.annotation.added",
        "response.function_call_arguments.done",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
    }
    for event in events:
        event_type = str(_obj_value(event, "type", ""))
        if event_type == "response.output_text.delta":
            yield _chat_chunk(content=str(_obj_value(event, "delta", "")))
        elif event_type == "response.reasoning_summary_text.delta":
            yield _chat_chunk(reasoning=str(_obj_value(event, "delta", "")))
        elif event_type == "response.refusal.delta":
            yield _chat_chunk(content=str(_obj_value(event, "delta", "")))
        elif event_type == "response.output_item.added":
            item = _obj_value(event, "item")
            if _obj_value(item, "type") != "function_call":
                continue
            saw_tool_calls = True
            output_index = int(_obj_value(event, "output_index", 0) or 0)
            call_id = _obj_value(item, "call_id") or _obj_value(item, "id")
            name = _obj_value(item, "name")
            if not call_id or not name:
                raise RuntimeError("Responses function_call 缺少 call_id 或 name")
            tool_call = SimpleNamespace(
                index=output_index,
                id=str(call_id),
                function=SimpleNamespace(name=str(name), arguments=""),
            )
            yield _chat_chunk(tool_calls=[tool_call])
        elif event_type == "response.function_call_arguments.delta":
            saw_tool_calls = True
            tool_call = SimpleNamespace(
                index=int(_obj_value(event, "output_index", 0) or 0),
                id=None,
                function=SimpleNamespace(
                    name="",
                    arguments=str(_obj_value(event, "delta", "")),
                ),
            )
            yield _chat_chunk(tool_calls=[tool_call])
        elif event_type == "response.completed":
            response = _obj_value(event, "response")
            yield _chat_chunk(
                finish_reason="tool_calls" if saw_tool_calls else "stop",
                usage=_responses_usage_as_chat_usage(response),
            )
        elif event_type == "response.incomplete":
            response = _obj_value(event, "response")
            details = _obj_value(response, "incomplete_details")
            reason = str(_obj_value(details, "reason", ""))
            if reason == "max_output_tokens":
                yield _chat_chunk(
                    finish_reason="length",
                    usage=_responses_usage_as_chat_usage(response),
                )
                continue
            raise RuntimeError(f"Responses 请求未完成: {reason or '未知原因'}")
        elif event_type in {"response.failed", "error"}:
            response = _obj_value(event, "response", event)
            error = _obj_value(response, "error", response)
            message = _obj_value(error, "message") or str(error)
            raise RuntimeError(f"Responses 请求失败: {message}")
        elif event_type in ignored:
            continue
        else:
            raise RuntimeError(f"未支持的 Responses 流事件: {event_type or '<empty>'}")


def _request_stream(prepared_or_protocol, kwargs=None, *, timeout=None, cancel_event=None):
    """按显式协议发起流请求；Responses 失败时禁止降级。

    返回 (stream, holder)：holder 登记在 cancel_event 名下的在飞注册表，
    供停止路径硬中断；由调用方在流消费结束后 unregister。
    """
    if isinstance(prepared_or_protocol, PreparedStreamRequest):
        if kwargs is not None or timeout is not None:
            raise TypeError("PreparedStreamRequest already contains provider kwargs and timeout")
        prepared = prepared_or_protocol
    else:
        if kwargs is None or timeout is None:
            raise TypeError("legacy request calls require kwargs and timeout")
        prepared = prepare_stream_request(prepared_or_protocol, kwargs, timeout)

    holder = _register_inflight_stream(cancel_event)
    try:
        if prepared.protocol != "responses":
            stream = completion(**prepared.kwargs)
            if holder is not None:
                holder.attach(stream)
            return stream, holder
        # 可关闭对象是底层原始流，不是外层映射生成器（生成器在执行中无法 close）
        raw_stream = _litellm_responses(**prepared.kwargs)
        if holder is not None:
            holder.attach(raw_stream)
        return _responses_stream_as_chat_chunks(raw_stream), holder
    except Exception:
        # 请求未建立：登记无意义，立即摘除避免空句柄滞留注册表
        _unregister_inflight_stream(cancel_event, holder)
        raise


def __getattr__(name):
    # 兼容别名：惰性计算，避免 import 时触发 MCP 连接
    if name == "TOOLS_SCHEMA":
        return get_tools_schema()
    if name == "litellm":
        return _get_litellm()
    if name == "mcp_manager":
        # 惰性加载 mcp_manager 单例：避免 import core.llm 时就拉起 mcp 包全家桶
        # （~1.1s 启动开销）。不缓存到 globals，保证外部属性访问始终反映
        # core.mcp_client.mcp_manager 的当前值（测试 monkeypatch 友好）。
        return _get_mcp_manager()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# estimate_tokens 结果缓存：tiktoken 全量编码整个历史相当慢（长会话可达秒级），
# 而 UI 状态栏、压缩判断等会在内容未变时反复调用。用 (模型, 消息数, 总字符数)
# 作为签名，未变化时直接复用上次结果。
_token_estimate_cache = {"sig": None, "value": 0}
# 前缀增量缓存：append-only 历史每轮只新增尾部消息，
# 记录前缀（消息数/字符总长/token 数），下一轮免全量 tokenize
_token_prefix_cache = {"model": None, "msg_count": 0, "char_len": 0, "value": 0}
_token_cache_lock = threading.RLock()


def _sanitize_error_text(text):
    """异常文本入 UI / 落库前脱敏。

    litellm / 网关异常文本常携带完整请求 URL 与请求上下文（query 里的 api_key、
    Authorization: Bearer 头、URL userinfo 等），直接发 error/turn_error 事件会把
    凭据泄露到界面与会话记录。这里对 URL 凭据段与常见密钥模式做不可逆脱敏。
    """
    text = str(text or "")
    # URL userinfo：scheme://user:password@host → scheme://user:***@host
    text = re.sub(
        r"([a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", r"\1***\2", text,
        flags=re.IGNORECASE,
    )
    # key=value / key: value 形式的敏感字段（含 query string 里的 api_key=...）
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|authorization)\s*[=:]\s*)[^\s,;&\"']+",
        r"\1***",
        text,
    )
    # Authorization: Bearer xxx
    text = re.sub(r"(?i)\bBearer\s+[^\s\"',;]+", "Bearer ***", text)
    # 常见 provider 密钥前缀
    return re.sub(
        r"\b(?:sk|ghp|gho|ghu|ghs|ghr|glpat|hf|xoxb|xoxp|xoxa|AKIA|ASIA|AIza)[-_][A-Za-z0-9_.-]{6,}\b",
        "***",
        text,
    )


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
    """本地离线快速估算 Token 数，防止超载崩溃（带签名缓存 + 前缀增量缓存）

    大会话（10 万+ token）全量 tokenize 一次要数秒，而历史是 append-only 的，
    每轮只有尾部几条消息是新的。前缀增量缓存记录“前 K 条消息”的计数结果，
    下一轮只对新增尾部计数，把每轮估算开销从秒级降到毫秒级。
    前缀一致性用字符总长校验（估算场景容忍极小概率的等长突变误判）。
    """
    if not messages:
        return 0
    # 两份缓存包含互相关联的字段，必须在一个临界区内读取、计算并提交，
    # 否则并发模型会形成“模型 A 的签名 + 模型 B 的值”的混合快照。
    with _token_cache_lock:
        return _estimate_tokens_locked(model_name, messages)


def _estimate_tokens_locked(model_name, messages):
    try:
        # 签名必须含内容哈希：仅用 (模型, 消息数, 总字符数) 时，等长内容突变
        # （改写历史但长度不变）会命中旧缓存，返回过期的 token 估算
        parts = [str(m.get("content") or "") for m in messages]
        total_len = sum(len(p) for p in parts)
        sig = (str(model_name), len(messages), total_len, hash(tuple(parts)))
    except Exception:
        sig = None
    if sig is not None and _token_estimate_cache.get("sig") == sig:
        return _token_estimate_cache["value"]

    def _count(msgs):
        try:
            # 历史图片先折叠再计数：base64 字符量与图片真实 token 消耗无关
            return _litellm_token_counter(model_name, _fold_old_media_for_estimate(msgs))
        except Exception:
            # 如果模型不支持或报错，采用约 2 字符 / Token 的经验值进行兜底估算
            return len(str(_fold_old_media_for_estimate(msgs))) // 2

    value = None
    pc = _token_prefix_cache
    if (
        pc.get("model") == str(model_name)
        and 0 < pc.get("msg_count", 0) < len(messages)
    ):
        prefix = messages[: pc["msg_count"]]
        try:
            prefix_len = sum(len(str(m.get("content") or "")) for m in prefix)
        except Exception:
            prefix_len = -1
        if prefix_len == pc.get("char_len"):
            # 前缀未变：只对新追加的尾部消息计数，加上已缓存的前缀计数
            value = pc["value"] + _count(messages[pc["msg_count"]:])
    if value is None:
        value = _count(messages)
    if sig is not None:
        _token_estimate_cache["sig"] = sig
        _token_estimate_cache["value"] = value
        _token_prefix_cache["model"] = str(model_name)
        _token_prefix_cache["msg_count"] = len(messages)
        _token_prefix_cache["char_len"] = total_len
        _token_prefix_cache["value"] = value
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
    cache_scope="main",
    cache_session_id=None,
):
    """Yield ordered :class:`TurnEvent` snapshots for one model turn.

    The legacy ``chat_with_model`` API remains text-oriented. This adapter is
    the event-oriented entry point used by the CLI and is deliberately kept
    separate while the existing tool loop is migrated incrementally.
    """
    resolved_turn_id = turn_id or f"turn-{uuid.uuid4().hex[:8]}"
    resolved_parent_turn_id = parent_turn_id or resolved_turn_id
    # Duck-type 管理器（未实现 Journal 存储接口的旧测试替身/外部调用方）
    # 不进入 Harness Journal，退回 legacy add_messages 持久化路径；
    # 真实 MemoryManager 始终具备完整接口，照常走 Journal。
    _manager = app_context.get_manager() if persist else None
    if persist and not journal_storage_supported(_manager):
        harness_runtime = None
    else:
        harness_runtime = TurnRuntime.start(
            resolved_turn_id,
            parent_session,
            persist=bool(persist),
            manager=_manager,
            cancel_event=cancel_event,
            clock_ms=lambda: time.time_ns() // 1_000_000,
        )
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
        cache_scope=cache_scope,
        cache_session_id=cache_session_id or parent_session,
        harness_runtime=harness_runtime,
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
        if harness_runtime is not None:
            harness_runtime.cancel()
        yield from drain_public_events()
        yield factory.emit("turn_cancelled", {})
        raise
    except Exception as exc:
        # 生成器前置区（记忆注入/通知 drain/请求构造）抛出的异常会穿透内部
        # try 直接传播到这里：补发 turn_error，保证调用方能感知回合失败
        if harness_runtime is not None:
            harness_runtime.fail(type(exc).__name__)
        yield from drain_public_events()
        yield factory.emit("turn_error", {"message": _sanitize_error_text(exc)})
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


def _is_official_kimi_route(api_base=""):
    """只识别已知支持 Kimi 缓存亲和私有字段的官方 API。"""
    try:
        parsed = urlsplit(str(api_base or ""))
        host = str(parsed.hostname or "").lower()
        path = str(parsed.path or "").rstrip("/").lower()
        port = parsed.port
    except Exception:
        return False
    if (
        str(parsed.scheme or "").lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    if host == "api.kimi.com":
        return path in {"/coding", "/coding/v1"}
    return (
        host in {"api.moonshot.cn", "api.moonshot.ai"}
        and path in {"", "/v1"}
    )


def _is_deepseek_family(model_name, api_base=""):
    model = str(model_name or "").split("/")[-1].lower()
    return "deepseek" in model or _is_official_deepseek_route(api_base)


def _is_official_deepseek_route(api_base=""):
    normalized = str(api_base or "").rstrip("/").lower()
    return normalized in {
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/beta",
    }


def _uses_deepseek_prompt_ledger(
    model_name, api_base, cache_scope, persist, parent_session
):
    """Select the isolated ledger only for official DeepSeek main sessions."""
    return bool(
        persist
        and parent_session
        and str(cache_scope or "main") == "main"
        and _is_deepseek_family(model_name, api_base)
        and _is_official_deepseek_route(api_base)
    )


def _select_request_tools_for_route(model_name, api_base, tools):
    """Canonicalize only DeepSeek tools; preserve every other provider payload."""
    if not (
        _is_deepseek_family(model_name, api_base)
        and _is_official_deepseek_route(api_base)
    ):
        return tools
    selected = deepcopy(list(tools or []))
    return sorted(
        selected,
        key=lambda tool: str(tool.get("function", {}).get("name") or ""),
    )


def _deepseek_stable_header_matches(generation, current_messages, current_tools):
    """Compare the stable request header represented by this OpenAI payload."""
    if generation["tools"] != current_tools:
        return False
    old_messages = generation["messages"]
    if not old_messages or not current_messages:
        return old_messages == current_messages
    old_system = old_messages[0] if old_messages[0].get("role") == "system" else None
    current_system = (
        current_messages[0] if current_messages[0].get("role") == "system" else None
    )
    return old_system == current_system


def _new_tail_after_ledger_prefix(ledger_messages, current_messages):
    """Return the append-only tail without trusting rewritten normal history.

    The common case is a literal prefix.  After normal-history maintenance, the
    immutable ledger prefix may no longer occur in full; in that case its final
    message must occur exactly once in the current request and acts as the
    explicit hand-off boundary.  Ambiguous or missing boundaries fail loudly.
    """
    if current_messages[:len(ledger_messages)] == ledger_messages:
        return deepcopy(current_messages[len(ledger_messages):])
    if not ledger_messages:
        return deepcopy(current_messages)
    boundary = ledger_messages[-1]
    matches = [
        index for index, message in enumerate(current_messages)
        if message == boundary
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "DeepSeek prompt 账本无法与当前历史建立唯一追加边界"
        )
    return deepcopy(current_messages[matches[0] + 1:])


def _prepare_deepseek_ledger_request(
    manager, session_name, model_name, api_base, cache_scope, messages, tools,
    *, force_new=False
):
    """Build and persist the immutable request view for one DeepSeek call."""
    generation = manager.get_or_create_deepseek_prompt_generation(
        session_name,
        model_name,
        api_base,
        cache_scope,
        deepcopy(messages),
        deepcopy(tools),
        force_new=force_new,
    )
    if generation.get("created"):
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
        }
    if not _deepseek_stable_header_matches(generation, messages, tools):
        generation = manager.get_or_create_deepseek_prompt_generation(
            session_name,
            model_name,
            api_base,
            cache_scope,
            deepcopy(messages),
            deepcopy(tools),
            force_new=True,
        )
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
        }
    try:
        tail = _new_tail_after_ledger_prefix(generation["messages"], messages)
    except RuntimeError:
        generation = manager.get_or_create_deepseek_prompt_generation(
            session_name,
            model_name,
            api_base,
            cache_scope,
            deepcopy(messages),
            deepcopy(tools),
            force_new=True,
        )
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
        }
    if tail:
        manager.append_deepseek_prompt_messages(
            generation["id"], tail, expected_count=len(generation["messages"])
        )
        generation["messages"].extend(tail)
    return {
        "generation_id": generation["id"],
        "messages": generation["messages"],
        "tools": generation["tools"],
    }


def _deepseek_thinking_enabled(model_name, api_base="", model_cfg=None):
    if not _is_deepseek_family(model_name, api_base):
        return False
    cfg = model_cfg or {}
    if isinstance(cfg.get("enable_thinking"), bool):
        return cfg["enable_thinking"]
    if not _is_official_deepseek_route(api_base):
        return False
    model = str(model_name or "").split("/")[-1].lower()
    return "v4" in model or "reasoner" in model


def _supports_cache_keepalive(model_name, api_base=""):
    """保活仅适用于已明确验证自动 prompt caching 的 DeepSeek/Kimi 路由。"""
    return (
        _is_kimi_family(model_name, api_base)
        and _is_official_kimi_route(api_base)
    ) or (
        _is_deepseek_family(model_name, api_base)
        and _is_official_deepseek_route(api_base)
    )


def _supports_reasoning_fields(model_name, api_base=""):
    prefix = str(model_name or "").split("/")[0].lower()
    if prefix in _REASONING_FIELD_PROVIDERS:
        return True
    # Kimi 推理模型（含经销商 openai/ 路由）：多轮工具调用要求 assistant
    # 消息保留 reasoning_content，剥掉会导致 400 或推理链断裂
    return _is_kimi_family(model_name, api_base) or _is_deepseek_family(
        model_name, api_base
    )


def _requires_tool_call_reasoning(model_name, api_base=""):
    """DeepSeek thinking 模式：带 tool_calls 的 assistant 必须回传 reasoning_content key。"""
    try:
        from core.model_manager import get_model_config_by_litellm_model
        model_cfg = get_model_config_by_litellm_model(model_name, api_base) or {}
    except Exception:
        model_cfg = {}
    return _deepseek_thinking_enabled(model_name, api_base, model_cfg)


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


def _tool_contract_fingerprint(tools):
    """返回最终工具 schema 的确定性指纹，用于隔离不同请求契约的缓存通道。"""
    payload = json.dumps(
        tools or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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
_vision_support_cache_lock = threading.RLock()


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
    with _vision_support_cache_lock:
        return _supports_vision_locked(model_name)


def _supports_vision_locked(model_name):
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


def _strip_media_and_merge(messages, model_name=None, api_base=""):
    """
    1. 视觉输入处理：支持视觉的模型保留最近一条消息中的图片/视频（多模态负载原样透传），
       更早的历史图片一律折叠为占位符，避免每轮重复发送烧 Token；
       不支持视觉的模型把图片折叠为占位符（绝不把 base64 当文本发给模型）。
       文档/文本文件已在 input_parser 中转为普通文本，此处不视为视觉输入。
    2. 合并连续相同 role 的消息，满足 LiteLLM / Anthropic 对 user/assistant 严格轮流的要求
    3. reasoning_content / thinking_blocks 仅对支持的 provider 完整回传，避免其他
       provider 报 400，同时保持 Kimi 等模型要求的连续 provider 消息信封

    返回 (fixed_messages, folded_media_count, keep_media)。
    """
    keep_reasoning_fields = _supports_reasoning_fields(model_name, api_base)
    keep_thinking_blocks = str(model_name or "").split("/")[0].lower() == "anthropic"
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
        # 支持原生推理字段的 provider 需要收到自己上轮返回的完整消息信封。
        # 是否存在 tool_calls 不能作为删除条件：Kimi OpenAI 路由会在普通 assistant
        # 消息中返回 reasoning_content，Anthropic 路由还可能返回签名 thinking_blocks。
        if keep_reasoning_fields and msg.get("reasoning_content") is not None:
            cleaned_msg["reasoning_content"] = msg["reasoning_content"]
        if keep_reasoning_fields and keep_thinking_blocks and msg.get("thinking_blocks") is not None:
            cleaned_msg["thinking_blocks"] = deepcopy(msg["thinking_blocks"])
        # DeepSeek thinking 契约：带 tool_calls 的 assistant 必须携带
        # reasoning_content 字符串字段。供应商可能不产生 reasoning delta，
        # 此时流组装层会保留显式空串；字段缺失、None 或类型错误仍立即暴露。
        if (
            role == "assistant"
            and cleaned_msg.get("tool_calls")
            and _requires_tool_call_reasoning(model_name, api_base)
            and not isinstance(cleaned_msg.get("reasoning_content"), str)
        ):
            raise ValueError(
                "DeepSeek thinking 工具轮缺少 reasoning_content 字符串字段，"
                "无法安全继续请求"
            )

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


def _resolve_cache_session_identity(cache_session_id=None):
    """解析请求所属会话；后台任务可用捕获值覆盖当前 UI 会话。"""
    global _process_cache_key_fallback
    sid = str(cache_session_id or "").strip()
    if not sid:
        try:
            sid = str(app_context.get_current_session() or "").strip()
        except Exception:
            sid = ""
    if not sid:
        if not _process_cache_key_fallback:
            _process_cache_key_fallback = uuid.uuid4().hex
        sid = _process_cache_key_fallback
    return sid


def _cache_affinity_session_id(
    *,
    cache_scope="main",
    model_name="",
    api_base="",
    tool_contract="",
    cache_session_id=None,
):
    """返回 Kimi prompt 缓存亲和使用的会话级隔离 ID。

    Moonshot 后端是多集群架构，请求带 prompt_cache_key（OpenAI 路由）或
    metadata.user_id（Anthropic 路由）时，网关会把同 ID 的请求优先路由到
    持有其前缀 KV cache 的集群，显著提高缓存命中率（命中部分自动折扣计费）。
    官方建议该值在会话生命周期内保持稳定，会话恢复后沿用原值——正好对应
    app_context 的 current_session；后台任务可显式传入启动时捕获的父会话，避免 UI
    切换会话后串线。原始会话名只参与 SHA-256，不发送给 provider。

    通道维度包含角色、模型、API 路由与工具契约。这样主代理、不同子代理角色、
    不同工具 schema 不会共用同一个 prompt_cache_key。
    """
    sid = _resolve_cache_session_identity(cache_session_id)

    session_hash = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
    lane_material = json.dumps(
        {
            "scope": str(cache_scope or "main"),
            "model": str(model_name or "").lower(),
            "api_base": str(api_base or "").rstrip("/").lower(),
            "tool_contract": str(tool_contract or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    lane_hash = hashlib.sha256(lane_material.encode("utf-8")).hexdigest()[:16]
    return f"ycode-{session_hash}-{lane_hash}"


_process_cache_key_fallback = None

_REASONING_EFFORT_LEVELS = ("low", "high", "max")


def _attach_kimi_request_extras(
    result,
    model_name,
    model_cfg,
    *,
    api_base="",
    cache_scope="main",
    cache_session_id=None,
    tool_contract="",
):
    """给 Kimi 系模型的请求参数附加缓存亲和与推理强度配置（原地修改 result）。

    - 缓存亲和：OpenAI 兼容路由传 prompt_cache_key，Anthropic 路由传 metadata.user_id。
    - reasoning_effort：K3 官方推理强度档位（low/high/max，默认 max），仅 OpenAI
      兼容路由支持；Anthropic 路由的推理强度由 thinking_budget 控制，不注入。
    """
    anthropic_route = str(model_name or "").split("/")[0].lower() == "anthropic"

    session_id = None
    if _is_official_kimi_route(api_base):
        session_id = _cache_affinity_session_id(
            cache_scope=cache_scope,
            model_name=model_name,
            api_base=api_base,
            tool_contract=tool_contract,
            cache_session_id=cache_session_id,
        )
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


def _provider_cache_affinity_options(
    model_name,
    api_base,
    *,
    cache_scope,
    cache_session_id=None,
    tool_contract="",
):
    """仅返回 provider 缓存亲和参数，不改变输出长度或推理配置。"""
    if not _is_kimi_family(model_name, api_base):
        return {}
    result = {}
    _attach_kimi_request_extras(
        result,
        model_name,
        {},
        api_base=api_base,
        cache_scope=cache_scope,
        cache_session_id=cache_session_id,
        tool_contract=tool_contract,
    )
    return result


def _provider_request_options(
    model_name,
    api_base,
    *,
    cache_scope="main",
    cache_session_id=None,
    tool_contract="",
):
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
    deepseek_family = _is_deepseek_family(model_name, api_base)
    deepseek_thinking = _deepseek_thinking_enabled(
        model_name, api_base, model_cfg
    )
    route_prefix = str(model_name or "").split("/")[0].lower()
    anthropic_route = route_prefix == "anthropic"

    result = {}

    if deepseek_family:
        if cfg_max_tokens is not None:
            result["max_tokens"] = int(cfg_max_tokens)
        if _is_official_deepseek_route(api_base) or isinstance(cfg_enable_thinking, bool):
            result.setdefault("extra_body", {})["thinking"] = {
                "type": "enabled" if deepseek_thinking else "disabled"
            }
        if deepseek_thinking:
            effort = str(model_cfg.get("reasoning_effort") or "max").strip().lower()
            if effort not in _REASONING_EFFORT_LEVELS:
                raise ValueError(
                    "DeepSeek reasoning_effort 仅支持 low/high/max，"
                    f"实际为 {effort!r}"
                )
            result["reasoning_effort"] = effort
            # LiteLLM 对尚未内置识别的 OpenAI 兼容模型会在客户端拒绝
            # reasoning_effort。这里按参数白名单精确放行，不能用全局
            # drop_params，否则会静默丢失用户要求的推理强度。
            result["allowed_openai_params"] = ["reasoning_effort"]
    elif cfg_max_tokens is not None or cfg_thinking_budget is not None or cfg_enable_thinking is not None:
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
        _attach_kimi_request_extras(
            result,
            model_name,
            model_cfg,
            api_base=api_base,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id,
            tool_contract=tool_contract,
        )
    return result


def _complete_thinking_blocks(delta):
    """Extract only complete provider blocks that are safe to round-trip."""
    return complete_thinking_blocks(delta)

def _notify_event(event_callback, kind, payload):
    if event_callback is not None:
        event_callback(kind, payload)


def _stream_chunks_with_idle_notice(response_stream, event_callback, raise_if_cancelled, inflight_holder=None, cancel_event=None):
    """在守护线程中消费 LLM 流，主循环用带超时的队列读取。

    直接 for chunk in response_stream 时，首个 chunk 之后流中段 stalled 没有任何
    反馈，最长静默一整个请求超时（随载荷放大，封顶数百秒），用户无法区分
    “慢”与“卡死”。改为后台线程 pump + 主线程带 0.5s 超时的队列读取：
    空闲超阈值后按间隔上报等待状态，同时保持对取消事件的响应能力。
    注意：调用方提前放弃（取消/异常）时 pump 线程会继续读到流结束，线程为
    daemon 且流自身有读超时，不会泄漏存活。
    inflight_holder：本流在在飞注册表中的句柄，消费结束（无论正常/异常/取消）
    统一摘除；用户取消时流已被 abort_inflight_streams 关闭，pump 抛出的读错误
    需经 raise_if_cancelled 归一为用户取消，避免误入网络错误分支。
    """
    chunks_queue: "queue.Queue" = queue.Queue()
    _END = object()

    def _pump():
        try:
            for chunk in response_stream:
                chunks_queue.put((True, chunk))
        except BaseException as err:
            chunks_queue.put((False, err))
        finally:
            chunks_queue.put((False, _END))

    threading.Thread(target=_pump, daemon=True, name="llm-stream-pump").start()
    idle_started_at = time.monotonic()
    last_notice_at = idle_started_at
    try:
        while True:
            raise_if_cancelled()
            try:
                ok, item = chunks_queue.get(timeout=0.5)
            except queue.Empty:
                idle = time.monotonic() - idle_started_at
                if (
                    idle >= WAIT_NOTICE_THRESHOLD_SECONDS
                    and time.monotonic() - last_notice_at >= WAIT_NOTICE_INTERVAL_SECONDS
                ):
                    last_notice_at = time.monotonic()
                    wait_message = (
                        f"模型流式响应中断，已 {int(idle)} 秒未收到新数据，仍在等待连接恢复；"
                        "可按 Ctrl+C 中断"
                    )
                    if event_callback is None:
                        print(f"\n[⏳ {wait_message}]")
                    else:
                        _notify_event(event_callback, "status", {"message": wait_message})
                continue
            idle_started_at = time.monotonic()
            last_notice_at = idle_started_at
            if not ok:
                if item is _END:
                    return
                # 用户取消触发的硬中断会把底层流关掉，pump 随之抛读错误；
                # 先走取消判定，保证归一为 KeyboardInterrupt 打捞路径而非网络错误
                raise_if_cancelled()
                raise item
            yield item
    finally:
        _unregister_inflight_stream(cancel_event, inflight_holder)


def _tool_call_arguments(tc):
    raw_arguments = tc["function"].get("arguments", "{}")
    if not isinstance(raw_arguments, str):
        raise ValueError("工具参数 JSON 必须是字符串")

    try:
        arguments = json.loads(raw_arguments)
    except ValueError as direct_error:
        try:
            arguments = json.loads(
                _escape_control_chars_in_json_strings(raw_arguments)
            )
        except ValueError:
            try:
                arguments = json.loads(_repair_truncated_json(raw_arguments))
            except ValueError as repair_error:
                raise ValueError(
                    f"工具参数 JSON 解析失败：{repair_error}"
                ) from direct_error

    if not isinstance(arguments, dict):
        raise ValueError("工具参数 JSON 必须是对象")
    return arguments


def _tool_event_parts(tool_call):
    if isinstance(tool_call, dict):
        return (
            tool_call.get("id", ""),
            tool_call["function"]["name"],
            _tool_call_arguments(tool_call),
        )
    return tool_call.call_id, tool_call.name, tool_call.arguments


def _tool_event_payload_from_parts(call_id, function_name, arguments):
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
        "id": call_id,
        "name": function_name,
        "preview": str(preview)[:120],
        "arguments": arguments,
        "summary": summarize_tool_started(function_name, arguments),
        "display": build_tool_display(function_name, arguments),
    }


def _tool_event_payload(tool_call):
    return _tool_event_payload_from_parts(*_tool_event_parts(tool_call))


def _tool_finished_event_payload(tool_call, result, duration_ms):
    call_id, function_name, arguments = _tool_event_parts(tool_call)
    ok = _tool_result_ok(function_name, result)
    return {
        **_tool_event_payload_from_parts(call_id, function_name, arguments),
        "ok": ok,
        "summary": _tool_output_preview(function_name, arguments, result),
        "result": _tool_result_text(result),
        "duration_ms": max(0, int(duration_ms)),
        "display": build_tool_display(function_name, arguments, result, ok=ok),
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

    这是附加字段：CLI 只读 summary，多出的 result 对它无副作用。
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


def _show_file_diff(
    file_path,
    old_content,
    new_content,
    event_callback,
    panel_title,
    truncated_note,
    tool_call_id="",
):
    """Compatibility wrapper for the extracted diff adapter."""
    from core.harness.tool_adapters import _show_file_diff as _render_file_diff

    return _render_file_diff(
        file_path,
        old_content,
        new_content,
        event_callback,
        panel_title,
        truncated_note,
        tool_call_id=tool_call_id,
    )

def _get_imom_active_publish_root():
    """Compatibility wrapper for the extracted imom adapter."""
    from core.harness.tool_adapters import _get_imom_active_publish_root as _get_root

    return _get_root()

def _sandbox_outside_guard(
    function_name,
    path_value,
    event_callback,
    permission_owner,
    parent_turn_id,
    permission_source,
    cancel_event,
):
    """Compatibility wrapper for the extracted sandbox adapter."""
    from core.harness.tool_adapters import _sandbox_outside_guard as _guard

    return _guard(
        function_name,
        path_value,
        event_callback,
        permission_owner,
        parent_turn_id,
        permission_source,
        cancel_event,
        permission_checker=check_permission_and_ask,
        is_outside_workspace=is_outside_workspace,
        sandbox_bypass=sandbox_bypass,
        permission_error_type=PermissionSystemError,
    )

def _build_tool_pipeline_and_context(
    event_callback=None,
    *,
    parent_session=None,
    parent_turn_id=None,
    cancel_event=None,
    allow_subagents=True,
    permission_owner=None,
    permission_source="主代理",
):
    from core.harness.tool_adapters import ToolAdapterSet
    from core.harness.tool_pipeline import ToolExecutionContext, ToolPipeline
    from core.tool_registry import ToolRegistry

    bound_execute = tool_registry.execute
    registry_execute_override = (
        bound_execute
        if getattr(bound_execute, "__func__", None) is not ToolRegistry.execute
        else None
    )
    adapters = ToolAdapterSet(
        read_file=read_file,
        write_file=write_file,
        edit_file=edit_file,
        run_subagent=run_subagent,
        manage_task=manage_task,
        show_file_diff=_show_file_diff,
        get_imom_active_publish_root=_get_imom_active_publish_root,
        sandbox_guard=_sandbox_outside_guard,
        sandbox_bypass=sandbox_bypass,
        read_text=_read_text_with_encoding_fallback,
        notify_event=_notify_event,
        mark_plan_turn=_mark_plan_turn,
        registry_execute_override=registry_execute_override,
        is_outside_workspace=is_outside_workspace,
        safe_print=safe_print,
    )
    context = ToolExecutionContext(
        event_callback=event_callback,
        parent_session=parent_session,
        parent_turn_id=parent_turn_id,
        cancel_event=cancel_event,
        allow_subagents=allow_subagents,
        permission_owner=permission_owner,
        permission_source=permission_source,
        permission_checker=check_permission_and_ask,
        registry=tool_registry,
        mcp_manager=_get_mcp_manager(),
    )
    pipeline = ToolPipeline(
        adapters,
        permission_error_type=PermissionSystemError,
        truncate_result=_truncate_tool_result_for_context,
        plan_turn_checker=_is_plan_turn,
        pending_plan_checker=_has_pending_plan,
    )
    return pipeline, context


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
    """Compatibility wrapper around the extracted deterministic pipeline."""
    pipeline, context = _build_tool_pipeline_and_context(
        event_callback,
        parent_session=parent_session,
        parent_turn_id=parent_turn_id,
        cancel_event=cancel_event,
        allow_subagents=allow_subagents,
        permission_owner=permission_owner,
        permission_source=permission_source,
    )
    return pipeline.execute_one(tc, context)


_ORIGINAL_EXECUTE_SINGLE_TOOL_CALL = _execute_single_tool_call

# 旧测试和外部调用方可能仍从 core.llm 导入该内部兼容名；实现只保留在
# 固定 Harness 管线中，避免形成第二套重复调用判定逻辑。
from core.harness.tool_pipeline import RepeatCallGuard as _RepeatCallGuard

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
# read_file 自带行数/字节上限，且编辑流程依赖完整内容，默认不做二次截断。
# 但大文件全文读入是前缀缓存条目膨胀的最大来源（单个结果可达数万 token），
# 在按容量驱逐的网关上更容易被挤掉、失效后的重建成本也更高。可在 config.json
# 顶层设 "read_file_result_max_chars": 24000 启用截断（保头尾），中间锚点丢失
# 可能影响 edit 流程，故默认保持不限。
_TOOL_RESULT_EXEMPT = {"read_file"}


def _read_file_result_limit():
    """read_file 结果入上下文的字符上限；0/缺省 = 不限。配置读取失败时静默不限。"""
    try:
        from core.model_manager import load_config
        return int(load_config().get("read_file_result_max_chars", 0) or 0)
    except Exception:
        return 0


def _truncate_tool_result_for_context(function_name, text):
    if function_name in _TOOL_RESULT_EXEMPT:
        limit = _read_file_result_limit() if function_name == "read_file" else 0
        if not limit or len(text) <= limit:
            return text
        head_len = limit // 2
        tail = text[-(limit - head_len):]
        return (
            f"{text[:head_len]}\n"
            f"…[文件过长，中间省略 {len(text) - limit} 字符。"
            f"如需中间内容请按行号范围重新 read_file]…\n{tail}"
        )
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
    kwargs.update(_provider_cache_affinity_options(
        secondary["litellm_model"],
        secondary["base_url"],
        cache_scope="secondary:vision",
        tool_contract="no-tools",
    ))

    warning_logger = logging.getLogger("py.warnings")

    for attempt in range(max_retries):
        try:
            future = _submit_daemon_call(_litellm_completion, **kwargs)
            try:
                while not future.done():
                    if cancel_event is not None and cancel_event.is_set():
                        future.cancel()
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
    cache_scope="main",
    cache_session_id=None,
    *,
    harness_runtime=None,
):
    """
    内部大模型驱动生成器：一律开启流式传输以对思考标签和工具链参数进行动态清洗和处理。
    """
    from core.model_manager import get_model_protocol

    request_protocol = get_model_protocol(model_name, api_base)
    if request_protocol is None:
        request_protocol = "anthropic" if str(model_name).startswith("anthropic/") else "openai"
    turn_snapshot = deepcopy(messages)
    keepalive_interval = 0
    if cache_scope == "main":
        from core.cache_keepalive import cache_keepalive_service, load_keepalive_interval
        configured_keepalive = load_keepalive_interval()
        # 每次主模型请求先停掉旧快照，防止切换 provider 后仍保活旧模型。
        cache_keepalive_service.register_request({}, interval=0)
        if request_protocol != "responses" and _supports_cache_keepalive(model_name, api_base):
            keepalive_interval = configured_keepalive
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
    # "只思考不产出"时的最终总结重试上限：连续多次欠佳仍尝试补救，
    # 避免长任务中一两轮失败就静默收尾（用户反馈"任务未执行完就结束"）。
    FINAL_SUMMARY_MAX_RETRIES = 2
    # 重复工具调用抑制：同一签名成功 2 次后拦截，随本轮会话循环生命周期
    from core.harness.tool_pipeline import RepeatCallGuard

    repeat_call_guard = RepeatCallGuard()
    # 计划未执行完时的续推次数上限：防止模型反复提前收尾与续推消息互相打转
    plan_continue_nudges = 0
    pending_persistence = []
    persistence_failure = None
    current_step = None
    current_tool_calls = []
    pending_tool_records = {}

    def flush_persistence():
        """把本轮已确认的助手/工具消息按序落库（与正常结束路径一致）。"""
        nonlocal pending_persistence, persistence_failure, pending_tool_records
        if stream and pending_persistence:
            try:
                if harness_runtime is not None:
                    if current_step is None:
                        # 前置阶段产生的通知由首个 Step 一并原子提交。
                        return
                    from core.harness.models import HarnessEventKind, JournalEventDraft
                    from core.harness.tool_pipeline import (
                        ToolExecutionRecord,
                        build_tool_result_event_draft,
                    )

                    tool_calls_by_id = {
                        tool_call.get("id"): tool_call
                        for tool_call in current_tool_calls
                    }
                    events = []
                    for message_index, (role, content) in enumerate(pending_persistence):
                        if role == "assistant":
                            events.append(JournalEventDraft(
                                HarnessEventKind.ASSISTANT_COMMITTED,
                                step_id=current_step.step_id,
                                message_index=message_index,
                            ))
                            continue
                        if role != "tool" or not isinstance(content, dict):
                            continue
                        call_id = content.get("tool_call_id")
                        tool_call = tool_calls_by_id.get(call_id)
                        record = pending_tool_records.get(call_id)
                        if tool_call is None:
                            events.append(JournalEventDraft(
                                HarnessEventKind.TOOL_RESULT_COMMITTED,
                                step_id=current_step.step_id,
                                message_index=message_index,
                            ))
                            continue
                        if record is None:
                            record = ToolExecutionRecord(
                                call_ordinal=current_tool_calls.index(tool_call),
                                message=content,
                                duration_ms=0,
                            )
                        events.append(build_tool_result_event_draft(
                            current_step.step_id,
                            tool_call,
                            record,
                            message_index=message_index,
                            ok=_tool_result_ok(tool_call["function"]["name"], content),
                        ))
                    current_step.turn.journal.commit_messages(
                        current_step.turn.session_name,
                        current_step.turn.turn_id,
                        current_step.step_id,
                        pending_persistence,
                        events,
                    )
                    del pending_persistence[:]
                    pending_tool_records = {}
                    return
                _mgr = app_context.get_manager()
                if _mgr is None:
                    raise RuntimeError("消息持久化失败：存储管理器不可用")
                _mgr.add_messages(parent_session, pending_persistence)
                del pending_persistence[:]
            except Exception as error:
                persistence_failure = error
                raise

    def salvage_persistence_on_cancel():
        """取消时打捞待写队列：带 tool_calls 的 assistant 消息若缺对应 tool
        结果（工具未跑完就被终止），为每个缺失的 tool_call_id 合成取消占位
        结果。保证落库历史对下一轮 API 合法（悬空 tool_calls 会 400），
        同时保住终止前已完成的助手输出与工具结果，重开会话能看到进度。"""
        answered = {
            content.get("tool_call_id")
            for role, content in pending_persistence
            if role == "tool" and isinstance(content, dict)
        }
        for role, content in list(pending_persistence):
            if role != "assistant" or not isinstance(content, dict):
                continue
            for tc in content.get("tool_calls") or []:
                tcid = tc.get("id")
                if tcid and tcid not in answered:
                    answered.add(tcid)
                    pending_persistence.append(("tool", {
                        "role": "tool",
                        "tool_call_id": tcid,
                        "name": (tc.get("function") or {}).get("name"),
                        "content": "[用户终止了对话，该工具未执行完成]",
                    }))

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

    request_tools = _select_request_tools_for_route(
        model_name, api_base, request_tools
    )
    request_tool_contract = _tool_contract_fingerprint(request_tools)
    telemetry_session_id = _resolve_cache_session_identity(cache_session_id)
    deepseek_ledger_enabled = _uses_deepseek_prompt_ledger(
        model_name, api_base, cache_scope, stream, parent_session
    )
    deepseek_ledger_manager = (
        app_context.get_manager() if deepseek_ledger_enabled else None
    )
    if deepseek_ledger_enabled and deepseek_ledger_manager is None:
        raise RuntimeError("DeepSeek prompt 账本不可用：存储管理器未初始化")

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
    kwargs.update(_provider_request_options(
        model_name,
        api_base,
        cache_scope=cache_scope,
        cache_session_id=telemetry_session_id,
        tool_contract=request_tool_contract,
    ))
    deepseek_thinking_request = (
        _is_deepseek_family(model_name, api_base)
        and kwargs.get("extra_body", {}).get("thinking", {}).get("type") == "enabled"
    )
    if not deepseek_thinking_request:
        kwargs["tool_choice"] = "auto"

    # 默认输出长度上限：防止免费/轻量模型（如 glm-4-flash）默认 max_tokens 过小
    # 导致长回复被静默截断。若 provider 已显式配置则尊重其值。
    if "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
        kwargs["max_tokens"] = 8192

    if api_base:
        kwargs["api_base"] = api_base

    # 主模型不识图时，尝试用副模型先分析图片并替换为文字描述。
    vision_user_message = next(
        (message for message in reversed(messages) if message.get("role") == "user"),
        None,
    )
    vision_original_content = (
        deepcopy(vision_user_message.get("content"))
        if vision_user_message is not None
        else None
    )
    _describe_images_with_secondary(messages, model_name, event_callback=event_callback, cancel_event=cancel_event)
    if (
        stream
        and parent_session
        and vision_user_message is not None
        and vision_user_message.get("content") != vision_original_content
    ):
        manager = app_context.get_manager()
        if manager is None:
            raise RuntimeError("视觉 canonical 消息持久化失败：存储管理器不可用")
        replaced = manager.replace_latest_user_message_content(
            parent_session,
            vision_original_content,
            vision_user_message.get("content"),
        )
        if not replaced:
            raise RuntimeError("视觉 canonical 消息持久化失败：最新用户消息已变化")

    # (pending_persistence and flush_persistence are now defined earlier)

    def raise_if_cancelled():
        if harness_runtime is not None:
            harness_runtime.heartbeat()
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("用户终止了对话。")

    response_stream = None
    try:
        if harness_runtime is not None:
            harness_runtime.mark_running()
        max_turns = 100
        for turn in range(max_turns):
            raise_if_cancelled()
            if harness_runtime is not None:
                current_step = harness_runtime.begin_step()
            if turn > 0:
                # 轮次边界事件：GUI 流式渲染据此把每个 LLM 轮次拆成独立消息块，
                # 与历史重放（每轮一条 DB 记录）的结构对齐；CLI/子代理只认已知事件，自动忽略
                _notify_event(event_callback, "round_started", {"round": turn + 1})

            # API 调用指数退避与 Jitter 抖动重试
            response_stream = None
            inflight_holder = None
            max_api_retries = 5
            base_delay = 1.0
            # 超窗自动压缩只允许一次：压缩后仍超限说明单条消息/附件本身过大，
            # 继续重试只会反复重发超大载荷空烧额度
            overflow_compress_attempts = 0
            rotate_deepseek_generation = False
            classified_message_shape = None
            request_cache_reason = "冷启动"
            request_diagnostic = None
            active_deepseek_generation_id = None
            active_deepseek_message_count = None

            for attempt in range(max_api_retries):
                raise_if_cancelled()
                try:
                    effective_request_tools = request_tools
                    # 先生成 provider 真正会收到的 canonical 消息，再写入 DeepSeek
                    # 账本。若先保存原始媒体/连续同角色消息，后续清洗会改变历史字节，
                    # 账本记录就不再等于实际请求前缀。
                    cleaned_current, folded_count, keep_media = _strip_media_and_merge(
                        messages, model_name, api_base
                    )
                    cleaned_messages = cleaned_current
                    if deepseek_ledger_enabled:
                        ledger_request = _prepare_deepseek_ledger_request(
                            deepseek_ledger_manager,
                            parent_session,
                            model_name,
                            api_base,
                            cache_scope,
                            cleaned_current,
                            request_tools,
                            force_new=rotate_deepseek_generation,
                        )
                        rotate_deepseek_generation = False
                        cleaned_messages = ledger_request["messages"]
                        effective_request_tools = ledger_request["tools"]
                        kwargs["tools"] = effective_request_tools
                        active_deepseek_generation_id = ledger_request["generation_id"]
                        active_deepseek_message_count = len(cleaned_messages)
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
                    current_message_shape = _cache_message_fingerprints(cleaned_messages)
                    if current_message_shape != classified_message_shape:
                        request_diagnostic = _classify_cache_request_details(
                            telemetry_session_id,
                            cache_scope,
                            model_name,
                            api_base,
                            _tool_contract_fingerprint(effective_request_tools),
                            cleaned_messages,
                            tools=effective_request_tools,
                        )
                        request_cache_reason = request_diagnostic["request_reason"]
                        classified_message_shape = current_message_shape
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
                    prepared_request = prepare_stream_request(
                        request_protocol,
                        kwargs,
                        attempt_timeout,
                    )
                    request_envelope = RequestEnvelope.capture(
                        prepared_request,
                        cache_scope=cache_scope,
                        request_reason=request_cache_reason,
                        attempt=attempt + 1,
                        deepseek_generation_id=active_deepseek_generation_id,
                        deepseek_message_count=active_deepseek_message_count,
                    )
                    if current_step is not None:
                        current_step.capture_request(request_envelope)
                    # 将同步阻塞的 completion 放到线程池中执行，使主循环可以响应取消事件。
                    # 在飞请求登记到 cancel_event 名下，停止路径可硬中断底层连接。
                    future = _submit_daemon_call(
                        _request_stream,
                        prepared_request,
                        cancel_event=cancel_event,
                    )
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
                        response_stream, inflight_holder = future.result()
                        if current_step is not None:
                            current_step.mark_streaming()
                    finally:
                        # 用户取消时避免 shutdown(wait=True) 阻塞到网络请求完成（最长 90s）
                        future.cancel()
                    break
                except Exception as api_err:
                    # 硬中断（用户取消关闭在飞流）会让请求线程抛连接错误；
                    # 先走取消判定，确保不误入下面的网络错误重试
                    raise_if_cancelled()
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
                            if deepseek_ledger_enabled:
                                rotate_deepseek_generation = True
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

            stream_assembler = StreamAssembler(
                keep_reasoning_fields=_supports_reasoning_fields(
                    model_name, api_base
                ),
                keep_thinking_blocks=(
                    str(model_name or "").split("/")[0].lower() == "anthropic"
                ),
                require_tool_call_reasoning=_requires_tool_call_reasoning(
                    model_name, api_base
                ),
            )
            for chunk in _stream_chunks_with_idle_notice(
                response_stream, event_callback, raise_if_cancelled,
                inflight_holder=inflight_holder, cancel_event=cancel_event,
            ):
                raise_if_cancelled()
                update = stream_assembler.consume(chunk)
                if update.public_text:
                    yield update.public_text

            # litellm 流式包装器在 finish 之后收到的最终 usage chunk 会被吞进
            # wrapper.chunks 而不再 yield（StopIteration 分支）。迭代中拿到的
            # 最后一个 usage 可能是不含缓存明细的中间态（prompt_tokens_details=None），
            # 导致 DeepSeek 等通道命中记账恒为 0。流结束后从 chunks 尾部补取最终 usage。
            _tail_chunks = getattr(response_stream, "chunks", None)
            if _tail_chunks:
                for _tail_chunk in reversed(_tail_chunks):
                    _tail_usage = getattr(_tail_chunk, "usage", None)
                    if _tail_usage is not None:
                        stream_assembler.observe_usage(_tail_usage)
                        break

            assembled_response = stream_assembler.finish()
            if stream_assembler.finish_public_text:
                yield stream_assembler.finish_public_text

            finish_reason = assembled_response.finish_reason
            stream_usage = assembled_response.usage

            # 输出被模型长度限制截断：给出明确提示，避免用户以为任务已完成。
            if finish_reason == "length":
                truncated_notice = (
                    "\n[回复已被模型长度限制截断。如需完整结果，请尝试："
                    "1) 让模型分步骤输出；2) 换用输出上限更高的模型；"
                    "3) 在 config.json 中为当前模型增大 max_tokens。]"
                )
                _notify_event(event_callback, "notice", {"message": truncated_notice.strip()})

            if cache_scope == "main" and keepalive_interval:
                cache_keepalive_service.register_request(
                    kwargs,
                    interval=keepalive_interval,
                    telemetry={
                        "model_name": model_name,
                        "api_base": api_base,
                        "session_id": telemetry_session_id,
                    },
                )

            # 流结束：按最终 usage chunk 严格校验并统一记账一次。
            _persist_cache_request_diagnostic(request_diagnostic, stream_usage)
            if stream_usage is not None:
                _account_server_usage(
                    stream_usage,
                    model_name=model_name,
                    api_base=api_base,
                    session_id=telemetry_session_id,
                    cache_scope=cache_scope,
                    invalidation_reason=request_cache_reason,
                )

            reasoning_content = stream_assembler.raw_reasoning_content
            public_content = assembled_response.content
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
                not assembled_response.tool_calls
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

            tool_calls_list = assembled_response.tool_calls
            current_tool_calls = tool_calls_list
            replay_reasoning_content = assembled_response.reasoning_content
            provider_thinking_blocks = assembled_response.thinking_blocks
                
            message_to_append = {
                "role": "assistant",
                "content": public_content or None,
                "reasoning_content": replay_reasoning_content,
            }
            if provider_thinking_blocks is not None:
                message_to_append["thinking_blocks"] = provider_thinking_blocks
            if tool_calls_list:
                message_to_append["tool_calls"] = tool_calls_list
            messages.append(message_to_append)
            if current_step is not None:
                current_step.mark_assistant_ready()

            persisted_message = {
                "role": "assistant",
                "content": public_content or None,
                "reasoning_content": replay_reasoning_content,
            }
            if provider_thinking_blocks is not None:
                persisted_message["thinking_blocks"] = provider_thinking_blocks
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
            # 重试机会用尽后的最终消息必须照常入库，否则内存与 DB 分叉。
            # 连续"只思考不产出"最多重试 FINAL_SUMMARY_MAX_RETRIES 次（共
            # FINAL_SUMMARY_MAX_RETRIES+1 次机会），避免长任务中模型一两轮
            # 欠佳就静默收尾，造成用户感知的"任务未执行完就结束"。
            will_retry_summary = (
                needs_final_summary
                and final_summary_retry_count < FINAL_SUMMARY_MAX_RETRIES
            )

            if stream and not will_retry_summary:
                pending_persistence.append(("assistant", persisted_message))

            if deepseek_ledger_enabled and not will_retry_summary:
                canonical_assistant, _, _ = _strip_media_and_merge(
                    [message_to_append], model_name, api_base
                )
                deepseek_ledger_manager.append_deepseek_prompt_messages(
                    active_deepseek_generation_id,
                    canonical_assistant,
                    expected_count=active_deepseek_message_count,
                )

            if not tool_calls_list:
                if will_retry_summary:
                    final_summary_retry_count += 1
                    # 上一轮因 max_tokens 截断（finish_reason == "length"）导致正文
                    # 未输出时，用相同上限重试几乎必然再次把预算全烧在思考上。
                    # 同样地，"只思考不产出"也说明思考已烧掉大量输出预算。两者都
                    # 临时翻倍输出上限（封顶 32768）并在催促文案中要求压缩思考，
                    # 提高重试成功率。
                    length_truncated = finish_reason == "length"
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
                    flush_persistence()
                    if current_step is not None and turn + 1 < max_turns:
                        current_step.complete()
                    # 内部兜底重试静默进行：底部状态栏已显示"正在干活..."，
                    # 这里不再发 status 通知暴露"模型未生成结论"的内部细节。
                    continue
                # 计划仍有可执行项时不允许提前收尾：向模型注入续推消息，
                # 要求继续通过 run_subagent 完成剩余计划项（最多续推 2 次，
                # 避免模型坚持收尾时与续推消息无限打转）。
                if allow_subagents and parent_session and plan_continue_nudges < 2:
                    try:
                        from core.plan import get_default_plan_manager as _get_plan_for_nudge
                        from core.plan import (
                            clear_plan_auto_continue_suppression as _clear_nudge_suppress,
                            plan_auto_continue_suppressed as _nudge_suppressed,
                        )
                        _nudge_plan = _get_plan_for_nudge()
                        _nudge_ready = [
                            item for item in _nudge_plan.ready_items()
                            if item.get("kind") != "input_preparation"
                        ] if _nudge_plan.session_id == str(parent_session) else []
                        _nudge_running = any(
                            item.get("status") == "in_progress"
                            for item in _nudge_plan.snapshot()
                        ) if _nudge_plan.session_id == str(parent_session) else False
                        # 用户明确要求继续（计划项重新进入执行）或计划已清空时，
                        # 解除“用户取消”的续推抑制；否则跳过本次 nudge，避免把
                        # 刚被用户取消的任务复活
                        if _nudge_running or not _nudge_plan.snapshot():
                            _clear_nudge_suppress(parent_session)
                        _nudge_skip = _nudge_suppressed(parent_session)
                    except Exception:
                        _nudge_ready, _nudge_running, _nudge_skip = [], False, False
                    if _nudge_ready and not _nudge_running and not _nudge_skip:
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
                        _notify_event(event_callback, "notice", {
                            "message": "计划仍有未执行项，已提醒模型继续执行。",
                        })
                        flush_persistence()
                        if current_step is not None and turn + 1 < max_turns:
                            current_step.complete()
                        continue
                # 模型未调用工具且未生成任何正文：给出明确提示，避免 UI 在
                # "思考完成" 后陷入沉默（常见于某些模型只输出 reasoning 或空回复）。
                # 判断用促升前的原始正文（raw_content_meaningful）：促升兜底本身
                # 就说明模型没产出正式回复。重试耗尽后必须让用户明确感知回合未
                # 正常完成：发 error 事件（GUI 会写进对话流/状态栏），而不是仅靠
                # 静默 notice。
                if not raw_content_meaningful:
                    fallback_msg = "模型本轮未返回有效正文内容（可能只输出了思考过程或空回复），任务未正常完成。"
                    if reasoning_content and reasoning_content.strip():
                        fallback_msg += " 如需查看思考过程，可尝试换用支持推理展示的模型或重新提问。"
                    if final_summary_retry_count > 0:
                        _notify_event(event_callback, "error", {"message": fallback_msg})
                    else:
                        _notify_event(event_callback, "notice", {"message": fallback_msg})
                # 促升的思考正文此前只写入了历史、从未推送到 UI——用户会看到
                # 回合默默结束什么都没输出。此处补推给界面流式显示。
                if promoted_reasoning and public_content:
                    yield from _yield_content(public_content)
                flush_persistence()
                if current_step is not None:
                    current_step.complete()
                if harness_runtime is not None:
                    harness_runtime.complete()
                return

            if current_step is not None:
                current_step.mark_tools_running()

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
                if current_step is not None:
                    current_step.mark_tools_committed()
                    if turn + 1 < max_turns:
                        current_step.complete()
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

            from core.harness.tool_pipeline import ToolBatchExecutor

            tool_pipeline, tool_context = _build_tool_pipeline_and_context(
                event_callback,
                parent_session=parent_session,
                parent_turn_id=parent_turn_id,
                cancel_event=cancel_event,
                allow_subagents=allow_subagents,
                permission_owner=permission_owner,
                permission_source=permission_source,
            )
            tool_calls_by_id = {
                tool_call["id"]: tool_call for tool_call in tool_calls_list
            }

            def execute_decoded(decoded, context):
                if _execute_single_tool_call is not _ORIGINAL_EXECUTE_SINGLE_TOOL_CALL:
                    return _execute_single_tool_call(
                        tool_calls_by_id[decoded.call_id],
                        event_callback=event_callback,
                        parent_session=parent_session,
                        parent_turn_id=parent_turn_id,
                        cancel_event=cancel_event,
                        allow_subagents=allow_subagents,
                        permission_owner=permission_owner,
                        permission_source=permission_source,
                    )
                return tool_pipeline.execute_decoded(decoded, context)

            batch_executor = ToolBatchExecutor(
                tool_pipeline,
                repeat_guard=repeat_call_guard,
                execute_decoded=execute_decoded,
                event_payload=_tool_event_payload,
                finished_event_payload=_tool_finished_event_payload,
                result_ok=_tool_result_ok,
            )
            records = []
            notify_tool_event = (
                lambda kind, payload: _notify_event(event_callback, kind, payload)
            )
            for prepared_batch in batch_executor.prepare(
                tool_calls_list,
                tool_context,
            ):
                batch_executor.begin_batch(
                    prepared_batch,
                    tool_context,
                    notify_tool_event,
                )
                yield ""
                records.extend(batch_executor.execute_batch(
                    prepared_batch,
                    tool_context,
                    notify_tool_event,
                ))
                yield ""
            records = tuple(sorted(
                records,
                key=lambda record: record.call_ordinal,
            ))

            results = [record.message for record in records]
            messages.extend(results)
            pending_tool_records = {
                record.message.get("tool_call_id"): record for record in records
            }
            if stream:
                pending_persistence.extend(("tool", result) for result in results)
            
            if event_callback is None:
                app_context.set_task_status('executing')
                app_context.invalidate_ui()
            else:
                _notify_event(event_callback, "status", {"state": "executing"})

            flush_persistence()
            if current_step is not None:
                current_step.mark_tools_committed()
                if turn + 1 < max_turns:
                    current_step.complete()
            
        # 超过轮次上限：提示消息写入历史并与正常结束一样落库，避免下一轮丢失上下文
        msg = f"{TOOL_ERROR_PREFIX} 任务轮次超过上限（100 轮），请把任务拆小一点再试！"
        limit_message = {"role": "assistant", "content": msg}
        messages.append(limit_message)
        if stream:
            pending_persistence.append(("assistant", deepcopy(limit_message)))
        flush_persistence()
        if current_step is not None:
            current_step.complete()
        if harness_runtime is not None:
            harness_runtime.complete()
        yield from _yield_content(msg)
        return

    except KeyboardInterrupt:
        if cancel_event is not None:
            cancel_event.set()
            abort_inflight_streams(cancel_event)
        # 关闭底层 HTTP 流：否则每次取消都会泄漏一个连接/套接字
        try:
            response_stream.close()
        except Exception:
            pass
        # 用户中断：先把本轮已产生的完整消息打捞落库（悬空的 tool_calls 组补
        # 取消占位结果），否则终止前已完成的助手输出/工具结果整批丢失，
        # 重开会话看不到任何终止前进度。
        try:
            salvage_persistence_on_cancel()
            flush_persistence()
        except Exception as salvage_error:
            if harness_runtime is not None:
                harness_runtime.fail(type(salvage_error).__name__)
            raise
        finally:
            # 安全回退删除此轮 Turn 添加的内存消息残影（DB 已保留打捞部分）
            rollback_turn_messages()
        if harness_runtime is not None:
            harness_runtime.cancel()
        raise
    except GeneratorExit:
        # GUI 消费端检测到取消后 break，生成器在 yield 点被 close：这条路径
        # 会绕过上面的 KeyboardInterrupt 分支，必须同样打捞落库再退出。
        try:
            response_stream.close()
        except Exception:
            pass
        if cancel_event is not None:
            cancel_event.set()
            abort_inflight_streams(cancel_event)
        try:
            salvage_persistence_on_cancel()
            flush_persistence()
        except Exception as salvage_error:
            if harness_runtime is not None:
                harness_runtime.fail(type(salvage_error).__name__)
            raise
        if harness_runtime is not None:
            harness_runtime.cancel()
        raise
    except Exception as e:
        try:
            response_stream.close()
        except Exception:
            pass
        # 异常路径同样回滚本轮未持久化的内存消息：既避免留下带 tool_calls 却无
        # tool 响应的悬空 assistant 消息（下一轮 API 会 400），也保持内存历史与 DB 一致
        rollback_turn_messages()
        if harness_runtime is not None:
            harness_runtime.fail(type(e).__name__)
        if e is persistence_failure:
            raise
        if event_callback is not None:
            _notify_event(event_callback, "error", {
                "message": _sanitize_error_text(e),
            })
            # 专用结束事件：chat_with_model_events 不再为异常回合发 turn_finished
            _notify_event(event_callback, "turn_error", {
                "message": _sanitize_error_text(e),
            })
        else:
            from prompt_toolkit.formatted_text import HTML

            safe_print(HTML("<ansiyellow>星瑶暂未完成当前步骤</ansiyellow>"))
        return
