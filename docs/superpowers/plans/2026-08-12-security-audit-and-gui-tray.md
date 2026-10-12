# Security Audit and GUI Tray Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix confirmed audit findings and implement a testable Windows close-to-tray lifecycle with deterministic Exit cleanup.

**Architecture:** Security boundaries are enforced at shared execution and path helpers, with narrow adapters at MCP, installer, memory, skills, and GUI APIs. A standalone tray lifecycle controller owns window/tray state while `ApiBridge.shutdown()` owns application resources.

**Tech Stack:** Python 3.10+, pytest, pywebview 6, pystray, Pillow, Vue 3/Vitest.

## Global Constraints

- Keep changes small, auditable, and compatible with the existing layered modules.
- Do not claim a regex command policy is a complete sandbox; permission confirmation remains authoritative.
- All new behavior is test-first and must preserve UTF-8 Chinese output.
- GUI worker threads do not directly mutate frontend state during shutdown.

---

### Task 1: Shared command and MCP boundaries

**Files:**
- Modify: `core/command_policy.py`
- Modify: `core/task_manager.py`
- Modify: `core/tools.py`
- Modify: `core/task_scheduler.py`
- Modify: `core/tool_registry.py`
- Modify: `core/mcp_client.py`
- Test: `tests/test_command_policy.py`
- Test: `tests/test_task_manager.py`
- Test: `tests/test_task_scheduler.py`
- Test: `tests/test_mcp_client.py`

**Interfaces:**
- Produces: `is_dangerous_command(command: str) -> bool`, MCP redaction/validation helpers, and closed-state-safe connection methods.

- [ ] Write failing behavior tests for bypass variants, foreground/verification denial, MCP mutation permission, secret redaction, and close/reconnect races.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement the minimal shared policy and lifecycle changes.
- [ ] Run the focused tests until green.

### Task 2: Installer and uninstaller safety

**Files:**
- Modify: `ycode_setup.py`
- Modify: `ycode_uninstall.py`
- Test: `tests/test_setup_engine.py`
- Test: `tests/test_uninstall_engine.py`

**Interfaces:**
- Produces: validated install/archive paths, correctly quoted elevation/shortcut arguments, atomic seed files, and non-recursive self-removal.

- [ ] Write failing tests for quoted paths, Zip Slip, PATH separators, elevated argument quoting, atomic seeding, and self-removal scope.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement the minimal fixes.
- [ ] Run the focused tests until green.

### Task 3: Prompt-data and GUI bridge hardening

**Files:**
- Modify: `core/memory_kairos.py`
- Modify: `core/skills.py`
- Modify: `core/llm.py`
- Modify: `gui_main.py`
- Modify: `gui_bridge.py`
- Test: `tests/test_memory_kairos.py`
- Test: `tests/test_skills.py`
- Test: `tests/test_gui_lifecycle.py`

**Interfaces:**
- Produces: bounded/delimited prompt data, resolved skill-root checks, strict Y-CODE dev-server identity, safe local bridge paths, generalized redaction, and `ApiBridge.shutdown()`.

- [ ] Write failing tests for each externally observable boundary.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement the minimal fixes and resource cleanup.
- [ ] Run the focused tests until green.

### Task 4: Windows tray lifecycle

**Files:**
- Create: `core/gui_tray.py`
- Modify: `gui_main.py`
- Modify: `requirements.txt`
- Modify: `ycode.spec`
- Test: `tests/test_gui_lifecycle.py`

**Interfaces:**
- Produces: `TrayLifecycle(window, shutdown, icon_path, tray_factory=None)` with `start()`, `handle_closing()`, `show_window()`, and `exit_application()`.

- [ ] Write failing tests for close-to-tray, unavailable tray fallback, show, and idempotent Exit.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement pystray integration and wire it before `webview.start()`.
- [ ] Run focused tests until green.

### Task 5: Full verification and audit disposition

**Files:**
- Review all modified files and `AUDIT-REPORT.md` findings.

- [ ] Run focused security and lifecycle tests.
- [ ] Run the full Python test suite.
- [ ] Run Python compile checks.
- [ ] Run frontend tests and production build.
- [ ] Inspect `git diff --check`, final diff, and status.
- [ ] Report each audit item as confirmed/fixed, partial/hardened, false/outdated, or architectural follow-up.
