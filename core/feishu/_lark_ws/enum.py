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
