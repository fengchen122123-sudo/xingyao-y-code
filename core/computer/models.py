"""电脑操控领域的不可变值对象、枚举、错误类型与 backend 协议。

本模块不导入 GUI、CLI、LLM 传输层、Playwright 或全局配置（设计第 6 节规则 1）。
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
    # R10 语义定位与局部观察（desktop only，协议扩展）。
    LIST_SURFACES = "list_surfaces"
    ACTIVATE_SURFACE = "activate_surface"
    INVOKE_SURFACE = "invoke_surface"
    OBSERVE_REGION = "observe_region"
    # 批量微动作（2026-09-06 协议扩展）：一次调用串行执行多个基本输入动作。
    SEQUENCE = "sequence"
    # 阶段二（2026-09-06 协议扩展，desktop only）：UIA 元素观察与元素引用动作。
    LIST_ELEMENTS = "list_elements"
    CLICK_ELEMENT = "click_element"
    SET_VALUE_ELEMENT = "set_value_element"


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
    # R7 滚动单位契约：css_pixel（browser，每轴 ±4000）/ wheel_step（desktop，
    # 每轴 ±10）；None 表示按 target 默认单位。正方向统一为向右/向下。
    unit: str | None = None
    text: str | None = field(default=None, repr=False)
    keys: tuple[str, ...] = ()
    url: str | None = None
    duration_ms: int | None = None
    reason_code: str | None = None
    # R10 语义定位动作（activate_surface/observe_region）引用 list_surfaces
    # 观察给出的候选 ref 与代数；普通坐标动作为 None。
    surface_ref: str | None = None
    surface_generation: int | None = None
    # 起终点输入在 parse_computer_action 解析时立即规范化为 path，存储层恒为 None。
    end_x: int | None = None
    end_y: int | None = None
    # sequence 批量动作的子步骤（仅 kind="sequence" 非空；子项本身也是
    # 不可变 ComputerAction，kind 限定为无副作用定位的八种基本输入动作）。
    steps: tuple["ComputerAction", ...] = ()
    # 阶段二元素引用动作（click_element/set_value_element）引用的控件 ref；
    # 仅在最近一次 list_elements 后有效，普通动作恒为 None。
    element_ref: str | None = None


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
    # R10：list_surfaces 观察给出的语义定位候选（SurfaceCandidate 的字典
    # 形状：ref/kind/label/bounds/operations/surface_generation）；仅本次
    # 观察有效，普通观察恒为空 tuple。
    surface_candidates: tuple = ()
    # 阶段二：list_elements 观察携带的控件元素列表（dict：ref/kind/name/
    # bounds/enabled）；与 surface_candidates 同一生命周期语义，普通观察恒空。
    elements: tuple = ()
    # 通用化重构 Task C：本动作是否发生了前台切换（输入前置激活），仅本地
    # 自愈判定使用；绝不加入 observation_provider_projection 投影（投影字节不变）。
    input_activated: bool = False
    # 帧契约（2026-10-21）：本步截图与**上一步**截图完全相同。用于在 Provider
    # 投影层省掉重复附图的字节——同画面重复发图只烧上下文，模型从上一张图就能
    # 读到内容。同样不进 observation_provider_projection，投影字节保持稳定。
    screen_unchanged: bool = False


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

    def is_reusable(self) -> bool:
        """retained 领取前裁决（R4）：healthy、未关闭、无 pending/running 命令。

        返回 False 的留存实例不得复用；有未结束 worker 的实例不得进入 retained。
        """
        ...
