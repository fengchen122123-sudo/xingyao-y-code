import os
import sys
import json
import logging
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Optional


_CLI_FLAG = "--cli"
# CLI 模式自救时打开的 CONOUT$/CONIN$ 流必须持有引用：main.py 会把 sys.stdout
# 重新包一层 TextIOWrapper，旧对象一旦被 GC 回收就会顺带关闭底层 buffer。
_CLI_STDIO_STREAMS = []


def _stream_usable(stream) -> bool:
    """标准流是否真的可用（None 与 runtime hook 的哑对象都应视为不可用）。"""
    if stream is None:
        return False
    try:
        stream.fileno()
        return True
    except Exception:
        return False


def _ensure_cli_stdio() -> None:
    if sys.platform != "win32":
        for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
            if _stream_usable(getattr(sys, name, None)):
                continue
            try:
                stream = open(os.devnull, mode, encoding="utf-8", errors="replace")
            except Exception:
                continue
            _CLI_STDIO_STREAMS.append(stream)
            setattr(sys, name, stream)
        return

    try:
        import ctypes

        ctypes.windll.kernel32.AttachConsole(-1)
    except Exception:
        pass

    for name, device, mode in (
        ("stdin", "CONIN$", "r"),
        ("stdout", "CONOUT$", "w"),
        ("stderr", "CONOUT$", "w"),
    ):
        if _stream_usable(getattr(sys, name, None)):
            continue
        stream = None
        for target in (device, os.devnull):
            try:
                stream = open(
                    target, mode, encoding="utf-8", errors="replace",
                    buffering=1 if mode == "w" else -1,
                )
                break
            except Exception:
                stream = None
        if stream is None:
            continue
        _CLI_STDIO_STREAMS.append(stream)
        setattr(sys, name, stream)


def _run_cli_entry(cli_main) -> int:
    try:
        from config import UnknownConfigKeysError, audit_config_keys

        audit_config_keys()
    except UnknownConfigKeysError as config_key_error:
        print(f"\n🚨 配置校验失败：{config_key_error}", file=sys.stderr)
        print("请修正 config.json 中的未知键（或删除拼错的键）后重试。", file=sys.stderr)
        return 2
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

    args, _unknown = parser.parse_known_args()

    if args.prompt is not None:
        return cli_main.run_headless(prompt=args.prompt, session_name=args.session)
    cli_main.run_cli()
    return 0


def _dispatch_cli_argv() -> None:
    if _CLI_FLAG not in sys.argv:
        return

    try:
        sys.argv.remove(_CLI_FLAG)
    except ValueError:
        pass
    # CLI 专属写代码档位（coding）：core.llm 在 import 期按该变量构建工具表（仅保留核心
    # 编程工具）。main.py 只在自身作为 __main__ 运行时设置它，这里是同一个「真实入口」语义，
    # 必须早于 import main；普通 GUI 启动不进此分支，保留完整编程 + 办公能力。
    os.environ.setdefault("YCODE_TOOL_PROFILE", "coding")
    # 必须先修好标准流：main.py 导入期就会按 isatty() 决定是否打印启动提示。
    _ensure_cli_stdio()

    import main as cli_main

    entry = getattr(cli_main, "main", None)
    if callable(entry):
        raise SystemExit(entry())
    raise SystemExit(_run_cli_entry(cli_main))


def _dispatch_office_convert_argv() -> None:
    if "--ycode-office-convert" not in sys.argv:
        return
    from core.office_preview import (
        WORKER_ARGV_FLAG,
        _write_worker_stderr,
        run_convert_worker,
    )

    if WORKER_ARGV_FLAG not in sys.argv:
        return
    index = sys.argv.index(WORKER_ARGV_FLAG)
    # 标志之后必须正好跟 src / dst 两个参数（顺序即位置参数，不做批量 remove）
    pair = sys.argv[index + 1:index + 3]
    if len(pair) < 2:
        _write_worker_stderr("Office 转换子进程参数不足（需要 SRC 与 DST）")
        raise SystemExit(2)
    raise SystemExit(run_convert_worker(pair[0], pair[1]))


def _dispatch_relay_config_argv() -> None:
    if "--ycode-relay-configure" not in sys.argv and "--ycode-relay-status" not in sys.argv:
        return
    if _CLI_FLAG in sys.argv:
        try:
            sys.argv.remove(_CLI_FLAG)
        except ValueError:
            pass
    _ensure_cli_stdio()

    import argparse
    from core.cloud_relay.service import configure_cloud_relay, read_runtime_status

    if "--ycode-relay-status" in sys.argv:
        print(json.dumps(read_runtime_status(), ensure_ascii=False))
        raise SystemExit(0)

    parser = argparse.ArgumentParser(prog="ycode-relay-configure")
    parser.add_argument("--ycode-relay-configure", action="store_true")
    parser.add_argument("--url", default="")
    parser.add_argument("--device-id", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--disable", action="store_true")
    args, _unknown = parser.parse_known_args()
    res = configure_cloud_relay(
        args.url,
        args.device_id,
        args.token,
        enabled=not args.disable,
    )
    print(json.dumps(res, ensure_ascii=False))
    raise SystemExit(0 if res.get("ok") else 2)


# 必须排在 CLI 分派之前：本进程是被 core.office_preview 用 sys.executable 直接
# 拉起的转换子进程，即使外层启动器（ycode.exe）再注入 --cli，也要先接住转换标志。
_dispatch_office_convert_argv()
_dispatch_relay_config_argv()
_dispatch_cli_argv()


def _dispatch_browser_worker_argv() -> bool:
    """PyInstaller 子进程入口；命中后不导入或启动 GUI。"""
    from core.computer.browser_backend import WORKER_ARGV_FLAG

    if WORKER_ARGV_FLAG not in sys.argv:
        return False
    sys.argv.remove(WORKER_ARGV_FLAG)
    from core.computer.browser_worker import main as worker_main

    raise SystemExit(worker_main())


_dispatch_browser_worker_argv()

# 兜底：安装版是 console=False 的 GUI 子系统程序，本进程没有控制台。第三方库
# （mcp SDK 经 anyio 启动 stdio MCP 服务器）启动控制台子进程时无处传
# creationflags，Windows 会弹出可见的控制台窗口；这里尽早统一兜底，且仅在
# 无控制台时生效（源码调试/CLI 版行为不变）。
from core.gui_lifecycle import hide_child_console_windows

hide_child_console_windows()

import webview
from gui_bridge import ApiBridge
from core.gui_lifecycle import ApplicationLifecycle
from core.gui_runtime_check import check_webview2_runtime, warn_render_failure
from core.gui_theme import ThemeError, get_repository as get_gui_theme_repository

logger = logging.getLogger("gui_main")
# 单实例互斥体句柄的模块级引用：句柄一旦被 GC 回收，Windows 会自动释放
# 互斥体，单实例保护即失效，因此必须持有引用直到进程退出。
_SINGLETON_MUTEX = None

_INSTANCE_INFO_NAME = "gui_instance.json"


def _data_dir() -> Path:
    return Path(os.environ.get("YCODE_DATA_DIR") or (Path.home() / ".ycode"))


_HTTP_PORT_FROZEN = 42001
_HTTP_PORT_SOURCE = 42002


def _frontend_http_port() -> int:
    return _HTTP_PORT_FROZEN if getattr(sys, "frozen", False) else _HTTP_PORT_SOURCE


def _webview_start_options() -> dict:
    return {
        "debug": _debug_enabled(),
        "private_mode": False,
        "storage_path": str(_data_dir() / "webview"),
        "http_port": _frontend_http_port(),
    }


def _write_instance_info():
    try:
        info = {
            "pid": os.getpid(),
            "exe": sys.executable,
            "argv": sys.argv,
            "python": sys.version.split()[0],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        target = _data_dir() / _INSTANCE_INFO_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("写入实例信息文件失败", exc_info=True)


def _read_instance_info() -> Optional[dict]:
    try:
        target = _data_dir() / _INSTANCE_INFO_NAME
        if not target.exists():
            return None
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None


def _remove_instance_info():
    try:
        target = _data_dir() / _INSTANCE_INFO_NAME
        info = _read_instance_info()
        # 只有文件仍归属本进程时才删，避免误删新接管实例的信息
        if info and info.get("pid") == os.getpid():
            target.unlink(missing_ok=True)
    except Exception:
        pass


def _configure_runtime_logging():
    try:
        import logging
        from logging.handlers import RotatingFileHandler

        log_dir = Path(
            os.environ.get("YCODE_DATA_DIR") or (Path.home() / ".ycode")
        ) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        target = log_dir / "gui-runtime.log"

        root = logging.getLogger()
        for handler in root.handlers:
            if getattr(handler, "_ycode_gui_runtime_handler", False):
                return
        handler = RotatingFileHandler(
            target, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        setattr(handler, "_ycode_gui_runtime_handler", True)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        handler.setLevel(logging.INFO)
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)

        logger = logging.getLogger("gui_main")

        def _excepthook(exc_type, exc, tb):
            logger.error("未捕获异常", exc_info=(exc_type, exc, tb))

        def _thread_excepthook(args):
            logger.error(
                "线程 %s 未捕获异常",
                getattr(args.thread, "name", "?"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        sys.excepthook = _excepthook
        threading.excepthook = _thread_excepthook
        logger.info("GUI 运行日志初始化: %s", target)
    except Exception:
        # 日志初始化失败绝不能影响启动
        pass


def _clear_stale_webview_cache():
    try:
        index_path = os.path.join(_get_base_dir(), "frontend", "dist", "index.html")
        if not os.path.exists(index_path):
            return
        stat = os.stat(index_path)
        fingerprint = f"{int(stat.st_mtime)}:{stat.st_size}"
        marker = _data_dir() / "webview" / "dist_fingerprint.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == fingerprint:
            return
        default_root = _data_dir() / "webview" / "EBWebView" / "Default"
        for cache_name in ("Cache", "Code Cache", "GPUCache"):
            shutil.rmtree(default_root / cache_name, ignore_errors=True)
        marker.write_text(fingerprint, encoding="utf-8")
        logger.info("检测到前端产物更新（%s），已清理 WebView2 缓存", fingerprint)
    except Exception:
        logger.debug("清理 WebView2 缓存失败（不影响启动）", exc_info=True)


# 前端产物运行期监控参数：每 2 秒采样一次；发现与基线不同后等 1.5 秒复核
# （vite build 会先清理/覆盖 dist，期间存在"文件缺失或指纹抖动"的中间态，
# 必须等构建落盘、复核指纹稳定后才视为有效变更）。
_DIST_WATCH_INTERVAL_SECONDS = 2.0
_DIST_WATCH_CONFIRM_SECONDS = 1.5


def _frontend_dist_fingerprint():
    try:
        index_path = os.path.join(_get_base_dir(), "frontend", "dist", "index.html")
        stat = os.stat(index_path)
    except OSError:
        return None
    return (int(stat.st_mtime), stat.st_size)


def _frontend_dist_watch_loop(
    sampler=None,
    emit=None,
    stop_event=None,
    sleep_fn=None,
    interval=_DIST_WATCH_INTERVAL_SECONDS,
    confirm_seconds=_DIST_WATCH_CONFIRM_SECONDS,
):
    sampler = sampler or _frontend_dist_fingerprint
    emit = emit if emit is not None else (lambda _fingerprint: None)
    sleep_fn = sleep_fn or time.sleep

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    def _wait(seconds: float) -> bool:
        if stop_event is not None:
            return bool(stop_event.wait(seconds))
        sleep_fn(seconds)
        return False

    baseline = sampler()
    while not _stopped():
        if _wait(interval):
            return
        current = sampler()
        if current == baseline:
            continue
        if _wait(confirm_seconds):
            return
        confirmed = sampler()
        if confirmed is None or confirmed != current:
            continue
        baseline = confirmed
        fingerprint = f"{confirmed[0]}:{confirmed[1]}"
        logger.info("检测到前端产物运行期更新（%s），已通知前端刷新", fingerprint)
        try:
            emit(fingerprint)
        except Exception:
            logger.exception("推送前端产物更新事件失败")


def _start_frontend_dist_watchdog(api):
    stop_event = threading.Event()

    def _emit(fingerprint: str) -> None:
        # 后台线程安全：emit_event 内部走 _queue_js_payload 单消费者派发
        api.emit_event("frontend_dist_updated", {"fingerprint": fingerprint})

    def _watch():
        try:
            _frontend_dist_watch_loop(emit=_emit, stop_event=stop_event)
        except Exception:
            # 监控是 best-effort：线程异常绝不能影响 GUI 主流程
            logger.exception("前端产物运行期监控线程异常退出")

    try:
        threading.Thread(target=_watch, daemon=True, name="gui-dist-watchdog").start()
    except Exception:
        logger.exception("启动前端产物监控线程失败（前端将不会感知运行期重建）")
    return stop_event


def is_port_open(host="127.0.0.1", port=5173) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(0.5)
            return s.connect_ex((host, port)) == 0
        finally:
            s.close()
    except Exception:
        return False

def _is_ycode_vite_dev_server(url: str = "http://127.0.0.1:5173", timeout: float = 1.0) -> bool:
    try:
        from urllib.request import urlopen
        with urlopen(url, timeout=timeout) as resp:
            # 只读取前 8KB，避免大响应拖慢启动
            body = resp.read(8192).decode("utf-8", errors="ignore").lower()
        # 使用应用专属标记；通用的 /@vite/client 不能证明页面属于 Y-CODE。
        return '<meta name="application-name" content="y-code"' in body
    except Exception:
        return False


def _get_base_dir() -> str:
    """获取资源根目录：开发模式为脚本所在目录，PyInstaller 打包后为 _internal 目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.abspath(__file__))


def _frontend_missing_dialog(message: str) -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                "星瑶Code · 启动失败",
                0x10 | 0x00040000,
            )
            return True
        except Exception:
            logger.exception("前端入口缺失告警弹窗失败（回退 stderr）")
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass
    return False


def get_frontend_url() -> str:
    # 1. 优先检查 Vite 实时热更新开发服务 (http://localhost:5173)
    #    仅限源码开发模式：打包后的安装版绝不能走这条——用户机器上任何
    #    项目的 Vite dev server（含旧版 ycode 自己的）都会被当作前端加载，
    #    表现为“装了新版打开还是旧界面”（_is_ycode_vite_dev_server 的
    #    vite 特征判断任何 Vite 项目都能通过）。
    if not getattr(sys, "frozen", False) and is_port_open("127.0.0.1", 5173) and _is_ycode_vite_dev_server():
        print("Detected Vite dev server running at http://127.0.0.1:5173, loading HMR live version...")
        return "http://127.0.0.1:5173"

    base_dir = _get_base_dir()
    dist_path = os.path.abspath(os.path.join(base_dir, "frontend", "dist", "index.html"))
    if os.path.exists(dist_path):
        print(f"Loading compiled Vue 3 production dist from: {dist_path}")
        return dist_path

    # 3. 两个入口都不存在：启动无法继续。绝不能只 print+exit——打包版没有
    #    控制台（console=False），用户看到的是"双击无反应"。先弹不依赖 WebView
    #    的原生告警（含期望路径与修复建议），再退出。
    print(f"Error: No valid frontend entry point found!", file=sys.stderr)
    logger.error(
        "未找到前端入口（期望 %s）",
        os.path.join(base_dir, "frontend", "dist", "index.html"),
    )
    _frontend_missing_dialog(
        "未找到前端入口文件，星瑶Code 无法启动。\n\n"
        f"期望的前端入口：\n{dist_path}\n"
        "（即程序目录下的 frontend/dist/index.html）\n\n"
        "请重新安装 星瑶Code；如果是源码运行，请在 frontend 目录执行\n"
        "npm install 后 npm run build 重新构建前端产物。\n\n"
        f"运行日志：{_data_dir() / 'logs' / 'gui-runtime.log'}"
    )
    sys.exit(1)


def _initial_gui_theme() -> dict:
    try:
        theme = get_gui_theme_repository().get_active()
        return {
            "appearance": theme["appearance"],
            "background": theme["tokens"]["background_app"],
        }
    except ThemeError as error:
        logger.error("启动 GUI 主题无效: %s", error.as_dict())
        startup_error = error
    except (OSError, IOError) as error:
        logger.exception("启动 GUI 主题读取遇到 I/O 错误")
        startup_error = ThemeError("theme_io_failed", "gui_theme_id", str(error))
    except Exception:
        logger.exception("启动 GUI 主题读取发生未预期错误")
        startup_error = ThemeError(
            "theme_operation_failed", "gui_theme_id", "读取启动 GUI 主题失败",
        )
    return {
        "appearance": "dark",
        "background": "#1a1a18",
        "error": startup_error.as_dict(),
    }


def _build_gui_theme_fragment(gui_theme: dict) -> str:
    fragment = (
        f"theme={gui_theme['appearance']}"
        f"&bg={gui_theme['background'].lstrip('#')}"
    )
    # Win10 无 DWMWA_BORDER_COLOR 可隐藏 frameless 外框（core/frameless.py），
    # 前端据 os=win10 补 1px CSS 内边框勾勒窗口边界；Win11 由 DWM 阴影+圆角
    # 勾勒边界，无需标记。getattr 防御：getwindowsversion 仅 Windows 存在。
    get_win_ver = getattr(sys, "getwindowsversion", None)
    if get_win_ver is not None and get_win_ver().build < 22000:
        fragment += "&os=win10"
    if gui_theme.get("error") is not None:
        fragment += "&gui_theme_error=1"
    return fragment


def _debug_enabled() -> bool:
    """开发者工具开关：设置环境变量 YCODE_GUI_DEBUG=1 即可打开调试控制台。"""
    return os.environ.get("YCODE_GUI_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _start_preload_thread():
    import threading

    def _preload():
        try:
            from core.llm import _token_counter, _supports_vision
            model = None
            try:
                from core.model_manager import get_active_model
                model = (get_active_model() or {}).get("litellm_model")
            except Exception:
                pass
            try:
                _token_counter(
                    model or "openai/gpt-4o-mini",
                    [{"role": "user", "content": "预热"}],
                )
            except Exception:
                pass
            try:
                if model:
                    _supports_vision(model)
            except Exception:
                pass
        except Exception:
            # 预热失败绝不能影响启动：首个回合会按原有惰性路径加载
            pass

    threading.Thread(target=_preload, daemon=True, name="gui-preload").start()


def _acquire_single_instance() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    import time

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    # Local 域（登录会话内可见）：避免 Global 域需要管理员权限创建互斥体。
    # 命名含项目标识即可，升级安装不改变名字，保证新旧版本互相排斥。
    mutex_name = "Local\\YCODE_GUI_SINGLETON"
    mutex = kernel32.CreateMutexW(None, False, mutex_name)
    if not mutex:
        logger.warning("创建单实例互斥体失败，跳过单实例保护（启动将不受限制）")
        return False

    global _SINGLETON_MUTEX
    _SINGLETON_MUTEX = mutex

    if kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
        _write_instance_info()
        return False

    existing = _read_instance_info()
    if existing:
        logger.warning(
            "检测到已有实例（互斥体被持有）: PID=%s exe=%s python=%s started=%s",
            existing.get("pid"),
            existing.get("exe"),
            existing.get("python"),
            existing.get("started_at"),
        )
    else:
        logger.warning("检测到已有实例（互斥体被持有），但无实例信息文件（可能是旧版本实例）")

    def _find_instance_hwnd():
        title = "星瑶Code · 星瑶"
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == title:
                    found.append(hwnd)
                    return False
            return True

        user32.EnumWindows(_cb, 0)
        return found[0] if found else 0

    hwnd = _find_instance_hwnd()
    if hwnd:
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        logger.info("检测到已有实例，已唤起其主窗口，本实例退出")
        return True

    logger.warning("检测到已有实例但主窗口未创建，等待窗口出现或旧实例退出...")
    WAIT_OBJECT_0 = 0
    WAIT_ABANDONED = 0x80
    deadline = time.time() + 20
    while time.time() < deadline:
        hwnd = _find_instance_hwnd()
        if hwnd:
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            logger.info("检测到已有实例（等待后窗口已创建），已唤起其主窗口，本实例退出")
            return True
        wait_result = kernel32.WaitForSingleObject(mutex, 0)
        if wait_result in (WAIT_OBJECT_0, WAIT_ABANDONED):
            logger.info("旧实例已退出并释放互斥锁，本实例接管启动")
            _write_instance_info()
            return False
        time.sleep(0.5)

    # 20 秒后窗口仍未出现且互斥锁未释放：前一个实例很可能卡在加载页。
    # 弹窗提示用户，不要静默退出（避免用户以为"双击没反应"）。
    logger.warning("检测到已有实例但主窗口始终未创建，弹窗提示用户")
    user32.MessageBoxW(
        0,
        "检测到 星瑶Code 已在运行，但主窗口当前不可见。\n\n"
        "如果右下角托盘已有 星瑶Code 图标，请右键托盘图标选择“显示主窗口”。\n"
        "如果程序卡死在启动页面，请稍后从托盘菜单退出后重新启动。",
        "星瑶Code · 星瑶",
        0x40,
    )
    return True


def _create_main_window(api, url: str, gui_theme: dict):
    """创建主窗口，并从源头禁用 pywebview 的 1px DWM frame 延伸。"""
    return webview.create_window(
        title="星瑶Code · 星瑶",
        url=url,
        js_api=api,
        width=1178,
        height=840,
        min_size=(960, 640),
        text_select=True,
        confirm_close=False,
        background_color=gui_theme["background"],
        frameless=True,
        easy_drag=False,
        # 默认 shadow=True 会先 DwmExtendFrameIntoClientArea(1px)，形成固定灰线。
        # WS_THICKFRAME 稍后仍提供系统阴影、Snap 与动画语义。
        shadow=False,
    )


def _startup_computer_runtime() -> None:
    try:
        from core.computer.runtime import (
            attach_preview_publisher,
            start_computer_runtime,
        )

        attach_preview_publisher(start_computer_runtime())
    except Exception:
        logger.exception("启动电脑操控运行时失败（电脑操控保持不可用）")


def _shutdown_computer_runtime() -> None:
    try:
        from core.computer.runtime import shutdown_process_computer_runtime

        shutdown_process_computer_runtime("gui_exit")
    except Exception:
        logger.exception("退出时关闭电脑操控运行时失败")


def _finalize_gui_exit(lifecycle) -> None:
    lifecycle.finalize()
    _shutdown_computer_runtime()


def _audit_config_keys() -> None:
    """T2 配置未知键自检：默认仅写 GUI 运行日志（现有警告通道）；
    strict_config=true 时原生弹窗报错并拒绝启动（严格模式的配置错误必须用户可见）。"""
    try:
        from config import UnknownConfigKeysError, audit_config_keys

        audit_config_keys()
    except UnknownConfigKeysError as error:
        logger.error("配置校验失败（strict_config=true）：%s", error)
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                "config.json 存在未知配置键，且已开启严格校验（strict_config=true），"
                "启动已中止。\n\n"
                f"{error}\n\n"
                "请修正或删除这些键后重新启动。",
                "星瑶Code · 配置错误",
                0x10 | 0x00040000,
            )
        except Exception:
            logger.exception("配置错误告警弹窗失败")
        sys.exit(1)
    except Exception:
        # 自检属于 best-effort 诊断，自身故障绝不能反过来阻断启动
        logger.debug("配置未知键自检失败（忽略）", exc_info=True)


def main():
    _configure_runtime_logging()
    # 内嵌 WebView2 的默认内存参数：进程级环境变量注入（pywebview 的
    # edgechromium 后端以及 core.embedded_browser 直接 new 的控件都会读到它）。
    # 必须用 setdefault——用户系统里已有的 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
    # 优先，绝不覆盖；参数串的唯一数据源是 core.embedded_browser 里的常量，这里
    # 不重复写一份字面量，避免两处漂移。只能在此处（任何 WebView2 实例化之前）
    # 注入：WebView2 环境在首次创建控件时就固化了。
    try:
        from core.embedded_browser import _DEFAULT_BROWSER_MEMORY_ARGS

        os.environ.setdefault(
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", _DEFAULT_BROWSER_MEMORY_ARGS
        )
    except Exception:
        logger.debug("注入 WebView2 默认内存参数失败（保持浏览器默认行为）", exc_info=True)
    logger.info(
        "GUI 进程启动: PID=%s exe=%s python=%s argv=%s",
        os.getpid(),
        sys.executable,
        sys.version.split()[0],
        sys.argv,
    )
    # 单实例保护：已有实例运行时直接激活其窗口并退出本进程，避免双开
    if _acquire_single_instance():
        sys.exit(0)
    _audit_config_keys()
    # 显式传入真正的 MCP 单例，避免桥接层拿到空的 MCP 管理器
    try:
        from core.mcp_client import mcp_manager
    except Exception:
        mcp_manager = None
    # 插件信任校验已由 core/plugin_loader 移除，这里不再注册确认回调：GUI 进程
    # 没有可交互终端，一旦有路径落到控制台 input()，提示用户看不见、等待输入会把
    # 触发刷新的回合线程永久挂住（2026-11-04 实测：plugin_manage 卡死 5.5 小时，
    # 停止按钮也无法中断）。
    api = ApiBridge(mcp_manager=mcp_manager)
    _start_preload_thread()
    try:
        from core.computer.controller import get_computer_control_service
        from core.computer.settings import (
            register_browser_viewport_listener,
            set_process_default_browser,
        )

        set_process_default_browser("embedded")
        get_computer_control_service().set_embedded_endpoint_provider(
            api.embedded_browser_automation_endpoint
        )

        def _on_browser_viewport_changed(_workdir, viewport):
            # 事件必须带发起会话：browser_set_viewport 由某个会话的回合调用，前端据此
            # 把浏览器页签开进该会话。缺 session_name 时前端只能回落「当前查看的会话」，
            # 后台会话改一次分辨率就会把浏览器面板抢到用户正在看的另一个窗口上
            # （2026-10-28 用户反馈：B 会话调 browser_set_viewport，页签冒到 A 会话）。
            try:
                from core import app_context

                session_name = (
                    app_context.get_thread_session()
                    or app_context.get_current_session()
                    or ""
                )
            except Exception:
                session_name = ""
            api.emit_event(
                "embedded_browser_automation",
                {
                    "width": viewport[0],
                    "height": viewport[1],
                    "session_name": session_name,
                },
            )

        register_browser_viewport_listener(_on_browser_viewport_changed)
    except Exception:
        logger.exception("接线内置浏览器自动化通道失败（保持外部浏览器可用）")

    check_webview2_runtime()

    _clear_stale_webview_cache()
    url = get_frontend_url()
    gui_theme = _initial_gui_theme()
    url += ("&" if "#" in url else "#") + _build_gui_theme_fragment(gui_theme)
    window = _create_main_window(api, url, gui_theme)

    api.set_window(window)
    lifecycle = ApplicationLifecycle(
        window,
        api.shutdown,
        icon_path=os.path.join(_get_base_dir(), "assets", "ycode.ico"),
        has_running_work=api.has_active_turns,
    )
    api._tray_ready_flag = lambda: lifecycle.is_tray_ready()
    api._exit_application = lifecycle.exit_application
    lifecycle.start_tray()
    window.events.closing += lifecycle.on_closing

    try:
        api.start_automation_engine()
    except Exception:
        logging.getLogger(__name__).exception("启动自动化调度引擎失败")

    _startup_computer_runtime()

    # pywebview 桥注入看门狗：NavigationCompleted → inject_pywebview 在独立
    # 线程执行，且首行 run_js 位于 try 之外——快速重启（上一个实例的 WebView2
    # 浏览器进程尚未退净）等竞态下该线程可能异常终止或从未启动，前端
    # pywebviewready 永不触发，启动页永久卡死。这里 15 秒探测一次注入结果，
    # 失败则用与 edgechromium.on_navigation_completed 完全相同的调用重新注入，
    # 把偶发竞态从"死屏"变成"晚几秒进入"。
    def _register_bridge_watchdog():
        ready_js = (
            "!!(window.pywebview && window.pywebview.api"
            " && typeof window.pywebview.api.get_initial_state === 'function')"
        )

        def _probe() -> bool:
            try:
                return bool(window.evaluate_js(ready_js))
            except Exception:
                return False

        def _watch():
            for _ in range(15):
                time.sleep(1.0)
                if _probe():
                    return
            logger.warning("pywebview 桥 15 秒未注入，尝试重新注入 js bridge")
            try:
                from webview.util import inject_pywebview

                inject_pywebview("edgechromium", window)
            except Exception:
                logger.exception("重新注入 pywebview 桥失败")
                return
            for _ in range(10):
                time.sleep(1.0)
                if _probe():
                    logger.info("pywebview 桥重新注入成功")
                    return
            logger.error("pywebview 桥重新注入后仍未就绪，前端将提示连接失败")
            # 原生兜底告警：此刻前端大概率根本没起来（WebView2 初始化失败、
            # 渲染进程被安全软件拦截等），用户面对的是一片空白窗口。原生弹窗
            # 不依赖 WebView，黑屏下依然可见，且直接给出日志路径——否则反馈者
            # 只能提供一张纯色截图（见 2026-09-16 Win10 黑屏反馈）。
            warn_render_failure(_data_dir() / "logs" / "gui-runtime.log")

        threading.Thread(
            target=_watch, daemon=True, name="gui-bridge-watchdog"
        ).start()

    _register_bridge_watchdog()

    dist_watchdog_stop = _start_frontend_dist_watchdog(api)

    def _register_dnd():
        try:
            # loaded 事件 + 轮询双保险：冻结版冷启动（杀毒扫描）可能超过单次
            # wait 超时；body 拿不到时 5s 重试，最多 60s。订阅成功前
            # _dnd_state['num_listeners'] 为 0，WebView2 不放行系统拖拽（拖入无效），
            # 所以必须保证慢启动下也能完成订阅。
            body = None
            for _ in range(12):
                if window.events.loaded.wait(timeout=5):
                    body = window.dom.get_element('body')
                    if body:
                        break
                logger.info("等待页面就绪以注册拖拽订阅（冷启动慢）...")
            if not body:
                logger.error("60s 内未能注册拖拽订阅：拖放文件功能将不可用")
                return

            def _on_body_drop(event):
                files = event.get('dataTransfer', {}).get('files', [])
                items = [
                    {'name': f.get('name') or os.path.basename(f.get('pywebviewFullPath') or ''),
                     'path': f.get('pywebviewFullPath')}
                    for f in files if f.get('pywebviewFullPath')
                ]
                if not items:
                    return
                api.store_dropped_paths([it['path'] for it in items])  # 兼容旧前端的轮询方式
                # 事件驱动推送：必须走 api 的 _js_queue 单消费者派发（evaluate_js
                # 只允许 dispatcher 线程调用）。此回调运行在 GUI 线程（WebView2 的
                # WebMessageReceived 直接分发），回调内同步 evaluate_js 会因脚本的
                # continuation 排在 GUI 消息队列而死锁整个窗口（拖放必卡死）。
                api.emit_event("dropped_paths_push", {"files": items})

            body.on('drop', _on_body_drop)
        except Exception:
            pass

    threading.Thread(target=_register_dnd, daemon=True, name="gui-dnd-register").start()

    # pywebview 的 asset 路由先 set_header 再 return static_file(...)，bottle
    # 会丢弃前者——no-cache 从未真正发出，WebView2 启发式缓存 index.html 后，
    # 端口复用 + dist 更新会让启动加载过期入口资产直接白屏。必须在服务器开始
    # 服务前补上真正的缓存控制头。
    from core.webview_no_cache import apply as apply_webview_no_cache

    apply_webview_no_cache()

    # 启动 WebView 消息主循环（默认关闭开发者工具，YCODE_GUI_DEBUG=1 时打开）
    logger.info("准备启动 WebView 消息主循环（webview.start）")
    try:
        webview.start(**_webview_start_options())
        logger.info("webview.start 已返回（窗口/消息循环结束）")
    except Exception:
        logger.exception("webview.start 异常退出（GUI 未能启动）")
        warn_render_failure(_data_dir() / "logs" / "gui-runtime.log")
    finally:
        # 退出收尾固定顺序（Task 16）：回合级 shutdown（含取消/线程 drain）
        # 先于电脑操控运行时统一关闭；异常路径同样经过本 finally。
        # 前端产物监控线程先停：webview 主循环已结束，无前端可通知
        # （daemon=True 兜底，句柄为 None 时跳过，退出收尾不受影响）。
        if dist_watchdog_stop is not None:
            dist_watchdog_stop.set()
        _finalize_gui_exit(lifecycle)
        _remove_instance_info()

if __name__ == "__main__":
    try:
        from core.net_env import sanitize_dead_local_proxy

        sanitize_dead_local_proxy()
    except Exception:
        pass
    main()
