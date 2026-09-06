"""电脑操控严格领域模型与动作解析测试（Task 2）。

只覆盖 core.computer.models / core.computer.actions 的纯解析与值对象行为；
不触碰真实键鼠、浏览器、截图或网络。
"""

import json
from dataclasses import FrozenInstanceError

import pytest

from core.computer.actions import (
    COMPUTER_ACTION_TOOL_SCHEMA,
    observation_provider_projection,
    parse_computer_action,
    sanitized_action_summary,
)
from core.computer.models import (
    BrowserKind,
    ComputerAction,
    ComputerActionKind,
    ComputerBackend,
    ComputerCapability,
    ComputerControlError,
    ComputerObservation,
    ComputerOwner,
    ComputerProtocol,
    ComputerRunSnapshot,
    ComputerRunStatus,
    ComputerTarget,
)


# ---------------------------------------------------------------------------
# brief 样例测试（逐字）
# ---------------------------------------------------------------------------


def test_parse_click_requires_strict_integer_coordinates():
    action = parse_computer_action({"kind": "click", "target": "desktop", "x": 12, "y": 34})
    assert (action.kind, action.target, action.x, action.y) == ("click", "desktop", 12, 34)


def test_parse_drag_normalizes_start_end_to_path():
    action = parse_computer_action(
        {"kind": "drag", "target": "desktop", "x": 1, "y": 2, "end_x": 3, "end_y": 4}
    )
    assert action.path == ((1, 2), (3, 4))
    assert action.end_x is None and action.end_y is None


def test_parse_takeover_requires_stable_reason_code():
    action = parse_computer_action(
        {"kind": "request_user_takeover", "target": "browser", "reason_code": "login_required"}
    )
    assert action.reason_code == "login_required"


@pytest.mark.parametrize("payload", [
    {"kind": "click", "target": "desktop", "x": True, "y": 2},
    {"kind": "observe", "target": "browser", "x": 1},
    {"kind": "wait", "target": "desktop", "duration_ms": 99},
    {"kind": "keypress", "target": "desktop", "keys": ["CTRL"] * 9},
    {"kind": "navigate", "target": "desktop", "url": "https://example.com"},
    {"kind": "navigate", "target": "browser", "url": "file:///c:/secret"},
    {"kind": "click", "target": "desktop", "x": 1, "y": 2, "mystery": 3},
    {"kind": "drag", "target": "desktop", "path": [(1, 2)], "x": 1, "y": 2},
    {"kind": "drag", "target": "desktop"},
    {"kind": "request_user_takeover", "target": "browser", "reason_code": "just_because"},
])
def test_parse_action_rejects_invalid_payload(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


# ---------------------------------------------------------------------------
# 每种合法动作的参数化覆盖
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"kind": "observe", "target": "browser"},
    {"kind": "observe", "target": "desktop"},
    {"kind": "click", "target": "desktop", "x": 1, "y": 2},
    {"kind": "click", "target": "browser", "x": 1, "y": 2, "button": "right", "keys": ["CTRL"]},
    {"kind": "double_click", "target": "desktop", "x": 1, "y": 2, "button": "middle"},
    {"kind": "move", "target": "desktop", "x": 0, "y": 0},
    {"kind": "drag", "target": "desktop", "path": [(1, 2), (3, 4), (5, 6)]},
    {"kind": "drag", "target": "desktop", "x": 1, "y": 2, "end_x": 3, "end_y": 4,
     "button": "left", "keys": ["SHIFT"]},
    {"kind": "scroll", "target": "desktop", "x": 10, "y": 20, "delta_x": 0, "delta_y": -3},
    {"kind": "scroll", "target": "browser", "delta_y": 240},
    {"kind": "scroll", "target": "browser", "delta_y": -4000, "unit": "css_pixel"},
    {"kind": "scroll", "target": "desktop", "delta_y": 10, "unit": "wheel_step"},
    {"kind": "type_text", "target": "desktop", "text": "你好"},
    {"kind": "type_text", "target": "browser", "text": "hello"},
    {"kind": "keypress", "target": "desktop", "keys": ["CTRL", "A"]},
    {"kind": "wait", "target": "desktop", "duration_ms": 100},
    {"kind": "wait", "target": "browser", "duration_ms": 10000},
    {"kind": "navigate", "target": "browser", "url": "https://example.com/path?q=1#f"},
    {"kind": "navigate", "target": "browser", "url": "http://example.com"},
    {"kind": "back", "target": "browser"},
    {"kind": "forward", "target": "browser"},
    {"kind": "reload", "target": "browser"},
    {"kind": "request_user_takeover", "target": "browser", "reason_code": "captcha_required"},
    {"kind": "request_user_takeover", "target": "desktop", "reason_code": "credential_required"},
])
def test_parse_accepts_each_legal_action(payload):
    action = parse_computer_action(payload)
    assert action.kind == payload["kind"]
    assert action.target == payload["target"]


def test_parse_drag_accepts_explicit_path_and_keeps_tuple_shape():
    action = parse_computer_action(
        {"kind": "drag", "target": "desktop", "path": [[1, 2], [3, 4], [3, 4]]}
    )
    assert action.path == ((1, 2), (3, 4), (3, 4))
    assert all(type(point) is tuple for point in action.path)


# ---------------------------------------------------------------------------
# 未知字段 / 缺失字段 / 非法顶层形状
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"kind": "type_text", "target": "desktop", "text": "a", "extra": 1},
    {"kind": "keypress", "target": "desktop", "keys": ["A"], "x": 1},
    {"kind": "back", "target": "browser", "url": "https://example.com"},
    {"kind": "wait", "target": "desktop", "duration_ms": 100, "text": "a"},
    {"kind": "move", "target": "desktop", "x": 1, "y": 2, "button": "left"},
    {"kind": "scroll", "target": "desktop", "delta_y": 1, "keys": ["CTRL"]},
    {"kind": "request_user_takeover", "target": "browser", "reason_code": "login_required",
     "text": "密码正文"},
])
def test_parse_rejects_unknown_fields(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


@pytest.mark.parametrize("payload", [
    {"target": "desktop"},                       # 缺 kind
    {"kind": "click", "x": 1, "y": 2},           # 缺 target
    {"kind": "click", "target": "desktop", "x": 1},          # 缺 y
    {"kind": "move", "target": "desktop", "y": 2},           # 缺 x
    {"kind": "type_text", "target": "desktop"},              # 缺 text
    {"kind": "keypress", "target": "desktop"},               # 缺 keys
    {"kind": "wait", "target": "desktop"},                   # 缺 duration_ms
    {"kind": "navigate", "target": "browser"},               # 缺 url
    {"kind": "request_user_takeover", "target": "browser"},  # 缺 reason_code
    {"kind": "drag", "target": "desktop", "x": 1, "y": 2, "end_x": 3},  # 缺 end_y
])
def test_parse_rejects_missing_required_fields(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


@pytest.mark.parametrize("payload", [
    "click",
    [("kind", "click")],
    {"kind": "click", "target": "phone", "x": 1, "y": 2},
    {"kind": "teleport", "target": "desktop"},
    {"kind": 3, "target": "desktop"},
    {"kind": "observe", "target": ""},
])
def test_parse_rejects_invalid_envelope(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


# ---------------------------------------------------------------------------
# bool 冒充 int / NaN / 非整数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"kind": "click", "target": "desktop", "x": True, "y": 2},
    {"kind": "click", "target": "desktop", "x": 1, "y": False},
    {"kind": "click", "target": "desktop", "x": 1.0, "y": 2},
    {"kind": "click", "target": "desktop", "x": float("nan"), "y": 2},
    {"kind": "click", "target": "desktop", "x": "1", "y": 2},
    {"kind": "move", "target": "desktop", "x": True, "y": 2},
    {"kind": "scroll", "target": "desktop", "delta_y": True},
    {"kind": "scroll", "target": "desktop", "delta_x": 1.5},
    {"kind": "wait", "target": "desktop", "duration_ms": True},
    {"kind": "wait", "target": "desktop", "duration_ms": 100.0},
    {"kind": "drag", "target": "desktop", "path": [(True, 2), (3, 4)]},
    {"kind": "drag", "target": "desktop", "x": 1, "y": 2, "end_x": 3, "end_y": False},
])
def test_parse_rejects_bool_float_nan_and_str_as_int(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


# ---------------------------------------------------------------------------
# drag 字段互斥与路径边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"kind": "drag", "target": "desktop", "path": [(1, 2), (3, 4)], "end_x": 5, "end_y": 6},
    {"kind": "drag", "target": "desktop", "path": [(1, 2), (3, 4)], "x": 1, "y": 2,
     "end_x": 3, "end_y": 4},
    {"kind": "drag", "target": "desktop", "path": [], "x": 1, "y": 2, "end_x": 3, "end_y": 4},
    {"kind": "drag", "target": "desktop", "path": [(1, 2)]},
    {"kind": "drag", "target": "desktop", "path": [(i, i) for i in range(257)]},
    {"kind": "drag", "target": "desktop", "path": [(1, 2, 3), (4, 5, 6)]},
    {"kind": "drag", "target": "desktop", "path": [("1", 2), (3, 4)]},
    {"kind": "drag", "target": "desktop", "path": "not-a-path"},
    {"kind": "drag", "target": "desktop", "x": 1, "y": 2},
])
def test_parse_drag_rejects_invalid_path_and_mutual_exclusion(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


@pytest.mark.parametrize("size", [2, 3, 256])
def test_parse_drag_accepts_path_size_bounds(size):
    action = parse_computer_action(
        {"kind": "drag", "target": "desktop", "path": [(i, i + 1) for i in range(size)]}
    )
    assert len(action.path) == size


# ---------------------------------------------------------------------------
# text 8000 code point 边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["密" * 8000, "a"], ids=["exact-8000-cjk", "single-char"])
def test_parse_type_text_accepts_up_to_8000_code_points(text):
    action = parse_computer_action({"kind": "type_text", "target": "desktop", "text": text})
    assert action.text == text


@pytest.mark.parametrize(
    "text",
    ["密" * 8001, "", 123, None],
    ids=["8001-cjk-code-points", "empty", "int", "none"],
)
def test_parse_type_text_rejects_out_of_range_or_non_string(text):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action({"kind": "type_text", "target": "desktop", "text": text})
    assert raised.value.code == "computer_invalid_action"


# ---------------------------------------------------------------------------
# wait 100–10000 边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("duration_ms", [100, 5000, 10000])
def test_parse_wait_accepts_duration_bounds(duration_ms):
    action = parse_computer_action(
        {"kind": "wait", "target": "desktop", "duration_ms": duration_ms}
    )
    assert action.duration_ms == duration_ms


@pytest.mark.parametrize("duration_ms", [99, 10001, 0, -100])
def test_parse_wait_rejects_out_of_range_duration(duration_ms):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(
            {"kind": "wait", "target": "desktop", "duration_ms": duration_ms}
        )
    assert raised.value.code == "computer_invalid_action"


# ---------------------------------------------------------------------------
# keys 最多 8 个及白名单
# ---------------------------------------------------------------------------


def test_parse_keypress_accepts_at_most_eight_keys():
    action = parse_computer_action(
        {"kind": "keypress", "target": "desktop",
         "keys": ["CTRL", "ALT", "SHIFT", "META", "ENTER", "TAB", "ESC", "F12"]}
    )
    assert len(action.keys) == 8


@pytest.mark.parametrize("keys", [
    [],
    ["CTRL"] * 9,
    ["CTRL", "F13"],
    ["MOUSE1"],
    ["ctrl alt"],
    ["A", 1],
    ["CTRL", None],
])
def test_parse_keypress_rejects_bad_keys(keys):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action({"kind": "keypress", "target": "desktop", "keys": keys})
    assert raised.value.code == "computer_invalid_action"


@pytest.mark.parametrize("keys", [
    ["A"], ["Z"], ["0"], ["9"], ["F1"], ["F12"],
    ["BACKSPACE"], ["DELETE"], ["HOME"], ["END"],
    ["PAGEUP"], ["PAGEDOWN"], ["UP"], ["DOWN"], ["LEFT"], ["RIGHT"], ["SPACE"],
])
def test_parse_keypress_accepts_whitelisted_keys(keys):
    action = parse_computer_action({"kind": "keypress", "target": "desktop", "keys": keys})
    assert action.keys == tuple(keys)


@pytest.mark.parametrize("keys", [
    ["ctrl"],
    ["ctrl", "a"],
    ["Ctrl", "Shift", "A"],
    ["f1"],
], ids=["lower-ctrl", "lower-combo", "capitalized", "lower-f1"])
def test_parse_keypress_rejects_non_canonical_key_case(keys):
    """键名必须逐字匹配大写白名单，不做大小写归一（设计 21 禁止自动纠正）。"""
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action({"kind": "keypress", "target": "desktop", "keys": keys})
    assert raised.value.code == "computer_invalid_action"


# ---------------------------------------------------------------------------
# URL scheme 仅 http/https 且有 hostname；browser-only 导航
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://example.com",
    "http://example.com/path?x=1&y=2#frag",
    "HTTPS://EXAMPLE.COM/",
])
def test_parse_navigate_accepts_http_https_with_hostname(url):
    action = parse_computer_action({"kind": "navigate", "target": "browser", "url": url})
    assert action.url == url


@pytest.mark.parametrize("url", [
    "file:///c:/secret",
    "ftp://example.com/x",
    "javascript:alert(1)",
    "https://",
    "https:///no-host",
    "//example.com",
    "example.com",
    "",
    123,
    None,
    "http://[::1",
    "https://example.com:notaport/",
    "http://example.com:99999/",
], ids=[
    "file-scheme", "ftp-scheme", "javascript-scheme", "scheme-only", "no-host",
    "scheme-relative", "bare-host", "empty", "int", "none",
    "broken-ipv6-literal", "non-numeric-port", "out-of-range-port",
])
def test_parse_navigate_rejects_bad_url(url):
    """非法 IPv6 字面量与非法端口也必须归为 computer_invalid_action，不得泄漏 ValueError。"""
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action({"kind": "navigate", "target": "browser", "url": url})
    assert raised.value.code == "computer_invalid_action"


def test_parse_navigate_accepts_explicit_valid_port():
    action = parse_computer_action(
        {"kind": "navigate", "target": "browser", "url": "https://example.com:8443/path"}
    )
    assert action.url == "https://example.com:8443/path"


@pytest.mark.parametrize("kind", ["navigate", "back", "forward", "reload"])
def test_browser_only_navigation_rejects_desktop_target(kind):
    payload = {"kind": kind, "target": "desktop"}
    if kind == "navigate":
        payload["url"] = "https://example.com"
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


# ---------------------------------------------------------------------------
# request_user_takeover reason_code 白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason_code", [
    "login_required", "captcha_required", "credential_required",
])
def test_takeover_accepts_whitelisted_reason_codes(reason_code):
    action = parse_computer_action(
        {"kind": "request_user_takeover", "target": "browser", "reason_code": reason_code}
    )
    assert action.reason_code == reason_code


@pytest.mark.parametrize("reason_code", [
    "just_because", "", "LOGIN_REQUIRED", "password_is_123456", None, 7,
])
def test_takeover_rejects_unregistered_reason_codes(reason_code):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(
            {"kind": "request_user_takeover", "target": "browser", "reason_code": reason_code}
        )
    assert raised.value.code == "computer_invalid_action"


# ---------------------------------------------------------------------------
# button 白名单与鼠标 modifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"kind": "click", "target": "desktop", "x": 1, "y": 2, "button": ["left"]},
    {"kind": "click", "target": "desktop", "x": 1, "y": 2, "button": 1},
    {"kind": "click", "target": "desktop", "x": 1, "y": 2, "button": None},
    {"kind": "drag", "target": "desktop", "path": [(1, 2), (3, 4)], "button": ["left"]},
    {"kind": "request_user_takeover", "target": "browser", "reason_code": ["login_required"]},
    {"kind": "request_user_takeover", "target": "browser", "reason_code": 1},
    {"kind": "observe", "target": ["browser"]},
    {"kind": "observe", "target": 1},
    {"kind": ["click"], "target": "desktop", "x": 1, "y": 2},
], ids=[
    "button-list", "button-int", "button-none", "drag-button-list",
    "reason-list", "reason-int", "target-list", "target-int", "kind-list",
])
def test_parse_rejects_unhashable_or_non_string_enum_values(payload):
    """frozenset 成员判断遇到 list 等不可哈希值不得泄漏 TypeError。"""
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


@pytest.mark.parametrize("payload", [
    {"kind": "click", "target": "desktop", "x": 1, "y": 2, "button": "primary"},
    {"kind": "click", "target": "desktop", "x": 1, "y": 2, "button": ""},
    {"kind": "click", "target": "desktop", "x": 1, "y": 2, "keys": ["F13"]},
    {"kind": "double_click", "target": "desktop", "x": 1, "y": 2, "keys": ["CTRL"] * 9},
    {"kind": "drag", "target": "desktop", "path": [(1, 2), (3, 4)], "keys": ["BAD"]},
])
def test_mouse_actions_reject_bad_button_and_modifier(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


# ---------------------------------------------------------------------------
# 值对象：错误类型、不可变性、隐私 repr
# ---------------------------------------------------------------------------


def test_control_error_str_is_chinese_message_and_details_default_empty():
    error = ComputerControlError("computer_invalid_action", "动作字段不合法")
    assert str(error) == "动作字段不合法"
    assert error.code == "computer_invalid_action"
    assert error.message == "动作字段不合法"
    assert error.details == {}
    with_details = ComputerControlError("computer_session_busy", "会话被占用", {"a": 1})
    assert with_details.details == {"a": 1}


def test_value_objects_are_frozen():
    owner = ComputerOwner(session_id="s", turn_id="t", session_label="标签",
                          cancel_event=__import__("threading").Event())
    with pytest.raises(FrozenInstanceError):
        owner.session_id = "x"
    action = parse_computer_action({"kind": "click", "target": "desktop", "x": 1, "y": 2})
    with pytest.raises(FrozenInstanceError):
        action.x = 9
    assert isinstance(action, ComputerAction)


def test_action_repr_never_exposes_text():
    action = parse_computer_action(
        {"kind": "type_text", "target": "desktop", "text": "秘密正文-secret"}
    )
    assert "秘密正文-secret" not in repr(action)


def _sample_observation(**overrides):
    values = {
        "run_id": "run-1",
        "target": "browser",
        "action": "click",
        "ok": True,
        "screen_sha256": "abc123",
        "png_path": "D:\\shots\\secret_capture.png",
        "width": 1440,
        "height": 900,
        "page_origin": "https://example.com",
        "page_title": "Example",
    }
    values.update(overrides)
    return ComputerObservation(**values)


def test_observation_repr_never_exposes_png_path():
    observation = _sample_observation()
    assert "secret_capture.png" not in repr(observation)
    assert "D:\\shots" not in repr(observation)


def test_observation_provider_projection_is_minimal_and_deterministic():
    observation = _sample_observation()
    projection = observation_provider_projection(observation)
    assert projection == {
        "ok": True,
        "target": "browser",
        "action": "click",
        "screen_sha256": "abc123",
        "width": 1440,
        "height": 900,
        "origin_or_window": "https://example.com",
        "error_code": "",
        "error_message": "",
    }
    blob = json.dumps(projection, sort_keys=True)
    assert "secret_capture" not in blob
    assert "png_path" not in blob
    assert "path?x" not in blob  # 只含 origin，不含 query


def test_observation_provider_projection_uses_window_summary_for_desktop():
    observation = _sample_observation(
        target="desktop", page_origin="", page_title="",
        window_title="记事本", ok=False, error_code="computer_no_progress",
        error_message="画面连续无变化",
    )
    projection = observation_provider_projection(observation)
    assert projection["origin_or_window"] == "记事本"
    assert projection["ok"] is False
    assert projection["error_code"] == "computer_no_progress"


def test_run_snapshot_public_dict_has_no_sensitive_fields():
    snapshot = ComputerRunSnapshot(
        run_id="run-1", owner_session_id="s", owner_turn_id="t",
        status="running", target="browser", browser="edge",
        model_name="k3", protocol="generic", started_at=123.0,
        action_count=2, current_action="click",
        current_surface="https://example.com", preview_url="http://127.0.0.1/x",
        pause_reason="", stop_reason="", error_code="", hotkey_available=True,
    )
    public = snapshot.to_public_dict()
    assert public["run_id"] == "run-1"
    assert public["status"] == "running"
    for forbidden in ("text", "png", "png_path", "api_key", "cookie"):
        assert forbidden not in public
    assert json.dumps(public)  # 可 JSON 序列化


def test_capability_and_enums_have_design_values():
    assert {member.value for member in ComputerProtocol} == {"none", "generic", "native_openai"}
    assert {member.value for member in ComputerTarget} == {"browser", "desktop"}
    assert {member.value for member in BrowserKind} == {"edge", "chrome"}
    assert {member.value for member in ComputerRunStatus} == {
        "idle", "starting", "running", "paused", "waiting_for_user",
        "provider_safety_required", "completing", "completed",
        "stopping", "stopped", "failed",
    }
    assert {member.value for member in ComputerActionKind} == {
        "observe", "click", "double_click", "move", "drag", "scroll",
        "type_text", "keypress", "wait", "navigate", "back", "forward", "reload",
        "request_user_takeover",
        # R10 协议扩展（2026-09-05）：desktop 语义定位与局部观察。
        "list_surfaces", "activate_surface", "invoke_surface", "observe_region",
        # 2026-09-06 协议扩展：sequence 批量微动作（提速计划 Task 1）。
        "sequence",
        # 2026-09-06 协议扩展：UIA 元素观察与元素引用动作（提速计划 Task 10）。
        "list_elements", "click_element", "set_value_element",
    }
    capability = ComputerCapability(
        enabled=True, protocol="generic", targets=("browser", "desktop"),
        browser="edge", settings_generation=1,
    )
    assert capability.error_code == "" and capability.reason == ""


def test_computer_backend_is_runtime_checkable_protocol():
    class _FakeBackend:
        target = "desktop"

        def start(self):
            return None

        def observe(self, run_id):
            return _sample_observation(target="desktop")

        def execute(self, run_id, action):
            return None

        def release_inputs(self):
            return None

        def close(self):
            return None

        def is_reusable(self):
            return False

    assert isinstance(_FakeBackend(), ComputerBackend)


def test_computer_action_tool_schema_shape():
    schema = COMPUTER_ACTION_TOOL_SCHEMA
    assert schema["type"] == "function"
    function = schema["function"]
    assert function["name"] == "computer_action"
    parameters = function["parameters"]
    assert parameters["type"] == "object"
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["kind", "target"]
    kinds = parameters["properties"]["kind"]["enum"]
    assert set(kinds) == {member.value for member in ComputerActionKind}
    assert parameters["properties"]["target"]["enum"] == ["browser", "desktop"]
    assert parameters["properties"]["keys"]["maxItems"] == 8
    assert parameters["properties"]["duration_ms"]["minimum"] == 100
    assert parameters["properties"]["duration_ms"]["maximum"] == 10000
    assert parameters["properties"]["path"]["minItems"] == 2
    assert parameters["properties"]["path"]["maxItems"] == 256
    reason_enum = parameters["properties"]["reason_code"]["enum"]
    assert set(reason_enum) == {"login_required", "captcha_required", "credential_required"}
    description = function["description"]
    assert "完成任务后输出正常最终结论，不继续无意义观察" in description


# ---------------------------------------------------------------------------
# R7：滚动单位/方向契约（css_pixel vs wheel_step，正方向向右/向下）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"kind": "scroll", "target": "browser", "delta_x": 4001},                 # 超像素上限
    {"kind": "scroll", "target": "browser", "delta_y": -4001},
    {"kind": "scroll", "target": "desktop", "delta_y": 11},                   # 超步进上限
    {"kind": "scroll", "target": "desktop", "delta_x": -11},
    {"kind": "scroll", "target": "browser"},                                  # 两轴为零
    {"kind": "scroll", "target": "desktop", "delta_x": 0, "delta_y": 0},
    {"kind": "scroll", "target": "browser", "delta_y": 100, "unit": "wheel_step"},  # 单位与 target 不匹配
    {"kind": "scroll", "target": "desktop", "delta_y": 1, "unit": "css_pixel"},
    {"kind": "scroll", "target": "browser", "delta_y": 100, "unit": "pixel"},        # 未知单位
    {"kind": "scroll", "target": "browser", "delta_y": 100, "unit": True},
    {"kind": "scroll", "target": "desktop", "delta_y": -240},                 # 旧像素值在桌面超限
])
def test_parse_rejects_scroll_unit_range_and_zero_violations(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


def test_scroll_unit_defaults_follow_target():
    browser = parse_computer_action({"kind": "scroll", "target": "browser", "delta_y": 500})
    desktop = parse_computer_action({"kind": "scroll", "target": "desktop", "delta_y": -2})
    assert browser.unit == "css_pixel"
    assert desktop.unit == "wheel_step"


def test_scroll_schema_declares_unit_enum():
    properties = COMPUTER_ACTION_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert properties["unit"]["enum"] == ["css_pixel", "wheel_step"]
    assert "scroll" in COMPUTER_ACTION_TOOL_SCHEMA["function"]["parameters"]["properties"]["kind"]["enum"]


def test_sanitized_summary_includes_scroll_unit():
    action = parse_computer_action({"kind": "scroll", "target": "browser", "delta_y": 120})
    assert sanitized_action_summary(action)["unit"] == "css_pixel"
