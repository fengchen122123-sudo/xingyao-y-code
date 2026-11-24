import base64
import copy
import ctypes
import glob
import json
import logging
import os
import queue
import pathlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
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
from core import novel_service
from core import secretary
from core.secretary_dispatch import SecretaryDispatchError
from core.secretary_dispatches import (
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    DispatchStore,
)
from core import project_profile
from core.project_prompt_suggestions import build_workspace_prompt_suggestions
from core.gui_theme import ThemeError, get_repository as get_gui_theme_repository
from core.gui_terminal import PtyTerminalError, clamp_terminal_size, create_pty_terminal
from gui_git_mixin import GitBridgeMixin
from gui_browser_mixin import BrowserBridgeMixin
from gui_snip_mixin import SnipBridgeMixin
from gui_pet_mixin import PetBridgeMixin
from core.model_manager import (
    add_preset_provider as mm_add_preset_provider,
    add_model_to_provider,
    add_provider as mm_add_provider,
    delete_provider as mm_delete_provider,
    delete_model as mm_delete_model,
    effective_provider_api_key,
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
from core import tokenrhythm_auth
from core.reasoning_capabilities import build_reasoning_snapshot
from core.permission_broker import permission_broker
from core.skills import (
    get_active_skill_ids,
    save_active_skill_ids,
    scan_all_skills,
    restore_builtin_skill,
    delete_skill,
    get_released_builtin_names,
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

_STALL_TIMEOUT_SECONDS = 1800.0
_STOP_WATCHDOG_SECONDS = 60.0
_STALL_POLL_SECONDS = 10.0


def _force_finish_reason(idle_seconds: float, stop_requested: bool) -> Optional[str]:
    if stop_requested and idle_seconds >= _STOP_WATCHDOG_SECONDS:
        return "stop"
    if idle_seconds >= _STALL_TIMEOUT_SECONDS:
        return "stall"
    return None


# CLI 的 prompt_toolkit 着色标记（<ansiyellow> 等）：GUI 不渲染，桥接层统一剥离，
# 否则用户会在提示里看到 <ansiyellow> 原文。CLI 路径不经此处，不受影响。
_ANSI_MARKUP_RE = re.compile(r"</?ansi\w*>")
_RGB_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DWM_COLOR_CAPABILITY_SUPPORTED = "supported"
_DWM_COLOR_CAPABILITY_UNSUPPORTED = "unsupported"
_DWM_COLOR_CAPABILITY_PENDING = "pending"
_DWM_COLOR_CAPABILITY_NOT_APPLICABLE = "not_applicable"
_DWM_COLOR_CAPABILITY_UNKNOWN = "unknown"

_BACKGROUND_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})

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

TURN_SOURCES = ("local", "lan", "feishu", "mcp", "goal", "server")


def _cursor_snap_vk() -> Optional[int]:
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
            return 0x25
        if point.x >= mi.rcMonitor.right - 2:
            return 0x27
        if point.y <= mi.rcMonitor.top + 2:
            return 0x26
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
    if not isinstance(color, str) or not _RGB_HEX_RE.fullmatch(color):
        raise ValueError("color must be #RRGGBB")
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    return red | (green << 8) | (blue << 16)


def _load_initial_gui_theme_state() -> Dict[str, Any]:
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
    return _ANSI_MARKUP_RE.sub("", str(text)).strip()


class _CompactStartAck(dict):

    def __str__(self) -> str:
        return "已在后台开始压缩，完成后会在当前对话给出提示"


def _session_from_turn_id(turn_id: str) -> str:
    value = str(turn_id or "")
    return value.rsplit(":", 1)[0] if ":" in value else ""


def _undo_node_state_dict(state) -> Dict[str, Any]:
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
    try:
        from core.computer.controller import get_computer_control_service

        status = str(get_computer_control_service().snapshot().status or "")
    except Exception:
        return True
    return status not in ("running", "paused", "waiting_for_user")


def _ensure_session_model_binding(memory, session_name: str, model_id, workdir: str) -> None:
    memory.ensure_session(session_name, workdir=workdir, source="GUI")
    normalized_model_id = str(model_id or "").strip() or None
    if memory.get_session_model_id(session_name) is None and normalized_model_id is not None:
        memory.set_session_model_id(session_name, normalized_model_id)

# MCP 单例：import 失败（如未安装 mcp 依赖）不能拖垮整个 GUI
try:
    from core.mcp_client import mcp_manager as _mcp_manager_singleton
except Exception:  # pragma: no cover - 依赖缺失时的降级路径
    _mcp_manager_singleton = None

TOKEN_FLUSH_INTERVAL = 0.06

JS_QUEUE_MAXSIZE = 2000

# ask_user 提问超时（秒）：超时按取消处理。权限确认与 CLI 一致无限等待，
# 不再因用户暂时离开而自动拒绝（拒绝会让模型拿到"用户拒绝"继续跑偏）
PERMISSION_TIMEOUT_SECONDS = 300

FILE_PREVIEW_MAX_BYTES = 256 * 1024
IMAGE_PREVIEW_MAX_BYTES = 20 * 1024 * 1024
PDF_PREVIEW_MAX_BYTES = 32 * 1024 * 1024
DOCX_PREVIEW_MAX_BYTES = 32 * 1024 * 1024
DOCX_PREVIEW_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
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

FILE_TREE_MAX_DEPTH = 4
FILE_TREE_MAX_ENTRIES_PER_DIR = 100
FILE_TREE_SKIP_DIRS = {
    ".git", "venv", ".venv", "node_modules", "__pycache__", "dist", "build",
}

TEST_MODEL_TIMEOUT = 40


def _probe_keys_for_provider(provider: Dict[str, Any]) -> List[str]:
    try:
        from core.provider_keys import split_provider_keys

        keys = split_provider_keys(str(provider.get("api_key") or ""))
    except Exception:
        keys = []
    if not keys:
        return [""]
    try:
        current = str(effective_provider_api_key(provider) or "")
    except Exception:
        current = ""
    if current and current in keys:
        keys = [current] + [key for key in keys if key != current]
    return keys

# 后台子代理全部终态后自动发起的汇总回合（与 CLI 的内部汇总指令同文案）。
# 前缀复用 core.injection_markers 的常量：注入文案与展示层过滤前缀同源，
# 从结构上杜绝"改了注入文案、忘改过滤表"导致的泄漏（这两类曾渲染成用户
# 气泡并污染红点导航，用户报告过）。
_BG_SUMMARY_PROMPT = (
    BG_SUMMARY_INJECTION_PREFIX + "。如果某些任务失败了，"
    "可以用你自身的工具（如 search_news / fetch_url 等）接手完成；"
    "但已被用户取消的任务不要接手或重试，如实说明即可。"
)
_BG_COMMAND_SUMMARY_PROMPT = (
    BG_COMMAND_SUMMARY_INJECTION_PREFIX
    + "，请用一句话简短总结它的结果（成功/失败 + 关键信息，不要复述命令本身）。"
)
_BG_SUMMARY_DISPLAY_TEXT = "📦 所有后台子代理任务已结束，正在自动汇总结果…"
_BG_COMMAND_SUMMARY_DISPLAY_TEXT = "📦 后台任务已完成，正在总结结果…"
_SECRETARY_DISPATCH_SUMMARY_DISPLAY_TEXT = "📋 派发任务已产生结果，秘书正在整理汇报…"


def _resolve_real_path(path: str) -> str:
    try:
        p = pathlib.Path(path)
        if p.exists():
            return str(p.resolve())
        return os.path.abspath(path)
    except Exception:
        return os.path.abspath(path)


def _global_workspace_root() -> str:
    try:
        from core.ai_output_paths import resolve_ai_output_dir

        root = str(resolve_ai_output_dir(respect_workdir=False))
        os.makedirs(root, exist_ok=True)
        return _resolve_real_path(root)
    except Exception:
        fallback = os.path.join(os.path.expanduser("~"), ".ycode", "tmp")
        try:
            os.makedirs(fallback, exist_ok=True)
            return _resolve_real_path(fallback)
        except OSError:
            # 目录不可创建时退回旧行为，宁可共用工作区也不要让回合起不来
            return _resolve_real_path(os.getcwd())


def _is_app_preview_artifact(real_path: str) -> bool:
    try:
        from core import web_preview

        preview_dir = os.path.normcase(os.path.realpath(str(web_preview.get_preview_dir())))
        candidate = os.path.normcase(os.path.realpath(str(real_path)))
        return os.path.dirname(candidate) == preview_dir
    except Exception:
        return False
def _reveal_via_shell_api(path: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
        ole32.CoInitialize(None)
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong,
        ]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        pidl = shell32.ILCreateFromPathW(path)
        if not pidl:
            return False
        try:
            hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            return (hr & 0xFFFFFFFF) == 0
        finally:
            shell32.ILFree(pidl)
    except Exception:
        logger.debug("SHOpenFolderAndSelectItems 不可用，退回 explorer 命令行", exc_info=True)
        return False


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


_BROWSER_BRIDGE_METHODS = frozenset(
    {
        "browser_pane_open",
        "browser_pane_set_bounds",
        "browser_pane_navigate",
        "browser_pane_back",
        "browser_pane_forward",
        "browser_pane_reload",
        "browser_pane_stop",
        "browser_pane_hide",
        "browser_pane_get_state",
        "browser_pane_assign",
        "browser_pane_set_active_session",
    }
)


class _GuiContext:

    def __init__(self, bridge: "ApiBridge"):
        self._bridge = bridge
        self.ui_flavor = "gui"

    def __getattr__(self, name: str):
        if name in _BROWSER_BRIDGE_METHODS:
            bridge = object.__getattribute__(self, "_bridge")
            return getattr(bridge, name)
        raise AttributeError(name)

    @property
    def app_instance(self):
        return None

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

    def invalidate_ui(self):
        pass


def _reasoning_snapshot_for_model(model: dict | None) -> dict:
    snapshot = build_reasoning_snapshot(model)
    model_id = str((model or {}).get("id") or "")
    if model_id and snapshot["kind"] not in {"fixed", "hidden"}:
        try:
            snapshot["choice"] = get_model_reasoning_choice(model_id)
        except ValueError:
            # 配置里存的 reasoning_choice / reasoning_effort 已不在该模型当前能力表的
            # 合法档位内（例如曾支持 low、后来能力表收窄）——这正是
            # _reasoning_choice_from_model 的失效档位信号（只捕获 ValueError）。
            # 这里绝不能把整行隐藏（置 reasoning_visible=False）：隐藏后 UI 不再渲染
            # 思考设置行，用户就失去了从界面改回合法档位的入口，只能手改配置文件。
            # 故只把选择回退为默认档展示，其余字段（visible / options / kind /
            # disabled_reason）仍按能力表原样给出；用户下次保存时
            # set_model_reasoning_choice 的正常校验会纠正落库值。
            logger.debug(
                "模型 %s 存了已失效的思考档位，按默认档展示: %s",
                model_id,
                model.get("reasoning_choice") or model.get("reasoning_effort"),
            )
            snapshot["choice"] = "default"
    return snapshot


_MEDIA_SAVE_IMAGE_FILTER = "图片文件 (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.avif)"
_MEDIA_SAVE_VIDEO_FILTER = "视频文件 (*.mp4;*.webm)"
_MEDIA_SAVE_VIDEO_EXTS = frozenset({".mp4", ".webm"})


def _media_save_file_types(ext: str) -> tuple:
    if (ext or "").lower() in _MEDIA_SAVE_VIDEO_EXTS:
        return (_MEDIA_SAVE_VIDEO_FILTER,)
    return (_MEDIA_SAVE_IMAGE_FILTER,)


def _media_save_filename(suggested_name: str, ext: str) -> str:
    """把建议文件名清理成安全的单段文件名，并强制使用 Blob 的真实扩展名。

    建议名来自图片 alt 或视频 caption / 原始文件名，可能带路径成分、
    Windows 非法字符，或与实际内容不符的扩展名（如 alt 写成 xxx.jpg 而
    Blob 是 png）。
    """
    name = re.split(r"[\\/]", (suggested_name or "").strip())[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if "." in name:
        name = name.rsplit(".", 1)[0].strip(" .")
    fallback = "ycode-video" if (ext or "").lower() in _MEDIA_SAVE_VIDEO_EXTS else "ycode-image"
    return (name or fallback) + ext


_SECRETARY_SEARCH_LIMIT_MAX = 50
_SECRETARY_DISPATCH_LIMIT_MAX = 50
_SECRETARY_DISPATCH_EXCERPT_CHARS = 200
# 同一会话最多回带几条命中片段（与 memory 检索口径一致，防止长会话刷屏）
_SECRETARY_SNIPPET_MAX = 3
_SECRETARY_DRAFT_NAME_RE = re.compile(r"^session_[a-f0-9]{6}$")
_SECRETARY_NEW_SESSION_MARKS = ("新会话", "新对话", "new", "new_session")
_SECRETARY_PROFILE_HEADER = "【项目背景（自动带入，可忽略）】"
_SECRETARY_TASK_HEADER = "【任务】"
_SECRETARY_BATCH_MAX = 8


def _secretary_parse_date(value: str, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        time.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise SecretaryDispatchError(f"{field} 需要 YYYY-MM-DD 格式的日期，收到：{raw}")
    return raw


def _secretary_in_date_range(updated_at: str, date_from: str, date_to: str) -> bool:
    day = str(updated_at or "")[:10]
    if not day:
        return not (date_from or date_to)
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    return True


def _secretary_batch_cycle_path(dep_map: Dict[int, List[int]]) -> List[int]:
    unvisited, on_stack, finished = 0, 1, 2
    color = {index: unvisited for index in dep_map}
    stack: List[int] = []
    stack_pos: Dict[int, int] = {}

    def _visit(node: int) -> List[int]:
        color[node] = on_stack
        stack_pos[node] = len(stack)
        stack.append(node)
        for upstream in dep_map.get(node) or []:
            if upstream not in color:
                continue
            if color[upstream] == on_stack:
                start = stack_pos.get(upstream, 0)
                return list(stack[start:]) + [upstream]
            if color[upstream] == unvisited:
                found = _visit(upstream)
                if found:
                    return found
        stack.pop()
        stack_pos.pop(node, None)
        color[node] = finished
        return []

    for index in sorted(dep_map):
        if color[index] != unvisited:
            continue
        found = _visit(index)
        if found:
            return found
    return []


def _secretary_parse_batch_tasks(tasks) -> List[Dict[str, Any]]:
    if isinstance(tasks, (str, bytes)) or not isinstance(tasks, (list, tuple)):
        raise SecretaryDispatchError("dispatch_batch 的 tasks 需要是任务数组")
    items = list(tasks)
    if not items:
        raise SecretaryDispatchError("dispatch_batch 至少需要一条任务")
    if len(items) > _SECRETARY_BATCH_MAX:
        raise SecretaryDispatchError(
            f"单批最多 {_SECRETARY_BATCH_MAX} 条任务，收到 {len(items)} 条；"
            "请拆成多批，或先挑最急的几条"
        )
    total = len(items)
    normalized: List[Dict[str, Any]] = []
    dep_map: Dict[int, List[int]] = {}
    for index, task in enumerate(items):
        if not isinstance(task, dict):
            raise SecretaryDispatchError(
                f"第 {index + 1} 条任务需要是对象（含 prompt / target / depends_on 等字段）"
            )
        prompt_text = str(task.get("prompt") or "").strip()
        if not prompt_text:
            raise SecretaryDispatchError(f"第 {index + 1} 条任务的 prompt（任务正文）不能为空")
        raw_depends = task.get("depends_on")
        dep_indices: List[int] = []
        if raw_depends is not None and raw_depends != "":
            if isinstance(raw_depends, (int, str)):
                raw_depends = [raw_depends]
            if not isinstance(raw_depends, (list, tuple, set, frozenset)):
                raise SecretaryDispatchError(
                    f"第 {index + 1} 条任务的 depends_on 需要是下标数组"
                )
            for raw in raw_depends:
                try:
                    parsed = int(raw)
                except (TypeError, ValueError):
                    raise SecretaryDispatchError(
                        f"第 {index + 1} 条任务的 depends_on 含非整数项：{raw!r}"
                    )
                if parsed < 0 or parsed >= total:
                    raise SecretaryDispatchError(
                        f"第 {index + 1} 条任务的 depends_on 下标 {parsed} 越界"
                        f"（合法范围 0..{total - 1}）"
                    )
                if parsed == index:
                    raise SecretaryDispatchError(
                        f"第 {index + 1} 条任务的 depends_on 不能指向自己"
                    )
                if parsed not in dep_indices:
                    dep_indices.append(parsed)
        dep_map[index] = list(dep_indices)
        normalized.append({
            "title": str(task.get("title") or "").strip(),
            "prompt": prompt_text,
            "target": str(task.get("target") or "").strip(),
            "workdir": str(task.get("workdir") or "").strip(),
            "depends_on": list(dep_indices),
            "model_id": str(task.get("model_id") or "").strip(),
            "permission_mode": str(task.get("permission_mode") or "").strip(),
        })
    cycle = _secretary_batch_cycle_path(dep_map)
    if cycle:
        # 下标在提示里按人类习惯从 1 数起，避免与"依赖数组下标 0"混淆
        path_text = " → ".join(f"#{position + 1}" for position in cycle)
        raise SecretaryDispatchError(
            f"任务依赖成环（循环依赖）：{path_text}；请调整 depends_on 后重试"
        )
    return normalized


class _SecretaryDispatchService:

    def __init__(self, bridge: "ApiBridge") -> None:
        self._bridge = bridge
        # 派活台账（P0）：与记忆库共用同一个连接（DispatchStore 内部全程吞异常，
        # 写失败只记 debug，不影响派活与回合收尾）。取不到 manager 时置 None，
        # 调用处一律判空，台账缺失不得让派活失败。
        try:
            self._store = DispatchStore(bridge._memory)
        except Exception:
            logger.debug("初始化派活台账失败", exc_info=True)
            self._store = None

    def _candidate_sessions(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        """候选会话清单（GUI + 秘书来源）：秘书会话的 workdir 同为 GLOBAL，
        只能靠 source 区分，因此必须一次取回两类来源。"""
        try:
            raw = self._bridge._memory.get_all_sessions(
                workdir=None,
                sources=["GUI", "SECRETARY"],
                include_empty=True,
                include_archived=include_archived,
            )
        except Exception:
            logger.debug("读取会话清单失败", exc_info=True)
            return []
        return [dict(s) for s in (raw or [])]

    def _is_secretary(self, session_name: str) -> bool:
        try:
            if str(self._bridge._memory.get_session_source(session_name) or "") == "SECRETARY":
                return True
        except Exception:
            pass
        try:
            return bool(secretary.is_secretary_session(session_name))
        except Exception:
            return False

    def _display_name(self, session_name: str) -> str:
        name = str(session_name or "")
        if self._is_secretary(name):
            return secretary.get_secretary_name(name) or secretary.FALLBACK_SECRETARY_NAME
        return "新会话" if _SECRETARY_DRAFT_NAME_RE.match(name) else name

    def _session_workdir(self, session_name: str) -> str:
        name = str(session_name or "")
        for item in self._candidate_sessions(include_archived=True):
            if str(item.get("name")) == name:
                return str(item.get("workdir") or "GLOBAL")
        return "GLOBAL"

    def _candidate_view(self, session_name: str) -> Dict[str, str]:
        return {
            "session_name": session_name,
            "display_name": self._display_name(session_name),
            "workdir": self._session_workdir(session_name),
        }

    def _resolve_target(self, target: str) -> tuple:
        raw = str(target or "").strip()
        if not raw:
            return None, []
        names = [str(s.get("name") or "") for s in self._candidate_sessions(include_archived=True)]
        names = [n for n in names if n]
        if raw in names:
            return raw, []
        pairs = [(n, self._display_name(n)) for n in names]
        exact = [n for n, display in pairs if display == raw]
        if len(exact) == 1:
            return exact[0], []
        if len(exact) > 1:
            return None, [self._candidate_view(n) for n in exact]
        fuzzy = [n for n, display in pairs if raw in n or raw in display]
        if len(fuzzy) == 1:
            return fuzzy[0], []
        return None, [self._candidate_view(n) for n in fuzzy]

    def _validate_workdir(self, workdir: str) -> str:
        raw = str(workdir or "").strip()
        if not raw or raw.upper() == "GLOBAL":
            return "GLOBAL"
        path = _resolve_real_path(raw)
        if os.path.isdir(path):
            return path
        hints = self._similar_dirs(raw)
        hint_text = f"；相近目录：{'、'.join(hints)}" if hints else ""
        raise SecretaryDispatchError(f"工作目录不存在或不是目录：{raw}{hint_text}")

    def _similar_dirs(self, raw: str, limit: int = 5) -> List[str]:
        """给非法 workdir 找相近目录候选（同父目录下名字互为子串，兜底取前几个）。"""
        resolved = _resolve_real_path(raw)
        parent = os.path.dirname(resolved) or os.sep
        base = os.path.basename(str(raw).rstrip("\\/")).lower()
        try:
            entries = [
                e for e in os.listdir(parent)
                if os.path.isdir(os.path.join(parent, e))
            ]
        except Exception:
            return []
        hits = [e for e in entries if base and (base in e.lower() or e.lower() in base)]
        if not hits:
            hits = entries
        return [os.path.join(parent, name) for name in hits[:limit]]

    def search_conversations(self, keyword: str = "", date_from: str = "",
                             date_to: str = "", workdir: str = "",
                             limit: int = 20) -> List[Dict[str, Any]]:
        bridge = self._bridge
        kw = str(keyword or "").strip()
        start = _secretary_parse_date(date_from, "date_from")
        end = _secretary_parse_date(date_to, "date_to")
        if start and end and start > end:
            raise SecretaryDispatchError(f"日期区间反了：{start} > {end}")
        if not kw and not start and not end:
            raise SecretaryDispatchError("请至少给出关键词或日期范围，避免把全部会话倾倒出来")
        try:
            size = int(limit or 20)
        except (TypeError, ValueError):
            raise SecretaryDispatchError(f"limit 需要是 1..{_SECRETARY_SEARCH_LIMIT_MAX} 的整数")
        size = max(1, min(size, _SECRETARY_SEARCH_LIMIT_MAX))
        workdir_filter = str(workdir or "").strip()

        merged: Dict[str, Dict[str, Any]] = {}
        if kw:
            for source in ("GUI", "SECRETARY"):
                try:
                    rows = bridge._memory.search_sessions_and_messages(
                        query=kw,
                        scope="all",
                        workdir=workdir_filter or None,
                        source=source,
                        limit=size,
                        include_archived=False,
                    )
                except Exception as e:
                    logger.exception("秘书检索会话失败")
                    raise SecretaryDispatchError(f"检索失败：{e}")
                self._merge_search_rows(merged, rows, start, end)
        else:
            # 只给日期：memory 的检索必须有关键词，这里按会话更新时间过滤
            for item in self._candidate_sessions():
                updated = str(item.get("updated_at") or "")
                if not _secretary_in_date_range(updated, start, end):
                    continue
                if workdir_filter and workdir_filter.upper() != "GLOBAL":
                    from core.memory import _normalize_workdir

                    item_wd = str(item.get("workdir") or "").strip()
                    if not item_wd or _normalize_workdir(item_wd) != _normalize_workdir(workdir_filter):
                        continue
                elif workdir_filter.upper() == "GLOBAL":
                    item_wd = str(item.get("workdir") or "").strip()
                    if item_wd and item_wd.upper() != "GLOBAL":
                        continue
                name = str(item.get("name") or "")
                if not name or name in merged:
                    continue
                merged[name] = self._session_row(
                    name, updated, item.get("msg_count"), [], item.get("workdir")
                )

        rows = sorted(merged.values(), key=lambda r: str(r["updated_at"]), reverse=True)
        return rows[:size]

    def _merge_search_rows(self, merged: Dict[str, Dict[str, Any]], raw_rows,
                           start: str, end: str) -> None:
        for item in raw_rows or []:
            name = str(item.get("session_name") or "")
            if not name:
                continue
            updated = str(item.get("updated_at") or "")
            if not _secretary_in_date_range(updated, start, end):
                continue
            row = merged.get(name)
            if row is None:
                row = self._session_row(
                    name, updated, item.get("msg_count"), [], item.get("workdir")
                )
                merged[name] = row
            role = item.get("role")
            snippet = item.get("snippet")
            if role and snippet and len(row["snippets"]) < _SECRETARY_SNIPPET_MAX:
                row["snippets"].append({
                    "role": str(role),
                    "excerpt": str(snippet),
                    "at": updated,
                })

    def _session_row(self, name: str, updated_at, message_count, snippets,
                     workdir) -> Dict[str, Any]:
        return {
            "session_name": str(name),
            "display_name": self._display_name(name),
            "is_secretary": self._is_secretary(name),
            "workdir": str(workdir or "GLOBAL"),
            "updated_at": str(updated_at or ""),
            "message_count": int(message_count or 0),
            "snippets": snippets,
        }

    def dispatch_task(self, target: str = "", prompt: str = "", workdir: str = "",
                      model_id: str = "", permission_mode: str = "",
                      task_title: str = "") -> Dict[str, Any]:
        plan = self._prepare_dispatch(
            target=target,
            prompt=prompt,
            workdir=workdir,
            model_id=model_id,
            permission_mode=permission_mode,
            task_title=task_title,
        )
        if plan.get("ok") is False:
            return plan
        state, queued_entry, dispatch_context = self._launch_dispatch(plan)

        # 落台账（P0）：已启动或已入队之后才写，保证状态与真实投递一致。台账任何
        # 异常都不得让派活失败——store 缺失 / create 返回 None 时 dispatch_id 传空，
        # notice 退化成不带任务编号的旧文案，其余流程照旧。dispatch_context 是同一
        # 个 dict 对象（worker 收尾时才读），此处补写两个键的时序是安全的。
        task_title_text = plan["task_title"]
        dispatch_id = None
        try:
            store = getattr(self, "_store", None)
            if store is not None:
                dispatch_id = store.create(
                    secretary_session=plan["notify_session"],
                    target_session=plan["target_session"],
                    target_display=plan["display_name"],
                    workdir=plan["workdir"],
                    title=task_title_text,
                    prompt=plan["prompt"],
                    status=STATUS_QUEUED if state == STATUS_QUEUED else STATUS_RUNNING,
                )
        except Exception:
            logger.debug("派活台账落库失败", exc_info=True)
            dispatch_id = None
        self._bind_ledger_to_dispatch(
            dispatch_context, queued_entry, dispatch_id, task_title_text
        )
        return {
            "ok": True,
            "target_session": plan["target_session"],
            "display_name": plan["display_name"],
            "workdir": plan["workdir"],
            "state": state,
            "dispatch_id": dispatch_id,
            "task_title": task_title_text,
        }

    def _prepare_dispatch(self, target: str = "", prompt: str = "", workdir: str = "",
                          model_id: str = "", permission_mode: str = "",
                          task_title: str = "") -> Dict[str, Any]:
        bridge = self._bridge
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            raise SecretaryDispatchError("任务内容不能为空")
        raw_target = str(target or "").strip()

        # 指定模型必须真实存在（与 send_user_message 的 known_ids 防线同源）；
        # 配置读取失败时放行，交由 worker 侧回退全局默认模型
        clean_model_id = str(model_id or "").strip()
        if clean_model_id:
            known_ids = set()
            try:
                known_ids = {
                    str(m.get("id"))
                    for p in (load_config().get("providers") or [])
                    for m in (p.get("models") or [])
                }
            except Exception:
                known_ids = set()
            if known_ids and clean_model_id not in known_ids:
                raise SecretaryDispatchError(f"模型不存在或已被删除：{clean_model_id}")

        # 会话级权限档位：未知值直接拒绝（宁可不派也不误放权）
        raw_permission = str(permission_mode or "").strip()
        pending_permission = ""
        if raw_permission:
            if not app_context.is_known_permission_mode(raw_permission):
                raise SecretaryDispatchError(f"未知权限模式：{raw_permission}")
            pending_permission = app_context.normalize_permission_mode(raw_permission)

        is_new = (not raw_target) or raw_target.lower() in _SECRETARY_NEW_SESSION_MARKS
        if is_new:
            session_name = f"session_{uuid.uuid4().hex[:6]}"
            pending_workdir = self._validate_workdir(workdir)
            display_name = "新会话"
        else:
            session_name, candidates = self._resolve_target(raw_target)
            if session_name is None:
                if candidates:
                    return {
                        "ok": False,
                        "msg": (
                            f"「{raw_target}」命中 {len(candidates)} 个会话，"
                            "请指明具体会话名后再派活"
                        ),
                        "candidates": candidates,
                    }
                raise SecretaryDispatchError(
                    f"没找到会话「{raw_target}」，可以先用 search_conversations 检索"
                )
            pending_workdir = ""
            display_name = self._display_name(session_name)
            if self._is_secretary(session_name):
                raise SecretaryDispatchError(
                    "不能把任务派给秘书会话：秘书会话只读，没有命令执行和写文件工具"
                )
        if pending_permission and not is_new:
            try:
                bridge._memory.set_session_permission_mode(session_name, pending_permission)
            except Exception:
                logger.debug("派活绑定额度档位失败", exc_info=True)

        # 完成通知与自动唤醒按"发起该派活的执行会话"优先路由，其次回落 UI 当前会话：
        # 避免用户在秘书运行途中切走视图后完成结果错路由或唤醒失败
        notify_session = (
            str(app_context.get_thread_session() or "").strip()
            or str(getattr(bridge, "_current_session", "") or "").strip()
        )
        resolved_workdir = pending_workdir or self._session_workdir(session_name)
        return {
            "ok": True,
            "target_session": session_name,
            "display_name": display_name,
            "workdir": resolved_workdir,
            "pending_workdir": pending_workdir,
            "is_new": is_new,
            "pending_permission": pending_permission,
            "notify_session": notify_session,
            "delivery_text": self._delivery_text(prompt_text, resolved_workdir),
            "prompt": prompt_text,
            "task_title": str(task_title or "").strip() or prompt_text[:40],
            "model_id": clean_model_id,
        }

    def _delivery_text(self, prompt_text: str, workdir: str) -> str:
        try:
            injection = project_profile.build_injection(workdir)
            if injection:
                return (
                    f"{_SECRETARY_PROFILE_HEADER}\n{injection}\n\n"
                    f"{_SECRETARY_TASK_HEADER}\n{prompt_text}"
                )
        except Exception:
            logger.debug("派活注入项目画像失败", exc_info=True)
            return prompt_text
        return prompt_text

    def _presumed_status(self, session_name: str) -> str:
        """建台账行时预判的派发状态：目标忙 → queued，否则 running。

        真实状态由 ``_launch_dispatch`` 在 _turn_start_lock 内给出，两者不一致时
        由 ``_reconcile_ledger_status`` 纠正（正常情况下二者必然一致）。
        """
        try:
            return STATUS_QUEUED if self._bridge._is_turn_active(session_name) else STATUS_RUNNING
        except Exception:
            return STATUS_RUNNING

    def _pending_workdir_for(self, session_name: str, workdir: str) -> str:
        """待建会话的懒创建工作目录：目标会话尚未落库时返回该目录，否则空串。

        批量派活里"派给新会话的活"会先落 blocked，解锁时那个 session_xxxxxx 还
        不存在，必须把 workdir 透传给 worker 才能绑到正确项目；已存在的会话一律
        以自身归属为准（与 ``_automation_pending_workdir`` 同口径）。
        """
        raw = str(workdir or "").strip()
        if not raw:
            return ""
        try:
            for item in self._candidate_sessions(include_archived=True):
                if str(item.get("name")) == session_name:
                    return ""
        except Exception:
            logger.debug("查询派活目标会话归属失败", exc_info=True)
            return ""
        return raw

    def _bind_ledger_to_dispatch(self, dispatch_context, queued_entry,
                                 dispatch_id, task_title) -> None:
        """把台账 id / 标题绑到回合上下文与排队条目上。

        收尾回写（``_finalize_secretary_dispatch``）靠 dispatch_context 里的
        dispatch_id，排队补发（``_drain_message_queue``）靠排队条目里的同名键；
        两处必须同时写，否则排队派活跑完不会回写台账。
        """
        if isinstance(dispatch_context, dict):
            dispatch_context["dispatch_id"] = dispatch_id
            dispatch_context["task_title"] = task_title
        if isinstance(queued_entry, dict):
            queued_entry["secretary_dispatch_id"] = dispatch_id
            queued_entry["secretary_dispatch_task_title"] = task_title

    def _reconcile_ledger_status(self, store, dispatch_id, presumed: str, actual: str) -> None:
        """实际派发状态与建行时的预判不一致时纠正台账。

        只在新行仍处于预判状态时才改：回合可能在本方法返回前就跑完并写进终态，
        那种情况下绝不能把它改回 running（宁可留个中间态也不覆盖已出的结论）。
        """
        if presumed == actual:
            return
        try:
            row = store.get(dispatch_id)
            if not isinstance(row, dict):
                return
            if str(row.get("status") or "") != presumed:
                return
            store.mark_status(dispatch_id, actual)
        except Exception:
            logger.debug("派活台账状态纠正失败", exc_info=True)

    def _launch_dispatch(self, plan, *, dispatch_id=None, task_title="") -> tuple:
        bridge = self._bridge
        session_name = plan["target_session"]
        display_name = plan["display_name"]
        pending_workdir = str(plan.get("pending_workdir") or "")
        queued_permission = plan["pending_permission"] if plan["is_new"] else ""
        dispatch_context = {
            "display_name": display_name,
            "notify_session": plan["notify_session"],
            "dispatch_id": dispatch_id,
            "task_title": task_title,
        }
        queued_entry: Optional[Dict[str, Any]] = None
        with bridge._turn_start_lock:
            if bridge._is_turn_active(session_name):
                queued_entry = {
                    "id": f"q_{uuid.uuid4().hex[:8]}",
                    "text": plan["delivery_text"],
                    "attachments": [],
                    "queued_at": time.time(),
                    "turn_source": "local",
                    "pending_workdir": pending_workdir,
                    "secretary_model_id": plan["model_id"],
                    "secretary_permission_mode": queued_permission,
                    "secretary_dispatch_display_name": display_name,
                    "secretary_dispatch_notify_session": plan["notify_session"],
                }
                bridge._message_queues.setdefault(session_name, []).append(queued_entry)
                bridge._emit_message_queue_snapshot(session_name)
                state = STATUS_QUEUED
            else:
                try:
                    bridge._spawn_turn_worker(
                        plan["delivery_text"],
                        session_name,
                        pending_workdir=pending_workdir,
                        turn_source="local",
                        pending_model_id=plan["model_id"],
                        pending_permission_mode=queued_permission,
                        secretary_dispatch_context=dispatch_context,
                    )
                except Exception as e:
                    logger.exception("秘书派活启动回合失败")
                    raise SecretaryDispatchError(f"派活失败：{e}")
                state = STATUS_RUNNING
        return state, queued_entry, dispatch_context

    def dispatch_batch(self, tasks=None) -> Dict[str, Any]:
        entries = _secretary_parse_batch_tasks(tasks)
        store = self._ledger_store()
        batch_id = f"batch_{uuid.uuid4().hex[:8]}"

        plans: List[Dict[str, Any]] = []
        for index, entry in enumerate(entries):
            try:
                plan = self._prepare_dispatch(
                    target=entry["target"],
                    prompt=entry["prompt"],
                    workdir=entry["workdir"],
                    model_id=entry.get("model_id", ""),
                    permission_mode=entry.get("permission_mode", ""),
                    task_title=entry["title"],
                )
            except SecretaryDispatchError as exc:
                raise SecretaryDispatchError(f"第 {index + 1} 条任务无法派发：{exc}")
            if plan.get("ok") is False:
                candidates = plan.get("candidates") or []
                names = "、".join(str(c.get("session_name") or "") for c in candidates)
                raise SecretaryDispatchError(
                    f"第 {index + 1} 条任务的目标「{entry['target']}」命中多个会话"
                    f"（{names}），请指明具体会话后再批量派活"
                )
            plans.append(plan)

        index_to_id: Dict[int, int] = {}
        presumed: Dict[int, str] = {}
        for index, entry in enumerate(entries):
            plan = plans[index]
            status = (
                STATUS_BLOCKED if entry["depends_on"]
                else self._presumed_status(plan["target_session"])
            )
            dispatch_id = store.create(
                secretary_session=plan["notify_session"],
                target_session=plan["target_session"],
                target_display=plan["display_name"],
                workdir=plan["workdir"],
                title=plan["task_title"],
                prompt=plan["prompt"],
                status=status,
                batch_id=batch_id,
            )
            if dispatch_id is None:
                raise SecretaryDispatchError(
                    f"派活台账写入失败（第 {index + 1} 条任务），本批已中止，未派发任何任务"
                )
            index_to_id[index] = dispatch_id
            presumed[index] = status

        for index, entry in enumerate(entries):
            if not entry["depends_on"]:
                continue
            store.set_depends_on(
                index_to_id[index],
                [index_to_id[dep_index] for dep_index in entry["depends_on"]],
            )

        results: List[Dict[str, Any]] = []
        for index, entry in enumerate(entries):
            dispatch_id = index_to_id[index]
            plan = plans[index]
            upstream_ids = [index_to_id[dep_index] for dep_index in entry["depends_on"]]
            if upstream_ids:
                results.append({
                    "index": index,
                    "dispatch_id": dispatch_id,
                    "state": STATUS_BLOCKED,
                    "depends_on": upstream_ids,
                })
                continue
            try:
                state, queued_entry, dispatch_context = self._launch_dispatch(
                    plan, dispatch_id=dispatch_id, task_title=plan["task_title"]
                )
            except Exception as exc:
                logger.debug("批量派活第 %s 条启动失败", index + 1, exc_info=True)
                store.mark_status(dispatch_id, STATUS_FAILED, error=str(exc))
                try:
                    self._bridge._advance_dependents(dispatch_id)
                except Exception:
                    logger.debug("批量派活失败项的下游阻断失败", exc_info=True)
                results.append({
                    "index": index,
                    "dispatch_id": dispatch_id,
                    "state": STATUS_FAILED,
                    "error": str(exc),
                })
                continue
            self._bind_ledger_to_dispatch(
                dispatch_context, queued_entry, dispatch_id, plan["task_title"]
            )
            self._reconcile_ledger_status(store, dispatch_id, presumed[index], state)
            results.append({"index": index, "dispatch_id": dispatch_id, "state": state})
        return {"ok": True, "batch_id": batch_id, "items": results}

    def resume_dispatch(self, dispatch_id: int = 0) -> Dict[str, Any]:
        store = self._ledger_store()
        try:
            parsed_id = int(dispatch_id)
        except (TypeError, ValueError):
            raise SecretaryDispatchError(f"dispatch_id 需要是整数，收到：{dispatch_id!r}")
        row = store.get(parsed_id)
        if not isinstance(row, dict):
            raise SecretaryDispatchError(f"没有找到任务 #{parsed_id}")
        current = str(row.get("status") or "")
        if current != STATUS_BLOCKED:
            # 幂等护栏：两个上游同时完成时可能并发推进到同一条下游，
            # 只有仍处于 blocked 的记录才需要（且允许）被重新启动
            raise SecretaryDispatchError(
                f"任务 #{parsed_id} 当前状态为 {current or '未知'}，不需要重新启动"
            )
        session_name = str(row.get("target_session") or "").strip()
        prompt_text = str(row.get("prompt") or "").strip()
        if not session_name or not prompt_text:
            raise SecretaryDispatchError(
                f"任务 #{parsed_id} 的台账缺少目标会话或任务正文，无法自动启动"
            )
        workdir = str(row.get("workdir") or "").strip()
        plan = {
            "target_session": session_name,
            "display_name": str(row.get("target_display") or "") or session_name,
            "workdir": workdir,
            "pending_workdir": self._pending_workdir_for(session_name, workdir),
            "is_new": False,
            "pending_permission": "",
            "notify_session": str(row.get("secretary_session") or ""),
            "delivery_text": self._delivery_text(prompt_text, workdir),
            "prompt": prompt_text,
            "model_id": "",
            "task_title": str(row.get("title") or ""),
        }
        state, queued_entry, dispatch_context = self._launch_dispatch(
            plan, dispatch_id=parsed_id, task_title=plan["task_title"]
        )
        self._bind_ledger_to_dispatch(
            dispatch_context, queued_entry, parsed_id, plan["task_title"]
        )
        self._reconcile_ledger_status(store, parsed_id, STATUS_BLOCKED, state)
        return {
            "ok": True,
            "dispatch_id": parsed_id,
            "state": state,
            "target_session": session_name,
        }

    def create_project(self, path: str = "") -> Dict[str, Any]:
        raw = str(path or "").strip()
        if not raw:
            raise SecretaryDispatchError("项目路径不能为空")
        if raw.upper() in {"GLOBAL", "SECRETARY"}:
            raise SecretaryDispatchError(f"「{raw}」是保留分组标识，不是磁盘目录")
        if not os.path.isabs(raw):
            raise SecretaryDispatchError(
                f"只接受完整绝对路径（如 D:{os.sep}work{os.sep}demo）：{raw}"
            )
        target = _resolve_real_path(raw)
        if os.path.exists(target) and not os.path.isdir(target):
            raise SecretaryDispatchError(f"同名文件已存在，无法作为项目目录：{target}")
        created = not os.path.exists(target)
        try:
            os.makedirs(target, exist_ok=True)
        except Exception as exc:
            raise SecretaryDispatchError(f"创建目录失败：{target}（{exc}）")
        bridge = self._bridge
        bridge._memory.add_workspace(target)
        try:
            bridge.emit_event("session_moved", {"sessions": bridge.get_sessions()})
        except Exception:
            logger.debug("新建项目后刷新侧栏失败", exc_info=True)
        return {
            "ok": True,
            "workdir": target,
            "created": created,
            "display_name": os.path.basename(target.rstrip("\\/")) or target,
        }

    def open_session(self, target: str = "") -> Dict[str, Any]:
        bridge = self._bridge
        raw_target = str(target or "").strip()
        if not raw_target:
            raise SecretaryDispatchError("请给出要打开的会话名或关键词")
        session_name, candidates = self._resolve_target(raw_target)
        if session_name is None:
            if candidates:
                return {
                    "ok": False,
                    "msg": (
                        f"「{raw_target}」命中 {len(candidates)} 个会话，"
                        "请指明具体会话名后再跳转"
                    ),
                    "candidates": candidates,
                }
            raise SecretaryDispatchError(
                f"没找到会话「{raw_target}」，可以先用 search_conversations 检索"
            )
        display_name = self._display_name(session_name)
        workdir = self._session_workdir(session_name)

        try:
            archived_at = bridge._session_archived_at(session_name)
        except Exception:
            archived_at = None
        if archived_at:
            try:
                result = bridge.unarchive_session(session_name)
            except Exception as e:
                logger.exception("秘书跳转前取消归档失败")
                raise SecretaryDispatchError(f"取消归档失败：{e}")
            if not (isinstance(result, dict) and result.get("ok")):
                reason = result.get("msg") if isinstance(result, dict) else result
                raise SecretaryDispatchError(f"取消归档失败：{reason}")

        bridge._set_current_session(session_name)
        bridge.emit_event("open_session", {
            "session_name": session_name,
            "display_name": display_name,
            "workdir": workdir,
        })
        return {
            "ok": True,
            "session_name": session_name,
            "display_name": display_name,
            "workdir": workdir,
        }

    # ------------------------------------------------------------------
    # 能力五：派活台账查询（升级设计 2026-11-04 第 3.4 节）
    # 只读：直接落到 DispatchStore（与写台账共用同一个连接），不碰回合通道。
    # ------------------------------------------------------------------
    def _ledger_store(self):
        store = getattr(self, "_store", None)
        if store is None:
            raise SecretaryDispatchError(
                "派活台账不可用（记忆库尚未就绪），暂时查不到任务记录"
            )
        return store

    def list_dispatches(self, status: str = "", workdir: str = "",
                        target: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        store = self._ledger_store()
        try:
            size = int(limit or 20)
        except (TypeError, ValueError):
            raise SecretaryDispatchError(
                f"limit 需要是 1..{_SECRETARY_DISPATCH_LIMIT_MAX} 的整数"
            )
        size = max(1, min(size, _SECRETARY_DISPATCH_LIMIT_MAX))
        rows = store.list(
            status=str(status or "").strip(),
            workdir=str(workdir or "").strip(),
            target_like=str(target or "").strip(),
            limit=size,
        )
        out: List[Dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            out.append({
                "id": row.get("id"),
                "title": str(row.get("title") or ""),
                "target_display": str(row.get("target_display") or ""),
                "workdir": str(row.get("workdir") or ""),
                "status": str(row.get("status") or ""),
                "started_at": str(row.get("started_at") or ""),
                "finished_at": str(row.get("finished_at") or ""),
                "result_excerpt": str(row.get("result_excerpt") or "")[
                    :_SECRETARY_DISPATCH_EXCERPT_CHARS
                ],
            })
            if len(out) >= size:
                break
        return out

    def get_dispatch_result(self, dispatch_id: int = 0) -> Optional[Dict[str, Any]]:
        """按 id 取一条派活的完整详情（prompt 与摘要都不截断）；不存在返回 None。"""
        store = self._ledger_store()
        try:
            parsed_id = int(dispatch_id)
        except (TypeError, ValueError):
            raise SecretaryDispatchError(
                f"dispatch_id 需要是整数，收到：{dispatch_id!r}"
            )
        row = store.get(parsed_id)
        if not isinstance(row, dict):
            return None
        return dict(row)


class ApiBridge(GitBridgeMixin, BrowserBridgeMixin, SnipBridgeMixin, PetBridgeMixin):
    _active_turn_lock = threading.Lock()
    _active_turn_snapshots: dict[str, dict] = {}
    _session_last_errors: dict[str, dict] = {}
    _session_last_errors_lock = threading.Lock()

    def __init__(self, mcp_manager=None):
        # 内部状态变量使用下划线开头，防止 PyWebView 在 Windows .NET 检查中产生属性递归问题
        self._window = None
        self._dropped_paths: list = []
        self._git_svc = None
        self._git_monitor = None
        self._git_subs = {}
        self._git_versions = {}
        self._git_serialized = {}
        self._memory = MemoryManager(db_path=GUI_DB_FILE)
        self._mcp = mcp_manager if mcp_manager is not None else _mcp_manager_singleton

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

        latest = self._find_latest_session()
        self._current_session = latest or f"session_{uuid.uuid4().hex[:6]}"
        if not latest:
            try:
                self._memory.ensure_session(self._current_session, workdir="GLOBAL", source="GUI")
            except Exception:
                logger.warning("初始化 GUI 启动会话失败", exc_info=True)

        self._current_permission_mode = app_context.normalize_permission_mode(MY_PERMISSION_MODE)
        self._task_status = "idle"
        self._active_turns: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_turn_snapshots: dict[str, dict] = {}
        self._active_turn_lock = threading.Lock()
        # 每会话当前回合的 turn_id（session:xxx 复合 ID）：权限请求的 owner
        # 是 turn_id 而非会话名，stop_current_turn 要按它精确取消权限等待
        self._active_turn_ids: dict[str, str] = {}
        # 回合无进展看门狗的活动时钟：事件循环与子代理轮询每推进一步就刷新一次；
        # 超过阈值仍未刷新说明回合线程卡在不可取消的同步调用上（控制台 input、
        # 外部锁、原生库），由看门狗强制收尾，避免会话永久显示运行中。
        self._turn_activity_lock = threading.Lock()
        self._turn_activity: dict[str, float] = {}
        self._turn_owner_sessions: dict[str, str] = {}
        self._turn_owner_lock = threading.Lock()
        # 回合启动互斥：send_user_message 与后台自动汇总都可能启动 turn 线程，
        # "检查 is_alive + 启动"必须原子，否则两边同时通过检查会并发跑两个回合。
        # RLock 仅为纵深防御（跨线程互斥语义与 Lock 一致）：临界区内调用的
        # 改名随迁已通过 holds_turn_start_lock 显式跳过重入取锁，不依赖此重入性
        self._turn_start_lock = threading.RLock()
        self._message_queues: dict[str, list[dict]] = {}
        # 已弃用：多会话并行改为线程本地 workdir 上下文（core.workspace_context），
        # 不再需要进程级串行锁。保留变量定义避免历史引用报错。
        self._turn_workdir_lock = threading.Lock()
        self._bg_summary_triggered: set = set()
        self._bg_summary_lock = threading.Lock()
        self._bg_summary_pending_sessions: set = set()
        self._secretary_summary_lock = threading.Lock()
        self._secretary_summary_pending_sessions: set = set()
        # 用户主动点过“终止”的会话：抑制后台任务终态的自动汇总回合，
        # 防止被取消的任务被“接手完成”机制复活；用户发新消息时解除
        self._user_cancelled_sessions: set = set()
        self._goal_runs: dict[str, threading.Event] = {}
        # 会话 AI 总结命名：已派发线程的会话去重，防止同一会话并发起两个
        # 命名请求（首轮结束与手动改名/重复收尾的竞态）
        self._pending_title_sessions: set = set()
        self._pending_title_lock = threading.Lock()

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

        self._pty_session = None
        self._pty_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutting_down = False
        self._lan_server = None
        # 保护 _lan_server 惰性创建的双检锁：并发（启动后台线程 + 用户点二维码）时不得创建两个实例
        self._lan_lock = threading.Lock()
        self._mcp_server = None
        self._mcp_server_lock = threading.Lock()
        self._feishu_service = None
        self._feishu_lock = threading.Lock()
        self._cloud_relay_service = None
        self._cloud_relay_lock = threading.Lock()
        self._cloud_relay_cfg_mtime_ns = None
        self._cloud_relay_cfg_sig = None
        self._cloud_relay_watcher_active = False
        # 电脑操控事件桥：controller listener 注册标记（进程内建立一次、幂等，
        # 见 _ensure_computer_event_bridge；set_window 与各控制方法都会触发）
        self._computer_event_listener = None
        self._skills_cache: Optional[List[Dict[str, Any]]] = None
        self._skills_scan_lock = threading.Lock()

        self._js_queue: "queue.Queue" = queue.Queue(maxsize=JS_QUEUE_MAXSIZE)
        self._js_dispatcher: Optional[threading.Thread] = None
        self._js_dispatcher_lock = threading.Lock()
        self._js_dropped = 0
        self._reapply_thread: Optional[threading.Thread] = None
        self._native_titlebar_color_capability = _DWM_COLOR_CAPABILITY_PENDING

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
        try:
            from core.llm import set_token_usage_listener
            set_token_usage_listener(self._emit_token_usage)
        except Exception:
            logger.warning("注册 token 用量实时推送监听器失败", exc_info=True)
        try:
            from core.mcp_client import set_status_logger
            set_status_logger(lambda message: logger.info("%s", message))
        except Exception:
            pass

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

        from core.automation.engine import AutomationEngine
        from core.automation.storage import AutomationStorage
        self._automation_storage = AutomationStorage(self._memory)
        self._automation_storage.on_run_updated = self._on_automation_run_updated
        self._automation_engine = AutomationEngine(
            self._automation_storage, self._dispatch_automation_run
        )
        # 星流工作流运行台账：与自动化存储同库同模式；工具层经
        # core.workflows.runs.get_run_storage() 取进程级句柄（未注入时降级）
        from core.workflows.runs import init_run_storage, set_cancel_provider, set_event_sink
        self._workflow_run_storage = init_run_storage(self._memory)
        # 角色库（P2）：同库同模式的新增表（characters / character_refs /
        # character_generation_records），工具层经 get_character_storage()
        # 取进程级句柄（未注入的 CLI/headless 场景自动降级为不可用提示）
        from core.character_store import init_character_storage

        self._character_storage = init_character_storage(self._memory)
        set_event_sink(self._on_workflow_run_event)
        set_cancel_provider(self._workflow_cancel_event)

        self._register_automation_service()
        self._register_secretary_dispatch_service()
        self._register_decision_audit_listener()
        self._automation_turn_labels: Dict[str, Dict[str, str]] = {}

        threading.Thread(
            target=cleanup_old_installers,
            daemon=True,
            name="gui-update-cleaner",
        ).start()

        # 桌宠自动恢复（gui_pet_mixin.PetBridgeMixin）：延迟 2.5s 在后台线程按
        # pet_enabled 开关拉起桌宠。延迟是为了让窗口首帧先出来；定时器 daemon 化 +
        # 回调内部全量 try/except，桌宠起不来或多等一会儿都不能阻塞 __init__、更不能
        # 让 GUI 启动失败。
        try:
            _pet_restore_timer = threading.Timer(2.5, self._pet_auto_restore)
            _pet_restore_timer.daemon = True
            _pet_restore_timer.start()
        except Exception:
            logger.warning("桌宠延迟自动恢复定时器创建失败（已忽略）", exc_info=True)

    def _on_workflow_run_event(self, run_id: str, kind: str, payload: dict) -> None:
        try:
            self.emit_event(
                "workflow_run_progress",
                {"run_id": run_id, "kind": kind, "payload": payload},
            )
        except Exception:
            logger.debug("工作流运行事件推送失败（run=%s kind=%s）", run_id, kind, exc_info=True)

    def _workflow_cancel_event(self):
        target = self._current_session
        evt = self._cancel_events.get(target)
        if evt is not None and evt.is_set():
            return evt
        return None

    def set_window(self, window):
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
        return any(t.is_alive() for t in list(self._active_turns.values()))

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

    def _send_sys_command(self, sc_code: int) -> bool:
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
                    u.SendMessageW(hwnd, 0x0112, 0xF012, 0)
                    # 移动循环已结束（用户松手）：光标贴边 → 模拟系统 Snap 快捷键。
                    # 多显示器坐标不能用主屏宽度直接比较（见 _cursor_snap_vk），
                    # 统一交给该辅助函数按光标所在显示器边界判定。
                    vk = _cursor_snap_vk()
                    if vk:
                        KEYUP = 0x0002
                        u.keybd_event(0x5B, 0, 0, 0)
                        u.keybd_event(vk, 0, 0, 0)
                        u.keybd_event(vk, 0, KEYUP, 0)
                        u.keybd_event(0x5B, 0, KEYUP, 0)
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
            if self._send_sys_command(0xF020):
                return {"ok": True}
            if self._window is not None:
                self._window.minimize()
            return {"ok": True}
        except Exception as error:  # noqa: BLE001
            logger.warning("窗口最小化失败: %s", error)
            return {"ok": False, "error": str(error)}

    def window_toggle_maximize(self) -> Dict[str, Any]:
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
                if not self._send_sys_command(0xF120):
                    self._window.restore()
                self._frameless_maximized = False
            else:
                if not self._send_sys_command(0xF030):
                    self._window.maximize()
                self._frameless_maximized = True

            self.emit_event("window_maximize_state", {"maximized": self._frameless_maximized})
            return {"ok": True, "maximized": self._frameless_maximized}
        except Exception as error:  # noqa: BLE001
            logger.warning("窗口最大化切换失败: %s", error)
            return {"ok": False, "error": str(error)}

    def window_native_maximize(self) -> Dict[str, Any]:
        return self.window_toggle_maximize()

    def window_close(self) -> Dict[str, Any]:
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

        # 秘书调度服务句柄同步注销：退出后工具层 get() 返回 None，避免悬挂引用
        # 指向已停摆的桥；同样包 try/except，不中断后续收尾链
        try:
            from core import secretary_dispatch

            secretary_dispatch.unregister()
        except Exception:
            logger.exception("退出时注销秘书调度服务句柄失败")

        for event in list(self._cancel_events.values()):
            event.set()
        with self._bg_summary_lock:
            self._bg_summary_pending_sessions.clear()
        sec_lock, sec_pending = self._get_secretary_summary_state()
        with sec_lock:
            sec_pending.clear()
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
            if getattr(self, "_mcp_server", None) is not None:
                self._mcp_server.stop()
        except Exception:
            logger.exception("退出时停止局域网 MCP 服务失败")

        try:
            if getattr(self, "_feishu_service", None) is not None:
                self._feishu_service.stop()
        except Exception:
            logger.exception("退出时停止飞书通道服务失败")

        try:
            if getattr(self, "_cloud_relay_service", None) is not None:
                self._cloud_relay_service.stop()
        except Exception:
            logger.exception("退出时停止云中转失败")

        if self._mcp is not None:
            try:
                self._mcp.close()
            except Exception:
                logger.exception("退出时关闭 MCP 失败")
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
        if self._is_frameless():
            self._schedule_frameless_reinstall()

    def _schedule_frameless_reinstall(self):
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
        if sys.platform != "win32":
            return
        try:
            title = getattr(self._window, "title", "") or ""
            frameless = bool(getattr(self._window, "frameless", False))
            for _ in range(300):
                hwnd = self._find_own_hwnd(title)
                if hwnd:
                    self._hwnd = hwnd
                    # 先设图标：WM_SETICON 触发 DWM 重建非客户区
                    self._apply_window_icon()
                    if frameless:
                        self._enable_frameless_resize()
                        # 统一走标题栏应用入口：撤销 pywebview 延伸进客户区的
                        # 1px frame，并隐藏 WS_THICKFRAME 的 DWM 外框。
                        self._apply_titlebar_theme()
                    else:
                        time.sleep(0.05)
                        self._apply_titlebar_theme()
                        self._start_reapply_thread(name="gui-titlebar-reapply")
                    return
                time.sleep(0.2)
        except Exception:
            logger.debug("查找窗口句柄失败", exc_info=True)

    def _enable_frameless_resize(self):
        try:
            import webview.platforms.winforms as wf

            from core.frameless import enable_native_window_behaviors

            import time as _time

            uid = getattr(self._window, "uid", "master")
            for _ in range(60):
                form = wf.BrowserView.instances.get(uid)
                if form is not None and form.Handle is not None and form.Handle.ToInt32() != 0:
                    self._browser_form = form
                    enable_native_window_behaviors(form)
                    return
                _time.sleep(0.2)
            logger.warning("frameless 原生窗口行为：BrowserForm 未就绪，未启用")
        except Exception:
            logger.warning("frameless 原生窗口行为初始化失败", exc_info=True)

    @staticmethod
    def _find_own_hwnd(title: str) -> int:
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
                    return False
            return True

        u32.EnumWindows(_callback, 0)
        return found[0] if found else 0

    def _reapply_worker(self):
        for delay in (0.5, 3, 7, 15, 30):
            time.sleep(delay)
            try:
                self._apply_titlebar_theme()
            except Exception:
                # DWM / HWND 在 loaded、restored 交界可能短暂不可写；保留后续
                # 重放机会，避免一次瞬态失败终止整条 55.5 秒恢复序列。
                logger.warning("延迟重放 frameless 窗口框架失败", exc_info=True)

    def _apply_window_icon(self):
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

    def _find_ffmpeg(self) -> Optional[str]:
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "vendor" / "ffmpeg" / "ffmpeg.exe")
        candidates.append(Path(__file__).resolve().parent / "vendor" / "ffmpeg" / "ffmpeg.exe")
        candidates.append(Path(os.getcwd()) / "vendor" / "ffmpeg" / "ffmpeg.exe")
        try:
            from core import components

            candidates.append(Path(components.component_dir("ffmpeg")) / "ffmpeg.exe")
        except Exception:
            # 组件模块缺失/未知 id 时只是少一个候选，不能影响后面的 PATH 回退
            logger.warning("定位视频转换组件目录失败，跳过该候选", exc_info=True)
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
        found = shutil.which("ffmpeg")
        if found:
            return found
        for candidate in (
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(os.path.expanduser(os.path.join("~", "scoop", "shims", "ffmpeg.exe"))),
            Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"),
        ):
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
        return None

    @staticmethod
    def _probe_video_meta(ffmpeg: str, source: str) -> Dict[str, Any]:
        meta: Dict[str, Any] = {"duration": None, "width": None, "height": None}
        try:
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", source],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return meta
        text = (proc.stderr or "") + (proc.stdout or "")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if match:
            hours, minutes, seconds = match.groups()
            meta["duration"] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        match = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
        if match:
            meta["width"], meta["height"] = int(match.group(1)), int(match.group(2))
        return meta

    def _background_convert_limits(self) -> Dict[str, int]:
        from core.gui_theme import _MAX_IMAGE_BYTES, _MAX_IMAGE_DIMENSION
        return {"max_bytes": int(_MAX_IMAGE_BYTES), "max_dimension": int(_MAX_IMAGE_DIMENSION)}

    def pick_background_video(self) -> Dict[str, Any]:
        """选择待转换的视频，并返回探测信息 + 预检结论（能否转换、有哪些提醒）。"""
        def operation():
            import webview

            result = self._require_theme_window().create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("视频文件 (*.mp4;*.mov;*.mkv;*.webm;*.avi;*.m4v)",),
            )
            source = self._dialog_path(result)
            if source is None:
                raise ThemeError("dialog_cancelled", "source", "已取消选择视频")
            return {"ok": True, **self.describe_background_video(str(source))}

        return self._run_gui_theme_operation("pick_background_video", operation, "source")

    def describe_background_video(self, source: str) -> Dict[str, Any]:
        """给定视频路径，返回大小/时长/分辨率与"能不能转"的结论。"""
        limits = self._background_convert_limits()
        source_path = Path(source)
        if not source_path.is_file():
            raise ThemeError("invalid_video", "source", "视频文件不存在或不可读")
        if source_path.suffix.lower() not in _BACKGROUND_VIDEO_EXTENSIONS:
            raise ThemeError(
                "unsupported_video",
                "source",
                "只支持 mp4 / mov / mkv / webm / avi / m4v 视频文件",
            )
        size = source_path.stat().st_size
        ffmpeg = self._find_ffmpeg()
        info: Dict[str, Any] = {
            "path": str(source_path),
            "name": source_path.name,
            "bytes": size,
            "ffmpeg_available": bool(ffmpeg),
            "max_bytes": limits["max_bytes"],
            "max_dimension": limits["max_dimension"],
            "duration": None,
            "width": None,
            "height": None,
            "can_convert": True,
            "blockers": [],
            "warnings": [],
        }
        if not ffmpeg:
            info["can_convert"] = False
            # 安装包已不再内置 ffmpeg（改为按需组件），所以文案必须给出「去哪下载」
            # 的可执行路径，而不是让用户自己去找 ffmpeg 安装包
            info["blockers"].append(
                "未找到视频转换组件（ffmpeg）。可在「设置 → 背景图转换」中点击"
                "下载视频转换组件，或自行安装 ffmpeg 并加入 PATH 后重启应用。"
            )
            return info
        info.update(self._probe_video_meta(ffmpeg, str(source_path)))
        if info["duration"] is None:
            info["warnings"].append("无法读取视频时长，转换中将只显示『处理中』而无法给出百分比。")
        else:
            fps_guess = 12
            frames = int(info["duration"] * fps_guess)
            if frames > 600:
                info["warnings"].append(
                    f"视频约 {info['duration']:.1f} 秒；默认 12fps 下乒乓循环将有约 {frames * 2} 帧，"
                    "文件会偏大、内存占用偏高。建议先用视频剪辑工具截取 4~6 秒的循环片段。"
                )
        return info

    def pick_background_output_dir(self) -> Dict[str, Any]:
        """选择转换结果的输出文件夹（默认落在桌面）。"""
        def operation():
            import webview

            try:
                from installer_common import get_desktop_dir
                initial = get_desktop_dir()
            except Exception:
                initial = os.path.expanduser("~")
            result = self._require_theme_window().create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=initial,
            )
            if not result:
                raise ThemeError("dialog_cancelled", "destination", "已取消选择文件夹")
            chosen = Path(str(result[0] if isinstance(result, (list, tuple)) else result))
            if not chosen.is_dir():
                raise ThemeError("invalid_directory", "destination", "选择的路径不是文件夹")
            return {"ok": True, "path": str(chosen)}

        return self._run_gui_theme_operation("pick_background_output_dir", operation, "destination")

    def start_background_convert(
        self,
        source: str,
        output_dir: str,
        fps: int = 12,
        quality: int = 78,
        pingpong: bool = True,
    ) -> Dict[str, Any]:
        """后台线程执行 ffmpeg 转换，进度经 bg_convert_progress 事件实时推送。"""
        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            return {"ok": False, "msg": "未找到视频转换组件（ffmpeg），请先在设置中下载组件"}
        if getattr(self, "_bg_convert_running", False):
            return {"ok": False, "msg": "已有转换任务在进行中"}
        source_path = Path(source)
        out_dir = Path(output_dir)
        if not source_path.is_file():
            return {"ok": False, "msg": "源视频不存在"}
        if not out_dir.is_dir():
            return {"ok": False, "msg": "输出文件夹不存在"}
        fps = max(1, min(30, int(fps)))
        quality = max(0, min(100, int(quality)))
        meta = self._probe_video_meta(ffmpeg, str(source_path))
        output_path = out_dir / (source_path.stem + "-动态背景.webp")

        self._bg_convert_running = True
        self._bg_convert_proc = None
        thread = threading.Thread(
            target=self._background_convert_worker,
            args=(ffmpeg, str(source_path), str(output_path), fps, quality, bool(pingpong), meta),
            name="bg-convert", daemon=True,
        )
        thread.start()
        return {"ok": True, "output": str(output_path)}

    def cancel_background_convert(self) -> Dict[str, Any]:
        """中止正在进行的转换（终止 ffmpeg 子进程并删除半成品）。"""
        proc = getattr(self, "_bg_convert_proc", None)
        self._bg_convert_cancelled = True
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            return {"ok": True, "msg": "已请求中止"}
        return {"ok": True, "msg": "当前没有转换任务"}

    def _background_convert_worker(self, ffmpeg, source, output_path, fps, quality, pingpong, meta):
        limits = self._background_convert_limits()
        self._bg_convert_cancelled = False
        duration = meta.get("duration")
        width = min(int(meta.get("width") or 1920), 1920)
        total_frames = int(duration * fps) if duration else None
        temp_dir = tempfile.mkdtemp(prefix="ycode-bgconv-")
        tail: list = []
        try:
            self.emit_event("bg_convert_progress", {"percent": 0, "stage": "extracting"})
            extract_cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", source,
                "-vf", f"fps={fps},scale={width}:-2:flags=lanczos",
                "-progress", "pipe:1", "-nostats",
                os.path.join(temp_dir, "f-%05d.png"),
            ]
            proc = subprocess.Popen(
                extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            self._bg_convert_proc = proc
            last_percent = -1
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if line.startswith("frame="):
                    try:
                        done = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    if total_frames:
                        percent = max(0, min(80, int(done / total_frames * 80)))
                        if percent != last_percent:
                            last_percent = percent
                            self.emit_event("bg_convert_progress", {
                                "percent": percent, "stage": "extracting",
                                "frames_done": done, "frames_total": total_frames,
                            })
                elif line:
                    tail.append(line)
                    if len(tail) > 60:
                        tail.pop(0)
            returncode = proc.wait()
            self._bg_convert_proc = None
            if self._bg_convert_cancelled:
                self.emit_event("bg_convert_error", {"message": "已中止转换"})
                return
            if returncode != 0:
                reason = tail[-1] if tail else f"ffmpeg 退出码 {returncode}"
                self.emit_event("bg_convert_error", {"message": f"抽取帧失败：{reason}"})
                return

            frames = sorted(glob.glob(os.path.join(temp_dir, "f-*.png")))
            if not frames:
                self.emit_event("bg_convert_error", {"message": "未能从该视频中抽取到任何画面帧"})
                return

            ordered = frames + frames[-2:0:-1] if pingpong else frames
            self.emit_event("bg_convert_progress", {"percent": 82, "stage": "assembling"})
            for index, frame_path in enumerate(ordered, start=1):
                shutil.copyfile(frame_path, os.path.join(temp_dir, f"s-{index:05d}.png"))
                if self._bg_convert_cancelled:
                    self.emit_event("bg_convert_error", {"message": "已中止转换"})
                    return
            self.emit_event("bg_convert_progress", {"percent": 88, "stage": "encoding"})

            assemble_cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-framerate", str(fps),
                "-i", os.path.join(temp_dir, "s-%05d.png"),
                "-c:v", "libwebp_anim", "-lossless", "0", "-q:v", str(quality),
                "-loop", "0", "-compression_level", "4", "-an",
                output_path,
            ]
            proc = subprocess.Popen(
                assemble_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            self._bg_convert_proc = proc
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if line:
                    tail.append(line)
                    if len(tail) > 60:
                        tail.pop(0)
            returncode = proc.wait()
            self._bg_convert_proc = None
            if self._bg_convert_cancelled:
                self._safe_unlink(output_path)
                self.emit_event("bg_convert_error", {"message": "已中止转换"})
                return
            if returncode != 0 or not os.path.exists(output_path):
                reason = tail[-1] if tail else f"ffmpeg 退出码 {returncode}"
                # 合成失败时清掉可能已写出的半个容器，避免用户拿到不可用的 webp
                self._safe_unlink(output_path)
                self.emit_event("bg_convert_error", {"message": f"合成动画失败：{reason}"})
                return

            size = os.path.getsize(output_path)
            if size > limits["max_bytes"]:
                self._safe_unlink(output_path)
                self.emit_event("bg_convert_error", {
                    "message": (
                        f"转换结果 {size / 1048576:.2f} MiB，超过背景图上限 "
                        f"{limits['max_bytes'] / 1048576:.0f} MiB，已删除。"
                        "请降低帧率或质量后重试，或先裁剪更短的循环片段。"
                    ),
                })
                return
            self.emit_event("bg_convert_progress", {"percent": 100, "stage": "done"})
            self.emit_event("bg_convert_done", {
                "path": output_path,
                "bytes": size,
                "frames": len(ordered),
                "fps": fps,
                "pingpong": pingpong,
            })
        except Exception as error:  # noqa: BLE001 — 后台线程必须兜住一切
            logger.exception("背景图转换失败")
            self._safe_unlink(output_path)
            self.emit_event("bg_convert_error", {"message": f"转换异常：{error}"})
        finally:
            self._bg_convert_running = False
            self._bg_convert_proc = None
            shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _safe_unlink(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def install_converted_background(self, webp_path: str) -> Dict[str, Any]:
        """把转换好的 webp 直接设为当前主题的全局背景图（省去手动选图）。"""
        def operation():
            repository = get_gui_theme_repository()
            # 激活主题只能经 get_active() 读取（返回完整主题 DTO，id 即激活主题 id）。
            # 不能假设仓库有 active_theme_id()：目标库 ThemeRepository 没有该方法，
            # 裸调用会抛 AttributeError 被兜底成「GUI 主题操作失败」。这里显式判空，
            # 无激活主题时给出可读提示。
            active_theme = repository.get_active()
            active = active_theme.get("id") if isinstance(active_theme, dict) else None
            if not active:
                raise ThemeError("no_active_theme", "theme", "当前没有激活的主题")
            asset_path = repository.import_image(active, "app", Path(webp_path))
            return {"ok": True, "theme_id": active, "asset_path": asset_path}

        return self._run_gui_theme_operation("install_converted_background", operation, "theme")

    def get_gui_theme_asset(self, theme_id: str, asset_path: str) -> Dict[str, Any]:
        """Compatibility alias for the initially published Task 3 method name."""
        return self.read_gui_theme_asset(theme_id, asset_path)

    def preview_native_titlebar(self, native_titlebar: Dict[str, Any]) -> Dict[str, Any]:
        def operation():
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
        if session_name not in self._cancel_events:
            self._cancel_events[session_name] = threading.Event()
        return self._cancel_events[session_name]

    def _touch_turn_activity(self, session_name: str) -> None:
        lock = getattr(self, "_turn_activity_lock", None)
        activity = getattr(self, "_turn_activity", None)
        if lock is None or activity is None:
            return
        with lock:
            activity[session_name] = time.monotonic()

    def _turn_idle_seconds(self, session_name: str) -> float:
        lock = getattr(self, "_turn_activity_lock", None)
        activity = getattr(self, "_turn_activity", None)
        if lock is None or activity is None:
            return 0.0
        with lock:
            last = activity.get(session_name)
        return time.monotonic() - last if last else 0.0

    def _is_turn_active(self, session_name: str) -> bool:
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
        try:
            self._pet_notify_event(event_type, data)
        except Exception:
            logger.debug("桌宠提示转发失败", exc_info=True)

    def _emit_raw(self, event_type: str, data: Any):
        if self._shutting_down:
            return
        try:
            payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
            payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        except Exception as e:
            logger.warning("序列化事件 %s 失败: %s", event_type, e)
            return
        if self._window:
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
        relay = getattr(self, "_cloud_relay_service", None)
        if relay is not None:
            relay.on_event(event_type, data)

    def _queue_js_payload(self, payload: str) -> None:
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
            if payload is None:
                return
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
        payload = None
        while True:
            try:
                raw = self._js_queue.get_nowait()
            except queue.Empty:
                break
            if raw is None:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw
        return payload

    def _emit_delta(self, kind: str, text: str, session_name: str = ""):
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
                swapped_text = sanitize_protocol_text(swapped_text)
            if swapped_text:
                self._emit_raw(swapped[0], {"text": swapped_text, "session_name": session_name})
        self._ensure_delta_flusher()
        if flush_now:
            self._flush_deltas()

    def _flush_deltas(self, final: bool = False):
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
                        text, rest = sanitize_stream_text(text)
                        if rest:
                            buf.append(rest)
                if text:
                    pending.append((kind, text, session_name))
            self._delta_last_flush = time.monotonic()
        for kind, text, session_name in pending:
            self._emit_raw(kind, {"text": text, "session_name": session_name})

    def _finalize_session_deltas(self, session_name: str) -> None:
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
                    final_media = self._media_items_for_frontend(msg.get("_display"))
                    break
            final_text = sanitize_protocol_text(final_text)
            if final_text or final_reasoning or final_media:
                payload = {
                    "session_name": session_name,
                    "content": final_text,
                    "reasoning_content": final_reasoning,
                }
                if final_media:
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

    def _run_media_migration_once(self) -> None:
        """历史消息 media_v1 清单迁移（幂等，失败只记日志，绝不影响启动）。"""
        try:
            from core.media_migration import run_media_migration_once
            run_media_migration_once(self._memory)
        except Exception:
            logger.debug("媒体清单迁移执行异常", exc_info=True)

    def _find_latest_session(self) -> str:
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
        try:
            source = media_server.local_path_for_url(url or "")
        except Exception:
            logger.debug("媒体 URL 反解失败", exc_info=True)
            source = None
        if not source:
            return {"ok": False, "msg": "该媒体不在本地媒体仓库中，无法另存"}
        if self._window is None:
            return {"ok": False, "msg": "窗口尚未就绪，无法打开保存对话框"}

        ext = os.path.splitext(source)[1].lower() or ".png"
        try:
            import webview

            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=_media_save_filename(suggested_name, ext),
                file_types=_media_save_file_types(ext),
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
        raise_on_error: bool = False,
    ) -> Tuple[List[Dict[str, Any]], bool, Optional[int]]:
        msgs: List[Dict[str, Any]] = []
        has_more = False
        oldest_id: Optional[int] = None
        from core.computer.provider_generic import is_observation_protocol_message

        try:
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
                        if is_observation_protocol_message(msg):
                            continue
                        if is_system_injected_user_text(content):
                            continue
                        content = strip_context_suffix(content)
                    elif role == "assistant":
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
                        "model_id": (msg.get("_display") or {}).get("model_id") or "",
                        "model_name": (msg.get("_display") or {}).get("model_name") or "",
                        "metrics": (msg.get("_display") or {}).get("metrics") or None,
                        "error_note": (msg.get("_display") or {}).get("error_note") or "",
                        "execution_state": (msg.get("_display") or {}).get("execution_state") or "",
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
            if raise_on_error:
                raise
        return msgs, has_more, oldest_id

    def load_older_messages(self, session_name: str, before_id: Any) -> Dict[str, Any]:
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
        try:
            msgs, has_more, oldest_id = self._load_session_messages(
                session_name, before_id=before_id, raise_on_error=True
            )
        except Exception as e:
            # 读库失败必须显式报错（ok=False）：前端据此保留原分页状态、下次重试，
            # 而不是把「读失败」当成「已到最早」永久关掉翻页
            logger.exception("加载更早历史失败: %s", session_name)
            return {"ok": False, "messages": [], "has_more": False, "oldest_id": None, "msg": str(e)}
        return {"ok": True, "messages": msgs, "has_more": has_more, "oldest_id": oldest_id}

    _NAV_PREVIEW_MAX = 100

    def get_session_nav_index(self, session_name: str) -> Dict[str, Any]:
        try:
            raw_items = self._memory.get_session_user_nav_items(session_name)
            items = []
            for it in raw_items:
                if it.get("is_observation"):
                    continue
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
        logger.info("get_initial_state 入口被调用（前端桥已打通）")
        # 前端桥已打通：后台惰性启动局域网手机远程控制服务（不阻塞初始化），
        # 同一线程里紧接着启动局域网 MCP 服务（手机一打开星瑶就能连上手机端
        # MCP 客户端；MCP 复用 LAN 的配对 token，故必须排在 LAN 之后）。
        def _startup_lan_and_mcp():
            try:
                self._ensure_lan_server()
            except Exception:
                logger.debug("局域网远程控制服务启动失败", exc_info=True)
            self._ensure_mcp_server()

        try:
            threading.Thread(
                target=_startup_lan_and_mcp,
                daemon=True,
                name="lan-remote-startup",
            ).start()
        except Exception:
            logger.debug("局域网远程控制服务启动线程创建失败", exc_info=True)
        try:
            threading.Thread(
                target=self._ensure_feishu_service,
                daemon=True,
                name="feishu-startup",
            ).start()
        except Exception:
            logger.debug("飞书通道服务启动线程创建失败", exc_info=True)
        try:
            self._cloud_relay_watcher_active = True
            threading.Thread(
                target=self._ensure_cloud_relay,
                daemon=True,
                name="cloud-relay-startup",
            ).start()
            threading.Thread(
                target=self._cloud_relay_watcher_loop,
                daemon=True,
                name="cloud-relay-config-watcher",
            ).start()
        except Exception:
            logger.debug("云中转启动线程创建失败", exc_info=True)
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
                file_tree = []
                threading.Thread(
                    target=self._async_load_file_tree,
                    args=(workdir,),
                    daemon=True,
                    name="gui-file-tree-loader",
                ).start()
            threading.Thread(
                target=self._async_recalc_token_usage,
                args=(self._current_session,),
                daemon=True,
                name="gui-token-recalc",
            ).start()
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
                # 决策模型总开关：未开启时前端不展示「自动决策」权限档位
                "decision_enabled": self._is_decision_model_enabled(),
                "allow_outside_workspace": bool(config_data.get("allow_outside_workspace", False)),
                "system_settings": self.get_system_settings(),
                "persona_settings": self.get_persona_settings(),
                "disclaimer": self.get_disclaimer_consent(),
                "sessions": self.get_sessions(),
                "skills": list(getattr(self, "_skills_cache", None) or []),
                "mcps": self._build_mcp_list(),
                "plugins": self.get_plugins(),
                "workdir": workdir,
                "is_global": is_global,
                "file_tree": file_tree,
                "current_session": self._current_session,
                "messages": initial_msgs,
                "history_has_more": init_has_more,
                "history_oldest_id": init_oldest_id,
                "active_sessions": [
                    name for name, t in getattr(self, "_active_turns", {}).items()
                    if t.is_alive()
                ],
                "active_turn_snapshots": self.get_all_active_turn_snapshots(),
                "active_turn_snapshot": self.get_active_turn_snapshot(self._current_session),
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
                "decision_enabled": False,
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

    def _get_file_tree(self, path: str, depth: int = FILE_TREE_MAX_DEPTH) -> List[Dict[str, Any]]:
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
        try:
            if not workdir or workdir == "GLOBAL" or not os.path.isdir(workdir):
                return {"ok": False, "file_tree": []}
            return {"ok": True, "file_tree": self._get_file_tree(workdir)}
        except Exception as e:
            logger.exception("get_file_tree 失败")
            return {"ok": False, "file_tree": [], "msg": str(e)}

    def get_dir_children(self, path: str) -> Dict[str, Any]:
        try:
            if not path or path == "GLOBAL" or not os.path.isdir(path):
                return {"ok": False, "children": [], "msg": "not a directory"}
            return {"ok": True, "children": self._get_file_tree(path, depth=1)}
        except Exception as e:
            return {"ok": False, "children": [], "msg": str(e)}

    _FILE_SUGGEST_IGNORE_DIRS = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode",
    }
    _FILE_SUGGEST_CACHE_TTL = 5.0

    def _suggest_scan_root(self) -> str:
        try:
            wdir = self._session_workdir(self._current_session)
        except Exception:
            wdir = ""
        if wdir and wdir != "GLOBAL" and os.path.isdir(wdir):
            return _resolve_real_path(wdir)
        return _resolve_real_path(os.getcwd())

    def _list_workspace_files_for_suggest(self) -> List[str]:
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
            raw = self._memory.get_all_sessions(
                workdir=None, include_empty=True, include_archived=False
            )
            items = []
            for s in raw:
                name = str(s.get("name") or "")
                if not name or (exclude_name and name == exclude_name):
                    continue
                count = int(s.get("count") or 0)
                if count == 0 and re.match(r"^session_[a-f0-9]{6}$", name):
                    continue
                if q and q not in name.lower() and q not in str(s.get("search_text") or "").lower():
                    continue
                items.append({
                    "name": name,
                    "count": count,
                    "updated_at": str(s.get("updated_at") or ""),
                    "workdir": str(s.get("workdir") or ""),
                })
            items.sort(key=lambda it: it["updated_at"], reverse=True)
            if q:
                # 稳定排序：名字前缀命中 → 名字子串命中 → 仅正文命中。
                # 正文命中排最后：这类结果的相关性最弱（可能只是会话中途
                # 提过一嘴），不能把真正名字命中的目标挤出前 12 条。
                # 同档内名字更短的更贴近输入，再保持 updated_at 倒序。
                items.sort(key=lambda it: (
                    0 if it["name"].lower().startswith(q)
                    else (1 if q in it["name"].lower() else 2),
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
        include_archived: bool = False,
    ) -> Dict[str, Any]:
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
                include_archived=bool(include_archived),
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

    def get_workspace_prompt_suggestions(
        self,
        workdir: str,
        git_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """按工作区项目特征生成新会话推荐提示词（只读，不修改磁盘或切换目录）。"""
        try:
            raw = str(workdir or "").strip()
            if not raw or raw.upper() == "GLOBAL":
                return {"ok": True, "workdir": raw, "suggestions": []}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": True, "workdir": raw, "suggestions": []}
            recent_titles: List[str] = []
            try:
                rows = self._memory.get_all_sessions(
                    workdir=path, sources=["GUI"], include_empty=False
                )
                recent_titles = [
                    str(r.get("title") or r.get("name") or "").strip()
                    for r in (rows or [])[:3]
                    if str(r.get("title") or r.get("name") or "").strip()
                ]
            except Exception:
                recent_titles = []
            suggestions = build_workspace_prompt_suggestions(
                path,
                git_snapshot=git_snapshot if isinstance(git_snapshot, dict) else None,
                recent_session_titles=recent_titles,
            )
            return {"ok": True, "workdir": path, "suggestions": suggestions}
        except Exception as e:
            logger.exception("get_workspace_prompt_suggestions 失败")
            return {"ok": False, "msg": str(e), "suggestions": []}

    def get_novel_status(self, workdir: str) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw == "GLOBAL":
                return {"is_novel": False, "error": f"项目文件夹不存在: {raw or '<empty>'}"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"is_novel": False, "error": f"项目文件夹不存在: {raw}"}
            return novel_service.novel_status(path)
        except Exception as e:
            logger.exception("get_novel_status 失败")
            return {"is_novel": False, "error": str(e)}

    def get_novel_chapter(self, workdir: str, chapter: Any) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw == "GLOBAL":
                return {"ok": False, "error": f"项目文件夹不存在: {raw or '<empty>'}"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"项目文件夹不存在: {raw}"}
            return novel_service.read_chapter(path, chapter)
        except Exception as e:
            logger.exception("get_novel_chapter 失败")
            return {"ok": False, "error": str(e)}

    def save_novel_chapter(self, workdir: str, chapter: Any, content: str) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw == "GLOBAL":
                return {"ok": False, "error": f"项目文件夹不存在: {raw or '<empty>'}"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"项目文件夹不存在: {raw}"}
            return novel_service.save_chapter(path, chapter, content)
        except Exception as e:
            logger.exception("save_novel_chapter 失败")
            return {"ok": False, "error": str(e)}

    def get_project_profile(self, workdir: str) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw.upper() == "GLOBAL":
                return {"ok": False, "error": "当前会话没有归属项目目录（自由对话分组不记录项目状态）"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"项目文件夹不存在: {raw}"}
            state = project_profile.read_state(path)
            state["ok"] = True
            return state
        except Exception as e:
            logger.exception("get_project_profile 失败")
            return {"ok": False, "error": str(e)}

    def save_project_profile(self, workdir: str, section: str, content: str) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw.upper() == "GLOBAL":
                return {"ok": False, "error": "当前会话没有归属项目目录（自由对话分组不记录项目状态）"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"项目文件夹不存在: {raw}"}
            if not project_profile.set_section(path, section, content):
                return {"ok": False, "error": "项目状态没有写成（写入被拒绝或目录不可写）"}
            state = project_profile.read_state(path)
            state["ok"] = True
            return state
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.exception("save_project_profile 失败")
            return {"ok": False, "error": str(e)}

    def list_novel_projects(self, workdir: str) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw == "GLOBAL":
                return {"ok": False, "error": f"项目文件夹不存在: {raw or '<empty>'}"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"项目文件夹不存在: {raw}"}
            return novel_service.discover_novel_projects(path)
        except Exception as e:
            logger.exception("list_novel_projects 失败")
            return {"ok": False, "error": str(e)}

    def export_novel(self, workdir: str, fmt: str = "md") -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw == "GLOBAL":
                return {"ok": False, "error": f"项目文件夹不存在: {raw or '<empty>'}"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"项目文件夹不存在: {raw}"}
            return novel_service.export_novel(path, fmt)
        except Exception as e:
            logger.exception("export_novel 失败")
            return {"ok": False, "error": str(e)}

    def get_novel_workbench(self, workdir: str) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw == "GLOBAL":
                return {"is_novel": False, "error": f"项目文件夹不存在: {raw or '<empty>'}"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"is_novel": False, "error": f"项目文件夹不存在: {raw}"}
            return novel_service.novel_workbench(path)
        except Exception as e:
            logger.exception("get_novel_workbench 失败")
            return {"is_novel": False, "error": str(e)}

    def read_novel_doc(self, workdir: str, rel_path: str) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw == "GLOBAL":
                return {"ok": False, "error": f"项目文件夹不存在: {raw or '<empty>'}"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"项目文件夹不存在: {raw}"}
            return novel_service.read_novel_doc(path, rel_path)
        except Exception as e:
            logger.exception("read_novel_doc 失败")
            return {"ok": False, "error": str(e)}

    def run_novel_audit(
        self, workdir: str, scope: str = "chapter", chapter: Any = None
    ) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw == "GLOBAL":
                return {"ok": False, "error": f"项目文件夹不存在: {raw or '<empty>'}"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"项目文件夹不存在: {raw}"}
            return novel_service.novel_audit(path, scope=scope, chapter=chapter)
        except Exception as e:
            logger.exception("run_novel_audit 失败")
            return {"ok": False, "error": str(e)}

    def run_novel_audit_fix(
        self, workdir: str, finding: Any = None, action: str = "defer",
        planned_resolution_chapter: Any = None, dry_run: bool = False
    ) -> Dict[str, Any]:
        try:
            raw = str(workdir or "").strip()
            if not raw or raw == "GLOBAL":
                return {"ok": False, "error": f"项目文件夹不存在: {raw or '<empty>'}"}
            path = _resolve_real_path(raw)
            if not os.path.isdir(path):
                return {"ok": False, "error": f"项目文件夹不存在: {raw}"}
            return novel_service.novel_audit_fix(
                path,
                finding,
                action,
                planned_resolution_chapter=planned_resolution_chapter,
                dry_run=bool(dry_run),
            )
        except Exception as e:
            logger.exception("run_novel_audit_fix 失败")
            return {"ok": False, "error": str(e)}

    def browse_remote_dirs(self, path: str = "") -> Dict[str, Any]:
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
        try:
            selected_path = _resolve_real_path(str(path or ""))
            if not selected_path or not os.path.isdir(selected_path):
                return {"ok": False, "msg": "路径不是有效文件夹"}
            if self._memory is None:
                return {"ok": False, "msg": "后端未就绪"}
            self._memory.add_workspace(selected_path)
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
            # 只登记目录，绝不改写已有会话归属：曾在此把当前 GLOBAL 会话
            # 自动迁进所选目录（a9a509cb），但"聊着天引入新工作区"也会被
            # 误搬走。需要迁移时由用户在侧栏拖拽 / 右键显式操作。
            self._memory.add_workspace(selected_path)
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
        try:
            self._recalc_token_usage(session_name)
            usage = self.get_token_usage()
            self._emit_raw("token_usage_updated", usage)
            logger.info("异步 token 用量重算完成: %s", session_name)
        except Exception:
            logger.debug("异步 token 用量重算失败: %s", session_name, exc_info=True)

    def _async_build_skill_list(self):
        # 同一时刻只允许一个扫描线程（见 _skills_scan_lock 的说明）；
        # 取不到锁属性（旧实例/测试裸构造）时按"无并发"降级处理
        lock = getattr(self, "_skills_scan_lock", None)
        if lock is not None and not lock.acquire(blocking=False):
            logger.debug("技能扫描已在进行中，跳过重复触发")
            return
        try:
            started = time.time()
            skills = self._build_skill_list()
            self._skills_cache = skills
            logger.info(
                "异步技能列表扫描完成（%d 项，耗时 %.2fs）", len(skills), time.time() - started
            )
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

    def get_sessions(self) -> List[Dict[str, Any]]:
        try:
            # include_empty=True：空会话交给下方自己的跳过逻辑处理（当前会话
            # 即使还没有消息也要显示），否则刚新建的会话在侧栏永远是 0 个会话
            # 一次取回 GUI 与秘书会话：秘书归属必须靠 source 区分，因为秘书会话的
            # workdir 同为 "GLOBAL"，光看路径与自由对话无从辨别
            raw = self._memory.get_all_sessions(
                workdir=None, sources=["GUI", "SECRETARY"], include_empty=True
            )
            saved_workspaces = self._memory.get_all_workspaces()
            current_wdir = _resolve_real_path(os.getcwd())
            app_dir = _app_install_dir()
            # “当前”标记必须以【当前会话归属】为准，不能用进程 cwd：
            # 恢复最近会话会 chdir 到项目目录，此时新建自由对话，
            # 若按 cwd 标记就会出现“对话”和项目分组同时显示“当前”
            current_session_wdir = self._session_workdir(self._current_session)
            # 秘书会话的 workdir 同样是 "GLOBAL"，所以“当前”归属也只能看 source：
            # 否则“秘书”和“对话”两个分组会同时显示为当前分组
            current_is_secretary = any(
                s.get("name") == self._current_session
                and (s.get("source") or "GUI") == "SECRETARY"
                for s in raw
            )

            if current_wdir != "GLOBAL" and current_wdir != app_dir:
                self._memory.add_workspace(current_wdir)

            groups_dict: Dict[str, Dict[str, Any]] = {}

            global_group = {
                "folder_path": "GLOBAL",
                "folder_name": "对话",
                "is_global": True,
                "is_current": (current_session_wdir == "GLOBAL" and not current_is_secretary),
                "sessions": [],
            }

            # 秘书分组：与"对话"一样不对应任何项目文件夹，故 is_global 必须是 False
            # （前端多处用 is_global 判定"自由对话分组"，不能被秘书组误命中）；
            # folder_path 仅作分组标识，不会作为 workdir 落库
            secretary_group = {
                "folder_path": "SECRETARY",
                "folder_name": "秘书",
                "is_global": False,
                "is_secretary": True,
                "is_current": current_is_secretary,
                "sessions": [],
            }

            all_known_wdirs = set(saved_workspaces)
            if current_wdir != "GLOBAL" and current_wdir != app_dir:
                all_known_wdirs.add(current_wdir)

            for s in raw:
                wdir_item = s.get("workdir")
                if wdir_item and wdir_item not in ("GLOBAL", "SECRETARY"):
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

            for s in raw:
                msg_count = s.get("count", 0)
                name = s.get("name", "")
                is_default_name = bool(re.match(r"^session_[a-f0-9]{6}$", name))

                if msg_count == 0 and is_default_name and name != self._current_session:
                    continue

                wdir = s.get("workdir")
                display = "新会话" if is_default_name else name

                if (s.get("source") or "GUI") == "SECRETARY":
                    sec_name = secretary.get_secretary_name(name) or secretary.FALLBACK_SECRETARY_NAME
                    secretary_group["sessions"].append({
                        "name": name,
                        "session_id": s.get("id"),
                        "display_name": sec_name,
                        "secretary_name": sec_name,
                        "secretary_persona": secretary.get_secretary_persona(name),
                        # 显式标注秘书归属（前端此前靠 secretary_name 兜底判定，
                        # 这里给出明确字段，避免与"名字恰好叫秘书"的普通会话混淆）
                        "is_secretary": True,
                        "count": msg_count,
                        "workdir": "GLOBAL",
                        "is_global": True,
                        "updated_at": s.get("updated_at") or "",
                    })
                    continue

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

            # 3. 秘书组与全局对话组无条件保留在最顶部（秘书组置顶），
            # 项目分组按名称稳定排序。不能把"当前会话所在目录"置顶：
            # 否则点击不同目录的会话时列表会整体跳动
            result_groups = [secretary_group, global_group]
            project_groups = list(groups_dict.values())
            project_groups.sort(key=lambda g: g["folder_name"])
            result_groups.extend(project_groups)
            return result_groups
        except Exception:
            logger.exception("get_sessions 失败")
            return []

    def get_archived_sessions(self) -> List[Dict[str, Any]]:
        try:
            # 归档区必须同时收 GUI 与 SECRETARY：秘书会话已归档后在秘书分组里被
            # 归档过滤隐去，若这里仍按 source="GUI" 过滤就会两边都看不到，等于
            # 凭空消失（归档只是标记，列表里丢了就再也找不回来）
            raw = self._memory.get_all_sessions(
                workdir=None, sources=["GUI", "SECRETARY"],
                include_empty=True, archived_only=True
            )
            items: List[Dict[str, Any]] = []
            for s in raw:
                name = s.get("name", "")
                wdir = s.get("workdir")
                is_global = (not wdir) or wdir == "GLOBAL"
                is_secretary = (s.get("source") or "GUI") == secretary.SECRETARY_SOURCE
                if is_secretary:
                    sec_name = (
                        secretary.get_secretary_name(name)
                        or secretary.FALLBACK_SECRETARY_NAME
                    )
                    display_name = sec_name
                else:
                    display_name = (
                        "新会话" if re.match(r"^session_[a-f0-9]{6}$", name) else name
                    )
                entry = {
                    "name": name,
                    "session_id": s.get("id"),
                    "display_name": display_name,
                    "count": s.get("count", 0),
                    "workdir": "GLOBAL" if is_global else _resolve_real_path(wdir),
                    "is_global": is_global,
                    "updated_at": s.get("updated_at") or "",
                    "archived_at": s.get("archived_at") or "",
                }
                if is_secretary:
                    entry["secretary_name"] = display_name
                    entry["secretary_persona"] = secretary.get_secretary_persona(name)
                    entry["is_secretary"] = True
                items.append(entry)
            # archived_at 是 ISO8601 文本，字符串倒序即时间倒序；
            # 同一时刻归档的会话再按名称兜底，避免列表顺序抖动
            items.sort(key=lambda it: (it["archived_at"], it["name"]), reverse=True)
            return items
        except Exception:
            logger.exception("get_archived_sessions 失败")
            return []

    def _session_archived_at(self, session_name: str) -> Optional[str]:
        for s in self._memory.get_all_sessions(
            workdir=None, include_empty=True, include_archived=True
        ):
            if s.get("name") == session_name:
                return s.get("archived_at") or None
        return None

    def create_session(self, title: str = "新会话", workdir: Optional[str] = None,
                       source: str = "GUI") -> Dict[str, Any]:
        try:
            new_name = f"session_{uuid.uuid4().hex[:6]}"
            target_workdir = workdir or _resolve_real_path(os.getcwd())
            self._memory.create_session_explicit(new_name, workdir=target_workdir, source=source)
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

    def create_secretary_session(self, title: str = "新会话", name: str = "",
                                 persona: str = "") -> Dict[str, Any]:
        try:
            try:
                secretary_name = secretary.validate_secretary_name(name)
                secretary_persona = secretary.validate_secretary_persona(persona)
            except ValueError as e:
                return {"ok": False, "msg": str(e)}
            new_name = f"session_{uuid.uuid4().hex[:6]}"
            self._memory.create_session_explicit(
                new_name, workdir="GLOBAL", source="SECRETARY",
                secretary_name=secretary_name, secretary_persona=secretary_persona,
            )
            # 新会话名此前不可能进过缓存，这里失效是为并发路径兜底（同名复用等），
            # 成本一次字典 pop，却能让首轮工具提示 / 标题立刻拿到新名字
            secretary.invalidate_secretary_cache(new_name)
            self._set_current_session(new_name)
            # 同 create_session：新建后立即重算，避免残留旧会话的 token 统计
            self._recalc_token_usage(new_name)
            return {
                "ok": True,
                "session_name": new_name,
                "title": title,
                "display_name": secretary_name,
                "secretary_name": secretary_name,
                "secretary_persona": secretary_persona,
                "workdir": "GLOBAL",
            }
        except Exception as e:
            logger.exception("create_secretary_session 失败")
            return {"ok": False, "msg": str(e)}

    def set_secretary_persona(self, session_name: str, persona: str = "") -> Dict[str, Any]:
        """设置某个秘书会话的专属人设（空串 = 恢复内置默认秘书人设）。

        人设只落 `secretary_persona` 列，不碰 session_name / 消息 / 摘要，因此
        改人设不迁移任何键；改完必须失效身份缓存，否则同一进程内已缓存旧身份的
        高频路径（提示词、工具提示）会沿用旧人设到下次重启。
        """
        try:
            if not session_name:
                return {"ok": False, "msg": "会话名不能为空"}
            if not secretary.is_secretary_session(session_name):
                return {"ok": False, "msg": "该会话不是秘书会话，无法设置秘书人设"}
            try:
                cleaned = secretary.validate_secretary_persona(persona)
            except ValueError as e:
                return {"ok": False, "msg": str(e)}
            if not self._memory.set_session_secretary_persona(session_name, cleaned):
                return {"ok": False, "msg": f"会话 {session_name} 不存在"}
            secretary.invalidate_secretary_cache(session_name)
            return {
                "ok": True,
                "session_name": session_name,
                "secretary_persona": cleaned,
                "sessions": self.get_sessions(),
            }
        except Exception as e:
            logger.exception("set_secretary_persona 失败")
            return {"ok": False, "msg": str(e)}

    def get_active_agent_name(self) -> Dict[str, Any]:
        try:
            return {
                "agent_name": secretary.get_active_agent_name(),
                "is_secretary": bool(secretary.is_secretary_session()),
            }
        except Exception as e:
            logger.exception("get_active_agent_name 失败")
            return {"agent_name": "", "is_secretary": False, "msg": str(e)}

    def rename_secretary_session(self, session_name: str, new_name: str) -> Dict[str, Any]:
        """给秘书会话改名：只改 `secretary_name`，不动 `session_name`。

        名字是提示词身份与显示名的共同来源，改完必须失效缓存，否则同一进程内
        早先缓存过旧名字的高频路径（工具提示、标题）会继续显示旧名。
        """
        try:
            if not session_name:
                return {"ok": False, "msg": "会话名不能为空"}
            if not secretary.is_secretary_session(session_name):
                return {"ok": False, "msg": "该会话不是秘书会话，无法改名"}
            try:
                cleaned = secretary.validate_secretary_name(new_name)
            except ValueError as e:
                return {"ok": False, "msg": str(e)}
            if not self._memory.set_session_secretary_name(session_name, cleaned):
                return {"ok": False, "msg": f"会话 {session_name} 不存在"}
            secretary.invalidate_secretary_cache(session_name)
            return {
                "ok": True,
                "session_name": session_name,
                "secretary_name": cleaned,
                "display_name": cleaned,
                "sessions": self.get_sessions(),
            }
        except Exception as e:
            logger.exception("rename_secretary_session 失败")
            return {"ok": False, "msg": str(e)}

    def _set_current_session(self, session_name: str):
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
                candidate_wdir = target_workdir or stored_wdir
                if candidate_wdir and candidate_wdir != "GLOBAL" and os.path.isdir(candidate_wdir):
                    candidate_wdir = _resolve_real_path(candidate_wdir)

            msgs, has_more, oldest_id = self._load_session_messages(session_name)

            self._async_recalc_token_usage(session_name)
            session_model = get_active_model(session_name) or {}
            result = {
                "ok": True,
                "session_name": session_name,
                "messages": msgs,
                "history_has_more": has_more,
                "history_oldest_id": oldest_id,
                "sessions": self.get_sessions(),
                "active_model_id": str(session_model.get("id") or ""),
                "active_model_name": str(session_model.get("name") or "未激活模型"),
                "reasoning": _reasoning_snapshot_for_model(session_model),
                # 会话级权限档位随切换返回：前端权限按钮按会话恢复显示
                "permission_mode": self._effective_permission_mode(session_name),
                "message_queue": self._message_queue_snapshot(session_name),
                "active_turn_snapshot": self.get_active_turn_snapshot(session_name),
            }
            if is_global:
                result.update({"workdir": "GLOBAL", "is_global": True})
            else:
                current_workdir = _resolve_real_path(candidate_wdir or stored_wdir or os.getcwd())
                result.update({"workdir": current_workdir, "is_global": False})
            self._emit_token_usage()
            self.emit_event("active_session_changed", {"session_name": session_name})
            return result
        except Exception as e:
            logger.exception("switch_session 失败")
            return {"ok": False, "msg": str(e), "messages": []}

    def _migrate_session_aux_state(
        self, old_name: str, final_name: str, *, holds_turn_start_lock: bool = False
    ) -> None:
        try:
            secretary.invalidate_secretary_cache(old_name)
            secretary.invalidate_secretary_cache(final_name)
        except Exception:
            logger.debug("秘书缓存随改名失效失败", exc_info=True)
        try:
            from core import permission_grants
            from core.tools import rename_session_sandbox_bypass

            permission_grants.rename_task(old_name, final_name)
            rename_session_sandbox_bypass(old_name, final_name)
        except Exception:
            logger.warning("授权/沙盒放行随改名迁移失败 (%s -> %s)", old_name, final_name, exc_info=True)
        self._migrate_goal_state(old_name, final_name)
        if holds_turn_start_lock:
            self._migrate_message_queue_unlocked(old_name, final_name)
        else:
            with self._turn_start_lock:
                self._migrate_message_queue_unlocked(old_name, final_name)

    def _migrate_goal_state(self, old_name: str, final_name: str) -> None:
        if not old_name or not final_name or old_name == final_name:
            return
        try:
            store = self._goal_store()
            state = store.load(old_name)
            if state is None:
                return
            state.session = final_name
            store.save(state)
            store.clear(old_name)
            runs = self._goal_runs_map()
            if old_name in runs:
                runs[final_name] = runs.pop(old_name)
        except Exception:
            # 迁移失败不能让改名整体失败：界面仍要看到改名成功，目标可在新名下重建
            logger.warning("目标状态随改名迁移失败 (%s -> %s)", old_name, final_name, exc_info=True)

    def _migrate_message_queue_unlocked(self, old_name: str, final_name: str) -> None:
        if old_name in self._message_queues:
            self._message_queues[final_name] = self._message_queues.pop(old_name)
            self._emit_message_queue_snapshot(final_name)

    def rename_session(self, old_name: str, new_name: str) -> Dict[str, Any]:
        try:
            if self._is_turn_active(old_name):
                return {"ok": False, "msg": "该会话正在运行回合，请先停止或等待完成后再重命名", "name": old_name}
            if old_name in self._goal_runs_map():
                return {"ok": False, "msg": "该会话有进行中的目标，请先 /goal stop 再重命名", "name": old_name}
            success, final_name = self._memory.rename_session(
                old_name, new_name, title_source="manual"
            )
            if not success:
                return {"ok": False, "msg": "重命名失败（可能目标名称已存在）", "name": old_name}
            if self._current_session == old_name:
                self._set_current_session(final_name)
            self._migrate_session_aux_state(old_name, final_name)
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

    def move_session_to_workdir(self, session_name: str, workdir: str) -> Dict[str, Any]:
        name = str(session_name or "").strip()
        if not name:
            return {"ok": False, "msg": "会话名不能为空"}
        if self._memory is None:
            return {"ok": False, "msg": "后端未就绪"}
        try:
            if self._is_turn_active(name):
                return {"ok": False, "msg": "该会话正在运行回合，请先停止或等待完成后再移动", "name": name}
            try:
                from core.tools import subagent_manager
                if subagent_manager.has_running_background_tasks(name):
                    return {"ok": False, "msg": "该会话还有后台任务在执行，请等待完成或停止后再移动", "name": name}
            except Exception:
                logger.debug("查询后台任务状态失败", exc_info=True)

            from core.memory import _normalize_workdir

            target = str(workdir or "").strip()
            if not target:
                return {"ok": False, "msg": "目标分组不能为空", "name": name}
            if target != "GLOBAL":
                resolved = _resolve_real_path(target)
                if not resolved or not os.path.isdir(resolved):
                    return {"ok": False, "msg": "目标文件夹不存在或不可访问", "name": name}
                target = _normalize_workdir(resolved)
                # 登记工作区，否则侧栏拿不到这个分组（空分组也要能显示出来）
                self._memory.add_workspace(target)

            # 先取当前归属：会话不存在要报错，不能落到 _session_workdir 的
            # "查不到就按当前 cwd 处理"兜底上（那会把不存在的会话误判成"已在目标分组"）
            current = None
            current_source = "GUI"
            # include_archived=True：归档只是列表隐藏，会话仍存在，不能因为已归档
            # 就误报"会话不存在"
            for s in self._memory.get_all_sessions(
                workdir=None, include_empty=True, include_archived=True
            ):
                if s.get("name") == name:
                    current = s.get("workdir") or "GLOBAL"
                    current_source = s.get("source") or "GUI"
                    break
            if current is None:
                return {"ok": False, "msg": "会话不存在", "name": name}
            if current_source == secretary.SECRETARY_SOURCE:
                return {"ok": False, "msg": "秘书会话固定归在「秘书」分组，不支持移动", "name": name}
            if current != "GLOBAL":
                current = _normalize_workdir(current)

            if current == target:
                # 已在目标分组：不写库。否则 updated_at 被刷新会把会话顶到该组
                # 列表最前，看起来像"什么都没干却跳了位置"
                return {"ok": True, "name": name, "workdir": target, "moved": False,
                        "sessions": self.get_sessions()}

            if not self._memory.update_session_workdir(name, target):
                return {"ok": False, "msg": "移动失败（会话可能已不存在）", "name": name}

            try:
                sessions = self.get_sessions()
                self.emit_event("session_moved", {
                    "name": name,
                    "workdir": target,
                    "sessions": sessions,
                })
            except Exception:
                logger.exception("推送 session_moved 事件失败")
                sessions = None
            return {"ok": True, "name": name, "workdir": target, "moved": True, "sessions": sessions}
        except Exception as e:
            logger.exception("move_session_to_workdir 失败")
            return {"ok": False, "msg": str(e), "name": name}

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
                self._message_queues.pop(session_name, None)

            self._memory.delete_session(session_name)
            # 会话没了，身份缓存必须跟着失效：否则将来复用到同名会话名时
            # 会读到已删会话的旧来源 / 旧秘书名
            secretary.invalidate_secretary_cache(session_name)
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

    def archive_session(self, session_name: str) -> Dict[str, Any]:
        try:
            if not session_name:
                return {"ok": False, "msg": "会话名不能为空"}
            if session_name == self._current_session:
                return {"ok": False, "msg": "不能归档当前活跃会话，请先切换到其它会话"}
            if not self._memory.archive_session(session_name):
                return {"ok": False, "msg": f"会话 {session_name} 不存在"}
            archived_at = self._session_archived_at(session_name)
            # 推送失败不能让前端误以为归档失败：DB 已生效，此时报失败更糟
            try:
                self.emit_event("session_archived", {
                    "name": session_name,
                    "session_name": session_name,
                    "archived_at": archived_at,
                    "sessions": self.get_sessions(),
                    "archived_sessions": self.get_archived_sessions(),
                })
            except Exception:
                logger.exception("推送 session_archived 事件失败")
            return {"ok": True, "name": session_name, "archived_at": archived_at}
        except Exception as e:
            logger.exception("archive_session 失败")
            return {"ok": False, "msg": str(e)}

    def unarchive_session(self, session_name: str) -> Dict[str, Any]:
        """取消归档：会话恢复回侧栏主列表；成功推送 session_unarchived。"""
        try:
            if not session_name:
                return {"ok": False, "msg": "会话名不能为空"}
            if not self._memory.unarchive_session(session_name):
                return {"ok": False, "msg": f"会话 {session_name} 不存在"}
            try:
                self.emit_event("session_unarchived", {
                    "name": session_name,
                    "session_name": session_name,
                    "archived_at": None,
                    "sessions": self.get_sessions(),
                    "archived_sessions": self.get_archived_sessions(),
                })
            except Exception:
                logger.exception("推送 session_unarchived 事件失败")
            return {"ok": True, "name": session_name, "archived_at": None}
        except Exception as e:
            logger.exception("unarchive_session 失败")
            return {"ok": False, "msg": str(e)}

    @staticmethod
    def _normalize_session_names(session_names: Any) -> List[str]:
        if isinstance(session_names, str) or not isinstance(session_names, (list, tuple, set)):
            return []
        seen = set()
        names: List[str] = []
        for raw in session_names:
            name = str(raw or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    def _emit_archive_batch_event(self, event_type: str, names: List[str],
                                  failed: List[Dict[str, str]]) -> None:
        try:
            self.emit_event(event_type, {
                "name": names[0] if names else "",
                "session_name": names[0] if names else "",
                "names": list(names),
                "failed": list(failed),
                "archived_at": None,
                "sessions": self.get_sessions(),
                "archived_sessions": self.get_archived_sessions(),
            })
        except Exception:
            logger.exception("推送 %s 事件失败", event_type)

    def unarchive_sessions(self, session_names: List[str]) -> Dict[str, Any]:
        try:
            names = self._normalize_session_names(session_names)
            if not names:
                return {"ok": False, "success": False, "msg": "会话名不能为空"}
            restored: List[str] = []
            failed: List[Dict[str, str]] = []
            for name in names:
                try:
                    if self._memory.unarchive_session(name):
                        restored.append(name)
                    else:
                        failed.append({"name": name, "msg": f"会话 {name} 不存在"})
                except Exception as e:
                    logger.exception("批量取消归档单项失败: %s", name)
                    failed.append({"name": name, "msg": str(e)})
            # 没有任何成功项时列表零变化，不推事件（避免前端白跑一次刷新）
            if restored:
                self._emit_archive_batch_event("session_unarchived", restored, failed)
            return {
                "ok": bool(restored),
                "success": bool(restored),
                "count": len(restored),
                "names": restored,
                "failed": failed,
                "sessions": self.get_sessions(),
                "archived_sessions": self.get_archived_sessions(),
                "msg": "" if restored else "选中的会话都不存在",
            }
        except Exception as e:
            logger.exception("unarchive_sessions 失败")
            return {"ok": False, "success": False, "msg": str(e)}

    def delete_archived_sessions(self, session_names: List[str]) -> Dict[str, Any]:
        try:
            names = self._normalize_session_names(session_names)
            if not names:
                return {"ok": False, "success": False, "msg": "会话名不能为空"}
            from core import permission_grants
            from core.tools import clear_session_sandbox_bypass

            deleted: List[str] = []
            failed: List[Dict[str, str]] = []
            for name in names:
                try:
                    if not self._session_archived_at(name):
                        failed.append({"name": name, "msg": "已不在归档区，已跳过"})
                        continue
                    with self._turn_start_lock:
                        if self._is_turn_active(name):
                            failed.append({
                                "name": name,
                                "msg": "该会话正在运行回合，请先停止或等待完成后再删除",
                            })
                            continue
                        self._message_queues.pop(name, None)
                    self._memory.delete_session(name)
                    # 与单条删除同口径：会话没了，身份缓存不能留
                    secretary.invalidate_secretary_cache(name)
                    permission_grants.clear_task(name)
                    clear_session_sandbox_bypass(name)
                    if self._current_session == name:
                        new_session = f"session_{uuid.uuid4().hex[:6]}"
                        self._memory.create_session_explicit(new_session, workdir="GLOBAL", source="GUI")
                        self._set_current_session(new_session)
                    deleted.append(name)
                except Exception as e:
                    logger.exception("批量删除归档会话单项失败: %s", name)
                    failed.append({"name": name, "msg": str(e)})
            if deleted:
                self._emit_archive_batch_event("session_deleted", deleted, failed)
            return {
                "ok": bool(deleted),
                "success": bool(deleted),
                "count": len(deleted),
                "names": deleted,
                "failed": failed,
                "sessions": self.get_sessions(),
                "archived_sessions": self.get_archived_sessions(),
                "current_session": self._current_session,
                "msg": "" if deleted else "选中的会话都不在归档区",
            }
        except Exception as e:
            logger.exception("delete_archived_sessions 失败")
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
                    secretary.invalidate_secretary_cache(name)
                    permission_grants.clear_task(name)
                    clear_session_sandbox_bypass(name)
            return {"ok": True, "success": True, "sessions": self.get_sessions()}
        except Exception as e:
            logger.exception("delete_folder_group 失败")
            return {"ok": False, "success": False, "msg": str(e)}

    def clear_session_history(self, session_name: Optional[str] = None) -> Dict[str, Any]:
        try:
            target = session_name or self._current_session
            # M21：与 delete/rename 同界——清空会删 messages、harness turns、
            # DeepSeek prompt generations 与摘要归档，在飞回合随后引用已删行
            # （CAS 世代被从脚下删掉、turn 边界失效），必须先停或等完成。
            if self._is_turn_active(target):
                return {
                    "ok": False, "success": False,
                    "msg": "该会话正在运行回合，请先停止或等待完成后再清空历史",
                }
            self._clear_session_last_error(target)
            deleted = self._memory.clear_session_messages(target)
            from core.llm import session_token_usage, token_usage_lock
            from core.context_breakdown import clear_breakdown
            with token_usage_lock:
                session_token_usage["latest_total"] = 0
                session_token_usage["latest_prompt"] = 0
                session_token_usage["latest_cached"] = 0
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
                self._memory.set_session_model_id(session_name, str(model_id))
            else:
                set_active_model(str(model_id))
            self._sync_active_model_context()
            # 切模型后 window 立即变为新模型的 context_window，但全局
            # latest_total 还是旧值（可能属于别的会话），必须先按当前会话
            # 历史重算 used，否则前端会算出 600K/256K=243% 这类虚假占比
            self._recalc_token_usage(session_name)
            self._emit_token_usage()
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

    def get_reasoning_snapshot_by_name(self, model_name: str) -> Dict[str, Any]:
        try:
            name = str(model_name or "").strip()
            if not name:
                return {"ok": False, "msg": "模型名不能为空"}
            return {"ok": True, "reasoning": build_reasoning_snapshot({"name": name})}
        except Exception as e:
            logger.exception("get_reasoning_snapshot_by_name 失败")
            return {"ok": False, "msg": str(e)}

    def set_secondary_model(self, model_id: str) -> Dict[str, Any]:
        try:
            mid = str(model_id or "").strip()
            if mid:
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

    @staticmethod
    def _is_decision_model_enabled() -> bool:
        from core.permission_auto_decision import is_decision_model_enabled

        return is_decision_model_enabled()

    def _effective_permission_mode(self, session_name: str) -> str:
        mode = ""
        if session_name:
            try:
                bound = self._memory.get_session_permission_mode(session_name)
                if bound:
                    mode = bound
            except Exception:
                logger.debug("读取会话权限档位失败", exc_info=True)
        if not mode:
            mode = getattr(self, "_current_permission_mode", "confirm") or "confirm"
        if mode == "auto_decision" and not self._is_decision_model_enabled():
            return "confirm"
        return mode

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
            if canonical == "auto_decision" and not self._is_decision_model_enabled():
                fallback_mode = (
                    self._effective_permission_mode(target_session)
                    if target_session
                    else getattr(self, "_current_permission_mode", "confirm")
                )
                return {
                    "ok": False,
                    "msg": "决策模型（JEV）尚未启用，无法切换到「自动决策」；请先在「设置 → 记忆与决策 → 决策模型（JEV）」中开启。",
                    "permission_mode": fallback_mode,
                }
            warning = ""
            if canonical == "auto_decision":
                from core.permission_auto_decision import get_auto_decision_readiness_warning

                warning = get_auto_decision_readiness_warning()
            if target_session:
                bound = self._memory.set_session_permission_mode(target_session, canonical)
                res: Dict[str, Any] = {"ok": True, "permission_mode": canonical, "bound": bound}
                if warning:
                    res["decision_warning"] = warning
                return res
            self._current_permission_mode = canonical
            app_context.set_permission_mode(canonical)
            try:
                from config import save_config_value
                save_config_value("permission_mode", canonical)
            except Exception:
                logger.warning("持久化权限模式失败", exc_info=True)
            res = {"ok": True, "permission_mode": canonical}
            if warning:
                res["decision_warning"] = warning
            return res
        except Exception as e:
            logger.exception("set_permission_mode 失败")
            return {"ok": False, "msg": str(e)}

    def post_notice(self, message: str, session_name: str = "", kind: str = "notice") -> Dict[str, Any]:
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

    def _notice_emitter(self, session_name: str):

        def _emit(kind: str, message: str) -> None:
            self.emit_event("notice", {
                "message": _strip_ansi_markup(message),
                "session_name": str(session_name or ""),
            })

        return _emit

    def set_sandbox_enabled(self, enabled: bool) -> Dict[str, Any]:
        """沙盒开关：是否允许访问工作目录之外的文件（写 config，CLI/GUI 双端热生效）。"""
        try:
            from config import save_config_value

            save_config_value("allow_outside_workspace", bool(enabled))
            return {"ok": True, "allow_outside_workspace": bool(enabled)}
        except Exception as e:
            logger.exception("set_sandbox_enabled 失败")
            return {"ok": False, "msg": str(e)}

    def get_system_settings(self) -> Dict[str, Any]:
        """返回可扩展的系统级设置快照；不得包含凭据。"""
        from core.command_shell import get_system_settings

        return get_system_settings(active_model_id=self._effective_active_model_id())

    def _effective_active_model_id(self) -> str:
        try:
            active = get_active_model(getattr(self, "_current_session", None)) or {}
            return str(active.get("id") or "")
        except Exception:
            logger.debug("解析会话生效模型失败，回退全局默认", exc_info=True)
            return ""

    def set_command_shell(self, shell_id: str) -> Dict[str, Any]:
        """设置命令默认解释器；不可用解释器必须显式拒绝，不做静默降级。"""
        from core.command_shell import CommandShellError, set_selected_command_shell

        try:
            settings = set_selected_command_shell(shell_id)
            return {"ok": True, "settings": settings, "msg": "默认命令解释器已更新"}
        except (CommandShellError, ValueError) as error:
            return {"ok": False, "msg": str(error), "settings": self.get_system_settings()}

    def set_ai_output_dir(self, path: str) -> Dict[str, Any]:
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

    def set_context_watermark_base(self, value: int) -> Dict[str, Any]:
        """设置上下文水位基准（tokens）；0/None 表示不设基准、跟随真实窗口。

        显式非法值（非整数 / 越界）必须拒绝且不写盘。
        """
        from core.context_watermark import WatermarkBaseError, save_watermark_base

        try:
            save_watermark_base(value)
            return {
                "ok": True,
                "settings": self.get_system_settings(),
                "msg": "上下文水位基准已更新",
            }
        except (WatermarkBaseError, ValueError) as error:
            return {
                "ok": False,
                "msg": str(error),
                "settings": self.get_system_settings(),
            }

    def set_subagent_enabled(self, value: bool) -> Dict[str, Any]:
        """设置全局子代理开关；非法值显式拒绝且不写盘。"""
        from core.subagent_settings import SubagentSettingError, set_subagent_enabled

        try:
            effective = set_subagent_enabled(value)
            return {
                "ok": True,
                "settings": self.get_system_settings(),
                "msg": f"子代理开关已{'开启' if effective else '关闭'}",
            }
        except (SubagentSettingError, ValueError) as error:
            return {
                "ok": False,
                "msg": str(error),
                "settings": self.get_system_settings(),
            }

    def run_doctor(self, skip_network: bool = False) -> Dict[str, Any]:
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

    def get_growth_settings(self) -> Dict[str, Any]:
        """返回成长设置 + 自我演化快照（含变更日志、自我模型、用户档案、经验摘要）。"""
        from core import identity_store

        try:
            snapshot = dict(identity_store.get_growth_snapshot() or {})
            snapshot["changelog"] = identity_store.get_changelog(50)
            snapshot["self_model"] = identity_store.get_self_model()
            snapshot["profile"] = identity_store.get_user_profile()
            snapshot["lessons"] = [
                {
                    "slug": str(item.get("slug") or ""),
                    "kind": str(item.get("kind") or ""),
                    "hits": int(item.get("hits") or 0),
                    "last_hit_at": str(item.get("last_hit_at") or ""),
                }
                for item in (identity_store.iter_lessons() or [])[:100]
            ]
            return {
                "ok": True,
                "settings": identity_store.get_growth_settings(),
                "snapshot": snapshot,
            }
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("get_growth_settings 失败")
            return {"ok": False, "msg": str(e)}

    def set_growth_settings(self, enabled=None, learning_enabled=None, user_key=None) -> Dict[str, Any]:
        """保存成长设置；只提交非 None 字段，user_key 传空串表示恢复自动解析。"""
        from core import identity_store

        try:
            patch: Dict[str, Any] = {}
            if enabled is not None:
                patch["enabled"] = enabled
            if learning_enabled is not None:
                patch["learning_enabled"] = learning_enabled
            if user_key is not None:
                patch["user_key"] = user_key
            settings = identity_store.save_growth_settings(**patch)
            return {"ok": True, "settings": settings}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("set_growth_settings 失败")
            return {"ok": False, "msg": str(e)}

    def reset_growth(self, scope: str = "all") -> Dict[str, Any]:
        """重置成长数据；scope 取 self / user / all。"""
        from core import identity_store

        try:
            scope_text = str(scope or "all").strip().lower()
            if scope_text == "self":
                identity_store.reset_self()
            elif scope_text == "user":
                identity_store.reset_user()
            elif scope_text == "all":
                identity_store.reset_all()
            else:
                return {"ok": False, "msg": "unknown scope"}
            return {"ok": True, "scope": scope_text}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("reset_growth 失败")
            return {"ok": False, "msg": str(e)}

    def export_growth_snapshot(self) -> Dict[str, Any]:
        """导出自我演化快照（仅内存返回，不写盘）。"""
        from core import identity_store

        try:
            snapshot = dict(identity_store.get_growth_snapshot() or {})
            snapshot["changelog"] = identity_store.get_changelog(200)
            snapshot["self_model"] = identity_store.get_self_model()
            snapshot["profile"] = identity_store.get_user_profile()
            snapshot["lessons"] = [
                {
                    "slug": str(item.get("slug") or ""),
                    "kind": str(item.get("kind") or ""),
                    "hits": int(item.get("hits") or 0),
                    "last_hit_at": str(item.get("last_hit_at") or ""),
                }
                for item in (identity_store.iter_lessons() or [])[:100]
            ]
            return {"ok": True, "snapshot": snapshot}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("export_growth_snapshot 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 决策模型（JEV）设置桥（Task 2：委托 core.decision_service，含异常兜底）
    # ------------------------------------------------------------------
    def get_decision_settings(self) -> Dict[str, Any]:
        """返回决策模型设置快照（配置 + 运行时状态）。"""
        from core import decision_service

        try:
            snapshot = decision_service.get_decision_settings_snapshot()
            return {
                "ok": True,
                "settings": snapshot.get("config") or {},
                "runtime": snapshot.get("runtime") or {},
            }
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("get_decision_settings 失败")
            return {"ok": False, "msg": str(e), "settings": {}, "runtime": {}}

    def set_decision_settings(self, patch: dict) -> Dict[str, Any]:
        """校验并保存决策模型设置；非法值拒绝写盘并返回中文原因，成功返回新快照。"""
        from core import decision_service

        try:
            settings = decision_service.save_decision_config(patch)
            decision_enabled = bool(settings.get("enabled"))
            if not decision_enabled and getattr(self, "_current_permission_mode", "confirm") == "auto_decision":
                self._current_permission_mode = "confirm"
                app_context.set_permission_mode("confirm")
                try:
                    from config import save_config_value

                    save_config_value("permission_mode", "confirm")
                except Exception:
                    logger.warning("关闭决策模型后重置全局权限模式失败", exc_info=True)
            if hasattr(self, "emit_event"):
                try:
                    self.emit_event("decision_settings_changed", {
                        "enabled": decision_enabled,
                        "permission_mode": self._effective_permission_mode(
                            getattr(self, "_current_session", "")
                        ),
                    })
                except Exception:
                    logger.debug("推送 decision_settings_changed 失败", exc_info=True)
            return {"ok": True, "msg": "决策设置已保存", "settings": settings}
        except ValueError as error:
            current = decision_service.load_decision_config()
            return {"ok": False, "msg": str(error), "settings": current}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("set_decision_settings 失败")
            return {"ok": False, "msg": str(e)}

    def _decision_scene_rows(self, cfg: dict) -> list:
        from core import decision_service

        return decision_service.build_decision_scene_rows(cfg)

    def get_decision_scenes(self) -> Dict[str, Any]:
        """返回业务场景的二态快照（设置页「业务场景」区块用）。"""
        from core import decision_service

        try:
            cfg = decision_service.load_decision_config()
            rows = self._decision_scene_rows(cfg)
            return {
                "ok": True,
                "scenes": rows,
                "enabled": bool(cfg.get("enabled")),
                "msg": "",
            }
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("get_decision_scenes 失败")
            return {"ok": False, "msg": str(e), "scenes": [], "enabled": False}

    def set_decision_scene(self, scene: str, mode: str, threshold=None) -> Dict[str, Any]:
        from core import decision_service

        return decision_service.update_decision_scene(scene, mode, threshold)

    def probe_decision(self) -> Dict[str, Any]:
        """逐 Key 各跑一次无害探针，返回分类结果与目标任务模型。"""
        from core import decision_service

        try:
            result = decision_service.probe_decision()
            return {
                "ok": bool(result.get("ok", True)),
                "results": result.get("results") or [],
                "model_id": result.get("model_id") or "",
                "msg": result.get("msg") or "",
            }
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("probe_decision 失败")
            return {"ok": False, "results": [], "model_id": "", "msg": str(e)}

    def list_decision_models(self) -> Dict[str, Any]:
        """从上游拉取 decision 类型模型列表（设置页「从上游拉取」下拉用）。"""
        from core import decision_service

        try:
            result = decision_service.list_decision_models()
            return {
                "ok": bool(result.get("ok")),
                "models": result.get("models") or [],
                "msg": result.get("msg") or "",
            }
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("list_decision_models 失败")
            return {"ok": False, "models": [], "msg": str(e)}

    def list_decision_providers(self) -> Dict[str, Any]:
        """列出「决策模型」可选的调用通道（不含 Key 内容；设置页通道下拉用）。"""
        from core import decision_service

        try:
            result = decision_service.list_decision_providers()
            return {
                "ok": bool(result.get("ok")),
                "providers": result.get("providers") or [],
            }
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("list_decision_providers 失败")
            return {"ok": False, "providers": [], "msg": str(e)}

    def get_decision_audit_summary(
        self, limit: int = 20, session_id=None, session_only: bool = False
    ) -> Dict[str, Any]:
        """决策审计汇总：今日计数 + 最近 N 条记录。

        ``session_only=True`` 且 ``session_id`` 非空时，``today_*`` 与 ``recent``
        也按该会话过滤（会话头部「决策」面板口径，避免展示别的会话的记录）；
        否则返回全局汇总，设置页面板依赖全局数据、不能出现缺口。
        ``has_business_records`` 只在传了非空 ``session_id`` 时按会话判定。
        """
        from core import decision_service

        try:
            scope_session = str(session_id or "").strip()
            if scope_session:
                result = decision_service.get_decision_audit_summary(
                    limit, scope_session, bool(session_only)
                )
            else:
                # 空会话不追加参数：下游默认 None = 全局口径，也保持旧调用签名兼容
                result = decision_service.get_decision_audit_summary(limit)
            return result if isinstance(result, dict) else {"ok": False, "msg": "汇总返回异常"}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("get_decision_audit_summary 失败")
            return {"ok": False, "msg": str(e)}

    def get_decision_record_detail(self, trace_id: str) -> Dict[str, Any]:
        """按 trace_id 取单条决策记录详情（发给 JEV 的请求 + 上游返回的答案）。

        面板点开一条记录时调用：只读审计文件，找不到返回 ok=false，不抛错。
        """
        from core import decision_service

        try:
            result = decision_service.get_decision_record_detail(trace_id)
            return result if isinstance(result, dict) else {"ok": False, "msg": "详情返回异常"}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("get_decision_record_detail 失败")
            return {"ok": False, "msg": str(e)}

    def open_decision_audit_dir(self) -> Dict[str, Any]:
        """在系统文件管理器中打开决策审计目录（缺失时先创建）。"""
        from core import decision_service

        try:
            directory = decision_service._audit_dir()
            os.makedirs(directory, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(directory)  # type: ignore[attr-defined]
            else:
                import subprocess

                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, directory])
            return {"ok": True, "path": directory}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("open_decision_audit_dir 失败")
            return {"ok": False, "msg": str(e)}

    def get_decision_audit_tail(self, limit: int = 20) -> Dict[str, Any]:
        """读取决策审计目录最新文件的尾部若干行；目录/文件缺失时返回空列表。"""
        from core import decision_service

        try:
            try:
                count = max(1, int(limit))
            except (TypeError, ValueError):
                count = 20
            directory = decision_service._audit_dir()
            if not os.path.isdir(directory):
                return {"ok": True, "lines": []}
            names = sorted(
                name for name in os.listdir(directory) if name.endswith(".jsonl")
            )
            if not names:
                return {"ok": True, "lines": []}
            lines: list = []
            with open(os.path.join(directory, names[-1]), "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        lines.append(line)
            return {"ok": True, "lines": lines[-count:]}
        except Exception as e:  # pragma: no cover - 防御性兜底
            logger.exception("get_decision_audit_tail 失败")
            return {"ok": False, "msg": str(e), "lines": []}

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


    def _browser_control_preflight(self) -> None:
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
        try:
            from core.computer.runtime import refresh_computer_runtime_hotkey

            refresh_computer_runtime_hotkey()
        except Exception:
            logger.exception("同步全局紧急停止热键失败")

    def _has_active_computer_run(self) -> bool:
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
        else:
            # 恢复条件①（core/computer/stop_notice.py 注释）：用户在设置里重新
            # 开启电脑操控，等于明确要求恢复，解除"用户已停止"抑制与未投递通知；
            # 否则用户只能靠发一句含恢复关键词的消息才能再让模型操作电脑。
            from core.computer import stop_notice

            stop_notice.deactivate_user_stop("settings_reenabled")
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

    def _ensure_computer_event_bridge(self):
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
        """用户紧急停止当前 run；固定停止顺序由 controller.stop_for_user 执行（设计 15.2）。

        用 stop_for_user 而非 stop：用户点停止只应结束电脑操控 run，不应置位
        回合 cancel_event 把整个对话回合掐掉（否则模型收不到"用户已停止"）。
        """
        return self._run_computer_mutation(
            lambda controller: controller.stop_for_user(str(run_id or ""), "user_stop")
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

    # ------------------------------------------------------------------
    # Playwright 按需组件（core/components.py）：安装包不再内置 Playwright，
    # 设置页「浏览器操控组件」卡片按需下载/取消；进度事件经
    # _on_playwright_component_event → emit_event 入 _js_queue，由
    # gui-js-dispatcher 单消费者线程 evaluate_js 投递（下载线程绝不碰 WebView）
    # ------------------------------------------------------------------

    def _empty_playwright_component_status(self) -> Dict[str, Any]:
        """组件状态兜底快照：core.components 不可用时保持前端契约形状不报错。"""
        return {
            "installed": False,
            "version": "",
            "downloading": False,
            "percent": 0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "error": "",
            # digest_verified/warning：官方 SHA256 缺失时供前端提示风险
            "digest_verified": False,
            "warning": "",
            "required_version": "",
            "version_compatible": None,
            "outdated": False,
            "version_warning": "",
            "stage": "",
            "dir": "",
        }

    def get_playwright_component_status(self) -> Dict[str, Any]:
        try:
            from core import components

            return {"ok": True, "component": components.get_status()}
        except Exception as exc:
            logger.exception("读取 Playwright 组件状态失败")
            return {
                "ok": False,
                "msg": f"读取 Playwright 组件状态失败：{exc}",
                "component": self._empty_playwright_component_status(),
            }

    def download_playwright_component(self) -> Dict[str, Any]:
        try:
            from core import components

            started = components.start_download(
                on_event=self._on_playwright_component_event
            )
            status = components.get_status()
            if not started:
                return {
                    "ok": False,
                    "msg": "组件已在下载中，请等待当前下载完成或先取消",
                    "component": status,
                }
            return {"ok": True, "msg": "已开始下载 Playwright 组件", "component": status}
        except Exception as exc:
            logger.exception("启动 Playwright 组件下载失败")
            return {
                "ok": False,
                "msg": f"启动组件下载失败：{exc}",
                "component": self._empty_playwright_component_status(),
            }

    def cancel_playwright_component_download(self) -> Dict[str, Any]:
        """取消进行中的组件下载（工作线程在下一块下载时退出并清理临时文件）。"""
        try:
            from core import components

            components.cancel_download()
            return {"ok": True, "msg": "已请求取消组件下载", "component": components.get_status()}
        except Exception as exc:
            logger.exception("取消 Playwright 组件下载失败")
            return {
                "ok": False,
                "msg": f"取消组件下载失败：{exc}",
                "component": self._empty_playwright_component_status(),
            }

    def _on_playwright_component_event(self, event: dict) -> None:
        try:
            raw = event if isinstance(event, dict) else {}

            def _as_int(value) -> int:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0

            payload: Dict[str, Any] = {
                "stage": str(raw.get("stage") or ""),
                "percent": _as_int(raw.get("percent")),
                "downloaded_bytes": _as_int(raw.get("downloaded_bytes")),
                "total_bytes": _as_int(raw.get("total_bytes")),
                "error": str(raw.get("error") or ""),
                "warning": str(raw.get("warning") or ""),
            }
        except Exception:
            logger.warning("规整 Playwright 组件进度事件失败", exc_info=True)
            payload = {
                "stage": "",
                "percent": 0,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "error": "",
                "warning": "",
            }
        # installed/version 不能从进度事件推断，统一按当前磁盘/状态快照补齐；
        # 读取失败时保持 False/空串，前端据此只更新进度不做安装态跳变
        try:
            from core import components

            status = components.get_status()
            payload["installed"] = bool(status.get("installed"))
            payload["version"] = str(status.get("version") or "")
        except Exception:
            logger.warning("读取组件状态失败（进度事件缺少 installed/version）", exc_info=True)
            payload["installed"] = False
            payload["version"] = ""
        try:
            self.emit_event("playwright_component_progress", payload)
        except Exception:
            logger.exception("推送 Playwright 组件进度事件失败")

    def _empty_component_status(self) -> Dict[str, Any]:
        return self._empty_playwright_component_status()

    def get_ffmpeg_component_status(self) -> Dict[str, Any]:
        """返回视频转换组件状态快照（设置页「背景图转换」卡片组件的唯一真值源）。"""
        try:
            from core import components

            return {"ok": True, "component": components.get_component_status("ffmpeg")}
        except Exception as exc:
            logger.exception("读取视频转换组件状态失败")
            return {
                "ok": False,
                "msg": f"读取视频转换组件状态失败：{exc}",
                "component": self._empty_component_status(),
            }

    def download_ffmpeg_component(self) -> Dict[str, Any]:
        try:
            from core import components

            started = components.start_component_download(
                "ffmpeg", on_event=self._on_ffmpeg_component_event
            )
            status = components.get_component_status("ffmpeg")
            if not started:
                return {
                    "ok": False,
                    "msg": "组件已在下载中，请等待当前下载完成或先取消",
                    "component": status,
                }
            return {"ok": True, "msg": "已开始下载视频转换组件", "component": status}
        except Exception as exc:
            logger.exception("启动视频转换组件下载失败")
            return {
                "ok": False,
                "msg": f"启动组件下载失败：{exc}",
                "component": self._empty_component_status(),
            }

    def cancel_ffmpeg_component_download(self) -> Dict[str, Any]:
        """取消进行中的视频转换组件下载（工作线程在下一块下载时退出并清理临时文件）。"""
        try:
            from core import components

            components.cancel_component_download("ffmpeg")
            return {
                "ok": True,
                "msg": "已请求取消组件下载",
                "component": components.get_component_status("ffmpeg"),
            }
        except Exception as exc:
            logger.exception("取消视频转换组件下载失败")
            return {
                "ok": False,
                "msg": f"取消组件下载失败：{exc}",
                "component": self._empty_component_status(),
            }

    def _on_ffmpeg_component_event(self, event: dict) -> None:
        try:
            raw = event if isinstance(event, dict) else {}

            def _as_int(value) -> int:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0

            payload: Dict[str, Any] = {
                "stage": str(raw.get("stage") or ""),
                "percent": _as_int(raw.get("percent")),
                "downloaded_bytes": _as_int(raw.get("downloaded_bytes")),
                "total_bytes": _as_int(raw.get("total_bytes")),
                "error": str(raw.get("error") or ""),
                "warning": str(raw.get("warning") or ""),
            }
        except Exception:
            logger.warning("规整视频转换组件进度事件失败", exc_info=True)
            payload = {
                "stage": "",
                "percent": 0,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "error": "",
                "warning": "",
            }
        # installed/version 不能从进度事件推断，统一按当前磁盘/状态快照补齐；
        # 读取失败时保持 False/空串，前端据此只更新进度、不做安装态跳变
        try:
            from core import components

            status = components.get_component_status("ffmpeg")
            payload["installed"] = bool(status.get("installed"))
            payload["version"] = str(status.get("version") or "")
        except Exception:
            logger.warning("读取组件状态失败（进度事件缺少 installed/version）", exc_info=True)
            payload["installed"] = False
            payload["version"] = ""
        try:
            self.emit_event("ffmpeg_component_progress", payload)
        except Exception:
            logger.exception("推送视频转换组件进度事件失败")

    def get_search_component_status(self) -> Dict[str, Any]:
        """返回网页搜索组件状态快照（设置页「网页搜索」卡片组件的唯一真值源）。"""
        try:
            from core import components

            return {"ok": True, "component": components.get_component_status("search")}
        except Exception as exc:
            logger.exception("读取网页搜索组件状态失败")
            return {
                "ok": False,
                "msg": f"读取网页搜索组件状态失败：{exc}",
                "component": self._empty_component_status(),
            }

    def download_search_component(self) -> Dict[str, Any]:
        try:
            from core import components

            started = components.start_component_download(
                "search", on_event=self._on_search_component_event
            )
            status = components.get_component_status("search")
            if not started:
                return {
                    "ok": False,
                    "msg": "组件已在下载中，请等待当前下载完成或先取消",
                    "component": status,
                }
            return {"ok": True, "msg": "已开始下载网页搜索组件", "component": status}
        except Exception as exc:
            logger.exception("启动网页搜索组件下载失败")
            return {
                "ok": False,
                "msg": f"启动组件下载失败：{exc}",
                "component": self._empty_component_status(),
            }

    def cancel_search_component_download(self) -> Dict[str, Any]:
        """取消进行中的网页搜索组件下载（工作线程在下一块下载时退出并清理临时文件）。"""
        try:
            from core import components

            components.cancel_component_download("search")
            return {
                "ok": True,
                "msg": "已请求取消组件下载",
                "component": components.get_component_status("search"),
            }
        except Exception as exc:
            logger.exception("取消网页搜索组件下载失败")
            return {
                "ok": False,
                "msg": f"取消组件下载失败：{exc}",
                "component": self._empty_component_status(),
            }

    def _on_search_component_event(self, event: dict) -> None:
        try:
            raw = event if isinstance(event, dict) else {}

            def _as_int(value) -> int:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0

            payload: Dict[str, Any] = {
                "stage": str(raw.get("stage") or ""),
                "percent": _as_int(raw.get("percent")),
                "downloaded_bytes": _as_int(raw.get("downloaded_bytes")),
                "total_bytes": _as_int(raw.get("total_bytes")),
                "error": str(raw.get("error") or ""),
                "warning": str(raw.get("warning") or ""),
            }
        except Exception:
            logger.warning("规整网页搜索组件进度事件失败", exc_info=True)
            payload = {
                "stage": "",
                "percent": 0,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "error": "",
                "warning": "",
            }
        # installed/version 不能从进度事件推断，按当前磁盘/状态快照补齐；
        # 读取失败时保持 False/空串，前端据此只更新进度、不做安装态跳变
        try:
            from core import components

            status = components.get_component_status("search")
            payload["installed"] = bool(status.get("installed"))
            payload["version"] = str(status.get("version") or "")
        except Exception:
            logger.warning("读取组件状态失败（进度事件缺少 installed/version）", exc_info=True)
            payload["installed"] = False
            payload["version"] = ""
        try:
            self.emit_event("search_component_progress", payload)
        except Exception:
            logger.exception("推送网页搜索组件进度事件失败")

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
            "summary": describe_permission_request(
                request.function_name, dict(request.arguments or {})
            ),
            "source": request.source,
            "options": options,
            "permission_options": permission_options,
            # 按回合撤销：undo_turn 权限卡附带服务预检的影响摘要（只读）
            "undo_impact": self._undo_turn_impact_for_event(request),
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
        # 归属会话：提示只进任务自己的会话，避免串到用户正在看的其他窗口
        # （空串与 post_notice 一致：前端视作缺省，落当前会话）
        notice_session = str(label.get("session_name") or "")
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
                    "session_name": notice_session,
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
                "session_name": notice_session,
            })
        except Exception:
            logger.exception("自动化权限自动拒绝失败")

    def _dismiss_permission_prompt(self):
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

    def _ensure_lan_server(self):
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

    def _ensure_mcp_server(self):
        if os.environ.get("YCODE_TOOL_PROFILE", "").strip().lower() == "server":
            return None
        if getattr(self, "_mcp_server", None) is not None:
            return self._mcp_server
        lock = getattr(self, "_mcp_server_lock", None)
        if lock is None:
            # getattr 兜底：测试里的裸实例（__new__ 构造）不经过 __init__，
            # 缺状态时就地补齐，避免后台启动线程里抛 AttributeError
            lock = threading.Lock()
            self._mcp_server_lock = lock
        with lock:
            if getattr(self, "_mcp_server", None) is not None:
                return self._mcp_server
            try:
                data = config._load()
                if not data.get(
                    config.REMOTE_MCP_ENABLED_KEY, config.REMOTE_MCP_ENABLED_DEFAULT
                ):
                    return None
                # MCP 复用 LAN 远程控制的配对 token：LAN 服务起不来就没有可校验的
                # 凭据，此时直接放弃启动（fail-closed，绝不裸奔成无鉴权入口）。
                lan = self._ensure_lan_server()
                if lan is None:
                    logger.debug("局域网 MCP 服务未启动：LAN 远程控制服务不可用")
                    return None

                def _token_provider():
                    return getattr(lan, "_token", b"") or b""

                try:
                    port = int(data.get(config.REMOTE_MCP_PORT_KEY, config.REMOTE_MCP_PORT_DEFAULT))
                except (TypeError, ValueError):
                    port = config.REMOTE_MCP_PORT_DEFAULT
                from core.mcp_server import McpServer

                srv = McpServer(bridge=self, port=port, token_provider=_token_provider)
                info = srv.start()
                if not info.get("ok"):
                    logger.warning("局域网 MCP 服务启动失败: %s", info.get("msg") or info)
                    return None
                self._mcp_server = srv
                logger.info("局域网 MCP 服务已启动: %s", info.get("url"))
                return srv
            except Exception:
                logger.exception("局域网 MCP 服务启动失败")
                return None

    def _ensure_feishu_service(self):
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

    @staticmethod
    def _cloud_relay_config_signature(data: Dict[str, Any]) -> tuple:
        return (
            bool(data.get(config.CLOUD_RELAY_ENABLED_KEY, False)),
            str(data.get(config.CLOUD_RELAY_URL_KEY) or "").strip(),
            str(data.get(config.CLOUD_RELAY_DEVICE_ID_KEY) or "").strip(),
            str(data.get(config.CLOUD_RELAY_TOKEN_KEY) or "").strip(),
        )

    @staticmethod
    def _safe_config_mtime_ns() -> Optional[int]:
        try:
            cfg_file = getattr(config, "_CONFIG_FILE", "")
            return os.stat(cfg_file).st_mtime_ns if cfg_file else None
        except OSError:
            return None

    def _record_cloud_relay_config_snapshot(self, data: Optional[Dict[str, Any]] = None) -> None:
        try:
            loaded = data if data is not None else config._load()
            self._cloud_relay_cfg_sig = self._cloud_relay_config_signature(loaded)
            self._cloud_relay_cfg_mtime_ns = self._safe_config_mtime_ns()
        except Exception:
            pass

    def _sync_cloud_relay_from_disk(self) -> None:
        if not getattr(self, "_cloud_relay_watcher_active", False):
            return
        mtime_ns = self._safe_config_mtime_ns()
        if mtime_ns is None or mtime_ns == getattr(self, "_cloud_relay_cfg_mtime_ns", None):
            return
        lock = getattr(self, "_cloud_relay_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._cloud_relay_lock = lock
        with lock:
            if mtime_ns == getattr(self, "_cloud_relay_cfg_mtime_ns", None):
                return
            try:
                data = config._load()
                sig = self._cloud_relay_config_signature(data)
            except Exception:
                return
            self._cloud_relay_cfg_mtime_ns = mtime_ns
            if sig == getattr(self, "_cloud_relay_cfg_sig", None):
                return
            self._cloud_relay_cfg_sig = sig
            current = getattr(self, "_cloud_relay_service", None)
            if current is not None:
                try:
                    current.stop()
                except Exception:
                    pass
                self._cloud_relay_service = None
            if not sig[0]:
                try:
                    from core.cloud_relay.service import write_runtime_status

                    write_runtime_status({"state": "disabled", "connected": False})
                except Exception:
                    pass
                return
            try:
                from core.cloud_relay.service import CloudRelayService

                try:
                    service = CloudRelayService(bridge=self, persist_status=True)
                except TypeError:
                    service = CloudRelayService(bridge=self)
                if service.start():
                    self._cloud_relay_service = service
            except Exception:
                logger.exception("云中转热重载失败")

    def _cloud_relay_watcher_loop(self) -> None:
        while not getattr(self, "_shutting_down", False):
            time.sleep(2.0)
            if getattr(self, "_shutting_down", False):
                return
            self._sync_cloud_relay_from_disk()

    def _ensure_cloud_relay(self):
        if getattr(self, "_cloud_relay_service", None) is not None:
            return self._cloud_relay_service
        lock = getattr(self, "_cloud_relay_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._cloud_relay_lock = lock
        with lock:
            if getattr(self, "_cloud_relay_service", None) is not None:
                return self._cloud_relay_service
            try:
                from core.cloud_relay.service import CloudRelayService

                self._record_cloud_relay_config_snapshot()
                try:
                    service = CloudRelayService(bridge=self, persist_status=True)
                except TypeError:
                    service = CloudRelayService(bridge=self)
                if not service.start():
                    return None
                self._cloud_relay_service = service
                return service
            except Exception:
                logger.exception("云中转启动失败")
                return None

    def get_cloud_relay_settings(self) -> Dict[str, Any]:
        """给设置卡片一次性拿全：开关、网关地址、设备号、密钥是否已配、连接状态。"""
        try:
            self._sync_cloud_relay_from_disk()
            data = config._load()
            service = getattr(self, "_cloud_relay_service", None)
            detail = service.status() if service is not None else {"state": "disabled"}
            return {
                "ok": True,
                "settings": {
                    "enabled": bool(data.get(config.CLOUD_RELAY_ENABLED_KEY, False)),
                    "url": data.get(config.CLOUD_RELAY_URL_KEY) or "",
                    "device_id": data.get(config.CLOUD_RELAY_DEVICE_ID_KEY) or "",
                    "has_token": bool(str(data.get(config.CLOUD_RELAY_TOKEN_KEY) or "").strip()),
                    "state": detail.get("state", "disabled"),
                    "connected": bool(detail.get("connected", False)),
                    "generation": int(detail.get("generation") or 0),
                    "queued": int(detail.get("queued") or 0),
                    "dropped": int(detail.get("dropped") or 0),
                    "lost": int(detail.get("lost") or 0),
                },
            }
        except Exception as error:
            logger.exception("读取云中转设置失败")
            return {"ok": False, "msg": str(error)}

    def save_cloud_relay_settings(self, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        try:
            payload = payload or {}
            config.save_config_value(config.CLOUD_RELAY_ENABLED_KEY, bool(payload.get("enabled")))
            config.save_config_value(config.CLOUD_RELAY_URL_KEY, str(payload.get("url") or ""))
            config.save_config_value(
                config.CLOUD_RELAY_DEVICE_ID_KEY,
                str(payload.get("device_id") or ""),
            )
            token = str(payload.get("token") or "").strip()
            if token:
                from core.credentials import protect_secret

                config.save_config_value(config.CLOUD_RELAY_TOKEN_KEY, protect_secret(token))
            self._record_cloud_relay_config_snapshot()
            current = getattr(self, "_cloud_relay_service", None)
            if current is not None:
                current.stop()
                self._cloud_relay_service = None
            if bool(payload.get("enabled")) and not token and not str(
                config._load().get(config.CLOUD_RELAY_TOKEN_KEY) or ""
            ).strip():
                return self.get_cloud_relay_settings()
            self._ensure_cloud_relay()
            return self.get_cloud_relay_settings()
        except Exception as error:
            logger.exception("保存云中转设置失败")
            return {"ok": False, "msg": str(error)}

    def cloud_relay_connect(self) -> Dict[str, Any]:
        """设置卡片「重连」按钮：丢掉旧连接按当前配置重拨，配置不变。"""
        try:
            current = getattr(self, "_cloud_relay_service", None)
            if current is not None:
                current.stop()
                self._cloud_relay_service = None
            self._record_cloud_relay_config_snapshot()
            self._ensure_cloud_relay()
            return self.get_cloud_relay_settings()
        except Exception as error:
            logger.exception("云中转重连失败")
            return {"ok": False, "msg": str(error)}

    def cloud_relay_disconnect(self) -> Dict[str, Any]:
        """设置卡片「断开」按钮：只断当前连接，不清配置（下次启动还会连）。"""
        try:
            self._record_cloud_relay_config_snapshot()
            current = getattr(self, "_cloud_relay_service", None)
            if current is not None:
                current.stop()
                self._cloud_relay_service = None
            return self.get_cloud_relay_settings()
        except Exception as error:
            logger.exception("云中转断开失败")
            return {"ok": False, "msg": str(error)}

    def get_remote_bind_qr(self, force_new: bool = False, mobile: bool = False) -> Dict[str, Any]:
        try:
            self._sync_cloud_relay_from_disk()
            fallback: Dict[str, str] = {}
            public = self._cloud_relay_pair_url(mobile=bool(mobile), reason=fallback)
            if public is not None:
                from core.lan_server import build_pair_payload

                return build_pair_payload(public["url"], public["token"], public["expires_in"])
            srv = self._ensure_lan_server()
            if srv is None:
                return {"ok": False, "msg": "远程服务启动失败"}
            payload = srv.get_bind_info(
                force_new=bool(force_new), path="/m/" if mobile else "/"
            )
            notice = self._relay_fallback_notice(fallback)
            if notice:
                payload["relay_notice"] = notice
            return payload
        except Exception as e:
            logger.exception("get_remote_bind_qr 失败")
            return {"ok": False, "msg": str(e)}

    def _relay_fallback_notice(self, reason: Dict[str, str]) -> str:
        try:
            if not bool(config._load().get(config.CLOUD_RELAY_ENABLED_KEY, False)):
                return ""
            return str(reason.get("message") or "")
        except Exception:
            return ""

    def _cloud_relay_pair_url(
        self, mobile: bool, reason: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """云中转公网二维码；不可用时返回 None（内部已兜住所有异常）。"""
        try:
            from core.cloud_relay.portal import build_pair_url

            data = config._load()
            ttl = int(
                data.get(config.REMOTE_TOKEN_TTL_KEY, config.REMOTE_TOKEN_TTL_DEFAULT) or 0
            )
            return build_pair_url(self, mobile=mobile, ttl_seconds=ttl, reason=reason)
        except Exception:
            logger.debug("云中转公网二维码不可用，回落局域网", exc_info=True)
            return None

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

    def edit_user_message(self, session_name: Optional[str], msg_id: Any, new_text: str, interrupt: bool = True) -> Dict[str, Any]:
        try:
            if self._shutting_down:
                return {"ok": False, "msg": "应用正在退出"}
            clean_text = str(new_text or "").strip()
            try:
                target_msg_id = int(msg_id)
            except (TypeError, ValueError):
                return {"ok": False, "msg": "无效的消息编号"}
            target_session = session_name or self._current_session
            if not target_session:
                return {"ok": False, "msg": "会话不存在"}
            if self._is_turn_active(target_session):
                if not interrupt:
                    return {"ok": False, "msg": "会话正在生成，请先停止或等待完成"}
                with self._turn_start_lock:
                    if self._message_queues.pop(target_session, None):
                        self._emit_message_queue_snapshot(target_session)
                stopped = self.stop_current_turn(target_session)
                if not stopped.get("ok"):
                    return {
                        "ok": False,
                        "msg": "停止当前回合失败: " + str(stopped.get("msg") or "未知错误"),
                    }
                # 锁外 join：worker 的 finally 会取 _turn_start_lock 做收尾/补发，
                # 持锁等待会直接死锁。超时上限 10s——工具卡在长命令里时取消要等
                # 它自己返回，宁可提示用户稍后重试，也不能无限挂住桥调用
                worker = getattr(self, "_active_turns", {}).get(target_session)
                if worker is not None:
                    try:
                        worker.join(10.0)
                    except Exception:
                        logger.debug("等待回合线程退出失败", exc_info=True)
                if self._is_turn_active(target_session):
                    return {"ok": False, "msg": "停止当前回合超时，请稍后重试"}
            with self._turn_start_lock:
                # 角色校验必须在删除之前：rewind 会连同该条一起删掉，
                # 先删后校验会把「误点助手消息」变成一次真实的数据丢失
                role = self._memory.get_message_role(target_session, target_msg_id)
                if role is None:
                    return {"ok": False, "msg": "目标消息不存在"}
                if role != "user":
                    return {"ok": False, "msg": "只能编辑用户消息"}
                rewound = self._memory.rewind_to_message(target_session, target_msg_id)
                if rewound is None:
                    return {"ok": False, "msg": "目标消息不存在"}
                if self._message_queues.pop(target_session, None):
                    self._emit_message_queue_snapshot(target_session)
            return {
                "ok": True,
                "removed": rewound.get("removed", 0),
                "session_name": target_session,
            }
        except Exception as exc:
            logger.exception("编辑用户消息失败")
            return {"ok": False, "msg": f"编辑失败: {exc}"}

    def list_slash_commands(self) -> Dict[str, Any]:
        try:
            from core.slash_commands.specs import register_builtin_commands
            from core.slash_commands.registry import all_specs
            register_builtin_commands()
            return {
                "ok": True,
                "commands": [
                    {
                        "name": spec.name,
                        "usage": spec.usage,
                        "summary": spec.summary,
                        "kind": spec.kind,
                    }
                    for spec in all_specs()
                ],
            }
        except Exception as exc:
            # 与桥内其他方法保持一致：异常不穿透，交给前端按 ok=False 降级
            logger.exception("读取斜杠命令清单失败")
            return {"ok": False, "msg": str(exc)}

    def _manual_compress(self, session_name, args=""):
        try:
            active_model = get_active_model(session_name) or {}
            api_model = str(active_model.get("litellm_model") or "")
            api_key = str(active_model.get("api_key") or "")
            api_url = str(active_model.get("base_url") or "")
            if not api_model:
                return {"ok": False, "msg": "当前会话未激活任何模型，无法压缩"}
            if args:
                logger.info("/compact 收到保留指令（压缩器暂不支持自定义策略）：%s", args)

            def _run():
                try:
                    from core.memory_compressor import compress_session_memory

                    compress_session_memory(
                        self._memory,
                        session_name,
                        api_model=api_model,
                        api_key=api_key,
                        api_url=api_url,
                        # 压缩器吐的是 CLI 着色标签文案，GUI 卡片要剥离；且提示必须带
                        # 会话名，否则用户已切走的对话里的进度会串到当前对话
                        output_callback=lambda msg: self.post_notice(
                            _strip_ansi_markup(msg), session_name
                        ),
                    )
                except Exception as exc:
                    logger.exception("手动压缩失败")
                    self.post_notice("压缩失败: " + str(exc), session_name, "error")

            threading.Thread(
                target=_run,
                name=f"gui-manual-compress-{session_name}",
                daemon=True,
            ).start()
            return _CompactStartAck(ok=True)
        except Exception as exc:
            logger.exception("准备手动压缩失败")
            self.post_notice("压缩失败: " + str(exc), session_name, "error")
            return {"ok": False, "msg": str(exc)}

    def _dispatch_slash(self, clean_text, session_name, full_text, workdir="", turn_source="local", model_id=""):
        try:
            from core.slash_commands.dispatch import dispatch_command
            result = dispatch_command(
                clean_text, ctx={"compress": lambda _args: self._manual_compress(session_name)}
            )
        except Exception:
            # 命令层自身的缺陷不该让整条消息发不出去：兜底按普通消息继续，
            # 用户至少不会因为 /fix 写错参数而彻底发不出话。
            logger.exception("斜杠命令分发失败")
            return None
        if not result.handled:
            return None
        if result.kind == "prompt":
            return {"prompt": result.prompt}
        if result.kind == "error":
            self.post_notice(result.error, session_name, "error")
            return {"ok": False, "msg": result.error, "slash": True}
        if result.kind == "action":
            return self._run_slash_action(
                result.action,
                session_name,
                workdir=workdir,
                turn_source=turn_source,
                model_id=model_id,
            )
        self.post_notice(result.notice, session_name)
        return {"ok": True, "slash": True}

    def _run_slash_action(self, action, session_name, workdir="", turn_source="local", model_id=""):
        if isinstance(action.get("action"), dict):
            action = action["action"]
        kind = str(action.get("action") or "")
        if kind == "commit":
            # 两阶段提交的落地在 _slash_commit：这里不再只弹一句"即将提交"，
            # 而是真的暂存 / 提交（细节与原因见该方法注释）。
            return self._slash_commit(action, session_name)
        if kind == "forget_memory":
            # 两阶段删除的落地在 _slash_forget：这里不再只弹一句"收到删除请求"，
            # 而是真的先扫命中行、确认后才删（范围约束与原因见该方法注释）。
            return self._slash_forget(action, session_name)
        if kind == "goal_help":
            from core.slash_commands.goal_command import GOAL_USAGE

            self.post_notice(GOAL_USAGE, session_name)
            return {"ok": True, "slash": True}
        if kind == "goal_status":
            return self._goal_status(session_name)
        if kind == "goal_stop":
            return self._goal_stop(session_name)
        if kind == "goal_resume":
            return self._goal_resume(session_name)
        if kind == "goal_confirm":
            return self._goal_confirm(session_name)
        if kind == "goal_start":
            # workdir/turn_source/model_id 必须透传：草稿首条消息是 /goal 时靠前两者
            # 落库归组，靠 model_id 把用户当前选的模型继承到新会话（否则目标循环
            # 每轮回退全局默认模型）
            return self._goal_start(
                action,
                session_name,
                workdir=workdir,
                turn_source=turn_source,
                model_id=model_id,
            )
        content = str(action.get("content") or "")
        if kind == "use_skill" and content:
            skill = action.get("skill", "")
            requirement = str(action.get("requirement") or "").strip() or "按技能说明执行"
            return {"prompt": f"请严格按以下技能执行任务。\n\n# 技能：{skill}\n\n{content}\n\n任务：{requirement}"}
        prompts = {
            "spawn_subagent": f"请使用 run_subagent 工具派一个 {action.get('subagent_type','')} 子代理执行：{action.get('task','')}",
            "run_workflow": f"请运行工作流 {action.get('name','')}，参数 {action.get('args',{})}",
            "use_skill": f"请加载技能 {action.get('skill','')} 并完成：{action.get('requirement','')}",
        }
        prompt = prompts.get(kind)
        if prompt:
            return {"prompt": prompt}
        return {"ok": False, "msg": f"未知命令动作: {kind}", "slash": True}

    def _slash_commit(self, action, session_name):
        confirmed = bool(action.get("confirmed"))
        message = str(action.get("message") or "").strip()
        workdir = str(self._session_workdir(session_name) or "").strip()
        if not workdir or workdir.upper() == "GLOBAL" or not os.path.isdir(workdir):
            self.post_notice(
                "当前会话没有关联项目目录，无法定位要提交的 Git 仓库。"
                "请在项目会话里执行 /commit。",
                session_name,
                "error",
            )
            return {"ok": False, "msg": "当前会话没有关联项目目录", "slash": True}
        try:
            stage_result = self.git_stage_all(workdir)
            if not stage_result.get("ok"):
                reason = str(
                    ((stage_result.get("error") or {}).get("message")) or "未知错误"
                )
                self.post_notice(f"暂存失败：{reason}", session_name, "error")
                return {"ok": False, "msg": reason, "slash": True}
            status_result = self.git_get_status(workdir)
            snapshot = (
                status_result.get("snapshot") if status_result.get("ok") else None
            ) or stage_result.get("snapshot") or {}
            names = []
            for entry in snapshot.get("staged") or []:
                path = entry.get("path") if isinstance(entry, dict) else entry
                if path:
                    names.append(str(path))
            if not names:
                # 没有改动就不要走到 commit：core.git 的 commit 会直接抛
                # "暂存区为空，没有可提交的内容"，与其回一条错误卡片，
                # 不如明确告诉用户"没什么可提交"，这是正常的空操作。
                self.post_notice("工作区没有可提交的改动，/commit 已跳过。", session_name)
                return {"ok": True, "slash": True}
            count = len(names)
            preview = "、".join(names[:5])
            if not confirmed:
                self.post_notice(
                    f"已暂存 {count} 个文件：{preview}。"
                    "确认后执行 /commit --yes [说明] 完成提交",
                    session_name,
                )
                return {"ok": True, "slash": True, "confirm_required": True}
            title = message or f"更新 {count} 个文件"
            commit_result = self.git_commit(workdir, title=title)
            if not commit_result.get("ok"):
                reason = str(
                    ((commit_result.get("error") or {}).get("message")) or "未知错误"
                )
                self.post_notice(f"提交失败：{reason}", session_name, "error")
                return {"ok": False, "msg": reason, "slash": True}
            self.post_notice(
                f"已提交 {count} 个文件：{preview}。提交信息：{title}", session_name
            )
            return {"ok": True, "slash": True}
        except Exception as exc:
            logger.exception("/commit 执行失败")
            self.post_notice(f"/commit 执行失败：{exc}", session_name, "error")
            return {"ok": False, "msg": str(exc), "slash": True}

    def _slash_forget(self, action, session_name):
        try:
            from core import memory_kairos
            from core.slash_commands.memory_forget import (
                delete_matches,
                scan_matches,
            )

            keyword = str(action.get("keyword") or "").strip()
            confirmed = bool(action.get("confirmed"))
            log_path = memory_kairos.daily_log_path()
            index_path = memory_kairos.MEMORY_DIR / "MEMORY.md"
            if not confirmed:
                stats = scan_matches(keyword, log_path=log_path, index_path=index_path)
                samples = "；".join(stats.get("samples") or [])
                notice = (
                    f"当日日志命中 {stats['log_lines']} 行、"
                    f"记忆索引命中 {stats['index_lines']} 行。"
                )
                if samples:
                    notice += f"样例：{samples}。"
                notice += f"确认删除请执行 /forget --yes {keyword}"
                self.post_notice(notice, session_name)
                return {"ok": True, "slash": True, "confirm_required": True}
            # 第二阶段：删除命中行（原子写回），并回显两处各删了多少行，
            # 让用户能对上第一阶段的统计数字。
            stats = delete_matches(keyword, log_path=log_path, index_path=index_path)
            self.post_notice(
                f"已删除：当日日志 {stats['log_lines']} 行、"
                f"索引 {stats['index_lines']} 行",
                session_name,
            )
            return {"ok": True, "slash": True}
        except Exception as exc:
            logger.exception("/forget 执行失败")
            self.post_notice(f"/forget 执行失败：{exc}", session_name, "error")
            return {"ok": False, "msg": str(exc), "slash": True}

    _GOAL_STATUS_TEXT = {
        "running": "进行中",
        "awaiting_confirm": "待确认",
        "achieved": "已达成",
        "blocked": "受阻（轮次用尽）",
        "stopped": "已停止",
        "interrupted": "已中断",
    }
    # 只接受这三种状态 resume：achieved 再跑没有意义，awaiting_confirm 需要的是
    # "确认达成"而不是"再来一轮"，running 说明线程还在跑（重复启动会被拒）。
    # 取值必须与 core.slash_commands.goal.state 的 STATUS_* 一致——那里是权威
    # 定义，这里只是"可恢复集合"的适配层视图。
    _GOAL_RESUMABLE = ("blocked", "stopped", "interrupted")

    def _goal_runs_map(self):
        runs = getattr(self, "_goal_runs", None)
        if runs is None:
            runs = self._goal_runs = {}
        return runs

    def _goal_store(self):
        from core.slash_commands.goal.gui_executor import open_goal_store

        return open_goal_store()

    def get_goal_state(self, session_name: str = "") -> Dict[str, Any]:
        target_session = str(session_name or "") or self._current_session
        if not target_session:
            return {"ok": True, "state": None}
        try:
            state = self._goal_store().load(target_session)
        except Exception as exc:
            logger.exception("读取目标状态失败")
            return {"ok": False, "msg": str(exc)}
        if state is None:
            return {"ok": True, "state": None}
        last_summary = ""
        if state.history:
            last_summary = str(state.history[-1].get("summary") or "")
        return {
            "ok": True,
            "state": {
                "goal": state.goal,
                "round": state.round,
                "max_rounds": state.max_rounds,
                "status": state.status,
                "last_summary": last_summary,
                "check": state.check,
            },
        }

    def goal_stop(self, session_name: str = "") -> Dict[str, Any]:
        target_session = str(session_name or "") or self._current_session
        if not target_session:
            return {"ok": False, "msg": "会话不存在"}
        live = self._goal_runs_map().get(target_session)
        if live is not None and not live.is_set():
            return self._goal_stop(target_session)
        try:
            from core.slash_commands.goal.state import (
                STATUS_AWAITING_CONFIRM,
                STATUS_RUNNING,
                STATUS_STOPPED,
            )

            store = self._goal_store()
            state = store.load(target_session)
            if state is None or state.status not in (STATUS_RUNNING, STATUS_AWAITING_CONFIRM):
                return self._goal_stop(target_session)
            state.status = STATUS_STOPPED
            store.save(state)
        except Exception as exc:
            logger.exception("停止目标失败")
            self.post_notice(f"停止目标失败：{exc}", target_session, "error")
            return {"ok": False, "msg": str(exc)}
        self.post_notice("已停止目标：不再自动续跑", target_session)
        return {"ok": True}

    def goal_resume(self, session_name: str = "") -> Dict[str, Any]:
        """状态条上的「继续」：复用 /goal resume（仅 blocked/stopped/interrupted 可恢复）。"""
        target_session = str(session_name or "") or self._current_session
        if not target_session:
            return {"ok": False, "msg": "会话不存在"}
        return self._goal_resume(target_session)

    def goal_confirm(self, session_name: str = "") -> Dict[str, Any]:
        target_session = str(session_name or "") or self._current_session
        if not target_session:
            return {"ok": False, "msg": "会话不存在"}
        return self._goal_confirm(target_session)

    def _goal_status(self, session_name):
        try:
            state = self._goal_store().load(session_name)
        except Exception as exc:
            logger.exception("读取目标状态失败")
            self.post_notice(f"读取目标状态失败：{exc}", session_name, "error")
            return {"ok": False, "msg": str(exc), "slash": True}
        if state is None:
            self.post_notice("当前会话没有进行中的目标", session_name)
            return {"ok": True, "slash": True}
        summary = "（还没有完成任何一轮）"
        if state.history:
            summary = str(state.history[-1].get("summary") or "") or summary
        status_text = self._GOAL_STATUS_TEXT.get(state.status, state.status)
        self.post_notice(
            f"第 {state.round}/{state.max_rounds} 轮 · {status_text} · {summary}",
            session_name,
        )
        return {"ok": True, "slash": True}

    def _goal_stop(self, session_name):
        event = self._goal_runs_map().get(session_name)
        if event is None:
            self.post_notice("当前没有运行中的目标", session_name)
            return {"ok": True, "slash": True}
        event.set()
        try:
            self.stop_current_turn(session_name)
        except Exception:
            logger.debug("停止目标回合失败", exc_info=True)
        self.post_notice("已停止目标：本轮中断，不再自动续跑", session_name)
        return {"ok": True, "slash": True}

    def _goal_resume(self, session_name):
        try:
            store = self._goal_store()
            state = store.load(session_name)
        except Exception as exc:
            logger.exception("读取目标状态失败")
            self.post_notice(f"读取目标状态失败：{exc}", session_name, "error")
            return {"ok": False, "msg": str(exc), "slash": True}
        if state is None:
            self.post_notice("当前会话没有可恢复的目标，用 /goal <目标> 新建一个", session_name)
            return {"ok": True, "slash": True}
        if state.status not in self._GOAL_RESUMABLE:
            status_text = self._GOAL_STATUS_TEXT.get(state.status, state.status)
            hint = {
                "achieved": "目标已达成，无需恢复；要跑新目标请用 /goal <目标>",
                "awaiting_confirm": "目标在待确认状态，请确认是否达成或用 /goal <目标> 重新设定",
                "running": "目标已在运行中，无需重复恢复",
            }.get(state.status, "")
            self.post_notice(
                f"当前状态为「{status_text}」，不能恢复。{hint}".strip(), session_name
            )
            return {"ok": True, "slash": True}
        if state.round >= state.max_rounds:
            self.post_notice(
                f"轮次已用尽（{state.round}/{state.max_rounds}），"
                "请用 /goal <目标> [--rounds N] 重新设定目标与轮次",
                session_name,
            )
            return {"ok": True, "slash": True}
        from core.slash_commands.goal.state import STATUS_RUNNING

        state.status = STATUS_RUNNING
        try:
            store.save(state)
        except Exception as exc:
            logger.exception("保存目标状态失败")
            self.post_notice(f"保存目标状态失败：{exc}", session_name, "error")
            return {"ok": False, "msg": str(exc), "slash": True}
        started, message = self._start_goal_run(state)
        if not started:
            self.post_notice(message, session_name, "error")
            return {"ok": False, "msg": message, "slash": True}
        self.post_notice(
            f"已恢复目标（第 {state.round}/{state.max_rounds} 轮起）：{state.goal}",
            session_name,
        )
        return {"ok": True, "slash": True}

    def _goal_confirm(self, session_name):
        from core.slash_commands.goal.state import (
            STATUS_ACHIEVED,
            STATUS_AWAITING_CONFIRM,
        )

        try:
            store = self._goal_store()
            state = store.load(session_name)
        except Exception as exc:
            logger.exception("读取目标状态失败")
            self.post_notice(f"读取目标状态失败：{exc}", session_name, "error")
            return {"ok": False, "msg": str(exc), "slash": True}
        if state is None:
            self.post_notice(
                "当前会话没有可确认的目标，用 /goal <目标> 新建一个", session_name
            )
            return {"ok": True, "slash": True}
        if state.status != STATUS_AWAITING_CONFIRM:
            status_text = self._GOAL_STATUS_TEXT.get(state.status, state.status)
            hint = {
                "running": "目标还在跑，等出现「待确认」再确认达成",
                "achieved": "目标已达成，无需重复确认；要跑新目标请用 /goal <目标>",
            }.get(state.status, "")
            self.post_notice(
                f"当前状态为「{status_text}」，无需确认。{hint}".strip(), session_name
            )
            return {"ok": True, "slash": True}
        state.status = STATUS_ACHIEVED
        try:
            store.save(state)
        except Exception as exc:
            logger.exception("保存目标状态失败")
            self.post_notice(f"保存目标状态失败：{exc}", session_name, "error")
            return {"ok": False, "msg": str(exc), "slash": True}
        self.post_notice(
            f"已确认目标达成（第 {state.round}/{state.max_rounds} 轮）：{state.goal}",
            session_name,
        )
        return {"ok": True, "slash": True}

    def _materialize_goal_session(self, session_name, workdir, goal, turn_source="local"):
        if not re.match(r"^session_[a-f0-9]{6}$", session_name or ""):
            return session_name
        if not str(workdir or "").strip():
            return session_name
        try:
            existing = {
                s.get("name")
                for s in self._memory.get_all_sessions(workdir=None, include_empty=True)
            }
            if session_name in existing:
                return session_name
            self._memory.ensure_session(session_name, workdir=str(workdir), source="GUI")
            if turn_source == "local" and getattr(self, "_current_session", None) != session_name:
                self._set_current_session(session_name)
            return session_name
        except Exception:
            # 落库失败不拦住目标启动（回合 worker 里还有 ensure_session 兜底），
            # 错误留给日志——用户至少能看到目标在跑，而不是点了没反应
            logger.debug("goal 草稿落库失败", exc_info=True)
            return session_name

    def _known_model_ids(self):
        return {
            str(m.get("id"))
            for p in load_config().get("providers", []) or []
            for m in (p.get("models") or [])
        }

    def _goal_target_model_id(self, session_name, model_id=""):
        candidate = str(model_id or "").strip()
        if candidate:
            try:
                known_ids = self._known_model_ids()
            except Exception:
                known_ids = set()
            if not known_ids or candidate in known_ids:
                return candidate
        # 回退：触发时所在会话的绑定（旧客户端/其他入口不带 model_id 时）。
        # 直接读库里的值，不需要再校验——能被绑进会话的 id 本来就是合法的。
        getter = getattr(getattr(self, "_memory", None), "get_session_model_id", None)
        if getter is None:
            return ""
        for name in (session_name, getattr(self, "_current_session", "")):
            if not name:
                continue
            try:
                bound = str(getter(name) or "").strip()
            except Exception:
                bound = ""
            if bound:
                return bound
        return ""

    def _inherit_goal_session_model(self, session_name, model_id):
        """把触发 /goal 时的模型绑到目标会话；已有绑定不覆盖。

        已有绑定代表用户在该会话里显式选过模型，是更强的意图；没有绑定则必须补，
        否则目标循环每轮都退回全局默认模型（本问题的表现）。
        """
        if not model_id or not session_name:
            return
        memory = getattr(self, "_memory", None)
        if memory is None:
            return
        getter = getattr(memory, "get_session_model_id", None)
        setter = getattr(memory, "set_session_model_id", None)
        if setter is None:
            return
        try:
            if getter is not None and getter(session_name):
                return
            setter(session_name, model_id)
        except Exception:
            logger.debug("目标会话模型继承失败", exc_info=True)

    def _goal_start(self, action, session_name, workdir="", turn_source="local", model_id=""):
        from core.slash_commands.goal.state import GoalState
        from core.slash_commands.goal_command import DEFAULT_MAX_ROUNDS, GOAL_USAGE

        goal = str(action.get("goal") or "").strip()
        if not goal:
            # 理论上命令层已拦住空目标，这里兜底：没有目标的循环只会空转烧 token
            self.post_notice(GOAL_USAGE, session_name, "error")
            return {"ok": False, "msg": "目标不能为空", "slash": True}
        try:
            max_rounds = int(action.get("max_rounds") or 0)
        except (TypeError, ValueError):
            max_rounds = 0
        if max_rounds <= 0:
            max_rounds = DEFAULT_MAX_ROUNDS
        runs = self._goal_runs_map()
        existing = runs.get(session_name)
        if existing is not None and not existing.is_set():
            self.post_notice(
                "当前会话已有运行中的目标，先 /goal stop 再重新设定", session_name, "error"
            )
            return {"ok": False, "msg": "已有运行中的目标", "slash": True}
        goal_model_id = self._goal_target_model_id(session_name, model_id)
        # 草稿会话先按前端携带的目录落库：/goal 就地执行走不到普通消息的落库
        # 分支，缺这步会让目标循环首轮把会话错挂到进程启动目录（本次 bug）
        final_session = self._materialize_goal_session(
            session_name, workdir, goal, turn_source=turn_source
        )
        if final_session != session_name:
            # 后台循环、运行表与状态文件全部改用最终名：改名后旧名不复存在，
            # 否则 GoalBar / stop / resume 都查不到这个目标
            session_name = final_session
        # 目标循环的每一轮都按会话绑定取模型：草稿会话刚落库还没有绑定，
        # 不补这一步就会回退全局默认（"用 A 模型敲的命令，跑成了 B 模型"）。
        self._inherit_goal_session_model(session_name, goal_model_id)
        state = GoalState(
            session=session_name,
            goal=goal,
            check=str(action.get("check") or "").strip(),
            max_rounds=max_rounds,
        )
        try:
            self._goal_store().save(state)
        except Exception as exc:
            logger.exception("保存目标状态失败")
            self.post_notice(f"保存目标状态失败：{exc}", session_name, "error")
            return {"ok": False, "msg": str(exc), "slash": True}
        started, message = self._start_goal_run(state)
        if not started:
            self.post_notice(message, session_name, "error")
            return {"ok": False, "msg": message, "slash": True}
        self.post_notice(
            f"已启动目标（最多 {state.max_rounds} 轮）：{state.goal}"
            + (f" · 验证命令：{state.check}" if state.check else ""),
            session_name,
        )
        return {"ok": True, "slash": True, "session": session_name}

    def _start_goal_run(self, state):
        from core.slash_commands.goal.gui_executor import GuiGoalExecutor
        from core.slash_commands.goal.runner import run_goal_loop

        session_name = state.session
        runs = self._goal_runs_map()
        existing = runs.get(session_name)
        if existing is not None and not existing.is_set():
            return False, "该会话已有运行中的目标"
        cancel_event = threading.Event()
        runs[session_name] = cancel_event
        try:
            store = self._goal_store()
        except Exception as exc:
            runs.pop(session_name, None)
            logger.exception("打开目标状态仓库失败")
            return False, f"打开目标状态仓库失败：{exc}"
        executor = GuiGoalExecutor(self, session_name)

        def on_event(event, current_state):
            """循环进度回调：只做广播，不碰界面对象也绝不抛给循环。"""
            try:
                if event == "round_done":
                    summary = ""
                    if current_state.history:
                        summary = str(current_state.history[-1].get("summary") or "")
                    self.post_notice(
                        f"目标第 {current_state.round}/{current_state.max_rounds} 轮：{summary}",
                        session_name,
                    )
                elif event == "finished":
                    status_text = self._GOAL_STATUS_TEXT.get(
                        current_state.status, current_state.status
                    )
                    self.post_notice(
                        f"目标流程结束：{status_text}"
                        f"（第 {current_state.round}/{current_state.max_rounds} 轮）",
                        session_name,
                    )
            except Exception:
                logger.debug("目标进度提示广播失败", exc_info=True)

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
                logger.exception("目标循环异常退出")
                try:
                    self.post_notice(f"目标循环异常终止：{exc}", session_name, "error")
                except Exception:
                    pass
            finally:
                if runs.get(session_name) is cancel_event:
                    runs.pop(session_name, None)

        thread = threading.Thread(
            target=worker, name=f"goal-loop-{session_name}", daemon=True
        )
        thread.start()
        return True, ""

    def send_user_message(self, text: str, session_name: Optional[str] = None, attachments: Optional[List[str]] = None, workdir: Optional[str] = None, model_id: Optional[str] = None, permission_mode: Optional[str] = None, turn_source: str = "local") -> Dict[str, Any]:
        if turn_source not in TURN_SOURCES:
            return {"ok": False, "msg": "无效的回合来源"}
        try:
            if self._shutting_down:
                return {"ok": False, "msg": "应用正在退出"}
            clean_text = str(text or "").strip()
            attach_list = [str(p) for p in (attachments or []) if str(p).strip()]
            if not clean_text and not attach_list:
                return {"ok": False, "msg": "消息内容不能为空"}
            full_text = clean_text
            if attach_list:
                joined = " ".join("@" + p for p in attach_list)
                full_text = f"{clean_text} {joined}" if clean_text else joined
            target_session = session_name or self._current_session
            clean_model_id = str(model_id or "").strip()
            blocked = self._tr_session_gate(clean_model_id)
            if blocked is not None:
                return blocked
            # 会话级权限档位：pending 草稿会话在落库前无法绑定，用户的选择随
            # 首条消息带给 worker 补绑定；未知值直接拒绝（宁可不发也不误放权）
            pending_permission_mode = ""
            raw_permission = str(permission_mode or "").strip()
            if raw_permission:
                if not app_context.is_known_permission_mode(raw_permission):
                    return {"ok": False, "msg": f"未知权限模式: {raw_permission}"}
                pending_permission_mode = app_context.normalize_permission_mode(raw_permission)
                if pending_permission_mode == "auto_decision" and not self._is_decision_model_enabled():
                    pending_permission_mode = "confirm"
            # 斜杠命令接入点放在取锁之前：命令要么是纯文本展开、要么是本地副作用，
            # 都不需要进入回合临界区；若放进锁内，命令里的 post_notice 广播会在持锁
            # 期间触发前端回调，容易和排队/补发逻辑互相等待。
            # workdir/turn_source 一并带入：草稿首条消息是 /goal 这类"本地命令 +
            # 后续真实回合"时，动作层要按本条消息携带的目录先落库（见 _goal_start）；
            # model_id 同样要带进去，否则 /goal 新建的会话拿不到用户当前选的模型
            slash = self._dispatch_slash(
                clean_text,
                target_session,
                full_text,
                workdir=str(workdir or ""),
                turn_source=turn_source,
                model_id=clean_model_id,
            )
            if slash is not None:
                if "prompt" in slash:
                    # 提示词命令：用展开后的提示词替换本回合正文，仍走模型；
                    # 附件 @路径 是拼接在 clean_text 之后的，替掉正文时要把尾部
                    # （附件标记或用户续写）接回去，否则附件会被静默丢掉。
                    prompt_text = slash["prompt"]
                    if full_text != clean_text:
                        tail = full_text[len(clean_text):].strip()
                        if tail:
                            prompt_text = f"{prompt_text}\n\n{tail}"
                    full_text = prompt_text
                else:
                    return slash
            with self._turn_start_lock:
                if self._is_turn_active(target_session):
                    self._message_queues.setdefault(target_session, []).append({
                        "id": f"q_{uuid.uuid4().hex[:8]}",
                        "text": full_text,
                        "display_text": clean_text,
                        "attachments": attach_list,
                        "queued_at": time.time(),
                        # LAN 排队消息补发时保持来源（不得降级为 local）
                        "turn_source": turn_source,
                    })
                    self._emit_message_queue_snapshot(target_session)
                    return {"ok": True, "queued": True}

                if workdir and re.match(r"^session_[a-f0-9]{6}$", target_session or ""):
                    # 标题派生前先净化 @{会话名} 标记：直接把标记原文截进名字会
                    # 产生「@{xxx」这类残缺标题，令该会话永远无法被 @ 引用命中
                    from core.input_parser import sanitize_session_title
                    base_title = sanitize_session_title(clean_text)[:12].replace("\n", " ").strip()
                    if base_title:
                        try:
                            self._memory.ensure_session(target_session, workdir=str(workdir), source="GUI")
                            # 本处已持有 _turn_start_lock（send 临界区内）：
                            # 随迁队列不得重入取锁（裸构造测试桥装的是普通 Lock）
                            new_session = self._rename_session_to_title(
                                target_session, base_title, holds_turn_start_lock=True
                            )
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
                    if turn_source == "local" and self._current_session != target_session:
                        self._set_current_session(target_session)
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
                           pending_permission_mode: str = "",
                           secretary_dispatch_context: Optional[Dict[str, str]] = None) -> None:
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
        if secretary_dispatch_context:
            turn_kwargs["secretary_dispatch_context"] = secretary_dispatch_context
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
                automation_run_id = str(entry.get("automation_run_id") or "")
                secretary_notify_session = str(
                    entry.get("secretary_dispatch_notify_session") or ""
                )
                try:
                    self._spawn_turn_worker(
                        str(entry.get("text") or ""),
                        session_name,
                        display_text=str(entry.get("display_text") or ""),
                        # 排队消息补发保持原回合来源：LAN 消息不得降级为 local
                        turn_source=str(entry.get("turn_source") or "local"),
                        attachments=[str(p) for p in (entry.get("attachments") or [])],
                        pending_workdir=self._automation_pending_workdir(
                            str(entry.get("pending_workdir") or ""), session_name
                        ),
                        pending_model_id=str(
                            entry.get("automation_model_id")
                            or entry.get("secretary_model_id")
                            or ""
                        ),
                        pending_permission_mode=str(
                            entry.get("secretary_permission_mode") or ""
                        ),
                        internal_kind="automation" if automation_run_id else "",
                        automation_context={
                            "run_id": automation_run_id,
                            "task_name": str(entry.get("automation_task_name") or ""),
                            "permission_mode": str(entry.get("automation_permission_mode") or ""),
                            "notify_on_success": str(entry.get("automation_notify_success") or ""),
                        } if automation_run_id else None,
                        secretary_dispatch_context={
                            "display_name": str(
                                entry.get("secretary_dispatch_display_name") or ""
                            ),
                            "notify_session": secretary_notify_session,
                            "dispatch_id": entry.get("secretary_dispatch_id"),
                            "task_title": str(
                                entry.get("secretary_dispatch_task_title") or ""
                            ),
                        } if secretary_notify_session else None,
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
        try:
            recovered_runs = self._workflow_run_storage.recover_stale_runs()
            if recovered_runs:
                logger.info("星流工作流启动恢复：%s 条遗留运行标记为 interrupted", recovered_runs)
        except Exception:
            logger.exception("星流工作流启动恢复失败")
        self._automation_engine.start()
        self._emit_automation_tasks_changed()

    def _automation_pending_workdir(self, task_workdir: str, session_name: str) -> str:
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

    def _ensure_automation_run_session(self, task: dict, run_id: str) -> str:
        import datetime

        from core.automation.session_naming import build_run_session_name

        task_name = str(task.get("name") or "")
        name = build_run_session_name(task_name, datetime.datetime.now())
        existing = False
        try:
            for s in self._memory.get_all_sessions(
                workdir=None, include_empty=True, include_archived=True
            ):
                if s.get("name") == name:
                    existing = True
                    if s.get("archived_at"):
                        try:
                            self._memory.unarchive_session(name)
                        except Exception:
                            logger.debug("复活归档的运行会话失败", exc_info=True)
                    break
        except Exception:
            logger.debug("查询运行会话是否存在失败，按不存在处理", exc_info=True)
            existing = False
        if existing:
            return name
        workdir = str(task.get("workdir") or "").strip() or "GLOBAL"
        self._memory.create_session_explicit(name, workdir=workdir, source="GUI")
        location = "对话分组" if workdir.upper() == "GLOBAL" else f"项目目录 {workdir}"
        try:
            self.emit_event("notice", {
                "message": f"星轨任务「{task_name.strip()}」已新建会话「{name}」（{location}）",
            })
        except Exception:
            logger.debug("推送运行会话新建提示失败", exc_info=True)
        return name

    def _dispatch_automation_run(self, task: dict, run_id: str, trigger_kind: str) -> None:
        if self._shutting_down:
            return
        session_mode = str(task.get("session_mode") or "per_run").strip() or "per_run"
        if session_mode == "fixed":
            target_session = str(task.get("target_session") or "").strip() or "星轨自动化任务"
        else:
            target_session = ""
            try:
                target_session = str(
                    self._ensure_automation_run_session(task, run_id) or ""
                ).strip()
            except Exception as exc:
                # 建会话失败：绝不让异常冒泡到引擎线程（会停摆整个调度循环）
                logger.exception("星轨自动化运行会话创建失败")
                target_session = ""
                failure = f"新建运行会话失败: {exc}"[:500]
            else:
                failure = ""
            if not target_session:
                failure = failure or "新建运行会话失败"
                try:
                    self._automation_storage.finish_run(run_id, "error", failure)
                except Exception:
                    logger.exception("自动化运行错误终态写入失败")
                try:
                    self.emit_event("notice", {
                        "message": (
                            f"星轨自动化任务「{task.get('name')}」新建对话失败，"
                            f"本次运行已取消：{failure}"
                        ),
                    })
                except Exception:
                    logger.debug("推送运行会话失败提示异常", exc_info=True)
                return
        try:
            self._automation_storage.set_run_session(run_id, target_session)
        except Exception:
            logger.warning("自动化运行会话名回写失败", exc_info=True)
        display_text = f"星轨自动化任务「{task.get('name')}」"
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
                    "turn_source": "local",
                    "automation_run_id": run_id,
                    "automation_task_name": automation_context["task_name"],
                    "automation_permission_mode": automation_context["permission_mode"],
                    "automation_notify_success": automation_context["notify_on_success"],
                    "pending_workdir": pending_workdir,
                    "automation_model_id": task_model_id,
                })
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
                logger.exception("星轨自动化回合 spawn 失败")
                try:
                    self._automation_storage.finish_run(
                        run_id, "error", f"回合线程启动失败: {exc}"[:500]
                    )
                except Exception:
                    logger.exception("自动化运行错误终态写入失败")

    def _on_automation_run_updated(self, run: dict) -> None:
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
            dto["enabled"] = bool(dto.get("enabled"))
            dto["notify_on_success"] = bool(dto.get("notify_on_success"))
            max_runs = dto.get("max_runs")
            try:
                dto["remaining_runs"] = (
                    None
                    if max_runs is None
                    else int(max_runs) - int(dto.get("scheduled_run_count") or 0)
                )
            except (TypeError, ValueError):
                dto["remaining_runs"] = None
            dtos.append(dto)
        return dtos

    def _register_automation_service(self) -> None:
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

    def _register_decision_audit_listener(self) -> None:
        """把决策审计事件转发为前端桥事件（会话头部徽章 / 决策面板实时刷新）。

        独立支路：不参与会话 runtime 归属；转发失败只记调试日志，绝不影响
        决策主流程与审计落盘。
        """
        from core import decision_service

        def _forward(brief: dict) -> None:
            try:
                self.emit_event("decision_recorded", dict(brief or {}))
            except Exception:
                logger.debug("decision_recorded 转发失败", exc_info=True)

        decision_service.add_audit_listener(_forward)

    def _register_secretary_dispatch_service(self) -> None:
        from core import secretary_dispatch

        secretary_dispatch.register(_SecretaryDispatchService(self))

    def _secretary_dispatch_store(self):
        """取派活台账 store：优先用已注册调度服务的实例，取不到返回 None。

        台账是"锦上添花"：服务未注册时回落直接构造，连管理器都取不到就返回
        None，调用方判空跳过，绝不让回写路径抛异常。
        """
        try:
            from core import secretary_dispatch as _dispatch_module

            service = _dispatch_module.get()
            store = getattr(service, "_store", None) if service is not None else None
            if store is not None:
                return store
            manager = getattr(self, "_memory", None)
            if manager is None:
                return None
            return DispatchStore(manager)
        except Exception:
            logger.debug("获取派活台账失败", exc_info=True)
            return None

    def _secretary_dispatch_last_assistant(self, session_name: str, limit: int = 800) -> str:
        """目标会话最后一条有正文的 assistant 消息（截 limit 字）；没有返回空串。

        工具调用消息（content 为空）与纯思考消息都要跳过，否则 notice 会出现
        「已完成：」后面空着。
        """
        try:
            history = self._memory.get_history(session_name) or []
        except Exception:
            logger.debug("读取派活目标会话历史失败", exc_info=True)
            return ""
        for message in reversed(list(history)):
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                chunks = []
                for block in content:
                    if isinstance(block, str):
                        chunks.append(block)
                    elif isinstance(block, dict) and isinstance(block.get("text"), str):
                        chunks.append(block["text"])
                text = "".join(chunks)
            else:
                text = ""
            text = text.strip()
            if text:
                return text[:limit]
        return ""

    def _finalize_secretary_dispatch(self, context, session_name: str, *,
                                     cancelled: bool = False,
                                     terminal_error: str = "") -> None:
        """秘书派活回合收尾：回写台账 → 推送 notice → 推进下游依赖。

        三种终态：cancelled / failed（terminal_error 非空）/ done（取目标会话最后
        一条 assistant 正文截 800 字）。台账缺失或写失败一律只记 debug，notice
        退化成不带任务编号的旧文案，收尾流程完全不受影响。
        """
        try:
            ctx = dict(context or {})
            notify_session = str(ctx.get("notify_session") or "") or session_name
            label = str(ctx.get("display_name") or "").strip()
            if (
                (not label or label == "新会话")
                and session_name
                and not _SECRETARY_DRAFT_NAME_RE.match(session_name)
            ):
                label = session_name
            label = label or session_name
            dispatch_id = ctx.get("dispatch_id")
            try:
                dispatch_id = None if dispatch_id in (None, "", 0) else int(dispatch_id)
            except Exception:
                dispatch_id = None
            id_suffix = f"（任务 #{dispatch_id}）" if dispatch_id is not None else ""

            # 1) 按终态回写台账（异常只记 debug，绝不外抛）。成功终态的摘要先取
            # 出来，台账写失败也仍能用于 notice（摘要只是结论预览，不依赖落库）
            result_excerpt = ""
            if dispatch_id is not None and not cancelled and not terminal_error:
                result_excerpt = self._secretary_dispatch_last_assistant(session_name)
            ledger_written = False
            if dispatch_id is not None:
                store = self._secretary_dispatch_store()
                if store is not None:
                    try:
                        if cancelled:
                            store.mark_status(dispatch_id, "cancelled")
                        elif terminal_error:
                            store.mark_status(
                                dispatch_id, "failed", error=str(terminal_error)[:500]
                            )
                        else:
                            store.mark_status(
                                dispatch_id, "done", result_excerpt=result_excerpt
                            )
                        ledger_written = True
                    except Exception:
                        logger.debug("派活台账回写失败", exc_info=True)
                    # 台账落终态成功后，往该项目画像的「任务流水」追加一行
                    # （设计 5.1）：流水记「什么时候派了什么、结果如何」，下次
                    # 派活前能直接回看。整段吞异常——流水是附带记录，绝不能
                    # 影响 notice 推送与回合收尾。
                    if ledger_written:
                        self._write_dispatch_task_log(store, dispatch_id)

            if cancelled:
                notice_message = f"「{label}」已取消{id_suffix}"
            elif terminal_error:
                notice_message = (
                    f"「{label}」执行失败：{str(terminal_error)[:80]}{id_suffix}"
                )
            elif id_suffix and result_excerpt:
                preview = result_excerpt[:60]
                ellipsis = "…" if len(result_excerpt) > 60 else ""
                notice_message = f"「{label}」已完成：{preview}{ellipsis}{id_suffix}"
            else:
                notice_message = f"任务已在「{label}」会话执行完成{id_suffix}"
            self.emit_event("notice", {
                "message": notice_message,
                "session_name": notify_session,
            })

            if ledger_written:
                try:
                    self._advance_dependents(dispatch_id)
                except Exception:
                    logger.debug("派活依赖推进失败", exc_info=True)
                if not cancelled and store is not None and dispatch_id is not None:
                    try:
                        finished_row = store.get(dispatch_id)
                        batch_id = str((finished_row or {}).get("batch_id") or "").strip()
                        if not batch_id:
                            self._schedule_secretary_dispatch_wakeup(notify_session)
                    except Exception:
                        logger.debug("触发单条派活秘书唤醒失败", exc_info=True)
        except Exception:
            logger.debug("秘书派活收尾处理失败", exc_info=True)

    def _write_dispatch_task_log(self, store, dispatch_id) -> None:
        """把已落终态的派活追加进该项目画像的「任务流水」（升级设计 5.1）。

        行格式：``- YYYY-MM-DD #<id> 「<title>」<status>：<摘要前 40 字>``。
        workdir 为 ``GLOBAL`` 或空（没有项目归属）时跳过；任何异常只记
        debug——流水是附带记录，绝不能影响 notice 与回合收尾。
        """
        try:
            row = store.get(dispatch_id) if store is not None else None
            if not isinstance(row, dict):
                return
            workdir = str(row.get("workdir") or "").strip()
            if not workdir or workdir.upper() == "GLOBAL":
                return
            excerpt = " ".join(str(row.get("result_excerpt") or "").split())
            line = "%s #%s 「%s」%s：%s" % (
                time.strftime("%Y-%m-%d"),
                dispatch_id,
                str(row.get("title") or "").strip(),
                str(row.get("status") or "").strip(),
                excerpt[:40],
            )
            project_profile.append_task_log(workdir, line)
        except Exception:
            logger.debug("追加派活任务流水失败", exc_info=True)

    # ------------------------------------------------------------------
    # 秘书派活依赖推进器（P3：升级设计 2026-11-04 6.2）
    # 上游落终态后由 _finalize_secretary_dispatch 调用，全部在独立 daemon 线程里
    # 跑：查库与起下游回合都可能耗时，绝不能拖慢回合收尾；调用方不等待。
    # ------------------------------------------------------------------
    def _advance_dependents(self, dispatch_id) -> None:
        """上游落终态后推进依赖：done → 解锁下游；failed/cancelled → 级联阻断。

        只做线程编排，真正的判定与派发在 ``_advance_dependents_worker`` 里。线程
        起不来（极端资源耗尽）也只记 debug：下游保持 blocked 下次还能等人救，
        比把回合收尾带崩划算。
        """
        try:
            parsed_id = int(dispatch_id)
        except (TypeError, ValueError):
            logger.debug("派活依赖推进跳过：dispatch_id 非法 %r", dispatch_id)
            return
        try:
            threading.Thread(
                target=self._advance_dependents_worker,
                args=(parsed_id,),
                daemon=True,
                name="secretary-dispatch-advance",
            ).start()
        except Exception:
            logger.debug("启动派活依赖推进线程失败", exc_info=True)

    def _advance_dependents_worker(self, dispatch_id: int) -> None:
        try:
            store = self._secretary_dispatch_store()
            if store is None:
                return
            row = store.get(dispatch_id)
            if not isinstance(row, dict):
                return
            status = str(row.get("status") or "")
            if status == STATUS_DONE:
                self._unlock_dependents(store, dispatch_id)
            elif status in (STATUS_FAILED, STATUS_CANCELLED):
                self._block_dependents(
                    store,
                    parent_id=dispatch_id,
                    root_id=dispatch_id,
                    cancelled=(status == STATUS_CANCELLED),
                    seen={dispatch_id},
                )
            batch_id = str(row.get("batch_id") or "").strip()
            secretary_session = str(row.get("secretary_session") or "").strip()
            if batch_id and secretary_session:
                if status == STATUS_FAILED or not store.batch_has_active_tasks(batch_id):
                    self._schedule_secretary_dispatch_wakeup(secretary_session)
        except Exception:
            logger.debug("派活依赖推进失败", exc_info=True)

    def _unlock_dependents(self, store, dispatch_id: int) -> None:
        try:
            unlocked = store.unlocked_dependents(dispatch_id) or []
        except Exception:
            logger.debug("查询已解锁下游失败", exc_info=True)
            return
        service = self._secretary_dispatch_service_handle()
        for downstream in unlocked:
            try:
                downstream_id = int(downstream.get("id"))
            except (TypeError, ValueError):
                continue
            try:
                if service is None:
                    logger.debug("派活服务未注册，下游 #%s 无法自动启动", downstream_id)
                    continue
                service.resume_dispatch(downstream_id)
            except Exception:
                logger.debug("自动启动下游任务 #%s 失败", downstream_id, exc_info=True)
                continue
            label = (
                str(downstream.get("target_display") or "").strip()
                or str(downstream.get("target_session") or "").strip()
                or "目标会话"
            )
            try:
                self.emit_event("notice", {
                    "message": (
                        f"「{label}」已自动启动（依赖的上游 #{dispatch_id} 已完成）"
                        f"（任务 #{downstream_id}）"
                    ),
                    "session_name": str(downstream.get("secretary_session") or ""),
                })
            except Exception:
                logger.debug("自动启动下游的 notice 推送失败", exc_info=True)

    def _block_dependents(self, store, *, parent_id: int, root_id: int,
                          cancelled: bool, seen: set) -> None:
        verb = "已取消" if cancelled else "执行失败"
        try:
            downstream_list = store.dependents_of(parent_id) or []
        except Exception:
            logger.debug("查询下游失败", exc_info=True)
            return
        for downstream in downstream_list:
            try:
                downstream_id = int(downstream.get("id"))
            except (TypeError, ValueError):
                continue
            if downstream_id in seen:
                continue
            seen.add(downstream_id)
            status = str(downstream.get("status") or "")
            if status in (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED):
                continue
            if parent_id == root_id:
                reason = f"上游 #{root_id} {verb}，本任务未启动"
            else:
                reason = f"上游 #{parent_id} 的上游 #{root_id} {verb}，依赖链中断"
            try:
                store.mark_status(downstream_id, STATUS_BLOCKED, error=reason)
            except Exception:
                logger.debug("阻断下游任务 #%s 失败", downstream_id, exc_info=True)
            self._block_dependents(
                store,
                parent_id=downstream_id,
                root_id=root_id,
                cancelled=cancelled,
                seen=seen,
            )

    def _secretary_dispatch_service_handle(self):
        try:
            from core import secretary_dispatch as _dispatch_module

            return _dispatch_module.get()
        except Exception:
            logger.debug("获取秘书调度服务失败", exc_info=True)
            return None

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
            if raw.get("permission_mode") == "auto_decision" and not self._is_decision_model_enabled():
                return {"ok": False, "errors": ["决策模型（JEV）尚未启用，无人值守任务不能设为「自动决策」"]}
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

    def get_workflows(self, workspace_root: str = "") -> Dict[str, Any]:
        try:
            from core.workflows import store as workflow_store

            data = workflow_store.list_workflows(str(workspace_root or "").strip() or None)
            return {"ok": True, "workflows": data["workflows"], "invalid": data["invalid"]}
        except Exception as e:
            logger.exception("读取工作流清单失败")
            return {"ok": False, "msg": str(e)}

    def get_workflow_detail(self, name: str, workspace_root: str = "") -> Dict[str, Any]:
        try:
            from core.workflows import store as workflow_store

            entry = workflow_store.get_workflow(
                str(name or "").strip(), str(workspace_root or "").strip() or None
            )
            return {"ok": True, "workflow": entry}
        except LookupError as exc:
            return {"ok": False, "msg": str(exc)}
        except Exception as e:
            logger.exception("读取工作流详情失败")
            return {"ok": False, "msg": str(e)}

    def pick_workflow_files(self) -> Dict[str, Any]:
        """原生文件选择框（多选 .dwf.ts）；用户取消返回 ok=False + msg。"""
        try:
            import webview

            if not self._window:
                return {"ok": False, "msg": "窗口尚未就绪"}
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=("Workflow (*.dwf.ts;*.ts)",),
            )
            if not result:
                return {"ok": False, "msg": "已取消选择"}
            return {"ok": True, "paths": [str(item) for item in result]}
        except Exception as e:
            logger.exception("选择工作流文件失败")
            return {"ok": False, "msg": str(e)}

    def import_workflows(
        self,
        paths: List[str],
        workspace_root: str = "",
        scope: str = "project",
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """导入用户选择的工作流文件（.dwf.ts）到指定档位。"""
        try:
            from core.workflows.store import import_workflow_file

            if scope not in ("project", "user"):
                scope = "project"
            root = str(workspace_root or "").strip() or None
            imported: List[Dict[str, Any]] = []
            failed: List[Dict[str, Any]] = []
            for raw in list(paths or [])[:50]:
                result = import_workflow_file(
                    raw, scope=scope, workspace_root=root, overwrite=bool(overwrite)
                )
                if result.get("ok"):
                    imported.append(
                        {"name": result["name"], "scope": scope, "path": result["path"]}
                    )
                else:
                    failed.append(
                        {
                            "name": str(raw).rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                            "error": str(result.get("error", "")),
                            "exists": bool(result.get("exists")),
                        }
                    )
            return {"ok": True, "imported": imported, "failed": failed}
        except Exception as e:
            logger.exception("导入工作流文件失败")
            return {"ok": False, "msg": str(e)}

    def save_workflow(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """新建 / 更新工作流（GUI 表单与对话创建共用）。"""
        try:
            if self._shutting_down:
                return {"ok": False, "errors": ["应用正在退出"]}
            from core.workflows.store import save_workflow

            data = payload or {}
            error = save_workflow(
                str(data.get("name") or "").strip(),
                description=str(data.get("description") or ""),
                when_to_use=str(data.get("when_to_use") or ""),
                args=data.get("args") if isinstance(data.get("args"), dict) else {},
                body=str(data.get("body") or ""),
                scope=str(data.get("scope") or "project"),
                workspace_root=str(data.get("workspace_root") or "").strip() or None,
                overwrite=bool(data.get("overwrite")),
            )
            if error:
                return {"ok": False, "errors": [error]}
            return {
                "ok": True,
                "name": str(data.get("name") or "").strip(),
                "scope": str(data.get("scope") or "project"),
            }
        except Exception as e:
            logger.exception("保存工作流失败")
            return {"ok": False, "errors": [str(e)]}

    def delete_workflow(
        self, name: str, scope: str, workspace_root: str = ""
    ) -> Dict[str, Any]:
        try:
            from core.workflows.store import delete_workflow

            error = delete_workflow(
                str(name or "").strip(),
                str(scope or "project"),
                str(workspace_root or "").strip() or None,
            )
            if error:
                return {"ok": False, "msg": error}
            return {"ok": True}
        except Exception as e:
            logger.exception("删除工作流失败")
            return {"ok": False, "msg": str(e)}

    def get_workflow_runs(self, name: str = "", limit: int = 20) -> Dict[str, Any]:
        """工作流运行台账：name 为空返回全部工作流的最近运行。"""
        try:
            storage = getattr(self, "_workflow_run_storage", None)
            if storage is None:
                return {"ok": True, "runs": []}
            runs = storage.list_runs(str(name or ""), limit=max(1, min(int(limit or 20), 200)))
            return {"ok": True, "runs": runs}
        except Exception as e:
            logger.exception("读取工作流运行历史失败")
            return {"ok": False, "msg": str(e)}

    def clear_workflow_runs(self, name: str) -> Dict[str, Any]:
        try:
            storage = getattr(self, "_workflow_run_storage", None)
            if storage is None:
                return {"ok": True}
            storage.clear_runs(str(name or ""))
            return {"ok": True}
        except Exception as e:
            logger.exception("清空工作流运行历史失败")
            return {"ok": False, "msg": str(e)}

    def stop_current_turn(self, session_name: str = "") -> Dict[str, Any]:
        try:
            target = session_name or self._current_session
            evt = self._cancel_events.get(target)
            if evt is None:
                logger.warning(
                    "停止请求未命中活跃回合：target=%s，活跃会话=%s",
                    target,
                    sorted(self._cancel_events.keys()),
                )
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
                sec_lock, sec_pending = self._get_secretary_summary_state()
                with sec_lock:
                    sec_pending.discard(target)
            except Exception:
                pass
            try:
                from core.plan import suppress_plan_auto_continue
                suppress_plan_auto_continue(target)
            except Exception:
                pass
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
            # idle=True：该会话本就没有活跃回合，前端据此立即解锁运行态。
            # 保留 ok=True 兼容旧前端（它们忽略这个字段，行为与以前一致）。
            return {"ok": True, "idle": evt is None}
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
        return self._run_turn_worker(user_text, session_name, *args, **kwargs)

    def _run_turn_worker(self, user_text: str, session_name: str, internal_kind: str = "", display_text: str = "", pending_workdir: str = "", turn_source: str = "local", attachments: Optional[List[str]] = None, automation_context: Optional[Dict[str, str]] = None, pending_model_id: str = "", pending_permission_mode: str = "", secretary_dispatch_context: Optional[Dict[str, str]] = None):
        api_key = ""
        turn_finished = False
        event_session_name = session_name
        # 自动改名（首轮标题化）后队列已随迁新名，finally 末尾补发必须用新名，
        # 否则按旧名 drain 扑空、排队消息滞留（旧名还会被 ensure_session 成幽灵会话）
        drain_session_name = session_name
        cancelled = False
        turn_id = ""
        automation_run_id = str((automation_context or {}).get("run_id") or "")
        automation_task_name = str((automation_context or {}).get("task_name") or "")
        automation_permission_mode = str((automation_context or {}).get("permission_mode") or "")
        automation_error: Optional[str] = None
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
        # 线程本地自动化回合标记作用域（G3 防任务自我繁殖）：与权限档位同理，
        # 回合内标记本回合来源，退出时恢复，防止线程复用时残留污染普通回合
        _automation_scope = None
        error_state = {
            "toast_messages": set(),
            "terminal_message": None,
        }
        cancel_event = self._cancel_events.get(session_name) or self._get_cancel_event(session_name)

        _phase_t0 = time.monotonic()
        _phase_last = _phase_t0
        _turn_watchdog_stop: Optional[threading.Event] = None
        _FINISH_WATCHDOG_SECONDS = 5.0
        # 回合无进展看门狗：默认 30 分钟收不到任何事件 / 子代理心跳即判定卡死。
        # 阈值刻意远大于单次工具上限（execute_command 600s、权限确认 300s），
        # 只兜底「线程卡在不可取消的同步调用上」这类停止按钮也无效的极端情况。
        _stall_stop: Optional[threading.Event] = None

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
            if _stall_stop is not None:
                _stall_stop.set()
            _js_queue = getattr(self, "_js_queue", None)
            logger.info(
                "回合计时 [%s] turn_finish 已入队：finish_once 内 %.0fms（JS 队列剩余 %d 条）",
                event_session_name,
                (time.monotonic() - _finish_t0) * 1000.0,
                _js_queue.qsize() if _js_queue is not None else -1,
            )
            self._emit_token_usage()
            self._clear_session_last_error(session_name)

        def _start_stall_watchdog() -> None:
            nonlocal _stall_stop
            if _stall_stop is not None:
                return
            _stall_stop = threading.Event()
            _stop = _stall_stop

            def _watch() -> None:
                nonlocal undo_finished
                while not _stop.wait(_STALL_POLL_SECONDS):
                    if _stop.is_set() or turn_finished:
                        return
                    idle = self._turn_idle_seconds(session_name)
                    reason = _force_finish_reason(idle, cancel_event.is_set())
                    if reason is None:
                        continue
                    if reason == "stop":
                        # 用户已经点过停止：这条路径下更要快速收尾，不能再等 30 分钟
                        logger.warning(
                            "停止看门狗 [%s]：用户已请求停止，但 %.0f 秒无任何进展仍未退出，强制收尾",
                            session_name, idle,
                        )
                    else:
                        logger.warning(
                            "回合无进展看门狗 [%s]：已 %.0f 秒无任何进展，强制收尾",
                            session_name, idle,
                        )
                    # 卡死现场线程栈：定位阻塞在哪个不可取消的调用上
                    try:
                        import sys as _sys
                        import traceback as _tb

                        for _tid, _frame in _sys._current_frames().items():
                            logger.warning(
                                "线程栈 tid=%s：\n%s", _tid, "".join(_tb.format_stack(_frame))
                            )
                    except Exception:
                        logger.exception("回合无进展看门狗抓取线程栈失败")
                    try:
                        cancel_event.set()
                        from core.llm import abort_inflight_streams

                        abort_inflight_streams(cancel_event)
                    except Exception:
                        logger.debug("看门狗取消在飞请求失败", exc_info=True)
                    try:
                        _stalled_by_stop = reason == "stop"
                        self.emit_event("turn_error", {
                            "message": (
                                f"已请求停止，但回合 {int(_STOP_WATCHDOG_SECONDS)} 秒内没有任何进展，"
                                "判定为卡在不可中断的同步调用上，已强制结束并解锁会话。"
                                "完整线程栈已写入运行日志。"
                                if _stalled_by_stop
                                else
                                f"本回合已连续 {int(_STALL_TIMEOUT_SECONDS // 60)} 分钟没有任何进展，"
                                "判定为卡死，已强制结束并解锁会话。"
                                "常见原因是工具阻塞在系统弹窗或不可中断的同步调用上，"
                                "完整线程栈已写入运行日志。"
                            ),
                            "session_name": session_name,
                        })
                    except Exception:
                        logger.debug("看门狗推送 turn_error 失败", exc_info=True)
                    if undo_ctx is not None and not undo_finished:
                        try:
                            from core.turn_undo import service as _tu_service

                            _tu_service.finish_turn(
                                undo_ctx,
                                aborted=True,
                                reason="回合长时间无进展，看门狗强制结束",
                            )
                            undo_finished = True
                        except Exception:
                            logger.exception("看门狗封闭按回合撤销检查点失败")
                    try:
                        if turn_id:
                            record = self._memory.get_harness_turn(turn_id) or {}
                            if record.get("status") in (
                                "created", "preparing", "running", "cancelling",
                            ):
                                self._memory.transition_harness_turn(
                                    turn_id,
                                    str(record.get("status")),
                                    "failed",
                                    int(time.time() * 1000),
                                    error_code="TurnStalled",
                                )
                    except Exception:
                        logger.debug("看门狗回写 harness 回合终态失败", exc_info=True)
                    try:
                        if self._cancel_events.get(session_name) is cancel_event:
                            self._cancel_events.pop(session_name, None)
                        self._active_turns.pop(session_name, None)
                        self._active_turn_ids.pop(session_name, None)
                        if turn_id:
                            with self._turn_owner_lock:
                                self._turn_owner_sessions.pop(turn_id, None)
                        with self._active_turn_lock:
                            self._active_turn_snapshots.pop(session_name, None)
                        with self._turn_activity_lock:
                            self._turn_activity.pop(session_name, None)
                    except Exception:
                        logger.debug("看门狗清理回合运行态失败", exc_info=True)
                    _stop.set()
                    finish_once()
                    return

            threading.Thread(
                target=_watch, daemon=True, name="gui-turn-stall-watchdog"
            ).start()

        self._clear_session_last_error(session_name)
        turn_start_data = {"user_text": user_text, "session_name": session_name}
        if display_text and display_text != user_text:
            turn_start_data["display_text"] = display_text
        if internal_kind:
            turn_start_data["internal"] = internal_kind
            if internal_kind == "automation":
                turn_start_data["display_text"] = display_text or "星轨自动化任务"
            elif internal_kind == "bg_command_summary":
                turn_start_data["display_text"] = _BG_COMMAND_SUMMARY_DISPLAY_TEXT
            elif internal_kind == "secretary_dispatch_summary":
                turn_start_data["display_text"] = (
                    display_text or _SECRETARY_DISPATCH_SUMMARY_DISPLAY_TEXT
                )
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
                error_state["terminal_message"] = "当前未激活任何 LLM 模型"
                self.emit_event("error", {"message": "当前未激活任何 LLM 模型，请先在模型管理中配置并激活模型。", "session_name": session_name})
                self.emit_event("turn_cancelled", {"session_name": session_name})
                finish_once()
                return

            model_name = active_model["litellm_model"]
            api_key = active_model.get("api_key", "")
            api_base = active_model.get("base_url", "")
            ctx_win = active_model.get("context_window", 128000)
            # 本回合实际发请求所用的模型 id（= 会话激活模型）。
            model_id = active_model.get("id")
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
            self._sync_active_model_context(session_name)

            # 会话归属目录是每一轮 Agent 的权威工作目录。待发送项目会话虽然
            # 已把 workdir 透传到这里，但进程 cwd 仍可能停在启动目录或上一会话；
            # 必须在解析附件、组装运行上下文和执行任何工具前完成绑定。
            turn_workdir = session_workdir
            if turn_workdir == "GLOBAL":
                # 自由对话无具体项目目录：用独立工作区根（不再兜底进程 cwd，
                # 否则会与启动目录下的项目会话抢同一个工作区锁域）
                turn_workdir = _global_workspace_root()
            else:
                turn_workdir = _resolve_real_path(turn_workdir)
                if not os.path.isdir(turn_workdir):
                    raise FileNotFoundError(f"会话项目文件夹不存在: {turn_workdir}")
            from core.workspace_context import workspace_scope
            _workdir_scope = workspace_scope(turn_workdir)
            _workdir_scope.__enter__()
            self._memory.set_workdir(turn_workdir)
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
            # 自动化回合标记（G3 防任务自我繁殖）：与权限档位同一生命周期，
            # 供工具 schema 层与执行门控判定「本回合由星轨自动化派发」。
            _automation_scope = app_context.automation_turn_scope(
                automation_run_id or None
            )
            _automation_scope.__enter__()

            try:
                from core.input_parser import parse_user_input, extract_session_refs
                if internal_kind:
                    parsed_content, attached_msgs = user_text, []
                    session_refs = []
                else:
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
                        try:
                            from core.agent_service import describe_session_ref_skips
                            attached_msgs = list(attached_msgs) + describe_session_ref_skips(
                                self._memory, session_refs, session_name
                            )
                        except Exception:
                            logger.debug("引用失败提示生成失败（不影响回合）", exc_info=True)
            except Exception:
                logger.debug("解析用户输入失败，退回纯文本", exc_info=True)
                parsed_content, attached_msgs = user_text, []
                session_refs = []
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
                    "session_name": session_name,
                }
                try:
                    self._automation_storage.set_run_turn_id(automation_run_id, turn_id)
                except Exception:
                    logger.debug("回填自动化运行 turn_id 失败", exc_info=True)
            from core.turn_undo import service as _tu_service

            _tu_checkpoint, undo_ctx = _tu_service.begin_turn(
                session_id=_tu_service.resolve_session_id(session_name),
                turn_id=turn_id,
                workspace_root=turn_workdir,
                description=str(user_text)[:80],
            )
            # 检查点已建立 → 启动无进展看门狗。放在这里而不是更早：看门狗收尾
            # 需要 undo_ctx；更早的启动阶段卡住由外层异常路径兜底封闭检查点。
            _start_stall_watchdog()
            self._touch_turn_activity(session_name)
            self._memory.add_message(
                session_name, "user", parsed_content,
                turn_checkpoint_id=_tu_checkpoint.checkpoint_id,
                turn_position_kind="user",
            )
            turn_start_data["turn_id"] = turn_id
            turn_start_data["session_id"] = _tu_checkpoint.session_id
            # 用户消息的数据库 id 一并下发：前端乐观渲染的气泡靠它补上 _msg_id，
            # 「编辑已发送消息」按 id 定位——不回填的话刚发出的这条要等切会话
            # 重载历史才拿得到 id，表现为「刚发的不能编辑、旧消息可以」。
            # 这是可选增强：memory 实现缺该方法时跳过，绝不能让回合 worker 崩掉
            get_last_id = getattr(self._memory, "get_last_message_id", None)
            if callable(get_last_id):
                try:
                    user_msg_id = get_last_id(session_name)
                except Exception:
                    logger.debug("取用户消息 id 失败（不影响回合）", exc_info=True)
                    user_msg_id = 0
                if user_msg_id:
                    turn_start_data["user_msg_id"] = int(user_msg_id)
            self.emit_event("turn_start", turn_start_data)
            if attached_msgs:
                self.emit_event("user_attachment_notes", {
                    "session_name": session_name,
                    "notes": [str(note) for note in attached_msgs],
                })
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

            from core.agent_service import prepare_turn_messages
            history = prepare_turn_messages(
                self._memory,
                session_name,
                model_name,
                ctx_win,
                permission_mode=automation_permission_mode or self._effective_permission_mode(session_name),
                cancel_event=cancel_event,
                notify=self._notice_emitter(session_name),
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
                    model_id=model_id,
                    error_state=error_state,
                    turn_source=turn_source,
                )
            finally:
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
                if undo_ctx is not None and not undo_finished:
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
                drain_session_name = event_session_name
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
            if _automation_scope is not None:
                _automation_scope.__exit__(None, None, None)
            # 身份校验清理 cancel 事件：finally 竞态窗口内（本线程还没走完
            # finally，但 _active_turns 已 pop）新回合可能已建立同名事件，
            # 仅当字典里仍是本回合自己的事件对象时才删除，防误删新回合事件
            if self._cancel_events.get(session_name) is cancel_event:
                self._cancel_events.pop(session_name, None)
            self._active_turns.pop(session_name, None)
            self._active_turn_ids.pop(session_name, None)
            if turn_id:
                with self._turn_owner_lock:
                    self._turn_owner_sessions.pop(turn_id, None)
            with self._active_turn_lock:
                self._active_turn_snapshots.pop(session_name, None)
            self._finalize_session_deltas(session_name)
            _phase_mark("delta 缓冲收尾完成")
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
                try:
                    notify_success = (
                        str((automation_context or {}).get("notify_on_success") or "") == "1"
                    )
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
            if secretary_dispatch_context:
                try:
                    self._finalize_secretary_dispatch(
                        secretary_dispatch_context,
                        drain_session_name or session_name,
                        cancelled=bool(cancelled),
                        terminal_error=str(error_state.get("terminal_message") or ""),
                    )
                except Exception:
                    logger.debug("秘书派活完成通知推送失败", exc_info=True)
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
            try:
                sec_lock, sec_pending = self._get_secretary_summary_state()
                with sec_lock:
                    sec_has_pending = session_name in sec_pending
                    sec_pending.discard(session_name)
                if sec_has_pending and not self._shutting_down:
                    self._schedule_secretary_dispatch_wakeup(session_name)
            except Exception:
                logger.debug("秘书派活待汇报补检失败", exc_info=True)
            if not self._shutting_down:
                try:
                    idle_maintenance_service.on_turn_finished(
                        session_name,
                        # 与回合内/后台压缩共用同一发射器，避免再漏会话归属
                        notify=self._notice_emitter(session_name),
                    )
                except Exception:
                    pass
            self._drain_message_queue(drain_session_name)

    def _on_background_task_finished(self, task_id: str, command: str, returncode: int,
                                     session_id: str = "", log_tail: str = "") -> None:
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

        if session_id:
            threading.Thread(
                target=self._start_bg_command_summary,
                args=(session_id, command, returncode, log_tail),
                daemon=True,
                name="gui-bg-command-summary",
            ).start()

    def _start_bg_command_summary(self, session: str, command: str,
                                  returncode: int, log_tail: str) -> None:
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
        try:
            time.sleep(0.5)
            if self._shutting_down:
                return
            self._maybe_start_bg_summary_turn(session)
        except Exception:
            logger.debug("后台任务自动汇总触发失败", exc_info=True)

    def _maybe_start_bg_summary_turn(self, session: str) -> None:
        from core.tools import subagent_manager

        if self._shutting_down or not session:
            return
        # （getattr 兜底：测试构造的裸桥可能未走完整 __init__）
        if session in getattr(self, "_user_cancelled_sessions", ()):
            return
        if subagent_manager.has_running_background_tasks(session):
            return
        try:
            known = {
                str(s.get("name") or "")
                for s in self._memory.get_all_sessions(workdir=None)
            }
            if session not in known:
                return
        except Exception:
            logger.debug("后台汇总前会话存在性检查失败，继续原流程", exc_info=True)

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

    def _get_secretary_summary_state(self):
        """懒取秘书派活唤醒锁与挂起集合（兼容裸实例测试桩）。"""
        lock = getattr(self, "_secretary_summary_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._secretary_summary_lock = lock
        pending = getattr(self, "_secretary_summary_pending_sessions", None)
        if pending is None:
            pending = set()
            self._secretary_summary_pending_sessions = pending
        return lock, pending

    def _schedule_secretary_dispatch_wakeup(self, secretary_session: str) -> None:
        """在独立守护线程中延迟防抖后尝试唤醒秘书会话整理汇报派活结果。"""
        sec = str(secretary_session or "").strip()
        if (
            not sec
            or getattr(self, "_shutting_down", False)
            or not hasattr(self, "_turn_start_lock")
            or getattr(self, "_active_turns", None) is None
        ):
            return
        try:
            threading.Thread(
                target=self._deferred_secretary_dispatch_wakeup,
                args=(sec,),
                daemon=True,
                name="gui-secretary-dispatch-wakeup",
            ).start()
        except Exception:
            logger.debug("启动秘书派活唤醒线程失败", exc_info=True)

    def _deferred_secretary_dispatch_wakeup(self, secretary_session: str) -> None:
        try:
            time.sleep(0.15)
            if getattr(self, "_shutting_down", False):
                return
            self._maybe_start_secretary_dispatch_summary_turn(secretary_session)
        except Exception:
            logger.debug("秘书派活自动唤醒触发失败", exc_info=True)

    def _maybe_start_secretary_dispatch_summary_turn(self, secretary_session: str) -> None:
        sec = str(secretary_session or "").strip()
        if (
            not sec
            or getattr(self, "_shutting_down", False)
            or not hasattr(self, "_turn_start_lock")
            or getattr(self, "_active_turns", None) is None
            or getattr(self, "_cancel_events", None) is None
        ):
            return
        if sec in getattr(self, "_user_cancelled_sessions", ()):
            return
        memory = getattr(self, "_memory", None)
        if memory is None:
            return
        try:
            if str(memory.get_session_source(sec) or "").strip().upper() != "SECRETARY":
                return
            known = {
                str(s.get("name") or "")
                for s in (memory.get_all_sessions(workdir=None, include_empty=True, include_archived=False) or ())
            }
            if sec not in known:
                return
        except Exception:
            logger.debug("秘书唤醒前会话校验失败", exc_info=True)
            return

        try:
            active_model = get_active_model(sec)
        except Exception:
            active_model = None
        if not isinstance(active_model, dict) or not active_model.get("litellm_model"):
            return

        store = self._secretary_dispatch_store()
        if store is None:
            return

        from core.secretary_dispatches import format_secretary_dispatch_summary

        with self._turn_start_lock:
            if not store.has_unreported_wakeable(sec):
                return
            if self._is_turn_active(sec):
                sec_lock, sec_pending = self._get_secretary_summary_state()
                with sec_lock:
                    sec_pending.add(sec)
                return
            rows = store.claim_unreported(sec)
            if not rows:
                return
            summary_body = format_secretary_dispatch_summary(rows)
            if not summary_body:
                return
            wakeup_prompt = (
                "<task-notification>\n"
                f"{summary_body}\n\n"
                "请按照调度协议，向用户简洁清晰地汇报上述派发任务的最新结果"
                "（说明已完成结论、失败原因或待决策事项；已被用户取消的任务严禁擅自重试）。\n"
                "</task-notification>"
            )
            cancel_event = threading.Event()
            self._cancel_events[sec] = cancel_event
            worker = threading.Thread(
                target=self._run_turn_worker_serialized,
                args=(
                    wakeup_prompt,
                    sec,
                    "secretary_dispatch_summary",
                    _SECRETARY_DISPATCH_SUMMARY_DISPLAY_TEXT,
                ),
                daemon=True,
                name=f"gui-sec-dispatch-summary-{sec}",
            )
            self._active_turns[sec] = worker
            worker.start()

    @staticmethod
    def _format_subagent_live_detail(snap: Any) -> str:
        stage = str(getattr(snap, "stage", "") or "")
        if stage == "waiting_approval":
            approval = str(getattr(snap, "approval_summary", "") or "").strip()
            if approval:
                return approval[:80]
        if stage == "thinking":
            for note in reversed(getattr(snap, "round_notes", ()) or ()):
                thinking = str(note.get("thinking") or "").strip()
                if thinking:
                    lines = [ln.strip() for ln in thinking.splitlines() if ln.strip()]
                    if lines:
                        return lines[-1][:80]
        if stage == "tool":
            records = getattr(snap, "tool_records", ()) or ()
            running = next((r for r in reversed(records) if r.get("status") == "running"), None)
            rec = running or (records[-1] if records else None)
            if rec:
                action = str(rec.get("action") or rec.get("name") or "").strip()
                target = str(rec.get("target") or "").strip()
                joined = f"{action} {target}".strip() if target else action
                if joined:
                    return joined[:80]
        if stage == "composing":
            partial = str(getattr(snap, "partial_result", "") or "").strip()
            if partial:
                lines = [ln.strip() for ln in partial.splitlines() if ln.strip()]
                if lines:
                    return lines[-1][:80]
        stages = getattr(snap, "stages", ()) or ()
        return str(stages[-1].detail if stages else "")

    def _subagent_progress_poller(self, session_name: str, stop: threading.Event) -> None:
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
                    last_detail = self._format_subagent_live_detail(snap)
                    tasks.append({
                        "id": snap.task_id,
                        "description": snap.description,
                        "type": snap.subagent_type,
                        "status": snap.status,
                        "stage": snap.stage,
                        "stage_detail": last_detail,
                        "elapsed": elapsed,
                        "background": bool(snap.run_in_background),
                        "tool_call_id": snap.tool_call_id,
                        "stages": [
                            {"name": s.name, "at": s.at, "detail": s.detail}
                            for s in snap.stages
                        ],
                    })
                # 没有任务且上一轮也没有任务时不推事件，避免空轮询刷屏
                if tasks or had_tasks:
                    had_tasks = bool(tasks)
                    if tasks:
                        self._touch_turn_activity(session_name)
                    self.emit_event("subagent_progress", {"tasks": tasks, "session_name": session_name})
            except Exception:
                logger.debug("推送子代理进度失败", exc_info=True)
            if stopped:
                break

    def _emit_owned_error(self, message, api_key, error_state, *, terminal, session_name=""):
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
        parent_turn_id=None,
        request_reason="",
        resume_turn=False,
    ) -> bool:
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
            parent_turn_id=parent_turn_id or turn_id,
            permission_owner=turn_id,
            permission_source="主代理",
            model_id=model_id,
            computer_turn_source=turn_source,
            request_reason=request_reason,
            resume_turn=resume_turn,
        ):
            self._touch_turn_activity(session_name)
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
                message = str(data.get("reason") or "")
                if message:
                    self.emit_event("notice", {
                        "message": _strip_ansi_markup(message),
                        "session_name": session_name,
                    })
            elif kind == "plan_item_handoff":
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

    def _run_resume_turn_worker(
        self, session_name: str, *, turn_id: str, parent_turn_id: str
    ) -> None:
        from core.agent_service import build_resume_messages
        from core.plan import set_current_plan_session
        from core.workspace_context import workspace_scope

        error_state: Dict[str, Any] = {"toast_messages": set(), "terminal_message": None}
        cancel_event = self._get_cancel_event(session_name)
        api_key = ""
        workdir_scope = None
        permission_scope = None
        finished = False

        def finish_once() -> None:
            """turn_finish 只发一次：前端输入框解锁依赖它，任何路径都不能漏。"""
            nonlocal finished
            if finished:
                return
            finished = True
            try:
                self._emit_message_final(session_name)
            except Exception:
                logger.debug("断点续跑 message_final 补发失败", exc_info=True)
            self.emit_event("turn_finish", {"session_name": session_name})
            # 续跑写入的是新的 assistant 消息：窗口占用必须重算后再推面板，
            # 否则上下文圆圈停在父回合中断时的数字。来源占比快照保留不清
            # （clear_breakdown_snapshot=False）：清空会让面板数据变空白
            try:
                self._recalc_token_usage(session_name, clear_breakdown_snapshot=False)
            except Exception:
                logger.debug("断点续跑 token 用量重算失败", exc_info=True)
            self._emit_token_usage()
            self._clear_session_last_error(session_name)

        self._active_turns[session_name] = threading.current_thread()
        self._active_turn_ids[session_name] = turn_id
        with self._turn_owner_lock:
            self._turn_owner_sessions[turn_id] = session_name
        try:
            active_model = get_active_model(session_name)
            if not active_model or not active_model.get("litellm_model"):
                self.emit_event("error", {
                    "message": "当前未激活任何 LLM 模型，请先在模型管理中配置并激活模型。",
                    "session_name": session_name,
                })
                self.emit_event("notice", {
                    "message": "断点续跑未执行：当前未激活任何 LLM 模型。",
                    "session_name": session_name,
                })
                return

            model_name = active_model["litellm_model"]
            api_key = active_model.get("api_key", "") or ""
            api_base = active_model.get("base_url", "") or ""
            model_id = active_model.get("id")

            # 线程隔离作用域：与普通回合同一套（工作目录 / 会话路由 / 权限档位）。
            # 缺任一项都会让续跑回合里的工具调用读到别的会话或全局档位。
            turn_workdir = self._session_workdir(session_name)
            if turn_workdir == "GLOBAL":
                turn_workdir = _global_workspace_root()
            else:
                turn_workdir = _resolve_real_path(turn_workdir)
            workdir_scope = workspace_scope(turn_workdir)
            workdir_scope.__enter__()
            self._memory.set_workdir(turn_workdir)
            app_context.set_thread_session(session_name)
            set_current_plan_session(session_name)
            permission_scope = app_context.permission_mode_scope(
                self._effective_permission_mode(session_name)
            )
            permission_scope.__enter__()
            self._sync_active_model_context(session_name)

            # 只读组装：稳定系统提示词 + 库中已定型的 LLM 历史（不做水位线维护、
            # 不追加 user 消息、不重写字节），前缀与父回合逐字节相同。
            history = build_resume_messages(self._memory, session_name, is_gui=True)

            with self._active_turn_lock:
                self._active_turn_snapshots[session_name] = {
                    "session_name": session_name,
                    "user_text": "",
                    "display_text": "断点续跑：从上次中断处继续",
                    "started_at_ms": int(time.time() * 1000),
                    "thinking_text": "",
                    "streaming_text": "",
                    "tool_calls": [],
                    "internal_kind": "resume",
                }
            self.emit_event("turn_start", {
                "user_text": "",
                "display_text": "断点续跑：从上次中断处继续",
                "internal": "resume",
                "turn_id": turn_id,
                "session_name": session_name,
            })

            cancelled = self._consume_turn_events(
                history,
                model_name,
                api_key,
                api_base,
                session_name,
                turn_id,
                model_id=model_id,
                error_state=error_state,
                parent_turn_id=parent_turn_id,
                request_reason="resume",
                resume_turn=True,
            )
            if cancelled:
                self.emit_event("turn_cancelled", {"session_name": session_name})
            terminal_error = str(error_state.get("terminal_message") or "")
            if terminal_error:
                self.emit_event("notice", {
                    "message": f"断点续跑已中止：{terminal_error[:200]}",
                    "session_name": session_name,
                })
        except Exception as e:
            logger.exception("断点续跑回合失败")
            # 异常绝不静默：终态事件让前端退出 running，notice 给出可读原因
            self._emit_owned_error(
                str(e), api_key, error_state, terminal=True, session_name=session_name
            )
            self.emit_event("notice", {
                "message": f"断点续跑失败：{e}",
                "session_name": session_name,
            })
        finally:
            try:
                from core.plan import set_current_plan_session as _unbind_plan_route

                _unbind_plan_route(None)
            except Exception:
                pass
            app_context.set_thread_session(None)
            if workdir_scope is not None:
                workdir_scope.__exit__(None, None, None)
            if permission_scope is not None:
                permission_scope.__exit__(None, None, None)
            if self._cancel_events.get(session_name) is cancel_event:
                self._cancel_events.pop(session_name, None)
            self._active_turns.pop(session_name, None)
            self._active_turn_ids.pop(session_name, None)
            with self._turn_owner_lock:
                self._turn_owner_sessions.pop(turn_id, None)
            with self._active_turn_lock:
                self._active_turn_snapshots.pop(session_name, None)
            self._finalize_session_deltas(session_name)
            finish_once()

    def _session_workdir(self, session_name: str) -> str:
        try:
            for s in self._memory.get_all_sessions(workdir=None, include_empty=True):
                if s.get("name") == session_name:
                    return s.get("workdir") or _resolve_real_path(os.getcwd())
        except Exception:
            pass
        return _resolve_real_path(os.getcwd())

    def _derive_title_from_first_user_msg(self, session_name: str) -> Optional[str]:
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
        from core.input_parser import sanitize_session_title
        first_text = sanitize_session_title(first_text)
        return first_text[:12] if first_text else None

    def _rename_session_to_title(
        self,
        session_name: str,
        base_title: str,
        *,
        holds_turn_start_lock: bool = False,
        title_source: Optional[str] = None,
        require_untitled: bool = False,
    ) -> str:
        existing = {s.get("name") for s in self._memory.get_all_sessions(workdir=None)}
        # 自动命名兜底标题可能与当前名相同（起步截断名），不能把自己当重名
        existing.discard(session_name)
        new_title = base_title
        suffix = 2
        while new_title in existing:
            new_title = f"{base_title}({suffix})"
            suffix += 1
        if require_untitled:
            # AI 总结命名专用：条件更新在同一事务内判定 title_source，
            # 用户已手动改名时原子失败，绝不覆盖用户输入
            success, final_name = self._memory.rename_session_if_untitled(
                session_name, new_title
            )
        else:
            success, final_name = self._memory.rename_session(
                session_name, new_title, title_source=title_source
            )
        if success:
            if self._current_session == session_name:
                self._set_current_session(final_name)
            self._migrate_session_aux_state(
                session_name, final_name, holds_turn_start_lock=holds_turn_start_lock
            )
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

    def _claim_title_job(self, session_name: str) -> bool:
        lock = getattr(self, "_pending_title_lock", None)
        pending = getattr(self, "_pending_title_sessions", None)
        if lock is None or pending is None:
            self._pending_title_lock = lock = threading.Lock()
            self._pending_title_sessions = pending = set()
        with lock:
            if session_name in pending:
                return False
            pending.add(session_name)
            return True

    def _release_title_job(self, session_name: str) -> None:
        lock = getattr(self, "_pending_title_lock", None)
        pending = getattr(self, "_pending_title_sessions", None)
        if lock is None or pending is None:
            return
        with lock:
            pending.discard(session_name)

    def _maybe_auto_rename(self, session_name: str) -> str:
        """首轮问答完成后异步把会话改成 AI 总结标题；立即返回原名。

        只要标题来源仍为空（未 AI 命名、未被手动改名）就会在回合结束时
        尝试一次；成功后 title_source 落为 'ai'，之后永不自动变化。
        模型调用放在 daemon 线程里，绝不阻塞回合收尾；落库走
        rename_session_if_untitled 的条件更新，与用户手动改名并发时不会覆盖。
        """
        if not session_name:
            return session_name
        if session_name in self._goal_runs_map():
            return session_name
        try:
            source = self._memory.get_session_title_source(session_name)
        except Exception:
            logger.debug("读取会话标题来源失败", exc_info=True)
            return session_name
        if source not in (None, "", "default"):
            return session_name
        if not self._claim_title_job(session_name):
            return session_name

        def _job():
            try:
                self._run_auto_title_job(session_name)
            except Exception:
                logger.debug("会话 AI 总结命名失败", exc_info=True)
            finally:
                self._release_title_job(session_name)

        threading.Thread(
            target=_job,
            name=f"session-title-{session_name}",
            daemon=True,
        ).start()
        return session_name

    def _run_auto_title_job(self, session_name: str) -> None:
        # 等当前回合彻底收尾（_active_turns 已清理）再改名，避免 stop/消息队列
        # 等按旧名键控的状态失联；最多等 5 秒，超时放弃，下一轮结束会再试
        for _ in range(25):
            if not self._is_turn_active(session_name):
                break
            time.sleep(0.2)
        else:
            return

        try:
            history = self._memory.get_history(session_name)
        except Exception:
            logger.debug("读取首轮对话失败", exc_info=True)
            return
        from core.session_title import message_text

        user_msgs = [m for m in history if m.get("role") == "user"]
        assistant_msgs = [m for m in history if m.get("role") == "assistant"]
        raw_user = message_text(user_msgs[0].get("content", "")) if user_msgs else ""
        raw_assistant = (
            message_text(assistant_msgs[0].get("content", "")) if assistant_msgs else ""
        )
        try:
            from core.agent_service import strip_context_suffix

            user_text = str(strip_context_suffix(raw_user)).strip()
        except Exception:
            user_text = str(raw_user).strip()

        title = None
        active = get_active_model(session_name) or {}
        model = str(active.get("litellm_model") or "")
        if model:
            from core.session_title import generate_session_title

            title = generate_session_title(
                model,
                str(active.get("api_key") or ""),
                str(active.get("base_url") or ""),
                user_text,
                str(raw_assistant).strip(),
            )
        if not title:
            # 兜底沿用旧的本地截断规则，保证首轮结束后总能定下名字
            title = self._derive_title_from_first_user_msg(session_name)
        if not title:
            return

        # 二次校验：等待模型返回期间用户可能已手动改名或又发起了下一轮
        try:
            source = self._memory.get_session_title_source(session_name)
        except Exception:
            return
        if source not in (None, "", "default"):
            return
        if self._is_turn_active(session_name):
            return
        self._rename_session_to_title(
            session_name, title, require_untitled=True
        )

    def get_token_usage_statistics(self, days: int = 30) -> Dict[str, Any]:
        if type(days) is not int or days not in (0, 1, 7, 30, 365):
            return {"ok": False, "msg": "统计范围仅支持 0、1、7、30、365 天"}
        range_days = days

        try:
            from datetime import date, timedelta

            end_date = date.today()
            if range_days == 0:
                rows = self._memory.query_token_usage_by_day_and_model(
                    "0000-01-01", end_date.isoformat()
                )
                start_date = end_date
                earliest = min(
                    (str(row.get("date") or "") for row in rows if row.get("date")),
                    default="",
                )
                if earliest:
                    try:
                        earliest_date = date.fromisoformat(earliest[:10])
                    except ValueError:
                        earliest_date = end_date
                    if earliest_date < start_date:
                        start_date = earliest_date
                range_days = (end_date - start_date).days + 1
            else:
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
                "total_prompt": int(process_usage.get("cumulative_prompt", 0) or 0),
                "total_completion": int(
                    process_usage.get("cumulative_completion", 0) or 0
                ),
                "total_cached": int(process_usage.get("cumulative_cached", 0) or 0),
                "session_totals": self._session_token_totals(self._current_session),
                "cache_lane_model": lane_model,
                "cache_lane_api_base": lane_api_base,
                "cache_keepalive_hot_pct": rounded(
                    (keepalive_usage.get("hot") or {}).get("rate")
                ),
                "cache_keepalive_session_pct": rounded(
                    (keepalive_usage.get("session_all") or {}).get("rate")
                ),
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

    def _session_token_baseline(
        self,
        session_name: str,
        active: Dict[str, Any],
        *,
        empty_is_zero: bool = False,
    ) -> int:
        """按会话当前绑定模型与路由重算上下文占用基线。"""
        from core.agent_service import _read_llm_history, get_session_token_baseline

        history = _read_llm_history(self._memory, session_name)
        if empty_is_zero and not history:
            return 0
        return get_session_token_baseline(
            self._memory,
            session_name,
            active.get("litellm_model", ""),
            history=history,
            api_base=active.get("base_url", ""),
        )

    def _recalc_token_usage(self, session_name: str, *, clear_breakdown_snapshot: bool = True):
        """切会话 / 清空历史后重算窗口占用（优先真实用量，无记录才估算兜底）。

        clear_breakdown_snapshot=False 供「回合组装后重算」路径使用：该路径下
        来源占比快照是组装刚写入的最新数据，绝不能被当作"旧会话残留"清掉
        （否则组装后推给前端的面板数据永远为空，2026-09-23 修复）。
        """
        try:
            from core.context_breakdown import clear_breakdown
            from core.llm import session_token_usage, token_usage_lock
            # 按会话取模型：切模型后重算必须用该会话新绑定模型的窗口
            active = get_active_model(session_name) or {}
            total = self._session_token_baseline(
                session_name, active, empty_is_zero=True
            )
            with token_usage_lock:
                session_token_usage["latest_total"] = total
                session_token_usage["latest_prompt"] = 0
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
            from core.agent_service import SUMMARIZE_RATIO
            total = self._session_token_baseline(session_name, active)
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
                        notify=self._notice_emitter(session_name),
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

    def _build_skill_list(self) -> List[Dict[str, Any]]:
        try:
            active_ids = set(get_active_skill_ids())
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
        try:
            name = os.path.basename(os.path.dirname(str(skill_id or "")))
            if not name:
                # skill_id 为空或路径非法时说明原因即可，不必走到 core 层
                return {"ok": False, "msg": "技能标识无效，无法定位技能目录"}
            result = restore_builtin_skill(name)
            if not result.get("ok"):
                return {"ok": False, "msg": str(result.get("msg") or "恢复内置版本失败")}
            return {"ok": True, "skills": self._build_skill_list()}
        except Exception as e:
            logger.exception("restore_skill 失败")
            return {"ok": False, "msg": str(e)}

    def delete_skill(self, skill_id: str) -> Dict[str, Any]:
        """删除技能目录（技能页垃圾桶）。文件从磁盘移除，不回收安装包里的内置源。"""
        try:
            result = delete_skill(skill_id)
            if not result.get("ok"):
                return {"ok": False, "msg": str(result.get("msg") or "删除技能失败")}
            return {"ok": True, "msg": result.get("msg") or "已删除技能", "skills": self._build_skill_list()}
        except Exception as e:
            logger.exception("delete_skill 失败")
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

    def get_plugins(self) -> List[Dict[str, Any]]:
        try:
            from core.plugin_loader import get_plugin_infos, refresh_plugins
            # 增量扫盘：运行期间新安装/删除的插件无需重启即可反映到列表
            # （此前只在进程启动时扫一次，Agent 自己装的插件必须重启才显示）
            refresh_plugins()
            result = []
            for info in get_plugin_infos():
                status = str(info.get("status") or "")
                enabled = status in ("loaded", "pending_disable")
                result.append({
                    "name": str(info.get("name") or ""),
                    "version": str(info.get("version") or ""),
                    "description": str(info.get("description") or ""),
                    "source": str(info.get("source") or "builtin"),
                    "status": status,
                    "enabled": enabled,
                    "tools": list(info.get("tools") or []),
                    "has_settings": bool(info.get("has_settings")),
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
            try:
                settings = self.get_model_settings()
            except Exception:
                logger.exception("toggle_plugin 获取模型设置快照失败")
                settings = None
            return {
                "ok": ok,
                "msg": msg,
                "plugins": self.get_plugins(),
                "settings": settings,
            }
        except Exception as e:
            logger.exception("toggle_plugin 失败")
            return {"ok": False, "msg": str(e)}

    def delete_plugin(self, name: str) -> Dict[str, Any]:
        """插件页垃圾桶：用户插件物理删除，内置插件只记移除名单（不删随包文件，可恢复）。"""
        try:
            from core.plugin_loader import remove_plugin as _remove
            ok, msg = _remove(name)
            return {"ok": ok, "msg": msg, "plugins": self.get_plugins()}
        except Exception as e:
            logger.exception("delete_plugin 失败")
            return {"ok": False, "msg": str(e)}

    def restore_plugin(self, name: str) -> Dict[str, Any]:
        """恢复被移除的内置插件（清移除名单并热加载），返回最新插件快照。"""
        try:
            from core.plugin_loader import restore_plugin as _restore
            ok, msg = _restore(name)
            return {"ok": ok, "msg": msg, "plugins": self.get_plugins()}
        except Exception as e:
            logger.exception("restore_plugin 失败")
            return {"ok": False, "msg": str(e)}

    def trust_plugin(self, name: str) -> Dict[str, Any]:
        try:
            from core.plugin_loader import trust_plugin as _trust
            ok, msg = _trust(name)
            return {"ok": ok, "msg": msg, "plugins": self.get_plugins()}
        except Exception as e:
            logger.exception("trust_plugin 失败")
            return {"ok": False, "msg": str(e)}

    def get_plugin_settings(self, name: str) -> Dict[str, Any]:
        try:
            from core.plugin_loader import call_plugin_hook

            result = call_plugin_hook(name, "plugin_settings", {"action": "render"})
            if not result.get("ok"):
                return {
                    "ok": False,
                    "msg": str(result.get("message") or "插件设置页不可用"),
                    "needs": result.get("needs"),
                }
            data = result.get("data") or {}
            page = data.get("page") if isinstance(data, dict) else None
            if not isinstance(page, dict):
                return {"ok": False, "msg": "插件未返回有效的设置页内容", "needs": None}
            return {"ok": True, "msg": "", "needs": None, "page": page}
        except Exception as e:
            logger.exception("get_plugin_settings 失败")
            return {"ok": False, "msg": str(e), "needs": None}

    def run_plugin_settings_action(
        self, name: str, action: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """执行插件设置页上的一个动作，返回插件声明的 envelope（透传）。"""
        try:
            from core.plugin_loader import call_plugin_hook

            result = call_plugin_hook(
                name,
                "plugin_settings",
                {"action": str(action or ""), "params": params or {}},
            )
            if not result.get("ok"):
                return {
                    "ok": False,
                    "msg": str(result.get("message") or "插件动作不可用"),
                    "needs": result.get("needs"),
                }
            data = result.get("data") or {}
            if not isinstance(data, dict):
                return {"ok": False, "msg": "插件动作返回值无效", "needs": None}
            return {
                "ok": bool(data.get("ok", True)),
                "msg": str(data.get("message") or ""),
                "needs": None,
                "page": data.get("page"),
                "auth_url": data.get("auth_url"),
                "poll": data.get("poll"),
                "done": data.get("done"),
            }
        except Exception as e:
            logger.exception("run_plugin_settings_action 失败")
            return {"ok": False, "msg": str(e), "needs": None}

    def open_external_url(self, url: str) -> Dict[str, Any]:
        """在系统默认浏览器打开 http/https 链接（插件授权页等）；仅放行 http(s)。"""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(str(url or "").strip())
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return {"ok": False, "msg": "仅支持打开 http/https 链接"}
            import webbrowser

            opened = webbrowser.open(parsed.geturl())
            return {"ok": bool(opened), "msg": "" if opened else "系统浏览器未能打开链接"}
        except Exception as e:
            logger.exception("open_external_url 失败")
            return {"ok": False, "msg": str(e)}

    def _hook_workspace_root(self) -> str:
        # _session_workdir 已含查不到时回退 _resolve_real_path(os.getcwd()) 的逻辑；
        # GLOBAL 会话（自由对话）回合运行时用独立工作区根，这里同口径。
        session_workdir = self._session_workdir(self._current_session)
        if not session_workdir or session_workdir == "GLOBAL":
            return _global_workspace_root()
        return _resolve_real_path(session_workdir)

    def get_hooks(self) -> Dict[str, Any]:
        """列出全部 hook（用户级+项目级）与信任状态（C11 卡片 / C12 管理页共用）。"""
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

    def _mcp_tool_counts(self) -> Dict[str, int]:
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
                # 最近一次连接失败原因（无失败为 None），供界面提示排查
                "last_error": item.get("last_error"),
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

    def remove_mcp_server(self, name: str) -> Dict[str, Any]:
        """删除自定义 MCP 服务：删配置与重连放后台线程，避免阻塞界面。"""
        if self._mcp is None:
            return {"ok": False, "msg": "MCP 管理器不可用（依赖未安装）"}
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "msg": "服务名不能为空"}

        def _job():
            try:
                ok, msg = self._mcp.remove_server(name)
            except Exception as e:
                ok, msg = False, str(e)
            self.emit_event("mcp_status", {"mcps": self._build_mcp_list(), "ok": ok, "msg": msg})

        try:
            threading.Thread(target=_job, daemon=True, name="gui-mcp-remove").start()
            return {
                "ok": True,
                "msg": f"正在删除 {name}，完成后会自动刷新状态",
                "mcps": self._build_mcp_list(),
            }
        except Exception as e:
            logger.exception("remove_mcp_server 失败")
            return {"ok": False, "msg": str(e)}

    def reconnect_mcp(self) -> Dict[str, Any]:
        if self._mcp is None:
            return {"ok": False, "msg": "MCP 管理器不可用（依赖未安装）"}

        def _job():
            try:
                ok, msg = self._mcp.reconnect()
            except Exception as e:
                ok, msg = False, str(e)
            time.sleep(1.5)
            self.emit_event("mcp_status", {"mcps": self._build_mcp_list(), "ok": ok, "msg": msg})

        try:
            threading.Thread(target=_job, daemon=True, name="gui-mcp-reconnect").start()
            return {"ok": True, "msg": "已发起重连，完成后自动刷新状态", "mcps": self._build_mcp_list()}
        except Exception as e:
            logger.exception("reconnect_mcp 失败")
            return {"ok": False, "msg": str(e)}

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

    @staticmethod
    def _resume_switch_enabled() -> bool:
        try:
            section = config._load().get("resume") or {}
        except Exception:
            return False
        return bool(section.get("enabled")) if isinstance(section, dict) else False

    def _resume_make_planner(self):
        """装配规划层；默认注册表构造失败降级为 None（planner 会判工具不可用→BLOCK）。"""
        from core.harness.resume import ResumePlanner

        registry = None
        try:
            from core.tool_registry import build_default_registry

            registry = build_default_registry()
        except Exception:
            logger.warning("断点续跑：默认工具注册表构造失败，按无注册表降级", exc_info=True)
            registry = None
        return ResumePlanner(self._memory, registry)

    @staticmethod
    def _resume_call_snapshot(call: Any) -> Dict[str, Any]:
        """单个调用的可序列化快照：只含摘要与判定，绝不含参数/结果正文（红线 §9.3）。"""
        return {
            "call_ordinal": int(call.call_ordinal),
            "tool_name": call.tool_name,
            "klass": call.klass,
            "reason": call.reason,
            "step_id": call.step_id,
            "arguments_sha256": call.arguments_sha256,
            "result_sha256": call.result_sha256,
            "message_row_present": bool(call.message_row_present),
        }

    def _resume_plan_snapshot(self, planner: Any, plan: Any) -> Dict[str, Any]:
        return {
            "session_name": plan.session_name,
            "parent_turn_id": plan.parent_turn_id,
            "resume_from_step_ordinal": int(plan.resume_from_step_ordinal),
            "anchor": {
                "complete": plan.anchor.complete(),
                "message_count": int(plan.anchor.message_count),
                "messages_sha256": plan.anchor.messages_sha256,
                "system_sha256": plan.anchor.system_sha256,
                "tools_sha256": plan.anchor.tools_sha256,
                "cache_lane_sha256": plan.anchor.cache_lane_sha256,
            },
            "decisions": [self._resume_call_snapshot(call) for call in plan.decisions],
            "blockers": list(plan.blockers),
            "replayable_count": len(plan.replayable),
            "fresh_count": len(plan.fresh),
            "blocked_count": len(plan.blocked),
            "aborts": [str(reason) for reason in (planner.aborts(plan) or ())],
        }

    def resume_plan(self, session_name: str = "") -> Dict[str, Any]:
        """规划接口：只读 journal，返回可续跑快照；不发请求、不写库。"""
        try:
            if not self._resume_switch_enabled():
                return {"ok": False, "reason": "disabled"}
            planner = self._resume_make_planner()
            plan = planner.plan(session_name)
            if plan is None:
                return {
                    "ok": True,
                    "resumable": False,
                    "reason": "no_resumable_turn",
                    "aborts": [str(reason) for reason in (planner.aborts(None) or ())],
                }
            snapshot = self._resume_plan_snapshot(planner, plan)
            return {
                "ok": True,
                "resumable": not snapshot["aborts"],
                "plan": snapshot,
                "aborts": snapshot["aborts"],
            }
        except Exception as e:
            logger.exception("resume_plan 失败")
            return {"ok": False, "msg": f"断点续跑规划失败：{e}"}

    def _resume_journal_writer(self):
        from core.harness.journal import (
            PROCESS_OWNER_ID,
            HarnessJournal,
            journal_storage_supported,
        )

        if not journal_storage_supported(self._memory):
            return None
        return HarnessJournal(
            self._memory,
            PROCESS_OWNER_ID,
            clock_ms=lambda: time.time_ns() // 1_000_000,
        )

    def resume_execute(self, session_name: str = "", confirmed: Any = None) -> Dict[str, Any]:
        try:
            if not self._resume_switch_enabled():
                return {"ok": False, "reason": "disabled"}
            planner = self._resume_make_planner()
            plan = planner.plan(session_name)
            aborts = [str(reason) for reason in (planner.aborts(plan) or ())]
            if aborts:
                return {"ok": False, "reason": aborts[0], "reasons": aborts}
            from core.harness.resume import ResumeExecutor

            def _request_resume_turn(
                *, turn_id: str, parent_turn_id: str, request_reason: str = "resume"
            ) -> None:
                threading.Thread(
                    target=self._run_resume_turn_worker,
                    args=(session_name,),
                    kwargs={"turn_id": turn_id, "parent_turn_id": parent_turn_id},
                    daemon=True,
                    name=f"gui-resume-turn-{session_name}",
                ).start()

            executor = ResumeExecutor(
                self._memory,
                planner,
                writer=self._resume_journal_writer(),
                request_fn=_request_resume_turn,
            )
            return executor.run(plan, confirmed=set(confirmed or ()))
        except Exception as e:
            logger.exception("resume_execute 失败")
            return {"ok": False, "msg": f"断点续跑执行失败：{e}"}

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
            if not allowed and not _is_app_preview_artifact(real_path):
                return {"ok": False, "msg": deny_msg}
            if not os.path.isfile(real_path):
                if os.path.isdir(real_path):
                    return {"ok": False, "msg": "这是一个文件夹，不支持文本预览（可在侧栏文件树中浏览其内容）"}
                return {"ok": False, "msg": "文件不存在或不是普通文件"}
            size = os.path.getsize(real_path)
            ext = os.path.splitext(real_path)[1].lower()
            if ext in IMAGE_PREVIEW_MIME:
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
            if ext == ".docx":
                # docx 走前端 docx-preview 直接渲染（解析 OOXML 生成 DOM），不再由
                # core.office_preview 调 Word COM 转 PDF：省掉 10~25 秒的冷启动，
                # 也不要求本机装 Office/WPS。字节经 base64 data URL 过桥，所以按
                # 源文件大小卡上限；超出请走「在系统中打开」。
                if size > DOCX_PREVIEW_MAX_BYTES:
                    return {"ok": False, "msg": (
                        f"Word 文档过大（{size / 1024 / 1024:.1f} MB，"
                        f"上限 {DOCX_PREVIEW_MAX_BYTES // 1024 // 1024} MB），请使用「在系统中打开」"
                    )}
                # 扩展名不可信：见过「把 PDF 改名成 .docx」和「PDF 转 Word 工具写坏」
                # 两种情况。docx 本质是 ZIP（PK\x03\x04），不是的话 docx-preview 的
                # JSZip 会抛出英文的 "Can't find end of central directory"，用户看不懂。
                with open(real_path, "rb") as f:
                    head = f.read(8)
                if head.startswith(b"%PDF-"):
                    # 内容其实是 PDF：降级走下面的 pdf.js 分支，至少能正常看
                    ext = ".pdf"
                elif not head.startswith(b"PK\x03\x04"):
                    stem = os.path.splitext(real_path)[0]
                    sibling_pdf = stem + ".pdf"
                    hint = (
                        f"；同目录下的 {os.path.basename(sibling_pdf)} 是本来的 PDF，可以直接打开它"
                        if os.path.isfile(sibling_pdf) else ""
                    )
                    return {"ok": False, "msg": (
                        "这个 .docx 不是有效的 Word 文档（内容既不是 docx 压缩包也不是 PDF，"
                        f"常见于「PDF 转 Word」失败后留下的空壳文件）{hint}"
                    )}
                else:
                    import base64
                    with open(real_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                    return {
                        "ok": True,
                        "kind": "docx",
                        "data_url": f"data:{DOCX_PREVIEW_MIME};base64,{b64}",
                        "size": size,
                        "path": real_path,
                    }
            if ext == ".pdf":
                if size > PDF_PREVIEW_MAX_BYTES:
                    return {"ok": False, "msg": (
                        f"PDF 过大（{size / 1024 / 1024:.1f} MB，"
                        f"上限 {PDF_PREVIEW_MAX_BYTES // 1024 // 1024} MB），请使用「在系统中打开」"
                    )}
                import base64
                with open(real_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                return {
                    "ok": True,
                    "kind": "pdf",
                    "data_url": f"data:application/pdf;base64,{b64}",
                    "size": size,
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

    def save_pdf_edits(self, path: str, ops: list = None, mode: str = "copy", out_path: str = "") -> Dict[str, Any]:
        try:
            from core.gui_path_policy import gui_path_access_allowed
            from core.pdf_edit import apply_edits

            real_path = _resolve_real_path(path)
            workspace = self._session_workdir(self._current_session)
            allowed, deny_msg = gui_path_access_allowed(
                real_path, workspace, action="preview", session_id=self._current_session
            )
            if not allowed and not _is_app_preview_artifact(real_path):
                return {"ok": False, "msg": deny_msg}
            if not os.path.isfile(real_path):
                return {"ok": False, "msg": "文件不存在或不是普通文件"}
            if os.path.splitext(real_path)[1].lower() != ".pdf":
                return {"ok": False, "msg": "只支持保存 PDF 文件的编辑结果"}
            if not isinstance(ops, (list, tuple)):
                return {"ok": False, "msg": "编辑操作格式不正确（应为数组）"}

            import datetime

            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = os.path.splitext(real_path)[0]
            backup_path = ""
            if str(mode or "").lower() == "overwrite":
                backup_path = f"{stem}.bak_{stamp}.pdf"
                shutil.copy2(real_path, backup_path)
                target_path = real_path
            else:
                if out_path:
                    target_path = os.path.abspath(out_path)
                    # 输出路径同样要过路径策略：桥层可能被非常规调用，不能让
                    # 「保存编辑」变成往任意可写目录写文件的口子
                    out_ok, out_msg = gui_path_access_allowed(
                        target_path, workspace, action="preview",
                        session_id=self._current_session,
                    )
                    if not out_ok:
                        return {"ok": False, "msg": f"输出路径不允许: {out_msg}"}
                else:
                    target_path = f"{stem}_已编辑_{stamp}.pdf"

            result = apply_edits(real_path, list(ops), target_path)
            return {
                "ok": True,
                "path": os.path.abspath(target_path),
                "pages": int(result.get("pages", 0)),
                "backup": backup_path,
            }
        except Exception as exc:
            logger.exception("save_pdf_edits 失败")
            summary = str(exc).strip().splitlines()
            return {"ok": False, "msg": summary[0][:300] if summary else exc.__class__.__name__}

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

    def reveal_in_explorer(self, path: str) -> Dict[str, Any]:
        try:
            if not path:
                return {"ok": False, "msg": "路径为空"}
            real_path = _resolve_real_path(path)
            if not os.path.exists(real_path):
                return {"ok": False, "msg": f"路径不存在: {real_path}"}
            if os.path.isdir(real_path):
                opener = getattr(os, "startfile", None)
                if opener is not None:
                    opener(real_path)
                    return {"ok": True}
                import subprocess
                cmd = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([cmd, real_path])
                return {"ok": True}
            import subprocess
            if sys.platform == "win32":
                # 首选 Shell API：不经命令行解析，路径里有空格/中文都不会变形。
                # 退回命令行时必须是「整串命令」——用参数列表 subprocess 会给引号
                # 加转义，explorer 解析不到 /select, 的路径就退回打开「桌面」
                # （2026-11-06 用户实测踩到）。
                if _reveal_via_shell_api(real_path):
                    return {"ok": True}
                subprocess.Popen(f'explorer /select,"{real_path}"')
                return {"ok": True}
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", real_path])
                return {"ok": True}
            subprocess.Popen(["xdg-open", os.path.dirname(real_path)])
            return {"ok": True}
        except Exception as e:
            logger.exception("reveal_in_explorer 失败")
            return {"ok": False, "msg": str(e)}

    def preview_office_doc(self, path: str) -> Dict[str, Any]:
        try:
            from core.gui_path_policy import gui_path_access_allowed

            if not path:
                return {"ok": False, "msg": "路径为空"}
            real_path = _resolve_real_path(path)
            if not os.path.isfile(real_path):
                return {"ok": False, "msg": f"文件不存在: {real_path}"}
            workspace = self._session_workdir(self._current_session)
            allowed, deny_msg = gui_path_access_allowed(
                real_path, workspace, action="preview", session_id=self._current_session
            )
            if not allowed:
                return {"ok": False, "msg": deny_msg}
            try:
                from core.office_preview import (
                    OfficeConvertError, convert_to_pdf, is_office_path,
                )
            except Exception as e:
                logger.exception("preview_office_doc 导入 core.office_preview 失败")
                return {"ok": False, "msg": f"Office 预览能力不可用: {e}"}
            if not is_office_path(real_path):
                return {
                    "ok": False,
                    "msg": f"不是可预览的 Office 文档: {os.path.basename(real_path)}",
                }
            try:
                pdf_path = convert_to_pdf(real_path)
            except OfficeConvertError as e:
                return {"ok": False, "msg": str(e)}
            return {"ok": True, "path": str(pdf_path), "source": real_path}
        except Exception as e:
            logger.exception("preview_office_doc 失败")
            return {"ok": False, "msg": str(e)}

    def get_ui_layout(self) -> Dict[str, Any]:
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
            if len(b64) > 8 * 1024 * 1024 * 4 // 3 + 1024:
                return {"ok": False, "msg": "图片超过 8MB，无法粘贴"}
            raw = base64.b64decode(b64)
            paste_dir = os.path.join(tempfile.gettempdir(), "ycode_paste")
            os.makedirs(paste_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            ms = int(time.time() * 1000) % 1000
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
        out: List[Dict[str, Any]] = []
        try:
            clean = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
            for p in clean:
                try:
                    real = _resolve_real_path(str(p))
                    item = archive_media_file(real)
                    url = media_server.public_url(item["digest"], item["ext"])
                    if url:
                        out.append({
                            "path": str(p),
                            "url": url,
                            "width": item.get("width"),
                            "height": item.get("height"),
                        })
                except Exception:
                    logger.debug("附件缩略图归档失败: %s", p, exc_info=True)
                    continue
        except Exception:
            # 兜底：任何意外都不允许缩略图逻辑影响发送主流程
            logger.debug("get_attachment_thumbnails 整体异常", exc_info=True)
        return {"ok": True, "items": out}

    def store_dropped_paths(self, paths: List[str]) -> None:
        """pywebview 原生 drop 事件回调存储的完整路径，供前端 drop 时查询。"""
        self._dropped_paths = list(paths or [])

    UPLOAD_MAX_BYTES = 50 * 1024 * 1024

    def _save_bytes_to_temp(self, raw: bytes, ext: str) -> str:
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
        try:
            from core.tools import subagent_manager
            wanted = str(task_id or "").strip()
            session = str(session_name or "")
            now = time.time()
            tasks: List[Dict[str, Any]] = []
            target = None
            target_item: Dict[str, Any] | None = None
            for snap in subagent_manager.snapshot():
                elapsed = 0
                if snap.started_at:
                    elapsed = int((snap.finished_at or now) - snap.started_at)
                exec_elapsed = int(round(float(getattr(snap, "execution_elapsed", 0.0) or 0.0)))
                wait_elapsed = int(round(float(getattr(snap, "approval_wait_elapsed", 0.0) or 0.0)))
                changed_files = list(getattr(snap, "changed_files", ()) or ())
                item = {
                    "id": snap.task_id,
                    "description": snap.description,
                    "type": snap.subagent_type,
                    "status": snap.status,
                    "stage": snap.stage,
                    "stage_detail": self._format_subagent_live_detail(snap),
                    "elapsed": elapsed,
                    "execution_elapsed": exec_elapsed,
                    "approval_wait_elapsed": wait_elapsed,
                    "timeout_seconds": int(getattr(snap, "timeout_seconds", 600) or 600),
                    "started_at": snap.started_at,
                    "finished_at": snap.finished_at,
                    "background": bool(snap.run_in_background),
                    "model_name": str(getattr(snap, "model_name", "") or ""),
                    "rounds": int(getattr(snap, "rounds", 0) or 0),
                    "tool_ok": int(getattr(snap, "tool_success_count", 0) or 0),
                    "tool_err": int(getattr(snap, "tool_error_count", 0) or 0),
                    "changed_count": len(changed_files),
                }
                if not session or snap.parent_session == session:
                    tasks.append(item)
                if wanted and snap.task_id == wanted:
                    target = snap
                    target_item = item
            if not wanted:
                return {"ok": True, "task": None, "tasks": tasks}
            if target is None or target_item is None:
                return {"ok": False, "task": None, "tasks": tasks,
                        "msg": "未找到该子代理任务（可能已被清理）"}
            raw_result = str(target.result or "")
            raw_partial = str(target.partial_result or "")
            detail = {
                **target_item,
                "plan_item_id": target.plan_item_id,
                "handoff": str(getattr(target, "handoff", "") or ""),
                "approval_summary": str(getattr(target, "approval_summary", "") or ""),
                "stages": [
                    {"name": s.name, "at": s.at, "detail": s.detail}
                    for s in target.stages
                ],
                "tool_records": [
                    {
                        **dict(r),
                        **({"diff": dict(r["diff"])} if isinstance(r.get("diff"), dict) else {}),
                    }
                    for r in (getattr(target, "tool_records", ()) or ())
                ],
                "round_notes": [dict(n) for n in (getattr(target, "round_notes", ()) or ())],
                "file_diffs": [dict(d) for d in (getattr(target, "file_diffs", ()) or ())],
                "changed_files": list(getattr(target, "changed_files", ()) or ()),
                "result": raw_result[:12000],
                "result_truncated": len(raw_result) > 12000,
                "error": str(target.error or ""),
                "partial": raw_partial[:4000],
            }
            return {"ok": True, "task": detail, "tasks": tasks}
        except Exception as exc:
            logger.debug("get_subagent_task_detail 失败", exc_info=True)
            return {"ok": False, "task": None, "tasks": [], "msg": str(exc)}

    def cancel_subagent_task(self, task_id: str, session_name: str = "") -> Dict[str, Any]:
        """协作式停止指定子代理任务（仅允许停止当前会话或匹配归属的任务）。"""
        try:
            from core.tools import _SUBAGENT_TERMINAL_STATUSES, subagent_manager

            tid = str(task_id or "").strip()
            if not tid:
                return {"ok": False, "msg": "缺少子代理任务 ID"}
            snap = subagent_manager.task_snapshot(tid)
            if snap is None:
                return {"ok": False, "msg": "未找到该子代理任务"}
            session = str(session_name or "")
            if session and snap.parent_session and snap.parent_session != session:
                return {"ok": False, "msg": "该子代理不属于当前会话"}
            if snap.status in _SUBAGENT_TERMINAL_STATUSES:
                return {
                    "ok": True,
                    "already_terminal": True,
                    "status": snap.status,
                    "msg": "子代理已处于终态",
                }
            cancelled = subagent_manager.cancel(tid, reason="user requested via GUI")
            if cancelled:
                return {"ok": True, "status": "cancelled", "msg": "已停止该子代理"}
            return {"ok": False, "msg": "子代理已结束或无法停止"}
        except Exception as exc:
            logger.debug("cancel_subagent_task 失败", exc_info=True)
            return {"ok": False, "msg": str(exc)}

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
                return {"ok": True, "msg": "任务在终止前已自行结束"}
            if result == KILL_RESULT_NOT_FOUND:
                return {"ok": False, "msg": "未找到该后台任务"}
            if result == KILL_RESULT_IDENTITY_UNVERIFIABLE:
                return {"ok": False, "msg": "无法校验进程身份，已拒绝终止"}
            return {"ok": False, "msg": "终止失败，进程可能仍在运行"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def _emit_pty_exit(self, code) -> None:
        if self._shutting_down:
            return
        try:
            self.emit_event("pty_exit", {"code": int(code)})
        except Exception:
            logger.debug("推送终端退出事件失败", exc_info=True)

    def pty_terminal_ensure(self, cols=80, rows=24) -> Dict[str, Any]:
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

    def get_model_settings(self) -> Dict[str, Any]:
        """返回不含 API Key 明文的完整模型设置快照。"""
        config_data = load_config()
        # active_model_id 要给当前会话生效的模型，不能用全局默认：全局值可能属于另一家厂商的
        # 同名模型，直接返回会让界面选中态跳到错的厂商（选了 A 厂的模型自己跳到 B 厂）。
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
                    "supports_function_calling": bool(
                        model.get("supports_function_calling", False)
                    ),
                    "computer_protocol": str(model.get("computer_protocol") or ""),
                    "max_tokens": (
                        int(model["max_tokens"]) if model.get("max_tokens") else None
                    ),
                }
                # 思考档位（能选哪些 + 上次存的选择），给模型编辑弹窗回填下拉框用。
                # 单个模型的数据出问题不能把整个 get_model_settings 拖跨（调得很频繁，
                # 一报错整个设置页就打不开），所以统一兜底成安全值。
                try:
                    reasoning = _reasoning_snapshot_for_model(model)
                    item["reasoning_choice"] = str(reasoning.get("choice") or "default")
                    item["reasoning_options"] = list(reasoning.get("options") or [])
                    item["reasoning_visible"] = bool(reasoning.get("visible", False))
                    item["reasoning_kind"] = str(reasoning.get("kind") or "hidden")
                    item["reasoning_disabled_reason"] = str(
                        reasoning.get("disabled_reason") or ""
                    )
                    item["reasoning_default_choice"] = str(
                        reasoning.get("default_choice") or ""
                    )
                except Exception:
                    logger.debug(
                        "计算模型思考能力快照失败，回退安全值: %s",
                        model.get("id"),
                        exc_info=True,
                    )
                    item["reasoning_choice"] = "default"
                    item["reasoning_options"] = []
                    item["reasoning_visible"] = False
                    item["reasoning_kind"] = "hidden"
                    item["reasoning_disabled_reason"] = ""
                    item["reasoning_default_choice"] = ""
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

    _TR_NEED_LOGIN_MSG = "基元律动登录已失效，请重新登录后再使用该模型"

    def _tr_effective_base_url(self, model_id: str = "") -> str:
        target = str(model_id or "").strip()
        if target:
            try:
                for provider in load_config().get("providers", []) or []:
                    if not isinstance(provider, dict):
                        continue
                    for model in provider.get("models") or []:
                        if isinstance(model, dict) and str(model.get("id") or "") == target:
                            return str(provider.get("base_url") or "")
            except Exception:
                logger.debug("解析激活模型厂商失败", exc_info=True)
        try:
            active = get_active_model(getattr(self, "_current_session", None)) or {}
        except Exception:
            logger.debug("解析会话生效模型失败，按非基元律动处理", exc_info=True)
            return ""
        return str(active.get("base_url") or "")

    def _tr_session_gate(self, model_id: str = "") -> Optional[Dict[str, Any]]:
        """基元律动会话守门：专属 Key 的登录会话失效时返回拦截结果，否则 None。

        只有「本条消息用的模型来自 tokenrhythm 厂商」且「本地存在账号绑定的专属
        Key」时才校验会话，其它模型的调用路径完全不受影响（不多发任何请求）。
        守门自身异常一律放行并记日志：宁可漏拦一次，也不能把对话入口打挂。
        """
        try:
            base_url = self._tr_effective_base_url(model_id).lower()
            if "tokenrhythm.studio" not in base_url:
                return None
            if not tokenrhythm_auth.binding_active():
                return None
            health = tokenrhythm_auth.session_health() or {}
            if bool(health.get("ok")):
                return None
            logger.warning(
                "基元律动会话不可用，已拦截发消息：%s", str(health.get("reason") or "")
            )
            return {"ok": False, "msg": self._TR_NEED_LOGIN_MSG, "need_login": True}
        except Exception:
            logger.warning("基元律动会话守门校验失败，本次放行", exc_info=True)
            return None

    def _tr_settings_snapshot(self) -> Dict[str, Any]:
        """模型设置快照；失败只降级为空 dict，不用它拖垮登录结果展示。"""
        try:
            return self.get_model_settings()
        except Exception:
            logger.warning("基元律动登录后读取模型设置失败", exc_info=True)
            return {}

    def _tr_has_configured_provider(self) -> bool:
        try:
            config_data = load_config()
        except Exception:
            logger.debug("读取本地模型配置失败，视为未配置基元律动", exc_info=True)
            return False
        providers = config_data.get("providers", []) if isinstance(config_data, dict) else []
        if not isinstance(providers, list):
            return False
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            base_url = str(provider.get("base_url") or "").lower()
            if "tokenrhythm.studio" in base_url and str(provider.get("api_key") or "").strip():
                return True
        return False

    def tr_session_health(self, force: bool = False) -> Dict[str, Any]:
        try:
            health = tokenrhythm_auth.session_health(force=bool(force)) or {}
        except Exception as e:
            logger.exception("tr_session_health 失败")
            return {"ok": False, "logged_in": False, "reason": str(e), "msg": str(e)}
        return {
            "ok": bool(health.get("ok")),
            "logged_in": bool(health.get("logged_in")),
            "reason": str(health.get("reason") or ""),
        }

    def tr_auth_status(self) -> Dict[str, Any]:
        """查询基元律动登录状态 + 本地是否已配置可用 Key。"""
        try:
            status = tokenrhythm_auth.account_status() or {}
            logged_in = bool(status.get("logged_in"))
            user = status.get("user") if logged_in else None
            return {
                "ok": True,
                "logged_in": logged_in,
                "user": user if isinstance(user, dict) else None,
                "configured": self._tr_has_configured_provider(),
            }
        except Exception as e:
            logger.exception("tr_auth_status 失败")
            return {"ok": False, "msg": str(e)}

    def tr_account_overview(self) -> Dict[str, Any]:
        try:
            overview = tokenrhythm_auth.account_overview() or {}
            if not overview.get("ok"):
                return {
                    "ok": False,
                    "msg": str(overview.get("reason") or "尚未登录基元律动账号"),
                }
            credits = overview.get("temporary_credits")
            if not isinstance(credits, dict):
                # 老版本 core 或字段缺失：给一份和 core 降级值一样的空值，前端不用判空
                credits = {"total_cny": "0", "count": 0, "nearest_expires_at": "", "nearest_amount_cny": "0"}
            raw_model_credits = overview.get("model_credits")
            if not isinstance(raw_model_credits, list):
                model_credits: list[dict[str, Any]] = []
            else:
                model_credits = [
                    item for item in raw_model_credits if isinstance(item, dict)
                ]
            result: Dict[str, Any] = {
                "ok": True,
                "user": overview.get("user") or {},
                "wallet": overview.get("wallet") or {},
                "usage_today": overview.get("usage_today") or {},
                "temporary_credits": credits,
                "model_credits": model_credits,
            }
            if overview.get("partial"):
                result["partial"] = True
                result["errors"] = dict(overview.get("errors") or {})
            return result
        except Exception as e:
            logger.exception("tr_account_overview 失败")
            return {"ok": False, "msg": str(e)}

    def tr_auth_send_sms(
        self, phone: str, captcha_verify_param: str = ""
    ) -> Dict[str, Any]:
        """发送短信验证码（challengeId 由 core 层进程内保存）。

        captcha_verify_param 由前端在阿里云验证码 success 回调里取得，原样透传给
        core 层；空串归一成 None，保证未集成验证码时请求体里不出现
        captchaVerifyParam 字段。该值是一次性校验凭据，绝不写日志。
        """
        try:
            phone = str(phone or "").strip()
            if not phone:
                return {"ok": False, "msg": "请输入手机号"}
            captcha = str(captcha_verify_param or "").strip()
            result = tokenrhythm_auth.send_sms_code(phone, captcha or None) or {}
            return {
                "ok": True,
                "challenge_id": str(result.get("challenge_id") or ""),
                "interval_seconds": result.get("interval_seconds"),
                "ttl_seconds": result.get("ttl_seconds"),
                "masked_target": str(result.get("masked_target") or ""),
                "msg": "验证码已发送",
            }
        except Exception as e:
            logger.exception("tr_auth_send_sms 失败")
            # 异常里的提示本来就是给用户看的中文，不能吞成空字符串
            return {"ok": False, "msg": str(e) or "验证码发送失败，请稍后重试"}

    def _tr_finish_login(self, login_result: Dict[str, Any]) -> Dict[str, Any]:
        user = (login_result or {}).get("user") or {}
        try:
            bind = dict(tokenrhythm_auth.ensure_bound() or {})
        except Exception as error:
            logger.warning("基元律动登录后自动绑定 API Key 失败: %s", error)
            bind = {
                "ok": False,
                "created": False,
                "provider_id": "",
                "error": str(error) or "自动绑定 API Key 失败",
            }
        else:
            bind["ok"] = True

        models: Dict[str, Any] = {"ok": False, "error": "未绑定 API Key，跳过模型同步"}
        if bind.get("ok"):
            try:
                sync = getattr(self, "_sync_active_model_context", None)
                if callable(sync):
                    sync()
            except Exception:
                logger.warning("基元律动登录后同步激活模型上下文失败", exc_info=True)
            try:
                synced = dict(
                    tokenrhythm_auth.sync_models(bind.get("provider_id") or None) or {}
                )
                synced["ok"] = True
                models = synced
            except Exception as error:
                logger.warning("基元律动登录后同步模型失败: %s", error)
                models = {"ok": False, "error": str(error) or "模型同步失败"}
            bind_error = ""
            base_msg = (
                "登录成功，已自动配置基元律动 API Key"
                if bind.get("created")
                else "登录成功，本地已有可用 API Key"
            )
            if models.get("ok"):
                msg = f"{base_msg}（同步 {int(models.get('added') or 0)} 个模型）"
            else:
                msg = f"{base_msg}（模型同步失败：{models.get('error') or '未知原因'}）"
        else:
            bind_error = str(bind.get("error") or "自动绑定 API Key 失败")
            msg = f"登录成功，但自动绑定 API Key 失败：{bind_error}"

        return {
            "ok": True,
            "user": user if isinstance(user, dict) else {},
            "bind": bind,
            "bind_ok": bool(bind.get("ok")),
            "bind_error": bind_error,
            "models": models,
            "settings": self._tr_settings_snapshot(),
            "msg": msg,
        }

    def tr_auth_sync_models(self) -> Dict[str, Any]:
        """把基元律动官方模型全量同步到本地厂商配置（幂等，已存在的跳过）。"""
        try:
            result = dict(tokenrhythm_auth.sync_models() or {})
            result.setdefault("ok", True)
            try:
                sync = getattr(self, "_sync_active_model_context", None)
                if callable(sync):
                    sync()
            except Exception:
                logger.warning("同步模型后刷新激活模型上下文失败", exc_info=True)
            result["settings"] = self._tr_settings_snapshot()
            return result
        except Exception as e:
            logger.exception("tr_auth_sync_models 失败")
            return {"ok": False, "msg": str(e) or "同步模型失败，请稍后重试"}

    def tr_auth_sms_login(
        self, phone: str, code: str, invite_code: str = ""
    ) -> Dict[str, Any]:
        try:
            phone = str(phone or "").strip()
            code = str(code or "").strip()
            invite = str(invite_code or "").strip()
            if not phone:
                return {"ok": False, "msg": "请输入手机号"}
            if not code:
                return {"ok": False, "msg": "请输入验证码"}
            login_result = tokenrhythm_auth.sms_login(phone, code, invite or None)
            return self._tr_finish_login(login_result)
        except Exception as e:
            logger.exception("tr_auth_sms_login 失败")
            return {"ok": False, "msg": str(e) or "登录失败，请稍后重试"}

    def tr_auth_password_login(self, account: str, password: str) -> Dict[str, Any]:
        """账号密码登录，成功后自动绑定 Key。"""
        try:
            account = str(account or "").strip()
            password = "" if password is None else str(password)
            if not account:
                return {"ok": False, "msg": "请输入手机号或邮箱"}
            if not password:
                return {"ok": False, "msg": "请输入密码"}
            login_result = tokenrhythm_auth.password_login(account, password)
            return self._tr_finish_login(login_result)
        except Exception as e:
            logger.exception("tr_auth_password_login 失败")
            return {"ok": False, "msg": str(e) or "登录失败，请稍后重试"}

    def tr_auth_logout(self) -> Dict[str, Any]:
        try:
            result = dict(tokenrhythm_auth.logout() or {})
            deleted = bool(result.get("key_deleted"))
            providers_removed = int(result.get("providers_removed") or 0)
            if result.get("had_key") and not deleted:
                msg = "已退出基元律动账号，但专属 API Key 删除失败，请到基元律动控制台手动删除"
            elif deleted and providers_removed:
                msg = "已退出基元律动账号，专属 API Key 与模型配置已移除"
            elif deleted:
                msg = "已退出基元律动账号，该账号的专属 API Key 已删除"
            elif providers_removed:
                msg = "已退出基元律动账号，模型配置已移除"
            else:
                msg = "已退出基元律动账号"
            return {
                "ok": True,
                "msg": msg,
                "key_deleted": deleted,
                "had_key": bool(result.get("had_key")),
                "providers_removed": providers_removed,
            }
        except Exception as e:
            logger.exception("tr_auth_logout 失败")
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------
    # 账号中心只读查询：充值记录 / 用量明细 / 个人信息 / 调用日志，用已登录会话直连官方后台
    # ------------------------------------------------------------------
    def tr_profile(self) -> Dict[str, Any]:
        """个人信息：前端「个人信息」弹窗拉取。"""
        try:
            return dict(tokenrhythm_auth.user_profile() or {"ok": False, "msg": "未登录"})
        except Exception as e:
            logger.exception("tr_profile 失败")
            return {"ok": False, "msg": str(e) or "获取个人信息失败"}

    def tr_usage_detail(
        self, range: str = "7d", page: int = 1, page_size: int = 20
    ) -> Dict[str, Any]:
        """用量明细：前端「用量」弹窗拉取（默认近 7 天）。"""
        try:
            result = dict(
                tokenrhythm_auth.usage_detail(
                    str(range or "7d"), int(page or 1), int(page_size or 20)
                )
                or {"ok": False, "msg": "未登录"}
            )
            return result
        except Exception as e:
            logger.exception("tr_usage_detail 失败")
            return {"ok": False, "msg": str(e) or "获取用量明细失败"}

    def tr_wallet_transactions(self) -> Dict[str, Any]:
        try:
            return dict(
                tokenrhythm_auth.wallet_transactions()
                or {"ok": False, "msg": "未登录"}
            )
        except Exception as e:
            logger.exception("tr_wallet_transactions 失败")
            return {"ok": False, "msg": str(e) or "获取钱包流水失败"}

    def tr_call_logs(
        self, page: int = 1, page_size: int = 20, status: str = ""
    ) -> Dict[str, Any]:
        """调用日志：前端「用量」弹窗的「调用记录」分页拉取。"""
        try:
            return dict(
                tokenrhythm_auth.call_logs(
                    int(page or 1), int(page_size or 20), str(status or "")
                )
                or {"ok": False, "msg": "未登录"}
            )
        except Exception as e:
            logger.exception("tr_call_logs 失败")
            return {"ok": False, "msg": str(e) or "获取调用日志失败"}

    def _resolve_provider_id(self, provider_ref: str) -> Optional[str]:
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

    def _apply_reasoning_choice(self, model_id: str, choice: str | None) -> str:
        if choice is None:
            return ""
        ok, message = set_model_reasoning_choice(str(model_id), choice)
        if ok:
            return ""
        logger.warning("模型 %s 的思考设置保存失败: %s", model_id, message)
        return f"（思考设置未生效：{message}）"

    def add_model(
        self,
        provider_id: str,
        model_name: str,
        context_window: int = 128000,
        supports_vision: bool = False,
        supports_function_calling: bool = True,
        computer_protocol: str = "",
        max_tokens: int | None = None,
        reasoning_choice: str | None = None,
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
                normalized_max_tokens = max(int(max_tokens), 0) or None if max_tokens is not None else None
            except (TypeError, ValueError):
                normalized_max_tokens = None
            # 地址前缀和厂商内部模型 ID 怎么拼，统一交给 model_manager 处理。
            # computer_protocol 不填或空串 = 自动（不写进配置，能看图又能调工具就算启用），
            # 不能硬改成 "none"（旧代码这么做，导致新的视觉模型永远开不起来）。
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
            warning = self._apply_reasoning_choice(model_id, reasoning_choice)
            return {
                "ok": True,
                "model_id": model_id,
                "models": self._all_models(),
                "settings": self.get_model_settings(),
                "msg": f"已添加模型 {model_name}{warning}",
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
        reasoning_choice: str | None = None,
    ) -> Dict[str, Any]:
        try:
            mm_update_model(
                str(provider_id),
                str(model_id),
                name=str(model_name or "").strip(),
                context_window=int(context_window),
                supports_vision=bool(supports_vision),
                supports_function_calling=supports_function_calling,
                computer_protocol=computer_protocol,
                max_tokens=max_tokens,
            )
            self._sync_active_model_context()
            warning = self._apply_reasoning_choice(model_id, reasoning_choice)
            return {
                "ok": True,
                "settings": self.get_model_settings(),
                "msg": f"模型已更新{warning}",
            }
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

            # 多个 Key 的厂商：一个个试（先用池子里当前该用的那个），哪个通过
            # 校验就用它的结果；绝不能把好几行的 Key 整串塞进 Authorization / x-api-key。
            from core.model_discovery import fetch_model_list

            return fetch_model_list(base_url, _probe_keys_for_provider(provider))
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

            # 多个 Key 的厂商：一个个试（先用池子里当前该用的那个），有一个成功
            # 就算通过；报错信息里各自的 Key 要打码。单个 Key 还是原来的超时时长；多个 Key 时
            # 整轮探测给 2 倍总时长，免得一个个等叠加起来太久。
            probe_keys = _probe_keys_for_provider(provider)
            budget = TEST_MODEL_TIMEOUT if len(probe_keys) <= 1 else TEST_MODEL_TIMEOUT * 2
            deadline = time.monotonic() + budget
            last_msg = ""
            for api_key in probe_keys:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                outcome: Dict[str, Any] = {}

                def _ping(_key: str = api_key, _out: Dict[str, Any] = outcome) -> None:
                    try:
                        from core.llm import _request_stream

                        stream, _holder = _request_stream(
                            provider.get("protocol", "openai"),
                            {
                                "model": target.get("litellm_model"),
                                "api_key": _key,
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
                        _out["ok"] = True
                        _out["msg"] = "连接成功"
                    except Exception as e:
                        _out["ok"] = False
                        _out["msg"] = _sanitize_for_ui(str(e), _key)[:300]

                thread = threading.Thread(target=_ping, daemon=True, name="gui-test-model")
                thread.start()
                thread.join(timeout=remaining)
                if thread.is_alive():
                    last_msg = (
                        f"连接超时（超过 {TEST_MODEL_TIMEOUT} 秒）"
                        "，推理模型首次响应可能较慢，可稍后重试"
                    )
                    continue
                if outcome.get("ok"):
                    return {"ok": True, "msg": str(outcome.get("msg") or "连接成功")}
                last_msg = str(outcome.get("msg") or "未知结果")
            if len(probe_keys) > 1:
                return {"ok": False, "msg": f"{len(probe_keys)} 个 Key 均未连通，最后错误：{last_msg}"}
            return {"ok": False, "msg": last_msg or "连接失败"}
        except Exception as e:
            logger.exception("test_model 失败")
            return {"ok": False, "msg": str(e)}
