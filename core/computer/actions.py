"""统一电脑动作的白名单 schema、严格解析与 Provider 投影。

解析规则以 docs/superpowers/specs/2026-09-02-computer-control-design.md 第 7.2、10 节为准：
任何未知字段、缺失字段、bool 伪装整数、NaN 或越界值都抛出
ComputerControlError("computer_invalid_action", ...)，绝不通过删除字段继续。
"""

from collections.abc import Mapping
from urllib.parse import urlsplit

import hashlib

from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerObservation,
)

_COMMON = {"kind", "target"}
_ALLOWED = {
    "observe": _COMMON,
    "click": _COMMON | {"x", "y", "button", "keys"},
    "double_click": _COMMON | {"x", "y", "button", "keys"},
    "move": _COMMON | {"x", "y"},
    "drag": _COMMON | {"path", "x", "y", "end_x", "end_y", "button", "keys"},
    "scroll": _COMMON | {"x", "y", "delta_x", "delta_y", "unit"},
    "type_text": _COMMON | {"text"},
    "keypress": _COMMON | {"keys"},
    "wait": _COMMON | {"duration_ms"},
    "navigate": _COMMON | {"url"},
    "back": _COMMON,
    "forward": _COMMON,
    "reload": _COMMON,
    "request_user_takeover": _COMMON | {"reason_code"},
    # R10 语义定位与局部观察（协议扩展）：desktop 用 surface_ref/generation
    # 定位候选窗口；browser（E5，2026-11-03）用 x/y/width/height 指定 CSS
    # 视口内的区域，用于放大图片型/网格型界面里的小目标。
    "list_surfaces": _COMMON,
    "activate_surface": _COMMON | {"surface_ref", "surface_generation"},
    "invoke_surface": _COMMON | {"surface_ref", "surface_generation"},
    "observe_region": _COMMON | {
        "surface_ref", "surface_generation", "x", "y", "width", "height",
    },
    # 批量微动作（2026-09-06 协议扩展）：一次调用串行执行多个基本输入动作，
    # 中途不截图，完成后只返回最终截图。
    "sequence": _COMMON | {"steps"},
    # 阶段二（2026-09-06 协议扩展；desktop only）：UIA 元素观察与元素引用动作。
    "list_elements": _COMMON,
    "click_element": _COMMON | {"element_ref"},
    "set_value_element": _COMMON | {"element_ref", "text"},
    # Windows-MCP 对齐（2026-11-09 协议扩展，desktop only）条件等待：
    # 按 condition 轮询界面直到命中或超时，元素引用必须来自最近一次
    # list_elements；不提供 duration_ms（那是 wait 的固定休眠语义）。
    "wait_for": _COMMON | {
        "condition", "text", "window_name", "element_ref", "timeout_ms", "interval_ms",
    },
}

_TARGETS = ("browser", "desktop")
_BROWSER_ONLY_KINDS = frozenset({"navigate", "back", "forward", "reload"})
# E2（2026-09-21 元素定位修复）有意协议变更：click_element / set_value_element
# 改为双 target 支持——浏览器 observe 自动携带元素列表（el-N），元素动作按 ref
# 走同一套 Playwright locator 通路；list_elements 仍为桌面专属（浏览器版职责
# 由 observe 直接承担，不提供独立动作，见设计规格 3.1）。
_DESKTOP_ONLY_KINDS = frozenset(
    {"list_surfaces", "activate_surface", "invoke_surface", "list_elements", "wait_for"}
)
_BUTTONS = frozenset({"left", "right", "middle"})
_REASON_CODES = frozenset({"login_required", "captcha_required", "credential_required"})
_KEY_NAMES = frozenset(
    {
        "CTRL", "ALT", "SHIFT", "META", "ENTER", "TAB", "ESC", "SPACE",
        "BACKSPACE", "DELETE", "HOME", "END", "PAGEUP", "PAGEDOWN",
        "UP", "DOWN", "LEFT", "RIGHT",
    }
    | {chr(code) for code in range(ord("A"), ord("Z") + 1)}
    | {str(digit) for digit in range(10)}
    | {f"F{number}" for number in range(1, 13)}
)

MAX_TEXT_CODEPOINTS = 8000
MIN_WAIT_MS = 100
MAX_WAIT_MS = 10000
MAX_KEYS = 8
MIN_PATH_POINTS = 2
MAX_PATH_POINTS = 256

# wait_for 条件等待（Windows-MCP 对齐，2026-11-09 协议扩展，desktop only）：
# - condition 缺省 text_exists；每个条件只认自己的必填字段，无关字段拒绝；
# - text / window_name 为 1~MAX_WAIT_FOR_NAME_CODEPOINTS 个字符；window_name
#   在非 window_active 条件下可选作范围限定；
# - timeout_ms / interval_ms 有默认值，越界直接拒绝（不裁剪、不静默收敛）。
WAIT_FOR_CONDITIONS = ("text_exists", "element_exists", "element_enabled", "window_active")
WAIT_FOR_DEFAULT_CONDITION = "text_exists"
WAIT_FOR_DEFAULT_TIMEOUT_MS = 8000
WAIT_FOR_DEFAULT_INTERVAL_MS = 300
MIN_WAIT_FOR_TIMEOUT_MS = 1000
MAX_WAIT_FOR_TIMEOUT_MS = 30000
MIN_WAIT_FOR_INTERVAL_MS = 100
MAX_WAIT_FOR_INTERVAL_MS = 2000
MAX_WAIT_FOR_NAME_CODEPOINTS = 200

# sequence 批量动作（2026-09-06 协议扩展）：
# - 子步骤白名单：无副作用定位的基本输入动作。observe/request_user_takeover
#   改变操控状态、语义定位依赖观察候选、navigate 是 browser 专属跳转，
#   均不允许打包（必须单独调用并逐次观察）。
# - 2026-11-09（Windows-MCP 对齐）：click_element / set_value_element 可打包，
#   让模型把「点按钮 → 填输入框」按元素引用一次提交，减少往返；它们只按
#   el-N 引用操作，不刷新 ref 表。list_elements 会整体刷新 ref（它之前解析的
#   子步骤引用会失效），故仍必须单独调用、不允许进 sequence。
# - 子步骤 target 必须与 sequence 本身一致；schema 层 items 故意宽松
#   （嵌套完整动作 schema 会成倍膨胀 tools 字节），严格校验在 parse 层。
_SEQUENCE_STEP_KINDS = frozenset(
    {
        "click",
        "double_click",
        "move",
        "drag",
        "scroll",
        "type_text",
        "keypress",
        "wait",
        "click_element",
        "set_value_element",
    }
)
MAX_SEQUENCE_STEPS = 10

# R7 滚动单位契约（显式协议扩展，2026-09-05）：
# - browser：css_pixel（Playwright mouse.wheel 语义），每轴 -4000..4000；
# - desktop：wheel_step（Win32 滚轮步进），每轴 -10..10；
# 正方向统一为向右/向下；超限拒绝（不裁剪），两轴同时为零拒绝。
SCROLL_UNITS = ("css_pixel", "wheel_step")
MAX_BROWSER_SCROLL_PX = 4000
MAX_DESKTOP_SCROLL_STEPS = 10
_TARGET_DEFAULT_SCROLL_UNIT = {"browser": "css_pixel", "desktop": "wheel_step"}

_INVALID = "computer_invalid_action"

COMPUTER_ACTION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "computer_action",
        "description": (
            "对已开启的浏览器或 Windows 桌面执行一个电脑动作并获取最新观察。"
            "第一次必须先 observe；每次只调用一个动作；坐标以最新截图左上角为 (0,0)；"
            "对每个动作后必须根据新截图判断结果，不得猜测成功；"
            "遇到登录、验证码或需要凭据时改用 request_user_takeover 请求用户接管；"
            "完成任务后输出正常最终结论，不继续无意义观察。"
            "scroll 滚动使用 delta_x/delta_y，正方向向右/向下：browser 单位为"
            " CSS 像素（css_pixel，每轴 -4000 到 4000），desktop 单位为滚轮步进"
            "（wheel_step，每轴 -10 到 10）；可用 unit 显式声明，缺省按 target"
            " 默认；两轴同时为零或超限将被拒绝。"
            "desktop 支持语义定位：先用 list_surfaces 观察窗口/托盘候选，"
            "再用 activate_surface（窗口置前台）、invoke_surface（语义调用，"
            "如托盘图标）或 observe_region（按候选边界取得局部原分辨率观察），"
            "并回传候选的 surface_ref 与 surface_generation；候选只在最近一次"
            " list_surfaces 后有效；候选 bounds 为桌面物理坐标，与候选交互"
            "请先用 observe_region 取得局部观察再按其坐标点击。"
            "browser 的 observe_region 用 x/y/width/height（CSS 视口坐标）"
            "裁剪放大该区域；之后再点击时坐标以这块区域左上角为 (0,0)。"
            "等待界面变化优先用 wait_for（text_exists / element_exists /"
            " element_enabled / window_active，desktop only），不要用 wait 加"
            "反复 observe 轮询：wait_for 命中后直接返回新观察，超时返回"
            " computer_wait_timeout，此时改用 observe 检查画面再决定下一步。"
            "sequence 可以打包 click_element / set_value_element 等元素动作，但"
            "元素引用必须来自 sequence 之前最近一次 list_elements 返回的 ref，"
            "否则会报 computer_element_stale。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_ALLOWED)},
                "target": {"type": "string", "enum": list(_TARGETS)},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "path": {
                    "type": "array",
                    "minItems": MIN_PATH_POINTS,
                    "maxItems": MAX_PATH_POINTS,
                    "items": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {"type": "integer"},
                    },
                },
                "end_x": {"type": "integer"},
                "end_y": {"type": "integer"},
                "button": {"type": "string", "enum": sorted(_BUTTONS)},
                "delta_x": {"type": "integer"},
                "delta_y": {"type": "integer"},
                "unit": {"type": "string", "enum": list(SCROLL_UNITS)},
                "text": {"type": "string", "maxLength": MAX_TEXT_CODEPOINTS},
                "keys": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_KEYS,
                    "items": {"type": "string", "enum": sorted(_KEY_NAMES)},
                },
                "url": {"type": "string"},
                "duration_ms": {
                    "type": "integer",
                    "minimum": MIN_WAIT_MS,
                    "maximum": MAX_WAIT_MS,
                },
                "reason_code": {"type": "string", "enum": sorted(_REASON_CODES)},
                "surface_ref": {"type": "string"},
                "surface_generation": {"type": "integer", "minimum": 0},
                "width": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "browser observe_region：区域宽度（CSS 像素）",
                },
                "height": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "browser observe_region：区域高度（CSS 像素）",
                },
                "element_ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": "元素引用（来自最近一次 list_elements 返回的 ref）",
                },
                "condition": {
                    "type": "string",
                    "enum": list(WAIT_FOR_CONDITIONS),
                    "description": (
                        "wait_for 的等待条件，缺省 text_exists：text_exists 需 text；"
                        "element_exists/element_enabled 需 element_ref；"
                        "window_active 需 window_name"
                    ),
                },
                "window_name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_WAIT_FOR_NAME_CODEPOINTS,
                    "description": (
                        "wait_for：窗口标题子串（window_active 必填，"
                        "其余条件可选作范围限定）"
                    ),
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": MIN_WAIT_FOR_TIMEOUT_MS,
                    "maximum": MAX_WAIT_FOR_TIMEOUT_MS,
                    "description": "wait_for：总等待上限（毫秒，默认 8000）",
                },
                "interval_ms": {
                    "type": "integer",
                    "minimum": MIN_WAIT_FOR_INTERVAL_MS,
                    "maximum": MAX_WAIT_FOR_INTERVAL_MS,
                    "description": "wait_for：轮询间隔（毫秒，默认 300）",
                },
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_SEQUENCE_STEPS,
                    "items": {"type": "object"},
                    "description": (
                        "sequence 打包的子动作列表；每项是与 computer_action 相同格式的"
                        "单个动作对象，kind 限 click/double_click/move/drag/scroll/"
                        "type_text/keypress/wait/click_element/set_value_element"
                        "（list_elements 会刷新 ref，必须单独调用），target 必须与本"
                        "动作一致，按顺序执行，中途不截图"
                    ),
                },
            },
            "required": ["kind", "target"],
            "additionalProperties": False,
        },
    },
}


def _invalid(message: str) -> ComputerControlError:
    return ComputerControlError(_INVALID, message)


def _strict_int(value: object, field_name: str) -> int:
    """只接受真正的 int；bool、float（含 NaN）和字符串一律拒绝。"""
    if type(value) is not int:
        raise _invalid(f"{field_name} 必须是整数，收到 {type(value).__name__}")
    return value


def _required_point(payload: Mapping) -> tuple[int, int]:
    if "x" not in payload or "y" not in payload:
        raise _invalid("必须同时提供整数坐标 x/y")
    return _strict_int(payload["x"], "x"), _strict_int(payload["y"], "y")


def _optional_point(payload: Mapping) -> tuple[int | None, int | None]:
    has_x = "x" in payload
    has_y = "y" in payload
    if has_x != has_y:
        raise _invalid("x 和 y 必须同时提供或同时省略")
    if not has_x:
        return None, None
    return _strict_int(payload["x"], "x"), _strict_int(payload["y"], "y")


def _parse_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _invalid("keys 必须是键名数组")
    if not 1 <= len(value) <= MAX_KEYS:
        raise _invalid(f"keys 需要 1-{MAX_KEYS} 个键")
    normalized = []
    for key in value:
        if not isinstance(key, str):
            raise _invalid("keys 只允许键名字符串")
        if key not in _KEY_NAMES:
            raise _invalid(f"不支持的键名: {key!r}")
        normalized.append(key)
    return tuple(normalized)


def _parse_path(value: object) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, (list, tuple)):
        raise _invalid("path 必须是坐标点数组")
    if not MIN_PATH_POINTS <= len(value) <= MAX_PATH_POINTS:
        raise _invalid(f"path 需要 {MIN_PATH_POINTS}-{MAX_PATH_POINTS} 个坐标点")
    points = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise _invalid("path 的每个点必须是两个整数坐标")
        points.append((_strict_int(point[0], "path 点横坐标"), _strict_int(point[1], "path 点纵坐标")))
    return tuple(points)


def _parse_scroll_unit(payload: Mapping, target: str) -> str:
    """滚动单位（R7）：省略时按 target 默认；显式给出时必须与 target 匹配。"""
    default = _TARGET_DEFAULT_SCROLL_UNIT[target]
    if "unit" not in payload:
        return default
    unit = payload["unit"]
    if not isinstance(unit, str) or unit not in SCROLL_UNITS:
        raise _invalid(f"unit 只支持 {sorted(SCROLL_UNITS)}，收到 {unit!r}")
    if unit != default:
        raise _invalid(
            f"target {target} 的滚动只支持 {default} 单位，收到 {unit!r}"
        )
    return unit


def _parse_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid("url 必须是非空字符串")
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        # 访问 port 触发 urlsplit 的端口合法性校验（非法 IPv6 字面量、
        # 非数字或越界端口在此抛 ValueError），统一归为 computer_invalid_action。
        parts.port
    except ValueError as exc:
        raise _invalid(f"url 不是合法地址: {exc}") from exc
    if scheme not in ("http", "https") or not hostname:
        raise _invalid("url 只接受带主机名的 http:// 或 https:// 地址")
    return value


def parse_computer_action(payload: Mapping[str, object]) -> ComputerAction:
    """把模型给出的动作 payload 严格解析为不可变 ComputerAction。"""
    if not isinstance(payload, Mapping):
        raise _invalid("动作必须是对象")
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in _ALLOWED:
        raise _invalid(f"未知动作类型: {kind!r}")
    target = payload.get("target")
    if not isinstance(target, str) or target not in _TARGETS:
        raise _invalid(f"target 必须是 browser 或 desktop，收到 {target!r}")
    unknown = set(payload) - _ALLOWED[kind]
    if unknown:
        raise _invalid(f"动作 {kind} 不允许字段: {sorted(unknown)}")
    if kind in _BROWSER_ONLY_KINDS and target != "browser":
        raise _invalid(f"动作 {kind} 只允许 browser target")
    if kind in _DESKTOP_ONLY_KINDS and target != "desktop":
        raise _invalid(f"动作 {kind} 只允许 desktop target")

    kwargs: dict = {"kind": kind, "target": target}

    if kind in ("click", "double_click", "move"):
        kwargs["x"], kwargs["y"] = _required_point(payload)
    elif kind == "scroll":
        kwargs["x"], kwargs["y"] = _optional_point(payload)
        if "delta_x" in payload:
            kwargs["delta_x"] = _strict_int(payload["delta_x"], "delta_x")
        if "delta_y" in payload:
            kwargs["delta_y"] = _strict_int(payload["delta_y"], "delta_y")
        kwargs["unit"] = _parse_scroll_unit(payload, target)
        limit = (
            MAX_BROWSER_SCROLL_PX
            if kwargs["unit"] == "css_pixel"
            else MAX_DESKTOP_SCROLL_STEPS
        )
        delta_x = kwargs.get("delta_x", 0)
        delta_y = kwargs.get("delta_y", 0)
        if not delta_x and not delta_y:
            raise _invalid("scroll 的 delta_x/delta_y 不能同时为零")
        if abs(delta_x) > limit or abs(delta_y) > limit:
            raise _invalid(
                f"scroll 单位 {kwargs['unit']} 每轴范围 ±{limit}，"
                f"收到 delta_x={delta_x}, delta_y={delta_y}（超限拒绝，不裁剪）"
            )

    if kind == "drag":
        has_path = "path" in payload
        has_endpoints = any(name in payload for name in ("x", "y", "end_x", "end_y"))
        if has_path and has_endpoints:
            raise _invalid("drag 的 path 与起终点字段互斥，只能提供其一")
        if not has_path and not has_endpoints:
            raise _invalid("drag 必须提供 path 或起终点 x/y/end_x/end_y")
        if has_path:
            kwargs["path"] = _parse_path(payload["path"])
        else:
            missing = [name for name in ("x", "y", "end_x", "end_y") if name not in payload]
            if missing:
                raise _invalid(f"drag 起终点缺少字段: {missing}")
            start_x, start_y = _required_point(payload)
            end_x = _strict_int(payload["end_x"], "end_x")
            end_y = _strict_int(payload["end_y"], "end_y")
            kwargs["x"], kwargs["y"] = start_x, start_y
            kwargs["path"] = ((start_x, start_y), (end_x, end_y))

    if kind in ("click", "double_click", "drag"):
        if "button" in payload:
            button = payload["button"]
            if not isinstance(button, str) or button not in _BUTTONS:
                raise _invalid(f"button 只支持 left/right/middle，收到 {button!r}")
            kwargs["button"] = button
        if "keys" in payload:
            kwargs["keys"] = _parse_keys(payload["keys"])

    if kind in ("type_text", "set_value_element"):
        text = payload.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT_CODEPOINTS:
            raise _invalid(f"text 需要 1-{MAX_TEXT_CODEPOINTS} 个字符")
        kwargs["text"] = text

    if kind == "keypress":
        if "keys" not in payload:
            raise _invalid("keypress 必须提供 keys")
        kwargs["keys"] = _parse_keys(payload["keys"])

    if kind == "wait":
        if "duration_ms" not in payload:
            raise _invalid("wait 必须提供 duration_ms")
        duration_ms = _strict_int(payload["duration_ms"], "duration_ms")
        if not MIN_WAIT_MS <= duration_ms <= MAX_WAIT_MS:
            raise _invalid(f"duration_ms 必须在 {MIN_WAIT_MS}-{MAX_WAIT_MS} 毫秒之间")
        kwargs["duration_ms"] = duration_ms

    if kind == "navigate":
        if "url" not in payload:
            raise _invalid("navigate 必须提供 url")
        kwargs["url"] = _parse_url(payload["url"])

    if kind == "request_user_takeover":
        reason_code = payload.get("reason_code")
        if not isinstance(reason_code, str) or reason_code not in _REASON_CODES:
            raise _invalid(f"reason_code 只支持 {sorted(_REASON_CODES)}，收到 {reason_code!r}")
        kwargs["reason_code"] = reason_code

    if kind in ("activate_surface", "invoke_surface"):
        ref = payload.get("surface_ref")
        if not isinstance(ref, str) or not ref:
            raise _invalid(f"{kind} 需要非空 surface_ref（来自最近一次 list_surfaces）")
        generation = _strict_int(payload.get("surface_generation"), "surface_generation")
        if generation < 0:
            raise _invalid("surface_generation 必须是非负整数")
        kwargs["surface_ref"] = ref
        kwargs["surface_generation"] = generation

    if kind == "observe_region":
        if target == "desktop":
            # desktop 沿用语义定位：按候选窗口边界裁剪，区域由候选决定。
            ref = payload.get("surface_ref")
            if not isinstance(ref, str) or not ref:
                raise _invalid(
                    f"{kind} 需要非空 surface_ref（来自最近一次 list_surfaces）"
                )
            generation = _strict_int(
                payload.get("surface_generation"), "surface_generation"
            )
            if generation < 0:
                raise _invalid("surface_generation 必须是非负整数")
            kwargs["surface_ref"] = ref
            kwargs["surface_generation"] = generation
        else:
            # browser（E5）：按 CSS 视口坐标显式指定区域，用于放大密集小目标。
            if "surface_ref" in payload or "surface_generation" in payload:
                raise _invalid("browser observe_region 不接受 surface_ref，请用 x/y/width/height")
            if any(name not in payload for name in ("x", "y", "width", "height")):
                raise _invalid("browser observe_region 必须同时提供 x/y/width/height")
            x, y = _required_point(payload)
            width = _strict_int(payload["width"], "width")
            height = _strict_int(payload["height"], "height")
            if width <= 0 or height <= 0:
                raise _invalid("observe_region 的 width/height 必须是正整数")
            if x < 0 or y < 0:
                raise _invalid("observe_region 的 x/y 不能为负")
            kwargs["x"], kwargs["y"] = x, y
            kwargs["width"], kwargs["height"] = width, height

    if kind == "sequence":
        steps_raw = payload.get("steps")
        if (
            not isinstance(steps_raw, (list, tuple))
            or not 1 <= len(steps_raw) <= MAX_SEQUENCE_STEPS
        ):
            raise _invalid(f"sequence 需要 1-{MAX_SEQUENCE_STEPS} 个 steps")
        steps = []
        for index, item in enumerate(steps_raw):
            if not isinstance(item, Mapping):
                raise _invalid(f"steps[{index}] 必须是动作对象")
            step_kind = item.get("kind")
            if step_kind not in _SEQUENCE_STEP_KINDS:
                raise _invalid(
                    f"steps[{index}] 的 kind {step_kind!r} 不允许在 sequence 内执行"
                    "（observe/语义定位/request_user_takeover/navigate 必须单独调用）"
                )
            if item.get("target") != target:
                raise _invalid(f"steps[{index}] 的 target 必须与本动作一致：{target!r}")
            steps.append(parse_computer_action(dict(item)))
        kwargs["steps"] = tuple(steps)

    if kind in ("click_element", "set_value_element"):
        ref = payload.get("element_ref")
        if not isinstance(ref, str) or not ref:
            raise _invalid(f"{kind} 需要非空 element_ref（来自最近一次 list_elements）")
        kwargs["element_ref"] = ref

    if kind == "wait_for":
        # 条件等待（Windows-MCP 对齐，desktop only）：condition 缺省 text_exists；
        # 每个条件只认自己的必填字段，不属于该条件的字段一律拒绝（不静默忽略），
        # timeout_ms / interval_ms 越界直接拒绝，不裁剪也不静默收敛。
        condition = payload.get("condition", WAIT_FOR_DEFAULT_CONDITION)
        if not isinstance(condition, str) or condition not in WAIT_FOR_CONDITIONS:
            raise _invalid(
                f"wait_for 的 condition 只支持 {list(WAIT_FOR_CONDITIONS)}，"
                f"收到 {condition!r}"
            )
        kwargs["condition"] = condition
        if condition == "text_exists":
            text = payload.get("text")
            if (
                not isinstance(text, str)
                or not 1 <= len(text) <= MAX_WAIT_FOR_NAME_CODEPOINTS
            ):
                raise _invalid(
                    "wait_for 的 text_exists 需要 text"
                    f"（1-{MAX_WAIT_FOR_NAME_CODEPOINTS} 个字符）"
                )
            kwargs["text"] = text
        elif "text" in payload:
            raise _invalid(
                f"wait_for 的 {condition} 不接受 text，只有 text_exists 使用 text"
            )
        if condition in ("element_exists", "element_enabled"):
            ref = payload.get("element_ref")
            if not isinstance(ref, str) or not ref:
                raise _invalid(
                    f"wait_for 的 {condition} 需要非空 element_ref"
                    "（来自最近一次 list_elements）"
                )
            kwargs["element_ref"] = ref
        elif "element_ref" in payload:
            raise _invalid(
                f"wait_for 的 {condition} 不接受 element_ref，"
                "只有 element_exists / element_enabled 使用 element_ref"
            )
        if "window_name" in payload:
            window_name = payload["window_name"]
            if (
                not isinstance(window_name, str)
                or not 1 <= len(window_name) <= MAX_WAIT_FOR_NAME_CODEPOINTS
            ):
                raise _invalid(
                    "wait_for 的 window_name 需要 1-"
                    f"{MAX_WAIT_FOR_NAME_CODEPOINTS} 个字符"
                )
            kwargs["window_name"] = window_name
        elif condition == "window_active":
            raise _invalid("wait_for 的 window_active 需要 window_name（窗口标题子串）")
        timeout_ms = _strict_int(
            payload.get("timeout_ms", WAIT_FOR_DEFAULT_TIMEOUT_MS), "timeout_ms"
        )
        if not MIN_WAIT_FOR_TIMEOUT_MS <= timeout_ms <= MAX_WAIT_FOR_TIMEOUT_MS:
            raise _invalid(
                f"wait_for 的 timeout_ms 必须在 {MIN_WAIT_FOR_TIMEOUT_MS}-"
                f"{MAX_WAIT_FOR_TIMEOUT_MS} 毫秒之间"
            )
        interval_ms = _strict_int(
            payload.get("interval_ms", WAIT_FOR_DEFAULT_INTERVAL_MS), "interval_ms"
        )
        if not MIN_WAIT_FOR_INTERVAL_MS <= interval_ms <= MAX_WAIT_FOR_INTERVAL_MS:
            raise _invalid(
                f"wait_for 的 interval_ms 必须在 {MIN_WAIT_FOR_INTERVAL_MS}-"
                f"{MAX_WAIT_FOR_INTERVAL_MS} 毫秒之间"
            )
        kwargs["timeout_ms"] = timeout_ms
        kwargs["interval_ms"] = interval_ms

    return ComputerAction(**kwargs)


def sanitized_action_summary(action: ComputerAction) -> dict:
    """统一动作 → 脱敏摘要（设计 22.4，Task 16 隐私审计）。

    输入正文/组合键/URL 只留 code point 数与 SHA-256；坐标、滚轮量、等待
    时长等非敏感参数原样保留。generic 工具参数投影与 native 持久化投影
    共用同一语义；投影是纯函数，同一动作两次构造完全一致。
    """
    summary: dict = {"kind": action.kind, "target": action.target}
    if action.x is not None:
        summary["x"] = action.x
    if action.y is not None:
        summary["y"] = action.y
    if action.width is not None:
        summary["width"] = action.width
    if action.height is not None:
        summary["height"] = action.height
    if action.path:
        summary["path_points"] = len(action.path)
    if action.button and action.button != "left":
        summary["button"] = action.button
    if action.delta_x or action.delta_y:
        summary["delta_x"] = action.delta_x
        summary["delta_y"] = action.delta_y
        if action.unit:
            summary["unit"] = action.unit
    if action.duration_ms is not None:
        summary["duration_ms"] = action.duration_ms
    if action.text is not None:
        summary["text_code_points"] = len(action.text)
        summary["text_sha256"] = hashlib.sha256(
            action.text.encode("utf-8")
        ).hexdigest()
    if action.keys:
        summary["keys_count"] = len(action.keys)
        summary["keys_sha256"] = hashlib.sha256(
            "+".join(action.keys).encode("utf-8")
        ).hexdigest()
    if action.url:
        summary["url_sha256"] = hashlib.sha256(
            action.url.encode("utf-8")
        ).hexdigest()
    if action.steps:
        summary["steps_count"] = len(action.steps)
        summary["steps"] = [sanitized_action_summary(step) for step in action.steps]
    if action.element_ref:
        summary["element_ref"] = action.element_ref
    return summary


def observation_provider_projection(observation: ComputerObservation) -> dict:
    """发给 Provider 的精简结果（设计 10.2）。

    只含 ok/target/action/screen_sha256/width/height/origin_or_window/
    error_code/error_message；绝不包含 png_path、时间戳、随机序号、
    完整 URL query 或输入正文，保证相同画面产生相同字节。

    R10：仅当观察携带语义定位候选（list_surfaces 结果）时附加
    surface_candidates（ref/kind/label/bounds/operations/代数摘要），
    普通观察投影字节不变。
    """
    if observation.target == "browser":
        origin_or_window = observation.page_origin
    else:
        origin_or_window = observation.window_title
    projection = {
        "ok": observation.ok,
        "target": observation.target,
        "action": observation.action,
        "screen_sha256": observation.screen_sha256,
        "width": observation.width,
        "height": observation.height,
        "origin_or_window": origin_or_window,
        "error_code": observation.error_code,
        "error_message": observation.error_message,
    }
    if observation.surface_candidates:
        projection["surface_candidates"] = [
            {
                "ref": str(candidate.get("ref", "")),
                "kind": str(candidate.get("kind", "")),
                "label": str(candidate.get("label", "")),
                "bounds": [int(v) for v in candidate.get("bounds", (0, 0, 0, 0))],
                "operations": [str(op) for op in candidate.get("operations", ())],
                "surface_generation": int(candidate.get("surface_generation", 0)),
            }
            for candidate in observation.surface_candidates
        ]
    return projection
