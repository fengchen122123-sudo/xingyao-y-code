"""CLI Git 工作台（规格 §12）。

- ``handle_git_command``：严格解析 /git 子命令 → GitApplicationService 调用，
  绝不透传任意参数（§12.3）；
- 高风险操作两步确认：第一次执行返回 CONFIRMATION_REQUIRED 时展示 preview 并
  暂存一次性令牌（60 秒 TTL，进程内存），用户追加 ``--confirm <目标>`` 后执行；
- ``render_git_status_bar``：CLI 底部状态栏 Git 段（TTL 缓存，渲染永不抛错）；
- ``open_git_manager``：全屏管理器（8 页签，prompt_toolkit 独立 Application，
  模式照 core/session_selector.py）。
"""

from __future__ import annotations

import html
import time
from typing import List, Optional, Tuple

from core.git.bootstrap import get_git_service
from core.git.models import GitError, GitErrorCode, SnapshotVersions
from core.git.repository import resolve_repository
from core.git.runner import GitRunner

_CONFIRM_TTL = 55.0  # 略小于服务端 60 秒令牌有效期

# 两步确认的进程内暂存：op 名 → (token, params, expires_at)
_pending_cli_confirm: dict = {}


# ---------------------------------------------------------------- 输出助手

# 可注入打印机（style: ansigray/ansired）；默认走 prompt_toolkit。
# 测试注入收集器即可在无控制台环境验证输出。
def _default_printer(text: str, style: str = "ansigray") -> None:
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import HTML

    print_formatted_text(HTML(f"<{style}>{html.escape(text)}</{style}>"))


_printer = _default_printer


def _print_html(text: str, style: str = "ansigray") -> None:
    _printer(text, style)


def _print_error_raw(text: str) -> None:
    _printer(text, "ansired")


def _print_error(error: GitError) -> None:
    hint = ""
    if error.code is GitErrorCode.CONFIRMATION_REQUIRED:
        hint = "\n再次执行同一命令并追加 --confirm <目标分支名或短Hash> 以确认。"
    elif error.code is GitErrorCode.REPOSITORY_UNTRUSTED:
        hint = "\n可执行 /git trust repo 信任此仓库，或 /git trust once 允许本次。"
    _print_error_raw(f"🚨 [{error.code.value}] {error.message}{hint}")


def _service():
    return get_git_service()


def _snapshot_or_error(ws: str):
    try:
        return _service().get_snapshot(ws), None
    except GitError as error:
        return None, error


# ---------------------------------------------------------------- 子命令实现

def _cmd_status(ws: str, parts: List[str]) -> None:
    snap, error = _snapshot_or_error(ws)
    if error:
        _print_error(error)
        return
    lines = [
        f"仓库: {snap.repository.worktree_root}",
        f"分支: {snap.branch_name or '(detached)'}  HEAD: {snap.head_oid[:10]}",
    ]
    if snap.upstream:
        lines.append(f"上游: {snap.upstream}  ↑{snap.ahead} ↓{snap.behind}")
    if snap.operation_kind.value != "idle":
        lines.append(f"进行中: {snap.operation_kind.value} ({snap.operation_phase.value})")
    if snap.staged:
        lines.append(f"已暂存 {len(snap.staged)}:")
        lines += [f"  {e.index_status} {e.path}" for e in snap.staged[:20]]
    if snap.unstaged:
        lines.append(f"未暂存 {len(snap.unstaged)}:")
        lines += [f"  {e.worktree_status} {e.path}" for e in snap.unstaged[:20]]
    if snap.untracked:
        lines.append(f"未跟踪 {len(snap.untracked)}:")
        lines += [f"  ? {p}" for p in snap.untracked[:20]]
    if snap.conflicts:
        lines.append(f"冲突 {len(snap.conflicts)}:")
        lines += [f"  ! {c.path} ({c.kind})" for c in snap.conflicts]
    if not (snap.staged or snap.unstaged or snap.untracked or snap.conflicts):
        lines.append("工作区干净")
    lines.append(f"stash: {snap.stash_count}  忽略: {snap.ignored_count}")
    _print_html("\n".join(lines))


def _cmd_diff(ws: str, parts: List[str]) -> None:
    staged = "--staged" in parts[1:]
    path = next((p for p in parts[2:] if not p.startswith("-")), "")
    try:
        doc = _service().diff_document(ws, path=path or None, staged=staged)
    except GitError as error:
        _print_error(error)
        return
    if doc.is_binary:
        _print_html("二进制文件：不生成文本 Diff")
        return
    if not doc.raw_text.strip():
        _print_html("无差异")
        return
    text = doc.raw_text
    if len(text) > 4000:
        text = text[:4000] + "\n…（大 Diff 已截断）"
    # diff 行内容原样输出（HTML 已转义），+/- 行加色
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import HTML

    for line in text.splitlines():
        safe = html.escape(line)
        if line.startswith("+") and not line.startswith("+++"):
            print_formatted_text(HTML(f"<ansigreen>{safe}</ansigreen>"))
        elif line.startswith("-") and not line.startswith("---"):
            print_formatted_text(HTML(f"<ansired>{safe}</ansired>"))
        else:
            print_formatted_text(HTML(f"<ansigray>{safe}</ansigray>"))


def _cmd_stage(ws: str, parts: List[str]) -> None:
    args = [p for p in parts[1:] if p != "all"]
    if not parts[1:]:
        _print_error_raw("用法: /git stage <路径>... | /git stage all")
        return
    try:
        snap = _service().execute(
            ws, "stage",
            {"paths": args, "all": "all" in parts},
            versions=SnapshotVersions(None, None),
            session_id=_session(),
        )
        _print_html(f"✔ 已暂存，当前已暂存 {len(snap.staged)} 个文件")
    except GitError as error:
        _print_error(error)


def _cmd_unstage(ws: str, parts: List[str]) -> None:
    if len(parts) < 2:
        _print_error_raw("用法: /git unstage <路径>...")
        return
    try:
        snap = _service().execute(
            ws, "unstage", {"paths": parts[1:]},
            versions=SnapshotVersions(None, None), session_id=_session(),
        )
        _print_html(f"✔ 已取消暂存，剩余已暂存 {len(snap.staged)} 个文件")
    except GitError as error:
        _print_error(error)


def _cmd_commit(ws: str, parts: List[str]) -> None:
    if len(parts) < 2 or not " ".join(parts[1:]).strip():
        _print_error_raw("用法: /git commit <提交说明标题>")
        return
    try:
        snap = _service().commit(
            ws, " ".join(parts[1:]), "", False,
            SnapshotVersions(None, None), session_id=_session(),
        )
        _print_html(f"✔ 已提交 {snap.head_oid[:10]}")
    except GitError as error:
        _print_error(error)


def _cmd_branch(ws: str, parts: List[str]) -> None:
    svc = _service()
    sub = parts[1] if len(parts) > 1 else "list"
    try:
        if sub == "list":
            branches = svc.list_branches(ws)
            for b in branches:
                if b.is_remote:
                    continue
                mark = "* " if b.is_current else "  "
                extra = f"  {b.upstream} ↑{b.ahead} ↓{b.behind}" if b.upstream else ""
                _print_html(f"{mark}{b.name}{extra}")
        elif sub == "create":
            if len(parts) < 3:
                _print_error_raw("用法: /git branch create <名称> [起点]")
                return
            snap = svc.execute(
                ws, "branch_create",
                {"name": parts[2], "start_point": parts[3] if len(parts) > 3 else None},
                versions=SnapshotVersions(None, None), session_id=_session(),
            )
            _print_html(f"✔ 已创建分支 {parts[2]}（当前在 {snap.branch_name}）")
        elif sub == "delete":
            name = next((p for p in parts[2:] if not p.startswith("-")), "")
            if not name:
                _print_error_raw("用法: /git branch delete [-f] <名称>")
                return
            operation = "branch_delete_force" if "-f" in parts else "branch_delete_safe"
            confirm = _confirm_arg(parts)
            params = {"name": name}
            if confirm is not None:
                params["confirm_input"] = confirm
            _execute_with_cli_confirm(ws, operation, params, name, confirmed=confirm is not None)
        else:
            _print_error_raw("用法: /git branch [list|create|delete]")
    except GitError as error:
        _print_error(error)


def _cmd_switch(ws: str, parts: List[str]) -> None:
    if len(parts) < 2:
        _print_error_raw("用法: /git switch <分支名>")
        return
    try:
        snap = _service().switch_branch(
            ws, parts[1], SnapshotVersions(None, None), session_id=_session()
        )
        _print_html(f"✔ 已切换到 {snap.branch_name}")
    except GitError as error:
        _print_error(error)


def _cmd_fetch(ws: str, parts: List[str]) -> None:
    remote = next((p for p in parts[1:] if not p.startswith("-")), "")
    try:
        _service().execute(
            ws, "fetch", {"remote": remote or None, "prune": "--prune" in parts},
            versions=SnapshotVersions(None, None), session_id=_session(),
        )
        _print_html("✔ fetch 完成")
    except GitError as error:
        _print_error(error)


def _cmd_pull(ws: str, parts: List[str]) -> None:
    try:
        _service().execute(ws, "pull", {}, versions=SnapshotVersions(None, None), session_id=_session())
        _print_html("✔ pull 完成（仅 fast-forward）")
    except GitError as error:
        _print_error(error)


def _cmd_push(ws: str, parts: List[str]) -> None:
    args = [p for p in parts[1:] if not p.startswith("-")]
    remote = args[0] if args else "origin"
    refspec = args[1] if len(args) > 1 else ""
    force = "--force-with-lease" in parts
    confirm = _confirm_arg(parts)
    if force:
        params = {"remote": remote, "refspec": refspec}
        if confirm is not None:
            params["confirm_input"] = confirm
        _execute_with_cli_confirm(
            ws, "push_force_with_lease", params, refspec or remote,
            confirmed=confirm is not None,
        )
        return
    try:
        _service().execute(
            ws, "push", {"remote": remote, "refspec": refspec},
            versions=SnapshotVersions(None, None), session_id=_session(),
        )
        _print_html(f"✔ 已推送到 {remote}")
    except GitError as error:
        _print_error(error)


def _cmd_stash(ws: str, parts: List[str]) -> None:
    svc = _service()
    sub = parts[1] if len(parts) > 1 else "list"
    try:
        if sub == "list":
            stashes = svc.list_stashes(ws)
            if not stashes:
                _print_html("没有 stash")
            for s in stashes:
                _print_html(f"stash@{{{s.index}}}: {s.message}")
        elif sub == "push":
            message = " ".join(p for p in parts[2:] if not p.startswith("-")) or "stash"
            snap = svc.execute(
                ws, "stash_create",
                {"message": message, "include_untracked": "-u" in parts},
                versions=SnapshotVersions(None, None), session_id=_session(),
            )
            _print_html(f"✔ 已创建 stash（工作区剩余未提交 {len(snap.staged) + len(snap.unstaged) + len(snap.untracked)}）")
        elif sub in ("apply", "pop"):
            if len(parts) < 3 or not parts[2].isdigit():
                _print_error_raw(f"用法: /git stash {sub} <序号>")
                return
            operation = "stash_apply" if sub == "apply" else "stash_pop"
            snap = svc.execute(
                ws, operation, {"index": int(parts[2])},
                versions=SnapshotVersions(None, None), session_id=_session(),
            )
            _print_html(f"✔ stash {sub} 完成（冲突 {len(snap.conflicts)}）")
        elif sub == "drop":
            if len(parts) < 3 or not parts[2].isdigit():
                _print_error_raw("用法: /git stash drop <序号>")
                return
            confirm = _confirm_arg(parts)
            params = {"index": int(parts[2])}
            if confirm is not None:
                params["confirm_input"] = confirm
            _execute_with_cli_confirm(
                ws, "stash_drop", params, f"stash@{{{parts[2]}}}",
                confirmed=confirm is not None,
            )
        else:
            _print_error_raw("用法: /git stash [list|push|apply|pop|drop]")
    except GitError as error:
        _print_error(error)


def _cmd_log(ws: str, parts: List[str]) -> None:
    limit = 20
    if len(parts) > 1 and parts[1].isdigit():
        limit = int(parts[1])
    try:
        commits, _more = _service().log_page(ws, skip=0, limit=limit)
        for c in commits:
            refs = f" ({', '.join(c.refs)})" if c.refs else ""
            _print_html(f"{c.short} {c.summary}<ansigray> — {c.author_name}{refs}</ansigray>")
    except GitError as error:
        _print_error(error)


def _cmd_worktree(ws: str, parts: List[str]) -> None:
    # 保留既有 /worktree 命令入口（main.py），这里仅提示
    _print_html("worktree 管理请使用 /worktree 命令（list|create|remove|prune）")


def _cmd_trust(ws: str, parts: List[str]) -> None:
    from core.git.trust import (
        allow_once,
        check_trust,
        collect_security_fingerprint,
        trust_repository,
    )

    sub = parts[1] if len(parts) > 1 else "status"
    try:
        identity = _service().resolve(ws)
        fingerprint = collect_security_fingerprint(GitRunner(), identity)
        if sub == "status":
            _print_html(f"仓库信任状态: {check_trust(identity, fingerprint)}")
        elif sub == "once":
            allow_once(identity, fingerprint)
            _print_html("✔ 本次会话内允许该仓库（不落盘）")
        elif sub == "repo":
            trust_repository(identity, fingerprint)
            _print_html("✔ 已信任此仓库（安全配置变化时会自动失效）")
        else:
            _print_error_raw("用法: /git trust [status|once|repo]")
    except GitError as error:
        _print_error(error)


def _cmd_reset(ws: str, parts: List[str]) -> None:
    stripped = [p.lstrip("-") for p in parts]
    mode = next((p for p in stripped[1:] if p in ("soft", "mixed", "hard")), "")
    target = next((p for p in parts[2:] if not p.startswith("-")), "HEAD~1")
    if not mode:
        _print_error_raw("用法: /git reset [--soft|--mixed|--hard] <目标>")
        return
    operation = f"reset_{mode}"
    confirm = _confirm_arg(parts)
    params = {"target": target}
    if confirm is not None:
        params["confirm_input"] = confirm
    _execute_with_cli_confirm(ws, operation, params, target, confirmed=confirm is not None)


def _cmd_clean(ws: str, parts: List[str]) -> None:
    try:
        svc = _service()
        if "-n" in parts or "--dry-run" in parts:
            preview_text = _preview_clean(svc, ws)
            _print_html(f"将删除以下未跟踪文件:\n{preview_text}\n确认后执行: /git clean --go")
            return
        if "--go" not in parts:
            preview_text = _preview_clean(svc, ws)
            _print_html(f"将删除以下未跟踪文件:\n{preview_text}\n确认无误后执行: /git clean --go")
            return
        preview_list = _preview_clean(svc, ws)
        # 不可恢复操作要求输入分支名/短 Hash 二次确认（§13）：clean 以当前分支名为确认目标
        branch = _service().get_snapshot(ws).branch_name or "HEAD"
        _execute_with_cli_confirm(
            ws, "clean", {"confirmed_files": preview_list, "confirm_input": branch},
            branch, confirmed=True,
        )
    except GitError as error:
        _print_error(error)


def _preview_clean(svc, ws: str) -> str:
    from core.git.runner import ExecScope

    result = svc.runner.run_checked(
        ["clean", "-d", "-n"],
        cwd=svc.resolve(ws).worktree_root,
        scope=ExecScope.WRITE,
        timeout=60.0,
    )
    return result.stdout_text.strip() or "（无未跟踪文件）"


def _cmd_operation(ws: str, parts: List[str]) -> None:
    if len(parts) < 2 or parts[1] not in ("continue", "skip", "abort"):
        _print_error_raw("用法: /git continue | /git skip | /git abort")
        return
    try:
        snap = _service().get_snapshot(ws)
        kind = snap.operation_kind.value
        if kind == "idle":
            _print_error_raw("当前没有进行中的操作")
            return
        _service().execute(
            ws, f"operation_{parts[1]}", {"kind": kind},
            versions=SnapshotVersions(None, None), session_id=_session(),
        )
        _print_html(f"✔ {kind} {parts[1]} 完成")
    except GitError as error:
        _print_error(error)


def _session() -> str:
    try:
        import core.app_context as app_context

        return app_context.get_current_session() or ""
    except Exception:
        return ""


def _confirm_arg(parts: List[str]) -> Optional[str]:
    if "--confirm" in parts:
        idx = parts.index("--confirm")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _execute_with_cli_confirm(
    ws: str, operation: str, params: dict, confirm_target: str, *, confirmed: bool
) -> None:
    """CLI 两步确认：首次调用 → preview + 暂存令牌；用户追加 --confirm 后执行。"""
    key = f"{ws}|{operation}"
    pending = _pending_cli_confirm.get(key)
    if pending and time.time() < pending["expires"] and confirmed:
        try:
            params = dict(params)
            params.setdefault("confirm_input", confirm_target)
            snap = _service().execute(
                ws, operation, params, token=pending["token"],
                versions=pending["versions"], session_id=_session(),
            )
            _pending_cli_confirm.pop(key, None)
            _print_html(f"✔ {operation} 完成（HEAD: {snap.head_oid[:10]}）")
        except GitError as error:
            _print_error(error)
        return
    # 首次：先 preview 取影响范围与令牌
    try:
        preview = _service().preview(ws, operation, params, session_id=_session())
    except GitError as error:
        _print_error(error)
        return
    if not preview.confirm_required:
        snap = _service().execute(
            ws, operation, params, versions=preview.versions, session_id=_session()
        )
        _print_html(f"✔ {operation} 完成（HEAD: {snap.head_oid[:10]}）")
        return
    _pending_cli_confirm[key] = {
        "token": preview.token,
        "params": params,
        "versions": preview.versions,
        "expires": time.time() + _CONFIRM_TTL,
    }
    _print_error_raw(
        f"⚠️ {preview.confirm_text}\n"
        f"确认执行请追加 --confirm {confirm_target}（60 秒内有效）"
    )


# ---------------------------------------------------------------- 命令分发

_USAGE = (
    "用法: /git <子命令>\n"
    "  status | diff [--staged] [路径] | stage <路径>|all | unstage <路径>\n"
    "  commit <标题> | branch [list|create|delete] | switch <分支>\n"
    "  fetch [远端] [--prune] | pull | push [远端] [分支] [--force-with-lease]\n"
    "  stash [list|push|apply|pop|drop] | log [N] | worktree\n"
    "  reset [--soft|--mixed|--hard] <目标> | clean | continue | skip | abort\n"
    "  trust [status|once|repo]"
)

_HANDLERS = {
    "status": _cmd_status,
    "diff": _cmd_diff,
    "stage": _cmd_stage,
    "unstage": _cmd_unstage,
    "commit": _cmd_commit,
    "branch": _cmd_branch,
    "switch": _cmd_switch,
    "checkout": _cmd_switch,
    "fetch": _cmd_fetch,
    "pull": _cmd_pull,
    "push": _cmd_push,
    "stash": _cmd_stash,
    "log": _cmd_log,
    "worktree": _cmd_worktree,
    "trust": _cmd_trust,
    "reset": _cmd_reset,
    "clean": _cmd_clean,
    "continue": _cmd_operation,
    "skip": _cmd_operation,
    "abort": _cmd_operation,
}


def parse_git_subcommand(text: str) -> Tuple[str, List[str]]:
    """严格解析 /git 子命令（表驱动，非法子命令报用法，绝不透传 Shell）。"""
    parts = text.strip().split()
    if len(parts) < 2:
        return "", []
    return parts[1], parts[1:]


def handle_git_command(command_text: str) -> None:
    """main.py /git 分支入口：解析并执行，直接负责终端输出。"""
    sub, parts = parse_git_subcommand(command_text)
    if not sub:
        _print_html(_USAGE)
        return
    handler = _HANDLERS.get(sub)
    if handler is None:
        _print_error_raw(f"未知子命令 {sub}。\n{_USAGE}")
        return
    ws = _workspace()
    if not ws:
        _print_error_raw("当前会话没有工作目录，无法使用 Git 工作台。")
        return
    handler(ws, parts)


def _workspace() -> str:
    try:
        from core.workspace_context import get_workdir

        return get_workdir() or ""
    except Exception:
        return ""


# ---------------------------------------------------------------- 状态栏

_status_cache: dict = {"key": None, "at": 0.0, "text": ""}


def render_git_status_bar(workspace: str) -> str:
    """CLI 底部状态栏 Git 段（§12.1）。

    带缓存（同一工作区 1.5 秒内复用上次结果）；任何异常返回空串，
    状态栏渲染绝不因 Git 异常抖动。
    """
    now = time.time()
    key = workspace or ""
    if _status_cache["key"] == key and now - _status_cache["at"] < 1.5:
        return _status_cache["text"]
    try:
        snap = _service().get_snapshot(key) if key else None
        if snap is None:
            text = ""
        else:
            dirty = "*" if (snap.staged or snap.unstaged or snap.untracked) else ""
            sync = ""
            if snap.upstream and (snap.ahead or snap.behind):
                sync = f" ↑{snap.ahead} ↓{snap.behind}" if snap.ahead or snap.behind else ""
            n = len(snap.staged) + len(snap.unstaged) + len(snap.untracked)
            counts = f" {n} changes" if n else " clean"
            if snap.conflicts:
                counts = f" {len(snap.conflicts)} conflicts"
            op = ""
            if snap.operation_kind.value not in ("idle", "unsupported_external"):
                op = f" {snap.operation_kind.value.upper()}"
            branch = snap.branch_name or (f"HEAD@{snap.head_oid[:7]}" if snap.head_oid else "unborn")
            text = f"⎇ {branch}{dirty}{sync}{op}{counts}"
    except Exception:
        text = ""
    _status_cache.update(key=key, at=now, text=text)
    return text


# ---------------------------------------------------------------- 全屏管理器

def open_git_manager(workspace: str) -> None:
    """/git 全屏管理器（§12.2）：8 页签、键盘导航、q 退出。

    独立 prompt_toolkit Application（模式照 core/session_selector.py），
    只读展示 + 安全动作；写操作仍走 GitApplicationService 的完整门禁。
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    state = {"tab": 0, "selected": 0, "rows": [], "header": ""}
    tabs = ["Status", "Branches", "History", "Stashes", "Worktrees", "Remotes", "Tags", "Reflog"]

    def reload() -> None:
        svc = _service()
        try:
            tab = tabs[state["tab"]]
            rows: List[str] = []
            if tab == "Status":
                snap = svc.get_snapshot(workspace)
                state["header"] = f"⎇ {snap.branch_name}  HEAD {snap.head_oid[:10]}"
                rows = [
                    f"staged    {e.index_status} {e.path}" for e in snap.staged
                ] + [
                    f"unstaged  {e.worktree_status} {e.path}" for e in snap.unstaged
                ] + [
                    f"untracked ? {p}" for p in snap.untracked
                ] + [
                    f"conflict  {c.path}" for c in snap.conflicts
                ] or ["（工作区干净）"]
            elif tab == "Branches":
                rows = [
                    f"{'*' if b.is_current else ' '} {b.name}"
                    + (f"  {b.upstream} ↑{b.ahead} ↓{b.behind}" if b.upstream else "")
                    for b in svc.list_branches(workspace) if not b.is_remote
                ]
            elif tab == "History":
                commits, _ = svc.log_page(workspace, skip=0, limit=30)
                rows = [f"{c.short} {c.summary} ({c.author_name})" for c in commits]
            elif tab == "Stashes":
                rows = [f"stash@{{{s.index}}} {s.message}" for s in svc.list_stashes(workspace)] or ["（无）"]
            elif tab == "Worktrees":
                from core.git.query_refs import list_worktrees

                rows = [
                    f"{wt.path}  {wt.branch or '(detached)'}"
                    for wt in list_worktrees(svc.runner, svc.resolve(workspace))
                ]
            elif tab == "Remotes":
                rows = [f"{r.name}  {r.fetch_url}" for r in svc.list_remotes(workspace)] or ["（无）"]
            elif tab == "Tags":
                rows = [f"{t.name}" for t in svc.list_tags(workspace)] or ["（无）"]
            else:
                rows = [f"{e.selector} {e.message}" for e in svc.reflog_page(workspace, skip=0, limit=30)]
            state["rows"] = rows
            state["selected"] = min(state["selected"], max(len(rows) - 1, 0))
        except GitError as error:
            state["rows"] = [f"[{error.code.value}] {error.message}"]

    def render_text():
        fragment = [("bold", f" Git 工作台  [{' | '.join(tabs)}]\n")]
        fragment.append(("class:dim", f" {tabs[state['tab']]} — {state['header']}\n\n"))
        for i, row in enumerate(state["rows"]):
            style = "reverse" if i == state["selected"] else ""
            fragment.append((style, f" {row}\n"))
        fragment.append(("class:dim", "\n ←/→ 切换页签  ↑/↓ 选择  r 刷新  q 退出"))
        return fragment

    def build_rows_window() -> Window:
        return Window(content=FormattedTextControl(render_text), wrap_lines=False)

    kb = KeyBindings()

    @kb.add("left")
    def _left(event) -> None:
        state["tab"] = (state["tab"] - 1) % len(tabs)
        state["selected"] = 0
        reload()

    @kb.add("right")
    def _right(event) -> None:
        state["tab"] = (state["tab"] + 1) % len(tabs)
        state["selected"] = 0
        reload()

    @kb.add("up")
    def _up(event) -> None:
        state["selected"] = max(state["selected"] - 1, 0)

    @kb.add("down")
    def _down(event) -> None:
        state["selected"] = min(state["selected"] + 1, len(state["rows"]) - 1)

    @kb.add("r")
    def _r(event) -> None:
        reload()

    @kb.add("q")
    @kb.add("escape")
    def _quit(event) -> None:
        event.app.exit()

    reload()
    app: Application = Application(
        layout=Layout(build_rows_window()),
        key_bindings=kb,
        full_screen=True,
    )
    app.run()
