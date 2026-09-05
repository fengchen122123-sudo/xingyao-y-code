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
    "scroll": _COMMON | {"x", "y", "delta_x", "delta_y"},
    "type_text": _COMMON | {"text"},
    "keypress": _COMMON | {"keys"},
    "wait": _COMMON | {"duration_ms"},
    "navigate": _COMMON | {"url"},
    "back": _COMMON,
    "forward": _COMMON,
    "reload": _COMMON,
    "request_user_takeover": _COMMON | {"reason_code"},
}

_TARGETS = ("browser", "desktop")
_BROWSER_ONLY_KINDS = frozenset({"navigate", "back", "forward", "reload"})
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

    kwargs: dict = {"kind": kind, "target": target}

    if kind in ("click", "double_click", "move"):
        kwargs["x"], kwargs["y"] = _required_point(payload)
    elif kind == "scroll":
        kwargs["x"], kwargs["y"] = _optional_point(payload)
        if "delta_x" in payload:
            kwargs["delta_x"] = _strict_int(payload["delta_x"], "delta_x")
        if "delta_y" in payload:
            kwargs["delta_y"] = _strict_int(payload["delta_y"], "delta_y")

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

    if kind == "type_text":
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
    if action.path:
        summary["path_points"] = len(action.path)
    if action.button and action.button != "left":
        summary["button"] = action.button
    if action.delta_x or action.delta_y:
        summary["delta_x"] = action.delta_x
        summary["delta_y"] = action.delta_y
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
    return summary


def observation_provider_projection(observation: ComputerObservation) -> dict:
    """发给 Provider 的精简结果（设计 10.2）。

    只含 ok/target/action/screen_sha256/width/height/origin_or_window/
    error_code/error_message；绝不包含 png_path、时间戳、随机序号、
    完整 URL query 或输入正文，保证相同画面产生相同字节。
    """
    if observation.target == "browser":
        origin_or_window = observation.page_origin
    else:
        origin_or_window = observation.window_title
    return {
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
