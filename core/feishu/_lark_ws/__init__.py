"""飞书长连接传输层（搬运自 lark-oapi 1.7.3，MIT）。

对外只暴露 Client 与少量枚举/异常；帧编解码在 frame.py（本地实现）。
来源与改造清单见同目录 README.md。
"""

from .client import Client
from .enum import FrameType, LogLevel, MessageType
from .exception import (
    ClientException,
    ConnectionClosedException,
    HeaderNotFoundException,
    ServerException,
    ServerUnreachableException,
)
from .frame import Frame, Header, HeaderList, get_header

__all__ = [
    "Client",
    "Frame",
    "Header",
    "HeaderList",
    "get_header",
    "FrameType",
    "MessageType",
    "LogLevel",
    "ClientException",
    "ServerException",
    "HeaderNotFoundException",
    "ConnectionClosedException",
    "ServerUnreachableException",
]
