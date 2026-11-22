from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Optional


def canonicalize_workspace_root(path: str | os.PathLike) -> str:
    """工作区根的锁身份：完整 realpath（含最终目录组件）+ 平台大小写规则。"""
    value = os.fspath(path)
    absolute = os.path.abspath(value)
    return os.path.normcase(os.path.realpath(absolute))


def thread_workspace_root() -> str:
    from core.workspace_context import get_workdir

    return canonicalize_workspace_root(get_workdir() or os.getcwd())


class WorkspaceCoordinator:
    """按规范化工作区根互斥；同根多会话共享，异根并行。"""

    def __init__(self):
        self._registry_lock = threading.Lock()
        self._workspace_locks: dict[str, threading.RLock] = {}
        self._active_turns: dict[str, set[str]] = {}
        self._active_paths: dict[tuple[str, str], str] = {}
        # workspace_root -> set(turn_id)：正在 recording 的后台任务检查点。
        # 与前台分开登记：后台任务按设计不参与写入互斥（recorder 依 owner_kind
        # 直接放行），但它确实在写文件，撤销/恢复必须仍能看见并拒绝并发。
        self._background_turns: dict[str, set[str]] = {}
        self._active_undos: dict[str, str] = {}
        self._frozen_paths: dict[str, set[str]] = {}

    def _lock_for(self, workspace_root: str) -> threading.RLock:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            lock = self._workspace_locks.get(root)
            if lock is None:
                lock = threading.RLock()
                self._workspace_locks[root] = lock
            return lock

    @contextmanager
    def workspace_lock(self, workspace_root: str):
        """工作区互斥：受控修改、撤销、恢复共用。"""
        lock = self._lock_for(workspace_root)
        with lock:
            yield

    def register_active_turn(self, workspace_root: str, turn_id: str) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            self._active_turns.setdefault(root, set()).add(turn_id)

    def unregister_active_turn(self, workspace_root: str, turn_id: str) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            turns = self._active_turns.get(root)
            if not turns:
                return
            turns.discard(turn_id)
            if not turns:
                self._active_turns.pop(root, None)

    def active_turn_id(self, workspace_root: str) -> Optional[str]:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            turns = self._active_turns.get(root)
            if not turns:
                return None
            return sorted(turns)[0]

    def register_background_turn(self, workspace_root: str, turn_id: str) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            self._background_turns.setdefault(root, set()).add(turn_id)

    def unregister_background_turn(self, workspace_root: str, turn_id: str) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            turns = self._background_turns.get(root)
            if not turns:
                return
            turns.discard(turn_id)
            if not turns:
                self._background_turns.pop(root, None)

    def has_active_turn(self, workspace_root: str) -> bool:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            return bool(self._active_turns.get(root) or self._background_turns.get(root))

    def claim_path(self, workspace_root: str, canonical_path: str,
                   turn_id: str) -> Optional[str]:
        root = canonicalize_workspace_root(workspace_root)
        key = (root, canonicalize_workspace_root(canonical_path))
        with self._registry_lock:
            owner = self._active_paths.get(key)
            if owner and owner != turn_id:
                return owner
            self._active_paths[key] = turn_id
            return None

    def release_path(self, workspace_root: str, canonical_path: str,
                     turn_id: Optional[str] = None) -> None:
        """释放文件写入占用；turn_id 为空时无条件释放（写入失败回滚路径用）。"""
        root = canonicalize_workspace_root(workspace_root)
        key = (root, canonicalize_workspace_root(canonical_path))
        with self._registry_lock:
            owner = self._active_paths.get(key)
            if owner is None:
                return
            if turn_id is None or owner == turn_id:
                self._active_paths.pop(key, None)

    def release_turn_paths(self, workspace_root: str, turn_id: str) -> None:
        """回合封闭兜底：清掉该回合遗留的全部文件占用，防异常路径泄漏。"""
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            for key in [k for k, owner in self._active_paths.items()
                        if k[0] == root and owner == turn_id]:
                self._active_paths.pop(key, None)

    def register_undo(self, workspace_root: str, operation_id: str) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            self._active_undos[root] = operation_id

    def unregister_undo(self, workspace_root: str, operation_id: str) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            if self._active_undos.get(root) == operation_id:
                self._active_undos.pop(root, None)

    def active_undo_id(self, workspace_root: str) -> Optional[str]:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            return self._active_undos.get(root)

    def freeze_paths(self, workspace_root: str, canonical_paths) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            frozen = self._frozen_paths.setdefault(root, set())
            frozen.update(canonical_paths)

    def unfreeze_paths(self, workspace_root: str, canonical_paths) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            frozen = self._frozen_paths.get(root)
            if not frozen:
                return
            frozen.difference_update(canonical_paths)
            if not frozen:
                self._frozen_paths.pop(root, None)

    def frozen_paths(self, workspace_root: str) -> frozenset[str]:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            return frozenset(self._frozen_paths.get(root, ()))

    def is_path_frozen(self, workspace_root: str, canonical_path: str) -> bool:
        root = canonicalize_workspace_root(workspace_root)
        path = canonicalize_workspace_root(canonical_path)
        with self._registry_lock:
            frozen = self._frozen_paths.get(root)
            if not frozen:
                return False
            for item in frozen:
                if path == item:
                    return True
                if path.startswith(item + os.sep):
                    return True
                if item.startswith(path + os.sep):
                    return True
            return False

    def is_path_frozen_any(self, canonical_path: str) -> bool:
        """跨全部工作区检查路径冻结（文件工具的触盘前防线）。"""
        path = canonicalize_workspace_root(canonical_path)
        with self._registry_lock:
            all_frozen = [frozen for frozen in self._frozen_paths.values() if frozen]
        for frozen in all_frozen:
            for item in frozen:
                if path == item or path.startswith(item + os.sep) \
                        or item.startswith(path + os.sep):
                    return True
        return False

    def any_frozen_paths(self, workspace_root: str) -> bool:
        """该工作区是否存在中断冻结（通用终端命令拒绝判定用）。"""
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            return bool(self._frozen_paths.get(root))


_coordinator = WorkspaceCoordinator()


def get_coordinator() -> WorkspaceCoordinator:
    return _coordinator
