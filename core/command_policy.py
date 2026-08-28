"""Pure policy helpers for session-scoped command authorization."""

import os
import re
import shlex

from core.permission_policy import PermissionRisk


_INLINE_EXEC_FLAGS = {
    "-c",
    "-e",
    "--eval",
    "-command",
    "-encodedcommand",
    "/c",
    "/k",
    "-m",
    "-p",
}
_QUERY_FLAGS = {"-v", "--version", "-version", "-h", "--help"}
_UNSAFE_COMMAND_TEXT = (";", "&", "|", "<", ">", "`", "\r", "\n", "$(")
_PYTHON_OR_PIP = re.compile(r"(?:python|pip)(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z", re.IGNORECASE)
_QUERY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "rev-parse", "ls-files"}
_DELETE_COMMANDS = {"rm", "del", "erase", "rd", "rmdir", "remove-item"}
_PROCESS_KILL_COMMANDS = {"kill", "pkill", "taskkill", "stop-process"}
_PACKAGE_MUTATION_PAIRS = {
    ("pip", "install"),
    ("pip", "uninstall"),
    ("npm", "install"),
    ("npm", "uninstall"),
    ("npm", "remove"),
    ("npm", "update"),
    ("pnpm", "add"),
    ("pnpm", "install"),
    ("pnpm", "remove"),
    ("yarn", "add"),
    ("yarn", "install"),
    ("yarn", "remove"),
    ("cargo", "install"),
    ("cargo", "uninstall"),
}
_QUERY_COMMANDS = {
    "cat",
    "get-childitem",
    "get-content",
    "head",
    "ls",
    "pwd",
    "rg",
    "ripgrep",
    "select-string",
    "tail",
    "test-path",
    "type",
    "where",
    "where.exe",
    "whoami",
}
_VERSION_QUERY_COMMANDS = {
    "cargo",
    "java",
    "mvn",
    "node",
    "npm",
    "pip",
    "pnpm",
    "python",
    "yarn",
}

# ---------------------------------------------------------------------------
# 敏感信息脱敏：命令回显/日志/持久化前屏蔽内联凭据，
# 避免 export TOKEN=sk-... 这类命令把密钥明文喂回模型或写入磁盘记录
# ---------------------------------------------------------------------------
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{4,}")
_SENSITIVE_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|pwd)"
    r"(\s*[=:]\s*['\"]?)([^\s'\";&|]+)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def redact_sensitive_text(text):
    """对命令/日志文本中的内联凭据做脱敏（替换为 ***）。"""
    redacted = str(text or "")
    redacted = _SK_KEY_RE.sub("sk-***", redacted)
    redacted = _SENSITIVE_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", redacted)
    redacted = _BEARER_RE.sub(lambda m: f"{m.group(1)}***", redacted)
    return redacted

_CATASTROPHIC_COMMAND_PATTERNS = (
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|:\s*&\s*\}\s*;\s*:", re.IGNORECASE),
    re.compile(
        r">\s*/dev/(?:[sh]d[a-z]|nvme\d+n\d+|mmcblk\d+|loop\d+|rdisk\d+|disk\d+)",
        re.IGNORECASE,
    ),
    re.compile(r"\bdd\s+[^\r\n]*\bof\s*=\s*/dev/", re.IGNORECASE),
    re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\s+", re.IGNORECASE),
    re.compile(
        r"\b(?:curl|wget)\b[^\r\n|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|ksh)\b",
        re.IGNORECASE,
    ),
    # 下载后交给脚本解释器执行（sh 家族之外的变体）
    re.compile(
        r"\b(?:curl|wget)\b[^\r\n|]*\|\s*(?:sudo\s+)?(?:python\d*(?:\.exe)?|perl|ruby)\b",
        re.IGNORECASE,
    ),
    # base64 解码后管道执行：载荷被编码混淆，静态审查不可见
    re.compile(
        r"\bbase64\b[^\r\n|]*(?:\s-d\b|--decode\b)[^\r\n]*\|\s*(?:sudo\s+)?"
        r"(?:sh|bash|zsh|ksh|python\d*(?:\.exe)?|perl)\b",
        re.IGNORECASE,
    ),
    # PowerShell -EncodedCommand：base64 载荷无法静态审查，一律视为危险
    re.compile(
        r"\b(?:powershell|pwsh)(?:\.exe)?\b[^\r\n]*\s-(?:enc|ec|encodedcommand)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:format(?:\.com)?\s+[a-z]:|reboot\b|halt\b|poweroff\b|shutdown\s+(?:/s|/r|-h|-r)\b)", re.IGNORECASE),
)


def _shell_segments(text):
    return [part.strip() for part in re.split(r"(?:&&|\|\||[;&])", text) if part.strip()]


def _normalize_delete_target(target, *, workdir=None):
    text = str(target or "").strip().strip("\"'")
    if not text:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(text))
    if not os.path.isabs(expanded):
        try:
            if workdir is None:
                from core.workspace_context import get_workdir

                workdir = str(get_workdir())
            expanded = os.path.join(str(workdir), expanded)
        except Exception:
            return ""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(expanded)))
    except (OSError, TypeError, ValueError):
        return ""


def _is_protected_delete_target(target, *, workdir=None, home=None):
    candidate = _normalize_delete_target(target, workdir=workdir)
    if not candidate:
        return False
    protected_values = [workdir]
    protected_values.append(home if home is not None else os.path.expanduser("~"))
    for protected_value in protected_values:
        protected = _normalize_delete_target(protected_value, workdir=workdir)
        if not protected:
            continue
        try:
            common = os.path.commonpath((candidate, protected))
        except ValueError:
            continue
        if os.path.normcase(common) == candidate:
            return True
    return False


def _dangerous_rm(segment, *, workdir=None, home=None):
    try:
        tokens = shlex.split(segment, posix=os.name != "nt")
    except ValueError:
        return False
    if tokens and tokens[0].lower() == "sudo":
        tokens = tokens[1:]
    if not tokens or tokens[0].lower() != "rm":
        return False
    recursive = force = False
    targets = []
    for token in tokens[1:]:
        lower = token.lower()
        if lower in {"--recursive", "--force", "--no-preserve-root", "--preserve-root"}:
            recursive |= lower == "--recursive"
            force |= lower == "--force"
        elif token.startswith("-") and token != "-":
            flags = token.lstrip("-").lower()
            recursive |= "r" in flags
            force |= "f" in flags
        else:
            targets.append(token)
    # 灾难性目标：根路径/家目录/通配符，以及纯变量展开目标（$VAR/${VAR}/%VAR%）。
    # 纯变量目标的实际取值静态不可见，递归强制删除等同于未审查的 rm -rf /。
    catastrophic_target = re.compile(
        r"^(?:/(?:\*|\.\*|\?)?|~(?:/(?:\*|\.\*|\?))?|\$HOME(?:/(?:\*|\.\*|\?))?|\.(?:/(?:\*|\.\*|\?))?|\*|\.\*|\?"
        r"|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%)$",
        re.IGNORECASE,
    )
    return recursive and force and any(
        catastrophic_target.fullmatch(target.strip("\"'"))
        or _is_protected_delete_target(target, workdir=workdir, home=home)
        for target in targets
    )


def _dangerous_windows_delete(segment, *, workdir=None, home=None):
    command_match = re.match(r"^(?:cmd(?:\.exe)?\s+/(?:c|k)\s+)?(del|erase|rd|rmdir)\b(.*)$", segment, re.IGNORECASE)
    if not command_match:
        return False
    body = command_match.group(2)
    flags = set(re.findall(r"(?:^|\s)/([a-z])(?=\s|$)", body, re.IGNORECASE))
    root_target = re.search(r"(?:^|\s)[\"']?(?:[a-z]:\\|\\\\)[\"']?(?=\s|$)", body, re.IGNORECASE)
    recursive = "s" in {flag.lower() for flag in flags}
    forceful = command_match.group(1).lower() in {"rd", "rmdir"} or "q" in {
        flag.lower() for flag in flags
    }
    try:
        body_tokens = shlex.split(body, posix=False)
    except ValueError:
        body_tokens = []
    targets = [token for token in body_tokens if not token.startswith("/")]
    protected_target = any(
        _is_protected_delete_target(target, workdir=workdir, home=home)
        for target in targets
    )
    return recursive and forceful and (bool(root_target) or protected_target)


def _dangerous_powershell_delete(segment, *, workdir=None, home=None):
    command = re.match(r"^\s*(?:powershell|pwsh)(?:\.exe)?\b", segment, re.IGNORECASE)
    if command:
        match = re.search(r"\b(Remove-Item|Format-Volume)\b(.*)$", segment, re.IGNORECASE)
    else:
        match = re.match(r"^\s*(Remove-Item|Format-Volume)\b(.*)$", segment, re.IGNORECASE)
    if not match:
        return False
    if match.group(1).lower() == "format-volume":
        return True
    body = match.group(2)
    recursive = re.search(
        r"(?:^|\s)-(?:Recurse|R)(?=\s|$|[\"'])",
        body,
        re.IGNORECASE,
    ) is not None
    forceful = re.search(
        r"(?:^|\s)-(?:Force|F)(?=\s|$|[\"'])",
        body,
        re.IGNORECASE,
    ) is not None
    root_target = re.search(
            r"(?:^|\s|[\"'])(?:[a-z]:\\+|\\\\|~(?:[/\\]|$)|\$HOME(?:[/\\]|$)|[*.?])(?=\s|$|[\"'])",
            body,
            re.IGNORECASE,
        ) is not None
    try:
        body_tokens = shlex.split(body, posix=False)
    except ValueError:
        body_tokens = []
    targets = []
    consume_path = False
    for token in body_tokens:
        lowered = token.lower()
        if consume_path:
            targets.append(token)
            consume_path = False
        elif lowered in {"-path", "-literalpath"}:
            consume_path = True
        elif not token.startswith("-"):
            targets.append(token)
    protected_target = any(
        _is_protected_delete_target(target, workdir=workdir, home=home)
        for target in targets
    )
    return recursive and forceful and (root_target or protected_target)


def is_dangerous_command(command: str, *, workdir=None, home=None) -> bool:
    """Identify unambiguously catastrophic shell operations.

    This is defense in depth, not a complete shell sandbox. Permission
    confirmation remains required for mutating commands that are not matched.
    """
    text = str(command or "").strip()
    if not text:
        return False
    if any(pattern.search(text) for pattern in _CATASTROPHIC_COMMAND_PATTERNS):
        return True
    return any(
        _dangerous_rm(segment, workdir=workdir, home=home)
        or _dangerous_windows_delete(segment, workdir=workdir, home=home)
        or _dangerous_powershell_delete(segment, workdir=workdir, home=home)
        for segment in _shell_segments(text)
    )


def _safe_tokens(command: str) -> tuple[str, ...] | None:
    if not isinstance(command, str) or not command.strip():
        return None
    if any(fragment in command for fragment in _UNSAFE_COMMAND_TEXT):
        return None
    try:
        tokens = tuple(shlex.split(command, posix=os.name != "nt"))
    except ValueError:
        return None
    return tokens or None


def _is_python_or_pip(executable: str) -> bool:
    name = os.path.basename(executable.strip('"'))
    return _PYTHON_OR_PIP.fullmatch(name) is not None


def _has_inline_exec_flag(tokens: tuple[str, ...]) -> bool:
    executable = os.path.basename(tokens[0].strip('"')).lower()
    for argument in tokens[1:]:
        flag = argument.lower()
        if flag == "-c" and executable in {"git", "git.exe"}:
            continue
        if flag in _INLINE_EXEC_FLAGS:
            return True
    return False


def _normalized_executable(token: str) -> str:
    executable = os.path.basename(token.strip('"')).lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable


def classify_command_risk(command: str, *, workdir=None, home=None) -> PermissionRisk:
    """Classify a shell command without executing or expanding it."""
    if is_dangerous_command(command, workdir=workdir, home=home):
        return PermissionRisk.CATASTROPHIC

    tokens = _safe_tokens(command)
    if tokens is None or _has_inline_exec_flag(tokens):
        return PermissionRisk.SENSITIVE_MUTATION

    executable = _normalized_executable(tokens[0])
    arguments = tuple(token.lower() for token in tokens[1:])

    if executable == "git":
        if not arguments or (
            len(arguments) == 1 and arguments[0] in _QUERY_FLAGS
        ):
            return PermissionRisk.QUERY
        if arguments[0] in _QUERY_GIT_SUBCOMMANDS:
            return PermissionRisk.QUERY
        return PermissionRisk.SENSITIVE_MUTATION

    if executable in _DELETE_COMMANDS or executable in _PROCESS_KILL_COMMANDS:
        return PermissionRisk.SENSITIVE_MUTATION

    if arguments and (executable, arguments[0]) in _PACKAGE_MUTATION_PAIRS:
        return PermissionRisk.SENSITIVE_MUTATION

    if executable in _QUERY_COMMANDS:
        return PermissionRisk.QUERY

    if (
        executable in _VERSION_QUERY_COMMANDS
        and len(arguments) == 1
        and arguments[0] in _QUERY_FLAGS
    ):
        return PermissionRisk.QUERY

    return PermissionRisk.NORMAL_MUTATION


def parse_manual_command_rule(command: str) -> tuple[str, ...]:
    """Parse a complete, persistable token prefix entered by the user."""
    if is_dangerous_command(command):
        raise ValueError("灾难性命令不能加入白名单")

    tokens = _safe_tokens(command)
    if tokens is None:
        raise ValueError("命令白名单不支持管道、串联、重定向或变量展开")
    if _has_inline_exec_flag(tokens):
        raise ValueError("内联解释器命令不能加入白名单")
    return tokens


def session_rule_for_command(command: str) -> tuple[str, ...] | None:
    """Return the narrow session rule for a safe, non-composed command."""
    tokens = _safe_tokens(command)
    if tokens is None:
        return None

    if _has_inline_exec_flag(tokens):
        return None

    executable = tokens[0]
    if len(tokens) == 1:
        return (executable,)
    if any(token.lower() in _QUERY_FLAGS for token in tokens[1:]):
        return tokens
    if tokens[1].startswith("-"):
        return tokens
    if _is_python_or_pip(executable):
        return executable, tokens[1]
    return executable, tokens[1]


def command_matches_session_rule(command: str, rule: tuple[str, ...]) -> bool:
    """Match a command against complete token elements of a session rule."""
    if not isinstance(rule, tuple) or not rule or not all(isinstance(item, str) for item in rule):
        return False
    tokens = _safe_tokens(command)
    if tokens is None:
        return False
    if _has_inline_exec_flag(tokens):
        return False
    if _session_rule_is_exact(rule):
        return tokens == rule
    return len(tokens) >= len(rule) and tokens[: len(rule)] == rule


def _session_rule_is_exact(rule: tuple[str, ...]) -> bool:
    return (
        len(rule) == 1
        or (len(rule) > 1 and rule[1].startswith("-"))
        or any(token.lower() in _QUERY_FLAGS for token in rule[1:])
    )
