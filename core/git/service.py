"""GitApplicationService：GUI/CLI 唯一应用服务门面（规格 §6/§9.3/§13）。

每个写操作按 §9.3 九步顺序编排：
  解析仓库 → 获取锁（固定顺序）→ 锁内重读权威状态 → 比较快照版本 →
  前置检查（兼容矩阵/信任/风险确认/硬规则）→ 执行单个白名单操作 →
  重读结果 → 发布失效事件 → 释放锁。

状态过期（stale_snapshot）拒绝执行；确认令牌随快照过期立即失效。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from core.git import mutation, patch as git_patch, query, query_refs
from core.git.coordinator import (
    GitOperationCategory,
    compatible_for_workspace,
    compute_refs_version,
    compute_worktree_version,
    repository_locks,
    versions_match,
    worktree_lock,
)
from core.git.models import (
    ConflictEntry,
    GitError,
    GitErrorCode,
    GitSnapshot,
    HeadState,
    OperationKind,
    OperationPhase,
    RepositoryIdentity,
    SnapshotVersions,
)
from core.git.repository import normalize_repo_path as _norm, resolve_repository
from core.git.risk import (
    ConfirmationService,
    RiskLevel,
    classify_risk,
    requires_confirmation,
    requires_typed_confirmation,
)
from core.git.runner import ExecScope, GitRunner
from core.git.trust import collect_security_fingerprint, require_trust

# 操作 → 兼容矩阵类别（§9.2）
_OPERATION_CATEGORY: Dict[str, GitOperationCategory] = {
    "status": GitOperationCategory.READ_ONLY,
    "log": GitOperationCategory.READ_ONLY,
    "diff": GitOperationCategory.READ_ONLY,
    "blame": GitOperationCategory.READ_ONLY,
    "reflog": GitOperationCategory.READ_ONLY,
    "stage": GitOperationCategory.INDEX,
    "unstage": GitOperationCategory.INDEX,
    "commit": GitOperationCategory.INDEX,
    "discard_changes": GitOperationCategory.WORKTREE_REWRITE,
    "branch_create": GitOperationCategory.REF_ONLY,
    "branch_rename": GitOperationCategory.REF_ONLY,
    "branch_delete_safe": GitOperationCategory.REF_ONLY,
    "branch_delete_force": GitOperationCategory.REF_ONLY,
    "set_upstream": GitOperationCategory.REF_ONLY,
    "switch": GitOperationCategory.WORKTREE_REWRITE,
    "remote_add": GitOperationCategory.COMMON_REPOSITORY,
    "remote_set_url": GitOperationCategory.COMMON_REPOSITORY,
    "remote_delete": GitOperationCategory.COMMON_REPOSITORY,
    "fetch": GitOperationCategory.COMMON_REPOSITORY,
    "pull": GitOperationCategory.WORKTREE_REWRITE,
    "push": GitOperationCategory.COMMON_REPOSITORY,
    "push_force_with_lease": GitOperationCategory.COMMON_REPOSITORY,
    "stash_create": GitOperationCategory.WORKTREE_REWRITE,
    "stash_apply": GitOperationCategory.WORKTREE_REWRITE,
    "stash_pop": GitOperationCategory.WORKTREE_REWRITE,
    "stash_drop": GitOperationCategory.REF_ONLY,
    "stash_branch": GitOperationCategory.WORKTREE_REWRITE,
    "tag_create": GitOperationCategory.REF_ONLY,
    "tag_push": GitOperationCategory.COMMON_REPOSITORY,
    "tag_delete_local": GitOperationCategory.REF_ONLY,
    "tag_delete_remote": GitOperationCategory.COMMON_REPOSITORY,
    "merge": GitOperationCategory.WORKTREE_REWRITE,
    "rebase": GitOperationCategory.WORKTREE_REWRITE,
    "cherry_pick": GitOperationCategory.WORKTREE_REWRITE,
    "revert": GitOperationCategory.WORKTREE_REWRITE,
    "operation_continue": GitOperationCategory.WORKTREE_REWRITE,
    "operation_skip": GitOperationCategory.WORKTREE_REWRITE,
    "operation_abort": GitOperationCategory.WORKTREE_REWRITE,
    "reset_soft": GitOperationCategory.WORKTREE_REWRITE,
    "reset_mixed": GitOperationCategory.WORKTREE_REWRITE,
    "reset_hard": GitOperationCategory.WORKTREE_REWRITE,
    "clean": GitOperationCategory.WORKTREE_REWRITE,
    "undo_last_commit": GitOperationCategory.WORKTREE_REWRITE,
    "worktree_add": GitOperationCategory.COMMON_REPOSITORY,
    "worktree_remove": GitOperationCategory.COMMON_REPOSITORY,
    "worktree_prune": GitOperationCategory.COMMON_REPOSITORY,
    "apply_patch": GitOperationCategory.WORKTREE_REWRITE,
    "stage_hunk": GitOperationCategory.INDEX,
    "ignore_add": GitOperationCategory.WORKTREE_REWRITE,
}

# 可能间接触发仓库脚本/过滤器/外部程序的操作：必须先过仓库信任（§4.5）
_TRUST_REQUIRED = frozenset(
    {
        "commit", "merge", "rebase", "rebase_plan_write", "cherry_pick", "revert",
        "pull", "push", "push_force_with_lease", "stash_apply", "stash_pop",
        "stash_branch", "apply_patch", "discard_changes", "switch", "reset_hard",
        "reset_soft", "reset_mixed", "clean", "operation_continue", "operation_skip",
        "operation_abort", "tag_create",
    }
)


@dataclass(frozen=True)
class GitRiskPreview:
    """§13 preview 产物：影响范围 + 风险级别 + 一次性确认令牌。"""

    operation: str
    risk_level: RiskLevel
    confirm_required: bool
    typed_confirm_required: bool
    confirm_text: str
    versions: SnapshotVersions
    token: Optional[str]


@dataclass
class GitApplicationService:
    """应用服务门面。on_invalidate 供 GUI/CLI 注入失效事件回调。"""

    runner: GitRunner = field(default_factory=GitRunner)
    confirmations: ConfirmationService = field(default_factory=ConfirmationService)
    on_invalidate: Optional[Callable[[str], None]] = None

    # ---------------------------------------------------------------- 快照

    def resolve(self, ws: str) -> RepositoryIdentity:
        """每个请求先解析真实仓库（§9.3 第 1 步）。"""
        return resolve_repository(str(ws), self.runner)

    def get_snapshot(self, ws: str) -> GitSnapshot:
        identity = self.resolve(ws)
        with worktree_lock(identity.worktree_root):
            return self._build_snapshot(identity)

    def _build_snapshot(self, identity: RepositoryIdentity) -> GitSnapshot:
        # --ignored=matching 并入唯一一次 status 扫描（ignored 计数由解析产出）；
        # 曾为计数单独再全量扫一遍工作区，node_modules 仓库单次就要秒级
        raw_status = self.runner.run_checked(
            [
                "status", "--porcelain=v2", "-z", "--branch", "--show-stash",
                "--ignored=matching",
            ],
            cwd=identity.worktree_root,
            scope=ExecScope.READ_ONLY,
            timeout=60.0,
        )
        status = query.parse_status_v2(raw_status.stdout)
        kind, phase = query.detect_operation_state(
            self.runner, identity, status.has_conflicts
        )
        wv = compute_worktree_version(
            identity=identity,
            head_oid=status.head_oid or "",
            index_material="",  # index 材料已包含于 status 原始字节
            # 版本材料剥掉 `!` 记录，与不带 --ignored 的扫描字节保持一致：
            # ignored 文件变化不得触发版本变化（乐观并发会误报冲突）
            status_bytes=query.strip_ignored_records(raw_status.stdout),
            operation_markers=f"{kind.value}:{phase.value}",
            conflict_material=";".join(
                f"{c.path}:{c.kind}" for c in status.conflicts
            ),
        )
        rv = self._compute_refs_version(identity)
        from core.git.repository import probe_identity_state

        identity_state = probe_identity_state(self.runner, identity.worktree_root)
        return GitSnapshot(
            repository=identity,
            head_oid=status.head_oid or "",
            branch_name=status.branch_name,
            head_state=query.head_state_from(status),
            upstream=status.upstream,
            ahead=status.ahead,
            behind=status.behind,
            staged=status.staged,
            unstaged=status.unstaged,
            untracked=status.untracked,
            ignored_count=status.ignored_count,
            conflicts=status.conflicts,
            stash_count=status.stash_count,
            operation_kind=kind,
            operation_phase=phase,
            identity_state=identity_state,
            worktree_version=wv,
            refs_version=rv,
        )

    def _compute_refs_version(self, identity: RepositoryIdentity) -> str:
        head = self.runner.run(
            ["rev-parse", "-q", "--verify", "HEAD"], cwd=identity.worktree_root, timeout=15.0
        )
        remote_cfg = self.runner.run(
            ["config", "--get-regexp", r"^remote\..*\.url$"],
            cwd=identity.worktree_root,
            timeout=15.0,
        )
        return compute_refs_version(
            common_git_dir=identity.common_git_dir,
            ref_oids={"HEAD": head.stdout_text.strip() if head.returncode == 0 else ""},
            remote_config_digest=hashlib.sha256(
                (remote_cfg.stdout or b"")
            ).hexdigest(),
        )

    # ---------------------------------------------------------------- 读接口

    def list_branches(self, ws: str):
        identity = self.resolve(ws)
        return query_refs.list_branches(self.runner, identity)

    def occupied_branches(self, ws: str):
        identity = self.resolve(ws)
        return query_refs.occupied_branches(self.runner, identity)

    def log_page(self, ws: str, **kwargs):
        identity = self.resolve(ws)
        return query_refs.log_page(self.runner, identity, **kwargs)

    def commit_summary_of(self, ws: str, oid: str):
        return query_refs.commit_summary_of(self.runner, self.resolve(ws), oid)

    def diff_document(self, ws: str, **kwargs):
        return query_refs.diff_document(self.runner, self.resolve(ws), **kwargs)

    def blame(self, ws: str, path: str):
        return query_refs.blame(self.runner, self.resolve(ws), path)

    def reflog_page(self, ws: str, **kwargs):
        return query_refs.reflog_page(self.runner, self.resolve(ws), **kwargs)

    def list_tags(self, ws: str):
        return query_refs.list_tags(self.runner, self.resolve(ws))

    def list_remotes(self, ws: str):
        return query_refs.list_remotes(self.runner, self.resolve(ws))

    def list_stashes(self, ws: str):
        return query_refs.list_stashes(self.runner, self.resolve(ws))

    # ---------------------------------------------------------------- 风险与确认（§13）

    def preview(self, ws: str, operation: str, params: dict, session_id: str = "") -> GitRiskPreview:
        risk = classify_risk(operation)
        identity = self.resolve(ws)
        with worktree_lock(identity.worktree_root):
            fresh = self._build_snapshot(identity)
            versions = SnapshotVersions(
                worktree_version=fresh.worktree_version,
                refs_version=fresh.refs_version,
            )
        token = None
        if requires_confirmation(risk):
            token = self.confirmations.issue(
                session_id=session_id,
                repo_digest=self._repo_digest(identity),
                params_digest=ConfirmationService.params_digest(
                    self._digest_params(params)
                ),
                versions=versions,
            )
        return GitRiskPreview(
            operation=operation,
            risk_level=risk,
            confirm_required=requires_confirmation(risk),
            typed_confirm_required=requires_typed_confirmation(risk),
            confirm_text=self._confirm_text(operation, params),
            versions=versions,
            token=token,
        )

    def _confirm_text(self, operation: str, params: dict) -> str:
        target = params.get("target") or params.get("branch") or ""
        return (
            f"操作 {operation} {target} 属于高风险操作，将改写历史或造成不可恢复的丢失。"
            f"确认后执行；不可恢复操作需输入目标分支名或短 Hash 二次确认。"
        )

    def _repo_digest(self, identity: RepositoryIdentity) -> str:
        return hashlib.sha256(
            (
                _norm(identity.common_git_dir)
                + "|"
                + _norm(identity.worktree_root)
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _digest_params(params: dict) -> dict:
        """参与确认摘要的参数：排除 confirm_input（它是验证凭据，不是操作参数），
        保证 preview（无 confirm_input）与 execute（有 confirm_input）摘要一致。"""
        return {k: v for k, v in params.items() if k != "confirm_input"}

    # ---------------------------------------------------------------- 写执行（§9.3）

    def execute(
        self,
        ws: str,
        operation: str,
        params: dict,
        *,
        versions: Optional[SnapshotVersions] = None,
        token: Optional[str] = None,
        session_id: str = "",
    ) -> GitSnapshot:
        """执行单个白名单写操作，返回执行后的新快照。"""
        category = _OPERATION_CATEGORY.get(operation)
        if category is None:
            raise GitError(
                GitErrorCode.UNSAFE_GIT_EXTENSION,
                f"操作 {operation} 不在兼容矩阵白名单内",
            )
        risk = classify_risk(operation)
        needs_token = requires_confirmation(risk)
        if needs_token and not token:
            raise GitError(
                GitErrorCode.CONFIRMATION_REQUIRED,
                f"操作 {operation} 需要先 preview 获取影响范围并完成确认",
            )
        identity = self.resolve(ws)
        trust_fingerprint = None
        if operation in _TRUST_REQUIRED:
            trust_fingerprint = collect_security_fingerprint(self.runner, identity)

        def _run_locked() -> GitSnapshot:
            # 第 3-5 步：锁内重读权威状态、比较版本、前置检查
            fresh = self._build_snapshot(identity)
            if versions is not None:
                if versions.worktree_version is not None and not versions_match(
                    versions.worktree_version, fresh.worktree_version
                ):
                    raise GitError(
                        GitErrorCode.STALE_SNAPSHOT,
                        "工作区状态已变化，界面快照过期；请刷新后重试",
                    )
                if versions.refs_version is not None and not versions_match(
                    versions.refs_version, fresh.refs_version
                ):
                    raise GitError(
                        GitErrorCode.STALE_SNAPSHOT,
                        "共享引用状态已变化；请刷新后重试",
                    )
            allowed, blockers = compatible_for_workspace(
                category, identity.worktree_root
            )
            if not allowed:
                raise GitError(
                    GitErrorCode.WORKSPACE_PROCESS_ACTIVE,
                    "存在需要停止的活动任务: " + ", ".join(blockers),
                )
            if trust_fingerprint is not None:
                require_trust(identity, trust_fingerprint)
            if needs_token:
                self.confirmations.verify_and_consume(
                    token,
                    session_id=session_id,
                    repo_digest=self._repo_digest(identity),
                    params_digest=ConfirmationService.params_digest(
                        self._digest_params(params)
                    ),
                    versions=SnapshotVersions(
                        worktree_version=fresh.worktree_version,
                        refs_version=fresh.refs_version,
                    ),
                )
            if risk is RiskLevel.UNRECOVERABLE:
                self._verify_typed_confirmation(operation, params, fresh)
            # 第 6 步：执行单个白名单操作
            _OPERATION_EXECUTORS[operation](self, identity, fresh, params)
            # 第 7-8 步：重读结果 + 发布失效
            result_snapshot = self._build_snapshot(identity)
            if self.on_invalidate:
                try:
                    self.on_invalidate(identity.workspace_path)
                except Exception:
                    pass
            return result_snapshot

        if category is GitOperationCategory.COMMON_REPOSITORY:
            with repository_locks(identity.common_git_dir, identity.worktree_root):
                return _run_locked()
        with worktree_lock(identity.worktree_root):
            return _run_locked()

    def _verify_typed_confirmation(
        self, operation: str, params: dict, fresh: GitSnapshot
    ) -> None:
        """不可恢复操作要求输入目标分支名或短 Hash 二次确认（§13）。"""
        expected = params.get("target") or params.get("branch") or fresh.branch_name or ""
        typed = str(params.get("confirm_input") or "")
        if not expected or typed.strip() not in (expected, expected[:7], expected[:8]):
            raise GitError(
                GitErrorCode.CONFIRMATION_INVALID,
                f"二次确认输入不匹配目标 {expected}，操作已拒绝",
            )

    # ---------------------------------------------------------------- 便捷写方法（内部走 execute）

    def stage(self, ws: str, paths, versions: SnapshotVersions, session_id: str = "") -> GitSnapshot:
        return self.execute(
            ws, "stage", {"paths": list(paths)}, versions=versions, session_id=session_id
        )

    def unstage(self, ws: str, paths, versions: SnapshotVersions, session_id: str = "") -> GitSnapshot:
        return self.execute(
            ws, "unstage", {"paths": list(paths)}, versions=versions, session_id=session_id
        )

    def commit(
        self, ws: str, title: str, body: str, amend: bool,
        versions: SnapshotVersions, session_id: str = "",
    ) -> GitSnapshot:
        return self.execute(
            ws, "commit", {"title": title, "body": body, "amend": amend},
            versions=versions, session_id=session_id,
        )

    def switch_branch(
        self, ws: str, target: str, versions: SnapshotVersions, session_id: str = ""
    ) -> GitSnapshot:
        occupied = self.occupied_branches(ws)
        return self.execute(
            ws, "switch", {"target": target, "occupied": list(occupied)},
            versions=versions, session_id=session_id,
        )


# ---------------------------------------------------------------- 操作执行器（每个 = 单个白名单 git 调用）

def _op_stage(service, identity, snapshot, params):
    paths = params.get("paths") or []
    if params.get("all"):
        mutation.stage_all(service.runner, identity)
    else:
        mutation.stage(service.runner, identity, paths)


def _op_unstage(service, identity, snapshot, params):
    mutation.unstage(service.runner, identity, params.get("paths") or [])


def _op_commit(service, identity, snapshot, params):
    if snapshot.identity_state != "ok":
        raise GitError(
            GitErrorCode.IDENTITY_MISSING,
            "Git 用户名/邮箱未配置，无法提交；请先设置仓库或全局身份",
        )
    if not snapshot.staged:
        raise GitError(GitErrorCode.INVALID_STATE, "暂存区为空，没有可提交的内容")
    mutation.commit(
        service.runner, identity,
        params.get("title") or "", params.get("body") or "",
        amend=bool(params.get("amend")),
    )


def _op_discard_changes(service, identity, snapshot, params):
    mutation.discard_worktree_changes(service.runner, identity, params.get("paths") or [])


def _op_branch_create(service, identity, snapshot, params):
    mutation.create_branch(
        service.runner, identity,
        params.get("name") or "", params.get("start_point"),
        switch_after=bool(params.get("switch_after")),
    )


def _op_branch_rename(service, identity, snapshot, params):
    mutation.rename_branch(
        service.runner, identity, params.get("name") or "", params.get("new_name") or ""
    )


def _op_branch_delete_safe(service, identity, snapshot, params):
    occupied = query_refs.occupied_branches(service.runner, identity)
    if params.get("name") in occupied:
        raise GitError(
            GitErrorCode.BRANCH_IN_USE,
            f"分支 {params.get('name')} 正被其他 Worktree 使用，禁止删除",
        )
    mutation.delete_local_branch(service.runner, identity, params.get("name") or "", force=False)


def _op_branch_delete_force(service, identity, snapshot, params):
    occupied = query_refs.occupied_branches(service.runner, identity)
    if params.get("name") in occupied:
        raise GitError(
            GitErrorCode.BRANCH_IN_USE,
            f"分支 {params.get('name')} 正被其他 Worktree 使用，禁止删除",
        )
    mutation.delete_local_branch(service.runner, identity, params.get("name") or "", force=True)


def _op_set_upstream(service, identity, snapshot, params):
    mutation.set_upstream(
        service.runner, identity, params.get("branch") or "", params.get("upstream")
    )


def _op_switch(service, identity, snapshot, params):
    mutation.switch_branch_checked(
        service.runner, identity, params.get("target") or "", _status_of(snapshot),
        params.get("occupied") or (),
    )


def _status_of(snapshot: GitSnapshot):
    """从快照重建 switch 硬规则所需的 StatusV2 视图。"""
    from core.git.query import StatusV2

    return StatusV2(
        head_oid=snapshot.head_oid or None,
        branch_name=snapshot.branch_name,
        upstream=snapshot.upstream,
        ahead=snapshot.ahead,
        behind=snapshot.behind,
        stash_count=snapshot.stash_count,
        entries=tuple(snapshot.staged) + tuple(snapshot.unstaged),
        untracked=snapshot.untracked,
        conflicts=snapshot.conflicts,
    )


def _op_remote_add(service, identity, snapshot, params):
    mutation.remote_add(service.runner, identity, params.get("name") or "", params.get("url") or "")


def _op_remote_set_url(service, identity, snapshot, params):
    mutation.remote_set_url(service.runner, identity, params.get("name") or "", params.get("url") or "")


def _op_remote_delete(service, identity, snapshot, params):
    mutation.remote_remove(service.runner, identity, params.get("name") or "")


def _op_fetch(service, identity, snapshot, params):
    mutation.fetch(service.runner, identity, params.get("remote"), bool(params.get("prune")))


def _op_pull(service, identity, snapshot, params):
    mutation.pull_ff_only(service.runner, identity)


def _op_push(service, identity, snapshot, params):
    mutation.push(
        service.runner, identity, params.get("remote") or "origin",
        params.get("refspec") or "", set_upstream=bool(params.get("set_upstream")),
    )


def _op_push_force_with_lease(service, identity, snapshot, params):
    mutation.push(
        service.runner, identity, params.get("remote") or "origin",
        params.get("refspec") or "", force_with_lease=True,
    )


def _op_stash_create(service, identity, snapshot, params):
    mutation.stash_create(
        service.runner, identity, params.get("message") or "",
        include_untracked=bool(params.get("include_untracked")),
    )


def _op_stash_apply(service, identity, snapshot, params):
    mutation.stash_apply(service.runner, identity, int(params.get("index", 0)), pop=False)


def _op_stash_pop(service, identity, snapshot, params):
    mutation.stash_pop(service.runner, identity, int(params.get("index", 0)))


def _op_stash_drop(service, identity, snapshot, params):
    mutation.stash_drop(service.runner, identity, int(params.get("index", 0)))


def _op_stash_branch(service, identity, snapshot, params):
    mutation.stash_branch(
        service.runner, identity, int(params.get("index", 0)), params.get("name") or ""
    )


def _op_tag_create(service, identity, snapshot, params):
    mutation.tag_create(
        service.runner, identity, params.get("name") or "", params.get("target"),
        annotated=bool(params.get("annotated")), message=params.get("message") or "",
    )


def _op_tag_push(service, identity, snapshot, params):
    mutation.tag_push(
        service.runner, identity, params.get("remote") or "origin", params.get("name") or ""
    )


def _op_tag_delete_local(service, identity, snapshot, params):
    mutation.tag_delete_local(service.runner, identity, params.get("name") or "")


def _op_tag_delete_remote(service, identity, snapshot, params):
    mutation.tag_delete_remote(
        service.runner, identity, params.get("remote") or "origin", params.get("name") or ""
    )


def _op_merge(service, identity, snapshot, params):
    if snapshot.operation_kind is not OperationKind.IDLE:
        raise GitError(
            GitErrorCode.OPERATION_IN_PROGRESS,
            f"已有进行中的操作 {snapshot.operation_kind.value}，先 Continue/Abort",
        )
    mutation.merge(service.runner, identity, params.get("ref") or "", params.get("ff_mode") or "ff")


def _op_rebase(service, identity, snapshot, params):
    if snapshot.operation_kind is not OperationKind.IDLE:
        raise GitError(
            GitErrorCode.OPERATION_IN_PROGRESS,
            f"已有进行中的操作 {snapshot.operation_kind.value}，先 Continue/Abort",
        )
    mutation.rebase(service.runner, identity, params.get("onto") or "")


def _op_cherry_pick(service, identity, snapshot, params):
    if snapshot.operation_kind is not OperationKind.IDLE:
        raise GitError(
            GitErrorCode.OPERATION_IN_PROGRESS,
            f"已有进行中的操作 {snapshot.operation_kind.value}",
        )
    mutation.cherry_pick(service.runner, identity, params.get("oids") or [])


def _op_revert(service, identity, snapshot, params):
    if snapshot.operation_kind is not OperationKind.IDLE:
        raise GitError(
            GitErrorCode.OPERATION_IN_PROGRESS,
            f"已有进行中的操作 {snapshot.operation_kind.value}",
        )
    mutation.revert(service.runner, identity, params.get("oids") or [])


def _op_operation_continue(service, identity, snapshot, params):
    mutation.operation_continue(service.runner, identity, params.get("kind") or "")


def _op_operation_skip(service, identity, snapshot, params):
    mutation.operation_skip(service.runner, identity, params.get("kind") or "")


def _op_operation_abort(service, identity, snapshot, params):
    mutation.operation_abort(service.runner, identity, params.get("kind") or "")


def _op_reset_soft(service, identity, snapshot, params):
    mutation.reset(service.runner, identity, "soft", params.get("target") or "HEAD~1")


def _op_reset_mixed(service, identity, snapshot, params):
    mutation.reset(service.runner, identity, "mixed", params.get("target") or "HEAD~1")


def _op_reset_hard(service, identity, snapshot, params):
    mutation.reset(service.runner, identity, "hard", params.get("target") or "HEAD")


def _op_clean(service, identity, snapshot, params):
    if not params.get("confirmed_files"):
        raise GitError(
            GitErrorCode.CONFIRMATION_REQUIRED,
            "Clean 前必须先 dry-run 展示准确清单并确认",
        )
    mutation.clean_untracked(service.runner, identity, dry_run=False)


def _op_undo_last_commit(service, identity, snapshot, params):
    mutation.undo_last_commit(
        service.runner, identity, keep_staged=bool(params.get("keep_staged"))
    )


def _op_worktree_add(service, identity, snapshot, params):
    mutation.worktree_add(
        service.runner, identity, params.get("path") or "", params.get("branch") or "",
        new_branch=bool(params.get("new_branch")),
    )


def _op_worktree_remove(service, identity, snapshot, params):
    mutation.worktree_remove(
        service.runner, identity, params.get("path") or "", force=bool(params.get("force"))
    )


def _op_worktree_prune(service, identity, snapshot, params):
    mutation.worktree_prune(service.runner, identity)


def _op_apply_patch(service, identity, snapshot, params):
    git_patch.apply_patch_text(
        service.runner, identity, params.get("patch_text") or "",
        staged=bool(params.get("staged")),
    )


def _op_stage_hunk(service, identity, snapshot, params):
    patch_text = params.get("patch_text") or ""
    git_patch.apply_patch_text(service.runner, identity, patch_text, staged=True)


def _op_ignore_add(service, identity, snapshot, params):
    import os

    rule = params.get("rule") or ""
    if not rule.strip():
        raise GitError(GitErrorCode.INVALID_STATE, ".gitignore 规则不能为空")
    gitignore = os.path.join(identity.worktree_root, ".gitignore")
    with open(gitignore, "a", encoding="utf-8") as handle:
        handle.write(("" if rule.startswith("\n") else "\n") + rule + "\n")


def _op_branch_delete_force_guard(service, identity, snapshot, params):
    _op_branch_delete_force(service, identity, snapshot, params)


_OPERATION_EXECUTORS: Dict[str, Callable] = {
    "stage": _op_stage,
    "unstage": _op_unstage,
    "commit": _op_commit,
    "discard_changes": _op_discard_changes,
    "branch_create": _op_branch_create,
    "branch_rename": _op_branch_rename,
    "branch_delete_safe": _op_branch_delete_safe,
    "branch_delete_force": _op_branch_delete_force,
    "set_upstream": _op_set_upstream,
    "switch": _op_switch,
    "remote_add": _op_remote_add,
    "remote_set_url": _op_remote_set_url,
    "remote_delete": _op_remote_delete,
    "fetch": _op_fetch,
    "pull": _op_pull,
    "push": _op_push,
    "push_force_with_lease": _op_push_force_with_lease,
    "stash_create": _op_stash_create,
    "stash_apply": _op_stash_apply,
    "stash_pop": _op_stash_pop,
    "stash_drop": _op_stash_drop,
    "stash_branch": _op_stash_branch,
    "tag_create": _op_tag_create,
    "tag_push": _op_tag_push,
    "tag_delete_local": _op_tag_delete_local,
    "tag_delete_remote": _op_tag_delete_remote,
    "merge": _op_merge,
    "rebase": _op_rebase,
    "cherry_pick": _op_cherry_pick,
    "revert": _op_revert,
    "operation_continue": _op_operation_continue,
    "operation_skip": _op_operation_skip,
    "operation_abort": _op_operation_abort,
    "reset_soft": _op_reset_soft,
    "reset_mixed": _op_reset_mixed,
    "reset_hard": _op_reset_hard,
    "clean": _op_clean,
    "undo_last_commit": _op_undo_last_commit,
    "worktree_add": _op_worktree_add,
    "worktree_remove": _op_worktree_remove,
    "worktree_prune": _op_worktree_prune,
    "apply_patch": _op_apply_patch,
    "stage_hunk": _op_stage_hunk,
    "ignore_add": _op_ignore_add,
}
