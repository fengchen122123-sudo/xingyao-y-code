"""Git worktree 管理模块。

设计目标：让多会话并行工作在不同分支，互不干扰。
参考：OpenCode 的 worktree 模块

用法：
- create_worktree(branch, path): 创建新分支的 worktree
- list_worktrees(): 列出所有 worktree
- remove_worktree(path): 移除 worktree
- 在 config.json 配置默认 worktree 根目录

与 OpenCode 区别：
- 不做自动 worktree 切换（保持简单，用户手动管理）
- 不绑定 session（避免引入复杂的状态管理）
"""

import os
import subprocess
from pathlib import Path

from core.workspace_context import get_workdir


def _run_git(args, cwd=None):
    """运行 git 命令，返回 (returncode, stdout, stderr)。"""
    try:
        result = subprocess.run(
            ['git'] + args,
            cwd=cwd or str(get_workdir()),
            capture_output=True, text=True,
            encoding='utf-8', errors='replace',
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return -1, '', 'git 命令未找到，请确保已安装 Git'
    except subprocess.TimeoutExpired:
        return -2, '', 'git 命令执行超时'
    except Exception as e:
        return -3, '', f'git 命令执行异常: {e}'


def _is_git_repo(cwd=None):
    """判断当前目录是否是 git 仓库。"""
    code, _, _ = _run_git(['rev-parse', '--git-dir'], cwd=cwd)
    return code == 0


def _is_option_like(value):
    """T4 修复：以 - 开头的字符串会被 git 解析为选项（如 --force），必须拒绝。"""
    return isinstance(value, str) and value.startswith('-')


def create_worktree(branch, path, base_branch=None, cwd=None):
    """创建 git worktree，让指定分支在新路径工作。

    参数：
    - branch: 新分支名（会自动创建）
    - path: worktree 路径（相对路径相对于当前工作目录）
    - base_branch: 从哪个分支创建新分支（默认当前 HEAD）
    - cwd: git 仓库根目录（默认当前工作目录）

    返回：操作结果描述
    """
    if not _is_git_repo(cwd):
        return "🚨 当前目录不是 git 仓库"

    # T4 修复：先拒选项式字符串，再做字符集校验——
    # 旧校验只查字符集，"--force" 去掉 - 后是纯字母可通过，会被 git 当作选项注入。
    if _is_option_like(branch):
        return f"🚨 分支名不能以 - 开头（会被 git 解析为选项）：{branch!r}"
    if not branch or not branch.replace('-', '').replace('_', '').replace('/', '').replace('.', '').isalnum():
        return f"🚨 分支名只能包含字母、数字、下划线、连字符、斜杠、点号：{branch!r}"
    if base_branch and _is_option_like(base_branch):
        return f"🚨 基分支名不能以 - 开头（会被 git 解析为选项）：{base_branch!r}"
    if not path or _is_option_like(str(path)):
        return f"🚨 worktree 路径不合法：{path!r}"

    # 解析 worktree 路径（相对路径应相对会话工作目录，而非进程当前目录）
    repo_root = Path(cwd or get_workdir()).resolve()
    abs_path = Path(repo_root, path).resolve()
    # T4 修复：规范化后的边界校验——不能覆盖仓库根目录，也不能落在 .git 目录内
    if abs_path == repo_root:
        return "🚨 worktree 路径不能是仓库根目录本身"
    if (repo_root / '.git') in abs_path.parents:
        return "🚨 worktree 路径不能位于 .git 目录内"
    if abs_path.exists():
        return f"🚨 目标路径已存在: {abs_path}"

    # 确保父目录存在
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    # git worktree add [-b <new-branch>] <path> [<base>]
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

    # 解析 porcelain 格式：worktree <path>\nHEAD <sha>\nbranch <ref>\n\n
    worktrees = []
    current = {}
    for line in stdout.split('\n'):
        if not line.strip():
            if current:
                worktrees.append(current)
                current = {}
            continue
        if line.startswith('worktree '):
            current['path'] = line[len('worktree '):]
        elif line.startswith('HEAD '):
            current['head'] = line[len('HEAD '):][:8]
        elif line.startswith('branch '):
            current['branch'] = line[len('branch '):].replace('refs/heads/', '')

    if current:
        worktrees.append(current)

    if not worktrees:
        return "当前仓库没有 worktree"

    lines = [f"当前仓库共有 {len(worktrees)} 个 worktree:"]
    for i, wt in enumerate(worktrees, 1):
        branch = wt.get('branch', '(detached)')
        lines.append(f"  [{i}] {wt.get('path', '?')}")
        lines.append(f"      分支: {branch}  HEAD: {wt.get('head', '?')}")
    return '\n'.join(lines)


def remove_worktree(path, force=False, cwd=None):
    """移除 git worktree。

    参数：
    - path: worktree 路径
    - force: 强制移除（即使有未提交修改）
    - cwd: git 仓库根目录
    """
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
