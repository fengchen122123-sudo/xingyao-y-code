"""Headless serve 档（Personal Agent worker 的第一段）。

本机验收监听 127.0.0.1:8766。容器里把 YCODE_SERVE_BIND 设为 0.0.0.0，
端口仍是 8766。浏览器链和网页不在这一段。
"""

SERVE_BIND = "127.0.0.1"
SERVE_PORT = 8766
