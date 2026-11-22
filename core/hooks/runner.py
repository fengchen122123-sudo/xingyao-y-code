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
                break
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

    def _trust_status(self, registration, workspace_identity: str) -> str:
        try:
            return self._trust_store.status_for(
                workspace_identity=workspace_identity,
                digest=registration.digest,
                source_path=registration.source_path,
                scope=registration.scope,
            )
        except Exception as exc:
            self._logger.warning("hook 信任判定失败（不执行）: %s", exc)
            return TRUST_BLOCKED

    def _prepare_input(self, registration, hook_input: HookInput) -> HookInput:
        if registration.scope != "project" or hook_input.tool_input is None:
            return hook_input
        if self._project_full_input_resolver():
            return hook_input
        redacted = {
            str(key): REDACTED_PLACEHOLDER for key in hook_input.tool_input
        }
        return replace(hook_input, tool_input=redacted)
