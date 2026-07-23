import json
import logging
import os
import random
import re
import threading
import time
import uuid
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


_litellm_version = None


def _get_litellm_version():
    """缓存 litellm 版本号，避免重复查询。"""
    global _litellm_version
    if _litellm_version is None:
        try:
            from importlib.metadata import PackageNotFoundError, version

            _litellm_version = version("litellm")
        except Exception:
            try:
                _litellm_version = _get_litellm().__version__
            except Exception:
                _litellm_version = "unknown"
    return _litellm_version


def _litellm_completion(**kwargs):
    """litellm.completion 的懒加载包装，统一注入 ycode 客户端标识。"""
    kwargs.setdefault("extra_headers", {})
    kwargs["extra_headers"]["User-Agent"] = (
        f"ycode/{YCODE_VERSION} litellm/{_get_litellm_version()}"
    )
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

# 工具结果中的错误哨兵前缀（统一在此定义，供结果判断与构造处引用）
TOOL_ERROR_PREFIX = "🚨"
ASK_USER_ERROR_PREFIX = "无法提问："
TOOL_INTERNAL_ERROR_PREFIX = "Tool execution error:"
PERMISSION_DENIED_MESSAGE = f"{TOOL_ERROR_PREFIX} 用户拒绝了执行该操作。"
PERMISSION_CANCELLED_MESSAGE = f"{TOOL_ERROR_PREFIX} 操作已取消。"

# 🌟 注意这里：我们把四个工具全都引进来了！
from core.tools import read_file, write_file, list_directory, execute_command, search_news, search_code, edit_file, manage_task, run_subagent, glob_files, fetch_url, get_weather, sandbox_bypass, is_outside_workspace, set_session_sandbox_bypass
from core.mcp_client import mcp_manager
from core.permission_broker import CANCELLED_RESULT, permission_broker
from core.reasoning_stream import ReasoningStreamParser, split_reasoning_text
from core.tool_presenter import summarize_tool_finished, summarize_tool_started
from core.turn_events import TurnEventFactory
from core.version import __version__ as YCODE_VERSION

# 会话授权白名单（当前会话生命周期内有效，切换会话时自动清理）
session_allowed_commands = set()      # 放行的命令前缀 (如 'git', 'date')
session_allowed_write_files = set()   # 放行的写入文件绝对路径
session_allowed_edit_files = set()    # 放行的修改文件绝对路径
_last_tracked_session = None
_raw_permission_lock = threading.Lock()
# 保护会话放行白名单与 _last_tracked_session 的跨线程读写
_session_whitelist_lock = threading.Lock()
_plan_turn_lock = threading.Lock()
_plan_turn_started_at = {}
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
    "glob_files",
    "fetch_url",
    "search_news",
    "get_weather",
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
    import main
    with _session_whitelist_lock:
        if _last_tracked_session != main.current_session:
            _last_tracked_session = main.current_session
            session_allowed_commands.clear()
            session_allowed_write_files.clear()
            session_allowed_edit_files.clear()


# 内联执行参数：含这些参数的命令不做会话前缀放行（等价于放行任意代码）
# -m (python -m pip)、-p (node/perl -e 同类) 同样能执行任意模块/代码，必须拦截
_INLINE_EXEC_FLAGS = {"-c", "-e", "--eval", "-command", "-encodedcommand", "/c", "-m", "-p"}


def _session_prefix_for_command(cmd):
    """计算可会话放行的命令前缀；内联执行命令返回 None 表示不予放行。"""
    words = cmd.split()
    if not words:
        return None
    lowered = [word.lower() for word in words]
    if any(flag in lowered for flag in _INLINE_EXEC_FLAGS):
        return None
    first_word = words[0]
    # 特殊处理：python / pip 脚本保留首个参数（脚本路径）以防过度放行整个解释器；
    # 首个参数以 - 开头（如 python -m pip install）等价于任意模块执行，不予放行
    if len(words) > 1 and ("python" in first_word or "pip" in first_word):
        if words[1].startswith("-"):
            return None
        return f"{first_word} {words[1]}"
    return first_word


def _command_matches_prefix(cmd, prefix):
    """前缀匹配：前缀之后必须是空格或结尾，避免放行 'git' 后命中 'gitfoo'。"""
    return cmd == prefix or cmd.startswith(prefix + " ")

def safe_print(formatted_text):
    """线程安全地在 TUI 终端输出格式化文本，避免破坏屏幕布局"""
    import main
    from prompt_toolkit import print_formatted_text
    if main.app_instance and main.app_instance.loop:
        main.app_instance.loop.call_soon_threadsafe(lambda: print_formatted_text(formatted_text))
    else:
        print_formatted_text(formatted_text)

def ask_in_raw_terminal(function_name, arguments):
    """
    纯文本原始终端兜底：只在确实没有活动 TUI App 时才被调用（例如管道重定向或早期初始化时）。
    不使用任何 ANSI 转义序列或 msvcrt 菜单，直接用 print/input 避免污染 TUI 屏幕。
    """
    import main
    mode_display = {"confirm": "每次确认", "plan": "计划确认"}
    mode_tip = mode_display.get(main.current_permission_mode, "确认")
    
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
                    print(f"[当前会话已放行前缀]：{prefix}*")
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
    import main

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
    if main.current_permission_mode == "full":
        if event_callback is not None:
            _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
        return True

    # 3. 如果是计划模式且本轮对话的计划已经获得批准，直接通过
    if main.current_permission_mode == "plan" and main.plan_approved_this_turn:
        if event_callback is not None:
            _notify_event(event_callback, "permission_resolved", {"approved": True, "automatic": True})
        return True

    _notify_event(event_callback, "permission_requested", {
        "function_name": function_name,
        "arguments": dict(arguments),
        "source": permission_source,
    })

    app = main.app_instance
    if not app or not app.loop:
        with _raw_permission_lock:
            return ask_in_raw_terminal(function_name, arguments)

    # owner/turn_id 统一来源：优先显式传入，turn 兜底 owner，线程名最后兜底
    owner_id = str(permission_owner or parent_turn_id or threading.current_thread().name)
    turn_id = str(parent_turn_id or owner_id)
    request = permission_broker.enqueue(
        function_name,
        arguments,
        owner_id,
        turn_id,
        permission_source,
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
            except TimeoutError:
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
                        safe_print(ANSI(f"   ✨ [当前会话已放行前缀]：{prefix}*"))
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
            elif function_name == "sandbox_outside_access":
                set_session_sandbox_bypass(True)
                if event_callback is None:
                    safe_print(ANSI("   ✨ [当前会话已放行沙盒外访问]"))
            return True

        approved = (choice_idx in (0, 2))
        # plan 模式仅在用户明确选择“始终放行”时才批准整轮计划，
        # “同意本次”(0) 只放行当前这一次操作
        if choice_idx == 2 and main.current_permission_mode == "plan":
            main.plan_approved_this_turn = True
        return approved
    except Exception as e:
        _notify_event(event_callback, "error", {
            "message": f"权限询问交互发生异常: {str(e)}",
        })
        # 系统异常不应伪装成"用户拒绝"（False 会被模型理解为用户主动拒绝），
        # 返回取消语义让调用方按"操作中断"处理，并确保 broker 队列被清理
        permission_broker.cancel_owner(owner_id)
        return False
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


def get_tools_schema():
    """每次构建请求时新鲜获取工具 schema（registry 静态 + MCP 动态）。

    不做模块级一次性快照：启动时 MCP 连接失败不会导致本进程永久缺失
    MCP 工具 schema，且 mcp_client 为惰性连接，import 时不触发网络。
    """
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

def estimate_tokens(model_name, messages):
    """本地离线快速估算 Token 数，防止超载崩溃"""
    if not messages:
        return 0
    try:
        return _litellm_token_counter(model_name, messages)
    except Exception:
        # 如果模型不支持或报错，采用约 2 字符 / Token 的经验值进行兜底估算
        return len(str(messages)) // 2

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
_REASONING_FIELD_PROVIDERS = ("anthropic", "deepseek")


def _supports_reasoning_fields(model_name):
    prefix = str(model_name or "").split("/")[0].lower()
    return prefix in _REASONING_FIELD_PROVIDERS


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


def _supports_vision(model_name):
    """判断模型是否支持图像输入。

    判定顺序：
    1. 模型配置中的 supports_vision 显式标注（config.json 的 models 条目，可选）
    2. LiteLLM 内置模型信息（知名模型）
    3. 未知模型按协议兜底：openai/anthropic 兼容端点通常接受 image_url 负载
    """
    if not model_name:
        return False
    try:
        from core.model_manager import load_config
        for provider in load_config().get("providers", []):
            for model in provider.get("models", []):
                if model.get("litellm_model") == model_name and "supports_vision" in model:
                    return bool(model["supports_vision"])
    except Exception:
        pass
    try:
        if _litellm_supports_vision(model_name):
            return True
    except Exception:
        pass
    return str(model_name).startswith(("openai/", "anthropic/"))


def _flatten_media_content(content_list):
    """把多模态 list 内容折叠为纯文本：图片/文档用短占位符，绝不内联原始 base64 数据。"""
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
    1. 多媒体处理：支持视觉的模型保留最近一条消息中的图片（多模态负载原样透传），
       更早的历史图片一律折叠为占位符，避免每轮重复发送烧 Token；
       不支持视觉的模型全部折叠为占位符（绝不把 base64 当文本发给模型）
    2. 合并连续相同 role 的消息，满足 LiteLLM / Anthropic 对 user/assistant 严格轮流的要求
    3. reasoning_content / thinking_blocks 仅对支持的 provider 回传，避免其他 provider 报 400
    """
    keep_reasoning_fields = _supports_reasoning_fields(model_name)
    keep_media = _supports_vision(model_name)

    # 仅保留最近一条含多媒体的消息（其余历史中的图片折叠为占位符）
    last_media_idx = -1
    if keep_media:
        for idx, msg in enumerate(messages):
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") in ("image", "image_url", "document")
                for item in content
            ):
                last_media_idx = idx

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
        else:
            cleaned_msg = {"role": role, "content": str(content) if content is not None else ""}

        # 保留工具链和 provider 原生推理字段，避免工具轮次破坏签名思考块。
        if "tool_calls" in msg and msg["tool_calls"]:
            cleaned_msg["tool_calls"] = msg["tool_calls"]
        if "tool_call_id" in msg:
            cleaned_msg["tool_call_id"] = msg["tool_call_id"]
        if "name" in msg:
            cleaned_msg["name"] = msg["name"]
        if keep_reasoning_fields and msg.get("reasoning_content") is not None:
            cleaned_msg["reasoning_content"] = msg["reasoning_content"]
        if keep_reasoning_fields and msg.get("thinking_blocks") is not None:
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
    return fixed


def _provider_request_options(model_name, api_base):
    """Return options required by a known provider/model pair."""
    normalized_base = str(api_base or "").rstrip("/").lower()
    normalized_model = str(model_name or "").split("/")[-1].lower()
    if "api.kimi.com/coding" in normalized_base and normalized_model == "k3":
        return {
            "thinking": {"type": "enabled", "budget_tokens": 4096},
            "max_tokens": 32000,
            "allowed_openai_params": ["thinking"],
        }
    return {}


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
    json.loads 会报 "Unterminated string" 错误。本函数尝试：
    1. 补全未闭合的字符串（在末尾加引号）
    2. 移除对象/数组末尾多余的逗号
    3. 按栈顺序闭合所有未闭合的 [ { 括号
    4. 特殊处理：如果在某个值中间被截断且不在字符串内，尝试找到最近的键结束位置
    """
    if not raw or not raw.strip():
        return raw

    in_string = False
    escaped = False
    stack = []
    last_colon_pos = -1
    last_comma_pos = -1
    last_open_brace = -1
    
    for i, ch in enumerate(raw):
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
                if ch == "{":
                    last_open_brace = i
            elif ch == "]":
                if stack and stack[-1] == "[":
                    stack.pop()
            elif ch == "}":
                if stack and stack[-1] == "{":
                    stack.pop()
            elif ch == ":":
                last_colon_pos = i
            elif ch == ",":
                last_comma_pos = i

    # 如果不在字符串内且在某个值中间，尝试回退到上一个完整的键值对
    if not in_string and last_colon_pos > last_comma_pos and last_open_brace >= 0:
        # 在某个值中间被截断且不在字符串内：回退到上一个完整的键值对
        # 找到上一个逗号或左大括号的位置
        cutoff_pos = max(last_comma_pos, last_open_brace)
        if cutoff_pos > 0:
            repaired = raw[:cutoff_pos + 1].rstrip()
            # 重新计算栈状态
            in_string = False
            escaped = False
            stack = []
            for ch in repaired:
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
        else:
            repaired = raw.rstrip()
    else:
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
                "text": diff_output,
            })
    except Exception as diff_err:
        logger.warning("渲染文件 diff 失败 (%s): %s", file_path, diff_err)


def _sandbox_outside_guard(
    function_name,
    path_value,
    event_callback,
    permission_owner,
    parent_turn_id,
    permission_source,
    cancel_event,
):
    """沙盒开启时访问工作目录外路径：先询问用户，同意则放行本次。

    返回 (bypass_cm, error)：
    - (None, None)：在沙盒内或已全局/会话放行，无需特殊处理
    - (cm, None)：用户同意本次，调用方需在 cm 上下文中执行工具
    - (None, err)：用户拒绝/取消，err 为工具结果文案
    """
    if not path_value:
        return None, None
    try:
        outside = is_outside_workspace(path_value)
    except Exception:
        outside = False
    if not outside:
        return None, None
    from core import tools as _tools
    if _tools._outside_workspace_allowed():
        return None, None

    approved = check_permission_and_ask(
        "sandbox_outside_access",
        {"path": str(path_value), "operation": function_name},
        event_callback=event_callback,
        permission_owner=permission_owner,
        parent_turn_id=parent_turn_id,
        permission_source=permission_source,
        cancel_event=cancel_event,
    )
    if approved:
        return sandbox_bypass(), None
    if cancel_event is not None and cancel_event.is_set():
        return None, PERMISSION_CANCELLED_MESSAGE
    return None, "🚨 用户拒绝了访问沙盒外路径的请求（可提示用户使用 /sandbox on 长期开放）。"


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
            except Exception:
                arguments = {}

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
    if requires_permission:
        approved = check_permission_and_ask(
            function_name,
            arguments,
            event_callback=event_callback,
            permission_owner=permission_owner,
            parent_turn_id=parent_turn_id,
            permission_source=permission_source,
            cancel_event=cancel_event,
        )
        if not approved:
            # 取消与拒绝使用不同的工具结果文案，避免把“取消”误述为“拒绝”喂给模型
            if cancel_event is not None and cancel_event.is_set():
                result = PERMISSION_CANCELLED_MESSAGE
            else:
                result = PERMISSION_DENIED_MESSAGE

    _raise_if_tool_cancelled(cancel_event)

    try:
        if result in (PERMISSION_DENIED_MESSAGE, PERMISSION_CANCELLED_MESSAGE):
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
            # 写入权限已在上方统一确认（确认文案含文件路径），用户批准即视为允许该次沙盒外写入
            with sandbox_bypass():
                old_content = ""
                if os.path.exists(file_path):
                    try:
                        old_content = _read_text_with_encoding_fallback(file_path)
                    except OSError as read_err:
                        logger.warning("读取旧文件内容失败 (%s): %s", file_path, read_err)
                        old_content = ""
                # 新建文件也记录空内容快照，保证 undo 可以回退到“文件不存在之前”
                from core.memory import set_undo_state
                set_undo_state(file_path, old_content)
                write_file(file_path, new_content)
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
                    timeout_seconds=arguments.get("timeout_seconds", 600),
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
            # 只读目录类工具在沙盒外访问时需用户确认（同意本次/始终放行会话）
            bypass_cm, sandbox_err = None, None
            if function_name in ("list_directory", "search_code", "glob_files"):
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
            # 修改权限已在上方统一确认（确认文案含文件路径），用户批准即视为允许该次沙盒外修改
            with sandbox_bypass():
                if not os.path.isfile(file_path or ""):
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
                try:
                    old_content = _read_text_with_encoding_fallback(file_path)
                except OSError as read_err:
                    logger.warning("读取旧文件内容失败 (%s): %s", file_path, read_err)
                    old_content = ""
                # 修复后校验：target_content 必须在文件中能找到，否则说明截断修复不完整
                if target and target not in old_content:
                    result = (
                        f"{TOOL_ERROR_PREFIX} edit_file 的 target_content 经 JSON 修复后"
                        f"不完整（原始内容被截断），无法在文件中定位。"
                        f"请将本次编辑拆分为更小的片段后重试。"
                    )
                else:
                    # undo 快照由 core.tools.edit_file 内部统一记录
                    result = edit_file(
                        file_path,
                        target,
                        repl,
                        replace_all=bool(arguments.get("replace_all", False)),
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
            if any(s["function"]["name"] == function_name for s in mcp_manager.get_schemas()):
                result = mcp_manager.call_tool_sync(function_name, arguments)
            else:
                result = f"{TOOL_ERROR_PREFIX} 未找到名为 {function_name} 的工具！"
    except Exception as tool_err:
        result = f"{TOOL_ERROR_PREFIX} 调用工具 {function_name} 时发生系统内部错误：{str(tool_err)}"

    return {
        "role": "tool",
        "tool_call_id": tc["id"],
        "name": function_name,
        "content": str(result)
    }

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
    # 1. 自动注入 KAIROS 长期记忆指示与 MEMORY.md 索引
    import core.memory_kairos as mk
    memory_section = mk.build_memory_system_section()
    
    system_msg = None
    for msg in messages:
        if msg.get("role") == "system":
            system_msg = msg
            break
    if system_msg:
        if "# Auto Memory" not in system_msg["content"]:
            system_msg["content"] += "\n\n" + memory_section
    else:
        messages.insert(0, {"role": "system", "content": memory_section})

    # 2. 注入后台子 Agent 回报数据 (XML task-notification)
    from core.tools import subagent_manager
    drained_notifications = []
    if consume_background_notifications and parent_session:
        drained_notifications = subagent_manager.drain_notifications(parent_session)
        for notif in drained_notifications:
            messages.append({
                "role": "user",
                "content": notif
            })

    def rollback_turn_messages():
        """回滚本轮新增的消息残影；已 drain 的后台通知重新保留，避免丢失。"""
        messages[:] = turn_snapshot
        for notif in drained_notifications:
            messages.append({"role": "user", "content": notif})

    active_plan = allow_subagents and _has_pending_plan(parent_session)
    # 计划模式下仍向 LLM 暴露计划控制工具和只读探索工具，
    # 让主 Agent 既能推进计划，也能直接读取上下文验证子代理产出。
    _PLAN_AVAILABLE_TOOLS = _PLAN_ROOT_CONTROL_TOOLS | _PLAN_READ_ONLY_TOOLS
    request_tools = [
        schema
        for schema in get_tools_schema()
        if (
            not active_plan
            or schema.get("function", {}).get("name") in _PLAN_AVAILABLE_TOOLS
        )
        if allow_subagents
        or schema.get("function", {}).get("name")
        not in {"run_subagent", "plan_write", "plan_update"}
        if allowed_tools is None
        or schema.get("function", {}).get("name") in allowed_tools
    ]

    kwargs = {
        "model": model_name,
        "api_key": api_key,
        "messages": _strip_media_and_merge(messages, model_name),
        "tools": request_tools,
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
    }
    kwargs.update(_provider_request_options(model_name, api_base))
    
    if api_base:
        kwargs["api_base"] = api_base
        
    tool_activity_seen = False
    final_summary_retry_count = 0
    pending_persistence = []

    def flush_persistence():
        """把本轮已确认的助手/工具消息按序落库（与正常结束路径一致）。"""
        if stream and pending_persistence:
            import main as _main
            try:
                _main.manager.add_messages(parent_session, pending_persistence)
            except Exception as flush_err:
                # 落库失败不回滚内存：add_messages 可能已部分写入，回滚会造成
                # 内存/DB 分叉。仅上报错误事件并丢弃待写队列，避免重复写入。
                _notify_event(event_callback, "error", {
                    "message": f"消息持久化失败：{str(flush_err)}",
                })
            finally:
                del pending_persistence[:]

    def raise_if_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("用户终止了对话。")

    try:
        max_turns = 100
        for turn in range(max_turns):
            raise_if_cancelled()

            # API 调用指数退避与 Jitter 抖动重试
            response_stream = None
            max_api_retries = 3
            base_delay = 1.0

            for attempt in range(max_api_retries):
                raise_if_cancelled()
                try:
                    kwargs["messages"] = _apply_cache_control(
                        _strip_media_and_merge(messages, model_name),
                        model_name,
                    )
                    # 将同步阻塞的 completion 放到线程池中执行，使主循环可以响应取消事件。
                    # 网络请求一旦发出无法真正中断，但调用方无需傻等到超时才能退出。
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(completion, **kwargs, timeout=API_REQUEST_TIMEOUT_SECONDS)
                        while not future.done():
                            raise_if_cancelled()
                            try:
                                future.result(timeout=0.1)
                            except Exception:
                                # result(timeout=0.1) 在超时或异常时都会抛；
                                # 交给下面统一处理 future.exception()
                                pass
                        if future.cancelled():
                            raise KeyboardInterrupt("用户终止了对话。")
                        exc = future.exception()
                        if exc is not None:
                            raise exc
                        response_stream = future.result()
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
                        try:
                            import main as _main
                            if event_callback is None:
                                print("\n[⚠️ 检测到上下文窗口超限，正在尝试压缩历史记录以缩减 Prompt 长度...]")
                            else:
                                _notify_event(event_callback, "status", {
                                    "message": "检测到上下文窗口超限，正在压缩历史记录。",
                                })
                            from core.memory_compressor import compress_session_memory
                            # 复用全局单例 manager（共享 SQLite 连接 + 锁），
                            # 避免每次新建 MemoryManager 导致连接泄漏
                            manager = _main.manager
                            # 子代理回合必须压缩自己的会话（parent_session），
                            # 不能误压主 UI 会话
                            session = parent_session or _main.current_session
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
                            continue # 跳回重试当前 API
                        except Exception as compress_err:
                            if event_callback is None:
                                from prompt_toolkit.formatted_text import HTML

                                safe_print(HTML("<ansiyellow>历史记录暂未完成压缩</ansiyellow>"))
                            else:
                                _notify_event(event_callback, "error", {
                                    "message": str(compress_err),
                                })

                    if attempt < max_api_retries - 1:
                        sleep_time = (base_delay * (2 ** attempt)) + (random.random() * 0.5)
                        retry_message = (
                            f"API 暂未成功，正在进行第 {attempt+1}/{max_api_retries} 次重试，"
                            f"等待 {sleep_time:.1f} 秒"
                        )
                        if event_callback is None:
                            print(f"\n[⚠️ {retry_message}]")
                        else:
                            _notify_event(event_callback, "status", {"message": retry_message})
                        time.sleep(sleep_time)
                    else:
                        raise api_err

            accumulated_content = ""
            tool_calls_dict = {}
            in_native_think = False
            provider_thinking_blocks = None
            
            stream_usage = None
            for chunk in response_stream:
                raise_if_cancelled()

                # 同一流中可能出现多个带 usage 的 chunk，只保留最后一个，
                # 在流结束后统一记账一次，避免 cumulative_* 被反复累加
                if getattr(chunk, 'usage', None):
                    stream_usage = chunk.usage

                if not chunk.choices:
                    continue
                    
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
                                "id": getattr(tc, "id", f"call_{idx}"),
                                "type": "function",
                                "function": {"name": "", "arguments": ""}
                            }
                        # name 只在为空时赋值：provider 重复发 name 时 += 会拼出
                        # "read_fileread_file" 之类的坏名字
                        if getattr(tc.function, "name", None) and not tool_calls_dict[idx]["function"]["name"]:
                            tool_calls_dict[idx]["function"]["name"] = tc.function.name
                        if getattr(tc.function, "arguments", None):
                            tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments
            
            if in_native_think:
                yield "</think>"
                accumulated_content += "</think>"

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
                    session_token_usage["latest_cached"] = latest_cached
                    session_token_usage["cumulative_total"] += session_token_usage["latest_total"]
                    session_token_usage["cumulative_prompt"] += session_token_usage["latest_prompt"]
                    session_token_usage["cumulative_cached"] += latest_cached

            reasoning_content, public_content = split_reasoning_text(accumulated_content)
            from core.turn_renderer import sanitize_protocol_text
            public_content = sanitize_protocol_text(public_content)

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

            needs_final_summary = (
                not tool_calls_list
                and tool_activity_seen
                and not _is_meaningful_assistant_content(public_content)
            )
            
            if stream and not needs_final_summary:
                pending_persistence.append(("assistant", persisted_message))
            
            if not tool_calls_list:
                if needs_final_summary and final_summary_retry_count == 0:
                    final_summary_retry_count += 1
                    # 空 assistant 消息（content 为 None）不入库也不留内存：
                    # 留在历史里会让下一轮 API 报 400（Anthropic 拒绝空 content）
                    messages.pop()
                    messages.append({
                        "role": "user",
                        "content": (
                            "工具已经执行完毕。现在必须只输出最终中文检查结论，"
                            "说明发现的问题或下一步建议；不要调用工具，也不要只输出标点或 Markdown 标记。"
                        ),
                    })
                    _notify_event(event_callback, "status", {
                        "state": "executing",
                        "message": "模型未生成结论，正在请求最终总结。",
                    })
                    continue
                flush_persistence()
                return

            tool_activity_seen = True
            if event_callback is None:
                import main
                main.task_status = 'executing'
                if main.app_instance:
                    main.app_instance.loop.call_soon_threadsafe(main.app_instance.invalidate)
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
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=min(len(batch), 4)) as executor:
                        results = list(executor.map(execute_in_context, batch))
                    messages.extend(results)
                    for tc, res in zip(batch, results):
                        arguments = _tool_call_arguments(tc)
                        _notify_event(event_callback, "tool_finished", {
                            **_tool_event_payload(tc),
                            "ok": _tool_result_ok(tc["function"]["name"], res),
                            "summary": _tool_output_preview(
                                tc["function"]["name"], arguments, res
                            ),
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
                        })
                        if stream:
                            pending_persistence.append(("tool", res))
                yield ""
            
            if event_callback is None:
                main.task_status = 'executing'
                if main.app_instance:
                    main.app_instance.loop.call_soon_threadsafe(main.app_instance.invalidate)
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
        # 用户中断：安全回退删除此轮 Turn 添加的一切消息残影
        rollback_turn_messages()
        raise
    except Exception as e:
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
