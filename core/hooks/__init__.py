"""Hooks 体系公开 API（面向 ZCode 能力借鉴 · 规格 01 · 阶段 C1-C5）。

模块划分（规格 §5.1）：

- ``types``：事件 / 注册项 / stdin-stdout 契约 / 聚合与合并规则；
- ``registry``：两级配置加载、容错解析、matcher 匹配；
- ``trust``：信任指纹、状态机、存储（fail-closed）；
- ``executor``：单 hook 执行（spawn shell + JSON 编解码）；
- ``runner``：HookRunner 编排（信任重校验、串行、取消、合并）。

调用方（C6-C10 接入层）从这里导入即可，不要深入子模块。
"""

from core.hooks.executor import HookExecutionOutcome, command_preview, run_hook
from core.hooks.registry import (
    HookRegistry,
    default_project_hooks_path,
    default_user_hooks_path,
    matches_tool,
    registration_matches,
)
from core.hooks.runner import HookRunner
from core.hooks.trust import (
    TRUST_BLOCKED,
    TRUST_PENDING,
    TRUST_REVOKED,
    TRUST_STALE,
    TRUST_TRUSTED,
    HookTrustStore,
    compute_hook_digest,
    default_hook_trust_path,
    normalize_workspace_identity,
)
from core.hooks.types import (
    DEFAULT_TIMEOUT_MS,
    MAX_TIMEOUT_MS,
    REDACTED_PLACEHOLDER,
    TOOL_EVENTS,
    HookEvent,
    HookInput,
    HookOutput,
    HookRegistration,
    HookRunResult,
)

__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "MAX_TIMEOUT_MS",
    "REDACTED_PLACEHOLDER",
    "TOOL_EVENTS",
    "TRUST_BLOCKED",
    "TRUST_PENDING",
    "TRUST_REVOKED",
    "TRUST_STALE",
    "TRUST_TRUSTED",
    "HookEvent",
    "HookExecutionOutcome",
    "HookInput",
    "HookOutput",
    "HookRegistration",
    "HookRegistry",
    "HookRunResult",
    "HookRunner",
    "HookTrustStore",
    "command_preview",
    "compute_hook_digest",
    "default_hook_trust_path",
    "default_project_hooks_path",
    "default_user_hooks_path",
    "matches_tool",
    "normalize_workspace_identity",
    "registration_matches",
    "run_hook",
]
