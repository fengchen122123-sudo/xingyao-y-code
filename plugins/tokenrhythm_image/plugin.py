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
_GENERATE_URL = _API_BASE + "/images/generations"
_EDITS_URL = _API_BASE + "/images/edits"

VALID_SIZES = (
    "1024x1024",
    "1280x720",
    "720x1280",
    "1280x1280",
    "1024x1792",
    "1792x1024",
)
VALID_MODELS = ("qwen-image-2.0", "wan2.7-image")

_EDIT_MAX_BYTES = 10 * 1024 * 1024
_EDIT_ALLOWED_EXTS = (".jpg", ".jpeg", ".png")

_NOT_CONFIGURED_MSG = (
    "未配置基元律动（TokenRhythm）API Key：请在 星瑶Code 设置页添加提供商"
    "（base_url=https://tokenrhythm.studio/v1）并填入 Key，或确认"
    " ~/.ycode/config.json providers 中已有该条目，随后在插件页启用本插件。"
)


def _resolve_api_key() -> str:
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

                key = unprotect_secret(raw_key).strip()
                return key
            except Exception:
                return ""
        return ""
    except Exception:
        return ""


def _detect_ext(head: bytes) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    return ".img"


def _response_format_for(model: str) -> str:
    return "b64_json" if model == "wan2.7-image" else "url"


def _resolve_save_dir(save_dir: str | None) -> str:
    from core.ai_output_paths import resolve_ai_output_save_dir

    return resolve_ai_output_save_dir(save_dir)


def _save_image_bytes(
    payload: bytes, save_dir: str | None, prefix: str, tool_name: str = "tokenrhythm_generate_image"
) -> str:
    save_dir = _resolve_save_dir(save_dir)
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(
        save_dir,
        "%s_%s%s" % (
            prefix,
            time.strftime("%Y%m%d_%H%M%S"),
            _detect_ext(bytes(payload[:8])),
        ),
    )
    from core.turn_undo import coordinator as _tu_coordinator
    from core.turn_undo import recorder as _tu_recorder
    from core.turn_undo.models import NodeKind, TurnUndoError

    handle = None
    with _tu_coordinator.get_coordinator().workspace_lock(_tu_coordinator.thread_workspace_root()):
        try:
            handle = _tu_recorder.prepare_mutation(
                display_path=path,
                after_kind=NodeKind.REGULAR_FILE,
                after_content=payload,
                tool_name=tool_name,
                tool_call_id="",
            )
        except TurnUndoError as error:
            if error.code != "no_active_checkpoint":
                raise
            handle = None
        with open(path, "wb") as f:
            f.write(payload)
        if handle is not None:
            _tu_recorder.verify_after_mutation(handle)
    return path


def _download_image(url: str, save_dir: str | None, prefix: str, tool_name: str = "tokenrhythm_generate_image") -> str:
    save_dir = _resolve_save_dir(save_dir)
    os.makedirs(save_dir, exist_ok=True)
    tmp = os.path.join(save_dir, ".tokenrhythm_%s.part" % uuid.uuid4().hex)
    try:
        urllib.request.urlretrieve(url, tmp)
        with open(tmp, "rb") as f:
            payload = f.read()
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
    return _save_image_bytes(payload, save_dir, prefix, tool_name)


def _save_b64_image(
    b64: str, save_dir: str | None, prefix: str, tool_name: str = "tokenrhythm_generate_image"
) -> str:
    import base64

    text = str(b64 or "").strip()
    if "base64," in text:  # 兼容 data URL 前缀写法
        text = text.split("base64,", 1)[1]
    data = base64.b64decode(text)
    if not data:
        raise ValueError("base64 解码结果为空")
    return _save_image_bytes(data, save_dir, prefix, tool_name)


def _save_image_from_result(
    result: dict, save_dir: str | None, prefix: str, tool_name: str = "tokenrhythm_generate_image"
) -> tuple:
    items = result.get("data") or []
    item = items[0] if items else {}
    if not isinstance(item, dict):
        item = {}
    oss_url = str(item.get("url") or "").strip()
    b64 = str(item.get("b64_json") or "").strip()
    if b64:
        try:
            return _save_b64_image(b64, save_dir, prefix, tool_name), "", ""
        except Exception as exc:
            return "", "接口返回的 base64 图片数据无法解码：%s" % (exc,), ""
    if oss_url:
        try:
            return _download_image(oss_url, save_dir, prefix, tool_name), "", oss_url
        except Exception as exc:
            return "", "图片下载失败（URL 请尽快使用）：{}\nimageUrl: {}".format(exc, oss_url), oss_url
    return "", (
        "接口响应缺少 data[0].b64_json / data[0].url，原始响应：%s"
        % json.dumps(result, ensure_ascii=False)[:500]
    ), ""

def _error_from_http(exc: urllib.error.HTTPError) -> ToolResult:
    try:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        detail = ""
    hint = ""
    if exc.code == 401:
        hint = "（Key 无效：请到设置页检查基元律动提供商的 API Key）"
    elif exc.code == 400:
        hint = "（参数问题：请检查 prompt/size/model/n 是否符合要求）"
    return ToolResult(
        "生图接口返回 HTTP %d%s\n%s" % (exc.code, hint, detail), is_error=True
    )


def generate_image(
    prompt: str,
    size: str = "1024x1024",
    model: str = "qwen-image-2.0",
    n: int = 1,
    save_dir: str | None = None,
) -> ToolResult:
    prompt = (prompt or "").strip()
    if not prompt:
        return ToolResult("缺少 prompt 参数：请提供图片描述文字。", is_error=True)
    if size not in VALID_SIZES:
        return ToolResult(
            "非法 size：%r，可选值：%s" % (size, " / ".join(VALID_SIZES)),
            is_error=True,
        )
    if model not in VALID_MODELS:
        return ToolResult(
            "非法 model：%r，可选值：%s" % (model, " / ".join(VALID_MODELS)),
            is_error=True,
        )
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= int(n) <= 4:
        return ToolResult("非法 n：%r，必须为 1-4 的整数。" % (n,), is_error=True)
    n = int(n)

    api_key = _resolve_api_key()
    if not api_key:
        return ToolResult(_NOT_CONFIGURED_MSG, is_error=True)

    payload = {
        "model": model,
        "prompt": prompt,
        "n": n,
        "size": size,
        "response_format": _response_format_for(model),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _GENERATE_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            **tokenrhythm_agent_headers(_GENERATE_URL),
            **session_id_headers(),
        },
        method="POST",
    )

    try:
        # 生图正常耗时约 5~17 秒，超时必须给足
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return _error_from_http(exc)
    except urllib.error.URLError as exc:
        return ToolResult("生图接口网络错误：%s" % (exc.reason,), is_error=True)
    except Exception as exc:
        return ToolResult("生图请求失败：%s" % (exc,), is_error=True)

    path, error, oss_url = _save_image_from_result(result, save_dir, "tokenrhythm")
    if error:
        return ToolResult(error, is_error=True)

    cost_cny = 0.20 * n

    lines = [
        "图片已生成并保存：%s" % path,
        "本次费用：%.2f 元（%.2f 元/张 × %d 张）" % (cost_cny, 0.20, n),
        "请务必在回复中直接用 Markdown 图片语法展示该图片（无需用户要求，用户可见）："
        "![生成的图片](%s)" % path,
    ]
    return ToolResult(
        "\n".join(lines), changed_files=[path],
        metadata={"oss_url": oss_url, "cost_cny": cost_cny},
    )


def edit_image(
    prompt: str,
    image: str,
    size: str = "",
    model: str = "qwen-image-2.0",
    save_dir: str | None = None,
) -> ToolResult:
    """图生图（垫图改图）：multipart 上传 1 张原图 + 文字指令，POST /v1/images/edits。"""
    prompt = (prompt or "").strip()
    if not prompt:
        return ToolResult("缺少 prompt 参数：请提供改图指令文字。", is_error=True)

    img_path = os.path.abspath(str(image or "").strip())
    if not img_path or not os.path.isfile(img_path):
        return ToolResult("垫图文件不存在：%s" % (img_path,), is_error=True)
    if os.path.getsize(img_path) > _EDIT_MAX_BYTES:
        return ToolResult(
            "垫图超过 10MB 限制：%s，请压缩后再试。" % (img_path,), is_error=True
        )
    ext = os.path.splitext(img_path)[1].lower()
    if ext not in _EDIT_ALLOWED_EXTS:
        return ToolResult(
            "垫图格式不支持：%r，仅支持 jpg/jpeg/png。" % (ext,), is_error=True
        )

    api_key = _resolve_api_key()
    if not api_key:
        return ToolResult(_NOT_CONFIGURED_MSG, is_error=True)

    boundary = "----YCodeBoundary%s" % uuid.uuid4().hex
    lines = []
    for field_name, value in (
        ("model", model),
        ("prompt", prompt),
    ):
        lines.append("--%s" % boundary)
        lines.append('Content-Disposition: form-data; name="%s"' % field_name)
        lines.append("")
        lines.append(value)
    size = str(size or "").strip()
    if size:
        lines.append("--%s" % boundary)
        lines.append('Content-Disposition: form-data; name="size"')
        lines.append("")
        lines.append(size)
    response_format = _response_format_for(model)
    if response_format == "b64_json":
        lines.append("--%s" % boundary)
        lines.append('Content-Disposition: form-data; name="response_format"')
        lines.append("")
        lines.append(response_format)
    with open(img_path, "rb") as f:
        image_bytes = f.read()
    lines.append("--%s" % boundary)
    lines.append(
        'Content-Disposition: form-data; name="image"; filename="%s"'
        % os.path.basename(img_path)
    )
    lines.append("Content-Type: %s" % (
        "image/png" if ext == ".png" else "image/jpeg"
    ))
    lines.append("")
    lines.append("")
    head = "\r\n".join(lines).encode("utf-8")
    body = (
        head
        + image_bytes
        + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    )

    req = urllib.request.Request(
        _EDITS_URL,
        data=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
            "Authorization": "Bearer " + api_key,
            **tokenrhythm_agent_headers(_EDITS_URL),
            **session_id_headers(),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return _error_from_http(exc)
    except urllib.error.URLError as exc:
        return ToolResult("改图接口网络错误：%s" % (exc.reason,), is_error=True)
    except Exception as exc:
        return ToolResult("改图请求失败：%s" % (exc,), is_error=True)

    path, error, oss_url = _save_image_from_result(
        result, save_dir, "tokenrhythm_edit", tool_name="tokenrhythm_edit_image"
    )
    if error:
        return ToolResult(error, is_error=True)

    lines = [
        "图片已改图并保存：%s" % path,
        "请务必在回复中直接用 Markdown 图片语法展示该图片（无需用户要求，用户可见）："
        "![生成的图片](%s)" % path,
    ]
    return ToolResult(
        "\n".join(lines), changed_files=[path],
        metadata={"oss_url": oss_url, "cost_cny": 0.2},
    )


_PERMISSION_KWARGS = dict(
    read_only=False,
    mutates_files=True,
    requires_permission=True,
    cancellable=True,
    parallel_safe=False,
    side_effect_scope="network",
)


def register(registry):
    registry.register(ToolSpec(
        name="tokenrhythm_generate_image",
        description=(
            "基元律动（TokenRhythm）文生图：用 qwen-image-2.0 或 wan2.7-image 模型"
            "根据文字描述（中英文均可，越具体越好）生成 1~4 张图片并立即下载保存到"
            "本地，返回本地文件路径与本次费用（0.20 元/张）。支持尺寸："
            "1024x1024 / 1280x720 / 720x1280 / 1280x1280 / 1024x1792 / 1792x1024。"
            "生成耗时约 5~17 秒。"
            "【注意计费】每张 0.20 元，同一主题已生成过图片不要重复生成。"
            "生成成功后必须在回复中用 Markdown 图片语法 ![](本地路径) 直接展示图片，"
            "不要只回复文件路径。"
            "【前置条件】需在插件页启用 tokenrhythm_image 插件，并在设置页配置"
            " base_url 含 tokenrhythm.studio 的提供商与 API Key（未配置时工具会"
            "返回配置指引）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "图片描述，中文英文均可，越具体效果越好",
                },
                "size": {
                    "type": "string",
                    "enum": list(VALID_SIZES),
                    "description": "图片尺寸，默认 1024x1024；横幅用 1280x720，竖幅用 720x1280",
                },
                "model": {
                    "type": "string",
                    "enum": list(VALID_MODELS),
                    "description": "模型，qwen-image-2.0（默认，通用高质量）或 wan2.7-image",
                },
                "n": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                    "description": "生成张数 1-4，默认 1；每张 0.20 元，按需选择",
                },
                "save_dir": {
                    "type": "string",
                    "description": "图片保存目录；不传则落到 AI 生成内容目录（设置页可配置，默认 ~/.ycode/tmp）",
                },
            },
            "required": ["prompt"],
        },
        execute=generate_image,
        activity_description="TokenRhythm 生图中",
        **_PERMISSION_KWARGS,
    ))

    registry.register(ToolSpec(
        name="tokenrhythm_edit_image",
        description=(
            "基元律动（TokenRhythm）图生图（垫图改图）：上传 1 张原图（≤10MB，"
            "jpg/jpeg/png）并附文字指令修改画面（如换背景、改风格、加元素），"
            "结果立即下载保存到本地。生成耗时约 5~17 秒，按生成结果计费。"
            "【垫图来源】用户消息中 @图片文件 时，附件提示会给出完整本地路径，"
            "把该路径作为 image 参数传入即可。"
            "成功后必须在回复中用 Markdown 图片语法 ![](本地路径) 直接展示图片。"
            "【前置条件】需在插件页启用 tokenrhythm_image 插件，并在设置页配置"
            " base_url 含 tokenrhythm.studio 的提供商与 API Key。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "改图指令，说明要在原图上做什么修改",
                },
                "image": {
                    "type": "string",
                    "description": "原图本地路径（必填）：仅 1 张、≤10MB、jpg/jpeg/png",
                },
                "size": {
                    "type": "string",
                    "description": "输出尺寸（可选）；留空则沿用接口默认",
                },
                "model": {
                    "type": "string",
                    "enum": list(VALID_MODELS),
                    "description": "模型，qwen-image-2.0（默认）或 wan2.7-image",
                },
                "save_dir": {
                    "type": "string",
                    "description": "图片保存目录；不传则落到 AI 生成内容目录（设置页可配置，默认 ~/.ycode/tmp）",
                },
            },
            "required": ["prompt", "image"],
        },
        execute=edit_image,
        activity_description="TokenRhythm 改图中",
        **_PERMISSION_KWARGS,
    ))
