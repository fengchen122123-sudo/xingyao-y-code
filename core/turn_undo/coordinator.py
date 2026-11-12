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
        # workspace_root -> set(turn_id)：正在 recording 的前台回合。
        # 同一目录允许多个回合并存：不同文件互不影响，目录级独占会把"同目录
        # 另一个会话干活"整片阻塞（实测一次长回合让整个工作区写不了文件）。
        # 互斥粒度由下面的 _active_paths 收到文件级。
        self._active_turns: dict[str, set[str]] = {}
        # (workspace_root, canonical_path) -> turn_id：正在写该文件的前台回合。
        # 只有真正同时写同一个文件才短暂互斥（prepare→verify 的写入窗口），
        # 同目录不同文件的回合可以并行。
        self._active_paths: dict[tuple[str, str], str] = {}
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
            # 并存登记：同目录多个回合各自入表，不再互相覆盖（覆盖式登记会让
            # 先到的回合被后到的踢出活跃位，反过来把自己判成"另一个回合"）
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
            # 多回合并存时给一个确定性代表（后台任务只需记一个父回合）
            return sorted(turns)[0]

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

    # -- 文件级写入占用（同一文件同一时刻只允许一个前台回合在写） --

    def claim_path(self, workspace_root: str, canonical_path: str,
                   turn_id: str) -> Optional[str]:
        """占用一个文件的写入窗口；已被别的回合占用时返回占用者 turn_id。

        冲突时不回填自己：调用方直接失败，等对方 verify 释放后重试即可。
        """
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
