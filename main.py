import os
import shutil
import sys
import io

# Windows GBK 终端在输出 emoji/非 BMP 字符时会触发 UnicodeEncodeError 崩溃。
# 强制 stdout/stderr 使用 UTF-8 + replace 错误策略：无法显示的字符会变成 �，
# 保证程序不崩溃；现代 Windows Terminal 已支持 UTF-8，旧版 conhost 最多显示乱码。
# 光把 stdout 包成 UTF-8 还不够：控制台代码页若仍是 GBK(936)，UTF-8 字节流会被
# 按 GBK 解释，非 BMP emoji（🚨 🤖 等）照样乱码。启动时尽量把控制台代码页切到
# UTF-8(65001)，让旧版 conhost 也能正确显示。
if sys.platform == "win32":
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

# 防止 PyInstaller 双重导入主入口模块导致全局变量（如 app_instance 等）在子线程内读取为 None 的双重导入陷阱
if '__main__' in sys.modules and 'main' not in sys.modules:
    sys.modules['main'] = sys.modules['__main__']

# 启动即时反馈：后续 import 链（prompt_toolkit/mcp/core 等）需要数秒，
# 在此之前先打印一行启动提示，避免用户输入 ycode 后终端毫无反应。
# headless 模式（-p/--prompt）与重定向输出（管道/测试捕获）下保持安静，
# 避免污染 stdout 的程序化消费。
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
    print("  星瑶正在启动，请稍候...", flush=True)

# TEMP STARTUP TRACE (revert after diagnosis)：从启动提示处开始全程计时
import time as _time
_STARTUP_T0 = _time.monotonic()
def _startup_trace(label):
    try:
        import os as _os
        with open(_os.path.join(_os.path.expanduser("~"), ".ycode", "startup-trace.log"), "a", encoding="utf-8") as _tf:
            _tf.write(f"{_time.monotonic()-_STARTUP_T0:8.2f}s  {_time.strftime('%H:%M:%S')}  {label}\n")
    except Exception:
        pass
_startup_trace("startup line printed (import chain begins)")

from core.runtime_warnings import configure_runtime_warnings
configure_runtime_warnings()
import datetime
import logging
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
from prompt_toolkit.styles import Style
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
# 启动优化：history_renderer / terminal_markdown（连带 rich+markdown_it ~0.5s）
# 不在顶部 import，延迟到首次渲染历史/回答时在函数内加载。
from core.paste_placeholders import PastedTextStore, normalize_pasted_text
import core.agent_service as _agent_service
from core.llm import chat_with_model_events, session_token_usage, token_usage_lock
from core.permission_broker import PermissionRequest, permission_broker
from core.tool_progress import ActiveToolStatus
from core.turn_controller import TurnController
from core.turn_renderer import TurnRenderer
from core.ui_markers import ANSI_GRAY, ANSI_RESET, plan_color, plan_html_color, plan_mark
from core.plan import bind_default_plan_session, get_default_plan_manager
from core.model_manager import get_active_model
from core.version import __version__ as YCODE_VERSION
# 警告：from-import 会在 import 时冻结常量值。新增调用方禁止 from-import 这些常量，
# 应 import config 后动态读取（如 config.MY_MODEL），以便配置热更新生效。
from config import MY_MODEL, MY_KEY, MY_BASE_URL, MY_THEME, MY_PERMISSION_MODE, DB_FILE

_startup_trace("import chain done")
manager = MemoryManager(db_path=DB_FILE)
_startup_trace("MemoryManager ready")

# ---------------------------------------------------------------------------
# 全局状态所有权约定（为后续 AppState 结构化重构铺路）：
#   1. 以下可变全局原则上只能由 UI 主线程直接写入；后台 worker / 辅助线程
#      一律通过 schedule_ui() 把状态变更提交回 UI 线程执行。
#   2. 例外：active_cancel_event / active_turn_id 由 worker 线程在 turn 的
#      try/finally 作用域内建立与清理，UI 线程只读。
#   3. session_token_usage 由 core.llm.token_usage_lock 保护，读写必须持锁。
#   4. 后台线程绝不直接调用 print_formatted_text / app_instance.invalidate()。
# ---------------------------------------------------------------------------
current_theme = MY_THEME
# 权限模式统一走 app_context 规范化，全系统只允许 confirm / auto / plan / full
import core.app_context as _app_ctx
current_permission_mode = _app_ctx.normalize_permission_mode(MY_PERMISSION_MODE)
plan_approved_this_turn = False
show_memory_summary = False
confirm_prompt = None

# 全局运行时状态（优先从 config.json 读取激活模型）
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
# 记录每个 turn_id 是否已经打印过"🤖 星瑶 >"头部：头部改为在第一个 Markdown
# 正文块渲染前才打印（而不是回合的第一个事件到来时），若本轮完全没有产出文字，
# 则永远不会打印头部——这是符合预期的，见 _render_public_answer_block。
_answer_header_printed = {}
# 已触发自动汇总的会话集合：避免对同一批后台任务重复触发汇总 turn
# 当用户发起新对话（新一轮后台任务）时，需要重置该会话的标志
_bg_summary_triggered = set()
_bg_summary_lock = threading.Lock()  # 保护 _bg_summary_triggered 的检查+添加原子性
active_thinking_text = ""
active_thinking_turn_id = None
active_cancel_event = None
active_turn_id = None
app_instance = None
is_thinking = False
# LLM 引擎（litellm/tiktoken/模型能力探测）是否已在后台预热完成。
# 首轮对话若赶在预热完成前发出，状态行会显示“正在启动模型引擎”而不是
# 让用户误以为卡死。
_llm_engine_ready = False
task_status = 'idle'  # 全局运行状态：'idle' (闲置), 'thinking' (思考中), 'executing' (执行中), 'waiting' (等待确认)
think_start_time = 0
# 底部活动指示区使用的独立墙钟计时起点：思考开始/首个工具开始时刷新，
# 避免工具执行阶段复用上一次思考的起始时间。
activity_start_time = 0
# 当前工具活动只保存 renderer 产出的安全展示字段，不读取原始参数或工具结果。
_active_tool_status: ActiveToolStatus | None = None
# 思考面板延迟隐藏标志：思考结束后保留面板显示最后一帧内容，直到下一个事件（工具/文字/新思考）到来
# 避免短思考（如 14ms）面板闪现即消失，让用户能读到思考内容
_thinking_panel_linger = False
# 思考段逐段即时打印：每段有实质内容的思考在 thinking_finished 时立刻
# 打印一行"思考完成 · 用时 X"，作为该段输出正文的天然分隔线，
# 避免多段短输出揉成一团难以阅读。
# 粘贴大段文本的占位符存储：输入框里只显示 [粘贴文本 #N · 共 M 行]，
# 提交时 expand 还原成完整原文再发送（见 kb 的 BracketedPaste 绑定与 submit）
_paste_store = PastedTextStore()
# 轮次分隔线标志：首次回显 "✨ You >" 前置 False（屏幕上还没有内容，不打印分隔线），
# 之后每轮提问前打印一条灰色细分隔线，把相邻轮次在视觉上分开。
# 切换/回放会话（show_session_history）与 /clear 清屏时重置为 False。
_turn_separator_needed = False
pending_action = None   # 待处理的主线程操作：目前可能的值有 "open_models" / "open_skills" / "open_sessions"
# 计划项状态提示去重：避免同一个计划项反复失败/重试时刷屏
_last_printed_plan_state: dict[str, str] = {}

# ---------------------------------------------------------------------------
# 注册 TUI 运行时上下文（让 core/llm.py 通过 app_context 访问，而非直接 import main）
# _app_ctx 已在文件顶部导入（权限模式规范化需要）
# ---------------------------------------------------------------------------

class _TuiContext:
    """TUI 运行时上下文适配器，代理到 main 模块的全局变量。"""

    # UI 形态：run_cli 启动时会写成 "tui"，headless 模式保持默认值
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
    def plan_approved_this_turn(self) -> bool:
        return plan_approved_this_turn

    @plan_approved_this_turn.setter
    def plan_approved_this_turn(self, val: bool):
        global plan_approved_this_turn
        plan_approved_this_turn = val

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
    def permission_callback(self):
        return None  # TUI 使用 prompt_toolkit broker，不需要 GUI 回调

    def invalidate_ui(self):
        if app_instance and app_instance.loop:
            app_instance.loop.call_soon_threadsafe(app_instance.invalidate)

_app_ctx.register(_TuiContext())


def _sync_active_model_context():
    """把当前激活模型同步给 app_context，供 core 层（子代理 / 梦境整理）读取。

    必须在每一处更新 current_model / current_key / current_url 之后调用，
    否则 core/tools.py 会拿到过期的模型配置。
    """
    _app_ctx.set_active_model_tuple(current_model, current_key, current_url)


_sync_active_model_context()


# 主题配色展示元数据（/theme list 与无参数帮助共用）
THEME_DESCS = {
    "cyan": ("极光青", "#00b4d8"),
    "matrix": ("骇客绿", "#39ff14"),
    "nord": ("北欧蓝", "#88c0d0"),
    "cyberpunk": ("赛博粉", "#ff007f"),
    "dracula": ("德古拉紫", "#bd93f9"),
    "lava": ("熔岩红", "#ff5555"),
    "sunset": ("落日橙", "#ff9f43"),
    "amber": ("琥珀黄", "#f1fa8c"),
    "white": ("象牙白", "#ffffff"),
}

# /permission 帮助文本（list 子命令与无参数帮助共用）
PERMISSION_HELP_LINES = (
    "  - <b>confirm</b> : 变更前确认 (修改文件、执行命令均需询问)",
    "  - <b>plan</b>    : 计划模式 (开始执行修改前需一次性批准计划)",
    "  - <b>full</b>    : 完全访问 (自动运行，无需任何确认)",
)

# @文件补全的工作目录清单缓存（TTL 秒内重复按键不再重新 os.walk）
_FILE_LIST_CACHE_TTL = 5.0
_file_list_cache = {"timestamp": 0.0, "files": []}

# 记忆压缩触发阈值：历史 token 数占上下文窗口的比例超过此值即触发压缩。
# 默认 0.70（折中值）：
#   - 0.80（旧值）太晚，单次压缩压力大且易触发兜底逻辑（prune_history_files）
#   - 0.65 偏激进，配合 prompt caching 后缓存命中部分成本仅 1/10，频繁触发反而更费 token
#   - 0.70 配合 prompt caching 是较好的折中：仍留有 30% 缓冲给当前轮 + 工具结果
MEMORY_COMPRESS_THRESHOLD = 0.70


def _esc(text) -> str:
    """转义一切插入 prompt_toolkit HTML(...) 的外部/用户内容，防止标签解析错乱。"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _term_display_width(text):
    """按终端显示宽度计算字符串宽度（中文等 East Asian 宽字符算 2 格）。"""
    import unicodedata
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in str(text)
    )


def _pad_right(text, width):
    """按终端显示宽度左对齐补齐到 width 格。"""
    text = str(text)
    return text + " " * max(0, width - _term_display_width(text))


def _pad_left(text, width):
    """按终端显示宽度右对齐补齐到 width 格。"""
    text = str(text)
    return " " * max(0, width - _term_display_width(text)) + text


def _parse_usage_date_args(args):
    """解析 /usage 的日期参数。

    支持:
      /usage                                -> None（默认 今日/近7天/近30天 三段统计）
      /usage 2026-08-01                     -> (date, date) 指定单日
      /usage 2026-08-01 2026-08-05          -> (start, end) 指定区间
      /usage 2026-08-01 ~ 2026-08-05        -> 同上（~ 与 to 作为分隔符被忽略）
    区间先后写反时自动纠正；日期非法或参数过多时抛出 ValueError。
    """
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
    """打印一个日期区间的按模型 token 用量统计段，返回该段是否有数据。"""
    title = f"{label} ({start_s})" if start_s == end_s else f"{label} ({start_s} ~ {end_s})"
    print_formatted_text(HTML(f"<b><ansiyellow>{_esc(title)}</ansiyellow></b>"))
    rows = manager.query_token_usage_by_model(start_s, end_s)
    if not rows:
        print_formatted_text(HTML("  <ansigray>暂无记录</ansigray>"))
        return False
    name_w = max(12, min(40, max(_term_display_width(r["model"]) for r in rows) + 2))
    _header = (
        "  " + _pad_right("模型", name_w)
        + " " + _pad_left("输入", 14)
        + " " + _pad_left("输出", 14)
        + " " + _pad_left("缓存命中", 14)
        + " " + _pad_left("命中率", 10)
        + " " + _pad_left("合计", 14)
    )
    print_formatted_text(HTML(f"<ansigray>{_header}</ansigray>"))
    for r in rows:
        _rate = f"{(r['cached'] / r['prompt'] * 100) if r['prompt'] else 0.0:.1f}%"
        print_formatted_text(HTML(
            f"  {_pad_right(_esc(r['model']), name_w)} {r['prompt']:>14} {r['completion']:>14} {r['cached']:>14} {_rate:>10} {r['total']:>14}"
        ))
    if len(rows) > 1:
        sub = {k: sum(r[k] for r in rows) for k in ("prompt", "completion", "cached", "total")}
        _sub_rate = f"{(sub['cached'] / sub['prompt'] * 100) if sub['prompt'] else 0.0:.1f}%"
        print_formatted_text(HTML(
            f"  <ansicyan>{_pad_right('小计', name_w)} {sub['prompt']:>14} {sub['completion']:>14} {sub['cached']:>14} {_sub_rate:>10} {sub['total']:>14}</ansicyan>"
        ))
    inv = manager.query_cache_invalidation_events(start_s, end_s)
    if inv["count"]:
        print_formatted_text(HTML(
            f"  <ansigray>缓存失效事件 {inv['count']} 次（输入≥2万且命中率&lt;50%），"
            f"多消耗输入 {inv['lost_tokens']} tokens——属 provider/网关侧缓存驱逐，客户端无法避免</ansigray>"
        ))
    return True


def _format_token_usage_summary(
    cumulative_prompt, cumulative_completion, cumulative_cached
):
    """格式化状态栏累计 Token，并用同口径数据计算缓存命中率。"""
    def format_num(num):
        if num >= 1_000_000:
            return f"{num / 1_000_000:.1f}M"
        if num >= 1_000:
            return f"{num / 1_000:.1f}K"
        return str(num)

    hit_rate = (
        cumulative_cached / cumulative_prompt * 100
        if cumulative_prompt > 0
        else 0.0
    )
    return (
        f"输入 {format_num(cumulative_prompt)} · "
        f"输出 {format_num(cumulative_completion)} · "
        f"命中 {format_num(cumulative_cached)} · 命中率 {hit_rate:.0f}%"
    )


def set_terminal_title(title: str) -> None:
    """设置终端窗口标题，兼容 Windows 控制台与 Windows Terminal 等现代终端。"""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass
    try:
        # ANSI OSC 0：Windows Terminal / 现代终端通用
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()
    except Exception:
        pass


def _list_workspace_files():
    """返回工作目录文件清单（带 TTL 缓存，避免大仓库下每次按键都 os.walk）。"""
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
    # 注：以下两步赋值非原子操作。当前 _list_workspace_files 仅在 UI 主线程
    # （SlashCommandCompleter.get_completions）调用，不存在并发问题。
    # 若未来需要在后台线程调用，应改为原子替换整个 dict 对象。
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
    # Application.run() 结束后 prompt_toolkit 会 loop.close()；已关闭的 loop 不是 None，
    # 在 App 退出到重建的间隙（如 /models 选择器期间）再调度只会抛 RuntimeError，直接丢弃。
    is_closed = getattr(loop, "is_closed", None)
    if callable(is_closed) and is_closed():
        return
    try:
        loop.call_soon_threadsafe(callback)
    except RuntimeError:
        # 与 loop.close() 的竞态窗口：is_closed() 判定后 loop 仍可能在调度前关闭
        pass


def _on_background_subagent_terminal(task_id: str, status: str, description: str, summary: str) -> None:
    """后台子代理终态实时通知（在 worker 线程中触发，切回 UI 线程打印）。

    遵守 AGENTS.md 线程安全规则：不在 worker 线程直接调 print_formatted_text，
    通过 schedule_ui 回到主 prompt_toolkit 线程。

    额外职责：当本会话所有后台子代理任务都进入终态时，自动向 msg_queue 注入
    一条汇总指令，触发主代理汇总所有后台任务结果（用户要求"运行完告诉我"）。
    用 _bg_summary_triggered 集合避免对同一批任务重复触发。
    """
    def _print_notification():
        # 状态对应的图标和颜色
        icon_color = {
            "completed": "ansigreen",
            "failed": "ansired",
            "timed_out": "ansired",
            "cancelled": "ansigray",
        }.get(status, "ansiyellow")
        icon = {
            "completed": "✓",
            "failed": "✗",
            "timed_out": "⏰",
            "cancelled": "⊘",
        }.get(status, "•")
        status_label = {
            "completed": "已完成",
            "failed": "已失败",
            "timed_out": "已超时",
            "cancelled": "已取消",
        }.get(status, status)

        # 描述截断到 40 字符，避免通知过长
        desc_short = (description or "")[:40]
        # 摘要截断到 150 字符
        summary_short = (summary or "")[:150]
        # HTML 转义
        import html as _html
        desc_safe = _html.escape(desc_short)
        summary_safe = _html.escape(summary_short)
        tid_safe = _html.escape(task_id)

        try:
            print_formatted_text(HTML(""))
            print_formatted_text(HTML(
                f"<ansicyan>🔔 后台子代理{status_label}：</ansicyan>"
                f"<ansigray>{desc_safe}</ansigray> "
                f"(<{icon_color}>{icon}</{icon_color}> "
                f"id=<ansicyan>{tid_safe}</ansicyan>)"
            ))
            if summary_short:
                print_formatted_text(HTML(
                    f"   <ansigray>详情：{summary_safe}</ansigray>"
                ))
            print_formatted_text(HTML(
                f"   <ansigray>输入 /tasks tail {tid_safe} 查看完整结果</ansigray>"
            ))
            print_formatted_text(HTML(""))
        except Exception:
            # 通知打印失败不能影响主流程
            pass

    schedule_ui(_print_notification)

    # 自动汇总触发：在独立线程中延迟检查，避免与并发任务终态回调产生竞态。
    # 直接在回调中检查 has_running_background_tasks 可能误判——另一个后台任务
    # 可能正在终止过程中（状态还没更新到 SubagentManager）。
    # 延迟 0.5s 后再检查，给并发的终态回调留出更新状态的时间。
    def _deferred_summary_check():
        try:
            from core.tools import subagent_manager as _sm_for_trigger
            session = current_session
            if not session:
                return
            # 短暂等待，让并发的终态回调完成状态更新
            time.sleep(0.5)
            # 再次检查：如果还有运行中的后台任务，不触发汇总
            if _sm_for_trigger.has_running_background_tasks(session):
                return
            # 本会话已触发过汇总时，不重复触发（用户发起新对话时会重置标志）。
            # 加锁保证检查+添加的原子性：两个子代理在 0.5s 内先后结束时，
            # 无锁版本会双双通过检查并触发两次汇总，白白烧掉一轮 token。
            with _bg_summary_lock:
                if session in _bg_summary_triggered:
                    return
                _bg_summary_triggered.add(session)
            # 注入汇总指令到 msg_queue，让主代理自动开始汇总 turn
            msg_queue.put({
                "text": "__ycode_internal__:summarize_background_tasks",
                "was_queued": False,
                "is_skill_call": False,
                "is_internal": True,
            })
        except Exception:
            # 触发失败不能影响主流程
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


def _confirm_prompt_text(prompt: dict | None) -> str:
    if not prompt:
        return ""
    func = prompt["function_name"]
    args = prompt["arguments"]
    text = _permission_source_line(prompt)
    if func == "execute_command":
        cmd = args.get("command", "")
        cmd_lines = cmd.splitlines()
        if len(cmd_lines) > 2:
            cmd_display = "\n".join(cmd_lines[:2]) + f"\n    ... (共 {len(cmd_lines)} 行)"
        else:
            cmd_display = cmd
        if len(cmd_display) > 250:
            cmd_display = cmd_display[:247] + "..."
        text += f"💻  星瑶申请执行命令: {_esc(cmd_display)}\n"
    elif func == "write_file":
        text += f"✍️  星瑶申请写入文件: {_esc(args.get('file_path', ''))}\n"
    elif func == "edit_file":
        text += f"📝  星瑶申请修改文件: {_esc(args.get('file_path', ''))}\n"
    elif func == "ask_user":
        q = str(args.get("question", ""))
        q_lines = q.splitlines()
        if len(q_lines) > 3:
            q_display = "\n".join(q_lines[:3]) + f"\n    ... (问题包含多行内容，共 {len(q_lines)} 行)"
        else:
            q_display = q
        if len(q_display) > 200:
            q_display = q_display[:197] + "..."
        text += f"❓  星瑶需要你的选择: {_esc(q_display)}\n"
    elif func == "sandbox_outside_access":
        text += f"🛡  星瑶申请访问工作目录之外的路径: {_esc(args.get('path', ''))}\n"
        text += "    （沙盒模式下默认仅限工作目录内；同意本次仅放行此操作，可用 /sandbox on 长期开放）\n"
    else:
        # 默认分支：其余工具（含 MCP 工具）展示函数名与参数摘要，不再显示空白
        summary = ", ".join(
            f"{key}={str(value)[:80]}" for key, value in list(args.items())[:3]
        )
        text += f"🔧  星瑶申请调用工具: {_esc(func)}({_esc(summary)})\n"

    text += "👉 请选择确认操作:\n"
    for idx, option in enumerate(prompt["options"]):
        prefix = "  > " if idx == prompt["current_idx"] else "    "
        if idx == prompt["current_idx"]:
            text += f"<ansigreen><b>{prefix}{_esc(option)}</b></ansigreen>\n"
        else:
            text += f"{prefix}{_esc(option)}\n"
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
        # 仅当 active_turn_id 不为 None（确实有 turn 正在运行）时才恢复 executing 状态；
        # 若 turn 已结束（active_turn_id 已被清空），保持 idle 状态，避免旋转动画永不消失。
        if active_cancel_event is not None and active_turn_id is not None:
            task_status = "executing"
            is_thinking = True
        if app_instance is not None:
            app_instance.invalidate()

    schedule_ui(dismiss)


# 注意：permission_broker.set_prompt_handlers 已移入 run_cli()，
# 避免 import main 即产生全局副作用，保证本模块可被测试安全导入。


def _clear_active_thinking() -> None:
    global active_thinking_text, active_thinking_turn_id
    active_thinking_text = ""
    active_thinking_turn_id = None


def _thinking_panel_text() -> str:
    """Return transient reasoning text for the live TUI panel only."""
    return active_thinking_text or "正在分析..."


def _reset_turn_state():
    global is_thinking, task_status, _active_tool_status, _thinking_panel_linger
    _clear_active_thinking()
    is_thinking = False
    task_status = "idle"
    _active_tool_status = None
    _thinking_panel_linger = False
    if app_instance is not None:
        app_instance.invalidate()


def _cancel_active_turn() -> None:
    from core.tools import subagent_manager
    from core.task_scheduler import get_default_scheduler

    # 取消完全由每 turn 的 cancel_event 驱动（core.llm 已不再读取模块级 cancel_requested）
    if active_cancel_event is not None:
        active_cancel_event.set()
    if active_turn_id is not None:
        permission_broker.cancel_turn(active_turn_id)
        subagent_manager.cancel_foreground(current_session, active_turn_id)
    get_default_scheduler().cancel_all()


def post_ui_event(event):
    """Queue a turn event and arrange for the main TUI loop to consume it."""
    ui_event_queue.put(event)
    schedule_ui(drain_ui_events)


def _forward_response_events(events, post_event=post_ui_event):
    """Forward one model event stream and collect only public assistant text."""
    reply_parts = []
    for event in events:
        if event.kind == "assistant_delta":
            reply_parts.append(str(event.payload.get("text", "")))
        post_event(event)
    return "".join(reply_parts)


def _resolve_confirm_prompt(index):
    """Resolve the active TUI question only when the selected option exists."""
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


def _format_plan_items(items):
    return "\n".join(
        f"  {plan_mark(item.get('status'))} {item.get('subject', '').strip()}"
        for item in items
        if item.get("subject", "").strip()
    )


def _format_live_plan_items(items):
    """Return a compact colored snapshot for the execution-only plan panel."""
    visible = [item for item in items if str(item.get("subject", "")).strip()]
    completed = sum(item.get("status") == "completed" for item in visible)
    lines = [f"  进度 {completed}/{len(visible)}"]
    for item in visible[:8]:
        status = item.get("status")
        color = plan_color(status)
        marker = plan_mark(status)
        subject = str(item.get("subject", "")).replace("\x1b", "")
        stage = str(item.get("stage", "")).strip()
        stage_label = {"plan": "计划", "build": "构建", "review": "审查"}.get(stage, "")
        suffix = f" [{stage_label}]" if stage_label else ""
        lines.append(f"  {color}{marker}{ANSI_RESET} {subject}{suffix}")
    if len(visible) > 8:
        lines.append(f"  ... 还有 {len(visible) - 8} 项")
    return "\n".join(lines)


# 工具名 → 状态行具体活动文案（与 tool_presenter._TOOL_COPY 的动作名保持一致）
_TOOL_ACTIVITY_LABELS = {
    "read_file": "星瑶正在查看文件...",
    "write_file": "星瑶正在写入文件...",
    "edit_file": "星瑶正在修改文件...",
    "execute_command": "星瑶正在执行命令...",
    "list_directory": "星瑶正在查看目录...",
    "glob_files": "星瑶正在查找文件...",
    "search_code": "星瑶正在搜索代码...",
    "search_news": "星瑶正在搜索资料...",
    "fetch_url": "星瑶正在访问网址...",
    "get_weather": "星瑶正在查询天气...",
    "run_subagent": "星瑶正在调度子代理...",
    "manage_task": "星瑶正在管理后台任务...",
    "ask_user": "星瑶等待你的回答...",
    "plan_write": "星瑶正在更新任务计划...",
    "plan_update": "星瑶正在更新计划项...",
}


def _turn_activity_label(status, current_tool=""):
    if status == "executing" and current_tool:
        return _TOOL_ACTIVITY_LABELS.get(current_tool, "星瑶正在干活...")
    labels = {
        "thinking": "星瑶正在思考...",
        "executing": "星瑶正在干活...",
        "waiting": "星瑶等待你的确认...",
    }
    return labels.get(status, "")


def _format_activity_text(
    status: str,
    active_status: ActiveToolStatus | None,
    elapsed: float,
    frame: str,
) -> str:
    """Format the live activity region from safe renderer-owned display data."""
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


def _terminal_width():
    return max(20, shutil.get_terminal_size((100, 24)).columns)


def _print_turn_separator() -> None:
    """在两轮对话之间打印一条灰色细分隔线（前后各留一个空行）。"""
    line = "─" * min(_terminal_width(), 60)
    _print_ui_fragment(f"\n{ANSI_GRAY}{line}{ANSI_RESET}\n")


def _echo_user_input(html_text: str) -> None:
    """回显用户输入；非首轮时在前面补一条轮次分隔线。"""
    global _turn_separator_needed
    if _turn_separator_needed:
        _print_turn_separator()
    _turn_separator_needed = True
    print_formatted_text(HTML(html_text))


def _format_thinking_elapsed(elapsed: float) -> str:
    # 短思考（< 1s）用毫秒显示，避免全部显示成 "0.0s" 让用户误以为没思考
    if elapsed < 1.0:
        return f"{int(elapsed * 1000)}ms"
    return f"{elapsed:.1f}s"


def _render_public_answer_block(turn_id: str, text: str) -> None:
    block = str(text or "").strip()
    if not block:
        return
    # 头部延迟到这里——第一个真正要渲染的正文块——才打印，而不是回合的
    # 第一个事件（可能只是一次工具调用）到来时就打印。同一回合只打印一次。
    if not _answer_header_printed.get(turn_id):
        _answer_header_printed[turn_id] = True
        print_formatted_text(HTML("\n<b><magenta>🤖 星瑶 &gt;</magenta></b> "))
    try:
        from core.terminal_markdown import MarkdownRenderError, render_markdown_to_ansi
        rendered = render_markdown_to_ansi(block, _terminal_width())
    except MarkdownRenderError:
        rendered = block
    _print_ui_fragment(rendered)


def _find_paragraph_boundary(buffer: str) -> int:
    """在 buffer 中查找段分隔符 \n\n，但避开 ``` 代码块内部。"""
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

    # Prefer paragraph boundaries so headings, lists, and tables are rendered as
    # complete Markdown blocks. Long prose also flushes at sentence boundaries.
    # 只在代码块外按空行分段，避免 ``` 内的空行把代码块拆碎导致渲染失败。
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
            # 英文句号边界（". "）会让剩余部分以空格开头，rich 会把它渲染成
            # 一个突兀的缩进，这里去掉行首空格（保留换行语义）
            buffer = buffer[boundary + 1:].lstrip(" ")
    _assistant_stream_buffers[turn_id] = buffer


def _render_ui_event(event):
    global is_thinking, task_status, think_start_time, activity_start_time
    global active_thinking_text, active_thinking_turn_id, _active_tool_status
    global _thinking_panel_linger
    renderer = ui_renderers.get(event.turn_id)
    if renderer is None:
        # 简短模式：只显示"星瑶正在干活"汇总行 + 收尾统计行，不显示具体工具调用细节。
        # 数据库记录不受此影响，event 仍带完整 summary 入库。
        renderer = TurnRenderer()
        ui_renderers[event.turn_id] = renderer
        # "🤖 星瑶 >" 头部不在这里打印：改到 _render_public_answer_block 里，
        # 在本回合第一个 Markdown 正文块真正要渲染之前才打印（见该函数注释）。

    if event.kind == "status":
        state = event.payload.get("state")
        if state in {"thinking", "executing", "waiting", "idle"}:
            task_status = state
            if state != "idle":
                is_thinking = state in {"thinking", "executing"}
            else:
                _active_tool_status = None
        message = str(event.payload.get("message", ""))
        if message:
            print_formatted_text(HTML(f"\n<ansigray>{_esc(message)}</ansigray>"))
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

    if event.kind == "plan_updated" or event.kind.startswith("plan_item_"):
        if event.kind.startswith("plan_item_"):
            item = dict(event.payload.get("item") or {})
            subject = str(item.get("subject", "")).replace("\x1b", "").strip()
            item_id = str(item.get("id", "")).strip()
            status = str(item.get("status", "")).strip()
            if subject and item_id:
                # 同一个计划项的同一状态只打印一次，避免失败重试时刷屏
                state_key = f"{item_id}:{event.kind}"
                global _last_printed_plan_state
                if _last_printed_plan_state.get(state_key) == subject:
                    if app_instance is not None:
                        app_instance.invalidate()
                    return
                _last_printed_plan_state[state_key] = subject
                # 统一走 ui_markers：<mark> <subject>，标记按状态着色，不再有
                # 各不相同的动词前缀和末尾多余的 " ✓"
                mark = plan_mark(status)
                color = plan_color(status)
                _print_ui_fragment(f"{color}{mark}{ANSI_RESET} {subject}\n")
                if event.kind == "plan_item_completed":
                    # 完成后清理该计划项的历史状态缓存
                    for key in list(_last_printed_plan_state.keys()):
                        if key.startswith(f"{item_id}:"):
                            del _last_printed_plan_state[key]
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind in {"tool_started", "tool_requested"}:
        task_status = "executing"
        is_thinking = True
        # 工具开始，清除思考面板延迟显示
        _thinking_panel_linger = False
        active_thinking_text = ""

    if event.kind == "notice":
        message = str(event.payload.get("message", ""))
        if message:
            print_formatted_text(HTML(f"<ansiyellow>! {_esc(message)}</ansiyellow>"))
        if app_instance is not None:
            app_instance.invalidate()
        return

    if event.kind == "thinking_started":
        active_thinking_text = ""
        active_thinking_turn_id = event.turn_id
        task_status = "thinking"
        is_thinking = True
        # 回到思考阶段，清空工具活动，状态行显示"星瑶正在思考..."
        _active_tool_status = None
        # 新思考开始，清除上一段思考的延迟显示
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
        # 不立即清空 active_thinking_text：保留最后一帧思考内容供面板延迟显示
        # 仅重置 turn_id 标记，让下一个 thinking_started 能识别新一段思考
        active_thinking_turn_id = None
        task_status = "executing"
        is_thinking = True
        # 思考结束进入执行阶段，此时还没有具体工具。
        _active_tool_status = None
        # 启用思考面板延迟隐藏：保留最后一帧思考内容，直到下一个事件到来
        # 这样即使思考只有十几毫秒，用户也能读到思考内容
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
        # 并行工具增加时不重置 UI 墙钟计时；只在第一项工具开始时刷新。
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
        # 模型开始输出文字，清除思考面板延迟显示
        if _thinking_panel_linger or active_thinking_text:
            _thinking_panel_linger = False
            active_thinking_text = ""
            if app_instance is not None:
                app_instance.invalidate()
    else:
        for fragment in fragments:
            _print_ui_fragment(fragment)

    if event.kind == "turn_finished":
        for fragment in renderer.flush_tool_progress():
            _print_ui_fragment(fragment)
        # 把渲染器里滞留的尾部正文（保守持留的末尾字符、收尾代码围栏等）
        # 送进公开回答缓冲区一起做 Markdown 渲染。此前传 False 会导致
        # 回答最后几个字符/结尾 ``` 静默丢失。
        tail = "".join(renderer.finish_incremental(include_assistant_text=True))
        if tail:
            _assistant_stream_buffers[event.turn_id] = (
                _assistant_stream_buffers.get(event.turn_id, "") + tail
            )
        _flush_public_answer(event.turn_id, final=True)
        # 回合结束后留一个空行，与下一轮的轮次分隔线形成呼吸间距
        print_formatted_text("\n", end="", flush=True)
        ui_renderers.pop(event.turn_id, None)
        _answer_header_printed.pop(event.turn_id, None)
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
        print_formatted_text(HTML("\n<ansiyellow>⏹ 本轮任务已取消。</ansiyellow>\n"))
        ui_renderers.pop(event.turn_id, None)
        _answer_header_printed.pop(event.turn_id, None)
        _clear_active_thinking()
        _active_tool_status = None
        is_thinking = False
        task_status = "idle"
    elif event.kind == "turn_error":
        # 异常结束的回合：core.llm 只发 turn_error（不再发 turn_finished），
        # 这里负责 UI 收尾：冲刷剩余文本、打印错误行并重置状态栏。
        for fragment in renderer.flush_tool_progress():
            _print_ui_fragment(fragment)
        tail = "".join(renderer.finish_incremental(include_assistant_text=True))
        if tail:
            _assistant_stream_buffers[event.turn_id] = (
                _assistant_stream_buffers.get(event.turn_id, "") + tail
            )
        _flush_public_answer(event.turn_id, final=True)
        message = str(event.payload.get("message") or "未知错误")
        print_formatted_text(HTML(f"\n<ansired>⚠️ 回合异常结束：{_esc(message)}</ansired>\n"))
        ui_renderers.pop(event.turn_id, None)
        _answer_header_printed.pop(event.turn_id, None)
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
            # 单个坏事件不允许卡死整个事件队列，记录诊断日志后继续消费
            logging.getLogger("py.warnings").warning("ui event render failed: %s", exc)
    if app_instance is not None:
        app_instance.invalidate()


def recalc_token_usage():
    """全局刷新当前会话的 Token 占用（优先真实用量，无记录才估算兜底）"""
    from core.agent_service import get_session_token_baseline
    history = manager.get_history(current_session)
    with token_usage_lock:
        session_token_usage["latest_total"] = get_session_token_baseline(
            manager, current_session, current_model, history=history
        )


def _invalidate_ui():
    """供 schedule_ui 提交到 UI 线程执行的安全刷新。"""
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
            print_formatted_text(HTML(f"\n<ansigray>💡 会话已自动命名为：{_esc(final_title)}</ansigray>"))
            if app_instance:
                app_instance.invalidate()
        schedule_ui(update_ui)

    def _rename_job():
        try:
            # 提取纯文本内容
            query_text = ""
            if isinstance(user_query, str):
                query_text = user_query
            elif isinstance(user_query, list):
                for item in user_query:
                    if isinstance(item, dict) and item.get("type") == "text":
                        query_text += item.get("text", "")

            # 截取前 200 字符进行提炼，防长上下文文本耗时
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
            from litellm import completion
            response = completion(
                model=api_model,
                api_key=api_key,
                api_base=api_url,
                messages=[{"role": "user", "content": prompt}],
                timeout=10
            )
            title = response.choices[0].message.content.strip()
            # 过滤特殊字符与多余标点
            title = re.sub(r'["\'“”‘’，。！、]', '', title)
            title = title[:8].strip()

            # 确保生成的标题有效且不是默认 Hex 格式
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


def print_logo(clear_screen=True):
    """打印 Logo (画卡版) 及系统信息卡片"""
    if clear_screen:
        if app_instance is not None and getattr(app_instance, "renderer", None):
            # 应用运行期间必须用 prompt_toolkit 自己的清屏：os.system 会绕过
            # 渲染器的屏幕缓存，导致输入框/状态栏错位或残影
            try:
                app_instance.renderer.clear()
            except Exception:
                os.system('cls' if os.name == 'nt' else 'clear')
        else:
            os.system('cls' if os.name == 'nt' else 'clear')
        
    custom_fig = pyfiglet.Figlet(font='slant')
    logo_text = custom_fig.renderText('Y-CODE')
    safe_logo = logo_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    from core.themes import THEMES
    theme_color = THEMES.get(current_theme, {}).get("border", "#00b4d8")
    
    print_formatted_text(HTML(f"<style fg='{theme_color}'><b>{safe_logo}</b></style>"))
    
    cwd = os.getcwd()
    
    cwd_display = cwd
    if len(cwd) > 35:
        cwd_display = "..." + cwd[-32:]
        
    active_info = get_active_model()
    model_display = active_info["name"] if active_info else "未激活模型"
    safe_session = current_session.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    def display_width(s):
        clean_s = re.sub(r'<[^>]+>', '', s)
        clean_s = clean_s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        width = 0
        for char in clean_s:
            code = ord(char)
            if 0x2500 <= code <= 0x259F:
                width += 1
            elif code > 127:
                width += 2
            else:
                width += 1
        return width
        
    def make_box_line(content, inside_width=56):
        w = display_width(content)
        spaces = inside_width - w
        if spaces < 0:
            spaces = 0
        return f"<style fg='{theme_color}'>│</style>{content}{' ' * spaces}<style fg='{theme_color}'>│</style>\n"
        
    def make_data_line(label, val):
        val_str = str(val)
        w = display_width(val_str)
        if w > 38:
            truncated = ""
            curr_w = 0
            for char in val_str:
                char_w = 2 if ord(char) > 127 else 1
                if curr_w + char_w > 35:
                    truncated += "..."
                    break
                truncated += char
                curr_w += char_w
            val_str = truncated
            
        escaped_val = val_str.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return make_box_line(f"{label}<ansigray>{escaped_val}</ansigray>")

    avatar_row1 = f"  <style fg='{theme_color}'>▄▄    ▄▄</style>    <b>Welcome to Y-CODE CLI!</b>"
    avatar_row2 = f" <style fg='{theme_color}'>██████████</style>   星瑶 (Hacker Girl) 为您服务"
    avatar_row3 = f" <style fg='{theme_color}'>██ █  █ ██</style>   输入 /help 获取帮助"
    avatar_row4 = f"  <style fg='{theme_color}'>▀██████▀</style>    "
    
    card = (
        f"<style fg='{theme_color}'>┌────────────────────────────────────────────────────────┐</style>\n"
        + make_box_line(avatar_row1)
        + make_box_line(avatar_row2)
        + make_box_line(avatar_row3)
        + make_box_line(avatar_row4)
        + make_box_line("")
        + make_data_line("   Directory : ", cwd_display)
        + make_data_line("   Session   : ", safe_session)
        + make_data_line("   Model     : ", model_display)
        + make_data_line("   Theme     : ", current_theme)
        + make_data_line("   Version   : ", f"v{YCODE_VERSION}")
        + make_box_line("")
        + make_box_line("   提示: 星瑶是您的编程伙伴，使用 <ansigreen>@文件</ansigreen> 可附带上下文。")
        + f"<style fg='{theme_color}'>└────────────────────────────────────────────────────────┘</style>\n"
    )
    
    print_formatted_text(HTML(card))
    print_formatted_text(HTML("<ansigray>   Enter 发送 | Ctrl+N 换行 | Ctrl+C 清空/中断 | Ctrl+D 退出 | Ctrl+T 记忆</ansigray>\n"))


def _resolve_session_after_selector(current_session_name, existed_before):
    """会话选择器关闭后校验当前会话。

    仅当会话“原本已入库、且现在不在列表中”（即在选择器里被物理删除）时才切回；
    未入库的新会话（首轮对话前没有数据库记录）按 Esc 返回时保持原样。
    返回 (session_name, was_deleted)。
    """
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
    # 切换/回放会话后屏幕被 logo + 历史块重新铺过，下一轮提问前不再补分隔线
    _turn_separator_needed = False
    history = manager.get_history(session_name)
    safe_session = session_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    from core.history_renderer import render_session_history
    rendered = render_session_history(history, _terminal_width())
    if not rendered:
        print_formatted_text(HTML(f"<ansiyellow>ℹ 会话 {safe_session} 当前没有任何对话记录。</ansiyellow>"))
        return

    print_formatted_text(HTML(f"\n<ansicyan><b>--- 会话 {safe_session} 历史对话记录 ---</b></ansicyan>"))
    _print_ui_fragment(rendered)
    print_formatted_text(HTML("<ansicyan><b>--------------------------------------</b></ansicyan>\n"))


def handle_slash_command(command_text):
    """斜杠指令路由器"""
    global current_session, current_model, _turn_separator_needed

    parts = command_text.strip().split()
    cmd = parts[0].lower()

    if cmd == "/help":
        print_formatted_text(HTML("<b><ansiyellow>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</ansiyellow></b>"))
        print_formatted_text(HTML("🌟 <b><ansigreen>Y-code CLI 系统指令与操作指南</ansigreen></b>"))
        print_formatted_text(HTML("<b><ansiyellow>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</ansiyellow></b>"))
        print_formatted_text(HTML("<b><ansiyellow>【1. 会话与上下文管理】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>/session</ansicyan>                             : 打开全屏会话管理器 UI (仅显示当前目录的会话，支持回车切换，Del 删除，R/F2改名)"))
        print_formatted_text(HTML("  <ansicyan>/session all</ansicyan>                         : 列出所有目录下的历史会话 (含归属路径与时间)"))
        print_formatted_text(HTML("  <ansicyan>/session rename &lt;新名称&gt;</ansicyan>          : 重命名当前活动会话"))
        print_formatted_text(HTML("  <ansicyan>/session delete &lt;会话名称&gt;</ansicyan>          : 物理删除指定会话（安全切回防御）"))
        print_formatted_text(HTML("  <ansicyan>/new</ansicyan>                                    : 创建并切换到全新会话（历史会话保留，/session 可切回）"))
        print_formatted_text(HTML("  <ansicyan>/undo</ansicyan>                                   : 撤销星瑶上一次修改的代码，并同步抹除模型记忆"))
        print_formatted_text(HTML("  <ansicyan>/checkpoint</ansicyan>                             : 会话级检查点管理 (list 查看 / restore &lt;turn_id&gt; 整轮回滚)"))
        print_formatted_text(HTML("  <ansicyan>/share</ansicyan>                                  : 导出当前会话为 Markdown/HTML/JSON（默认 md，可加参数）"))
        print_formatted_text(HTML("  <ansicyan>/worktree</ansicyan>                              : Git worktree 管理 (list/create/remove/prune)"))
        print_formatted_text(HTML("  <ansicyan>/clear</ansicyan>                                  : 清理终端当前屏幕"))
        print_formatted_text(HTML(""))
        print_formatted_text(HTML("<b><ansiyellow>【2. 模型与样式配置】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>/models</ansicyan>                              : 打开全屏模型管理器 UI (切换模型、API 密钥、API 地址)"))
        print_formatted_text(HTML("  <ansicyan>/effort &lt;low|high|max|default&gt;</ansicyan>      : 切换当前模型的推理强度档位 (按模型保存，下轮请求生效)"))
        print_formatted_text(HTML("  <ansicyan>/theme</ansicyan>                               : 列出当前所有可用的终端配色主题"))
        print_formatted_text(HTML("  <ansicyan>/theme &lt;主题名称&gt;</ansicyan>                    : 切换至指定主题 (支持: cyan, matrix, nord, cyberpunk, dracula, lava, sunset, amber, white)"))
        print_formatted_text(HTML("  <ansicyan>/permission &lt;confirm|plan|full&gt;</ansicyan>        : 切换安全权限级别 (confirm:变更前确认 | plan:计划模式 | full:完全访问)"))
        print_formatted_text(HTML("  <ansicyan>/sandbox &lt;on|off&gt;</ansicyan>                     : 沙盒开关 (on:允许访问工作目录外文件 | off:仅限工作目录内)"))
        print_formatted_text(HTML("  <ansicyan>/usage</ansicyan>                                : 查看 Token 用量统计 (按模型区分: 今日/近7天/近30天，精确数值)"))
        print_formatted_text(HTML("  <ansicyan>/usage &lt;日期&gt; [截止日期]</ansicyan>          : 查询指定单日或区间的 Token 用量 (例: /usage 2026-08-01 2026-08-05)"))
        print_formatted_text(HTML(""))
        print_formatted_text(HTML("<b><ansiyellow>【3. 技能插件管理】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>/skills</ansicyan>                              : 打开全屏技能选择器 UI (空格勾选开启/关闭)"))
        print_formatted_text(HTML("  <ansicyan>/skills list</ansicyan>                         : 在终端列出所有扫描到的技能，展示启用状态与短 ID"))
        print_formatted_text(HTML("  <ansicyan>/skills enable &lt;短ID&gt;</ansicyan>             : 启用指定短 ID 对应的技能"))
        print_formatted_text(HTML("  <ansicyan>/skills disable &lt;短ID&gt;</ansicyan>            : 禁用指定短 ID 对应的技能"))
        print_formatted_text(HTML("  <ansicyan>/&lt;短ID&gt; [提问内容]</ansicyan>                   : <b>主动且强制调用指定技能</b>让星瑶为您执行任务"))
        print_formatted_text(HTML("  <ansicyan>/plugins</ansicyan>                             : 查看已安装的进程内插件 (名称/版本/来源/加载状态/提供的工具)"))
        print_formatted_text(HTML("  <ansicyan>/plugins enable|disable &lt;name&gt;</ansicyan>        : 启用/禁用指定插件 (写回 plugin.json，重启后生效)"))
        print_formatted_text(HTML("  <ansicyan>/plugins reload</ansicyan>                       : 重新扫描插件目录刷新清单 (不重启)"))
        print_formatted_text(HTML("  <ansicyan>/mcp</ansicyan>                                 : MCP 服务管理 (open/status/tools/enable/disable/reconnect/edit)"))
        print_formatted_text(HTML(""))
        print_formatted_text(HTML("<b><ansiyellow>【4. 任务托管与后台进程】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>/tasks</ansicyan>                               : 查看或接管目前正在运行的后台长周期服务/命令"))
        print_formatted_text(HTML("  <ansicyan>/plan</ansicyan>                                : 查看当前任务计划与逐项状态"))
        print_formatted_text(HTML("  <ansicyan>/plan &lt;pause|resume|retry ID|cancel&gt;</ansicyan>       : 暂停、恢复、重试或取消计划"))
        print_formatted_text(HTML(""))
        print_formatted_text(HTML("<b><ansiyellow>【5. KAIROS 长期记忆系统】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>/remember &lt;内容&gt;</ansicyan>                     : 手动追加一条规则、偏好或事实到今日的记忆日志"))
        print_formatted_text(HTML("  <ansicyan>/dream</ansicyan>                                 : 在后台触发梦境总结，将所有记忆日志提炼归纳并更新索引"))
        print_formatted_text(HTML("  <ansicyan>/memory</ansicyan>                                : 快速查阅当前的长期记忆索引 (MEMORY.md) 内容"))
        print_formatted_text(HTML("  <ansicyan>/compress [force]</ansicyan>                      : 手动触发当前会话的记忆压缩 (保留最近若干条，更早的历史由大模型提炼为摘要；加 force 强制保留更少条数)"))
        print_formatted_text(HTML(""))
        print_formatted_text(HTML("<b><ansiyellow>【6. 终端常用快捷操作】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>@文件名</ansicyan>                               : <b>在提问中挂载本地文件</b> (例如: <ansigray>这行代码怎么改 @main.py</ansigray>)"))
        print_formatted_text(HTML("  <ansicyan>Enter</ansicyan>                                 : 提交当前文本给星瑶（闲置立即执行，繁忙则自动排队等待）"))
        print_formatted_text(HTML("  <ansicyan>Ctrl+N</ansicyan>                                    : 在输入框内输入新的一行换行符"))
        print_formatted_text(HTML("  <ansicyan>Ctrl+C</ansicyan>                                : <b>星瑶思考中时立即发送强行中断信号</b>；闲置时清空输入框"))
        print_formatted_text(HTML("  <ansicyan>Ctrl+T</ansicyan>                                : <b>切换展开/收起底部浮窗显示当前会话的背景记忆摘要</b>"))
        print_formatted_text(HTML("  <ansicyan>Shift+Tab</ansicyan>                             : <b>循环切换 Y-CODE 权限模式</b> (变更前确认 ➡️ 计划模式 ➡️ 完全访问)"))
        print_formatted_text(HTML("  <ansicyan>Ctrl+D / /exit</ansicyan>                        : 安全退出 Y-code CLI"))
        print_formatted_text(HTML("<b><ansiyellow>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</ansiyellow></b>"))

    elif cmd in ("/models", "/model"):
        # 不在后台线程里直接开选择器（会和主线程的 event loop 冲突）
        # 而是设一个标志，然后退出主 App，让主线程安全地开启选择器
        global pending_action
        pending_action = "open_models"
        if app_instance:
            app_instance.exit()

    elif cmd in ("/skills", "/skill"):
        from core.skills import scan_all_skills, get_active_skill_ids, save_active_skill_ids
        
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub == "list":
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
                        print_formatted_text(HTML(f"  {check} 短ID: <b><ansiyellow>{_esc(short_id)}</ansiyellow></b> | 名称: {_esc(s['name'])} - <ansigray>{_esc(s['description'])}</ansigray>"))
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
                print_formatted_text(HTML("  <ansicyan>/skills enable &lt;短ID&gt;</ansicyan>  : 启用指定技能"))
                print_formatted_text(HTML("  <ansicyan>/skills disable &lt;短ID&gt;</ansicyan> : 禁用指定技能"))
        else:
            pending_action = "open_skills"
            if app_instance:
                app_instance.exit()

    elif cmd == "/theme":
        from core.themes import THEMES
        if len(parts) > 1:
            theme_name = parts[1].lower().strip("<>\"'")
            if theme_name == "list":
                print_formatted_text(HTML("<b><ansicyan>可用主题配色方案：</ansicyan></b>"))
                for t in THEMES.keys():
                    desc, color = THEME_DESCS.get(t, ("未知", "#ffffff"))
                    print_formatted_text(HTML(f"  - <b>{t:<10}</b> ({desc:<4}) : <style fg='{color}'>████</style>"))
            elif theme_name in THEMES:
                from config import save_config_value
                save_config_value("theme", theme_name)
                global current_theme
                current_theme = theme_name
                print_formatted_text(HTML(f"<ansigreen>✔ 已切换配色主题为: <b>{theme_name}</b></ansigreen>"))
                # 不退出主 App（exit 且未设 pending_action 会导致整个程序退出），
                # current_theme 已更新，刷新一次让状态栏/边框立即反映新主题
                if app_instance:
                    app_instance.invalidate()
            else:
                available = ", ".join(THEMES.keys())
                print_formatted_text(HTML(f"<ansired>❌ 未知主题配色「{_esc(theme_name)}」，可选主题：{available}</ansired>"))
        else:
            print_formatted_text(HTML("<b><ansicyan>可用主题配色方案：</ansicyan></b>"))
            for t in THEMES.keys():
                desc, color = THEME_DESCS.get(t, ("未知", "#ffffff"))
                print_formatted_text(HTML(f"  - <b>{t:<10}</b> ({desc:<4}) : <style fg='{color}'>████</style>"))
            print_formatted_text(HTML("用法: <ansiyellow>/theme &lt;主题名称&gt;</ansiyellow>"))

    elif cmd == "/permission":
        if len(parts) > 1:
            mode_name = parts[1].lower().strip("<>\"'")
            if mode_name == "list":
                print_formatted_text(HTML("<b><ansicyan>可用权限模式：</ansicyan></b>"))
                for line in PERMISSION_HELP_LINES:
                    print_formatted_text(HTML(line))
            elif mode_name in ("confirm", "plan", "full"):
                from config import save_config_value
                global current_permission_mode
                # 统一经规范化后再落库与写内存，杜绝非规范值扩散
                current_permission_mode = _app_ctx.normalize_permission_mode(mode_name)
                save_config_value("permission_mode", current_permission_mode)
                mapping = {"confirm": "变更前确认", "plan": "计划模式", "full": "完全访问"}
                print_formatted_text(HTML(f"<ansigreen>✔ 权限控制模式已切换为: <b>{mapping[mode_name]}</b></ansigreen>"))
                if app_instance:
                    app_instance.invalidate()
            else:
                print_formatted_text(HTML(f"<ansired>❌ 未知权限模式「{_esc(mode_name)}」，可选模式：confirm, plan, full</ansired>"))
        else:
            print_formatted_text(HTML("<b><ansicyan>可用权限模式：</ansicyan></b>"))
            for line in PERMISSION_HELP_LINES:
                print_formatted_text(HTML(line))
            print_formatted_text(HTML("用法: <ansiyellow>/permission &lt;confirm|plan|full&gt;</ansiyellow>"))

    elif cmd == "/effort":
        # 推理强度按模型持久化；请求链路（core/llm.py）每轮新鲜读 config.json，改完下轮即生效
        from core.model_manager import load_config, get_model_reasoning_effort, set_model_reasoning_effort
        cfg = load_config()
        active_id = cfg.get("active_model_id")
        if not active_id:
            print_formatted_text(HTML("<ansired>❌ 当前没有激活模型，请先用 /models 配置模型。</ansired>"))
        elif len(parts) > 1:
            level = parts[1].lower().strip("<>\"'")
            ok, message = set_model_reasoning_effort(active_id, level)
            if ok:
                print_formatted_text(HTML(f"<ansigreen>✔ {_esc(message)}（下一轮请求生效）</ansigreen>"))
                litellm_model = ""
                for provider in cfg.get("providers", []):
                    for m in provider.get("models", []):
                        if m.get("id") == active_id:
                            litellm_model = str(m.get("litellm_model") or "")
                if litellm_model.split("/")[0].lower() == "anthropic":
                    print_formatted_text(HTML("<ansiyellow>ℹ 当前模型走 Anthropic 路由，reasoning_effort 不会注入请求（该路由请在配置中设置 thinking_budget）。</ansiyellow>"))
                if app_instance:
                    app_instance.invalidate()
            else:
                print_formatted_text(HTML(f"<ansired>❌ {_esc(message)}</ansired>"))
        else:
            current = get_model_reasoning_effort(active_id) or "default"
            print_formatted_text(HTML(f"<b><ansicyan>当前模型推理强度: {current}</ansicyan></b> (low 最快 / max 最深思考)"))
            print_formatted_text(HTML("用法: <ansiyellow>/effort &lt;low|high|max|default&gt;</ansiyellow>  (default = 恢复内置默认)"))

    elif cmd == "/sandbox":
        # 沙盒开关：控制文件工具是否可访问当前工作目录之外的路径
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
            status = "已开放（可访问工作目录外）" if enabled else "已开启（仅限工作目录内）"
            print_formatted_text(HTML(f"<b><ansicyan>沙盒状态：{status}</ansicyan></b>"))
            print_formatted_text(HTML("用法: <ansiyellow>/sandbox &lt;on|off&gt;</ansiyellow>（on=允许访问工作目录外，off=仅限工作目录内）"))

    elif cmd == "/new":
        # 创建全新会话并切换：历史会话全部保留，随时可用 /session 切回
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
                print_formatted_text(HTML(f"<ansigreen>✔ 会话 {safe_target} 已被成功删除。</ansigreen>"))

                # 如果删除的是当前活动会话，触发安全切回
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
                # 跨目录查看全部历史会话（含归属路径与最近活跃时间）
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
                # 重命名会话
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
            else:
                # 向后兼容：默认会话切换 /session <会话名>
                target = parts[1].strip("<>")
                # 确保目标会话在数据库中已存在，避免后续 get_history/计划绑定/压缩
                # 基于一个不存在的 session_name 工作，导致状态失配。
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
        from core.task_manager import list_active_tasks, get_task_output, kill_background_task
        from core.tools import subagent_manager

        if len(parts) > 1:
            subcmd = parts[1]
            if subcmd == "kill" and len(parts) > 2:
                tid = parts[2]
                cleaned_tid = tid.strip("<>")
                safe_tid = cleaned_tid.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                # 先尝试 shell 后台进程
                if kill_background_task(cleaned_tid):
                    print_formatted_text(HTML(f"<ansigreen>✔ 后台进程 {safe_tid} 已成功终结。</ansigreen>"))
                else:
                    # 再尝试子代理后台任务（cancel 而非 kill，协作式取消）
                    sub_tasks = subagent_manager.snapshot()
                    matched = [t for t in sub_tasks if t.task_id == cleaned_tid]
                    if matched and matched[0].status not in {"completed", "failed", "cancelled", "timed_out"}:
                        subagent_manager.cancel(cleaned_tid, "用户通过 /tasks kill 终止")
                        print_formatted_text(HTML(f"<ansigreen>✔ 子代理任务 {safe_tid} 已请求取消。</ansigreen>"))
                    else:
                        print_formatted_text(HTML(f"<ansired>❌ 找不到任务 ID: {safe_tid}，或任务已经提前结束。</ansired>"))
            elif subcmd in ("tail", "log", "status") and len(parts) > 2:
                tid = parts[2]
                cleaned_tid = tid.strip("<>")
                safe_tid = cleaned_tid.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                # 先查 shell 后台进程日志；返回的 🚨 错误信息不算有效日志，
                # 必须继续查子代理任务状态，避免对 subagent-* ID 显示"未找到日志"
                output = get_task_output(cleaned_tid, tail_lines=30)
                shell_log_available = bool(
                    output
                    and output.strip()
                    and not output.strip().startswith("🚨")
                )
                if shell_log_available:
                    print_formatted_text(HTML(f"<ansicyan><b>--- 任务 {safe_tid} 最新 30 行日志 ---</b></ansicyan>"))
                    print(output)
                else:
                    # 再查子代理任务状态
                    sub_tasks = subagent_manager.snapshot()
                    matched = [t for t in sub_tasks if t.task_id == cleaned_tid]
                    if matched:
                        t = matched[0]
                        print_formatted_text(HTML(f"<ansicyan><b>--- 子代理任务 {safe_tid} 状态 ---</b></ansicyan>"))
                        status_color = {"completed": "ansigreen", "failed": "ansired",
                                       "cancelled": "ansigray", "timed_out": "ansired"}.get(t.status, "ansiyellow")
                        print_formatted_text(HTML(f"  状态: <{status_color}>{t.status}</{status_color}> | 类型: {t.subagent_type} | 阶段: {t.stage}"))
                        print_formatted_text(HTML(f"  执行: {round(t.execution_elapsed, 1)}s | 等待审批: {round(t.approval_wait_elapsed, 1)}s"))
                        desc_safe = (t.description or "")[:80].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        print_formatted_text(HTML(f"  描述: {desc_safe}"))
                        if t.result:
                            result_safe = (t.result or "")[:500].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                            print_formatted_text(HTML(f"  <ansigray>结果摘要: {result_safe}</ansigray>"))
                        if t.error:
                            error_safe = (t.error or "")[:500].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                            print_formatted_text(HTML(f"  <ansired>错误: {error_safe}</ansired>"))
                    else:
                        # shell 日志返回错误时直接展示错误信息，否则提示无日志
                        if output and output.strip():
                            print_formatted_text(HTML(f"<ansiyellow>{_esc(output.strip())}</ansiyellow>"))
                        else:
                            print_formatted_text(HTML(f"<ansiyellow>ℹ 任务 {safe_tid} 没有可用日志或状态信息。</ansiyellow>"))
            else:
                print_formatted_text(HTML("<ansired>❌ 用法错误。可选指令：</ansired>"))
                print_formatted_text(HTML("  <ansicyan>/tasks</ansicyan>           : 列出所有后台任务"))
                print_formatted_text(HTML("  <ansicyan>/tasks kill &lt;ID&gt;</ansicyan> : 终结指定后台任务"))
                print_formatted_text(HTML("  <ansicyan>/tasks tail &lt;ID&gt;</ansicyan> : 查看任务日志输出"))
        else:
            # 同时查询 shell 后台进程和子代理后台任务
            shell_tasks = list_active_tasks()
            sub_snapshots = subagent_manager.snapshot()
            # 只显示后台子代理任务（前台子代理在状态栏已有实时显示）
            bg_sub_snapshots = [t for t in sub_snapshots if t.run_in_background]

            if not shell_tasks and not bg_sub_snapshots:
                print_formatted_text(HTML("<ansiyellow>ℹ 当前没有运行中的后台异步任务。</ansiyellow>"))
            else:
                if shell_tasks:
                    print_formatted_text(HTML("<ansicyan><b>当前运行的后台进程（shell）：</b></ansicyan>"))
                    for t in shell_tasks:
                        safe_cmd = t['command'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        print_formatted_text(HTML(f"  <ansigreen>• ID: {t['task_id']}</ansigreen> | PID: <ansicyan>{t['pid']}</ansicyan> | 运行: {t['duration']} | 命令: {safe_cmd}"))
                if bg_sub_snapshots:
                    if shell_tasks:
                        print_formatted_text(HTML(""))
                    print_formatted_text(HTML("<ansicyan><b>当前运行的后台子代理：</b></ansicyan>"))
                    for t in bg_sub_snapshots:
                        status_color = {"completed": "ansigreen", "failed": "ansired",
                                       "cancelled": "ansigray", "timed_out": "ansired",
                                       "in_progress": "ansiyellow", "running": "ansiyellow"}.get(t.status, "ansigray")
                        desc_safe = (t.description or "")[:50].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        print_formatted_text(HTML(
                            f"  <ansigreen>• ID: {t.task_id}</ansigreen> | 状态: <{status_color}>{t.status}</{status_color}> "
                            f"| 类型: <ansicyan>{t.subagent_type}</ansicyan> | 阶段: {t.stage} "
                            f"| 执行: {round(t.execution_elapsed, 1)}s | 描述: {desc_safe}"
                        ))
                print_formatted_text(HTML("<ansigray>提示：你可以输入 `/tasks tail &lt;ID&gt;` 查看日志/状态，或 `/tasks kill &lt;ID&gt;` 终结任务。</ansigray>"))

    elif cmd in ("/plan", "/plans"):
        from core.task_scheduler import get_default_scheduler

        scheduler = get_default_scheduler()
        if len(parts) == 1:
            items = get_default_plan_manager().snapshot()
            if not items:
                print_formatted_text(HTML("<ansiyellow>ℹ 当前会话没有任务计划。</ansiyellow>"))
            else:
                completed = sum(item.get("status") == "completed" for item in items)
                print_formatted_text(HTML(f"<ansicyan><b>当前任务计划 ({completed}/{len(items)})</b></ansicyan>"))
                for item in items:
                    marker = plan_mark(item.get("status"))
                    color = plan_html_color(item.get("status"))
                    subject = _esc(item.get("subject", ""))
                    print_formatted_text(HTML(f"  <{color}>{marker}</{color}> {_esc(item['id'])}  {subject}"))
        else:
            action = parts[1].lower()
            if action == "pause":
                scheduler.pause()
                print_formatted_text(HTML("<ansiyellow>⏸ 任务计划已暂停，不再启动新的计划项。</ansiyellow>"))
            elif action == "resume":
                scheduler.resume()
                print_formatted_text(HTML("<ansigreen>▶ 任务计划已恢复。</ansigreen>"))
            elif action == "retry" and len(parts) > 2:
                item_id = parts[2].strip("<>")
                if scheduler.retry(item_id):
                    print_formatted_text(HTML(f"<ansigreen>↻ 已重新排队计划项：{_esc(item_id)}</ansigreen>"))
                else:
                    print_formatted_text(HTML(f"<ansired>❌ 计划项 {_esc(item_id)} 当前不可重试。</ansired>"))
            elif action == "cancel":
                count = scheduler.cancel_all()
                print_formatted_text(HTML(f"<ansiyellow>⏹ 已取消 {count} 个未完成计划项。</ansiyellow>"))
            else:
                print_formatted_text(HTML("<ansired>❌ 用法：/plan、/plan pause、/plan resume、/plan retry &lt;id&gt;、/plan cancel</ansired>"))

    elif cmd == "/undo":
        # 撤销的实际逻辑在 core/agent_service.undo_last_edit（与 GUI 共用），
        # 这里只负责"删除新建文件"的二次确认与终端提示。
        from core.memory import get_undo_state

        state = get_undo_state(current_session)
        file_path = state.get("file_path")
        old_content = state.get("old_content")
        # 删除新建文件是高危操作：要求显式确认，避免跨会话/误操作导致用户数据丢失
        will_delete = bool(file_path) and old_content == ""
        confirm = command_text.strip().split()
        forced = len(confirm) >= 2 and confirm[1].lower() in ("y", "yes", "--force")
        if will_delete and not forced:
            print_formatted_text(
                HTML(f"<ansiyellow>⚠️ 该操作将删除星瑶新建的文件 {_esc(file_path)}，"
                     f"请在命令后追加 y/yes/--force 确认，例如：/undo y</ansiyellow>")
            )
            return

        ok, message = _agent_service.undo_last_edit(manager, current_session, force=forced)
        if ok:
            print_formatted_text(HTML(f"<ansigreen>↩️ {_esc(message)}</ansigreen>"))
            recalc_token_usage()  # 撤销后重新计算 token
            print_formatted_text(HTML("<ansigreen>🧠 已同步抹除星瑶对刚才操作的记忆，防止幻觉复发。</ansigreen>"))
        else:
            print_formatted_text(HTML(f"<ansired>⚠️ {_esc(message)}</ansired>"))

    elif cmd == "/checkpoint" or cmd.startswith("/checkpoint "):
        # 会话级检查点管理：list 查看、restore <turn_id> 回滚整轮所有文件修改
        # 设计参考 Grok Build 的 workspace 级 checkpoint
        from core.memory import list_checkpoints, restore_checkpoint
        parts = command_text.strip().split(maxsplit=2)
        sub = parts[1] if len(parts) > 1 else "list"

        if sub == "list":
            checkpoints = list_checkpoints(current_session)
            if not checkpoints:
                print_formatted_text(HTML("<ansiyellow>ℹ 当前会话没有检查点记录。</ansiyellow>"))
                print_formatted_text(HTML("<ansigray>  每轮对话开始前会自动打一个检查点，记录本轮所有文件修改的旧内容。</ansigray>"))
                print_formatted_text(HTML("<ansigray>  使用 /checkpoint restore &lt;turn_id&gt; 可整体回滚到该检查点。</ansigray>"))
            else:
                print_formatted_text(HTML(f"<ansicyan>📋 当前会话共有 {len(checkpoints)} 个检查点:</ansicyan>"))
                for cp in checkpoints:
                    desc = cp.get("description", "") or "(无描述)"
                    if len(desc) > 50:
                        desc = desc[:47] + "..."
                    n_files = len(cp.get("file_snapshots", []))
                    print_formatted_text(HTML(
                        f"  <ansigreen>{_esc(cp['turn_id'])}</ansigreen> "
                        f"<ansigray>{_esc(cp['created_at'])}</ansigray> "
                        f"文件修改: {n_files}  "
                        f"<ansigray>{_esc(desc)}</ansigray>"
                    ))
                print_formatted_text(HTML("<ansigray>\n  使用 /checkpoint restore &lt;turn_id&gt; 整体回滚该轮所有文件修改</ansigray>"))

        elif sub == "restore":
            if len(parts) < 3:
                print_formatted_text(HTML("<ansired>⚠️ 用法: /checkpoint restore &lt;turn_id&gt;</ansired>"))
            else:
                target_turn_id = parts[2].strip()
                snapshots = restore_checkpoint(current_session, target_turn_id)
                if snapshots is None:
                    print_formatted_text(HTML(f"<ansired>⚠️ 未找到检查点: {_esc(target_turn_id)}</ansired>"))
                    print_formatted_text(HTML("<ansigray>  使用 /checkpoint list 查看所有可用的检查点 ID</ansigray>"))
                elif not snapshots:
                    print_formatted_text(HTML(f"<ansiyellow>ℹ 检查点 {_esc(target_turn_id)} 没有文件修改记录（该轮没有写/编辑操作）。</ansiyellow>"))
                else:
                    print_formatted_text(HTML(f"<ansicyan>🔄 正在回滚检查点 {_esc(target_turn_id)}，共 {len(snapshots)} 个文件...</ansicyan>"))
                    from core.tools import write_file, sandbox_bypass
                    success = 0
                    failed = 0
                    for snap in snapshots:
                        try:
                            file_path = snap["file_path"]
                            old_content = snap["old_content"]
                            if old_content == "":
                                # 原本不存在该文件，删除
                                if os.path.exists(file_path):
                                    os.remove(file_path)
                                print_formatted_text(HTML(f"  <ansigreen>✔ 删除新建文件: {_esc(file_path)}</ansigreen>"))
                            else:
                                # 回滚是用户显式发起的恢复操作：沙盒外路径也直接放行，
                                # 避免之前单次批准写出的沙盒外文件无法回滚
                                with sandbox_bypass():
                                    write_file(file_path, old_content)
                                print_formatted_text(HTML(f"  <ansigreen>✔ 恢复文件: {_esc(file_path)}</ansigreen>"))
                            success += 1
                        except Exception as e:
                            print_formatted_text(HTML(f"  <ansired>✗ 恢复失败 {_esc(snap.get('file_path', '?'))}: {_esc(e)}</ansired>"))
                            failed += 1
                    print_formatted_text(HTML(
                        f"<ansigreen>✅ 检查点回滚完成：成功 {success} 个，失败 {failed} 个。</ansigreen>"
                    ))
                    if success > 0:
                        recalc_token_usage()

        else:
            print_formatted_text(HTML("<ansired>⚠️ 未知子命令。用法: /checkpoint [list|restore &lt;turn_id&gt;]</ansired>"))

    elif cmd == "/share" or cmd.startswith("/share "):
        # 会话导出（参考 OpenCode 的 share 模块）
        # 用法: /share [md|html|json]
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

    elif cmd == "/worktree" or cmd.startswith("/worktree "):
        # Git worktree 管理（参考 OpenCode 的 worktree 模块）
        # 用法: /worktree list / /worktree create <branch> <path> / /worktree remove <path>
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

    elif cmd == "/mcp" or cmd.startswith("/mcp "):
        # MCP 服务管理：默认打开全屏 TUI 配置器，也支持子命令
        # 用法: /mcp                  -> 打开全屏 TUI 配置器（推荐）
        #       /mcp status           -> 查看连接状态（轻量，不开全屏）
        #       /mcp tools            -> 列出已注册 MCP 工具
        #       /mcp enable <name>    -> 启用某 server
        #       /mcp disable <name>   -> 禁用某 server
        #       /mcp reconnect        -> 运行期重连
        #       /mcp edit             -> 用记事本打开 mcp.json
        from core.mcp_client import mcp_manager
        parts = command_text.strip().split(maxsplit=2)
        # 不带子命令时默认打开全屏 TUI 配置器
        sub = parts[1].strip() if len(parts) > 1 else "open"

        if sub == "open":
            # 打开全屏 TUI 配置器：退出主 App，由主循环在主线程安全开启
            # 注意：pending_action 已在函数顶部 /models 分支声明为 global，这里直接赋值
            pending_action = "open_mcp"
            if app_instance:
                app_instance.exit()

        elif sub == "status":
            # 触发一次惰性连接尝试（不阻塞过久），再读取状态
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
                # 触发一次 schema 拉取以刷新 tool_schemas（连接成功后会自动更新）
                try:
                    mcp_manager.get_schemas()
                except Exception:
                    pass
                for name, conf in config.items():
                    # 读取 enabled 字段：未设置或非 false 都视为已启用（向后兼容）
                    enabled = True
                    if isinstance(conf, dict):
                        enabled = conf.get("enabled", True) is not False
                    # 使用与 /skills 一致的 [✓]/[ ] 勾选样式
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
                # 按服务名分组
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
            # /mcp enable <name>：启用某个 MCP server（写回 mcp.json + 重连）
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
            # /mcp disable <name>：禁用某个 MCP server（写回 mcp.json + 重连，配置保留）
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
            # /mcp edit：用记事本打开 mcp.json（Windows 走 notepad，其他平台走 vi）
            config_path = mcp_manager.config_file
            print_formatted_text(HTML(f"<ansicyan>📝 正在打开配置文件: {_esc(config_path)}</ansicyan>"))
            print_formatted_text(HTML("<ansigray>  编辑保存后，回到 TUI 运行 /mcp reconnect 让改动生效。</ansigray>"))
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
        # 进程内插件清单与开关管理
        # 用法: /plugins                -> 查看清单
        #       /plugins enable <name>  -> 启用某插件
        #       /plugins disable <name> -> 禁用某插件
        #       /plugins reload         -> 重新扫描插件目录刷新清单
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
                    "<ansigray>  使用 /plugins enable|disable &lt;插件名&gt; 切换开关（重启后生效），/plugins reload 刷新清单。</ansigray>"
                ))
        else:
            print_formatted_text(HTML(
                "<ansired>⚠️ 未知子命令。用法: /plugins [enable &lt;name&gt;|disable &lt;name&gt;|reload]</ansired>"
            ))

    elif cmd == "/usage" or cmd.startswith("/usage "):
        # Token 用量统计：按模型分桶，精确数值（不做缩写）
        # 用法: /usage                            -> 今日/近7天/近30天
        #       /usage 2026-08-01                 -> 指定单日
        #       /usage 2026-08-01 2026-08-05      -> 指定区间（也支持 ~ 分隔）
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
        # 清屏后屏幕上没有内容，下一轮提问前不打印轮次分隔线
        _turn_separator_needed = False
        print_logo()

    elif cmd == "/dream":
        print_formatted_text(HTML("<ansiyellow>✨ 星瑶正在闭上眼睛，在后台慢慢提炼归纳咱们的长期记忆... 梦境整理中... 🛌</ansiyellow>"))
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
                    # compress_session_memory 在"已在压缩中"或"对话过少"时返回 False
                    # emit 已经输出过对应提示，这里不重复打印
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

    elif cmd == "/exit":
        # 使用 print_formatted_text 避免 rich 在 prompt_toolkit patch_stdout 下出现 ANSI 乱码
        print_formatted_text(HTML("<ansigreen>👋 星瑶去休息啦，下次见！</ansigreen>"))
        if app_instance:
            app_instance.exit()
    else:
        # 使用 print_formatted_text 避免 rich 在 prompt_toolkit patch_stdout 下出现 ANSI 乱码
        safe_cmd = cmd.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        print_formatted_text(HTML(f"<ansired>❌ 未知指令: {safe_cmd}，输入 /help 查看帮助。</ansired>"))


def _make_input_app():
    """
    构建 Claude Code 风格的输入框：
    ┌──────────────────────────────────────┐
    │ >                                    │
    └──────────────────────────────────────┘
     model · session · dir     快捷键提示
    """
    COMMAND_COMPLETIONS = [
        ("/help", "查看系统指令说明"),
        ("/session", "切换或创建新的记忆会话"),
        ("/new", "创建并切换到全新会话"),
        ("/models", "打开模型管理器"),
        ("/effort", "切换当前模型推理强度 (low/high/max/default)"),
        ("/usage", "查看 Token 用量统计 (按模型: 今日/近7天/近30天，可加日期/区间)"),
        ("/tasks", "查看或托管后台长周期进程"),
        ("/plan", "查看或控制当前任务计划"),
        ("/undo", "撤销星瑶上一次修改的代码"),
        ("/checkpoint", "会话级检查点 (list/restore 整轮回滚)"),
        ("/share", "导出会话为 Markdown/HTML/JSON"),
        ("/compress", "手动触发会话记忆压缩 (force 强制保留更少条)"),
        ("/worktree", "Git worktree 管理 (list/create/remove)"),
        ("/mcp", "MCP 服务管理 (status/tools/enable/disable/reconnect/edit)"),
        ("/plugins", "插件管理 (查看清单/enable|disable 禁启用/reload 刷新)"),
        ("/skills", "查看或安装/开启/禁用技能插件"),
        ("/theme", "切换配色主题 (Nord, Cyberpunk, Matrix等)"),
        ("/permission", "切换安全权限级别 (confirm/plan/full)"),
        ("/sandbox", "沙盒开关 (on:允许访问工作目录外/off:仅限目录内)"),
        ("/clear", "清理当前屏幕"),
        ("/exit", "退出 Y-code"),
    ]

    class SlashCommandCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            
            # 1. 尝试匹配斜杠命令
            if text.startswith('/'):
                word = document.get_word_before_cursor(WORD=True)
                for cmd, desc in COMMAND_COMPLETIONS:
                    if cmd.startswith(word):
                        yield Completion(
                            cmd, 
                            start_position=-len(word),
                            display_meta=desc
                        )
                        
                # 额外动态补充已开启的技能插件到下拉菜单供主动调用
                try:
                    from core.skills import get_active_skills_list
                    active_skills = get_active_skills_list()
                    for short_id, s in active_skills.items():
                        cmd = f"/{short_id}"
                        if cmd.startswith(word):
                            yield Completion(
                                cmd,
                                start_position=-len(word),
                                display_meta=f"⚡ 技能: {s['description']}"
                            )
                except Exception:
                    pass
                        
            # 2. 尝试匹配 @文件（目录清单走 TTL 缓存，避免大仓库每次按键都 os.walk 卡 UI）
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

    # 输入历史：跨会话持久化，Up/Down 可翻阅之前输入过的内容
    try:
        _hist_dir = os.path.join(os.path.expanduser("~"), ".ycode")
        os.makedirs(_hist_dir, exist_ok=True)
        _input_history = FileHistory(os.path.join(_hist_dir, "input_history.txt"))
    except Exception:
        _input_history = None

    input_area = TextArea(
        multiline=True,
        wrap_lines=True,
        # 固定 1 行：超过的内容在框内滚动查看
        height=D.exact(1),
        style="class:input-field",
        focus_on_click=True,
        read_only=Condition(lambda: confirm_prompt is not None),
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        history=_input_history,
    )

    kb = KeyBindings()

    # ─── 输入历史翻阅：光标在首行时按 ↑ 翻上一条，末行时按 ↓ 翻下一条 ───
    # （光标在中间行时 ↑/↓ 仍是正常的行间移动；确认框弹出时由确认框接管）
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
            idx = confirm_prompt['current_idx']
            if _resolve_confirm_prompt(idx):
                event.app.invalidate()
            return
            
        display_text = input_area.text.strip()
        text = _paste_store.expand(display_text)
        if text:
            # 写入输入历史（供 ↑/↓ 翻阅）；失败不影响提交。
            # 历史里存展开后的完整原文（而非占位符），↑ 翻出来可直接复用。
            try:
                if display_text != text:
                    input_area.text = text
                input_area.buffer.append_to_history()
            except Exception:
                pass
            if text.startswith('/'):
                # 检查是否是主动调用激活的技能插件快捷指令（形如 /chembl_database hello）
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
                    # 主动调用技能，放入后台队列处理
                    was_queued = is_thinking
                    safe_input = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    if was_queued:
                        _echo_user_input(f"✨ <b><cyan>You &gt;</cyan></b> <ansigray>(排队等待中...) {safe_input}</ansigray>")
                    else:
                        _echo_user_input(f"✨ <b><cyan>You &gt;</cyan></b> {safe_input}")
                        
                    msg_queue.put({"text": text, "was_queued": was_queued, "is_skill_call": True})
                    input_area.text = ""
                else:
                    # 正常的系统命令：设计上有意在 UI 线程同步执行（/session delete、
                    # /undo、/memory 等可能短暂阻塞 UI，但保证命令与用户输入严格按序执行），
                    # 因此后台 worker 里不再有 slash 路由分支。
                    input_area.text = ""
                    safe_input = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    _echo_user_input(f"✨ <b><cyan>You &gt;</cyan></b> {safe_input}")
                    try:
                        handle_slash_command(text)
                    except Exception as e:
                        print_formatted_text(
                            HTML(f"\n<ansired>⚠️ 命令执行出错：{_esc(str(e))}</ansired>\n")
                        )
            else:
                was_queued = is_thinking
                safe_input = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                if was_queued:
                    # 立即打印置灰的排队等待提示
                    _echo_user_input(f"✨ <b><cyan>You &gt;</cyan></b> <ansigray>(排队等待中...) {safe_input}</ansigray>")
                else:
                    # 正常的青色高亮提示
                    _echo_user_input(f"✨ <b><cyan>You &gt;</cyan></b> {safe_input}")
                
                msg_queue.put({"text": text, "was_queued": was_queued})
                input_area.text = ""

    @kb.add("c-n")
    def newline(event):
        """Ctrl+N 换行"""
        event.current_buffer.insert_text("\n")

    @kb.add("c-c")
    def clear_input(event):
        """Ctrl+C: 如果星瑶正在执行，则中断执行；如果闲置，则清空输入框"""
        global is_thinking, confirm_prompt
        if is_thinking or active_cancel_event is not None or confirm_prompt is not None:
            _cancel_active_turn()
            # 同时丢弃排队中的消息：用户按 Ctrl+C 的意图是"全部停下"，
            # 只取消当前轮而继续执行队列里的旧消息会让人困惑。
            drained = 0
            try:
                while True:
                    msg_queue.get_nowait()
                    drained += 1
            except queue.Empty:
                pass
            if drained:
                print_formatted_text(HTML(
                    f"\n<ansired>🛑 收到中断信号，正在终止星瑶的执行...（已丢弃 {drained} 条排队消息）</ansired>"
                ))
            else:
                print_formatted_text(HTML("\n<ansired>🛑 收到中断信号，正在终止星瑶的执行...</ansired>"))
            if confirm_prompt is not None and not confirm_prompt.get("request_id"):
                # future 型确认框（非 broker）：显式以最后一个选项表达“取消/停止”，
                # 不再硬编码索引 1
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
        if confirm_prompt:
            confirm_prompt['current_idx'] = (confirm_prompt['current_idx'] - 1) % len(confirm_prompt['options'])
            event.app.invalidate()

    @kb.add("down", filter=Condition(lambda: confirm_prompt is not None))
    def confirm_arrow_down(event):
        global confirm_prompt
        if confirm_prompt:
            confirm_prompt['current_idx'] = (confirm_prompt['current_idx'] + 1) % len(confirm_prompt['options'])
            event.app.invalidate()

    # y/n/a 快捷键只对权限确认框（带 request_id 的 broker 请求）生效：
    # ask_user 的选项是任意文案（如"方案A/方案B"），y/n 与其毫无对应关系，
    # 误触会静默选中错误选项。ask_user 框请用 ↑/↓ + Enter 选择。
    _broker_confirm_active = Condition(
        lambda: confirm_prompt is not None and bool(confirm_prompt.get("request_id"))
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
        """Shift+Tab 循环切换 Y-CODE 权限模式"""
        global current_permission_mode
        modes = ["confirm", "plan", "full"]
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
        """底部状态栏内容：极简无底色，带 Token 统计与后台任务监控"""
        # 1. 提取短模型名（使用内存缓存，避免每次渲染都读盘解析 config.json）
        model_display = current_model_name or "未激活模型"

        # 2. 计算 Token 消耗与缓存命中（分离概念）；与 worker 并发读写，必须持锁
        with token_usage_lock:
            cum_prompt = session_token_usage.get("cumulative_prompt", 0)
            cum_completion = session_token_usage.get("cumulative_completion", 0)
            cum_cached = session_token_usage.get("cumulative_cached", 0)
            latest_total = session_token_usage["latest_total"]
        max_win = current_ctx_win

        pct = (latest_total / max_win * 100) if max_win > 0 else 0
        
        def format_num(n):
            if n >= 1_000_000:
                val = n / 1_000_000
                return f"{int(val)}M" if val.is_integer() else f"{val:.1f}M"
            if n >= 1_000:
                val = n / 1_000
                return f"{int(val)}K" if val.is_integer() else f"{val:.1f}K"
            return str(n)
            
        latest_total_str = format_num(latest_total)
        max_win_str = format_num(max_win)

        token_info = _format_token_usage_summary(
            cum_prompt, cum_completion, cum_cached
        ) + f" · 上下文 {pct:.0f}% ({latest_total_str}/{max_win_str})"
        from core.cache_keepalive import cache_keepalive_service
        keepalive_status = cache_keepalive_service.status()
        if keepalive_status["enabled"]:
            token_info += f" · 保活 {keepalive_status['interval']}s"
        elif keepalive_status["error"]:
            token_info += f" · 保活已停止: {keepalive_status['error']}"

        # 3. 统计星瑶的执行状态与播放动画，证明星瑶仍在操作/思考
        # 直接读模块全局变量，不再 import main 自引用
        status_map = {
            "idle": (" 闲置 ", "class:status.task_idle"),
            "thinking": (" 思考中... ", "class:status.task_active"),
            "executing": (" 正在干活... ", "class:status.task_active"),
            "waiting": (" 等待授权确认... ", "class:status.task_active"),
        }
        status_label, task_style = status_map.get(task_status, (" 闲置 ", "class:status.task_idle"))

        if task_status != "idle":
            # 播放流式旋转动画
            elapsed = time.time()
            frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            frame = frames[int(elapsed * 5) % len(frames)]
            task_info = f" {frame}{status_label}"
        else:
            task_info = status_label

        # 2.5 渲染权限模式标识
        perm_style = "class:status.permission_confirm"
        perm_label = " 变更前确认 "
        if current_permission_mode == "confirm":
            perm_label = " 变更前确认 "
            perm_style = "class:status.permission_confirm"
        elif current_permission_mode == "auto":
            # auto 由 GUI 侧设置并共享同一份 config，TUI 至少要如实显示当前状态，
            # 避免用户在 GUI 切成"自动接受编辑"后回到 TUI 仍看到"变更前确认"
            perm_label = " 自动接受编辑 "
            perm_style = "class:status.permission_plan"
        elif current_permission_mode == "plan":
            perm_label = " 计划模式 "
            perm_style = "class:status.permission_plan"
        elif current_permission_mode == "full":
            perm_label = " 完全访问 "
            perm_style = "class:status.permission_full"

        return [
            ("class:status.model",   f" {model_display} "),
            ("class:status.sep",     "  |  "),
            (perm_style,             perm_label),
            ("class:status.sep",     "  |  "),
            ("class:status.token",   token_info),
            ("class:status.sep",     "  |  "),
            (task_style,             task_info),
        ]

    def get_activity_text():
        """渲染独立区域的执行中状态。"""
        if is_thinking:
            elapsed = max(0.0, time.time() - activity_start_time)
            # 帧下标与状态栏的旋转动画用同一套公式（基于绝对时间而非本地
            # elapsed），避免两个动画刷新率不一致导致的观感卡顿。
            frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            frame = frames[int(time.time() * 5) % len(frames)]
            # 首轮对话赶在后台预热完成前发出时，litellm/tiktoken 的首次
            # import 需要数秒——如实显示“启动模型引擎”，避免看似卡死。
            if not _llm_engine_ready and _active_tool_status is None:
                return f"  {frame} 星瑶正在启动模型引擎 · {max(0.0, elapsed):.1f}s · Ctrl+C 中断"
            return _format_activity_text(task_status, _active_tool_status, elapsed, frame)
        return ""

    @Condition
    def is_live_thinking_cond():
        # 思考中，或思考刚结束但面板延迟显示最后一帧内容（直到工具/文字到来）
        return task_status == "thinking" or _thinking_panel_linger

    @Condition
    def is_working_cond():
        return is_thinking and task_status != "thinking" and not _thinking_panel_linger

    def get_thinking_cursor_position():
        text = _thinking_panel_text()
        lines = text.split("\n")
        return Point(x=len(lines[-1]), y=len(lines) - 1)

    # 确认面板内容生成器
    def get_confirm_prompt_content():
        return HTML(_confirm_prompt_text(confirm_prompt))

    confirm_window = ConditionalContainer(
        content=Frame(
            Window(
                content=FormattedTextControl(get_confirm_prompt_content),
                height=D(min=6, max=9),
                wrap_lines=True
            ),
            title="🚨 敏感操作授权确认 (用方向键选择，回车确认)",
            style="class:status.confirm-frame"
        ),
        filter=Condition(lambda: confirm_prompt is not None)
    )

    # 记忆与任务拆解面板内容生成器
    def get_memory_summary_content():
        summary = manager.get_memory_summary(current_session) or "暂无会话记忆摘要。"
        
        from core.plan import get_default_plan_manager
        plan_text = _format_plan_items(get_default_plan_manager().snapshot())
                    
        display_text = f"💡 [会话记忆摘要]\n  {summary}\n"
        if plan_text:
            display_text += "\n📋 [当前任务规划]\n" + plan_text
        else:
            display_text += "\n📋 [当前任务规划]\n  暂无计划步骤 (可在对话中输入以产生任务)"
            
        return display_text

    def get_live_plan_content():
        from core.plan import get_default_plan_manager

        return ANSI(_format_live_plan_items(get_default_plan_manager().snapshot()))

    def _has_active_plan_items() -> bool:
        """判断是否有未完成的计划项（pending/in_progress）。

        所有项都进入终态（completed/failed/blocked/cancelled）时返回 False，
        避免已完成的计划长期占用状态栏。注意 blocked 在 plan.py 的
        TERMINAL_STATUSES 中属于终态，不应算作 active。
        """
        from core.plan import TERMINAL_STATUSES
        items = get_default_plan_manager().snapshot()
        if not items:
            return False
        return any(item.get("status") not in TERMINAL_STATUSES for item in items)

    live_plan_window = ConditionalContainer(
        content=Frame(
            Window(
                content=FormattedTextControl(get_live_plan_content),
                height=D(min=2, max=10),
                wrap_lines=True,
            ),
            title="📋 当前任务计划",
            style="class:status.live-plan-frame",
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
        content=Frame(
            Window(
                content=FormattedTextControl(get_memory_summary_content),
                height=6,
                wrap_lines=True
            ),
            title="🧠 会话记忆与任务拆解面板 (Ctrl+T 折叠/展开)",
            style="class:status.memory-frame"
        ),
        filter=Condition(lambda: show_memory_summary)
    )

    root_container = HSplit([
        confirm_window,
        live_plan_window,
        memory_window,
        ConditionalContainer(
            content=Frame(
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
                title="星瑶正在思考",
                style="class:status.thinking-frame",
            ),
            filter=is_live_thinking_cond,
        ),
        ConditionalContainer(
            content=Window(
                content=FormattedTextControl(get_activity_text),
                # 活动标题与安全命令摘要各占一行。
                height=2,
                style="class:status.think"
            ),
            filter=is_working_cond
        ),
        # 弹性占位：吸收 HSplit 的所有剩余高度（非全屏模式下布局高度只增不减，
        # 思考面板消失后多出的行会灌进唯一无 max 上限的容器）。把它放在输入框上方，
        # 输入框+状态栏就能始终钉在布局区域底部，不随思考面板显隐而跳动。
        # weight 设大，防止思考面板 Frame（同样无 max 上限）分走空间被撑高。
        Window(height=D(min=0, weight=100)),
        Window(
            content=FormattedTextControl(
                lambda: [("class:input-line", "─" * _terminal_width() + "\n")]
            ),
            height=1,
            style="class:input-line",
        ),
        VSplit([
            Window(
                content=FormattedTextControl([("class:input-prefix", "> ")]),
                width=D.exact(2),
                style="class:input-field",
            ),
            input_area,
        ]),
        Window(
            content=FormattedTextControl(
                lambda: [("class:input-line", "─" * _terminal_width() + "\n")]
            ),
            height=1,
            style="class:input-line",
        ),
        Window(
            content=FormattedTextControl(get_status),
            height=1,
        ),
    ])
    
    # 真正的下拉菜单容器
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

    from core.themes import THEMES
    theme_cfg = THEMES.get(current_theme, THEMES["cyan"])
    border_color = theme_cfg.get("border", "#00b4d8")
    label_color = theme_cfg.get("label", "bold #00b4d8")
    status_main = theme_cfg.get("status_main", "bold #00b4d8")
    status_text = theme_cfg.get("status_text", "#718096")
    status_sep = theme_cfg.get("status_sep", "#445566")
    text_input = theme_cfg.get("text_input", "#dde1e7")
    menu_current = theme_cfg.get("menu_current", "bg:#00b4d8 #ffffff bold")
    menu_current_meta = theme_cfg.get("menu_current_meta", "bg:#00b4d8 #ffffff")

    style = Style.from_dict({
        # 输入框边框
        "input-frame":     "bg:#0a0a0f",
        "input-line":      f"{border_color}",
        "input-prefix":    f"bg:#0a0a0f {label_color}",
        "status.memory-frame": f"bg:#0a0a0f {status_text}",
        "status.live-plan-frame": f"bg:#0a0a0f {status_text}",
        "status.thinking-frame": f"bg:#0a0a0f {status_text}",
        "status.thinking-content": f"bg:#0a0a0f {status_text}",

        "status.confirm-frame": f"bg:#0a0a0f {status_main} bold",
        "frame.border":    border_color,
        "frame.label":     label_color,
        # 输入文字
        "input-field":     f"bg:#0a0a0f {text_input}",
        # 状态栏：去掉所有底色，直接使用前景色
        "status.model":    status_main,
        "status.sep":      status_sep,
        "status.token":    status_text,
        "status.think":    "bold #e2e8f0",
        "status.task_active": "bold #39ff14",  # 矩阵亮绿
        "status.task_idle":   status_sep,
        "status.permission_confirm": "bold #50fa7b",
        "status.permission_plan":    "bold #88c0d0",
        "status.permission_full":    "bold #ffb86c",
        
        # 下拉补全菜单的颜色
        "completion-menu": "bg:#1a2a3a #e2e8f0",
        "completion-menu.completion": "bg:#1a2a3a #e2e8f0",
        "completion-menu.completion.current": menu_current,
        "completion-menu.meta.completion": "bg:#1a2a3a #718096",
        "completion-menu.meta.completion.current": menu_current_meta,
    })

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
        mouse_support=False,
        erase_when_done=True,
    )

    return app


# 按完整轮次淘汰最早的一段历史（首个非 system 消息起到下一条 user 消息前）。
# 实现已下沉到 core/agent_service.py 供 TUI 与 GUI 共用，这里保留同名别名。
_evict_oldest_turn = _agent_service.evict_oldest_turn


def _background_worker():
    """后台消费者线程：专门处理排队的对话请求"""
    global plan_approved_this_turn, active_cancel_event, active_turn_id
    while True:
        task_data = msg_queue.get()
        # worker 是 daemon 线程随主进程退出；队列从未投递 None 哨兵，无需判空分支
        task_session = None  # 预初始化，确保 except 块中可安全引用
        try:
            # 新一轮对话，重置计划模式授权状态
            plan_approved_this_turn = False

            # 立即给 UI 反馈：消息已收到，正在准备请求。
            # 后续构建 system prompt、读取历史、压缩记忆等步骤可能耗时数秒，
            # 提前显示状态可显著降低用户感知的“首次响应延迟”。
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
            early_cancel = threading.Event()
            active_cancel_event = early_cancel

            # 防御性检查：某些边界路径或旧进程可能投递不一致的任务结构，
            # 直接取 key 会导致后台 worker 崩溃并阻塞后续任务。
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
                continue

            # 内部指令处理：把 __ycode_internal__:summarize_background_tasks
            # 转换为友好的用户消息，触发主代理汇总后台任务结果
            # 这不是用户主动输入的，而是后台任务全部终态后自动触发的
            if is_internal and user_input == "__ycode_internal__:summarize_background_tasks":
                # 打印系统触发提示（不是 "✨ You >"，避免混淆）
                schedule_ui(lambda: print_formatted_text(
                    HTML("\n<ansicyan>📦 所有后台子代理任务已结束，正在自动汇总结果...</ansicyan>\n")
                ))
                # 转换为友好的用户消息，让主代理自然汇总
                user_input = "请汇总所有后台子代理任务的结果并告诉我。如果某些任务失败了，请用你自身的工具（如 search_news / fetch_url 等）接手完成，然后一并汇总。"
                # internal 指令不算排队任务，不打印 "⚡ [开始执行排队任务]"
                was_queued = False
                is_skill_call = False
            else:
                # 用户主动发起的新对话：重置该会话的自动汇总标志，
                # 这样下一批后台任务完成时可以再次触发自动汇总
                try:
                    _bg_summary_triggered.discard(current_session)
                except Exception:
                    pass

            # 注：普通 slash 命令在 Enter 键绑定里已由 UI 线程同步路由执行，
            # 能进入队列的 slash 输入只有“主动技能调用”（is_skill_call=True）。
            if was_queued:
                # 先按原始文本截断再转义，避免把 &lt; 之类的 HTML 实体从中间切断
                display_input = user_input[:50] + "..." if len(user_input) > 50 else user_input
                display_input = _esc(display_input)
                schedule_ui(lambda text=display_input: print_formatted_text(
                    HTML(f"⚡ <b><ansigreen>[开始执行排队任务]</ansigreen></b> <ansigray>{text}</ansigray>")
                ))

            # 捕捉配置
            from core.model_manager import get_active_model
            active_info = get_active_model()
            if not active_info or not active_info.get("litellm_model"):
                schedule_ui(lambda: print_formatted_text(
                    HTML("<ansired>! 您当前未配置或激活任何大模型！请输入 /models 添加并激活模型后再进行对话。</ansired>")
                ))
                continue

            task_session = current_session
            sync_plan_session(task_session)
            task_model = active_info["litellm_model"]
            task_key = active_info.get("api_key", "")
            task_url = active_info.get("base_url", "")
            task_ctx_win = active_info.get("context_window", 128000)

            from core.input_parser import parse_user_input
            if is_skill_call:
                # 提取短 ID 与实际的提问及挂载文件
                parts = user_input.split(maxsplit=1)
                skill_name = parts[0][1:]
                actual_query = parts[1] if len(parts) > 1 else "请启动并运行该技能。"

                schedule_ui(lambda name=_esc(skill_name): print_formatted_text(
                    HTML(f"\n⚡ <b><ansigreen>[主动调用技能: {name}]</ansigreen></b>")
                ))

                parsed_content, attached_msgs = parse_user_input(actual_query)
                # 在发送前强行贴上显式激活指示，指导大模型重点发挥对应技能作用
                instruction = f"【指示：用户已明确指定并主动调用技能【{skill_name}】来处理以下提问。请务必优先执行该技能指令并做出响应。】\n提问："
                if isinstance(parsed_content, list):
                    for item in parsed_content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            item["text"] = instruction + item.get("text", "")
                            break
                else:
                    parsed_content = instruction + parsed_content
            else:
                parsed_content, attached_msgs = parse_user_input(user_input)
                # 在终端打印挂载的文件提示
                for msg in attached_msgs:
                    schedule_ui(lambda message=_esc(msg): print_formatted_text(
                        HTML(f"  <ansigreen>{message}</ansigreen>")
                    ))

            # 将用户最新输入存入数据库
            manager.add_message(task_session, "user", parsed_content)

            # 组装本轮发给大模型的完整上下文：记忆压缩、系统提示词、运行上下文、
            # 后台子代理结果注入等步骤已下沉到 core/agent_service.py，与 GUI 共用同一套逻辑。
            # 界面副作用（压缩进度提示）通过 notify 回调切回 UI 线程打印。
            def _turn_notify(kind, message):
                schedule_ui(lambda text=message: print_formatted_text(HTML(text)))

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
            )


            # 后台线程只切换 UI 状态，不直接触碰 prompt_toolkit 布局。
            # turn 状态的建立（active_* 赋值）与清理在同一个 try/finally 作用域内，
            # 中途任何异常都不会留下永不清空的 cancel_event / executing 状态。
            turn_id = f"{task_session}:{uuid.uuid4().hex[:8]}"
            turn_controller = TurnController(history)
            turn_controller.begin_turn()
            history = turn_controller.messages
            cancel_event = turn_controller.cancel_event
            if early_cancel.is_set():
                # 用户在准备阶段按过 Ctrl+C：继承取消状态，本轮启动后第一时间终止
                cancel_event.set()
            active_cancel_event = cancel_event
            active_turn_id = turn_id
            # 主代理线程标记当前会话，使子代理可以共享工具级缓存
            from core import tool_cache as _tool_cache
            _tool_cache.set_current_session(task_session)
            # 将 undo 状态绑定到当前会话，防止切会话后 /undo 误操作其他会话的文件
            from core.memory import set_undo_session
            set_undo_session(task_session)

            # 会话级检查点：本轮所有 write_file/edit_file 的旧内容会被自动记录
            # 用户可通过 /checkpoint restore <turn_id> 整体回滚（含文件 + 对话上下文）
            from core.memory import begin_turn_checkpoint, end_turn_checkpoint
            begin_turn_checkpoint(
                session_name=task_session,
                turn_id=turn_id,
                last_msg_id=len(history),
                description=str(user_input)[:80],
            )

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
            try:
                schedule_ui(mark_turn_started)

                # 思考期间界面重绘已交由全局 UI 刷新线程托管，此处无需重复启动本地子线程
                try:
                    # 模型和工具层只产生事件；终端输出统一交给主 TUI 线程。
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
                    )
                    # 回复头部由 _render_ui_event 在主线程统一输出；
                    # 消息已由 generator 在生成迭代中实时持久化入库，此处不再重复写入
                    _forward_response_events(response_gen)
                    turn_controller.complete()
                    # 提交本轮检查点到全局缓存（即使本轮没有文件修改也要记录，
                    # 用户可借此回滚到"本轮对话开始前"的状态）
                    end_turn_checkpoint()
                    schedule_ui(_reset_turn_state)
                except KeyboardInterrupt:
                    # The queued turn_cancelled event renders the cancellation line.
                    turn_controller.cancel()
                    end_turn_checkpoint()  # 取消也提交检查点（已记录的快照仍可用）
                    schedule_ui(_reset_turn_state)
                    turn_interrupted = True
                except Exception as e:
                    turn_controller.cancel()
                    end_turn_checkpoint()
                    # 必须记录到 runtime-warnings.log，否则异常被静默吞掉，
                    # 用户消息已入库但 assistant 回复缺失，无法排查（session 97 类问题）
                    import logging as _logging_mod
                    _logging_mod.getLogger("py.warnings").exception(
                        "LLM 调用失败 session=%s turn=%s: %s",
                        task_session, turn_id, e,
                    )
                    # 同时持久化一条失败标记消息到数据库，确保会话历史完整
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
            finally:
                # active_cancel_event / active_turn_id 由 worker 线程在本 turn 的
                # try/finally 作用域内建立，也必须由 worker 线程在 finally 直接清空。
                # 不能依赖 schedule_ui 投递清理：App 重启间隙（如 /models 选择器期间）
                # schedule_ui 会静默丢弃回调，导致 active_cancel_event 残留为旧 event，
                # 下一轮 Ctrl+C 无法中断新轮次、旋转动画永不消失。
                # 这两个全局的设计意图就是"worker 建立、worker 清理、UI 只读"。
                active_cancel_event = None
                active_turn_id = None
                # 同样由 worker 线程直接清理 UI 状态变量，避免 App 重启间隙 schedule_ui 丢弃
                # 导致旋转动画/executing 状态永久残留。
                global is_thinking, task_status
                is_thinking = False
                task_status = "idle"
                _clear_active_thinking()
                from core import tool_cache as _tool_cache
                from core.memory import clear_undo_session
                _tool_cache.set_current_session(None)
                clear_undo_session()
                # 清理卡在 in_progress 的计划项：子 agent 异常退出（超时/崩溃/未调用 terminal_callback）
                # 会导致计划项永久卡在 in_progress，turn 结束后仍显示在状态栏。
                # 但必须跳过"后台子代理仍在运行"的 in_progress 项——否则会误杀正常后台任务
                # （用户输入新对话触发 turn 结束清理时，后台任务还在跑）。
                try:
                    from core.plan import get_default_plan_manager, TERMINAL_STATUSES
                    from core.tools import subagent_manager
                    plan_mgr = get_default_plan_manager()
                    # 只清理"无对应运行中子代理"的 in_progress 项
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
                # 检查是否需要自动重命名会话（当且仅当是新建的默认 hex 会话）
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
        except Exception as e:
            # 兜底防线：parse/add_message/prune/压缩/提示词组装等任何未预期异常
            # 都不允许杀死 worker 线程，否则之后所有用户输入会被静默吞掉、UI 卡死
            logging.getLogger("py.warnings").exception("background worker turn failed: %s", e)
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
    """Headless 模式：执行一次性任务后退出，不进入交互式 TUI。

    用途：CI/脚本/管道集成
    参考：Grok Build/OpenCode 都支持的 headless 模式

    流程：
    1. 初始化配置（与 run_cli 相同）
    2. 解析 prompt（支持 "-" 读取 stdin）
    3. 后台 worker 线程执行任务
    4. 主线程通过事件回调接收回复并打印到 stdout
    5. 任务完成或取消后退出

    与交互模式区别：
    - 不启动 prompt_toolkit Application
    - 所有输出走 print_formatted_text 到 stdout
    - 权限请求直接按"拒绝"处理（headless 模式不交互确认）
    - 返回退出码：0=成功，1=任务失败，2=配置错误
    """
    global current_session, current_model, current_key, current_url, current_ctx_win

    # 1. 初始化运行时（与 run_cli 相同的副作用集中执行）
    bind_default_plan_session(current_session)
    # headless 模式：不在 TUI 中与用户交互，自动拒绝所有权限请求，避免 worker 永久挂起
    def _deny_permission_request(request):
        try:
            permission_broker.resolve(request.request_id, 1)  # 1 = No, 拒绝
        except Exception:
            pass

    permission_broker.set_prompt_handlers(
        _deny_permission_request,  # 自动拒绝
        lambda: None,              # 不需要 dismiss
    )

    set_terminal_title("Y-code (headless)")

    # 2. 处理 prompt 输入（支持 stdin）
    if prompt == "-":
        import sys as _sys
        prompt = _sys.stdin.read().strip()
        if not prompt:
            print("错误：从 stdin 读取的内容为空", file=_sys.stderr)
            return 2
    elif not prompt.strip():
        print("错误：prompt 不能为空", file=sys.stderr)
        return 2

    # 3. 指定会话（如果传了 --session）
    if session_name:
        current_session = session_name
        sync_plan_session(current_session)

    # 4. 启动后台 worker 线程
    worker_thread = threading.Thread(target=_background_worker, daemon=True)
    worker_thread.start()

    # 5. 投递任务并等待完成
    # 用一个简单的事件机制等 worker 处理完成
    done_event = threading.Event()
    task_data = {
        "text": prompt,
        "was_queued": False,
        "is_skill_call": False,
    }
    msg_queue.put(task_data)

    # 监听事件：worker 处理完一轮会 idle 等 msg_queue.get()，
    # 这里用 polling 检测 msg_queue 是否被消费 + task_done
    # 简化方案：等待 worker 把这一轮处理完（msg_queue.task_done() 会调用）
    # task_done() 在 finally 块里调用，这里等待它
    try:
        # Queue.join() 没有 timeout 参数；改用轮询，设 30 分钟硬超时，
        # 防止 worker 意外崩溃漏调 task_done() 导致 headless 进程永久挂起。
        deadline = time.monotonic() + 1800  # 30 分钟
        while msg_queue.unfinished_tasks > 0:
            if time.monotonic() > deadline:
                print("\n[headless 任务超时（30 分钟），强制退出]", file=sys.stderr)
                return 1
            time.sleep(0.1)
        # 给事件队列一点时间排空
        time.sleep(0.5)

        # headless 下不经过 TUI 渲染，需要从数据库取出最终回复并打印到 stdout
        try:
            history = manager.get_history(current_session)
            assistant_messages = [
                msg for msg in history
                if isinstance(msg, dict) and msg.get("role") == "assistant"
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

    # 6. 返回成功退出码，由入口统一 sys.exit
    return 0


def run_cli():
    """主入口 — 封装在 while 循环里，支持弹出全屏选择器后自动重启"""
    global app_instance, pending_action, current_session
    global current_model, current_key, current_url, current_ctx_win, current_model_name

    # TEMP STARTUP TRACE (revert after diagnosis)
    _trace_mark = _startup_trace
    _trace_mark("run_cli enter")

    # 运行时初始化副作用集中在入口执行（而非模块 import 时），保证 main.py 可被安全 import
    _app_ctx.set_ui_flavor("tui")
    bind_default_plan_session(current_session)
    permission_broker.set_prompt_handlers(
        _activate_permission_request,
        _dismiss_permission_prompt,
    )
    # MCP 状态打印器的注册移入 _preload_mcp 后台线程（见下文），避免在首绘前
    # 同步 import core.mcp_client（连带 mcp/pydantic/httpx，约 1.1~1.5s）。

    set_terminal_title("Y-code")

    # 首次启动/完全无配置模型时，自动引导开启全屏模型配置选择器
    active_info = get_active_model()
    if not active_info:
        print("\n🔑 欢迎使用 Y-CODE！检测到您尚未配置大模型。")
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
        # 已有激活模型但密钥为空时，提示输入 Key
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
                current_key = key_input
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

    # 启动后台处理线程
    _trace_mark("before worker_thread")
    worker_thread = threading.Thread(target=_background_worker, daemon=True)
    worker_thread.start()

    # ── 启动预热（同步阶段，TUI 出现前完成）──────────────────────────────
    # 设计取向：启动慢一点没关系，但进入界面后必须立即可用、输入不卡。
    # 若改为后台预热，litellm/mcp/tiktoken 的 CPU 密集 import 会与 UI 渲染线程
    # 抢 GIL，实测会把首帧绘制卡 8 秒、键盘输入无回显。因此三个预热任务在
    # 此处并发执行、主线程显示进度动画并等全部完成后再进入 TUI。
    _trace_mark("preload phase begin")

    def _preload_litellm():
        """预热 LLM 引擎：litellm import、tiktoken 编码加载（token_counter
        首次调用）、模型视觉能力探测（首个 generator 会调用）。"""
        global _llm_engine_ready
        try:
            from core.llm import _get_litellm, _litellm_token_counter, _supports_vision
            _get_litellm()
            try:
                _litellm_token_counter(
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
            logging.getLogger("py.warnings").warning("预加载 litellm 失败: %s", e)
        finally:
            _llm_engine_ready = True
            _trace_mark("preload litellm done")

    def _mcp_status_to_ui(message):
        # 预热期 TUI 尚未启动：直接打印；TUI 运行后：经 schedule_ui 回 UI 线程
        if app_instance is None:
            print_formatted_text(message)
        else:
            schedule_ui(lambda text=message: print_formatted_text(text))

    def _preload_mcp():
        """后台启动 MCP 连接，让首次 LLM 请求不必同步等待 MCP server 就绪。"""
        # 廉价预检：没有任何“启用中”的 MCP 服务时直接跳过——避免 import mcp 全家桶
        # （~1.1s），也不再打印“跳过已禁用的服务”噪音。
        # 若用户之后通过 /mcp 启用服务，连接路径会按需惰性 import，不受影响。
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
                _trace_mark("preload mcp skipped (no enabled servers)")
                return
        except Exception:
            pass  # 预检失败不阻碍正常预热流程
        try:
            from core.mcp_client import mcp_manager, set_status_logger as _set_mcp_status_logger
            # MCP 连接状态改由 TUI 主线程打印（core/mcp_client 自身不再 import main）
            _set_mcp_status_logger(_mcp_status_to_ui)
            mcp_manager._ensure_connected()
            _trace_mark("preload mcp done")
        except Exception as e:
            logging.getLogger("py.warnings").warning("MCP 后台连接失败: %s", e)

    def _warm_token_usage():
        """预热当前 session 的 Token 统计（会连带 tiktoken 首次加载），
        同步完成后状态栏 token 数在首绘时就是准确值。"""
        try:
            recalc_token_usage()
            _trace_mark("warm token usage done")
        except Exception as e:
            logging.getLogger("py.warnings").warning("启动预热 recalc_token_usage 失败: %s", e)

    _preload_threads = [
        threading.Thread(target=_preload_litellm, daemon=True),
        threading.Thread(target=_preload_mcp, daemon=True),
        threading.Thread(target=_warm_token_usage, daemon=True),
    ]
    for _t in _preload_threads:
        _t.start()

    # 进度动画：纯文本终端阶段（TUI 未启动），非 tty 时安静等待
    _preload_t0 = time.monotonic()
    _spin_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    _tty = sys.stdout is not None and sys.stdout.isatty()
    try:
        while any(_t.is_alive() for _t in _preload_threads):
            if _tty:
                _frame = _spin_frames[int((time.monotonic() - _preload_t0) * 10) % len(_spin_frames)]
                print(f"\r  {_frame} 星瑶正在预热模型引擎 · {time.monotonic() - _preload_t0:.1f}s   ", end="", flush=True)
            for _t in _preload_threads:
                _t.join(timeout=0.1)
    except KeyboardInterrupt:
        # Ctrl+C：跳过等待直接进入界面（后台 daemon 线程仍会继续预热）
        if _tty:
            print("\r  ⚠️ 已跳过预热等待，首轮对话可能稍慢。           ")
    _trace_mark("preload phase done")

    # 预热全部完成后才铺主界面：print_logo 会清屏，启动提示与预热动画随之消失，
    # 用户直接看到完整可输入的主页面（logo + 输入框 + 准确的状态栏）
    _trace_mark("before print_logo")
    print_logo()
    _trace_mark("after print_logo")

    # 启动全局常驻 UI 刷新线程（用以维持思考计时器和星瑶运转状态微动画）
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

    # 主循环：支持 /models 弹出选择器并重启主 App
    while True:
        pending_action = None
        _trace_mark("before _make_input_app")
        app_instance = _make_input_app()
        _trace_mark("after _make_input_app")

        # 注册后台子代理终态实时通知回调
        # 后台任务完成/失败/超时/取消时，立即在终端打印一条通知，不等下一轮对话。
        # 回调在 worker 线程中触发，必须通过 schedule_ui 切回 UI 线程打印（遵守 AGENTS.md 线程安全规则）。
        from core.tools import subagent_manager as _sm_for_notification
        _sm_for_notification.set_on_terminal(_on_background_subagent_terminal)

        with patch_stdout():
            try:
                # pre_run：App 重启间隙（如 /models 触发的退出与重建之间）
                # schedule_ui 静默丢弃的积压事件，在新 App 启动时立即补渲染
                _trace_mark("before app.run")
                # TEMP STARTUP TRACE：记录首帧绘制的开始/结束时间点
                _orig_renderer_render = app_instance.renderer.render
                def _first_render_gate(*_a, **_kw):
                    _trace_mark("first renderer.render begin (first paint)")
                    app_instance.renderer.render = _orig_renderer_render
                    try:
                        return _orig_renderer_render(*_a, **_kw)
                    finally:
                        _trace_mark("first renderer.render end")
                app_instance.renderer.render = _first_render_gate
                app_instance.run(pre_run=drain_ui_events)
            except EOFError:
                # Ctrl+D：退出系统
                pass
            except KeyboardInterrupt:
                # Ctrl+C：业务执行中→终止任务并回到输入循环；闲置→也回到输入循环
                # （不退出系统；退出请用 Ctrl+D）
                if is_thinking or active_cancel_event is not None:
                    _cancel_active_turn()
                    # 清空排队消息，避免取消后旧任务继续执行
                    try:
                        while True:
                            msg_queue.get_nowait()
                    except queue.Empty:
                        pass
                    print_formatted_text(HTML(
                        "\n<ansired>🛑 收到中断信号，已终止星瑶的执行。</ansired>\n"
                    ))
                else:
                    print_formatted_text(HTML(
                        "\n<ansigray>（Ctrl+C 清空输入；退出请按 Ctrl+D）</ansigray>\n"
                    ))
                _reset_turn_state()
                pending_action = "resume_input"  # 标记：重启输入循环，不退出

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
            # 选择器关闭后（即使删光了模型未触发 on_selected），均重新同步当前激活模型
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
            # 重铺当前会话历史：全屏选择器退出后屏幕被清空，不回放历史
            # 用户会误以为“上下文丢了/被开了新会话”，实际上会话从未变化
            show_session_history(current_session)
            pending_action = None
            continue
            
        elif pending_action == "open_skills":
            from core.skill_selector import open_skill_selector
            open_skill_selector()
            pending_action = None
            continue

        elif pending_action == "open_mcp":
            # /mcp 不带参数时打开的全屏 TUI 配置器
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

            # 记录打开选择器前当前会话是否已入库：全新会话在首轮对话前还没有数据库记录，
            # 没有它，下面的“被删除”防御会把新会话误判为已删除而错误切回最近会话
            session_existed_before = current_session in {s["name"] for s in manager.get_all_sessions()}
            open_session_selector(
                on_switch=_on_switch,
                on_rename=_on_rename,
                manager=manager,
                current_session=current_session,
            )

            # 选择器全屏 App 已退出，判断是否在里面按了 [R/F2] 触发重命名
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
            # Ctrl+C 中断后回到输入循环：不退出系统，重建输入 App
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
    # Headless 模式：ycode -p "task" 一次性执行任务后退出，不进入交互式 TUI
    # 用途：CI/脚本/管道集成（参考 Grok Build/OpenCode 的 headless 模式）
    # 用法：
    #   ycode -p "解释这个代码库"
    #   ycode --prompt "实现一个加法函数" --session work
    #   echo "问题" | ycode --prompt -
    import argparse
    parser = argparse.ArgumentParser(
        prog="ycode",
        description="Y-code 终端 AI 编程助手",
        add_help=False,  # 交互模式下 argparse 的 -h/--help 会干扰；只在显式 -p 时启用
    )
    parser.add_argument("-p", "--prompt", dest="prompt",
                        help="Headless 模式：执行一次性任务后退出（- 读取 stdin）")
    parser.add_argument("--session", dest="session", default=None,
                        help="指定会话名（headless 模式下有效）")
    parser.add_argument("-h", "--help", action="help",
                        help="显示帮助信息")

    args, _ = parser.parse_known_args()

    if args.prompt is not None:
        # Headless 模式：统一在此退出，方便测试/包装器捕获状态码
        sys.exit(run_headless(prompt=args.prompt, session_name=args.session))
    else:
        # 交互式模式（默认）
        run_cli()
