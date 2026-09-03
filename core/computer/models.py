"""电脑操控领域的不可变值对象、枚举、错误类型与 backend 协议。

本模块不导入 GUI、CLI、LiteLLM、Playwright 或全局配置（设计第 6 节规则 1）。
枚举取值、错误码与字段形状以 docs/superpowers/specs/2026-09-02-computer-control-design.md
第 7、9.3、21 节为准。
"""

import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class ComputerProtocol(str, Enum):
    """电脑操控 Provider 协议（设计 7.1）。"""

    NONE = "none"
    GENERIC = "generic"
    NATIVE_OPENAI = "native_openai"


class ComputerTarget(str, Enum):
    """操控目标（设计 7.1）。"""

    BROWSER = "browser"
    DESKTOP = "desktop"


class BrowserKind(str, Enum):
    """受支持的系统浏览器（设计 7.1）。"""

    EDGE = "edge"
    CHROME = "chrome"


class ComputerRunStatus(str, Enum):
    """run 状态机取值（设计 7.1、14.2）。"""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_USER = "waiting_for_user"
    PROVIDER_SAFETY_REQUIRED = "provider_safety_required"
    COMPLETING = "completing"
    COMPLETED = "completed"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ComputerActionKind(str, Enum):
    """统一动作类型（设计 7.1）。"""

    OBSERVE = "observe"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    MOVE = "move"
    DRAG = "drag"
    SCROLL = "scroll"
    TYPE_TEXT = "type_text"
    KEYPRESS = "keypress"
    WAIT = "wait"
    NAVIGATE = "navigate"
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"
    REQUEST_USER_TAKEOVER = "request_user_takeover"


class ComputerControlError(Exception):
    """电脑操控领域错误；code 为稳定英文错误码（设计第 21 节），message 为中文。"""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details) if details else {}

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class ComputerOwner:
    session_id: str
    turn_id: str
    session_label: str
    cancel_event: threading.Event = field(compare=False, repr=False)


@dataclass(frozen=True)
class ComputerAction:
    kind: str
    target: str
    x: int | None = None
    y: int | None = None
    path: tuple[tuple[int, int], ...] = ()
    button: str = "left"
    delta_x: int = 0
    delta_y: int = 0
    text: str | None = field(default=None, repr=False)
    keys: tuple[str, ...] = ()
    url: str | None = None
    duration_ms: int | None = None
    reason_code: str | None = None
    # 起终点输入在 parse_computer_action 解析时立即规范化为 path，存储层恒为 None。
    end_x: int | None = None
    end_y: int | None = None


@dataclass(frozen=True)
class ComputerObservation:
    """统一观察值对象（设计 7.3）。

    png_path 是短期本地截图路径，绝不进入 repr 或 Provider 投影。
    模型可见字段不得包含时间戳或随机序号，避免相同画面被误判为有进展。
    """

    run_id: str
    target: str
    action: str = ""
    ok: bool = True
    screen_sha256: str = ""
    png_path: str = field(default="", repr=False)
    width: int = 0
    height: int = 0
    original_width: int = 0
    original_height: int = 0
    origin_x: int = 0
    origin_y: int = 0
    scale_x: float = 1.0
    scale_y: float = 1.0
    window_title: str = ""
    page_origin: str = ""
    page_title: str = ""
    error_code: str = ""
    error_message: str = ""
    before_fingerprint: str = ""
    after_fingerprint: str = ""
    preview_url: str = ""


@dataclass(frozen=True)
class ComputerCapability:
    """Turn 起点冻结的能力决策（设计 9.3）。"""

    enabled: bool = False
    protocol: str = ComputerProtocol.NONE.value
    targets: tuple[str, ...] = ()
    browser: str = ""
    settings_generation: int = 0
    error_code: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ComputerRunSnapshot:
    """UI/CLI 可见快照（设计 7.4）。

    快照不得包含输入文本、截图 base64、API Key、Cookie 或浏览器 profile 内容；
    本模型只保留脱敏后的展示字段，to_public_dict() 原样输出这些公开字段。
    """

    run_id: str = ""
    owner_session_id: str = ""
    owner_turn_id: str = ""
    status: str = ComputerRunStatus.IDLE.value
    target: str = ""
    browser: str = ""
    model_name: str = ""
    protocol: str = ComputerProtocol.NONE.value
    started_at: float = 0.0
    action_count: int = 0
    current_action: str = ""
    current_surface: str = ""
    preview_url: str = ""
    pause_reason: str = ""
    stop_reason: str = ""
    error_code: str = ""
    hotkey_available: bool = False

    def to_public_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class ComputerBackend(Protocol):
    target: str

    def start(self) -> None: ...

    def observe(self, run_id: str) -> ComputerObservation: ...

    def execute(self, run_id: str, action: ComputerAction) -> None: ...

    def release_inputs(self) -> None: ...

    def close(self) -> None: ...
