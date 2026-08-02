# CLI Permission and Network Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the confirmed command, trusted-path, plan-verification, MCP, and SSRF permission bypasses without changing unrelated CLI behavior.

**Architecture:** Centralize command authorization in a pure policy module, separate trusted reads from writes, route plan verification through an injected authorized runner, make MCP configuration changes permissioned and atomic, and validate every HTTP redirect target. Existing public tool names remain unchanged.

**Tech Stack:** Python 3.12, pytest, prompt_toolkit permission broker, urllib, SQLite-independent unit tests.

## Global Constraints

- 默认失败关闭：权限、路径、命令、迁移和网络目标无法证明安全时直接拒绝并报告原因。
- 不使用降级、兜底或启发式后处理掩盖错误；不确定的编辑必须失败。
- 每个生产代码变更先增加能复现原缺陷的测试，确认测试按预期失败后才实现修复。
- 每个任务独立提交并接受任务级代码审查；Critical/Important 审查意见必须闭环。
- 不依赖外网完成测试；涉及 HTTP、LiteLLM 和 MCP 的测试使用受控本地边界或依赖注入。
- 不修改用户已有未跟踪文件，不直接在 `master` 开发。
- 不在日志、工具结果、模型上下文或测试失败信息中暴露 API Key、token、password、cookie 或数据库内容。

---

### Task 1: Structure Session Command Authorization

**Files:**
- Create: `core/command_policy.py`
- Modify: `core/llm.py:194-235,294-303,338-352`
- Test: `tests/test_command_policy.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `session_rule_for_command(command: str) -> tuple[str, ...] | None`
- Produces: `command_matches_session_rule(command: str, rule: tuple[str, ...]) -> bool`
- Consumes: existing `session_allowed_commands`, converted from `set[str]` to `set[tuple[str, ...]]`

- [ ] **Step 1: Write failing command-boundary tests**

```python
from core.command_policy import command_matches_session_rule, session_rule_for_command


def test_git_status_rule_does_not_authorize_other_git_subcommands():
    rule = session_rule_for_command("git status")
    assert rule == ("git", "status")
    assert command_matches_session_rule("git status --short", rule)
    assert not command_matches_session_rule("git reset --hard", rule)
    assert not command_matches_session_rule("git push --force", rule)


def test_shell_composition_is_never_session_allowlisted():
    assert session_rule_for_command("git status; whoami") is None
    assert session_rule_for_command("git status && whoami") is None
    assert session_rule_for_command("git status | Out-File result.txt") is None
    assert session_rule_for_command("git status\nwhoami") is None
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_command_policy.py -q`

Expected: collection fails because `core.command_policy` does not exist.

- [ ] **Step 3: Implement the pure authorization policy**

Create a policy module that tokenizes with `shlex.split(posix=os.name != "nt")`, rejects empty or malformed input, rejects `;`, `&`, `|`, `<`, `>`, backticks, CR/LF, `$(` and PowerShell call-expression syntax before tokenization, rejects existing inline execution flags, and returns:

```python
(executable, subcommand)
```

for ordinary multiword commands. Version/help queries retain the full query tokens and match exact argv only. Single-token commands also match exact argv only. Python/pip script rules retain `(executable, script_path)` and inline module/code execution returns `None`.

Matching must parse the candidate through the same safe tokenizer and compare complete tuple elements, never a raw string prefix.

- [ ] **Step 4: Run policy tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_command_policy.py -q`

Expected: all tests pass.

- [ ] **Step 5: Integrate the policy with `core.llm` and update compatibility tests**

Replace `_session_prefix_for_command` and `_command_matches_prefix` internals with imports/delegation to the new policy while preserving their public names for extension compatibility. Update `tests/test_llm.py` expectations so `git status` stores `("git", "status")`, and add an integration assertion that a stored status rule cannot auto-approve reset or shell composition.

- [ ] **Step 6: Run the focused regression group**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_command_policy.py tests/test_llm.py tests/test_llm_events_fixes.py -q`

Expected: all tests pass without warnings introduced by this task.

- [ ] **Step 7: Commit**

```bash
git add core/command_policy.py core/llm.py tests/test_command_policy.py tests/test_llm.py
git commit -m "fix: scope session command authorization"
```

### Task 2: Separate Trusted Reads from Protected Writes

**Files:**
- Modify: `core/skills.py:102-158`
- Modify: `core/tools.py:120-186`
- Modify: `core/llm.py:1389-1439,1600-1667`
- Test: `tests/test_tools.py`
- Test: `tests/test_llm_events_fixes.py`

**Interfaces:**
- Produces: `is_trusted_read_path(file_path: str) -> bool`
- Produces: `_resolve_workspace_path(file_path, *, allow_trusted_read: bool = False) -> Path`
- Preserves: `is_trusted_path` as a compatibility alias for trusted reads only; write paths must not call it.

- [ ] **Step 1: Write failing tests for sensitive Y-code files and Skill writes**

Add tests using temporary home/workspace directories and monkeypatched Skill directories:

```python
def test_ycode_config_is_not_a_trusted_read_path(...):
    assert not is_trusted_read_path(str(fake_home / ".ycode" / "config.json"))


def test_skill_markdown_is_trusted_for_read_only(...):
    assert is_trusted_read_path(str(skill_dir / "SKILL.md"))


def test_sandbox_guard_requires_confirmation_for_writing_skill_file(...):
    # write_file outside workspace must call the permission path even under a Skill directory
```

Also test that `.ycode/ycode.db`, `.ycode/mcp.json`, `.ycode/system_prompt.md`, and `.agents` files do not receive blanket trust.

- [ ] **Step 2: Run the tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_llm_events_fixes.py -k "trusted or sandbox_guard" -q`

Expected: sensitive files are currently treated as trusted and the new assertions fail.

- [ ] **Step 3: Implement operation-specific path policy**

Restrict trusted reads to files beneath configured Skill directories that are required by a Skill package. Remove blanket trust for `~/.ycode` and `~/.agents`. Change `_resolve_workspace_path` to accept `allow_trusted_read=False`; only `read_file` and explicitly read-only Skill resource consumers pass `True`. `write_file` and `edit_file` always use the default.

In `_sandbox_outside_guard`, bypass confirmation for trusted paths only when `function_name == "read_file"`. All write/edit operations outside the workspace request `sandbox_outside_access` regardless of path.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_llm_events_fixes.py -k "trusted or sandbox or outside_workspace" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Run file-tool regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_agent_turn_e2e.py tests/test_agent_service.py -q`

Expected: all tests pass; ordinary workspace file reads/writes and approved outside access remain functional.

- [ ] **Step 6: Commit**

```bash
git add core/skills.py core/tools.py core/llm.py tests/test_tools.py tests/test_llm_events_fixes.py
git commit -m "fix: protect sensitive paths from trusted bypass"
```

### Task 3: Authorize Plan Verification Commands

**Files:**
- Modify: `core/task_scheduler.py:224-264,398-453`
- Modify: `core/tools.py:2900-2980`
- Test: `tests/test_task_scheduler.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `TaskScheduler.complete(..., verification_runner: VerificationRunner | None)` that refuses command verification when no authorized runner exists.
- Produces: a subagent terminal callback runner that calls `check_permission_and_ask("execute_command", ...)` before `execute_command`.
- Preserves: custom runners used by unit tests and embedding callers.

- [ ] **Step 1: Write failing fail-closed scheduler tests**

```python
def test_command_verification_without_authorized_runner_fails_closed():
    # in-progress command-verification item
    assert scheduler.complete("build") is False
    assert plan.get("build")["status"] == "failed"
    assert "授权" in plan.get("build")["error"]


def test_command_verification_uses_injected_runner_only():
    seen = []
    assert scheduler.complete("build", verification_runner=lambda cmd: (seen.append(cmd) or True, "ok"))
    assert seen == ["pytest -q"]
```

- [ ] **Step 2: Run scheduler tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_task_scheduler.py -k "verification" -q`

Expected: the no-runner case currently executes a shell command instead of failing closed.

- [ ] **Step 3: Remove the direct shell fallback**

Change `complete()` so command verification requires an explicit runner or configured `_custom_verification_runner`. Replace `_run_verification` as the implicit default with a fail-closed result; retain a private subprocess runner only for explicitly configured trusted embedding tests if necessary, never as the default CLI path.

- [ ] **Step 4: Add the authorized CLI runner**

In the subagent completion path, create a closure carrying `permission_owner`, `parent_turn_id`, `permission_source`, and `cancel_event`. It must:

1. call `check_permission_and_ask("execute_command", {"command": command, ...})`;
2. return `(False, "验证命令未获用户授权")` when denied;
3. call `execute_command(command, cancel_event=...)` only when approved;
4. translate the real command result into `(passed, detail)` without invoking another shell path.

- [ ] **Step 5: Run integration regressions**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_task_scheduler.py tests/test_tools.py -k "verification or plan_item" -q`

Expected: all selected tests pass and a denied verification does not spawn a process.

- [ ] **Step 6: Commit**

```bash
git add core/task_scheduler.py core/tools.py tests/test_task_scheduler.py tests/test_tools.py
git commit -m "fix: require authorization for plan verification"
```

### Task 4: Permission and Atomicity for MCP Operations

**Files:**
- Modify: `core/tool_registry.py:404-469`
- Modify: `core/llm.py:1594-1618,1944-1981`
- Modify: `core/mcp_client.py:43-70,300-433`
- Test: `tests/test_tool_registry.py`
- Test: `tests/test_llm_events.py`
- Test: `tests/test_mcp_client.py`

**Interfaces:**
- Produces: `MCPManager._save_config_locked() -> None`, an atomic same-directory write used under `self._config_lock`.
- Changes: all exposed MCP admin `ToolSpec` objects have `requires_permission=True` except listing.
- Changes: dynamic MCP calls default to permission required.

- [ ] **Step 1: Write failing MCP permission tests**

Add registry assertions that add/remove/set-enabled require permission and list remains read-only. Add an LLM event test with a fake dynamic MCP schema asserting `check_permission_and_ask` is called before `call_tool_sync`, and denial prevents the call.

- [ ] **Step 2: Write failing configuration safety tests**

```python
def test_list_servers_redacts_every_environment_value(...):
    # PASSWORD, COOKIE, short tokens, and API_KEY all become "<redacted>"


def test_add_server_uses_atomic_replace_and_preserves_valid_json_on_failure(...):
    # force os.replace failure; original mcp.json remains byte-for-byte valid
```

Also add a two-thread mutation test proving add/enable operations do not lose each other's updates.

- [ ] **Step 3: Run MCP tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tool_registry.py tests/test_llm_events.py tests/test_mcp_client.py -k "mcp" -q`

Expected: admin permission, dynamic tool permission, full redaction, or atomicity assertions fail.

- [ ] **Step 4: Implement permission defaults**

Set MCP admin mutation specs to `requires_permission=True`. In `_execute_single_tool_call`, detect dynamic MCP schema membership before permission evaluation and default it to `True`; only built-in registry tools with explicit read-only metadata may bypass confirmation.

- [ ] **Step 5: Implement locked atomic MCP configuration**

Add `self._config_lock = threading.RLock()`. Under that lock, reload current disk state, mutate, write JSON to a temporary file in the same directory, flush and `os.fsync`, then `os.replace`. Clean the temporary file on failure. Reconnect only after the committed write. Redact every env value as the literal `<redacted>`.

- [ ] **Step 6: Run MCP regression group**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tool_registry.py tests/test_llm_events.py tests/test_mcp_client.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add core/tool_registry.py core/llm.py core/mcp_client.py tests/test_tool_registry.py tests/test_llm_events.py tests/test_mcp_client.py
git commit -m "fix: enforce MCP permissions and atomic config"
```

### Task 5: Validate Private Targets and Every HTTP Redirect

**Files:**
- Modify: `core/tools.py:650-826`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `_SSRFRedirectHandler(HTTPRedirectHandler)` that validates each redirect destination.
- Produces: `_open_http_request(request: Request, timeout: float)` as the monkeypatchable HTTP boundary.
- Changes: strict SSRF behavior is the default; exact `fetch_url_allow_hosts` entries remain the only private-target exception.

- [ ] **Step 1: Replace permissive expectations with failing secure-default tests**

Change the existing default-private-IP test to assert `SSRFBlockedError`. Keep a fake-IP compatibility test only when its hostname is explicitly present in `fetch_url_allow_hosts`.

Add:

```python
def test_fetch_url_revalidates_redirect_target(monkeypatch):
    # public initial URL redirects to http://127.0.0.1/admin
    # assert SSRFBlockedError before the redirected request is opened


def test_strict_ssrf_rejects_dns_failure_by_default(monkeypatch):
    # no config flag required
```

- [ ] **Step 2: Run SSRF tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tools.py -k "ssrf or redirect" -q`

Expected: default private targets and redirect targets are currently allowed.

- [ ] **Step 3: Make safe target validation the default**

Remove the permissive early return. Continue to honor an exact normalized host allowlist before IP/DNS checks. Reject any literal or resolved address that is not globally routable. DNS failure remains a rejection.

- [ ] **Step 4: Add redirect-time validation**

Subclass `urllib.request.HTTPRedirectHandler`; resolve relative `Location` values with `urljoin`, then call `_validate_http_url` and `_check_ssrf_url` before delegating. `_open_http_request` must use an opener containing this handler. Cap redirects using urllib's existing redirect limits.

- [ ] **Step 5: Run HTTP and SSRF regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tools.py -k "fetch_url or ssrf or http" -q`

Expected: all selected tests pass without real network access.

- [ ] **Step 6: Run the complete phase regression group**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_command_policy.py tests/test_llm.py tests/test_llm_events.py tests/test_llm_events_fixes.py tests/test_tool_registry.py tests/test_task_scheduler.py tests/test_mcp_client.py tests/test_tools.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add core/tools.py tests/test_tools.py
git commit -m "fix: enforce SSRF checks across redirects"
```
