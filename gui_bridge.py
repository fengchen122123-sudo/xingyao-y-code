"""
gui_bridge.py
=============
PyWebView JS-API 桥接层：为 Vue 前端提供后端能力，并把一轮对话中的
流式 Token、思考过程、工具卡片、文件 Diff、权限确认等实时推送到界面。

设计约束：
- 绝不 import main 或任何依赖 prompt_toolkit 的模块，对话逻辑一律复用
  core/agent_service.py（与 TUI 完全同源，保证回答质量一致）。
- 所有对外方法都返回可 JSON 序列化的 dict，异常一律吞掉后返回
  {"ok": False, "msg": ...}，绝不让异常穿过桥接层（否则前端 Promise 直接 reject）。
"""

import json
import logging
import os
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
    add_model_to_provider,
    add_provider as mm_add_provider,
    delete_model as mm_delete_model,
    get_active_model,
    load_config,
    set_active_model,
)
from core.permission_broker import permission_broker
from core.skills import get_active_skill_ids, save_active_skill_ids, scan_all_skills
from core.task_manager import kill_background_task, list_active_tasks
from core.version import __version__ as YCODE_VERSION

logger = logging.getLogger(__name__)

# MCP 单例：import 失败（如未安装 mcp 依赖）不能拖垮整个 GUI
try:
    from core.mcp_client import mcp_manager as _mcp_manager_singleton
except Exception:  # pragma: no cover - 依赖缺失时的降级路径
    _mcp_manager_singleton = None

# 流式 token 批量推送间隔（秒）：过密的 evaluate_js 调用会拖垮 WebView
TOKEN_FLUSH_INTERVAL = 0.06

# 权限请求超时（秒）：超时一律按拒绝处理，绝不放行
PERMISSION_TIMEOUT_SECONDS = 300

# 文件预览上限：256KB
FILE_PREVIEW_MAX_BYTES = 256 * 1024

# 文件树扫描限制
FILE_TREE_MAX_DEPTH = 4
FILE_TREE_MAX_ENTRIES = 500
FILE_TREE_SKIP_DIRS = {
    ".git", "venv", ".venv", "node_modules", "__pycache__", "dist", "build",
}

# 测试模型连通性的超时（秒）
TEST_MODEL_TIMEOUT = 15

# 后台子代理全部终态后自动发起的汇总回合（与 TUI 的内部汇总指令同文案）
_BG_SUMMARY_PROMPT = (
    "请汇总所有后台子代理任务的结果并告诉我。如果某些任务失败了，"
    "请用你自身的工具（如 search_news / fetch_url 等）接手完成，然后一并汇总。"
)
# 内部汇总回合在前端展示的系统提示文案（代替把上面的指令原文渲染成用户气泡）
_BG_SUMMARY_DISPLAY_TEXT = "📦 所有后台子代理任务已结束，正在自动汇总结果…"


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
    """将可能包含 API key 的错误文本脱敏后再输出到 UI。"""
    if not isinstance(text, str):
        text = str(text)
    if api_key and len(api_key) >= 4 and api_key in text:
        text = text.replace(api_key, "***")
    return text


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
        return None  # GUI 没有 TUI prompt_toolkit App

    @property
    def permission_mode(self) -> str:
        return self._bridge._current_permission_mode

    @permission_mode.setter
    def permission_mode(self, val: str):
        self._bridge._current_permission_mode = app_context.normalize_permission_mode(val)

    @property
    def plan_approved_this_turn(self) -> bool:
        return self._bridge._plan_approved_this_turn

    @plan_approved_this_turn.setter
    def plan_approved_this_turn(self, val: bool):
        self._bridge._plan_approved_this_turn = val

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
    # GUI 与 TUI 一样统一走 core.permission_broker（见 _activate_permission_request）。

    def invalidate_ui(self):
        pass  # GUI 通过事件驱动，不需要主动重绘


class ApiBridge:
    def __init__(self, mcp_manager=None):
        # 内部状态变量使用下划线开头，防止 PyWebView 在 Windows .NET 检查中产生属性递归问题
        self._window = None
        # GUI 专用会话库：与 TUI 的 ycode.db 分离，两边对话互不同步
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
        self._plan_approved_this_turn = False
        self._task_status = "idle"
        self._active_turn_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        # 回合启动互斥：send_user_message 与后台自动汇总都可能启动 turn 线程，
        # "检查 is_alive + 启动"必须原子，否则两边同时通过检查会并发跑两个回合
        self._turn_start_lock = threading.Lock()
        # 后台子代理自动汇总（与 TUI 的 _bg_summary_triggered 同源）：
        # 每批后台任务只触发一次汇总，用户主动发新消息时重置
        self._bg_summary_triggered: set = set()
        self._bg_summary_lock = threading.Lock()
        # 终态回调到来时若有回合正在运行，挂起到回合结束后补触发
        self._bg_summary_pending_session: str = ""

        # 流式 token 批量推送缓冲
        self._delta_lock = threading.Lock()
        self._delta_buffer: List[str] = []
        self._delta_kind = "token_delta"
        self._delta_last_flush = 0.0
        self._delta_flusher: Optional[threading.Thread] = None

        # 权限请求：request_id -> 超时定时器
        self._perm_timers: Dict[str, threading.Timer] = {}
        self._perm_lock = threading.Lock()

        # 内嵌终端：右侧面板用户手动执行命令的子进程（同一时刻最多一个）
        self._term_proc = None
        self._term_lock = threading.Lock()

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
        # 终态后自动发起一轮汇总 turn（对应 TUI 的 _on_background_subagent_terminal）
        try:
            from core.tools import subagent_manager
            subagent_manager.set_on_terminal(self._on_background_subagent_terminal)
        except Exception:
            logger.warning("注册后台子代理终态回调失败", exc_info=True)

    # ------------------------------------------------------------------
    # 基础设施：窗口、事件推送
    # ------------------------------------------------------------------
    def set_window(self, window):
        """设置 PyWebView window 引用"""
        self._window = window
        self._hwnd = 0
        self._titlebar_dark = True  # 前端默认深色主题
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

    def _on_window_frame_event(self):
        """窗口框架事件回调：DWM 重建非客户区后重设标题栏主题（线程内延迟执行）。"""
        threading.Thread(target=self._reapply_worker, daemon=True,
                         name="gui-titlebar-event-reapply").start()

    # ------------------------------------------------------------------
    # 窗口标题栏主题（Windows DWM 沉浸式深色标题栏，保留原生最小化/关闭按钮）
    # ------------------------------------------------------------------
    def _find_hwnd_worker(self):
        """按窗口标题轮询查找 HWND（原生窗口在 webview.start 后才创建）。
        冻结版启动慢（WebView2 初始化/首次解压），轮询预算放宽到 60 秒。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            title = getattr(self._window, "title", "") or ""
            for _ in range(300):  # 最多等 60 秒
                hwnd = ctypes.windll.user32.FindWindowW(None, title)
                if hwnd:
                    self._hwnd = hwnd
                    # 先设图标再设标题栏主题：WM_SETICON 会触发 DWM 重建非客户区，
                    # 把已应用的标题栏颜色重置回系统默认（白），顺序不能反。
                    self._apply_window_icon()
                    time.sleep(0.05)
                    self._apply_titlebar_theme()
                    # WebView2 子窗口就绪、DPI 变化等时机也可能重建边框，延迟补两次
                    threading.Thread(target=self._reapply_worker, daemon=True,
                                     name="gui-titlebar-reapply").start()
                    return
                time.sleep(0.2)
        except Exception:
            logger.debug("查找窗口句柄失败", exc_info=True)

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

    def _sync_active_model_context(self):
        """把当前激活模型同步给 app_context，供 core 层（子代理等）读取。"""
        try:
            active = get_active_model() or {}
            app_context.set_active_model_tuple(
                active.get("litellm_model", ""),
                active.get("api_key", ""),
                active.get("base_url", ""),
            )
        except Exception:
            logger.debug("同步激活模型到 app_context 失败", exc_info=True)

    def emit_event(self, event_type: str, data: Any):
        """向 JS 传递结构化事件（非流式事件会先冲刷 token 缓冲，保证顺序）。"""
        if event_type not in ("token_delta", "thinking_delta"):
            self._flush_deltas()
        self._emit_raw(event_type, data)

    def _emit_raw(self, event_type: str, data: Any):
        if not self._window:
            return
        try:
            # ensure_ascii=False 保留中文原文（体积更小、调试可读）；
            # JSON 是 JS 字面量的子集，唯二的例外是 U+2028 / U+2029
            # （JS 里属于行终止符，直接嵌入会造成语法错误），单独转义即可。
            payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
            payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
            self._window.evaluate_js(f"window.onBridgeEvent && window.onBridgeEvent({payload})")
        except Exception as e:
            print(f"[GuiBridge] Error emitting event {event_type}: {e}", file=sys.stderr)

    # -- 流式增量的批量推送 ------------------------------------------------
    def _emit_delta(self, kind: str, text: str):
        """累积流式文本，最多每 TOKEN_FLUSH_INTERVAL 推送一次。"""
        if not text:
            return
        flush_now = False
        with self._delta_lock:
            if kind != self._delta_kind and self._delta_buffer:
                # 思考流与正文流切换时先把旧缓冲吐出去，避免串台
                pending_kind, pending_text = self._delta_kind, "".join(self._delta_buffer)
                self._delta_buffer = []
                self._emit_raw(pending_kind, {"text": pending_text})
            self._delta_kind = kind
            self._delta_buffer.append(text)
            now = time.monotonic()
            if now - self._delta_last_flush >= TOKEN_FLUSH_INTERVAL:
                flush_now = True
        self._ensure_delta_flusher()
        if flush_now:
            self._flush_deltas()

    def _flush_deltas(self):
        with self._delta_lock:
            if not self._delta_buffer:
                return
            kind = self._delta_kind
            text = "".join(self._delta_buffer)
            self._delta_buffer = []
            self._delta_last_flush = time.monotonic()
        self._emit_raw(kind, {"text": text})

    def _ensure_delta_flusher(self):
        """惰性启动尾包冲刷线程：保证最后一小段文本不会滞留在缓冲里。"""
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
        try:
            active_model = get_active_model() or {}
            config_data = load_config()

            models = []
            providers = []
            for provider in config_data.get("providers", []):
                providers.append({
                    "id": str(provider.get("id", "")),
                    "name": str(provider.get("name", "")),
                    "base_url": str(provider.get("base_url", "")),
                    "protocol": str(provider.get("protocol", "openai")),
                })
                for m in provider.get("models", []):
                    models.append({
                        "id": str(m.get("id", "")),
                        "name": str(m.get("name", "")),
                        "provider_name": str(provider.get("name", "")),
                    })

            # 以“当前会话归属”恢复工作区上下文（启动时已自动选中最近一次对话）：
            # GLOBAL 会话显示自由对话；项目会话切到其工作目录并加载对应文件树。
            # 不能用启动目录判断——否则会出现“显示在工作区、实际发进对话”的错位。
            cur_wdir = self._session_workdir(self._current_session)
            is_global = (cur_wdir == "GLOBAL")
            if is_global:
                workdir = "GLOBAL"
                file_tree: List[Dict[str, Any]] = []
            else:
                if os.path.isdir(cur_wdir) and os.path.abspath(cur_wdir) != os.path.abspath(os.getcwd()):
                    os.chdir(cur_wdir)
                workdir = _resolve_real_path(os.getcwd())
                file_tree = self._get_file_tree(workdir)
            self._recalc_token_usage(self._current_session)
            return {
                "models": models,
                "providers": providers,
                "active_model_id": str(config_data.get("active_model_id") or ""),
                "active_model_name": str(active_model.get("name") or "未激活模型"),
                "permission_mode": self._current_permission_mode,
                "sessions": self.get_sessions(),
                "skills": self._build_skill_list(),
                "mcps": self._build_mcp_list(),
                "plugins": self.get_plugins(),
                "workdir": workdir,
                "is_global": is_global,
                "file_tree": file_tree,
                "current_session": self._current_session,
                "messages": self._load_session_messages(self._current_session),
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
                "sessions": [], "skills": [], "mcps": [],
                "workdir": "", "current_session": self._current_session,
                "token_usage": {"used": 0, "window": 0, "cached_pct": 0, "cumulative": 0},
                "version": str(YCODE_VERSION), "ok": False, "msg": str(e),
            }

    # ------------------------------------------------------------------
    # 文件树 / 工作区
    # ------------------------------------------------------------------
    def _get_file_tree(self, path: str, depth: int = FILE_TREE_MAX_DEPTH) -> List[Dict[str, Any]]:
        """递归扫描目录树；限制深度与总条目数，跳过体积巨大的构建/依赖目录。"""
        counter = {"n": 0}

        def _scan(cur_path: str, cur_depth: int) -> List[Dict[str, Any]]:
            if cur_depth <= 0 or counter["n"] >= FILE_TREE_MAX_ENTRIES:
                return []
            items = []
            try:
                entries = sorted(os.listdir(cur_path))
            except Exception:
                return []
            for entry in entries:
                if counter["n"] >= FILE_TREE_MAX_ENTRIES:
                    break
                if entry in FILE_TREE_SKIP_DIRS or entry.startswith("."):
                    continue
                full_path = os.path.join(cur_path, entry)
                try:
                    if os.path.islink(full_path):
                        continue
                    is_dir = os.path.isdir(full_path)
                except Exception:
                    continue
                counter["n"] += 1
                # 同时提供两套字段名：n/d 供当前前端组件使用，
                # name/type 为语义化别名，便于后续前端演进。
                item: Dict[str, Any] = {
                    "n": str(entry),
                    "d": 1 if is_dir else 0,
                    "name": str(entry),
                    "type": "dir" if is_dir else "file",
                }
                if is_dir:
                    children = _scan(full_path, cur_depth - 1)
                    if children:
                        item["children"] = children
                items.append(item)
            return items

        if not path or path == "GLOBAL" or not os.path.isdir(path):
            return []
        return _scan(path, depth)

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
            os.chdir(selected_path)
            self._memory.add_workspace(selected_path)
            # 当前会话跟随进入所选工作区：否则启动时挂在 GLOBAL 的会话
            # 即使已在项目文件夹里干活，侧栏仍会一直显示在"对话"分组下
            if self._current_session:
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

    # ------------------------------------------------------------------
    # 会话管理
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

                if wdir == "GLOBAL":
                    global_group["sessions"].append({
                        "name": name,
                        "display_name": display,
                        "count": msg_count,
                        "workdir": "GLOBAL",
                        "is_global": True,
                        "updated_at": s.get("updated_at") or "",
                    })
                    continue

                wdir_norm = _resolve_real_path(wdir) if wdir else current_wdir
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

            # 3. 全局对话组无条件保留在最顶部，随后接入引入的所有项目分组
            result_groups = [global_group]
            project_groups = list(groups_dict.values())
            project_groups.sort(key=lambda g: (0 if g["is_current"] else 1, g["folder_name"]))
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

            if not is_global and target_workdir and os.path.isdir(target_workdir):
                if os.path.abspath(target_workdir) != os.path.abspath(os.getcwd()):
                    os.chdir(target_workdir)

            msgs = self._load_session_messages(session_name)

            self._recalc_token_usage(session_name)
            result = {
                "ok": True,
                "session_name": session_name,
                "messages": msgs,
                "skills": self._build_skill_list(),
                "sessions": self.get_sessions(),
            }
            if is_global:
                result.update({"workdir": "GLOBAL", "is_global": True, "file_tree": []})
            else:
                current_workdir = _resolve_real_path(os.getcwd())
                result.update({
                    "workdir": current_workdir,
                    "is_global": False,
                    "file_tree": self._get_file_tree(current_workdir),
                })
            self._emit_token_usage()
            return result
        except Exception as e:
            logger.exception("switch_session 失败")
            return {"ok": False, "msg": str(e), "messages": []}

    def rename_session(self, old_name: str, new_name: str) -> Dict[str, Any]:
        try:
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
            self._recalc_token_usage(target)
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

            set_active_model(str(model_id))
            self._sync_active_model_context()
            self._emit_token_usage()
            return {
                "ok": True,
                "active_model_id": str(model_id),
                "active_model_name": str(matched.get("name", "")),
            }
        except Exception as e:
            logger.exception("set_model 失败")
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
    # 权限确认：统一走 core.permission_broker（与 TUI 同一套串行化机制）
    # ------------------------------------------------------------------
    def _activate_permission_request(self, request):
        """broker 激活某个请求 → 推送到前端弹窗，并挂一个"超时即拒绝"的定时器。"""
        try:
            self.emit_event("permission_request", {
                "request_id": request.request_id,
                "function_name": request.function_name,
                # 兼容字段：部分旧界面读 tool_name / details
                "tool_name": request.function_name,
                "arguments": dict(request.arguments or {}),
                "details": dict(request.arguments or {}),
                "source": request.source,
                "options": list(request.options or ()),
            })
        except Exception:
            logger.exception("推送权限请求失败")

        def _timeout_deny():
            try:
                # 超时一律按"拒绝"处理，绝不因为用户没看到弹窗就放行
                if permission_broker.is_active(request.request_id):
                    permission_broker.resolve(request.request_id, 1)
                    self.emit_event("error", {"message": "权限确认超时，已自动拒绝该操作。"})
            except Exception:
                logger.debug("权限超时处理失败", exc_info=True)

        timer = threading.Timer(PERMISSION_TIMEOUT_SECONDS, _timeout_deny)
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

    # ------------------------------------------------------------------
    # 对话流
    # ------------------------------------------------------------------
    def send_user_message(self, text: str, session_name: Optional[str] = None, attachments: Optional[List[str]] = None) -> Dict[str, Any]:
        try:
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
                if self._active_turn_thread and self._active_turn_thread.is_alive():
                    return {"ok": False, "msg": "已有正在运行的回合"}

                # 用户主动发起新对话：重置该会话的自动汇总标志，
                # 下一批后台任务完成时可以再次触发自动汇总（与 TUI 行为一致）
                with self._bg_summary_lock:
                    self._bg_summary_triggered.discard(target_session)

                self._cancel_event = threading.Event()
                self._active_turn_thread = threading.Thread(
                    target=self._run_turn_worker,
                    args=(full_text, target_session),
                    kwargs={"display_text": clean_text} if attach_list else {},
                    daemon=True,
                    name="gui-turn-worker",
                )
                self._active_turn_thread.start()
            return {"ok": True}
        except Exception as e:
            logger.exception("send_user_message 失败")
            return {"ok": False, "msg": str(e)}

    def stop_current_turn(self) -> Dict[str, Any]:
        try:
            self._cancel_event.set()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def _run_turn_worker(self, user_text: str, session_name: str, internal_kind: str = "", display_text: str = ""):
        """后台线程：组装上下文 → 调用模型 → 翻译事件推送给前端。

        internal_kind 非空表示这是系统自动发起的内部回合（如后台任务汇总），
        turn_start 事件会带上 internal / display_text，让前端用系统提示样式
        展示而不是把内部指令原文当成用户气泡。
        """
        api_key = ""
        turn_finished = False
        cancelled = False
        error_state = {
            "toast_messages": set(),
            "terminal_message": None,
        }

        def finish_once():
            """turn_finish 只允许发出一次（历史实现在 end + finally 各发一次）。"""
            nonlocal turn_finished
            if turn_finished:
                return
            turn_finished = True
            self.emit_event("turn_finish", {})
            self._emit_token_usage()

        self._task_status = "executing"
        turn_start_data = {"user_text": user_text, "session_name": session_name}
        if display_text and display_text != user_text:
            turn_start_data["display_text"] = display_text
        if internal_kind:
            turn_start_data["internal"] = internal_kind
            turn_start_data["display_text"] = _BG_SUMMARY_DISPLAY_TEXT
        self.emit_event("turn_start", turn_start_data)

        try:
            self._plan_approved_this_turn = False
            active_model = get_active_model()
            if not active_model or not active_model.get("litellm_model"):
                # 关键修复：没有激活模型时也必须让前端解锁输入框
                self.emit_event("error", {"message": "当前未激活任何 LLM 模型，请先在模型管理中配置并激活模型。"})
                self.emit_event("turn_cancelled", {})
                finish_once()
                return

            model_name = active_model["litellm_model"]
            api_key = active_model.get("api_key", "")
            api_base = active_model.get("base_url", "")
            ctx_win = active_model.get("context_window", 128000)
            self._sync_active_model_context()

            # 确保会话存在，再解析用户输入（支持 @文件 / 图片挂载）并入库
            self._memory.ensure_session(session_name, workdir=self._session_workdir(session_name), source="GUI")
            try:
                from core.input_parser import parse_user_input
                parsed_content, attached_msgs = parse_user_input(user_text)
            except Exception:
                logger.debug("解析用户输入失败，退回纯文本", exc_info=True)
                parsed_content, attached_msgs = user_text, []
            for note in attached_msgs or []:
                self.emit_event("notice", {"message": str(note)})
            self._memory.add_message(session_name, "user", parsed_content)

            turn_id = f"{session_name}:{uuid.uuid4().hex[:8]}"
            self._set_current_session(session_name)

            # 与 TUI 完全同源的上下文组装（记忆压缩 / 系统提示词 / 运行上下文 / 后台任务汇总）
            from core.agent_service import prepare_turn_messages
            history = prepare_turn_messages(
                self._memory,
                session_name,
                model_name,
                ctx_win,
                permission_mode=self._current_permission_mode,
                cancel_event=self._cancel_event,
                notify=lambda kind, message: self.emit_event("notice", {"message": str(message)}),
                api_key=api_key,
                api_base=api_base,
            )

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
                self.emit_event("turn_cancelled", {})
            else:
                self._maybe_auto_rename(session_name)
        except Exception as e:
            logger.exception("GUI 对话回合失败")
            self._emit_owned_error(str(e), api_key, error_state, terminal=True)
        finally:
            self._task_status = "idle"
            try:
                permission_broker.cancel_owner(session_name)
            except Exception:
                pass
            finish_once()
            # 回合运行期间若有后台子代理进入终态（当时不能并发起新回合），
            # 此时补触发自动汇总检查
            pending = self._bg_summary_pending_session
            if pending:
                self._bg_summary_pending_session = ""
                threading.Thread(
                    target=self._deferred_bg_summary_check,
                    args=(pending,),
                    daemon=True,
                    name="gui-bg-summary-recheck",
                ).start()

    def _on_background_subagent_terminal(self, task_id: str, status: str, description: str, summary: str) -> None:
        """后台子代理终态实时通知（worker 线程触发，与 TUI 同源的 GUI 版本）。

        职责一：立即向前端推送一条终态通知（前端以 toast 呈现）。
        职责二：延迟检查本会话是否所有后台任务都已终态，是则自动发起一轮
        汇总 turn，把结果主动呈现给用户——不用等用户手动发下一条消息。
        """
        try:
            self.emit_event("background_subagent_terminal", {
                "task_id": task_id,
                "status": status,
                "description": (description or "")[:60],
                "summary": (summary or "")[:150],
            })
        except Exception:
            logger.debug("推送后台子代理终态通知失败", exc_info=True)

        session = self._current_session
        if not session:
            return
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
            self._maybe_start_bg_summary_turn(session)
        except Exception:
            logger.debug("后台任务自动汇总触发失败", exc_info=True)

    def _maybe_start_bg_summary_turn(self, session: str) -> None:
        """条件满足时自动发起汇总 turn。

        条件：会话未切走、无仍在运行的后台任务、本批任务未触发过汇总、
        当前没有正在运行的回合（有则挂起，回合结束后由 _run_turn_worker 补触发）。
        """
        from core.tools import subagent_manager

        if not session or session != self._current_session:
            return
        if subagent_manager.has_running_background_tasks(session):
            return

        with self._turn_start_lock:
            if self._active_turn_thread and self._active_turn_thread.is_alive():
                # 回合运行中不能并发起新回合；挂起，回合结束时补检查
                self._bg_summary_pending_session = session
                return
            with self._bg_summary_lock:
                if session in self._bg_summary_triggered:
                    return
                self._bg_summary_triggered.add(session)
            self._cancel_event = threading.Event()
            self._active_turn_thread = threading.Thread(
                target=self._run_turn_worker,
                args=(_BG_SUMMARY_PROMPT, session, "bg_summary"),
                daemon=True,
                name="gui-bg-summary-turn",
            )
            self._active_turn_thread.start()

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
                    self.emit_event("subagent_progress", {"tasks": tasks})
            except Exception:
                logger.debug("推送子代理进度失败", exc_info=True)
            if stopped:
                break

    def _emit_owned_error(self, message, api_key, error_state, *, terminal):
        """Emit one terminal state and one toast per sanitized error message."""
        sanitized = _sanitize_for_ui(str(message), api_key)
        if terminal and error_state["terminal_message"] is None:
            error_state["terminal_message"] = sanitized
            self.emit_event("turn_error", {"message": sanitized})

        toast_messages = error_state["toast_messages"]
        if sanitized in toast_messages:
            return
        toast_messages.add(sanitized)
        self.emit_event("error", {"message": sanitized})

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
            cancel_event=self._cancel_event,
            parent_session=session_name,
            parent_turn_id=turn_id,
            permission_owner=turn_id,
            permission_source="主代理",
        ):
            if self._cancel_event.is_set():
                cancelled = True
                break

            kind = getattr(event, "kind", None) or (event.get("type") if isinstance(event, dict) else None)
            if not kind:
                continue
            payload = getattr(event, "payload", None)
            if payload is None and isinstance(event, dict):
                payload = event.get("data")
            data = _to_plain_dict(payload)

            if kind == "thinking_delta":
                self._emit_delta("thinking_delta", str(data.get("text", "")))
            elif kind in ("assistant_delta", "token_delta", "answer_delta"):
                self._emit_delta("token_delta", str(data.get("text", "")))
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
                })
            elif kind == "diff_ready":
                # 优先取纯文本 unified diff；缺失时才退回终端版本（含 ANSI 颜色）
                self.emit_event("diff_ready", {
                    "tool_call_id": str(data.get("tool_call_id") or ""),
                    "file_path": str(data.get("file_path") or ""),
                    "diff_text": str(data.get("diff_text") or data.get("text") or ""),
                })
            elif kind in ("plan_updated", "plan_change"):
                self.emit_event("plan_updated", {
                    "title": str(data.get("title") or "当前任务计划"),
                    "items": data.get("items") or [],
                })
            elif kind == "turn_cancelled":
                cancelled = True
            elif kind == "turn_error":
                self._emit_owned_error(
                    data.get("message") or "模型生成错误",
                    api_key,
                    error_state,
                    terminal=True,
                )
            elif kind == "error":
                self._emit_owned_error(
                    data.get("message") or "模型生成错误",
                    api_key,
                    error_state,
                    terminal=False,
                )
            elif kind in ("notice", "status"):
                message = data.get("message")
                if message:
                    self.emit_event("notice", {"message": str(message)})

        if self._cancel_event.is_set():
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

    def _maybe_auto_rename(self, session_name: str):
        """首轮对话完成后，把默认的 session_xxxxxx 命名换成提炼出的标题。"""
        if not re.match(r"^session_[a-f0-9]{6}$", session_name or ""):
            return
        try:
            history = self._memory.get_history(session_name)
            user_msgs = [m for m in history if m.get("role") == "user"]
            if not user_msgs:
                return
            raw = user_msgs[0].get("content", "")
            if isinstance(raw, list):
                raw = " ".join(
                    str(item.get("text", "")) for item in raw if isinstance(item, dict)
                )
            first_text = str(raw).strip().replace("\n", " ")
            if not first_text:
                return
            base_title = first_text[:12]
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
        except Exception:
            logger.debug("自动重命名会话失败", exc_info=True)

    # ------------------------------------------------------------------
    # Token 用量
    # ------------------------------------------------------------------
    def get_token_usage(self) -> Dict[str, Any]:
        try:
            from core.llm import session_token_usage, token_usage_lock
            with token_usage_lock:
                latest_prompt = int(session_token_usage.get("latest_prompt", 0) or 0)
                latest_total = int(session_token_usage.get("latest_total", 0) or 0)
                cumulative = int(session_token_usage.get("cumulative_total", 0) or 0)
                cum_cached = int(session_token_usage.get("cumulative_cached", 0) or 0)
            active = get_active_model() or {}
            window = int(active.get("context_window", 128000) or 128000)
            cached_pct = round((cum_cached / cumulative * 100), 1) if cumulative > 0 else 0
            return {
                # 首轮请求返回前 latest_prompt 还是 0，用本地估算的 latest_total 兜底
                "used": latest_prompt or latest_total,
                "window": window,
                "cached_pct": cached_pct,
                "cumulative": cumulative,
            }
        except Exception:
            logger.debug("get_token_usage 失败", exc_info=True)
            return {"used": 0, "window": 0, "cached_pct": 0, "cumulative": 0}

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
            from core.plugin_loader import get_plugin_infos
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
    _MCP_EXAMPLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp.example.json")

    @staticmethod
    def _is_placeholder(val: Any) -> bool:
        """判断配置值是否为示例占位符（如 your-xxx-key / cli_your_app_id）。"""
        return isinstance(val, str) and bool(re.search(r"your", val, re.I))

    def _load_mcp_presets(self) -> Dict[str, Dict[str, Any]]:
        """读取 mcp.example.json 中的预置服务模板，并解析出需要用户填写的密钥项。

        必填项来源：env 中值为占位符的键（key=环境变量名），
        以及 args 中整段为占位符的参数（key=占位符原文，如 cli_your_app_id）。
        """
        presets: Dict[str, Dict[str, Any]] = {}
        try:
            with open(self._MCP_EXAMPLE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return presets
        for name, cfg in (data.get("mcpServers") or {}).items():
            if not isinstance(cfg, dict):
                continue
            required: List[str] = []
            env = cfg.get("env") or {}
            if isinstance(env, dict):
                for k, v in env.items():
                    if self._is_placeholder(v):
                        required.append(str(k))
            for a in cfg.get("args") or []:
                if self._is_placeholder(a) and str(a) not in required:
                    required.append(str(a))
            presets[str(name)] = {
                "command": str(cfg.get("command") or ""),
                "args": list(cfg.get("args") or []),
                "env": dict(env) if isinstance(env, dict) else {},
                "comment": str(cfg.get("comment") or ""),
                "required_inputs": required,
            }
        return presets

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
        configured_names = set()
        for item in raw:
            name = str(item.get("name", ""))
            configured_names.add(name)
            enabled = bool(item.get("enabled", True))
            if enabled_override and name in enabled_override:
                enabled = enabled_override[name]
            result.append({
                "name": name,
                "enabled": enabled,
                "connected": bool(item.get("connected", False)),
                "tools_count": int(counts.get(name, 0)),
                "configured": True,
                "required_inputs": [],
                "comment": "",
            })
        # 追加 mcp.example.json 中尚未配置的预置服务，显示为“预置 · 未配置”
        for pname, preset in self._load_mcp_presets().items():
            if pname in configured_names:
                continue
            result.append({
                "name": pname,
                "enabled": False,
                "connected": False,
                "tools_count": 0,
                "configured": False,
                "required_inputs": list(preset.get("required_inputs") or []),
                "comment": str(preset.get("comment") or ""),
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

    def enable_mcp_server(self, name: str, env_values: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """启用 MCP 服务：已配置的只拨开关；预置服务先把模板（注入用户填写的密钥）
        写入 ~/.ycode/mcp.json 再启用，重连在后台线程完成。
        已输入过的密钥持久化在 mcp.json 中，之后开关不再要求输入。"""
        if self._mcp is None:
            return {"ok": False, "msg": "MCP 管理器不可用（依赖未安装）"}
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "msg": "服务名不能为空"}
        try:
            self._mcp._load_config()
            configured = self._mcp.config.get("mcpServers", {}) or {}
        except Exception:
            configured = {}

        if name in configured:
            # 已配置过（密钥已持久化），直接拨开关即可
            return self.toggle_mcp_server(name, True)

        preset = self._load_mcp_presets().get(name)
        if not preset:
            return {"ok": False, "msg": f"未找到预置服务: {name}"}

        values = {str(k): str(v) for k, v in (env_values or {}).items()}
        # 校验必填项：env 键名 或 args 占位符原文，二者任一命中即可
        missing = [k for k in preset["required_inputs"] if not values.get(k, "").strip()]
        if missing:
            return {"ok": False, "msg": "缺少必填密钥: " + ", ".join(missing), "required_inputs": preset["required_inputs"]}

        # 组装真实配置：env 占位符按键名替换，args 整段占位符按原文替换
        env = {}
        for k, v in preset["env"].items():
            env[k] = values[k] if self._is_placeholder(v) else v
        args = [values[a] if self._is_placeholder(a) else a for a in preset["args"]]

        def _job():
            try:
                ok, msg = self._mcp.add_server(name, preset["command"], args, env=env, enabled=True)
            except Exception as e:
                ok, msg = False, str(e)
            time.sleep(1.5)
            self.emit_event("mcp_status", {"mcps": self._build_mcp_list(), "ok": ok, "msg": msg})

        try:
            threading.Thread(target=_job, daemon=True, name="gui-mcp-enable").start()
            return {
                "ok": True,
                "msg": f"正在启用 {name}，连接完成后会自动刷新状态",
                "mcps": self._build_mcp_list(enabled_override={name: True}),
            }
        except Exception as e:
            logger.exception("enable_mcp_server 失败")
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
            if not os.path.exists(real_path):
                return {"ok": False, "msg": "路径不存在"}
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
            path = os.path.join(paste_dir, f"paste_{ts}_{ms:03d}{MIME_EXT[mime]}")
            with open(path, "wb") as f:
                f.write(raw)
            return {"ok": True, "path": path}
        except Exception as e:
            logger.exception("save_clipboard_image 失败")
            return {"ok": False, "msg": str(e)}

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
            try:
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
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
                proc.kill()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 模型管理
    # ------------------------------------------------------------------
    def _all_models(self) -> List[Dict[str, Any]]:
        models = []
        try:
            for provider in load_config().get("providers", []):
                for m in provider.get("models", []):
                    models.append({
                        "id": str(m.get("id", "")),
                        "name": str(m.get("name", "")),
                        "provider_name": str(provider.get("name", "")),
                    })
        except Exception:
            logger.debug("读取模型列表失败", exc_info=True)
        return models

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
            return {"ok": True, "provider_id": provider_id, "msg": f"已添加供应商 {name}"}
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

    def add_model(self, provider_id: str, model_name: str, context_window: int = 128000) -> Dict[str, Any]:
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
            # litellm_model 的拼接规则（含 "/" 时不重复加协议前缀）由 model_manager 统一处理
            model_id = add_model_to_provider(real_provider_id, str(model_name).strip(), window)
            self._sync_active_model_context()
            return {
                "ok": True,
                "model_id": model_id,
                "models": self._all_models(),
                "msg": f"已添加模型 {model_name}",
            }
        except Exception as e:
            logger.exception("add_model 失败")
            return {"ok": False, "msg": str(e)}

    def delete_model(self, model_id: str) -> Dict[str, Any]:
        try:
            known = {m["id"] for m in self._all_models()}
            if str(model_id) not in known:
                return {"ok": False, "msg": f"未找到模型: {model_id}", "models": self._all_models()}
            mm_delete_model(str(model_id))
            self._sync_active_model_context()
            return {"ok": True, "models": self._all_models()}
        except Exception as e:
            logger.exception("delete_model 失败")
            return {"ok": False, "msg": str(e)}

    def test_model(self, model_id: str) -> Dict[str, Any]:
        """用一次最小 completion 请求验证模型可用性（15 秒超时）。"""
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
                    from core.llm import _get_litellm
                    litellm = _get_litellm()
                    litellm.completion(
                        model=target.get("litellm_model"),
                        api_key=provider.get("api_key") or "",
                        base_url=provider.get("base_url") or None,
                        messages=[{"role": "user", "content": "ping"}],
                        max_tokens=1,
                        timeout=TEST_MODEL_TIMEOUT,
                    )
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
