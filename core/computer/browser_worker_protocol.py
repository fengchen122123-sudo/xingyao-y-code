from __future__ import annotations

import json
from typing import Any

OPERATIONS = ("observe", "execute", "sync_frame", "release", "close")

OBSERVATION_FIELDS = (
    "run_id",
    "target",
    "action",
    "ok",
    "screen_sha256",
    "png_path",
    "width",
    "height",
    "original_width",
    "original_height",
    "origin_x",
    "origin_y",
    "scale_x",
    "scale_y",
    "window_title",
    "page_origin",
    "page_title",
    # B 方案（窗口被遮挡时的降级观察）：正文摘要与"本次为何没有截图"的说明
    # 必须跨管道回传父进程，否则降级观察在受管进程模式下只剩空图信息。
    "page_text",
    "screenshot_error",
    "surface_id",
    "surface_generation",
    "frame_id",
    "viewport_generation",
    "elements",
)

_TUPLE_FIELDS = {
    "keys": (str,),
    "path": ((int, float), (int, float)),
}

# 元素列表 name 的防御性截断上限（与执行侧 ELEMENT_COLLECT_SCRIPT 的
# MAX_NAME 对齐；正常载荷已截断，这里兜底异常形态）。
_MAX_ELEMENT_NAME_CHARS = 80


def encode_request(command_id: int, op: str, **payload: Any) -> str:
    """请求编码：op 必须在白名单内（父进程侧出口校验）。"""
    if op not in OPERATIONS:
        raise ValueError(f"未知的受管进程操作: {op!r}")
    request: dict[str, Any] = {"id": int(command_id), "op": op}
    request.update(payload)
    return json.dumps(request, ensure_ascii=False)


def decode_request(line: str) -> dict:
    """子进程侧严格解码：op 白名单外直接拒绝（fail-closed）。"""
    request = json.loads(line)
    if not isinstance(request, dict):
        raise ValueError("受管进程请求必须是 JSON 对象")
    op = request.get("op")
    if op not in OPERATIONS:
        raise ValueError(f"未知的受管进程操作: {op!r}")
    command_id = request.get("id")
    if not isinstance(command_id, int) or isinstance(command_id, bool):
        raise ValueError("受管进程请求缺少整数 id")
    return request


def encode_response(command_id: int, **payload: Any) -> str:
    response: dict[str, Any] = {"id": int(command_id), "ok": bool(payload.pop("ok", True))}
    response.update(payload)
    return json.dumps(response, ensure_ascii=False)


def decode_response(line: str) -> dict:
    """父进程侧解码一行响应/事件。"""
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("受管进程响应必须是 JSON 对象")
    return message


def normalize_action_payload(payload: dict) -> dict:
    normalized = dict(payload)
    keys = normalized.get("keys")
    if isinstance(keys, list):
        normalized["keys"] = tuple(str(key) for key in keys)
    path = normalized.get("path")
    if isinstance(path, list):
        normalized["path"] = tuple(
            (int(point[0]), int(point[1])) for point in path
        )
    return normalized


def observation_payload(observation) -> dict:
    """ComputerObservation → 协议字段字典（只含 OBSERVATION_FIELDS）。"""
    return {field: getattr(observation, field) for field in OBSERVATION_FIELDS}


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_observation_payload(payload: dict) -> dict:
    """把 asdict(ComputerObservation) 的 JSON 形态还原为可构造 ComputerObservation 的形态。

    JSON 往返会把 elements 外层 tuple 变成 list；这里还原为 tuple（元素内部
    bounds 保持 list 形态，与桌面脱敏 dict 契约一致）。字段类型做防御：非
    dict 项跳过、缺失归一为空 tuple——元素不可用时回落截图是产品决定，
    绝不因载荷形态异常让 observe 失败。
    """
    normalized = dict(payload)
    elements = normalized.get("elements")
    listing: list[dict] = []
    if isinstance(elements, (list, tuple)):
        for item in elements:
            if not isinstance(item, dict):
                continue
            ref = item.get("ref")
            if not isinstance(ref, str) or not ref:
                continue
            kind = item.get("kind")
            name = item.get("name")
            bounds = item.get("bounds")
            if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
                normalized_bounds = [_safe_int(component) for component in bounds]
            else:
                normalized_bounds = [0, 0, 0, 0]
            enabled = item.get("enabled")
            listing.append({
                "ref": ref,
                "kind": kind if isinstance(kind, str) else "",
                "name": (name if isinstance(name, str) else "")[:_MAX_ELEMENT_NAME_CHARS],
                "bounds": normalized_bounds,
                "enabled": enabled if isinstance(enabled, bool) else True,
            })
    normalized["elements"] = tuple(listing)
    return normalized


FRAME_FIELDS = (
    "frame_id",
    "run_id",
    "target",
    "surface_id",
    "surface_generation",
    "viewport_generation",
    "origin_x",
    "origin_y",
    "crop_width",
    "crop_height",
    "image_width",
    "image_height",
    "model_to_css_x",
    "model_to_css_y",
    "coordinate_input_enabled",
    "disabled_reason",
)


def frame_payload(frame) -> dict:
    """ObservationFrame → 协议字段字典。"""
    return {field: getattr(frame, field) for field in FRAME_FIELDS}


def normalize_frame_payload(payload: dict) -> dict:
    """把 JSON 往返后的 frame payload 归一化为可构造 ObservationFrame 的字段。"""
    return {
        "frame_id": str(payload.get("frame_id") or ""),
        "run_id": str(payload.get("run_id") or ""),
        "target": str(payload.get("target") or ""),
        "surface_id": str(payload.get("surface_id") or ""),
        "surface_generation": _safe_int(payload.get("surface_generation")),
        "viewport_generation": _safe_int(payload.get("viewport_generation")),
        "origin_x": _safe_int(payload.get("origin_x")),
        "origin_y": _safe_int(payload.get("origin_y")),
        "crop_width": _safe_int(payload.get("crop_width")),
        "crop_height": _safe_int(payload.get("crop_height")),
        "image_width": _safe_int(payload.get("image_width")),
        "image_height": _safe_int(payload.get("image_height")),
        "model_to_css_x": float(payload.get("model_to_css_x") or 1.0),
        "model_to_css_y": float(payload.get("model_to_css_y") or 1.0),
        "coordinate_input_enabled": bool(payload.get("coordinate_input_enabled", True)),
        "disabled_reason": str(payload.get("disabled_reason") or ""),
    }
