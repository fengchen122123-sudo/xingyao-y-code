"""HookRunner：匹配 → 信任校验 → 串行执行 → 合并（面向 ZCode 能力借鉴 · 规格 01 · C5）。

关键设计（规格 §5.4）：

- **每个 hook 执行前重新校验信任状态，不缓存 admission**——前序 hook 运行期间
  发生的 revoke / 配置收紧必须对尚未开始的 hook 立即生效（ZCode runner 的明确
  经验）；因此信任存储每次状态判定都重读磁盘；
- 每次 run 都重新加载配置（registry.load），使 command/matcher 变更后的新 digest
  立即参与信任判定（配置变更 → stale → 不执行）；
- 串行执行（v1 不做并行），超时/失败只记 warning/failure，**绝不阻断**；
- ``cancel_event`` 被 set → 停止后续 hook，已执行结果保留；
- 项目级 hook 默认拿不到完整 tool_input（C10 机制，规格 §3.3）：scope=project
  且用户配置未显式开启 ``project_hook_full_input`` 时，tool_input 只保留参数
  **键名**，值统一替换为 ``"[redacted]"`` 占位符（形态由测试锁定）。

为什么 ``project_hook_full_input`` 从用户级 config（~/.ycode/config.json）读取：
恶意项目可以改自己仓库里的任何文件，若开关放项目配置，恶意仓库可自行开启后
窃取用户数据；放用户配置才是真正的用户授权（规格 §3.3 的防窃取本意）。
"""

from __future__ import annotations

import logging
from dataclasses import replace

from core.hooks.executor import command_preview, run_hook
from core.hooks.registry import HookRegistry, registration_matches
from core.hooks.trust import (
    TRUST_BLOCKED,
    TRUST_TRUSTED,
    HookTrustStore,
    normalize_workspace_identity,
)
from core.hooks.types import (
    REDACTED_PLACEHOLDER,
    HookEvent,
    HookInput,
    HookRunResult,
)

_logger = logging.getLogger("ycode.hooks")


def _default_project_full_input() -> bool:
    """读取用户级 config 的 ``project_hook_full_input`` 开关（默认 False）。

    只有 JSON 布尔 ``true`` 才开启（``is True``）：字符串 "true"、数字 1 等
    模糊值一律按关闭处理——这是数据保护开关，宽松解析等于给恶意项目留口子。
    """
    try:
        import config

        data = config._read_json_with_backup(config._CONFIG_FILE, default=None)
        return isinstance(data, dict) and data.get("project_hook_full_input") is True
    except Exception:
        return False


class HookRunner:
    """hook 执行编排器。构造参数全部可注入，测试用 tmp 路径隔离。"""

    def __init__(
        self,
        registry: HookRegistry | None = None,
        trust_store: HookTrustStore | None = None,
        *,
        project_full_input_resolver=None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._registry = registry if registry is not None else HookRegistry()
        self._trust_store = (
            trust_store if trust_store is not None else HookTrustStore()
        )
        self._project_full_input_resolver = (
            project_full_input_resolver or _default_project_full_input
        )
        self._logger = logger or _logger

    @property
    def registry(self) -> HookRegistry:
        return self._registry

    @property
    def trust_store(self) -> HookTrustStore:
        return self._trust_store

    def run(
        self, event, hook_input: HookInput, *, cancel_event=None
    ) -> HookRunResult:
        """执行匹配 event 的全部 hook 并聚合结果（规格 §5.4 流程）。

        注意：本方法不实现 Stop 续跑上限（STOP_CONTINUE_MAX）——那是
        agent_service 接入层（C9）的职责，runner 只报告 ``continue_after_stop``。
        """
        try:
            hook_event = event if isinstance(event, HookEvent) else HookEvent(event)
        except ValueError:
            raise ValueError(f"未知 hook 事件: {event!r}") from None

        result = HookRunResult()
        workspace_identity = normalize_workspace_identity(hook_input.cwd)
        registrations = self._registry.load(workspace_root=hook_input.cwd)
        matched = [
            registration
            for registration in registrations
            if registration_matches(registration, hook_event, hook_input.tool_name)
        ]
        for registration in matched:
            if cancel_event is not None and cancel_event.is_set():
                break  # 取消：停止后续 hook，已执行结果保留
            status = self._trust_status(registration, workspace_identity)
            if status != TRUST_TRUSTED:
                result.failures.append(
                    f"{registration.event.value} hook 未获信任（{status}），已跳过: "
                    f"{command_preview(registration.command)}"
                )
                continue
            effective_input = self._prepare_input(registration, hook_input)
            outcome = run_hook(
                registration,
                effective_input,
                cancel_event=cancel_event,
                logger=self._logger,
            )
            if outcome.status == "skipped":
                break
            result.executed_count += 1
            if outcome.output is not None:
                result.merge_output(outcome.output, hook_event)
            if outcome.status != "ok":
                result.failures.append(
                    f"{command_preview(registration.command)}: {outcome.message}"
                )
        return result

    # ── 内部实现 ────────────────────────────────────────────────

    def _trust_status(self, registration, workspace_identity: str) -> str:
        """执行前重新查询信任状态；任何判定异常都按 blocked 处理（fail-closed）。"""
        try:
            return self._trust_store.status_for(
                workspace_identity=workspace_identity,
                digest=registration.digest,
                source_path=registration.source_path,
                scope=registration.scope,
            )
        except Exception as exc:  # 判定失败 = 无法证明可信 → 不执行
            self._logger.warning("hook 信任判定失败（不执行）: %s", exc)
            return TRUST_BLOCKED

    def _prepare_input(self, registration, hook_input: HookInput) -> HookInput:
        """按 scope 裁剪发给该 hook 的 tool_input（规格 §3.3 / C10）。"""
        if registration.scope != "project" or hook_input.tool_input is None:
            return hook_input
        if self._project_full_input_resolver():
            return hook_input
        redacted = {
            str(key): REDACTED_PLACEHOLDER for key in hook_input.tool_input
        }
        return replace(hook_input, tool_input=redacted)
