"""core/trace_export.py — 审计/排障轨迹导出（T5，借鉴 Codex rollout-trace）。

把 harness journal（turns/steps/events 的 ID 与元数据）、`/doctor` 聚合自检
结果与 GUI 运行日志尾部打成一个 zip 诊断包，供报障与审计。

设计约束（实施计划与验收清单 · T5）：
- 默认"精简模式"不含任何消息正文；`full=True` 追加会话消息导出，且必须
  配合显式确认参数（confirm=True），调用方（CLI）还须完成交互确认；
  消息正文统一经 `core.share._redact_export_text` 脱敏；
- harness 记录遵循 Harness 纪律：只含 ID 与元数据（sha256 / 计数 / 时长等），
  不落消息正文；
- 单项缺失/失败（无日志文件、无 MemoryManager、doctor 抛异常）优雅降级：
  仍产出 zip，并把原因记入 manifest.errors；
- 不引入第三方依赖（仅标准库 + 项目既有模块）。

包结构：
    manifest.json   版本/平台/时间范围/会话/文件清单/降级记录
    journal.jsonl   harness turns/steps/events 逐行 JSON（含回合 ID）
    doctor.json     core.doctor.run_checks 的结果（数组，schema 同 --json）
    messages.jsonl  仅 full 模式：经脱敏的会话消息（role/content）
    logs/           GUI 运行日志尾部（gui-runtime.log 存在才打包）
"""
from __future__ import annotations

import json
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

from core.version import __version__ as YCODE_VERSION

# GUI 运行日志只打包尾部，避免把全部历史塞进诊断包（日志本身按 2MB x 3 滚动）
LOG_TAIL_BYTES = 512 * 1024

# manifest 的格式版本：字段发生不兼容调整时递增
BUNDLE_FORMAT = "ycode-diagnostics"
BUNDLE_FORMAT_VERSION = 1


def _gui_runtime_log_path() -> Path:
    """GUI 运行日志路径（跟随 YCODE_DATA_DIR 覆盖，与既有日志约定一致）。"""
    try:
        import config

        base = Path(config.YCODE_DIR)
    except Exception:
        base = Path.home() / ".ycode"
    return base / "logs" / "gui-runtime.log"


def _safe_fragment(value: str) -> str:
    """把会话名等自由文本收敛为可放进文件名的片段。"""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value or ""))


def _jsonl(rows: list[dict]) -> str:
    if not rows:
        return ""
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"


def _collect_journal(session_name: str) -> tuple[list[dict], str]:
    """收集 harness journal 记录；失败时返回 (空列表, 错误说明)。"""
    from core import app_context

    manager = app_context.get_manager()
    if manager is None:
        return [], "journal 未导出：运行时上下文未注册 MemoryManager"
    try:
        return list(manager.export_harness_journal(session_name=session_name)), ""
    except Exception as exc:
        return [], f"journal 导出失败：{exc.__class__.__name__}: {exc}"


def _collect_doctor() -> tuple[list[dict], str]:
    """收集 doctor 检查结果（数组 schema 同 /doctor --json）；失败隔离。"""
    from core.doctor import run_checks

    try:
        results = run_checks()
    except Exception as exc:
        return [], f"doctor 检查失败：{exc.__class__.__name__}: {exc}"
    rows = []
    for result in results:
        try:
            rows.append(result.to_dict())
        except Exception:
            rows.append(
                {
                    "name": str(getattr(result, "name", "")),
                    "status": "fail",
                    "detail": "结果序列化失败",
                    "hint": "",
                }
            )
    return rows, ""


def _collect_messages(session_name: str) -> tuple[list[dict], str]:
    """收集会话消息（full 模式），逐条经 _redact_export_text 脱敏。"""
    from core import app_context
    from core.share import _redact_export_text

    if not session_name:
        return [], "messages 未导出：未指定会话名"
    manager = app_context.get_manager()
    if manager is None:
        return [], "messages 未导出：运行时上下文未注册 MemoryManager"
    try:
        history = manager.get_history(session_name)
    except Exception as exc:
        return [], f"messages 导出失败：{exc.__class__.__name__}: {exc}"
    rows = []
    for message in history or []:
        if str(message.get("role") or "") == "system":
            continue
        rows.append(
            {
                "role": str(message.get("role") or ""),
                "content": _redact_export_text(message.get("content") or ""),
            }
        )
    return rows, ""


def _platform_info() -> dict:
    import platform

    return {
        "system": sys.platform,
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def _write_log_tail(zip_file: zipfile.ZipFile, log_path: Path) -> None:
    """写入日志尾部：超过上限时截到 LOG_TAIL_BYTES 并去掉截断处的半行。"""
    size = log_path.stat().st_size
    with open(log_path, "rb") as handle:
        if size > LOG_TAIL_BYTES:
            handle.seek(size - LOG_TAIL_BYTES)
            data = handle.read()
            newline = data.find(b"\n")
            if newline != -1:
                data = data[newline + 1:]
        else:
            data = handle.read()
    zip_file.writestr("logs/gui-runtime.log", data)


def export_diagnostics_bundle(
    *,
    dest_dir: str,
    session_name: str = "",
    full: bool = False,
    confirm: bool = False,
) -> str:
    """导出一份诊断包 zip，返回 zip 路径。

    参数：
    - dest_dir：包输出目录（不存在时创建）。
    - session_name：会话名；指定时 journal 只含该会话的回合轨迹，
      为空时导出全部会话的 harness 轨迹。
    - full：True 时追加 messages.jsonl（消息正文，经脱敏）；默认精简模式。
    - confirm：full=True 必须显式确认（调用方完成交互确认后传入），
      否则抛 ValueError —— 防止误把会话正文写进诊断包。
    """
    if full and not confirm:
        raise ValueError(
            "full=True 需要显式确认（confirm=True）：诊断包将包含会话消息正文"
        )

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    journal_rows, journal_error = _collect_journal(session_name)
    doctor_rows, doctor_error = _collect_doctor()

    log_path = _gui_runtime_log_path()
    log_included = log_path.is_file()

    messages_rows: list[dict] = []
    messages_error = ""
    if full:
        messages_rows, messages_error = _collect_messages(session_name)

    turn_rows = [row for row in journal_rows if row.get("type") == "turn"]
    started_values = [
        int(row["started_at_ms"]) for row in turn_rows
        if row.get("started_at_ms") is not None
    ]
    finished_values = [
        int(row["finished_at_ms"]) for row in turn_rows
        if row.get("finished_at_ms") is not None
    ]
    time_range = {
        "started_at_ms": min(started_values) if started_values else None,
        "ended_at_ms": max(finished_values) if finished_values else None,
    }

    files = ["manifest.json", "journal.jsonl", "doctor.json"]
    if full and messages_rows:
        files.append("messages.jsonl")
    if log_included:
        files.append("logs/gui-runtime.log")

    errors = [item for item in (journal_error, doctor_error, messages_error) if item]

    manifest = {
        "bundle_format": BUNDLE_FORMAT,
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "ycode_version": YCODE_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "platform": _platform_info(),
        "mode": "full" if full else "slim",
        "session_name": str(session_name or ""),
        "redacted": True,
        "time_range": time_range,
        "counts": {
            "turns": len(turn_rows),
            "steps": sum(1 for row in journal_rows if row.get("type") == "step"),
            "events": sum(1 for row in journal_rows if row.get("type") == "event"),
            "messages": len(messages_rows),
        },
        "files": files,
        "errors": errors,
        "notes": [
            "journal.jsonl 仅含回合/步进/事件的 ID 与元数据，不含消息正文。",
            "full 模式下 messages.jsonl 已按 _redact_export_text 脱敏，"
            "仍建议分享前人工复核。",
        ],
    }

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    parts = ["ycode-diagnostics"]
    safe_session = _safe_fragment(session_name)
    if safe_session:
        parts.append(safe_session)
    parts.append(timestamp)
    zip_path = dest / f"{'-'.join(parts)}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        bundle.writestr("journal.jsonl", _jsonl(journal_rows))
        bundle.writestr("doctor.json", json.dumps(doctor_rows, ensure_ascii=False, indent=2))
        if full and messages_rows:
            bundle.writestr("messages.jsonl", _jsonl(messages_rows))
        if log_included:
            try:
                _write_log_tail(bundle, log_path)
            except Exception as exc:
                manifest["errors"].append(
                    f"logs 打包失败：{exc.__class__.__name__}: {exc}"
                )
                bundle.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )

    return str(zip_path)