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

# 观察跟进消息持久化时的结构标记（_observation_followup_message 写入的
# _computer_v1 键）。展示层据此把这类系统协议消息从用户气泡/红点导航中
# 过滤掉——它们是系统发给模型的观察提示，不是用户发送的内容。
_OBSERVATION_MARKER_KEY = "_computer_v1"


def is_observation_protocol_message(obj) -> bool:
    """判定是否为电脑操控观察跟进协议消息（展示层过滤用）。

    兼容三种输入形状：
    - memory 解析后的消息行 dict：_parse_message_rows 会把 JSON content 的
      顶层键提升到行上，_computer_v1 出现在行 dict；
    - 持久化 content 原始串（整个 message dict 的 JSON）：串内含该标记；
    - 解析后的 message dict 本身。
    其余形状（真实用户文本、多模态块列表、非字符串/字典）一律 False——
    真实用户消息绝无此标记，模型上下文（get_history）不受展示过滤影响。
    """
    if isinstance(obj, dict):
        return _OBSERVATION_MARKER_KEY in obj
    if isinstance(obj, str):
        return f'"{_OBSERVATION_MARKER_KEY}"' in obj
    return False

# 动态运行上下文里的电脑操控说明（通用化重构 Task E 的元素优先学说；只在
# capability 决策为 enabled 时加入，绝不进 build_stable_system_message）。
_GENERIC_DYNAMIC_PROMPT = (
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
    "   - 只有下一步依赖观察结果（需要看画面才知道点哪里）时才单独调用；能一次性\n"
    "     规划完的，一律打包。\n"
    "4. 每个动作后依据最新观察决定下一步，不得猜测成功；观察里的"
    "surface_generation 变化表示画面已切换，旧坐标与旧元素引用立即作废。\n"
    "5. 出现焦点被抢、会话失活、窗口挪位时：先 list_elements 或 activate_surface"
    "重建基线，严禁按记忆里的旧坐标连续重试。\n"
    "6. 遇到登录、验证码或需要凭据时，改用 request_user_takeover 请求用户接管。\n"
    "7. 完成任务后输出正常最终结论，停止调用电脑工具，不继续无意义观察。\n"
    "8. 浏览器自动化在 GUI 右侧面板的内嵌浏览器里实时呈现（用户围观），页面按"
    "项目分辨率渲染（桌面页默认 1440×900，移动页建议 375×667）；与目标页面"
    "不匹配时先用 browser_set_viewport 调整再导航。\n"
    "   - 图片型/密集小目标（网格游戏、图表、地图）看不清时，用 observe_region"
    " 指定 target=browser 与 x/y/width/height（CSS 视口坐标）放大该区域；"
    "之后的点击坐标以这块区域左上角为 (0,0)，回到整屏用 observe。"
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
    "computer_invalid_action": "按工具说明修正参数后重试，不要原样重复同一调用",
}


def _error_message_with_next_step(error: ComputerControlError) -> str:
    """错误文案追加「下一步」，让模型把错误码换成具体策略而不是盲目重试。"""
    message = str(getattr(error, "message", "") or "")
    hint = _ERROR_NEXT_STEP.get(str(getattr(error, "code", "") or ""))
    if not hint or hint in message:
        return message
    return f"{message}；下一步：{hint}"


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
        "error_message": _error_message_with_next_step(error),
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

# 无元素场景的定位提示（只在本次没有控件列表、模型只能看截图时追加）：
# 目标控件可能只有三四十像素（如 32×33 的按钮），整屏截图目测必然点偏，
# 必须先 observe_region 放大局部再点。有控件列表时不加——模型用 click_element
# 就够，加了只会白占 token。
_OBSERVATION_REGION_HINT = (
    "【定位提示】若目标控件较小、密集或看不清，先用 observe_region 放大该区域再"
    "点击，不要凭整屏截图目测坐标。"
)

# 浏览器 target 的定位提示（E3 修复）：observe_region 只支持 desktop，浏览器
# 没有局部放大动作；旧文案对 browser 一视同仁，模型照做只会拿到
# computer_invalid_action。浏览器只能看整屏图，因此提示强调"坐标以截图为准"。
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

# 「密集无名元素」判定阈值（元素数下限）：低于这个数量时逐行清单仍然便宜且
# 有参考价值；超过它再叠加"命名元素占比 ≤20%"才判为清单无信息量。
_DENSE_ELEMENT_MIN_COUNT = 60
# 硬上限：元素多到这个量级时，即使都有名字，逐行清单也会把上下文吃掉，
# 一律退化为「截图为主 + 显式 observe 可取全量」。
_DENSE_ELEMENT_HARD_CAP = 300

# 帧契约（2026-10-21，借 zcode frame-contract 的「体积上限 + 成对引用」思路）：
# 单张观察图进上下文前必须有字节上限，否则长回合里截图会累积吃干上下文。
# 2MB 是权衡值——1280 宽的界面截图 PNG 通常 100~600KB，超限基本意味着高分屏
# 整屏原图，缩放一档就能回到预算内。
_OBSERVATION_IMAGE_MAX_BYTES = 2 * 1024 * 1024
_OBSERVATION_IMAGE_MAX_EDGE = 1280

# 画面与上一张完全相同时的替代说明：不重复附图，省下的 token 留给真实信息。
_OBSERVATION_UNCHANGED_TEXT = (
    "【电脑观察】本次动作后画面与上一张截图完全相同，已省略重复图片，"
    "请直接依据上一张截图判断下一步；若怀疑画面其实已变，先调用 observe 重新确认。"
)

# 截图超限且缩放后仍超限时的说明。
_OBSERVATION_IMAGE_OMITTED_TEXT = (
    "【电脑观察】本次截图体积仍超出上限，已省略图片，只返回画面摘要；"
    "请依据 screen_sha256 与画面尺寸判断，必要时缩小窗口比例后重新 observe。"
)

# B 方案（窗口被遮挡时的降级观察）：本次没有截图的说明。窗口最小化/被遮挡时
# WebView2 渲染被节流，截图会卡住或失败；必须显式告诉模型"这次没图"，否则它
# 会把缺图当成页面空白，或者反复 observe 期待出现图片。
_OBSERVATION_NO_SCREENSHOT_TEXT = (
    "【电脑观察】本次没有截图（浏览器窗口不可见或被遮挡时渲染会被节流，"
    "截图可能卡住或失败）。请只依据下面的文本信息判断下一步，"
    "不要假设画面为空；需要图片时先让窗口重新可见（切回窗口或取消最小化）再 observe。"
)

# 无截图时附在文本观察里的页面正文章节标题（正文长度已由 backend 按码点截断）。
_OBSERVATION_PAGE_TEXT_HEADING = "【页面正文】"


def no_screenshot_observation_text(observation) -> str:
    """无截图时的纯文本观察正文（B 方案降级；generic 与原生协议共用）。

    为什么需要：截图缺失时观察只剩 DOM 信息（元素列表 / 页面正文），模型必须
    同时知道"没有图"和"为什么没有图"，才能决定是继续按文本操作还是先恢复窗口。
    正文长度已由 backend 截断，这里不再二次截断（再截短只会丢信息）。
    """
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


def _frame_reference_line(observation, *, size_bytes: int = 0) -> str:
    """成对引用行（帧契约）：模型据此引用「同一帧」，而不是反复请求新图。

    只含画面身份与尺寸，不含路径、时间戳或随机序号，保证相同画面字节稳定。
    """
    sha = str(getattr(observation, "screen_sha256", "") or "")
    parts = [
        f"frame={sha[:12] or 'unknown'}",
        f"{int(getattr(observation, 'width', 0) or 0)}x"
        f"{int(getattr(observation, 'height', 0) or 0)}",
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


def _element_position_label(element, observation) -> str:
    """按元素 bounds（物理屏幕坐标，矩形四角 left/top/right/bottom）给一个方位描述。

    name 为空的控件（WebView2/Chromium 常见）只靠 bounds 无法判断是什么，
    方位描述至少能让模型知道「右上角那个按钮」与列表项的对应关系。
    坐标换算失败或落在画面外时返回空串，不阻断列表输出。
    """
    bounds = tuple(element.get("bounds", ()) or ())
    if len(bounds) != 4:
        return ""
    try:
        # 元素 bounds 是矩形四角 (left, top, right, bottom)——与生产方一致：
        # windows_elements_uia 直接取 UIA CurrentBoundingRectangle 的四边，
        # 浏览器侧 JS 取 getBoundingClientRect 的 right/bottom；绘图端
        # som_annotator 也按同一语义读。故中心是两角均值，不能把 right/bottom
        # 当宽高写成 left + right/2（那会把中心推到元素右下方，甚至推出画面）。
        left, top, right, bottom = (float(v) for v in bounds)
        vw = float(getattr(observation, "width", 0) or 0)
        vh = float(getattr(observation, "height", 0) or 0)
        ow = float(getattr(observation, "original_width", 0) or 0)
        oh = float(getattr(observation, "original_height", 0) or 0)
        if vw <= 0 or vh <= 0 or ow <= 0 or oh <= 0:
            return ""
        # 反算与 screenshot_to_physical 同构：(物理 - 原点) * 模型尺寸 / 原始尺寸。
        # 原点可为负（副屏在主屏上方/左侧），减法即完成平移，无需额外分支。
        # 退化矩形（宽或高为 0，right==left / bottom==top）中心即角点，同样安全。
        mx = (((left + right) / 2.0) - float(getattr(observation, "origin_x", 0) or 0)) * vw / ow
        my = (((top + bottom) / 2.0) - float(getattr(observation, "origin_y", 0) or 0)) * vh / oh
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


def _element_lines(observation) -> list[str]:
    """控件列表的逐行渲染；带方位描述，未知位置时不加后缀。"""
    lines = []
    for element in getattr(observation, "elements", ()) or ():
        position = _element_position_label(element, observation)
        suffix = (" 位置=" + position) if position else ""
        lines.append(
            f"- {element.get('ref', '')} {element.get('kind', '')} "
            f"name={element.get('name', '')!r} "
            f"bounds={tuple(element.get('bounds', ()))} "
            f"enabled={bool(element.get('enabled', True))}{suffix}"
        )
    return lines


def _elements_content_blocks(element_lines: list[str]) -> list[dict]:
    """控件列表的两个文本块（前缀 + 逐行）；无元素时返回空列表。

    显式 observe 的附图路径与两条降级路径（无截图/超限）共用同一形状，
    保证「能看图但看不了图」时也不会丢掉 el-N 引用。
    """
    if not element_lines:
        return []
    return [
        {"type": "text", "text": _OBSERVATION_ELEMENTS_TEXT_PREFIX},
        {"type": "text", "text": "\n".join(element_lines)},
    ]


def _is_dense_unnamed_elements(elements) -> bool:
    """密集且几乎无名的元素列表 → 逐行清单信息量极低。

    图片型/自绘界面（老式图片网格游戏、canvas 外壳里的空链接）会一次返回
    上百个 name='' 的元素：清单既定位不了"哪个格子是哪一格"，又每步吞掉大量
    上下文，还把模型从"看图判断"引向"按 bounds 猜"。这类列表改为以截图为主，
    只在显式 observe 时保留清单。
    """
    items = list(elements or ())
    if len(items) >= _DENSE_ELEMENT_HARD_CAP:
        # 元素本身已经多到一行一行发就会吃掉大部分上下文：不论有没有名字
        # 都退化为「截图为主」，显式 observe 仍可取回完整清单。
        return True
    if len(items) < _DENSE_ELEMENT_MIN_COUNT:
        return False
    named = sum(1 for item in items if str(item.get("name") or "").strip())
    return named * 5 <= len(items)


def _dense_elements_note(count: int) -> str:
    """密集无名元素时的替代说明：不给逐行清单，明确让模型看图/用坐标。"""
    return (
        f"【电脑观察·控件列表】本页 {count} 个可交互元素且几乎没有名称"
        "（图片型/自绘界面），本次已省略逐行清单；请依据截图判断局面，"
        "坐标以这张截图左上角为 (0,0)。需要精确点击密集小目标时，"
        "显式调用 observe 可取回完整控件列表（el-N 引用）。"
    )


def _observation_followup_message(observation, *, run_id: str, ordinal: int) -> dict:
    """观察的 follow-up user 消息（设计 10.2 + 阶段二双路径 + 帧契约）。

    四条路径按优先级：
    1. 有 elements 且不是显式 observe、且不是「密集无名」：纯文本控件列表
       （a11y-first，请求不含截图字节）；
    2. 画面与上一张完全相同（screen_unchanged 且不是显式 observe）：纯文本
       说明，不重复附图；
    3. 本次没有截图（png_path 为空，B 方案降级：窗口被遮挡/最小化导致截图
       失败）：纯文本观察（无截图说明 + 页面正文），绝不读图；有 elements 时
       仍附控件列表（否则模型既没图也没 el-N 引用）；
    4. 其余：PNG 转 data:image/png;base64；显式 observe 有 elements 时先给
       控件列表再附图，受体积上限约束——超限先缩放，缩放后仍超限则退化为
       纯文摘略（带控件列表）。无 elements 时按 target 追加定位提示：
       desktop 引导 observe_region 放大局部，browser 只能看整屏图，改为明确
       坐标语义（避免模型对 browser 调用 desktop 专属动作）。

    密集无名元素（图片型/网格界面）走附图路径但不带逐行清单：这类页面必须
    靠看图判断，纯 bounds 清单既看不懂也点不准；显式 observe 例外，仍给全量。

    tool result JSON、metadata 与日志绝不携带截图字节。_computer_v1 为私有
    元数据（run_id/ordinal/target，必要时带 image_omitted 原因）。
    """
    meta = {
        "run_id": run_id,
        "ordinal": int(ordinal),
        "target": observation.target,
    }
    frame_line = _frame_reference_line(observation)
    elements = getattr(observation, "elements", ()) or ()
    element_lines = _element_lines(observation)
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
    # 显式 observe 例外：模型主动要看画面时必须给图，同时保留控件列表，
    # 让它「看图认位置、用 el-N 精确点击」，而不是退回目测坐标。
    if (
        element_lines
        and getattr(observation, "action", "") != "observe"
        and not dense_unnamed
    ):
        return {
            "role": "user",
            "content": _elements_content_blocks(element_lines),
            "_computer_v1": meta,
        }
    # 显式 observe 永远附图：模型主动要看画面时省略图片等于没回答它。
    # 局部观察同理——模型点名要看这块区域，画面没变也必须给。
    if (
        getattr(observation, "screen_unchanged", False)
        and observation.action not in ("observe", "observe_region")
    ):
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{_OBSERVATION_UNCHANGED_TEXT}\n{frame_line}"}
            ],
            "_computer_v1": {**meta, "image_omitted": "unchanged"},
        }
    # B 方案降级：png_path 为空说明本次没有截图，绝不能走 _read_observation_png
    # （空路径会抛 computer_backend_error，整条观察链就白降级了）。改为纯文本
    # 观察：说明"本次没有截图"的原因，并附上后端截断好的页面正文。
    if not str(getattr(observation, "png_path", "") or ""):
        return {
            "role": "user",
            "content": list_blocks + [
                {"type": "text", "text": no_screenshot_observation_text(observation)},
                {"type": "text", "text": frame_line},
            ],
            "_computer_v1": {**meta, "image_omitted": "no_screenshot"},
        }
    png_bytes = _read_observation_png(observation.png_path)
    if len(png_bytes) > _OBSERVATION_IMAGE_MAX_BYTES:
        scaled = _downscale_png(png_bytes)
        if scaled:
            png_bytes = scaled
    if len(png_bytes) > _OBSERVATION_IMAGE_MAX_BYTES:
        return {
            "role": "user",
            "content": list_blocks + [
                {
                    "type": "text",
                    "text": f"{_OBSERVATION_IMAGE_OMITTED_TEXT}\n"
                    f"{_frame_reference_line(observation, size_bytes=len(png_bytes))}",
                }
            ],
            "_computer_v1": {**meta, "image_omitted": "too_large"},
        }
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    content = list_blocks
    content.append({"type": "text", "text": OBSERVATION_FOLLOWUP_TEXT})
    # 定位提示按 target 区分：observe_region 是 desktop 专属动作，浏览器没有
    # 局部放大能力（旧文案会让模型对 browser 调用它并直接吃 invalid_action）。
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
