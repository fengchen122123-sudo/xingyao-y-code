"""core/tools_admin_session.py
Agent 工具 session_manage：会话生命周期与回合检查点管理。

覆盖能力：
- 会话：列出、新建、改名、删除、归档/取消归档、迁移工作目录、清空历史；
- 记忆压缩：触发会话记忆摘要压缩（复用 memory_compressor 的既有入口）；
- 检查点 / 按回合撤销：列出检查点、回滚某个回合的文件修改、列出撤销记录、
  预检某回合撤销影响、对中断的撤销操作做恢复。

设计约束：
- 全部复用 core 层既有公开 API（MemoryManager / memory_compressor /
  turn_undo.service），绝不 import gui_bridge / main / pywebview；
- 不实现权限档位、沙箱、白名单、信任确认类动作（这些属于权限系统职责）；
- 输出统一为 JSON 字符串（ensure_ascii=False）：成功 {"ok": true, ...}，
  失败 {"ok": false, "error": "中文原因"}；任何异常都在本层捕获，不向外抛；
- 找不到可靠 core 层入口的能力一律显式报错，不做假实现。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from core.tool_protocol import ToolSpec

# 参数白名单：调度层可能注入内部键（cancel_event 等），转发前先过滤，
# 避免把未知键传给固定签名的 session_manage 造成 TypeError。
_KNOWN_ARGUMENTS = frozenset({
    "action", "session", "new_name", "workdir", "turn_id",
    "operation_id", "output_path",
})

_ACTIONS = (
    "list",
    "create",
    "rename",
    "delete",
    "archive",
    "unarchive",
    "move_workdir",
    "clear_history",
    "compress",
    "checkpoint_list",
    "checkpoint_restore",
    "undo_list",
    "undo_preview",
    "undo_recover",
)

# 会话列表最多返回条数（列表只需可读概览，不追求全量倾倒）
_LIST_LIMIT = 200
# 检查点 / 撤销记录最多返回条数
_CHECKPOINT_LIMIT = 100
# 压缩过程提示最多回传条数
_COMPRESS_NOTICE_LIMIT = 5


# ---------------------------------------------------------------------------
# 统一 JSON 输出
# ---------------------------------------------------------------------------
def _ok(**payload: Any) -> str:
    result: dict[str, Any] = {"ok": True}
    result.update(payload)
    return json.dumps(result, ensure_ascii=False)


def _fail(error: str, **payload: Any) -> str:
    result: dict[str, Any] = {"ok": False, "error": str(error)}
    result.update(payload)
    return json.dumps(result, ensure_ascii=False)


def _jsonable(value: Any) -> Any:
    """把 dataclass / Enum / 元组 等结构转成可直接 json.dumps 的普通对象。"""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _enum_value(value: Any) -> str:
    """取枚举的字符串值；非枚举按 str 返回。"""
    return str(getattr(value, "value", value))


def _strip_markup(text: str) -> str:
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


# ---------------------------------------------------------------------------
# core 层依赖获取（全部延迟导入，避免导入期副作用）
# ---------------------------------------------------------------------------
def _manager():
    """返回进程级 MemoryManager；未初始化时显式失败。"""
    from core import app_context

    manager = app_context.get_manager()
    if manager is None:
        raise RuntimeError("会话存储尚未初始化（MemoryManager 未注册）")
    return manager


def _require_session(session: str) -> str:
    """取目标会话名；参数为空时回落到当前会话。"""
    name = str(session or "").strip()
    if name:
        return name
    name = ""
    try:
        from core import app_context

        name = str(app_context.get_thread_session() or "").strip()
        if not name:
            name = str(app_context.get_current_session() or "").strip()
    except Exception:
        name = ""
    if not name:
        raise ValueError("缺少 session 参数，且当前没有可用的会话上下文")
    return name


def _session_records(manager) -> list[dict]:
    """全量会话记录（含归档、含空会话），供存在性判断与列表展示。"""
    return manager.get_all_sessions(include_archived=True, include_empty=True)


def _session_exists(manager, name: str) -> bool:
    return any(str(row.get("name") or "") == name for row in _session_records(manager))


def _require_existing_session(manager, name: str) -> None:
    if not _session_exists(manager, name):
        raise LookupError(f"会话不存在: {name}")


def _auto_session_name(manager) -> str:
    """自动命名：与既有 GUI/CLI 约定一致，生成 session_<6位十六进制>。"""
    existing = {str(row.get("name") or "") for row in _session_records(manager)}
    for _ in range(50):
        candidate = f"session_{uuid.uuid4().hex[:6]}"
        if candidate not in existing:
            return candidate
    raise RuntimeError("无法生成唯一的会话名")


def _session_id(manager, name: str) -> int:
    """会话名 → 不可变主键 sessions.id（turn_undo 入口专用）。"""
    from core.turn_undo import service as turn_undo_service

    _ensure_turn_undo_ready()
    return int(turn_undo_service.resolve_session_id(name))


def _ensure_turn_undo_ready() -> None:
    from core.turn_undo import service as turn_undo_service

    if not turn_undo_service.has_default_storage():
        raise RuntimeError("按回合撤销存储尚未配置，检查点/撤销能力当前不可用")


def _turn_undo():
    from core.turn_undo import service as turn_undo_service

    _ensure_turn_undo_ready()
    return turn_undo_service


# ---------------------------------------------------------------------------
# 检查点 / 撤销条目摘要
# ---------------------------------------------------------------------------
def _checkpoint_item(state: dict) -> dict:
    turn_id = str(state.get("turn_id") or "")
    description = _strip_markup(state.get("description") or "")
    return {
        "turn_id": turn_id,
        "short_id": turn_id[:8],
        "status": str(state.get("status") or ""),
        "owner_kind": str(state.get("owner_kind") or ""),
        "file_count": int(state.get("file_count") or 0),
        "net_added": int(state.get("net_added") or 0),
        "net_removed": int(state.get("net_removed") or 0),
        "description": description[:120],
        "started_at_ms": state.get("started_at_ms"),
        "completed_at_ms": state.get("completed_at_ms"),
        "reverted_at_ms": state.get("reverted_at_ms"),
        "snapshot_availability": str(state.get("snapshot_availability") or ""),
        "has_uncovered_command_effects": bool(state.get("has_uncovered_command_effects")),
    }


def _list_checkpoints(manager, name: str) -> dict:
    session_id = _session_id(manager, name)
    states = _turn_undo().list_turn_undo_states(session_id, limit=_CHECKPOINT_LIMIT)
    items = [_checkpoint_item(state) for state in states]
    return {"session": name, "session_id": session_id, "count": len(items), "items": items}


def _preview_payload(preview: Any) -> dict:
    """TurnUndoPreview → 可读摘要（绝不回传确认令牌）。"""
    payload = _jsonable(preview)
    if isinstance(payload, dict):
        payload.pop("preview_token", None)
        conflicts = payload.get("conflicts") or []
        payload["conflict_count"] = len(conflicts) if isinstance(conflicts, list) else 0
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# 各 action 实现
# ---------------------------------------------------------------------------
def _action_list() -> str:
    manager = _manager()
    rows = _session_records(manager)
    rows = sorted(rows, key=lambda row: row.get("updated_at") or "", reverse=True)
    items = [
        {
            "name": str(row.get("name") or ""),
            "messages": int(row.get("count") or 0),
            "workdir": row.get("workdir") or "",
            "archived": bool(row.get("archived_at")),
            "source": str(row.get("source") or ""),
            "updated_at": row.get("updated_at") or "",
        }
        for row in rows[:_LIST_LIMIT]
    ]
    return _ok(count=len(items), total=len(rows), truncated=len(rows) > len(items),
               sessions=items)


def _action_create(manager, session: str, workdir: str) -> str:
    name = str(session or "").strip()
    auto_named = False
    if not name:
        name = _auto_session_name(manager)
        auto_named = True
    elif _session_exists(manager, name):
        return _fail(f"同名会话已存在: {name}")
    target_workdir = str(workdir or "").strip()
    manager.create_session_explicit(
        name,
        workdir=(target_workdir or None),
        source="GUI",
    )
    record = next(
        (row for row in _session_records(manager) if str(row.get("name") or "") == name),
        None,
    )
    return _ok(
        session=name,
        created=True,
        auto_named=auto_named,
        workdir=(record or {}).get("workdir") or "",
    )


def _action_rename(manager, session: str, new_name: str) -> str:
    old_name = _require_session(session)
    target = str(new_name or "").strip()
    if not target:
        return _fail("rename 需要 new_name（新会话名不能为空）")
    _require_existing_session(manager, old_name)
    renamed, final_name = manager.rename_session(old_name, target)
    if not renamed:
        return _fail(f"重命名失败: {old_name} → {target}")
    payload: dict[str, Any] = {"session": old_name, "new_name": final_name}
    if final_name != target:
        payload["notice"] = f"目标名称已被占用，已自动改为 {final_name}"
    return _ok(**payload)


def _action_delete(manager, session: str) -> str:
    name = _require_session(session)
    _require_existing_session(manager, name)
    manager.delete_session(name)
    return _ok(session=name, deleted=True)


def _action_archive(manager, session: str) -> str:
    name = _require_session(session)
    _require_existing_session(manager, name)
    if not manager.archive_session(name):
        return _fail(f"归档失败: {name}")
    return _ok(session=name, archived=True)


def _action_unarchive(manager, session: str) -> str:
    name = _require_session(session)
    _require_existing_session(manager, name)
    if not manager.unarchive_session(name):
        return _fail(f"取消归档失败: {name}")
    return _ok(session=name, archived=False)


def _action_move_workdir(manager, session: str, workdir: str) -> str:
    name = _require_session(session)
    target = str(workdir or "").strip()
    if not target:
        return _fail("move_workdir 需要 workdir（目标工作目录，或 GLOBAL 表示自由对话分组）")
    _require_existing_session(manager, name)
    if not manager.update_session_workdir(name, target):
        return _fail(f"迁移工作目录失败: {name} → {target}")
    record = next(
        (row for row in _session_records(manager) if str(row.get("name") or "") == name),
        None,
    )
    return _ok(session=name, workdir=(record or {}).get("workdir") or target)


def _action_clear_history(manager, session: str) -> str:
    name = _require_session(session)
    _require_existing_session(manager, name)
    deleted = manager.clear_session_messages(name)
    return _ok(session=name, deleted_messages=int(deleted or 0))


def _action_compress(manager, session: str) -> str:
    name = _require_session(session)
    _require_existing_session(manager, name)

    from core.memory_compressor import compress_session_memory, is_session_compressing

    if is_session_compressing(manager, name):
        return _ok(session=name, status="skipped", reason="该会话已有记忆压缩在执行中")

    import core.model_manager as model_manager

    active = model_manager.get_active_model(name) or {}
    model_name = str(active.get("litellm_model") or "").strip()
    if not model_name:
        return _fail("未配置可用的激活模型，无法执行记忆压缩")

    notices: list[str] = []

    def _collect(message: str) -> None:
        text = _strip_markup(message)
        if text:
            notices.append(text)

    outcome = compress_session_memory(
        manager,
        name,
        model_name,
        str(active.get("api_key") or ""),
        str(active.get("base_url") or ""),
        force=False,
        output_callback=_collect,
        return_outcome=True,
    )
    status = str(getattr(outcome, "status", "") or "")
    reason = str(getattr(outcome, "reason", "") or "")
    tail = notices[-_COMPRESS_NOTICE_LIMIT:]

    if status == "compressed":
        return _ok(session=name, status=status, reason=reason, notices=tail)
    if status == "skipped":
        return _ok(session=name, status=status,
                   reason=reason or "当前会话无需压缩（活跃对话过少或已有压缩在执行）",
                   notices=tail)
    return _fail(
        f"记忆压缩未完成（{reason or status or '未知原因'}）",
        session=name, status=status or "failed", notices=tail,
    )


def _action_checkpoint_list(manager, session: str) -> str:
    name = _require_session(session)
    return _ok(**_list_checkpoints(manager, name))


def _resolve_turn_for_restore(manager, name: str, turn_id: str, operation_id: str) -> str:
    """checkpoint_restore 的回合解析：优先 turn_id，其次由 operation_id 反查所属回合。"""
    session_id = _session_id(manager, name)
    turn = str(turn_id or "").strip()
    operation = str(operation_id or "").strip()
    if turn:
        return _turn_undo().resolve_turn_id_in_session(session_id, turn)
    if not operation:
        raise ValueError("checkpoint_restore 需要 turn_id 或 operation_id")
    storage = _turn_undo().get_default_storage()
    op_row = storage.get_operation(operation)
    checkpoint = storage.get_checkpoint(op_row.checkpoint_id)
    if int(checkpoint.session_id) != int(session_id):
        raise LookupError("该撤销操作不属于指定会话")
    return str(checkpoint.turn_id)


def _serialize_revert(result: Any) -> dict:
    payload = _jsonable(result)
    return payload if isinstance(payload, dict) else {"raw": payload}


def _action_checkpoint_restore(manager, session: str, turn_id: str, operation_id: str) -> str:
    name = _require_session(session)
    _require_existing_session(manager, name)
    service = _turn_undo()
    session_id = _session_id(manager, name)
    resolved = _resolve_turn_for_restore(manager, name, turn_id, operation_id)

    preview, token = service.preview_turn_undo(session_id, resolved)
    preview_payload = _preview_payload(preview)
    if _enum_value(getattr(preview, "status", "")) == "conflicted":
        return _fail(
            "该回合存在文件冲突，未执行回滚",
            session=name, turn_id=resolved, preview=preview_payload,
        )
    if not token:
        return _fail(
            "预检未签发回滚令牌，未执行回滚",
            session=name, turn_id=resolved, preview=preview_payload,
        )

    try:
        result = service.execute_turn_undo(session_id, resolved, token)
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "")
        if code == "revert_interrupted":
            operation = ""
            try:
                status = service.get_turn_undo_status(session_id, resolved) or {}
                operation = str(status.get("last_operation_id") or "")
            except Exception:
                operation = ""
            return _fail(
                f"回滚事务中断：{exc}",
                session=name, turn_id=resolved, code=code,
                hint=(
                    f"使用 undo_recover 并传 operation_id={operation} 继续或取消该次撤销"
                    if operation else "请使用 undo_recover 处理中断的撤销事务"
                ),
            )
        return _fail(f"回滚失败：{exc}", session=name, turn_id=resolved, code=code)

    return _ok(session=name, turn_id=resolved,
               preview=preview_payload, result=_serialize_revert(result))


def _action_undo_list(manager, session: str) -> str:
    name = _require_session(session)
    data = _list_checkpoints(manager, name)
    return _ok(
        session=data["session"],
        session_id=data["session_id"],
        count=data["count"],
        turns=data["items"],
        notice="每条记录对应一轮对话的文件改动快照；status=ready 的回合可撤销",
    )


def _action_undo_preview(manager, session: str, turn_id: str) -> str:
    name = _require_session(session)
    _require_existing_session(manager, name)
    turn = str(turn_id or "").strip()
    if not turn:
        return _fail("undo_preview 需要 turn_id（可传短 ID 前缀）")
    service = _turn_undo()
    session_id = _session_id(manager, name)
    resolved = service.resolve_turn_id_in_session(session_id, turn)
    preview, _token = service.preview_turn_undo(session_id, resolved)
    return _ok(session=name, turn_id=resolved, preview=_preview_payload(preview))


def _action_undo_recover(manager, session: str, operation_id: str) -> str:
    name = _require_session(session)
    _require_existing_session(manager, name)
    operation = str(operation_id or "").strip()
    if not operation:
        return _fail("undo_recover 需要 operation_id（中断撤销操作的 ID）")
    service = _turn_undo()
    session_id = _session_id(manager, name)
    action = "continue_revert"
    preview, token = service.preview_turn_undo_recovery(session_id, operation, action)
    preview_payload = _jsonable(preview)
    if not token:
        return _fail(
            "恢复预检未签发令牌，未执行恢复",
            session=name, operation_id=operation, action=action, preview=preview_payload,
        )
    result = service.execute_turn_undo_recovery(session_id, operation, action, token)
    return _ok(session=name, operation_id=operation, action=action,
               preview=preview_payload, result=_serialize_revert(result))


# ---------------------------------------------------------------------------
# 工具入口
# ---------------------------------------------------------------------------
def session_manage(action: str, session: str = "", new_name: str = "",
                   workdir: str = "", turn_id: str = "", operation_id: str = "",
                   output_path: str = "") -> str:
    """会话生命周期管理工具（action 大小写不敏感）。

    output_path 为协议预留参数，当前所有 action 均不使用。
    """
    try:
        key = str(action or "").strip().lower()
        if not key:
            return _fail("缺少 action 参数，可选：" + "/".join(_ACTIONS))
        if key not in _ACTIONS:
            return _fail(f"不支持的 action: {action}，可选：" + "/".join(_ACTIONS))

        if key == "list":
            return _action_list()

        manager = _manager()

        if key == "create":
            return _action_create(manager, session, workdir)
        if key == "rename":
            return _action_rename(manager, session, new_name)
        if key == "delete":
            return _action_delete(manager, session)
        if key == "archive":
            return _action_archive(manager, session)
        if key == "unarchive":
            return _action_unarchive(manager, session)
        if key == "move_workdir":
            return _action_move_workdir(manager, session, workdir)
        if key == "clear_history":
            return _action_clear_history(manager, session)
        if key == "compress":
            return _action_compress(manager, session)
        if key == "checkpoint_list":
            return _action_checkpoint_list(manager, session)
        if key == "checkpoint_restore":
            return _action_checkpoint_restore(manager, session, turn_id, operation_id)
        if key == "undo_list":
            return _action_undo_list(manager, session)
        if key == "undo_preview":
            return _action_undo_preview(manager, session, turn_id)
        if key == "undo_recover":
            return _action_undo_recover(manager, session, operation_id)

        return _fail(f"不支持的 action: {action}，可选：" + "/".join(_ACTIONS))
    except ValueError as exc:
        return _fail(str(exc) or "参数校验失败")
    except LookupError as exc:
        return _fail(str(exc) or "目标不存在")
    except Exception as exc:  # 统一兜底：绝不把异常抛给调度层
        # TurnUndoError 等带错误码的领域异常：直接回传原始中文原因与错误码，
        # 便于模型判断是"未找到/已撤销/冲突"还是真正的执行故障。
        code = str(getattr(exc, "code", "") or "")
        if code:
            return _fail(str(exc) or "回合撤销操作失败", code=code)
        return _fail(f"session_manage 执行失败: {exc}")


def _execute(**arguments: Any) -> str:
    """ToolSpec 执行入口：过滤调度层注入的内部键后转调 session_manage。"""
    clean = {name: value for name, value in arguments.items() if name in _KNOWN_ARGUMENTS}
    return session_manage(**clean)


_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": (
                "操作类型：list 列出全部会话；create 新建会话；rename 重命名；delete 删除；"
                "archive/unarchive 归档与取消归档；move_workdir 迁移会话所属工作目录；"
                "clear_history 清空会话消息（保留会话本身）；compress 触发会话记忆压缩；"
                "checkpoint_list 列出会话检查点；checkpoint_restore 回滚某回合的文件修改；"
                "undo_list 列出按回合撤销记录；undo_preview 预检某回合撤销影响；"
                "undo_recover 对中断的撤销操作做恢复（继续撤销）"
            ),
        },
        "session": {
            "type": "string",
            "description": "会话名称；create 时可留空由系统自动命名，其余动作留空表示当前会话",
        },
        "new_name": {"type": "string", "description": "新会话名（rename 必填，名称冲突时自动追加数字后缀）"},
        "workdir": {
            "type": "string",
            "description": (
                "工作目录：create 时为新建会话归属目录（留空用当前工作目录）；"
                "move_workdir 时为目标目录或 GLOBAL（自由对话分组）"
            ),
        },
        "turn_id": {
            "type": "string",
            "description": "回合 ID（checkpoint_restore/undo_preview 使用，可传唯一短 ID 前缀）",
        },
        "operation_id": {
            "type": "string",
            "description": "撤销操作 ID（undo_recover 必填；checkpoint_restore 可用它反查所属回合）",
        },
        "output_path": {"type": "string", "description": "预留参数，当前所有 action 均不使用"},
    },
    "required": ["action"],
}


def build_specs() -> list[ToolSpec]:
    """返回 session_manage 的工具规格（单条）。"""
    return [
        ToolSpec(
            name="session_manage",
            description=(
                "管理与运维会话（本地会话库）：列出会话、新建/重命名/删除会话、"
                "归档与取消归档、迁移会话工作目录、清空会话历史、触发会话记忆压缩，"
                "以及查看会话检查点与按回合撤销记录、回滚某回合的文件修改、"
                "对中断的撤销操作做恢复。删除与清空不可恢复，执行前请确认目标会话正确。"
            ),
            input_schema=_INPUT_SCHEMA,
            execute=_execute,
            read_only=False,
            mutates_files=False,
            requires_permission=True,
            side_effect_scope="system",
            activity_description="管理会话",
        )
    ]
