"""
gui_bridge.py
=============
PyWebView JS-API 桥接层：为 Vue 前端提供后端能力，并把一轮对话中的
流式 Token、思考过程、工具卡片、文件 Diff、权限确认等实时推送到界面。

设计约束：
- 绝不 import main 或任何依赖 prompt_toolkit 的模块，对话逻辑一律复用
  core/agent_service.py（与 CLI 完全同源，保证回答质量一致）。
- 所有对外方法都返回可 JSON 序列化的 dict，异常一律吞掉后返回
  {"ok": False, "msg": ...}，绝不让异常穿过桥接层（否则前端 Promise 直接 reject）。
"""

import json
import logging
import os
import queue
import pathlib
import re
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from config import GUI_DB_FILE, MY_PERMISSION_MODE
import core.app_context as app_context
from core.memory import MemoryManager
from core.model_manager import (
    add_preset_provider as mm_add_preset_provider,
    add_model_to_provider,
    add_provider as mm_add_provider,
    delete_provider as mm_delete_provider,
    delete_model as mm_delete_model,
    get_active_model,
    get_model_reasoning_effort,
    get_secondary_roles,
    load_config,
    set_active_model,
    set_model_reasoning_effort,
    set_secondary_model,
    set_secondary_roles,
    update_model as mm_update_model,
    update_provider as mm_update_provider,
)
from core.model_catalog import API_PROTOCOLS, get_preset_providers
from core.permission_broker import permission_broker
from core.skills import get_active_skill_ids, save_active_skill_ids, scan_all_skills
from core.task_manager import kill_background_task, list_active_tasks
from core.version import __version__ as YCODE_VERSION

logger = logging.getLogger(__name__)

# CLI 的 prompt_toolkit 着色标记（<ansiyellow> 等）：GUI 不渲染，桥接层统一剥离，
# 否则用户会在提示里看到 <ansiyellow> 原文。CLI 路径不经此处，不受影响。
_ANSI_MARKUP_RE = re.compile(r"</?ansi\w*>")


def _strip_ansi_markup(text) -> str:
    # 剥离 CLI 着色标签后，再去掉首尾空白/换行：CLI 习惯用 \n 做段落分隔，
    # 但 GUI notice 卡片用 white-space: pre-wrap 会把这个换行渲染成空行
    return _ANSI_MARKUP_RE.sub("", str(text)).strip()


def _session_from_turn_id(turn_id: str) -> str:
    """从 session:turn 复合 ID 提取会话名，不依赖可变的当前会话。"""
    value = str(turn_id or "")
    return value.rsplit(":", 1)[0] if ":" in value else ""

# MCP 单例：import 失败（如未安装 mcp 依赖）不能拖垮整个 GUI
try:
    from core.mcp_client import mcp_manager as _mcp_manager_singleton
except Exception:  # pragma: no cover - 依赖缺失时的降级路径
    _mcp_manager_singleton = None

# 流式 token 批量推送间隔（秒）：过密的 evaluate_js 调用会拖垮 WebView
TOKEN_FLUSH_INTERVAL = 0.06

# JS 事件派发队列容量：洪峰（工具大结果 + 计划更新 + 进度快照）下做缓冲；
# 渲染进程挂起时撑满即丢弃新事件，生产者线程永不阻塞
JS_QUEUE_MAXSIZE = 2000

# ask_user 提问超时（秒）：超时按取消处理。权限确认与 CLI 一致无限等待，
# 不再因用户暂时离开而自动拒绝（拒绝会让模型拿到"用户拒绝"继续跑偏）
PERMISSION_TIMEOUT_SECONDS = 300

# 文件预览上限：256KB
FILE_PREVIEW_MAX_BYTES = 256 * 1024

# 文件树扫描限制
FILE_TREE_MAX_DEPTH = 4
# 每层目录独立配额：保证顶层条目完整显示（旧实现整棵树共享全局计数，
# 深度优先扫描会被字母序靠前目录的子树吃满配额，导致靠后的顶层条目被丢弃）
FILE_TREE_MAX_ENTRIES_PER_DIR = 100
FILE_TREE_SKIP_DIRS = {
    ".git", "venv", ".venv", "node_modules", "__pycache__", "dist", "build",
}

# 测试模型连通性的超时（秒）
TEST_MODEL_TIMEOUT = 15

# 后台子代理全部终态后自动发起的汇总回合（与 CLI 的内部汇总指令同文案）
_BG_SUMMARY_PROMPT = (
    "请汇总所有后台子代理任务的结果并告诉我。如果某些任务失败了，"
    "可以用你自身的工具（如 search_news / fetch_url 等）接手完成；"
    "但已被用户取消的任务不要接手或重试，如实说明即可。"
)
# 内部汇总回合在前端展示的系统提示文案（代替把上面的指令原文渲染成用户气泡）
_BG_SUMMARY_DISPLAY_TEXT = "📦 所有后台子代理任务已结束，正在自动汇总结果…"
# 后台命令任务完成后的自动总结回合提示文案
_BG_COMMAND_SUMMARY_DISPLAY_TEXT = "📦 后台任务已完成，正在总结结果…"


def _resolve_real_path(path: str) -> str:
    try:
        p = pathlib.Path(path)
        if p.exists():
            return str(p.resolve())
        return os.path.abspath(path)
    except Exception:
        return os.path.abspath(path)


def _app_install_dir() -> str:
    """应用自身所在目录（冻结版为 exe 所在目录，开发模式为 gui_bridge.py 所在目录）。

    桌面快捷方式的起始位置是安装目录，GUI 进程 cwd 就是它；它不该被当作
    “项目文件夹”自动注册成分组，否则用户会看到一个永远删不掉的幻影分组。
    """
    if getattr(sys, "frozen", False):
        return _resolve_real_path(os.path.dirname(sys.executable))
    return _resolve_real_path(os.path.dirname(os.path.abspath(__file__)))


def _sanitize_for_ui(text: str, api_key: Optional[str] = None) -> str:
    """将可能包含 API key 的错误文本脱敏后再输出到 UI。

    长度封顶（2026-08-14）：provider/网关异常文本可能携带完整请求体（整个
    messages 数组），不截断直接推给前端会把聊天区变成一屏原始 JSON 文本墙
    并压垮渲染进程。截断保留头尾。
    """
    if not isinstance(text, str):
        text = str(text)
    # 先粗截断再做正则：URL userinfo 正则对超长无空白文本是 O(n²) 灾难性回溯，
    # 必须先限制参与匹配的长度（最终 UI 上限 2000，头尾各一段覆盖足够）
    if len(text) > 10000:
        text = text[:6000] + "\n…[错误文本过长，中段已截断]…\n" + text[-3000:]
    if api_key and len(api_key) >= 4 and api_key in text:
        text = text.replace(api_key, "***")
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer ***", text)
    text = re.sub(r"(?i)(\b(?:api[_-]?key|token|secret|password|passwd|auth)\s*[=:]\s*)[^\s,;]+", r"\1***", text)
    # 各段加上界，避免病态输入下的回溯爆炸
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]{0,30}://[^\s/:@]{1,200}:)[^\s/@]{1,300}(@)", r"\1***\2", text)
    text = re.sub(r"\b(?:sk|pk|ghp|glpat)-[A-Za-z0-9_.-]{8,}\b", "***", text)
    if len(text) > _MAX_UI_ERROR_TEXT_LEN:
        text = text[:1200] + "\n…[错误文本过长，中段已截断]…\n" + text[-500:]
    return text


# 错误文本入 UI 的长度上限（与 core/llm._sanitize_error_text 双保险）
_MAX_UI_ERROR_TEXT_LEN = 2000


def _path_is_within(path: str, root: str) -> bool:
    try:
        candidate = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        base = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        return os.path.commonpath([candidate, base]) == base
    except (OSError, ValueError, TypeError):
        return False


def _to_plain_dict(obj) -> dict:
    if not obj:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "items"):
        return {k: v for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {}


class _GuiContext:
    """GUI 运行时上下文适配器，代理到 ApiBridge 实例的状态。"""

    def __init__(self, bridge: "ApiBridge"):
        self._bridge = bridge
        self.ui_flavor = "gui"

    @property
    def app_instance(self):
        return None  # GUI 没有 CLI prompt_toolkit App

    @property
    def permission_mode(self) -> str:
        return self._bridge._current_permission_mode

    @permission_mode.setter
    def permission_mode(self, val: str):
        self._bridge._current_permission_mode = app_context.normalize_permission_mode(val)

    @property
    def task_status(self) -> str:
        return self._bridge._task_status

    @task_status.setter
    def task_status(self, val: str):
        self._bridge._task_status = val

    @property
    def current_session(self) -> str:
        return self._bridge._current_session

    @current_session.setter
    def current_session(self, val: str):
        self._bridge._current_session = str(val or "")

    @property
    def manager(self):
        return self._bridge._memory

    # 注意：不再提供 permission_callback。
    # GUI 与 CLI 一样统一走 core.permission_broker（见 _activate_permission_request）。

    def invalidate_ui(self):
        pass  # GUI 通过事件驱动，不需要主动重绘


class ApiBridge:
    def __init__(self, mcp_manager=None):
        # 内部状态变量使用下划线开头，防止 PyWebView 在 Windows .NET 检查中产生属性递归问题
        self._window = None
        self._dropped_paths: list = []
        # GUI 专用会话库：与 CLI 的 ycode.db 分离，两边对话互不同步
        self._memory = MemoryManager(db_path=GUI_DB_FILE)
        # 修复：形参曾经遮蔽模块级单例，导致 self._mcp 永远是 None
        self._mcp = mcp_manager if mcp_manager is not None else _mcp_manager_singleton

        # 启动时恢复最近一次对话（按最后活跃时间）；没有历史会话才新建空对话
        latest = self._find_latest_session()
        self._current_session = latest or f"session_{uuid.uuid4().hex[:6]}"
        if not latest:
            try:
                # GUI 启动会话统一挂在 GLOBAL（自由对话）分组下
                self._memory.ensure_session(self._current_session, workdir="GLOBAL", source="GUI")
            except Exception:
                logger.warning("初始化 GUI 启动会话失败", exc_info=True)

        self._current_permission_mode = app_context.normalize_permission_mode(MY_PERMISSION_MODE)
        self._task_status = "idle"
        # 多会话并行：按 session 隔离回合线程和取消事件
        self._active_turns: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        # 每会话当前回合的 turn_id（session:xxx 复合 ID）：权限请求的 owner
        # 是 turn_id 而非会话名，stop_current_turn 要按它精确取消权限等待
        self._active_turn_ids: dict[str, str] = {}
        # 回合启动互斥：send_user_message 与后台自动汇总都可能启动 turn 线程，
        # "检查 is_alive + 启动"必须原子，否则两边同时通过检查会并发跑两个回合
        self._turn_start_lock = threading.Lock()
        # 已弃用：多会话并行改为线程本地 workdir 上下文（core.workspace_context），
        # 不再需要进程级串行锁。保留变量定义避免历史引用报错。
        self._turn_workdir_lock = threading.Lock()
        # 后台子代理自动汇总（与 CLI 的 _bg_summary_triggered 同源）：
        # 每批后台任务只触发一次汇总，用户主动发新消息时重置
        self._bg_summary_triggered: set = set()
        self._bg_summary_lock = threading.Lock()
        # 终态回调到来时若有回合正在运行，挂起到回合结束后补触发；
        # 按会话分桶（set），多会话并行时 pending 不会互相覆盖
        self._bg_summary_pending_sessions: set = set()
        # 用户主动点过“终止”的会话：抑制后台任务终态的自动汇总回合，
        # 防止被取消的任务被“接手完成”机制复活；用户发新消息时解除
        self._user_cancelled_sessions: set = set()

        # 按会话隔离的流式 delta 缓冲，防止多会话并行时 token 混合
        self._delta_lock = threading.Lock()
        self._delta_buffers: dict[str, List[str]] = {}
        self._delta_kinds: dict[str, str] = {}
        self._delta_last_flush = 0.0
        self._delta_flusher: Optional[threading.Thread] = None
        # 惰性创建 flusher 的双检锁：并发回合同时首次触发时不得创建两个
        self._delta_flusher_lock = threading.Lock()

        # 权限请求：request_id -> 超时定时器
        self._perm_timers: Dict[str, threading.Timer] = {}
        self._perm_lock = threading.Lock()

        # 内嵌终端：右侧面板用户手动执行命令的子进程（同一时刻最多一个）
        self._term_proc = None
        self._term_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutting_down = False

        # JS 事件派发队列：evaluate_js 是同步调用（无超时），渲染进程挂起时
        # 永不返回。若事件源线程（回合 worker / delta 冲刷 / 终端泵）直接调用，
        # 一旦卡住会连带 _delta_lock 级联堵塞全管线（零 CPU、零网络的假死）。
        # 改为单消费者 FIFO 队列：生产者只 put_nowait，队列满丢弃新事件并计数，
        # 任何情况下事件源线程都不会被 WebView 阻塞。
        self._js_queue: "queue.Queue" = queue.Queue(maxsize=JS_QUEUE_MAXSIZE)
        self._js_dispatcher: Optional[threading.Thread] = None
        self._js_dispatcher_lock = threading.Lock()
        self._js_dropped = 0
        # 标题栏主题延迟重设线程去重引用（每次窗口事件不再各开一个）
        self._reapply_thread: Optional[threading.Thread] = None

        # 注册 GUI 运行时上下文 + 权限应答通道
        app_context.register(_GuiContext(self))
        app_context.set_ui_flavor("gui")
        app_context.set_manager(self._memory)
        app_context.set_current_session(self._current_session)
        self._sync_active_model_context()
        permission_broker.set_prompt_handlers(
            self._activate_permission_request,
            self._dismiss_permission_prompt,
        )
        # MCP 状态提示改走日志（GUI 不需要终端输出）
        try:
            from core.mcp_client import set_status_logger
            set_status_logger(lambda message: logger.info("%s", message))
        except Exception:
            pass

        # 注册后台子代理终态回调：任务结束立即通知前端；本会话全部后台任务
        # 终态后自动发起一轮汇总 turn（对应 CLI 的 _on_background_subagent_terminal）
        try:
            from core.tools import subagent_manager
            subagent_manager.set_on_terminal(self._on_background_subagent_terminal)
        except Exception:
            logger.warning("注册后台子代理终态回调失败", exc_info=True)

        # 注册后台命令任务完成回调：execute_command 的后台任务（如打包）退出后
        # 立即向前端推送完成通知，避免用户只能手动查询任务状态
        try:
            from core.task_manager import set_task_finished_callback
            set_task_finished_callback(self._on_background_task_finished)
        except Exception:
            logger.warning("注册后台命令任务完成回调失败", exc_info=True)

    # ------------------------------------------------------------------
    # 基础设施：窗口、事件推送
    # ------------------------------------------------------------------
    def set_window(self, window):
        """设置 PyWebView window 引用"""
        self._window = window
        self._hwnd = 0
        # 标题栏主题初始值必须读持久化配置（gui_theme_light）：首次 DWM 着色
        # 发生在句柄找到时，早于前端校正；硬编码深色会让浅色主题用户看到
        # 标题栏先黑一闪再变白。
        try:
            self._titlebar_dark = not bool(load_config().get("gui_theme_light", False))
        except Exception:
            self._titlebar_dark = True  # 读不到配置时保持深色默认
        # 后台线程轮询获取原生窗口句柄，拿到后应用深色标题栏
        threading.Thread(target=self._find_hwnd_worker, daemon=True, name="gui-hwnd-find").start()
        # WebView2 就绪、最小化还原等时机 DWM 可能重建非客户区，把标题栏重置回白色。
        # 冻结版冷启动慢（杀毒扫描），仅靠句柄找到后 7 秒内的延迟重设覆盖不到，
        # 订阅窗口事件兜底：页面加载完 / 窗口显示 / 还原时都重设一次。
        try:
            window.events.loaded += self._on_window_frame_event
            window.events.shown += self._on_window_frame_event
            window.events.restored += self._on_window_frame_event
        except Exception:
            logger.debug("订阅窗口事件失败", exc_info=True)

    def has_active_turns(self) -> bool:
        """是否有任一会话的回合线程仍在运行（关窗退出确认的判断依据）。

        _active_turns 在回合 finally 中 pop，条目存在即代表回合已登记；
        再用 is_alive 过滤刚登记尚未启动/已结束未清理的极窄竞态窗口。
        """
        return any(t.is_alive() for t in self._active_turns.values())

    def shutdown(self) -> None:
        """Idempotently stop GUI-owned work before the native window is destroyed."""
        with self._shutdown_lock:
            if self._shutting_down:
                return
            self._shutting_down = True

        for event in list(self._cancel_events.values()):
            event.set()
        with self._bg_summary_lock:
            self._bg_summary_pending_sessions.clear()
        with self._perm_lock:
            timers = list(self._perm_timers.values())
            self._perm_timers.clear()
        for timer in timers:
            timer.cancel()
        permission_broker.cancel_all()

        try:
            self.kill_terminal_command()
        except Exception:
            logger.exception("退出时终止内嵌终端失败")

        try:
            from core.tools import subagent_manager
            subagent_manager.shutdown(timeout=5.0)
        except Exception:
            logger.exception("退出时取消子代理失败")

        # Cooperative cancellation must get a bounded drain window before DB/MCP
        # teardown. Never wait on the current thread or block exit indefinitely.
        deadline = time.monotonic() + 5.0
        current = threading.current_thread()
        while time.monotonic() < deadline:
            active = [
                thread for thread in list(self._active_turns.values())
                if thread is not current and thread.is_alive()
            ]
            if not active:
                break
            for thread in active:
                thread.join(timeout=min(0.1, max(0.0, deadline - time.monotonic())))

        try:
            # 多会话并行：调度器按会话分桶，退出时必须全部收尾
            from core.task_scheduler import shutdown_all_schedulers

            shutdown_all_schedulers()
        except Exception:
            logger.exception("退出时停止计划调度器失败")

        if self._mcp is not None:
            try:
                self._mcp.close()
            except Exception:
                logger.exception("退出时关闭 MCP 失败")
        # 通知 JS 派发线程退出（哨兵可能被丢弃/线程已卡死，均为守护线程，尽力而为）
        try:
            self._js_queue.put_nowait(None)
        except Exception:
            pass

        try:
            self._memory.close()
        except Exception:
            logger.exception("退出时关闭会话数据库失败")

    def _on_window_frame_event(self):
        """窗口框架事件回调：DWM 重建非客户区后重设标题栏主题（线程内延迟执行）。"""
        self._start_reapply_thread()

    def _start_reapply_thread(self, name: str = "gui-titlebar-event-reapply"):
        """启动标题栏主题延迟重设线程；上一个还在跑就复用。
        重设序列累计睡 55.5 秒，若每次窗口事件都新开线程，密集事件下线程
        会越积越多。"""
        t = getattr(self, "_reapply_thread", None)
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=self._reapply_worker, daemon=True, name=name)
        self._reapply_thread = t
        t.start()

    # ------------------------------------------------------------------
    # 窗口标题栏主题（Windows DWM 沉浸式深色标题栏，保留原生最小化/关闭按钮）
    # ------------------------------------------------------------------
    def _find_hwnd_worker(self):
        """按窗口标题轮询查找本进程顶层窗口的 HWND（原生窗口在 webview.start 后才创建）。
        冻结版启动慢（WebView2 初始化/首次解压），轮询预算放宽到 60 秒。"""
        if sys.platform != "win32":
            return
        try:
            title = getattr(self._window, "title", "") or ""
            for _ in range(300):  # 最多等 60 秒
                hwnd = self._find_own_hwnd(title)
                if hwnd:
                    self._hwnd = hwnd
                    # 先设图标再设标题栏主题：WM_SETICON 会触发 DWM 重建非客户区，
                    # 把已应用的标题栏颜色重置回系统默认（白），顺序不能反。
                    self._apply_window_icon()
                    time.sleep(0.05)
                    self._apply_titlebar_theme()
                    # WebView2 子窗口就绪、DPI 变化等时机也可能重建边框，延迟补两次
                    self._start_reapply_thread(name="gui-titlebar-reapply")
                    return
                time.sleep(0.2)
        except Exception:
            logger.debug("查找窗口句柄失败", exc_info=True)

    @staticmethod
    def _find_own_hwnd(title: str) -> int:
        """EnumWindows + GetWindowThreadProcessId 按当前进程 PID 过滤顶层窗口，
        再按标题精确匹配。旧实现 FindWindowW 只按标题找窗，双开实例时可能
        命中另一个实例的窗口，把图标/标题栏主题设错对象。"""
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        pid = os.getpid()
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _callback(hwnd, lparam):
            proc_id = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if proc_id.value == pid:
                length = u32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == title:
                    found.append(hwnd)
                    return False  # 找到即停止枚举
            return True

        u32.EnumWindows(_callback, 0)
        return found[0] if found else 0

    def _reapply_worker(self):
        """找到窗口后延迟重设标题栏主题，覆盖启动后期边框被重建的场景。"""
        for delay in (0.5, 3, 7, 15, 30):
            time.sleep(delay)
            self._apply_titlebar_theme()

    def _apply_window_icon(self):
        """用 assets/ycode.ico 替换窗口标题栏与任务栏图标（WM_SETICON）。"""
        if not self._hwnd or sys.platform != "win32":
            return
        try:
            if getattr(sys, "frozen", False):
                base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
            else:
                base = os.path.dirname(os.path.abspath(__file__))
            ico_path = os.path.join(base, "assets", "ycode.ico")
            if not os.path.exists(ico_path):
                return
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            u32.LoadImageW.restype = wintypes.HANDLE
            u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
            LR_LOADFROMFILE = 0x00000010
            IMAGE_ICON = 1
            WM_SETICON = 0x0080
            # ICON_BIG=1 任务栏/Alt-Tab，ICON_SMALL=0 标题栏左上角
            big = u32.LoadImageW(None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            small = u32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            if big:
                u32.SendMessageW(self._hwnd, WM_SETICON, 1, big)
            if small:
                u32.SendMessageW(self._hwnd, WM_SETICON, 0, small)
            # LoadImageW 创建的 HICON 所有权归调用方（WM_SETICON 不接管），
            # 发完消息必须 DestroyIcon，否则每次设置都泄漏两个 GDI 句柄
            if big:
                u32.DestroyIcon(big)
            if small:
                u32.DestroyIcon(small)
        except Exception:
            logger.debug("设置窗口图标失败", exc_info=True)

    # 与前端 style.css 主题变量保持一致（标题栏取 --bg1 与应用顶栏无缝衔接，文字取 --text）
    _TITLEBAR_COLORS = {
        # dark:  (caption, text)   COLORREF = 0x00BBGGRR
        True:  (0x001E2020, 0x00D4D9D9),  # #20201e / #d9d9d4
        False: (0x00FFFFFF, 0x002A2C2C),  # #ffffff / #2c2c2a
    }

    def _apply_titlebar_theme(self):
        """把当前标题栏主题写入 DWM；19 兼容旧版 Win10，20 为 Win11/新版 Win10。
        Win11 22000+ 额外用 CAPTION/TEXT/BORDER_COLOR 让标题栏颜色与界面背景完全一致。"""
        if not self._hwnd or sys.platform != "win32":
            return
        try:
            import ctypes
            dwm = ctypes.windll.dwmapi
            val = ctypes.c_int(1 if self._titlebar_dark else 0)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
                if dwm.DwmSetWindowAttribute(
                    self._hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)
                ) == 0:
                    break
            caption, text = self._TITLEBAR_COLORS[self._titlebar_dark]
            # DWMWA_CAPTION_COLOR=35 / TEXT_COLOR=36 / BORDER_COLOR=34（Win11 起支持，失败忽略）
            for attr, color in ((35, caption), (36, text), (34, caption)):
                cval = ctypes.c_int(color)
                dwm.DwmSetWindowAttribute(
                    self._hwnd, attr, ctypes.byref(cval), ctypes.sizeof(cval)
                )
        except Exception:
            logger.debug("设置标题栏主题失败", exc_info=True)

    def set_titlebar_theme(self, dark: bool) -> Dict[str, Any]:
        """前端主题切换时同步窗口标题栏明暗，避免深色内容配白色标题栏刺眼。"""
        self._titlebar_dark = bool(dark)
        self._apply_titlebar_theme()
        return {"ok": True}

    def set_gui_theme(self, theme_light: bool) -> Dict[str, Any]:
        """持久化界面主题明暗（原子写入 config.json），并同步窗口标题栏。

        用户关闭再打开后主题保持不变：启动时由 get_initial_state 的
        theme_light 字段恢复。默认 False（深色）。
        """
        light = bool(theme_light)
        from config import save_config_value
        ok = save_config_value("gui_theme_light", light)
        self.set_titlebar_theme(not light)
        return {"ok": bool(ok)}

    def get_changelog(self) -> Dict[str, Any]:
        """解析随安装包发布的 assets/CHANGELOG.md，供关于页展示更新内容。

        格式约定：每个版本一节，标题行 `## <版本> (<日期>)`，
        其后每行一条 `- <内容>`。返回按文件顺序（最新在前）的结构化条目。
        """
        try:
            if getattr(sys, "frozen", False):
                base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            else:
                base = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(base, "assets", "CHANGELOG.md")
            if not os.path.isfile(path):
                return {"ok": False, "entries": [], "msg": "更新日志文件缺失"}
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()

            entries: List[Dict[str, Any]] = []
            version = ""
            date = ""
            notes: List[str] = []
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if line.startswith("## "):
                    if version:
                        entries.append({"version": version, "date": date, "notes": notes})
                    title = line[3:].strip()
                    m = re.match(r"^(\S+)\s*[(（](.+?)[)）]$", title)
                    if m:
                        version, date = m.group(1), m.group(2)
                    else:
                        version, date = title, ""
                    notes = []
                elif line.startswith("- ") and version:
                    notes.append(line[2:].strip())
            if version:
                entries.append({"version": version, "date": date, "notes": notes})
            return {"ok": True, "entries": entries}
        except Exception as e:
            logger.exception("解析更新日志失败")
            return {"ok": False, "entries": [], "msg": str(e)}

    def _sync_active_model_context(self, session_name: str | None = None):
        """把当前激活模型同步给 app_context，供 core 层（子代理等）读取。

        优先按传入的 session_name 取会话级模型；未传则取当前线程会话；
        都没有则回退到全局默认模型。确保 GUI 各会话、CLI 互不干扰。
        """
        try:
            resolved_session = session_name or app_context.get_thread_session()
            active = get_active_model(resolved_session) or {}
            app_context.set_active_model_tuple(
                active.get("litellm_model", ""),
                active.get("api_key", ""),
                active.get("base_url", ""),
            )
        except Exception:
            logger.debug("同步激活模型到 app_context 失败", exc_info=True)

    def _get_cancel_event(self, session_name: str) -> threading.Event:
        """获取指定会话的取消事件（不存在则创建）。"""
        if session_name not in self._cancel_events:
            self._cancel_events[session_name] = threading.Event()
        return self._cancel_events[session_name]

    def _is_turn_active(self, session_name: str) -> bool:
        """检查指定会话是否有正在运行的回合。"""
        # getattr 兜底：测试里的裸实例（__new__ 构造）不经过 __init__
        t = getattr(self, "_active_turns", {}).get(session_name)
        return t is not None and t.is_alive()

    def emit_event(self, event_type: str, data: Any):
        """向 JS 传递结构化事件（非流式事件会先冲刷 token 缓冲，保证顺序）。"""
        if self._shutting_down:
            return
        if event_type not in ("token_delta", "thinking_delta"):
            self._flush_deltas()
        self._emit_raw(event_type, data)

    def _emit_raw(self, event_type: str, data: Any):
        if self._shutting_down or not self._window:
            return
        try:
            # ensure_ascii=False 保留中文原文（体积更小、调试可读）；
            # JSON 是 JS 字面量的子集，唯二的例外是 U+2028 / U+2029
            # （JS 里属于行终止符，直接嵌入会造成语法错误），单独转义即可。
            payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
            payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        except Exception as e:
            logger.warning("序列化事件 %s 失败: %s", event_type, e)
            return
        self._ensure_js_dispatcher()
        try:
            self._js_queue.put_nowait(payload)
        except queue.Full:
            # 渲染进程挂起/死亡时队列会持续占满：丢弃新事件（保留已有的顺序事件），
            # 生产者绝不阻塞。计数限频记日志，便于事后诊断。
            self._js_dropped += 1
            if self._js_dropped == 1 or self._js_dropped % 500 == 0:
                logger.warning(
                    "JS 事件队列已满，丢弃事件（累计 %d）：渲染进程可能已挂起",
                    self._js_dropped,
                )

    def _ensure_js_dispatcher(self):
        """惰性启动 JS 事件派发线程：单消费者保证事件顺序，evaluate_js 只在此线程调用。"""
        t = self._js_dispatcher
        if t is not None and t.is_alive():
            return
        with self._js_dispatcher_lock:
            t = self._js_dispatcher
            if t is not None and t.is_alive():
                return
            t = threading.Thread(
                target=self._js_dispatcher_loop,
                daemon=True,
                name="gui-js-dispatcher",
            )
            self._js_dispatcher = t
            t.start()

    def _js_dispatcher_loop(self):
        while True:
            payload = self._js_queue.get()
            if payload is None:  # 关闭哨兵
                return
            # T9 时序闭环说明：shutdown() 先置 _shutting_down（_emit_raw/emit_event
            # 此后直接返回，生产者侧已封闭），再把哨兵放入队列。哨兵前排队的
            # 事件仍会被消费——此时窗口通常还活着，投递是期望行为；若窗口已在
            # 销毁中，evaluate_js 抛异常被下方捕获丢弃。shutdown 之后不会再有
            # 新事件入队（生产者被标志位挡住），所以不存在“销毁后 dispatcher
            # 仍持续调 evaluate_js”的窗口期漏洞；最坏情况是 dispatcher 阻塞在
            # 空队列的 get() 上，守护线程随进程退出。
            try:
                self._window.evaluate_js(
                    f"window.onBridgeEvent && window.onBridgeEvent({payload})"
                )
            except Exception as e:
                # WebView 销毁/跳转中 evaluate_js 会抛；事件丢掉即可，绝不带出线程
                logger.debug("evaluate_js 失败: %s", e)

    # -- 流式增量的批量推送 ------------------------------------------------
    def _emit_delta(self, kind: str, text: str, session_name: str = ""):
        """累积流式文本，最多每 TOKEN_FLUSH_INTERVAL 推送一次。按会话隔离缓冲。"""
        if not text:
            return
        flush_now = False
        swapped = None
        with self._delta_lock:
            buf = self._delta_buffers.setdefault(session_name, [])
            cur_kind = self._delta_kinds.get(session_name, "token_delta")
            if kind != cur_kind and buf:
                # 思考流与正文流切换时先把旧缓冲吐出去，避免串台。
                # 注意只在锁内取出内容，emit 放到锁外，绝不在持锁时触碰 JS 桥。
                swapped = (cur_kind, "".join(buf))
                buf.clear()
            self._delta_kinds[session_name] = kind
            buf.append(text)
            now = time.monotonic()
            if now - self._delta_last_flush >= TOKEN_FLUSH_INTERVAL:
                flush_now = True
        if swapped is not None:
            self._emit_raw(swapped[0], {"text": swapped[1], "session_name": session_name})
        self._ensure_delta_flusher()
        if flush_now:
            self._flush_deltas()

    def _flush_deltas(self):
        with self._delta_lock:
            if not self._delta_buffers:
                return
            pending = []
            for session_name, buf in self._delta_buffers.items():
                if not buf:
                    continue
                kind = self._delta_kinds.get(session_name, "token_delta")
                text = "".join(buf)
                buf.clear()
                pending.append((kind, text, session_name))
            self._delta_last_flush = time.monotonic()
        for kind, text, session_name in pending:
            self._emit_raw(kind, {"text": text, "session_name": session_name})

    def _ensure_delta_flusher(self):
        """惰性启动尾包冲刷线程：保证最后一小段文本不会滞留在缓冲里。
        双检加锁：多会话并发首次触发时不得重复创建 flusher。"""
        if self._delta_flusher is not None and self._delta_flusher.is_alive():
            return
        lock = getattr(self, "_delta_flusher_lock", None)
        if lock is None:  # 裸实例兜底（测试不经过 __init__）
            lock = self._delta_flusher_lock = threading.Lock()
        with lock:
            if self._delta_flusher is not None and self._delta_flusher.is_alive():
                return

            def _loop():
                while True:
                    time.sleep(TOKEN_FLUSH_INTERVAL)
                    try:
                        self._flush_deltas()
                    except Exception:
                        logger.debug("冲刷流式缓冲失败", exc_info=True)

            self._delta_flusher = threading.Thread(target=_loop, daemon=True, name="gui-delta-flusher")
            self._delta_flusher.start()

    # ------------------------------------------------------------------
    # 初始状态
    # ------------------------------------------------------------------
    def _find_latest_session(self) -> str:
        """查找最近一次活跃的非空会话（按 updated_at 排序），没有则返回空串。"""
        try:
            sessions = self._memory.get_all_sessions(workdir=None)
            if not sessions:
                return ""
            latest = max(sessions, key=lambda s: s.get("updated_at") or s.get("created_at") or "")
            return str(latest.get("name") or "")
        except Exception:
            logger.debug("查找最近会话失败", exc_info=True)
            return ""

    def _load_session_messages(self, session_name: str) -> List[Dict[str, Any]]:
        """读取会话历史并转成前端气泡结构（与 switch_session 同一套映射）。"""
        msgs: List[Dict[str, Any]] = []
        try:
            for msg in self._memory.get_history(session_name):
                role = msg.get("role")
                if role in ("user", "assistant", "tool"):
                    msgs.append({
                        "role": role,
                        "content": msg.get("content") or "",
                        "reasoning_content": msg.get("reasoning_content") or "",
                        "tool_calls": msg.get("tool_calls") or [],
                        "name": msg.get("name") or "",
                        "tool_call_id": msg.get("tool_call_id") or "",
                    })
        except Exception:
            logger.debug("加载会话消息失败", exc_info=True)
        return msgs

    def get_initial_state(self) -> Dict[str, Any]:
        # 启动诊断：前端能调到本方法，说明 JS→Python 桥已打通。
        # 若启动卡死时日志里没有这条，说明调用根本没到 Python（桥消息丢失），
        # 而非本方法内部卡住——据此区分"前端没调进来"与"卡在入口"。
        logger.info("get_initial_state 入口被调用（前端桥已打通）")
        try:
            active_model = get_active_model(self._current_session) or {}
            config_data = load_config()
            # 当前会话若已绑定模型，优先展示会话模型；否则展示全局默认
            effective_model_id = active_model.get("id") or config_data.get("active_model_id") or ""

            model_settings = self.get_model_settings()
            models = model_settings["models"]
            providers = model_settings["providers"]

            # 以"当前会话归属"恢复工作区上下文（启动时已自动选中最近一次对话）：
            # GLOBAL 会话显示自由对话；项目会话切到其工作目录并加载对应文件树。
            # 不能用启动目录判断——否则会出现"显示在工作区、实际发进对话"的错位。
            cur_wdir = self._session_workdir(self._current_session)
            is_global = (cur_wdir == "GLOBAL")
            if is_global:
                workdir = "GLOBAL"
                file_tree: List[Dict[str, Any]] = []
            else:
                workdir = _resolve_real_path(cur_wdir)
                # 文件树扫描改为异步：大目录（如 node_modules 深层）扫描可能耗时数秒，
                # 若在 get_initial_state 同步执行会卡住加载页（用户看到"一直转圈"）。
                # 先返回空树，后台扫描完成后通过事件推送到前端刷新文件面板。
                file_tree = []
                threading.Thread(
                    target=self._async_load_file_tree,
                    args=(workdir,),
                    daemon=True,
                    name="gui-file-tree-loader",
                ).start()
            # token 用量重算也改为异步：SQLite 在旧实例未完全退出时可能持锁，
            # 同步等待会卡住启动页。先返回空，后台算好后推送。
            threading.Thread(
                target=self._async_recalc_token_usage,
                args=(self._current_session,),
                daemon=True,
                name="gui-token-recalc",
            ).start()
            return {
                "models": models,
                "providers": providers,
                "active_model_id": str(effective_model_id),
                "active_model_name": str(active_model.get("name") or "未激活模型"),
                # 当前激活模型的推理强度档位（None/"default" = 内置默认）
                "reasoning_effort": get_model_reasoning_effort(effective_model_id) or "default",
                # 副模型配置：启动时同步给前端，避免设置页显示"无/全勾选"假象
                "secondary_model_id": str(config_data.get("secondary_model_id") or ""),
                "secondary_model_roles": get_secondary_roles(),
                "permission_mode": self._current_permission_mode,
                # 沙盒开关（是否允许访问工作目录外文件）：与 CLI /sandbox 共享同一配置键
                "allow_outside_workspace": bool(config_data.get("allow_outside_workspace", False)),
                "sessions": self.get_sessions(),
                "skills": self._build_skill_list(),
                "mcps": self._build_mcp_list(),
                "plugins": self.get_plugins(),
                "workdir": workdir,
                "is_global": is_global,
                "file_tree": file_tree,
                "current_session": self._current_session,
                "messages": self._load_session_messages(self._current_session),
                # M23：当前有活跃回合的会话名列表。窗口重载/崩溃后前端重建时
                # 据此恢复 running 态或解锁输入框（终态事件已在重载期间丢失）
                "active_sessions": [
                    name for name, t in getattr(self, "_active_turns", {}).items()
                    if t.is_alive()
                ],
                "token_usage": self.get_token_usage(),
                # 持久化的界面主题（True=浅色），前端启动时据此恢复，关闭重开不变
                "theme_light": bool(config_data.get("gui_theme_light", False)),
                "version": str(YCODE_VERSION),
            }
        except Exception as e:
            logger.exception("get_initial_state 失败")
            return {
                "models": [], "providers": [], "active_model_id": "",
                "active_model_name": "未激活模型", "permission_mode": "confirm",
                "reasoning_effort": "default",
                "sessions": [], "skills": [], "mcps": [],
                "workdir": "", "current_session": self._current_session,
                "active_sessions": [],
                "token_usage": {"used": 0, "window": 0, "cached_pct": 0, "cumulative": 0},
                "version": str(YCODE_VERSION), "ok": False, "msg": str(e),
            }

    # ------------------------------------------------------------------
    # 文件树 / 工作区
    # ------------------------------------------------------------------
    def _get_file_tree(self, path: str, depth: int = FILE_TREE_MAX_DEPTH) -> List[Dict[str, Any]]:
        """递归扫描目录树；限制深度与每层条目数，跳过体积巨大的构建/依赖目录。

        条目配额只按【每层目录】独立计算，不再设全局总预算：全局预算是
        深度优先扫描，会被字母序靠前目录的子树吃满，导致靠后的顶层条目
        被静默丢弃，表现为文件面板"显示不完全"。
        点开头的隐藏条目照常显示（与文件资源管理器一致）；符号链接显示
        但不递归进入（防循环）。
        """
        def _scan(cur_path: str, cur_depth: int) -> List[Dict[str, Any]]:
            if cur_depth <= 0:
                return []
            items = []
            try:
                entries = sorted(os.listdir(cur_path))
            except Exception:
                return []
            shown = 0
            for entry in entries:
                if shown >= FILE_TREE_MAX_ENTRIES_PER_DIR:
                    break
                if entry in FILE_TREE_SKIP_DIRS:
                    continue
                full_path = os.path.join(cur_path, entry)
                try:
                    is_link = os.path.islink(full_path)
                    is_dir = os.path.isdir(full_path)
                except Exception:
                    continue
                shown += 1
                # 同时提供两套字段名：n/d 供当前前端组件使用，
                # name/type 为语义化别名，便于后续前端演进。
                item: Dict[str, Any] = {
                    "n": str(entry),
                    "d": 1 if is_dir else 0,
                    "name": str(entry),
                    "type": "dir" if is_dir else "file",
                }
                if is_dir and not is_link:
                    children = _scan(full_path, cur_depth - 1)
                    if children:
                        item["children"] = children
                items.append(item)
            return items

        if not path or path == "GLOBAL" or not os.path.isdir(path):
            return []
        return _scan(path, depth)

    def get_file_tree(self, workdir: str) -> Dict[str, Any]:
        """异步拉取指定目录的文件树（会话切换后的后台加载通道）。

        与 switch_session 分离：切换响应只带消息与工作区信息秒回，
        树扫描由前端在切换完成后调用本接口在后台等待，过期结果前端丢弃。
        """
        try:
            if not workdir or workdir == "GLOBAL" or not os.path.isdir(workdir):
                return {"ok": False, "file_tree": []}
            return {"ok": True, "file_tree": self._get_file_tree(workdir)}
        except Exception as e:
            logger.exception("get_file_tree 失败")
            return {"ok": False, "file_tree": [], "msg": str(e)}

    def get_dir_children(self, path: str) -> Dict[str, Any]:
        """按需展开目录：只扫描指定目录的一层子项（前端文件树懒加载用）。

        初始 file_tree 有深度上限，超出层级的目录没有预载 children；
        前端展开这类目录时调用本接口按需取下一层，任意层级都能展开。
        """
        try:
            if not path or path == "GLOBAL" or not os.path.isdir(path):
                return {"ok": False, "children": [], "msg": "not a directory"}
            return {"ok": True, "children": self._get_file_tree(path, depth=1)}
        except Exception as e:
            return {"ok": False, "children": [], "msg": str(e)}

    # @ 引用补全忽略目录（与 CLI _list_workspace_files 保持一致）
    _FILE_SUGGEST_IGNORE_DIRS = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode",
    }
    _FILE_SUGGEST_CACHE_TTL = 5.0

    def _list_workspace_files_for_suggest(self) -> List[str]:
        """递归扫描当前工作区文件（/ 分隔相对路径），TTL 缓存避免输入时反复 walk。"""
        now = time.time()
        cache = getattr(self, "_file_suggest_cache", None)
        if cache and now - cache[0] < self._FILE_SUGGEST_CACHE_TTL:
            return cache[1]
        results: List[str] = []
        for root, dirs, files in os.walk("."):
            dirs[:] = [
                d for d in dirs
                if d not in self._FILE_SUGGEST_IGNORE_DIRS and not d.startswith(".")
            ]
            for f in files:
                if f.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(root, f), ".").replace("\\", "/")
                results.append(rel)
        self._file_suggest_cache = (now, results)
        return results

    def search_workspace_files(self, query: str = "", limit: int = 50) -> Dict[str, Any]:
        """@ 引用自动补全：大小写不敏感子串匹配工作区文件（与 CLI 语义一致）。"""
        try:
            q = str(query or "").strip().lower()
            files = self._list_workspace_files_for_suggest()
            if q:
                files = [f for f in files if q in f.lower()]
            return {"ok": True, "files": files[:max(1, int(limit or 50))]}
        except Exception as e:
            logger.exception("search_workspace_files 失败")
            return {"ok": False, "msg": str(e), "files": []}

    def get_workspace_state(self, workdir: str) -> Dict[str, Any]:
        """读取显式项目目录的前端快照，不依赖进程当前工作目录。"""
        try:
            path = _resolve_real_path(str(workdir or ""))
            if not workdir or workdir == "GLOBAL" or not os.path.isdir(path):
                return {"ok": False, "msg": f"项目文件夹不存在: {workdir or '<empty>'}"}
            return {
                "ok": True,
                "workdir": path,
                "file_tree": self._get_file_tree(path),
            }
        except Exception as e:
            logger.exception("get_workspace_state 失败")
            return {"ok": False, "msg": str(e)}

    def select_workspace_folder(self) -> Dict[str, Any]:
        """弹出原生文件夹对话框，选择并切换项目工作区文件夹。"""
        try:
            import webview
            if not self._window:
                return {"ok": False, "msg": "窗口尚未就绪"}
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=os.getcwd(),
            )
            if not result:
                return {"ok": False, "msg": "已取消选择"}
            selected_path = _resolve_real_path(result[0])
            if not os.path.isdir(selected_path):
                return {"ok": False, "msg": "选择的路径不是文件夹"}
            self._memory.add_workspace(selected_path)
            # 仅全局自由对话（GLOBAL）跟随迁入所选工作区：否则启动时挂在 GLOBAL
            # 的会话即使已在项目文件夹里干活，侧栏仍会一直显示在"对话"分组下。
            # 已归属其他项目的会话绝不能在此改写归属（否则引入新目录会把别的
            # 项目的会话"搬"进新目录）
            if self._current_session and self._session_workdir(self._current_session) == "GLOBAL":
                self._memory.update_session_workdir(self._current_session, selected_path)
            return {
                "ok": True,
                "workdir": selected_path,
                "file_tree": self._get_file_tree(selected_path),
                "sessions": self.get_sessions(),
                "skills": self._build_skill_list(),
            }
        except Exception as e:
            logger.exception("select_workspace_folder 失败")
            return {"ok": False, "msg": str(e)}

    def _async_load_file_tree(self, workdir: str):
        """异步加载文件树：后台扫描完成后通过事件推送到前端刷新文件面板。

        原同步实现在 get_initial_state 中执行，大目录（node_modules 深层、
        大量子目录）扫描耗时数秒，导致启动页卡死（前端一直在等
        get_initial_state 返回，"交换页面"永远转圈）。改为异步后，
        get_initial_state 立即返回空 file_tree，前端先渲染出主界面；
        扫描完成后通过 file_tree_loaded 事件推送到前端，文件面板即时刷新。
        """
        try:
            tree = self._get_file_tree(workdir)
            self._emit_raw("file_tree_loaded", {
                "file_tree": tree,
                "workdir": workdir,
            })
            logger.info("异步文件树加载完成: %s（%d 项）", workdir, len(tree))
        except Exception:
            logger.warning("异步文件树加载失败: %s", workdir, exc_info=True)

    def _async_recalc_token_usage(self, session_name: str):
        """异步重算 token 用量：SQLite 锁竞争时不再阻塞启动页。

        原同步实现在 get_initial_state 中调用 _recalc_token_usage，
        当旧实例未完全退出（WAL 锁仍被持有）或 ycode_gui.db 较大时，
        该调用可能卡住数十秒，表现为启动页转圈。改为后台执行，
        完成后推送 token_usage_updated 事件让前端刷新用量显示。
        """
        try:
            self._recalc_token_usage(session_name)
            usage = self.get_token_usage()
            self._emit_raw("token_usage_updated", usage)
            logger.info("异步 token 用量重算完成: %s", session_name)
        except Exception:
            logger.debug("异步 token 用量重算失败: %s", session_name, exc_info=True)

    # ------------------------------------------------------------------
    # 会话持久化：历史会话列表
    # ------------------------------------------------------------------
    def get_sessions(self) -> List[Dict[str, Any]]:
        try:
            # include_empty=True：空会话交给下方自己的跳过逻辑处理（当前会话
            # 即使还没有消息也要显示），否则刚新建的会话在侧栏永远是 0 个会话
            raw = self._memory.get_all_sessions(workdir=None, source="GUI", include_empty=True)
            saved_workspaces = self._memory.get_all_workspaces()
            current_wdir = _resolve_real_path(os.getcwd())
            app_dir = _app_install_dir()
            # “当前”标记必须以【当前会话归属】为准，不能用进程 cwd：
            # 恢复最近会话会 chdir 到项目目录，此时新建自由对话，
            # 若按 cwd 标记就会出现“对话”和项目分组同时显示“当前”
            current_session_wdir = self._session_workdir(self._current_session)

            if current_wdir != "GLOBAL" and current_wdir != app_dir:
                self._memory.add_workspace(current_wdir)

            groups_dict: Dict[str, Dict[str, Any]] = {}

            # 自由通用对话分组（独立于任何项目文件夹）
            global_group = {
                "folder_path": "GLOBAL",
                "folder_name": "对话",
                "is_global": True,
                "is_current": (current_session_wdir == "GLOBAL"),
                "sessions": [],
            }

            # 1. 初始化所有已记录引入过的项目文件夹，哪怕会话数为 0 也保留
            all_known_wdirs = set(saved_workspaces)
            if current_wdir != "GLOBAL" and current_wdir != app_dir:
                all_known_wdirs.add(current_wdir)

            for s in raw:
                wdir_item = s.get("workdir")
                if wdir_item and wdir_item != "GLOBAL":
                    all_known_wdirs.add(_resolve_real_path(wdir_item))

            for w_path in all_known_wdirs:
                if not w_path or w_path == "GLOBAL":
                    continue
                norm_p = _resolve_real_path(w_path)
                folder_name = os.path.basename(norm_p) or norm_p
                groups_dict[norm_p] = {
                    "folder_path": norm_p,
                    "folder_name": folder_name,
                    "is_current": (norm_p == current_session_wdir),
                    "sessions": [],
                }

            # 2. 填充各分组的具体会话
            for s in raw:
                msg_count = s.get("count", 0)
                name = s.get("name", "")
                is_default_name = bool(re.match(r"^session_[a-f0-9]{6}$", name))

                # 自动跳过没有消息记录的垃圾临时会话
                if msg_count == 0 and is_default_name and name != self._current_session:
                    continue

                wdir = s.get("workdir")
                display = "新会话" if is_default_name else name

                # 无 workdir 的历史会话（旧数据/CLI 产生）固定归入"对话"分组；
                # 不能兜底跟随进程 cwd，否则引入新目录 chdir 后它们会集体"搬"
                # 到新目录下，表现为会话归属错乱
                if not wdir or wdir == "GLOBAL":
                    global_group["sessions"].append({
                        "name": name,
                        "display_name": display,
                        "count": msg_count,
                        "workdir": "GLOBAL",
                        "is_global": True,
                        "updated_at": s.get("updated_at") or "",
                    })
                    continue

                wdir_norm = _resolve_real_path(wdir)
                s_info = {
                    "name": name,
                    "display_name": display,
                    "count": msg_count,
                    "workdir": wdir_norm,
                    "is_global": False,
                    "updated_at": s.get("updated_at") or "",
                }

                if wdir_norm not in groups_dict:
                    folder_name = os.path.basename(wdir_norm) or wdir_norm
                    groups_dict[wdir_norm] = {
                        "folder_path": wdir_norm,
                        "folder_name": folder_name,
                        "is_current": (wdir_norm == current_session_wdir),
                        "sessions": [],
                    }
                groups_dict[wdir_norm]["sessions"].append(s_info)

            # 3. 全局对话组无条件保留在最顶部，项目分组按名称稳定排序。
            # 不能把"当前会话所在目录"置顶：否则点击不同目录的会话时列表
            # 会整体跳动
            result_groups = [global_group]
            project_groups = list(groups_dict.values())
            project_groups.sort(key=lambda g: g["folder_name"])
            result_groups.extend(project_groups)
            return result_groups
        except Exception:
            logger.exception("get_sessions 失败")
            return []

    def create_session(self, title: str = "新会话", workdir: Optional[str] = None) -> Dict[str, Any]:
        try:
            new_name = f"session_{uuid.uuid4().hex[:6]}"
            target_workdir = workdir or _resolve_real_path(os.getcwd())
            self._memory.create_session_explicit(new_name, workdir=target_workdir, source="GUI")
            self._set_current_session(new_name)
            # 全局 session_token_usage 不按会话隔离，旧会话的 latest_total 会
            # 残留到新会话；新建后立即按空历史重算为 0，避免窗口占比虚高
            self._recalc_token_usage(new_name)
            return {
                "ok": True,
                "session_name": new_name,
                "title": title,
                "display_name": title,
                "workdir": target_workdir,
            }
        except Exception as e:
            logger.exception("create_session 失败")
            return {"ok": False, "msg": str(e)}

    def create_global_session(self, title: str = "新对话") -> Dict[str, Any]:
        """创建跳出文件夹限定的独立通用对话"""
        try:
            new_name = f"session_{uuid.uuid4().hex[:6]}"
            self._memory.create_session_explicit(new_name, workdir="GLOBAL", source="GUI")
            self._set_current_session(new_name)
            # 同 create_session：清掉旧会话残留在全局 latest_total 的窗口占用
            self._recalc_token_usage(new_name)
            return {
                "ok": True,
                "session_name": new_name,
                "title": title,
                "display_name": title,
                "workdir": "GLOBAL",
            }
        except Exception as e:
            logger.exception("create_global_session 失败")
            return {"ok": False, "msg": str(e)}

    def _set_current_session(self, session_name: str):
        """切换当前会话，并同步所有与会话绑定的运行时状态。"""
        self._current_session = session_name
        app_context.set_current_session(session_name)
        try:
            from core.plan import bind_default_plan_session
            bind_default_plan_session(session_name)
        except Exception:
            logger.debug("绑定计划会话失败", exc_info=True)
        try:
            from core import tool_cache
            tool_cache.set_current_session(session_name)
        except Exception:
            logger.debug("绑定工具缓存会话失败", exc_info=True)
        try:
            from core.memory import set_undo_session
            set_undo_session(session_name)
        except Exception:
            logger.debug("绑定撤销会话失败", exc_info=True)

    def switch_session(self, session_name: str, target_workdir: Optional[str] = None) -> Dict[str, Any]:
        try:
            self._set_current_session(session_name)

            stored_wdir = None
            # include_empty=True：刚新建的空会话（如删除当前会话后自动创建的
            # GLOBAL 会话）还没有消息，不过滤掉才能拿到它的 GLOBAL 归属，
            # 否则 is_global 误判为 False，工作区标签会错指到项目文件夹
            for s in self._memory.get_all_sessions(workdir=None, include_empty=True):
                if s.get("name") == session_name:
                    stored_wdir = s.get("workdir")
                    break

            is_global = (target_workdir == "GLOBAL" or stored_wdir == "GLOBAL")

            if not is_global:
                # 优先使用会话登记的 workdir：未传 target_workdir 的调用路径
                # （如删除当前会话后的自动切换）若退回进程 cwd，会话 A 的工作区
                # 会错显示成上一个项目 B 的目录
                candidate_wdir = target_workdir or stored_wdir
                if candidate_wdir and candidate_wdir != "GLOBAL" and os.path.isdir(candidate_wdir):
                    candidate_wdir = _resolve_real_path(candidate_wdir)

            msgs = self._load_session_messages(session_name)

            self._recalc_token_usage(session_name)
            # 切换会话后把该会话绑定的模型信息一并返回给前端，
            # 让状态栏与模型选择器随会话自动恢复，不再互相覆盖
            session_model = get_active_model(session_name) or {}
            result = {
                "ok": True,
                "session_name": session_name,
                "messages": msgs,
                "skills": self._build_skill_list(),
                "sessions": self.get_sessions(),
                "active_model_id": str(session_model.get("id") or ""),
                "active_model_name": str(session_model.get("name") or "未激活模型"),
                "reasoning_effort": get_model_reasoning_effort(session_model.get("id")) or "default",
            }
            if is_global:
                result.update({"workdir": "GLOBAL", "is_global": True})
            else:
                current_workdir = _resolve_real_path(candidate_wdir or stored_wdir or os.getcwd())
                result.update({"workdir": current_workdir, "is_global": False})
            # 文件树不再随切换响应同步扫描（大目录要数秒，拖慢切换）；
            # 前端切换成功后调 get_file_tree 后台异步加载。
            self._emit_token_usage()
            return result
        except Exception as e:
            logger.exception("switch_session 失败")
            return {"ok": False, "msg": str(e), "messages": []}

    def rename_session(self, old_name: str, new_name: str) -> Dict[str, Any]:
        try:
            # M21：回合运行中禁止重命名——运行中的回合终态事件带的是旧会话名，
            # 重命名后前端 running 态与新名失配（输入框锁死/状态残留）
            if self._is_turn_active(old_name):
                return {"ok": False, "msg": "该会话正在运行回合，请先停止或等待完成后再重命名", "name": old_name}
            success, final_name = self._memory.rename_session(old_name, new_name)
            if not success:
                return {"ok": False, "msg": "重命名失败（可能目标名称已存在）", "name": old_name}
            if self._current_session == old_name:
                self._set_current_session(final_name)
            return {"ok": True, "name": final_name, "sessions": self.get_sessions()}
        except Exception as e:
            logger.exception("rename_session 失败")
            return {"ok": False, "msg": str(e), "name": old_name}

    def delete_session(self, session_name: str) -> Dict[str, Any]:
        try:
            # M21：回合运行中禁止删除——回合线程会继续往已删会话写消息，
            # 产生幽灵数据；最小方案是直接拒绝，让用户先停止或等待完成
            if self._is_turn_active(session_name):
                return {"ok": False, "success": False, "msg": "该会话正在运行回合，请先停止或等待完成后再删除"}
            self._memory.delete_session(session_name)
            if self._current_session == session_name:
                new_session = f"session_{uuid.uuid4().hex[:6]}"
                self._memory.create_session_explicit(new_session, workdir="GLOBAL", source="GUI")
                self._set_current_session(new_session)
            return {
                "ok": True,
                "success": True,
                "sessions": self.get_sessions(),
                "current_session": self._current_session,
            }
        except Exception as e:
            logger.exception("delete_session 失败")
            return {"ok": False, "success": False, "msg": str(e)}

    def delete_folder_group(self, workdir: str) -> Dict[str, Any]:
        """移除项目文件夹分组：仅删除 DB 记录中的会话与分组记录，绝不删除磁盘文件"""
        try:
            self._memory.delete_sessions_by_workdir(workdir)
            self._memory.delete_workspace(workdir)
            return {"ok": True, "success": True, "sessions": self.get_sessions()}
        except Exception as e:
            logger.exception("delete_folder_group 失败")
            return {"ok": False, "success": False, "msg": str(e)}

    def clear_session_history(self, session_name: Optional[str] = None) -> Dict[str, Any]:
        try:
            target = session_name or self._current_session
            deleted = self._memory.clear_session_messages(target)
            from core.llm import session_token_usage, token_usage_lock
            with token_usage_lock:
                session_token_usage["latest_total"] = 0
                session_token_usage["latest_prompt"] = 0
                session_token_usage["latest_cached"] = 0
            self._emit_token_usage()
            return {"ok": True, "deleted": int(deleted), "msg": f"已清空 {deleted} 条历史记录"}
        except Exception as e:
            logger.exception("clear_session_history 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 模型与权限模式
    # ------------------------------------------------------------------
    def set_model(self, model_id: str) -> Dict[str, Any]:
        try:
            config_data = load_config()
            matched = None
            for provider in config_data.get("providers", []):
                for m in provider.get("models", []):
                    if str(m.get("id")) == str(model_id):
                        matched = m
                        break
                if matched:
                    break
            if matched is None:
                return {"ok": False, "msg": f"未找到模型: {model_id}"}

            session_name = self._current_session
            if session_name:
                # GUI 按会话绑定主模型：不同会话、不同界面互不干扰
                self._memory.set_session_model_id(session_name, str(model_id))
            else:
                # 尚未进入任何会话（启动初始化阶段）时仍回写全局默认值
                set_active_model(str(model_id))
            self._sync_active_model_context()
            # 切模型后 window 立即变为新模型的 context_window，但全局
            # latest_total 还是旧值（可能属于别的会话），必须先按当前会话
            # 历史重算 used，否则前端会算出 600K/256K=243% 这类虚假占比
            self._recalc_token_usage(session_name)
            self._emit_token_usage()
            # 从大窗口模型切到小窗口模型时，若当前会话占用已超新窗口的压缩
            # 阈值，立即压缩历史，保证新窗口模型可直接使用而不是发消息报错。
            # 压缩后再次重算并推送，让前端红圈及时回落。
            self._ensure_fits_active_window(session_name)
            return {
                "ok": True,
                "active_model_id": str(model_id),
                "active_model_name": str(matched.get("name", "")),
                # 模型切换后前端需同步该模型自己的推理强度档位
                "reasoning_effort": str(matched.get("reasoning_effort") or "default"),
            }
        except Exception as e:
            logger.exception("set_model 失败")
            return {"ok": False, "msg": str(e)}

    def set_reasoning_effort(self, level: str) -> Dict[str, Any]:
        """设置当前激活模型的推理强度档位（按模型持久化，下一轮请求生效）。"""
        try:
            active_id = str(load_config().get("active_model_id") or "")
            if not active_id:
                return {"ok": False, "msg": "当前没有激活模型"}
            ok, message = set_model_reasoning_effort(active_id, level)
            if not ok:
                return {"ok": False, "msg": message}
            effective = get_model_reasoning_effort(active_id) or "default"
            # Anthropic 路由不注入 reasoning_effort（走 thinking_budget），明确告知前端
            litellm_model = str((get_active_model() or {}).get("litellm_model") or "")
            if litellm_model.split("/")[0].lower() == "anthropic":
                message += "（当前模型走 Anthropic 路由，该档位不会注入请求）"
            return {"ok": True, "reasoning_effort": effective, "msg": message}
        except Exception as e:
            logger.exception("set_reasoning_effort 失败")
            return {"ok": False, "msg": str(e)}

    def set_secondary_model(self, model_id: str) -> Dict[str, Any]:
        """设置/清除副模型（用于视觉/压缩/子代理等后台任务）。传空字符串清除。"""
        try:
            mid = str(model_id or "").strip()
            if mid:
                # 校验模型存在性
                config_data = load_config()
                found = False
                for provider in config_data.get("providers", []):
                    for m in provider.get("models", []):
                        if str(m.get("id")) == mid:
                            found = True
                            break
                    if found:
                        break
                if not found:
                    return {"ok": False, "msg": f"未找到模型: {mid}"}
                set_secondary_model(mid)
                self._sync_active_model_context()
                return {"ok": True, "settings": self.get_model_settings(),
                        "msg": "副模型已设置"}
            else:
                set_secondary_model(None)
                self._sync_active_model_context()
                return {"ok": True, "settings": self.get_model_settings(),
                        "msg": "副模型已清除"}
        except Exception as e:
            logger.exception("set_secondary_model 失败")
            return {"ok": False, "msg": str(e)}

    def set_secondary_roles(self, roles: dict) -> Dict[str, Any]:
        """更新副模型用途开关（vision/compression/subagent）。"""
        try:
            set_secondary_roles({
                "vision": bool(roles.get("vision", True)),
                "compression": bool(roles.get("compression", True)),
                "subagent": bool(roles.get("subagent", True)),
            })
            self._sync_active_model_context()
            return {"ok": True, "settings": self.get_model_settings(),
                    "msg": "副模型用途已更新"}
        except Exception as e:
            logger.exception("set_secondary_roles 失败")
            return {"ok": False, "msg": str(e)}

    def set_permission_mode(self, mode: str) -> Dict[str, Any]:
        try:
            # 规范化会把无法识别的值兜底成 confirm；这里要明确告诉前端"没识别出来"，
            # 而不是让界面显示成一次成功的切换。
            if not app_context.is_known_permission_mode(mode):
                return {
                    "ok": False,
                    "msg": f"未知权限模式: {mode}",
                    "permission_mode": self._current_permission_mode,
                }
            canonical = app_context.normalize_permission_mode(mode)
            self._current_permission_mode = canonical
            app_context.set_permission_mode(canonical)
            try:
                from config import save_config_value
                save_config_value("permission_mode", canonical)
            except Exception:
                logger.warning("持久化权限模式失败", exc_info=True)
            return {"ok": True, "permission_mode": canonical}
        except Exception as e:
            logger.exception("set_permission_mode 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 沙盒开关与持久化白名单（与 CLI 的 /sandbox、/whitelist 同一配置与存储）
    # ------------------------------------------------------------------
    def set_sandbox_enabled(self, enabled: bool) -> Dict[str, Any]:
        """沙盒开关：是否允许访问工作目录之外的文件（写 config，CLI/GUI 双端热生效）。"""
        try:
            from config import save_config_value

            save_config_value("allow_outside_workspace", bool(enabled))
            return {"ok": True, "allow_outside_workspace": bool(enabled)}
        except Exception as e:
            logger.exception("set_sandbox_enabled 失败")
            return {"ok": False, "msg": str(e)}

    def get_permission_rules(self) -> Dict[str, Any]:
        """返回当前项目的持久化白名单（含沙盒开关状态），供设置页展示。"""
        try:
            from core import permission_store
            from core.model_manager import load_config
            from core.tools import is_session_sandbox_bypass_enabled

            rules = permission_store.project_rules()
            def _rule_text(rule):
                import subprocess
                try:
                    return subprocess.list2cmdline(rule)
                except Exception:
                    return " ".join(rule)
            return {
                "ok": True,
                "workdir": permission_store.canonical_workdir(),
                "allow_outside_workspace": bool(load_config().get("allow_outside_workspace", False)),
                "session_sandbox_bypass": is_session_sandbox_bypass_enabled(),
                "commands": [_rule_text(rule) for rule in sorted(rules["commands"])],
                "write_files": sorted(rules["write_files"]),
                "edit_files": sorted(rules["edit_files"]),
            }
        except Exception as e:
            logger.exception("get_permission_rules 失败")
            return {"ok": False, "msg": str(e)}

    def clear_permission_rules(self) -> Dict[str, Any]:
        """清空当前项目的持久化白名单（会话级白名单与沙盒会话放行不受影响）。"""
        try:
            from core import permission_store

            removed = permission_store.clear_project()
            return {"ok": True, "removed": removed}
        except Exception as e:
            logger.exception("clear_permission_rules 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 权限确认：统一走 core.permission_broker（与 CLI 同一套串行化机制）
    # ------------------------------------------------------------------
    def _activate_permission_request(self, request):
        """broker 激活某个请求 → 推送到前端对话流内联卡片，并挂一个"超时即拒绝"的定时器。"""
        try:
            from core.tool_presenter import describe_permission_request
            options = list(request.options or ())
            self.emit_event("permission_request", {
                "session_name": _session_from_turn_id(request.parent_turn_id),
                "request_id": request.request_id,
                "function_name": request.function_name,
                # 兼容字段：部分旧界面读 tool_name / details
                "tool_name": request.function_name,
                "arguments": dict(request.arguments or {}),
                "details": dict(request.arguments or {}),
                # 人类可读描述（执行什么命令 / 查看什么文件），前端直接展示
                "summary": describe_permission_request(
                    request.function_name, dict(request.arguments or {})
                ),
                "source": request.source,
                "options": options,
                # 沙盒外访问等场景只给 同意/拒绝 两项，不渲染"总是允许"
                "allow_always": len(options) >= 3,
            })
        except Exception:
            logger.exception("推送权限请求失败")

        # 超时语义与 CLI 对齐：权限确认无限等待，绝不因用户暂时离开而自动拒绝。
        # （自动拒绝会让模型拿到"用户拒绝"反馈继续跑偏甚至反复重试；无限等待则
        #   任务安全暂停，用户回来后仍可作答，或手动停止回合取消。）
        # ask_user 提问类保留超时=取消，防止无人值守时回合永久卡死在提问上。
        if request.function_name != "ask_user":
            return

        def _timeout_cancel():
            try:
                if permission_broker.is_active(request.request_id):
                    from core.permission_broker import CANCELLED_RESULT
                    permission_broker.resolve(request.request_id, CANCELLED_RESULT)
                    self.emit_event("error", {
                        "session_name": _session_from_turn_id(request.parent_turn_id),
                        "message": "问题确认超时，已自动取消。",
                    })
            except Exception:
                logger.debug("提问超时处理失败", exc_info=True)

        timer = threading.Timer(PERMISSION_TIMEOUT_SECONDS, _timeout_cancel)
        timer.daemon = True
        with self._perm_lock:
            self._perm_timers[request.request_id] = timer
        timer.start()

    def _dismiss_permission_prompt(self):
        """broker 队列清空：通知前端收起弹窗。"""
        self.emit_event("permission_dismiss", {})

    def _cancel_perm_timer(self, request_id: str):
        with self._perm_lock:
            timer = self._perm_timers.pop(request_id, None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def respond_permission(self, request_id: str, allow: bool, always: bool = False) -> Dict[str, Any]:
        try:
            # 选项索引与 PermissionBroker 默认选项一致：0=同意 1=拒绝 2=始终允许
            if not allow:
                choice = 1
            elif always:
                choice = 2
            else:
                choice = 0
            resolved = permission_broker.resolve(str(request_id), choice)
            self._cancel_perm_timer(str(request_id))
            if not resolved:
                return {"ok": False, "msg": "该权限请求已失效或已被处理"}
            return {"ok": True}
        except Exception as e:
            logger.exception("respond_permission 失败")
            return {"ok": False, "msg": str(e)}

    def respond_permission_choice(self, request_id: str, choice_index: int, custom_text: str = "") -> Dict[str, Any]:
        """按选项索引回答权限请求（ask_user 支持自由文本回答）。"""
        try:
            custom = str(custom_text or "").strip()
            resolved = permission_broker.resolve(
                str(request_id),
                int(choice_index),
                custom_answer=custom or None,
            )
            self._cancel_perm_timer(str(request_id))
            if not resolved:
                return {"ok": False, "msg": "该权限请求已失效或已被处理"}
            return {"ok": True}
        except Exception as e:
            logger.exception("respond_permission_choice 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 对话流
    # ------------------------------------------------------------------
    def send_user_message(self, text: str, session_name: Optional[str] = None, attachments: Optional[List[str]] = None, workdir: Optional[str] = None) -> Dict[str, Any]:
        try:
            if self._shutting_down:
                return {"ok": False, "msg": "应用正在退出"}
            clean_text = str(text or "").strip()
            attach_list = [str(p) for p in (attachments or []) if str(p).strip()]
            if not clean_text and not attach_list:
                return {"ok": False, "msg": "消息内容不能为空"}
            # 附件以 @路径 形式拼回文本，复用 input_parser 的既有解析/挂载逻辑；
            # 展示给用户的气泡保持干净文本（display_text），不含 @路径
            full_text = clean_text
            if attach_list:
                joined = " ".join("@" + p for p in attach_list)
                full_text = f"{clean_text} {joined}" if clean_text else joined
            target_session = session_name or self._current_session
            with self._turn_start_lock:
                if self._is_turn_active(target_session):
                    return {"ok": False, "msg": "该会话已有正在运行的回合"}

                # pending 新会话：先用首条消息前 12 字命名，立即生效，
                # 避免等整个回合跑完才由 _maybe_auto_rename 改名。
                # 注意：rename_session 要求会话已落库，故先 ensure_session 创建。
                if workdir and re.match(r"^session_[a-f0-9]{6}$", target_session or ""):
                    base_title = clean_text[:12].replace("\n", " ").strip()
                    if base_title:
                        try:
                            self._memory.ensure_session(target_session, workdir=str(workdir), source="GUI")
                            new_session = self._rename_session_to_title(target_session, base_title)
                            if new_session != target_session:
                                target_session = new_session
                        except Exception:
                            logger.debug("落库即命名失败", exc_info=True)

                # 用户主动发起新对话：重置该会话的自动汇总标志，
                # 下一批后台任务完成时可以再次触发自动汇总（与 CLI 行为一致）；
                # 同时解除"用户取消"标记，恢复自动汇总与计划续推
                with self._bg_summary_lock:
                    self._bg_summary_triggered.discard(target_session)
                self._user_cancelled_sessions.discard(target_session)

                cancel_event = threading.Event()
                self._cancel_events[target_session] = cancel_event
                turn_kwargs = {"display_text": clean_text} if attach_list else {}
                if workdir:
                    # 懒创建会话：前端 pending 新会话在首条消息时才落库，
                    # workdir 必须显式透传（进程 cwd 可能是别的项目目录）
                    turn_kwargs["pending_workdir"] = str(workdir)
                worker = threading.Thread(
                    target=self._run_turn_worker_serialized,
                    args=(full_text, target_session),
                    kwargs=turn_kwargs,
                    daemon=True,
                    name=f"gui-turn-{target_session}",
                )
                self._active_turns[target_session] = worker
                worker.start()
            return {"ok": True}
        except Exception as e:
            logger.exception("send_user_message 失败")
            return {"ok": False, "msg": str(e)}

    def stop_current_turn(self, session_name: str = "") -> Dict[str, Any]:
        try:
            # 优先取消指定会话，未指定则取消当前会话
            target = session_name or self._current_session
            evt = self._cancel_events.get(target)
            if evt:
                evt.set()
                # 硬中断在飞 LLM 请求：直接关闭底层 HTTP 流，阻塞读立即退出，
                # 而不是协作式取消后仍静默接收数据直到请求超时（最长数百秒）。
                # 必须先 set 事件再 abort：被关闭一侧据此归一为用户取消而非网络错误。
                try:
                    from core.llm import abort_inflight_streams
                    abort_inflight_streams(evt)
                except Exception:
                    pass
            # 用户主动取消：抑制后台任务终态的自动汇总回合与计划续推 nudge，
            # 否则被取消的任务会被“接手完成”/“计划尚未完毕”机制复活
            # （裸实例兜底：测试构造的桥可能未走完整 __init__）
            cancelled_sessions = getattr(self, "_user_cancelled_sessions", None)
            if cancelled_sessions is None:
                cancelled_sessions = self._user_cancelled_sessions = set()
            cancelled_sessions.add(target)
            try:
                with self._bg_summary_lock:
                    self._bg_summary_pending_sessions.discard(target)
            except AttributeError:
                pass
            try:
                from core.plan import suppress_plan_auto_continue
                suppress_plan_auto_continue(target)
            except Exception:
                pass
            # H6：取消点不只有 LLM 事件迭代层——回合线程可能正阻塞在权限确认
            # （最长 300s）或前台子代理的同步等待上，仅 set 事件线程不会退出，
            # 会话会一直持有 _turn_workdir_lock 串行卡住其他会话。与 CLI
            # _cancel_active_turn 同源：同步取消该回合的权限请求（owner 是
            # turn_id 而非会话名）并协作式终止前台子代理。本回合派生的后台
            # 子代理也会被看门狗经 parent_cancel_event 联动取消（tools.py
            # _watch_background）；只有更早回合遗留的后台任务才继续运行。
            turn_id = getattr(self, "_active_turn_ids", {}).get(target, "")
            if turn_id:
                try:
                    permission_broker.cancel_owner(turn_id)
                except Exception:
                    pass
                try:
                    from core.tools import subagent_manager
                    subagent_manager.cancel_foreground(target, turn_id)
                except Exception:
                    logger.debug("停止回合时取消前台子代理失败", exc_info=True)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def _run_turn_worker_serialized(self, user_text: str, session_name: str, *args, **kwargs):
        """多会话并行：线程本地 workdir 上下文（core.workspace_context）已替代
        进程级 os.chdir，回合不再需要 _turn_workdir_lock 串行化。
        保留函数名避免改动所有调用点，实现直接转发到 _run_turn_worker。"""
        return self._run_turn_worker(user_text, session_name, *args, **kwargs)

    def _run_turn_worker(self, user_text: str, session_name: str, internal_kind: str = "", display_text: str = "", pending_workdir: str = ""):
        """后台线程：组装上下文 → 调用模型 → 翻译事件推送给前端。

        internal_kind 非空表示这是系统自动发起的内部回合（如后台任务汇总），
        turn_start 事件会带上 internal / display_text，让前端用系统提示样式
        展示而不是把内部指令原文当成用户气泡。
        """
        api_key = ""
        turn_finished = False
        event_session_name = session_name
        cancelled = False
        turn_id = ""
        # 线程本地 workdir 作用域：绑定后必须在本回合 finally 中退出恢复，
        # 防止主线程直调（测试）或未来线程复用场景下 workdir 泄漏到下一回合
        _workdir_scope = None
        error_state = {
            "toast_messages": set(),
            "terminal_message": None,
        }
        cancel_event = self._cancel_events.get(session_name) or self._get_cancel_event(session_name)

        def finish_once():
            """turn_finish 只允许发出一次（历史实现在 end + finally 各发一次）。"""
            nonlocal turn_finished
            if turn_finished:
                return
            turn_finished = True
            self.emit_event("turn_finish", {"session_name": event_session_name})
            self._emit_token_usage()

        # 多会话并行：不再用全局 _task_status 表示"执行中"——它会被另一会话回合结束时的
        # idle 覆盖，也会让仍在跑的会话状态错乱。各会话的活跃状态由 self._active_turns
        # 字典承载（线程已在 send_user_message 中存入）。_task_status 仅在全部回合结束
        # 时恢复 idle，保持 _GuiContext.task_status 兼容。
        # self._task_status = "executing"
        turn_start_data = {"user_text": user_text, "session_name": session_name}
        if display_text and display_text != user_text:
            turn_start_data["display_text"] = display_text
        if internal_kind:
            turn_start_data["internal"] = internal_kind
            turn_start_data["display_text"] = (
                _BG_COMMAND_SUMMARY_DISPLAY_TEXT
                if internal_kind == "bg_command_summary"
                else _BG_SUMMARY_DISPLAY_TEXT
            )
        self.emit_event("turn_start", turn_start_data)

        try:
            active_model = get_active_model(session_name)
            if not active_model or not active_model.get("litellm_model"):
                # 关键修复：没有激活模型时也必须让前端解锁输入框
                self.emit_event("error", {"message": "当前未激活任何 LLM 模型，请先在模型管理中配置并激活模型。", "session_name": session_name})
                self.emit_event("turn_cancelled", {"session_name": session_name})
                finish_once()
                return

            model_name = active_model["litellm_model"]
            api_key = active_model.get("api_key", "")
            api_base = active_model.get("base_url", "")
            ctx_win = active_model.get("context_window", 128000)
            # 把本回合实际使用的模型写回会话级绑定：首次从全局默认进入该会话时
            # 自动落库，后续切换会话即可恢复该模型，不随全局配置漂移
            self._memory.set_session_model_id(session_name, active_model.get("id"))
            self._sync_active_model_context()

            # 会话归属目录是每一轮 Agent 的权威工作目录。待发送项目会话虽然
            # 已把 workdir 透传到这里，但进程 cwd 仍可能停在启动目录或上一会话；
            # 必须在解析附件、组装运行上下文和执行任何工具前完成绑定。
            turn_workdir = pending_workdir or self._session_workdir(session_name)
            if turn_workdir == "GLOBAL":
                # 自由对话无具体项目目录，线程本地 workdir 兜底进程 cwd
                turn_workdir = _resolve_real_path(os.getcwd())
            else:
                turn_workdir = _resolve_real_path(turn_workdir)
                if not os.path.isdir(turn_workdir):
                    raise FileNotFoundError(f"会话项目文件夹不存在: {turn_workdir}")
            # 线程本地 workdir 绑定：替代进程级 os.chdir，多会话并行时互不干扰。
            # 用 workspace_scope（进入绑定/退出恢复原值）而非裸 set_workdir：
            # 裸绑定在线程不被销毁的场景（主线程直调、未来线程池复用）会永久
            # 残留，导致后续回合路径校验全部误判"越界"
            from core.workspace_context import workspace_scope
            _workdir_scope = workspace_scope(turn_workdir)
            _workdir_scope.__enter__()
            self._memory.set_workdir(turn_workdir)
            # 多会话并行路由锚点：本线程后续所有“每会话”状态（计划管理器、
            # 授权白名单、沙盒放行、子代理调度器）都路由到 session_name，
            # 与并行运行的其他会话回合彻底隔离
            app_context.set_thread_session(session_name)
            from core.plan import set_current_plan_session
            set_current_plan_session(session_name)

            # 确保会话存在，再解析用户输入（支持 @文件 / 图片挂载）并入库
            self._memory.ensure_session(session_name, workdir=turn_workdir, source="GUI")
            try:
                from core.input_parser import parse_user_input
                if internal_kind:
                    # 内部回合（后台汇总/命令总结）：prompt 是系统构造的，不需要
                    # @文件 / 路径挂载；跳过 parse_user_input，避免把日志里的
                    # 盘符路径（如 dist\Y-code-1.2.2.exe）误当成附件挂载
                    parsed_content, attached_msgs = user_text, []
                else:
                    parsed_content, attached_msgs = parse_user_input(user_text)
            except Exception:
                logger.debug("解析用户输入失败，退回纯文本", exc_info=True)
                parsed_content, attached_msgs = user_text, []
            if attached_msgs:
                self.emit_event("user_attachment_notes", {
                    "session_name": session_name,
                    "notes": [str(note) for note in attached_msgs],
                })
            self._memory.add_message(session_name, "user", parsed_content)
            # 提取用户消息中的图片（base64 dataURL）补发给前端渲染气泡缩略图；
            # turn_start 已先行发出保证气泡即时出现，图片随后补挂到该气泡上
            if isinstance(parsed_content, list):
                _images = [
                    str(block.get("image_url", {}).get("url") or "")
                    for block in parsed_content
                    if isinstance(block, dict) and block.get("type") == "image_url"
                ]
                _images = [u for u in _images if u]
                if _images:
                    self.emit_event("user_message_images", {
                        "session_name": session_name,
                        "images": _images,
                    })

            turn_id = f"{session_name}:{uuid.uuid4().hex[:8]}"
            # 记录会话 → turn_id 映射：stop_current_turn 按 turn_id 精确取消
            # 权限等待（broker 的 owner 是 turn_id）与前台子代理
            self._active_turn_ids[session_name] = turn_id
            # 多会话并行：不再强制切换全局 _current_session。_set_current_session 会
            # 同步 tool_cache/undo/plan/app_context 等全局绑定，会话 B 的回合启动会
            # 把这些改成 B，从而影响会话 A 正在进行的后台子代理回调。本回合所需的会话
            # 上下文已通过 session_name 参数显式传递给 prepare_turn_messages /
            # chat_with_model_events（parent_session），不依赖全局 _current_session。
            # 会话归属目录的绑定（os.chdir / memory.set_workdir）已在上面按 session_name 完成。

            # 与 CLI 完全同源的上下文组装（记忆压缩 / 系统提示词 / 运行上下文 / 后台任务汇总）
            from core.agent_service import prepare_turn_messages
            history = prepare_turn_messages(
                self._memory,
                session_name,
                model_name,
                ctx_win,
                permission_mode=self._current_permission_mode,
                cancel_event=cancel_event,
                notify=lambda kind, message: self.emit_event("notice", {"message": _strip_ansi_markup(message)}),
                api_key=api_key,
                api_base=api_base,
                is_gui=True,
            )

            # 压缩（四级水位线阶梯）可能发生在 prepare_turn_messages 内部，会改写
            # 历史使上下文变小。压缩后 latest_prompt 仍是压缩前的上一轮真实用量，
            # 前端据此算占比会停在红圈不更新。这里立即重算窗口占用并推送，
            # 让上下文圆圈在压缩后及时回落。
            self._recalc_token_usage(session_name)
            self._emit_token_usage()

            # 会话级检查点：本轮所有 write_file/edit_file 的旧内容会被自动记录
            from core.memory import begin_turn_checkpoint, end_turn_checkpoint
            begin_turn_checkpoint(
                session_name=session_name,
                turn_id=turn_id,
                last_msg_id=len(history),
                description=str(user_text)[:80],
            )

            # 子代理进度轮询：让前端"运行中"的子代理卡片能实时展开查看进度
            poller_stop = threading.Event()
            poller = threading.Thread(
                target=self._subagent_progress_poller,
                args=(session_name, poller_stop),
                daemon=True,
                name="gui-subagent-poller",
            )
            poller.start()
            try:
                cancelled = self._consume_turn_events(
                    history,
                    model_name,
                    api_key,
                    api_base,
                    session_name,
                    turn_id,
                    error_state=error_state,
                )
            finally:
                poller_stop.set()
                try:
                    end_turn_checkpoint()
                except Exception:
                    logger.debug("提交检查点失败", exc_info=True)

            if cancelled:
                self.emit_event("turn_cancelled", {"session_name": session_name})
            else:
                event_session_name = self._maybe_auto_rename(session_name) or session_name
        except Exception as e:
            logger.exception("GUI 对话回合失败")
            self._emit_owned_error(str(e), api_key, error_state, terminal=True, session_name=session_name)
        finally:
            # 先解绑本回合的会话路由与线程本地 workdir（workspace_scope 退出语义），
            # 再走其余清理，保证任何提前 return / 异常路径都不会把本回合的
            # 会话归属与工作目录泄漏到回合之外
            try:
                from core.plan import set_current_plan_session as _unbind_plan_route

                _unbind_plan_route(None)
            except Exception:
                pass
            app_context.set_thread_session(None)
            if _workdir_scope is not None:
                _workdir_scope.__exit__(None, None, None)
            # 身份校验清理 cancel 事件：finally 竞态窗口内（本线程还没走完
            # finally，但 _active_turns 已 pop）新回合可能已建立同名事件，
            # 仅当字典里仍是本回合自己的事件对象时才删除，防误删新回合事件
            if self._cancel_events.get(session_name) is cancel_event:
                self._cancel_events.pop(session_name, None)
            # 从活跃回合字典中移除本会话
            self._active_turns.pop(session_name, None)
            self._active_turn_ids.pop(session_name, None)
            # 顺带清理 delta 缓冲（加锁防止与 _flush_deltas 的遍历竞态）
            with self._delta_lock:
                self._delta_buffers.pop(session_name, None)
                self._delta_kinds.pop(session_name, None)
            # 仅当没有其他活跃回合时才恢复 idle，避免会话 A 结束时把仍在跑的会话 B
            # 的全局状态错置成 idle
            if not any(t.is_alive() for t in self._active_turns.values()):
                self._task_status = "idle"
            # 权限请求的 owner 是 turn_id（不是会话名），回合结束必须按 turn_id
            # 清理残留等待；历史实现传会话名，永远不命中，等于没清理
            if turn_id:
                try:
                    permission_broker.cancel_owner(turn_id)
                except Exception:
                    pass
            finish_once()
            # 回合运行期间若有后台子代理进入终态（当时不能并发起新回合），
            # 此时按会话取出自己的 pending 补触发自动汇总检查（set 分桶，
            # 多会话并行互不覆盖）
            with self._bg_summary_lock:
                pending = session_name in self._bg_summary_pending_sessions
                self._bg_summary_pending_sessions.discard(session_name)
            if pending and not self._shutting_down:
                threading.Thread(
                    target=self._deferred_bg_summary_check,
                    args=(session_name,),
                    daemon=True,
                    name="gui-bg-summary-recheck",
                ).start()

    def _on_background_task_finished(self, task_id: str, command: str, returncode: int,
                                     session_id: str = "", log_tail: str = "") -> None:
        """后台命令任务完成通知（监听线程触发）：推送通知并触发 LLM 简短总结。

        与 _on_background_subagent_terminal 的区别：后者是子代理后台任务，
        本方法是 execute_command 的 run_in_background 后台命令（如打包）。
        """
        if self._shutting_down:
            return
        try:
            from core.command_policy import redact_sensitive_text
            self.emit_event("background_task_finished", {
                "task_id": task_id,
                "command": redact_sensitive_text(command or ""),
                "returncode": int(returncode or 0),
            })
        except Exception:
            logger.debug("推送后台命令任务完成通知失败", exc_info=True)

        # 有会话归属时，自动发起一轮简短总结回合（LLM 总结命令结果）
        if session_id:
            threading.Thread(
                target=self._start_bg_command_summary,
                args=(session_id, command, returncode, log_tail),
                daemon=True,
                name="gui-bg-command-summary",
            ).start()

    def _start_bg_command_summary(self, session: str, command: str,
                                  returncode: int, log_tail: str) -> None:
        """后台命令任务完成后，自动发起一轮 LLM 简短总结回合。"""
        try:
            time.sleep(0.5)
            if self._shutting_down or not session:
                return
            if session in getattr(self, "_user_cancelled_sessions", ()):
                return
            # 该会话正在运行回合时不并发起新回合，直接放弃（避免打断用户当前对话）
            with self._turn_start_lock:
                if self._is_turn_active(session):
                    return
            from core.command_policy import redact_sensitive_text
            cmd = redact_sensitive_text(command or "")
            tail = (log_tail or "").strip()[-1500:]
            prompt = (
                "有一个后台命令任务执行完毕，请用一句话简短总结它的结果"
                "（成功/失败 + 关键信息，不要复述命令本身）。\n"
                f"命令: {cmd}\n"
                f"退出码: {int(returncode or 0)}（0=成功，非0=失败）\n"
                f"日志尾部:\n{tail or '(无输出)'}\n"
            )
            cancel_event = threading.Event()
            self._cancel_events[session] = cancel_event
            worker = threading.Thread(
                target=self._run_turn_worker_serialized,
                args=(prompt, session, "bg_command_summary"),
                daemon=True,
                name=f"gui-bg-command-summary-{session}",
            )
            self._active_turns[session] = worker
            worker.start()
        except Exception:
            logger.debug("后台命令任务总结回合触发失败", exc_info=True)

    def _on_background_subagent_terminal(self, task_id: str, status: str, description: str, summary: str) -> None:
        """后台子代理终态实时通知（worker 线程触发，与 CLI 同源的 GUI 版本）。

        职责一：立即向前端推送一条终态通知（前端以 toast 呈现）。
        职责二：延迟检查本会话是否所有后台任务都已终态，是则自动发起一轮
        汇总 turn，把结果主动呈现给用户——不用等用户手动发下一条消息。
        """
        if self._shutting_down:
            return
        from core.tools import subagent_manager
        session = next(
            (
                snap.parent_session
                for snap in subagent_manager.snapshot()
                if snap.task_id == task_id
            ),
            "",
        )
        if not session:
            logger.warning("后台子代理终态缺少会话归属: %s", task_id)
            return
        try:
            self.emit_event("background_subagent_terminal", {
                "task_id": task_id,
                "session_name": session,
                "status": status,
                "description": (description or "")[:60],
                "summary": (summary or "")[:150],
            })
        except Exception:
            logger.debug("推送后台子代理终态通知失败", exc_info=True)

        threading.Thread(
            target=self._deferred_bg_summary_check,
            args=(session,),
            daemon=True,
            name="gui-bg-summary-check",
        ).start()

    def _deferred_bg_summary_check(self, session: str) -> None:
        """延迟 0.5s 后检查是否触发自动汇总（给并发终态回调留出状态更新时间）。"""
        try:
            time.sleep(0.5)
            if self._shutting_down:
                return
            self._maybe_start_bg_summary_turn(session)
        except Exception:
            logger.debug("后台任务自动汇总触发失败", exc_info=True)

    def _maybe_start_bg_summary_turn(self, session: str) -> None:
        """条件满足时自动发起汇总 turn。

        条件：会话未切走、无仍在运行的后台任务、本批任务未触发过汇总、
        当前没有正在运行的回合（有则挂起，回合结束后由 _run_turn_worker 补触发）。
        """
        from core.tools import subagent_manager

        if self._shutting_down or not session:
            return
        # （getattr 兜底：测试构造的裸桥可能未走完整 __init__）
        if session in getattr(self, "_user_cancelled_sessions", ()):
            # 本批后台任务是被用户取消的：不自动起汇总回合（用户下一条消息时
            # 仍会在 prepare 阶段看到终态结果，但不再附“接手完成”指令）
            return
        if subagent_manager.has_running_background_tasks(session):
            return

        with self._turn_start_lock:
            if self._is_turn_active(session):
                # 该会话回合运行中不能并发起新回合；挂起，回合结束时补检查
                with self._bg_summary_lock:
                    self._bg_summary_pending_sessions.add(session)
                return
            with self._bg_summary_lock:
                if session in self._bg_summary_triggered:
                    return
                self._bg_summary_triggered.add(session)
            cancel_event = threading.Event()
            self._cancel_events[session] = cancel_event
            worker = threading.Thread(
                target=self._run_turn_worker_serialized,
                args=(_BG_SUMMARY_PROMPT, session, "bg_summary"),
                daemon=True,
                name=f"gui-bg-summary-{session}",
            )
            self._active_turns[session] = worker
            worker.start()

    def _subagent_progress_poller(self, session_name: str, stop: threading.Event) -> None:
        """回合期间轮询子代理运行状态并推送 subagent_progress 事件。

        run_subagent 在工具执行内部同步等待所有子代理完成，期间事件流不会产出
        任何内容，前端卡片只能干等；此线程独立采样 SubagentManager 快照，
        让"运行中"的卡片可以展开查看每个子代理的阶段与耗时。
        """
        from core.tools import subagent_manager

        had_tasks = False
        while True:
            stopped = stop.wait(0.8)
            try:
                now = time.time()
                tasks = []
                for snap in subagent_manager.snapshot():
                    if snap.parent_session != session_name:
                        continue
                    elapsed = 0
                    if snap.started_at:
                        elapsed = int((snap.finished_at or now) - snap.started_at)
                    last_detail = snap.stages[-1].detail if snap.stages else ""
                    tasks.append({
                        "id": snap.task_id,
                        "description": snap.description,
                        "type": snap.subagent_type,
                        "status": snap.status,
                        "stage": snap.stage,
                        "stage_detail": last_detail,
                        "elapsed": elapsed,
                        "background": bool(snap.run_in_background),
                    })
                # 没有任务且上一轮也没有任务时不推事件，避免空轮询刷屏
                if tasks or had_tasks:
                    had_tasks = bool(tasks)
                    self.emit_event("subagent_progress", {"tasks": tasks, "session_name": session_name})
            except Exception:
                logger.debug("推送子代理进度失败", exc_info=True)
            if stopped:
                break

    def _emit_owned_error(self, message, api_key, error_state, *, terminal, session_name=""):
        """Emit one terminal state and one toast per sanitized error message."""
        sanitized = _sanitize_for_ui(str(message), api_key)
        session_payload = {"session_name": session_name} if session_name else {}
        if terminal and error_state["terminal_message"] is None:
            error_state["terminal_message"] = sanitized
            self.emit_event("turn_error", {"message": sanitized, **session_payload})

        toast_messages = error_state["toast_messages"]
        if sanitized in toast_messages:
            return
        toast_messages.add(sanitized)
        self.emit_event("error", {"message": sanitized, **session_payload})

    def _consume_turn_events(
        self,
        history,
        model_name,
        api_key,
        api_base,
        session_name,
        turn_id,
        *,
        error_state=None,
    ) -> bool:
        """迭代模型事件并翻译为前端事件；返回本轮是否被取消。"""
        from core.llm import chat_with_model_events

        cancelled = False
        tool_labels: Dict[str, str] = {}
        started_tool_ids: set[str] = set()
        if error_state is None:
            error_state = {
                "toast_messages": set(),
                "terminal_message": None,
            }

        for event in chat_with_model_events(
            model_name,
            api_key,
            history,
            api_base,
            persist=True,
            turn_id=turn_id,
            cancel_event=self._cancel_events.get(session_name) or self._get_cancel_event(session_name),
            parent_session=session_name,
            parent_turn_id=turn_id,
            permission_owner=turn_id,
            permission_source="主代理",
        ):
            if (self._cancel_events.get(session_name) or self._get_cancel_event(session_name)).is_set():
                cancelled = True
                break

            kind = getattr(event, "kind", None) or (event.get("type") if isinstance(event, dict) else None)
            if not kind:
                continue
            payload = getattr(event, "payload", None)
            if payload is None and isinstance(event, dict):
                payload = event.get("data")
            data = _to_plain_dict(payload)

            if kind == "round_started":
                # 轮次边界透传：前端据此把每个 LLM 轮次拆成独立消息块（分段思考）
                # emit_event 内部会先 flush 累积的 delta，顺序天然正确
                self.emit_event("round_started", {"round": data.get("round"), "session_name": session_name})
            elif kind == "thinking_delta":
                self._emit_delta("thinking_delta", str(data.get("text", "")), session_name=session_name)
            elif kind in ("assistant_delta", "token_delta", "answer_delta"):
                self._emit_delta("token_delta", str(data.get("text", "")), session_name=session_name)
            elif kind in ("tool_requested", "tool_started"):
                tool_id = str(data.get("id") or "")
                name = str(data.get("name") or "tool")
                label = str(data.get("summary") or data.get("preview") or name)
                tool_labels[tool_id] = label
                if tool_id not in started_tool_ids:
                    started_tool_ids.add(tool_id)
                    self.emit_event("tool_start", {
                        "id": tool_id,
                        "name": name,
                        "label": label,
                        "display": dict(data.get("display") or {}),
                        "started_at_ms": int(time.time() * 1000),
                        "session_name": session_name,
                    })
            elif kind == "tool_finished":
                tool_id = str(data.get("id") or "")
                name = str(data.get("name") or "tool")
                label = tool_labels.get(tool_id) or str(data.get("summary") or name)
                self.emit_event("tool_finish", {
                    "id": tool_id,
                    "name": name,
                    "label": label,
                    "ok": bool(data.get("ok", True)),
                    "summary": str(data.get("summary") or ""),
                    "result": str(data.get("result") or ""),
                    "error": str(data.get("error") or ""),
                    "duration_ms": data.get("duration_ms"),
                    "display": dict(data.get("display") or {}),
                    "session_name": session_name,
                })
            elif kind == "diff_ready":
                # 优先取纯文本 unified diff；缺失时才退回终端版本（含 ANSI 颜色）
                self.emit_event("diff_ready", {
                    "tool_call_id": str(data.get("tool_call_id") or ""),
                    "file_path": str(data.get("file_path") or ""),
                    "diff_text": str(data.get("diff_text") or data.get("text") or ""),
                    "session_name": session_name,
                })
            elif kind in ("plan_updated", "plan_change"):
                self.emit_event("plan_updated", {
                    "title": str(data.get("title") or "当前任务计划"),
                    "items": data.get("items") or [],
                    "session_name": session_name,
                })
            elif kind == "turn_cancelled":
                cancelled = True
            elif kind == "turn_error":
                self._emit_owned_error(
                    data.get("message") or "模型生成错误",
                    api_key,
                    error_state,
                    terminal=True,
                    session_name=session_name,
                )
            elif kind == "error":
                self._emit_owned_error(
                    data.get("message") or "模型生成错误",
                    api_key,
                    error_state,
                    terminal=False,
                    session_name=session_name,
                )
            elif kind in ("notice", "status"):
                message = data.get("message")
                if message:
                    self.emit_event("notice", {"message": _strip_ansi_markup(message), "session_name": session_name})

        if (self._cancel_events.get(session_name) or self._get_cancel_event(session_name)).is_set():
            cancelled = True
        return cancelled

    def _session_workdir(self, session_name: str) -> str:
        """查询会话已登记的 workdir；查不到时按当前工作目录处理。"""
        try:
            for s in self._memory.get_all_sessions(workdir=None, include_empty=True):
                if s.get("name") == session_name:
                    return s.get("workdir") or _resolve_real_path(os.getcwd())
        except Exception:
            pass
        return _resolve_real_path(os.getcwd())

    def _derive_title_from_first_user_msg(self, session_name: str) -> Optional[str]:
        """从会话首条用户消息提炼标题（前 12 字），提炼不出则返回 None。"""
        history = self._memory.get_history(session_name)
        user_msgs = [m for m in history if m.get("role") == "user"]
        if not user_msgs:
            return None
        raw = user_msgs[0].get("content", "")
        if isinstance(raw, list):
            raw = " ".join(
                str(item.get("text", "")) for item in raw if isinstance(item, dict)
            )
        first_text = str(raw).strip().replace("\n", " ")
        return first_text[:12] if first_text else None

    def _rename_session_to_title(self, session_name: str, base_title: str) -> str:
        """把会话重命名为 base_title（撞名自动加序号），成功推送 session_renamed。"""
        existing = {s.get("name") for s in self._memory.get_all_sessions(workdir=None)}
        new_title = base_title
        suffix = 2
        while new_title in existing:
            new_title = f"{base_title}({suffix})"
            suffix += 1
        success, final_name = self._memory.rename_session(session_name, new_title)
        if success:
            if self._current_session == session_name:
                self._set_current_session(final_name)
            self.emit_event("session_renamed", {
                "old": session_name,
                "new": final_name,
                # 兼容旧字段名
                "old_name": session_name,
                "new_name": final_name,
                "sessions": self.get_sessions(),
            })
            return final_name
        return session_name

    def _maybe_auto_rename(self, session_name: str) -> str:
        """首轮对话完成后，把默认的 session_xxxxxx 命名换成提炼出的标题。"""
        if not re.match(r"^session_[a-f0-9]{6}$", session_name or ""):
            return session_name
        try:
            base_title = self._derive_title_from_first_user_msg(session_name)
            if not base_title:
                return session_name
            return self._rename_session_to_title(session_name, base_title)
        except Exception:
            logger.debug("自动重命名会话失败", exc_info=True)
        return session_name

    # ------------------------------------------------------------------
    # Token 用量
    # ------------------------------------------------------------------
    def get_token_usage_statistics(self, days: int = 30) -> Dict[str, Any]:
        """按 CLI /usage 口径返回 GUI 的每日与模型 Token 统计。"""
        if type(days) is not int or days not in (1, 7, 30):
            return {"ok": False, "msg": "统计范围仅支持 1、7、30 天"}
        range_days = days

        try:
            from datetime import date, timedelta

            end_date = date.today()
            start_date = end_date - timedelta(days=range_days - 1)
            rows = self._memory.query_token_usage_by_day_and_model(
                start_date.isoformat(), end_date.isoformat()
            )
            metric_names = (
                "prompt", "completion", "cached", "cache_prompt",
                "total", "requests",
            )

            def empty_metrics() -> Dict[str, int]:
                return {name: 0 for name in metric_names}

            def add_metrics(target: Dict[str, Any], source: Dict[str, Any]) -> None:
                for name in metric_names:
                    target[name] += int(source.get(name, 0) or 0)

            def with_rate(metrics: Dict[str, Any]) -> Dict[str, Any]:
                result = dict(metrics)
                cache_prompt = int(result.get("cache_prompt", 0) or 0)
                result["cache_hit_rate"] = (
                    round(int(result.get("cached", 0) or 0) / cache_prompt * 100, 1)
                    if cache_prompt > 0 else None
                )
                return result

            summary = empty_metrics()
            model_totals: Dict[str, Dict[str, Any]] = {}
            day_lookup: Dict[str, Dict[str, Any]] = {}
            for offset in range(range_days):
                day = (start_date + timedelta(days=offset)).isoformat()
                day_lookup[day] = {
                    "date": day,
                    **empty_metrics(),
                    "models": [],
                }

            for row in rows:
                provider = str(row.get("provider") or "")
                model = str(row.get("model") or "")
                key = f"{provider}/{model}" if provider else model
                model_item = model_totals.setdefault(key, {
                    "key": key,
                    "provider": provider,
                    "model": model,
                    **empty_metrics(),
                })
                add_metrics(model_item, row)
                add_metrics(summary, row)

                day_item = day_lookup[str(row.get("date") or "")]
                add_metrics(day_item, row)
                day_item["models"].append(with_rate({
                    "key": key,
                    "provider": provider,
                    "model": model,
                    **{name: int(row.get(name, 0) or 0) for name in metric_names},
                }))

            models = [with_rate(item) for item in model_totals.values()]
            models.sort(key=lambda item: (-item["total"], item["key"]))
            day_items = []
            for day_item in day_lookup.values():
                day_item["models"].sort(
                    key=lambda item: (-item["total"], item["key"])
                )
                day_items.append(with_rate(day_item))

            return {
                "ok": True,
                "range": {
                    "days": range_days,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
                "summary": with_rate(summary),
                "models": models,
                "days": day_items,
            }
        except Exception as exc:
            logger.exception("get_token_usage_statistics 失败")
            return {"ok": False, "msg": str(exc)}

    def get_token_usage(self) -> Dict[str, Any]:
        try:
            from core.llm import get_token_usage_snapshot
            active = get_active_model() or {}
            snapshot = get_token_usage_snapshot(
                self._current_session,
                active.get("litellm_model", ""),
                active.get("base_url", ""),
                "main",
            )
            window_usage = snapshot["window"]
            process_usage = snapshot["process"]
            cache_usage = snapshot["cache"]
            latest_prompt = int(window_usage.get("latest_prompt", 0) or 0)
            latest_total = int(window_usage.get("latest_total", 0) or 0)
            cumulative = int(process_usage.get("cumulative_total", 0) or 0)
            window = int(active.get("context_window", 128000) or 128000)
            latest_rate = cache_usage["latest"].get("rate")
            hot_rate = cache_usage["hot"].get("rate")
            session_rate = cache_usage["session_all"].get("rate")

            def rounded(value):
                return None if value is None else round(float(value), 1)

            return {
                # 首轮请求返回前 latest_prompt 还是 0，用本地估算的 latest_total 兜底
                "used": latest_prompt or latest_total,
                "window": window,
                "cached_pct": rounded(session_rate) or 0.0,
                "cumulative": cumulative,
                "cache_latest_pct": rounded(latest_rate),
                "cache_hot_pct": rounded(hot_rate),
                "cache_session_pct": rounded(session_rate),
                "cache_available": bool(cache_usage.get("available", False)),
                "cache_hot_turns": int(cache_usage.get("hot_turns", 0) or 0),
                "cache_invalidation_reason": str(
                    cache_usage.get("invalidation_reason", "") or ""
                ),
                "prompt_tokens": int(
                    cache_usage["session_all"].get("prompt_tokens", 0) or 0
                ),
                "cached_tokens": int(
                    cache_usage["session_all"].get("cached_tokens", 0) or 0
                ),
                # CLI 状态栏口径（main.py get_status）：输入/输出/命中取计费累计
                "total_prompt": int(process_usage.get("cumulative_prompt", 0) or 0),
                "total_completion": int(
                    process_usage.get("cumulative_completion", 0) or 0
                ),
                "total_cached": int(process_usage.get("cumulative_cached", 0) or 0),
            }
        except Exception:
            logger.debug("get_token_usage 失败", exc_info=True)
            return {
                "used": 0, "window": 0, "cached_pct": 0.0, "cumulative": 0,
                "cache_latest_pct": None, "cache_hot_pct": None,
                "cache_session_pct": None, "cache_available": False,
                "cache_hot_turns": 0, "cache_invalidation_reason": "",
                "prompt_tokens": 0, "cached_tokens": 0,
            }

    def _emit_token_usage(self):
        try:
            self.emit_event("token_usage", self.get_token_usage())
        except Exception:
            logger.debug("推送 token_usage 失败", exc_info=True)

    def _recalc_token_usage(self, session_name: str):
        """切会话 / 清空历史后重算窗口占用（优先真实用量，无记录才估算兜底）。"""
        try:
            from core.agent_service import get_session_token_baseline
            from core.llm import session_token_usage, token_usage_lock
            active = get_active_model() or {}
            history = self._memory.get_history(session_name)
            total = get_session_token_baseline(
                self._memory, session_name, active.get("litellm_model", ""), history=history
            )
            with token_usage_lock:
                session_token_usage["latest_total"] = total
                session_token_usage["latest_prompt"] = 0
        except Exception:
            logger.debug("重算 token 用量失败", exc_info=True)

    def _ensure_fits_active_window(self, session_name: str):
        """切换模型后兜底：当前会话占用超过新激活模型窗口阈值时立即压缩历史。

        从大窗口（如 100 万）切到小窗口（如 25.6 万）模型时，若占用超出新窗口
        的压缩阈值（0.95），直接调用 prepare_turn_messages 触发同一套四级水位线
        压缩（snip/elide/摘要/滑窗淘汰），让小窗口模型可用而非发消息报错。
        压缩后重算窗口占用并推送，前端红圈随之回落。无需压缩时静默跳过。
        """
        try:
            active = get_active_model() or {}
            window = int(active.get("context_window") or 0)
            if window <= 0:
                return
            from core.agent_service import SUMMARIZE_RATIO, get_session_token_baseline
            history = self._memory.get_history(session_name)
            total = get_session_token_baseline(
                self._memory, session_name, active.get("litellm_model", ""), history=history
            )
            if total <= window * SUMMARIZE_RATIO:
                return
            from core.agent_service import prepare_turn_messages
            logger.info(
                "切换模型后会话占用 %d 超新窗口 %d 阈值，触发自动压缩", total, window
            )
            prepare_turn_messages(
                self._memory,
                session_name,
                active.get("litellm_model", ""),
                window,
                permission_mode=self._current_permission_mode,
                notify=lambda kind, message: self.emit_event(
                    "notice", {"message": _strip_ansi_markup(message)}
                ),
                api_key=active.get("api_key", ""),
                api_base=active.get("base_url", ""),
                is_gui=True,
            )
            self._recalc_token_usage(session_name)
            self._emit_token_usage()
        except Exception:
            # 压缩失败不阻断模型切换本身，发消息时 prepare 还会再兜底
            logger.exception("切换模型后自动压缩失败")

    # ------------------------------------------------------------------
    # 技能
    # ------------------------------------------------------------------
    def _build_skill_list(self) -> List[Dict[str, Any]]:
        try:
            active_ids = set(get_active_skill_ids())
            skills = []
            for s in (scan_all_skills() or []):
                skill_id = str(s.get("id", ""))
                skills.append({
                    "id": skill_id,
                    "short_id": os.path.basename(os.path.dirname(skill_id)) if skill_id else "",
                    "name": str(s.get("name", "")),
                    "description": str(s.get("description", "")),
                    "enabled": skill_id in active_ids,
                })
            return skills
        except Exception:
            logger.exception("扫描技能失败")
            return []

    def toggle_skill(self, skill_id: str, enabled: bool) -> Dict[str, Any]:
        try:
            active_ids = list(get_active_skill_ids())
            if enabled:
                if skill_id not in active_ids:
                    active_ids.append(skill_id)
            else:
                active_ids = [i for i in active_ids if i != skill_id]
            save_active_skill_ids(active_ids)
            return {"ok": True, "skills": self._build_skill_list()}
        except Exception as e:
            logger.exception("toggle_skill 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 插件（与 CLI 共用 core.plugin_loader 的扫描与开关持久化，天然同步）
    # ------------------------------------------------------------------
    def get_plugins(self) -> List[Dict[str, Any]]:
        try:
            from core.plugin_loader import get_plugin_infos, refresh_plugins
            # 增量扫盘：运行期间新安装/删除的插件无需重启即可反映到列表
            # （此前只在进程启动时扫一次，Agent 自己装的插件必须重启才显示）
            refresh_plugins()
            result = []
            for info in get_plugin_infos():
                status = str(info.get("status") or "")
                # 当前进程内实际生效状态：loaded/pending_disable 视为启用中
                enabled = status in ("loaded", "pending_disable")
                result.append({
                    "name": str(info.get("name") or ""),
                    "version": str(info.get("version") or ""),
                    "description": str(info.get("description") or ""),
                    "source": str(info.get("source") or "builtin"),
                    "status": status,
                    "enabled": enabled,
                    "tools": list(info.get("tools") or []),
                    "error": str(info.get("error") or ""),
                })
            return result
        except Exception:
            logger.exception("get_plugins 失败")
            return []

    def toggle_plugin(self, name: str, enabled: bool) -> Dict[str, Any]:
        try:
            from core.plugin_loader import toggle_plugin as _toggle
            ok, msg = _toggle(name, enabled)
            return {"ok": ok, "msg": msg, "plugins": self.get_plugins()}
        except Exception as e:
            logger.exception("toggle_plugin 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # MCP
    # ------------------------------------------------------------------
    def _mcp_tool_counts(self) -> Dict[str, int]:
        """按 "{server}__{tool}" 前缀统计每个 server 已就绪的工具数。"""
        counts: Dict[str, int] = {}
        if self._mcp is None:
            return counts
        try:
            for schema in (self._mcp.get_schemas() or []):
                full_name = str((schema.get("function") or {}).get("name", ""))
                if "__" not in full_name:
                    continue
                server = full_name.split("__", 1)[0]
                counts[server] = counts.get(server, 0) + 1
        except Exception:
            logger.debug("统计 MCP 工具数失败", exc_info=True)
        return counts

    def _build_mcp_list(self, enabled_override: Optional[Dict[str, bool]] = None) -> List[Dict[str, Any]]:
        if self._mcp is None:
            return []
        try:
            raw = json.loads(self._mcp.list_servers() or "[]")
            if not isinstance(raw, list):
                return []
        except Exception:
            logger.debug("读取 MCP 服务列表失败", exc_info=True)
            return []
        counts = self._mcp_tool_counts()
        result = []
        for item in raw:
            name = str(item.get("name", ""))
            enabled = bool(item.get("enabled", True))
            if enabled_override and name in enabled_override:
                enabled = enabled_override[name]
            result.append({
                "name": name,
                "enabled": enabled,
                "connected": bool(item.get("connected", False)),
                "tools_count": int(counts.get(name, 0)),
            })
        return result

    def get_mcp_servers(self) -> List[Dict[str, Any]]:
        try:
            return self._build_mcp_list()
        except Exception:
            logger.exception("get_mcp_servers 失败")
            return []

    def toggle_mcp_server(self, name: str, enabled: bool) -> Dict[str, Any]:
        """切换 MCP 服务开关：配置写入后的重连放到后台线程，避免阻塞界面。"""
        if self._mcp is None:
            return {"ok": False, "msg": "MCP 管理器不可用（依赖未安装）"}

        def _job():
            try:
                ok, msg = self._mcp.set_server_enabled(name, bool(enabled))
            except Exception as e:
                ok, msg = False, str(e)
            self.emit_event("mcp_status", {"mcps": self._build_mcp_list(), "ok": ok, "msg": msg})

        try:
            threading.Thread(target=_job, daemon=True, name="gui-mcp-toggle").start()
            action = "启用" if enabled else "禁用"
            return {
                "ok": True,
                "msg": f"正在{action} {name}，重连完成后会自动刷新状态",
                "mcps": self._build_mcp_list(enabled_override={name: bool(enabled)}),
            }
        except Exception as e:
            logger.exception("toggle_mcp_server 失败")
            return {"ok": False, "msg": str(e)}

    def add_mcp_server(
        self,
        name: str,
        command: str,
        args: Any,
        env: Optional[Dict[str, str]] = None,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        """添加自定义 MCP 服务：先做基础字段校验，写盘与重连放后台线程避免阻塞界面。

        命令白名单、危险 env 键等安全校验由 mcp_manager.add_server 强制执行，
        这里只做 GUI 表单层面的类型/空值校验。"""
        if self._mcp is None:
            return {"ok": False, "msg": "MCP 管理器不可用（依赖未安装）"}
        name = str(name or "").strip()
        command = str(command or "").strip()
        if not name:
            return {"ok": False, "msg": "服务名不能为空"}
        if not command:
            return {"ok": False, "msg": "启动命令不能为空"}
        if not isinstance(args, list):
            return {"ok": False, "msg": "args 必须是数组"}
        if env is not None and not isinstance(env, dict):
            return {"ok": False, "msg": "env 必须是对象"}
        clean_args = [str(a) for a in args]
        clean_env = {str(k): str(v) for k, v in (env or {}).items()} or None

        def _job():
            try:
                ok, msg = self._mcp.add_server(
                    name, command, clean_args, env=clean_env, enabled=bool(enabled)
                )
            except Exception as e:
                ok, msg = False, str(e)
            time.sleep(1.5)
            self.emit_event("mcp_status", {"mcps": self._build_mcp_list(), "ok": ok, "msg": msg})

        try:
            threading.Thread(target=_job, daemon=True, name="gui-mcp-add").start()
            return {
                "ok": True,
                "msg": f"正在添加 {name}，连接完成后会自动刷新状态",
                "mcps": self._build_mcp_list(),
            }
        except Exception as e:
            logger.exception("add_mcp_server 失败")
            return {"ok": False, "msg": str(e)}

    def reconnect_mcp(self) -> Dict[str, Any]:
        if self._mcp is None:
            return {"ok": False, "msg": "MCP 管理器不可用（依赖未安装）"}

        def _job():
            try:
                ok, msg = self._mcp.reconnect()
            except Exception as e:
                ok, msg = False, str(e)
            # 连接是惰性建立的，稍等片刻再取工具数会更准确
            time.sleep(1.5)
            self.emit_event("mcp_status", {"mcps": self._build_mcp_list(), "ok": ok, "msg": msg})

        try:
            threading.Thread(target=_job, daemon=True, name="gui-mcp-reconnect").start()
            return {"ok": True, "msg": "已发起重连，完成后自动刷新状态", "mcps": self._build_mcp_list()}
        except Exception as e:
            logger.exception("reconnect_mcp 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 导出 / 撤销 / 文件
    # ------------------------------------------------------------------
    def export_session(self, fmt: str = "md") -> Dict[str, Any]:
        try:
            import datetime
            from pathlib import Path
            from core.share import (
                export_session_html,
                export_session_json,
                export_session_markdown,
            )

            fmt = str(fmt or "md").lower()
            exporters = {
                "md": (export_session_markdown, "md"),
                "markdown": (export_session_markdown, "md"),
                "html": (export_session_html, "html"),
                "json": (export_session_json, "json"),
            }
            if fmt not in exporters:
                return {"ok": False, "msg": f"未知格式: {fmt}（支持 md / html / json）"}
            exporter, ext = exporters[fmt]

            session_name = self._current_session
            export_dir = Path.home() / ".ycode" / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_name)
            output_path = export_dir / f"{safe_name}_{timestamp}.{ext}"

            result = exporter(session_name, manager=self._memory, output_path=str(output_path))
            if isinstance(result, str) and result.startswith("🚨"):
                return {"ok": False, "msg": result}
            return {"ok": True, "path": str(output_path), "msg": str(result)}
        except Exception as e:
            logger.exception("export_session 失败")
            return {"ok": False, "msg": str(e)}

    def undo_last_edit(self) -> Dict[str, Any]:
        try:
            from core.agent_service import undo_last_edit as _undo
            # 前端已经做过二次确认，这里按已确认处理
            ok, message = _undo(self._memory, self._current_session, force=True)
            if ok:
                self._recalc_token_usage(self._current_session)
                self._emit_token_usage()
            return {"ok": bool(ok), "msg": message}
        except Exception as e:
            logger.exception("undo_last_edit 失败")
            return {"ok": False, "msg": str(e)}

    def read_file_preview(self, path: str) -> Dict[str, Any]:
        try:
            real_path = _resolve_real_path(path)
            workspace = self._session_workdir(self._current_session)
            if workspace == "GLOBAL" or not _path_is_within(real_path, workspace):
                return {"ok": False, "msg": "文件不在当前工作区内"}
            if not os.path.isfile(real_path):
                return {"ok": False, "msg": "文件不存在或不是普通文件"}
            size = os.path.getsize(real_path)
            truncated = size > FILE_PREVIEW_MAX_BYTES
            with open(real_path, "rb") as f:
                raw = f.read(FILE_PREVIEW_MAX_BYTES)
            if b"\x00" in raw:
                return {"ok": False, "msg": "二进制文件不支持预览"}
            for encoding in ("utf-8", "gbk"):
                try:
                    content = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    content = None
            if content is None:
                return {"ok": False, "msg": "无法识别文件编码（仅支持 UTF-8 / GBK 文本）"}
            return {"ok": True, "content": content, "truncated": truncated, "path": real_path}
        except Exception as e:
            logger.exception("read_file_preview 失败")
            return {"ok": False, "msg": str(e)}

    def open_path(self, path: str) -> Dict[str, Any]:
        try:
            real_path = _resolve_real_path(path)
            workspace = self._session_workdir(self._current_session)
            if workspace == "GLOBAL" or not _path_is_within(real_path, workspace):
                return {"ok": False, "msg": "路径不在当前工作区内"}
            if not os.path.exists(real_path):
                return {"ok": False, "msg": "路径不存在"}
            if os.path.isfile(real_path) and os.path.splitext(real_path)[1].lower() in {
                ".bat", ".cmd", ".com", ".cpl", ".exe", ".hta", ".js", ".jse",
                ".lnk", ".msi", ".msp", ".ps1", ".reg", ".scr", ".url", ".vbs", ".wsf",
            }:
                return {"ok": False, "msg": "安全策略禁止直接启动可执行或快捷方式文件"}
            opener = getattr(os, "startfile", None)
            if opener is not None:
                opener(real_path)
                return {"ok": True}
            # 非 Windows 平台的兜底
            import subprocess
            cmd = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([cmd, real_path])
            return {"ok": True}
        except Exception as e:
            logger.exception("open_path 失败")
            return {"ok": False, "msg": str(e)}

    def open_folder_in_explorer(self, path: str) -> Dict[str, Any]:
        """在系统文件资源管理器中打开指定目录（侧栏分组文件夹图标点击）。

        与 open_path 不同：不要求路径落在当前会话工作区内——侧栏分组的
        folder_path 是用户主动引入的项目目录，点击图标即可打开该目录本身，
        即使它不是当前激活会话所属的工作区。
        """
        try:
            if not path:
                return {"ok": False, "msg": "路径为空"}
            real_path = _resolve_real_path(path)
            if not os.path.isdir(real_path):
                return {"ok": False, "msg": f"目录不存在: {real_path}"}
            opener = getattr(os, "startfile", None)
            if opener is not None:
                opener(real_path)
                return {"ok": True}
            import subprocess
            cmd = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([cmd, real_path])
            return {"ok": True}
        except Exception as e:
            logger.exception("open_folder_in_explorer 失败")
            return {"ok": False, "msg": str(e)}

    def pick_files(self) -> Dict[str, Any]:
        try:
            import webview
            if not self._window:
                return {"ok": False, "msg": "窗口尚未就绪"}
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                directory=os.getcwd(),
            )
            if not result:
                return {"ok": False, "msg": "已取消选择"}
            return {"ok": True, "paths": [str(p) for p in result]}
        except Exception as e:
            logger.exception("pick_files 失败")
            return {"ok": False, "msg": str(e)}

    def save_clipboard_image(self, data_url: str) -> Dict[str, Any]:
        """把前端粘贴的剪贴板图片（dataURL）落盘为临时文件，返回路径供作为附件发送。"""
        try:
            import base64
            import tempfile
            import time

            MIME_EXT = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }
            header, sep, b64 = str(data_url or "").partition(",")
            if not sep or not header.startswith("data:"):
                return {"ok": False, "msg": "无效的图片数据"}
            mime = header[5:].split(";")[0].strip().lower()
            if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                return {"ok": False, "msg": f"不支持的图片类型: {mime or '未知'}"}
            # base64 体积 ≈ 原始 4/3 倍；限制 8MB 原始大小，超出让发送端压缩逻辑处理
            if len(b64) > 8 * 1024 * 1024 * 4 // 3 + 1024:
                return {"ok": False, "msg": "图片超过 8MB，无法粘贴"}
            raw = base64.b64decode(b64)
            paste_dir = os.path.join(tempfile.gettempdir(), "ycode_paste")
            os.makedirs(paste_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            ms = int(time.time() * 1000) % 1000
            # 毫秒时间戳仍会撞名（同一毫秒内连续粘贴会互相覆盖），加随机后缀
            path = os.path.join(
                paste_dir,
                f"paste_{ts}_{ms:03d}_{uuid.uuid4().hex[:6]}{MIME_EXT[mime]}",
            )
            with open(path, "wb") as f:
                f.write(raw)
            return {"ok": True, "path": path}
        except Exception as e:
            logger.exception("save_clipboard_image 失败")
            return {"ok": False, "msg": str(e)}

    def store_dropped_paths(self, paths: List[str]) -> None:
        """pywebview 原生 drop 事件回调存储的完整路径，供前端 drop 时查询。"""
        self._dropped_paths = list(paths or [])

    def get_dropped_paths(self) -> List[str]:
        """取出并清空最近一次的拖拽文件路径（一次性消费）。"""
        paths = getattr(self, '_dropped_paths', []) or []
        self._dropped_paths = []
        return paths

    def report_dropped_files(self, event: Any = None) -> Dict[str, Any]:
        """前端拖拽 drop 事件触发的桥接调用。

        pywebview 6.2+ 的路径注入机制已改为：Python 侧通过 dom.on('drop') 注册
        原生事件，WebView2 在系统层放行拖拽，pywebviewEventHandler 把
        pywebviewFullPath 注入到 event.dataTransfer.files。
        前端调用此方法时，从 _dropped_paths 读取已存储的完整路径。
        """
        return {"ok": True, "paths": self.get_dropped_paths()}

    def get_files_info(self, paths: List[str]) -> List[Dict[str, Any]]:
        """批量返回附件的名称 / 扩展名 / 大小，供前端附件卡片展示。"""
        infos: List[Dict[str, Any]] = []
        for p in paths or []:
            raw = str(p)
            try:
                real_path = _resolve_real_path(raw)
                is_file = os.path.isfile(real_path)
                ext = os.path.splitext(real_path)[1]
                infos.append({
                    "path": raw,
                    "name": os.path.basename(real_path),
                    "ext": ext[1:].lower() if ext else "",
                    "size": os.path.getsize(real_path) if is_file else None,
                    "ok": is_file,
                })
            except Exception:
                infos.append({
                    "path": raw,
                    "name": os.path.basename(raw),
                    "ext": "",
                    "size": None,
                    "ok": False,
                })
        return infos

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------
    def get_background_tasks(self) -> List[Dict[str, Any]]:
        try:
            return list_active_tasks()
        except Exception:
            logger.debug("get_background_tasks 失败", exc_info=True)
            return []

    def kill_task(self, task_id: str) -> Dict[str, Any]:
        try:
            ok = kill_background_task(task_id)
            return {"ok": bool(ok)}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 内嵌终端（右侧面板）
    # 仅执行用户在终端输入框里手动敲下的命令——模型/工具链路不经过这里，
    # 模型执行命令仍走 execute_command 工具的权限确认流程。
    # ------------------------------------------------------------------
    def run_terminal_command(self, command: str) -> Dict[str, Any]:
        import subprocess

        command = str(command or "").strip()
        if not command:
            return {"ok": False, "msg": "命令为空"}

        with self._term_lock:
            if self._term_proc is not None and self._term_proc.poll() is None:
                return {"ok": False, "msg": "已有命令正在运行，请先等它结束或点击终止"}
            cwd = self._session_workdir(self._current_session)
            if cwd == "GLOBAL" or not os.path.isdir(cwd):
                # M19：GLOBAL 自由对话的 workdir 只是归属标记字符串，直接当
                # Popen cwd 必抛 FileNotFoundError；统一归一为进程 cwd
                cwd = _resolve_real_path(os.getcwd())
            try:
                popen_kwargs = {
                    "shell": True,
                    "cwd": cwd,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "stdin": subprocess.DEVNULL,
                }
                if os.name == "nt":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                else:
                    # M20：独立进程组启动，终止时用 killpg 杀整棵子树，
                    # 避免只 kill shell 父进程后子进程成孤儿
                    popen_kwargs["start_new_session"] = True
                proc = subprocess.Popen(command, **popen_kwargs)
            except Exception as e:
                return {"ok": False, "msg": str(e)}
            self._term_proc = proc

        def _pump():
            # 按行读取并小批量推送：逐行 evaluate_js 会在输出很密时拖垮 WebView
            buf: List[str] = []
            last_flush = time.monotonic()

            def _flush():
                nonlocal last_flush
                if buf:
                    self.emit_event("terminal_output", {"text": "".join(buf)})
                    buf.clear()
                last_flush = time.monotonic()

            try:
                for raw in iter(proc.stdout.readline, b""):
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        # Windows 下 cmd 内置命令常输出 GBK
                        line = raw.decode("gbk", errors="replace")
                    buf.append(line)
                    if time.monotonic() - last_flush >= 0.08 or len(buf) >= 200:
                        _flush()
            except Exception:
                logger.debug("终端输出读取中断", exc_info=True)
            finally:
                _flush()
                try:
                    rc = proc.wait(timeout=5)
                except Exception:
                    rc = -1
                self.emit_event("terminal_finished", {"code": rc})

        threading.Thread(target=_pump, daemon=True, name="gui-terminal-pump").start()
        return {"ok": True, "cwd": cwd}

    def kill_terminal_command(self) -> Dict[str, Any]:
        import subprocess

        with self._term_lock:
            proc = self._term_proc
        if proc is None or proc.poll() is not None:
            return {"ok": False, "msg": "当前没有正在运行的命令"}
        try:
            if os.name == "nt":
                # shell=True 会套一层 cmd，必须 /T 杀掉整棵进程树
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                # M20：配合 Popen 的 start_new_session，杀整个进程组而不是
                # 只 kill shell 父进程（子树否则成孤儿）
                import signal
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 模型管理
    # ------------------------------------------------------------------
    def get_model_settings(self) -> Dict[str, Any]:
        """返回不含 API Key 明文的完整模型设置快照。"""
        config_data = load_config()
        providers = []
        models = []
        for provider in config_data.get("providers", []):
            provider_models = []
            for model in provider.get("models", []):
                item = {
                    "id": str(model.get("id", "")),
                    "name": str(model.get("name", "")),
                    "provider_id": str(provider.get("id", "")),
                    "provider_name": str(provider.get("name", "")),
                    "context_window": int(model.get("context_window", 128000) or 128000),
                    "supports_vision": bool(model.get("supports_vision", False)),
                }
                provider_models.append(item)
                models.append(dict(item))
            provider_item = {
                "id": str(provider.get("id", "")),
                "name": str(provider.get("name", "")),
                "base_url": str(provider.get("base_url", "")),
                "protocol": str(provider.get("protocol", "openai")),
                "preset_id": str(provider.get("preset_id", "")),
                "has_api_key": bool(provider.get("api_key")),
                "models": provider_models,
            }
            providers.append(provider_item)
        return {
            "protocols": [dict(item) for item in API_PROTOCOLS],
            "presets": get_preset_providers(),
            "providers": providers,
            "models": models,
            "active_model_id": str(config_data.get("active_model_id") or ""),
            "secondary_model_id": str(config_data.get("secondary_model_id") or ""),
            "secondary_model_roles": get_secondary_roles(),
        }

    def _all_models(self) -> List[Dict[str, Any]]:
        try:
            return self.get_model_settings()["models"]
        except Exception:
            logger.debug("读取模型列表失败", exc_info=True)
            return []

    def add_preset_provider(self, preset_id: str, api_key: str) -> Dict[str, Any]:
        try:
            provider_id = mm_add_preset_provider(str(preset_id), str(api_key or "").strip())
            self._sync_active_model_context()
            return {
                "ok": True,
                "provider_id": provider_id,
                "settings": self.get_model_settings(),
                "msg": "预置厂商已添加",
            }
        except Exception as e:
            logger.exception("add_preset_provider 失败")
            return {"ok": False, "msg": str(e)}

    def add_provider(self, name: str, base_url: str, api_key: str, protocol: str) -> Dict[str, Any]:
        try:
            if not str(name or "").strip():
                return {"ok": False, "msg": "供应商名称不能为空"}
            provider_id = mm_add_provider(
                str(name).strip(),
                str(base_url or "").strip(),
                str(api_key or "").strip(),
                str(protocol or "openai").strip() or "openai",
            )
            return {
                "ok": True,
                "provider_id": provider_id,
                "settings": self.get_model_settings(),
                "msg": f"已添加供应商 {name}",
            }
        except Exception as e:
            logger.exception("add_provider 失败")
            return {"ok": False, "msg": str(e)}

    def _resolve_provider_id(self, provider_ref: str) -> Optional[str]:
        """provider_ref 既可能是真实 id，也可能是前端回传的供应商名称。"""
        ref = str(provider_ref or "").strip()
        if not ref:
            return None
        providers = load_config().get("providers", [])
        for p in providers:
            if str(p.get("id", "")) == ref:
                return str(p.get("id"))
        for p in providers:
            if str(p.get("name", "")) == ref:
                return str(p.get("id"))
        return None

    def update_provider(
        self,
        provider_id: str,
        name: str,
        base_url: str,
        api_key: str,
        protocol: str,
    ) -> Dict[str, Any]:
        try:
            mm_update_provider(
                str(provider_id),
                name=str(name or "").strip(),
                base_url=str(base_url or "").strip(),
                api_key=str(api_key or "").strip(),
                protocol=str(protocol or "").strip(),
            )
            self._sync_active_model_context()
            return {"ok": True, "settings": self.get_model_settings(), "msg": "厂商已更新"}
        except Exception as e:
            logger.exception("update_provider 失败")
            return {"ok": False, "msg": str(e)}

    def delete_provider(self, provider_id: str) -> Dict[str, Any]:
        try:
            mm_delete_provider(str(provider_id))
            self._sync_active_model_context()
            return {"ok": True, "settings": self.get_model_settings(), "msg": "厂商已删除"}
        except Exception as e:
            logger.exception("delete_provider 失败")
            return {"ok": False, "msg": str(e)}

    def add_model(
        self,
        provider_id: str,
        model_name: str,
        context_window: int = 128000,
        supports_vision: bool = False,
    ) -> Dict[str, Any]:
        try:
            if not str(model_name or "").strip():
                return {"ok": False, "msg": "模型名称不能为空"}
            real_provider_id = self._resolve_provider_id(provider_id)
            if real_provider_id is None:
                return {"ok": False, "msg": f"未找到供应商: {provider_id}"}
            try:
                window = int(context_window or 128000)
            except (TypeError, ValueError):
                window = 128000
            # LiteLLM 传输前缀与厂商作用域模型 ID 的拼接规则由 model_manager 统一处理。
            model_id = add_model_to_provider(
                real_provider_id,
                str(model_name).strip(),
                window,
                supports_vision=bool(supports_vision),
            )
            self._sync_active_model_context()
            return {
                "ok": True,
                "model_id": model_id,
                "models": self._all_models(),
                "settings": self.get_model_settings(),
                "msg": f"已添加模型 {model_name}",
            }
        except Exception as e:
            logger.exception("add_model 失败")
            return {"ok": False, "msg": str(e)}

    def update_model(
        self,
        provider_id: str,
        model_id: str,
        model_name: str,
        context_window: int,
        supports_vision: bool,
    ) -> Dict[str, Any]:
        try:
            mm_update_model(
                str(provider_id),
                str(model_id),
                name=str(model_name or "").strip(),
                context_window=int(context_window),
                supports_vision=bool(supports_vision),
            )
            self._sync_active_model_context()
            return {"ok": True, "settings": self.get_model_settings(), "msg": "模型已更新"}
        except Exception as e:
            logger.exception("update_model 失败")
            return {"ok": False, "msg": str(e)}

    def delete_model(self, model_id: str) -> Dict[str, Any]:
        try:
            known = {m["id"] for m in self._all_models()}
            if str(model_id) not in known:
                return {"ok": False, "msg": f"未找到模型: {model_id}", "models": self._all_models()}
            mm_delete_model(str(model_id))
            self._sync_active_model_context()
            return {
                "ok": True,
                "models": self._all_models(),
                "settings": self.get_model_settings(),
            }
        except Exception as e:
            logger.exception("delete_model 失败")
            return {"ok": False, "msg": str(e)}

    def test_model(self, model_id: str) -> Dict[str, Any]:
        """按厂商实际协议发起最小流请求，验证完整请求链路。"""
        try:
            target = None
            provider = None
            for p in load_config().get("providers", []):
                for m in p.get("models", []):
                    if str(m.get("id")) == str(model_id):
                        target, provider = m, p
                        break
                if target:
                    break
            if target is None:
                return {"ok": False, "msg": f"未找到模型: {model_id}"}

            result: Dict[str, Any] = {}

            def _ping():
                try:
                    from core.llm import _request_stream

                    stream, _holder = _request_stream(
                        provider.get("protocol", "openai"),
                        {
                            "model": target.get("litellm_model"),
                            "api_key": provider.get("api_key") or "",
                            "api_base": provider.get("base_url") or None,
                            "messages": [{"role": "user", "content": "ping"}],
                            "stream": True,
                            "max_tokens": 1,
                            "num_retries": 0,
                        },
                        timeout=TEST_MODEL_TIMEOUT,
                    )
                    for _ in stream:
                        pass
                    result["ok"] = True
                    result["msg"] = "连接成功"
                except Exception as e:
                    result["ok"] = False
                    result["msg"] = _sanitize_for_ui(str(e), provider.get("api_key"))[:300]

            thread = threading.Thread(target=_ping, daemon=True, name="gui-test-model")
            thread.start()
            thread.join(timeout=TEST_MODEL_TIMEOUT)
            if thread.is_alive():
                return {"ok": False, "msg": f"连接超时（超过 {TEST_MODEL_TIMEOUT} 秒）"}
            return {"ok": bool(result.get("ok")), "msg": str(result.get("msg", "未知结果"))}
        except Exception as e:
            logger.exception("test_model 失败")
            return {"ok": False, "msg": str(e)}
