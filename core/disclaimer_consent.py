"""免责条款同意记录持久化。

授权链路的最后一环：首次使用（以及条款版本升级后的首次使用）必须由
用户显式确认，确认结果落到 ~/.ycode/disclaimer_consent.json
（YCODE_DATA_DIR 优先）。同一条款版本只确认一次，之后不再打扰。

设计约束：
- 读取失败一律回落"未同意"，绝不影响进程启动（磁盘异常不该让软件打不开）；
- 记录必须包含条款版本与同意时间，用于事后举证；
- 写盘走原子写，避免中断产生半截文件；
- 版本号同时在前端 frontend/src/data/disclaimer.ts 维护，改条款正文时
  必须两处同步递增，否则升级后的用户不会重新看到确认弹窗。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

_STORE_LOCK = threading.Lock()

_STORE_VERSION = 1

# 当前生效的条款版本。条款正文有实质修改时递增（如 v1.1），
# 已同意旧版本的用户会在下次启动时重新看到确认弹窗。
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


def _defaults() -> dict[str, Any]:
    """未同意（或缺记录）时的快照：accepted=False 会触发确认弹窗。"""
    return {
        "version": _STORE_VERSION,
        "disclaimer_version": DISCLAIMER_VERSION,
        "disclaimer_updated_at": DISCLAIMER_UPDATED_AT,
        "accepted": False,
        "accepted_version": "",
        "accepted_at": "",
    }


def get_disclaimer_consent() -> dict[str, Any]:
    """读取同意状态快照；任何读取异常回落默认值（未同意）。

    只认「accepted_version 非空」这一判据：手改文件写个 accepted=true
    但没有版本号，不视为有效同意。
    """
    try:
        import config

        data = config._read_json_with_backup(_store_path(), default=None)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return _defaults()
    accepted_version = data.get("accepted_version")
    accepted_at = data.get("accepted_at")
    accepted = isinstance(accepted_version, str) and bool(accepted_version.strip())
    return {
        "version": _STORE_VERSION,
        "disclaimer_version": DISCLAIMER_VERSION,
        "disclaimer_updated_at": DISCLAIMER_UPDATED_AT,
        "accepted": accepted,
        "accepted_version": accepted_version.strip() if accepted else "",
        "accepted_at": accepted_at.strip() if isinstance(accepted_at, str) else "",
    }


def needs_acceptance() -> bool:
    """当前条款版本是否尚未被接受（覆盖首次使用与条款升级两种情况）。"""
    return str(get_disclaimer_consent().get("accepted_version") or "") != DISCLAIMER_VERSION


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
    }
    with _STORE_LOCK:
        try:
            import config

            config._atomic_write_json(_store_path(), payload, indent=2)
        except Exception as exc:  # pragma: no cover - 磁盘故障等极端情况
            raise DisclaimerConsentError(f"保存失败：{exc}") from exc
    return dict(payload)
