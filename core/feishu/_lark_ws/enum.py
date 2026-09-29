"""飞书长连接枚举（搬运自 lark-oapi 1.7.3，MIT）。

上游：lark_oapi/ws/enum.py 与 lark_oapi/core/enum.py 的 LogLevel。
"""

from enum import Enum


class FrameType(Enum):
    CONTROL = 0
    DATA = 1


class MessageType(Enum):
    EVENT = "event"
    CARD = "card"
    PING = "ping"
    PONG = "pong"


class LogLevel(Enum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50
