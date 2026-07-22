"""
gui_main.py
Y-CODE 现代原生 GUI 窗口启动入口
基于 PyWebView + HTML5/CSS3 全套现代交互
"""
import os
import sys
import webview
from gui_bridge import ApiBridge

def main():
    api = ApiBridge()
    html_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "demo", "ycode_gui_demo.html"))
    
    if not os.path.exists(html_path):
        print(f"Error: HTML template not found at {html_path}", file=sys.stderr)
        sys.exit(1)

    # 启动 PyWebView 原生桌面窗口
    window = webview.create_window(
        title="Y-CODE · 星瑶 GUI 编码助手",
        url=html_path,
        js_api=api,
        width=1320,
        height=840,
        min_size=(960, 640),
        text_select=True,
        confirm_close=False,
        background_color="#0d1117"
    )

    api.set_window(window)
    
    # 启动 WebView 消息主循环
    webview.start(debug=True)

if __name__ == "__main__":
    main()
