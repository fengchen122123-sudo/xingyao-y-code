"""项目级权限白名单持久化存储。

「始终允许」产生的命令前缀规则与文件路径白名单按项目工作目录分组，
持久化到 ~/.ycode/permissions.json（YCODE_DATA_DIR 优先）。
重启与切换会话后自动恢复；沙盒外访问的会话级放行不在此存储（不落盘）。

读写复用 config 的 mtime 缓存与原子写（含 .bak 自愈），单项目条目设上限
防止长期累积膨胀。任何损坏都回落空规则，绝不影响启动与正常询问流程。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_STORE_LOCK = threading.Lock()

# 单项目条目上限：超出后不再累积（旧规则保留），避免文件无限膨胀
MAX_COMMAND_RULES_PER_PROJECT = 100
MAX_FILE_ENTRIES_PER_PROJECT = 500

_VERSION = 1


def _store_path() -> Path:
    try:
        import config

        return Path(config.YCODE_DIR) / "permissions.json"
    except Exception:
        return Path.home() / ".ycode" / "permissions.json"


def canonical_workdir(workdir=None) -> str:
    """规范化项目工作目录作为分组键（realpath + normcase，Windows 大小写不敏感）。"""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(str(workdir or os.getcwd()))))
    except (OSError, ValueError, TypeError):
        return ""


def canonical_file_path(path) -> str:
    """与 core.llm._canonical_permission_path 同语义的文件路径规范化。"""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(str(path or ""))))
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
        logger.warning("权限白名单写入失败: %s", exc)


def _project_bucket(data: dict, key: str) -> dict:
    projects = data["projects"]
    bucket = projects.get(key)
    if not isinstance(bucket, dict):
        bucket = {}
        projects[key] = bucket
    bucket.setdefault("commands", [])
    bucket.setdefault("write_files", [])
    bucket.setdefault("edit_files", [])
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


def _append_unique(items: list, value, cap: int) -> bool:
    """去重追加；超出上限时丢弃新条目并返回 False（已有条目始终保留）。"""
    if value in items:
        return True
    if len(items) >= cap:
        return False
    items.append(value)
    return True


def add_command_rule(rule, workdir=None) -> bool:
    """记录一条命令前缀规则（tuple/list[str]）。返回是否成功写入。"""
    key = canonical_workdir(workdir)
    if not key or not isinstance(rule, (tuple, list)) or not rule:
        return False
    stored = [str(token) for token in rule]
    with _STORE_LOCK:
        data = _load()
        bucket = _project_bucket(data, key)
        ok = _append_unique(bucket["commands"], stored, MAX_COMMAND_RULES_PER_PROJECT)
        if ok:
            _save(data)
        return ok


def _add_file(field: str, path, workdir=None) -> bool:
    key = canonical_workdir(workdir)
    canonical = canonical_file_path(path)
    if not key or not canonical:
        return False
    with _STORE_LOCK:
        data = _load()
        bucket = _project_bucket(data, key)
        ok = _append_unique(bucket[field], canonical, MAX_FILE_ENTRIES_PER_PROJECT)
        if ok:
            _save(data)
        return ok


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
        _save(data)
        return removed
