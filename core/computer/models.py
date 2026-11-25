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
    LIST_SURFACES = "list_surfaces"
    ACTIVATE_SURFACE = "activate_surface"
    INVOKE_SURFACE = "invoke_surface"
    OBSERVE_REGION = "observe_region"
    SEQUENCE = "sequence"
    LIST_ELEMENTS = "list_elements"
    CLICK_ELEMENT = "click_element"
    SET_VALUE_ELEMENT = "set_value_element"
    WAIT_FOR = "wait_for"


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
    width: int | None = None
    height: int | None = None
    path: tuple[tuple[int, int], ...] = ()
    button: str = "left"
    delta_x: int = 0
    delta_y: int = 0
    unit: str | None = None
    text: str | None = field(default=None, repr=False)
    keys: tuple[str, ...] = ()
    url: str | None = None
    duration_ms: int | None = None
    reason_code: str | None = None
    surface_ref: str | None = None
    surface_generation: int | None = None
    end_x: int | None = None
    end_y: int | None = None
    # sequence 批量动作的子步骤（仅 kind="sequence" 非空；子项本身也是
    # 不可变 ComputerAction，kind 限定为无副作用定位的八种基本输入动作）。
    steps: tuple["ComputerAction", ...] = ()
    element_ref: str | None = None
    condition: str | None = None
    window_name: str | None = None
    timeout_ms: int | None = None
    interval_ms: int | None = None


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
    # B 方案（窗口被遮挡时的降级观察）：页面正文摘要（只含可见文本，已按码点
    # 截断）。窗口最小化/被遮挡时 WebView2 渲染被节流，截图可能卡住或失败，
    # 此时模型仍能凭正文判断页面内容。repr=False：正文属于页面内容，绝不进日志。
    page_text: str = field(default="", repr=False)
    # 本次未取到截图的原因（空串表示截图正常）：只含异常类名的简短中文说明，
    # 绝不含堆栈或页面 URL，用于告知模型"这次没有图"而不是让它以为页面空白。
    screenshot_error: str = ""
    error_code: str = ""
    error_message: str = ""
    before_fingerprint: str = ""
    after_fingerprint: str = ""
    preview_url: str = ""
    # R5：受控画面身份（仅本地使用，绝不进入 Provider 投影）。
    # browser 为本地 page 序号（"page-N"），desktop 为前台窗口本地标识
    # （"hwnd:<句柄>"）；surface_generation 在页切换/前台窗口/几何变化时递增。
    surface_id: str = ""
    surface_generation: int = 0
    # Task 3：视觉帧身份与视口代际（仅本地/协议元数据，不进入 Provider 9 键投影）。
    # frame_id 绑定本次观察的不可变坐标逆变换；不依赖 screen_sha256 充当文档或帧身份。
    frame_id: str = ""
    viewport_generation: int = 0
    surface_candidates: tuple = ()
    elements: tuple = ()
    # 通用化重构 Task C：本动作是否发生了前台切换（输入前置激活），仅本地
    # 自愈判定使用；绝不加入 observation_provider_projection 投影（投影字节不变）。
    input_activated: bool = False
    screen_unchanged: bool = False
    # 桌面观察给模型的补充说明（自绘窗口等）。不进入 9 键投影。
    ui_note: str = ""


@dataclass(frozen=True)
class ObservationFrame:
    """不可变视觉帧记录（Task 3）：绑定观察瞬间的有效坐标域与逆变换。

    由 backend 在 observe 产出初始记录，Provider 若对超限截图重缩放或因超限/无图
    省略截图，必须同步更新到 controller/backend 持有的同一 frame_id 记录上：
    - 正常重缩放：同步更新 image_width / image_height 与 model_to_css 逆比例；
    - 无图或超限且变换未知：coordinate_input_enabled=False，拒绝按旧比例盲点。
    """

    frame_id: str
    run_id: str
    target: str
    surface_id: str = ""
    surface_generation: int = 0
    viewport_generation: int = 0
    origin_x: int = 0
    origin_y: int = 0
    crop_width: int = 0
    crop_height: int = 0
    image_width: int = 0
    image_height: int = 0
    model_to_css_x: float = 1.0
    model_to_css_y: float = 1.0
    coordinate_input_enabled: bool = True
    disabled_reason: str = ""


@dataclass(frozen=True)
class BackendIdentity:
    """一次 run 请求的 backend 不可变身份（R4）：只在本地执行层存在。

    browser/profile_path 取自 Turn 冻结能力快照与领取时解析的规范绝对路径；
    desktop 的 browser/profile_path 恒为空串。身份绝不进入 Provider 消息或
    Journal。cdp_endpoint 仅 embedded（内置 WebView2 CDP 直连）模式使用，
    是 retained 会话身份的一部分：端口变化即视为配置变更、不复用旧实例。
    """

    platform: str
    target: str
    browser: str = ""
    profile_path: str = ""
    cdp_endpoint: str = ""
    viewport_width: int = 0
    viewport_height: int = 0


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


def resolve_browser_mode(target: str, browser: str) -> str:
    """区分内置 WebView2（embedded）与外部 Edge/Chrome（external）。"""
    if target != "browser":
        return ""
    if browser == "embedded":
        return "embedded"
    if browser:
        return "external"
    return ""


@dataclass(frozen=True)
class ComputerRunSnapshot:
    """UI/CLI 可见快照（设计 7.4）。

    快照不得包含输入文本、截图 base64、API Key、Cookie 或浏览器 profile 内容；
    本模型只保留脱敏后的展示字段，to_public_dict() 原样输出这些公开字段。
    """

    run_id: str = ""
    run_generation: int = 0
    event_seq: int = 0
    owner_session_id: str = ""
    owner_turn_id: str = ""
    status: str = ComputerRunStatus.IDLE.value
    target: str = ""
    browser: str = ""
    browser_mode: str = ""
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

    def is_reusable(self) -> bool:
        """retained 领取前裁决（R4）：healthy、未关闭、无 pending/running 命令。

        返回 False 的留存实例不得复用；有未结束 worker 的实例不得进入 retained。
        """
        ...
