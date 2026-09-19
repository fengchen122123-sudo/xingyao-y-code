"""OpenAI Responses 原生 Computer Use 协议（设计 11，Task 11a）。

官方 GA 事实（2026-09-03 复核 developers.openai.com Computer use 指南与
Responses API 参考）：tools: [{"type":"computer"}]、computer_call.actions[]、
computer_call_output、previous_response_id；computer-use-preview 与单个
``action`` 字段是旧 preview 集成，本项目不为新实现采用或自动回退。
pending_safety_checks 条目形状为 {id, code, message}。

本模块是纯解析/构造层：不执行鼠标键盘（动作执行经 controller 窄接口）、
不读全局 config、不导入 GUI/CLI/Playwright。原生解析失败一律
computer_provider_protocol_error，绝不改走 generic 协议。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from core.computer.actions import parse_computer_action, sanitized_action_summary
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerObservation,
    ComputerProtocol,
)
from core.computer.tool_adapter import COMPUTER_SELECT_TARGET_TOOL_NAME
from core.tool_protocol import ToolResult

# 官方 GA 精确 built-in 定义：只允许经 capability 分支直接传入 Responses 请求，
# _responses_tools_from_chat_tools 仍拒绝任意 built-in（设计 11.1）。
OPENAI_COMPUTER_TOOL_DEFINITION = {"type": "computer"}

# 双 target 时的固定 selector function tool schema（Task 11a brief Step 3 逐字）。
COMPUTER_SELECT_TARGET_TOOL_SCHEMA = {
    "type": "function",
    "name": "computer_select_target",
    "description": "为本次电脑操控选择唯一目标；选择后不可更改。",
    "parameters": {
        "type": "object",
        "properties": {"target": {"type": "string", "enum": ["browser", "desktop"]}},
        "required": ["target"],
        "additionalProperties": False,
    },
}

# 官方 GA wait 动作不携带时长字段（参考手册 Wait object 只有 type），
# 映射到统一动作时使用固定 2 秒（官方指南示例的默认等待）。
NATIVE_WAIT_DEFAULT_MS = 2000

_PROVIDER_JSON_KWARGS = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
}

_PROTOCOL_ERROR = "computer_provider_protocol_error"

# 官方 GA 动作字段白名单（Responses API 参考 ComputerActionList 各 object）。
_OFFICIAL_ACTION_FIELDS = {
    "click": frozenset({"type", "x", "y", "button", "keys"}),
    "double_click": frozenset({"type", "x", "y", "keys"}),
    "drag": frozenset({"type", "path", "keys"}),
    "keypress": frozenset({"type", "keys"}),
    "move": frozenset({"type", "x", "y", "keys"}),
    "screenshot": frozenset({"type"}),
    "scroll": frozenset({"type", "x", "y", "scroll_x", "scroll_y", "keys"}),
    "type": frozenset({"type", "text"}),
    "wait": frozenset({"type"}),
}

# 官方键名别名归一化到统一动作键名（官方指南 normalize_key 表的子集）。
_KEY_ALIASES = {
    "RETURN": "ENTER",
    "ESCAPE": "ESC",
    "DEL": "DELETE",
    "CONTROL": "CTRL",
    "OPTION": "ALT",
    "CMD": "META",
    "COMMAND": "META",
    "ARROWUP": "UP",
    "ARROWDOWN": "DOWN",
    "ARROWLEFT": "LEFT",
    "ARROWRIGHT": "RIGHT",
}

# 官方鼠标按键 → 统一 button；back/forward 无统一等价，原样交给统一解析拒绝。
_BUTTON_ALIASES = {"left": "left", "right": "right", "wheel": "middle"}


# ---------------- 值对象 ----------------


@dataclass(frozen=True)
class SafetyCheck:
    """Provider 强制安全检查（设计 11.5）：只保存稳定 id/code 与说明文案。

    id/code 可进 Journal，message 原文不得持久化到 Journal；无已验证确认
    契约时绝不构造 acknowledged payload。
    """

    id: str
    message: str = ""
    code: str = ""


@dataclass(frozen=True)
class NativeComputerCall:
    """一个原生 computer_call 的严格解析结果（response id + call id + 有序统一动作）。"""

    response_id: str
    call_id: str
    actions: tuple[ComputerAction, ...]
    pending_safety_checks: tuple[SafetyCheck, ...] = ()


@dataclass(frozen=True)
class NativeFinalText:
    """原生分支以最终文本结束（无 computer call）。"""

    text: str = ""


@dataclass(frozen=True)
class NativeCallResult:
    """串行执行一批原生动作的结果；observation 为最后一个动作的本地观察。"""

    status: str
    observation: ComputerObservation | Any = None
    executed_actions: int = 0


# ---------------- 内部工具 ----------------


def _protocol_error(message: str, **details) -> ComputerControlError:
    return ComputerControlError(_PROTOCOL_ERROR, message, details=details or None)


def _field(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _normalized_type(event_type) -> str:
    """事件 type 归一化为 'response.*' 字符串（桥接枚举走 .value）。"""
    if isinstance(event_type, str):
        return event_type
    value = getattr(event_type, "value", None)
    if isinstance(value, str):
        return value
    return str(event_type or "")


def _is_native_enabled(capability) -> bool:
    return bool(
        capability is not None
        and capability.enabled
        and capability.protocol == ComputerProtocol.NATIVE_OPENAI.value
    )


def _merge_response_id(current: str, seen) -> str:
    seen = str(seen or "")
    if not seen:
        return current
    if current and current != seen:
        # 11b 用户裁决（Task 16 隐私审计）：冲突错误消息绝不内嵌任一
        # response id 原文——该错误可经 turn_error 外泄到 UI/持久化面。
        raise _protocol_error("Responses response id 冲突（两个不同的 id，原文不回显）")
    return seen


def _map_keys(value):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise _protocol_error("官方动作的 keys 必须是键名数组")
    mapped = []
    for key in value:
        if not isinstance(key, str):
            raise _protocol_error(
                f"官方动作的键名必须是字符串，收到 {type(key).__name__}"
            )
        mapped.append(_KEY_ALIASES.get(key, key))
    return mapped


def _map_button(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise _protocol_error("官方动作的 button 必须是字符串")
    return _BUTTON_ALIASES.get(value, value)


def _map_drag_path(value):
    if not isinstance(value, (list, tuple)):
        raise _protocol_error("官方 drag 的 path 必须是坐标点数组")
    points = []
    for point in value:
        if isinstance(point, dict):
            points.append([point.get("x"), point.get("y")])
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            points.append([point[0], point[1]])
        else:
            raise _protocol_error("官方 drag 的 path 点必须是 {x,y} 对象或坐标对")
    return points


def _map_official_action(action, target: str) -> ComputerAction:
    """官方 GA computer action → 统一 ComputerAction。

    映射到统一动作后再次经 parse_computer_action 严格校验坐标/字段
    （设计 11.2）：官方 screenshot 映射 observe，官方 drag 的 path/keys
    直接映射统一动作的 path/keys；move/scroll 携带 keys 等统一 schema
    不允许的组合由统一解析以 computer_invalid_action 拒绝。
    """
    if not isinstance(action, dict):
        raise _protocol_error("官方 computer action 必须是对象")
    action_type = action.get("type")
    if not isinstance(action_type, str) or action_type not in _OFFICIAL_ACTION_FIELDS:
        raise _protocol_error(f"未知官方电脑动作类型: {action_type!r}")
    unknown = set(action) - _OFFICIAL_ACTION_FIELDS[action_type]
    if unknown:
        raise _protocol_error(f"官方动作 {action_type} 不允许字段: {sorted(unknown)}")

    if action_type == "screenshot":
        payload = {"kind": "observe", "target": target}
    elif action_type == "wait":
        payload = {"kind": "wait", "target": target, "duration_ms": NATIVE_WAIT_DEFAULT_MS}
    elif action_type == "type":
        payload = {"kind": "type_text", "target": target, "text": action.get("text")}
    elif action_type == "keypress":
        payload = {"kind": "keypress", "target": target, "keys": _map_keys(action.get("keys"))}
    elif action_type == "drag":
        payload = {
            "kind": "drag",
            "target": target,
            "path": _map_drag_path(action.get("path")),
        }
        keys = _map_keys(action.get("keys"))
        if keys is not None:
            payload["keys"] = keys
    elif action_type == "scroll":
        if target == "desktop":
            # R7：官方原生滚动为像素语义（参考集成直接喂 mouse.wheel）；
            # 桌面实现只能发滚轮步进，缺少可验证的像素滚动实现前明确
            # 拒绝该 target 的原生滚动——整批零执行，绝不静默近似换算。
            raise _protocol_error(
                "官方原生滚动为像素语义，桌面 target 暂不支持像素滚动"
                "（桌面仅支持显式 wheel_step 单位）；本批动作零执行"
            )
        payload = {
            "kind": "scroll",
            "target": target,
            "x": action.get("x"),
            "y": action.get("y"),
            "delta_x": action.get("scroll_x"),
            "delta_y": action.get("scroll_y"),
            "unit": "css_pixel",
        }
        keys = _map_keys(action.get("keys"))
        if keys is not None:
            payload["keys"] = keys
    else:  # click / double_click / move
        payload = {"kind": action_type, "target": target, "x": action.get("x"), "y": action.get("y")}
        button = _map_button(action.get("button"))
        if button is not None:
            payload["button"] = button
        keys = _map_keys(action.get("keys"))
        if keys is not None:
            payload["keys"] = keys
    return parse_computer_action(payload)


def _parse_safety_check(check) -> SafetyCheck:
    check_id = _field(check, "id")
    if not isinstance(check_id, str) or not check_id:
        raise _protocol_error("pending_safety_checks 条目缺少 id")
    message = _field(check, "message") or ""
    code = _field(check, "code") or ""
    if not isinstance(message, str) or not isinstance(code, str):
        raise _protocol_error("pending_safety_checks 条目字段类型非法")
    return SafetyCheck(id=check_id, message=message, code=code)


def _dedupe_computer_items(items):
    """合并同一 computer_call 的重复终态载荷（done 与 completed 重复携带）。"""
    by_call: dict[str, Any] = {}
    ordered = []
    for item in items:
        call_id = _field(item, "call_id")
        if isinstance(call_id, str) and call_id:
            if call_id in by_call:
                if by_call[call_id] != item:
                    raise _protocol_error(
                        f"computer_call {call_id!r} 的重复载荷内容冲突"
                    )
                continue
            by_call[call_id] = item
        ordered.append(item)
    return ordered


def _native_call_from_item(response_id, item, target) -> NativeComputerCall:
    """computer_call item → NativeComputerCall（parse 与流式载荷共用的严格校验）。"""
    call_id = _field(item, "call_id")
    if not isinstance(call_id, str) or not call_id:
        raise _protocol_error("computer_call 缺少 call id")
    if _field(item, "action") is not None:
        raise _protocol_error(
            "旧 preview 单个 action 字段不被支持，必须使用 GA actions[]"
        )
    actions_raw = _field(item, "actions")
    if not isinstance(actions_raw, (list, tuple)) or not actions_raw:
        raise _protocol_error("computer_call 缺少非空 actions[]")
    status = _field(item, "status")
    if status is not None and status != "completed":
        raise _protocol_error(f"computer_call 状态非 completed: {status!r}")
    if not isinstance(response_id, str) or not response_id:
        raise _protocol_error("computer_call 缺少 response id")
    if target not in ("browser", "desktop"):
        raise ComputerControlError(
            "computer_invalid_state",
            "原生 computer call 需要先经 computer_select_target 选择目标"
            "（或单 target 能力），未选择前不得执行",
        )
    actions = tuple(_map_official_action(action, target) for action in actions_raw)
    safety_raw = _field(item, "pending_safety_checks") or ()
    checks = tuple(_parse_safety_check(check) for check in safety_raw)
    return NativeComputerCall(response_id, call_id, actions, checks)


# ---------------- 公共 API ----------------


def native_has_dual_targets(capability) -> bool:
    """原生能力是否同时启用两个 target（此时首请求固定 parallel_tool_calls=false）。"""
    return _is_native_enabled(capability) and len(tuple(capability.targets or ())) > 1


def build_openai_computer_tools(capability, target_selected) -> tuple[dict, ...]:
    """构造 Responses 请求的原生 tools 尾部（设计 11.1）。

    - 双 target 且尚未选择：依次返回 computer_select_target 与精确 computer
      built-in（selector 必须单独完成一个 Step）；
    - 单 target 或已选择：只返回 computer built-in；
    - capability 关闭/非原生：返回空 tuple（请求字节不受影响）。
    返回深拷贝，绝不暴露可变模块常量。
    """
    if not _is_native_enabled(capability):
        return ()
    targets = tuple(capability.targets or ())
    if target_selected is not None and target_selected not in targets:
        raise ComputerControlError(
            "computer_disabled", f"操控目标 {target_selected!r} 未在设置中开启"
        )
    tools: list[dict] = []
    if len(targets) > 1 and target_selected is None:
        tools.append(copy.deepcopy(COMPUTER_SELECT_TARGET_TOOL_SCHEMA))
    tools.append(copy.deepcopy(OPENAI_COMPUTER_TOOL_DEFINITION))
    return tuple(tools)


def parse_openai_computer_events(events, target=None) -> NativeComputerCall | NativeFinalText:
    """严格解析一个 Provider response 的原生事件序列（设计 11.2）。

    target 是本 Turn 冻结的操控目标（selector 租约或单 target 能力），
    用于把官方动作映射为统一 ComputerAction；未选择时出现 computer_call
    以 computer_invalid_state 拒绝。缺少 response id/call id/非空 actions[]、
    selector 与 computer_call 同 response、未知动作或字段冲突一律
    computer_provider_protocol_error；completed/incomplete/failed 三终态显式
    区分，后两者同样以协议错误失败，绝不进入 generic 分支。
    """
    response_id = ""
    selector_seen = False
    computer_items = []
    text_parts = []
    terminal = None
    terminal_detail = ""
    for event in events or ():
        event_type = _normalized_type(_field(event, "type", ""))
        if event_type == "response.created":
            response_id = _merge_response_id(
                response_id, _field(_field(event, "response"), "id", "")
            )
        elif event_type == "response.output_item.done":
            item = _field(event, "item")
            item_type = _field(item, "type")
            if item_type == "computer_call":
                computer_items.append(item)
            elif (
                item_type == "function_call"
                and _field(item, "name") == COMPUTER_SELECT_TARGET_TOOL_NAME
            ):
                selector_seen = True
        elif event_type == "response.completed":
            response = _field(event, "response")
            response_id = _merge_response_id(response_id, _field(response, "id", ""))
            for output_item in (_field(response, "output") or ()):
                if _field(output_item, "type") == "computer_call":
                    computer_items.append(output_item)
                elif (
                    _field(output_item, "type") == "function_call"
                    and _field(output_item, "name") == COMPUTER_SELECT_TARGET_TOOL_NAME
                ):
                    selector_seen = True
            terminal = "completed"
        elif event_type == "response.incomplete":
            response = _field(event, "response")
            response_id = _merge_response_id(response_id, _field(response, "id", ""))
            details = _field(response, "incomplete_details")
            terminal_detail = str(_field(details, "reason", "") or "")
            terminal = "incomplete"
        elif event_type in ("response.failed", "error"):
            response = _field(event, "response", event)
            error = _field(response, "error", response)
            terminal_detail = str(_field(error, "message") or error)
            terminal = "failed"
        elif event_type == "response.output_text.delta":
            text_parts.append(str(_field(event, "delta", "") or ""))
        # 其余事件（added/各 delta/忽略类）与现有 Responses 桥一致忽略；
        # output_item.added 的半截 computer_call 绝不进入 computer_items。

    if selector_seen and computer_items:
        raise _protocol_error(
            "同一个 Provider response 同时出现 computer_select_target 与 "
            "computer_call，selector 必须单独完成一个 Step，绝不按 output "
            "数组顺序猜测",
            selector_and_computer_call=True,
        )
    if terminal == "failed":
        raise _protocol_error(
            f"Responses 响应失败(failed)：{terminal_detail or '未知错误'}",
            terminal_state="failed",
        )
    if terminal == "incomplete":
        raise _protocol_error(
            f"Responses 响应未完成(incomplete)：{terminal_detail or '未知原因'}",
            terminal_state="incomplete",
        )

    unique_items = _dedupe_computer_items(computer_items)
    if unique_items:
        if len(unique_items) > 1:
            raise _protocol_error(
                "同一个 Provider response 出现多个不同 computer_call，无法确定执行批次"
            )
        call = _native_call_from_item(response_id, unique_items[0], target)
        if terminal != "completed":
            raise _protocol_error("computer_call 缺少 completed 终态事件")
        return call
    if terminal != "completed":
        raise _protocol_error("Responses 事件缺少 completed/incomplete/failed 终态事件")
    return NativeFinalText(text="".join(text_parts))


def native_computer_call_from_stream(payload, target=None) -> NativeComputerCall:
    """从 assembler 合并的终态 computer_call 流载荷构造 NativeComputerCall。

    payload 形如 {"response_id": ..., "item": {...}}（StreamAssembler 在
    output_item.done / response.completed 终态后才暴露）；半截 JSON 永远
    到不了这里。校验与 parse_openai_computer_events 共用同一严格路径。
    """
    if not isinstance(payload, dict):
        raise _protocol_error("computer_call 流载荷必须是对象")
    item = payload.get("item")
    if item is None:
        raise _protocol_error("computer_call 流载荷缺少 item")
    return _native_call_from_item(payload.get("response_id"), item, target)


def execute_native_computer_call(call, controller) -> NativeCallResult:
    """按原序串行执行一批原生动作（设计 11.2，GA actions[] 例外规则）。

    controller 是本函数的窄执行协议，不是 ComputerControlService 本体：
    - execute(action)：必须在单动作内完成 cancel/pause/owner/generation
      检查、backend 执行、本地 fresh after observation 与无进展判断；
    - enter_provider_safety_required()：pending safety 时被调用。
    真实服务签名是 execute(run_id, owner, action) /
    enter_provider_safety_required(run_id)（core/computer/controller.py），
    Task 11b 接线时必须由调用方构造绑定 run_id/owner 的适配器传入，
    不得把真实服务直接传给本函数（审查修复轮 I-2 契约声明）。
    任一动作失败或被停止即向上传播领域错误，余项绝不执行。整批成功后
    observation 为最后一个动作的本地观察（"只回传最后一图"不等于
    "中间动作不观察"）。pending safety checks 非空时零执行，controller
    进入 provider_safety_required（设计 11.5：无已验证确认契约，绝不
    伪造 acknowledged safety checks，也不回退 generic 绕过）。
    """
    if call.pending_safety_checks:
        controller.enter_provider_safety_required()
        return NativeCallResult(status="provider_safety_required")
    observation = None
    executed = 0
    for action in call.actions:
        observation = controller.execute(action)
        executed += 1
    return NativeCallResult(
        status="ok", observation=observation, executed_actions=executed
    )


# ---------------- Task 11b：原生 continuation 与脱敏持久化投影 ----------------


# 续传请求必须重发的请求级 options（不含 model/input/tools/instructions/
# previous_response_id/stream/timeout——前五者由结构化字段承载，timeout 由
# prepare_native_continuation_request 按当前 Step 注入）。
NATIVE_CONTINUATION_OPTION_KEYS = (
    "api_key",
    "api_base",
    "max_output_tokens",
    "tool_choice",
    "num_retries",
    "extra_body",
    "parallel_tool_calls",
    "reasoning",
)


@dataclass(frozen=True)
class NativeContinuationState:
    """原生续传的 Turn 内存状态（设计 11.3，brief Step 3）。

    只存活于当前模型回合生成器的局部变量：model/tools/instructions/options
    取自产生 computer_call 的那个 Step 的真实 Provider 请求 kwargs（字节级
    同序重发），response_id 取自该 computer_call。previous_response_id 的
    生命周期到此对象为止——绝不写入 message、Journal、event 或 runtime log。
    """

    model: str
    tools: tuple
    instructions: str
    options: dict
    response_id: str = ""


def build_continuation_request(turn_state, call_output) -> dict:
    """构造原生续传 Step 的 Provider 请求 kwargs（设计 11.3 第 4 步）。

    重发同一 model、同序 tools（含 computer built-in；selector 已选后本就不
    在产生 computer_call 的 Step 请求里）、必要 instructions/options 与上一
    response id；input 只有一个 computer_call_output，绝不重发完整消息历史
    （产物无 "messages" 键）。官方 Responses API 明确上一响应的 instructions
    不会经 previous_response_id 自动继承，漏发会导致 system prompt 语义漂移，
    因此 instructions 必须显式重发。Provider 未返回可用 response id 时立即以
    computer_provider_protocol_error 失败；禁止退回"重发全部截图历史"或
    通用 function tool。键序固定，供 byte-golden 精确断言。
    """
    response_id = str(getattr(turn_state, "response_id", "") or "")
    if not response_id:
        raise _protocol_error("Provider 未返回可用 response id，无法构造原生续传请求")
    request = {
        "model": turn_state.model,
        "api_key": turn_state.options.get("api_key"),
        "input": [call_output],
        "tools": [copy.deepcopy(tool) for tool in turn_state.tools],
        "stream": True,
        "previous_response_id": response_id,
        "instructions": turn_state.instructions,
    }
    for key in NATIVE_CONTINUATION_OPTION_KEYS:
        if key != "api_key" and key in turn_state.options:
            request[key] = copy.deepcopy(turn_state.options[key])
    return request


def _read_png_bytes(observation) -> bytes:
    png_path = str(getattr(observation, "png_path", "") or "")
    if not png_path:
        raise ComputerControlError("computer_backend_error", "观察缺少截图文件")
    try:
        with open(png_path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise ComputerControlError(
            "computer_backend_error", f"读取最新观察截图失败：{error}"
        ) from error


def build_computer_call_output(call, observation) -> dict:
    """整批动作成功后的 computer_call_output 输入条目（设计 11.3 第 3-4 步）。

    只携带整批动作最后一张截图（"只回传最后一图"）；data URL 只存在于本
    条目并发给 Provider，绝不持久化。不含任何确认字段（设计 11.5：GA
    Computer use 请求 schema 未定义 acknowledged_safety_checks 契约前绝不
    产出——pending safety 时 execute_native_computer_call 已先行返回
    provider_safety_required，本函数只服务已执行成功的批次）。
    """
    png_bytes = _read_png_bytes(observation)
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return {
        "type": "computer_call_output",
        "call_id": call.call_id,
        "output": {"type": "computer_screenshot", "image_url": data_url},
    }


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _sanitized_native_action(action) -> dict:
    """统一动作 → 脱敏摘要：输入正文/组合键/URL 只留 code point 数与 SHA-256。

    坐标、滚轮量、等待时长等非敏感参数原样保留；截图相关信息（screen
    hash/尺寸）由 build_native_call_persistence_row 附在最后一个动作上。
    Task 16 起与 generic 工具参数投影共用 actions.sanitized_action_summary。
    """
    return sanitized_action_summary(action)


def build_native_call_persistence_row(run_id, call, observation) -> dict:
    """原生中间行的脱敏持久化投影（设计 11.4，brief Step 5 逐字形状）。

    输入文字只保存 code point 数和 SHA-256；截图只保存 screen hash/尺寸
    （附在整批最后一个动作的摘要上——该动作的 post observation 即回传
    Provider 的那张截图）；不保存 base64、profile path、完整 URL、response
    id。投影是确定性的：同一 call 与 observation 两次构造完全一致。
    """
    summaries = [_sanitized_native_action(action) for action in call.actions]
    if observation is not None and summaries:
        summaries[-1]["screen_sha256"] = str(
            getattr(observation, "screen_sha256", "") or ""
        )
        summaries[-1]["screen_width"] = int(getattr(observation, "width", 0) or 0)
        summaries[-1]["screen_height"] = int(getattr(observation, "height", 0) or 0)
    return {
        "role": "assistant",
        "content": "",
        "_computer_v1": {
            "kind": "native_call",
            "run_id": str(run_id),
            "call_id_hash": _sha256_hex(call.call_id),
            "action": summaries,
        },
    }


class BoundNativeController:
    """把真实 ComputerControlService 绑定 run_id/owner 的窄执行协议适配器。

    execute_native_computer_call 只认 execute(action) /
    enter_provider_safety_required()（11a 窄协议）；真实服务签名是
    execute(run_id, owner, action) / enter_provider_safety_required(run_id)
    （审查修复轮 I-2 契约）。调用方（core/llm.py 原生分支）必须经本适配器
    绑定后传入，不得把真实服务直接交给协议层。
    """

    def __init__(self, controller, run_id, owner) -> None:
        self._controller = controller
        self._run_id = str(run_id)
        self._owner = owner

    def execute(self, action):
        return self._controller.execute(self._run_id, self._owner, action)

    def enter_provider_safety_required(self):
        return self._controller.enter_provider_safety_required(self._run_id)


# ---------------- target selector 固定适配（Task 9 保留名机制） ----------------


class NativeTargetSelection:
    """Turn 级原生 target 选择状态：selector adapter 写入，请求构造读取。

    由 chat_with_model_events 在双 target 原生回合创建并注入
    ToolExecutionContext.computer_native_selection；不进入普通 ToolRegistry。
    """

    def __init__(self, capability, owner, controller) -> None:
        self.capability = capability
        self.owner = owner
        self.controller = controller
        self.target = None  # None = 尚未选择（首请求带 selector）
        self.run_id = ""


def _selector_error(code: str, message: str) -> ToolResult:
    return ToolResult(
        content=json.dumps(
            {"ok": False, "error_code": code, "error_message": message},
            **_PROVIDER_JSON_KWARGS,
        ),
        is_error=True,
    )


def execute_reserved_computer_selector(decoded, selection) -> ToolResult | None:
    """computer_select_target 固定适配（Task 11a，设计 11.2）。

    非 selector 调用返回 None 走普通解析；重复选择、选择后变更、target
    值非法、target 未开启、start_run 失败均返回稳定 JSON 错误结果；
    成功时 start_run(owner, capability, target) 写入 target 租约并记录
    run_id，供后续请求去掉 selector、原生动作执行与 Turn 收尾使用。
    """
    if decoded.name != COMPUTER_SELECT_TARGET_TOOL_NAME:
        return None
    if selection is None:
        return _selector_error(
            "computer_disabled", "当前 Turn 未提供原生双 target 选择上下文"
        )
    if selection.target is not None:
        return _selector_error(
            "computer_invalid_action",
            f"目标已选择为 {selection.target!r}，选择后不可更改",
        )
    arguments = decoded.arguments if isinstance(decoded.arguments, dict) else {}
    target = arguments.get("target")
    if not isinstance(target, str) or target not in ("browser", "desktop"):
        return _selector_error(
            "computer_invalid_action", "target 必须是 browser 或 desktop"
        )
    if target not in selection.capability.targets:
        return _selector_error(
            "computer_disabled", f"操控目标 {target!r} 未在设置中开启"
        )
    try:
        snapshot = selection.controller.start_run(
            selection.owner, selection.capability, target=target
        )
    except ComputerControlError as error:
        return _selector_error(error.code, error.message)
    selection.target = target
    selection.run_id = str(getattr(snapshot, "run_id", "") or "")
    return ToolResult(
        content=json.dumps({"ok": True, "target": target}, **_PROVIDER_JSON_KWARGS)
    )
