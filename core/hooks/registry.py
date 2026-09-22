"""Hooks 配置加载与 matcher 匹配（面向 ZCode 能力借鉴 · 规格 01 · C2）。

两级配置（规格 §3.1）：

- ``~/.ycode/hooks.json``：用户级，默认信任（用户自己写的）；
- ``<project>/.ycode/hooks.json``：项目级，默认不信任（需用户确认）。

所有路径可注入（``user_hooks_path`` / ``project_hooks_path`` / ``workspace_root``），
测试绝不触碰真实用户目录；生产默认走 ``YCODE_DATA_DIR``（与 config.YCODE_DIR
口径一致），便于测试/多实例隔离。

容错原则（规格 §7）：文件不存在静默跳过；文件级问题（JSON 非法/顶层结构非法）
warning 后跳过该文件、不影响另一个；条目级问题（缺 command/字段非法）warning 后
跳过该条、不影响同文件其他条目。绝不因坏配置让主流程感知异常。
"""

from __future__ import annotations

import json
import logging
import os
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
    """用户数据目录：与 config.YCODE_DIR 口径一致（YCODE_DATA_DIR 优先）。

    直接读环境变量而不 import config，避免仅为取一个目录而触发 config 模块级
    初始化（复制默认配置等副作用）；两处解析规则保持一致。
    """
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
    """工具名匹配（规格 §3.2）：``"*"`` 通配 / ``"a|b"`` 多值 / 精确名。

    大小写敏感（工具名是固定的蛇形小写标识）；``|`` 分段两侧空白容忍，
    方便用户写 ``"write_file | edit_file"`` 这类可读形式。
    """
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
    """两级 hook 配置的加载器。

    为什么每次 ``load()`` 都重新读文件、重算 digest（不缓存）：
    配置变更必须立即反映为新 digest（信任失效的触发条件，规格 §4.3），
    缓存会让「改了 command 仍在用旧指纹」的窗口出现。文件很小，成本可忽略。
    """

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
        """加载全部有效注册项。

        顺序约定：用户级在前、项目级在后。理由：用户自己的配置更可信，
        其注入上下文应更早出现；阻断/权限合并不受顺序影响（向最严格收敛）。

        ``include_disabled`` 默认 False = 执行侧口径（disabled 条目被跳过，
        行为与改造前完全一致）。管理列表（``trust.list_hooks_with_status``）
        传 True 让被禁用的钩子仍可见、可重新启用——否则「停用」在管理页上
        等同于条目消失，成为不可逆操作。
        """
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

    # ── 内部实现 ────────────────────────────────────────────────

    def _load_file(
        self, path: Path, *, scope: str, include_disabled: bool = False
    ) -> list[HookRegistration]:
        if not path.exists():
            return []  # 文件不存在是正常情况（规格 §7），静默跳过
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
        """解析单条 hook；任何字段非法都 warning 并返回 None（跳过该条）。"""
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
            # 纯展示字段：非法值忽略即可，不值得为它丢弃一条可执行 hook；
            # 它也不参与信任指纹（规格 §4.3），这里怎么处理都不影响安全。
            status_message = ""

        disabled = entry.get("disabled", False)
        if not isinstance(disabled, bool):
            # 非法 disabled 值两种模式都跳过：无法判断用户启停意图时既不执行、
            # 也不让管理页把它当成可启停的条目（用户需先修好文件里的坏值）。
            self._logger.warning("hook 配置项 disabled 非法，已跳过（%s）", where)
            return None
        if disabled and not include_disabled:
            # 执行侧（默认口径）：disabled 条目直接跳过，与改造前行为一致；
            # 管理列表（include_disabled=True）：保留并带 disabled 标记。
            return None

        # digest 计算逻辑不变：disabled 不参与指纹（启停不改变执行内容，
        # digest 只绑定 event/command/matcher/timeout_ms/source_path）。
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
