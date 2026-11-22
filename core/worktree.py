from pathlib import Path

from core.git.query_refs import parse_worktree_porcelain

from core.git.runner import ExecScope, GitRunner

_RUNNER = GitRunner()


# 只读子命令 → READ_ONLY 作用域（享受 fsmonitor/外部 diff 加固），其余 WRITE
_READONLY_VERBS = {'rev-parse', 'status', 'log', 'diff'}


def _run_git(args, cwd=None):
    try:
        is_readonly = bool(args) and (
            args[0] in _READONLY_VERBS
            or (args[0] == 'worktree' and len(args) > 1 and args[1] == 'list')
        )
        scope = ExecScope.READ_ONLY if is_readonly else ExecScope.WRITE
        result = _RUNNER.run(args, cwd=cwd or str(get_workdir()), scope=scope, timeout=30)
        return result.returncode, result.stdout_text.strip(), result.stderr_text.strip()
    except Exception as e:
        from core.git.models import GitError, GitErrorCode

        if isinstance(e, GitError):
            if e.code is GitErrorCode.GIT_NOT_FOUND:
                return -1, '', 'git 命令未找到，请确保已安装 Git'
            if e.code is GitErrorCode.TIMEOUT:
                return -2, '', 'git 命令执行超时'
        return -3, '', f'git 命令执行异常: {e}'


def _is_git_repo(cwd=None):
    code, _, _ = _run_git(['rev-parse', '--git-dir'], cwd=cwd)
    return code == 0


def _is_option_like(value):
    """T4 修复：以 - 开头的字符串会被 git 解析为选项（如 --force），必须拒绝。"""
    return isinstance(value, str) and value.startswith('-')


def create_worktree(branch, path, base_branch=None, cwd=None):
    if not _is_git_repo(cwd):
        return "🚨 当前目录不是 git 仓库"

    if _is_option_like(branch):
        return f"🚨 分支名不能以 - 开头（会被 git 解析为选项）：{branch!r}"
    if not branch or not branch.replace('-', '').replace('_', '').replace('/', '').replace('.', '').isalnum():
        return f"🚨 分支名只能包含字母、数字、下划线、连字符、斜杠、点号：{branch!r}"
    if base_branch and _is_option_like(base_branch):
        return f"🚨 基分支名不能以 - 开头（会被 git 解析为选项）：{base_branch!r}"
    if not path or _is_option_like(str(path)):
        return f"🚨 worktree 路径不合法：{path!r}"

    repo_root = Path(cwd or get_workdir()).resolve()
    abs_path = Path(repo_root, path).resolve()
    # T4 修复：规范化后的边界校验——不能覆盖仓库根目录，也不能落在 .git 目录内
    if abs_path == repo_root:
        return "🚨 worktree 路径不能是仓库根目录本身"
    if (repo_root / '.git') in abs_path.parents:
        return "🚨 worktree 路径不能位于 .git 目录内"
    if abs_path.exists():
        return f"🚨 目标路径已存在: {abs_path}"

    abs_path.parent.mkdir(parents=True, exist_ok=True)

    args = ['worktree', 'add']
    if base_branch:
        args.extend(['-b', branch, str(abs_path), base_branch])
    else:
        args.extend(['-b', branch, str(abs_path)])

    code, stdout, stderr = _run_git(args, cwd=cwd)
    if code != 0:
        return f"🚨 创建 worktree 失败: {stderr or stdout}"

    return (
        f"✔ 已创建 worktree:\n"
        f"  分支: {branch}\n"
        f"  路径: {abs_path}\n"
        f"  基分支: {base_branch or '当前 HEAD'}\n"
        f"  在新终端 cd 到该路径即可开始工作"
    )


def list_worktrees(cwd=None):
    """列出当前仓库的所有 worktree。"""
    if not _is_git_repo(cwd):
        return "🚨 当前目录不是 git 仓库"

    code, stdout, stderr = _run_git(['worktree', 'list', '--porcelain'], cwd=cwd)
    if code != 0:
        return f"🚨 列出 worktree 失败: {stderr or stdout}"

    parsed = parse_worktree_porcelain(stdout)
    worktrees = [
        {'path': wt.path, 'head': wt.head_oid[:8], 'branch': wt.branch}
        for wt in parsed
    ]

    if not worktrees:
        return "当前仓库没有 worktree"

    lines = [f"当前仓库共有 {len(worktrees)} 个 worktree:"]
    for i, wt in enumerate(worktrees, 1):
        branch = wt.get('branch', '(detached)')
        lines.append(f"  [{i}] {wt.get('path', '?')}")
        lines.append(f"      分支: {branch}  HEAD: {wt.get('head', '?')}")
    return '\n'.join(lines)


def remove_worktree(path, force=False, cwd=None):
    if not _is_git_repo(cwd):
        return "🚨 当前目录不是 git 仓库"

    # T4 修复：拒选项式字符串，防止被 git 解析为额外 flag
    if not path or _is_option_like(str(path)):
        return f"🚨 worktree 路径不合法：{path!r}"

    abs_path = Path(cwd or get_workdir(), path).resolve()
    args = ['worktree', 'remove']
    if force:
        args.append('--force')
    args.append(str(abs_path))

    code, stdout, stderr = _run_git(args, cwd=cwd)
    if code != 0:
        return f"🚨 移除 worktree 失败: {stderr or stdout}"

    return f"✔ 已移除 worktree: {abs_path}"


def prune_worktrees(cwd=None):
    """清理已失效的 worktree 引用（路径已被删除但 git 还记录着）。"""
    if not _is_git_repo(cwd):
        return "🚨 当前目录不是 git 仓库"

    code, stdout, stderr = _run_git(['worktree', 'prune', '-v'], cwd=cwd)
    if code != 0:
        return f"🚨 清理 worktree 失败: {stderr or stdout}"

    if not stdout:
        return "✔ 没有需要清理的失效 worktree"
    return f"✔ 已清理失效 worktree:\n{stdout}"
