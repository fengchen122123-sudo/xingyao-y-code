"""GitStateMonitor：外部 Git 状态同步（规格 §10）。

- 按 Worktree 建立监控订阅；同一 Worktree 的全部会话共享同一探测线程；
- 轻量指纹探测（HEAD/Index/Packed Refs/refs 目录/操作状态文件/工作区顶层
  stat），零 git 进程开销；发现变化只做"失效"通知，最终状态由消费方重新
  调用 Git 权威读取（§10：监控不生成第二事实源）；
- 大量事件 Debounce/Coalesce 合并成一次通知；
- 目标：应用活跃时外部分支切换通常在 1 秒内显示；窗口重新聚焦时调用
  force_check() 立即校准；
- 监控线程绝不直接触碰 prompt_toolkit / pywebview——只调用订阅者回调，
  UI 线程投递由消费方（gui_bridge / CLI call_soon_threadsafe）负责。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.git.repository import normalize_repo_path, resolve_repository
from core.git.runner import GitRunner

# 工作区顶层探测的最大条目数（防止超大目录拖慢探测；变更列表失效以此为准）
_PROBE_TOP_MAX = 512


@dataclass
class _Subscription:
    sub_id: str
    workspace: str          # 规范化 worktree 路径
    callback: Callable[[str], None]


@dataclass
class _Watcher:
    workspace: str
    thread: threading.Thread
    stop_event: threading.Event
    subscriptions: List[_Subscription] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)


class GitStateMonitor:
    """外部变化监控器。单实例全局共享（同一 Worktree 只有一个探测线程）。"""

    def __init__(
        self,
        runner: Optional[GitRunner] = None,
        poll_interval: float = 0.5,
        debounce: float = 0.2,
        probe_depth: int = 1,
    ):
        self._runner = runner or GitRunner()
        self._poll_interval = poll_interval
        self._debounce = debounce
        self._probe_depth = probe_depth
        self._lock = threading.RLock()
        self._watchers: Dict[str, _Watcher] = {}  # normalized ws → watcher
        self._subscribers: Dict[str, _Subscription] = {}  # sub_id → sub
        self._stopped = False

    # ---------------------------------------------------------------- 公开接口

    def subscribe(self, workspace_path: str, on_change: Callable[[str], None]) -> str:
        """订阅一个工作区的失效通知，返回订阅 ID。"""
        ws = normalize_repo_path(str(workspace_path))
        sub_id = uuid.uuid4().hex
        sub = _Subscription(sub_id=sub_id, workspace=ws, callback=on_change)
        with self._lock:
            if self._stopped:
                raise RuntimeError("monitor already stopped")
            self._subscribers[sub_id] = sub
            watcher = self._watchers.get(ws)
            if watcher is None:
                watcher = _Watcher(
                    workspace=ws,
                    thread=threading.Thread(
                        target=self._watch_loop,
                        args=(ws,),
                        daemon=True,
                        name=f"git-monitor-{os.path.basename(ws)}",
                    ),
                    stop_event=threading.Event(),
                )
                self._watchers[ws] = watcher
                watcher.thread.start()
            watcher.subscriptions.append(sub)
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        with self._lock:
            sub = self._subscribers.pop(sub_id, None)
            if sub is None:
                return False
            watcher = self._watchers.get(sub.workspace)
            if watcher is not None:
                with watcher.lock:
                    watcher.subscriptions = [
                        s for s in watcher.subscriptions if s.sub_id != sub_id
                    ]
                if not watcher.subscriptions:
                    watcher.stop_event.set()
                    self._watchers.pop(sub.workspace, None)
            return True

    def force_check(self, workspace_path: str) -> None:
        """窗口重新聚焦等场景：立即做一次权威校准通知（无论指纹是否变化）。"""
        ws = normalize_repo_path(str(workspace_path))
        with self._lock:
            watcher = self._watchers.get(ws)
            subs = list(watcher.subscriptions) if watcher else []
        for sub in subs:
            self._safe_notify(sub, ws)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            for watcher in self._watchers.values():
                watcher.stop_event.set()
            self._watchers.clear()
            self._subscribers.clear()

    def subscriber_count(self, workspace_path: str) -> int:
        ws = normalize_repo_path(str(workspace_path))
        with self._lock:
            watcher = self._watchers.get(ws)
            return len(watcher.subscriptions) if watcher else 0

    # ---------------------------------------------------------------- 内部

    def _watch_loop(self, workspace: str) -> None:
        with self._lock:
            watcher = self._watchers.get(workspace)
            stop_event = watcher.stop_event if watcher else None
        if stop_event is None:
            return
        last_fingerprint = self._probe(workspace)
        while not stop_event.is_set():
            stop_event.wait(self._poll_interval)
            if stop_event.is_set():
                break
            fingerprint = self._probe(workspace)
            if fingerprint == last_fingerprint:
                continue
            # Debounce/Coalesce：等待窗口后复核，仍变化才通知一次
            stop_event.wait(self._debounce)
            confirmed = self._probe(workspace)
            last_fingerprint = confirmed
            if confirmed == fingerprint:
                # 抖动回落也视为变化过一次（外部可能连续操作），保守通知
                pass
            with self._lock:
                watcher = self._watchers.get(workspace)
                subs = list(watcher.subscriptions) if watcher else []
            for sub in subs:
                self._safe_notify(sub, workspace)

    def _safe_notify(self, sub: _Subscription, workspace: str) -> None:
        try:
            sub.callback(workspace)
        except Exception:
            # 订阅者回调异常绝不拖垮监控线程
            pass

    def _probe(self, workspace: str) -> str:
        """轻量指纹：不启动 git 进程，只 stat 关键文件与目录。"""
        hasher = hashlib.sha256()
        self._hash_git_dir(workspace, hasher)
        self._hash_worktree_top(workspace, hasher)
        return hasher.hexdigest()

    def _hash_git_dir(self, workspace: str, hasher) -> None:
        git_dir = self._resolve_git_dir(workspace)
        if not git_dir:
            hasher.update(b"no-git-dir")
            return
        candidates = [
            "HEAD", "index", "ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD",
            "REVERT_HEAD", "BISECT_LOG", "BISECT_START",
            os.path.join("rebase-merge"), os.path.join("rebase-apply"),
            os.path.join("sequencer"),
        ]
        for name in candidates:
            self._stat_into(hasher, os.path.join(git_dir, name))
        # refs 目录递归 stat（通常很小）
        self._hash_tree(os.path.join(git_dir, "refs"), hasher)
        self._stat_into(hasher, os.path.join(git_dir, "packed-refs"))
        # stash reflog（外部 stash 变化）
        self._stat_into(hasher, os.path.join(git_dir, "logs", "refs", "stash"))
        # linked worktrees 注册表
        self._stat_into(hasher, os.path.join(git_dir, "worktrees"))

    def _resolve_git_dir(self, workspace: str) -> Optional[str]:
        """解析真实 Git Dir（不假设 .git 一定是目录，§10）。

        结果按 workspace 缓存于进程内：git dir 位置本身变化时由探测失败兜底。
        """
        cache = getattr(self, "_git_dir_cache", None)
        if cache is None:
            cache = self._git_dir_cache = {}
        key = normalize_repo_path(workspace)
        cached = cache.get(key)
        if cached is not None and os.path.exists(cached):
            return cached
        git_path = os.path.join(key, ".git")
        if os.path.isfile(git_path):
            try:
                with open(git_path, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read().strip()
                for line in content.splitlines():
                    if line.startswith("gitdir:"):
                        target = line[len("gitdir:"):].strip()
                        if not os.path.isabs(target):
                            target = os.path.join(key, target)
                        resolved = os.path.normpath(target)
                        # gitdir: 文件指向 worktrees/<name>；common dir 在其上层
                        cache[key] = resolved
                        return resolved
            except OSError:
                return None
        if os.path.isdir(git_path):
            cache[key] = git_path
            return git_path
        return None

    def _hash_worktree_top(self, workspace: str, hasher) -> None:
        """工作区顶层条目 stat（名称 + mtime + size），用于失效变更列表/文件树。"""
        try:
            entries = sorted(os.scandir(workspace), key=lambda e: e.name)
        except OSError:
            hasher.update(b"unreadable")
            return
        count = 0
        for entry in entries:
            if entry.name == ".git":
                continue
            count += 1
            if count > _PROBE_TOP_MAX:
                hasher.update(b"truncated")
                break
            try:
                stat = entry.stat(follow_symlinks=False)
                hasher.update(
                    f"{entry.name}|{stat.st_mtime_ns}|{stat.st_size}|".encode("utf-8")
                )
            except OSError:
                hasher.update(f"{entry.name}|gone|".encode("utf-8"))

    def _hash_tree(self, root: str, hasher, depth: int = 0) -> None:
        if depth > 4:
            return
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            try:
                stat = entry.stat(follow_symlinks=False)
                hasher.update(
                    f"{entry.name}|{stat.st_mtime_ns}|{stat.st_size}|".encode("utf-8")
                )
                if entry.is_dir(follow_symlinks=False):
                    self._hash_tree(entry.path, hasher, depth + 1)
            except OSError:
                continue

    @staticmethod
    def _stat_into(hasher, path: str) -> None:
        try:
            stat = os.stat(path)
            exists = "1"
        except OSError:
            exists = "0"
            stat = None
        if stat is not None:
            hasher.update(
                f"{path}|{exists}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8")
            )
        else:
            hasher.update(f"{path}|0|".encode("utf-8"))
