"""In-memory permission grants isolated by stable task/session identifiers."""

from __future__ import annotations

import threading
from collections.abc import Iterator, MutableSet

from core import app_context
from core.command_policy import command_matches_session_rule, session_rule_for_command


_DEFAULT_TASK_ID = "__default__"
_GRANTS_LOCK = threading.Lock()
_TASK_GRANTS: dict[str, dict[str, set]] = {}


def current_task_id(task_id: str | None = None) -> str:
    if task_id is not None and str(task_id).strip():
        return str(task_id).strip()
    bound = app_context.get_thread_session()
    if bound:
        return str(bound)
    visible = app_context.get_current_session()
    return str(visible) if visible else _DEFAULT_TASK_ID


def _new_bucket() -> dict[str, set]:
    return {
        "commands": set(),
        "exact_commands": set(),
        "write_files": set(),
        "edit_files": set(),
    }


def _bucket_locked(task_id: str) -> dict[str, set]:
    bucket = _TASK_GRANTS.get(task_id)
    if bucket is None:
        bucket = _new_bucket()
        _TASK_GRANTS[task_id] = bucket
    return bucket


def grant_command(command: str, task_id: str | None = None) -> None:
    normalized = str(command or "").strip()
    if not normalized:
        raise ValueError("授权命令不能为空")
    rule = session_rule_for_command(normalized)
    with _GRANTS_LOCK:
        bucket = _bucket_locked(current_task_id(task_id))
        if rule is None:
            bucket["exact_commands"].add(normalized)
        else:
            bucket["commands"].add(rule)


def grant_command_rule(rule: tuple[str, ...], task_id: str | None = None) -> None:
    if not isinstance(rule, tuple) or not rule or not all(
        isinstance(token, str) and token for token in rule
    ):
        raise ValueError("命令授权规则无效")
    with _GRANTS_LOCK:
        _bucket_locked(current_task_id(task_id))["commands"].add(rule)


def grant_exact_command(command: str, task_id: str | None = None) -> None:
    normalized = str(command or "").strip()
    if not normalized:
        raise ValueError("授权命令不能为空")
    with _GRANTS_LOCK:
        _bucket_locked(current_task_id(task_id))["exact_commands"].add(normalized)


def grant_file(kind: str, path: str, task_id: str | None = None) -> None:
    field = _file_field(kind)
    normalized = str(path or "").strip()
    if not normalized:
        raise ValueError("授权文件路径不能为空")
    with _GRANTS_LOCK:
        _bucket_locked(current_task_id(task_id))[field].add(normalized)


def matches_command(command: str, task_id: str | None = None) -> bool:
    normalized = str(command or "").strip()
    if not normalized:
        return False
    with _GRANTS_LOCK:
        bucket = _TASK_GRANTS.get(current_task_id(task_id))
        if bucket is None:
            return False
        return normalized in bucket["exact_commands"] or any(
            command_matches_session_rule(normalized, rule)
            for rule in bucket["commands"]
        )


def matches_exact_command(command: str, task_id: str | None = None) -> bool:
    normalized = str(command or "").strip()
    if not normalized:
        return False
    with _GRANTS_LOCK:
        bucket = _TASK_GRANTS.get(current_task_id(task_id))
        return bucket is not None and normalized in bucket["exact_commands"]


def matches_file(kind: str, path: str, task_id: str | None = None) -> bool:
    field = _file_field(kind)
    normalized = str(path or "").strip()
    if not normalized:
        return False
    with _GRANTS_LOCK:
        bucket = _TASK_GRANTS.get(current_task_id(task_id))
        return bucket is not None and normalized in bucket[field]


def snapshot(task_id: str | None = None) -> dict[str, set]:
    with _GRANTS_LOCK:
        bucket = _TASK_GRANTS.get(current_task_id(task_id))
        if bucket is None:
            return _new_bucket()
        return {name: set(values) for name, values in bucket.items()}


def clear_task(task_id: str) -> None:
    normalized = str(task_id or "").strip()
    if not normalized:
        raise ValueError("任务 ID 不能为空")
    with _GRANTS_LOCK:
        _TASK_GRANTS.pop(normalized, None)


def rename_task(old_task_id: str, new_task_id: str) -> None:
    old_key = str(old_task_id or "").strip()
    new_key = str(new_task_id or "").strip()
    if not old_key or not new_key:
        raise ValueError("任务 ID 不能为空")
    if old_key == new_key:
        return
    with _GRANTS_LOCK:
        if new_key in _TASK_GRANTS and any(_TASK_GRANTS[new_key].values()):
            raise ValueError(f"目标任务 {new_key} 已有授权，不能覆盖")
        bucket = _TASK_GRANTS.pop(old_key, None)
        if bucket is not None:
            _TASK_GRANTS[new_key] = bucket


def _file_field(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized == "write":
        return "write_files"
    if normalized == "edit":
        return "edit_files"
    raise ValueError("文件授权类型必须是 write 或 edit")


class TaskGrantSetView(MutableSet):
    """Compatibility view that routes set operations to the current task."""

    def __init__(self, field: str) -> None:
        if field not in _new_bucket():
            raise ValueError(f"未知授权字段: {field}")
        self._field = field

    def __contains__(self, value) -> bool:
        with _GRANTS_LOCK:
            bucket = _TASK_GRANTS.get(current_task_id())
            return bucket is not None and value in bucket[self._field]

    def __iter__(self) -> Iterator:
        with _GRANTS_LOCK:
            values = set(_bucket_locked(current_task_id())[self._field])
        return iter(values)

    def __len__(self) -> int:
        with _GRANTS_LOCK:
            bucket = _TASK_GRANTS.get(current_task_id())
            return len(bucket[self._field]) if bucket is not None else 0

    def add(self, value) -> None:
        with _GRANTS_LOCK:
            _bucket_locked(current_task_id())[self._field].add(value)

    def discard(self, value) -> None:
        with _GRANTS_LOCK:
            bucket = _TASK_GRANTS.get(current_task_id())
            if bucket is not None:
                bucket[self._field].discard(value)

    def clear(self) -> None:
        with _GRANTS_LOCK:
            bucket = _TASK_GRANTS.get(current_task_id())
            if bucket is not None:
                bucket[self._field].clear()

    def __eq__(self, other) -> bool:
        if isinstance(other, (set, frozenset, TaskGrantSetView)):
            return set(self) == set(other)
        return NotImplemented


def task_set_view(field: str) -> TaskGrantSetView:
    return TaskGrantSetView(field)


def _clear_all_for_tests() -> None:
    with _GRANTS_LOCK:
        _TASK_GRANTS.clear()
