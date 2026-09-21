"""单 hook 执行器：spawn 系统 shell + stdin/stdout 编解码（面向 ZCode 能力借鉴 · 规格 01 · C4）。

安全与容错红线（规格 §7）：

- hook 的任何失败（超时 / 非 0 退出码 / spawn 异常 / stdout 非 JSON）都只返回
  「无干预」，绝不抛异常到调用方、绝不阻断主流程——hook 是增强能力，不是关键路径；
- 日志绝不写入 stdin 正文（tool_input 可能含用户敏感内容），failure 消息只含
  命令预览、退出码、耗时；
- 通过系统 shell 执行（``shell=True``），与「hook 是 shell 命令」的契约一致
  （Windows 下即 cmd.exe，POSIX 下即 /bin/sh）；
- Windows 无控制台场景传 CREATE_NO_WINDOW，避免黑窗闪现（项目既有惯例，
  与 core/git/runner.py、core/tools.py 等调用点一致）。
"""

from __future__ import annotations

import json
import locale
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from core.hooks.types import HookInput, HookOutput, HookRegistration

_logger = logging.getLogger("ycode.hooks")

#: 命令预览的最大长度：warning 里只放截断后的单行命令，避免日志被超长命令撑爆。
_COMMAND_PREVIEW_LIMIT = 120


def command_preview(command: str, limit: int = _COMMAND_PREVIEW_LIMIT) -> str:
    """单行截断的命令预览（供 warning / failures 使用，不含 stdin 正文）。"""
    single_line = " ".join(str(command).split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1] + "…"


@dataclass(frozen=True)
class HookExecutionOutcome:
    """单 hook 一次执行的结果。

    status 取值：
    - ``ok``：进程正常结束（无论 stdout 是否给出 JSON；无 JSON = 无干预）；
    - ``timeout``：超时（已按规格记为 warning，不阻断）；
    - ``failed``：退出码非 0；
    - ``error``：spawn / IO 异常；
    - ``skipped``：执行前发现已取消，未启动进程。
    """

    status: str
    output: HookOutput | None = None
    exit_code: int | None = None
    message: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _resolve_cwd(cwd) -> str | None:
    """hook 子进程的工作目录。

    优先用 ``HookInput.cwd``（hook 常需在项目目录内操作文件，如 formatter），
    路径无效/不存在时回退继承本进程 cwd（宁可在错误目录执行，也不要 spawn 失败）。
    """
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
    """解析 stdout：非 JSON / 非对象一律视为「无干预」，不报错（规格 §7）。"""
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


def run_hook(
    registration: HookRegistration,
    hook_input: HookInput,
    *,
    cancel_event=None,
    logger: logging.Logger | None = None,
) -> HookExecutionOutcome:
    """执行单条 hook（串行、前台、同步）。

    ``cancel_event`` 只做入口检查：取消语义是「停止后续 hook」，当前 hook 已启动
    就让它在超时约束内跑完（规格 §7），避免杀进程带来的半写状态。
    """
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
    kwargs = {
        "shell": True,
        "input": payload,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout_s,
        "cwd": _resolve_cwd(hook_input.cwd),
    }
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags and os.name == "nt":
        kwargs["creationflags"] = creationflags

    started = time.monotonic()
    try:
        completed = subprocess.run(registration.command, **kwargs)
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
