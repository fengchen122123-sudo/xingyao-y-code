from __future__ import annotations

import json
from typing import Optional


_RECENT_FILE_LIMIT = 6
_MAX_BLOCKS = 8


def build_workspace_correction_section(workspace_root) -> str:
    try:
        from core.turn_undo import coordinator as coordinator_mod
        from core.turn_undo import service as turn_undo_service

        if not turn_undo_service.has_default_storage():
            return ""
        root = coordinator_mod.canonicalize_workspace_root(workspace_root)
        storage = turn_undo_service.get_default_storage()
        corrections = storage.list_corrections(root, limit=_MAX_BLOCKS)
        interrupted = storage.list_checkpoints_by_status(("interrupted",))
        interrupted = [
            cp for cp in interrupted if cp.workspace_root == root
        ]
    except Exception:
        return ""

    if not corrections and not interrupted:
        return ""

    lines: list[str] = ["<workspace_correction_ledger>"]
    for cp in interrupted:
        lines.append(
            f"警告：回合 {cp.turn_id[:8]} 的撤销事务处于中断状态，"
            "工作区可能处于部分恢复状态；受影响文件修改被暂时冻结，"
            "请等待用户完成恢复（继续撤销或取消撤销），不要猜测磁盘状态。"
        )
    for item in corrections:
        try:
            files = json.loads(item.get("files_summary") or "[]")
        except (TypeError, ValueError):
            files = []
        if not isinstance(files, list):
            files = []
        turn_id = str(item.get("turn_id") or "")
        short_id = turn_id[:8] if turn_id else "?"
        created = int(item.get("created_at_ms") or 0)
        if len(files) <= _RECENT_FILE_LIMIT:
            file_text = "、".join(str(f) for f in files) or "（无文件明细）"
        else:
            file_text = (
                "、".join(str(f) for f in files[:_RECENT_FILE_LIMIT])
                + f" 等 {len(files)} 个文件"
            )
        lines.append(
            f"回合 {short_id}（完整 ID：{turn_id}）的文件修改已由用户撤销"
            f"（{created}）。影响文件：{file_text}。"
        )
        if item.get("has_uncovered_command_effects"):
            lines.append("该回合执行过通用终端命令，命令的其他副作用没有随文件恢复。")
    lines.append(
        "以上回合的历史消息仍表示曾执行过操作，但其文件结果已失效；"
        "继续相关工作前必须重新读取磁盘文件，不要盲信旧 diff。"
    )
    lines.append("</workspace_correction_ledger>")
    return "\n".join(lines)
