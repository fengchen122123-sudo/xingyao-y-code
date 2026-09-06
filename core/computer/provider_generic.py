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
    sanitized_action_summary,
)
from core.computer.models import ComputerControlError, ComputerProtocol
from core.computer.tool_adapter import (
    COMPUTER_ACTION_TOOL_NAME,
    COMPUTER_SELECT_TARGET_TOOL_NAME,
    computer_turn_context_error,
    lease_from_context,
)
from core.tool_protocol import ToolResult

_LOGGER = logging.getLogger(__name__)

# follow-up 观察 user message 的固定说明文本（Task 9 brief 样例逐字）。
OBSERVATION_FOLLOWUP_TEXT = "【电脑观察】请只依据这张最新截图决定下一步。"

# 动态运行上下文里的电脑操控说明（通用化重构 Task E 的元素优先学说；只在
# capability 决策为 enabled 时加入，绝不进 build_stable_system_message）。
_GENERIC_DYNAMIC_PROMPT = (
    "电脑操控使用说明（generic 协议）：\n"
    "1. 第一次必须先调用 computer_action 的 observe 建立观察基线。\n"
    "2. 原生应用窗口优先用 list_elements 拿控件列表：用 click_element 点击、"
    "set_value_element 写输入框（中文直接写值不过输入法）；标注截图里的"
    "编号（el-N）就是元素引用；只有元素不可达（自绘界面/远程/游戏）才退回"
    "截图坐标点击。\n"
    "3. 每次只调用一个 computer_action，不得与其他工具混用；有把握的连续微动作"
    "（点击→输入→按键）用 kind=sequence 打包一次执行（中途不截图，完成后返回"
    "标注截图）；没把握时单独执行并逐次验证。\n"
    "4. 每个动作后依据最新观察决定下一步，不得猜测成功；观察里的"
    "surface_generation 变化表示画面已切换，旧坐标与旧元素引用立即作废。\n"
    "5. 出现焦点被抢、会话失活、窗口挪位时：先 list_elements 或 activate_surface"
    "重建基线，严禁按记忆里的旧坐标连续重试。\n"
    "6. 遇到登录、验证码或需要凭据时，改用 request_user_takeover 请求用户接管。\n"
    "7. 完成任务后输出正常最终结论，停止调用电脑工具，不继续无意义观察。"
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


def computer_tool_arguments_projection(name: str, arguments) -> dict | None:
    """computer 保留名工具参数的脱敏投影（设计 22.3/22.4，Task 16 隐私审计）。

    - computer_action：经 parse_computer_action 严格解析后只保留
      actions.sanitized_action_summary 摘要（输入正文/组合键/URL 只留
      code point 数与 SHA-256）；解析失败回稳定错误形状，绝不回显原文；
    - computer_select_target：只含 target 枚举，原样保留；
    - 其他工具名返回 None（调用方保持原样，绝不触碰普通工具参数）。

    ``arguments`` 可为已解析 dict 或原始 JSON 字符串；非法形态按解析失败
    处理。投影是纯函数，供事件载荷与落库 tool_calls 两处共用。
    """
    if name == COMPUTER_SELECT_TARGET_TOOL_NAME:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        target = arguments.get("target") if isinstance(arguments, dict) else None
        return {"target": target} if isinstance(target, str) else {}
    if name != COMPUTER_ACTION_TOOL_NAME:
        return None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = None
    if not isinstance(arguments, dict):
        arguments = None
    try:
        action = parse_computer_action(arguments)
    except ComputerControlError:
        return {
            "error_code": "computer_invalid_action",
            "error_message": "computer_action 参数无法解析，原文已脱敏省略",
        }
    return sanitized_action_summary(action)


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


# 元素列表观察的 follow-up 说明文本（阶段二 a11y-first，2026-09-06）。
_OBSERVATION_ELEMENTS_TEXT_PREFIX = (
    "【电脑观察·控件列表】请优先用元素引用动作操作"
    "（click_element 点击 / set_value_element 写值，目标输入框优先"
    " set_value_element）；元素都不可用时再用截图坐标。元素列表："
)


def _observation_followup_message(observation, *, run_id: str, ordinal: int) -> dict:
    """观察的 follow-up user 消息（设计 10.2 + 阶段二双路径）。

    有 elements 时为纯文本控件列表（a11y-first，请求不含截图字节）；
    否则维持既有路径：PNG 转 data:image/png;base64 只存在于此消息。
    tool result JSON、metadata 与日志绝不携带截图字节。_computer_v1 为
    私有元数据（run_id/ordinal/target），两种路径一致。
    """
    meta = {
        "run_id": run_id,
        "ordinal": int(ordinal),
        "target": observation.target,
    }
    if observation.elements:
        lines = [
            f"- {e.get('ref', '')} {e.get('kind', '')} name={e.get('name', '')!r} "
            f"bounds={tuple(e.get('bounds', ()))} enabled={bool(e.get('enabled', True))}"
            for e in observation.elements
        ]
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": _OBSERVATION_ELEMENTS_TEXT_PREFIX},
                {"type": "text", "text": "\n".join(lines)},
            ],
            "_computer_v1": meta,
        }
    png_bytes = _read_observation_png(observation.png_path)
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": OBSERVATION_FOLLOWUP_TEXT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
        "_computer_v1": meta,
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
