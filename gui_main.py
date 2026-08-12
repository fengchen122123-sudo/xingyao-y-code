"""
gui_main.py
Y-CODE 现代原生 GUI 窗口启动入口
基于 PyWebView + Vue 3 + TypeScript + Vite 模块化架构
"""
import os
import sys
import json
import socket
import webview
from gui_bridge import ApiBridge

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
        # Vite 开发服务器的 index.html 通常包含 ycode/y-code 或 vite 特征
        return "ycode" in body or "y-code" in body or "vite" in body
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


def main():
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

    import threading
    threading.Thread(target=_register_dnd, daemon=True, name="gui-dnd-register").start()

    # 启动 WebView 消息主循环（默认关闭开发者工具，YCODE_GUI_DEBUG=1 时打开）
    webview.start(debug=_debug_enabled())

if __name__ == "__main__":
    main()
