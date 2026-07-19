# Advanced Task Orchestration Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

Goal: Turn the current in-memory plan and generic subagent threads into a recoverable task orchestrator that schedules dependency-aware work and renders one live checklist in the terminal.

Architecture: Keep the existing prompt-toolkit UI and event stream. Replace the plan list with a persisted session plan containing dependency-aware items, add a scheduler that only starts ready items, and let tool/subagent/test outcomes update item status automatically. Use role-specific subagent policies (explore, implement, test, review) and keep plan mode read-only until the user approves implementation.

Tech Stack: Python 3.12, dataclasses, JSON persistence already used by Y-code, threading, concurrent.futures, prompt_toolkit, pytest.

---

### Task 1: Define a persisted dependency-aware plan

Files:
- Modify: core/plan.py
- Create: tests/test_plan.py coverage for dependencies, persistence, and status transitions

- [x] Add PlanItem fields: id, subject, status, depends_on, owner, files, verification, stage, attempts, result, and error.
- [x] Validate duplicate IDs, unknown dependencies, self-dependencies, invalid statuses, and cycles in replace.
- [x] Add ready_items() and scheduler-owned completion so only pending items whose dependencies are completed can start.
- [x] Persist the active plan under .ycode_tasks/plans/<session>.json with atomic replacement and load it when a session resumes.
- [x] Keep snapshot() deep-copy semantics and retain compatibility with existing plan_write and plan_update callers.

### Task 2: Add scheduler-owned execution state

Files:
- Create: core/task_scheduler.py
- Modify: core/plan.py
- Modify: core/tools.py
- Modify: core/llm.py
- Create: tests/test_task_scheduler.py

- [x] Implement TaskScheduler.start_ready(), start_item(), and complete() around the plan manager.
- [x] Enforce dependency readiness, a bounded worker pool, and no overlapping file owners.
- [x] Mark items in_progress when execution starts and completed only after a usable result and optional verification command pass.
- [x] Record failed, blocked, cancelled, attempts, and error text without allowing the model to fake completion.
- [x] Add retry, pause, resume, cancel, and restart recovery operations that reuse the persisted plan and skip completed items.
- [x] Emit plan item events with a live snapshot; plan persistence emits plan_snapshot.

### Task 3: Add role-specific agents and execution phases

Files:
- Modify: core/tools.py
- Modify: core/tool_registry.py
- Modify: core/llm.py
- Modify: system_prompt.md
- Modify: core/context_builder.py
- Create: tests/test_agent_roles.py

- [x] Add subagent_type to the subagent schema with explore, implement, test, and review.
- [x] Give explore/review agents read-only tools, implement agents file tools, and test agents command tools.
- [x] Add explicit plan/build/review phase state to plan items and session context.
- [x] Keep subagents from recursively spawning agents.
- [x] Route implement-agent mutations through the existing permission/plan approval gate.

### Task 4: Render one live task checklist in the TUI

Files:
- Modify: core/turn_events.py
- Modify: core/turn_renderer.py
- Modify: main.py
- Modify: core/tool_presenter.py
- Create: tests/test_task_checklist_renderer.py

- [x] Render a compact checklist with status markers, completed count, current item, stage, and error state.
- [x] Update the same checklist on scheduler events instead of printing duplicate plan lines.
- [x] Keep reasoning, tool summaries, assistant output, and the existing startup layout unchanged.
- [x] Show concise execution and completion lines only after real scheduler events and verification.

### Task 5: Add user controls and recovery

Files:
- Modify: main.py
- Modify: core/plan.py
- Modify: core/task_manager.py
- Modify: system_prompt.md
- Create: tests/test_plan_commands.py

- [x] Add /plan, /plan pause, /plan resume, /plan retry <id>, and /plan cancel commands.
- [x] Load persisted plans on startup without changing the welcome screen.
- [x] Ensure cancellation propagates to owned permissions, foreground subagents, scheduler workers, and verification processes.
- [x] Keep plan state and blockers available to the live panel and runtime context for final model summaries.

### Task 6: Verification and compatibility

Files:
- Modify: tests/test_llm_events.py
- Modify: tests/test_main_ui_events.py
- Modify: tests/test_context_builder.py
- Modify: setup.py
- Modify: main.py

- [x] Add regression coverage for dependency chains, independent parallel tasks, failed verification, retry, cancellation, restart resume, UI events, and real child-agent plan binding.
- [x] Run focused tests after each task.
- [x] Run venv\Scripts\python -m pytest -q.
- [x] Run the project py_compile command and venv\Scripts\python -m pip check.
- [x] Bump the displayed/package version to 1.0.17 after the orchestration, network, and TUI streaming fixes were verified.
