"""Shared observation visual-frame identity and reverse-transform synchronization."""

from __future__ import annotations

from dataclasses import replace
import io
import struct
from typing import Callable

from core.computer.models import ComputerControlError, ObservationFrame

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_DISABLED_MESSAGES = {
    "too_large": (
        "当前观察截图体积超限已被省略且无可用坐标映射，已禁用坐标输入；"
        "请改用元素引用或缩小窗口/视口后重新 observe"
    ),
    "transform_unknown": (
        "当前缩放观察截图尺寸/逆变换未知，已禁用坐标输入；"
        "请重新 observe 后再操作"
    ),
    "no_screenshot": (
        "当前观察未取到有效截图，已禁用坐标输入；"
        "请改用元素引用或恢复窗口可见后重新 observe"
    ),
}


def png_dimensions(png_bytes: bytes) -> tuple[int, int] | None:
    """Read (width, height) from real PNG bytes without trusting stale caller metadata."""
    if (
        isinstance(png_bytes, (bytes, bytearray))
        and len(png_bytes) >= 24
        and bytes(png_bytes[:8]) == _PNG_SIGNATURE
        and bytes(png_bytes[12:16]) == b"IHDR"
    ):
        width, height = struct.unpack(">II", png_bytes[16:24])
        if width > 0 and height > 0:
            return int(width), int(height)
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png_bytes)) as image:
            width, height = image.size
            if width > 0 and height > 0:
                return int(width), int(height)
    except Exception:
        return None
    return None


def frame_from_observation(
    observation,
    *,
    run_id: str = "",
    coordinate_input_enabled: bool | None = None,
    disabled_reason: str = "",
) -> ObservationFrame:
    """Build an immutable ObservationFrame bound to an observation's viewport/crop."""
    image_w = int(getattr(observation, "width", 0) or 0)
    image_h = int(getattr(observation, "height", 0) or 0)
    crop_w = int(getattr(observation, "original_width", 0) or 0) or image_w
    crop_h = int(getattr(observation, "original_height", 0) or 0) or image_h
    scale_x = float(getattr(observation, "scale_x", 0.0) or 0.0)
    scale_y = float(getattr(observation, "scale_y", 0.0) or 0.0)
    if scale_x <= 0 and image_w > 0 and crop_w > 0:
        scale_x = crop_w / image_w
    if scale_y <= 0 and image_h > 0 and crop_h > 0:
        scale_y = crop_h / image_h
    if coordinate_input_enabled is None:
        has_png = bool(str(getattr(observation, "png_path", "") or ""))
        enabled = bool(has_png and image_w > 0 and image_h > 0 and crop_w > 0 and crop_h > 0)
        reason = disabled_reason or ("" if enabled else "no_screenshot")
    else:
        enabled = bool(coordinate_input_enabled)
        reason = disabled_reason
    return ObservationFrame(
        frame_id=str(getattr(observation, "frame_id", "") or ""),
        run_id=str(run_id or getattr(observation, "run_id", "") or ""),
        target=str(getattr(observation, "target", "") or ""),
        surface_id=str(getattr(observation, "surface_id", "") or ""),
        surface_generation=int(getattr(observation, "surface_generation", 0) or 0),
        viewport_generation=int(getattr(observation, "viewport_generation", 0) or 0),
        origin_x=int(getattr(observation, "origin_x", 0) or 0),
        origin_y=int(getattr(observation, "origin_y", 0) or 0),
        crop_width=crop_w,
        crop_height=crop_h,
        image_width=image_w,
        image_height=image_h,
        model_to_css_x=scale_x if scale_x > 0 else 1.0,
        model_to_css_y=scale_y if scale_y > 0 else 1.0,
        coordinate_input_enabled=enabled,
        disabled_reason=reason,
    )


def coordinate_disabled_error(reason: str) -> ComputerControlError:
    """Return consistent domain error when coordinate input is disabled for a frame."""
    message = _DISABLED_MESSAGES.get(
        str(reason or ""),
        "当前视觉帧未启用坐标输入，请改用元素引用或重新 observe",
    )
    return ComputerControlError("computer_invalid_action", message)


def _sync_controller_frame(controller, run_id: str, frame: ObservationFrame) -> None:
    if controller is None or not frame.frame_id:
        return
    sync = getattr(controller, "sync_observation_frame", None)
    if callable(sync):
        sync(str(run_id or frame.run_id), frame)


def sync_frame_disabled(
    observation,
    *,
    reason: str,
    controller=None,
    run_id: str = "",
) -> ObservationFrame:
    """Record a disabled coordinate-input state for the observation frame."""
    resolved_run_id = str(run_id or getattr(observation, "run_id", "") or "")
    frame = frame_from_observation(
        observation,
        run_id=resolved_run_id,
        coordinate_input_enabled=False,
        disabled_reason=reason,
    )
    _sync_controller_frame(controller, resolved_run_id, frame)
    return frame


def prepare_observation_image_frame(
    png_bytes: bytes,
    observation,
    *,
    max_bytes: int,
    downscale_fn: Callable[[bytes], bytes | None],
    controller=None,
    run_id: str = "",
) -> tuple[bytes | None, str, ObservationFrame]:
    """Ensure sent image size, omitted state, and coordinate reverse transform match."""
    resolved_run_id = str(run_id or getattr(observation, "run_id", "") or "")
    base_frame = frame_from_observation(observation, run_id=resolved_run_id)
    downscaled = False
    if len(png_bytes) > max_bytes:
        scaled = downscale_fn(png_bytes)
        if scaled:
            png_bytes = scaled
            downscaled = True
    if len(png_bytes) > max_bytes:
        updated = replace(
            base_frame,
            coordinate_input_enabled=False,
            disabled_reason="too_large",
        )
        _sync_controller_frame(controller, resolved_run_id, updated)
        return None, "too_large", updated
    if downscaled:
        dims = png_dimensions(png_bytes)
        if dims is None or base_frame.crop_width <= 0 or base_frame.crop_height <= 0:
            updated = replace(
                base_frame,
                coordinate_input_enabled=False,
                disabled_reason="transform_unknown",
            )
            _sync_controller_frame(controller, resolved_run_id, updated)
            return png_bytes, "", updated
        new_w, new_h = dims
        updated = replace(
            base_frame,
            image_width=new_w,
            image_height=new_h,
            model_to_css_x=base_frame.crop_width / new_w,
            model_to_css_y=base_frame.crop_height / new_h,
            coordinate_input_enabled=True,
            disabled_reason="",
        )
        _sync_controller_frame(controller, resolved_run_id, updated)
        return png_bytes, "", updated
    return png_bytes, "", base_frame


def _has_media_blocks(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get("type") in ("image", "image_url")
        for item in content
    )


def find_retained_media_index(
    messages: list[dict] | tuple[dict, ...],
    *,
    active_computer_run_id: str | None = None,
) -> int:
    """Return index of the single user image message to keep unsquashed, or -1.

    Rules:
    1. If the latest user message already contains an image, keep it.
    2. If the latest user message is text-only inside an active computer control
       run (e.g. ``screen_unchanged`` or a text-only nudge while the run is
       active), keep scanning backward strictly within that same ``run_id`` to
       preserve the active frame image unless the newest frame itself had
       ``image_omitted`` in ``("no_screenshot", "too_large")``.
    3. For ordinary chat (no active computer run tail), stop at the trailing
       text-only user message so old chat images are folded normally.
    """
    seen_user = False
    run_scope = str(active_computer_run_id or "") or None
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        has_media = _has_media_blocks(content)
        meta = msg.get("_computer_v1")
        meta_dict = meta if isinstance(meta, dict) else None
        if not seen_user:
            seen_user = True
            if has_media:
                return idx
            if meta_dict is not None:
                omitted = str(meta_dict.get("image_omitted") or "")
                if omitted in ("too_large", "no_screenshot"):
                    return -1
                msg_run_id = str(meta_dict.get("run_id") or "")
                if msg_run_id:
                    run_scope = msg_run_id
                    continue
                if run_scope is not None:
                    continue
                return -1
            if run_scope is not None:
                continue
            return -1
        if meta_dict is not None:
            msg_run_id = str(meta_dict.get("run_id") or "")
            if msg_run_id and run_scope and msg_run_id != run_scope:
                return -1
            omitted = str(meta_dict.get("image_omitted") or "")
            if has_media:
                return idx
            if omitted in ("too_large", "no_screenshot"):
                return -1
            continue
        return -1
    return -1
