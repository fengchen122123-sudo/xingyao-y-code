"""GUI Git 桥接混入（规格 §11/§13；实施计划跨阶段契约"GUI 桥契约"）。

ApiBridge 继承本混入；pywebview 反射暴露 public snake_case 方法。
铁律：
- 返回 {"ok": bool, ...}，错误为 {"ok": False, "error": {code, message, retryable}}；
- 事件只经 _emit_raw（→ _js_queue 单消费者线程 → evaluate_js），绝不直接触碰 JS；
- 乐观并发：桥内缓存每个工作区最近一次权威快照版本，写操作自动携带；
  外部变化经 git_state_changed 事件提醒前端调 git_refresh 刷新版本；
- 高风险操作走 preview（git_preview_risk）→ 用户确认 → execute(token)。
"""

from __future__ import annotations

import logging
from dataclasses import fields as dataclass_fields, is_dataclass
from typing import Any, Dict, Optional

from core.git.bootstrap import get_git_monitor, get_git_service
from core.git.models import GitError, GitErrorCode, GitSnapshot, SnapshotVersions
from core.git.repository import normalize_repo_path

logger = logging.getLogger(__name__)


def _to_dict(obj: Any) -> Any:
    """dataclass/枚举/元组的安全 JSON 化（浅层递归）。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "value") and hasattr(obj, "name"):  # str Enum
        return obj.value
    if is_dataclass(obj):
        return {f.name: _to_dict(getattr(obj, f.name)) for f in dataclass_fields(obj)}
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_dict(v) for v in obj]
    return str(obj)


class GitBridgeMixin:
    """Git 工作台桥接方法集（ApiBridge 的继承混入）。"""

    # ---- 惰性初始化 ------------------------------------------------------

    def _git(self):
        if getattr(self, "_git_svc", None) is None:
            self._git_svc = get_git_service()
            self._git_svc.on_invalidate = self._git_on_invalidated
            self._git_monitor = get_git_monitor()
            self._git_subs: Dict[str, str] = {}
            self._git_versions: Dict[str, SnapshotVersions] = {}
            self._git_serialized: Dict[str, Dict] = {}
        return self._git_svc

    def _git_ok(self, **data) -> Dict:
        return {"ok": True, **data}

    def _git_err(self, error: Exception) -> Dict:
        if isinstance(error, GitError):
            return {
                "ok": False,
                "error": {
                    "code": error.code.value,
                    "message": error.message,
                    "retryable": error.retryable,
                    "command": error.command_summary,
                },
            }
        logger.warning("Git 桥接操作失败", exc_info=True)
        return {
            "ok": False,
            "error": {
                "code": GitErrorCode.GIT_COMMAND_FAILED.value,
                "message": str(error),
                "retryable": False,
                "command": "",
            },
        }

    # ---- 事件与订阅 ------------------------------------------------------

    def _git_on_invalidated(self, workspace: str) -> None:
        """监控线程回调：只入队事件，UI 线程投递由 _js_queue 派发器完成。"""
        try:
            self._emit_raw("git_state_changed", {"workspace": workspace})
        except Exception:
            logger.warning("git_state_changed 事件投递失败", exc_info=True)

    def _git_ensure_subscription(self, workdir: str) -> None:
        self._git()  # 确保惰性字段就绪
        key = normalize_repo_path(workdir)
        if key in self._git_subs:
            return

        def _on_change(ws: str) -> None:
            self._git_on_invalidated(ws)

        try:
            self._git_subs[key] = self._git_monitor.subscribe(str(key), _on_change)
        except Exception:
            logger.warning("Git 监控订阅失败: %s", key, exc_info=True)

    def _git_remember_versions(self, snapshot: GitSnapshot) -> None:
        key = normalize_repo_path(snapshot.repository.workspace_path)
        self._git_versions[key] = SnapshotVersions(
            worktree_version=snapshot.worktree_version,
            refs_version=snapshot.refs_version,
        )

    def _git_versions_for(self, workdir: str) -> SnapshotVersions:
        return self._git_versions.get(
            normalize_repo_path(workdir), SnapshotVersions(None, None)
        )

    def _git_session(self) -> str:
        return getattr(self, "_current_session", "") or ""

    # ---- 仓库信任（§4.5）--------------------------------------------------

    def git_get_trust(self, workdir: str) -> Dict:
        """查询仓库信任状态：前端据此展示"允许本次 / 信任此仓库"对话框。"""
        try:
            from core.git.trust import (
                check_trust,
                collect_security_fingerprint,
            )

            svc = self._git()
            identity = svc.resolve(workdir)
            fingerprint = collect_security_fingerprint(svc.runner, identity)
            return self._git_ok(
                level=check_trust(identity, fingerprint),
                fingerprint=fingerprint,
            )
        except Exception as error:
            return self._git_err(error)

    def git_allow_once(self, workdir: str) -> Dict:
        """"允许本次"：仅当前进程内存有效，不落盘。"""
        try:
            from core.git.trust import allow_once, collect_security_fingerprint

            svc = self._git()
            identity = svc.resolve(workdir)
            allow_once(identity, collect_security_fingerprint(svc.runner, identity))
            return self._git_ok()
        except Exception as error:
            return self._git_err(error)

    def git_trust_repository(self, workdir: str) -> Dict:
        """"信任此仓库"：持久化到 git_trust.json（原子写，指纹变化即失效）。"""
        try:
            from core.git.trust import collect_security_fingerprint, trust_repository

            svc = self._git()
            identity = svc.resolve(workdir)
            trust_repository(
                identity, collect_security_fingerprint(svc.runner, identity)
            )
            return self._git_ok()
        except Exception as error:
            return self._git_err(error)

    # ---- 快照与订阅入口 --------------------------------------------------

    def git_get_status(self, workdir: str) -> Dict:
        try:
            svc = self._git()
            snapshot = svc.get_snapshot(workdir)
            self._git_remember_versions(snapshot)
            self._git_ensure_subscription(workdir)
            payload = _to_dict(snapshot)
            key = normalize_repo_path(workdir)
            self._git_serialized[key] = payload
            return self._git_ok(snapshot=payload)
        except Exception as error:
            return self._git_err(error)

    def git_refresh(self, workdir: str) -> Dict:
        """外部变化后的权威重读（§10：最终状态必须重新调用 Git）。"""
        return self.git_get_status(workdir)

    # ---- 暂存 / 提交 ------------------------------------------------------

    def git_stage(self, workdir: str, paths) -> Dict:
        try:
            snap = self._git().stage(
                workdir, list(paths or []), self._git_versions_for(workdir),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    def git_stage_all(self, workdir: str) -> Dict:
        try:
            svc = self._git()
            snap = svc.execute(
                workdir, "stage", {"all": True},
                versions=self._git_versions_for(workdir),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    def git_unstage(self, workdir: str, paths) -> Dict:
        try:
            snap = self._git().unstage(
                workdir, list(paths or []), self._git_versions_for(workdir),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    def git_discard_changes(self, workdir: str, paths) -> Dict:
        """Restore 未提交内容（高风险：前端必须先 git_preview_risk 并持令牌调用
        git_confirm_operation；本方法保留为无令牌显式拒绝入口）。"""
        return self.git_preview_risk(
            workdir, "discard_changes", {"paths": list(paths or [])}
        )

    def git_commit(self, workdir: str, title: str, body: str = "", amend: bool = False) -> Dict:
        try:
            snap = self._git().commit(
                workdir, title, body, amend, self._git_versions_for(workdir),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    def git_undo_last_commit(self, workdir: str, keep_staged: bool) -> Dict:
        return self.git_preview_risk(
            workdir, "undo_last_commit", {"keep_staged": bool(keep_staged)}
        )

    # ---- 分支 ------------------------------------------------------------

    def git_list_branches(self, workdir: str) -> Dict:
        try:
            branches = self._git().list_branches(workdir)
            return self._git_ok(branches=_to_dict(branches))
        except Exception as error:
            return self._git_err(error)

    def git_switch_branch(self, workdir: str, target: str) -> Dict:
        try:
            snap = self._git().switch_branch(
                workdir, target, self._git_versions_for(workdir),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    def git_create_branch(
        self, workdir: str, name: str, start_point: str = "", switch_after: bool = False
    ) -> Dict:
        try:
            snap = self._git().execute(
                workdir, "branch_create",
                {"name": name, "start_point": start_point or None,
                 "switch_after": bool(switch_after)},
                versions=self._git_versions_for(workdir),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    # ---- Remote 与同步 ---------------------------------------------------

    def git_list_remotes(self, workdir: str) -> Dict:
        try:
            return self._git_ok(remotes=_to_dict(self._git().list_remotes(workdir)))
        except Exception as error:
            return self._git_err(error)

    def git_fetch(self, workdir: str, remote: str = "", prune: bool = False) -> Dict:
        try:
            snap = self._git().execute(
                workdir, "fetch", {"remote": remote or None, "prune": bool(prune)},
                versions=SnapshotVersions(None, None),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    def git_pull(self, workdir: str) -> Dict:
        try:
            snap = self._git().execute(
                workdir, "pull", {},
                versions=self._git_versions_for(workdir),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    def git_push(
        self, workdir: str, remote: str = "origin", refspec: str = "",
        force_with_lease: bool = False, token: str = "",
    ) -> Dict:
        try:
            operation = "push_force_with_lease" if force_with_lease else "push"
            params = {"remote": remote, "refspec": refspec}
            svc = self._git()
            if force_with_lease:
                snap = svc.execute(
                    workdir, operation, params, token=token,
                    versions=self._git_versions_for(workdir),
                    session_id=self._git_session(),
                )
            else:
                snap = svc.execute(
                    workdir, operation, params,
                    versions=self._git_versions_for(workdir),
                    session_id=self._git_session(),
                )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    # ---- 历史 / Diff / Blame / Reflog / Tag / Stash -----------------------

    def git_load_history(self, workdir: str, skip: int = 0, limit: int = 50) -> Dict:
        try:
            commits, has_more = self._git().log_page(
                workdir, skip=int(skip), limit=int(limit)
            )
            return self._git_ok(commits=_to_dict(commits), has_more=has_more)
        except Exception as error:
            return self._git_err(error)

    def git_load_diff(
        self, workdir: str, path: str = "", staged: bool = False,
        left: str = "", right: str = "",
    ) -> Dict:
        try:
            doc = self._git().diff_document(
                workdir, path=path or None, staged=bool(staged),
                left=left or None, right=right or None,
            )
            return self._git_ok(diff=_to_dict(doc))
        except Exception as error:
            return self._git_err(error)

    def git_blame(self, workdir: str, path: str) -> Dict:
        try:
            return self._git_ok(lines=_to_dict(self._git().blame(workdir, path)))
        except Exception as error:
            return self._git_err(error)

    def git_load_reflog(self, workdir: str, skip: int = 0, limit: int = 50) -> Dict:
        try:
            entries = self._git().reflog_page(workdir, skip=int(skip), limit=int(limit))
            return self._git_ok(entries=_to_dict(entries))
        except Exception as error:
            return self._git_err(error)

    def git_list_tags(self, workdir: str) -> Dict:
        try:
            return self._git_ok(tags=_to_dict(self._git().list_tags(workdir)))
        except Exception as error:
            return self._git_err(error)

    def git_list_stashes(self, workdir: str) -> Dict:
        try:
            return self._git_ok(stashes=_to_dict(self._git().list_stashes(workdir)))
        except Exception as error:
            return self._git_err(error)

    def git_stash_create(self, workdir: str, message: str, include_untracked: bool) -> Dict:
        try:
            snap = self._git().execute(
                workdir, "stash_create",
                {"message": message, "include_untracked": bool(include_untracked)},
                versions=self._git_versions_for(workdir),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    def git_stash_apply(self, workdir: str, index: int) -> Dict:
        return self.git_preview_risk(workdir, "stash_apply", {"index": int(index)})

    def git_stash_drop(self, workdir: str, index: int) -> Dict:
        return self.git_preview_risk(workdir, "stash_drop", {"index": int(index)})

    # ---- 风险与两段式确认（§13）-------------------------------------------

    def git_preview_risk(self, workdir: str, operation: str, params: Dict) -> Dict:
        try:
            preview = self._git().preview(
                workdir, operation, dict(params or {}),
                session_id=self._git_session(),
            )
            # preview 内含权威快照版本，同步更新缓存供执行使用
            self._git_versions[normalize_repo_path(workdir)] = preview.versions
            return self._git_ok(preview=_to_dict(preview))
        except Exception as error:
            return self._git_err(error)

    def git_confirm_operation(
        self, workdir: str, operation: str, params: Dict, token: str
    ) -> Dict:
        try:
            snap = self._git().execute(
                workdir, operation, dict(params or {}), token=token,
                versions=self._git_versions_for(workdir),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    # ---- Worktree --------------------------------------------------------

    def git_worktree_list(self, workdir: str) -> Dict:
        try:
            from core.git.query_refs import list_worktrees

            wts = list_worktrees(self._git().runner, self._git().resolve(workdir))
            return self._git_ok(worktrees=_to_dict(wts))
        except Exception as error:
            return self._git_err(error)

    def git_worktree_add(self, workdir: str, path: str, branch: str, new_branch: bool) -> Dict:
        try:
            snap = self._git().execute(
                workdir, "worktree_add",
                {"path": path, "branch": branch, "new_branch": bool(new_branch)},
                versions=SnapshotVersions(None, None),
                session_id=self._git_session(),
            )
            self._git_remember_versions(snap)
            return self._git_ok(snapshot=_to_dict(snap))
        except Exception as error:
            return self._git_err(error)

    def git_worktree_remove(self, workdir: str, path: str, force: bool) -> Dict:
        return self.git_preview_risk(
            workdir, "worktree_remove" if not force else "worktree_remove_dirty",
            {"path": path},
        )

    def git_stop_monitoring(self, workdir: str) -> Dict:
        """会话切换/关闭时取消该工作区的监控订阅。"""
        try:
            self._git()
            key = normalize_repo_path(workdir)
            sub_id = self._git_subs.pop(key, None)
            if sub_id:
                self._git_monitor.unsubscribe(sub_id)
            self._git_versions.pop(key, None)
            self._git_serialized.pop(key, None)
            return self._git_ok()
        except Exception as error:
            return self._git_err(error)
