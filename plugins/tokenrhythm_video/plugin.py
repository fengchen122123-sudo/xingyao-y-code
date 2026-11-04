"""tokenrhythm_video：基元律动（TokenRhythm）视频生成插件入口。

提供 tokenrhythm_generate_video 工具：
- 纯文生视频（wan3.0-video）
- 图生视频：首帧支持公网 HTTPS URL 直传；本地首帧文件先经 /v1/images/edits
  保真重绘换公网 OSS URL（¥0.2/次），再以 first_frame 角色注入 content

协议（异步任务式，与生图的同步接口不同；下述要点来自 2026-11-03 真实调用实测，
不是文档转述）：
- POST /v1/videos/generations → HTTP 202 返回 {"id", "status": "queued", ...}
- GET  /v1/videos/{id} 轮询，终态是 success（实测状态机 queued→running→success）
- 文本必须放 content 数组：content:[{"type":"text","text":"..."}]，写 prompt 字段直接 400
- 图生视频首帧只吃公网 HTTPS URL（上限约 4096 字符；base64 / data URL 实测全被拒），
  且 content 项必须带顶层 role 字段：{"type":"image_url","image_url":{"url":...},
  "role":"first_frame"}——缺 role 一律 400（不扣费），补上即 202
- 不显式传 resolution/duration 会按最贵档（1080P/5s）扣钱：本工具总是显式传参
- result.url 约 60 分钟时效，完成后必须及时下载；失败任务不扣费；无取消接口
- 计费按秒：480P ¥0.3/秒、720P ¥0.6/秒、1080P ¥1.2/秒；输入图片本身免费
- 本地首帧文件无法直接传：先用 /v1/images/edits（qwen-image-2.0，¥0.2/次）把本地图
  保真重绘一次、拿返回的 data[0].url 公网 OSS 地址作首帧（画面以重绘结果为准）

密钥安全约定与 tokenrhythm_image 完全一致：Key 只在运行时从
~/.ycode/config.json 的 providers 中 base_url 含 tokenrhythm.studio 的条目读取
（DPAPI 解密），仅存于内存、只用于请求头 Authorization: Bearer，绝不落盘。
未配置时工具直接返回配置指引错误，不发起任何请求。

为什么是独立插件而不是塞进 tokenrhythm_image：用户约束明确要求不改现有
生图插件的任何 schema 与行为；两个插件的请求协议也不同（同步 JSON vs
异步轮询），混在一个 register 里只会互相牵连。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

from core.llm_policy import session_id_headers, tokenrhythm_agent_headers
from core.tool_protocol import ToolResult, ToolSpec

_API_BASE = "https://tokenrhythm.studio/v1"
_VIDEO_URL = _API_BASE + "/videos/generations"

VALID_MODELS = ("wan3.0-video",)
VALID_RESOLUTIONS = ("480P", "720P", "1080P")

# duration：文档口径 2~30 秒，-1 为智能时长
_DURATION_MIN = 2
_DURATION_MAX = 30
_DURATION_SMART = -1

# 首帧图片限制：与生图插件垫图同口径（1 张、≤10MB、jpg/jpeg/png）
_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_IMAGE_ALLOWED_EXTS = (".jpg", ".jpeg", ".png")

# 轮询节奏：官方视频生成约 1~5 分钟。间隔 5s、总预算 600s；
# 超时只代表"不再等了"，任务可能仍在云端跑（错误文案里必须说清）。
_POLL_INTERVAL = 5.0
_POLL_TIMEOUT = 600.0
_HTTP_TIMEOUT = 60.0

_NOT_CONFIGURED_MSG = (
    "未配置基元律动（TokenRhythm）API Key：请在 星瑶Code 设置页添加提供商"
    "（base_url=https://tokenrhythm.studio/v1）并填入 Key，或确认"
    " ~/.ycode/config.json providers 中已有该条目，随后在插件页启用本插件。"
)


def _resolve_api_key() -> str:
    """运行时从用户配置读取基元律动 Key 并解密；失败一律返回空串。

    与 tokenrhythm_image 同一份配置条目（base_url 含 tokenrhythm.studio）。
    注意：import config 必须放在函数内部（避免循环导入）。两插件各自持有
    一份该函数是刻意的——plugin_loader 按目录独立加载模块，跨插件 import
    既不可靠（用户级同名覆盖会改变语义）也不被 import_sibling 支持。
    """
    try:
        import config  # 函数内导入，避免循环导入

        for provider in config._load().get("providers", []) or []:
            base_url = str(provider.get("base_url", "") or "")
            if "tokenrhythm.studio" not in base_url:
                continue
            raw_key = str(provider.get("api_key", "") or "")
            if not raw_key:
                return ""
            try:
                from core.credentials import unprotect_secret

                return unprotect_secret(raw_key).strip()
            except Exception:
                return ""
        return ""
    except Exception:
        return ""


def _detect_video_ext(head: bytes) -> str:
    """按文件头判断真实格式：MP4 的 ftyp 盒在 4~8 字节处，其余保守归 .mp4。"""
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return ".mp4"
    return ".mp4"


def _resolve_save_dir(save_dir: str | None) -> str:
    """解析保存目录：未传 → AI 生成内容目录；显式传值 → 原样尊重（与生图插件同口径）。"""
    if save_dir is None:
        from core.ai_output_paths import resolve_ai_output_dir

        return str(resolve_ai_output_dir())
    return os.path.abspath(save_dir or ".")


def _frame_dimensions(image_path: str) -> tuple:
    """读首帧宽高（用于推导 ratio）；读不出时回退 (0, 0) → 16:9。"""
    try:
        from PIL import Image

        with Image.open(image_path) as img:
            return img.size
    except Exception:
        return 0, 0


def _refit_size(width: int, height: int) -> str:
    """按首帧宽高比给 images/edits 选 size 档位（服务端支持
    1024x1024 / 1280x720 / 720x1280 / 1280x1280 / 1024x1792 / 1792x1024）。

    实测（2026-11-03 探针真跑）：不传 size 时 1280×720 的横图会被重绘成
    1024×1024 正方形，构图被重新裁切，首帧保真显著劣化（MAE 24 的主要
    来源）——所以必须显式传与原图宽高比一致的档位。
    """
    if width > 0 and height > 0:
        if height > width:
            return "720x1280"
        if width > height:
            return "1280x720"
    return "1024x1024"


# 本地首帧换公网 URL 的保真指令：视频接口不收 base64（实测被拒，上限 4096
# 字符），本地图必须先经 images/edits 换 OSS 地址。edits 是改图接口，语义上
# 会重绘一次——指令要求"原样重绘"把漂移压到最小，但画面仍以重绘结果为准。
_FRAME_REFIT_PROMPT = "请原样重绘输出这张图片，保持画面内容、构图、人物与色彩完全不变。"


def _upload_frame_for_url(image_path: str, api_key: str) -> str:
    """本地首帧 → 公网 HTTPS URL：经 /v1/images/edits 保真重绘换取 data[0].url。

    为什么绕这一步：视频接口对首帧只接受公网 HTTPS URL（实测 base64 / data
    URL 全被拒），而平台唯一的图片"上传换地址"入口就是改图接口。这一步额外
    消耗 ¥0.2/次（qwen-image-2.0），调用方（工具描述/桥）必须把这一点讲清楚。
    """
    boundary = "----YCodeVideoBoundary%s" % uuid.uuid4().hex
    # size 必须显式传：不传时服务端按 1024×1024 重绘，横/竖图构图会被重新裁切
    refit_size = _refit_size(*_frame_dimensions(image_path))
    lines = []
    for field_name, value in (
        ("model", "qwen-image-2.0"),
        ("prompt", _FRAME_REFIT_PROMPT),
        ("size", refit_size),
        ("response_format", "url"),
    ):
        lines.append("--%s" % boundary)
        lines.append('Content-Disposition: form-data; name="%s"' % field_name)
        lines.append("")
        lines.append(value)
    ext = os.path.splitext(image_path)[1].lower()
    with open(image_path, "rb") as handle:
        image_bytes = handle.read()
    lines.append("--%s" % boundary)
    lines.append(
        'Content-Disposition: form-data; name="image"; filename="%s"'
        % os.path.basename(image_path)
    )
    lines.append("Content-Type: %s" % ("image/png" if ext == ".png" else "image/jpeg"))
    lines.append("")
    lines.append("")
    head = "\r\n".join(lines).encode("utf-8")
    body = head + image_bytes + ("\r\n--%s--\r\n" % boundary).encode("utf-8")

    url = _API_BASE + "/images/edits"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
            "Authorization": "Bearer " + api_key,
            **tokenrhythm_agent_headers(url),
            **session_id_headers(),
        },
        method="POST",
    )
    result = _http_json(req)
    items = result.get("data") or []
    item = items[0] if items else {}
    if not isinstance(item, dict):
        item = {}
    oss_url = str(item.get("url") or "").strip()
    if not oss_url:
        raise ValueError(
            "换首帧公网地址失败：images/edits 未返回 data[0].url，原始响应：%s"
            % json.dumps(result, ensure_ascii=False)[:300]
        )
    return oss_url


def _derive_ratio(width: int, height: int) -> str:
    """按首帧宽高比推导 ratio：竖图（高>宽）出竖幅 9:16，其余 16:9。

    wan3.0-video 文档口径 ratio 16:9 / 9:16；首帧与输出比例不一致会导致
    画面裁切或黑边，所以跟随首帧而不是让用户手填。
    """
    if width > 0 and height > 0 and height > width:
        return "9:16"
    return "16:9"


def _save_video_bytes(payload: bytes, save_dir: str, prefix: str) -> str:
    """视频字节落盘：先写 .part 再 os.replace（半截文件绝不顶着正式名字出现）。

    刻意不走生图插件的 turn_undo 快照链路：视频几十 MB，把产物字节整体
    存进撤销记录既慢又会撑爆库；本工具的产物本也不属于回合撤销
    （落盘在回合外执行，prepare_mutation 会走 no_active_checkpoint 分支）。
    """
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(
        save_dir, "%s_%s%s" % (prefix, time.strftime("%Y%m%d_%H%M%S"), _detect_video_ext(payload[:12]))
    )
    tmp = os.path.join(save_dir, ".tokenrhythm_video_%s.part" % uuid.uuid4().hex)
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
    return path


def _download_video(url: str, save_dir: str) -> str:
    """下载 result.url 并落盘。官方要求"及时下载"，URL 可能有时效。"""
    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
        payload = resp.read()
    if not payload:
        raise ValueError("下载结果为空")
    return _save_video_bytes(payload, _resolve_save_dir(save_dir), "tokenrhythm_video")


def _error_from_http(exc: urllib.error.HTTPError) -> ToolResult:
    """HTTPError → 带排查提示的错误结果（detail 截前 500 字符）。"""
    try:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        detail = ""
    hint = ""
    if exc.code == 401:
        hint = "（Key 无效：请到设置页检查基元律动提供商的 API Key）"
    elif exc.code == 400:
        hint = "（参数问题：请检查 prompt/duration/resolution/content 是否符合要求）"
    elif exc.code == 402:
        hint = "（余额不足：请到 TokenRhythm 控制台充值）"
    return ToolResult(
        "视频接口返回 HTTP %d%s\n%s" % (exc.code, hint, detail), is_error=True
    )


def _http_json(req: urllib.request.Request) -> dict:
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        text = resp.read().decode("utf-8")
    return json.loads(text) if text.strip() else {}


def _auth_headers(api_key: str, url: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
        **tokenrhythm_agent_headers(url),
        **session_id_headers(),
    }


def generate_video(
    prompt: str,
    image: str = "",
    duration: int = 5,
    resolution: str = "720P",
    model: str = "wan3.0-video",
    save_dir: str | None = None,
) -> ToolResult:
    """文生视频 / 图生视频：创建任务 → 轮询 → 下载 result.url 落盘。"""
    prompt = str(prompt or "").strip()
    if not prompt:
        return ToolResult("缺少 prompt 参数：请提供画面/运动描述文字。", is_error=True)
    if model not in VALID_MODELS:
        return ToolResult(
            "非法 model：%r，可选值：%s" % (model, " / ".join(VALID_MODELS)), is_error=True
        )
    if resolution not in VALID_RESOLUTIONS:
        return ToolResult(
            "非法 resolution：%r，可选值：%s" % (resolution, " / ".join(VALID_RESOLUTIONS)),
            is_error=True,
        )
    # duration：-1 智能时长，或 2~30 的整数；bool 是 int 子类，先挡掉
    if isinstance(duration, bool) or not isinstance(duration, int):
        return ToolResult(
            "非法 duration：%r，必须为整数（%d~%d 秒，%d 表示智能时长）。"
            % (duration, _DURATION_MIN, _DURATION_MAX, _DURATION_SMART),
            is_error=True,
        )
    if duration != _DURATION_SMART and not _DURATION_MIN <= duration <= _DURATION_MAX:
        return ToolResult(
            "非法 duration：%d，可选 %d~%d 秒，或 %d 表示智能时长。"
            % (duration, _DURATION_MIN, _DURATION_MAX, _DURATION_SMART),
            is_error=True,
        )

    # 首帧（可选）：公网 HTTPS URL 直传；本地路径 → images/edits 换公网 URL。
    # 视频接口实测不收 base64 / data URL（上限约 4096 字符），本地图没有直传
    # 通道，只能先花 ¥0.2 经改图接口换 OSS 地址。
    image_ref = str(image or "").strip()
    width = height = 0
    if image_ref and not image_ref.lower().startswith(("http://", "https://")):
        image_path = os.path.abspath(image_ref)
        if not os.path.isfile(image_path):
            return ToolResult("首帧图片不存在：%s" % (image_path,), is_error=True)
        if os.path.getsize(image_path) > _IMAGE_MAX_BYTES:
            return ToolResult(
                "首帧图片超过 10MB 限制：%s，请压缩后再试。" % (image_path,), is_error=True
            )
        ext = os.path.splitext(image_path)[1].lower()
        if ext not in _IMAGE_ALLOWED_EXTS:
            return ToolResult(
                "首帧图片格式不支持：%r，仅支持 jpg/jpeg/png。" % (ext,), is_error=True
            )
        width, height = _frame_dimensions(image_path)

    api_key = _resolve_api_key()
    if not api_key:
        return ToolResult(_NOT_CONFIGURED_MSG, is_error=True)

    if image_ref and not image_ref.lower().startswith(("http://", "https://")):
        try:
            image_ref = _upload_frame_for_url(image_path, api_key)
        except urllib.error.HTTPError as exc:
            return _error_from_http(exc)
        except Exception as exc:
            return ToolResult(
                "换取首帧公网地址失败（本步骤消耗 ¥0.2 改图费）：%s" % (exc,), is_error=True
            )

    content = []
    if image_ref:
        # 图生视频：首帧图片项在前，文本指令在后。role 必须放 content 项顶层，
        # 缺失会被 400 拒绝（实测，不扣费）。
        content.append({
            "type": "image_url",
            "image_url": {"url": image_ref},
            "role": "first_frame",
        })
    content.append({"type": "text", "text": prompt})

    payload = {
        "model": model,
        "content": content,
        "resolution": resolution,
        "duration": duration,
        "ratio": _derive_ratio(width, height),
    }
    req = urllib.request.Request(
        _VIDEO_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_auth_headers(api_key, _VIDEO_URL),
        method="POST",
    )

    # ---- 阶段 1：创建任务（HTTP 202 + 任务 id）----
    try:
        created = _http_json(req)
    except urllib.error.HTTPError as exc:
        return _error_from_http(exc)
    except urllib.error.URLError as exc:
        return ToolResult("视频接口网络错误：%s" % (exc.reason,), is_error=True)
    except Exception as exc:
        return ToolResult("创建视频任务失败：%s" % (exc,), is_error=True)

    task_id = str(created.get("id") or "").strip()
    if not task_id:
        return ToolResult(
            "视频接口未返回任务 id，原始响应：%s"
            % json.dumps(created, ensure_ascii=False)[:500],
            is_error=True,
        )
    estimate = created.get("estimated_output_cost_cny")

    # ---- 阶段 2：轮询直到终态或超时 ----
    # 轮询端点是 /v1/videos/{id}（2026-11-03 真实调用实测：拼成
    # /v1/videos/generations/{id} 会 404，探针真跑踩过这个坑）
    poll_url = "%s/videos/%s" % (_API_BASE, task_id)
    deadline = time.monotonic() + _POLL_TIMEOUT
    final: dict = {}
    while time.monotonic() < deadline:
        poll_req = urllib.request.Request(
            poll_url, headers=_auth_headers(api_key, poll_url), method="GET"
        )
        try:
            status_doc = _http_json(poll_req)
        except urllib.error.HTTPError as exc:
            return _error_from_http(exc)
        except urllib.error.URLError as exc:
            # 单次轮询网络抖动不判死：下一轮再试（deadline 兜底）
            time.sleep(_POLL_INTERVAL)
            continue
        except Exception as exc:
            return ToolResult("轮询视频任务失败：%s" % (exc,), is_error=True)

        status = str(status_doc.get("status") or "").strip().lower()
        # 终态口径以实测为准：queued → running → success（不是文档里的 succeeded）
        if status in ("success", "succeeded", "completed"):
            final = status_doc
            break
        if status in ("failed", "error", "cancelled", "canceled"):
            reason = str(status_doc.get("error") or status_doc.get("message") or status)
            return ToolResult(
                "视频任务 %s 生成失败（%s）。失败任务不扣费；请调整描述或首帧后重试。"
                % (task_id, reason),
                is_error=True,
            )
        time.sleep(_POLL_INTERVAL)
    else:
        return ToolResult(
            "视频任务 %s 轮询超时（约 %d 秒仍未完成）。任务可能仍在云端执行，"
            "请稍后到 TokenRhythm 控制台查看任务 %s 的状态，不要立刻重复提交（可能重复扣费）。"
            % (task_id, int(_POLL_TIMEOUT), task_id),
            is_error=True,
        )

    # ---- 阶段 3：下载产物 ----
    result_field = final.get("result") or {}
    if not isinstance(result_field, dict):
        result_field = {}
    video_url = str(result_field.get("url") or "").strip()
    if not video_url:
        return ToolResult(
            "视频任务 %s 已完成但未返回 result.url，原始响应：%s"
            % (task_id, json.dumps(final, ensure_ascii=False)[:500]),
            is_error=True,
        )
    try:
        path = _download_video(video_url, save_dir)
    except Exception as exc:
        return ToolResult(
            "视频下载失败（result.url 可能已过期，任务 %s 不会重复扣费）：\n%s\nurl: %s"
            % (task_id, exc, video_url),
            is_error=True,
        )

    billing = final.get("billing") or {}
    cost = billing.get("total_cost_cny") if isinstance(billing, dict) else None

    lines = ["视频已生成并保存：%s" % path]
    if cost is not None:
        try:
            lines.append("本次费用：%.2f 元（以任务完成响应的 billing 为准）" % float(cost))
        except (TypeError, ValueError):
            pass
    elif estimate is not None:
        lines.append("预估费用：%s 元（任务完成响应未带 billing，以控制台账单为准）" % (estimate,))
    lines.append("任务 id：%s" % task_id)
    lines.append(
        "请务必在回复中直接告诉用户本地文件路径，便于用户用本地播放器查看该视频。"
    )
    # metadata 给桥层记账用（对话路径模型看不到）：任务 id 与实测费用，
    # 生成台账（character_generation_records）从这里取数
    meta_cost = None
    if cost is not None:
        try:
            meta_cost = float(cost)
        except (TypeError, ValueError):
            pass
    return ToolResult(
        "\n".join(lines), changed_files=[path],
        metadata={"task_id": task_id, "cost_cny": meta_cost, "resolution": resolution},
    )


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------

_PERMISSION_KWARGS = dict(
    read_only=False,
    # 不声明 mutates_files：视频落的是本工具的 AI 产物（save_dir 缺省 → AI 生成
    # 内容目录；落盘在回合外执行），不属于受控源码编辑，且
    # _save_video_bytes 刻意不接入按回合撤销记录器（几十 MB 产物存快照既慢又
    # 撑库，见该函数注释）；与 screenshot_capture / clipboard_image 的 AI 产物
    # 口径一致。声明为 True 会让规格 9.5 的启动校验按"未接入记录器"直接失败。
    mutates_files=False,
    requires_permission=True,    # 视频按条计费且耗时分钟级，写操作走用户审批
    cancellable=True,            # 轮询+下载耗时长，允许用户中断
    parallel_safe=False,
    # 规格 03 §3.2 取值表：发起外部网络请求的工具归 network
    side_effect_scope="network",
)


def register(registry):
    registry.register(ToolSpec(
        name="tokenrhythm_generate_video",
        description=(
            "基元律动（TokenRhythm）视频生成（wan3.0-video）：支持纯文生视频与"
            "图生视频（首帧：公网 HTTPS URL 直传；本地文件会先经改图接口保真重绘"
            "换公网地址，额外消耗 ¥0.2/次，画面以重绘结果为准）。"
            "创建任务后自动轮询（视频生成约 1~5 分钟），完成后下载保存到本地并返回"
            "路径与费用。duration 2~30 秒（-1 智能时长，默认 5），resolution "
            "480P/720P/1080P（默认 720P）。"
            "【计费】视频按秒计费：480P ¥0.3/秒、720P ¥0.6/秒、1080P ¥1.2/秒"
            "（720P/5秒 约 ¥3）；失败任务不扣费；无取消接口，提交前确认内容。"
            "【前置条件】需在插件页启用 tokenrhythm_video 插件，并在设置页配置"
            " base_url 含 tokenrhythm.studio 的提供商与 API Key（与生图插件同一 Key）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "画面与运动描述（图生视频时描述首帧之后如何运动），越具体越好",
                },
                "image": {
                    "type": "string",
                    "description": (
                        "首帧图片（可选）：公网 HTTPS URL 直接传入；本地路径仅支持"
                        " 1 张、≤10MB、jpg/jpeg/png，会先经改图接口换取公网地址"
                        "（¥0.2/次）。传入即图生视频"
                    ),
                },
                "duration": {
                    "type": "integer",
                    "minimum": -1,
                    "maximum": 30,
                    "description": "视频时长秒数，2~30；-1 表示智能时长；默认 5",
                },
                "resolution": {
                    "type": "string",
                    "enum": list(VALID_RESOLUTIONS),
                    "description": "分辨率，默认 720P",
                },
                "save_dir": {
                    "type": "string",
                    "description": "视频保存目录；不传则落到 AI 生成内容目录（设置页可配置，默认 ~/.ycode/tmp）",
                },
            },
            "required": ["prompt"],
        },
        execute=generate_video,
        activity_description="TokenRhythm 视频生成中",
        **_PERMISSION_KWARGS,
    ))
