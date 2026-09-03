"""generic 协议的 Provider 投影：固定工具 schema、动态提示、批处理约束与观察消息。

设计 10（统一动作协议）、19.1/19.3（Harness 和工具管线接入）：本模块不执行
鼠标或键盘，只做消息/投影转换；动作执行经 ComputerTurnLease.controller。
"""

from __future__ import annotations

import base64
import copy
import json
import logging

from core.computer.actions import (
    COMPUTER_ACTION_TOOL_SCHEMA,
    observation_provider_projection,
    parse_computer_action,
)
from core.computer.models import ComputerControlError, ComputerProtocol
from core.computer.tool_adapter import (
    COMPUTER_ACTION_TOOL_NAME,
    computer_turn_context_error,
    lease_from_context,
)
from core.tool_protocol import ToolResult

_LOGGER = logging.getLogger(__name__)

# follow-up 观察 user message 的固定说明文本（Task 9 brief 样例逐字）。
OBSERVATION_FOLLOWUP_TEXT = "【电脑观察】请只依据这张最新截图决定下一步。"

# 动态运行上下文里的电脑操控说明（设计 10.1 的七条语义；只在 capability
# 决策为 enabled 时加入，绝不进 build_stable_system_message）。
_GENERIC_DYNAMIC_PROMPT = (
    "电脑操控使用说明（generic 协议）：\n"
    "1. 第一次必须先调用 computer_action 的 observe 建立观察基线。\n"
    "2. 每次只调用一个电脑动作，不得批量调用或与其他工具混用。\n"
    "3. 每个动作后必须依据返回的最新截图决定下一步，不得猜测动作成功。\n"
    "4. 坐标以最新截图左上角为 (0,0)。\n"
    "5. 遇到登录、验证码或需要输入凭据时，改用 request_user_takeover 请求用户手动接管。\n"
    "6. 完成任务后输出正常最终结论，停止调用电脑工具，不继续无意义观察。"
)

# Provider tool result 的稳定 JSON 序列化参数（设计 10.2）。
_PROVIDER_JSON_KWARGS = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
}


def _is_generic_enabled(capability) -> bool:
    return bool(
        capability is not None
        and capability.enabled
        and capability.protocol == ComputerProtocol.GENERIC.value
    )


def build_generic_computer_tool(capability) -> dict | None:
    """仅 generic 能力开启时返回 computer_action 工具 schema 副本。

    返回深拷贝：调用方把它追加到现有 tools 尾部时不得改写模块级常量；
    capability 关闭或非 generic 协议时返回 None（tools 字节保持原样）。
    """
    if not _is_generic_enabled(capability):
        return None
    return copy.deepcopy(COMPUTER_ACTION_TOOL_SCHEMA)


def build_generic_dynamic_prompt(capability) -> str:
    """仅 generic 能力开启时返回电脑操控动态说明；否则空串（字节不变）。"""
    if not _is_generic_enabled(capability):
        return ""
    return _GENERIC_DYNAMIC_PROMPT


def validate_computer_tool_batch(tool_calls) -> None:
    """整批校验（设计 10.1）：一次只能有一个 computer_action，且不得混用。

    多个 computer call 或电脑动作与其他工具混用时抛 computer_invalid_action，
    本 Step 不执行任何电脑动作（零副作用）；无电脑动作或单个独立电脑动作
    时返回 None。接线点在调度层收到 tool_calls 之后、执行之前（Task 10）。
    """
    calls = list(tool_calls or [])
    computer_calls = sum(
        1
        for tool_call in calls
        if str((tool_call.get("function") or {}).get("name", "") or "")
        == COMPUTER_ACTION_TOOL_NAME
    )
    if computer_calls == 0:
        return
    if computer_calls > 1 or len(calls) > 1:
        raise ComputerControlError(
            "computer_invalid_action",
            "computer_action 一次只能调用一个电脑动作，不得批量调用或与其他工具混用；"
            "请只保留一个电脑动作，其余工作等看到新截图后再继续。",
        )


def _attempted_value(decoded, key: str) -> str:
    arguments = decoded.arguments if isinstance(decoded.arguments, dict) else {}
    value = arguments.get(key)
    return value if isinstance(value, str) else ""


def _error_result(target: str, action: str, error: ComputerControlError) -> ToolResult:
    """稳定 9 键 JSON 错误结果（设计 10.2）：不携带截图、输入正文或旧图。"""
    payload = {
        "ok": False,
        "target": target,
        "action": action,
        "screen_sha256": "",
        "width": 0,
        "height": 0,
        "origin_or_window": "",
        "error_code": error.code,
        "error_message": error.message,
    }
    return ToolResult(
        content=json.dumps(payload, **_PROVIDER_JSON_KWARGS),
        is_error=True,
    )


def _read_observation_png(png_path: str) -> bytes:
    if not png_path:
        raise ComputerControlError("computer_backend_error", "观察缺少截图文件")
    try:
        with open(png_path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise ComputerControlError(
            "computer_backend_error", f"读取最新观察截图失败：{error}"
        ) from error


def _observation_followup_message(observation, *, run_id: str, ordinal: int) -> dict:
    """最新截图的 follow-up user 多模态消息（设计 10.2）。

    PNG 转 data:image/png;base64 只存在于此消息；tool result JSON、metadata
    与日志绝不携带截图字节。_computer_v1 为私有元数据（run_id/ordinal/target）。
    """
    png_bytes = _read_observation_png(observation.png_path)
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": OBSERVATION_FOLLOWUP_TEXT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
        "_computer_v1": {
            "run_id": run_id,
            "ordinal": int(ordinal),
            "target": observation.target,
        },
    }


def execute_reserved_computer_tool(decoded, context) -> ToolResult | None:
    """固定适配入口（设计 19.2）：非 computer_action 返回 None 走普通解析。

    context 必须携带 Turn 冻结的 capability token、owner lease 与 run id；
    设置关闭、owner 不符或动作非法分别返回稳定 9 键 JSON 错误结果。
    动作序号取 controller.snapshot().action_count（controller 串行执行，
    execute 返回后该值即本次动作的 ordinal）。
    """
    if decoded.name != COMPUTER_ACTION_TOOL_NAME:
        return None
    lease = lease_from_context(context)
    context_error = computer_turn_context_error(lease)
    if context_error is not None:
        return _error_result(
            _attempted_value(decoded, "target"),
            _attempted_value(decoded, "kind"),
            context_error,
        )
    try:
        action = parse_computer_action(decoded.arguments)
    except ComputerControlError as parse_error:
        return _error_result(
            _attempted_value(decoded, "target"),
            _attempted_value(decoded, "kind"),
            parse_error,
        )
    try:
        observation = lease.controller.execute(lease.run_id, lease.owner, action)
        ordinal = int(lease.controller.snapshot().action_count)
        followup = _observation_followup_message(
            observation, run_id=lease.run_id, ordinal=ordinal
        )
    except ComputerControlError as domain_error:
        return _error_result(action.target, action.kind, domain_error)
    content = json.dumps(
        observation_provider_projection(observation),
        **_PROVIDER_JSON_KWARGS,
    )
    return ToolResult(content=content, followup_messages=(followup,))


def computer_error_tool_message(tool_call: dict, error: ComputerControlError) -> dict:
    """从原始 tool_call dict 合成稳定 9 键 JSON 错误 tool result 消息。

    供调度层两处共用（Task 10）：批处理约束拒绝（混用/多个 computer call，
    本 Step 不执行任何电脑动作）与取消打捞（悬空 computer call 生成
    computer_cancelled 结果）。参数解析失败时 target/kind 以空串占位，
    绝不携带截图、输入正文或旧图。
    """
    arguments = {}
    raw_arguments = str((tool_call.get("function") or {}).get("arguments") or "")
    if raw_arguments:
        try:
            parsed = json.loads(raw_arguments)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            arguments = parsed

    def _attempted(key: str) -> str:
        value = arguments.get(key)
        return value if isinstance(value, str) else ""

    result = _error_result(_attempted("target"), _attempted("kind"), error)
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id"),
        "name": (tool_call.get("function") or {}).get("name"),
        "content": result.to_message_content(),
    }
