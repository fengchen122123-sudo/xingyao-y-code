import os
import shutil
import sys
import io

# 工具档位：必须写在 core.* 导入之前，core.llm 导入时就按它建表（coding 只留 24 个
# 写代码工具，比全能档省约 77% 参数体积）。只在直接运行时设置，且不影响测试进程。
if __name__ == "__main__":
    os.environ.setdefault("YCODE_TOOL_PROFILE", "coding")
    try:
        from core.net_env import sanitize_dead_local_proxy

        sanitize_dead_local_proxy()
    except Exception:
        pass


def _agent_name() -> str:
    try:
        from core.persona_settings import get_agent_display_name

        return get_agent_display_name()
    except Exception:
        return "星瑶"


def _dispatch_browser_worker_argv() -> bool:
    # 先用字符串判断要不要进浏览器子进程，不急着 import：core.computer.browser_backend 一导入
    # 会整套加载 core.computer，实测多花约 0.45 秒，普通启动路径用不到。
    # 这里写的字符串必须和 WORKER_ARGV_FLAG 保持一致，导入后再核对一次兜底。
    if "--ycode-browser-worker" not in sys.argv:
        return False
    from core.computer.browser_backend import WORKER_ARGV_FLAG

    if WORKER_ARGV_FLAG not in sys.argv:
        return False
    sys.argv.remove(WORKER_ARGV_FLAG)
    from core.computer.browser_worker import main as worker_main

    raise SystemExit(worker_main())


_dispatch_browser_worker_argv()


def _dispatch_office_convert_argv() -> bool:
    if "--ycode-office-convert" not in sys.argv:
        return False
    from core.office_preview import (
        WORKER_ARGV_FLAG,
        _write_worker_stderr,
        run_convert_worker,
    )

    if WORKER_ARGV_FLAG not in sys.argv:
        return False
    index = sys.argv.index(WORKER_ARGV_FLAG)
    pair = sys.argv[index + 1:index + 3]
    if len(pair) < 2:
        _write_worker_stderr("Office 转换子进程参数不足（需要 SRC 与 DST）")
        raise SystemExit(2)
    raise SystemExit(run_convert_worker(pair[0], pair[1]))


_dispatch_office_convert_argv()

# Windows 默认 GBK 终端输出 emoji 会抛编码错误崩掉：强制 UTF-8 输出，
# 显示不了的字符替换掉；再把代码页切到 65001，否则 UTF-8 内容仍被当 GBK 解释。
if sys.platform == "win32":
    # ctypes 要等真需要改代码页时才导入：它第一次加载要多花约 0.25~0.36 秒，
    # 输出到管道或后台运行时不经终端显示，UTF-8 终端也不需要切代码页。
    # 因此只对老式 GBK 交互终端执行代码页切换。
    _needs_codepage_switch = False
    try:
        _needs_codepage_switch = bool(
            sys.stdout is not None and sys.stdout.isatty()
        ) and "utf" not in str(getattr(sys.stdout, "encoding", "") or "").lower()
    except Exception:
        _needs_codepage_switch = False
    if _needs_codepage_switch:
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
if sys.stdout and hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", None) != "utf-8":
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "buffer") and getattr(sys.stderr, "encoding", None) != "utf-8":
    try:
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
    except Exception:
        pass

# 打包成 exe 后主入口会被导入两次，不拦住的话全局变量（如 app_instance）在子线程里会读到空
if '__main__' in sys.modules and 'main' not in sys.modules:
    sys.modules['main'] = sys.modules['__main__']

# 后续导入耗时数秒，先输出一行提示，避免用户以为程序未响应；
# 无人值守和输出被重定向时保持安静，不污染标准输出。
def _is_headless_invocation():
    argv = sys.argv[1:]
    return any(
        arg in ("-p", "--prompt") or arg.startswith("--prompt=")
        for arg in argv
    )


if (
    sys.stdout is not None
    and sys.stdout.isatty()
    and not _is_headless_invocation()
):
    print(f"  {_agent_name()}正在启动，请稍候...", flush=True)

from core.runtime_warnings import configure_runtime_warnings
configure_runtime_warnings()
import datetime
import logging
import math
import re
import time
import threading
import queue
import uuid
import pyfiglet
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer, FloatContainer, Float, VSplit
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style, DynamicStyle
from prompt_toolkit.filters import Condition
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.data_structures import Point
from prompt_toolkit.history import FileHistory
from prompt_toolkit.keys import Keys

from core.memory import MemoryManager
from core.paste_placeholders import PastedTextStore, normalize_pasted_text
import core.agent_service as _agent_service
from core.llm import (
    chat_with_model_events,
    get_token_usage_snapshot,
    session_token_usage,
    token_usage_lock,
)
from core.permission_broker import PermissionRequest, permission_broker
from core.tool_progress import ActiveToolStatus
from core.turn_controller import TurnController
from core.turn_renderer import TurnRenderer
from core.themes import THEME_DESCS as _THEME_DESCS, get_theme as _get_theme
from core.ui_markers import (
    ANSI_BOLD,
    ANSI_GRAY,
    ANSI_RESET,
    diff_stat_bar,
    html_escape,
    pad_left,
    pad_right,
    plan_color,
    plan_html_color,
    plan_mark,
    progress_bar,
    term_display_width,
    theme_ansi,
    truncate_display,
)
from core.plan import bind_default_plan_session, get_default_plan_manager
from core.model_manager import get_active_model
from core.version import __version__ as YCODE_VERSION
# 注意：写成 from config import 某常量，导入那一刻值就固定了。以后加新代码要读这些配置，
# 请先 import config 再用 config.某常量 取值，这样配置改了能立刻生效。
# config.py 里的 MY_* 常量是靠模块级 __getattr__ 现算的，必须按属性访问才有效。
import config

manager = MemoryManager(db_path=config.DB_FILE)

# 缓存保活的判断依据要跨进程沿用：命令行版启动时，把上个进程存库的轮次重新读进内存，
# 不然每次重启都得重新白耗 10 轮全价请求才能判断一次。读失败也不影响启动。
try:
    from core.llm_diagnostics import seed_keepalive_lane_evidence_from_store

    seed_keepalive_lane_evidence_from_store(manager=manager)
except Exception:  # 读不到就退回老做法：重启后重新攒 10 轮
    pass

from core.turn_undo import service as _turn_undo_service

_turn_undo_service.configure_default_manager(manager)
try:
    _interrupted_ops = _turn_undo_service.startup_recovery_scan()
    if _interrupted_ops:
        _TURN_UNDO_SCAN_LOGGER = logging.getLogger("py.warnings")
        _TURN_UNDO_SCAN_LOGGER.warning(
            "按回合撤销：启动恢复收敛了 %d 个未终态操作", len(_interrupted_ops)
        )
except Exception:
    pass

current_theme = config.MY_THEME
# 权限模式统一交给 app_context 校验一遍，避免出现系统没定义过的模式
import core.app_context as _app_ctx
current_permission_mode = _app_ctx.normalize_permission_mode(config.MY_PERMISSION_MODE)
if current_permission_mode == "auto_decision":
    from core.permission_auto_decision import is_decision_model_enabled as _jev_enabled_at_boot

    if not _jev_enabled_at_boot():
        current_permission_mode = "confirm"
show_memory_summary = False
confirm_prompt = None

current_session = f"session_{uuid.uuid4().hex[:6]}"
_active = get_active_model()
current_model   = _active["litellm_model"]   if _active else ""
current_model_name = _active["name"]         if _active else "未激活模型"
current_key     = _active["api_key"]          if _active else ""
current_url     = _active["base_url"]          if _active else ""
current_ctx_win = _active["context_window"]   if _active else 128000
msg_queue = queue.Queue()
ui_event_queue = queue.Queue()
ui_renderers = {}
_assistant_stream_buffers = {}
_answer_header_printed = {}
_bg_summary_triggered = set()
_bg_summary_lock = threading.Lock()  # 加锁，保证“先查再加”这两步不会被打断
active_thinking_text = ""
active_thinking_turn_id = None
active_cancel_event = None
active_turn_id = None
app_instance = None
is_thinking = False
_llm_engine_ready = False
task_status = 'idle'
# 状态栏显示的出字速度：上一次模型请求实际跑出来的 token 速度（每秒多少字），
# 数据来自 core.llm 请求结束时上报的统计（用实际输出量除以真实耗时，
# 非流式的情况 core.llm 已经折算好）。None 表示还没测到。
_last_token_speed: float | None = None
think_start_time = 0
activity_start_time = 0
_active_tool_status: ActiveToolStatus | None = None
_thinking_panel_linger = False
_paste_store = PastedTextStore()
_turn_separator_needed = False
pending_action = None
# 记下每个计划项上次显示的状态，同一个项反复失败重试时不再重复刷屏
_last_printed_plan_state: dict[str, str] = {}
_turn_started_at: dict[str, float] = {}

class _CLIContext:

    ui_flavor = "headless"

    @property
    def app_instance(self):
        return app_instance

    @property
    def permission_mode(self) -> str:
        return current_permission_mode

    @permission_mode.setter
    def permission_mode(self, val: str):
        global current_permission_mode
        current_permission_mode = _app_ctx.normalize_permission_mode(val)

    @property
    def task_status(self) -> str:
        return task_status

    @task_status.setter
    def task_status(self, val: str):
        global task_status
        task_status = val

    @property
    def current_session(self) -> str:
        return current_session

    @current_session.setter
    def current_session(self, val: str):
        global current_session
        current_session = str(val or "")

    @property
    def manager(self):
        return manager

    @property
    def current_theme(self) -> str:
        return current_theme

    @property
    def permission_callback(self):
        return None

    def invalidate_ui(self):
        if app_instance and app_instance.loop:
            app_instance.loop.call_soon_threadsafe(app_instance.invalidate)

_app_ctx.register(_CLIContext())


def _sync_active_model_context():
    """把当前激活模型同步给 app_context，供 core 层（子代理 / 梦境整理）读取。

    必须在每一处更新 current_model / current_key / current_url 之后调用，
    否则 core/tools.py 会拿到过期的模型配置。
    """
    _app_ctx.set_active_model_tuple(current_model, current_key, current_url)


_sync_active_model_context()


THEME_DESCS = _THEME_DESCS

PERMISSION_HELP_LINES = (
    "  - <b>confirm</b>       : 变更前确认 (查询自动放行；任何变更都需要确认)",
    "  - <b>accept_edits</b>  : 自动编辑 (普通编辑和构建自动放行；删除、提交、推送等敏感操作需要确认)",
    "  - <b>auto_decision</b> : 自动决策 (权限与提问交由 JEV 决策；灾难性硬拒，越界/凭据仍确认)",
    "  - <b>plan</b>          : 计划模式 (允许读取、查询和制定计划；禁止修改或删除任务文件)",
    "  - <b>full</b>          : 完全访问 (沙盒内放行所有非灾难性操作；工作区外由沙盒开关控制)",
)

_FILE_LIST_CACHE_TTL = 5.0
_file_list_cache = {"timestamp": 0.0, "files": []}

MEMORY_COMPRESS_THRESHOLD = 0.70


def _esc(text) -> str:
    return html_escape(text)


def _term_display_width(text, *, strip_html: bool = False):
    return term_display_width(text, strip_html=strip_html)


def _pad_right(text, width, *, strip_html: bool = False):
    return pad_right(text, width, strip_html=strip_html)


def _pad_left(text, width):
    return pad_left(text, width)


def _truncate_visible(text, width, ellipsis="…"):
    return truncate_display(text, width, ellipsis=ellipsis)


def _parse_usage_date_args(args):
    from datetime import date as _date_cls
    tokens = [a for a in args if a not in ("~", "to")]
    if not tokens:
        return None
    if len(tokens) > 2:
        raise ValueError("参数过多")
    try:
        dates = [_date_cls.fromisoformat(t) for t in tokens]
    except ValueError:
        raise ValueError("日期格式须为 YYYY-MM-DD")
    if len(dates) == 1:
        return dates[0], dates[0]
    return min(dates), max(dates)


def _render_usage_section(manager, label, start_s, end_s):
    t = _get_theme(current_theme)
    title = f"{label} ({start_s})" if start_s == end_s else f"{label} ({start_s} ~ {end_s})"
    print_formatted_text(HTML(f"  <style fg='{t['accent']}'><b>▸ {_esc(title)}</b></style>"))
    rows = manager.query_token_usage_by_model(start_s, end_s)
    if not rows:
        print_formatted_text(HTML(f"    <style fg='{t['text_muted']}'>暂无记录</style>"))
        return False
    display_rows = [
        (row, _usage_model_label(row, row.get("api_base"), row.get("cache_scope")))
        for row in rows
    ]
    name_w = max(
        12,
        min(60, max(_term_display_width(name) for _, name in display_rows) + 2),
    )
    _header = (
        "    " + _pad_right("模型", name_w)
        + " " + _pad_left("输入", 14)
        + " " + _pad_left("输出", 14)
        + " " + _pad_left("缓存输入", 14)
        + " " + _pad_left("缓存命中", 14)
        + " " + _pad_left("命中率", 10)
        + " " + _pad_left("合计", 14)
    )
    print_formatted_text(HTML(f"<style fg='{t['text_muted']}'>{_header}</style>"))
    for r, route_label in display_rows:
        _cache_prompt = int(r.get("cache_prompt", 0) or 0)
        _rate = _format_cache_hit_rate(r["cached"], _cache_prompt)
        _name_cell = _esc(_pad_right(_truncate_visible(route_label, name_w), name_w))
        print_formatted_text(HTML(
            f"    {_name_cell} {r['prompt']:>14} "
            f"{r['completion']:>14} {_cache_prompt:>14} {r['cached']:>14} "
            f"{_rate:>10} {r['total']:>14}"
        ))
    if len(rows) > 1:
        sub = {
            k: sum(int(r.get(k, 0) or 0) for r in rows)
            for k in ("prompt", "completion", "cached", "cache_prompt", "total")
        }
        _sub_rate = "--"
        print_formatted_text(HTML(
            f"    <style fg='{t['accent_soft']}'>{_pad_right('小计', name_w)} {sub['prompt']:>14} "
            f"{sub['completion']:>14} {sub['cache_prompt']:>14} {sub['cached']:>14} "
            f"{_sub_rate:>10} {sub['total']:>14}</style>"
        ))
    from core.cache_keepalive import load_keepalive_interval
    idle_seconds = int(load_keepalive_interval() or 120)
    diagnostic = manager.query_cache_diagnostic_summary(
        start_s, end_s, idle_seconds=idle_seconds, cache_scope="main"
    )
    diagnostic_text = _format_cache_diagnostic_summary(diagnostic)
    if diagnostic_text:
        print_formatted_text(HTML(
            f"    <style fg='{t['text_muted']}'>{_esc(diagnostic_text)}</style>"
        ))
    return True


def _format_cache_diagnostic_summary(summary):
    count = int(summary.get("count", 0) or 0)
    unreported = int(summary.get("unreported", 0) or 0)
    if count == 0 and unreported == 0:
        return ""
    categories = summary.get("categories") or {}
    labels = (
        ("cold_start", "冷启动"),
        ("identity_change", "模型/接口变化"),
        ("tool_contract_change", "工具契约变化"),
        ("client_history_rewrite", "历史重写"),
        ("prompt_drop", "prompt 缩短"),
        ("idle_gap", "空闲间隔"),
        ("provider_or_unknown", "未确定"),
    )
    parts = [
        f"{label} {int(categories.get(key, 0) or 0)}"
        for key, label in labels
        if int(categories.get(key, 0) or 0) > 0
    ]
    text = f"低命中诊断 {count} 次"
    if parts:
        text += "（" + "、".join(parts) + "）"
    text += f"，多消耗输入 {int(summary.get('lost_tokens', 0) or 0)} tokens"
    if unreported:
        text += f"；未报告缓存明细 {unreported} 次"
    if int(categories.get("provider_or_unknown", 0) or 0):
        text += "；未确定事件缺少足够证据，需结合 endpoint A/B 或服务端日志确认"
    return text


def _format_cache_hit_rate(cached_tokens, cache_prompt_tokens):
    if cache_prompt_tokens <= 0:
        return "--"
    return f"{cached_tokens / cache_prompt_tokens * 100:.1f}%"


def _status_cache_metrics(session_id, model_name, api_base, cache_scope="main"):
    snapshot = get_token_usage_snapshot(session_id, model_name, api_base, cache_scope)
    cache = snapshot.get("cache") or {}
    scopes = snapshot.get("cache_scopes") or {}
    current_scope = str(cache_scope or "main")
    keepalive = cache if current_scope == "keepalive" else (scopes.get("keepalive") or {})
    hot = cache.get("hot") or {}
    session_all = cache.get("session_all") or {}
    keepalive_hot = keepalive.get("hot") or {}
    return {
        "hot_rate": hot.get("rate"),
        "hot_prompt": int(hot.get("prompt_tokens", 0) or 0),
        "hot_cached": int(hot.get("cached_tokens", 0) or 0),
        "session_rate": session_all.get("rate"),
        "keepalive_rate": keepalive_hot.get("rate"),
        "keepalive_prompt": int(keepalive_hot.get("prompt_tokens", 0) or 0),
        "keepalive_cached": int(keepalive_hot.get("cached_tokens", 0) or 0),
    }


def _compose_status_token_info(
    *,
    prompt,
    completion,
    cached,
    global_rate,
    ctx_pct,
    ctx_used,
    ctx_max,
):
    return (
        f"↑ {prompt} · ↓ {completion} · ⚡ {cached} ({global_rate}) · "
        f"上下文 {ctx_pct} ({ctx_used}/{ctx_max})"
    )


def _format_token_speed(speed) -> str:
    """状态栏吐字速度文本：只认实测的正值，未测到一律显示 "-- t/s"。

    速度全部由 core.llm 的 step_metrics 提供（completion_tokens ÷ 真实流式
    输出时长），这里只做格式化，绝不按字符数/耗时之类自行估算凑数。
    """
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return "-- t/s"
    if not math.isfinite(value) or value <= 0:
        return "-- t/s"
    return f"{value:.1f} t/s"


def _compose_status_task_segment(task_status, speed):
    speed_label = f" {_format_token_speed(speed)} "
    status_map = {
        "idle": (speed_label, "class:status.task_speed"),
        "thinking": (speed_label, "class:status.task_speed"),
        "executing": (speed_label, "class:status.task_speed"),
        "waiting": (" 等待授权确认... ", "class:status.task_active"),
    }
    return status_map.get(task_status, (speed_label, "class:status.task_speed"))


def _usage_model_label(row, api_base=None, cache_scope=None):
    provider = str(row.get("provider") or "")
    model = str(row.get("model") or "")
    label = f"{provider}/{model}" if provider else model
    base = str(api_base or "").strip()
    if base:
        label = f"{label} @ {base}"
    scope = str(cache_scope or "").strip()
    if scope and scope != "main":
        label = f"{label} [{scope}]"
    return label


def set_terminal_title(title: str) -> None:
    """设置终端窗口标题，兼容 Windows 控制台与 Windows Terminal 等现代终端。"""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass
    try:
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()
    except Exception:
        pass


def _list_workspace_files():
    now = time.time()
    if now - _file_list_cache["timestamp"] < _FILE_LIST_CACHE_TTL:
        return _file_list_cache["files"]
    ignore_dirs = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.idea', '.vscode'}
    results = []
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith('.')]
        for f in files:
            if f.startswith('.'):
                continue
            rel_path = os.path.relpath(os.path.join(root, f), '.')
            results.append(rel_path.replace('\\', '/'))
    # 下面两行是分两步改的，中间理论上会被别的线程看到半成品。目前只有界面主线程
    # （SlashCommandCompleter.get_completions）调这里，不会出问题。
    # 以后若要从后台线程调用，得改成整体替换一个新的 dict。
    _file_list_cache["timestamp"] = now
    _file_list_cache["files"] = results
    return results


def sync_plan_session(session_name=None):
    bind_default_plan_session(session_name or current_session)


def schedule_ui(callback):
    """Schedule a callback on the prompt_toolkit thread without direct worker I/O."""
    app = app_instance
    if app is None or app.loop is None:
        return
    loop = app.loop
    is_closed = getattr(loop, "is_closed", None)
    if callable(is_closed) and is_closed():
        return
    try:
        loop.call_soon_threadsafe(callback)
    except RuntimeError:
        # 这里仍可能踩到 loop.close()：刚查完 is_closed() 还没调度，事件循环就被关了
        pass


def _on_background_subagent_terminal(task_id: str, status: str, description: str, summary: str) -> None:
    def _print_notification():
        t = _get_theme(current_theme)
        icon_color = {
            "completed": t["ok"],
            "failed": t["err"],
            "timed_out": t["err"],
            "cancelled": t["text_muted"],
        }.get(status, t["warn"])
        icon = {
            "completed": "✓",
            "failed": "✗",
            "timed_out": "◷",
            "cancelled": "⊘",
        }.get(status, "•")
        status_label = {
            "completed": "已完成",
            "failed": "已失败",
            "timed_out": "已超时",
            "cancelled": "已取消",
        }.get(status, status)

        # 说明文字截到 40 个字，避免通知过长
        desc_short = (description or "")[:40]
        summary_short = (summary or "")[:150]
        desc_safe = _esc(desc_short)
        summary_safe = _esc(summary_short)
        tid_safe = _esc(task_id)

        try:
            lines = [
                "",
                (
                    f"  <style fg='{t['accent']}'><b>◈ 后台子代理{status_label}</b></style> "
                    f"<style fg='{t['text']}'>{desc_safe}</style> "
                    f"<style fg='{t['text_muted']}'>(</style>"
                    f"<style fg='{icon_color}'>{icon}</style>"
                    f"<style fg='{t['text_muted']}'> · id=</style>"
                    f"<style fg='{t['accent_soft']}'>{tid_safe}</style>"
                    f"<style fg='{t['text_muted']}'>)</style>"
                ),
            ]
            if summary_short:
                lines.append(
                    f"    <style fg='{t['text_muted']}'>├─</style> "
                    f"<style fg='{t['text_soft']}'>{summary_safe}</style>"
                )
            lines.append(
                f"    <style fg='{t['text_muted']}'>└─ 查看完整结果:</style> "
                f"<style fg='{t['accent_soft']}'>/tasks tail {tid_safe}</style>"
            )
            lines.append("")
            _print_ui_html("\n".join(lines))
        except Exception:
            # 通知没打印出来也不能影响主流程
            pass

    schedule_ui(_print_notification)

    _cb_session_snapshot = current_session

    # 等 0.5 秒再查：直接查会误判，另一个后台任务可能正在收尾、状态还没写回 SubagentManager。
    def _deferred_summary_check():
        try:
            from core.tools import subagent_manager as _sm_for_trigger
            session = _cb_session_snapshot
            if not session:
                return
            time.sleep(0.5)
            if _sm_for_trigger.has_running_background_tasks(session):
                return
            # 加锁保证“先查再加”：两个子代理 0.5 秒内先后结束时不加锁会各触发一次汇总，白花一轮额度。
            # _bg_summary_triggered 按会话分开记，集合元素为会话名。
            with _bg_summary_lock:
                if session in _bg_summary_triggered:
                    return
                _bg_summary_triggered.add(session)
            msg_queue.put({
                "text": "__ycode_internal__:summarize_background_tasks",
                "was_queued": False,
                "is_skill_call": False,
                "is_internal": True,
                "session": session,
            })
        except Exception:
            # 触发失败也不能影响主流程
            pass

    threading.Thread(target=_deferred_summary_check, daemon=True).start()


def _permission_prompt(request: PermissionRequest) -> dict:
    return {
        "request_id": request.request_id,
        "function_name": request.function_name,
        "arguments": dict(request.arguments),
        "source": request.source,
        "options": list(request.options),
        "current_idx": 0,
    }


def _permission_source_line(prompt: dict) -> str:
    source = str(prompt.get("source") or "主代理")
    return f"请求来源：{_esc(source)}\n"


def _undo_turn_impact_for_card(args: dict) -> dict:
    """undo_turn 权限卡富化：服务预检的影响摘要（只读）。"""
    try:
        from core.turn_undo import service as tu_service

        return tu_service.undo_turn_permission_impact(dict(args or {}))
    except Exception as exc:
        return {"available": False, "message": str(exc)}


def _confirm_card_meta(prompt: dict | None) -> tuple[str, str]:
    if not prompt:
        return ("class:frame.warn", "确认操作")
    func = str(prompt.get("function_name", "") or "")
    args = prompt.get("arguments") or {}
    if func == "ask_user":
        return ("class:frame", "✦ 需要你的选择")
    if func == "sandbox_outside_access":
        return ("class:frame.warn", "⚑ 越界访问确认")
    if func == "undo_turn":
        return ("class:frame.warn", "↩ 撤销回合确认")
    if func == "execute_command":
        try:
            from core.command_policy import classify_command_risk
            from core.permission_policy import PermissionRisk

            risk = classify_command_risk(str(args.get("command", "") or ""))
            if risk in (PermissionRisk.SENSITIVE_MUTATION, PermissionRisk.CATASTROPHIC):
                return ("class:frame.err", "⚠ 敏感命令确认")
        except Exception:
            pass
        return ("class:frame.warn", "⚡ 执行命令确认")
    if func in ("write_file", "edit_file"):
        return ("class:frame.warn", "✎ 文件变更确认")
    return ("class:frame.warn", "◈ 工具调用确认")


def _confirm_prompt_text(prompt: dict | None) -> str:
    if not prompt:
        return ""
    t = _get_theme(current_theme)
    func = prompt["function_name"]
    args = prompt["arguments"]
    source_line = _permission_source_line(prompt)
    text = (
        f"<style fg='{t['text_muted']}'>{source_line.rstrip()}</style>\n"
        if source_line
        else ""
    )
    frame_cls, _ = _confirm_card_meta(prompt)
    lead_color = t["err"] if frame_cls == "class:frame.err" else t["warn"]

    if func == "execute_command":
        cmd = args.get("command", "")
        cmd_lines = cmd.splitlines()
        if len(cmd_lines) > 2:
            cmd_display = "\n".join(cmd_lines[:2]) + f"\n    ... (共 {len(cmd_lines)} 行)"
        else:
            cmd_display = cmd
        if len(cmd_display) > 250:
            cmd_display = cmd_display[:247] + "..."
        text += (
            f"<style fg='{lead_color}'>❯</style>  "
            f"<style fg='{t['text']}'>{_esc(_agent_name())}申请执行命令: {_esc(cmd_display)}</style>\n"
        )
    elif func == "write_file":
        text += (
            f"<style fg='{t['warn']}'>✎</style>  "
            f"<style fg='{t['text']}'>{_esc(_agent_name())}申请写入文件: {_esc(args.get('file_path', ''))}</style>\n"
        )
    elif func == "edit_file":
        text += (
            f"<style fg='{t['warn']}'>✎</style>  "
            f"<style fg='{t['text']}'>{_esc(_agent_name())}申请修改文件: {_esc(args.get('file_path', ''))}</style>\n"
        )
    elif func == "ask_user":
        q = str(args.get("question", ""))
        q_lines = q.splitlines()
        if len(q_lines) > 3:
            q_display = "\n".join(q_lines[:3]) + f"\n    ... (问题包含多行内容，共 {len(q_lines)} 行)"
        else:
            q_display = q
        if len(q_display) > 200:
            q_display = q_display[:197] + "..."
        text += (
            f"<style fg='{t['accent']}'>?</style>  "
            f"<style fg='{t['text']}'>{_esc(_agent_name())}需要你的选择: {_esc(q_display)}</style>\n"
        )
    elif func == "sandbox_outside_access":
        text += (
            f"<style fg='{t['warn']}'>⚑</style>  "
            f"<style fg='{t['text']}'>{_esc(_agent_name())}申请访问工作目录之外的路径: {_esc(args.get('path', ''))}</style>\n"
        )
        text += (
            f"<style fg='{t['text_muted']}'>    （沙盒模式下默认仅限工作目录内；"
            "可选择在本任务内放行，或用 /sandbox on 长期开放）</style>\n"
        )
    elif func == "undo_turn":
        # 按回合撤销权限卡：预检影响摘要 + 未覆盖副作用 + 后续回合语义依赖警告
        text += (
            f"<style fg='{t['warn']}'>↩</style>  "
            f"<style fg='{t['text']}'>{_esc(_agent_name())}申请撤销一整轮的文件修改: "
            f"{_esc(str(args.get('turn_id') or '最近可撤销回合'))}</style>\n"
        )
        impact = _undo_turn_impact_for_card(args)
        if impact.get("available"):
            lines = [
                f"<style fg='{t['text']}'>    回合 {_esc(impact['short_id'])}：共 {impact['file_count']} 个文件 — "
                f"恢复原内容 {impact['restore_content_count']}，"
                f"删除本轮新建 {impact['delete_created_count']}，"
                f"恢复本轮删除 {impact['restore_deleted_count']}</style>",
            ]
            if impact.get("created_files"):
                lines.append(
                    f"<style fg='{t['text_muted']}'>    将删除的新建文件: {_esc('、'.join(impact['created_files']))}</style>"
                )
            lines.append(
                f"<style fg='{t['text_muted']}'>    聊天历史保留，星瑶将记住文件结果已失效。</style>"
            )
            if impact.get("has_uncovered_command_effects"):
                lines.append(
                    f"<style fg='{t['warn']}'>    ! 本轮执行过通用命令：仅恢复受控文件，命令的其他副作用不撤销。</style>"
                )
            if impact.get("subsequent_completed_turns", 0) > 0:
                lines.append(
                    f"<style fg='{t['warn']}'>    ! 目标回合之后还有 {impact['subsequent_completed_turns']} 个已完成回合，"
                    "可能存在语义依赖，撤销后请重新检查或测试。</style>"
                )
            if impact.get("conflicts"):
                lines.append(
                    f"<style fg='{t['err']}'>    ! 预检发现冲突文件，批准后将拒绝整轮撤销、零文件写入。</style>"
                )
            text += "\n".join(lines) + "\n"
        else:
            msg = impact.get("message") or "预检不可用"
            text += f"<style fg='{t['warn']}'>    ! 预检未通过：{_esc(msg)}（批准后执行会拒绝写入）</style>\n"
    else:
        summary = ", ".join(
            f"{key}={str(value)[:80]}" for key, value in list(args.items())[:3]
        )
        text += (
            f"<style fg='{t['accent']}'>◈</style>  "
            f"<style fg='{t['text']}'>{_esc(_agent_name())}申请调用工具: {_esc(func)}({_esc(summary)})</style>\n"
        )

    if func == "ask_user":
        text += (
            f"<style fg='{t['text_muted']}'>› 请选择确认操作（↑/↓ 选择后 Enter；或直接输入内容后 Enter 自定义回答）:</style>\n"
        )
    else:
        text += f"<style fg='{t['text_muted']}'>› 请选择确认操作:</style>\n"
    for idx, option in enumerate(prompt["options"]):
        prefix = "  > " if idx == prompt["current_idx"] else "    "
        if idx == prompt["current_idx"]:
            text += f"<style bg='{t['accent_bg']}' fg='{t['accent']}'><b>{prefix}{_esc(option)}</b></style>\n"
        else:
            text += f"<style fg='{t['text_muted']}'>{prefix}{_esc(option)}</style>\n"
    return text


def _activate_permission_request(request: PermissionRequest) -> None:
    def activate() -> None:
        global confirm_prompt, task_status, is_thinking
        if not permission_broker.is_active(request.request_id):
            return
        confirm_prompt = _permission_prompt(request)
        task_status = "waiting"
        is_thinking = False
        if app_instance is not None:
            app_instance.invalidate()

    schedule_ui(activate)


def _dismiss_permission_prompt() -> None:
    def dismiss() -> None:
        global confirm_prompt, task_status, is_thinking
        active_request = permission_broker.active_request()
        if active_request is not None:
            if (
                confirm_prompt is None
                or confirm_prompt.get("request_id") != active_request.request_id
            ):
                confirm_prompt = _permission_prompt(active_request)
            task_status = "waiting"
            is_thinking = False
            if app_instance is not None:
                app_instance.invalidate()
            return
        if confirm_prompt is not None and confirm_prompt.get("request_id"):
            confirm_prompt = None
        if active_cancel_event is not None and active_turn_id is not None:
            task_status = "executing"
            is_thinking = True
        if app_instance is not None:
            app_instance.invalidate()

    schedule_ui(dismiss)


def _clear_active_thinking() -> None:
    global active_thinking_text, active_thinking_turn_id
    active_thinking_text = ""
    active_thinking_turn_id = None


def _thinking_panel_text() -> str:
    raw = str(active_thinking_text or "").strip()
    if not raw:
        return "正在分析..."
    lines = raw.splitlines()
    if len(lines) > 10:
        return f"… (已省略前 {len(lines) - 10} 行)\n" + "\n".join(lines[-10:])
    return "\n".join(lines)


def _reset_turn_state():
    global is_thinking, task_status, _active_tool_status, _thinking_panel_linger
    _clear_active_thinking()
    is_thinking = False
    task_status = "idle"
    _active_tool_status = None
    _thinking_panel_linger = False
    if app_instance is not None:
        app_instance.invalidate()


# 状态栏不再展示 Git 段：本机实测 `git status` 约 1.0s、`git rev-parse` 约 0.9s
# （仓库索引大 + 实时防护），任何同步调用都会卡住首帧与键盘回显；放后台线程又
# 因为缺少权限上下文拿不到快照（返回空）。Git 信息统一走 `/git status` 或
# `/git` 管理器按需查看，绝不再占渲染路径。


def _ensure_cli_disclaimer_consent() -> None:
    from core.disclaimer_consent import (
        DISCLAIMER_VERSION,
        accept_disclaimer,
        needs_acceptance,
    )

    try:
        if not needs_acceptance():
            return
    except Exception:
        # 状态不可读时不阻断启动（与 GUI 侧同一降级口径）
        return

    width = 66
    print()
    print("=" * width)
    print(f"  免责条款确认（{DISCLAIMER_VERSION}）")
    print("=" * width)
    print("  本软件是本地 AI 编程辅助工具，自身不提供、不运营任何生成式")
    print("  人工智能模型服务；模型服务由你自行配置。")
    print()
    print("  · AI 生成内容可能不准确、不完整或已过时，需你自行审查验证；")
    print("  · 软件可读写文件、执行命令，请确认影响范围并自行备份数据；")
    print("  · 会话内容将按你的配置发送至所选模型服务提供者；")
    print("  · 不得利用本软件从事违反法律法规的活动。")
    print()
    print("  在法律允许的最大范围内，作者不对使用本软件造成的任何损失")
    print("  承担责任；本软件按「现状」提供，不作任何明示或默示担保。")
    print()
    print("  完整条款（共 8 章）：GUI「设置 → 法律 → 免责条款」")
    print("=" * width)
    print()
    try:
        answer = input("  输入 y 并回车表示已阅读并同意，其它输入将退出软件：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in ("y", "yes"):
        try:
            accept_disclaimer()
        except Exception:
            print("  提示：同意记录保存失败，请检查 ~/.ycode 目录是否可写。", file=sys.stderr)
        return
    print("  未接受免责条款，已退出。", file=sys.stderr)
    sys.exit(0)


def _require_interactive_console() -> None:
    stdout_tty = sys.stdout.isatty()
    stdin_tty = sys.stdin.isatty()
    if stdout_tty and stdin_tty:
        return
    missing = "与".join(
        name
        for name, tty in (("标准输出", stdout_tty), ("标准输入", stdin_tty))
        if not tty
    )
    print(
        f"无法启动交互式界面：检测到{missing}不是终端（TTY），"
        "当前可能通过管道或重定向运行。",
        file=sys.stderr,
    )
    print("如需在脚本/管道中执行一次性任务，请改用 headless 模式：", file=sys.stderr)
    print('  ycode -p "你的任务"', file=sys.stderr)
    print('  echo "问题" | ycode --prompt -', file=sys.stderr)
    raise SystemExit(2)


COMMAND_COMPLETIONS = [
    ("/help", "查看系统指令说明"),
    ("/session", "切换或创建新的记忆会话"),
    ("/new", "创建并切换到全新会话"),
    ("/models", "打开模型管理器"),
    ("/effort", "切换当前模型思考模式 (档位随模型, 无参查看可选档位)"),
    ("/clear-history", "清空当前会话全部历史消息 (y 确认)"),
    ("/persona", "人设设置 (name/title/text/reset)"),
    ("/shell", "选择默认命令解释器 (无参查看列表)"),
    ("/open", "在系统中打开文件/目录 (无参=当前工作区)"),
    ("/queue", "排队消息管理 (list/cancel 序号/clear)"),
    ("/paste-image", "发送剪贴板图片 (可附说明文字)"),
    ("/update", "更新检查与安装 (check/download/cancel/install)"),
    ("/changes", "查看最近 N 个回合的文件变更汇总 (默认 3)"),
    ("/usage", "查看 Token 用量统计 (按模型: 今日/近7天/近30天，可加日期/区间)"),
    ("/doctor", "聚合自检 (配置/凭据/数据/数据库/MCP/网络/GUI 运行时；--json 机器可读；export 导出诊断包)"),
    ("/tasks", "查看或托管后台长周期进程"),
    ("/plan", "查看或控制当前任务计划"),
    ("/goal", "目标模式：自主迭代直到达成（支持 status / stop / resume / confirm / --check / --rounds）"),
    ("/undo", "按回合撤销文件修改 (list/preview/status/recover)"),
    ("/resume", "断点续跑：从本会话最近中断的回合继续 (需 resume.enabled=true)"),
    ("/checkpoint", "会话级检查点 (list/restore 整轮回滚)"),
    ("/share", "导出会话为 Markdown/HTML/JSON"),
    ("/diagram", "打开/列出 Mermaid 流程图网页 (open 序号/list)"),
    ("/compress", "手动触发会话记忆压缩 (force 强制保留更少条)"),
    ("/remember", "手动追加一条规则/偏好/事实到记忆日志"),
    ("/dream", "后台触发梦境总结，提炼归纳记忆日志"),
    ("/memory", "查阅长期记忆索引 (MEMORY.md) 内容"),
    ("/worktree", "Git worktree 管理 (list/create/remove)"),
    ("/git", "Git 工作台 (status/diff/stage/commit/branch/switch/fetch/pull/push/stash/log)"),
    ("/mcp", "MCP 服务管理 (status/tools/enable/disable/reconnect/edit)"),
    ("/plugins", "插件管理 (查看清单/enable|disable 禁启用/reload 刷新)"),
    ("/hooks", "Hook 管理 (查看清单/信任确认/撤销)"),
    ("/hooks import", "从 ZCode 配置导入 hook (预览确认后写入,rollback 撤销)"),
    ("/skills", "技能管理 (list/refresh/enable/disable 或开选择器)"),
    ("/theme", "切换配色主题 (Nord, Cyberpunk, Matrix等)"),
    ("/permission", "切换安全权限级别 (confirm/accept_edits/auto_decision/plan/full)"),
    ("/turns", "设置单任务最大模型轮次 (10-1000)"),
    ("/sandbox", "沙盒开关 (on:工作目录外直接放行/off:仅限目录内)"),
    ("/whitelist", "管理项目白名单 (list/add/remove/clear)"),
    ("/clear", "清理当前屏幕"),
    ("/exit", "退出 星瑶Code"),
]

SESSION_SUBCOMMANDS = [
    ("all", "列出所有目录下的历史会话"),
    ("archive", "归档指定会话（从默认列表隐藏，数据保留可恢复）"),
    ("unarchive", "取消归档，恢复回默认会话列表"),
    ("archived", "列出全部已归档会话"),
    ("rename", "重命名当前活动会话"),
    ("delete", "物理删除指定会话"),
]


def _active_reasoning_snapshot():
    from core.model_manager import load_config, get_model_reasoning_choice
    from core.reasoning_capabilities import build_reasoning_snapshot

    cfg = load_config()
    active_id = cfg.get("active_model_id")
    if not active_id:
        return None, None, None
    model = None
    for provider in cfg.get("providers", []):
        for m in provider.get("models", []):
            if str(m.get("id")) == str(active_id):
                model = m
                break
        if model is not None:
            break
    return active_id, model, build_reasoning_snapshot(model, get_model_reasoning_choice(active_id))


_REASONING_COMPLETION_CACHE = {"key": None, "ts": 0.0, "rows": ()}


def _reasoning_options_for_completion(ttl: float = 5.0):
    try:
        active_id, _model, snapshot = _active_reasoning_snapshot()
        key = str(active_id)
        now = time.monotonic()
        cached = _REASONING_COMPLETION_CACHE
        if cached["key"] == key and now - cached["ts"] < ttl:
            return cached["rows"]
        rows = tuple(
            (o["value"], o["label"], o["value"] == snapshot["choice"])
            for o in snapshot.get("options", [])
        ) if (active_id and snapshot) else ()
        cached.update(key=key, ts=now, rows=rows)
        return rows
    except Exception:
        return ()


_COMMAND_SHELL_COMPLETION_CACHE = {"ts": 0.0, "rows": ()}


def _command_shell_options_for_completion(ttl: float = 5.0):
    try:
        now = time.monotonic()
        cached = _COMMAND_SHELL_COMPLETION_CACHE
        if now - cached["ts"] < ttl:
            return cached["rows"]
        from core.command_shell import list_command_shell_options
        rows = tuple(
            (o["id"], o["label"], bool(o["available"]))
            for o in list_command_shell_options()
        )
        cached.update(ts=now, rows=rows)
        return rows
    except Exception:
        return ()


def _cancel_active_turn() -> None:
    from core.tools import subagent_manager
    from core.task_scheduler import get_default_scheduler
    from core.plan import suppress_plan_auto_continue

    if active_cancel_event is not None:
        active_cancel_event.set()
        from core.llm import abort_inflight_streams
        abort_inflight_streams(active_cancel_event)
    if active_turn_id is not None:
        permission_broker.cancel_turn(active_turn_id)
        subagent_manager.cancel_foreground(current_session, active_turn_id)
    get_default_scheduler().cancel_all()
    # 用户主动取消：抑制计划续推 nudge，否则下一个回合收尾时
    # “计划尚未执行完毕”会把刚取消的任务复活
    suppress_plan_auto_continue(current_session)


def post_ui_event(event):
    """Queue a turn event and arrange for the main CLI loop to consume it."""
    ui_event_queue.put(event)
    schedule_ui(drain_ui_events)


def _forward_response_events(events, post_event=post_ui_event):
    reply_parts = []
    for event in events:
        if event.kind == "assistant_delta":
            reply_parts.append(str(event.payload.get("text", "")))
        post_event(event)
    return "".join(reply_parts)


def _resolve_confirm_prompt(index):
    """Resolve the active CLI question only when the selected option exists."""
    global confirm_prompt
    prompt = confirm_prompt
    if prompt is None or not 0 <= index < len(prompt.get("options", [])):
        return False

    request_id = prompt.get("request_id")
    if request_id:
        return permission_broker.resolve(str(request_id), index)

    future = prompt.get("future")
    if future is not None and not future.done():
        confirm_prompt = None
        future.set_result(index)
        return True
    return False


def _resolve_confirm_custom(text):
    prompt = confirm_prompt
    cleaned = str(text or "").strip()
    if prompt is None or prompt.get("function_name") != "ask_user" or not cleaned:
        return False
    request_id = prompt.get("request_id")
    if not request_id:
        return False
    return permission_broker.resolve(str(request_id), -1, custom_answer=cleaned)


def _submit_confirm_input(text, clear_input, invalidate):
    prompt = confirm_prompt
    if prompt is None:
        return False
    typed = str(text or "").strip()
    if prompt.get("function_name") == "ask_user" and typed:
        # 请求失效时仍消费 Enter：不清输入，也绝不回落到当前选项。
        if _resolve_confirm_custom(typed):
            clear_input()
            invalidate()
        return True
    index = prompt["current_idx"]
    if _resolve_confirm_prompt(index):
        invalidate()
    return True


def _format_plan_items(items):
    return "\n".join(
        f"  {plan_mark(item.get('status'))} {item.get('subject', '').strip()}"
        for item in items
        if item.get("subject", "").strip()
    )


def _format_live_plan_items(items):
    visible = [item for item in items if str(item.get("subject", "")).strip()]
    total = len(visible)
    completed = sum(item.get("status") == "completed" for item in visible)
    pct = int(completed * 100 / total) if total else 0
    bar = progress_bar(completed, total, width=10, theme_name=current_theme)
    muted = theme_ansi("text_soft", current_theme)
    dim = theme_ansi("text_muted", current_theme)
    text_bright = theme_ansi("text", current_theme)
    warn = theme_ansi("warn", current_theme)
    accent_soft = theme_ansi("accent_soft", current_theme)

    lines = [
        f"  {muted}进度 {completed}/{total}{ANSI_RESET}  {bar}  {dim}{pct}%{ANSI_RESET}"
    ]

    if total <= 8:
        shown_indices = list(range(total))
    else:
        # 优先围绕进行中项保留上下文，避免活跃项被截断在 8 项之外
        active_idxs = [i for i, it in enumerate(visible) if it.get("status") == "in_progress"]
        anchor = active_idxs[0] if active_idxs else max(0, completed - 1)
        start = max(0, anchor - 2)
        end = min(total, start + 8)
        if end - start < 8:
            start = max(0, end - 8)
        shown_indices = list(range(start, end))

    for idx in shown_indices:
        item = visible[idx]
        status = item.get("status")
        color = plan_color(status)
        marker = plan_mark(status)
        subject = str(item.get("subject", "")).replace("\x1b", "")
        stage = str(item.get("stage", "")).strip()
        stage_label = {"plan": "计划", "build": "构建", "review": "审查"}.get(stage, "")
        suffix = f" [{stage_label}]" if stage_label else ""
        if status == "in_progress":
            subj_color = text_bright
        elif status == "completed":
            subj_color = dim
        else:
            subj_color = muted

        meta_parts = []
        exec_mode = str(item.get("execution_mode", "") or "").strip()
        takeover_state = str(item.get("takeover_status", "") or "").strip()
        if exec_mode == "takeover":
            if status == "in_progress":
                meta_parts.append(f"{warn}⚡ 接管中{ANSI_RESET}")
            elif takeover_state == "queued":
                meta_parts.append(f"{warn}⏳ 接管排队{ANSI_RESET}")
        elif exec_mode == "subagent" and status == "in_progress":
            meta_parts.append(f"{accent_soft}→ 子代理{ANSI_RESET}")
        blocked_by = item.get("blocked_by")
        if blocked_by and status != "completed":
            refs = ", ".join(f"#{b}" for b in list(blocked_by)[:2])
            meta_parts.append(f"{dim}等待 {refs}{ANSI_RESET}")
        meta_suffix = f"  {' · '.join(meta_parts)}" if meta_parts else ""
        lines.append(
            f"  {color}{marker}{ANSI_RESET} {subj_color}{subject}{suffix}{ANSI_RESET}{meta_suffix}"
        )

    if total > len(shown_indices):
        lines.append(
            f"  {dim}… 共 {total} 项（已折叠 {total - len(shown_indices)} 项）{ANSI_RESET}"
        )
    return "\n".join(lines)


_TOOL_ACTIVITY_LABELS = {
    "read_file": "{agent}正在查看文件...",
    "write_file": "{agent}正在写入文件...",
    "edit_file": "{agent}正在修改文件...",
    "execute_command": "{agent}正在执行命令...",
    "list_directory": "{agent}正在查看目录...",
    "glob_files": "{agent}正在查找文件...",
    "search_code": "{agent}正在搜索代码...",
    "search_news": "{agent}正在搜索资料...",
    "fetch_url": "{agent}正在访问网址...",
    "get_weather": "{agent}正在查询天气...",
    "run_subagent": "{agent}正在调度子代理...",
    "manage_task": "{agent}正在管理后台任务...",
    "ask_user": "{agent}等待你的回答...",
    "plan_write": "{agent}正在更新任务计划...",
    "plan_update": "{agent}正在更新计划项...",
}


def _turn_activity_label(status, current_tool=""):
    agent = _agent_name()
    if status == "executing" and current_tool:
        label = _TOOL_ACTIVITY_LABELS.get(current_tool)
        # 兼容历史：万一外部塞入未带占位符的旧文案，format 缺占位符时原样返回
        return label.format(agent=agent) if label else f"{agent}正在干活..."
    labels = {
        "thinking": f"{agent}正在思考...",
        "executing": f"{agent}正在干活...",
        "waiting": f"{agent}等待你的确认...",
    }
    return labels.get(status, "")


def _format_activity_text(
    status: str,
    active_status: ActiveToolStatus | None,
    elapsed: float,
    frame: str,
) -> str:
    label = active_status.label if active_status is not None else _turn_activity_label(status)
    line = f"  {frame} {label} · {max(0.0, elapsed):.1f}s · Ctrl+C 中断"
    if active_status is not None and active_status.detail:
        line += f"\n    └─ {active_status.detail}"
    return line


def _print_ui_fragment(fragment):
    if fragment:
        def write_fragment():
            output_end = "" if fragment.endswith("\n") else "\n"
            print_formatted_text(ANSI(fragment), end=output_end, flush=True)

        app = app_instance
        if (
            app is not None
            and app.loop is not None
            and getattr(app, "_is_running", False)
        ):
            run_in_terminal(write_fragment)
        else:
            write_fragment()


def _print_ui_html(html_text: str) -> None:
    def _write():
        print_formatted_text(HTML(html_text), flush=True)

    app = app_instance
    if (
        app is not None
        and app.loop is not None
        and getattr(app, "_is_running", False)
    ):
        run_in_terminal(_write)
    else:
        print_formatted_text(HTML(html_text))


def _terminal_width():
    return max(20, shutil.get_terminal_size((100, 24)).columns)


def _print_turn_separator() -> None:
    border = theme_ansi("border", current_theme)
    line = "─" * min(max(16, _terminal_width() - 4), 60)
    _print_ui_fragment(f"\n  {border}{line}{ANSI_RESET}\n")


def _echo_user_input(html_text: str) -> None:
    global _turn_separator_needed
    if _turn_separator_needed:
        _print_turn_separator()
    _turn_separator_needed = True
    _print_ui_html(html_text)


def _echo_user_message(
    raw_text: str, *, queued: bool = False, prefix_tag: str = ""
) -> None:
    t = _get_theme(current_theme)
    safe = _esc(raw_text)
    tag = f"{_esc(prefix_tag)} " if prefix_tag else ""
    if queued:
        body = f"<style fg='{t['text_muted']}'>(排队等待中...) {tag}{safe}</style>"
    else:
        body = f"<style fg='{t['text']}'>{tag}{safe}</style>"
    _echo_user_input(f"  <style fg='{t['accent']}'><b>❯ 你</b></style> {body}")


def _render_public_answer_block(turn_id: str, text: str) -> None:
    block = str(text or "").strip()
    if not block:
        return
    if not _answer_header_printed.get(turn_id):
        _answer_header_printed[turn_id] = True
        t = _get_theme(current_theme)
        model_label = str(current_model_name or "").strip()
        model_suffix = (
            f" <style fg='{t['text_muted']}'>· {_esc(model_label)}</style>"
            if model_label and model_label != "未激活模型"
            else ""
        )
        _print_ui_html(
            f"\n  <style fg='{t['accent']}'><b>✦ {_esc(_agent_name())}</b></style>{model_suffix}"
        )
    try:
        from core.terminal_markdown import MarkdownRenderError, render_markdown_to_ansi
        rendered = render_markdown_to_ansi(
            block, _terminal_width(), theme_name=current_theme
        )
    except MarkdownRenderError:
        rendered = block
    _print_ui_fragment(rendered)
    _maybe_export_mermaid_block(block)


def _maybe_export_mermaid_block(block: str) -> None:
    try:
        from core.diagram_export import export_diagram_html, open_diagram

        export_result = export_diagram_html(block)
    except Exception as export_error:
        # 导出失败必须可见：静默吞掉会让"没有提示"变得无法诊断
        _print_ui_html(
            f"  <style fg='{_get_theme(current_theme)['text_muted']}'>! Mermaid 图表导出失败: {_esc(str(export_error))}</style>"
        )
        return
    if export_result is None:
        return
    diagram_path, diagram_count = export_result
    auto_open = False
    try:
        from core.model_manager import load_config

        auto_open = bool(load_config().get("mermaid_auto_open", False))
    except Exception:
        auto_open = False
    if auto_open:
        open_diagram(diagram_path)
        notice = (
            f"🌐 检测到 {diagram_count} 张 Mermaid 图，已在浏览器打开: "
            f"{diagram_path.name}（可设 config.mermaid_auto_open=false 关闭自动打开）"
        )
    else:
        notice = (
            f"ℹ 检测到 {diagram_count} 张 Mermaid 图，已生成网页: {diagram_path.name}"
            " —— 输入 /diagram open 在浏览器查看"
        )
    _print_ui_html(f"  <style fg='{_get_theme(current_theme)['text_muted']}'>{_esc(notice)}</style>")


def _find_paragraph_boundary(buffer: str) -> int:
    in_code_block = False
    i = 0
    while i < len(buffer) - 1:
        if buffer.startswith("```", i) and (i == 0 or buffer[i - 1] == "\n"):
            in_code_block = not in_code_block
            i += 3
            continue
        if not in_code_block and buffer.startswith("\n\n", i):
            return i
        i += 1
    return -1


def _flush_public_answer(turn_id: str, *, final: bool = False) -> None:
    buffer = _assistant_stream_buffers.get(turn_id, "")
    if not buffer:
        return
    if final:
        _assistant_stream_buffers.pop(turn_id, None)
        _render_public_answer_block(turn_id, buffer)
        return

    while True:
        boundary = _find_paragraph_boundary(buffer)
        if boundary == -1:
            break
        block = buffer[:boundary]
        buffer = buffer[boundary + 2 :]
        _render_public_answer_block(turn_id, block)

    if "```" not in buffer and not buffer.lstrip().startswith("|") and len(buffer) >= 160:
        boundary = max(
            buffer.rfind(mark, 0, 160)
            for mark in ("。", "！", "？", ". ", "! ", "? ")
        )
        if boundary >= 80:
            _render_public_answer_block(turn_id, buffer[: boundary + 1])
            buffer = buffer[boundary + 1:].lstrip(" ")
    _assistant_stream_buffers[turn_id] = buffer


def _render_ui_event(event):
    global is_thinking, task_status, think_start_time, activity_start_time
    global active_thinking_text, active_thinking_turn_id, _active_tool_status
    global _thinking_panel_linger
    global _last_printed_plan_state
    global _last_token_speed
    renderer = ui_renderers.get(event.turn_id)
    if renderer is None:
        renderer = TurnRenderer()
        ui_renderers[event.turn_id] = renderer
        _turn_started_at.setdefault(event.turn_id, time.time())

    if event.kind == "status":
        state = event.payload.get("state")
        if state in {"thinking", "executing", "waiting", "idle"}:
            task_status = state
            if state != "idle":
                is_thinking = state in {"thinking", "executing"}
            else:
                # idle 分支必须重置 is_thinking，否则旋转动画/排队判断会永久残留
                is_thinking = False
                _active_tool_status = None
        message = str(event.payload.get("message", ""))
        if message:
            t = _get_theme(current_theme)
            _print_ui_html(
                f"\n  <style fg='{t['text_muted']}'>{_esc(message)}</style>"
            )
        return

    if event.kind == "step_metrics":
        # 状态栏吐字速度：只接收 core.llm 用上游 usage（completion_tokens）÷
        # 真实流式输出时长算出的 token_speed。缺失/非正值一律不覆盖（保持
        # "-- t/s"），绝不在这里按字符数自行推算，速度必须是实测的。
        try:
            speed = float(event.payload.get("token_speed") or 0)
        except (TypeError, ValueError):
            speed = 0.0
        if math.isfinite(speed) and speed > 0:
            _last_token_speed = speed
            if app_instance is not None:
                app_instance.invalidate()
        return

    if event.kind == "permission_requested":
        task_status = "waiting"
        is_thinking = False
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind == "permission_resolved":
        if confirm_prompt is None:
            task_status = "executing"
            is_thinking = True
        else:
            task_status = "waiting"
            is_thinking = False
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind == "plan_item_handoff":
        item = dict(event.payload.get("item") or {})
        item_id = str(item.get("id", "")).strip()
        mode = str(
            event.payload.get("mode") or item.get("execution_mode") or ""
        ).strip()
        queue_position = event.payload.get("queue_position") or 0
        state_key = f"{item_id}:plan_item_handoff:{mode}"
        if _last_printed_plan_state.get(state_key) == item_id:
            if app_instance is not None:
                app_instance.invalidate()
            return
        _last_printed_plan_state[state_key] = item_id
        subject = str(item.get("subject", "")).replace("\x1b", "").strip()
        if mode == "takeover_queued":
            line = (
                f"{subject} 等待主 Agent 接管（队列第 {queue_position} 项）"
                if subject else f"等待主 Agent 接管（队列第 {queue_position} 项）"
            )
            tag = "⏳ 接管排队"
        else:
            line = (
                f"{subject} 子 Agent 已超时，主 Agent 正在接管并核查现场"
                if subject else "子 Agent 已超时，主 Agent 正在接管并核查现场"
            )
            tag = "⚡ 接管切换"
        warn = theme_ansi("warn", current_theme)
        muted = theme_ansi("text_muted", current_theme)
        _print_ui_fragment(f"\n{warn}{tag}{ANSI_RESET} {muted}{line}{ANSI_RESET}\n")
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind == "plan_writeback_rejected":
        # 回写被拒属于调度诊断：打印原因本身，不套用计划项的 <mark> 渲染，
        # 否则会把计划项显示成它原有的状态，反而看不出"为什么一直没动"
        message = str(event.payload.get("reason") or "").replace("\x1b", "").strip()
        if message:
            warn = theme_ansi("warn", current_theme)
            _print_ui_fragment(f"{warn}! 调度诊断：{ANSI_RESET}{message}\n")
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind == "plan_updated" or event.kind.startswith("plan_item_"):
        if event.kind.startswith("plan_item_"):
            item = dict(event.payload.get("item") or {})
            subject = str(item.get("subject", "")).replace("\x1b", "").strip()
            item_id = str(item.get("id", "")).strip()
            status = str(item.get("status", "")).strip()
            if subject and item_id:
                # 同一个计划项的同一状态只打印一次，避免失败重试时刷屏
                state_key = f"{item_id}:{event.kind}"
                if _last_printed_plan_state.get(state_key) == subject:
                    if app_instance is not None:
                        app_instance.invalidate()
                    return
                _last_printed_plan_state[state_key] = subject
                mark = plan_mark(status)
                color = plan_color(status)
                _print_ui_fragment(f"{color}{mark}{ANSI_RESET} {subject}\n")
                if event.kind == "plan_item_completed":
                    for key in list(_last_printed_plan_state.keys()):
                        if key.startswith(f"{item_id}:"):
                            del _last_printed_plan_state[key]
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind in {"tool_started", "tool_requested"}:
        task_status = "executing"
        is_thinking = True
        _thinking_panel_linger = False
        active_thinking_text = ""

    if event.kind == "notice":
        message = str(event.payload.get("message", ""))
        if message:
            t = _get_theme(current_theme)
            _print_ui_html(f"  <style fg='{t['warn']}'>! {_esc(message)}</style>")
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind == "thinking_started":
        active_thinking_text = ""
        active_thinking_turn_id = event.turn_id
        task_status = "thinking"
        is_thinking = True
        _active_tool_status = None
        _thinking_panel_linger = False
        think_start_time = time.time()
        activity_start_time = time.time()
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind == "thinking_delta":
        if active_thinking_turn_id != event.turn_id:
            active_thinking_text = ""
            active_thinking_turn_id = event.turn_id
            think_start_time = time.time()
            activity_start_time = time.time()
        text = str(event.payload.get("text", ""))
        active_thinking_text += (
            text.replace("\x1b", "").replace("\r\n", "\n").replace("\r", "\n")
        )
        task_status = "thinking"
        is_thinking = True
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind == "thinking_finished":
        active_thinking_turn_id = None
        task_status = "executing"
        is_thinking = True
        _active_tool_status = None
        _thinking_panel_linger = bool(active_thinking_text)
        if app_instance is not None:
            app_instance.invalidate()
        return

    # 工具聚合行是已格式化的 ANSI 终端输出，必须在 Markdown 正文之外直出。
    if event.kind == "assistant_delta":
        for fragment in renderer.flush_tool_progress():
            _print_ui_fragment(fragment)

    previous_active_status = _active_tool_status
    fragments = renderer.consume(event.kind, dict(event.payload))
    if event.kind in {"tool_started", "tool_requested", "tool_finished"}:
        _active_tool_status = renderer.active_tool_status()
        if previous_active_status is None and _active_tool_status is not None:
            activity_start_time = time.time()
        if app_instance is not None:
            app_instance.invalidate()
    if event.kind == "assistant_delta":
        _assistant_stream_buffers[event.turn_id] = (
            _assistant_stream_buffers.get(event.turn_id, "")
            + "".join(fragments)
        )
        _flush_public_answer(event.turn_id)
        if _thinking_panel_linger or active_thinking_text:
            _thinking_panel_linger = False
            active_thinking_text = ""
            if app_instance is not None:
                app_instance.invalidate()
    else:
        for fragment in fragments:
            _print_ui_fragment(fragment)

    if event.kind == "turn_finished":
        from core.plan import maybe_clear_completed_plan

        if maybe_clear_completed_plan():
            if app_instance is not None:
                app_instance.invalidate()
        for fragment in renderer.flush_tool_progress():
            _print_ui_fragment(fragment)
        tail = "".join(renderer.finish_incremental(include_assistant_text=True))
        if tail:
            _assistant_stream_buffers[event.turn_id] = (
                _assistant_stream_buffers.get(event.turn_id, "") + tail
            )
        _flush_public_answer(event.turn_id, final=True)
        had_answer = bool(_answer_header_printed.pop(event.turn_id, False))
        started_at = _turn_started_at.pop(event.turn_id, None)
        if had_answer and started_at is not None:
            t = _get_theme(current_theme)
            elapsed = max(0.0, time.time() - started_at)
            speed_part = (
                f" · {_last_token_speed:.1f} t/s"
                if _last_token_speed is not None and _last_token_speed > 0
                else ""
            )
            _print_ui_html(
                f"  <style fg='{t['text_muted']}'>── 完成 · 用时 {elapsed:.1f}s{speed_part}</style>\n"
            )
        else:
            app = app_instance
            if (
                app is not None
                and app.loop is not None
                and getattr(app, "_is_running", False)
            ):
                _print_ui_fragment("\n")
            else:
                print_formatted_text("\n", end="", flush=True)
        ui_renderers.pop(event.turn_id, None)
        _clear_active_thinking()
        _active_tool_status = None
        is_thinking = False
        task_status = "idle"
    elif event.kind == "turn_cancelled":
        for fragment in renderer.flush_tool_progress():
            _print_ui_fragment(fragment)
        tail = "".join(renderer.finish_incremental(include_assistant_text=True))
        if tail:
            _assistant_stream_buffers[event.turn_id] = (
                _assistant_stream_buffers.get(event.turn_id, "") + tail
            )
        _flush_public_answer(event.turn_id, final=True)
        t = _get_theme(current_theme)
        _print_ui_html(f"\n  <style fg='{t['warn']}'>⏹ 本轮任务已取消。</style>\n")
        ui_renderers.pop(event.turn_id, None)
        _answer_header_printed.pop(event.turn_id, None)
        _turn_started_at.pop(event.turn_id, None)
        _clear_active_thinking()
        _active_tool_status = None
        is_thinking = False
        task_status = "idle"
    elif event.kind == "turn_error":
        for fragment in renderer.flush_tool_progress():
            _print_ui_fragment(fragment)
        tail = "".join(renderer.finish_incremental(include_assistant_text=True))
        if tail:
            _assistant_stream_buffers[event.turn_id] = (
                _assistant_stream_buffers.get(event.turn_id, "") + tail
            )
        _flush_public_answer(event.turn_id, final=True)
        message = str(event.payload.get("message") or "未知错误")
        t = _get_theme(current_theme)
        _print_ui_html(
            f"\n  <style fg='{t['err']}'>⚠ 回合异常结束：{_esc(message)}</style>\n"
        )
        ui_renderers.pop(event.turn_id, None)
        _answer_header_printed.pop(event.turn_id, None)
        _turn_started_at.pop(event.turn_id, None)
        _clear_active_thinking()
        _active_tool_status = None
        is_thinking = False
        task_status = "idle"


def drain_ui_events():
    """Consume all queued turn events on the prompt_toolkit/UI thread."""
    while True:
        try:
            event = ui_event_queue.get_nowait()
        except queue.Empty:
            break
        try:
            _render_ui_event(event)
        except Exception as exc:
            logging.getLogger("py.warnings").warning("ui event render failed: %s", exc)
    if app_instance is not None:
        app_instance.invalidate()


def recalc_token_usage():
    """全局刷新当前会话的 Token 占用（优先真实用量，无记录才估算兜底）"""
    from core.agent_service import _read_llm_history, get_session_token_baseline
    history = _read_llm_history(manager, current_session)
    total = (
        0
        if not history
        else get_session_token_baseline(
            manager,
            current_session,
            current_model,
            history=history,
            api_base=current_url,
        )
    )
    with token_usage_lock:
        session_token_usage["latest_total"] = total
        session_token_usage["latest_prompt"] = 0


def _invalidate_ui():
    if app_instance is not None:
        app_instance.invalidate()

def trigger_session_auto_rename(session_name, user_query, api_model, api_key, api_url):
    """异步后台根据用户首个提问智能为会话重命名"""
    def _apply_rename(final_title):
        """在 UI 线程上应用重命名结果。

        current_session 的检查与赋值在同一个 UI 回调内原子完成，
        避免与 /session rename（同样在 UI 线程执行）之间产生竞态。
        """
        def update_ui():
            global current_session
            if current_session == session_name:
                current_session = final_title
            t = _get_theme(current_theme)
            _print_ui_html(
                f"\n  <style fg='{t['text_muted']}'>◈ 会话已自动命名为：{_esc(final_title)}</style>"
            )
            if app_instance:
                app_instance.invalidate()
        schedule_ui(update_ui)

    def _rename_job():
        try:
            query_text = ""
            if isinstance(user_query, str):
                query_text = user_query
            elif isinstance(user_query, list):
                for item in user_query:
                    if isinstance(item, dict) and item.get("type") == "text":
                        query_text += item.get("text", "")

            query_text = query_text[:200].strip()
            if not query_text:
                return

            prompt = (
                "你是一个会话起名助手。请根据以下用户的提问，提炼出一个非常简短、概括性强、通俗易懂的中文会话标题。\n"
                "要求：\n"
                "1. 必须在 2 到 6 个汉字以内，千万不要带标点符号，不要带任何解释说明文字，也不要带任何 Markdown 格式或引号。\n"
                "2. 必须直接返回会话标题本身。\n"
                "例如，用户问“帮我写个冒泡排序”，你应该返回“冒泡排序”；用户问“运行报错怎么办”，你应该返回“错误调试”。\n\n"
                f"用户提问：\"{query_text}\"\n"
                "会话标题："
            )
            from core.llm_native_transport import native_completion
            response = native_completion(
                model=api_model,
                api_key=api_key,
                api_base=api_url,
                messages=[{"role": "user", "content": prompt}],
                timeout=10
            )
            title = response.choices[0].message.content.strip()
            title = re.sub(r'["\'“”‘’，。！、]', '', title)
            title = title[:8].strip()

            if title and not re.match(r"^session_[a-f0-9]{6}$", title):
                success, final_title = manager.rename_session(session_name, title)
                if success:
                    _apply_rename(final_title)
        except Exception as exc:
            # 备用防线：如果大模型起名由于网络/超时失败，直接截取用户提问前 6 个有效字符作为会话名称
            logging.getLogger("py.warnings").warning("session auto-rename failed: %s", exc)
            try:
                clean_query = re.sub(r'[^\w\u4e00-\u9fa5]', '', query_text).strip()
                clean_query = "".join(clean_query.split())
                fallback_title = clean_query[:6]
                if not fallback_title:
                    fallback_title = "新对话"

                success, final_title = manager.rename_session(session_name, fallback_title)
                if success:
                    _apply_rename(final_title)
            except Exception as fallback_exc:
                logging.getLogger("py.warnings").warning(
                    "session auto-rename fallback failed: %s", fallback_exc
                )

    threading.Thread(target=_rename_job, daemon=True).start()


def _truncate_path_left(path_text: str, max_width: int) -> str:
    s = str(path_text or "")
    if max_width <= 0 or _term_display_width(s) <= max_width:
        return s
    if max_width <= 2:
        return _truncate_visible(s, max_width)
    acc = ""
    acc_w = 0
    budget = max_width - 1
    for ch in reversed(s):
        cw = _term_display_width(ch)
        if acc_w + cw > budget:
            break
        acc = ch + acc
        acc_w += cw
    return "…" + acc


def _collect_home_capabilities() -> str:
    parts: list[str] = []
    try:
        from core.skills import get_active_skill_ids

        parts.append(f"技能 {len(get_active_skill_ids())}")
    except Exception:
        pass
    mcp_mod = sys.modules.get("core.mcp_client")
    if mcp_mod is not None:
        try:
            srv = getattr(mcp_mod.mcp_manager, "servers", {}) or {}
            parts.append(f"MCP {len(srv)}")
        except Exception:
            pass
    pl_mod = sys.modules.get("core.plugin_loader")
    if pl_mod is not None:
        try:
            pinfos = pl_mod.get_plugin_infos() or []
            enabled_p = sum(1 for p in pinfos if getattr(p, "enabled", False))
            parts.append(f"插件 {enabled_p}")
        except Exception:
            pass
    parts.append(f"主题 {current_theme}")
    return " · ".join(parts)


def print_logo(clear_screen=True):
    """打印方案 A 首页概览卡片（保留经典 4 行像素头像，单主色克制随主题变化）。"""
    if clear_screen:
        if app_instance is not None and getattr(app_instance, "renderer", None):
            # 应用运行期间必须用 prompt_toolkit 自己的清屏：os.system 会绕过
            # 渲染器的屏幕缓存，导致输入框/状态栏错位或残影
            try:
                app_instance.renderer.clear()
            except Exception:
                os.system("cls" if os.name == "nt" else "clear")
        else:
            os.system("cls" if os.name == "nt" else "clear")

    t = _get_theme(current_theme)
    term_w = _terminal_width()
    card_w = min(max(term_w - 4, 54), 100)
    inner_w = card_w - 2

    cwd = os.getcwd()
    active_info = get_active_model()
    model_display = active_info["name"] if active_info else "未激活模型"
    effort_val = ""
    if active_info:
        try:
            effort_val = str(active_info.get("reasoning_effort") or "").strip()
        except Exception:
            effort_val = ""
    model_summary = f"{model_display} · 思考:{effort_val}" if effort_val else model_display

    perm_map = {
        "confirm": "确认",
        "accept_edits": "自动编辑",
        "auto_decision": "自动决策",
        "plan": "计划只读",
        "full": "完全访问",
    }
    perm_label = perm_map.get(current_permission_mode, current_permission_mode)
    try:
        from core.model_manager import load_config

        sandbox_open = bool(load_config().get("allow_outside_workspace", False))
        sandbox_label = "目录外放行" if sandbox_open else "沙盒限制"
    except Exception:
        sandbox_label = "沙盒限制"
    perm_summary = f"{perm_label} · {sandbox_label}"
    caps_summary = _collect_home_capabilities()
    agent_title = _agent_name()

    def _fit_html(content: str, width: int) -> str:
        w = _term_display_width(content, strip_html=True)
        if w < width:
            return content + (" " * (width - w))
        return content

    def _box_row(content: str) -> str:
        fitted = _fit_html(content, inner_w)
        return (
            f"  <style fg='{t['border']}'>│</style>"
            f"{fitted}"
            f"<style fg='{t['border']}'>│</style>\n"
        )

    def _kv_line(label: str, val: str, col_w: int, *, is_path: bool = False) -> str:
        prefix = f"  {label}  "
        val_budget = max(8, col_w - _term_display_width(prefix) - 1)
        trimmed = (
            _truncate_path_left(val, val_budget)
            if is_path
            else _truncate_visible(str(val or ""), val_budget)
        )
        return (
            f"<style fg='{t['text_muted']}'>{_esc(prefix)}</style>"
            f"<style fg='{t['text']}'>{_esc(trimmed)}</style>"
        )

    top_title_plain = f" ✦ XINGYAO CODE  v{YCODE_VERSION} "
    top_dashes = max(2, card_w - 3 - _term_display_width(top_title_plain))
    top_line = (
        f"  <style fg='{t['border']}'>╭─</style>"
        f"<style fg='{t['accent']}'><b> ✦ XINGYAO CODE</b></style>"
        f"<style fg='{t['text_muted']}'>  v{_esc(YCODE_VERSION)} </style>"
        f"<style fg='{t['border']}'>{'─' * top_dashes}╮</style>\n"
    )
    bottom_line = f"  <style fg='{t['border']}'>╰{'─' * inner_w}╯</style>\n"

    short_agent_sub = _truncate_visible(f"{agent_title} (Hacker Girl) · 终端编程伙伴", max(18, card_w - 50 if card_w >= 84 else inner_w - 14))
    avatar_rows = (
        f"  <style fg='{t['accent']}'>▄▄    ▄▄</style>   <b><style fg='{t['text']}'>欢迎回来，{_esc(_truncate_visible(agent_title, 12))}为您服务</style></b>",
        f" <style fg='{t['accent']}'>██████████</style>  <style fg='{t['text_soft']}'>{_esc(short_agent_sub)}</style>",
        f" <style fg='{t['accent']}'>██ █  █ ██</style>  <style fg='{t['text_muted']}'>输入 </style><style fg='{t['accent_soft']}'>/help</style><style fg='{t['text_muted']}'> 查看全部指令</style>",
        f"  <style fg='{t['accent']}'>▀██████▀</style>   ",
    )

    if card_w >= 84:
        right_w = 34
        left_w = inner_w - right_w - 1
        left_lines = [
            "",
            *avatar_rows,
            "",
            _kv_line("工作区", cwd, left_w, is_path=True),
            _kv_line("会话  ", current_session, left_w),
            _kv_line("模型  ", model_summary, left_w),
            _kv_line("权限  ", perm_summary, left_w),
            _kv_line("能力  ", caps_summary, left_w),
            "",
        ]
        right_lines = [
            "",
            f"  <style fg='{t['accent']}'><b>常用指令</b></style>",
            f"  <style fg='{t['accent_soft']}'>/models</style>   <style fg='{t['text_soft']}'>切换模型与思考档位</style>",
            f"  <style fg='{t['accent_soft']}'>/session</style>  <style fg='{t['text_soft']}'>会话管理与归档切换</style>",
            f"  <style fg='{t['accent_soft']}'>/skills</style>   <style fg='{t['text_soft']}'>管理与调用技能插件</style>",
            f"  <style fg='{t['accent_soft']}'>/plan</style>     <style fg='{t['text_soft']}'>查看或控制任务计划</style>",
            f"  <style fg='{t['accent_soft']}'>/goal</style>     <style fg='{t['text_soft']}'>目标驱动自主迭代</style>",
            f"  <style fg='{t['accent_soft']}'>/changes</style>  <style fg='{t['text_soft']}'>文件改动与 /undo 回滚</style>",
            f"  <style fg='{t['accent_soft']}'>/theme</style>    <style fg='{t['text_soft']}'>切换克制单主色主题</style>",
            "",
            f"  <style fg='{t['accent_soft']}'>@文件</style><style fg='{t['text_muted']}'> 挂载上下文 · </style><style fg='{t['accent_soft']}'>@会话</style><style fg='{t['text_muted']}'> 引用</style>",
            "",
        ]
        body_rows = ""
        for idx in range(max(len(left_lines), len(right_lines))):
            l_cell = _fit_html(left_lines[idx] if idx < len(left_lines) else "", left_w)
            r_cell = _fit_html(right_lines[idx] if idx < len(right_lines) else "", right_w)
            if _term_display_width(l_cell, strip_html=True) > left_w:
                l_cell = _esc(_truncate_visible(l_cell, left_w))
                l_cell = _fit_html(l_cell, left_w)
            if _term_display_width(r_cell, strip_html=True) > right_w:
                r_cell = _esc(_truncate_visible(r_cell, right_w))
                r_cell = _fit_html(r_cell, right_w)
            body_rows += (
                f"  <style fg='{t['border']}'>│</style>"
                f"{l_cell}"
                f"<style fg='{t['border_dim']}'>│</style>"
                f"{r_cell}"
                f"<style fg='{t['border']}'>│</style>\n"
            )
    else:
        single_lines = [
            "",
            *avatar_rows,
            "",
            _kv_line("工作区", cwd, inner_w, is_path=True),
            _kv_line("会话  ", current_session, inner_w),
            _kv_line("模型  ", model_summary, inner_w),
            _kv_line("权限  ", perm_summary, inner_w),
            _kv_line("能力  ", caps_summary, inner_w),
            "",
            f"  <style fg='{t['accent_soft']}'>@文件</style><style fg='{t['text_muted']}'> 挂载上下文 · </style><style fg='{t['accent_soft']}'>/models</style><style fg='{t['text_muted']}'> 切换模型</style>",
            "",
        ]
        body_rows = "".join(_box_row(line) for line in single_lines)

    card = top_line + body_rows + bottom_line
    print_formatted_text(HTML(card))
    print_formatted_text(HTML(
        f"<style fg='{t['text_muted']}'>  Enter 发送 · Ctrl+N 换行 · Ctrl+C 清空/中断 · Shift+Tab 权限 · Ctrl+T 记忆 · Ctrl+D 退出</style>\n"
    ))


def _resolve_session_after_selector(current_session_name, existed_before):
    sessions = manager.get_all_sessions()
    session_names = [s["name"] for s in sessions]
    if existed_before and current_session_name not in session_names:
        if session_names:
            return session_names[0], True
        return f"session_{uuid.uuid4().hex[:6]}", True
    return current_session_name, False


def show_session_history(session_name):
    """在终端里漂亮地重现该会话的所有历史对话消息"""
    global _turn_separator_needed
    _turn_separator_needed = False
    history = manager.get_history(session_name)
    safe_session = session_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    from core.history_renderer import render_session_history
    t = _get_theme(current_theme)
    rendered = render_session_history(
        history, _terminal_width(), theme_name=current_theme
    )
    if not rendered:
        print_formatted_text(HTML(
            f"  <style fg='{t['warn']}'>ℹ 会话 {safe_session} 当前没有任何对话记录。</style>"
        ))
        return

    _print_ui_html(
        f"\n  <style fg='{t['accent']}'><b>◈ 会话 {safe_session} 历史对话记录</b></style>"
    )
    _print_ui_fragment(rendered)
    _print_ui_html(
        f"  <style fg='{t['border']}'>{'─' * 38}</style>\n"
    )


def _undo_service():
    from core.turn_undo import service as tu_service

    return tu_service


def _undo_session_id(session_name: str) -> int:
    tu = _undo_service()
    manager.ensure_session(session_name)
    return tu.resolve_session_id(session_name)


def _undo_workdir_root() -> str:
    from core.turn_undo.coordinator import canonicalize_workspace_root
    from core.workspace_context import get_workdir

    return canonicalize_workspace_root(get_workdir() or os.getcwd())


def _undo_status_label(s: dict) -> str:
    status = s.get("status")
    availability = s.get("snapshot_availability")
    labels = {
        "recording": "记录中",
        "ready": "可撤销",
        "no_changes": "无文件修改",
        "unavailable": "不可撤销",
        "conflicted": "撤销受阻（文件已变化）",
        "reverting": "正在撤销",
        "reverted": "已撤销",
        "interrupted": "撤销中断",
        "recovering": "正在恢复撤销事务",
        "failed": "撤销失败",
    }
    label = labels.get(status, status)
    if status == "reverted" and availability == "expired":
        label = "已撤销（原 diff 已过期）"
    elif status == "ready" and availability == "expired":
        label = "撤销记录已过期"
    elif status not in ("reverted", "ready") and availability == "expired":
        label += "（快照已过期）"
    return label


def _undo_short_id(turn_id: str) -> str:
    return turn_id[:8]


def _undo_resolve_turn_id(session_name: str, value: str) -> str:
    tu = _undo_service()
    value = str(value or "").strip()
    states = tu.list_turn_undo_states(_undo_session_id(session_name), limit=200)
    exact = [s for s in states if s["turn_id"] == value]
    if exact:
        return value
    prefix = [s for s in states if s["turn_id"].startswith(value)]
    if len(prefix) == 1:
        return prefix[0]["turn_id"]
    if len(prefix) > 1:
        raise ValueError(
            f"回合 ID 前缀 {value} 匹配到 {len(prefix)} 个回合，请使用完整 ID"
        )
    raise ValueError(f"当前会话不存在回合: {value}")


def _undo_print_preview(preview) -> None:
    from core.turn_undo.models import TurnUndoStatus

    if preview.status is TurnUndoStatus.CONFLICTED and preview.conflicts:
        print_formatted_text(HTML("<ansired>⚠️ 撤销受阻：以下文件与回合结束时的状态不一致，本次没有修改任何文件：</ansired>"))
        for c in preview.conflicts:
            print_formatted_text(HTML(
                f"  <ansired>✗</ansired> {_esc(c.display_path)}  "
                f"<ansigray>预期 {_esc(node_kind_label(c.expected.kind))}，"
                f"当前 {_esc(node_kind_label(c.actual.kind))}</ansigray>"
            ))
        print_formatted_text(HTML("<ansigray>  处理磁盘文件后可重新预检；不存在强制覆盖。</ansigray>"))
        return
    print_formatted_text(HTML(
        f"<ansicyan>🔍 撤销影响：共 {preview.file_count} 个文件 — "
        f"恢复原内容 {preview.restore_content_count}，"
        f"删除本轮新建 {preview.delete_created_count}，"
        f"恢复本轮删除 {preview.restore_deleted_count}</ansicyan>"
    ))
    if preview.created_files:
        for path in preview.created_files:
            print_formatted_text(HTML(f"  <ansiyellow>将删除新建文件: {_esc(path)}</ansiyellow>"))
    print_formatted_text(HTML("<ansigray>  聊天历史保留，星瑶将记住文件结果已失效。</ansigray>"))
    if preview.has_uncovered_command_effects:
        print_formatted_text(HTML(
            "<ansiyellow>⚠️ 本轮执行过通用命令：仅恢复列出的受控文件，"
            "命令的其他副作用（进程/网络/数据库等）不撤销。</ansiyellow>"
        ))
    if preview.subsequent_completed_turns > 0:
        print_formatted_text(HTML(
            f"<ansiyellow>⚠️ 目标回合之后还有 {preview.subsequent_completed_turns} 个已完成回合，"
            "可能存在语义依赖，撤销后请重新检查或运行测试。</ansiyellow>"
        ))


def node_kind_label(kind: str) -> str:
    return {
        "missing": "不存在",
        "regular_file": "普通文件",
        "symlink": "符号链接",
        "directory": "目录",
        "special": "特殊文件",
    }.get(str(kind), str(kind))


def _undo_print_result(result) -> None:
    from core.turn_undo.models import TurnUndoStatus

    if result.idempotent:
        print_formatted_text(HTML(
            f"<ansiyellow>ℹ 该回合此前已撤销过（{_esc(str(result.original_reverted_at_ms))}），"
            "本次未修改任何文件。</ansiyellow>"
        ))
        return
    total = (len(result.restored_files) + len(result.deleted_created_files)
             + len(result.restored_deleted_files))
    print_formatted_text(HTML(
        f"<ansigreen>✅ 撤销完成：共 {total} 个文件 — "
        f"恢复原内容 {len(result.restored_files)}，"
        f"删除本轮新建 {len(result.deleted_created_files)}，"
        f"恢复本轮删除 {len(result.restored_deleted_files)}。</ansigreen>"
    ))
    print_formatted_text(HTML("<ansigray>  聊天历史保留；通用命令与外部副作用未随文件恢复。</ansigray>"))


# ── 断点续跑（规格 §4.1 CLI 入口，S4）─────────────────────────────
# 与 /undo 同级：开关关闭时只提示不动作；开启后一律"打印断点 → 一次性确认 →
# ResumeExecutor"，绝不静默续跑（规格 §9.9）。
_RESUME_REASON_TEXT = {
    "disabled": "断点续跑未启用（resume.enabled=false）",
    "no_resumable_turn": "本会话没有可续跑的中断回合",
    "anchor_missing": "缺少缓存锚点（父回合没有已完成的 Step），请手动重来",
    "session_archived": "会话已归档，请先取消归档后再续跑",
    "undo_recovering": "会话处于撤销恢复态，请先执行 /undo recover",
    "owner_still_alive": "该回合可能仍在其他进程运行，已拒绝续跑",
    "confirm_required": "仍有写操作待确认，已暂停（零工具执行、零模型请求）",
    "hash_anchor_mismatch": "缓存锚点已变化（历史被改写），请手动重来",
    "planner_failed": "规划层故障，无法确定断点，请手动重来",
    "resume_cancelled": "续跑已取消",
}


def _resume_reason_text(code: str) -> str:
    return _RESUME_REASON_TEXT.get(str(code or ""), str(code or "未知原因"))


def _resume_switch_enabled() -> bool:
    try:
        section = config._load().get("resume") or {}
    except Exception:
        return False
    return bool(section.get("enabled")) if isinstance(section, dict) else False


def _resume_make_planner(manager_obj):
    """装配规划层；默认注册表构造失败降级为 None（planner 会判工具不可用→BLOCK）。"""
    from core.harness.resume import ResumePlanner

    registry = None
    try:
        from core.tool_registry import build_default_registry

        registry = build_default_registry()
    except Exception:
        logging.getLogger(__name__).warning(
            "断点续跑：默认工具注册表构造失败，按无注册表降级", exc_info=True
        )
        registry = None
    return ResumePlanner(manager_obj, registry)


def _resume_journal_writer(manager_obj):
    from core.harness.journal import (
        PROCESS_OWNER_ID,
        HarnessJournal,
        journal_storage_supported,
    )

    if not journal_storage_supported(manager_obj):
        return None
    return HarnessJournal(
        manager_obj,
        PROCESS_OWNER_ID,
        clock_ms=lambda: time.time_ns() // 1_000_000,
    )


def _run_resume_turn_cli_worker(session_name: str, *, turn_id: str, parent_turn_id: str) -> None:
    global active_cancel_event, active_turn_id

    cancel_event = None
    _prev_thread_session = _app_ctx.get_thread_session()
    try:
        from core.agent_service import build_resume_messages
        from core.llm import chat_with_model_events
        from core.model_manager import get_active_model

        active_model = get_active_model(session_name)
        if not active_model or not active_model.get("litellm_model"):
            schedule_ui(lambda: print_formatted_text(HTML(
                "<ansired>❌ 断点续跑未执行：当前未激活任何 LLM 模型。</ansired>"
            )))
            return
        model_name = active_model["litellm_model"]
        api_key = active_model.get("api_key", "") or ""
        api_base = active_model.get("base_url", "") or ""
        model_id = active_model.get("id")
        history = build_resume_messages(manager, session_name, is_gui=False)
        cancel_event = threading.Event()
        active_cancel_event = cancel_event
        active_turn_id = turn_id
        try:
            from core import tool_cache as _tool_cache_cli

            _tool_cache_cli.set_current_session(session_name)
        except Exception:
            pass
        _app_ctx.set_thread_session(session_name)
        schedule_ui(lambda: print_formatted_text(HTML(
            "<ansigray>↻ 断点续跑：正在从上次中断处继续…</ansigray>"
        )))
        response_gen = chat_with_model_events(
            model_name,
            api_key,
            history,
            api_base,
            persist=True,
            turn_id=turn_id,
            cancel_event=cancel_event,
            parent_session=session_name,
            parent_turn_id=parent_turn_id,
            permission_owner=turn_id,
            permission_source="主代理",
            model_id=model_id,
            request_reason="resume",
            resume_turn=True,
        )
        # 事件统一交回主 CLI 线程渲染（后台线程绝不直接 print）
        _forward_response_events(response_gen)
    except Exception as e:
        logging.getLogger("py.warnings").exception(
            "CLI 断点续跑回合失败 session=%s turn=%s", session_name, turn_id
        )
        message = _esc(str(e))
        schedule_ui(lambda text=message: print_formatted_text(HTML(
            f"<ansired>❌ 断点续跑失败：{text}</ansired>"
        )))
    finally:
        try:
            _app_ctx.set_thread_session(_prev_thread_session)
        except Exception:
            pass
        if cancel_event is not None and active_cancel_event is cancel_event:
            active_cancel_event = None
        if active_turn_id == turn_id:
            active_turn_id = None
        schedule_ui(_reset_turn_state)


def _resume_print_plan(plan, aborts) -> None:
    anchor_state = "完整" if plan.anchor.complete() else "缺失"
    print_formatted_text(HTML(
        "<ansicyan><b>断点续跑</b></ansicyan> "
        f"父回合 {_esc(_undo_short_id(plan.parent_turn_id))} · "
        f"从第 {plan.resume_from_step_ordinal} 个 Step 继续 · 锚点{anchor_state}"
    ))
    if plan.replayable:
        names = ", ".join(sorted({call.tool_name for call in plan.replayable}))
        print_formatted_text(HTML(
            f"<ansigray>  可回灌的只读调用 {len(plan.replayable)} 个（{_esc(names)}）："
            "结果直接取自 messages，不会重新执行。</ansigray>"
        ))
    if plan.fresh:
        names = ", ".join(sorted({call.tool_name for call in plan.fresh}))
        print_formatted_text(HTML(
            f"<ansigray>  需重新获取结果的只读调用 {len(plan.fresh)} 个（{_esc(names)}）。</ansigray>"
        ))
    if plan.blocked:
        print_formatted_text(HTML("<ansiyellow>  待确认清单（写操作/不确定态，需逐项同意）：</ansiyellow>"))
        for call in plan.blocked:
            print_formatted_text(HTML(
                f"<ansiyellow>    · 第 {call.call_ordinal} 个调用 {_esc(call.tool_name)}"
                f" — {_esc(call.reason)}</ansiyellow>"
            ))
    if aborts:
        reasons = "、".join(_resume_reason_text(code) for code in aborts)
        print_formatted_text(HTML(f"<ansired>❌ 不可续跑：{_esc(reasons)}</ansired>"))


def _handle_resume_command(session_name: str) -> None:
    if not _resume_switch_enabled():
        print_formatted_text(HTML("<ansiyellow>断点续跑未启用（resume.enabled=false）。</ansiyellow>"))
        print_formatted_text(HTML(
            '<ansigray>  如需启用：在 config.json 写入 "resume": {"enabled": true} 后重启。</ansigray>'
        ))
        return
    if not session_name:
        print_formatted_text(HTML("<ansired>⚠️ 当前没有活动会话，无法确定续跑目标。</ansired>"))
        return
    from core.harness.resume import ResumeExecutor

    planner = _resume_make_planner(manager)
    plan = planner.plan(session_name)
    if plan is None:
        print_formatted_text(HTML("<ansigray>ℹ 没有断点信息（本会话最近没有中断的回合），请手动重来。</ansigray>"))
        return
    aborts = [str(code) for code in (planner.aborts(plan) or ())]
    _resume_print_plan(plan, aborts)
    if aborts:
        return
    question = "确认从断点继续？"
    if plan.blocked:
        question = f"确认从断点继续？将一次确认全部 {len(plan.blocked)} 项写操作"
    if not _undo_confirm(question):
        print_formatted_text(HTML("<ansigray>已取消（默认 N）：父回合保持中断，未做任何修改。</ansigray>"))
        return
    confirmed: set = {call.call_ordinal for call in plan.blocked}

    def _request_resume_turn(
        *, turn_id: str, parent_turn_id: str, request_reason: str = "resume"
    ) -> None:
        """request_fn 契约（core/harness/resume.py）：只发起，绝不阻塞。

        本命令由 CLI 主提示循环同步调用：若在此同步跑完整个模型回合，界面会
        整段冻住（既看不到输出也无法响应 Ctrl+C）。故起 daemon 线程后立即返回，
        真实请求与事件渲染都由 _run_resume_turn_cli_worker 负责。
        """
        threading.Thread(
            target=_run_resume_turn_cli_worker,
            args=(session_name,),
            kwargs={"turn_id": turn_id, "parent_turn_id": parent_turn_id},
            daemon=True,
            name=f"cli-resume-turn-{session_name}",
        ).start()

    result = ResumeExecutor(
        manager,
        planner,
        writer=_resume_journal_writer(manager),
        request_fn=_request_resume_turn,
    ).run(plan, confirmed=confirmed)
    if result.get("ok"):
        turn_id = str(result.get("turn_id") or "")
        note = (
            "（dry-run：请求层未接线，未发出模型请求）"
            if result.get("dry_run")
            else "（模型请求已在后台线程发出，输出稍后到达）"
        )
        print_formatted_text(HTML(
            f"<ansigreen>✅ 已从断点续跑：新回合 {_esc(_undo_short_id(turn_id))}，"
            f"回灌只读调用 {int(result.get('replayed') or 0)} 个{_esc(note)}。</ansigreen>"
        ))
        return
    first = str(result.get("reason") or "")
    print_formatted_text(HTML(f"<ansired>❌ 续跑未执行：{_esc(_resume_reason_text(first))}</ansired>"))


def _undo_confirm(question: str) -> bool:
    def _ask() -> bool:
        try:
            answer = input(f"{question} [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")
    try:
        return bool(run_in_terminal(_ask))
    except Exception:
        return _ask()


def _undo_interactive_revert(session_name: str, turn_id: str) -> None:
    tu = _undo_service()
    session_id = _undo_session_id(session_name)
    preview, token = tu.preview_turn_undo(session_id, turn_id)
    _undo_print_preview(preview)
    from core.turn_undo.models import TurnUndoStatus

    if preview.status is TurnUndoStatus.CONFLICTED and preview.conflicts:
        return
    if not _undo_confirm(f"确认撤销回合 {_undo_short_id(turn_id)} 的全部文件修改?"):
        print_formatted_text(HTML("<ansigray>已取消（默认 N），本次没有修改任何文件。</ansigray>"))
        return
    preview2, token2 = tu.preview_turn_undo(session_id, turn_id)
    if preview2.status is TurnUndoStatus.CONFLICTED and preview2.conflicts:
        _undo_print_preview(preview2)
        return
    try:
        result = tu.execute_turn_undo(session_id, turn_id, token2)
        _undo_print_result(result)
    except Exception as e:
        code = getattr(e, "code", "")
        if code == "revert_interrupted":
            print_formatted_text(HTML(
                f"<ansired>⚠️ 撤销事务中断：{_esc(str(e))}</ansired>"
            ))
            _undo_print_recover_hint(session_id, turn_id)
        elif code == "file_conflict":
            print_formatted_text(HTML(f"<ansired>⚠️ {_esc(str(e))}</ansired>"))
        else:
            print_formatted_text(HTML(f"<ansired>⚠️ 撤销失败：{_esc(str(e))}</ansired>"))


def _undo_print_recover_hint(session_id: int, turn_id: str) -> None:
    status = _undo_service().get_turn_undo_status(session_id, turn_id)
    if status and status.get("last_operation_id"):
        print_formatted_text(HTML(
            f"  <ansiyellow>使用 /undo recover {_esc(status['last_operation_id'])} "
            "查看恢复选项（继续撤销或取消撤销）。</ansiyellow>"
        ))


def handle_undo_command(command_text: str, current_session: str) -> None:
    tu = _undo_service()
    parts = command_text.strip().split(maxsplit=2)
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        states = tu.list_turn_undo_states(_undo_session_id(current_session))
        if not states:
            print_formatted_text(HTML("<ansiyellow>ℹ 当前会话没有按回合撤销记录。</ansiyellow>"))
            return
        print_formatted_text(HTML(f"<ansicyan>📋 当前会话共 {len(states)} 个回合检查点:</ansicyan>"))
        for s in states:
            desc = s.get("description") or ""
            if len(desc) > 40:
                desc = desc[:37] + "..."
            owner = " [后台任务]" if s.get("owner_kind") == "background_task" else ""
            print_formatted_text(HTML(
                f"  <ansigreen>{_esc(_undo_short_id(s['turn_id']))}</ansigreen> "
                f"{_esc(_undo_status_label(s))}  "
                f"文件: {s['file_count']}  <ansigray>+{s['net_added']} -{s['net_removed']} "
                f"{_esc(desc)}{_esc(owner)}</ansigray>"
            ))
        return

    if sub == "preview":
        if len(parts) < 3:
            print_formatted_text(HTML("<ansired>⚠️ 用法: /undo preview &lt;turn_id&gt;</ansired>"))
            return
        try:
            turn_id = _undo_resolve_turn_id(current_session, parts[2])
            preview, _ = tu.preview_turn_undo(_undo_session_id(current_session), turn_id)
            _undo_print_preview(preview)
        except Exception as e:
            print_formatted_text(HTML(f"<ansired>⚠️ {_esc(str(e))}</ansired>"))
        return

    if sub == "status":
        if len(parts) < 3:
            print_formatted_text(HTML("<ansired>⚠️ 用法: /undo status &lt;turn_id&gt;</ansired>"))
            return
        try:
            turn_id = _undo_resolve_turn_id(current_session, parts[2])
            status = tu.get_turn_undo_status(_undo_session_id(current_session), turn_id)
            if status is None:
                print_formatted_text(HTML(f"<ansired>⚠️ 未找到回合: {_esc(turn_id)}</ansired>"))
                return
            print_formatted_text(HTML(
                f"<ansicyan>回合 {_esc(_undo_short_id(status['turn_id']))} "
                f"({_esc(status['status'])})，快照: {_esc(status['snapshot_availability'])}，"
                f"文件 {len(status.get('files', []))} 个</ansicyan>"
            ))
            for f in status.get("files", []):
                print_formatted_text(HTML(
                    f"  {_esc(f['display_path'])}  "
                    f"<ansigray>+{f['net_added']} -{f['net_removed']} {_esc(f.get('diff_kind') or '')}</ansigray>"
                ))
        except Exception as e:
            print_formatted_text(HTML(f"<ansired>⚠️ {_esc(str(e))}</ansired>"))
        return

    if sub == "recover":
        if len(parts) < 3:
            print_formatted_text(HTML("<ansired>⚠️ 用法: /undo recover &lt;operation_id&gt;</ansired>"))
            return
        _undo_interactive_recovery(current_session, parts[2].strip())
        return

    if sub and sub not in ("list", "preview", "status", "recover"):
        try:
            turn_id = _undo_resolve_turn_id(current_session, sub)
        except Exception as e:
            print_formatted_text(HTML(f"<ansired>⚠️ {_esc(str(e))}</ansired>"))
            return
        _undo_interactive_revert(current_session, turn_id)
        return

    session_id = _undo_session_id(current_session)
    candidates = tu.get_default_storage().list_workdir_ready_checkpoints(
        session_id, _undo_workdir_root()
    )
    candidates = [c for c in candidates if c.status_enum.value == "ready"]
    if not candidates:
        print_formatted_text(HTML(
            "<ansiyellow>ℹ 当前会话/工作区没有可撤销的回合。</ansiyellow>\n"
            "<ansigray>  使用 /undo list 查看全部检查点状态。</ansigray>"
        ))
        return
    _undo_interactive_revert(current_session, candidates[0].turn_id)


def _undo_interactive_recovery(session_name: str, operation_id: str) -> None:
    tu = _undo_service()
    session_id = _undo_session_id(session_name)
    try:
        preview, token = tu.preview_turn_undo_recovery(session_id, operation_id, "continue_revert")
    except Exception as e:
        print_formatted_text(HTML(f"<ansired>⚠️ {_esc(str(e))}</ansired>"))
        return
    print_formatted_text(HTML("<ansicyan>🔍 撤销中断恢复选项：</ansicyan>"))
    if preview.processed_files:
        print_formatted_text(HTML(f"  已恢复: {_esc('、'.join(preview.processed_files))}"))
    if preview.remaining_files:
        print_formatted_text(HTML(f"  未处理: {_esc('、'.join(preview.remaining_files))}"))
    if preview.unknown_files:
        print_formatted_text(HTML(
            f"  <ansired>状态未知（需人工核对，已拒绝自动写入）: "
            f"{_esc('、'.join(preview.unknown_files))}</ansired>"
        ))
        return
    print_formatted_text(HTML("  1. 继续撤销 — 使用本轮开始前快照完成剩余撤销"))
    print_formatted_text(HTML("  2. 取消撤销 — 把已处理路径恢复到点击撤销之前"))
    choice = ""
    try:
        choice = run_in_terminal(lambda: input("选择恢复方向 [1=继续撤销 / 2=取消撤销 / 回车取消]: ").strip()) or ""
    except Exception:
        choice = ""
    if choice not in ("1", "2"):
        print_formatted_text(HTML("<ansigray>已取消，本次没有修改任何文件。</ansigray>"))
        return
    action = "continue_revert" if choice == "1" else "cancel_revert"
    try:
        _, token = tu.preview_turn_undo_recovery(session_id, operation_id, action)
        question = (
            f"确认继续撤销（完成剩余 {len(preview.remaining_files)} 个文件）?"
            if action == "continue_revert"
            else "确认取消撤销（恢复已处理文件到点击撤销前的状态）?"
        )
        if not _undo_confirm(question):
            print_formatted_text(HTML("<ansigray>已取消，本次没有修改任何文件。</ansigray>"))
            return
        result = tu.execute_turn_undo_recovery(session_id, operation_id, action, token)
        if result.action.value == "continue_revert":
            print_formatted_text(HTML(
                f"<ansigreen>✅ 撤销已继续完成：{len(result.affected_files)} 个文件恢复。</ansigreen>"
            ))
        else:
            print_formatted_text(HTML(
                "<ansigreen>✅ 撤销事务已取消：文件恢复到点击撤销之前的状态，回合重新可撤销。</ansigreen>"
            ))
    except Exception as e:
        print_formatted_text(HTML(f"<ansired>⚠️ 恢复失败：{_esc(str(e))}</ansired>"))


def handle_checkpoint_command(command_text: str, current_session: str) -> None:
    parts = command_text.strip().split(maxsplit=2)
    sub = parts[1] if len(parts) > 1 else "list"

    if sub == "list":
        tu = _undo_service()
        states = tu.list_turn_undo_states(_undo_session_id(current_session))
        if not states:
            print_formatted_text(HTML("<ansiyellow>ℹ 当前会话没有检查点记录。</ansiyellow>"))
            print_formatted_text(HTML("<ansigray>  每轮对话开始前会自动创建持久化检查点，记录本轮受控文件修改。</ansigray>"))
            print_formatted_text(HTML("<ansigray>  使用 /checkpoint restore &lt;turn_id&gt; 或 /undo 可整体回滚该轮文件修改。</ansigray>"))
            return
        print_formatted_text(HTML(f"<ansicyan>📋 当前会话共有 {len(states)} 个检查点:</ansicyan>"))
        for s in states:
            desc = s.get("description") or "(无描述)"
            if len(desc) > 50:
                desc = desc[:47] + "..."
            print_formatted_text(HTML(
                f"  <ansigreen>{_esc(_undo_short_id(s['turn_id']))}</ansigreen> "
                f"{_esc(_undo_status_label(s))}  文件修改: {s['file_count']}  "
                f"<ansigray>{_esc(desc)}</ansigray>"
            ))
        print_formatted_text(HTML(
            "<ansigray>\n  使用 /checkpoint restore &lt;turn_id&gt; 或 /undo 整体回滚该轮所有文件修改</ansigray>"
        ))
        return

    if sub == "restore":
        if len(parts) < 3:
            print_formatted_text(HTML("<ansired>⚠️ 用法: /checkpoint restore &lt;turn_id&gt;</ansired>"))
            return
        try:
            turn_id = _undo_resolve_turn_id(current_session, parts[2].strip())
        except Exception as e:
            print_formatted_text(HTML(f"<ansired>⚠️ {_esc(str(e))}</ansired>"))
            print_formatted_text(HTML("<ansigray>  使用 /checkpoint list 查看所有可用的检查点 ID</ansigray>"))
            return
        _undo_interactive_revert(current_session, turn_id)
        return

    print_formatted_text(HTML("<ansired>⚠️ 未知子命令。用法: /checkpoint [list|restore &lt;turn_id&gt;]</ansired>"))


def _diff_stat_html(added: int, removed: int, *, width: int = 5) -> str:
    total = max(0, int(added)) + max(0, int(removed))
    if total <= 0 or width <= 0:
        return ""
    slots = min(width, total)
    plus_slots = round(max(0, added) / total * slots)
    if added > 0 and plus_slots == 0:
        plus_slots = 1
    if removed > 0 and plus_slots == slots and slots > 1:
        plus_slots = slots - 1
    minus_slots = slots - plus_slots
    t = _get_theme(current_theme)
    parts = []
    if plus_slots:
        parts.append(f"<style fg='{t['ok']}'>{'+' * plus_slots}</style>")
    if minus_slots:
        parts.append(f"<style fg='{t['err']}'>{'-' * minus_slots}</style>")
    return "".join(parts)


def _progress_bar_html(completed: int, total: int, *, width: int = 10) -> str:
    width = max(2, int(width))
    if total <= 0:
        filled = 0
    else:
        ratio = max(0.0, min(1.0, completed / total))
        filled = round(ratio * width)
        if completed > 0 and filled == 0:
            filled = 1
        if completed < total and filled == width:
            filled = width - 1
    empty = width - filled
    t = _get_theme(current_theme)
    parts = []
    if filled:
        parts.append(f"<style fg='{t['accent']}'>{'━' * filled}</style>")
    if empty:
        parts.append(f"<style fg='{t['border_dim']}'>{'━' * empty}</style>")
    return "".join(parts)


def print_turn_file_summary_if_ready(session_name: str, turn_id: str) -> None:
    try:
        tu = _undo_service()
        session_id = tu.resolve_session_id(session_name)
        status = tu.get_turn_undo_status(session_id, turn_id)
        if status is None or status.get("status") != "ready":
            return
        files = status.get("files") or []
        if not files:
            return
        t = _get_theme(current_theme)
        net_added = sum(f.get("net_added") or 0 for f in files)
        net_removed = sum(f.get("net_removed") or 0 for f in files)
        rule_w = min(max(_terminal_width() - 6, 48), 72)
        header_plain = f" ✎ 本轮文件变更 · {len(files)} 个文件 +{net_added} -{net_removed} "
        top_dash = "─" * max(2, rule_w - 2 - _term_display_width(header_plain))

        card_lines = [
            "",
            (
                f"  <style fg='{t['border']}'>╭─</style>"
                f"<style fg='{t['accent']}'><b> ✎ 本轮文件变更</b></style>"
                f"<style fg='{t['text_muted']}'> · {len(files)} 个文件已编辑 </style>"
                f"<style fg='{t['ok']}'>+{net_added}</style> "
                f"<style fg='{t['err']}'>-{net_removed}</style> "
                f"<style fg='{t['border']}'>{top_dash}</style>"
            ),
        ]
        shown = files[:5]
        path_w = max(18, min(42, max(_term_display_width(f.get("display_path") or "") for f in shown) + 2))
        for f in shown:
            fa = int(f.get("net_added") or 0)
            fr = int(f.get("net_removed") or 0)
            dpath = str(f.get("display_path") or "")
            path_padded = _esc(_pad_right(_truncate_visible(dpath, path_w), path_w))
            stat_bar = _diff_stat_html(fa, fr, width=5)
            bar_suffix = f"  {stat_bar}" if stat_bar else ""
            card_lines.append(
                f"  <style fg='{t['border']}'>│</style> "
                f"<style fg='{t['text']}'>{path_padded}</style> "
                f"<style fg='{t['ok']}'>+{fa}</style> "
                f"<style fg='{t['err']}'>-{fr}</style>"
                f"{bar_suffix}"
            )
        if len(files) > 5:
            card_lines.append(
                f"  <style fg='{t['border']}'>│</style> "
                f"<style fg='{t['text_muted']}'>… 还有 {len(files) - 5} 个文件（/undo status 查看全部）</style>"
            )
        if status.get("has_uncovered_command_effects"):
            card_lines.append(
                f"  <style fg='{t['border']}'>│</style> "
                f"<style fg='{t['warn']}'>! 本轮执行过通用命令：命令的其他副作用不在撤销范围内。</style>"
            )
        short_id = _undo_short_id(status["turn_id"])
        foot_plain = f" ↩ 可撤销本轮修改：/undo {short_id} "
        foot_dash = "─" * max(2, rule_w - 2 - _term_display_width(foot_plain))
        card_lines.append(
            f"  <style fg='{t['border']}'>╰─</style>"
            f"<style fg='{t['text_muted']}'> ↩ 可撤销本轮修改：</style>"
            f"<style fg='{t['accent_soft']}'>/undo {_esc(short_id)}</style> "
            f"<style fg='{t['border']}'>{foot_dash}</style>"
        )
        card_lines.append("")
        _print_ui_html("\n".join(card_lines))
    except Exception:
        logging.getLogger("py.warnings").debug(
            "回合文件摘要展示失败", exc_info=True
        )


_update_downloader = None
_last_update_installer = {"path": ""}


def _get_update_downloader():
    global _update_downloader
    if _update_downloader is None:
        from core.updater import UpdateDownloader
        _update_downloader = UpdateDownloader()
    return _update_downloader


def _print_threadsafe(html_text: str) -> None:
    """后台线程打印代理：有 UI 循环时把打印调度回主线程（AGENTS.md 线程铁律）；
    无循环（测试/特殊环境）时按当前线程直接打印。"""
    app = app_instance
    loop = getattr(app, "loop", None) if app is not None else None
    if loop is None:
        print_formatted_text(HTML(html_text))
        return
    is_closed = getattr(loop, "is_closed", None)
    if callable(is_closed) and is_closed():
        return
    try:
        loop.call_soon_threadsafe(print_formatted_text, HTML(html_text))
    except RuntimeError:
        pass


def _update_cmd_check() -> None:
    # fetch_latest_release 网络超时最长 8 秒，必须放后台线程，否则冻结 UI；
    # 结果统一经 _print_threadsafe 回主线程
    from core.updater import fetch_latest_release
    print_formatted_text(HTML("<ansigray>⏳ 正在检查更新…</ansigray>"))

    def _worker():
        try:
            info = fetch_latest_release()
        except Exception as exc:
            _print_threadsafe(f"<ansired>❌ 检查更新失败：{_esc(str(exc))}</ansired>")
            return
        _print_threadsafe_update_check(info)

    threading.Thread(target=_worker, daemon=True).start()


def _print_threadsafe_update_check(info: dict) -> None:
    """渲染检查结果（逐行经 _print_threadsafe 回主线程；测试可同步直调）。"""
    if not info.get("ok"):
        _print_threadsafe(f"<ansired>❌ {_esc(str(info.get('msg') or '检查更新失败'))}</ansired>")
        return
    current = str(info.get("current_version") or "")
    if not info.get("has_update"):
        _print_threadsafe(f"<ansigreen>✔ 已是最新版本（当前 v{_esc(current)}）。</ansigreen>")
        return
    from core.updater import find_cached_installer
    latest = str(info.get("latest_version") or "")
    size_mb = int(info.get("asset_size") or 0) / (1024 * 1024)
    split_mark = f" · 分卷 x{info.get('part_count')}" if info.get("is_split") else ""
    _print_threadsafe(
        f"<b><ansicyan>发现新版本 v{_esc(latest)}</ansicyan></b>（当前 v{_esc(current)}）"
        f" · 包大小约 {size_mb:.1f} MB{split_mark}"
    )
    notes = str(info.get("release_notes") or "").strip()
    if notes:
        _print_threadsafe("<ansigray>更新日志（节选）：</ansigray>")
        for line in notes.splitlines()[:40]:
            _print_threadsafe(f"  {_esc(line)}")
    cached = ""
    try:
        cached = find_cached_installer(
            latest, str(info.get("sha256") or ""), int(info.get("asset_size") or 0)
        ) or ""
    except Exception:
        cached = ""
    if cached:
        _print_threadsafe(f"<ansigreen>✔ 检测到已下载的安装包，可直接安装：</ansigreen> {_esc(cached)}")
        _print_threadsafe("  输入 <ansicyan>/update install y</ansicyan> 开始安装（安装会退出 星瑶Code）。")
    else:
        _print_threadsafe("  输入 <ansicyan>/update download</ansicyan> 开始下载。")


def _update_cmd_download() -> None:
    downloader = _get_update_downloader()
    if downloader.is_downloading():
        print_formatted_text(HTML("<ansiyellow>ℹ 已有下载任务进行中，/update cancel 可取消。</ansiyellow>"))
        return
    from core.updater import fetch_latest_release
    print_formatted_text(HTML("<ansigray>⏳ 正在获取版本信息…</ansigray>"))

    def _worker():
        try:
            info = fetch_latest_release()
        except Exception as exc:
            _print_threadsafe(f"<ansired>❌ 检查更新失败：{_esc(str(exc))}</ansired>")
            return
        if not info.get("ok"):
            # 网络失败必须显式报错，不得并入"已是最新版本"（代码审查 Important #1）
            _print_threadsafe(f"<ansired>❌ {_esc(str(info.get('msg') or '检查更新失败'))}</ansired>")
            return
        if not info.get("has_update"):
            _print_threadsafe(f"<ansigreen>✔ 已是最新版本（当前 v{_esc(str(info.get('current_version') or ''))}），无需下载。</ansigreen>")
            return
        version = str(info.get("latest_version") or "")
        part_urls = [str(u) for u in (info.get("part_urls") or []) if str(u).strip()]
        progress_bucket = {"last": -1}

        def _on_progress(data):
            # downloader 工作线程：只经 _print_threadsafe 回主线程，按 10% 步进防刷屏
            try:
                percent = int(data.get("percent") or 0)
            except Exception:
                percent = 0
            bucket = percent // 10
            if bucket > progress_bucket["last"]:
                progress_bucket["last"] = bucket
                _print_threadsafe(f"<ansigray>  下载进度 {min(percent, 100)}%</ansigray>")

        def _on_complete(dest_path):
            _last_update_installer["path"] = str(dest_path)
            _print_threadsafe(f"<ansigreen>✔ 安装包下载完成：{_esc(str(dest_path))}</ansigreen>")
            _print_threadsafe("  输入 <ansicyan>/update install y</ansicyan> 开始安装（安装会退出 星瑶Code）。")

        def _on_error(message):
            _print_threadsafe(f"<ansired>❌ 下载失败：{_esc(str(message))}</ansired>")

        started = downloader.start_download(
            download_url=str(info.get("download_url") or ""),
            target_version=version,
            on_progress=_on_progress,
            on_complete=_on_complete,
            on_error=_on_error,
            part_urls=part_urls,
            expected_sha256=str(info.get("sha256") or "").strip().lower(),
            total_size_hint=int(info.get("asset_size") or 0),
        )
        if started:
            _print_threadsafe(
                f"<ansicyan>⬇ 已开始下载 v{_esc(version)}"
                "（进度按 10% 步进打印，/update cancel 可取消）。</ansicyan>"
            )
        else:
            _print_threadsafe("<ansired>❌ 未能启动下载（可能已有任务在进行中）。</ansired>")

    threading.Thread(target=_worker, daemon=True).start()


def _update_cmd_install(args: list) -> None:
    from core.updater import fetch_latest_release, find_cached_installer, launch_installer
    tokens = [str(t or "").strip() for t in args if str(t or "").strip()]
    confirmed = any(t.lower() in ("y", "yes", "--force") for t in tokens)
    path = " ".join(
        t.strip('"').strip("'") for t in tokens if t.lower() not in ("y", "yes", "--force")
    ).strip()
    if not path:
        path = _last_update_installer["path"]
    if not path:
        try:
            info = fetch_latest_release()
        except Exception:
            info = {}
        if info.get("has_update"):
            try:
                path = find_cached_installer(
                    str(info.get("latest_version") or ""),
                    str(info.get("sha256") or ""),
                    int(info.get("asset_size") or 0),
                ) or ""
            except Exception:
                path = ""
    if not confirmed:
        if path and os.path.isfile(path):
            print_formatted_text(HTML(f"即将安装: {_esc(path)}"))
            print_formatted_text(HTML("确认请输入: <ansicyan>/update install y</ansicyan>（安装会退出 星瑶Code）"))
        else:
            print_formatted_text(HTML("<ansiyellow>ℹ 未找到可安装的安装包。先 /update download，或指定路径: /update install &lt;路径&gt; y</ansiyellow>"))
        return
    if not path or not os.path.isfile(path):
        print_formatted_text(HTML("<ansired>❌ 安装包不存在，请先 /update download。</ansired>"))
        return
    if is_thinking:
        print_formatted_text(HTML("<ansired>❌ 当前有正在执行的回合，请先 Ctrl+C 结束后再安装。</ansired>"))
        return
    try:
        from core.task_manager import list_active_tasks
        active_tasks = list_active_tasks()
    except Exception:
        active_tasks = []
    if active_tasks:
        print_formatted_text(HTML(
            f"<ansired>❌ 还有 {len(active_tasks)} 个后台任务未结束（/tasks 查看），请先处理再安装。</ansired>"
        ))
        return
    if launch_installer(path):
        print_formatted_text(HTML("<ansigreen>✔ 安装向导已启动，星瑶Code 即将退出…</ansigreen>"))

        def _exit_soon():
            time.sleep(0.8)
            # 先优雅退出 prompt_toolkit 事件循环恢复终端状态，再兜底硬退出
            #（对齐 GUI _delayed_exit 的收尾顺序）
            try:
                app = app_instance
                if app is not None:
                    app.exit()
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=_exit_soon, daemon=True).start()
    else:
        print_formatted_text(HTML("<ansired>❌ 唤起安装包失败，请手动运行安装包。</ansired>"))


_GOAL_STATUS_TEXT = {
    "running": "进行中",
    "awaiting_confirm": "待确认",
    "achieved": "已达成",
    "blocked": "受阻（轮次用尽）",
    "stopped": "已停止",
    "interrupted": "已中断",
}

# 只接受这三种状态 resume：achieved 再跑没有意义；awaiting_confirm 需要的是
# "确认达成"而不是"再来一轮"；running 说明线程还在跑（重复启动会被拒）。
# 取值必须与 core.slash_commands.goal.state 的 STATUS_* 一致——那里是权威定义，
# 这里只是"可恢复集合"的适配层视图。
_GOAL_RESUMABLE = ("blocked", "stopped", "interrupted")

_GOAL_RUNS = {}
_GOAL_RUNS_LOCK = threading.Lock()


def _format_goal_status(state) -> str:
    if state is None:
        return "当前会话没有进行中的目标"
    summary = ""
    if state.history:
        summary = str(state.history[-1].get("summary") or "")
    status_text = _GOAL_STATUS_TEXT.get(state.status, state.status)
    return (
        f"第 {state.round}/{state.max_rounds} 轮 · {status_text} · "
        f"{summary or '（还没有完成任何一轮）'}"
    )


def _goal_notice(html_text: str) -> None:
    """打印一行目标模式提示（跨线程安全）。

    有 prompt_toolkit App 时统一经 schedule_ui 回到 UI 线程并通过 _print_ui_html 打印，
    遵守线程纪律并与 Markdown 流保持保序；没有 App（headless）时直接打印。
    """
    if app_instance is None:
        try:
            print_formatted_text(HTML(html_text))
        except Exception:
            pass
        return
    schedule_ui(lambda: _print_ui_html(html_text))


def _goal_store():
    from core.slash_commands.goal.cli_executor import open_goal_store

    return open_goal_store()


def _submit_goal_turn(prompt: str, session_name: str) -> dict:
    task = {
        "text": prompt,
        "was_queued": False,
        "is_skill_call": False,
        "session": session_name,
        "result": {},
    }
    msg_queue.put(task)
    return task["result"]


def _goal_has_running() -> bool:
    with _GOAL_RUNS_LOCK:
        return bool(_GOAL_RUNS)


def _set_all_goal_cancel_events() -> int:
    with _GOAL_RUNS_LOCK:
        events = list(_GOAL_RUNS.values())
    count = 0
    for event in events:
        try:
            event.set()
            count += 1
        except Exception:
            continue
    return count


def _goal_start_run(state):
    from core.slash_commands.goal.cli_executor import CliGoalExecutor
    from core.slash_commands.goal.runner import run_goal_loop

    session_name = state.session
    with _GOAL_RUNS_LOCK:
        existing = _GOAL_RUNS.get(session_name)
        if existing is not None and not existing.is_set():
            # 检查与写入必须在同一把锁内：每个会话最多一条循环，两条同时推进
            # 会把同一份 history / 状态文件互相覆盖。
            return False, "该会话已有运行中的目标"
        cancel_event = threading.Event()
        _GOAL_RUNS[session_name] = cancel_event
    try:
        store = _goal_store()
    except Exception as exc:
        with _GOAL_RUNS_LOCK:
            if _GOAL_RUNS.get(session_name) is cancel_event:
                _GOAL_RUNS.pop(session_name, None)
        return False, f"打开目标状态仓库失败：{exc}"

    # host 传当前模块自身（sys.modules[__name__]）：cli_executor 不能 `import main`
    # ——CLI 以 `python main.py` 启动时入口模块名是 "__main__"，再 import main 会把
    # 入口文件执行第二遍、得到另一份 msg_queue，目标回合就投进没人消费的队列了。
    host = sys.modules[__name__]
    executor = CliGoalExecutor(host, session_name)

    def on_event(event, current_state):
        """循环进度回调：只打印，不碰界面对象，也绝不把异常抛回循环。"""
        try:
            t = _get_theme(current_theme)
            if event == "round_done":
                summary = ""
                if current_state.history:
                    summary = str(current_state.history[-1].get("summary") or "")
                _goal_notice(
                    f"  <style fg='{t['accent']}'><b>◎ 目标第 {current_state.round}/{current_state.max_rounds} 轮</b></style> "
                    f"<style fg='{t['text_muted']}'>{_esc(summary)}</style>"
                )
            elif event == "finished":
                hint = (
                    "，可用 /goal resume 继续"
                    if current_state.status in _GOAL_RESUMABLE
                    else ""
                )
                _goal_notice(
                    f"  <style fg='{t['ok']}'><b>✔ 目标流程结束{hint}</b></style> "
                    f"<style fg='{t['text_muted']}'>{_esc(_format_goal_status(current_state))}</style>"
                )
        except Exception:
            logging.getLogger("py.warnings").debug("目标进度提示打印失败", exc_info=True)

    def worker():
        try:
            run_goal_loop(
                state,
                executor,
                store,
                on_event=on_event,
                cancel_event=cancel_event,
                run_check=executor.run_check,
            )
        except Exception as exc:
            logging.getLogger("py.warnings").exception("目标循环异常退出")
            t = _get_theme(current_theme)
            _goal_notice(f"  <style fg='{t['err']}'>❌ 目标循环异常终止：{_esc(exc)}</style>")
        finally:
            with _GOAL_RUNS_LOCK:
                if _GOAL_RUNS.get(session_name) is cancel_event:
                    _GOAL_RUNS.pop(session_name, None)

    thread = threading.Thread(
        target=worker, name=f"goal-loop-{session_name}", daemon=True
    )
    thread.start()
    return True, ""


def _goal_handle_status(session_name) -> None:
    t = _get_theme(current_theme)
    try:
        state = _goal_store().load(session_name)
    except Exception as exc:
        print_formatted_text(HTML(f"  <style fg='{t['err']}'>❌ 读取目标状态失败：{_esc(exc)}</style>"))
        return
    if state is None:
        print_formatted_text(
            HTML(f"  <style fg='{t['warn']}'>ℹ {_esc(_format_goal_status(None))}</style>")
        )
        return
    bar = _progress_bar_html(state.round, max(1, state.max_rounds), width=12)
    print_formatted_text(HTML(
        f"  <style fg='{t['accent']}'><b>◎ {_esc(_format_goal_status(state))}</b></style>  {bar}"
    ))
    if state.goal:
        print_formatted_text(HTML(
            f"    <style fg='{t['text_muted']}'>目标：{_esc(state.goal)}</style>"
        ))
    if state.check:
        print_formatted_text(HTML(
            f"    <style fg='{t['text_muted']}'>验证命令：{_esc(state.check)}</style>"
        ))


def _goal_handle_stop(session_name) -> None:
    t = _get_theme(current_theme)
    with _GOAL_RUNS_LOCK:
        cancel_event = _GOAL_RUNS.get(session_name)
    if cancel_event is None:
        print_formatted_text(HTML(
            f"  <style fg='{t['warn']}'>ℹ 当前会话没有运行中的目标（/goal status 查看进度）。</style>"
        ))
        return
    cancel_event.set()
    try:
        _cancel_active_turn()
    except Exception:
        logging.getLogger("py.warnings").debug("中断目标回合失败", exc_info=True)
    print_formatted_text(HTML(
        f"  <style fg='{t['ok']}'>✔ 已停止目标：本轮中断，不再自动续跑。</style>"
    ))


def _goal_handle_resume(session_name) -> None:
    from core.slash_commands.goal.state import STATUS_RUNNING

    t = _get_theme(current_theme)
    try:
        store = _goal_store()
        state = store.load(session_name)
    except Exception as exc:
        print_formatted_text(HTML(f"  <style fg='{t['err']}'>❌ 读取目标状态失败：{_esc(exc)}</style>"))
        return
    if state is None:
        print_formatted_text(HTML(
            f"  <style fg='{t['warn']}'>ℹ 当前会话没有可恢复的目标，用 /goal &lt;目标&gt; 新建一个。</style>"
        ))
        return
    if state.status not in _GOAL_RESUMABLE:
        hint = {
            "achieved": "目标已达成，无需恢复；要跑新目标请用 /goal <目标>",
            "awaiting_confirm": "目标在待确认状态，请确认是否达成，或用 /goal <目标> 重新设定",
            "running": "目标已在运行中，无需重复恢复",
        }.get(state.status, "")
        status_text = _GOAL_STATUS_TEXT.get(state.status, state.status)
        print_formatted_text(HTML(
            f"  <style fg='{t['warn']}'>ℹ 当前状态为「{_esc(status_text)}」，不能恢复。{_esc(hint)}</style>"
        ))
        return
    if state.round >= state.max_rounds:
        print_formatted_text(HTML(
            f"  <style fg='{t['warn']}'>ℹ 轮次已用尽（{state.round}/{state.max_rounds}），"
            "请用 /goal &lt;目标&gt; [--rounds N] 重新设定目标与轮次。</style>"
        ))
        return
    state.status = STATUS_RUNNING
    try:
        store.save(state)
    except Exception as exc:
        print_formatted_text(HTML(f"  <style fg='{t['err']}'>❌ 保存目标状态失败：{_esc(exc)}</style>"))
        return
    started, message = _goal_start_run(state)
    if not started:
        print_formatted_text(HTML(f"  <style fg='{t['err']}'>❌ {_esc(message)}</style>"))
        return
    print_formatted_text(HTML(
        f"  <style fg='{t['ok']}'>✔ 已恢复目标（第 {state.round}/{state.max_rounds} 轮起）："
        f"{_esc(state.goal)}</style>"
    ))


def _goal_handle_confirm(session_name) -> None:
    from core.slash_commands.goal.state import (
        STATUS_ACHIEVED,
        STATUS_AWAITING_CONFIRM,
    )

    t = _get_theme(current_theme)
    try:
        store = _goal_store()
        state = store.load(session_name)
    except Exception as exc:
        print_formatted_text(HTML(f"  <style fg='{t['err']}'>❌ 读取目标状态失败：{_esc(exc)}</style>"))
        return
    if state is None:
        print_formatted_text(HTML(
            f"  <style fg='{t['warn']}'>ℹ 当前会话没有可确认的目标，用 /goal &lt;目标&gt; 新建一个。</style>"
        ))
        return
    if state.status != STATUS_AWAITING_CONFIRM:
        status_text = _GOAL_STATUS_TEXT.get(state.status, state.status)
        hint = {
            "running": "目标还在跑，等出现「待确认」再确认达成",
            "achieved": "目标已达成，无需重复确认；要跑新目标请用 /goal <目标>",
        }.get(state.status, "")
        print_formatted_text(HTML(
            f"  <style fg='{t['warn']}'>ℹ 当前状态为「{_esc(status_text)}」，无需确认。{_esc(hint)}</style>"
        ))
        return
    state.status = STATUS_ACHIEVED
    try:
        store.save(state)
    except Exception as exc:
        print_formatted_text(HTML(f"  <style fg='{t['err']}'>❌ 保存目标状态失败：{_esc(exc)}</style>"))
        return
    print_formatted_text(HTML(
        f"  <style fg='{t['ok']}'>✔ 已确认目标达成（第 {state.round}/{state.max_rounds} 轮）："
        f"{_esc(state.goal)}</style>"
    ))


def _goal_handle_start(action, session_name) -> None:
    from core.slash_commands.goal.state import GoalState
    from core.slash_commands.goal_command import DEFAULT_MAX_ROUNDS, GOAL_USAGE

    t = _get_theme(current_theme)
    goal = str(action.get("goal") or "").strip()
    if not goal:
        # 理论上命令层已拦住空目标，这里兜底：没有目标的循环只会空转烧 token
        print_formatted_text(HTML(f"  <style fg='{t['err']}'>❌ 目标不能为空。{_esc(GOAL_USAGE)}</style>"))
        return
    try:
        max_rounds = int(action.get("max_rounds") or 0)
    except (TypeError, ValueError):
        max_rounds = 0
    if max_rounds <= 0:
        max_rounds = DEFAULT_MAX_ROUNDS
    with _GOAL_RUNS_LOCK:
        existing = _GOAL_RUNS.get(session_name)
    if existing is not None and not existing.is_set():
        print_formatted_text(HTML(
            f"  <style fg='{t['err']}'>❌ 当前会话已有运行中的目标，先 /goal stop 再重新设定。</style>"
        ))
        return
    state = GoalState(
        session=session_name,
        goal=goal,
        check=str(action.get("check") or "").strip(),
        max_rounds=max_rounds,
    )
    try:
        _goal_store().save(state)
    except Exception as exc:
        print_formatted_text(HTML(f"  <style fg='{t['err']}'>❌ 保存目标状态失败：{_esc(exc)}</style>"))
        return
    started, message = _goal_start_run(state)
    if not started:
        print_formatted_text(HTML(f"  <style fg='{t['err']}'>❌ {_esc(message)}</style>"))
        return
    suffix = f" · 验证命令：{_esc(state.check)}" if state.check else ""
    print_formatted_text(HTML(
        f"  <style fg='{t['ok']}'>✔ 已启动目标（最多 {state.max_rounds} 轮）：{_esc(state.goal)}"
        f"{suffix}</style>"
    ))
    print_formatted_text(HTML(
        f"    <style fg='{t['text_muted']}'>进度自动播报；/goal status 查看，/goal stop 或 Ctrl+C 停止。</style>"
    ))


def _run_goal_cli(args_text: str) -> None:
    from core.slash_commands.goal_command import GOAL_USAGE, handle_goal

    t = _get_theme(current_theme)
    result = handle_goal(args_text)
    if not result.get("ok"):
        print_formatted_text(HTML(
            f"  <style fg='{t['err']}'>❌ {_esc(result.get('msg') or '目标参数无效')}</style>"
        ))
        return
    action = result.get("action") or {}
    kind = str(action.get("action") or "")
    if kind == "goal_help":
        print_formatted_text(HTML(f"  <style fg='{t['accent']}'><b>◎ {_esc(GOAL_USAGE)}</b></style>"))
        print_formatted_text(HTML(
            f"    <style fg='{t['text_muted']}'>控制: /goal status | /goal stop | /goal resume | /goal confirm（自评达成后确认收尾）</style>"
        ))
        print_formatted_text(HTML(
            f"    <style fg='{t['text_muted']}'>示例: /goal 把测试跑绿 --check pytest -q --rounds 5</style>"
        ))
        return
    session_name = current_session
    if kind == "goal_status":
        _goal_handle_status(session_name)
    elif kind == "goal_stop":
        _goal_handle_stop(session_name)
    elif kind == "goal_resume":
        _goal_handle_resume(session_name)
    elif kind == "goal_confirm":
        _goal_handle_confirm(session_name)
    elif kind == "goal_start":
        _goal_handle_start(action, session_name)
    else:
        print_formatted_text(HTML(f"<ansired>❌ 未知目标动作: {_esc(kind)}</ansired>"))


def handle_slash_command(command_text):
    """斜杠指令路由器"""
    global current_session, current_model, current_theme, _turn_separator_needed

    parts = command_text.strip().split()
    cmd = parts[0].lower()

    if cmd == "/help":
        t = _get_theme(current_theme)
        a = t["accent"]
        s = t["accent_soft"]
        m = t["text_muted"]
        d = t["text_muted"]
        b = t["border"]
        agent = _esc(_agent_name())

        def _help_section(title: str) -> None:
            dash = "─" * max(4, 62 - _term_display_width(title))
            print_formatted_text(HTML(
                f"  <style fg='{a}'><b>❖ {_esc(title)}</b></style> <style fg='{t['border_dim']}'>{dash}</style>"
            ))

        def _help_row(command_label: str, desc: str) -> None:
            padded = _esc(_pad_right(command_label, 38))
            print_formatted_text(HTML(
                f"    <style fg='{s}'>{padded}</style> <style fg='{m}'>{desc}</style>"
            ))

        print_formatted_text(HTML(
            f"  <style fg='{b}'>╭─</style><style fg='{a}'><b> ✦ 星瑶Code CLI 系统指令与操作指南</b></style>"
            f" <style fg='{b}'>{'─' * 34}</style>"
        ))
        _help_section("1. 会话与上下文管理")
        _help_row("/session", "打开全屏会话管理器 UI（Enter 切换 / Del 删除 / R 改名 / A 归档 / Tab 归档区）")
        _help_row("/session <会话名称>", "按名称直接切换到指定会话（不存在则创建）")
        _help_row("/session all", "列出所有目录下的历史会话（含归属路径与时间）")
        _help_row("/session rename <新名称>", "重命名当前活动会话")
        _help_row("/session delete <会话名称>", "物理删除指定会话（安全切回防御）")
        _help_row("/session archive <会话名称>", "归档指定会话（从默认列表隐藏，数据保留可恢复）")
        _help_row("/session unarchive <会话名称>", "取消归档，恢复回默认会话列表")
        _help_row("/session archived", "列出全部已归档会话（含归档时间与归属路径）")
        _help_row("/new", "创建并切换到全新会话（历史会话保留，/session 可切回）")
        _help_row("/undo [turn_id]", "按回合撤销文件修改（list / status / recover，默认确认 N）")
        _help_row("/checkpoint", "会话级检查点管理（list 查看 / restore &lt;turn_id&gt; 整轮回滚）")
        _help_row("/share", "导出当前会话为 Markdown/HTML/JSON（默认 md，可加参数）")
        _help_row("/diagram [open 序号|list]", f"打开/列出已生成的 Mermaid 流程图网页（{agent}输出时自动生成）")
        _help_row("/worktree", "Git worktree 管理（list / create / remove / prune）")
        _help_row("/git <子命令>", "Git 工作台（status/diff/stage/commit/branch/switch/fetch/pull/push/stash/log）")
        _help_row("/clear", "清理终端当前屏幕")
        print_formatted_text(HTML(""))

        _help_section("2. 模型与样式配置")
        _help_row("/models", "打开全屏模型管理器 UI（切换模型、API 密钥、API 地址）")
        _help_row("/effort <档位>", "切换当前模型思考模式（无参查看可选档位；下轮请求生效）")
        _help_row("/persona [name|title|text|reset]", "人设设置（智能体名称/对你的称呼/人设文本，保存即全局生效）")
        _help_row("/shell [id]", "选择默认命令解释器（AI 命令/后台任务统一使用；无参查看列表）")
        _help_row("/open [path]", "在系统中打开文件/目录（无参=当前工作区）")
        _help_row("/clear-history [y]", "清空当前会话全部历史消息（不影响任务计划与长期记忆）")
        _help_row("/queue [list|cancel 序号|clear]", "管理排队中的消息（AI 忙碌时新消息会排队）")
        _help_row("/paste-image [说明]", "发送剪贴板图片给模型（可附说明文字）")
        _help_row("/update [check|download|cancel|install]", "检查/下载/安装新版本（安装会退出 星瑶Code）")
        _help_row("/changes [N]", "查看最近 N 个回合的文件变更汇总（默认 3）")
        _help_row("/theme", "列出当前所有可用的终端单主色主题")
        _help_row("/theme <主题名称>", "切换主题（cyan, matrix, nord, cyberpunk, dracula, lava, sunset, amber, white, starlight）")
        _help_row("/permission <confirm|accept_edits|auto_decision|plan|full>", "切换权限级别（confirm | accept_edits | auto_decision | plan | full）")
        _help_row("/turns <轮次>", "设置单任务最大模型轮次（10-1000，无参查看当前值）")
        _help_row("/sandbox <on|off>", "沙盒开关（on=目录外直接放行 | off=仅限工作目录内）")
        _help_row("/whitelist list|add|remove|clear", "管理当前项目白名单（command/write/edit）")
        _help_row("/usage", "查看 Token 用量统计（按模型区分：今日/近7天/近30天）")
        _help_row("/usage <日期> [截止日期]", "查询指定单日或区间的 Token 用量（例: /usage 2026-08-01 2026-08-05）")
        print_formatted_text(HTML(""))

        _help_section("3. 技能插件管理")
        _help_row("/skills", "打开全屏技能选择器 UI（空格勾选开启/关闭）")
        _help_row("/skills list", "在终端列出所有扫描到的技能，展示启用状态与短 ID")
        _help_row("/skills refresh", "重新扫描技能目录（新装技能免重启生效）")
        _help_row("/skills enable <短ID>", "启用指定短 ID 对应的技能")
        _help_row("/skills disable <短ID>", "禁用指定短 ID 对应的技能")
        _help_row("/<短ID> [提问内容]", f"<b>主动且强制调用指定技能</b>让{agent}为您执行任务")
        _help_row("/plugins", "查看已安装的进程内插件（名称/版本/来源/状态/工具）")
        _help_row("/plugins enable|disable <name>", "启用/禁用指定插件（写回 plugin.json，重启生效）")
        _help_row("/plugins reload", "重新扫描插件目录刷新清单（不重启）")
        _help_row("/hooks", "Hook 生命周期钩子管理（清单/trust/deny/revoke/reset）")
        _help_row("/hooks import", "<b>从 ZCode 配置导入 hook</b>（预览确认后写入；rollback 撤销）")
        _help_row("/mcp", "MCP 服务管理（open/status/tools/enable/disable/reconnect/edit）")
        print_formatted_text(HTML(""))

        _help_section("4. 任务托管与后台进程")
        _help_row("/tasks", "查看或接管目前正在运行的后台长周期服务/命令")
        _help_row("/tasks kill <任务ID>", "终结指定后台进程或子代理任务")
        _help_row("/tasks tail <任务ID>", "查看任务最新 30 行日志（子代理任务显示运行状态）")
        _help_row("/plan", "查看当前任务计划与逐项状态")
        _help_row("/plan <pause|resume|retry ID|cancel>", "暂停、恢复、重试或取消计划")
        _help_row("/doctor [--json]", "聚合自检（配置/凭据/数据库/MCP/网络；--json 机器可读）")
        _help_row("/doctor export [--full y]", "导出排障诊断包 zip（默认不含正文，--full 经确认后追加）")
        print_formatted_text(HTML(""))

        _help_section("5. KAIROS 长期记忆系统")
        _help_row("/remember <内容>", "手动追加一条规则、偏好或事实到今日的记忆日志")
        _help_row("/dream", "在后台触发梦境总结，将记忆日志提炼归纳并更新索引")
        _help_row("/memory", "快速查阅当前的长期记忆索引（MEMORY.md）内容")
        _help_row("/compress [force]", "手动触发当前会话的记忆压缩（force 强制保留更少条数）")
        print_formatted_text(HTML(""))

        _help_section("6. 终端常用快捷操作")
        _help_row("@文件名", "<b>在提问中挂载本地文件</b>（例如: 这行代码怎么改 @main.py）")
        _help_row("Enter", f"提交当前文本给{agent}（闲置立即执行，繁忙则自动排队等待）")
        _help_row("Ctrl+N", "在输入框内插入换行符（输入框随行数自动增高 1~5 行）")
        _help_row("Ctrl+C", f"<b>{agent}思考/执行中时强行中断</b>；闲置时清空输入框")
        _help_row("Ctrl+T", "<b>切换展开/收起底部背景记忆摘要浮窗</b>")
        _help_row("Shift+Tab", "<b>循环切换权限模式</b>（确认 → 自动编辑 → 自动决策 → 计划模式 → 完全访问）")
        _help_row("Ctrl+D / /exit", "安全退出 星瑶Code CLI")
        print_formatted_text(HTML(""))

        _help_section("7. 目标模式 (自主迭代)")
        _help_row("/goal <目标> [--check <命令>] [--rounds <N>]", "启动目标模式，自主迭代直到达成或轮次用尽（--rounds 默认 10）")
        _help_row("/goal status", "查看当前目标进度（第 N/M 轮 · 状态 · 最近一轮摘要）")
        _help_row("/goal stop", "停止当前目标（本轮中断，不再自动续跑；Ctrl+C 同效）")
        _help_row("/goal resume", "恢复已停止/受阻/中断的目标，从当前轮次继续跑")
        _help_row("/goal help", "查看 /goal 参数用法")
        print_formatted_text(HTML(f"  <style fg='{b}'>╰{'─' * 68}</style>"))
        print_formatted_text(HTML(f"    <style fg='{d}'>提示：输入 / 可触发命令自动补全</style>"))

    elif cmd in ("/models", "/model"):
        # 不在后台线程里直接开选择器（会和主线程的 event loop 冲突）
        # 而是设一个标志，然后退出主 App，让主线程安全地开启选择器
        global pending_action
        pending_action = "open_models"
        if app_instance:
            app_instance.exit()

    elif cmd in ("/skills", "/skill"):
        from core.skills import scan_all_skills, get_active_skill_ids, save_active_skill_ids, skill_source_label
        
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub == "refresh":
                skills = scan_all_skills()
                active_ids = set(get_active_skill_ids())
                active_count = sum(1 for s in skills if s["id"] in active_ids)
                print_formatted_text(HTML(f"<ansigreen>✔ 技能目录已重新扫描：共 {len(skills)} 个技能，已启用 {active_count} 个。</ansigreen>"))
                print_formatted_text(HTML("  输入 <ansicyan>/skills list</ansicyan> 查看清单，新增技能的 /补全 与 /短ID 调用立即生效。"))
            elif sub == "list":
                skills = scan_all_skills()
                active_ids = set(get_active_skill_ids())
                if not skills:
                    print_formatted_text(HTML("<ansiyellow>ℹ 没有扫描到任何技能。请在 .agents/skills 或 ~/.ycode/skills 中放置带有 SKILL.md 的技能包。</ansiyellow>"))
                else:
                    print_formatted_text(HTML("<b><ansicyan>可用技能插件列表：</ansicyan></b>"))
                    for s in skills:
                        short_id = os.path.basename(os.path.dirname(s["id"]))
                        is_active = s["id"] in active_ids
                        check = "<ansigreen>[✓]</ansigreen>" if is_active else "<ansigray>[ ]</ansigray>"
                        src_label = skill_source_label(s)
                        print_formatted_text(HTML(f"  {check} 短ID: <b><ansiyellow>{_esc(short_id)}</ansiyellow></b> | 名称: {_esc(s['name'])} - <ansigray>{_esc(s['description'])}</ansigray> <ansigray>| 来源: {_esc(src_label)}</ansigray>"))
            elif sub == "enable" and len(parts) > 2:
                target = parts[2].lower()
                skills = scan_all_skills()
                active_ids = list(get_active_skill_ids())
                matched = [s for s in skills if os.path.basename(os.path.dirname(s["id"])).lower() == target]
                if matched:
                    sid = matched[0]["id"]
                    if sid not in active_ids:
                        active_ids.append(sid)
                        save_active_skill_ids(active_ids)
                    print_formatted_text(HTML(f"<ansigreen>✔ 技能 <b>{_esc(target)}</b> 已成功启用！</ansigreen>"))
                else:
                    print_formatted_text(HTML(f"<ansired>❌ 找不到短 ID 为 {_esc(target)} 的技能，输入 `/skills list` 查看可用技能。</ansired>"))
            elif sub == "disable" and len(parts) > 2:
                target = parts[2].lower()
                skills = scan_all_skills()
                active_ids = list(get_active_skill_ids())
                matched = [s for s in skills if os.path.basename(os.path.dirname(s["id"])).lower() == target]
                if matched:
                    sid = matched[0]["id"]
                    if sid in active_ids:
                        active_ids.remove(sid)
                        save_active_skill_ids(active_ids)
                    print_formatted_text(HTML(f"<ansigreen>✔ 技能 <b>{_esc(target)}</b> 已禁用。</ansigreen>"))
                else:
                    print_formatted_text(HTML(f"<ansired>❌ 找不到短 ID 为 {_esc(target)} 的技能。</ansired>"))
            else:
                print_formatted_text(HTML("<ansired>❌ 用法错误。可选指令：</ansired>"))
                print_formatted_text(HTML("  <ansicyan>/skills list</ansicyan>             : 列出所有技能"))
                print_formatted_text(HTML("  <ansicyan>/skills refresh</ansicyan>          : 重新扫描技能目录"))
                print_formatted_text(HTML("  <ansicyan>/skills enable &lt;短ID&gt;</ansicyan>  : 启用指定技能"))
                print_formatted_text(HTML("  <ansicyan>/skills disable &lt;短ID&gt;</ansicyan> : 禁用指定技能"))
        else:
            pending_action = "open_skills"
            if app_instance:
                app_instance.exit()

    elif cmd == "/theme":
        from core.themes import THEMES

        def _print_theme_list() -> None:
            cur_t = _get_theme(current_theme)
            print_formatted_text(HTML(
                f"  <style fg='{cur_t['accent']}'><b>✦ 可用单主色主题方案</b></style> "
                f"<style fg='{cur_t['text_muted']}'>(当前: {_esc(current_theme)})</style>"
            ))
            for tname in THEMES.keys():
                desc, color = THEME_DESCS.get(tname, ("未知", "#ffffff"))
                active_mark = (
                    f"<style fg='{cur_t['accent']}'>●</style>"
                    if tname == current_theme
                    else f"<style fg='{cur_t['text_muted']}'>○</style>"
                )
                print_formatted_text(HTML(
                    f"    {active_mark} <style fg='{cur_t['text']}'><b>{tname:<10}</b></style> "
                    f"<style fg='{cur_t['text_soft']}'>({_esc(desc):<4})</style>  "
                    f"<style fg='{color}'>████</style> <style fg='{cur_t['text_muted']}'>{color}</style>"
                ))

        if len(parts) > 1:
            theme_name = parts[1].lower().strip("<>\"'")
            if theme_name == "list":
                _print_theme_list()
            elif theme_name in THEMES:
                from config import save_config_value
                save_config_value("theme", theme_name)
                current_theme = theme_name
                new_t = _get_theme(current_theme)
                print_formatted_text(HTML(
                    f"  <style fg='{new_t['ok']}'>✔ 已切换配色主题为: <b>{theme_name}</b></style>"
                ))
                if app_instance:
                    app_instance.invalidate()
            else:
                cur_t = _get_theme(current_theme)
                available = ", ".join(THEMES.keys())
                print_formatted_text(HTML(
                    f"  <style fg='{cur_t['err']}'>❌ 未知主题配色「{_esc(theme_name)}」，可选主题：{available}</style>"
                ))
        else:
            cur_t = _get_theme(current_theme)
            _print_theme_list()
            print_formatted_text(HTML(
                f"    <style fg='{cur_t['text_muted']}'>用法: </style>"
                f"<style fg='{cur_t['accent_soft']}'>/theme &lt;主题名称&gt;</style>"
            ))

    elif cmd == "/permission":
        if len(parts) > 1 and parts[1] != "list":
            mode_name = parts[1].lower().strip("<>\"'")
            # 与 GUI 的 set_permission_mode 一致：接受已登记别名（yolo/中文标签等），
            # 拼错的值明确报错，绝不静默回落成 confirm 后当作切换成功
            if _app_ctx.is_known_permission_mode(mode_name):
                from core.permission_auto_decision import (
                    get_auto_decision_readiness_warning,
                    is_decision_model_enabled,
                )

                target_mode = _app_ctx.normalize_permission_mode(mode_name)
                if target_mode == "auto_decision" and not is_decision_model_enabled():
                    print_formatted_text(HTML(
                        "<ansired>❌ 决策模型（JEV）尚未启用，无法切换到自动决策模式；请先启用决策模型。</ansired>"
                    ))
                else:
                    from config import save_config_value
                    global current_permission_mode
                    current_permission_mode = target_mode
                    save_config_value("permission_mode", current_permission_mode)
                    mapping = {
                        "confirm": "变更前确认",
                        "accept_edits": "自动编辑",
                        "auto_decision": "自动决策",
                        "plan": "计划模式",
                        "full": "完全访问",
                    }
                    print_formatted_text(HTML(f"<ansigreen>✔ 权限控制模式已切换为: <b>{mapping[current_permission_mode]}</b></ansigreen>"))
                    if current_permission_mode == "auto_decision":
                        warn_msg = get_auto_decision_readiness_warning()
                        if warn_msg:
                            print_formatted_text(HTML(f"<ansiyellow>⚠ {_esc(warn_msg)}</ansiyellow>"))
                    if app_instance:
                        app_instance.invalidate()
            else:
                from core.permission_auto_decision import is_decision_model_enabled

                opts = "confirm, accept_edits, auto_decision, plan, full" if is_decision_model_enabled() else "confirm, accept_edits, plan, full"
                print_formatted_text(HTML(f"<ansired>❌ 未知权限模式「{_esc(mode_name)}」，可选模式：{opts}</ansired>"))
        else:
            from core.permission_auto_decision import is_decision_model_enabled

            jev_enabled = is_decision_model_enabled()
            print_formatted_text(HTML("<b><ansicyan>可用权限模式：</ansicyan></b>"))
            for line in PERMISSION_HELP_LINES:
                if not jev_enabled and "<b>auto_decision</b>" in line:
                    continue
                print_formatted_text(HTML(line))
            usage_modes = "confirm|accept_edits|auto_decision|plan|full" if jev_enabled else "confirm|accept_edits|plan|full"
            print_formatted_text(HTML(f"用法: <ansiyellow>/permission &lt;{usage_modes}&gt;</ansiyellow>"))

    elif cmd == "/turns":
        from core.harness.turn_limits import (
            MAX_TURN_STEP_LIMIT,
            MIN_TURN_STEP_LIMIT,
            TurnStepLimitError,
            load_turn_step_limit,
            save_turn_step_limit,
        )
        if len(parts) > 1:
            raw = parts[1].strip("<>\"'")
            try:
                value = int(raw)
            except ValueError:
                print_formatted_text(HTML(f"<ansired>❌ 轮次必须是整数（{MIN_TURN_STEP_LIMIT}–{MAX_TURN_STEP_LIMIT}），收到「{_esc(raw)}」。</ansired>"))
            else:
                try:
                    resolved = save_turn_step_limit(value)
                except TurnStepLimitError as error:
                    print_formatted_text(HTML(f"<ansired>❌ {_esc(str(error))}</ansired>"))
                else:
                    print_formatted_text(HTML(f"<ansigreen>✔ 单任务最大模型轮次已更新为: <b>{resolved}</b>（仅影响新任务）</ansigreen>"))
        else:
            current = load_turn_step_limit()
            print_formatted_text(HTML(f"当前单任务最大模型轮次: <b>{current}</b>（可选范围 {MIN_TURN_STEP_LIMIT}–{MAX_TURN_STEP_LIMIT}）"))
            print_formatted_text(HTML("用法: <ansiyellow>/turns &lt;轮次&gt;</ansiyellow>"))

    elif cmd == "/effort":
        from core.model_manager import set_model_reasoning_choice
        active_id, model, snapshot = None, None, None
        snapshot_error = ""
        try:
            active_id, model, snapshot = _active_reasoning_snapshot()
        except ValueError as exc:
            snapshot_error = str(exc)
        if not active_id:
            print_formatted_text(HTML("<ansired>❌ 当前没有激活模型，请先用 /models 配置模型。</ansired>"))
        elif len(parts) > 1:
            level = parts[1].lower().strip("<>\"'")
            ok, message = set_model_reasoning_choice(active_id, level)
            if ok:
                print_formatted_text(HTML(f"<ansigreen>✔ {_esc(message)}（下一轮请求生效）</ansigreen>"))
                litellm_model = str((model or {}).get("litellm_model") or "")
                if litellm_model.split("/")[0].lower() == "anthropic":
                    print_formatted_text(HTML("<ansiyellow>ℹ 当前模型走 Anthropic 路由：思考档位会按模型协议自动转换（Kimi K3 → thinking budget，阶跃 → output_config.effort）。</ansiyellow>"))
                if app_instance:
                    app_instance.invalidate()
            else:
                print_formatted_text(HTML(f"<ansired>❌ {_esc(message)}</ansired>"))
        elif snapshot_error:
            print_formatted_text(HTML(f"<ansired>❌ {_esc(snapshot_error)}</ansired>"))
        elif model is None:
            print_formatted_text(HTML(f"<ansired>❌ 未在配置中找到激活模型（id: {_esc(str(active_id))}），请用 /models 重新选择。</ansired>"))
        elif not snapshot["visible"]:
            print_formatted_text(HTML("<ansiyellow>ℹ 当前模型未声明思考模式调节能力。</ansiyellow>"))
        elif snapshot["disabled"]:
            reason = snapshot.get("disabled_reason") or "该模型始终开启思考"
            print_formatted_text(HTML(f"<ansiyellow>ℹ 当前模型始终开启思考（{_esc(reason)}），无需也无法切换档位。</ansiyellow>"))
        else:
            current = snapshot["choice"]
            current_label = next((o["label"] for o in snapshot["options"] if o["value"] == current), current)
            marks = "  ".join(
                f"{'●' if o['value'] == current else '○'} {_esc(o['value'])}({_esc(o['label'])})"
                for o in snapshot["options"]
            )
            print_formatted_text(HTML(f"<b><ansicyan>当前模型思考模式: {_esc(current_label)} ({_esc(current)})</ansicyan></b>"))
            print_formatted_text(HTML(f"  可选档位: {marks}"))
            print_formatted_text(HTML("用法: <ansiyellow>/effort &lt;档位&gt;</ansiyellow>  (default = 恢复模型默认，下一轮请求生效)"))

    elif cmd == "/clear-history":
        from core.llm import session_token_usage, token_usage_lock
        confirm = parts[1].lower() if len(parts) > 1 else ""
        if confirm not in ("y", "yes", "--force"):
            print_formatted_text(HTML(f"<ansiyellow>⚠️ 即将清空当前会话 <b>{_esc(current_session)}</b> 的全部历史消息，清空后无法恢复。</ansiyellow>"))
            print_formatted_text(HTML("确认请输入: <ansicyan>/clear-history y</ansicyan>"))
        else:
            try:
                deleted = manager.clear_session_messages(current_session)
            except Exception as exc:
                print_formatted_text(HTML(f"<ansired>❌ 清空失败：{_esc(str(exc))}</ansired>"))
            else:
                with token_usage_lock:
                    session_token_usage["latest_total"] = 0
                    session_token_usage["latest_prompt"] = 0
                    session_token_usage["latest_cached"] = 0
                print_formatted_text(HTML(f"<ansigreen>✔ 已清空 {deleted} 条历史记录，上下文用量已重置（下一轮请求生效）。</ansigreen>"))
                if app_instance:
                    app_instance.invalidate()

    elif cmd == "/persona":
        from core.persona_settings import DEFAULT_AGENT_NAME, get_persona_settings, save_persona_settings, PersonaSettingsError
        try:
            persona_snapshot = get_persona_settings()
        except Exception:
            persona_snapshot = {"agent_name": DEFAULT_AGENT_NAME, "persona": "", "user_title": ""}
        if len(parts) > 1:
            sub = parts[1].lower()
            # 值取原始文本剩余部分（保留大小写/空格/换行），避免 split 丢格式
            value_match = re.match(r"^/persona\s+\S+\s?(.*)$", command_text, re.S)
            value = (value_match.group(1) if value_match else "").strip()
            try:
                if sub == "name":
                    if not value:
                        print_formatted_text(HTML("<ansired>❌ 用法: /persona name &lt;新名称&gt;</ansired>"))
                    else:
                        save_persona_settings(value, persona_snapshot.get("persona", ""), persona_snapshot.get("user_title", ""))
                        print_formatted_text(HTML(f"<ansigreen>✔ 智能体名称已改为 {_esc(value)}（下一轮请求生效）。</ansigreen>"))
                elif sub == "title":
                    if not value:
                        print_formatted_text(HTML("<ansired>❌ 用法: /persona title &lt;对你的称呼&gt; （/persona title - 表示清除称呼）</ansired>"))
                    else:
                        new_title = "" if value == "-" else value
                        save_persona_settings(persona_snapshot.get("agent_name", DEFAULT_AGENT_NAME), persona_snapshot.get("persona", ""), new_title)
                        print_formatted_text(HTML("<ansigreen>✔ 称呼已更新（下一轮请求生效）。</ansigreen>"))
                elif sub == "text":
                    if not value:
                        print_formatted_text(HTML("<ansired>❌ 用法: /persona text &lt;人设全文&gt;</ansired>"))
                    else:
                        save_persona_settings(persona_snapshot.get("agent_name", DEFAULT_AGENT_NAME), value, persona_snapshot.get("user_title", ""))
                        print_formatted_text(HTML(f"<ansigreen>✔ 人设文本已保存（共 {len(value)} 字，下一轮请求生效，取代内置人设但保留安全规范）。</ansigreen>"))
                elif sub == "reset":
                    save_persona_settings(DEFAULT_AGENT_NAME, "", "")
                    print_formatted_text(HTML("<ansigreen>✔ 人设已恢复默认。</ansigreen>"))
                else:
                    print_formatted_text(HTML("<ansired>❌ 未知子命令。用法: /persona [name|title|text|reset] [值]</ansired>"))
            except PersonaSettingsError as exc:
                print_formatted_text(HTML(f"<ansired>❌ {_esc(str(exc))}</ansired>"))
        else:
            persona_text = str(persona_snapshot.get("persona") or "")
            if len(persona_text) > 400:
                persona_display = persona_text[:200] + "\n    ……（中间省略）……\n    " + persona_text[-200:]
            else:
                persona_display = persona_text or "（未设置，使用内置人设）"
            print_formatted_text(HTML("<b><ansicyan>当前人设设置：</ansicyan></b>"))
            print_formatted_text(HTML(f"  智能体名称: {_esc(persona_snapshot.get('agent_name') or DEFAULT_AGENT_NAME)}"))
            print_formatted_text(HTML(f"  对你的称呼: {_esc(persona_snapshot.get('user_title') or '（不使用特定称呼）')}"))
            print_formatted_text(HTML(f"  人设文本（{len(persona_text)} 字）:\n    {_esc(persona_display)}"))
            print_formatted_text(HTML("用法: <ansicyan>/persona name|title|text|reset</ansicyan>  例: /persona text &lt;人设全文&gt;"))

    elif cmd == "/shell":
        from core.command_shell import CommandShellError, list_command_shell_options, set_selected_command_shell
        options = list_command_shell_options()
        if len(parts) > 1:
            shell_id = parts[1].strip()
            matched = next((o for o in options if o["id"] == shell_id), None)
            if matched is None:
                allowed = _esc("/".join(o["id"] for o in options))
                print_formatted_text(HTML(f"<ansired>❌ 不支持的命令解释器: {_esc(shell_id)}（可选 {allowed}）</ansired>"))
            elif not matched["available"]:
                print_formatted_text(HTML(f"<ansired>❌ {_esc(matched['label'])} 未安装或不在 星瑶Code 的 PATH 中，无法选择。</ansired>"))
            else:
                try:
                    set_selected_command_shell(shell_id)
                except CommandShellError as exc:
                    print_formatted_text(HTML(f"<ansired>❌ {_esc(str(exc))}</ansired>"))
                else:
                    print_formatted_text(HTML(f"<ansigreen>✔ 默认命令解释器已切换为 {_esc(matched['label'])}（{_esc(matched['id'])}），下一轮请求生效。</ansigreen>"))
        else:
            from core.command_shell import describe_selected_command_shell, get_selected_command_shell
            current_shell_id = get_selected_command_shell()
            print_formatted_text(HTML("<b><ansicyan>可选命令解释器：</ansicyan></b>"))
            for o in options:
                mark = "●" if o["available"] else "○"
                current_mark = "<ansigreen>（当前）</ansigreen>" if o["id"] == current_shell_id else ""
                reason = f" <ansigray>[{_esc(o['unavailable_reason'])}]</ansigray>" if not o["available"] else ""
                print_formatted_text(HTML(f"  {mark} {_esc(o['id']):<12} {_esc(o['label'])} <ansigray>- {_esc(o['description'])}</ansigray>{reason} {current_mark}"))
            print_formatted_text(HTML("用法: <ansicyan>/shell &lt;id&gt;</ansicyan>"))

    elif cmd == "/open":
        raw = " ".join(parts[1:]).strip().strip('"').strip("'") if len(parts) > 1 else "."
        from core.workspace_context import get_workdir
        base_dir = get_workdir() or os.getcwd()
        target_path = raw if os.path.isabs(raw) else os.path.join(base_dir, raw)
        target_path = os.path.abspath(target_path)
        if not os.path.exists(target_path):
            print_formatted_text(HTML(f"<ansired>❌ 路径不存在: {_esc(target_path)}</ansired>"))
        elif not hasattr(os, "startfile"):
            print_formatted_text(HTML("<ansiyellow>ℹ 当前平台不支持在系统中打开（仅支持 Windows）。</ansiyellow>"))
        else:
            try:
                os.startfile(target_path)  # noqa: S606 - 用户显式请求打开本地路径
            except OSError as exc:
                print_formatted_text(HTML(f"<ansired>❌ 打开失败：{_esc(str(exc))}</ansired>"))

    elif cmd == "/queue":
        # 排队消息管理（对齐 GUI 排队条的 list/cancel/clear；插队依赖完整取消链路，
        # 按实施计划列为后续）。queue.Queue 不可遍历：UI 线程内 get_nowait 全量取出
        # → 处理 → 按原序回填。put 方有两个（代码审查 Important #3 修正）：
        # Enter 处理器的用户消息 + _deferred_summary_check 守护线程的
        # __ycode_internal__ 自动汇总指令（is_internal: True）——后者不参与序号
        # 编排与 cancel/clear，回填时原位保留，避免后台任务汇总被静默丢弃。
        queued = []
        while True:
            try:
                queued.append(msg_queue.get_nowait())
            except queue.Empty:
                break
        user_queued = [item for item in queued if not item.get("is_internal")]
        sub = parts[1].lower() if len(parts) > 1 else "list"
        if sub == "list":
            for item in queued:
                msg_queue.put(item)
            if not user_queued:
                print_formatted_text(HTML("<ansiyellow>ℹ 当前没有排队中的消息。</ansiyellow>"))
            else:
                print_formatted_text(HTML(f"<b><ansicyan>排队中的消息（{len(user_queued)} 条）：</ansicyan></b>"))
                for idx, item in enumerate(user_queued, 1):
                    preview = str(item.get("text", "")).replace("\n", " ")[:60]
                    kind = "技能" if item.get("is_skill_call") else "消息"
                    session_mark = str(item.get("session") or "?")
                    print_formatted_text(HTML(f"  {idx}. [{kind} → {_esc(session_mark)}] {_esc(preview)}"))
        elif sub == "cancel" and len(parts) > 2:
            try:
                target_idx = int(parts[2])
            except ValueError:
                target_idx = 0
            if not user_queued:
                for item in queued:
                    msg_queue.put(item)
                print_formatted_text(HTML("<ansiyellow>ℹ 当前没有排队中的消息。</ansiyellow>"))
            elif 1 <= target_idx <= len(user_queued):
                kept = []
                user_seen = 0
                removed = None
                for item in queued:
                    if not item.get("is_internal"):
                        user_seen += 1
                        if user_seen == target_idx:
                            removed = item
                            continue
                    kept.append(item)
                for item in kept:
                    msg_queue.put(item)
                preview = str(removed.get("text", "")).replace("\n", " ")[:60]
                print_formatted_text(HTML(f"<ansigreen>✔ 已取消排队消息 {target_idx}：{_esc(preview)}</ansigreen>"))
            else:
                for item in queued:
                    msg_queue.put(item)
                print_formatted_text(HTML(f"<ansired>❌ 序号超出范围（当前共 {len(user_queued)} 条），用 /queue list 查看。</ansired>"))
        elif sub == "clear":
            for item in queued:
                if item.get("is_internal"):
                    msg_queue.put(item)
            note = f"（另有 {len(queued) - len(user_queued)} 条系统内部消息已保留）" if len(queued) > len(user_queued) else ""
            print_formatted_text(HTML(f"<ansigreen>✔ 已清空 {len(user_queued)} 条排队消息。{note}</ansigreen>"))
        else:
            for item in queued:
                msg_queue.put(item)
            print_formatted_text(HTML("<ansired>❌ 用法: /queue [list|cancel &lt;序号&gt;|clear]</ansired>"))

    elif cmd == "/paste-image":
        note = command_text[len("/paste-image"):].strip() or "（剪贴板图片）"
        explicit_paths = []
        try:
            from PIL import ImageGrab
        except Exception:
            print_formatted_text(HTML("<ansired>❌ 剪贴板图片需要 Pillow 支持，请检查依赖安装。</ansired>"))
        else:
            try:
                grabbed = ImageGrab.grabclipboard()
            except Exception as exc:
                print_formatted_text(HTML(f"<ansired>❌ 读取剪贴板失败：{_esc(str(exc))}</ansired>"))
            else:
                import tempfile
                if isinstance(grabbed, list) and grabbed:
                    explicit_paths = [str(p) for p in grabbed if str(p).strip()]
                elif grabbed is not None:
                    try:
                        fd, tmp_path = tempfile.mkstemp(prefix="ycode_clip_", suffix=".png")
                        os.close(fd)
                        grabbed.save(tmp_path, "PNG")
                    except Exception as exc:
                        print_formatted_text(HTML(f"<ansired>❌ 剪贴板图片保存失败：{_esc(str(exc))}</ansired>"))
                    else:
                        explicit_paths = [tmp_path]
                else:
                    print_formatted_text(HTML("<ansiyellow>ℹ 剪贴板中没有图片。</ansiyellow>"))
        if explicit_paths:
            was_queued = is_thinking
            _echo_user_message(note, queued=was_queued, prefix_tag="[剪贴板图片]")
            msg_queue.put({
                "text": note,
                "was_queued": was_queued,
                "session": current_session,
                "explicit_paths": explicit_paths,
            })

    elif cmd == "/update":
        # 更新检查与安装（core/updater 此前仅 GUI 接入）；
        # 下载回调经 _print_threadsafe 调度回主线程，遵守线程纪律
        sub = parts[1].lower() if len(parts) > 1 else "check"
        if sub == "check":
            _update_cmd_check()
        elif sub == "download":
            _update_cmd_download()
        elif sub == "cancel":
            _get_update_downloader().cancel()
            print_formatted_text(HTML("<ansigreen>✔ 已请求取消下载。</ansigreen>"))
        elif sub == "install":
            rest_match = re.match(r"^/update\s+install\s+(.*)$", command_text, re.S)
            rest = rest_match.group(1).strip() if rest_match else ""
            _update_cmd_install(rest.split())
        else:
            print_formatted_text(HTML("<ansired>❌ 用法: /update [check|download|cancel|install [路径] [y]]</ansired>"))

    elif cmd == "/changes":
        # 会话文件变更历史回看（数据源：turn_undo 检查点，只读消费）。
        # 与每轮结束后的自动摘要互补：/changes 面向任意时刻的跨回合回看。
        t = _get_theme(current_theme)
        try:
            limit_n = int(parts[1]) if len(parts) > 1 else 3
        except ValueError:
            limit_n = 3
        limit_n = max(1, min(limit_n, 50))
        tu = _undo_service()
        session_id = _undo_session_id(current_session)
        try:
            states = tu.list_turn_undo_states(session_id, limit=50)
        except Exception as exc:
            print_formatted_text(HTML(f"<style fg='{t['err']}'>❌ 读取变更记录失败：{_esc(str(exc))}</style>"))
            return
        with_changes = [s for s in states if (s.get("file_count") or 0) > 0]
        if not with_changes:
            print_formatted_text(HTML(f"<style fg='{t['warn']}'>ℹ 当前会话还没有文件修改记录。</style>"))
            return
        shown_turns = with_changes[:limit_n]
        print_formatted_text(HTML(
            f"  <style fg='{t['border']}'>╭─</style>"
            f"<style fg='{t['accent']}'><b> ✎ 最近文件变更（{len(shown_turns)} 个回合）</b></style> "
            f"<style fg='{t['border']}'>{'─' * 34}</style>"
        ))
        for s in shown_turns:
            short_tid = _esc(_undo_short_id(s['turn_id']))
            print_formatted_text(HTML(
                f"  <style fg='{t['border']}'>│</style> 回合 <style fg='{t['accent_soft']}'><b>{short_tid}</b></style> "
                f"<style fg='{t['text_muted']}'>· {_esc(_undo_status_label(s))} · {s.get('file_count') or 0} 文件</style> "
                f"<style fg='{t['ok']}'>+{s.get('net_added') or 0}</style> "
                f"<style fg='{t['err']}'>-{s.get('net_removed') or 0}</style>"
            ))
            try:
                detail = tu.get_turn_undo_status(session_id, s["turn_id"])
            except Exception:
                detail = None
            files = (detail or {}).get("files") or []
            for f in files[:8]:
                fa = int(f.get("net_added") or 0)
                fr = int(f.get("net_removed") or 0)
                bar = _diff_stat_html(fa, fr, width=5)
                bar_suffix = f"  {bar}" if bar else ""
                print_formatted_text(HTML(
                    f"  <style fg='{t['border']}'>│</style>   <style fg='{t['text']}'>{_esc(str(f.get('display_path') or ''))}</style> "
                    f"<style fg='{t['ok']}'>+{fa}</style> "
                    f"<style fg='{t['err']}'>-{fr}</style>"
                    f"{bar_suffix}"
                ))
            if len(files) > 8:
                print_formatted_text(HTML(
                    f"  <style fg='{t['border']}'>│</style>   <style fg='{t['text_muted']}'>… 还有 {len(files) - 8} 个文件（/undo status {short_tid} 查看全部）</style>"
                ))
        print_formatted_text(HTML(
            f"  <style fg='{t['border']}'>╰─</style> "
            f"<style fg='{t['text_muted']}'>回滚某轮：/undo &lt;短ID&gt; 或 /checkpoint restore &lt;turn_id&gt;</style>"
        ))

    elif cmd == "/sandbox":
        from core.model_manager import load_config
        if len(parts) > 1:
            mode_name = parts[1].lower().strip("<>\"'")
            if mode_name in ("on", "off"):
                from config import save_config_value
                enabled = (mode_name == "on")
                save_config_value("allow_outside_workspace", enabled)
                if enabled:
                    print_formatted_text(HTML("<ansigreen>✔ 沙盒限制已解除：允许访问工作目录以外的文件</ansigreen>"))
                else:
                    print_formatted_text(HTML("<ansigreen>✔ 沙盒限制已开启：只允许访问当前工作目录</ansigreen>"))
            else:
                print_formatted_text(HTML(f"<ansired>❌ 未知参数「{_esc(mode_name)}」，可选：on, off</ansired>"))
        else:
            enabled = bool(load_config().get("allow_outside_workspace", False))
            status = "已开放（工作目录外读写直接放行，不再询问）" if enabled else "已开启（仅限工作目录内）"
            print_formatted_text(HTML(f"<b><ansicyan>沙盒状态：{status}</ansicyan></b>"))
            print_formatted_text(HTML("用法: <ansiyellow>/sandbox &lt;on|off&gt;</ansiyellow>（on=工作目录外直接放行，off=仅限工作目录内）"))

    elif cmd == "/whitelist":
        from core import permission_grants, permission_store
        from core.llm import _format_session_command_rule
        from core.tools import is_session_sandbox_bypass_enabled

        whitelist_parts = command_text.strip().split(maxsplit=3)
        action = whitelist_parts[1].lower() if len(whitelist_parts) > 1 else "list"
        if action in {"add", "remove"}:
            if len(whitelist_parts) != 4:
                print_formatted_text(HTML(
                    "<ansired>❌ 用法：/whitelist add|remove command|write|edit &lt;命令或文件路径&gt;</ansired>"
                ))
            else:
                rule_type = whitelist_parts[2].lower()
                value = whitelist_parts[3]
                try:
                    operation = (
                        permission_store.add_rule
                        if action == "add"
                        else permission_store.remove_rule
                    )
                    result = operation(rule_type, value)
                except (ValueError, permission_store.PermissionStoreError) as exc:
                    print_formatted_text(HTML(f"<ansired>❌ {_esc(exc)}</ansired>"))
                else:
                    labels = {
                        "added": "已新增",
                        "exists": "已存在",
                        "removed": "已删除",
                        "not_found": "未找到",
                        "limit_reached": "已达到规则数量上限",
                    }
                    label = labels.get(result.status, result.status)
                    if result.status in {"limit_reached", "not_found"}:
                        print_formatted_text(HTML(
                            f"<ansired>❌ {label}：{_esc(result.normalized_value)}</ansired>"
                        ))
                    else:
                        color = "ansiyellow" if result.status == "exists" else "ansigreen"
                        print_formatted_text(HTML(
                            f"<{color}>✔ {label} {rule_type} 规则：{_esc(result.normalized_value)}</{color}>"
                        ))
        elif action == "clear":
            confirmed = (
                len(whitelist_parts) >= 3
                and whitelist_parts[2].lower() == "confirm"
            )
            if not confirmed:
                print_formatted_text(HTML(
                    "<ansiyellow>⚠ 清空会删除当前项目的全部持久化规则；确认执行请使用 /whitelist clear confirm</ansiyellow>"
                ))
            else:
                removed = permission_store.clear_project()
                print_formatted_text(HTML(f"<ansigreen>✔ 已清空当前项目的持久化白名单（共 {removed} 条）。任务级白名单与沙盒任务放行不受影响。</ansigreen>"))
        elif action != "list":
            print_formatted_text(HTML(
                "<ansired>❌ 用法：/whitelist list|add|remove|clear</ansired>"
            ))
        else:
            print_formatted_text(HTML(f"<b><ansicyan>当前任务白名单（切换界面不清除，删除任务或重启后失效）：</ansicyan></b>"))
            task_rules = permission_grants.snapshot()
            session_cmds = sorted(task_rules["commands"])
            session_writes = sorted(task_rules["write_files"])
            session_edits = sorted(task_rules["edit_files"])
            if session_cmds:
                for rule in session_cmds:
                    print_formatted_text(HTML(f"  命令: {_esc(_format_session_command_rule(rule))}"))
            else:
                print_formatted_text(HTML("  （无命令规则）"))
            if session_writes:
                for path in session_writes:
                    print_formatted_text(HTML(f"  写入: {_esc(path)}"))
            if session_edits:
                for path in session_edits:
                    print_formatted_text(HTML(f"  修改: {_esc(path)}"))
            sandbox_state = "已放行" if is_session_sandbox_bypass_enabled() else "未放行"
            print_formatted_text(HTML(f"  沙盒外访问（本任务）: {sandbox_state}"))

            print_formatted_text(HTML(f"<b><ansicyan>当前项目持久化白名单（重启保留，/whitelist clear confirm 清空）：</ansicyan></b>"))
            rules = permission_store.project_rules()
            if rules["commands"]:
                for rule in sorted(rules["commands"]):
                    print_formatted_text(HTML(f"  命令: {_esc(_format_session_command_rule(rule))}"))
            if rules["write_files"]:
                for path in sorted(rules["write_files"]):
                    print_formatted_text(HTML(f"  写入: {_esc(path)}"))
            if rules["edit_files"]:
                for path in sorted(rules["edit_files"]):
                    print_formatted_text(HTML(f"  修改: {_esc(path)}"))
            if not (rules["commands"] or rules["write_files"] or rules["edit_files"]):
                print_formatted_text(HTML("  （无持久化规则）"))

    elif cmd == "/new":
        new_session = f"session_{uuid.uuid4().hex[:6]}"
        manager.create_session_explicit(new_session)
        current_session = new_session
        sync_plan_session(current_session)
        recalc_token_usage()
        safe_session = new_session.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        print_formatted_text(HTML(f"\n<ansigreen>✔ 已创建并切换到全新会话: {safe_session}</ansigreen>"))
        print_formatted_text(HTML("<ansigray>  历史会话仍保留，输入 /session 可随时切回。</ansigray>"))
        print_logo(clear_screen=False)

    elif cmd in ("/sessions", "/session"):
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub == "delete" and len(parts) > 2:
                # 物理删除会话（先确认目标存在，避免误报删除成功）
                target = parts[2].strip("<>")
                safe_target = _esc(target)
                existing_names = {s["name"] for s in manager.get_all_sessions()}
                if target not in existing_names:
                    print_formatted_text(HTML(f"<ansired>❌ 会话 {safe_target} 不存在，未执行删除。</ansired>"))
                    return
                manager.delete_session(target)
                from core import permission_grants
                from core.tools import clear_session_sandbox_bypass

                permission_grants.clear_task(target)
                clear_session_sandbox_bypass(target)
                print_formatted_text(HTML(f"<ansigreen>✔ 会话 {safe_target} 已被成功删除。</ansigreen>"))

                if target == current_session:
                    sessions = manager.get_all_sessions()
                    if sessions:
                        current_session = sessions[0]["name"]
                    else:
                        current_session = f"session_{uuid.uuid4().hex[:6]}"
                    sync_plan_session(current_session)
                    recalc_token_usage()
                    print_formatted_text(HTML(f"<ansiyellow>⚠️ 已删除当前活动会话，系统自动切换至：{_esc(current_session)}</ansiyellow>"))
                    print_logo(clear_screen=False)
                    show_session_history(current_session)
            elif sub == "all":
                all_sessions = manager.get_all_sessions()
                if not all_sessions:
                    print_formatted_text(HTML("<ansiyellow>(没有任何历史会话)</ansiyellow>"))
                else:
                    print_formatted_text(HTML("<b><ansicyan>全部历史会话（按最近创建排序）：</ansicyan></b>"))
                    for s in all_sessions:
                        time_str = (s.get("updated_at") or "-")[:16]
                        workdir = s.get("workdir") or "(历史遗留/全局)"
                        print_formatted_text(HTML(
                            f"  - <b>{_esc(s['name'])}</b>  [{_esc(time_str)}]  ({s['count']} 条消息)  <ansigray>{_esc(workdir)}</ansigray>"
                        ))
            elif sub == "rename" and len(parts) > 2:
                new_name = " ".join(parts[2:]).strip('"\'<>')
                if new_name:
                    success, final_name = manager.rename_session(current_session, new_name)
                    if success:
                        current_session = final_name
                        sync_plan_session(current_session)
                        recalc_token_usage()
                        safe_final = final_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        print_formatted_text(HTML(f"<ansigreen>✔ 会话已成功重命名为: {safe_final}</ansigreen>"))
                        print_logo(clear_screen=False)
                    else:
                        print_formatted_text(HTML("<ansired>❌ 会话重命名失败。</ansired>"))
                else:
                    print_formatted_text(HTML("<ansired>❌ 用法错误。请输入：/session rename &lt;新名称&gt;</ansired>"))
            elif sub == "archive" and len(parts) > 2:
                target = parts[2].strip("<>")
                safe_target = _esc(target)
                if target == current_session:
                    print_formatted_text(HTML("<ansired>❌ 不能归档当前活跃会话，请先切换到其它会话。</ansired>"))
                    return
                if not manager.archive_session(target):
                    print_formatted_text(HTML(f"<ansired>❌ 会话 {safe_target} 不存在，未执行归档。</ansired>"))
                    return
                print_formatted_text(HTML(
                    f"<ansigreen>✔ 会话 {safe_target} 已归档（数据保留，可用 /session unarchive {safe_target} 恢复）。</ansigreen>"
                ))
            elif sub == "unarchive" and len(parts) > 2:
                # 取消归档：把会话恢复回默认列表（幂等，未归档会话调用同样安全）
                target = parts[2].strip("<>")
                safe_target = _esc(target)
                if not manager.unarchive_session(target):
                    print_formatted_text(HTML(f"<ansired>❌ 会话 {safe_target} 不存在，未执行取消归档。</ansired>"))
                    return
                print_formatted_text(HTML(f"<ansigreen>✔ 会话 {safe_target} 已取消归档，已恢复回会话列表。</ansigreen>"))
            elif sub in ("archive", "unarchive"):
                print_formatted_text(HTML(f"<ansired>❌ 用法错误。请输入：/session {sub} &lt;会话名称&gt;</ansired>"))
            elif sub == "archived":
                archived_sessions = manager.get_all_sessions(archived_only=True)
                if not archived_sessions:
                    print_formatted_text(HTML("<ansiyellow>(没有任何已归档会话)</ansiyellow>"))
                else:
                    print_formatted_text(HTML("<b><ansicyan>已归档会话（按归档时间排序）：</ansicyan></b>"))
                    for s in archived_sessions:
                        time_str = (s.get("updated_at") or "-")[:16]
                        archived_at = (s.get("archived_at") or "-")[:16]
                        workdir = s.get("workdir") or "(历史遗留/全局)"
                        print_formatted_text(HTML(
                            f"  - <b>{_esc(s['name'])}</b>  [{_esc(time_str)}]  ({s['count']} 条消息)  <ansigray>{_esc(workdir)}</ansigray>  <ansigray>归档于 {_esc(archived_at)}</ansigray>"
                        ))
            else:
                # 向后兼容：默认会话切换 /session <会话名>
                target = parts[1].strip("<>")
                # 不变量：切换到归档会话时自动取消归档，避免「会话处于归档态却正在被使用」
                if target in {s["name"] for s in manager.get_all_sessions(archived_only=True)}:
                    manager.unarchive_session(target)
                    print_formatted_text(HTML(f"<ansiyellow>✔ 会话 {_esc(target)} 已自动取消归档。</ansiyellow>"))
                manager.create_session_explicit(target)
                current_session = target
                sync_plan_session(current_session)
                recalc_token_usage()
                safe_session = current_session.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"\n<ansigreen>Switching to session {safe_session}...</ansigreen>"))
                print_logo(clear_screen=False)
                show_session_history(current_session)
        else:
            pending_action = "open_sessions"
            if app_instance:
                app_instance.exit()

    elif cmd in ("/tasks", "/task"):
        from core.task_manager import (
            list_active_tasks, get_task_output, kill_background_task, turn_short_code,
        )
        from core.tools import subagent_manager

        t_theme = _get_theme(current_theme)
        if len(parts) > 1:
            subcmd = parts[1]
            if subcmd == "kill" and len(parts) > 2:
                tid = parts[2]
                cleaned_tid = tid.strip("<>")
                safe_tid = _esc(cleaned_tid)
                if kill_background_task(cleaned_tid):
                    print_formatted_text(HTML(f"<style fg='{t_theme['ok']}'>✔ 后台进程 {safe_tid} 已成功终结。</style>"))
                else:
                    sub_tasks = subagent_manager.snapshot()
                    matched = [t for t in sub_tasks if t.task_id == cleaned_tid]
                    if matched and matched[0].status not in {"completed", "failed", "cancelled", "timed_out"}:
                        subagent_manager.cancel(cleaned_tid, "用户通过 /tasks kill 终止")
                        print_formatted_text(HTML(f"<style fg='{t_theme['ok']}'>✔ 子代理任务 {safe_tid} 已请求取消。</style>"))
                    else:
                        print_formatted_text(HTML(f"<style fg='{t_theme['err']}'>❌ 找不到任务 ID: {safe_tid}，或任务已经提前结束。</style>"))
            elif subcmd in ("tail", "log", "status") and len(parts) > 2:
                tid = parts[2]
                cleaned_tid = tid.strip("<>")
                safe_tid = _esc(cleaned_tid)
                # 先查 shell 后台进程日志；返回的 🚨 错误信息不算有效日志，
                # 必须继续查子代理任务状态，避免对 subagent-* ID 显示"未找到日志"
                output = get_task_output(cleaned_tid, tail_lines=30)
                shell_log_available = bool(
                    output
                    and output.strip()
                    and not output.strip().startswith("🚨")
                )
                if shell_log_available:
                    print_formatted_text(HTML(
                        f"  <style fg='{t_theme['accent']}'><b>◈ 任务 {safe_tid} 最新 30 行日志</b></style>"
                    ))
                    # Low 修复：任务日志可能携带 ANSI 控制序列，直接 print 会注入终端样式；
                    # get_task_output 已整段剥离，这里兜底再过一次共享清洗函数
                    from core.process_io import strip_ansi_escapes
                    print(strip_ansi_escapes(output))
                else:
                    sub_tasks = subagent_manager.snapshot()
                    matched = [t for t in sub_tasks if t.task_id == cleaned_tid]
                    if matched:
                        t = matched[0]
                        print_formatted_text(HTML(
                            f"  <style fg='{t_theme['border']}'>╭─</style>"
                            f"<style fg='{t_theme['accent']}'><b> ◈ 子代理任务 {safe_tid} 状态</b></style>"
                        ))
                        status_color = {
                            "completed": t_theme["ok"],
                            "failed": t_theme["err"],
                            "cancelled": t_theme["text_muted"],
                            "timed_out": t_theme["err"],
                        }.get(t.status, t_theme["warn"])
                        print_formatted_text(HTML(
                            f"  <style fg='{t_theme['border']}'>│</style> 状态: <style fg='{status_color}'>{t.status}</style> "
                            f"<style fg='{t_theme['text_muted']}'>· 类型: </style><style fg='{t_theme['accent_soft']}'>{t.subagent_type}</style> "
                            f"<style fg='{t_theme['text_muted']}'>· 阶段: {t.stage}</style>"
                        ))
                        print_formatted_text(HTML(
                            f"  <style fg='{t_theme['border']}'>│</style> <style fg='{t_theme['text_muted']}'>"
                            f"执行: {round(t.execution_elapsed, 1)}s · 等待审批: {round(t.approval_wait_elapsed, 1)}s</style>"
                        ))
                        detail_turn_code = turn_short_code(t.parent_turn_id)
                        if detail_turn_code:
                            print_formatted_text(HTML(
                                f"  <style fg='{t_theme['border']}'>│</style> <style fg='{t_theme['text_muted']}'>回合: [turn:{detail_turn_code}]</style>"
                            ))
                        desc_safe = _esc((t.description or "")[:80])
                        print_formatted_text(HTML(
                            f"  <style fg='{t_theme['border']}'>│</style> 描述: {desc_safe}"
                        ))
                        if t.result:
                            result_safe = _esc((t.result or "")[:500])
                            print_formatted_text(HTML(
                                f"  <style fg='{t_theme['border']}'>│</style> <style fg='{t_theme['text_muted']}'>结果摘要: {result_safe}</style>"
                            ))
                        if t.error:
                            error_safe = _esc((t.error or "")[:500])
                            print_formatted_text(HTML(
                                f"  <style fg='{t_theme['border']}'>│</style> <style fg='{t_theme['err']}'>错误: {error_safe}</style>"
                            ))
                        print_formatted_text(HTML(f"  <style fg='{t_theme['border']}'>╰{'─' * 40}</style>"))
                    else:
                        # shell 日志返回错误时直接展示错误信息，否则提示无日志
                        if output and output.strip():
                            print_formatted_text(HTML(f"<style fg='{t_theme['warn']}'>{_esc(output.strip())}</style>"))
                        else:
                            print_formatted_text(HTML(f"<style fg='{t_theme['warn']}'>ℹ 任务 {safe_tid} 没有可用日志或状态信息。</style>"))
            else:
                print_formatted_text(HTML(f"<style fg='{t_theme['err']}'>❌ 用法错误。可选指令：</style>"))
                print_formatted_text(HTML(f"  <style fg='{t_theme['accent_soft']}'>/tasks</style>           : 列出所有后台任务"))
                print_formatted_text(HTML(f"  <style fg='{t_theme['accent_soft']}'>/tasks kill &lt;ID&gt;</style> : 终结指定后台任务"))
                print_formatted_text(HTML(f"  <style fg='{t_theme['accent_soft']}'>/tasks tail &lt;ID&gt;</style> : 查看任务日志输出"))
        else:
            shell_tasks = list_active_tasks()
            sub_snapshots = subagent_manager.snapshot()
            bg_sub_snapshots = [t for t in sub_snapshots if t.run_in_background]

            if not shell_tasks and not bg_sub_snapshots:
                print_formatted_text(HTML(f"<style fg='{t_theme['warn']}'>ℹ 当前没有运行中的后台异步任务。</style>"))
            else:
                if shell_tasks:
                    print_formatted_text(HTML(
                        f"<style fg='{t_theme['accent']}'><b>◈ 当前运行的后台进程（shell）</b></style>"
                    ))
                    for t in shell_tasks:
                        safe_cmd = _esc(t['command'])
                        turn_code = turn_short_code(t.get('turn_id'))
                        turn_suffix = (
                            f" · 回合: <style fg='{t_theme['text_muted']}'>[turn:{turn_code}]</style>" if turn_code else ""
                        )
                        print_formatted_text(HTML(
                            f"  <style fg='{t_theme['accent']}'>•</style> ID: <style fg='{t_theme['accent_soft']}'>{t['task_id']}</style> "
                            f"<style fg='{t_theme['text_muted']}'>· PID: {t['pid']} · 运行: {t['duration']}{turn_suffix} · 命令: {safe_cmd}</style>"
                        ))
                if bg_sub_snapshots:
                    if shell_tasks:
                        print_formatted_text(HTML(""))
                    print_formatted_text(HTML(
                        f"<style fg='{t_theme['accent']}'><b>◈ 当前运行的后台子代理</b></style>"
                    ))
                    for t in bg_sub_snapshots:
                        status_color = {
                            "completed": t_theme["ok"],
                            "failed": t_theme["err"],
                            "cancelled": t_theme["text_muted"],
                            "timed_out": t_theme["err"],
                            "in_progress": t_theme["accent"],
                            "running": t_theme["accent"],
                        }.get(t.status, t_theme["text_muted"])
                        desc_safe = _esc((t.description or "")[:50])
                        sub_turn_code = turn_short_code(t.parent_turn_id)
                        sub_turn_suffix = (
                            f" · 回合: <style fg='{t_theme['text_muted']}'>[turn:{sub_turn_code}]</style>" if sub_turn_code else ""
                        )
                        print_formatted_text(HTML(
                            f"  <style fg='{status_color}'>•</style> ID: <style fg='{t_theme['accent_soft']}'>{t.task_id}</style> "
                            f"<style fg='{t_theme['text_muted']}'>· 状态: </style><style fg='{status_color}'>{t.status}</style> "
                            f"<style fg='{t_theme['text_muted']}'>· 类型: {t.subagent_type} · 阶段: {t.stage} "
                            f"· 执行: {round(t.execution_elapsed, 1)}s{sub_turn_suffix} · 描述: {desc_safe}</style>"
                        ))
                print_formatted_text(HTML(
                    f"<style fg='{t_theme['text_muted']}'>提示：你可以输入 </style>"
                    f"<style fg='{t_theme['accent_soft']}'>/tasks tail &lt;ID&gt;</style>"
                    f"<style fg='{t_theme['text_muted']}'> 查看日志/状态，或 </style>"
                    f"<style fg='{t_theme['accent_soft']}'>/tasks kill &lt;ID&gt;</style>"
                    f"<style fg='{t_theme['text_muted']}'> 终结任务。</style>"
                ))

    elif cmd in ("/plan", "/plans"):
        from core.task_scheduler import get_default_scheduler

        t = _get_theme(current_theme)
        scheduler = get_default_scheduler()
        if len(parts) == 1:
            items = get_default_plan_manager().snapshot()
            if not items:
                print_formatted_text(HTML(f"  <style fg='{t['warn']}'>ℹ 当前会话没有任务计划。</style>"))
            else:
                completed = sum(item.get("status") == "completed" for item in items)
                pct = int(completed * 100 / len(items)) if items else 0
                bar = _progress_bar_html(completed, len(items), width=12)
                print_formatted_text(HTML(
                    f"  <style fg='{t['border']}'>╭─</style>"
                    f"<style fg='{t['accent']}'><b> ❖ 当前任务计划 ({completed}/{len(items)})</b></style> "
                    f"{bar} <style fg='{t['text_muted']}'>{pct}%</style>"
                ))
                for item in items:
                    st = str(item.get("status") or "").lower()
                    marker = plan_mark(st)
                    st_hex = {
                        "completed": t["ok"],
                        "done": t["ok"],
                        "in_progress": t["accent"],
                        "active": t["accent"],
                        "failed": t["err"],
                        "blocked": t["warn"],
                        "cancelled": t["text_muted"],
                    }.get(st, t["text_muted"])
                    subject = _esc(item.get("subject", ""))
                    stage = str(item.get("stage", "") or "").strip()
                    stage_tag = {"plan": "计划", "build": "构建", "review": "审查"}.get(stage, "")
                    stage_suffix = f" <style fg='{t['text_muted']}'>[{stage_tag}]</style>" if stage_tag else ""
                    print_formatted_text(HTML(
                        f"  <style fg='{t['border']}'>│</style> <style fg='{st_hex}'>{marker}</style> "
                        f"<style fg='{t['text_muted']}'>{_esc(item['id'])}</style>  "
                        f"<style fg='{t['text']}'>{subject}</style>{stage_suffix}"
                    ))
                print_formatted_text(HTML(f"  <style fg='{t['border']}'>╰{'─' * 40}</style>"))
        else:
            action = parts[1].lower()
            if action == "pause":
                scheduler.pause()
                print_formatted_text(HTML(f"<style fg='{t['warn']}'>⏸ 任务计划已暂停，不再启动新的计划项。</style>"))
            elif action == "resume":
                scheduler.resume()
                print_formatted_text(HTML(f"<style fg='{t['ok']}'>▶ 任务计划已恢复。</style>"))
            elif action == "retry" and len(parts) > 2:
                item_id = parts[2].strip("<>")
                if scheduler.retry(item_id):
                    print_formatted_text(HTML(f"<style fg='{t['ok']}'>↻ 已重新排队计划项：{_esc(item_id)}</style>"))
                else:
                    print_formatted_text(HTML(f"<style fg='{t['err']}'>❌ 计划项 {_esc(item_id)} 当前不可重试。</style>"))
            elif action == "cancel":
                count = scheduler.cancel_all()
                print_formatted_text(HTML(f"<style fg='{t['warn']}'>⏹ 已取消 {count} 个未完成计划项。</style>"))
            else:
                print_formatted_text(HTML(f"<style fg='{t['err']}'>❌ 用法：/plan、/plan pause、/plan resume、/plan retry &lt;id&gt;、/plan cancel</style>"))

    elif cmd == "/resume":
        # 断点续跑（规格 §4.1 CLI 入口）：开关关闭只提示；否则打印断点 + 一次性确认。
        try:
            _handle_resume_command(current_session)
        except Exception as e:
            print_formatted_text(HTML(f"<ansired>⚠️ {_esc(str(e))}</ansired>"))

    elif cmd == "/undo":
        try:
            handle_undo_command(command_text, current_session)
        except Exception as e:
            print_formatted_text(HTML(f"<ansired>⚠️ {_esc(str(e))}</ansired>"))

    elif cmd == "/checkpoint" or cmd.startswith("/checkpoint "):
        # 会话级检查点管理：list 查看、restore <turn_id> 整轮回滚。
        # 恢复动作已迁移为 TurnUndoService 兼容入口（预检 + 令牌 + 多文件事务）
        try:
            handle_checkpoint_command(command_text, current_session)
        except Exception as e:
            print_formatted_text(HTML(f"<ansired>⚠️ {_esc(str(e))}</ansired>"))

    elif cmd == "/share" or cmd.startswith("/share "):
        from core.share import export_session_markdown, export_session_html, export_session_json
        parts = command_text.strip().split(maxsplit=1)
        fmt = parts[1].strip() if len(parts) > 1 else "md"
        if fmt == "md" or fmt == "markdown":
            result = export_session_markdown(current_session, manager=manager)
        elif fmt == "html":
            result = export_session_html(current_session, manager=manager)
        elif fmt == "json":
            result = export_session_json(current_session, manager=manager)
        else:
            result = f"🚨 未知格式: {fmt}。支持: md / html / json"
        print_formatted_text(HTML(f"<ansigray>{_esc(result)}</ansigray>"))

    elif cmd == "/diagram" or cmd.startswith("/diagram "):
        from core.diagram_export import handle_diagram_command

        print_formatted_text(HTML(f"<ansigray>{_esc(handle_diagram_command(command_text))}</ansigray>"))

    elif cmd == "/worktree" or cmd.startswith("/worktree "):
        from core.worktree import list_worktrees, create_worktree, remove_worktree, prune_worktrees
        parts = command_text.strip().split(maxsplit=3)
        sub = parts[1] if len(parts) > 1 else "list"

        if sub == "list":
            print_formatted_text(HTML(f"<ansigray>{_esc(list_worktrees())}</ansigray>"))
        elif sub == "create":
            if len(parts) < 4:
                print_formatted_text(HTML("<ansired>⚠️ 用法: /worktree create &lt;branch&gt; &lt;path&gt;</ansired>"))
            else:
                branch, wt_path = parts[2], parts[3]
                print_formatted_text(HTML(f"<ansigray>{_esc(create_worktree(branch, wt_path))}</ansigray>"))
        elif sub == "remove":
            if len(parts) < 3:
                print_formatted_text(HTML("<ansired>⚠️ 用法: /worktree remove &lt;path&gt;</ansired>"))
            else:
                print_formatted_text(HTML(f"<ansigray>{_esc(remove_worktree(parts[2]))}</ansigray>"))
        elif sub == "prune":
            print_formatted_text(HTML(f"<ansigray>{_esc(prune_worktrees())}</ansigray>"))
        else:
            print_formatted_text(HTML("<ansired>⚠️ 未知子命令。用法: /worktree [list|create|remove|prune]</ansired>"))

    elif cmd == "/git" or cmd.startswith("/git "):
        # Git 工作台（规格 §12）：严格子命令解析 → GitApplicationService，
        # 高风险操作两步确认（--confirm），绝不透传任意参数
        from core.git_cli import handle_git_command
        handle_git_command(command_text)

    elif cmd == "/mcp" or cmd.startswith("/mcp "):
        from core.mcp_client import mcp_manager
        parts = command_text.strip().split(maxsplit=2)
        sub = parts[1].strip() if len(parts) > 1 else "open"

        if sub == "open":
            # 打开全屏 CLI 配置器：退出主 App，由主循环在主线程安全开启
            # 注意：pending_action 已在函数顶部 /models 分支声明为 global，这里直接赋值
            pending_action = "open_mcp"
            if app_instance:
                app_instance.exit()

        elif sub == "status":
            try:
                mcp_manager._ensure_connected()
            except Exception as e:
                print_formatted_text(HTML(f"<ansiyellow>ℹ 连接检查异常（不影响后续使用）: {_esc(str(e))}</ansiyellow>"))

            config = mcp_manager.config.get("mcpServers", {}) if isinstance(mcp_manager.config, dict) else {}
            if not config:
                print_formatted_text(HTML("<ansiyellow>ℹ 未配置任何 MCP 服务。</ansiyellow>"))
                print_formatted_text(HTML(
                    f"<ansigray>  输入 <ansicyan>/mcp</ansicyan> 打开配置器添加，或编辑 <ansicyan>{_esc(mcp_manager.config_file)}</ansicyan> 后运行 /mcp reconnect 生效。</ansigray>"
                ))
            else:
                print_formatted_text(HTML(f"<ansicyan>🔌 已配置 {len(config)} 个 MCP 服务:</ansicyan>"))
                try:
                    mcp_manager.get_schemas()
                except Exception:
                    pass
                for name, conf in config.items():
                    enabled = True
                    if isinstance(conf, dict):
                        enabled = conf.get("enabled", True) is not False
                    check = "<ansigreen>[✓]</ansigreen>" if enabled else "<ansigray>[ ]</ansigray>"
                    connected = name in mcp_manager.servers
                    tool_count = sum(
                        1 for s in mcp_manager.tool_schemas
                        if s.get("function", {}).get("name", "").startswith(f"{name}__")
                    )
                    if not enabled:
                        state = "<ansigray>已禁用</ansigray>"
                    elif connected:
                        state = "<ansigreen>已连接</ansigreen>"
                    else:
                        state = "<ansired>未连接</ansired>"
                    print_formatted_text(HTML(
                        f"  {check} <b><ansicyan>{_esc(name)}</ansicyan></b>  {state}  <ansigray>工具数: {tool_count}</ansigray>"
                    ))
                print_formatted_text(HTML(
                    "<ansigray>  使用 /mcp tools 查看工具详情，/mcp enable|disable &lt;name&gt; 切换开关，/mcp reconnect 重连。</ansigray>"
                ))

        elif sub == "tools":
            try:
                mcp_manager._ensure_connected()
            except Exception:
                pass
            schemas = mcp_manager.get_schemas()
            if not schemas:
                print_formatted_text(HTML("<ansiyellow>ℹ 当前没有可用的 MCP 工具。</ansiyellow>"))
                print_formatted_text(HTML("<ansigray>  请先在 mcp.json 配置服务并 /mcp reconnect。</ansigray>"))
            else:
                print_formatted_text(HTML(f"<ansicyan>🔧 共 {len(schemas)} 个 MCP 工具:</ansicyan>"))
                by_server = {}
                for s in schemas:
                    fn = s.get("function", {})
                    full_name = fn.get("name", "")
                    if "__" in full_name:
                        srv, tool_name = full_name.split("__", 1)
                    else:
                        srv, tool_name = "(unknown)", full_name
                    by_server.setdefault(srv, []).append((tool_name, fn.get("description", "")))
                for srv, tools in by_server.items():
                    print_formatted_text(HTML(f"  <ansicyan>{_esc(srv)}</ansicyan> ({len(tools)}):"))
                    for tool_name, desc in tools:
                        desc_short = (desc or "").split("\n", 1)[0]
                        if len(desc_short) > 60:
                            desc_short = desc_short[:57] + "..."
                        print_formatted_text(HTML(
                            f"    • <ansigreen>{_esc(tool_name)}</ansigreen> <ansigray>{_esc(desc_short)}</ansigray>"
                        ))

        elif sub == "reconnect":
            print_formatted_text(HTML("<ansiyellow>🔄 正在关闭旧连接并重新发起 MCP 连接...</ansiyellow>"))
            ok, msg = mcp_manager.reconnect()
            if ok:
                print_formatted_text(HTML(f"<ansigreen>✔ {_esc(msg)}</ansigreen>"))
                print_formatted_text(HTML("<ansigray>  连接状态将在后台异步更新，稍后用 /mcp status 查看。</ansigray>"))
            else:
                print_formatted_text(HTML(f"<ansired>⚠️ {_esc(msg)}</ansired>"))

        elif sub == "enable":
            target = parts[2].strip() if len(parts) > 2 else ""
            if not target:
                print_formatted_text(HTML("<ansired>⚠️ 用法: /mcp enable &lt;server_name&gt;</ansired>"))
            else:
                print_formatted_text(HTML(f"<ansiyellow>⚡ 正在启用服务 {_esc(target)}...</ansiyellow>"))
                ok, msg = mcp_manager.set_server_enabled(target, True)
                if ok:
                    print_formatted_text(HTML(f"<ansigreen>✔ {_esc(msg)}</ansigreen>"))
                    print_formatted_text(HTML("<ansigray>  连接状态将在后台异步更新，稍后用 /mcp status 查看。</ansigray>"))
                else:
                    print_formatted_text(HTML(f"<ansired>⚠️ {_esc(msg)}</ansired>"))

        elif sub == "disable":
            target = parts[2].strip() if len(parts) > 2 else ""
            if not target:
                print_formatted_text(HTML("<ansired>⚠️ 用法: /mcp disable &lt;server_name&gt;</ansired>"))
            else:
                print_formatted_text(HTML(f"<ansiyellow>🌙 正在禁用服务 {_esc(target)}...</ansiyellow>"))
                ok, msg = mcp_manager.set_server_enabled(target, False)
                if ok:
                    print_formatted_text(HTML(f"<ansigreen>✔ {_esc(msg)}</ansigreen>"))
                    print_formatted_text(HTML("<ansigray>  配置已保留，需要时用 /mcp enable 重新启用。</ansigray>"))
                else:
                    print_formatted_text(HTML(f"<ansired>⚠️ {_esc(msg)}</ansired>"))

        elif sub == "edit":
            config_path = mcp_manager.config_file
            print_formatted_text(HTML(f"<ansicyan>📝 正在打开配置文件: {_esc(config_path)}</ansicyan>"))
            print_formatted_text(HTML("<ansigray>  编辑保存后，回到 CLI 运行 /mcp reconnect 让改动生效。</ansigray>"))
            try:
                import platform
                if platform.system() == "Windows":
                    import subprocess
                    subprocess.Popen(["notepad", config_path])
                else:
                    import subprocess
                    editor = os.environ.get("EDITOR", "vi")
                    subprocess.Popen([editor, config_path])
            except Exception as e:
                print_formatted_text(HTML(f"<ansired>⚠️ 打开编辑器失败: {_esc(str(e))}</ansired>"))
                print_formatted_text(HTML(f"<ansigray>  请手动编辑: {_esc(config_path)}</ansigray>"))

        else:
            print_formatted_text(HTML(
                "<ansired>⚠️ 未知子命令。用法: /mcp [open|status|tools|reconnect|enable &lt;name&gt;|disable &lt;name&gt;|edit]</ansired>"
            ))

    elif cmd == "/plugins" or cmd.startswith("/plugins "):
        from core import plugin_loader
        sub_parts = command_text.strip().split(maxsplit=2)
        sub = sub_parts[1].strip() if len(sub_parts) > 1 else ""

        if sub == "enable":
            target = sub_parts[2].strip() if len(sub_parts) > 2 else ""
            if not target:
                print_formatted_text(HTML("<ansired>⚠️ 用法: /plugins enable &lt;插件名&gt;</ansired>"))
            else:
                ok, msg = plugin_loader.toggle_plugin(target, True)
                if ok:
                    print_formatted_text(HTML(f"<ansigreen>✔ {_esc(msg)}</ansigreen>"))
                else:
                    print_formatted_text(HTML(f"<ansired>⚠️ {_esc(msg)}</ansired>"))

        elif sub == "disable":
            target = sub_parts[2].strip() if len(sub_parts) > 2 else ""
            if not target:
                print_formatted_text(HTML("<ansired>⚠️ 用法: /plugins disable &lt;插件名&gt;</ansired>"))
            else:
                ok, msg = plugin_loader.toggle_plugin(target, False)
                if ok:
                    print_formatted_text(HTML(f"<ansigreen>✔ {_esc(msg)}</ansigreen>"))
                else:
                    print_formatted_text(HTML(f"<ansired>⚠️ {_esc(msg)}</ansired>"))

        elif sub == "restore":
            target = sub_parts[2].strip() if len(sub_parts) > 2 else ""
            if not target:
                print_formatted_text(HTML("<ansired>⚠️ 用法: /plugins restore &lt;插件名&gt;</ansired>"))
            else:
                ok, msg = plugin_loader.restore_plugin(target)
                if ok:
                    print_formatted_text(HTML(f"<ansigreen>✔ {_esc(msg)}</ansigreen>"))
                else:
                    print_formatted_text(HTML(f"<ansired>⚠️ {_esc(msg)}</ansired>"))

        elif sub == "reload":
            print_formatted_text(HTML("<ansiyellow>🔄 正在重新扫描插件目录...</ansiyellow>"))
            from core.tool_registry import ToolRegistry
            registry = ToolRegistry()
            plugin_loader.load_and_register(registry)
            print_formatted_text(HTML("<ansigreen>✔ 插件清单已刷新。</ansigreen>"))
            print_formatted_text(HTML("<ansigray>  注意：工具注册需重启后才会生效，reload 仅刷新列表显示。</ansigray>"))

        elif sub == "":
            infos = plugin_loader.get_plugin_infos()
            if not infos:
                print_formatted_text(HTML("<ansiyellow>ℹ 未发现任何插件。</ansiyellow>"))
                print_formatted_text(HTML(
                    "<ansigray>  插件目录: 用户级 ~/.ycode/plugins/ 、内置 plugins/ ；每个插件一个目录，内含 plugin.json。</ansigray>"
                ))
            else:
                print_formatted_text(HTML(f"<ansicyan>🧩 已发现 {len(infos)} 个插件:</ansicyan>"))
                for info in infos:
                    status = info.get("status")
                    if status == "loaded":
                        check = "<ansigreen>[✓]</ansigreen>"
                        state = "<ansigreen>已加载</ansigreen>"
                    elif status == "disabled":
                        check = "<ansigray>[ ]</ansigray>"
                        state = "<ansigray>已禁用</ansigray>"
                    elif status == "removed":
                        check = "<ansigray>[ ]</ansigray>"
                        state = "<ansigray>已移除（可恢复）</ansigray>"
                    elif status == "overridden":
                        check = "<ansigray>[ ]</ansigray>"
                        state = "<ansigray>被同名用户级插件覆盖</ansigray>"
                    elif status == "pending_enable":
                        check = "<ansiyellow>[~]</ansiyellow>"
                        state = "<ansiyellow>已启用（重启后加载）</ansiyellow>"
                    elif status == "pending_disable":
                        check = "<ansiyellow>[~]</ansiyellow>"
                        state = "<ansiyellow>已禁用（重启后卸载）</ansiyellow>"
                    else:
                        check = "<ansired>[x]</ansired>"
                        state = "<ansired>加载失败</ansired>"
                    source = "用户级" if info.get("source") == "user" else "内置"
                    tools = info.get("tools") or []
                    tool_part = f"  <ansigray>工具数: {len(tools)}</ansigray>" if status == "loaded" else ""
                    print_formatted_text(HTML(
                        f"  {check} <b><ansicyan>{_esc(info['name'])}</ansicyan></b> <ansigray>v{_esc(str(info.get('version') or '?'))}</ansigray>  {source} · {state}{tool_part}"
                    ))
                    desc = info.get("description")
                    if desc:
                        print_formatted_text(HTML(f"      <ansigray>{_esc(desc)}</ansigray>"))
                    if status == "failed" and info.get("error"):
                        print_formatted_text(HTML(f"      <ansired>错误: {_esc(info['error'])}</ansired>"))
                    if tools:
                        print_formatted_text(HTML(f"      <ansigray>工具: {_esc(', '.join(tools))}</ansigray>"))
                    print_formatted_text(HTML(f"      <ansigray>路径: {_esc(info.get('path', ''))}</ansigray>"))
                print_formatted_text(HTML(
                    "<ansigray>  使用 /plugins enable|disable &lt;插件名&gt; 切换开关（重启后生效），/plugins restore &lt;插件名&gt; 恢复已移除的内置插件，/plugins reload 刷新清单。</ansigray>"
                ))
        else:
            print_formatted_text(HTML(
                "<ansired>⚠️ 未知子命令。用法: /plugins [enable &lt;name&gt;|disable &lt;name&gt;|restore &lt;name&gt;|reload]</ansired>"
            ))

    elif cmd == "/hooks" or command_text.strip().startswith("/hooks "):
        from core.workspace_context import get_workdir
        from core.hooks.trust import apply_hook_trust_action, list_hooks_with_status

        sub_parts = command_text.strip().split()
        sub = sub_parts[1].strip().lower() if len(sub_parts) > 1 else ""
        workspace_root = str(get_workdir() or os.getcwd())

        entries = []
        try:
            entries = list_hooks_with_status(workspace_root=workspace_root)
        except Exception as e:
            print_formatted_text(HTML(f"<ansired>⚠️ 读取 Hook 配置失败: {_esc(str(e))}</ansired>"))

        if sub == "import":
            # ZCode hooks 导入（待开发/ZCode钩子与工作流导入/设计规格.md §2.1）：
            # 扫描 → 预览（导入/跳过逐条点名，绝不静默丢条目）→ 确认 → 写入。
            from core.hooks.zcode_import import apply_plan, plan_import

            try:
                plan = plan_import(workspace_root=workspace_root)
            except Exception as e:
                print_formatted_text(HTML(f"<ansired>⚠️ 扫描 ZCode 配置失败: {_esc(str(e))}</ansired>"))
            else:
                files = plan.get("files", [])
                if not files:
                    print_formatted_text(HTML(
                        "<ansiyellow>ℹ 未发现任何 ZCode hooks 配置（扫描 ~/.zcode/cli/config.json、"
                        "&lt;项目&gt;/.zcode/config.json、zcode.json、.zcode/settings.json）。</ansiyellow>"
                    ))
                else:
                    total_entries = 0
                    for file_report in files:
                        label = file_report.get("label")
                        path = file_report.get("path")
                        if file_report.get("status") != "imported":
                            print_formatted_text(HTML(
                                f"  <ansigray>[跳过] {label}: {file_report.get('reason')}（{_esc(str(path))}）</ansigray>"
                            ))
                            continue
                        for entry in file_report.get("entries", []):
                            total_entries += 1
                            print_formatted_text(HTML(
                                f"  <ansigreen>[导入]</ansigreen> <b><ansicyan>{_esc(entry['event'])}</b></ansicyan>"
                                f" matcher={_esc(entry['matcher'])} timeout={entry['timeout_ms']}ms"
                                f" <ansigray>{_esc(entry['command'][:70])}</ansigray>"
                            ))
                        for item in file_report.get("skipped", []):
                            print_formatted_text(HTML(
                                f"  <ansiyellow>[跳过]</ansiyellow> {_esc(item.get('event', '?'))}"
                                f" <ansigray>{_esc(item.get('reason', ''))}</ansigray>"
                            ))
                    if total_entries == 0:
                        print_formatted_text(HTML("<ansiyellow>ℹ 没有可导入的条目（全部被跳过，原因见上）。</ansiyellow>"))
                    else:
                        try:
                            answer = input(f"确认导入以上 {total_entries} 条 hook？(y/N) ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            answer = ""
                        if answer in ("y", "yes"):
                            summary = apply_plan(plan, workspace_root=workspace_root)
                            for scope_item in summary.get("scopes", []):
                                print_formatted_text(HTML(
                                    f"<ansigreen>✔ 写入 {scope_item['written']} 条 → {_esc(scope_item['path'])}</ansigreen>"
                                ))
                            for err in summary.get("errors", []):
                                print_formatted_text(HTML(f"<ansired>⚠️ {_esc(err)}</ansired>"))
                            if summary.get("duplicates"):
                                print_formatted_text(HTML(
                                    f"<ansigray>  跳过 {summary['duplicates']} 条重复条目（目标文件已存在同事件同命令）。</ansigray>"
                                ))
                            print_formatted_text(HTML(
                                "<ansigray>  项目级条目按既有信任流程生效（/hooks 查看）；撤销导入用 /hooks rollback。</ansigray>"
                            ))
                        else:
                            print_formatted_text(HTML("<ansigray>已取消，未写入任何内容。</ansigray>"))

        elif sub == "rollback":
            from core.hooks.zcode_import import rollback_import

            result = rollback_import(workspace_root=workspace_root)
            if result.get("removed"):
                for item in result["touched"]:
                    print_formatted_text(HTML(
                        f"<ansigreen>✔ 从 {_esc(item['path'])} 移除 {item['removed']} 条导入条目</ansigreen>"
                    ))
            else:
                print_formatted_text(HTML("<ansiyellow>ℹ 没有找到任何 ZCode 导入条目。</ansiyellow>"))

        elif sub == "":
            if not entries:
                print_formatted_text(HTML("<ansiyellow>ℹ 未配置任何 Hook。</ansiyellow>"))
                print_formatted_text(HTML(
                    "<ansigray>  钩子文件: 用户级 ~/.ycode/hooks.json 、项目级 &lt;项目&gt;/.ycode/hooks.json</ansigray>"
                ))
            else:
                print_formatted_text(HTML(f"<ansicyan>🪝 已发现 {len(entries)} 个 Hook:</ansicyan>"))
                for idx, item in enumerate(entries, start=1):
                    status = item.get("status")
                    if status == "trusted":
                        check = "<ansigreen>[✓]</ansigreen>"
                        state = "<ansigreen>已信任</ansigreen>"
                    elif status == "pending":
                        check = "<ansiyellow>[~]</ansiyellow>"
                        state = "<ansiyellow>待确认</ansiyellow>"
                    elif status == "stale":
                        check = "<ansiyellow>[~]</ansiyellow>"
                        state = "<ansiyellow>配置已变更</ansiyellow>"
                    elif status == "blocked":
                        check = "<ansired>[x]</ansired>"
                        state = "<ansired>已拒绝</ansired>"
                    elif status == "revoked":
                        check = "<ansigray>[ ]</ansigray>"
                        state = "<ansigray>已撤销</ansigray>"
                    else:
                        check = "<ansigray>[?]</ansigray>"
                        state = f"<ansigray>{_esc(str(status))}</ansigray>"
                    scope_label = "用户级" if item.get("scope") == "user" else "项目级"
                    command_preview = str(item.get("command") or "")
                    if len(command_preview) > 70:
                        command_preview = command_preview[:70] + "..."
                    print_formatted_text(HTML(
                        f"  [{idx}] {check} <b><ansicyan>{_esc(str(item.get('event') or '?'))}</ansicyan></b>"
                        f"  <ansigray>{scope_label}</ansigray> · {state}  <ansigray>{_esc(command_preview)}</ansigray>"
                    ))
                print_formatted_text(HTML(
                    "<ansigray>  使用 /hooks trust|deny|revoke|reset &lt;序号&gt; 管理信任（序号以最近一次 /hooks 输出为准）。</ansigray>"
                ))

        elif sub in ("trust", "deny", "revoke", "reset"):
            # 序号解析：越界/非数字一律给用法提示，绝不带着无效下标去调信任操作
            index_text = sub_parts[2].strip() if len(sub_parts) > 2 else ""
            index = None
            if index_text.isdigit():
                try:
                    index = int(index_text)
                except ValueError:
                    index = None  # 如 "²"：isdigit() 为真但 int() 不可解析
            if index is None or not (1 <= index <= len(entries)):
                print_formatted_text(HTML(
                    f"<ansired>⚠️ 用法: /hooks {sub} &lt;序号&gt;（先运行 /hooks 查看编号）</ansired>"
                ))
            else:
                action = "grant" if sub == "trust" else sub
                ok, msg = apply_hook_trust_action(
                    action, entries[index - 1]["digest"], workspace_root=workspace_root
                )
                if ok:
                    print_formatted_text(HTML(f"<ansigreen>✔ {_esc(msg)}</ansigreen>"))
                    print_formatted_text(HTML("<ansigray>  运行 /hooks 查看最新状态。</ansigray>"))
                else:
                    print_formatted_text(HTML(f"<ansired>⚠️ {_esc(msg)}</ansired>"))

        else:
            print_formatted_text(HTML(
                "<ansired>⚠️ 未知子命令。用法: /hooks [import|rollback|trust|deny|revoke|reset &lt;序号&gt;]</ansired>"
            ))
    elif cmd == "/doctor" or cmd.startswith("/doctor "):
        # 聚合自检（T3）：配置/凭据/数据/数据库/MCP/网络/GUI 运行时一站式体检；
        # --json 输出机器可读结果（供脚本采集/报障），默认人类可读。
        # export 子命令（T5）：导出排障诊断包（默认精简、不含消息正文；
        # --full 追加经脱敏的会话消息，需 y 交互确认后才执行）。
        from core.doctor import run_checks

        doctor_args = [part.lower() for part in parts[1:]]
        if doctor_args and doctor_args[0] == "export":
            from core.share import default_export_dir
            from core.trace_export import export_diagnostics_bundle

            export_args = doctor_args[1:]
            bad_flags = [part for part in export_args if part not in ("--full", "y")]
            full_requested = "--full" in export_args
            confirmed = "y" in export_args
            if bad_flags:
                print_formatted_text(HTML(
                    f"<ansired>❌ 未知参数「{_esc(bad_flags[0])}」。用法: /doctor export [--full y]</ansired>"
                ))
            elif full_requested and not confirmed:
                print_formatted_text(HTML(
                    "<ansiyellow>⚠️ --full 将在诊断包中追加当前会话消息正文（已按既有规则脱敏）。</ansiyellow>"
                ))
                print_formatted_text(HTML(
                    "确认请输入: <ansicyan>/doctor export --full y</ansicyan> "
                    "<ansigray>(分享前请人工复核包内内容)</ansigray>"
                ))
            else:
                try:
                    bundle_path = export_diagnostics_bundle(
                        dest_dir=str(default_export_dir()),
                        session_name=current_session,
                        full=full_requested,
                        confirm=confirmed,
                    )
                except Exception as exc:
                    print_formatted_text(HTML(
                        f"<ansired>❌ 诊断包导出失败：{_esc(str(exc))}</ansired>"
                    ))
                else:
                    print_formatted_text(HTML(
                        f"<ansigreen>✔ 诊断包已导出：{_esc(bundle_path)}</ansigreen>"
                    ))
                    print_formatted_text(HTML(
                        "<ansigray>  包内：manifest.json / journal.jsonl（回合轨迹仅 ID 与元数据）"
                        " / doctor.json / logs（日志存在时）。精简模式不含消息正文。</ansigray>"
                    ))
        else:
            bad_flags = [part for part in doctor_args if part != "--json"]
            if bad_flags:
                print_formatted_text(HTML(
                    f"<ansired>❌ 未知参数「{_esc(bad_flags[0])}」。用法: /doctor [--json] 或 /doctor export [--full y]</ansired>"
                ))
            else:
                import json as _doctor_json

                results = run_checks()
                if "--json" in doctor_args:
                    print_formatted_text(_doctor_json.dumps(
                        [result.to_dict() for result in results], ensure_ascii=False, indent=2
                    ))
                else:
                    status_marks = {
                        "ok": "<ansigreen>✔ 通过</ansigreen>",
                        "warn": "<ansiyellow>⚠ 警告</ansiyellow>",
                        "fail": "<ansired>✖ 失败</ansired>",
                        "skip": "<ansigray>○ 跳过</ansigray>",
                    }
                    counts = {key: 0 for key in ("ok", "warn", "fail", "skip")}
                    print_formatted_text(HTML("<b><ansicyan>🩺 星瑶Code 聚合自检</ansicyan></b>"))
                    for result in results:
                        if result.status in counts:
                            counts[result.status] += 1
                        mark = status_marks.get(result.status, _esc(result.status))
                        print_formatted_text(HTML(
                            f"  {mark}  <b>{_esc(result.name)}</b>：{_esc(result.detail)}"
                        ))
                        if result.hint:
                            print_formatted_text(HTML(f"      <ansigray>建议：{_esc(result.hint)}</ansigray>"))
                    print_formatted_text(HTML(
                        f"<b>总结</b>：{counts['ok']} 项通过 / {counts['warn']} 项警告 / "
                        f"{counts['fail']} 项失败 / {counts['skip']} 项跳过"
                    ))
                    if counts["fail"] or counts["warn"]:
                        print_formatted_text(HTML("<ansigray>  按上方建议逐项处理后重新运行 /doctor 复核。</ansigray>"))

    elif cmd == "/usage" or cmd.startswith("/usage "):
        from datetime import date as _usage_date_cls, timedelta as _usage_td
        from core import app_context as _usage_app_ctx
        _usage_mgr = _usage_app_ctx.get_manager()
        if _usage_mgr is None:
            print_formatted_text(HTML("<ansired>⚠️ 记忆数据库未初始化，暂无法统计 token 用量。</ansired>"))
        else:
            try:
                _usage_range = _parse_usage_date_args(parts[1:])
            except ValueError as _usage_err:
                print_formatted_text(HTML(
                    f"<ansired>⚠️ {_esc(str(_usage_err))}。用法: /usage [YYYY-MM-DD] [YYYY-MM-DD]</ansired>"
                ))
            else:
                print_formatted_text(HTML("<b><ansigreen>📊 Token 用量统计</ansigreen></b>"))
                if _usage_range is not None:
                    _start_d, _end_d = _usage_range
                    _render_usage_section(
                        _usage_mgr, "查询结果", _start_d.isoformat(), _end_d.isoformat()
                    )
                else:
                    _usage_today = _usage_date_cls.today()
                    _usage_ranges = [
                        ("今日", _usage_today, _usage_today),
                        ("近 7 天", _usage_today - _usage_td(days=6), _usage_today),
                        ("近 30 天", _usage_today - _usage_td(days=29), _usage_today),
                    ]
                    _usage_any_data = False
                    for _usage_label, _usage_start, _usage_end in _usage_ranges:
                        if _render_usage_section(
                            _usage_mgr, _usage_label,
                            _usage_start.isoformat(), _usage_end.isoformat(),
                        ):
                            _usage_any_data = True
                    if not _usage_any_data:
                        print_formatted_text(HTML(
                            "<ansigray>暂无 token 用量记录。统计从本次升级后开始累计，历史用量无数据。</ansigray>"
                        ))

    elif cmd == "/clear":
        _turn_separator_needed = False
        print_logo()

    elif cmd == "/dream":
        print_formatted_text(HTML(f"<ansiyellow>✨ {_agent_name()}正在闭上眼睛，在后台慢慢提炼归纳咱们的长期记忆... 梦境整理中... 🛌</ansiyellow>"))
        def run_dream_bg():
            try:
                from core.tools import dream_consolidation
                res = dream_consolidation()
                res_safe = res.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                schedule_ui(lambda message=res_safe: print_formatted_text(
                    HTML(f"\n<ansigreen>✔ {message}</ansigreen>")
                ))
            except Exception as e:
                safe_error = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                schedule_ui(lambda message=safe_error: print_formatted_text(
                    HTML(f"\n<ansired>❌ 梦境提炼失败：{message}</ansired>")
                ))
        threading.Thread(target=run_dream_bg, daemon=True).start()

    elif cmd == "/memory":
        import core.memory_kairos as mk
        idx = mk.load_memory_index()
        if not idx.strip():
            print_formatted_text(HTML("<ansiyellow>ℹ 当前没有任何长期记忆索引。你可以输入 `/remember &lt;内容&gt;` 记录，或使用 `/dream` 整理日志。</ansiyellow>"))
        else:
            print_formatted_text(HTML("<b><ansicyan>✨ 长期记忆索引内容 (MEMORY.md)：</ansicyan></b>\n"))
            print(idx)

    elif cmd == "/remember":
        note = " ".join(parts[1:]).strip()
        if not note:
            print_formatted_text(HTML("<ansired>❌ 用法错误。请输入：/remember &lt;记忆内容描述&gt;</ansired>"))
        else:
            import core.memory_kairos as mk
            try:
                mk.append_to_daily_log(note)
                print_formatted_text(HTML(f"<ansigreen>✔ 成功记录到今日记忆日志中：<b>{_esc(note)}</b></ansigreen>"))
            except Exception as e:
                print_formatted_text(HTML(f"<ansired>❌ 写入记忆日志失败：{_esc(e)}</ansired>"))

    elif cmd == "/compress" or cmd.startswith("/compress "):
        # 手动触发会话记忆压缩：保留最近若干条对话，更早的历史由大模型提炼为摘要
        # 后台执行避免阻塞 UI，状态/结果通过 schedule_ui 回调输出
        sub = parts[1].lower() if len(parts) > 1 else ""
        force = sub == "force"

        def run_compress_bg():
            try:
                from core.memory_compressor import compress_session_memory
                session = current_session
                def emit(msg):
                    safe = msg.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    schedule_ui(lambda m=safe: print_formatted_text(HTML(m)))
                ok = compress_session_memory(
                    manager,
                    session,
                    api_model=current_model,
                    api_key=current_key,
                    api_url=current_url,
                    force=force,
                    output_callback=emit,
                )
                if ok is False:
                    pass
            except Exception as e:
                safe_error = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                schedule_ui(lambda msg=safe_error: print_formatted_text(
                    HTML(f"\n<ansired>❌ 记忆压缩失败：{msg}</ansired>")
                ))

        print_formatted_text(HTML(
            f"<ansiyellow>🧠 正在后台压缩会话记忆{'（强制模式）' if force else ''}...</ansiyellow>"
        ))
        threading.Thread(target=run_compress_bg, daemon=True).start()

    elif cmd == "/goal" or cmd.startswith("/goal "):
        _goal_args_text = command_text.strip()[len(parts[0]):].strip()
        _run_goal_cli(_goal_args_text)

    elif cmd == "/exit":
        # 使用 print_formatted_text 避免 rich 在 prompt_toolkit patch_stdout 下出现 ANSI 乱码
        print_formatted_text(HTML(f"<ansigreen>👋 {_agent_name()}去休息啦，下次见！</ansigreen>"))
        if app_instance:
            app_instance.exit()
    else:
        # 使用 print_formatted_text 避免 rich 在 prompt_toolkit patch_stdout 下出现 ANSI 乱码
        safe_cmd = cmd.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        print_formatted_text(HTML(f"<ansired>❌ 未知指令: {safe_cmd}，输入 /help 查看帮助。</ansired>"))


class _RoundedFrame:

    def __init__(
        self,
        body,
        title="",
        style: str = "",
        border_style="class:frame.border",
        title_style=None,
    ) -> None:
        self.body = body

        def _border_cls() -> str:
            return (
                border_style()
                if callable(border_style)
                else str(border_style or "class:frame.border")
            )

        def _title_cls() -> str:
            if callable(title_style):
                return title_style()
            if title_style:
                return str(title_style)
            b = _border_cls()
            return "class:frame.label" if b in ("class:frame", "class:frame.border", "class:frame.dim") else b

        def _title_fragments():
            text = title() if callable(title) else str(title or "")
            if not text:
                return [(_border_cls(), "─")]
            return [(_title_cls(), f" {text} ")]

        top_row = VSplit(
            [
                Window(width=D.exact(2)),
                Window(width=1, height=1, char="╭", style=_border_cls),
                Window(width=1, height=1, char="─", style=_border_cls),
                Window(
                    content=FormattedTextControl(_title_fragments),
                    height=1,
                    dont_extend_width=True,
                ),
                Window(height=1, char="─", style=_border_cls),
                Window(width=1, height=1, char="╮", style=_border_cls),
                Window(width=D.exact(2)),
            ],
            height=1,
        )
        middle_row = VSplit(
            [
                Window(width=D.exact(2)),
                Window(width=1, char="│", style=_border_cls),
                Window(width=1),
                body,
                Window(width=1),
                Window(width=1, char="│", style=_border_cls),
                Window(width=D.exact(2)),
            ],
            padding=0,
        )
        bottom_row = VSplit(
            [
                Window(width=D.exact(2)),
                Window(width=1, height=1, char="╰", style=_border_cls),
                Window(height=1, char="─", style=_border_cls),
                Window(width=1, height=1, char="╯", style=_border_cls),
                Window(width=D.exact(2)),
            ],
            height=1,
        )
        self.container = HSplit([top_row, middle_row, bottom_row], style=style)

    def __pt_container__(self):
        return self.container


def _active_subagent_snapshots():
    tools_mod = sys.modules.get("core.tools")
    if tools_mod is None:
        return []
    mgr = getattr(tools_mod, "subagent_manager", None)
    if mgr is None:
        return []
    try:
        snaps = mgr.snapshot() or []
    except Exception:
        return []
    terminal = {"completed", "failed", "cancelled", "timed_out"}
    return [
        s for s in snaps
        if getattr(s, "status", "") not in terminal
        and (
            not getattr(s, "parent_session", "")
            or getattr(s, "parent_session", "") == current_session
        )
    ]


def _context_watermark_style(pct: float) -> str:
    if pct >= 95.0:
        return "class:status.ctx_crit"
    if pct >= 80.0:
        return "class:status.ctx_high"
    if pct >= 60.0:
        return "class:status.ctx_warn"
    return "class:status.token"


def _context_bar_text(pct: float, width: int = 6) -> str:
    width = max(2, int(width))
    clamped = max(0.0, min(100.0, float(pct)))
    filled = max(0, min(width, round(clamped / 100.0 * width)))
    if clamped > 0 and filled == 0:
        filled = 1
    return ("▓" * filled) + ("░" * (width - filled))


def _make_input_app():
    command_completions = COMMAND_COMPLETIONS

    class SlashCommandCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            
            if text.startswith('/'):
                effort_arg = re.match(r'^/effort\s+(\S*)$', text)
                if effort_arg is not None:
                    typed = effort_arg.group(1)
                    for value, label, is_current in _reasoning_options_for_completion():
                        if value.startswith(typed.lower()):
                            yield Completion(
                                value,
                                start_position=-len(typed),
                                display_meta="思考档位: " + label + (" (当前)" if is_current else ""),
                            )
                    return
                shell_arg = re.match(r'^/shell\s+(\S*)$', text)
                if shell_arg is not None:
                    typed = shell_arg.group(1)
                    for opt_id, label, available in _command_shell_options_for_completion():
                        if opt_id.startswith(typed.lower()):
                            yield Completion(
                                opt_id,
                                start_position=-len(typed),
                                display_meta="解释器: " + label + ("" if available else " (未安装)"),
                            )
                    return
                session_arg = re.match(r'^/sessions?\s+(\S*)$', text)
                if session_arg is not None:
                    typed = session_arg.group(1)
                    for sub_name, sub_desc in SESSION_SUBCOMMANDS:
                        if sub_name.startswith(typed.lower()):
                            yield Completion(
                                sub_name,
                                start_position=-len(typed),
                                display_meta="会话子命令: " + sub_desc,
                            )
                    return
                word = document.get_word_before_cursor(WORD=True)
                for cmd, desc in command_completions:
                    if cmd.startswith(word):
                        yield Completion(
                            cmd, 
                            start_position=-len(word),
                            display_meta=desc
                        )
                        
                try:
                    from core.skills import get_active_skills_list, skill_source_label
                    active_skills = get_active_skills_list()
                    for short_id, s in active_skills.items():
                        cmd = f"/{short_id}"
                        if cmd.startswith(word):
                            yield Completion(
                                cmd,
                                start_position=-len(word),
                                display_meta=f"⚡ {skill_source_label(s)}技能: {s['description']}"
                            )
                except Exception:
                    pass
                        
            match = re.search(r'@([^\s]*)$', text)
            if match:
                prefix = match.group(1)
                count = 0
                for rel_path in _list_workspace_files():
                    if prefix.lower() in rel_path.lower():
                        yield Completion(rel_path, start_position=-len(prefix))
                        count += 1
                    if count > 50:
                        break

    # 输入历史：跨会话持久化，Up/Down 可翻阅之前输入过的内容。
    # 数据目录统一走 config.YCODE_DIR（尊重 YCODE_DATA_DIR 覆盖），
    # 不能硬编码 ~/.ycode，否则多实例/测试隔离场景会把输入史写进真实用户目录。
    try:
        _hist_dir = config.YCODE_DIR
        os.makedirs(_hist_dir, exist_ok=True)
        _input_history = FileHistory(os.path.join(_hist_dir, "input_history.txt"))
    except Exception:
        _input_history = None

    input_area = TextArea(
        multiline=True,
        wrap_lines=True,
        height=D(min=1, max=5),
        style="class:input-field",
        focus_on_click=True,
        read_only=Condition(lambda: confirm_prompt is not None),
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        history=_input_history,
    )

    kb = KeyBindings()

    def _can_history_prev():
        if confirm_prompt is not None:
            return False
        buf = input_area.buffer
        return buf.document.cursor_position_row == 0 and not buf.complete_state

    def _can_history_next():
        if confirm_prompt is not None:
            return False
        buf = input_area.buffer
        doc = buf.document
        return doc.cursor_position_row == doc.line_count - 1 and not buf.complete_state

    @kb.add("up", filter=Condition(_can_history_prev))
    def history_prev(event):
        input_area.buffer.history_backward()
        event.app.invalidate()

    @kb.add("down", filter=Condition(_can_history_next))
    def history_next(event):
        input_area.buffer.history_forward()
        event.app.invalidate()

    @kb.add(Keys.BracketedPaste, eager=True)
    def paste(event):
        """粘贴大段文本：超阈值折叠为 [粘贴文本 #N · 共 M 行] 占位符，提交时展开"""
        pasted = normalize_pasted_text(event.data)
        event.current_buffer.insert_text(_paste_store.maybe_placeholderize(pasted))

    @kb.add("enter")
    def submit(event):
        """Enter 提交（如果是指令则立即在主线程同步执行，不阻塞队列；如果是普通对话则放入后台队列）"""
        global confirm_prompt
        if confirm_prompt:
            typed = _paste_store.expand(input_area.text.strip())
            _submit_confirm_input(
                typed,
                lambda: setattr(input_area, "text", ""),
                event.app.invalidate,
            )
            return
            
        display_text = input_area.text.strip()
        text = _paste_store.expand(display_text)
        if text:
            try:
                if display_text != text:
                    input_area.text = text
                input_area.buffer.append_to_history()
            except Exception:
                pass
            if text.startswith('/'):
                parts = text.split(maxsplit=1)
                cmd = parts[0]
                
                is_skill = False
                try:
                    from core.skills import get_active_skills_list
                    active_skills = get_active_skills_list()
                    skill_cmd = cmd[1:].lower()
                    if skill_cmd in active_skills:
                        is_skill = True
                except Exception:
                    pass
                    
                if is_skill:
                    was_queued = is_thinking
                    _echo_user_message(text, queued=was_queued)
                        
                    msg_queue.put({
                        "text": text,
                        "was_queued": was_queued,
                        "is_skill_call": True,
                        "session": current_session,
                    })
                    input_area.text = ""
                else:
                    input_area.text = ""
                    _echo_user_message(text)
                    try:
                        handle_slash_command(text)
                    except Exception as e:
                        t = _get_theme(current_theme)
                        print_formatted_text(
                            HTML(f"\n<style fg='{t['err']}'>⚠ 命令执行出错：{_esc(str(e))}</style>\n")
                        )
            else:
                was_queued = is_thinking
                _echo_user_message(text, queued=was_queued)
                
                msg_queue.put({
                    "text": text,
                    "was_queued": was_queued,
                    "session": current_session,
                })
                input_area.text = ""

    @kb.add("c-n")
    def newline(event):
        """Ctrl+N 换行"""
        event.current_buffer.insert_text("\n")

    @kb.add("c-c")
    def clear_input(event):
        """Ctrl+C: 如果星瑶正在执行，则中断执行；如果闲置，则清空输入框"""
        global is_thinking, confirm_prompt
        if (
            is_thinking
            or active_cancel_event is not None
            or confirm_prompt is not None
            or _goal_has_running()
        ):
            _cancel_active_turn()
            if _set_all_goal_cancel_events():
                print_formatted_text(HTML(
                    "<ansired>🎯 目标模式已收到停止信号，本轮结束后不再自动续跑。</ansired>"
                ))
            drained = 0
            try:
                while True:
                    msg_queue.get_nowait()
                    drained += 1
            except queue.Empty:
                pass
            if drained:
                print_formatted_text(HTML(
                    f"\n<ansired>🛑 收到中断信号，正在终止{_agent_name()}的执行...（已丢弃 {drained} 条排队消息）</ansired>"
                ))
            else:
                print_formatted_text(HTML(f"\n<ansired>🛑 收到中断信号，正在终止{_agent_name()}的执行...</ansired>"))
            if confirm_prompt is not None and not confirm_prompt.get("request_id"):
                options = confirm_prompt.get("options", [])
                cancel_index = len(options) - 1 if options else 0
                _resolve_confirm_prompt(cancel_index)
                event.app.invalidate()
        else:
            input_area.text = ""


    @kb.add("c-d")
    def exit_app(event):
        """Ctrl+D 退出"""
        event.app.exit()

    @kb.add("up", filter=Condition(lambda: confirm_prompt is not None))
    def confirm_arrow_up(event):
        global confirm_prompt
        # T2 修复：options 为空时取模会 ZeroDivisionError，必须防御
        if confirm_prompt and confirm_prompt.get('options'):
            confirm_prompt['current_idx'] = (confirm_prompt['current_idx'] - 1) % len(confirm_prompt['options'])
            event.app.invalidate()

    @kb.add("down", filter=Condition(lambda: confirm_prompt is not None))
    def confirm_arrow_down(event):
        global confirm_prompt
        # T2 修复：options 为空时取模会 ZeroDivisionError，必须防御
        if confirm_prompt and confirm_prompt.get('options'):
            confirm_prompt['current_idx'] = (confirm_prompt['current_idx'] + 1) % len(confirm_prompt['options'])
            event.app.invalidate()

    _broker_confirm_active = Condition(
        lambda: confirm_prompt is not None
        and bool(confirm_prompt.get("request_id"))
        and confirm_prompt.get("function_name") != "ask_user"
    )

    @kb.add("y", filter=_broker_confirm_active)
    def confirm_y(event):
        if _resolve_confirm_prompt(0):
            event.app.invalidate()

    @kb.add("n", filter=_broker_confirm_active)
    def confirm_n(event):
        if _resolve_confirm_prompt(1):
            event.app.invalidate()

    @kb.add("a", filter=_broker_confirm_active)
    def confirm_a(event):
        if confirm_prompt and len(confirm_prompt.get("options", [])) > 2:
            if _resolve_confirm_prompt(2):
                event.app.invalidate()

    @kb.add("c-t")
    def toggle_memory_summary(event):
        """Ctrl+T 切换记忆与任务拆解面板显示状态"""
        global show_memory_summary
        show_memory_summary = not show_memory_summary
        event.app.invalidate()

    @kb.add("s-tab")
    def toggle_permission_mode(event):
        """Shift+Tab 循环切换 星瑶Code 权限模式"""
        global current_permission_mode
        from core.permission_auto_decision import is_decision_model_enabled

        modes = (
            ["confirm", "accept_edits", "auto_decision", "plan", "full"]
            if is_decision_model_enabled()
            else ["confirm", "accept_edits", "plan", "full"]
        )
        try:
            curr_idx = modes.index(current_permission_mode)
        except ValueError:
            curr_idx = 0
        next_mode = _app_ctx.normalize_permission_mode(modes[(curr_idx + 1) % len(modes)])
        current_permission_mode = next_mode
        from config import save_config_value
        save_config_value("permission_mode", next_mode)
        event.app.invalidate()

    def get_status():
        """底部状态栏内容：克制单主色、四级上下文水位与动态运行标签。"""
        model_display = current_model_name or "未激活模型"

        usage_snapshot = get_token_usage_snapshot(
            current_session, current_model, current_url, "main"
        )
        process_usage = usage_snapshot["process"]
        latest_total = usage_snapshot["window"]["latest_total"]
        max_win = current_ctx_win

        cum_prompt = process_usage["cumulative_prompt"]
        cum_completion = process_usage["cumulative_completion"]
        cum_cached = process_usage["cumulative_cached"]

        global_rate = (cum_cached / cum_prompt * 100) if cum_prompt > 0 else None
        pct = (latest_total / max_win * 100) if max_win > 0 else 0.0

        def format_num(n):
            if n >= 1_000_000:
                val = n / 1_000_000
                return f"{int(val)}M" if val.is_integer() else f"{val:.1f}M"
            if n >= 1_000:
                val = n / 1_000
                return f"{int(val)}K" if val.is_integer() else f"{val:.1f}K"
            return str(n)

        prompt_str = format_num(cum_prompt)
        completion_str = format_num(cum_completion)
        cached_str = format_num(cum_cached)
        global_rate_str = f"{global_rate:.0f}%" if global_rate is not None else "--"
        latest_total_str = format_num(latest_total)
        max_win_str = format_num(max_win)

        token_flow = f"↑ {prompt_str} · ↓ {completion_str} · ⚡ {cached_str} ({global_rate_str})"
        ctx_bar = _context_bar_text(pct, width=6)
        ctx_text = f" {ctx_bar} {pct:.0f}% ({latest_total_str}/{max_win_str})"
        ctx_style = _context_watermark_style(pct)

        status_label, task_style = _compose_status_task_segment(
            task_status, _last_token_speed
        )

        if task_status != "idle":
            elapsed = time.time()
            frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            frame = frames[int(elapsed * 5) % len(frames)]
            task_info = f" {frame}{status_label}"
        else:
            task_info = status_label

        perm_style = "class:status.permission_confirm"
        perm_label = " 确认 "
        if current_permission_mode == "confirm":
            perm_label = " 确认 "
            perm_style = "class:status.permission_confirm"
        elif current_permission_mode == "accept_edits":
            perm_label = " 自动编辑 "
            perm_style = "class:status.permission_accept_edits"
        elif current_permission_mode == "auto_decision":
            perm_label = " 自动决策 "
            perm_style = "class:status.permission_auto_decision"
        elif current_permission_mode == "plan":
            perm_label = " 计划模式 "
            perm_style = "class:status.permission_plan"
        elif current_permission_mode == "full":
            perm_label = " 完全访问 "
            perm_style = "class:status.permission_full"

        segments = [
            ("class:status.model", f"   ✦ {model_display} "),
            ("class:status.sep",   "│"),
            (perm_style,           perm_label),
            ("class:status.sep",   "│ "),
            ("class:status.token", token_flow),
            (ctx_style,            ctx_text),
        ]
        try:
            q_size = msg_queue.qsize()
        except Exception:
            q_size = 0
        if q_size > 0:
            segments.extend([
                ("class:status.sep",  " │"),
                ("class:status.warn", f" 排队 {q_size} "),
            ])
        active_subs = _active_subagent_snapshots()
        if active_subs:
            segments.extend([
                ("class:status.sep",   " │"),
                ("class:status.model", f" 子代理 {len(active_subs)} "),
            ])
        if _goal_has_running():
            segments.extend([
                ("class:status.sep",   " │"),
                ("class:status.model", " 目标运行中 "),
            ])
        segments.extend([
            ("class:status.sep", " │"),
            (task_style,         task_info),
        ])
        return segments

    def get_activity_text():
        """渲染独立区域的执行中状态。"""
        if is_thinking:
            elapsed = max(0.0, time.time() - activity_start_time)
            frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            frame = frames[int(time.time() * 5) % len(frames)]
            if not _llm_engine_ready and _active_tool_status is None:
                return [
                    ("class:status.activity_spin", f"  {frame} "),
                    ("class:status.think", f"{_agent_name()}正在启动模型引擎"),
                    ("class:status.activity_meta", f" · {max(0.0, elapsed):.1f}s · Ctrl+C 中断"),
                ]
            raw = _format_activity_text(task_status, _active_tool_status, elapsed, frame)
            lines = raw.split("\n", 1)
            head = lines[0]
            stripped_head = head.lstrip()
            indent = head[: len(head) - len(stripped_head)]
            if stripped_head.startswith(frame + " "):
                rest = stripped_head[len(frame) + 1 :]
                frags = [
                    ("class:status.activity_spin", f"{indent}{frame} "),
                    ("class:status.think", rest),
                ]
            else:
                frags = [("class:status.think", head)]
            if len(lines) > 1 and lines[1]:
                frags.append(("class:status.activity_detail", f"\n{lines[1]}"))
            return frags
        return []

    @Condition
    def is_live_thinking_cond():
        return task_status == "thinking" or _thinking_panel_linger

    @Condition
    def is_working_cond():
        return is_thinking and task_status != "thinking" and not _thinking_panel_linger

    def get_thinking_cursor_position():
        text = _thinking_panel_text()
        lines = text.split("\n")
        return Point(x=len(lines[-1]), y=len(lines) - 1)

    def get_confirm_prompt_content():
        return HTML(_confirm_prompt_text(confirm_prompt))

    confirm_window = ConditionalContainer(
        content=_RoundedFrame(
            Window(
                content=FormattedTextControl(get_confirm_prompt_content),
                height=D(min=6, max=11),
                wrap_lines=True,
            ),
            title=lambda: _confirm_card_meta(confirm_prompt)[1],
            style="class:status.confirm-frame",
            border_style=lambda: _confirm_card_meta(confirm_prompt)[0],
        ),
        filter=Condition(lambda: confirm_prompt is not None),
    )

    def get_memory_summary_content():
        summary = manager.get_memory_summary(current_session) or "暂无会话记忆摘要。"

        from core.plan import get_default_plan_manager
        plan_text = _format_plan_items(get_default_plan_manager().snapshot())

        display_text = f"◈ [会话记忆摘要]\n  {summary}\n"
        if plan_text:
            display_text += "\n❖ [当前任务规划]\n" + plan_text
        else:
            display_text += "\n❖ [当前任务规划]\n  暂无计划步骤 (可在对话中输入以产生任务)"

        return display_text

    def get_live_plan_content():
        from core.plan import get_default_plan_manager

        return ANSI(_format_live_plan_items(get_default_plan_manager().snapshot()))

    def _has_active_plan_items() -> bool:
        from core.plan import TERMINAL_STATUSES
        items = get_default_plan_manager().snapshot()
        if not items:
            return False
        return any(item.get("status") not in TERMINAL_STATUSES for item in items)

    live_plan_window = ConditionalContainer(
        content=_RoundedFrame(
            Window(
                content=FormattedTextControl(get_live_plan_content),
                height=D(min=2, max=10),
                wrap_lines=True,
            ),
            title="❖ 当前任务计划",
            style="class:status.live-plan-frame",
            border_style="class:frame.dim",
        ),
        filter=Condition(
            lambda: _has_active_plan_items()
            and (
                task_status != "idle"
                or any(
                    item.get("status") == "in_progress"
                    for item in get_default_plan_manager().snapshot()
                )
            )
        ),
    )

    memory_window = ConditionalContainer(
        content=_RoundedFrame(
            Window(
                content=FormattedTextControl(get_memory_summary_content),
                height=6,
                wrap_lines=True,
            ),
            title="◈ 会话记忆与任务拆解 (Ctrl+T 折叠/展开)",
            style="class:status.memory-frame",
            border_style="class:frame.dim",
        ),
        filter=Condition(lambda: show_memory_summary),
    )

    def get_subagent_panel_content():
        active = _active_subagent_snapshots()
        if not active:
            return []
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spinner = frames[int(time.time() * 5) % len(frames)]
        fragments = []
        for idx, s in enumerate(active[:4]):
            if idx > 0:
                fragments.append(("", "\n"))
            stype = str(getattr(s, "subagent_type", "") or "agent")
            stage = str(getattr(s, "stage", "") or "")
            desc = _truncate_visible(str(getattr(s, "description", "") or ""), 44)
            elapsed_s = float(getattr(s, "execution_elapsed", 0.0) or 0.0)
            badge = "后台" if getattr(s, "run_in_background", False) else "并发"
            stage_part = f" · {stage}" if stage else ""
            fragments.append(("class:status.subagent_spin", f"  {spinner} "))
            fragments.append(("class:status.subagent_type", f"[{stype}] "))
            fragments.append(("class:status.subagent_text", f"{desc} "))
            fragments.append(("class:status.subagent_meta", f"({badge}{stage_part} · {elapsed_s:.1f}s)"))
        if len(active) > 4:
            fragments.append(("", "\n"))
            fragments.append(("class:status.subagent_meta", f"  … 还有 {len(active) - 4} 个并行子代理"))
        return fragments

    subagent_window = ConditionalContainer(
        content=_RoundedFrame(
            Window(
                content=FormattedTextControl(get_subagent_panel_content),
                height=D(min=1, max=5),
                wrap_lines=True,
            ),
            title=lambda: f"◈ 并发子代理 ({len(_active_subagent_snapshots())})",
            style="class:status.subagent-frame",
            border_style="class:frame.dim",
        ),
        filter=Condition(lambda: bool(_active_subagent_snapshots())),
    )

    def get_input_prefix():
        if confirm_prompt is not None:
            return [("class:input-prefix-wait", "  ? ")]
        return [("class:input-prefix", "  ❯ ")]

    root_container = HSplit([
        confirm_window,
        live_plan_window,
        memory_window,
        ConditionalContainer(
            content=_RoundedFrame(
                Window(
                    content=FormattedTextControl(
                        _thinking_panel_text,
                        get_cursor_position=get_thinking_cursor_position,
                    ),
                    height=D(min=2, max=8),
                    wrap_lines=True,
                    always_hide_cursor=True,
                    style="class:status.thinking-content",
                ),
                title=lambda: f"◌ {_agent_name()} 思考轨迹 · {max(0.0, time.time() - think_start_time):.1f}s",
                style="class:status.thinking-frame",
                border_style="class:frame.dim",
            ),
            filter=is_live_thinking_cond,
        ),
        ConditionalContainer(
            content=Window(
                content=FormattedTextControl(get_activity_text),
                height=2,
                style="class:status.think",
            ),
            filter=is_working_cond,
        ),
        subagent_window,
        Window(height=D(min=0, weight=100)),
        Window(
            content=FormattedTextControl(
                lambda: [("class:input-line", "  " + ("─" * max(16, _terminal_width() - 4)) + "\n")]
            ),
            height=1,
            style="class:input-line",
        ),
        VSplit([
            Window(
                content=FormattedTextControl(get_input_prefix),
                width=D.exact(4),
                style="class:input-field",
            ),
            input_area,
            Window(width=D.exact(2)),
        ]),
        Window(
            content=FormattedTextControl(
                lambda: [("class:input-line", "  " + ("─" * max(16, _terminal_width() - 4)) + "\n")]
            ),
            height=1,
            style="class:input-line",
        ),
        Window(
            content=FormattedTextControl(get_status),
            height=1,
        ),
    ])
    
    main_container = FloatContainer(
        content=root_container,
        floats=[
            Float(
                xcursor=True,
                ycursor=True,
                content=CompletionsMenu(max_height=10, scroll_offset=1)
            )
        ]
    )

    layout = Layout(main_container)

    style = DynamicStyle(lambda: _build_cli_style(current_theme))

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
        mouse_support=False,
        erase_when_done=True,
    )

    return app


_CLI_STYLE_CACHE = {}


def _build_cli_style(theme_name):
    cached = _CLI_STYLE_CACHE.get(theme_name)
    if cached is not None:
        return cached
    t = _get_theme(theme_name)

    style = Style.from_dict({
        "input-frame":            "",
        "input-line":             t["border_dim"],
        "input-prefix":           f"bold {t['accent']}",
        "input-prefix-wait":      f"bold {t['warn']}",
        "input-field":            t["text_input"],

        "frame.border":           t["border"],
        "frame.dim":              t["border_dim"],
        "frame.warn":             t["warn"],
        "frame.err":              t["err"],
        "frame.label":            t["label"],
        "status.memory-frame":    t["text_soft"],
        "status.live-plan-frame": t["text_soft"],
        "status.thinking-frame":  t["text_muted"],
        "status.thinking-content": t["text_soft"],
        "status.subagent-frame":  t["text_soft"],
        "status.confirm-frame":   f"bold {t['accent']}",

        "status.model":           t["status_main"],
        "status.sep":             t["status_sep"],
        "status.token":           t["text_muted"],
        "status.ctx_warn":        t["warn"],
        "status.ctx_high":        t["err"],
        "status.ctx_crit":        f"bold {t['err']}",
        "status.warn":            f"bold {t['warn']}",
        "status.think":           f"bold {t['text']}",
        "status.activity_spin":   f"bold {t['accent']}",
        "status.activity_meta":   t["text_muted"],
        "status.activity_detail": t["accent_soft"],
        "status.subagent_spin":   f"bold {t['accent']}",
        "status.subagent_type":   t["accent_soft"],
        "status.subagent_text":   t["text"],
        "status.subagent_meta":   t["text_muted"],
        "status.task_active":     f"bold {t['warn']}",
        "status.task_idle":       t["status_sep"],
        "status.task_speed":      f"bold {t['accent']}",

        # 权限档位标识（低饱和信号/主题色，避免霓虹撞色）
        "status.permission_confirm":       f"bold {t['ok']}",
        "status.permission_accept_edits":  f"bold {t['accent_soft']}",
        "status.permission_auto_decision": f"bold {t['accent']}",
        "status.permission_plan":          f"bold {t['warn']}",
        "status.permission_full":          f"bold {t['err']}",

        "completion-menu":                        f"bg:{t['menu_bg']} {t['menu_text']}",
        "completion-menu.completion":             f"bg:{t['menu_bg']} {t['menu_text']}",
        "completion-menu.completion.current":     t["menu_current"],
        "completion-menu.meta.completion":        f"bg:{t['menu_bg']} {t['menu_meta']}",
        "completion-menu.meta.completion.current": t["menu_current_meta"],
        "scrollbar.background":                   f"bg:{t['surface_bg']}",
        "scrollbar.button":                       f"bg:{t['border_dim']}",
    })
    _CLI_STYLE_CACHE[theme_name] = style
    return style


_evict_oldest_turn = _agent_service.evict_oldest_turn


def _report_task_result(task_data, ok, error=None):
    if not isinstance(task_data, dict):
        return
    result = task_data.get("result")
    if isinstance(result, dict):
        result["ok"] = bool(ok)
        if error:
            result["error"] = str(error)


def _background_worker():
    global active_cancel_event, active_turn_id
    while True:
        task_data = msg_queue.get()
        task_session = None
        try:
            def mark_turn_initializing():
                global is_thinking, task_status, think_start_time, active_thinking_text
                global activity_start_time
                _clear_active_thinking()
                active_thinking_text = "正在准备请求..."
                is_thinking = True
                task_status = "executing"
                think_start_time = time.time()
                activity_start_time = time.time()
                if app_instance is not None:
                    app_instance.invalidate()

            schedule_ui(mark_turn_initializing)

            # 尽早发布取消事件：让 Ctrl+C 在“准备请求”阶段（读历史、压缩记忆等
            # 耗时步骤）也能生效。真正的 turn cancel_event 建立后会继承该状态。
            # M36 修复：赋值必须保持在内层 try 块内（本 try 覆盖整个准备阶段），
            # 外层 except 会显式置 None 清理——否则准备阶段抛异常时该残留 event
            # 会让下一次 Ctrl+C 误判"执行中"，清空整个消息队列。
            early_cancel = threading.Event()
            active_cancel_event = early_cancel

            if not isinstance(task_data, dict):
                logging.getLogger("py.warnings").warning(
                    "background worker received non-dict task_data: %s", type(task_data).__name__
                )
                schedule_ui(lambda: print_formatted_text(
                    HTML("\n<ansired>! 后台任务执行失败：任务格式异常，已跳过。</ansired>\n")
                ))
                continue

            user_input = task_data.get("text", "")
            if not isinstance(user_input, str):
                user_input = str(user_input) if user_input is not None else ""
            was_queued = task_data.get("was_queued", False)
            is_skill_call = task_data.get("is_skill_call", False)
            is_internal = task_data.get("is_internal", False)
            if not user_input.strip():
                # 空任务：调度到 UI 提示并跳过，避免后续空转或 400
                logging.getLogger("py.warnings").warning(
                    "background worker received empty task_data: %s",
                    {k: type(v).__name__ for k, v in task_data.items()},
                )
                schedule_ui(lambda: print_formatted_text(
                    HTML("\n<ansired>! 后台任务执行失败：收到空任务，已跳过。</ansired>\n")
                ))
                _report_task_result(task_data, False, "收到空任务")
                continue

            task_session = task_data.get("session") or current_session

            if is_internal and user_input == "__ycode_internal__:summarize_background_tasks":
                # 打印系统触发提示（不是 "❯ 你"，避免混淆）
                schedule_ui(lambda: _print_ui_html(
                    f"\n  <style fg='{_get_theme(current_theme)['accent']}'>◈ 所有后台子代理任务已结束，正在自动汇总结果...</style>\n"
                ))
                user_input = "请汇总所有后台子代理任务的结果并告诉我。如果某些任务失败了，请用你自身的工具（如 search_news / fetch_url 等）接手完成，然后一并汇总。"
                was_queued = False
                is_skill_call = False
            else:
                try:
                    _bg_summary_triggered.discard(task_session)
                except Exception:
                    pass

            if was_queued:
                # 先按原始文本截断再转义，避免把 &lt; 之类的 HTML 实体从中间切断
                display_input = user_input[:50] + "..." if len(user_input) > 50 else user_input
                display_input = _esc(display_input)
                schedule_ui(lambda text=display_input: _print_ui_html(
                    f"  <style fg='{_get_theme(current_theme)['accent']}'><b>▶ [开始执行排队任务]</b></style> "
                    f"<style fg='{_get_theme(current_theme)['text_muted']}'>{text}</style>"
                ))

            from core.model_manager import get_active_model
            active_info = get_active_model()
            if not active_info or not active_info.get("litellm_model"):
                _report_task_result(task_data, False, "未配置或激活任何大模型")
                schedule_ui(lambda: _print_ui_html(
                    f"  <style fg='{_get_theme(current_theme)['err']}'>"
                    "! 您当前未配置或激活任何大模型！请输入 /models 添加并激活模型后再进行对话。"
                    "</style>"
                ))
                continue

            sync_plan_session(task_session)
            task_model = active_info["litellm_model"]
            task_key = active_info.get("api_key", "")
            task_url = active_info.get("base_url", "")
            task_ctx_win = active_info.get("context_window", 128000)
            # 本回合实际发请求所用的模型 id（默认=激活模型）。router 场景可能一次性
            # 换成副模型 id；必须与 task_model 成对替换——llm_dialects 按 id 读
            # thinking / max_tokens 等模型级配置，只换名字不换 id 会用错推理参数。
            task_model_id = active_info.get("id")
            try:
                from core.turn_model_override import router_scene_live, resolve_turn_model

                if router_scene_live():
                    from core.agent_service import get_session_token_baseline

                    try:
                        baseline_tokens = get_session_token_baseline(
                            manager, task_session, task_model, api_base=task_url
                        )
                    except Exception:
                        baseline_tokens = 0
                    turn_override = resolve_turn_model(
                        active_info,
                        turn_source="local",
                        pending_model_id="",
                        baseline_tokens=baseline_tokens,
                        user_text=user_input,
                    )
                    if turn_override:
                        logging.getLogger("py.warnings").info(
                            "router 场景覆盖本回合模型 [%s]：%s → %s（基线 %s tokens，副模型窗口 %s tokens）",
                            task_session,
                            task_model,
                            turn_override["litellm_model"],
                            baseline_tokens,
                            turn_override["context_window"],
                        )
                        task_model = turn_override["litellm_model"]
                        task_key = turn_override["api_key"]
                        task_url = turn_override["base_url"]
                        task_ctx_win = turn_override["context_window"]
                        task_model_id = turn_override["model_id"]
            except Exception:
                logging.getLogger("py.warnings").debug(
                    "router 场景模型档位覆盖失败，本回合沿用主模型", exc_info=True
                )

            from core.input_parser import parse_user_input, extract_session_refs
            if is_skill_call:
                session_refs = []
                parts = user_input.split(maxsplit=1)
                skill_name = parts[0][1:]
                actual_query = parts[1] if len(parts) > 1 else "请启动并运行该技能。"

                schedule_ui(lambda name=_esc(skill_name): _print_ui_html(
                    f"\n  <style fg='{_get_theme(current_theme)['accent']}'><b>⚡ [主动调用技能: {name}]</b></style>"
                ))

                parsed_content, attached_msgs = parse_user_input(actual_query)
                instruction = f"【指示：用户已明确指定并主动调用技能【{skill_name}】来处理以下提问。请务必优先执行该技能指令并做出响应。】\n提问："
                if isinstance(parsed_content, list):
                    for item in parsed_content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            item["text"] = instruction + item.get("text", "")
                            break
                else:
                    parsed_content = instruction + parsed_content
            else:
                # /paste-image 等来源会在队列条目携带显式附件路径（用户明确授权，
                # 不受沙盒越界限制），经 parse_user_input 的 explicit_paths 通道
                # 挂载——不依赖文本内路径提取，含空格路径也不会丢（审查 Important #4）
                # @{会话名} 提取与 @文件解析同一 try：任一步骤异常都退回纯文本，
                # 绝不让后台 worker 崩溃（与 GUI 侧解析段的异常兜底对齐）
                try:
                    explicit_paths = task_data.get("explicit_paths") or None
                    _, session_refs = extract_session_refs(user_input)
                    parsed_content, attached_msgs = parse_user_input(user_input, explicit_paths=explicit_paths)
                except Exception:
                    logging.getLogger("py.warnings").exception(
                        "解析用户输入失败，退回纯文本"
                    )
                    session_refs = []
                    parsed_content, attached_msgs = user_input, []
                for msg in attached_msgs:
                    schedule_ui(lambda message=_esc(msg): _print_ui_html(
                        f"    <style fg='{_get_theme(current_theme)['ok']}'>{message}</style>"
                    ))
                if session_refs:
                    for name in session_refs:
                        schedule_ui(lambda n=_esc(name): _print_ui_html(
                            f"    <style fg='{_get_theme(current_theme)['accent_soft']}'>◈ 已引用对话: {n}</style>"
                        ))

            from core.turn_undo import service as _tu_service
            from core.workspace_context import get_workdir

            turn_id = uuid.uuid4().hex
            try:
                manager.ensure_session(task_session)
                _tu_checkpoint, _tu_undo_ctx = _tu_service.begin_turn(
                    session_id=_tu_service.resolve_session_id(task_session),
                    turn_id=turn_id,
                    workspace_root=get_workdir(),
                    description=str(user_input)[:80],
                )
            except Exception:
                import logging as _logging_mod

                _logging_mod.getLogger("py.warnings").exception(
                    "创建按回合撤销检查点失败，回合中止 session=%s", task_session
                )
                _report_task_result(task_data, False, "回合检查点创建失败，本轮未开始")
                continue
            try:
                manager.add_message(
                    task_session, "user", parsed_content,
                    turn_checkpoint_id=_tu_checkpoint.checkpoint_id,
                    turn_position_kind="user",
                )
            except Exception:
                from core.turn_undo import service as _tu_service_inner

                try:
                    _tu_service_inner.finish_turn(_tu_undo_ctx, aborted=True, reason="用户消息落库失败")
                except Exception:
                    pass
                raise

            def _turn_notify(kind, message):
                schedule_ui(lambda text=message: print_formatted_text(HTML(text)))

            try:
                from core.idle_maintenance import idle_maintenance_service
                idle_maintenance_service.on_turn_started(task_session)
            except Exception:
                pass

            try:
                history = _agent_service.prepare_turn_messages(
                    manager,
                    task_session,
                    task_model,
                    task_ctx_win,
                    permission_mode=current_permission_mode,
                    cancel_event=early_cancel,
                    notify=_turn_notify,
                    api_key=task_key,
                    api_base=task_url,
                    compress_threshold=MEMORY_COMPRESS_THRESHOLD,
                    referenced_sessions=session_refs,
                )
            except Exception:
                # 回合在进入下方 try/finally 之前早退：on_turn_started 已置位的
                # 闲时维护忙位必须在这里收口，否则该会话的闲时任务在到期时
                # 被忙闲复查跳过，直到下一个成功回合才自愈
                try:
                    idle_maintenance_service.on_turn_finished(task_session, notify=None)
                except Exception:
                    pass
                raise


            turn_controller = TurnController(history)
            turn_controller.begin_turn()
            history = turn_controller.messages
            cancel_event = turn_controller.cancel_event
            if early_cancel.is_set():
                cancel_event.set()
            active_cancel_event = cancel_event
            active_turn_id = turn_id
            from core import tool_cache as _tool_cache
            _tool_cache.set_current_session(task_session)
            _prev_thread_session = _app_ctx.get_thread_session()
            _app_ctx.set_thread_session(task_session)
            # 将 undo 状态绑定到当前会话，防止切会话后 /undo 误操作其他会话的文件
            def mark_turn_started():
                global is_thinking, think_start_time, task_status, activity_start_time
                _clear_active_thinking()
                is_thinking = True
                task_status = "executing"
                think_start_time = time.time()
                activity_start_time = time.time()
                if app_instance:
                    app_instance.invalidate()

            turn_interrupted = False
            turn_error = None
            try:
                schedule_ui(mark_turn_started)

                try:
                    response_gen = chat_with_model_events(
                        task_model,
                        task_key,
                        history,
                        task_url,
                        persist=True,
                        turn_id=turn_id,
                        cancel_event=cancel_event,
                        parent_session=task_session,
                        parent_turn_id=turn_id,
                        permission_owner=turn_id,
                        permission_source="主代理",
                        model_id=task_model_id,
                    )
                    _forward_response_events(response_gen)
                    turn_controller.complete()
                    try:
                        manager.mark_turn_final_assistant(_tu_checkpoint.checkpoint_id)
                    except Exception:
                        pass
                    closed_status = _tu_service.finish_turn(_tu_undo_ctx)
                    if closed_status and closed_status.value == "ready":
                        schedule_ui(lambda s=task_session, t=turn_id:
                                    print_turn_file_summary_if_ready(s, t))
                    schedule_ui(_reset_turn_state)
                except KeyboardInterrupt:
                    turn_controller.cancel()
                    _tu_service.finish_turn(_tu_undo_ctx, aborted=True, reason="回合被用户取消")
                    schedule_ui(_reset_turn_state)
                    turn_interrupted = True
                    turn_error = "已被用户取消"
                except Exception as e:
                    turn_controller.cancel()
                    _tu_service.finish_turn(_tu_undo_ctx, aborted=True, reason=f"回合异常: {type(e).__name__}")
                    # 必须记录到 runtime-warnings.log，否则异常被静默吞掉，
                    # 用户消息已入库但 assistant 回复缺失，无法排查（session 97 类问题）
                    import logging as _logging_mod
                    _logging_mod.getLogger("py.warnings").exception(
                        "LLM 调用失败 session=%s turn=%s: %s",
                        task_session, turn_id, e,
                    )
                    try:
                        failure_msg = f"[回复生成失败] {type(e).__name__}: {e}"
                        manager.add_message(task_session, "assistant", failure_msg)
                    except Exception:
                        pass
                    schedule_ui(lambda message=_esc(str(e)): print_formatted_text(
                        HTML(f"\n<ansired>⚠️ 连接或回复生成失败: {message}</ansired>\n")
                    ))
                    schedule_ui(_reset_turn_state)
                    turn_interrupted = True
                    turn_error = f"{type(e).__name__}: {e}"
            finally:
                active_cancel_event = None
                active_turn_id = None
                global is_thinking, task_status
                is_thinking = False
                task_status = "idle"
                _clear_active_thinking()
                try:
                    idle_maintenance_service.on_turn_finished(task_session, notify=_turn_notify)
                except Exception:
                    pass
                from core import tool_cache as _tool_cache
                _tool_cache.set_current_session(None)
                _app_ctx.set_thread_session(_prev_thread_session)
                # 清理卡在 in_progress 的计划项：子 agent 异常退出（超时/崩溃/未调用 terminal_callback）
                # 会导致计划项永久卡在 in_progress，turn 结束后仍显示在状态栏。
                # 但必须跳过"后台子代理仍在运行"的 in_progress 项——否则会误杀正常后台任务
                # （用户输入新对话触发 turn 结束清理时，后台任务还在跑）。
                try:
                    from core.plan import get_default_plan_manager, TERMINAL_STATUSES
                    from core.tools import subagent_manager
                    plan_mgr = get_default_plan_manager()
                    stuck_items = [
                        item for item in plan_mgr.snapshot()
                        if item.get("status") == "in_progress"
                        and not subagent_manager.is_plan_item_running(item.get("id", ""))
                    ]
                    if stuck_items:
                        from core.task_scheduler import get_default_scheduler
                        for item in stuck_items:
                            get_default_scheduler().fail(
                                item["id"],
                                f"turn 结束时仍处于 in_progress 且无运行中子代理（子 agent 可能异常退出或未回调终止状态）",
                            )
                    # 自动归档：所有计划项都已进入终态时清空 plan_manager，
                    # 避免上一轮已完成的计划跨越多轮对话仍残留在状态栏（用户问"你能做什么"等无关问题时尤其明显）。
                    # 必须放在 in_progress 项被标记为 failed 之后，否则会漏判。
                    # 归档只清空内存与磁盘计划文件，不影响会话历史中的对话记录。
                    # 注意：仍然后台子代理在运行时不要归档（否则计划消失后回调写不回去）。
                    items = plan_mgr.snapshot()
                    has_running_bg = any(
                        subagent_manager.is_plan_item_running(item.get("id", ""))
                        for item in items
                        if item.get("status") == "in_progress"
                    )
                    if (
                        items
                        and not has_running_bg
                        and all(item.get("status") in TERMINAL_STATUSES for item in items)
                    ):
                        plan_mgr.clear()
                except Exception:
                    pass

            if not turn_interrupted:
                _report_task_result(task_data, True)
                if re.match(r"^session_[a-f0-9]{6}$", task_session):
                    history = manager.get_history(task_session)
                    user_msgs = [m for m in history if m.get("role") == "user"]
                    if user_msgs:
                        first_user_query = user_msgs[0].get("content", "")
                        if first_user_query:
                            trigger_session_auto_rename(
                                task_session,
                                first_user_query,
                                task_model,
                                task_key,
                                task_url
                            )
            else:
                _report_task_result(task_data, False, turn_error or "任务被取消或回复生成失败")
        except Exception as e:
            # 兜底防线：parse/add_message/prune/压缩/提示词组装等任何未预期异常
            # 都不允许杀死 worker 线程，否则之后所有用户输入会被静默吞掉、UI 卡死
            # M36 修复：准备阶段（early_cancel 建立后、turn try/finally 之前）的异常
            # 不会经过内层 finally，active_cancel_event 会残留非 None，导致后续
            # Ctrl+C 误判“执行中”而清空整个消息队列——这里显式置 None 清理。
            active_cancel_event = None
            active_turn_id = None
            logging.getLogger("py.warnings").exception("background worker turn failed: %s", e)
            _report_task_result(task_data, False, f"{type(e).__name__}: {e}")
            # 持久化兜底异常的失败标记，确保会话历史完整可排查
            try:
                if task_session:
                    failure_msg = f"[后台任务异常] {type(e).__name__}: {e}"
                    manager.add_message(task_session, "assistant", failure_msg)
            except Exception:
                pass
            schedule_ui(lambda message=_esc(str(e)): print_formatted_text(
                HTML(f"\n<ansired>⚠️ 后台任务执行失败: {message}</ansired>\n")
            ))
            schedule_ui(_reset_turn_state)
        finally:
            msg_queue.task_done()


def run_headless(prompt, session_name=None) -> int:
    global current_session, current_model, current_key, current_url, current_ctx_win

    bind_default_plan_session(current_session)
    # headless 模式：不在 CLI 中与用户交互，自动拒绝所有权限请求，避免 worker 永久挂起
    def _deny_permission_request(request):
        try:
            permission_broker.resolve(request.request_id, 1)
        except Exception:
            pass

    permission_broker.set_prompt_handlers(
        _deny_permission_request,
        lambda: None,
    )

    set_terminal_title("星瑶Code (headless)")

    if prompt == "-":
        import sys as _sys
        prompt = _sys.stdin.read().strip()
        if not prompt:
            print("错误：从 stdin 读取的内容为空", file=_sys.stderr)
            return 2
    elif not prompt.strip():
        print("错误：prompt 不能为空", file=sys.stderr)
        return 2

    if session_name:
        current_session = session_name
        sync_plan_session(current_session)

    worker_thread = threading.Thread(target=_background_worker, daemon=True)
    worker_thread.start()

    done_event = threading.Event()
    task_data = {
        "text": prompt,
        "was_queued": False,
        "is_skill_call": False,
        "session": current_session,
        # High 8：worker 通过该共享对象上报成败，失败时返回退出码 1
        "result": {},
    }
    msg_queue.put(task_data)

    try:
        # Queue.join() 没有 timeout 参数；改用轮询，设 30 分钟硬超时，
        # 防止 worker 意外崩溃漏调 task_done() 导致 headless 进程永久挂起。
        deadline = time.monotonic() + 1800
        while msg_queue.unfinished_tasks > 0:
            if time.monotonic() > deadline:
                if active_cancel_event is not None:
                    active_cancel_event.set()
                    grace_deadline = time.monotonic() + 5
                    while msg_queue.unfinished_tasks > 0 and time.monotonic() < grace_deadline:
                        time.sleep(0.1)
                print("\n[headless 任务超时（30 分钟），强制退出]", file=sys.stderr)
                return 1
            time.sleep(0.1)
        time.sleep(0.5)

        # headless 没有 UI 消费者：回合失败只以 turn_error 事件留在
        # ui_event_queue 里（交互模式由 UI 线程 drain 后打印），而 worker 侧
        # 回合已"完整走完"会上报成功。必须在投递方这里排空队列检出失败，
        # 否则失败任务也返回退出码 0，CI/脚本无法判败。
        stream_error = None
        while True:
            try:
                _queued_event = ui_event_queue.get_nowait()
            except queue.Empty:
                break
            if _queued_event.kind == "turn_error" and stream_error is None:
                stream_error = str((_queued_event.payload or {}).get("message") or "未知错误")
        if stream_error is not None:
            print(f"\n[headless 任务失败: {stream_error}]", file=sys.stderr)
            return 1

        # High 8：失败与成功在队列层无法区分（task_done 在 finally 无条件调用），
        # 必须看 worker 通过共享 result 对象上报的成败；失败返回 1 让 CI 流水线判败。
        task_result = task_data.get("result") or {}
        if not task_result.get("ok"):
            detail = task_result.get("error") or "任务未成功完成"
            print(f"\n[headless 任务失败: {detail}]", file=sys.stderr)
            return 1

        try:
            history = manager.get_history(current_session)
            # High 8：过滤 worker 持久化的失败标记消息，避免被当作正常回复打印
            assistant_messages = [
                msg for msg in history
                if isinstance(msg, dict) and msg.get("role") == "assistant"
                and not (
                    isinstance(msg.get("content"), str)
                    and msg["content"].startswith(("[回复生成失败]", "[后台任务异常]"))
                )
            ]
            if assistant_messages:
                last = assistant_messages[-1]
                content = last.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        str(item.get("text", item)) for item in content
                    )
                if isinstance(content, str) and content.strip():
                    print("\n" + content.strip())
        except Exception as e:
            print(f"\n[headless 获取回复失败: {e}]", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        print("\n[已取消]", file=sys.stderr)
        return 130

    return 0


def run_cli():
    """主入口 — 封装在 while 循环里，支持弹出全屏选择器后自动重启"""
    global app_instance, pending_action, current_session
    global current_model, current_key, current_url, current_ctx_win, current_model_name

    # 交互式前置检查：非 TTY（管道/重定向）快速失败并引导 headless -p，
    # 必须先于下方一切初始化副作用（模型选择器/后台线程/MCP 预热/首绘）
    _require_interactive_console()

    _ensure_cli_disclaimer_consent()

    _app_ctx.set_ui_flavor("cli")
    bind_default_plan_session(current_session)
    permission_broker.set_prompt_handlers(
        _activate_permission_request,
        _dismiss_permission_prompt,
    )

    set_terminal_title("星瑶Code")

    active_info = get_active_model()
    if not active_info:
        print("\n🔑 欢迎使用 星瑶Code！检测到您尚未配置大模型。")
        print("💡 正在自动为您开启模型管理界面，请选择厂商并配置 API Key...")
        time.sleep(1.2)
        from core.model_selector import open_model_selector
        def _on_init_selected(info: dict):
            global current_model, current_key, current_url, current_ctx_win, current_model_name
            current_model      = info["litellm_model"]
            current_model_name = info["name"]
            current_key        = info["api_key"]
            current_url        = info["base_url"]
            current_ctx_win    = info["context_window"]
            _sync_active_model_context()
        open_model_selector(on_model_selected=_on_init_selected)
        active_info = get_active_model()
        if active_info:
            current_model      = active_info["litellm_model"]
            current_model_name = active_info["name"]
            current_key        = active_info["api_key"]
            current_url        = active_info["base_url"]
            current_ctx_win    = active_info["context_window"]
            _sync_active_model_context()
            print(f"\n✔ 已成功配置并激活模型：{current_model_name}")

    elif not active_info.get("api_key"):
        model_label = active_info["name"]
        provider_label = active_info.get("provider_name", "未配置厂商")
        protocol_label = active_info.get("protocol", "openai")
        ctx_label = active_info.get("context_window", 128000)
        print("\n🔑 检测到激活模型尚未配置 API 密钥。")
        print(f"当前激活模型：{model_label}")
        print(f"接口协议：{protocol_label} | 厂商：{provider_label} | 上下文支持：{ctx_label:,}")
        print("请在下方输入您的 API 密钥 (API Key)：")
        try:
            key_input = input("👉 API Key: ").strip()
            if key_input:
                from core.provider_keys import split_provider_keys
                _parsed_keys = split_provider_keys(key_input)
                current_key = _parsed_keys[0] if _parsed_keys else key_input
                _sync_active_model_context()
                from core.model_manager import load_config, _write_config
                cfg = load_config()
                active_id = cfg.get("active_model_id")
                target_provider = None
                for provider in cfg.get("providers", []):
                    if any(m.get("id") == active_id for m in provider.get("models", [])):
                        target_provider = provider
                        break
                if target_provider is None and cfg.get("providers"):
                    target_provider = cfg["providers"][0]
                if target_provider is not None:
                    target_provider["api_key"] = key_input
                    _write_config(cfg)
                    print("✔ 密钥已成功保存到配置文件中！")
                else:
                    print("⚠️ 配置中没有任何厂商，密钥仅对本次运行生效。")
            else:
                print("⚠️ 未输入密钥，系统将使用空密钥启动。")
        except (KeyboardInterrupt, EOFError):
            print("\n👋 启动已取消。")
            sys.exit(0)

    worker_thread = threading.Thread(target=_background_worker, daemon=True)
    worker_thread.start()

    try:
        from core.turn_undo import service as _tu_service_cli
        from core.turn_undo.models import TurnUndoStatus as _TUS

        _interrupted_cps = _tu_service_cli.get_default_storage().list_checkpoints_by_status(
            (_TUS.INTERRUPTED, _TUS.RECOVERING)
        )
        if _interrupted_cps:
            print_formatted_text(HTML(
                "<ansiyellow>⚠️ 检测到未完成的撤销事务：相关文件已被冻结保护"
                "（星瑶的文件工具与命令在这些路径上暂不可用）。</ansiyellow>"
            ))
            for _cp in _interrupted_cps:
                print_formatted_text(HTML(
                    f"  回合 {_esc(_cp.turn_id[:8])}  "
                    f"<ansigray>{_esc(_cp.last_error_message or '撤销中断')}</ansigray>"
                ))
            print_formatted_text(HTML(
                "  处理方式：/undo list 查看状态，/undo recover &lt;操作ID&gt; 选择"
                "继续撤销或取消撤销。\n"
            ))
    except Exception:
        pass

    def _preload_llm_engine():
        global _llm_engine_ready
        try:
            from core.llm import _token_counter, _supports_vision
            try:
                _token_counter(
                    current_model or "openai/gpt-4o-mini",
                    [{"role": "user", "content": "预热"}],
                )
            except Exception:
                pass
            try:
                if current_model:
                    _supports_vision(current_model)
            except Exception:
                pass
        except Exception as e:
            logging.getLogger("py.warnings").warning("预加载 LLM 引擎失败: %s", e)
        finally:
            _llm_engine_ready = True

    def _mcp_status_to_ui(message):
        if app_instance is None:
            logging.getLogger("py.warnings").info("MCP 状态: %s", message)
        else:
            schedule_ui(lambda text=message: print_formatted_text(text))

    def _preload_mcp():
        try:
            import json as _json
            from config import MCP_FILE as _mcp_file
            try:
                with open(_mcp_file, "r", encoding="utf-8") as _f:
                    _servers = _json.load(_f).get("mcpServers", {})
            except Exception:
                _servers = {}
            if not any(
                not (isinstance(c, dict) and c.get("enabled", True) is False)
                for c in _servers.values()
            ):
                return
        except Exception:
            pass
        try:
            from core.mcp_client import mcp_manager, set_status_logger as _set_mcp_status_logger
            _set_mcp_status_logger(_mcp_status_to_ui)
            mcp_manager._ensure_connected()
        except Exception as e:
            logging.getLogger("py.warnings").warning("MCP 后台连接失败: %s", e)

    def _warm_token_usage():
        try:
            recalc_token_usage()
        except Exception as e:
            logging.getLogger("py.warnings").warning("启动预热 recalc_token_usage 失败: %s", e)

    _preload_llm_thread = threading.Thread(target=_preload_llm_engine, daemon=True)
    _preload_llm_thread.start()
    threading.Thread(target=_warm_token_usage, daemon=True).start()

    _preload_t0 = time.monotonic()
    _spin_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    _tty = sys.stdout is not None and sys.stdout.isatty()
    try:
        while _preload_llm_thread.is_alive():
            if _tty:
                _frame = _spin_frames[int((time.monotonic() - _preload_t0) * 10) % len(_spin_frames)]
                print(f"\r  {_frame} {_agent_name()}正在预热模型引擎 · {time.monotonic() - _preload_t0:.1f}s   ", end="", flush=True)
            _preload_llm_thread.join(timeout=0.1)
    except KeyboardInterrupt:
        if _tty:
            print("\r  ⚠️ 已跳过预热等待，首轮对话可能稍慢。           ")

    print_logo()

    def _warm_background_services():
        time.sleep(1.5)
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.kernel32.SetThreadPriority(
                    ctypes.windll.kernel32.GetCurrentThread(), -2
                )
            except Exception:
                pass
        try:
            import core.llm as _llm_module

            _llm_module.tool_registry.all()
        except Exception:
            pass
        # 刻意不在这里连 MCP：amap 实测要 6~7s（npx 拉起 + mcp 全家桶 import），
        # 全程 CPU 密集，会持续抢 GIL 让键盘回显一顿一顿。MCP 交给首次请求时
        # 的惰性连接（get_tools_schema 的有界等待，只等一次），界面先保持流畅。
        # 同理也不在后台预热 @文件清单：该缓存只在 UI 线程读写，跨线程会引入
        # 竞态，而它只有 0.17s，留给第一次输入 @ 时现扫更划算。

    threading.Thread(target=_warm_background_services, daemon=True).start()

    def _ui_refresher_loop():
        while True:
            try:
                # 只要星瑶在操作或思考，就主动刷新 UI 以更新旋转动画。
                # snapshot() 内部持锁、线程安全；invalidate 必须经 schedule_ui 回到 UI 线程。
                if is_thinking or any(
                    item.get("status") == "in_progress"
                    for item in get_default_plan_manager().snapshot()
                ):
                    schedule_ui(_invalidate_ui)
            except Exception:
                # 兜底防线：刷新线程的任何异常都不允许杀死该线程（否则动画永久停转）
                pass
            time.sleep(0.2)

    threading.Thread(target=_ui_refresher_loop, daemon=True).start()

    while True:
        pending_action = None
        app_instance = _make_input_app()

        # 注册后台子代理终态实时通知回调
        # 后台任务完成/失败/超时/取消时，立即在终端打印一条通知，不等下一轮对话。
        # 回调在 worker 线程中触发，必须通过 schedule_ui 切回 UI 线程打印（遵守 AGENTS.md 线程安全规则）。
        from core.tools import subagent_manager as _sm_for_notification
        _sm_for_notification.set_on_terminal(_on_background_subagent_terminal)

        with patch_stdout():
            try:
                app_instance.run(pre_run=drain_ui_events)
            except EOFError:
                # Ctrl+D：退出系统
                pass
            except KeyboardInterrupt:
                if is_thinking or active_cancel_event is not None:
                    _cancel_active_turn()
                    # 清空排队消息，避免取消后旧任务继续执行
                    try:
                        while True:
                            msg_queue.get_nowait()
                    except queue.Empty:
                        pass
                    print_formatted_text(HTML(
                        f"\n<ansired>🛑 收到中断信号，已终止{_agent_name()}的执行。</ansired>\n"
                    ))
                else:
                    print_formatted_text(HTML(
                        "\n<ansigray>（Ctrl+C 清空输入；退出请按 Ctrl+D）</ansigray>\n"
                    ))
                _reset_turn_state()
                pending_action = "resume_input"

        if pending_action == "open_models":
            # 在主线程安全地开启全屏选择器
            from core.model_selector import open_model_selector

            def _on_selected(info: dict):
                global current_model, current_key, current_url, current_ctx_win, current_model_name
                current_model   = info["litellm_model"]
                current_model_name = info["name"]
                current_key     = info["api_key"]
                current_url     = info["base_url"]
                current_ctx_win = info["context_window"]
                _sync_active_model_context()

            open_model_selector(on_model_selected=_on_selected)
            active_info = get_active_model()
            if active_info:
                current_model      = active_info["litellm_model"]
                current_model_name = active_info["name"]
                current_key        = active_info["api_key"]
                current_url        = active_info["base_url"]
                current_ctx_win    = active_info["context_window"]
            else:
                current_model      = ""
                current_model_name = "未激活模型"
                current_key        = ""
                current_url        = ""
                current_ctx_win    = 128000
            _sync_active_model_context()

            recalc_token_usage()
            print_logo(clear_screen=True)
            show_session_history(current_session)
            pending_action = None
            continue
            
        elif pending_action == "open_skills":
            from core.skill_selector import open_skill_selector
            open_skill_selector()
            pending_action = None
            continue

        elif pending_action == "open_mcp":
            from core.mcp_selector import open_mcp_selector
            open_mcp_selector()
            pending_action = None
            continue
            
        elif pending_action == "open_sessions":
            from core.session_selector import open_session_selector
            switched_session = [None]
            rename_target = [None]
            
            def _on_switch(new_session):
                global current_session
                current_session = new_session
                sync_plan_session(current_session)
                switched_session[0] = new_session
                
            def _on_rename(session_name):
                rename_target[0] = session_name

            session_existed_before = current_session in {s["name"] for s in manager.get_all_sessions()}
            open_session_selector(
                on_switch=_on_switch,
                on_rename=_on_rename,
                manager=manager,
                current_session=current_session,
            )

            if rename_target[0]:
                target = rename_target[0]
                try:
                    new_name = input(f"请输入会话「{target}」的新名称: ").strip()
                    if new_name:
                        success, final_name = manager.rename_session(target, new_name)
                        if success:
                            if current_session == target:
                                current_session = final_name
                                sync_plan_session(current_session)
                            safe_final = final_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                            print_formatted_text(HTML(f"<ansigreen>✔ 会话已成功重命名为: {safe_final}</ansigreen>"))
                            recalc_token_usage()
                            print_logo(clear_screen=False)
                        else:
                            print_formatted_text(HTML("<ansired>❌ 会话重命名失败。</ansired>"))
                except (KeyboardInterrupt, EOFError):
                    print_formatted_text(HTML("\n<ansiyellow>⚠️ 已取消重命名操作。</ansiyellow>"))
            
            # 防御：仅当当前会话“原本已入库、且在选择器里被物理删除”时才自动切回；
            # 未入库的新会话按 Esc 返回时保持原样，不得切走
            resolved_session, was_deleted = _resolve_session_after_selector(
                current_session, session_existed_before
            )
            if was_deleted:
                current_session = resolved_session
                sync_plan_session(current_session)
                recalc_token_usage()
                print_logo(clear_screen=True)
                show_session_history(current_session)
            elif switched_session[0]:
                recalc_token_usage()
                safe_session = switched_session[0].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"\n<ansigreen>Switching to session {safe_session}...</ansigreen>"))
                print_logo(clear_screen=False)
                show_session_history(switched_session[0])
                
            pending_action = None
            continue

        elif pending_action == "resume_input":
            pending_action = None
            continue

        else:
            # 正常退出（Ctrl+D 或其他原因）
            try:
                manager.close()
            except Exception:
                pass
            break


if __name__ == "__main__":
    try:
        from config import UnknownConfigKeysError, audit_config_keys

        audit_config_keys()
    except UnknownConfigKeysError as config_key_error:
        print(f"\n🚨 配置校验失败：{config_key_error}", file=sys.stderr)
        print("请修正 config.json 中的未知键（或删除拼错的键）后重试。", file=sys.stderr)
        sys.exit(2)
    except Exception:
        # 自检属于 best-effort 诊断，自身故障绝不能反过来阻断启动
        pass

    import argparse
    parser = argparse.ArgumentParser(
        prog="ycode",
        description="星瑶Code 终端 AI 编程助手",
        add_help=False,
    )
    parser.add_argument("-p", "--prompt", dest="prompt",
                        help="Headless 模式：执行一次性任务后退出（- 读取 stdin）")
    parser.add_argument("--session", dest="session", default=None,
                        help="指定会话名（headless 模式下有效）")
    parser.add_argument("-h", "--help", action="help",
                        help="显示帮助信息")

    args, _ = parser.parse_known_args()

    if args.prompt is not None:
        exit_code = run_headless(prompt=args.prompt, session_name=args.session)
        sys.exit(exit_code)
    else:
        run_cli()
