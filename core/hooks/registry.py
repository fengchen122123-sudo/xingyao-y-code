from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from core.hooks.trust import compute_hook_digest
from core.hooks.types import (
    DEFAULT_TIMEOUT_MS,
    MAX_TIMEOUT_MS,
    TOOL_EVENTS,
    HookEvent,
    HookRegistration,
)

_logger = logging.getLogger("ycode.hooks")


def _ycode_dir() -> Path:
    env_dir = os.environ.get("YCODE_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".ycode"


def default_user_hooks_path() -> Path:
    """用户级配置文件路径：~/.ycode/hooks.json。"""
    return _ycode_dir() / "hooks.json"


def default_project_hooks_path(workspace_root) -> Path:
    """项目级配置文件路径：<workspace_root>/.ycode/hooks.json。"""
    return Path(workspace_root) / ".ycode" / "hooks.json"


def matches_tool(matcher: str, tool_name: str) -> bool:
    if matcher == "*":
        return True
    if not tool_name:
        return False
    return any(part.strip() == tool_name for part in matcher.split("|"))


def registration_matches(
    registration: HookRegistration, event: HookEvent, tool_name: str
) -> bool:
    """注册项是否参与本次事件：事件相同 + matcher 命中。

    matcher 仅对 PreToolUse / PostToolUse 生效，其余事件默认全匹配（规格 §3.2），
    否则用户写 ``"matcher": "*"`` 之外的值会让 SessionStart/Stop hook 静默失联。
    """
    if registration.event != event:
        return False
    if event in TOOL_EVENTS:
        return matches_tool(registration.matcher, tool_name)
    return True


class HookRegistry:

    def __init__(
        self,
        *,
        user_hooks_path=None,
        project_hooks_path=None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._user_hooks_path = (
            Path(user_hooks_path) if user_hooks_path else default_user_hooks_path()
        )
        self._project_hooks_path = (
            Path(project_hooks_path) if project_hooks_path else None
        )
        self._logger = logger or _logger
        self._cache_lock = threading.Lock()
        self._file_cache: dict[
            tuple[str, str, bool],
            tuple[tuple[int, int, int] | None, tuple[HookRegistration, ...]],
        ] = {}

    @property
    def user_hooks_path(self) -> Path:
        return self._user_hooks_path

    def project_hooks_path(self, workspace_root=None) -> Path | None:
        """解析项目级配置路径：显式注入优先，其次由 workspace_root 推导。"""
        if self._project_hooks_path is not None:
            return self._project_hooks_path
        if workspace_root is None:
            return None
        return default_project_hooks_path(workspace_root)

    def load(
        self, *, workspace_root=None, include_disabled: bool = False
    ) -> list[HookRegistration]:
        registrations = self._load_file(
            self._user_hooks_path, scope="user", include_disabled=include_disabled
        )
        project_path = self.project_hooks_path(workspace_root)
        if project_path is not None:
            registrations.extend(
                self._load_file(
                    project_path, scope="project", include_disabled=include_disabled
                )
            )
        return registrations

    @staticmethod
    def _stat_signature(path: Path) -> tuple[int, int, int] | None:
        try:
            st = path.stat()
            return (
                int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
                int(st.st_size),
                int(getattr(st, "st_ctime_ns", 0)),
            )
        except OSError:
            return None

    def _load_file(
        self, path: Path, *, scope: str, include_disabled: bool = False
    ) -> list[HookRegistration]:
        cache_key = (str(path), scope, bool(include_disabled))
        sig = self._stat_signature(path)
        with self._cache_lock:
            cached = self._file_cache.get(cache_key)
            if cached is not None and cached[0] == sig:
                return list(cached[1])

        if sig is None:
            with self._cache_lock:
                self._file_cache[cache_key] = (None, ())
            return []

        regs = self._load_file_uncached(
            path, scope=scope, include_disabled=include_disabled
        )
        post_sig = self._stat_signature(path)
        if post_sig == sig:
            with self._cache_lock:
                self._file_cache[cache_key] = (sig, tuple(regs))
        return regs

    def _load_file_uncached(
        self, path: Path, *, scope: str, include_disabled: bool = False
    ) -> list[HookRegistration]:
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._logger.warning("hook 配置读取失败，已跳过（%s）: %s", path, exc)
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            self._logger.warning("hook 配置 JSON 非法，已跳过该文件（%s）", path)
            return []
        if not isinstance(data, dict):
            self._logger.warning("hook 配置顶层不是 JSON 对象，已跳过该文件（%s）", path)
            return []
        hooks = data.get("hooks")
        if hooks is None:
            return []
        if not isinstance(hooks, dict):
            self._logger.warning("hook 配置 hooks 字段不是对象，已跳过该文件（%s）", path)
            return []
        source_path = str(path)
        registrations: list[HookRegistration] = []
        for event_name, entries in hooks.items():
            try:
                event = HookEvent(event_name)
            except ValueError:
                self._logger.warning(
                    "hook 配置含未知事件 %r，已跳过该事件（%s）", event_name, path
                )
                continue
            if not isinstance(entries, list):
                self._logger.warning(
                    "hook 配置 %s 的值不是数组，已跳过（%s）", event.value, path
                )
                continue
            for index, entry in enumerate(entries):
                registration = self._parse_entry(
                    entry,
                    event=event,
                    scope=scope,
                    source_path=source_path,
                    index=index,
                    path=path,
                    include_disabled=include_disabled,
                )
                if registration is not None:
                    registrations.append(registration)
        return registrations

    def _parse_entry(
        self,
        entry,
        *,
        event: HookEvent,
        scope: str,
        source_path: str,
        index: int,
        path: Path,
        include_disabled: bool = False,
    ) -> HookRegistration | None:
        where = f"{path} · {event.value}[{index}]"
        if not isinstance(entry, dict):
            self._logger.warning("hook 配置项不是对象，已跳过（%s）", where)
            return None

        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            self._logger.warning("hook 配置项缺少 command，已跳过（%s）", where)
            return None

        matcher = entry.get("matcher", "*")
        if not isinstance(matcher, str):
            self._logger.warning("hook 配置项 matcher 不是字符串，已跳过（%s）", where)
            return None
        matcher = matcher.strip()
        if not matcher:
            # 空 matcher 既不是通配也不是工具名，语义不明；严格跳过而不是
            # 默默当全匹配，避免用户写出「以为没生效其实全命中」的配置。
            self._logger.warning("hook 配置项 matcher 为空，已跳过（%s）", where)
            return None

        raw_timeout = entry.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        # bool 是 int 的子类，必须显式排除（True 会被当成 1ms 的超时）
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            self._logger.warning("hook 配置项 timeout_ms 非法，已跳过（%s）", where)
            return None
        timeout_ms = int(raw_timeout)
        if timeout_ms <= 0:
            self._logger.warning("hook 配置项 timeout_ms 非正数，已跳过（%s）", where)
            return None
        if timeout_ms > MAX_TIMEOUT_MS:
            # 超上限按上限截断而不是跳过：用户意图明确（想要更长），
            # 截断是安全降级，跳过会让 hook 静默消失。
            self._logger.warning(
                "hook 配置项 timeout_ms 超上限 %dms，已截断（%s）", MAX_TIMEOUT_MS, where
            )
            timeout_ms = MAX_TIMEOUT_MS

        status_message = entry.get("status_message", "")
        if not isinstance(status_message, str):
            status_message = ""

        disabled = entry.get("disabled", False)
        if not isinstance(disabled, bool):
            self._logger.warning("hook 配置项 disabled 非法，已跳过（%s）", where)
            return None
        if disabled and not include_disabled:
            return None

        digest = compute_hook_digest(
            {
                "event": event.value,
                "command": command,
                "matcher": matcher,
                "timeout_ms": timeout_ms,
            },
            source_path,
        )
        return HookRegistration(
            event=event,
            matcher=matcher,
            command=command,
            timeout_ms=timeout_ms,
            status_message=status_message,
            source_path=source_path,
            scope=scope,
            digest=digest,
            disabled=disabled,
        )
