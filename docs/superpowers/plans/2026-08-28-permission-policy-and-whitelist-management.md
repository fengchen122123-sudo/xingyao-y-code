# Permission Policy and Whitelist Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一 Y-CODE 的四种权限模式，并让 CLI、GUI 同时支持任务级授权和项目白名单的手动新增、单条删除及清空。

**Architecture:** 新增纯函数权限策略层，将命令风险、模式矩阵和原因码集中管理；副作用协调继续通过 Harness 工具管线、Permission Broker、任务授权存储和 `permissions.json` 完成。CLI 与 GUI 只负责展示后端下发的授权选项并调用同一套规则增删接口。

**Tech Stack:** Python 3、pytest、prompt_toolkit、pywebview、Vue 3、Pinia、TypeScript、Vitest、Vite。

**Spec:** `docs/superpowers/specs/2026-08-28-permission-policy-and-whitelist-management-design.md`

## Implementation Status (2026-08-28)

This section is the live handoff checkpoint. Continue from the remaining items instead of reimplementing completed work.

Completed:

- Unified `PermissionFacts -> PermissionDecision` policy and command risk classifier.
- Catastrophic command hard denial before approval, task grants, project rules, or mode checks.
- Stable task-scoped command/file/sandbox grants with task rename and deletion handling.
- Project whitelist add/remove/list/clear APIs with command token-prefix and exact-file normalization.
- Keyed permission choices in broker, CLI, GUI bridge, Pinia store, and `PermCard.vue`.
- GUI Settings manual whitelist add, per-rule delete, and confirmed clear.
- CLI `/whitelist list|add|remove|clear` support and updated permission help.
- Plan-mode read-only command support and dynamic prompt constraints without changing the stable prompt prefix.
- Final focused backend regression: 241 tests passing across permission policy, broker, tool pipeline, task isolation, CLI, GUI Bridge, command policy, agent-turn E2E, context builder, and prompt byte-stability suites.
- Frontend permission components: 17 tests passing; production TypeScript/Vite build passing.
- Remaining frontend tests excluding the unrelated maximized-window handle test: 369 tests passing.
- Harness journal/message/model/runtime group: 41 tests passing when run as an isolated group.
- Independent security review findings were addressed: unknown Git operations and arbitrary `--help` commands no longer become queries; explicit shell overrides are sensitive; recursive deletion of the workspace/user directory is catastrophic; CLI clear requires explicit confirmation; whitelist failure statuses are no longer shown as success.

Known repository-level verification issues outside the permission diff:

- Full frontend suite has one stable existing failure: `src/App.guiTheme.test.ts` expects maximized native windows to remove eight resize handles. No permission change touches `App.vue` or that test; the other 369 frontend tests pass.
- Full backend discovery from repository root enters unreadable historical `.pytest_tmp` and `.superpowers/.../pytest-dev` directories. Running the formal `tests/` directory avoids those collection errors.
- The formal backend suite has an existing `tests/test_gui_token_usage.py::test_gui_session_rename_and_clear_update_cache_lifecycle` fixture that constructs `ApiBridge` without `_turn_start_lock`; the failing implementation line is outside this feature diff.
- In full-suite order, `test_harness_runtime.py::test_persistent_turn_records_two_model_steps_for_one_tool_round` can block after preceding tests. The Harness group passes 41/41 in isolation, so this remains a repository test-isolation issue rather than a permission regression.
- Do not commit or push until the user grants each action separately.

Local Python note: the repository `venv\\Scripts\\python.exe` currently points at an unavailable historical user path. Tests were run with the bundled Python plus the repository venv site-packages and pywin32 paths; preserve this invocation if the venv launcher remains broken.

## Global Constraints

- 日常开发只在 `dev` 分支进行；不得主动合并到 `master` 或其他分支。
- 灾难性命令在所有模式、任务授权、项目白名单和沙盒状态下都直接拒绝。
- 计划模式只允许读取、查询、计划元数据和用户澄清，不允许修改或删除工作区文件。
- 文件白名单只支持规范化后的精确文件路径，不增加目录递归授权。
- `permissions.json` 保持版本 1 和现有 `commands`、`write_files`、`edit_files` 字段，不做破坏性迁移。
- 不新增第三方 Python 或前端依赖。
- 动态权限说明只能进入动态运行上下文，不能改变稳定 system prompt 的跨轮字节。
- 不得覆盖或回滚工作树中已有的无关修改；实施开始前重新运行 `git status --short`。
- 删除、Git 提交和 Git 推送属于产品内的敏感操作；实施过程中的每次 `git commit`、`git push` 也必须先获得用户明确授权。
- 所有文件修改遵循 TDD：先写失败测试，确认失败，再写最小实现。

---

## File Map

### 新文件

- `core/permission_policy.py`：风险枚举、决策对象、四模式纯函数矩阵。
- `core/permission_grants.py`：按任务 ID 隔离的命令、文件和精确命令临时授权。
- `tests/test_permission_policy.py`：策略矩阵与命令分类单元测试。
- `frontend/src/components/PermCard.test.ts`：审批卡动态选项测试。

### 核心修改

- `core/command_policy.py:145-230`：命令风险分类和手动命令前缀解析。
- `core/permission_store.py:1-170`：统一规则新增、删除、规范化和显式写盘错误。
- `core/permission_broker.py:12-110`：稳定授权选项 key，普通请求最多四项。
- `core/llm.py:328-930`：移出任务授权集合，按选择写入任务或项目作用域，取消静默持久化失败。
- `core/harness/tool_pipeline.py:20-46,634-668`：统一调用策略，允许计划模式只读命令，返回稳定拒绝原因。
- `core/harness/tool_adapters.py:178-283,378-480`：把路径边界分类与询问拆开，避免模式检查和沙盒检查互相绕过。
- `core/tools.py:625-635,1519,3492`：保留灾难性命令执行前二次拒绝，并给内部敏感操作传递明确风险。
- `core/context_builder.py:52-83`：计划模式动态输出约束。

### CLI 修改

- `main.py:265-272,608-710`：权限提示选项与文案。
- `main.py:1492-1520,1647-1765`：帮助、模式说明和 `/whitelist` 增删命令。
- `main.py:1780-1810`：删除会话时清理任务授权。

### GUI 与前端修改

- `gui_bridge.py:2514-2560`：删除会话或项目分组时清理任务授权。
- `gui_bridge.py:2784-2920`：规则新增/删除接口与稳定授权 key 响应。
- `frontend/src/types/pywebview.d.ts:14,235-368`：权限选项和规则 API 类型。
- `frontend/src/stores/appStore.ts:257-269,2386-2400,2655-2700`：存储后端选项并按 key 应答。
- `frontend/src/components/PermCard.vue:1-55`：渲染普通权限选项，不硬编码三按钮。
- `frontend/src/components/SettingsPage.vue:263-313,768-810,980-1040`：手动新增、单条删除和清空确认。
- `frontend/src/components/PermDropdown.vue:35-44`：四种模式说明。

### 测试与文档修改

- `tests/test_permission_upgrade.py`：任务/项目授权、GUI Bridge、沙盒组合。
- `tests/test_permission_broker.py`：四选项和稳定 key。
- `tests/test_tool_pipeline.py`：权限矩阵接入、计划查询和沙盒执行。
- `tests/test_session_parallel_isolation.py`：切换页面不清授权、并行隔离、删除清理。
- `tests/test_context_builder.py`：计划模式动态约束。
- `tests/test_main_ui_events.py`：CLI `/whitelist` 增删与审批显示。
- `frontend/src/components/SettingsPage.test.ts`：GUI 规则表单和删除确认。
- `README.md`：权限矩阵、审批层级和 CLI 命令。

---

### Task 1: Pure permission matrix

**Files:**
- Create: `core/permission_policy.py`
- Create: `tests/test_permission_policy.py`

**Interfaces:**
- Produces: `PermissionRisk`, `PermissionAction`, `PermissionFacts`, `PermissionDecision`, `decide_permission(facts)`。
- Later tasks pass already-computed grant and sandbox facts into this pure function; this module performs no IO and imports no UI code。

- [ ] **Step 1: Write the failing matrix tests**

```python
import pytest

from core.permission_policy import (
    PermissionAction,
    PermissionFacts,
    PermissionRisk,
    decide_permission,
)


@pytest.mark.parametrize(
    ("mode", "risk", "expected"),
    [
        ("confirm", PermissionRisk.QUERY, PermissionAction.ALLOW),
        ("confirm", PermissionRisk.NORMAL_MUTATION, PermissionAction.ASK),
        ("confirm", PermissionRisk.SENSITIVE_MUTATION, PermissionAction.ASK),
        ("accept_edits", PermissionRisk.QUERY, PermissionAction.ALLOW),
        ("accept_edits", PermissionRisk.NORMAL_MUTATION, PermissionAction.ALLOW),
        ("accept_edits", PermissionRisk.SENSITIVE_MUTATION, PermissionAction.ASK),
        ("plan", PermissionRisk.QUERY, PermissionAction.ALLOW),
        ("plan", PermissionRisk.NORMAL_MUTATION, PermissionAction.DENY),
        ("plan", PermissionRisk.SENSITIVE_MUTATION, PermissionAction.DENY),
        ("full", PermissionRisk.QUERY, PermissionAction.ALLOW),
        ("full", PermissionRisk.NORMAL_MUTATION, PermissionAction.ALLOW),
        ("full", PermissionRisk.SENSITIVE_MUTATION, PermissionAction.ALLOW),
    ],
)
def test_mode_matrix(mode, risk, expected):
    decision = decide_permission(PermissionFacts(mode=mode, risk=risk))
    assert decision.action is expected


@pytest.mark.parametrize("mode", ["confirm", "accept_edits", "plan", "full"])
def test_catastrophic_always_denied(mode):
    decision = decide_permission(
        PermissionFacts(
            mode=mode,
            risk=PermissionRisk.CATASTROPHIC,
            task_grant_hit=True,
            project_rule_hit=True,
            outside_workspace=True,
            sandbox_restricted=False,
        )
    )
    assert decision == PermissionDecision(
        PermissionAction.DENY,
        "catastrophic_denied",
    )


def test_plan_internal_tools_remain_allowed():
    decision = decide_permission(
        PermissionFacts(
            mode="plan",
            risk=PermissionRisk.NORMAL_MUTATION,
            plan_internal=True,
        )
    )
    assert decision.action is PermissionAction.ALLOW
    assert decision.reason == "plan_internal_allow"


def test_sandbox_boundary_precedes_whitelists():
    decision = decide_permission(
        PermissionFacts(
            mode="full",
            risk=PermissionRisk.NORMAL_MUTATION,
            outside_workspace=True,
            sandbox_restricted=True,
            project_rule_hit=True,
        )
    )
    assert decision.action is PermissionAction.ASK
    assert decision.reason == "sandbox_boundary_requires_confirmation"


def test_project_rule_precedes_mode_default():
    decision = decide_permission(
        PermissionFacts(
            mode="confirm",
            risk=PermissionRisk.SENSITIVE_MUTATION,
            project_rule_hit=True,
        )
    )
    assert decision.action is PermissionAction.ALLOW
    assert decision.reason == "project_rule_hit"
```

- [ ] **Step 2: Run the new test and confirm the import failure**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_permission_policy.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'core.permission_policy'`。

- [ ] **Step 3: Implement the immutable policy types and decision order**

```python
from dataclasses import dataclass
from enum import Enum


class PermissionRisk(str, Enum):
    QUERY = "query"
    NORMAL_MUTATION = "normal_mutation"
    SENSITIVE_MUTATION = "sensitive_mutation"
    CATASTROPHIC = "catastrophic"


class PermissionAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionFacts:
    mode: str
    risk: PermissionRisk
    plan_internal: bool = False
    outside_workspace: bool = False
    sandbox_restricted: bool = True
    task_sandbox_bypass: bool = False
    task_grant_hit: bool = False
    project_rule_hit: bool = False


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str


def decide_permission(facts: PermissionFacts) -> PermissionDecision:
    if facts.risk is PermissionRisk.CATASTROPHIC:
        return PermissionDecision(PermissionAction.DENY, "catastrophic_denied")
    if facts.mode == "plan" and not facts.plan_internal and facts.risk is not PermissionRisk.QUERY:
        return PermissionDecision(PermissionAction.DENY, "plan_mutation_denied")
    if facts.plan_internal:
        return PermissionDecision(PermissionAction.ALLOW, "plan_internal_allow")
    if facts.outside_workspace and facts.sandbox_restricted and not facts.task_sandbox_bypass:
        return PermissionDecision(PermissionAction.ASK, "sandbox_boundary_requires_confirmation")
    if facts.project_rule_hit:
        return PermissionDecision(PermissionAction.ALLOW, "project_rule_hit")
    if facts.task_grant_hit:
        return PermissionDecision(PermissionAction.ALLOW, "task_grant_hit")
    if facts.risk is PermissionRisk.QUERY:
        return PermissionDecision(PermissionAction.ALLOW, "query_allow")
    if facts.mode == "confirm":
        return PermissionDecision(PermissionAction.ASK, "confirm_requires_confirmation")
    if facts.mode == "accept_edits":
        if facts.risk is PermissionRisk.SENSITIVE_MUTATION:
            return PermissionDecision(PermissionAction.ASK, "sensitive_requires_confirmation")
        return PermissionDecision(PermissionAction.ALLOW, "normal_mutation_allow")
    if facts.mode == "full":
        return PermissionDecision(PermissionAction.ALLOW, "full_mode_allow")
    return PermissionDecision(PermissionAction.ASK, "unknown_mode_requires_confirmation")
```

- [ ] **Step 4: Run the policy tests**

Run: `venv\Scripts\python -m pytest tests/test_permission_policy.py -v`

Expected: all Task 1 tests pass。

- [ ] **Step 5: Request commit approval, then commit only Task 1 files**

```powershell
git add core/permission_policy.py tests/test_permission_policy.py
git commit -m "feat: add deterministic permission policy"
```

Do not run these Git commands until the user explicitly approves this commit。

---

### Task 2: Deterministic command risk classification

**Files:**
- Modify: `core/command_policy.py:145-230`
- Modify: `tests/test_permission_policy.py`

**Interfaces:**
- Consumes: `PermissionRisk` from Task 1。
- Produces: `classify_command_risk(command: str) -> PermissionRisk` and `parse_manual_command_rule(command: str) -> tuple[str, ...]`。

- [ ] **Step 1: Add failing command classification tests**

```python
from core.command_policy import classify_command_risk, parse_manual_command_rule


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff --stat",
        "git log -5 --oneline",
        "git show HEAD",
        "git rev-parse --show-toplevel",
        "git ls-files",
        "rg permission core",
        "Get-Content README.md",
        "python --version",
    ],
)
def test_read_only_commands_are_queries(command):
    assert classify_command_risk(command) is PermissionRisk.QUERY


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m change",
        "git push origin dev",
        "git reset --soft HEAD~1",
        "git merge feature",
        "git rebase dev",
        "Remove-Item file.txt",
        "npm install lodash",
        "pip uninstall requests",
        "Stop-Process -Id 1234",
    ],
)
def test_sensitive_commands_require_sensitive_policy(command):
    assert classify_command_risk(command) is PermissionRisk.SENSITIVE_MUTATION


def test_build_command_is_normal_mutation():
    assert classify_command_risk("npm run build") is PermissionRisk.NORMAL_MUTATION


def test_composed_or_inline_commands_are_never_queries():
    assert classify_command_risk("git status; Remove-Item x") is PermissionRisk.SENSITIVE_MUTATION
    assert classify_command_risk('python -c "print(1)"') is PermissionRisk.SENSITIVE_MUTATION


def test_manual_rule_preserves_complete_safe_token_prefix():
    assert parse_manual_command_rule("git push origin") == ("git", "push", "origin")


@pytest.mark.parametrize(
    "command",
    ["git status | Out-File x", 'python -c "print(1)"', "Remove-Item -Recurse -Force C:\\"],
)
def test_manual_rule_rejects_unpersistable_commands(command):
    with pytest.raises(ValueError):
        parse_manual_command_rule(command)
```

- [ ] **Step 2: Run only the new tests and confirm failure**

Run: `venv\Scripts\python -m pytest tests/test_permission_policy.py -k "command or manual_rule" -v`

Expected: import fails because both new functions are absent。

- [ ] **Step 3: Implement explicit command families**

Add these exact families beside the existing safe-token helpers:

```python
_QUERY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "rev-parse", "ls-files"}
_SENSITIVE_GIT_SUBCOMMANDS = {
    "commit", "push", "reset", "merge", "rebase", "checkout", "switch",
    "clean", "rm", "tag", "cherry-pick", "revert",
}
_DELETE_COMMANDS = {"rm", "del", "erase", "rmdir", "remove-item"}
_PROCESS_KILL_COMMANDS = {"kill", "pkill", "taskkill", "stop-process"}
_PACKAGE_MUTATION_PAIRS = {
    ("pip", "install"), ("pip", "uninstall"),
    ("npm", "install"), ("npm", "uninstall"),
    ("npm", "remove"), ("npm", "update"),
    ("pnpm", "add"), ("pnpm", "install"), ("pnpm", "remove"),
    ("yarn", "add"), ("yarn", "install"), ("yarn", "remove"),
    ("cargo", "install"), ("cargo", "uninstall"),
}
```

Implement `classify_command_risk` with this order: existing `is_dangerous_command`; safe token parse; composed/inline rejection from query; explicit Git/query families; explicit sensitive families; otherwise `NORMAL_MUTATION`。Normalize executable basenames by lowercasing and removing `.exe`。

Implement `parse_manual_command_rule` by calling `_safe_tokens`, rejecting inline flags and catastrophic commands, and returning the complete token tuple instead of the shorter automatic session family produced by `session_rule_for_command`。

- [ ] **Step 4: Run command-policy and existing safety tests**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_permission_policy.py tests/test_tools.py -k "command or dangerous" -v
```

Expected: new classifier tests and existing dangerous-command tests pass。

- [ ] **Step 5: Request commit approval, then commit Task 2**

```powershell
git add core/command_policy.py tests/test_permission_policy.py
git commit -m "feat: classify command permission risk"
```

---

### Task 3: Persistent whitelist add/remove API

**Files:**
- Modify: `core/permission_store.py:1-170`
- Modify: `tests/test_permission_upgrade.py:218-350`

**Interfaces:**
- Consumes: `parse_manual_command_rule` from Task 2。
- Produces: `RuleMutationResult`, `PermissionStoreError`, `add_rule`, `remove_rule`。
- Preserves: `project_rules`, `add_command_rule`, `add_write_file`, `add_edit_file`, `clear_project`。

- [ ] **Step 1: Add failing storage mutation tests**

```python
def test_manual_add_and_remove_command_rule(tmp_path, monkeypatch):
    import config
    from core import permission_store

    monkeypatch.setattr(config, "YCODE_DIR", tmp_path / "data")
    project = tmp_path / "project"
    project.mkdir()

    added = permission_store.add_rule("command", "git push origin", workdir=project)
    assert added.status == "added"
    assert added.normalized_value == "git push origin"
    assert ("git", "push", "origin") in permission_store.project_rules(project)["commands"]

    duplicate = permission_store.add_rule("command", "git push origin", workdir=project)
    assert duplicate.status == "exists"

    removed = permission_store.remove_rule("command", "git push origin", workdir=project)
    assert removed.status == "removed"
    assert permission_store.project_rules(project)["commands"] == set()

    missing = permission_store.remove_rule("command", "git push origin", workdir=project)
    assert missing.status == "not_found"


def test_file_rule_is_exact_and_canonical(tmp_path, monkeypatch):
    from core import permission_store
    from core.workspace_context import workspace_scope

    project = tmp_path / "project"
    project.mkdir()
    with workspace_scope(str(project)):
        result = permission_store.add_rule("write", "src/app.py", workdir=project)
        expected = permission_store.canonical_file_path("src/app.py")
    assert result.normalized_value == expected
    assert expected in permission_store.project_rules(project)["write_files"]


def test_store_write_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    from core import permission_store

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        permission_store,
        "_save",
        lambda _data: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(permission_store.PermissionStoreError, match="disk full"):
        permission_store.add_rule("edit", str(project / "a.py"), workdir=project)
```

- [ ] **Step 2: Run the storage tests and confirm missing APIs**

Run: `venv\Scripts\python -m pytest tests/test_permission_upgrade.py -k "manual_add or file_rule or store_write" -v`

Expected: failures report missing `add_rule`, `remove_rule`, or `PermissionStoreError`。

- [ ] **Step 3: Implement normalized mutation results and exact deletion**

Add:

```python
from dataclasses import dataclass


class PermissionStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuleMutationResult:
    status: str
    rule_type: str
    normalized_value: str
```

`add_rule(rule_type, value, workdir=None)` accepts only `command`, `write`, `edit`; command strings use `parse_manual_command_rule`, file strings use `canonical_file_path`。`remove_rule` performs the same normalization and removes exactly one stored value under `_STORE_LOCK`。Translate `_save` exceptions to `PermissionStoreError` and do not return a successful result after a failed atomic write。

Keep compatibility wrappers: existing tuple-based `add_command_rule` stores its supplied tuple without re-truncating it; `add_write_file` and `add_edit_file` return `True` for both `added` and `exists`。

- [ ] **Step 4: Run the completed permission-store slice**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_permission_upgrade.py -k "manual_add or file_rule or store_write or permission_store_isolated" -v
```

Expected: all selected storage tests pass。Do not commit Task 3 while any selected test fails; approval-choice tests are deliberately migrated in Task 5 and are not part of this slice。

- [ ] **Step 5: Request commit approval, then commit Task 3**

```powershell
git add core/permission_store.py tests/test_permission_upgrade.py
git commit -m "feat: manage persistent permission rules"
```

---

### Task 4: Task-scoped grants and stable broker option keys

**Files:**
- Create: `core/permission_grants.py`
- Modify: `core/permission_broker.py:12-110`
- Modify: `core/llm.py:328-550`
- Modify: `core/tools.py:153-275`
- Modify: `tests/test_permission_broker.py`
- Modify: `tests/test_session_parallel_isolation.py:230-420`

**Interfaces:**
- Produces task grant functions: `grant_command`, `grant_file`, `matches_command`, `matches_file`, `snapshot`, `clear_task`, `rename_task`。
- Produces broker types: `PermissionOption`, `PermissionOptionKey` and `PermissionRequest.option_keys`。
- Produces sandbox lifecycle functions: `clear_session_sandbox_bypass(task_id)` and `rename_session_sandbox_bypass(old_task_id, new_task_id)`。
- `PermissionBroker.resolve` continues returning an integer index through `future` so `ask_user` remains compatible。

- [ ] **Step 1: Write failing task-isolation and four-option tests**

```python
def test_task_grant_survives_visible_session_switch(monkeypatch):
    from core import app_context
    from core.permission_grants import grant_command, matches_command

    visible = {"name": "task-a"}
    monkeypatch.setattr(app_context, "get_current_session", lambda: visible["name"])
    grant_command("git status")
    visible["name"] = "task-b"
    assert matches_command("git status --short") is False
    visible["name"] = "task-a"
    assert matches_command("git status --short") is True


def test_bound_background_task_keeps_its_own_grant(monkeypatch):
    from core import app_context
    from core.permission_grants import grant_file, matches_file

    app_context.set_thread_session("background-a")
    try:
        grant_file("write", "C:/project/a.py")
        monkeypatch.setattr(app_context, "get_current_session", lambda: "visible-b")
        assert matches_file("write", "C:/project/a.py") is True
    finally:
        app_context.set_thread_session(None)


def test_broker_accepts_four_keyed_permission_options():
    from core.permission_broker import PermissionBroker, PermissionOption

    broker = PermissionBroker(activate_prompt=lambda _request: None)
    request = broker.enqueue(
        "write_file",
        {},
        "owner",
        "turn",
        "主代理",
        options=(
            PermissionOption("allow_once", "允许本次"),
            PermissionOption("deny", "拒绝"),
            PermissionOption("allow_task", "本任务同类操作放行"),
            PermissionOption("allow_project", "加入项目白名单"),
        ),
    )
    assert request.option_keys == (
        "allow_once", "deny", "allow_task", "allow_project",
    )
    assert broker.resolve_by_key(request.request_id, "allow_project") is True
    assert request.future.result() == 3
```

- [ ] **Step 2: Run the focused tests and confirm missing modules/types**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_permission_broker.py tests/test_session_parallel_isolation.py -k "four_keyed or task_grant or bound_background" -v
```

Expected: missing `permission_grants`, `PermissionOption`, or `resolve_by_key` failures。

- [ ] **Step 3: Implement task ID routing and grant storage**

Use `app_context.get_thread_session() or app_context.get_current_session() or "__default__"` as the task key。Store command families, exact commands, write files, and edit files in a lock-protected dictionary。`grant_command` uses `session_rule_for_command`; if no safe family is available, store the exact command only。`snapshot(task_id)` returns copied sets and never exposes mutable internal buckets。`rename_task` moves one bucket under the same lock and raises `ValueError` if the destination already has grants, preventing silent overwrite。

Update `core.tools._sandbox_session_id` to use the same thread-session/current-session resolution。Add explicit clear and rename functions for `_session_sandbox_bypass_by_session`; do not clear any bucket merely because the visible task changes。

Remove `_SESSION_GRANTS` ownership from `core/llm.py`; keep compatibility helper names there as thin wrappers during this task so existing callers do not break in the middle of the sequence。

- [ ] **Step 4: Implement keyed broker options without breaking ask_user**

```python
from enum import Enum


@dataclass(frozen=True)
class PermissionOption:
    key: str
    label: str


class PermissionOptionKey(str, Enum):
    ALLOW_ONCE = "allow_once"
    DENY = "deny"
    ALLOW_TASK = "allow_task"
    ALLOW_PROJECT = "allow_project"
    ALLOW_TASK_SANDBOX = "allow_task_sandbox"
```

`enqueue` accepts one to four `PermissionOption` objects or one to three legacy strings。Legacy strings receive generated keys `choice_0`, `choice_1`, `choice_2` so `ask_user` behavior and custom answers remain unchanged。`resolve_by_key` looks up the active request key and delegates to `resolve` with the matching index; unknown keys return `False` without resolving the request。

- [ ] **Step 5: Run broker, task isolation, and ask_user regressions**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_permission_broker.py tests/test_session_parallel_isolation.py tests/test_tools.py -k "permission or session or ask_user" -v
```

Expected: pass。

- [ ] **Step 6: Request commit approval, then commit Task 4**

```powershell
git add core/permission_grants.py core/permission_broker.py core/llm.py core/tools.py tests/test_permission_broker.py tests/test_session_parallel_isolation.py
git commit -m "feat: separate task and project permission grants"
```

---

### Task 5: Integrate policy with Harness, sandbox, and approval choices

**Files:**
- Modify: `core/harness/tool_pipeline.py:20-46,634-668`
- Modify: `core/harness/tool_adapters.py:178-283,378-480`
- Modify: `core/llm.py:665-930,3520-3555`
- Modify: `core/tools.py:625-635,1519,3492`
- Modify: `tests/test_tool_pipeline.py`
- Modify: `tests/test_permission_upgrade.py`

**Interfaces:**
- Consumes policy, command risk, grants, store, and broker option keys from Tasks 1-4。
- Produces `AuthorizationResult(allowed, reason, sandbox_bypass)` from the permission coordinator used by ToolPipeline。
- Keeps `check_permission_and_ask(...) -> bool` as a compatibility wrapper for internal `manage_task` and plan-verification callers。

- [ ] **Step 1: Write failing pipeline behavior tests**

```python
from core.permission_broker import PermissionBroker


def _auto_answer_pipeline_permission(monkeypatch, choice, asked, requests):
    broker = PermissionBroker()

    def activate(request):
        asked.append(request.function_name)
        requests.append(request)
        broker.resolve(request.request_id, choice)

    broker.set_prompt_handlers(activate, lambda: None)
    monkeypatch.setattr(llm, "permission_broker", broker)


def test_accept_edits_runs_query_and_normal_mutation_but_asks_sensitive(monkeypatch):
    from core.tool_protocol import ToolResult, ToolSpec
    from core.tool_registry import ToolRegistry

    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    executed = []
    registry = ToolRegistry([
        ToolSpec(
            name="execute_command",
            description="command",
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
            execute=lambda command: executed.append(command) or ToolResult("ok"),
        )
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)
    asked, requests = [], []
    _auto_answer_pipeline_permission(monkeypatch, 1, asked, requests)

    assert _call("execute_command", {"command": "git status"})["content"] == "ok"
    assert _call("execute_command", {"command": "npm run build"})["content"] == "ok"
    result = _call("execute_command", {"command": "git push origin dev"})
    assert result["content"] == llm.PERMISSION_DENIED_MESSAGE
    assert asked == ["execute_command"]
    assert executed == ["git status", "npm run build"]


def test_plan_mode_allows_read_only_command_and_denies_write(monkeypatch):
    from core.tool_protocol import ToolResult, ToolSpec
    from core.tool_registry import ToolRegistry

    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "plan")
    registry = ToolRegistry([
        ToolSpec(
            name="execute_command",
            description="command",
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
            execute=lambda command: ToolResult("query-ok"),
        ),
        llm.tool_registry.get("write_file"),
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)
    query = _call("execute_command", {"command": "git status"})
    assert query["content"] == "query-ok"

    target = "plan-denied.txt"
    denied = _call("write_file", {"file_path": target, "content": "x"})
    assert denied["content"] == llm.PLAN_MODE_DENIED_MESSAGE


def test_project_rule_cannot_bypass_sandbox_boundary(tmp_path, monkeypatch):
    import config
    from core import permission_store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "YCODE_DIR", tmp_path / "data")
    target = tmp_path.parent / "outside.txt"
    assert permission_store.add_write_file(str(target)) is True
    monkeypatch.setattr(llm, "is_outside_workspace", lambda _path: True)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    asked, requests = [], []
    _auto_answer_pipeline_permission(monkeypatch, 0, asked, requests)
    result = _call("write_file", {"file_path": str(target), "content": "x"})
    assert result["content"].startswith("已成功将内容写入文件")
    assert asked == ["sandbox_outside_access"]


def test_catastrophic_command_never_enqueues_permission(monkeypatch):
    enqueued = []
    monkeypatch.setattr(llm.permission_broker, "enqueue", lambda *a, **k: enqueued.append(a))
    result = _call("execute_command", {"command": "format C:"})
    assert result["content"] == "🚨 危险命令已被安全策略拒绝。请改用范围明确、可恢复的操作。"
    assert enqueued == []
```

Use a fake registry executor in the first two tests so no real command runs; assert executor invocation rather than shell output。

- [ ] **Step 2: Run focused pipeline tests and confirm current behavior differs**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_tool_pipeline.py tests/test_permission_upgrade.py -k "accept_edits or plan_mode_allows or sandbox_boundary or catastrophic" -v
```

Expected: read-only commands are blocked in plan mode and accept-edits command behavior does not match the new matrix。

- [ ] **Step 3: Add a typed authorization result and one coordinator path**

In `core/llm.py` define:

```python
@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    reason: str
    sandbox_bypass: bool = False
    cancelled: bool = False
```

Implement the coordinator with this exact signature: `authorize_tool_call(function_name, arguments, *, tool_spec, outside_workspace=False, event_callback=None, permission_owner=None, parent_turn_id=None, permission_source="主代理", cancel_event=None) -> AuthorizationResult`。

The implementation body follows this exact sequence:

1. Compute `PermissionRisk` from the tool name, arguments, `read_only`, `mutates_files`, `manage_task.action`, and `classify_command_risk`。
2. Mark `plan_write/read/update/retry` and `ask_user` as `plan_internal`。
3. Read task and project rule hits without mutating either store。
4. Read `allow_outside_workspace`; convert it to `sandbox_restricted = not allow_outside_workspace`。
5. Call `decide_permission`。
6. Return immediately for automatic allow or deny。
7. For `sandbox_boundary_requires_confirmation`, present `allow_once`, `deny`, `allow_task_sandbox`。
8. For ordinary approval, present `allow_once`, `deny`, `allow_task`, and `allow_project` when the rule is persistable。
9. Apply only the selected grant; project persistence failure raises `PermissionSystemError` and does not silently create a task grant。

`check_permission_and_ask` becomes a Boolean compatibility wrapper around this coordinator for direct internal callers。

- [ ] **Step 4: Make ToolPipeline consume the typed result**

Replace the plan-mode name whitelist and Boolean permission branch with one authorization call after tool resolution。Map `catastrophic_denied` to the existing dangerous-command text, `plan_mutation_denied` to `PLAN_MODE_DENIED_MESSAGE`, cancellation to `PERMISSION_CANCELLED_MESSAGE`, and user denial to `PERMISSION_DENIED_MESSAGE`。

Update `_plan_constraint` so an `execute_command` classified as `QUERY` is treated like `_PLAN_READ_ONLY_TOOLS`; pending plan items must not force a read-only query through `run_subagent`。Mutation commands keep the existing plan-item orchestration gate。

When `AuthorizationResult.sandbox_bypass` is true, execute the file adapter inside `self.adapters.sandbox_bypass()` so the already-approved path is not asked a second time。Keep `execute_command`'s internal `is_dangerous_command` check as defense in depth。

- [ ] **Step 5: Split adapter path classification from prompting**

Extract a side-effect-free `classify_path_access(function_name, path_value)` returning `inside`, `trusted_read`, `outside`, or `credential_write`。Preserve current `~/.ycode` and trusted Skill read exceptions。The adapter may enforce a missing bypass, but it must no longer decide permission mode or silently auto-approve a write merely because `allow_outside_workspace` is true。

- [ ] **Step 6: Update approval tests for explicit task/project choices**

Change old choice-index assumptions in `tests/test_permission_upgrade.py`:

- `allow_task` stores only task memory and survives UI switching。
- `allow_project` writes `permissions.json` and survives task-grant clearing。
- sandbox `allow_task_sandbox` never writes a project rule。
- shell override, inline command, and composite command omit `allow_project`。
- project whitelist does not bypass a restricted sandbox boundary。

- [ ] **Step 7: Run the core permission regression**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_permission_policy.py tests/test_permission_upgrade.py tests/test_permission_broker.py tests/test_tool_pipeline.py tests/test_session_parallel_isolation.py tests/test_tools.py -v
```

Expected: pass。

- [ ] **Step 8: Request commit approval, then commit Task 5**

```powershell
git add core/harness/tool_pipeline.py core/harness/tool_adapters.py core/llm.py core/tools.py tests/test_tool_pipeline.py tests/test_permission_upgrade.py
git commit -m "feat: enforce unified permission decisions"
```

---

### Task 6: Plan-mode dynamic output contract

**Files:**
- Modify: `core/context_builder.py:52-83`
- Modify: `tests/test_context_builder.py`
- Test: `tests/test_prompt_byte_stability.py`

**Interfaces:**
- Consumes: canonical mode string from `RuntimeContext.permission_mode`。
- Produces: a plan-only instruction in dynamic context; stable prompt bytes remain unchanged。

- [ ] **Step 1: Add failing context tests**

```python
def test_plan_mode_dynamic_context_requires_plan_output():
    runtime = RuntimeContext(
        model_name="test",
        permission_mode="plan",
        working_dir="C:/project",
        current_time="2026-08-28 12:00:00",
    )
    prompt = build_dynamic_prompt(runtime)
    assert "只允许读取信息、执行明确的只读查询和维护计划元数据" in prompt
    assert "最终回答必须输出可执行计划" in prompt
    assert "切换到变更前确认、自动编辑或完全访问模式后再实施" in prompt


def test_non_plan_dynamic_context_has_no_plan_only_instruction():
    runtime = RuntimeContext(
        model_name="test",
        permission_mode="confirm",
        working_dir="C:/project",
        current_time="2026-08-28 12:00:00",
    )
    assert "最终回答必须输出可执行计划" not in build_dynamic_prompt(runtime)
```

- [ ] **Step 2: Run context tests and confirm failure**

Run: `venv\Scripts\python -m pytest tests/test_context_builder.py -k "plan_mode_dynamic or non_plan_dynamic" -v`

Expected: both new assertions fail because the conditional instruction is absent。

- [ ] **Step 3: Add only a dynamic conditional block**

```python
plan_instruction = ""
if runtime.permission_mode == "plan":
    plan_instruction = (
        "\n计划模式约束：只允许读取信息、执行明确的只读查询和维护计划元数据；"
        "不得修改或删除工作区文件。最终回答必须输出可执行计划，并说明需要切换到"
        "变更前确认、自动编辑或完全访问模式后再实施。"
    )
```

Append `plan_instruction` only inside `build_dynamic_prompt`; do not edit `_DECISION_POLICY`, `build_stable_prompt`, `system_prompt.md`, or `system_prompt_gui.md`。

- [ ] **Step 4: Run context and byte-stability tests**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_context_builder.py tests/test_prompt_byte_stability.py -v
```

Expected: pass without changing golden request bytes for non-plan contexts。

- [ ] **Step 5: Request commit approval, then commit Task 6**

```powershell
git add core/context_builder.py tests/test_context_builder.py
git commit -m "feat: require plan output in plan mode"
```

---

### Task 7: CLI approval and whitelist management

**Files:**
- Modify: `main.py:265-272,608-710,1492-1520,1647-1765,1780-1810`
- Modify: `tests/test_main_ui_events.py`
- Modify: `tests/test_permission_broker.py`

**Interfaces:**
- Consumes: keyed `PermissionRequest` options and `permission_store.add_rule/remove_rule`。
- Produces: `/whitelist list|add|remove|clear` with identical normalization to GUI。

- [ ] **Step 1: Add failing CLI parsing and rendering tests**

```python
def test_whitelist_add_and_remove_command(monkeypatch):
    import main
    from core import permission_store

    calls = []
    monkeypatch.setattr(
        permission_store,
        "add_rule",
        lambda rule_type, value: calls.append(("add", rule_type, value))
        or type("R", (), {"status": "added", "normalized_value": value})(),
    )
    monkeypatch.setattr(
        permission_store,
        "remove_rule",
        lambda rule_type, value: calls.append(("remove", rule_type, value))
        or type("R", (), {"status": "removed", "normalized_value": value})(),
    )
    monkeypatch.setattr(main, "print_formatted_text", lambda *_args, **_kwargs: None)

    main.handle_slash_command("/whitelist add command git status")
    main.handle_slash_command("/whitelist remove command git status")

    assert calls == [
        ("add", "command", "git status"),
        ("remove", "command", "git status"),
    ]


def test_cli_permission_prompt_renders_all_backend_options(monkeypatch):
    request = _make_keyed_request(
        [
            ("allow_once", "允许本次"),
            ("deny", "拒绝"),
            ("allow_task", "本任务同类操作放行"),
            ("allow_project", "加入项目白名单"),
        ]
    )
    text = main._confirm_prompt_text(main._permission_prompt(request))
    assert "允许本次" in text
    assert "本任务同类操作放行" in text
    assert "加入项目白名单" in text
```

- [ ] **Step 2: Run focused CLI tests and confirm failure**

Run: `venv\Scripts\python -m pytest tests/test_main_ui_events.py tests/test_permission_broker.py -k "whitelist_add or renders_all" -v`

Expected: `/whitelist add/remove` is not routed and four-option helper is absent。

- [ ] **Step 3: Implement exact CLI grammar**

Parse `/whitelist` with `command_text.strip().split(maxsplit=3)` so the fourth field retains the complete command or path。Map `command`, `write`, `edit` to the three rule types。No argument means `list`。For `clear`, retain the existing project-only behavior and add explicit confirmation using the existing CLI confirmation mechanism before deleting all rules。

Render task rules with `permission_grants.snapshot(current_session)` rather than module-level fallback sets。Update labels from “切会话即清空” to “绑定当前任务；切换界面不清除”。

- [ ] **Step 4: Clear task grants when CLI physically deletes a session**

Immediately after `manager.delete_session(target)` succeeds, call `permission_grants.clear_task(target)` and clear that task's sandbox bypass bucket。Do not clear grants when merely switching sessions。

- [ ] **Step 5: Run CLI and broker tests**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_main_ui_events.py tests/test_permission_broker.py tests/test_permission_upgrade.py -v
```

Expected: pass。

- [ ] **Step 6: Request commit approval, then commit Task 7**

```powershell
git add main.py tests/test_main_ui_events.py tests/test_permission_broker.py
git commit -m "feat: manage permission rules from cli"
```

---

### Task 8: GUI Bridge contract and cleanup

**Files:**
- Modify: `gui_bridge.py:2514-2560,2784-2920`
- Modify: `tests/test_permission_upgrade.py`
- Modify: `tests/test_gui_workspace_groups.py`

**Interfaces:**
- Produces: `add_permission_rule(rule_type, value)`, `remove_permission_rule(rule_type, value)`, `respond_permission_option(request_id, option_key)`。
- Emits: `permission_options: [{"key": str, "label": str}]` for ordinary permission requests。
- Preserves legacy `respond_permission` during the migration so an older frontend cannot crash the bridge。

- [ ] **Step 1: Add failing Bridge API tests**

```python
def test_gui_add_and_remove_permission_rule(monkeypatch):
    from core import permission_store

    bridge = _bare_bridge()
    monkeypatch.setattr(
        permission_store,
        "add_rule",
        lambda kind, value: type(
            "R", (), {"status": "added", "rule_type": kind, "normalized_value": value}
        )(),
    )
    monkeypatch.setattr(
        permission_store,
        "remove_rule",
        lambda kind, value: type(
            "R", (), {"status": "removed", "rule_type": kind, "normalized_value": value}
        )(),
    )
    assert bridge.add_permission_rule("command", "git status")["status"] == "added"
    assert bridge.remove_permission_rule("command", "git status")["status"] == "removed"


def test_gui_permission_event_exposes_keyed_options(monkeypatch):
    emitted = []
    bridge = _bare_bridge()
    bridge.emit_event = lambda kind, payload: emitted.append((kind, payload))
    request = _make_keyed_request(
        [("allow_once", "允许本次"), ("deny", "拒绝")]
    )
    bridge._activate_permission_request(request)
    assert emitted[0][1]["permission_options"] == [
        {"key": "allow_once", "label": "允许本次"},
        {"key": "deny", "label": "拒绝"},
    ]
```

- [ ] **Step 2: Run focused GUI backend tests and confirm failure**

Run: `venv\Scripts\python -m pytest tests/test_permission_upgrade.py tests/test_gui_workspace_groups.py -k "permission_rule or keyed_options or delete_session" -v`

Expected: missing Bridge methods or event field failures。

- [ ] **Step 3: Implement Bridge methods and stable key response**

Both rule mutation methods call `permission_store` and return:

```python
{
    "ok": True,
    "status": result.status,
    "rule_type": result.rule_type,
    "normalized_value": result.normalized_value,
}
```

Catch `ValueError` and `PermissionStoreError` and return `{"ok": False, "msg": str(error)}`。`respond_permission_option` calls `permission_broker.resolve_by_key` and cancels the request timer only after successful resolution。

- [ ] **Step 4: Clear task grants on GUI deletion paths**

After `delete_session` succeeds, clear that session's permission and sandbox buckets。In `delete_folder_group`, clear every session listed in `group_sessions`。Do not clear on rename; migrate the grant bucket from old to new with a new `permission_grants.rename_task(old, new)` operation and test it。

- [ ] **Step 5: Run GUI Bridge and workspace tests**

Run:

```powershell
venv\Scripts\python -m pytest tests/test_permission_upgrade.py tests/test_gui_workspace_groups.py tests/test_session_parallel_isolation.py -v
```

Expected: pass。

- [ ] **Step 6: Request commit approval, then commit Task 8**

```powershell
git add gui_bridge.py core/permission_grants.py tests/test_permission_upgrade.py tests/test_gui_workspace_groups.py tests/test_session_parallel_isolation.py
git commit -m "feat: expose permission management bridge"
```

---

### Task 9: Frontend approval card and manual whitelist UI

**Files:**
- Modify: `frontend/src/types/pywebview.d.ts:14,235-368`
- Modify: `frontend/src/stores/appStore.ts:257-269,2386-2400,2655-2700`
- Modify: `frontend/src/components/PermCard.vue:1-55`
- Create: `frontend/src/components/PermCard.test.ts`
- Modify: `frontend/src/components/SettingsPage.vue:263-313,768-810,980-1040`
- Modify: `frontend/src/components/SettingsPage.test.ts`
- Modify: `frontend/src/components/PermDropdown.vue:35-44`

**Interfaces:**
- Consumes Bridge APIs and `permission_options` from Task 8。
- Produces `PermissionOption { key: string; label: string }` and `PermissionRuleType = 'command' | 'write' | 'edit'` frontend types。

- [ ] **Step 1: Add failing approval-card tests**

```typescript
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PermCard from './PermCard.vue';
import { useAppStore } from '../stores/appStore';

describe('PermCard', () => {
  beforeEach(() => setActivePinia(createPinia()));

  it('renders and submits backend keyed options', async () => {
    const store = useAppStore();
    store.permRequest = {
      show: true,
      sessionName: 's1',
      reqId: 'p1',
      summary: '写入 a.py',
      source: '主代理',
      functionName: 'write_file',
      options: [],
      permissionOptions: [
        { key: 'allow_once', label: '允许本次' },
        { key: 'deny', label: '拒绝' },
        { key: 'allow_task', label: '本任务同类操作放行' },
        { key: 'allow_project', label: '加入项目白名单' },
      ],
    } as never;
    const respond = vi.spyOn(store, 'respondPermissionOption').mockResolvedValue();
    const wrapper = mount(PermCard);
    const buttons = wrapper.findAll('[data-test="permission-option"]');
    expect(buttons).toHaveLength(4);
    await buttons[2].trigger('click');
    expect(respond).toHaveBeenCalledWith('allow_task');
  });
});
```

- [ ] **Step 2: Add failing Settings page tests**

```typescript
it('adds and removes a command permission rule', async () => {
  const add = vi.fn(async () => ({ ok: true, status: 'added' }));
  const remove = vi.fn(async () => ({ ok: true, status: 'removed' }));
  (window as any).pywebview = { api: {
    get_permission_rules: vi.fn(async () => ({
      ok: true, commands: ['git status'], write_files: [], edit_files: [],
    })),
    add_permission_rule: add,
    remove_permission_rule: remove,
  }};
  const store = useAppStore();
  vi.spyOn(store, 'askConfirm').mockResolvedValue(true);
  const wrapper = mount(SettingsPage);
  await wrapper.get('[data-test="permission-nav"]').trigger('click');
  await flushPromises();

  await wrapper.get('[data-test="add-permission-rule"]').trigger('click');
  await wrapper.get('[data-test="permission-rule-type"]').setValue('command');
  await wrapper.get('[data-test="permission-rule-value"]').setValue('git diff');
  await wrapper.get('[data-test="save-permission-rule"]').trigger('click');
  expect(add).toHaveBeenCalledWith('command', 'git diff');

  await wrapper.get('[data-test="remove-permission-command-0"]').trigger('click');
  expect(remove).toHaveBeenCalledWith('command', 'git status');
});
```

- [ ] **Step 3: Run frontend tests and confirm failure**

Run from `frontend`:

```powershell
npm test -- src/components/PermCard.test.ts src/components/SettingsPage.test.ts
```

Expected: missing types, actions, selectors, and Bridge methods。

- [ ] **Step 4: Implement keyed approval rendering**

Add `permissionOptions` to `PermReqState`。On `permission_request`, map `data.permission_options` to `{key, label}`。Implement:

```typescript
async respondPermissionOption(optionKey: string) {
  const reqId = this.permRequest.reqId;
  const sessionName = this.permRequest.sessionName;
  this.permRequest.show = false;
  if (!reqId) return;
  const res = await this._api()?.respond_permission_option(reqId, optionKey);
  if (!res?.ok) {
    this._getSessionRuntime(sessionName).messages.push({
      role: 'error',
      content: '权限响应发送失败: ' + (res?.msg || '未知错误'),
    });
  }
}
```

`PermCard.vue` loops over `permissionOptions` and adds `data-test="permission-option"`。Keep the existing `ask_user` custom-answer branch unchanged。

- [ ] **Step 5: Implement the rule form and row deletion**

In `SettingsPage.vue`, add local state:

```typescript
type PermissionRuleType = 'command' | 'write' | 'edit';
const showPermRuleForm = ref(false);
const permRuleType = ref<PermissionRuleType>('command');
const permRuleValue = ref('');
const permSaving = ref(false);
```

`savePermRule` rejects a blank value locally, calls `add_permission_rule`, closes only on success, and then calls `loadPermRules`。`removePermRule` calls `store.askConfirm`, then `remove_permission_rule`, and refreshes only after success。`clearPermRules` must also call `askConfirm` before the existing clear API。

Render each rule as a row with type badge, monospace value, and deterministic delete selector `remove-permission-${type}-${index}`。On narrow screens, allow the value to wrap and keep the delete button reachable。

- [ ] **Step 6: Update mode descriptions and frontend types**

Use these exact descriptions:

- confirm: `查询自动放行；任何变更都需要确认`
- accept_edits: `普通编辑和构建自动放行；删除、提交、推送等敏感操作需要确认`
- plan: `允许读取、查询和制定计划；禁止修改或删除任务文件`
- full: `沙盒内放行所有非灾难性操作；工作区外由沙盒开关控制`

Add typed Bridge methods for rule add/remove and keyed permission response while retaining legacy methods until the backend/frontend release is complete。

- [ ] **Step 7: Run frontend unit tests and build**

Run from `frontend`:

```powershell
npm test
npm run build
```

Expected: all Vitest tests pass and `vue-tsc -b && vite build` exits 0。

- [ ] **Step 8: Request commit approval, then commit Task 9**

```powershell
git add frontend/src/types/pywebview.d.ts frontend/src/stores/appStore.ts frontend/src/components/PermCard.vue frontend/src/components/PermCard.test.ts frontend/src/components/SettingsPage.vue frontend/src/components/SettingsPage.test.ts frontend/src/components/PermDropdown.vue
git commit -m "feat: manage permission rules in gui"
```

---

### Task 10: Documentation and final verification

**Files:**
- Modify: `README.md:348-370,570-595`
- Modify: CLI help text in `main.py:1500-1525,2550-2570` if Task 7 did not already update every occurrence。
- Verify: all files from Tasks 1-9。

**Interfaces:**
- Consumes the final behavior; produces user-facing documentation only。

- [ ] **Step 1: Update documentation with the exact final semantics**

Document:

- The four-row mode matrix。
- Catastrophic commands are never authorizable。
- Ordinary approval choices: allow once, deny, task grant, project whitelist。
- Sandbox approval choices: allow once, deny, task sandbox bypass。
- `/whitelist list|add|remove|clear` examples for command/write/edit。
- Task grants survive UI switching, remain isolated between concurrent tasks, and disappear on task deletion or app restart。
- File rules are exact paths; command rules are token prefixes。

- [ ] **Step 2: Run Python syntax validation**

Run:

```powershell
venv\Scripts\python -m py_compile main.py core/llm.py core/permission_policy.py core/permission_grants.py core/permission_store.py core/command_policy.py core/harness/tool_pipeline.py core/harness/tool_adapters.py core/context_builder.py gui_bridge.py
```

Expected: exit code 0 with no output。

- [ ] **Step 3: Run the focused backend regression**

```powershell
venv\Scripts\python -m pytest tests/test_permission_policy.py tests/test_permission_upgrade.py tests/test_permission_broker.py tests/test_tool_pipeline.py tests/test_session_parallel_isolation.py tests/test_context_builder.py tests/test_main_ui_events.py tests/test_gui_workspace_groups.py tests/test_prompt_byte_stability.py -v
```

Expected: all tests pass。

- [ ] **Step 4: Run the full backend suite**

Run: `venv\Scripts\python -m pytest`

Expected: all tests pass; any pre-existing unrelated failure must be reported with its exact test name and must not be hidden by retry or fallback。

- [ ] **Step 5: Run the full frontend suite and production build**

```powershell
Set-Location frontend
npm test
npm run build
Set-Location ..
```

Expected: all tests pass and build exits 0。

- [ ] **Step 6: Inspect the final diff and working tree**

```powershell
git diff --check
git status --short
git diff -- core/permission_policy.py core/permission_grants.py core/command_policy.py core/permission_store.py core/permission_broker.py core/llm.py core/harness/tool_pipeline.py core/harness/tool_adapters.py core/context_builder.py main.py gui_bridge.py frontend/src README.md tests
```

Confirm that unrelated pre-existing edits are still present and unchanged。Confirm no generated `frontend/dist`, cache, database, credentials, or local `permissions.json` file is staged。

- [ ] **Step 7: Request final commit approval, then commit documentation or remaining verified changes**

```powershell
git add README.md main.py
git commit -m "docs: document permission modes and whitelist controls"
```

Do not push. A push requires a separate explicit user authorization after reporting the final test results and commit list。
