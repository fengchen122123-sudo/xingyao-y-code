from __future__ import annotations

import json
import locale
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from core.hooks.types import HookInput, HookOutput, HookRegistration

_logger = logging.getLogger("ycode.hooks")

#: 命令预览的最大长度：warning 里只放截断后的单行命令，避免日志被超长命令撑爆。
_COMMAND_PREVIEW_LIMIT = 120

_TEMPLATE_VARS: dict[str, str] = {
    "YCODE_PROJECT_DIR": "cwd",
    "ZCODE_PROJECT_DIR": "cwd",
    "CLAUDE_PROJECT_DIR": "cwd",
    "YCODE_SESSION_ID": "session_id",
    "CLAUDE_SESSION_ID": "session_id",
}

_TEMPLATE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def command_preview(command: str, limit: int = _COMMAND_PREVIEW_LIMIT) -> str:
    """单行截断的命令预览（供 warning / failures 使用，不含 stdin 正文）。"""
    single_line = " ".join(str(command).split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1] + "…"


@dataclass(frozen=True)
class HookExecutionOutcome:

    status: str
    output: HookOutput | None = None
    exit_code: int | None = None
    message: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _resolve_cwd(cwd) -> str | None:
    if not cwd:
        return None
    try:
        path = Path(str(cwd))
        if path.is_dir():
            return str(path)
    except OSError:
        return None
    return None


def _parse_stdout(raw: bytes | None) -> HookOutput | None:
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # 用户 hook 脚本在 Windows 上常按 locale 编码（GBK 等）print 中文——
        # 先按 UTF-8 严格解码，失败时回退 locale 编码（非法序列容错替换）。
        text = raw.decode(locale.getpreferredencoding(False), errors="replace")
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return HookOutput.from_wire(data)


def _template_values(hook_input: HookInput) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, field in _TEMPLATE_VARS.items():
        value = getattr(hook_input, field, "") or ""
        if value:
            values[name] = str(value)
    return values


def expand_template_variables(command: str, hook_input: HookInput) -> str:
    values = _template_values(hook_input)

    def _sub(match: re.Match) -> str:
        value = values.get(match.group(1))
        return match.group(0) if value is None else value

    return _TEMPLATE_PATTERN.sub(_sub, command)


def _build_hook_env(hook_input: HookInput) -> dict[str, str]:
    env = dict(os.environ)
    env.update(_template_values(hook_input))
    return env


def _stderr_tail(raw: bytes | None, limit: int = 200) -> str:
    fallback = "hook 以退出码 2 请求阻断"
    if not raw:
        return fallback
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(locale.getpreferredencoding(False), errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return fallback
    tail = lines[-1]
    return tail if len(tail) <= limit else tail[: limit - 1] + "…"


def run_hook(
    registration: HookRegistration,
    hook_input: HookInput,
    *,
    cancel_event=None,
    logger: logging.Logger | None = None,
) -> HookExecutionOutcome:
    log = logger or _logger
    if cancel_event is not None and cancel_event.is_set():
        return HookExecutionOutcome(status="skipped", message="已取消，未执行")

    # ASCII 安全序列化（ensure_ascii=True）：中文等非 ASCII 字符转成 \uXXXX 转义。
    # 为什么必须如此：hook 子进程在 Windows 中文环境下 sys.stdin 默认按 locale
    # （GBK/cp936）解码；若写入原样 UTF-8 字节，子进程解码含中文载荷时可能抛
    # UnicodeDecodeError（hook 直接崩溃、退出码 1），且是否崩溃还取决于路径等
    # 字节序列的相位——极难排查。全 ASCII 载荷在任何编码下都能被无歧义解析。
    payload = json.dumps(
        hook_input.to_wire(), separators=(",", ":")
    ).encode("ascii")
    timeout_s = max(0.001, registration.timeout_ms / 1000.0)
    command = expand_template_variables(registration.command, hook_input)
    kwargs = {
        "shell": True,
        "input": payload,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout_s,
        "cwd": _resolve_cwd(hook_input.cwd),
        "env": _build_hook_env(hook_input),
    }
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags and os.name == "nt":
        kwargs["creationflags"] = creationflags

    started = time.monotonic()
    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - started) * 1000)
        log.warning(
            "hook 执行超时（%dms，事件 %s）: %s",
            registration.timeout_ms,
            registration.event.value,
            command_preview(registration.command),
        )
        return HookExecutionOutcome(
            status="timeout",
            message=f"执行超时（{registration.timeout_ms}ms）",
            duration_ms=duration_ms,
        )
    except OSError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        log.warning(
            "hook 启动失败（事件 %s）: %s · %s",
            registration.event.value,
            command_preview(registration.command),
            exc,
        )
        return HookExecutionOutcome(
            status="error", message=f"启动失败: {exc}", duration_ms=duration_ms
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    exit_code = completed.returncode
    if exit_code == 2:
        return HookExecutionOutcome(
            status="ok",
            output=HookOutput(decision="block", reason=_stderr_tail(completed.stderr)),
            exit_code=2,
            duration_ms=duration_ms,
        )
    if exit_code != 0:
        log.warning(
            "hook 退出码非 0（%s，事件 %s）: %s",
            exit_code,
            registration.event.value,
            command_preview(registration.command),
        )
        return HookExecutionOutcome(
            status="failed",
            exit_code=exit_code,
            message=f"退出码 {exit_code}",
            duration_ms=duration_ms,
        )
    return HookExecutionOutcome(
        status="ok",
        output=_parse_stdout(completed.stdout),
        exit_code=0,
        duration_ms=duration_ms,
    )
