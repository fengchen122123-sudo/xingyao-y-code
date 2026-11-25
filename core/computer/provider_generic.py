from __future__ import annotations

import base64
import copy
from dataclasses import replace
import json
import logging

from core.computer.actions import (
    COMPUTER_ACTION_TOOL_SCHEMA,
    observation_provider_projection,
    parse_computer_action,
    sanitized_action_summary,
)
from core.computer.models import ComputerControlError, ComputerProtocol
from core.computer.settings import get_browser_label, is_external_browser
from core.computer.tool_adapter import (
    COMPUTER_ACTION_TOOL_NAME,
    COMPUTER_SELECT_TARGET_TOOL_NAME,
    computer_turn_context_error,
    lease_from_context,
)
from core.tool_protocol import ToolResult

_LOGGER = logging.getLogger(__name__)

OBSERVATION_FOLLOWUP_TEXT = "【电脑观察】请只依据这张最新截图决定下一步。"

_OBSERVATION_MARKER_KEY = "_computer_v1"


def is_observation_protocol_message(obj) -> bool:
    if isinstance(obj, dict):
        return _OBSERVATION_MARKER_KEY in obj
    if isinstance(obj, str):
        return f'"{_OBSERVATION_MARKER_KEY}"' in obj
    return False

# 动态运行上下文里的电脑操控说明（通用化重构 Task E 的元素优先学说；只在
# capability 决策为 enabled 时加入，绝不进 build_stable_system_message）。
_GENERIC_DYNAMIC_CORE_RULES = (
    "电脑操控使用说明（generic 协议）：\n"
    "1. 第一次必须先调用 computer_action 的 observe 建立观察基线。\n"
    "2. 优先用元素引用操作，其次才是坐标：\n"
    "   - 观察结果返回「控件列表」时，用 click_element 点击、set_value_element 写值\n"
    "     （输入框优先 set_value_element，中文不过输入法）；\n"
    "   - 浏览器观察会自动附带页面可交互元素列表（el-N 编号），直接按列表操作，\n"
    "     禁止凭截图目测坐标；\n"
    "   - 只有元素列表为空（canvas 界面、图片型界面、自绘 UI）时，才退回截图坐标点击，\n"
    "     并在回复中说明为什么元素不可用。\n"
    "3. 每次只调用一个 computer_action，不得与其他工具混用；有把握的连续微动作"
    "（点击→输入→按键）用 kind=sequence 打包一次执行（中途不截图，完成后返回"
    "标注截图）；没把握时单独执行并逐次验证。\n"
    "   - 【强制】确定性的连续操作（点击输入框 → 输入文本 → 回车）必须打包成一次\n"
    "     sequence 调用，禁止把每一步拆成独立调用逐步请示。\n"
    "   - sequence 内允许条件等待：页面需要加载时插入 wait（duration_ms 100-10000）\n"
    "     等页面就绪，再由后续步骤接着做，不要靠反复 observe 轮询等元素出现；\n"
    "     click_element / set_value_element 自带元素自动等待，不必在外层反复观察。\n"
    "   - 等待界面变化（文本出现、元素出现/可用、窗口激活）优先用 wait_for 条件等待\n"
    "     （text_exists / element_exists / element_enabled 支持 browser 同文档和\n"
    "     desktop；window_active 只支持 desktop），不要用 wait 加反复 observe 轮询；\n"
    "     wait_for 命中后直接返回新观察，超时返回 computer_wait_timeout，此时改用\n"
    "     observe 检查画面再决定下一步。\n"
    "   - sequence 里打包 click_element / set_value_element 时，元素引用必须来自\n"
    "     sequence 之前最近一次 list_elements 返回的 ref，否则会报\n"
    "     computer_element_stale，需要重新 list_elements 取新 ref 后再发。\n"
    "   - 只有下一步依赖观察结果（需要看画面才知道点哪里）时才单独调用；能一次性\n"
    "     规划完的，一律打包。\n"
    "4. 每个动作后依据最新观察决定下一步，不得猜测成功；观察里的"
    "surface_generation 变化表示画面已切换，旧坐标与旧元素引用立即作废。\n"
    "5. 出现焦点被抢、会话失活、窗口挪位时：先 list_elements 或 activate_surface"
    "重建基线，严禁按记忆里的旧坐标连续重试。\n"
    "6. 遇到登录、验证码或需要凭据时，改用 request_user_takeover 请求用户接管。\n"
    "7. 完成任务后输出正常最终结论，停止调用电脑工具，不继续无意义观察。"
)

_BROWSER_REGION_OBSERVE_HINT = (
    "   - 图片型/密集小目标（网格游戏、图表、地图）看不清时，用 observe_region"
    " 指定 target=browser 与 x/y/width/height（CSS 视口坐标）放大该区域；"
    "之后的点击坐标以这块区域左上角为 (0,0)，回到整屏用 observe。"
)


def _build_browser_dynamic_rule(capability) -> str:
    targets = tuple(getattr(capability, "targets", ()) or ())
    if "browser" not in targets:
        if "desktop" in targets:
            return "\n8. 当前仅开启桌面操控（target=desktop），未开启浏览器操控，不得调用 target=browser。"
        return ""
    browser = str(getattr(capability, "browser", "") or "").strip()
    if browser == "embedded":
        lead = (
            "8. 当前系统默认浏览器为「星瑶内置浏览器」：浏览器网页打开（http/https）、"
            "导航与交互必须优先使用 computer_action（target=browser），直接在 GUI 右侧面板"
            "的内嵌浏览器里实时呈现（用户围观），不要先用 browser_pane 打开无调试端口的页面。"
        )
    elif is_external_browser(browser):
        label = get_browser_label(browser)
        window_note = "独立窗口运行，" if browser != "chromium" else ""
        lead = (
            f"8. 当前系统默认浏览器为「{label}」（{browser}，外部浏览器）：浏览器操作必须优先"
            f"并严格通过 computer_action（target=browser）驱动该浏览器执行（{window_note}"
            "实时截图与状态在 GUI 右侧面板「操控」页签预览），禁止改用 browser_pane 内置面板绕开系统设置。"
        )
    else:
        lead = "8. 浏览器自动化在 GUI 右侧面板实时呈现。"
    return (
        f"\n{lead}"
        "页面按项目分辨率渲染（桌面页默认 1440×900，移动页建议 375×667）；"
        "与目标页面不匹配时先用 browser_set_viewport 调整再导航。\n"
        f"{_BROWSER_REGION_OBSERVE_HINT}"
    )

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
    if not _is_generic_enabled(capability):
        return None
    return copy.deepcopy(COMPUTER_ACTION_TOOL_SCHEMA)


def build_generic_dynamic_prompt(capability) -> str:
    """仅 generic 能力开启时返回电脑操控动态说明；否则空串（字节不变）。"""
    if not _is_generic_enabled(capability):
        return ""
    return f"{_GENERIC_DYNAMIC_CORE_RULES}{_build_browser_dynamic_rule(capability)}"


def validate_computer_tool_batch(tool_calls) -> None:
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


# 稳定错误码 → 模型下一步动作（2026-10-21）：错误码本身只是分类，模型真正
# 需要的是"现在该换什么策略"。文案统一为短句祈使，避免模型把可重试错误当
# 致命错误、或把不可重试错误当偶发失败反复重试。只在 error_message 末尾追加，
# 9 键 JSON 形状与 error_code 一律不变。
_ERROR_NEXT_STEP = {
    "computer_element_stale": "先重新 list_elements 或 observe 拿到新的元素引用，再操作",
    "computer_elements_unavailable": "当前界面没有可用控件，改用截图坐标或换一个目标窗口",
    "computer_element_unsupported": "该控件不支持这个操作，换用其它控件或改用坐标点击",
    "computer_surface_changed": "画面或窗口已切换，先 observe 重建基线再继续",
    "computer_surface_ambiguous": "候选窗口不唯一，先 list_surfaces 后用 surface_ref 指定",
    "computer_coordinate_out_of_bounds": "坐标超出当前画面尺寸，先 observe 确认尺寸再点",
    "computer_no_progress": "画面连续多次没有变化，换一种操作策略或直接询问用户",
    "computer_action_timeout": "动作超时且结果未知，先 observe 确认当前状态再决定",
    "computer_wait_timeout": "等待条件在时限内未满足，先 observe 检查画面再决定下一步",
    "computer_invalid_action": "按工具说明修正参数后重试，不要原样重复同一调用",
}


def _error_message_with_next_step(error: ComputerControlError) -> str:
    message = str(getattr(error, "message", "") or "")
    hint = _ERROR_NEXT_STEP.get(str(getattr(error, "code", "") or ""))
    if not hint or hint in message:
        return message
    return f"{message}；下一步：{hint}"


def _error_result(target: str, action: str, error: ComputerControlError) -> ToolResult:
    payload = {
        "ok": False,
        "target": target,
        "action": action,
        "screen_sha256": "",
        "width": 0,
        "height": 0,
        "origin_or_window": "",
        "error_code": error.code,
        "error_message": _error_message_with_next_step(error),
    }
    details = getattr(error, "details", None) or {}
    if action == "sequence" or "failed_step_index" in details:
        if "failed_step_index" in details:
            payload["failed_step_index"] = details["failed_step_index"]
        if "submission_state" in details:
            payload["submission_state"] = details["submission_state"]
        if "completed_steps" in details:
            payload["completed_steps"] = details["completed_steps"]
        if "total_steps" in details:
            payload["total_steps"] = details["total_steps"]
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


_OBSERVATION_ELEMENTS_TEXT_PREFIX = (
    "【电脑观察·控件列表】请优先用元素引用动作操作"
    "（click_element 点击 / set_value_element 写值，目标输入框优先"
    " set_value_element）；元素都不可用时再用截图坐标。元素列表："
)

# 无元素场景的定位提示（只在本次没有控件列表、模型只能看截图时追加）：
# 目标控件可能只有三四十像素（如 32×33 的按钮），整屏截图目测必然点偏，
# 必须先 observe_region 放大局部再点。有控件列表时不加——模型用 click_element
# 就够，加了只会白占 token。
_OBSERVATION_REGION_HINT = (
    "【定位提示】若目标控件较小、密集或看不清，先用 observe_region 放大该区域再"
    "点击，不要凭整屏截图目测坐标。"
)

_OBSERVATION_BROWSER_POINT_HINT = (
    "【定位提示】本页没有可用的元素引用，请依据这张整屏截图判断位置，"
    "坐标以截图左上角为 (0,0)，不要凭记忆复用旧坐标。"
)

# 局部观察（browser observe_region）的坐标语义提示（E5，2026-11-03）：
# 区域图的坐标原点不是页面左上角，模型必须知道点击会按区域原点换算。
_OBSERVATION_REGION_VIEW_HINT = (
    "【局部观察】当前是页面局部画面：坐标以这块区域左上角为 (0,0)，"
    "点击会换算回页面真实位置；需要回到整屏时调用 observe。"
)

_DENSE_ELEMENT_MIN_COUNT = 60
_DENSE_ELEMENT_HARD_CAP = 300

# 帧契约（2026-10-21，借 zcode frame-contract 的「体积上限 + 成对引用」思路）：
# 单张观察图进上下文前必须有字节上限，否则长回合里截图会累积吃干上下文。
# 2MB 是权衡值——1280 宽的界面截图 PNG 通常 100~600KB，超限基本意味着高分屏
# 整屏原图，缩放一档就能回到预算内。
_OBSERVATION_IMAGE_MAX_BYTES = 2 * 1024 * 1024
_OBSERVATION_IMAGE_MAX_EDGE = 1280

_OBSERVATION_UNCHANGED_TEXT = (
    "【电脑观察】本次动作后画面与上一张截图完全相同，已省略重复图片，"
    "请直接依据上一张截图判断下一步；若怀疑画面其实已变，先调用 observe 重新确认。"
)

_OBSERVATION_IMAGE_OMITTED_TEXT = (
    "【电脑观察】本次截图体积仍超出上限，已省略图片，只返回画面摘要；"
    "请依据 frame 标识、控件列表与画面尺寸判断，必要时缩小窗口比例后重新 observe。"
)

# B 方案（窗口被遮挡时的降级观察）：本次没有截图的说明。窗口最小化/被遮挡时
# WebView2 渲染被节流，截图会卡住或失败；必须显式告诉模型"这次没图"，否则它
# 会把缺图当成页面空白，或者反复 observe 期待出现图片。
_OBSERVATION_NO_SCREENSHOT_TEXT = (
    "【电脑观察】本次没有截图（浏览器窗口不可见或被遮挡时渲染会被节流，"
    "截图可能卡住或失败）。请只依据下面的文本信息判断下一步，"
    "不要假设画面为空；需要图片时先让窗口重新可见（切回窗口或取消最小化）再 observe。"
)

_OBSERVATION_DESKTOP_NO_SCREENSHOT_TEXT = (
    "【电脑观察】本次没有截图（桌面截图或控件采集期间窗口可能正在切换或被占用）。"
    "请重新 observe 建立稳定的桌面观察基线后再操作。"
)

_OBSERVATION_PAGE_TEXT_HEADING = "【页面正文】"


def no_screenshot_observation_text(observation) -> str:
    if getattr(observation, "target", "") == "desktop":
        lines = [_OBSERVATION_DESKTOP_NO_SCREENSHOT_TEXT]
        error_message = str(getattr(observation, "error_message", "") or "")
        if error_message and not getattr(observation, "ok", True):
            lines.append(error_message)
        reason = str(getattr(observation, "screenshot_error", "") or "")
        if reason and reason != error_message:
            lines.append(reason)
        return "\n".join(lines)
    lines = [_OBSERVATION_NO_SCREENSHOT_TEXT]
    reason = str(getattr(observation, "screenshot_error", "") or "")
    if reason:
        lines.append(reason)
    page_text = str(getattr(observation, "page_text", "") or "")
    if page_text:
        lines.append(f"{_OBSERVATION_PAGE_TEXT_HEADING}\n{page_text}")
    else:
        lines.append("【页面正文】本次也未能读到可见文本。")
    return "\n".join(lines)


def _frame_reference_line(
    observation, *, size_bytes: int = 0, frame=None
) -> str:
    frame_id = str(
        getattr(frame or observation, "frame_id", "")
        or getattr(observation, "frame_id", "")
        or ""
    )
    sha = str(getattr(observation, "screen_sha256", "") or "")
    width = int(getattr(frame, "image_width", 0) or getattr(observation, "width", 0) or 0)
    height = int(getattr(frame, "image_height", 0) or getattr(observation, "height", 0) or 0)
    parts = [
        f"frame={frame_id or sha[:12] or 'unknown'}",
        f"{width}x{height}",
    ]
    if size_bytes:
        parts.append(f"png={size_bytes}B")
    return "；".join(parts)


def _downscale_png(png_bytes: bytes) -> bytes | None:
    """超过体积上限时按最长边缩放重编码；Pillow 不可用或失败返回 None。

    缩放是尽力而为：拿不到 Pillow 就按原图继续走体积判断，最终由调用方
    决定是省略图片还是照常发送——绝不因为缩放失败让整个观察链路报错。
    """
    try:
        import io

        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            image.load()
            longest = max(image.width, image.height)
            if longest <= _OBSERVATION_IMAGE_MAX_EDGE:
                return None
            ratio = _OBSERVATION_IMAGE_MAX_EDGE / float(longest)
            target = (
                max(1, int(image.width * ratio)),
                max(1, int(image.height * ratio)),
            )
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
            resized = image.resize(target, resample)
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
    except Exception:
        _LOGGER.exception("观察截图缩放失败，按原图判断体积上限")
        return None


def _element_position_label(element, observation, frame=None) -> str:
    bounds = tuple(element.get("bounds", ()) or ())
    if len(bounds) != 4:
        return ""
    try:
        left, top, right, bottom = (float(v) for v in bounds)
        vw = float(
            getattr(frame, "image_width", 0) or getattr(observation, "width", 0) or 0
        )
        vh = float(
            getattr(frame, "image_height", 0) or getattr(observation, "height", 0) or 0
        )
        ow = float(
            getattr(frame, "crop_width", 0)
            or getattr(observation, "original_width", 0)
            or 0
        )
        oh = float(
            getattr(frame, "crop_height", 0)
            or getattr(observation, "original_height", 0)
            or 0
        )
        if vw <= 0 or vh <= 0 or ow <= 0 or oh <= 0:
            return ""
        ox = float(getattr(observation, "origin_x", 0) or 0)
        oy = float(getattr(observation, "origin_y", 0) or 0)
        mx = (((left + right) / 2.0) - ox) * vw / ow
        my = (((top + bottom) / 2.0) - oy) * vh / oh
    except (TypeError, ValueError):
        return ""
    if not (0.0 <= mx <= vw and 0.0 <= my <= vh):
        return ""
    col = "左" if mx < vw / 3 else ("右" if mx > vw * 2 / 3 else "中")
    row = "上" if my < vh / 3 else ("下" if my > vh * 2 / 3 else "中")
    if col == "中" and row == "中":
        return "画面中央"
    if col == "中":
        return "画面" + row + "部"
    if row == "中":
        return "画面" + col + "侧"
    return "画面" + col + row + "角"


def _project_bounds_to_image(bounds, observation, frame=None) -> tuple:
    raw = tuple(bounds or ())
    if len(raw) != 4:
        return raw
    try:
        left, top, right, bottom = (float(v) for v in raw)
    except (TypeError, ValueError):
        return raw
    ox = float(getattr(observation, "origin_x", 0) or 0)
    oy = float(getattr(observation, "origin_y", 0) or 0)
    sx = float(
        getattr(frame, "model_to_css_x", 0.0)
        or getattr(observation, "scale_x", 1.0)
        or 1.0
    )
    sy = float(
        getattr(frame, "model_to_css_y", 0.0)
        or getattr(observation, "scale_y", 1.0)
        or 1.0
    )
    if sx <= 0:
        sx = 1.0
    if sy <= 0:
        sy = 1.0
    if ox == 0.0 and oy == 0.0 and abs(sx - 1.0) < 1e-6 and abs(sy - 1.0) < 1e-6:
        return raw
    img_w = int(getattr(frame, "image_width", 0) or getattr(observation, "width", 0) or 0)
    img_h = int(getattr(frame, "image_height", 0) or getattr(observation, "height", 0) or 0)
    x0 = round((left - ox) / sx)
    y0 = round((top - oy) / sy)
    x1 = round((right - ox) / sx)
    y1 = round((bottom - oy) / sy)
    if img_w > 0 and img_h > 0:
        x0 = min(max(x0, 0), img_w)
        x1 = min(max(x1, 0), img_w)
        y0 = min(max(y0, 0), img_h)
        y1 = min(max(y1, 0), img_h)
    return (x0, y0, x1, y1)


def _element_lines(observation, frame=None) -> list[str]:
    lines = []
    for element in getattr(observation, "elements", ()) or ():
        position = _element_position_label(element, observation, frame=frame)
        suffix = (" 位置=" + position) if position else ""
        bounds = _project_bounds_to_image(
            element.get("bounds", ()), observation, frame=frame
        )
        lines.append(
            f"- {element.get('ref', '')} {element.get('kind', '')} "
            f"name={element.get('name', '')!r} "
            f"bounds={bounds} "
            f"enabled={bool(element.get('enabled', True))}{suffix}"
        )
    return lines


def _ui_note_block(observation) -> list[dict]:
    note = str(getattr(observation, "ui_note", "") or "").strip()
    if not note:
        return []
    return [{"type": "text", "text": note}]


def _elements_content_blocks(element_lines: list[str]) -> list[dict]:
    if not element_lines:
        return []
    return [
        {"type": "text", "text": _OBSERVATION_ELEMENTS_TEXT_PREFIX},
        {"type": "text", "text": "\n".join(element_lines)},
    ]


def _is_dense_unnamed_elements(elements) -> bool:
    items = list(elements or ())
    if len(items) >= _DENSE_ELEMENT_HARD_CAP:
        return True
    if len(items) < _DENSE_ELEMENT_MIN_COUNT:
        return False
    named = sum(1 for item in items if str(item.get("name") or "").strip())
    return named * 5 <= len(items)


def _dense_elements_note(count: int) -> str:
    return (
        f"【电脑观察·控件列表】本页 {count} 个可交互元素且几乎没有名称"
        "（图片型/自绘界面），本次已省略逐行清单；请依据截图判断局面，"
        "坐标以这张截图左上角为 (0,0)。需要精确点击密集小目标时，"
        "显式调用 observe 可取回完整控件列表（el-N 引用）。"
    )


def _observation_followup_message(
    observation, *, run_id: str, ordinal: int, controller=None
) -> dict:
    from core.computer.frames import (
        prepare_observation_image_frame,
        sync_frame_disabled,
    )

    meta = {
        "run_id": run_id,
        "ordinal": int(ordinal),
        "target": observation.target,
    }
    if getattr(observation, "frame_id", ""):
        meta["frame_id"] = str(observation.frame_id)
    frame_line = _frame_reference_line(observation)
    elements = getattr(observation, "elements", ()) or ()
    has_png = bool(str(getattr(observation, "png_path", "") or ""))
    # 显式 observe 永远附图：模型主动要看画面时省略图片等于没回答它。
    # 局部观察同理——模型点名要看这块区域，画面没变也必须给。
    if (
        getattr(observation, "screen_unchanged", False)
        and observation.action not in ("observe", "observe_region")
    ):
        element_lines = _element_lines(observation)
        dense_unnamed = _is_dense_unnamed_elements(elements)
        if dense_unnamed and getattr(observation, "action", "") != "observe":
            list_blocks = [
                {"type": "text", "text": _dense_elements_note(len(element_lines))}
            ]
        else:
            list_blocks = _elements_content_blocks(element_lines)
        unchanged_blocks = list_blocks + _ui_note_block(observation) + [
            {"type": "text", "text": f"{_OBSERVATION_UNCHANGED_TEXT}\n{frame_line}"}
        ]
        return {
            "role": "user",
            "content": unchanged_blocks,
            "_computer_v1": {**meta, "image_omitted": "unchanged"},
        }
    # B 方案降级 / 纯文本控件清单：
    # - png_path 为空且无截图失败、非显式 observe、有控件列表时，保持纯文本观察，且不修改后端 _coord_frame；
    # - 真正截图失败 / 显式 observe 无图时，禁用本帧坐标输入并显式说明无图原因。
    if not has_png:
        element_lines = _element_lines(observation)
        dense_unnamed = _is_dense_unnamed_elements(elements)
        if dense_unnamed and getattr(observation, "action", "") != "observe":
            list_blocks = [
                {"type": "text", "text": _dense_elements_note(len(element_lines))}
            ]
        else:
            list_blocks = _elements_content_blocks(element_lines)
        if (
            element_lines
            and not str(getattr(observation, "screenshot_error", "") or "")
            and getattr(observation, "action", "") != "observe"
            and not dense_unnamed
        ):
            return {
                "role": "user",
                "content": _elements_content_blocks(element_lines),
                "_computer_v1": meta,
            }
        sync_frame_disabled(
            observation,
            reason="no_screenshot",
            controller=controller,
            run_id=run_id,
        )
        return {
            "role": "user",
            "content": list_blocks + _ui_note_block(observation) + [
                {"type": "text", "text": no_screenshot_observation_text(observation)},
                {"type": "text", "text": frame_line},
            ],
            "_computer_v1": {**meta, "image_omitted": "no_screenshot"},
        }
    raw_png = _read_observation_png(observation.png_path)
    png_bytes, omitted_reason, frame = prepare_observation_image_frame(
        raw_png,
        observation,
        max_bytes=_OBSERVATION_IMAGE_MAX_BYTES,
        downscale_fn=_downscale_png,
        controller=controller,
        run_id=run_id,
    )
    element_lines = _element_lines(observation, frame=frame)
    dense_unnamed = _is_dense_unnamed_elements(elements)
    # 密集无名列表在「动作后的自动观察」里不逐行输出（每步几百行会吞掉大量
    # 上下文），但显式 observe 是模型主动要看，必须给全，否则它永远拿不到
    # el-N 引用、只能靠目测坐标点 16px 格子。
    if dense_unnamed and getattr(observation, "action", "") != "observe":
        list_blocks = [
            {"type": "text", "text": _dense_elements_note(len(element_lines))}
        ]
    else:
        list_blocks = _elements_content_blocks(element_lines)
    if png_bytes is None:
        return {
            "role": "user",
            "content": list_blocks + [
                {
                    "type": "text",
                    "text": f"{_OBSERVATION_IMAGE_OMITTED_TEXT}\n"
                    f"{_frame_reference_line(observation, size_bytes=len(raw_png), frame=frame)}",
                }
            ],
            "_computer_v1": {**meta, "image_omitted": omitted_reason or "too_large"},
        }
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    content = list_blocks + _ui_note_block(observation)
    content.append({"type": "text", "text": OBSERVATION_FOLLOWUP_TEXT})
    if not element_lines:
        if getattr(observation, "action", "") == "observe_region":
            content.append(
                {"type": "text", "text": _OBSERVATION_REGION_VIEW_HINT}
            )
        elif observation.target == "browser":
            content.append({"type": "text", "text": _OBSERVATION_BROWSER_POINT_HINT})
        else:
            content.append({"type": "text", "text": _OBSERVATION_REGION_HINT})
    content.append({"type": "image_url", "image_url": {"url": data_url}})
    return {
        "role": "user",
        "content": content,
        "_computer_v1": meta,
    }


def execute_reserved_computer_tool(decoded, context) -> ToolResult | None:
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
            observation,
            run_id=lease.run_id,
            ordinal=ordinal,
            controller=lease.controller,
        )
        frame_id = str(getattr(observation, "frame_id", "") or "")
        if frame_id:
            synced = lease.controller.get_observation_frame(lease.run_id, frame_id)
            if (
                synced is not None
                and synced.image_width > 0
                and synced.image_height > 0
            ):
                observation = replace(
                    observation,
                    width=synced.image_width,
                    height=synced.image_height,
                    scale_x=synced.model_to_css_x,
                    scale_y=synced.model_to_css_y,
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
