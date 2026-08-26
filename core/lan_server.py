"""局域网手机远程控制的 HTTP + WebSocket 单端口服务骨架。

Task 1 仅提供基础骨架：IP 探测、绑定 URL 生成与事件广播。
真实 HTTP/WebSocket 服务在后续任务中补全。
"""

import socket
import threading
from typing import Dict


class LanServer:
    """在 Y-code 进程内运行的局域网远程控制服务。

    与桌面 GUI 共享同一 bridge，使手机通过 Web 页面作为第二个前端。
    """

    def __init__(self, bridge=None, port=0):
        self.bridge = bridge
        self.port = port
        self.lan_ip = self._detect_lan_ip()
        self._clients = []
        self._clients_lock = threading.Lock()
        self._token = None
        self._token_expires = 0.0
        self._ttl = 86400

    def _detect_lan_ip(self) -> str:
        """探测本机局域网出口 IP，失败回退到 hostname 解析。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            return socket.gethostbyname(socket.gethostname())

    def bind_url(self, token: str) -> str:
        """生成手机扫码打开时携带配对 token 的绑定 URL。"""
        return f"http://{self.lan_ip}:{self.port}/?token={token}"

    def broadcast(self, payload: str) -> None:
        """向所有已连接客户端发送事件 payload，异常吞掉。"""
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.send(payload)
            except Exception:
                pass

    def start(self) -> Dict:
        """占位实现：真实 HTTP + WebSocket 服务在 Task 2 补全。"""
        return {"ok": True, "host": "0.0.0.0", "port": self.port, "lan_ip": self.lan_ip}

    def stop(self) -> None:
        """占位实现：关闭服务。"""
        pass
