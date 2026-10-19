"""管理交互类 Agent 工具：PTY 终端 / 内嵌浏览器面板 / 截屏 / 剪贴板图片 / 电脑操控。

设计约束：
- 只复用 core 层公开模块（gui_terminal / app_context / ai_output_paths /
  workspace_context / computer.*），禁止 import gui_bridge / main / pywebview；
  需要 GUI 能力时统一通过 ``core.app_context.get()`` 动态取当前运行上下文，
  用 getattr 探测其公开方法，缺失即返回明确中文错误，绝不伪造成功。
- 所有工具统一返回 JSON 字符串（ensure_ascii=False）：
  成功 ``{"ok": true, ...}``，失败 ``{"ok": false, "error": "中文原因"}``；
- 所有异常在工具入口兜底，任何情况下都不向调度层抛出；
- 不新增权限档位 / 信任类动作：本模块只做用户可感知的交互与状态查询。

read_only 口径：本模块全部工具都会产生进程外副作用（创建 PTY、驱动 UI、
落盘截图/剪贴板图片、启停电脑操控 run），即使是 state/status 这类只读动作，
也按工具整体语义置 read_only=False，避免把混合语义工具误标成只读；
mutates_files 口径：screenshot_capture / clipboard_image 落的是 AI 产物文件（截图 /
剪贴板图片），不属于受控源码编辑，因此统一 mutates_files=False、不接入按回合撤销
记录器；产物路径仍由返回值（path 字段）如实给出。

工具清单（build_specs() 返回 5 个 ToolSpec）：
pty_manage / browser_pane / screenshot_capture / clipboard_image / computer_control
"""

from __future__ import annotations

import datetime
import functools
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from core.tool_protocol import ToolSpec


# ---------------------------------------------------------------------------
# 通用返回与容错
# ---------------------------------------------------------------------------


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ok(**payload: Any) -> str:
    data: dict[str, Any] = {"ok": True}
    data.update(payload)
    return _dumps(data)


def _fail(error: Any, **extra: Any) -> str:
    data: dict[str, Any] = {"ok": False, "error": str(error)}
    data.update(extra)
    return _dumps(data)


def _guard(func):
    """工具入口兜底：任何异常都转成统一失败 JSON，不让异常冒到调度层。"""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 工具边界必须吞掉全部异常
            return _fail(f"{type(exc).__name__}: {exc}")

    return wrapper


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return number


def _timestamp_slug(prefix: str, suffix: str = ".png") -> str:
    now = datetime.datetime.now()
    return f"{prefix}-{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond // 1000:03d}{suffix}"


# ---------------------------------------------------------------------------
# 1) pty_manage：常驻 shell（复用 core.gui_terminal）
# ---------------------------------------------------------------------------

# 工具侧输出缓冲上限：只保留尾部，避免常驻 shell 的输出无限吃内存
_PTY_BUFFER_CHARS = 32 * 1024
# 返回给模型时只给尾部，避免单条工具结果过大
_PTY_TAIL_CHARS = 4000

PTY_ACTIONS = ("ensure", "write", "resize", "kill", "state")


class _PtyHolder:
    """模块级 PTY 单例容器：线程锁 + 输出环形缓冲 + 会话复用。

    为什么自己缓冲输出：``PtyTerminalSession`` 只提供 write/resize/kill/is_alive，
    输出仅通过构造时的 on_output 回调派发，没有同步读取接口。工具需要「最近
    输出」用于状态汇报，因此在回调里保存尾部文本。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session = None
        self._chunks: list[str] = []
        self._total = 0
        self._cols = 80
        self._rows = 24
        self._cwd = ""
        self._started_at = 0.0
        self._last_exit: int | None = None

    # -- 回调（由会话后台线程触发，持锁时间极短） --
    def _append_output(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)
            self._total += len(text)
            while self._total > _PTY_BUFFER_CHARS and len(self._chunks) > 1:
                dropped = self._chunks.pop(0)
                self._total -= len(dropped)

    def _mark_exit(self, code: Any) -> None:
        with self._lock:
            self._last_exit = _as_int(code, 0)
            self._session = None

    # -- 对外操作 --
    def tail(self) -> str:
        with self._lock:
            text = "".join(self._chunks)
        if len(text) <= _PTY_TAIL_CHARS:
            return text
        return "…" + text[-_PTY_TAIL_CHARS:]

    def _alive_session(self):
        session = self._session
        if session is None:
            return None
        try:
            if session.is_alive():
                return session
        except Exception:
            return None
        self._session = None
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            session = self._alive_session()
            return {
                "alive": session is not None,
                "pid": getattr(session, "pid", None) if session is not None else None,
                "cols": self._cols,
                "rows": self._rows,
                "cwd": self._cwd,
                "started_at": self._started_at,
                "last_exit_code": self._last_exit,
                "buffered_chars": self._total,
                "recent_output": self.tail(),
            }

    def ensure(self, cols: int, rows: int) -> dict[str, Any]:
        from core.gui_terminal import clamp_terminal_size, create_pty_terminal
        from core.workspace_context import get_workdir

        with self._lock:
            session = self._alive_session()
            if session is not None:
                payload = self.status()
                payload["reused"] = True
                return payload
            cols, rows = clamp_terminal_size(cols, rows)
            cwd = str(get_workdir())
            # 重建前清掉旧缓冲，避免上一条会话的输出现到新会话状态里
            self._chunks.clear()
            self._total = 0
            self._last_exit = None
            session = create_pty_terminal(
                cwd,
                cols=cols,
                rows=rows,
                on_output=self._append_output,
                on_exit=self._mark_exit,
            )
            self._session = session
            self._cols, self._rows = cols, rows
            self._cwd = cwd
            self._started_at = time.time()
            payload = self.status()
            payload["reused"] = False
            return payload

    def write(self, data: str) -> dict[str, Any]:
        with self._lock:
            session = self._alive_session()
            if session is None:
                return {}
            written = bool(session.write(data))
        return {"written": written, "chars": len(data)}

    def resize(self, cols: int, rows: int) -> dict[str, Any]:
        from core.gui_terminal import clamp_terminal_size

        cols, rows = clamp_terminal_size(cols, rows)
        with self._lock:
            session = self._alive_session()
            if session is None:
                return {}
            resized = bool(session.resize(cols, rows))
            if resized:
                self._cols, self._rows = cols, rows
        return {"resized": resized}

    def kill(self) -> dict[str, Any]:
        with self._lock:
            session = self._session
            self._session = None
        if session is None:
            return {"killed": False, "note": "当前没有运行中的终端会话"}
        # 注意：kill() 内部会等待退出事件（≤3s），必须在锁外调用，
        # 否则 on_exit 回调（monitor 线程）取锁会被长时间阻塞。
        session.kill()
        with self._lock:
            self._last_exit = self._last_exit if self._last_exit is not None else -1
        return {"killed": True}


_PTY_HOLDER = _PtyHolder()


def _get_pty_holder() -> _PtyHolder:
    return _PTY_HOLDER


@_guard
def pty_manage(action: str = "state", data: str = "", cols: int = 80, rows: int = 24) -> str:
    """管理常驻 shell 伪终端：创建 / 写入 / 调整尺寸 / 终止 / 查看状态。"""
    act = str(action or "").strip().lower()
    if act not in PTY_ACTIONS:
        return _fail(f"不支持的 action：{action}（可选 {'/'.join(PTY_ACTIONS)}）")

    holder = _get_pty_holder()
    if act == "ensure":
        try:
            payload = holder.ensure(cols, rows)
        except Exception as exc:  # PtyTerminalError 携带中文消息，其余异常原样展示
            return _fail(str(exc) or f"{type(exc).__name__}")
        return _ok(action=act, **payload)

    if act == "write":
        text = str(data or "")
        if not text:
            return _fail("write 需要非空 data（要写入终端的文本，可含换行）")
        result = holder.write(text)
        if not result:
            return _fail("终端会话尚未创建或已退出，请先执行 action=ensure")
        if not result.get("written"):
            return _fail("写入终端失败：会话可能已退出，请重新 action=ensure")
        return _ok(action=act, **result, **_strip_output(holder.status()))

    if act == "resize":
        result = holder.resize(cols, rows)
        if not result:
            return _fail("终端会话尚未创建或已退出，请先执行 action=ensure")
        if not result.get("resized"):
            return _fail("调整终端尺寸失败：会话可能已退出")
        return _ok(action=act, **result, **_strip_output(holder.status()))

    if act == "kill":
        result = holder.kill()
        return _ok(action=act, **result)

    # state
    return _ok(action=act, **_strip_output(holder.status()))


def _strip_output(status: dict[str, Any]) -> dict[str, Any]:
    """终态汇报里保留尾部输出，同时给出长度便于判断是否有内容。"""
    data = dict(status)
    output = str(data.pop("recent_output", "") or "")
    data["recent_output"] = output
    data["recent_output_chars"] = len(output)
    return data


# ---------------------------------------------------------------------------
# 2) browser_pane：GUI 内嵌浏览器面板（经 app_context 取 GUI 上下文）
# ---------------------------------------------------------------------------

BROWSER_ACTIONS = (
    "open",
    "close",
    "navigate",
    "back",
    "forward",
    "reload",
    "state",
    "set_bounds",
)

# action -> (桥方法名, 是否需要 rect)
_BROWSER_METHODS: dict[str, tuple[str, bool]] = {
    "open": ("browser_pane_open", True),
    "close": ("browser_pane_hide", False),
    "navigate": ("browser_pane_navigate", False),
    "back": ("browser_pane_back", False),
    "forward": ("browser_pane_forward", False),
    "reload": ("browser_pane_reload", False),
    "state": ("browser_pane_get_state", False),
    "set_bounds": ("browser_pane_set_bounds", True),
}

_BROWSER_ERROR_TEXT = {
    "unsupported_platform": "当前平台不支持内嵌浏览器面板（仅 Windows 可用）",
    "not_open": "浏览器面板尚未打开，请先执行 action=open",
    "invalid_rect": "面板位置参数 rect 非法（必须包含 x/y/width/height 四个键）",
    "no_gui": "当前不在 GUI 环境，浏览器面板不可用",
}


def _browser_context() -> Any:
    from core import app_context

    return app_context.get()


def _call_browser_bridge(method_name: str, rect: dict | None, url: str, zoom: float) -> Any:
    """按桥方法的真实签名调用；缺少方法时抛 RuntimeError（中文消息）。"""
    ctx = _browser_context()
    if ctx is None:
        raise RuntimeError(_BROWSER_ERROR_TEXT["no_gui"])
    method = getattr(ctx, method_name, None)
    if not callable(method):
        raise RuntimeError(f"当前上下文不支持该浏览器面板操作：缺少 {method_name}")
    if method_name == "browser_pane_open":
        return method(rect, url, zoom)
    if method_name == "browser_pane_set_bounds":
        return method(rect, zoom)
    if method_name == "browser_pane_navigate":
        return method(url)
    return method()


@_guard
def browser_pane(action: str, url: str = "", rect: Any = None, zoom: float = 1.0) -> str:
    """驱动 GUI 内嵌浏览器面板（打开/关闭/导航/前进后退/刷新/状态/改位置）。"""
    act = str(action or "").strip().lower()
    if act not in _BROWSER_METHODS:
        return _fail(f"不支持的 action：{action}（可选 {'/'.join(BROWSER_ACTIONS)}）")

    method_name, need_rect = _BROWSER_METHODS[act]
    rect_value: dict | None = None
    if need_rect:
        if not isinstance(rect, dict) or not rect:
            return _fail(
                "该操作需要 rect 参数（对象，包含 x/y/width/height 四个数值键）",
                hint="rect 会原样交给桥层校验，不做本地改写",
            )
        rect_value = dict(rect)

    if act == "navigate":
        target = str(url or "").strip()
        if not target:
            return _fail("navigate 需要非空 url")
    if act == "open":
        target = str(url or "").strip()
    else:
        target = str(url or "")

    try:
        result = _call_browser_bridge(
            method_name, rect_value, target, _as_float(zoom, 1.0)
        )
    except RuntimeError as exc:
        return _fail(str(exc))
    except TypeError as exc:
        # 桥方法签名与预期不一致（上下文不是 GUI 桥）：明确报错，不猜参数
        return _fail(f"调用 {method_name} 失败（方法签名不匹配）：{exc}")

    if not isinstance(result, dict):
        return _ok(action=act, result=result)

    code = str(result.get("error") or "")
    if result.get("ok") is False:
        return _fail(
            _BROWSER_ERROR_TEXT.get(code, code or "浏览器面板操作失败"),
            action=act,
            bridge_error=code,
            bridge_detail={k: v for k, v in result.items() if k != "ok"},
        )
    payload = {k: v for k, v in result.items() if k != "ok"}
    return _ok(action=act, **payload)


# ---------------------------------------------------------------------------
# 3) screenshot_capture：全屏 / 区域截屏（PIL.ImageGrab）
# ---------------------------------------------------------------------------

SCREENSHOT_MODES = ("full", "region")

_PILLOW_LOCK = threading.Lock()
_PILLOW_CACHE: dict[str, Any] = {"checked": False, "available": False, "version": "", "source": ""}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _venv_python_candidates() -> list[Path]:
    root = _repo_root()
    return [
        root / "venv" / "Scripts" / "python.exe",
        root / "venv" / "bin" / "python",
    ]


def _probe_pillow() -> tuple[bool, str, str]:
    """检测 Pillow 是否可用，返回 (可用, 版本, 探测来源)。

    先按约定执行 ``.\\venv\\Scripts\\python.exe -c "import PIL; print(PIL.__version__)"``
    （打包/多解释器环境下能确认落盘进程之外的解释器也确实装了 Pillow）；
    venv 解释器不存在或执行失败时，退回进程内 import 探测。
    """
    with _PILLOW_LOCK:
        if _PILLOW_CACHE["checked"] and _PILLOW_CACHE["available"]:
            return True, str(_PILLOW_CACHE["version"]), str(_PILLOW_CACHE["source"])
        for exe in _venv_python_candidates():
            if not exe.exists():
                continue
            try:
                proc = subprocess.run(
                    [str(exe), "-c", "import PIL; print(PIL.__version__)"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except Exception:
                continue
            version = (proc.stdout or "").strip()
            if proc.returncode == 0 and version:
                _PILLOW_CACHE.update(
                    checked=True, available=True, version=version, source=str(exe)
                )
                return True, version, str(exe)
        try:
            import PIL  # noqa: F401 - 只做可用性探测

            version = str(getattr(PIL, "__version__", "") or "unknown")
            _PILLOW_CACHE.update(
                checked=True, available=True, version=version, source="in-process import"
            )
            return True, version, "in-process import"
        except Exception:
            _PILLOW_CACHE.update(checked=True, available=False, version="", source="")
            return False, "", ""


def _resolve_output_target(output_path: str, prefix: str) -> Path:
    """解析落盘目标：指定路径按用户路径（相对路径按线程 workdir），否则落 AI 输出目录。"""
    from core.ai_output_paths import resolve_ai_output_dir
    from core.workspace_context import get_workdir

    text = str(output_path or "").strip()
    if text:
        target = Path(os.path.expandvars(os.path.expanduser(text)))
        if not target.is_absolute():
            target = Path(get_workdir()) / target
        target = target.resolve()
        if target.suffix == "":
            target = target.with_name(target.name + ".png")
    else:
        target = Path(resolve_ai_output_dir()) / _timestamp_slug(prefix)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _save_image(image: Any, target: Path) -> int:
    """保存图片并返回字节数；异常由调用方翻译成中文错误。"""
    if target.suffix.lower() == ".png":
        image.save(str(target), format="PNG")
    else:
        image.save(str(target))
    try:
        return int(target.stat().st_size)
    except OSError:
        return 0


@_guard
def screenshot_capture(
    mode: str = "full",
    x: int = 0,
    y: int = 0,
    width: int = 0,
    height: int = 0,
    output_path: str = "",
) -> str:
    """截取桌面全屏或指定矩形区域；返回真实落盘路径。"""
    use_mode = str(mode or "full").strip().lower()
    if use_mode not in SCREENSHOT_MODES:
        return _fail(f"不支持的 mode：{mode}（可选 full / region）")

    box: tuple[int, int, int, int] | None = None
    if use_mode == "region":
        left, top = _as_int(x, 0), _as_int(y, 0)
        w, h = _as_int(width, 0), _as_int(height, 0)
        if w <= 0 or h <= 0:
            return _fail("region 模式需要 width 与 height 均为正整数")
        box = (left, top, left + w, top + h)

    available, version, source = _probe_pillow()
    if not available:
        return _fail(
            "截屏需要 Pillow，但当前解释器无法导入 PIL"
            "（已尝试 venv 解释器与进程内导入）"
        )

    try:
        from PIL import ImageGrab
    except Exception as exc:
        return _fail(f"导入 PIL.ImageGrab 失败：{exc}")

    try:
        if box is not None:
            image = ImageGrab.grab(bbox=box, all_screens=True)
        else:
            image = ImageGrab.grab(all_screens=True)
    except Exception as exc:
        return _fail(f"截屏失败（可能没有可用的桌面会话）：{type(exc).__name__}: {exc}")
    if image is None:
        return _fail("截屏失败：系统未返回画面（可能没有可用的桌面会话）")

    try:
        target = _resolve_output_target(output_path, "screenshot")
        size_bytes = _save_image(image, target)
    except Exception as exc:
        return _fail(f"保存截图失败：{type(exc).__name__}: {exc}")

    return _ok(
        mode=use_mode,
        path=str(target),
        width=int(getattr(image, "width", 0) or 0),
        height=int(getattr(image, "height", 0) or 0),
        size_bytes=size_bytes,
        box=list(box) if box is not None else None,
        pillow_version=version,
        pillow_source=source,
    )


# ---------------------------------------------------------------------------
# 4) clipboard_image：读取 / 保存剪贴板图片
# ---------------------------------------------------------------------------

CLIPBOARD_ACTIONS = ("read", "save")


class _ClipboardNoImage(Exception):
    """剪贴板里没有图片（携带中文消息）。"""


def _grab_clipboard_image() -> Any:
    available, version, source = _probe_pillow()
    if not available:
        raise RuntimeError("读取剪贴板图片需要 Pillow，但当前解释器无法导入 PIL")
    try:
        from PIL import ImageGrab
    except Exception as exc:
        raise RuntimeError(f"导入 PIL.ImageGrab 失败：{exc}") from exc
    try:
        data = ImageGrab.grabclipboard()
    except Exception as exc:
        raise _ClipboardNoImage(
            f"读取剪贴板失败（可能没有可用的桌面会话）：{type(exc).__name__}: {exc}"
        ) from exc
    if data is None:
        raise _ClipboardNoImage("剪贴板中没有图片（为空或内容不是图像）")
    if isinstance(data, list):
        names = ", ".join(str(item) for item in data[:5])
        raise _ClipboardNoImage(f"剪贴板中是文件而不是图片：{names}")
    if not hasattr(data, "save"):
        raise _ClipboardNoImage(f"剪贴板内容无法识别为图片（类型 {type(data).__name__}）")
    return data


@_guard
def clipboard_image(action: str = "read", output_path: str = "") -> str:
    """读取剪贴板图片并保存到 AI 输出目录（或指定路径），返回真实文件路径。"""
    act = str(action or "read").strip().lower()
    if act not in CLIPBOARD_ACTIONS:
        return _fail(f"不支持的 action：{action}（可选 read / save）")

    try:
        image = _grab_clipboard_image()
    except _ClipboardNoImage as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(str(exc) or f"{type(exc).__name__}")

    try:
        target = _resolve_output_target(output_path, f"clipboard-{act}")
        size_bytes = _save_image(image, target)
    except Exception as exc:
        return _fail(f"保存剪贴板图片失败：{type(exc).__name__}: {exc}")

    return _ok(
        action=act,
        path=str(target),
        width=int(getattr(image, "width", 0) or 0),
        height=int(getattr(image, "height", 0) or 0),
        size_bytes=size_bytes,
    )


# ---------------------------------------------------------------------------
# 5) computer_control：查看 / 暂停 / 恢复 / 停止电脑操控，查看浏览器选项
# ---------------------------------------------------------------------------

COMPUTER_ACTIONS = ("status", "pause", "resume", "stop", "browser")


def _computer_settings_payload() -> dict[str, Any]:
    from core.computer.settings import load_computer_settings

    snapshot = load_computer_settings()
    return {
        "browser_control_enabled": bool(snapshot.browser_control_enabled),
        "computer_control_enabled": bool(snapshot.computer_control_enabled),
        "computer_browser": str(snapshot.computer_browser or ""),
        "profile_display_path": str(snapshot.profile_display_path or ""),
        "platform_supported": bool(snapshot.platform_supported),
        "browser_options": [
            option.to_dict() for option in getattr(snapshot, "browser_options", ())
        ],
        "generation": int(getattr(snapshot, "generation", 0) or 0),
    }


def _hotkey_available() -> bool:
    try:
        from core.computer.runtime import get_computer_runtime

        runtime = get_computer_runtime()
        return runtime is not None and getattr(runtime, "hotkey", None) is not None
    except Exception:
        return False


def _computer_run_state() -> tuple[str, dict[str, Any]]:
    """返回 (状态来源, run 公开快照)。

    优先走 GUI 上下文（它会确保事件桥已挂接，供前端实时校准）；无 GUI 上下文
    或上下文缺该方法时退回 core controller 的进程级单例快照。
    """
    from core import app_context

    ctx = app_context.get()
    if ctx is not None:
        getter = getattr(ctx, "get_computer_control_status", None)
        if callable(getter):
            try:
                data = getter()
            except Exception:
                data = None
            if isinstance(data, dict):
                run = {k: v for k, v in data.items() if k != "ok"}
                return "gui", run
    from core.computer.controller import get_computer_control_service

    snapshot = get_computer_control_service().snapshot()
    return "core", dict(snapshot.to_public_dict())


def _computer_mutate(act: str, run_id: str) -> dict[str, Any]:
    """执行 pause/resume/stop：优先 GUI 桥方法，退回 core controller 公开方法。"""
    from core import app_context

    ctx = app_context.get()
    gui_method = {
        "pause": "pause_computer_control",
        "resume": "resume_computer_control",
        "stop": "stop_computer_control",
    }[act]
    if ctx is not None:
        method = getattr(ctx, gui_method, None)
        if callable(method):
            return {"source": "gui", "result": method(run_id)}

    from core.computer.controller import get_computer_control_service
    from core.computer.models import ComputerControlError

    controller = get_computer_control_service()
    try:
        if act == "pause":
            snapshot = controller.pause(run_id, "user_pause")
        elif act == "resume":
            snapshot = controller.resume(run_id)
        else:
            snapshot = controller.stop(run_id, "user_stop")
    except ComputerControlError as exc:
        return {
            "source": "core",
            "error": str(exc),
            "error_code": getattr(exc, "code", ""),
        }
    return {"source": "core", "result": {"ok": True, **snapshot.to_public_dict()}}


@_guard
def computer_control(action: str) -> str:
    """查看/暂停/恢复/停止电脑操控 run，或查看浏览器操控选项。"""
    act = str(action or "").strip().lower()
    if act not in COMPUTER_ACTIONS:
        return _fail(f"不支持的 action：{action}（可选 {'/'.join(COMPUTER_ACTIONS)}）")

    if act == "browser":
        try:
            settings_payload = _computer_settings_payload()
        except Exception as exc:
            return _fail(f"读取浏览器操控设置失败：{type(exc).__name__}: {exc}")
        return _ok(action=act, **settings_payload)

    source, run = _computer_run_state()

    if act == "status":
        try:
            settings_payload = _computer_settings_payload()
        except Exception:
            settings_payload = {}
        return _ok(
            action=act,
            source=source,
            run=run,
            status=str(run.get("status") or ""),
            active=bool(str(run.get("run_id") or "")),
            hotkey_available=_hotkey_available(),
            settings=settings_payload,
        )

    run_id = str(run.get("run_id") or "").strip()
    if not run_id:
        return _fail(
            f"当前没有正在进行的电脑操控任务，无法{'暂停' if act == 'pause' else ('恢复' if act == 'resume' else '停止')}",
            source=source,
            run=run,
        )

    outcome = _computer_mutate(act, run_id)
    result = outcome.get("result")
    if outcome.get("error"):
        return _fail(
            outcome["error"],
            action=act,
            source=outcome.get("source", ""),
            error_code=outcome.get("error_code", ""),
            run_id=run_id,
        )
    if isinstance(result, dict):
        if result.get("ok") is False:
            return _fail(
                str(result.get("msg") or result.get("error") or "电脑操控操作被拒绝"),
                action=act,
                source=outcome.get("source", ""),
                error_code=str(result.get("error_code") or ""),
                run_id=run_id,
                run={k: v for k, v in result.items() if k != "ok"},
            )
        payload = {k: v for k, v in result.items() if k != "ok"}
        # 桥层返回的是完整快照（自带 run_id / status 等字段）：先铺快照再兜底
        # 补 run_id，避免与本地默认关键字重名
        merged: dict[str, Any] = {"action": act, "source": outcome.get("source", "")}
        merged.update(payload)
        merged.setdefault("run_id", run_id)
        return _ok(**merged)
    return _ok(action=act, source=outcome.get("source", ""), run_id=run_id, result=result)


# ---------------------------------------------------------------------------
# 工具契约
# ---------------------------------------------------------------------------


def build_specs() -> list[ToolSpec]:
    """返回 5 个管理交互工具契约（供工具注册表装配）。"""
    rect_schema = {
        "type": "object",
        "description": "面板矩形（物理/逻辑像素以桥层校验为准），需含 x/y/width/height",
        "properties": {
            "x": {"type": "number", "description": "左上角 x"},
            "y": {"type": "number", "description": "左上角 y"},
            "width": {"type": "number", "description": "宽度（像素）"},
            "height": {"type": "number", "description": "高度（像素）"},
        },
        "required": ["x", "y", "width", "height"],
        "additionalProperties": True,
    }

    return [
        ToolSpec(
            name="pty_manage",
            description=(
                "管理常驻 shell 伪终端：ensure 创建/复用会话、write 写入文本、"
                "resize 调整尺寸、kill 终止、state 查看状态与最近输出。"
                "适合需要交互式会话（cd 保持、REPL）的场景；一次性命令仍用 execute_command。"
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": list(PTY_ACTIONS),
                        "description": "操作类型，默认 state",
                    },
                    "data": {
                        "type": "string",
                        "description": "write 时写入终端的文本（可含换行）",
                    },
                    "cols": {"type": "integer", "description": "终端列数，默认 80"},
                    "rows": {"type": "integer", "description": "终端行数，默认 24"},
                },
                required=["action"],
            ),
            execute=pty_manage,
            read_only=False,
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="管理终端会话",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="browser_pane",
            description=(
                "驱动 GUI 内嵌浏览器面板：open 打开、close 关闭、navigate 导航、"
                "back/forward/reload 前进后退刷新、state 查看状态、set_bounds 调整位置。"
                "仅在 GUI 运行环境可用，非 GUI 环境会明确报错。"
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": list(BROWSER_ACTIONS),
                        "description": "操作类型",
                    },
                    "url": {"type": "string", "description": "open/navigate 使用的地址"},
                    "rect": rect_schema,
                    "zoom": {
                        "type": "number",
                        "description": "页面缩放（1.0 为原始比例），默认 1.0",
                    },
                },
                required=["action"],
            ),
            execute=browser_pane,
            read_only=False,
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="控制浏览器面板",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="screenshot_capture",
            description=(
                "截取桌面画面并保存为图片文件：mode=full 全屏（含多显示器），"
                "mode=region 截取 x/y/width/height 指定区域。默认保存到 AI 输出目录，"
                "返回真实文件路径。没有桌面会话时返回真实错误。"
            ),
            input_schema=_schema(
                {
                    "mode": {
                        "type": "string",
                        "enum": list(SCREENSHOT_MODES),
                        "description": "截屏范围，默认 full",
                    },
                    "x": {"type": "integer", "description": "region 模式左上角 x"},
                    "y": {"type": "integer", "description": "region 模式左上角 y"},
                    "width": {"type": "integer", "description": "region 模式宽度"},
                    "height": {"type": "integer", "description": "region 模式高度"},
                    "output_path": {
                        "type": "string",
                        "description": "可选保存路径；留空则保存到 AI 输出目录",
                    },
                }
            ),
            execute=screenshot_capture,
            read_only=False,
            # 只写截图产物文件（default 落 AI 输出目录，返回值即真实路径），
            # 不属于受控源码编辑，因此不声明 mutates_files、不纳入按回合撤销记录器。
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="截取屏幕画面",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="clipboard_image",
            description=(
                "读取系统剪贴板中的图片并保存为文件（read 读取并落盘，save 保存到指定路径），"
                "返回真实文件路径；剪贴板无图片时返回明确错误。"
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": list(CLIPBOARD_ACTIONS),
                        "description": "操作类型，默认 read",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "可选保存路径；留空则保存到 AI 输出目录",
                    },
                }
            ),
            execute=clipboard_image,
            read_only=False,
            # 只写剪贴板图片落盘文件（返回值即真实产物路径），不属于受控源码编辑，
            # 因此不声明 mutates_files、不纳入按回合撤销记录器。
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="读取剪贴板图片",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="computer_control",
            description=(
                "电脑操控状态与运行控制：status 查看当前 run 状态与设置、"
                "pause 暂停、resume 恢复、stop 紧急停止、browser 查看可用浏览器选项。"
                "不提供权限档位或信任类操作。"
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": list(COMPUTER_ACTIONS),
                        "description": "操作类型",
                    }
                },
                required=["action"],
            ),
            execute=computer_control,
            read_only=False,
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="控制电脑操控运行",
            side_effect_scope="system",
        ),
    ]
