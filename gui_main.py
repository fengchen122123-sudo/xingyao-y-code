"""
gui_main.py
Y-CODE 现代原生 GUI 窗口启动入口
基于 PyWebView + Vue 3 + TypeScript + Vite 模块化架构
"""
import os
import sys
import json
import logging
import socket
import threading
from pathlib import Path
import webview
from gui_bridge import ApiBridge
from core.gui_lifecycle import ApplicationLifecycle

logger = logging.getLogger("gui_main")
# 单实例互斥体句柄的模块级引用：句柄一旦被 GC 回收，Windows 会自动释放
# 互斥体，单实例保护即失效，因此必须持有引用直到进程退出。
_SINGLETON_MUTEX = None


def _configure_runtime_logging():
    """GUI 滚动运行日志：打包版无控制台，logger/stderr 输出全部丢失，
    出问题无从诊断。落 ~/.ycode/logs/gui-runtime.log（2MB x 3 滚动）。"""
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
                return  # 已配置，防重复调用
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


def is_port_open(host="127.0.0.1", port=5173) -> bool:
    """检查 Vite 开发服务器端口 5173 是否处于打开监听状态"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        res = s.connect_ex((host, port))
        s.close()
        return res == 0
    except Exception:
        return False

def _is_ycode_vite_dev_server(url: str = "http://127.0.0.1:5173", timeout: float = 1.0) -> bool:
    """校验 5173 端口上的服务确实是 Y-CODE 前端，避免加载其他程序页面导致 RCE。"""
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


def get_frontend_url() -> str:
    # 1. 优先检查 Vite 实时热更新开发服务 (http://localhost:5173)
    #    仅限源码开发模式：打包后的安装版绝不能走这条——用户机器上任何
    #    项目的 Vite dev server（含旧版 ycode 自己的）都会被当作前端加载，
    #    表现为“装了新版打开还是旧界面”（_is_ycode_vite_dev_server 的
    #    vite 特征判断任何 Vite 项目都能通过）。
    if not getattr(sys, "frozen", False) and is_port_open("127.0.0.1", 5173) and _is_ycode_vite_dev_server():
        print("Detected Vite dev server running at http://127.0.0.1:5173, loading HMR live version...")
        return "http://127.0.0.1:5173"

    # 2. 检查 Vue 3 静态编译产物 (frontend/dist/index.html)
    #    必须返回本地路径形式（不能转 file:/// URI）：pywebview 只对“本地路径”
    #    启动内置 HTTP 服务器并注入 js_api；file:// URI 会被当成外部 URL 直连，
    #    JS 桥丢失且 vite 产物的绝对资源路径在 file:// 下 404（白屏根因）。
    base_dir = _get_base_dir()
    dist_path = os.path.abspath(os.path.join(base_dir, "frontend", "dist", "index.html"))
    if os.path.exists(dist_path):
        print(f"Loading compiled Vue 3 production dist from: {dist_path}")
        return dist_path

    print(f"Error: No valid frontend entry point found!", file=sys.stderr)
    sys.exit(1)


def _initial_theme_light() -> bool:
    """启动前读取持久化主题（config.json 的 gui_theme_light，True=浅色）。

    pywebview 默认 private_mode=True，WebView2 无痕模式不持久化 localStorage，
    前端的 localStorage 主题镜像重启后即丢失；主题真值必须随 URL 传给前端，
    同时同步原生窗口背景色，避免浅色主题用户看到黑色启动页/黑闪。
    """
    try:
        from core.model_manager import load_config
        return bool(load_config().get("gui_theme_light", False))
    except Exception:
        return False


def _debug_enabled() -> bool:
    """开发者工具开关：设置环境变量 YCODE_GUI_DEBUG=1 即可打开调试控制台。"""
    return os.environ.get("YCODE_GUI_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _start_preload_thread():
    """后台预热 LLM 引擎：litellm import + tiktoken 编码加载 + 视觉能力探测。

    GUI 的界面渲染在独立 WebView 进程，Python 侧后台预热不会卡 UI 首帧
    （与 TUI 必须同步预热不同，见 main.py 启动预热段的注释）。
    不做预热时，首个回合会在工作线程里现场完成这些 import，
    表现为“第一条消息要等很久”。线程安全由 core.llm._litellm_lock 保证。
    """
    import threading

    def _preload():
        try:
            from core.llm import _get_litellm, _litellm_token_counter, _supports_vision
            _get_litellm()
            model = None
            try:
                from core.model_manager import get_active_model
                model = (get_active_model() or {}).get("litellm_model")
            except Exception:
                pass
            try:
                _litellm_token_counter(
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
    """Windows 命名互斥锁实现单实例保护。

    背景：窗口点 X 只是隐藏（托盘就绪时进程常驻），用户再次双击快捷方式会
    启动第二个实例——右下角出现两个托盘图标，且两个实例并发抢同一份
    ycode.db / WebView2 数据目录，新实例初始化被 SQLite 锁阻塞，表现为
    "一直停留在加载页打不开，关掉重开才能进来"（gui-runtime.log 中 11:44:46
    与 11:45:46 连续两次启动即为该现象的直接证据）。

    本函数返回 True 表示已有实例在运行：本进程应激活已有窗口后退出；
    返回 False 表示本进程是首个实例，可继续正常启动。

    关键改进（2026-08-14）：当已有实例但找不到主窗口时（前一个实例卡在
    加载页/webview.start() 尚未创建窗口句柄，或被用户从托盘退出但进程未
    完全退出），不再静默退出，而是弹窗告知用户"已有实例正在运行/正在启动中"，
    并等待 3 秒后重试激活（覆盖前一个实例正在退出但尚未释放互斥锁的短暂
    窗口期），仍找不到才退出。

    验证思路：连续双击快捷方式两次，第二次应唤起已有窗口；若前一个实例卡死
    在加载页，第二次双击会弹窗提示而不是静默消失；任务管理器只应出现一个
    Y-code.exe；托盘图标恒为 1 个。
    """
    if sys.platform != "win32":
        return False  # 非 Windows 不启用单实例锁
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
    _SINGLETON_MUTEX = mutex  # 持有句柄引用，防止被 GC 后互斥体失效

    if kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
        return False  # 本实例是第一个，正常启动

    # 已有实例：找到其主窗口并唤起，然后本进程退出
    title = "Y-CODE · 星瑶"
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        # SW_RESTORE(9)：若窗口被最小化/隐藏则恢复正常显示
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        logger.info("检测到已有实例，已唤起其主窗口，本实例退出")
        return True

    # 已有实例但窗口未创建（前一个实例可能正卡在 webview.start() 前，或正在退出中）：
    # 等 3 秒重试一次，覆盖前一个实例正在关闭/启动窗口的短暂窗口期
    logger.warning("检测到已有实例但主窗口未创建，等待 3s 后重试激活...")
    time.sleep(3)
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        logger.info("检测到已有实例（重试成功），已唤起其主窗口，本实例退出")
        return True

    # 重试后仍找不到：前一个实例很可能卡在加载页或正在退出
    # 弹窗提示用户，不要静默退出（避免用户以为"双击没反应"）
    logger.warning("检测到已有实例但主窗口始终未创建，弹窗提示用户")
    ctypes.windll.user32.MessageBoxW(
        0,
        "检测到 Y-code 已在运行，但主窗口当前不可见。\n\n"
        "如果右下角托盘已有 Y-code 图标，请右键托盘图标选择“显示主窗口”。\n"
        "如果程序卡死在启动页面，请稍后从托盘菜单退出后重新启动。",
        "Y-code · 星瑶",
        0x40,  # MB_ICONINFORMATION
    )
    return True


def main():
    _configure_runtime_logging()
    # 单实例保护：已有实例运行时直接激活其窗口并退出本进程，避免双开
    if _acquire_single_instance():
        sys.exit(0)
    # 显式传入真正的 MCP 单例，避免桥接层拿到空的 MCP 管理器
    try:
        from core.mcp_client import mcp_manager
    except Exception:
        mcp_manager = None
    api = ApiBridge(mcp_manager=mcp_manager)
    _start_preload_thread()
    url = get_frontend_url()
    theme_light = _initial_theme_light()
    url += ("&" if "#" in url else "#") + "theme=" + ("light" if theme_light else "dark")

    # 启动 PyWebView 原生桌面窗口（背景色与主题一致，页面加载前不闪反色）
    window = webview.create_window(
        title="Y-CODE · 星瑶",
        url=url,
        js_api=api,
        width=1320,
        height=840,
        min_size=(960, 640),
        text_select=True,
        confirm_close=False,
        background_color="#f4f4f1" if theme_light else "#1a1a18"
    )

    api.set_window(window)
    lifecycle = ApplicationLifecycle(
        window,
        api.shutdown,
        icon_path=os.path.join(_get_base_dir(), "assets", "ycode.ico"),
    )
    lifecycle.start_tray()
    window.events.closing += lifecycle.on_closing

    # pywebview 6.2+ 的拖拽文件完整路径注入：WebView2 只在
    # _dnd_state['num_listeners'] > 0（即通过 dom.on('drop') 注册过）时才
    # 放行系统层拖拽事件。前端原生 addEventListener 不会增加计数，
    # 导致 pywebviewFullPath 从未注入。
    def _register_dnd():
        try:
            window.events.loaded.wait(timeout=10)
            body = window.dom.get_element('body')
            if not body:
                return

            def _on_body_drop(event):
                # 事件 dataTransfer.files 已带 pywebviewFullPath（pywebview 注入，
                # 注意：该字段只注入到 Python 侧的事件字典里，JS 侧 File 对象拿不到）
                files = event.get('dataTransfer', {}).get('files', [])
                items = [
                    {'name': f.get('name') or os.path.basename(f.get('pywebviewFullPath') or ''),
                     'path': f.get('pywebviewFullPath')}
                    for f in files if f.get('pywebviewFullPath')
                ]
                if not items:
                    return
                api.store_dropped_paths([it['path'] for it in items])  # 兼容旧前端的轮询方式
                # 事件驱动推送：前端 onDrop 等待此回调拿到完整路径，
                # 替代"前端轮询 Python 存储"的竞态方式（曾导致首次拖拽取不到路径、
                # 以及连续拖拽时读到上一次的旧路径）
                try:
                    payload = json.dumps({'files': items}, ensure_ascii=False)
                    payload = payload.replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
                    window.evaluate_js(
                        f"window.__ycode_on_drop_paths && window.__ycode_on_drop_paths({payload})"
                    )
                except Exception:
                    pass

            body.on('drop', _on_body_drop)
        except Exception:
            pass

    threading.Thread(target=_register_dnd, daemon=True, name="gui-dnd-register").start()

    # 启动 WebView 消息主循环（默认关闭开发者工具，YCODE_GUI_DEBUG=1 时打开）
    try:
        webview.start(debug=_debug_enabled())
    finally:
        lifecycle.finalize()

if __name__ == "__main__":
    main()
