"""星瑶按回合撤销：规范化工作区互斥协调器。

规格 21 的锁序：
  获取规范化工作区独占协调锁 -> 锁内查询活跃回合/进程状态 -> 短数据库事务
  -> 文件 I/O -> 短数据库事务推进状态 -> 释放工作区锁 -> 发送 UI/CLI 事件。

铁律：
- 数据库连接锁内禁止等待工作区锁（先拿工作区锁，再进短事务）。
- 受控工具的 prepare -> 实际写入 -> verify 完整区间与撤销共用同一把锁。
- 锁身份来自规范化真实路径（中间目录符号链接、大小写、UNC 别名都归一），
  避免同一目录获得多把锁；不同工作区允许并行。
"""

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
    """当前线程的规范化工作区根（受控工具与撤销共同的锁键，规格 21）。

    回合 worker / 子代理线程都绑定了线程本地 workdir；回合外调用兜底进程 cwd。
    所有受控文件修改必须以此为锁键——撤销事务锁的也是同一把键，保证
    prepare→写入→verify 与撤销应用全区间互斥（子目录文件也不例外）。
    """
    from core.workspace_context import get_workdir

    return canonicalize_workspace_root(get_workdir() or os.getcwd())


class WorkspaceCoordinator:
    """按规范化工作区根互斥；同根多会话共享，异根并行。"""

    def __init__(self):
        self._registry_lock = threading.Lock()
        self._workspace_locks: dict[str, threading.RLock] = {}
        # workspace_root -> turn_id：正在 recording 的前台回合（参与写入互斥）
        self._active_turns: dict[str, str] = {}
        # workspace_root -> set(turn_id)：正在 recording 的后台任务检查点。
        # 与前台分开登记：后台任务按设计不参与写入互斥（recorder 依 owner_kind
        # 直接放行），但它确实在写文件，撤销/恢复必须仍能看见并拒绝并发。
        self._background_turns: dict[str, set[str]] = {}
        # workspace_root -> operation_id：进行中的撤销事务
        self._active_undos: dict[str, str] = {}
        # workspace_root -> set(frozen canonical path)（中断事务冻结路径）
        self._frozen_paths: dict[str, set[str]] = {}

    def _lock_for(self, workspace_root: str) -> threading.RLock:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            lock = self._workspace_locks.get(root)
            if lock is None:
                lock = threading.RLock()
                self._workspace_locks[root] = lock
            return lock

    # -- 锁 --

    @contextmanager
    def workspace_lock(self, workspace_root: str):
        """工作区互斥：受控修改、撤销、恢复共用。"""
        lock = self._lock_for(workspace_root)
        with lock:
            yield

    # -- 活跃回合登记（须持有工作区锁调用） --

    def register_active_turn(self, workspace_root: str, turn_id: str) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            self._active_turns[root] = turn_id

    def unregister_active_turn(self, workspace_root: str, turn_id: str) -> None:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            if self._active_turns.get(root) == turn_id:
                self._active_turns.pop(root, None)

    def active_turn_id(self, workspace_root: str) -> Optional[str]:
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            return self._active_turns.get(root)

    # -- 后台任务登记（不参与写入互斥，仅用于撤销/恢复的并发判定） --

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
        """该工作区是否有回合（前台或后台任务）正在写文件。

        撤销/恢复用这个判断，而不是只看前台登记：后台任务虽然不参与前台
        互斥，但它此刻确实可能在写文件，撤销必须避开。
        """
        root = canonicalize_workspace_root(workspace_root)
        with self._registry_lock:
            return bool(self._active_turns.get(root) or self._background_turns.get(root))

    # -- 撤销事务登记（须持有工作区锁调用） --

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

    # -- 中断冻结路径（规格 21：规范化路径包含关系，不只字符串相等） --

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
        """目标路径或其父/子包含关系被冻结时返回 True。

        判定使用规范化路径包含关系：修改冻结文件本身、删除/移动其父目录、
        把目录覆盖到其上级位置等操作全部拒绝。
        """
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
                    # 目标在冻结目录之下（父目录被冻结）
                    return True
                if item.startswith(path + os.sep):
                    # 目标是冻结文件的祖先目录（删除/移动父目录会影响它）
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
