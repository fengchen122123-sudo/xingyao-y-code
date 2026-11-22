from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

_STORE_LOCK = threading.Lock()

_STORE_VERSION = 1

DISCLAIMER_VERSION = "v1.0"
DISCLAIMER_UPDATED_AT = "2026-09-11"


class DisclaimerConsentError(ValueError):
    """Raised when the consent record fails to persist."""


def _store_path():
    try:
        import config

        return (
            config.YCODE_DIR / "disclaimer_consent.json"
            if hasattr(config.YCODE_DIR, "__truediv__")
            else config.YCODE_DIR + "/disclaimer_consent.json"
        )
    except Exception:
        import os
        from pathlib import Path

        data_dir = os.environ.get("YCODE_DATA_DIR") or os.path.join(str(Path.home()), ".ycode")
        return os.path.join(data_dir, "disclaimer_consent.json")


def is_disclaimer_version_accepted(
    accepted_version: Any,
    required_version: str = DISCLAIMER_VERSION,
) -> bool:
    if not isinstance(accepted_version, str):
        return False
    acc = accepted_version.strip()
    req = required_version.strip()
    if not acc or not req:
        return False
    if acc == req:
        return True
    acc_norm = acc[1:] if acc.startswith(("v", "V")) else acc
    req_norm = req[1:] if req.startswith(("v", "V")) else req
    if acc_norm == req_norm:
        return True
    try:
        acc_parts = [int(p) for p in acc_norm.split(".")]
        req_parts = [int(p) for p in req_norm.split(".")]
        return acc_parts >= req_parts
    except (ValueError, AttributeError):
        return acc == req


def _defaults() -> dict[str, Any]:
    return {
        "version": _STORE_VERSION,
        "disclaimer_version": DISCLAIMER_VERSION,
        "disclaimer_updated_at": DISCLAIMER_UPDATED_AT,
        "accepted": False,
        "accepted_version": "",
        "accepted_at": "",
        "needs_acceptance": True,
    }


def get_disclaimer_consent() -> dict[str, Any]:
    try:
        import config

        data = config._read_json_with_backup(_store_path(), default=None)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return _defaults()
    accepted_version = data.get("accepted_version")
    accepted_at = data.get("accepted_at")
    accepted = is_disclaimer_version_accepted(accepted_version, DISCLAIMER_VERSION)
    needs_accept = not accepted
    return {
        "version": _STORE_VERSION,
        "disclaimer_version": DISCLAIMER_VERSION,
        "disclaimer_updated_at": DISCLAIMER_UPDATED_AT,
        "accepted": accepted,
        "accepted_version": accepted_version.strip() if isinstance(accepted_version, str) else "",
        "accepted_at": accepted_at.strip() if isinstance(accepted_at, str) else "",
        "needs_acceptance": needs_accept,
    }


def needs_acceptance() -> bool:
    """当前条款版本是否尚未被接受（覆盖首次使用与条款升级两种情况）。"""
    return bool(get_disclaimer_consent().get("needs_acceptance", True))


def accept_disclaimer() -> dict[str, Any]:
    """记录用户已接受当前条款版本，返回写入后的完整快照。

    版本号以服务端 DISCLAIMER_VERSION 为准，不接受调用方传入——
    否则前端缓存了旧版本号时，会把"已同意旧条款"记成当前版本，
    让条款升级后的再次确认失效。
    """
    accepted_at = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {
        "version": _STORE_VERSION,
        "disclaimer_version": DISCLAIMER_VERSION,
        "disclaimer_updated_at": DISCLAIMER_UPDATED_AT,
        "accepted": True,
        "accepted_version": DISCLAIMER_VERSION,
        "accepted_at": accepted_at,
        "needs_acceptance": False,
    }
    with _STORE_LOCK:
        try:
            import config

            config._atomic_write_json(_store_path(), payload, indent=2)
        except Exception as exc:  # pragma: no cover - 磁盘故障等极端情况
            raise DisclaimerConsentError(f"保存失败：{exc}") from exc
    return dict(payload)
