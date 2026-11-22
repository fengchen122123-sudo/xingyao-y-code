import fnmatch
import os
import re
import shlex

from collections.abc import Mapping

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
_SCRIPT_EVAL_EXECUTABLES = {"node", "nodejs", "perl", "ruby", "php"}
_SHELL_EXECUTABLES = {"sh", "bash", "zsh", "dash", "ksh", "ash"}
_POWERSHELL_EXECUTABLES = {"powershell", "pwsh"}
_SAFE_PYTHON_MODULES = frozenset({
    "py_compile",
    "compileall",
    "ast",
    "dis",
    "tokenize",
    "json.tool",
    "inspect",
    "pytest",
    "unittest",
    "mypy",
    "ruff",
    "flake8",
    "black",
    "isort",
})
_READONLY_PYTHON_MODULES = frozenset({
    "py_compile",
    "ast",
    "dis",
    "tokenize",
    "json.tool",
    "inspect",
})
_QUERY_FLAGS = {"-v", "--version", "-version", "-h", "--help"}
_UNSAFE_COMMAND_TEXT = (";", "&", "|", "<", ">", "`", "\r", "\n", "$(")
_PYTHON_OR_PIP = re.compile(r"(?:python|pip)(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z", re.IGNORECASE)
_QUERY_GIT_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "cat-file",
    "blame",
    "shortlog",
    "describe",
    "whatchanged",
    "grep",
    "diff-tree",
    "merge-base",
}
_GIT_BRANCH_WRITE_FLAGS = {
    "-d",
    "-m",
    "-c",
    "--delete",
    "--move",
    "--copy",
    "--edit-description",
    "--set-upstream-to",
    "--unset-upstream",
}
_GIT_BRANCH_QUERY_FLAGS = {
    "-a",
    "--all",
    "-r",
    "--remotes",
    "-l",
    "--list",
    "-v",
    "-vv",
    "--verbose",
    "--show-current",
    "--contains",
    "--no-contains",
    "--merged",
    "--no-merged",
    "--sort",
    "--format",
}
_GIT_TAG_QUERY_FLAGS = {"-l", "--list", "-n", "--sort", "--contains", "--points-at"}
_GIT_REMOTE_QUERY_SUBS = {"-v", "--verbose", "show", "get-url"}
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
    "dir",
    "findstr",
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
    "tree",
    "type",
    "wc",
    "where",
    "where.exe",
    "which",
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


def _is_git_command(executable: str) -> bool:
    return executable in {"git", "git.exe"}


def _has_inline_exec_flag(tokens: tuple[str, ...]) -> bool:
    executable = _normalized_executable(tokens[0])
    is_python = _is_python_or_pip(tokens[0])
    is_shell = executable in _SHELL_EXECUTABLES
    is_script_eval = executable in _SCRIPT_EVAL_EXECUTABLES
    i = 1
    while i < len(tokens):
        flag = tokens[i].lower()
        if flag in {"-command", "-encodedcommand"}:
            return True
        if flag in {"/c", "/k"} and executable == "cmd":
            return True
        if flag == "-c" and (is_python or is_shell or is_script_eval):
            return True
        if flag in {"-e", "--eval"} and (is_script_eval or is_shell):
            return True
        if flag == "-p" and executable in {"perl", "ruby"}:
            return True
        if flag in {"-p", "--print"} and executable in {"node", "nodejs"}:
            return True
        if flag == "-m" and is_python:
            module_name = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
            if module_name not in _SAFE_PYTHON_MODULES:
                return True
            i += 1
        i += 1
    return False


def _normalized_executable(token: str) -> str:
    executable = os.path.basename(token.strip('"')).lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable


def _classify_git_command(arguments: tuple[str, ...]) -> PermissionRisk:
    """精确区分只读 git 查询、安全选项查询与变更型 git 子命令。"""
    if not arguments or (len(arguments) == 1 and arguments[0] in _QUERY_FLAGS):
        return PermissionRisk.QUERY
    sub = arguments[0]
    if sub in _QUERY_GIT_SUBCOMMANDS:
        return PermissionRisk.QUERY
    rest = arguments[1:]
    if sub == "branch":
        if any(arg in _GIT_BRANCH_WRITE_FLAGS for arg in rest):
            return PermissionRisk.SENSITIVE_MUTATION
        # 无参数或仅包含只读查询标志时视为 QUERY；含位置参数视为新建分支
        for arg in rest:
            if arg.startswith("--") and arg.split("=", 1)[0] in _GIT_BRANCH_QUERY_FLAGS:
                continue
            if arg in _GIT_BRANCH_QUERY_FLAGS:
                continue
            return PermissionRisk.SENSITIVE_MUTATION
        return PermissionRisk.QUERY
    if sub == "tag":
        if not rest:
            return PermissionRisk.QUERY
        if all(arg in _GIT_TAG_QUERY_FLAGS for arg in rest):
            return PermissionRisk.QUERY
        return PermissionRisk.SENSITIVE_MUTATION
    if sub == "remote":
        if not rest or rest[0] in _GIT_REMOTE_QUERY_SUBS:
            return PermissionRisk.QUERY
        return PermissionRisk.SENSITIVE_MUTATION
    if sub == "stash":
        if rest and rest[0] in {"list", "show"}:
            return PermissionRisk.QUERY
        return PermissionRisk.SENSITIVE_MUTATION
    if sub == "config":
        if rest and any(arg in {"--get", "--get-all", "--list", "-l", "--show-origin"} for arg in rest):
            positional = [arg for arg in rest if not arg.startswith("-")]
            if "--list" in rest or "-l" in rest or len(positional) <= 1:
                return PermissionRisk.QUERY
        return PermissionRisk.SENSITIVE_MUTATION
    return PermissionRisk.SENSITIVE_MUTATION


def _classify_single_command_tokens(
    tokens: tuple[str, ...], *, allow_chdir_query: bool = False
) -> PermissionRisk:
    if _has_inline_exec_flag(tokens):
        return PermissionRisk.SENSITIVE_MUTATION

    executable = _normalized_executable(tokens[0])
    arguments = tuple(token.lower() for token in tokens[1:])

    if allow_chdir_query and executable in {"cd", "set-location"}:
        return PermissionRisk.QUERY

    if executable == "git":
        return _classify_git_command(arguments)

    if executable in _DELETE_COMMANDS or executable in _PROCESS_KILL_COMMANDS:
        return PermissionRisk.SENSITIVE_MUTATION

    if arguments and (executable, arguments[0]) in _PACKAGE_MUTATION_PAIRS:
        return PermissionRisk.SENSITIVE_MUTATION

    if _is_python_or_pip(tokens[0]) and "-m" in arguments:
        m_idx = arguments.index("-m")
        mod = arguments[m_idx + 1] if m_idx + 1 < len(arguments) else ""
        if mod in _READONLY_PYTHON_MODULES:
            return PermissionRisk.QUERY
        if mod in _SAFE_PYTHON_MODULES:
            return PermissionRisk.NORMAL_MUTATION

    if executable == "ruff" and arguments and arguments[0] == "check":
        if "--fix" not in arguments:
            return PermissionRisk.QUERY
        return PermissionRisk.NORMAL_MUTATION

    if executable == "tsc" and "--noemit" in arguments:
        return PermissionRisk.QUERY

    if executable == "pytest" and "--collect-only" in arguments:
        return PermissionRisk.QUERY

    if executable in _QUERY_COMMANDS:
        return PermissionRisk.QUERY

    if (
        executable in _VERSION_QUERY_COMMANDS
        and len(arguments) == 1
        and arguments[0] in _QUERY_FLAGS
    ):
        return PermissionRisk.QUERY

    return PermissionRisk.NORMAL_MUTATION


def _split_safe_compound_segments(command: str) -> list[tuple[str, ...]] | None:
    if not isinstance(command, str) or not command.strip():
        return None

    segments_raw: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    i = 0
    length = len(command)
    while i < length:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < length else ""
        if ch in {"\r", "\n"}:
            return None
        if not in_single and (command[i : i + 2] in {"$(", "${"} or ch == "`"):
            return None
        if os.name != "nt" and not in_single and ch == "\\" and i + 1 < length:
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if ch in {"<", ">"}:
                return None
            if ch == "&":
                if nxt == "&":
                    segments_raw.append("".join(buf))
                    buf.clear()
                    i += 2
                    continue
                return None
            if ch == "|":
                segments_raw.append("".join(buf))
                buf.clear()
                i += 2 if nxt == "|" else 1
                continue
            if ch == ";":
                segments_raw.append("".join(buf))
                buf.clear()
                i += 1
                continue
        buf.append(ch)
        i += 1

    if in_single or in_double:
        return None
    segments_raw.append("".join(buf))

    parsed: list[tuple[str, ...]] = []
    for raw in segments_raw:
        text = raw.strip()
        if not text:
            return None
        try:
            tokens = tuple(shlex.split(text, posix=os.name != "nt"))
        except ValueError:
            return None
        if not tokens:
            return None
        parsed.append(tokens)
    return parsed or None


_RISK_SEVERITY = {
    PermissionRisk.QUERY: 0,
    PermissionRisk.NORMAL_MUTATION: 1,
    PermissionRisk.SENSITIVE_MUTATION: 2,
    PermissionRisk.CATASTROPHIC: 3,
}


def classify_command_risk(command: str, *, workdir=None, home=None) -> PermissionRisk:
    """Classify a shell command without executing or expanding it."""
    if is_dangerous_command(command, workdir=workdir, home=home):
        return PermissionRisk.CATASTROPHIC

    tokens = _safe_tokens(command)
    if tokens is not None:
        return _classify_single_command_tokens(tokens)

    segments = _split_safe_compound_segments(command)
    if not segments:
        return PermissionRisk.SENSITIVE_MUTATION

    max_risk = PermissionRisk.QUERY
    last_idx = len(segments) - 1
    for idx, seg_tokens in enumerate(segments):
        risk = _classify_single_command_tokens(
            seg_tokens, allow_chdir_query=(idx < last_idx)
        )
        if _RISK_SEVERITY[risk] > _RISK_SEVERITY[max_risk]:
            max_risk = risk
    return max_risk


def parse_manual_command_rule(command: str) -> tuple[str, ...]:
    if is_dangerous_command(command):
        raise ValueError("灾难性命令不能加入白名单")

    tokens = _safe_tokens(command)
    if tokens is None:
        raise ValueError("命令白名单不支持管道、串联、重定向或变量展开")
    if _has_inline_exec_flag(tokens):
        raise ValueError("内联解释器命令不能加入白名单")
    return tokens


def session_rule_for_command(command: str) -> tuple[str, ...] | None:
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


SHELL_ENV_POLICY_CONFIG_KEY = "shell_env_policy"
DEFAULT_SHELL_ENV_POLICY = {"inherit": "all", "exclude_sensitive": True}
DEFAULT_SENSITIVE_ENV_PATTERNS = ("*KEY*", "*SECRET*", "*TOKEN*", "*PASSWORD*")


def _env_pattern_list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _env_name_matches(name: str, patterns: list[str]) -> bool:
    upper = str(name).upper()
    return any(fnmatch.fnmatchcase(upper, pattern.upper()) for pattern in patterns)


def build_child_env(
    base_env: Mapping[str, str], *, policy: dict | None = None
) -> dict[str, str]:
    options = policy if isinstance(policy, dict) else {}
    base = dict(base_env) if isinstance(base_env, Mapping) else {}

    inherit = str(options.get("inherit", "all") or "all").strip().lower()
    if inherit == "none":
        env: dict[str, str] = {}
    else:
        env = {str(key): str(value) for key, value in base.items()}

    exclude_patterns: list[str] = []
    if options.get("exclude_sensitive", True):
        exclude_patterns.extend(DEFAULT_SENSITIVE_ENV_PATTERNS)
    exclude_patterns.extend(_env_pattern_list(options.get("exclude")))
    for name in list(env):
        if _env_name_matches(name, exclude_patterns):
            del env[name]

    include_only = _env_pattern_list(options.get("include_only"))
    if include_only:
        env = {
            name: value
            for name, value in env.items()
            if _env_name_matches(name, include_only)
        }

    fixed = options.get("set")
    if isinstance(fixed, Mapping):
        env.update({str(key): str(value) for key, value in fixed.items()})
    return env


def load_shell_env_policy() -> dict:
    resolved = dict(DEFAULT_SHELL_ENV_POLICY)
    try:
        import config

        data = config._load()
    except Exception:
        data = None
    user_policy = (
        data.get(SHELL_ENV_POLICY_CONFIG_KEY) if isinstance(data, dict) else None
    )
    if isinstance(user_policy, dict):
        resolved.update(user_policy)
    return resolved
