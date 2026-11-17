"""极简中转服务器：设备 WS 注册 + App WS 只读接入 + 页面托管。"""

from .app import create_app

__all__ = ["create_app"]
