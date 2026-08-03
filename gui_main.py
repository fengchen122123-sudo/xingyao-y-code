"""
gui_main.py
Y-CODE 现代原生 GUI 窗口启动入口
基于 PyWebView + Vue 3 + TypeScript + Vite 模块化架构
"""
import os
import sys
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
    if is_port_open("127.0.0.1", 5173) and _is_ycode_vite_dev_server():
        print("Detected Vite dev server running at http://127.0.0.1:5173, loading HMR live version...")
        return "http://127.0.0.1:5173"

    # 2. 检查 Vue 3 静态编译产物 (frontend/dist/index.html)
    base_dir = _get_base_dir()
    dist_path = os.path.abspath(os.path.join(base_dir, "frontend", "dist", "index.html"))
    if os.path.exists(dist_path):
        print(f"Loading compiled Vue 3 production dist from: {dist_path}")
        return dist_path

    print(f"Error: No valid frontend entry point found!", file=sys.stderr)
    sys.exit(1)


def _debug_enabled() -> bool:
    """开发者工具开关：设置环境变量 YCODE_GUI_DEBUG=1 即可打开调试控制台。"""
    return os.environ.get("YCODE_GUI_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def main():
    # 显式传入真正的 MCP 单例，避免桥接层拿到空的 MCP 管理器
    try:
        from core.mcp_client import mcp_manager
    except Exception:
        mcp_manager = None
    api = ApiBridge(mcp_manager=mcp_manager)
    url = get_frontend_url()

    # 启动 PyWebView 原生桌面窗口
    window = webview.create_window(
        title="Y-CODE · 星瑶 GUI 编码助手 (Vue 3)",
        url=url,
        js_api=api,
        width=1320,
        height=840,
        min_size=(960, 640),
        text_select=True,
        confirm_close=False,
        background_color="#0f1114"
    )

    api.set_window(window)

    # 启动 WebView 消息主循环（默认关闭开发者工具，YCODE_GUI_DEBUG=1 时打开）
    webview.start(debug=_debug_enabled())

if __name__ == "__main__":
    main()
