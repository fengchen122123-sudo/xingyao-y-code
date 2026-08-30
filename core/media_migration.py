"""core/media_migration.py
=========================
历史消息媒体清单一次性迁移（幂等，DB 层）。

设计见 docs/superpowers/specs/2026-08-30-media-display-design.md §4.7：
- 门闩：config 的 ``media_v1_migration_done``；整体成功后置位。
- 候选：JSON dict 形态、无 media_v1 的 assistant 消息（memory 层已过滤）。
- 每条消息正文文本字节不动，只补写 ``_display.media_v1``；
  归档失败的路径生成 kind=error 清单项，正文不展示降级。
- 仓库不可写等基础设施错误：中止本轮且不置门闩，下次启动自动重试
  （已更新的消息天然跳过，重跑安全）。
- 相对路径按消息所属会话的 workdir 解析；GLOBAL 会话回退用户主目录。
"""

from __future__ import annotations

import logging
import os

from config import save_config_value
from core.media_refs import build_media_manifest
from core.media_store import MediaStoreError
from core.model_manager import load_config

logger = logging.getLogger(__name__)

_MIGRATION_FLAG = "media_v1_migration_done"


def _fast_text_prefilter(content: str) -> bool:
    """返回 True 表示正文中不可能存在媒体语法（直接跳过，无需解析）。"""
    return (
        "![" not in content
        and "video" not in content.lower()
        and "视频" not in content
    )


def run_media_migration_once(manager) -> dict:
    """执行迁移并返回统计；调用方（GUI 启动后台线程）忽略返回值即可。"""
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    if isinstance(cfg, dict) and cfg.get(_MIGRATION_FLAG):
        return {"skipped": True}

    stats = {"scanned": 0, "updated": 0, "failed": 0, "abort_error": None}
    try:
        rows = manager.iter_assistant_media_migration_rows()
    except Exception:
        logger.warning("媒体清单迁移读取候选行失败", exc_info=True)
        stats["abort_error"] = "db_read_failed"
        return stats

    for msg_id, session_workdir, parsed in rows:
        content = parsed.get("content") or ""
        stats["scanned"] += 1
        if _fast_text_prefilter(content):
            continue
        workdir = session_workdir if session_workdir not in ("", "GLOBAL", None) \
            else os.path.expanduser("~")
        try:
            manifest = build_media_manifest(content, workdir)
        except MediaStoreError as exc:
            # 基础设施错误：中止且不置门闩，下次启动重试（已更新行天然跳过）
            logger.warning(
                "媒体清单迁移因仓库错误中止: %s (message_id=%s)", exc.error_code, msg_id
            )
            stats["failed"] += 1
            stats["abort_error"] = exc.error_code
            break
        if manifest is None:
            continue
        try:
            if manager.update_message_display_media(msg_id, manifest):
                stats["updated"] += 1
        except Exception:
            logger.warning("媒体清单迁移写回失败 (message_id=%s)", msg_id, exc_info=True)
            stats["failed"] += 1
            stats["abort_error"] = "db_write_failed"
            break

    if stats["abort_error"] is None:
        try:
            save_config_value(_MIGRATION_FLAG, True)
        except Exception:
            logger.warning("媒体清单迁移完成但门闩写盘失败，下次启动将重跑（幂等）", exc_info=True)
    logger.info("媒体清单迁移结束: %s", stats)
    return stats
