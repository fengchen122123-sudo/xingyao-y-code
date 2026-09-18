"""唯一 Git 进程执行器（规格 §8）。

铁律：
- 参数数组启动，绝不用 Shell；
- 固定可执行文件与按作用域的子命令白名单（扩展必须改表并过评审）；
- 显式设置 --no-optional-locks / protocol.ext.allow=never；只读覆盖 core.fsmonitor=false；
  diff/log/show 默认 --no-ext-diff --no-textconv；
- stdin 默认关闭（子进程不得等待不可见终端输入），GIT_TERMINAL_PROMPT=0；
- 超时终止进程树，绝不自动重试；
- 错误归类为 GitErrorCode，命令摘要与错误消息一律脱敏。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from core.git.models import GitError, GitErrorCode, redact_text

MIN_GIT_VERSION: Tuple[int, int, int] = (2, 36, 0)

_GIT_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ExecScope(str, Enum):
    """执行作用域：决定环境加固与白名单（规格 §8/§9.2 兼容矩阵的分类依据）。"""

    READ_ONLY = "read_only"
    INDEX = "index"
    REF = "ref"
    WRITE = "write"
    NETWORK = "network"


# 按作用域的子命令白名单（第一层：args[0]）。动词级校验（如 stash list vs stash pop）
# 由唯一的上层调用方 query/mutation 层负责；runner 保证裸调不越过作用域边界。
_SCOPE_SUBCOMMANDS: Dict[ExecScope, frozenset] = {
    ExecScope.READ_ONLY: frozenset(
        {
            "status", "diff", "log", "show", "blame", "reflog", "rev-parse",
            "check-ref-format", "cat-file", "ls-files", "for-each-ref",
            "symbolic-ref", "rev-list", "merge-base", "worktree", "var",
            "version", "branch", "tag", "remote", "stash", "config",
            "describe", "shortlog", "ls-remote",
        }
    ),
    ExecScope.INDEX: frozenset(
        {"add", "restore", "reset", "apply", "update-index", "rm"}
    ),
    ExecScope.REF: frozenset({"branch", "tag", "update-ref", "symbolic-ref"}),
    ExecScope.WRITE: frozenset(
        {
            "commit", "switch", "checkout", "merge", "rebase", "cherry-pick",
            "revert", "reset", "stash", "clean", "restore", "apply", "rm",
            "mv", "worktree", "config", "branch", "tag", "remote", "notes",
        }
    ),
    ExecScope.NETWORK: frozenset(
        {"fetch", "pull", "push", "ls-remote", "clone", "remote", "archive"}
    ),
}

# diff/log/show 会触发外部 diff/textconv（规格 §8）
_NO_EXTERNAL_DIFF_COMMANDS = frozenset({"diff", "log", "show"})


@dataclass(frozen=True)
class GitProcessResult:
    """一次 Git 进程的原始结果。非零退出不等于异常，由调用方决定显式失败。"""

    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", "replace")


def parse_git_version(version_output: str) -> Tuple[int, int, int]:
    match = _GIT_VERSION_RE.search(version_output or "")
    if not match:
        raise ValueError(f"无法解析 git 版本输出: {version_output!r}")
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch or 0))


def with_paths(args: Sequence[str], paths: Iterable[str]) -> List[str]:
    """路径参数前加 ``--`` 终止选项解析（规格 §8）。"""
    return [*args, "--", *[str(p) for p in paths]]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
        else:
            import errno

            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError as exc:
                if exc.errno not in (errno.ESRCH, errno.EPERM):
                    raise
            proc.kill()
    except Exception:
        # 关闭动作吞掉一切异常（与在飞流中断同一铁律），超时错误本身照常抛出
        pass


class GitRunner:
    """进程执行器实例：可注入 git 可执行文件（测试/自定义路径）。"""

    def __init__(self, git_executable: str = "git"):
        self.git_executable = git_executable

    # ------------------------------------------------------------------ 公开入口

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        scope: ExecScope = ExecScope.READ_ONLY,
        timeout: float = 30.0,
        stdin: bool = False,
        env_extra: Optional[Dict[str, str]] = None,
    ) -> GitProcessResult:
        """白名单内的 Git 调用入口。非零退出返回结果，不抛异常。"""
        argv = [str(a) for a in args]
        if not argv:
            raise GitError(GitErrorCode.INVALID_STATE, "git 参数为空")
        self._check_whitelist(argv, scope)
        return self._execute(
            argv, cwd=cwd, scope=scope, timeout=timeout, stdin=stdin, env_extra=env_extra
        )

    def run_checked(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        scope: ExecScope = ExecScope.READ_ONLY,
        timeout: float = 30.0,
        stdin: bool = False,
        env_extra: Optional[Dict[str, str]] = None,
    ) -> GitProcessResult:
        """与 run 相同，但非零退出显式失败（规格 §4.3）。"""
        result = self.run(
            args, cwd=cwd, scope=scope, timeout=timeout, stdin=stdin, env_extra=env_extra
        )
        if result.returncode != 0:
            raise self.map_error(result, [str(a) for a in args])
        return result

    def execute_raw(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> GitProcessResult:
        """裸进程通道：不加 git 前缀、不做白名单与配置注入（仅用于 --version 探测与测试）。"""
        return self._execute([str(a) for a in args], cwd=cwd, scope=None, timeout=timeout, stdin=False, env_extra=None)

    def check_available(self) -> Tuple[int, int, int]:
        """探测 git 可执行与版本；缺失/不可解析显式失败。"""
        try:
            result = self.execute_raw([self.git_executable, "--version"], timeout=10.0)
        except GitError as error:
            if error.code is GitErrorCode.GIT_NOT_FOUND:
                raise
            raise GitError(
                GitErrorCode.GIT_NOT_FOUND,
                f"git 可执行探测失败: {error.message}",
            ) from error
        try:
            return parse_git_version(result.stdout_text)
        except ValueError as exc:
            raise GitError(
                GitErrorCode.GIT_NOT_FOUND,
                f"无法识别 git 版本输出: {redact_text(result.stdout_text.strip()[:120])}",
            ) from exc

    def ensure_version(
        self,
        minimum: Tuple[int, int, int],
        detected: Tuple[int, int, int],
    ) -> Tuple[int, int, int]:
        """低于基线显式报错并给升级指引，不用旧命令模拟（规格 §5.1）。"""
        if detected < minimum:
            raise GitError(
                GitErrorCode.GIT_VERSION_UNSUPPORTED,
                (
                    f"Git 版本过低：检测到 {detected[0]}.{detected[1]}.{detected[2]}，"
                    f"Git 工作台最低要求 {minimum[0]}.{minimum[1]}.{minimum[2]}，请升级 Git 后重试"
                ),
            )
        return detected

    def check_branch_name(self, name: str, *, cwd: str) -> None:
        """分支名交由 git check-ref-format 校验（规格 §5.5），不以自研正则替代。"""
        candidate = str(name)
        if not candidate or candidate.startswith("-"):
            # 选项注入形状的名字在进入 git 前本地拒绝
            raise GitError(
                GitErrorCode.INVALID_REF,
                f"非法分支名: {redact_text(candidate[:80])}",
            )
        try:
            self.run_checked(
                ["check-ref-format", "--branch", candidate],
                cwd=cwd,
                scope=ExecScope.READ_ONLY,
                timeout=10.0,
            )
        except GitError as error:
            raise GitError(
                GitErrorCode.INVALID_REF,
                f"git check-ref-format 拒绝分支名 {redact_text(candidate)}: {error.message}",
                exit_code=error.exit_code,
                command_summary=error.command_summary,
            ) from error

    def map_error(self, result: GitProcessResult, args: Sequence[str]) -> GitError:
        """退出码 + stderr 归类为 GitError，消息与命令摘要脱敏。

        兼容任何携带 returncode/stdout/stderr bytes 的结果对象。
        """
        raw_stderr = result.stderr if isinstance(result.stderr, bytes) else b""
        stderr = raw_stderr.decode("utf-8", "replace").strip()
        lowered = stderr.lower()
        summary = redact_text("git " + " ".join(str(a) for a in args[:6]))
        code = GitErrorCode.GIT_COMMAND_FAILED
        retryable = False
        if "not a git repository" in lowered:
            code = GitErrorCode.NOT_A_REPOSITORY
        elif (
            "authentication failed" in lowered
            or "could not read username" in lowered
            or "could not read password" in lowered
            or "terminal prompts disabled" in lowered
            or "http 401" in lowered
            or "http 403" in lowered
        ):
            code = GitErrorCode.AUTHENTICATION_REQUIRED
        elif "index.lock" in lowered or "another git process" in lowered:
            code = GitErrorCode.REPOSITORY_BUSY
            retryable = True
        elif (
            "could not resolve host" in lowered
            or "connection refused" in lowered
            or "connection reset" in lowered
            or "unable to access" in lowered
            or "timed out" in lowered
            or "failed to push some refs" in lowered
            or "(fetch first)" in lowered
        ):
            code = GitErrorCode.NETWORK_FAILED
        elif "permission denied" in lowered or "dubious ownership" in lowered:
            code = GitErrorCode.PERMISSION_DENIED
        elif (
            "does not match any" in lowered
            or "not a valid ref" in lowered
            or "invalid reference" in lowered
            or "ambiguous argument" in lowered
            or "src refspec" in lowered
        ):
            code = GitErrorCode.INVALID_REF
        return GitError(
            code=code,
            message=redact_text(stderr[:400]) or "git 命令失败",
            exit_code=result.returncode,
            command_summary=summary,
            retryable=retryable,
        )

    # ------------------------------------------------------------------ 内部

    def _check_whitelist(self, argv: List[str], scope: ExecScope) -> None:
        allowed = _SCOPE_SUBCOMMANDS.get(scope)
        if allowed is None:
            raise GitError(
                GitErrorCode.UNSAFE_GIT_EXTENSION,
                f"未知执行作用域: {scope!r}",
            )
        if argv[0] not in allowed:
            raise GitError(
                GitErrorCode.UNSAFE_GIT_EXTENSION,
                f"子命令 {redact_text(argv[0])} 不在 {scope.value} 作用域白名单内",
            )

    def _build_command(self, argv: List[str], scope: Optional[ExecScope]) -> List[str]:
        # 裸通道（scope=None）：原样执行，不加前缀、不注入任何配置
        if scope is None:
            return list(argv)
        cmd: List[str] = [self.git_executable]
        if scope is not ExecScope.NETWORK:
            cmd.append("--no-optional-locks")
        cmd += ["-c", "protocol.ext.allow=never"]
        if scope is ExecScope.READ_ONLY:
            cmd += ["-c", "core.fsmonitor=false"]
        if scope is ExecScope.READ_ONLY and argv[0] in _NO_EXTERNAL_DIFF_COMMANDS:
            cmd += ["--no-ext-diff", "--no-textconv"]
        cmd += argv
        return cmd

    def _build_env(self, env_extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        if env_extra:
            env.update(env_extra)
        return env

    def _execute(
        self,
        argv: List[str],
        *,
        cwd: Optional[str],
        scope: Optional[ExecScope],
        timeout: float,
        stdin: bool,
        env_extra: Optional[Dict[str, str]],
    ) -> GitProcessResult:
        cmd = self._build_command(argv, scope)
        env = self._build_env(env_extra)
        popen_kwargs: Dict = {
            "cwd": cwd,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.PIPE if stdin else subprocess.DEVNULL,
            "env": env,
            "shell": False,
        }
        if _CREATE_NO_WINDOW:
            popen_kwargs["creationflags"] = _CREATE_NO_WINDOW
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError as exc:
            raise GitError(
                GitErrorCode.GIT_NOT_FOUND,
                f"找不到 git 可执行文件: {self.git_executable}",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                GitErrorCode.TIMEOUT,
                f"git 进程启动超时: {redact_text(' '.join(argv[:4]))}",
            ) from exc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise GitError(
                GitErrorCode.TIMEOUT,
                f"git 命令超过 {timeout}s 超时上限，进程树已终止: {redact_text(' '.join(argv[:4]))}",
                command_summary=redact_text("git " + " ".join(argv[:6])),
            )
        return GitProcessResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout or b"",
            stderr=stderr or b"",
        )
