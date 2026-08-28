"""项目级权限白名单持久化存储。

用户手动添加或审批产生的命令前缀规则与文件路径白名单按项目工作目录分组，
持久化到 ~/.ycode/permissions.json（YCODE_DATA_DIR 优先）。
重启与切换任务后自动恢复；沙盒外访问的任务级放行不在此存储（不落盘）。

读写复用 config 的 mtime 缓存与原子写（含 .bak 自愈），单项目条目设上限
防止长期累积膨胀。任何损坏都回落空规则，绝不影响启动与正常询问流程。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from core.command_policy import parse_manual_command_rule
from core.workspace_context import get_workdir

_STORE_LOCK = threading.Lock()

# 单项目条目上限：超出后不再累积（旧规则保留），避免文件无限膨胀
MAX_COMMAND_RULES_PER_PROJECT = 100
MAX_FILE_ENTRIES_PER_PROJECT = 500

_VERSION = 1


class PermissionStoreError(RuntimeError):
    """Raised when a requested whitelist mutation cannot be persisted."""


@dataclass(frozen=True)
class RuleMutationResult:
    status: str
    rule_type: str
    normalized_value: str


def _store_path() -> Path:
    try:
        import config

        return Path(config.YCODE_DIR) / "permissions.json"
    except Exception:
        return Path.home() / ".ycode" / "permissions.json"


def canonical_workdir(workdir=None) -> str:
    """规范化项目工作目录作为分组键（realpath + normcase，Windows 大小写不敏感）。"""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(str(workdir or str(get_workdir())))))
    except (OSError, ValueError, TypeError):
        return ""


def canonical_file_path(path) -> str:
    """与 core.llm._canonical_permission_path 同语义的文件路径规范化。

    相对路径按线程本地 workdir 解析（进程 cwd 在 GUI 多会话并行下不可靠），
    绝对路径与 realpath+normcase 行为一致。
    """
    try:
        from core.tools import _resolve_against_workdir

        return os.path.normcase(str(_resolve_against_workdir(path)))
    except (OSError, ValueError, TypeError):
        return ""


def _load() -> dict:
    import config

    try:
        data = config._read_json_with_backup(_store_path(), default=None)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return {"version": _VERSION, "projects": {}}
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    return {"version": _VERSION, "projects": projects}


def _save(data: dict) -> None:
    import config

    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        config._atomic_write_json(path, data)
    except Exception as exc:
        raise PermissionStoreError(f"权限白名单写入失败: {exc}") from exc


def _persist(data: dict) -> None:
    try:
        _save(data)
    except PermissionStoreError:
        raise
    except Exception as exc:
        raise PermissionStoreError(f"权限白名单写入失败: {exc}") from exc


def _project_bucket(data: dict, key: str) -> dict:
    projects = data["projects"]
    bucket = projects.get(key)
    if not isinstance(bucket, dict):
        bucket = {}
        projects[key] = bucket
    for field in ("commands", "write_files", "edit_files"):
        if not isinstance(bucket.get(field), list):
            bucket[field] = []
    return bucket


def project_rules(workdir=None) -> dict:
    """返回指定项目（默认当前工作目录）的持久化白名单。

    返回 {"commands": set[tuple[str, ...]], "write_files": set[str], "edit_files": set[str]}，
    任何字段的损坏条目都会被静默剔除。
    """
    key = canonical_workdir(workdir)
    with _STORE_LOCK:
        data = _load()
    bucket = data["projects"].get(key) if key else None
    if not isinstance(bucket, dict):
        return {"commands": set(), "write_files": set(), "edit_files": set()}
    commands = set()
    for rule in bucket.get("commands", []):
        if isinstance(rule, list) and rule and all(isinstance(t, str) for t in rule):
            commands.add(tuple(rule))
    return {
        "commands": commands,
        "write_files": {str(p) for p in bucket.get("write_files", []) if isinstance(p, str)},
        "edit_files": {str(p) for p in bucket.get("edit_files", []) if isinstance(p, str)},
    }


def _add_stored_rule(
    *,
    key: str,
    field: str,
    stored_value,
    cap: int,
) -> str:
    with _STORE_LOCK:
        data = _load()
        bucket = _project_bucket(data, key)
        if stored_value in bucket[field]:
            return "exists"
        if len(bucket[field]) >= cap:
            return "limit_reached"
        bucket[field].append(stored_value)
        _persist(data)
        return "added"


def _canonical_file_for_workdir(path, workdir=None) -> str:
    try:
        raw_path = os.fspath(path)
    except TypeError:
        return ""
    if workdir is not None and not os.path.isabs(raw_path):
        raw_path = os.path.join(os.fspath(workdir), raw_path)
    return canonical_file_path(raw_path)


def _normalized_rule(rule_type: str, value, workdir=None):
    normalized_type = str(rule_type or "").strip().lower()
    if normalized_type == "command":
        tokens = parse_manual_command_rule(str(value or ""))
        return normalized_type, "commands", list(tokens), " ".join(tokens)
    if normalized_type in {"write", "edit"}:
        canonical = _canonical_file_for_workdir(value, workdir)
        if not canonical:
            raise ValueError("文件白名单路径无效")
        field = "write_files" if normalized_type == "write" else "edit_files"
        return normalized_type, field, canonical, canonical
    raise ValueError("规则类型必须是 command、write 或 edit")


def add_rule(rule_type: str, value, workdir=None) -> RuleMutationResult:
    """Add one manually entered command-prefix or exact-file rule."""
    key = canonical_workdir(workdir)
    if not key:
        raise ValueError("项目工作目录无效")
    normalized_type, field, stored_value, display_value = _normalized_rule(
        rule_type,
        value,
        workdir,
    )
    cap = (
        MAX_COMMAND_RULES_PER_PROJECT
        if field == "commands"
        else MAX_FILE_ENTRIES_PER_PROJECT
    )
    status = _add_stored_rule(
        key=key,
        field=field,
        stored_value=stored_value,
        cap=cap,
    )
    return RuleMutationResult(status, normalized_type, display_value)


def remove_rule(rule_type: str, value, workdir=None) -> RuleMutationResult:
    """Remove one exact persisted rule from the selected project."""
    key = canonical_workdir(workdir)
    if not key:
        raise ValueError("项目工作目录无效")
    normalized_type, field, stored_value, display_value = _normalized_rule(
        rule_type,
        value,
        workdir,
    )
    with _STORE_LOCK:
        data = _load()
        bucket = data["projects"].get(key)
        if not isinstance(bucket, dict) or not isinstance(bucket.get(field), list):
            status = "not_found"
        elif stored_value not in bucket[field]:
            status = "not_found"
        else:
            bucket[field].remove(stored_value)
            if not any(bucket.get(name) for name in ("commands", "write_files", "edit_files")):
                data["projects"].pop(key, None)
            _persist(data)
            status = "removed"
    return RuleMutationResult(status, normalized_type, display_value)


def add_command_rule(rule, workdir=None) -> bool:
    """记录一条命令前缀规则（tuple/list[str]）。返回是否成功写入。"""
    key = canonical_workdir(workdir)
    if not key or not isinstance(rule, (tuple, list)) or not rule:
        return False
    stored = [str(token) for token in rule]
    status = _add_stored_rule(
        key=key,
        field="commands",
        stored_value=stored,
        cap=MAX_COMMAND_RULES_PER_PROJECT,
    )
    return status in {"added", "exists"}


def _add_file(field: str, path, workdir=None) -> bool:
    key = canonical_workdir(workdir)
    canonical = _canonical_file_for_workdir(path, workdir)
    if not key or not canonical:
        return False
    status = _add_stored_rule(
        key=key,
        field=field,
        stored_value=canonical,
        cap=MAX_FILE_ENTRIES_PER_PROJECT,
    )
    return status in {"added", "exists"}


def add_write_file(path, workdir=None) -> bool:
    """记录一条写入文件白名单（规范化绝对路径）。"""
    return _add_file("write_files", path, workdir)


def add_edit_file(path, workdir=None) -> bool:
    """记录一条修改文件白名单（规范化绝对路径）。"""
    return _add_file("edit_files", path, workdir)


def clear_project(workdir=None) -> int:
    """清空指定项目的持久化白名单，返回清除的条目总数。"""
    key = canonical_workdir(workdir)
    if not key:
        return 0
    with _STORE_LOCK:
        data = _load()
        bucket = data["projects"].pop(key, None)
        if not isinstance(bucket, dict):
            return 0
        removed = (
            len(bucket.get("commands", []))
            + len(bucket.get("write_files", []))
            + len(bucket.get("edit_files", []))
        )
        _persist(data)
        return removed
