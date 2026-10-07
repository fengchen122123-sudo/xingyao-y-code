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

import base64
import copy
import ctypes
import json
import logging
import os
import queue
import pathlib
import re
import shutil
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import config
from config import GUI_DB_FILE, MY_PERMISSION_MODE
import core.app_context as app_context
from core.agent_service import strip_context_suffix
from core.injection_markers import (
    BG_COMMAND_SUMMARY_INJECTION_PREFIX,
    BG_SUMMARY_INJECTION_PREFIX,
    is_system_injected_user_text,
)
from core.turn_renderer import sanitize_protocol_text, sanitize_stream_text
from core.media_refs import MEDIA_DISPLAY_ERROR_LABELS
from core.media_server import media_server
from core.media_store import archive_media_file
from core.frameless import _MONITORINFO
from core.memory import MemoryManager
from core.gui_theme import ThemeError, get_repository as get_gui_theme_repository
from core.gui_terminal import PtyTerminalError, clamp_terminal_size, create_pty_terminal
from gui_git_mixin import GitBridgeMixin
from gui_browser_mixin import BrowserBridgeMixin
from gui_snip_mixin import SnipBridgeMixin
from core.model_manager import (
    add_preset_provider as mm_add_preset_provider,
    add_model_to_provider,
    add_provider as mm_add_provider,
    delete_provider as mm_delete_provider,
    delete_model as mm_delete_model,
    get_active_model,
    get_model_reasoning_choice,
    get_secondary_roles,
    load_config,
    set_active_model,
    set_model_reasoning_choice,
    set_secondary_model,
    set_secondary_roles,
    update_model as mm_update_model,
    update_provider as mm_update_provider,
)
from core.model_catalog import API_PROTOCOLS, get_preset_providers
from core.reasoning_capabilities import build_reasoning_snapshot
from core.permission_broker import permission_broker
from core.skills import (
    get_active_skill_ids,
    save_active_skill_ids,
    scan_all_skills,
    restore_builtin_skill,
    get_released_builtin_names,
    # 私有助手：core.skills 未提供公开的内置源目录访问器，技能列表要判断
    # "恢复内置版本"是否可用，只能直接取该目录路径
    _builtin_skills_dir,
)
from core.task_manager import list_active_tasks
from core.version import __version__ as YCODE_VERSION
from core.updater import (
    cleanup_old_installers,
    fetch_latest_release,
    find_cached_installer,
    updater_downloader,
    launch_installer,
)

logger = logging.getLogger(__name__)

# CLI 的 prompt_toolkit 着色标记（<ansiyellow> 等）：GUI 不渲染，桥接层统一剥离，
# 否则用户会在提示里看到 <ansiyellow> 原文。CLI 路径不经此处，不受影响。
_ANSI_MARKUP_RE = re.compile(r"</?ansi\w*>")
_RGB_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DWM_COLOR_CAPABILITY_SUPPORTED = "supported"
_DWM_COLOR_CAPABILITY_UNSUPPORTED = "unsupported"
_DWM_COLOR_CAPABILITY_PENDING = "pending"
_DWM_COLOR_CAPABILITY_NOT_APPLICABLE = "not_applicable"
_DWM_COLOR_CAPABILITY_UNKNOWN = "unknown"

# DwmSetWindowAttribute returns HRESULT. Pre-Win11 builds report custom color
# attributes 34/35/36 as E_INVALIDARG; E_NOTIMPL and HRESULT_FROM_WIN32(
# ERROR_NOT_SUPPORTED) are also explicit capability signals. No other failure
# is treated as unsupported.
_DWM_UNSUPPORTED_HRESULTS = frozenset({0x80070057, 0x80004001, 0x80070032})

# MonitorFromPoint 的 dwFlags：找不到时回退到最近的显示器
_MONITOR_DEFAULTTONEAREST = 2

# 会话历史分页懒加载：切会话/启动只加载最近一页，滚动到顶向前翻页。
# 回合对齐扩展上限为 limit + 30（见 core.memory.get_history_paged）。
HISTORY_PAGE_SIZE = 50

# 入口层受控回合来源：GUI/CLI 本地 = local，LAN 手机端 = lan，飞书通道 = feishu。
# 由入口层注入，模型/用户正文无法设置（send_user_message 校验后拒绝非法值）。
# 与 core.llm.COMPUTER_TURN_SOURCES 必须保持一致——低层还会再校验一次来源，
# 两处不一致会让新来源的回合在模型请求处直接抛错（有测试断言两者相等）。
TURN_SOURCES = ("local", "lan", "feishu")


# _MONITORINFO 结构体统一从 core.frameless 导入：两个模块各自定义同名结构体会
# 在同一个 ctypes.windll.user32 上互相覆盖 GetMonitorInfoW.argtypes，导致
# frameless 的 WM_GETMINMAXINFO 回调调用 GetMonitorInfoW 时抛 ArgumentError，
# 最大化钳制静默失效、窗口覆盖任务栏。单一来源后两者 argtypes 一致，不再冲突。


def _cursor_snap_vk() -> Optional[int]:
    """光标贴到所在显示器边缘时返回对应的 Win+方向键 VK 码，否则 None。

    GetCursorPos 返回的是虚拟桌面坐标：多显示器下，位于主屏左边或上边的显示器
    是负坐标、右边/下边会超出主屏宽高。因此不能拿主屏宽度/原点直接比较，必须
    用 MonitorFromPoint 定位光标实际所在显示器，再相对该显示器 rcMonitor 判断，
    否则第二屏上的拖拽松手会被误判成贴主屏边缘，触发自动贴边/最大化。
    """
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT, ctypes.wintypes.DWORD]
        user32.MonitorFromPoint.restype = ctypes.wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.POINTER(_MONITORINFO),
        ]
        user32.GetMonitorInfoW.restype = ctypes.wintypes.BOOL
        point = ctypes.wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        monitor = user32.MonitorFromPoint(point, _MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
            return None
        if point.x <= mi.rcMonitor.left + 2:
            return 0x25  # VK_LEFT → 左半屏
        if point.x >= mi.rcMonitor.right - 2:
            return 0x27  # VK_RIGHT → 右半屏
        if point.y <= mi.rcMonitor.top + 2:
            return 0x26  # VK_UP → 最大化
        return None
    except Exception:  # noqa: BLE001
        logger.debug("光标贴边判定失败", exc_info=True)
        return None


def _windows_build_number() -> Optional[int]:
    if sys.platform != "win32":
        return None
    try:
        return int(sys.getwindowsversion().build)
    except (AttributeError, OSError, TypeError, ValueError):
        logger.exception("无法确定 Windows build，不能判定 DWM 自定义颜色能力")
        return None


def _rgb_hex_to_colorref(color: str) -> int:
    """Convert CSS #RRGGBB to the Windows COLORREF 0x00BBGGRR layout."""
    if not isinstance(color, str) or not _RGB_HEX_RE.fullmatch(color):
        raise ValueError("color must be #RRGGBB")
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    return red | (green << 8) | (blue << 16)


def _load_initial_gui_theme_state() -> Dict[str, Any]:
    """Resolve the active theme separately from the non-blocking theme catalog."""
    active_theme = None
    themes: List[Dict[str, Any]] = []
    active_error = None
    list_error = None
    try:
        repository = get_gui_theme_repository()
    except ThemeError as error:
        logger.warning("创建 GUI 主题仓库失败: %s", error.as_dict())
        active_error = error.as_dict()
        return {
            "gui_theme": active_theme, "gui_themes": themes,
            "gui_theme_error": active_error, "gui_theme_list_error": list_error,
        }
    except (OSError, IOError) as error:
        logger.exception("创建 GUI 主题仓库失败")
        active_error = ThemeError("theme_io_failed", "gui_theme_id", str(error)).as_dict()
        return {
            "gui_theme": active_theme, "gui_themes": themes,
            "gui_theme_error": active_error, "gui_theme_list_error": list_error,
        }
    except Exception:
        logger.exception("创建 GUI 主题仓库发生未预期错误")
        active_error = ThemeError(
            "theme_operation_failed", "gui_theme_id", "创建 GUI 主题仓库失败",
        ).as_dict()
        return {
            "gui_theme": active_theme, "gui_themes": themes,
            "gui_theme_error": active_error, "gui_theme_list_error": list_error,
        }
    try:
        active_theme = repository.get_active()
    except ThemeError as error:
        logger.warning("读取活动 GUI 主题失败: %s", error.as_dict())
        active_error = error.as_dict()
    except (OSError, IOError) as error:
        logger.exception("读取活动 GUI 主题失败")
        active_error = ThemeError("theme_io_failed", "gui_theme_id", str(error)).as_dict()
    except Exception:
        logger.exception("读取活动 GUI 主题发生未预期错误")
        active_error = ThemeError(
            "theme_operation_failed", "gui_theme_id", "读取活动 GUI 主题失败",
        ).as_dict()
    try:
        themes = repository.list_themes()
    except ThemeError as error:
        logger.warning("读取 GUI 主题列表失败: %s", error.as_dict())
        list_error = error.as_dict()
    except (OSError, IOError) as error:
        logger.exception("读取 GUI 主题列表失败")
        list_error = ThemeError("theme_io_failed", "gui_themes", str(error)).as_dict()
    except Exception:
        logger.exception("读取 GUI 主题列表发生未预期错误")
        list_error = ThemeError(
            "theme_operation_failed", "gui_themes", "读取 GUI 主题列表失败",
        ).as_dict()
    return {
        "gui_theme": active_theme,
        "gui_themes": themes,
        "gui_theme_error": active_error,
        "gui_theme_list_error": list_error,
    }


def _strip_ansi_markup(text) -> str:
    # 剥离 CLI 着色标签后，再去掉首尾空白/换行：CLI 习惯用 \n 做段落分隔，
    # 但 GUI notice 卡片用 white-space: pre-wrap 会把这个换行渲染成空行
    return _ANSI_MARKUP_RE.sub("", str(text)).strip()


def _session_from_turn_id(turn_id: str) -> str:
    value = str(turn_id or "")
    return value.rsplit(":", 1)[0] if ":" in value else ""


def _undo_node_state_dict(state) -> Dict[str, Any]:
    """NodeState → 前端冲突展示 DTO。"""
    return {
        "kind": state.kind.value,
        "exists": bool(state.exists),
        "size": state.size,
        "sha256": state.sha256,
        "identity": state.identity,
        "link_count": state.link_count,
        "symlink_target": state.symlink_target,
    }

def _gui_may_raise_window() -> bool:
    """电脑操控焦点礼让守卫（通用化重构 Task F'）。

    任何会把 ycode 主窗口提到前台/置顶的调用点都必须先经过本守卫：电脑操控
    run 处于 running/paused/waiting_for_user 时返回 False（礼让被控窗口，
    防止操控期间抢焦点），其余状态返回 True。controller 不可用时按可提升
    处理（fail-open：操控关闭时 GUI 行为完全不变）。惰性导入铁律：
    core.computer 只在函数内导入。
    """
    try:
        from core.computer.controller import get_computer_control_service

        status = str(get_computer_control_service().snapshot().status or "")
    except Exception:
        return True
    return status not in ("running", "paused", "waiting_for_user")


def _ensure_session_model_binding(memory, session_name: str, model_id, workdir: str) -> None:
    """先创建会话，仅初始化空绑定；已有值可能是用户刚做的新选择。"""
    memory.ensure_session(session_name, workdir=workdir, source="GUI")
    normalized_model_id = str(model_id or "").strip() or None
    if memory.get_session_model_id(session_name) is None and normalized_model_id is not None:
        memory.set_session_model_id(session_name, normalized_model_id)

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
# 图片预览上限：20MB（base64 传输体积再膨胀约 33%）
IMAGE_PREVIEW_MAX_BYTES = 20 * 1024 * 1024
# 可应用内预览的图片扩展名 → MIME（<img> 加载 data URL 时 SVG 脚本不执行，无 XSS 风险）
IMAGE_PREVIEW_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}

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

# 后台子代理全部终态后自动发起的汇总回合（与 CLI 的内部汇总指令同文案）。
# 前缀复用 core.injection_markers 的常量：注入文案与展示层过滤前缀同源，
# 从结构上杜绝"改了注入文案、忘改过滤表"导致的泄漏（这两类曾渲染成用户
# 气泡并污染红点导航，用户报告过）。
_BG_SUMMARY_PROMPT = (
    BG_SUMMARY_INJECTION_PREFIX + "。如果某些任务失败了，"
    "可以用你自身的工具（如 search_news / fetch_url 等）接手完成；"
    "但已被用户取消的任务不要接手或重试，如实说明即可。"
)
# 后台命令任务完成后自动发起总结回合的注入指令（同样以 role=user 落库，展示层过滤）
_BG_COMMAND_SUMMARY_PROMPT = (
    BG_COMMAND_SUMMARY_INJECTION_PREFIX
    + "，请用一句话简短总结它的结果（成功/失败 + 关键信息，不要复述命令本身）。"
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


def _reasoning_snapshot_for_model(model: dict | None) -> dict:
    """返回指定模型的能力与持久化选择，避免依赖全局活动模型。"""
    snapshot = build_reasoning_snapshot(model)
    model_id = str((model or {}).get("id") or "")
    if model_id and snapshot["kind"] not in {"fixed", "hidden"}:
        snapshot["choice"] = get_model_reasoning_choice(model_id)
    return snapshot


def _media_save_filename(suggested_name: str, ext: str) -> str:
    """把建议文件名清理成安全的单段文件名，并强制使用 Blob 的真实扩展名。

    建议名来自图片 alt / 原始文件名，可能带路径成分、Windows 非法字符，
    或与实际内容不符的扩展名（如 alt 写成 xxx.jpg 而 Blob 是 png）。
    """
    name = re.split(r"[\\/]", (suggested_name or "").strip())[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if "." in name:
        name = name.rsplit(".", 1)[0].strip(" .")
    return (name or "ycode-image") + ext


class ApiBridge(GitBridgeMixin, BrowserBridgeMixin, SnipBridgeMixin):
    _active_turn_lock = threading.Lock()
    _active_turn_snapshots: dict[str, dict] = {}
    _session_last_errors: dict[str, dict] = {}
    _session_last_errors_lock = threading.Lock()

    def __init__(self, mcp_manager=None):
        # 内部状态变量使用下划线开头，防止 PyWebView 在 Windows .NET 检查中产生属性递归问题
        self._window = None
        self._dropped_paths: list = []
        # Git 工作台桥接状态（gui_git_mixin.GitBridgeMixin，惰性初始化）
        self._git_svc = None
        self._git_monitor = None
        self._git_subs = {}
        self._git_versions = {}
        self._git_serialized = {}
        # GUI 专用会话库：与 CLI 的 ycode.db 分离，两边对话互不同步
        self._memory = MemoryManager(db_path=GUI_DB_FILE)
        # 修复：形参曾经遮蔽模块级单例，导致 self._mcp 永远是 None
        self._mcp = mcp_manager if mcp_manager is not None else _mcp_manager_singleton

        # 按回合撤销：注册进程级存储并执行启动恢复扫描（收敛中断事务、
        # 重建路径冻结、清理跨 boot 失效令牌；不自动重放任何文件写入）
        try:
            from core.turn_undo import service as _tu_service

            _tu_service.configure_default_manager(self._memory)
            interrupted = _tu_service.startup_recovery_scan()
            if interrupted:
                logger.warning(
                    "按回合撤销：启动恢复收敛了 %d 个未终态操作", len(interrupted)
                )
        except Exception:
            logger.warning("按回合撤销服务初始化失败", exc_info=True)

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
        # 进行中回合快照（会话名 -> 正在执行的思考、文本、工具列表等，供手机中途扫码或切换会话拉取）
        self._active_turn_snapshots: dict[str, dict] = {}
        self._active_turn_lock = threading.Lock()
        # 每会话当前回合的 turn_id（session:xxx 复合 ID）：权限请求的 owner
        # 是 turn_id 而非会话名，stop_current_turn 要按它精确取消权限等待
        self._active_turn_ids: dict[str, str] = {}
        # 按回合撤销：turn_id（新协议为全局随机 UUID）→ 会话名 的归属映射。
        # 权限请求事件按它路由会话，不再从 turn_id 字符串前缀推导（规格 20.1）
        self._turn_owner_sessions: dict[str, str] = {}
        self._turn_owner_lock = threading.Lock()
        # 回合启动互斥：send_user_message 与后台自动汇总都可能启动 turn 线程，
        # "检查 is_alive + 启动"必须原子，否则两边同时通过检查会并发跑两个回合
        self._turn_start_lock = threading.Lock()
        # 用户消息排队：会话回合运行中收到的新消息进入该队列（FIFO），
        # 回合 finally 结束时自动补发队首。条目含 id/text/display_text/
        # attachments，供前端排队条目展示与取消/插队操作定位
        self._message_queues: dict[str, list[dict]] = {}
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

        # 内嵌交互式终端（底部面板，pywinpty/ConPTY 常驻 shell）：单会话、
        # 用户手动开启；模型/工具链路不经过这里
        self._pty_session = None
        self._pty_lock = threading.Lock()
        # IDE 调试（DAP 适配器进程）：单会话、用户显式启动；读线程只 put_nowait 事件
        self._debug_session = None
        self._debug_lock = threading.Lock()
        # 当前暂停线程/帧：单步与求值都作用在它上面（由 dap_stopped 事件刷新）
        self._debug_last_thread_id = 0
        self._debug_last_frame_id = 0
        # terminated 后由读线程投递出去的「临时回收线程」（见 _discard_debug_session）：
        # 关停时 join 它们，保证适配器进程一定被收掉，不留孤儿
        self._debug_reapers: List[threading.Thread] = []
        # 被调试程序是否已退出（收到 dap_exited）：DAP 里 exited 只表示「被调试程序结束」，
        # 会话未必结束。只用来放行「重新开始调试」时回收旧会话，不在 exited 时丢会话
        # （否则 restart 这类合法流程会被误杀）
        self._debug_debuggee_exited = False
        self._shutdown_lock = threading.Lock()
        self._shutting_down = False
        # 局域网手机远程控制服务（惰性创建，见 Task 3 的 _ensure_lan_server）
        self._lan_server = None
        # 保护 _lan_server 惰性创建的双检锁：并发（启动后台线程 + 用户点二维码）时不得创建两个实例
        self._lan_lock = threading.Lock()
        # 飞书通道服务（惰性创建，见 _ensure_feishu_service）：未配置时完全不启动，
        # 对现有功能零影响
        self._feishu_service = None
        self._feishu_lock = threading.Lock()
        # 电脑操控事件桥：controller listener 注册标记（进程内建立一次、幂等，
        # 见 _ensure_computer_event_bridge；set_window 与各控制方法都会触发）
        self._computer_event_listener = None
        # 技能列表缓存：首屏扫描已改为后台异步（见 _async_build_skill_list），
        # get_initial_state 只返回这里保存的上一次结果，冷盘首次扫描（曾达 25s+）
        # 不再阻塞窗口首帧；None 表示"还没扫过任何一次"。
        self._skills_cache: Optional[List[Dict[str, Any]]] = None
        # 技能扫描去重锁：窗口重载/手机扫码连接会再次调用 get_initial_state，
        # 非阻塞获取失败即说明已有后台扫描在跑，直接复用它稍后推送的结果，
        # 避免冷盘上叠加出多次全目录扫描。
        self._skills_scan_lock = threading.Lock()

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
        self._native_titlebar_color_capability = _DWM_COLOR_CAPABILITY_PENDING

        # 注册 GUI 运行时上下文 + 权限应答通道
        app_context.register(_GuiContext(self))
        app_context.set_ui_flavor("gui")
        app_context.set_manager(self._memory)
        app_context.set_current_session(self._current_session)
        # 保活止损证据跨进程续用：把上一个进程落库的 keepalive 轮次播进内存证据桶。
        # 不播种则每次重启都要重新白烧 10 轮全价前缀才够判一次止损（实测亏损车道
        # 单轮保活平均 10.6 万 token）。失败只记日志，绝不阻断窗口启动。
        try:
            from core.llm_diagnostics import seed_keepalive_lane_evidence_from_store

            seed_keepalive_lane_evidence_from_store()
        except Exception:
            logger.warning("保活车道证据播种失败（已忽略）", exc_info=True)
        self._sync_active_model_context()
        permission_broker.set_prompt_handlers(
            self._activate_permission_request,
            self._dismiss_permission_prompt,
        )
        # 注册 token 记账完成监听器：每次 LLM 请求（含子代理/副模型）记账后
        # 立即推送 token_usage 事件，统计栏与上下文圆圈逐请求实时刷新，
        # 不再等整个回合结束才更新。CLI 未注册时无任何额外开销。
        try:
            from core.llm import set_token_usage_listener
            set_token_usage_listener(self._emit_token_usage)
        except Exception:
            logger.warning("注册 token 用量实时推送监听器失败", exc_info=True)
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

        # 星轨自动化任务（待开发/星轨自动化任务/01-完整设计规格.md）：
        # 存储与调度引擎挂到桥上；引擎不在 __init__ 启动——gui_main 窗口就绪后
        # 调 start_automation_engine()，测试可裸构造桥而不起调度线程
        from core.automation.engine import AutomationEngine
        from core.automation.storage import AutomationStorage
        self._automation_storage = AutomationStorage(self._memory)
        self._automation_storage.on_run_updated = self._on_automation_run_updated
        self._automation_engine = AutomationEngine(
            self._automation_storage, self._dispatch_automation_run
        )
        # 把桥侧自动化能力注册为 core 层服务句柄，供 Agent 工具在对话里复用：
        # 工具经 core.automation.service.get() 取句柄后调用同名方法，落到桥侧既有
        # 实现（参数校验、任务上限检查、事件推送、引擎通知），不在 core 层重复实现。
        self._register_automation_service()
        # 正在运行的星轨自动化回合：turn_id → {"run_id", "task_name"}。
        # 无人值守权限自动拒绝的判定依据（规格 5.5）
        self._automation_turn_labels: Dict[str, Dict[str, str]] = {}

        # 启动时后台策略化清理更新缓存：删除版本 ≤ 当前运行版本的安装包、
        # 下载残片与超期残留，保留版本高于当前的包（用户可能还要安装）
        threading.Thread(
            target=cleanup_old_installers,
            daemon=True,
            name="gui-update-cleaner",
        ).start()

    # ------------------------------------------------------------------
    # 基础设施：窗口、事件推送
    # ------------------------------------------------------------------
    def set_window(self, window):
        """设置 PyWebView window 引用"""
        self._window = window
        self._hwnd = 0
        self._native_titlebar_color_capability = _DWM_COLOR_CAPABILITY_PENDING
        # 首次 DWM 着色直接解析当前活动主题。gui_theme_id 是唯一真值；旧的
        # gui_theme_light 只由仓库迁移逻辑读取，不再单独决定标题栏。
        try:
            theme = get_gui_theme_repository().get_active()
            self._native_titlebar = theme["native_titlebar"]
            self._titlebar_dark = self._native_titlebar["mode"] == "dark"
        except Exception:
            self._native_titlebar = None
            logger.exception("读取活动 GUI 主题失败，标题栏暂不着色")
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
        # 电脑操控事件桥在窗口就绪时建立（进程内一次、幂等）：所有电脑操控 run
        # 都发生在窗口就绪之后，保证 listener 不会迟到丢事件；失败只留痕。
        try:
            self._ensure_computer_event_bridge()
        except Exception:
            logger.exception("建立电脑操控事件桥失败")

    def has_active_turns(self) -> bool:
        """是否有任一会话的回合线程仍在运行（关窗退出确认的判断依据）。

        _active_turns 在回合 finally 中 pop，条目存在即代表回合已登记；
        再用 is_alive 过滤刚登记尚未启动/已结束未清理的极窄竞态窗口。
        """
        return any(t.is_alive() for t in self._active_turns.values())

    def get_active_turn_snapshot(self, session_name: str) -> Optional[Dict[str, Any]]:
        """获取指定会话当前进行中的回合快照（若无活跃回合返回 None）。"""
        if not session_name:
            return None
        with self._active_turn_lock:
            snap = self._active_turn_snapshots.get(session_name)
            if not snap:
                return None
            copied = dict(snap)
            copied["tool_calls"] = [dict(c) for c in snap.get("tool_calls", [])]
            return copied

    def get_all_active_turn_snapshots(self) -> Dict[str, Dict[str, Any]]:
        """获取所有会话当前进行中的回合快照字典。"""
        with self._active_turn_lock:
            res = {}
            for s, snap in self._active_turn_snapshots.items():
                copied = dict(snap)
                copied["tool_calls"] = [dict(c) for c in snap.get("tool_calls", [])]
                res[s] = copied
            return res

    def _record_session_last_error(self, session_name: str, message: str, model_name: str = "", model_id: str = ""):
        """记录会话最后一次终态错误，供多端与重载恢复。"""
        if not session_name:
            return
        payload = {
            "message": str(message),
            "timestamp": time.time(),
            "model_name": model_name or "",
            "model_id": model_id or "",
        }
        lock = getattr(self, "_session_last_errors_lock", None)
        if lock is not None:
            with lock:
                getattr(self, "_session_last_errors", {})[session_name] = payload
        else:
            getattr(self, "_session_last_errors", {})[session_name] = payload
        try:
            if hasattr(self, "_memory") and hasattr(self._memory, "set_session_last_error"):
                self._memory.set_session_last_error(session_name, payload)
        except Exception:
            logger.debug("持久化会话最后错误失败", exc_info=True)

    def _get_session_last_error(self, session_name: str) -> Optional[Dict[str, Any]]:
        """获取会话最后一次终态错误。"""
        if not session_name:
            return None
        lock = getattr(self, "_session_last_errors_lock", None)
        if lock is not None:
            with lock:
                res = getattr(self, "_session_last_errors", {}).get(session_name)
        else:
            res = getattr(self, "_session_last_errors", {}).get(session_name)
        if res is not None:
            return copy.deepcopy(res)
        try:
            if hasattr(self, "_memory") and hasattr(self._memory, "get_session_last_error"):
                return self._memory.get_session_last_error(session_name)
        except Exception:
            pass
        return None

    def _clear_session_last_error(self, session_name: str):
        """清除会话的最后一次终态错误。"""
        if not session_name:
            return
        lock = getattr(self, "_session_last_errors_lock", None)
        if lock is not None:
            with lock:
                getattr(self, "_session_last_errors", {}).pop(session_name, None)
        else:
            getattr(self, "_session_last_errors", {}).pop(session_name, None)
        try:
            if hasattr(self, "_memory") and hasattr(self._memory, "clear_session_last_error"):
                self._memory.clear_session_last_error(session_name)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 自绘标题栏窗口控制（frameless 模式）：最小化 / 最大化还原 / 关闭
    # ------------------------------------------------------------------
    def _send_sys_command(self, sc_code: int) -> bool:
        """原生系统命令（WM_SYSCOMMAND）：与系统菜单同路径，最小化/最大化
        动画、任务栏交互全部原生。需已找到 hwnd。"""
        hwnd = getattr(self, "_hwnd", 0)
        if not hwnd or sys.platform != "win32":
            return False
        import ctypes

        ctypes.windll.user32.SendMessageW(hwnd, 0x0112, sc_code, 0)
        return True

    def window_begin_drag(self) -> Dict[str, Any]:
        """标题栏拖拽：进入系统原生移动循环（SC_MOVE + HTCAPTION）。

        JS 逐帧调 move() 的程序化移动不触发 Aero Snap；SC_MOVE 是系统菜单
        "移动"同路径，进入 DefWindowProc 的模态移动循环，拖到屏幕边缘的
        Snap / 动画全部原生。调用会阻塞到用户松开鼠标（移动循环结束）。
        必须在 GUI 线程执行（Invoke），否则移动循环消息泵不工作。
        """
        try:
            hwnd = getattr(self, "_hwnd", 0)
            form = getattr(self, "_browser_form", None)
            if not hwnd or form is None or sys.platform != "win32":
                return {"ok": False, "error": "窗口未就绪"}
            done = {"ok": False}

            def _drag():
                try:
                    import ctypes

                    u = ctypes.windll.user32
                    # 权威状态守卫：前端 400ms 拖动定时器可能在左键已松开后
                    # 仍触发（DOM mouseup 不保证每次到达）。此处必须在 GUI
                    # 线程内、紧邻 ReleaseCapture/SendMessageW 之前读取真实
                    # 左键状态：已松开则拒绝过期请求，绝不进入无有效按键手势
                    # 的 SC_MOVE（否则 Windows 显示禁用光标）。
                    if not (u.GetAsyncKeyState(0x01) & 0x8000):
                        done["ok"] = False
                        done["error"] = "left_button_released"
                        logger.info("拒绝左键已释放的过期拖动请求: hwnd=%s", hwnd)
                        return
                    u.ReleaseCapture()
                    # SC_MOVE(0xF010)+HTCAPTION(2)：鼠标驱动的原生移动循环（窗口
                    # 跟随、松手结束）。该循环不触发 Aero Snap（caption 专有），
                    # 松手后按光标贴边情况补发 Win+方向键触发系统 Snap。
                    u.SendMessageW(hwnd, 0x0112, 0xF012, 0)
                    # 移动循环已结束（用户松手）：光标贴边 → 模拟系统 Snap 快捷键。
                    # 多显示器坐标不能用主屏宽度直接比较（见 _cursor_snap_vk），
                    # 统一交给该辅助函数按光标所在显示器边界判定。
                    vk = _cursor_snap_vk()
                    if vk:
                        KEYUP = 0x0002
                        u.keybd_event(0x5B, 0, 0, 0)      # LWIN 按下
                        u.keybd_event(vk, 0, 0, 0)
                        u.keybd_event(vk, 0, KEYUP, 0)
                        u.keybd_event(0x5B, 0, KEYUP, 0)  # LWIN 抬起
                    done["ok"] = True
                except Exception as error:  # noqa: BLE001
                    done["ok"] = False
                    done["error"] = str(error)

            from System.Windows import Forms as WinForms

            form.Invoke(WinForms.MethodInvoker(_drag))
            return {"ok": done["ok"], "error": done.get("error")}
        except Exception as error:  # noqa: BLE001
            logger.warning("标题栏拖拽启动失败: %s", error)
            return {"ok": False, "error": str(error)}

    def window_begin_resize(self, edge: str) -> Dict[str, Any]:
        """由页面透明边缘热区进入 Windows 原生八方向 resize 循环。"""
        resize_codes = {
            "left": 1,
            "right": 2,
            "top": 3,
            "top_left": 4,
            "top_right": 5,
            "bottom": 6,
            "bottom_left": 7,
            "bottom_right": 8,
        }
        code = resize_codes.get(str(edge))
        if code is None:
            return {"ok": False, "error": "无效的窗口缩放方向"}
        hwnd = getattr(self, "_hwnd", 0)
        form = getattr(self, "_browser_form", None)
        if not hwnd or form is None or sys.platform != "win32":
            return {"ok": False, "error": "窗口未就绪"}
        done = {"ok": False, "error": None}

        def _resize():
            try:
                from ctypes import wintypes

                user32 = ctypes.windll.user32
                user32.IsZoomed.argtypes = [wintypes.HWND]
                user32.IsZoomed.restype = wintypes.BOOL
                user32.ReleaseCapture.argtypes = []
                user32.ReleaseCapture.restype = wintypes.BOOL
                user32.SendMessageW.argtypes = [
                    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                ]
                user32.SendMessageW.restype = wintypes.LPARAM
                if user32.IsZoomed(hwnd):
                    done["error"] = "最大化窗口不能调整大小"
                    return
                user32.ReleaseCapture()
                user32.SendMessageW(hwnd, 0x0112, 0xF000 | code, 0)
                done["ok"] = True
            except Exception as error:  # noqa: BLE001
                done["error"] = str(error)

        try:
            from System.Windows import Forms as WinForms

            form.Invoke(WinForms.MethodInvoker(_resize))
            return done
        except Exception as error:  # noqa: BLE001
            logger.warning("窗口边缘缩放启动失败: %s", error)
            return {"ok": False, "error": str(error)}

    def _is_window_zoomed(self) -> bool:
        hwnd = getattr(self, "_hwnd", 0)
        if not hwnd or sys.platform != "win32":
            return False
        import ctypes

        return bool(ctypes.windll.user32.IsZoomed(hwnd))

    def window_minimize(self) -> Dict[str, Any]:
        try:
            # SC_MINIMIZE（0xF020）：原生路径带最小化到任务栏的动画
            if self._send_sys_command(0xF020):
                return {"ok": True}
            if self._window is not None:
                self._window.minimize()
            return {"ok": True}
        except Exception as error:  # noqa: BLE001
            logger.warning("窗口最小化失败: %s", error)
            return {"ok": False, "error": str(error)}

    def window_toggle_maximize(self) -> Dict[str, Any]:
        """最大化 / 还原（原生系统路径）。

        frameless 窗口带 WS_THICKFRAME 时原生 SC_MAXIMIZE 默认扩展到全屏
        （含任务栏）；core.frameless 的 WM_GETMINMAXINFO 处理已把最大尺寸
        钳制到当前显示器工作区（排除任务栏），因此这里直接走原生
        SC_MAXIMIZE/SC_RESTORE：动画、任务栏交互、多显示器/DPI 全部系统接管。
        最大化状态以 IsZoomed 为权威来源（不再维护伪最大化标志）。
        """
        try:
            if self._window is None:
                return {"ok": False, "error": "窗口未就绪"}
            if sys.platform != "win32":
                # 非 Windows 回退到 pywebview 原生路径
                maximized = bool(self._is_window_zoomed())
                if maximized:
                    self._window.restore()
                else:
                    self._window.maximize()
                self._frameless_maximized = not maximized
                return {"ok": True, "maximized": self._frameless_maximized}

            maximized = bool(self._is_window_zoomed())
            if maximized:
                if not self._send_sys_command(0xF120):  # SC_RESTORE
                    self._window.restore()
                self._frameless_maximized = False
            else:
                if not self._send_sys_command(0xF030):  # SC_MAXIMIZE
                    self._window.maximize()
                self._frameless_maximized = True

            # 同步最大化状态到前端（按钮渲染）
            self.emit_event("window_maximize_state", {"maximized": self._frameless_maximized})
            return {"ok": True, "maximized": self._frameless_maximized}
        except Exception as error:  # noqa: BLE001
            logger.warning("窗口最大化切换失败: %s", error)
            return {"ok": False, "error": str(error)}

    def window_native_maximize(self) -> Dict[str, Any]:
        """原生最大化/还原（SC_MAXIMIZE/SC_RESTORE），等价标题栏 HTCAPTION 双击。

        前端双击标题栏拖拽区时调用：与 HTCAPTION 双击完全同路径（系统命令），
        避免 beginDrag 的 SC_MOVE 移动循环吞掉双击序列导致无法最大化。
        语义与 window_toggle_maximize 一致，单独接口便于前端区分“按钮切换”
        与“双击原生最大化”两条路径。
        """
        return self.window_toggle_maximize()

    def window_close(self) -> Dict[str, Any]:
        """关闭（frameless 自绘按钮）：推送 close_requested 事件，由前端弹现代确认框；
        托盘退出、系统关机等其它关闭路径仍走原生 closing 流程兜底。"""
        try:
            if self._window is None:
                return {"ok": False, "error": "窗口未就绪"}
            self.emit_event("close_requested", {
                "has_running_work": self.has_active_turns(),
                "tray_available": self._tray_available(),
            })
            return {"ok": True}
        except Exception as error:  # noqa: BLE001
            logger.warning("关闭请求派发失败: %s", error)
            return {"ok": False, "error": str(error)}

    def window_confirm_close(self, choice: str) -> Dict[str, Any]:
        """前端确认框结果：exit（停止回合并退出）/ background（托盘驻留）/ cancel。"""
        try:
            if choice == "cancel":
                return {"ok": True}
            if choice == "background":
                self._window.hide()
                return {"ok": True}
            if choice == "exit":
                self._lifecycle_exit()
                return {"ok": True}
            return {"ok": False, "error": f"未知选择: {choice}"}
        except Exception as error:  # noqa: BLE001
            logger.warning("关闭确认执行失败: %s", error)
            return {"ok": False, "error": str(error)}

    def _tray_available(self) -> bool:
        """托盘是否就绪（由 ApplicationLifecycle 注入，未注入按不可用）。"""
        try:
            return bool(getattr(self, "_tray_ready_flag", lambda: False)())
        except Exception:
            return False

    def _lifecycle_exit(self) -> None:
        """走完整退出序列（取消回合 → drain → destroy）；无生命周期时直接 destroy。"""
        exit_fn = getattr(self, "_exit_application", None)
        if callable(exit_fn):
            exit_fn()
        else:
            self._window.destroy()

    def window_is_maximized(self) -> Dict[str, Any]:
        return {"ok": True, "maximized": bool(self._is_window_zoomed()) or bool(getattr(self, "_frameless_maximized", False))}

    def shutdown(self) -> None:
        """Idempotently stop GUI-owned work before the native window is destroyed."""
        with self._shutdown_lock:
            if self._shutting_down:
                return
            self._shutting_down = True

        # 星轨自动化：先停调度引擎（不再产生新触发），再走既有取消/收尾链
        try:
            self._automation_engine.stop()
        except Exception:
            logger.exception("退出时停止自动化调度引擎失败")

        # 注销服务句柄必须排在 stop() 之后：先停引擎确保不再产生新的触发/回调，
        # 再注销句柄，避免引擎线程仍持有服务句柄形成悬挂引用。包在 try/except
        # 内，注销异常不得中断后续收尾链。
        try:
            from core.automation import service as automation_service

            automation_service.unregister()
        except Exception:
            logger.exception("退出时注销自动化服务句柄失败")

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
            with self._pty_lock:
                pty_session = self._pty_session
                self._pty_session = None
            if pty_session is not None:
                pty_session.kill()
        except Exception:
            logger.exception("退出时终止内嵌交互式终端失败")

        # 调试适配器进程：与内嵌终端同口径，退出必须回收（否则用户机器上留下
        # 一个活着的调试器进程）
        try:
            self._shutdown_debug_session()
        except Exception:
            logger.exception("退出时终止调试适配器失败")

        # 内嵌浏览器：best-effort 释放原生 WebView2 控件并取消节流定时器。
        # 放在 JS 派发哨兵之前；shutdown 内部吞异常，失败不阻断退出链
        try:
            embedded = getattr(self, "_embedded_browser", None)
            if embedded is not None:
                embedded.shutdown()
        except Exception:
            logger.exception("退出时释放内嵌浏览器失败")

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

        try:
            if self._lan_server is not None:
                self._lan_server.stop()
        except Exception:
            logger.exception("退出时停止局域网远程控制服务失败")

        try:
            if getattr(self, "_feishu_service", None) is not None:
                self._feishu_service.stop()
        except Exception:
            logger.exception("退出时停止飞书通道服务失败")

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
        # frameless：WebView2 子窗口在页面加载时可能销毁重建，loaded 事件后重装
        if self._is_frameless():
            self._schedule_frameless_reinstall()

    def _schedule_frameless_reinstall(self):
        """延迟多次重装子窗口子类化（覆盖 WebView2 加载期间窗口销毁重建）。"""
        def _work():
            import time as _t
            for delay in (0.5, 2.0, 5.0):
                _t.sleep(delay)
                try:
                    self._enable_frameless_resize()
                except Exception:
                    logger.debug("frameless 重装子类化失败", exc_info=True)

        threading.Thread(target=_work, daemon=True, name="gui-frameless-reinstall").start()

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
    # 注：frameless 自绘标题栏模式下无原生标题栏，DWM 改色停用，仅保留窗口图标，
    # 并启用 WM_NCHITTEST 子类化以恢复边缘调整大小与 Aero Snap。
    # ------------------------------------------------------------------
    def _find_hwnd_worker(self):
        """按窗口标题轮询查找本进程顶层窗口的 HWND（原生窗口在 webview.start 后才创建）。
        冻结版启动慢（WebView2 初始化/首次解压），轮询预算放宽到 60 秒。"""
        if sys.platform != "win32":
            return
        try:
            title = getattr(self._window, "title", "") or ""
            frameless = bool(getattr(self._window, "frameless", False))
            for _ in range(300):  # 最多等 60 秒
                hwnd = self._find_own_hwnd(title)
                if hwnd:
                    self._hwnd = hwnd
                    # 先设图标：WM_SETICON 触发 DWM 重建非客户区
                    self._apply_window_icon()
                    if frameless:
                        # 无边框：启用边缘调整大小 + Aero Snap（GUI 线程内子类化）
                        self._enable_frameless_resize()
                        # 统一走标题栏应用入口：撤销 pywebview 延伸进客户区的
                        # 1px frame，并隐藏 WS_THICKFRAME 的 DWM 外框。
                        self._apply_titlebar_theme()
                    else:
                        time.sleep(0.05)
                        self._apply_titlebar_theme()
                        # WebView2 子窗口就绪、DPI 变化等时机也可能重建边框，延迟补两次
                        self._start_reapply_thread(name="gui-titlebar-reapply")
                    return
                time.sleep(0.2)
        except Exception:
            logger.debug("查找窗口句柄失败", exc_info=True)

    def _enable_frameless_resize(self):
        """frameless 窗口恢复原生行为：边缘调整大小、最小化/最大化动画
        （WM_NCHITTEST 双窗口子类化仅处理边缘；标题栏交互由前端 DOM 驱动）。"""
        try:
            import webview.platforms.winforms as wf

            from core.frameless import enable_native_window_behaviors

            # 轮询等待 BrowserForm 注册（pywebview 在 webview.start 后才创建）
            import time as _time

            uid = getattr(self._window, "uid", "master")
            for _ in range(60):
                form = wf.BrowserView.instances.get(uid)
                if form is not None and form.Handle is not None and form.Handle.ToInt32() != 0:
                    self._browser_form = form  # window_begin_drag 的 GUI 线程 Invoke 用
                    enable_native_window_behaviors(form)
                    return
                _time.sleep(0.2)
            logger.warning("frameless 原生窗口行为：BrowserForm 未就绪，未启用")
        except Exception:
            logger.warning("frameless 原生窗口行为初始化失败", exc_info=True)

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
            try:
                self._apply_titlebar_theme()
            except Exception:
                # DWM / HWND 在 loaded、restored 交界可能短暂不可写；保留后续
                # 重放机会，避免一次瞬态失败终止整条 55.5 秒恢复序列。
                logger.warning("延迟重放 frameless 窗口框架失败", exc_info=True)

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

    @staticmethod
    def _native_titlebar_write_result(ok: bool, color_capability: str) -> Dict[str, Any]:
        return {"ok": bool(ok), "color_capability": color_capability}

    @classmethod
    def _coerce_native_titlebar_write_result(cls, result: Any) -> Dict[str, Any]:
        """Accept legacy boolean test doubles while production returns capability data."""
        if isinstance(result, dict) and isinstance(result.get("ok"), bool):
            return result
        if isinstance(result, bool):
            capability = (
                _DWM_COLOR_CAPABILITY_SUPPORTED
                if result else _DWM_COLOR_CAPABILITY_UNKNOWN
            )
            return cls._native_titlebar_write_result(result, capability)
        return cls._native_titlebar_write_result(False, _DWM_COLOR_CAPABILITY_UNKNOWN)

    def _write_native_titlebar_dwm(self, native_titlebar: Dict[str, Any]) -> Dict[str, Any]:
        """Write all required DWM attributes; never mutate bridge theme state."""
        try:
            if not isinstance(native_titlebar, dict):
                raise ValueError("native_titlebar must be an object")
            mode = native_titlebar["mode"]
            if mode not in ("light", "dark"):
                raise ValueError("native_titlebar.mode must be light or dark")
            caption = _rgb_hex_to_colorref(native_titlebar["background"])
            text = _rgb_hex_to_colorref(native_titlebar["text"])
            border = _rgb_hex_to_colorref(native_titlebar["border"])

            if sys.platform != "win32":
                return self._native_titlebar_write_result(
                    True, _DWM_COLOR_CAPABILITY_NOT_APPLICABLE,
                )
            # Before HWND discovery there is no native surface. Commit validated
            # pending state; the existing reapply worker writes it after discovery.
            if not getattr(self, "_hwnd", 0):
                return self._native_titlebar_write_result(
                    True, _DWM_COLOR_CAPABILITY_PENDING,
                )

            dwm = ctypes.windll.dwmapi
            val = ctypes.c_int(1 if mode == "dark" else 0)
            immersive_applied = False
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
                if dwm.DwmSetWindowAttribute(
                    self._hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)
                ) == 0:
                    immersive_applied = True
                    break
            if not immersive_applied:
                logger.error("DWM 沉浸式标题栏属性 20/19 均应用失败")
                return self._native_titlebar_write_result(
                    False, _DWM_COLOR_CAPABILITY_UNKNOWN,
                )
            color_results = []
            for attr, color in ((35, caption), (36, text), (34, border)):
                cval = ctypes.c_int(color)
                result = dwm.DwmSetWindowAttribute(
                    self._hwnd, attr, ctypes.byref(cval), ctypes.sizeof(cval)
                )
                normalized = int(result) & 0xFFFFFFFF
                if result != 0 and normalized not in _DWM_UNSUPPORTED_HRESULTS:
                    logger.error("DWM 标题栏属性 %s 应用失败: HRESULT=0x%08X", attr, normalized)
                    return self._native_titlebar_write_result(
                        False, _DWM_COLOR_CAPABILITY_UNKNOWN,
                    )
                color_results.append(normalized)
            if all(result == 0 for result in color_results):
                return self._native_titlebar_write_result(
                    True, _DWM_COLOR_CAPABILITY_SUPPORTED,
                )
            if all(result in _DWM_UNSUPPORTED_HRESULTS for result in color_results):
                build = _windows_build_number()
                if build is None or build >= 22000:
                    logger.error(
                        "支持 DWM 自定义颜色的平台返回不支持 HRESULT: build=%s results=%s",
                        build, color_results,
                    )
                    return self._native_titlebar_write_result(
                        False, _DWM_COLOR_CAPABILITY_UNKNOWN,
                    )
                logger.warning("当前 Windows 不支持 DWM 自定义标题栏颜色，已仅应用明暗模式")
                return self._native_titlebar_write_result(
                    True, _DWM_COLOR_CAPABILITY_UNSUPPORTED,
                )
            logger.error("DWM 自定义标题栏颜色能力返回不一致: %s", color_results)
            return self._native_titlebar_write_result(
                False, _DWM_COLOR_CAPABILITY_UNKNOWN,
            )
        except Exception:
            logger.debug("设置标题栏主题失败", exc_info=True)
            return self._native_titlebar_write_result(
                False, _DWM_COLOR_CAPABILITY_UNKNOWN,
            )

    def _apply_native_titlebar_theme(self, native_titlebar: Dict[str, Any]) -> bool:
        """Transactionally apply DWM colors and commit memory only after success."""
        previous = copy.deepcopy(getattr(self, "_native_titlebar", None))
        previous_dark = bool(getattr(self, "_titlebar_dark", True))
        previous_capability = getattr(
            self, "_native_titlebar_color_capability", _DWM_COLOR_CAPABILITY_PENDING,
        )
        candidate = copy.deepcopy(native_titlebar)
        write_result = self._coerce_native_titlebar_write_result(
            self._write_native_titlebar_dwm(candidate),
        )
        if not write_result["ok"]:
            rollback_result = self._native_titlebar_write_result(
                True, previous_capability,
            )
            if previous is not None:
                rollback_result = self._coerce_native_titlebar_write_result(
                    self._write_native_titlebar_dwm(previous),
                )
            if not rollback_result["ok"]:
                logger.error("DWM 标题栏应用失败后的原主题回滚也失败")
            self._native_titlebar = previous
            self._titlebar_dark = previous_dark
            self._native_titlebar_color_capability = previous_capability
            return False
        self._native_titlebar = candidate
        self._titlebar_dark = candidate["mode"] == "dark"
        self._native_titlebar_color_capability = write_result["color_capability"]
        return True

    def _apply_titlebar_colors(self, native_titlebar: Dict[str, Any]) -> bool:
        """Compatibility name retained for the Task 3 bridge contract."""
        return self._apply_native_titlebar_theme(native_titlebar)

    def _is_frameless(self) -> bool:
        """当前窗口是否为 frameless 自绘标题栏模式（无原生非客户区）。
        裸实例（测试）或未 set_window 时按非 frameless 处理，走原 DWM 路径。"""
        window = getattr(self, "_window", None)
        return bool(getattr(window, "frameless", False)) if window is not None else False

    def _apply_frameless_titlebar_memory(self, native_titlebar: Dict[str, Any]) -> None:
        """frameless 下提交标题栏内存态，并确保 DWM 不绘制 THICKFRAME 外框。"""
        candidate = copy.deepcopy(native_titlebar)
        mode = candidate.get("mode")
        if mode not in ("light", "dark"):
            raise ThemeError("invalid_value", "native_titlebar.mode", "标题栏模式只能是 light 或 dark")
        hwnd = getattr(self, "_hwnd", 0)
        if hwnd:
            try:
                from core.frameless import suppress_frame_border

                suppressed = suppress_frame_border(hwnd)
            except Exception as error:
                raise ThemeError(
                    "native_titlebar_failed",
                    "native_titlebar.border",
                    "隐藏 frameless DWM 窗口外框失败",
                ) from error
            if suppressed is False:
                raise ThemeError(
                    "native_titlebar_failed",
                    "native_titlebar.border",
                    "隐藏 frameless DWM 窗口外框失败",
                )
        self._native_titlebar = candidate
        self._titlebar_dark = mode == "dark"
        self._native_titlebar_color_capability = _DWM_COLOR_CAPABILITY_NOT_APPLICABLE

    def _apply_titlebar_theme(self) -> bool:
        """Reapply the last resolved native titlebar after DWM rebuilds the frame."""
        native_titlebar = getattr(self, "_native_titlebar", None)
        if native_titlebar is None:
            return False
        if self._is_frameless():
            self._apply_frameless_titlebar_memory(native_titlebar)
            return True
        return self._apply_native_titlebar_theme(native_titlebar)

    def _native_titlebar_capability(self) -> str:
        return str(getattr(
            self,
            "_native_titlebar_color_capability",
            _DWM_COLOR_CAPABILITY_PENDING,
        ))

    def set_titlebar_theme(self, dark: bool) -> Dict[str, Any]:
        """前端主题切换时同步窗口标题栏明暗，避免深色内容配白色标题栏刺眼。"""
        def operation():
            theme_id = "builtin.dark" if bool(dark) else "builtin.light"
            theme = get_gui_theme_repository().get_theme(theme_id)
            if self._is_frameless():
                self._apply_frameless_titlebar_memory(theme["native_titlebar"])
            elif not self._apply_titlebar_colors(theme["native_titlebar"]):
                raise ThemeError("native_titlebar_failed", "native_titlebar", "原生标题栏颜色应用失败")
            return {
                "ok": True,
                "native_titlebar_color_capability": self._native_titlebar_capability(),
            }

        return self._run_gui_theme_operation("set_titlebar_theme", operation, "native_titlebar")

    def set_gui_theme(self, theme_light: bool) -> Dict[str, Any]:
        """One-release compatibility adapter for the old boolean theme API."""
        theme_id = "builtin.light" if bool(theme_light) else "builtin.dark"
        return self.set_active_gui_theme(theme_id)

    @staticmethod
    def _gui_theme_error(error: ThemeError) -> Dict[str, Any]:
        return {"ok": False, "error": error.as_dict()}

    def _run_gui_theme_operation(self, name: str, operation, path: str = "theme") -> Dict[str, Any]:
        """Keep every GUI-theme bridge call inside a structured PyWebView envelope."""
        try:
            return operation()
        except ThemeError as error:
            logger.warning("GUI 主题操作 %s 失败: %s", name, error.as_dict())
            return self._gui_theme_error(error)
        except (OSError, IOError) as error:
            logger.exception("GUI 主题操作 %s 遇到 I/O 错误", name)
            return self._gui_theme_error(ThemeError("theme_io_failed", path, str(error)))
        except Exception:
            logger.exception("GUI 主题操作 %s 发生未预期错误", name)
            return self._gui_theme_error(
                ThemeError("theme_operation_failed", path, "GUI 主题操作失败"),
            )

    @staticmethod
    def _dialog_path(result: Any) -> Optional[pathlib.Path]:
        if not result:
            return None
        if isinstance(result, (list, tuple)):
            if not result:
                return None
            result = result[0]
        return pathlib.Path(str(result))

    def _require_theme_window(self):
        if self._window is None:
            raise ThemeError("window_not_ready", "window", "窗口尚未就绪")
        return self._window

    def list_gui_themes(self) -> Dict[str, Any]:
        return self._run_gui_theme_operation(
            "list_gui_themes",
            lambda: {"ok": True, "themes": get_gui_theme_repository().list_themes()},
            "gui_themes",
        )

    def get_gui_theme(self, theme_id: str) -> Dict[str, Any]:
        return self._run_gui_theme_operation(
            "get_gui_theme",
            lambda: {"ok": True, "theme": get_gui_theme_repository().get_theme(theme_id)},
        )

    def get_active_gui_theme(self) -> Dict[str, Any]:
        def operation():
            return {
                "ok": True,
                "theme": get_gui_theme_repository().get_active(),
                "native_titlebar_color_capability": self._native_titlebar_capability(),
            }

        return self._run_gui_theme_operation(
            "get_active_gui_theme", operation, "gui_theme_id",
        )

    def save_gui_theme(self, theme: Dict[str, Any]) -> Dict[str, Any]:
        def operation():
            # get_gui_theme returns a full DTO with the derived read-only
            # ``builtin`` flag. The persisted manifest schema intentionally does
            # not contain that field.
            manifest = {key: value for key, value in theme.items() if key != "builtin"}
            saved = get_gui_theme_repository().save_theme(manifest)
            return {"ok": True, "theme": saved}

        return self._run_gui_theme_operation("save_gui_theme", operation)

    def set_active_gui_theme(self, theme_id: str) -> Dict[str, Any]:
        """Apply native colors first; persist gui_theme_id only after DWM accepts them."""
        def operation():
            repository = get_gui_theme_repository()
            theme = repository.get_theme(theme_id)
            previous_titlebar = copy.deepcopy(getattr(self, "_native_titlebar", None))
            previous_dark = bool(getattr(self, "_titlebar_dark", True))
            previous_capability = self._native_titlebar_capability()
            if self._is_frameless():
                # frameless 自绘标题栏：撤销 1px frame 延伸并隐藏 DWM 外框。
                self._apply_frameless_titlebar_memory(theme["native_titlebar"])
            elif not self._apply_titlebar_colors(theme["native_titlebar"]):
                raise ThemeError(
                    "native_titlebar_failed", "native_titlebar", "原生标题栏颜色应用失败",
                )
            try:
                active_theme = repository.set_active(theme_id)
            except Exception as persist_error:
                rollback_ok = True
                try:
                    if previous_titlebar is not None:
                        if self._is_frameless():
                            self._apply_frameless_titlebar_memory(previous_titlebar)
                        else:
                            rollback_result = self._coerce_native_titlebar_write_result(
                                self._write_native_titlebar_dwm(previous_titlebar),
                            )
                            rollback_ok = rollback_result["ok"]
                except Exception:
                    rollback_ok = False
                    logger.exception("活动主题持久化失败后的标题栏回滚发生异常")
                finally:
                    # Memory must always describe the authoritative persisted
                    # theme so a later frame reapply can heal native DWM state.
                    self._native_titlebar = previous_titlebar
                    self._titlebar_dark = previous_dark
                    self._native_titlebar_color_capability = previous_capability
                if not rollback_ok:
                    logger.error("活动主题持久化失败后，原生标题栏 DWM 回滚失败")
                    raise ThemeError(
                        "native_titlebar_rollback_failed",
                        "native_titlebar",
                        "活动主题持久化失败，且原生标题栏回滚失败",
                    ) from persist_error
                raise

            # Temporary compatibility mirror for one release. The repository's
            # gui_theme_id write above is authoritative.
            from config import save_config_value
            if not save_config_value("gui_theme_light", active_theme["appearance"] == "light"):
                logger.warning("写入兼容配置 gui_theme_light 失败")
            return {
                "ok": True,
                "theme": active_theme,
                "native_titlebar_color_capability": self._native_titlebar_capability(),
            }

        return self._run_gui_theme_operation("set_active_gui_theme", operation, "gui_theme_id")

    def copy_gui_theme(self, source_id: str, new_id: str, new_name: str) -> Dict[str, Any]:
        def operation():
            theme = get_gui_theme_repository().copy_theme(source_id, new_id, new_name)
            return {"ok": True, "theme": theme}

        return self._run_gui_theme_operation("copy_gui_theme", operation)

    def rename_gui_theme(self, theme_id: str, new_id: str, new_name: str) -> Dict[str, Any]:
        def operation():
            theme = get_gui_theme_repository().rename_theme(theme_id, new_id, new_name)
            return {"ok": True, "theme": theme}

        return self._run_gui_theme_operation("rename_gui_theme", operation)

    def delete_gui_theme(self, theme_id: str) -> Dict[str, Any]:
        def operation():
            get_gui_theme_repository().delete_theme(theme_id)
            return {"ok": True}

        return self._run_gui_theme_operation("delete_gui_theme", operation)

    def import_gui_theme(self) -> Dict[str, Any]:
        def operation():
            import webview

            result = self._require_theme_window().create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("星瑶Code GUI Theme (*.zip)",),
            )
            source = self._dialog_path(result)
            if source is None:
                raise ThemeError("dialog_cancelled", "source", "已取消导入")
            theme = get_gui_theme_repository().import_zip(source)
            return {"ok": True, "theme": theme}

        return self._run_gui_theme_operation("import_gui_theme", operation, "source")

    def export_gui_theme(self, theme_id: str) -> Dict[str, Any]:
        def operation():
            import webview

            result = self._require_theme_window().create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"{theme_id}.zip",
                file_types=("星瑶Code GUI Theme (*.zip)",),
            )
            destination = self._dialog_path(result)
            if destination is None:
                raise ThemeError("dialog_cancelled", "destination", "已取消导出")
            if destination.suffix.lower() != ".zip":
                destination = destination.with_suffix(".zip")
            exported = get_gui_theme_repository().export_zip(theme_id, destination)
            return {"ok": True, "path": str(exported)}

        return self._run_gui_theme_operation("export_gui_theme", operation, "destination")

    def import_gui_theme_image(self, theme_id: str, region: str) -> Dict[str, Any]:
        def operation():
            import webview

            result = self._require_theme_window().create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("Images (*.png;*.jpg;*.jpeg;*.webp)",),
            )
            source = self._dialog_path(result)
            if source is None:
                raise ThemeError("dialog_cancelled", "source", "已取消选择图片")
            asset_path = get_gui_theme_repository().import_image(theme_id, region, source)
            return {"ok": True, "asset_path": asset_path}

        return self._run_gui_theme_operation("import_gui_theme_image", operation, "source")

    def remove_gui_theme_image(self, theme_id: str, region: str) -> Dict[str, Any]:
        def operation():
            theme = get_gui_theme_repository().remove_image(theme_id, region)
            return {"ok": True, "theme": theme}

        return self._run_gui_theme_operation("remove_gui_theme_image", operation)

    def read_gui_theme_asset(self, theme_id: str, asset_path: str) -> Dict[str, Any]:
        def operation():
            mime_type, data = get_gui_theme_repository().read_asset(theme_id, asset_path)
            return {
                "ok": True,
                "mime": mime_type,
                "base64": base64.b64encode(data).decode("ascii"),
            }

        return self._run_gui_theme_operation("read_gui_theme_asset", operation, "asset_path")

    def get_gui_theme_asset(self, theme_id: str, asset_path: str) -> Dict[str, Any]:
        """Compatibility alias for the initially published Task 3 method name."""
        return self.read_gui_theme_asset(theme_id, asset_path)

    def preview_native_titlebar(self, native_titlebar: Dict[str, Any]) -> Dict[str, Any]:
        def operation():
            # frameless 自绘标题栏：无原生栏可改色，控制按钮明暗由前端按 mode 自绘。
            # 校验通过后直接提交内存态，返回 not_applicable。
            if self._is_frameless():
                self._apply_frameless_titlebar_memory(native_titlebar)
                return {
                    "ok": True,
                    "native_titlebar_color_capability": _DWM_COLOR_CAPABILITY_NOT_APPLICABLE,
                }
            if not self._apply_titlebar_colors(native_titlebar):
                raise ThemeError(
                    "native_titlebar_failed", "native_titlebar", "原生标题栏颜色应用失败",
                )
            return {
                "ok": True,
                "native_titlebar_color_capability": self._native_titlebar_capability(),
            }

        return self._run_gui_theme_operation(
            "preview_native_titlebar", operation, "native_titlebar",
        )

    def restore_native_titlebar(self) -> Dict[str, Any]:
        def operation():
            theme = get_gui_theme_repository().get_active()
            # frameless：同上，仅提交内存态，不做 DWM 调用
            if self._is_frameless():
                self._apply_frameless_titlebar_memory(theme["native_titlebar"])
                return {
                    "ok": True,
                    "theme": theme,
                    "native_titlebar_color_capability": _DWM_COLOR_CAPABILITY_NOT_APPLICABLE,
                }
            if not self._apply_titlebar_colors(theme["native_titlebar"]):
                raise ThemeError(
                    "native_titlebar_failed", "native_titlebar", "原生标题栏颜色应用失败",
                )
            return {
                "ok": True,
                "theme": theme,
                "native_titlebar_color_capability": self._native_titlebar_capability(),
            }

        return self._run_gui_theme_operation(
            "restore_native_titlebar", operation, "native_titlebar",
        )

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

    def check_update(self, manual: bool = False) -> Dict[str, Any]:
        """检查 Gitee Releases 是否有新版本。

        在既有返回结构上新增 cached_installer（纯加法）：当更新缓存目录里
        已有目标版本的安装包（按 Release 发布的官方 SHA256 / 大小校验通过）
        时给出其绝对路径，前端据此直接进入"已就绪、可安装"，无需重新下载。
        缓存探测属增强能力，任何异常一律兜底为 ""，绝不影响更新检查本身。
        """
        try:
            info = fetch_latest_release()
            info["cached_installer"] = self._probe_cached_installer(info)
            return info
        except Exception as e:
            logger.exception("检查更新失败")
            return {
                "ok": False,
                "has_update": False,
                "cached_installer": "",
                "msg": f"检查更新失败: {e}",
            }

    @staticmethod
    def _probe_cached_installer(info: Dict[str, Any]) -> str:
        """探测可复用的缓存安装包，返回绝对路径；不可复用时返回空串。

        仅在"确实有更新"时才做探测：无更新时目标版本等于当前运行版本，
        缓存里的包只可能是启动清理未及处理的旧包，复用无意义，同时也能
        避免每次静默检查都去哈希几百 MB 的安装包。
        """
        try:
            if not isinstance(info, dict) or not info.get("has_update"):
                return ""
            return find_cached_installer(
                str(info.get("latest_version") or ""),
                str(info.get("sha256") or ""),
                int(info.get("asset_size") or 0),
            ) or ""
        except Exception:
            logger.warning("探测缓存安装包失败，按未命中处理", exc_info=True)
            return ""

    def download_update(
        self,
        download_url: str,
        target_version: str,
        part_urls: Optional[List[str]] = None,
        sha256: str = "",
        total_size: int = 0,
    ) -> Dict[str, Any]:
        """开启后台线程流式下载安装包，并通过 JS 事件实时广播进度。

        分卷模式（part_urls 非空）：Gitee 单附件 100MB 上限导致主安装包
        按 .partN 分卷发布，下载器顺序下载拼接还原 exe，并用 Release
        描述里发布的 sha256 强制校验，不匹配即拒绝安装。
        """
        try:
            # 分卷列表归一化：pywebview 侧传 null/标量时兜底成空列表
            clean_parts = [str(u) for u in (part_urls or []) if str(u).strip()]
            clean_sha = str(sha256 or "").strip().lower()

            def _on_progress(data):
                self.emit_event("update_download_progress", data)

            def _on_complete(dest_path):
                self.emit_event("update_download_complete", {"installer_path": dest_path})

            def _on_error(err_msg):
                self.emit_event("update_download_error", {"message": err_msg})

            started = updater_downloader.start_download(
                download_url=download_url,
                target_version=target_version,
                on_progress=_on_progress,
                on_complete=_on_complete,
                on_error=_on_error,
                part_urls=clean_parts,
                expected_sha256=clean_sha,
                total_size_hint=int(total_size or 0),
            )
            return {"ok": started, "msg": "下载已开始" if started else "已有下载任务在进行中"}
        except Exception as e:
            logger.exception("启动下载异常")
            return {"ok": False, "msg": str(e)}

    def cancel_download_update(self) -> Dict[str, Any]:
        """取消当前安装包下载。"""
        try:
            updater_downloader.cancel()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def has_active_tasks(self) -> Dict[str, Any]:
        """查询当前是否有对话/后台任务正在执行（安装门禁判定）。"""
        try:
            has_turns = self.has_active_turns()
            active_tasks = list_active_tasks()
            busy = has_turns or bool(active_tasks)
            return {
                "ok": True,
                "busy": busy,
                "has_turns": has_turns,
                "active_task_count": len(active_tasks),
            }
        except Exception as e:
            # 失败关闭：查询异常时按"有任务"处理，避免安装中断真实运行中的任务
            logger.exception("查询活动任务失败")
            return {"ok": False, "busy": True, "msg": str(e)}

    def launch_installer(self, file_path: str) -> Dict[str, Any]:
        """唤起外部安装向导并安全退出当前程序。

        安装包清理由安装向导（安装成功时自清理）与启动时的
        cleanup_old_installers（未安装成功的残留）负责，
        这里调用的是 core.updater 模块级函数 launch_installer。
        """
        try:
            ok = launch_installer(file_path)
            if ok:
                threading.Thread(target=self._delayed_exit, daemon=True).start()
                return {"ok": True}
            return {"ok": False, "msg": "唤起安装包失败"}
        except Exception as e:
            logger.exception("唤起安装包异常")
            return {"ok": False, "msg": str(e)}

    def _delayed_exit(self):
        """延迟退出，确保 JS Promise 正常返回。"""
        time.sleep(0.5)
        try:
            self.shutdown()
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass
        finally:
            os._exit(0)

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
        self._queue_js_payload(payload)
        # 广播到局域网手机客户端（broadcast 内部已吞异常，不影响桌面 JS 派发）；
        # event_type 供 LAN 层判定断线补发白名单（丢事件类别不参与补发）
        lan = getattr(self, "_lan_server", None)
        if lan is not None:
            lan.broadcast(payload, event_type=event_type)
        # 转发给飞书通道（未配置/未启动时为 None，零开销；on_event 内部已吞异常，
        # 绝不影响桌面 JS 派发）
        feishu = getattr(self, "_feishu_service", None)
        if feishu is not None:
            feishu.on_event(event_type, data)

    def _queue_js_payload(self, payload: str) -> None:
        """唯一入队点：保证派发线程存在后 put_nowait；队列满丢弃新事件并计数。"""
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
            _js_t0 = time.monotonic()
            try:
                self._window.evaluate_js(
                    f"window.onBridgeEvent && window.onBridgeEvent({payload})"
                )
            except Exception as e:
                # WebView 销毁/跳转中 evaluate_js 会抛；事件丢掉即可，绝不带出线程。
                # 但渲染进程繁忙时的投递失败是「流式尾部丢失」的直接原因，必须可诊断：
                # 前 3 次与每 100 次记 warning，其余保持 debug 防刷屏。
                self._js_eval_failed = getattr(self, "_js_eval_failed", 0) + 1
                if self._js_eval_failed <= 3 or self._js_eval_failed % 100 == 0:
                    logger.warning(
                        "evaluate_js 投递事件失败（累计 %d 次，该事件已丢失）: %s",
                        self._js_eval_failed, e,
                    )
                else:
                    logger.debug("evaluate_js 失败: %s", e)
            else:
                # 渲染进程繁忙时 evaluate_js 会阻塞在 IPC 上：单条投递变慢会让排
                # 在队尾的 turn_finish 迟迟到不了前端（表现为「输出完了仍显示生成中」）。
                # 慢投递限频告警，附带事件类型与队列积压量，便于直接定位。
                _js_elapsed = time.monotonic() - _js_t0
                if _js_elapsed >= 0.5:
                    _js_now = time.monotonic()
                    if _js_now - getattr(self, "_js_slow_warned_at", 0.0) >= 5.0:
                        self._js_slow_warned_at = _js_now
                        try:
                            _js_etype = json.loads(payload).get("type", "?")
                        except Exception:
                            _js_etype = "?"
                        logger.warning(
                            "JS 事件投递缓慢：单条 %.0fms，队列剩余 %d 条，事件=%s",
                            _js_elapsed * 1000.0,
                            self._js_queue.qsize(),
                            _js_etype,
                        )

    def drain_js_queue(self):
        """测试 seam：排空 _js_queue 并返回最后一条已解析事件（空队列返回 None）。

        生产路径由 gui-js-dispatcher 单消费者线程消费，本方法仅供测试断言
        事件桥投递内容，不在运行时调用。
        """
        payload = None
        while True:
            try:
                raw = self._js_queue.get_nowait()
            except queue.Empty:
                break
            if raw is None:
                continue  # 关闭哨兵不是事件
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw
        return payload

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
        if session_name:
            with self._active_turn_lock:
                snap = self._active_turn_snapshots.get(session_name)
                if snap is not None:
                    if kind == "thinking_delta":
                        snap["thinking_text"] = snap.get("thinking_text", "") + text
                    else:
                        snap["streaming_text"] = snap.get("streaming_text", "") + text
        if swapped is not None:
            swapped_text = swapped[1]
            if swapped[0] == "token_delta":
                # 思考流切换为正文的边界：旧缓冲整体废弃，无后续分片可拼，直接整段清理
                swapped_text = sanitize_protocol_text(swapped_text)
            if swapped_text:
                self._emit_raw(swapped[0], {"text": swapped_text, "session_name": session_name})
        self._ensure_delta_flusher()
        if flush_now:
            self._flush_deltas()

    def _flush_deltas(self, final: bool = False):
        """冲刷 delta 缓冲。final=True 时不暂扣尾部（回合收尾，无后续分片）。"""
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
                if kind == "token_delta":
                    if final:
                        text = sanitize_protocol_text(text)
                    else:
                        # 占位符可能横跨两次冲刷，疑似前缀的尾部暂扣拼齐后再判定
                        text, rest = sanitize_stream_text(text)
                        if rest:
                            buf.append(rest)
                if text:
                    pending.append((kind, text, session_name))
            self._delta_last_flush = time.monotonic()
        for kind, text, session_name in pending:
            self._emit_raw(kind, {"text": text, "session_name": session_name})

    def _finalize_session_deltas(self, session_name: str) -> None:
        """回合收尾：先冲刷再清理该会话的 delta 缓冲。

        顺序敏感：回合末尾最后一块文本可能还滞留在缓冲里（距上次冲刷不足
        60ms、冲刷线程还没轮到），直接 pop 会把尾部静默丢弃——前端消息靠
        delta 累加渲染，这块文本就永久缺失（「回复尾部缺一段」的根因）。
        finish_once 的 turn_finish 虽会触发冲刷，但那之前缓冲若已被 pop，
        冲刷的就是个空。任何异常吞掉，绝不阻断回合收尾。
        """
        try:
            self._flush_deltas(final=True)
        except Exception:
            logger.debug("回合收尾冲刷流式缓冲失败", exc_info=True)
        try:
            with self._delta_lock:
                self._delta_buffers.pop(session_name, None)
                self._delta_kinds.pop(session_name, None)
        except Exception:
            logger.debug("回合收尾清理流式缓冲失败", exc_info=True)

    def _emit_message_final(self, session_name: str, after_msg_id: Optional[int] = None) -> None:
        """补发最后一条助手消息的全文（message_final 事件），供前端自愈校正。

        前端气泡内容完全靠 token_delta 累加，delta 在 evaluate_js 投递环节丢失
        后没有任何校正手段（表现为「回复尾部缺一段」）。回合结束时从数据库读取
        已落库的全文补发，前端仅在全文更长时覆盖，正常路径是无害 no-op。
        任何异常都吞掉：本事件是兜底增强，绝不能阻断 turn_finish。

        after_msg_id 是回合边界（本回合开始前会话的最后一条消息 id）：
        - None（默认）：不过滤，取全库最后一条 assistant 消息，行为与历史完全
          一致（既有调用点与测试依赖此语义）。
        - int：只在 _msg_id 大于该边界的消息里反向找最后一条 assistant 消息。
          这个坑必须堵住——回合异常/取消时 core/llm.rollback_turn_messages()
          回滚了本轮内存消息，本轮若尚未落库任何助手消息，不过滤就会取到
          **上一轮**的助手全文；前端「全文更长就覆盖」（appStore.ts）会把上一轮
          结果灌进本轮气泡。过滤后若没有助手消息则不发事件（与原有
          final_text/final_reasoning/final_media 全空则不发的事件语义一致）。
        """
        try:
            if after_msg_id is None:
                history = self._memory.get_history(session_name)
            else:
                history = [
                    msg
                    for msg in self._memory.get_history_with_ids(session_name)
                    if int(msg.get("_msg_id") or 0) > after_msg_id
                ]
            final_text = ""
            final_reasoning = ""
            final_media: list = []
            for msg in reversed(history):
                if msg.get("role") == "assistant":
                    content = msg.get("content")
                    reasoning = msg.get("reasoning_content")
                    final_text = content if isinstance(content, str) else ""
                    final_reasoning = reasoning if isinstance(reasoning, str) else ""
                    # 媒体清单随全文补发：流式气泡的媒体画廊在回合收尾统一补挂
                    #（独立于文本自愈的长度门控，media 非空即携带）
                    final_media = self._media_items_for_frontend(msg.get("_display"))
                    break
            # 数据库旧消息可能残留上游网关的空正文占位符，补发前精确清理
            final_text = sanitize_protocol_text(final_text)
            if final_text or final_reasoning or final_media:
                payload = {
                    "session_name": session_name,
                    "content": final_text,
                    "reasoning_content": final_reasoning,
                }
                if final_media:
                    # 无媒体时保持旧事件形状（历史测试断言精确载荷），
                    # 有媒体才补 media 键，前端非空即挂
                    payload["media"] = final_media
                self.emit_event("message_final", payload)
        except Exception:
            logger.debug("补发 message_final 失败", exc_info=True)

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
    def _run_media_migration_once(self) -> None:
        """历史消息 media_v1 清单迁移（幂等，失败只记日志，绝不影响启动）。"""
        try:
            from core.media_migration import run_media_migration_once
            run_media_migration_once(self._memory)
        except Exception:
            logger.debug("媒体清单迁移执行异常", exc_info=True)

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

    def _media_items_for_frontend(self, display) -> list:
        """把 _display.media_v1 清单转成前端可用的 media 列表。

        非 error 项补拼受控媒体服务的带令牌 URL；服务不可用降级为
        service_unavailable 错误项。任何异常返回空列表（媒体是增强能力，
        绝不能阻断消息加载）。
        """
        try:
            items = ((display or {}).get("media_v1") or {}).get("items") or []
            if not items:
                return []
            out = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                entry = dict(item)
                if item.get("kind") == "error":
                    entry.setdefault(
                        "error_label",
                        MEDIA_DISPLAY_ERROR_LABELS.get(item.get("error_code") or "", "媒体不可用"),
                    )
                else:
                    url = media_server.public_url(item.get("digest") or "", item.get("ext") or "")
                    if not url:
                        entry["kind"] = "error"
                        entry["error_code"] = "service_unavailable"
                        entry["error_label"] = MEDIA_DISPLAY_ERROR_LABELS["service_unavailable"]
                    else:
                        entry["url"] = url
                out.append(entry)
            return out
        except Exception:
            logger.debug("组装前端媒体清单失败", exc_info=True)
            return []

    def save_media_image(self, url: str, suggested_name: str = "") -> Dict[str, Any]:
        """把消息里的媒体图片另存到用户选择的位置（原生"另存为"对话框）。

        只接受本进程媒体服务签发的 URL：反解回仓库 Blob 后原样复制字节，
        不重新编码，保证另存结果与原图完全一致。用户取消不算错误，返回
        cancelled 标记供前端静默处理。
        """
        try:
            source = media_server.local_path_for_url(url or "")
        except Exception:
            logger.debug("媒体 URL 反解失败", exc_info=True)
            source = None
        if not source:
            return {"ok": False, "msg": "该图片不在本地媒体仓库中，无法另存"}
        if self._window is None:
            return {"ok": False, "msg": "窗口尚未就绪，无法打开保存对话框"}

        ext = os.path.splitext(source)[1].lower() or ".png"
        try:
            import webview

            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=_media_save_filename(suggested_name, ext),
                file_types=("图片文件 (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.avif)",),
            )
            destination = self._dialog_path(result)
            if destination is None:
                return {"ok": False, "cancelled": True, "msg": "已取消保存"}
            shutil.copyfile(source, destination)
            return {"ok": True, "path": str(destination)}
        except (OSError, IOError) as error:
            logger.warning("另存图片失败: %s", error)
            return {"ok": False, "msg": f"保存失败：{error}"}
        except Exception:
            logger.exception("另存图片发生未预期错误")
            return {"ok": False, "msg": "保存失败，请重试"}

    def _load_session_messages(
        self,
        session_name: str,
        before_id: Optional[int] = None,
        limit: int = HISTORY_PAGE_SIZE,
    ) -> Tuple[List[Dict[str, Any]], bool, Optional[int]]:
        """分页读取会话历史并转成前端气泡结构（与 switch_session 同一套映射）。

        返回 (msgs, has_more, oldest_id)：oldest_id 取未过滤的本页最旧 DB 行
        _msg_id 作为翻页游标（role 过滤会丢 system 等消息，取过滤后首条会漏翻）；
        会话级 last_error 附加仅首页（before_id=None）执行，翻页页不携带。
        """
        msgs: List[Dict[str, Any]] = []
        has_more = False
        oldest_id: Optional[int] = None
        # 电脑操控观察跟进消息识别（展示层过滤，见循环内说明）
        from core.computer.provider_generic import is_observation_protocol_message

        try:
            # 按回合撤销：消息 → 回合关联（纯界面元数据，Provider 请求不读取）
            try:
                turn_links = self._memory.load_session_turn_links(session_name)
            except Exception:
                turn_links = {}
            rows, has_more = self._memory.get_history_paged(
                session_name, before_id=before_id, limit=limit
            )
            if rows:
                oldest_id = rows[0].get("_msg_id")
            for msg in rows:
                role = msg.get("role")
                if role in ("user", "assistant", "tool"):
                    content = msg.get("content") or ""
                    if role == "user":
                        # 电脑操控观察跟进消息（设计 19.3）：系统发给模型的观察提示
                        # 协议消息，不是用户发送的。展示层一律跳过——气泡与红点
                        # 导航都按 user 消息渲染，不过滤就会冒充用户发言；
                        # 模型上下文（get_history）不受影响。_parse_message_rows
                        # 会把持久化 JSON 的 _computer_v1 键提升到行顶层。
                        if is_observation_protocol_message(msg):
                            continue
                        # 后台任务回报 / 护栏催促 / hook 追加上下文等系统注入的
                        # user 消息：不是用户发送的内容，气泡一律不渲染（与红点
                        # 导航同一裁决）；模型上下文（get_history）不受影响。
                        if is_system_injected_user_text(content):
                            continue
                        # 历史回放：剥离当轮注入的「当前运行上下文 / 当前时间」后缀，
                        # 那部分是给模型看的运行快照，不该显示在历史气泡里
                        content = strip_context_suffix(content)
                    elif role == "assistant":
                        # 数据库旧消息可能残留上游网关的空正文占位符，回放前精确清理
                        content = sanitize_protocol_text(content)
                    link = turn_links.get(msg.get("_msg_id"))
                    msgs.append({
                        "role": role,
                        "_msg_id": msg.get("_msg_id"),
                        "content": content,
                        "reasoning_content": msg.get("reasoning_content") or "",
                        "tool_calls": msg.get("tool_calls") or [],
                        "name": msg.get("name") or "",
                        "tool_call_id": msg.get("tool_call_id") or "",
                        # 透传生成该消息时所用模型的显示元数据（旧消息无 _display 时为空串）
                        "model_id": (msg.get("_display") or {}).get("model_id") or "",
                        "model_name": (msg.get("_display") or {}).get("model_name") or "",
                        "metrics": (msg.get("_display") or {}).get("metrics") or None,
                        "error_note": (msg.get("_display") or {}).get("error_note") or "",
                        "execution_state": (msg.get("_display") or {}).get("execution_state") or "",
                        # media_v1 媒体清单（已拼好带令牌 URL）；旧消息为空列表
                        "media": self._media_items_for_frontend(msg.get("_display")),
                        # 权威回合归属（旧消息无关联时为空串，前端回退启发式分组）
                        "turn_id": (link or {}).get("turn_id") or "",
                        "position_kind": (link or {}).get("position_kind") or "",
                    })
            if before_id is None:
                err_info = self._get_session_last_error(session_name)
                if err_info and err_info.get("message"):
                    err_msg = str(err_info["message"])
                    if msgs and msgs[-1].get("role") == "assistant":
                        msgs[-1]["error_note"] = err_msg
                        msgs[-1]["execution_state"] = "error"
                        if err_info.get("model_name") and not msgs[-1].get("model_name"):
                            msgs[-1]["model_name"] = err_info["model_name"]
                    elif msgs and msgs[-1].get("role") == "user":
                        msgs.append({
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "",
                            "tool_calls": [],
                            "execution_state": "error",
                            "error_note": err_msg,
                            "model_name": err_info.get("model_name", ""),
                            "model_id": err_info.get("model_id", ""),
                            "metrics": None,
                            "media": [],
                        })
                    elif not msgs:
                        msgs.append({
                            "role": "error",
                            "content": err_msg,
                        })
        except Exception:
            logger.debug("加载会话消息失败", exc_info=True)
        return msgs, has_more, oldest_id

    def load_older_messages(self, session_name: str, before_id: Any) -> Dict[str, Any]:
        """向上翻页加载更早历史（前端滚动到顶触发）。

        返回 {ok, messages, has_more, oldest_id}；before_id 非正数视为已到最早
        （空页 has_more=False），无法解析或会话不存在视为请求错误（ok=False）。
        """
        try:
            before_id = int(before_id)
        except (TypeError, ValueError):
            return {"ok": False, "messages": [], "has_more": False, "oldest_id": None}
        if before_id <= 0:
            return {"ok": True, "messages": [], "has_more": False, "oldest_id": None}
        try:
            names = {
                s.get("name")
                for s in self._memory.get_all_sessions(workdir=None, include_empty=True)
            }
        except Exception:
            names = set()
        if session_name not in names:
            return {"ok": False, "messages": [], "has_more": False, "oldest_id": None}
        msgs, has_more, oldest_id = self._load_session_messages(
            session_name, before_id=before_id
        )
        return {"ok": True, "messages": msgs, "has_more": has_more, "oldest_id": oldest_id}

    # 红点导航预览上限：与前端 MessageNavRail 的悬停截断口径一致
    _NAV_PREVIEW_MAX = 100

    def get_session_nav_index(self, session_name: str) -> Dict[str, Any]:
        """红点导航全量索引：该会话全部 user 消息的 {id, preview}（正序、不分页）。

        只收录用户真实发送的内容：电脑操控观察跟进消息与系统注入的提示词
        （后台任务回报 / 护栏催促 / hook 追加上下文）不产生红点。
        preview 剥离运行上下文后缀、折叠空白、截断 100 字；纯图片消息给占位。
        会话不存在/无消息返回空列表（ok=True），前端无差别渲染零红点。
        """
        try:
            raw_items = self._memory.get_session_user_nav_items(session_name)
            items = []
            for it in raw_items:
                # 电脑操控观察跟进消息不是用户发送的：红点导航一并跳过
                # （与 _load_session_messages 的气泡过滤同一裁决）
                if it.get("is_observation"):
                    continue
                # 后台任务回报 / 护栏催促 / hook 追加上下文等系统注入的 user
                # 消息同样不是用户发送的：不产生红点（与气泡过滤同一裁决）
                if is_system_injected_user_text(str(it.get("text") or "")):
                    continue
                text = strip_context_suffix(str(it.get("text") or ""))
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    text = "[图片]"
                if len(text) > self._NAV_PREVIEW_MAX:
                    text = text[: self._NAV_PREVIEW_MAX] + "…"
                items.append({"id": it["id"], "preview": text})
            return {"ok": True, "items": items, "total": len(items)}
        except Exception as e:
            logger.exception("get_session_nav_index 失败")
            return {"ok": False, "items": [], "total": 0, "msg": str(e)}

    def get_initial_state(self) -> Dict[str, Any]:
        # 启动诊断：前端能调到本方法，说明 JS→Python 桥已打通。
        # 若启动卡死时日志里没有这条，说明调用根本没到 Python（桥消息丢失），
        # 而非本方法内部卡住——据此区分"前端没调进来"与"卡在入口"。
        logger.info("get_initial_state 入口被调用（前端桥已打通）")
        # 前端桥已打通：后台惰性启动局域网手机远程控制服务（不阻塞初始化）
        try:
            threading.Thread(
                target=self._ensure_lan_server,
                daemon=True,
                name="lan-remote-startup",
            ).start()
        except Exception:
            logger.debug("局域网远程控制服务启动线程创建失败", exc_info=True)
        # 飞书通道：未配置时 _ensure_feishu_service 直接返回 None，不做任何事
        try:
            threading.Thread(
                target=self._ensure_feishu_service,
                daemon=True,
                name="feishu-startup",
            ).start()
        except Exception:
            logger.debug("飞书通道服务启动线程创建失败", exc_info=True)
        # 历史消息媒体清单迁移：一次性、幂等，后台执行不阻塞初始化
        try:
            threading.Thread(
                target=self._run_media_migration_once,
                daemon=True,
                name="media-migration",
            ).start()
        except Exception:
            logger.debug("媒体清单迁移线程创建失败", exc_info=True)
        gui_theme_state = _load_initial_gui_theme_state()
        active_gui_theme = gui_theme_state["gui_theme"]
        gui_themes = gui_theme_state["gui_themes"]
        gui_theme_error = gui_theme_state["gui_theme_error"]
        gui_theme_list_error = gui_theme_state["gui_theme_list_error"]
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
            # 技能扫描同样改为异步：core.skills.scan_all_skills 在冷盘首次扫描
            # 曾观测到 25s+（大技能目录/杀软扫描），同步调用会让"窗口已出现但
            # 界面一直转圈"。先返回缓存，扫完由 skills_loaded 事件推送刷新技能页。
            threading.Thread(
                target=self._async_build_skill_list,
                daemon=True,
                name="gui-skills-loader",
            ).start()
            initial_msgs, init_has_more, init_oldest_id = \
                self._load_session_messages(self._current_session)
            return {
                "models": models,
                "providers": providers,
                "active_model_id": str(effective_model_id),
                "active_model_name": str(active_model.get("name") or "未激活模型"),
                "reasoning": _reasoning_snapshot_for_model(active_model),
                # 副模型配置：启动时同步给前端，避免设置页显示"无/全勾选"假象
                "secondary_model_id": str(config_data.get("secondary_model_id") or ""),
                "secondary_model_roles": get_secondary_roles(),
                # 当前会话生效的权限档位（会话绑定优先，回退全局默认）
                "permission_mode": self._effective_permission_mode(self._current_session),
                # 沙盒开关（是否允许访问工作目录外文件）：与 CLI /sandbox 共享同一配置键
                "allow_outside_workspace": bool(config_data.get("allow_outside_workspace", False)),
                "system_settings": self.get_system_settings(),
                "persona_settings": self.get_persona_settings(),
                # 免责条款同意状态：前端据此判断是否需要弹"首次使用确认"框
                "disclaimer": self.get_disclaimer_consent(),
                "sessions": self.get_sessions(),
                # 只返回上一次后台扫描的缓存（无缓存时为 []），不再同步扫盘：
                # 前端拿到空数组后由 skills_loaded 事件补齐，见 _async_build_skill_list
                "skills": list(getattr(self, "_skills_cache", None) or []),
                "mcps": self._build_mcp_list(),
                "plugins": self.get_plugins(),
                "workdir": workdir,
                "is_global": is_global,
                "file_tree": file_tree,
                "current_session": self._current_session,
                "messages": initial_msgs,
                # 历史分页元信息：前端据此判断是否还能向前翻页及翻页游标
                "history_has_more": init_has_more,
                "history_oldest_id": init_oldest_id,
                # M23：当前有活跃回合的会话名列表。窗口重载/崩溃后前端重建时
                # 据此恢复 running 态或解锁输入框（终态事件已在重载期间丢失）
                "active_sessions": [
                    name for name, t in getattr(self, "_active_turns", {}).items()
                    if t.is_alive()
                ],
                # 活跃回合快照：供手机扫码连接或重载时恢复正在执行的思考、流式文本与工具状态
                "active_turn_snapshots": self.get_all_active_turn_snapshots(),
                "active_turn_snapshot": self.get_active_turn_snapshot(self._current_session),
                # 排队消息随初始状态返回：重载期间 message_queue 事件丢失，
                # 前端据此恢复各会话的排队条目展示
                "message_queues": {
                    name: self._message_queue_snapshot(name)
                    for name in list(getattr(self, "_message_queues", {}).keys())
                },
                "token_usage": self.get_token_usage(),
                # 旧前端的一代兼容字段；必须由权威 gui_theme_id 解析结果派生，
                # 不能反过来让可能陈旧的 gui_theme_light 覆盖活动主题。
                "theme_light": (
                    active_gui_theme["appearance"] == "light"
                    if active_gui_theme is not None
                    else bool(config_data.get("gui_theme_light", False))
                ),
                "gui_theme": active_gui_theme,
                "gui_themes": gui_themes,
                "gui_theme_error": gui_theme_error,
                "gui_theme_list_error": gui_theme_list_error,
                "native_titlebar_color_capability": self._native_titlebar_capability(),
                "version": str(YCODE_VERSION),
            }
        except Exception as e:
            logger.exception("get_initial_state 失败")
            return {
                "models": [], "providers": [], "active_model_id": "",
                "active_model_name": "未激活模型", "permission_mode": "confirm",
                "reasoning": _reasoning_snapshot_for_model(None),
                "sessions": [], "skills": [], "mcps": [],
                "workdir": "", "current_session": self._current_session,
                "active_sessions": [],
                "gui_theme": active_gui_theme,
                "gui_themes": gui_themes,
                "gui_theme_error": gui_theme_error,
                "gui_theme_list_error": gui_theme_list_error,
                "native_titlebar_color_capability": self._native_titlebar_capability(),
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

    def _suggest_scan_root(self) -> str:
        """@ 补全的扫描根目录：当前会话登记的 workdir，GLOBAL/无效时兜底进程 cwd。

        不能直接 os.walk(".")：GUI 多会话架构已用线程本地 workdir 绑定替代
        进程级 os.chdir，进程 cwd 永远停在启动目录，walk(".") 会列出启动目录
        的文件而非当前项目文件夹的文件。
        """
        try:
            wdir = self._session_workdir(self._current_session)
        except Exception:
            wdir = ""
        if wdir and wdir != "GLOBAL" and os.path.isdir(wdir):
            return _resolve_real_path(wdir)
        return _resolve_real_path(os.getcwd())

    def _list_workspace_files_for_suggest(self) -> List[str]:
        """递归扫描当前会话工作区文件（/ 分隔相对路径），TTL 缓存避免输入时反复 walk。

        缓存必须按扫描根目录键控：切换会话/项目后不能命中旧目录的清单。
        """
        scan_root = self._suggest_scan_root()
        now = time.time()
        cache = getattr(self, "_file_suggest_cache", None)
        if cache and len(cache) >= 3 and cache[2] == scan_root and now - cache[0] < self._FILE_SUGGEST_CACHE_TTL:
            return cache[1]
        results: List[str] = []
        for root, dirs, files in os.walk(scan_root):
            dirs[:] = [
                d for d in dirs
                if d not in self._FILE_SUGGEST_IGNORE_DIRS and not d.startswith(".")
            ]
            for f in files:
                if f.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(root, f), scan_root).replace("\\", "/")
                results.append(rel)
        self._file_suggest_cache = (now, results, scan_root)
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

    def search_sessions(
        self,
        query: str = "",
        limit: int = 12,
        current_session: Optional[str] = None,
    ) -> Dict[str, Any]:
        """@ 提及对话自动补全：会话名大小写不敏感子串匹配（排除当前会话）。

        current_session 是前端显式传入的「用户实际所在的会话名」：
        - 未传（None）时回退到后端自己维护的 _current_session，兼容旧前端；
        - 传空串表示用户正处于「新会话草稿」：该会话尚未落库，用户此刻并不在
          任何已落库会话的上下文里，此时必须不排除任何会话。草稿态下后端
          _current_session 仍停留在上一个真实会话（落库才切换，见
          _spawn_turn_worker 内的处理），若继续按它排除，用户在空白新会话里
          @ 上一个对话会永远搜不到，且改名字也无效——排除依据是会话身份而非名字。

        取数策略与侧栏 get_sessions() 对齐：
        - include_empty=True：否则刚新建、还没有任何消息的会话永远进不了 @ 候选；
        - 空会话（count==0）仅在【人工重命名过】时才补全，即名字不是默认的
          session_xxxxxx；默认名的空会话属于自动创建的垃圾临时会话，这里与
          get_sessions() 一样跳过，避免 @ 列表被大量无名空会话淹没。
        排序：无 query 时按最近活跃倒序；有 query 时前缀命中优先，其次名字更短
        （越贴近输入越可能是目标），最后才按最近活跃倒序，避免同名兄弟
        （X / X(2) / X(3)）因时间靠后而被挤出前 N 条。
        """
        try:
            if self._memory is None:
                return {"ok": False, "msg": "后端未就绪", "sessions": []}
            q = str(query or "").strip().lower()
            # 排除目标：前端显式传入的 current_session 优先（空串 = 草稿新会话，
            # 不排除任何会话）；未传入时回退到后端当前会话，保持旧调用方语义不变。
            if current_session is None:
                exclude_name = str(self._current_session or "")
            else:
                exclude_name = str(current_session)
            raw = self._memory.get_all_sessions(workdir=None, include_empty=True)
            items = []
            for s in raw:
                name = str(s.get("name") or "")
                if not name or (exclude_name and name == exclude_name):
                    continue
                count = int(s.get("count") or 0)
                # 空会话仅在人工重命名后才补全：默认名空会话是自动创建的
                # 垃圾临时会话，与 get_sessions() 的跳过策略保持一致
                if count == 0 and re.match(r"^session_[a-f0-9]{6}$", name):
                    continue
                if q and q not in name.lower():
                    continue
                items.append({
                    "name": name,
                    "count": count,
                    "updated_at": str(s.get("updated_at") or ""),
                    "workdir": str(s.get("workdir") or ""),
                })
            # 先按最近活跃倒序（稳定排序的基础顺序）
            items.sort(key=lambda it: it["updated_at"], reverse=True)
            if q:
                # 稳定排序：前缀命中优先 → 名字长度升序 → 保持上面的 updated_at 倒序
                items.sort(key=lambda it: (
                    0 if it["name"].lower().startswith(q) else 1,
                    len(it["name"]),
                ))
            return {"ok": True, "sessions": items[:max(1, int(limit or 12))]}
        except Exception as e:
            logger.exception("search_sessions 失败")
            return {"ok": False, "msg": str(e), "sessions": []}

    def search_sessions_full(
        self,
        query: str = "",
        scope: str = "all",
        workdir: str = "",
        limit: int = 30,
    ) -> Dict[str, Any]:
        """会话多维度全局检索（标题模糊匹配 + 对话内容全文检索）。"""
        try:
            if self._memory is None:
                return {"ok": False, "msg": "后端未就绪", "results": [], "total": 0}
            q = str(query or "").strip()
            if not q:
                return {"ok": True, "results": [], "total": 0}

            target_workdir = str(workdir or "").strip() or None
            raw_results = self._memory.search_sessions_and_messages(
                query=q,
                scope=scope,
                workdir=target_workdir,
                source="GUI",
                limit=max(1, min(int(limit or 30), 100)),
            )

            results = []
            for item in raw_results:
                name = str(item.get("session_name") or "")
                is_default_name = bool(re.match(r"^session_[a-f0-9]{6}$", name))
                display_name = "新会话" if is_default_name else name
                item_workdir = str(item.get("workdir") or "")
                if not item_workdir or item_workdir == "GLOBAL":
                    workdir_label = "自由对话"
                    workdir_val = "GLOBAL"
                else:
                    norm_w = _resolve_real_path(item_workdir)
                    workdir_label = os.path.basename(norm_w) or norm_w
                    workdir_val = norm_w

                results.append({
                    "type": item.get("type") or "title",
                    "session_name": name,
                    "display_name": display_name,
                    "workdir": workdir_val,
                    "workdir_label": workdir_label,
                    "updated_at": str(item.get("updated_at") or ""),
                    "msg_count": int(item.get("msg_count") or 0),
                    "role": item.get("role"),
                    "snippet": item.get("snippet"),
                })

            return {"ok": True, "results": results, "total": len(results)}
        except Exception as e:
            logger.exception("search_sessions_full 失败")
            return {"ok": False, "msg": str(e), "results": [], "total": 0}


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

    def browse_remote_dirs(self, path: str = "") -> Dict[str, Any]:
        """列出指定目录的子目录（手机端内置文件夹浏览器数据源）。

        path 为空时返回盘符列表（仅 Windows）。只列目录不列文件，
        无副作用，供 LAN 远程调用；真实目录校验在 select_workspace_by_path。
        """
        try:
            target = str(path or "").strip()
            if not target:
                if os.name == "nt":
                    import string
                    drives = []
                    for letter in string.ascii_uppercase:
                        drive = f"{letter}:\\"
                        if os.path.exists(drive):
                            drives.append({"name": drive, "path": drive})
                    return {"ok": True, "is_root": True, "current": "", "dirs": drives}
                home = os.path.expanduser("~")
                return self.browse_remote_dirs(home)
            real = _resolve_real_path(target)
            if not real or not os.path.isdir(real):
                return {"ok": False, "dirs": [], "msg": "目录不存在"}
            dirs = []
            try:
                for entry in os.scandir(real):
                    try:
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                            dirs.append({
                                "name": entry.name,
                                "path": os.path.normpath(os.path.join(real, entry.name)),
                            })
                    except OSError:
                        continue
            except PermissionError:
                pass
            dirs.sort(key=lambda d: d["name"].lower())
            return {"ok": True, "is_root": False, "current": real, "dirs": dirs}
        except Exception as e:
            logger.exception("browse_remote_dirs 失败")
            return {"ok": False, "dirs": [], "msg": str(e)}

    def select_workspace_by_path(self, path: str) -> Dict[str, Any]:
        """按绝对路径直接切换项目工作区（手机端内置浏览器的确认动作）。

        与 select_workspace_folder 的收尾逻辑一致：登记工作区、迁移 GLOBAL
        会话归属、返回文件树与会话列表；但跳过原生对话框，由前端传入路径。
        """
        try:
            selected_path = _resolve_real_path(str(path or ""))
            if not selected_path or not os.path.isdir(selected_path):
                return {"ok": False, "msg": "路径不是有效文件夹"}
            if self._memory is None:
                return {"ok": False, "msg": "后端未就绪"}
            self._memory.add_workspace(selected_path)
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
            logger.exception("select_workspace_by_path 失败")
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

    def pick_folder_dialog(self) -> Dict[str, Any]:
        """弹出原生文件夹对话框，仅返回所选路径，不改当前工作区。

        与 select_workspace_folder 的区别：不登记工作区、不迁移 GLOBAL 会话
        归属，供星轨自动化任务表单这类"纯选目录"的场景使用。
        """
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
            return {"ok": True, "path": selected_path}
        except Exception as e:
            logger.exception("pick_folder_dialog 失败")
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

    def _async_build_skill_list(self):
        """异步扫描技能列表：结果写入实例缓存并通过 skills_loaded 事件推送。

        原同步实现在 get_initial_state 中调用 _build_skill_list（内部 scan_all_skills），
        冷盘首次扫描曾达 25s+，表现为"窗口出来了但界面一直转圈"。改为后台执行：
        首帧立刻渲染，扫完由前端刷新技能页；下一次 get_initial_state（窗口重载 /
        手机连接）则直接命中缓存，不再有第二次冷盘等待。
        """
        # 同一时刻只允许一个扫描线程（见 _skills_scan_lock 的说明）；
        # 取不到锁属性（旧实例/测试裸构造）时按"无并发"降级处理
        lock = getattr(self, "_skills_scan_lock", None)
        if lock is not None and not lock.acquire(blocking=False):
            logger.debug("技能扫描已在进行中，跳过重复触发")
            return
        try:
            started = time.time()
            skills = self._build_skill_list()
            # _build_skill_list 成功时已顺带写入 _skills_cache；这里再兜一次，
            # 保证即使被测试桩替换过 _build_skill_list 也能留下可复用的结果
            self._skills_cache = skills
            logger.info(
                "异步技能列表扫描完成（%d 项，耗时 %.2fs）", len(skills), time.time() - started
            )
            # _emit_raw 在 _shutting_down 或无窗口时静默丢弃事件（见其实现）：
            # 启动早期首帧可能尚未就绪，此时事件会丢。这里重试一次兜住该窗口期，
            # 配合上面的缓存，前端不至于长期停在"技能为空"的状态。
            for _attempt in range(2):
                self._emit_raw("skills_loaded", {"skills": skills})
                if getattr(self, "_window", None):
                    break
                time.sleep(0.5)
        except Exception:
            # 只告警：技能列表为空不能影响窗口启动与其它功能
            logger.warning("异步技能列表扫描失败", exc_info=True)
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    logger.debug("释放技能扫描锁失败", exc_info=True)

    # ------------------------------------------------------------------
    # IDE 模式：编辑器文件读写 / 布局 / 已打开文件变更
    # ------------------------------------------------------------------
    _IDE_WATCH_INTERVAL = 1.0

    def _ide_watcher_instance(self):
        """惰性创建轮询器与线程；线程只负责轮询，派发交给 emit_event。"""
        from core.ide import file_service

        watcher = getattr(self, "_ide_watcher", None)
        if watcher is None:
            watcher = file_service.OpenFileWatcher(interval=self._IDE_WATCH_INTERVAL)
            self._ide_watcher = watcher
        return watcher

    def _ide_ensure_watch_thread(self) -> None:
        if getattr(self, "_ide_watch_thread", None) is not None:
            return
        import threading

        stop_event = threading.Event()
        self._ide_watch_stop = stop_event

        def _loop():
            while not stop_event.wait(self._IDE_WATCH_INTERVAL):
                try:
                    self._ide_poll_once()
                except Exception:
                    logger.exception("IDE 已打开文件轮询失败")

        thread = threading.Thread(target=_loop, name="ide-open-file-watcher", daemon=True)
        self._ide_watch_thread = thread
        thread.start()

    def _ide_poll_once(self) -> int:
        """轮询一次并派发变更事件，返回变更文件数（线程循环与测试共用）。"""
        watcher = self._ide_watcher_instance()
        changed = watcher.poll()
        for path in changed:
            self.emit_event("ide_file_changed", {"path": path})
        return len(changed)

    def ide_set_open_files(self, paths) -> Dict[str, Any]:
        """登记当前编辑器打开的文件（轮询集合由前端驱动）。"""
        try:
            watcher = self._ide_watcher_instance()
            watcher.set_open_files(paths or [])
            self._ide_ensure_watch_thread()
            return {"ok": True, "count": len(watcher._files)}
        except Exception as e:
            logger.exception("ide_set_open_files 失败")
            return {"ok": False, "msg": str(e)}

    def _ide_guard(self, path: str, action: str) -> "tuple[str, str]":
        """统一路径策略校验，返回 (real_path, 拒绝原因)。"""
        from core.gui_path_policy import gui_path_access_allowed

        real_path = _resolve_real_path(path)
        workspace = self._session_workdir(self._current_session)
        allowed, deny_msg = gui_path_access_allowed(
            real_path, workspace, action=action, session_id=self._current_session
        )
        if not allowed:
            return real_path, deny_msg
        return real_path, ""

    def ide_read_file(self, path: str) -> Dict[str, Any]:
        from core.ide import file_service

        try:
            real_path, deny = self._ide_guard(path, "edit")
            if deny:
                return {"ok": False, "msg": deny}
            return {"ok": True, **file_service.read_text_file(real_path)}
        except file_service.FileServiceError as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:
            logger.exception("ide_read_file 失败")
            return {"ok": False, "msg": str(e)}

    def ide_write_file(
        self,
        path: str,
        content: str,
        expected_hash: str,
        encoding: str = "utf-8",
        bom: bool = False,
        eol: str = "lf",
    ) -> Dict[str, Any]:
        """保存编辑器内容并刷新轮询基线。

        expected_hash 必填、无默认值：它是打开文件时 read_text_file 返回的 hash，
        作为冲突检测基准。**空串表示调用方明确要求覆盖**（用户在冲突提示里选择了
        「用我的覆盖」），非空时作冲突检测基准。这里刻意不给默认值——调用方漏传
        会被 Python 直接报 TypeError，而不是静默退化成「无条件覆盖」、关掉全部
        冲突保护。
        """
        from core.ide import file_service

        try:
            real_path, deny = self._ide_guard(path, "edit")
            if deny:
                return {"ok": False, "msg": deny}
            result = file_service.write_text_file(
                real_path, content, expected_hash,
                encoding=encoding, bom=bom, eol=eol,
            )
            # 只有真正落盘成功才回填基线：冲突 / 编码失败 / 权限失败都没改动磁盘，
            # 回填反而会掩盖紧接着的外部改动。
            if result.get("ok"):
                watcher = getattr(self, "_ide_watcher", None)
                if watcher is not None:
                    try:
                        watcher.acknowledge(real_path)
                    except Exception:
                        logger.debug("刷新 IDE 轮询基线失败", exc_info=True)
            return result
        except file_service.FileServiceError as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:
            logger.exception("ide_write_file 失败")
            return {"ok": False, "msg": str(e)}

    def ide_stat_files(self, paths) -> Dict[str, Any]:
        try:
            from core.ide import file_service

            stats = []
            for raw in paths or []:
                real_path, deny = self._ide_guard(raw, "preview")
                if deny:
                    stats.append({"path": raw, "denied": True, "msg": deny})
                    continue
                try:
                    st = os.stat(real_path)
                    with open(real_path, "rb") as fh:
                        digest = file_service._hash_bytes(fh.read())
                    stats.append({
                        "path": real_path,
                        "mtime": st.st_mtime,
                        "size": st.st_size,
                        "hash": digest,
                    })
                except OSError:
                    stats.append({"path": real_path, "missing": True})
            return {"ok": True, "stats": stats}
        except Exception as e:
            logger.exception("ide_stat_files 失败")
            return {"ok": False, "stats": [], "msg": str(e)}

    def ide_get_layout(self, session: str) -> Dict[str, Any]:
        try:
            from core.ide import layout_store

            return {"ok": True, "layout": layout_store.get_ide_layout(session)}
        except Exception as e:
            logger.exception("ide_get_layout 失败")
            return {"ok": False, "layout": None, "msg": str(e)}

    def ide_save_layout(self, session: str, layout) -> Dict[str, Any]:
        try:
            from core.ide import layout_store

            return {"ok": True, "layout": layout_store.save_ide_layout(session, layout)}
        except Exception as e:
            logger.exception("ide_save_layout 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # IDE 模式 S2：语言服务（LSP）
    # ------------------------------------------------------------------
    # 单次 LSP 请求的等待上限：补全 / 悬停这类交互请求超过这个时间对用户
    # 已经没有意义，宁可让前端拿到失败去回落纯文本编辑。
    # 桥方法本身跑在 pywebview 为每次 JS 调用新建的临时线程上（不是 UI 线程，
    # 也不是 gui-js-dispatcher），所以这里同步等待不会冻结界面；上限只是防止
    # 语言服务器失联时前端 Promise 永远悬着。
    _LSP_REQUEST_TIMEOUT = 15.0

    def _lsp_manager_instance(self):
        """惰性创建 LSP 管理器；两个回调都只做「进事件队列」这件事。

        回调在每次取实例时统一挂一次，而不是只在新建时挂：测试或其它入口可能
        把管理器直接塞进 `self._lsp_manager`（绕过本工厂），那种管理器没有回调，
        语言服务器的推送与状态变化会被静默丢掉。挂回调是幂等的纯赋值，代价可忽略。
        """
        manager = getattr(self, "_lsp_manager", None)
        if manager is None:
            from core.ide import lsp_manager

            manager = lsp_manager.LspManager()
            self._lsp_manager = manager
        # 回调只做「投递」：读线程 / 崩溃重启回调 / 合批定时器线程都可能在任意
        # 线程调到它们；emit_event 内部只往 _js_queue put_nowait，绝不直接
        # evaluate_js——那是 gui-js-dispatcher 单消费者线程的专属职责。
        manager._on_notification = self._lsp_on_notification
        manager._on_status = self._lsp_on_status
        return manager

    def _lsp_batcher_instance(self):
        batcher = getattr(self, "_lsp_batcher", None)
        if batcher is None:
            from core.ide import diagnostics_batcher

            batcher = diagnostics_batcher.DiagnosticsBatcher()
            self._lsp_batcher = batcher
        # 与 _lsp_manager_instance 同源的问题：只在「新建分支」挂 on_flush，测试或
        # 其它入口直接把 batcher 塞进 `self._lsp_batcher`（绕过本工厂）时，那种
        # batcher 永远没有 on_flush——诊断攒在内存里，一次都不会送到前端。
        # 末尾幂等赋值，代价可忽略。
        batcher._on_flush = self._lsp_flush_diagnostics
        return batcher

    def _lsp_on_status(self, status) -> None:
        """可能在 LSP 读线程 / 重启回调里被调用，只允许 emit_event。"""
        self.emit_event("lsp_status", status)

    def _lsp_on_notification(self, workspace: str, language_id: str, method: str, params) -> None:
        """语言服务器主动推送的入口，运行在该服务器的读线程上。

        读线程只允许把消息交给合批器（纯内存操作），绝不触碰 WebView：真正送到
        前端的 lsp_diagnostics 由合批定时器触发 _lsp_flush_diagnostics → emit_event。
        """
        if method != "textDocument/publishDiagnostics":
            return
        from core.ide.lsp_manager import uri_to_path

        params = params or {}
        uri = params.get("uri") or ""
        path = uri_to_path(uri) or uri
        self._lsp_batcher_instance().push(path, params.get("diagnostics") or [])

    def _lsp_flush_diagnostics(self, path: str, diagnostics) -> None:
        """由合批定时器线程调用，仍然只做 emit_event。"""
        self.emit_event("lsp_diagnostics", {"path": path, "diagnostics": diagnostics})

    def _lsp_discard_diagnostics(self, path: str, params=None) -> None:
        """文档关闭：丢弃该路径的待派发诊断与去重指纹（Task 6 评审项 A）。

        去重表 `_last_flushed` 唯一的清理点就是 `discard`；漏掉这一步它会随
        打开过的文件数无界累积。键必须与 `_lsp_on_notification` 推入时一致
        （uri 反解出来的路径）：前端传来的 path 与 didClose 的 uri 指向同一文件，
        两边都清一遍，免得符号链接 / 大小写归一化差异造成漏清。
        """
        batcher = getattr(self, "_lsp_batcher", None)
        if batcher is None:
            return
        from core.ide.lsp_manager import uri_to_path

        keys = {str(path)}
        doc = (params or {}).get("textDocument") if isinstance(params, dict) else None
        uri = (doc or {}).get("uri") if isinstance(doc, dict) else None
        if uri:
            derived = uri_to_path(uri)
            if derived:
                keys.add(derived)
        for key in keys:
            batcher.discard(key)

    def _lsp_workspace_guard(self) -> str:
        """工作区变化时回收全部语言服务器：进程与工作区绑定，不跨区复用。"""
        workspace = self._session_workdir(self._current_session) or ""
        if getattr(self, "_lsp_workspace", None) != workspace:
            if getattr(self, "_lsp_manager", None) is not None:
                try:
                    self._lsp_manager.recycle_all()
                except Exception:
                    logger.exception("切换工作区时回收语言服务器失败")
            self._lsp_workspace = workspace
        return workspace

    def _lsp_workspace_token(self) -> tuple:
        """取 (workspace, epoch)：两者必须同源取出，缺一都堵不住竞态窗口。

        单取 workspace 是不够的（S2 验收欠账 1）：取到 workspace 与真正建槽之间
        会穿插路径校验、语言识别、可执行文件探测等耗时动作，若这中间发生了工作区
        回收，`LspManager.ensure_server` 会给**旧工作区**重新建槽并拉起全新进程
        （世代护栏认的是新世代号，拦不住它），而此后 `_lsp_workspace` 已是新工作区，
        下一次 guard 判定「无变化」不再回收——那个进程会一直活到下次切换或 close()。
        把回收前取到的 epoch 一并交给 `ensure_server`，过期即拒绝启动。
        epoch 与 workspace 都在 guard 之后取：guard 自身触发的回收会把 epoch 推进到
        新值，这里读到的就是新工作区的有效令牌。
        """
        workspace = self._lsp_workspace_guard()
        manager = getattr(self, "_lsp_manager", None)
        epoch = manager.current_epoch() if manager is not None else 0
        return workspace, epoch

    def lsp_request(self, path: str, method: str, params=None) -> Dict[str, Any]:
        from core.ide.lsp_manager import LspError, LspRequestError

        try:
            workspace, epoch = self._lsp_workspace_token()
            real_path, deny = self._ide_guard(path, "preview")
            if deny:
                return {"ok": False, "error": {"code": -32003, "message": deny}, "msg": deny}
            from core.ide import language_registry

            language_id = language_registry.language_id_for_path(real_path)
            if not language_id:
                msg = "该文件类型未配置语言服务器，已降级为纯文本编辑"
                return {"ok": False, "error": {"code": -32000, "message": msg}, "msg": msg}
            manager = self._lsp_manager_instance()
            result = manager.request(
                workspace,
                language_id,
                method,
                params or {},
                timeout=self._LSP_REQUEST_TIMEOUT,
                epoch=epoch,
            )
            return {"ok": True, "result": result}
        except LspRequestError as e:
            return {"ok": False, "error": {"code": e.code, "message": e.message}, "msg": str(e)}
        except LspError as e:
            return {"ok": False, "error": {"code": -32001, "message": str(e)}, "msg": str(e)}
        except Exception as e:
            logger.exception("lsp_request 失败")
            return {"ok": False, "error": {"code": -32603, "message": str(e)}, "msg": str(e)}

    def lsp_notify(self, path: str, method: str, params=None) -> Dict[str, Any]:
        from core.ide.lsp_manager import LspError

        try:
            workspace, epoch = self._lsp_workspace_token()
            real_path, deny = self._ide_guard(path, "preview")
            if deny:
                return {"ok": False, "error": {"code": -32003, "message": deny}, "msg": deny}
            # 文档关闭是合批器状态的收口点：先清掉该文件的待派发诊断与去重指纹，
            # 再发 didClose。放在 notify 之前——即使服务器不可用、通知发不出去，
            # 前端已经关掉的文档也不该在去重表里留痕（评审项 A）。
            if method == "textDocument/didClose":
                self._lsp_discard_diagnostics(real_path, params)
            from core.ide import language_registry

            language_id = language_registry.language_id_for_path(real_path)
            if not language_id:
                msg = "该文件类型未配置语言服务器，已降级为纯文本编辑"
                return {"ok": False, "error": {"code": -32000, "message": msg}, "msg": msg}
            manager = self._lsp_manager_instance()
            manager.notify(workspace, language_id, method, params or {}, epoch=epoch)
            return {"ok": True}
        except LspError as e:
            return {"ok": False, "error": {"code": -32001, "message": str(e)}, "msg": str(e)}
        except Exception as e:
            logger.exception("lsp_notify 失败")
            return {"ok": False, "error": {"code": -32603, "message": str(e)}, "msg": str(e)}

    def lsp_sync_workspace(self) -> Dict[str, Any]:
        """前端在会话 / 工作区切换后主动调用：旧工作区的语言服务器立刻退出。

        配套的兜底是 _lsp_workspace_guard：即使前端漏调，下一次 LSP 请求也会
        先回收旧工作区的进程，因此不存在「切了工作区还在跑旧服务器」的窗口。
        """
        try:
            workspace = self._session_workdir(self._current_session) or ""
            recycled = []
            manager = getattr(self, "_lsp_manager", None)
            if manager is not None:
                recycled = [language_id for _, language_id in manager.recycle_all()]
            batcher = getattr(self, "_lsp_batcher", None)
            if batcher is not None:
                batcher.flush()
            self._lsp_workspace = workspace
            return {"ok": True, "workspace": workspace, "recycled": recycled}
        except Exception as e:
            logger.exception("lsp_sync_workspace 失败")
            return {"ok": False, "workspace": "", "recycled": [], "msg": str(e)}

    def lsp_server_status(self, workspace: str = "") -> Dict[str, Any]:
        """枚举全部已配置语言服务器的状态；不启动任何进程（不会因为看状态而拉起服务器）。"""
        try:
            from core.ide import language_registry, server_provision

            current = workspace or self._lsp_workspace_guard()
            manager = getattr(self, "_lsp_manager", None)
            live = {}
            if manager is not None:
                for row in manager.status(current):
                    live[row["language"]] = row
            servers = []
            for language_id, server_id in language_registry.all_language_servers():
                provision = server_provision.resolve_executable(server_id)
                row = live.get(language_id) or {}
                if provision["source"] == "missing":
                    state = "missing"
                else:
                    state = row.get("state", "stopped")
                servers.append({
                    "server": server_id,
                    "language": language_id,
                    "state": state,
                    "executable": provision["executable"],
                    "source": provision["source"],
                    "pid": row.get("pid"),
                    "msg": row.get("msg") or provision["msg"],
                })
            return {"ok": True, "workspace": current, "servers": servers}
        except Exception as e:
            logger.exception("lsp_server_status 失败")
            return {"ok": False, "servers": [], "msg": str(e)}

    def lsp_set_server_path(self, server_id: str, path: str) -> Dict[str, Any]:
        """设置页手动指定可执行文件路径；path 为空串表示恢复自动探测。"""
        from core.ide import server_provision

        try:
            if path:
                result = server_provision.save_override(server_id, path)
            else:
                result = server_provision.clear_override(server_id)
            # 可执行文件变了，旧进程必须换掉，否则用户改了路径却毫无效果
            if getattr(self, "_lsp_manager", None) is not None:
                self._lsp_manager.recycle_all()
            return {"ok": True, **result}
        except server_provision.ProvisionError as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:
            logger.exception("lsp_set_server_path 失败")
            return {"ok": False, "msg": str(e)}

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
                        "session_id": s.get("id"),
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
                    "session_id": s.get("id"),
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

            msgs, has_more, oldest_id = self._load_session_messages(session_name)

            # token 重算走异步实现（内部自 try/except，完成后推送
            # token_usage_updated），SQLite 锁竞争时不再阻塞会话切换
            self._async_recalc_token_usage(session_name)
            # 切换会话后把该会话绑定的模型信息一并返回给前端，
            # 让状态栏与模型选择器随会话自动恢复，不再互相覆盖
            session_model = get_active_model(session_name) or {}
            result = {
                "ok": True,
                "session_name": session_name,
                "messages": msgs,
                # 历史分页元信息：前端据此判断是否还能向前翻页及翻页游标
                "history_has_more": has_more,
                "history_oldest_id": oldest_id,
                # 不再随切换响应返回全量 skills 列表：前端 switchSession 不消费
                # 该字段，而每次切换都全盘扫描技能目录（实测 43 个技能约 34ms）
                # 纯属浪费；技能列表由 get_initial_state 读取 _skills_cache 提供，
                # 并由 refresh_skills / toggle_skill / restore_skill 维护更新。
                "sessions": self.get_sessions(),
                "active_model_id": str(session_model.get("id") or ""),
                "active_model_name": str(session_model.get("name") or "未激活模型"),
                "reasoning": _reasoning_snapshot_for_model(session_model),
                # 会话级权限档位随切换返回：前端权限按钮按会话恢复显示
                "permission_mode": self._effective_permission_mode(session_name),
                # 排队中的用户消息随切换返回：前端免额外往返即可渲染排队条目
                "message_queue": self._message_queue_snapshot(session_name),
                # 目标会话若正在运行，返回其实时流式快照供前端恢复
                "active_turn_snapshot": self.get_active_turn_snapshot(session_name),
            }
            if is_global:
                result.update({"workdir": "GLOBAL", "is_global": True})
            else:
                current_workdir = _resolve_real_path(candidate_wdir or stored_wdir or os.getcwd())
                result.update({"workdir": current_workdir, "is_global": False})
            # 文件树不再随切换响应同步扫描（大目录要数秒，拖慢切换）；
            # 前端切换成功后调 get_file_tree 后台异步加载。
            self._emit_token_usage()
            self.emit_event("active_session_changed", {"session_name": session_name})
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
            from core import permission_grants
            from core.tools import rename_session_sandbox_bypass

            permission_grants.rename_task(old_name, final_name)
            rename_session_sandbox_bypass(old_name, final_name)
            # 排队消息随会话迁移到新名（重命名时无运行回合，但队列可能非空：
            # 上一回合刚结束、补发线程尚未调度等窗口），快照让前端同步
            with self._turn_start_lock:
                if old_name in self._message_queues:
                    self._message_queues[final_name] = self._message_queues.pop(old_name)
                    self._emit_message_queue_snapshot(final_name)
            # 手动重命名与自动重命名（_rename_session_to_title）共用同一事件负载：
            # 前端按该负载迁移当前会话名、运行态与分组内条目，避免前端再写一份同步逻辑。
            # 取列表与推事件都放在保护块内：此时 DB 与授权迁移均已生效，报失败会让前端
            # 误以为没改名（并且不再刷新列表）；sessions=None 让前端跳过列表覆盖，不清空侧栏
            try:
                sessions = self.get_sessions()
                self.emit_event("session_renamed", {
                    "old": old_name,
                    "new": final_name,
                    # 兼容旧字段名
                    "old_name": old_name,
                    "new_name": final_name,
                    "sessions": sessions,
                })
            except Exception:
                logger.exception("推送 session_renamed 事件失败")
                sessions = None
            return {"ok": True, "name": final_name, "sessions": sessions}
        except Exception as e:
            logger.exception("rename_session 失败")
            return {"ok": False, "msg": str(e), "name": old_name}

    def delete_session(self, session_name: str) -> Dict[str, Any]:
        try:
            # M21：回合运行中禁止删除——回合线程会继续往已删会话写消息，
            # 产生幽灵数据；最小方案是直接拒绝，让用户先停止或等待完成。
            # 检查与排队消息清理收进 _turn_start_lock：否则在"旧回合已 pop
            # _active_turns、补发排队消息的线程尚未调度"窗口内删除会话，
            # 补发线程随后会把排队消息写进已删除的会话（幽灵数据）
            with self._turn_start_lock:
                if self._is_turn_active(session_name):
                    return {"ok": False, "success": False, "msg": "该会话正在运行回合，请先停止或等待完成后再删除"}
                # 会话删除后排队消息一并丢弃
                self._message_queues.pop(session_name, None)
            self._memory.delete_session(session_name)
            from core import permission_grants
            from core.tools import clear_session_sandbox_bypass

            permission_grants.clear_task(session_name)
            clear_session_sandbox_bypass(session_name)
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
            # 删除前先取组内会话名单：组内会话的排队消息一并丢弃，
            # 避免补发线程把排队消息写进已删除的会话（幽灵数据）
            group_sessions = [
                str(s.get("name") or "")
                for s in self._memory.get_all_sessions(workdir=workdir, include_empty=True)
            ]
            self._memory.delete_sessions_by_workdir(workdir)
            self._memory.delete_workspace(workdir)
            with self._turn_start_lock:
                for name in group_sessions:
                    self._message_queues.pop(name, None)
            from core import permission_grants
            from core.tools import clear_session_sandbox_bypass

            for name in group_sessions:
                if name:
                    permission_grants.clear_task(name)
                    clear_session_sandbox_bypass(name)
            return {"ok": True, "success": True, "sessions": self.get_sessions()}
        except Exception as e:
            logger.exception("delete_folder_group 失败")
            return {"ok": False, "success": False, "msg": str(e)}

    def clear_session_history(self, session_name: Optional[str] = None) -> Dict[str, Any]:
        try:
            target = session_name or self._current_session
            self._clear_session_last_error(target)
            deleted = self._memory.clear_session_messages(target)
            from core.llm import session_token_usage, token_usage_lock
            from core.context_breakdown import clear_breakdown
            with token_usage_lock:
                session_token_usage["latest_total"] = 0
                session_token_usage["latest_prompt"] = 0
                session_token_usage["latest_cached"] = 0
            # 上下文占比快照与 token 记账同生命周期：清空历史即清空快照
            clear_breakdown()
            self._emit_token_usage()
            return {"ok": True, "deleted": int(deleted), "msg": f"已清空 {deleted} 条历史记录"}
        except Exception as e:
            logger.exception("clear_session_history 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 模型与权限模式
    # ------------------------------------------------------------------
    def set_model(self, model_id: str, session_name: Optional[str] = None):
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

            session_name = session_name or self._current_session
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
            # 广播模型变更到所有端（桌面 JS 桥 + 局域网手机）：任一端切换模型后，
            # 其他端的模型芯片/状态栏/轮次标签实时跟随，避免两端显示与真实不符
            self.emit_event("model_changed", {
                "session_name": session_name or "",
                "model_id": str(model_id),
                "model_name": str(matched.get("name", "")),
                "reasoning": _reasoning_snapshot_for_model(matched),
            })
            return {
                "ok": True,
                "active_model_id": str(model_id),
                "active_model_name": str(matched.get("name", "")),
                "reasoning": _reasoning_snapshot_for_model(matched),
            }
        except Exception as e:
            logger.exception("set_model 失败")
            return {"ok": False, "msg": str(e)}

    def set_reasoning_choice(self, model_id: str, choice: str) -> Dict[str, Any]:
        """设置指定模型的思考开关或强度（下一轮请求生效）。"""
        try:
            target_id = str(model_id or "").strip()
            if not target_id:
                return {"ok": False, "msg": "当前没有激活模型"}
            ok, message = set_model_reasoning_choice(target_id, choice)
            if not ok:
                return {"ok": False, "msg": message}
            config_data = load_config()
            target_model = next(
                (
                    model
                    for provider in config_data.get("providers", [])
                    for model in provider.get("models", [])
                    if str(model.get("id")) == target_id
                ),
                None,
            )
            if target_model is None:
                raise ValueError(f"未找到模型: {target_id}")
            return {
                "ok": True,
                "reasoning": _reasoning_snapshot_for_model(target_model),
                "msg": message,
            }
        except Exception as e:
            logger.exception("set_reasoning_choice 失败")
            return {"ok": False, "msg": str(e)}

    def get_reasoning_snapshot(self, model_id: str) -> Dict[str, Any]:
        """只读返回指定模型的思考能力与当前选择，供 pending 草稿切模型同步 UI。"""
        try:
            target_id = str(model_id or "").strip()
            if not target_id:
                return {"ok": False, "msg": "当前没有激活模型"}
            config_data = load_config()
            target_model = next(
                (
                    model
                    for provider in config_data.get("providers", [])
                    for model in provider.get("models", [])
                    if str(model.get("id")) == target_id
                ),
                None,
            )
            if target_model is None:
                return {"ok": False, "msg": f"未找到模型: {target_id}"}
            return {
                "ok": True,
                "reasoning": _reasoning_snapshot_for_model(target_model),
            }
        except Exception as e:
            logger.exception("get_reasoning_snapshot 失败")
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

    def _effective_permission_mode(self, session_name: str) -> str:
        """会话生效的权限档位：会话绑定优先，未绑定回退全局默认。

        权限选择是会话级的（sessions.permission_mode）；全局值只作未绑定
        会话与新会话的兜底，改动不再互相覆盖。getattr 防御裸实例测试桥
        可能未初始化全局档位（同 _automation_turn_labels 先例）。
        """
        if session_name:
            try:
                bound = self._memory.get_session_permission_mode(session_name)
                if bound:
                    return bound
            except Exception:
                logger.debug("读取会话权限档位失败", exc_info=True)
        return getattr(self, "_current_permission_mode", "confirm") or "confirm"

    def get_permission_mode(self, session_name: str = "") -> Dict[str, Any]:
        """返回指定会话的**生效**权限档位（会话级优先，未绑定回退全局默认）。

        只读接口：供飞书/手机端在切换档位前展示当前状态，不修改任何状态。
        """
        try:
            target = str(session_name or "").strip() or self._current_session
            return {"ok": True, "session_name": target,
                    "permission_mode": self._effective_permission_mode(target)}
        except Exception as e:
            logger.exception("get_permission_mode 失败")
            return {"ok": False, "msg": str(e)}

    def set_permission_mode(self, mode: str, session_name: str = "") -> Dict[str, Any]:
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
            target_session = str(session_name or "").strip()
            if target_session:
                # 会话级绑定：只影响该会话，不改全局默认/配置文件。会话尚未
                # 落库（前端 pending 草稿）时绑不上，前端把选择随首条消息带
                # 给 worker 落绑定（pending_permission_mode）
                bound = self._memory.set_session_permission_mode(target_session, canonical)
                return {"ok": True, "permission_mode": canonical, "bound": bound}
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

    def post_notice(self, message: str, session_name: str = "", kind: str = "notice") -> Dict[str, Any]:
        """把客户端本地产生的对话提示（notice/error 卡片）广播到全部客户端。

        前端本地提示（如"已切换活动模型"）原来只插入本端消息流，桌面 GUI 与
        局域网手机端互不可见。改为统一调用本方法：emit_event 会把 notice 事件
        同时派发到桌面 webview 与手机端（_emit_raw 自带 LAN 广播），各端（含
        发起端）收到事件后回环插入，天然同步且不重复。提示仍是瞬态的，
        不写入会话历史、不影响模型上下文。
        """
        try:
            text = str(message or "").strip()
            if not text:
                return {"ok": False, "msg": "空提示内容"}
            if kind not in ("notice", "error"):
                kind = "notice"
            self.emit_event("notice", {
                "message": text,
                "session_name": str(session_name or ""),
                "kind": kind,
            })
            return {"ok": True}
        except Exception as e:
            logger.exception("post_notice 失败")
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

    # ------------------------------------------------------------------
    # 系统设置
    # ------------------------------------------------------------------
    def get_system_settings(self) -> Dict[str, Any]:
        """返回可扩展的系统级设置快照；不得包含凭据。"""
        from core.command_shell import get_system_settings

        return get_system_settings()

    def set_command_shell(self, shell_id: str) -> Dict[str, Any]:
        """设置命令默认解释器；不可用解释器必须显式拒绝，不做静默降级。"""
        from core.command_shell import CommandShellError, set_selected_command_shell

        try:
            settings = set_selected_command_shell(shell_id)
            return {"ok": True, "settings": settings, "msg": "默认命令解释器已更新"}
        except (CommandShellError, ValueError) as error:
            return {"ok": False, "msg": str(error), "settings": self.get_system_settings()}

    def set_ai_output_dir(self, path: str) -> Dict[str, Any]:
        """设置 AI 生成内容目录；不可用路径显式拒绝，不做静默降级。"""
        from core.ai_output_paths import AiOutputDirError, set_ai_output_dir_setting

        try:
            effective = set_ai_output_dir_setting(path)
            return {
                "ok": True,
                "settings": self.get_system_settings(),
                "msg": f"AI 生成内容目录已更新：{effective}",
            }
        except (AiOutputDirError, ValueError) as error:
            return {"ok": False, "msg": str(error), "settings": self.get_system_settings()}

    def set_agent_turn_step_limit(self, value: int) -> Dict[str, Any]:
        """设置单任务最大模型轮次；显式非法值必须拒绝且不写盘。"""
        from core.harness.turn_limits import TurnStepLimitError, save_turn_step_limit

        try:
            save_turn_step_limit(value)
            return {
                "ok": True,
                "settings": self.get_system_settings(),
                "msg": "单任务最大模型轮次已更新",
            }
        except TurnStepLimitError as error:
            return {
                "ok": False,
                "msg": str(error),
                "settings": self.get_system_settings(),
            }

    # ------------------------------------------------------------------
    # /doctor 聚合自检（T3b：设置页「运行自检」入口）
    # ------------------------------------------------------------------
    def run_doctor(self, skip_network: bool = False) -> Dict[str, Any]:
        """GUI 设置页一键自检：复用 core.doctor.run_checks（与 CLI /doctor 同源）。

        场景约定：GUI 由用户主动点击触发，默认做完整检查（含模型端点联网
        探测，单项 3 秒超时，不冻结界面——pywebview 在工作线程执行桥接方法）；
        skip_network=True 跳过网络项，供快速重跑/离线场景，与 CLI 同参对齐。

        成功: {"ok": True, "checks": [{name,status,detail,hint}...], "summary": {...}}
        异常: {"ok": False, "msg": str(e), "checks": [], "summary": 零值}
              （防御性兜底；run_checks 内部已把单项异常隔离为 fail，正常不触发）
        """
        try:
            from core.doctor import run_checks

            checks = [
                result.to_dict()
                for result in run_checks(skip_network=bool(skip_network))
            ]
            summary: Dict[str, int] = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
            for check in checks:
                status = str(check.get("status") or "")
                if status in summary:
                    summary[status] += 1
            return {"ok": True, "checks": checks, "summary": summary}
        except Exception as e:
            logger.exception("run_doctor 失败")
            return {
                "ok": False,
                "msg": str(e),
                "checks": [],
                "summary": {"ok": 0, "warn": 0, "fail": 0, "skip": 0},
            }

    # ------------------------------------------------------------------
    # 人设设置
    # ------------------------------------------------------------------
    def get_persona_settings(self) -> Dict[str, Any]:
        """返回智能体人设设置快照（名称 + 人设文本）。"""
        from core.persona_settings import get_persona_settings

        return {"ok": True, **get_persona_settings()}

    def set_persona_settings(self, agent_name: str, persona: str = "", user_title: str = "") -> Dict[str, Any]:
        """保存智能体人设设置；校验失败拒绝写盘并返回原因。"""
        from core.persona_settings import PersonaSettingsError, save_persona_settings

        try:
            snapshot = save_persona_settings(agent_name, persona or "", user_title or "")
            return {"ok": True, **snapshot, "msg": "人设设置已保存"}
        except PersonaSettingsError as error:
            return {"ok": False, "msg": str(error)}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("set_persona_settings 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 免责条款同意（合规链路：首次使用与条款版本升级后必须显式确认，
    # 不同意则退出软件；记录写入 ~/.ycode/disclaimer_consent.json）
    # ------------------------------------------------------------------

    def get_disclaimer_consent(self) -> Dict[str, Any]:
        """返回免责条款同意状态快照（当前版本 + 已同意版本 + 同意时间）。"""
        from core.disclaimer_consent import get_disclaimer_consent

        return {"ok": True, **get_disclaimer_consent()}

    def accept_disclaimer(self) -> Dict[str, Any]:
        """记录用户已接受当前免责条款版本（版本号以服务端为准，防前端旧缓存绕过）。"""
        from core.disclaimer_consent import DisclaimerConsentError, accept_disclaimer

        try:
            snapshot = accept_disclaimer()
            return {"ok": True, **snapshot, "msg": "已记录条款同意"}
        except DisclaimerConsentError as error:
            return {"ok": False, "msg": str(error)}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("accept_disclaimer 失败")
            return {"ok": False, "msg": str(e)}


    # ------------------------------------------------------------------
    # 电脑操控系统设置（设计 8.1/8.4：严格 setter；开启前组合 preflight，
    # 失败不落盘；关闭先落盘 false + 递增 generation，再通知 controller 停止）
    # ------------------------------------------------------------------

    def _browser_control_preflight(self) -> None:
        """浏览器操控开启 preflight：所选浏览器可用 + 全局热键可注册。"""
        from core.computer.models import ComputerControlError
        from core.computer.settings import load_computer_settings
        from core.computer.platforms import get_platform_adapter

        snapshot = load_computer_settings()
        adapter = get_platform_adapter()
        if not snapshot.platform_supported:
            raise ComputerControlError(
                "computer_unsupported_os", adapter.unavailable_reason
            )
        selected = next(
            (
                option
                for option in snapshot.browser_options
                if option.browser == snapshot.computer_browser
            ),
            None,
        )
        if selected is None or not selected.available:
            label = selected.label if selected is not None else snapshot.computer_browser
            reason = (
                selected.unavailable_reason
                if selected is not None
                else "未知浏览器"
            )
            raise ComputerControlError(
                "computer_browser_unavailable", f"{label} 不可用：{reason}"
            )
        if not adapter.probe_hotkey():
            raise ComputerControlError(
                "computer_hotkey_unavailable",
                "无法注册全局紧急停止热键 Ctrl+Alt+Esc",
            )

    def _computer_control_preflight(self) -> None:
        """完整电脑操控开启 preflight：由平台层校验授权、坐标环境与紧急停止。"""
        from core.computer.platforms import get_platform_adapter

        get_platform_adapter().preflight_desktop()

    def _stop_computer_runs_for_disabled_switch(self, switch_target: str) -> None:
        """开关关闭后通知 controller 停止对应 target 的活动 run（设计 8.1/8.4）。

        config 已先落盘 false 且 generation 已递增：即使本通知失败或清理
        尚在进行，新动作也会被 generation 检查拒绝，此处只加速取消当前 Turn；
        未绑定 target 的 run 由 controller 的 effective_target 检查拒绝越权动作。
        """
        try:
            from core.computer.controller import get_computer_control_service

            snapshot = get_computer_control_service().snapshot()
            if not snapshot.run_id:
                return
            if snapshot.status in ("completed", "stopped", "failed"):
                return
            if snapshot.target != switch_target:
                return
            get_computer_control_service().stop(snapshot.run_id, "settings_disabled")
        except Exception:
            logger.exception("关闭电脑操控开关后停止活动 run 失败")

    def _refresh_computer_hotkey(self) -> None:
        """开关切换成功后按当前设置同步全局紧急停止热键（Task 16 生命周期）。

        注册/注销失败只留痕：配置已落盘，开关结果不受影响，热键能力单独
        降级（设置页可见 unavailable）；语义（首次注册/活动保持/全关注销）
        由 core.computer.runtime.refresh_computer_runtime_hotkey 统一实现。
        """
        try:
            from core.computer.runtime import refresh_computer_runtime_hotkey

            refresh_computer_runtime_hotkey()
        except Exception:
            logger.exception("同步全局紧急停止热键失败")

    def _has_active_computer_run(self) -> bool:
        """是否存在活动电脑操控 run；读取失败按活动处理（fail-closed 拒绝切换）。"""
        try:
            from core.computer.controller import get_computer_control_service

            snapshot = get_computer_control_service().snapshot()
            return bool(snapshot.run_id) and snapshot.status not in (
                "completed",
                "stopped",
                "failed",
            )
        except Exception:
            logger.exception("读取电脑操控 run 状态失败")
            return True

    def set_browser_control_enabled(self, value: bool) -> Dict[str, Any]:
        """浏览器操控开关：严格 setter；开启 preflight 失败不落盘。"""
        from core.computer.models import ComputerControlError
        from core.computer.settings import set_browser_control_enabled

        try:
            set_browser_control_enabled(
                value, preflight=self._browser_control_preflight
            )
        except ComputerControlError as error:
            return {
                "ok": False,
                "error_code": error.code,
                "msg": str(error),
                "settings": self.get_system_settings(),
            }
        if value is False:
            self._stop_computer_runs_for_disabled_switch("browser")
        # 开关切换成功后同步紧急停止热键（Task 16：开启首次注册/全关注销）
        self._refresh_computer_hotkey()
        return {"ok": True, "settings": self.get_system_settings()}

    def set_computer_control_enabled(self, value: bool) -> Dict[str, Any]:
        """完整电脑操控开关：严格 setter；开启前 Windows backend + 热键 preflight。"""
        from core.computer.models import ComputerControlError
        from core.computer.settings import set_computer_control_enabled

        try:
            set_computer_control_enabled(
                value, preflight=self._computer_control_preflight
            )
        except ComputerControlError as error:
            return {
                "ok": False,
                "error_code": error.code,
                "msg": str(error),
                "settings": self.get_system_settings(),
            }
        if value is False:
            self._stop_computer_runs_for_disabled_switch("desktop")
        # 开关切换成功后同步紧急停止热键（Task 16：开启首次注册/全关注销）
        self._refresh_computer_hotkey()
        return {"ok": True, "settings": self.get_system_settings()}

    def set_computer_browser(self, value: str) -> Dict[str, Any]:
        """默认浏览器选择：活动会话中不可修改（设计 8.1），必须先停止操控。"""
        from core.computer.models import ComputerControlError
        from core.computer.settings import set_computer_browser

        try:
            set_computer_browser(value, active_run=self._has_active_computer_run())
        except ComputerControlError as error:
            return {
                "ok": False,
                "error_code": error.code,
                "msg": str(error),
                "settings": self.get_system_settings(),
            }
        return {"ok": True, "settings": self.get_system_settings()}

    # ------------------------------------------------------------------
    # 电脑操控运行控制与事件桥（设计 16.4/16.5）：控制 API 只调领域服务并
    # 返回完整 snapshot（前端用快照校准 Store）；controller 四类事件经
    # listener 以小 JSON 进入 _js_queue，evaluate_js 仍只在
    # gui-js-dispatcher 单消费者线程执行
    # ------------------------------------------------------------------

    def _ensure_computer_event_bridge(self):
        """建立进程内电脑操控事件桥：controller 惰性单例 + 一次性 listener。

        重复调用幂等（add_listener 按相等性去重）；set_window 与每个控制方法
        都会触发，保证任何 run 的事件都不会因桥接未建立而丢失。
        """
        from core.computer.controller import get_computer_control_service

        controller = get_computer_control_service()
        if getattr(self, "_computer_event_listener", None) is None:
            listener = self._on_computer_control_event
            controller.add_listener(listener)
            self._computer_event_listener = listener
        return controller

    def _on_computer_control_event(self, event: dict) -> None:
        """controller 事件 listener：小 JSON 序列化后 put_nowait 进 _js_queue。

        事件源线程（回合 worker / backend 线程）绝不直接调用 evaluate_js；
        队列满按现有策略丢弃新事件，生产者永不阻塞。
        """
        if self._shutting_down:
            return
        try:
            payload = json.dumps(event, ensure_ascii=False)
            payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        except Exception as e:
            logger.warning("序列化电脑操控事件失败: %s", e)
            return
        self._queue_js_payload(payload)

    def _run_computer_mutation(self, operation) -> Dict[str, Any]:
        """执行一次电脑操控 mutation 并返回完整快照。

        run_id 由 controller 校验（必须匹配当前活动 run，旧 UI 事件的 run id
        拒绝，防旧事件停新 run）；失败同样携带当前快照供前端校准（设计 16.4）。
        """
        from core.computer.models import ComputerControlError

        controller = self._ensure_computer_event_bridge()
        try:
            snapshot = operation(controller)
        except ComputerControlError as error:
            snapshot = controller.snapshot()
            return {
                "ok": False,
                **snapshot.to_public_dict(),
                "error_code": error.code,
                "msg": str(error),
            }
        return {"ok": True, **snapshot.to_public_dict()}

    def get_computer_control_status(self) -> Dict[str, Any]:
        """当前电脑操控 run 完整快照；前端校准 Store 的唯一真值源（设计 16.4）。"""
        controller = self._ensure_computer_event_bridge()
        return {"ok": True, **controller.snapshot().to_public_dict()}

    def pause_computer_control(self, run_id: str) -> Dict[str, Any]:
        """暂停当前 run：只阻止下一动作开始，不回滚当前动作（设计 15.3）。"""
        return self._run_computer_mutation(
            lambda controller: controller.pause(str(run_id or ""), "user_pause")
        )

    def resume_computer_control(self, run_id: str) -> Dict[str, Any]:
        """恢复当前 run；provider_safety_required 未验证契约前 controller 拒绝。"""
        return self._run_computer_mutation(
            lambda controller: controller.resume(str(run_id or ""))
        )

    def stop_computer_control(self, run_id: str) -> Dict[str, Any]:
        """用户紧急停止当前 run；固定停止顺序由 controller.stop 执行（设计 15.2）。"""
        return self._run_computer_mutation(
            lambda controller: controller.stop(str(run_id or ""), "user_stop")
        )

    def acknowledge_computer_safety(self, run_id: str, check_ids=None) -> Dict[str, Any]:
        """Provider 强制安全检查确认：官方契约验证完成前固定不可用。

        Task 11b Step 4 的三条件（官方 GA schema 提供确认字段 / 该精确形状
        byte-golden 通过 / 用户显式传入当前 run id 与精确 check ids）未全部
        满足前，Bridge 绝不构造确认 payload，固定返回
        computer_provider_safety_required；UI 只显示状态与停止。
        """
        controller = self._ensure_computer_event_bridge()
        snapshot = controller.snapshot()
        return {
            "ok": False,
            **snapshot.to_public_dict(),
            "error_code": "computer_provider_safety_required",
            "msg": "Provider 强制安全检查的确认通道未开放：官方确认契约验证前只允许查看状态与停止",
        }

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
        """清空当前项目的持久化白名单（任务级授权不受影响）。"""
        try:
            from core import permission_store

            removed = permission_store.clear_project()
            return {"ok": True, "removed": removed}
        except Exception as e:
            logger.exception("clear_permission_rules 失败")
            return {"ok": False, "msg": str(e)}

    def add_permission_rule(self, rule_type: str, value: str) -> Dict[str, Any]:
        try:
            from core import permission_store

            result = permission_store.add_rule(rule_type, value)
            if result.status == "limit_reached":
                return {"ok": False, "status": result.status, "msg": "项目白名单规则数量已达到上限"}
            return {
                "ok": True,
                "status": result.status,
                "rule_type": result.rule_type,
                "normalized_value": result.normalized_value,
            }
        except (ValueError, permission_store.PermissionStoreError) as exc:
            return {"ok": False, "msg": str(exc)}

    def remove_permission_rule(self, rule_type: str, value: str) -> Dict[str, Any]:
        try:
            from core import permission_store

            result = permission_store.remove_rule(rule_type, value)
            if result.status == "not_found":
                return {"ok": False, "status": result.status, "msg": "指定的项目白名单规则不存在"}
            return {
                "ok": True,
                "status": result.status,
                "rule_type": result.rule_type,
                "normalized_value": result.normalized_value,
            }
        except (ValueError, permission_store.PermissionStoreError) as exc:
            return {"ok": False, "msg": str(exc)}

    # ------------------------------------------------------------------
    # 权限确认：统一走 core.permission_broker（与 CLI 同一套串行化机制）
    # ------------------------------------------------------------------
    def _undo_turn_impact_for_event(self, request) -> Dict[str, Any]:
        """undo_turn 权限事件的预检富化；其余工具返回空 dict。"""
        try:
            if request.function_name != "undo_turn":
                return {}
            from core.turn_undo import service as _tu_service

            return _tu_service.undo_turn_permission_impact(
                dict(request.arguments or {})
            )
        except Exception:
            return {}

    def _permission_event_payload(self, request) -> Dict[str, Any]:
        """构造权限请求事件 payload（permission_request 事件与 get_active_permission 快照共用）。"""
        from core.tool_presenter import describe_permission_request
        options = list(request.options or ())
        permission_options = [
            {"key": key, "label": label}
            for key, label in zip(request.option_keys, options)
        ]
        owner_map = getattr(self, "_turn_owner_sessions", {}) or {}
        return {
            "session_name": owner_map.get(request.parent_turn_id)
            or _session_from_turn_id(request.parent_turn_id),
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
            "permission_options": permission_options,
            # 按回合撤销：undo_turn 权限卡附带服务预检的影响摘要（只读）
            "undo_impact": self._undo_turn_impact_for_event(request),
            # 沙盒外访问等场景只给 同意/拒绝 两项，不渲染"总是允许"
            "allow_always": len(options) >= 3,
        }

    def get_active_permission(self) -> Dict[str, Any]:
        """返回当前未决的权限请求快照（与 permission_request 事件同构）；无则 permission=None。

        供移动端在初始化/切换会话/断线重连后补齐错过的事件，避免审批静默丢失。
        """
        try:
            req = permission_broker.active_request()
            if req is None:
                return {"ok": True, "permission": None}
            return {"ok": True, "permission": self._permission_event_payload(req)}
        except Exception as e:
            logger.exception("get_active_permission 失败")
            return {"ok": False, "msg": str(e)}

    def _activate_permission_request(self, request):
        """broker 激活某个请求 → 推送到前端对话流内联卡片，并挂一个"超时即拒绝"的定时器。"""
        # 星轨自动化（规格 5.5）：无人值守回合的权限请求立即自动拒绝——
        # 不弹卡、不等待。这与人工回合"绝不因用户暂时离开而自动拒绝"刻意
        # 不同：星轨自动化回合没有"用户回来"的时刻，等待只会永久挂死回合。
        # getattr 防御：裸实例测试桥可能未初始化该映射（同 _user_cancelled_sessions 先例）
        automation_label = getattr(self, "_automation_turn_labels", {}).get(request.parent_turn_id)
        if automation_label is None:
            # 子代理派生的权限请求 owner 是 task_id（非主回合 turn_id），
            # 按归属链判定其父回合是否星轨自动化回合（P1-2：否则无人值守会挂死）
            automation_label = self._automation_subagent_owner_label(request.parent_turn_id)
        if automation_label:
            self._auto_deny_automation_permission(request, automation_label)
            return
        try:
            self.emit_event("permission_request", self._permission_event_payload(request))
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
                        "session_name": self._turn_owner_sessions.get(request.parent_turn_id)
                        or _session_from_turn_id(request.parent_turn_id),
                        "message": "问题确认超时，已自动取消。",
                    })
            except Exception:
                logger.debug("提问超时处理失败", exc_info=True)

        timer = threading.Timer(PERMISSION_TIMEOUT_SECONDS, _timeout_cancel)
        timer.daemon = True
        with self._perm_lock:
            self._perm_timers[request.request_id] = timer
        timer.start()

    def _automation_subagent_owner_label(self, owner_id: str) -> Optional[Dict[str, str]]:
        """子代理权限请求归属判定：owner 是子代理 task_id 时，查其父回合是否
        星轨自动化回合（P1-2）。非子代理 owner 或父回合非自动化时返回 None，
        人工回合路径零影响。"""
        owner = str(owner_id or "").strip()
        if not owner or not getattr(self, "_automation_turn_labels", {}):
            return None
        try:
            from core.tools import subagent_manager

            for task in subagent_manager.snapshot():
                if str(getattr(task, "task_id", "")) != owner:
                    continue
                parent_turn = str(getattr(task, "parent_turn_id", "") or "")
                return self._automation_turn_labels.get(parent_turn)
        except Exception:
            logger.debug("子代理权限归属判定失败", exc_info=True)
        return None

    def _auto_deny_automation_permission(self, request, label: Dict[str, str]) -> None:
        """星轨自动化回合的权限请求自动拒绝：定位拒绝项，找不到则按取消收尾。"""
        task_name = str(label.get("task_name") or "星轨自动化任务")
        try:
            if request.function_name == "ask_user":
                # 提问类没有"拒绝项"概念：与人工回合超时语义一致走取消，
                # 绝不能把任意回答项当成"自动作答第 2 项"（P2-4）
                from core.permission_broker import CANCELLED_RESULT

                permission_broker.resolve(request.request_id, CANCELLED_RESULT)
                logger.info(
                    "星轨自动化任务「%s」无人值守已自动取消 ask_user 提问", task_name
                )
                self.emit_event("notice", {
                    "message": (
                        f"星轨自动化任务「{task_name}」无人值守已自动取消提问："
                        "模型可在无人应答时自行继续。"
                    ),
                })
                return
            deny_key = next(
                (
                    key
                    for key in (request.option_keys or ())
                    if str(key).lower() in {"deny", "reject", "no", "cancel", "disallow"}
                ),
                None,
            )
            if deny_key is not None:
                permission_broker.resolve_by_key(request.request_id, deny_key)
            elif len(request.options or ()) >= 2:
                # 旧式三选项请求的 option_keys 是 choice_0/1/2：索引 1 即"拒绝"，
                # 与 respond_permission 里用户点拒绝的语义完全一致
                permission_broker.resolve(request.request_id, 1)
            else:
                from core.permission_broker import CANCELLED_RESULT

                permission_broker.resolve(request.request_id, CANCELLED_RESULT)
            logger.info(
                "星轨自动化任务「%s」无人值守已自动拒绝权限请求：%s",
                task_name,
                request.function_name,
            )
            self.emit_event("notice", {
                "message": (
                    f"星轨自动化任务「{task_name}」无人值守已自动拒绝权限请求："
                    f"{request.function_name}。如需放行，请在任务设置中选择"
                    "accept_edits/full 档位。"
                ),
            })
        except Exception:
            logger.exception("自动化权限自动拒绝失败")

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
            # 兼容旧前端：默认选项索引为 0=同意、1=拒绝、2=本任务放行
            if not allow:
                choice = 1
            elif always:
                choice = 2
            else:
                choice = 0
            resolved = permission_broker.resolve(str(request_id), choice)
            if not resolved:
                return {"ok": False, "msg": "该权限请求已失效或已被处理"}
            self._cancel_perm_timer(str(request_id))
            return {"ok": True}
        except Exception as e:
            logger.exception("respond_permission 失败")
            return {"ok": False, "msg": str(e)}

    def respond_permission_choice(self, request_id: str, choice_index: int, custom_text: str = "") -> Dict[str, Any]:
        """按选项索引回答权限请求（ask_user 支持自由文本回答）。"""
        try:
            custom = str(custom_text or "").strip()
            if custom and permission_broker.active_function_name(str(request_id)) != "ask_user":
                # 纵深防御：非 ask_user 请求的授权选项与
                # 自由文本无对应关系，前端误传时按普通索引回答处理
                custom = ""
            resolved = permission_broker.resolve(
                str(request_id),
                int(choice_index),
                custom_answer=custom or None,
            )
            if not resolved:
                return {"ok": False, "msg": "该权限请求已失效或已被处理"}
            self._cancel_perm_timer(str(request_id))
            return {"ok": True}
        except Exception as e:
            logger.exception("respond_permission_choice 失败")
            return {"ok": False, "msg": str(e)}

    def respond_permission_option(self, request_id: str, option_key: str) -> Dict[str, Any]:
        try:
            resolved = permission_broker.resolve_by_key(
                str(request_id),
                str(option_key),
            )
            if not resolved:
                return {"ok": False, "msg": "该权限请求已失效、选项无效或已被处理"}
            self._cancel_perm_timer(str(request_id))
            return {"ok": True}
        except Exception as e:
            logger.exception("respond_permission_option 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 局域网手机远程控制（配对 token / 二维码 / TTL）
    # ------------------------------------------------------------------
    def _ensure_lan_server(self):
        """惰性创建并启动局域网远程控制服务，失败返回 None（不抛给调用方）。

        双检锁保证并发（启动后台线程 + 用户点二维码）只创建一个实例；start()
        成功回填真实端口后才发布 self._lan_server，避免并发方拿到 port=0 的
        半成品（二维码 URL 会变成 http://ip:0/）。
        """
        if self._lan_server is not None:
            return self._lan_server
        with self._lan_lock:
            if self._lan_server is not None:
                return self._lan_server
            try:
                from core.lan_server import LanServer

                srv = LanServer(bridge=self)
                data = config._load()
                ttl = data.get(
                    config.REMOTE_TOKEN_TTL_KEY, config.REMOTE_TOKEN_TTL_DEFAULT
                )
                srv.set_ttl(ttl)
                srv.start()
                self._lan_server = srv
                return srv
            except Exception as e:
                logger.exception("局域网远程控制服务启动失败")
                return None

    def _ensure_feishu_service(self):
        """惰性创建并启动飞书通道服务，失败/未配置返回 None（不抛给调用方）。

        与 _ensure_lan_server 同范式：双检锁保证并发（启动后台线程 + 设置页保存）
        只创建一个实例。未配置（开关关闭或缺凭据）时不创建任何线程，对现有功能
        零影响——这也是"飞书功能不影响其它功能"的兜底开关。
        """
        if getattr(self, "_feishu_service", None) is not None:
            return self._feishu_service
        lock = getattr(self, "_feishu_lock", None)
        if lock is None:
            # getattr 兜底：测试里的裸实例（__new__ 构造）不经过 __init__，
            # 缺状态时就地补齐，避免后台启动线程里抛 AttributeError
            lock = threading.Lock()
            self._feishu_lock = lock
        with lock:
            if getattr(self, "_feishu_service", None) is not None:
                return self._feishu_service
            try:
                from core.feishu.bridge import FeishuBridge
                from core.feishu.service import FeishuService

                svc = FeishuService(bridge=FeishuBridge(bridge=self))
                if not svc.start():
                    # 未配置：不发布实例，避免事件热路径上做无谓转发
                    return None
                self._feishu_service = svc
                return svc
            except Exception:
                logger.exception("飞书通道服务启动失败")
                return None

    def get_remote_bind_qr(self, force_new: bool = False, mobile: bool = False) -> Dict[str, Any]:
        """返回手机扫码绑定信息。

        token 固定：未过期直接复用；过期自动重签；force_new=True 由前端
        "刷新"按钮触发，强制重签并使旧 token 失效。
        mobile=True 绑定到独立移动端子应用（/m/ 路径，co-team 风格界面）；
        两种入口共享同一配对 token 与 WS 事件通道。
        """
        try:
            srv = self._ensure_lan_server()
            if srv is None:
                return {"ok": False, "msg": "远程服务启动失败"}
            return srv.get_bind_info(force_new=bool(force_new), path="/m/" if mobile else "/")
        except Exception as e:
            logger.exception("get_remote_bind_qr 失败")
            return {"ok": False, "msg": str(e)}

    def set_remote_token_ttl(self, seconds: int) -> Dict[str, Any]:
        """持久化远程控制 token 过期时间，并同步到已启动的服务。"""
        try:
            config.save_config_value(config.REMOTE_TOKEN_TTL_KEY, int(seconds))
            if self._lan_server is not None:
                self._lan_server.set_ttl(int(seconds))
            return {"ok": True, "settings": self.get_system_settings()}
        except Exception as e:
            logger.exception("set_remote_token_ttl 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 飞书通道设置
    # ------------------------------------------------------------------

    def get_feishu_settings(self) -> Dict[str, Any]:
        """飞书集成设置快照。**不含 app_secret 明文**，只回 has_secret 布尔。"""
        try:
            from core.feishu.config import load_feishu_config

            cfg = load_feishu_config()
            svc = getattr(self, "_feishu_service", None)
            if svc is not None:
                status = svc.status()
            else:
                from core.feishu.service import FeishuService

                status = FeishuService().status()
            return {
                "ok": True,
                "settings": {
                    "enabled": cfg.enabled,
                    "app_id": cfg.app_id,
                    "has_secret": cfg.has_secret,
                    "approvers": list(cfg.approvers),
                    "notify_chat_id": cfg.notify_chat_id,
                    "status": status,
                },
            }
        except Exception as e:
            logger.exception("get_feishu_settings 失败")
            return {"ok": False, "msg": str(e)}

    def get_feishu_secret(self) -> Dict[str, Any]:
        """按需返回 app_secret 明文（仅设置页用户主动点「显示」时调用）。"""
        try:
            from core.feishu.config import load_feishu_config

            return {"ok": True, "app_secret": load_feishu_config().app_secret}
        except Exception as e:
            logger.exception("get_feishu_secret 失败")
            return {"ok": False, "msg": str(e)}

    def set_feishu_settings(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """保存飞书设置；app_secret 写盘前加密（dpapi:/keyring:）。

        payload.app_secret 省略或为空字符串表示「不修改已有密钥」——避免设置页
        因掩码回显把密钥写坏。
        """
        payload = dict(payload or {})
        try:
            from core.credentials import protect_secret

            app_id = str(payload.get("app_id") or "").strip()
            approvers = [str(x).strip() for x in (payload.get("approvers") or []) if str(x).strip()]
            notify_chat_id = str(payload.get("notify_chat_id") or "").strip()
            enabled = bool(payload.get("enabled"))
            secret = str(payload.get("app_secret") or "")
            secret_provided = bool(secret.strip())

            def mutate(data: Dict[str, Any]) -> None:
                data[config.FEISHU_ENABLED_KEY] = enabled
                data[config.FEISHU_APP_ID_KEY] = app_id
                if secret_provided:
                    data[config.FEISHU_APP_SECRET_KEY] = protect_secret(secret.strip())
                data[config.FEISHU_APPROVERS_KEY] = approvers
                data[config.FEISHU_NOTIFY_CHAT_ID_KEY] = notify_chat_id

            config.update_config(mutate)
            self._refresh_feishu_service()
            snapshot = self.get_feishu_settings()
            return {"ok": True, "settings": snapshot.get("settings") or {}, "msg": ""}
        except Exception as e:
            logger.exception("set_feishu_settings 失败")
            return {"ok": False, "msg": str(e)}

    def test_feishu_connection(self, app_id: str = "", app_secret: str = "") -> Dict[str, Any]:
        """校验凭据：请求飞书长连接接入点，返回是否拿到连接地址。

        传入空值时用已保存的配置（密钥未填则用已存密钥），便于设置页「测试连接」。
        """
        try:
            from core.feishu._lark_ws import Client
            from core.feishu.config import load_feishu_config

            cfg = load_feishu_config()
            use_id = str(app_id or "").strip() or cfg.app_id
            use_secret = str(app_secret or "").strip() or cfg.app_secret
            if not use_id or not use_secret:
                return {"ok": False, "msg": "请先填写 App ID 与 App Secret"}
            client = Client(app_id=use_id, app_secret=use_secret, source="xingyao")
            url = client._get_conn_url()
            return {
                "ok": True,
                "msg": "凭据有效，飞书已返回长连接地址（保存后即开始连接）",
                "endpoint_ready": bool(url),
            }
        except Exception as e:
            logger.info("test_feishu_connection 失败: %s", e)
            return {"ok": False, "msg": f"连接失败：{e}"}

    def _refresh_feishu_service(self) -> None:
        """配置变更后让飞书服务按新配置生效（启动 / 停止 / 重连）。"""
        try:
            from core.feishu.config import load_feishu_config

            configured = load_feishu_config().configured
        except Exception:
            configured = False
        svc = getattr(self, "_feishu_service", None)
        if svc is None:
            if configured:
                self._ensure_feishu_service()
            return
        if not configured:
            try:
                svc.stop()
            finally:
                self._feishu_service = None
            return
        try:
            svc.restart()
        except Exception:
            logger.exception("重启飞书通道服务失败")

    # ------------------------------------------------------------------
    # 对话流
    # ------------------------------------------------------------------
    def send_user_message(self, text: str, session_name: Optional[str] = None, attachments: Optional[List[str]] = None, workdir: Optional[str] = None, model_id: Optional[str] = None, permission_mode: Optional[str] = None, turn_source: str = "local") -> Dict[str, Any]:
        # turn_source 是入口层受控回合来源（设计 18）：LAN dispatch 由服务器注入
        # "lan"，本地 GUI/CLI 固定 "local"，飞书通道由 core/feishu 注入 "feishu"；
        # 只接受这几个受控值，模型/用户正文无法设置，非法值直接拒绝（不启动回合）。
        # 注意参数顺序：permission_mode 在 turn_source 之前——桌面 GUI 经
        # pywebview 按位置传参，第 6 位必须是会话级权限档位；LAN 分发只放行
        # 前 5 个位置参数（MESSAGE_ENTRY_POSITIONAL_LIMIT），客户端同样无法
        # 经位置通道注入档位。
        if turn_source not in TURN_SOURCES:
            return {"ok": False, "msg": "无效的回合来源"}
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
            clean_model_id = str(model_id or "").strip()
            # 会话级权限档位：pending 草稿会话在落库前无法绑定，用户的选择随
            # 首条消息带给 worker 补绑定；未知值直接拒绝（宁可不发也不误放权）
            pending_permission_mode = ""
            raw_permission = str(permission_mode or "").strip()
            if raw_permission:
                if not app_context.is_known_permission_mode(raw_permission):
                    return {"ok": False, "msg": f"未知权限模式: {raw_permission}"}
                pending_permission_mode = app_context.normalize_permission_mode(raw_permission)
            with self._turn_start_lock:
                if self._is_turn_active(target_session):
                    # 回合运行中：消息进入排队（FIFO），回合结束由
                    # _run_turn_worker 的 finally 补发，不再直接拒绝丢弃
                    self._message_queues.setdefault(target_session, []).append({
                        "id": f"q_{uuid.uuid4().hex[:8]}",
                        "text": full_text,
                        "display_text": clean_text,
                        "attachments": attach_list,
                        "queued_at": time.time(),
                        # LAN 排队消息补发时保持来源（不得降级为 local）
                        "turn_source": turn_source,
                    })
                    # 快照在锁内发：保证与补发/取消等后续变更的顺序一致
                    self._emit_message_queue_snapshot(target_session)
                    return {"ok": True, "queued": True}

                # pending 新会话：先用首条消息前 12 字命名，立即生效，
                # 避免等整个回合跑完才由 _maybe_auto_rename 改名。
                # 注意：rename_session 要求会话已落库，故先 ensure_session 创建。
                if workdir and re.match(r"^session_[a-f0-9]{6}$", target_session or ""):
                    # 标题派生前先净化 @{会话名} 标记：直接把标记原文截进名字会
                    # 产生「@{xxx」这类残缺标题，令该会话永远无法被 @ 引用命中
                    from core.input_parser import sanitize_session_title
                    base_title = sanitize_session_title(clean_text)[:12].replace("\n", " ").strip()
                    if base_title:
                        try:
                            self._memory.ensure_session(target_session, workdir=str(workdir), source="GUI")
                            new_session = self._rename_session_to_title(target_session, base_title)
                            if new_session != target_session:
                                target_session = new_session
                        except Exception:
                            logger.debug("落库即命名失败", exc_info=True)
                # pending 草稿会话预选的模型随首条消息落库：草稿期不能调 set_model
                # （_current_session 还指向上一个真实会话，会污染旧会话绑定）。
                # 必须在 worker 线程读取模型前完成绑定，否则回合回退全局默认模型
                if workdir:
                    try:
                        self._memory.ensure_session(target_session, workdir=str(workdir), source="GUI")
                    except Exception:
                        logger.debug("落库即命名失败", exc_info=True)
                    # pending 草稿落库后必须把 _current_session 从旧会话切过来：
                    # 草稿期刻意不切（防 set_model 污染旧会话），但落库后仍停留
                    # 在旧会话会让 search_sessions 误把旧会话当当前会话排除、
                    # 同时混入真正在用的会话，@ 文件候选也会扫错工作区。
                    # 仅 local 入口切换——LAN 分发同样携带 workdir，不能抢本地会话。
                    if turn_source == "local" and self._current_session != target_session:
                        self._set_current_session(target_session)
                # 已存在会话也随消息刷新模型绑定：前端始终携带当前激活模型 id，
                # 让"选模型立即生效"，不再只在 pending 新会话时绑定
                if clean_model_id:
                    try:
                        # 防线：只接受配置中真实存在的模型 id。显示态一旦被陈旧
                        # 全局值污染（如设置快照回灌），不能让下一条消息把错误
                        # 绑定落成真实的会话模型
                        known_ids = {
                            str(m.get("id"))
                            for p in load_config().get("providers", [])
                            for m in p.get("models", [])
                        }
                        if clean_model_id in known_ids:
                            self._memory.set_session_model_id(target_session, clean_model_id)
                        else:
                            logger.warning(
                                "忽略未知模型绑定: session=%s model_id=%s",
                                target_session, clean_model_id,
                            )
                    except Exception:
                        logger.debug("会话模型绑定失败，回合回退默认模型", exc_info=True)

                # 用户主动发起新对话：重置该会话的自动汇总标志，
                # 下一批后台任务完成时可以再次触发自动汇总（与 CLI 行为一致）；
                # 同时解除"用户取消"标记，恢复自动汇总与计划续推
                # （清理解构在 _spawn_turn_worker 内，与排队补发路径同源）
                self._spawn_turn_worker(
                    full_text,
                    target_session,
                    display_text=clean_text,
                    pending_workdir=str(workdir) if workdir else "",
                    turn_source=turn_source,
                    attachments=attach_list,
                    pending_permission_mode=pending_permission_mode,
                )
            return {"ok": True}
        except Exception as e:
            logger.exception("send_user_message 失败")
            return {"ok": False, "msg": str(e)}

    def _spawn_turn_worker(self, full_text: str, target_session: str,
                           display_text: str = "", pending_workdir: str = "",
                           turn_source: str = "local",
                           attachments: Optional[List[str]] = None,
                           internal_kind: str = "",
                           automation_context: Optional[Dict[str, str]] = None,
                           pending_model_id: str = "",
                           pending_permission_mode: str = "") -> None:
        """启动一个新回合线程。调用方必须已持有 _turn_start_lock 且确认
        _is_turn_active(target_session) 为 False（检查+启动原子）。

        send_user_message 与排队补发（_drain_message_queue）共用本路径：
        后台汇总标志重置与"用户取消"解除对两者语义一致——排队消息也是
        用户主动发的新消息。turn_source 是入口层受控回合来源（local|lan），
        随消息透传到回合 worker（设计 18）。
        internal_kind 非空表示系统内部回合（星轨自动化任务/后台汇总）。
        automation_context 为星轨自动化回合上下文（run_id/task_name/
        permission_mode），见待开发/星轨自动化任务/01-完整设计规格.md 5.4。
        """
        with self._bg_summary_lock:
            self._bg_summary_triggered.discard(target_session)
        self._user_cancelled_sessions.discard(target_session)
        cancel_event = threading.Event()
        self._cancel_events[target_session] = cancel_event
        turn_kwargs: dict = {}
        if display_text:
            turn_kwargs["display_text"] = display_text
        if pending_workdir:
            # 懒创建会话：前端 pending 新会话在首条消息时才落库，
            # workdir 必须显式透传（进程 cwd 可能是别的项目目录）
            turn_kwargs["pending_workdir"] = str(pending_workdir)
        if pending_model_id:
            # 星轨自动化任务指定模型：worker 在读取激活模型前落会话绑定
            turn_kwargs["pending_model_id"] = str(pending_model_id)
        if pending_permission_mode:
            # 会话级权限档位（pending 草稿会话首条消息携带）：worker 落库后绑定
            turn_kwargs["pending_permission_mode"] = str(pending_permission_mode)
        turn_kwargs["turn_source"] = turn_source
        if attachments:
            turn_kwargs["attachments"] = attachments
        if internal_kind:
            turn_kwargs["internal_kind"] = internal_kind
        if automation_context:
            turn_kwargs["automation_context"] = automation_context
        worker = threading.Thread(
            target=self._run_turn_worker_serialized,
            args=(full_text, target_session),
            kwargs=turn_kwargs,
            daemon=True,
            name=f"gui-turn-{target_session}",
        )
        self._active_turns[target_session] = worker
        worker.start()

    def _message_queue_snapshot(self, session_name: str) -> List[Dict[str, Any]]:
        """排队消息的前端展示快照（展示文本用 display_text，不含 @路径）。"""
        queue = self._message_queues.get(session_name) or []
        return [
            {
                "id": str(item.get("id") or ""),
                "text": str(item.get("display_text") or item.get("text") or ""),
                "attachments": [str(p) for p in (item.get("attachments") or [])],
            }
            for item in queue
        ]

    def _emit_message_queue_snapshot(self, session_name: str) -> None:
        """推送某会话排队消息全量快照（前端幂等替换渲染，防漂移）。"""
        try:
            self.emit_event("message_queue", {
                "session_name": session_name,
                "queue": self._message_queue_snapshot(session_name),
            })
        except Exception:
            logger.debug("推送排队消息快照失败", exc_info=True)

    def _drain_message_queue(self, session_name: str) -> None:
        """回合结束后按 FIFO 补发该会话排队中的用户消息。

        只在回合线程 finally 尾部调用：此刻本回合已从 _active_turns 移除，
        若并发竞态下新回合已被 send_user_message 建立（_is_turn_active 为
        True），剩余队列留给那个回合自己的 finally 处理。
        """
        if self._shutting_down:
            return
        try:
            with self._turn_start_lock:
                if self._is_turn_active(session_name):
                    return
                queue = self._message_queues.get(session_name)
                if not queue:
                    return
                entry = queue.pop(0)
                # 星轨自动化：排队条目可能是自动化触发（忙时入队），补发时把
                # 自动化上下文透传回 worker；旧用户消息条目无这些键，零影响
                automation_run_id = str(entry.get("automation_run_id") or "")
                try:
                    self._spawn_turn_worker(
                        str(entry.get("text") or ""),
                        session_name,
                        display_text=str(entry.get("display_text") or ""),
                        # 排队消息补发保持原回合来源：LAN 消息不得降级为 local
                        turn_source=str(entry.get("turn_source") or "local"),
                        attachments=[str(p) for p in (entry.get("attachments") or [])],
                        # 星轨自动化任务指定的工作目录：补发时重判会话是否仍不存在，
                        # 已被其他回合创建的会话以自身归属为准
                        pending_workdir=self._automation_pending_workdir(
                            str(entry.get("pending_workdir") or ""), session_name
                        ),
                        # 任务指定模型随排队条目透传，补发回合同样生效
                        pending_model_id=str(entry.get("automation_model_id") or ""),
                        internal_kind="automation" if automation_run_id else "",
                        automation_context={
                            "run_id": automation_run_id,
                            "task_name": str(entry.get("automation_task_name") or ""),
                            "permission_mode": str(entry.get("automation_permission_mode") or ""),
                            "notify_on_success": str(entry.get("automation_notify_success") or ""),
                        } if automation_run_id else None,
                    )
                except Exception:
                    logger.exception("补发排队消息失败")
                    queue.insert(0, entry)
                    return
                if automation_run_id:
                    try:
                        self._automation_storage.mark_run_running(automation_run_id)
                    except Exception:
                        logger.exception("补发自动化运行置 running 失败")
                self._emit_message_queue_snapshot(session_name)
        except Exception:
            logger.exception("排队消息补发流程异常")

    # ------------------------------------------------------------------
    # 星轨自动化任务（设计规格：待开发/星轨自动化任务/01-完整设计规格.md）
    # ------------------------------------------------------------------
    def start_automation_engine(self) -> None:
        """gui_main 窗口就绪后调用：恢复遗留运行 → 启动调度线程 → 推送任务快照。"""
        if self._shutting_down:
            return
        try:
            recovered = self._automation_storage.recover_stale_runs()
            if recovered:
                logger.info("星轨自动化任务启动恢复：%s 条遗留运行标记为 interrupted", recovered)
        except Exception:
            logger.exception("星轨自动化任务启动恢复失败")
        self._automation_engine.start()
        self._emit_automation_tasks_changed()

    def _automation_pending_workdir(self, task_workdir: str, session_name: str) -> str:
        """星轨自动化任务的待建会话工作目录。

        仅当目标会话尚未落库时返回任务配置的 workdir（含 "GLOBAL" 自由对话），
        让回合 worker 懒创建会话时绑定到该目录；已有会话一律返回空串——
        会话自身登记的 workdir 是权威归属，派发绝不改写（与 send_user_message
        的 pending_workdir 语义一致，后者也只服务懒创建）。
        """
        raw = str(task_workdir or "").strip()
        if not raw:
            return ""
        try:
            for s in self._memory.get_all_sessions(workdir=None, include_empty=True):
                if s.get("name") == session_name:
                    return ""
        except Exception:
            logger.debug("查询自动化目标会话归属失败", exc_info=True)
            return ""
        return raw

    def _dispatch_automation_run(self, task: dict, run_id: str, trigger_kind: str) -> None:
        """引擎/手动触发共用：把任务提示词投递为目标会话的一次真实回合。

        会话忙时进入既有消息排队（FIFO，回合结束自动补发）；空闲时立即启动
        内部回合（internal_kind="automation"，turn_start 带 internal 标记）。
        全程持 _turn_start_lock，与 send_user_message 的"检查+启动"原子语义一致。
        本方法在引擎线程或 js_api 线程执行，只做判定与登记，不做模型请求。
        """
        if self._shutting_down:
            return
        target_session = str(task.get("target_session") or "").strip() or "星轨自动化任务"
        display_text = f"星轨自动化任务「{task.get('name')}」"
        # 任务指定的工作目录只对"懒创建的新会话"生效；已有会话沿用自身归属
        pending_workdir = self._automation_pending_workdir(
            str(task.get("workdir") or ""), target_session
        )
        # 任务指定的执行模型：worker 在读取激活模型前落会话绑定，本回合即用
        # 该模型执行；模型已被删除时 get_active_model 自动回退全局默认
        task_model_id = str(task.get("model_id") or "").strip()
        automation_context = {
            "run_id": run_id,
            "task_name": str(task.get("name") or ""),
            "permission_mode": str(task.get("permission_mode") or "confirm"),
            "notify_on_success": "1" if task.get("notify_on_success") else "",
        }
        with self._turn_start_lock:
            if self._is_turn_active(target_session):
                self._message_queues.setdefault(target_session, []).append({
                    "id": f"q_{uuid.uuid4().hex[:8]}",
                    "text": str(task.get("prompt") or ""),
                    "display_text": display_text,
                    "attachments": [],
                    "queued_at": time.time(),
                    # 自动化排队条目：补发时透传回 worker（运行状态 queued→running）
                    "turn_source": "local",
                    "automation_run_id": run_id,
                    "automation_task_name": automation_context["task_name"],
                    "automation_permission_mode": automation_context["permission_mode"],
                    "automation_notify_success": automation_context["notify_on_success"],
                    # 补发时目标会话可能已被别的回合创建，drain 侧会重判
                    "pending_workdir": pending_workdir,
                    # 任务指定模型随排队条目透传，补发回合同样生效
                    "automation_model_id": task_model_id,
                })
                # 快照在锁内发：与 send_user_message 排队路径同序
                self._emit_message_queue_snapshot(target_session)
                return
            try:
                self._automation_storage.mark_run_running(run_id)
            except Exception:
                logger.exception("自动化运行置 running 失败")
            try:
                self._spawn_turn_worker(
                    str(task.get("prompt") or ""),
                    target_session,
                    display_text=display_text,
                    pending_workdir=pending_workdir,
                    pending_model_id=task_model_id,
                    turn_source="local",
                    internal_kind="automation",
                    automation_context=automation_context,
                )
            except Exception as exc:
                # spawn 失败（线程创建异常等）：立刻置 error 终态，避免运行行
                # 永久停在 running 导致后续触发全部被单飞跳过（P2-6）
                logger.exception("星轨自动化回合 spawn 失败")
                try:
                    self._automation_storage.finish_run(
                        run_id, "error", f"回合线程启动失败: {exc}"[:500]
                    )
                except Exception:
                    logger.exception("自动化运行错误终态写入失败")

    def _on_automation_run_updated(self, run: dict) -> None:
        """运行行任何状态变化（存储层统一回调）→ 推事件与跳过通知。"""
        try:
            self.emit_event("automation_run_updated", {"run": run})
            if run.get("status") in ("queued", "skipped"):
                # 认领推进了 next_run_at/last_run_at，同步刷新任务快照
                # （规格 11.2；P2-3），否则列表倒计时到下一次编辑才更新
                self._emit_automation_tasks_changed()
            if run.get("status") == "skipped":
                self.emit_event("notice", {
                    "message": (
                        f"星轨自动化任务「{run.get('task_name')}」本次触发已跳过："
                        f"{run.get('error') or '上一次运行尚未结束'}"
                    ),
                })
        except Exception:
            logger.debug("推送自动化运行事件失败", exc_info=True)

    def _emit_automation_tasks_changed(self) -> None:
        """推送星轨自动化任务全量快照（前端幂等替换渲染）。"""
        try:
            self.emit_event("automation_tasks_changed", {"tasks": self._automation_task_dtos()})
        except Exception:
            logger.debug("推送星轨自动化任务快照失败", exc_info=True)

    def _automation_task_dtos(self) -> List[Dict[str, Any]]:
        from core.automation.models import trigger_summary

        dtos: List[Dict[str, Any]] = []
        for task in self._automation_storage.list_tasks():
            dto = dict(task)
            dto["trigger_summary"] = trigger_summary(task["trigger_type"], task["trigger_config"])
            # SQLite 行直出 0/1，归一为布尔（P2-7：与 pywebview.d.ts 声明一致）
            dto["enabled"] = bool(dto.get("enabled"))
            dto["notify_on_success"] = bool(dto.get("notify_on_success"))
            dtos.append(dto)
        return dtos

    def _register_automation_service(self) -> None:
        """把桥侧自动化能力注册为 core 层服务句柄（Agent 工具的唯一入口）。

        所有回调都指向本桥已有的 js_api/存储实现：Agent 工具经
        ``core.automation.service.get()`` 取到句柄后调用同名方法，即复用桥侧
        既有的参数校验、任务上限检查与事件推送（automation_tasks_changed /
        automation_run_updated），不在 core 层重复实现一遍这些能力。
        """
        from core.automation import service as automation_service

        automation_service.register(automation_service.AutomationService(
            list_tasks=self.get_automation_tasks,
            get_task=self._automation_storage.get_task,
            save_task=self.save_automation_task,
            delete_task=self.delete_automation_task,
            set_enabled=self.set_automation_task_enabled,
            run_now=self.run_automation_task_now,
            get_runs=self.get_automation_runs,
        ))

    def get_automation_tasks(self) -> Dict[str, Any]:
        try:
            return {"ok": True, "tasks": self._automation_task_dtos()}
        except Exception as e:
            logger.exception("读取星轨自动化任务失败")
            return {"ok": False, "msg": str(e)}

    def save_automation_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if self._shutting_down:
                return {"ok": False, "msg": "应用正在退出"}
            from core.automation.models import MAX_TASKS, validate_task

            raw = dict(task or {})
            errors = validate_task(raw)
            if errors:
                return {"ok": False, "errors": errors}
            # 指定模型必须真实存在于配置（与 send_user_message 的 known_ids 防线同源）；
            # 保存后模型被删除的场景由派发端兜底：绑定陈旧 id 时 get_active_model
            # 自动回退全局默认，任务不会被卡死
            task_model_id = str(raw.get("model_id") or "").strip()
            if task_model_id:
                known_ids = {
                    str(m.get("id"))
                    for p in load_config().get("providers", [])
                    for m in p.get("models", [])
                }
                if task_model_id not in known_ids:
                    return {"ok": False, "errors": ["指定模型不存在或已被删除，请重新选择"]}
            existing_id = str(raw.get("id") or "").strip()
            if not existing_id and self._automation_storage.count_tasks() >= MAX_TASKS:
                return {"ok": False, "errors": [f"星轨自动化任务最多 {MAX_TASKS} 个，请先删除部分任务"]}
            task_id = self._automation_storage.save_task(raw)
            self._emit_automation_tasks_changed()
            self._automation_engine.notify_changed()
            return {"ok": True, "id": task_id}
        except Exception as e:
            logger.exception("保存星轨自动化任务失败")
            return {"ok": False, "msg": str(e)}

    def delete_automation_task(self, task_id: str) -> Dict[str, Any]:
        try:
            deleted = self._automation_storage.delete_task(str(task_id or ""))
            self._emit_automation_tasks_changed()
            self._automation_engine.notify_changed()
            return {"ok": True, "deleted": bool(deleted)}
        except Exception as e:
            logger.exception("删除星轨自动化任务失败")
            return {"ok": False, "msg": str(e)}

    def set_automation_task_enabled(self, task_id: str, enabled: bool) -> Dict[str, Any]:
        try:
            updated = self._automation_storage.set_enabled(str(task_id or ""), bool(enabled))
            self._emit_automation_tasks_changed()
            self._automation_engine.notify_changed()
            return {"ok": bool(updated), "msg": "" if updated else "任务不存在"}
        except Exception as e:
            logger.exception("启停星轨自动化任务失败")
            return {"ok": False, "msg": str(e)}

    def run_automation_task_now(self, task_id: str) -> Dict[str, Any]:
        try:
            if self._shutting_down:
                return {"ok": False, "msg": "应用正在退出"}
            task = self._automation_storage.get_task(str(task_id or ""))
            if task is None:
                return {"ok": False, "msg": "任务不存在"}
            # 原子单飞：锁内 check+insert（P2-1），并发双击只会有一个成功创建
            run = self._automation_storage.create_manual_run_if_idle(task)
            if run is None:
                return {"ok": False, "msg": "上一次运行尚未结束，请稍后再试"}
            try:
                self._dispatch_automation_run(task, run["id"], "manual")
            except Exception as exc:
                # 分发异常同样不留 stuck 行（P2-6）
                try:
                    self._automation_storage.finish_run(
                        run["id"], "error", f"分发失败: {exc}"[:500]
                    )
                except Exception:
                    logger.exception("自动化运行错误终态写入失败")
                raise
            return {"ok": True, "run_id": run["id"]}
        except Exception as e:
            logger.exception("手动执行星轨自动化任务失败")
            return {"ok": False, "msg": str(e)}

    def get_automation_runs(self, task_id: str = "", limit: int = 50) -> Dict[str, Any]:
        try:
            runs = self._automation_storage.list_runs(
                str(task_id or "") or None, limit=max(1, min(int(limit or 50), 200))
            )
            return {"ok": True, "runs": runs}
        except Exception as e:
            logger.exception("读取自动化运行历史失败")
            return {"ok": False, "msg": str(e)}

    def clear_automation_runs(self, task_id: str) -> Dict[str, Any]:
        try:
            self._automation_storage.clear_runs(str(task_id or ""))
            return {"ok": True}
        except Exception as e:
            logger.exception("清空自动化运行历史失败")
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

    def get_message_queue(self, session_name: str = "") -> Dict[str, Any]:
        """查询指定会话当前排队中的用户消息（前端会话切换/重载时拉取）。"""
        target = session_name or self._current_session
        return {
            "ok": True,
            "session_name": target,
            "queue": self._message_queue_snapshot(target),
        }

    def cancel_queued_message(self, session_name: str, message_id: str) -> Dict[str, Any]:
        """取消一条排队中的消息：仅从队列移除，不影响正在执行的回合。"""
        try:
            target = session_name or self._current_session
            message_id = str(message_id or "")
            with self._turn_start_lock:
                queue = self._message_queues.get(target) or []
                remaining = [m for m in queue if m.get("id") != message_id]
                removed = len(queue) - len(remaining)
                if removed:
                    if remaining:
                        self._message_queues[target] = remaining
                    else:
                        self._message_queues.pop(target, None)
                    self._emit_message_queue_snapshot(target)
            return {"ok": True, "removed": removed}
        except Exception as e:
            logger.exception("cancel_queued_message 失败")
            return {"ok": False, "msg": str(e)}

    def jump_queue(self, session_name: str, message_id: str) -> Dict[str, Any]:
        """插队发送：把排队消息移到队首并停止当前正在执行的回合；
        回合结束后 _drain_message_queue 立即补发队首（即刚插队的这条），
        其余排队消息顺位后移不受影响。
        """
        try:
            target = session_name or self._current_session
            message_id = str(message_id or "")
            with self._turn_start_lock:
                queue = self._message_queues.get(target) or []
                entry = next((m for m in queue if m.get("id") == message_id), None)
                if entry is None:
                    return {"ok": False, "msg": "该消息已不在排队中"}
                queue.remove(entry)
                queue.insert(0, entry)
                self._emit_message_queue_snapshot(target)
                turn_active = self._is_turn_active(target)
            if turn_active:
                # 停止当前回合（取消事件 + 硬中断在飞请求等完整语义），
                # 不直接起新回合：旧回合的 finally 会原子地补发队首消息
                self.stop_current_turn(target)
            return {"ok": True}
        except Exception as e:
            logger.exception("jump_queue 失败")
            return {"ok": False, "msg": str(e)}

    def _run_turn_worker_serialized(self, user_text: str, session_name: str, *args, **kwargs):
        """多会话并行：线程本地 workdir 上下文（core.workspace_context）已替代
        进程级 os.chdir，回合不再需要 _turn_workdir_lock 串行化。
        保留函数名避免改动所有调用点，实现直接转发到 _run_turn_worker。"""
        return self._run_turn_worker(user_text, session_name, *args, **kwargs)

    def _run_turn_worker(self, user_text: str, session_name: str, internal_kind: str = "", display_text: str = "", pending_workdir: str = "", turn_source: str = "local", attachments: Optional[List[str]] = None, automation_context: Optional[Dict[str, str]] = None, pending_model_id: str = "", pending_permission_mode: str = ""):
        """后台线程：组装上下文 → 调用模型 → 翻译事件推送给前端。

        internal_kind 非空表示这是系统自动发起的内部回合（后台任务汇总 /
        星轨自动化任务），turn_start 事件会带上 internal / display_text，
        让前端用系统提示样式展示而不是把内部指令原文当成用户气泡。
        turn_source 是入口层受控回合来源（local|lan，设计 18）：用户消息回合
        由 send_user_message 传入（LAN dispatch 注入 "lan"），内部回合固定
        local；透传到 prepare_turn_messages 与模型事件入口决定电脑操控能力。
        automation_context 非空表示星轨自动化回合：运行终态回写 automation_runs
        表（completed/cancelled/error），无人值守权限档位按任务配置覆盖
        （规格 5.4/5.5）。
        """
        api_key = ""
        turn_finished = False
        event_session_name = session_name
        cancelled = False
        turn_id = ""
        automation_run_id = str((automation_context or {}).get("run_id") or "")
        automation_task_name = str((automation_context or {}).get("task_name") or "")
        automation_permission_mode = str((automation_context or {}).get("permission_mode") or "")
        automation_error: Optional[str] = None
        # 闲时维护：回合开始即撤销本会话的空闲维护任务并协作中断在跑的
        # 闲时压缩（见 core/idle_maintenance.py），结束在 finally 重新计时
        try:
            from core.idle_maintenance import idle_maintenance_service
            idle_maintenance_service.on_turn_started(session_name)
        except Exception:
            pass
        # 本回合起点消息 id（turn_start 事件后记录）：异常/取消回滚后 history
        # 末尾会退化成上一轮的助手消息，补发全文必须按此边界过滤。必须在
        # finish_once 定义之前初始化，闭包才能读到；取值失败退回 None（旧行为）。
        turn_start_msg_id: Optional[int] = None
        # 线程本地 workdir 作用域：绑定后必须在本回合 finally 中退出恢复，
        # 防止主线程直调（测试）或未来线程复用场景下 workdir 泄漏到下一回合
        _workdir_scope = None
        # 线程本地权限档位作用域：与 workdir 同理，回合期间冻结会话生效档位，
        # 供鉴权路径（app_context.get_permission_mode）读取，退出时恢复
        _permission_scope = None
        error_state = {
            "toast_messages": set(),
            "terminal_message": None,
        }
        cancel_event = self._cancel_events.get(session_name) or self._get_cancel_event(session_name)

        # —— 回合收尾诊断（定位「内容已输出完但前端一直显示生成中」）——
        # 收尾链任一环（撤销封闭/落库/delta 冲刷/finish_once/JS 投递）耗时过长，
        # 前端 running 都会一直为真。_phase_mark 输出各步耗时；看门狗在模型流
        # 结束、进入收尾链后启动，超时仍未发出 turn_finish 就 dump 全部线程栈。
        _phase_t0 = time.monotonic()
        _phase_last = _phase_t0
        _turn_watchdog_stop: Optional[threading.Event] = None
        _FINISH_WATCHDOG_SECONDS = 5.0

        def _phase_mark(label: str) -> None:
            nonlocal _phase_last
            _now = time.monotonic()
            logger.info(
                "回合计时 [%s] %s：本步 %.0fms，累计 %.0fms",
                session_name, label,
                (_now - _phase_last) * 1000.0,
                (_now - _phase_t0) * 1000.0,
            )
            _phase_last = _now

        def _start_finish_watchdog() -> None:
            """模型流已结束、进入收尾链后启动：收尾超时则 dump 全部线程栈。

            正常收尾应在毫秒级完成；超过阈值说明收尾链被 IO/锁/JS 投递拖住，
            线程栈能直接指出卡在哪一行，无需事后猜测。
            """
            nonlocal _turn_watchdog_stop
            if _turn_watchdog_stop is not None:
                return
            _turn_watchdog_stop = threading.Event()
            _stop = _turn_watchdog_stop

            def _watch() -> None:
                if _stop.wait(_FINISH_WATCHDOG_SECONDS):
                    return
                try:
                    import sys as _sys
                    import traceback as _tb

                    logger.warning(
                        "回合收尾看门狗 [%s]：turn_finish 超过 %.0f 秒未发出，抓取线程栈",
                        session_name, _FINISH_WATCHDOG_SECONDS,
                    )
                    for _tid, _frame in _sys._current_frames().items():
                        logger.warning(
                            "线程栈 tid=%s：\n%s", _tid, "".join(_tb.format_stack(_frame))
                        )
                except Exception:
                    logger.exception("回合收尾看门狗抓取线程栈失败")

            threading.Thread(
                target=_watch, daemon=True, name="gui-turn-finish-watchdog"
            ).start()

        def finish_once():
            """turn_finish 只允许发出一次（历史实现在 end + finally 各发一次）。"""
            nonlocal turn_finished
            if turn_finished:
                return
            turn_finished = True
            from core.plan import maybe_clear_completed_plan
            # 必须显式传 session_name：此处 TLS 计划路由已在 finally 前段解绑，
            # 裸调会退回进程级单例——多会话并行下该单例可能已被切换会话重绑
            # （_set_current_session），清错会话的计划。注册表路由（回合内已
            # 创建本会话条目，跨回合存活）保证取到正确的管理器实例。
            # 清空失败（磁盘满/权限等落盘 IO 异常）只记录日志，绝不阻断
            # turn_finish——幂等标志已置位，异常逃逸会让 turn_finish 永久丢失
            # （前端输入框不解锁），finally 尾部的补查与排队消息补发也被跳过。
            try:
                if maybe_clear_completed_plan(session_name):
                    self.emit_event("plan_updated", {
                        "title": "当前任务计划",
                        "items": [],
                        "session_name": event_session_name,
                    })
            except Exception:
                logger.debug("回合结束清空已完成计划失败", exc_info=True)
            # 自愈校正：流式 delta 经 evaluate_js 投递可能静默丢失（渲染进程繁忙
            # 时执行失败此前只记 debug），前端消息纯靠 delta 累加、尾部一丢就永久
            # 缺失。turn_finish 前补发 message_final 携带最后一条助手消息全文，
            # 前端仅在全文更长时覆盖——正常路径是无害 no-op，丢包路径自我修复。
            # 必须带回合边界 turn_start_msg_id：异常/取消回滚后 history 末尾会
            # 退化成上一轮的助手消息，不过滤就会把上一轮全文补发进本轮气泡。
            _finish_t0 = time.monotonic()
            self._emit_message_final(event_session_name, after_msg_id=turn_start_msg_id)
            self.emit_event("turn_finish", {"session_name": event_session_name})
            if _turn_watchdog_stop is not None:
                _turn_watchdog_stop.set()
            _js_queue = getattr(self, "_js_queue", None)
            logger.info(
                "回合计时 [%s] turn_finish 已入队：finish_once 内 %.0fms（JS 队列剩余 %d 条）",
                event_session_name,
                (time.monotonic() - _finish_t0) * 1000.0,
                _js_queue.qsize() if _js_queue is not None else -1,
            )
            self._emit_token_usage()
            self._clear_session_last_error(session_name)

        # 多会话并行：不再用全局 _task_status 表示"执行中"——它会被另一会话回合结束时的
        # idle 覆盖，也会让仍在跑的会话状态错乱。各会话的活跃状态由 self._active_turns
        # 字典承载（线程已在 send_user_message 中存入）。_task_status 仅在全部回合结束
        # 时恢复 idle，保持 _GuiContext.task_status 兼容。
        # self._task_status = "executing"
        self._clear_session_last_error(session_name)
        turn_start_data = {"user_text": user_text, "session_name": session_name}
        if display_text and display_text != user_text:
            turn_start_data["display_text"] = display_text
        if internal_kind:
            turn_start_data["internal"] = internal_kind
            if internal_kind == "automation":
                # 星轨自动化：展示名随任务传入（"星轨自动化任务「X」"）
                turn_start_data["display_text"] = display_text or "星轨自动化任务"
            elif internal_kind == "bg_command_summary":
                turn_start_data["display_text"] = _BG_COMMAND_SUMMARY_DISPLAY_TEXT
            else:
                turn_start_data["display_text"] = _BG_SUMMARY_DISPLAY_TEXT
        with self._active_turn_lock:
            self._active_turn_snapshots[session_name] = {
                "session_name": session_name,
                "user_text": user_text,
                "display_text": display_text or user_text,
                "started_at_ms": int(time.time() * 1000),
                "thinking_text": "",
                "streaming_text": "",
                "tool_calls": [],
                "internal_kind": internal_kind,
            }
        # 按回合撤销生命周期（规格 9.1）：turn_start 事件与用户消息落库都
        # 推迟到持久化检查点创建之后；回合边界消息 id 在 worker 起点读取
        # （早于本回合任何落库行为），保证检查点创建前的异常路径也能正确过滤补发。
        turn_start_msg_id = None
        try:
            turn_start_msg_id = self._memory.get_last_message_id(session_name)
        except Exception:
            logger.debug("读取回合起点消息 id 失败，message_final 退回不过滤", exc_info=True)
            turn_start_msg_id = None
        turn_id = ""
        undo_ctx = None
        undo_finished = False

        try:
            # 星轨自动化任务指定模型：在读取激活模型前落会话与模型绑定，本回合即用
            # 该模型执行。新会话按任务 workdir 同步落库（无则挂 GLOBAL），否则
            # set_session_model_id 无行可更；指定模型已被删除时 get_active_model
            # 按陈旧绑定自动回退全局默认，任务不被卡死。绑定与聊天页"选模型后
            # 发消息"语义一致：持久落在会话上，会话后续回合沿用
            if pending_model_id:
                try:
                    self._memory.ensure_session(
                        session_name, workdir=pending_workdir or "GLOBAL", source="GUI"
                    )
                    self._memory.set_session_model_id(session_name, pending_model_id)
                except Exception:
                    logger.debug("预绑定星轨自动化任务模型失败", exc_info=True)
            active_model = get_active_model(session_name)
            if not active_model or not active_model.get("litellm_model"):
                # 关键修复：没有激活模型时也必须让前端解锁输入框
                if automation_run_id:
                    automation_error = "当前未激活任何 LLM 模型"
                self.emit_event("error", {"message": "当前未激活任何 LLM 模型，请先在模型管理中配置并激活模型。", "session_name": session_name})
                self.emit_event("turn_cancelled", {"session_name": session_name})
                finish_once()
                return

            model_name = active_model["litellm_model"]
            api_key = active_model.get("api_key", "")
            api_base = active_model.get("base_url", "")
            ctx_win = active_model.get("context_window", 128000)
            # 首轮必须先创建会话再写模型绑定；后续模型未变化时不重复写库。
            session_workdir = pending_workdir or self._session_workdir(session_name)
            _ensure_session_model_binding(
                self._memory, session_name, active_model.get("id"), session_workdir
            )
            # 首条消息携带的会话级权限档位：pending 草稿期无法落库绑定，此刻
            # 会话行已确保存在，补绑定让本回合即用该档位（后续回合沿用）
            if pending_permission_mode:
                try:
                    self._memory.set_session_permission_mode(session_name, pending_permission_mode)
                except Exception:
                    logger.debug("绑定会话权限档位失败", exc_info=True)
            # 显式传会话名：此刻线程 TLS 尚未绑定本会话（set_thread_session 在
            # 下文才调用），不传会把全局模型同步给子代理继承链
            self._sync_active_model_context(session_name)

            # 会话归属目录是每一轮 Agent 的权威工作目录。待发送项目会话虽然
            # 已把 workdir 透传到这里，但进程 cwd 仍可能停在启动目录或上一会话；
            # 必须在解析附件、组装运行上下文和执行任何工具前完成绑定。
            turn_workdir = session_workdir
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
            # 权限档位进入鉴权路径：会话级档位此前只落在提示词文本与 DB 列，
            # 鉴权路径（app_context.get_permission_mode）读的是全局值，导致
            # 会话级完整访问（full）在工具调用时不被认可、反复弹确认。这里
            # 把本回合生效档位绑定到线程，回合期间冻结，退出时恢复全局值。
            _permission_scope = app_context.permission_mode_scope(
                automation_permission_mode
                or self._effective_permission_mode(session_name)
            )
            _permission_scope.__enter__()

            # 会话已在模型绑定阶段创建；此处直接解析用户输入并入库。
            try:
                from core.input_parser import parse_user_input, extract_session_refs
                if internal_kind:
                    # 内部回合（后台汇总/命令总结）：prompt 是系统构造的，不需要
                    # @文件 / 路径挂载；跳过 parse_user_input，避免把日志里的
                    # 盘符路径（如 dist\Y-code-1.2.2.exe）误当成附件挂载
                    parsed_content, attached_msgs = user_text, []
                    session_refs = []
                else:
                    # LAN 回合按全局约束不解析 @{会话名}（prepare_turn_messages
                    # 咽喉点还有第二道强制置空）；@文件解析仍正常进行。
                    if turn_source == "local":
                        _, session_refs = extract_session_refs(user_text)
                    else:
                        session_refs = []
                    parsed_content, attached_msgs = parse_user_input(
                        user_text, explicit_paths=attachments, session_id=session_name
                    )
                    if session_refs:
                        attached_msgs = list(attached_msgs) + [
                            f"💬 已引用对话: {name}" for name in session_refs
                        ]
            except Exception:
                logger.debug("解析用户输入失败，退回纯文本", exc_info=True)
                parsed_content, attached_msgs = user_text, []
                session_refs = []
            # --- 按回合撤销生命周期重排（规格 9.1）---
            # 1) 生成全局唯一不透明 turn_id（完整 128 位随机 UUID，不再嵌会话名）
            turn_id = uuid.uuid4().hex
            self._active_turn_ids[session_name] = turn_id
            with self._turn_owner_lock:
                self._turn_owner_sessions[turn_id] = session_name
            # 星轨自动化：登记 turn_id → 任务名（无人值守权限自动拒绝依据），
            # 并把 turn_id 回填到运行记录供追踪
            if automation_run_id:
                self._automation_turn_labels[turn_id] = {
                    "run_id": automation_run_id,
                    "task_name": automation_task_name,
                }
                try:
                    self._automation_storage.set_run_turn_id(automation_run_id, turn_id)
                except Exception:
                    logger.debug("回填自动化运行 turn_id 失败", exc_info=True)
            # 2) 创建持久化检查点（失败则本轮不落任何消息、不发 turn_start）
            from core.turn_undo import service as _tu_service

            _tu_checkpoint, undo_ctx = _tu_service.begin_turn(
                session_id=_tu_service.resolve_session_id(session_name),
                turn_id=turn_id,
                workspace_root=turn_workdir,
                description=str(user_text)[:80],
            )
            # 3) 用户消息与 message_turn_links 原子落库
            self._memory.add_message(
                session_name, "user", parsed_content,
                turn_checkpoint_id=_tu_checkpoint.checkpoint_id,
                turn_position_kind="user",
            )
            # 4) 发出携带稳定身份的 turn_start（session_id + turn_id）
            turn_start_data["turn_id"] = turn_id
            turn_start_data["session_id"] = _tu_checkpoint.session_id
            self.emit_event("turn_start", turn_start_data)
            if attached_msgs:
                self.emit_event("user_attachment_notes", {
                    "session_name": session_name,
                    "notes": [str(note) for note in attached_msgs],
                })
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
                # 星轨自动化：无人值守档位按任务配置覆盖（规格 5.5）；其余回合
                # 用会话级绑定档位，未绑定回退全局默认；人工回合零改动
                permission_mode=automation_permission_mode or self._effective_permission_mode(session_name),
                cancel_event=cancel_event,
                notify=lambda kind, message: self.emit_event("notice", {"message": _strip_ansi_markup(message)}),
                api_key=api_key,
                api_base=api_base,
                is_gui=True,
                turn_source=turn_source,
                referenced_sessions=session_refs,
            )

            # 按回合撤销检查点已在用户消息落库前创建（undo_ctx）；
            # 历史压缩后重算窗口占用并推送，让上下文圆圈及时回落。
            # 组装刚写入的来源占比快照必须保留（那是本轮最新数据，不是旧会话
            # 残留）——否则下方推送的面板数据永远为空（2026-09-23 修复）。
            self._recalc_token_usage(session_name, clear_breakdown_snapshot=False)
            self._emit_token_usage()

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
                    model_id=active_model.get("id"),
                    error_state=error_state,
                    turn_source=turn_source,
                )
            finally:
                # 模型流已结束、进入收尾链：打点并启动看门狗（正常/异常路径都覆盖）
                _phase_mark("模型回合结束（_consume_turn_events 返回）")
                _start_finish_watchdog()
                poller_stop.set()
                try:
                    from core.turn_undo import service as _tu_service

                    if not cancelled:
                        self._memory.mark_turn_final_assistant(
                            _tu_checkpoint.checkpoint_id
                        )
                except Exception:
                    logger.debug("更新回合最终 assistant 关联失败", exc_info=True)
                try:
                    from core.turn_undo import service as _tu_service

                    _tu_service.finish_turn(
                        undo_ctx,
                        aborted=bool(cancelled),
                        reason="回合被用户取消" if cancelled else "",
                    )
                    undo_finished = True
                except Exception:
                    logger.exception("封闭按回合撤销检查点失败")

            _phase_mark("模型流结束、撤销检查点已封闭")
            if cancelled:
                self.emit_event("turn_cancelled", {"session_name": session_name})
            else:
                event_session_name = self._maybe_auto_rename(session_name) or session_name
            _phase_mark("取消事件/自动重命名完成")
        except Exception as e:
            logger.exception("GUI 对话回合失败")
            if automation_run_id:
                automation_error = str(e)
            # 星轨修复：begin_turn 之后、poller 内层 finally 之前抛出的异常
            # （消息落库/上下文组装等）原本永远不会走 finish_turn，活动检查点
            # 泄漏会把该工作区永久阻塞为 workspace_busy。此处兜底按异常终止封闭。
            if undo_ctx is not None and not undo_finished:
                try:
                    from core.turn_undo import service as _tu_service

                    _tu_service.finish_turn(undo_ctx, aborted=True, reason="回合启动阶段异常终止")
                    undo_finished = True
                except Exception:
                    logger.exception("异常路径封闭按回合撤销检查点失败")
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
            if _permission_scope is not None:
                _permission_scope.__exit__(None, None, None)
            # 身份校验清理 cancel 事件：finally 竞态窗口内（本线程还没走完
            # finally，但 _active_turns 已 pop）新回合可能已建立同名事件，
            # 仅当字典里仍是本回合自己的事件对象时才删除，防误删新回合事件
            if self._cancel_events.get(session_name) is cancel_event:
                self._cancel_events.pop(session_name, None)
            # 从活跃回合字典中移除本会话
            self._active_turns.pop(session_name, None)
            self._active_turn_ids.pop(session_name, None)
            if turn_id:
                with self._turn_owner_lock:
                    self._turn_owner_sessions.pop(turn_id, None)
            with self._active_turn_lock:
                self._active_turn_snapshots.pop(session_name, None)
            self._finalize_session_deltas(session_name)
            _phase_mark("delta 缓冲收尾完成")
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
            _phase_mark("finally 清理完成（finish_once 前）")
            finish_once()
            # 星轨自动化：回写运行终态。cancelled（用户停止）→ cancelled，
            # 回合异常或模型侧 turn_error（内层生成器把常见失败吞为事件，worker
            # 只能经 error_state["terminal_message"] 感知）→ error，否则 completed。
            # exactly-once 由 storage 的 queued/running 状态守卫兜底（规格 5.4；
            # 取消绝不自动重放）
            if automation_run_id:
                automation_error = (
                    automation_error or str(error_state.get("terminal_message") or "")
                )
                final_status = (
                    "cancelled" if cancelled
                    else ("error" if automation_error else "completed")
                )
                try:
                    self._automation_storage.finish_run(
                        automation_run_id, final_status, (automation_error or "")[:500]
                    )
                except Exception:
                    logger.exception("自动化运行 %s 终态回写失败", automation_run_id)
                # 终态通知（规格 5.7）：失败/取消恒提示，完成仅在任务开启时提示
                try:
                    notify_success = (
                        str((automation_context or {}).get("notify_on_success") or "") == "1"
                    )
                    # session_name 必带：前端按归属会话路由 notice，
                    # 缺省会落到用户正在查看的会话（跨会话串扰）
                    if final_status == "error":
                        self.emit_event("notice", {
                            "message": (
                                f"星轨自动化任务「{automation_task_name}」运行失败："
                                f"{automation_error[:120]}"
                            ),
                            "session_name": session_name,
                        })
                    elif final_status == "cancelled":
                        self.emit_event("notice", {
                            "message": f"星轨自动化任务「{automation_task_name}」运行已取消。",
                            "session_name": session_name,
                        })
                    elif final_status == "completed" and notify_success:
                        self.emit_event("notice", {
                            "message": f"星轨自动化任务「{automation_task_name}」运行完成。",
                            "session_name": session_name,
                        })
                except Exception:
                    logger.debug("自动化终态通知推送失败", exc_info=True)
                if turn_id:
                    self._automation_turn_labels.pop(turn_id, None)
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
            # 闲时维护重新计时：空闲超过配置延时后跑水位线维护与压缩（默认开，
            # config memory_compression.idle_maintenance_seconds = 0 关闭）
            if not self._shutting_down:
                try:
                    idle_maintenance_service.on_turn_finished(
                        session_name,
                        notify=lambda kind, message: self.emit_event(
                            "notice",
                            {
                                "message": _strip_ansi_markup(message),
                                "session_name": session_name,
                            },
                        ),
                    )
                except Exception:
                    pass
            # finally 末尾：补发排队中的用户消息（FIFO）。放在所有清理之后，
            # 保证补发回合的 turn_start 事件一定晚于本回合的终态事件到达前端
            self._drain_message_queue(session_name)

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
            # session_name 取回调的 session_id（实为发起任务的会话名，见
            # core/tools.py run_in_background 归属解析）；缺省时前端才回退当前会话
            self.emit_event("background_task_finished", {
                "task_id": task_id,
                "command": redact_sensitive_text(command or ""),
                "returncode": int(returncode or 0),
                "session_name": str(session_id or ""),
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
                f"{_BG_COMMAND_SUMMARY_PROMPT}\n"
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
                        # 完整阶段历史：右面板"子代理"工作台渲染执行流程时间线
                        "stages": [
                            {"name": s.name, "at": s.at, "detail": s.detail}
                            for s in snap.stages
                        ],
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
            if session_name:
                active_model = {}
                try:
                    active_model = get_active_model(session_name) or {}
                except Exception:
                    pass
                m_name = active_model.get("model_name") or active_model.get("display_name") or ""
                m_id = active_model.get("model_id") or ""
                self._record_session_last_error(session_name, sanitized, model_name=m_name, model_id=m_id)

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
        model_id=None,
        error_state=None,
        turn_source="local",
    ) -> bool:
        """迭代模型事件并翻译为前端事件；返回本轮是否被取消。

        turn_source 是入口层受控回合来源（local|lan，设计 18），透传给
        chat_with_model_events 的 computer_turn_source：LAN 回合即使本机
        开关开启也拿不到 computer tool / native built-in。
        """
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
            model_id=model_id,
            computer_turn_source=turn_source,
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
                # 快照必须按轮清零：前端是"一轮一条 assistant 消息"的粒度，
                # 否则中途接入/切换运行中会话会把整回合文本灌进最后一个气泡（输出粘成一块）。
                # 只在锁内改快照，绝不在持锁时触碰 JS 桥。
                with self._active_turn_lock:
                    snap = self._active_turn_snapshots.get(session_name)
                    if snap is not None:
                        snap["streaming_text"] = ""
                        snap["thinking_text"] = ""
                        snap["tool_calls"] = []
            elif kind == "thinking_delta":
                self._emit_delta("thinking_delta", str(data.get("text", "")), session_name=session_name)
            elif kind in ("assistant_delta", "token_delta", "answer_delta"):
                self._emit_delta("token_delta", str(data.get("text", "")), session_name=session_name)
            elif kind == "step_metrics":
                self.emit_event("step_metrics", {**data, "session_name": session_name})
            elif kind in ("tool_requested", "tool_started"):
                tool_id = str(data.get("id") or "")
                name = str(data.get("name") or "tool")
                label = str(data.get("summary") or data.get("preview") or name)
                tool_labels[tool_id] = label
                if tool_id not in started_tool_ids:
                    started_tool_ids.add(tool_id)
                    tool_item = {
                        "id": tool_id,
                        "name": name,
                        "label": label,
                        "display": dict(data.get("display") or {}),
                        "started_at_ms": int(time.time() * 1000),
                        "session_name": session_name,
                    }
                    self.emit_event("tool_start", tool_item)
                    with self._active_turn_lock:
                        snap = self._active_turn_snapshots.get(session_name)
                        if snap is not None:
                            calls = snap.setdefault("tool_calls", [])
                            idx = next((i for i, c in enumerate(calls) if c.get("id") == tool_id), -1)
                            if idx >= 0:
                                calls[idx].update(tool_item)
                            else:
                                calls.append(dict(tool_item))
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
                with self._active_turn_lock:
                    snap = self._active_turn_snapshots.get(session_name)
                    if snap is not None:
                        calls = snap.get("tool_calls") or []
                        hit = next((c for c in calls if c.get("id") == tool_id), None)
                        if hit is not None:
                            hit.update({
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
                    "added": data.get("added"),
                    "removed": data.get("removed"),
                    "session_name": session_name,
                })
                with self._active_turn_lock:
                    snap = self._active_turn_snapshots.get(session_name)
                    if snap is not None:
                        t_id = str(data.get("tool_call_id") or "")
                        calls = snap.get("tool_calls") or []
                        hit = next((c for c in calls if c.get("id") == t_id), None)
                        if hit is not None:
                            hit["diff"] = {
                                "file_path": str(data.get("file_path") or ""),
                                "diff_text": str(data.get("diff_text") or data.get("text") or ""),
                                "added": data.get("added"),
                                "removed": data.get("removed"),
                            }
            elif kind in ("plan_updated", "plan_change"):
                self.emit_event("plan_updated", {
                    "title": str(data.get("title") or "当前任务计划"),
                    "items": data.get("items") or [],
                    "session_name": session_name,
                })
            elif kind == "plan_writeback_rejected":
                # 完成/失败回写被拒：模型侧已有文字反馈，这里补一条用户可见提示，
                # 避免"任务已干完、状态却一直不动"在界面上没有任何线索
                message = str(data.get("reason") or "")
                if message:
                    self.emit_event("notice", {
                        "message": _strip_ansi_markup(message),
                        "session_name": session_name,
                    })
            elif kind == "plan_item_handoff":
                # 子 Agent 超时接管可见性：只透传展示字段，不改调度状态；
                # 旧子 Agent 卡片保持 timed_out，计划卡片显示 main_takeover
                self.emit_event("plan_item_handoff", {
                    "item": dict(data.get("item") or {}),
                    "source_task_id": str(data.get("source_task_id") or ""),
                    "mode": str(data.get("mode") or ""),
                    "queue_position": data.get("queue_position") or 0,
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
        # 首条用户消息也带着当轮注入的上下文后缀，提炼标题前剥掉，
        # 否则标题会混入「【当前运行上下文】当前模型：…」这类运行时文字
        first_text = str(strip_context_suffix(raw)).strip().replace("\n", " ")
        # @{会话名} 标记按内部名还原并剥掉花括号：与首条消息即时命名
        # （_send_message 内的 base_title 派生）保持同一净化口径
        from core.input_parser import sanitize_session_title
        first_text = sanitize_session_title(first_text)
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
        if type(days) is not int or days not in (1, 7, 30, 365):
            return {"ok": False, "msg": "统计范围仅支持 1、7、30、365 天"}
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
            from core.context_breakdown import get_breakdown
            # 按当前会话取模型：会话绑定模型与全局默认不同时，
            # 上下文圆圈的 window/used 必须按会话模型计算
            active = get_active_model(self._current_session) or {}
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
            # Task 4：补 lane 维度（当前会话的模型 / 接口 / scope）与保活单列
            lane_model = str(active.get("litellm_model", "") or "")
            lane_api_base = str(active.get("base_url", "") or "")
            keepalive_usage = (snapshot.get("cache_scopes") or {}).get("keepalive") or {}

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
                # 本会话口径（token_usage 落库聚合，重启不清零）：状态行默认
                # 显示当前会话的输入/输出/命中；命中率分母用 cache_prompt
                # （只含已报告缓存明细的轮次，未报告 ≠ 0）
                "session_totals": self._session_token_totals(self._current_session),
                # 车道身份与保活单列（前端上下文面板消费；口径与上面的 cache_*_pct 一致，
                # 因此不再重复输出 cache_lane_hot_pct / cache_lane_session_pct 这类
                # 与既有键同值的字段——白涨载荷）。现有键语义保持不变。
                "cache_lane_model": lane_model,
                "cache_lane_api_base": lane_api_base,
                # 保活单列：keepalive scope 的命中率（未报告为 None，不冒充 0）。
                # hot 是热窗口径（与 CLI 状态栏同源），session 与主口径
                # cache_session_pct 同分母，面板用后者才不会两行不同口径。
                "cache_keepalive_hot_pct": rounded(
                    (keepalive_usage.get("hot") or {}).get("rate")
                ),
                "cache_keepalive_session_pct": rounded(
                    (keepalive_usage.get("session_all") or {}).get("rate")
                ),
                # 输入框悬停面板：上下文来源字符占比快照（设计规格 §3.4）
                "context_breakdown": get_breakdown(),
            }
        except Exception:
            logger.debug("get_token_usage 失败", exc_info=True)
            return {
                "used": 0, "window": 0, "cached_pct": 0.0, "cumulative": 0,
                "cache_latest_pct": None, "cache_hot_pct": None,
                "cache_session_pct": None, "cache_available": False,
                "cache_hot_turns": 0, "cache_invalidation_reason": "",
                "prompt_tokens": 0, "cached_tokens": 0,
                "cache_lane_model": "", "cache_lane_api_base": "",
                "cache_keepalive_hot_pct": None,
                "cache_keepalive_session_pct": None,
                "session_totals": self._empty_session_totals(),
                "context_breakdown": [],
            }

    @staticmethod
    def _empty_session_totals():
        return {
            "session_id": "", "prompt": 0, "completion": 0, "cached": 0,
            "cache_prompt": 0, "total": 0, "requests": 0, "last_ts": None,
            "keepalive_prompt": 0, "keepalive_cached": 0, "keepalive_requests": 0,
        }

    def _session_token_totals(self, session_name):
        """本会话 token 用量落库聚合；失败返回零值，绝不影响主推送。"""
        try:
            totals = self._memory.get_token_usage_by_session(session_name)
        except Exception:
            logger.debug("本会话 token 聚合失败", exc_info=True)
            totals = self._empty_session_totals()
        totals = dict(totals)
        totals["session_id"] = str(session_name or "")
        return totals

    def get_token_usage_overview(self, limit: int = 20) -> Dict[str, Any]:
        """会话 token 弹层数据：全部会话合计 + 按用量排序的会话列表。

        合计不分 scope（保活/压缩也是真实计费），会话列表只含对话口径
        （main/legacy）且只列现存会话。供前端点击状态行时按需拉取。
        """
        try:
            if type(limit) is not int or not (1 <= limit <= 100):
                limit = 20
            sessions = self._memory.list_token_usage_by_sessions(limit=limit)
            return {
                "ok": True,
                "current": str(self._current_session or ""),
                "totals": self._memory.get_token_usage_totals(),
                "sessions": sessions,
            }
        except Exception as exc:
            logger.debug("get_token_usage_overview 失败", exc_info=True)
            return {"ok": False, "msg": str(exc), "current": "", "totals": {}, "sessions": []}

    def _emit_token_usage(self):
        try:
            self.emit_event("token_usage", self.get_token_usage())
        except Exception:
            logger.debug("推送 token_usage 失败", exc_info=True)

    def _recalc_token_usage(self, session_name: str, *, clear_breakdown_snapshot: bool = True):
        """切会话 / 清空历史后重算窗口占用（优先真实用量，无记录才估算兜底）。

        clear_breakdown_snapshot=False 供「回合组装后重算」路径使用：该路径下
        来源占比快照是组装刚写入的最新数据，绝不能被当作"旧会话残留"清掉
        （否则组装后推给前端的面板数据永远为空，2026-09-23 修复）。
        """
        try:
            from core.agent_service import get_session_token_baseline
            from core.context_breakdown import clear_breakdown
            from core.llm import session_token_usage, token_usage_lock
            # 按会话取模型：切模型后重算必须用该会话新绑定模型的窗口
            active = get_active_model(session_name) or {}
            history = self._memory.get_history(session_name)
            total = get_session_token_baseline(
                self._memory, session_name, active.get("litellm_model", ""), history=history
            )
            with token_usage_lock:
                session_token_usage["latest_total"] = total
                session_token_usage["latest_prompt"] = 0
            # 上下文占比快照与 token 记账同生命周期：重算即清理旧快照，
            # 避免切换会话后悬停面板残留上一会话的占比
            if clear_breakdown_snapshot:
                clear_breakdown()
        except Exception:
            logger.debug("重算 token 用量失败", exc_info=True)

    def _ensure_fits_active_window(self, session_name: str):
        """切换模型后兜底：当前会话占用超过新激活模型窗口阈值时后台异步压缩历史。

        从大窗口（如 100 万）切到小窗口（如 25.6 万）模型时，若占用超出新窗口
        的压缩阈值（0.95），在后台守护线程触发四级水位线压缩（snip/elide/摘要/滑窗淘汰），
        避免阻塞切换模型接口的快速响应。压缩后自动重算窗口占用并推送给前端。
        """
        try:
            # 并发保护：若当前会话正在运行回合，或后台已有记忆压缩在执行，跳过触发
            from core.memory_compressor import is_session_compressing
            if self._is_turn_active(session_name) or is_session_compressing(self._memory, session_name):
                logger.info(
                    "会话 %s 正在运行回合或后台压缩进行中，跳过切模型异步压缩", session_name
                )
                return

            # 按会话取模型：切到小窗口模型后是否需要立即压缩，
            # 必须按该会话绑定模型的窗口判断，全局模型与此无关
            active = get_active_model(session_name) or {}
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

            def _bg_compress():
                try:
                    if self._is_turn_active(session_name) or is_session_compressing(self._memory, session_name):
                        return
                    from core.agent_service import prepare_turn_messages
                    logger.info(
                        "切换模型后会话占用 %d 超新窗口 %d 阈值，后台触发自动压缩", total, window
                    )
                    prepare_turn_messages(
                        self._memory,
                        session_name,
                        active.get("litellm_model", ""),
                        window,
                        permission_mode=self._effective_permission_mode(session_name),
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
                    logger.exception("切换模型后后台自动压缩失败")

            threading.Thread(
                target=_bg_compress,
                name=f"gui-model-switch-compress-{session_name}",
                daemon=True,
            ).start()
        except Exception:
            logger.exception("切换模型后检查压缩失败")

    # ------------------------------------------------------------------
    # 技能
    # ------------------------------------------------------------------
    def _build_skill_list(self) -> List[Dict[str, Any]]:
        try:
            active_ids = set(get_active_skill_ids())
            # 释放名单与内置源目录对所有技能是同一份数据，只在开头各读一次：
            # 放进循环会让技能页随技能数量线性增加读盘次数
            try:
                released_builtin = get_released_builtin_names()
            except Exception:
                # 保守原则：名单读不到就整体不标"可恢复内置版本"，绝不能让
                # 这一个展示字段把整张技能列表拖垮
                logger.debug("读取内置技能释放名单失败", exc_info=True)
                released_builtin = set()
            try:
                builtin_root = _builtin_skills_dir()
            except Exception:
                logger.debug("读取内置技能源目录失败", exc_info=True)
                builtin_root = ""
            skills = []
            for s in (scan_all_skills() or []):
                skill_id = str(s.get("id", ""))
                short_id = os.path.basename(os.path.dirname(skill_id)) if skill_id else ""
                # "可恢复内置版本"需同时满足：曾由内置释放过 + 内置源里确有同名目录。
                # 后者不成立时点恢复必然失败，据此让前端隐藏/禁用按钮更贴合实际。
                from_builtin = bool(
                    builtin_root
                    and short_id
                    and short_id in released_builtin
                    and os.path.isdir(os.path.join(builtin_root, short_id))
                )
                skills.append({
                    "id": skill_id,
                    "short_id": short_id,
                    "name": str(s.get("name", "")),
                    "description": str(s.get("description", "")),
                    "enabled": skill_id in active_ids,
                    "source": str(s.get("source") or "user"),
                    "from_builtin": from_builtin,
                })
            # 顺带更新实例缓存：toggle/restore/refresh/get_skills 以及工作区切换、
            # 会话切换都会走到这里，缓存随最新结果更新，下次 get_initial_state
            # 就直接返回这份新数据，不会拿到启动时的旧列表
            self._skills_cache = skills
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

    def restore_skill(self, skill_id: str) -> Dict[str, Any]:
        """把指定技能恢复为随包内置版本（技能页"恢复内置版本"按钮）。

        与"跟随升级"不同，这是用户显式发起的动作，所以不做"是否被改过"的判断，
        直接以内置内容覆盖用户目录里的同名技能；真正的覆盖逻辑留在 core.skills，
        这里只负责把 GUI 的 skill_id（SKILL.md 绝对路径）换算成技能目录名。
        """
        try:
            # 内置技能以目录名（即列表里的 short_id）为唯一标识
            name = os.path.basename(os.path.dirname(str(skill_id or "")))
            if not name:
                # skill_id 为空或路径非法时说明原因即可，不必走到 core 层
                return {"ok": False, "msg": "技能标识无效，无法定位技能目录"}
            result = restore_builtin_skill(name)
            if not result.get("ok"):
                return {"ok": False, "msg": str(result.get("msg") or "恢复内置版本失败")}
            # 与 toggle_skill 一致：一次回传刷新后的完整列表，前端无需再调接口
            return {"ok": True, "skills": self._build_skill_list()}
        except Exception as e:
            logger.exception("restore_skill 失败")
            return {"ok": False, "msg": str(e)}

    def refresh_skills(self) -> Dict[str, Any]:
        """重新扫描并返回所有技能列表（供前端技能页刷新按钮调用）。"""
        try:
            return {"ok": True, "skills": self._build_skill_list()}
        except Exception as e:
            logger.exception("refresh_skills 失败")
            return {"ok": False, "msg": str(e), "skills": []}

    def get_skills(self) -> List[Dict[str, Any]]:
        """返回所有技能列表。"""
        return self._build_skill_list()

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

    def trust_plugin(self, name: str) -> Dict[str, Any]:
        try:
            from core.plugin_loader import trust_plugin as _trust
            ok, msg = _trust(name)
            return {"ok": ok, "msg": msg, "plugins": self.get_plugins()}
        except Exception as e:
            logger.exception("trust_plugin 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # Hooks（信任状态与操作走 core.hooks.trust，与 CLI / 执行侧同源；
    #        状态判定每次读盘、不缓存，执行期间的撤销立即生效）
    # ------------------------------------------------------------------
    def _hook_workspace_root(self) -> str:
        """当前会话对应的工作根路径（与回合运行时的 workspace_identity 口径一致）。"""
        # _session_workdir 已含查不到时回退 _resolve_real_path(os.getcwd()) 的逻辑；
        # GLOBAL 会话（自由对话）回合运行时用进程 cwd，这里同口径。
        session_workdir = self._session_workdir(self._current_session)
        if not session_workdir or session_workdir == "GLOBAL":
            return _resolve_real_path(os.getcwd())
        return _resolve_real_path(session_workdir)

    def get_hooks(self) -> Dict[str, Any]:
        """列出全部 hook（用户级+项目级）与信任状态（C11 卡片 / C12 管理页共用）。"""
        # 成功: {"ok": True, "hooks": [...], "workdir": root}
        # 异常: {"ok": False, "msg": str(e), "hooks": []}
        try:
            from core.hooks.trust import list_hooks_with_status
            root = self._hook_workspace_root()
            hooks = list_hooks_with_status(workspace_root=root)
            return {"ok": True, "hooks": hooks, "workdir": root}
        except Exception as e:
            logger.exception("get_hooks 失败")
            return {"ok": False, "msg": str(e), "hooks": []}

    def grant_hook_trust(self, digest: str) -> Dict[str, Any]:
        """信任指定 hook（pending/stale → trusted）。"""
        return self._hook_trust_action("grant", digest)

    def deny_hook_trust(self, digest: str) -> Dict[str, Any]:
        """拒绝指定 hook（pending → blocked，不再询问）。"""
        return self._hook_trust_action("deny", digest)

    def revoke_hook_trust(self, digest: str) -> Dict[str, Any]:
        """撤销对指定 hook 的信任（trusted → revoked）。"""
        return self._hook_trust_action("revoke", digest)

    def reset_hook_trust(self, digest: str) -> Dict[str, Any]:
        """重置指定 hook 的信任状态（blocked/revoked → pending）。"""
        return self._hook_trust_action("reset", digest)

    def _hook_trust_action(self, action: str, digest: str) -> Dict[str, Any]:
        """信任操作统一实现：执行后回传刷新列表（同 trust_plugin 回传 plugins 模式）。

        成功: {"ok": ok, "msg": msg, "hooks": [...], "workdir": root}
        异常: {"ok": False, "msg": str(e)}
        """
        try:
            from core.hooks.trust import apply_hook_trust_action, list_hooks_with_status
            root = self._hook_workspace_root()
            ok, msg = apply_hook_trust_action(action, digest, workspace_root=root)
            hooks = list_hooks_with_status(workspace_root=root)
            return {"ok": ok, "msg": msg, "hooks": hooks, "workdir": root}
        except Exception as e:
            logger.exception("_hook_trust_action(%s) 失败", action)
            return {"ok": False, "msg": str(e)}

    def save_hook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """保存（新增/更新）一条 Hook 配置（手动配置入口；成功后回传刷新列表）。

        成功: {"ok": ok, "msg": msg, "hooks": [...], "workdir": root}
        异常: {"ok": False, "msg": str(e)}（绝不抛出到 pywebview 调用侧）
        """
        try:
            from core.hooks.config_mutation import save_hook_config
            from core.hooks.trust import list_hooks_with_status
            root = self._hook_workspace_root()
            ok, msg = save_hook_config(payload or {}, workspace_root=root)
            hooks = list_hooks_with_status(workspace_root=root)
            return {"ok": ok, "msg": msg, "hooks": hooks, "workdir": root}
        except Exception as e:
            logger.exception("save_hook 失败")
            return {"ok": False, "msg": str(e)}

    def delete_hook(self, source_path: str, digest: str) -> Dict[str, Any]:
        """删除一条 Hook 配置（手动配置入口；成功后回传刷新列表）。

        成功: {"ok": ok, "msg": msg, "hooks": [...], "workdir": root}
        异常: {"ok": False, "msg": str(e)}（绝不抛出到 pywebview 调用侧）
        """
        try:
            from core.hooks.config_mutation import delete_hook_config
            from core.hooks.trust import list_hooks_with_status
            root = self._hook_workspace_root()
            ok, msg = delete_hook_config(source_path, digest, workspace_root=root)
            hooks = list_hooks_with_status(workspace_root=root)
            return {"ok": ok, "msg": msg, "hooks": hooks, "workdir": root}
        except Exception as e:
            logger.exception("delete_hook 失败")
            return {"ok": False, "msg": str(e)}

    def set_hook_disabled(
        self, source_path: str, digest: str, disabled: bool
    ) -> Dict[str, Any]:
        """启用/停用一条 Hook 配置（手动配置入口；成功后回传刷新列表）。

        成功: {"ok": ok, "msg": msg, "hooks": [...], "workdir": root}
        异常: {"ok": False, "msg": str(e)}（绝不抛出到 pywebview 调用侧）
        """
        try:
            # 别名导入：避免与桥接方法同名造成阅读歧义（导入的是一元布尔写入函数）
            from core.hooks.config_mutation import set_hook_disabled as _set_hook_disabled
            from core.hooks.trust import list_hooks_with_status
            root = self._hook_workspace_root()
            ok, msg = _set_hook_disabled(source_path, digest, disabled, workspace_root=root)
            hooks = list_hooks_with_status(workspace_root=root)
            return {"ok": ok, "msg": msg, "hooks": hooks, "workdir": root}
        except Exception as e:
            logger.exception("set_hook_disabled 失败")
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

    # ------------------------------------------------------------------
    # 按回合撤销：窄桥接（规格 19.1）。桥只做会话归属校验与会话名 →
    # sessions.id 解析，不复制文件检查和事务逻辑。
    # ------------------------------------------------------------------

    def _undo_resolve_session_id(self, session_name: str) -> int:
        from core.turn_undo import service as _tu_service
        from core.turn_undo.models import TurnUndoError

        try:
            return _tu_service.resolve_session_id(session_name)
        except TurnUndoError:
            raise ValueError(f"会话不存在: {session_name}")

    def _undo_error_payload(self, error) -> Dict[str, Any]:
        from core.turn_undo.models import TurnUndoError

        if isinstance(error, TurnUndoError):
            return {"ok": False, "code": error.code, "msg": error.message,
                    "detail": error.detail or {}}
        logger.exception("按回合撤销操作失败")
        return {"ok": False, "code": "internal_error", "msg": str(error), "detail": {}}

    def list_turn_undo_states(self, session_name: str) -> Dict[str, Any]:
        try:
            from core.turn_undo import service as _tu_service

            session_id = self._undo_resolve_session_id(session_name)
            states = _tu_service.list_turn_undo_states(session_id)
            return {"ok": True, "states": states}
        except Exception as e:
            return self._undo_error_payload(e)

    def get_turn_undo_status(self, session_name: str, turn_id: str) -> Dict[str, Any]:
        try:
            from core.turn_undo import service as _tu_service

            session_id = self._undo_resolve_session_id(session_name)
            status = _tu_service.get_turn_undo_status(session_id, turn_id)
            return {"ok": True, "status": status}
        except Exception as e:
            return self._undo_error_payload(e)

    def get_turn_undo_file_diff(self, session_name: str, turn_id: str,
                                file_id: str) -> Dict[str, Any]:
        try:
            from core.turn_undo import service as _tu_service

            session_id = self._undo_resolve_session_id(session_name)
            diff = _tu_service.get_turn_undo_file_diff(session_id, turn_id, file_id)
            return {"ok": True, "diff": diff}
        except Exception as e:
            return self._undo_error_payload(e)

    def preview_turn_undo(self, session_name: str, turn_id: str) -> Dict[str, Any]:
        try:
            from core.turn_undo import service as _tu_service

            session_id = self._undo_resolve_session_id(session_name)
            preview, token = _tu_service.preview_turn_undo(session_id, turn_id)
            from dataclasses import asdict

            payload = asdict(preview)
            for key, value in list(payload.items()):
                if hasattr(value, "value"):
                    payload[key] = value.value
            payload["conflicts"] = [
                {
                    "file_id": c.file_id,
                    "display_path": c.display_path,
                    "expected": _undo_node_state_dict(c.expected),
                    "actual": _undo_node_state_dict(c.actual),
                    "reasons": list(c.reasons),
                }
                for c in preview.conflicts
            ]
            return {"ok": True, "preview": payload, "preview_token": token}
        except Exception as e:
            return self._undo_error_payload(e)

    def execute_turn_undo(self, session_name: str, turn_id: str,
                          preview_token: str) -> Dict[str, Any]:
        try:
            from core.turn_undo import service as _tu_service
            from dataclasses import asdict

            session_id = self._undo_resolve_session_id(session_name)
            result = _tu_service.execute_turn_undo(session_id, turn_id, preview_token)
            payload = asdict(result)
            if hasattr(payload.get("status"), "value"):
                payload["status"] = result.status.value
            # 数据库提交成功后才发送事件（规格 20.2）
            self.emit_event("turn_undo_updated", {
                "session_id": session_id,
                "session_name": session_name,
                "turn_id": turn_id,
                "operation_id": result.operation_id,
                "status": payload["status"],
                "file_count": (len(result.restored_files)
                               + len(result.deleted_created_files)
                               + len(result.restored_deleted_files)),
                "reverted_at": result.reverted_at_ms,
                "owner_kind": "foreground_turn",
                "message": "本轮文件修改已撤销，聊天历史保留" if not result.idempotent
                else "该回合此前已撤销过",
            })
            return {"ok": True, "result": payload}
        except Exception as e:
            return self._undo_error_payload(e)

    def preview_turn_undo_recovery(self, session_name: str, operation_id: str,
                                   action: str) -> Dict[str, Any]:
        try:
            from core.turn_undo import service as _tu_service
            from dataclasses import asdict

            session_id = self._undo_resolve_session_id(session_name)
            preview, token = _tu_service.preview_turn_undo_recovery(
                session_id, operation_id, action
            )
            payload = asdict(preview)
            if hasattr(payload.get("action"), "value"):
                payload["action"] = preview.action.value
            return {"ok": True, "preview": payload, "preview_token": token}
        except Exception as e:
            return self._undo_error_payload(e)

    def execute_turn_undo_recovery(self, session_name: str, operation_id: str,
                                   action: str, preview_token: str) -> Dict[str, Any]:
        try:
            from core.turn_undo import service as _tu_service
            from dataclasses import asdict

            session_id = self._undo_resolve_session_id(session_name)
            result = _tu_service.execute_turn_undo_recovery(
                session_id, operation_id, action, preview_token
            )
            payload = asdict(result)
            for key in ("status", "checkpoint_status", "action"):
                if hasattr(payload.get(key), "value"):
                    payload[key] = getattr(result, key).value
            # L5：恢复事件必须携带权威 turn_id，否则前端 reducer 不会刷新卡片
            operation_row = _tu_service.get_default_storage().get_operation(operation_id)
            checkpoint_row = _tu_service.get_default_storage().get_checkpoint(
                operation_row.checkpoint_id
            )
            self.emit_event("turn_undo_updated", {
                "session_id": session_id,
                "session_name": session_name,
                "turn_id": checkpoint_row.turn_id,
                "operation_id": result.operation_id,
                "status": payload["checkpoint_status"],
                "file_count": len(result.affected_files),
                "reverted_at": 0,
                "owner_kind": "foreground_turn",
                "message": (
                    "撤销事务已继续完成" if result.action.value == "continue_revert"
                    else "撤销事务已取消，文件恢复到点击撤销前的状态"
                ),
            })
            return {"ok": True, "result": payload}
        except Exception as e:
            return self._undo_error_payload(e)

    def check_paths_kind(self, paths: List[str]) -> List[Dict[str, Any]]:
        """批量返回路径类型（file / dir / missing），供前端把目录与不存在的
        路径链接降级为纯文本，避免点击后才发现无法预览。"""
        results: List[Dict[str, Any]] = []
        for p in paths or []:
            raw = str(p)
            try:
                real_path = _resolve_real_path(raw)
                if os.path.isfile(real_path):
                    kind = "file"
                elif os.path.isdir(real_path):
                    kind = "dir"
                else:
                    kind = "missing"
            except Exception:
                kind = "missing"
            results.append({"path": raw, "kind": kind})
        return results

    def read_file_preview(self, path: str) -> Dict[str, Any]:
        try:
            from core.gui_path_policy import gui_path_access_allowed

            real_path = _resolve_real_path(path)
            workspace = self._session_workdir(self._current_session)
            allowed, deny_msg = gui_path_access_allowed(
                real_path, workspace, action="preview", session_id=self._current_session
            )
            if not allowed:
                return {"ok": False, "msg": deny_msg}
            if not os.path.isfile(real_path):
                # 目录路径（模型常在回复中输出文件夹路径，点击预览属常见操作）
                if os.path.isdir(real_path):
                    return {"ok": False, "msg": "这是一个文件夹，不支持文本预览（可在侧栏文件树中浏览其内容）"}
                return {"ok": False, "msg": "文件不存在或不是普通文件"}
            size = os.path.getsize(real_path)
            ext = os.path.splitext(real_path)[1].lower()
            if ext in IMAGE_PREVIEW_MIME:
                # 图片走 base64 data URL，前端预览弹窗用 <img> 直接渲染
                if size > IMAGE_PREVIEW_MAX_BYTES:
                    return {"ok": False, "msg": (
                        f"图片过大（{size / 1024 / 1024:.1f} MB，"
                        f"上限 {IMAGE_PREVIEW_MAX_BYTES // 1024 // 1024} MB），请使用「在系统中打开」"
                    )}
                import base64
                with open(real_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                return {
                    "ok": True,
                    "kind": "image",
                    "data_url": f"data:{IMAGE_PREVIEW_MIME[ext]};base64,{b64}",
                    "path": real_path,
                }
            truncated = size > FILE_PREVIEW_MAX_BYTES
            with open(real_path, "rb") as f:
                raw = f.read(FILE_PREVIEW_MAX_BYTES)
            if b"\x00" in raw:
                return {"ok": False, "msg": "二进制文件不支持预览，可在系统中打开"}
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
            from core.gui_path_policy import gui_path_access_allowed

            real_path = _resolve_real_path(path)
            workspace = self._session_workdir(self._current_session)
            allowed, deny_msg = gui_path_access_allowed(
                real_path, workspace, action="open", session_id=self._current_session
            )
            if not allowed:
                return {"ok": False, "msg": deny_msg}
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

    def get_ui_layout(self) -> Dict[str, Any]:
        """读取侧栏排序快照（项目分组顺序 + 分组内会话顺序）。

        权威副本落在数据目录的 ui_layout.json：WebView2 的 localStorage 在
        「拖完排序立刻重启」时会被 leveldb 恢复过程丢弃（写前日志 checksum
        mismatch），只靠前端缓存会出现"重启就复原"。前端启动时用本快照校正。
        """
        try:
            from core.ui_layout import get_ui_layout as _get_ui_layout

            return {"ok": True, **_get_ui_layout()}
        except Exception as e:
            logger.exception("get_ui_layout 失败")
            return {"ok": False, "msg": str(e)}

    def save_ui_layout(self, layout: Dict[str, Any]) -> Dict[str, Any]:
        """全量写入侧栏排序快照，返回落盘后的规范化结果。"""
        try:
            from core.ui_layout import save_ui_layout as _save_ui_layout

            if not isinstance(layout, dict):
                return {"ok": False, "msg": "layout 必须是对象"}
            return {"ok": True, **_save_ui_layout(layout)}
        except Exception as e:
            logger.exception("save_ui_layout 失败")
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
            # 登记为本进程粘贴落盘文件：后续 read_file 等只读工具访问
            # 该文件时按受托路径免沙盒确认（见 core.tools.is_trusted_paste_path）
            from core.tools import register_paste_file
            register_paste_file(path)
            return {"ok": True, "path": path}
        except Exception as e:
            logger.exception("save_clipboard_image 失败")
            return {"ok": False, "msg": str(e)}

    def get_attachment_thumbnails(self, paths: List[str]) -> Dict[str, Any]:
        """为输入框图片附件生成缩略图 URL（内容寻址媒体仓库归档）。

        缩略图是增强能力：单项失败（文件不存在 / 非白名单格式 / 仓库异常 /
        media 服务不可用）只影响该项，整体永远 ok=True 且不抛异常阻断前端。
        """
        out: List[Dict[str, Any]] = []
        try:
            clean = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
            for p in clean:
                try:
                    real = _resolve_real_path(str(p))
                    item = archive_media_file(real)
                    # media_server 未启动时 public_url 返回空串，跳过该项即可
                    url = media_server.public_url(item["digest"], item["ext"])
                    if url:
                        out.append({
                            "path": str(p),
                            "url": url,
                            "width": item.get("width"),
                            "height": item.get("height"),
                        })
                except Exception:
                    # 单项失败不冒泡：非图片格式、文件消失等都属正常场景
                    logger.debug("附件缩略图归档失败: %s", p, exc_info=True)
                    continue
        except Exception:
            # 兜底：任何意外都不允许缩略图逻辑影响发送主流程
            logger.debug("get_attachment_thumbnails 整体异常", exc_info=True)
        return {"ok": True, "items": out}

    def store_dropped_paths(self, paths: List[str]) -> None:
        """pywebview 原生 drop 事件回调存储的完整路径，供前端 drop 时查询。"""
        self._dropped_paths = list(paths or [])

    UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50MB

    def _save_bytes_to_temp(self, raw: bytes, ext: str) -> str:
        """把二进制写入 ycode_paste 临时目录，返回完整路径（时间戳+随机后缀防撞名）。"""
        import tempfile

        paste_dir = os.path.join(tempfile.gettempdir(), "ycode_paste")
        os.makedirs(paste_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        ms = int(time.time() * 1000) % 1000
        path = os.path.join(
            paste_dir,
            f"upload_{ts}_{ms:03d}_{uuid.uuid4().hex[:6]}{ext}",
        )
        with open(path, "wb") as f:
            f.write(raw)
        from core.tools import register_paste_file
        register_paste_file(path)
        return path

    def upload_file(self, name: str, data_b64: str) -> Dict[str, Any]:
        """手机/平板端经 WS RPC 上传文件（base64），落盘后返回路径供作为附件发送。"""
        try:
            raw = base64.b64decode(data_b64 or "", validate=True)
        except Exception:
            return {"ok": False, "msg": "无效的文件数据"}
        try:
            if len(raw) > self.UPLOAD_MAX_BYTES:
                return {"ok": False, "msg": "文件超过 50MB，无法上传"}
            base = os.path.basename(str(name or "file"))
            _, ext = os.path.splitext(base)
            path = self._save_bytes_to_temp(raw, ext or ".bin")
            return {"ok": True, "path": path}
        except Exception as e:
            logger.exception("upload_file 失败")
            return {"ok": False, "msg": str(e)}

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

    def get_background_task_output(self, task_id: str, tail_lines: int = 200) -> Dict[str, Any]:
        """后台任务执行过程：读取任务日志尾部（右面板详情用）。

        get_task_output 自带末尾字节截断与异常兜底，失败返回 🚨 文本而非抛错。
        """
        try:
            from core.task_manager import get_task_output
            return {"ok": True, "output": get_task_output(str(task_id or ""), int(tail_lines))}
        except Exception as exc:
            logger.debug("get_background_task_output 失败", exc_info=True)
            return {"ok": False, "output": "", "msg": str(exc)}

    def get_subagent_task_detail(self, task_id: str, session_name: str = "") -> Dict[str, Any]:
        """子代理执行过程详情（右面板"子代理"工作台轮询用）。

        返回目标任务的完整阶段历史 + 该会话全部子代理的轻量清单（供任务切换条）。
        目标任务按 task_id 全局查找（会话切换后仍可回看）；清单按 session 过滤，
        session_name 为空时返回全部任务。任务可能因容量清理被回收，此时 ok=False，
        前端保留最后已知状态继续展示。
        """
        try:
            from core.tools import subagent_manager
            wanted = str(task_id or "")
            session = str(session_name or "")
            now = time.time()
            tasks: List[Dict[str, Any]] = []
            target = None
            target_item: Dict[str, Any] | None = None
            for snap in subagent_manager.snapshot():
                elapsed = 0
                if snap.started_at:
                    elapsed = int((snap.finished_at or now) - snap.started_at)
                item = {
                    "id": snap.task_id,
                    "description": snap.description,
                    "type": snap.subagent_type,
                    "status": snap.status,
                    "stage": snap.stage,
                    "elapsed": elapsed,
                    "background": bool(snap.run_in_background),
                }
                if not session or snap.parent_session == session:
                    tasks.append(item)
                if snap.task_id == wanted:
                    target = snap
                    target_item = item
            if target is None or target_item is None:
                return {"ok": False, "task": None, "tasks": tasks,
                        "msg": "未找到该子代理任务（可能已被清理）"}
            detail = {
                **target_item,
                "stage_detail": target.stages[-1].detail if target.stages else "",
                "plan_item_id": target.plan_item_id,
                "started_at": target.started_at,
                "finished_at": target.finished_at,
                "stages": [
                    {"name": s.name, "at": s.at, "detail": s.detail}
                    for s in target.stages
                ],
                "result": str(target.result or "")[:4000],
                "error": str(target.error or ""),
                "partial": str(target.partial_result or "")[:2000],
            }
            return {"ok": True, "task": detail, "tasks": tasks}
        except Exception as exc:
            logger.debug("get_subagent_task_detail 失败", exc_info=True)
            return {"ok": False, "task": None, "tasks": [], "msg": str(exc)}

    def kill_task(self, task_id: str) -> Dict[str, Any]:
        try:
            from core.task_manager import (
                KILL_RESULT_FAILED, KILL_RESULT_IDENTITY_UNVERIFIABLE,
                KILL_RESULT_KILLED, KILL_RESULT_NATURAL_DEATH, KILL_RESULT_NOT_FOUND,
                kill_background_task_detailed,
            )
            result = kill_background_task_detailed(str(task_id or ""))
            if result == KILL_RESULT_KILLED:
                return {"ok": True, "msg": "已终止后台任务"}
            if result == KILL_RESULT_NATURAL_DEATH:
                # 进程在终止前已自行结束：任务已不在运行，目标已达成，不报"终止失败"
                return {"ok": True, "msg": "任务在终止前已自行结束"}
            if result == KILL_RESULT_NOT_FOUND:
                return {"ok": False, "msg": "未找到该后台任务"}
            if result == KILL_RESULT_IDENTITY_UNVERIFIABLE:
                return {"ok": False, "msg": "无法校验进程身份，已拒绝终止"}
            return {"ok": False, "msg": "终止失败，进程可能仍在运行"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 内嵌交互式终端（底部面板，pywinpty/ConPTY 常驻 shell）
    # 旧版右侧面板单命令终端（run/kill_terminal_command）已下线删除，
    # 交互式终端能力由 pty_terminal_* 承载——模型/工具链路不经过这里，
    # 模型执行命令仍走 execute_command 工具的权限确认流程。
    # 输出/退出经 emit_event（pty_data / pty_exit）进 _js_queue 派发。
    # ------------------------------------------------------------------
    def _emit_pty_exit(self, code) -> None:
        if self._shutting_down:
            return
        try:
            self.emit_event("pty_exit", {"code": int(code)})
        except Exception:
            logger.debug("推送终端退出事件失败", exc_info=True)

    def pty_terminal_ensure(self, cols=80, rows=24) -> Dict[str, Any]:
        """确保存在一个存活的交互式终端会话；没有或已退出则新开一个。

        以当前会话的 workdir 为起始目录；GLOBAL 自由对话按进程 cwd 兜底
        （M19 同款处理，workdir 标记字符串不能直接当目录用）。
        """
        cols, rows = clamp_terminal_size(cols, rows)
        with self._pty_lock:
            session = self._pty_session
            if session is not None and session.is_alive():
                return {"ok": True, "pid": session.pid, "cwd": session.cwd, "reused": True}
            cwd = self._session_workdir(self._current_session)
            if cwd == "GLOBAL" or not os.path.isdir(cwd):
                cwd = _resolve_real_path(os.getcwd())
            try:
                session = create_pty_terminal(
                    cwd=cwd,
                    cols=cols,
                    rows=rows,
                    on_output=lambda text: self.emit_event("pty_data", {"text": text}),
                    on_exit=self._emit_pty_exit,
                )
            except PtyTerminalError as e:
                return {"ok": False, "msg": str(e)}
            except Exception as e:
                logger.exception("启动内嵌交互式终端失败")
                return {"ok": False, "msg": str(e)}
            self._pty_session = session
            return {"ok": True, "pid": session.pid, "cwd": cwd, "reused": False}

    def pty_terminal_write(self, data: str) -> Dict[str, Any]:
        """把用户键入内容写进常驻 shell（xterm.js onData 原样字符串）。

        session.write 返回 bool：显式 False 表示输入未送达（shell 已退出、
        写入异常等），桥层如实回 {"ok": False}，前端据此提示而不是静默丢键。
        """
        data = str(data or "")
        if not data:
            return {"ok": False, "msg": "输入为空"}
        with self._pty_lock:
            session = self._pty_session
        if session is None or not session.is_alive():
            return {"ok": False, "msg": "终端未运行"}
        try:
            ok = session.write(data)
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        if ok is False:
            return {"ok": False, "msg": "终端已退出，输入未送达"}
        return {"ok": True}

    def pty_terminal_resize(self, cols=80, rows=24) -> Dict[str, Any]:
        """同步前端 xterm 视口尺寸到 ConPTY（面板拖拽/窗口缩放时调用）。

        session.resize 返回 bool：显式 False 表示尺寸未生效（会话已退出等）。
        """
        cols, rows = clamp_terminal_size(cols, rows)
        with self._pty_lock:
            session = self._pty_session
        if session is None or not session.is_alive():
            return {"ok": False, "msg": "终端未运行"}
        try:
            ok = session.resize(cols, rows)
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        if ok is False:
            return {"ok": False, "msg": "终端已退出，尺寸同步失败"}
        return {"ok": True}

    def pty_terminal_kill(self) -> Dict[str, Any]:
        """终止常驻 shell（下次 pty_terminal_ensure 会重新拉起）。"""
        with self._pty_lock:
            session = self._pty_session
            self._pty_session = None
        if session is None:
            return {"ok": True, "msg": "终端未启动"}
        try:
            session.kill()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # IDE 一键运行：命令写进既有常驻终端会话（输出继续走 pty_data 事件）
    # core.ide 的导入放在方法内：与 core.gui_path_policy 的懒导入同口径
    # （core/ide 依赖链不该拖慢 GUI 启动）
    # ------------------------------------------------------------------
    def ide_run_file(self, path: str, extra_args: str = "", command: str = "") -> Dict[str, Any]:
        """解析运行命令并写进常驻终端；不新开进程，复用 pty_terminal_ensure 的会话语义。

        运行进程由用户显式触发，不进 permission_broker（规格 §7.4），但路径策略一步
        不少：先过 _resolve_real_path 再走 gui_path_access_allowed，口径与
        open_path / read_file_preview 完全一致——否则本方法就是"往常驻 shell 里塞任意
        命令"的越权通道。

        返回 {ok, command?, source?, pid?, note?, msg?}。note 是 build_run_plan 的
        回退说明（没能按项目配置解析、退成了语言默认命令时的中文原因，无回退时为空串），
        并同步放进 msg：前端只渲染 msg 也能看到"为什么没按 launch.json 跑"。
        """
        from core.gui_path_policy import gui_path_access_allowed
        from core.ide.run_command import RunPlanError, build_run_plan

        try:
            real = _resolve_real_path(path)
            if not os.path.isfile(real):
                return {"ok": False, "msg": "文件不存在或不是普通文件，无法运行"}
            workspace = self._session_workdir(self._current_session)
            if workspace and workspace != "GLOBAL":
                # build_run_plan 内部用 os.path.abspath 按**进程 cwd** 解析相对路径，
                # 而进程 cwd 不随会话工作区切换：桥层必须传解析后的绝对路径
                workspace = _resolve_real_path(workspace)
            allowed, reason = gui_path_access_allowed(
                real, workspace, action="open", session_id=self._current_session
            )
            if not allowed:
                return {"ok": False, "msg": reason}
            try:
                plan = build_run_plan(
                    real, workspace, explicit_command=command, extra_args=extra_args
                )
            except RunPlanError as e:
                return {"ok": False, "msg": str(e)}
            ensured = self.pty_terminal_ensure()
            if ensured.get("ok") is not True:
                return {"ok": False, "msg": ensured.get("msg") or "终端启动失败，请重试"}
            written = self.pty_terminal_write(plan.command + "\r")
            if written.get("ok") is not True:
                return {"ok": False, "msg": written.get("msg") or "命令未送达终端，请重试"}
            return {
                "ok": True,
                "command": plan.command,
                "source": plan.source,
                "pid": ensured.get("pid"),
                "note": plan.note,
                "msg": plan.note,
            }
        except Exception as e:
            logger.exception("ide_run_file 失败")
            return {"ok": False, "msg": f"一键运行失败：{e}"}

    # ------------------------------------------------------------------
    # IDE 调试：适配器进程由后端托管，事件一律经 emit_event（put_nowait 入队）
    # core.ide 的导入同样放在方法内（同 ide_run_file：core/ide 依赖链不该
    # 拖慢 GUI 启动）。路径口径与 ide_run_file 完全一致：先 _resolve_real_path，
    # 再 gui_path_access_allowed——调试会起进程，这里少一步就是越权通道。
    # ------------------------------------------------------------------
    def _emit_dap_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """调试事件唯一出口：绝不在这里 evaluate_js（读线程不是派发线程）。

        emit_event 内部只做 put_nowait（队列满丢弃新事件、绝不阻塞），所以
        DAP 适配器读线程调用它是安全的：不触碰 WebView，也不会卡住读循环。
        """
        if event_type == "dap_stopped":
            self._debug_last_thread_id = int(data.get("thread_id") or 0)
            self._debug_last_frame_id = int(data.get("frame_id") or 0)
            self._debug_debuggee_exited = False
        elif event_type == "dap_continued":
            self._debug_debuggee_exited = False
        elif event_type == "dap_exited":
            # 被调试程序已退出，但**会话（适配器进程）未必结束**：DAP 里 exited 与
            # terminated 是两件事，restart 这类「重启被调试程序」的流程在 exited 之后
            # 仍然合法。所以这里只记事实、不去动会话引用，只把它当作「重新开始调试时
            # 可以安全回收旧会话」的依据（见 ide_debug_start；S3-T9 评审）。
            self._debug_debuggee_exited = True
        elif event_type == "dap_terminated":
            # 程序自然结束时适配器推 terminated：这里必须把会话引用放掉（S3-T7 评审），
            # 否则上面那个「已有调试会话在运行」的判重会把**已经结束的会话**当成在跑的
            # 会话拒绝掉，而前端 reduceDapEvent('dap_terminated') 只把 debugState 置
            # stopped、并不调 ide_debug_stop → 用户必须手动点一次「停止」才能重新调试。
            # 同时**必须回收适配器进程**（S3-T8 评审）：_discard_debug_session 里起临时
            # 线程去做 stop()，本方法仍在 DAP 读线程内，只做非阻塞投递。
            self._discard_debug_session()
        if self._shutting_down:
            return
        try:
            self.emit_event(event_type, data)
        except Exception:
            logger.debug("推送调试事件失败", exc_info=True)

    def _reset_debug_cursors(self) -> None:
        """复位「当前线程 / 当前帧」记忆。

        线程号与帧号只在**一次会话**内有意义：上一会话的值留到新会话，会让第一条
        `dap_stopped` 到达之前的 continue / next / stepIn / stepOut / pause 与
        stackTrace / evaluate 拿着失效 id 去问适配器，被拒成 "thread not found"
        （S3-T7 评审）。
        """
        self._debug_last_thread_id = 0
        self._debug_last_frame_id = 0

    def _discard_debug_session(self) -> None:
        """放掉会话引用、复位线程/帧记忆，并把进程回收投递给临时线程。

        **可从 DAP 读线程调用**：这里绝不直接 `session.stop()`——stop() 内部要发同步
        disconnect 请求，读线程自己发请求会命中 `DapReaderBlockingError` 护栏（见
        `DapProcess._reject_reader_blocking`）。只起一个临时线程去做 stop()，投递本身
        非阻塞，读循环不会被拖住。

        也不能像旧实现那样指望 `DebugSession.__del__` 兜底（S3-T8 评审三条理由）：
        ① `DapProcess.on_event` 是 `DebugSession` 的绑定方法，二者互指成环，带 `__del__`
           的环只能靠分代 GC 非确定回收；
        ② 读线程活着时环根本收不了——`ide-dap-reader` 的运行帧强引用 `DapProcess`，而
           dlv 的 TCP dap-server / lldb-dap 在 terminated 后**不自行退出**，读线程永久
           阻塞在 read 上，`__del__` 永不执行，进程与线程一路泄漏；
        ③ 关停兜底也失效——`_shutdown_debug_session` 读的也是 `self._debug_session`，
           此时已被置 `None`，`if session is None: return` 直接短路，适配器子进程不会
           随父进程退出被杀，最终成为孤儿进程。
        """
        with self._debug_lock:
            session = self._debug_session
            self._debug_session = None
            self._debug_debuggee_exited = False
            reaper = None
            if session is not None:
                reaper = threading.Thread(
                    target=self._reap_debug_session,
                    args=(session,),
                    daemon=True,
                    name="ide-dap-reap",
                )
                self._debug_reapers.append(reaper)
        self._reset_debug_cursors()
        if reaper is None:
            return
        reaper.start()

    def _reap_debug_session(self, session: Any) -> None:
        """临时线程体：stop() 幂等，失败也不能把回收线程炸掉（回收是 best-effort 兜底）。"""
        try:
            session.stop()
        except Exception:
            logger.debug("回收调试适配器会话失败", exc_info=True)
        finally:
            with self._debug_lock:
                try:
                    self._debug_reapers.remove(threading.current_thread())
                except ValueError:
                    pass

    def _shutdown_debug_session(self, join_timeout: float = 5.0) -> None:
        """结束会话并回收适配器进程（幂等；shutdown 与 ide_debug_stop 共用）。

        除了当前会话，还要 join 掉 terminated 时投递出去的临时回收线程：否则关停
        （或进程退出）会与那条线程抢时序，极端情况下父进程先走，适配器变成孤儿。
        """
        with self._debug_lock:
            session = self._debug_session
            self._debug_session = None
            self._debug_debuggee_exited = False
            reapers = list(self._debug_reapers)
        # 会话回收时一并复位线程/帧记忆：下一次调试不会继承上一会话的失效 id
        self._reset_debug_cursors()
        if session is not None:
            session.stop()
        current = threading.current_thread()
        for reaper in reapers:
            if reaper is current:
                continue  # 防御：绝不在回收线程里 join 自己
            reaper.join(join_timeout)

    def _debug_path_denied(self, real_path: str, workspace: str, label: str) -> str:
        """路径策略出口：入参必须已过 _resolve_real_path（口径同 ide_run_file）。

        返回空串表示放行；否则是可诊断的中文原因（前缀说明是哪个路径被拒，
        用户能直接看出该去改工作区还是关沙盒）。
        """
        from core.gui_path_policy import gui_path_access_allowed

        allowed, reason = gui_path_access_allowed(
            real_path, workspace, action="open", session_id=self._current_session
        )
        if allowed:
            return ""
        return f"{label}不可访问：{reason}"

    def ide_debug_availability(self) -> Dict[str, Any]:
        """探测各语言调试适配器（给调试面板与设置页展示安装状态）。"""
        from core.ide.debug_adapters import adapter_availability

        return {"ok": True, "adapters": adapter_availability()}

    def ide_debug_start(
        self,
        path: str = "",
        breakpoints: Optional[List[Dict[str, Any]]] = None,
        program: str = "",
        args: Optional[List[str]] = None,
        stop_on_entry: bool = False,
    ) -> Dict[str, Any]:
        """启动调试：路径策略 → 探测适配器 → 起会话 → 下发断点 → configurationDone。

        program / cwd 也要过策略（S3-T4 评审）：两者都会原样进 DAP launch 参数，
        不过策略就等于"用调试器起任意程序"。
        """
        from core.ide.dap_manager import DapProtocolError, DapRequestError
        from core.ide.debug_adapters import probe_adapter
        from core.ide.debug_session import DebugSession, DebugSessionError
        from core.ide.language_registry import language_id_for_path

        with self._debug_lock:
            existing = self._debug_session
            debuggee_exited = self._debug_debuggee_exited
        if existing is not None and existing.is_alive() and not debuggee_exited:
            return {"ok": False, "msg": "已有调试会话在运行，请先停止"}
        if existing is not None:
            # 会话对象还在、但适配器已经不可用（程序自然结束推了 terminated，或适配器进程
            # 意外退出连 terminated 都没送到），或者适配器只发了 exited 而进程还赖着不走
            # （DAP 里 exited 只表示**被调试程序**退出，会话未必结束）：先回收掉再放行。
            # 只看 `is not None` 会让用户重新开始调试时永远拿到「已有调试会话在运行」却
            # 无会话可停（S3-T7 评审）；exited 这条是 S3-T9 评审补的：前端
            # reduceDapEvent('dap_exited') 会清空 sessionId、界面据此放行「再次开始调试」，
            # 后端不认 exited 就会两边打架。
            self._shutdown_debug_session()
        real = _resolve_real_path(path)
        if not os.path.isfile(real):
            return {"ok": False, "msg": "文件不存在或不是普通文件，无法调试"}
        workspace = self._session_workdir(self._current_session)
        if workspace and workspace != "GLOBAL":
            # 与 ide_run_file 同口径：工作区先归一化再交给策略做包含判断
            workspace = _resolve_real_path(workspace)
        denied = self._debug_path_denied(real, workspace, "调试目标文件")
        if denied:
            return {"ok": False, "msg": denied}
        program_real = ""
        if str(program or "").strip():
            # program 是 launch 参数里的「被调试程序」：同样先 realpath 再校验
            program_real = _resolve_real_path(str(program))
            denied = self._debug_path_denied(program_real, workspace, "调试程序")
            if denied:
                return {"ok": False, "msg": denied}
        # cwd 由目标文件推导（适配器的工作目录），送进适配器前照样过策略
        cwd_real = os.path.dirname(real)
        denied = self._debug_path_denied(cwd_real, workspace, "调试工作目录")
        if denied:
            return {"ok": False, "msg": denied}
        language_id = language_id_for_path(real)
        probe = probe_adapter(language_id)
        if not probe.ok or probe.spec is None:
            return {"ok": False, "msg": probe.hint or f"星瑶暂不支持调试该语言：{language_id or '未知'}"}
        session = DebugSession(
            probe.spec,
            program=program_real or real,
            cwd=cwd_real,
            args=list(args or []),
            stop_on_entry=bool(stop_on_entry),
            emit=self._emit_dap_event,
            # debugpy 的适配器必须用启动它自己的解释器，否则 -m debugpy.adapter 找不到模块
            python=probe.spec.command[0] if language_id == "python" else "",
        )
        try:
            info = session.start()
        except DebugSessionError as e:
            session.stop()
            return {"ok": False, "msg": str(e)}
        except (DapProtocolError, DapRequestError) as e:
            session.stop()
            return {"ok": False, "msg": f"启动调试适配器失败：{e}"}
        try:
            for item in breakpoints or []:
                if not isinstance(item, dict):
                    continue
                entry_path = str(item.get("path") or real)
                lines = [int(line) for line in (item.get("lines") or [])]
                session.set_breakpoints(entry_path, lines)
            session.configuration_done()
        except (DapProtocolError, DapRequestError, DebugSessionError) as e:
            session.stop()
            return {"ok": False, "msg": f"调试初始化失败：{e}"}
        with self._debug_lock:
            self._debug_session = session
        self._emit_dap_event("dap_status", {"session_id": session.session_id, "state": "running", "msg": ""})
        return {
            "ok": True,
            "session_id": session.session_id,
            "adapter": probe.spec.adapter_id,
            "pid": info.get("pid"),
        }

    def ide_debug_set_breakpoints(self, path: str, lines: Optional[List[int]] = None) -> Dict[str, Any]:
        """会话未启动时断点只存在前端，这里返回 pending 由启动时统一下发。"""
        from core.ide.dap_manager import DapProtocolError, DapRequestError
        from core.ide.debug_session import DebugSessionError

        with self._debug_lock:
            session = self._debug_session
        if session is None:
            return {"ok": True, "pending": True, "verified": []}
        try:
            verified = session.set_breakpoints(_resolve_real_path(path), [int(line) for line in (lines or [])])
        except (DebugSessionError, DapProtocolError, DapRequestError) as e:
            return {"ok": False, "msg": f"设置断点失败：{e}"}
        return {"ok": True, "pending": False, "verified": verified}

    def ide_debug_command(self, command: str, thread_id: int = 0) -> Dict[str, Any]:
        """调试控制：continue / pause / next / stepIn / stepOut / restart。

        `thread_id` 可选：显式传 >0 的线程号时优先用它（暂停的典型时机是「程序正在
        运行」，此时还没有 dap_stopped 记录、桥层记忆是 0，前端先取线程表拿到真实线程号
        再传进来；S3-T9 评审）；传 0/不传时沿用既有回退逻辑（桥层记忆 → 会话自报）。
        """
        from core.ide.dap_manager import DapProtocolError, DapRequestError
        from core.ide.debug_session import DebugSessionError

        handlers = {
            "continue": lambda session, thread_id: session.continue_(thread_id),
            "pause": lambda session, thread_id: session.pause(thread_id),
            "next": lambda session, thread_id: session.next_(thread_id),
            "stepIn": lambda session, thread_id: session.step_in(thread_id),
            "stepOut": lambda session, thread_id: session.step_out(thread_id),
            "restart": lambda session, thread_id: session.restart(),
        }
        handler = handlers.get(str(command))
        if handler is None:
            return {"ok": False, "msg": f"不支持的调试命令：{command}"}
        with self._debug_lock:
            session = self._debug_session
        if session is None:
            return {"ok": False, "msg": "调试未启动"}
        # 当前暂停线程来自 dap_stopped（桥层记录）；DebugSession 自己不带这个状态，
        # 用 0 发 continue/next 会被适配器拒绝（"thread not found"）
        thread_id = int(thread_id or 0)
        if thread_id <= 0:
            thread_id = self._debug_thread_id() or int(getattr(session, "_active_thread_id", 0) or 0)
        try:
            handler(session, int(thread_id or 0))
        except (DebugSessionError, DapProtocolError, DapRequestError) as e:
            return {"ok": False, "msg": f"{command} 失败：{e}"}
        return {"ok": True}

    def ide_debug_threads(self) -> Dict[str, Any]:
        return self._debug_query("threads", lambda session: session.threads(), field="threads")

    def ide_debug_stack_trace(self, thread_id: int = 0, levels: int = 20) -> Dict[str, Any]:
        return self._debug_query(
            "stackTrace",
            lambda session: session.stack_trace(int(thread_id) or self._debug_thread_id(), int(levels)),
            field="frames",
        )

    def ide_debug_scopes(self, frame_id: int = 0) -> Dict[str, Any]:
        return self._debug_query("scopes", lambda session: session.scopes(int(frame_id)), field="scopes")

    def ide_debug_variables(self, variables_reference: int = 0) -> Dict[str, Any]:
        return self._debug_query(
            "variables", lambda session: session.variables(int(variables_reference)), field="variables"
        )

    def ide_debug_evaluate(self, expression: str, frame_id: int = 0) -> Dict[str, Any]:
        """求值：结果平铺返回（result / type / variablesReference），前端直接渲染。"""
        from core.ide.dap_manager import DapProtocolError, DapRequestError
        from core.ide.debug_session import DebugSessionError

        with self._debug_lock:
            session = self._debug_session
        if session is None:
            return {"ok": False, "msg": "调试未启动", "result": "", "type": "", "variablesReference": 0}
        try:
            payload = session.evaluate(str(expression), int(frame_id) or self._debug_frame_id())
        except (DebugSessionError, DapProtocolError, DapRequestError) as e:
            return {"ok": False, "msg": f"求值失败：{e}", "result": "", "type": "", "variablesReference": 0}
        return {"ok": True, **payload}

    def ide_debug_stop(self) -> Dict[str, Any]:
        """停止调试并回收适配器进程（幂等）。"""
        self._shutdown_debug_session()
        self._emit_dap_event("dap_status", {"session_id": "", "state": "stopped", "msg": ""})
        return {"ok": True}

    def _debug_query(self, label: str, action, field: str) -> Dict[str, Any]:
        """调试查询的统一出口：无会话 / 适配器报错的返回结构在这里收敛。"""
        from core.ide.dap_manager import DapProtocolError, DapRequestError
        from core.ide.debug_session import DebugSessionError

        with self._debug_lock:
            session = self._debug_session
        if session is None:
            return {"ok": False, "msg": "调试未启动", field: None}
        try:
            return {"ok": True, field: action(session)}
        except (DebugSessionError, DapProtocolError, DapRequestError) as e:
            return {"ok": False, "msg": f"{label} 失败：{e}", field: None}

    def _debug_thread_id(self) -> int:
        return int(getattr(self, "_debug_last_thread_id", 0) or 0)

    def _debug_frame_id(self) -> int:
        return int(getattr(self, "_debug_last_frame_id", 0) or 0)

    # ------------------------------------------------------------------
    # 模型管理
    # ------------------------------------------------------------------
    def get_model_settings(self) -> Dict[str, Any]:
        """返回不含 API Key 明文的完整模型设置快照。"""
        config_data = load_config()
        # active_model_id 必须返回"当前会话生效模型"而非陈旧的全局默认：
        # GUI 按会话绑定模型、不回写全局值，全局值可能属于另一厂商的同名
        # 模型；直接返回会让前端 applyModelSettings 把界面选中态打回错误
        # 厂商（表现为"选了 A 厂商的模型自动跳到 B 厂商"）。
        effective_model_id = str(config_data.get("active_model_id") or "")
        try:
            active = get_active_model(getattr(self, "_current_session", None))
            if active and active.get("id"):
                effective_model_id = str(active["id"])
        except Exception:
            logger.debug("解析会话生效模型失败，active_model_id 回退全局默认", exc_info=True)
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
                    # 电脑操控能力契约（设计 9.1）：supports_* 缺省 false；
                    # computer_protocol 缺省返回 ""（自动：视觉+工具调用即启用），
                    # 前端编辑表单据此回填「自动」，显式 none 仍为主动关闭。
                    "supports_function_calling": bool(
                        model.get("supports_function_calling", False)
                    ),
                    "computer_protocol": str(model.get("computer_protocol") or ""),
                    # 最大输出：缺省 None（前端输入框留空 = 使用内置默认）
                    "max_tokens": (
                        int(model["max_tokens"]) if model.get("max_tokens") else None
                    ),
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
            "active_model_id": effective_model_id,
            "secondary_model_id": str(config_data.get("secondary_model_id") or ""),
            "secondary_model_roles": get_secondary_roles(),
        }

    def get_provider_api_key(self, provider_id: str) -> Dict[str, Any]:
        """按需返回厂商 API Key 明文（仅设置页用户主动点击"显示/复制"时调用）。"""
        try:
            config_data = load_config()
            for provider in config_data.get("providers", []):
                if str(provider.get("id")) == str(provider_id):
                    return {"ok": True, "api_key": str(provider.get("api_key") or "")}
            return {"ok": False, "msg": "厂商不存在"}
        except Exception as e:
            logger.exception("get_provider_api_key 失败")
            return {"ok": False, "msg": str(e)}

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
        supports_function_calling: bool = True,
        computer_protocol: str = "",
        max_tokens: int | None = None,
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
            try:
                # 最大输出：空/0 = 不落键（请求时走内置默认表）
                normalized_max_tokens = max(int(max_tokens), 0) or None if max_tokens is not None else None
            except (TypeError, ValueError):
                normalized_max_tokens = None
            # 传输前缀与厂商作用域模型 ID 的拼接规则由 model_manager 统一处理。
            # computer_protocol 缺省/"" = 自动（不落键，视觉+工具调用即启用），
            # 不得强转 "none"（旧实现导致新视觉模型永不自动激活）。
            model_id = add_model_to_provider(
                real_provider_id,
                str(model_name).strip(),
                window,
                supports_vision=bool(supports_vision),
                supports_function_calling=bool(supports_function_calling),
                computer_protocol=str(computer_protocol or ""),
                max_tokens=normalized_max_tokens,
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
        supports_function_calling: bool | None = None,
        computer_protocol: str | None = None,
        max_tokens: int | None = None,
    ) -> Dict[str, Any]:
        try:
            mm_update_model(
                str(provider_id),
                str(model_id),
                name=str(model_name or "").strip(),
                context_window=int(context_window),
                supports_vision=bool(supports_vision),
                # None 表示保留既有值（model_manager 语义），旧前端调用不受影响
                supports_function_calling=supports_function_calling,
                computer_protocol=computer_protocol,
                max_tokens=max_tokens,
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

    def fetch_provider_models(self, provider_id: str) -> Dict[str, Any]:
        """拉取厂商端点的可用模型列表（GET {base_url}/v1/models），供添加模型时一键填充。

        与对话请求链路完全独立：短超时、一次性 httpx 请求，不触碰共享连接池。
        鉴权头同时携带 Bearer 与 x-api-key——OpenAI 网关认 Bearer，Anthropic
        兼容端点认 x-api-key（实测阶跃 /v1/models 单发 x-api-key 会 401）。
        """
        try:
            real_provider_id = self._resolve_provider_id(provider_id)
            if real_provider_id is None:
                return {"ok": False, "msg": f"未找到供应商: {provider_id}"}
            provider = None
            for p in load_config().get("providers", []):
                if str(p.get("id")) == str(real_provider_id):
                    provider = p
                    break
            if provider is None:
                return {"ok": False, "msg": "厂商不存在"}
            base_url = str(provider.get("base_url") or "").strip()
            if not base_url:
                return {"ok": False, "msg": "该厂商未配置 Base URL"}
            api_key = str(provider.get("api_key") or "")
            # base_url 可能已含版本段（OpenAI 惯例 …/v1，阶跃 step_plan/v1），
            # 也可能是裸端点（如 …/step_plan）：前者直接拼 /models，后者补 /v1/models，
            # 绝不能重复拼出版本段（曾出现 /v1/v1/models 404）。
            base = base_url.rstrip("/")
            url = (base + "/models") if base.endswith("/v1") else (base + "/v1/models")
            headers = {"accept": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
                headers["x-api-key"] = api_key
            from core.llm_policy import tokenrhythm_agent_headers
            import httpx as _httpx

            headers.update(tokenrhythm_agent_headers(url))
            try:
                resp = _httpx.get(url, headers=headers, timeout=15.0)
            except Exception as exc:
                return {"ok": False, "msg": f"无法连接 {base_url}: {exc}"}
            if resp.status_code != 200:
                body = (resp.text or "").strip()[:200]
                return {"ok": False, "msg": f"获取失败 (HTTP {resp.status_code}): {body}"}
            try:
                data = resp.json()
            except ValueError:
                return {"ok": False, "msg": "响应不是有效 JSON（该端点可能不支持 /v1/models）"}
            items = data.get("data") if isinstance(data, dict) else data
            if not isinstance(items, list):
                return {"ok": False, "msg": "响应格式无法解析（非 OpenAI /v1/models 形状）"}

            # 上下文/多模态元数据字段名因厂商而异，逐个候选键提取：
            # - 上下文：阶跃 max_input_tokens、OpenRouter/Together context_length、
            #   Groq context_window、vLLM max_model_len
            # - 最大输出：OpenRouter max_completion_tokens、通用 max_output_tokens
            # - 多模态：阶跃 enable_vision_input、通用 supports_vision、
            #   OpenRouter architecture.input_modalities 含 "image"
            _context_keys = (
                "max_input_tokens", "context_length", "context_window",
                "max_model_len", "max_context_length",
            )
            _output_keys = ("max_completion_tokens", "max_output_tokens")
            _vision_bool_keys = ("enable_vision_input", "supports_vision", "supports_image_input")

            def _extract_meta(entry_item: dict):
                context_window = None
                for key in _context_keys:
                    try:
                        val = int(entry_item.get(key))
                    except (TypeError, ValueError):
                        continue
                    if val > 0:
                        context_window = val
                        break
                max_output = None
                for key in _output_keys:
                    try:
                        val = int(entry_item.get(key))
                    except (TypeError, ValueError):
                        continue
                    if val > 0:
                        max_output = val
                        break
                supports_vision = None
                for key in _vision_bool_keys:
                    if isinstance(entry_item.get(key), bool):
                        supports_vision = entry_item[key]
                        break
                if supports_vision is None:
                    arch = entry_item.get("architecture")
                    if isinstance(arch, dict):
                        modalities = arch.get("input_modalities")
                        if isinstance(modalities, list) and modalities:
                            supports_vision = "image" in [str(m).lower() for m in modalities]
                return context_window, max_output, supports_vision

            from core.model_spec_table import lookup_model_spec

            models: List[Dict[str, Any]] = []
            for item in items:
                if isinstance(item, dict):
                    mid = str(item.get("id") or "").strip()
                    model_type = str(item.get("model_type") or "")
                    context_window, max_output, supports_vision = _extract_meta(item)
                elif isinstance(item, str):
                    mid, model_type = item.strip(), ""
                    context_window, max_output, supports_vision = None, None, None
                else:
                    continue
                if mid:
                    # 厂商未提供元数据的字段回填本地规格表（官方文档核实值）
                    spec_context, spec_output = lookup_model_spec(mid)
                    if context_window is None:
                        context_window = spec_context
                    if max_output is None:
                        max_output = spec_output
                    models.append({
                        "id": mid,
                        "model_type": model_type,
                        "context_window": context_window,
                        "supports_vision": supports_vision,
                        "max_tokens": max_output,
                    })

            def _sort_key(entry: Dict[str, str]):
                # 明确标注为语音/图像/embedding 的条目排到末尾，对话模型靠前
                model_type = entry.get("model_type") or ""
                is_non_chat = any(
                    keyword in model_type.lower()
                    for keyword in ("语音", "图像", "音频", "文生图", "embedding", "rerank", "tts", "asr")
                )
                return (1 if is_non_chat else 0, entry["id"].lower())

            models.sort(key=_sort_key)
            return {"ok": True, "models": models, "count": len(models), "base_url": base_url}
        except Exception as e:
            logger.exception("fetch_provider_models 失败")
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
