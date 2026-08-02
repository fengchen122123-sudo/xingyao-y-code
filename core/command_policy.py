"""Pure policy helpers for session-scoped command authorization."""

import os
import re
import shlex


_INLINE_EXEC_FLAGS = {
    "-c",
    "-e",
    "--eval",
    "-command",
    "-encodedcommand",
    "/c",
    "-m",
    "-p",
}
_QUERY_FLAGS = {"-v", "--version", "-version", "-h", "--help"}
_UNSAFE_COMMAND_TEXT = (";", "&", "|", "<", ">", "`", "\r", "\n", "$(")
_PYTHON_OR_PIP = re.compile(r"(?:python|pip)(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z", re.IGNORECASE)


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
